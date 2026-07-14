"""S9 — 池子构建 + 因子计算 + Top-K 排序.

进攻腿: high_prox = close_t / max(close, OFF_WINDOW)
防守腿: idiovol_low = -std(daily_ret - 池均ret, DEF_WINDOW)
"""

from __future__ import annotations

import logging
import math
import sqlite3

from strategy_common import get_market_db, is_st, iso_to_yyyymmdd, read_latest_industries, read_latest_names

# akshare 名字缓存 (一次会话内只拉一次, 5500 行)
_AK_NAME_CACHE: dict[str, str] | None = None


def _resolve_names(codes: list[str], t_date_iso: str) -> dict[str, str]:
    """名字解析三级 fallback: limit_up_pool → stock_concept_map → akshare 全市场快照."""
    out: dict[str, str] = {}
    out.update(read_latest_names(codes, as_of=t_date_iso))   # ① limit_up_pool
    missing = [c for c in codes if c not in out and c.split(".")[0] not in out]
    if missing:
        conn = get_market_db()
        try:
            placeholders = ",".join("?" * len(missing))
            rows = conn.execute(
                f"SELECT DISTINCT ts_code, name FROM stock_concept_map "
                f"WHERE ts_code IN ({placeholders}) AND name IS NOT NULL",
                missing,
            ).fetchall()
            for r in rows:
                out[r["ts_code"]] = r["name"]
        finally:
            conn.close()
    missing = [c for c in codes if c not in out and c.split(".")[0] not in out]
    if missing:
        global _AK_NAME_CACHE
        if _AK_NAME_CACHE is None:
            try:
                import akshare as ak
                df = ak.stock_info_a_code_name()
                _AK_NAME_CACHE = {str(r["code"]).zfill(6): r["name"] for _, r in df.iterrows()}
            except Exception as e:
                logging.warning("akshare 名字兜底失败(忽略): %s", str(e)[:80])
                _AK_NAME_CACHE = {}
        for c in missing:
            n = _AK_NAME_CACHE.get(c.split(".")[0])
            if n:
                out[c] = n
    return out


def build_universe(t_date_iso: str, cfg: dict) -> list[dict]:
    """读 daily_basic 当日 ≥100亿总市值 + 非ST. 返回 [{code,total_mv_yi,...}]."""
    t_compact = iso_to_yyyymmdd(t_date_iso)
    bcfg = cfg["base"]
    mv_threshold_wanyuan = float(bcfg["min_total_mv_yi"]) * 1e4  # 100 → 1e6 万元
    conn = get_market_db()
    try:
        rows = conn.execute(
            """
            SELECT b.ts_code AS code, b.close, b.total_mv, b.circ_mv,
                   b.turnover_rate, b.pe_ttm, d.pct_chg, d.vol, d.amount
            FROM daily_basic b
            JOIN daily d ON d.ts_code=b.ts_code AND d.trade_date=b.trade_date
            WHERE b.trade_date=? AND b.total_mv>=?
            """,
            (t_compact, mv_threshold_wanyuan),
        ).fetchall()
    finally:
        conn.close()

    codes = [r["code"] for r in rows]
    names = _resolve_names(codes, t_date_iso)
    inds = read_latest_industries(codes, as_of=t_date_iso)
    pool = []
    for r in rows:
        code = r["code"]
        name = names.get(code) or names.get(code.split(".")[0])
        if bcfg.get("exclude_st", True) and is_st(name):
            continue
        pool.append({
            "code": code,
            "name": name,
            "industry": inds.get(code) or inds.get(code.split(".")[0]),
            "close": float(r["close"]) if r["close"] is not None else None,
            "total_mv_yi": float(r["total_mv"]) / 1e4 if r["total_mv"] is not None else None,
            "circ_mv_yi": float(r["circ_mv"]) / 1e4 if r["circ_mv"] is not None else None,
            "turnover_rate": float(r["turnover_rate"]) if r["turnover_rate"] is not None else None,
            "pe_ttm": float(r["pe_ttm"]) if r["pe_ttm"] is not None else None,
            "pct_chg": float(r["pct_chg"]) if r["pct_chg"] is not None else None,
            "amount": float(r["amount"]) if r["amount"] is not None else None,
        })
    return pool


def _load_history(codes: list[str], t_date_iso: str, lookback: int) -> dict[str, list[dict]]:
    """每 code 取截至 t_date 的最近 lookback 个交易日 (close, pct_chg). 升序返回."""
    if not codes:
        return {}
    t_compact = iso_to_yyyymmdd(t_date_iso)
    conn = get_market_db()
    try:
        out: dict[str, list[dict]] = {}
        # 批量化: 单次 IN 查询 + python 端 group
        placeholders = ",".join("?" * len(codes))
        cursor = conn.execute(
            f"""
            SELECT trade_date, ts_code, close, pct_chg FROM daily
            WHERE trade_date<=? AND ts_code IN ({placeholders})
            ORDER BY ts_code, trade_date DESC
            """,
            (t_compact, *codes),
        )
        for row in cursor:
            code = row["ts_code"]
            arr = out.setdefault(code, [])
            if len(arr) >= lookback:
                continue
            arr.append({"date": row["trade_date"], "close": float(row["close"]) if row["close"] is not None else None,
                        "pct_chg": float(row["pct_chg"]) / 100.0 if row["pct_chg"] is not None else None})
        # 升序
        for code in out:
            out[code] = list(reversed(out[code]))
        return out
    finally:
        conn.close()


