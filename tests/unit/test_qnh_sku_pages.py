"""跨屏详情页拼接：多屏 dump → 10 个 SKU 各自完整一行。

真机 2026-10-21（tests/fixtures/qnh_longsku_page_0{0,1,2}.xml，等于
qnh_acceptance/20261021/longsku/detail_0{0,1,2}.xml）：10 种面值筹码
（20/2/500/100/1/1000/10/5/200/50），SPU 1784849278276173862。

三屏是同一个详情页滚动下来的 dump：坐标以屏幕为原点（每屏整体平移），吸顶
导航「商品详情」每屏都在同一位置，滚出视口的行被 RN 把 y1 钉在视口顶
（bounds 反了）。所以不能直接拼节点取 y——这里按「唯一文本锚点算滚动距离 →
平移到内容坐标 → 按 (文本, x, 内容 y) 去重」拼成一份整页，再复用
parse_sku_details 的单页解析。

这里验的是 qnh_sku_capture.parse_sku_pages：10 个 SKU 各一行、三渠道价格库存
齐全；重叠屏去重、重复末屏偏移 0；跨屏价格打架、缺渠道行、规格集合不符，
一律抛 C.CaptureContractError，不挑一个冒充完整。
"""

import unittest
from pathlib import Path

import qnh_capture_common as C
import qnh_product_scan as scan
import qnh_sku_capture as cap

FIXTURES = Path(__file__).parents[1] / "fixtures"
PAGES = [FIXTURES / f"qnh_longsku_page_{i:02d}.xml" for i in range(3)]

SPECS = ["20", "2", "500", "100", "1", "1000", "10", "5", "200", "50"]
SPU = "1784849278276173862"
CHANNELS = cap.CHANNELS


def _pages():
    return [scan.parse_nodes(p.read_text(encoding="utf-8")) for p in PAGES]


def _drop_band(nodes, y0, y1):
    """丢掉一屏里 y 落在 [y0, y1) 的节点（造缺行用）。"""
    return [n for n in nodes if not (y0 <= n["y1"] < y1)]


def _retype(nodes, text, y0, y1, new):
    """把一屏里某个 y 段上的某段文字改掉（造跨屏冲突用）。"""
    out = []
    for n in nodes:
        n = dict(n)
        if y0 <= n["y1"] < y1 and n["text"] == text:
            n["text"] = new
        out.append(n)
    return out


