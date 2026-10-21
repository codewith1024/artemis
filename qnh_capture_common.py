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
DEFAULT_DB_CONFIG_FILES = (
    os.path.join(ROOT, ".qnh_db_config.json"),
    os.path.expanduser("~/.claude/skills/qnh-database/db_config.json"),
)
MANIFEST_SCHEMA_VERSION = 1
SEGMENT_MODE = "leaf_categories"
PROFILE_SCHEMA_VERSION = 1
STORE_CHANNEL = "qnh"
# 渠道编码 → (CSV 中文渠道名, 列表卡价格列)。渠道键必须显式映射：英文渠道码
# 拼中文列名（"meituan零售价"）取不到任何值，渠道详情列会整体丢失。
CHANNEL_VIEW = {
    "meituan": ("美团闪购", "美团闪购价"),
    "eleme": ("饿了么", "饿了么价"),
    "jddj": ("京东到家", "京东到家价"),
}
PRICE_CHANNELS = [(code, view[1]) for code, view in CHANNEL_VIEW.items()]
CHANNEL_NAMES = [view[0] for view in CHANNEL_VIEW.values()]
CHANNEL_CARD_PRICES = {view[0]: view[1] for view in CHANNEL_VIEW.values()}

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
# 多 SKU 卡片：一个 SPU 挂多个 SKU，卡级 40 列契约装不下每个 SKU 的详情。
# 卡片行只保留列表卡级字段，状态标「多SKU」，SKU 级列全空；每个 SKU 的完整
# 40 列写进本地 sidecar（artifacts.sku_path 指向的 .skus.json）。capture-only：
# 入库前拒绝，只做本地校验。
MULTISKU_STATUS = "多SKU"
SKU_SIDECAR_MODE = "sidecar_v1"
SKU_SIDECAR_SCHEMA_VERSION = 1
# sidecar 里单个 SKU 的合法状态（子行不再嵌套多 SKU）
SKU_CHILD_STATUSES = {"成功", "自建商品"}
# 多 SKU 卡片行禁止携带的列：SKU 级字段，塞进去就是拿代表 SKU 冒充整卡。
MULTISKU_CARD_FORBIDDEN_COLUMNS = tuple(DETAIL_COLUMNS)
COMPLETE_ROW_STATUSES = {"成功", "自建商品", "携带", MULTISKU_STATUS}
# 携带行的详情列来自上一轮基线快照（历史合成值），不是当日详情页真值。
# 入库会把历史推导值当当日字段，加载器必须拒绝，强制重新实访详情页。
CARRY_ROW_STATUS = "携带"
# ── 本地异常（观测）契约 ──────────────────────────────────────────────
# 平台标题数与列表可见卡片数对不上、或某渠道在详情页整行缺失时，这次采集不能
# 声称 success。但也不许改数：缺就记「未知」，把原始证据（列表页 XML、详情页
# XML）挂进 manifest，状态落 observed_with_exceptions（终态、非 success）。
# 只有带 v1 观测契约的例外态才允许「未知」通过本地门禁；普通 success 里的未知
# 依旧失败——声称成功就不该有未知。
OBSERVATION_CONTRACT_VERSION = "v1"
OBSERVED_MANIFEST_STATE = "observed_with_exceptions"
CHANNEL_UNKNOWN_STATUS = "未知"
OBSERVATION_ROW_ABSENT = "row_absent"
# platform_count_mismatch 是分片级例外（该分片的平台标题数 vs 可见卡数）。
# 聚合级例外用 segment_platform_count_mismatch：聚合的 expected_product_count
# 是去重后的时间窗并集（恒等于行数），拿它当「平台标题数」会把分片级的
# 计数差抹平——所以聚合另记一条带类目路径的分片计数差。
OBSERVATION_EXCEPTION_CODES = {"channel_row_absent", "platform_count_mismatch",
                               "segment_platform_count_mismatch"}
LIST_EVIDENCE_SCHEMA_VERSION = 1
# 详情页页底标记：渠道表读到这里的「其他信息」才算读到底，缺它就无法区分
# 「这一 SKU 真没有行」和「这一页还没滑到底」。
DETAIL_PAGE_FOOTER = "其他信息"
TERMINAL_MANIFEST_STATES = {"success", "partial", "interrupted", "failed",
                             OBSERVED_MANIFEST_STATE}
# 聚合覆盖口径：分片跨时段采集，聚合行数是「时间窗并集」，不是某一时点的全店
# 快照。用它自己证明完整＝自证。唯一能证伪的是聚合时从 App「全部商品」页读到
# 的实时全店计数：两者相等＝按数量对账通过（count_reconciled_time_window），
# 仍不代表同一时点的全店快照；不等＝覆盖无法证明，拒绝完整入库。
FULL_STORE_COVERAGE_MODE = "count_reconciled_time_window"
FULL_STORE_COVERAGE_STATUSES = {"verified", "unverified"}
LISTING_STATUS_MAP = {"已上架": "on_shelf", "已下架": "off_shelf", "未上线": "not_online"}
YES_NO_MAP = {"是": 1, "否": 0}
IS_STANDARD_MAP = {"标品": 1, "非标品": 0, "自建": 0, "自建商品": 0}
PROFILE_IDENTITY_FIELDS = (
    "name", "store_name", "warehouse_id", "tenant_id", "store_code", "role",
)
STAMP_PAT = re.compile(r"qnh_scan_(\d{8}-\d{6})")
FULL_ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
PURCHASE_PAT = re.compile(r"^(\d+(?:\.\d+)?)(?:/(\S+))?$")


class CaptureContractError(ValueError):
    pass


def db_config_path():
    configured = os.environ.get("QNH_DB_CONFIG")
    candidates = (os.path.expanduser(configured),) if configured else DEFAULT_DB_CONFIG_FILES
    for path in candidates:
        if os.path.isfile(path):
            return path
    raise FileNotFoundError("数据库配置不存在：" + " 或 ".join(candidates))


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
    # 观测例外：被凭据支撑的「渠道行缺失」条数与明细（不算 detail_complete）。
    observation_count: int = 0
    observed_unknowns: list = field(default_factory=list)
    observations: dict = field(default_factory=dict)
    # sidecar 的多 SKU 子行（卡片序号 -> 子行）：核对 manifest 例外与凭据时用。
    listings: dict = field(default_factory=dict)
    # 子行全干净的多 SKU 卡片序号：多 SKU 卡的完整性看子行，不看父卡行。
    multisku_complete_cards: set = field(default_factory=set)

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
            "observation_count": self.observation_count,
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


def listing_identity(name, barcode, source_sku_id=None, source_spu_id=None) -> str:
    """Stable listing locator, preserving the established barcode-first contract.

    QNH rows historically key barcode-bearing products by barcode; changing those to
    SKU would fork every product's history. Only genuinely barcode-less rows fall back
    to the detail-page SKU/SPU before the last-resort normalized name.
    """
    barcode = text_value(barcode)
    source_sku_id = text_value(source_sku_id)
    source_spu_id = text_value(source_spu_id)
    if barcode:
        return "barcode:" + barcode
    if source_sku_id:
        return "sku:" + source_sku_id
    if source_spu_id:
        return "spu:" + source_spu_id
    return "name:" + normalize_name(name) + "|"


def listing_key(name, barcode, seen, source_sku_id=None, source_spu_id=None):
    base = listing_identity(name, barcode, source_sku_id, source_spu_id)
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


def is_multisku_row(row: dict) -> bool:
    return text_value(row.get("状态")) == MULTISKU_STATUS


def row_detail_errors(row: dict) -> list[str]:
    if is_multisku_row(row):
        # 多 SKU 卡片行没有自己的 SKU 级详情（详情在 sidecar 里逐 SKU 成行）：
        # 这里放行，完整性由 sidecar 门禁逐 SKU 校验，不靠状态字段自证。
        return []
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
    if purchase_price not in (None, "无数据") and nonnegative_amount(purchase_price) is None:
        missing.append("采购价_详情格式")
    for channel in CHANNEL_NAMES:
        if not channel_detail_complete(row, channel):
            missing.append(channel + "详情")
    return missing


