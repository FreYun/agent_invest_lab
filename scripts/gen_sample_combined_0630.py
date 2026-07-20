# 生成 docs/sample-combined-0630.csv —— 复刻 docs/sample_combined.csv 的 26 列「combined」格式
# (order 交收流水 + nav 每日组合快照 两类记录合一)
# 数据源:
#   - 单基金择时 7 bot: 28080 页面同源库 .openclaw/data/fund.db   (portfolio_name = botX)
#   - 多基金配置 bot101 (run_id=oos-bot101-daily): 本项目 data/fund.db
#       * 其中 3 只场内 ETF 换成同指数场外 C 联接, 净值/份额/费用相应重算 (净值源 fund_nav)
import sqlite3, csv, json
from collections import defaultdict

OPENCLAW_DB = "/home/rooot/.openclaw/data/fund.db"
LAB_DB      = "/home/rooot/agent_invest_lab/data/fund.db"
OUT         = "/home/rooot/agent_invest_lab/docs/sample-combined-0630.csv"
SINGLE_BOTS = ["bot2", "bot7", "bot4", "bot11", "bot6", "bot1", "bot5"]
RID, CAP    = "oos-bot101-daily", 1_000_000.0
CUTOFF      = "2026-06-29"  # 0630 快照窗口: DB 持续增长, 重跑时截断到首版生成时的最后交易日
# record_type=position 行(起始日基金级持仓明细)列复用:
#   confirm_vol=份额 confirm_nav=当日净值 apply_amount=成本 market_value=市值
#   排在该组合起始日 nav 行之前; bot101 期初全现金无 position 行

# customer_no 映射(来自导入侧账户表截图 docs/5f35a150-fe55-4c65-ac76-03a5d6f984e6.png, 按 bot 编号对应)
CUSTOMER_NO = {
    "bot1":   "74d61a1b1e0b4f769be393c5002d7ae9",
    "bot2":   "bcb301b7d2e64a31806dd4d7683c0304",
    "bot4":   "32da071a845444fc9caf9a540345a66e",
    "bot5":   "4a18aff95df04119a042235fa0c43345",
    "bot6":   "5ffa0fcb465e4ab1b316f2eaa6e36b91",
    "bot7":   "4b8d48a372384d4aa9d559d96a32fd99",
    "bot11":  "19f5c89e46f94539b40a343df5e38733",
    "bot101": "462e5e5bf5a2484e81602011b730ecca",
}
# 场内 ETF -> 同指数场外 C 联接
REP = {"159546": "020227", "159732": "018301", "159773": "021683"}

HEADER = ["record_type","customer_no","sub_account_no","portfolio_id","portfolio_name",
          "biz_date","confirm_date","fund_code","fund_name","C_BUSINTYPE","busin_type_desc",
          "apply_amount","apply_vol","confirm_nav","confirm_vol","confirm_amount","fee",
          "order_status","net_value","total_asset","market_value","cash","cash_weight",
          "noncash_ratio","cum_return_pct","max_drawdown_pct"]

def acct(bot): return "SIMACCT" + bot.replace("bot","").zfill(4)
def s(v):
    if v is None: return ""
    if isinstance(v,str): return v
    if isinstance(v,float): return repr(v)
    return str(v)
def name_map_of(conn):
    return {str(r["fund_code"]).zfill(6): r["fund_name"]
            for r in conn.execute("SELECT fund_code,fund_name FROM fund_info") if r["fund_name"]}
def blank(): return {k:"" for k in HEADER}

