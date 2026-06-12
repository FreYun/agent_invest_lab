"""Single-index timing factor sweep for HS300 (510300.SH) and CSI1000 (512100.SH).

Train (IS) = 2017-01-01 → 2023-12-31     (7 years, covers 2018 bear, 2019-21
                                          bull, 2022 bear, 2023 grind)
Test (OOS) = 2024-01-01 → 2026-05-26     (covers 2024-02 small-cap crash and
                                          the subsequent recovery)

ETF used as a tradable index proxy because the vibe-trading akshare loader
routes 510xxx/512xxx through fund_etf_hist_sina (real prices, qfq-adjusted),
whereas 000300.SH / 000852.SH would hit stock_zh_a_hist and fail.

Cost: 5bp one-way (commission=0.0005). Daily bars.

Strategy families explored:

  default-long de-risk (carried over from chinext_timing/sweep_v3, validated
    structure for ChiNext): position=1 unless a danger flag fires. Variants:
    crash-only, crash+bear, crash+deep-dd, vol-trim, bear-only, ensemble.

  trend-on long-only (the "naive" comparison): position=1 only while a slow
    trend filter (MA200 up, donchian-up) holds, else flat. Known to miss
    snap-back rallies but kept as a reference.

  momentum (mostly relevant for ZZ1000 — small caps trend stronger): long
    when N-day momentum > 0, else flat.

We score IS by Sharpe (post-cost) primarily, with Calmar and turnover as
tie-breakers, then re-run the IS top 5 on OOS. We touch OOS exactly once per
index — disclosure: any subsequent peeking erodes statistical purity.
"""
import csv
import json
import os
import subprocess
from pathlib import Path

BASE = Path("/home/ubuntu/rooot/agent_invest_lab/research/index_timing")
SP = "/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages"
PY = "/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python"

UNIVERSE = [
    ("hs300", "510300.SH"),
    ("zz1000", "512100.SH"),
]

IS_START, IS_END = "2017-01-01", "2023-12-31"
OOS_START, OOS_END = "2024-01-01", "2026-05-26"

HEADER = (
    "import pandas as pd\n"
    "import numpy as np\n"
    "from typing import Dict\n\n\n"
    "class SignalEngine:\n"
)


def cfg(code: str, start: str, end: str) -> dict:
    return {
        "source": "akshare",
        "codes": [code],
        "start_date": start,
        "end_date": end,
        "interval": "1D",
        "initial_cash": 1_000_000,
        "commission": 0.0005,
        "extra_fields": None,
        "fundamental_fields": None,
        "optimizer": None,
        "optimizer_params": {},
        "engine": "daily",
        "validation": None,
    }


def make_engine(body: str) -> str:
    return (
        HEADER
        + "    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:\n"
        "        signals = {}\n"
        "        for code, df in data_map.items():\n"
        "            close = df['close']\n"
        "            high = df['high']\n"
        "            low = df['low']\n"
        "            " + body + "\n"
        "            signals[code] = sig.fillna(0.0).clip(0.0, 1.0)\n"
        "        return signals\n"
    )


def run(name: str, body: str, config: dict) -> dict:
    rd = BASE / "runs" / name
    (rd / "code").mkdir(parents=True, exist_ok=True)
    (rd / "config.json").write_text(json.dumps(config, indent=2))
    (rd / "code" / "signal_engine.py").write_text(make_engine(body))
    env = dict(os.environ, VIBE_TRADING_ALLOWED_RUN_ROOTS=str(BASE / "runs"))
    p = subprocess.run(
        [PY, "-m", "backtest.runner", str(rd)],
        cwd=SP, env=env, capture_output=True, text=True, timeout=300,
    )
    out = p.stdout.strip()
    try:
        return json.loads(out[out.index("{"):])
    except Exception:
        return {"error": (p.stderr or out)[-400:]}


# ────────────────────────── strategy bodies ───────────────────────────────

CRASH = "(close/close.shift({look})-1.0 < {thr}).rolling({cool},min_periods=1).max().fillna(0)>0"


def b_buyhold():
    return "sig=pd.Series(1.0,index=close.index)"


def b_dl_crash(thr=-0.10, look=10, cool=10):
    c = CRASH.format(look=look, thr=thr, cool=cool)
    return (
        f"crash={c}; "
        f"sig=pd.Series(1.0,index=close.index); sig=sig.where(~crash,0.0)"
    )


def b_dl_crash_bear(thr=-0.10, look=10, cool=10, slow=200, sw=20):
    c = CRASH.format(look=look, thr=thr, cool=cool)
    return (
        f"crash={c}; s=close.rolling({slow}).mean(); "
        f"bear=(close<s)&(s.diff({sw})<0); danger=crash|bear; "
        f"sig=pd.Series(1.0,index=close.index); sig=sig.where(~danger,0.0); "
        f"sig=sig.where(s.notna(),1.0)"
    )


