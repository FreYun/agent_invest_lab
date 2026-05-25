"""Round 3: robustness of the ct_stop crash-stop params + sub-period split.
Confirms the headline factor is not an artifact of one parameter or one crash."""
import json, subprocess, os
from pathlib import Path

BASE = Path("/home/rooot/agent_invest_lab/research/chinext_timing")
SP = "/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages"
PY = "/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python"
HEADER = "import pandas as pd\nimport numpy as np\nfrom typing import Dict\n\n\nclass SignalEngine:\n"

def cfg(start, end):
    return {"source":"akshare","codes":["159915.SZ"],"start_date":start,"end_date":end,
            "interval":"1D","initial_cash":1000000,"commission":0.0005,"extra_fields":None,
            "fundamental_fields":None,"optimizer":None,"optimizer_params":{},"engine":"daily","validation":None}

def ct_stop(fast=20, slow=120, thr=-0.10, look=10, cool=10):
    return (f"s_f=close.rolling({fast}).mean(); s_s=close.rolling({slow}).mean(); up=s_s.diff(20)>0; "
            f"base=((s_f>s_s)&(close>s_s)&up); "
            f"retk=close/close.shift({look})-1.0; crash=(retk<{thr}).rolling({cool},min_periods=1).max().fillna(0)>0; "
            f"sig=(base & ~crash).astype(float); sig=sig.where(s_s.notna(),0.0)")

def make_engine(body):
    return (HEADER + "    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:\n"
            "        signals = {}\n        for code, df in data_map.items():\n            close = df['close']\n"
            "            " + body + "\n            signals[code] = sig.fillna(0.0)\n        return signals\n")

def run(name, body, config):
    rd = BASE/"runs"/name; (rd/"code").mkdir(parents=True, exist_ok=True)
    (rd/"config.json").write_text(json.dumps(config, indent=2))
    (rd/"code"/"signal_engine.py").write_text(make_engine(body))
    env = dict(os.environ, VIBE_TRADING_ALLOWED_RUN_ROOTS=str(BASE/"runs"))
    p = subprocess.run([PY,"-m","backtest.runner",str(rd)], cwd=SP, env=env, capture_output=True, text=True)
    out = p.stdout.strip()
    try: return json.loads(out[out.index("{"):])
    except Exception: return {"error": (p.stderr or out)[-200:]}

FULL = cfg("2012-01-01","2026-05-21")

print("=== A) crash-stop PARAMETER robustness (core 20/120, full period) ===")
print(f"{'variant':<22} {'totRet':>8} {'CAGR':>6} {'Sharpe':>7} {'MaxDD':>7} {'Calmar':>7} {'trades':>7}")
grid = [
    ("thr=-8%",  ct_stop(thr=-0.08)),
    ("thr=-10%(base)", ct_stop(thr=-0.10)),
    ("thr=-12%", ct_stop(thr=-0.12)),
    ("thr=-15%", ct_stop(thr=-0.15)),
    ("cool=5",   ct_stop(cool=5)),
    ("cool=15",  ct_stop(cool=15)),
    ("cool=20",  ct_stop(cool=20)),
    ("look=5",   ct_stop(look=5)),
    ("look=15",  ct_stop(look=15)),
    ("core25/140", ct_stop(fast=25, slow=140)),
    ("core15/100", ct_stop(fast=15, slow=100)),
]
for name, body in grid:
    m = run("r3_"+name.replace("%","").replace("/","_").replace("=","").replace("(","").replace(")",""), body, FULL)
    if "error" in m: print(f"{name:<22} ERR {m['error']}"); continue
    print(f"{name:<22} {m['total_return']*100:>7.0f}% {m['annual_return']*100:>5.1f}% "
          f"{m['sharpe']:>7.2f} {m['max_drawdown']*100:>6.0f}% {m['calmar']:>7} {int(m['trade_count']):>7}")

print("\n=== B) SUB-PERIOD check (base ct_stop 20/120/-10%/10/10) vs Buy&Hold ===")
print(f"{'period':<18} {'strat_ret':>10} {'strat_DD':>9} {'sharpe':>7} {'B&H_ret':>9} {'trades':>7}")
for label, s, e in [("2012-2015(bull)","2012-01-01","2015-12-31"),
                    ("2016-2018(bear)","2016-01-01","2018-12-31"),
                    ("2019-2021(bull)","2019-01-01","2021-12-31"),
                    ("2022-2026(mixed)","2022-01-01","2026-05-21"),
                    ("first half 12-18","2012-01-01","2018-12-31"),
                    ("2nd half 19-26","2019-01-01","2026-05-21")]:
    m = run("sp_"+label.split("(")[0].strip().replace(" ","_"), ct_stop(), cfg(s,e))
    if "error" in m: print(f"{label:<18} ERR {m['error']}"); continue
    print(f"{label:<18} {m['total_return']*100:>9.1f}% {m['max_drawdown']*100:>8.1f}% "
          f"{m['sharpe']:>7.2f} {m['benchmark_return']*100:>8.1f}% {int(m['trade_count']):>7}")
