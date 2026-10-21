# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""QNH 原始列表证据复算：把 uiautomator 原始 XML 还原成有序商品卡列表。

输入是逐屏 dump 的原始 XML（``qnh_acceptance/<date>/count_gaps/<cat>/list_NN.xml``），
输出是可核对的证据：平台标题计数、有序商品卡、列表是否读尽。

结构事实（真机 ARTEMIS/ADB 探索所得，改动前请重新核对）：

* 商品卡容器 = **右侧商品 ScrollView 的直接子 ViewGroup**（左侧类目 ScrollView 无卡）。
* 卡片文本列固定在 x>=380：图片容器 -> 标题 -> ``规格：`` -> ``条码:`` -> 月售/库存 -> 价格。
* 开头截断卡：贴住列表顶边、缺标题与规格，是上一屏卡片的残留 -> 丢弃，等前屏。
* ``bounds=[0,0][0,0]`` 的节点是预取/离屏占位 -> 不计入。
* 列表末尾会出现 ``已加载全部数据``（0 bounds 预取节点）-> 配合末两屏卡序列相同，
  才能证明列表读尽。
* 类目以完整路径（如 ``萌宠专区/猫粮``）传参，页面标题只带末级名。

调用约定：协议 gate 先校验页路径与 hash，通过后再调 :func:`audit_pages`，
inventory 的计数一律重算，不信 JSON 里的 boolean。
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "QNHListEvidenceError",
    "parse_nodes",
    "extract_cards",
    "audit_pages",
]


class QNHListEvidenceError(RuntimeError):
    """原始列表证据不可信（结构异常、缺页、计数漂移、卡内容缺失等）。"""


_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_ZERO_BOUNDS = (0, 0, 0, 0)
_SCROLL_VIEW = "android.widget.ScrollView"

_SPEC_RE = re.compile(r"^规格[:：]")
_BARCODE_RE = re.compile(r"^条码[:：]")
_HEADER_RE = re.compile(r"^(?P<leaf>.+?)[（(](?P<count>\d+)[)）]$")
_PRICE_RE = re.compile(r"^[￥¥0-9.,]+$")
_UNIT_RE = re.compile(r"^[/／]\S{1,4}$|^起$")
_END_MARKER = "已加载全部数据"

# 卡内非标题的固定文案：价格、库存、促销角标等
_NOT_TITLE = (
    "规格",
    "条码",
    "月售",
    "库存",
    "￥",
    "¥",
    "无数据",
    "查看全部价格",
    "改价",
    "活动品",
    "已售罄",
    "力荐",
    "推荐",
    "爆款",
    "秒杀",
    "包邮",
    "特价",
    "查看(",
)


def _text(node: ET.Element) -> str:
    """节点文本，空白归一（换行/连续空格 -> 单空格）。"""
    return re.sub(r"\s+", " ", node.get("text") or "").strip()


def _bounds(node: ET.Element) -> tuple[int, int, int, int] | None:
    match = _BOUNDS_RE.match(node.get("bounds") or "")
    return tuple(int(x) for x in match.groups()) if match else None  # type: ignore[return-value]


def _is_price(text: str) -> bool:
    return bool(_PRICE_RE.match(text))


def _is_title(text: str) -> bool:
    return (
        bool(text)
        and not text.startswith(_NOT_TITLE)
        and not _is_price(text)
        and not _UNIT_RE.match(text)
    )


def _card_fields(card: ET.Element) -> tuple[str | None, str | None, str]:
    """取卡片的 (标题, 规格, 条码)；缺失返回 None。"""
    title: str | None = None
    spec: str | None = None
    barcode = ""
    for node in card.iter():
        text = _text(node)
        if not text:
            continue
        if _SPEC_RE.match(text):
            if spec is None:
                spec = _SPEC_RE.sub("", text).strip()
        elif _BARCODE_RE.match(text):
            if not barcode:
                barcode = _BARCODE_RE.sub("", text).strip()
        elif title is None and _is_title(text):
            title = text
    return title, spec, barcode


def _product_scroll(root: ET.Element) -> ET.Element:
    """右侧商品 ScrollView（含『规格：』文本的那个）。"""
    for node in root.iter():
        if node.get("class") != _SCROLL_VIEW:
            continue
        if any(_SPEC_RE.match(_text(d)) for d in node.iter()):
            return node
    raise QNHListEvidenceError("找不到商品列表 ScrollView：页面里没有任何『规格：』文本")


