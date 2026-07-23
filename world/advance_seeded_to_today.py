#!/usr/bin/env python3.12
"""把 27 条昨天 seed 的 live run 从各自 source 结束日逐日补跑到今天 2026-07-22。

- 每条 run：从 (source replay.to 之后 或 已应用日之后) 的交易日起，逐日 oos-daily-driver。
- <=2026-07-21 跑全天（NAV 已出，decide+close+settle）；2026-07-22 跑 --phase decide（当日 NAV 未出）。
- 账户逐日结转；幂等：只跑比 state.trading_dates[-1] 更新的交易日。
- 单 run 内串行、按日推进；跨 run 最多 CONC 并发。任一日 rc!=0 立即停该 run（不留缺口），其余 run 继续。
"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

WORLD = "/home/rooot/agent_invest_lab/world"
TODAY = "2026-07-22"            # 当日：只 decide
LAST_FULL = "2026-07-21"       # 含此日及之前：全天
CONC = 4
LOGDIR = f"/tmp/advance-seeded-{TODAY}"
os.chdir(WORLD)
os.makedirs(LOGDIR, exist_ok=True)

CAL = json.load(open("runtime/calendar.json"))["trading_days"]

SEEDED = [l.split("|")[2].strip()
          for l in open("/tmp/live_status.txt")
          if l.startswith("seeded|")]


def cfg_replay_to(rid):
    import yaml
    c = yaml.safe_load(open(f"config/world-live-{rid}.yaml"))
    return str(c["replay"]["to"]), c["bots"][0]  # PyYAML 可能把无引号日期读成 date，统一转字符串


def state_last(rid):
    try:
        s = json.load(open(f"runtime/runs/{rid}/state.json"))
        td = s.get("trading_dates") or []
        return td[-1] if td else None
    except Exception:
        return None


def plan_days(rid):
    """返回该 run 待补交易日（升序），已跑过的自动排除。"""
    replay_to, bot = cfg_replay_to(rid)
    last = state_last(rid)
    start_after = last if last else replay_to
    days = [d for d in CAL if start_after < d <= TODAY]
    return bot, days


def run_one(rid):
    bot, days = plan_days(rid)
    cfg = f"config/world-live-{rid}.yaml"
    log = f"{LOGDIR}/{rid}.log"
    with open(log, "a") as lf:
        lf.write(f"\n===== {rid}/{bot} 补跑 {days[0] if days else '(无)'}..{TODAY} "
                 f"{time.strftime('%F %T')} =====\n"); lf.flush()
        if not days:
            lf.write("--- 无待补交易日，已是最新 ---\n")
            return (rid, bot, "OK", state_last(rid), 0)
        ran = 0
        for d in days:
            if state_last(rid) and d <= state_last(rid):
                lf.write(f"--- {d} 已应用，跳过 ---\n"); lf.flush(); continue
            cmd = ["node", "--experimental-strip-types", "src/oos-daily-driver.ts",
                   "--date", d, "--bot-id", bot, "--run-id", rid, "--config", cfg]
            if d == TODAY:                       # 当日 NAV 未出 → 只决策
                cmd += ["--phase", "decide"]
            lf.write(f"--- {d} START {time.strftime('%F %T')} "
                     f"{'(decide)' if d==TODAY else '(full)'} ---\n"); lf.flush()
            rc = subprocess.call(cmd, stdout=lf, stderr=subprocess.STDOUT)
            got = state_last(rid)
            if rc != 0:
                lf.write(f"--- {d} FAILED rc={rc} (state={got}) ---\n"); lf.flush()
                return (rid, bot, "FAILED", got, ran)
            ran += 1
            lf.write(f"--- {d} OK (state now {got}) ---\n"); lf.flush()
        return (rid, bot, "OK", state_last(rid), ran)


def main():
    print(f"[advance-seeded] {len(SEEDED)} 条待补，并发 {CONC}，目标 {TODAY}")
    for rid in SEEDED:
        bot, days = plan_days(rid)
        print(f"  {bot:6} {rid}: {len(days)} 天 {days[0] if days else '-'}..{days[-1] if days else '-'}")
    results = []
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        futs = {}
        for rid in SEEDED:
            futs[ex.submit(run_one, rid)] = rid
            time.sleep(3)  # 错峰启动，避免 LLM 上游瞬时打满
        for fut in as_completed(futs):
            r = fut.result(); results.append(r)
            flag = "OK " if r[2] == "OK" else "✗✗✗"
            print(f"[{len(results)}/{len(SEEDED)}] {flag} {r[2]:7} {r[1]:6} {r[0]} "
                  f"-> state={r[3]} (+{r[4]}天)")
    with open(f"{LOGDIR}/summary.tsv", "w") as sf:
        sf.write("status\tbot\trun_id\tstate\tran_days\n")
        for r in sorted(results, key=lambda x: x[2] != "OK"):
            sf.write(f"{r[2]}\t{r[1]}\t{r[0]}\t{r[3]}\t{r[4]}\n")
    ok = [r for r in results if r[2] == "OK"]
    bad = [r for r in results if r[2] != "OK"]
    print(f"\n===== 完成 OK={len(ok)}/{len(results)} =====")
    for r in bad:
        print(f"  ✗ {r[2]:7} {r[1]:6} {r[0]} state={r[3]}")


if __name__ == "__main__":
    main()
