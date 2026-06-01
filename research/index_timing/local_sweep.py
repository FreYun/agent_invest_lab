"""Local single-index timing sweep — no network, no vibe-trading runner.

Uses cached CSVs under `data/` produced by the akshare loader, and applies a
self-contained daily long-only backtest with 5bp one-way commission.

Train (IS) = 2017-01-01 → 2023-12-31
Test (OOS) = 2024-01-01 → 2026-05-26

Universe: 510300.SH (HS300 ETF) and 512100.SH (CSI 1000 ETF) as tradable
proxies for the indices (akshare doesn't route 000300/000852 to a quotes API).

Signal semantics:
  sig in [0,1] → long-only fractional position, applied on the NEXT bar's
  return (sig.shift(1) * close.pct_change()). Cost = abs(turnover) * 0.0005
  is debited the bar the position changes.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/index_timing")
DATA = BASE / "data"

UNIVERSE = [
    ("hs300", "510300.SH"),
    ("zz1000", "512100.SH"),
]

IS_START, IS_END = "2017-01-01", "2023-12-31"
OOS_START, OOS_END = "2024-01-01", "2026-05-26"
COMMISSION = 0.0005


def load(code: str) -> pd.DataFrame:
    df = pd.read_csv(DATA / f"{code}.csv", parse_dates=[0], index_col=0)
    df.index.name = "date"
    return df.sort_index()


def slice_range(df: pd.DataFrame, start: str, end: str, warmup: int = 260) -> pd.DataFrame:
    """Slice to [start,end] but include `warmup` bars of pre-history so
    rolling windows are already warm at `start`."""
    start_ts = pd.Timestamp(start)
    # window of pre-history
    pre = df.loc[:start_ts].iloc[:-1].tail(warmup)
    main = df.loc[start_ts:end]
    return pd.concat([pre, main])


def backtest(df_window: pd.DataFrame, sig: pd.Series, eval_start: str) -> dict:
    """Apply `sig` (0..1 long fraction) to `df_window`, then compute metrics on
    the slice from `eval_start` onward so warmup bars don't inflate stats."""
    sig = sig.clip(0.0, 1.0).fillna(0.0).reindex(df_window.index).fillna(0.0)
    ret = df_window["close"].pct_change().fillna(0.0)
    pos = sig.shift(1).fillna(0.0)
    turnover = pos.diff().abs().fillna(pos.iloc[0])
    cost = turnover * COMMISSION
    strat_ret = pos * ret - cost
    # restrict to eval window
    mask = df_window.index >= pd.Timestamp(eval_start)
    sr = strat_ret.loc[mask]
    pos_e = pos.loc[mask]
    bh = ret.loc[mask]
    if sr.empty:
        return {"error": "empty_eval_window"}
    eq = (1.0 + sr).cumprod()
    eq_bh = (1.0 + bh).cumprod()
    n = len(sr)
    total_return = float(eq.iloc[-1] - 1.0)
    annual_return = float((1.0 + total_return) ** (252.0 / n) - 1.0) if n > 0 else 0.0
    daily_std = float(sr.std(ddof=1)) if n > 1 else 0.0
    sharpe = float(sr.mean() / daily_std * np.sqrt(252.0)) if daily_std > 0 else 0.0
    downside = sr.where(sr < 0, 0.0)
    d_std = float(downside.std(ddof=1)) if n > 1 else 0.0
    sortino = float(sr.mean() / d_std * np.sqrt(252.0)) if d_std > 0 else 0.0
    peak = eq.cummax()
    dd = (eq / peak - 1.0)
    max_dd = float(dd.min())
    calmar = float(annual_return / abs(max_dd)) if max_dd < 0 else 0.0
    # turnover stats (counted on eval window only)
    to_e = turnover.loc[mask]
    flips = int((to_e > 0).sum())
    avg_exposure = float(pos_e.mean())
    bh_total = float(eq_bh.iloc[-1] - 1.0)
    bh_ann = float((1.0 + bh_total) ** (252.0 / n) - 1.0)
    bh_std = float(bh.std(ddof=1))
    bh_sharpe = float(bh.mean() / bh_std * np.sqrt(252.0)) if bh_std > 0 else 0.0
    peak_bh = eq_bh.cummax()
    bh_dd = float((eq_bh / peak_bh - 1.0).min())
    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "calmar": calmar,
        "trade_flips": flips,
        "avg_exposure": avg_exposure,
        "bh_total_return": bh_total,
        "bh_annual_return": bh_ann,
        "bh_sharpe": bh_sharpe,
        "bh_max_drawdown": bh_dd,
        "bars": n,
    }


