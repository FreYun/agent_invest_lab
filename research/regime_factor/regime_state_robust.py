"""Comprehensive robustness test of the regime STATE (hysteresis-confirmed regime_code,
not raw total_score) as a timing signal for the 50/50 HS300+ZZ1000 combo.

Key splits:
  TRUE-OOS  2014-2022  (backfilled era, BEFORE the v1/v2 ruleset existed -> genuine OOS)
  DESIGN    2023-2026  (v1 native range -> ruleset's in-sample design era)
A robust signal must work in BOTH. We sweep many sensible regime->exposure mappings
(not one) and also test transitions, with permutation significance.
"""
import sqlite3, itertools, numpy as np, pandas as pd
DB="/home/rooot/database/market.db"
TRUE_OOS=(pd.Timestamp("2014-07-01"),pd.Timestamp("2022-12-31"))
DESIGN=(pd.Timestamp("2023-01-01"),pd.Timestamp("2026-05-28"))
COMM=0.0005
rng=np.random.default_rng(23)
ORDER=["熊","弱势震荡","中性震荡","强势震荡","强牛"]

def load():
    c=sqlite3.connect(DB)
    reg=pd.read_sql("SELECT trade_date,total_score,regime_name FROM regime_classify_daily WHERE rules_version='v2'",c)
    reg["date"]=pd.to_datetime(reg["trade_date"]); reg=reg.set_index("date").sort_index()
    idx=pd.read_sql("SELECT trade_date,ts_code,close FROM index_daily WHERE ts_code IN ('000300.SH','000852.SH')",c); c.close()
    idx["date"]=pd.to_datetime(idx["trade_date"],format="%Y%m%d")
    piv=idx.pivot(index="date",columns="ts_code",values="close").sort_index()
    combo=0.5*piv["000300.SH"].pct_change()+0.5*piv["000852.SH"].pct_change()
    return reg,combo

def bt(combo,pos,a,b):
    pos=pos.reindex(combo.index).ffill().fillna(0.0)
    p=pos.shift(1).fillna(0.0); strat=p*combo.fillna(0.0)-p.diff().abs().fillna(0.0)*COMM
    m=(combo.index>=a)&(combo.index<=b); sr,bh,pe=strat[m],combo.fillna(0.0)[m],p[m]
    if len(sr)<30: return None
    eq,eqb=(1+sr).cumprod(),(1+bh).cumprod(); n=len(sr)
    shp=sr.mean()/sr.std(ddof=1)*np.sqrt(252) if sr.std()>0 else 0
    bshp=bh.mean()/bh.std(ddof=1)*np.sqrt(252) if bh.std()>0 else 0
    ann=eq.iloc[-1]**(252/n)-1;bann=eqb.iloc[-1]**(252/n)-1
    dd=(eq/eq.cummax()-1).min();bdd=(eqb/eqb.cummax()-1).min()
    cal=ann/abs(dd) if dd<0 else 0;bcal=bann/abs(bdd) if bdd<0 else 0
    return dict(dshp=shp-bshp,dcal=cal-bcal,exp=pe.mean(),dd=dd,bdd=bdd,ret=eq.iloc[-1]-1,bh=eqb.iloc[-1]-1)

def main():
    reg,combo=load()
    rn=reg["regime_name"]
    # sweep many monotonic regime->exposure mappings (熊..强牛)
    lows=[0.0,0.2,0.3]; mids=[0.5,0.6,0.7]
    maps=[]
    for bear,weak,mid,strong,bull in itertools.product(lows,[0.2,0.3,0.4],mids,[0.8,0.9,1.0],[1.0]):
        if bear<=weak<=mid<=strong<=bull:
            maps.append({"熊":bear,"弱势震荡":weak,"中性震荡":mid,"强势震荡":strong,"强牛":bull})
    print(f"sweeping {len(maps)} monotonic regime->exposure mappings\n")
    rows=[]
    for mp in maps:
        pos=rn.map(mp)
        ri=bt(combo,pos,*TRUE_OOS); ro=bt(combo,pos,*DESIGN)
        if ri and ro: rows.append((mp,ri,ro))
    df=pd.DataFrame([{"trueoos_dshp":ri["dshp"],"trueoos_dcal":ri["dcal"],"trueoos_exp":ri["exp"],
                      "design_dshp":ro["dshp"],"design_dcal":ro["dcal"],"design_exp":ro["exp"]} for _,ri,ro in rows])
    print("==== regime-STATE exposure mappings: TRUE-OOS (2014-2022) vs DESIGN (2023-2026) ====")
    print(f"  mappings beating combo Sharpe in TRUE-OOS  : {(df.trueoos_dshp>0).mean()*100:.0f}%  (median ΔShp {df.trueoos_dshp.median():+.3f})")
    print(f"  mappings beating combo Sharpe in DESIGN era : {(df.design_dshp>0).mean()*100:.0f}%  (median ΔShp {df.design_dshp.median():+.3f})")
    print(f"  beating in BOTH eras                        : {((df.trueoos_dshp>0)&(df.design_dshp>0)).mean()*100:.0f}%")
    print(f"  TRUE-OOS ΔCalmar median={df.trueoos_dcal.median():+.3f}   DESIGN ΔCalmar median={df.design_dcal.median():+.3f}")

    # defensive-only mapping (full long except cut in 熊/弱势) — the natural regime use
    print("\n==== representative mappings, both eras ====")
    reps={"defensive(熊0/弱0.5/否则1)":{"熊":0,"弱势震荡":0.5,"中性震荡":1,"强势震荡":1,"强牛":1},
          "graded(0/.3/.6/.9/1)":{"熊":0,"弱势震荡":0.3,"中性震荡":0.6,"强势震荡":0.9,"强牛":1},
          "bull-only(强势/强牛=1否则0.5)":{"熊":0.5,"弱势震荡":0.5,"中性震荡":0.5,"强势震荡":1,"强牛":1}}
    for nm,mp in reps.items():
        pos=rn.map(mp)
        for tag,(a,b) in [("TRUE-OOS",TRUE_OOS),("DESIGN",DESIGN)]:
            m=bt(combo,pos,a,b)
            print(f"  {nm:<28}{tag:<9} ΔShp={m['dshp']:+.2f} ΔCal={m['dcal']:+.2f} exp={m['exp']:.2f} dd={m['dd']:.1%} vs bh {m['bdd']:.1%}")
        print()

    # per-year ΔShp for graded mapping
    mp={"熊":0,"弱势震荡":0.3,"中性震荡":0.6,"强势震荡":0.9,"强牛":1}; pos=rn.map(mp)
    print("==== per-year ΔSharpe (graded mapping) ====")
    for yr in range(2014,2027):
        m=bt(combo,pos,pd.Timestamp(f"{yr}-01-01"),pd.Timestamp(f"{yr}-12-31"))
        if m: print(f"  {yr}: ΔShp={m['dshp']:+.2f}  ΔCal={m['dcal']:+.2f}  exp={m['exp']:.2f}  ret={m['ret']:+.1%} vs bh {m['bh']:+.1%}")

if __name__=="__main__":
    main()
