#!/usr/bin/env python3.12
"""把看板所有「合格」的单指数 run 从 07-16 推进到覆盖 07-20。

- 合格口径：停在 2026-07-16 的单日 dash-* run，单 bot ∈ bot1..bot20（排除多指数 bot101/102/103），
  且未被 run-verdicts 手动标 fail。
- 每个 run 内部按 [07-17, 07-20] 串行推进（账户逐日结转），幂等：只跑比当前 state 更新的交易日。
- 跨 run 最多 CONC 个并发。逐 run 写日志，末尾写 summary.tsv。
"""
import json, os, glob, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

WORLD = "/home/rooot/agent_invest_lab/world"
DATES = ["2026-07-17", "2026-07-20"]
CONC = 5
LOGDIR = "/tmp/advance-0720"
os.chdir(WORLD)
os.makedirs(LOGDIR, exist_ok=True)

verdicts = json.load(open("runtime/run-verdicts.json"))

def build_pairs():
    pairs = []
    for f in glob.glob("runtime/runs/*/state.json"):
        try:
            s = json.load(open(f))
        except Exception:
            continue
        if s.get("trading_dates") != ["2026-07-16"]:
            continue
        rid = os.path.basename(os.path.dirname(f))
        if not rid.startswith("dash-"):
            continue
        bots = s.get("bots", [])
        if len(bots) != 1:
            continue
        bot = bots[0]
        if bot in ("bot101", "bot102", "bot103"):   # 多指数，跳过
            continue
        if verdicts.get(f"{rid}|{bot}") == "fail":   # 手动不合格，跳过
            continue
        cfg = f"config/world-tmp-{rid}.yaml"
        if not os.path.exists(cfg):
            continue
        pairs.append((rid, bot, cfg))
    def botn(p):
        m = re.search(r"\d+", p[1]); return int(m.group()) if m else 0
    pairs.sort(key=lambda p: (botn(p), p[0]))
    return pairs

def applied_date(rid):
    """该 run 当前已应用到的交易日（state.trading_dates 的单日值）。"""
    try:
        s = json.load(open(f"runtime/runs/{rid}/state.json"))
        td = s.get("trading_dates", [])
        return td[0] if td else "0000-00-00"
    except Exception:
        return "0000-00-00"

def run_one(rid, bot, cfg):
    log = f"{LOGDIR}/{rid}.log"
    with open(log, "a") as lf:
        lf.write(f"\n===== {rid} / {bot} 开始 {time.strftime('%F %T')} =====\n"); lf.flush()
        for d in DATES:
            if d <= applied_date(rid):
                lf.write(f"--- {d} 已应用，跳过 ---\n"); lf.flush()
                continue
            lf.write(f"--- {d} START {time.strftime('%F %T')} ---\n"); lf.flush()
            rc = subprocess.call(
                ["node", "--experimental-strip-types", "src/oos-daily-driver.ts",
                 "--date", d, "--bot-id", bot, "--run-id", rid, "--config", cfg],
                stdout=lf, stderr=subprocess.STDOUT)
            if rc != 0:
                lf.write(f"--- {d} FAILED rc={rc} ---\n"); lf.flush()
                return (rid, bot, d, "FAILED", applied_date(rid))
            got = applied_date(rid)
            lf.write(f"--- {d} OK (state now {got}) ---\n"); lf.flush()
            if got != d:
                lf.write(f"--- WARN: state {got} != 目标 {d} ---\n"); lf.flush()
                return (rid, bot, d, "STATE_MISMATCH", got)
        return (rid, bot, DATES[-1], "OK", applied_date(rid))

def main():
    pairs = build_pairs()
    print(f"合格待推进 run: {len(pairs)} 个，并发 {CONC}，各推进 {DATES}")
    for rid, bot, _ in pairs:
        print(f"  {bot:7} {rid}")
    results = []
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        futs = {}
        for i, (rid, bot, cfg) in enumerate(pairs):
            futs[ex.submit(run_one, rid, bot, cfg)] = rid
            time.sleep(3)  # 错峰启动，缓解 setup 期端口/DB 争用
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            print(f"[{len(results)}/{len(pairs)}] {r[3]:14} {r[1]:7} {r[0]} -> {r[4]}")
    ok = [r for r in results if r[3] == "OK"]
    bad = [r for r in results if r[3] != "OK"]
    with open(f"{LOGDIR}/summary.tsv", "w") as sf:
        sf.write("status\tbot\trun_id\tlast_date\tstate_now\n")
        for r in sorted(results, key=lambda x: x[3] != "OK"):
            sf.write(f"{r[3]}\t{r[1]}\t{r[0]}\t{r[2]}\t{r[4]}\n")
    print(f"\n===== 完成 OK={len(ok)} 失败/异常={len(bad)} / 共 {len(results)} =====")
    for r in bad:
        print(f"  ✗ {r[3]:14} {r[1]:7} {r[0]} (卡在 {r[2]}, state={r[4]})")
    print(f"日志目录 {LOGDIR}/  汇总 {LOGDIR}/summary.tsv")

if __name__ == "__main__":
    main()
