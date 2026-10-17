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
MAX_STABLE_LIST_SHORTFALL = 2
MIN_STABLE_LIST_PASSES = 2


def atomic_state(path, value):
    C.atomic_write_json(path, value)


def load_inventory(path):
    value = C.read_json(path)
    segments = [segment for segment in value.get("segments", [])
                if segment.get("expected_count", 0) > 0]
    if not segments:
        raise C.CaptureContractError("类目分片清单为空")
    return value, segments


def segment_dir(root, segment):
    return os.path.join(root, f"{segment['order']:03d}")


def segment_manifest(directory):
    paths = [os.path.join(directory, name) for name in os.listdir(directory)
             if name.endswith(".manifest.json")] if os.path.isdir(directory) else []
    return max(paths, key=os.path.getmtime) if paths else None


def newest_resumable_segment(directory, segment):
    """全量回溯，选可信完整行最多的断点。

    success 分片若被更严格门禁判出污染，可作为修复父 manifest：scanner 会
    先剔除跨 run 重复再续采。连续快速失败工件也不会因 mtime 或窗口大小
    挤掉覆盖更高的有效断点。
    """
    paths = [os.path.join(directory, name) for name in os.listdir(directory)
             if name.endswith(".manifest.json")] if os.path.isdir(directory) else []
    candidates = []
    expected_category = f"{segment['top']}/{segment['leaf']}"
    profile = C.load_profile(PROFILE)
    for path in paths:
        report = C.validate_manifest(path, require_complete=False,
                                     expected_profile=profile)
        manifest = report.manifest
        if (report.ok and report.row_count > 0
                and (manifest.get("scope") or {}).get("category") == expected_category):
            candidates.append((_trusted_row_count(report.rows, complete_only=True),
                               os.path.getmtime(path), path))
    return max(candidates)[2] if candidates else None


def _row_capture_identity(row):
    return (C.text_value(row.get("条码")),
            C.normalize_name(row.get("商品名称")),
            C.normalize_name(row.get("规格")))


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


def stable_list_ceiling(directory, segment, *, max_shortfall=MAX_STABLE_LIST_SHORTFALL,
                        min_passes=MIN_STABLE_LIST_PASSES):
    """Return a safe visible-list ceiling after repeated complete bottom passes."""
    expected_category = f"{segment['top']}/{segment['leaf']}"
    profile = C.load_profile(PROFILE)
    by_count = {}
    if not os.path.isdir(directory):
        return None
    for name in os.listdir(directory):
        if not name.endswith(".manifest.json"):
            continue
        path = os.path.join(directory, name)
        report = C.validate_manifest(path, require_complete=False,
                                     expected_profile=profile)
        manifest = report.manifest
        progress = manifest.get("progress") or {}
        scope = manifest.get("scope") or {}
        if (not report.ok
                or manifest.get("state") not in {"partial", "interrupted"}
                or progress.get("stop_reason") != BOTTOM_STOP_REASON
                or scope.get("category") != expected_category
                or report.row_count <= 0
                or _trusted_row_count(report.rows, complete_only=True) != report.row_count
                or _cross_run_duplicate_count(report.rows)):
            continue
        shortfall = scope.get("expected_product_count", 0) - report.row_count
        if not 0 < shortfall <= max_shortfall:
            continue
        by_count.setdefault(report.row_count, []).append((os.path.getmtime(path), path, report))
    stable = [(count, items) for count, items in by_count.items()
              if len(items) >= min_passes]
    if not stable:
        return None
    count, items = max(stable, key=lambda item: (item[0], len(item[1])))
    _, path, report = max(items)
    return count, path, report


