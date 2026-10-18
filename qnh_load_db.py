#!/usr/bin/env python3
"""Load qnh_product_scan captures into retail_price_snapshot, transactionally."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from decimal import Decimal

import pymysql

import qnh_capture_common as C

DATABASE = "retail_price_snapshot"
STORE_CHANNEL = C.STORE_CHANNEL
PRICE_CHANNELS = C.PRICE_CHANNELS
BASE_COLUMNS = C.BASE_COLUMNS
CARD_COLUMNS = C.CARD_COLUMNS
DETAIL_COLUMNS = C.DETAIL_COLUMNS
LISTING_STATUS_MAP = C.LISTING_STATUS_MAP
YES_NO_MAP = C.YES_NO_MAP
IS_STANDARD_MAP = C.IS_STANDARD_MAP
PURCHASE_PAT = C.PURCHASE_PAT
amount = C.amount
field_value = C.field_value
listing_key = C.listing_key
source_prefix = C.source_prefix
row_evidence = C.row_evidence
row_times = C.row_times
lower_bound_int = C.lower_bound_int


class LoadError(Exception):
    pass


def die(message):
    raise SystemExit(f"错误：{message}")


def read_csv(path):
    rows, fields = C.read_csv_rows(path)
    missing = [column for column in BASE_COLUMNS if column not in fields]
    if missing:
        raise LoadError(f"{path} 不是 qnh_product_scan.py 输出（缺少：{'、'.join(missing)}）")
    missing_card = [column for column in CARD_COLUMNS if column not in fields]
    if missing_card:
        print(f"提示：旧版 CSV 缺少 {'、'.join(missing_card)}；对应字段留空")
    if DETAIL_COLUMNS[0] not in fields:
        print("提示：旧版 CSV 缺少详情页字段；对应字段留空")
    return rows


def collector_version():
    here = os.path.dirname(os.path.abspath(__file__))
    scanner = os.path.join(here, "qnh_product_scan.py")
    try:
        rev = subprocess.run(["git", "-C", here, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        rev = ""
    try:
        digest = C.sha256_file(scanner)[:12]
    except OSError:
        digest = "missing"
    return "qnh_product_scan.py" + (f"@{rev}" if rev else "") + f"+{digest}"


def insert(cur, table, row):
    columns = ", ".join(row)
    cur.execute(f"INSERT INTO {table} ({columns}) VALUES ({', '.join(['%s'] * len(row))})",
                list(row.values()))


def exact_upc_matches(cur, rows, chunk_size=500):
    """UPC -> (SPU, SKU); only unique 13-digit exact master hits are eligible."""
    upcs = sorted({C.text_value(row.get("条码")) for row in rows
                   if __import__("re").fullmatch(r"\d{13}", C.text_value(row.get("条码")))})
    found = {}
    for start in range(0, len(upcs), chunk_size):
        chunk = upcs[start:start + chunk_size]
        cur.execute(
            "SELECT DISTINCT upc, spu, sku FROM qnh_data.goods_master_goods"
            f" WHERE upc IN ({', '.join(['%s'] * len(chunk))})", chunk)
        for item in cur.fetchall():
            found.setdefault(item["upc"], set()).add((item["spu"], item["sku"]))
    return {upc: next(iter(values)) for upc, values in found.items() if len(values) == 1}


def _parse_dt(value, fallback):
    if value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return fallback


def _db_config():
    with open(C.db_config_path(), encoding="utf-8") as fh:
        return json.load(fh)


def _open_connection():
    cfg = _db_config()
    return pymysql.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"],
        database=DATABASE, charset="utf8mb4", autocommit=False,
        cursorclass=pymysql.cursors.DictCursor)


def _manifest_context(manifest_path, *, expected_profile=None,
                      profiles_file=C.DEFAULT_PROFILE_FILE):
    report = C.validate_manifest(
        manifest_path, require_complete=True, expected_profile=expected_profile)
    if not report.ok:
        raise LoadError(C.format_gate_report(report))
    manifest = report.manifest
    profile = manifest["profile"]
    try:
        configured_profile = C.load_profile(profile["name"], profiles_file)
    except C.CaptureContractError as exc:
        raise LoadError(f"manifest profile 无法从配置确认：{exc}") from exc
    mismatches = C.profile_mismatches(profile, configured_profile)
    if mismatches:
        raise LoadError("manifest profile 与配置不一致：" + "；".join(mismatches))
    scope = manifest["scope"]
    artifacts = manifest["artifacts"]
    return {
        "manifest": manifest,
        "gate": report,
        "csv_path": os.path.abspath(artifacts["csv_path"]),
        "store_name": profile["store_name"],
        "warehouse_id": str(profile["warehouse_id"]),
        "tenant_id": str(profile["tenant_id"]),
        "store_code": profile["store_code"],
        "role": profile["role"],
        "category": scope.get("category"),
        "segments": scope.get("segments"),
        "platform_count": int(scope["expected_product_count"]),
        "device": manifest.get("device_id"),
        "status_override": "success" if manifest["state"] == "success" else "partial",
        "snapshot_id": manifest["capture_id"],
        "batch_id": manifest["batch_id"],
        "manifest_path": os.path.abspath(manifest_path),
        "manifest_sha256": report.manifest_sha256,
        "csv_sha256": report.csv_sha256,
    }


def _existing_capture_result(conn, context):
    if not context.get("manifest"):
        return None
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM store_snapshot WHERE store_snapshot_id = %s",
                    (context["snapshot_id"],))
        row = cur.fetchone()
        if not row:
            return None
        expected = {
            "batch_id": context["batch_id"],
            "snapshot_hour": context["snapshot_hour"],
            "channel_code": STORE_CHANNEL,
            "store_code": context["store_code"],
            "store_name": context["store_name"],
            "store_role": context["role"],
            "source_store_id": context["warehouse_id"],
            "status": context["status"],
            "capture_method": "app_ui",
            "collector_version": context["collector_version"],
            "device_id": context["device"],
            "started_at": context["started_at"],
            "finished_at": context["finished_at"],
            "covered_categories": context["covered_categories"],
            "evidence_uri": context["evidence_prefix"],
            "manifest_uri": context["manifest_path"],
            "manifest_sha256": context["manifest_sha256"],
            "csv_sha256": context["csv_sha256"],
            "category_count": context["category_count"],
            "expected_spu_count": context["platform_count"],
            "captured_spu_count": context["row_count"],
            "captured_sku_count": context["row_count"],
            "raw_record_count": context["raw_record_count"],
            "error_message": context["error_message"],
        }
        mismatches = [f"{key}: DB={row.get(key)!r} manifest={value!r}"
                      for key, value in expected.items() if row.get(key) != value]
        cur.execute(
            "SELECT store_name,store_role,source_store_id,source_tenant_id FROM dim_store"
            " WHERE channel_code=%s AND store_code=%s",
            (STORE_CHANNEL, context["store_code"]))
        store = cur.fetchone()
        expected_store = {
            "store_name": context["store_name"],
            "store_role": context["role"],
            "source_store_id": context["warehouse_id"],
            "source_tenant_id": context["tenant_id"],
        }
        if not store:
            mismatches.append("dim_store 不存在")
        else:
            mismatches.extend(
                f"dim_store.{key}: DB={store.get(key)!r} profile={value!r}"
                for key, value in expected_store.items() if store.get(key) != value)
        cur.execute("SELECT COUNT(*) AS n FROM fact_store_product_snapshot"
                    " WHERE store_snapshot_id=%s", (context["snapshot_id"],))
        fact_count = cur.fetchone()["n"]
        if fact_count != context["fact_row_count"]:
            mismatches.append(
                f"事实表行数: DB={fact_count} expected={context['fact_row_count']}")
        cur.execute("SELECT COUNT(*) AS n FROM fact_store_category_snapshot"
                    " WHERE store_snapshot_id=%s", (context["snapshot_id"],))
        category_rows = cur.fetchone()["n"]
        expected_category_rows = context["category_count"] or 0
        if category_rows != expected_category_rows:
            mismatches.append(
                f"类目表行数: DB={category_rows} expected={expected_category_rows}")
        if mismatches:
            raise LoadError("capture_id 已存在但内容不一致：" + "；".join(mismatches))
        import qnh_validate_db as validator
        validation = validator.validate_snapshot(
            conn, context["csv_path"], context["snapshot_id"],
            manifest_path=context["manifest_path"])
        if not validation.ok:
            raise LoadError("已存在 capture 校验失败：" + json.dumps(
                validation.as_dict(), ensure_ascii=False))
        return {
            "ok": True,
            "snapshot_id": context["snapshot_id"],
            "batch_id": row["batch_id"],
            "status": row["status"],
            "rows": context["row_count"],
            "fact_rows": context["fact_row_count"],
            "new_matches": 0,
            "committed": True,
            "recovered": True,
            "validation": validation.as_dict(),
        }
    finally:
        cur.close()


def load_capture(*, csv_path=None, manifest_path=None, require_complete=False,
                 store_name=None, warehouse_id=None, tenant_id=None, store_code=None,
                 role="competitor", category=None, platform_count=None,
                 status_override=None, device=None, video=None, commit=False,
                 expected_profile=None, profiles_file=C.DEFAULT_PROFILE_FILE,
                 allow_legacy_partial_commit=False):
    if manifest_path:
        context = _manifest_context(
            os.path.abspath(manifest_path), expected_profile=expected_profile,
            profiles_file=profiles_file)
    else:
        if not csv_path or not store_name or not warehouse_id:
            raise LoadError("直接 CSV 模式需要 csv、store_name、warehouse_id")
        if commit:
            if not allow_legacy_partial_commit:
                raise LoadError(
                    "直接 CSV 持久化仅用于显式旧版 partial：需加 --allow-legacy-partial-commit")
            if status_override != "partial":
                raise LoadError("直接 CSV 持久化必须显式传 --status partial")
            if tenant_id is None or not store_code:
                raise LoadError("直接 CSV 持久化必须显式提供 --tenant-id 和 --store-code")
        context = {
            "manifest": None,
            "gate": None,
            "csv_path": os.path.abspath(csv_path),
            "store_name": store_name,
            "warehouse_id": str(warehouse_id),
            "tenant_id": str(tenant_id) if tenant_id is not None else None,
            "store_code": store_code or f"qnh_{warehouse_id}",
            "role": role,
            "category": category,
            "platform_count": platform_count,
            "device": device,
            "status_override": status_override,
            "snapshot_id": str(uuid.uuid4()),
            "batch_id": str(uuid.uuid4()),
            "manifest_path": None,
            "manifest_sha256": None,
            "csv_sha256": None,
        }

    rows = read_csv(context["csv_path"])
    times = C.row_times(context["csv_path"], rows)
    valid_times = [value for value in times if value]
    if not valid_times:
        raise LoadError("CSV 没有有效采集时间")
    started, finished = min(valid_times), max(valid_times)
    manifest = context.get("manifest") or {}
    capture_meta = manifest.get("capture") or {}
    started = _parse_dt(capture_meta.get("started_at"), started)
    finished = _parse_dt(capture_meta.get("finished_at"), finished)

    n = len(rows)
    counts = C.status_counts(rows)
    incomplete = {key: value for key, value in counts.items()
                  if key not in C.COMPLETE_ROW_STATUSES}
    count_complete = context["platform_count"] is not None and n >= int(context["platform_count"])
    status = context["status_override"] or ("success" if count_complete and not incomplete else "partial")
    reasons = []
    if context["platform_count"] is not None and n < int(context["platform_count"]):
        reasons.append(f"只采了 {n} 个商品，App 显示 {context['platform_count']} 个")
    if incomplete:
        reasons.append("详情字段未完整：" + "、".join(f"{key}{value}个" for key, value in incomplete.items()))

    evidence_prefix = C.source_prefix(context["csv_path"])
    video_path = os.path.abspath(video) if video else next(
        (evidence_prefix + ext for ext in (".mp4", ".mkv")
         if os.path.exists(evidence_prefix + ext)), None)
    if video and not os.path.exists(video_path):
        raise LoadError(f"录像不存在：{video_path}")
    category_parts = (context["category"].split("/", 1) + [None])[:2] if context["category"] else (None, None)
    segments = context.get("segments") or []
    row_categories = (C.row_category_map(manifest, n) if manifest else
                      [tuple(category_parts)] * n)
    category_l1, category_l2 = category_parts
    if segments:
        top_names = list(dict.fromkeys(segment["top"] for segment in segments))
        covered_categories = "|".join(top_names)
        category_count = len(top_names) + sum(segment["leaf"] != segment["top"]
                                               for segment in segments)
    else:
        covered_categories = context["category"].replace("/", " > ") if context["category"] else None
        category_count = 2 if category_l2 else (1 if category_l1 else None)
    hour = started.replace(minute=0, second=0, microsecond=0)
    raw_record_count = ((manifest.get("scope") or {}).get("raw_segment_product_count", n)
                        if manifest else n)
    context.update({
        "started_at": started,
        "finished_at": finished,
        "snapshot_hour": hour,
        "status": status,
        "collector_version": capture_meta.get("collector_version") or collector_version(),
        "evidence_prefix": evidence_prefix,
        "covered_categories": covered_categories,
        "video_path": video_path,
        "row_count": n,
        "raw_record_count": raw_record_count,
        "fact_row_count": n * len(PRICE_CHANNELS),
        "category_count": category_count,
        "error_message": "；".join(reasons) or None,
    })

    conn = _open_connection()
    try:
        recovered = _existing_capture_result(conn, context)
        if recovered:
            conn.rollback()
            return recovered
        cur = conn.cursor()
        try:
            evidence_candidates = [evidence_prefix, context["csv_path"]]
            if video_path:
                evidence_candidates.append(video_path)
            cur.execute(
                "SELECT store_snapshot_id FROM store_snapshot"
                " WHERE channel_code=%s AND store_code=%s AND capture_method='app_ui'"
                f" AND evidence_uri IN ({', '.join(['%s'] * len(evidence_candidates))}) LIMIT 1",
                [STORE_CHANNEL, context["store_code"], *evidence_candidates])
            duplicate = cur.fetchone()
            if duplicate:
                raise LoadError(f"这份证据已经入库（{duplicate['store_snapshot_id']}）")

            cur.execute(
                "SELECT store_name,store_role,source_store_id,source_tenant_id"
                " FROM dim_store WHERE channel_code=%s AND store_code=%s",
                (STORE_CHANNEL, context["store_code"]))
            existing_store = cur.fetchone()
            expected_store = {
                "store_name": context["store_name"],
                "store_role": context["role"],
                "source_store_id": context["warehouse_id"],
                "source_tenant_id": context["tenant_id"],
            }
            if existing_store:
                store_mismatches = [
                    f"{key}: DB={existing_store.get(key)!r} profile={value!r}"
                    for key, value in expected_store.items()
                    if existing_store.get(key) != value
                ]
                if store_mismatches:
                    raise LoadError("门店维表与 profile 不一致：" + "；".join(store_mismatches))
            else:
                cur.execute(
                    "INSERT INTO dim_store (channel_code,store_code,store_name,store_role,"
                    " source_store_id,source_tenant_id) VALUES (%s,%s,%s,%s,%s,%s)",
                    (STORE_CHANNEL, context["store_code"], context["store_name"],
                     context["role"], context["warehouse_id"], context["tenant_id"]))

            insert(cur, "store_snapshot", dict(
                store_snapshot_id=context["snapshot_id"], batch_id=context["batch_id"],
                snapshot_hour=hour, channel_code=STORE_CHANNEL,
                store_code=context["store_code"], store_name=context["store_name"],
                store_role=context["role"], source_store_id=context["warehouse_id"],
                status=status, capture_method="app_ui",
                collector_version=context["collector_version"],
                device_id=context["device"], started_at=started, finished_at=finished,
                covered_categories=context["covered_categories"],
                evidence_uri=evidence_prefix,
                manifest_uri=context["manifest_path"],
                manifest_sha256=context["manifest_sha256"], csv_sha256=context["csv_sha256"],
                category_count=category_count,
                expected_spu_count=context["platform_count"], captured_spu_count=n,
                captured_sku_count=n, raw_record_count=raw_record_count,
                error_message=context["error_message"]))

            if segments:
                common = dict(store_snapshot_id=context["snapshot_id"],
                              batch_id=context["batch_id"], snapshot_hour=hour,
                              channel_code=STORE_CHANNEL, store_code=context["store_code"])
                top_order = {}
                for segment in segments:
                    top = segment["top"]
                    if top not in top_order:
                        top_order[top] = len(top_order) + 1
                        insert(cur, "fact_store_category_snapshot", dict(
                            common, category_key=f"name:{top}", category_name=top,
                            category_level=1, category_order=top_order[top],
                            platform_product_count=sum(item["expected_count"] for item in segments
                                                       if item["top"] == top),
                            captured_spu_count=sum(item["captured_count"] for item in segments
                                                   if item["top"] == top)))
                    if segment["leaf"] != top:
                        insert(cur, "fact_store_category_snapshot", dict(
                            common, category_key=f"name:{top}/{segment['leaf']}",
                            category_name=segment["leaf"], parent_category_key=f"name:{top}",
                            category_level=2,
                            platform_product_count=segment["expected_count"],
                            captured_spu_count=segment["captured_count"]))
            elif category_l1:
                common = dict(store_snapshot_id=context["snapshot_id"], batch_id=context["batch_id"],
                              snapshot_hour=hour, channel_code=STORE_CHANNEL,
                              store_code=context["store_code"])
                insert(cur, "fact_store_category_snapshot", dict(
                    common, category_key=f"name:{category_l1}", category_name=category_l1,
                    category_level=1,
                    platform_product_count=None if category_l2 else context["platform_count"],
                    captured_spu_count=None if category_l2 else n))
                if category_l2:
                    insert(cur, "fact_store_category_snapshot", dict(
                        common, category_key=f"name:{category_l1}/{category_l2}",
                        category_name=category_l2, parent_category_key=f"name:{category_l1}",
                        category_level=2, platform_product_count=context["platform_count"],
                        captured_spu_count=n))

            seen = {channel: {} for channel, _ in PRICE_CHANNELS}
            master_matches = exact_upc_matches(cur, rows)
            written = matched = 0
            for source, collected_at, row_category in zip(rows, times, row_categories):
                row_category_l1, row_category_l2 = row_category
                for channel, price_column in PRICE_CHANNELS:
                    key = listing_key(
                        source["商品名称"], source.get("条码"), seen[channel],
                        source.get("SKU"), source.get("SPU"))
                    fact_fields = C.fact_fields_from_csv(
                        source, channel, price_column, context["csv_path"])
                    insert(cur, "fact_store_product_snapshot", dict(
                        batch_id=context["batch_id"], store_snapshot_id=context["snapshot_id"],
                        snapshot_hour=hour, collected_at=collected_at, capture_method="app_ui",
                        channel_code=channel, store_code=context["store_code"],
                        store_name=context["store_name"], store_role=context["role"],
                        category_name=row_category_l1, sub_category_name=row_category_l2,
                        listing_key=key, **fact_fields))
                    written += 1
                    standard = master_matches.get(C.text_value(source.get("条码")))
                    if standard:
                        cur.execute(
                            "INSERT IGNORE INTO map_source_sku_standard"
                            " (channel_code,listing_key,listing_type,product_name,spec_name,upc,"
                            " standard_spu_id,standard_sku_id,match_status,match_method,match_score,remark)"
                            " VALUES (%s,%s,'barcode',%s,%s,%s,%s,%s,'matched','upc',1.0000,%s)",
                            (channel, key, C.text_value(source.get("商品名称")),
                             field_value(source.get("规格")), C.text_value(source.get("条码")),
                             standard[0], standard[1],
                             "牵牛花采集：13位 UPC 精确匹配"))
                        matched += cur.rowcount
        finally:
            cur.close()

        import qnh_validate_db as validator
        validation = validator.validate_snapshot(
            conn, context["csv_path"], context["snapshot_id"],
            manifest_path=context["manifest_path"])
        if not validation.ok:
            raise LoadError("事务内逐字段校验失败：" + json.dumps(
                validation.as_dict(), ensure_ascii=False))

        result = {
            "ok": True,
            "snapshot_id": context["snapshot_id"],
            "batch_id": context["batch_id"],
            "status": status,
            "rows": n,
            "fact_rows": written,
            "new_matches": matched,
            "committed": bool(commit),
            "recovered": False,
            "validation": validation.as_dict(),
        }
        if commit:
            conn.commit()
        else:
            conn.rollback()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _print_result(result):
    state = "已提交" if result["committed"] else "核对模式：已回滚"
    if result.get("recovered"):
        state = "已存在且校验一致（幂等恢复）"
    print(f"snapshot_id={result['snapshot_id']}  status={result['status']}")
    print(f"商品 {result.get('rows')} 个，事实表 {result.get('fact_rows', '既有')} 行，"
          f"新增精确匹配 {result.get('new_matches', '既有')}；{state}")
    print(f"事务校验：{'PASS' if result['validation']['ok'] else 'FAIL'}")


def main():
    parser = argparse.ArgumentParser(description="牵牛花 CSV 事务入库")
    parser.add_argument("csv", nargs="?", help="CSV（manifest 模式可省略）")
    parser.add_argument("--manifest", help="finalized manifest")
    parser.add_argument("--profiles-file", default=C.DEFAULT_PROFILE_FILE,
                        help="manifest profile 的可信配置文件")
    parser.add_argument("--require-complete", action="store_true",
                        help="兼容选项；manifest 模式始终执行严格 full gate")
    parser.add_argument("--store-name")
    parser.add_argument("--warehouse-id")
    parser.add_argument("--tenant-id")
    parser.add_argument("--store-code")
    parser.add_argument("--role", choices=["competitor", "own"], default="competitor")
    parser.add_argument("--category")
    parser.add_argument("--platform-count", type=int)
    parser.add_argument("--status", dest="status_override", choices=["success", "partial"])
    parser.add_argument("--device")
    parser.add_argument("--video")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--allow-legacy-partial-commit", action="store_true",
                        help="仅旧 CSV 手工 partial；manifest 正式路径不需要")
    parser.add_argument("--result-json", help="原子写机器结果")
    parser.add_argument("--json", action="store_true", help="末行输出机器 JSON")
    args = parser.parse_args()
    if args.commit and not args.result_json:
        die("--commit 必须同时提供 --result-json，确保提交结果可恢复")
    if args.commit:
        C.atomic_write_json(args.result_json, {
            "ok": False,
            "state": "commit_pending",
            "committed": None,
            "manifest_path": os.path.abspath(args.manifest) if args.manifest else None,
            "csv_path": os.path.abspath(args.csv) if args.csv else None,
        })
    try:
        result = load_capture(
            csv_path=args.csv, manifest_path=args.manifest,
            require_complete=args.require_complete,
            store_name=args.store_name, warehouse_id=args.warehouse_id,
            tenant_id=args.tenant_id, store_code=args.store_code, role=args.role,
            category=args.category, platform_count=args.platform_count,
            status_override=args.status_override, device=args.device, video=args.video,
            commit=args.commit, profiles_file=os.path.abspath(args.profiles_file),
            allow_legacy_partial_commit=args.allow_legacy_partial_commit)
    except (LoadError, C.CaptureContractError) as exc:
        die(str(exc))
    _print_result(result)
    if args.result_json:
        try:
            C.atomic_write_json(args.result_json, result)
        except OSError as exc:
            if args.commit:
                die(f"提交已完成，但最终 result 写入失败；commit_pending 仍在"
                    f" {os.path.abspath(args.result_json)}：{exc}")
            raise
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
