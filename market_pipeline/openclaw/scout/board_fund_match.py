"""scout/board_fund_match.py — 主线板块 × 可买池基金 双测度匹配

不靠名称/分类映射，直接用两个客观测度判定"某只指数基金是不是某条主线板块的可投载体"：
  ① 成分重叠(结构): 基金跟踪指数的【全成分】里，落在主线板块成分内的【权重】之和
       —— 用 idx_constituents(18078 PIT 全成分+权重) ∩ 板块成分(stock_concept_map 东财)
  ② 走势相关(行为): 主线板块日收益 vs 基金净值日收益 的 Pearson 相关
       —— 用 concept_board_daily.pct_change vs fund_nav 日收益率

股票代码是两者唯一的通用连接键 —— 板块(东财BK)和指数(中证/国证)不必同分类。

用法:
  python3 board_fund_match.py --board BK0963.DC --date 20260602          # 商业航天
  python3 board_fund_match.py --board BK0963.DC --date 20260602 --min-overlap 0.3
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import zipfile
from statistics import mean, pstdev

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402

PIT_URL = "http://localhost:18078/mcp"
POOL_XLSX = "/home/rooot/agent_invest_lab/data/被动指数型基金池（权益黄金）.xlsx"
FUND_DB = os.getenv("FUND_DB_PATH", "/home/rooot/agent_invest_lab/data/fund.db")
CORR_WINDOW = 120          # 相关性回看交易日
MIN_CORR_N = 20            # 少于这么多对齐日不算相关
BARE = lambda s: (s or "").split(".")[0]   # noqa: E731  去后缀: 600879.SH -> 600879


# ── PIT 取数 (18078, 强制 simulated_datetime) ──────────────────────────
_LAST_CALL = [0.0]
MIN_INTERVAL = 0.7   # 全局最小调用间隔(秒): 低频, 绝不连发


def _pit_call(name: str, asof_dt: str, **args) -> dict:
    """单次 PIT 调用. 全局节流 + 熔断退避重试. 高频是大忌, 调用方应优先批量/缓存."""
    args["simulated_datetime"] = asof_dt
    for attempt in range(5):
        wait = MIN_INTERVAL - (time.time() - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        r = requests.post(PIT_URL,
                          headers={"Content-Type": "application/json",
                                   "Accept": "application/json, text/event-stream"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": args}}, timeout=60)
        _LAST_CALL[0] = time.time()
        out = json.loads(r.json()["result"]["content"][0]["text"])
        msg = out.get("message") or ""
        if "熔断" in msg or "频繁" in msg:
            time.sleep(3 + attempt * 3)  # 退避后重试
            continue
        return out
    return out


def _dash(d: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD (fund_nav 要带横杠)."""
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if "-" not in d else d


# ── 指数成分: 本地缓存(idx_constituents 不能批量, 故缓存; 成分季度才变, 长期复用) ──
def ensure_cache(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS idx_cons_cache("
        "index_code TEXT, stock_code TEXT, weight REAL, cons_date TEXT, cached_at TEXT,"
        "PRIMARY KEY(index_code, stock_code))")
    conn.commit()


def ingest_index_constituents(codes: list[str], asof_dt: str, conn,
                              ttl_days: int = 90) -> dict[str, int]:
    """把指数全成分灌进本地缓存. 只拉【缓存里没有 或 过期】的指数 —— 低频、可长期复用.
    返回 {状态: 数量}. 这是离线 ingest 步骤, 不应在每次匹配时跑全量."""
    ensure_cache(conn)
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=ttl_days)).isoformat()
    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT index_code FROM idx_cons_cache WHERE cached_at>=?", (cutoff,))}
    todo = [BARE(c) for c in codes if BARE(c) not in have]
    stat = {"已缓存": len(codes) - len(todo), "新拉取": 0, "空/非A股": 0}
    now = datetime.now().isoformat()
    for code in todo:
        r = _pit_call("idx_constituents", asof_dt, index_code=code)  # 已全局节流
        items = r.get("items", [])
        conn.execute("DELETE FROM idx_cons_cache WHERE index_code=?", (code,))
        if not items:
            stat["空/非A股"] += 1
            # negative cache: 空成分(非A股/无数据)也记一条 __EMPTY__ 标记,使其进入 have、
            # 不再每次 todo 重拉。否则空指数每轮都发一次 18078 调用(实测占 ingest 一半耗时)。
            # 成分季度才变,TTL 90 天到期后自然重拉一次确认。
            conn.execute("INSERT OR REPLACE INTO idx_cons_cache VALUES (?,?,?,?,?)",
                         (code, "__EMPTY__", 0.0, "", now))
            conn.commit()
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO idx_cons_cache VALUES (?,?,?,?,?)",
            [(code, BARE(x["成分证券代码"]), x.get("权重百分比") or 0.0,
              x.get("成分日期", ""), now) for x in items])
        conn.commit()
        stat["新拉取"] += 1
    return stat