# ---------- 单基金 bot 区块(order + nav 直取, 以持仓快照为真相源对账修正) ----------
# 源库已知三类数据缺陷(以 fund_bot_position_snapshots 每日份额为真相源逐日对账发现):
#   1) 期初建仓(04-24 按当日 nav 直接置入持仓)无订单流水 -> 导出期初持仓明细文件 OUT_INIT
#   2) 05-08 central_allocation_rebalance 只写了 fund_bot_actions 没写 orders -> 按快照份额差合成补单
#   3) 部分订单(05-11 下的一批) confirm_nav 记录为过期净值, 份额随之记错(现金字段是对的)
#      -> 按快照当日真实 nav 重算份额: 卖出 vol=(净额+fee)/nav, 申购 shares=(金额-fee)/nav
def build_single(conn, bot, nmap, cal):
    A=acct(bot); base=dict(customer_no=CUSTOMER_NO[bot],sub_account_no=A,portfolio_id=f"{A}_live",portfolio_name=bot)
    orders=[dict(o) for o in conn.execute("SELECT * FROM fund_bot_orders WHERE bot_id=? AND order_date<=? ORDER BY order_id",(bot,CUTOFF))]
    # --- 真相源: 每日持仓快照(份额/净值) + 每日现金 ---
    days=[r[0] for r in conn.execute("SELECT trade_date FROM fund_bot_daily_snapshots WHERE bot_id=? AND trade_date<=? ORDER BY trade_date",(bot,CUTOFF))]
    cash={r[0]:r[1] for r in conn.execute("SELECT trade_date,cash FROM fund_bot_daily_snapshots WHERE bot_id=? AND trade_date<=?",(bot,CUTOFF))}
    snap={}; snav={}
    for r in conn.execute("SELECT trade_date,fund_code,shares,nav FROM fund_bot_position_snapshots WHERE bot_id=? AND trade_date<=?",(bot,CUTOFF)):
        snap.setdefault(r["trade_date"],{})[r["fund_code"]]=r["shares"]
        snav.setdefault(r["trade_date"],{})[r["fund_code"]]=r["nav"]
    def nav_at(fc,dt):  # 快照无行(如清仓日)时从 fund_nav 取
        if snav.get(dt,{}).get(fc): return snav[dt][fc]
        r=conn.execute("SELECT nav FROM fund_nav WHERE fund_code=? AND nav_date=? AND nav IS NOT NULL",(fc,dt)).fetchone()
        if not r: raise RuntimeError(f"{bot} {fc}@{dt} 无净值可用")
        return r[0]
    init=dict(snap.get(days[0],{}))  # 期初持仓(单独出明细文件, 不合成订单)

    # --- 逐日重放对账: 修正过期净值订单 + 合成缺失调仓单 ---
    confirmed=[o for o in orders if o["status"]=="confirmed" and o["confirm_date"] and o["confirm_date"]<=CUTOFF]
    by_confirm={}
    for o in confirmed: by_confirm.setdefault(o["confirm_date"],[]).append(o)
    synths=[]; fixed=0
    expected=dict(init)
    prev=days[0]
    for dt in days[1:]:
        # 累计 (prev, dt] 区间内确认的订单(覆盖 bot5 05-14 整日快照缺失的情况)
        span=[o for cd,os_ in by_confirm.items() if prev<cd<=dt for o in os_]
        for o in span:
            fc=str(o["fund_code"])
            # 缺陷3: confirm_nav 偏离当日真实净值 >0.5% -> 按真实 nav 修正份额(现金字段不动)
            if o["confirm_date"]==dt:
                real=nav_at(fc,dt)
                if o["confirm_nav"] and abs(o["confirm_nav"]-real)/real>0.005:
                    if o["order_type"]=="buy":
                        o["confirmed_shares"]=round((o["order_amount"]-(o["fee"] or 0))/real,6)
                    else:
                        o["order_amount"]=round((o["confirmed_amount"]+(o["fee"] or 0))/real,6)
                    o["confirm_nav"]=real; fixed+=1
            fc=str(o["fund_code"])
            expected[fc]=expected.get(fc,0.0)+(o["confirmed_shares"] if o["order_type"]=="buy" else -o["order_amount"])
        if dt not in snap and dt not in cash: prev=dt; continue
        # 缺陷2: 快照份额与订单重放不符 -> 合成补单(当日 biz=confirm, nav=当日真实净值)
        day_synth=[]
        for fc in set(expected)|set(snap.get(dt,{})):
            actual=snap.get(dt,{}).get(fc,0.0); diff=actual-expected.get(fc,0.0)
            if abs(diff)>0.02:
                day_synth.append(dict(fund_code=fc,delta=diff,nav=nav_at(fc,dt)))
                expected[fc]=actual
        if day_synth:
            # 现金残差(引擎实际多扣的部分=手续费)计入当日第一笔合成卖出的 fee
            flow=sum(o["confirmed_amount"] if o["order_type"]=="sell" else -o["order_amount"] for o in span)
            flow+=sum(-g["delta"]*g["nav"] for g in day_synth)
            resid=(cash[dt]-cash[prev])-flow
            sells=[g for g in day_synth if g["delta"]<0]
            if abs(resid)>0.02:
                if not (sells and -resid>0): raise RuntimeError(f"{bot} {dt} 现金残差 {resid:.2f} 无法归入合成卖出 fee")
                sells[0]["fee"]=round(-resid,2)
            synths.append((dt,day_synth))
        prev=dt

    recs=[]
    for o in orders:
        d=blank(); d.update(base); d["record_type"]="order"; d["biz_date"]=o["order_date"]
        d["confirm_date"]=s(o["confirm_date"]); d["fund_code"]=str(o["fund_code"])
        d["fund_name"]=o["fund_name"] or nmap.get(str(o["fund_code"]).zfill(6),"") or ""
        d["fee"]=s(o["fee"]); d["order_status"]=o["status"] or ""; d["confirm_nav"]=s(o["confirm_nav"])
        if o["order_type"]=="buy":
            d["C_BUSINTYPE"]="22"; d["busin_type_desc"]="申购"
            d["apply_amount"]=s(o["order_amount"]); d["confirm_vol"]=s(o["confirmed_shares"])
        else:
            d["C_BUSINTYPE"]="24"; d["busin_type_desc"]="赎回"
            # 赎回单口径: order_amount=赎回份额(confirmed_shares 恒为空), confirmed_amount=扣费后到账净额
            # 满足 confirm_vol * confirm_nav - fee ~= confirm_amount; 禁止用净额/nav 反推份额(会把 fee 吃掉)
            vol=o["order_amount"]
            d["apply_vol"]=s(vol); d["confirm_vol"]=s(vol); d["confirm_amount"]=s(o["confirmed_amount"])
        recs.append((o["order_date"],0,o["order_id"],d))
    # 合成补单行(缺陷2), order_id 加大偏移保证排序在当日真实订单之后
    sid=9_000_000
    for dt,day_synth in synths:
        for g in day_synth:
            sid+=1; vol=round(abs(g["delta"]),6); fee=g.get("fee",0.0)
            d=blank(); d.update(base); d["record_type"]="order"; d["biz_date"]=dt; d["confirm_date"]=dt
            d["fund_code"]=g["fund_code"]; d["fund_name"]=nmap.get(g["fund_code"].zfill(6),"") or ""
            d["confirm_nav"]=s(g["nav"]); d["fee"]=s(fee); d["order_status"]="confirmed"; d["confirm_vol"]=s(vol)
            if g["delta"]>0:
                d["C_BUSINTYPE"]="22"; d["busin_type_desc"]="申购"; d["apply_amount"]=s(round(vol*g["nav"]+fee,2))
            else:
                d["C_BUSINTYPE"]="24"; d["busin_type_desc"]="赎回"; d["apply_vol"]=s(vol)
                d["confirm_amount"]=s(round(vol*g["nav"]-fee,2))
            recs.append((dt,0,sid,d))
    n_synth=sum(len(x[1]) for x in synths)
    # 期初持仓明细 -> record_type=position 行(排序 kind=-1 保证在起始日 nav 行之前)
    n_init=0
    for idx,(fc,shv) in enumerate(sorted(init.items())):
        n_init+=1; nav0=snav[days[0]][fc]; mv=round(shv*nav0,2)
        d=blank(); d.update(base); d["record_type"]="position"; d["biz_date"]=days[0]
        d["fund_code"]=fc; d["fund_name"]=nmap.get(fc.zfill(6),"") or ""
        d["confirm_vol"]=s(shv); d["confirm_nav"]=s(nav0); d["apply_amount"]=s(mv); d["market_value"]=s(mv)
        recs.append((days[0],-1,idx,d))
    # --- 缺陷4: 源库整日快照缺失(如 bot5 2026-05-14) -> 用已对账的份额/现金重放 + 当日净值合成 nav 行 ---
    own=set(days); n_navfill=0
    for md in (d0 for d0 in cal if days[0]<d0<days[-1] and d0 not in own):
        sh=dict(init)
        for o in confirmed:
            if o["confirm_date"]<=md:
                fc=str(o["fund_code"]); sh[fc]=sh.get(fc,0.0)+(o["confirmed_shares"] if o["order_type"]=="buy" else -o["order_amount"])
        for dt,gs in synths:
            if dt<=md:
                for g in gs: sh[g["fund_code"]]=sh.get(g["fund_code"],0.0)+g["delta"]
        prev_own=max(d0 for d0 in days if d0<md)
        c0=cash[prev_own]
        for o in confirmed:
            if prev_own<o["confirm_date"]<=md:
                c0+=o["confirmed_amount"] if o["order_type"]=="sell" else -o["order_amount"]
        for dt,gs in synths:
            if prev_own<dt<=md:
                for g in gs: c0+=-g["delta"]*g["nav"]-g.get("fee",0.0)
        mkt=sum(v*nav_at(fc,md) for fc,v in sh.items() if v>0.02)
        tot=c0+mkt
        prow=conn.execute("SELECT initial_capital,max_drawdown_pct FROM fund_bot_daily_snapshots WHERE bot_id=? AND trade_date=?",(bot,prev_own)).fetchone()
        nv=tot/prow["initial_capital"]
        peak=max([x[0] for x in conn.execute("SELECT net_value FROM fund_bot_daily_snapshots WHERE bot_id=? AND trade_date<?",(bot,md))]+[nv])
        mdd=min(prow["max_drawdown_pct"],(nv/peak-1)*100)
        d=blank(); d.update(base); d["record_type"]="nav"; d["biz_date"]=md
        d["net_value"]=s(round(nv,6)); d["total_asset"]=s(round(tot,2)); d["market_value"]=s(round(mkt,2))
        d["cash"]=s(round(c0,2)); d["cash_weight"]=s(round(c0/tot,6)); d["noncash_ratio"]=s(round(mkt/tot,6))
        d["cum_return_pct"]=s(round((nv-1)*100,4)); d["max_drawdown_pct"]=s(round(mdd,4))
        recs.append((md,1,0,d)); n_navfill+=1
    seq=0
    for n in conn.execute("SELECT * FROM fund_bot_daily_snapshots WHERE bot_id=? AND trade_date<=? ORDER BY trade_date",(bot,CUTOFF)):
        seq+=1; d=blank(); d.update(base); d["record_type"]="nav"; d["biz_date"]=n["trade_date"]
        nv=n["net_value"]; tv=n["total_value"]; cash=n["cash"]; iv=n["invested_value"]; cw=n["cash_weight"]
        d["net_value"]=s(nv); d["total_asset"]=s(round(tv,2) if tv is not None else None)
        d["market_value"]=s(round(iv,2) if iv is not None else None); d["cash"]=s(round(cash,2) if cash is not None else None)
        d["cash_weight"]=s(round(cw,6) if cw is not None else None); d["noncash_ratio"]=s(round(1-cw,6) if cw is not None else None)
        d["cum_return_pct"]=s(round((nv-1)*100,4) if nv is not None else None)
        d["max_drawdown_pct"]=s(round(n["max_drawdown_pct"],4) if n["max_drawdown_pct"] is not None else None)
        recs.append((n["trade_date"],1,seq,d))
    recs.sort(key=lambda x:(x[0],x[1],x[2]))
    return [d for *_ ,d in recs], n_init, fixed, n_synth, n_navfill

