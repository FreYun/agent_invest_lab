#!/usr/bin/env python3.12
"""Phase 2：盘后纯系统结算 + 落净值（次日早上触发，NAV 就绪门闩）。

时间顺序：A 股基金净值当日不出，次日早上 06:17 由 lab-fund-daily-refresh 落库，
所以本阶段安排在早上（07:00，NAV 刷新之后），结算的是 **上一个交易日**（T-1）：
close_my_day 按 T-1 真实收盘 NAV 落净值快照。不唤醒 bot（--phase settle → skipChat）。

为何只结算 T-1（而非当天）：当天挂的单 order_date=today、settle as-of=today 不结算（T+1），
且当天 NAV 尚未落库；上一交易日的 NAV 此刻已就绪，持仓也恰好停在 T-1 收盘状态
（今天的单仍 pending、未推进持仓），close as-of T-1 与真实 EOD 一致。settle 幂等，
_already_closed 幂等门闩，重复跑安全。
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


def run_one(rid, bot, target, logdir):
    log = os.path.join(logdir, f"{rid}.log")
    with open(log, "a") as lf:
        if _already_closed(rid, target):
            lf.write(f"--- {target} 已 close，跳过 ---\n")
            return (rid, bot, "SKIP", target)
        codes = lc.run_fund_codes(DB_PATH, bot, rid)
        ok, missing = lc.nav_ready(DB_PATH, codes, target)
        if not ok:
            lf.write(f"--- {target} NAV 未就绪，缺 {missing}，顺延 ---\n")
            return (rid, bot, "NAV_PENDING", target)
        cfg = f"config/world-live-{rid}.yaml"
        lf.write(f"\n===== {rid}/{bot} settle {target} {time.strftime('%F %T')} =====\n"); lf.flush()
        rc = subprocess.call(
            ["node", "--experimental-strip-types", "src/oos-daily-driver.ts",
             "--date", target, "--bot-id", bot, "--run-id", rid,
             "--config", cfg, "--phase", "settle"],
            cwd=WORLD, stdout=lf, stderr=subprocess.STDOUT)
        status = "OK" if rc == 0 else "FAILED"
        lf.write(f"--- {target} {status} rc={rc} ---\n")
        return (rid, bot, status, target)


def main():
    today = lc.today_str()
    cal = os.path.join(WORLD, "runtime", "calendar.json")
    target = lc.prev_trading_day(cal, today)
    if not target:
        print(f"[live-settle] {today} 之前无交易日，退出"); return
    runs = lc.discover_live_runs(os.path.join(WORLD, "runtime", "runs"))
    if not runs:
        print("[live-settle] 无 live run，退出"); return
    logdir = f"/tmp/live-settle-{target}"; os.makedirs(logdir, exist_ok=True)
    print(f"[live-settle] today={today} 结算上一交易日 {target}，待结算 {len(runs)} run，并发 {CONC}")

    results = []
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        futs = {}
        for rid, bot in runs:
            futs[ex.submit(run_one, rid, bot, target, logdir)] = rid
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
