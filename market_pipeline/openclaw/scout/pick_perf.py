"""推票战绩回测 — 读 daily_pick(推票) + daily(OHLC) + index_daily(交易日历),
逐笔回测 agent 推荐有效性, 聚合胜率/盈亏。纯函数, conn 注入便于测试。

口径见 docs/superpowers/specs/2026-06-01-scout-pick-perf-design.md。
"""
from __future__ import annotations

import bisect
import json
import sqlite3

import perf  # 复用 _ts_code / trading_days / 复权思路

ENTRY_WINDOW = 10        # 入场观察窗(交易日)
HOLD_WINDOW = 20         # 最长持仓窗(交易日, 含买入日)
EXDIV_THRESHOLD = 0.005  # pre_close 与前日 close 相对偏差 > 此值判为除权日

# 业绩基准: [(指数代码, 权重)], 每日再平衡的加权组合; 可换成单指数 [(code, 1.0)]。
# 选型依据: 推票四板块全覆盖+市值中盘(中位数~300亿), 60/40 对策略日收益拟合最优
# (相关性 0.86 vs 单中证1000 0.84), 详见 2026-07-11 基准讨论。样本满一季度后复核配比。
BENCHMARK = (("000852.SH", 0.6), ("399006.SZ", 0.4))
BENCHMARK_LABEL = "基准(中证1000×0.6+创业板指×0.4)"


def _d8(d: str) -> str:
    """YYYY-MM-DD -> YYYYMMDD"""
    return d.replace("-", "")