def _first_not_none(*values):
    return next((value for value in values if value is not None), None)


def channel_view(channel: str) -> tuple[str, str]:
    """渠道键 → (CSV 中文渠道名, 列表卡价格列)。未知渠道直接拒绝。

    渠道键可以是英文编码（meituan/eleme/jddj）或中文渠道名，两者指向同一份
    CSV 列名映射。
    """
    key = text_value(channel)
    if key in CHANNEL_VIEW:
        return CHANNEL_VIEW[key]
    for view in CHANNEL_VIEW.values():
        if view[0] == key:
            return view
    raise CaptureContractError(
        f"未知渠道：{channel!r}；可用：{'、'.join(CHANNEL_VIEW)}")


def sidecar_listings(manifest):
    """manifest 指向的 sidecar 里的多 SKU 子行（卡片序号 -> 子行）。

    sidecar 的存在、哈希、结构已由 manifest 门禁校验过：这里只取数据。
    """
    path = (manifest.get("artifacts") or {}).get("sku_path")
    if not path:
        return {}
    sidecar = read_json(path)
    return sidecar.get("listings") or {}


def fact_sources(rows, listings):
    """把卡片行展开成入库用的 SKU 行。

    多 SKU 卡片行本身没有 SKU 级字段（40 列契约装不下），入库按每个 SKU 一行：
    sidecar 的子行才是这个 SKU 的真值。卡片行不入库——它没有 SKU 身份，
    入库就是拿一个代表 SKU 冒充整张卡。

    返回 [(行, 该 SPU 的 SKU 数, 是否多 SKU 卡, 卡片下标)]，顺序与 CSV 行一致。
    加载器与校验器共用这一份展开：两处各判各的，校验就成了自证。
    """
    sources = []
    for index, row in enumerate(rows):
        if not is_multisku_row(row):
            sources.append((row, 1, False, index))
            continue
        children = (listings or {}).get(text_value(row.get("序号"))) or []
        if len(children) < 2:
            raise CaptureContractError(
                f"多SKU卡片 序号{text_value(row.get('序号'))} 的 sidecar 子行缺失"
                "（门禁应已拦截，这里是防御）")
        for child in children:
            sources.append((child, len(children), True, index))
    return sources


def fact_fields_from_csv(source: dict, channel: str, price_column: str,
                         csv_path: str) -> dict:
    """Normalize every fact-table field derived only from one CSV row."""
    if text_value(source.get("状态")) == CARRY_ROW_STATUS:
        raise CaptureContractError(
            "携带行的渠道详情是历史合成值，不是当日真值，禁止入库：请重新实访详情页")
    channel_name, expected_price_column = channel_view(channel)
    if text_value(price_column) != expected_price_column:
        raise CaptureContractError(
            f"渠道 {channel_name} 的卡片价格列应为 {expected_price_column}，"
            f"实得 {price_column!r}")
    purchase = text_value(source.get("采购价"))
    purchase_match = PURCHASE_PAT.match(purchase)
    sold_out = text_value(source.get("售罄")) == "是"
    stock = text_value(source.get("库存"))
    detail_price = text_value(source.get(channel_name + "零售价"))
    detail_stock = text_value(source.get(channel_name + "库存"))
    detail_status = text_value(source.get(channel_name + "状态"))
    if detail_stock:
        detail_stock_amount = (Decimal(0) if detail_stock in {"售罄", "已售罄"}
                                else nonnegative_amount(detail_stock))
    else:
        detail_stock_amount = None
    detail_purchase = field_value(source.get("采购价_详情"))
    purchase_amount = (None if detail_purchase == "无数据" else _first_not_none(
        nonnegative_amount(detail_purchase),
        nonnegative_amount(purchase_match.group(1)) if purchase_match else None))
    detail_unit = field_value(source.get("采购单位_详情"))
    purchase_unit = detail_unit or (purchase_match.group(2) if purchase_match else None)
    month = text_value(source.get("月售"))
    card_price = text_value(source.get(price_column))
    listing_status = LISTING_STATUS_MAP.get(detail_status)
    # 价格口径：on_shelf=渠道在售，零售价缺失时回退同渠道卡片价；已下架/未上线
    # 时不拿列表值冒充渠道详情。
    if listing_status == "on_shelf":
        front_price = _first_not_none(
            nonnegative_amount(detail_price), nonnegative_amount(card_price))
    else:
        front_price = nonnegative_amount(detail_price)
    # 库存/售罄口径：渠道有自己的库存就完全按渠道库存判（0=售罄，>0=可售），
    # 列表售罄不能盖掉渠道的正库存（否则「已售罄 + 正数」自相矛盾）；只有在售
    # 渠道缺自己的库存时才回退列表售罄；已下架/未上线又没有渠道库存 → unknown，
    # 不按列表赋可售。
    if detail_stock_amount is not None:
        channel_sold_out = detail_stock_amount == 0
        channel_stock = detail_stock_amount
        sale_status = "out_of_stock" if channel_sold_out else "available"
        stock_text = "已售罄" if channel_sold_out else field_value(detail_stock)
    elif listing_status == "on_shelf":
        channel_sold_out = sold_out
        channel_stock = (Decimal(0) if sold_out
                          else (nonnegative_amount(stock) if stock else None))
        sale_status = "out_of_stock" if sold_out else (
            "available" if text_value(source.get("售罄")) in {"是", "否"} else "unknown")
        stock_text = "已售罄" if sold_out else field_value(stock)
    else:
        channel_sold_out = False
        channel_stock = None
        sale_status = "unknown"
        stock_text = None
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
        "front_display_price_amount": front_price,
        "purchase_price_amount": purchase_amount,
        "purchase_price_unit": purchase_unit,
        "purchase_price_text": detail_purchase or field_value(purchase),
        "month_sales_text": f"月售{month}" if month else None,
        "month_sales_qty": lower_bound_int(month),
        "sale_status": sale_status,
        "stock_text": stock_text,
        "stock_quantity": channel_stock,
        "is_promo_item": YES_NO_MAP.get(text_value(source.get("活动品"))),
        "listing_status": listing_status,
        "selling_points": field_value(source.get("卖点")),
        "sellable_date_text": field_value(source.get("可售日期")),
        "gift_bag_text": field_value(source.get("礼袋")),
        "is_recommended": YES_NO_MAP.get(text_value(source.get("是否力荐"))),
        "is_standard": IS_STANDARD_MAP.get(text_value(source.get("是否标品"))),
        "other_text": field_value(source.get("价格原文")) if not card_price else None,
        "evidence_uri": row_evidence(csv_path, source),
        "evidence_video_time": field_value(source.get("视频时间点")),
    }


def carry_price_key(text):
    """列表卡片价格文本的规范化比较键（每日增量指纹用）。

    数值价格 → 无指数、去尾零的十进制字符串（"45.00" 与 DB 45.0000 同键）；
    带单位后缀的采购价（"5.00/袋"）只比较金额部分（PURCHASE_PAT 同款解析）；
    非数值（如「无数据」）保留原文；空值 → None。
    """
    value = text_value(text)
    if not value:
        return None
    match = PURCHASE_PAT.match(value)
    amount = Decimal(match.group(1)) if match else nonnegative_amount(value)
    if amount is not None:
        return format(amount.normalize(), "f")
    return value


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


