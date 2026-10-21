"""Tests for leaf-category shard aggregation."""

import csv
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

import qnh_capture_common as C
import qnh_category_pipeline as category_pipeline
from tests.unit.test_qnh_manifest_pipeline import complete_row


def distinct_skus(rows):
    """不同商品给不同 SKU（同名商品共用）。

    合成数据默认所有商品共用 SKU-1，会造出假的身份冲突；真实数据里 SKU 属于
    具体商品。
    """
    for row in rows:
        row["SKU"] = "SKU-" + C.text_value(row.get("商品名称"))
    return rows


class CategoryAggregationTests(unittest.TestCase):
    def make_segment(self, root, order, top, leaf, rows):
        directory = os.path.join(root, f"{order:03d}")
        os.makedirs(directory)
        prefix = os.path.join(directory, "qnh_scan_segment")
        csv_path = prefix + ".csv"
        for index, row in enumerate(rows, 1):
            row["序号"] = str(index)
            row["证据前缀"] = prefix
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        profile = C.load_profile("xiniu-changan")
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()), run_id=str(uuid.uuid4()),
            state="success", profile=profile, device_id="R28M31LJX0E",
            scope={"list_title": leaf, "category": f"{top}/{leaf}",
                   "expected_product_count": len(rows), "requested_count": 0},
            capture={"method": "app_ui", "started_at": "2026-10-09T10:00:00",
                     "run_started_at": "2026-10-09T10:00:00",
                     "finished_at": "2026-10-09T10:01:00",
                     "collector_version": "test", "pace": "ultra",
                     "tall_screen": True, "verify_every": 1,
                     "checkpoint_every": 25, "record": False,
                     "dismiss_order_dialogs": False},
            artifacts={"csv_path": csv_path, "evidence_prefix": prefix,
                       "video_path": None, "csv_sha256": C.sha256_file(csv_path)},
            rows=rows)
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest)
        return manifest_path

    def make_state(self, root, completed):
        """state 记录必须带 manifest_sha256（聚合时按它核对工件）。"""
        state_path = os.path.join(root, "capture.state.json")
        C.atomic_write_json(state_path, {
            "capture_id": str(uuid.uuid4()), "batch_id": str(uuid.uuid4()),
            "dismiss_order_dialogs": False,
            "completed": {order: {"manifest_path": path,
                                  "manifest_sha256": C.sha256_file(path)}
                          for order, path in completed.items()},
        })
        return state_path

    def merge(self, root, inventory, segments, state_path, *, full_count):
        """聚合。完整门禁不通过时 merge_segments 会抛错，但 manifest 已落盘。

        返回 (manifest 路径, 错误信息或 None)。
        """
        try:
            path = category_pipeline.merge_segments(
                root, inventory, segments, state_path, full_count=full_count)
            return path, None
        except C.CaptureContractError as exc:
            paths = [os.path.join(root, name) for name in os.listdir(root)
                     if name.endswith(".manifest.json")]
            self.assertTrue(paths, "聚合未落盘 manifest")
            return paths[0], str(exc)

    def test_merge_deduplicates_cross_category_and_maps_primary_category(self):
        with tempfile.TemporaryDirectory() as root:
            first = self.make_segment(
                root, 1, "休闲零食", "薯片薯条",
                distinct_skus([complete_row(1, "商品A", "6900000000001"),
                               complete_row(2, "商品B", "6900000000002")]))
            second = self.make_segment(
                root, 2, "精选业务", "精选业务",
                distinct_skus([complete_row(1, "商品B", "6900000000002")]))
            state_path = self.make_state(root, {"1": first, "2": second})
            inventory = {"full_store_count": 2}
            segments = [
                {"order": 1, "top": "休闲零食", "leaf": "薯片薯条", "expected_count": 2},
                {"order": 2, "top": "精选业务", "leaf": "精选业务", "expected_count": 1},
            ]
            manifest_path, error = self.merge(
                root, inventory, segments, state_path, full_count=2)
            # 并集 2 行 == 实时全店计数 2 → 按数量对账通过（显式标注非同时点）。
            self.assertIsNone(error)
            report = C.validate_manifest(
                manifest_path, require_complete=False,
                expected_profile=C.load_profile("xiniu-changan"))
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.row_count, 2)
            scope = report.manifest["scope"]
            self.assertEqual(scope["raw_segment_product_count"], 3)
            self.assertEqual(scope["cross_category_duplicate_count"], 1)
            self.assertEqual(C.row_category_map(report.manifest, 2),
                             [("休闲零食", "薯片薯条"),
                              ("休闲零食", "薯片薯条")])

    def test_merge_preserves_duplicate_barcode_occurrences_within_leaf(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_segment(
                root, 1, "休闲零食", "薯片薯条",
                distinct_skus([complete_row(1, "规格A", "6900000000001"),
                               complete_row(2, "规格B", "6900000000001")]))
            state_path = self.make_state(root, {"1": manifest})
            output, _ = self.merge(
                root, {"full_store_count": 2},
                [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                  "expected_count": 2}], state_path, full_count=2)
            report = C.validate_manifest(output, require_complete=False,
                                         expected_profile=C.load_profile("xiniu-changan"))
            self.assertTrue(report.ok, report.errors)
            self.assertEqual([row["商品名称"] for row in report.rows], ["规格A", "规格B"])

    def test_merge_keeps_same_barcode_different_specs_across_leaves(self):
        with tempfile.TemporaryDirectory() as root:
            first_row = complete_row(1, "同条码规格A", "6900000000001")
            second_row = complete_row(1, "同条码规格B", "6900000000001")
            first_row["规格"] = "100g"
            second_row["规格"] = "200g"
            first = self.make_segment(root, 1, "食品", "规格A",
                                      distinct_skus([first_row]))
            second = self.make_segment(root, 2, "食品", "规格B",
                                       distinct_skus([second_row]))
            state_path = self.make_state(root, {"1": first, "2": second})
            output, _ = self.merge(
                root, {"full_store_count": 2},
                [{"order": 1, "top": "食品", "leaf": "规格A", "expected_count": 1},
                 {"order": 2, "top": "食品", "leaf": "规格B", "expected_count": 1}],
                state_path, full_count=2)
            report = C.validate_manifest(output, require_complete=False,
                                         expected_profile=C.load_profile("xiniu-changan"))
            self.assertTrue(report.ok, report.errors)
            self.assertEqual([row["规格"] for row in report.rows], ["100g", "200g"])

    def test_merge_collapses_blank_spec_twin_with_same_sku(self):
        with tempfile.TemporaryDirectory() as root:
            concrete = complete_row(1, "同一商品", "6900000000001")
            blank = complete_row(2, "同一商品", "6900000000001")
            concrete["规格"] = "100g"
            blank["规格"] = "无"
            blank["SKU"] = concrete["SKU"]
            manifest = self.make_segment(
                root, 1, "食品", "测试类目", [concrete, blank])
            state_path = self.make_state(root, {"1": manifest})
            output, _ = self.merge(
                root, {"full_store_count": 1},
                [{"order": 1, "top": "食品", "leaf": "测试类目",
                  "expected_count": 2}], state_path, full_count=1)
            report = C.validate_manifest(
                output, require_complete=False,
                expected_profile=C.load_profile("xiniu-changan"))
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.row_count, 1)
            self.assertEqual(report.rows[0]["规格"], "100g")

    def test_merge_collapses_different_blank_spec_renderings(self):
        first = complete_row(1, "同一商品", "6900000000001")
        second = complete_row(2, "同一商品", "6900000000001")
        first["规格"] = ""
        second["规格"] = "无"
        second["SKU"] = first["SKU"]
        self.assertEqual(
            category_pipeline._redundant_spec_row_indexes([first, second]), {0})

    def test_merge_keeps_blank_spec_when_sku_differs(self):
        with tempfile.TemporaryDirectory() as root:
            concrete = complete_row(1, "同一商品", "6900000000001")
            blank = complete_row(2, "同一商品", "6900000000001")
            concrete["规格"] = "100g"
            blank["规格"] = ""
            blank["SKU"] = "OTHER-SKU"
            manifest = self.make_segment(
                root, 1, "食品", "测试类目", [concrete, blank])
            state_path = self.make_state(root, {"1": manifest})
            output, _ = self.merge(
                root, {"full_store_count": 2},
                [{"order": 1, "top": "食品", "leaf": "测试类目",
                  "expected_count": 2}], state_path, full_count=2)
            report = C.validate_manifest(
                output, require_complete=False,
                expected_profile=C.load_profile("xiniu-changan"))
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.row_count, 2)