# ────────────────────────── signal bodies ──────────────────────────────────

def s_buyhold(df):
    return pd.Series(1.0, index=df.index)


def _crash(close, look, thr, cool):
    return (close / close.shift(look) - 1.0 < thr).rolling(cool, min_periods=1).max().fillna(0).astype(bool)


def s_dl_crash(df, thr=-0.10, look=10, cool=10):
    c = _crash(df["close"], look, thr, cool)
    s = pd.Series(1.0, index=df.index)
    return s.where(~c, 0.0)


def s_dl_crash_bear(df, thr=-0.10, look=10, cool=10, slow=200, sw=20, bear_to=0.0):
    c = _crash(df["close"], look, thr, cool)
    ma = df["close"].rolling(slow).mean()
    bear = (df["close"] < ma) & (ma.diff(sw) < 0)
    s = pd.Series(1.0, index=df.index)
    s = s.where(~bear, bear_to)
    s = s.where(~c, 0.0)
    s = s.where(ma.notna(), 1.0)
    return s


def s_dl_deepdd(df, thr=-0.10, look=10, cool=10, peak=120, dd=-0.15):
    c = _crash(df["close"], look, thr, cool)
    pk = df["close"].rolling(peak).max()
    deep = (df["close"] / pk - 1.0 < dd)
    s = pd.Series(1.0, index=df.index)
    return s.where(~(c | deep), 0.0)


def s_dl_voltrim(df, tv=0.20, floor=0.3, win=20, thr=-0.10, look=10, cool=10):
    c = _crash(df["close"], look, thr, cool)
    ret = df["close"].pct_change()
    rv = ret.rolling(win).std() * np.sqrt(252.0)
    size = (tv / rv).clip(floor, 1.0)
    s = size.where(~c, 0.0).fillna(1.0)
    return s


def s_dl_bearonly(df, slow=200, sw=20):
    ma = df["close"].rolling(slow).mean()
    bear = (df["close"] < ma) & (ma.diff(sw) < 0)
    s = pd.Series(1.0, index=df.index)
    s = s.where(~bear, 0.0)
    s = s.where(ma.notna(), 1.0)
    return s


def s_dl_ensemble(df, thr=-0.10, look=10, cool=10, slow=200, sw=20, peak=120, dd=-0.15):
    c = _crash(df["close"], look, thr, cool)
    ma = df["close"].rolling(slow).mean()
    bear = (df["close"] < ma) & (ma.diff(sw) < 0)
    pk = df["close"].rolling(peak).max()
    deep = (df["close"] / pk - 1.0 < dd)
    s = ((~c).astype(float) + (~bear).astype(float) + (~deep).astype(float)) / 3.0
    return s.where(ma.notna(), 1.0)


def s_trendon(df, slow=200, sw=20):
    ma = df["close"].rolling(slow).mean()
    on = (df["close"] > ma) & (ma.diff(sw) > 0)
    return on.astype(float).where(ma.notna(), 0.0)


def s_donchian(df, win=120):
    hh = df["high"].shift(1).rolling(win).max()
    ll = df["low"].shift(1).rolling(win).min()
    long_ = df["close"] > hh
    flat = df["close"] < ll
    state = pd.Series(np.nan, index=df.index)
    state = state.where(~long_, 1.0)
    state = state.where(~flat, 0.0)
    return state.ffill().fillna(0.0)


def s_mom(df, look=60, thr=0.0):
    mom = df["close"] / df["close"].shift(look) - 1.0
    s = (mom > thr).astype(float)
    return s.where(mom.notna(), 0.0)


def s_mom_derisk(df, look=60, thr=0.0, c_thr=-0.10, c_look=10, cool=10):
    mom = df["close"] / df["close"].shift(look) - 1.0
    on = (mom > thr).astype(float)
    c = _crash(df["close"], c_look, c_thr, cool)
    s = on.where(~c, 0.0)
    return s.where(mom.notna(), 0.0)


