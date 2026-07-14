"""回补东财板块日线与 board_trend_daily 历史.

用途:
  python3 backfill_board_history.py --beg 20240101

只做两件事:
1. 从东财 push2his 拉当前 scout 有效板块的官方日 K, INSERT OR IGNORE 到 concept_board_daily。
2. 对 board_trend_daily 缺失的交易日运行 board_trend.compute_trend/write。

安全边界:
- 不覆盖 concept_board_daily 既有行, 避免抹掉 leading_code 等原始字段。
- 默认只计算 board_trend_daily 缺失日期, 不重写已有趋势分。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime

import board_trend
import scout_db


DB_PATH = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))
EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def _placeholders(values) -> str:
    return ",".join("?" * len(values))


def _code_filter(column: str, codes: list[str]) -> tuple[str, list[str]]:
    if not codes or len(codes) > 900:
        return "", []
    return f" AND {column} IN ({_placeholders(codes)})", codes


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=60)
    c.row_factory = sqlite3.Row
    return c


def _board_universe(c):
    rows = c.execute(
        "SELECT board_code, MAX(board_name) AS board_name "
        "FROM board_trend_daily GROUP BY board_code ORDER BY board_code"
    ).fetchall()
    if rows:
        return [(r["board_code"], r["board_name"] or r["board_code"]) for r in rows]
    rows = c.execute(
        "SELECT board_code, MAX(board_name) AS board_name "
        "FROM concept_board_daily GROUP BY board_code ORDER BY board_code"
    ).fetchall()
    return [(r["board_code"], r["board_name"] or r["board_code"]) for r in rows]


def _secid(board_code: str) -> str:
    code = board_code.split(".", 1)[0]
    return f"90.{code}"


def fetch_klines(board_code: str, beg: str, end: str) -> tuple[str | None, list[tuple]]:
    params = {
        "secid": _secid(board_code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "beg": beg,
        "end": end,
    }
    url = EASTMONEY_KLINE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        payload = json.loads(r.read().decode("utf-8"))
    data = payload.get("data") or {}
    name = data.get("name")
    out = []
    for line in data.get("klines") or []:
        # date,open,close,high,low,volume,amount,amplitude,pct_change,change,turnover
        parts = line.split(",")
        if len(parts) < 11:
            continue
        date = parts[0].replace("-", "")
        pct_change = float(parts[8]) if parts[8] not in ("", "-") else None
        turnover = float(parts[10]) if parts[10] not in ("", "-") else None
        out.append((date, board_code, name, pct_change, turnover))
    return name, out


def backfill_concept_daily(c, boards, beg: str, end: str, sleep_s: float, dry_run: bool) -> int:
    total_new = 0
    for i, (board_code, fallback_name) in enumerate(boards, 1):
        try:
            name, rows = fetch_klines(board_code, beg, end)
        except Exception as e:
            print(f"[fetch-fail] {board_code} {fallback_name}: {e}")
            continue
        insert_rows = [(d, bc, name or fallback_name, pct, tr) for d, bc, _nm, pct, tr in rows]
        if dry_run:
            existed = c.execute(
                "SELECT COUNT(*) FROM concept_board_daily WHERE board_code=? AND trade_date BETWEEN ? AND ?",
                (board_code, beg, end),
            ).fetchone()[0]
            new_n = max(0, len(insert_rows) - existed)
        else:
            before = c.total_changes
            c.executemany(
                "INSERT OR IGNORE INTO concept_board_daily "
                "(trade_date, board_code, board_name, pct_change, turnover_rate) "
                "VALUES (?, ?, ?, ?, ?)",
                insert_rows,
            )
            c.commit()
            new_n = c.total_changes - before
        total_new += new_n
        print(f"[concept] {i:03d}/{len(boards)} {board_code} {name or fallback_name}: fetched={len(rows)} new={new_n}")
        if sleep_s:
            time.sleep(sleep_s)
    return total_new


class CachedTrendComputer:
    def __init__(self, c, end: str):
        board_trend.WHITELIST.load_if_changed()
        self.whitelist_codes = set(board_trend.WHITELIST.codes())
        if self.whitelist_codes:
            self.board_codes = sorted(self.whitelist_codes)
        else:
            self.board_codes = [row[0] for row in c.execute(
                "SELECT DISTINCT board_code FROM concept_board_daily WHERE trade_date<=? ORDER BY board_code", (end,)
            )]
        self.trade_dates = [row[0] for row in c.execute(
            "SELECT DISTINCT trade_date FROM concept_board_daily WHERE trade_date<=? ORDER BY trade_date", (end,)
        )]
        self.date_index = {trade_date: index for index, trade_date in enumerate(self.trade_dates)}
        self.board_names: dict[str, str] = {}
        self.pct_by_board: dict[str, dict[str, float | None]] = defaultdict(dict)
        self.amount_by_board: dict[str, dict[str, float]] = defaultdict(dict)
        self.market_amount_by_date: dict[str, float] = {}
        self.member_counts: dict[str, int] = {}
        self.leads_by_date: dict[str, dict[str, tuple[str | None, str | None]]] = defaultdict(dict)
        self._load(c, end)

    def _load(self, c, end: str) -> None:
        if not self.trade_dates:
            return
        start = self.trade_dates[0]
        code_sql, code_params = _code_filter("board_code", self.board_codes)
        rows = c.execute(
            "SELECT board_code, board_name, trade_date, pct_change, leading_code, leading_name "
            "FROM concept_board_daily WHERE trade_date BETWEEN ? AND ?"
            f"{code_sql} ORDER BY trade_date",
            [start, end] + code_params,
        )
        for board_code, board_name, trade_date, pct_change, leading_code, leading_name in rows:
            if board_name:
                self.board_names[board_code] = board_name
            self.pct_by_board[board_code][trade_date] = pct_change
            self.leads_by_date[trade_date][board_code] = (leading_code, leading_name)

        self.market_amount_by_date = {trade_date: amount or 0.0 for trade_date, amount in c.execute(
            "SELECT trade_date, SUM(amount)/1e5 FROM daily "
            "WHERE trade_date BETWEEN ? AND ? GROUP BY trade_date",
            (start, end),
        )}

        scm_code_sql, scm_code_params = _code_filter("scm.board_code", self.board_codes)
        rows = c.execute(
            "SELECT scm.board_code, dd.trade_date, SUM(dd.amount)/1e5 "
            "FROM daily dd JOIN stock_concept_map scm ON scm.ts_code=dd.ts_code "
            "WHERE scm.snapshot_date=(SELECT MAX(snapshot_date) FROM stock_concept_map) "
            "AND dd.trade_date BETWEEN ? AND ?"
            f"{scm_code_sql} GROUP BY scm.board_code, dd.trade_date",
            [start, end] + scm_code_params,
        )
        for board_code, trade_date, amount in rows:
            self.amount_by_board[board_code][trade_date] = amount or 0.0

        code_sql, code_params = _code_filter("board_code", self.board_codes)
        rows = c.execute(
            "SELECT board_code, COUNT(*) FROM stock_concept_map "
            "WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM stock_concept_map)"
            f"{code_sql} GROUP BY board_code",
            code_params,
        )
        self.member_counts = {board_code: count for board_code, count in rows}

        try:
            rows = c.execute(
                "SELECT trade_date, board_code, ts_code, name FROM board_leader_daily "
                "WHERE rank=1 AND trade_date BETWEEN ? AND ?"
                f"{code_sql}",
                [start, end] + code_params,
            )
            for trade_date, board_code, ts_code, name in rows:
                self.leads_by_date[trade_date][board_code] = (ts_code, name)
        except sqlite3.OperationalError:
            pass

        print(
            f"[trend-cache] dates={len(self.trade_dates)} boards={len(self.pct_by_board)} "
            f"amount_boards={len(self.amount_by_board)}"
        )

    def _window_dates(self, trade_date: str) -> list[str]:
        index = self.date_index.get(trade_date)
        if index is None:
            return []
        start = max(0, index - board_trend.WINDOW + 1)
        return self.trade_dates[start:index + 1]

    def compute(self, trade_date: str) -> list[dict]:
        dates = self._window_dates(trade_date)
        if not dates:
            return []
        leads = self.leads_by_date.get(trade_date, {})
        raw = []
        for board_code, pct_map in self.pct_by_board.items():
            board_name = self.board_names.get(board_code, board_code)
            if board_trend._is_broad(board_name) or board_trend._is_noise(board_name):
                continue
            if self.whitelist_codes and board_code not in self.whitelist_codes:
                continue
            member_count = self.member_counts.get(board_code, 0)
            if member_count < board_trend.MIN_MEMBERS or member_count > board_trend.MAX_MEMBERS:
                continue
            pcts = [pct_map[date] for date in dates if pct_map.get(date) is not None]
            if len(pcts) < 21:
                continue
            index_series = board_trend._recon_index(pcts)
            ma5 = board_trend._ma(index_series, 5)
            ma10 = board_trend._ma(index_series, 10)
            ma20 = board_trend._ma(index_series, 20)
            ma60 = board_trend._ma(index_series, 60)
            current = index_series[-1]
            above_ma20 = 1 if (ma20 and current >= ma20) else 0
            above_ma60 = 1 if (ma60 and current >= ma60) else 0
            ma_aligned = 1 if (ma5 and ma10 and ma20 and ma60 and ma5 > ma10 > ma20 > ma60) else 0
            trend_pos = 0.4 * above_ma20 + 0.3 * above_ma60 + 0.3 * ma_aligned
            ret20 = board_trend._ret(index_series, 20)
            ret60 = board_trend._ret(index_series, 60)
            ret5 = board_trend._ret(index_series, 5)
            mom_raw = 0.6 * max(ret20 or 0, 0) + 0.4 * max((ret60 if ret60 is not None else (ret20 or 0)), 0)
            dist_ma20 = board_trend._dist_pct(current, ma20)

            board_amounts = self.amount_by_board.get(board_code, {})
            amounts = [board_amounts.get(date, 0.0) for date in dates]
            amt_peak_ratio = board_trend._peak_ratio(amounts)
            amount5 = sum(amounts[-5:]) / min(5, len(amounts)) if amounts else 0.0
            amount20 = sum(amounts[-20:]) / min(20, len(amounts)) if amounts else 0.0
            vol_ratio = (amount5 / amount20) if amount20 > 0 else None
            shares = [
                (board_amounts.get(date, 0.0) / self.market_amount_by_date[date])
                if self.market_amount_by_date.get(date) else None
                for date in dates
            ]
            share5 = [share for share in shares[-5:] if share is not None]
            share20 = [share for share in shares[-20:] if share is not None]
            amt_share = sum(share5) / len(share5) if share5 else None
            share20_avg = sum(share20) / len(share20) if share20 else None
            amt_share_trend = (amt_share / share20_avg - 1) if (amt_share and share20_avg and share20_avg > 0) else None

            lead_code, lead_name = leads.get(board_code, (None, None))
            raw.append({
                "board_code": board_code, "board_name": board_name,
                "trend_pos": trend_pos, "mom_raw": mom_raw,
                "vol_ratio": vol_ratio, "amt_share": amt_share, "amt_share_trend": amt_share_trend,
                "ret20": round(ret20, 2) if ret20 is not None else None,
                "ret60": round(ret60, 2) if ret60 is not None else None,
                "ret5": round(ret5, 2) if ret5 is not None else None,
                "amt_peak_ratio": round(amt_peak_ratio, 3) if amt_peak_ratio is not None else None,
                "dist_ma20": round(dist_ma20, 2) if dist_ma20 is not None else None,
                "ma_aligned": ma_aligned, "above_ma20": above_ma20, "above_ma60": above_ma60,
                "lead_code": lead_code, "lead_name": lead_name,
            })
        if not raw:
            return []

        norm_momentum = board_trend._minmax([row["mom_raw"] for row in raw])
        norm_volume = board_trend._minmax([
            min(max(row["vol_ratio"], 0.8), 2.0) if row["vol_ratio"] else None for row in raw
        ])
        norm_share_level = board_trend._minmax([row["amt_share"] for row in raw])
        norm_share_trend = board_trend._minmax([row["amt_share_trend"] for row in raw])
        for index, row in enumerate(raw):
            amount_factor = 0.5 * norm_share_level[index] + 0.5 * norm_share_trend[index]
            score = (board_trend.WEIGHTS["trend_pos"] * row["trend_pos"]
                     + board_trend.WEIGHTS["momentum"] * norm_momentum[index]
                     + board_trend.WEIGHTS["vol_ratio"] * norm_volume[index]
                     + board_trend.WEIGHTS["amt_share"] * amount_factor)
            row["trend_score"] = round(100 * score, 2)
            row["amt_share"] = round(row["amt_share"], 5) if row["amt_share"] is not None else None
            row["amt_share_trend"] = round(row["amt_share_trend"], 4) if row["amt_share_trend"] is not None else None
            row["vol_ratio"] = round(row["vol_ratio"], 3) if row["vol_ratio"] is not None else None
        raw.sort(key=lambda row: -row["trend_score"])
        for rank, row in enumerate(raw, 1):
            row["rank"] = rank
        return raw[:board_trend.TOP_N_STORE]


def backfill_trend(
    c,
    beg: str,
    end: str,
    dry_run: bool,
    max_dates: int | None = None,
    progress_every: int = 20,
    use_cache: bool = True,
) -> int:
    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM concept_board_daily "
        "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date", (beg, end)
    )]
    missing = []
    for d in dates:
        exists = c.execute("SELECT 1 FROM board_trend_daily WHERE trade_date=? LIMIT 1", (d,)).fetchone()
        if exists is None:
            missing.append(d)
    if max_dates is not None:
        missing = missing[:max_dates]
    print(f"[trend] missing_dates={len(missing)} range={missing[0] if missing else '-'}..{missing[-1] if missing else '-'}")
    if dry_run:
        return 0
    computer = CachedTrendComputer(c, end) if use_cache and missing else None
    wrote = 0
    for i, d in enumerate(missing, 1):
        rows = computer.compute(d) if computer else board_trend.compute_trend(c, d)
        if rows:
            board_trend.write(c, d, rows)
            wrote += len(rows)
        if i <= 5 or i == len(missing) or (progress_every and i % progress_every == 0):
            print(f"[trend] {i:03d}/{len(missing)} {d}: rows={len(rows)}")
    return wrote


def _recompute_dates(c, codes: list[str]) -> list[str]:
    """需重算日期 = 板块在 concept_board_daily 的交易日 ∩ board_trend_daily 已有交易日, 升序.
    新板块没行情的早期日子本就不该有它, 不动; 不在 board_trend_daily 的日子走正常 missing 路径不归这."""
    codes = [c0 for c0 in (codes or []) if c0]
    if not codes:
        return []
    ph = ",".join("?" * len(codes))
    code_dates = {r[0] for r in c.execute(
        f"SELECT DISTINCT trade_date FROM concept_board_daily WHERE board_code IN ({ph})", codes)}
    bt_dates = {r[0] for r in c.execute("SELECT DISTINCT trade_date FROM board_trend_daily")}
    return sorted(code_dates & bt_dates)


def recompute_for_code(c, codes: list[str], progress_every: int = 20) -> int:
    """对指定板块按日全量重算重写 board_trend_daily(仅交集日期), 不联网.
    白名单须已含这些板块(调用前已写好 coarse_themes.json). 用 compute_trend(规范全字段)
    而非 CachedTrendComputer(后者缺 ret5/amt_peak_ratio/dist_ma20 字段)."""
    board_trend.WHITELIST.load_if_changed()
    dates = _recompute_dates(c, codes)
    print(f"[recompute] codes={list(codes)} dates={len(dates)} "
          f"range={dates[0] if dates else '-'}..{dates[-1] if dates else '-'}")
    wrote = 0
    for i, d in enumerate(dates, 1):
        rows = board_trend.compute_trend(c, d)
        if rows:
            board_trend.write(c, d, rows)
            wrote += len(rows)
        if i <= 5 or i == len(dates) or (progress_every and i % progress_every == 0):
            print(f"[recompute] {i:03d}/{len(dates)} {d}: rows={len(rows)}")
    return wrote


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--beg", default="20240101", help="YYYYMMDD")
    ap.add_argument("--end", default=None, help="YYYYMMDD, 默认 concept_board_daily 最新日")
    ap.add_argument("--sleep", type=float, default=0.08, help="每个板块请求后的暂停秒数")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--skip-trend", action="store_true")
    ap.add_argument("--max-trend-dates", type=int, default=None)
    ap.add_argument("--progress-every", type=int, default=20)
    ap.add_argument("--slow-trend", action="store_true", help="不用缓存路径, 逐日调用 board_trend.compute_trend")
    ap.add_argument("--recompute-code", action="append", default=None,
                    help="只对指定板块按日全量重算重写 board_trend_daily(本地交易日∩已有趋势日), 不联网. 可重复.")
    args = ap.parse_args()

    scout_db.init_schema()
    c = _conn()
    try:
        if args.recompute_code:
            n = recompute_for_code(c, args.recompute_code, args.progress_every)
            print(f"[recompute] total_rows_written={n}")
            return
        end = args.end or c.execute("SELECT MAX(trade_date) FROM concept_board_daily").fetchone()[0]
        if not end:
            end = datetime.now().strftime("%Y%m%d")
        boards = _board_universe(c)
        print(f"[backfill] boards={len(boards)} beg={args.beg} end={end} dry_run={args.dry_run}")
        if not args.skip_fetch:
            n = backfill_concept_daily(c, boards, args.beg, end, args.sleep, args.dry_run)
            print(f"[concept] total_new={n}")
        if not args.skip_trend:
            n = backfill_trend(
                c, args.beg, end, args.dry_run, args.max_trend_dates,
                args.progress_every, not args.slow_trend,
            )
            print(f"[trend] total_rows_written={n}")
    finally:
        c.close()


if __name__ == "__main__":
    main()