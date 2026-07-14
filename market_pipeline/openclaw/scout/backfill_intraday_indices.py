"""补 intraday_snapshot 的指数涨幅字段(sh_pct/csi300_pct/... 8 个).

场景: collect.py 用 tushare realtime_quote 取指数时偶发失败, 那分钟入库了但 index
字段全 NULL (前端折线断). 或 flock skip 那分钟整行缺失 (前端也断). 本脚本用东财
push2his trends2 端点 (逐分钟历史) 回补, preClose+close 反算涨幅.

数据源: http://push2his.eastmoney.com/api/qt/stock/trends2/get
    secid 映射见 INDEX_SECID; 走 http 非 https (本机到东财 HTTPS 握手偶发超时),
    不走系统代理 (7897 会劈坏国内财经 API).

只补 index_pct 8 列; up/down/limit_up/blast/amount 是全市场成分快照, 过后无源
不能补, 保持 NULL. trade_date 从 snapshot_time 推.

用法:
    python3 backfill_intraday_indices.py                    # 补 today 所有指数字段有 NULL 的行 + gap 补齐
    python3 backfill_intraday_indices.py --date 20260706
    python3 backfill_intraday_indices.py --dry-run          # 只打印不写库
    python3 backfill_intraday_indices.py --range 10:50-10:55  # 只处理这段
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime


DB_PATH = os.getenv("MARKET_DB", __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db")))

# ts_code -> (snapshot 列名, eastmoney secid)
INDEX_SECID = {
    "000001.SH":  ("sh_pct",      "1.000001"),
    "000300.SH":  ("csi300_pct",  "1.000300"),
    "399001.SZ":  ("szcz_pct",    "0.399001"),
    "399006.SZ":  ("gem_pct",     "0.399006"),
    "000688.SH":  ("star50_pct",  "1.000688"),
    "899050.BJ":  ("bz50_pct",    "0.899050"),
    "000852.SH":  ("csi1000_pct", "1.000852"),
    "932000.CSI": ("csi2000_pct", "2.932000"),
}

# 交易时段 (HH:MM), 只在这里补分钟, 避免造非交易时段的假数据
TRADING_MINUTES = set()
for hh, mm_lo, mm_hi in [(9, 30, 59), (10, 0, 59), (11, 0, 30),
                          (13, 0, 59), (14, 0, 59), (15, 0, 0)]:
    for mm in range(mm_lo, mm_hi + 1):
        TRADING_MINUTES.add(f"{hh:02d}:{mm:02d}")


def fetch_index_trends(secid: str) -> tuple[float | None, dict[str, float]]:
    """返回 (preClose, {'HH:MM': close}). 网络/解析失败返回 (None, {})."""
    url = (
        "http://push2his.eastmoney.com/api/qt/stock/trends2/get"
        f"?secid={secid}&iscr=0&iscca=0&ndays=1"
        "&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=8) as f:
            d = json.load(f)
    except Exception as e:
        print(f"  [{secid}] fetch 失败: {e}", file=sys.stderr)
        return None, {}
    data = d.get("data") or {}
    pc = data.get("preClose")
    out: dict[str, float] = {}
    for row in data.get("trends") or []:
        parts = row.split(",")
        if len(parts) < 3:
            continue
        try:
            hhmm = parts[0][-5:]
            close = float(parts[2])
        except (ValueError, IndexError):
            continue
        out[hhmm] = close
    return pc, out


def load_all_indices() -> dict[str, tuple[float | None, dict[str, float]]]:
    """一次性拉 8 个指数. 返回 {ts_code: (preClose, {HH:MM: close})}."""
    result = {}
    for code, (col, secid) in INDEX_SECID.items():
        pc, series = fetch_index_trends(secid)
        result[code] = (pc, series)
        print(f"  {code:12s} → {col:12s}  preClose={pc}  n={len(series)}")
    return result


def pct(pre: float | None, close: float | None) -> float | None:
    if pre is None or close is None or not pre:
        return None
    return round((close - pre) / pre * 100, 2)


def collect_backfill_rows(conn: sqlite3.Connection, date: str,
                          time_range: tuple[str, str] | None) -> list[tuple[str, dict]]:
    """扫 intraday_snapshot 找 (a) 已有行但 index 字段全 NULL, (b) 分钟 gap.
    返回 [(HH:MM, {'existing': bool, 'snapshot_time': str or None})]."""
    # 现有行的 HH:MM 集合
    existing_rows: dict[str, str] = {}   # HH:MM -> snapshot_time
    null_rows: set[str] = set()          # HH:MM 有行但 index 字段全 NULL
    cur = conn.execute(
        "SELECT snapshot_time, sh_pct, csi300_pct, szcz_pct, gem_pct, star50_pct, "
        "bz50_pct, csi1000_pct, csi2000_pct FROM intraday_snapshot "
        "WHERE trade_date=? ORDER BY snapshot_time", (date,))
    for r in cur.fetchall():
        st = r[0] or ""
        hhmm = st[11:16]
        if not hhmm:
            continue
        existing_rows[hhmm] = st
        if all(r[i] is None for i in range(1, 9)):
            null_rows.add(hhmm)

    # 时段筛选
    def in_range(hhmm):
        if time_range is None:
            return True
        lo, hi = time_range
        return lo <= hhmm <= hi

    # 分钟 gap: 交易时段内且不在 existing_rows 里
    if existing_rows:
        first = min(existing_rows)
        last = max(existing_rows)
        gap_rows = {m for m in TRADING_MINUTES
                    if first <= m <= last and m not in existing_rows}
    else:
        gap_rows = set()

    todo = []
    for m in sorted(null_rows | gap_rows):
        if not in_range(m):
            continue
        todo.append((m, {"existing": m in existing_rows,
                          "snapshot_time": existing_rows.get(m)}))
    return todo


def build_row_pcts(indices: dict, hhmm: str) -> dict[str, float | None]:
    """输入 8 指数原始数据 + 目标分钟, 输出 {col: pct}."""
    out: dict[str, float | None] = {}
    for code, (col, _) in INDEX_SECID.items():
        pre, series = indices[code]
        out[col] = pct(pre, series.get(hhmm))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="YYYYMMDD, 默认取 intraday_snapshot 的 MAX(trade_date)")
    ap.add_argument("--range", dest="time_range", default=None,
                    help="HH:MM-HH:MM, 只处理这段时间")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    try:
        if args.date is None:
            r = conn.execute("SELECT MAX(trade_date) FROM intraday_snapshot").fetchone()
            args.date = r[0]
        print(f"目标交易日: {args.date}")
        tr = None
        if args.time_range:
            lo, hi = args.time_range.split("-")
            tr = (lo.strip(), hi.strip())
            print(f"时段筛选: {tr[0]} - {tr[1]}")

        todo = collect_backfill_rows(conn, args.date, tr)
        if not todo:
            print("无需要补的行.")
            return 0
        print(f"待补 {len(todo)} 行:")
        for m, info in todo:
            tag = "NULL-fill" if info["existing"] else "MISSING-insert"
            print(f"  {m}  [{tag}]")

        print("\n拉指数分钟历史...")
        indices = load_all_indices()

        print("\n应用补丁:")
        n_upd = n_ins = 0
        for m, info in todo:
            pcts = build_row_pcts(indices, m)
            if all(v is None for v in pcts.values()):
                print(f"  {m}  跳过(东财也没这分钟)")
                continue
            snapshot_time = info["snapshot_time"]
            if info["existing"]:
                # UPDATE 已有行, 只覆盖 NULL 字段
                sets = ", ".join(f"{col}=COALESCE({col}, ?)" for col in pcts)
                vals = list(pcts.values()) + [snapshot_time]
                print(f"  UPDATE {snapshot_time}  " + " ".join(
                    f"{col}={v}" for col, v in pcts.items() if v is not None))
                if not args.dry_run:
                    conn.execute(
                        f"UPDATE intraday_snapshot SET {sets} WHERE snapshot_time=?",
                        vals)
                    n_upd += 1
            else:
                # INSERT 缺失行, 时间戳用 HH:MM:00 (与真实入库 HH:MM:01 区分, 方便追溯)
                st = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:8]} {m}:00"
                print(f"  INSERT {st}  " + " ".join(
                    f"{col}={v}" for col, v in pcts.items() if v is not None))
                if not args.dry_run:
                    conn.execute(
                        "INSERT OR IGNORE INTO intraday_snapshot "
                        "(snapshot_time, trade_date, sh_pct, csi300_pct, szcz_pct, "
                        "gem_pct, star50_pct, bz50_pct, csi1000_pct, csi2000_pct) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (st, args.date, pcts["sh_pct"], pcts["csi300_pct"],
                         pcts["szcz_pct"], pcts["gem_pct"], pcts["star50_pct"],
                         pcts["bz50_pct"], pcts["csi1000_pct"], pcts["csi2000_pct"]))
                    n_ins += 1
        if not args.dry_run:
            conn.commit()
        print(f"\n完成: UPDATE {n_upd} 行, INSERT {n_ins} 行"
              + (" [dry-run]" if args.dry_run else ""))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
