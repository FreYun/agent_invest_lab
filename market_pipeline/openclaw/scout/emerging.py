"""萌芽主线探测器 — N选K + 持续门 + 去重 + 撞门 + regime闸, 写 emerging_signal_daily.

EOD 在 board_trend 之后跑. 用法: python3 emerging.py [--date YYYYMMDD]
靶标(已回测验证): 报警后~2-3日内爬进确认主线Top10; 收益 regime-conditional,
强牛(STRONG_BULL)才高置信可操作(actionable=1), 其它 regime 仅描述性记录.
"""
from __future__ import annotations
import os, sys, json, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emerging_signals as es

EMERGING_K = 2     # N选K: 至少 K 个核心信号点头
EMERGING_D = 2     # 持续: N选K 条件连续 D 个交易日成立
COOLDOWN = 5       # 报过的板块 N 日内不重报


def _signals_for(conn, trade_date):
    return es.compute_core_signals(conn, trade_date)


def _recent_trade_dates(conn, trade_date, n):
    ds = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM concept_board_daily "
        "WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?", (trade_date, n))]
    return ds[::-1]


def _whitelist_codes():
    from whitelist import Whitelist
    wl = Whitelist(os.path.join(os.path.dirname(os.path.abspath(__file__)), "coarse_themes.json"))
    wl.load_if_changed()
    return set(wl.codes() or [])


def _mainline_top10(conn, trade_date):
    return {r[0] for r in conn.execute(
        "SELECT board_code FROM board_trend_daily WHERE trade_date=? AND rank<=10", (trade_date,))}


def _regime_for(conn, trade_date):
    """regime_classify_daily 日期带横杠, 用 replace 匹配 YYYYMMDD."""
    row = conn.execute(
        "SELECT regime_code FROM regime_classify_daily "
        "WHERE replace(trade_date,'-','')=?", (trade_date,)).fetchone()
    return row[0] if row else None


def _recent_alerted(conn, trade_date, cooldown):
    dates = _recent_trade_dates(conn, trade_date, cooldown + 1)
    if len(dates) < 2:
        return set()
    lo = dates[0]
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT board_code FROM emerging_signal_daily "
        "WHERE trade_date>=? AND trade_date<?", (lo, trade_date))}


def detect(conn, trade_date, K=EMERGING_K, D=EMERGING_D):
    """→ list[dict] 报警. 数据源经模块级函数注入(便于测试)."""
    wl = _whitelist_codes()
    mainline = _mainline_top10(conn, trade_date)
    regime = _regime_for(conn, trade_date)
    actionable = 1 if regime == "STRONG_BULL" else 0
    dates = _recent_trade_dates(conn, trade_date, D + 1)
    sig_by_date = {d: _signals_for(conn, d) for d in dates}
    today = sig_by_date.get(trade_date, {})
    alerts = []
    for bc, r in today.items():
        if bc in mainline:                       # 已到达确认主线, 非萌芽
            continue
        in_wl = bc in wl
        need_d = D if in_wl else D + 1           # 白名单外撞门 +1 天
        win = dates[-need_d:]
        if len(win) < need_d:
            continue
        ok = all(sig_by_date.get(d, {}).get(bc, {}).get("n_signals", 0) >= K for d in win)
        if not ok:
            continue
        tag = "强牛·可操作" if actionable else f"{regime or '?'}·仅观察"
        alerts.append({
            "trade_date": trade_date, "board_code": bc, "board_name": r["board_name"],
            "n_signals": r["n_signals"], "fired": sorted(r["fired"]),
            "persist_days": need_d, "is_whitelist": 1 if in_wl else 0,
            "regime_code": regime, "actionable": actionable,
            "reason": f"{'/'.join(sorted(r['fired']))} 连续{need_d}日点头" +
                      ("" if in_wl else "(撞门)") + f" · {tag}",
        })
    return alerts


def write(conn, trade_date, alerts):
    conn.execute("DELETE FROM emerging_signal_daily WHERE trade_date=?", (trade_date,))
    conn.executemany(
        "INSERT OR REPLACE INTO emerging_signal_daily "
        "(trade_date,board_code,board_name,n_signals,fired,persist_days,is_whitelist,"
        "regime_code,actionable,reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(a["trade_date"], a["board_code"], a["board_name"], a["n_signals"],
          json.dumps(a["fired"], ensure_ascii=False), a["persist_days"], a["is_whitelist"],
          a["regime_code"], a["actionable"], a["reason"]) for a in alerts])
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    import scout_db
    scout_db.init_schema()
    conn = scout_db.conn()
    td = a.date or conn.execute("SELECT MAX(trade_date) FROM board_trend_daily").fetchone()[0]
    if not td:
        print("[emerging] board_trend_daily 无数据, 跳过")
        conn.close(); return
    cooled = _recent_alerted(conn, td, COOLDOWN)
    alerts = [a for a in detect(conn, td) if a["board_code"] not in cooled]
    write(conn, td, alerts)
    act = sum(1 for a in alerts if a["actionable"])
    names = ", ".join(f"{a['board_name']}({'/'.join(a['fired'])})" for a in alerts) or "(无)"
    print(f"[emerging] {td}: 报警 {len(alerts)} 条(可操作 {act}); {names}")
    conn.close()


if __name__ == "__main__":
    main()