def coverage_allows_shortfall(coverage, expected):
    """平台标题数高于可见卡数时：只有成立的覆盖证明才放行。

    平台标题数是 App 自己的口径（它会把没渲染出来的商品也算进去）。证明成立
    说明「App 渲染了 N 张卡、我们读到了末尾、采到的正是这 N 张」——差额是
    App 侧多计，不是我们漏抓。证明不成立就是真漏了（或身份对不上），一律失败。

    证明由扫描器的 coverage_snapshot 给出，三条都要成立：
      1. list_exhausted：列表读到了末尾（App 的读尽标记）；
      2. cards_match：滚动时解析到的身份集合 == 采集行里的身份集合；
      3. rows_have_distinct_identities：采集行里没有两行共享身份——否则同名
         同规格的卡塌成一张，行数就不能当卡数用（旧证明的坑）。
    """
    if not isinstance(coverage, dict):
        return False
    if coverage.get("list_exhausted") is not True:
        return False
    if coverage.get("cards_match") is not True:
        return False
    if coverage.get("rows_have_distinct_identities") is not True:
        return False
    if coverage.get("unresolved_specs"):
        return False
    rendered = coverage.get("rendered_cards")
    captured = coverage.get("captured_cards")
    if not isinstance(rendered, int) or isinstance(rendered, bool):
        return False
    if not isinstance(captured, int) or isinstance(captured, bool):
        return False
    if rendered != captured:
        return False
    if not isinstance(expected, int) or isinstance(expected, bool):
        return False
    # 证明成立时差额是 App 侧多计；没有差额就不该走这条路。
    return captured < expected


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
        if state not in {"pending", "running", "success", "partial", "failed",
                         OBSERVED_MANIFEST_STATE}:
            report.fail(f"segment {top}/{leaf} state 无效：{state!r}")
        if state == "success":
            if captured != expected and not coverage_allows_shortfall(
                    segment.get("coverage"), expected):
                report.fail(f"segment {top}/{leaf} 数量不完整：{captured}/{expected}"
                            "（覆盖证明暂不承认：卡片集合按规格+标题去重，"
                            "同名同规格的卡片会塌成一张，set 相等不等于张数相等）")
            completed_raw += captured if isinstance(captured, int) else 0
        elif state == OBSERVED_MANIFEST_STATE:
            # 观测终态分片：平台目标数保留（不改写成实抓行数），数量差由该分片
            # 的例外凭据证明。行数照样计入 raw 合计——它确实采到了这些行。
            completed_raw += captured if isinstance(captured, int) else 0
            if require_complete:
                report.fail(f"segment 未完成（观测终态，业务不完整）："
                            f"{top}/{leaf}={state}")
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


def validate_full_store_coverage(scope, report, *, require_complete):
    """聚合清单必须显式声明全店覆盖口径；证明不成立就 fail closed。

    叶子类目分片跨时段采集，聚合的 expected_product_count 是「时间窗并集」，
    它同时等于 CSV 行数——用它自己证明完整就是自证。真正能证伪的只有聚合
    时从 App「全部商品」页读到的实时全店计数：差为 0 才谈得上完整。差额只
    说明覆盖无法证明，不代表漏抓了多少（分片不是同时点采集），所以报错只说
    「未证明」，不给缺口数字。
    """
    mode = scope.get("coverage_mode")
    if mode != FULL_STORE_COVERAGE_MODE:
        report.fail(f"manifest scope.coverage_mode 必须是 "
                    f"{FULL_STORE_COVERAGE_MODE!r}：{mode!r}")
    coverage = scope.get("full_store_coverage")
    if not isinstance(coverage, dict):
        report.fail("manifest 缺少 scope.full_store_coverage（未声明全店覆盖口径）")
        return
    status = coverage.get("status")
    if status not in FULL_STORE_COVERAGE_STATUSES:
        report.fail(f"scope.full_store_coverage.status 无效：{status!r}")
    full = scope.get("realtime_full_store_count")
    if not (isinstance(full, int) and not isinstance(full, bool) and full > 0):
        report.fail(f"scope.realtime_full_store_count 必须显式为正整数：{full!r}")
        return
    union = report.row_count
    declared = scope.get("realtime_count_difference")
    difference = union - full
    if declared != difference:
        report.fail(f"scope.realtime_count_difference={declared!r} 与实际不符："
                    f"{union} - {full} = {difference}")
    if coverage.get("full_count_source") not in (None, "device"):
        report.warnings.append("全店计数来自命令行覆盖（人工信任边界），未真机复验")
    if coverage.get("category_inventory_verified") is not True:
        # 静态类目清单没跟 App 实时类目树回验过：计数对账只证明数量，不证明
        # 集合覆盖。限制必须写在报告里，不能默默当全店完整。
        report.warnings.append("静态类目清单未与 App 实时类目树回验："
                               "计数对账不证明集合覆盖")
    if difference != 0:
        message = (f"全店覆盖未证明：聚合 {union} 行 ≠ 实时全店计数 {full}"
                   f"（coverage_mode={FULL_STORE_COVERAGE_MODE}：分片非同时点采集，"
                   "差额不等同漏抓数量，不推断缺口）")
        if require_complete:
            report.fail(message)
        else:
            report.warnings.append(message)
    reconciled = difference == 0
    if status == "verified" and not reconciled:
        report.fail("scope.full_store_coverage.status=verified 与计数事实矛盾")
    if require_complete and reconciled and status != "verified":
        report.fail(f"scope.full_store_coverage.status={status!r}：计数已对账，"
                    "status 必须是 verified")


def validate_product_identities(rows, report, extra_rows=None):
    """同一 SKU 指向不同商品身份时必须失败（待核实，不等于一定错）。

    无条码行靠 SKU/SPU 兜底定位（listing_identity）：同一个 SKU 若在两行里
    分别属于不同 SPU 或挂着不同商品名，会被拆成 sku:X 与 sku:X#2 两个身份
    悄悄入库。合法场景不受影响：同 SPU 多 SKU 完全允许，同 SKU 多规格但
    同名也允许（只比商品名称，不比规格）。

    只按「同一非空 SKU → 不同 SPU / 不同规范名称」判定，不做名称模糊比较：
    花王/高洁丝这类共享类目词的商品名必须由人复核，不靠启发式猜。
    """
    # extra_rows：sidecar 里的 SKU 子行（label, row），一起参与身份核对，
    # 否则同一个 SKU 出现在卡片行和 sidecar 里时会被拆成两个身份悄悄入库。
    entries = [(f"第{index}行", row) for index, row in enumerate(rows, 1)]
    entries += list(extra_rows or [])
    sku_spus = {}
    sku_names = {}
    for label, row in entries:
        sku = text_value(row.get("SKU"))
        if not sku:
            continue
        spu = text_value(row.get("SPU"))
        name = normalize_name(row.get("商品名称"))
        if spu:
            sku_spus.setdefault(sku, {}).setdefault(spu, label)
        if name:
            sku_names.setdefault(sku, {}).setdefault(name, label)
    conflicts = []
    for sku, spus in sorted(sku_spus.items()):
        if len(spus) > 1:
            conflicts.append("SKU " + sku + " 关联多个 SPU：" + "、".join(
                f"{spu}（{label}）" for spu, label in sorted(spus.items())))
    for sku, names in sorted(sku_names.items()):
        if len(names) > 1:
            conflicts.append("SKU " + sku + " 关联多个商品名称：" + "、".join(
                f"{name}（{label}）" for name, label in sorted(names.items())))
    for conflict in conflicts:
        report.fail("商品身份冲突（待核实）：" + conflict
                    + "；请重采受影响分片后人工复核")


def multi_sku_rows(rows: list[dict]) -> list[tuple[int, dict]]:
    """多 SKU 卡片行（状态=多SKU）及其 CSV 行号。"""
    return [(index, row) for index, row in enumerate(rows, 1) if is_multisku_row(row)]


def _seq_sort_key(value):
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def _parse_iso(value):
    try:
        return datetime.fromisoformat(text_value(value))
    except (TypeError, ValueError):
        return None


