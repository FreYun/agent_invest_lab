"""有色散户申赎 contrarian 因子 — 参数 robustness sweep。

样本 <2 年 → 纯探索：不挑单点最优，看整个参数邻域是否普遍 work。
标的：三个有色子指数各用自身散户信号（swnf/csnf/gznf）。
网格：source(5) × cumN(4) × z_win(3) × thr(3) = 180 配置 × 3 标的 = 540 回测。
方向：contrarian(散户热→减仓) 为主；另跑 momentum 方向做证伪对照。
成本 5bp 单边，T+1（pos=sig.shift(1)），砍 60 bar 预热。
最后用 512400 有色金属ETF（可交易，tracks 000819）验证头部配置。
"""
from __future__ import annotations
from pathlib import Path
from itertools import product
import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/nonferrous_sr")
DATA = BASE / "data"
COMMISSION = 0.0005
WARMUP = 60

UNIVERSE = [  # (label, index_code, price_file)
    ("swnf", "000819", "swnf_000819.csv"),
    ("csnf", "930708", "csnf_930708.csv"),
    ("gznf", "399395", "gznf_399395.csv"),
]
SOURCES = ["net", "net_ex_aip", "applied", "applied_ex_aip", "redeemed"]
CUM_NS = [5, 10, 15, 20]
Z_WINS = [30, 60, 90]
THRESHOLDS = [0.5, 1.0, 1.5]


def load_close(fn: str) -> pd.Series:
    df = pd.read_csv(DATA / fn, parse_dates=["date"]).set_index("date").sort_index()
    s = df["close"].astype(float); s.index = pd.to_datetime(s.index).normalize()
    return s


def load_sr() -> pd.DataFrame:
    # index_code 按字符串读，否则 000819 被解析成整数 819
    return pd.read_csv(BASE / "sr_daily.csv", parse_dates=["date"], dtype={"index_code": str})


def sr_series(sr, code, col):
    sub = sr[(sr["index_code"].astype(str) == code) & (sr["persona"] == "个人")]
    s = sub.set_index("date")[col].astype(float).sort_index()
    s.index = pd.to_datetime(s.index).normalize()
    return s[~s.index.duplicated(keep="last")]