def _ymd(d8: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD"""
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"


def _eligible_start(slot: str, trade_date: str, days: list) -> str | None:
    """入场起算日: premarket 取 >= 推票日的首个交易日; 盘中票取 > 推票日的首个交易日。"""
    if slot == "premarket":
        i = bisect.bisect_left(days, trade_date)   # 首个 >= trade_date
    else:
        i = bisect.bisect_right(days, trade_date)  # 首个 > trade_date
    return days[i] if i < len(days) else None


def _window_days(days: list, start: str, n: int) -> list:
    """days 中从 start(含) 起的 n 个交易日。start 不在 days 时返回 []。"""
    i = bisect.bisect_left(days, start)
    if i >= len(days) or days[i] != start:
        return []
    return days[i:i + n]


def _first_entry(bars: dict, days: list, eligible_start: str,
                 entry_low: float, entry_high: float,
                 window: int = ENTRY_WINDOW) -> str | None:
    """eligible_start 起 window 个交易日内首个能按均值价成交的交易日。
    买入价固定取区间均值 (entry_low+entry_high)/2; 仅当该均值价落在当日 [low, high]
    内才算真正买得到——跳空高开越过均值价(low>均值)或整日在区间下方都不算入场,
    继续等价格回到均值价(仍受 window 约束)。"""
    mid = (entry_low + entry_high) / 2.0
    for d in _window_days(days, eligible_start, window):
        bar = bars.get(d)
        if bar and bar["low"] <= mid <= bar["high"]:
            return d
    return None


def _adjusted_bars(conn, code: str, from_date: str) -> dict:
    """from_date(含) 起该票 daily OHLC, 前复权对齐到 from_date 价基。
    检测除权: pre_close 偏离上一交易日 raw close > EXDIV_THRESHOLD -> 累乘因子。
    返回 {YYYY-MM-DD: {open,high,low,close}} (已复权)。"""
    ts = perf._ts_code(code)
    try:
        rows = conn.execute(
            "SELECT trade_date, open, high, low, close, pre_close FROM daily "
            "WHERE ts_code=? AND trade_date>=? ORDER BY trade_date",
            (ts, _d8(from_date))).fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    cumfactor = 1.0
    prev_close_raw = None
    for td, o, h, l, c, pc in rows:
        if prev_close_raw and pc is not None and prev_close_raw > 0:
            if abs(pc - prev_close_raw) / prev_close_raw > EXDIV_THRESHOLD:
                cumfactor *= prev_close_raw / pc
        f = cumfactor
        out[_ymd(td)] = {"open": (o or 0) * f, "high": (h or 0) * f,
                         "low": (l or 0) * f, "close": (c or 0) * f}
        prev_close_raw = c   # 用 raw close 做下一次除权检测
    return out


def _simulate_exit(bars: dict, days: list, buy_date: str, buy_price: float,
                   stop: float, target: float, hold_window: int = HOLD_WINDOW):
    """T+1 起逐日判卖出(止损优先), 满 hold_window 个交易日强平。
    返回 (status, sell_date, sell_price, hold_days)。"""
    bi = bisect.bisect_left(days, buy_date)
    last_day, last_close = buy_date, bars.get(buy_date, {}).get("close")
    # T+1 .. 第 hold_window 日 (下标 bi+1 .. bi+hold_window-1)
    for j in range(bi + 1, bi + hold_window):
        if j >= len(days):
            break
        bar = bars.get(days[j])
        if not bar:
            continue
        last_day, last_close = days[j], bar["close"]
        if bar["open"] <= stop:                     # ① 开盘跳空打止损
            return "stop", days[j], bar["open"], j - bi
        if bar["low"] <= stop:                      # ② 盘中最低打止损
            return "stop", days[j], stop, j - bi
        if bar["high"] >= target:                   # ③ 摸目标(含跳空高开)
            return "target", days[j], target, j - bi
    last_idx = bi + hold_window - 1
    hd = bisect.bisect_left(days, last_day) - bi    # 与 last_day 自洽(停牌时不强行=19)
    if last_idx < len(days):                        # 满 20 日 -> 收盘强平
        return "timeout", last_day, last_close, hd
    return "holding", last_day, last_close, hd


def _position_day_rets(bars: dict, days: list, buy_date: str, buy_price: float,
                       sell_date: str, sell_price: float) -> dict:
    """单笔持仓逐日收益(供组合口径累计收益聚合)。
    买入日 = close/买价 - 1; 中间日 = close/前收 - 1; 卖出日 = 卖价/前收 - 1;
    停牌日(缺 bar)记 0(资金停驻)。返回 {YYYY-MM-DD: ret}。"""
    i = bisect.bisect_left(days, buy_date)
    j = bisect.bisect_left(days, sell_date)
    out, prev = {}, buy_price
    for k in range(i, j + 1):
        d = days[k]
        px = sell_price if d == sell_date else bars.get(d, {}).get("close")
        if not px or not prev:
            out[d] = 0.0
            continue
        out[d] = px / prev - 1.0
        prev = px
    return out


def _base_result(pick: dict, status: str, **extra) -> dict:
    r = {"trade_date": pick["trade_date"], "slot": pick["slot"],
         "picked_at": pick.get("picked_at"),
         "reviewer": pick["reviewer"], "rank": pick.get("rank"),
         "code": pick["code"], "name": pick.get("name"),
         "entry_low": pick.get("entry_low"), "entry_high": pick.get("entry_high"),
         "stop_loss": pick.get("stop_loss"), "target_price": pick.get("target_price"),
         "one_liner": pick.get("one_liner"), "logic_stars": pick.get("logic_stars"),
         "action_stars": pick.get("action_stars"), "position_tier": pick.get("position_tier"),
         "status": status, "buy_date": None, "buy_price": None,
         "sell_date": None, "sell_price": None, "ret": None,
         "hold_days": None, "win": None, "day_rets": {}}
    r.update(extra)
    return r


def evaluate_pick(conn, pick: dict, days: list) -> dict:
    """单笔推票回测。days = 交易日轴(YYYY-MM-DD 升序)。"""
    el, eh = pick.get("entry_low"), pick.get("entry_high")
    stop, target = pick.get("stop_loss"), pick.get("target_price")
    if None in (el, eh, stop, target) or el <= 0 or eh <= 0 or stop <= 0 or target <= 0:
        return _base_result(pick, "no_data")
    start = _eligible_start(pick["slot"], pick["trade_date"], days)
    if start is None:
        return _base_result(pick, "no_data")
    bars = _adjusted_bars(conn, pick["code"], start)
    if not bars:
        return _base_result(pick, "no_data")
    buy_date = _first_entry(bars, days, start, el, eh)
    if buy_date is None:
        # 观察窗未走满 10 个交易日(轴还没到) → pending(待入场), 别过早判 no_entry
        elapsed = len(_window_days(days, start, ENTRY_WINDOW))
        return _base_result(pick, "no_entry" if elapsed >= ENTRY_WINDOW else "pending")
    buy_price = (el + eh) / 2.0
    status, sell_date, sell_price, hold_days = _simulate_exit(
        bars, days, buy_date, buy_price, stop, target)
    ret = (sell_price - buy_price) / buy_price if sell_price else None
    if status == "target":
        win = True
    elif status == "stop":
        win = False
    elif status == "timeout":
        win = ret is not None and ret > 0
    else:  # holding -> 不计胜负
        win = None
    day_rets = (_position_day_rets(bars, days, buy_date, buy_price, sell_date, sell_price)
                if sell_price else {})
    return _base_result(pick, status, buy_date=buy_date, buy_price=buy_price,
                        sell_date=sell_date, sell_price=sell_price,
                        ret=ret, hold_days=hold_days, win=win, day_rets=day_rets)


def _load_picks(conn, reviewer: str = None, since: str = None) -> list:
    """读 daily_pick, 解 target_price。reviewer/since 可选过滤。
    剔除买点区间/止损/目标价不全的票(无完整交易计划无法回测, 不进推票战绩)。"""
    sql = ("SELECT trade_date, slot, reviewer, rank, code, name, "
           "entry_low, entry_high, stop_loss, deep_research_json, picked_at, "
           "one_liner, logic_stars, action_stars, position_tier FROM daily_pick WHERE 1=1")
    args = []
    if reviewer:
        sql += " AND reviewer=?"; args.append(reviewer)
    if since:
        sql += " AND trade_date>=?"; args.append(since)
    sql += " ORDER BY trade_date, reviewer, rank"
    picks = []
    for r in conn.execute(sql, args).fetchall():
        try:
            tp = (json.loads(r["deep_research_json"]) or {}).get("verdict", {}).get("target_price")
        except (TypeError, ValueError, json.JSONDecodeError):
            tp = None
        plan = (r["entry_low"], r["entry_high"], r["stop_loss"], tp)
        if any(v is None for v in plan) or any(v <= 0 for v in plan):
            continue  # 计划不全(常见: 缺目标价或老推票缺买点) -> 不纳入回测
        picks.append({"trade_date": r["trade_date"], "slot": r["slot"],
                      "reviewer": r["reviewer"], "rank": r["rank"], "code": r["code"],
                      "name": r["name"], "entry_low": r["entry_low"],
                      "entry_high": r["entry_high"], "stop_loss": r["stop_loss"],
                      "target_price": tp, "picked_at": r["picked_at"],
                      "one_liner": r["one_liner"], "logic_stars": r["logic_stars"],
                      "action_stars": r["action_stars"], "position_tier": r["position_tier"]})
    return picks


def _daily_port_rets(trades: list) -> dict:
    """{date: 当日组合收益}: 资金在当日所有在场票间等权分摊, 日收益取算术平均。"""
    by_day = {}
    for t in trades:
        for d, r in (t.get("day_rets") or {}).items():
            by_day.setdefault(d, []).append(r)
    return {d: sum(rs) / len(rs) for d, rs in by_day.items()}


def _benchmark_day_rets(conn, from_date: str, to_date: str) -> dict:
    """基准组合日收益 {YYYY-MM-DD: ret}: 各成分指数 pct_chg 加权和(每日再平衡)。
    取各成分都有数的日期交集; 表缺列/缺表时返回 {} (基准指标整体降级为 None)。"""
    per = []
    for code, _w in BENCHMARK:
        try:
            rows = conn.execute(
                "SELECT trade_date, pct_chg FROM index_daily "
                "WHERE ts_code=? AND trade_date>=? AND trade_date<=?",
                (code, _d8(from_date), _d8(to_date))).fetchall()
        except sqlite3.OperationalError:
            return {}
        per.append({_ymd(td): pc / 100.0 for td, pc in rows if pc is not None})
    common = set(per[0]).intersection(*per[1:]) if per else set()
    return {d: sum(m[d] * w for m, (_c, w) in zip(per, BENCHMARK)) for d in common}


def _excess_ret(trades: list, bench_rets: dict):
    """超额收益(%) = 组合累计收益 - 基准同窗口累计收益。
    窗口 = 该组首个至最后一个有持仓的交易日(含); 基准在窗口内跨日复利。
    无入场记录或无基准数据返回 (None, None)。返回 (excess, bench_cum)。"""
    day_rets = _daily_port_rets(trades)
    if not day_rets or not bench_rets:
        return None, None
    lo, hi = min(day_rets), max(day_rets)
    nav = 1.0
    for d, r in bench_rets.items():
        if lo <= d <= hi:
            nav *= 1.0 + r
    bench_cum = round((nav - 1.0) * 100, 2)
    cum = _cum_ret(trades)
    return round(cum - bench_cum, 2), bench_cum


def _nav_stats(trades: list) -> dict:
    """组合净值曲线统计(同一口径一次遍历)。净值 = 每个交易日资金在当日所有
    在场票间等权分摊, 日组合收益取算术平均, 空仓日持平(现金), 跨日复利;
    持有中的票按最新收盘 mark-to-market 计入。
    cum_ret = 期末累计收益(%); cum_max/cum_min = 曲线最高/最低累计收益(%, 含期初 0,
    从未浮盈则 cum_max=0, 从未破本则 cum_min=0); max_drawdown = 峰谷最大回撤(%, 正数)。
    无任何入场记录时全为 None。"""
    day_rets = _daily_port_rets(trades)
    if not day_rets:
        return {"cum_ret": None, "cum_max": None, "cum_min": None, "max_drawdown": None}
    nav, hi, lo, peak, mdd = 1.0, 1.0, 1.0, 1.0, 0.0
    for d in sorted(day_rets):
        nav *= 1.0 + day_rets[d]
        hi, lo, peak = max(hi, nav), min(lo, nav), max(peak, nav)
        mdd = max(mdd, 1.0 - nav / peak)
    pc = lambda v: round((v - 1.0) * 100, 2)
    return {"cum_ret": pc(nav), "cum_max": pc(hi), "cum_min": pc(lo),
            "max_drawdown": round(mdd * 100, 2)}


def _cum_ret(trades: list):
    """组合口径期末累计收益(%), 详见 _nav_stats。"""
    return _nav_stats(trades)["cum_ret"]


def _max_drawdown(trades: list):
    """组合净值曲线最大回撤(%), 详见 _nav_stats。"""
    return _nav_stats(trades)["max_drawdown"]


def _curves(trades: list, bench_rets: dict = None) -> dict:
    """净值曲线(供前端画图): 日期轴 = 各组有持仓日期的并集(升序),
    series 按组(overall + 各 reviewer)给出逐日累计收益(%)。
    组内空仓日 NAV 持平(沿用前值), 该组首笔入场前为 None。
    有基准数据时附 series['benchmark'](全轴复利, 缺数日持平)。"""
    groups = {"overall": trades}
    for rv in sorted({t["reviewer"] for t in trades}):
        groups[rv] = [t for t in trades if t["reviewer"] == rv]
    day_rets = {g: _daily_port_rets(ts) for g, ts in groups.items()}
    axis = sorted(set().union(*day_rets.values())) if day_rets else []
    series = {}
    for g, m in day_rets.items():
        nav, started, vals = 1.0, False, []
        for d in axis:
            if d in m:
                started = True
                nav *= 1.0 + m[d]
            vals.append(round((nav - 1.0) * 100, 2) if started else None)
        series[g] = vals
    if bench_rets and axis:
        nav, vals = 1.0, []
        for d in axis:
            nav *= 1.0 + bench_rets.get(d, 0.0)
            vals.append(round((nav - 1.0) * 100, 2))
        series["benchmark"] = vals
    return {"dates": axis, "series": series, "benchmark_label": BENCHMARK_LABEL}


def _summarize(trades: list, bench_rets: dict = None) -> dict:
    total = len(trades)
    no_entry = sum(1 for t in trades if t["status"] == "no_entry")
    no_data = sum(1 for t in trades if t["status"] == "no_data")
    pending = sum(1 for t in trades if t["status"] == "pending")
    holding = sum(1 for t in trades if t["status"] == "holding")
    settled = [t for t in trades if t["status"] in ("target", "stop", "timeout")]
    win = sum(1 for t in settled if t["win"] is True)
    lose = sum(1 for t in settled if t["win"] is False and t["ret"] != 0)
    flat = sum(1 for t in settled if t["ret"] == 0)
    rets = [t["ret"] for t in settled if t["ret"] is not None]
    gains = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    win_rate = round(win / (win + lose) * 100, 1) if (win + lose) else None
    avg_ret = round(sum(rets) / len(rets) * 100, 2) if rets else None
    avg_gain = sum(gains) / len(gains) if gains else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    pf = round(avg_gain / avg_loss, 2) if losses else None
    max_gain = round(max(gains) * 100, 2) if gains else None   # 最大单次收益(已结算)
    max_loss = round(min(losses) * 100, 2) if losses else None  # 最大单次亏损(负值)
    excess, bench_cum = _excess_ret(trades, bench_rets or {})
    return {"total": total, "entered": total - no_entry - no_data - pending,
            "no_entry": no_entry, "no_data": no_data, "pending": pending, "holding": holding,
            "win": win, "lose": lose, "flat": flat,
            "win_rate": win_rate, "avg_ret": avg_ret, "profit_factor": pf,
            "max_gain": max_gain, "max_loss": max_loss,
            "excess_ret": excess, "bench_ret": bench_cum, **_nav_stats(trades)}


def evaluate_all(conn, reviewer: str = None, since: str = None) -> dict:
    """读 daily_pick 逐笔回测 + 按 reviewer/overall 聚合。"""
    days = perf.trading_days(conn)
    trades = [evaluate_pick(conn, p, days) for p in _load_picks(conn, reviewer, since)]
    active = sorted({d for t in trades for d in (t.get("day_rets") or {})})
    bench = _benchmark_day_rets(conn, active[0], active[-1]) if active else {}
    by_reviewer = {}
    for rv in sorted({t["reviewer"] for t in trades}):
        by_reviewer[rv] = _summarize([t for t in trades if t["reviewer"] == rv], bench)
    summary = {"by_reviewer": by_reviewer, "overall": _summarize(trades, bench)}
    curves = _curves(trades, bench)
    for t in trades:
        t.pop("day_rets", None)   # 仅供累计收益聚合, 不下发前端
    return {"trades": trades, "summary": summary, "curves": curves}
