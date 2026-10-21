"""多 SKU 详情页离线解析：每个 SKU 独立一行，歧义/缺行/缺字段必须报错。

真机 2026-10-21（tests/fixtures/qnh_multispec_detail.xml，等于
qnh_acceptance/20261021/multispec/live.xml，高屏 1080x4414）：
SPU 2064972073654448174 下两个 SKU——「粉色牡丹一束（5朵花）」2064972073662615613
与「香槟色牡丹一束（一朵花）」2064972073664102474，各 20克(g)/单品、采购价 ¥5；
三个渠道各两行（零售价 ￥14.5、库存 1）。旧解析把左列规格取第一行、SKU/条码
取最后一行、渠道价取最后一行，拼出一条假行（规格粉色 + SKU 香槟色）。

这里验的是 qnh_sku_capture.parse_sku_details：按行归组，逐 SKU 出详情，歧义、
重复匹配、缺行、缺字段一律抛 C.CaptureContractError，不挑第一个冒充完整。
"""

import copy
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import qnh_capture_common as C
import qnh_product_scan as scan
import qnh_sku_capture as cap

MULTISPEC = Path(__file__).parents[1] / "fixtures/qnh_multispec_detail.xml"
SINGLE = Path(__file__).parents[1] / "fixtures/qnh_barcode_less_detail.xml"

PINK = "粉色牡丹一束（5朵花）"
CHAMPAGNE = "香槟色牡丹一束（一朵花）"
PINK_SKU = "2064972073662615613"
CHAMPAGNE_SKU = "2064972073664102474"
SPU = "2064972073654448174"

# 真机 fixture 里的节点坐标（用来精确定位要改的节点）
BUY_PINK_SPEC = "[45,1074][236,1119]"
BUY_PINK_PRICE = "[833,1117][1035,1162]"
BUY_CHAMPAGNE_SPEC = "[45,1241][236,1286]"
BUY_CHAMPAGNE_PRICE = "[833,1284][1035,1329]"
BUY_CHAMPAGNE_ROW = (
    "[45,1241][236,1286]", "[45,1286][236,1331]", "[258,1261][811,1306]",
    "[45,1331][247,1372]", "[258,1309][651,1350]", "[680,1311][734,1349]",
    "[833,1284][1035,1329]",
)
MT_PINK_PRICE = "[540,1710][628,1751]"
MT_PINK_STOCK = "[787,1708][1035,1753]"
MT_CHAMPAGNE_PRICE = "[540,1791][628,1832]"
MT_CHAMPAGNE_STOCK = "[787,1789][1035,1834]"
JDDJ_PINK_PRICE = "[540,2347][628,2388]"
JDDJ_PINK_STOCK = "[787,2345][1035,2390]"
JDDJ_CHAMPAGNE_ROW = (
    "[45,2426][293,2471]", "[292,2426][539,2471]",
    "[540,2428][628,2469]", "[787,2426][1035,2471]",
)
CHANNEL_ROWS = {
    "美团闪购": (("[45,1708][293,1753]", "[292,1708][539,1753]",
                  "[540,1710][628,1751]", "[787,1708][1035,1753]"),
                 ("[45,1789][293,1834]", "[292,1789][539,1834]",
                  "[540,1791][628,1832]", "[787,1789][1035,1834]")),
    "饿了么": (("[45,2026][293,2071]", "[292,2026][539,2071]",
                "[540,2028][628,2069]", "[787,2026][1035,2071]"),
               ("[45,2107][293,2152]", "[292,2107][539,2152]",
                "[540,2109][628,2150]", "[787,2107][1035,2152]")),
    "京东到家": (("[45,2345][293,2390]", "[292,2345][539,2390]",
                  "[540,2347][628,2388]", "[787,2345][1035,2390]"),
                 ("[45,2426][293,2471]", "[292,2426][539,2471]",
                  "[540,2428][628,2469]", "[787,2426][1035,2471]")),
}


def _parents(root):
    return {child: parent for parent in root.iter() for child in parent}