def cumN_z(s, n, z_win):
    cum = s.rolling(n, min_periods=max(3, n // 2)).sum()
    mu = cum.rolling(z_win, min_periods=20).mean()
    sd = cum.rolling(z_win, min_periods=20).std()
    return (cum - mu) / sd


def make_signal(close, raw, cumN, z_win, thr, direction):
    """direction='contra': z>thr→0(减), z<-thr→1(加); 'mom': 反过来。默认0.5。"""
    z = cumN_z(raw, cumN, z_win).reindex(close.index, method="ffill")
    hi, lo = (0.0, 1.0) if direction == "contra" else (1.0, 0.0)
    state = pd.Series(np.nan, index=close.index)
    state = state.where(~(z > thr), hi)
    state = state.where(~(z < -thr), lo)
    return state.ffill().fillna(0.5)


def backtest(close, sig):
    sig = sig.clip(0, 1).fillna(0).reindex(close.index).fillna(0)
    ret = close.pct_change().fillna(0)
    pos = sig.shift(1).fillna(0)
    turnover = pos.diff().abs().fillna(0)
    strat = pos * ret - turnover * COMMISSION
    s = strat.iloc[WARMUP:]; bh = ret.iloc[WARMUP:]; pe = pos.iloc[WARMUP:]
    n = len(s)
    if n == 0:
        return None
    def stats(r):
        eq = (1 + r).cumprod()
        tot = float(eq.iloc[-1] - 1)
        ann = float((1 + tot) ** (252 / max(n, 1)) - 1)
        sd = float(r.std(ddof=1)) or 0.0
        shp = float(r.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
        dd = float((eq / eq.cummax() - 1).min())
        cal = float(ann / abs(dd)) if dd < 0 else 0.0
        return ann, shp, dd, cal
    ann, shp, dd, cal = stats(s)
    b_ann, b_shp, b_dd, b_cal = stats(bh)
    flips = int((turnover.iloc[WARMUP:] > 0.001).sum())
    yrs = n / 252
    return {"ann": ann, "shp": shp, "dd": dd, "cal": cal, "exp": float(pe.mean()),
            "flips_yr": flips / yrs, "bh_cal": b_cal, "bh_shp": b_shp, "bh_dd": b_dd,
            "dcal": cal - b_cal, "dshp": shp - b_shp}


def run(direction):
    sr = load_sr()
    pairs = [(lab, load_close(fn), {s: sr_series(sr, code, s) for s in SOURCES})
             for lab, code, fn in UNIVERSE]
    rows = []
    for src, cumN, z_win, thr in product(SOURCES, CUM_NS, Z_WINS, THRESHOLDS):
        for lab, close, srcs in pairs:
            raw = srcs[src]
            if raw.empty:
                continue
            m = backtest(close, make_signal(close, raw, cumN, z_win, thr, direction))
            if m is None:
                continue
            rows.append({"dir": direction, "src": src, "cumN": cumN, "z_win": z_win,
                         "thr": thr, "label": lab, **m,
                         "beat_cal": int(m["dcal"] > 0), "beat_shp": int(m["dshp"] > 0)})
    return pd.DataFrame(rows)


def report(df, direction):
    print(f"\n{'='*70}\n方向 = {direction}  ({df['src'].nunique()}源×{len(CUM_NS)}cumN×{len(Z_WINS)}zwin×{len(THRESHOLDS)}thr × 3标的)\n{'='*70}")
    agg = df.groupby(["src", "cumN", "z_win", "thr"]).agg(
        beat_cal_n=("beat_cal", "sum"), mean_dcal=("dcal", "mean"),
        median_dcal=("dcal", "mean"), mean_flips=("flips_yr", "mean")).reset_index()
    n_cfg = len(agg)
    print(f"配置数={n_cfg} (每配置跨3标的)")
    print(f"  ≥3/3 标的击败 buyhold Calmar: {(agg['beat_cal_n']==3).sum()}/{n_cfg} = {(agg['beat_cal_n']==3).mean():.0%}")
    print(f"  ≥2/3 标的击败 buyhold Calmar: {(agg['beat_cal_n']>=2).sum()}/{n_cfg} = {(agg['beat_cal_n']>=2).mean():.0%}")
    print(f"  全网格 median dCalmar (跨标的均值): {agg['mean_dcal'].median():+.3f}")
    print(f"  全网格 mean   dCalmar: {agg['mean_dcal'].mean():+.3f}")
    print(f"  平均年翻转: {df['flips_yr'].mean():.0f}")
    print("\n  -- 按信号源 (各36配置×3标的) beat_cal% / mean dCalmar --")
    bs = df.groupby("src").agg(beat_cal_pct=("beat_cal", "mean"), mean_dcal=("dcal", "mean"),
                               mean_exp=("exp", "mean"), mean_flips=("flips_yr", "mean"))
    print("  " + bs.to_string(float_format=lambda x: f"{x:+.3f}").replace("\n", "\n  "))
    print("\n  -- 按标的 beat_cal% / mean dCalmar --")
    bl = df.groupby("label").agg(beat_cal_pct=("beat_cal", "mean"), mean_dcal=("dcal", "mean"))
    print("  " + bl.to_string(float_format=lambda x: f"{x:+.3f}").replace("\n", "\n  "))
    return agg


def main():
    print("buyhold 基准:")
    sr = load_sr()
    for lab, code, fn in UNIVERSE:
        c = load_close(fn); m = backtest(c, pd.Series(1.0, index=c.index))
        print(f"  {lab} {code}: cal={m['bh_cal']:+.2f} shp={m['bh_shp']:+.2f} dd={m['bh_dd']:+.2f}")

    df_c = run("contra"); agg_c = report(df_c, "contra")
    df_m = run("mom"); report(df_m, "mom")
    df_all = pd.concat([df_c, df_m])
    df_all.to_csv(BASE / "robustness_full.csv", index=False)
    agg_c.to_csv(BASE / "robustness_agg_contra.csv", index=False)

    print(f"\n{'='*70}\ncontra 头部15配置 (按跨标的 mean dCalmar):\n{'='*70}")
    top = agg_c.sort_values("mean_dcal", ascending=False).head(15)
    print(top.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))

    # ── 512400 ETF 验证头部配置（可交易代理）──
    print(f"\n{'='*70}\n512400 有色金属ETF 验证 (signal=000819散户, 可交易):\n{'='*70}")
    etf = load_close("etf_512400.csv")
    m_bh = backtest(etf, pd.Series(1.0, index=etf.index))
    print(f"  buyhold ETF: cal={m_bh['bh_cal']:+.2f} shp={m_bh['bh_shp']:+.2f} dd={m_bh['bh_dd']:+.2f}")
    for _, r in top.head(8).iterrows():
        raw = sr_series(sr, "000819", r["src"])
        m = backtest(etf, make_signal(etf, raw, int(r["cumN"]), int(r["z_win"]), r["thr"], "contra"))
        print(f"  {r['src']:>14} cumN={int(r['cumN']):2d} zw={int(r['z_win']):2d} thr={r['thr']}: "
              f"cal={m['cal']:+.2f}(d{m['dcal']:+.2f}) shp={m['shp']:+.2f} dd={m['dd']:+.2f} "
              f"exp={m['exp']:.2f} flips/yr={m['flips_yr']:.0f}")


if __name__ == "__main__":
    main()
