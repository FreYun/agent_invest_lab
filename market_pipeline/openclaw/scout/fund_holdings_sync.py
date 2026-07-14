"""scout/fund_holdings_sync.py — 主动基金前十大重仓股 正向全量抓取 (季度跑)

为什么存在: 板块×主动基金匹配原先靠"按股票反查基金"(board_etf.py:fetch_stock_holdings),
只覆盖上盘面雷达的热门股, 白酒/食品饮料等防御板块的龙头(茅台/五粮液)从没被抓, 重仓它们的
主流主动基金(如 005827)整只缺失. 本脚本反过来——直接按基金抓前十大重仓(ttjj fund_top_holdings),
落到独立表 fund_top_holdings, 让 board_active_fund_daily 改读这张正向表.

正向表是 scout 基金匹配的唯一底表, 同时喂两个产出:
  - board_active_fund_daily (主动基金): etf_meta.is_active_equity=1
  - board_etf_daily        (对标场内ETF): etf_meta.is_strict_etf=1

流程:
  1. tushare fund_basic 拉全市场存续基金 (market=O offset 翻页 + market=E)
  2. 筛权益+混合里会被用到的两类 + 逐只分类:
       主动(invest_type∉被动集) → is_active_equity=1
       场内ETF(market=E 且 invest_type∈被动集) → is_strict_etf=1
       场外被动指数/非权益类 → 丢弃
  3. 份额去重: 同 (公司, 去后缀名) 组只留主份额 (复用 board_etf._main_share_key)
  4. upsert etf_meta (按行写分类位), 供匹配 SQL 的 JOIN
  5. 串行分批调 fund_top_holdings, 过滤港股/不可用, 幂等写 fund_top_holdings

持仓季报披露后季度跑一次 (绝不进每日 cron). 单进程串行, 不并发 (记忆: feedback_no_parallel_cpu_burn).

用法:
  /usr/bin/python3.12 fund_holdings_sync.py            # 全量
  /usr/bin/python3.12 fund_holdings_sync.py --limit 200  # 只抓前 200 只 (调试)
  /usr/bin/python3.12 fund_holdings_sync.py --dry-run    # 只拉列表+筛选, 不写库不抓持仓
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402
import _tushare_client  # noqa: E402
import _ttjj_client  # noqa: E402
from board_etf import _base_name, _main_share_key  # noqa: E402  复用份额去重

log = logging.getLogger("fund_holdings_sync")

# 间接引用方便测试 monkeypatch
_tushare_call = _tushare_client.call
_ttjj_call = _ttjj_client.call

PAGE_SIZE = 15000              # tushare fund_basic 单页上限 (offset 翻页)
HOLDINGS_BATCH = 48            # fund_top_holdings 批量 (实测 ≥48 无截断)
SLEEP_BETWEEN_BATCH = 0.5      # 批间礼貌休眠 (秒)
WALL_BUDGET_SECONDS = 60 * 60  # 全市场首次铺底兜底 (~5000 基金 / 48 ≈ 105 批)

EQUITY_FUND_TYPES = ("股票型", "混合型")
PASSIVE_INVEST_TYPES = ("被动指数型", "增强指数型")  # 指数/被动: 场内的归 ETF 侧, 场外的丢弃


# ── 1. 全市场基金列表 ────────────────────────────────────────────────
def fetch_fund_universe() -> list[dict]:
    """tushare fund_basic 拉全部存续基金 (场外 O offset 翻页 + 场内 E).
    返回原始行 list, 每行含 ts_code/name/management/fund_type/invest_type/market."""
    universe: list[dict] = []
    for market in ("O", "E"):
        offset = 0
        while True:
            resp = _tushare_call("fund_basic", market=market, status="L",
                                 offset=str(offset))
            rows = resp.get("rows", []) or []
            universe.extend(rows)
            log.info("fund_basic market=%s offset=%d -> %d 行 (累计 %d)",
                     market, offset, len(rows), len(universe))
            if not resp.get("has_more") or not rows:
                break
            offset += len(rows)
    return universe


# ── 2+3. 筛持A股权益(主动+场内ETF), 逐只分类, 份额去重 ───────────────
def select_funds(universe: list[dict]) -> list[dict]:
    """筛权益+混合里【会被匹配用到】的两类基金, 并逐只打分类位:
      - 主动权益+混合 (is_active_equity=1): invest_type ∉ 被动集
      - 场内 ETF       (is_strict_etf=1):    market=='E' 且 invest_type ∈ 被动集
    丢弃: 场外被动指数(两个 bucket 都不用)、非权益类(债券/货币/FOF/商品/REITs)。
    同 (公司, 去后缀名) 组只留主份额 (ETF 基本无 A/C 份额, 不受影响).
    返回归一化 dict: {fund_code(裸6位), fund_name, company, fund_type,
                      is_active_equity, is_strict_etf}."""
    cand = []
    for r in universe:
        if r.get("fund_type") not in EQUITY_FUND_TYPES:
            continue
        ts = r.get("ts_code") or ""
        code = ts.split(".")[0]
        if not (code.isdigit() and len(code) == 6):
            continue
        passive = r.get("invest_type") in PASSIVE_INVEST_TYPES
        market = r.get("market")
        is_active = 0 if passive else 1
        is_etf = 1 if (passive and market == "E") else 0
        if not (is_active or is_etf):
            continue                       # 场外被动指数: 两 bucket 都不用, 丢弃
        cand.append({"fund_code": code, "fund_name": r.get("name") or "",
                     "company": r.get("management") or "",
                     "fund_type": r.get("fund_type"),
                     "is_active_equity": is_active, "is_strict_etf": is_etf})
    # 份额去重: (公司, 去后缀名) 组内取主份额 (无后缀 > A > 最小 code)
    groups: dict[tuple, dict] = {}
    for r in cand:
        key = (r["company"], _base_name(r["fund_name"]))
        cur = groups.get(key)
        if cur is None or _main_share_key(r) < _main_share_key(cur):
            groups[key] = r
    deduped = sorted(groups.values(), key=lambda r: r["fund_code"])
    n_act = sum(r["is_active_equity"] for r in deduped)
    n_etf = sum(r["is_strict_etf"] for r in deduped)
    log.info("筛选: 候选 %d 只 -> 份额去重后 %d 只 (主动 %d + 场内ETF %d)",
             len(cand), len(deduped), n_act, n_etf)
    return deduped


# ── 4. upsert etf_meta ───────────────────────────────────────────────
def upsert_etf_meta(db_path: str, funds: list[dict]) -> None:
    """候选写 etf_meta, 按行写各自的 is_active_equity / is_strict_etf.
    冲突时不覆盖已有 fund_scale."""
    if not funds:
        return
    now = datetime.now().isoformat()
    c = scout_db.conn(db_path)
    try:
        c.executemany(
            "INSERT INTO etf_meta"
            "(fund_code, fund_name, fund_type, is_strict_etf, fund_scale, "
            " cached_at, company, is_active_equity) "
            "VALUES (?,?,?,?,NULL,?,?,?) "
            "ON CONFLICT(fund_code) DO UPDATE SET "
            "  fund_name=excluded.fund_name, fund_type=excluded.fund_type, "
            "  is_strict_etf=excluded.is_strict_etf, "
            "  is_active_equity=excluded.is_active_equity, "
            "  company=excluded.company, cached_at=excluded.cached_at",  # fund_scale 不覆盖
            [(f["fund_code"], f["fund_name"], f["fund_type"], f["is_strict_etf"],
              now, f["company"], f["is_active_equity"]) for f in funds])
        c.commit()
    finally:
        c.close()


# ── 5. 正向抓持仓 ────────────────────────────────────────────────────
def _is_a_share(code) -> bool:
    """6 位纯数字 A 股 (排除港股 5 位 00700 / 带后缀)."""
    c = str(code).split(".")[0]
    return c.isdigit() and len(c) == 6


def store_holdings_batch(db_path: str, fund_codes: list[str]) -> tuple[int, int]:
    """对一批基金调 fund_top_holdings, 幂等写 fund_top_holdings.
    返回 (写入行数, 命中基金数). 港股/不可用基金跳过."""
    resp = _ttjj_call("fund_top_holdings", fund_codes=fund_codes)
    items = resp.get("items", []) or []
    now = datetime.now().isoformat()
    rows = []
    hit_funds = set()
    for it in items:
        if not it.get("是否可用"):
            continue
        fcode = it.get("基金代码")
        if not fcode:
            continue
        report = it.get("报告日期") or ""
        for idx, h in enumerate(it.get("重仓股列表", []) or [], 1):
            sc = h.get("股票代码")
            if not _is_a_share(sc):
                continue
            rows.append((fcode, str(sc).split(".")[0], h.get("股票名称"),
                         h.get("占净值比例"), idx, report, now))
            hit_funds.add(fcode)
    c = scout_db.conn(db_path)
    try:
        c.execute("BEGIN")
        ph = ",".join("?" * len(fund_codes))
        c.execute(f"DELETE FROM fund_top_holdings WHERE fund_code IN ({ph})",
                  fund_codes)
        if rows:
            c.executemany(
                "INSERT OR REPLACE INTO fund_top_holdings"
                "(fund_code, stock_code, stock_name, weight, holding_rank, "
                " report_date, cached_at) VALUES (?,?,?,?,?,?,?)", rows)
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    return len(rows), len(hit_funds)


# ── 编排 ─────────────────────────────────────────────────────────────
def run(db_path: str, limit: int | None = None, dry_run: bool = False) -> int:
    """返回 rc (0=正常, 2=预算到点提前结束)."""
    scout_db.init_schema(db_path)
    start = time.time()

    universe = fetch_fund_universe()
    funds = select_funds(universe)
    if limit:
        funds = funds[:limit]
        log.info("--limit %d: 只处理前 %d 只", limit, len(funds))

    if dry_run:
        log.info("[dry-run] 不写库. 候选 %d 只, 样例: %s",
                 len(funds), [f["fund_code"] for f in funds[:10]])
        return 0

    upsert_etf_meta(db_path, funds)
    log.info("etf_meta upsert %d 只基金 (主动+场内ETF)", len(funds))

    codes = [f["fund_code"] for f in funds]
    total_rows = total_hit = 0
    aborted = False
    for i in range(0, len(codes), HOLDINGS_BATCH):
        if time.time() - start > WALL_BUDGET_SECONDS:
            log.warning("超 wall-clock 预算 (%ds), 提前结束 (已处理 %d/%d 只)",
                        WALL_BUDGET_SECONDS, i, len(codes))
            aborted = True
            break
        batch = codes[i:i + HOLDINGS_BATCH]
        try:
            nrows, nhit = store_holdings_batch(db_path, batch)
        except RuntimeError as e:
            log.warning("批 %d-%d 抓取失败, 跳过: %s", i, i + len(batch), e)
            continue
        total_rows += nrows
        total_hit += nhit
        log.info("[%d/%d] 批 %d 只 -> %d 行, %d 只命中A股 (累计 %d 基金/%d 行)",
                 min(i + len(batch), len(codes)), len(codes), len(batch),
                 nrows, nhit, total_hit, total_rows)
        time.sleep(SLEEP_BETWEEN_BATCH)

    skipped = len(codes) - total_hit
    log.info("完成: %d 只基金 -> %d 只有A股重仓入表 (%d 行), %d 只无A股/不可用被过滤, 耗时 %.1fs%s",
             len(codes), total_hit, total_rows, skipped, time.time() - start,
             " (预算到点)" if aborted else "")
    return 2 if aborted else 0


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=scout_db.DB_PATH)
    ap.add_argument("--limit", type=int, help="只处理前 N 只主动基金 (调试用)")
    ap.add_argument("--dry-run", action="store_true",
                    help="只拉列表+筛选, 不写库不抓持仓")
    args = ap.parse_args()
    sys.exit(run(args.db, args.limit, args.dry_run))


if __name__ == "__main__":
    main()
