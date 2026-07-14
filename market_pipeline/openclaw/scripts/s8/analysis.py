"""S8 大势定调(tone) + 主线识别(mainline).

定调决定走哪条路径:
  强结构 → Path A borrow_strength (借势接力主升)
  弱结构 → Path B buy_divergence (自控买分歧低吸)
  中性/过渡 → Path B (框架"不确定就自控")
"""

from __future__ import annotations

from strategy_common import get_market_db, iso_to_yyyymmdd


def _breadth(conn, raw_date: str, limitdown_pct: float) -> dict:
    """当日 涨/跌/跌停家数 (跌停用 pct_chg ≤ limitdown_pct 近似)."""
    up = conn.execute(
        "SELECT COUNT(*) n FROM daily WHERE trade_date=? AND pct_chg>0", (raw_date,)
    ).fetchone()["n"]
    down = conn.execute(
        "SELECT COUNT(*) n FROM daily WHERE trade_date=? AND pct_chg<0", (raw_date,)
    ).fetchone()["n"]
    limitdown = conn.execute(
        "SELECT COUNT(*) n FROM daily WHERE trade_date=? AND pct_chg<=?",
        (raw_date, limitdown_pct),
    ).fetchone()["n"]
    return {"advance": up, "decline": down, "limit_down": limitdown}


def _advance_trend(conn, raw_date: str, limitdown_pct: float, n: int = 3) -> list[int]:
    """近 n 个交易日(含当日)的涨家数, 最新在末尾."""
    dates = [
        r["trade_date"]
        for r in conn.execute(
            "SELECT DISTINCT trade_date FROM daily WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?",
            (raw_date, n),
        ).fetchall()
    ]
    dates.reverse()
    return [
        conn.execute(
            "SELECT COUNT(*) n FROM daily WHERE trade_date=? AND pct_chg>0", (d,)
        ).fetchone()["n"]
        for d in dates
    ]


def compute_tone(t_date: str, regime_data: dict, config: dict) -> dict:
    """大势定调 → {path, tone, reasons[], gauge{}}.

    path ∈ {"borrow_strength", "buy_divergence"}.
    """
    tcfg = config["tone"]
    raw = iso_to_yyyymmdd(t_date)
    conn = get_market_db()
    try:
        gauge = _breadth(conn, raw, tcfg["limitdown_pct"])
        trend = _advance_trend(conn, raw, tcfg["limitdown_pct"], 3)
    finally:
        conn.close()

    regime = regime_data.get("regime")
    score = regime_data.get("score", {}).get("total") or 0
    emergency = regime_data.get("emergency_switch")
    emergency_down = bool(emergency) and regime_data.get("emergency_direction") == "down" \
        if "emergency_direction" in regime_data else bool(emergency)

    adv, ld = gauge["advance"], gauge["limit_down"]
    cliff = bool(trend) and len(trend) >= 2 and adv < tcfg["cliff_ratio"] * max(trend[:-1] or [adv])

    weak_reasons, strong_ok_reasons = [], []
    # ── 弱结构触发(任一) ──
    if regime in tcfg["weak_regimes"]:
        weak_reasons.append(f"regime={regime}∈弱势/熊")
    if emergency_down:
        weak_reasons.append("emergency_switch=down")
    if adv < tcfg["weak_max_advance"]:
        weak_reasons.append(f"涨家数{adv}<{tcfg['weak_max_advance']}(赚钱效应塌陷)")
    if ld >= tcfg["weak_min_limitdown"]:
        weak_reasons.append(f"跌停{ld}≥{tcfg['weak_min_limitdown']}(塌陷)")
    if cliff:
        weak_reasons.append(f"3日涨家数断崖{trend}")

    # ── 强结构触发(全部满足) ──
    strong_checks = [
        (regime in tcfg["strong_regimes"], f"regime={regime}∈强牛/强势震荡"),
        (score >= tcfg["strong_min_score"], f"score={score}≥{tcfg['strong_min_score']}"),
        (adv >= tcfg["strong_min_advance"], f"涨家数{adv}≥{tcfg['strong_min_advance']}"),
        (ld <= tcfg["strong_max_limitdown"], f"跌停{ld}≤{tcfg['strong_max_limitdown']}"),
        (not emergency_down, "非紧急下切"),
    ]
    strong_all = all(ok for ok, _ in strong_checks)
    for ok, msg in strong_checks:
        strong_ok_reasons.append(("✓" if ok else "✗") + msg)

    if strong_all and not weak_reasons:
        path, tone = "borrow_strength", "强结构·借势接力"
        reasons = strong_ok_reasons
    elif weak_reasons:
        path, tone = "buy_divergence", "弱结构·买分歧低吸"
        reasons = weak_reasons
    else:
        path, tone = "buy_divergence", "中性/过渡·默认自控"
        reasons = ["不满足强结构全条件且无明确弱结构信号 → 框架默认自控"] + strong_ok_reasons

    return {
        "path": path,
        "tone": tone,
        "reasons": reasons,
        "gauge": {**gauge, "advance_trend_3d": trend, "cliff": cliff},
    }