def index_constituents_cached(index_code: str, conn) -> dict[str, float]:
    """从本地缓存读指数全成分 {裸股票码: 权重}. 零实时调用.
    过滤 __EMPTY__ negative-cache 标记行(空成分指数的占位,不是真成分)。"""
    return {sc: w for sc, w in conn.execute(
        "SELECT stock_code, weight FROM idx_cons_cache WHERE index_code=? AND stock_code!='__EMPTY__'",
        (BARE(index_code),))}


# ── 净值: 批量取(fund_nav 接受 fund_codes 列表, 一次多只, 减少调用次数) ──
def ensure_nav_cache(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fund_nav_cache("
        "fund_code TEXT, trade_date TEXT, ret REAL,"
        "PRIMARY KEY(fund_code, trade_date))")
    conn.commit()


def fund_returns_batch(fund_codes: list[str], asof_dt: str, start: str, end: str,
                       chunk: int = 25, conn=None) -> dict[str, dict[str, float]]:
    """批量基金日收益 {fund_code: {YYYYMMDD: 日收益率%}}. 分块, 每块一次调用.

    conn 提供时启用持久缓存(fund_nav_cache): 日收益率对某交易日是确定值(PIT, date<=asof
    不随 asof 变), 缓存后跨 run/跨日复用 → 把"每次重拉全部候选净值"(实测占单 board 一半
    耗时)降为只拉未覆盖到 end 的基金。命中判据: 该基金缓存里最大交易日距 end<=7 天(容忍
    asof 落在周末/节假日)。conn=None 时退回原逻辑(无缓存), 兼容 scout 其它调用。"""
    from datetime import datetime as _dt
    out: dict[str, dict[str, float]] = {}
    end_ymd = end.replace("-", "")[:8]
    start_ymd = start.replace("-", "")[:8]
    to_fetch = list(fund_codes)
    if conn is not None:
        ensure_nav_cache(conn)
        to_fetch = []
        ph = ",".join("?" * len(fund_codes))
        cached: dict[str, dict[str, float]] = {}
        if fund_codes:
            for fc, td, ret in conn.execute(
                f"SELECT fund_code, trade_date, ret FROM fund_nav_cache "
                f"WHERE fund_code IN ({ph}) AND trade_date>=? AND trade_date<=?",
                (*fund_codes, start_ymd, end_ymd)):
                cached.setdefault(fc, {})[td] = ret
        for fc in fund_codes:
            d = cached.get(fc)
            # 命中: 缓存覆盖到 end 附近(最大交易日距 end<=7 天)。否则需拉新增交易日。
            if d and abs((_dt.strptime(max(d), "%Y%m%d") - _dt.strptime(end_ymd, "%Y%m%d")).days) <= 7:
                out[fc] = d
            else:
                to_fetch.append(fc)
    for i in range(0, len(to_fetch), chunk):
        batch = to_fetch[i:i + chunk]
        r = _pit_call("fund_nav", asof_dt, fund_codes=batch,
                      start_date=_dash(start), end_date=_dash(end))
        for it in r.get("items", []):
            fc = it.get("基金代码")
            series = {rec["交易日期"].replace("-", ""): rec["日收益率"]
                      for rec in it.get("净值记录", []) if rec.get("日收益率") is not None}
            out[fc] = series
            if conn is not None and series:
                conn.executemany(
                    "INSERT OR REPLACE INTO fund_nav_cache VALUES (?,?,?)",
                    [(fc, td, ret) for td, ret in series.items()])
        if conn is not None:
            conn.commit()
    return out


# ── 板块数据 (scout DB, 东财概念板块) ──────────────────────────────────
def board_members(c, board_code: str) -> set[str]:
    return {BARE(r[0]) for r in c.execute(
        "SELECT ts_code FROM stock_concept_map "
        "WHERE board_code=? AND snapshot_date=(SELECT MAX(snapshot_date) FROM stock_concept_map)",
        (board_code,))}


def board_returns(c, board_code: str, asof: str, n: int = CORR_WINDOW) -> dict[str, float]:
    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM concept_board_daily "
        "WHERE board_code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT ?",
        (board_code, asof, n))]
    if not dates:
        return {}
    ph = ",".join("?" * len(dates))
    return {d: p for d, p in c.execute(
        f"SELECT trade_date, pct_change FROM concept_board_daily "
        f"WHERE board_code=? AND trade_date IN ({ph})", [board_code, *dates])
        if p is not None}


