"""盘中采集器 — 拉实时行情+资金流, 算大势温度计/候选盯盘/板块实时强弱, 写 market.db.

设计: 单次调用拉完即退, 零常驻. 系统 cron 盘中每 3-5min 跑一次.
数据源 (实测 2026-05-22 盘中):
  ts.realtime_list(src='dc')                  全市场实时快照 5855行/7.6s
  ak.stock_individual_fund_flow_rank('今日')  全市场实时主力净流入 5288行/4.7s
  ts.realtime_quote('000001.SH,399006.SZ')    指数实时
板块实时强弱不额外取: 用成分股实时涨幅 join stock_concept_map 自算.

用法:
  python3 collect.py            # 盘中跑(非盘中自动 no-op 退出)
  python3 collect.py --force    # 无视时段强制跑一次(demo/盘后用昨收价)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/home/rooot/agent_invest_lab/market_pipeline/openclaw/workspace-bot11/scripts")  # config.py (token)
import scout_db  # noqa: E402
import s8_live  # noqa: E402
import logic  # noqa: E402  # 复用 enrich_one/selfheal
import lof_arb  # noqa: E402  # LOF 场内外套利

# S8 是盘中实时选(s8_live), 不读 {s}_candidates; S1~S7/S9 锚定昨日收盘候选
# 2026-06-02 按结构择时: S4 回踩战法已下线(不再生成候选), 从锚定列表移除; S1/S5/S6 保留(降为 observe)
# 2026-06-04 S9(大市值 regime 自适应因子选股)接入: 观察池形态(无日内买点, signal="—", 不进战绩)
ANCHORED = ["s1", "s2", "s3", "s5", "s6", "s7", "s9"]
STRATEGIES = ANCHORED + ["s8"]

MEMBER_TOP_N = 10   # 板块下钻每侧(领涨/领跌)落库条数

# regime -> 单票仓位上限(占常规仓位比例). 无 position 列时的静态映射.
REGIME_POSITION = {
    "强牛": 1.0, "强势震荡": 0.7, "中性震荡": 0.5, "弱势震荡": 0.3, "熊": 0.1,
}


def in_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 15 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)


def limit_pct(code: str, name: str) -> float:
    if "ST" in (name or "").upper():
        return 5.0
    if code.startswith(("8", "4", "920", "43")):
        return 30.0
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    return 10.0


def is_new(name: str) -> bool:
    return (name or "").startswith(("N", "C"))


def is_st(name) -> bool:
    """ST/退市风险股统一判定(与 strategy_common.is_st 同口径): 名称含 ST 或 退 即剔除.

    各战法剔 ST 的权威拦截点: s1/s2/s7 源表 name 存的是代码, 真名要到这里用实时行情
    rt.get('name') 才解析得到, 故所有锚定候选(含 S8 已自带过滤)统一在此处再过滤一道.
    """
    s = str(name or "")
    return "ST" in s.upper() or "退" in s


def is_limit_up(rt: dict) -> bool:
    """单只实时记录是否涨停: 现价贴涨停价(pre_close*(1+limit%))内 1 分钱."""
    if not rt.get("pre_close") or rt.get("price") is None or is_new(rt.get("name")):
        return False
    lim = limit_pct(rt["code"], rt["name"])
    lp = round(rt["pre_close"] * (1 + lim / 100), 2)
    return abs(rt["price"] - lp) < 0.011


def is_limit_down(rt: dict) -> bool:
    """单只实时记录是否跌停: 现价贴跌停价(pre_close*(1-limit%))内 1 分钱."""
    if not rt.get("pre_close") or rt.get("price") is None or is_new(rt.get("name")):
        return False
    lim = limit_pct(rt["code"], rt["name"])
    dp = round(rt["pre_close"] * (1 - lim / 100), 2)
    return abs(rt["price"] - dp) < 0.011


class HardTimeout(Exception):
    """SIGALRM 触发的硬超时, 区别于网络库自身的异常."""


@contextmanager
def hard_timeout(seconds: int, label: str = ""):
    """SIGALRM 硬超时兜底.

    tushare/akshare 底层走 requests/urllib3, urllib3 会显式把 socket timeout 设成 None(阻塞),
    覆盖 socket.setdefaulttimeout, 所以半开连接仍可能挂死几分钟拖垮 flock 后续轮次.
    用信号在主线程强制打断 (cron 单线程跑, 满足主线程前提). 仅整数秒粒度.
    """
    def _handler(signum, frame):
        raise HardTimeout(f"{label or '调用'} 超过 {seconds}s 硬超时")

    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def fetch_realtime():
    import socket
    # 注意: realtime_list 不是 pro 接口, 走 verify_token 校验官方端点, brze 代理用不了
    # 必须用官方 token, 不能用 config.TUSHARE_TOKEN (那是代理专属 token)
    from config import TUSHARE_TOKEN_OFFICIAL
    import tushare as ts
    ts.set_token(TUSHARE_TOKEN_OFFICIAL)
    socket.setdefaulttimeout(15)  # 兜底: 杜绝半开连接挂死几分钟拖累 flock 后续轮次
    last = None
    for attempt in range(3):  # 东方财富源盘中高峰偶发 connection reset, 瞬时错误重试自愈
        try:
            with hard_timeout(25, "realtime_list"):  # socket timeout 对 urllib3 无效, 信号兜底
                df = ts.realtime_list(src="dc")
            return df, ts
        except Exception as e:
            last = e
            print(f"  [realtime] 第{attempt + 1}次失败, {str(e)[:60]}")
            time.sleep(2 * (attempt + 1))
    raise last


# 指数实时涨幅: ts_code -> snapshot 列名
INDEX_CODES = {
    "000001.SH": "sh_pct",      # 上证指数
    "399001.SZ": "szcz_pct",    # 深证成指
    "399006.SZ": "gem_pct",     # 创业板指
    "000688.SH": "star50_pct",  # 科创50
    "899050.BJ": "bz50_pct",    # 北证50
    "000300.SH": "csi300_pct",  # 沪深300
    "000852.SH": "csi1000_pct",  # 中证1000
    # 中证2000(932000) 走东财 push2(见 fetch_csi2000_em)——tushare realtime_quote
    # 无 .CSI→东财 secid 映射, 各后缀/源均取不到
}


def fetch_index_pct(ts):
    out = {}
    try:
        with hard_timeout(15, "index_quote"):
            q = ts.realtime_quote(ts_code=",".join(INDEX_CODES))
        for _, r in q.iterrows():
            pre, px = r.get("PRE_CLOSE"), r.get("PRICE")
            col = INDEX_CODES.get(r["TS_CODE"])
            if col and pre and px:
                out[col] = round((px - pre) / pre * 100, 2)
    except Exception as e:
        print(f"  [index] 取指数失败(跳过): {str(e)[:80]}")
    return out


def parse_em_index_pct(payload):
    """解析东财 push2 stock/get 返回的指数涨跌%。

    f43=现价×100, f60=昨收×100；用比值消掉×100 缩放, 比信 f170 稳。
    payload 缺 data / f43 / f60(或 f60=0) → None。
    """
    d = (payload or {}).get("data")
    if not d:
        return None
    f43, f60 = d.get("f43"), d.get("f60")
    if f43 is None or not f60:
        return None
    return round((f43 - f60) / f60 * 100, 2)


def fetch_csi2000_em():
    """中证2000(932000) 盘中涨跌%走东财 push2 secid=2.932000。

    tushare realtime_quote 的 dc 源本质爬东财, 但无 .CSI→secid 映射(会拼成
    市场1=错), 取不到; 这里直接用正确的 secid=2.932000(市场2)。
    用 http(非 https): 本机到 push2 的 HTTPS 握手会超时, http 正常。
    不走系统代理(7897 会劈坏国内财经 API SSL)。任何失败 → None(列留空)。
    """
    import json as _json
    import urllib.request
    url = ("http://push2.eastmoney.com/api/qt/stock/get"
           "?secid=2.932000&fields=f43,f60,f170")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with hard_timeout(10, "csi2000_em"):
            with opener.open(req, timeout=8) as resp:
                payload = _json.loads(resp.read().decode("utf-8"))
        return parse_em_index_pct(payload)
    except Exception as e:
        print(f"  [index] 中证2000 东财取失败(跳过): {str(e)[:80]}")
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_quote_fallback(ts, ts_codes):
    """realtime_list 偶发漏掉的候选, 用 realtime_quote 单独补 (price/pct/high/low/amount).

    realtime_quote 无 vol_ratio/turnover/5min, 这些字段保持 None; 但 price/涨幅/信号灯能算出来,
    避免候选在某一帧里显示"无数据"(实测 dc 全市场快照会随机丢几只票).
    """
    out = {}
    if not ts_codes:
        return out
    try:
        with hard_timeout(15, "quote_fallback"):
            q = ts.realtime_quote(ts_code=",".join(ts_codes))
        for _, r in q.iterrows():
            pre, px = r.get("PRE_CLOSE"), r.get("PRICE")
            pct = round((px - pre) / pre * 100, 2) if (pre and px) else None
            amt = float(r.get("AMOUNT") or 0)
            out[r["TS_CODE"]] = {
                "code": str(r["TS_CODE"]).split(".")[0], "name": r.get("NAME"),
                "price": px, "pct": pct, "high": r.get("HIGH"), "low": r.get("LOW"),
                "pre_close": pre, "vol_ratio": None, "turnover": None,
                "amount": round(amt / 1e8, 3), "amount_raw": amt, "speed_5min": None,
            }
    except Exception as e:
        print(f"  [quote-fallback] 失败(跳过): {str(e)[:80]}")
    return out


def candidate_ts_codes(c, anchor, pick_codes=(), er_ts_codes=()):
    """昨日候选 + 今日 pick + 业绩共振名单 的 ts_code 全集 (带后缀).
    用于 realtime_list 漏抓时的 quote-fallback."""
    out = set()
    for s in ANCHORED:
        try:
            for (code,) in c.execute(f"SELECT code FROM {s}_candidates WHERE date=?", (anchor,)):
                out.add(scout_db.to_suffix(str(code).zfill(6)))
        except Exception:
            pass
    for code in pick_codes:
        out.add(scout_db.to_suffix(str(code).zfill(6)))
    for ts in er_ts_codes:
        out.add(ts)  # 已带后缀,直接加
    return out


def earnings_ts_codes(c):
    """业绩共振当日名单的 ts_code 集合. 取 earnings_resonance_daily 表最新 trade_date 一批."""
    try:
        r = c.execute("SELECT MAX(trade_date) FROM earnings_resonance_daily").fetchone()
    except sqlite3.OperationalError:
        return set()
    if not r or not r[0]:
        return set()
    td = r[0]
    return {row[0] for row in c.execute(
        "SELECT ts_code FROM earnings_resonance_daily WHERE trade_date=?", (td,))}


def latest_picks(c, today: str):
    """今日 daily_pick 最新 slot 的 pick 行 (跨 reviewer 去重 code, 保留先出现的一条).
    slot 优先级: intraday_pm > intraday_am > premarket. 表缺失/无今日 pick 返回 []."""
    try:
        latest = c.execute(
            "SELECT slot FROM daily_pick WHERE trade_date=? "
            "ORDER BY CASE slot WHEN 'intraday_pm' THEN 3 WHEN 'intraday_am' THEN 2 "
            "ELSE 1 END DESC LIMIT 1", (today,)).fetchone()
    except Exception:
        return []
    if not latest:
        return []
    rows = c.execute(
        "SELECT code, name, entry_low, entry_high, stop_loss, deep_research_json "
        "FROM daily_pick WHERE trade_date=? AND slot=? ORDER BY reviewer, rank",
        (today, latest[0])).fetchall()
    out, seen = [], set()
    for r in rows:
        d = dict(r)
        code = str(d["code"]).zfill(6)
        if code in seen:
            continue
        seen.add(code)
        try:
            dr = json.loads(d.get("deep_research_json") or "{}")
            target = (dr.get("verdict") or {}).get("target_price")
        except Exception:
            target = None
        d["code"] = code
        d["target_1"] = target
        out.append(d)
    return out


def fetch_fundflow():
    """全市场实时主力净流入: bare_code -> 净额(万元). 净额接口已是元, /1e4 转万元."""
    try:
        import akshare as ak
        with hard_timeout(20, "fundflow"):
            df = ak.stock_individual_fund_flow_rank(indicator="今日")
        col = "今日主力净流入-净额"
        out = {}
        for _, r in df.iterrows():
            v = _num(r[col])
            out[str(r["代码"]).zfill(6)] = (v / 1e4) if v is not None else None
        return out
    except Exception as e:
        print(f"  [fundflow] 取资金流失败(跳过): {str(e)[:80]}")
        return {}


def latest_regime(c):
    r = c.execute(
        "SELECT regime_code, regime_name FROM regime_classify_daily ORDER BY trade_date DESC LIMIT 1"
    ).fetchone()
    if not r:
        return None, None, None
    name = r["regime_name"]
    return r["regime_code"], name, REGIME_POSITION.get(name, 0.5)


def compute_gauge(rt_rows):
    up = down = flat = lu = ld = blast = 0
    amount_sum = 0.0
    for r in rt_rows:
        pct = r["pct"]
        if pct is None:
            continue
        amount_sum += (r["amount_raw"] or 0)
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        if is_new(r["name"]) or not r["pre_close"]:
            continue
        lim = limit_pct(r["code"], r["name"])
        lp = round(r["pre_close"] * (1 + lim / 100), 2)
        dp = round(r["pre_close"] * (1 - lim / 100), 2)
        price, high, low = r["price"], r["high"], r["low"]
        if price and abs(price - lp) < 0.011:
            lu += 1
        elif high and high >= lp - 0.011 and price and price < lp - 0.011:
            blast += 1
        if price and abs(price - dp) < 0.011:
            ld += 1
    return dict(up=up, down=down, flat=flat, limit_up=lu, limit_down=ld,
                blast=blast, amount=round(amount_sum / 1e8, 1))


def _member_chip(x):
    """成分元组 (pct,code,name,price,lu,ld,vol_ratio,turnover,net_main,total_mv,float_mv) -> 前端下钻 chip dict.
    vr=量比 tr=换手 mf=主力净流入(万元) tmv=总市值(亿) fmv=流通市值(亿)."""
    return {"c": x[1], "n": x[2], "p": x[3], "pct": round(x[0], 2),
            "vr": x[6], "tr": x[7], "mf": (round(x[8]) if x[8] is not None else None),
            "tmv": x[9], "fmv": x[10]}


def compute_boards(c, rt_by_ts, fundflow, snapshot_time):
    """成分股实时涨幅 -> 板块实时强弱(全量落库). 每板块附涨停家数 + 领涨股(实时涨幅最高成分).
    顺手把成分个股清单(涨停/跌停/领涨/领跌)写 intraday_board_members 供前端点开下钻.
    返回 {board_code: (name, live_pct, member)} 给候选 top_theme 用."""
    name_map = {row["board_code"]: row["board_name"] for row in c.execute(
        "SELECT board_code, board_name FROM concept_board_daily "
        "WHERE trade_date=(SELECT MAX(trade_date) FROM concept_board_daily)")}
    # board_code -> {sum,m,up,lu,ld,amt(元), mem:[(pct,code,name,price,lu_flag,ld_flag)]}
    agg = {}
    for bc, ts_code in c.execute("SELECT board_code, ts_code FROM stock_concept_map"):
        rt = rt_by_ts.get(ts_code)
        if not rt or rt["pct"] is None:
            continue
        a = agg.get(bc)
        if a is None:
            a = agg[bc] = {"sum": 0.0, "m": 0, "up": 0, "lu": 0, "ld": 0, "amt": 0.0, "mem": []}
        luf, ldf = is_limit_up(rt), is_limit_down(rt)
        a["sum"] += rt["pct"]
        a["m"] += 1
        if rt["pct"] > 0:
            a["up"] += 1
        if luf:
            a["lu"] += 1
        if ldf:
            a["ld"] += 1
        a["amt"] += (rt.get("amount_raw") or 0)
        a["mem"].append((rt["pct"], rt["code"], rt["name"], rt["price"], luf, ldf,
                         rt.get("vol_ratio"), rt.get("turnover"), fundflow.get(rt["code"]),
                         rt.get("total_mv"), rt.get("float_mv")))

    boards, member_rows = [], []
    for bc, a in agg.items():
        if a["m"] < 3:
            continue
        mem = sorted(a["mem"], key=lambda x: x[0], reverse=True)  # pct 降序; 首位=领涨
        lpct, lcode, lname, lprice = mem[0][0], mem[0][1], mem[0][2], mem[0][3]
        boards.append((bc, name_map.get(bc, bc), round(a["sum"] / a["m"], 2), a["m"], a["up"],
                       a["lu"], lcode, lname, round(lpct, 2) if lpct is not None else None,
                       lprice, a["ld"], round(a["amt"] / 1e8, 2)))
        member_rows.append((
            bc, snapshot_time,
            json.dumps([_member_chip(x) for x in mem if x[4]], ensure_ascii=False),         # 涨停
            json.dumps([_member_chip(x) for x in mem if x[5]], ensure_ascii=False),         # 跌停
            json.dumps([_member_chip(x) for x in mem[:MEMBER_TOP_N]], ensure_ascii=False),  # 领涨
            json.dumps([_member_chip(x) for x in mem[-MEMBER_TOP_N:][::-1]],                # 领跌(pct升序)
                       ensure_ascii=False),
            json.dumps([_member_chip(x) for x in mem], ensure_ascii=False),                 # 全成分(当前帧)
        ))
    boards.sort(key=lambda x: x[2], reverse=True)
    rows = [(snapshot_time, bc, bn, lp, m, u, rank, lu, lcode, lname, lpct, lprice, ld, amt_yi)
            for rank, (bc, bn, lp, m, u, lu, lcode, lname, lpct, lprice, ld, amt_yi)
            in enumerate(boards, 1)]
    if rows:
        c.executemany(
            "INSERT OR REPLACE INTO intraday_board VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    # 个股下钻: 只保留当前帧, 整表覆盖
    c.execute("DELETE FROM intraday_board_members")
    if member_rows:
        c.executemany(
            "INSERT OR REPLACE INTO intraday_board_members VALUES (?,?,?,?,?,?,?)", member_rows)
    # board_code -> (name, live_pct, member), 给候选 top_theme 用
    return ({bc: (bn, lp, m) for bc, bn, lp, m, u, lu, lcode, lname, lpct, lprice, ld, amt_yi in boards},
            name_map)


def candidate_signal(price, lo, hi, stop, t1, t2):
    if price is None:
        return "无数据", None
    if stop and price <= stop:
        return "跌破止损", None
    if t2 and price >= t2:
        return "摸止盈2", None
    if t1 and price >= t1:
        return "摸止盈1", None
    if lo and hi and lo <= price <= hi:
        return "进区间", 0.0
    if hi and price > hi:
        return "已突破", round((price - hi) / hi * 100, 2)
    if lo and price < lo:
        return "待入场", round((price - lo) / lo * 100, 2)
    return "—", None


def collect_candidates(c, rt_by_ts, rt_by_code, fundflow, board_live, snapshot_time, anchor):
    rows = []
    for s in ANCHORED:
        try:
            recs = c.execute(
                f"SELECT * FROM {s}_candidates WHERE date=?", (anchor,)
            ).fetchall()
        except Exception as e:
            print(f"  [{s}] 候选查询失败: {str(e)[:60]}")
            continue
        for row in recs:
            r = dict(row)  # 各策略 schema 不一, 用 dict.get 容错
            code = str(r["code"]).zfill(6)
            ts_code = scout_db.to_suffix(code)
            rt = rt_by_ts.get(ts_code) or rt_by_code.get(code) or {}
            name = r["name"] if r.get("name") and r["name"] != code else rt.get("name")
            if is_st(name):  # 各战法剔 ST/退市: 此处真名已解析, 兜住 s1/s2/s7 源表只存代码的情况
                continue
            price = rt.get("price")
            # 目标列各策略不同: s5=target_1/2_price, s7=take_profit_price, 其余无
            t1 = r.get("target_1_price") or r.get("take_profit_price")
            t2 = r.get("target_2_price")
            sig, dist = candidate_signal(price, r.get("entry_zone_low"), r.get("entry_zone_high"),
                                         r.get("stop_loss_price"), t1, t2)
            # top_theme: 候选所属板块中今日(实时)最强的一个
            top_theme, top_pct = None, None
            for (bc,) in c.execute("SELECT board_code FROM stock_concept_map WHERE ts_code=?", (ts_code,)):
                bl = board_live.get(bc)
                if bl and bl[2] >= 5 and (top_pct is None or bl[1] > top_pct):
                    top_theme, top_pct = bl[0], bl[1]
            rows.append((
                snapshot_time, s, code, ts_code, name, r["industry"], r["date"],
                price, rt.get("pct"), rt.get("vol_ratio"), rt.get("turnover"),
                rt.get("amount"), rt.get("speed_5min"), fundflow.get(code),
                r.get("entry_zone_low"), r.get("entry_zone_high"), r.get("stop_loss_price"),
                t1, t2, r.get("position_pct"),
                dist, sig, top_theme, top_pct,
            ))
    if rows:
        c.executemany(
            "INSERT OR REPLACE INTO intraday_candidate_live "
            "(snapshot_time, strategy, code, ts_code, name, industry, cand_date, "
            " price, pct, vol_ratio, turnover_rate, amount, speed_5min, net_main, "
            " entry_low, entry_high, stop_loss, target_1, target_2, position_pct, "
            " dist_to_entry, signal, top_theme, top_theme_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def collect_picks(c, rt_by_ts, rt_by_code, fundflow, board_live, snapshot_time, today, picks):
    """今日 daily_pick → 伪策略 'dp' 写入 intraday_candidate_live, 让前端推荐池卡片拿到实时行情.
    pick 不在 S1~S7/S8 候选时仍可被采集. industry 留 None (daily_pick 表未存)."""
    rows = []
    for p in picks:
        code = p["code"]
        ts_code = scout_db.to_suffix(code)
        rt = rt_by_ts.get(ts_code) or rt_by_code.get(code) or {}
        name = p.get("name") or rt.get("name")
        price = rt.get("price")
        t1 = p.get("target_1")
        sig, dist = candidate_signal(price, p.get("entry_low"), p.get("entry_high"),
                                     p.get("stop_loss"), t1, None)
        top_theme, top_pct = None, None
        for (bc,) in c.execute("SELECT board_code FROM stock_concept_map WHERE ts_code=?", (ts_code,)):
            bl = board_live.get(bc)
            if bl and bl[2] >= 5 and (top_pct is None or bl[1] > top_pct):
                top_theme, top_pct = bl[0], bl[1]
        rows.append((
            snapshot_time, "dp", code, ts_code, name, None, today,
            price, rt.get("pct"), rt.get("vol_ratio"), rt.get("turnover"),
            rt.get("amount"), rt.get("speed_5min"), fundflow.get(code),
            p.get("entry_low"), p.get("entry_high"), p.get("stop_loss"),
            t1, None, None,
            dist, sig, top_theme, top_pct,
        ))
    if rows:
        c.executemany(
            "INSERT OR REPLACE INTO intraday_candidate_live "
            "(snapshot_time, strategy, code, ts_code, name, industry, cand_date, "
            " price, pct, vol_ratio, turnover_rate, amount, speed_5min, net_main, "
            " entry_low, entry_high, stop_loss, target_1, target_2, position_pct, "
            " dist_to_entry, signal, top_theme, top_theme_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def collect_earnings(c, rt_by_ts, snapshot_time, today, earnings_codes):
    """业绩共振名单 → 伪策略 'er' 写入 intraday_candidate_live, 供前端 tab 分钟刷新.

    只写 rt_by_ts 命中的票, 缺价的跳过 (由 quote-fallback 兜底后再次调用). 策略专用
    字段(entry_low/stop_loss/target_1 等)全 NULL, 板块 top_theme 也留 NULL, 因为
    业绩共振 tab 不需要板块热度指标 (已按主线板块归类)."""
    rows = []
    for ts_code in earnings_codes:
        rt = rt_by_ts.get(ts_code)
        if not rt:
            continue
        code = rt.get("code") or ts_code.split(".")[0]
        rows.append((
            snapshot_time, "er", code, ts_code, rt.get("name"), None, today,
            rt.get("price"), rt.get("pct"), rt.get("vol_ratio"), rt.get("turnover"),
            rt.get("amount"), rt.get("speed_5min"), None,
            None, None, None,        # entry_low / entry_high / stop_loss
            None, None, None,        # target_1 / target_2 / position_pct
            None, None, None, None,  # dist_to_entry / signal / top_theme / top_theme_pct
            rt.get("low"), rt.get("high"), rt.get("pre_close"),  # low_px / high_px / pre_close_px
        ))
    if rows:
        c.executemany(
            "INSERT OR REPLACE INTO intraday_candidate_live "
            "(snapshot_time, strategy, code, ts_code, name, industry, cand_date, "
            " price, pct, vol_ratio, turnover_rate, amount, speed_5min, net_main, "
            " entry_low, entry_high, stop_loss, target_1, target_2, position_pct, "
            " dist_to_entry, signal, top_theme, top_theme_pct, "
            " low_px, high_px, pre_close_px) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def collect_holdings(c, rt_by_code, snapshot_time):
    """为 follow_holding 里每只持仓写一行 strategy='hold' 的实时行情到 intraday_candidate_live.

    全市场快照 rt_by_code 已覆盖任意代码; 缺价的(已退市/接口漏抓)跳过, 不写空行
    (server 端会回落 daily 收盘价). 返回写入条数. 表未建 → 兜底 0.
    """
    try:
        # 同 code 多笔(每次跟买独立成笔)只需写一行实时行情, 按 code 去重
        holds = c.execute(
            "SELECT code, MAX(name) AS name FROM follow_holding GROUP BY code").fetchall()
    except Exception:
        return 0
    rows = []
    for r in holds:
        code = str(r["code"]).zfill(6)
        rt = rt_by_code.get(code)
        if not rt or rt.get("price") is None:
            continue
        rows.append((
            snapshot_time, "hold", code, scout_db.to_suffix(code),
            rt.get("name") or r["name"], None, None,
            rt.get("price"), rt.get("pct"), rt.get("vol_ratio"),
            rt.get("turnover"), rt.get("amount"), rt.get("speed_5min"), None,
            None, None, None, None, None, None, None, None, None, None))
    if rows:
        c.executemany(
            "INSERT OR REPLACE INTO intraday_candidate_live "
            "(snapshot_time, strategy, code, ts_code, name, industry, cand_date, "
            " price, pct, vol_ratio, turnover_rate, amount, speed_5min, net_main, "
            " entry_low, entry_high, stop_loss, target_1, target_2, position_pct, "
            " dist_to_entry, signal, top_theme, top_theme_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


BUY_TRIGGER_SIGNALS = ("进区间", "已突破")


def _upsert_trigger(c, td, r, snapshot_time, s8_meta):
    """单条候选 → intraday_trigger_log upsert. S8 首现即触发(买点=首现价); S1~S7 进区间/已突破触发."""
    s, code, price, sig = r["strategy"], r["code"], r["price"], r["signal"]
    is_trigger = (s == "s8") or (sig in BUY_TRIGGER_SIGNALS)
    meta = s8_meta.get(code, {}) if s == "s8" else {}
    ex = c.execute(
        "SELECT triggered, entry_price, max_ret, min_ret FROM intraday_trigger_log "
        "WHERE trade_date=? AND strategy=? AND code=?", (td, s, code)
    ).fetchone()
    if ex is None:
        trig = 1 if (is_trigger and price) else 0
        entry = price if trig else None
        ret = 0.0 if trig else None
        c.execute(
            "INSERT OR REPLACE INTO intraday_trigger_log "
            "(trade_date, strategy, code, ts_code, name, industry, first_seen_time, "
            "entry_low, entry_high, stop_loss, target_1, target_2, position_pct, "
            "triggered, trigger_time, trigger_signal, entry_price, last_time, last_price, "
            "ret_pct, max_ret, min_ret, still_candidate, tone, path, mainline) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (td, s, code, r["ts_code"], r["name"], r["industry"], snapshot_time,
             r["entry_low"], r["entry_high"], r["stop_loss"], r["target_1"], r["target_2"],
             r["position_pct"], trig, snapshot_time if trig else None, sig if trig else None,
             entry, snapshot_time, price, ret, ret, ret, 1,
             meta.get("tone"), meta.get("path"), meta.get("mainline")))
        return
    triggered, entry, mx, mn = ex["triggered"], ex["entry_price"], ex["max_ret"], ex["min_ret"]
    if not triggered and is_trigger and price:
        c.execute(
            "UPDATE intraday_trigger_log SET triggered=1, trigger_time=?, trigger_signal=?, "
            "entry_price=?, last_time=?, last_price=?, ret_pct=0.0, max_ret=0.0, min_ret=0.0, "
            "still_candidate=1 WHERE trade_date=? AND strategy=? AND code=?",
            (snapshot_time, sig, price, snapshot_time, price, td, s, code))
    elif triggered and entry and price:
        ret = round((price - entry) / entry * 100, 2)
        nmx = max(mx, ret) if mx is not None else ret
        nmn = min(mn, ret) if mn is not None else ret
        c.execute(
            "UPDATE intraday_trigger_log SET last_time=?, last_price=?, ret_pct=?, "
            "max_ret=?, min_ret=?, still_candidate=1 WHERE trade_date=? AND strategy=? AND code=?",
            (snapshot_time, price, ret, nmx, nmn, td, s, code))
    else:
        c.execute(
            "UPDATE intraday_trigger_log SET last_time=?, last_price=?, still_candidate=1 "
            "WHERE trade_date=? AND strategy=? AND code=?",
            (snapshot_time, price, td, s, code))


def update_trigger_log(c, snapshot_time, td, rt_by_code, s8_meta):
    """全策略触发记录+滚动浮盈. 本帧候选逐条 upsert; 已触发但本帧掉出池(主要 S8)的继续滚动浮盈, still=0.
    dp(推荐池) / hold(用户持仓) 均为伪策略, 不进 trigger_log: 它们各自单独展示, 不混入 S1~S8 复盘."""
    cur = c.execute(
        "SELECT * FROM intraday_candidate_live WHERE snapshot_time=? AND strategy NOT IN ('dp', 'hold')",
        (snapshot_time,)
    ).fetchall()
    seen = set()
    for r in cur:
        seen.add((r["strategy"], r["code"]))
        _upsert_trigger(c, td, r, snapshot_time, s8_meta)
    for er in c.execute(
        "SELECT strategy, code, triggered, entry_price, max_ret, min_ret "
        "FROM intraday_trigger_log WHERE trade_date=?", (td,)
    ).fetchall():
        if (er["strategy"], er["code"]) in seen:
            continue
        if er["triggered"] and er["entry_price"]:
            rt = rt_by_code.get(er["code"])
            price = rt.get("price") if rt else None
            if price:
                ret = round((price - er["entry_price"]) / er["entry_price"] * 100, 2)
                mx = max(er["max_ret"], ret) if er["max_ret"] is not None else ret
                mn = min(er["min_ret"], ret) if er["min_ret"] is not None else ret
                c.execute(
                    "UPDATE intraday_trigger_log SET last_time=?, last_price=?, ret_pct=?, "
                    "max_ret=?, min_ret=?, still_candidate=0 "
                    "WHERE trade_date=? AND strategy=? AND code=?",
                    (snapshot_time, price, ret, mx, mn, td, er["strategy"], er["code"]))
                continue
        c.execute(
            "UPDATE intraday_trigger_log SET still_candidate=0 "
            "WHERE trade_date=? AND strategy=? AND code=?", (td, er["strategy"], er["code"]))


def carry_forward_s8(c, snapshot_time, today_iso, rt_by_code):
    """fundflow 抓取失败时的兜底: 沿用上一帧今日 S8 候选, 重盖 snapshot_time, 用本帧实时
    行情刷新现价/涨幅(net_main 无源故保留上帧). 避免资金流接口抽风把整池误标掉出池。
    返回 (24列元组列表, s8_meta, 条数); 无上帧可沿用则返回空。"""
    prev = c.execute(
        "SELECT MAX(snapshot_time) FROM intraday_candidate_live "
        "WHERE strategy='s8' AND cand_date=? AND snapshot_time<?",
        (today_iso, snapshot_time)).fetchone()[0]
    if not prev:
        return [], {}, 0
    rows, meta = [], {}
    for r in c.execute(
            "SELECT * FROM intraday_candidate_live WHERE strategy='s8' AND snapshot_time=?",
            (prev,)):
        d = dict(r)
        rt = rt_by_code.get(d["code"]) or {}

        def pick(rk, dk):
            v = rt.get(rk)
            return v if v is not None else d[dk]

        rows.append((
            snapshot_time, "s8", d["code"], d["ts_code"], d["name"], d["industry"], today_iso,
            pick("price", "price"), pick("pct", "pct"), pick("vol_ratio", "vol_ratio"),
            pick("turnover", "turnover_rate"), pick("amount", "amount"),
            pick("speed_5min", "speed_5min"), d["net_main"],
            d["entry_low"], d["entry_high"], d["stop_loss"], d["target_1"], d["target_2"],
            d["position_pct"], d["dist_to_entry"], d["signal"], d["top_theme"],
            d["top_theme_pct"]))
        meta[d["code"]] = {"tone": None, "path": None, "mainline": d["top_theme"]}
    return rows, meta, len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="无视盘中时段强制跑")
    a = ap.parse_args()
    now = datetime.now()
    if not a.force and not in_market_hours(now):
        print(f"[scout-collect] {now:%H:%M} 非盘中, no-op 退出")
        return
    t0 = time.time()
    scout_db.init_schema()
    print(f"[scout-collect] {now:%Y-%m-%d %H:%M:%S} 开始采集...")

    df, ts = fetch_realtime()
    rt_by_ts, rt_by_code, rt_rows = {}, {}, []
    for _, r in df.iterrows():
        tmv, fmv = _num(r.get("TOTAL_MV")), _num(r.get("FLOAT_MV"))  # 元 -> 亿元
        rec = {
            "code": str(r["TS_CODE"]).split(".")[0], "name": r["NAME"],
            "price": r["PRICE"], "pct": r["PCT_CHANGE"], "high": r["HIGH"], "low": r["LOW"],
            "pre_close": r["CLOSE"], "vol_ratio": r["VOL_RATIO"], "turnover": r["TURNOVER_RATE"],
            "amount": round((r["AMOUNT"] or 0) / 1e8, 3), "amount_raw": r["AMOUNT"],
            "speed_5min": r.get("5MIN"),
            "total_mv": (round(tmv / 1e8, 1) if tmv else None),
            "float_mv": (round(fmv / 1e8, 1) if fmv else None),
        }
        rt_by_ts[r["TS_CODE"]] = rec
        rt_by_code[rec["code"]] = rec
        rt_rows.append(rec)
    print(f"  realtime_list: {len(rt_rows)} 行  ({time.time()-t0:.1f}s)")

    fundflow = fetch_fundflow()
    print(f"  fundflow: {len(fundflow)} 票")
    idx = fetch_index_pct(ts)
    idx["csi2000_pct"] = fetch_csi2000_em()  # 中证2000 走东财 push2(tushare 取不到)

    snapshot_time = now.strftime("%Y-%m-%d %H:%M:%S")
    trade_date = now.strftime("%Y%m%d")
    c = scout_db.conn()
    rcode, rname, plimit = latest_regime(c)
    g = compute_gauge(rt_rows)
    c.execute(
        "INSERT OR REPLACE INTO intraday_snapshot "
        "(snapshot_time, trade_date, up_count, down_count, flat_count, limit_up, "
        "limit_down, blast_count, total_amount, sh_pct, gem_pct, szcz_pct, star50_pct, "
        "bz50_pct, csi300_pct, csi1000_pct, csi2000_pct, regime_code, regime_name, "
        "position_limit, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (snapshot_time, trade_date, g["up"], g["down"], g["flat"], g["limit_up"],
         g["limit_down"], g["blast"], g["amount"],
         idx.get("sh_pct"), idx.get("gem_pct"), idx.get("szcz_pct"),
         idx.get("star50_pct"), idx.get("bz50_pct"), idx.get("csi300_pct"),
         idx.get("csi1000_pct"), idx.get("csi2000_pct"),
         rcode, rname, plimit, now.isoformat()))
    board_live, _ = compute_boards(c, rt_by_ts, fundflow, snapshot_time)
    anchor = scout_db.anchor_date(c, now.strftime("%Y-%m-%d"))
    today_iso = now.strftime("%Y-%m-%d")
    picks = latest_picks(c, today_iso)
    hold_codes = [r[0] for r in c.execute("SELECT code FROM follow_holding")]
    er_codes = earnings_ts_codes(c)
    missing = [tc for tc in candidate_ts_codes(c, anchor,
                                               [p["code"] for p in picks] + hold_codes,
                                               er_ts_codes=er_codes)
               if tc not in rt_by_ts]
    if missing:
        fb = fetch_quote_fallback(ts, missing)
        for tc, rec in fb.items():
            rt_by_ts[tc] = rec
            rt_by_code[rec["code"]] = rec
        print(f"  quote-fallback: 候选缺 {len(missing)} 只, 补回 {len(fb)} 只")
    n_cand = collect_candidates(c, rt_by_ts, rt_by_code, fundflow, board_live, snapshot_time, anchor)
    n_pick = collect_picks(c, rt_by_ts, rt_by_code, fundflow, board_live, snapshot_time, today_iso, picks)
    n_hold = collect_holdings(c, rt_by_code, snapshot_time)
    n_er = collect_earnings(c, rt_by_ts, snapshot_time, today_iso, er_codes)

    # S8 盘中实时选股 (不锚定昨日, 用实时口径重算)
    s8_meta, n_s8 = {}, 0
    try:
        if fundflow:
            s8_rows, s8_meta, s8_info = s8_live.select_live(
                c, rt_by_ts, rt_by_code, fundflow, board_live, g, plimit, snapshot_time, today_iso)
            n_s8 = len(s8_rows)
            log_line = (f"  S8实时选: {n_s8} 只 [{s8_info['path']}] {s8_info['tone']} "
                        f"| 主线池 {s8_info['universe']} | regime={s8_info['regime']}")
        else:
            # fundflow 抓空(接口抽风): 不重算(净流入必过项会清空整池), 沿用上帧防误标掉出池
            s8_rows, s8_meta, n_s8 = carry_forward_s8(c, snapshot_time, today_iso, rt_by_code)
            log_line = f"  S8实时选: fundflow 抓空, 兜底沿用上帧 {n_s8} 只 (不重算, 防误标掉出池)"
        if s8_rows:
            c.executemany(
                "INSERT OR REPLACE INTO intraday_candidate_live "
                "(snapshot_time, strategy, code, ts_code, name, industry, cand_date, "
                " price, pct, vol_ratio, turnover_rate, amount, speed_5min, net_main, "
                " entry_low, entry_high, stop_loss, target_1, target_2, position_pct, "
                " dist_to_entry, signal, top_theme, top_theme_pct) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", s8_rows)
        print(log_line)
    except Exception as e:
        print(f"  [s8-live] 失败(跳过): {str(e)[:120]}")

    # 纪要/题材自愈富集: 本帧所有候选(S1~S7 + S8实时), 缺失或超 60min 的补 candidate_logic
    try:
        # hold = 用户持仓伪策略, 仅供现价展示, 不参与纪要自愈, 避免无意义的 zsxq 搜索与 candidate_logic 死写入
        cand_ident = c.execute(
            "SELECT strategy, code, name, ts_code FROM intraday_candidate_live "
            "WHERE snapshot_time=? AND strategy != 'hold'", (snapshot_time,)).fetchall()
        zc = sqlite3.connect(logic.ZSXQ_DB, timeout=60)
        zc.row_factory = sqlite3.Row
        logic.load_groups(zc)
        since = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        board_pct = logic.load_board_pct(c)
        lg_rows = logic.selfheal(
            c, zc, [(r["strategy"], r["code"], r["name"], r["ts_code"]) for r in cand_ident],
            since, board_pct, snapshot_time)
        zc.close()
        if lg_rows:
            c.executemany(
                "INSERT OR REPLACE INTO candidate_logic VALUES (?,?,?,?,?,?,?)", lg_rows)
        print(f"  纪要自愈: 候选 {len(cand_ident)} 只, 补/刷 {len(lg_rows)} 只")
    except Exception as e:
        print(f"  [scout-logic] 自愈失败(跳过): {str(e)[:120]}")

    # 全策略触发记录 + 滚动浮盈 (S1~S8)
    try:
        update_trigger_log(c, snapshot_time, today_iso, rt_by_code, s8_meta)
    except Exception as e:
        print(f"  [trigger] 失败(跳过): {str(e)[:120]}")

    # LOF 场内外套利: QDII+商品 折溢价 + 扣费净套利 (try/except 隔离, 失败不影响主采集)
    try:
        n_lof = lof_arb.compute_live(c, now, ts)
        print(f"  LOF套利: {n_lof} 只")
    except Exception as e:
        print(f"  [lof-arb] 失败(跳过): {str(e)[:120]}")

    c.commit()
    c.close()
    print(f"  温度计: 涨{g['up']}/跌{g['down']} 涨停{g['limit_up']} 跌停{g['limit_down']} "
          f"炸板{g['blast']} 成交{g['amount']}亿 | regime={rname} 仓位上限{plimit}")
    print(f"  候选盯盘: {n_cand} 只 (锚定昨日 {anchor}) + S8实时 {n_s8} 只 + 推荐池 {n_pick} 只 + 持仓 {n_hold} 只 | 板块: {len(board_live)} 个")
    print(f"  业绩共振实时: {n_er} 只 (名单 {len(er_codes)})")
    print(f"[scout-collect] 完成 ({time.time()-t0:.1f}s) @ {snapshot_time}")


if __name__ == "__main__":
    main()
