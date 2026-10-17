"""Tests for profile/manifest/gate pieces of the 牵牛花 DB pipeline."""

import csv
import os
import tempfile
import unittest
import uuid
from datetime import datetime

import qnh_capture_common as C


def complete_row(index=1, name="商品A", barcode="6900000000001", prefix=""):
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": str(index), "商品名称": name, "条码": barcode,
        "店内码": "A-1", "货号": "SKU-1", "状态": "成功",
        "时刻": "10:00:00", "采集时间": "2026-10-09T10:00:00",
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


class ManifestGateTests(unittest.TestCase):
    def make_capture(self, directory, rows):
        prefix = os.path.join(directory, "qnh_scan_20261009-100000")
        csv_path = prefix + ".csv"
        for row in rows:
            row["证据前缀"] = prefix
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()), run_id=str(uuid.uuid4()),
            state="success",
            profile={"name": "test", "store_name": "测试店", "warehouse_id": "1",
                     "tenant_id": "2", "store_code": "qnh_test", "role": "competitor"},
            device_id="device", scope={"list_title": "全部商品", "category": None,
                                       "expected_product_count": len(rows), "requested_count": 0},
            capture={"method": "app_ui", "started_at": "2026-10-09T10:00:00",
                     "run_started_at": "2026-10-09T10:00:00",
                     "finished_at": "2026-10-09T10:01:00", "collector_version": "test",
                     "pace": "ultra", "tall_screen": True, "verify_every": 1,
                     "checkpoint_every": 50, "record": False},
            artifacts={"csv_path": csv_path, "evidence_prefix": prefix, "video_path": None,
                       "csv_sha256": C.sha256_file(csv_path)},
            rows=rows)
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest, overwrite=False)
        return csv_path, manifest_path

    def test_segmented_manifest_maps_rows_and_validates_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [complete_row(1, "商品A", "6900000000001"),
                    complete_row(2, "商品B", "6900000000002")]
            _, path = self.make_capture(directory, rows)
            manifest = C.read_json(path)
            manifest["scope"] = {
                "mode": C.SEGMENT_MODE,
                "list_title": "全部商品",
                "category": None,
                "expected_product_count": 2,
                "requested_count": 0,
                "raw_segment_product_count": 3,
                "cross_category_duplicate_count": 1,
                "segments": [
                    {"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                     "expected_count": 2, "captured_count": 2, "unique_count": 2,
                     "duplicate_count": 0, "state": "success", "row_start": 1,
                     "row_end": 2},
                    {"order": 2, "top": "精选2", "leaf": "精选2",
                     "expected_count": 1, "captured_count": 1, "unique_count": 0,
                     "duplicate_count": 1, "state": "success", "row_start": None,
                     "row_end": None},
                ],
            }
            C.atomic_write_json(path, manifest)
            report = C.validate_manifest(path, require_complete=True)
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(C.row_category_map(manifest, 2),
                             [("休闲零食", "薯片薯条"),
                              ("休闲零食", "薯片薯条")])

    def test_segmented_manifest_rejects_unfinished_leaf(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.make_capture(directory, [complete_row()])
            manifest = C.read_json(path)
            manifest["scope"].update({
                "mode": C.SEGMENT_MODE,
                "raw_segment_product_count": 0,
                "cross_category_duplicate_count": 0,
                "segments": [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                              "expected_count": 1, "captured_count": 0,
                              "unique_count": 0, "state": "partial",
                              "row_start": None, "row_end": None}],
            })
            C.atomic_write_json(path, manifest)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("segment 未完成" in error for error in report.errors))

    def test_complete_manifest_passes_with_offline_channels_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.make_capture(directory, [complete_row()])
            report = C.validate_manifest(path, require_complete=True)
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.detail_complete_count, 1)

    def test_missing_recommended_flag_is_preserved_as_unknown(self):
        row = complete_row()
        row["是否力荐"] = ""
        self.assertNotIn("是否力荐", C.row_detail_errors(row))
        facts = C.fact_fields_from_csv(row, "美团闪购", "美团闪购价", "/tmp/test.csv")
        self.assertIsNone(facts["is_recommended"])

    def test_invalid_nonempty_recommended_flag_is_rejected(self):
        row = complete_row()
        row["是否力荐"] = "未知文本"
        self.assertIn("是否力荐", C.row_detail_errors(row))

    def test_missing_standard_flag_is_preserved_as_unknown(self):
        row = complete_row()
        row["是否标品"] = ""
        self.assertNotIn("是否标品", C.row_detail_errors(row))
        facts = C.fact_fields_from_csv(row, "美团闪购", "美团闪购价", "/tmp/test.csv")
        self.assertIsNone(facts["is_standard"])

    def test_invalid_nonempty_standard_flag_is_rejected(self):
        row = complete_row()
        row["是否标品"] = "未知文本"
        self.assertIn("是否标品", C.row_detail_errors(row))

    def test_csv_hash_tamper_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path, path = self.make_capture(directory, [complete_row()])
            with open(csv_path, "a", encoding="utf-8") as fh:
                fh.write("tampered")
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("SHA-256" in error for error in report.errors))

    def test_count_and_bad_status_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            row = complete_row()
            row["状态"] = "详情不全"
            _, path = self.make_capture(directory, [row])
            manifest = C.read_json(path)
            manifest["scope"]["expected_product_count"] = 2
            C.atomic_write_json(path, manifest)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("商品数不完整" in error for error in report.errors))
            self.assertTrue(any("不完整状态" in error for error in report.errors))

    def test_duplicate_names_without_any_stable_id_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [complete_row(1, "同 名", ""), complete_row(2, "同   名", "")]
            for row in rows:
                row["SPU"] = ""
                row["SKU"] = ""
            _, path = self.make_capture(directory, rows)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("无条码/SKU/SPU同名" in error for error in report.errors))

    def test_duplicate_barcode_less_names_with_distinct_skus_are_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [complete_row(1, "同 名", ""), complete_row(2, "同   名", "")]
            rows[0]["SPU"], rows[0]["SKU"] = "spu-1", "sku-1"
            rows[1]["SPU"], rows[1]["SKU"] = "spu-2", "sku-2"
            _, path = self.make_capture(directory, rows)
            report = C.validate_manifest(path, require_complete=True)
            self.assertTrue(report.ok, report.errors)

    def test_invalid_numeric_on_shelf_fields_fail_without_card_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            row = complete_row()
            row["美团闪购零售价"] = "abc"
            row["美团闪购价"] = ""
            _, path = self.make_capture(directory, [row])
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("美团闪购详情" in error for error in report.errors))

    def test_on_shelf_channel_uses_card_price_and_stock_when_detail_row_missing(self):
        row = complete_row()
        row["饿了么零售价"] = ""
        row["饿了么库存"] = ""
        row["饿了么价"] = "5.50"
        row["库存"] = "5"
        self.assertTrue(C.channel_detail_complete(row, "饿了么"))
        facts = C.fact_fields_from_csv(row, "饿了么", "饿了么价", "/tmp/test.csv")
        self.assertEqual(str(facts["front_display_price_amount"]), "5.50")
        self.assertEqual(str(facts["stock_quantity"]), "5")

    def test_malformed_scope_returns_gate_failure_without_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.make_capture(directory, [complete_row()])
            manifest = C.read_json(path)
            manifest["scope"]["expected_product_count"] = "one"
            C.atomic_write_json(path, manifest)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("正整数" in error for error in report.errors))

    def test_nested_manifest_type_returns_gate_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.make_capture(directory, [complete_row()])
            manifest = C.read_json(path)
            manifest["profile"] = []
            C.atomic_write_json(path, manifest)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("profile 必须是对象" in error for error in report.errors))

    def test_invalid_capture_options_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.make_capture(directory, [complete_row()])
            manifest = C.read_json(path)
            manifest["capture"].update({
                "pace": ["bad"],
                "verify_every": "bad",
                "checkpoint_every": -1,
                "tall_screen": "yes",
                "record": "no",
            })
            C.atomic_write_json(path, manifest)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("capture.pace" in error for error in report.errors))
            self.assertTrue(any("capture.verify_every" in error for error in report.errors))

    def test_unhashable_state_returns_gate_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.make_capture(directory, [complete_row()])
            manifest = C.read_json(path)
            manifest["state"] = []
            C.atomic_write_json(path, manifest)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("state 无效" in error for error in report.errors))

    def test_profile_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.make_capture(directory, [complete_row()])
            expected = C.read_json(path)["profile"] | {"tenant_id": "wrong"}
            report = C.validate_manifest(
                path, require_complete=True, expected_profile=expected)
            self.assertFalse(report.ok)
            self.assertTrue(any("指定 profile 不一致" in error for error in report.errors))

    def test_exact_header_contract_rejects_unknown_column(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path, path = self.make_capture(directory, [complete_row()])
            with open(csv_path, encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS + ["未知列"])
                writer.writeheader()
                writer.writerow(rows[0] | {"未知列": "x"})
            manifest = C.read_json(path)
            manifest["artifacts"]["csv_sha256"] = C.sha256_file(csv_path)
            C.atomic_write_json(path, manifest)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("未知列" in error for error in report.errors))

    def test_row_time_must_be_inside_capture_window(self):
        with tempfile.TemporaryDirectory() as directory:
            row = complete_row()
            row["采集时间"] = "2026-10-09T09:59:59"
            _, path = self.make_capture(directory, [row])
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("时间范围" in error for error in report.errors))

    def test_blank_product_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.make_capture(directory, [complete_row(name="", barcode="")])
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("缺少商品名称" in error for error in report.errors))

    def test_immutable_json_refuses_different_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "final.json")
            C.atomic_write_json(path, {"a": 1}, overwrite=False)
            C.atomic_write_json(path, {"a": 1}, overwrite=False)
            with self.assertRaises(C.CaptureContractError):
                C.atomic_write_json(path, {"a": 2}, overwrite=False)


class ProfileTests(unittest.TestCase):
    def test_xiniu_profile(self):
        profile = C.load_profile("xiniu-changan")
        self.assertEqual(profile["warehouse_id"], "1137294")
        self.assertEqual(profile["tenant_id"], "1003684")
        self.assertEqual(profile["store_code"], "qnh_1137294")
        self.assertEqual(profile["role"], "competitor")
    def test_profile_role_type_error_is_reported_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "profiles.json")
            C.atomic_write_json(path, {
                "schema_version": 1,
                "profiles": {
                    "bad": {
                        "store_name": "店",
                        "warehouse_id": "1",
                        "tenant_id": "2",
                        "store_code": "qnh_bad",
                        "role": [],
                    }
                },
            })
            with self.assertRaises(C.CaptureContractError):
                C.load_profile("bad", path)


if __name__ == "__main__":
    unittest.main()
