"""Combine hs300_erp_vix_timing + zz1000_erp_vix_timing into ONE signal for the
50/50 HS300+ZZ1000 book. Compare: combo buyhold | two-separate (per-index) | combined-single.
IS 2018-2023 / OOS 2024-2026, vs the 50/50 combo. Honest: Sharpe/Calmar/exposure + per-year.
"""
from pathlib import Path
import numpy as np, pandas as pd
ETF=Path("/home/rooot/agent_invest_lab/research/sr_factor/data")
DATA=Path("/home/rooot/agent_invest_lab/research/timing_factors/data")
VIXP=Path("/home/rooot/agent_invest_lab/research/vix_factor/data/vix_50etf.csv")
COMM=0.0005
IS=(pd.Timestamp("2018-03-01"),pd.Timestamp("2023-12-31"))
OOS=(pd.Timestamp("2024-01-01"),pd.Timestamp("2026-05-26"))

def loadpx(code): return pd.read_csv(ETF/f"{code}.csv",parse_dates=[0],index_col=0).sort_index()["close"]
def loaderp(gz): return pd.read_csv(DATA/f"gzxjb_{gz}.csv",parse_dates=["date"],index_col="date").sort_index()["erp_pct3y"]/100.0
vix=pd.read_csv(VIXP,parse_dates=["date"],index_col="date").sort_index()["vix"]
vz=(vix-vix.rolling(252,min_periods=126).mean())/vix.rolling(252,min_periods=126).std()

def factor_pos(idx,pct,LO=0.35,HI=0.95,addk=0.4,trimk=0.35,H=20):
    pct=pct.reindex(idx,method="ffill"); vzA=vz.reindex(idx,method="ffill")
    base=(LO+(HI-LO)*pct).clip(0,1)
    panic=(vzA>1.5).astype(float).rolling(H,min_periods=1).max().fillna(0.0)
    complac=(vzA<-1.5).astype(float)
    return (base+addk*panic-trimk*complac).clip(0,1)

def stats(ret_series,a,b,bench=None):
    m=(ret_series.index>=a)&(ret_series.index<=b); sr=ret_series[m]
    if len(sr)<30: return None
    eq=(1+sr).cumprod(); n=len(sr)
    shp=sr.mean()/sr.std(ddof=1)*np.sqrt(252) if sr.std()>0 else 0
    ann=eq.iloc[-1]**(252/n)-1; dd=(eq/eq.cummax()-1).min(); cal=ann/abs(dd) if dd<0 else 0
    d=dict(ret=eq.iloc[-1]-1,shp=shp,cal=cal,dd=dd)
    if bench is not None:
        b2=bench[m]; eqb=(1+b2).cumprod(); bsh=b2.mean()/b2.std(ddof=1)*np.sqrt(252) if b2.std()>0 else 0
        bann=eqb.iloc[-1]**(252/n)-1; bdd=(eqb/eqb.cummax()-1).min(); bcal=bann/abs(bdd) if bdd<0 else 0
        d.update(dshp=shp-bsh,dcal=cal-bcal)
    return d

# build returns
c300=loalpx if False else loadpx("510300.SH"); c1000=loadpx("512100.SH")
idx=c300.index.intersection(c1000.index)
r300=c300.pct_change().reindex(idx).fillna(0.0); r1000=c1000.pct_change().reindex(idx).fillna(0.0)
combo=0.5*r300+0.5*r1000
pos300=factor_pos(idx,loaderp("000300")); pos1000=factor_pos(idx,loaderp("000852"))
posavg=0.5*(pos300+pos1000)

def apply(pos,ret):
    p=pos.shift(1).fillna(0.0); return p*ret-p.diff().abs().fillna(0.0)*COMM
def apply2(pa,ra,pb,rb):
    Pa,Pb=pa.shift(1).fillna(0.0),pb.shift(1).fillna(0.0)
    return 0.5*(Pa*ra)+0.5*(Pb*rb)-0.5*(Pa.diff().abs().fillna(0.0)+Pb.diff().abs().fillna(0.0))*COMM

bench=combo.copy()
ret_two=apply2(pos300,r300,pos1000,r1000)        # two separate factors per index
ret_comb=apply(posavg,combo)                      # combined single factor on combo
ret_bh=combo

