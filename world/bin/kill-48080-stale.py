#!/usr/bin/env python3.12
"""ExecStartPre 钩子：清掉占着 48080 的残留/游离 backtest-dashboard server.ts 进程，
确保 systemd 起的新实例能干净绑定端口。

根治「端口被占 → service 反复 exit 1 → 撞 StartLimitBurst → systemd 放手 → 需人肉顶替」。
此脚本在 ExecStart 之前运行（新实例尚未启动），所以杀到的都是旧/游离实例，不会误伤自己。
"""
import glob
import os
import signal
import sys

me = str(os.getpid())
ppid = str(os.getppid())  # systemd 自己
killed = []
for p in glob.glob("/proc/[0-9]*"):
    pid = os.path.basename(p)
    if pid in (me, ppid):
        continue
    try:
        cmd = open(f"{p}/cmdline", "rb").read().replace(b"\x00", b" ").decode("utf-8", "ignore")
    except Exception:
        continue
    if "backtest-dashboard/server.ts" in cmd:
        try:
            os.kill(int(pid), signal.SIGTERM)
            killed.append(pid)
        except Exception:
            pass

print(f"[kill-48080-stale] cleared stale server.ts: {killed or 'none'}", flush=True)
sys.exit(0)
