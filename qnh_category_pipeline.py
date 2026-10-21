#!/usr/bin/env python3
"""Run and aggregate QNH leaf-category capture segments into one strict capture."""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime

import qnh_capture_common as C

ROOT = os.path.dirname(os.path.abspath(__file__))
PIPELINE = os.path.join(ROOT, "qnh_capture_pipeline.py")
DEFAULT_SEGMENTS = os.path.join(ROOT, "config", "qnh_category_segments.json")
PROFILE = "xiniu-changan"
DEVICE = "192.168.100.14:5555"  # Wi-Fi ADB；手机重启后需 USB 重新 adb tcpip 5555 + adb connect
BOTTOM_STOP_REASON = "多次翻页画面不再变化，已到列表底部。"
# 观测终态：观测做完了，业务没做完（有证据的计数差/渠道缺行）。
# 契约 v1 的 manifest 形状与例外凭据由 common 的 relaxed 门禁校验。
OBSERVED_STATE = "observed_with_exceptions"


def atomic_state(path, value):
    C.atomic_write_json(path, value)


def load_inventory(path):
    value = C.read_json(path)
    segments = [segment for segment in value.get("segments", [])
                if segment.get("expected_count", 0) > 0]
    if not segments:
        raise C.CaptureContractError("类目分片清单为空")
    value["source_path"] = os.path.abspath(path)
    return value, segments


def state_order_mismatches(state, segments):
    """capture.state.json 的 completed 分片集合与分片清单的差异（空＝一致）。

    只比数量会漏掉「拿了别的分片的工件顶替」：必须按 order 集合比。
    """
    completed = state.get("completed")
    if not isinstance(completed, dict):
        return ["completed 不是对象"]
    expected = {str(segment["order"]) for segment in segments}
    actual = {str(key) for key in completed}
    problems = []
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        problems.append("缺分片 " + "、".join(missing))
    if extra:
        problems.append("多分片 " + "、".join(extra))
    return problems


def verify_completed_manifest(record, manifest_path):
    """state 里记录的 manifest sha256 必须与磁盘上的文件一致。"""
    expected = C.text_value(record.get("manifest_sha256"))
    if not expected:
        raise C.CaptureContractError(f"state 记录缺少 manifest_sha256：{manifest_path}")
    actual = C.sha256_file(manifest_path)
    if actual != expected:
        raise C.CaptureContractError(
            f"分片 manifest 与 state 记录不一致：{manifest_path}\n"
            f"state={expected}\n实际={actual}")


def segment_dir(root, segment):
    return os.path.join(root, f"{segment['order']:03d}")


def _manifest_paths(directory):
    if not os.path.isdir(directory):
        return []
    return [os.path.join(directory, name) for name in os.listdir(directory)
            if name.endswith(".manifest.json")]


def tainted_manifests(directory, untrusted_paths):
    """不可信工件及其全部后代（parent_manifest_sha256 血缘闭包）。

    root 被换过 → 直接子工件、孙工件都不可信：既不当完成，也不当 resume
    起点，否则被替换的行会一路继承下去洗白。ponytail: 只按当前文件 sha 认
    血缘；root 被改过两次时中间那代认不出来，需要历史 sha 时再补。
    """
    roots = {os.path.abspath(path) for path in (untrusted_paths or ())}
    if not roots:
        return set()
    tainted = set(roots)
    tainted_shas = set()
    for path in roots:
        try:
            tainted_shas.add(C.sha256_file(path))
        except OSError:
            continue
    changed = True
    while changed:
        changed = False
        for path in _manifest_paths(directory):
            abspath = os.path.abspath(path)
            if abspath in tainted:
                continue
            try:
                manifest = C.read_json(path)
            except (OSError, ValueError, C.CaptureContractError):
                continue
            if (manifest or {}).get("parent_manifest_sha256") not in tainted_shas:
                continue
            tainted.add(abspath)
            try:
                tainted_shas.add(C.sha256_file(path))
            except OSError:
                pass
            changed = True
    return tainted


def segment_manifests(directory, exclude=None):
    """目录里的 manifest 路径；不可信工件及其血缘后代永不返回。"""
    tainted = tainted_manifests(directory, exclude)
    return [path for path in _manifest_paths(directory)
            if os.path.abspath(path) not in tainted]


def segment_manifest(directory, exclude=None):
    paths = segment_manifests(directory, exclude)
    return max(paths, key=os.path.getmtime) if paths else None


def manifest_snapshot(directory):
    """目录里现有 manifest 的 (路径 -> mtime)：判断本轮是否真产出新工件。"""
    snapshot = {}
    for path in segment_manifests(directory):
        try:
            snapshot[os.path.abspath(path)] = os.path.getmtime(path)
        except OSError:
            continue
    return snapshot


def _manifest_is_fresh(path, snapshot):
    """本轮新产出的工件：路径新出现，或原地重写过且 mtime 变了。"""
    key = os.path.abspath(path)
    if key not in snapshot:
        return True
    try:
        return os.path.getmtime(path) > snapshot[key]
    except OSError:
        return False


def newest_resumable_segment(directory, segment, exclude=None):
    """全量回溯，选可信完整行最多的断点。

    success 分片若被更严格门禁判出污染，可作为修复父 manifest：scanner 会
    先剔除跨 run 重复再续采。连续快速失败工件也不会因 mtime 或窗口大小
    挤掉覆盖更高的有效断点。

    exclude 是不可信工件（state 记录的哈希对不上）：它和它的子工件、孙工件
    （parent_manifest_sha256 血缘）都不能当断点。同目录其它可信工件照常可用。
    """
    # 不可信工件连同它的子工件、孙工件一起排除（血缘闭包）。
    paths = segment_manifests(directory, exclude)
    candidates = []
    expected_category = f"{segment['top']}/{segment['leaf']}"
    profile = C.load_profile(PROFILE)
    for path in paths:
        report = C.validate_manifest(path, require_complete=False,
                                     expected_profile=profile)
        manifest = report.manifest or {}
        if (report.ok and report.row_count > 0
                and "平台标题计数高于连续完整到底可见上限" not in
                (manifest.get("progress") or {}).get("stop_reason", "")
                and (manifest.get("scope") or {}).get("category") == expected_category):
            candidates.append((_trusted_row_count(report.rows, complete_only=True),
                               os.path.getmtime(path), path))
    return max(candidates)[2] if candidates else None


def _row_capture_identity(row):
    return (C.text_value(row.get("条码")),
            C.normalize_name(row.get("商品名称")),
            C.normalize_name(row.get("规格")))