print("strategy comparison vs 50/50 HS300+ZZ1000 combo (T+1, 5bp)\n")
for tag,a,b in [("IS 2018-2023",*[IS[0],IS[1]]),("OOS 2024-2026",*[OOS[0],OOS[1]])]:
    print(f"== {tag} ==")
    print(f"  {'strategy':<22}{'ret':>8}{'shp':>7}{'cal':>6}{'dd':>8}{'Δshp':>7}{'Δcal':>7}{'avgexp':>8}")
    bh=stats(ret_bh,a,b)
    print(f"  {'50/50 combo (bench)':<22}{bh['ret']:>+8.2f}{bh['shp']:>7.2f}{bh['cal']:>6.2f}{bh['dd']:>8.1%}{'—':>7}{'—':>7}{1.00:>8.2f}")
    for nm,rs,pos in [("two-separate",ret_two,posavg),("combined-single",ret_comb,posavg)]:
        s=stats(rs,a,b,bench)
        ex=pos.shift(1).fillna(0.0)[(idx>=a)&(idx<=b)].mean()
        print(f"  {nm:<22}{s['ret']:>+8.2f}{s['shp']:>7.2f}{s['cal']:>6.2f}{s['dd']:>8.1%}{s['dshp']:>+7.2f}{s['dcal']:>+7.2f}{ex:>8.2f}")
    print()

print("== per-year (combined-single vs combo) ==")
for yr in range(2018,2027):
    a,b=pd.Timestamp(f"{yr}-01-01"),pd.Timestamp(f"{yr}-12-31")
    s=stats(ret_comb,a,b,bench); bh=stats(ret_bh,a,b)
    if s and bh: print(f"  {yr}: combined ret {s['ret']:+.1%} vs combo {bh['ret']:+.1%}  Δshp {s['dshp']:+.2f}  Δcal {s['dcal']:+.2f}  dd {s['dd']:.0%} vs {bh['dd']:.0%}")

# ===== extension: combined robustness + apply as market-level signal to 双创50 =====
print("\n\n######## EXTENSION ########")
import itertools
def combo_pos_grid(idx,LO,HI,addk,trimk,H):
    p3=factor_pos(idx,loaderp("000300"),LO,HI,addk,trimk,H); p1=factor_pos(idx,loaderp("000852"),LO,HI,addk,trimk,H)
    return 0.5*(p3+p1)
# robustness of combined on 50/50 combo OOS
beats=0; tot=0; ds=[]
for LO,HI,addk,trimk,H in itertools.product([0.3,0.35,0.4],[0.85,0.95,1.0],[0.3,0.4,0.5],[0.25,0.35,0.45],[10,15,20]):
    pos=combo_pos_grid(idx,LO,HI,addk,trimk,H); rs=apply(pos,combo)
    s=stats(rs,OOS[0],OOS[1],bench)
    if s: tot+=1; ds.append(s['dshp']); beats+= (s['dshp']>0)
print(f"combined-single robustness on 50/50 combo, OOS: {beats}/{tot} configs beat combo Sharpe ({100*beats/tot:.0f}%), median Δshp {np.median(ds):+.3f}")

# apply combined market-level signal to 双创50 (588800, ETF since 2023 -> OOS only)
try:
    c588=loadpx("588800.SH"); idx2=c588.index.intersection(idx)
    r588=c588.pct_change().reindex(idx2).fillna(0.0)
    pos_m=combo_pos_grid(idx,0.35,0.95,0.4,0.35,20).reindex(idx2).ffill()
    rs=apply(pos_m,r588); a,b=pd.Timestamp("2023-09-01"),OOS[1]
    s=stats(rs,a,b,r588)
    ex=pos_m.shift(1).fillna(0.0)[(idx2>=a)&(idx2<=b)].mean()
    print(f"combined as MARKET-level signal on 双创50 (2023-09..2026-05): ret {s['ret']:+.1%} Δshp {s['dshp']:+.2f} Δcal {s['dcal']:+.2f} exp {ex:.2f}")
except Exception as e:
    print("双创50 test err:",e)
