#!/usr/bin/env python3
"""离线解析商品详情页的多 SKU 表格：nodes → 每个 SKU 一条详情字典。

一个 SPU 下可以有多个 SKU（真机 2026-10-21 含苞牡丹仿真花：粉色牡丹一束（5朵花）
/ 香槟色牡丹一束（一朵花））。采购表和渠道表都按 SKU 分行，一行 40 列契约装不下
两个 SKU；单行解析会拼出一条假行（规格取第一行、条码/SKU/渠道价取最后一行）。

本模块只做纯本地文本+几何解析：不碰设备、不碰数据库、不 import 扫描器（避免
循环依赖）。输入是 scanner.parse_nodes 的产物，输出是每个 SKU 一条详情。

契约（歧义和缺字段一律抛 C.CaptureContractError，绝不挑第一个冒充完整）：
  * 采购表每行必须有且只有一个「SKU:」和「条码:」；缺或多 → 报错；
  * 两个 SKU 规格同名 → 歧义报错（渠道表按规格匹配，同名无法区分）；
  * 渠道表里同一个 SKU 匹配到多行 → 重复匹配报错；
  * 渠道状态为「已上架」却缺行 / 缺零售价 / 缺库存 → 报错（主线程可轮询重试）；
  * 渠道表出现采购表里没有的规格 → 报错（页面与表格对不上，不猜）；
  * 三个渠道必须都在页面上：缺一整块渠道表 → 报错（等主线程重抓）。

多屏（滚动分段）抓取用 parse_sku_pages：它把多屏 dump 拼成一份整页节点后
复用同一个单页解析器，跨屏的重叠/截断/冲突都在 stitch_pages 里处理。

ponytail: parse_sku_details 仍只处理「一屏内可见的完整详情页」；多屏拼接只
认「唯一文本锚点 + 滚动距离」，页面结构再变（表头换文案、列顺序变）要重写锚点。
"""

from __future__ import annotations

import re
import unicodedata

import qnh_capture_common as C

CHANNELS = ("美团闪购", "饿了么", "京东到家")

# 采购表三列表头（列锚点）
HEAD_LEFT = "规格/重量"
HEAD_MID = "商品条码/SKU"
HEAD_RIGHT = "采购价"

# 表格里混在值列的操作按钮
ROW_NOISE = frozenset({"复制", "复制全部", "修改", "编辑", "查看", "改价"})

# 同一视觉行的纵向容差（像素）
ROW_TOLERANCE = 25
# 同列相邻文本行算「同一块」的最大间距：行内紧贴（0~5px），行间留缝（36px+）。
# 取该列文本高度中位数的一半。
GAP_RATIO = 0.5
GAP_MIN = 12
# 左右两列配对的最大纵向偏差（相对行距）
PAIR_RATIO = 0.6

OPEN_PLATFORM_PAT = re.compile(r"^开放平台标识[:：]\s*(\S+)")
SPU_PAT = re.compile(r"^SPU[:：]\s*(\S+)")
SKU_PAT = re.compile(r"^SKU[:：]\s*(\S+)")
BARCODE_PAT = re.compile(r"^条码[:：]\s*(\S(?:.*\S)?)\s*$")
# 详情页身份表的条码列从 x=264 起；列表卡片的条码从 x=452 起（屏外幽灵节点）
BARCODE_MAX_X = 350
WEIGHT_PAT = re.compile(
    r"^\d+(?:\.\d+)?\s*(?:毫克|千克|吨|克|kg|KG|g|G|毫升|ml|ML|L|升|斤|两|"
    r"米|厘米|毫米|个|袋|包|盒|罐|瓶|片|条|支|桶|杯|双|组|提)"
    r"\s*[（(][^）)]*[）)]$")
PRICE_PAT = re.compile(r"^[￥¥](\d+(?:\.\d+)?)$")
STATUS_PAT = re.compile(r"^(已上架|已下架|未上线)\((.+)\)$")
CHANNEL_NAME_PAT = re.compile(r"^(美团闪购|饿了么|京东到家)$")
STOCK_PAT = re.compile(r"^\d+$")
SOLD_OUT_TEXTS = frozenset({"售罄", "已售罄"})

