#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 qnh_product_scan.py 的 CSV 写进 retail_price_snapshot 库。

一次采集 = store_snapshot 一行；每个商品在美团闪购 / 饿了么 / 京东到家各写一行
fact_store_product_snapshot（列表卡片上三个渠道各有一个价格）。给了 --category
时再写一级、二级两行类目。门店不存在就新建，存在就更新名称和平台 ID。

默认只在事务里写一遍、打印核对结果后回滚，库里不留数据；确认无误后加 --commit。

用法
----
  python3 qnh_load_db.py qnh_scan_20261008-180000.csv \\
      --store-name "犀牛百货（长安店）" --warehouse-id 1137294 --tenant-id 1003684 \\
      --category 休闲零食/薯片薯条 --platform-count 163 --device R28M31LJX0E
  # 核对没问题后同样的参数加 --commit

依赖：pymysql；数据库连接读 ~/.claude/skills/qnh-database/db_config.json
"""

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pymysql

DB_CONFIG = os.path.expanduser("~/.claude/skills/qnh-database/db_config.json")
DATABASE = "retail_price_snapshot"

# 后台采集的门店记在 qnh 渠道下；商品行按销售渠道拆开。顺序与 CSV 列一一对应。
STORE_CHANNEL = "qnh"
PRICE_CHANNELS = [("meituan", "美团闪购价"), ("eleme", "饿了么价"), ("jddj", "京东到家价")]

BASE_COLUMNS = ["序号", "商品名称", "条码", "店内码", "货号", "状态", "视频时间点", "时刻"]
CARD_COLUMNS = ["规格", "月售", "库存", "售罄", "活动品", "美团闪购价", "饿了么价",
                "京东到家价", "采购价", "价格原文"]
DETAIL_COLUMNS = ["开放平台标识", "SPU", "SKU", "重量", "采购价_详情", "采购单位_详情",
                  "美团闪购状态", "美团闪购零售价", "美团闪购库存",
                  "饿了么状态", "饿了么零售价", "饿了么库存",
                  "京东到家状态", "京东到家零售价", "京东到家库存",
                  "卖点", "可售日期", "礼袋"]
# 详情页的渠道状态 → 库的上架状态枚举
LISTING_STATUS_MAP = {"已上架": "on_shelf", "已下架": "off_shelf", "未上线": "not_online"}

STAMP_PAT = re.compile(r"qnh_scan_(\d{8}-\d{6})")
PURCHASE_PAT = re.compile(r"^(\d+(?:\.\d+)?)(?:/(\S+))?$")


def die(msg):
    sys.exit(f"错误：{msg}")


class LoadError(Exception):
    """已经安全回滚、应该用一行中文提示给人的入库错误。"""


def amount(text):
    try:
        return Decimal(text) if text else None
    except InvalidOperation:
        return None


def field_value(text):
    """详情页没填的字段显示「-」，入库记空。"""
    return None if (text or "").strip() in {"", "-"} else text.strip()


def listing_key(name, barcode, seen):
    """
    牵牛花 App 优先用列表「条码」（13 位 UPC 或 19 位后台内部码）做稳定定位键；
    真正没有条码时才退回标准化商品名。同一采集版本、同一渠道内仍重复的，在
    定位串末尾加 #2、#3，避免唯一键冲突。
    """
    if (barcode or "").strip():
        base = "barcode:" + barcode.strip()
    else:
        base = "name:" + re.sub(r"\s+", " ", name.strip()) + "|"
    seen[base] = seen.get(base, 0) + 1
    key = base if seen[base] == 1 else f"{base}#{seen[base]}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        die(f"{path} 是空文件")
    missing = [c for c in BASE_COLUMNS if c not in rows[0]]
    if missing:
        die(f"{path} 不是 qnh_product_scan.py 的输出（缺少列：{'、'.join(missing)}）")
    missing_card = [c for c in CARD_COLUMNS if c not in rows[0]]
    if missing_card:
        print(f"提示：旧版 CSV 缺少 {'、'.join(missing_card)}；对应字段会留空，其余字段照常入库")
    if DETAIL_COLUMNS[0] not in rows[0]:
        print("提示：旧版 CSV 缺少详情页字段（开放平台标识 / SPU / 渠道状态等）；这些列会留空")
    return rows


def source_prefix(path):
    """CSV 与录像共用同名文件前缀；数据库记前缀即可同时定位两份证据。"""
    return os.path.splitext(os.path.abspath(path))[0]


def row_evidence(path, row):
    """每个商品自己的证据前缀；续跑时可以指向上一轮的 CSV / 录像。"""
    value = row.get("证据前缀") or source_prefix(path)
    return value if os.path.isabs(value) else os.path.abspath(os.path.join(os.path.dirname(path), value))


def row_times(path, rows):
    """
    新版 CSV 直接带完整 ISO 采集时间。旧版则从该行的证据前缀（续跑旧行）或
    当前 CSV 文件名取日期，再和「时刻」组合；因此跨天和多轮续跑不会串日期。
    """
    out = []
    for r in rows:
        raw = r.get("采集时间")
        if raw:
            try:
                out.append(datetime.fromisoformat(raw))
                continue
            except ValueError:
                pass
        m = STAMP_PAT.search(os.path.basename(row_evidence(path, r)))
        try:
            day = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").date() if m else None
            out.append(datetime.combine(day, datetime.strptime(r["时刻"], "%H:%M:%S").time())
                       if day else None)
        except ValueError:
            out.append(None)
    return out


def lower_bound_int(text):
    """「1000+」取 1000；没有数字返回 None。"""
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None


def collector_version():
    """Git 提交 + 脚本内容哈希；即使工作区未提交，也能准确追到实际采集版本。"""
    here = os.path.dirname(os.path.abspath(__file__))
    scanner = os.path.join(here, "qnh_product_scan.py")
    try:
        rev = subprocess.run(["git", "-C", here, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        rev = ""
    try:
        with open(scanner, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        digest = "missing"
    return "qnh_product_scan.py" + (f"@{rev}" if rev else "") + f"+{digest}"


def insert(cur, table, row):
    cols = ", ".join(row)
    cur.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join(['%s'] * len(row))})",
                list(row.values()))


def exact_upc_matches(cur, rows):
    """返回 UPC -> (标准 SPU, 标准 SKU)；只有唯一精确命中的 13 位 UPC 才自动匹配。"""
    upcs = sorted({r["条码"] for r in rows if re.fullmatch(r"\d{13}", r["条码"] or "")})
    if not upcs:
        return {}
    cur.execute(
        "SELECT DISTINCT upc, spu, sku FROM qnh_data.goods_master_goods"
        f" WHERE upc IN ({', '.join(['%s'] * len(upcs))})", upcs)
    found = {}
    for upc, spu, sku in cur.fetchall():
        found.setdefault(upc, set()).add((spu, sku))
    return {upc: next(iter(values)) for upc, values in found.items() if len(values) == 1}


def main():
    ap = argparse.ArgumentParser(description="qnh_product_scan.py 的 CSV 入库")
    ap.add_argument("csv", help="qnh_product_scan.py 输出的 CSV")
    ap.add_argument("--store-name", required=True, help="门店名称，如 犀牛百货（长安店）")
    ap.add_argument("--warehouse-id", required=True, help="牵牛花仓库ID（记为平台门店 ID）")
    ap.add_argument("--tenant-id", help="牵牛花租户ID")
    ap.add_argument("--store-code", help="门店稳定编码，默认 qnh_<仓库ID>")
    ap.add_argument("--role", choices=["competitor", "own"], default="competitor",
                    help="门店角色，默认 competitor")
    ap.add_argument("--category", help="本次采的类目，如 休闲零食/薯片薯条；不给表示没按类目采")
    ap.add_argument("--platform-count", type=int,
                    help="App 上该类目显示的商品数，如「薯片薯条（163）」里的 163")
    ap.add_argument("--status", choices=["success", "partial"],
                    help="采集结果；默认：商品数达到平台数且详情均完整才记 success，否则 partial")
    ap.add_argument("--device", help="采集用的手机（ADB 序列号）")
    ap.add_argument("--video", help="对应的录像；默认找 CSV 同名的 mp4 / mkv")
    ap.add_argument("--commit", action="store_true", help="真正提交；不加则核对后回滚")
    args = ap.parse_args()

    csv_path = os.path.abspath(args.csv)
    rows = read_csv(csv_path)
    times = row_times(csv_path, rows)
    if not any(times):
        die("CSV 里没有一行带有效的「时刻」")
    started = min(t for t in times if t)
    finished = max(t for t in times if t)
    store_code = args.store_code or f"qnh_{args.warehouse_id}"
    cat_l1, cat_l2 = (args.category.split("/", 1) + [None])[:2] if args.category else (None, None)

    video = os.path.abspath(args.video) if args.video else None
    if video and not os.path.exists(video):
        die(f"录像不存在：{video}")
    evidence_prefix = source_prefix(csv_path)
    if not video:
        video = next((evidence_prefix + ext for ext in (".mp4", ".mkv")
                      if os.path.exists(evidence_prefix + ext)), None)

    n = len(rows)
    state_counts = {}
    for r in rows:
        state_counts[r["状态"]] = state_counts.get(r["状态"], 0) + 1
    incomplete = {k: v for k, v in state_counts.items() if k not in {"成功", "自建商品"}}
    count_complete = args.platform_count is not None and n >= args.platform_count
    status = args.status or ("success" if count_complete and not incomplete else "partial")
    reasons = []
    if args.platform_count is not None and n < args.platform_count:
        reasons.append(f"只采了 {n} 个商品，App 显示 {args.platform_count} 个")
    if incomplete:
        reasons.append("详情字段未完整：" + "、".join(f"{k}{v}个" for k, v in incomplete.items()))
    snapshot_id, batch_id = str(uuid.uuid4()), str(uuid.uuid4())
    hour = started.replace(minute=0, second=0, microsecond=0)

    cfg = json.load(open(DB_CONFIG))
    conn = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                           password=cfg["password"], database=DATABASE,
                           charset="utf8mb4", autocommit=False)
    cur = conn.cursor()
    try:
        # 同一份 CSV / 录像只允许形成一个快照。兼容首版入库程序曾记过的
        # 「CSV 文件」「录像文件」两种 evidence_uri 格式。
        evidence_candidates = [evidence_prefix, csv_path]
        if video:
            evidence_candidates.append(video)
        cur.execute(
            "SELECT store_snapshot_id FROM store_snapshot"
            " WHERE channel_code = %s AND store_code = %s AND capture_method = 'app_ui'"
            f" AND evidence_uri IN ({', '.join(['%s'] * len(evidence_candidates))}) LIMIT 1",
            [STORE_CHANNEL, store_code, *evidence_candidates])
        duplicate = cur.fetchone()
        if duplicate:
            raise LoadError(f"这份采集已经入库（store_snapshot_id={duplicate[0]}），已拒绝重复写入")

        cur.execute(
            "INSERT INTO dim_store (channel_code, store_code, store_name, store_role,"
            " source_store_id, source_tenant_id) VALUES (%s, %s, %s, %s, %s, %s)"
            " ON DUPLICATE KEY UPDATE store_name = VALUES(store_name),"
            " source_store_id = VALUES(source_store_id),"
            " source_tenant_id = COALESCE(VALUES(source_tenant_id), source_tenant_id)",
            (STORE_CHANNEL, store_code, args.store_name, args.role,
             args.warehouse_id, args.tenant_id))
        cur.execute("SELECT store_role FROM dim_store WHERE channel_code = %s AND store_code = %s",
                    (STORE_CHANNEL, store_code))
        if cur.fetchone()[0] != args.role:
            raise LoadError(f"门店 {store_code} 在库里的角色和 --role {args.role} 不一致，没有写入")

        insert(cur, "store_snapshot", dict(
            store_snapshot_id=snapshot_id, batch_id=batch_id, snapshot_hour=hour,
            channel_code=STORE_CHANNEL, store_code=store_code, store_name=args.store_name,
            store_role=args.role, source_store_id=args.warehouse_id, status=status,
            capture_method="app_ui", collector_version=collector_version(),
            device_id=args.device, started_at=started, finished_at=finished,
            covered_categories=args.category.replace("/", " > ") if args.category else None,
            evidence_uri=evidence_prefix,
            category_count=2 if cat_l2 else (1 if cat_l1 else None),
            expected_spu_count=args.platform_count, captured_spu_count=n,
            raw_record_count=n, error_message="；".join(reasons) or None))

        if cat_l1:
            common = dict(store_snapshot_id=snapshot_id, batch_id=batch_id, snapshot_hour=hour,
                          channel_code=STORE_CHANNEL, store_code=store_code)
            insert(cur, "fact_store_category_snapshot", dict(
                common, category_key=f"name:{cat_l1}", category_name=cat_l1, category_level=1,
                platform_product_count=None if cat_l2 else args.platform_count,
                captured_spu_count=None if cat_l2 else n))
            if cat_l2:
                insert(cur, "fact_store_category_snapshot", dict(
                    common, category_key=f"name:{cat_l1}/{cat_l2}", category_name=cat_l2,
                    parent_category_key=f"name:{cat_l1}", category_level=2,
                    platform_product_count=args.platform_count, captured_spu_count=n))

        seen = {ch: {} for ch, _ in PRICE_CHANNELS}
        master_matches = exact_upc_matches(cur, rows)
        has_detail = DETAIL_COLUMNS[0] in rows[0]
        written = matched = 0
        for r, t in zip(rows, times):
            purchase = r.get("采购价", "")
            pm = PURCHASE_PAT.match(purchase)
            month = r.get("月售", "")
            sold_out = r.get("售罄", "")
            stock = r.get("库存", "")
            # 列表上的库存是门店库存，三个渠道共用；表注释约定显示「售罄」记 0
            stock_qty = amount(stock) if stock else (Decimal(0) if sold_out == "是" else None)
            # 采购价：详情页优先（¥5.27、单位可能是「单品」），没有退回列表卡片
            detail_buy = r.get("采购价_详情", "") if has_detail else ""
            detail_unit = r.get("采购单位_详情", "") if has_detail else ""
            purchase_amount = amount(detail_buy) or (amount(pm.group(1)) if pm else None)
            purchase_unit = detail_unit or (pm.group(2) if pm else None)
            for ch, col in PRICE_CHANNELS:
                key = listing_key(r["商品名称"], r["条码"], seen[ch])
                # 详情页的字段更权威（渠道、状态都有标签）；没有详情时退回列表卡片
                detail_status = r.get(ch + "状态", "") if has_detail else ""
                detail_price = r.get(ch + "零售价", "") if has_detail else ""
                detail_stock = r.get(ch + "库存", "") if has_detail else ""
                # 详情有渠道库存用渠道的，没有用门店级；详情的「售罄」按表注释约定记 0
                channel_stock = (Decimal(0) if detail_stock in {"售罄", "已售罄"}
                                 else amount(detail_stock)) if detail_stock else stock_qty
                insert(cur, "fact_store_product_snapshot", dict(
                    batch_id=batch_id, store_snapshot_id=snapshot_id, snapshot_hour=hour,
                    collected_at=t, capture_method="app_ui", channel_code=ch,
                    store_code=store_code, store_name=args.store_name, store_role=args.role,
                    category_name=cat_l1, sub_category_name=cat_l2,
                    listing_key=key,
                    product_name=r["商品名称"].strip(), spec_name=r.get("规格") or None,
                    upc=r["条码"] or None, store_inner_code=field_value(r["店内码"]),
                    article_no=field_value(r["货号"]),
                    open_platform_id=r.get("开放平台标识") or None,
                    source_spu_id=r.get("SPU") or None,
                    source_sku_id=r.get("SKU") or None,
                    weight_text=r.get("重量") or None,
                    front_display_price_amount=amount(detail_price) or amount(r.get(col, "")),
                    purchase_price_amount=purchase_amount,
                    purchase_price_unit=purchase_unit,
                    purchase_price_text=(detail_buy or purchase) or None,
                    month_sales_text=f"月售{month}" if month else None,
                    month_sales_qty=lower_bound_int(month),
                    sale_status={"是": "out_of_stock", "否": "available"}.get(sold_out, "unknown"),
                    stock_text=("已售罄" if sold_out == "是" else stock) or None,
                    stock_quantity=channel_stock,
                    is_promo_item={"是": 1, "否": 0}.get(r.get("活动品", "")),
                    listing_status=LISTING_STATUS_MAP.get(detail_status),
                    selling_points=r.get("卖点") or None,
                    sellable_date_text=r.get("可售日期") or None,
                    gift_bag_text=r.get("礼袋") or None,
                    # 三个渠道价没拆出来时（价格行数不对），原文留在这里备查
                    other_text=(r.get("价格原文") or None) if not r.get(col) else None,
                    evidence_uri=row_evidence(csv_path, r),
                    evidence_video_time=r["视频时间点"] or None))
                written += 1
                standard = master_matches.get(r["条码"])
                if standard:
                    cur.execute(
                        "INSERT IGNORE INTO map_source_sku_standard"
                        " (channel_code, listing_key, listing_type, product_name, spec_name, upc,"
                        " standard_spu_id, standard_sku_id, match_status, match_method, match_score, remark)"
                        " VALUES (%s, %s, 'barcode', %s, %s, %s, %s, %s, 'matched', 'upc', 1.0000, %s)",
                        (ch, key, r["商品名称"].strip(), r.get("规格") or None, r["条码"],
                         standard[0], standard[1], "牵牛花采集：13位 UPC 精确匹配"))
                    matched += cur.rowcount

        print(f"门店：{args.store_name}（{STORE_CHANNEL}/{store_code}，{args.role}）")
        print(f"采集版本：{snapshot_id}  状态 {status}  {started:%Y-%m-%d %H:%M:%S} ~ {finished:%H:%M:%S}")
        print(f"商品：{n} 个 × {len(PRICE_CHANNELS)} 个渠道 = {written} 行")
        cur.execute("SELECT channel_code, COUNT(*), COUNT(front_display_price_amount),"
                    " COUNT(store_inner_code), COUNT(purchase_price_amount),"
                    " SUM(sale_status = 'out_of_stock')"
                    " FROM fact_store_product_snapshot WHERE store_snapshot_id = %s"
                    " GROUP BY channel_code", (snapshot_id,))
        print("  渠道        行数  有价格  有店内码  有采购价  售罄")
        for ch, cnt, price, code, buy, out in cur.fetchall():
            print(f"  {ch:<10}{cnt:>6}{price:>8}{code:>10}{buy:>10}{int(out or 0):>6}")
        cur.execute("SELECT COUNT(*) FROM v_store_product_latest WHERE store_code = %s",
                    (store_code,))
        print(f"「最新价格」视图里这家店：{cur.fetchone()[0]} 行")

        if args.commit:
            conn.commit()
            print("已提交。")
        else:
            conn.rollback()
            print("核对模式：已回滚，库里没有留下数据。确认无误后加 --commit 再跑一次。")
    except LoadError as e:
        conn.rollback()
        die(str(e))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
