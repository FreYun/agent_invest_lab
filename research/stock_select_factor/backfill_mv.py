"""回填真实历史市值 (akshare stock_value_em, 2018+) → 研究目录独立DB.
可断点续传(已完成code跳过), 流式写盘(每股flush), 串行节流(守内存铁律).
用法: /usr/bin/python3.12 backfill_mv.py
"""
import sqlite3, sqlite3 as _s, time, sys
import akshare as ak

MARKET="/home/rooot/database/market.db"
OUT="/home/rooot/agent_invest_lab/research/stock_select_factor/data/hist_mv.db"
SLEEP=0.25

def main():
    # 目标 code 列表: daily 里出现过的全部 ts_code
    m=sqlite3.connect(MARKET); codes=[r[0] for r in m.execute("SELECT DISTINCT ts_code FROM daily").fetchall()]; m.close()
    o=sqlite3.connect(OUT)
    o.execute("CREATE TABLE IF NOT EXISTS hist_mv(trade_date TEXT, ts_code TEXT, total_mv REAL, circ_mv REAL, PRIMARY KEY(trade_date,ts_code))")
    o.execute("CREATE TABLE IF NOT EXISTS done(ts_code TEXT PRIMARY KEY, n INTEGER, ok INTEGER)")
    o.commit()
    done=set(r[0] for r in o.execute("SELECT ts_code FROM done").fetchall())
    todo=[c for c in codes if c not in done]
    print(f"总 {len(codes)} 只, 已完成 {len(done)}, 待办 {len(todo)}", flush=True)
    for i,ts in enumerate(todo):
        sym=ts.split(".")[0]
        try:
            df=ak.stock_value_em(symbol=sym)
            if df is None or len(df)==0:
                o.execute("INSERT OR REPLACE INTO done VALUES(?,?,?)",(ts,0,0)); o.commit(); continue
            rows=[(str(r["数据日期"]).replace("-",""), ts, float(r["总市值"]) if r["总市值"]==r["总市值"] else None,
                   float(r["流通市值"]) if r["流通市值"]==r["流通市值"] else None) for _,r in df.iterrows()]
            o.executemany("INSERT OR REPLACE INTO hist_mv VALUES(?,?,?,?)",rows)
            o.execute("INSERT OR REPLACE INTO done VALUES(?,?,?)",(ts,len(rows),1)); o.commit()
        except Exception as e:
            o.execute("INSERT OR REPLACE INTO done VALUES(?,?,?)",(ts,0,0)); o.commit()
            if i<5 or "Connection" in str(e): print(f"  {ts} fail: {str(e)[:60]}", flush=True)
        if i%200==0:
            n=o.execute("SELECT COUNT(*) FROM hist_mv").fetchone()[0]
            print(f"  [{i}/{len(todo)}] {ts} 累计行数={n:,}", flush=True)
        time.sleep(SLEEP)
    n=o.execute("SELECT COUNT(*) FROM hist_mv").fetchone()[0]
    ok=o.execute("SELECT SUM(ok) FROM done").fetchone()[0]
    print(f"完成: {n:,} 行, 成功 {ok} 只", flush=True); o.close()

if __name__=="__main__": main()
