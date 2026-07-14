"""趋势主线 EOD 计算 — 复刻概念板块指数算趋势分, 写 board_trend_daily.

日线为底; 盘中实时叠加在 server.py build_payload 做.
stock-select-daily.sh 末尾每个交易日调一次.

趋势分(0~100) = 0.25*趋势位置 + 0.30*动量 + 0.15*量能放大 + 0.30*成交占比
  趋势位置 = 站上MA20(0.4)+站上MA60(0.3)+多头排列(0.3)
  动量     = ret20/ret60 加权后横截面 min-max 归一
  量能放大 = 近5日/近20日 板块成交额比, 横截面归一
  成交占比 = 0.5*占比水平 + 0.5*占比提升趋势, 横截面归一

用法:
  python3 board_trend.py              # 最新交易日
  python3 board_trend.py --date 20260522
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s8_live import _is_broad  # noqa: E402  复用宽基/地域过滤
from whitelist import Whitelist  # noqa: E402

_WL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coarse_themes.json")
WHITELIST = Whitelist(_WL_PATH)

DB_PATH = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))

# 东财"市场表现/打板梯队/价格分桶/股权结构"伪板块 — 非投资性主线, 必须剔除.
# 注意: 只匹配确切 token, 不能误伤真实概念(印制电路板/玻璃基板/面板/瓷砖地板等).
_NOISE_RE = re.compile(
    r"^昨日|热股|人气|龙虎|连板|首板|多板|炸板|触板|打.?板|"
    r"次新股|破净股|破发股|超跌股|微利股|价值股|周期股|红利股|低价股|高价股|百元股|"
    r"大盘股|中盘股|小盘股|微盘股|权重股|证金持股|做市股|AB股|AH股|B股|ST股")


def _is_noise(name: str) -> bool:
    return bool(_NOISE_RE.search(name or ""))
WEIGHTS = {"trend_pos": 0.25, "momentum": 0.30, "vol_ratio": 0.15, "amt_share": 0.30}
WINDOW = 70          # 复刻指数回看交易日
MIN_MEMBERS = 5
MAX_MEMBERS = 400    # 剔超大行业宽板(如"电子"全行业), 只留概念级主线
TOP_N_STORE = 200    # 落库白名单全量 (~86个有效), 给"潜在主线"算 rank 变化提供完整底座
                     # 前端 趋势主线 仍只取 Top10; 潜在主线 用全量 rank 历史算 5日上升幅度


def _recon_index(pcts):
    """日 pct_change(%) 序列(旧→新) 复利还原为指数, 起点 1.0. 返回等长序列."""
    idx, v = [], 1.0
    for p in pcts:
        v *= (1 + (p or 0) / 100)
        idx.append(v)
    return idx


def _ma(series, n):
    return sum(series[-n:]) / n if len(series) >= n else None


def _ret(series, n):
    """近 n 日累计涨幅 %. 序列长度需 > n, 否则 None."""
    if len(series) <= n:
        return None
    return (series[-1] / series[-1 - n] - 1) * 100


def _peak_ratio(amts, n=20):
    """近5日均额 / 近n日峰值额. 天量度: 越接近1越是在量能高位.
    无量/峰值<=0/空序列 → None."""
    if not amts:
        return None
    recent = amts[-n:]
    peak = max(recent) if recent else 0.0
    if peak <= 0:
        return None
    a5 = sum(amts[-5:]) / min(5, len(amts))
    return a5 / peak


def _dist_pct(cur, ma):
    """当前值相对均线的距离 %. (cur/ma − 1)×100. cur/ma 缺或 ma<=0 → None."""
    if cur is None or ma is None or ma <= 0:
        return None
    return (cur / ma - 1) * 100


def _minmax(values):
    """list[float|None] -> list[0..1]; None→0; 全相等→全0.5."""
    nums = [v for v in values if v is not None]
    if not nums:
        return [0.0 for _ in values]
    lo, hi = min(nums), max(nums)
    if hi - lo < 1e-12:
        return [0.5 if v is not None else 0.0 for v in values]
    return [0.0 if v is None else (v - lo) / (hi - lo) for v in values]


# ── DB 读取 ────────────────────────────────────────────────────────────
def _window_dates(c, trade_date, n=WINDOW):
    """返回截至 trade_date 的最近 n 个交易日(旧→新)."""
    ds = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM concept_board_daily "
        "WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?", (trade_date, n))]
    return ds[::-1]


def _board_pct_series(c, dates):
    """board_code -> [board_name, {date: pct_change}]."""
    dph = ",".join("?" * len(dates))
    out = {}
    for bc, bn, d, p in c.execute(
        f"SELECT board_code, board_name, trade_date, pct_change FROM concept_board_daily "
        f"WHERE trade_date IN ({dph})", dates):
        e = out.setdefault(bc, [bn, {}])
        if bn:
            e[0] = bn
        e[1][d] = p
    return out


def _board_amount_by_date(c, dates):
    """board_code -> {date: amount(亿)}.

    主源: board_amount_daily (东财 dc_daily 板块级成交额, 元) / 1e8 — 板块级直取, 满额稳定,
          不依赖逐日成分(dc_member 对最新交易日常发不全). 与旧"成分 SUM"口径数值逐位等价.
    回退: 该窗口 board_amount_daily 全空时, 退回最新成分快照 SUM(daily.amount千元)/1e5 (软回滚)."""
    dph = ",".join("?" * len(dates))
    out = {}
    try:
        for bc, d, amt in c.execute(
            f"SELECT board_code, trade_date, amount/1e8 FROM board_amount_daily "
            f"WHERE trade_date IN ({dph})", dates):
            if amt is not None:
                out.setdefault(bc, {})[d] = amt
    except sqlite3.OperationalError:
        out = {}  # 表不存在(全新库/backfill 未跑) → 走软回滚
    if out:
        return out
    # 软回滚: 板块级成交额表为空 → 旧成分聚合口径
    for bc, d, amt in c.execute(
        f"SELECT scm.board_code, dd.trade_date, SUM(dd.amount)/1e5 "
        f"FROM stock_concept_map scm JOIN daily dd ON scm.ts_code=dd.ts_code "
        f"WHERE scm.snapshot_date=(SELECT MAX(snapshot_date) FROM stock_concept_map) "
        f"AND dd.trade_date IN ({dph}) GROUP BY scm.board_code, dd.trade_date", dates):
        out.setdefault(bc, {})[d] = amt or 0.0
    return out


def _market_amount_by_date(c, dates):
    """date -> 全市场成交额(亿) = SUM(daily.amount千元)/1e5."""
    dph = ",".join("?" * len(dates))
    return {d: (a or 0.0) for d, a in c.execute(
        f"SELECT trade_date, SUM(amount)/1e5 FROM daily WHERE trade_date IN ({dph}) "
        f"GROUP BY trade_date", dates)}


def _member_count(c):
    return {bc: n for bc, n in c.execute(
        "SELECT board_code, COUNT(*) FROM stock_concept_map "
        "WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM stock_concept_map) "
        "GROUP BY board_code")}


def _lead(c, trade_date):
    """{board_code: (leader_ts_code, leader_name)} — 优先用 board_leader_daily 4 因子评分,
    缺失/未启动板块兜底东财 leading_code (一日游). leader_ts_code 带后缀."""
    out = {bc: (lc, ln) for bc, lc, ln in c.execute(
        "SELECT board_code, leading_code, leading_name FROM concept_board_daily "
        "WHERE trade_date=?", (trade_date,))}
    for bc, ts_code, name in c.execute(
            "SELECT board_code, ts_code, name FROM board_leader_daily "
            "WHERE trade_date=? AND rank=1", (trade_date,)):
        out[bc] = (ts_code, name)
    return out


# ── orchestrator ───────────────────────────────────────────────────────
def compute_trend(c, trade_date):
    """→ [dict] 按 trend_score 降序, 取前 TOP_N_STORE."""
    WHITELIST.load_if_changed()
    dates = _window_dates(c, trade_date)
    if not dates:
        return []
    pct_map = _board_pct_series(c, dates)
    amt_map = _board_amount_by_date(c, dates)
    mkt_amt = _market_amount_by_date(c, dates)
    mcount = _member_count(c)
    lead = _lead(c, trade_date)

    # 接入粗粒度白名单 (空 codes 时跳过, 退化到旧过滤逻辑, 软回滚开关)
    wl_codes = WHITELIST.codes()
    raw = []
    for bc, (bn, pmap) in pct_map.items():
        if _is_broad(bn) or _is_noise(bn):
            continue
        if wl_codes and bc not in wl_codes:
            continue
        m = mcount.get(bc, 0)
        if m < MIN_MEMBERS or m > MAX_MEMBERS:
            continue
        pcts = [pmap[d] for d in dates if pmap.get(d) is not None]
        if len(pcts) < 21:
            continue
        idx = _recon_index(pcts)
        ma5, ma10, ma20, ma60 = _ma(idx, 5), _ma(idx, 10), _ma(idx, 20), _ma(idx, 60)
        cur = idx[-1]
        dist_ma20 = _dist_pct(cur, ma20)
        above20 = 1 if (ma20 and cur >= ma20) else 0
        above60 = 1 if (ma60 and cur >= ma60) else 0
        aligned = 1 if (ma5 and ma10 and ma20 and ma60 and ma5 > ma10 > ma20 > ma60) else 0
        trend_pos = 0.4 * above20 + 0.3 * above60 + 0.3 * aligned
        ret20, ret60, ret5 = _ret(idx, 20), _ret(idx, 60), _ret(idx, 5)
        mom_raw = 0.6 * max(ret20 or 0, 0) + 0.4 * max((ret60 if ret60 is not None else (ret20 or 0)), 0)

        amts = [amt_map.get(bc, {}).get(d, 0.0) for d in dates]
        a5 = sum(amts[-5:]) / min(5, len(amts)) if amts else 0.0
        a20 = sum(amts[-20:]) / min(20, len(amts)) if amts else 0.0
        amt_peak_ratio = _peak_ratio(amts)
        vol_ratio = (a5 / a20) if a20 > 0 else None
        shares = [(amt_map.get(bc, {}).get(d, 0.0) / mkt_amt[d]) if mkt_amt.get(d) else None
                  for d in dates]
        s5 = [s for s in shares[-5:] if s is not None]
        s20 = [s for s in shares[-20:] if s is not None]
        amt_share = sum(s5) / len(s5) if s5 else None
        share20 = sum(s20) / len(s20) if s20 else None
        amt_share_trend = (amt_share / share20 - 1) if (amt_share and share20 and share20 > 0) else None

        lc, ln = lead.get(bc, (None, None))
        raw.append({
            "board_code": bc, "board_name": bn,
            "trend_pos": trend_pos, "mom_raw": mom_raw,
            "vol_ratio": vol_ratio, "amt_share": amt_share, "amt_share_trend": amt_share_trend,
            "ret20": round(ret20, 2) if ret20 is not None else None,
            "ret60": round(ret60, 2) if ret60 is not None else None,
            "ret5": round(ret5, 2) if ret5 is not None else None,
            "amt_peak_ratio": round(amt_peak_ratio, 3) if amt_peak_ratio is not None else None,
            "dist_ma20": round(dist_ma20, 2) if dist_ma20 is not None else None,
            "ma_aligned": aligned, "above_ma20": above20, "above_ma60": above60,
            "lead_code": lc, "lead_name": ln,
        })
    if not raw:
        return []

    nmom = _minmax([r["mom_raw"] for r in raw])
    nvol = _minmax([min(max(r["vol_ratio"], 0.8), 2.0) if r["vol_ratio"] else None for r in raw])
    nlvl = _minmax([r["amt_share"] for r in raw])
    ntrd = _minmax([r["amt_share_trend"] for r in raw])
    for i, r in enumerate(raw):
        amt_factor = 0.5 * nlvl[i] + 0.5 * ntrd[i]
        score = (WEIGHTS["trend_pos"] * r["trend_pos"]
                 + WEIGHTS["momentum"] * nmom[i]
                 + WEIGHTS["vol_ratio"] * nvol[i]
                 + WEIGHTS["amt_share"] * amt_factor)
        r["trend_score"] = round(100 * score, 2)
        r["amt_share"] = round(r["amt_share"], 5) if r["amt_share"] is not None else None
        r["amt_share_trend"] = round(r["amt_share_trend"], 4) if r["amt_share_trend"] is not None else None
        r["vol_ratio"] = round(r["vol_ratio"], 3) if r["vol_ratio"] is not None else None
    raw.sort(key=lambda r: -r["trend_score"])
    for rank, r in enumerate(raw, 1):
        r["rank"] = rank
    return raw[:TOP_N_STORE]


def write(c, trade_date, rows):
    c.execute("DELETE FROM board_trend_daily WHERE trade_date=?", (trade_date,))
    c.executemany(
        "INSERT OR REPLACE INTO board_trend_daily "
        "(trade_date,board_code,board_name,trend_score,ret20,ret60,ret5,amt_peak_ratio,dist_ma20,"
        "vol_ratio,amt_share,amt_share_trend,ma_aligned,above_ma20,above_ma60,lead_code,lead_name,rank) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(trade_date, r["board_code"], r["board_name"], r["trend_score"], r["ret20"], r["ret60"],
          r["ret5"], r["amt_peak_ratio"], r["dist_ma20"], r["vol_ratio"], r["amt_share"],
          r["amt_share_trend"], r["ma_aligned"], r["above_ma20"], r["above_ma60"],
          r["lead_code"], r["lead_name"], r["rank"]) for r in rows])
    c.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYYMMDD, 默认最新交易日")
    a = ap.parse_args()
    import scout_db
    scout_db.init_schema()
    c = scout_db.conn()
    td = a.date or c.execute("SELECT MAX(trade_date) FROM concept_board_daily").fetchone()[0]
    if not td:
        print("[board_trend] concept_board_daily 无数据, 跳过")
        c.close()
        return
    rows = compute_trend(c, td)
    write(c, td, rows)
    top = ", ".join(f"{r['board_name']}({r['trend_score']})" for r in rows[:5])
    print(f"[board_trend] {td}: 写入 {len(rows)} 条; top5= {top}")
    c.close()


if __name__ == "__main__":
    main()
