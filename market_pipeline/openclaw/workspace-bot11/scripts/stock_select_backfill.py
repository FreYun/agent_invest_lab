"""选股数据回填 — 把 tushare 的题材/资金/估值数据灌进 market.db.

为"看懂龙头股"复盘框架的【选股层】补数据. 复用 config.py 的 token.
所有新表 date=YYYYMMDD, ts_code 带后缀(000001.SZ), 与 daily 表同口径直接 join.

设计原则:
  - 单进程串行, 每个交易日 1 次 API 调用, time.sleep 节流 (不并发, 不拉爆 CPU)
  - 可断点续传: 进度写 backfill_progress, 重跑从上次完成处继续
  - 遇限频/超限优雅退出: 保存进度 + 打印提示, 不抛栈, 下次接着跑

用法:
  python3 stock_select_backfill.py daily_basic              # 单表回填近1年
  python3 stock_select_backfill.py all                      # 全部表
  python3 stock_select_backfill.py concept_board --start 20250101 --end 20260521
  python3 stock_select_backfill.py stock_map                # 个股↔题材 最新快照(单日, 带完整性闸门)
  python3 stock_select_backfill.py board_amount             # 板块成交额(东财 dc_daily, 板块级)
表名: daily_basic | moneyflow | concept_board | board_amount | kpl_theme | stock_map | all
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_tushare_pro  # noqa: E402  走 brze 代理, 绕官方 1次/分钟 限频

DB_PATH = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))
SLEEP = 0.2
# 成分快照完整性闸门: 东财 dc_member 对最新交易日的成分是收盘后逐步发布的,
# 21:35 cron 常拉到"不满页就到底"的残档(实测 129/626 板块). 正常满额 ~1000 板块.
# 拉取板块数低于此阈值且库里已有更全的旧快照时, 判定上游未就绪, 保留旧快照不覆盖.
# (成分变化极慢, 旧快照用于盘后成交额聚合无害.)
STOCK_MAP_MIN_BOARDS = 700
# 白名单关键主线命中率闸门: 总板块数闸门只看数量, 挡不住"总数够但缺的恰是热门主线"
# 的早晨抢跑残档(2026-06-22 实测 952 板块过 700 闸门, 却缺 18 个 BK11xx 新概念主线,
# 导致 board_trend 趋势主线 94→76). 校验 scout 粗粒度白名单(coarse_themes.json)板块的
# 命中率, 低于此阈值且旧快照覆盖更全时保留旧快照. 完整快照命中率=100%, 残档=80%.
WHITELIST_MIN_HITRATE = 0.95
# coarse_themes.json 在 scout/ 下, 从本脚本位置推导(禁写死 /home 绝对路径, 适配云端/本地双前缀)
_WHITELIST_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "scout", "coarse_themes.json"))


def _load_whitelist_codes():
    """读 scout 粗粒度白名单板块 code 集合; 文件缺失/损坏 → 空集(闸门软降级为纯数量)."""
    import json
    try:
        with open(_WHITELIST_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[sel_stock_map] ⚠️ 白名单加载失败({e}), 完整性闸门降级为纯数量")
        return set()
    return {b.get("code") for g in data.get("groups", []) for b in g.get("boards", []) if b.get("code")}

SCHEMA = """
CREATE TABLE IF NOT EXISTS concept_board_daily (
    trade_date    TEXT NOT NULL,
    board_code    TEXT NOT NULL,
    board_name    TEXT,
    pct_change    REAL,
    up_num        INTEGER,
    down_num      INTEGER,
    leading_code  TEXT,
    leading_name  TEXT,
    leading_pct   REAL,
    total_mv      REAL,
    turnover_rate REAL,
    idx_type      TEXT,
    level         TEXT,
    PRIMARY KEY (trade_date, board_code)
);
CREATE INDEX IF NOT EXISTS idx_cbd_date ON concept_board_daily(trade_date);