# 页面级公共字段（同一 SPU 下所有 SKU 相同）。键名与 scanner.LOWER_DETAIL_COLUMNS
# 对齐，外加「卖点」——页面上的标签是「商品卖点」，scanner.parse_detail 只认
# 「卖点：」那种写法，取不到它。
SHARED_LABEL_COLUMNS = {
    "礼袋绑定": "礼袋",
    "可售日期": "可售日期",
    "是否力荐": "是否力荐",
    "是否标品": "是否标品",
    "商品卖点": "卖点",
}
SHARED_VALUE_PATS = (
    (re.compile(r"^卖点[:：]\s*(\S.*)$"), "卖点"),
    (re.compile(r"^可售日期[:：]\s*(\S.*)$"), "可售日期"),
    (re.compile(r"^礼袋[:：]\s*(\S.*)$"), "礼袋"),
)


# --------------------------------------------------------------------------
# 节点清洗与几何工具
# --------------------------------------------------------------------------

def _clean_nodes(nodes):
    """去空文本、去水印文字、去 0 面积节点（RN 把屏外/隐藏元素渲染成 [0,0][0,0]）。"""
    out = []
    for n in nodes or []:
        text = (n.get("text") or "").strip()
        if not text or "kaixin" in text:
            continue
        x1, y1, x2, y2 = n["x1"], n["y1"], n["x2"], n["y2"]
        if x2 <= x1 or y2 <= y1:
            continue
        out.append({"text": text, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2})
    return out


def _one(nodes, text):
    """第一个文本完全等于 text 的节点。"""
    return next((n for n in nodes if n["text"] == text), None)


def _norm(text):
    """规格匹配键：全角/半角、空白差异不算不同规格。"""
    return unicodedata.normalize("NFKC", text or "").strip()