def _is_fading(conn, raw_date: str, theme_name: str, days: int,
               peak_min: int, ratio: float) -> bool:
    """钝化: 窗口峰值 zt_num ≥ peak_min 且今日 zt_num ≤ 峰值×ratio."""
    dates = [r["trade_date"] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM kpl_theme_daily WHERE trade_date<=? "
        "ORDER BY trade_date DESC LIMIT ?", (raw_date, days)
    )]
    if not dates:
        return False
    ph = ",".join("?" * len(dates))
    zt_by_date = {
        r["trade_date"]: r["z"] or 0
        for r in conn.execute(
            f"SELECT trade_date, MAX(zt_num) z FROM kpl_theme_daily "
            f"WHERE theme_name=? AND trade_date IN ({ph}) GROUP BY trade_date",
            (theme_name, *dates),
        )
    }
    today_zt = zt_by_date.get(raw_date, 0)
    peak = max(zt_by_date.values()) if zt_by_date else 0
    return peak >= peak_min and today_zt <= peak * ratio


def identify_mainline(t_date: str, config: dict) -> dict:
    """主线识别 → {themes[], top_streak{}, is_divergence}.

    themes: 当日 kpl_theme_daily 按 zt_num desc, hot_num desc 取 top_n, 剔除 fading.
    top_streak: limit_up_pool 全市场最高连板(情绪温度计风向标).
    is_divergence: 主线是否处于分歧位(最高连板未在主线题材内 或 主线题材当日少涨停).
    """
    mcfg = config["mainline"]
    raw = iso_to_yyyymmdd(t_date)
    conn = get_market_db()
    try:
        # 多取一倍冗余, 然后剔除 fading 主线, 取前 top_n
        candidate_rows = conn.execute(
            """
            SELECT theme_name, MAX(zt_num) zt, MAX(up_num) up_num, MAX(hot_num) hot, COUNT(*) cons
            FROM kpl_theme_daily WHERE trade_date=?
            GROUP BY theme_name ORDER BY zt DESC, hot DESC LIMIT ?
            """,
            (raw, mcfg["top_n"] * 3),
        ).fetchall()
        themes = []
        for r in candidate_rows:
            if _is_fading(conn, raw, r["theme_name"], mcfg["fading_trend_days"],
                          mcfg["fading_peak_min"], mcfg["fading_ratio"]):
                continue  # 心法: 钝化主线不再算主线
            themes.append({"name": r["theme_name"], "zt": r["zt"], "up_num": r["up_num"],
                           "hot": r["hot"], "cons": r["cons"]})
            if len(themes) >= mcfg["top_n"]:
                break
        top = conn.execute(
            "SELECT code, name, streak, pct_chg FROM limit_up_pool WHERE date=? "
            "ORDER BY streak DESC, pct_chg DESC LIMIT 1",
            (t_date,),
        ).fetchone()
        top_streak = dict(top) if top else None
        # 最高连板所属题材是否在主线内
        in_mainline = False
        if top_streak:
            theme_names = {t["name"] for t in themes}
            owned = {
                r["theme_name"]
                for r in conn.execute(
                    "SELECT theme_name FROM kpl_theme_daily WHERE trade_date=? AND ts_code LIKE ?",
                    (raw, str(top_streak["code"]).zfill(6) + "%"),
                ).fetchall()
            }
            in_mainline = bool(theme_names & owned)
    finally:
        conn.close()

    # 分歧位: 最高连板掉出主线(高低切/退潮) 或 主线龙头题材当日涨停数低(分歧)
    max_zt = max((t["zt"] for t in themes), default=0)
    is_divergence = (top_streak is not None and not in_mainline) or max_zt <= 1
    return {
        "themes": themes,
        "top_streak": top_streak,
        "top_streak_in_mainline": in_mainline,
        "is_divergence": is_divergence,
    }
