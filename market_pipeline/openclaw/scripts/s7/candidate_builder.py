"""S7 candidate assembly helpers."""

from __future__ import annotations


def build_candidate(
    code: str,
    name: str,
    industry: str,
    detection: dict,
    t_bar: dict,
    regime_data: dict,
    config: dict,
    rank: int,
    active_exposure_pct: float = 0.0,
) -> dict:
    signal = detection["signal"]
    close = float(t_bar["close"])
    stop_pct = float(config["stop_pct"])
    take_profit_pct = float(config["take_profit_pct"])
    position_pct, position_calc = _position_size(config, active_exposure_pct)

    return {
        "date": t_bar["date"],
        "code": code,
        "name": name or code,
        "industry": industry or "未知",
        "mode": config.get("mode") or "paper_observe",
        "rank": rank,
        "signal": signal,
        "t_bar": {
            "open": t_bar["open"],
            "high": t_bar["high"],
            "low": t_bar["low"],
            "close": t_bar["close"],
            "pct_chg": t_bar["pct_chg"],
            "amount": t_bar.get("amount"),
        },
        "entry": {
            "zone_low": round(close * 0.80, 2),
            "zone_high": round(close * 1.20, 2),
            "rule": "T+1 open entry; reject one-word limit-up and down-limit open",
        },
        "stop_loss": {
            "price": round(close * (1 + stop_pct / 100), 2),
            "rule": f"verification recalculates hard stop from actual entry {stop_pct:+.0f}%",
        },
        "take_profit": {
            "price": round(close * (1 + take_profit_pct / 100), 2),
            "rule": f"verification recalculates take-profit from actual entry {take_profit_pct:+.0f}%",
        },
        "exit_rule": "T+1/T+2 intraday stop/take-profit; otherwise T+2 close; limit-down blocked exit postpones",
        "position_pct": round(position_pct, 4),
        "position_calc": position_calc,
        "regime_risk_note": _regime_risk_note(regime_data),
    }


def build_regime_observation(regime_data: dict) -> dict:
    code = regime_data.get("regime_code") or "UNKNOWN"
    confidence = regime_data.get("confidence") or "unknown"
    parts = [f"regime={code}", f"confidence={confidence}", "S7 regime is advisory only"]
    if regime_data.get("switched"):
        parts.append("switched")
    if regime_data.get("emergency_switch"):
        parts.append("emergency_switch")
    return {"note": "; ".join(parts), "decision_owner": "agent", "hard_gate": False}


def _position_size(config: dict, active_exposure_pct: float) -> tuple[float, str]:
    total_cap = float(config.get("position_total_cap_pct", 0.20))
    single_cap = float(config.get("position_single_cap_pct", 0.04))
    max_candidates = max(int(config.get("max_candidates") or 1), 1)
    overlap_buffer = max(int(config.get("position_overlap_buffer") or 1), 1)
    planned_slots = max(max_candidates * overlap_buffer, 1)
    base_equal = total_cap / planned_slots
    remaining = max(total_cap - float(active_exposure_pct or 0.0), 0.0)
    remaining_equal = remaining / max_candidates
    position_pct = min(single_cap, base_equal, remaining_equal)
    calc = (
        f"min(single_cap={single_cap:.4f}, total_cap/{planned_slots}={base_equal:.4f}, "
        f"remaining/{max_candidates}={remaining_equal:.4f}) = {position_pct:.4f}; "
        f"active_exposure={active_exposure_pct:.4f}, total_cap={total_cap:.4f}"
    )
    return position_pct, calc


def _regime_risk_note(regime_data: dict) -> str | None:
    notes = []
    code = regime_data.get("regime_code")
    if code and code != "BEAR":
        notes.append(f"regime={code}, S7 needs agent judgment")
    if regime_data.get("confidence") == "low":
        notes.append("regime confidence low")
    if regime_data.get("switched"):
        notes.append("regime switched today")
    if regime_data.get("emergency_switch"):
        notes.append("emergency switch today")
    return "; ".join(notes) if notes else None