def _page_cards(root: ET.Element) -> tuple[list[dict[str, str]], ET.Element]:
    """单屏的完整可见商品卡（丢弃开头截断卡与 0 bounds 预取节点）。"""
    scroll = _product_scroll(root)
    scroll_box = _bounds(scroll)
    cards: list[dict[str, str]] = []
    children = list(scroll)
    for index, child in enumerate(children):
        if (child.get("class") == "android.widget.ProgressBar"
                or _text(child) in {"正在加载更多", _END_MARKER}):
            continue
        box = _bounds(child)
        if box == _ZERO_BOUNDS:
            continue  # 预取/离屏占位，不计
        title, spec, barcode = _card_fields(child)
        if title is None or spec is None:
            head_truncated = (
                index == 0
                and box is not None
                and scroll_box is not None
                and box[1] <= scroll_box[1]
            )
            tail_truncated = (
                box is not None and scroll_box is not None
                and box[3] >= scroll_box[3]
                and box[1] >= scroll_box[3] - 260
            )
            if head_truncated or tail_truncated:
                continue  # 边缘残片由相邻重叠页提供完整卡片，门禁另核对连续性
            raise QNHListEvidenceError(
                f"商品卡容器缺 title/spec 且不是开头截断：bounds={box} title={title!r} spec={spec!r}"
            )
        cards.append({"name": title, "barcode": barcode, "spec": spec})
    return cards, scroll


def parse_nodes(xml: str) -> list[dict[str, Any]]:
    """把一页原始 XML 摊平成节点列表（纯 ET，不依赖 scanner）。

    每个节点给 ``text`` 与坐标 ``x1/y1/x2/y2/cx/cy/w/h``（``cx/cy`` 是中心点，
    ``x/y`` 是左上角别名），供只需要文本与坐标的解析器（如 SKU parser、详情证据
    复核）直接使用。
    """
    root = ET.fromstring(xml)
    nodes: list[dict[str, Any]] = []
    for node in root.iter():
        x1, y1, x2, y2 = _bounds(node) or (0, 0, 0, 0)
        width = x2 - x1
        height = y2 - y1
        nodes.append(
            {
                "text": _text(node),
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "cx": x1 + width // 2,
                "cy": y1 + height // 2,
                "w": width,
                "h": height,
                "x": x1,
                "y": y1,
            }
        )
    return nodes


def extract_cards(xml: str) -> list[dict[str, str]]:
    """从一页原始 XML 提取完整可见的商品卡。

    返回 ``[{'name': 标题, 'barcode': 条码, 'spec': 规格}, ...]``，顺序即屏幕顺序。
    开头截断卡与 0 bounds 预取节点不返回；其它缺 title/spec 的容器直接报错，不静默丢弃。
    """
    root = ET.fromstring(xml)
    cards, _ = _page_cards(root)
    return cards


def _header(root: ET.Element, leaf: str) -> tuple[str, int, ET.Element]:
    """类目标题节点（如『猫粮（52）』）与平台计数。"""
    for node in root.iter():
        match = _HEADER_RE.match(_text(node))
        if match and match.group("leaf") == leaf:
            return leaf, int(match.group("count")), node
    raise QNHListEvidenceError(f"页面里找不到类目标题『{leaf}（N）』")


def _identity(card: dict[str, str]) -> tuple[str, str]:
    return (card["name"], card["spec"])


def _overlap(merged: list[dict[str, str]], new: list[dict[str, str]]) -> int:
    """相邻两屏的边界重叠长度（0 表示无法衔接）。"""
    limit = min(len(merged), len(new))
    valid = [
        k
        for k in range(1, limit + 1)
        if [_identity(c) for c in merged[-k:]] == [_identity(c) for c in new[:k]]
    ]
    if not valid:
        return 0
    # 多个可行重叠长度时，重构结果必须一致，否则同名同 spec 的连续卡无法证明出现次数
    results = {tuple(_identity(c) for c in merged + new[k:]) for k in valid}
    if len(results) != 1:
        raise QNHListEvidenceError(
            f"相邻两屏的重叠有歧义（可行重叠长度 {valid}），无法证明商品卡出现次数"
        )
    for k in valid:
        for old, new_card in zip(merged[-k:], new[:k]):
            if old["barcode"] and new_card["barcode"] and old["barcode"] != new_card["barcode"]:
                raise QNHListEvidenceError(
                    f"同一商品在不同屏条码不一致：{old['barcode']} != {new_card['barcode']}"
                )
    return max(valid)


