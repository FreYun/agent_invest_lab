"""策略累计表现计算 (v2 真实化口径) — 读 market.db, 输出 /api/perf payload。

带交易成本 + 复权(本地 pct_chg 重建) + 现金约束逐笔组合模拟。
每策略独立资金账户(起始 NAV=1.0), 固定 N 个等权仓位, 超容量按 signal_score 取前 K。
纯函数, sqlite conn 注入便于测试。横轴用 index_daily(000300.SH) 交易日。
"""
from __future__ import annotations

import datetime
import sqlite3
from collections import defaultdict

STRATEGIES = ["s1", "s2", "s3", "s4", "s5", "s6", "s7"]
BENCHMARKS = ["000300.SH", "000852.SH"]
_INVALID_STATUS = ("no_data", "limit_down_open_skip")

DEFAULT_SLOTS = 10           # 同时最多持仓数 N (等权)
PERF_START_DATE = "2025-01-01"
COMMISSION = 0.0000854       # 券商佣金 0.00854% 双边
STAMP_MAIN = 0.0005          # 印花税 主板 0.05% (仅卖出)
STAMP_TECH = 0.0006          # 印花税 科创/创业 0.06% (仅卖出)
TRANSFER_SH = 0.00001        # 过户费 沪市 0.001% 双向


