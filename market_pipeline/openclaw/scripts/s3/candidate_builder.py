"""S3 candidate assembly."""

from __future__ import annotations

from strategy_common import calculate_position_pct, is_strategy_allowed


def is_s3_allowed(regime_data: dict) -> tuple[bool, str | None, dict | None]:
    return is_strategy_allowed(regime_data, "S3")


def build_candidate(row: dict, detection: dict, t_bar: dict, regime_data: dict) -> dict:
    close = float(t_bar["close"])
    low = float(t_bar["low"])
    entry_low = round(close * 0.97, 2)
    entry_high = round(close * 1.01, 2)
    stop_loss = round(low * 0.99, 2)
    position_pct, position_calc = calculate_position_pct(
        regime_data,
        extra_multiplier=0.8,
        extra_label="S3首板折扣",
    )

    signal = detection["signal"]
    return {
        "code": row["code"],
        "name": row.get("name") or row["code"],
        "industry": row.get("industry") or "未知",
        "signal": signal,
        "t_bar": {
            "open": t_bar["open"],
            "high": t_bar["high"],
            "low": t_bar["low"],
            "close": t_bar["close"],
            "pct_chg": t_bar["pct_chg"],
        },
        "entry": {
            "zone_low": entry_low,
            "zone_high": entry_high,
            "rule": "T 日收盘 -3% ~ +1%",
        },
        "stop_loss": {"price": stop_loss, "rule": "T 日最低价下方 1%"},
        "exit_rule": "T+1 不涨停或炸板退出, 封板持有到 T+2",
        "position_pct": position_pct,
        "position_calc": position_calc,
    }
