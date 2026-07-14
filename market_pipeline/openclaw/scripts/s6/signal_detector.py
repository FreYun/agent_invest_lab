"""S6 trend-hold signal detection.

S6 is a short trend-continuation strategy. The production candidate is the
long-horizon robust trend mode; defensive mode is retained as observation only.
"""

from __future__ import annotations

from typing import Optional

from strategy_common import moving_average


TREND_DEFAULTS = {
    "mode": "trend",
    "lookback_days": 90,
    "min_amount_yi": 5.0,
    "min_ret20_pct": 10.0,
    "min_rel20_pct": 5.0,
    "min_dist_ma5_pct": 0.0,
    "max_dist_ma5_pct": 3.5,
    "min_ma10_slope5_pct": 0.5,
    "min_pct_chg": None,
    "entry_low_pct": -3.0,
    "entry_high_pct": 1.0,
    "stop_pct": -5.0,
    "max_hold_days": 5,
    "cooldown_days": 20,
    "max_candidates": 2,
    "index_code": "000852.SH",
    "allow_regimes": "STRONG_BULL",
    "min_score_volume_trend": 1,
    "position_multiplier": 0.35,
}


DEFENSIVE_DEFAULTS = {
    "mode": "defensive_observe",
    "lookback_days": 90,
    "min_amount_yi": 8.0,
    "min_ret20_pct": 10.0,
    "min_rel20_pct": 5.0,
    "min_dist_ma5_pct": 0.0,
    "max_dist_ma5_pct": 3.0,
    "min_ma10_slope5_pct": None,
    "min_pct_chg": -1.0,
    "entry_low_pct": -3.0,
    "entry_high_pct": 1.0,
    "stop_pct": -7.0,
    "max_hold_days": 3,
    "cooldown_days": 0,
    "max_candidates": 2,
    "index_code": "000852.SH",
    "allow_regimes": "WEAK_RANGE",
    "min_total_score": -3,
    "position_multiplier": 0.2,
}


DEFAULTS = TREND_DEFAULTS


def config_for_mode(mode: str, overrides: Optional[dict] = None) -> dict:
    if mode in ("defensive", "defensive_observe"):
        base = DEFENSIVE_DEFAULTS
    elif mode == "trend":
        base = TREND_DEFAULTS
    else:
        raise ValueError(f"unsupported S6 mode: {mode}")
    return {**base, **(overrides or {})}


