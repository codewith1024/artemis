"""Real-device fixture: one SPU with two SKUs must not collapse into one row.

真机 2026-10-21（qnh_acceptance/20261021/multispec/live.xml，高屏 1080x4414）：
SPU 2064972073654448174 同一 SPU 下两行 SKU——「粉色牡丹一束（5朵花）」
2064972073662615613 与「香槟色牡丹一束（一朵花）」2064972073664102474，
各 20克(g)/单品、采购价 ¥5；渠道表每个渠道两行（零售价 ￥14.5、库存 1）。
列表卡规格是聚合串「粉色牡丹一束（5朵花），香槟色牡丹一束（一朵花）」。

旧解析把左列规格取第一行、SKU/条码取最后一行、渠道价取最后一行，拼成一条
假的完整行（规格粉色 + SKU 香槟色）。一行 40 列契约装不下两个 SKU，必须
明确报错停住分片，不能挑第一个冒充完整。
"""

import unittest
from pathlib import Path

import qnh_capture_common as C
import qnh_product_scan as scan

MULTISPEC = Path(__file__).parents[1] / "fixtures/qnh_multispec_detail.xml"
SINGLE = Path(__file__).parents[1] / "fixtures/qnh_barcode_less_detail.xml"


class MultiSpecDetailTests(unittest.TestCase):
    def nodes(self, path):
        return scan.parse_nodes(path.read_text(encoding="utf-8"))

    def test_multi_sku_detail_raises_instead_of_flattening(self):
        nodes = self.nodes(MULTISPEC)
        self.assertTrue(scan.is_detail_page(nodes))
        with self.assertRaises(C.CaptureContractError) as ctx:
            scan.parse_detail(nodes)
        message = str(ctx.exception)
        self.assertIn(scan.MULTISPEC_ERROR, message)
        self.assertIn("2个SKU", message)
        # 报错要能让人决策：列出规格和 SKU 清单
        self.assertIn("粉色牡丹一束（5朵花）", message)
        self.assertIn("香槟色牡丹一束（一朵花）", message)
        self.assertIn("2064972073662615613", message)
        self.assertIn("2064972073664102474", message)

    def test_detail_identity_check_accepts_real_multi_sku_title(self):
        nodes = self.nodes(MULTISPEC)
        product = {"title": "含苞牡丹仿真花芍药室内家居摆件 1束 婚庆手捧花野餐冰箱客厅桌面装饰",
                   "barcode": "", "spec": "粉色牡丹一束（5朵花），香槟色牡丹一束（一朵花）"}
        self.assertTrue(scan.detail_matches_product(nodes, product))
        self.assertFalse(scan.detail_matches_product(nodes, dict(product, title="其他商品")))

    def test_single_sku_detail_still_parses(self):
        """单 SKU 详情不受影响：不能因为多规格保护把正常商品也拒了。"""
        nodes = self.nodes(SINGLE)
        detail = scan.parse_detail(nodes)
        self.assertEqual(detail["SKU"], "2112566687605661794")
        self.assertEqual(detail["SPU"], "2112566687602413616")
        self.assertEqual(detail["规格_身份"], "20寸 1个")

    def test_non_detail_dump_does_not_raise(self):
        """不是详情页时不按 SKU 计数拒单：普通 dump 不能误伤整轮采集。"""
        nodes = [n for n in self.nodes(MULTISPEC) if n["text"] != "商品详情"]
        self.assertFalse(scan.is_detail_page(nodes))
        scan.parse_detail(nodes)          # 不抛异常即通过


if __name__ == "__main__":
    unittest.main()
