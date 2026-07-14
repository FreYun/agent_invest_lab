"""S5 龙回头三段信号判定 — 纯函数, 不接外部数据源。

输入: 一只股票的近 35 日 K 线 + 历史涨停日列表
输出: 通过/不通过 + 各阶段详情 (用于解释为什么通过/拒掉)

设计原则:
- 完全纯函数, 单元测试不需要 mock akshare
- 任何阶段拒掉立刻 short-circuit, 不浪费计算
- 拒因 (reject_reason) 和通过详情同样详细, 便于调参时知道 "差一点的标的差在哪里"

阈值定义见 references/strategy.md, 默认值在本文件 DEFAULTS 字典里, 调参时改这里。
"""

from __future__ import annotations

from typing import Optional

# --------------------------------------------------------------------------- #
# 阈值默认值 — 调参时改这里
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "dragon_lookback_days": 30,        # 龙头窗口 (T-N ~ T-1)
    "dragon_min_streak": 2,             # 最低连板数
    "cooldown_min_days": 2,             # 冷却最少天数
    "cooldown_max_days": 7,             # 冷却最多天数
    "cooldown_min_drop_pct": 5.0,       # 冷却最小阶段跌幅 (%)
    "rebound_min_pct": 7.0,             # T 日反包最低涨幅 (%)
    "rebound_must_cover_t1_high": True, # T 日收盘是否必须 >= T-1 最高
}


# --------------------------------------------------------------------------- #
# 三段判定
# --------------------------------------------------------------------------- #


def detect_dragon(
    klines: list,
    historical_streaks: list,
    t_date: str,
    config: Optional[dict] = None,
) -> dict:
    """龙头确认: 近 30 日内是否出现过 ≥2 连板。

    Args:
        klines: 该股 35 日 K 线, 时序从老到新, 每项 dict 含 date/open/high/low/close/pct_chg
        historical_streaks: 该股近 30 日的连板记录, 每项 dict {date, max_streak, close}
                            通常由 data_fetcher 从 stock_zt_pool_em 历史聚合得到
        t_date: T 日日期 'YYYY-MM-DD'
        config: 可选阈值覆盖

    Returns:
        {
          "passed": bool,
          "reject_reason": str | None,
          "dragon_peak": {"date": ..., "close": ..., "max_streak": ...} | None
        }
    """
    cfg = {**DEFAULTS, **(config or {})}
    min_streak = cfg["dragon_min_streak"]

    if not historical_streaks:
        return {
            "passed": False,
            "reject_reason": f"近 30 日无连板记录",
            "dragon_peak": None,
        }

    # 找最高连板那天 (并列时取最近的)
    qualified = [s for s in historical_streaks if s["max_streak"] >= min_streak and s["date"] < t_date]
    if not qualified:
        max_streak_seen = max((s["max_streak"] for s in historical_streaks), default=0)
        return {
            "passed": False,
            "reject_reason": f"近 30 日最高仅 {max_streak_seen} 板, 未达 {min_streak} 板门槛",
            "dragon_peak": None,
        }

    # 选最高连板那天 (max_streak 大的优先, 同 max_streak 取最近的)
    peak = max(qualified, key=lambda s: (s["max_streak"], s["date"]))

    return {
        "passed": True,
        "reject_reason": None,
        "dragon_peak": {
            "date": peak["date"],
            "close": peak["close"],
            "max_streak": peak["max_streak"],
        },
    }


def detect_cooldown(
    klines: list,
    dragon_peak: dict,
    t_date: str,
    config: Optional[dict] = None,
) -> dict:
    """冷却确认: 龙头日 → T-1 之间间隔 2-7 个交易日, 阶段跌幅 ≥5%。

    Args:
        klines: 该股 K 线 (含龙头日和 T-1 日)
        dragon_peak: detect_dragon 返回的 dragon_peak 字典
        t_date: T 日日期
        config: 可选阈值覆盖

    Returns:
        {"passed": bool, "reject_reason": str | None,
         "cooldown": {"days": int, "drop_pct": float, "t1_close": float} | None}
    """
    cfg = {**DEFAULTS, **(config or {})}
    min_days = cfg["cooldown_min_days"]
    max_days = cfg["cooldown_max_days"]
    min_drop = cfg["cooldown_min_drop_pct"]

    # 找龙头日和 T-1 日在 klines 中的位置
    peak_idx = next((i for i, k in enumerate(klines) if k["date"] == dragon_peak["date"]), None)
    t_idx = next((i for i, k in enumerate(klines) if k["date"] == t_date), None)

    if peak_idx is None:
        return {
            "passed": False,
            "reject_reason": f"K 线中找不到龙头日 {dragon_peak['date']}",
            "cooldown": None,
        }
    if t_idx is None:
        return {
            "passed": False,
            "reject_reason": f"K 线中找不到 T 日 {t_date}",
            "cooldown": None,
        }

    cooldown_days = t_idx - peak_idx - 1  # 不含 T 日和龙头日本身
    if cooldown_days < min_days:
        return {
            "passed": False,
            "reject_reason": f"冷却仅 {cooldown_days} 天, 少于 {min_days} 天 (太接近接力, 不算回头)",
            "cooldown": None,
        }
    if cooldown_days > max_days:
        return {
            "passed": False,
            "reject_reason": f"冷却已 {cooldown_days} 天, 超过 {max_days} 天 (龙头身份衰减)",
            "cooldown": None,
        }

    # 阶段跌幅 (高点收盘 → T-1 收盘)
    t1_close = klines[t_idx - 1]["close"]
    peak_close = dragon_peak["close"]
    drop_pct = (t1_close - peak_close) / peak_close * 100

    if drop_pct > -min_drop:  # drop_pct 是负数, > -5 表示跌幅不够
        return {
            "passed": False,
            "reject_reason": f"阶段跌幅 {drop_pct:.2f}%, 不足 -{min_drop}% (回调不充分)",
            "cooldown": None,
        }

    return {
        "passed": True,
        "reject_reason": None,
        "cooldown": {
            "days": cooldown_days,
            "drop_pct": round(drop_pct, 2),
            "t1_close": t1_close,
        },
    }