def promote_stable_segment(directory, segment):
    """Create an immutable success manifest for a proven stable list ceiling."""
    stable = stable_list_ceiling(directory, segment)
    if not stable:
        return None
    count, source_path, source_report = stable
    source = source_report.manifest
    promoted = json.loads(json.dumps(source))
    promoted["run_id"] = str(uuid.uuid4())
    promoted["parent_manifest_sha256"] = source_report.manifest_sha256
    promoted["state"] = "success"
    promoted["scope"]["expected_product_count"] = count
    promoted["progress"]["stop_reason"] = (
        f"平台标题计数高于连续完整到底可见上限；{MIN_STABLE_LIST_PASSES} 次稳定于 {count}，"
        f"按最多差 {MAX_STABLE_LIST_SHORTFALL} 条的窄门禁收敛。")
    prefix = os.path.join(directory, "qnh_scan_stable_" + datetime.now().strftime("%Y%m%d-%H%M%S")
                          + "_" + promoted["run_id"][:8])
    promoted["artifacts"]["evidence_prefix"] = os.path.splitext(
        promoted["artifacts"]["csv_path"])[0]
    path = prefix + ".manifest.json"
    C.atomic_write_json(path, promoted, overwrite=False)
    report = validate_segment(path, {**segment, "expected_count": count})
    if not report.ok:
        raise C.CaptureContractError(C.format_gate_report(report))
    return path, report


