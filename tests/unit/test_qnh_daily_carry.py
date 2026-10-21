"""Tests for the daily carry-baseline (每日增量) semantics."""

import csv
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

import qnh_capture_common as C
import qnh_daily_launch as launch
import qnh_daily_run as daily


def carry_row(index=1, name="商品A", barcode="6900000000001", prefix=""):
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": str(index), "商品名称": name, "条码": barcode,
        "店内码": "A-1", "货号": "SKU-1", "状态": "携带",
        "时刻": "00:06:00", "采集时间": "2026-10-16T00:06:00",
        "证据前缀": prefix, "规格": "100g", "月售": "10", "库存": "2",
        "售罄": "否", "活动品": "否",
        "美团闪购价": "10", "饿了么价": "10", "京东到家价": "10",
        "采购价": "5/袋", "价格原文": "10 | 10 | 10 | 5/袋",
        "开放平台标识": "SPU-1", "SPU": "SPU-1", "SKU": "SKU-1",
        "重量": "100克(g)", "采购价_详情": "5", "采购单位_详情": "袋",
        "美团闪购状态": "已上架", "美团闪购零售价": "10", "美团闪购库存": "2",
        "饿了么状态": "未上线",
        "京东到家状态": "已下架",
        "可售日期": "不限", "礼袋": "0个礼袋", "是否力荐": "否", "是否标品": "标品",
    })
    return row


class DatabaseConfigTests(unittest.TestCase):
    def test_project_config_precedes_legacy_and_env_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            project = os.path.join(directory, "project.json")
            legacy = os.path.join(directory, "legacy.json")
            override = os.path.join(directory, "override.json")
            for path in (project, legacy, override):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("{}")
            original_candidates = C.DEFAULT_DB_CONFIG_FILES
            original_override = os.environ.get("QNH_DB_CONFIG")
            try:
                C.DEFAULT_DB_CONFIG_FILES = (project, legacy)
                os.environ.pop("QNH_DB_CONFIG", None)
                self.assertEqual(C.db_config_path(), project)
                os.environ["QNH_DB_CONFIG"] = override
                self.assertEqual(C.db_config_path(), override)
            finally:
                C.DEFAULT_DB_CONFIG_FILES = original_candidates
                if original_override is None:
                    os.environ.pop("QNH_DB_CONFIG", None)
                else:
                    os.environ["QNH_DB_CONFIG"] = original_override


class DailyRunnerLockTests(unittest.TestCase):
    def test_runner_lock_is_exclusive_and_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            original_root = daily.ROOT
            daily.ROOT = directory
            try:
                first = daily.acquire_runner_lock()
                with self.assertRaisesRegex(RuntimeError, "已有每日采集 runner"):
                    daily.acquire_runner_lock()
                first.close()
                second = daily.acquire_runner_lock()
                second.close()
            finally:
                daily.ROOT = original_root


