#!/usr/bin/env python3
"""One-command 牵牛花 capture → gate → transactional DB load → verification."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import uuid
from datetime import datetime

import qnh_capture_common as C
import qnh_load_db as loader
import qnh_validate_db as validator

ROOT = os.path.dirname(os.path.abspath(__file__))
SCANNER = os.path.join(ROOT, "qnh_product_scan.py")


def _result_path(manifest_path):
    suffix = ".manifest.json"
    return (manifest_path[:-len(suffix)] if manifest_path.endswith(suffix)
            else os.path.splitext(manifest_path)[0]) + ".result.json"


def _print_gate(report):
    print(C.format_gate_report(report), flush=True)


def _resume_hint(manifest_path, args):
    try:
        manifest = C.read_json(manifest_path) if os.path.isfile(manifest_path) else {}
    except Exception:
        manifest = {}
    profile = manifest.get("profile") or {}
    scope = manifest.get("scope") or {}
    capture = manifest.get("capture") or {}
    artifacts = manifest.get("artifacts") or {}
    csv_path = artifacts.get("csv_path")
    if csv_path:
        csv_path = (os.path.abspath(csv_path) if os.path.isabs(csv_path)
                    else os.path.abspath(os.path.join(os.path.dirname(manifest_path), csv_path)))
    out_dir = os.path.dirname(csv_path) if csv_path else os.path.abspath(args.out_dir)
    device_value = manifest.get("device_id")
    device = device_value if isinstance(device_value, str) and device_value else args.device
    category_value = scope.get("category") if "category" in scope else args.category
    category = category_value if isinstance(category_value, str) else args.category
    expected_count = scope.get("expected_product_count")
    count = (expected_count
             if isinstance(expected_count, int) and not isinstance(expected_count, bool)
             and expected_count > 0
             else args.count if args.count is not None else 0)
    verify_value = capture.get("verify_every")
    verify_every = (verify_value if isinstance(verify_value, int)
                    and not isinstance(verify_value, bool) and verify_value >= 0
                    else args.verify_every)
    pace_value = capture.get("pace")
    pace = (pace_value if isinstance(pace_value, str)
            and pace_value in {"ultra", "fast", "natural", "demo"} else args.pace)
    checkpoint_value = capture.get("checkpoint_every")
    checkpoint_every = (checkpoint_value if isinstance(checkpoint_value, int)
                        and not isinstance(checkpoint_value, bool) and checkpoint_value >= 0
                        else args.checkpoint_every)
    tall_value = capture.get("tall_screen")
    tall_screen = tall_value if isinstance(tall_value, bool) else args.tall_screen
    record_value = capture.get("record")
    record = record_value if isinstance(record_value, bool) else args.record
    dismiss_value = capture.get("dismiss_order_dialogs")
    dismiss_order_dialogs = (dismiss_value if isinstance(dismiss_value, bool)
                             else getattr(args, "dismiss_order_dialogs", False))
    profile_value = profile.get("name")
    profile_name = (profile_value if isinstance(profile_value, str) and profile_value
                    else args.profile or "xiniu-changan")
    command = [
        sys.executable, os.path.join(ROOT, "qnh_capture_pipeline.py"),
        "--profile", profile_name,
        "--profiles-file", os.path.abspath(args.profiles_file),
        "--out-dir", out_dir,
        "--resume", os.path.abspath(manifest_path),
        "--count", str(count),
        "--verify-every", str(verify_every),
        "--pace", pace,
        "--checkpoint-every", str(checkpoint_every),
    ]
    if device:
        command += ["--device", device]
    if category:
        command += ["--category", category]
    command.append("--tall-screen" if tall_screen else "--no-tall-screen")
    if record:
        command.append("--record")
    if dismiss_order_dialogs:
        command.append("--dismiss-order-dialogs")
    # 安全开关按 manifest 推导，不只看本次命令行：观测契约产物续跑必须带
    # --observe-exceptions（否则扫描器直接拒），而 --observe-exceptions 只能
    # 与 --capture-only 同用——两个一起补上，给出的命令才真能跑。
    observed_artifact = bool(C.text_value(capture.get("observation_contract")))
    if getattr(args, "capture_only", False) or observed_artifact:
        command.append("--capture-only")
    if getattr(args, "observe_exceptions", False) or observed_artifact:
        command.append("--observe-exceptions")
    if getattr(args, "require_helper", False):
        command.append("--require-helper")
    return shlex.join(command)


def _is_aggregate(manifest):
    """分片聚合产物（scope.mode=segment）不是一次采集，不能当断点续跑。"""
    scope = (manifest or {}).get("scope") or {}
    return scope.get("mode") == C.SEGMENT_MODE


def _restart_hint(args):
    command = [
        sys.executable, os.path.join(ROOT, "qnh_capture_pipeline.py"),
        "--profile", args.profile,
        "--profiles-file", os.path.abspath(args.profiles_file),
        "--out-dir", os.path.abspath(args.out_dir),
        "--count", str(args.count if args.count is not None else 0),
        "--verify-every", str(args.verify_every),
        "--pace", args.pace,
        "--checkpoint-every", str(args.checkpoint_every),
    ]
    if args.device:
        command += ["--device", args.device]
    if args.category:
        command += ["--category", args.category]
    command.append("--tall-screen" if args.tall_screen else "--no-tall-screen")
    if args.record:
        command.append("--record")
    if getattr(args, "dismiss_order_dialogs", False):
        command.append("--dismiss-order-dialogs")
    if getattr(args, "capture_only", False):
        command.append("--capture-only")
    if getattr(args, "observe_exceptions", False):
        command.append("--observe-exceptions")
    if getattr(args, "require_helper", False):
        command.append("--require-helper")
    return shlex.join(command)


def _post_validate(manifest_path, result):
    # 观测终态产物按宽松门禁复核：它不是「业务完整」的快照，但入库是允许的
    # （未知的渠道格记 unknown，快照状态 partial）。严格门禁仍然会说它不完整。
    observed = False
    try:
        manifest = C.read_json(manifest_path)
        observed = manifest.get("state") == C.OBSERVED_MANIFEST_STATE
    except Exception:
        observed = False
    gate = C.validate_manifest(manifest_path, require_complete=not observed)
    if not gate.ok:
        raise C.CaptureContractError(C.format_gate_report(gate))
    manifest = gate.manifest
    conn = loader._open_connection()
    try:
        report = validator.validate_snapshot(
            conn, manifest["artifacts"]["csv_path"], result["snapshot_id"],
            manifest_path=manifest_path)
    finally:
        conn.close()
    validator.print_report(report)
    return report


def run_capture(args, profile):
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    capture_id = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    parent = None
    parent_path = None
    resume_arg = args.resume
    if resume_arg and str(resume_arg).endswith(".json"):
        parent_path = os.path.abspath(resume_arg)
    elif resume_arg:
        sibling = C.source_prefix(os.path.abspath(resume_arg)) + ".manifest.json"
        if os.path.isfile(sibling):
            parent_path = sibling
    if parent_path:
        parent_report = C.validate_manifest(
            parent_path, require_complete=False, expected_profile=profile)
        if not parent_report.ok:
            raise C.CaptureContractError(
                "父 manifest 校验失败：\n" + C.format_gate_report(parent_report))
        parent = parent_report.manifest
    if parent:
        capture_id = parent["capture_id"]
        batch_id = parent["batch_id"]
    run_id = str(uuid.uuid4())
    os.makedirs(args.out_dir, exist_ok=True)
    prefix = os.path.join(os.path.abspath(args.out_dir),
                          f"qnh_scan_{now}_{run_id[:8]}")
    manifest_path = prefix + ".manifest.json"
    if os.path.lexists(manifest_path):
        raise C.CaptureContractError(f"本次 manifest 路径已存在：{manifest_path}")
    device = (args.device or (parent or {}).get("device_id")
              or profile.get("default_device_serial"))
    command = [
        sys.executable, SCANNER,
        "--profile", args.profile,
        "--profiles-file", os.path.abspath(args.profiles_file),
        "--capture-id", capture_id,
        "--batch-id", batch_id,
        "--run-id", run_id,
        "--manifest-output", manifest_path,
        "--out-dir", os.path.abspath(args.out_dir),
        "--count", str(args.count),
        "--verify-every", str(args.verify_every),
        "--pace", args.pace,
        "--checkpoint-every", str(args.checkpoint_every),
    ]
    if device:
        command += ["--serial", device]
    if args.tall_screen:
        command.append("--tall-screen")
    if args.record:
        command.append("--record")
    if args.dismiss_order_dialogs:
        command.append("--dismiss-order-dialogs")
    if getattr(args, "observe_exceptions", False):
        command.append("--observe-exceptions")
    if getattr(args, "require_helper", False):
        command.append("--require-helper")
    if args.category:
        command += ["--category", args.category]
    if getattr(args, "carry_baseline", None):
        command += ["--carry-baseline", os.path.abspath(args.carry_baseline)]
    if args.resume:
        command += ["--resume", os.path.abspath(args.resume)]
    print("启动采集：" + " ".join(command), flush=True)
    process = subprocess.Popen(command)
    try:
        returncode = process.wait()
    except KeyboardInterrupt:
        print("收到中断，等待扫描器保存断点并还原设备…", flush=True)
        try:
            returncode = process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                returncode = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait()
    if not os.path.isfile(manifest_path):
        if returncode == 0:
            raise C.CaptureContractError(
                f"扫描器退出码 0，但没有 finalized manifest：{manifest_path}")
        return manifest_path, returncode
    manifest = C.read_json(manifest_path)
    expected_ids = {"capture_id": capture_id, "batch_id": batch_id, "run_id": run_id}
    mismatches = [f"{key}: expected={value} actual={manifest.get(key)}"
                  for key, value in expected_ids.items() if manifest.get(key) != value]
    mismatches.extend("profile " + item for item in C.profile_mismatches(
        manifest.get("profile"), profile))
    csv_path = ((manifest.get("artifacts") or {}).get("csv_path")
                if isinstance(manifest.get("artifacts"), dict) else None)
    if csv_path and C.source_prefix(os.path.abspath(csv_path)) != prefix:
        mismatches.append(f"csv_path 不属于本次 run：{csv_path}")
    if mismatches:
        raise C.CaptureContractError("扫描器输出身份不一致：" + "；".join(mismatches))
    return manifest_path, returncode


def main():
    parser = argparse.ArgumentParser(description="牵牛花 App 全量采集并安全入库")
    parser.add_argument("--profile", help="默认新采集为 xiniu-changan；resume 继承父 manifest")
    parser.add_argument("--profiles-file", default=C.DEFAULT_PROFILE_FILE)
    parser.add_argument("--device", help="覆盖 profile 默认设备")
    parser.add_argument("--out-dir", default=ROOT)
    parser.add_argument("--count", type=int, default=0,
                        help="0=读取实时平台总数（正式全量推荐）")
    parser.add_argument("--verify-every", type=int, default=1)
    parser.add_argument("--pace", choices=["ultra", "fast", "natural", "demo"], default="ultra")
    parser.add_argument("--checkpoint-every", type=int, default=50)
    parser.add_argument("--tall-screen", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--record", action="store_true", help="长全量默认不录像")
    parser.add_argument("--dismiss-order-dialogs", action="store_true",
                        help="按业务授权自动点‘我知道了’关闭订单通知弹窗")
    parser.add_argument("--require-helper", action="store_true",
                        help="把 --require-helper 传给扫描器：助手不可用即快速失败")
    parser.add_argument("--category")
    parser.add_argument("--carry-baseline",
                        help="每日增量携带基线 JSON（qnh_daily_baseline.py 导出），"
                             "透传给扫描器")
    parser.add_argument("--resume", help="父 manifest 或 CSV")
    parser.add_argument("--from-manifest", help="跳过采集，直接 gate/load 既有 final manifest")
    parser.add_argument("--capture-only", action="store_true")
    parser.add_argument("--observe-exceptions", action="store_true",
                        help="仅 capture-only：保留有证据的平台计数差和渠道缺行，业务完整性仍失败")
    parser.add_argument("--commit", action="store_true",
                        help="显式提交；不加则完整事务验证后 rollback")
    args = parser.parse_args()
    if args.observe_exceptions and (not args.capture_only or args.commit):
        parser.error("--observe-exceptions 只能用于 --capture-only，不能提交或执行数据库 dry-run")
    if args.resume and args.from_manifest:
        parser.error("--resume 与 --from-manifest 不能同时使用")
    manifest_path = None
    try:
        if not args.profile:
            source = args.resume or args.from_manifest
            source_manifest = None
            if source:
                candidate = os.path.abspath(source)
                source_manifest = (candidate if candidate.endswith(".json") else
                                   C.source_prefix(candidate) + ".manifest.json")
            inherited_name = None
            if source_manifest and os.path.isfile(source_manifest):
                try:
                    candidate_name = ((C.read_json(source_manifest).get("profile") or {})
                                      .get("name"))
                    if isinstance(candidate_name, str) and candidate_name:
                        C.load_profile(candidate_name, args.profiles_file)
                        inherited_name = candidate_name
                except Exception:
                    inherited_name = None
            args.profile = inherited_name or "xiniu-changan"
        profile = C.load_profile(args.profile, args.profiles_file)
        if args.from_manifest:
            manifest_path = os.path.abspath(args.from_manifest)
            scan_exit = 0
        else:
            manifest_path, scan_exit = run_capture(args, profile)

        if scan_exit != 0:
            gate = (C.validate_manifest(manifest_path, require_complete=False,
                                        expected_profile=profile)
                    if os.path.isfile(manifest_path) else C.GateReport(ok=False))
            if not os.path.isfile(manifest_path):
                gate.fail("扫描器未产出 finalized manifest")
            result = {
                "ok": False,
                "state": "scan_failed",
                "manifest_path": manifest_path,
                "manifest_sha256": gate.manifest_sha256,
                "gate": gate.as_dict(),
                "scan_exit_code": scan_exit,
            }
            if os.path.isfile(manifest_path) and gate.ok and gate.row_count > 0:
                result["resume_command"] = _resume_hint(manifest_path, args)
            else:
                result["restart_command"] = _restart_hint(args)
            C.atomic_write_json(_result_path(manifest_path), result)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            raise SystemExit(1)

        if args.capture_only:
            local_gate = C.validate_manifest(
                manifest_path, require_complete=False, expected_profile=profile)
            _print_gate(local_gate)
            if not local_gate.ok:
                raise C.CaptureContractError(C.format_gate_report(local_gate))
            print(f"capture-only 完成：{manifest_path}")
            return

        gate = C.validate_manifest(
            manifest_path, require_complete=True, expected_profile=profile)
        _print_gate(gate)
        observed_snapshot = False
        if not gate.ok:
            relaxed = C.validate_manifest(
                manifest_path, require_complete=False, expected_profile=profile)
            # 观测终态（observed_with_exceptions）：业务不完整，但可以入库——
            # 未知的渠道格按 sale_status='unknown' 落库，快照状态记 partial，
            # 例外清单写进 error_message。严格门禁仍然拒绝它（不完整性不被
            # 掩盖），只是不再挡住入库。
            observed_snapshot = bool(
                relaxed.ok
                and (relaxed.manifest or {}).get("state") == C.OBSERVED_MANIFEST_STATE)
            if not observed_snapshot:
                result = {
                    "ok": False,
                    "state": "gate_failed",
                    "manifest_path": manifest_path,
                    "manifest_sha256": gate.manifest_sha256,
                    "gate": gate.as_dict(),
                    "scan_exit_code": scan_exit,
                }
                if _is_aggregate(relaxed.manifest):
                    # 聚合产物（叶子类目分片的并集）不是一次采集：续跑/重采的起点
                    # 都是单个类目的采集产物，给聚合发采集命令只会报错。指向分片器
                    # 的观测模式。
                    result["restart_command"] = shlex.join([
                        sys.executable, os.path.join(ROOT, "qnh_category_pipeline.py"),
                        "--observe-exceptions", "--capture-only",
                    ])
                    guidance = ("未触碰数据库。该 manifest 是分片聚合产物，不可续跑、"
                                "不可入库：请用分片器的观测模式重采异常分片后重新聚合：\n"
                                + result["restart_command"])
                elif relaxed.ok and relaxed.row_count > 0:
                    result["resume_command"] = _resume_hint(manifest_path, args)
                    guidance = "未触碰数据库。续跑命令：\n" + result["resume_command"]
                else:
                    result["restart_command"] = _restart_hint(args)
                    guidance = "未触碰数据库。该 manifest 不可续跑，请重新采集：\n" + result["restart_command"]
                C.atomic_write_json(_result_path(manifest_path), result)
                print(guidance)
                raise SystemExit(2)
            print("观测终态产物：业务不完整，按 partial 快照入库（未知的渠道格记 unknown）")

        result_path = _result_path(manifest_path)
        recovery_command = shlex.join([
            sys.executable, os.path.join(ROOT, "qnh_capture_pipeline.py"),
            "--profile", args.profile,
            "--profiles-file", os.path.abspath(args.profiles_file),
            "--from-manifest", manifest_path,
            *(["--commit"] if args.commit else []),
        ])
        if args.commit:
            C.atomic_write_json(result_path, {
                "ok": False,
                "state": "commit_pending",
                "committed": None,
                "manifest_path": manifest_path,
                "manifest_sha256": gate.manifest_sha256,
                "snapshot_id": gate.manifest.get("capture_id"),
                "batch_id": gate.manifest.get("batch_id"),
                "recovery_command": recovery_command,
            })

        try:
            db_result = loader.load_capture(
                manifest_path=manifest_path, require_complete=True, commit=args.commit,
                expected_profile=profile, profiles_file=os.path.abspath(args.profiles_file))
        except Exception as exc:
            result = {
                "ok": False,
                "state": "load_failed_or_commit_unknown" if args.commit else "load_failed",
                "committed": None if args.commit else False,
                "error": f"{type(exc).__name__}: {exc}",
                "manifest_path": manifest_path,
                "manifest_sha256": gate.manifest_sha256,
                "gate": gate.as_dict(),
                "recovery_command": recovery_command,
            }
            try:
                C.atomic_write_json(result_path, result)
            except OSError:
                # A commit_pending record was fsynced before DB work and remains recoverable.
                pass
            raise loader.LoadError(result["error"]) from exc

        result = {
            **db_result,
            "state": "committed" if db_result["committed"] else "dry_run_rolled_back",
            "manifest_path": manifest_path,
            "manifest_sha256": gate.manifest_sha256,
            "gate": gate.as_dict(),
        }
        if args.commit:
            try:
                post = _post_validate(manifest_path, db_result)
                result["post_validation"] = post.as_dict()
                if not post.ok:
                    result["ok"] = False
                    result["state"] = "committed_validation_failed"
            except Exception as exc:
                result["ok"] = False
                result["state"] = "committed_post_validation_error"
                result["post_validation_error"] = f"{type(exc).__name__}: {exc}"
                result["recovery_command"] = recovery_command
        try:
            C.atomic_write_json(result_path, result)
        except OSError as exc:
            if args.commit:
                print("错误：提交已完成，但最终 result 写入失败；"
                      f"commit_pending 记录仍在 {result_path}。\n"
                      f"恢复命令：{recovery_command}\n{type(exc).__name__}: {exc}",
                      file=sys.stderr)
                raise SystemExit(1)
            raise
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        if not result.get("ok"):
            raise SystemExit(1)
    except (C.CaptureContractError, loader.LoadError) as exc:
        if manifest_path is None and args.resume:
            source = os.path.abspath(args.resume)
            parent_path = (source if source.endswith(".json") else
                           C.source_prefix(source) + ".manifest.json")
            result = {
                "ok": False,
                "state": "resume_contract_failed",
                "committed": False,
                "manifest_path": parent_path if os.path.isfile(parent_path) else None,
                "error": f"{type(exc).__name__}: {exc}",
                "restart_command": _restart_hint(args),
            }
            try:
                C.atomic_write_json(_result_path(parent_path), result)
            except OSError:
                pass
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
