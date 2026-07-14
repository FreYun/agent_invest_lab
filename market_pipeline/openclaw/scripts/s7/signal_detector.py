"""S7 oversold-rebound signal detection.

Production candidate is v13 from 2026-05-18/19 exploration:
dist_ma20<=-15 + ret5<=-10 + pct_chg>=0 + amount>=3yi + top5.
Regime fields are advisory metadata in select.py; this module is stock-local.
"""

from __future__ import annotations

from typing import Optional


DEFAULTS = {
    "mode": "paper_observe",
    "lookback_days": 35,
    "min_amount_yi": 3.0,
    "max_dist_ma20_pct": -15.0,
    "max_ret5_pct": -10.0,
    "min_pct_chg": 0.0,
    "max_candidates": 5,
    "stop_pct": -5.0,
    "take_profit_pct": 8.0,
    "max_hold_days": 2,
    "limit_down_extend_days": 5,
    "position_total_cap_pct": 0.20,
    "position_single_cap_pct": 0.04,
    "position_overlap_buffer": 2,
}


def config_with_overrides(overrides: Optional[dict] = None) -> dict:
    return {**DEFAULTS, **(overrides or {})}


def detect_s7(
    klines: list[dict],
    t_date: str,
    code: str | None = None,
    config: Optional[dict] = None,
) -> dict:
    cfg = config_with_overrides(config)
    bars = [bar for bar in klines if bar["date"] <= t_date]
    min_history = 25
    if len(bars) < min_history:
        return _reject("history", f"K line history less than {min_history}: {len(bars)}")
    if bars[-1]["date"] != t_date:
        return _reject("daily_bar", f"missing T bar {t_date}")

    t_bar = bars[-1]
    close = float(t_bar.get("close") or 0)
    if close <= 0:
        return _reject("daily_bar", "invalid T close")

    normalized_code = str(code or t_bar.get("code") or "")
    if _is_bj_or_kechuang(normalized_code):
        return _reject("universe", "exclude BJ / STAR Market 688")

    amount_yi = float(t_bar.get("amount") or 0) / 100_000_000
    if amount_yi < float(cfg["min_amount_yi"]):
        return _reject("amount", f"amount {amount_yi:.2f} yi < {cfg['min_amount_yi']} yi")

    pct_chg = float(t_bar.get("pct_chg") or 0)
    if pct_chg < float(cfg["min_pct_chg"]):
        return _reject("candle", f"pct_chg {pct_chg:.2f}% < {cfg['min_pct_chg']}%")

    closes = [float(bar.get("close") or 0) for bar in bars]
    ma20 = _moving_average(closes, 20)
    if ma20 is None or ma20 <= 0:
        return _reject("ma20", "MA20 unavailable")
    dist_ma20 = (close / ma20 - 1) * 100
    if dist_ma20 > float(cfg["max_dist_ma20_pct"]):
        return _reject("dist_ma20", f"dist_ma20 {dist_ma20:.2f}% > {cfg['max_dist_ma20_pct']}%")

    prev5 = closes[-6] if len(closes) >= 6 else None
    ret5 = (close / prev5 - 1) * 100 if prev5 else None
    if ret5 is None or ret5 > float(cfg["max_ret5_pct"]):
        return _reject("ret5", f"ret5 {ret5 or 0:.2f}% > {cfg['max_ret5_pct']}%")

    score = _score(dist_ma20=dist_ma20, ret5=ret5, amount_yi=amount_yi, pct_chg=pct_chg)
    return {
        "passed": True,
        "stage_failed": None,
        "reject_reason": None,
        "signal": {
            "ma20": round(ma20, 2),
            "dist_ma20_pct": round(dist_ma20, 2),
            "ret5_pct": round(ret5, 2),
            "amount_yi": round(amount_yi, 2),
            "pct_chg": round(pct_chg, 2),
            "score": round(score, 2),
            "regime_quality_note": None,
        },
    }


def _moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    sample = values[-window:]
    if any(v <= 0 for v in sample):
        return None
    return sum(sample) / window


def _is_bj_or_kechuang(code: str) -> bool:
    normalized = code.upper()
    return normalized.endswith(".BJ") or normalized.startswith("688")


def _score(dist_ma20: float, ret5: float, amount_yi: float, pct_chg: float) -> float:
    # Lower dist_ma20 is better; ret5 compression adds rebound elasticity.
    dist_component = abs(min(dist_ma20, 0)) * 2.0
    ret_component = abs(min(ret5, 0)) * 1.2
    liquidity_component = min(amount_yi, 30.0) * 0.5
    pct_component = max(pct_chg, 0) * 0.8
    return dist_component + ret_component + liquidity_component + pct_component


def _reject(stage: str, reason: str) -> dict:
    return {
        "passed": False,
        "stage_failed": stage,
        "reject_reason": reason,
        "signal": None,
    }
