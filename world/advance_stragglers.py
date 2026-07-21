#!/usr/bin/env python3.12
"""补跑两个落后的合格全窗口 run 到 2026-07-20（逐日推进，账户不重置）。"""
import json, os, subprocess, time
from concurrent.futures import ThreadPoolExecutor, as_completed

WORLD = "/home/rooot/agent_invest_lab/world"
LOGDIR = "/tmp/advance-0720"
os.chdir(WORLD); os.makedirs(LOGDIR, exist_ok=True)

# run_id -> (bot, [需补交易日，升序])
JOBS = {
    "dash-2026-07-20T15-04-39": ("bot18",
        ["2026-07-13","2026-07-14","2026-07-15","2026-07-16","2026-07-17","2026-07-20"]),
    "dash-2026-07-20T15-06-12": ("bot20",
        ["2026-07-01","2026-07-02","2026-07-03","2026-07-06","2026-07-07","2026-07-08",
         "2026-07-09","2026-07-10","2026-07-13","2026-07-14","2026-07-15","2026-07-16",
         "2026-07-17","2026-07-20"]),
}

def applied_date(rid):
    try:
        s = json.load(open(f"runtime/runs/{rid}/state.json"))
        td = s.get("trading_dates", [])
        return td[-1] if td else "0000-00-00"
    except Exception:
        return "0000-00-00"

def run_one(rid, bot, dates):
    cfg = f"config/world-tmp-{rid}.yaml"
    log = f"{LOGDIR}/{rid}.log"
    with open(log, "a") as lf:
        lf.write(f"\n===== {rid} / {bot} 补跑开始 {time.strftime('%F %T')} =====\n"); lf.flush()
        for d in dates:
            if d <= applied_date(rid):
                lf.write(f"--- {d} 已应用，跳过 ---\n"); lf.flush(); continue
            lf.write(f"--- {d} START {time.strftime('%F %T')} ---\n"); lf.flush()
            rc = subprocess.call(
                ["node","--experimental-strip-types","src/oos-daily-driver.ts",
                 "--date",d,"--bot-id",bot,"--run-id",rid,"--config",cfg],
                stdout=lf, stderr=subprocess.STDOUT)
            got = applied_date(rid)
            if rc != 0:
                lf.write(f"--- {d} FAILED rc={rc} (state={got}) ---\n"); lf.flush()
                return (rid, bot, d, "FAILED", got)
            lf.write(f"--- {d} OK (state now {got}) ---\n"); lf.flush()
            if got != d:
                lf.write(f"--- WARN state {got} != 目标 {d} ---\n"); lf.flush()
                return (rid, bot, d, "STATE_MISMATCH", got)
        return (rid, bot, dates[-1], "OK", applied_date(rid))

def main():
    for rid,(bot,ds) in JOBS.items():
        print(f"{bot} {rid}: 从 {applied_date(rid)} 补 {len(ds)} 天 -> {ds[-1]}")
    results = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(run_one, rid, bot, ds): rid for rid,(bot,ds) in JOBS.items()}
        for fut in as_completed(futs):
            r = fut.result(); results.append(r)
            print(f"[{len(results)}/{len(JOBS)}] {r[3]:14} {r[1]:7} {r[0]} -> {r[4]}")
    ok = [r for r in results if r[3]=="OK"]
    print(f"\n===== 补跑完成 OK={len(ok)}/{len(results)} =====")
    for r in results:
        flag = "✅" if r[3]=="OK" else "✗"
        print(f"  {flag} {r[3]:14} {r[1]:7} {r[0]} state={r[4]}")

if __name__ == "__main__":
    main()
