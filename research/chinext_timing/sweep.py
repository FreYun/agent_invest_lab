"""Orchestrator: build candidate timing factors as framework run dirs and
run them through the library's backtest runner. We only DESIGN factors here;
the backtest engine is the vibe-trading library's `backtest.runner`.
"""
import json, subprocess, sys, os
from pathlib import Path

BASE = Path("/home/rooot/agent_invest_lab/research/chinext_timing")
SP = "/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages"
PY = "/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python"

CONFIG = {
    "source": "akshare", "codes": ["159915.SZ"],
    "start_date": "2012-01-01", "end_date": "2026-05-21",
    "interval": "1D", "initial_cash": 1000000, "commission": 0.0005,
    "extra_fields": None, "fundamental_fields": None,
    "optimizer": None, "optimizer_params": {}, "engine": "daily", "validation": None,
}

HEADER = "import pandas as pd\nimport numpy as np\nfrom typing import Dict\n\n\nclass SignalEngine:\n"

# Each body computes `sig` (a 0/1 float Series aligned to close.index) inside the loop.
CANDIDATES = {
    "ma120": (
        "close>SMA120",
        "ma=close.rolling(120).mean(); sig=(close>ma).astype(float); sig=sig.where(ma.notna(),0.0)",
    ),
    "gc_50_200": (
        "golden cross SMA50>SMA200",
        "f=close.rolling(50).mean(); s=close.rolling(200).mean(); sig=(f>s).astype(float); sig=sig.where(s.notna(),0.0)",
    ),
    "gc_20_120": (
        "fast golden cross SMA20>SMA120",
        "f=close.rolling(20).mean(); s=close.rolling(120).mean(); sig=(f>s).astype(float); sig=sig.where(s.notna(),0.0)",
    ),
    "tsmom_120": (
        "120d time-series momentum>0",
        "mom=close/close.shift(120)-1.0; sig=(mom>0).astype(float); sig=sig.where(mom.notna(),0.0)",
    ),
    "slope200": (
        "close>SMA200 AND SMA200 rising",
        "ma=close.rolling(200).mean(); up=ma.diff(20)>0; sig=((close>ma)&up).astype(float); sig=sig.where(ma.notna(),0.0)",
    ),
    # Designed composite: fast trend cross + medium trend + slope-up confirmation
    "ct_dual": (
        "DESIGNED: SMA20>SMA120 AND close>SMA120 AND SMA120 rising",
        "s20=close.rolling(20).mean(); s120=close.rolling(120).mean(); up=s120.diff(20)>0; "
        "sig=((s20>s120)&(close>s120)&up).astype(float); sig=sig.where(s120.notna(),0.0)",
    ),
}


def make_engine(body: str) -> str:
    return (
        HEADER
        + "    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:\n"
        + "        signals = {}\n"
        + "        for code, df in data_map.items():\n"
        + "            close = df['close']\n"
        + "            " + body + "\n"
        + "            signals[code] = sig.fillna(0.0)\n"
        + "        return signals\n"
    )


def run_one(name: str, body: str) -> dict:
    rd = BASE / "runs" / name
    (rd / "code").mkdir(parents=True, exist_ok=True)
    (rd / "config.json").write_text(json.dumps(CONFIG, indent=2))
    (rd / "code" / "signal_engine.py").write_text(make_engine(body))
    env = dict(os.environ, VIBE_TRADING_ALLOWED_RUN_ROOTS=str(BASE / "runs"))
    p = subprocess.run([PY, "-m", "backtest.runner", str(rd)],
                       cwd=SP, env=env, capture_output=True, text=True)
    try:
        # runner prints metrics JSON to stdout (last JSON object)
        out = p.stdout.strip()
        start = out.index("{")
        return json.loads(out[start:])
    except Exception:
        return {"error": p.stderr.strip()[-300:] or p.stdout.strip()[-300:]}


def main():
    rows = []
    for name, (desc, body) in CANDIDATES.items():
        m = run_one(name, body)
        if "error" in m:
            print(f"[{name}] ERROR: {m['error']}")
            continue
        rows.append((name, desc, m))
        print(f"[{name}] done  ret={m.get('total_return'):.2%} sharpe={m.get('sharpe'):.2f} "
              f"maxDD={m.get('max_drawdown'):.2%} calmar={m.get('calmar')} trades={int(m.get('trade_count',0))}")

    bench = rows[0][2].get("benchmark_return") if rows else None
    print("\n==== COMPARISON (159915.SZ, 2012-01 .. 2026-05) ====")
    print(f"{'factor':<12} {'totRet':>9} {'CAGR':>7} {'Sharpe':>7} {'Sortino':>8} {'MaxDD':>8} {'Calmar':>7} {'trades':>7} {'win%':>6}")
    for name, desc, m in rows:
        print(f"{name:<12} {m['total_return']*100:>8.1f}% {m['annual_return']*100:>6.1f}% "
              f"{m['sharpe']:>7.2f} {m['sortino']:>8.2f} {m['max_drawdown']*100:>7.1f}% "
              f"{m['calmar']:>7} {int(m['trade_count']):>7} {m['win_rate']*100:>5.0f}")
    if bench is not None:
        print(f"{'BUY&HOLD':<12} {bench*100:>8.1f}% {'':>7} {'':>7} {'':>8} {'-69.6%':>8} {'':>7} {'0':>7}")
    # persist
    import csv
    with open(BASE / "sweep_results.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["factor","desc","total_return","annual_return","sharpe","sortino","max_drawdown","calmar","trade_count","win_rate"])
        for name, desc, m in rows:
            w.writerow([name,desc,m['total_return'],m['annual_return'],m['sharpe'],m['sortino'],m['max_drawdown'],m['calmar'],m['trade_count'],m['win_rate']])
    print("\nsaved -> sweep_results.csv")


if __name__ == "__main__":
    main()
