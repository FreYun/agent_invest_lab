"""S5 T+1 验证 — 两种模式:

1. live (默认): 当日盘中跑, 用 akshare stock_intraday_em 拿实时分时
   判定: triggered / gap_up_skip / triggered_late / stop_hit / wait
   只适用于"今天验证昨天的 candidate"场景

2. backtest: 历史回放, 多日持仓跟踪 (涨停跟随出场).
   入场判定 (T+1 日): triggered_at_open / triggered_intraday / gap_up_skip / gap_down_skip
   出场判定 (T+1 ~ T+N 每日, 优先级):
     stop_hit          当日 low ≤ stop_loss
     limit_up_held     当日 close ≥ up_limit (封涨停, 持有)
     limit_up_broken   当日 high ≥ up_limit 但 close < up_limit (炸板, 卖出)
     close_hold        其余 (持有, 进入下一日)
     max_hold_reached  达到最大持仓天数仍未出, 按当日 close 强制平仓
   用于在已有历史数据上跑 "T 日选股 → 涨停跟随" 真实检验

数据 I/O 全部走 market.db:
    读: s5_select_runs / s5_candidates (通过 db_writer.read_select_run)
    写: s5_verifications (通过 db_writer.write_verification)

用法:
    python3 verify.py --date=2026-04-09                   # live
    python3 verify.py --date=2026-04-09 --mode=backtest   # backtest
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from data_fetcher import fetch_intraday, shift_trading_days, fetch_klines_batch


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


MAX_HOLD_DAYS = 15  # 涨停跟随最大持仓天数, 超出强制平仓


def _fetch_followup_bars(code: str, t1_date: str, n_days: int = MAX_HOLD_DAYS) -> list:
    """拉 T+1 起最多 n 个交易日的 K 线 (含 up_limit), 按日期升序."""
    import sqlite3
    from datetime import datetime, timedelta

    end_date = (datetime.strptime(t1_date, "%Y-%m-%d") + timedelta(days=int(n_days * 1.6))).strftime("%Y%m%d")
    t1_yyyymmdd = t1_date.replace("-", "")

    conn = sqlite3.connect(__import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db")))
    try:
        ts_row = conn.execute(
            "SELECT DISTINCT ts_code FROM daily WHERE substr(ts_code, 1, 6) = ? LIMIT 1",
            (code,),
        ).fetchone()
        if not ts_row:
            return []
        ts_code = ts_row[0]

        rows = conn.execute(
            """
            SELECT d.trade_date, d.open, d.high, d.low, d.close, l.up_limit
            FROM daily d
            LEFT JOIN stk_limit l ON d.trade_date = l.trade_date AND d.ts_code = l.ts_code
            WHERE d.ts_code = ? AND d.trade_date >= ? AND d.trade_date <= ?
            ORDER BY d.trade_date ASC
            LIMIT ?
            """,
            (ts_code, t1_yyyymmdd, end_date, n_days),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "date": f"{r[0][:4]}-{r[0][4:6]}-{r[0][6:8]}",
            "open": r[1], "high": r[2], "low": r[3], "close": r[4],
            "up_limit": r[5],
        }
        for r in rows
    ]


def verify_one_backtest(candidate: dict, t1_bar: dict, followup_bars: list = None) -> dict:
    """历史回放: T+1 入场判定 + 多日涨停跟随出场。

    阶段 1 — 入场判定 (open-based, 仅 T+1):
      gap_up_skip        T+1 open > entry_high
      triggered_at_open  open ∈ [entry_low, entry_high] (买入价 = open)
      triggered_intraday open < entry_low 且 day high ≥ entry_low (买入价 = entry_low)
      gap_down_skip      open < entry_low 且 day high < entry_low (全天没回到入场区)

    阶段 2 — 多日跟踪 (T+1 ~ T+N), 每日按优先级判定:
      stop_hit         当日 low ≤ stop_loss → 止损出, exit = stop_loss
      limit_up_held    当日 close ≥ up_limit (封涨停) → 持有进入下一日
      limit_up_broken  当日 high ≥ up_limit 但 close < up_limit (炸板) → 卖出 close
      close_hold       否则持有进入下一日
      max_hold_reached 第 N 日仍持仓, 按当日 close 强制平仓

    Args:
        candidate: select.py 输出的单只 candidate
        t1_bar: T+1 当天的日线 bar (含 up_limit)
        followup_bars: T+2 起的后续 bar 列表, 升序; 为空则只看 T+1 当日
    """
    entry_low = candidate["entry"]["zone_low"]
    entry_high = candidate["entry"]["zone_high"]
    stop_loss = candidate["stop_loss"]["price"]

    t1_open = t1_bar["open"]
    t1_high = t1_bar["high"]
    t1_low = t1_bar["low"]
    t1_close = t1_bar["close"]

    # 阶段 1: 入场判定
    entry_price = None
    if t1_open > entry_high:
        return {
            "status": "gap_up_skip",
            "entry_price": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_pct": None,
            "t1_open": t1_open,
            "t1_high": t1_high,
            "t1_low": t1_low,
            "t1_close": t1_close,
            "hold_days": 0,
            "exit_date": None,
            "note": f"T+1 开盘 {t1_open:.2f} > 入场区上沿 {entry_high:.2f}, 放弃",
        }

    if entry_low <= t1_open <= entry_high:
        entry_price = t1_open
        entry_status = "triggered_at_open"
        entry_note = f"T+1 开盘 {t1_open:.2f} 命中入场区, 买入价 = {entry_price:.2f}"
    elif t1_open < entry_low and t1_high >= entry_low:
        entry_price = entry_low  # 保守: 按入场区下沿回踩买
        entry_status = "triggered_intraday"
        entry_note = f"T+1 低开 {t1_open:.2f}, 日内最高 {t1_high:.2f} 回到入场区, 买入价 = {entry_price:.2f}"
    else:
        return {
            "status": "gap_down_skip",
            "entry_price": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_pct": None,
            "t1_open": t1_open,
            "t1_high": t1_high,
            "t1_low": t1_low,
            "t1_close": t1_close,
            "hold_days": 0,
            "exit_date": None,
            "note": (
                f"T+1 低开 {t1_open:.2f} 且日内最高 {t1_high:.2f} < 入场区下沿 {entry_low:.2f}, "
                f"全天未进入入场区"
            ),
        }

    # 阶段 2: 多日涨停跟随
    all_bars = [t1_bar] + (followup_bars or [])
    exit_price = None
    exit_reason = None
    final_status = None
    exit_date = None
    hold_days = 0
    daily_log = []

    for bar in all_bars:
        hold_days += 1
        bar_date = bar.get("date")
        b_open, b_high, b_low, b_close = bar["open"], bar["high"], bar["low"], bar["close"]
        up_limit = bar.get("up_limit")

        # 优先级 1: 止损 (T+1 当日同时检查, 因为 stop_loss 是 T 日最低, 可能盘中先破)
        # 跳空跌穿: 开盘已破止损 → 按开盘价成交 (无法在止损价挂出)
        if b_open <= stop_loss:
            exit_price = b_open
            exit_reason = "stop_hit_at_open"
            final_status = "stop_hit_at_open"
            exit_date = bar_date
            daily_log.append(f"{bar_date} 跳空止损 开盘 {b_open:.2f}≤{stop_loss:.2f}")
            break
        if b_low <= stop_loss:
            exit_price = stop_loss
            exit_reason = "stop_hit"
            final_status = "stop_hit"
            exit_date = bar_date
            daily_log.append(f"{bar_date} 止损 {b_low:.2f}≤{stop_loss:.2f}")
            break

        # 优先级 2: 涨停状态判定 (需要 up_limit)
        if up_limit is not None:
            is_close_at_limit = b_close >= up_limit - 0.01
            is_high_touched_limit = b_high >= up_limit - 0.01

            if is_close_at_limit:
                # 封涨停: 持有进入下一日 (即使 hold_days 到 max 也持有)
                daily_log.append(f"{bar_date} 封涨停 close={b_close:.2f}≥up_limit={up_limit:.2f}, 持有")
                if hold_days >= MAX_HOLD_DAYS and bar is all_bars[-1]:
                    # 已是最后一根 bar 仍封停, 按 close 平仓
                    exit_price = b_close
                    exit_reason = "max_hold_reached"
                    final_status = "max_hold_reached"
                    exit_date = bar_date
                    break
                continue

            if is_high_touched_limit and b_close < up_limit - 0.01:
                # 炸板: 卖出
                exit_price = b_close
                exit_reason = "limit_up_broken"
                final_status = "limit_up_broken"
                exit_date = bar_date
                daily_log.append(f"{bar_date} 炸板 high={b_high:.2f}触涨停 close={b_close:.2f}<up_limit, 卖")
                break

        # 优先级 3: 普通持有
        daily_log.append(f"{bar_date} 持有 O={b_open:.2f} H={b_high:.2f} L={b_low:.2f} C={b_close:.2f}")
        if hold_days >= MAX_HOLD_DAYS:
            exit_price = b_close
            exit_reason = "max_hold_reached"
            final_status = "max_hold_reached"
            exit_date = bar_date
            break

    # 兜底: 没有任何 bar 触发出场 (followup 数据不足)
    if exit_price is None:
        last_bar = all_bars[-1]
        exit_price = last_bar["close"]
        exit_reason = "close_hold"
        final_status = "close_hold"
        exit_date = last_bar.get("date")

    pnl_pct = (exit_price - entry_price) / entry_price * 100

    return {
        "status": final_status,
        "entry_status": entry_status,
        "entry_price": round(entry_price, 2),
        "exit_price": round(exit_price, 2),
        "exit_reason": exit_reason,
        "pnl_pct": round(pnl_pct, 2),
        "t1_open": t1_open,
        "t1_high": t1_high,
        "t1_low": t1_low,
        "t1_close": t1_close,
        "hold_days": hold_days,
        "exit_date": exit_date,
        "note": f"{entry_note}; 持仓 {hold_days} 日 → {exit_reason} @ {exit_price:.2f}, 盈亏 {pnl_pct:+.2f}% | " + " → ".join(daily_log),
    }


def verify_one(candidate: dict) -> dict:
    """对单只 candidate 拉分时, 判定状态。

    Returns:
        {"status": str, "open_price": float | None, "current": float | None, "note": str}
    """
    code = candidate["code"]
    entry_low = candidate["entry"]["zone_low"]
    entry_high = candidate["entry"]["zone_high"]
    stop_loss = candidate["stop_loss"]["price"]

    df = fetch_intraday(code)
    if df is None or df.empty:
        return {
            "status": "no_data",
            "open_price": None,
            "current": None,
            "note": "分时数据为空 (可能停牌)",
        }

    # akshare stock_intraday_em 字段: 时间 成交价 手数 买卖盘性质
    # 第一行 = 开盘成交; 最后一行 = 当前最新
    try:
        first_price = float(df.iloc[0]["成交价"])
        last_price = float(df.iloc[-1]["成交价"])
    except Exception as e:
        return {
            "status": "parse_error",
            "open_price": None,
            "current": None,
            "note": f"分时数据解析失败: {e}",
        }

    # 1. 当前价已破止损 — 立即弃单
    if last_price <= stop_loss:
        return {
            "status": "stop_hit",
            "open_price": first_price,
            "current": last_price,
            "note": f"当前价 {last_price:.2f} ≤ 止损 {stop_loss:.2f}",
        }

    # 2. 开盘高开过多
    if first_price > entry_high:
        return {
            "status": "gap_up_skip",
            "open_price": first_price,
            "current": last_price,
            "note": f"开盘 {first_price:.2f} 高于入场区上沿 {entry_high:.2f}, 不追",
        }

    # 3. 开盘在入场区
    if entry_low <= first_price <= entry_high:
        return {
            "status": "triggered",
            "open_price": first_price,
            "current": last_price,
            "note": f"开盘 {first_price:.2f} 在入场区 [{entry_low:.2f}, {entry_high:.2f}], 按计划买入",
        }

    # 4. 开盘低开但当前回到入场区内
    if first_price < entry_low and entry_low <= last_price <= entry_high:
        return {
            "status": "triggered_late",
            "open_price": first_price,
            "current": last_price,
            "note": f"低开 {first_price:.2f} 已回到入场区, 当前 {last_price:.2f}, 可买入",
        }

    # 5. 仍在等待
    return {
        "status": "wait",
        "open_price": first_price,
        "current": last_price,
        "note": f"开盘 {first_price:.2f}, 当前 {last_price:.2f}, 等待回到入场区 [{entry_low:.2f}, {entry_high:.2f}]",
    }


def run_verify(t1_date: str, mode: str = "live"):
    setup_logging()
    from db_writer import read_select_run, write_verification

    t_date = shift_trading_days(t1_date, -1)
    logging.info(f"===== S5 verify mode={mode} t+1={t1_date} t={t_date} =====")

    cand_payload = read_select_run(t_date)
    if cand_payload is None:
        logging.info(f"s5_select_runs 无 {t_date} 记录, 跳过")
        write_verification(t1_date, t_date, [], mode)
        return []

    candidates = cand_payload.get("candidates", [])
    if not candidates:
        logging.info("T 日无 candidate, 跳过")
        write_verification(t1_date, t_date, [], mode)
        return []

    results = []

    if mode == "backtest":
        for cand in candidates:
            code = cand["code"]
            # 拉 T+1 起最多 MAX_HOLD_DAYS 个交易日 (含 up_limit)
            bars = _fetch_followup_bars(code, t1_date, n_days=MAX_HOLD_DAYS)
            if not bars:
                verification = {
                    "status": "no_data",
                    "entry_price": None,
                    "exit_price": None,
                    "pnl_pct": None,
                    "hold_days": 0,
                    "exit_date": None,
                    "note": f"T+1 {t1_date} 起无 K 线数据 (停牌? 节假日?)",
                }
            else:
                t1_bar = bars[0]
                followup_bars = bars[1:]
                verification = verify_one_backtest(cand, t1_bar, followup_bars)
            logging.info(f"  {code} {cand['name']}: {verification['status']} pnl={verification.get('pnl_pct')} hold={verification.get('hold_days')}")
            results.append({"candidate": cand, "verification": verification})
    else:
        for cand in candidates:
            verification = verify_one(cand)
            logging.info(f"  {cand['code']} {cand['name']}: {verification['status']}")
            results.append({"candidate": cand, "verification": verification})

    write_verification(t1_date, t_date, results, mode)

    print(f"\n📊 T+1 验证结果 mode={mode} ({t1_date}):")
    for r in results:
        c = r["candidate"]
        v = r["verification"]
        pnl = v.get("pnl_pct")
        pnl_str = f" pnl={pnl:+.2f}%" if pnl is not None else ""
        print(f"  {c['code']} {c['name']}: {v['status']}{pnl_str}")
    if mode == "backtest":
        # 汇总统计
        triggered = [r for r in results if r["verification"].get("pnl_pct") is not None]
        if triggered:
            pnls = [r["verification"]["pnl_pct"] for r in triggered]
            avg = sum(pnls) / len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            print(f"\n  汇总: {len(triggered)}/{len(results)} 触发, 胜率 {wins}/{len(triggered)} ({wins*100/len(triggered):.0f}%), 平均盈亏 {avg:+.2f}%")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="T+1 日期 YYYY-MM-DD")
    ap.add_argument(
        "--mode",
        choices=["live", "backtest"],
        default="live",
        help="live=实时分时(akshare stock_intraday_em); backtest=历史日线回放(research-mcp)",
    )
    args = ap.parse_args()

    run_verify(args.date, mode=args.mode)


if __name__ == "__main__":
    main()