class DailyCompletionTests(unittest.TestCase):
    def write_result(self, directory, stamp, **updates):
        payload = {
            "ok": True, "committed": True, "state": "committed",
            "snapshot_id": "snapshot-ok", "batch_id": "batch-ok",
            "status": "success", "rows": 10, "fact_rows": 30,
            "new_matches": 2, "manifest_path": "/tmp/final.manifest.json",
            "post_validation": {"ok": True, "passed": 19, "failed": 0},
        }
        payload.update(updates)
        path = os.path.join(directory, f"qnh_category_full_{stamp}.result.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path, payload

    def test_committed_result_ignores_later_failed_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = self.write_result(directory, "20261019-133718")
            self.write_result(directory, "20261019-143245", ok=False,
                              committed=None, state="load_failed_or_commit_unknown",
                              post_validation={})
            self.assertEqual(daily.committed_result(directory), expected)

    def test_dry_run_result_accepts_successful_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = self.write_result(
                directory, "20261019-133718", committed=False,
                state="dry_run_rolled_back", post_validation=None)
            self.assertEqual(daily.dry_run_result(directory), expected)
            self.assertIsNone(daily.committed_result(directory))

    def test_main_restores_completed_status_without_touching_device(self):
        with tempfile.TemporaryDirectory() as directory:
            run_date = "20261018"
            out_dir = os.path.join(directory, "qnh_daily", run_date)
            os.makedirs(out_dir)
            result_path, result = self.write_result(out_dir, "20261019-133718")
            original_root = daily.ROOT
            daily.ROOT = directory
            try:
                with mock.patch.object(daily, "adb", side_effect=AssertionError("ADB called")), \
                     mock.patch.object(daily, "acquire_runner_lock",
                                       side_effect=AssertionError("lock acquired")), \
                     mock.patch("sys.argv", ["qnh_daily_run.py", "--date", run_date]):
                    daily.main()
            finally:
                daily.ROOT = original_root
            with open(os.path.join(out_dir, "daily.status.json"), encoding="utf-8") as fh:
                status = json.load(fh)
            self.assertEqual(status, {
                "updated_at": status["updated_at"],
                **daily.completed_status(result_path, result),
            })


class DailyLaunchTests(unittest.TestCase):
    def test_stop_is_idempotent_after_launchd_service_disappears(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(launch, "ROOT", directory), \
             mock.patch.object(launch, "_launchctl", return_value=subprocess.CompletedProcess(
                 [], 3, stdout="", stderr="Boot-out failed: 3: No such process")):
            out_dir, path = launch.paths_for("20261020")
            os.makedirs(out_dir)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("stale plist")
            launch.stop("20261020")
            self.assertFalse(os.path.exists(path))

    def test_plist_is_explicitly_one_shot(self):
        payload = launch.plist_for("20261020")
        self.assertIs(payload["RunAtLoad"], True)
        self.assertIs(payload["KeepAlive"], False)
        self.assertEqual(payload["Label"], "com.jalen.qnh.daily.20261020")
        self.assertIn("qnh_daily_run.py --date 20261020",
                      payload["ProgramArguments"][-1])

    def test_start_bootstraps_plist_instead_of_submit(self):
        calls = []

        def fake_launchctl(*args):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1 if args[0] == "print" else 0,
                                               stdout="", stderr="not found")

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(launch, "ROOT", directory), \
             mock.patch.object(launch, "_launchctl", side_effect=fake_launchctl):
            launch.start("20261020")
            _, plist_path = launch.paths_for("20261020")
            with open(plist_path, "rb") as fh:
                payload = plistlib.load(fh)
        self.assertEqual(calls, [
            ("print", f"gui/{os.getuid()}/com.jalen.qnh.daily.20261020"),
            ("bootstrap", f"gui/{os.getuid()}", plist_path),
        ])
        self.assertIs(payload["KeepAlive"], False)


class CarryStatusTests(unittest.TestCase):
    def test_carry_is_complete_status(self):
        self.assertIn("携带", C.COMPLETE_ROW_STATUSES)
        row = carry_row()
        self.assertEqual(C.row_detail_errors(row), [])
        self.assertNotIn("携带", C.LISTING_STATUS_MAP.values())

    def test_carry_row_fails_strict_gate_without_current_detail_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = os.path.join(directory, "qnh_scan_20261016-000500")
            csv_path = prefix + ".csv"
            row = carry_row(prefix=prefix)
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
                writer.writeheader()
                writer.writerows([row])
            profile = C.load_profile("xiniu-changan")
            manifest = C.build_manifest(
                capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()),
                run_id=str(uuid.uuid4()), state="success", profile=profile,
                device_id="R28M31LJX0E",
                scope={"list_title": "叶", "category": "休闲零食/叶",
                       "expected_product_count": 1, "requested_count": 0},
                capture={"method": "app_ui", "started_at": "2026-10-16T00:06:00",
                         "run_started_at": "2026-10-16T00:06:00",
                         "finished_at": "2026-10-16T00:07:00",
                         "collector_version": "test", "pace": "ultra",
                         "tall_screen": True, "verify_every": 1,
                         "checkpoint_every": 25, "record": False,
                         "dismiss_order_dialogs": False},
                artifacts={"csv_path": csv_path, "evidence_prefix": prefix,
                           "video_path": None, "csv_sha256": C.sha256_file(csv_path)},
                rows=[row], stop_reason="test")
            manifest_path = prefix + ".manifest.json"
            C.atomic_write_json(manifest_path, manifest)
            report = C.validate_manifest(
                manifest_path, require_complete=True, expected_profile=profile)
            self.assertFalse(report.ok)
            self.assertTrue(any("携带" in error for error in report.errors), report.errors)

    def test_carry_price_key_normalizes_amounts(self):
        self.assertEqual(C.carry_price_key("45.00"), C.carry_price_key("45"))
        self.assertEqual(C.carry_price_key("无数据"), "无数据")
        # 非数值非空文本保留原文：意外文案 ≠ 基线 → 触发详情页（安全方向）
        self.assertEqual(C.carry_price_key("abc"), "abc")
        self.assertIsNone(C.carry_price_key(""))


