"""Unit tests for the 牵牛花 scan → CSV → database pipeline helpers."""

import csv
import os
import tempfile
import unittest
from datetime import datetime

import qnh_load_db as loader
import qnh_product_scan as scan


def node(text, x1, y1, x2, y2):
    return {
        "text": text,
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "cx": (x1 + x2) // 2,
        "cy": (y1 + y2) // 2,
    }


class ProductIdentityTests(unittest.TestCase):
    def setUp(self):
        self.rows = scan.CTX.rows
        scan.CTX.rows = []

    def tearDown(self):
        scan.CTX.rows = self.rows

    def test_same_title_different_barcodes_are_distinct(self):
        a = {"title": "同名商品", "barcode": "1111111111111"}
        b = {"title": "同名商品", "barcode": "2222222222222"}
        self.assertFalse(scan.same_product(a, b))

    def test_same_barcode_is_same_even_if_title_changes(self):
        a = {"title": "旧标题", "barcode": "1111111111111"}
        b = {"title": "新标题", "barcode": "1111111111111"}
        self.assertTrue(scan.same_product(a, b))

    def test_title_fallback_for_barcode_less_product(self):
        a = {"title": "自建商品", "barcode": ""}
        b = {"title": "自建商品", "barcode": ""}
        self.assertTrue(scan.same_product(a, b))

    def test_done_marks_do_not_hide_same_title_new_barcode(self):
        scan.CTX.rows = [[1, "同名商品", "1111111111111", "", "", "成功", "00:01",
                          "10:00:00", "2026-10-08T10:00:00", "/tmp/qnh_scan_a"]]
        marks = scan.done_marks()
        self.assertFalse(scan.is_done({"title": "同名商品", "barcode": "2222222222222"}, marks))
        self.assertTrue(scan.is_done({"title": "改过标题", "barcode": "1111111111111"}, marks))


class CardParsingTests(unittest.TestCase):
    def setUp(self):
        self.cards = scan.CTX.cards
        self.resumed_cards = scan.CTX.resumed_cards
        scan.CTX.cards = {}
        scan.CTX.resumed_cards = {}

    def tearDown(self):
        scan.CTX.cards = self.cards
        scan.CTX.resumed_cards = self.resumed_cards

    def test_reads_four_price_rows_and_card_metadata(self):
        spec = node("规格：75g/袋", 418, 200, 700, 240)
        nodes = [
            spec,
            node("条码: 6975795362456", 418, 245, 800, 275),
            node("月售：1000+", 418, 280, 540, 310),
            node("库存：2", 580, 280, 700, 310),
            node("活动品", 428, 315, 520, 345),
            node("￥", 467, 350, 498, 375), node("11", 498, 345, 550, 380),
            node(".90", 550, 350, 600, 375),
            node("￥", 467, 390, 498, 415), node("8", 498, 385, 550, 420),
            node(".90", 550, 390, 600, 415),
            node("￥", 467, 430, 498, 455), node("8", 498, 425, 550, 460),
            node(".90", 550, 430, 600, 455),
            node("￥", 467, 470, 498, 495), node("5", 498, 465, 550, 500),
            node(".27", 550, 470, 600, 495), node("/袋", 600, 470, 650, 495),
            node("查看全部价格", 418, 510, 620, 540),
        ]
        card = scan.read_card(nodes, spec, 600)
        self.assertEqual(card["spec"], "75g/袋")
        self.assertEqual(card["month_sales"], "1000+")
        self.assertEqual(card["stock"], "2")
        self.assertFalse(card["sold_out"])
        self.assertTrue(card["promo_item"])
        self.assertEqual(card["prices"], ["11.90", "8.90", "8.90", "5.27/袋"])

    def test_truncated_card_is_not_recorded(self):
        spec = node("规格：无", 418, 200, 540, 240)
        self.assertIsNone(scan.read_card([spec, node("￥", 467, 300, 498, 330)], spec, 500))

    def test_wrong_price_row_count_keeps_raw_but_does_not_assign_channels(self):
        scan.CTX.cards["条码"] = {
            "spec": "无", "month_sales": "0", "stock": "", "sold_out": True,
            "promo_item": False, "prices": ["10.00", "9.00", "无数据"],
        }
        values = scan.card_cols("商品", "条码")
        result = dict(zip(scan.CARD_COLUMNS, values))
        self.assertEqual(result["售罄"], "是")
        self.assertEqual(result["美团闪购价"], "")
        self.assertEqual(result["价格原文"], "10.00 | 9.00 | 无数据")


