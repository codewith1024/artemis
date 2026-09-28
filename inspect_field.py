#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性诊断：进第一个商品详情页，打印『店内码/货号』附近的真实节点。"""
import sys
import time

sys.path.insert(0, "/Users/jalen/artemis")
import qnh_product_scan as Q

Q.CTX.adb = "/Users/jalen/opt/platform-tools/adb"
Q.CTX.serial = "R28M31LJX0E"
Q.CTX.out_dir = "/tmp"

sw, sh_ = Q.screen_size()

# 可能停在详情页，用脚本自己的逻辑找回列表页（不会退出 App）
nodes, products = Q.ensure_list_page()
if not products:
    print("找不回商品列表页，请手动把牵牛花切到列表页再跑")
    sys.exit(1)

want = sys.argv[1] if len(sys.argv) > 1 else None
p = None
for screen in range(12):
    hits = [q for q in products
            if want is None or want in q["title"]]
    if hits:
        p = hits[0]
        break
    print(f"本屏无匹配（{len(products)} 个），下翻一屏")
    Q.scroll_list(sw, sh_, Q.PACING["natural"])
    time.sleep(1.0)
    nodes, products = Q.dump_products()
    if not products:
        print("翻到底了")
        sys.exit(1)

if p is None:
    print(f"翻了 12 屏没找到标题含 {want!r} 的商品")
    sys.exit(1)

print(f"进入商品：{p['title'][:40]}  点击点 {p['tap']}")
Q.human_tap(*p["tap"], box=p["box"])
time.sleep(2.5)

x_mid = sw // 2
y_from, y_to = int(sh_ * 0.72), int(sh_ * 0.30)

for k in range(5):
    nodes = Q.parse_nodes(Q.dump_xml())
    texts = [n for n in nodes if n["text"]]
    hit = [i for i, n in enumerate(texts)
           if Q.TARGET_FIELD_PAT.search(n["text"])]
    print(f"\n=== 滑动 {k} 次后，命中锚点 {len(hit)} 个 ===")
    for i in hit:
        n = texts[i]
        print(f"  锚点 text={n['text']!r}  x={n['x1']}-{n['x2']} cy={n['cy']} h={n['h']}")
        same_row = [t for t in texts
                    if t is not n
                    and abs(t["cy"] - n["cy"]) <= max(12, n["h"])
                    and t["x1"] >= n["x2"] - 4]
        print(f"    同行右侧：{[(t['text'], t['cy'], t['x1']) for t in same_row] or '（无）'}")
        nxt = texts[i + 1]["text"] if i + 1 < len(texts) else None
        print(f"    文档序下一个（兜底取这个）：{nxt!r}")
    if hit:
        print(f"\n  find_target_field 返回：{Q.find_target_field(nodes)}")
        print("\n  该屏全部文本节点（按 y 排序）：")
        for t in sorted(texts, key=lambda t: t["cy"]):
            print(f"    y={t['cy']:5d} x={t['x1']:4d}-{t['x2']:4d}  {t['text'][:46]!r}")
        break
    Q.human_swipe(x_mid, y_from, x_mid, y_to)
    time.sleep(1.2)

print("\n返回列表页")
Q.sh("input keyevent 4")
time.sleep(1.5)
