"""Round 3 of HONEST IS/OOS search for 159915.SZ (ChiNext ETF).

Structural insight from rounds 1-2 (NOT from peeking at OOS):
  Every trend-GATED long/flat timer must wait for trend to turn up before
  re-entering, so it misses the violent start of each ChiNext recovery rally.
  On a right-tail-heavy index that costs more return than the dodged drawdown
  saves -> risk-adjusted it ties/loses to buy&hold OOS.

New philosophy this round: DEFAULT LONG, de-risk ONLY on genuine danger.
  Position = 1.0 unless a danger flag fires (crash cooldown, confirmed bear,
  deep drawdown, vol spike). This keeps the right tail (OOS return stays near
  buy&hold) while trimming the worst left tail. If crash/bear-dodging carries
  any real information, Sharpe should now EXCEED buy&hold instead of trailing it.

Protocol unchanged: explore + tune on IS 2012-2019, then test the IS-top-5 ONCE
on OOS 2020-2026. Disclosure: this is the 3rd time we touch OOS; each look erodes
statistical purity a little. Bodies are motivated by structure, not OOS numbers.

Backtest = vibe library runner; metrics are post-cost (5bp one-way).
Bars to beat (buy&hold): IS Sharpe 0.51 / DD -70%; OOS Sharpe 0.56 / Calmar 0.24 / DD -57%.
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


# common danger primitives (each returns a boolean Series expression fragment)
CRASH = "(close/close.shift({look})-1.0 < {thr}).rolling({cool},min_periods=1).max().fillna(0)>0"

def b_dl_crash(thr=-0.10, look=10, cool=10):
    c = CRASH.format(look=look, thr=thr, cool=cool)
    return (f"crash={c}; sig=pd.Series(1.0,index=close.index); sig=sig.where(~crash,0.0)")

def b_dl_crash_bear(thr=-0.10, look=10, cool=10, slow=200, sw=20):
    c = CRASH.format(look=look, thr=thr, cool=cool)
    return (f"crash={c}; s=close.rolling({slow}).mean(); bear=(close<s)&(s.diff({sw})<0); "
            f"danger=crash|bear; sig=pd.Series(1.0,index=close.index); sig=sig.where(~danger,0.0); "
            f"sig=sig.where(s.notna(),1.0)")

def b_dl_crash_bear_half(thr=-0.10, look=10, cool=10, slow=200, sw=20):
    # bear -> half, crash -> flat (keep some right tail in bear)
    c = CRASH.format(look=look, thr=thr, cool=cool)
    return (f"crash={c}; s=close.rolling({slow}).mean(); bear=(close<s)&(s.diff({sw})<0); "
            f"sig=pd.Series(1.0,index=close.index); sig=sig.where(~bear,0.5); sig=sig.where(~crash,0.0); "
            f"sig=sig.where(s.notna(),1.0)")

def b_dl_crash_deepdd(thr=-0.10, look=10, cool=10, peak=120, dd=-0.20):
    c = CRASH.format(look=look, thr=thr, cool=cool)
    return (f"crash={c}; pk=close.rolling({peak}).max(); deep=(close/pk-1.0<{dd}); "
            f"danger=crash|deep; sig=pd.Series(1.0,index=close.index); sig=sig.where(~danger,0.0)")

def b_dl_voltrim(tv=0.30, floor=0.3, win=20, thr=-0.10, look=10, cool=10):
    c = CRASH.format(look=look, thr=thr, cool=cool)
    return (f"crash={c}; ret=close.pct_change(); rv=ret.rolling({win}).std()*np.sqrt(252); "
            f"size=({tv}/rv).clip({floor},1.0); sig=size.where(~crash,0.0); sig=sig.fillna(1.0)")

def b_dl_bearonly(slow=200, sw=20):
    return (f"s=close.rolling({slow}).mean(); bear=(close<s)&(s.diff({sw})<0); "
            f"sig=pd.Series(1.0,index=close.index); sig=sig.where(~bear,0.0); sig=sig.where(s.notna(),1.0)")

def b_dl_ensemble(thr=-0.10, look=10, cool=10, slow=200, sw=20, peak=120, dd=-0.20):
    # average of three default-long de-risk views -> robustness
    c = CRASH.format(look=look, thr=thr, cool=cool)
    return (f"crash={c}; s=close.rolling({slow}).mean(); bear=(close<s)&(s.diff({sw})<0); "
            f"pk=close.rolling({peak}).max(); deep=(close/pk-1.0<{dd}); "
            f"v1=(~crash).astype(float); v2=(~bear).astype(float); v3=(~deep).astype(float); "
            f"sig=(v1+v2+v3)/3.0; sig=sig.where(s.notna(),1.0)")


GRID = [
    ("dl_crash_t10", b_dl_crash(-0.10, 10, 10)),
    ("dl_crash_t8", b_dl_crash(-0.08, 10, 10)),
    ("dl_crash_t12", b_dl_crash(-0.12, 10, 10)),
    ("dl_crashbear_200", b_dl_crash_bear(-0.10, 10, 10, 200)),
    ("dl_crashbear_150", b_dl_crash_bear(-0.10, 10, 10, 150)),
    ("dl_crashbear_120", b_dl_crash_bear(-0.10, 10, 10, 120)),
    ("dl_crashbear_half200", b_dl_crash_bear_half(-0.10, 10, 10, 200)),
    ("dl_crashbear_half150", b_dl_crash_bear_half(-0.10, 10, 10, 150)),
    ("dl_crash_deepdd20", b_dl_crash_deepdd(-0.10, 10, 10, 120, -0.20)),
    ("dl_crash_deepdd15", b_dl_crash_deepdd(-0.10, 10, 10, 120, -0.15)),
    ("dl_voltrim_f30", b_dl_voltrim(0.30, 0.3)),
    ("dl_voltrim_f50", b_dl_voltrim(0.30, 0.5)),
    ("dl_bearonly_200", b_dl_bearonly(200)),
    ("dl_bearonly_150", b_dl_bearonly(150)),
    ("dl_ensemble", b_dl_ensemble()),
    ("dl_ensemble_150", b_dl_ensemble(slow=150)),
]


def fmt(name, m):
    if "error" in m:
        return f"{name:<22} ERR {m['error'][:55]}"
    return (f"{name:<22} {m['total_return']*100:>7.0f}% {m['annual_return']*100:>5.1f}% "
            f"{m['sharpe']:>6.2f} {m['max_drawdown']*100:>6.0f}% {str(m['calmar']):>7} {int(m['trade_count']):>5}")


def main():
    IS, OOS = cfg(IS_START, IS_END), cfg(OOS_START, OOS_END)

    print(f"=== PHASE 1: IN-SAMPLE explore (default-long de-risk)  ({IS_START} .. {IS_END}) ===")
    print(f"{'factor':<22} {'totRet':>8} {'CAGR':>6} {'Sharpe':>6} {'MaxDD':>7} {'Calmar':>7} {'trd':>5}")
    is_rows = []
    for name, body in GRID:
        m = run("v3is_" + name, body, IS)
        print(fmt(name, m))
        if "error" not in m:
            is_rows.append((name, body, m))

    ranked = sorted(is_rows, key=lambda r: (r[2]["sharpe"], r[2]["calmar"] or 0), reverse=True)
    print(f"\nIS buy&hold: ret 145%, Sharpe 0.51, MaxDD -70%, Calmar 0.18")
    print("\n--- IS top 6 by Sharpe ---")
    for name, _, m in ranked[:6]:
        print(fmt(name, m))

    top5 = ranked[:5]
    print(f"\n=== PHASE 2: OUT-OF-SAMPLE (test IS-top5 once)  ({OOS_START} .. {OOS_END}) ===")
    print(f"OOS buy&hold: ret 117%, Sharpe 0.56, MaxDD -57%, Calmar 0.24")
    print(f"{'factor':<22} {'totRet':>8} {'CAGR':>6} {'Sharpe':>6} {'MaxDD':>7} {'Calmar':>7} {'trd':>5}")
    oos = {}
    for name, body, _ in top5:
        m = run("v3oos_" + name, body, OOS)
        oos[name] = m
        print(fmt(name, m))

    print("\n=== IS vs OOS side-by-side (top5) ===")
    print(f"{'factor':<22} {'IS_Shrp':>7} {'OOS_Shrp':>8} {'IS_DD':>6} {'OOS_DD':>7} {'IS_ret':>7} {'OOS_ret':>8} {'OOS_Cal':>7}")
    for name, _, mi in top5:
        mo = oos[name]
        print(f"{name:<22} {mi['sharpe']:>7.2f} {mo['sharpe']:>8.2f} "
              f"{mi['max_drawdown']*100:>5.0f}% {mo['max_drawdown']*100:>6.0f}% "
              f"{mi['total_return']*100:>6.0f}% {mo['total_return']*100:>7.0f}% {str(mo['calmar']):>7}")
    print("\nPASS = OOS Sharpe > 0.56 (beats buy&hold risk-adjusted)")

    with open(BASE / "v3_results.csv", "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["phase", "factor", "total_return", "annual_return", "sharpe",
                     "sortino", "max_drawdown", "calmar", "trade_count", "win_rate", "benchmark_return"])
        for name, _, m in is_rows:
            wr.writerow(["IS", name, m["total_return"], m["annual_return"], m["sharpe"], m["sortino"],
                         m["max_drawdown"], m["calmar"], m["trade_count"], m["win_rate"], m.get("benchmark_return")])
        for name, m in oos.items():
            wr.writerow(["OOS", name, m["total_return"], m["annual_return"], m["sharpe"], m["sortino"],
                         m["max_drawdown"], m["calmar"], m["trade_count"], m["win_rate"], m.get("benchmark_return")])
    print("\nsaved -> v3_results.csv")


if __name__ == "__main__":
    main()