# ---------- bot101 区块(场内ETF换场外C, 净值重算) ----------
def build_bot101(conn, nmap):
    A=acct("bot101"); base=dict(customer_no=CUSTOMER_NO["bot101"],sub_account_no=A,
        portfolio_id=f"{A}_{RID}",portfolio_name="bot101_多基金配置")
    orders=conn.execute("SELECT * FROM fund_bot_orders WHERE bot_id='bot101' AND order_run_id=? AND order_date<=? ORDER BY order_id",(RID,CUTOFF)).fetchall()
    snaps=conn.execute("SELECT trade_date,invested_value,total_value,holdings_json FROM oos_bot_daily_snapshots WHERE bot_id='bot101' AND live_run_id=? AND trade_date<=? ORDER BY trade_date",(RID,CUTOFF)).fetchall()
    # C 份额净值序列
    nav={}
    for code in set(REP.values()):
        nav[code]={r["nav_date"]:r["nav"] for r in conn.execute("SELECT nav_date,nav FROM fund_nav WHERE fund_code=? AND nav IS NOT NULL",(code,))}
    # 替换基金: 06-11 各买 100000, C 申购费0 -> shares = 100000 / Cnav(订单日)
    od_of={str(o["fund_code"]):o["order_date"] for o in orders if str(o["fund_code"]) in REP and o["order_type"]=="buy"}
    amt_of={str(o["fund_code"]):o["order_amount"] for o in orders if str(o["fund_code"]) in REP and o["order_type"]=="buy"}
    newshares={etf:amt_of[etf]/nav[REP[etf]][od_of[etf]] for etf in REP if etf in od_of}

    recs=[]
    # --- order 行 ---
    for o in orders:
        code=str(o["fund_code"]); d=blank(); d.update(base); d["record_type"]="order"
        d["biz_date"]=o["order_date"]; d["confirm_date"]=s(o["confirm_date"])
        d["order_status"]=o["status"] or ""
        if o["order_type"]!="buy":
            # 赎回单口径同 build_single: order_amount=份额, confirmed_amount=扣费后净额
            if code in REP:  # 替换基金的赎回需重算份额/净额, 当前窗口内不存在, 出现时再实现
                raise NotImplementedError(f"REP 基金 {code} 出现赎回单, 需补份额/净额重算逻辑")
            d["C_BUSINTYPE"]="24"; d["busin_type_desc"]="赎回"
            d["fund_code"]=code; d["fund_name"]=o["fund_name"] or nmap.get(code.zfill(6),"") or ""
            d["confirm_nav"]=s(o["confirm_nav"]); d["fee"]=s(o["fee"])
            d["apply_vol"]=s(o["order_amount"]); d["confirm_vol"]=s(o["order_amount"])
            d["confirm_amount"]=s(o["confirmed_amount"])
            recs.append((o["order_date"],0,o["order_id"],d)); continue
        d["C_BUSINTYPE"]="22"; d["busin_type_desc"]="申购"
        d["apply_amount"]=s(o["order_amount"])
        if code in REP:
            Cc=REP[code]; d["fund_code"]=Cc; d["fund_name"]=nmap.get(Cc.zfill(6),"")
            d["confirm_nav"]=s(nav[Cc][o["order_date"]]); d["confirm_vol"]=s(newshares[code]); d["fee"]="0.0"
        else:
            d["fund_code"]=code; d["fund_name"]=o["fund_name"] or nmap.get(code.zfill(6),"") or ""
            d["confirm_nav"]=s(o["confirm_nav"]); d["confirm_vol"]=s(o["confirmed_shares"]); d["fee"]=s(o["fee"])
        recs.append((o["order_date"],0,o["order_id"],d))
    # --- nav 行(替换基金市值重算, 复用原始 cash 与未换基金市值) ---
    nv_series=[]
    for sp in snaps:
        d0=sp["trade_date"]; hold=json.loads(sp["holdings_json"])
        held_rep=[h["fund_code"] for h in hold if h["fund_code"] in REP]
        orig_rep_mv=sum(h["market_value"] for h in hold if h["fund_code"] in REP)
        new_rep_mv=sum(newshares[etf]*nav[REP[etf]][d0] for etf in held_rep)
        new_market=sp["invested_value"]-orig_rep_mv+new_rep_mv
        new_total =sp["total_value"]-orig_rep_mv+new_rep_mv
        nv_series.append((d0,new_total,new_market))
    peak=1.0; seq=0
    for d0,tot,mkt in nv_series:
        seq+=1; nv=tot/CAP; peak=max(peak,nv); mdd=(nv/peak-1)*100
        cash=tot-mkt
        d=blank(); d.update(base); d["record_type"]="nav"; d["biz_date"]=d0
        d["net_value"]=s(round(nv,6)); d["total_asset"]=s(round(tot,2)); d["market_value"]=s(round(mkt,2))
        d["cash"]=s(round(cash,2)); d["cash_weight"]=s(round(cash/tot,6)); d["noncash_ratio"]=s(round(mkt/tot,6))
        d["cum_return_pct"]=s(round((nv-1)*100,4)); d["max_drawdown_pct"]=s(round(mdd,4))
        recs.append((d0,1,seq,d))
    recs.sort(key=lambda x:(x[0],x[1],x[2]))
    return [d for *_ ,d in recs]

