#!/usr/bin/env python3.12
"""Phase 1：live 盘中决策（默认 14:00 触发，14:55 硬截止）。

对每个 live run 跑 oos-daily-driver --phase decide：结算昨天的单 + bot 决策挂今天的
pending 单（awaiting_nav 冻现金）+ 跳过 close_my_day。到 14:55 仍未完成的 run 记 MISSED。
"""
import json, os, subprocess, sys, time, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, os.path.dirname(__file__))
import live_common as lc

WORLD = lc.WORLD
CONC = 5
HARD_DEADLINE = os.environ.get("LIVE_DECIDE_DEADLINE", "14:55")  # HH:MM 本地时


def _past_deadline() -> bool:
    now = datetime.datetime.now().strftime("%H:%M")
    return now >= HARD_DEADLINE


def _already_decided(rid, today) -> bool:
    try:
        s = json.load(open(os.path.join(WORLD, "runtime", "runs", rid, "state.json")))
        if today in (s.get("trading_dates") or []):
            return True
    except Exception:
        pass
    return False


def run_one(rid, bot, today, logdir):
    log = os.path.join(logdir, f"{rid}.log")
    with open(log, "a") as lf:
        if _already_decided(rid, today):
            lf.write(f"--- {today} 已决策，跳过 ---\n")
            return (rid, bot, "SKIP", today)
        if _past_deadline():
            lf.write(f"--- {today} 已过硬截止 {HARD_DEADLINE}，MISSED，不提交 ---\n")
            return (rid, bot, "MISSED", today)
        cfg = f"config/world-live-{rid}.yaml"
        lf.write(f"\n===== {rid}/{bot} decide {today} {time.strftime('%F %T')} =====\n"); lf.flush()
        rc = subprocess.call(
            ["node", "--experimental-strip-types", "src/oos-daily-driver.ts",
             "--date", today, "--bot-id", bot, "--run-id", rid,
             "--config", cfg, "--phase", "decide"],
            cwd=WORLD, stdout=lf, stderr=subprocess.STDOUT)
        status = "OK" if rc == 0 else "FAILED"
        lf.write(f"--- {today} {status} rc={rc} ---\n")
        return (rid, bot, status, today)


def main():
    today = lc.today_str()
    if not lc.is_weekday(today):
        print(f"[live-decide] {today} 非工作日，退出"); return
    changed = lc.ensure_calendar_has(os.path.join(WORLD, "runtime", "calendar.json"), today)
    if changed:
        print(f"[live-decide] 日历已兜底追加 {today}")

    runs = lc.discover_live_runs(os.path.join(WORLD, "runtime", "runs"))
    if not runs:
        print("[live-decide] 无 live run，退出"); return
    logdir = f"/tmp/live-decide-{today}"; os.makedirs(logdir, exist_ok=True)
    print(f"[live-decide] {today} 待决策 {len(runs)} run，并发 {CONC}，硬截止 {HARD_DEADLINE}")

    results = []
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        futs = {}
        for rid, bot in runs:
            futs[ex.submit(run_one, rid, bot, today, logdir)] = rid
            time.sleep(3)  # 错峰启动
        for fut in as_completed(futs):
            r = fut.result(); results.append(r)
            print(f"[{len(results)}/{len(runs)}] {r[2]:7} {r[1]:7} {r[0]}")

    with open(os.path.join(logdir, "summary.tsv"), "w") as sf:
        sf.write("status\tbot\trun_id\tdate\n")
        for r in sorted(results, key=lambda x: x[2] != "OK"):
            sf.write(f"{r[2]}\t{r[1]}\t{r[0]}\t{r[3]}\n")
    missed = [r for r in results if r[2] in ("MISSED", "FAILED")]
    print(f"[live-decide] 完成 OK={sum(1 for r in results if r[2]=='OK')} 异常={len(missed)}")
    for r in missed:
        print(f"  ✗ {r[2]:7} {r[1]:7} {r[0]}")


if __name__ == "__main__":
    main()