def _ymd(d: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD"""
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


# ---------- 板块 / 代码 / 成本 ----------

def board_of(code: str) -> str:
    """6 位代码前缀判板块: sh_main/sh_star/sz_main/sz_gem/bj。"""
    p2 = code[:2]
    if p2 == "60":
        return "sh_main"
    if p2 == "68":
        return "sh_star"
    if p2 == "00":
        return "sz_main"
    if p2 == "30":
        return "sz_gem"
    return "bj"


def _ts_code(code: str) -> str:
    """6 位代码补后缀 (查 daily 用)。"""
    b = board_of(code)
    if b in ("sh_main", "sh_star"):
        return f"{code}.SH"
    if b in ("sz_main", "sz_gem"):
        return f"{code}.SZ"
    return f"{code}.BJ"


def trade_costs(code: str) -> tuple:
    """返回 (买入成本率, 卖出成本率)。卖出含印花税(按板), 沪市双向含过户费。"""
    b = board_of(code)
    transfer = TRANSFER_SH if b in ("sh_main", "sh_star") else 0.0
    stamp = STAMP_TECH if b in ("sh_star", "sz_gem") else STAMP_MAIN
    buy = COMMISSION + transfer
    sell = COMMISSION + stamp + transfer
    return round(buy, 8), round(sell, 8)


def net_return(gross: float, code: str) -> float:
    """毛收益(小数) -> 扣双边成本后的净收益。net=(1+g)(1-sell)/(1+buy)-1。"""
    buy, sell = trade_costs(code)
    return round((1.0 + gross) * (1.0 - sell) / (1.0 + buy) - 1.0, 6)


# ---------- 复权因子 (本地 pct_chg 重建, 零 API) ----------

def _index_from_rows(rows, needed=None) -> dict:
    """由某代码升序 daily 行重建累计复权索引 {trade_date: (G_cum, close)}。

    G_cum = ∏(1+pct_chg/100) 自序列起点(pct 缺失按 1.0 处理, 不放弃复权)。
    任一窗口 (t1, exit] 的 R_adj = G(exit)/G(t1), 绝对锚点在比值中抵消。
    needed 非空时只保留所需日期(端点), G 仍跨被跳过行累计 -> 省内存批量用。
    """
    idx = {}
    g = 1.0
    for r in rows:
        if r[2] is not None:
            g *= (1.0 + r[2] / 100.0)
        if needed is None or r[0] in needed:
            idx[r[0]] = (g, r[1])
    return idx


def adj_ratio(conn, code: str, t1_date: str, exit_date: str, index: dict = None) -> float:
    """复权修正因子 f = R_adj / R_raw, 用本地 daily.pct_chg 重建。

    R_adj = G(exit)/G(t1) (= ∏(1+pct_chg/100), 窗口 (t1, exit]);
    R_raw = close[exit] / close[t1]。无分红送配窗口 f≈1.0; 端点缺失/无数据 -> 1.0 兜底。
    index 提供时走 O(1) 查表(批量); 否则即时单查询重建该代码全历史(独立调用)。
    """
    if not t1_date or not exit_date:
        return 1.0
    if index is None:
        ts = _ts_code(code)
        try:
            rows = conn.execute(
                "SELECT trade_date, close, pct_chg FROM daily "
                "WHERE ts_code=? ORDER BY trade_date", (ts,)).fetchall()
        except sqlite3.OperationalError:
            return 1.0
        index = _index_from_rows([(r[0], r[1], r[2]) for r in rows])
    t1c, exc = t1_date.replace("-", ""), exit_date.replace("-", "")
    a, b = index.get(t1c), index.get(exc)
    if not a or not b:
        return 1.0
    g0, c0 = a
    g1, c1 = b
    if not c0 or not c1 or g0 == 0:
        return 1.0
    r_raw = c1 / c0
    if r_raw == 0:
        return 1.0
    return round((g1 / g0) / r_raw, 6)


def _build_adj_indexes(conn, raw_by_strategy: dict) -> dict:
    """批量重建各代码复权端点索引: {code: {date:(G_cum, close)}}。

    单次按日期升序全表扫描 daily, 每代码维护累计 G(running product), 只在端点日期
    (t1/exit) 落库。daily 仅有 (trade_date,ts_code) 索引、无 ts_code 前导索引, 逐码
    查询会退化成全索引扫描(每码 ~0.8s × 数千码); 改为一次扫描则 O(行数), 内存 O(端点数)。
    """
    need = defaultdict(set)          # ts_code -> {端点日期 YYYYMMDD}
    code6 = {}                       # ts_code -> 6 位代码
    for raw in raw_by_strategy.values():
        for r in raw:
            ts = _ts_code(r["code"])
            need[ts].add(r["entry_date"].replace("-", ""))
            need[ts].add(r["exit_date"].replace("-", ""))
            code6[ts] = r["code"]
    if not need:
        return {}
    idx_map = {}                     # 6 位代码 -> {date:(G,close)}
    g = {}                           # ts_code -> 累计 G
    try:
        cur = conn.execute(
            "SELECT ts_code, trade_date, close, pct_chg FROM daily ORDER BY trade_date")
    except sqlite3.OperationalError:
        return {}
    for ts, td, close, pct in cur:
        dates = need.get(ts)
        if dates is None:
            continue
        cg = g.get(ts, 1.0)
        if pct is not None:
            cg *= (1.0 + pct / 100.0)
        g[ts] = cg
        if td in dates:
            idx_map.setdefault(code6[ts], {})[td] = (cg, close)
    return idx_map


def _build_mark_paths(conn, raw_by_strategy: dict) -> dict:
    """逐日盯市用的累计复权路径: {6位代码: {trade_date YYYYMMDD: G_cum}}。

    与 _build_adj_indexes(只存端点) 不同, 这里记录每个代码在其 [min_entry, max_exit]
    持仓窗口内**所有交易日**的 G_cum, 供 simulate_nav 对未平仓持仓逐日盯市。
    单次按日期升序全表扫描 daily, 每代码维护累计 G, 落库窗口内每一天。
    """
    window = {}                      # ts_code -> [min_entry_raw, max_exit_raw]
    code6 = {}                       # ts_code -> 6 位代码
    for raw in raw_by_strategy.values():
        for r in raw:
            ts = _ts_code(r["code"])
            e = r["entry_date"].replace("-", "")
            x = r["exit_date"].replace("-", "")
            lo, hi = (e, x) if e <= x else (x, e)
            w = window.get(ts)
            if w is None:
                window[ts] = [lo, hi]
            else:
                if lo < w[0]:
                    w[0] = lo
                if hi > w[1]:
                    w[1] = hi
            code6[ts] = r["code"]
    if not window:
        return {}
    paths = {}                       # 6 位代码 -> {date: G_cum}
    g = {}                           # ts_code -> 累计 G
    try:
        cur = conn.execute(
            "SELECT ts_code, trade_date, pct_chg FROM daily ORDER BY trade_date")
    except sqlite3.OperationalError:
        return {}
    for ts, td, pct in cur:
        w = window.get(ts)
        if w is None:
            continue
        cg = g.get(ts, 1.0)
        if pct is not None:
            cg *= (1.0 + pct / 100.0)
        g[ts] = cg
        if w[0] <= td <= w[1]:
            paths.setdefault(code6[ts], {})[td] = cg
    return paths


def _attach_marks(trades: list, days: list, paths: dict) -> None:
    """给每笔 trade 追加 marks: {交易日 ISO: 持仓期毛收益}, 供 simulate_nav 盯市。

    对 (entry_date, exit_date) 间每个交易日 d: gross = G(d)/G(entry) - 1。
    入场日 mark=0(成本), 出场日由 net_ret 实现, 均不入 marks。停牌(该日无 G)跳过,
    由 simulate_nav carry-forward 上一已知 mark。
    """
    for t in trades:
        e_iso, x_iso = t["entry_date"], t["exit_date"]
        code_path = paths.get(t["code"])
        g_entry = code_path.get(e_iso.replace("-", "")) if code_path else None
        if not g_entry:
            t["marks"] = {}
            continue
        marks = {}
        for d in days:
            if d <= e_iso or d >= x_iso:
                continue
            gd = code_path.get(d.replace("-", ""))
            if gd is None:
                continue
            marks[d] = gd / g_entry - 1.0
        t["marks"] = marks


# ---------- 交易日轴 ----------

def trading_days(conn) -> list:
    """完整 A 股交易日轴 = 沪深300 trade_date (转 YYYY-MM-DD), 升序。"""
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM index_daily "
        "WHERE ts_code='000300.SH' ORDER BY trade_date").fetchall()
    return [_ymd(r[0]) for r in rows]


def _has_col(conn, table: str, col: str) -> bool:
    try:
        return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})").fetchall())
    except sqlite3.OperationalError:
        return False


def _can_filter_regime_gate(conn, s: str) -> bool:
    return _has_col(conn, f"{s}_select_runs", "regime_gate_allowed")


# ---------- 单策略取数 (逐笔, join 候选取分 + 复权 + 净收益) ----------

def _collect(conn, s: str, tradable_only: bool = True, since_date: str = PERF_START_DATE) -> list:
    """某策略已结算逐笔原始行(未算 f)。表缺失 -> []。

    每行: {t_date, entry_date(t1), exit_date, code, score, entry, exit_p,
           pnl_pct, exit_reason}。按 (entry_date, score desc, code) 排序。
    """
    inv = ",".join("?" * len(_INVALID_STATUS))
    score_sel = "c.signal_score" if _has_col(conn, f"{s}_candidates", "signal_score") else "NULL"
    gate_join = ""
    gate_where = ""
    if tradable_only and _can_filter_regime_gate(conn, s):
        gate_join = f" LEFT JOIN {s}_select_runs sr ON v.t_date=sr.date "
        gate_where = "AND (sr.regime_gate_allowed IS NULL OR sr.regime_gate_allowed=1) "
    since_clause = "AND v.t_date >= ? " if since_date else ""
    params = list(_INVALID_STATUS)
    if since_date:
        params.append(since_date)
    try:
        cur = conn.execute(
            f"SELECT v.t_date, v.t1_date, v.exit_date, v.code, v.entry_price, v.exit_price, "
            f"v.pnl_pct, v.exit_reason, {score_sel} AS score "
            f"FROM {s}_verifications v LEFT JOIN {s}_candidates c ON v.candidate_id=c.id "
            f"{gate_join}"
            f"WHERE v.pnl_pct IS NOT NULL AND v.status NOT IN ({inv}) "
            f"{since_clause}"
            f"{gate_where}"
            f"ORDER BY v.t1_date, score DESC, v.code", tuple(params))
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        entry, exit_p = r["entry_price"], r["exit_price"]
        if not entry or exit_p is None:
            continue
        if not r["t1_date"] or not r["exit_date"]:
            continue   # 缺时间线(s5 2015 老数据), 现金模型无法定位 -> 跳过
        out.append({
            "t_date": r["t_date"],
            "entry_date": r["t1_date"],
            "exit_date": r["exit_date"],
            "code": r["code"],
            "score": r["score"],
            "entry": entry,
            "exit_p": exit_p,
            "pnl_pct": r["pnl_pct"],
            "exit_reason": r["exit_reason"],
        })
    return out


def _apply_costs(raw: list, conn, adjust: bool = True, idx_map: dict = None) -> list:
    """原始行 -> 含 gross/net 的成交。idx_map: {code: 复权端点索引} 批量加速。

    每笔: {t_date, entry_date, exit_date, code, score, gross_ret, net_ret,
           exit_reason, pnl_pct}。
    """
    out = []
    for r in raw:
        if not adjust:
            f = 1.0
        elif idx_map is not None:
            # 批量模式: 缺失代码给空索引 -> f=1.0, 绝不回退逐码全扫
            f = adj_ratio(conn, r["code"], r["entry_date"], r["exit_date"], index=idx_map.get(r["code"], {}))
        else:
            f = adj_ratio(conn, r["code"], r["entry_date"], r["exit_date"])
        gross = (r["exit_p"] / r["entry"]) * f - 1.0
        out.append({
            "t_date": r["t_date"],
            "entry_date": r["entry_date"],
            "exit_date": r["exit_date"],
            "code": r["code"],
            "score": r["score"],
            "gross_ret": round(gross, 6),
            "net_ret": net_return(gross, r["code"]),
            "exit_reason": r["exit_reason"],
            "pnl_pct": r["pnl_pct"],
        })
    return out


def strategy_trades(conn, s: str, adjust: bool = True, idx_map: dict = None, tradable_only: bool = True, since_date: str = PERF_START_DATE) -> list:
    """某策略已结算逐笔交易(含 gross/net)。表缺失 -> []。

    idx_map 提供时走批量复权查表; 否则每笔即时单查询(独立/测试用)。
    """
    return _apply_costs(_collect(conn, s, tradable_only=tradable_only, since_date=since_date), conn, adjust, idx_map)


# ---------- 现金约束逐笔组合模拟 ----------

def simulate_nav(trades: list, days: list, n: int = DEFAULT_SLOTS) -> dict:
    """现金约束 + N 等权仓位 + 超容量取前 K 的逐笔模拟。

    起始 cash=1.0; 持有期逐日盯市(按 trade["marks"] 的当日毛收益估值, 缺 marks 退回成本口径),
    NAV 每日按 现金 + Σ 持仓盯市值 计; 出场日按净收益(扣双边成本)实现。
    每日先出场释放资金, 再按 score 降序入场(size=当日权益/N), 容量满或现金不足则跳过。
    返回 {nav:[[date,val]], taken:[trade...], exposure:float}。
    """
    if not trades or not days:
        return {"nav": [], "taken": [], "exposure": 0.0}
    by_entry = defaultdict(list)
    for t in trades:
        by_entry[t["entry_date"]].append(t)
    first_entry = min(t["entry_date"] for t in trades)

    cash = 1.0
    open_pos = []
    taken = []
    nav_series = []
    day_open = []   # [(date, open_count)]
    for d in days:
        if d < first_entry:
            continue
        # 1. 出场: 释放本金 + 实现净收益
        keep = []
        for p in open_pos:
            if p["exit_date"] == d:
                cash += p["basis"] * (1.0 + p["net_ret"])
            else:
                keep.append(p)
        open_pos = keep
        # 2. 入场: 按 score 降序, 当日固定仓位规模(权益用上一已知盯市值口径)
        equity = cash + sum(p["basis"] * (1.0 + p["mark"]) for p in open_pos)
        size = equity / n
        cands = sorted(by_entry.get(d, []),
                       key=lambda t: (-(t["score"] if t["score"] is not None else -1e18), t["code"]))
        for t in cands:
            if len(open_pos) >= n:
                break
            if cash < size - 1e-9:
                break          # 现金不足, 同日仓位规模相同, 直接停
            cash -= size
            open_pos.append({"exit_date": t["exit_date"], "basis": size,
                             "net_ret": t["net_ret"], "marks": t.get("marks", {}), "mark": 0.0})
            taken.append(t)
        # 3. 盯市: 持仓票按当日 mark 估值(停牌缺当日则 carry-forward 上一已知 mark)
        nav = cash
        for p in open_pos:
            m = p["marks"].get(d)
            if m is not None:
                p["mark"] = m
            nav += p["basis"] * (1.0 + p["mark"])
        nav_series.append([d, round(nav, 4)])
        day_open.append((d, len(open_pos)))

    last_exit = max((t["exit_date"] for t in taken), default=None)
    if taken and last_exit:
        active = [oc for dd, oc in day_open if dd <= last_exit]
        exposure = round(sum(oc / n for oc in active) / len(active), 4) if active else 0.0
    else:
        exposure = 0.0
    return {"nav": nav_series, "taken": taken, "exposure": exposure}


# ---------- 统计 (由 taken 净收益 + NAV 派生) ----------

def compute_stats(taken: list, nav: list, signals: int, exposure: float) -> dict:
    """总收益/胜率/平均单笔(net)/最大回撤/笔数/盈亏比 + signals/exposure。"""
    n = len(taken)
    if n == 0:
        return {"total_ret": None, "win_rate": None, "avg_pnl": None, "max_dd": None,
                "trades": 0, "profit_factor": None, "signals": signals, "exposure": exposure}
    rets = [t["net_ret"] for t in taken]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    total_ret = round(nav[-1][1] - 1.0, 4) if nav else None
    peak, max_dd = None, 0.0
    for _, v in nav:
        peak = v if peak is None else max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, v / peak - 1.0)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    # profit_factor: 全亏 -> 0.0; 全赢 -> None(除零未定义)
    pf = round(avg_win / abs(avg_loss), 2) if avg_loss != 0 else None
    return {
        "total_ret": total_ret,
        "win_rate": round(len(wins) / n, 4),
        "avg_pnl": round(sum(rets) / n * 100.0, 3),
        "max_dd": round(max_dd, 4),
        "trades": n,
        "profit_factor": pf,
        "signals": signals,
        "exposure": exposure,
    }


def _classify_exit(er: str) -> str | None:
    """退出原因 -> stop/take_profit/expire/None(空)。覆盖各策略真实口径。

    止损: 含 stop (stop_hit / stop_hit_at_open / stop_open / stop_intraday / stop_*_fill)。
    止盈: 含 target / take_profit / 前高 (hit_target_1/2 / take_profit[_open] / 反弹触及前高)。
    到期: 其余非空 (T+2_close / max_hold* / limit_up_broken / close below MA5 / close_exit|hold ...)。
    """
    if not er:
        return None
    low = er.lower()
    if "stop" in low or "止损" in er:
        return "stop"
    if "target" in low or "take_profit" in low or "前高" in er or "止盈" in er:
        return "take_profit"
    return "expire"


def exit_distribution(rows: list) -> dict:
    """退出原因三类占比: 止损/止盈/到期。"""
    cats = {"stop": 0, "take_profit": 0, "expire": 0}
    for r in rows:
        cat = _classify_exit(r.get("exit_reason") or "")
        if cat:
            cats[cat] += 1
    total = sum(cats.values())
    if total == 0:
        return {"stop": 0.0, "take_profit": 0.0, "expire": 0.0}
    return {k: round(v / total, 4) for k, v in cats.items()}


def monthly_from_nav(nav: list) -> list:
    """由 NAV 月末环比派生月度收益: nav_end[m]/nav_end[m-1]-1。首月以起始 NAV 为基。"""
    if not nav:
        return []
    last_of_month = {}
    for d, v in nav:
        last_of_month[d[:7]] = v
    prev = nav[0][1]
    out = []
    for ym in sorted(last_of_month):
        end = last_of_month[ym]
        out.append([ym, round(end / prev - 1.0, 4) if prev else 0.0])
        prev = end
    return out


# ---------- 今日盘中 / 基准 / 最新日 ----------

def today_live(conn, s: str, trade_today: str):
    """某策略今日已触发票浮盈均值 {"mean_ret": %, "n": int} 或 None。"""
    if not trade_today:
        return None
    try:
        cur = conn.execute(
            "SELECT ret_pct FROM intraday_trigger_log "
            "WHERE trade_date=? AND strategy=? AND triggered=1 AND ret_pct IS NOT NULL",
            (trade_today, s))
    except sqlite3.OperationalError:
        return None
    rets = [r[0] for r in cur.fetchall()]
    if not rets:
        return None
    return {"mean_ret": round(sum(rets) / len(rets), 3), "n": len(rets)}


def latest_trade_today(conn):
    try:
        return conn.execute("SELECT MAX(trade_date) FROM intraday_trigger_log").fetchone()[0]
    except sqlite3.OperationalError:
        return None


def latest_t_date(conn):
    dates = []
    for s in STRATEGIES:
        try:
            d = conn.execute(f"SELECT MAX(t_date) FROM {s}_verifications").fetchone()[0]
        except sqlite3.OperationalError:
            d = None
        if d:
            dates.append(d)
    return max(dates) if dates else None


def benchmark_series(conn, code: str, since_date: str = PERF_START_DATE) -> list:
    try:
        sql = "SELECT trade_date, close FROM index_daily WHERE ts_code=?"
        params = [code]
        if since_date:
            sql += " AND trade_date >= ?"
            params.append(since_date.replace("-", ""))
        sql += " ORDER BY trade_date"
        cur = conn.execute(sql, tuple(params))
    except sqlite3.OperationalError:
        return []
    return [[_ymd(r[0]), r[1]] for r in cur.fetchall() if r[1] is not None]


# ---------- 组装 ----------

def compute_settled(conn, n: int = DEFAULT_SLOTS, adjust: bool = True, tradable_only: bool = True, since_date: str = PERF_START_DATE) -> dict:
    """已结算历史段(最贵): nav/stats/exit_dist/monthly + 基准。

    含复权全表扫描(~10s), 但日内不变 -- 仅随每日结算(latest_t_date 推进)变化。
    server 按 latest_t_date 缓存此段, 盘中 60s 轮询只跑 overlay_live(廉价)。
    """
    days = trading_days(conn)
    raw_by = {s: _collect(conn, s, tradable_only=tradable_only, since_date=since_date) for s in STRATEGIES}
    idx_map = _build_adj_indexes(conn, raw_by) if adjust else None
    mark_paths = _build_mark_paths(conn, raw_by)
    strategies = {}
    for s in STRATEGIES:
        trades = _apply_costs(raw_by[s], conn, adjust=adjust, idx_map=idx_map)
        if not trades:
            continue
        _attach_marks(trades, days, mark_paths)
        sim = simulate_nav(trades, days, n=n)
        nav = sim["nav"]
        taken = sim["taken"]
        strategies[s] = {
            "nav": nav,
            "stats": compute_stats(taken, nav, signals=len(trades), exposure=sim["exposure"]),
            "exit_dist": exit_distribution(taken),
            "monthly": monthly_from_nav(nav),
        }
    benchmarks = {}
    for b in BENCHMARKS:
        ser = benchmark_series(conn, b)
        if ser:
            benchmarks[b] = ser
    return {
        "latest_t_date": latest_t_date(conn),
        "slots": n,
        "adjusted": adjust,
        "tradable_only": tradable_only,
        "since_date": since_date,
        "strategies": strategies,
        "benchmarks": benchmarks,
    }


def overlay_live(conn, settled: dict) -> dict:
    """在已结算段上叠加今日盘中浮盈点(廉价: 仅查 intraday_trigger_log), 返回完整 payload。

    不修改入参 settled(可被 server 复用缓存), 每个策略追加 today_live, 顶层补 meta/s8_today。
    """
    tt = latest_trade_today(conn)
    strategies = {}
    for s, d in settled["strategies"].items():
        nav = d["nav"]
        tl = today_live(conn, s, tt) if tt else None
        today_pt = None
        if tl and nav:
            today_pt = {
                "nav": round(nav[-1][1] * (1 + tl["mean_ret"] / 100.0), 4),
                "mean_ret": tl["mean_ret"], "n": tl["n"],
            }
        strategies[s] = {**d, "today_live": today_pt}
    s8 = today_live(conn, "s8", tt) if tt else None
    s8_today = None
    if s8:
        s8_today = {"nav": round(1.0 * (1 + s8["mean_ret"] / 100.0), 4),
                    "mean_ret": s8["mean_ret"], "n": s8["n"], "date": tt}
    return {
        "meta": {
            "now": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "latest_t_date": settled["latest_t_date"],
            "trade_today": tt,
            "slots": settled["slots"],
            "adjusted": settled["adjusted"],
            "tradable_only": settled.get("tradable_only", True),
            "since_date": settled.get("since_date", PERF_START_DATE),
        },
        "strategies": strategies,
        "s8_today": s8_today,
        "benchmarks": settled["benchmarks"],
    }


def compute_perf(conn, n: int = DEFAULT_SLOTS, adjust: bool = True, tradable_only: bool = True, since_date: str = PERF_START_DATE) -> dict:
    """组装 /api/perf 完整 payload (v2 真实化口径) = 已结算段 + 盘中叠加。"""
    return overlay_live(conn, compute_settled(conn, n=n, adjust=adjust, tradable_only=tradable_only, since_date=since_date))
