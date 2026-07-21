#!/usr/bin/env python3.12
"""Phase 2：盘后纯系统结算 + 落净值（傍晚触发，NAV 就绪门闩）。

不唤醒 bot（--phase settle → skipChat）。settle catch-up 幂等 + close_my_day 按今天真实
收盘 NAV 落净值快照。今天挂的单 order_date=today，settle as-of=today 不结算（T+1），
留到明天 Phase 1；本阶段只保证净值快照落库。
"""
import json, os, sqlite3, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, os.path.dirname(__file__))
import live_common as lc

WORLD = lc.WORLD
DB_PATH = lc.DB_PATH
CONC = 5


def _already_closed(rid, today) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM fund_bot_daily_snapshots WHERE run_id=? AND trade_date=?",
            (rid, today)).fetchone()[0]
    finally:
        conn.close()
    return n > 0


def run_one(rid, bot, today, logdir):
    log = os.path.join(logdir, f"{rid}.log")
    with open(log, "a") as lf:
        if _already_closed(rid, today):
            lf.write(f"--- {today} 已 close，跳过 ---\n")
            return (rid, bot, "SKIP", today)
        codes = lc.run_fund_codes(DB_PATH, bot, rid)
        ok, missing = lc.nav_ready(DB_PATH, codes, today)
        if not ok:
            lf.write(f"--- {today} NAV 未就绪，缺 {missing}，顺延 ---\n")
            return (rid, bot, "NAV_PENDING", today)
        cfg = f"config/world-live-{rid}.yaml"
        lf.write(f"\n===== {rid}/{bot} settle {today} {time.strftime('%F %T')} =====\n"); lf.flush()
        rc = subprocess.call(
            ["node", "--experimental-strip-types", "src/oos-daily-driver.ts",
             "--date", today, "--bot-id", bot, "--run-id", rid,
             "--config", cfg, "--phase", "settle"],
            cwd=WORLD, stdout=lf, stderr=subprocess.STDOUT)
        status = "OK" if rc == 0 else "FAILED"
        lf.write(f"--- {today} {status} rc={rc} ---\n")
        return (rid, bot, status, today)


def main():
    today = lc.today_str()
    runs = lc.discover_live_runs(os.path.join(WORLD, "runtime", "runs"))
    if not runs:
        print("[live-settle] 无 live run，退出"); return
    logdir = f"/tmp/live-settle-{today}"; os.makedirs(logdir, exist_ok=True)
    print(f"[live-settle] {today} 待结算 {len(runs)} run，并发 {CONC}")

    results = []
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        futs = {}
        for rid, bot in runs:
            futs[ex.submit(run_one, rid, bot, today, logdir)] = rid
            time.sleep(3)
        for fut in as_completed(futs):
            r = fut.result(); results.append(r)
            print(f"[{len(results)}/{len(runs)}] {r[2]:12} {r[1]:7} {r[0]}")

    with open(os.path.join(logdir, "summary.tsv"), "w") as sf:
        sf.write("status\tbot\trun_id\tdate\n")
        for r in sorted(results, key=lambda x: x[2] != "OK"):
            sf.write(f"{r[2]}\t{r[1]}\t{r[0]}\t{r[3]}\n")
    pend = [r for r in results if r[2] == "NAV_PENDING"]
    bad = [r for r in results if r[2] == "FAILED"]
    print(f"[live-settle] OK={sum(1 for r in results if r[2]=='OK')} 顺延={len(pend)} 失败={len(bad)}")
    if pend:
        print("  顺延（等 NAV）：", [r[0] for r in pend])


if __name__ == "__main__":
    main()
