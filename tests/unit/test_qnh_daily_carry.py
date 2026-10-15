"""Tests for the daily carry-baseline (每日增量) semantics."""

import csv
import json
import os
import tempfile
import unittest
import uuid

import qnh_capture_common as C


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


class CarryStatusTests(unittest.TestCase):
    def test_carry_is_complete_status(self):
        self.assertIn("携带", C.COMPLETE_ROW_STATUSES)
        row = carry_row()
        self.assertEqual(C.row_detail_errors(row), [])
        self.assertNotIn("携带", C.LISTING_STATUS_MAP.values())

    def test_carry_row_passes_strict_gate(self):
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
            self.assertTrue(report.ok, report.errors)

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

    def test_identical_fingerprint_carries(self):
        import qnh_product_scan as scan
        card = self.make_card(["45", "50", "55", "3.5"])
        self.assertEqual(
            scan.compare_card_baseline(card, {"fingerprint": self.fingerprint(card),
                                              "detail": ["d"]}), ["d"])

    def test_price_change_triggers(self):
        import qnh_product_scan as scan
        base = self.make_card(["45", "50", "55", "3.5"])
        today = self.make_card(["44", "50", "55", "3.5"])
        self.assertIsNone(scan.compare_card_baseline(
            today, {"fingerprint": self.fingerprint(base), "detail": ["d"]}))

    def test_amount_format_difference_still_carries(self):
        import qnh_product_scan as scan
        base = self.make_card(["45", "50", "55", "3.5"])
        today = self.make_card(["45.00", "50.0", "55", "3.50"])
        self.assertIsNotNone(scan.compare_card_baseline(
            today, {"fingerprint": self.fingerprint(base), "detail": ["d"]}))

    def test_stock_and_month_change_do_not_trigger(self):
        # 二级变化：只改库存/月售，指纹只看一级字段 → 仍携带（今日值经卡片入 CSV）
        import qnh_product_scan as scan
        base = self.make_card(["45", "50", "55", "3.5"])
        today = dict(base, month_sales="999", stock="1")
        self.assertIsNotNone(scan.compare_card_baseline(
            today, {"fingerprint": self.fingerprint(base), "detail": ["d"]}))

    def test_sold_out_or_spec_or_promo_triggers(self):
        import qnh_product_scan as scan
        base = self.make_card(["45", "50", "55", "3.5"])
        for variant in (dict(base, sold_out=True), dict(base, spec="200g"),
                        dict(base, promo_item=True)):
            self.assertIsNone(scan.compare_card_baseline(
                variant, {"fingerprint": self.fingerprint(base), "detail": ["d"]}))


if __name__ == "__main__":
    unittest.main()