def detect_rebound(
    klines: list,
    t_date: str,
    config: Optional[dict] = None,
) -> dict:
    """反包确认: T 日涨幅 ≥7% 且 T 日收盘 ≥ T-1 最高。

    Args:
        klines: 该股 K 线 (含 T 日和 T-1 日)
        t_date: T 日日期
        config: 可选阈值覆盖

    Returns:
        {"passed": bool, "reject_reason": str | None,
         "rebound": {"t_pct": float, "t_close": float, "t_low": float, "t1_high": float} | None}
    """
    cfg = {**DEFAULTS, **(config or {})}
    min_pct = cfg["rebound_min_pct"]
    must_cover = cfg["rebound_must_cover_t1_high"]

    t_idx = next((i for i, k in enumerate(klines) if k["date"] == t_date), None)
    if t_idx is None or t_idx == 0:
        return {
            "passed": False,
            "reject_reason": f"K 线中找不到 T 日 {t_date} 或缺 T-1 日",
            "rebound": None,
        }

    t_bar = klines[t_idx]
    t1_bar = klines[t_idx - 1]

    t_pct = t_bar.get("pct_chg")
    if t_pct is None:
        # 如果没给 pct_chg 字段, 自己算
        t_pct = (t_bar["close"] - t1_bar["close"]) / t1_bar["close"] * 100

    if t_pct < min_pct:
        return {
            "passed": False,
            "reject_reason": f"T 日涨幅 {t_pct:.2f}%, 不足 +{min_pct}% (反包力度不够)",
            "rebound": None,
        }

    if must_cover and t_bar["close"] < t1_bar["high"]:
        return {
            "passed": False,
            "reject_reason": (
                f"T 日收盘 {t_bar['close']:.2f} 未收复 T-1 最高 {t1_bar['high']:.2f} "
                f"(虽涨 {t_pct:.2f}% 但未真反包)"
            ),
            "rebound": None,
        }

    return {
        "passed": True,
        "reject_reason": None,
        "rebound": {
            "t_pct": round(t_pct, 2),
            "t_close": t_bar["close"],
            "t_low": t_bar["low"],
            "t_high": t_bar["high"],
            "t1_high": t1_bar["high"],
        },
    }


# --------------------------------------------------------------------------- #
# 总入口
# --------------------------------------------------------------------------- #


def detect_s5(
    klines: list,
    historical_streaks: list,
    t_date: str,
    config: Optional[dict] = None,
) -> dict:
    """S5 三段全检, short-circuit 模式: 任一段拒掉就停。

    Returns:
        {
          "passed": bool,
          "stage_failed": "dragon" | "cooldown" | "rebound" | None,
          "reject_reason": str | None,
          "dragon_peak": dict | None,
          "cooldown": dict | None,
          "rebound": dict | None,
        }
    """
    # Stage 1
    dragon = detect_dragon(klines, historical_streaks, t_date, config)
    if not dragon["passed"]:
        return {
            "passed": False,
            "stage_failed": "dragon",
            "reject_reason": dragon["reject_reason"],
            "dragon_peak": None,
            "cooldown": None,
            "rebound": None,
        }

    # Stage 2
    cooldown = detect_cooldown(klines, dragon["dragon_peak"], t_date, config)
    if not cooldown["passed"]:
        return {
            "passed": False,
            "stage_failed": "cooldown",
            "reject_reason": cooldown["reject_reason"],
            "dragon_peak": dragon["dragon_peak"],
            "cooldown": None,
            "rebound": None,
        }

    # Stage 3
    rebound = detect_rebound(klines, t_date, config)
    if not rebound["passed"]:
        return {
            "passed": False,
            "stage_failed": "rebound",
            "reject_reason": rebound["reject_reason"],
            "dragon_peak": dragon["dragon_peak"],
            "cooldown": cooldown["cooldown"],
            "rebound": None,
        }

    return {
        "passed": True,
        "stage_failed": None,
        "reject_reason": None,
        "dragon_peak": dragon["dragon_peak"],
        "cooldown": cooldown["cooldown"],
        "rebound": rebound["rebound"],
    }
