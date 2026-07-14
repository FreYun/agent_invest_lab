"""S5 candidate 组装 — 把信号判定的结果组装成下游契约。

包含:
  - build_candidate: 单只股 dict (价格/止损/仓位/解释)
  - calculate_position_pct: 仓位乘数链
  - is_s5_allowed: regime 检查
"""

from __future__ import annotations

from typing import Optional


def is_s5_allowed(regime_data: dict) -> tuple:
    """检查 regime 是否允许 S5。

    Returns:
        (allowed: bool, reason: str | None)
    """
    playbook = regime_data.get("playbook", {})
    recommended = playbook.get("recommended", [])
    rec_ids = [item.get("id") for item in recommended]
    if "S5" not in rec_ids:
        regime_name = regime_data.get("regime", regime_data.get("regime_code", "UNKNOWN"))
        return False, f"regime={regime_name}, S5 not in playbook.recommended ({rec_ids})"
    return True, None


def calculate_position_pct(regime_data: dict) -> tuple:
    """根据 regime + 乘数链计算单票仓位。

    乘数链 (来自 classifier mapping.md):
        base = playbook.position_limit.single
        if confidence == "low":         base *= 0.5
        elif confidence == "medium":    base *= 0.8
        if switched:                    base *= 0.8
        if emergency_switch:            base *= 0.7

    Returns:
        (final_pct: float, calc_string: str)
    """
    playbook = regime_data.get("playbook", {})
    pos_limit = playbook.get("position_limit", {})
    base = float(pos_limit.get("single", 0.10))

    parts = [f"{base:.2f} (base)"]
    final = base

    confidence = regime_data.get("confidence", "high")
    if confidence == "low":
        final *= 0.5
        parts.append("× 0.5 (confidence=low)")
    elif confidence == "medium":
        final *= 0.8
        parts.append("× 0.8 (confidence=medium)")

    if regime_data.get("switched"):
        final *= 0.8
        parts.append("× 0.8 (switched)")

    if regime_data.get("emergency_switch"):
        final *= 0.7
        parts.append("× 0.7 (emergency_switch)")

    return round(final, 4), " ".join(parts) + f" = {final:.4f}"


def build_candidate(
    code: str,
    name: str,
    industry: str,
    detection: dict,
    klines: list,
    t_date: str,
    regime_data: dict,
) -> dict:
    """组装单只 candidate dict (signal_detector.detect_s5 返回 passed=True 才调)。

    Args:
        code, name, industry: 股票基本信息
        detection: signal_detector.detect_s5 的返回 (passed=True)
        klines: 该股 K 线 (用于读 T 日 OHLC)
        t_date: T 日 'YYYY-MM-DD'
        regime_data: classifier 输出 JSON (用于算仓位)

    Returns:
        candidate dict
    """
    # 从 K 线找 T 日的 bar
    t_bar = next((b for b in klines if b["date"] == t_date), None)
    if t_bar is None:
        raise ValueError(f"K 线中无 T 日 {t_date}, code={code}")

    t_close = t_bar["close"]
    t_low = t_bar["low"]
    t_open = t_bar["open"]
    t_high = t_bar["high"]

    # 一字板检测: open == high == low == close (整天封住, T+1 无法低开进, 拒掉)
    if abs(t_high - t_low) < 0.01 and abs(t_close - t_open) < 0.01:
        raise ValueError(f"{code} T 日一字涨停, T+1 无法进场")

    # 入场区间: T 日收盘 -3% ~ +2%
    entry_low = round(t_close * 0.97, 2)
    entry_high = round(t_close * 1.02, 2)

    # 止损 = T 日最低; 但若 T 日 close == low (尾盘急拉接近涨停, 止损位太高没意义),
    # 改用 T 日最低 - 2% 给出缓冲
    if abs(t_close - t_low) < 0.01:
        stop_loss = round(t_low * 0.98, 2)
        stop_rule = "T 日最低 × 0.98 (T 日尾盘急拉, 给 2% 缓冲)"
    else:
        stop_loss = t_low
        stop_rule = "T 日最低"

    # 仓位
    position_pct, position_calc = calculate_position_pct(regime_data)

    return {
        "code": code,
        "name": name,
        "industry": industry,
        "dragon_peak": detection["dragon_peak"],
        "cooldown": detection["cooldown"],
        "rebound": detection["rebound"],
        "entry": {
            "zone_low": entry_low,
            "zone_high": entry_high,
            "rule": "T 日收盘 -3% ~ +2%",
        },
        "stop_loss": {"price": stop_loss, "rule": stop_rule},
        # 出场: 涨停跟随 (封涨停未开 → 持有, 炸板 → 卖出, 跌破止损 → 出);
        # 不再使用固定百分比止盈, target_1/2 字段保留为 None 仅作向后兼容
        "target_1": {"price": None, "pct": None},
        "target_2": {"price": None, "pct": None},
        "exit_rule": "涨停跟随: 封停持有, 炸板/破止损卖出",
        "position_pct": position_pct,
        "position_calc": position_calc,
    }