def detect_s6(
    klines: list[dict],
    t_date: str,
    index_closes: dict[str, float],
    config: Optional[dict] = None,
) -> dict:
    cfg = {**DEFAULTS, **(config or {})}
    bars = [bar for bar in klines if bar["date"] <= t_date]
    min_history = 66
    if len(bars) < min_history:
        return _reject("history", f"K line history less than {min_history}: {len(bars)}")
    if bars[-1]["date"] != t_date:
        return _reject("daily_bar", f"missing T bar {t_date}")

    t_bar = bars[-1]
    close = float(t_bar["close"] or 0)
    if close <= 0:
        return _reject("daily_bar", "invalid T close")
    if _is_one_word_up(t_bar):
        return _reject("liquidity", "T day one-word limit-up, poor T+1 executability")

    amount_yi = float(t_bar["amount"] or 0) / 100_000_000
    if amount_yi < float(cfg["min_amount_yi"]):
        return _reject("amount", f"amount {amount_yi:.2f} yi < {cfg['min_amount_yi']} yi")

    pct_chg = float(t_bar.get("pct_chg") or 0)
    if cfg.get("min_pct_chg") is not None and pct_chg < float(cfg["min_pct_chg"]):
        return _reject("candle", f"T pct_chg {pct_chg:.2f}% < {cfg['min_pct_chg']}%")

    closes = [float(bar["close"] or 0) for bar in bars]
    volumes = [float(bar["volume"] or 0) for bar in bars]
    ma5 = moving_average(closes, 5)
    ma10 = moving_average(closes, 10)
    ma20 = moving_average(closes, 20)
    ma60 = moving_average(closes, 60)
    if ma5 is None or ma10 is None or ma20 is None or ma60 is None:
        return _reject("ma", "MA5/10/20/60 history unavailable")
    if not (close >= ma5 and ma10 >= ma20 >= ma60):
        return _reject("trend", f"trend order failed close/MA5/10/20/60={close:.2f}/{ma5:.2f}/{ma10:.2f}/{ma20:.2f}/{ma60:.2f}")

    dist_ma5 = (close / ma5 - 1) * 100 if ma5 else None
    if dist_ma5 is None or dist_ma5 < float(cfg["min_dist_ma5_pct"]) or dist_ma5 > float(cfg["max_dist_ma5_pct"]):
        return _reject("ma_distance", f"dist_ma5 {dist_ma5 or 0:.2f}% not in {cfg['min_dist_ma5_pct']}~{cfg['max_dist_ma5_pct']}%")

    prev20 = closes[-21]
    prev60 = closes[-61]
    ret20 = (close / prev20 - 1) * 100 if prev20 else None
    ret60 = (close / prev60 - 1) * 100 if prev60 else None
    if ret20 is None or ret20 < float(cfg["min_ret20_pct"]):
        return _reject("strength", f"ret20 {ret20 or 0:.2f}% < {cfg['min_ret20_pct']}%")

    rel20 = _relative_ret20(bars, index_closes)
    if rel20 is None:
        return _reject("index", f"missing {cfg['index_code']} 20-day relative strength")
    if rel20 < float(cfg["min_rel20_pct"]):
        return _reject("strength", f"rel20 {rel20:.2f}% < {cfg['min_rel20_pct']}%")

    ma10_slope5 = None
    if len(closes) >= 15:
        prev_ma10 = sum(closes[-15:-5]) / 10
        ma10_slope5 = (ma10 / prev_ma10 - 1) * 100 if prev_ma10 else None
    min_ma10_slope5 = cfg.get("min_ma10_slope5_pct")
    if min_ma10_slope5 is not None:
        if ma10_slope5 is None or ma10_slope5 < float(min_ma10_slope5):
            return _reject("ma_slope", f"MA10 5-day slope {ma10_slope5 or 0:.2f}% < {min_ma10_slope5}%")

    avg_vol5_prev = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else None
    volume_ratio = t_bar["volume"] / avg_vol5_prev if avg_vol5_prev else None
    score = _score(
        ret20_pct=ret20,
        rel20_pct=rel20,
        ret60_pct=ret60,
        dist_ma5_pct=dist_ma5,
        amount_yi=amount_yi,
        volume_ratio=volume_ratio,
        pct_chg=pct_chg,
    )

    return {
        "passed": True,
        "stage_failed": None,
        "reject_reason": None,
        "signal": {
            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "dist_ma5_pct": round(dist_ma5, 2),
            "ma10_slope5_pct": round(ma10_slope5, 2) if ma10_slope5 is not None else None,
            "amount_yi": round(amount_yi, 2),
            "ret20_pct": round(ret20, 2),
            "rel20_pct": round(rel20, 2),
            "ret60_pct": round(ret60, 2) if ret60 is not None else None,
            "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
            "pct_chg": round(pct_chg, 2),
            "score": round(score, 2),
        },
    }


def _relative_ret20(bars: list[dict], index_closes: dict[str, float]) -> float | None:
    t_date = bars[-1]["date"]
    prev_date = bars[-21]["date"]
    idx_now = index_closes.get(t_date)
    idx_prev = index_closes.get(prev_date)
    if not idx_now or not idx_prev or not bars[-21]["close"]:
        return None
    stock_ret20 = (bars[-1]["close"] / bars[-21]["close"] - 1) * 100
    index_ret20 = (idx_now / idx_prev - 1) * 100
    return stock_ret20 - index_ret20


def _is_one_word_up(bar: dict) -> bool:
    up_limit = bar.get("up_limit")
    return (
        up_limit is not None
        and bar["close"] >= up_limit - 0.01
        and abs(bar["high"] - bar["low"]) < 0.01
        and abs(bar["close"] - bar["open"]) < 0.01
    )


def _score(
    ret20_pct: float,
    rel20_pct: float,
    ret60_pct: float | None,
    dist_ma5_pct: float,
    amount_yi: float,
    volume_ratio: float | None,
    pct_chg: float,
) -> float:
    volume_bonus = max((volume_ratio or 0) - 1.0, 0) * 4
    dist_bonus = max(0, 4.0 - dist_ma5_pct) * 3
    pct_penalty = max(pct_chg - 7.0, 0) * 1.5
    return (
        ret20_pct * 1.35
        + rel20_pct * 1.8
        + (ret60_pct or 0) * 0.25
        + dist_bonus
        + min(amount_yi, 30) * 0.25
        + volume_bonus
        - pct_penalty
    )


def _reject(stage: str, reason: str) -> dict:
    return {
        "passed": False,
        "stage_failed": stage,
        "reject_reason": reason,
        "signal": None,
    }