# ── 两个纯函数(可单测) ────────────────────────────────────────────────
def constituent_overlap(index_cons: dict[str, float], members: set[str]) -> dict:
    """指数全成分里落在板块内的权重占比. index_cons={股票码:权重%}, members=板块成分集合."""
    total = sum(index_cons.values())
    if total <= 0:
        return {"overlap_ratio": 0.0, "overlap_weight": 0.0, "n_common": 0, "n_index": len(index_cons)}
    common = [s for s in index_cons if s in members]
    inb = sum(index_cons[s] for s in common)
    return {"overlap_ratio": inb / total, "overlap_weight": round(inb, 1),
            "n_common": len(common), "n_index": len(index_cons)}


def return_correlation(board_ret: dict[str, float], fund_ret: dict[str, float],
                       min_n: int = MIN_CORR_N) -> dict:
    """板块日收益 vs 基金日收益 Pearson 相关 + beta + R²."""
    common = sorted(set(board_ret) & set(fund_ret))
    if len(common) < min_n:
        return {"corr": None, "beta": None, "r2": None, "n": len(common)}
    xs = [board_ret[d] for d in common]   # 板块
    ys = [fund_ret[d] for d in common]    # 基金
    n = len(common)
    mx, my = mean(xs), mean(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / n
    sx, sy = pstdev(xs), pstdev(ys)
    if sx == 0 or sy == 0:
        return {"corr": None, "beta": None, "r2": None, "n": n}
    corr = cov / (sx * sy)
    return {"corr": round(corr, 2), "beta": round(cov / (sx * sx), 2),
            "r2": round(corr * corr, 2), "n": n}


def verdict(overlap_ratio: float, corr: float | None) -> str:
    """分档。原则：相关性高即入选（哪怕重叠不高，作代理）；底线每条主线至少有产品。
    - 纯载体  : 重叠≥50% 且 相关≥0.8（同一批股 + 真的一起动）
    - 代理载体: 相关≥0.8 且 重叠<50%（走势跟得上、只是不够纯——可投的代理）
    - 弱代理  : 相关 0.6–0.8（次优，作兜底）
    - 非该主线: 相关<0.6（走势都不跟，真不相关）
    """
    if corr is None:
        return "数据不足"
    if overlap_ratio >= 0.5 and corr >= 0.8:
        return "✓纯载体"
    if corr >= 0.8:
        return "○代理载体"
    if corr >= 0.6:
        return "△弱代理"
    return "✗非该主线"


# ── 可买池 ──────────────────────────────────────────────────────────────
def load_pool(source: str = "fund_db", path: str = POOL_XLSX) -> list[dict]:
    """候选宇宙。默认 source='fund_db'：从 fund_db 取、只留【场外 C 份额载体】
    （名字尾 'C' —— 场外联接C 与 LOF-C 都算；纯场内 ETF 永不以 C 结尾、场外 A 排除）。
    跟踪指数码取 fund_info.track_index_code（算成分重叠的连接键）。
    source='xlsx' 走旧静态池逻辑（兜底/对照用，保留不删）。"""
    if source == "fund_db":
        return _load_pool_fund_db()
    z = zipfile.ZipFile(path)
    ss = re.findall(r"<t[^>]*>(.*?)</t>", z.read("xl/sharedStrings.xml").decode("utf-8", "ignore"), re.S)
    sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "ignore")
    out = []
    for rm in re.finditer(r"<row r=\"(\d+)\"[^>]*>(.*?)</row>", sheet, re.S):
        rn = int(rm.group(1))
        if rn == 1:
            continue
        cells = {}
        for cm in re.finditer(r"<c r=\"([A-Z]+)\d+\"(?:[^>]*t=\"(\w+)\")?[^>]*>(?:<v>(.*?)</v>)?", rm.group(2)):
            col, typ, val = cm.group(1), cm.group(2), cm.group(3)
            if val is None:
                continue
            cells[col] = ss[int(val)] if typ == "s" else val
        # A代码 B简称 I指数代码 J指数名 G规模 R入池
        # 注意: xlsx 把基金代码当数字存, 0/1 开头的场外基金会丢前导零 → 补齐 6 位
        if cells.get("A") and cells.get("I"):
            out.append({"fund": cells["A"].zfill(6), "name": cells.get("B", ""),
                        "index_code": cells["I"], "index_name": cells.get("J", ""),
                        "scale": float(cells.get("G") or 0), "tier": cells.get("R", "")})
    return out


