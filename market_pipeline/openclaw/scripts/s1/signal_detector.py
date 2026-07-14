"""S1 平台突破信号判定."""

from __future__ import annotations

from typing import Optional

from strategy_common import moving_average


DEFAULTS = {
    "lookback_days": 90,
    "base_window": 20,
    "max_base_range_pct": 14.0,
    "min_breakout_pct": 2.0,
    "min_volume_ratio": 1.5,
    "min_amount_yi": 5.0,
    "min_ret20_pct": 10.0,
    "min_rel20_pct": 5.0,
    "entry_low_pct": -3.0,
    "entry_high_pct": 2.0,
    "stop_pct": -8.0,
    "max_hold_days": 2,
    "max_candidates": 2,
    "index_code": "000852.SH",
    "allow_regimes": "STRONG_RANGE",
    "mode": "platform_breakout_v0",
    "reject_one_word_breakout": True,
    "position_multiplier": 0.5,
}


def detect_s1(
    klines: list[dict],
    t_date: str,
    index_closes: dict[str, float],
    config: Optional[dict] = None,
) -> dict:
    cfg = {**DEFAULTS, **(config or {})}
    bars = [b for b in klines if b["date"] <= t_date]
    base_window = int(cfg["base_window"])
    min_history = max(61, base_window + 21)
    if len(bars) < min_history:
        return _reject("history", f"K 线不足 {min_history} 日: {len(bars)}")
    if bars[-1]["date"] != t_date:
        return _reject("daily_bar", f"缺 T 日 K 线 {t_date}")

    t = bars[-1]
    if t["close"] <= 0:
        return _reject("daily_bar", "T 日收盘价无效")
    if cfg["reject_one_word_breakout"] and _is_one_word_bar(t):
        return _reject("liquidity", "T 日一字涨停/极窄幅, T+1 可执行性差")

    amount_yi = t["amount"] / 100_000_000
    if amount_yi < float(cfg["min_amount_yi"]):
        return _reject("amount", f"成交额 {amount_yi:.2f} 亿 < {cfg['min_amount_yi']} 亿")

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    ma20 = moving_average(closes, 20)
    ma60 = moving_average(closes, 60)
    if ma20 is None or ma60 is None:
        return _reject("ma", "MA20/MA60 数据不足")
    if not (t["close"] >= ma20 >= ma60):
        return _reject("trend", f"趋势不满足 close>=MA20>=MA60 ({t['close']:.2f}/{ma20:.2f}/{ma60:.2f})")

    prev20_close = closes[-21]
    ret20 = (t["close"] / prev20_close - 1) * 100 if prev20_close else None
    if ret20 is None or ret20 < float(cfg["min_ret20_pct"]):
        return _reject("strength", f"20日涨幅 {ret20 or 0:.2f}% < {cfg['min_ret20_pct']}%")

    rel20 = _relative_ret20(bars, index_closes)
    if rel20 is None:
        return _reject("index", f"缺 {cfg['index_code']} 20日相对强度数据")
    if rel20 < float(cfg["min_rel20_pct"]):
        return _reject("strength", f"20日相对强度 {rel20:.2f}% < {cfg['min_rel20_pct']}%")

    base = bars[-base_window - 1:-1]
    if len(base) != base_window:
        return _reject("base", f"平台窗口不足 {base_window} 日")
    base_high = max(b["high"] for b in base)
    base_low = min(b["low"] for b in base)
    if base_low <= 0:
        return _reject("base", "平台最低价无效")
    base_range_pct = (base_high - base_low) / base_low * 100
    if base_range_pct > float(cfg["max_base_range_pct"]):
        return _reject("base", f"平台宽度 {base_range_pct:.2f}% > {cfg['max_base_range_pct']}%")

    breakout_pct = (t["close"] / base_high - 1) * 100 if base_high else None
    if breakout_pct is None or breakout_pct < float(cfg["min_breakout_pct"]):
        return _reject("breakout", f"突破幅度 {breakout_pct or 0:.2f}% < {cfg['min_breakout_pct']}%")

    avg_vol5_prev = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else None
    volume_ratio = t["volume"] / avg_vol5_prev if avg_vol5_prev else None
    if volume_ratio is None or volume_ratio < float(cfg["min_volume_ratio"]):
        return _reject("volume", f"量比 {volume_ratio or 0:.2f} < {cfg['min_volume_ratio']}")

    ret60 = (t["close"] / closes[-61] - 1) * 100 if len(closes) >= 61 and closes[-61] else None
    score = _score(breakout_pct, volume_ratio, base_range_pct, amount_yi, ret20, rel20)
    return {
        "passed": True,
        "stage_failed": None,
        "reject_reason": None,
        "signal": {
            "base_window": base_window,
            "base_high": round(base_high, 2),
            "base_low": round(base_low, 2),
            "base_range_pct": round(base_range_pct, 2),
            "breakout_pct": round(breakout_pct, 2),
            "volume_ratio": round(volume_ratio, 2),
            "amount_yi": round(amount_yi, 2),
            "ret20_pct": round(ret20, 2),
            "rel20_pct": round(rel20, 2),
            "ret60_pct": round(ret60, 2) if ret60 is not None else None,
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "score": round(score, 2),
        },
    }


def _relative_ret20(bars: list[dict], index_closes: dict[str, float]) -> float | None:
    t_date = bars[-1]["date"]
    prev_date = bars[-21]["date"]
    idx_now = index_closes.get(t_date)
    idx_prev = index_closes.get(prev_date)
    if not idx_now or not idx_prev:
        return None
    stock_ret20 = (bars[-1]["close"] / bars[-21]["close"] - 1) * 100
    index_ret20 = (idx_now / idx_prev - 1) * 100
    return stock_ret20 - index_ret20


def _is_one_word_bar(bar: dict) -> bool:
    up_limit = bar.get("up_limit")
    if up_limit is not None and bar["close"] < up_limit - 0.01:
        return False
    price_range = abs(bar["high"] - bar["low"])
    body_range = abs(bar["close"] - bar["open"])
    return price_range < 0.01 and body_range < 0.01


def _score(
    breakout_pct: float,
    volume_ratio: float,
    base_range_pct: float,
    amount_yi: float,
    ret20_pct: float,
    rel20_pct: float,
) -> float:
    breakout_score = min(max(breakout_pct, 0), 6) * 6
    volume_score = min(max(volume_ratio, 0), 3) * 14
    base_score = max(0, 24 - base_range_pct)
    amount_score = min(amount_yi, 10) * 2
    strength_score = min(max(ret20_pct - 10, 0), 20) * 0.6
    relative_score = min(max(rel20_pct - 5, 0), 15) * 0.8
    return breakout_score + volume_score + base_score + amount_score + strength_score + relative_score


def _reject(stage: str, reason: str) -> dict:
    return {
        "passed": False,
        "stage_failed": stage,
        "reject_reason": reason,
        "signal": None,
    }