def s_rsi(df, win=14, hi=70, lo=30, slow=200):
    delta = df["close"].diff()
    up = delta.clip(lower=0).rolling(win).mean()
    dn = (-delta.clip(upper=0)).rolling(win).mean()
    rs = up / dn.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    ma = df["close"].rolling(slow).mean()
    # default long; exit when RSI > hi in a downtrend, re-enter when RSI < lo
    sig = pd.Series(1.0, index=df.index)
    # combined: cut to 0 when RSI < lo AND below MA (panic), back when RSI > 50
    state = pd.Series(np.nan, index=df.index)
    state = state.where(~((rsi < lo) & (df["close"] < ma)), 0.0)
    state = state.where(~(rsi > 50), 1.0)
    return state.ffill().fillna(1.0)


# ────────────────────────── DD-first family ────────────────────────────────

def s_vol_target(df, tv=0.15, win=20, cap=1.0):
    """Constant-vol sizing — `tv` annual vol target, clamped to [0, cap]."""
    ret = df["close"].pct_change()
    rv = ret.rolling(win).std() * np.sqrt(252.0)
    size = (tv / rv).clip(0.0, cap)
    return size.fillna(1.0)


def s_vol_target_ema(df, tv=0.15, span=30, cap=1.0):
    """EMA-smoothed vol target — fewer flips than rolling-std version."""
    ret = df["close"].pct_change()
    rv = (ret.pow(2).ewm(span=span, adjust=False).mean()).pow(0.5) * np.sqrt(252.0)
    size = (tv / rv).clip(0.0, cap)
    return size.fillna(1.0)


def s_trail_dd(df, peak_win=120, dd=-0.10, re_ma=60):
    """Trailing-DD cap. Flat if price < peak * (1+dd). Re-enter when close
    crosses back above MA(re_ma)."""
    close = df["close"]
    pk = close.rolling(peak_win).max()
    flat = (close / pk - 1.0) < dd
    on = close > close.rolling(re_ma).mean()
    state = pd.Series(np.nan, index=df.index)
    state = state.where(~flat, 0.0)
    state = state.where(~on, 1.0)
    return state.ffill().fillna(1.0)


def s_chandelier(df, atr_win=22, k=3.0, hh_win=22, re_ma=60):
    """Chandelier exit — flat if close < (rolling-high - k*ATR). Re-enter
    when close > MA(re_ma)."""
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(atr_win).mean()
    chand = high.rolling(hh_win).max() - k * atr
    flat = close < chand
    on = close > close.rolling(re_ma).mean()
    state = pd.Series(np.nan, index=df.index)
    state = state.where(~flat, 0.0)
    state = state.where(~on, 1.0)
    return state.ffill().fillna(1.0)


def s_regime_lever(df, vol_win=20, slow=200, sw=20, hi_vol=0.25, low_lever=0.3):
    """Trend up + low vol → full; trend up + high vol → low_lever; trend
    down → flat."""
    ret = df["close"].pct_change()
    rv = ret.rolling(vol_win).std() * np.sqrt(252.0)
    ma = df["close"].rolling(slow).mean()
    up = (df["close"] > ma) & (ma.diff(sw) > 0)
    base = pd.Series(low_lever, index=df.index)
    base = base.where(rv > hi_vol, 1.0)
    base = base.where(up, 0.0)
    return base.where(ma.notna(), 1.0)


def s_donchian_trail(df, brk=60, atr_win=22, k=3.0):
    """Donchian entry, ATR-based trailing stop on a tracked rolling high."""
    high, low, close = df["high"], df["low"], df["close"]
    hh = high.shift(1).rolling(brk).max()
    long_ = close > hh
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(atr_win).mean()
    stop_line = high.rolling(brk).max() - k * atr
    stop = close < stop_line
    state = pd.Series(np.nan, index=df.index)
    state = state.where(~long_, 1.0)
    state = state.where(~stop, 0.0)
    return state.ffill().fillna(0.0)


def s_vol_target_trend(df, tv=0.15, win=20, slow=200, sw=20, cap=1.0):
    """Vol target × trend gate — only sized long while trend is up, else flat."""
    ret = df["close"].pct_change()
    rv = ret.rolling(win).std() * np.sqrt(252.0)
    size = (tv / rv).clip(0.0, cap)
    ma = df["close"].rolling(slow).mean()
    up = (df["close"] > ma) & (ma.diff(sw) > 0)
    sig = size.where(up, 0.0)
    return sig.where(ma.notna(), size).fillna(0.0)


