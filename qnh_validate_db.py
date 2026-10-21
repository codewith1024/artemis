#!/usr/bin/env python3
"""Field-by-field validation for committed or uncommitted 牵牛花 DB snapshots."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import pymysql

import qnh_capture_common as C

DATABASE = "retail_price_snapshot"

# 校验器的期望值刻意不与加载器同源：渠道关键字段（上架状态/零售价/库存/售罄）
# 在下面独立推导，加载器的渠道映射若回归，这里的期望不会跟着错，校验就能抓到。
# 非渠道字段仍复用 C.fact_fields_from_csv，避免两套全实现。
CHANNEL_VIEW = {
    "meituan": ("美团闪购", "美团闪购价"),
    "eleme": ("饿了么", "饿了么价"),
    "jddj": ("京东到家", "京东到家价"),
}
LISTING_STATUS = {"已上架": "on_shelf", "已下架": "off_shelf", "未上线": "not_online"}
SOLD_OUT_TOKENS = {"售罄", "已售罄"}
CARRY_ROW_STATUS = "携带"


@dataclass
class ValidationReport:
    passes: list[tuple[str, str]] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    infos: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self):
        return not self.failures

    def check(self, name, condition, detail=""):
        (self.passes if condition else self.failures).append((name, detail))

    def info(self, name, detail=""):
        self.infos.append((name, detail))

    def as_dict(self):
        return {
            "ok": self.ok,
            "passed": len(self.passes),
            "failed": len(self.failures),
            "info": len(self.infos),
            "failures": [{"name": n, "detail": d} for n, d in self.failures],
            "infos": [{"name": n, "detail": d} for n, d in self.infos],
        }


def _parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value))


def _amount(text):
    """非负金额解析：校验器自带一份，不走加载器的解析。"""
    value = str(text or "").strip()
    if not value:
        return None
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _field(text):
    value = str(text or "").strip()
    return None if value in {"", "-"} else value


def _channel_expectation(source, channel):
    """独立推导一个渠道的关键字段：上架状态 / 零售价 / 库存 / 售罄。

    口径与加载器同语义，但代码独立写一遍（加载器回归时这里不会跟着错）：
      on_shelf → 渠道在售：零售价取详情零售价、缺失回退同渠道卡片价。
      已下架 / 未上线 / 状态缺失 → 渠道不在售：只保留详情页自己的零售价与库存，
                 不拿列表值冒充渠道详情。
      渠道有自己的库存 → 完全按渠道库存判：0 = 售罄，>0 = 可售；列表售罄
                 盖不掉渠道的正库存（否则「已售罄 + 正数」自相矛盾）。
      在售渠道缺自己的库存 → 才回退列表售罄（列表售罄 → 库存 0、文案「已售罄」）。
      渠道不在售又无渠道库存 → sale_status = unknown，不按列表赋可售。
    """
    if channel not in CHANNEL_VIEW:
        raise C.CaptureContractError(f"未知渠道：{channel!r}")
    channel_name, card_column = CHANNEL_VIEW[channel]
    listing_status = LISTING_STATUS.get(_field(source.get(channel_name + "状态")))
    detail_price = _amount(source.get(channel_name + "零售价"))
    card_price = _amount(source.get(card_column))
    detail_stock_text = str(source.get(channel_name + "库存") or "").strip()
    list_stock_text = str(source.get("库存") or "").strip()
    sold_out = str(source.get("售罄") or "").strip() == "是"
    if detail_stock_text:
        detail_stock = (Decimal(0) if detail_stock_text in SOLD_OUT_TOKENS
                        else _amount(detail_stock_text))
    else:
        detail_stock = None
    if listing_status == "on_shelf":
        price = detail_price if detail_price is not None else card_price
    else:
        price = detail_price
    if detail_stock is not None:
        stock = detail_stock
        sold_out = detail_stock == 0
        sale_status = "out_of_stock" if sold_out else "available"
        stock_text = "已售罄" if sold_out else _field(detail_stock_text)
    elif listing_status == "on_shelf":
        stock = Decimal(0) if sold_out else (
            _amount(list_stock_text) if list_stock_text else None)
        sale_status = "out_of_stock" if sold_out else (
            "available" if str(source.get("售罄") or "").strip() in {"是", "否"}
            else "unknown")
        stock_text = "已售罄" if sold_out else _field(list_stock_text)
    else:
        stock = None
        sale_status = "unknown"
        stock_text = None
    card_price_text = str(source.get(card_column) or "").strip()
    return {
        "front_display_price_amount": price,
        "listing_status": listing_status,
        "sale_status": sale_status,
        "stock_quantity": stock,
        "stock_text": stock_text,
        "other_text": (_field(source.get("价格原文")) if not card_price_text else None),
    }


def _expected_fields(source, channel, price_column, csv_path):
    """校验器自己的期望值：渠道关键字段独立推导，其余字段复用加载器转换。"""
    facts = dict(C.fact_fields_from_csv(source, channel, price_column, csv_path))
    facts.update(_channel_expectation(source, channel))
    return facts


def _db_value(value):
    return (value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if isinstance(value, Decimal) else value)


def _field_equal(actual, expected):
    return actual == _db_value(expected)


def _expected_by_key(rows, csv_path, snapshot, category=None, manifest=None):
    expected = {}
    times = C.row_times(csv_path, rows)
    category_parts = (category.split("/", 1) + [None])[:2] if category else (None, None)
    row_categories = (C.row_category_map(manifest, len(rows)) if manifest else
                      [tuple(category_parts)] * len(rows))
    # 多 SKU 卡片按 sidecar 展开成每个 SKU 一行：与加载器同一份展开
    # （C.fact_sources），两处各判各的，校验就成了自证。
    listings = C.sidecar_listings(manifest or {})
    sources = C.fact_sources(rows, listings)
    for channel, (_, price_column) in CHANNEL_VIEW.items():
        seen = {}
        for (source, sku_total, multi, card_index), collected_at, row_category in zip(
                sources, [times[card_index] for *_r, _t, _m, card_index in sources],
                [row_categories[card_index] for *_r, _t, _m, card_index in sources]):
            category_l1, category_l2 = row_category
            key = C.listing_key(
                source["商品名称"], source.get("条码"), seen,
                source.get("SKU"), source.get("SPU"))
            if C.text_value(source.get("状态")) == CARRY_ROW_STATUS:
                # 携带行不入库（见 validate_snapshot 的「携带行不入库」检查）；
                # 但仍参与定位序号计数，保持与加载器一致的 #2/#3 后缀。
                continue
            fields = _expected_fields(source, channel, price_column, csv_path)
            fields["sku_count"] = sku_total
            fields["has_multi_spec"] = 1 if multi else 0
            expected[(channel, key)] = {
                "batch_id": snapshot["batch_id"],
                "store_snapshot_id": snapshot["store_snapshot_id"],
                # 每行事实的 snapshot_hour 取该行采集时间所在小时；快照自身的
                # hour 保持批次开始时刻，不套到全部事实行。
                "snapshot_hour": (collected_at.replace(minute=0, second=0, microsecond=0)
                                  if collected_at else snapshot["snapshot_hour"]),
                "collected_at": collected_at,
                "capture_method": "app_ui",
                "channel_code": channel,
                "store_code": snapshot["store_code"],
                "store_name": snapshot["store_name"],
                "store_role": snapshot["store_role"],
                "category_name": category_l1,
                "sub_category_name": category_l2,
                "listing_key": key,
                **fields,
            }
    return expected


def validate_snapshot(conn, csv_path, snapshot_id, *, manifest_path=None):
    """Validate through ``conn``; uncommitted rows on that connection are visible."""
    report = ValidationReport()
    csv_path = os.path.abspath(csv_path)
    try:
        rows, _ = C.read_csv_rows(csv_path)
    except Exception as exc:
        report.failures.append(("CSV", str(exc)))
        return report
    manifest = C.read_json(manifest_path) if manifest_path else None

    carry_rows = [index for index, row in enumerate(rows, 1)
                  if C.text_value(row.get("状态")) == CARRY_ROW_STATUS]
    if carry_rows:
        report.check("携带行不入库", False,
                     "第" + "、".join(str(index) for index in carry_rows[:10])
                     + "行状态=携带：渠道详情是历史合成值，须重新实访详情页")

    cur = conn.cursor(pymysql.cursors.DictCursor)
    snapshots = []
    try:
        cur.execute("SELECT * FROM store_snapshot WHERE store_snapshot_id = %s", (snapshot_id,))
        snapshots = cur.fetchall()
        report.check("采集版本唯一", len(snapshots) == 1, f"实际 {len(snapshots)} 行")
        if not snapshots:
            return report
        snapshot = snapshots[0]
        store_code = snapshot["store_code"]

        cur.execute("SELECT * FROM dim_store WHERE channel_code = %s AND store_code = %s",
                    (snapshot["channel_code"], store_code))
        stores = cur.fetchall()
        report.check("门店维表唯一", len(stores) == 1, f"实际 {len(stores)} 行")
        profile = (manifest.get("profile") or {}) if manifest else {}
        if stores:
            expected_store = {
                "store_name": profile.get("store_name", snapshot["store_name"]),
                "store_role": profile.get("role", snapshot["store_role"]),
                "source_store_id": (str(profile["warehouse_id"])
                                    if profile.get("warehouse_id") is not None
                                    else snapshot["source_store_id"]),
                "source_tenant_id": (str(profile["tenant_id"])
                                     if profile.get("tenant_id") is not None
                                     else stores[0]["source_tenant_id"]),
            }
            store_mismatches = [
                f"{key}: 库={stores[0].get(key)!r} 应为={value!r}"
                for key, value in expected_store.items() if stores[0].get(key) != value
            ]
            report.check("门店身份一致", not store_mismatches, "；".join(store_mismatches))

        report.check("快照商品数", snapshot["captured_spu_count"] == len(rows),
                     f"库 {snapshot['captured_spu_count']} / CSV {len(rows)}")
        if manifest or snapshot["captured_sku_count"] is not None:
            # SKU 数按多 SKU 展开算：一张卡挂 N 个 SKU 就是 N 行。
            expected_sku_count = len(C.fact_sources(
                rows, C.sidecar_listings(manifest or {})))
            report.check("快照 SKU 数", snapshot["captured_sku_count"] == expected_sku_count,
                         f"库 {snapshot['captured_sku_count']} / CSV {expected_sku_count}")
        else:
            report.info("快照 SKU 数", "旧版快照未记录 captured_sku_count，跳过")
        expected_raw_count = (((manifest.get("scope") or {}).get(
            "raw_segment_product_count", len(rows))) if manifest else len(rows))
        report.check("原始行数", snapshot["raw_record_count"] == expected_raw_count,
                     f"库 {snapshot['raw_record_count']} / manifest {expected_raw_count}")
        prefix = C.source_prefix(csv_path)
        report.check("快照证据前缀", snapshot["evidence_uri"] == prefix,
                     f"库 {snapshot['evidence_uri']} / CSV {prefix}")
        report.check("CSV 证据存在", os.path.isfile(prefix + ".csv"), prefix + ".csv")

        if manifest:
            scope = manifest.get("scope") or {}
            capture = manifest.get("capture") or {}
            started_at = _parse_iso(capture.get("started_at"))
            finished_at = _parse_iso(capture.get("finished_at"))
            category_scope = scope.get("category")
            segments_scope = scope.get("segments") or []
            if segments_scope:
                category_count = (len({item["top"] for item in segments_scope})
                                  + sum(item["leaf"] != item["top"] for item in segments_scope))
                covered_categories = "|".join(dict.fromkeys(
                    item["top"] for item in segments_scope))
            else:
                category_count = (2 if category_scope and "/" in category_scope
                                  else 1 if category_scope else None)
                covered_categories = (category_scope.replace("/", " > ")
                                      if category_scope else None)
            snapshot_expected = {
                "store_snapshot_id": manifest.get("capture_id"),
                "batch_id": manifest.get("batch_id"),
                "snapshot_hour": (started_at.replace(minute=0, second=0, microsecond=0)
                                  if started_at else None),
                "channel_code": C.STORE_CHANNEL,
                "store_code": profile.get("store_code"),
                "store_name": profile.get("store_name"),
                "store_role": profile.get("role"),
                "source_store_id": str(profile.get("warehouse_id")),
                "status": "partial" if manifest.get("state") == C.OBSERVED_MANIFEST_STATE else "success",
                "capture_method": "app_ui",
                "collector_version": capture.get("collector_version"),
                "device_id": manifest.get("device_id"),
                "started_at": started_at,
                "finished_at": finished_at,
                "covered_categories": covered_categories,
                "category_count": category_count,
                "expected_spu_count": scope.get("expected_product_count"),
                "captured_spu_count": len(rows),
                "captured_sku_count": len(C.fact_sources(
                    rows, C.sidecar_listings(manifest))),
                "raw_record_count": scope.get("raw_segment_product_count", len(rows)),
                # error_message 的内容由加载器拼（例外清单等），这里不比内容：
                # 例外态另有一条检查确认它写明了例外。
                "manifest_uri": os.path.abspath(manifest_path),
                "manifest_sha256": C.sha256_file(manifest_path),
                "csv_sha256": C.sha256_file(csv_path),
            }
            snapshot_mismatches = [
                f"{key}: 库={snapshot.get(key)!r} 应为={value!r}"
                for key, value in snapshot_expected.items() if snapshot.get(key) != value
            ]
            observed_manifest = manifest.get("state") == C.OBSERVED_MANIFEST_STATE
            if observed_manifest:
                # 例外态快照：状态必须是 partial，error_message 必须写明例外
                # （不完整性写在快照上，不靠猜）。
                if snapshot.get("status") != "partial":
                    snapshot_mismatches.append(
                        f"status: 库={snapshot.get('status')!r} 应为='partial'")
                error_message = snapshot.get("error_message") or ""
                if "观测例外" not in error_message:
                    snapshot_mismatches.append("error_message: 没有记录观测例外")
            report.check("manifest 与快照逐字段一致", not snapshot_mismatches,
                         "；".join(snapshot_mismatches[:10]))

        cur.execute("SELECT * FROM fact_store_product_snapshot"
                    " WHERE store_snapshot_id = %s", (snapshot_id,))
        products = cur.fetchall()
        # 事实表行数按 SKU 算：多 SKU 卡展开成每个 SKU 一行。
        sku_rows = len(C.fact_sources(rows, C.sidecar_listings(manifest or {})))
        expected_total = sku_rows * len(C.PRICE_CHANNELS)
        report.check("商品事实表总行数", len(products) == expected_total,
                     f"实际 {len(products)} / 应为 {expected_total}")
        counts = Counter(item["channel_code"] for item in products)
        report.check("三个渠道行数",
                     counts == Counter({channel: sku_rows for channel, _ in C.PRICE_CHANNELS}),
                     str(counts))
        actual = {(item["channel_code"], item["listing_key"]): item for item in products}
        report.check("事实表定位键唯一", len(actual) == len(products))
        category = (((manifest.get("scope") or {}).get("category")) if manifest
                    else (snapshot.get("covered_categories") or "").replace(" > ", "/") or None)
        expected = _expected_by_key(rows, csv_path, snapshot, category=category,
                                    manifest=manifest)
        report.check("校验器渠道表与加载器一致",
                     set(CHANNEL_VIEW) == {code for code, _ in C.PRICE_CHANNELS},
                     f"校验器={sorted(CHANNEL_VIEW)} "
                     f"加载器={sorted(code for code, _ in C.PRICE_CHANNELS)}")
        report.check("CSV 定位键唯一", len(expected) == expected_total)
        report.check("CSV/DB 定位键集合一致", set(expected) == set(actual),
                     f"missing={len(set(expected)-set(actual))} extra={len(set(actual)-set(expected))}")
        mismatches = []
        for key, wanted_fields in expected.items():
            target = actual.get(key)
            if not target:
                continue
            for field_name, wanted in wanted_fields.items():
                if not _field_equal(target[field_name], wanted):
                    mismatches.append(
                        f"{key[0]}/{wanted_fields['product_name'][:20]} {field_name}: "
                        f"CSV={wanted!r} DB={target[field_name]!r}")
        report.check("CSV 与事实表逐字段一致", not mismatches, "；".join(mismatches[:10]))

        cur.execute("SELECT * FROM fact_store_category_snapshot WHERE store_snapshot_id = %s",
                    (snapshot_id,))
        categories = cur.fetchall()
        report.check("类目行数与快照声明一致",
                     len(categories) == (snapshot["category_count"] or 0),
                     f"类目表 {len(categories)} / 快照 {snapshot['category_count']}")
        category_parts = (category.split("/", 1) + [None])[:2] if category else (None, None)
        category_l1, category_l2 = category_parts
        category_common = {
            "store_snapshot_id": snapshot_id,
            "batch_id": snapshot["batch_id"],
            "snapshot_hour": snapshot["snapshot_hour"],
            "channel_code": C.STORE_CHANNEL,
            "store_code": snapshot["store_code"],
        }
        segments = ((manifest.get("scope") or {}).get("segments") if manifest else None)
        if segments:
            expected_categories = {}
            top_order = {}
            for segment in segments:
                top, leaf = segment["top"], segment["leaf"]
                if top not in top_order:
                    top_order[top] = len(top_order) + 1
                    expected_categories[f"name:{top}"] = {
                        **category_common, "category_key": f"name:{top}",
                        "category_name": top, "parent_category_key": None,
                        "category_level": 1, "category_order": top_order[top],
                        "platform_product_count": sum(item["expected_count"] for item in segments
                                                       if item["top"] == top),
                        "captured_spu_count": sum(item["captured_count"] for item in segments
                                                   if item["top"] == top),
                    }
                if leaf != top:
                    expected_categories[f"name:{top}/{leaf}"] = {
                        **category_common, "category_key": f"name:{top}/{leaf}",
                        "category_name": leaf, "parent_category_key": f"name:{top}",
                        "category_level": 2,
                        "platform_product_count": segment["expected_count"],
                        "captured_spu_count": segment["captured_count"],
                    }
        else:
            expected_categories = {}
            if category_l1:
                expected_categories[f"name:{category_l1}"] = {
                    **category_common,
                    "category_key": f"name:{category_l1}",
                    "category_name": category_l1,
                    "parent_category_key": None,
                    "category_level": 1,
                    "platform_product_count": None if category_l2 else snapshot["expected_spu_count"],
                    "captured_spu_count": None if category_l2 else len(rows),
                }
                if category_l2:
                    expected_categories[f"name:{category_l1}/{category_l2}"] = {
                        **category_common,
                        "category_key": f"name:{category_l1}/{category_l2}",
                        "category_name": category_l2,
                        "parent_category_key": f"name:{category_l1}",
                        "category_level": 2,
                        "platform_product_count": snapshot["expected_spu_count"],
                        "captured_spu_count": len(rows),
                    }
        actual_categories = {item["category_key"]: item for item in categories}
        category_errors = []
        if set(actual_categories) != set(expected_categories):
            category_errors.append(
                f"keys 库={sorted(actual_categories)} 应为={sorted(expected_categories)}")
        for key, wanted_fields in expected_categories.items():
            target = actual_categories.get(key)
            if not target:
                continue
            for field_name, wanted in wanted_fields.items():
                if target[field_name] != wanted:
                    category_errors.append(
                        f"{key} {field_name}: 库={target[field_name]!r} 应为={wanted!r}")
        report.check("类目快照逐字段一致", not category_errors,
                     "；".join(category_errors[:10]))

        upcs = sorted({C.text_value(row.get("条码")) for row in rows
                       if len(C.text_value(row.get("条码"))) == 13
                       and C.text_value(row.get("条码")).isdigit()})
        master_values = {}
        for start in range(0, len(upcs), 500):
            chunk = upcs[start:start + 500]
            cur.execute(
                "SELECT DISTINCT upc,spu,sku FROM qnh_data.goods_master_goods"
                f" WHERE upc IN ({', '.join(['%s'] * len(chunk))})", chunk)
            for item in cur.fetchall():
                master_values.setdefault(item["upc"], set()).add(
                    (item["spu"], item["sku"]))
        unique_master = {upc: next(iter(values)) for upc, values in master_values.items()
                         if len(values) == 1}
        # map 一行一个 listing（uk_map_listing），不按源 SKU 唯一：同 SKU 同名多规格
        # 合法，同一个 SKU 会对应多个 listing_key。平台源 SKU 不写进 map
        # （uk_map_source_sku 与多规格互斥），事实表已按行留存 source_sku_id。
        expected_maps = {}
        for channel, _ in C.PRICE_CHANNELS:
            seen = {}
            for source in rows:
                key = C.listing_key(
                source["商品名称"], source.get("条码"), seen,
                source.get("SKU"), source.get("SPU"))
                standard = unique_master.get(C.text_value(source.get("条码")))
                if standard:
                    expected_maps[(channel, key)] = {
                        "listing_type": "barcode",
                        "product_name": C.text_value(source.get("商品名称")),
                        "spec_name": C.field_value(source.get("规格")),
                        "upc": C.text_value(source.get("条码")),
                        "standard_spu_id": standard[0],
                        "standard_sku_id": standard[1],
                        "match_status": "matched",
                        "match_method": "upc",
                        "match_score": Decimal("1.0000"),
                        "remark": "牵牛花采集：13位 UPC 精确匹配",
                    }

        map_keys = sorted({key for _, key in expected_maps})
        mapping_rows = []
        for start in range(0, len(map_keys), 500):
            chunk = map_keys[start:start + 500]
            if not chunk:
                continue
            cur.execute("SELECT channel_code,listing_key,listing_type,product_name,spec_name,upc,"
                        " standard_spu_id,standard_sku_id,match_status,match_method,match_score,remark"
                        " FROM map_source_sku_standard"
                        f" WHERE channel_code IN ({', '.join(['%s'] * len(C.PRICE_CHANNELS))})"
                        f" AND listing_key IN ({', '.join(['%s'] * len(chunk))})",
                        [*[channel for channel, _ in C.PRICE_CHANNELS], *chunk])
            mapping_rows.extend(cur.fetchall())
        actual_maps = {(item["channel_code"], item["listing_key"]): item
                       for item in mapping_rows}
        map_errors = []
        manual_preserved = 0
        metadata_drift = 0
        for key, wanted_fields in expected_maps.items():
            item = actual_maps.get(key)
            if not item:
                map_errors.append(f"{key[0]}/{key[1][:10]} 缺少映射")
                continue
            if (item["match_method"] == "manual" and item["match_status"] == "matched"
                    and item["standard_spu_id"] and item["standard_sku_id"]):
                manual_preserved += 1
                continue
            # The global map is INSERT IGNORE: first-capture display metadata is immutable,
            # while the barcode-to-standard identity must remain exact.
            stable_fields = (
                "listing_type", "upc", "standard_spu_id", "standard_sku_id",
                "match_status", "match_method", "match_score",
            )
            mismatched_fields = [
                f"{field}: 库={item.get(field)!r} 应为={wanted_fields[field]!r}"
                for field in stable_fields if item.get(field) != wanted_fields[field]
            ]
            remark = C.text_value(item.get("remark"))
            if not remark.endswith("牵牛花采集：13位 UPC 精确匹配"):
                mismatched_fields.append(f"remark 非牵牛花 UPC 证据：{remark!r}")
            if any(item.get(field) != wanted_fields[field]
                   for field in ("product_name", "spec_name", "remark")):
                metadata_drift += 1
            if mismatched_fields:
                map_errors.append(
                    f"{key[0]}/{key[1][:10]} " + ", ".join(mismatched_fields))
        report.check("精确 UPC 映射逐渠道完整", not map_errors, "；".join(map_errors[:10]))
        if manual_preserved:
            report.info("人工映射", f"保留 {manual_preserved} 条已确认人工映射")
        if metadata_drift:
            report.info("映射展示元数据", f"{metadata_drift} 条沿用首次匹配时的名称/规格/备注")

        cur.execute(
            "WITH ranked AS ("
            " SELECT p.id,p.store_snapshot_id,p.snapshot_hour,"
            " ROW_NUMBER() OVER (PARTITION BY p.channel_code,p.store_code,p.listing_key"
            " ORDER BY p.snapshot_hour DESC,s.started_at DESC,p.id DESC) AS rn,"
            " MAX(p.snapshot_hour) OVER (PARTITION BY p.channel_code,p.store_code)"
            " AS store_latest_hour"
            " FROM fact_store_product_snapshot p"
            " JOIN store_snapshot s ON s.store_snapshot_id=p.store_snapshot_id"
            " WHERE s.status IN ('success','partial')"
            ") SELECT id FROM ranked WHERE store_snapshot_id=%s AND rn=1"
            " AND snapshot_hour >= store_latest_hour - INTERVAL 7 DAY",
            (snapshot_id,))
        expected_latest_ids = {item["id"] for item in cur.fetchall()}
        cur.execute("SELECT id FROM v_store_product_latest WHERE store_snapshot_id = %s",
                    (snapshot_id,))
        actual_latest_ids = {item["id"] for item in cur.fetchall()}
        report.check("最新价格视图与定义一致", expected_latest_ids == actual_latest_ids,
                     f"定义 {len(expected_latest_ids)} / 视图 {len(actual_latest_ids)}")
        if not expected_latest_ids:
            report.info("latest view", "该历史快照没有当前赢家商品")
        elif len(expected_latest_ids) < len(products):
            report.info("latest view", f"历史快照仍有 {len(expected_latest_ids)} 个商品为当前赢家")
    except Exception as exc:
        report.failures.append(("校验异常", f"{type(exc).__name__}: {exc}"))
    finally:
        cur.close()
    return report


def print_report(report):
    print(f"PASS {len(report.passes)} / {len(report.passes) + len(report.failures)}")
    for name, detail in report.passes:
        print(f"  OK   {name}" + (f"：{detail}" if detail else ""))
    for name, detail in report.infos:
        print(f"  INFO {name}" + (f"：{detail}" if detail else ""))
    for name, detail in report.failures:
        print(f"  FAIL {name}" + (f"：{detail}" if detail else ""))


def main():
    parser = argparse.ArgumentParser(description="核对牵牛花 CSV 与 retail_price_snapshot 入库结果")
    parser.add_argument("csv", nargs="?", help="采集 CSV；传 --manifest 时可省略")
    parser.add_argument("--manifest", help="finalized manifest")
    parser.add_argument("--snapshot-id", help="store_snapshot_id；传 manifest 时默认 capture_id")
    parser.add_argument("--json", action="store_true", help="末行输出机器 JSON")
    args = parser.parse_args()
    manifest_path = os.path.abspath(args.manifest) if args.manifest else None
    manifest = C.read_json(manifest_path) if manifest_path else None
    csv_path = args.csv or ((manifest or {}).get("artifacts") or {}).get("csv_path")
    if csv_path:
        csv_path = (os.path.abspath(csv_path) if os.path.isabs(csv_path)
                    else os.path.abspath(os.path.join(
                        os.path.dirname(manifest_path) if manifest_path else os.getcwd(), csv_path)))
    snapshot_id = args.snapshot_id or (manifest or {}).get("capture_id")
    if not csv_path or not snapshot_id:
        parser.error("需要 CSV + --snapshot-id，或 --manifest")
    with open(C.db_config_path(), encoding="utf-8") as fh:
        cfg = json.load(fh)
    conn = pymysql.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"],
        database=DATABASE, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    try:
        report = validate_snapshot(conn, csv_path, snapshot_id, manifest_path=manifest_path)
    finally:
        conn.close()
    print_report(report)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    if not report.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