# 卡级去重键：多 SKU 卡按 SPU（sidecar 给了真实身份），单卡沿用历史口径
# （条码优先的 listing_identity + 规格）——不改所有历史分片的单卡去重行为。
def _card_dedup_key(row, children):
    """卡级去重键：多 SKU 卡按 SPU，单卡按 listing_identity + 规格。

    规格留在单卡键里：同一 SKU 的不同规格是两条记录，绝不能互相吞掉。
    """
    if children:
        # 多 SKU 卡按 SPU 认卡（SPU=商品，同卡子行必须同 SPU，门禁已校验）。
        # 两次观测的 SKU 集合若不同 → fail closed，不合并也不丢弃（见
        # _fail_on_sku_set_change）。
        spus = sorted({C.text_value(child.get("SPU")) for child in children})
        return "multi:spu:" + "|".join(spus)
    # ponytail(qnh-single-vs-multi-not-merged): 同一商品在甲片是单 SKU 卡、在
    # 乙片是多 SKU 卡时，两张卡不合并（卡片级字段含义不同），并集会比全店
    # 计数多，覆盖门禁会显式失败而不是静默合并。
    return ("single:" + C.listing_identity(
        row.get("商品名称"), row.get("条码"), row.get("SKU"), row.get("SPU"))
        + "|spec:" + C.normalize_name(row.get("规格")))


def _card_sku_set(row, children):
    """一张卡观测到的 SKU 集合：多 SKU 卡取 sidecar 子行，单卡取自己的 SKU。"""
    skus = {C.text_value(child.get("SKU")) for child in children}
    skus.discard("")
    if skus:
        return skus
    sku = C.text_value(row.get("SKU"))
    return {sku} if sku else set()


def _fail_on_sku_set_change(kept, row, children, segment):
    """多 SKU 卡两次观测的 SKU 集合不同 → 失败，不合并、不丢弃。

    多 SKU 卡的 SKU 集合就是这张卡的内容（sidecar 是它的真实身份）：集合变了，
    保留一边会丢掉另一边的 SKU（跨片 SKU 误删），合并两边又会混字段。
    fail closed：直接报错，让人重采受影响分片。

    单卡沿用历史口径（条码即身份）：SKU id 变了当字段变动记进 divergences，
    不改所有历史分片的单卡合并行为。
    """
    if not children or not kept["children"]:
        return
    kept_skus = _card_sku_set(kept["row"], kept["children"])
    dropped_skus = _card_sku_set(row, children)
    if not kept_skus or not dropped_skus or kept_skus == dropped_skus:
        return
    raise C.CaptureContractError(
        "SKU集合跨时点变化无法合并：分片"
        f"{segment['order']} 的卡观测到 SKU {sorted(dropped_skus)}，分片"
        f"{kept['segment_order']} 的同一张卡是 SKU {sorted(kept_skus)}；"
        "不合并时间快照，也不丢弃任何一边的 SKU（请重采受影响分片后人工复核）")


def _segment_listings(manifest):
    """分片 sidecar 的 listings（卡片序号 -> SKU 子行）。没有多 SKU 就返回 {}。

    sidecar 的存在、哈希、结构已由分片门禁校验过：这里只取数据。
    """
    path = (manifest.get("artifacts") or {}).get("sku_path")
    if not path:
        return {}
    sidecar = C.read_json(path)
    return sidecar.get("listings") or {}


def _segment_observations(manifest):
    """分片 sidecar 的 observations（卡片序号 -> SKU -> 渠道 -> 缺行凭据）。

    聚合时凭据要跟着卡重编号一起搬：序号变了，observations 的下标也得改。
    """
    path = (manifest.get("artifacts") or {}).get("sku_path")
    if not path:
        return {}
    sidecar = C.read_json(path)
    return sidecar.get("observations") or {}


def _merged_detail_complete_count(rows, listings):
    """聚合后的详情完整卡数，口径与 common 门禁一致。

    多 SKU 卡：所有子行都无详情缺口才算完整——含「渠道行缺失记成未知」的
    子行（未知就是缺口，不能伪成完整）。单卡：row_detail_errors 为空才算完整。
    """
    complete = 0
    for row in rows:
        if C.is_multisku_row(row):
            children = listings.get(C.text_value(row.get("序号"))) or []
            if len(children) >= 2 and all(not C.row_detail_errors(child)
                                         for child in children):
                complete += 1
        elif not C.row_detail_errors(row):
            complete += 1
    return complete


def _merged_observation_exceptions(rows, listings, observations):
    """从聚合后的观测凭据反推「渠道行缺失」例外（sku / channel / spec）。

    例外清单跟着聚合后的实际数据走：凭据里有的才算数，凭据没有的绝不补写。
    """
    specs = {}
    for row in rows:
        seq = C.text_value(row.get("序号"))
        if not C.is_multisku_row(row):
            specs[(seq, C.text_value(row.get("SKU")))] = C.text_value(row.get("规格"))
    for seq, children in (listings or {}).items():
        for child in children:
            specs[(seq, C.text_value(child.get("SKU")))] = C.text_value(child.get("规格"))
    exceptions = []
    for seq in sorted(observations, key=lambda value: (0, int(value))
                      if str(value).isdigit() else (1, 0)):
        block = observations[seq]
        if not isinstance(block, dict):
            continue
        for sku in sorted(block):
            channels = block[sku]
            if not isinstance(channels, dict):
                continue
            spec = specs.get((seq, sku), "")
            for channel in sorted(channels):
                if not spec:
                    raise C.CaptureContractError(
                        f"聚合卡序号{seq} 的 SKU {sku} 缺规格，无法登记渠道行缺失例外")
                entry = {"type": "channel_row_absent", "sku": sku,
                         "channel": channel, "spec": spec}
                # 凭据原样带过来（区块状态/原因/详情 XML 证据）：聚合的例外要
                # 能单读自证，不许只留 sku/channel/spec。
                credential = channels[channel] or {}
                for key in ("observation", "section_status", "reason",
                            "evidence_pages"):
                    if credential.get(key) is not None:
                        entry[key] = credential[key]
                exceptions.append(entry)
    return exceptions


def _card_children(row, listings):
    """多 SKU 卡的 sidecar 子行；单卡返回空列表。"""
    if not C.is_multisku_row(row):
        return []
    return list(listings.get(C.text_value(row.get("序号"))) or [])


def _record_sku_spu(sku_spus, row, label):
    sku = C.text_value(row.get("SKU"))
    spu = C.text_value(row.get("SPU"))
    if not sku or not spu:
        return
    sku_spus.setdefault(sku, {}).setdefault(spu, label)