def s_dd_ladder(df, peak_win=120, dd_half=-0.07, dd_flat=-0.15, re_ma=60):
    """Two-tier DD ladder — half position after `dd_half`, flat after
    `dd_flat`; re-enter when close > MA(re_ma)."""
    close = df["close"]
    pk = close.rolling(peak_win).max()
    dd = close / pk - 1.0
    flat = dd < dd_flat
    half = (dd < dd_half) & ~flat
    on = close > close.rolling(re_ma).mean()
    state = pd.Series(np.nan, index=df.index)
    state = state.where(~flat, 0.0)
    state = state.where(~half, 0.5)
    state = state.where(~on, 1.0)
    return state.ffill().fillna(1.0)


def s_bbands(df, win=120, k=2.0):
    ma = df["close"].rolling(win).mean()
    sd = df["close"].rolling(win).std()
    lower = ma - k * sd
    upper = ma + k * sd
    # default long; flat when close breaches lower band AND below ma (continuation crash)
    state = pd.Series(np.nan, index=df.index)
    state = state.where(~(df["close"] < lower), 0.0)
    state = state.where(~(df["close"] > ma), 1.0)
    return state.ffill().fillna(1.0)


GRID = [
    ("buyhold",             s_buyhold,         {}),
    # ---- default-long de-risk family ----
    ("dl_crash_t10",        s_dl_crash,        dict(thr=-0.10, look=10, cool=10)),
    ("dl_crash_t08",        s_dl_crash,        dict(thr=-0.08, look=10, cool=10)),
    ("dl_crash_t12",        s_dl_crash,        dict(thr=-0.12, look=10, cool=10)),
    ("dl_crashbear_200",    s_dl_crash_bear,   dict(slow=200, sw=20)),
    ("dl_crashbear_150",    s_dl_crash_bear,   dict(slow=150, sw=20)),
    ("dl_crashbear_120",    s_dl_crash_bear,   dict(slow=120, sw=20)),
    ("dl_crashbear_half200", s_dl_crash_bear,  dict(slow=200, sw=20, bear_to=0.5)),
    ("dl_crashbear_half150", s_dl_crash_bear,  dict(slow=150, sw=20, bear_to=0.5)),
    ("dl_deepdd15_p120",    s_dl_deepdd,       dict(peak=120, dd=-0.15)),
    ("dl_deepdd20_p120",    s_dl_deepdd,       dict(peak=120, dd=-0.20)),
    ("dl_voltrim_f30",      s_dl_voltrim,      dict(tv=0.20, floor=0.3)),
    ("dl_voltrim_f50",      s_dl_voltrim,      dict(tv=0.20, floor=0.5)),
    ("dl_bearonly_200",     s_dl_bearonly,     dict(slow=200, sw=20)),
    ("dl_bearonly_150",     s_dl_bearonly,     dict(slow=150, sw=20)),
    ("dl_ensemble",         s_dl_ensemble,     {}),
    # ---- trend-on long-only reference ----
    ("trendon_200_20",      s_trendon,         dict(slow=200, sw=20)),
    ("trendon_120_20",      s_trendon,         dict(slow=120, sw=20)),
    ("donchian_120",        s_donchian,        dict(win=120)),
    ("donchian_60",         s_donchian,        dict(win=60)),
    # ---- momentum ----
    ("mom_60",              s_mom,             dict(look=60)),
    ("mom_120",             s_mom,             dict(look=120)),
    ("mom_20",              s_mom,             dict(look=20)),
    ("mom_60_derisk",       s_mom_derisk,      dict(look=60)),
    # ---- mean-reversion-aware ----
    ("rsi_panic_re_entry",  s_rsi,             dict(win=14, lo=30, slow=200)),
    ("bbands_break_flat",   s_bbands,          dict(win=120, k=2.0)),
    # ---- DD-first family (vol-target, trailing-stop, regime, ladder) ----
    ("vol_tgt_15_20",       s_vol_target,      dict(tv=0.15, win=20)),
    ("vol_tgt_20_20",       s_vol_target,      dict(tv=0.20, win=20)),
    ("vol_tgt_10_20",       s_vol_target,      dict(tv=0.10, win=20)),
    ("vol_tgt_ema_15_30",   s_vol_target_ema,  dict(tv=0.15, span=30)),
    ("vol_tgt_ema_20_60",   s_vol_target_ema,  dict(tv=0.20, span=60)),
    ("trail_dd10_p120",     s_trail_dd,        dict(peak_win=120, dd=-0.10, re_ma=60)),
    ("trail_dd15_p120",     s_trail_dd,        dict(peak_win=120, dd=-0.15, re_ma=60)),
    ("trail_dd10_p60",      s_trail_dd,        dict(peak_win=60,  dd=-0.10, re_ma=40)),
    ("chand_22_k3",         s_chandelier,      dict(atr_win=22, k=3.0, hh_win=22, re_ma=60)),
    ("chand_22_k2",         s_chandelier,      dict(atr_win=22, k=2.0, hh_win=22, re_ma=60)),
    ("regime_lev_hi25_l30", s_regime_lever,    dict(hi_vol=0.25, low_lever=0.3)),
    ("regime_lev_hi20_l50", s_regime_lever,    dict(hi_vol=0.20, low_lever=0.5)),
    ("donch60_trail_k3",    s_donchian_trail,  dict(brk=60, atr_win=22, k=3.0)),
    ("donch120_trail_k3",   s_donchian_trail,  dict(brk=120, atr_win=22, k=3.0)),
    ("vt15_x_trend200",     s_vol_target_trend, dict(tv=0.15, slow=200, sw=20)),
    ("dd_ladder_7_15",      s_dd_ladder,       dict(dd_half=-0.07, dd_flat=-0.15)),
    ("dd_ladder_10_20",     s_dd_ladder,       dict(dd_half=-0.10, dd_flat=-0.20)),
]