def _high_prox(hist: list[dict], window: int) -> float | None:
    closes = [h["close"] for h in hist[-window:] if h["close"] is not None]
    if len(closes) < max(window // 2, 30):
        return None
    last = closes[-1]
    mx = max(closes)
    if mx <= 0:
        return None
    return last / mx


def _ret_n(hist: list[dict], n: int) -> float:
    """近 n 日累计收益(简单乘积), 不足返回 0 (二级排序用, 不应阻塞)."""
    arr = [h["pct_chg"] for h in hist[-n:] if h["pct_chg"] is not None]
    if not arr:
        return 0.0
    eq = 1.0
    for r in arr:
        eq *= (1.0 + r)
    return eq - 1.0


def _idiovol_low(hist: list[dict], market_mean_ret_by_date: dict[str, float], window: int) -> float | None:
    rets = []
    for h in hist[-window:]:
        if h["pct_chg"] is None: continue
        mkt = market_mean_ret_by_date.get(h["date"])
        if mkt is None: continue
        rets.append(h["pct_chg"] - mkt)
    if len(rets) < max(window // 2, 20):
        return None
    n = len(rets); mu = sum(rets) / n
    var = sum((r - mu) ** 2 for r in rets) / max(n - 1, 1)
    return -math.sqrt(var)


def _market_mean_by_date(pool_hist: dict[str, list[dict]], window: int) -> dict[str, float]:
    """每个交易日: 池内所有 code 的 pct_chg 均值 (用作 market 基准, 给 idiovol 去均值)."""
    by_date: dict[str, list[float]] = {}
    for code, hist in pool_hist.items():
        for h in hist[-window:]:
            if h["pct_chg"] is None: continue
            by_date.setdefault(h["date"], []).append(h["pct_chg"])
    return {d: sum(v) / len(v) for d, v in by_date.items() if v}


def filter_candidates(t_date_iso: str, regime: dict, cfg: dict) -> dict:
    """编排: 池子 → 历史 → 因子 → top-K. 返回 {candidates, stats}."""
    pool = build_universe(t_date_iso, cfg)
    if not pool:
        return {"candidates": [], "stats": {"universe_size": 0, "valid_factor_count": 0, "kept_count": 0}}

    offensive = regime["offensive"]
    if offensive:
        scfg = cfg["offensive"]
        lookback = max(int(scfg["window"]), 60)
    else:
        scfg = cfg["defensive"]
        lookback = max(int(scfg["window"]), 60)

    codes = [p["code"] for p in pool]
    hist = _load_history(codes, t_date_iso, lookback + 5)

    valid = 0
    if offensive:
        win = int(scfg["window"])
        for p in pool:
            h = hist.get(p["code"]) or []
            p["factor"] = _high_prox(h, win)
            # 二级排序: 距高点并列时(牛市常见, 多只都 =1.0)按20日动量取胜
            p["tiebreak"] = _ret_n(h, 20)
            if p["factor"] is not None: valid += 1
    else:
        win = int(scfg["window"])
        mean_by_date = _market_mean_by_date(hist, win)
        for p in pool:
            h = hist.get(p["code"]) or []
            p["factor"] = _idiovol_low(h, mean_by_date, win)
            # 防守腿二级排序: 同等低波时按20日动量(正方向)取胜
            p["tiebreak"] = _ret_n(h, 20)
            if p["factor"] is not None: valid += 1

    ranked = sorted(
        [p for p in pool if p.get("factor") is not None],
        key=lambda x: (round(x["factor"], 4), x.get("tiebreak") or 0.0),
        reverse=True,
    )
    top_k = int(cfg.get("top_k") or 40)
    kept = ranked[:top_k]
    for rank, c in enumerate(kept, 1):
        c["rank"] = rank
        c["sleeve"] = "offensive" if offensive else "defensive"
        c["sleeve_label"] = scfg["label"]
        c["factor_name"] = scfg["factor_name"]

    logging.info("S9 候选: universe=%d, valid_factor=%d, kept=%d (sleeve=%s)",
                 len(pool), valid, len(kept), kept[0]["sleeve"] if kept else "?")
    return {
        "candidates": kept,
        "stats": {"universe_size": len(pool), "valid_factor_count": valid, "kept_count": len(kept)},
    }
