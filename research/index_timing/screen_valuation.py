"""估值因子族找绿灯：用 simworld 指数行情自带的 PE/PB，构造分位 contrarian + 估值×趋势因子，
对 6 个新能源子板块 validation。重点验证公用事业(绿电/电力)的估值锚是否是它们的绿灯。"""
import sys,json,urllib.request
sys.path.insert(0,"/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np,pandas as pd
from backtest.validation import bootstrap_sharpe_ci, walk_forward_analysis
URL="http://127.0.0.1:18078/mcp";H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
def call(m,p,sid=None):
    h=dict(H)
    if sid:h["Mcp-Session-Id"]=sid
    r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),headers=h),timeout=90)
    sid=r.headers.get("Mcp-Session-Id",sid);b=r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"):return json.loads(ln[5:].strip()),sid
    return json.loads(b),sid
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"val","version":"1"}})
NE6=[("光伏","931151.CSI"),("电网设备","931994.CSI"),("电池","931719.CSI"),
     ("新能车","399976.SZ"),("绿电","931897.CSI"),("电力","H30199.CSI")]
syms=[c for _,c in NE6]
res,_=call("tools/call",{"name":"market_index_quote","arguments":{"market":"cn","symbols":syms,"start_date":"2014-01-01","end_date":"2026-06-05","simulated_today":"2026-06-05","simulated_datetime":"2026-06-05 15:00:00"}},sid)
items={it.get("指数标识"):it for it in json.loads(res["result"]["content"][0]["text"]).get("items",[])}

def to_df(rec):
    df=pd.DataFrame(rec);df["date"]=pd.to_datetime(df["日期"]);df=df.set_index("date").sort_index()
    out=pd.DataFrame(index=df.index)
    out["close"]=pd.to_numeric(df["收盘"],errors="coerce")
    out["pb"]=pd.to_numeric(df.get("PB"),errors="coerce") if "PB" in df.columns else np.nan
    out["pe"]=pd.to_numeric(df.get("PETTM"),errors="coerce") if "PETTM" in df.columns else np.nan
    return out.dropna(subset=["close"])

def roll_pct(s,win=756,minp=252):
    # 当前值在过去 win 日里的分位（0=最低/最便宜，1=最高/最贵）
    return s.rolling(win,min_periods=minp).apply(lambda x:(x<=x[-1]).mean(),raw=True)

def factors(df):
    out={}
    pb,pe,c=df["pb"],df["pe"],df["close"]
    ma200=c.rolling(200).mean()
    up=(c>ma200)
    if pb.notna().sum()>300:
        pbp=roll_pct(pb)
        out["pb_contra"]=(1.1-pbp).clip(0,1)                       # PB分位越低仓位越高(连续)
        out["pb_2tier"]=pd.Series(np.where(pbp<0.5,1.0,0.3),index=df.index).where(pbp.notna())
        out["pb_3tier"]=pd.Series(np.select([pbp<0.33,pbp<0.66],[1.0,0.6],0.25),index=df.index).where(pbp.notna())
        out["pb_x_trend"]=pd.Series(np.where((pbp<0.6)&up,1.0,np.where(up,0.5,0.2)),index=df.index).where(pbp.notna()&ma200.notna())
        out["pb_or_trend"]=pd.Series(np.where((pbp<0.5)|up.values,1.0,0.3),index=df.index).where(pbp.notna()&ma200.notna())
    if pe.notna().sum()>300:
        pep=roll_pct(pe.where(pe>0))
        out["pe_contra"]=(1.1-pep).clip(0,1)
        out["earnyield_x_trend"]=pd.Series(np.where((pep<0.6)&up,1.0,np.where(up,0.5,0.2)),index=df.index).where(pep.notna()&ma200.notna())
    return out

def ev(df,sig,s,e):
    sig=sig.clip(0,1).fillna(0).reindex(df.index).fillna(method="ffill").fillna(0)
    ret=df["close"].pct_change().fillna(0);pos=sig.shift(1).fillna(0);cost=pos.diff().abs().fillna(0)*0.0005
    m=(df.index>=pd.Timestamp(s))&(df.index<=pd.Timestamp(e))
    sr=(pos*ret-cost).loc[m];eq=(1+sr).cumprod();bh=(1+ret.loc[m]).cumprod()
    def cal(e2):
        r=e2.pct_change().fillna(0);n=len(r);a=e2.iloc[-1]**(252/n)-1;d=(e2/e2.cummax()-1).min();return a/abs(d) if d<0 else 0,d
    fc,fd=cal(eq);bc,bd=cal(bh);return fc,fd,bc,bd,pos.loc[m].mean(),int((pos.loc[m].diff().abs()>0).sum())

IS_S,IS_E,OOS_S,OOS_E="2017-06-01","2021-12-31","2022-01-01","2026-06-05"
for nm,code in NE6:
    it=items.get(code)
    if not it or not it.get("行情记录"): print("\n#### %s 不可用"%nm);continue
    df=to_df(it["行情记录"]);F=factors(df)
    pbok=df["pb"].notna().sum();peok=df["pe"].notna().sum()
    print("\n#### %s (%s) PB有效=%d PE有效=%d"%(nm,code,pbok,peok))
    if not F: print("  无PB/PE数据，跳过");continue
    res_rows=[]
    for fn,sig in F.items():
        fc_i,_,bc_i,_,_,_=ev(df,sig,IS_S,IS_E)
        fc_o,fd_o,bc_o,bd_o,exp,flips=ev(df,sig,OOS_S,OOS_E)
        res_rows.append((fn,fc_i-bc_i,fc_o,bc_o,fc_o-bc_o,fd_o,exp,flips))
    res_rows.sort(key=lambda r:r[4],reverse=True)
    for fn,dci,fco,bco,dco,fdo,exp,flips in res_rows:
        if dco<=0 and dci<=0:
            print("  %-18s IS_dCal=%+.2f OOS_cal=%.2f(BH%.2f dCal%+.2f) ❌"%(fn,dci,fco,bco,dco));continue
        eq=None
        # 全周期 validation
        sig=F[fn];sg=sig.clip(0,1).fillna(method="ffill").fillna(0)
        ret=df["close"].pct_change().fillna(0);pos=sg.shift(1).fillna(0);cost=pos.diff().abs().fillna(0)*0.0005
        eq=(1+(pos*ret-cost)).cumprod()
        ci=bootstrap_sharpe_ci(eq,n_bootstrap=600,confidence=0.95)
        wf=walk_forward_analysis(eq,[],n_windows=5);shs=[x.get("sharpe") for x in (wf.get("windows") or wf.get("per_window") or [])];pos5=sum(1 for x in shs if x and x>0)
        cl=ci.get("ci_lower",0);pl=ci.get("prob_positive",0)*100
        verd="✅绿灯" if (dco>0 and dci>0 and cl>0 and pos5>=4) else ("🟢近绿" if (dco>0 and pl>=90 and pos5>=4) else "🟡黄灯")
        print("  %-18s IS_dCal=%+.2f OOS_cal=%.2f(BH%.2f dCal%+.2f) dd%.0f%% exp%.2f flips%d | CI[%.2f,%.2f]p+%.0f%% wf%d/5 %s"%(
            fn,dci,fco,bco,dco,fdo*100,exp,flips,cl,ci.get("ci_upper",0),pl,pos5,verd))
