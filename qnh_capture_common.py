#!/usr/bin/env python3
"""Shared, dependency-free contract for the 牵牛花 capture → database pipeline."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROFILE_FILE = os.path.join(ROOT, "config", "qnh_capture_profiles.json")
MANIFEST_SCHEMA_VERSION = 1
SEGMENT_MODE = "leaf_categories"
PROFILE_SCHEMA_VERSION = 1
STORE_CHANNEL = "qnh"
PRICE_CHANNELS = [
    ("meituan", "美团闪购价"),
    ("eleme", "饿了么价"),
    ("jddj", "京东到家价"),
]
CHANNEL_NAMES = ["美团闪购", "饿了么", "京东到家"]
CHANNEL_CARD_PRICES = {
    "美团闪购": "美团闪购价",
    "饿了么": "饿了么价",
    "京东到家": "京东到家价",
}

BASE_COLUMNS = ["序号", "商品名称", "条码", "店内码", "货号", "状态", "视频时间点", "时刻"]
META_COLUMNS = ["采集时间", "证据前缀"]
CARD_COLUMNS = [
    "规格", "月售", "库存", "售罄", "活动品",
    "美团闪购价", "饿了么价", "京东到家价", "采购价", "价格原文",
]
DETAIL_COLUMNS = [
    "开放平台标识", "SPU", "SKU", "重量", "采购价_详情", "采购单位_详情",
    "美团闪购状态", "美团闪购零售价", "美团闪购库存",
    "饿了么状态", "饿了么零售价", "饿了么库存",
    "京东到家状态", "京东到家零售价", "京东到家库存",
    "卖点", "可售日期", "礼袋", "是否力荐", "是否标品",
]
FULL_COLUMNS = BASE_COLUMNS + META_COLUMNS + CARD_COLUMNS + DETAIL_COLUMNS
COMPLETE_ROW_STATUSES = {"成功", "自建商品"}
TERMINAL_MANIFEST_STATES = {"success", "partial", "interrupted", "failed"}
LISTING_STATUS_MAP = {"已上架": "on_shelf", "已下架": "off_shelf", "未上线": "not_online"}
YES_NO_MAP = {"是": 1, "否": 0}
IS_STANDARD_MAP = {"标品": 1, "非标品": 0, "自建": 0, "自建商品": 0}
PROFILE_IDENTITY_FIELDS = (
    "name", "store_name", "warehouse_id", "tenant_id", "store_code", "role",
)
STAMP_PAT = re.compile(r"qnh_scan_(\d{8}-\d{6})")
PURCHASE_PAT = re.compile(r"^(\d+(?:\.\d+)?)(?:/(\S+))?$")


class CaptureContractError(ValueError):
    pass


@dataclass
class GateReport:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    row_count: int = 0
    expected_count: int | None = None
    status_counts: dict[str, int] = field(default_factory=dict)
    detail_complete_count: int = 0
    manifest_sha256: str = ""
    csv_sha256: str = ""
    manifest: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "row_count": self.row_count,
            "expected_count": self.expected_count,
            "status_counts": self.status_counts,
            "detail_complete_count": self.detail_complete_count,
            "manifest_sha256": self.manifest_sha256,
            "csv_sha256": self.csv_sha256,
        }


def text_value(value) -> str:
    return str(value or "").strip()


def amount(text):
    try:
        value = Decimal(text_value(text)) if text_value(text) else None
        return value if value is None or value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def nonnegative_amount(text):
    value = amount(text)
    return value if value is not None and value >= 0 else None


def field_value(text):
    value = text_value(text)
    return None if value in {"", "-"} else value


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", text_value(name))


def listing_identity(name, barcode) -> str:
    barcode = text_value(barcode)
    return "barcode:" + barcode if barcode else "name:" + normalize_name(name) + "|"


def listing_key(name, barcode, seen):
    base = listing_identity(name, barcode)
    seen[base] = seen.get(base, 0) + 1
    locator = base if seen[base] == 1 else f"{base}#{seen[base]}"
    return hashlib.sha256(locator.encode("utf-8")).hexdigest()


def row_identity(index, name, barcode):
    """Stable capture-order key for scanner caches, including duplicate occurrences."""
    return f"{int(index)}|{listing_identity(name, barcode).rstrip('|')}"


def lower_bound_int(text):
    match = re.search(r"\d+", text or "")
    return int(match.group()) if match else None


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_prefix(path):
    return os.path.splitext(os.path.abspath(path))[0]


def row_evidence(path, row):
    value = row.get("证据前缀") or source_prefix(path)
    return value if os.path.isabs(value) else os.path.abspath(os.path.join(os.path.dirname(path), value))


def row_times(path, rows):
    out = []
    for row in rows:
        raw = row.get("采集时间")
        if raw:
            try:
                out.append(datetime.fromisoformat(raw))
                continue
            except ValueError:
                pass
        match = STAMP_PAT.search(os.path.basename(row_evidence(path, row)))
        try:
            day = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").date() if match else None
            out.append(datetime.combine(day, datetime.strptime(row["时刻"], "%H:%M:%S").time())
                       if day else None)
        except (ValueError, KeyError):
            out.append(None)
    return out


def read_csv_rows(path: str) -> tuple[list[dict], list[str]]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    if not rows:
        raise CaptureContractError(f"CSV 为空：{path}")
    return rows, fields


def atomic_write_json(path: str, value: dict, *, overwrite: bool = True) -> str:
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if not overwrite and os.path.exists(path):
        with open(path, "rb") as fh:
            existing = fh.read()
        if existing != data:
            raise CaptureContractError(f"不可变 JSON 已存在且内容不同：{path}")
        return path
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise CaptureContractError(f"JSON 顶层必须是对象：{path}")
    return value


def profile_identity(profile: dict | None) -> dict[str, str]:
    profile = profile if isinstance(profile, dict) else {}
    return {key: text_value(profile.get(key)) for key in PROFILE_IDENTITY_FIELDS}


def profile_mismatches(actual: dict | None, expected: dict | None) -> list[str]:
    left, right = profile_identity(actual), profile_identity(expected)
    return [f"{key}: manifest={left[key]!r} profile={right[key]!r}"
            for key in PROFILE_IDENTITY_FIELDS if left[key] != right[key]]


def load_profiles(path: str = DEFAULT_PROFILE_FILE) -> dict[str, dict]:
    root = read_json(path)
    if root.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise CaptureContractError(f"不支持的 profile schema_version：{root.get('schema_version')}")
    profiles = root.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise CaptureContractError("profile 文件没有 profiles")
    required = set(PROFILE_IDENTITY_FIELDS) - {"name"}
    identities = {}
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise CaptureContractError(f"profile {name} 必须是对象")
        missing = sorted(key for key in required if not text_value(profile.get(key)))
        if missing:
            raise CaptureContractError(f"profile {name} 缺少：{', '.join(missing)}")
        role = profile.get("role")
        if not isinstance(role, str) or role not in {"own", "competitor"}:
            raise CaptureContractError(f"profile {name} role 无效：{role}")
        stable = (text_value(profile["store_code"]), text_value(profile["warehouse_id"]),
                  text_value(profile["tenant_id"]))
        if stable in identities:
            raise CaptureContractError(
                f"profile {name} 与 {identities[stable]} 使用相同门店稳定身份")
        identities[stable] = name
    return profiles


def load_profile(name: str, path: str = DEFAULT_PROFILE_FILE) -> dict:
    profiles = load_profiles(path)
    if name not in profiles:
        raise CaptureContractError(f"未知 profile：{name}；可用：{', '.join(sorted(profiles))}")
    return {"name": name, **profiles[name]}


def status_counts(rows: list[dict]) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get("状态") or "").strip() for row in rows).items()))


def channel_detail_complete(row: dict, channel_name: str) -> bool:
    state = text_value(row.get(channel_name + "状态"))
    if state not in LISTING_STATUS_MAP:
        return False
    if state == "已上架":
        detail_price = nonnegative_amount(row.get(channel_name + "零售价"))
        card_price = nonnegative_amount(row.get(CHANNEL_CARD_PRICES[channel_name]))
        detail_stock = text_value(row.get(channel_name + "库存"))
        list_stock = text_value(row.get("库存"))
        detail_stock_ok = (detail_stock in {"售罄", "已售罄"}
                           or nonnegative_amount(detail_stock) is not None)
        list_stock_ok = (text_value(row.get("售罄")) == "是"
                         or nonnegative_amount(list_stock) is not None)
        # ponytail(qnh-channel-list-fallback): App 偶尔只渲染已上架区块标题，
        # 不挂载该渠道的详情表格行；列表卡仍有同渠道价格和库存。加载器本来
        # 就按相同优先级回退，这里保持门禁一致。upgrade when App 修复详情行。
        return ((detail_price is not None or card_price is not None)
                and (detail_stock_ok or list_stock_ok))
    return True


# ponytail(gstack-shortcut dec-open-platform-optional): 开放平台标识/货号在部分商品
# 详情页天生缺失（如荷氏 6924513906175，真机 2026-10-10 验证 App 无此字段），
# 门禁按可空放行；upgrade when App 端补齐字段或需要强校验 open_platform_id。
OPTIONAL_DETAIL_KEYS = ("开放平台标识",)


def row_detail_errors(row: dict) -> list[str]:
    missing = []
    for key in ("开放平台标识", "SPU", "SKU", "采购单位_详情",
                "可售日期", "礼袋"):
        if key in OPTIONAL_DETAIL_KEYS:
            continue
        if not field_value(row.get(key)):
            missing.append(key)
    recommend = text_value(row.get("是否力荐"))
    # ponytail(qnh-optional-recommend-flag): 部分商品详情页长期不渲染“是否力荐”；
    # 空值保留为数据库 NULL，非空值仍严格限定枚举。upgrade when App 提供稳定字段。
    if recommend and recommend not in YES_NO_MAP:
        missing.append("是否力荐")
    standard = text_value(row.get("是否标品"))
    # ponytail(qnh-optional-standard-flag): 部分商品详情页长期不渲染“是否标品”；
    # 空值保留为数据库 NULL，非空值仍严格限定枚举。upgrade when App 提供稳定字段。
    if standard and standard not in IS_STANDARD_MAP:
        missing.append("是否标品")
    purchase_price = field_value(row.get("采购价_详情"))
    if purchase_price is not None and nonnegative_amount(purchase_price) is None:
        missing.append("采购价_详情格式")
    for channel in CHANNEL_NAMES:
        if not channel_detail_complete(row, channel):
            missing.append(channel + "详情")
    return missing


def _first_not_none(*values):
    return next((value for value in values if value is not None), None)


def fact_fields_from_csv(source: dict, channel: str, price_column: str,
                         csv_path: str) -> dict:
    """Normalize every fact-table field derived only from one CSV row."""
    purchase = text_value(source.get("采购价"))
    purchase_match = PURCHASE_PAT.match(purchase)
    sold_out = text_value(source.get("售罄")) == "是"
    stock = text_value(source.get("库存"))
    detail_price = text_value(source.get(channel + "零售价"))
    detail_stock = text_value(source.get(channel + "库存"))
    detail_status = text_value(source.get(channel + "状态"))
    list_stock = nonnegative_amount(stock) if stock else (Decimal(0) if sold_out else None)
    if detail_stock:
        channel_stock = (Decimal(0) if detail_stock in {"售罄", "已售罄"}
                         else nonnegative_amount(detail_stock))
    else:
        channel_stock = list_stock
    detail_purchase = field_value(source.get("采购价_详情"))
    purchase_amount = _first_not_none(
        nonnegative_amount(detail_purchase),
        nonnegative_amount(purchase_match.group(1)) if purchase_match else None)
    detail_unit = field_value(source.get("采购单位_详情"))
    purchase_unit = detail_unit or (purchase_match.group(2) if purchase_match else None)
    month = text_value(source.get("月售"))
    card_price = text_value(source.get(price_column))
    channel_sold_out = sold_out or detail_stock in {"售罄", "已售罄"}
    sale_status = "out_of_stock" if channel_sold_out else (
        "available" if text_value(source.get("售罄")) in {"是", "否"} else "unknown")
    return {
        "product_name": text_value(source.get("商品名称")),
        "spec_name": field_value(source.get("规格")),
        "upc": field_value(source.get("条码")),
        "store_inner_code": field_value(source.get("店内码")),
        "article_no": field_value(source.get("货号")),
        "open_platform_id": field_value(source.get("开放平台标识")),
        "source_spu_id": field_value(source.get("SPU")),
        "source_sku_id": field_value(source.get("SKU")),
        "weight_text": field_value(source.get("重量")),
        "front_display_price_amount": _first_not_none(
            nonnegative_amount(detail_price), nonnegative_amount(card_price)),
        "purchase_price_amount": purchase_amount,
        "purchase_price_unit": purchase_unit,
        "purchase_price_text": detail_purchase or field_value(purchase),
        "month_sales_text": f"月售{month}" if month else None,
        "month_sales_qty": lower_bound_int(month),
        "sale_status": sale_status,
        "stock_text": ("已售罄" if channel_sold_out else field_value(detail_stock or stock)),
        "stock_quantity": channel_stock,
        "is_promo_item": YES_NO_MAP.get(text_value(source.get("活动品"))),
        "listing_status": LISTING_STATUS_MAP.get(detail_status),
        "selling_points": field_value(source.get("卖点")),
        "sellable_date_text": field_value(source.get("可售日期")),
        "gift_bag_text": field_value(source.get("礼袋")),
        "is_recommended": YES_NO_MAP.get(text_value(source.get("是否力荐"))),
        "is_standard": IS_STANDARD_MAP.get(text_value(source.get("是否标品"))),
        "other_text": field_value(source.get("价格原文")) if not card_price else None,
        "evidence_uri": row_evidence(csv_path, source),
        "evidence_video_time": field_value(source.get("视频时间点")),
    }


def row_category_map(manifest: dict, row_count: int) -> list[tuple[str | None, str | None]]:
    """Return one (L1,L2) category tuple per final CSV row."""
    scope = manifest.get("scope") or {}
    segments = scope.get("segments")
    if not segments:
        category = scope.get("category")
        parts = (category.split("/", 1) + [None])[:2] if category else (None, None)
        return [tuple(parts)] * row_count
    result = [(None, None)] * row_count
    for segment in segments:
        top, leaf = segment.get("top"), segment.get("leaf")
        start, end = segment.get("row_start"), segment.get("row_end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        sub = leaf if leaf != top else None
        for index in range(max(1, start), min(row_count, end) + 1):
            result[index - 1] = (top, sub)
    return result


def validate_segments(scope: dict, row_count: int, report: GateReport, *, require_complete: bool):
    segments = scope.get("segments")
    mode = scope.get("mode")
    if mode != SEGMENT_MODE and segments is None:
        return
    if mode != SEGMENT_MODE:
        report.fail(f"manifest scope.mode 无效：{mode!r}")
    if not isinstance(segments, list) or not segments:
        report.fail("manifest scope.segments 必须是非空数组")
        return
    completed_raw = 0
    covered_rows = []
    seen_paths = set()
    for position, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            report.fail(f"segment {position} 必须是对象")
            continue
        top, leaf = segment.get("top"), segment.get("leaf")
        expected = segment.get("expected_count")
        captured = segment.get("captured_count")
        unique = segment.get("unique_count")
        state = segment.get("state")
        if not all(isinstance(value, str) and value.strip() for value in (top, leaf)):
            report.fail(f"segment {position} 类目路径无效")
        path = (top, leaf)
        if path in seen_paths:
            report.fail(f"segment 重复类目：{top}/{leaf}")
        seen_paths.add(path)
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            report.fail(f"segment {top}/{leaf} expected_count 无效：{expected!r}")
        if not isinstance(captured, int) or isinstance(captured, bool) or captured < 0:
            report.fail(f"segment {top}/{leaf} captured_count 无效：{captured!r}")
        if not isinstance(unique, int) or isinstance(unique, bool) or unique < 0 or (
                isinstance(captured, int) and unique > captured):
            report.fail(f"segment {top}/{leaf} unique_count 无效：{unique!r}")
        if state not in {"pending", "running", "success", "partial", "failed"}:
            report.fail(f"segment {top}/{leaf} state 无效：{state!r}")
        if state == "success":
            if captured != expected:
                report.fail(f"segment {top}/{leaf} 数量不完整：{captured}/{expected}")
            completed_raw += captured if isinstance(captured, int) else 0
        elif require_complete:
            report.fail(f"segment 未完成：{top}/{leaf}={state}")
        start, end = segment.get("row_start"), segment.get("row_end")
        if unique == 0:
            if start is not None or end is not None:
                report.fail(f"空 segment 不应声明行范围：{top}/{leaf}")
        elif isinstance(start, int) and isinstance(end, int):
            if end - start + 1 != unique or start < 1 or end > row_count:
                report.fail(f"segment {top}/{leaf} 行范围与 unique_count 不一致")
            covered_rows.extend(range(start, end + 1))
        else:
            report.fail(f"segment {top}/{leaf} 缺少行范围")
    if sorted(covered_rows) != list(range(1, row_count + 1)):
        report.fail("scope.segments 行范围未连续覆盖最终 CSV")
    raw_expected = scope.get("raw_segment_product_count")
    if raw_expected != completed_raw:
        report.fail(f"raw_segment_product_count={raw_expected}，成功分片合计={completed_raw}")
    duplicates = scope.get("cross_category_duplicate_count")
    if not isinstance(duplicates, int) or duplicates < 0:
        report.fail("cross_category_duplicate_count 必须是非负整数")
    elif completed_raw - row_count != duplicates:
        report.fail(f"跨类目去重数不一致：raw {completed_raw} - unique {row_count} != {duplicates}")


def build_manifest(*, capture_id: str, batch_id: str, run_id: str, state: str,
                   profile: dict | None, device_id: str | None, scope: dict,
                   capture: dict, artifacts: dict, rows: list[dict],
                   stop_reason: str = "", parent_manifest_sha256: str | None = None) -> dict:
    counts = status_counts(rows)
    detail_complete = sum(not row_detail_errors(row) for row in rows)
    artifacts = dict(artifacts)
    csv_path = text_value(artifacts.get("csv_path"))
    if csv_path:
        artifacts["row_evidence_prefixes"] = sorted({row_evidence(csv_path, row) for row in rows})
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "capture_id": capture_id,
        "batch_id": batch_id,
        "run_id": run_id,
        "parent_manifest_sha256": parent_manifest_sha256,
        "state": state,
        "profile": profile,
        "device_id": device_id,
        "scope": scope,
        "capture": capture,
        "artifacts": artifacts,
        "progress": {
            "row_count": len(rows),
            "status_counts": counts,
            "detail_complete_count": detail_complete,
            "stop_reason": stop_reason,
        },
    }


def _resolve_path(value, base_dir: str) -> str:
    value = text_value(value)
    if not value:
        return ""
    return os.path.abspath(value if os.path.isabs(value) else os.path.join(base_dir, value))


def _manifest_datetime(value, field_name: str, report: GateReport):
    value = text_value(value)
    if not value:
        report.fail(f"manifest {field_name} 为空")
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        report.fail(f"manifest {field_name} 不是 ISO 时间：{value}")
        return None


def _uuid_valid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def validate_manifest(path: str, *, require_complete: bool = True,
                      expected_profile: dict | None = None) -> GateReport:
    report = GateReport()
    path = os.path.abspath(path)
    manifest_dir = os.path.dirname(path)
    try:
        manifest = read_json(path)
        report.manifest_sha256 = sha256_file(path)
    except Exception as exc:
        report.fail(str(exc))
        return report
    report.manifest = manifest
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        report.fail(f"manifest schema_version 不支持：{manifest.get('schema_version')}")

    nested = {}
    for key in ("profile", "scope", "capture", "artifacts", "progress"):
        value = manifest.get(key)
        if not isinstance(value, dict):
            report.fail(f"manifest {key} 必须是对象")
            value = {}
        nested[key] = value
    for key in ("capture_id", "batch_id", "run_id", "state"):
        if key not in manifest:
            report.fail(f"manifest 缺少 {key}")
    for key in ("capture_id", "batch_id", "run_id"):
        if not _uuid_valid(manifest.get(key)):
            report.fail(f"manifest {key} 不是 UUID")
    parent_hash = manifest.get("parent_manifest_sha256")
    if parent_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", str(parent_hash)):
        report.fail("manifest parent_manifest_sha256 无效")

    profile = nested["profile"]
    for key in PROFILE_IDENTITY_FIELDS:
        value = profile.get(key)
        if not isinstance(value, str) or not value.strip():
            report.fail(f"manifest profile {key} 必须是非空字符串")
    role = profile.get("role")
    if not isinstance(role, str) or role not in {"own", "competitor"}:
        report.fail(f"manifest profile role 无效：{role}")
    if expected_profile:
        mismatches = profile_mismatches(profile, expected_profile)
        if mismatches:
            report.fail("manifest 与指定 profile 不一致：" + "；".join(mismatches))

    state = manifest.get("state")
    if not isinstance(state, str) or state not in TERMINAL_MANIFEST_STATES | {"running"}:
        report.fail(f"manifest state 无效：{state}")
    if require_complete and state != "success":
        report.fail(f"manifest 不是 success：{state}")

    scope = nested["scope"]
    capture = nested["capture"]
    artifacts = nested["artifacts"]
    progress = nested["progress"]
    device_id = manifest.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        report.fail("manifest device_id 必须是非空字符串")
    list_title = scope.get("list_title")
    if not isinstance(list_title, str) or not list_title.strip():
        report.fail("manifest scope.list_title 必须是非空字符串")
    if scope.get("category") is not None and not isinstance(scope.get("category"), str):
        report.fail("manifest scope.category 必须是字符串或 null")
    expected = scope.get("expected_product_count")
    valid_expected = isinstance(expected, int) and not isinstance(expected, bool) and expected > 0
    if not valid_expected:
        report.fail(f"manifest expected_product_count 必须是正整数：{expected!r}")
        expected = None
    report.expected_count = expected
    requested = scope.get("requested_count")
    if not (isinstance(requested, int) and not isinstance(requested, bool) and requested >= 0):
        report.fail(f"manifest requested_count 必须是非负整数：{requested!r}")
    if capture.get("method") != "app_ui":
        report.fail(f"manifest capture.method 无效：{capture.get('method')}")
    started_at = _manifest_datetime(capture.get("started_at"), "capture.started_at", report)
    run_started_at = _manifest_datetime(
        capture.get("run_started_at"), "capture.run_started_at", report)
    finished_at = _manifest_datetime(capture.get("finished_at"), "capture.finished_at", report)
    if started_at and finished_at:
        try:
            if finished_at < started_at:
                report.fail("manifest capture.finished_at 早于 started_at")
            if run_started_at and not started_at <= run_started_at <= finished_at:
                report.fail("manifest capture.run_started_at 不在 capture 时间范围内")
        except TypeError:
            report.fail("manifest capture 时间的时区格式不一致")
    collector = capture.get("collector_version")
    if not isinstance(collector, str) or not collector.strip():
        report.fail("manifest capture.collector_version 必须是非空字符串")
    pace = capture.get("pace")
    if not isinstance(pace, str) or pace not in {"ultra", "fast", "natural", "demo"}:
        report.fail(f"manifest capture.pace 无效：{pace!r}")
    for key in ("verify_every", "checkpoint_every"):
        value = capture.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            report.fail(f"manifest capture.{key} 必须是非负整数：{value!r}")
    for key in ("tall_screen", "record"):
        if not isinstance(capture.get(key), bool):
            report.fail(f"manifest capture.{key} 必须是布尔值：{capture.get(key)!r}")
    if "dismiss_order_dialogs" in capture and not isinstance(capture.get("dismiss_order_dialogs"), bool):
        report.fail("manifest capture.dismiss_order_dialogs 必须是布尔值")
    # 可选观测字段：旧 manifest 无此键放行；存在则必须形状正确。
    if "helper" in capture:
        helper = capture.get("helper")
        if not isinstance(helper, dict):
            report.fail("manifest capture.helper 必须是对象")
        else:
            for key in ("ready", "use_for_dump", "has_any_text_query",
                        "has_semantic_scroll"):
                if key in helper and not isinstance(helper[key], bool):
                    report.fail(f"manifest capture.helper.{key} 必须是布尔值")
            if "version" in helper and (not isinstance(helper["version"], int)
                                        or isinstance(helper["version"], bool)
                                        or helper["version"] < 0):
                report.fail("manifest capture.helper.version 必须是非负整数")

    for key in ("csv_path", "evidence_prefix"):
        value = artifacts.get(key)
        if not isinstance(value, str) or not value.strip():
            report.fail(f"manifest artifacts.{key} 必须是非空字符串")
    if artifacts.get("video_path") is not None and not isinstance(artifacts.get("video_path"), str):
        report.fail("manifest artifacts.video_path 必须是字符串或 null")
    csv_path = _resolve_path(artifacts.get("csv_path"), manifest_dir)
    artifacts["csv_path"] = csv_path
    if not csv_path or not os.path.isfile(csv_path):
        report.fail(f"CSV 不存在：{csv_path}")
        return report
    artifact_prefix = _resolve_path(artifacts.get("evidence_prefix"), manifest_dir)
    if not artifact_prefix:
        report.fail("manifest artifacts.evidence_prefix 为空")
    elif artifact_prefix != source_prefix(csv_path):
        report.fail(f"manifest evidence_prefix 与 CSV 不一致：{artifact_prefix}")
    video_path = _resolve_path(artifacts.get("video_path"), manifest_dir)
    if video_path and not os.path.isfile(video_path):
        report.fail(f"录像不存在：{video_path}")
    if video_path:
        artifacts["video_path"] = video_path
    try:
        rows, fields = read_csv_rows(csv_path)
    except Exception as exc:
        report.fail(str(exc))
        return report
    report.rows = rows
    report.row_count = len(rows)
    validate_segments(scope, len(rows), report, require_complete=require_complete)
    if len(fields) != len(set(fields)):
        report.fail("CSV 表头存在重复列")
    if fields != FULL_COLUMNS:
        missing_columns = [column for column in FULL_COLUMNS if column not in fields]
        extra_columns = [column for column in fields if column not in FULL_COLUMNS]
        if missing_columns:
            report.fail("CSV 缺列：" + "、".join(missing_columns))
        if extra_columns:
            report.fail("CSV 多出未知列：" + "、".join(extra_columns))
        if not missing_columns and not extra_columns:
            report.fail("CSV 列顺序与 40 列契约不一致")
    for index, row in enumerate(rows, 1):
        extra_values = row.get(None)
        missing_values = [field for field in fields if row.get(field) is None]
        if extra_values or missing_values:
            details = []
            if extra_values:
                details.append(f"多出 {len(extra_values)} 个字段")
            if missing_values:
                details.append("缺少 " + "、".join(missing_values[:10]))
            report.fail(f"CSV 第{index}行格式错误：{'；'.join(details)}")
    report.status_counts = status_counts(rows)
    if progress.get("row_count") != len(rows):
        report.fail(f"manifest row_count={progress.get('row_count')}，CSV={len(rows)}")
    if progress.get("status_counts") != report.status_counts:
        report.fail("manifest status_counts 与 CSV 不一致")
    if expected is not None and len(rows) > expected:
        report.fail(f"商品数超过平台总数：CSV {len(rows)} / 平台 {expected}")
    elif expected is not None and require_complete and len(rows) != expected:
        report.fail(f"商品数不完整：CSV {len(rows)} / 平台 {expected}")

    sequences = []
    for row in rows:
        try:
            sequences.append(int(row.get("序号", "")))
        except (ValueError, TypeError):
            report.fail(f"序号无效：{row.get('序号')}")
            break
    if sequences and sequences != list(range(1, len(rows) + 1)):
        report.fail("CSV 序号不连续或顺序错误")

    bad_statuses = {key: value for key, value in report.status_counts.items()
                    if key not in COMPLETE_ROW_STATUSES}
    if require_complete and bad_statuses:
        report.fail("存在不完整状态：" + "、".join(
            f"{key or '<空>'}{value}个" for key, value in bad_statuses.items()))

    detail_errors = []
    blank_name_counts = Counter()
    evidence_prefixes = set()
    row_datetimes = []
    for i, row in enumerate(rows, 1):
        name = normalize_name(row.get("商品名称"))
        barcode = text_value(row.get("条码"))
        if not name:
            report.fail(f"第{i}行缺少商品名称")
        if not barcode:
            blank_name_counts[name] += 1
        raw_status = str(row.get("状态") or "")
        if raw_status != raw_status.strip():
            report.fail(f"第{i}行状态含首尾空白")
        for channel in CHANNEL_NAMES:
            raw_channel_status = str(row.get(channel + "状态") or "")
            if raw_channel_status != raw_channel_status.strip():
                report.fail(f"第{i}行{channel}状态含首尾空白")
        errs = row_detail_errors(row)
        if errs:
            detail_errors.append(f"第{i}行({name[:20]})缺少{'/'.join(errs)}")

        raw_time = text_value(row.get("采集时间"))
        if not raw_time:
            report.fail(f"第{i}行缺少采集时间")
        else:
            try:
                row_time = datetime.fromisoformat(raw_time)
                row_datetimes.append(row_time)
                if started_at and finished_at:
                    try:
                        if not started_at <= row_time <= finished_at:
                            report.fail(f"第{i}行采集时间不在 capture 时间范围内")
                    except TypeError:
                        report.fail(f"第{i}行采集时间与 capture 时区格式不一致")
            except ValueError:
                report.fail(f"第{i}行采集时间不是 ISO 时间：{raw_time}")

        prefix = field_value(row.get("证据前缀"))
        if not prefix:
            report.fail(f"第{i}行缺少证据前缀")
        else:
            evidence_prefixes.add(row_evidence(csv_path, row))
    report.detail_complete_count = len(rows) - len(detail_errors)
    if progress.get("detail_complete_count") != report.detail_complete_count:
        report.fail("manifest detail_complete_count 与 CSV 不一致")
    if require_complete and detail_errors:
        report.fail("详情字段不完整：" + "；".join(detail_errors[:10]))
    ambiguous = [name for name, count in blank_name_counts.items() if name and count > 1]
    if ambiguous:
        message = "无条码同名商品无法稳定定位：" + "、".join(ambiguous[:10])
        if require_complete:
            report.fail(message)
        else:
            report.warnings.append(message + "；续跑时将合并后重做")

    declared_prefixes = artifacts.get("row_evidence_prefixes")
    if not isinstance(declared_prefixes, list):
        report.fail("manifest artifacts.row_evidence_prefixes 必须是数组")
    else:
        if any(not isinstance(item, str) or not item.strip() for item in declared_prefixes):
            report.fail("manifest row_evidence_prefixes 只能包含非空字符串")
        normalized_declared = {_resolve_path(item, manifest_dir) for item in declared_prefixes
                               if isinstance(item, str) and item.strip()}
        if len(normalized_declared) != len(declared_prefixes):
            report.fail("manifest row_evidence_prefixes 含空值或重复项")
        if normalized_declared != evidence_prefixes:
            report.fail("manifest row_evidence_prefixes 与 CSV 行证据不一致")
    for prefix in sorted(evidence_prefixes):
        if not os.path.isfile(prefix + ".csv"):
            report.fail(f"行证据 CSV 不存在：{prefix}.csv")
            if len(report.errors) >= 20:
                break

    actual_hash = sha256_file(csv_path)
    report.csv_sha256 = actual_hash
    expected_hash = text_value(artifacts.get("csv_sha256"))
    if isinstance(state, str) and state in TERMINAL_MANIFEST_STATES and not expected_hash:
        report.fail("terminal manifest 缺少 csv_sha256")
    elif expected_hash and not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        report.fail("manifest csv_sha256 格式无效")
    elif expected_hash and actual_hash != expected_hash:
        report.fail("CSV SHA-256 与 manifest 不一致")
    return report


def format_gate_report(report: GateReport) -> str:
    lines = [f"manifest gate: {'PASS' if report.ok else 'FAIL'}",
             f"rows={report.row_count} expected={report.expected_count} "
             f"details={report.detail_complete_count} statuses={report.status_counts}"]
    lines += ["ERROR: " + item for item in report.errors]
    lines += ["WARN: " + item for item in report.warnings]
    return "\n".join(lines)