def _by_bounds(root, bounds):
    for el in root.iter("node"):
        if el.get("bounds") == bounds:
            return el
    raise AssertionError(f"fixture 里没有 bounds={bounds} 的节点")


def _text_by_bounds(root, bounds):
    return (_by_bounds(root, bounds).get("text") or "").strip()


def _set_text(root, bounds, text):
    _by_bounds(root, bounds).set("text", text)


def _drop(root, bounds_list):
    parents = _parents(root)
    for bounds in bounds_list:
        el = _by_bounds(root, bounds)
        parents[el].remove(el)


def _duplicate_row(root, bounds_list, dy):
    """把一行复制一份并整体下移 dy 像素（造重复行用）。"""
    parents = _parents(root)
    for bounds in bounds_list:
        el = _by_bounds(root, bounds)
        clone = copy.deepcopy(el)
        x1, y1, x2, y2 = (int(v) for v in _nums(clone.get("bounds")))
        clone.set("bounds", f"[{x1},{y1 + dy}][{x2},{y2 + dy}]")
        parents[el].append(clone)


def _nums(bounds):
    return [int(v) for v in bounds.replace("][", ",").strip("[]").split(",")]


def _text_node(text, bounds):
    return ET.Element("node", {
        "index": "0", "text": text, "resource-id": "",
        "class": "android.widget.TextView",
        "package": "com.sankuai.scsx.android.shuguopai",
        "content-desc": "", "checkable": "false", "checked": "false",
        "clickable": "false", "enabled": "true", "focusable": "false",
        "focused": "false", "scrollable": "false", "long-clickable": "false",
        "password": "false", "selected": "false", "bounds": bounds,
    })


def _add_hidden_row(root, texts, visible=False, y=2507):
    """往京东到家区块里塞一行；默认渲染成 [0,0][0,0]（屏外隐藏行）。"""
    spec, channel, price, stock = texts
    boxes = [f"[45,{y}][293,{y + 45}]", f"[292,{y}][539,{y + 45}]",
             f"[540,{y + 2}][628,{y + 43}]", f"[787,{y}][1035,{y + 45}]"]
    if not visible:
        boxes = ["[0,0][0,0]"] * 4
    parents = _parents(root)
    anchor = parents[_by_bounds(root, "[45,2426][293,2471]")]
    for text, box in zip(texts, boxes):
        anchor.append(_text_node(text, box))


def _mutated(path, fn):
    """改完 fixture 再解析成节点；变更只发生在内存里。"""
    root = ET.fromstring(Path(path).read_text(encoding="utf-8"))
    fn(root)
    return scan.parse_nodes(ET.tostring(root, encoding="unicode"))


