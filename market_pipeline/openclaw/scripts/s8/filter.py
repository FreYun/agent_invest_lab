"""S8 两路径候选筛选 + 分级.

候选池都从主线题材成分股出发, JOIN daily/daily_basic/moneyflow_daily.
Path B 弱结构买分歧 / Path A 强结构借势, 由 tone.path 决定走哪条.
"""

from __future__ import annotations

from strategy_common import calculate_position_pct, get_market_db, iso_to_yyyymmdd


def _bare(ts_code: str) -> str:
    return ts_code.split(".")[0]


def _load_universe(conn, raw_date: str, theme_names: list[str]) -> dict:
    """主线题材成分股 JOIN 行情/资金 → {ts_code: row dict}. 一只票多题材去重."""
    if not theme_names:
        return {}
    ph = ",".join("?" * len(theme_names))
    rows = conn.execute(
        f"""
        SELECT k.ts_code, k.stock_name,
               d.pct_chg, d.low, d.close,
               b.volume_ratio, b.turnover_rate, b.circ_mv, b.pe_ttm,
               m.net_main
        FROM kpl_theme_daily k
        JOIN daily d        ON d.ts_code=k.ts_code AND d.trade_date=k.trade_date
        LEFT JOIN daily_basic b     ON b.ts_code=k.ts_code AND b.trade_date=k.trade_date
        LEFT JOIN moneyflow_daily m ON m.ts_code=k.ts_code AND m.trade_date=k.trade_date
        WHERE k.trade_date=? AND k.theme_name IN ({ph})
        """,
        (raw_date, *theme_names),
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        if r["ts_code"] not in out:
            out[r["ts_code"]] = dict(r)
    return out


def _themes_map(conn, raw_date: str, theme_names: list[str]) -> dict[str, list[str]]:
    if not theme_names:
        return {}
    ph = ",".join("?" * len(theme_names))
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        f"SELECT ts_code, theme_name FROM kpl_theme_daily WHERE trade_date=? AND theme_name IN ({ph})",
        (raw_date, *theme_names),
    ):
        out.setdefault(r["ts_code"], []).append(r["theme_name"])
    return out


def _ret10(conn, ts_code: str, raw_date: str) -> float | None:
    rows = conn.execute(
        "SELECT close FROM daily WHERE ts_code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 11",
        (ts_code, raw_date),
    ).fetchall()
    if len(rows) < 11 or not rows[10]["close"]:
        return None
    return round((rows[0]["close"] - rows[10]["close"]) / rows[10]["close"] * 100, 2)


def _ret5(conn, ts_code: str, raw_date: str) -> float | None:
    rows = conn.execute(
        "SELECT close FROM daily WHERE ts_code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 6",
        (ts_code, raw_date),
    ).fetchall()
    if len(rows) < 6 or not rows[5]["close"]:
        return None
    return round((rows[0]["close"] - rows[5]["close"]) / rows[5]["close"] * 100, 2)


def _ma5(conn, ts_code: str, raw_date: str) -> float | None:
    rows = conn.execute(
        "SELECT close FROM daily WHERE ts_code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 5",
        (ts_code, raw_date),
    ).fetchall()
    if len(rows) < 5:
        return None
    return sum(r["close"] for r in rows) / 5


def _trend_intact_b(conn, ts_code: str, raw_date: str, close_today: float) -> bool:
    """PATH_B 个股趋势判定: close > MA5 或 ret5 > 0 (二选一即可)."""
    ma5 = _ma5(conn, ts_code, raw_date)
    if ma5 is not None and close_today > ma5:
        return True
    ret5 = _ret5(conn, ts_code, raw_date)
    return ret5 is not None and ret5 > 0


def _lub(conn, code: str, iso_date: str) -> tuple[int | None, int | None]:
    r = conn.execute(
        "SELECT streak, blast_count FROM limit_up_pool WHERE code=? AND date=?", (code, iso_date)
    ).fetchone()
    return (r["streak"], r["blast_count"]) if r else (None, None)


def _base_ok(row: dict, base_cfg: dict) -> bool:
    name = row.get("stock_name") or ""
    if base_cfg["exclude_st"] and "ST" in name.upper():
        return False
    circ_yi = (row.get("circ_mv") or 0) / 1e4
    if circ_yi < base_cfg["circ_mv_yi_min"]:
        return False
    pe = row.get("pe_ttm")
    if base_cfg["require_pe_positive_or_null"] and pe is not None and pe <= 0:
        return False
    return True


