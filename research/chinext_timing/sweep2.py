"""Round 2: parameter-robustness of the 20/120 trend family + an asymmetric
crash-stop overlay. Same framework runner; we only vary the factor definition."""
import json, subprocess, os
from pathlib import Path

BASE = Path("/home/rooot/agent_invest_lab/research/chinext_timing")
SP = "/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages"
PY = "/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python"
CONFIG = {"source":"akshare","codes":["159915.SZ"],"start_date":"2012-01-01","end_date":"2026-05-21",
          "interval":"1D","initial_cash":1000000,"commission":0.0005,"extra_fields":None,
          "fundamental_fields":None,"optimizer":None,"optimizer_params":{},"engine":"daily","validation":None}
HEADER = "import pandas as pd\nimport numpy as np\nfrom typing import Dict\n\n\nclass SignalEngine:\n"

def ct(fast, slow, slopewin=20):
    return (f"s_f=close.rolling({fast}).mean(); s_s=close.rolling({slow}).mean(); "
            f"up=s_s.diff({slopewin})>0; sig=((s_f>s_s)&(close>s_s)&up).astype(float); sig=sig.where(s_s.notna(),0.0)")

CANDIDATES = {
    # robustness neighborhood of ct_dual (20/120)
    "ct_15_100": ct(15,100), "ct_20_100": ct(20,100),
    "ct_25_140": ct(25,140), "ct_20_140": ct(20,140), "ct_30_150": ct(30,150),
    # DESIGNED v2: ct_dual core + asymmetric fast crash-stop (force cash for cooldown
    # days after a sharp short-term drawdown; re-enter only when core trend re-confirms)
    "ct_stop": (
        "s20=close.rolling(20).mean(); s120=close.rolling(120).mean(); up=s120.diff(20)>0; "
        "base=((s20>s120)&(close>s120)&up); "
        "ret10=close/close.shift(10)-1.0; crash=(ret10<-0.10).rolling(10,min_periods=1).max().fillna(0)>0; "
        "sig=(base & ~crash).astype(float); sig=sig.where(s120.notna(),0.0)"
    ),
    # DESIGNED v3: ct core but exit faster via 60d trend break for the slow leg
    "ct_fastexit": (
        "s20=close.rolling(20).mean(); s120=close.rolling(120).mean(); s60=close.rolling(60).mean(); up=s120.diff(20)>0; "
        "long_cond=((s20>s120)&(close>s120)&up); exit_cond=(close<s60); "
        "raw=long_cond & ~exit_cond; sig=raw.astype(float); sig=sig.where(s120.notna(),0.0)"
    ),
}

def make_engine(body):
    return (HEADER + "    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:\n"
            "        signals = {}\n        for code, df in data_map.items():\n            close = df['close']\n"
            "            " + body + "\n            signals[code] = sig.fillna(0.0)\n        return signals\n")

def run_one(name, body):
    rd = BASE/"runs"/name; (rd/"code").mkdir(parents=True, exist_ok=True)
    (rd/"config.json").write_text(json.dumps(CONFIG, indent=2))
    (rd/"code"/"signal_engine.py").write_text(make_engine(body))
    env = dict(os.environ, VIBE_TRADING_ALLOWED_RUN_ROOTS=str(BASE/"runs"))
    p = subprocess.run([PY,"-m","backtest.runner",str(rd)], cwd=SP, env=env, capture_output=True, text=True)
    out = p.stdout.strip()
    try:
        return json.loads(out[out.index("{"):])
    except Exception:
        return {"error": (p.stderr or out)[-300:]}

print(f"{'factor':<12} {'totRet':>9} {'CAGR':>7} {'Sharpe':>7} {'MaxDD':>8} {'Calmar':>7} {'trades':>7}")
for name, body in CANDIDATES.items():
    m = run_one(name, body)
    if "error" in m: print(f"{name:<12} ERROR {m['error']}"); continue
    print(f"{name:<12} {m['total_return']*100:>8.1f}% {m['annual_return']*100:>6.1f}% "
          f"{m['sharpe']:>7.2f} {m['max_drawdown']*100:>7.1f}% {m['calmar']:>7} {int(m['trade_count']):>7}")
