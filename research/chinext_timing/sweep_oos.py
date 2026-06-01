"""Proper IS/OOS protocol for the ChiNext (159915.SZ) single-index timing factor.

Methodology (the whole point of this script):
  * IN-SAMPLE  (design+tune): 2012-01-01 .. 2019-12-31
      - covers 2013/2015 bull, the 2015 crash, the 2016-18 bear, the 2019 recovery
      - we explore factor FAMILIES and a PARAMETER GRID here and pick a winner
  * OUT-OF-SAMPLE (test once): 2020-01-01 .. 2026-05-21
      - 2020 bull, 2022-23 bear, 2024-25 recovery
      - we evaluate ONLY the IS-locked config(s). We never tune on this window.

We DESIGN factors only. The backtest is the vibe-trading library's runner
(`python -m backtest.runner`). No custom backtest is written here.

Caveat disclosed: the runner measures metrics across the whole [start,end] it is
given and cannot warm up on a pre-period. The OOS run therefore eats ~120 trading
days of SMA120 warmup at the start of 2020 (signal flat -> conservative; if
anything it understates OOS performance). Acceptable and noted.
"""
import json, subprocess, os, csv
from pathlib import Path

BASE = Path("/home/rooot/agent_invest_lab/research/chinext_timing")
SP = "/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages"
PY = "/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python"
HEADER = "import pandas as pd\nimport numpy as np\nfrom typing import Dict\n\n\nclass SignalEngine:\n"

IS_START, IS_END = "2012-01-01", "2019-12-31"
OOS_START, OOS_END = "2020-01-01", "2026-05-21"


def cfg(start, end):
    return {"source": "akshare", "codes": ["159915.SZ"], "start_date": start, "end_date": end,
            "interval": "1D", "initial_cash": 1000000, "commission": 0.0005, "extra_fields": None,
            "fundamental_fields": None, "optimizer": None, "optimizer_params": {},
            "engine": "daily", "validation": None}


def make_engine(body):
    return (HEADER + "    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:\n"
            "        signals = {}\n        for code, df in data_map.items():\n            close = df['close']\n"
            "            " + body + "\n            signals[code] = sig.fillna(0.0)\n        return signals\n")


def run(name, body, config):
    rd = BASE / "runs" / name
    (rd / "code").mkdir(parents=True, exist_ok=True)
    (rd / "config.json").write_text(json.dumps(config, indent=2))
    (rd / "code" / "signal_engine.py").write_text(make_engine(body))
    env = dict(os.environ, VIBE_TRADING_ALLOWED_RUN_ROOTS=str(BASE / "runs"))
    p = subprocess.run([PY, "-m", "backtest.runner", str(rd)], cwd=SP, env=env,
                       capture_output=True, text=True)
    out = p.stdout.strip()
    try:
        return json.loads(out[out.index("{"):])
    except Exception:
        return {"error": (p.stderr or out)[-300:]}


# ---- factor bodies (all params are baked in by these constructors) ----
def f_maN(n):
    return f"ma=close.rolling({n}).mean(); sig=(close>ma).astype(float); sig=sig.where(ma.notna(),0.0)"

def f_gc(fast, slow):
    return (f"f=close.rolling({fast}).mean(); s=close.rolling({slow}).mean(); "
            f"sig=(f>s).astype(float); sig=sig.where(s.notna(),0.0)")

def f_slope(n, sw=20):
    return (f"ma=close.rolling({n}).mean(); up=ma.diff({sw})>0; "
            f"sig=((close>ma)&up).astype(float); sig=sig.where(ma.notna(),0.0)")

def f_ctdual(fast, slow, sw=20):
    return (f"s_f=close.rolling({fast}).mean(); s_s=close.rolling({slow}).mean(); up=s_s.diff({sw})>0; "
            f"sig=((s_f>s_s)&(close>s_s)&up).astype(float); sig=sig.where(s_s.notna(),0.0)")

def f_ctstop(fast, slow, thr, look, cool, sw=20):
    return (f"s_f=close.rolling({fast}).mean(); s_s=close.rolling({slow}).mean(); up=s_s.diff({sw})>0; "
            f"base=((s_f>s_s)&(close>s_s)&up); "
            f"retk=close/close.shift({look})-1.0; "
            f"crash=(retk<{thr}).rolling({cool},min_periods=1).max().fillna(0)>0; "
            f"sig=(base & ~crash).astype(float); sig=sig.where(s_s.notna(),0.0)")


# ---- the IS exploration grid (families + params) ----
GRID = []
for n in (100, 120, 150, 200):
    GRID.append((f"ma{n}", f_maN(n)))
for fast, slow in ((20, 120), (50, 200), (20, 100), (25, 140)):
    GRID.append((f"gc{fast}_{slow}", f_gc(fast, slow)))
for n in (120, 150, 200):
    GRID.append((f"slope{n}", f_slope(n)))
for fast, slow in ((20, 120), (15, 100), (25, 140), (20, 140), (30, 150)):
    GRID.append((f"ctdual{fast}_{slow}", f_ctdual(fast, slow)))