def _gap(items):
    """同列切块的最大行间距：文本高度中位数的一半。"""
    heights = sorted(n["y2"] - n["y1"] for n in items)
    median = heights[len(heights) // 2] if heights else 0
    return max(GAP_MIN, int(median * GAP_RATIO))


def _runs(items, gap):
    """把同列的文本按纵向切成若干块（每块 = 一行）。"""
    runs = []
    for n in sorted(items, key=lambda n: n["y1"]):
        if runs and n["y1"] - runs[-1]["y2"] <= gap:
            runs[-1]["y2"] = n["y2"]
            runs[-1]["nodes"].append(n)
        else:
            runs.append({"y1": n["y1"], "y2": n["y2"], "nodes": [n]})
    for run in runs:
        run["cy"] = (run["y1"] + run["y2"]) // 2
    return runs


def _pair(runs, others, tolerance):
    """runs 每块配 others 里纵向中心最近且在容差内的一块，一对一。"""
    pairs = {}
    used = set()
    for i, run in enumerate(runs):
        best = None
        for j, other in enumerate(others):
            if j in used:
                continue
            dist = abs(run["cy"] - other["cy"])
            if dist <= tolerance and (best is None or dist < best[0]):
                best = (dist, j)
        if best is not None:
            used.add(best[1])
            pairs[i] = best[1]
        else:
            pairs[i] = None
    return pairs


def _row_pitch(runs):
    """行距（相邻行中心的距离中位数）；只有一行时给个大兜底值。"""
    if len(runs) < 2:
        return 10 ** 6
    gaps = sorted(b["cy"] - a["cy"] for a, b in zip(runs, runs[1:]))
    return gaps[len(gaps) // 2]


# --------------------------------------------------------------------------
# 采购表：每个 SKU 一行
# --------------------------------------------------------------------------

def _purchase_rows(ns):
    """解析采购表，返回 [{规格, 条码, SKU, 重量, 采购价_详情, 采购单位_详情}]。"""
    head_mid = _one(ns, HEAD_MID)
    if head_mid is None:
        raise C.CaptureContractError(
            f"采购表表头「{HEAD_MID}」未渲染，无法切分 SKU 行")
    head_left = _one(ns, HEAD_LEFT)
    head_right = _one(ns, HEAD_RIGHT)
    top = max(h["y2"] for h in (head_left, head_mid, head_right) if h)
    bottom = next((n["y1"] for n in ns
                   if n["text"] in ("渠道信息", "其他信息")), None)
    body = [n for n in ns
            if n["y1"] > top and (bottom is None or n["y2"] < bottom)]

    mid_x = head_mid["x1"]
    right_x = head_right["x1"] if head_right else None
    left_col = [n for n in body if n["x1"] < mid_x and n["text"] not in ROW_NOISE]
    mid_col = [n for n in body
               if mid_x <= n["x1"] and (right_x is None or n["x1"] < right_x)
               and n["text"] not in ROW_NOISE]
    right_col = [n for n in body
                 if right_x is not None and n["x1"] >= right_x
                 and n["text"] not in ROW_NOISE]

    mid_runs = _runs(mid_col, _gap(mid_col))
    if not mid_runs:
        raise C.CaptureContractError("详情页采购表里没有 SKU 行")
    left_runs = _runs(left_col, _gap(left_col))
    right_runs = _runs(right_col, _gap(right_col))
    pitch = _row_pitch(mid_runs)

    left_of = _pair(mid_runs, left_runs, pitch * PAIR_RATIO)
    right_of = _pair(mid_runs, right_runs, pitch * PAIR_RATIO)
    if len(left_runs) != len(mid_runs):
        raise C.CaptureContractError(
            f"采购表规格列有 {len(left_runs)} 行、条码/SKU 列有 {len(mid_runs)} 行，"
            "行数对不上，无法按行归组")

    rows = []
    for i, run in enumerate(mid_runs):
        skus = [m.group(1) for m in (SKU_PAT.match(n["text"]) for n in run["nodes"])
                if m]
        barcodes = [m.group(1)
                    for n in run["nodes"] if n["x1"] < BARCODE_MAX_X
                    and (m := BARCODE_PAT.match(n["text"]))]
        if len(skus) != 1:
            raise C.CaptureContractError(
                f"采购表第 {i + 1} 行有 {len(skus)} 个「SKU:」标识，"
                "行结构不完整，拒绝按行解析")
        if len(barcodes) != 1:
            raise C.CaptureContractError(
                f"采购表第 {i + 1} 行有 {len(barcodes)} 个「条码:」标识，"
                "行结构不完整，拒绝按行解析")
        row = {"规格": "", "条码": barcodes[0], "SKU": skus[0]}
        j = left_of[i]
        if j is None:
            raise C.CaptureContractError(
                f"采购表第 {i + 1} 行（SKU {skus[0]}）没有对应的规格/重量/采购单位行")
        texts = [n["text"] for n in left_runs[j]["nodes"]]
        row["规格"] = texts[0]
        weights = [t for t in texts if WEIGHT_PAT.match(t)]
        if weights:
            row["重量"] = weights[0]
        last = texts[-1]
        if last != texts[0] and last != "-" and not WEIGHT_PAT.match(last):
            row["采购单位_详情"] = last
        k = right_of[i]
        if k is not None:
            prices = [m.group(1) for m in (PRICE_PAT.match(n["text"])
                                           for n in right_runs[k]["nodes"]) if m]
            if len(prices) > 1:
                raise C.CaptureContractError(
                    f"采购表第 {i + 1} 行有 {len(prices)} 个采购价，无法确定取哪个")
            if prices:
                row["采购价_详情"] = prices[0]
            elif "无数据" in [n["text"] for n in right_runs[k]["nodes"]]:
                row["采购价_详情"] = "无数据"
        rows.append(row)

    seen = {}
    for row in rows:
        key = _norm(row["规格"])
        if key in seen:
            raise C.CaptureContractError(
                f"同规格多 SKU 歧义：规格「{row['规格']}」同时对应 "
                f"SKU {seen[key]} 和 SKU {row['SKU']}，渠道表按规格匹配无法区分")
        seen[key] = row["SKU"]
    return rows


# --------------------------------------------------------------------------
# 渠道表：每个渠道一个区块，区块内每个 SKU 一行
# --------------------------------------------------------------------------

def _channel_sections(ns):
    """返回 {渠道: [(状态, 规格, 零售价, 库存)]}。"""
    headers = []
    for n in ns:
        m = STATUS_PAT.match(n["text"])
        if m:
            headers.append((n, m.group(1), m.group(2)))
    headers.sort(key=lambda h: h[0]["y1"])
    end = next((n["y1"] for n in ns if n["text"] == "其他信息"), None)
    sections = {}
    for i, (hnode, status, channel) in enumerate(headers):
        if channel not in CHANNELS:
            continue
        y1 = hnode["y1"]
        y2 = headers[i + 1][0]["y1"] if i + 1 < len(headers) else end
        rows = []
        for cn in (n for n in ns
                   if CHANNEL_NAME_PAT.match(n["text"]) and y1 <= n["cy"]
                   and (y2 is None or n["cy"] < y2)):
            row = [t for t in ns if abs(t["cy"] - cn["cy"]) <= ROW_TOLERANCE]
            spec = min((t for t in row if t["x1"] < cn["x1"]),
                       key=lambda t: t["x1"], default=None)
            if spec is None:
                raise C.CaptureContractError(
                    f"渠道「{channel}」有一行没有规格，无法归到 SKU")
            price_nodes = [t for t in row
                           if t["x1"] > cn["x1"] and PRICE_PAT.match(t["text"])]
            if len(price_nodes) > 1:
                raise C.CaptureContractError(
                    f"渠道「{channel}」规格「{spec['text']}」这一行有 "
                    f"{len(price_nodes)} 个零售价")
            price = PRICE_PAT.match(price_nodes[0]["text"]).group(1) if price_nodes else ""
            floor = price_nodes[0]["x1"] if price_nodes else cn["x1"]
            stocks = [t["text"] for t in row
                      if t["x1"] > floor
                      and (STOCK_PAT.match(t["text"]) or t["text"] in SOLD_OUT_TEXTS)]
            if len(stocks) > 1:
                raise C.CaptureContractError(
                    f"渠道「{channel}」规格「{spec['text']}」这一行有 "
                    f"{len(stocks)} 个库存值")
            rows.append((status, spec["text"], price, stocks[0] if stocks else ""))
        sections.setdefault(channel, []).extend(rows)
    return sections


def _channel_fields(sections, spec, *, allow_absent=False):
    """按规格绑定渠道行；完整表尾已观察到时，缺行记为未知，歧义仍报错。"""
    out = {}
    key = _norm(spec)
    for channel in CHANNELS:
        entries = sections.get(channel, [])
        matches = [e for e in entries if _norm(e[1]) == key]
        if len(matches) > 1:
            raise C.CaptureContractError(
                f"渠道「{channel}」重复匹配 SKU「{spec}」：{len(matches)} 行")
        if len(matches) == 1:
            status, _, price, stock = matches[0]
            if status == "已上架" and not (price and stock):
                raise C.CaptureContractError(
                    f"渠道「{channel}」SKU「{spec}」已上架但缺零售价/库存")
            out[channel + "状态"] = status
            out[channel + "零售价"] = price
            out[channel + "库存"] = stock
            continue
        statuses = {e[0] for e in entries}
        if not entries:
            raise C.CaptureContractError(
                f"渠道「{channel}」的渠道表整块缺失：三个渠道必须都在页面上，"
                "缺一个就无法确认这一行的上架价/库存")
        if len(statuses) > 1:
            raise C.CaptureContractError(
                f"渠道「{channel}」有多个不同状态（{'、'.join(sorted(statuses))}），"
                f"SKU「{spec}」没有对应行，无法确定状态")
        status = statuses.pop()
        if not allow_absent:
            raise C.CaptureContractError(
                f"渠道「{channel}」缺 SKU「{spec}」的行，尚无完整表尾证据")
        out.update({channel + "状态": "未知", channel + "零售价": "", channel + "库存": ""})
        out.setdefault("_channel_observations", {})[channel] = {
            "observation": "row_absent", "section_status": status,
            "reason": "已观察完整渠道表至其他信息，该 SKU 无对应行，不能推断规格级上架状态",
        }
    return out


# --------------------------------------------------------------------------
# 页面级公共字段
# --------------------------------------------------------------------------

def _identity(ns):
    """开放平台标识 / SPU：整页一个值。"""
    out = {}
    for pat, key in ((OPEN_PLATFORM_PAT, "开放平台标识"), (SPU_PAT, "SPU")):
        values = {m.group(1) for m in (pat.match(n["text"]) for n in ns) if m}
        if len(values) > 1:
            raise C.CaptureContractError(
                f"详情页出现多个{key}：{'、'.join(sorted(values))}")
        if values:
            out[key] = values.pop()
    return out


def _shared(ns):
    """可售日期 / 礼袋 / 是否力荐 / 是否标品 / 卖点：同 SPU 下所有 SKU 相同。"""
    out = {}
    for label, column in SHARED_LABEL_COLUMNS.items():
        node = _one(ns, label)
        if node is None:
            continue
        row = [t for t in ns
               if t is not node and t["x1"] > node["x1"]
               and abs(t["cy"] - node["cy"]) <= ROW_TOLERANCE]
        if row:
            out[column] = min(row, key=lambda t: t["x1"])["text"]
    for pat, column in SHARED_VALUE_PATS:
        for n in ns:
            m = pat.match(n["text"])
            if m:
                out.setdefault(column, m.group(1))
    return out


# --------------------------------------------------------------------------
# 多屏拼接：把多次 dump 拼成一份完整详情
# --------------------------------------------------------------------------
#
# 跨屏不能直接拼节点：滚动后每屏的 y 都以屏幕为原点，同一段内容在两屏里坐标
# 不同；页面边缘还会把行切开（滚出视口的行被 RN 把 y1 钉在视口顶，bounds 反了）。
# 做法：
#   * 每屏先丢掉「被裁掉」的节点（bounds 反了 / y1 钉在裁剪线上）：它们的内容
#     在别的屏里是完整的；
#   * 相邻两屏的滚动距离，用两屏都有的唯一文本锚点算（SKU 号、渠道段头、公共
#     字段标签；渠道名/价格这些重复文本不能当锚）；
#   * 每屏平移到内容坐标后，按（文本, x, 内容 y）去重；
#   * 每屏都在同一位置的吸顶导航不参与拼接；
#   * 末尾重复的一屏（滚动到底的证据）偏移为 0，去重后自然没有影响。

def _node_sig(node):
    return (node["text"], node["x1"], node["x2"], node["y1"], node["y2"])


def _page_clip_line(nodes):
    """滚出视口的行会被 RN 把 y1 钉在视口顶：这些反着的节点给出裁剪线。"""
    ys = {n["y1"] for n in nodes
          if n["x2"] > n["x1"] and n["y2"] <= n["y1"]}
    if not ys:
        return None
    if len(ys) > 1:
        raise C.CaptureContractError(f"这一屏的裁剪线不唯一：{sorted(ys)}")
    return ys.pop()


def _page_nodes(nodes):
    """一屏 dump → 干净节点（去掉被裁掉、零面积、水印的）。"""
    clip = _page_clip_line(nodes)
    ns = [n for n in _clean_nodes(nodes) if n["text"] != "商品详情"]
    if clip is None:
        return ns
    return [n for n in ns if n["y1"] != clip]


def _unique_ys(nodes):
    """只出现一次的文本 → 它的 y（当滚动锚点用；重复文本不能当锚）。"""
    counts = {}
    for n in nodes:
        counts[n["text"]] = counts.get(n["text"], 0) + 1
    out = {}
    for n in nodes:
        if counts[n["text"]] == 1:
            out[n["text"]] = n["y1"]
    return out


def _page_offset(prev, cur):
    """相邻两屏的滚动距离 = 两屏唯一文本锚点的 y 差中位数。"""
    if {(_node_sig(n)) for n in prev} == {(_node_sig(n)) for n in cur}:
        return 0
    a, b = _unique_ys(prev), _unique_ys(cur)
    deltas = sorted(a[t] - b[t] for t in a if t in b)
    if not deltas:
        raise C.CaptureContractError(
            "相邻两屏没有共同的唯一文本锚点，算不出滚动距离")
    mid = deltas[len(deltas) // 2]
    if max(deltas) - min(deltas) > 2:
        raise C.CaptureContractError(
            f"相邻两屏的滚动距离不一致（{min(deltas)}~{max(deltas)}），锚点不可信")
    return mid


def stitch_pages(pages):
    """多屏 dump → 一份内容坐标的节点列表（去重后的整页）。

    pages：按滑动顺序排的各屏 dump（scanner.parse_nodes 的产物）。
    """
    cleaned = [_page_nodes(p) for p in (pages or [])]
    cleaned = [p for p in cleaned if p]
    if not cleaned:
        return []
    sticky = set()
    if len(cleaned) > 1:
        sticky = set.intersection(*[{_node_sig(n) for n in p} for p in cleaned])
    cleaned = [[n for n in p if _node_sig(n) not in sticky] for p in cleaned]
    offsets = [0]
    for i in range(1, len(cleaned)):
        offsets.append(offsets[-1] + _page_offset(cleaned[i - 1], cleaned[i]))
    merged = {}
    for page, off in zip(cleaned, offsets):
        for n in page:
            if _node_sig(n) in sticky:
                continue
            node = dict(n, y1=n["y1"] + off, y2=n["y2"] + off)
            merged.setdefault((node["text"], node["x1"], node["x2"],
                               node["y1"], node["y2"]), node)
    return list(merged.values())


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def parse_sku_pages(pages, expected_specs=None):
    """多屏 dump → 每个 SKU 一条详情（字段与 parse_sku_details 完全一致）。

    pages：按滑动顺序排的各屏 dump（scanner.parse_nodes 的产物）。
    expected_specs：可选，期望的规格集合；给了就校验「不多不少」，用来证明
    页面已经滑到底、没漏屏。

    歧义、缺行、缺字段抛 C.CaptureContractError（同 parse_sku_details）。
    """
    details = parse_sku_details(stitch_pages(pages))
    if expected_specs is not None:
        want = {_norm(s) for s in expected_specs}
        got = {_norm(d["规格"]) for d in details}
        if want != got:
            raise C.CaptureContractError(
                f"采集到的规格集合与目标不一致：缺 {sorted(want - got)}；"
                f"多 {sorted(got - want)}")
    return details


def parse_sku_details(nodes):
    """把详情页层级解析成每个 SKU 一条详情字典。

    nodes 是 scanner.parse_nodes 的产物。返回 list[dict]，每个字典含：
    规格、条码、SKU、重量、采购价_详情、采购单位_详情、三渠道的状态/零售价/
    库存，外加页面公共字段 SPU、开放平台标识、可售日期、礼袋、是否力荐、
    是否标品、卖点。

    歧义、缺行、缺字段抛 C.CaptureContractError（消息带可定位信息）。
    """
    ns = _clean_nodes(nodes)
    if not ns:
        raise C.CaptureContractError("详情页层级为空，解析不出任何 SKU")
    identity = _identity(ns)
    shared = _shared(ns)
    rows = _purchase_rows(ns)
    sections = _channel_sections(ns)
    known = {_norm(row["规格"]) for row in rows}
    for channel, entries in sections.items():
        for _, spec_text, _, _ in entries:
            if _norm(spec_text) not in known:
                raise C.CaptureContractError(
                    f"渠道「{channel}」出现采购表里没有的规格「{spec_text}」")

    details = []
    for row in rows:
        detail = dict(identity)
        detail.update(shared)
        detail.update(row)
        detail.update(_channel_fields(sections, row["规格"],
                                      allow_absent=any(n["text"] == "其他信息" for n in ns)))
        details.append(detail)
    if not details:
        raise C.CaptureContractError("详情页没有解析出任何 SKU")
    return details