class DetailParsingTests(unittest.TestCase):
    """详情页字段解析（夹具：真机抓到的详情页层级）。"""

    @classmethod
    def setUpClass(cls):
        import xml.etree.ElementTree as ET
        cls.xml_path = "/tmp/qnh_detail2.xml"
        import os
        if not os.path.exists(cls.xml_path):
            raise unittest.SkipTest("真机详情页夹具不在本机")

    def test_parses_detail_top_fields(self):
        import xml.etree.ElementTree as ET
        import qnh_product_scan as scan
        nodes = scan.parse_nodes(open(self.xml_path).read())
        detail = scan.parse_detail(nodes)
        self.assertEqual(detail["开放平台标识"], "2069757556936007751")
        self.assertEqual(detail["SPU"], "2069757556936007751")
        self.assertEqual(detail["SKU"], "2069757556948492378")
        self.assertEqual(detail["重量"], "75克(g)")
        self.assertEqual(detail["采购价_详情"], "5.27")
        self.assertEqual(detail["采购单位_详情"], "单品")

    def test_merges_channel_tables(self):
        import xml.etree.ElementTree as ET
        import qnh_product_scan as scan
        nodes = scan.parse_nodes(open(self.xml_path).read())
        detail = scan.parse_detail(nodes)
        self.assertEqual(detail["美团闪购状态"], "已上架")
        self.assertEqual(detail["美团闪购零售价"], "11.9")
        self.assertEqual(detail["美团闪购库存"], "2")
        self.assertEqual(detail["饿了么零售价"], "8.9")
        # 京东到家的表格在这份夹具里在屏幕外；解析器不为看不见的字段编值
        self.assertNotIn("京东到家零售价", detail)
        self.assertFalse(scan.channels_complete(detail))

    def test_sparse_dump_yields_nothing(self):
        import xml.etree.ElementTree as ET
        import qnh_product_scan as scan
        nodes = scan.parse_nodes(open("/tmp/qnh_detail6.xml").read())
        self.assertEqual(scan.parse_detail(nodes), {})

    def test_merge_keeps_earlier_fields(self):
        import qnh_product_scan as scan
        merged = scan.parse_detail([], {"SPU": "1", "美团闪购零售价": "2"})
        self.assertEqual(merged, {"SPU": "1", "美团闪购零售价": "2"})


class EvidenceAndLoaderHelperTests(unittest.TestCase):
    def test_capture_meta_contains_full_timestamp_and_prefix(self):
        old_dir, old_stamp = scan.CTX.out_dir, scan.CTX.run_stamp
        try:
            scan.CTX.out_dir = "/tmp"
            scan.CTX.run_stamp = "20261008-180000"
            meta = scan.capture_meta(datetime(2026, 10, 8, 18, 1, 2))
            self.assertEqual(meta, ["18:01:02", "2026-10-08T18:01:02",
                                    "/tmp/qnh_scan_20261008-180000"])
        finally:
            scan.CTX.out_dir, scan.CTX.run_stamp = old_dir, old_stamp

    def test_resume_meta_upgrades_legacy_csv(self):
        meta = scan.resume_meta("/tmp/qnh_scan_20260928-105253.csv", {"时刻": "10:14:44"})
        self.assertEqual(meta[0], "10:14:44")
        self.assertEqual(meta[1], "2026-09-28T10:14:44")
        self.assertEqual(meta[2], "/tmp/qnh_scan_20260928-105253")

    def test_loader_prefers_per_row_full_time_and_evidence(self):
        rows = [
            {"时刻": "23:59:59", "采集时间": "2026-10-08T23:59:59",
             "证据前缀": "/archive/qnh_scan_20261008-230000"},
            {"时刻": "00:00:05", "采集时间": "2026-10-09T00:00:05",
             "证据前缀": "/archive/qnh_scan_20261008-230000"},
        ]
        self.assertEqual(loader.row_times("/tmp/current.csv", rows), [
            datetime(2026, 10, 8, 23, 59, 59), datetime(2026, 10, 9, 0, 0, 5)])

    def test_loader_legacy_resume_uses_each_rows_evidence_date(self):
        rows = [
            {"时刻": "10:00:00", "证据前缀": "/archive/qnh_scan_20260928-090000"},
            {"时刻": "11:00:00", "证据前缀": "/archive/qnh_scan_20261008-100000"},
        ]
        times = loader.row_times("/tmp/qnh_scan_20261008-100000.csv", rows)
        self.assertEqual(times, [datetime(2026, 9, 28, 10), datetime(2026, 10, 8, 11)])

    def test_month_sales_plus_uses_lower_bound(self):
        self.assertEqual(loader.lower_bound_int("1000+"), 1000)
        self.assertIsNone(loader.lower_bound_int(""))

    def test_listing_key_prefers_barcode_and_survives_rename(self):
        # 不同的采集版本用各自独立的 seen；同一条码改名后键不变
        by_barcode = loader.listing_key("旧标题", "6945906100802", {})
        renamed = loader.listing_key("改了名", "6945906100802", {})
        self.assertEqual(by_barcode, renamed)

    def test_listing_key_falls_back_to_name_without_barcode(self):
        seen = {}
        first = loader.listing_key(" 同  名 ", "", seen)
        second = loader.listing_key("同 名", "", seen)
        self.assertNotEqual(first, second)
        # 有条码的商品不占用名称键的序号
        self.assertEqual(loader.listing_key("同名", "123", seen), loader.listing_key("同名", "123", {}))

    def test_listing_key_disambiguates_duplicate_names(self):
        seen = {}
        first = loader.listing_key("同名", "", seen)
        second = loader.listing_key("同名", "", seen)
        self.assertNotEqual(first, second)

    def test_read_csv_accepts_legacy_base_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "qnh_scan_20260928-100000.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=loader.BASE_COLUMNS)
                w.writeheader()
                w.writerow(dict.fromkeys(loader.BASE_COLUMNS, "x"))
            self.assertEqual(len(loader.read_csv(path)), 1)


if __name__ == "__main__":
    unittest.main()