def validate_segment(path, segment, *, allow_count_refresh=False):
    report = C.validate_manifest(path, require_complete=True,
                                 expected_profile=C.load_profile(PROFILE))
    if not report.ok:
        return report
    manifest = report.manifest
    expected_path = f"{segment['top']}/{segment['leaf']}"
    if (manifest.get("scope") or {}).get("category") != expected_path:
        report.fail(f"分片类目不一致：{(manifest.get('scope') or {}).get('category')} != {expected_path}")
    live_count = report.row_count
    # 完整分片不得用后续 attempt 重复追加同一卡片来凑 expected_count。
    # 同一原始 run 内的重复出现仍允许（平台可能真有重复列表项）。
    cross_run_duplicates = _cross_run_duplicate_count(report.rows)
    if cross_run_duplicates:
        report.fail(f"分片含 {cross_run_duplicates} 条跨 run 重复追加记录")
    if allow_count_refresh and report.ok and live_count != segment["expected_count"]:
        segment["expected_count"] = live_count
    if live_count != segment["expected_count"]:
        report.fail(f"分片数量不一致：{live_count} != {segment['expected_count']}")
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
                 carry_baseline=None):
    directory = segment_dir(root, segment)
    os.makedirs(directory, exist_ok=True)
    last_error = ""
    for attempt in range(max_attempts):
        manifest_path = segment_manifest(directory)
        if manifest_path:
            report = validate_segment(manifest_path, segment, allow_count_refresh=True)
            if report.ok:
                return manifest_path, report
        promoted = promote_stable_segment(directory, segment)
        if promoted:
            path, report = promoted
            print(f"[segments] 连续完整到底后稳定于 {report.row_count} 条，"
                  f"收敛分片：{segment['top']}/{segment['leaf']}", flush=True)
            return path, report
        resume_path = newest_resumable_segment(directory, segment)
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
        stalled = 0.0
        killed_for_stall = False
        try:
            while process.poll() is None:
                time.sleep(poll_seconds)
                done = _attempt_progress(directory, started_after)[1]
                if done > baseline_done:
                    baseline_done = done
                    stalled = 0.0
                    continue
                stalled += poll_seconds
                if stalled >= stall_seconds:
                    warn = (f"[segments] 分片 {stalled:.0f}s 无有效行增长，发送"
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
        newest = segment_manifest(directory)
        if newest:
            report = validate_segment(newest, segment, allow_count_refresh=True)
            if report.ok:
                return newest, report
            last_error = C.format_gate_report(report)
        else:
            last_error = f"分片没有 manifest：{category}（exit={returncode}）"
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


def merge_segments(root, inventory, segments, state_path, *, full_count=None):
    profile = C.load_profile(PROFILE)
    state = C.read_json(state_path)
    capture_id = state["capture_id"]
    batch_id = state["batch_id"]
    run_id = str(uuid.uuid4())
    merged_rows = []
    segment_records = []
    seen = {}
    started = []
    finished = []
    evidence_prefixes = set()

    for segment in segments:
        manifest_path = state["completed"][str(segment["order"])]["manifest_path"]
        report = validate_segment(manifest_path, segment)
        if not report.ok:
            raise C.CaptureContractError(C.format_gate_report(report))
        manifest = report.manifest
        capture = manifest["capture"]
        started.append(datetime.fromisoformat(capture["started_at"]))
        finished.append(datetime.fromisoformat(capture["finished_at"]))
        evidence_prefixes.update(manifest["artifacts"].get("row_evidence_prefixes", []))
        start = len(merged_rows) + 1
        raw_count = len(report.rows)
        redundant_spec_rows = _redundant_spec_row_indexes(report.rows)
        kept = 0
        occurrence = {}
        for row_index, row in enumerate(report.rows):
            if row_index in redundant_spec_rows:
                continue
            base = (C.listing_identity(
                row.get("商品名称"), row.get("条码"), row.get("SKU"), row.get("SPU"))
                    + "|spec:" + C.normalize_name(row.get("规格")))
            occurrence[base] = occurrence.get(base, 0) + 1
            key = f"{base}#{occurrence[base]}"
            if key in seen:
                seen[key]["categories"].append([segment["top"], segment["leaf"]])
                continue
            copied = dict(row)
            copied["序号"] = str(len(merged_rows) + 1)
            merged_rows.append(copied)
            seen[key] = {"categories": [[segment["top"], segment["leaf"]]]}
            kept += 1
        end = len(merged_rows)
        segment_records.append({
            "order": segment["order"],
            "top": segment["top"],
            "leaf": segment["leaf"],
            "expected_count": segment["expected_count"],
            "captured_count": raw_count,
            "unique_count": kept,
            "duplicate_count": raw_count - kept,
            "state": "success",
            "row_start": start if kept else None,
            "row_end": end if kept else None,
            "manifest_path": os.path.abspath(manifest_path),
            "manifest_sha256": report.manifest_sha256,
        })

    realtime_full_count = full_count if full_count is not None else inventory["full_store_count"]
    union_count = len(merged_rows)

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
        "expected_product_count": union_count,
        "requested_count": 0,
        "raw_segment_product_count": sum(item["captured_count"] for item in segment_records),
        "cross_category_duplicate_count": sum(item["duplicate_count"] for item in segment_records),
        "realtime_full_store_count": realtime_full_count,
        "realtime_count_difference": union_count - realtime_full_count,
        "count_note": (
            "叶子类目分片跨时段采集；expected_product_count 为各成功分片去重后的时间窗并集。"
            "realtime_full_store_count 仅记录聚合时全部商品页读数，不作为阻断条件。"
        ),
        "segments": segment_records,
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
    manifest = C.build_manifest(
        capture_id=capture_id, batch_id=batch_id, run_id=run_id, state="success",
        profile=profile, device_id=DEVICE, scope=scope, capture=capture,
        artifacts=artifacts, rows=merged_rows, stop_reason="all leaf categories completed")
    manifest_path = prefix + ".manifest.json"
    C.atomic_write_json(manifest_path, manifest, overwrite=False)
    report = C.validate_manifest(manifest_path, require_complete=True, expected_profile=profile)
    if not report.ok:
        raise C.CaptureContractError(C.format_gate_report(report))
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
                        help="测试/离线聚合覆盖实时全部商品数")
    parser.add_argument("--carry-baseline",
                        help="每日增量携带基线 JSON，透传给各分片扫描")
    parser.add_argument("--commit", action="store_true",
                        help="聚合入库时直接提交（默认 dry-run 回滚）")
    args = parser.parse_args()
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

    if not args.aggregate_only:
        for index, segment in enumerate(segments, 1):
            key = str(segment["order"])
            existing = state["completed"].get(key)
            if existing and os.path.isfile(existing.get("manifest_path", "")):
                report = validate_segment(existing["manifest_path"], segment)
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
                                if args.carry_baseline else None))
            segment["expected_count"] = report.row_count
            state["completed"][key] = {
                "manifest_path": os.path.abspath(manifest_path),
                "manifest_sha256": report.manifest_sha256,
                "expected_count": report.row_count,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            }
            atomic_state(state_path, state)

    if len(state["completed"]) != len(segments):
        raise SystemExit(f"只完成 {len(state['completed'])}/{len(segments)} 个分片")
    full_count = (args.full_store_count if args.full_store_count is not None
                  else read_full_store_count())
    print(f"[segments] realtime full-store count={full_count}", flush=True)
    manifest_path = merge_segments(
        root, inventory, segments, state_path, full_count=full_count)
    print("[segments] aggregate manifest=" + manifest_path, flush=True)
    subprocess.run([
        sys.executable, PIPELINE, "--profile", PROFILE,
        "--from-manifest", manifest_path,
        *(["--commit"] if args.commit else []),
    ], check=True)


if __name__ == "__main__":
    main()