CREATE TABLE IF NOT EXISTS stock_concept_map (
    snapshot_date TEXT NOT NULL,
    board_code    TEXT NOT NULL,
    board_name    TEXT,
    ts_code       TEXT NOT NULL,
    name          TEXT,
    PRIMARY KEY (board_code, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_scm_ts ON stock_concept_map(ts_code);

CREATE TABLE IF NOT EXISTS kpl_theme_daily (
    trade_date  TEXT NOT NULL,
    theme_code  TEXT NOT NULL,
    theme_name  TEXT,
    zt_num      INTEGER,
    up_num      INTEGER,
    ts_code     TEXT NOT NULL,
    stock_name  TEXT,
    reason      TEXT,
    hot_num     INTEGER,
    PRIMARY KEY (trade_date, theme_code, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_ktd_date ON kpl_theme_daily(trade_date);
CREATE INDEX IF NOT EXISTS idx_ktd_ts ON kpl_theme_daily(ts_code);

CREATE TABLE IF NOT EXISTS daily_basic (
    trade_date      TEXT NOT NULL,
    ts_code         TEXT NOT NULL,
    close           REAL,
    turnover_rate   REAL,
    turnover_rate_f REAL,
    volume_ratio    REAL,
    pe              REAL,
    pe_ttm          REAL,
    pb              REAL,
    ps_ttm          REAL,
    total_share     REAL,
    float_share     REAL,
    free_share      REAL,
    total_mv        REAL,
    circ_mv         REAL,
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_db_date ON daily_basic(trade_date);

CREATE TABLE IF NOT EXISTS moneyflow_daily (
    trade_date     TEXT NOT NULL,
    ts_code        TEXT NOT NULL,
    buy_lg_amount  REAL,
    sell_lg_amount REAL,
    buy_elg_amount REAL,
    sell_elg_amount REAL,
    net_main       REAL,   -- 主力净额(万元) = (大单+特大单) 买-卖
    net_mf_amount  REAL,   -- 全单净流入(万元)
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_mf_date ON moneyflow_daily(trade_date);

-- 板块级成交额 (东财 dc_daily). amount 单位: 元. /1e8 = 亿.
-- 取代 board_trend 原"炸成分股 SUM(daily.amount)"口径(依赖时灵时不灵的 dc_member 成分).
-- dc_daily 与 dc_index 同宇宙(BKxxxx.DC)且每天满额, 是稳定的板块成交额来源.
CREATE TABLE IF NOT EXISTS board_amount_daily (
    trade_date TEXT NOT NULL,
    board_code TEXT NOT NULL,
    amount     REAL,   -- 板块成交额(元)
    PRIMARY KEY (trade_date, board_code)
);
CREATE INDEX IF NOT EXISTS idx_bad_date ON board_amount_daily(trade_date);
"""


def conn():
    c = sqlite3.connect(DB_PATH, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_schema():
    c = conn()
    c.executescript(SCHEMA)
    c.commit()
    c.close()


def is_rate_limit(e: Exception) -> bool:
    s = str(e)
    return "超限" in s or "限频" in s or "每分钟" in s or "每天" in s or "权限" in s


def trade_dates(c, start: str, end: str):
    """从 daily 表取真实交易日 (避免依赖空的 trading_calendar)."""
    rows = c.execute(
        "SELECT DISTINCT trade_date FROM daily WHERE trade_date>=? AND trade_date<=? ORDER BY trade_date",
        (start, end),
    ).fetchall()
    return [r[0] for r in rows]


def get_progress(c, task: str):
    r = c.execute(
        "SELECT last_completed_date FROM backfill_progress WHERE task=?", (task,)
    ).fetchone()
    if r:
        return r[0]
    return None


def set_progress(c, task: str, date: str):
    c.execute(
        "INSERT INTO backfill_progress(task,last_completed_date,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(task) DO UPDATE SET last_completed_date=excluded.last_completed_date, "
        "updated_at=excluded.updated_at",
        (task, date, datetime.now().isoformat()),
    )
    c.commit()


def _bare(code: str) -> str:
    """带后缀代码 -> 6位裸码 (000001.SZ -> 000001)."""
    return str(code).split(".")[0].zfill(6)


def to_suffix(code: str) -> str:
    """6位代码 -> 带后缀. 已带后缀则原样返回."""
    if "." in code:
        return code
    if code.startswith(("6", "9")):
        return code + ".SH"
    if code.startswith(("0", "3", "2")):
        return code + ".SZ"
    if code.startswith(("4", "8")):
        return code + ".BJ"
    return code


def backfill_per_date(table, task, fetch_fn, rows_fn, start, end):
    """通用逐日回填框架. fetch_fn(pro,date)->df; rows_fn(date,df)->list[tuple]."""
    pro = get_tushare_pro()
    c = conn()
    dates = trade_dates(c, start, end)
    last = get_progress(c, task)
    if last:
        dates = [d for d in dates if d > last]
    if not dates:
        print(f"[{task}] 无待回填交易日 (last_completed={last})")
        c.close()
        return
    print(f"[{task}] 待回填 {len(dates)} 个交易日: {dates[0]} → {dates[-1]}")
    cols = None
    done = 0
    for d in dates:
        try:
            df = fetch_fn(pro, d)
        except Exception as e:
            if is_rate_limit(e):
                print(f"[{task}] ⏳ 限频/超限于 {d}, 已完成 {done} 天, 进度已存. 稍后重跑接着续传.\n     -> {str(e)[:90]}")
                break
            print(f"[{task}] ❌ {d} 取数失败(跳过): {str(e)[:90]}")
            time.sleep(SLEEP)
            continue
        rows = rows_fn(d, df)
        if rows:
            if cols is None:
                cols = len(rows[0])
            ph = ",".join(["?"] * cols)
            c.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({ph})", rows)
            c.commit()
        set_progress(c, task, d)
        done += 1
        if done % 20 == 0:
            print(f"[{task}] ... {done}/{len(dates)} ({d}, {len(rows)} 行)")
        time.sleep(SLEEP)
    n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"[{task}] 本次完成 {done} 天, 表内累计 {n} 行")
    c.close()


def fb_daily_basic(start, end):
    F = "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,pb,ps_ttm,total_share,float_share,free_share,total_mv,circ_mv"

    def fetch(pro, d):
        return pro.daily_basic(trade_date=d, fields=F)

    def rows(d, df):
        out = []
        for _, r in df.iterrows():
            out.append((
                r["trade_date"], r["ts_code"], r["close"], r["turnover_rate"],
                r["turnover_rate_f"], r["volume_ratio"], r["pe"], r["pe_ttm"], r["pb"],
                r["ps_ttm"], r["total_share"], r["float_share"], r["free_share"],
                r["total_mv"], r["circ_mv"],
            ))
        return out

    backfill_per_date("daily_basic", "sel_daily_basic", fetch, rows, start, end)


def fb_moneyflow(start, end):
    def fetch(pro, d):
        return pro.moneyflow(trade_date=d)

    def rows(d, df):
        out = []
        for _, r in df.iterrows():
            blg = r.get("buy_lg_amount", 0) or 0
            slg = r.get("sell_lg_amount", 0) or 0
            belg = r.get("buy_elg_amount", 0) or 0
            selg = r.get("sell_elg_amount", 0) or 0
            net_main = (blg - slg) + (belg - selg)
            out.append((
                r["trade_date"], r["ts_code"], blg, slg, belg, selg,
                net_main, r.get("net_mf_amount"),
            ))
        return out

    backfill_per_date("moneyflow_daily", "sel_moneyflow", fetch, rows, start, end)


def fb_concept_board(start, end):
    def fetch(pro, d):
        return pro.dc_index(trade_date=d)

    def rows(d, df):
        out = []
        for _, r in df.iterrows():
            out.append((
                r["trade_date"], r["ts_code"], r.get("name"), r.get("pct_change"),
                r.get("up_num"), r.get("down_num"), r.get("leading_code"),
                r.get("leading"), r.get("leading_pct"), r.get("total_mv"),
                r.get("turnover_rate"), r.get("idx_type"), r.get("level"),
            ))
        return out

    backfill_per_date("concept_board_daily", "sel_concept_board", fetch, rows, start, end)


def fb_board_amount(start, end):
    """板块成交额 (东财 dc_daily, 板块级直取, 不依赖成分). 单位: 元."""
    def fetch(pro, d):
        return pro.dc_daily(trade_date=d)

    def rows(d, df):
        return [(r["trade_date"], r["ts_code"], r.get("amount"))
                for _, r in df.iterrows()]

    backfill_per_date("board_amount_daily", "sel_board_amount", fetch, rows, start, end)


def fb_kpl_theme(start, end):
    """开盘啦每日主线题材 + 成分 + 入选理由.

    题材库 kpl_concept(原供题材级 z_t_num/up_num)接口 2026-06-03 起被 tushare 废弃
    (代理/官方均报"请指定正确的接口名"; 06-02 前可用). 改用 kpl_list(当日涨停个股)
    按 kpl_concept_cons 成分重算题材级 zt_num(已用 06-02 真值对账 100% 命中);
    up_num 是开盘啦内部指标、废接口后无源且 S8 不消费, 记 NULL. 一天 2 调用(成分+涨停).
    """
    pro = get_tushare_pro()
    c = conn()
    dates = trade_dates(c, start, end)
    last = get_progress(c, "sel_kpl_theme")
    if last:
        dates = [x for x in dates if x > last]
    if not dates:
        print(f"[sel_kpl_theme] 无待回填 (last={last})")
        c.close()
        return
    print(f"[sel_kpl_theme] 待回填 {len(dates)} 天: {dates[0]} → {dates[-1]}")
    done = 0
    for d in dates:
        try:
            cons = pro.kpl_concept_cons(trade_date=d)
            time.sleep(SLEEP)
            zt_codes = {_bare(x) for x in pro.kpl_list(trade_date=d)["ts_code"]}
        except Exception as e:
            if is_rate_limit(e):
                print(f"[sel_kpl_theme] ⏳ 限频于 {d}, 完成 {done} 天, 进度已存.\n     -> {str(e)[:90]}")
                break
            print(f"[sel_kpl_theme] ❌ {d}: {str(e)[:80]}")
            time.sleep(SLEEP)
            continue
        # 按成分把题材→涨停数: theme_code = cons['ts_code'], 成分裸码 ∩ 当日涨停集合
        theme_members = defaultdict(set)
        for _, r in cons.iterrows():
            theme_members[r["ts_code"]].add(_bare(to_suffix(str(r["con_code"]))))
        theme_zt = {tc: len(ms & zt_codes) for tc, ms in theme_members.items()}
        out = []
        for _, r in cons.iterrows():
            tcode = r["ts_code"]
            out.append((
                d, tcode, r.get("name"), theme_zt.get(tcode), None,
                to_suffix(str(r["con_code"])), r.get("con_name"), r.get("desc"), r.get("hot_num"),
            ))
        if out:
            c.executemany("INSERT OR REPLACE INTO kpl_theme_daily VALUES (?,?,?,?,?,?,?,?,?)", out)
            c.commit()
        set_progress(c, "sel_kpl_theme", d)
        done += 1
        if done % 20 == 0:
            print(f"[sel_kpl_theme] ... {done}/{len(dates)} ({d})")
        time.sleep(SLEEP)
    n = c.execute("SELECT COUNT(*) FROM kpl_theme_daily").fetchone()[0]
    print(f"[sel_kpl_theme] 完成 {done} 天, 累计 {n} 行")
    c.close()


def fb_stock_map(snapshot_date):
    """个股↔题材 最新快照 (东财 dc_member, 分页). 全表覆盖刷新."""
    pro = get_tushare_pro()
    c = conn()
    seen, off = [], 0
    try:
        while True:
            p = pro.dc_member(trade_date=snapshot_date, offset=off, limit=8000)
            if len(p) == 0:
                break
            seen.append(p)
            off += len(p)
            print(f"[sel_stock_map] 拉取 {off} 行...")
            if len(p) < 8000:
                break
            time.sleep(SLEEP)
    except Exception as e:
        if is_rate_limit(e):
            print(f"[sel_stock_map] ⏳ 限频, 已拉 {off} 行, 放弃本次刷新(保留旧快照).\n     -> {str(e)[:90]}")
            c.close()
            return
        raise
    if not seen:
        print(f"[sel_stock_map] {snapshot_date} 无数据")
        c.close()
        return
    import pandas as pd
    allm = pd.concat(seen)
    rows = [
        (snapshot_date, r["ts_code"], r.get("name"), to_suffix(str(r["con_code"])), None)
        for _, r in allm.iterrows()
    ]
    new_codes = {r[1] for r in rows}  # 本次拉到的去重板块 code
    new_boards = len(new_codes)

    # 完整性闸门(两道): (a) 总板块数偏低; (b) 白名单关键主线命中率偏低.
    # 任一不达标且库里旧快照在该维度更全时, 判定上游未发全, 保留旧快照不覆盖.
    wl_codes = _load_whitelist_codes()
    wl_n = len(wl_codes)
    new_wl_hit = len(wl_codes & new_codes)
    new_wl_rate = (new_wl_hit / wl_n) if wl_n else 1.0
    count_short = new_boards < STOCK_MAP_MIN_BOARDS
    wl_short = wl_n > 0 and new_wl_rate < WHITELIST_MIN_HITRATE
    if count_short or wl_short:
        old_date = c.execute("SELECT MAX(snapshot_date) FROM stock_concept_map").fetchone()[0]
        old_total, old_wl_hit = 0, 0
        if old_date:
            old_set = {r[0] for r in c.execute(
                "SELECT DISTINCT board_code FROM stock_concept_map WHERE snapshot_date=?", (old_date,))}
            old_total = len(old_set)
            old_wl_hit = len(wl_codes & old_set)
        reason = (f"总板块{new_boards}<{STOCK_MAP_MIN_BOARDS}" if count_short else "") + \
                 (" 且 " if count_short and wl_short else "") + \
                 (f"白名单命中{new_wl_hit}/{wl_n}={new_wl_rate*100:.0f}%<{WHITELIST_MIN_HITRATE*100:.0f}%" if wl_short else "")
        # 旧快照在触发维度上更全(总数更多 或 关键主线命中更多)才保留, 否则仍写入避免越守越旧.
        if old_date and (old_total > new_boards or old_wl_hit > new_wl_hit):
            print(f"[sel_stock_map] ⚠️ {snapshot_date} 判定上游未发全({reason}); "
                  f"保留旧快照 {old_date}(总{old_total}/白名单命中{old_wl_hit})不覆盖.")
            c.close()
            return
        print(f"[sel_stock_map] ⚠️ {snapshot_date} 残档({reason}), "
              f"但无更全旧快照(旧总{old_total}/命中{old_wl_hit}), 仍写入本次结果.")

    c.execute("DELETE FROM stock_concept_map")
    c.executemany("INSERT OR REPLACE INTO stock_concept_map VALUES (?,?,?,?,?)", rows)
    c.commit()
    n = c.execute("SELECT COUNT(*) FROM stock_concept_map").fetchone()[0]
    nb = c.execute("SELECT COUNT(DISTINCT board_code) FROM stock_concept_map").fetchone()[0]
    print(f"[sel_stock_map] 快照 {snapshot_date}: {n} 行, {nb} 个板块")
    c.close()


def default_window():
    c = conn()
    end = c.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
    c.close()
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=370)).strftime("%Y%m%d")
    return start, end


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("table", choices=["daily_basic", "moneyflow", "concept_board", "board_amount", "kpl_theme", "stock_map", "all"])
    ap.add_argument("--start")
    ap.add_argument("--end")
    a = ap.parse_args()
    init_schema()
    ds, de = default_window()
    start = a.start or ds
    end = a.end or de
    print(f"窗口: {start} → {end}  DB={DB_PATH}\n")
    if a.table in ("daily_basic", "all"):
        fb_daily_basic(start, end)
    if a.table in ("concept_board", "all"):
        fb_concept_board(start, end)
    if a.table in ("board_amount", "all"):
        fb_board_amount(start, end)
    if a.table in ("kpl_theme", "all"):
        fb_kpl_theme(start, end)
    if a.table in ("moneyflow", "all"):
        fb_moneyflow(start, end)
    if a.table in ("stock_map", "all"):
        fb_stock_map(end)


if __name__ == "__main__":
    main()