def validate_sku_sidecar(scope, artifacts, progress, rows, report, *,
                         csv_path, manifest_dir, state="", capture=None):
    """多 SKU sidecar 门禁：CSV 卡片行只是壳，真实数据在 sidecar 里。

    partial 和 complete 都要过这一关。状态写着「多SKU」不能替代 sidecar 本身：
    文件在不在、hash 对不对、下标一一对应（不漏不多）、每组至少 2 个 SKU、
    组内 SKU 唯一且同 SPU、每个 SKU 的标题/采集时间/证据与父卡一致、
    店内码/货号不能复制第一个、详情列完整、状态合法——全部自己算，不信 CSV 状态。

    零多 SKU 且没声明 scope.sku_mode 时原样放行：旧契约一个字都不改。
    """
    multi = multi_sku_rows(rows)
    mode = scope.get("sku_mode")
    raw_path = artifacts.get("sku_path")
    has_sidecar = isinstance(raw_path, str) and bool(raw_path.strip())
    if mode is None and not multi and not has_sidecar:
        return []
    if mode is not None and mode != SKU_SIDECAR_MODE:
        report.fail(f"manifest scope.sku_mode 无效：{mode!r}")
        return []
    if multi and mode is None:
        report.fail(f"CSV 有 {len(multi)} 行多SKU，但 manifest scope.sku_mode 未声明"
                    f"（多SKU采集产物必须声明 {SKU_SIDECAR_MODE!r}）")
        return []
    if not has_sidecar:
        report.fail("manifest artifacts.sku_path 必须是非空字符串")
        return []
    sku_path = _resolve_path(raw_path, manifest_dir)
    artifacts["sku_path"] = sku_path
    if not os.path.isfile(sku_path):
        report.fail(f"多SKU sidecar 不存在：{sku_path}")
        return []
    expected_hash = text_value(artifacts.get("sku_sha256"))
    actual_hash = sha256_file(sku_path)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        report.fail(f"manifest artifacts.sku_sha256 无效：{expected_hash!r}")
    elif actual_hash != expected_hash:
        report.fail("多SKU sidecar SHA-256 与 manifest 不一致")
    try:
        sidecar = read_json(sku_path)
    except Exception as exc:
        report.fail(str(exc))
        return []
    if sidecar.get("schema_version") != SKU_SIDECAR_SCHEMA_VERSION:
        report.fail(f"sidecar schema_version 不支持：{sidecar.get('schema_version')}")
        return []
    listings = sidecar.get("listings")
    if listings is None and not multi:
        # 单卡采集也可能带 sidecar（只装 observations），没有 listings 是正常的。
        listings = {}
    if not isinstance(listings, dict):
        report.fail("sidecar 缺少 listings 对象")
        return []
    observations = validate_observations(sidecar.get("observations"), rows, listings, report)
    report.observations = observations
    report.listings = listings if isinstance(listings, dict) else {}
    gate_on = observation_gate_enabled(state, capture)
    cards = {}
    for index, row in multi:
        seq = text_value(row.get("序号"))
        if not seq.isdigit():
            report.fail(f"第{index}行序号无效：{seq!r}")
            continue
        cards[str(int(seq))] = (index, row)
    for key in sorted(set(cards) - set(listings), key=_seq_sort_key):
        report.fail(f"多SKU卡片 序号{key} 在 sidecar 里没有 listing")
    for key in sorted(set(listings) - set(cards), key=_seq_sort_key):
        report.fail(f"sidecar listing {key} 没有对应的多SKU卡片行（多余下标）")
    children = []
    for key in sorted(cards, key=_seq_sort_key):
        index, row = cards[key]
        card_errors = 0
        listing = listings.get(key)
        if not isinstance(listing, list):
            report.fail(f"多SKU卡片 序号{key} 的 sidecar listing 必须是数组")
            continue
        if len(listing) < 2:
            report.fail(f"多SKU卡片 序号{key} 的 sidecar 只有 {len(listing)} 个SKU"
                        "（多SKU至少 2 个）")
            continue
        for column in MULTISKU_CARD_FORBIDDEN_COLUMNS:
            if text_value(row.get(column)):
                report.fail(f"第{index}行是多SKU卡片行，不得携带 SKU 级列「{column}」"
                            "（每个 SKU 的详情在 sidecar 里逐个成行）")
        parent_title = normalize_name(row.get("商品名称"))
        parent_time = _parse_iso(row.get("采集时间"))
        parent_evidence = row_evidence(csv_path, row)
        skus, spus = [], set()
        store_codes, article_numbers = [], []
        for position, child in enumerate(listing, 1):
            label = f"第{index}行(序号{key})第{position}个SKU"
            if not isinstance(child, dict):
                report.fail(f"{label} 必须是对象")
                continue
            missing = [column for column in FULL_COLUMNS if column not in child]
            extra = [column for column in child if column not in FULL_COLUMNS]
            if missing:
                report.fail(f"{label} 缺少列：{'、'.join(missing)}")
                continue
            if extra:
                report.fail(f"{label} 多出未知列：{'、'.join(extra)}")
                continue
            if text_value(child.get("序号")) != key:
                report.fail(f"{label} 序号必须是父卡序号 {key}："
                            f"{text_value(child.get('序号'))!r}")
            child_status = text_value(child.get("状态"))
            if child_status not in SKU_CHILD_STATUSES:
                report.fail(f"{label} 状态无效：{child_status!r}"
                            f"（只允许 {'、'.join(sorted(SKU_CHILD_STATUSES))}）")
            if normalize_name(child.get("商品名称")) != parent_title:
                report.fail(f"{label} 商品名称与父卡不一致："
                            f"{text_value(child.get('商品名称'))!r} != {parent_title!r}")
            child_time = _parse_iso(child.get("采集时间"))
            if child_time is None:
                report.fail(f"{label} 采集时间不是 ISO 时间：{child.get('采集时间')!r}")
            elif parent_time is not None and child_time != parent_time:
                report.fail(f"{label} 采集时间与父卡不一致：{child.get('采集时间')!r}")
            if row_evidence(csv_path, child) != parent_evidence:
                report.fail(f"{label} 证据前缀与父卡不一致："
                            f"{row_evidence(csv_path, child)} != {parent_evidence}")
            if not field_value(child.get("规格")):
                report.fail(f"{label} 缺少该 SKU 的具体规格")
            sku = text_value(child.get("SKU"))
            if not sku:
                report.fail(f"{label} 缺少 SKU")
            else:
                skus.append(sku)
            spu = text_value(child.get("SPU"))
            if not spu:
                report.fail(f"{label} 缺少 SPU")
            else:
                spus.add(spu)
            for column, bucket in (("店内码", store_codes), ("货号", article_numbers)):
                value = text_value(child.get(column))
                if value:
                    bucket.append(value)
            errors = row_detail_errors(child)
            observed = []
            hard = []
            for error in errors:
                channel = _channel_error_channel(error)
                entry = (observation_entry(observations, key, sku, channel)
                         if (channel and gate_on) else None)
                if entry is not None and observation_backs_unknown(
                        child, channel, entry, seq=key, sku=sku,
                        label=f"{label} 渠道「{channel}」", report=report):
                    # 凭据支撑的行缺失：记成观测例外，不算 detail_complete。
                    observed.append(error)
                    report.observed_unknowns.append(
                        (key, sku, channel, text_value(entry.get("section_status")),
                         text_value(child.get("规格"))))
                    continue
                hard.append(error)
            if hard:
                report.fail(f"{label} 详情字段不完整：{'/'.join(hard)}")
                card_errors += len(hard)
            report.observation_count += len(observed)
            # 未知也是缺口：含未知的子行 → 整张多 SKU 卡片不算 detail_complete。
            card_errors += len(observed)
            children.append((label, child))
        if len(set(skus)) != len(skus):
            report.fail(f"多SKU卡片 序号{key} 的 sidecar 里 SKU 重复：{skus}")
        if len(spus) > 1:
            report.fail(f"多SKU卡片 序号{key} 的 sidecar 里 SKU 跨了多个 SPU："
                        f"{'、'.join(sorted(spus))}")
        for column, values in (("店内码", store_codes), ("货号", article_numbers)):
            if len(set(values)) != len(values):
                report.fail(f"多SKU卡片 序号{key} 的 {column} 复制了第一个 SKU：{values}")
        if card_errors == 0:
            # 这张多 SKU 卡片的所有子行都干净 → 整卡算 detail_complete。
            report.multisku_complete_cards.add(key)
    # progress 口径（主线程约定）：listing_count=卡片行数；sku_row_count=
    # 多SKU卡片按子行数计、单卡按 1 计（含不完整卡，一行也算一个）。
    if multi:
        listing_count = progress.get("listing_count")
        if listing_count != len(rows):
            report.fail(f"manifest progress.listing_count={listing_count!r}，"
                        f"CSV 卡片数={len(rows)}")
        expected_rows = 0
        for row in rows:
            if not is_multisku_row(row):
                expected_rows += 1
                continue
            seq = text_value(row.get("序号"))
            listing = listings.get(seq) if seq.isdigit() else None
            expected_rows += len(listing) if isinstance(listing, list) else 0
        sku_row_count = progress.get("sku_row_count")
        if sku_row_count != expected_rows:
            report.fail(f"manifest progress.sku_row_count={sku_row_count!r}，"
                        f"应为 {expected_rows}（多SKU卡片按子行数计，单卡按 1 计）")
    return children