def _load_pool_fund_db(db: str = FUND_DB) -> list[dict]:
    """从 fund_db 取候选宇宙，只留场外 C 份额载体（名字尾 'C' + 有跟踪指数码）。
    与 xlsx 版同 dict 形状：{fund,name,index_code,index_name,scale,tier}。
    index_code 取 track_index_code（去后缀由下游 BARE() 处理）；tier 仅展示用，留空。"""
    import sqlite3
    con = sqlite3.connect(db)
    out = []
    try:
        cur = con.execute(
            "SELECT fund_code, fund_name, track_index_code, track_index_name, scale "
            "FROM fund_info "
            "WHERE track_index_code IS NOT NULL AND track_index_code != '' "
            "  AND fund_name LIKE '%C'")
        for code, name, tic, tin, scale in cur:
            out.append({"fund": str(code).zfill(6), "name": name or "",
                        "index_code": tic, "index_name": tin or "",
                        "scale": float(scale or 0), "tier": ""})
    finally:
        con.close()
    return out


# ── 编排 ───────────────────────────────────────────────────────────────
def run(board_code: str, asof: str, min_overlap: float = 0.3) -> list[dict]:
    asof_dt = f"{asof[:4]}-{asof[4:6]}-{asof[6:]} 15:00:00"
    c = scout_db.conn()
    members = board_members(c, board_code)
    bret = board_returns(c, board_code, asof)
    bname = c.execute("SELECT board_name FROM concept_board_daily WHERE board_code=? LIMIT 1",
                      (board_code,)).fetchone()
    bname = bname[0] if bname else board_code
    print(f"主线板块: {bname}({board_code}) | 成分{len(members)}只 | 板块收益序列{len(bret)}日 | as-of {asof}")

    pool = load_pool()
    idx_codes = sorted({BARE(f["index_code"]) for f in pool})
    # ① 指数成分: 先确保本地缓存(只拉缺失的, 低频; 之后季度才需再 ingest)
    stat = ingest_index_constituents(idx_codes, asof_dt, c)
    print(f"可买池基金 {len(pool)} 只, 去重指数 {len(idx_codes)} 个; 成分缓存: {stat}")
    # ② 成分重叠全部从本地缓存算, 零实时调用
    ov_cache = {ic: constituent_overlap(index_constituents_cached(ic, c), members)
                for ic in idx_codes}
    # 候选门槛 = 成分重叠下限(只为框定"同板块邻域"的基金、省净值调用)；
    # 真正决定入选的是相关性(相关高→代理也入选)，故门槛设低(默认10%)。
    hit_idx = {ic: o for ic, o in ov_cache.items() if o["n_index"] and o["overlap_ratio"] >= min_overlap}
    print(f"  候选: 成分重叠 ≥{min_overlap:.0%} 的指数 {len(hit_idx)} 个")

    # ③ 候选基金 → 批量取净值算相关
    start = min(bret) if bret else asof
    cand = [f for f in pool if BARE(f["index_code"]) in hit_idx]
    print(f"  对应可买基金 {len(cand)} 只, 批量取净值算相关...")
    nav = fund_returns_batch([f["fund"] for f in cand], asof_dt, start, asof, conn=c)
    rows, dropped = [], []
    for f in cand:
        o = hit_idx[BARE(f["index_code"])]
        cor = return_correlation(bret, nav.get(f["fund"], {}))
        if cor["corr"] is None:
            dropped.append({**f, "corr_n": cor["n"]})  # 净值<120日窗口(成立不满半年)→剔除
            continue
        rows.append({**f, **{f"ov_{k}": v for k, v in o.items()},
                     "corr": cor["corr"], "beta": cor["beta"], "r2": cor["r2"], "corr_n": cor["n"],
                     "verdict": verdict(o["overlap_ratio"], cor["corr"])})
    if dropped:
        print(f"  剔除 {len(dropped)} 只(成立不满半年/净值<{MIN_CORR_N}日)")
    # 排序: 纯载体 > 代理 > 弱代理 > 非该主线; 同档按 相关 主、重叠 次(相关是入选主信号)
    rank = {"✓纯载体": 0, "○代理载体": 1, "△弱代理": 2, "✗非该主线": 3, "数据不足": 4}
    # 档内按 重叠×相关 排——既要走势跟得上, 又要尽量是同板块的股(压过纯宽基β)
    rows.sort(key=lambda r: (rank.get(r["verdict"], 9),
                             -(r["ov_overlap_ratio"] * (r["corr"] or 0))))
    # 底线: 每条主线最少要有产品。若常规候选(重叠≥门槛)里没有任何可投档,
    # 放宽——取重叠最高的 top15 指数(不卡门槛)再算一轮相关, 挑相关最高的作"兜底代理"。
    投档 = [r for r in rows if r["verdict"] in ("✓纯载体", "○代理载体", "△弱代理")]
    if not 投档:
        top_ic = sorted([ic for ic in idx_codes if ov_cache[ic]["n_index"]],
                        key=lambda ic: -ov_cache[ic]["overlap_ratio"])[:15]
        fb = [f for f in pool if BARE(f["index_code"]) in set(top_ic)]
        fbnav = fund_returns_batch([f["fund"] for f in fb], asof_dt, start, asof, conn=c)
        fbrows = []
        for f in fb:
            o = ov_cache[BARE(f["index_code"])]
            cor = return_correlation(bret, fbnav.get(f["fund"], {}))
            if cor["corr"] is None:
                continue
            fbrows.append({**f, **{f"ov_{k}": v for k, v in o.items()},
                           "corr": cor["corr"], "beta": cor["beta"], "r2": cor["r2"],
                           "corr_n": cor["n"], "verdict": "✗非该主线"})
        if fbrows:
            # 按 重叠×相关 挑, 让"同板块代理"压过"纯宽基β"(中证1000 这种)
            best = max(fbrows, key=lambda r: r["ov_overlap_ratio"] * (r["corr"] or 0))
            if (best["corr"] or 0) >= 0.5:
                best["verdict"] = "▽兜底代理"   # 重叠低、但走势最跟得上的可投代理
                rows = [r for r in rows if r["fund"] != best["fund"]] + [best]
                rank = {"✓纯载体": 0, "○代理载体": 1, "△弱代理": 2, "▽兜底代理": 3,
                        "✗非该主线": 4, "数据不足": 5}
                rows.sort(key=lambda r: (rank.get(r["verdict"], 9),
                                         -(r["corr"] or 0), -r["ov_overlap_ratio"]))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True, help="东财板块码, 如 BK0963.DC")
    ap.add_argument("--date", required=True, help="as-of 交易日 YYYYMMDD")
    ap.add_argument("--min-overlap", type=float, default=0.10)
    a = ap.parse_args()
    rows = run(a.board, a.date, a.min_overlap)
    print(f"\n{'判定':<12} {'基金':<22} {'指数':<14} 重叠%  相关  β    规模  入池")
    print("=" * 92)
    for r in rows[:25]:
        print(f"{r['verdict']:<12} {r['name'][:20]:<22} {r['index_name'][:12]:<14} "
              f"{r['ov_overlap_ratio']*100:4.0f}  {str(r['corr']):>5} {str(r['beta']):>5} "
              f"{r['scale']:6.1f} {r['tier']}")
    投 = [r for r in rows if r["verdict"] in ("✓纯载体", "○代理载体", "△弱代理", "▽兜底代理")]
    print(f"\n底线产品(每条主线≥1只): "
          + (f"{投[0]['name']} [{投[0]['verdict']}] 重叠{投[0]['ov_overlap_ratio']*100:.0f}%/相关{投[0]['corr']}"
             if 投 else "无任何相关基金(corr<0.5)——真无载体"))


if __name__ == "__main__":
    main()