rows_out=[]; stats=[]
oc=sqlite3.connect(f"file:{OPENCLAW_DB}?mode=ro",uri=True); oc.row_factory=sqlite3.Row
nmap_oc=name_map_of(oc)
# 全体 bot 快照日期并集 = 交易日历(用于探测某 bot 整日快照缺失)
CAL=[r[0] for r in oc.execute("SELECT DISTINCT trade_date FROM fund_bot_daily_snapshots WHERE bot_id IN (%s) AND trade_date<=? ORDER BY trade_date"%",".join("?"*len(SINGLE_BOTS)),(*SINGLE_BOTS,CUTOFF))]
for bot in SINGLE_BOTS:
    blk,n_init,fixed,n_synth,n_navfill=build_single(oc,bot,nmap_oc,CAL); rows_out+=blk
    stats.append((bot,sum(r["record_type"]=="order" for r in blk),sum(r["record_type"]=="nav" for r in blk),n_init,fixed,n_synth,n_navfill))
oc.close()

lab=sqlite3.connect(f"file:{LAB_DB}?mode=ro",uri=True); lab.row_factory=sqlite3.Row
blk=build_bot101(lab,name_map_of(lab)); rows_out+=blk
stats.append(("bot101_多基金配置("+RID+")",sum(r["record_type"]=="order" for r in blk),sum(r["record_type"]=="nav" for r in blk),0,0,0,0))
lab.close()
# bot101 期初=全现金 CAP, 无持仓行(建仓流水完整在 order 行中)

with open(OUT,"w",newline="",encoding="utf-8-sig") as f:
    w=csv.writer(f,quoting=csv.QUOTE_MINIMAL); w.writerow(HEADER)
    for d in rows_out: w.writerow([d[k] for k in HEADER])

print("OUT:",OUT,"total_rows:",len(rows_out))
for b,o,n,ini,fx,sy,nf in stats: print(f"  {b}: position={ini} order={o} nav={n} 修正过期净值单={fx} 合成补单={sy} 补缺日nav行={nf}")
