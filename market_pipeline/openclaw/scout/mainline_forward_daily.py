"""scout 主线 Top10 次日 (T+1) 前瞻累计: 组合净值曲线 + 相对大盘累计超额.

口径 (见 docs/superpowers/specs/2026-07-13-mainline-forward-daily-design.md):
  - 样本: board_trend_daily 每日 rank<=top 的板块.
  - 板块次日收益: concept_board_daily.pct_change 在 T 的下一个交易日 (点位时点真实, 无前视偏差).
    (历史成分快照 stock_concept_map 仅当前一份, 无法历史回填成分股重算, 故用板块指数口径; 不剔 IPO 首日.)
  - 组合日收益: 篮子内各板块次日收益等权平均.
  - 大盘基准: 全A等权 daily.pct_chg 同一次日.
  - nav = 组合日收益逐日复利; mkt_nav 同理; excess_nav = nav/mkt_nav.

用法: /usr/bin/python3.12 mainline_forward_daily.py [--start 20240130] [--top 10]
"""
from __future__ import annotations
import argparse, os, sqlite3, statistics, csv
from collections import defaultdict

DB = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mainline_report")


def load_calendar(c):
    return [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM concept_board_daily ORDER BY trade_date")]


def load_board_pct(c):
    """board_code -> {trade_date: pct_change} (板块指数日涨跌)."""
    out = defaultdict(dict)
    for bc, d, p in c.execute(
            "SELECT board_code, trade_date, pct_change FROM concept_board_daily WHERE pct_change IS NOT NULL"):
        out[bc][d] = p
    return out


def load_mkt(c):
    """trade_date -> 全A等权日收益 (AVG daily.pct_chg)."""
    return {d: v for d, v in c.execute(
        "SELECT trade_date, AVG(pct_chg) FROM daily WHERE pct_chg IS NOT NULL GROUP BY trade_date")}


def load_topN(c, top, start):
    out = defaultdict(list)
    for d, bc in c.execute(
            "SELECT trade_date, board_code FROM board_trend_daily WHERE rank<=? ORDER BY trade_date, rank", (top,)):
        if d >= start:
            out[d].append(bc)
    return out


def compute_series(c, top, start):
    cal = load_calendar(c)
    idxof = {d: i for i, d in enumerate(cal)}
    board_pct = load_board_pct(c)
    mkt = load_mkt(c)
    topN = load_topN(c, top, start)

    rows = []
    nav = mkt_nav = 1.0
    for T in sorted(topN):
        i = idxof.get(T)
        if i is None or i + 1 >= len(cal):
            continue  # 无次日
        fwd = cal[i + 1]
        board_rets = [board_pct[bc][fwd] for bc in topN[T]
                      if bc in board_pct and fwd in board_pct[bc]]
        if not board_rets:
            continue  # 整篮次日无板块行情
        m = mkt.get(fwd)
        if m is None:
            continue
        port = statistics.mean(board_rets)
        nav *= (1 + port / 100)
        mkt_nav *= (1 + m / 100)
        rows.append({
            "trade_date": T, "fwd_date": fwd, "top": top,
            "port_ret": port, "mkt_ret": m, "excess": port - m,
            "nav": nav, "mkt_nav": mkt_nav, "excess_nav": nav / mkt_nav,
            "n_boards": len(board_rets), "n_stocks": None,
        })
    return rows


def write_table(c, rows, top):
    # 只清本次写入覆盖的日期区间 (而非该 top 全史), 使增量 --start 重跑不误删历史.
    if rows:
        lo = min(r["trade_date"] for r in rows)
        hi = max(r["trade_date"] for r in rows)
        c.execute("DELETE FROM board_trend_forward_daily WHERE top=? AND trade_date BETWEEN ? AND ?",
                  (top, lo, hi))
    c.executemany(
        "INSERT INTO board_trend_forward_daily"
        "(trade_date,fwd_date,top,port_ret,mkt_ret,excess,nav,mkt_nav,excess_nav,n_boards,n_stocks) "
        "VALUES(:trade_date,:fwd_date,:top,:port_ret,:mkt_ret,:excess,:nav,:mkt_nav,:excess_nav,:n_boards,:n_stocks)",
        rows)
    c.commit()
    return len(rows)


def max_drawdown(navs):
    if not navs or len(navs) < 2:
        return 0.0
    peak = navs[0]
    mdd = 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    return mdd * 100


def annualized(final_nav, n_days):
    if n_days <= 0:
        return 0.0
    return (final_nav ** (252 / n_days) - 1) * 100


def board_cum_ret(bc, T, w, cal, idxof, board_pct):
    """该板块 T 之后 w 个交易日板块指数累乘收益 %; 窗口不足 w 或窗口内无行情则 None."""
    i = idxof.get(T)
    if i is None:
        return None
    win = cal[i + 1:i + 1 + w]
    if len(win) < w:
        return None
    ps = [board_pct[bc][d] for d in win if bc in board_pct and d in board_pct[bc]]
    if len(ps) != len(win):
        return None  # 窗口内某日缺板块行情 -> 不算部分持有
    v = 1.0
    for p in ps:
        v *= (1 + p / 100)
    return (v - 1) * 100


def _mkt_cum_ret(T, w, cal, idxof, mkt):
    i = idxof.get(T)
    if i is None:
        return None
    win = cal[i + 1:i + 1 + w]
    if len(win) < w:
        return None
    ps = [mkt[d] for d in win if d in mkt]
    if len(ps) != len(win):
        return None  # 窗口内某日缺大盘数据 -> 不算部分持有
    v = 1.0
    for p in ps:
        v *= (1 + p / 100)
    return (v - 1) * 100


def holding_stats(c, top, start, w):
    cal = load_calendar(c)
    idxof = {d: i for i, d in enumerate(cal)}
    board_pct = load_board_pct(c)
    mkt = load_mkt(c)
    topN = load_topN(c, top, start)
    ports, wins = [], []
    for T in sorted(topN):
        brs = [board_cum_ret(bc, T, w, cal, idxof, board_pct) for bc in topN[T]]
        brs = [x for x in brs if x is not None]
        if not brs:
            continue
        mk = _mkt_cum_ret(T, w, cal, idxof, mkt)
        if mk is None:
            continue
        p = statistics.mean(brs)
        ports.append(p)
        wins.append(1 if p - mk > 0 else 0)
    if not ports:
        return {"n": 0, "mean": None, "win": None}
    return {"n": len(ports), "mean": statistics.mean(ports),
            "win": sum(wins) / len(wins) * 100}


def _agg(rows):
    if not rows:
        return {"n": 0, "port_mean": None, "excess_mean": None, "up_win": None,
                "beat_win": None, "end_nav": None, "end_excess_nav": None,
                "ann": None, "mdd_nav": None, "mdd_excess": None}
    ports = [r["port_ret"] for r in rows]
    exs = [r["excess"] for r in rows]
    navs = [r["nav"] for r in rows]
    exnavs = [r["excess_nav"] for r in rows]
    # 组内相对净值 (从该组第一行归 1)
    base = navs[0]
    rel = [v / base for v in navs]
    return {
        "n": len(rows),
        "port_mean": statistics.mean(ports),
        "excess_mean": statistics.mean(exs),
        "up_win": sum(1 for p in ports if p > 0) / len(ports) * 100,
        "beat_win": sum(1 for e in exs if e > 0) / len(exs) * 100,
        "end_nav": navs[-1],
        "end_excess_nav": exnavs[-1],
        "ann": annualized(rel[-1], len(rel)),
        "mdd_nav": max_drawdown(navs),
        "mdd_excess": max_drawdown(exnavs),
    }


def _last_year_cutoff(rows):
    if not rows:
        return "99999999"
    last = max(r["trade_date"] for r in rows)  # YYYYMMDD
    y, md = int(last[:4]), last[4:]
    return f"{y-1}{md}"


def summarize(rows, holds):
    cut = _last_year_cutoff(rows)
    out = {
        "overall": _agg(rows),
        "last_year": _agg([r for r in rows if r["trade_date"] >= cut]),
    }
    for y in ("2024", "2025", "2026"):
        out[f"y{y}"] = _agg([r for r in rows if r["trade_date"][:4] == y])
    out["hold3"] = holds.get(3, {"n": 0, "mean": None, "win": None})
    out["hold5"] = holds.get(5, {"n": 0, "mean": None, "win": None})
    return out


def forward_payload(db=DB, top=10):
    c = sqlite3.connect(db)
    series, agg_rows = [], []
    for (td, fd, pr, mr, ex, nav, mnav, exnav) in c.execute(
            "SELECT trade_date,fwd_date,port_ret,mkt_ret,excess,nav,mkt_nav,excess_nav "
            "FROM board_trend_forward_daily WHERE top=? ORDER BY trade_date", (top,)):
        series.append({"date": fd, "sel_date": td, "nav": nav, "mkt_nav": mnav,
                       "excess_nav": exnav, "port_ret": pr, "mkt_ret": mr, "excess": ex})
        agg_rows.append({"trade_date": td, "port_ret": pr, "excess": ex,
                         "nav": nav, "excess_nav": exnav})
    # 沪深300 基准线: 按各实现日 000300.SH 次日涨跌从 1.0 复利 (缺数据/缺表当日按 0%)
    try:
        hs = {d: p for d, p in c.execute(
            "SELECT trade_date, pct_chg FROM index_daily WHERE ts_code='000300.SH' AND pct_chg IS NOT NULL")}
    except sqlite3.OperationalError:
        hs = {}
    hv = 1.0
    for row in series:
        hv *= (1 + hs.get(row["date"], 0.0) / 100)
        row["hs300_nav"] = hv
    if agg_rows:
        start = min(r["trade_date"] for r in agg_rows)
        holds = {w: holding_stats(c, top, start, w) for w in (3, 5)}
    else:
        holds = {}
    summary = summarize(agg_rows, holds)
    c.close()
    meta = {"top": top, "n": len(series),
            "start": series[0]["sel_date"] if series else None,
            "end": series[-1]["sel_date"] if series else None}
    return {"series": series, "summary": summary, "meta": meta}


def run(db=DB, top=10, start="20240130", write=True, csv_out=True):
    c = sqlite3.connect(db)
    rows = compute_series(c, top, start)
    if write:
        write_table(c, rows, top)
    holds = {w: holding_stats(c, top, start, w) for w in (3, 5)}
    summary = summarize(rows, holds)
    csv_path = None
    if csv_out and rows:
        os.makedirs(OUT, exist_ok=True)
        csv_path = os.path.join(OUT, "mainline_forward_detail.csv")
        with open(csv_path, "w", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["选中日", "实现日", "口径", "组合次日%", "大盘次日%", "超额%",
                         "组合净值", "大盘净值", "超额净值", "板块数"])
            rd = lambda x: round(x, 4) if x is not None else None
            for r in rows:
                wr.writerow([r["trade_date"], r["fwd_date"], r["top"], rd(r["port_ret"]),
                             rd(r["mkt_ret"]), rd(r["excess"]), rd(r["nav"]), rd(r["mkt_nav"]),
                             rd(r["excess_nav"]), r["n_boards"]])
    c.close()
    return {"rows": rows, "summary": summary, "csv": csv_path}


def _fmt(label, s):
    if not s or s.get("n", 0) == 0:
        return f"  {label:<10} (样本不足)"
    return (f"  {label:<10} n={s['n']:>4}  组合日均{s['port_mean']:+.3f}% 超额日均{s['excess_mean']:+.3f}%  "
            f"上涨胜率{s['up_win']:4.1f}% 跑赢胜率{s['beat_win']:4.1f}%  "
            f"末净值{s['end_nav']:.3f} 超额净值{s['end_excess_nav']:.3f}  "
            f"年化{s['ann']:+.1f}% 回撤{s['mdd_nav']:.1f}%/超额{s['mdd_excess']:.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20240130")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--no-csv", action="store_true")
    a = ap.parse_args()
    res = run(top=a.top, start=a.start, write=not a.no_write, csv_out=not a.no_csv)
    s = res["summary"]
    rows = res["rows"]
    print(f"=== scout 主线 Top{a.top} 次日(T+1)前瞻累计 ===")
    if rows:
        print(f"区间 {rows[0]['trade_date']}~{rows[-1]['trade_date']}, 共 {len(rows)} 个选中交易日")
    print("口径: 板块指数(concept_board_daily)次日涨跌等权; 超额=组合−全A等权; nav逐日复利\n")
    print(_fmt("全周期", s["overall"]))
    print(_fmt("近一年", s["last_year"]))
    for y in ("2024", "2025", "2026"):
        print(_fmt(f"{y}年", s[f"y{y}"]))
    h3, h5 = s["hold3"], s["hold5"]
    def hf(w, h):
        if not h or h.get("n", 0) == 0:
            return f"  T+{w} 持有   (样本不足)"
        return f"  T+{w} 持有   n={h['n']:>4}  组合均值{h['mean']:+.3f}%  跑赢胜率{h['win']:4.1f}%"
    print("\n-- 持有多日对照 --")
    print(hf(3, h3)); print(hf(5, h5))
    if res["csv"]:
        print(f"\n明细 CSV: {res['csv']}")


if __name__ == "__main__":
    main()