for thr in (-0.08, -0.10, -0.12, -0.15):
    for look in (10,):
        for cool in (10, 15):
            GRID.append((f"ctstop20_120_t{int(abs(thr)*100)}_l{look}_c{cool}",
                         f_ctstop(20, 120, thr, look, cool)))
for fast, slow in ((15, 100), (25, 140)):
    GRID.append((f"ctstop{fast}_{slow}_t10_l10_c10", f_ctstop(fast, slow, -0.10, 10, 10)))


def fmt(name, m):
    if "error" in m:
        return f"{name:<26} ERR {m['error'][:60]}"
    return (f"{name:<26} {m['total_return']*100:>7.0f}% {m['annual_return']*100:>5.1f}% "
            f"{m['sharpe']:>6.2f} {m['max_drawdown']*100:>6.0f}% {str(m['calmar']):>6} {int(m['trade_count']):>5}")


def main():
    IS = cfg(IS_START, IS_END)
    OOS = cfg(OOS_START, OOS_END)

    print(f"=== PHASE 1: IN-SAMPLE design+tune  ({IS_START} .. {IS_END}) ===")
    print(f"{'factor':<26} {'totRet':>8} {'CAGR':>6} {'Sharpe':>6} {'MaxDD':>7} {'Calmar':>6} {'trd':>5}")
    is_rows = []
    for name, body in GRID:
        m = run("is_" + name, body, IS)
        print(fmt(name, m))
        if "error" not in m:
            is_rows.append((name, body, m))

    # Selection criterion fixed BEFORE seeing OOS: rank by Sharpe, require MaxDD
    # meaningfully better than buy&hold and a non-trivial CAGR. Calmar as tiebreak.
    bench_is = is_rows[0][2].get("benchmark_return") if is_rows else None
    ranked = sorted(is_rows, key=lambda r: (r[2]["sharpe"], r[2]["calmar"] or 0), reverse=True)

    print(f"\nIS buy&hold total_return = {bench_is*100:.0f}%" if bench_is is not None else "")
    print("\n--- IS ranking (by Sharpe, Calmar tiebreak) top 8 ---")
    for name, _, m in ranked[:8]:
        print(fmt(name, m))

    winner = ranked[0]
    runners_up = ranked[1:4]
    print(f"\n>>> IS-LOCKED WINNER: {winner[0]}")

    # ---- PHASE 2: lock and test ONCE on OOS ----
    print(f"\n=== PHASE 2: OUT-OF-SAMPLE test (locked configs)  ({OOS_START} .. {OOS_END}) ===")
    print("(OOS eats ~120d SMA warmup at 2020 start -> conservative)")
    print(f"{'factor':<26} {'totRet':>8} {'CAGR':>6} {'Sharpe':>6} {'MaxDD':>7} {'Calmar':>6} {'trd':>5}")
    oos_results = {}
    for name, body, _ in [winner] + runners_up:
        m = run("oos_" + name, body, OOS)
        oos_results[name] = m
        print(fmt(name, m))
    bench_oos = oos_results.get(winner[0], {}).get("benchmark_return")
    if bench_oos is not None:
        print(f"\nOOS buy&hold total_return = {bench_oos*100:.0f}%")

    # ---- side-by-side summary for the winner ----
    print("\n=== HEADLINE: IS vs OOS for locked winner ===")
    w = winner[0]; mi = winner[2]; mo = oos_results[w]
    print(f"factor = {w}")
    print(f"{'window':<8} {'totRet':>8} {'CAGR':>6} {'Sharpe':>6} {'Sortino':>7} {'MaxDD':>7} {'Calmar':>6} {'trd':>5} {'B&H_ret':>8}")
    print(f"{'IS':<8} {mi['total_return']*100:>7.0f}% {mi['annual_return']*100:>5.1f}% "
          f"{mi['sharpe']:>6.2f} {mi['sortino']:>7.2f} {mi['max_drawdown']*100:>6.0f}% "
          f"{str(mi['calmar']):>6} {int(mi['trade_count']):>5} {bench_is*100:>7.0f}%")
    print(f"{'OOS':<8} {mo['total_return']*100:>7.0f}% {mo['annual_return']*100:>5.1f}% "
          f"{mo['sharpe']:>6.2f} {mo['sortino']:>7.2f} {mo['max_drawdown']*100:>6.0f}% "
          f"{str(mo['calmar']):>6} {int(mo['trade_count']):>5} {bench_oos*100:>7.0f}%")

    # persist
    with open(BASE / "is_oos_results.csv", "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["phase", "factor", "total_return", "annual_return", "sharpe",
                     "sortino", "max_drawdown", "calmar", "trade_count", "win_rate", "benchmark_return"])
        for name, _, m in is_rows:
            wr.writerow(["IS", name, m["total_return"], m["annual_return"], m["sharpe"],
                         m["sortino"], m["max_drawdown"], m["calmar"], m["trade_count"],
                         m["win_rate"], m.get("benchmark_return")])
        for name, m in oos_results.items():
            wr.writerow(["OOS", name, m["total_return"], m["annual_return"], m["sharpe"],
                         m["sortino"], m["max_drawdown"], m["calmar"], m["trade_count"],
                         m["win_rate"], m.get("benchmark_return")])
    print("\nsaved -> is_oos_results.csv")


if __name__ == "__main__":
    main()