def _buy_points_b(row: dict, cfg_b: dict) -> dict:
    low, close = row["low"], row["close"]
    return {
        "entry_zone_low": round(low, 2),
        "entry_zone_high": round(close, 2),
        "entry_rule": "回踩低吸[当日低点,当日收盘]",
        "stop_loss_price": round(low * cfg_b["stop_mult"], 2),
        "stop_loss_rule": f"低点×{cfg_b['stop_mult']}",
        "take_profit_price": round(close * cfg_b["take_profit_mult"], 2),
        "take_profit_rule": f"收盘×{cfg_b['take_profit_mult']}",
    }


def _buy_points_a(row: dict, cfg_a: dict) -> dict:
    close = row["close"]
    return {
        "entry_zone_low": round(close * cfg_a["entry_low_mult"], 2),
        "entry_zone_high": round(close * cfg_a["entry_high_mult"], 2),
        "entry_rule": f"接力[收盘×{cfg_a['entry_low_mult']},收盘×{cfg_a['entry_high_mult']}]",
        "stop_loss_price": round(close * cfg_a["stop_mult"], 2),
        "stop_loss_rule": f"收盘×{cfg_a['stop_mult']}",
        "take_profit_price": round(close * cfg_a["take_profit_mult"], 2),
        "take_profit_rule": f"收盘×{cfg_a['take_profit_mult']}",
    }


def _make_candidate(row, themes, ret10, streak, blast, tier, buy, pos_pct, pos_calc, tone, regime_data):
    code = _bare(row["ts_code"])
    return {
        "code": code,
        "ts_code": row["ts_code"],
        "name": row.get("stock_name"),
        "themes": themes,
        "tier": tier,
        "play_mode": tone["path"],
        "tone": tone["tone"],
        "regime_name": regime_data.get("regime"),
        "pct_chg": row.get("pct_chg"),
        "net_main": row.get("net_main"),
        "vol_ratio": row.get("volume_ratio"),
        "turnover_rate": row.get("turnover_rate"),
        "circ_mv_yi": round((row.get("circ_mv") or 0) / 1e4, 1),
        "pe_ttm": row.get("pe_ttm"),
        "dist10_pct": ret10,
        "lub_streak": streak,
        "lub_blast": blast,
        "entry_zone_low": buy["entry_zone_low"],
        "entry_zone_high": buy["entry_zone_high"],
        "entry_rule": buy["entry_rule"],
        "stop_loss_price": buy["stop_loss_price"],
        "stop_loss_rule": buy["stop_loss_rule"],
        "take_profit_price": buy["take_profit_price"],
        "take_profit_rule": buy["take_profit_rule"],
        "position_pct": pos_pct,
        "position_calc": pos_calc,
        "signal_score": round((row.get("net_main") or 0) / 1e4, 3),  # 资金强度(亿), 越大越强
    }