def _sku_spu_conflicts(sku_spus):
    """同一 SKU 指向多个 SPU：身份冲突，必须失败（不许合并、不许静默择一）。"""
    return [
        f"SKU {sku} 关联多个 SPU：" + "、".join(
            f"{spu}（{label}）" for spu, label in sorted(spus.items()))
        for sku, spus in sorted(sku_spus.items()) if len(spus) > 1
    ]


# 跨时点观测的比较口径：卡片级公共观测 + SKU 级详情（价格/库存变动才算分歧）。
_CARD_DIFF_COLUMNS = ("商品名称",) + tuple(C.CARD_COLUMNS)
_SKU_DIFF_COLUMNS = tuple(C.DETAIL_COLUMNS)


def _diff_columns(left, right, columns):
    return [column for column in columns
            if C.text_value(left.get(column)) != C.text_value(right.get(column))]


def _record_divergence(divergences, kept, row, children, segment, captured_at):
    """同卡跨时点观测：只记差异，绝不逐字段混合（保留最先出现那次观测）。

    被丢弃观测里的独有 SKU 也记下来：整行丢弃不等于这个 SKU 不存在。
    """
    if kept["segment_order"] == segment["order"]:
        # 同一分片内的重复列表项是同一时点的重复，不是时点分歧。
        # ponytail(qnh-same-segment-duplicate-kept): 同片重复的卡按平台列表
        # 原样各留一行（与单卡旧口径一致），sidecar 里同一 SKU 可能出现在两个
        # listing。upgrade when 平台不再出现同片重复卡。
        return
    card_fields = _diff_columns(kept["row"], row, _CARD_DIFF_COLUMNS)
    kept_children = {C.text_value(child.get("SKU")): child
                     for child in kept["children"]}
    dropped_children = {C.text_value(child.get("SKU")): child
                        for child in children}
    sku_entries = []
    for sku in sorted(set(kept_children) | set(dropped_children)):
        if not sku:
            continue
        left = kept_children.get(sku)
        right = dropped_children.get(sku)
        if left is None or right is None:
            fields = ["仅在一次观测中出现"]
        else:
            fields = _diff_columns(left, right, _SKU_DIFF_COLUMNS)
        if not fields:
            continue
        sku_entries.append({"sku": sku,
                            "spu": C.text_value((right or left).get("SPU")),
                            "fields": fields})
    if not children:
        sku = C.text_value(row.get("SKU"))
        if sku:
            fields = _diff_columns(kept["row"], row, _SKU_DIFF_COLUMNS)
            if fields:
                sku_entries.append({"sku": sku,
                                    "spu": C.text_value(row.get("SPU")),
                                    "fields": fields})
    if not card_fields and not sku_entries:
        return
    divergences.append({
        "kept": {"segment_order": kept["segment_order"],
                 "captured_at": kept["captured_at"]},
        "dropped": {"segment_order": segment["order"],
                    "captured_at": captured_at},
        "card_fields": card_fields,
        "skus": sku_entries,
    })


_BLANK_SPECS = {"", "-", "无"}


def _redundant_spec_row_indexes(rows):
    """Return same-run rendering duplicates without collapsing real variants."""
    groups = {}
    for index, row in enumerate(rows):
        barcode = C.text_value(row.get("条码"))
        sku = C.text_value(row.get("SKU"))
        if not barcode or not sku:
            continue
        key = (barcode, C.normalize_name(row.get("商品名称")), sku,
               C.text_value(row.get("状态")))
        spec = C.normalize_name(row.get("规格"))
        groups.setdefault(key, {}).setdefault(spec, []).append(index)

    redundant = set()
    for by_spec in groups.values():
        concrete = [spec for spec in by_spec if spec not in _BLANK_SPECS]
        blank = [spec for spec in by_spec if spec in _BLANK_SPECS]
        if len(concrete) > 1 or not blank:
            continue
        variants = ([concrete[0]] if concrete else []) + sorted(
            blank, key=lambda spec: (spec != "无", spec != "-", spec))
        if len(variants) < 2:
            continue
        keep_count = max(len(by_spec[spec]) for spec in variants)
        preferred = [index for spec in variants for index in by_spec[spec]]
        redundant.update(preferred[keep_count:])
    return redundant


def _trusted_row_count(rows, *, complete_only=False):
    """Count each identity at its largest occurrence count in any one run."""
    groups = {}
    for row in rows:
        if complete_only and (C.text_value(row.get("状态")) not in C.COMPLETE_ROW_STATUSES
                              or C.row_detail_errors(row)):
            continue
        prefix = os.path.abspath(row.get("证据前缀") or "")
        key = _row_capture_identity(row)
        by_prefix = groups.setdefault(key, {})
        by_prefix[prefix] = by_prefix.get(prefix, 0) + 1
    return sum(max(by_prefix.values()) for by_prefix in groups.values())


def _cross_run_duplicate_count(rows):
    return len(rows) - _trusted_row_count(rows)


def observation_exceptions(manifest):
    """manifest 里的观测异常清单（progress.exceptions）。"""
    progress = manifest.get("progress") or {}
    value = progress.get("exceptions")
    return value if isinstance(value, list) else []