class MultiScreenSkuParseTests(unittest.TestCase):
    def test_fixed_toolbar_does_not_pollute_scroll_offset(self):
        pages = _pages()
        for page in pages:
            page.append({"text": "固定悬浮工具栏", "x1": 0, "x2": 200,
                         "y1": 100, "y2": 140, "cx": 100, "cy": 120,
                         "w": 200, "h": 40})
        self.assertEqual(cap.parse_sku_pages(pages), cap.parse_sku_pages(_pages()))

    def test_real_three_pages_yield_ten_skus(self):
        rows = cap.parse_sku_pages(_pages())
        self.assertEqual(len(rows), 10)
        self.assertEqual([r["规格"] for r in rows], SPECS)
        for row in rows:
            self.assertEqual(row["SPU"], SPU)
            self.assertEqual(row["开放平台标识"], SPU)
            self.assertEqual(row["重量"], "0.5克(g)")
            self.assertEqual(row["采购单位_详情"], "单品")
            self.assertTrue(row["采购价_详情"])
            for channel in CHANNELS:
                self.assertEqual(row[channel + "状态"], "已上架")
                self.assertTrue(row[channel + "零售价"], row["规格"])
                self.assertTrue(row[channel + "库存"], row["规格"])

    def test_each_sku_keeps_its_own_price_and_stock(self):
        """10 个面值的库存各不相同：不能串行（串了就是全店同一份库存）。"""
        rows = {r["规格"]: r for r in cap.parse_sku_pages(_pages())}
        stocks = {s: rows[s]["美团闪购库存"] for s in SPECS}
        self.assertEqual(stocks, {"20": "101", "2": "99", "500": "91",
                                  "100": "6", "1": "5", "1000": "71",
                                  "10": "180", "5": "122", "200": "13",
                                  "50": "102"})
        for spec in SPECS:
            for channel in CHANNELS:
                self.assertEqual(rows[spec][channel + "库存"], stocks[spec])

    def test_shared_fields_come_from_the_bottom_pages(self):
        """公共字段只在最后一屏（其他信息/商品卖点），跨屏也要拼上来。"""
        rows = cap.parse_sku_pages(_pages())
        for row in rows:
            self.assertEqual(row["礼袋"], "0个礼袋")
            self.assertEqual(row["可售日期"], "不限")
            self.assertEqual(row["是否力荐"], "否")
            self.assertEqual(row["是否标品"], "非标品")
            self.assertEqual(row["卖点"], "加厚磨砂 PVC高档材质")

    def test_single_page_is_incomplete_and_loud(self):
        """只抓一屏：京东到家那整块还没出现，必须报错，不能当半个结果放行。"""
        with self.assertRaises(C.CaptureContractError) as ctx:
            cap.parse_sku_pages(_pages()[:1])
        self.assertIn("整块缺失", str(ctx.exception))

    def test_repeated_last_page_is_ignored(self):
        """滚动到底会再抓一屏一样的内容：偏移 0，去重后结果不变。"""
        pages = _pages()
        once = cap.parse_sku_pages(pages)
        twice = cap.parse_sku_pages(pages + [pages[-1]])
        self.assertEqual(twice, once)

    def test_expected_specs_mismatch_is_rejected(self):
        """期望规格集合必须不多不少：少一屏就必须报错。"""
        with self.assertRaises(C.CaptureContractError) as ctx:
            cap.parse_sku_pages(_pages(), expected_specs=SPECS[:-1])
        self.assertIn("规格集合与目标不一致", str(ctx.exception))
        self.assertIn("50", str(ctx.exception))

    def test_expected_specs_extra_is_rejected(self):
        with self.assertRaises(C.CaptureContractError) as ctx:
            cap.parse_sku_pages(_pages(), expected_specs=SPECS + ["999"])
        self.assertIn("999", str(ctx.exception))

    def test_conflicting_price_across_screens_is_rejected(self):
        """同一 SKU 在两屏里零售价不同（采集中价格变了）：保守报错，不混。"""
        pages = _pages()
        pages[1] = _retype(pages[1], "￥0.31", 1330, 1390, "￥0.99")
        with self.assertRaises(C.CaptureContractError) as ctx:
            cap.parse_sku_pages(pages)
        self.assertIn("零售价", str(ctx.exception))

    def test_absent_channel_row_is_saved_as_unknown_with_other_rows_preserved(self):
        pages = _pages()
        pages[0] = _drop_band(pages[0], 3360, 3420)
        pages[1] = _drop_band(pages[1], 1330, 1390)
        pages[2] = _drop_band(pages[2], 5361, 5421)
        rows = cap.parse_sku_pages(pages)
        missing = [row for row in rows if row.get("美团闪购状态") == "未知"]
        self.assertEqual(len(rows), 10)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["美团闪购库存"], "")
        self.assertEqual(missing[0]["美团闪购零售价"], "")
        self.assertEqual(missing[0]["_channel_observations"]["美团闪购"]["observation"], "row_absent")

    def test_empty_pages_raise(self):
        with self.assertRaises(C.CaptureContractError):
            cap.parse_sku_pages([])

    def test_module_does_not_import_the_scanner(self):
        source = Path(cap.__file__).read_text(encoding="utf-8")
        self.assertNotIn("qnh_product_scan", source)
        self.assertNotIn("import scan", source)


if __name__ == "__main__":
    unittest.main()