def filter_candidates(t_date, mainline, tone, regime_data, config) -> dict:
    raw = iso_to_yyyymmdd(t_date)
    base_cfg, tier_cfg = config["base"], config["tier"]
    path = tone["path"]
    pos_pct, pos_calc = calculate_position_pct(regime_data)

    theme_names = [t["name"] for t in mainline["themes"]]
    conn = get_market_db()
    try:
        universe = _load_universe(conn, raw, theme_names)
        tmap = _themes_map(conn, raw, theme_names)

        kept, extras, reject_n = [], [], 0
        for ts, row in universe.items():
            if row.get("close") is None or row.get("pct_chg") is None:
                reject_n += 1
                continue
            if not _base_ok(row, base_cfg):
                reject_n += 1
                continue
            net = row.get("net_main")
            vr = row.get("volume_ratio")
            pct = row.get("pct_chg")
            circ_yi = (row.get("circ_mv") or 0) / 1e4

            if path == "buy_divergence":
                cfg = config["path_b"]
                if not (net and net > cfg["net_main_min"]):
                    reject_n += 1; continue
                if not (vr and vr > cfg["vol_ratio_min"]):
                    reject_n += 1; continue
                if circ_yi > cfg["circ_mv_yi_max"]:
                    reject_n += 1; continue
                if pct <= cfg["pct_chg_min"]:
                    reject_n += 1; continue
                # 心法: 真"分歧低吸" = 当日回调, 不追高 (涨停归 avoid 在 tier 里)
                if pct > cfg.get("pct_chg_max", 999):
                    reject_n += 1; continue
                # 心法: 个股趋势还在 (close>MA5 或 ret5>0)
                if cfg.get("trend_require_above_ma5") and not _trend_intact_b(conn, ts, raw, row["close"]):
                    reject_n += 1; continue
                # 心法: 近10日累计涨幅在 (ret10_min, ret10_max) 之间 (排除下跌通道 + 充分演绎)
                ret10_pre = _ret10(conn, ts, raw)
                if ret10_pre is None or ret10_pre <= cfg.get("ret10_min", -999):
                    reject_n += 1; continue
                buy = _buy_points_b(row, cfg)
            else:  # borrow_strength
                cfg = config["path_a"]
                if not (net and net > cfg["net_main_min"]):
                    reject_n += 1; continue
                if not (vr and vr > cfg["vol_ratio_min"]):
                    reject_n += 1; continue
                if circ_yi > cfg["circ_mv_yi_max"]:
                    reject_n += 1; continue
                if pct < cfg["pct_chg_min"]:
                    reject_n += 1; continue
                buy = _buy_points_a(row, cfg)

            ret10 = _ret10(conn, ts, raw)
            streak, blast = _lub(conn, _bare(ts), t_date)
            themes = tmap.get(ts, [])

            tier = _classify_tier(path, pct, ret10, streak, blast, cfg, tier_cfg, config["path_b"])
            cand = _make_candidate(row, themes, ret10, streak, blast, tier, buy,
                                   pos_pct, pos_calc, tone, regime_data)
            if tier in ("core", "strength_watch"):
                kept.append(cand)
            else:
                extras.append(cand)

        # 全市场最高连板 → 情绪温度计风向标(独立于主线池)
        top = mainline.get("top_streak")
        if top and top.get("streak") and top["streak"] >= config["mainline"]["min_streak_for_top"]:
            tcode = str(top["code"]).zfill(6)
            if not any(c["code"] == tcode for c in kept + extras):
                extras.append(_sentiment_gauge_from_lub(conn, raw, t_date, top, tone, regime_data))
    finally:
        conn.close()

    kept.sort(key=lambda c: (-(c["signal_score"] or 0), c["code"]))
    extras.sort(key=lambda c: (-(c.get("lub_streak") or 0), -(c["signal_score"] or 0)))
    for rank, c in enumerate(kept, 1):
        c["rank"] = rank
    return {
        "candidates": kept,
        "extras": extras,
        "stats": {
            "universe_size": len(universe),
            "kept_count": len(kept),
            "extras_count": len(extras),
            "reject_count": reject_n,
        },
    }


def _classify_tier(path, pct, ret10, streak, blast, cfg, tier_cfg, cfg_b) -> str:
    """连板龙头 → sentiment_gauge; 充分演绎/涨停低吸矛盾 → avoid; 涨停接力 → strength_watch; 其余 → core."""
    if streak and streak >= 2:
        return "sentiment_gauge"
    if ret10 is not None and ret10 >= cfg_b["ret10_max"]:
        return "avoid"
    if pct >= tier_cfg["limit_up_pct"]:
        # Path B 涨停归 avoid: 心法上"分歧低吸"≠追高 (常规已被 pct_chg_max 在 filter 层拦掉,
        # 仅在 cfg 被覆盖时才会走到这里; 防御性保留)
        if path == "buy_divergence":
            return "avoid"
        # Path A 高位放量见顶(炸板多)降级为观察
        if blast and blast > cfg.get("blast_max", 99):
            return "avoid"
        return "strength_watch"
    return "core"


def _sentiment_gauge_from_lub(conn, raw_date, iso_date, top, tone, regime_data) -> dict:
    tcode = str(top["code"]).zfill(6)
    row = conn.execute(
        """
        SELECT d.pct_chg, d.low, d.close, b.volume_ratio, b.turnover_rate, b.circ_mv, b.pe_ttm, m.net_main
        FROM daily d
        LEFT JOIN daily_basic b ON b.ts_code=d.ts_code AND b.trade_date=d.trade_date
        LEFT JOIN moneyflow_daily m ON m.ts_code=d.ts_code AND m.trade_date=d.trade_date
        WHERE d.trade_date=? AND d.ts_code LIKE ?
        """,
        (raw_date, tcode + "%"),
    ).fetchone()
    row = dict(row) if row else {}
    row["stock_name"] = top.get("name")
    row["ts_code"] = tcode
    ret10 = _ret10(conn, conn.execute("SELECT ts_code FROM daily WHERE trade_date=? AND ts_code LIKE ? LIMIT 1",
                                      (raw_date, tcode + "%")).fetchone()["ts_code"], raw_date) \
        if row else None
    buy = {"entry_zone_low": None, "entry_zone_high": None, "entry_rule": "风向标·不入场",
           "stop_loss_price": None, "stop_loss_rule": None,
           "take_profit_price": None, "take_profit_rule": None}
    cand = _make_candidate(row, [], ret10, top.get("streak"), None, "sentiment_gauge", buy,
                           None, None, tone, regime_data)
    return cand
