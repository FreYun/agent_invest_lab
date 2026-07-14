"""924 行情(2024-09-24)以来累计涨跌幅: 个股 / 申万行业 / 概念 三视图.

两种口径(mode), 由前端开关切换:
  adj   (默认) = 复权含分红总回报: 每日 pct_chg 累乘 EXP(SUM(LN(1+pct_chg/100)))-1,
                 等价前/后复权累计收益, 送转分红除权缺口全部消化(见 CLAUDE.md「未复权陷阱」)。
  price        = 纯价格涨跌不含分红: 送转/配股机械缺口日(pre_close/前日close<0.92)用 close/pre_close
                 还原(只取当日真实涨跌), 其余日用 close/前日close 裸日收益(分红除息的价跌计入=不含分红)。
基准 = 2024-09-23 收盘 (窗口 trade_date>='20240924', 含 9-24 当天大涨). 只读, 不写库.

缓存仅服务端包装 (return_map/stocks_payload/...) 使用, 按 (latest trade_date, mode) 失效;
单测直接调纯函数 (_compute_ret_map/build_stocks_payload/...) 不经缓存, 故测试间隔离.
"""
from __future__ import annotations

import threading
from collections import defaultdict

BASE_DATE = "20240923"       # 基准收盘日 (展示)
WINDOW_START = "20240924"    # 累乘窗口起点 (含当天)

MODES = ("adj", "price")     # adj=复权含分红(默认), price=纯价格不含分红(送转还原)

BENCHMARKS = [("000001.SH", "上证指数"), ("000300.SH", "沪深300"), ("000852.SH", "中证1000")]

# 缓存: 按 latest trade_date 失效; modes 下每个 mode 一份 {ret_map, stocks, concept}
_CACHE = {"key": None, "modes": {}}
_LOCK = threading.Lock()


def segment_of(ts_code: str) -> str:
    """从代码派生板块段 (1:1 干净)."""
    code, _, mkt = ts_code.partition(".")
    if mkt == "BJ":
        return "北交所"
    if mkt == "SH":
        return "科创板" if code.startswith("688") else "沪主板"
    if mkt == "SZ":
        return "创业板" if code[:3] in ("300", "301") else "深主板"
    return "其他"


def _pct(sv, p):
    """sv: 已升序的非空 list[float]; p: 0-100 分位. 线性插值(numpy 默认法)."""
    n = len(sv)
    if n == 0:
        return None
    if n == 1:
        return sv[0]
    k = (n - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, n - 1)
    return sv[f] + (sv[c] - sv[f]) * (k - f)


def _stats(vals):
    """vals: list[float] 收益. 返回 count/median/mean/up_ratio + 分位 p10/p25/p75/p90.
    median 即 p50; 分位用于看板块内部分布离散度(中位附近多集中 vs 两极分化)."""
    n = len(vals)
    if n == 0:
        return {"count": 0, "median": None, "mean": None, "up_ratio": None,
                "p10": None, "p25": None, "p75": None, "p90": None}
    sv = sorted(vals)
    up = sum(1 for v in vals if v > 0)
    return {"count": n, "median": _pct(sv, 50), "mean": sum(vals) / n, "up_ratio": up / n,
            "p10": _pct(sv, 10), "p25": _pct(sv, 25), "p75": _pct(sv, 75), "p90": _pct(sv, 90)}