def _channel_error_channel(error: str):
    """row_detail_errors 的渠道错误形如「<渠道>详情」；其余错误不是渠道缺失。"""
    for channel in CHANNEL_NAMES:
        if error == channel + "详情":
            return channel
    return None


def observation_gate_enabled(state, capture) -> bool:
    """观测门禁只在例外态 + v1 契约下开。

    success 里的「未知」永远失败：声称成功就不该有未知。例外态（state 非
    success）且 capture 声明 observation_contract=v1 时，才允许凭观测凭据把
    「渠道行缺失」记成未知而不是失败。
    """
    return (text_value(state) != "success"
            and text_value((capture or {}).get("observation_contract"))
            == OBSERVATION_CONTRACT_VERSION)


def observation_entry(observations, card_seq, sku, channel):
    """精确匹配 (卡片序号, SKU, 渠道) 的观测凭据；匹配不上就是没有凭据。"""
    block = (observations or {}).get(str(card_seq))
    if not isinstance(block, dict):
        return None
    channels = block.get(str(sku))
    if not isinstance(channels, dict):
        return None
    entry = channels.get(channel)
    return entry if isinstance(entry, dict) else None


def validate_observations(observations, rows, listings, report) -> dict:
    """sidecar observations 形状校验。

    形状：observations{str(卡片序号): {str(SKU): {中文渠道: {observation,
    section_status, reason, evidence_pages}}}}。observation 必须是 row_absent，
    section_status 必须是真实上架状态，reason 不能空。形状不对就是编造凭据，
    直接 fail——凭据本身是「渠道行缺失」的唯一放行理由。
    """
    if observations is None:
        return {}
    if not isinstance(observations, dict):
        report.fail("sidecar observations 必须是对象")
        return {}
    row_skus = {text_value(row.get("序号")): text_value(row.get("SKU")) for row in rows}
    for key in sorted(observations, key=_seq_sort_key):
        block = observations[key]
        if not isinstance(block, dict):
            report.fail(f"sidecar observations[{key}] 必须是对象")
            continue
        if key not in row_skus:
            report.fail(f"sidecar observations 序号{key} 没有对应的 CSV 行")
            continue
        listing = listings.get(key) if isinstance(listings, dict) else None
        known_skus = set()
        if isinstance(listing, list):
            known_skus |= {text_value(child.get("SKU")) for child in listing
                           if isinstance(child, dict)}
        if row_skus.get(key):
            known_skus.add(row_skus[key])
        for sku, channels in sorted(block.items()):
            if not isinstance(channels, dict):
                report.fail(f"sidecar observations[{key}][{sku}] 必须是对象")
                continue
            if known_skus and sku not in known_skus:
                report.fail(f"sidecar observations[{key}] 的 SKU {sku} 不在该卡上")
                continue
            for channel, entry in sorted(channels.items()):
                label = f"sidecar observations[{key}][{sku}][{channel}]"
                if channel not in CHANNEL_NAMES:
                    report.fail(f"{label} 未知渠道：{channel!r}")
                    continue
                if not isinstance(entry, dict):
                    report.fail(f"{label} 必须是对象")
                    continue
                if text_value(entry.get("observation")) != OBSERVATION_ROW_ABSENT:
                    report.fail(f"{label} observation 必须是 "
                                f"{OBSERVATION_ROW_ABSENT!r}："
                                f"{entry.get('observation')!r}")
                if text_value(entry.get("section_status")) not in LISTING_STATUS_MAP:
                    report.fail(f"{label} section_status 无效："
                                f"{entry.get('section_status')!r}")
                if not text_value(entry.get("reason")):
                    report.fail(f"{label} 缺少 reason")
                pages = entry.get("evidence_pages")
                if pages is not None and not isinstance(pages, list):
                    report.fail(f"{label} evidence_pages 必须是数组")
    return observations


def _evidence_pages_ok(entry, report, label) -> bool:
    """凭据必须挂真实 detail XML：逐页存在 + hash 对得上。"""
    pages = entry.get("evidence_pages")
    if not isinstance(pages, list) or not pages:
        report.fail(f"{label} 缺少 evidence_pages（渠道行缺失必须挂真实 detail XML）")
        return False
    ok = True
    for index, page in enumerate(pages, 1):
        if not isinstance(page, dict):
            report.fail(f"{label} evidence_pages 第{index}项必须是对象")
            ok = False
            continue
        path = text_value(page.get("path"))
        digest = text_value(page.get("sha256"))
        if not path:
            report.fail(f"{label} evidence_pages 第{index}项缺少 path")
            ok = False
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            report.fail(f"{label} evidence_pages 第{index}项 sha256 无效：{digest!r}")
            ok = False
            continue
        if not os.path.isfile(path):
            report.fail(f"{label} evidence_pages 文件不存在：{path}")
            ok = False
            continue
        if sha256_file(path) != digest:
            report.fail(f"{label} evidence_pages hash 与文件不一致：{path}")
            ok = False
    return ok


def _row_absent_confirmed(pages, channel, spec, section_status, label, report) -> bool:
    """独立复解析 detail XML：渠道块在、块状态相符、且这一 SKU 真没有行。

    凭据自己说「行缺失」不算数，只有原始 XML 能作证：块里若真有这一行，
    就是编造，直接 fail。
    """
    try:
        import qnh_sku_capture as sku_capture
    except Exception as exc:  # pragma: no cover - 依赖缺失时的兜底
        report.fail(f"{label} 无法复解析 detail XML：{exc}")
        return False
    try:
        from qnh_observation import parse_nodes
    except Exception as exc:
        report.fail(f"{label} 无法复解析 detail XML（qnh_observation.parse_nodes "
                    f"不可用）：{exc}")
        return False
    nodes = []
    for page in pages:
        path = text_value(page.get("path")) if isinstance(page, dict) else ""
        try:
            with open(path, encoding="utf-8") as fh:
                nodes.extend(parse_nodes(fh.read()))
        except Exception as exc:
            report.fail(f"{label} 读取 detail XML 失败：{path}：{exc}")
            return False
    try:
        ns = sku_capture._clean_nodes(nodes)
        sections = sku_capture._channel_sections(ns)
    except Exception as exc:
        report.fail(f"{label} detail XML 复解析失败：{exc}")
        return False
    if not any(node["text"] == DETAIL_PAGE_FOOTER for node in ns):
        report.fail(f"{label} detail XML 没有页底标记「{DETAIL_PAGE_FOOTER}」："
                    "无法证明渠道表已读到底")
        return False
    headers = {}
    for node in ns:
        match = sku_capture.STATUS_PAT.match(node["text"])
        if match and match.group(2) in sku_capture.CHANNELS:
            headers[match.group(2)] = match.group(1)
    if channel not in sections:
        report.fail(f"{label} detail XML 里没有渠道「{channel}」的区块"
                    "（整块缺失不是行缺失）")
        return False
    if headers.get(channel) != section_status:
        report.fail(f"{label} detail XML 里渠道「{channel}」区块状态是 "
                    f"{headers.get(channel)!r}，凭据写的是 {section_status!r}")
        return False
    for entry in sections[channel]:
        if sku_capture._norm(entry[1]) == sku_capture._norm(spec):
            report.fail(f"{label} detail XML 里渠道「{channel}」有这一 SKU 的行，"
                        "不是行缺失")
            return False
    return True


