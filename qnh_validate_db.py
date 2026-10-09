#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立核对 qnh_product_scan.py 的 CSV 与已提交的数据库快照是否逐值一致。"""

import argparse
import csv
import json
import os
import sys
from collections import Counter
from decimal import Decimal

import pymysql

import qnh_load_db as L


def dec(value):
    return Decimal(value) if value else None


def main():
    ap = argparse.ArgumentParser(description="核对牵牛花 CSV 与 retail_price_snapshot 入库结果")
    ap.add_argument("csv", help="采集 CSV")
    ap.add_argument("--snapshot-id", required=True, help="store_snapshot_id")
    args = ap.parse_args()

    csv_path = os.path.abspath(args.csv)
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("错误：CSV 为空")

    cfg = json.load(open(L.DB_CONFIG))
    conn = pymysql.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"],
        database=L.DATABASE, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()
    failures = []
    passes = []

    def check(name, condition, detail=""):
        (passes if condition else failures).append((name, detail))

    def query(sql, values=()):
        cur.execute(sql, values)
        return cur.fetchall()

    try:
        snapshots = query("SELECT * FROM store_snapshot WHERE store_snapshot_id = %s",
                          (args.snapshot_id,))
        check("采集版本唯一", len(snapshots) == 1, f"实际 {len(snapshots)} 行")
        if not snapshots:
            raise RuntimeError("找不到采集版本，后续检查无法继续")
        snapshot = snapshots[0]
        store_code = snapshot["store_code"]
        prefix = L.source_prefix(csv_path)

        stores = query("SELECT * FROM dim_store WHERE channel_code = %s AND store_code = %s",
                       (snapshot["channel_code"], store_code))
        check("门店维表唯一", len(stores) == 1, f"实际 {len(stores)} 行")
        if stores:
            check("门店名称和角色一致",
                  stores[0]["store_name"] == snapshot["store_name"]
                  and stores[0]["store_role"] == snapshot["store_role"], str(stores[0]))

        check("快照商品数", snapshot["captured_spu_count"] == len(rows),
              f"库 {snapshot['captured_spu_count']} / CSV {len(rows)}")
        check("原始行数", snapshot["raw_record_count"] == len(rows),
              f"库 {snapshot['raw_record_count']} / CSV {len(rows)}")
        check("快照证据前缀", snapshot["evidence_uri"] == prefix,
              f"库 {snapshot['evidence_uri']} / CSV {prefix}")
        check("CSV 证据存在", os.path.exists(prefix + ".csv"), prefix + ".csv")
        has_video = os.path.exists(prefix + ".mp4") or os.path.exists(prefix + ".mkv")
        if not has_video:
            print(f"提示：这份采集没有录像（--record 未开启），只有 CSV 证据：{prefix}")

        products = query(
            "SELECT * FROM fact_store_product_snapshot WHERE store_snapshot_id = %s"
            " ORDER BY channel_code, id", (args.snapshot_id,))
        expected_total = len(rows) * len(L.PRICE_CHANNELS)
        check("商品事实表总行数", len(products) == expected_total,
              f"实际 {len(products)} / 应为 {expected_total}")
        counts = Counter(p["channel_code"] for p in products)
        check("三个渠道行数", counts == Counter({c: len(rows) for c, _ in L.PRICE_CHANNELS}),
              str(counts))
        check("定位键在版本内唯一",
              len({(p["channel_code"], p["listing_key"]) for p in products}) == len(products))

        by_channel = {ch: [p for p in products if p["channel_code"] == ch]
                      for ch, _ in L.PRICE_CHANNELS}
        mismatches = []
        for ch, price_column in L.PRICE_CHANNELS:
            actual = by_channel[ch]
            if len(actual) != len(rows):
                continue
            for index, (source, target) in enumerate(zip(rows, actual), 1):
                purchase = source.get("采购价", "")
                pm = L.PURCHASE_PAT.match(purchase)
                sold_out = source.get("售罄") == "是"
                stock = source.get("库存", "")
                has_detail = L.DETAIL_COLUMNS[0] in source
                detail_price = source.get(ch + "零售价", "") if has_detail else ""
                detail_stock = source.get(ch + "库存", "") if has_detail else ""
                detail_status = source.get(ch + "状态", "") if has_detail else ""
                if detail_stock:
                    channel_stock = (Decimal(0) if detail_stock in {"售罄", "已售罄"}
                                     else dec(detail_stock))
                else:
                    channel_stock = Decimal(0) if sold_out else dec(stock)
                expected = {
                    "product_name": source["商品名称"].strip(),
                    "spec_name": L.field_value(source.get("规格")),
                    "upc": L.field_value(source["条码"]),
                    "store_inner_code": L.field_value(source["店内码"]),
                    "article_no": L.field_value(source["货号"]),
                    "open_platform_id": L.field_value(source.get("开放平台标识")),
                    "source_spu_id": L.field_value(source.get("SPU")),
                    "source_sku_id": L.field_value(source.get("SKU")),
                    "weight_text": L.field_value(source.get("重量")),
                    "front_display_price_amount":
                        dec(detail_price) or dec(source.get(price_column, "")),
                    "purchase_price_amount": dec(L.field_value(source.get("采购价_详情")))
                        if source.get("采购价_详情") else (dec(pm.group(1)) if pm else None),
                    "purchase_price_unit": L.field_value(source.get("采购单位_详情"))
                        if source.get("采购单位_详情") else (pm.group(2) if pm else None),
                    "month_sales_qty": L.lower_bound_int(source.get("月售")),
                    "stock_quantity": channel_stock,
                    "sale_status": "out_of_stock" if sold_out else "available",
                    "listing_status": L.LISTING_STATUS_MAP.get(detail_status),
                    "selling_points": L.field_value(source.get("卖点")),
                    "sellable_date_text": L.field_value(source.get("可售日期")),
                    "gift_bag_text": L.field_value(source.get("礼袋")),
                    "is_recommended": L.YES_NO_MAP.get(source.get("是否力荐", "")),
                    "is_standard": L.IS_STANDARD_MAP.get(source.get("是否标品", "")),
                    "is_promo_item": {"是": 1, "否": 0}.get(source.get("活动品", "")),
                    "evidence_uri": L.row_evidence(csv_path, source),
                    "evidence_video_time": source["视频时间点"] or None,
                }
                for field, wanted in expected.items():
                    if target[field] != wanted:
                        mismatches.append(
                            f"第{index}行 {ch} {field}: CSV={wanted!r} DB={target[field]!r}")
        check("CSV 与事实表关键字段逐值一致", not mismatches,
              "；".join(mismatches[:10]))

        categories = query(
            "SELECT * FROM fact_store_category_snapshot WHERE store_snapshot_id = %s",
            (args.snapshot_id,))
        check("类目行数与快照声明一致", len(categories) == (snapshot["category_count"] or 0),
              f"类目表 {len(categories)} / 快照 {snapshot['category_count']}")

        latest = query(
            "SELECT id FROM v_store_product_latest WHERE store_snapshot_id = %s",
            (args.snapshot_id,))
        check("本快照全部进入最新价格视图",
              {p["id"] for p in products} == {p["id"] for p in latest},
              f"事实 {len(products)} / 视图 {len(latest)}")
    except RuntimeError as e:
        failures.append(("前置条件", str(e)))
    finally:
        conn.close()

    print(f"PASS {len(passes)} / {len(passes) + len(failures)}")
    for name, detail in passes:
        print(f"  OK  {name}" + (f"：{detail}" if detail else ""))
    for name, detail in failures:
        print(f"  FAIL {name}" + (f"：{detail}" if detail else ""))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
