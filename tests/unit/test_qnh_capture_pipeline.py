"""Unit tests for the 牵牛花 scan → CSV → database pipeline helpers."""

import csv
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

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
        self.row_product_ids = scan.CTX.row_product_ids
        self.resume_missing_bindings = scan.CTX.resume_missing_bindings
        scan.CTX.rows = []
        scan.CTX.row_product_ids = {}
        scan.CTX.resume_missing_bindings = {}

    def tearDown(self):
        scan.CTX.rows = self.rows
        scan.CTX.row_product_ids = self.row_product_ids
        scan.CTX.resume_missing_bindings = self.resume_missing_bindings

    def test_detail_parser_ignores_list_barcode_ghost_node(self):
        nodes = [
            {**node("条码: 6900000000001", 264, 100, 700, 140), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 436, "h": 40},
            {**node("条码: 6900000000002", 418, 500, 800, 540), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 382, "h": 40},
        ]
        self.assertEqual(scan.parse_detail(nodes)["条码"], "6900000000001")

    def test_internal_barcode_requires_matching_detail_sku(self):
        nodes = [
            {**node("商品详情", 0, 0, 100, 20), "clickable": False, "pkg": scan.APP_PKG,
             "cls": "android.widget.TextView", "desc": "", "w": 100, "h": 20},
            {**node("SKU: 2100000000000000001", 264, 50, 800, 90), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 536, "h": 40},
        ]
        self.assertTrue(scan.detail_matches_product(
            nodes, {"title": "A", "barcode": "2100000000000000001"}))
        self.assertFalse(scan.detail_matches_product(
            nodes, {"title": "B", "barcode": "2100000000000000002"}))

    def test_detail_identity_rejects_stale_previous_standard_product(self):
        nodes = [
            {**node("商品详情", 0, 0, 100, 20), "clickable": False, "pkg": scan.APP_PKG,
             "cls": "android.widget.TextView", "desc": "", "w": 100, "h": 20},
            {**node("条码: 6900000000001", 0, 30, 300, 60), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 300, "h": 30},
        ]
        self.assertTrue(scan.detail_matches_product(
            nodes, {"title": "A", "barcode": "6900000000001"}))
        self.assertFalse(scan.detail_matches_product(
            nodes, {"title": "B", "barcode": "6900000000002"}))

    def test_detail_identity_normalizes_grouped_barcode(self):
        nodes = [
            {**node("商品详情", 0, 0, 100, 20), "clickable": False, "pkg": scan.APP_PKG,
             "cls": "android.widget.TextView", "desc": "", "w": 100, "h": 20},
            {**node("条码: 690 0000 0000-01", 0, 30, 300, 60), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 300, "h": 30},
        ]
        self.assertTrue(scan.detail_matches_product(
            nodes, {"title": "A", "barcode": "6900000000001"}))

    def test_same_title_different_barcodes_are_distinct(self):
        a = {"title": "同名商品", "barcode": "1111111111111"}
        b = {"title": "同名商品", "barcode": "2222222222222"}
        self.assertFalse(scan.same_product(a, b))

    def test_same_barcode_is_same_even_if_title_changes(self):
        a = {"title": "旧标题", "barcode": "1111111111111"}
        b = {"title": "新标题", "barcode": "1111111111111"}
        self.assertTrue(scan.same_product(a, b))

    def test_same_barcode_different_specs_are_distinct_variants(self):
        a = {"title": "商品", "barcode": "1111111111111", "spec": "100g"}
        b = {"title": "商品", "barcode": "1111111111111", "spec": "200g"}
        self.assertFalse(scan.same_product(a, b))

    def test_barcode_less_identity_ignores_spec_changes(self):
        a = {"title": "同一自建商品", "barcode": "", "spec": "S码"}
        b = {"title": "同一自建商品", "barcode": "", "spec": "M码"}
        self.assertTrue(scan.same_product(a, b))

    def test_title_fallback_for_barcode_less_product(self):
        a = {"title": "自建商品", "barcode": ""}
        b = {"title": "自建商品", "barcode": ""}
        self.assertTrue(scan.same_product(a, b))

    def test_done_marks_do_not_hide_same_title_new_barcode(self):
        scan.CTX.rows = [[1, "同名商品", "1111111111111", "", "", "成功", "00:01",
                          "10:00:00", "2026-10-08T10:00:00", "/tmp/qnh_scan_a"]]
        scan.CTX.row_product_ids = {1: ("1111111111111", "", "")}
        marks = scan.resume_marks()
        self.assertFalse(scan.is_done({"title": "同名商品", "barcode": "2222222222222"}, marks))
        self.assertTrue(scan.is_done({"title": "同名商品", "barcode": "1111111111111",
                                      "spec": "任意规格"}, marks))

    def test_is_done_matches_missing_spec_without_hiding_barcode_variant(self):
        # 截断卡片旧行缺规格：同标题同条码仍判 done；同条码不同标题/规格
        # 是合法变体，不能全局按条码吞掉。
        scan.CTX.rows = [[1, "认养一头牛 茉莉荔枝", "6977174081088", "", "", "成功",
                          "00:01", "10:00:00", "2026-10-12T10:00:00", "/tmp/qnh_scan_a"]]
        scan.CTX.row_product_ids = {1: ("6977174081088", "", "")}
        marks = scan.resume_marks()
        self.assertTrue(scan.is_done(
            {"title": "认养一头牛 茉莉荔枝", "barcode": "6977174081088",
             "spec": "65g/支"}, marks))
        self.assertFalse(scan.is_done(
            {"title": "认养一头牛 茉莉荔枝 5支装", "barcode": "6977174081088",
             "spec": "65g*5支"}, marks))
        # 同标题同条码的另一规格也不能被空规格兼容键一并吞掉；兼容键只
        # 绑定首次见到的一个当前 UI 身份。
        self.assertFalse(scan.is_done(
            {"title": "认养一头牛 茉莉荔枝", "barcode": "6977174081088",
             "spec": "65g*5支"}, marks))
        self.assertFalse(scan.is_done(
            {"title": "认养一头牛 茉莉荔枝", "barcode": "6977174081089",
             "spec": "65g/支"}, marks))
    def test_anchor_scroll_uses_exact_adb_distance(self):
        old_semantic = scan.HelperState.has_semantic_scroll
        scan.HelperState.has_semantic_scroll = True
        anchor = {"box": (400, 2500, 1000, 2600)}
        try:
            with mock.patch.object(scan, "helper_action") as helper, \
                    mock.patch.object(scan, "sh") as shell, \
                    mock.patch.object(scan.time, "sleep"):
                scan.scroll_list(1080, 4560, scan.PACING["ultra"], anchor=anchor)
            helper.assert_not_called()
            shell.assert_called_once_with("input swipe 702 3648 702 1909 4576")
        finally:
            scan.HelperState.has_semantic_scroll = old_semantic

    def test_open_detail_uses_adb_tap_not_helper_tap(self):
        prod = {"title": "商品A", "barcode": "6900000000001", "spec": "100g",
                "tap": (600, 500), "box": (400, 450, 900, 550)}
        list_nodes = [
            {**node("", 0, 0, 1080, 4560), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.FrameLayout", "desc": "", "w": 1080, "h": 4560},
            {**node("商品A", 400, 450, 900, 550), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 500, "h": 100},
            {**node("规格：100g", 400, 560, 700, 600), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 300, "h": 40},
            {**node("条码: 6900000000001", 400, 610, 800, 650), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 400, "h": 40},
        ]
        detail_nodes = [
            {**node("商品详情", 0, 0, 200, 40), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 200, "h": 40},
            {**node("条码: 6900000000001", 0, 50, 350, 90), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 350, "h": 40},
        ]
        with mock.patch.object(scan, "dump_xml", side_effect=["list", "detail", "detail",
                                                                 "detail"]), \
                mock.patch.object(scan, "parse_nodes", side_effect=[list_nodes, detail_nodes,
                                                                     detail_nodes, detail_nodes]), \
                mock.patch.object(scan, "screen_size", return_value=(1080, 4560)), \
                mock.patch.object(scan, "human_point", return_value=(600, 500)), \
                mock.patch.object(scan, "sh") as shell, \
                mock.patch.object(scan, "helper_find_any_text", return_value="商品详情"), \
                mock.patch.object(scan.time, "sleep"):
            self.assertTrue(scan.open_detail(prod, (0, 0), screen=(1080, 4560),
                                             pace=scan.PACING["ultra"]))
        shell.assert_called_once_with("input tap 600 500")

    def test_category_list_ready_rejects_title_before_flatlist_refresh(self):
        old = {("old", "", "")}
        new = {("new", "", "")}
        self.assertFalse(scan.category_list_ready(36, old, old, None))
        self.assertFalse(scan.category_list_ready(36, new, old, None))
        self.assertTrue(scan.category_list_ready(36, new, old, new))

    def test_category_refresh_allows_four_second_mrn_render(self):
        old_product = {"title": "旧商品", "barcode": "", "spec": ""}
        new_product = {"title": "新商品", "barcode": "", "spec": ""}
        before = {scan.product_identity(old_product)}
        with mock.patch.object(scan.time, "monotonic",
                               side_effect=[0.0, 0.0, 1.0, 4.0]), \
                mock.patch.object(scan.time, "sleep"), \
                mock.patch.object(scan, "dump_xml", side_effect=["a", "b", "c"]), \
                mock.patch.object(scan, "parse_nodes",
                                  side_effect=[[{"screen": 1}], [{"screen": 2}],
                                               [{"screen": 3}]]), \
                mock.patch.object(scan, "selected_list_count",
                                  side_effect=[None, 123, 123]), \
                mock.patch.object(scan, "detect_watermarks", return_value=set()), \
                mock.patch.object(scan, "find_products",
                                  side_effect=[[old_product], [new_product], [new_product]]):
            result = scan.wait_category_list_refresh("一次性用品", before, timeout=8.0)
        self.assertEqual(result, (123, [{"screen": 3}]))

    def test_selected_leaf_waits_for_all_products_before_switching_back(self):
        old_product = {"title": "旧叶子商品", "barcode": "", "spec": ""}
        all_product = {"title": "全部商品卡", "barcode": "", "spec": ""}
        target_nodes = [{"screen": "target"}]
        old_keys = {scan.product_identity(old_product)}
        all_keys = {scan.product_identity(all_product)}
        with mock.patch.object(scan, "dump_xml", side_effect=["initial", "reset"]), \
                mock.patch.object(scan, "parse_nodes", return_value=[{"screen": "old"}]), \
                mock.patch.object(scan, "detect_watermarks", return_value=set()), \
                mock.patch.object(scan, "find_products",
                                  side_effect=[[old_product], [all_product]]), \
                mock.patch.object(scan, "selected_list_count", return_value=123), \
                mock.patch.object(scan, "tap_text_xml", return_value=True), \
                mock.patch.object(scan, "find_click_target_xml", return_value=(1, 1, 2, 2)), \
                mock.patch.object(scan, "wait_category_list_refresh",
                                  side_effect=[(999, [{"screen": "all"}]),
                                               (123, target_nodes)]) as wait, \
                mock.patch.object(scan.time, "sleep"):
            result = scan.select_category_path("家居日用/节庆礼品")
        self.assertEqual(result, (123, target_nodes))
        self.assertEqual(wait.call_args_list, [
            mock.call("全部商品", old_keys, 8.0),
            mock.call("节庆礼品", all_keys, 8.0),
        ])

    def test_category_selection_guard_requires_leaf_count_title(self):
        nodes = [
            {**node("薯片薯条（169）", 230, 260, 1000, 310), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "",
             "w": 770, "h": 50},
        ]
        self.assertTrue(scan.category_selection_valid("休闲零食/薯片薯条", nodes))
        self.assertFalse(scan.category_selection_valid("酒水乳饮/茶饮料", nodes))

    def test_category_selection_guard_rejects_left_menu_count(self):
        nodes = [
            {**node("其他母婴用品（77）", 32, 600, 205, 660), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "",
             "w": 173, "h": 60},
            {**node("婴童尿裤（17）", 268, 293, 1080, 340), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "",
             "w": 812, "h": 47},
        ]
        self.assertFalse(scan.category_selection_valid(
            "母婴用品/其他母婴用品", nodes))
        self.assertTrue(scan.category_selection_valid("母婴用品/婴童尿裤", nodes))

    def test_product_detail_gate_uses_card_channel_fallback(self):
        product = {"title": "商品", "barcode": "", "spec": "100g"}
        old_cards = scan.CTX.cards
        scan.CTX.cards = {scan.product_identity(product): {
            "spec": "100g", "month_sales": "1", "stock": "5",
            "sold_out": False, "promo_item": False,
            "prices": ["5.50", "5.50", "5.50", "2.00/个"],
        }}
        detail = {
            "SPU": "1", "SKU": "2", "采购单位_详情": "单品",
            "美团闪购状态": "已上架", "美团闪购零售价": "5.5", "美团闪购库存": "5",
            "饿了么状态": "已上架",
            "京东到家状态": "已上架", "京东到家零售价": "5.5", "京东到家库存": "5",
            "可售日期": "不限", "礼袋": "0个礼袋",
        }
        try:
            self.assertEqual(scan.product_detail_errors(detail, product), [])
            scan.CTX.cards[scan.product_identity(product)]["prices"][1] = ""
            scan.CTX.cards[scan.product_identity(product)]["stock"] = ""
            self.assertIn("饿了么详情", scan.product_detail_errors(detail, product))
        finally:
            scan.CTX.cards = old_cards

    def test_list_child_with_product_like_nodes_is_not_valid_list(self):
        child_nodes = [
            {**node("修改价格", 400, 100, 700, 150), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 300, "h": 50},
            {**node("商品A", 400, 200, 1000, 260), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 600, "h": 60},
            {**node("规格: 100g", 400, 270, 700, 320), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 300, "h": 50},
        ]
        fake_products = [{"title": "商品A", "barcode": "", "spec": "100g"}]
        self.assertFalse(scan.valid_product_list(child_nodes, fake_products))

    def test_navigate_backs_from_child_before_accepting_fake_product(self):
        child_nodes = [
            {**node("修改价格", 400, 100, 700, 150), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 300, "h": 50},
        ]
        fake_products = [{"title": "商品A", "barcode": "", "spec": "100g"}]
        list_nodes = [{**node("商品B", 400, 100, 1000, 150), "clickable": False,
                       "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "",
                       "w": 600, "h": 50}]
        real_products = [{"title": "商品B", "barcode": "6900000000001", "spec": "100g"}]
        old = scan.HelperState.ready
        scan.HelperState.ready = True
        try:
            with mock.patch.object(scan, "dump_products",
                                   side_effect=[(child_nodes, fake_products),
                                                (list_nodes, real_products)]), \
                    mock.patch.object(scan, "helper_action", return_value=True) as action, \
                    mock.patch.object(scan.time, "sleep"):
                nodes, products = scan.navigate_to_product_list(max_steps=2)
            self.assertEqual(products, real_products)
            action.assert_called_once_with("global", action="back")
        finally:
            scan.HelperState.ready = old

    def test_operation_tab_fallback_uses_real_screen_ratio(self):
        self.assertEqual(scan.operation_tab_point((1080, 4560)), (500, 4400))
        self.assertEqual(scan.operation_tab_point((1080, 2280)), (500, 2200))

    def test_title_bar_back_target_uses_clickable_left_sibling(self):
        nodes = [
            {**node("", 21, 112, 126, 259), "clickable": True, "w": 105, "h": 147},
            {**node("退款订单", 445, 152, 637, 219), "clickable": False,
             "w": 192, "h": 67},
            {**node("待审批", 113, 395, 248, 458), "clickable": False,
             "w": 135, "h": 63},
        ]
        self.assertEqual(
            scan.title_bar_back_target(nodes, "退款订单"),
            (73, 185, (21, 112, 126, 259)),
        )

    def test_title_bar_back_target_falls_back_beside_heading(self):
        nodes = [
            {**node("", 0, 0, 1080, 2280), "clickable": False,
             "w": 1080, "h": 2280},
            {**node("退款订单", 445, 152, 637, 219), "clickable": False,
             "w": 192, "h": 67},
        ]
        self.assertEqual(
            scan.title_bar_back_target(nodes, "退款订单"),
            (64, 185, None),
        )

    def test_find_click_target_uses_clickable_parent(self):
        xml = """<?xml version='1.0'?><hierarchy><node text='' clickable='true' bounds='[10,20][210,220]'><node text='门店商品' clickable='false' bounds='[80,100][150,130]'/></node></hierarchy>"""
        self.assertEqual(scan.find_click_target_xml(xml, "门店商品"), (10, 20, 210, 220))

    def test_recover_child_empty_helper_match_does_not_back(self):
        old = (scan.HelperState.has_any_text_query, scan.HelperState.ready)
        scan.HelperState.has_any_text_query = True
        scan.HelperState.ready = True
        try:
            with mock.patch.object(scan, "helper_find_any_text", return_value=""), \
                    mock.patch.object(scan, "dump_xml") as dump, \
                    mock.patch.object(scan, "helper_action") as action:
                scan.recover_list_child_after_back()
            dump.assert_not_called()
            action.assert_not_called()
        finally:
            scan.HelperState.has_any_text_query, scan.HelperState.ready = old

    def test_recover_child_none_falls_back_to_fresh_list_without_back(self):
        list_nodes = [
            {**node("商品A", 400, 100, 900, 140), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 500, "h": 40},
        ]
        old = (scan.HelperState.has_any_text_query, scan.HelperState.ready)
        scan.HelperState.has_any_text_query = True
        scan.HelperState.ready = True
        try:
            with mock.patch.object(scan, "helper_find_any_text", return_value=None), \
                    mock.patch.object(scan, "dump_xml", return_value="fresh"), \
                    mock.patch.object(scan, "parse_nodes", return_value=list_nodes), \
                    mock.patch.object(scan, "helper_action") as action:
                scan.recover_list_child_after_back()
            action.assert_not_called()
        finally:
            scan.HelperState.has_any_text_query, scan.HelperState.ready = old

    def test_recover_child_confirmed_fresh_page_backs_once(self):
        child_nodes = [
            {**node("零售价趋势", 10, 10, 300, 60), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 290, "h": 50},
        ]
        old = (scan.HelperState.has_any_text_query, scan.HelperState.ready)
        scan.HelperState.has_any_text_query = True
        scan.HelperState.ready = True
        try:
            with mock.patch.object(scan, "helper_find_any_text", return_value="零售价趋势"), \
                    mock.patch.object(scan, "dump_xml", return_value="fresh"), \
                    mock.patch.object(scan, "parse_nodes", return_value=child_nodes), \
                    mock.patch.object(scan, "helper_action", return_value=True) as action, \
                    mock.patch.object(scan.time, "sleep"):
                scan.recover_list_child_after_back()
            action.assert_called_once_with("global", action="back")
        finally:
            scan.HelperState.has_any_text_query, scan.HelperState.ready = old

    def test_stale_detail_text_does_not_trigger_double_back(self):
        list_nodes = [
            {**node("商品A", 418, 100, 980, 140), "clickable": False, "pkg": scan.APP_PKG,
             "cls": "android.widget.TextView", "desc": "", "w": 562, "h": 40},
            {**node("规格：100g", 418, 150, 700, 190), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 282, "h": 40},
            {**node("条码: 6900000000001", 418, 195, 800, 230), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 382, "h": 35},
            {**node("查看全部价格", 418, 300, 650, 335), "clickable": False,
             "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": "", "w": 232, "h": 35},
        ]
        with mock.patch.object(scan, "helper_find_any_text", return_value="商品详情"), \
                mock.patch.object(scan, "dump_xml", return_value="ignored"), \
                mock.patch.object(scan, "parse_nodes", return_value=list_nodes), \
                mock.patch.object(scan, "tap_back") as tap_back:
            fresh_nodes = scan.parse_nodes(scan.dump_xml())
            if scan.helper_find_any_text(["商品详情"]) == "商品详情":
                if scan.is_detail_page(fresh_nodes):
                    tap_back(None)
            tap_back.assert_not_called()

    def test_list_total_excludes_account_watermark(self):
        watermark = "kaixinCHEnxue（469721）"
        nodes = [
            node("分类（10）", 248, 30, 600, 42),
            node(watermark, 668, 28, 985, 39),
            node("全部商品（12050）", 248, 64, 1000, 74),
        ]
        self.assertEqual(
            scan.find_list_total(nodes, 1080, 4560, {watermark}),
            (64, 12050, "全部商品（12050）"),
        )
        self.assertEqual(
            scan.find_list_total(nodes, 1080, 4560, {watermark}, "分类"),
            (30, 10, "分类（10）"),
        )

    def test_resume_count_inherits_full_parent_range(self):
        self.assertEqual(scan.resolve_resume_count(None, 12026), 12026)
        self.assertEqual(scan.resolve_resume_count(0, 12026), 12026)
        with self.assertRaises(scan.C.CaptureContractError):
            scan.resolve_resume_count(20, 12026)

    def test_repeated_card_labels_are_not_mistaken_for_watermarks(self):
        nodes = ([{"text": "规格：无"}] * 8
                 + [{"text": "￥"}] * 20
                 + [{"text": "改价"}] * 8
                 + [{"text": "kaixinCHEnxue（469721）"}] * 4)
        self.assertEqual(scan.detect_watermarks(nodes), {"kaixinCHEnxue（469721）"})


class CardParsingTests(unittest.TestCase):
    def setUp(self):
        self.cards = scan.CTX.cards
        self.resumed_cards = scan.CTX.resumed_cards
        self.row_product_ids = scan.CTX.row_product_ids
        scan.CTX.cards = {}
        scan.CTX.resumed_cards = {}
        scan.CTX.row_product_ids = {}

    def tearDown(self):
        scan.CTX.cards = self.cards
        scan.CTX.resumed_cards = self.resumed_cards
        scan.CTX.row_product_ids = self.row_product_ids

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

    def test_repeated_barcode_specs_use_separate_card_cache_entries(self):
        barcode = "6975795362456"
        first = {"title": "商品", "barcode": barcode, "spec": "100g"}
        second = {"title": "商品", "barcode": barcode, "spec": "200g"}
        scan.remember_row_product(1, first)
        scan.remember_row_product(2, second)
        base = {"month_sales": "1", "stock": "2", "sold_out": False,
                "promo_item": False, "prices": ["10", "10", "10", "5/袋"]}
        scan.CTX.cards[scan.product_identity(first)] = base | {"spec": "100g"}
        scan.CTX.cards[scan.product_identity(second)] = base | {"spec": "200g"}
        self.assertEqual(scan.card_cols("商品", barcode, index=1)[0], "100g")
        self.assertEqual(scan.card_cols("商品", barcode, index=2)[0], "200g")

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
        with open(self.xml_path, encoding="utf-8") as fh:
            nodes = scan.parse_nodes(fh.read())
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
        with open(self.xml_path, encoding="utf-8") as fh:
            nodes = scan.parse_nodes(fh.read())
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
        with open("/tmp/qnh_detail6.xml", encoding="utf-8") as fh:
            nodes = scan.parse_nodes(fh.read())
        self.assertEqual(scan.parse_detail(nodes), {})

    def test_parses_other_information_rows(self):
        nodes = [
            node("礼袋绑定", 30, 100, 210, 140), node("0个礼袋", 300, 100, 500, 140),
            node("可售日期", 30, 160, 210, 200), node("不限", 300, 160, 500, 200),
            node("是否力荐", 30, 220, 210, 260), node("否", 300, 220, 500, 260),
            node("是否标品", 30, 280, 210, 320), node("标品", 300, 280, 500, 320),
        ]
        detail = scan.parse_detail(nodes)
        self.assertEqual(detail["礼袋"], "0个礼袋")
        self.assertEqual(detail["可售日期"], "不限")
        self.assertEqual(detail["是否力荐"], "否")
        self.assertEqual(detail["是否标品"], "标品")

    def test_merge_keeps_earlier_fields(self):
        import qnh_product_scan as scan
        merged = scan.parse_detail([], {"SPU": "1", "美团闪购零售价": "2"})
        self.assertEqual(merged, {"SPU": "1", "美团闪购零售价": "2"})

    def test_tall_detail_scrolls_until_long_page_other_info_appears(self):
        top = {"SPU": "1", "SKU": "2", "采购单位_详情": "单品",
               "美团闪购状态": "已下架", "饿了么状态": "已下架",
               "京东到家状态": "已下架"}
        middle = {"可售日期": "不限"}
        bottom = {"礼袋": "0个礼袋", "是否力荐": "否", "是否标品": "非标品"}
        product = {"title": "多规格商品", "barcode": "", "spec": "十个规格"}
        with mock.patch.object(scan, "parse_detail",
                               side_effect=[dict(top), {**top, **middle},
                                            {**top, **middle, **bottom}]), \
                mock.patch.object(scan, "find_target_field", return_value=(False, [])), \
                mock.patch.object(scan, "blocking_dialog", return_value=None), \
                mock.patch.object(scan.time, "time", side_effect=[0.0, 3.0]), \
                mock.patch.object(scan.time, "sleep"), \
                mock.patch.object(scan, "human_swipe") as swipe, \
                mock.patch.object(scan, "dump_nodes_undisturbed",
                                  side_effect=[[{"screen": "middle"}],
                                               [{"screen": "bottom"}]]):
            detail, _, _, _ = scan.collect_tall_detail(
                product, [{"screen": "top"}], 1080, 4560, timeout=2.8)
        self.assertEqual({k: detail[k] for k in scan.LOWER_DETAIL_COLUMNS},
                         {**middle, **bottom})
        self.assertEqual(detail["SPU"], "1")
        self.assertEqual(swipe.call_count, 2)


class EvidenceAndLoaderHelperTests(unittest.TestCase):
    def test_strict_resume_retries_status_success_rows_with_incomplete_details(self):
        saved = (scan.CTX.rows, scan.CTX.resumed, scan.CTX.resumed_cards,
                 scan.CTX.resumed_details, scan.CTX.row_product_ids)
        with tempfile.TemporaryDirectory() as directory:
            prefix = os.path.join(directory, "qnh_scan_20261009-100000")
            path = prefix + ".csv"
            row = {column: "" for column in scan.C.FULL_COLUMNS}
            row.update({
                "序号": "1", "商品名称": "缺详情商品", "条码": "6900000000001",
                "状态": "成功", "时刻": "10:00:00",
                "采集时间": "2026-10-09T10:00:00", "证据前缀": prefix,
                "开放平台标识": "1", "SPU": "1", "SKU": "1",
                "采购单位_详情": "袋", "礼袋": "0个礼袋",
                "是否力荐": "否", "是否标品": "标品",
                "美团闪购状态": "已上架", "美团闪购零售价": "1",
                "美团闪购库存": "1", "饿了么状态": "未上线",
                "京东到家状态": "已下架",
            })
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=scan.C.FULL_COLUMNS)
                writer.writeheader()
                writer.writerow(row)
            try:
                scan.CTX.rows = []
                scan.CTX.resumed = 0
                scan.CTX.resumed_cards = {}
                scan.CTX.resumed_details = {}
                scan.CTX.row_product_ids = {}
                scan.load_resume(path, strict_manifest=True)
                self.assertEqual(scan.CTX.rows, [])
                self.assertEqual(scan.CTX.resumed, 0)
            finally:
                (scan.CTX.rows, scan.CTX.resumed, scan.CTX.resumed_cards,
                 scan.CTX.resumed_details, scan.CTX.row_product_ids) = saved

    def test_unhandled_setup_failure_emits_failed_manifest_with_empty_csv(self):
        names = (
            "rows", "profile", "profiles_file", "serial", "out_dir", "run_stamp",
            "csv_path", "capture_id", "batch_id", "run_id", "parent_manifest_sha256",
            "manifest_output", "progress_output", "started_at", "run_started_at",
            "finished_at", "target_count", "list_title", "category", "stop_reason",
            "final_state", "capture_options", "cleaned", "a11y_disabled",
            "display_backup", "scrcpy_proc", "record_raw", "record_out",
        )
        saved = {name: getattr(scan.CTX, name) for name in names}
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = os.path.join(directory, "failure.manifest.json")
            now = datetime(2026, 10, 9, 10)
            try:
                scan.CTX.rows = []
                scan.CTX.profile = scan.C.load_profile("xiniu-changan")
                scan.CTX.profiles_file = scan.C.DEFAULT_PROFILE_FILE
                scan.CTX.serial = "R28M31LJX0E"
                scan.CTX.out_dir = directory
                scan.CTX.run_stamp = "failure"
                scan.CTX.csv_path = None
                scan.CTX.capture_id = "11111111-1111-4111-8111-111111111111"
                scan.CTX.batch_id = "22222222-2222-4222-8222-222222222222"
                scan.CTX.run_id = "33333333-3333-4333-8333-333333333333"
                scan.CTX.parent_manifest_sha256 = None
                scan.CTX.manifest_output = manifest_path
                scan.CTX.progress_output = None
                scan.CTX.started_at = now
                scan.CTX.run_started_at = now
                scan.CTX.finished_at = None
                scan.CTX.target_count = None
                scan.CTX.list_title = None
                scan.CTX.category = None
                scan.CTX.stop_reason = ""
                scan.CTX.final_state = None
                scan.CTX.capture_options = {"requested_count": 0}
                scan.CTX.cleaned = False
                scan.CTX.a11y_disabled = False
                scan.CTX.display_backup = None
                scan.CTX.scrcpy_proc = None
                scan.CTX.record_raw = None
                scan.CTX.record_out = None
                with mock.patch.object(scan.sys, "__excepthook__"):
                    error = RuntimeError("setup failed")
                    scan.on_unhandled_exception(RuntimeError, error, None)
                scan.cleanup()
                manifest = scan.C.read_json(manifest_path)
                self.assertEqual(manifest["state"], "failed")
                self.assertIn("RuntimeError", manifest["progress"]["stop_reason"])
                self.assertTrue(os.path.isfile(manifest["artifacts"]["csv_path"]))
            finally:
                for name, value in saved.items():
                    setattr(scan.CTX, name, value)

    def test_restore_a11y_clears_disabled_flag(self):
        old_flag = scan.CTX.a11y_disabled
        with tempfile.TemporaryDirectory() as directory:
            backup = os.path.join(directory, "a11y.txt")
            with open(backup, "w", encoding="utf-8") as fh:
                fh.write("com.artemis.helper/.ArtemisAccessibilityService")
            scan.CTX.a11y_disabled = True
            try:
                with mock.patch.object(scan, "A11Y_BACKUP", backup), \
                        mock.patch.object(scan, "sh") as shell:
                    scan.restore_a11y()
                self.assertFalse(scan.CTX.a11y_disabled)
                self.assertEqual(shell.call_count, 2)
            finally:
                scan.CTX.a11y_disabled = old_flag

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

    def test_exact_upc_matching_normalizes_barcode_whitespace(self):
        cursor = mock.Mock()
        cursor.fetchall.return_value = [
            {"upc": "6900000000001", "spu": "SPU", "sku": "SKU"}
        ]
        matches = loader.exact_upc_matches(cursor, [{"条码": " 6900000000001 "}])
        self.assertEqual(matches, {"6900000000001": ("SPU", "SKU")})
        params = cursor.execute.call_args.args[1]
        self.assertEqual(params, ["6900000000001"])

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

    def test_listing_key_falls_back_to_name_without_stable_source_id(self):
        seen = {}
        first = loader.listing_key(" 同  名 ", "", seen)
        second = loader.listing_key("同 名", "", seen)
        self.assertNotEqual(first, second)
        # 有条码的商品不占用名称键的序号
        self.assertEqual(loader.listing_key("同名", "123", seen), loader.listing_key("同名", "123", {}))

    def test_listing_key_uses_sku_then_spu_only_when_barcode_is_blank(self):
        sku_a = loader.listing_key("同名", "", {}, "sku-a", "spu-a")
        sku_b = loader.listing_key("同名", "", {}, "sku-b", "spu-a")
        self.assertNotEqual(sku_a, sku_b)
        self.assertEqual(
            loader.listing_key("旧名", "", {}, "sku-a", "spu-a"),
            loader.listing_key("新名", "", {}, "sku-a", "spu-b"))
        self.assertEqual(
            loader.listing_key("旧名", "", {}, "", "spu-a"),
            loader.listing_key("新名", "", {}, "", "spu-a"))
        # Existing barcode history remains barcode-first even when SKU changes.
        self.assertEqual(
            loader.listing_key("旧名", "6900000000001", {}, "sku-a", "spu-a"),
            loader.listing_key("新名", "6900000000001", {}, "sku-b", "spu-b"))

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