METRIC_COLS = [
    "total_return", "annual_return", "sharpe", "max_drawdown", "calmar",
    "sortino", "trade_flips", "avg_exposure",
]
BH_COLS = ["bh_total_return", "bh_annual_return", "bh_sharpe", "bh_max_drawdown"]


def fmt(name: str, m: dict) -> str:
    g = m.get
    return (
        f"  {name:<24} ret={g('total_return',0):+.3f}  ann={g('annual_return',0):+.3f}  "
        f"shp={g('sharpe',0):+.2f}  dd={g('max_drawdown',0):+.2f}  cal={g('calmar',0):+.2f}  "
        f"sor={g('sortino',0):+.2f}  exp={g('avg_exposure',0):.2f}  flips={int(g('trade_flips',0))}"
    )


def main():
    summary = {}
    for label, code in UNIVERSE:
        df = load(code)
        print(f"\n══════ {label.upper()} ({code})  IS {IS_START}..{IS_END}  data rows={len(df)} ══════")
        win = slice_range(df, IS_START, IS_END, warmup=260)
        is_rows = []
        for name, fn, kw in GRID:
            sig = fn(win, **kw)
            m = backtest(win, sig, IS_START)
            is_rows.append((name, fn, kw, m))
            print(fmt(name, m))
        ranked = sorted(is_rows, key=lambda r: r[3].get("calmar", -9e9), reverse=True)
        top5 = ranked[:8]
        print(f"\n  IS top-8 by Calmar (post 5bp):")
        for name, *_rest, m in top5:
            print(fmt(name, m))

        print(f"\n══════ {label.upper()} ({code})  OOS {OOS_START}..{OOS_END} ══════")
        win_oos = slice_range(df, OOS_START, OOS_END, warmup=260)
        oos_rows = []
        for name, fn, kw, _ in top5:
            sig = fn(win_oos, **kw)
            m = backtest(win_oos, sig, OOS_START)
            oos_rows.append((name, m))
            print(fmt(name, m))
        # bench
        bh_sig = s_buyhold(win_oos)
        bh_m = backtest(win_oos, bh_sig, OOS_START)
        print(fmt("buyhold (bench)", bh_m))

        summary[label] = {
            "code": code,
            "is_top5": [(n, m) for n, *_r, m in top5],
            "oos_top5": oos_rows,
            "oos_buyhold": bh_m,
        }

    out = BASE / "summary.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["index", "split", "name"] + METRIC_COLS)
        for label, info in summary.items():
            for n, m in info["is_top5"]:
                w.writerow([label, "IS", n] + [m.get(k) for k in METRIC_COLS])
            for n, m in info["oos_top5"]:
                w.writerow([label, "OOS", n] + [m.get(k) for k in METRIC_COLS])
            w.writerow([label, "OOS", "buyhold"] + [info["oos_buyhold"].get(k) for k in METRIC_COLS])
    print(f"\nWrote {out}")

    # raw json dump for transparency
    (BASE / "summary.json").write_text(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