def validate_segment(path, segment, *, allow_count_refresh=False, allow_observed=False):
    profile = C.load_profile(PROFILE)
    try:
        manifest_state = (C.read_json(path) or {}).get("state")
    except (OSError, ValueError):
        manifest_state = None
    observed = manifest_state == OBSERVED_STATE
    if observed and allow_observed:
        # 观测终态走宽松门禁：结构/哈希/行数照常校验，业务完整性不通过。
        # 例外凭据（platform_count_mismatch/channel_row_absent 逐条登记、短收的
        # 逐屏列表证据）由 common 的 relaxed 门禁校验，category 侧不重复实现。
        report = C.validate_manifest(path, require_complete=False,
                                     expected_profile=profile)
        if not report.ok:
            return report
    else:
        report = C.validate_manifest(path, require_complete=True,
                                     expected_profile=profile)
        if not report.ok:
            return report
    manifest = report.manifest
    lowered_count = "平台标题计数高于连续完整到底可见上限" in (
        (manifest.get("progress") or {}).get("stop_reason", ""))
    if lowered_count:
        report.fail("分片曾降低平台标题计数，不可作为完成结果")
    expected_path = f"{segment['top']}/{segment['leaf']}"
    if (manifest.get("scope") or {}).get("category") != expected_path:
        report.fail(f"分片类目不一致：{(manifest.get('scope') or {}).get('category')} != {expected_path}")
    live_count = report.row_count
    coverage = (manifest.get("progress") or {}).get("coverage") or {}
    platform_count = (manifest.get("scope") or {}).get("expected_product_count")
    # 完整分片不得用后续 attempt 重复追加同一卡片来凑 expected_count。
    # 同一原始 run 内的重复出现仍允许（平台可能真有重复列表项）。
    cross_run_duplicates = _cross_run_duplicate_count(report.rows)
    if cross_run_duplicates:
        report.fail(f"分片含 {cross_run_duplicates} 条跨 run 重复追加记录")
    # 目标数只能是 App 实时平台计数：静态清单的旧计数可以刷新成它，
    # 绝不能拿实抓行数改写目标（那会把真漏抓一起藏掉）。覆盖证明暂不承认，
    # 短收一律失败。
    if not isinstance(platform_count, int) or isinstance(platform_count, bool):
        report.fail(f"分片 manifest 缺少平台计数：{manifest.get('scope')}")
        return report
    if observed:
        # 观测终态：原目标数保留——既不刷新为平台计数，也不刷新为实抓行数。
        # 数量差只作提示：凭据（platform_count_mismatch + 逐屏列表证据）
        # 已由 common 的 relaxed 门禁验过。
        if live_count != platform_count:
            report.warnings.append(
                f"观测例外：分片实抓 {live_count} != 平台计数 {platform_count}"
                "（有观测凭据，目标数未改写）")
        if segment["expected_count"] != platform_count:
            report.warnings.append(
                f"分片目标数 {segment['expected_count']} != 平台计数 {platform_count}"
                "（观测终态保留原目标数）")
        return report
    if live_count != platform_count and not C.coverage_allows_shortfall(
            coverage, platform_count):
        report.fail(f"分片数量不一致：实抓 {live_count} != 平台计数 {platform_count}")
    if segment["expected_count"] != platform_count:
        # 曾经把平台计数改写过的分片，它的计数不可信：不许采纳。
        if allow_count_refresh and not lowered_count:
            segment["expected_count"] = platform_count
        else:
            report.fail(f"分片目标数与平台计数不一致："
                        f"{segment['expected_count']} != {platform_count}")
    return report


