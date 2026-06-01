"""Multi-source combo factor anchored on 散户申赎 contrarian.

Diagnostic verdict (in-window): retail_z corr −0.16 (contra) ; 主力净流入 +0.00 (NOISE, drop) ;
集中度分位 +0.17, IR动量 +0.15 (trend, OPPOSITE sign → complementary).

Combo "bear-score" (减仓强度) = retail_z  − w_m*mom_z − w_c*conc_z
  散户过热(+) & 动量走弱(−mom) & 不拥挤(−conc) ⇒ score 高 ⇒ 减仓.
position = contrarian map on score. Compare vs retail-only baseline.
We do NOT optimize weights — equal/few priors + perturbation robustness.

⚠️ sample = 15 months (2024-08→2026-05), all bear/range regime. Exploratory only.
"""
from __future__ import annotations
from pathlib import Path
from itertools import product
import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/innodrug_sr")
DATA = BASE / "data"
COMMISSION = 0.0005
WARMUP = 90


def load_all(px_file="159992.SZ.csv"):
    sr = pd.read_csv(BASE / "innodrug_sr_daily.csv", parse_dates=["date"])
    sr = sr[sr["persona"] == "个人"].set_index("date").sort_index()
    sr.index = pd.to_datetime(sr.index).normalize()
    sr["imbalance"] = (sr["applied"] - sr["redeemed"]) / (sr["applied"] + sr["redeemed"]).replace(0, np.nan)
    m = pd.read_csv(DATA / "sector_BK000208_market.csv", parse_dates=["date"]).set_index("date")
    fd = pd.read_csv(DATA / "sector_BK000208_factor_detail.csv", parse_dates=["date"]).set_index("date")
    f = pd.read_csv(DATA / "sector_BK000208_factor.csv", parse_dates=["date"]).set_index("date")
    px = pd.read_csv(DATA / px_file, parse_dates=[0]); px.columns = [c.lower() for c in px.columns]
    px = px.set_index(px.columns[0]).sort_index(); px.index = pd.to_datetime(px.index).normalize()
    idx = px.loc["2024-08-01":].index
    df = pd.DataFrame(index=idx)
    df["close"] = px["close"]
    df["sr_imbalance"] = sr["imbalance"].reindex(idx, method="ffill")
    df["sr_net"] = sr["net"].reindex(idx, method="ffill")
    df["conc_q"] = fd["集中度分位"].reindex(idx, method="ffill")
    df["ir_mom"] = f["信息比率动量"].reindex(idx, method="ffill")
    df["main_inflow"] = m["main_inflow"].reindex(idx, method="ffill")
    return df