def observation_backs_unknown(row, channel, entry, *, seq, sku, label, report) -> bool:
    """凭据能否支撑这一行的「未知」。

    全部满足才认：状态是未知、确实没有价/库存、凭据形状合法、挂着的 detail
    XML 逐页 hash 对得上、且复解析后确认这一行真不存在。任何一条不满足就是
    没有凭据，按原来的严格口径失败。
    """
    if not isinstance(entry, dict):
        return False
    if text_value(entry.get("observation")) != OBSERVATION_ROW_ABSENT:
        return False
    section_status = text_value(entry.get("section_status"))
    if section_status not in LISTING_STATUS_MAP:
        return False
    if not text_value(entry.get("reason")):
        return False
    if text_value(row.get(channel + "状态")) != CHANNEL_UNKNOWN_STATUS:
        return False
    if field_value(row.get(channel + "零售价")) or field_value(row.get(channel + "库存")):
        return False
    if not _evidence_pages_ok(entry, report, label):
        return False
    pages = entry.get("evidence_pages") or []
    return _row_absent_confirmed(pages, channel, text_value(row.get("规格")),
                                 section_status, label, report)


def _observation_credential(report, rows, sku, channel):
    """在 sidecar 凭据里按 SKU + 渠道找一条（SKU 只在多 SKU 卡的子行里有）。

    找不到就是没有凭据：调用方按「没凭据」处理，不许拿别的 SKU 顶替。
    """
    listings = getattr(report, "listings", None) or {}
    for row in rows:
        if not is_multisku_row(row):
            continue
        seq = text_value(row.get("序号"))
        for child in (listings.get(seq) or []):
            if not isinstance(child, dict):
                continue
            if text_value(child.get("SKU")) == sku:
                return observation_entry(report.observations, seq, sku, channel)
    return None


def validate_observation_exceptions(progress, scope, rows, report) -> set:
    """progress.exceptions 逐条校验：声明的例外必须有观测凭据，观测到的必须登记。

    例外条目格式（主线程口径）：
      {'type': 'channel_row_absent', 'sku': ..., 'channel': ..., 'spec': ...}
      {'type': 'platform_count_mismatch', 'platform_count': 52,
       'visible_card_count': 51}
      {'type': 'segment_platform_count_mismatch', 'segment': '萌宠专区/猫粮',
       'platform_count': 52, 'visible_card_count': 51}（聚合级）
    """
    exceptions = progress.get("exceptions")
    if not isinstance(exceptions, list):
        report.fail("manifest progress.exceptions 必须是数组")
        return set()
    scope_segments = scope.get("segments")
    if not isinstance(scope_segments, list):
        scope_segments = []
    declared_types = set()
    declared_pairs = []
    declared_evidence = []
    for index, item in enumerate(exceptions, 1):
        label = f"progress.exceptions 第{index}项"
        if not isinstance(item, dict):
            report.fail(f"{label} 必须是对象")
            continue
        kind = text_value(item.get("type"))
        if kind not in OBSERVATION_EXCEPTION_CODES:
            report.fail(f"{label} type 无效：{kind!r}"
                        f"（可用：{'、'.join(sorted(OBSERVATION_EXCEPTION_CODES))}）")
            continue
        declared_types.add(kind)
        if kind == "channel_row_absent":
            sku = text_value(item.get("sku"))
            channel = text_value(item.get("channel"))
            spec = text_value(item.get("spec"))
            if not sku:
                report.fail(f"{label} 缺少 sku")
                continue
            if channel not in CHANNEL_NAMES:
                report.fail(f"{label} channel 无效：{channel!r}")
                continue
            if not spec:
                report.fail(f"{label} 缺少 spec")
                continue
            declared_pairs.append((sku, channel))
            declared_evidence.append((sku, channel, item.get("evidence_pages")))
        elif kind == "segment_platform_count_mismatch":
            # 聚合级例外：某个分片的平台标题数与该分片实抓卡数对不上。
            segment_path = text_value(item.get("segment"))
            if not segment_path:
                report.fail(f"{label} 缺少 segment（类目路径）")
                continue
            platform_count = item.get("platform_count")
            visible_count = item.get("visible_card_count")
            if not isinstance(platform_count, int) or isinstance(platform_count, bool) \
                    or platform_count <= 0:
                report.fail(f"{label} platform_count 无效：{platform_count!r}")
                continue
            if not isinstance(visible_count, int) or isinstance(visible_count, bool) \
                    or visible_count < 0:
                report.fail(f"{label} visible_card_count 无效：{visible_count!r}")
                continue
            if platform_count == visible_count:
                report.fail(f"{label} 没有数量差却声明 segment_platform_count_mismatch")
                continue
            # 聚合里声明的分片计数差必须对得上聚合自己的分片记录：例外不是
            # 自由文本，是分片清单里的事实。
            if scope_segments:
                matched = [item_segment for item_segment in scope_segments
                           if isinstance(item_segment, dict)
                           and f"{text_value(item_segment.get('top'))}/"
                           f"{text_value(item_segment.get('leaf'))}" == segment_path]
                if not matched:
                    report.fail(f"{label} segment={segment_path!r} 不在聚合的分片清单里")
                    continue
                record = matched[0]
                if record.get("expected_count") != platform_count:
                    report.fail(f"{label} platform_count={platform_count!r} 与分片记录 "
                                f"expected_count={record.get('expected_count')!r} 不一致")
                    continue
                if record.get("captured_count") != visible_count:
                    report.fail(f"{label} visible_card_count={visible_count!r} 与分片记录 "
                                f"captured_count={record.get('captured_count')!r} 不一致")
                    continue
        else:
            platform_count = item.get("platform_count")
            visible_count = item.get("visible_card_count")
            if platform_count != scope.get("expected_product_count"):
                report.fail(f"{label} platform_count={platform_count!r} 与 "
                            f"scope.expected_product_count="
                            f"{scope.get('expected_product_count')!r} 不一致")
                continue
            if visible_count != len(rows):
                report.fail(f"{label} visible_card_count={visible_count!r} 与 CSV "
                            f"{len(rows)} 行不一致")
                continue
            if platform_count == visible_count:
                report.fail(f"{label} 没有数量差却声明 platform_count_mismatch")
                continue
    observed = [(sku, channel) for _, sku, channel, _, _ in report.observed_unknowns]
    for pair in observed:
        if pair not in declared_pairs:
            report.fail(f"观测到的渠道行缺失没有登记进 progress.exceptions：{pair}")
    for pair in declared_pairs:
        if pair not in observed:
            report.fail(f"progress.exceptions 里的渠道行缺失没有观测凭据：{pair}")
    # 证据指针：manifest 的例外与 sidecar 的凭据必须指向同一份详情 XML。
    # 缺证据指针只警告（旧产物如此），指向不同证据就是矛盾，直接失败。
    for sku, channel, pages in declared_evidence:
        credential = _observation_credential(report, rows, sku, channel)
        if credential is None:
            continue
        sidecar_pages = credential.get("evidence_pages")
        if pages is None:
            report.warnings.append(
                f"渠道行缺失例外（SKU {sku} / {channel}）没有 evidence_pages："
                "证据只在 sidecar 里，manifest 单读看不出证据在哪")
        elif pages != sidecar_pages:
            report.fail(f"progress.exceptions 里 SKU {sku} / {channel} 的 "
                        "evidence_pages 与 sidecar 凭据不一致")
    return declared_types