def b_dl_crash_bear_half(thr=-0.10, look=10, cool=10, slow=200, sw=20):
    c = CRASH.format(look=look, thr=thr, cool=cool)
    return (
        f"crash={c}; s=close.rolling({slow}).mean(); "
        f"bear=(close<s)&(s.diff({sw})<0); "
        f"sig=pd.Series(1.0,index=close.index); sig=sig.where(~bear,0.5); "
        f"sig=sig.where(~crash,0.0); sig=sig.where(s.notna(),1.0)"
    )


def b_dl_deepdd(thr=-0.10, look=10, cool=10, peak=120, dd=-0.15):
    c = CRASH.format(look=look, thr=thr, cool=cool)
    return (
        f"crash={c}; pk=close.rolling({peak}).max(); deep=(close/pk-1.0<{dd}); "
        f"danger=crash|deep; sig=pd.Series(1.0,index=close.index); "
        f"sig=sig.where(~danger,0.0)"
    )


def b_dl_voltrim(tv=0.20, floor=0.3, win=20, thr=-0.10, look=10, cool=10):
    c = CRASH.format(look=look, thr=thr, cool=cool)
    return (
        f"crash={c}; ret=close.pct_change(); "
        f"rv=ret.rolling({win}).std()*np.sqrt(252); "
        f"size=({tv}/rv).clip({floor},1.0); sig=size.where(~crash,0.0); "
        f"sig=sig.fillna(1.0)"
    )


def b_dl_bearonly(slow=200, sw=20):
    return (
        f"s=close.rolling({slow}).mean(); bear=(close<s)&(s.diff({sw})<0); "
        f"sig=pd.Series(1.0,index=close.index); sig=sig.where(~bear,0.0); "
        f"sig=sig.where(s.notna(),1.0)"
    )


def b_dl_ensemble(thr=-0.10, look=10, cool=10, slow=200, sw=20, peak=120, dd=-0.15):
    c = CRASH.format(look=look, thr=thr, cool=cool)
    return (
        f"crash={c}; s=close.rolling({slow}).mean(); "
        f"bear=(close<s)&(s.diff({sw})<0); pk=close.rolling({peak}).max(); "
        f"deep=(close/pk-1.0<{dd}); "
        f"v1=(~crash).astype(float); v2=(~bear).astype(float); v3=(~deep).astype(float); "
        f"sig=(v1+v2+v3)/3.0; sig=sig.where(s.notna(),1.0)"
    )


def b_trendon(slow=200, sw=20):
    # long-only: position=1 only while MA slow is rising and price>MA, else flat
    return (
        f"s=close.rolling({slow}).mean(); on=(close>s)&(s.diff({sw})>0); "
        f"sig=on.astype(float); sig=sig.where(s.notna(),0.0)"
    )


def b_donchian(win=120):
    # long when close > rolling-high(win) of previous bar, exit on rolling-low
    return (
        f"hh=high.shift(1).rolling({win}).max(); ll=low.shift(1).rolling({win}).min(); "
        f"long=close>hh; flat=close<ll; state=pd.Series(np.nan,index=close.index); "
        f"state=state.where(~long,1.0); state=state.where(~flat,0.0); "
        f"sig=state.ffill().fillna(0.0)"
    )


def b_momentum(look=60, thr=0.0):
    return (
        f"mom=close/close.shift({look})-1.0; sig=(mom>{thr}).astype(float); "
        f"sig=sig.where(mom.notna(),0.0)"
    )


def b_mom_de_risk(look=60, thr=0.0, crash_thr=-0.10, crash_look=10, cool=10):
    c = CRASH.format(look=crash_look, thr=crash_thr, cool=cool)
    return (
        f"mom=close/close.shift({look})-1.0; on=(mom>{thr}).astype(float); "
        f"crash={c}; sig=on.where(~crash,0.0); sig=sig.where(mom.notna(),0.0)"
    )


