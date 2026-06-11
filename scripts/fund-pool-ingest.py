#!/usr/bin/env python3
"""
基金池入库脚本：从 xlsx 只取基金代码，其余信息全部走 ttjj-api 拉取并补全到 fund.db。

写入三张表：
- fund_info          基本信息（/api/fund/basic-info）
- fund_performance   分区间业绩（/api/fund/performance），as_of_date = end_date
- fund_nav           复权净值（/api/fund/nav），start_date..end_date

合并语义（不替换）：
- 新增 xlsx 里有、库里没有的基金；刷新两边都有的基金。
- 不删除库里已有、xlsx 里没有的基金（如债基）。
- fund_info 用 ON CONFLICT 只更新本脚本拉到的列，保留已有的 redeem_fee_json / theme /
  share_class / purchase_status / redeem_status。

数据源：ttjj-api（http://ttjj-data-api.jijinmima.cn）

用法：
  python3 scripts/fund-pool-ingest.py --start-date 2024-01-01            # 全量，end 默认今天
  python3 scripts/fund-pool-ingest.py --start-date 2024-01-01 --limit 3  # 只跑前 3 只（验证）
  python3 scripts/fund-pool-ingest.py --start-date 2024-01-01 --codes 000051 000059
  python3 scripts/fund-pool-ingest.py --start-date 2024-01-01 --skip-nav # 只补 info+performance

日更模式（lab 库每日自动追新，不读 xlsx）：
  python3 scripts/fund-pool-ingest.py --from-db --lookback-days 10 --skip-info
    --from-db        基金清单来自库内 fund_info（而非 xlsx）
    --lookback-days  nav 起始日 = 今天 - N 天（覆盖周末/节假日/漏跑缺口；省去 --start-date）
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

import requests

# 路径默认 /home/rooot（历史布局）；本机迁到 $HOME/rooot 后用 FUND_DB_PATH / OPENCLAW_ROOT 覆盖，
# 与 fund-portfolio-mcp/db.py 同约定。FUND_DB_PATH 显式指定库；否则按 OPENCLAW_ROOT 推导。
_ROOT = os.getenv("OPENCLAW_ROOT", "/home/rooot/agent_invest_lab")
DB_PATH = os.getenv("FUND_DB_PATH", os.path.join(_ROOT, "data", "fund.db"))
XLSX_PATH = os.path.join(_ROOT, "data", "被动指数型基金池（权益黄金）.xlsx")
LOG_PATH = os.path.join(_ROOT, "logs", "fund-pool-ingest.log")
TTJJ_API_URL = os.getenv("TTJJ_API_URL", "http://ttjj-data-api.jijinmima.cn").rstrip("/")
API_TIMEOUT = 120
API_HEADERS = {"Content-Type": "application/json"}

# ttjj-api 周期名称 -> fund_performance.period 代码（今年以来无对应列，不入库）
PERIOD_NAME_MAP = {
    "近一月": "1m",
    "近三月": "3m",
    "近六月": "6m",
    "近一年": "1y",
    "近两年": "2y",
    "近三年": "3y",
    "近五年": "5y",
}

Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("fund-pool-ingest")
log.setLevel(logging.INFO)
if not log.handlers:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.handlers = [fh, sh]
    log.propagate = False


# ----------------------------- ttjj-api -----------------------------
def ttjj_post(path: str, payload: dict, timeout: int = API_TIMEOUT) -> dict:
    """POST 到 ttjj-api 的某 REST 接口，返回解析后的 JSON。success=False 时抛错。"""
    resp = requests.post(f"{TTJJ_API_URL}{path}", json=payload, headers=API_HEADERS, timeout=timeout)
    resp.encoding = "utf-8"
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("success") is False:
        raise ValueError(data.get("message") or f"ttjj-api {path} 返回失败")
    return data


def ttjj_health() -> bool:
    try:
        resp = requests.get(f"{TTJJ_API_URL}/health", headers=API_HEADERS, timeout=10)
        resp.raise_for_status()
        log.info("ttjj-api ok: %s", TTJJ_API_URL)
        return True
    except Exception as exc:
        log.error("ttjj-api 不可达: %s", exc)
        return False


# --------------------------- helpers ---------------------------
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def normalize_date(s: str) -> str:
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    datetime.strptime(s, "%Y-%m-%d")
    return s


def parse_float(v):
    if v in (None, "", "--"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def chunked(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def read_codes_from_db(conn: sqlite3.Connection) -> list[str]:
    """日更模式：基金清单取自库内 fund_info（含债基等 xlsx 之外的产品）。"""
    rows = conn.execute("SELECT fund_code FROM fund_info ORDER BY fund_code").fetchall()
    return [r["fund_code"] for r in rows if r["fund_code"]]


def read_codes_from_xlsx(path: str) -> list[str]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    codes: list[str] = []
    seen = set()
    first = True
    for row in ws.iter_rows(values_only=True):
        if first:  # 表头
            first = False
            continue
        raw = row[0] if row else None  # 第一列：基金代码
        if raw is None:
            continue
        try:
            code = str(int(raw)).zfill(6)
        except (TypeError, ValueError):
            code = str(raw).strip().zfill(6)
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    wb.close()
    return codes


# --------------------------- fetch+upsert: fund_info ---------------------------
def fetch_info_batch(codes: list[str]) -> dict[str, dict]:
    res = ttjj_post("/api/fund/basic-info", {"fund_codes": list(codes)})
    out = {}
    for it in (res.get("items") or []):
        code = str(it.get("基金代码") or "").strip()
        if code:
            out[code] = it
    return out


def upsert_info(conn: sqlite3.Connection, code: str, m: dict) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    params = (
        code,
        m.get("基金名称"),
        m.get("基金公司"),
        m.get("基金经理"),
        m.get("基金类型"),
        m.get("成立时间"),
        parse_float(m.get("基金规模_亿元")),
        parse_float(m.get("基金管理费率")),
        parse_float(m.get("基金托管费率")),
        parse_float(m.get("最高申购费率")),
        parse_float(m.get("销售服务费率")),
        now,
    )
    with conn:
        conn.execute(
            """
            INSERT INTO fund_info
              (fund_code, fund_name, fund_company, fund_manager, fund_type,
               established_date, scale, mgmt_fee, custody_fee, purchase_fee,
               sales_service_fee, purchase_status, redeem_status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 'open', ?)
            ON CONFLICT(fund_code) DO UPDATE SET
              fund_name=excluded.fund_name,
              fund_company=excluded.fund_company,
              fund_manager=excluded.fund_manager,
              fund_type=excluded.fund_type,
              established_date=excluded.established_date,
              scale=excluded.scale,
              mgmt_fee=excluded.mgmt_fee,
              custody_fee=excluded.custody_fee,
              purchase_fee=excluded.purchase_fee,
              sales_service_fee=excluded.sales_service_fee,
              updated_at=excluded.updated_at
            """,
            params,
        )


# --------------------------- fetch+upsert: fund_performance ---------------------------
def fetch_perf_batch(codes: list[str]) -> dict[str, list]:
    res = ttjj_post("/api/fund/performance", {"fund_codes": list(codes)})
    out = {}
    for it in (res.get("items") or []):
        code = str(it.get("基金代码") or "").strip()
        if code:
            out[code] = it.get("绩效记录") or []
    return out


def upsert_perf(conn: sqlite3.Connection, code: str, recs: list, as_of_date: str) -> int:
    if not recs:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for r in recs:
        if not isinstance(r, dict):
            continue
        period = PERIOD_NAME_MAP.get(r.get("周期名称"))
        if not period:  # 今年以来 等不入库
            continue
        rows.append(
            (
                code,
                as_of_date,
                period,
                parse_float(r.get("收益率")),
                parse_float(r.get("收益率排名百分比")),
                r.get("收益率排名"),
                parse_float(r.get("最大回撤")),
                parse_float(r.get("波动率")),
                parse_float(r.get("夏普比率")),
                parse_float(r.get("卡玛比率")),
                now,
            )
        )
    if not rows:
        return 0
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO fund_performance
              (fund_code, as_of_date, period, return_pct, rank_pct, rank_text,
               max_drawdown_pct, volatility_pct, sharpe_ratio, calmar_ratio, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


# --------------------------- fetch+upsert: fund_nav ---------------------------
def fetch_nav_rows(fund_code: str, start_date: str, end_date: str) -> list[dict]:
    res = ttjj_post(
        "/api/fund/nav",
        {"fund_codes": [fund_code], "start_date": start_date, "end_date": end_date},
    )
    items = res.get("items") or []
    records = []
    for it in items:
        if str(it.get("基金代码")) == str(fund_code):
            records = it.get("净值记录") or []
            break
    else:
        if items:
            records = items[0].get("净值记录") or []

    out = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        nav_date = rec.get("交易日期")
        if nav_date and len(str(nav_date)) == 8 and str(nav_date).isdigit():
            s = str(nav_date)
            nav_date = f"{s[:4]}-{s[4:6]}-{s[6:]}"
        if not nav_date:
            continue
        out.append(
            {
                "fund_code": fund_code,
                "nav_date": nav_date,
                "nav": parse_float(rec.get("复权单位净值")),
                "acc_nav": None,
                "daily_return_pct": parse_float(rec.get("日收益率")),
            }
        )
    return out


def upsert_nav_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO fund_nav
              (fund_code, nav_date, nav, acc_nav, daily_return_pct, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (r["fund_code"], r["nav_date"], r["nav"], r["acc_nav"], r["daily_return_pct"], now)
                for r in rows
            ],
        )
    return len(rows)


# --------------------- fund_nav_performance（从 nav 派生区间业绩）---------------------
# 口径逐字对齐 fund-portfolio-mcp/server.py 的 _compute_fund_nav_performance：
#   统一年化口径（vol/sharpe ×√252、calmar=年化收益/|MDD|、另存 annualized_return_pct；
#   return/MDD 区间原值）、rf=1%/252（对齐 server.py 的 _BOT_PERF_RF_ANNUAL_PCT）、
#   回撤 peak-to-trough、波动率/夏普用样本 std(N-1)、
#   窗口按交易日 21/63/126/252、since_inception 取全量、不满窗 fallback。
# 唯一差异：本库 acc_nav 恒空、复权单位净值落在 nav 列，故累计序列用
# COALESCE(acc_nav, nav)（复权净值已含分红再投，等价 acc_nav 的收益口径）。
_NAVPERF_RF_DAILY_PCT = 1.0 / 252
_NAVPERF_TRADING_DAYS = 252
_NAVPERF_ANN_FACTOR = _NAVPERF_TRADING_DAYS ** 0.5
_NAVPERF_PERIODS: list[tuple[str, int | None]] = [
    ("1m", 21), ("3m", 63), ("6m", 126), ("1y", 252), ("since_inception", None),
]


def _calc_max_drawdown(series: list[float]) -> float:
    if not series:
        return 0.0
    peak = series[0]
    max_dd = 0.0
    for v in series:
        if v > peak:
            peak = v
        dd = (v - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd
    return max_dd


def _stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return var ** 0.5


def _r(val, n=2):
    return round(val, n) if val is not None else None


def compute_nav_perf(conn: sqlite3.Connection, fund_code: str, trade_date: str) -> int:
    rows = conn.execute(
        "SELECT COALESCE(acc_nav, nav) AS cum, daily_return_pct AS dr FROM fund_nav "
        "WHERE fund_code = ? AND nav_date <= ? ORDER BY nav_date",
        (fund_code, trade_date),
    ).fetchall()
    if not rows:
        return 0
    total = len(rows)
    written = 0
    for period, target in _NAVPERF_PERIODS:
        if target is None or total < target:
            window = list(rows)
            data_points = total
            fallback = 1 if (target is not None and total < target) else 0
            window_target = target
        else:
            window = list(rows[-target:])
            data_points = target
            fallback = 0
            window_target = target
        if not window:
            continue

        first = float(window[0]["cum"]) if window[0]["cum"] is not None else None
        last = float(window[-1]["cum"]) if window[-1]["cum"] is not None else None
        return_pct = (last / first - 1.0) * 100 if (first and last) else 0.0

        cum_series = [float(r["cum"]) for r in window if r["cum"] is not None]
        max_dd = _calc_max_drawdown(cum_series) if cum_series else 0.0

        # 年化收益：年化基数 = 净值端点跨越的交易日数（点数 - 1）；1+r<=0 时无意义
        span = data_points - 1
        ann_base = 1 + return_pct / 100
        ann_return = (
            (ann_base ** (_NAVPERF_TRADING_DAYS / span) - 1) * 100
            if span > 0 and ann_base > 0 else None
        )

        drs = [float(r["dr"]) for r in window if r["dr"] is not None]
        if len(drs) >= 2:
            mean_d = sum(drs) / len(drs)
            std_d = _stdev(drs)
            vol = std_d * _NAVPERF_ANN_FACTOR
            sharpe = (mean_d - _NAVPERF_RF_DAILY_PCT) / std_d * _NAVPERF_ANN_FACTOR if std_d > 1e-9 else None
        else:
            vol = None
            sharpe = None

        calmar = (
            ann_return / abs(max_dd)
            if (max_dd is not None and abs(max_dd) > 1e-9 and ann_return is not None) else None
        )

        conn.execute(
            "INSERT OR REPLACE INTO fund_nav_performance "
            "(fund_code, trade_date, period, return_pct, annualized_return_pct, "
            " max_drawdown_pct, "
            " volatility_pct, sharpe_ratio, calmar_ratio, data_points, "
            " window_target_days, fallback, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                fund_code, trade_date, period,
                _r(return_pct, 4),
                _r(ann_return, 4) if ann_return is not None else None,
                _r(max_dd, 4) if max_dd is not None else None,
                _r(vol, 6) if vol is not None else None,
                _r(sharpe, 6) if sharpe is not None else None,
                _r(calmar, 6) if calmar is not None else None,
                data_points, window_target, fallback,
            ),
        )
        written += 1
    return written


# ------------------------------- main -------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", help="净值起始日 YYYY-MM-DD or YYYYMMDD（与 --lookback-days 二选一）")
    parser.add_argument("--lookback-days", type=int, help="日更模式：nav 起始日 = 今天 - N 天")
    parser.add_argument("--end-date", default=date.today().strftime("%Y-%m-%d"), help="净值/业绩截止日，默认今天")
    parser.add_argument("--xlsx", default=XLSX_PATH, help="基金池 xlsx 路径")
    parser.add_argument("--from-db", action="store_true", help="基金清单取自库内 fund_info（而非 xlsx）")
    parser.add_argument("--codes", nargs="*", help="指定基金代码（覆盖 xlsx / --from-db）")
    parser.add_argument("--limit", type=int, help="只处理前 N 只（验证用）")
    parser.add_argument("--batch-size", type=int, default=50, help="info/performance 每批代码数")
    parser.add_argument("--skip-info", action="store_true")
    parser.add_argument("--skip-perf", action="store_true")
    parser.add_argument("--skip-nav", action="store_true")
    parser.add_argument("--skip-nav-perf", action="store_true", help="跳过 fund_nav_performance 派生计算")
    args = parser.parse_args()

    end_date = normalize_date(args.end_date)
    if args.lookback_days is not None:
        if args.lookback_days < 0:
            parser.error("--lookback-days 不能为负")
        from datetime import timedelta
        start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=args.lookback_days)).strftime("%Y-%m-%d")
    elif args.start_date:
        start_date = normalize_date(args.start_date)
    else:
        parser.error("必须提供 --start-date 或 --lookback-days")

    conn = get_conn()

    if args.codes:
        codes = [c.zfill(6) for c in args.codes]
    elif args.from_db:
        codes = read_codes_from_db(conn)
    else:
        codes = read_codes_from_xlsx(args.xlsx)
    if args.limit:
        codes = codes[: args.limit]
    if not codes:
        log.warning("没有基金代码可处理")
        return 0
    log.info("待处理基金: %d 只 | 净值 %s..%s | 业绩 as_of=%s", len(codes), start_date, end_date, end_date)

    if not ttjj_health():
        return 2

    info_failed: list[str] = []
    perf_failed: list[str] = []
    nav_failed: list[str] = []

    # 1) fund_info
    if not args.skip_info:
        ok = 0
        for batch in chunked(codes, args.batch_size):
            try:
                got = fetch_info_batch(batch)
            except Exception as exc:
                log.error("info 批失败 %s..: %s", batch[0], exc)
                info_failed.extend(batch)
                continue
            for code in batch:
                m = got.get(code)
                if not m:
                    info_failed.append(code)
                    continue
                upsert_info(conn, code, m)
                ok += 1
            log.info("[info] 累计写入 %d / %d", ok, len(codes))
        log.info("[info] 完成: 写入 %d, 缺失 %d", ok, len(info_failed))

    # 2) fund_nav（先刷净值：业绩 as_of 要锚定刷新后的最新交易日）
    if not args.skip_nav:
        total_rows = 0
        for i, code in enumerate(codes, start=1):
            try:
                rows = fetch_nav_rows(code, start_date, end_date)
                n = upsert_nav_rows(conn, rows)
                total_rows += n
                log.info("[nav %d/%d] %s 写入 %d 行", i, len(codes), code, n)
            except Exception as exc:
                nav_failed.append(code)
                log.error("[nav %d/%d] %s 失败: %s", i, len(codes), code, exc)
        log.info("[nav] 完成: rows=%d, 失败 %d", total_rows, len(nav_failed))

    # 3) fund_performance
    # as_of_date 锚定「已发布的最新净值交易日」：get_fund_performance 不返回任何日期，
    # 指标是数据商基于最新净值算的。用 MAX(nav_date) 而非日历 today，避免当日净值
    # 尚未发布时把业绩虚标成今天（例：05-21 早上净值只到 05-20，as_of 必须是 05-20）。
    if not args.skip_perf:
        row = conn.execute("SELECT MAX(nav_date) AS d FROM fund_nav").fetchone()
        perf_as_of = (row["d"] if row else None) or end_date
        log.info("[perf] as_of_date = %s（最新净值交易日）", perf_as_of)
        ok = 0
        for batch in chunked(codes, args.batch_size):
            try:
                got = fetch_perf_batch(batch)
            except Exception as exc:
                log.error("perf 批失败 %s..: %s", batch[0], exc)
                perf_failed.extend(batch)
                continue
            for code in batch:
                rec = got.get(code)
                if not rec:
                    perf_failed.append(code)
                    continue
                n = upsert_perf(conn, code, rec, perf_as_of)
                if n:
                    ok += 1
                else:
                    perf_failed.append(code)
            log.info("[perf] 累计写入 %d / %d", ok, len(codes))
        log.info("[perf] 完成: 写入 %d, 缺失 %d", ok, len(perf_failed))

    # 4) fund_nav_performance（从 nav 派生区间业绩；每只基金只在其最新交易日落一组）
    if not args.skip_nav_perf:
        latest = {
            r["fund_code"]: r["d"]
            for r in conn.execute("SELECT fund_code, MAX(nav_date) AS d FROM fund_nav GROUP BY fund_code")
        }
        ok = 0
        rows_w = 0
        with conn:
            for i, code in enumerate(codes, start=1):
                nd = latest.get(code)
                if not nd:
                    continue
                try:
                    rows_w += compute_nav_perf(conn, code, nd)
                    ok += 1
                except Exception as exc:
                    log.error("[navperf] %s 失败: %s", code, exc)
                if i % 200 == 0:
                    log.info("[navperf] 进度 %d/%d", i, len(codes))
        log.info("[navperf] 完成: 基金 %d, 写入 %d 行", ok, rows_w)

    log.info(
        "全部完成 | info缺失=%d perf缺失=%d nav失败=%d",
        len(info_failed), len(perf_failed), len(nav_failed),
    )
    for label, lst in (("info", info_failed), ("perf", perf_failed), ("nav", nav_failed)):
        if lst:
            log.info("%s 缺失/失败代码: %s", label, ",".join(lst))
    return 1 if (info_failed or perf_failed or nav_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