def _card_triple(card):
    if not isinstance(card, dict):
        return None
    return (text_value(card.get("name")), text_value(card.get("barcode")),
            text_value(card.get("spec")))


def cards_match_rows(cards, rows):
    """采集行必须正好是列表卡片：身份多重集相等（不是 set 相等）。

    身份 = 规范化标题 + 规格。条码只在两侧都给得出时才逐组比对：列表卡常常
    不显示条码，采集行可能来自详情页，这时条码不可比。

    张数相等不等于采对了：漏一张又错收一张，张数照样相等，身份多重集才
    说得清。扫描器决定「是否升级到观测终态」与门禁复核都用这个口径。
    """
    def group(items, name_key, spec_key, barcode_key):
        out = {}
        for item in items:
            key = (normalize_name(item.get(name_key)), text_value(item.get(spec_key)))
            out.setdefault(key, []).append(text_value(item.get(barcode_key)))
        return out

    card_groups = group(cards, "name", "spec", "barcode")
    row_groups = group(rows, "商品名称", "规格", "条码")
    if (Counter({k: len(v) for k, v in card_groups.items()})
            != Counter({k: len(v) for k, v in row_groups.items()})):
        only_cards = sorted(k for k in card_groups if k not in row_groups)[:5]
        only_rows = sorted(k for k in row_groups if k not in card_groups)[:5]
        return (f"卡片身份多重集与采集行不一致（卡片多出 {only_cards}，"
                f"采集行多出 {only_rows}）")
    for key, codes in card_groups.items():
        if not all(codes):
            continue
        if sorted(row_groups.get(key, [])) != sorted(codes):
            return f"条码与采集行不一致：{key[0]}"
    return None


def validate_list_evidence(artifacts, scope, rows, report, *, manifest_dir,
                            expected) -> bool:
    """逐屏列表证据：平台标题数与可见卡片数不一致时的唯一放行凭据。

    证据 JSON 里自己写的 platform_count / cards / list_exhausted 都不算数：
    只有把原始列表页 XML 重新复解析一遍、结果一一相等才算数（qnh_observation
    .audit_pages 是纯函数，不反向依赖本模块）。
    """
    raw_path = text_value(artifacts.get("list_evidence_path"))
    if not raw_path:
        report.fail("例外态必须给出 artifacts.list_evidence_path"
                    "（逐屏列表证据：观测完成不等于业务完整，页面已采全要拿得出证据）")
        return False
    path = _resolve_path(raw_path, manifest_dir)
    artifacts["list_evidence_path"] = path
    if not os.path.isfile(path):
        report.fail(f"列表证据不存在：{path}")
        return False
    expected_hash = text_value(artifacts.get("list_evidence_sha256"))
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        report.fail(f"manifest artifacts.list_evidence_sha256 无效：{expected_hash!r}")
        return False
    if sha256_file(path) != expected_hash:
        report.fail("列表证据 SHA-256 与 manifest 不一致")
        return False
    try:
        evidence = read_json(path)
    except Exception as exc:
        report.fail(str(exc))
        return False
    if evidence.get("schema_version") != LIST_EVIDENCE_SCHEMA_VERSION:
        report.fail(f"列表证据 schema_version 不支持："
                    f"{evidence.get('schema_version')}")
        return False
    pages = evidence.get("pages")
    if not isinstance(pages, list) or not pages:
        report.fail("列表证据 pages 必须是非空数组")
        return False
    page_paths = []
    for index, page in enumerate(pages, 1):
        if not isinstance(page, dict):
            report.fail(f"列表证据 pages 第{index}项必须是对象")
            return False
        page_path = _resolve_path(page.get("path"), manifest_dir)
        digest = text_value(page.get("sha256"))
        if not page_path or not os.path.isfile(page_path):
            report.fail(f"列表证据 pages 第{index}项文件不存在：{page.get('path')!r}")
            return False
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            report.fail(f"列表证据 pages 第{index}项 sha256 无效：{digest!r}")
            return False
        if sha256_file(page_path) != digest:
            report.fail(f"列表证据 pages 第{index}项 hash 与文件不一致：{page_path}")
            return False
        page_paths.append(page_path)
    try:
        from qnh_observation import audit_pages
    except Exception as exc:
        report.fail(f"列表证据无法复解析（qnh_observation.audit_pages 不可用）：{exc}")
        return False
    category = scope.get("category")
    if not text_value(category):
        report.fail("例外态短收必须有 scope.category（列表证据按类目复解析）")
        return False
    try:
        audit = audit_pages(page_paths, category)
    except Exception as exc:
        report.fail(f"列表证据复解析失败：{exc}")
        return False
    if not isinstance(audit, dict):
        report.fail("列表证据复解析结果必须是对象")
        return False
    if audit.get("platform_count") != expected:
        report.fail(f"列表证据复解析平台计数 {audit.get('platform_count')!r} 与 "
                    f"scope.expected_product_count={expected!r} 不一致")
        return False
    if evidence.get("platform_count") != audit.get("platform_count"):
        report.fail("列表证据 platform_count 与复解析结果不一致")
        return False
    if evidence.get("list_exhausted") is not True or audit.get("list_exhausted") is not True:
        report.fail("列表证据 list_exhausted 必须是 true（列表必须读尽）")
        return False
    if text_value(audit.get("category")) != text_value(category):
        report.fail(f"列表证据复解析类目 {audit.get('category')!r} 与 "
                    f"scope.category={category!r} 不一致")
        return False
    if text_value(evidence.get("category")) != text_value(category):
        report.fail(f"列表证据 category={evidence.get('category')!r} 与 "
                    f"scope.category={category!r} 不一致")
        return False
    cards = evidence.get("cards")
    audit_cards = audit.get("cards")
    if not isinstance(cards, list) or not isinstance(audit_cards, list):
        report.fail("列表证据 cards 必须是数组")
        return False
    if [_card_triple(card) for card in cards] != [_card_triple(card) for card in audit_cards]:
        report.fail("列表证据 cards 与复解析结果不一致（顺序也要一致）")
        return False
    if len(cards) != len(rows):
        report.fail(f"列表证据 cards={len(cards)} 张，CSV={len(rows)} 行")
        return False
    mismatch = cards_match_rows(cards, rows)
    if mismatch:
        report.fail("列表证据卡片与采集行不匹配：" + mismatch)
        return False
    return True


def validate_observed_state(manifest, scope, artifacts, progress, rows, report, *,
                            manifest_dir, expected) -> None:
    """例外态门禁：观测完成 ≠ 业务完整。

    observed_with_exceptions 是终态但不是 success：必须有 v1 契约标记、观测
    完成标记、业务未完成标记、逐条登记的例外，以及（短收时）逐屏列表证据。
    缺任何一样都是「声称观测完成却拿不出证据」，直接 fail。
    """
    capture = manifest.get("capture") or {}
    if text_value(capture.get("observation_contract")) != OBSERVATION_CONTRACT_VERSION:
        report.fail(f"manifest capture.observation_contract 必须是 "
                    f"{OBSERVATION_CONTRACT_VERSION!r}："
                    f"{capture.get('observation_contract')!r}")
    if progress.get("observation_complete") is not True:
        report.fail(f"manifest progress.observation_complete 必须是 true："
                    f"{progress.get('observation_complete')!r}")
    if progress.get("business_complete") is not False:
        report.fail(f"manifest progress.business_complete 必须是 false："
                    f"{progress.get('business_complete')!r}")
    exceptions = progress.get("exceptions")
    if not isinstance(exceptions, list) or not exceptions:
        report.fail("manifest progress.exceptions 必须是非空数组"
                    "（例外态必须逐条登记例外）")
    declared_count = progress.get("observation_count")
    if declared_count is not None and declared_count != report.observation_count:
        report.fail(f"manifest progress.observation_count={declared_count!r} 与实际 "
                    f"{report.observation_count} 条观测例外不一致")
    declared = validate_observation_exceptions(progress, scope, rows, report)
    if expected is not None and len(rows) < expected:
        if "platform_count_mismatch" not in declared:
            report.fail(f"例外态短收必须登记 platform_count_mismatch："
                        f"CSV {len(rows)} 行 / 平台 {expected}")
    if scope.get("mode") == SEGMENT_MODE:
        # 聚合产物的逐屏列表证据在各个分片 manifest 里（scope.segments 记着
        # 分片 manifest 路径与哈希），聚合层不再要求一份列表证据。
        return
    # 例外态一律要逐屏列表证据：「页面已采全」这句话必须拿得出证据，不能
    # 只靠「行数对得上」。短收只是其中一种（张数少），不短收（只有渠道行
    # 缺失）同样要证。
    validate_list_evidence(artifacts, scope, rows, report,
                            manifest_dir=manifest_dir, expected=expected)