class MultiSkuParseTests(unittest.TestCase):
    def test_real_fixture_yields_one_row_per_sku(self):
        nodes = scan.parse_nodes(MULTISPEC.read_text(encoding="utf-8"))
        rows = cap.parse_sku_details(nodes)
        self.assertEqual(len(rows), 2)
        pink, champagne = rows
        self.assertEqual(pink["规格"], PINK)
        self.assertEqual(pink["条码"], PINK_SKU)
        self.assertEqual(pink["SKU"], PINK_SKU)
        self.assertEqual(pink["重量"], "20克(g)")
        self.assertEqual(pink["采购价_详情"], "5")
        self.assertEqual(pink["采购单位_详情"], "单品")
        self.assertEqual(champagne["规格"], CHAMPAGNE)
        self.assertEqual(champagne["条码"], CHAMPAGNE_SKU)
        self.assertEqual(champagne["SKU"], CHAMPAGNE_SKU)
        for row in rows:
            self.assertEqual(row["SPU"], SPU)
            self.assertEqual(row["开放平台标识"], SPU)
            for channel in cap.CHANNELS:
                self.assertEqual(row[channel + "状态"], "已上架")
                self.assertEqual(row[channel + "零售价"], "14.5")
                self.assertEqual(row[channel + "库存"], "1")

    def test_rows_satisfy_the_main_thread_detail_gate(self):
        """主线程 merge LOWER 公共字段后，每行都要过 C.row_detail_errors。"""
        nodes = scan.parse_nodes(MULTISPEC.read_text(encoding="utf-8"))
        rows = cap.parse_sku_details(nodes)
        shared_nodes = [n for n in nodes
                        if not scan.SKU_PAT.match(n.get("text") or "")]
        shared = scan.parse_detail(shared_nodes)
        shared = {k: shared[k] for k in scan.LOWER_DETAIL_COLUMNS if k in shared}
        for row in rows:
            row.update(shared)
            self.assertEqual(C.row_detail_errors(row), [], row.get("SKU"))

    def test_each_sku_keeps_its_own_price_and_stock(self):
        """两个 SKU 改成不同采购价/渠道价/库存后必须各归各，不能串行。"""
        def mutate(root):
            _set_text(root, BUY_PINK_PRICE, "¥7")
            _set_text(root, BUY_CHAMPAGNE_PRICE, "¥9")
            _set_text(root, MT_PINK_PRICE, "￥20.50")
            _set_text(root, MT_PINK_STOCK, "2")
            _set_text(root, MT_CHAMPAGNE_PRICE, "￥30.00")
            _set_text(root, MT_CHAMPAGNE_STOCK, "5")
            _set_text(root, JDDJ_PINK_PRICE, "￥40.00")
            _set_text(root, JDDJ_PINK_STOCK, "7")
        rows = cap.parse_sku_details(_mutated(MULTISPEC, mutate))
        pink, champagne = rows
        self.assertEqual((pink["采购价_详情"], champagne["采购价_详情"]), ("7", "9"))
        self.assertEqual(pink["美团闪购零售价"], "20.50")
        self.assertEqual(pink["美团闪购库存"], "2")
        self.assertEqual(champagne["美团闪购零售价"], "30.00")
        self.assertEqual(champagne["美团闪购库存"], "5")
        self.assertEqual(pink["京东到家零售价"], "40.00")
        self.assertEqual(pink["京东到家库存"], "7")
        # 饿了么没改，仍然是 fixture 里的 14.5/1
        self.assertEqual(pink["饿了么零售价"], "14.5")
        self.assertEqual(champagne["饿了么库存"], "1")

    def test_same_spec_two_skus_is_ambiguous(self):
        """两个 SKU 同规格：渠道表按规格匹配，歧义必须报错而不是挑一个。"""
        def mutate(root):
            _set_text(root, BUY_CHAMPAGNE_SPEC, PINK)
        with self.assertRaises(C.CaptureContractError) as ctx:
            cap.parse_sku_details(_mutated(MULTISPEC, mutate))
        self.assertIn("同规格多 SKU 歧义", str(ctx.exception))
        self.assertIn(PINK, str(ctx.exception))
        self.assertIn(PINK_SKU, str(ctx.exception))
        self.assertIn(CHAMPAGNE_SKU, str(ctx.exception))

    def test_duplicate_channel_row_is_rejected(self):
        """同一渠道里同一规格出现两行：重复匹配必须报错。"""
        def mutate(root):
            _duplicate_row(root, JDDJ_CHAMPAGNE_ROW, 81)
        with self.assertRaises(C.CaptureContractError) as ctx:
            cap.parse_sku_details(_mutated(MULTISPEC, mutate))
        self.assertIn("重复匹配", str(ctx.exception))
        self.assertIn("京东到家", str(ctx.exception))
        self.assertIn(CHAMPAGNE, str(ctx.exception))

    def test_missing_channel_row_is_unknown_not_another_skus_value(self):
        def mutate(root):
            _drop(root, JDDJ_CHAMPAGNE_ROW)
        rows = cap.parse_sku_details(_mutated(MULTISPEC, mutate))
        missing = next(row for row in rows if row["SKU"] == CHAMPAGNE_SKU)
        self.assertEqual(missing["京东到家状态"], "未知")
        self.assertEqual(missing["京东到家零售价"], "")
        self.assertEqual(missing["京东到家库存"], "")
        self.assertEqual(missing["_channel_observations"]["京东到家"]["observation"], "row_absent")
        self.assertEqual(rows[0]["京东到家零售价"], "14.5")

    def test_missing_stock_is_rejected(self):
        """已上架的行缺库存：必须报错，不能留空混过去。"""
        def mutate(root):
            _drop(root, ["[787,2426][1035,2471]"])
        with self.assertRaises(C.CaptureContractError) as ctx:
            cap.parse_sku_details(_mutated(MULTISPEC, mutate))
        self.assertIn("缺零售价/库存", str(ctx.exception))

    def test_missing_channel_block_is_rejected(self):
        """整块渠道区缺失（页面还没渲染完）必须报错，不能当半个结果放行。"""
        def mutate(root):
            for bounds in JDDJ_CHAMPAGNE_ROW:
                _by_bounds(root, bounds)
            root_parents = _parents(root)
            for el in list(root.iter("node")):
                if (el.get("text") or "").strip() == "京东到家":
                    root_parents[el].remove(el)
        with self.assertRaises(C.CaptureContractError) as ctx:
            cap.parse_sku_details(_mutated(MULTISPEC, mutate))
        self.assertIn("京东到家", str(ctx.exception))
        self.assertIn("整块缺失", str(ctx.exception))

    def test_hidden_zero_bounds_row_is_ignored(self):
        """屏外隐藏行渲染成 [0,0][0,0]：不能混进渠道表（价格/库存不能被它改写）。"""
        def mutate(root):
            _add_hidden_row(root, (PINK, "京东到家", "￥99.00", "9"))
        rows = cap.parse_sku_details(_mutated(MULTISPEC, mutate))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["京东到家零售价"], "14.5")
            self.assertEqual(row["京东到家库存"], "1")

    def test_channel_row_with_unknown_spec_is_rejected(self):
        """渠道表出现采购表里没有的规格：页面与表格对不上，必须报错不猜。"""
        def mutate(root):
            _add_hidden_row(root, ("紫牡丹一束（3朵花）", "京东到家", "￥19.00", "3"),
                            visible=True)
        with self.assertRaises(C.CaptureContractError) as ctx:
            cap.parse_sku_details(_mutated(MULTISPEC, mutate))
        self.assertIn("紫牡丹一束（3朵花）", str(ctx.exception))

    def test_single_sku_page_yields_one_row(self):
        """只剩一个 SKU 的详情页：出一行，字段照旧齐全。"""
        def mutate(root):
            _drop(root, BUY_CHAMPAGNE_ROW)
            for channel in cap.CHANNELS:
                _drop(root, CHANNEL_ROWS[channel][1])
        rows = cap.parse_sku_details(_mutated(MULTISPEC, mutate))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["规格"], PINK)
        self.assertEqual(rows[0]["SKU"], PINK_SKU)
        self.assertEqual(rows[0]["采购价_详情"], "5")
        self.assertEqual(rows[0]["美团闪购零售价"], "14.5")
        self.assertEqual(rows[0]["京东到家库存"], "1")

    def test_single_sku_fixture_without_channel_block_raises(self):
        """单 SKU fixture（无渠道区）：明确报错，不返回半截结果。"""
        nodes = scan.parse_nodes(SINGLE.read_text(encoding="utf-8"))
        with self.assertRaises(C.CaptureContractError) as ctx:
            cap.parse_sku_details(nodes)
        self.assertIn("整块缺失", str(ctx.exception))

    def test_empty_nodes_raise(self):
        with self.assertRaises(C.CaptureContractError):
            cap.parse_sku_details([])

    def test_module_does_not_import_the_scanner(self):
        """循环依赖保护：模块不能 import 扫描器（nodes 由调用方给）。"""
        source = Path(cap.__file__).read_text(encoding="utf-8")
        self.assertNotIn("qnh_product_scan", source)
        self.assertNotIn("import scan", source)


if __name__ == "__main__":
    unittest.main()
