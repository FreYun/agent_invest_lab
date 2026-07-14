"""S6 candidate assembly and regime gate helpers."""

from __future__ import annotations

from strategy_common import calculate_position_pct


def is_s6_allowed(regime_data: dict, config: dict) -> tuple[bool, str | None, str]:
    mode = config.get("mode") or "trend"
    allowed_regimes = _normalize_allow_regimes(config.get("allow_regimes"))
    regime_code = regime_data.get("regime_code")
    if allowed_regimes and regime_code not in allowed_regimes:
        return False, f"regime={regime_data.get('regime')}, S6 {mode} only allows {sorted(allowed_regimes)}", mode

    if config.get("min_score_volume_trend") is not None:
        value = regime_data.get("score", {}).get("breakdown", {}).get("volume_trend")
        if value is None or int(value) < int(config["min_score_volume_trend"]):
            return False, f"score_volume_trend={value}, need >= {config['min_score_volume_trend']}", mode

    if config.get("min_total_score") is not None:
        value = regime_data.get("score", {}).get("total")
        if value is None or int(value) < int(config["min_total_score"]):
            return False, f"total_score={value}, need >= {config['min_total_score']}", mode

    return True, None, mode


def build_candidate(
    code: str,
    name: str,
    industry: str,
    detection: dict,
    t_bar: dict,
    regime_data: dict,
    config: dict,
) -> dict:
    signal = detection["signal"]
    close = float(t_bar["close"])
    entry_low_pct = float(config["entry_low_pct"])
    entry_high_pct = float(config["entry_high_pct"])
    stop_pct = float(config["stop_pct"])
    entry_low = round(close * (1 + entry_low_pct / 100), 2)
    entry_high = round(close * (1 + entry_high_pct / 100), 2)
    stop_loss = round(close * (1 + stop_pct / 100), 2)
    position_pct, position_calc = calculate_position_pct(
        regime_data,
        extra_multiplier=float(config.get("position_multiplier", 0.7)),
        extra_label=f"S6 {config.get('mode', 'trend')} multiplier",
    )

    max_hold_days = int(config["max_hold_days"])
    mode = config.get("mode") or "trend"
    return {
        "code": code,
        "name": name or code,
        "industry": industry or "未知",
        "mode": mode,
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
            "rule": f"T+1 open must be within T close {entry_low_pct:+.0f}%~{entry_high_pct:+.0f}%",
        },
        "stop_loss": {
            "price": stop_loss,
            "rule": f"research stop from T close {stop_pct:+.0f}%; verification recalculates from actual entry",
        },
        "exit_rule": f"T+2 sellable; stop / close below MA5 / max hold {max_hold_days} trading days",
        "cooldown_days": int(config.get("cooldown_days") or 0),
        "position_pct": position_pct,
        "position_calc": position_calc,
    }


def _normalize_allow_regimes(value) -> set[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value)
    return {str(item).strip().upper() for item in items if str(item).strip()}
