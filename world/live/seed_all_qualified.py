#!/usr/bin/env python3.12
"""批量派生：把全部合格历史 run 一次性 seed 成 live run（幂等，已存在跳过）。"""
import json, os, glob, re, sys, subprocess
sys.path.insert(0, os.path.dirname(__file__))
import live_common as lc

WORLD = lc.WORLD


def build_pairs():
    """合格口径：单指数 dash-* run，单 bot∈bot1..bot20（排除 bot101/102/103），未手动标 fail，config 存在。"""
    verdicts = json.load(open(os.path.join(WORLD, "runtime", "run-verdicts.json")))
    pairs = []
    for f in glob.glob(os.path.join(WORLD, "runtime", "runs", "dash-*", "state.json")):
        try:
            s = json.load(open(f))
        except Exception:
            continue
        rid = os.path.basename(os.path.dirname(f))
        bots = s.get("bots", [])
        if len(bots) != 1:
            continue
        bot = bots[0]
        if bot in ("bot101", "bot102", "bot103"):
            continue
        if verdicts.get(f"{rid}|{bot}") == "fail":
            continue
        if not os.path.exists(os.path.join(WORLD, "config", f"world-tmp-{rid}.yaml")):
            continue
        pairs.append((rid, bot))
    pairs.sort(key=lambda p: (int(re.search(r"\d+", p[1]).group()), p[0]))
    return pairs


def main():
    pairs = build_pairs()
    print(f"合格 run {len(pairs)} 个，逐个幂等派生 live run")
    for rid, bot in pairs:
        subprocess.call(["/usr/bin/python3.12", os.path.join(WORLD, "live", "seed_live_run.py"),
                         "--source-run-id", rid, "--bot-id", bot, "--seed-date", lc.today_str()])


if __name__ == "__main__":
    main()
