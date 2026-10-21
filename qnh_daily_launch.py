#!/usr/bin/env python3
"""Start or remove one date's daily runner as a one-shot launchd job."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import shlex
import subprocess
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
LABEL_PREFIX = "com.jalen.qnh.daily."


def parse_date(value):
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须是有效的 YYYYMMDD") from exc
    if parsed.strftime("%Y%m%d") != value:
        raise argparse.ArgumentTypeError("日期必须是有效的 YYYYMMDD")
    return value


def label_for(run_date):
    return LABEL_PREFIX + run_date


def paths_for(run_date):
    out_dir = os.path.join(ROOT, "qnh_daily", run_date)
    return out_dir, os.path.join(out_dir, "runner.launchd.plist")


def plist_for(run_date):
    out_dir, _ = paths_for(run_date)
    log_path = os.path.join(out_dir, "runner.nohup.log")
    command = shlex.join([
        "/usr/bin/python3", os.path.join(ROOT, "qnh_daily_run.py"),
        "--date", run_date,
    ])
    return {
        "Label": label_for(run_date),
        "ProgramArguments": ["/bin/zsh", "-lc", "exec " + command],
        "WorkingDirectory": ROOT,
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }


def _target(run_date):
    return f"gui/{os.getuid()}/{label_for(run_date)}"


def _write_plist(run_date):
    out_dir, path = paths_for(run_date)
    os.makedirs(out_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        plistlib.dump(plist_for(run_date), fh, sort_keys=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def _launchctl(*args):
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True)


def start(run_date):
    target = _target(run_date)
    current = _launchctl("print", target)
    if current.returncode == 0:
        if re.search(r"^\s*state = running\s*$", current.stdout, re.MULTILINE):
            print(f"已在运行：{label_for(run_date)}")
            return
        stopped = _launchctl("bootout", target)
        if stopped.returncode != 0:
            raise RuntimeError(stopped.stderr.strip() or stopped.stdout.strip())

    path = _write_plist(run_date)
    loaded = _launchctl("bootstrap", f"gui/{os.getuid()}", path)
    if loaded.returncode != 0:
        raise RuntimeError(loaded.stderr.strip() or loaded.stdout.strip())
    print(f"已启动一次性任务：{label_for(run_date)}（{path}）")


def stop(run_date):
    target = _target(run_date)
    stopped = _launchctl("bootout", target)
    message = (stopped.stderr + stopped.stdout).strip()
    if (stopped.returncode != 0
            and "Could not find service" not in message
            and "No such process" not in message):
        raise RuntimeError(message)
    _, path = paths_for(run_date)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    print(f"已移除任务：{label_for(run_date)}")


def main():
    parser = argparse.ArgumentParser(description="管理牵牛花每日采集的一次性 launchd 任务")
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--date", required=True, type=parse_date)
    args = parser.parse_args()
    {"start": start, "stop": stop}[args.action](args.date)


if __name__ == "__main__":
    main()