class FullStoreCoverageGateTests(unittest.TestCase):
    """聚合不能自证完整：实时全店计数与并集不等就拒绝完整入库。"""

    def build(self, root, rows=2):
        directory = os.path.join(root, "001")
        os.makedirs(directory)
        prefix = os.path.join(directory, "qnh_scan_segment")
        csv_path = prefix + ".csv"
        for index, row in enumerate(rows, 1):
            row["序号"] = str(index)
            row["证据前缀"] = prefix
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        profile = C.load_profile("xiniu-changan")
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()), run_id=str(uuid.uuid4()),
            state="success", profile=profile, device_id="R28M31LJX0E",
            scope={"list_title": "薯片薯条", "category": "休闲零食/薯片薯条",
                   "expected_product_count": len(rows), "requested_count": 0},
            capture={"method": "app_ui", "started_at": "2026-10-09T10:00:00",
                     "run_started_at": "2026-10-09T10:00:00",
                     "finished_at": "2026-10-09T10:01:00",
                     "collector_version": "test", "pace": "ultra",
                     "tall_screen": True, "verify_every": 1,
                     "checkpoint_every": 25, "record": False,
                     "dismiss_order_dialogs": False},
            artifacts={"csv_path": csv_path, "evidence_prefix": prefix,
                       "video_path": None, "csv_sha256": C.sha256_file(csv_path)},
            rows=rows)
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest)
        return manifest_path, prefix

    def make_state(self, root, manifest_path, order="1"):
        state_path = os.path.join(root, "capture.state.json")
        C.atomic_write_json(state_path, {
            "capture_id": str(uuid.uuid4()), "batch_id": str(uuid.uuid4()),
            "dismiss_order_dialogs": False,
            "completed": {order: {"manifest_path": manifest_path,
                                  "manifest_sha256": C.sha256_file(manifest_path),
                                  "expected_count": 2}},
        })
        return state_path

    def merge(self, root, inventory, segments, state_path, *, full_count):
        """聚合。完整门禁不通过时 merge_segments 抛错，但 manifest 已落盘。

        返回 (manifest 路径, 错误信息或 None)。
        """
        try:
            path = category_pipeline.merge_segments(
                root, inventory, segments, state_path, full_count=full_count)
            return path, None
        except C.CaptureContractError as exc:
            paths = [os.path.join(root, name) for name in os.listdir(root)
                     if name.endswith(".manifest.json")
                     and "qnh_category_full" in name]
            self.assertTrue(paths, "聚合未落盘 manifest")
            return paths[0], str(exc)

    def test_merge_requires_explicit_realtime_full_store_count(self):
        with tempfile.TemporaryDirectory() as root:
            rows = distinct_skus([complete_row(1, "商品A", "6900000000001"),
                                  complete_row(2, "商品B", "6900000000002")])
            manifest_path, _ = self.build(root, rows)
            state_path = self.make_state(root, manifest_path)
            with self.assertRaises(C.CaptureContractError) as ctx:
                category_pipeline.merge_segments(
                    root, {"full_store_count": 999},
                    [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                      "expected_count": 2}], state_path)
            self.assertIn("必须显式提供实时全店计数", str(ctx.exception))

    def test_state_manifest_sha256_mismatch_blocks_merge(self):
        with tempfile.TemporaryDirectory() as root:
            rows = distinct_skus([complete_row(1, "商品A", "6900000000001"),
                                  complete_row(2, "商品B", "6900000000002")])
            manifest_path, _ = self.build(root, rows)
            state_path = self.make_state(root, manifest_path)
            state = C.read_json(state_path)
            state["completed"]["1"]["manifest_sha256"] = "0" * 64
            C.atomic_write_json(state_path, state)
            with self.assertRaises(C.CaptureContractError) as ctx:
                category_pipeline.merge_segments(
                    root, {"full_store_count": 2},
                    [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                      "expected_count": 2}], state_path, full_count=2)
            self.assertIn("与 state 记录不一致", str(ctx.exception))

    def test_state_order_set_mismatch_blocks_merge(self):
        with tempfile.TemporaryDirectory() as root:
            rows = distinct_skus([complete_row(1, "商品A", "6900000000001"),
                                  complete_row(2, "商品B", "6900000000002")])
            manifest_path, _ = self.build(root, rows)
            state_path = self.make_state(root, manifest_path)
            state = C.read_json(state_path)
            state["completed"]["7"] = state["completed"].pop("1")
            C.atomic_write_json(state_path, state)
            with self.assertRaises(C.CaptureContractError) as ctx:
                category_pipeline.merge_segments(
                    root, {"full_store_count": 2},
                    [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                      "expected_count": 2}], state_path, full_count=2)
            self.assertIn("completed 分片集合与分片清单不一致", str(ctx.exception))

    def test_complete_gate_rejects_unproven_full_store_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            rows = distinct_skus([complete_row(1, "商品A", "6900000000001"),
                                  complete_row(2, "商品B", "6900000000002")])
            manifest_path, _ = self.build(root, rows)
            state_path = self.make_state(root, manifest_path)
            with self.assertRaises(C.CaptureContractError) as ctx:
                category_pipeline.merge_segments(
                    root, {"full_store_count": 2},
                    [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                      "expected_count": 2}], state_path, full_count=99)
            self.assertIn("全店覆盖未证明", str(ctx.exception))
            # 并集 2 行 vs 实时全店 99：差额只说明无法证明，不推断缺口数量。
            self.assertNotIn("缺 97", str(ctx.exception))

    def test_complete_gate_accepts_count_reconciled_coverage(self):
        # 并集 2 行 == 实时全店计数 2 → 按数量对账通过；语义显式标注「非同时点」。
        with tempfile.TemporaryDirectory() as root:
            rows = distinct_skus([complete_row(1, "商品A", "6900000000001"),
                                  complete_row(2, "商品B", "6900000000002")])
            manifest_path, _ = self.build(root, rows)
            state_path = self.make_state(root, manifest_path)
            output, error = self.merge(
                root, {"full_store_count": 2},
                [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                  "expected_count": 2}], state_path, full_count=2)
            self.assertIsNone(error)
            report = C.validate_manifest(
                output, require_complete=True,
                expected_profile=C.load_profile("xiniu-changan"))
            self.assertTrue(report.ok, report.errors)
            scope = report.manifest["scope"]
            self.assertEqual(scope["coverage_mode"], C.FULL_STORE_COVERAGE_MODE)
            self.assertEqual(scope["full_store_coverage"]["status"], "verified")
            self.assertEqual(scope["realtime_count_difference"], 0)
            # 静态类目清单未回验的限制必须出现在报告里，不能默默当全店完整。
            self.assertTrue(any("未与 App 实时类目树回验" in w for w in report.warnings),
                            report.warnings)

    def test_gate_rejects_manifest_that_claims_verified_without_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            rows = distinct_skus([complete_row(1, "商品A", "6900000000001"),
                                  complete_row(2, "商品B", "6900000000002")])
            manifest_path, _ = self.build(root, rows)
            state_path = self.make_state(root, manifest_path)
            with self.assertRaises(C.CaptureContractError):
                category_pipeline.merge_segments(
                    root, {"full_store_count": 2},
                    [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                      "expected_count": 2}], state_path, full_count=99)
            merged = [os.path.join(root, name) for name in os.listdir(root)
                      if name.endswith(".manifest.json")
                      and "qnh_category_full" in name][0]
            manifest = C.read_json(merged)
            # 差额非 0 却声称 verified：口径事实与声明矛盾，必须被拒。
            manifest["scope"]["full_store_coverage"].update(
                {"status": "verified", "reason": ""})
            C.atomic_write_json(merged, manifest)
            report = C.validate_manifest(
                merged, require_complete=True,
                expected_profile=C.load_profile("xiniu-changan"))
            self.assertFalse(report.ok)
            self.assertTrue(any("与计数事实矛盾" in e for e in report.errors),
                            report.errors)


