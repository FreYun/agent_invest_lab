"""逃生门改进规则 v2 (实验性, 仅用于 A/B 回放对比)。

相对 regime_rules.check_emergency_hatch 的改动 (spec §6 修订候选):

(A) 冷静期
    前一次触发后 3 日内不再触发, 避免"连续下跌把 regime 反复打压"。
    v1 数据: 148 次下行触发中, 46% 的间隔 ≤3 日, 属无效反复触发。

(B) 下行 total_drop_5 约束加码
    v1 规则: today - yesterday ≤ -5 就触发。
    v2 规则: 还必须 today_score ≤ -2 (即已跌到悲观区)。
    v1 数据: 148 次下行触发中, 128 次 (86.5%) 仅因 total_drop_5 触发,
            其中大量是"从乐观区跌到低乐观区"的评分抖动, 不是崩盘。

(C) 上行条件放宽
    - index_rally: HS300 AND CSI1000 都 ≥ 2% → 任一 ≥ 2% (OR)
    - breadth_confirm: ≥ 0.80 → ≥ 0.70
    v1 数据: total_surge_5 满足 141 次, 但因两个严苛条件拒绝, 上行仅触发 3 次。

本文件独立于 regime_rules.py, 不修改生产规则。
replay.py 通过 --rules v2 调用本函数。
"""

from __future__ import annotations

from typing import Optional


def check_emergency_hatch_v2(
    today_score: int,
    yesterday_score: Optional[int],
    hs300_pct_change: Optional[float] = None,
    csi1000_pct_change: Optional[float] = None,
    advance_decline_ratio: Optional[float] = None,
    sentiment_index: Optional[float] = None,
    sentiment_delta: Optional[int] = None,
    days_since_last_emergency: Optional[int] = None,
    cooldown_days: int = 3,
) -> dict:
    """改进版逃生门检查。

    Args:
        today_score/yesterday_score/hs300_pct_change/...: 与 v1 相同
        days_since_last_emergency: 距离上次逃生门触发的交易日数。
            None 表示从未触发过, 此时不启用冷静期。
        cooldown_days: 冷静期长度 (默认 3 日)

    Returns dict 与 v1 同构, 额外包含 'cooldown': True 标识被冷静期拦截
    (不影响 replay.py 的其他逻辑, 只是诊断字段)。
    """
    # ---- (A) 冷静期拦截 ----
    if (
        days_since_last_emergency is not None
        and days_since_last_emergency <= cooldown_days
    ):
        return {
            "triggered": False,
            "direction": None,
            "reasons": [],
            "cooldown": True,
        }

    # ---- 下行 ----
    down_reasons: list[str] = []

    # (B) total_drop_5 加码: 还需 today_score <= -2
    if (
        yesterday_score is not None
        and (today_score - yesterday_score) <= -5
        and today_score <= -2
    ):
        down_reasons.append("total_drop_5")

    if (
        advance_decline_ratio is not None
        and advance_decline_ratio <= 0.25
        and (
            (hs300_pct_change is not None and hs300_pct_change <= -3.0)
            or (csi1000_pct_change is not None and csi1000_pct_change <= -3.0)
        )
    ):
        down_reasons.append("index_crash")

    sentiment_collapse = False
    if sentiment_index is not None and sentiment_index <= 20:
        sentiment_collapse = True
    if sentiment_delta is not None and sentiment_delta <= -40:
        sentiment_collapse = True
    if sentiment_collapse:
        down_reasons.append("sentiment_collapse")

    if down_reasons:
        return {
            "triggered": True,
            "direction": "down",
            "reasons": down_reasons,
            "cooldown": False,
        }

    # ---- 上行 (v2 放宽) ----
    # (C1) index_rally: AND → OR
    index_rally = (
        (hs300_pct_change is not None and hs300_pct_change >= 2.0)
        or (csi1000_pct_change is not None and csi1000_pct_change >= 2.0)
    )
    # (C2) breadth_confirm: 0.80 → 0.70
    breadth_ok = (
        advance_decline_ratio is not None and advance_decline_ratio >= 0.70
    )
    total_surge = (
        yesterday_score is not None and (today_score - yesterday_score) >= 5
    )

    if total_surge and index_rally and breadth_ok:
        return {
            "triggered": True,
            "direction": "up",
            "reasons": ["total_surge_5", "index_rally", "breadth_confirm"],
            "cooldown": False,
        }

    return {
        "triggered": False,
        "direction": None,
        "reasons": [],
        "cooldown": False,
    }