def zwin(s, n, w):
    mp = min(max(3, n // 2), n)
    cum = s.rolling(n, min_periods=mp).sum()
    return (cum - cum.rolling(w, min_periods=20).mean()) / cum.rolling(w, min_periods=20).std()


def metrics(r):
    n = len(r)
    if n == 0: return {"ann":0,"shp":0,"dd":0,"cal":0}
    eq=(1+r).cumprod(); tot=float(eq.iloc[-1]-1); ann=float((1+tot)**(252/max(n,1))-1)
    sd=float(r.std(ddof=1)) if n>1 else 0; shp=float(r.mean()/sd*np.sqrt(252)) if sd>0 else 0
    dd=float((eq/eq.cummax()-1).min()); cal=float(ann/abs(dd)) if dd<0 else 0
    return {"ann":ann,"shp":shp,"dd":dd,"cal":cal}


def backtest(df, sig, want_monthly=False):
    sig = sig.clip(0,1).reindex(df.index).ffill().fillna(0.5)
    ret = df["close"].pct_change().fillna(0)
    pos = sig.shift(1).fillna(0); turn = pos.diff().abs().fillna(0)
    strat = pos*ret - turn*COMMISSION
    s=strat.iloc[WARMUP:]; p=pos.iloc[WARMUP:]; b=ret.iloc[WARMUP:]; t=turn.iloc[WARMUP:]
    n=len(s); m=metrics(s); bm=metrics(b)
    half=n//2
    h1=metrics(s.iloc[:half]); h2=metrics(s.iloc[half:])
    exc=(s-b)
    mo=exc.resample("ME").apply(lambda x:(1+x).prod()-1) if want_monthly else None
    out={**m,"exp":float(p.mean()),"flips_yr":float((t>0.001).sum()/(n/252)),
         "bh_cal":bm["cal"],"bh_ann":bm["ann"],"h1_cal":h1["cal"],"h2_cal":h2["cal"],
         "h1_shp":h1["shp"],"h2_shp":h2["shp"]}
    if want_monthly: out["monthly"]=mo
    return out


def make_score(df, n, w, w_m, w_c, src="sr_imbalance"):
    retail_z = zwin(df[src], n, w)
    mom_z = zwin(df["ir_mom"], 1, w)
    conc_z = zwin(df["conc_q"], 1, w)
    return retail_z - w_m*mom_z - w_c*conc_z   # bear-score (high=减仓)


def pos_from_score(score, thr):
    st = pd.Series(np.nan, index=score.index)
    st = st.where(~(score > thr), 0.0)
    st = st.where(~(score < -thr), 1.0)
    return st.ffill().fillna(0.5)


def main():
    df = load_all()
    print(f"window {df.index.min().date()}..{df.index.max().date()} n={len(df)}  (15mo, all bear/range)")
    bh = backtest(df, pd.Series(1.0, index=df.index))
    print(f"buyhold: ann={bh['bh_ann']:+.3f} cal={bh['bh_cal']:+.2f}  (H1 vs H2 regime split below)\n")

    n, w, thr = 15, 60, 1.0
    # baselines vs combos
    variants = {
        "retail_only (imbalance)":  pos_from_score(zwin(df["sr_imbalance"],n,w), thr),
        "retail_only (net)":        pos_from_score(zwin(df["sr_net"],n,w), thr),
        "combo retail-mom":         pos_from_score(make_score(df,n,w,0.5,0.0), thr),
        "combo retail-conc":        pos_from_score(make_score(df,n,w,0.0,0.5), thr),
        "combo retail-mom-conc EW": pos_from_score(make_score(df,n,w,0.5,0.5), thr),
        "combo retail+inflow(对照)": pos_from_score(zwin(df["sr_imbalance"],n,w)+0.5*zwin(df["main_inflow"],n,w), thr),
    }
    print(f"  {'variant':<28}{'cal':>6}{'shp':>6}{'ann':>7}{'dd':>7}{'H1cal':>7}{'H2cal':>7}{'exp':>5}{'flip':>6}")
    rows={}
    for name, sig in variants.items():
        m = backtest(df, sig); rows[name]=m
        print(f"  {name:<28}{m['cal']:>+6.2f}{m['shp']:>+6.2f}{m['ann']:>+7.3f}{m['dd']:>+7.3f}"
              f"{m['h1_cal']:>+7.2f}{m['h2_cal']:>+7.2f}{m['exp']:>5.2f}{m['flips_yr']:>6.1f}")

    # ── FALSIFICATION: real trend components vs KNOWN-NOISE (inflow) vs PLACEBO (deterministic
    #    pseudo-random). If trend beat-rate ≈ noise/placebo beat-rate → "improvement" is overfit artifact.
    print("\n── Robustness + falsification: does combo beat retail-only ACROSS params? ──")
    # placebo: deterministic shuffle of close returns (no look-ahead structure to fwd ret)
    placebo = pd.Series(np.cos(np.arange(len(df)) * 1.7) * 1.3, index=df.index)  # structureless wiggle
    trend_better=noise_better=plac_better=total=0
    base_cals=[]; trend_cals=[]; noise_cals=[]; plac_cals=[]
    for nn, ww, t, wm, wc in product([10,15,20],[45,60,90],[0.75,1.0,1.5],[0.3,0.5,0.7],[0.3,0.5,0.7]):
        base = backtest(df, pos_from_score(zwin(df["sr_imbalance"],nn,ww), t))["cal"]
        trend = backtest(df, pos_from_score(make_score(df,nn,ww,wm,wc), t))["cal"]
        rz = zwin(df["sr_imbalance"],nn,ww)
        noise = backtest(df, pos_from_score(rz - wm*zwin(df["main_inflow"],nn,ww) - wc*placebo*0, t))["cal"]
        plac  = backtest(df, pos_from_score(rz - wm*placebo - wc*placebo, t))["cal"]
        base_cals.append(base); trend_cals.append(trend); noise_cals.append(noise); plac_cals.append(plac)
        trend_better+=int(trend>base); noise_better+=int(noise>base); plac_better+=int(plac>base); total+=1
    print(f"  configs={total}")
    print(f"  TREND combo (retail−mom−conc) beats retail-only: {trend_better}/{total} = {trend_better/total:.0%}  "
          f"median cal {np.median(base_cals):+.2f}→{np.median(trend_cals):+.2f}")
    print(f"  NOISE combo (retail−inflow)   beats retail-only: {noise_better}/{total} = {noise_better/total:.0%}  "
          f"median cal →{np.median(noise_cals):+.2f}   [对照: 已知噪声]")
    print(f"  PLACEBO (retail−随机wiggle)    beats retail-only: {plac_better}/{total} = {plac_better/total:.0%}  "
          f"median cal →{np.median(plac_cals):+.2f}   [对照: 纯安慰剂]")

    # monthly win-rate of best combo vs retail-only
    print("\n── Monthly excess win-rate (vs buyhold) ──")
    for name in ["retail_only (imbalance)","combo retail-mom-conc EW"]:
        m = backtest(df, variants[name], want_monthly=True); mo=m["monthly"]
        print(f"  {name:<28} 正月份 {(mo>0).sum()}/{len(mo)} = {(mo>0).mean():.0%}  累计超额={(np.prod(1+mo)-1)*100:+.1f}%")


if __name__ == "__main__":
    main()