def _attempt_progress(directory, started_after):
    """当前 attempt 的有效进展：run 开始后修改的 CSV 的有效完成行数。

    只统计状态为 成功/自建商品（RESUME_DONE）的行——失败 run 会写
    空 CSV 或原样继承 resume 行，mtime/size 变化不代表真进展。
    """
    best = (0.0, 0)
    if not os.path.isdir(directory):
        return best
    for name in os.listdir(directory):
        if not name.endswith(".csv"):
            continue
        path = os.path.join(directory, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if stat.st_mtime <= started_after:
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            done = _trusted_row_count(rows, complete_only=True)
        except (OSError, csv.Error, UnicodeDecodeError):
            done = 0
        key = (stat.st_mtime, done)
        if key > best:
            best = key
    return best

def _navigation_progress(directory, started_after):
    latest = started_after
    for name in os.listdir(directory):
        if name.endswith(".navigation"):
            try:
                latest = max(latest, os.path.getmtime(os.path.join(directory, name)))
            except OSError:
                pass
    return latest


def _terminate_process_group(process):
    """SIGINT first so the scanner checkpoints; then terminate/kill as needed."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGINT)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.send_signal(signal.SIGINT)
        except OSError:
            pass
    try:
        process.wait(timeout=180)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass
    process.kill()
    process.wait()


def run_segment(root, segment, *, dismiss_order_dialogs, max_attempts=20,
                 stall_seconds=1200, poll_seconds=5, backoff_cap_seconds=60,
                 carry_baseline=None, force_fresh=False, excluded_manifests=None,
                 allow_observed=False):
    directory = segment_dir(root, segment)
    os.makedirs(directory, exist_ok=True)
    # state 记录的哈希对不上的工件不可信：既不当完成，也不当 resume 起点。
    excluded = {os.path.abspath(path) for path in (excluded_manifests or ())}
    # force_fresh：本轮必须真产出新 manifest，旧工件一律不算完成。
    snapshot = manifest_snapshot(directory) if force_fresh else {}
    checked = set()
    last_error = ""
    for attempt in range(max_attempts):
        if not force_fresh:
            # 已有可信工件：直接复用，不重采。
            manifest_path = segment_manifest(directory, exclude=excluded)
            if manifest_path:
                report = validate_segment(manifest_path, segment,
                                          allow_count_refresh=True,
                                          allow_observed=allow_observed)
                if report.ok:
                    return manifest_path, report
        resume_path = newest_resumable_segment(directory, segment, exclude=excluded)
        category = f"{segment['top']}/{segment['leaf']}"
        command = [
            sys.executable, PIPELINE,
            "--profile", PROFILE,
            "--device", DEVICE,
            "--out-dir", directory,
            "--category", category,
            "--count", "0",
            "--verify-every", "1",
            "--pace", "ultra",
            "--checkpoint-every", "25",
            "--tall-screen",
            "--capture-only",
            "--require-helper",
        ]
        if carry_baseline:
            command += ["--carry-baseline", carry_baseline]
        if allow_observed:
            # 观测模式：子进程按观测契约收口（终态 observed_with_exceptions），
            # 证据留在 manifest 里。子进程的 --observe-exceptions 只允许配
            # --capture-only，本命令本就带 --capture-only。
            command.append("--observe-exceptions")
        if dismiss_order_dialogs:
            command.append("--dismiss-order-dialogs")
        if resume_path:
            command += ["--resume", resume_path]
        print(f"[segments] attempt={attempt + 1}/{max_attempts} {category}", flush=True)
        resume_rows = 0
        if resume_path:
            try:
                resume_report = C.validate_manifest(
                    resume_path, require_complete=False,
                    expected_profile=C.load_profile(PROFILE))
                resume_rows = _trusted_row_count(
                    resume_report.rows, complete_only=True)
            except (OSError, csv.Error, UnicodeDecodeError):
                resume_rows = 0
        started_after = time.time() - 1
        process = subprocess.Popen(command, start_new_session=True)
        # 基线是 resume 载入的完成行数：当前 attempt 必须超过它才算真
        # 进展；原样继承旧行/写空 CSV 的快速失败不重置停滞计时。
        baseline_done = max(resume_rows,
                            _attempt_progress(directory, started_after)[1])
        navigation_progress = _navigation_progress(directory, started_after)
        stalled = 0.0
        killed_for_stall = False
        try:
            while process.poll() is None:
                time.sleep(poll_seconds)
                done = _attempt_progress(directory, started_after)[1]
                navigation = _navigation_progress(directory, started_after)
                if done > baseline_done or navigation > navigation_progress:
                    baseline_done = max(baseline_done, done)
                    navigation_progress = navigation
                    stalled = 0.0
                    continue
                stalled += poll_seconds
                if stalled >= stall_seconds:
                    warn = (f"[segments] 分片 {stalled:.0f}s 无新增商品或翻页进展，发送"
                            f" SIGINT 让扫描器存断点：{category}")
                    print(warn, flush=True)
                    _terminate_process_group(process)
                    killed_for_stall = True
                    break
        except KeyboardInterrupt:
            # 宿主收到 Ctrl-C 必须转发给子进程专属进程组，否则扫描器
            # 成孤儿继续占设备。等它落盘 checkpoint 后再上抛。
            print("[segments] 收到中断，转发 SIGINT 给扫描器进程组…", flush=True)
            _terminate_process_group(process)
            raise
        returncode = process.returncode
        if force_fresh:
            # 只有本轮真产出的新 manifest 才算完成：旧工件（含被替换过的）
            # 绝不能被"洗"成新哈希当完成。
            candidates = [path for path in segment_manifests(directory, exclude=excluded)
                          if _manifest_is_fresh(path, snapshot)]
            candidates.sort(key=os.path.getmtime, reverse=True)
        else:
            newest = segment_manifest(directory, exclude=excluded)
            candidates = [newest] if newest else []
        for path in candidates:
            if path in checked:
                continue
            checked.add(path)
            report = validate_segment(path, segment, allow_count_refresh=True,
                                      allow_observed=allow_observed)
            if report.ok:
                return path, report
            last_error = C.format_gate_report(report)
        if not candidates:
            last_error = (f"分片没有本轮新产出的 manifest：{category}（exit={returncode}）"
                          if force_fresh else
                          f"分片没有 manifest：{category}（exit={returncode}）")
        # 指数退避：环境未恢复时不再每 5 秒硬撞。cap 60s：attempt 之间
        # scanner 自身会重新检查设备/导航，故障恢复后理应尽快续采；300s
        # 的旧 cap 让一个 6 商品的小片空转 25 分钟（2026-10-12 shard46 实测）。
        backoff = min(5 * (2 ** attempt), backoff_cap_seconds)
        # 秒退且有效完成行数没超过基线＝环境类故障（Helper 掉线/设备弹窗），
        # 等更久再试。快速失败 run 常原样继承 resume 行，mtime 变化不算进展。
        if not killed_for_stall and returncode not in (0, None):
            if _attempt_progress(directory, started_after)[1] <= baseline_done:
                backoff = max(backoff, 45)
        print(f"[segments] attempt 失败（exit={returncode}），退避 {backoff}s", flush=True)
        time.sleep(backoff)
    raise C.CaptureContractError(f"分片重试耗尽：{category}\n{last_error}")


def read_full_store_count():
    import qnh_product_scan as scan
    scan.CTX.adb = scan.DEFAULT_ADB
    scan.CTX.serial = DEVICE
    scan.HelperState.allow_dump = True
    # 此路径不走 require_device()，熄屏时 tap 会被静默丢弃，先唤醒
    scan.sh("input keyevent KEYCODE_WAKEUP")
    if not scan.helper_setup():
        raise C.CaptureContractError("Helper 不可用，无法读取实时全部商品数")
    try:
        scan.tap_text_xml("全部商品", max_x=260, min_y=220, force_adb=True)
        deadline = time.time() + 8
        while time.time() < deadline:
            nodes = scan.parse_nodes(scan.dump_xml())
            count = scan.selected_list_count(nodes, "全部商品")
            if count is not None:
                return count
            time.sleep(0.4)
    finally:
        scan.helper_teardown()
    raise C.CaptureContractError("未读到实时全部商品数")


def observed_segment_report(state, segments):
    """completed 分片里的观测终态分片。

    返回 [(order, 类目路径, manifest 路径, 异常清单)]：异常清单原样带出，
    报告里要看得见"哪一片、差在哪、证据是什么"。
    """
    found = []
    completed = state.get("completed") or {}
    for segment in segments:
        record = completed.get(str(segment["order"])) or {}
        path = record.get("manifest_path")
        if not path or not os.path.isfile(path):
            continue
        try:
            manifest = C.read_json(path)
        except (OSError, ValueError):
            continue
        if manifest.get("state") == OBSERVED_STATE:
            found.append((segment["order"],
                          f"{segment['top']}/{segment['leaf']}",
                          path, observation_exceptions(manifest)))
    return found


def merge_segments(root, inventory, segments, state_path, *, full_count=None,
                   full_count_source="device", allow_observed=False):
    profile = C.load_profile(PROFILE)
    state = C.read_json(state_path)
    if full_count is None:
        raise C.CaptureContractError(
            "聚合必须显式提供实时全店计数（full_count）：静态清单里的历史计数"
            "不能拿来证明全店覆盖")
    problems = state_order_mismatches(state, segments)
    if problems:
        raise C.CaptureContractError(
            "capture.state.json 的 completed 分片集合与分片清单不一致：" + "；".join(problems))
    completed = state["completed"]
    observed = observed_segment_report(state, segments)
    if observed and not allow_observed:
        # 观测终态分片有证据但业务不完整：绝不当成功聚合，也不静默丢掉。
        listing = "；".join(
            f"分片{order} {category}（manifest={path}）异常="
            + json.dumps(exceptions, ensure_ascii=False)
            for order, category, path, exceptions in observed)
        raise C.CaptureContractError(
            "分片含观测终态（observed_with_exceptions），不允许聚合成成功结果："
            + listing)
    capture_id = state["capture_id"]
    batch_id = state["batch_id"]
    run_id = str(uuid.uuid4())
    merged_rows = []
    merged_listings = {}
    merged_observations = {}
    segment_records = []
    observed_segments = []
    seen = {}
    started = []
    finished = []
    evidence_prefixes = set()
    sku_row_count = 0
    divergences = []
    sku_spus = {}

    for segment in segments:
        record = completed[str(segment["order"])]
        manifest_path = record.get("manifest_path")
        if not manifest_path or not os.path.isfile(manifest_path):
            raise C.CaptureContractError(f"分片 manifest 不存在：{manifest_path}")
        verify_completed_manifest(record, manifest_path)
        report = validate_segment(manifest_path, segment,
                                   allow_observed=allow_observed)
        if not report.ok:
            raise C.CaptureContractError(C.format_gate_report(report))
        manifest = report.manifest
        capture = manifest["capture"]
        started.append(datetime.fromisoformat(capture["started_at"]))
        finished.append(datetime.fromisoformat(capture["finished_at"]))
        evidence_prefixes.update(manifest["artifacts"].get("row_evidence_prefixes", []))
        listings = _segment_listings(manifest)
        observations = _segment_observations(manifest)
        manifest_state = C.text_value(manifest.get("state"))
        # 平台计数只认 manifest 里那一个（分片的 App 实时标题数），
        # 静态清单里的历史数字不参与。
        segment_platform_count = (manifest.get("scope") or {}).get("expected_product_count")
        # 跨分片 SKU 身份核对：同一 SKU 指向不同 SPU 立刻失败——不许合并、
        # 不许静默择一。多 SKU 卡的子行也参与核对。
        for index, row in enumerate(report.rows, 1):
            _record_sku_spu(sku_spus, row, f"分片{segment['order']}第{index}行")
            for position, child in enumerate(_card_children(row, listings), 1):
                _record_sku_spu(sku_spus, child,
                                f"分片{segment['order']}序号"
                                f"{C.text_value(row.get('序号'))}第{position}个SKU")
        conflicts = _sku_spu_conflicts(sku_spus)
        if conflicts:
            raise C.CaptureContractError("SKU 身份冲突（跨分片）："
                                          + "；".join(conflicts))
        start = len(merged_rows) + 1
        raw_count = len(report.rows)
        redundant_spec_rows = _redundant_spec_row_indexes(report.rows)
        kept = 0
        occurrence = {}
        for row_index, row in enumerate(report.rows):
            if row_index in redundant_spec_rows:
                continue
            children = _card_children(row, listings)
            if C.is_multisku_row(row) and len(children) < 2:
                raise C.CaptureContractError(
                    f"分片{segment['order']} 多SKU卡片 序号"
                    f"{C.text_value(row.get('序号'))} 的 sidecar 子行缺失"
                    "（门禁应已拦截，这里是防御）")
            card_key = _card_dedup_key(row, children)
            occurrence[card_key] = occurrence.get(card_key, 0) + 1
            key = f"{card_key}#{occurrence[card_key]}"
            if key in seen:
                # 同卡跨类目重复：整卡丢弃，SKU 不重复入库；差异只记不混。
                # SKU 集合变了就不许丢：先 fail closed。
                seen[key]["categories"].append([segment["top"], segment["leaf"]])
                _fail_on_sku_set_change(seen[key], row, children, segment)
                _record_divergence(divergences, seen[key], row, children,
                                   segment, capture.get("started_at"))
                continue
            copied = dict(row)
            new_seq = str(len(merged_rows) + 1)
            copied["序号"] = new_seq
            merged_rows.append(copied)
            if children:
                # 子行随聚合重编号：sidecar 下标 = 聚合后主 CSV 序号，子行的
                # 序号/标题/采集时间/证据前缀与父卡严格同步。
                merged_listings[new_seq] = [{**child, "序号": new_seq}
                                            for child in children]
                sku_row_count += len(children)
            else:
                sku_row_count += 1
            # 缺行凭据跟着卡一起搬：下标改成聚合后的序号，凭据内容原样保留。
            card_observations = observations.get(C.text_value(row.get("序号")))
            if card_observations:
                merged_observations[new_seq] = card_observations
            seen[key] = {"categories": [[segment["top"], segment["leaf"]]],
                         "row": copied, "children": children,
                         "segment_order": segment["order"],
                         "captured_at": capture.get("started_at")}
            kept += 1
        end = len(merged_rows)
        coverage = (manifest.get("progress") or {}).get("coverage") or {}
        segment_exceptions = observation_exceptions(manifest)
        segment_records.append({
            "order": segment["order"],
            "top": segment["top"],
            "leaf": segment["leaf"],
            "expected_count": segment_platform_count,
            "captured_count": raw_count,
            "unique_count": kept,
            "duplicate_count": raw_count - kept,
            "state": manifest_state,
            "coverage": coverage or None,
            "row_start": start if kept else None,
            "row_end": end if kept else None,
            "manifest_path": os.path.abspath(manifest_path),
            "manifest_sha256": report.manifest_sha256,
        })
        if manifest_state == OBSERVED_STATE:
            observed_segments.append({
                "order": segment["order"],
                "category": f"{segment['top']}/{segment['leaf']}",
                "platform_count": segment_platform_count,
                "captured_count": raw_count,
                "manifest_path": os.path.abspath(manifest_path),
                "exceptions": segment_exceptions,
            })
            segment_records[-1]["observation_exceptions"] = segment_exceptions

    realtime_full_count = full_count
    union_count = len(merged_rows)
    difference = union_count - realtime_full_count
    platform_count = sum(item["expected_count"] for item in segment_records)
    # 静态类目清单（config/qnh_category_segments.json）是历史快照，没有与 App
    # 实时类目树回验过：不能拿它自证全店覆盖，只能显式标记未回验。
    inventory_verified = False
    coverage_problems = []
    if difference != 0:
        coverage_problems.append(
            f"聚合 {union_count} 行 ≠ 实时全店计数 {realtime_full_count}"
            "（分片非同时点采集，差额不等同漏抓数量，不推断缺口）")
    full_store_coverage = {
        "status": "verified" if not coverage_problems else "unverified",
        "reason": "；".join(coverage_problems),
        "coverage_mode": C.FULL_STORE_COVERAGE_MODE,
        "category_inventory_verified": inventory_verified,
        "category_inventory_source": inventory.get("source_path"),
        "category_inventory_captured_at": inventory.get("captured_at"),
        "segment_count": len(segment_records),
        "platform_product_count": platform_count,
        "union_count": union_count,
        "realtime_full_store_count": realtime_full_count,
        "full_count_source": full_count_source,
    }

    prefix = os.path.join(root, "qnh_category_full_" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    csv_path = prefix + ".csv"
    with open(csv_path + ".tmp", "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
        writer.writeheader()
        writer.writerows(merged_rows)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(csv_path + ".tmp", csv_path)

    scope = {
        "mode": C.SEGMENT_MODE,
        "list_title": "全部商品",
        "category": None,
        "coverage_mode": C.FULL_STORE_COVERAGE_MODE,
        "expected_product_count": union_count,
        "requested_count": 0,
        "raw_segment_product_count": sum(item["captured_count"] for item in segment_records),
        "cross_category_duplicate_count": sum(item["duplicate_count"] for item in segment_records),
        "platform_product_count": platform_count,
        "realtime_full_store_count": realtime_full_count,
        "realtime_count_difference": difference,
        "full_store_coverage": full_store_coverage,
        "count_note": (
            "叶子类目分片跨时段采集；expected_product_count 为各成功分片去重后的时间窗并集，"
            "不等于某一时刻的全店快照。coverage_mode=count_reconciled_time_window："
            "realtime_full_store_count 是聚合时从 App「全部商品」页读到的实时全店计数，"
            "与并集相等即按数量对账通过（仍非同一时点快照）；不等则全店覆盖判定 unverified，"
            "差额不等同漏抓数量，拒绝完整入库。"
        ),
        "segments": segment_records,
        # 时点策略：同一 SKU/卡片被多个分片在不同时间点观测到时，保留分片清单
        # 顺序里最先出现的那次观测（字段原样保留），后续观测整行丢弃——绝不
        # 逐字段混合。差异逐条记在 sku_time_divergences 里。
        "sku_time_policy": {
            "policy": "first_observation_wins",
            "description": (
                "同一 SKU/卡片被多个分片在不同时间点观测到时，保留分片清单顺序里"
                "最先出现的那次观测（字段原样保留），后续观测整行丢弃，绝不逐字段"
                "混合；差异见 scope.sku_time_divergences。"),
        },
        "sku_time_divergences": divergences,
    }
    capture = {
        "method": "app_ui",
        "started_at": min(started).isoformat(timespec="seconds"),
        "run_started_at": min(started).isoformat(timespec="seconds"),
        "finished_at": max(finished).isoformat(timespec="seconds"),
        "collector_version": "qnh-category-segments-v1",
        "pace": "ultra",
        "tall_screen": True,
        "verify_every": 1,
        "checkpoint_every": 25,
        "record": False,
        "dismiss_order_dialogs": state.get("dismiss_order_dialogs", False),
    }
    artifacts = {
        "csv_path": csv_path,
        "evidence_prefix": prefix,
        "video_path": None,
        "csv_sha256": C.sha256_file(csv_path),
        "segment_evidence_prefixes": sorted(evidence_prefixes),
    }
    # 例外态聚合：分片里有观测终态，聚合就是观测终态——绝不写成 success。
    # 例外清单 = 分片级计数差（按类目标明）+ 聚合数据里真实的渠道行缺失。
    channel_exceptions = _merged_observation_exceptions(
        merged_rows, merged_listings, merged_observations)
    count_exceptions = []
    for item in observed_segments:
        for exception in item["exceptions"]:
            if exception.get("type") == "platform_count_mismatch":
                count_exceptions.append({
                    "type": "segment_platform_count_mismatch",
                    "segment": item["category"],
                    "platform_count": exception.get("platform_count"),
                    "visible_card_count": exception.get("visible_card_count"),
                })
    aggregate_exceptions = count_exceptions + channel_exceptions
    observed_state = bool(observed_segments)
    if observed_state:
        capture["observation_contract"] = C.OBSERVATION_CONTRACT_VERSION
        scope["observation"] = {
            "contract": C.OBSERVATION_CONTRACT_VERSION,
            "observed_segment_count": len(observed_segments),
            "business_complete": False,
            "segments": observed_segments,
        }
        scope["count_note"] += (
            " 本次聚合含观测终态分片（observed_with_exceptions）：聚合终态也是"
            "观测终态，business_complete=false——观测完成不等于业务完整，严格"
            "门禁拒绝、入库入口在连库之前拒绝。")
    if merged_listings or merged_observations:
        # 多 SKU 卡的 SKU 子行随聚合一起落 sidecar：主 CSV 仍是一卡一行，
        # 子行的完整 40 列不进主 CSV。下标是聚合后主 CSV 的序号。
        # observations 是「渠道行缺失」的凭据（凭详情页 XML 复解析），下标
        # 同样跟聚合后的序号。
        sku_path = prefix + ".skus.json"
        C.atomic_write_json(sku_path, {
            "schema_version": C.SKU_SIDECAR_SCHEMA_VERSION,
            "listings": merged_listings,
            **({"observations": merged_observations} if merged_observations else {}),
        }, overwrite=False)
        artifacts["sku_path"] = sku_path
        artifacts["sku_sha256"] = C.sha256_file(sku_path)
        scope["sku_mode"] = C.SKU_SIDECAR_MODE
    manifest = C.build_manifest(
        capture_id=capture_id, batch_id=batch_id, run_id=run_id,
        state=OBSERVED_STATE if observed_state else "success",
        profile=profile, device_id=DEVICE, scope=scope, capture=capture,
        artifacts=artifacts, rows=merged_rows,
        stop_reason=("all leaf categories observed (exceptions recorded)"
                     if observed_state else "all leaf categories completed"),
        detail_complete_count=_merged_detail_complete_count(
            merged_rows, merged_listings))
    # 两类计数分别记：卡片数（listing_count）与 SKU 行数（sku_row_count）。
    manifest["progress"]["listing_count"] = len(merged_rows)
    manifest["progress"]["sku_row_count"] = sku_row_count
    if observed_state:
        manifest["progress"].update({
            "observation_complete": True,
            "business_complete": False,
            "exceptions": aggregate_exceptions,
            "observation_count": len(channel_exceptions),
        })
    manifest_path = prefix + ".manifest.json"
    C.atomic_write_json(manifest_path, manifest, overwrite=False)
    report = C.validate_manifest(manifest_path,
                                 require_complete=not observed_state,
                                 expected_profile=profile)
    if not report.ok:
        raise C.CaptureContractError(
            f"聚合 manifest 未通过{'观测' if observed_state else '完整'}门禁："
            f"{manifest_path}\n" + C.format_gate_report(report))
    if observed_state:
        # 观测终态聚合必须仍被严格门禁拒绝：它不是业务完整的快照，入库入口
        # 也要在连库之前拒它。这里把这条不变量钉死。
        strict = C.validate_manifest(manifest_path, require_complete=True,
                                     expected_profile=profile)
        if strict.ok:
            raise C.CaptureContractError(
                f"观测终态聚合被严格门禁放行（不该发生）：{manifest_path}")
    return manifest_path


def main():
    parser = argparse.ArgumentParser(description="牵牛花按叶子类目分片采集")
    parser.add_argument("--segments-file", default=DEFAULT_SEGMENTS)
    parser.add_argument("--out-dir", default=os.path.join(ROOT, "qnh_category_capture"))
    parser.add_argument("--dismiss-order-dialogs", action="store_true")
    parser.add_argument("--segment-max-attempts", type=int, default=20)
    parser.add_argument("--segment-stall-seconds", type=int, default=1200,
                        help="分片产物无进展多少秒后 SIGINT 收断点并重试")
    parser.add_argument("--segment-poll-seconds", type=int, default=5)
    parser.add_argument("--segment-backoff-cap-seconds", type=int, default=60)
    parser.add_argument("--aggregate-only", action="store_true")
    parser.add_argument("--full-store-count", type=int,
                        help="测试/离线聚合覆盖实时全部商品数"
                             "（人工信任边界：未真机复验，只在离线演练用）")
    parser.add_argument("--carry-baseline",
                        help="已停用：携带行的详情是历史合成值，完整入库拒绝携带行")
    parser.add_argument("--commit", action="store_true",
                        help="聚合入库时直接提交（默认 dry-run 回滚）")
    parser.add_argument("--capture-only", action="store_true",
                        help="聚合与门禁通过后只保存 manifest 并退出，不执行入库/"
                             "dry-run（不连库）")
    parser.add_argument("--observe-exceptions", action="store_true",
                        help="仅 --capture-only：分片允许以观测终态收口"
                             "（observed_with_exceptions，证据留在 manifest 里），"
                             "业务完整性仍不通过")
    args = parser.parse_args()
    if args.observe_exceptions and not args.capture_only:
        # 先于任何操作失败：不建目录、不连设备。
        parser.error("--observe-exceptions 只能与 --capture-only 一起使用")
    if args.observe_exceptions and args.commit:
        parser.error("--observe-exceptions 与 --commit 互斥")
    if args.carry_baseline:
        # 携带行不再入库：进程启动前就停，别白跑 20 轮分片。
        parser.error("--carry-baseline 已停用：携带行详情是历史合成值，"
                     "必须重新实访详情页")
    inventory, segments = load_inventory(args.segments_file)
    root = os.path.abspath(args.out_dir)
    os.makedirs(root, exist_ok=True)
    state_path = os.path.join(root, "capture.state.json")
    if os.path.isfile(state_path):
        state = C.read_json(state_path)
        for segment in segments:
            completed = state.get("completed", {}).get(str(segment["order"]))
            if completed and isinstance(completed.get("expected_count"), int):
                segment["expected_count"] = completed["expected_count"]
    else:
        state = {
            "schema_version": 1,
            "capture_id": str(uuid.uuid4()),
            "batch_id": str(uuid.uuid4()),
            "dismiss_order_dialogs": bool(args.dismiss_order_dialogs),
            "completed": {},
        }
        atomic_state(state_path, state)
    # 被换过的不可信工件跨轮次留档：之后既不当完成，也不当 resume 起点。
    untrusted = {os.path.abspath(path)
                 for path in (state.get("untrusted_manifests") or [])}

    if not args.aggregate_only:
        for index, segment in enumerate(segments, 1):
            key = str(segment["order"])
            existing = state["completed"].get(key)
            force_fresh = False
            if existing and os.path.isfile(existing.get("manifest_path", "")):
                try:
                    # state 里记的哈希与磁盘工件必须一致：不一致就重跑，不能拿
                    # 被换过的工件顶替已完成分片。
                    verify_completed_manifest(existing, existing["manifest_path"])
                except C.CaptureContractError as exc:
                    print(f"[segments] {index}/{len(segments)} state 工件哈希不符，重跑 "
                          f"{segment['top']}/{segment['leaf']}：{exc}", flush=True)
                    # 被替换过的工件不可信：绝不当完成、也不当 resume 起点；必须
                    # 重新采出新的 manifest 才算这个分片完成。旧工件保留留档。
                    untrusted.add(os.path.abspath(existing["manifest_path"]))
                    state["untrusted_manifests"] = sorted(untrusted)
                    atomic_state(state_path, state)
                    force_fresh = True
                else:
                    report = validate_segment(existing["manifest_path"], segment,
                                              allow_observed=args.observe_exceptions)
                    if report.ok:
                        print(f"[segments] {index}/{len(segments)} skip {segment['top']}/{segment['leaf']} ({segment['expected_count']})", flush=True)
                        continue
            print(f"[segments] {index}/{len(segments)} run {segment['top']}/{segment['leaf']} ({segment['expected_count']})", flush=True)
            manifest_path, report = run_segment(
                root, segment, dismiss_order_dialogs=args.dismiss_order_dialogs,
                max_attempts=args.segment_max_attempts,
                stall_seconds=args.segment_stall_seconds,
                poll_seconds=args.segment_poll_seconds,
                backoff_cap_seconds=args.segment_backoff_cap_seconds,
                carry_baseline=(os.path.abspath(args.carry_baseline)
                                if args.carry_baseline else None),
                force_fresh=force_fresh, excluded_manifests=untrusted,
                allow_observed=args.observe_exceptions)
            # 留档平台计数（不是实抓行数）：平台 52 / 实抓 51 这个差必须看得见。
            segment["expected_count"] = report.expected_count
            manifest_state = (report.manifest or {}).get("state")
            if not isinstance(manifest_state, str):
                manifest_state = ""
            state["completed"][key] = {
                "manifest_path": os.path.abspath(manifest_path),
                "manifest_sha256": report.manifest_sha256,
                "expected_count": report.expected_count,
                "captured_count": report.row_count,
                # 平台计数与实抓行数分开记：观测终态下两者可以不等。
                "platform_expected_count": report.expected_count,
                "platform_actual_count": report.row_count,
                "state": manifest_state,
                "business_complete": manifest_state == "success",
                "observation_complete": manifest_state == OBSERVED_STATE,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            }
            atomic_state(state_path, state)

    problems = state_order_mismatches(state, segments)
    if problems:
        raise SystemExit("capture.state.json 的 completed 分片集合与分片清单不一致："
                         + "；".join(problems))
    if args.full_store_count is not None:
        full_count = args.full_store_count
        full_count_source = "cli_override"
    else:
        full_count = read_full_store_count()
        full_count_source = "device"
    print(f"[segments] realtime full-store count={full_count} "
          f"(source={full_count_source})", flush=True)
    manifest_path = merge_segments(
        root, inventory, segments, state_path, full_count=full_count,
        full_count_source=full_count_source,
        allow_observed=args.observe_exceptions)
    print("[segments] aggregate manifest=" + manifest_path, flush=True)
    if args.capture_only:
        # capture-only：只落 manifest，不连库、不执行入库 dry-run。
        print("[segments] capture-only：只保存 manifest，不入库", flush=True)
        return
    subprocess.run([
        sys.executable, PIPELINE, "--profile", PROFILE,
        "--from-manifest", manifest_path,
        *(["--commit"] if args.commit else []),
    ], check=True)


if __name__ == "__main__":
    main()