def _page_text(page: str | os.PathLike[str]) -> str:
    """页内容：直接给 XML 文本，或给文件路径。"""
    if isinstance(page, os.PathLike):
        return Path(page).read_text(encoding="utf-8")
    if page.lstrip().startswith("<"):
        return page
    return Path(page).read_text(encoding="utf-8")


def _has_end_marker(root: ET.Element) -> bool:
    return any(_END_MARKER in _text(node) for node in root.iter())


def audit_pages(pages_xml: Sequence[str | os.PathLike[str]], category: str) -> dict[str, Any]:
    """复算一个类目的原始列表证据。

    参数
    ----
    pages_xml: 逐屏原始 XML（文本或路径），必须按滚动顺序排列。
    category:  类目路径（如 ``萌宠专区/猫粮``），页面标题只带末级名（``猫粮``），
               这里取 ``category.rsplit('/', 1)[-1]`` 去比对。

    返回
    ----
    ``{'category': 类目完整路径, 'platform_count': 平台标题计数,
    'cards': 有序商品卡列表, 'list_exhausted': 是否读尽}``

    校验不通过时抛 :class:`QNHListEvidenceError`：各屏计数漂移、相邻屏无重叠、
    首屏不在列表顶部、卡容器缺 title/spec。计数与平台标题不一致**不**在此处报错，
    由调用方拿 ``len(cards)`` 与 ``platform_count`` 对比。

    ``list_exhausted`` 只在两条证据同时成立时为 True：末页有平台原文
    ``已加载全部数据``（该文本可能在 0 bounds 预取节点里），且末两屏的卡序列
    完全相同。缺任一条就是 False（宁可报"没读尽"，也不谎报读完）。
    """
    pages = list(pages_xml)
    if not pages:
        raise QNHListEvidenceError("没有任何页面 XML")

    leaf = category.rsplit("/", 1)[-1].strip()
    merged: list[dict[str, str]] = []
    platform_count: int | None = None
    screen_cards: list[list[dict[str, str]]] = []
    screen_marker: list[bool] = []

    for index, page in enumerate(pages):
        text = _page_text(page)
        root = ET.fromstring(text)
        _, count, header_node = _header(root, leaf)
        if platform_count is None:
            platform_count = count
        elif count != platform_count:
            raise QNHListEvidenceError(
                f"第 {index} 屏的类目计数变了：{platform_count} -> {count}（平台计数漂移）"
            )

        cards, scroll = _page_cards(root)
        if index == 0:
            _assert_top_of_list(scroll, header_node, cards)
            merged = list(cards)
        else:
            overlap = _overlap(merged, cards)
            if overlap == 0:
                raise QNHListEvidenceError(
                    f"第 {index} 屏与上一屏没有边界重叠：缺页或中间有漏采（merged={len(merged)}）"
                )
            merged.extend(cards[overlap:])
        screen_cards.append(cards)
        screen_marker.append(_has_end_marker(root))

    last_two_same = len(screen_cards) >= 2 and screen_cards[-1] == screen_cards[-2]
    exhausted = bool(screen_marker[-1]) and last_two_same
    return {
        "category": category,
        "platform_count": int(platform_count or 0),
        "cards": merged,
        "list_exhausted": exhausted,
    }


def _assert_top_of_list(
    scroll: ET.Element,
    header_node: ET.Element,
    cards: Sequence[dict[str, str]],
) -> None:
    """首屏必须从列表顶部开始：类目标题可见，且首卡不是截断残留。"""
    header_box = _bounds(header_node)
    scroll_box = _bounds(scroll)
    if header_box is None or scroll_box is None or header_box[3] > scroll_box[1]:
        raise QNHListEvidenceError("首屏看不到类目标题，采集没有从列表顶部开始")
    if not cards:
        raise QNHListEvidenceError("首屏没有抽到任何完整商品卡")
    first = _bounds(list(scroll)[0])
    if first is None or first[1] > scroll_box[1]:
        raise QNHListEvidenceError("首屏第一张卡没有贴在列表顶边，采集没有从列表顶部开始")
