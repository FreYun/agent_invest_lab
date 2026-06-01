"""Backtest total_score / regime labels as a timing overlay on the 50/50 HS300+ZZ1000
combo. A regime classifier's value is often in drawdown avoidance (out of 熊/弱势),
not score IC. Test 3 uses, IS(2014-2022) vs OOS(2023-2026), vs the 50/50 combo buyhold.
"""
import sqlite3, numpy as np, pandas as pd
DB="/home/rooot/database/market.db"
IS_A,IS_B=pd.Timestamp("2014-07-01"),pd.Timestamp("2022-12-31")
OOS_A,OOS_B=pd.Timestamp("2023-01-01"),pd.Timestamp("2026-05-28")
COMM=0.0005

def load():
    c=sqlite3.connect(DB)
    reg=pd.read_sql("SELECT trade_date,total_score,regime_name FROM regime_classify_daily WHERE rules_version='v2'",c)
    reg["date"]=pd.to_datetime(reg["trade_date"]); reg=reg.set_index("date").sort_index()
    idx=pd.read_sql("SELECT trade_date,ts_code,close FROM index_daily WHERE ts_code IN ('000300.SH','000852.SH')",c); c.close()
    idx["date"]=pd.to_datetime(idx["trade_date"],format="%Y%m%d")
    piv=idx.pivot(index="date",columns="ts_code",values="close").sort_index()
    combo=0.5*piv["000300.SH"].pct_change()+0.5*piv["000852.SH"].pct_change()
    return reg,combo

def metr(combo,pos,a,b):
    pos=pos.reindex(combo.index).ffill().fillna(0.0)
    p=pos.shift(1).fillna(0.0); strat=p*combo.fillna(0.0)-p.diff().abs().fillna(0.0)*COMM
    m=(combo.index>=a)&(combo.index<=b); sr,bh,pe=strat[m],combo.fillna(0.0)[m],p[m]
    if len(sr)<30: return None
    eq,eqb=(1+sr).cumprod(),(1+bh).cumprod(); n=len(sr)
    shp=sr.mean()/sr.std(ddof=1)*np.sqrt(252) if sr.std()>0 else 0
    bshp=bh.mean()/bh.std(ddof=1)*np.sqrt(252) if bh.std()>0 else 0
    ann=eq.iloc[-1]**(252/n)-1; bann=eqb.iloc[-1]**(252/n)-1
    dd=(eq/eq.cummax()-1).min(); bdd=(eqb/eqb.cummax()-1).min()
    cal=ann/abs(dd) if dd<0 else 0; bcal=bann/abs(bdd) if bdd<0 else 0
    return dict(ret=eq.iloc[-1]-1,bh=eqb.iloc[-1]-1,shp=shp,bshp=bshp,dshp=shp-bshp,
                cal=cal,bcal=bcal,dcal=cal-bcal,dd=dd,bdd=bdd,exp=pe.mean())

def main():
    reg,combo=load()
    score=reg["total_score"].astype(float)
    rname=reg["regime_name"]
    # 1) continuous score tilt: pos=clip((score+3)/9,0,1)  (score -9..12 -> ~0..1, centered)
    tilt=((score+3)/9).clip(0,1)
    # 2) regime-label exposure
    rmap={"熊":0.0,"弱势震荡":0.3,"中性震荡":0.6,"强势震荡":0.9,"强牛":1.0}
    rexp=rname.map(rmap)
    # 3) score threshold long/flat (>0 in, <=0 flat) and (>=0)
    thr0=(score>0).astype(float)
    strats={"50/50 combo (bench)":pd.Series(1.0,index=combo.index),
            "score_tilt":tilt,"regime_label_exp":rexp,"score>0 long/flat":thr0}
    for split,a,b in [("IS 2014-2022",IS_A,IS_B),("OOS 2023-2026",OOS_A,OOS_B)]:
        print(f"\n========== {split} ==========")
        print(f"  {'strategy':<22}{'ret':>9}{'bh':>9}{'shp':>7}{'bshp':>7}{'Δshp':>7}{'cal':>6}{'Δcal':>7}{'dd':>8}{'bdd':>8}{'exp':>6}")
        for s,pos in strats.items():
            m=metr(combo,pos,a,b)
            if m: print(f"  {s:<22}{m['ret']:>+9.2f}{m['bh']:>+9.2f}{m['shp']:>7.2f}{m['bshp']:>7.2f}{m['dshp']:>+7.2f}{m['cal']:>6.2f}{m['dcal']:>+7.2f}{m['dd']:>8.1%}{m['bdd']:>8.1%}{m['exp']:>6.2f}")

if __name__=="__main__":
    main()