def _names(conn):
    """查询 stock_names 表，返回 ts_code -> name 字典."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT ts_code, name FROM stock_names WHERE name IS NOT NULL")}


def _latest_date(conn):
    """查询 daily 表的最新交易日."""
    row = conn.execute("SELECT MAX(trade_date) FROM daily").fetchone()
    return row[0] if row else None


def _adj_returns(conn) -> dict:
    """复权含分红总回报: 每日 pct_chg 累乘. 返回 {ts_code: (ret, n)}.
    EXP(SUM(LN(1+pct_chg/100.0)))-1 自动消化送转分红除权缺口."""
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT ts_code, EXP(SUM(LN(1+pct_chg/100.0)))-1 AS cum, COUNT(*) AS n "
        "FROM daily WHERE trade_date >= ? AND pct_chg IS NOT NULL AND pct_chg > -100 "
        "GROUP BY ts_code", (WINDOW_START,))}


def _price_only_returns(conn) -> dict:
    """纯价格(送转还原、不含分红)累计涨跌幅. 返回 {ts_code: (ret, n)}.

    逐日因子: 送转/配股机械缺口日(前日close存在且 pre_close/前日close < 0.92)用 close/pre_close
    还原(只取当日真实涨跌, 不让送转假摔); 其余日用 close/前日close 的裸日收益(分红除息价跌计入=不含分红)。
    CTE 从 BASE_DATE 起以便窗口首日(20240924)能取到前日(20240923)收盘; 次新上市首日无前日时分母回退 pre_close。
    """
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "WITH seq AS ("
        "  SELECT ts_code, trade_date, close, pre_close, "
        "         LAG(close) OVER (PARTITION BY ts_code ORDER BY trade_date) AS prev_close "
        "  FROM daily WHERE trade_date >= ?"
        ") "
        "SELECT ts_code, "
        "       EXP(SUM(LN(CASE "
        "         WHEN prev_close IS NOT NULL AND prev_close>0 AND pre_close/prev_close < 0.92 "
        "              THEN close/pre_close "
        "         ELSE close/COALESCE(NULLIF(prev_close,0), pre_close) END)))-1 AS cum, "
        "       COUNT(*) AS n "
        "FROM seq "
        "WHERE trade_date >= ? AND close>0 AND pre_close>0 "
        "      AND COALESCE(NULLIF(prev_close,0), pre_close) > 0 "
        "GROUP BY ts_code", (BASE_DATE, WINDOW_START))}


def _compute_ret_map(conn, mode="adj") -> dict:
    """累计收益 + 现价 + 次新标注. 仅当前在交易(最新交易日有行)的股票.

    mode: 'adj'=复权含分红总回报(默认) | 'price'=纯价格不含分红(送转还原)。
    返回格式: {ts_code: {"ret": float, "days": int, "is_new": bool, "price": float}}
    - ret: 选定 mode 的累计涨跌幅
    - days: WINDOW_START 以来交易天数
    - is_new: 次新标记 (全历史首交易日 >= WINDOW_START)
    - price: 最新收盘价(裸价, 仅展示)
    """
    latest = _latest_date(conn)
    if not latest:
        return {}

    # 查询最新交易日所有股票的收盘价(裸价, 仅展示)
    price = {r[0]: r[1] for r in conn.execute(
        "SELECT ts_code, close FROM daily WHERE trade_date = ?", (latest,))}

    # 按 mode 选口径计算 WINDOW_START 以来的累计涨跌幅
    rets = _price_only_returns(conn) if mode == "price" else _adj_returns(conn)

    # 查询每只股票的全历史首个交易日，用于判断次新
    first = {r[0]: r[1] for r in conn.execute(
        "SELECT ts_code, MIN(trade_date) FROM daily GROUP BY ts_code")}

    # 构造输出：仅包含最新交易日有行且 WINDOW_START 以来有收益数据的股票
    out = {}
    for ts, px in price.items():
        if ts not in rets:
            continue
        cum, n = rets[ts]
        out[ts] = {"ret": cum, "days": n,
                   "is_new": first.get(ts, "") >= WINDOW_START, "price": px}
    return out


def _benchmarks(conn) -> list:
    """计算基准指数 WINDOW_START 以来的复权累计收益率. fail-soft: 无数据跳过."""
    out = []
    for code, nm in BENCHMARKS:
        row = conn.execute(
            "SELECT EXP(SUM(LN(1+pct_chg/100.0)))-1 FROM index_daily "
            "WHERE ts_code=? AND trade_date>=? AND pct_chg IS NOT NULL AND pct_chg > -100",
            (code, WINDOW_START)).fetchone()
        # 仅当有数据且结果非 NULL 时加入，否则 failsoft 跳过不报错
        if row and row[0] is not None:
            out.append({"name": nm, "code": code, "ret": row[0]})
    return out


def sw_agg(conn, ret_map) -> list:
    """申万一级聚合, 每个 L1 带 L2 子聚合 + 领涨/领跌. 未匹配归 '未分类'."""
    # 构建 ts_code -> (l1_name, l2_name) 的映射表
    mem = {r[0]: (r[1] or "未分类", r[2] or "未分类") for r in conn.execute(
        "SELECT ts_code, l1_name, l2_name FROM sw_industry_member")}
    names = _names(conn)

    # 按 L1、(L1,L2) 分组收益率
    l1g, l2g = defaultdict(list), defaultdict(list)
    for ts, d in ret_map.items():
        l1, l2 = mem.get(ts, ("未分类", "未分类"))
        l1g[l1].append((ts, d["ret"]))
        l2g[(l1, l2)].append((ts, d["ret"]))

    # 按 L1 计算聚合统计和领涨/领跌
    out = []
    for l1, items in l1g.items():
        st = _stats([r for _, r in items])
        top = max(items, key=lambda x: x[1])
        bot = min(items, key=lambda x: x[1])

        # 计算 L2 子聚合
        subs = []
        for (a, l2name), sit in l2g.items():
            if a != l1:
                continue
            subs.append({"l2_name": l2name, **_stats([r for _, r in sit])})
        # L2 按 median 降序排列
        subs.sort(key=lambda x: (x["median"] is None, -(x["median"] or 0)))

        # 构建 L1 项
        out.append({"l1_name": l1, **st,
                    "leader": {"code": top[0], "name": names.get(top[0], top[0]), "ret": top[1]},
                    "laggard": {"code": bot[0], "name": names.get(bot[0], bot[0]), "ret": bot[1]},
                    "l2": subs})

    # L1 按 median 降序排列
    out.sort(key=lambda x: (x["median"] is None, -(x["median"] or 0)))
    return out


def build_stocks_payload(conn, ret_map) -> dict:
    """构建个股全量 payload: 包含 overview + stocks + sw (申万聚合)."""
    latest = _latest_date(conn)
    names = _names(conn)
    # 构建 ts_code -> (l1_code, l1_name, l2_name) 映射
    sw = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT ts_code, l1_code, l1_name, l2_name FROM sw_industry_member")}

    # 构建个股列表和收益率列表
    stocks, rets = [], []
    for ts, d in ret_map.items():
        nm = names.get(ts, ts)
        l1c, l1n, l2n = sw.get(ts, (None, None, None))
        stocks.append({
            "code": ts, "name": nm, "ret": d["ret"], "price": d["price"],
            "segment": segment_of(ts), "is_st": "ST" in (nm or ""),
            "is_new": d["is_new"], "days": d["days"],
            "l1_code": l1c, "l1_name": l1n or "未分类", "l2_name": l2n or "未分类",
        })
        rets.append(d["ret"])

    # 按 ret 降序排列个股
    stocks.sort(key=lambda x: -x["ret"])

    # 计算 overview 统计
    st = _stats(rets)
    return {
        "base_date": BASE_DATE, "latest_date": latest, "count": len(stocks),
        "overview": {
            "median": st["median"], "mean": st["mean"],
            "up": sum(1 for v in rets if v > 0), "down": sum(1 for v in rets if v < 0),
            "double": sum(1 for v in rets if v >= 1.0), "halve": sum(1 for v in rets if v <= -0.5),
            "benchmarks": _benchmarks(conn),
        },
        "stocks": stocks,
        "sw": sw_agg(conn, ret_map),
    }


def _mode_slot(conn, mode):
    """返回该 mode 的缓存槽(按 latest trade_date 失效). 调用者须已持 _LOCK. 未知 mode 归 'adj'."""
    if mode not in MODES:
        mode = "adj"
    latest = _latest_date(conn)
    if _CACHE["key"] != latest:
        _CACHE["key"] = latest
        _CACHE["modes"] = {}
    return _CACHE["modes"].setdefault(mode, {})


def return_map(conn, mode="adj") -> dict:
    """服务端缓存入口: 按 (latest trade_date, mode) 失效."""
    with _LOCK:
        slot = _mode_slot(conn, mode)
        if "ret_map" not in slot:
            slot["ret_map"] = _compute_ret_map(conn, mode if mode in MODES else "adj")
        return slot["ret_map"]


def stocks_payload(conn, mode="adj") -> dict:
    """服务端缓存包装: 个股全量 payload. 按 (latest trade_date, mode) 失效."""
    with _LOCK:
        slot = _mode_slot(conn, mode)
        if "ret_map" not in slot:
            slot["ret_map"] = _compute_ret_map(conn, mode if mode in MODES else "adj")
        if "stocks" not in slot:
            slot["stocks"] = build_stocks_payload(conn, slot["ret_map"])
        return slot["stocks"]


def _latest_snapshot(conn):
    """查询 stock_concept_map 的最新 snapshot_date."""
    row = conn.execute("SELECT MAX(snapshot_date) FROM stock_concept_map").fetchone()
    return row[0] if row else None


def _board_names(conn):
    """{board_code: board_name} —— 概念板块真实名称来自 concept_board_daily(最新交易日, 按 board_code 稳定).
    注意: stock_concept_map.board_name 列在本库装的是个股名(脏数据), 不可当板块名用。"""
    row = conn.execute("SELECT MAX(trade_date) FROM concept_board_daily").fetchone()
    latest = row[0] if row else None
    if not latest:
        return {}
    return {r[0]: r[1] for r in conn.execute(
        "SELECT board_code, board_name FROM concept_board_daily WHERE trade_date=?", (latest,))}


def concept_agg(conn, ret_map) -> dict:
    """概念板块聚合: 按 snapshot_date 最新日期, 统计每个 board_code 的成分 rets.

    返回格式: {snapshot_date: str, concepts: [{board_code, board_name, count, median, mean,
                                               up_ratio, leader, laggard}]}
    - leader/laggard: 涨跌幅最大/最小个股
    - concepts 按 median 降序排列
    - board_name 取自 concept_board_daily(权威源), 不再用 stock_concept_map.board_name(脏数据)
    """
    snap = _latest_snapshot(conn)
    if not snap:
        return {"snapshot_date": None, "concepts": []}

    names = _names(conn)
    board_names = _board_names(conn)
    # 按 board_code 分组该 snapshot_date 的概念成分的 rets
    # 只取 board_code 和 ts_code，board_name 列不再使用（脏数据：装的是个股名而非板块名）
    g = defaultdict(list)
    for bc, ts in conn.execute(
            "SELECT board_code, ts_code FROM stock_concept_map WHERE snapshot_date=?",
            (snap,)):
        if ts in ret_map:
            g[bc].append((ts, ret_map[ts]["ret"]))

    # 计算每个概念的统计 + leader/laggard
    concepts = []
    for bc, items in g.items():
        st = _stats([r for _, r in items])
        top = max(items, key=lambda x: x[1])
        bot = min(items, key=lambda x: x[1])
        concepts.append({
            "board_code": bc, "board_name": board_names.get(bc, bc), **st,
            "leader": {"code": top[0], "name": names.get(top[0], top[0]), "ret": top[1]},
            "laggard": {"code": bot[0], "name": names.get(bot[0], bot[0]), "ret": bot[1]}
        })

    # 按 median 降序排列
    concepts.sort(key=lambda x: (x["median"] is None, -(x["median"] or 0)))
    return {"snapshot_date": snap, "concepts": concepts}


def concept_members(conn, ret_map, board_code) -> dict:
    """按 board_code 查询该概念的所有成分个股及其收益率.

    返回格式: {board_code, board_name, count, members: [{code, name, ret, price, is_st, is_new}]}
    - members 按 ret 降序排列
    - board_name 取自 concept_board_daily(权威源), 不再用 stock_concept_map.board_name(脏数据)
    """
    snap = _latest_snapshot(conn)
    names = _names(conn)
    board_names = _board_names(conn)
    members = []
    # 只取 board_code 和 ts_code，board_name 列不再使用（脏数据：装的是个股名而非板块名）
    for bc, ts in conn.execute(
            "SELECT board_code, ts_code FROM stock_concept_map "
            "WHERE snapshot_date=? AND board_code=?", (snap, board_code)):
        if ts in ret_map:
            d = ret_map[ts]
            nm = names.get(ts, ts)
            members.append({
                "code": ts, "name": nm, "ret": d["ret"], "price": d["price"],
                "is_st": "ST" in (nm or ""), "is_new": d["is_new"]
            })

    # 按 ret 降序排列
    members.sort(key=lambda x: -x["ret"])
    return {
        "board_code": board_code,
        "board_name": board_names.get(board_code, board_code),
        "count": len(members),
        "members": members,
    }


def concept_payload(conn, mode="adj") -> dict:
    """服务端缓存包装: 概念聚合. 按 (latest trade_date, mode) 失效."""
    with _LOCK:
        slot = _mode_slot(conn, mode)
        if "ret_map" not in slot:
            slot["ret_map"] = _compute_ret_map(conn, mode if mode in MODES else "adj")
        if "concept" not in slot:
            slot["concept"] = concept_agg(conn, slot["ret_map"])
        return slot["concept"]


def members_payload(conn, board_code, mode="adj") -> dict:
    """服务端包装: 按 board_code 查询概念成分. 不缓存."""
    return concept_members(conn, return_map(conn, mode), board_code)
