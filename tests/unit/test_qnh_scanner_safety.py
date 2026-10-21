"""Offline checks for the verified list/detail path and rejected historical carry."""

import csv
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import qnh_capture_common as C
import qnh_daily_run as daily
import qnh_product_scan as scan


def node(text, x1=264, y1=100, x2=800, y2=140):
    return {"text": text, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
            "w": x2 - x1, "h": y2 - y1, "clickable": False,
            "pkg": scan.APP_PKG, "cls": "android.widget.TextView", "desc": ""}


class ScannerSafetyTests(unittest.TestCase):
    def test_verified_milk_tea_barcode_matches_detail(self):
        nodes = [node("商品详情", 0, 0, 100, 20),
                 node("条码: 6938888888837"),
                 node("SPU: 2112518307509739611", y1=150, y2=190),
                 node("SKU: 2112518307509891160", y1=200, y2=240)]
        self.assertTrue(scan.detail_matches_product(
            nodes, {"title": "香飘飘 原味奶茶", "barcode": "6938888888837"}))
        self.assertFalse(scan.detail_matches_product(
            nodes, {"title": "其他商品", "barcode": "6938888888838"}))

    def test_new_fingerprint_alone_does_not_identify_barcode_less_product(self):
        nodes = [node("商品详情", 0, 0, 100, 20), node("SKU: 1234567890123456789")]
        with mock.patch.object(scan.CTX, "last_detail_fingerprint", ("old", "old", "old")):
            self.assertFalse(scan.detail_matches_product(
                nodes, {"title": "三太子干脆面", "barcode": ""}))
        nodes.append(node("商品名称: 香美客苏打饼干", y1=250, y2=290))
        self.assertFalse(scan.detail_matches_product(
            nodes, {"title": "三太子干脆面", "barcode": ""}))

    def test_real_barcode_less_detail_uses_structural_title_and_spec(self):
        path = Path(__file__).parents[1] / "fixtures/qnh_barcode_less_detail.xml"
        nodes = scan.parse_nodes(path.read_text(encoding="utf-8"))
        product = {"title": "【可登机】20寸多功能行李箱旅行箱子 1个 带usb充电口拉杆箱无侧提挂钩",
                   "barcode": "", "spec": "20寸 1个"}
        self.assertTrue(scan.detail_matches_product(nodes, product))
        detail = scan.parse_detail(nodes)
        self.assertEqual(detail["SKU"], "2112566687605661794")
        self.assertEqual(detail["SPU"], "2112566687602413616")
        self.assertFalse(scan.detail_matches_product(nodes, dict(product, title="其他行李箱")))
        self.assertFalse(scan.detail_matches_product(nodes, dict(product, spec="24寸 1个")))
        for n in nodes:
            n["detail_title"] = False
        nodes.append(node(product["title"], x1=418, y1=200, x2=1000, y2=250))
        self.assertFalse(scan.detail_matches_product(nodes, product))

    def test_open_real_barcode_less_detail_succeeds_without_label(self):
        path = Path(__file__).parents[1] / "fixtures/qnh_barcode_less_detail.xml"
        detail_nodes = scan.parse_nodes(path.read_text(encoding="utf-8"))
        product = {"title": "【可登机】20寸多功能行李箱旅行箱子 1个 带usb充电口拉杆箱无侧提挂钩",
                   "barcode": "", "spec": "20寸 1个", "tap": (750, 490),
                   "box": (452, 407, 1049, 583)}
        with mock.patch.object(scan, "dump_xml", return_value="xml"), \
             mock.patch.object(scan, "parse_nodes", side_effect=[[], detail_nodes, detail_nodes]), \
             mock.patch.object(scan, "relocate", return_value=product), \
             mock.patch.object(scan, "screen_size", return_value=(1080, 2280)), \
             mock.patch.object(scan, "human_point", return_value=(750, 490)), \
             mock.patch.object(scan, "sh") as shell, \
             mock.patch.object(scan.HelperState, "has_any_text_query", False), \
             mock.patch.object(scan.CTX, "last_detail_nodes", []), \
             mock.patch.object(scan.time, "sleep"):
            self.assertTrue(scan.open_detail(product, (0, 0)))
            self.assertEqual(scan.parse_detail(scan.CTX.last_detail_nodes)["SKU"],
                             "2112566687605661794")
        shell.assert_called_once_with("input tap 750 490")

    def test_title_like_list_ghost_is_not_detail_identity(self):
        xml = ('<hierarchy><node text="商品详情" bounds="[0,0][100,20]"/>'
               '<node><node text="目标商品" bounds="[418,50][1000,100]"/></node>'
               '<node><node text="开放平台标识: 123" bounds="[32,200][1000,240]"/>'
               '<node text="SPU: 123" bounds="[32,250][500,290]"/></node>'
               '<node text="SKU: 456" bounds="[264,300][700,340]"/></hierarchy>')
        nodes = scan.parse_nodes(xml)
        self.assertFalse(any(n["detail_title"] for n in nodes))
        self.assertFalse(scan.detail_matches_product(
            nodes, {"title": "目标商品", "barcode": "", "spec": "1个"}))

    def test_channel_retail_price_is_never_guessed_as_purchase_price(self):
        nodes = [node("无数据", y1=100, y2=140),
                 node("渠道信息", y1=200, y2=240),
                 node("￥6.5", y1=300, y2=340)]
        self.assertEqual(scan.parse_detail(nodes)["采购价_详情"], "无数据")
        self.assertNotIn("采购价_详情", scan.parse_detail(nodes[2:]))

    def test_no_purchase_data_survives_detail_gate_and_fact_conversion(self):
        from tests.unit.test_qnh_manifest_pipeline import complete_row
        nodes = [node("无数据", y1=100, y2=140),
                 node("渠道信息", y1=200, y2=240),
                 node("￥6.5", y1=300, y2=340)]
        detail = complete_row()
        detail.update(scan.parse_detail(nodes))
        detail["采购价"] = "9.99/袋"
        self.assertEqual(C.row_detail_errors(detail), [])
        for channel, price_column in C.PRICE_CHANNELS:
            fields = C.fact_fields_from_csv(detail, channel, price_column, "/tmp/capture.csv")
            self.assertIsNone(fields["purchase_price_amount"])
            self.assertEqual(fields["purchase_price_text"], "无数据")
        detail["采购价_详情"] = "无法识别"
        self.assertIn("采购价_详情格式", C.row_detail_errors(detail))

    def test_purchase_price_requires_its_own_section(self):
        nodes = [node("￥5.27", y1=100, y2=140),
                 node("渠道信息", y1=200, y2=240),
                 node("￥6.5", y1=300, y2=340)]
        self.assertEqual(scan.parse_detail(nodes)["采购价_详情"], "5.27")

    def test_historical_carry_cannot_be_enabled(self):
        with self.assertRaisesRegex(C.CaptureContractError, "携带已停用"):
            scan.load_carry_baseline("/does/not/exist.json")
        self.assertIsNone(scan.compare_card_baseline({}, {"detail": ["old"]}))
        with self.assertRaisesRegex(C.CaptureContractError, "携带已停用"):
            scan._carry_row(1, {"title": "商品", "barcode": ""}, ["old"])
        self.assertNotIn("携带", scan.RESUME_DONE)

    def test_resume_revisits_historical_carry(self):
        row = {column: "" for column in C.FULL_COLUMNS}
        row.update({"序号": "1", "商品名称": "旧商品", "条码": "6938888888837",
                    "状态": "携带", "时刻": "00:00:00"})
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "qnh_scan_20261020-000000.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
                writer.writeheader()
                writer.writerow(row)
            with mock.patch.object(scan.CTX, "rows", []), \
                 mock.patch.object(scan.CTX, "resumed", 0), \
                 mock.patch.object(scan.CTX, "resume_missing_bindings", {}):
                scan.load_resume(path)
                self.assertEqual(scan.CTX.rows, [])
                self.assertEqual(scan.CTX.resumed, 0)


class DailyRunnerSafetyTests(unittest.TestCase):
    def test_new_run_never_exports_or_passes_baseline(self):
        commands = []
        with tempfile.TemporaryDirectory() as directory:
            completed = {"snapshot_id": "done", "committed": True,
                         "post_validation": {"ok": True}}
            with mock.patch.object(daily, "ROOT", directory), \
                 mock.patch.object(daily, "committed_result", side_effect=[None, ("done.json", completed)]), \
                 mock.patch.object(daily, "acquire_runner_lock", return_value=mock.Mock()), \
                 mock.patch.object(daily, "adb", return_value=mock.Mock(returncode=0, stdout="device")), \
                 mock.patch.object(daily, "run", side_effect=lambda command, *_args, **_kwargs: commands.append(command) or 0), \
                 mock.patch("sys.argv", ["qnh_daily_run.py", "--date", "20261022"]):
                daily.main()
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0][1].endswith("qnh_category_pipeline.py"))
        self.assertNotIn("--carry-baseline", commands[0])


if __name__ == "__main__":
    unittest.main()
