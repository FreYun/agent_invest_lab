"""板块"真"龙头 EOD 评分 — 替代东财 leading_code 一日游.

设计意图: 窗口口径对齐 scout/board_trend.py (动量 0.6*ret20+0.4*ret60), 否则
"板块趋势分用 20+60 双窗口选出来的强主线" 跟 "龙头用 20 日单窗口选出来的成员" 不一致,
会出现"60 日中线龙头(如帝尔激光)" 被 "20 日短期爆发龙头(如长电科技)" 错位顶掉的反直觉.

leader_score(0~100) = 0.35*相对强度 + 0.25*持续性 + 0.20*量能放大 + 0.20*抗跌
  相对强度 = 0.6 * (ret20 / 板块ret20) + 0.4 * (ret60 / 板块ret60), 板块内 min-max
  持续性   = 近60日个股 pct_chg > 板块 pct_change 的天数 / 60
  量能放大 = 个股近5日均额 / 近20日均额, [0.8, 2.0] 裁后板块内归一
  抗跌     = 60日板块下跌日里 (个股 pct - 板块 pct) 均值, 板块内归一

注: 4 因子全部"板块内"min-max 归一, 而不是横截面归一(因为我们要的是板块内的相对龙头).
落库 board_leader_daily, 每板块写 Top3 (rank 1=龙头, 2/3=接力位).

用法:
  python3 board_leader.py              # 最新交易日
  python3 board_leader.py --date 20260527
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402
from s8_live import _is_broad  # noqa: E402

WINDOW = 60               # 主窗口拉到 60 日 (对齐 board_trend 中期视角)
SHORT_WINDOW = 20         # 短期窗口仍保留, 双窗口加权用
MIN_BOARD_RET = 0.0       # 板块 ret20 ≤ 0 跳过(没启动谈不上龙头, 用短期视角判断启动)
MIN_STOCK_DAYS = 45       # 60 日里至少要有 45 个交易日数据(剔新股/长期停牌)
MIN_MEMBERS = 5
TOP_N_STORE = 10          # 每板块落库 Top10 (前 3 给 UI 显示, 后 7 给 board_etf 反查 ETF)
WEIGHTS = {"str": 0.35, "per": 0.25, "vol": 0.20, "def": 0.20}
# 相对强度: 60日 0.6 + 20日 0.4. 板块层面 board_trend 用 0.6*短+0.4*长 是合理的(识别启动),
# 但"龙头"本质是"持续跑赢", 60 日窗口应主导, 短期只用来防止识别滞后.
STR_SHORT_W = 0.4
STR_LONG_W = 0.6


def _window_dates(c, trade_date: str):
    # 多取 1 天: N 日 ret 需要 N+1 个端点 (起点 close + N 天后 close)
    rows = c.execute(
        "SELECT DISTINCT trade_date FROM concept_board_daily WHERE trade_date<=? "
        "ORDER BY trade_date DESC LIMIT ?", (trade_date, WINDOW + 1)).fetchall()
    return [r[0] for r in rows][::-1]  # 旧→新


def _board_series(c, dates):
    """{board_code: {board_name, pct_by_date{}}}."""
    out = {}
    rows = c.execute(
        "SELECT trade_date, board_code, board_name, pct_change FROM concept_board_daily "
        f"WHERE trade_date IN ({','.join('?'*len(dates))})", dates).fetchall()
    for d, bc, bn, pct in rows:
        if pct is None:
            continue
        rec = out.setdefault(bc, {"name": bn, "pct": {}})
        rec["pct"][d] = pct
    return out


def _name_map(c):
    """ts_code → 股票名兜底. stock_concept_map.name 写入时全空, 多源 UNION 兜底.
    优先级: kpl_theme_daily (覆盖广) > concept_board_daily.leading (兜北交所).
    全市场覆盖率 > 99% 即可, 极少数缺失显示 ts_code 不影响功能."""
    out = {}
    for ts, name in c.execute(
            "SELECT ts_code, MAX(stock_name) FROM kpl_theme_daily "
            "WHERE stock_name IS NOT NULL AND stock_name != '' GROUP BY ts_code"):
        out[ts] = name
    for ts, name in c.execute(
            "SELECT leading_code, MAX(leading_name) FROM concept_board_daily "
            "WHERE leading_code IS NOT NULL AND leading_name IS NOT NULL "
            "GROUP BY leading_code"):
        out.setdefault(ts, name)
    return out


def _members(c, nmap):
    """{board_code: [(ts_code, name)]}, name 用 _name_map 兜底."""
    out = {}
    for bc, ts_code, name in c.execute(
            "SELECT board_code, ts_code, name FROM stock_concept_map"):
        out.setdefault(bc, []).append((ts_code, name or nmap.get(ts_code) or ts_code))
    return out


def _stock_daily(c, ts_codes, dates):
    """{ts_code: {date: (close, amount, pct_chg)}}. 一次性 IN 拉全, 内存友好."""
    out = {}
    if not ts_codes or not dates:
        return out
    # 分批避免 SQL IN 子句过长 (SQLite 默认 999 个变量上限)
    BATCH = 500
    ts_list = list(ts_codes)
    for i in range(0, len(ts_list), BATCH):
        batch = ts_list[i:i + BATCH]
        ph_ts = ",".join("?" * len(batch))
        ph_d = ",".join("?" * len(dates))
        sql = (f"SELECT ts_code, trade_date, close, amount, pct_chg FROM daily "
               f"WHERE ts_code IN ({ph_ts}) AND trade_date IN ({ph_d})")
        for ts, d, close, amt, pct in c.execute(sql, tuple(batch) + tuple(dates)):
            out.setdefault(ts, {})[d] = (close, amt, pct)
    return out


def _ret(closes):
    """末值/首值 - 1, %单位. 任一为 None/0 返回 None."""
    if len(closes) < 2 or not closes[0] or not closes[-1]:
        return None
    return (closes[-1] / closes[0] - 1) * 100


def _minmax(vals):
    """板块内 min-max 归一到 [0,1]; 全 None 返回 [None,...]; 极差 0 返回 [0.5,...]."""
    ok = [v for v in vals if v is not None]
    if not ok:
        return [None] * len(vals)
    lo, hi = min(ok), max(ok)
    if hi == lo:
        return [0.5 if v is not None else None for v in vals]
    return [(v - lo) / (hi - lo) if v is not None else None for v in vals]


def _ret_window(closes_ord, n):
    """已按日期升序的 close 序列, 取末 n+1 个点算累计涨幅(%). 不够长返回 None."""
    if len(closes_ord) < n + 1:
        return None
    a, b = closes_ord[-(n + 1)], closes_ord[-1]
    if not a or not b:
        return None
    return (b / a - 1) * 100


def _board_ret(pcts_ord, n):
    """已按日期升序的 pct 序列, 取末 n 个交易日复利还原累计涨幅(%). 不够长返回 None."""
    series = [p for p in pcts_ord[-n:] if p is not None]
    if len(series) < n - 2:  # 容忍偶尔停牌缺失
        return None
    idx = 1.0
    for p in series:
        idx *= (1 + p / 100)
    return (idx - 1) * 100


def compute_board(bc, members, dates, board_pct, sd):
    """单板块 → [dict] Top3 按 leader_score 降序. 不够 3 只补几只算几只.
    sd = {ts_code: {date: (close, amount, pct)}} 已预拉. dates 已按日期升序."""
    board_pcts = [board_pct.get(d) for d in dates]
    board_pcts_v = [p for p in board_pcts if p is not None]
    if len(board_pcts_v) < MIN_STOCK_DAYS:
        return []
    # 双窗口板块累计涨幅
    board_ret20 = _board_ret(board_pcts, SHORT_WINDOW)
    board_ret60 = _board_ret(board_pcts, WINDOW)
    if not board_ret20 or board_ret20 <= MIN_BOARD_RET:
        return []  # 短期未启动板块跳过

    raw = []
    for ts_code, name in members:
        rec = sd.get(ts_code, {})
        closes_ord = [rec.get(d, (None, None, None))[0] for d in dates]
        amts = [rec.get(d, (None, None, None))[1] for d in dates]
        pcts = [rec.get(d, (None, None, None))[2] for d in dates]
        valid_days = sum(1 for c in closes_ord if c is not None)
        if valid_days < MIN_STOCK_DAYS:
            continue
        stock_ret20 = _ret_window([c for c in closes_ord if c is not None], SHORT_WINDOW)
        stock_ret60 = _ret_window([c for c in closes_ord if c is not None], WINDOW)
        if stock_ret20 is None and stock_ret60 is None:
            continue
        # 因子1 相对强度: 双窗口加权 (0.6*短 + 0.4*长), 对齐 board_trend 动量
        s20 = (stock_ret20 / board_ret20) if (stock_ret20 is not None and board_ret20 > 0) else None
        s60 = (stock_ret60 / board_ret60) if (stock_ret60 is not None and board_ret60 and board_ret60 > 0) else None
        if s20 is None and s60 is None:
            str_factor = None
        elif s60 is None:
            str_factor = s20
        elif s20 is None:
            str_factor = s60
        else:
            str_factor = STR_SHORT_W * s20 + STR_LONG_W * s60
        # 因子2 持续性: 60日跑赢板块天数占比(中线持续性)
        win_days = 0
        cmp_days = 0
        for sp, bp in zip(pcts, board_pcts):
            if sp is None or bp is None:
                continue
            cmp_days += 1
            if sp > bp:
                win_days += 1
        per_factor = (win_days / cmp_days) if cmp_days else None
        # 因子3 量能放大: 5/20 日 (跟 board_trend 一致, 窗口不拉长否则失去短期信号)
        valid_amts = [a for a in amts if a is not None and a > 0]
        if len(valid_amts) >= 10:
            a5 = sum(valid_amts[-5:]) / min(5, len(valid_amts))
            a20 = sum(valid_amts[-20:]) / min(20, len(valid_amts))
            vol_factor = a5 / a20 if a20 > 0 else None
            if vol_factor is not None:
                vol_factor = min(max(vol_factor, 0.8), 2.0)
        else:
            vol_factor = None
        # 因子4 抗跌: 60日板块下跌日的超额
        excess_down = []
        for sp, bp in zip(pcts, board_pcts):
            if sp is None or bp is None or bp >= 0:
                continue
            excess_down.append(sp - bp)
        def_factor = sum(excess_down) / len(excess_down) if len(excess_down) >= 3 else None

        raw.append({
            "ts_code": ts_code, "name": name,
            "stock_ret20": round(stock_ret20, 2) if stock_ret20 is not None else None,
            "stock_ret60": round(stock_ret60, 2) if stock_ret60 is not None else None,
            "str_factor_raw": str_factor,
            "per_factor": round(per_factor, 3) if per_factor is not None else None,
            "vol_factor_raw": vol_factor,
            "def_factor_raw": def_factor,
        })
    if len(raw) < MIN_MEMBERS:
        return []

    # 板块内 min-max 归一
    nstr = _minmax([r["str_factor_raw"] for r in raw])
    nvol = _minmax([r["vol_factor_raw"] for r in raw])
    ndef = _minmax([r["def_factor_raw"] for r in raw])
    # 持续性本身已是 [0,1], 不再归一
    for i, r in enumerate(raw):
        per = r["per_factor"] if r["per_factor"] is not None else 0.0
        s = (WEIGHTS["str"] * (nstr[i] or 0)
             + WEIGHTS["per"] * per
             + WEIGHTS["vol"] * (nvol[i] or 0)
             + WEIGHTS["def"] * (ndef[i] or 0))
        r["leader_score"] = round(100 * s, 2)
        r["str_factor"] = round(nstr[i], 3) if nstr[i] is not None else None
        r["vol_factor"] = round(nvol[i], 3) if nvol[i] is not None else None
        r["def_factor"] = round(ndef[i], 3) if ndef[i] is not None else None
        # 落库的 board_ret20 字段保留, 但内容改为反映"双窗口主要参考的 60 日板块涨幅"
        # (短期 20 日仍在 stock_ret20 里, 长期 60 日在 stock_ret60)
        r["board_ret20"] = round(board_ret60 if board_ret60 is not None else board_ret20, 2)
    raw.sort(key=lambda r: -r["leader_score"])
    return raw[:TOP_N_STORE]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYYMMDD, 默认最新")
    a = ap.parse_args()
    scout_db.init_schema()
    c = scout_db.conn()
    if a.date:
        trade_date = a.date
    else:
        trade_date = c.execute(
            "SELECT MAX(trade_date) FROM concept_board_daily").fetchone()[0]
    print(f"[board-leader] trade_date={trade_date}")

    dates = _window_dates(c, trade_date)
    if len(dates) < MIN_STOCK_DAYS:
        print(f"  ❌ 历史不足 {MIN_STOCK_DAYS} 天 (实有 {len(dates)}), 跳过")
        return
    bseries = _board_series(c, dates)
    nmap = _name_map(c)
    members = _members(c, nmap)
    # 全部成分股 ts_code 全集 (一次性拉日线, 避免 N 个板块 N 次 query)
    all_ts = set()
    for bc, recs in members.items():
        if bc not in bseries:
            continue
        if _is_broad(bseries[bc]["name"] or ""):
            continue
        for ts, _ in recs:
            all_ts.add(ts)
    print(f"  全成分股 {len(all_ts)} 只, 板块 {len(bseries)} 个, 拉日线...")
    sd = _stock_daily(c, all_ts, dates)
    print(f"  日线 {sum(len(v) for v in sd.values())} 行")

    out_rows = []
    skipped = {"宽基": 0, "板块未启动": 0, "成分不足": 0}
    for bc, brec in bseries.items():
        bn = brec["name"] or ""
        if _is_broad(bn):
            skipped["宽基"] += 1
            continue
        ms = members.get(bc, [])
        if len(ms) < MIN_MEMBERS:
            skipped["成分不足"] += 1
            continue
        top = compute_board(bc, ms, dates, brec["pct"], sd)
        if not top:
            skipped["板块未启动"] += 1
            continue
        for rank, r in enumerate(top, 1):
            out_rows.append((
                trade_date, bc, rank, r["ts_code"], r["name"],
                r["leader_score"], r["str_factor"], r["per_factor"],
                r["vol_factor"], r["def_factor"],
                r.get("stock_ret20"), r.get("stock_ret60"), r["board_ret20"],
            ))

    if out_rows:
        c.execute("DELETE FROM board_leader_daily WHERE trade_date=?", (trade_date,))
        c.executemany(
            "INSERT INTO board_leader_daily "
            "(trade_date, board_code, rank, ts_code, name, leader_score, "
            "str_factor, per_factor, vol_factor, def_factor, "
            "stock_ret20, stock_ret60, board_ret20) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", out_rows)
        c.commit()
    n_boards = len({r[1] for r in out_rows})
    print(f"  ✓ 写入 {len(out_rows)} 行 / {n_boards} 个板块")
    print(f"  跳过: 宽基 {skipped['宽基']}, 板块未启动 {skipped['板块未启动']}, "
          f"成分不足 {skipped['成分不足']}")
    c.close()


if __name__ == "__main__":
    main()
