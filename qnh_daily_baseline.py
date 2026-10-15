#!/usr/bin/env python3
"""Export the daily carry baseline (携带基线) from the latest committed snapshot.

供 qnh_product_scan.py --carry-baseline 在每日增量采集中跳过指纹一致的
商品详情页。detail 列从 DB 反向映射回 CSV 文本；映射正确性由
fact_fields_from_csv 的 round-trip 抽样自检保证。

已知事实（2026-10-15 核实）：fact_fields_from_csv 以英文渠道键拼中文列名
（"meituan零售价"），渠道详情列从未被读入 DB——全库 listing_status NULL、
价格/库存均走卡片回退。因此：
  * 渠道状态列还原为按 sale_status 推导的合法枚举（入库后仍为 NULL，
    round-trip 天然一致）；
  * 价格/库存按卡片口径还原（这正是 DB 里存值的口径）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from decimal import Decimal

import pymysql

import qnh_capture_common as C

DB_CONFIG = os.path.expanduser("~/.claude/skills/qnh-database/db_config.json")
DATABASE = "retail_price_snapshot"

YES_NO = {1: "是", 0: "否"}
STD_FLAG = {1: "标品", 0: "非标品"}


def _db_open():
    with open(DB_CONFIG, encoding="utf-8") as fh:
        cfg = json.load(fh)
    return pymysql.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"],
        database=DATABASE, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)


def _txt(value):
    return "" if value is None else str(value)


def _amount_txt(value):
    """decimal → 最短等价文本（45.0000 → '45'，3.5000 → '3.5'）。"""
    return "" if value is None else format(Decimal(value).normalize(), "f")


def _channel_columns(fact):
    """一个渠道 fact 行 → (状态, 零售价, 库存) 三列文本。

    状态按 sale_status 推导：DB 无 listing_status（见模块 docstring）。
    零售价/库存按卡片口径还原（DB 存的就是卡片口径值）。
    """
    sale = fact.get("sale_status")
    if sale == "out_of_stock":
        return "已上架", _amount_txt(fact.get("front_display_price_amount")), "售罄"
    if sale == "available":
        return "已上架", _amount_txt(fact.get("front_display_price_amount")), \
            _amount_txt(fact.get("stock_quantity"))
    return "未上线", "", ""


def row_from_fact(meituan, eleme, jddj):
    """三个渠道的 fact 行（同一商品）→ 一行 40 列 CSV dict（不含序号/时间/证据）。

    还原规则以「fact_fields_from_csv 再解析后与 DB 一致」为准：
      * other_text 非空 = 卡片价格区不是标准 4 行 → split=False 形态：
        三渠道卡价列与采购价列全空，价格原文 = other_text；
      * 采购价_详情 优先 purchase_price_text 原文（金额列 decimal(18,4)
        截断了 6 位小数的原文精度）；「无数据」时金额列留空。
    """
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "商品名称": meituan["product_name"],
        "规格": _txt(meituan.get("spec_name")),
        "条码": _txt(meituan.get("upc")),
        "店内码": _txt(meituan.get("store_inner_code")),
        "货号": _txt(meituan.get("article_no")),
        "开放平台标识": _txt(meituan.get("open_platform_id")),
        "SPU": _txt(meituan.get("source_spu_id")),
        "SKU": _txt(meituan.get("source_sku_id")),
        "重量": _txt(meituan.get("weight_text")),
        "月售": (meituan.get("month_sales_text") or "")[2:],
        "活动品": YES_NO.get(meituan.get("is_promo_item"), ""),
        "是否力荐": YES_NO.get(meituan.get("is_recommended"), ""),
        "是否标品": STD_FLAG.get(meituan.get("is_standard"), ""),
        "卖点": _txt(meituan.get("selling_points")),
        "可售日期": _txt(meituan.get("sellable_date_text")),
        "礼袋": _txt(meituan.get("gift_bag_text")),
    })
    channel_rows = (("美团闪购", meituan), ("饿了么", eleme), ("京东到家", jddj))
    for channel_name, fact in channel_rows:
        status, price, stock = _channel_columns(fact)
        row[channel_name + "状态"] = status
        row[channel_name + "零售价"] = price
        row[channel_name + "库存"] = stock
    # 列表卡口径字段（以 meituan 行为锚，卡片上各渠道一行共用列表库存）。
    row["库存"] = _amount_txt(meituan.get("stock_quantity"))
    sale = meituan.get("sale_status")
    row["售罄"] = "是" if sale == "out_of_stock" else (
        "否" if sale == "available" else "")
    # 采购价：purchase_price_text 原文优先（保留 6 位小数精度与 /单位 后缀）。
    purchase_text = _txt(meituan.get("purchase_price_text"))
    purchase_amount = _amount_txt(meituan.get("purchase_price_amount"))
    if purchase_text and purchase_text != "无数据":
        row["采购价"] = purchase_text
        row["采购价_详情"] = purchase_text.split("/", 1)[0]
        row["采购单位_详情"] = (purchase_text.split("/", 1)[1]
                               if "/" in purchase_text else
                               _txt(meituan.get("purchase_price_unit")))
    else:
        row["采购价"] = purchase_text if purchase_text == "无数据" else ""
        row["采购价_详情"] = purchase_amount if purchase_text == "无数据" else ""
        row["采购单位_详情"] = ""
    other = meituan.get("other_text")
    if other:
        # split=False 形态：价格区非标准 4 行，fact 只存了 other_text 原文。
        row["价格原文"] = other
    else:
        prices = [row["美团闪购零售价"], row["饿了么零售价"], row["京东到家零售价"],
                  row["采购价"] or "无数据"]
        row["美团闪购价"], row["饿了么价"], row["京东到家价"] = prices[0], prices[1], prices[2]
        row["价格原文"] = " | ".join(prices)
    return row


def fingerprint_from_row(row):
    """40 列 CSV 行 → 一级触发指纹（与 scanner compare_card_baseline 对齐）。"""
    return {
        "spec": C.normalize_name(row.get("规格")),
        "sold_out": C.text_value(row.get("售罄")) == "是",
        "promo_item": C.text_value(row.get("活动品")) == "是",
        "prices": [C.carry_price_key(row.get(column)) for column in
                   ("美团闪购价", "饿了么价", "京东到家价", "采购价")],
    }


def _norm(value):
    """round-trip 比较：None/'' 同视为缺值；Decimal 归一为最短文本。"""
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def _roundtrip_errors(row, meituan, eleme, jddj):
    """fact_fields_from_csv round-trip：还原行 → fact 字段应与 DB 一致。"""
    channel_rows = (("meituan", meituan, "美团闪购价"),
                    ("eleme", eleme, "饿了么价"),
                    ("jddj", jddj, "京东到家价"))
    checked_fields = (
        "product_name", "spec_name", "upc", "store_inner_code", "article_no",
        "open_platform_id", "source_spu_id", "source_sku_id", "weight_text",
        "purchase_price_amount", "purchase_price_unit", "purchase_price_text",
        "month_sales_text", "month_sales_qty", "is_promo_item", "listing_status",
        "selling_points", "sellable_date_text", "gift_bag_text", "is_recommended",
        "is_standard", "front_display_price_amount", "stock_text",
        "stock_quantity", "sale_status",
    )
    errors = []
    for channel, fact, price_column in channel_rows:
        restored = C.fact_fields_from_csv(row, channel, price_column, "/dev/null")
        for field in checked_fields:
            actual, expected = _norm(restored.get(field)), _norm(fact.get(field))
            if actual != expected:
                errors.append(f"{channel} {meituan['product_name'][:16]} {field}: "
                              f"还原={restored.get(field)!r} DB={fact.get(field)!r}")
    return errors


def fetch_baseline(conn, store_code):
    """最近 success 快照 → (snapshot_id, products, 组装行缓存)。"""
    cur = conn.cursor()
    cur.execute(
        "SELECT store_snapshot_id FROM store_snapshot"
        " WHERE channel_code=%s AND store_code=%s AND status='success'"
        " ORDER BY started_at DESC LIMIT 1", (C.STORE_CHANNEL, store_code))
    snapshot = cur.fetchone()
    if not snapshot:
        raise SystemExit(f"错误：{store_code} 没有 success 快照，无法导出携带基线")
    snapshot_id = snapshot["store_snapshot_id"]
    cur.execute(
        "SELECT * FROM fact_store_product_snapshot WHERE store_snapshot_id=%s"
        " AND channel_code='meituan'", (snapshot_id,))
    meituan_rows = cur.fetchall()
    cur.execute(
        "SELECT * FROM fact_store_product_snapshot WHERE store_snapshot_id=%s"
        " AND channel_code IN ('eleme','jddj')", (snapshot_id,))
    others = {(r["channel_code"], r["listing_key"]): r for r in cur.fetchall()}
    cur.close()
    products = {}
    rebuilt = []   # (row, meituan, eleme, jddj) 供自检
    missing_channel = 0
    duplicate_listing = 0
    lossy = 0
    for meituan in meituan_rows:
        eleme = others.get(("eleme", meituan["listing_key"]))
        jddj = others.get(("jddj", meituan["listing_key"]))
        if not eleme or not jddj:
            missing_channel += 1
            continue
        row = row_from_fact(meituan, eleme, jddj)
        listing = C.listing_identity(row["商品名称"], row["条码"])
        if listing in products:
            duplicate_listing += 1
            continue
        if _roundtrip_errors(row, meituan, eleme, jddj):
            # DB 装不下原文精度（6 位小数采购价被 decimal(18,4) 截断）或
            # 渠道间残缺形态不可逆：这些商品不携带，每天走完整详情页。
            lossy += 1
            rebuilt.append((row, meituan, eleme, jddj))
            continue
        products[listing] = {
            "fingerprint": fingerprint_from_row(row),
            "detail": [row[column] for column in C.DETAIL_COLUMNS],
            "barcode": row["条码"],
            "name": row["商品名称"],
        }
        rebuilt.append((row, meituan, eleme, jddj))
    if missing_channel:
        print(f"警告：{missing_channel} 个商品缺少渠道行，未入基线", flush=True)
    if duplicate_listing:
        print(f"警告：{duplicate_listing} 个 listing 重复，保留首个", flush=True)
    if lossy:
        print(f"警告：{lossy} 个商品 DB 值不可逆（精度/形态边界），不携带、每日走详情页",
              flush=True)
    return snapshot_id, products, rebuilt


def main():
    parser = argparse.ArgumentParser(description="导出每日增量携带基线")
    parser.add_argument("--profile", default="xiniu-changan")
    parser.add_argument("--profiles-file", default=C.DEFAULT_PROFILE_FILE)
    parser.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "qnh_daily"))
    parser.add_argument("--check-sample", type=int, default=200,
                        help="round-trip 抽样自检数（0=全量，默认 200）")
    args = parser.parse_args()
    profile = C.load_profile(args.profile, args.profiles_file)
    conn = _db_open()
    try:
        snapshot_id, products, rebuilt = fetch_baseline(conn, profile["store_code"])
    finally:
        conn.close()
    print(f"基线快照 {snapshot_id}：{len(products)} 个商品")

    # round-trip 终检：入选基线的商品必须全部可逆（fetch_baseline 已剔除
    # 不可逆行）；这里复检抽样，防止剔除逻辑与自检逻辑脱节。
    carryable = [(row, m, e, j) for row, m, e, j in rebuilt
                 if C.listing_identity(row["商品名称"], row["条码"]) in products]
    sample = carryable if args.check_sample <= 0 else carryable[:args.check_sample]
    errors = []
    for row, meituan, eleme, jddj in sample:
        errors.extend(_roundtrip_errors(row, meituan, eleme, jddj))
    if errors:
        print(f"round-trip 自检失败：{len(errors)} 处（前 5 条）", file=sys.stderr)
        for err in errors[:5]:
            print("  " + err, file=sys.stderr)
        raise SystemExit(1)
    print(f"round-trip 自检：{len(sample)} 商品 PASS")

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, "carry_baseline.json")
    C.atomic_write_json(out_path, {
        "schema_version": 1,
        "baseline_snapshot_id": snapshot_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "products": products,
    })
    print(f"携带基线已导出：{out_path}（{len(products)} 商品）")


if __name__ == "__main__":
    main()