class SupervisorProgressTests(unittest.TestCase):
    """2026-10-12 审计修复回归：停滞判定按有效完成行数，不按 mtime。"""

    SEGMENT = {"order": 44, "top": "雪糕冰品", "leaf": "雪糕",
               "expected_count": 182}

    def write_csv(self, directory, name, statuses):
        path = os.path.join(directory, name)
        rows = []
        for index, status in enumerate(statuses, 1):
            row = complete_row(index, f"商品{index}", f"69000000000{index:02d}")
            row["状态"] = status
            if status == "未打开":
                for column in C.DETAIL_COLUMNS:
                    row[column] = ""
            row["证据前缀"] = os.path.splitext(path)[0]
            rows.append(row)
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_navigation_progress_ignores_csv_and_stale_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            started = time.time()
            heartbeat = os.path.join(directory, "qnh_scan.navigation")
            with open(heartbeat, "w") as fh:
                fh.write("100")
            os.utime(heartbeat, (started - 10, started - 10))
            self.write_csv(directory, "qnh_scan.csv", ["成功"])
            self.assertEqual(category_pipeline._navigation_progress(directory, started), started)
            os.utime(heartbeat, (started + 10, started + 10))
            self.assertEqual(category_pipeline._navigation_progress(directory, started), started + 10)

    def test_attempt_progress_counts_only_done_rows_after_start(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self.write_csv(directory, "qnh_scan_old.csv", ["成功"] * 3)
            started_after = os.path.getmtime(old)
            new = self.write_csv(directory, "qnh_scan_new.csv",
                                 ["成功", "未打开", "自建商品"])
            # 同秒 mtime 粒度太粗：把新 CSV 推到 started_after 之后，
            # 模拟"当前 attempt 的产物"。
            future = time.time() + 10
            os.utime(new, (future, future))
            self.assertEqual(
                category_pipeline._attempt_progress(directory, started_after),
                (future, 2))

    def make_segment(self, root, *, rows, name="qnh_scan_segment", state=None,
                     expected_count=None, stop_reason="", coverage=None):
        directory = os.path.join(root, "044")
        os.makedirs(directory, exist_ok=True)
        prefix = os.path.join(directory, name)
        csv_path = prefix + ".csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            if rows:
                for index, row in enumerate(rows, 1):
                    row["序号"] = str(index)
                    row["证据前缀"] = prefix
                writer.writerows(rows)
        profile = C.load_profile("xiniu-changan")
        segment = self.SEGMENT
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()), state=state or ("success" if rows else "failed"),
            profile=profile, device_id="R28M31LJX0E",
            scope={"list_title": segment["leaf"],
                   "category": f"{segment['top']}/{segment['leaf']}",
                   "expected_product_count": (expected_count if expected_count is not None
                                              else len(rows or [])),
                   "requested_count": 0},
            capture={"method": "app_ui", "started_at": "2026-10-09T10:00:00",
                     "run_started_at": "2026-10-09T10:00:00",
                     "finished_at": "2026-10-09T10:01:00",
                     "collector_version": "test", "pace": "ultra",
                     "tall_screen": True, "verify_every": 1,
                     "checkpoint_every": 25, "record": False,
                     "dismiss_order_dialogs": False},
            artifacts={"csv_path": csv_path, "evidence_prefix": prefix,
                       "video_path": None, "csv_sha256": C.sha256_file(csv_path)},
            rows=rows or [], stop_reason=stop_reason, coverage=coverage)
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest)
        return manifest_path

    def test_newest_resumable_segment_backtracks_past_failures(self):
        # 复现 shard44 断点丢失：最新 5 份全是无效工件，唯一有效断点排在
        # 第 6 旧。全量回溯必须仍能找到它；cap=5 的旧实现返回 None。
        with tempfile.TemporaryDirectory() as root:
            good = self.make_segment(
                root, rows=[complete_row(1, "商品A", "6900000000001")],
                name="qnh_scan_20261012-173614_f8b862e3")
            os.utime(good, (time.time() - 1000, time.time() - 1000))
            for index in range(6):
                self.make_segment(
                    root, rows=None,
                    name=f"qnh_scan_20261012-18{index:02d}00_{uuid.uuid4().hex[:8]}")
            directory = os.path.join(root, "044")
            found = category_pipeline.newest_resumable_segment(
                directory, self.SEGMENT)
            self.assertEqual(found, good)

    def test_rejects_legacy_lowered_count_even_with_count_refresh(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.make_segment(root, rows=[complete_row(1, "商品", "6900000000001")],
                                     stop_reason="平台标题计数高于连续完整到底可见上限；2 次稳定于 1")
            segment = {**self.SEGMENT, "expected_count": 3}
            report = category_pipeline.validate_segment(path, segment, allow_count_refresh=True)
            self.assertFalse(report.ok)
            self.assertEqual(segment["expected_count"], 3)
            self.assertIsNone(category_pipeline.newest_resumable_segment(
                os.path.dirname(path), segment))

    def test_segment_shortfall_with_coverage_proof_is_rejected(self):
        # 猫粮：平台标题计数 52，列表渲染 51 张。覆盖证明（set 口径）暂不被
        # 承认：短收一律失败；平台计数与实抓的差留在 manifest 里，不被改写。
        with tempfile.TemporaryDirectory() as root:
            path = self.make_segment(
                root, rows=[complete_row(1, "商品", "6900000000001")],
                expected_count=2,
                coverage={"list_exhausted": True, "end_marker": "已加载全部数据",
                          "rendered_cards": 1, "captured_cards": 1,
                          "cards_match": True, "unresolved_specs": [],
                          "platform_title_count": 2})
            segment = {**self.SEGMENT, "expected_count": 2}
            report = category_pipeline.validate_segment(path, segment, allow_count_refresh=True)
            self.assertFalse(report.ok)
            self.assertEqual(report.manifest["scope"]["expected_product_count"], 2)
            self.assertEqual(report.row_count, 1)

    def test_segment_without_coverage_proof_keeps_strict_count(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.make_segment(root, rows=[complete_row(1, "商品", "6900000000001")],
                                     expected_count=2)
            segment = {**self.SEGMENT, "expected_count": 2}
            report = category_pipeline.validate_segment(path, segment)
            self.assertFalse(report.ok)
            self.assertEqual(segment["expected_count"], 2)

    def test_segment_count_is_never_lowered_to_captured_rows(self):
        # 目标数绝不能被实抓行数改写（那会把真漏抓一起藏掉）：平台计数 5、
        # 实抓 1 行 → 失败，目标数仍是 5。
        with tempfile.TemporaryDirectory() as root:
            path = self.make_segment(root, rows=[complete_row(1, "商品", "6900000000001")],
                                     expected_count=5)
            segment = {**self.SEGMENT, "expected_count": 5}
            report = category_pipeline.validate_segment(path, segment, allow_count_refresh=True)
            self.assertFalse(report.ok)
            self.assertEqual(segment["expected_count"], 5)


class FakeProcess:
    """假装扫描器进程：立刻退出，不碰设备。"""

    returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class UntrustedArtifactTests(unittest.TestCase):
    """工件被换过（state 里的哈希对不上）：绝不当完成，也不当 resume 起点。"""

    SEGMENT = {"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
               "expected_count": 1}

    def make_manifest(self, directory, name, *, parent_sha=None):
        prefix = os.path.join(directory, name)
        csv_path = prefix + ".csv"
        rows = [complete_row(1, "商品A", "6900000000001")]
        for row in rows:
            row["证据前缀"] = prefix
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        profile = C.load_profile("xiniu-changan")
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()), state="success", profile=profile,
            device_id="R28M31LJX0E",
            scope={"list_title": self.SEGMENT["leaf"],
                   "category": f"{self.SEGMENT['top']}/{self.SEGMENT['leaf']}",
                   "expected_product_count": len(rows), "requested_count": 0},
            capture={"method": "app_ui", "started_at": "2026-10-09T10:00:00",
                     "run_started_at": "2026-10-09T10:00:00",
                     "finished_at": "2026-10-09T10:01:00",
                     "collector_version": "test", "pace": "ultra",
                     "tall_screen": True, "verify_every": 1,
                     "checkpoint_every": 25, "record": False,
                     "dismiss_order_dialogs": False},
            artifacts={"csv_path": csv_path, "evidence_prefix": prefix,
                       "video_path": None, "csv_sha256": C.sha256_file(csv_path)},
            rows=rows, parent_manifest_sha256=parent_sha)
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest)
        return manifest_path

    def test_force_fresh_without_new_manifest_never_returns_replaced_one(self):
        # 被替换的工件照样能通过门禁：force_fresh 必须拒绝它，并且本轮没产出
        # 新工件时整片判失败，绝不能把它"洗"成新哈希当完成。
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "001")
            os.makedirs(directory)
            replaced = self.make_manifest(directory, "qnh_scan_replaced")
            with mock.patch.object(category_pipeline.subprocess, "Popen",
                                   return_value=FakeProcess()), \
                 mock.patch.object(category_pipeline.time, "sleep"):
                with self.assertRaises(C.CaptureContractError) as ctx:
                    category_pipeline.run_segment(
                        root, dict(self.SEGMENT), dismiss_order_dialogs=False,
                        max_attempts=1, force_fresh=True,
                        excluded_manifests=[replaced])
            self.assertIn("没有本轮新产出的 manifest", str(ctx.exception))
            self.assertTrue(os.path.isfile(replaced), "旧工件必须保留")

    def test_force_fresh_completes_only_with_newly_produced_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "001")
            os.makedirs(directory)
            replaced = self.make_manifest(directory, "qnh_scan_replaced")
            produced = {}

            def fake_popen(command, **kwargs):
                produced["path"] = self.make_manifest(directory, "qnh_scan_fresh")
                return FakeProcess()

            with mock.patch.object(category_pipeline.subprocess, "Popen",
                                   side_effect=fake_popen), \
                 mock.patch.object(category_pipeline.time, "sleep"):
                manifest_path, _ = category_pipeline.run_segment(
                    root, dict(self.SEGMENT), dismiss_order_dialogs=False,
                    max_attempts=1, force_fresh=True,
                    excluded_manifests=[replaced])
            self.assertEqual(manifest_path, produced["path"])
            self.assertNotEqual(manifest_path, replaced)

    def test_untrusted_manifest_is_not_a_resume_source(self):
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "001")
            os.makedirs(directory)
            trusted = self.make_manifest(directory, "qnh_scan_trusted")
            untrusted = self.make_manifest(directory, "qnh_scan_untrusted")
            child = self.make_manifest(directory, "qnh_scan_child",
                                       parent_sha=C.sha256_file(untrusted))
            # 同目录其它可信工件照常可续采。
            found = category_pipeline.newest_resumable_segment(
                directory, dict(self.SEGMENT), exclude=[untrusted])
            self.assertEqual(found, trusted)
            # 从被篡改工件续采出来的子工件同样不能当断点。
            self.assertEqual(
                category_pipeline.newest_resumable_segment(
                    directory, dict(self.SEGMENT)), child)

    def test_untrusted_lineage_blocks_grandchild_resume(self):
        # 血缘要闭包：孙工件（从子工件续采）同样不能当断点，否则被替换的行
        # 会隔代继承下去洗白。
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "001")
            os.makedirs(directory)
            trusted = self.make_manifest(directory, "qnh_scan_trusted")
            untrusted = self.make_manifest(directory, "qnh_scan_untrusted")
            child = self.make_manifest(directory, "qnh_scan_child",
                                       parent_sha=C.sha256_file(untrusted))
            grandchild = self.make_manifest(directory, "qnh_scan_grandchild",
                                            parent_sha=C.sha256_file(child))
            found = category_pipeline.newest_resumable_segment(
                directory, dict(self.SEGMENT), exclude=[untrusted])
            self.assertEqual(found, trusted)
            self.assertNotIn(found, (child, grandchild))

    def test_run_segment_fast_path_rejects_manifests_inherited_from_untrusted(self):
        # 快路径（复用已有工件）也不能把子/孙工件当成已完成分片。
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "001")
            os.makedirs(directory)
            untrusted = self.make_manifest(directory, "qnh_scan_untrusted")
            child = self.make_manifest(directory, "qnh_scan_child",
                                       parent_sha=C.sha256_file(untrusted))
            grandchild = self.make_manifest(directory, "qnh_scan_grandchild",
                                            parent_sha=C.sha256_file(child))
            produced = {}

            def fake_popen(command, **kwargs):
                produced["path"] = self.make_manifest(directory, "qnh_scan_fresh")
                return FakeProcess()

            with mock.patch.object(category_pipeline.subprocess, "Popen",
                                   side_effect=fake_popen), \
                 mock.patch.object(category_pipeline.time, "sleep"):
                manifest_path, _ = category_pipeline.run_segment(
                    root, dict(self.SEGMENT), dismiss_order_dialogs=False,
                    max_attempts=1, excluded_manifests=[untrusted])
            self.assertEqual(manifest_path, produced["path"])
            self.assertNotIn(manifest_path, (untrusted, child, grandchild))

    def test_main_forces_fresh_capture_after_state_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "001")
            os.makedirs(directory)
            replaced = self.make_manifest(directory, "qnh_scan_replaced")
            fresh = self.make_manifest(directory, "qnh_scan_fresh")
            inventory_path = os.path.join(root, "segments.json")
            C.atomic_write_json(inventory_path, {"segments": [dict(self.SEGMENT)]})
            state_path = os.path.join(root, "capture.state.json")
            C.atomic_write_json(state_path, {
                "schema_version": 1, "capture_id": str(uuid.uuid4()),
                "batch_id": str(uuid.uuid4()), "dismiss_order_dialogs": False,
                "completed": {"1": {"manifest_path": replaced,
                                    "manifest_sha256": "0" * 64,
                                    "expected_count": 1}},
            })
            calls = {}

            def fake_run_segment(root_dir, segment, **kwargs):
                calls.update(kwargs)
                report = mock.Mock()
                report.expected_count = 1
                report.manifest_sha256 = C.sha256_file(fresh)
                report.row_count = 1
                return fresh, report

            argv = ["qnh_category_pipeline.py", "--segments-file", inventory_path,
                    "--out-dir", root]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(category_pipeline, "run_segment",
                                   side_effect=fake_run_segment), \
                 mock.patch.object(category_pipeline, "read_full_store_count",
                                   return_value=1), \
                 mock.patch.object(category_pipeline, "merge_segments",
                                   return_value="merged.manifest.json"), \
                 mock.patch.object(category_pipeline.subprocess, "run"):
                category_pipeline.main()
            # 哈希不符 → 必须重采新工件，旧工件既不当完成也不当 resume 起点。
            self.assertTrue(calls["force_fresh"])
            self.assertIn(os.path.abspath(replaced), calls["excluded_manifests"])
            state = C.read_json(state_path)
            self.assertEqual(state["completed"]["1"]["manifest_path"], fresh)
            self.assertEqual(state["untrusted_manifests"],
                             [os.path.abspath(replaced)])
            self.assertTrue(os.path.isfile(replaced), "旧工件必须保留留档")


if __name__ == "__main__":
    unittest.main()