def build_manifest(*, capture_id: str, batch_id: str, run_id: str, state: str,
                   profile: dict | None, device_id: str | None, scope: dict,
                   capture: dict, artifacts: dict, rows: list[dict],
                   stop_reason: str = "", parent_manifest_sha256: str | None = None,
                   coverage: dict | None = None,
                   observation: dict | None = None,
                   detail_complete_count: int | None = None) -> dict:
    counts = status_counts(rows)
    # 多 SKU 卡片行的 row_detail_errors 恒为空（详情在 sidecar 里逐 SKU 成行），
    # 所以这个默认口径会把「子行有未知渠道」的卡也算完整。聚合场景由调用方按
    # sidecar 口径传入真实值。
    detail_complete = (detail_complete_count if detail_complete_count is not None
                       else sum(not row_detail_errors(row) for row in rows))
    artifacts = dict(artifacts)
    csv_path = text_value(artifacts.get("csv_path"))
    if csv_path:
        artifacts["row_evidence_prefixes"] = sorted({row_evidence(csv_path, row) for row in rows})
    progress = {
        "row_count": len(rows),
        "status_counts": counts,
        "detail_complete_count": detail_complete,
        "stop_reason": stop_reason,
    }
    if coverage:
        progress["coverage"] = coverage
    if observation:
        # 例外态的观测口径：observation_complete / business_complete / exceptions。
        for key in ("observation_complete", "business_complete", "exceptions",
                    "observation_count"):
            if key in observation:
                progress[key] = observation[key]
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
        "progress": progress,
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
    sku_children = validate_sku_sidecar(scope, artifacts, progress, rows, report,
                                        csv_path=csv_path, manifest_dir=manifest_dir,
                                        state=state, capture=capture)
    validate_product_identities(rows, report, extra_rows=sku_children)
    if scope.get("mode") == SEGMENT_MODE:
        validate_full_store_coverage(scope, report, require_complete=require_complete)
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
        if coverage_allows_shortfall(progress.get("coverage"), expected):
            report.warnings.append(
                f"平台标题计数 {expected} 高于列表渲染卡片 {len(rows)}："
                "应用已标记列表读尽且渲染卡片全部采集，按卡片数完成")
        else:
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
    # 携带行的详情列是上一轮基线的历史合成值，不是当日详情页真值：完整入库
    # 必须拒绝（partial 门禁放行，好让旧工件被载入后丢弃）。
    if require_complete:
        carry_rows = [index for index, row in enumerate(rows, 1)
                      if text_value(row.get("状态")) == CARRY_ROW_STATUS]
        if carry_rows:
            report.fail(f"完整入库不接受携带行：{len(carry_rows)} 行"
                        f"（第{carry_rows[0]}行起）；携带行详情是历史合成值，"
                        "必须重新实访详情页")

    detail_errors = []
    unstable_name_counts = Counter()
    evidence_prefixes = set()
    row_datetimes = []
    incomplete_rows = 0
    gate_on = observation_gate_enabled(state, capture)
    for i, row in enumerate(rows, 1):
        name = normalize_name(row.get("商品名称"))
        barcode = text_value(row.get("条码"))
        if not name:
            report.fail(f"第{i}行缺少商品名称")
        if (not barcode and not text_value(row.get("SKU"))
                and not text_value(row.get("SPU")) and not is_multisku_row(row)):
            unstable_name_counts[name] += 1
        raw_status = str(row.get("状态") or "")
        if raw_status != raw_status.strip():
            report.fail(f"第{i}行状态含首尾空白")
        for channel in CHANNEL_NAMES:
            raw_channel_status = str(row.get(channel + "状态") or "")
            if raw_channel_status != raw_channel_status.strip():
                report.fail(f"第{i}行{channel}状态含首尾空白")
        errs = row_detail_errors(row)
        if is_multisku_row(row):
            # 多 SKU 卡片行的完整性看 sidecar 子行：任一子行有缺口或未知，
            # 整张卡就不算 detail_complete（父卡行本身没有 SKU 级列）。
            if text_value(row.get("序号")) not in report.multisku_complete_cards:
                incomplete_rows += 1
        elif errs:
            # 观测凭据支撑的渠道行缺失不算硬错误，但这一行仍不是 detail_complete：
            # 未知就是未知，不能伪成完整。
            incomplete_rows += 1
            hard = []
            for error in errs:
                channel = _channel_error_channel(error)
                entry = (observation_entry(report.observations,
                                           text_value(row.get("序号")),
                                           text_value(row.get("SKU")), channel)
                         if (channel and gate_on) else None)
                if entry is not None and observation_backs_unknown(
                        row, channel, entry,
                        seq=text_value(row.get("序号")),
                        sku=text_value(row.get("SKU")),
                        label=f"第{i}行 渠道「{channel}」", report=report):
                    report.observed_unknowns.append(
                        (text_value(row.get("序号")), text_value(row.get("SKU")),
                         channel, text_value(entry.get("section_status")),
                         text_value(row.get("规格"))))
                    report.observation_count += 1
                    continue
                hard.append(error)
            if hard:
                detail_errors.append(f"第{i}行({name[:20]})缺少{'/'.join(hard)}")

        raw_time = text_value(row.get("采集时间"))
        if not raw_time:
            report.fail(f"第{i}行缺少采集时间")
        else:
            try:
                row_time = datetime.fromisoformat(raw_time)
                if require_complete and not FULL_ISO_TIME.match(raw_time):
                    # 完整门禁要求每行都有完整 ISO 时间戳：row_times 的
                    # 「文件日期 + 行时刻」回退跨日会猜错，采集时间猜不得。
                    report.fail(f"第{i}行采集时间不是完整 ISO 时间戳：{raw_time}")
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
    report.detail_complete_count = len(rows) - incomplete_rows
    if progress.get("detail_complete_count") != report.detail_complete_count:
        report.fail("manifest detail_complete_count 与 CSV 不一致")
    if require_complete and detail_errors:
        report.fail("详情字段不完整：" + "；".join(detail_errors[:10]))
    ambiguous = [name for name, count in unstable_name_counts.items() if name and count > 1]
    if ambiguous:
        message = "无条码/SKU/SPU同名商品无法稳定定位：" + "、".join(ambiguous[:10])
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
    if state == OBSERVED_MANIFEST_STATE:
        # 例外态最后校验：观测凭据、例外登记、逐屏列表证据都要齐。
        validate_observed_state(manifest, scope, artifacts, progress, rows, report,
                                manifest_dir=manifest_dir, expected=expected)
    return report


def format_gate_report(report: GateReport) -> str:
    lines = [f"manifest gate: {'PASS' if report.ok else 'FAIL'}",
             f"rows={report.row_count} expected={report.expected_count} "
             f"details={report.detail_complete_count} statuses={report.status_counts}"]
    lines += ["ERROR: " + item for item in report.errors]
    lines += ["WARN: " + item for item in report.warnings]
    return "\n".join(lines)