class CarryCompareTests(unittest.TestCase):
    def make_card(self, price4, spec="100g", sold_out=False, promo=False):
        return {"prices": list(price4), "spec": spec, "month_sales": "10",
                "stock": "2", "sold_out": sold_out, "promo_item": promo}

    def fingerprint(self, card):
        row = {"规格": card["spec"], "售罄": "是" if card["sold_out"] else "否",
               "活动品": "是" if card["promo_item"] else "否",
               "美团闪购价": card["prices"][0], "饿了么价": card["prices"][1],
               "京东到家价": card["prices"][2], "采购价": card["prices"][3]}
        return {
            "spec": C.normalize_name(row["规格"]),
            "sold_out": row["售罄"] == "是",
            "promo_item": row["活动品"] == "是",
            "prices": [C.carry_price_key(row[c]) for c in
                       ("美团闪购价", "饿了么价", "京东到家价", "采购价")],
        }

    def test_identical_fingerprint_does_not_reuse_historical_details(self):
        import qnh_product_scan as scan
        card = self.make_card(["45", "50", "55", "3.5"])
        self.assertIsNone(
            scan.compare_card_baseline(card, {"fingerprint": self.fingerprint(card),
                                              "detail": ["d"]}))

    def test_persistent_price_change_still_triggers(self):
        import qnh_product_scan as scan
        base = self.make_card(["45", "50", "55", "3.5"])
        today = self.make_card(["44", "50", "55", "3.5"])
        self.assertIsNone(scan.compare_card_baseline(
            today, {"fingerprint": self.fingerprint(base), "detail": ["d"]}))

    def test_amount_format_difference_still_requires_visit(self):
        import qnh_product_scan as scan
        base = self.make_card(["45", "50", "55", "3.5"])
        today = self.make_card(["45.00", "50.0", "55", "3.50"])
        self.assertIsNone(scan.compare_card_baseline(
            today, {"fingerprint": self.fingerprint(base), "detail": ["d"]}))

    def test_unknown_baseline_price_component_still_requires_visit(self):
        import qnh_product_scan as scan
        today = self.make_card(["45", "50", "55", "无数据"])
        baseline = self.fingerprint(self.make_card(["45", "50", "55", "45"]))
        baseline["prices"][3] = None
        self.assertIsNone(scan.compare_card_baseline(
            today, {"fingerprint": baseline, "detail": ["d"]}))

    def test_concrete_baseline_price_still_triggers(self):
        import qnh_product_scan as scan
        base = self.make_card(["45", "50", "55", "3.5"])
        today = self.make_card(["45", "50", "55", "无数据"])
        self.assertIsNone(scan.compare_card_baseline(
            today, {"fingerprint": self.fingerprint(base), "detail": ["d"]}))

    def test_stock_month_and_sold_out_changes_require_visit(self):
        # 未建立逐字段详情来源时间前，所有商品都实访。
        import qnh_product_scan as scan
        base = self.make_card(["45", "50", "55", "3.5"])
        today = dict(base, month_sales="999", stock="", sold_out=True)
        self.assertIsNone(scan.compare_card_baseline(
            today, {"fingerprint": self.fingerprint(base), "detail": ["d"]}))

    def test_spec_or_promo_triggers(self):
        import qnh_product_scan as scan
        base = self.make_card(["45", "50", "55", "3.5"])
        for variant in (dict(base, spec="200g"), dict(base, promo_item=True)):
            self.assertIsNone(scan.compare_card_baseline(
                variant, {"fingerprint": self.fingerprint(base), "detail": ["d"]}))


if __name__ == "__main__":
    unittest.main()
