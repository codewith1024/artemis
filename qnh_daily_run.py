#!/usr/bin/env python3
"""每日采集编排：实访详情分片采集 → 聚合 → auto-commit。

供 Claude Code 定时任务（00:05）调用；全程幂等可续跑：
  * 前置检查设备在线（Wi-Fi ADB），失败写状态文件退出，由巡检会话处理；
  * qnh_category_pipeline 自带断点续跑（capture.state.json）与分片重试；
  * 每日新 out-dir（qnh_daily/YYYYMMDD），capture_id 每日新 UUID → 新快照；
  * --commit 走事务内逐字段校验，失败自动回滚，状态文件记录恢复命令。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
PROFILE = "xiniu-changan"
DEVICE = "192.168.100.14:5555"  # Wi-Fi ADB；手机重启后需 USB 重新 adb tcpip 5555 + adb connect

OK, FAILED, SKIPPED = "ok", "failed", "skipped"
CHILD_PIDS = []   # 当前子进程 PID（写入状态文件，供巡检会话精确清理）


def result_matching(out_dir, predicate):
    """Return the newest result satisfying predicate, ignoring later failures."""
    import glob
    for path in reversed(sorted(glob.glob(os.path.join(
            out_dir, "qnh_category_full_*.result.json")))):
        try:
            with open(path, encoding="utf-8") as fh:
                result = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if predicate(result):
            return path, result
    return None


def committed_result(out_dir):
    return result_matching(out_dir, lambda result: (
        result.get("ok") is True
        and result.get("committed") is True
        and result.get("state") == "committed"
        and (result.get("post_validation") or {}).get("ok") is True))


def dry_run_result(out_dir):
    return result_matching(out_dir, lambda result: (
        result.get("ok") is True
        and result.get("committed") is False
        and result.get("state") == "dry_run_rolled_back"))


def completed_status(result_path, result):
    return {
        "state": OK,
        "stage": "done",
        **{key: result.get(key) for key in
           ("snapshot_id", "batch_id", "status", "rows", "fact_rows",
            "new_matches", "committed", "post_validation", "manifest_path")},
        "result_path": result_path,
    }


def acquire_runner_lock():
    path = os.path.join(ROOT, "qnh_daily", ".runner.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.seek(0)
        owner = lock.read().strip() or "unknown"
        lock.close()
        raise RuntimeError(f"已有每日采集 runner 占用设备（{owner}）")
    lock.seek(0)
    lock.truncate()
    lock.write(f"pid={os.getpid()} started_at={datetime.now().isoformat(timespec='seconds')}\n")
    lock.flush()
    return lock


def adb(*args, timeout=15):
    return subprocess.run(
        [os.environ.get("ADB") or "/Users/jalen/opt/platform-tools/adb",
         "-s", DEVICE, *args],
        capture_output=True, text=True, timeout=timeout)


def write_status(out_dir, **fields):
    path = os.path.join(out_dir, "daily.status.json")
    payload = {"updated_at": datetime.now().isoformat(timespec="seconds"), **fields}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def run(command, log_fh, *, on_start=None):
    """启动子进程并登记 PID；Ctrl-C 先 SIGINT 让扫描器存断点再兜底 kill。"""
    print("[daily] " + " ".join(command), flush=True)
    process = subprocess.Popen(command, stdout=log_fh, stderr=subprocess.STDOUT)
    CHILD_PIDS.append(process.pid)
    if on_start:
        on_start(process.pid)
    try:
        return process.wait()
    except KeyboardInterrupt:
        try:
            os.kill(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            return process.wait(timeout=180)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()
    finally:
        try:
            CHILD_PIDS.remove(process.pid)
        except ValueError:
            pass


def main():
    parser = argparse.ArgumentParser(description="牵牛花每日增量采集编排")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"),
                        help="运行日期（默认今天）；out-dir 与状态文件按日期命名")
    parser.add_argument("--skip-baseline", action="store_true",
                        help="兼容旧命令；历史详情携带已停用，不再导出基线")
    parser.add_argument("--no-commit", action="store_true",
                        help="聚合后 dry-run 回滚，不提交（调试用）")
    parser.add_argument("--no-dismiss-dialogs", action="store_true",
                        help="不自动关闭店铺订单弹窗（默认自动关闭，业务已授权）")
    args = parser.parse_args()
    out_dir = os.path.join(ROOT, "qnh_daily", args.date)
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "daily.log")
    completed = committed_result(out_dir)
    if completed:
        result_path, result = completed
        status = write_status(out_dir, **completed_status(result_path, result))
        print(f"[daily] 该日期已成功提交，无需重跑：{status}")
        return
    try:
        runner_lock = acquire_runner_lock()
    except RuntimeError as exc:
        print(f"[daily] {exc}", file=sys.stderr)
        raise SystemExit(4)
    write_status(out_dir, state="running", stage="start", runner_pid=os.getpid())

    # 前置：设备必须在线（Wi-Fi ADB 常连接口）。
    probe = adb("get-state")
    if probe.returncode != 0 or probe.stdout.strip() != "device":
        status = write_status(out_dir, state=FAILED, stage="device_check",
                              runner_pid=os.getpid(),
                              error=f"设备 {DEVICE} 不在线：{probe.stdout.strip()} {probe.stderr.strip()}")
        print(f"[daily] {status}", file=sys.stderr)
        raise SystemExit(2)

    with open(log_path, "a", encoding="utf-8") as log_fh:
        def stamp(msg):
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)
            log_fh.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
            log_fh.flush()

        stamp("分片采集（逐商品实访详情 + 断点续跑；历史详情携带已停用）")
        write_status(out_dir, state="running", stage="capture",
                     runner_pid=os.getpid(), child_pids=[])
        code = run([sys.executable, os.path.join(ROOT, "qnh_category_pipeline.py"),
                    "--out-dir", out_dir,
                    "--observe-exceptions",
                    *([] if args.no_commit else ["--commit"]),
                    *([] if args.no_dismiss_dialogs else ["--dismiss-order-dialogs"])],
                   log_fh,
                   on_start=lambda pid: write_status(
                       out_dir, state="running", stage="capture",
                       runner_pid=os.getpid(), child_pids=[pid]))
        if code != 0:
            write_status(out_dir, state=FAILED, stage="capture",
                         error=f"采集/入库退出码 {code}；日志 {log_path}",
                         resume_hint=f"重跑同一命令即可断点续跑（capture.state.json 在 {out_dir}）")
            raise SystemExit(3)

    # 成功：正式运行必须已提交且 post-validation PASS；dry-run 必须已回滚。
    completed = dry_run_result(out_dir) if args.no_commit else committed_result(out_dir)
    if not completed:
        expected = ("dry_run_rolled_back result" if args.no_commit else
                    "committed + post-validation PASS result")
        write_status(out_dir, state=FAILED, stage="finalize",
                     error=f"采集进程成功退出，但未找到 {expected}")
        raise SystemExit(3)
    result_path, result = completed
    status = write_status(out_dir, **completed_status(result_path, result))
    print(f"[daily] 完成：{status}")


if __name__ == "__main__":
    main()
