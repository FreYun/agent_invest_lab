"""S1 candidate assembly."""

from __future__ import annotations

from strategy_common import calculate_position_pct


def is_s1_allowed(regime_data: dict, config: dict) -> tuple[bool, str | None, str]:
    mode = config.get("mode") or "platform_breakout_v0"
    allowed_regimes = _normalize_allow_regimes(config.get("allow_regimes"))
    regime_code = regime_data.get("regime_code")
    if allowed_regimes and regime_code not in allowed_regimes:
        return False, f"regime={regime_data.get('regime')}, S1 v0 only allows {sorted(allowed_regimes)}", mode
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
    entry_low = round(close * (1 + entry_low_pct / 100), 2)
    entry_high = round(close * (1 + entry_high_pct / 100), 2)
    stop_loss = round(max(signal["base_low"] * 0.98, close * (1 + float(config["stop_pct"]) / 100)), 2)
    failure_line = round(signal["base_high"], 2)
    position_pct, position_calc = calculate_position_pct(
        regime_data,
        extra_multiplier=float(config.get("position_multiplier", 0.5)),
        extra_label="S1平台突破观察版折扣",
    )

    return {
        "code": code,
        "name": name or code,
        "industry": industry or "未知",
        "mode": config.get("mode") or "platform_breakout_v0",
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
            "rule": f"T+1 开盘价相对 T 收盘 {entry_low_pct:+.0f}% ~ {entry_high_pct:+.0f}%",
        },
        "stop_loss": {
            "price": stop_loss,
            "rule": f"max(平台低点*0.98, T收盘{float(config['stop_pct']):+.0f}%)；实盘按入场价重算",
        },
        "failure_line": {
            "price": failure_line,
            "rule": "入场后收盘跌回平台上沿下方则失败退出",
        },
        "exit_rule": f"T+2 起可卖；止损/跌回平台上沿/最多持有 {int(config['max_hold_days'])} 日",
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
