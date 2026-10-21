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

"""原始列表证据复算的单测：用 20261021 真机 dump 的原始 XML 当夹具。

证据目录默认在仓库的 ``qnh_acceptance/20261021/count_gaps/``，
可用环境变量 ``QNH_COUNT_GAPS_DIR`` 指向别的目录。
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from qnh_observation import QNHListEvidenceError, audit_pages, extract_cards, parse_nodes

CAT_FOOD = "萌宠专区/猫粮"
CAT_SNACKS = "萌宠专区/猫零食"

EXPECTED = {
    "cat_food": (CAT_FOOD, 52, 51),
    "cat_snacks": (CAT_SNACKS, 44, 43),
}


def _pages_dir(slug: str) -> Path:
    env = os.environ.get("QNH_COUNT_GAPS_DIR")
    if env:
        candidate = Path(env) / slug
        if candidate.is_dir():
            return candidate
    root = Path(__file__).resolve().parents[2]
    for base in (root, *root.parents):
        candidate = base / "qnh_acceptance" / "20261021" / "count_gaps" / slug
        if candidate.is_dir():
            return candidate
    pytest.skip("找不到原始列表证据目录（可用 QNH_COUNT_GAPS_DIR 指定）")


def _pages(slug: str) -> list[str]:
    return [p.read_text(encoding="utf-8") for p in sorted(_pages_dir(slug).glob("list_*.xml"))]


def _product_scroll(root: ET.Element) -> ET.Element:
    for node in root.iter():
        if node.get("class") != "android.widget.ScrollView":
            continue
        if any((d.get("text") or "").startswith("规格") for d in node.iter()):
            return node
    raise AssertionError("页面里没有商品列表 ScrollView")


def _mutated(xml: str, mutate) -> str:
    root = ET.fromstring(xml)
    mutate(root)
    return ET.tostring(root, encoding="unicode")


@pytest.mark.parametrize("slug", sorted(EXPECTED))
def test_audit_recomputes_the_real_card_count(slug: str) -> None:
    """真实页面复算：平台标题计数 vs 列表卡片数（52/51、44/43）。"""
    category, platform_count, card_count = EXPECTED[slug]
    result = audit_pages(_pages(slug), category)

    assert result["category"] == category
    assert result["platform_count"] == platform_count
    assert len(result["cards"]) == card_count
    assert result["list_exhausted"] is True
    assert all(set(card) == {"name", "barcode", "spec"} for card in result["cards"])


@pytest.mark.parametrize("slug", sorted(EXPECTED))
def test_cards_keep_order_and_duplicates_are_not_collapsed(slug: str) -> None:
    """同名同 spec 的多张卡必须都留着（不能当 set 去重），顺序即滚动顺序。"""
    category, _, card_count = EXPECTED[slug]
    cards = audit_pages(_pages(slug), category)["cards"]

    identities = [(card["name"], card["spec"]) for card in cards]
    assert len(identities) == card_count
    assert all(card["name"] and card["spec"] for card in cards)


def test_spec_none_is_a_valid_spec() -> None:
    """规格写成『无』也是合法卡片，不能当缺字段丢掉。"""
    cards = audit_pages(_pages("cat_food"), CAT_FOOD)["cards"]

    assert any(card["spec"] == "无" for card in cards)


def test_missing_middle_page_is_rejected() -> None:
    """中间少一页：相邻两屏没有边界重叠，必须报错而不是少算。"""
    pages = _pages("cat_food")

    with pytest.raises(QNHListEvidenceError):
        audit_pages([p for i, p in enumerate(pages) if i != 10], CAT_FOOD)


def test_platform_count_change_is_rejected() -> None:
    """某一屏的类目计数变了：平台计数漂移，必须报错。"""
    pages = _pages("cat_food")

    def bump(root: ET.Element) -> None:
        for node in root.iter():
            if (node.get("text") or "").strip() == "猫粮（52）":
                node.set("text", "猫粮（51）")
                return
        raise AssertionError("页面上没有找到类目标题")

    mutated = list(pages)
    mutated[5] = _mutated(pages[5], bump)

    with pytest.raises(QNHListEvidenceError):
        audit_pages(mutated, CAT_FOOD)


def test_removing_one_complete_card_breaks_the_evidence() -> None:
    """抽掉一整张完整卡：结果必须对不上（或直接报错），不能悄悄少一张。"""
    pages = _pages("cat_food")

    def drop_card(root: ET.Element) -> None:
        scroll = _product_scroll(root)
        children = list(scroll)
        scroll.remove(children[1])  # 第二张卡是完整卡

    mutated = list(pages)
    mutated[5] = _mutated(pages[5], drop_card)

    try:
        result = audit_pages(mutated, CAT_FOOD)
    except QNHListEvidenceError:
        return  # 报错也算证据不可信
    assert len(result["cards"]) != 51


def test_card_without_title_is_rejected_not_dropped() -> None:
    """卡容器还在但标题被抹掉：既不是边缘截断就必须报错，不能静默丢卡。"""
    pages = _pages("cat_food")

    def strip_title(root: ET.Element) -> None:
        scroll = _product_scroll(root)
        card = list(scroll)[1]
        for node in list(card.iter()):
            if (node.get("text") or "").strip():
                card.remove(node)  # 卡里的第一个文本节点就是标题
                return
        raise AssertionError("卡里没有找到标题节点")

    mutated = list(pages)
    mutated[5] = _mutated(pages[5], strip_title)

    with pytest.raises(QNHListEvidenceError):
        audit_pages(mutated, CAT_FOOD)


def test_extract_cards_drops_head_truncated_and_prefetch_cards() -> None:
    """单屏抽取：开头截断卡与 0 bounds 预取节点都不算。"""
    first = _pages("cat_food")[0]
    second = _pages("cat_food")[1]

    first_cards = extract_cards(first)
    second_cards = extract_cards(second)
    assert len(first_cards) == 6
    assert len(second_cards) == 5  # 7 个容器 - 1 个开头截断 - 1 个 0 bounds 预取
    assert all(card["name"] and card["spec"] for card in second_cards)


def test_tail_fragment_and_loading_indicator_are_not_fake_cards() -> None:
    pages = _pages("cat_food")
    def add_tail(root: ET.Element) -> None:
        scroll = _product_scroll(root)
        box = [int(v) for v in re.findall(r'\d+', scroll.get('bounds'))]
        bottom = box[3]
        tail = ET.SubElement(scroll, 'node', {'class': 'android.view.ViewGroup',
                                            'bounds': f'[230,{bottom-30}][1053,{bottom}]'})
        ET.SubElement(tail, 'node', {'text': '下一张尚未完整露出的商品',
                                    'bounds': f'[388,{bottom-30}][1053,{bottom}]'})
        ET.SubElement(scroll, 'node', {'class': 'android.widget.ProgressBar',
                                      'bounds': f'[512,{bottom-75}][557,{bottom-30}]'})
        ET.SubElement(scroll, 'node', {'class': 'android.widget.TextView', 'text': '正在加载更多',
                                      'bounds': f'[573,{bottom-75}][771,{bottom-30}]'})
    before = extract_cards(pages[5])
    after = extract_cards(_mutated(pages[5], add_tail))
    assert before == after


def test_parse_nodes_gives_text_and_coordinates() -> None:
    """parse_nodes：给只要文本与坐标的解析器用（如 SKU parser）。"""
    nodes = parse_nodes(_pages("cat_food")[0])

    specs = [node for node in nodes if node["text"].startswith("规格：")]
    assert specs, "没有解析到规格文本"
    for node in specs:
        assert node["x1"] >= 380 and node["w"] > 0 and node["h"] > 0
        assert node["x2"] - node["x1"] == node["w"]
        assert node["y2"] - node["y1"] == node["h"]
        assert node["cx"] == node["x1"] + node["w"] // 2
        assert node["cy"] == node["y1"] + node["h"] // 2
    assert any(node["text"] == "猫粮（52）" for node in nodes)


def test_exhausted_needs_the_platform_end_marker() -> None:
    """末屏没有平台原文『已加载全部数据』就不能报读尽。"""
    pages = _pages("cat_food")

    result = audit_pages(pages[:-1], CAT_FOOD)  # 去掉带 marker 的末屏
    assert result["list_exhausted"] is False

    def break_marker(root: ET.Element) -> None:
        for node in root.iter():
            if (node.get("text") or "").strip() == "已加载全部数据":
                node.set("text", "加载中")
                return
        raise AssertionError("末屏没有找到『已加载全部数据』")

    mutated = list(pages)
    mutated[-1] = _mutated(pages[-1], break_marker)
    assert audit_pages(mutated, CAT_FOOD)["list_exhausted"] is False


def test_exhausted_needs_the_last_two_screens_to_match() -> None:
    """末屏有 marker 但末两屏卡序列不同 -> 不能算读尽（连续校验）。"""
    pages = _pages("cat_food")

    def drop_one_card(root: ET.Element) -> None:
        scroll = _product_scroll(root)
        scroll.remove(list(scroll)[1])  # 末屏少一张卡 -> 与倒数第二屏不再相同

    mutated = list(pages)
    mutated[-1] = _mutated(pages[-1], drop_one_card)
    result = audit_pages(mutated, CAT_FOOD)

    assert result["list_exhausted"] is False
