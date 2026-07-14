#!/usr/bin/env python3
"""通用 cron 任务防孤儿包装(Python 版本,使用 prctl PR_SET_CHILD_SUBREAPER)。

为什么需要它:
  历史上 update-investment-strategy-summaries.py 这种 cron 任务,因为 openclaw / openclaw-agent 是
  Go 二进制 + 自身 setsid 进新 session,Python 的 subprocess.run timeout 触发后只 SIGKILL 直接子进程,
  孙子进程(setsid 后的 agent 工作进程)会 reparent 到 init(ppid=1),变成长期孤儿吃内存。

  仅靠"每个脚本自己写正确的 killpg"防御不完全:任何一个脚本写漏了就会留孤儿。
  我们再加一层防御:cron-wrapper.sh -> cron_wrapper.py。

机制:
  1. wrapper 启动时调用 prctl(PR_SET_CHILD_SUBREAPER, 1) -- 把自己注册成 child subreaper。
     之后任何后代孤儿不会 reparent 到 init,而是 reparent 到 wrapper(类似 PID 1 的角色)。
  2. wrapper 启动实际命令,wait 直到它结束。
  3. wrapper 退出前(包括正常完成 / 信号 / 异常),递归找出所有"祖先链经过 wrapper 的进程",
     SIGKILL 它们 -- 包括孙子、曾孙、不论它们怎么 setsid / detach,都跑不掉 subreaper 的网。

用法:
  cron-wrapper.sh <job_name> <command> [args...]
  -> exec /usr/bin/python3 cron_wrapper.py <job_name> <command> [args...]

在 crontab 里替换原命令:
  原: 30 9 * * * /usr/bin/python3 /path/cron.py >> /log 2>&1
  后: 30 9 * * * /home/rooot/.openclaw/scripts/cron-wrapper.sh job_name /usr/bin/python3 /path/cron.py >> /log 2>&1
"""
import ctypes
import ctypes.util
import json
import os
import re
import signal
import subprocess
import sys
import time

# Linux prctl(2) constant
PR_SET_CHILD_SUBREAPER = 36

# 兜底 killpg 后给后代多少秒来响应 SIGTERM,然后 SIGKILL
GRACE_AFTER_TERM = 3

DEFAULT_RUNS_DIR = "/home/rooot/agent_invest_lab/logs/syscron-runs"
START_MS = 0  # populated in main()


def _safe_job_name(name: str) -> str:
    """Strip any character not in [a-zA-Z0-9._-] to prevent path escape.

    Returns "unknown" if the result is empty or consists entirely of dots
    (e.g. "..." -> "unknown" to avoid invisible/confusing filenames).
    """
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    if not sanitized or re.fullmatch(r"\.+", sanitized):
        return "unknown"
    return sanitized