GRID = [
    ("buyhold",            b_buyhold()),
    # default-long de-risk family (transferred from chinext_timing/sweep_v3)
    ("dl_crash_t10",       b_dl_crash(-0.10, 10, 10)),
    ("dl_crash_t08",       b_dl_crash(-0.08, 10, 10)),
    ("dl_crash_t12",       b_dl_crash(-0.12, 10, 10)),
    ("dl_crashbear_200",   b_dl_crash_bear(-0.10, 10, 10, 200, 20)),
    ("dl_crashbear_150",   b_dl_crash_bear(-0.10, 10, 10, 150, 20)),
    ("dl_crashbear_120",   b_dl_crash_bear(-0.10, 10, 10, 120, 20)),
    ("dl_crashbear_half200", b_dl_crash_bear_half(-0.10, 10, 10, 200, 20)),
    ("dl_crashbear_half150", b_dl_crash_bear_half(-0.10, 10, 10, 150, 20)),
    ("dl_deepdd15_p120",   b_dl_deepdd(-0.10, 10, 10, 120, -0.15)),
    ("dl_deepdd20_p120",   b_dl_deepdd(-0.10, 10, 10, 120, -0.20)),
    ("dl_voltrim_f30",     b_dl_voltrim(0.20, 0.3)),
    ("dl_voltrim_f50",     b_dl_voltrim(0.20, 0.5)),
    ("dl_bearonly_200",    b_dl_bearonly(200, 20)),
    ("dl_bearonly_150",    b_dl_bearonly(150, 20)),
    ("dl_ensemble",        b_dl_ensemble()),
    # trend-on long-only (reference family)
    ("trendon_200_20",     b_trendon(200, 20)),
    ("trendon_120_20",     b_trendon(120, 20)),
    ("donchian_120",       b_donchian(120)),
    ("donchian_60",        b_donchian(60)),
    # momentum (mostly for ZZ1000)
    ("mom_60",             b_momentum(60)),
    ("mom_120",            b_momentum(120)),
    ("mom_60_derisk",      b_mom_de_risk(60, 0.0, -0.10, 10, 10)),
]


METRIC_FIELDS = [
    "total_return", "annual_return", "sharpe", "max_drawdown",
    "calmar", "sortino", "trade_count",
]


def collect(metrics: dict) -> dict:
    return {k: metrics.get(k) for k in METRIC_FIELDS}


def fmt_row(name: str, m: dict) -> str:
    def f(x, p=2):
        if x is None:
            return "  -  "
        try:
            return f"{float(x):>+{p+5}.{p}f}"
        except Exception:
            return f"{str(x):>7}"
    return (
        f"  {name:<22} "
        f"ret={f(m.get('total_return'),3)}  "
        f"ann={f(m.get('annual_return'),3)}  "
        f"shp={f(m.get('sharpe'),2)}  "
        f"dd={f(m.get('max_drawdown'),2)}  "
        f"cal={f(m.get('calmar'),2)}  "
        f"trd={int(m.get('trade_count') or 0):>4d}"
    )


def main():
    summary = {}
    for label, code in UNIVERSE:
        print(f"\n══════ {label.upper()}  ({code})  IS {IS_START}..{IS_END} ══════")
        is_rows = []
        for name, body in GRID:
            run_name = f"{label}_IS_{name}"
            res = run(run_name, body, cfg(code, IS_START, IS_END))
            if "error" in res:
                print(f"  {name:<22} ERROR: {res['error'][:120]}")
                continue
            m = collect(res.get("metrics", res))
            is_rows.append((name, body, m))
            print(fmt_row(name, m))
        # Rank IS by Sharpe (asc None → skip)
        ranked = sorted(
            (r for r in is_rows if r[2].get("sharpe") is not None),
            key=lambda r: r[2]["sharpe"],
            reverse=True,
        )
        top5 = ranked[:5]
        print(f"\n  IS top-5 by Sharpe:")
        for name, _, m in top5:
            print(fmt_row(name, m))

        print(f"\n══════ {label.upper()}  ({code})  OOS {OOS_START}..{OOS_END} ══════")
        oos_rows = []
        for name, body, _ in top5:
            run_name = f"{label}_OOS_{name}"
            res = run(run_name, body, cfg(code, OOS_START, OOS_END))
            if "error" in res:
                print(f"  {name:<22} ERROR: {res['error'][:120]}")
                continue
            m = collect(res.get("metrics", res))
            oos_rows.append((name, m))
            print(fmt_row(name, m))
        # also buy&hold OOS for benchmark
        bh = run(f"{label}_OOS_buyhold", b_buyhold(), cfg(code, OOS_START, OOS_END))
        bhm = collect(bh.get("metrics", bh)) if "error" not in bh else {}
        if bhm:
            print(fmt_row("buyhold (bench)", bhm))
        summary[label] = {
            "code": code,
            "is_top5": [(n, m) for n, _, m in top5],
            "oos_top5": oos_rows,
            "oos_buyhold": bhm,
        }

    # write csv summary
    out_csv = BASE / "summary.csv"
    with out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["index", "split", "name"] + METRIC_FIELDS)
        for label, info in summary.items():
            for n, m in info["is_top5"]:
                w.writerow([label, "IS", n] + [m.get(k) for k in METRIC_FIELDS])
            for n, m in info["oos_top5"]:
                w.writerow([label, "OOS", n] + [m.get(k) for k in METRIC_FIELDS])
            if info["oos_buyhold"]:
                w.writerow([label, "OOS", "buyhold"] + [info["oos_buyhold"].get(k) for k in METRIC_FIELDS])
    print(f"\nWrote {out_csv}")


if __name__ == "__main__":
    main()
