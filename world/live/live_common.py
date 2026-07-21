#!/usr/bin/env python3.12
"""live 盘中跑共享工具：run 发现 / 日历兜底 / NAV 门闩 / run_id 摘要。"""
import json, os, sqlite3, glob, datetime, re

WORLD = os.environ.get("LIVE_WORLD", "/home/rooot/agent_invest_lab/world")
DB_PATH = os.environ.get("FUND_DB_PATH", "/home/rooot/agent_invest_lab/data/fund.db")


def today_str() -> str:
    return os.environ.get("TODAY") or datetime.date.today().isoformat()


def is_weekday(date: str) -> bool:
    return datetime.date.fromisoformat(date).weekday() < 5


def live_run_id(source_run_id: str, bot: str) -> str:
    # dash-2026-07-20T15-04-39 -> 20260720T150439
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})", source_run_id)
    stamp = "".join(m.groups()[:3]) + "T" + "".join(m.groups()[3:]) if m else re.sub(r"[^0-9A-Za-z]", "", source_run_id)
    return f"live-{bot}-{stamp}"


def discover_live_runs(runs_dir: str) -> list:
    out = []
    for f in glob.glob(os.path.join(runs_dir, "live-*", "state.json")):
        try:
            s = json.load(open(f))
        except Exception:
            continue
        rid = os.path.basename(os.path.dirname(f))
        bots = s.get("bots", [])
        if len(bots) == 1:
            out.append((rid, bots[0]))
    out.sort()
    return out


def ensure_calendar_has(cal_path: str, date: str) -> bool:
    cal = json.load(open(cal_path))
    days = cal["trading_days"]
    if date in days:
        return False
    if not is_weekday(date):
        raise ValueError(f"{date} 非工作日，拒绝自动追加日历（节假日需人工维护）")
    days.append(date)
    days.sort()
    json.dump(cal, open(cal_path, "w"), ensure_ascii=False, indent=2)
    return True


def nav_ready(db_path: str, fund_codes: list, date: str):
    if not fund_codes:
        return True, []
    conn = sqlite3.connect(db_path)
    try:
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT fund_code FROM fund_nav WHERE nav_date = ? AND fund_code IN (%s)"
            % ",".join("?" * len(fund_codes)),
            [date, *fund_codes],
        )}
    finally:
        conn.close()
    missing = [c for c in fund_codes if c not in have]
    return (len(missing) == 0), missing


def run_fund_codes(db_path: str, bot: str, run_id: str) -> list:
    conn = sqlite3.connect(db_path)
    try:
        codes = {r[0] for r in conn.execute(
            "SELECT fund_code FROM fund_bot_holdings WHERE bot_id=? AND run_id=? AND status='active'",
            (bot, run_id))}
        codes |= {r[0] for r in conn.execute(
            "SELECT fund_code FROM fund_bot_orders WHERE bot_id=? AND order_run_id=? AND status='pending'",
            (bot, run_id))}
    finally:
        conn.close()
    return sorted(c for c in codes if c)