def write_run_record(status: str, exit_code: int, error_msg: str = None):
    """Append one JSONL line to cron/syscron-runs/<job>.jsonl.

    On any write failure, log a warning but do NOT propagate — wrapper must
    never alter the child's exit code due to bookkeeping failure.
    """
    try:
        runs_dir = os.environ.get("CRON_WRAPPER_RUNS_DIR", DEFAULT_RUNS_DIR)
        os.makedirs(runs_dir, exist_ok=True)
        safe = _safe_job_name(JOB_NAME) or "unknown"
        record = {
            "ts": int(time.time() * 1000),
            "jobName": JOB_NAME,
            "status": status,
            "exitCode": exit_code,
            "runAtMs": START_MS,
            "durationMs": int(time.time() * 1000) - START_MS,
        }
        if error_msg:
            record["error"] = error_msg[:500]
        with open(os.path.join(runs_dir, f"{safe}.jsonl"), "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"WARNING: failed to write run record: {e}")


def set_child_subreaper() -> bool:
    """注册当前进程为 child subreaper。失败返回 False(降级:仅靠 wait + killpg 进程组)。"""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        rc = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
        if rc != 0:
            errno = ctypes.get_errno()
            log(f"WARNING: prctl(PR_SET_CHILD_SUBREAPER, 1) failed errno={errno}")
            return False
        return True
    except Exception as e:
        log(f"WARNING: cannot call prctl: {e}")
        return False


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[cron-wrapper:{JOB_NAME} {ts}] {msg}", flush=True)


def read_descendants(root_pid: int) -> list[int]:
    """走 /proc 找所有 ancestor 链经过 root_pid 的进程(不含 root_pid 自己)。

    用 ppid 反向追溯。即使后代用了 setsid 改了 PGID,只要 ppid 链最终连到 root_pid 就抓到。
    subreaper 机制保证所有原本 reparent 到 init 的孤儿现在 reparent 到 root_pid 上。
    """
    # 构建 pid -> ppid 映射
    pid_to_ppid: dict[int, int] = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/status") as f:
                    for line in f:
                        if line.startswith("PPid:"):
                            ppid = int(line.split()[1])
                            pid_to_ppid[pid] = ppid
                            break
            except OSError:
                continue
    except OSError:
        return []

    # BFS 找 root_pid 的所有后代
    descendants: set[int] = set()
    # 反向索引:ppid -> [children]
    children_index: dict[int, list[int]] = {}
    for pid, ppid in pid_to_ppid.items():
        children_index.setdefault(ppid, []).append(pid)

    queue = list(children_index.get(root_pid, []))
    while queue:
        p = queue.pop(0)
        if p in descendants or p == root_pid:
            continue
        descendants.add(p)
        queue.extend(children_index.get(p, []))
    return sorted(descendants)


def reap_descendants():
    """kill 所有后代进程,SIGTERM → grace → SIGKILL。"""
    my_pid = os.getpid()
    descendants = read_descendants(my_pid)
    if not descendants:
        return
    log(f"reaping {len(descendants)} descendants: {descendants[:10]}{'...' if len(descendants) > 10 else ''}")

    # SIGTERM
    for pid in descendants:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as e:
            log(f"  SIGTERM {pid} failed: {e}")

    time.sleep(GRACE_AFTER_TERM)

    # SIGKILL 兜底 — 重新扫一次后代(可能有新的孤儿 reparent 上来)
    remaining = read_descendants(my_pid)
    if remaining:
        log(f"SIGKILL {len(remaining)} survivors")
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as e:
                log(f"  SIGKILL {pid} failed: {e}")


# 全局,setup_signal_handlers 用
JOB_NAME = "unknown"
CHILD_PROC: subprocess.Popen | None = None


def signal_handler(signum, frame):
    log(f"received signal {signum}, reaping descendants then exiting")
    write_run_record("error", 128 + signum, f"killed by signal {signum}")
    reap_descendants()
    sys.exit(128 + signum)


def main():
    global JOB_NAME, CHILD_PROC, START_MS
    if len(sys.argv) < 3:
        print("Usage: cron_wrapper.py <job_name> <command> [args...]", file=sys.stderr)
        sys.exit(2)

    JOB_NAME = sys.argv[1]
    cmd = sys.argv[2:]
    START_MS = int(time.time() * 1000)

    subreaper_ok = set_child_subreaper()
    log(f"start subreaper={'YES' if subreaper_ok else 'NO(degraded)'} cmd: {' '.join(cmd)}")

    # 注册信号 handler(EXIT 用 finally 模拟)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal_handler)

    try:
        # 启动子进程。不设 start_new_session — 让子进程在 wrapper 同 session,
        # 它自己 setsid 后变孤儿时会被 subreaper 接管。
        CHILD_PROC = subprocess.Popen(cmd)
        rc = CHILD_PROC.wait()
        log(f"child exited rc={rc}")
        # 最后再 reap 一次:子进程退出但可能仍有 detached 后代
        reap_descendants()
        write_run_record("ok" if rc == 0 else "error", rc)
        sys.exit(rc)
    except FileNotFoundError as e:
        log(f"command not found: {e}")
        reap_descendants()
        write_run_record("error", 127, f"command not found: {e}")
        sys.exit(127)
    except Exception as e:
        log(f"exception: {e}")
        reap_descendants()
        write_run_record("error", 1, f"exception: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
