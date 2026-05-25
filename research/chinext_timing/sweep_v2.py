"""Round 2 of HONEST IS/OOS factor search for 159915.SZ (ChiNext ETF).

Motivation: the CT-Stop family (round 1) reduced drawdown OOS but lost to
buy&hold on a risk-adjusted basis — it gave up too much upside (death-by-whipsaw
+ slow SMA120 re-entry missed the 2020/2024-25 rallies). This round tries
mechanically different families aimed at those two failure modes:

  * VOLATILITY-TARGET sizing  -> continuous 0..1 position; small in turbulence,
    full in calm uptrends. Classic Sharpe-improver (Barroso/Santa-Clara style).
  * FASTER trend / re-entry   -> SMA60 instead of SMA120 (re-enter rallies sooner).
  * DONCHIAN breakout         -> high/low channel, turtle-style state machine.
  * VOL-REGIME switch         -> full when calm-uptrend, half when volatile-uptrend.
  * DUAL-TIMEFRAME            -> long trend AND short (20d) momentum confirm.
  * TRAILING stop             -> trend but exit on >15% drop from 60d high.
  * RISK-MANAGED MOMENTUM     -> tsmom sign x vol-target size.

Protocol (unchanged, the whole point):
  IS  2012-01-01 .. 2019-12-31  -> explore ALL of the above + params, rank by Sharpe.
  OOS 2020-01-01 .. 2026-05-21  -> test ONLY the IS-top-5, once. No tuning on OOS.

Backtest = vibe library `backtest.runner`. We only design factors. Metrics are
POST-COST (commission 5bp one-way), so turnover from continuous sizing is already
penalized in the reported numbers.
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


# ---------- factor bodies ----------
def b_voltarget(tv, win=20):
    return (f"ret=close.pct_change(); rv=ret.rolling({win}).std()*np.sqrt(252); "
            f"sig=({tv}/rv).clip(0.0,1.0); sig=sig.where(rv.notna(),0.0)")

def b_trend_vt(tv, slow=120, win=20):
    return (f"s_s=close.rolling({slow}).mean(); up=s_s.diff(20)>0; trend=((close>s_s)&up).astype(float); "
            f"ret=close.pct_change(); rv=ret.rolling({win}).std()*np.sqrt(252); vt=({tv}/rv).clip(0.0,1.0); "
            f"sig=trend*vt; sig=sig.where(s_s.notna(),0.0)")

def b_trend_vt_stop(tv, slow=120, win=20, thr=-0.10, look=10, cool=10):
    return (f"s_s=close.rolling({slow}).mean(); up=s_s.diff(20)>0; trend=((close>s_s)&up).astype(float); "
            f"ret=close.pct_change(); rv=ret.rolling({win}).std()*np.sqrt(252); vt=({tv}/rv).clip(0.0,1.0); "
            f"retk=close/close.shift({look})-1.0; crash=(retk<{thr}).rolling({cool},min_periods=1).max().fillna(0)>0; "
            f"sig=trend*vt; sig=sig.where(~crash,0.0); sig=sig.where(s_s.notna(),0.0)")

def b_fast_stop(slow=60, thr=-0.10, look=10, cool=10):
    return (f"s_m=close.rolling({slow}).mean(); up=s_m.diff(10)>0; base=((close>s_m)&up); "
            f"retk=close/close.shift({look})-1.0; crash=(retk<{thr}).rolling({cool},min_periods=1).max().fillna(0)>0; "
            f"sig=(base & ~crash).astype(float); sig=sig.where(s_m.notna(),0.0)")

def b_donchian(ne, nx):
    return (f"hh=df['high'].rolling({ne}).max().shift(1); ll=df['low'].rolling({nx}).min().shift(1); "
            f"raw=pd.Series(np.nan,index=close.index); raw[close>hh]=1.0; raw[close<ll]=0.0; "
            f"sig=raw.ffill().fillna(0.0)")

def b_volregime(slow=120, win=20, medwin=120):
    return (f"ret=close.pct_change(); rv=ret.rolling({win}).std(); med=rv.rolling({medwin}).median(); "
            f"s_s=close.rolling({slow}).mean(); up=s_s.diff(20)>0; trend=((close>s_s)&up); calm=rv<med; "
            f"sig=trend.astype(float)*np.where(calm,1.0,0.5); sig=pd.Series(sig,index=close.index); "
            f"sig=sig.where(s_s.notna(),0.0)")

def b_dualtf(slow=120, shortwin=20):
    return (f"s_s=close.rolling({slow}).mean(); up=s_s.diff(20)>0; longtrend=((close>s_s)&up); "
            f"mom=close/close.shift({shortwin})-1.0; shortok=mom>0; "
            f"sig=(longtrend & shortok).astype(float); sig=sig.where(s_s.notna(),0.0)")

def b_trail(slow=120, peakwin=60, dd=-0.15):
    return (f"s_s=close.rolling({slow}).mean(); up=s_s.diff(20)>0; trend=((close>s_s)&up); "
            f"pk=close.rolling({peakwin}).max(); ddv=close/pk-1.0; nostop=ddv>{dd}; "
            f"sig=(trend & nostop).astype(float); sig=sig.where(s_s.notna(),0.0)")

def b_rmm(tv, mom=120, win=20):
    return (f"m=close/close.shift({mom})-1.0; long=(m>0).astype(float); "
            f"ret=close.pct_change(); rv=ret.rolling({win}).std()*np.sqrt(252); vt=({tv}/rv).clip(0.0,1.0); "
            f"sig=long*vt; sig=sig.where(m.notna(),0.0)")

def discretize(body, step=0.2):
    # round continuous weight to discrete steps to cut turnover/cost
    k = int(round(1 / step))
    return body + f"; sig=(sig*{k}).round()/{k}.0"


GRID = []
for tv in (0.20, 0.25, 0.30):
    GRID.append((f"voltgt_tv{int(tv*100)}", b_voltarget(tv)))
for tv in (0.25, 0.30):
    GRID.append((f"trendVT_tv{int(tv*100)}", b_trend_vt(tv)))
    GRID.append((f"trendVTdisc_tv{int(tv*100)}", discretize(b_trend_vt(tv))))
    GRID.append((f"trendVTstop_tv{int(tv*100)}", b_trend_vt_stop(tv)))
    GRID.append((f"rmm_tv{int(tv*100)}", b_rmm(tv)))
GRID.append(("fast60_stop", b_fast_stop(60)))
GRID.append(("fast90_stop", b_fast_stop(90)))
for ne, nx in ((40, 20), (60, 30), (120, 60)):
    GRID.append((f"donch{ne}_{nx}", b_donchian(ne, nx)))
GRID.append(("volregime", b_volregime()))
GRID.append(("dualtf20", b_dualtf(120, 20)))
GRID.append(("dualtf60", b_dualtf(120, 60)))
GRID.append(("trail15", b_trail(120, 60, -0.15)))
GRID.append(("trail20", b_trail(120, 60, -0.20)))


def fmt(name, m):
    if "error" in m:
        return f"{name:<22} ERR {m['error'][:55]}"
    return (f"{name:<22} {m['total_return']*100:>7.0f}% {m['annual_return']*100:>5.1f}% "
            f"{m['sharpe']:>6.2f} {m['max_drawdown']*100:>6.0f}% {str(m['calmar']):>7} {int(m['trade_count']):>5}")


def main():
    IS, OOS = cfg(IS_START, IS_END), cfg(OOS_START, OOS_END)

    print(f"=== PHASE 1: IN-SAMPLE explore  ({IS_START} .. {IS_END}) ===")
    print(f"{'factor':<22} {'totRet':>8} {'CAGR':>6} {'Sharpe':>6} {'MaxDD':>7} {'Calmar':>7} {'trd':>5}")
    is_rows = []
    for name, body in GRID:
        m = run("v2is_" + name, body, IS)
        print(fmt(name, m))
        if "error" not in m:
            is_rows.append((name, body, m))

    bench_is = is_rows[0][2].get("benchmark_return") if is_rows else None
    ranked = sorted(is_rows, key=lambda r: (r[2]["sharpe"], r[2]["calmar"] or 0), reverse=True)
    print(f"\nIS buy&hold: ret 145%, Sharpe 0.51, MaxDD -70%")
    print("\n--- IS top 6 by Sharpe ---")
    for name, _, m in ranked[:6]:
        print(fmt(name, m))

    top5 = ranked[:5]
    print(f"\n=== PHASE 2: OUT-OF-SAMPLE (test IS-top5 once)  ({OOS_START} .. {OOS_END}) ===")
    print(f"OOS buy&hold: ret 117%, Sharpe 0.56, MaxDD -57%, Calmar 0.24")
    print(f"{'factor':<22} {'totRet':>8} {'CAGR':>6} {'Sharpe':>6} {'MaxDD':>7} {'Calmar':>7} {'trd':>5}")
    oos = {}
    for name, body, _ in top5:
        m = run("v2oos_" + name, body, OOS)
        oos[name] = m
        print(fmt(name, m))

    print("\n=== IS vs OOS side-by-side (top5) ===")
    print(f"{'factor':<22} {'IS_Shrp':>7} {'OOS_Shrp':>8} {'IS_DD':>6} {'OOS_DD':>7} {'IS_ret':>7} {'OOS_ret':>8}")
    for name, _, mi in top5:
        mo = oos[name]
        print(f"{name:<22} {mi['sharpe']:>7.2f} {mo['sharpe']:>8.2f} "
              f"{mi['max_drawdown']*100:>5.0f}% {mo['max_drawdown']*100:>6.0f}% "
              f"{mi['total_return']*100:>6.0f}% {mo['total_return']*100:>7.0f}%")
    print("\nOOS bar to clear: Sharpe > 0.56 AND/OR Calmar > 0.24 (beat buy&hold risk-adjusted)")

    with open(BASE / "v2_results.csv", "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["phase", "factor", "total_return", "annual_return", "sharpe",
                     "sortino", "max_drawdown", "calmar", "trade_count", "win_rate", "benchmark_return"])
        for name, _, m in is_rows:
            wr.writerow(["IS", name, m["total_return"], m["annual_return"], m["sharpe"], m["sortino"],
                         m["max_drawdown"], m["calmar"], m["trade_count"], m["win_rate"], m.get("benchmark_return")])
        for name, m in oos.items():
            wr.writerow(["OOS", name, m["total_return"], m["annual_return"], m["sharpe"], m["sortino"],
                         m["max_drawdown"], m["calmar"], m["trade_count"], m["win_rate"], m.get("benchmark_return")])
    print("\nsaved -> v2_results.csv")


if __name__ == "__main__":
    main()
