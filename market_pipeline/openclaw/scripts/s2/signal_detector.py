"""S2 龙头板接力信号判定."""

from __future__ import annotations

from typing import Optional

from strategy_common import parse_seal_time


DEFAULTS = {
    "hot_industry_top_n": 5,
    "min_industry_limit_count": 2,
    "min_streak": 3,
    "max_streak": 8,
    "max_blast_count": 1,
    "latest_first_seal_time": 143000,
    "min_turnover_rate": 3.0,
    "max_turnover_rate": 35.0,
    "reject_one_word_board": True,
    "require_hot_industry": False,
    "require_seal_time": False,
    "require_turnover_rate": False,
    "max_candidates": 5,
}


def detect_s2(row: dict, hot_by_industry: dict[str, dict], t_bar: Optional[dict], config: Optional[dict] = None) -> dict:
    cfg = {**DEFAULTS, **(config or {})}
    industry = row.get("industry") or "未知"
    hot = hot_by_industry.get(industry)

    if hot is None:
        if cfg["require_hot_industry"]:
            return _reject("sector", f"{industry} 不在 T 日热门行业 top{cfg['hot_industry_top_n']}")
        hot = {"rank": None, "limit_count": None}
    elif int(hot.get("limit_count") or 0) < cfg["min_industry_limit_count"]:
        return _reject("sector", f"{industry} 涨停数 {hot.get('limit_count')} < {cfg['min_industry_limit_count']}")

    streak = int(row.get("streak") or 0)
    if streak < int(cfg["min_streak"]):
        return _reject("height_board", f"连板数={streak}, 低于 {cfg['min_streak']} 板")
    if streak > int(cfg["max_streak"]):
        return _reject("height_board", f"连板数={streak}, 高于 {cfg['max_streak']} 板, 高位接力风险过大")

    blast_count = int(row.get("blast_count") or 0)
    if row.get("blast_count") is not None and blast_count > cfg["max_blast_count"]:
        return _reject("seal_quality", f"炸板次数 {blast_count} > {cfg['max_blast_count']}")

    first_seal = parse_seal_time(row.get("first_seal_time"))
    if first_seal is None:
        if cfg["require_seal_time"]:
            return _reject("seal_quality", "缺首次封板时间")
    elif first_seal > cfg["latest_first_seal_time"]:
        return _reject("seal_quality", f"首次封板 {row.get('first_seal_time')} 晚于 14:00")

    turnover_raw = row.get("turnover_rate")
    turnover = float(turnover_raw or 0)
    if turnover_raw is None:
        if cfg["require_turnover_rate"]:
            return _reject("liquidity", "缺换手率")
    else:
        if turnover < cfg["min_turnover_rate"]:
            return _reject("liquidity", f"换手率 {turnover:.2f}% < {cfg['min_turnover_rate']}%")
        if turnover > cfg["max_turnover_rate"]:
            return _reject("liquidity", f"换手率 {turnover:.2f}% > {cfg['max_turnover_rate']}%")

    if t_bar is None:
        return _reject("daily_bar", "缺 T 日 K 线")

    if cfg["reject_one_word_board"]:
        if abs(t_bar["high"] - t_bar["low"]) < 0.01 and abs(t_bar["close"] - t_bar["open"]) < 0.01:
            return _reject("liquidity", "T 日一字板, T+1 接力可执行性差")

    score = _score(row, hot, first_seal, t_bar)
    return {
        "passed": True,
        "stage_failed": None,
        "reject_reason": None,
        "signal": {
            "sector_rank": hot.get("rank"),
            "sector_limit_count": hot.get("limit_count"),
            "streak": streak,
            "first_seal_time": row.get("first_seal_time"),
            "last_seal_time": row.get("last_seal_time"),
            "blast_count": blast_count,
            "seal_amount": row.get("seal_amount"),
            "turnover_rate": turnover,
            "amount": row.get("amount"),
            "score": score,
        },
    }


def _score(row: dict, hot: dict, first_seal: int | None, t_bar: dict) -> float:
    streak = int(row.get("streak") or 0)
    streak_score = 35 if 3 <= streak <= 5 else 25 if streak <= 7 else 12
    sector_score = max(0, 6 - int(hot.get("rank") or 5)) * 10 if hot.get("rank") else 5
    limit_count_score = min(float(hot.get("limit_count") or 0), 10.0) * 3
    time_score = 20 if first_seal and first_seal <= 103000 else 12 if first_seal and first_seal <= 113000 else 5
    blast_score = 10 if row.get("blast_count") is not None and int(row.get("blast_count") or 0) == 0 else 5
    turnover = float(row.get("turnover_rate") or 0)
    turnover_score = 10 if 5 <= turnover <= 25 else 5 if turnover else 3
    amount_score = min(float(row.get("amount") or 0) / 1_000_000_000, 10.0)
    body_score = 5 if t_bar["close"] >= t_bar["open"] else 0
    return round(streak_score + sector_score + limit_count_score + time_score + blast_score + turnover_score + amount_score + body_score, 2)


def _reject(stage: str, reason: str) -> dict:
    return {
        "passed": False,
        "stage_failed": stage,
        "reject_reason": reason,
        "signal": None,
    }
