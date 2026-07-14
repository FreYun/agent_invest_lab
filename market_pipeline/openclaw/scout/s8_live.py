"""S8 主线分歧/借势 — 盘中实时选股 (collect.py 每帧调用).

与收盘后 scripts/s8/select.py 不同: 那个读当日已收盘的 daily/kpl_theme_daily 选票,
本模块完全用盘中实时口径重算 → 实时选, 实时入 intraday_candidate_live。

口径对齐 scripts/s8 但替换数据源:
  大势定调  = 实时温度计(gauge 涨家数/跌停) + 近2收盘日涨家数趋势 + 最新 regime → Path A/B
  主线识别  = 昨日 kpl_theme_daily top 题材成分 ∪ 今日实时最强板块成分 (并集)
  候选筛选  = 实时 rt(价/涨幅/量比/换手/低点) + 实时主力净流入 + 昨日 daily_basic(市值/PE)

阈值常量从 scripts/s8/config.py 经 importlib 以独立模块名加载, 避免与 collect.py
`from config import TUSHARE_TOKEN` (workspace-bot11/scripts/config.py) 撞 sys.modules。
"""
from __future__ import annotations

import importlib.util
import os
import re

_S8_DIR = "/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s8"

PATH_LABEL = {"buy_divergence": "分歧低吸", "borrow_strength": "借势接力"}
TONE_LABEL = {"buy_divergence": "弱结构·买分歧低吸", "borrow_strength": "强结构·借势接力"}


def _load_cfg() -> dict:
    spec = importlib.util.spec_from_file_location(
        "s8cfg_live", os.path.join(_S8_DIR, "config.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.full_config()


_CFG = None


def _cfg() -> dict:
    global _CFG
    if _CFG is None:
        _CFG = _load_cfg()
    return _CFG


# ── 大势定调 (实时口径) ────────────────────────────────────────────────
def _latest_regime(c) -> dict | None:
    r = c.execute(
        "SELECT regime_code, regime_name, total_score, confidence, switched, "
        "emergency_switch, emergency_direction "
        "FROM regime_classify_daily WHERE rules_version='v2' "
        "ORDER BY trade_date DESC LIMIT 1"
    ).fetchone()
    return dict(r) if r else None


def _prior_advance(c, n: int = 2) -> list[int]:
    """近 n 个已收盘交易日的涨家数 (最旧在前)."""
    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM daily ORDER BY trade_date DESC LIMIT ?", (n,)
    )]
    out = []
    for d in dates:
        out.append(c.execute(
            "SELECT COUNT(*) FROM daily WHERE trade_date=? AND pct_chg>0", (d,)
        ).fetchone()[0])
    out.reverse()
    return out


def compute_tone(c, gauge: dict, regime: dict | None) -> dict:
    tcfg = _cfg()["tone"]
    prior = _prior_advance(c, 2)
    adv = gauge.get("up", 0)
    ld = gauge.get("limit_down", 0)
    trend = prior + [adv]
    cliff = bool(prior) and adv < tcfg["cliff_ratio"] * max(prior)

    rname = (regime or {}).get("regime_name")
    score = (regime or {}).get("total_score") or 0
    emer = bool((regime or {}).get("emergency_switch"))
    emer_down = emer and (regime or {}).get("emergency_direction") == "down"

    weak_reasons = []
    if rname in tcfg["weak_regimes"]:
        weak_reasons.append(f"regime={rname}∈弱势/熊")
    if emer_down:
        weak_reasons.append("emergency_switch=down")
    if adv < tcfg["weak_max_advance"]:
        weak_reasons.append(f"涨家数{adv}<{tcfg['weak_max_advance']}(赚钱效应塌陷)")
    if ld >= tcfg["weak_min_limitdown"]:
        weak_reasons.append(f"跌停{ld}≥{tcfg['weak_min_limitdown']}(塌陷)")
    if cliff:
        weak_reasons.append(f"涨家数断崖{trend}")

    strong_checks = [
        (rname in tcfg["strong_regimes"], f"regime={rname}∈强牛/强势震荡"),
        (score >= tcfg["strong_min_score"], f"score={score}≥{tcfg['strong_min_score']}"),
        (adv >= tcfg["strong_min_advance"], f"涨家数{adv}≥{tcfg['strong_min_advance']}"),
        (ld <= tcfg["strong_max_limitdown"], f"跌停{ld}≤{tcfg['strong_max_limitdown']}"),
        (not emer_down, "非紧急下切"),
    ]
    strong_all = all(ok for ok, _ in strong_checks)

    if strong_all and not weak_reasons:
        path = "borrow_strength"
        reasons = [m for _, m in strong_checks]
    elif weak_reasons:
        path = "buy_divergence"
        reasons = weak_reasons
    else:
        path = "buy_divergence"
        reasons = ["不满足强结构全条件且无明确弱结构 → 默认自控"]

    return {
        "path": path,
        "tone": TONE_LABEL[path],
        "reasons": reasons,
        "gauge": {"advance": adv, "limit_down": ld, "advance_trend_3d": trend, "cliff": cliff},
    }


# ── 主线识别 (廖峥"安静拆主线"心法) ────────────────────────────────────
# 主线 ≠ 单日涨幅最高/涨停最多, 而是: ①接力赚钱效应(多日涨停持续性, 权重最高)
# ②梯队高度(连板龙头带动) ③当日实时强度; 叠"启动>充分演绎"加成/钝化惩罚;
# 剔宽基/指数成分/地域/政策池(非题材). 昨日 KPL = 接力骨架, 今日实时板块 = 新点火补充.
MAINLINE_SCORE = {
    "score_pool_top": 24,       # 先从昨日 KPL 按涨停数取这么多题材进打分池
    "trend_days": 3,            # zt_num 趋势回看交易日数
    "w_live": 1.0,              # 当日实时强度权重
    "w_persist": 1.3,           # 接力持续性权重 — 心法最看重的维度
    "w_ladder": 1.0,            # 梯队(连板高度)权重
    "ignition_bonus": 0.5,      # 启动(zt 维持/创近期新高)综合分加成
    "fading_penalty": 0.55,     # 钝化(zt 高位断崖)综合分惩罚系数
    "fading_peak_min": 4,       # 判定钝化的前高门槛(前高涨停家数)
    "fading_ratio": 0.4,        # 今日 zt ≤ 前高×该比例 → 钝化
    "keep_top_n": 6,            # 最终保留多少条主线成分入池
    "live_board_top": 3,        # 今日实时最强概念板块补充入池(过黑名单+密度)
    "live_board_scan_max": 40,  # 最多扫描前 N 个(按实时强度)候选板块, 避免弱势日全表扫
    "live_board_density_min": 0.10,  # 实时板块涨停密度下限(剔宽基)
    "live_board_member_min": 5,
    "live_board_member_max": 800,
    "live_zt_pct": 9.5,         # 实时"近似涨停"涨幅阈值
}
# 宽基/指数成分/地域/政策池 — 非题材主线, 涨停多只因成分巨大, 一律剔除
BROAD_RE = re.compile(
    r"融资融券|股通|富时|标普|标准普尔|MSCI|QFII|机构重仓|基金重仓|"
    r"创业板综|科创综|中证\d|深成\d|上证\d|上证50|沪深300|深证成指|深成500|"
    r"小盘股|中盘股|大盘股|微盘|破净|破发|破增发|增发价|专精特新|"
    r"央国企|国企改革|年报预增|年报预盈|中报预|一季报|业绩预|高送转|"
    r"大开发|自贸|龙虎榜|昨日涨停|昨日连板|昨日炸板|昨日强势|昨日高",
    re.I)


def _is_broad(name: str) -> bool:
    n = name or ""
    return bool(BROAD_RE.search(n)) or n.endswith("板块")  # "广东板块"等地域


def _theme_live_strength(rt_by_ts, fundflow, ts_codes, zt_pct):
    """题材成分的当日实时强度: 均涨幅/上涨占比/近似涨停密度/资金净流入合计."""
    n = up = zt = 0
    pct_sum = net = 0.0
    for ts in ts_codes:
        rt = rt_by_ts.get(ts)
        if not rt or rt.get("pct") is None:
            continue
        n += 1
        p = rt["pct"]
        pct_sum += p
        if p > 0:
            up += 1
        if p >= zt_pct:
            zt += 1
        nm = fundflow.get(ts.split(".")[0])
        if nm:
            net += nm
    if n == 0:
        return None
    return {"n": n, "mean_pct": pct_sum / n, "up_ratio": up / n,
            "zt": zt, "density": zt / n, "net": net}


def identify_mainline(c, rt_by_ts, fundflow, board_live: dict,
                      mcfg: dict | None = None) -> tuple[set, dict, list[str]]:
    """→ (成分 ts_code 集合, ts_code→题材名, 主线题材名列表[按综合分降序])."""
    cfg = MAINLINE_SCORE if not mcfg else {**MAINLINE_SCORE, **mcfg}
    constituents: set[str] = set()
    theme_of: dict[str, str] = {}
    names: list[str] = []

    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM kpl_theme_daily ORDER BY trade_date DESC LIMIT ?",
        (cfg["trend_days"],))]
    if not dates:
        return constituents, theme_of, names
    yest = dates[0]

    # 打分池: 昨日 KPL 按涨停数取前 N (剔宽基), 多取一倍冗余以补足被剔除的
    pool: list[str] = []
    for tn, _zt, _hot in c.execute(
        "SELECT theme_name, MAX(zt_num) z, MAX(hot_num) h FROM kpl_theme_daily "
        "WHERE trade_date=? GROUP BY theme_name ORDER BY z DESC, h DESC LIMIT ?",
        (yest, cfg["score_pool_top"] * 2)):
        if _is_broad(tn):
            continue
        pool.append(tn)
        if len(pool) >= cfg["score_pool_top"]:
            break
    if not pool:
        return constituents, theme_of, names

    ph = ",".join("?" * len(pool))
    dph = ",".join("?" * len(dates))
    # 多日涨停趋势 (题材级 zt_num)
    trend: dict[str, dict] = {tn: {} for tn in pool}
    for tn, d, z in c.execute(
        f"SELECT theme_name, trade_date, MAX(zt_num) FROM kpl_theme_daily "
        f"WHERE trade_date IN ({dph}) AND theme_name IN ({ph}) "
        f"GROUP BY theme_name, trade_date", (*dates, *pool)):
        trend[tn][d] = z or 0
    # 成分 (昨日)
    members: dict[str, list] = {tn: [] for tn in pool}
    for tn, ts in c.execute(
        f"SELECT theme_name, ts_code FROM kpl_theme_daily "
        f"WHERE trade_date=? AND theme_name IN ({ph})", (yest, *pool)):
        members[tn].append(ts)
    # 梯队: 最新 limit_up_pool 连板数 (裸码)
    lub_date = c.execute("SELECT MAX(date) FROM limit_up_pool").fetchone()[0]
    streak_of: dict[str, int] = {}
    if lub_date:
        for code, st in c.execute(
                "SELECT code, streak FROM limit_up_pool WHERE date=?", (lub_date,)):
            streak_of[code] = st or 0

    scored = []
    for tn in pool:
        ts_list = members[tn]
        live = _theme_live_strength(rt_by_ts, fundflow, ts_list, cfg["live_zt_pct"])
        if not live:
            continue
        zt_seq = [trend[tn].get(d, 0) for d in reversed(dates)]  # 最旧→最新
        peak = max(zt_seq) if zt_seq else 0
        recent = zt_seq[-1] if zt_seq else 0
        persist = sum(zt_seq)  # 窗口累计涨停 = 接力赚钱效应强度
        ignition = recent >= peak and recent >= 2
        fading = peak >= cfg["fading_peak_min"] and recent <= peak * cfg["fading_ratio"]
        max_streak = ladder_cnt = 0
        for ts in ts_list:
            s = streak_of.get(ts.split(".")[0], 0)
            max_streak = max(max_streak, s)
            if s >= 2:
                ladder_cnt += 1
        scored.append({
            "theme": tn, "ts": ts_list, "live": live, "persist": persist,
            "ladder": max_streak + 0.5 * ladder_cnt,
            "ignition": ignition, "fading": fading, "zt_seq": zt_seq,
        })
    if not scored:
        return constituents, theme_of, names

    def _norm(fn):
        mx = max(fn(s) for s in scored) or 1.0
        return lambda s: fn(s) / mx
    nlive = _norm(lambda s: s["live"]["density"] * 0.6 + s["live"]["mean_pct"] / 10 * 0.4)
    npers = _norm(lambda s: s["persist"])
    nlad = _norm(lambda s: s["ladder"])
    for s in scored:
        base = cfg["w_live"] * nlive(s) + cfg["w_persist"] * npers(s) + cfg["w_ladder"] * nlad(s)
        if s["ignition"]:
            base *= (1 + cfg["ignition_bonus"])
        if s["fading"]:
            base *= cfg["fading_penalty"]
        s["score"] = round(base, 4)
    # 心法: 趋势钝化的主线不再做"借势/低吸"; 不只是降权, 直接踢出 — 不是主线了
    scored = [s for s in scored if not s["fading"]]
    scored.sort(key=lambda s: -s["score"])

    for s in scored[:cfg["keep_top_n"]]:
        names.append(s["theme"])
        for ts in s["ts"]:
            constituents.add(ts)
            theme_of.setdefault(ts, s["theme"])

    # 补充: 今日实时最强概念板块(过黑名单+成分区间+实时密度) — 捕捉昨日 KPL 未覆盖的新点火
    # 按实时密度排序逐个尝试, 只保留真正贡献新成分的板块(东财把同一概念拆 Ⅱ/Ⅲ, 去重),
    # 凑满 live_board_top 个即停 — 而非只看前 N 个再被密度刷掉.
    sup = sorted(
        ((bc, bl[0], bl[1], bl[2]) for bc, bl in board_live.items()
         if not _is_broad(bl[0])
         and cfg["live_board_member_min"] <= bl[2] <= cfg["live_board_member_max"]),
        key=lambda x: x[2], reverse=True)
    added = 0
    for bc, bn, _lp, _mem in sup[:cfg.get("live_board_scan_max", 40)]:
        if added >= cfg["live_board_top"]:
            break
        if bn in names:
            continue
        bts = [r[0] for r in c.execute(
            "SELECT ts_code FROM stock_concept_map WHERE board_code=?", (bc,))]
        live = _theme_live_strength(rt_by_ts, fundflow, bts, cfg["live_zt_pct"])
        if not live or live["density"] < cfg["live_board_density_min"]:
            continue
        new_ts = [ts for ts in bts if ts not in constituents]
        if not new_ts:  # 成分全被已选主线覆盖(近义板块) → 不重复记名
            continue
        names.append(bn)
        added += 1
        for ts in new_ts:
            constituents.add(ts)
            theme_of.setdefault(ts, bn)
    return constituents, theme_of, names


# ── 候选筛选 (实时口径) ────────────────────────────────────────────────
def _basics(c, ts_codes: list[str]) -> dict:
    """最新 daily_basic: ts_code → (circ_mv 万元, pe_ttm)."""
    if not ts_codes:
        return {}
    yest = c.execute("SELECT MAX(trade_date) FROM daily_basic").fetchone()[0]
    out: dict[str, tuple] = {}
    for i in range(0, len(ts_codes), 800):
        chunk = ts_codes[i:i + 800]
        ph = ",".join("?" * len(chunk))
        for r in c.execute(
            f"SELECT ts_code, circ_mv, pe_ttm FROM daily_basic "
            f"WHERE trade_date=? AND ts_code IN ({ph})",
            (yest, *chunk),
        ):
            out[r["ts_code"]] = (r["circ_mv"], r["pe_ttm"])
    return out


def _ret10_live(c, ts: str, price: float) -> float | None:
    rows = c.execute(
        "SELECT close FROM daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 10", (ts,)
    ).fetchall()
    if len(rows) < 10 or not rows[9]["close"]:
        return None
    return round((price - rows[9]["close"]) / rows[9]["close"] * 100, 2)


def _ret5_live(c, ts: str, price: float) -> float | None:
    rows = c.execute(
        "SELECT close FROM daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 5", (ts,)
    ).fetchall()
    if len(rows) < 5 or not rows[4]["close"]:
        return None
    return round((price - rows[4]["close"]) / rows[4]["close"] * 100, 2)


def _ma5_live(c, ts: str) -> float | None:
    """MA5 用最近 5 个已收盘交易日(不含今日盘中)的 close."""
    rows = c.execute(
        "SELECT close FROM daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 5", (ts,)
    ).fetchall()
    if len(rows) < 5:
        return None
    return sum(r["close"] for r in rows) / 5


def _trend_intact_b_live(c, ts: str, price: float) -> bool:
    """PATH_B 个股趋势(实时口径): price > MA5 或 ret5 > 0 (二选一)."""
    ma5 = _ma5_live(c, ts)
    if ma5 is not None and price > ma5:
        return True
    ret5 = _ret5_live(c, ts, price)
    return ret5 is not None and ret5 > 0


def _lub(c, code: str, iso_date: str) -> tuple:
    r = c.execute(
        "SELECT streak, blast_count FROM limit_up_pool WHERE code=? AND date=?", (code, iso_date)
    ).fetchone()
    return (r["streak"], r["blast_count"]) if r else (None, None)


def _classify_tier(path, pct, ret10, streak, blast, tier_cfg, ret10_max, blast_max) -> str:
    if streak and streak >= 2:
        return "sentiment_gauge"
    if ret10 is not None and ret10 >= ret10_max:
        return "avoid"
    if pct >= tier_cfg["limit_up_pct"]:
        # Path B 涨停归 avoid: 心法上"分歧低吸"≠追高 (常规已被 pct_chg_max 拦掉, 防御保留)
        if path == "buy_divergence":
            return "avoid"
        if blast and blast > blast_max:
            return "avoid"
        return "strength_watch"
    return "core"


def _top_board(c, ts: str, board_live: dict, fallback: str | None):
    top_theme, top_pct = fallback, None
    for (bc,) in c.execute("SELECT board_code FROM stock_concept_map WHERE ts_code=?", (ts,)):
        bl = board_live.get(bc)
        if bl and bl[2] >= 5 and (top_pct is None or bl[1] > top_pct):
            top_theme, top_pct = bl[0], bl[1]
    return top_theme, top_pct


def select_live(c, rt_by_ts, rt_by_code, fundflow, board_live, gauge, plimit,
                snapshot_time, today_iso):
    """实时选 S8 → (intraday_candidate_live 24列元组列表, meta{code:{tone,path,mainline}}, info)."""
    cfg = _cfg()
    base_cfg, tier_cfg = cfg["base"], cfg["tier"]
    regime = _latest_regime(c)
    tone = compute_tone(c, gauge, regime)
    path = tone["path"]
    pcfg = cfg["path_b"] if path == "buy_divergence" else cfg["path_a"]
    ret10_max = cfg["path_b"]["ret10_max"]

    constituents, theme_of, ml_names = identify_mainline(
        c, rt_by_ts, fundflow, board_live, cfg.get("mainline"))
    basics = _basics(c, list(constituents))
    lub_date = c.execute("SELECT MAX(date) FROM limit_up_pool").fetchone()[0]

    kept = []
    for ts in constituents:
        rt = rt_by_ts.get(ts)
        if not rt or rt.get("price") is None or rt.get("pct") is None:
            continue
        code = ts.split(".")[0]
        name = rt.get("name") or code
        if base_cfg["exclude_st"] and ("ST" in str(name).upper() or "退" in str(name)):
            continue
        circ_mv, pe = basics.get(ts, (None, None))
        circ_yi = (circ_mv or 0) / 1e4
        if circ_yi < base_cfg["circ_mv_yi_min"]:
            continue
        if base_cfg["require_pe_positive_or_null"] and pe is not None and pe <= 0:
            continue

        net = fundflow.get(code)
        vr = rt.get("vol_ratio")
        pct = rt["pct"]
        price = rt["price"]
        low = rt.get("low") or price

        if not (net and net > pcfg["net_main_min"]):
            continue
        if not (vr and vr > pcfg["vol_ratio_min"]):
            continue
        if circ_yi > pcfg["circ_mv_yi_max"]:
            continue
        if path == "buy_divergence":
            if pct <= pcfg["pct_chg_min"]:
                continue
            # 心法: "分歧低吸"≠追高, 当日涨幅必须 ≤ pct_chg_max (常 2%)
            if pct > pcfg.get("pct_chg_max", 999):
                continue
            # 心法: 个股趋势还在 (price>MA5 或 ret5>0)
            if pcfg.get("trend_require_above_ma5") and not _trend_intact_b_live(c, ts, price):
                continue
            entry_low, entry_high = round(low, 2), round(price, 2)
            stop = round(low * pcfg["stop_mult"], 2)
            tp = round(price * pcfg["take_profit_mult"], 2)
        else:
            if pct < pcfg["pct_chg_min"]:
                continue
            entry_low = round(price * pcfg["entry_low_mult"], 2)
            entry_high = round(price * pcfg["entry_high_mult"], 2)
            stop = round(price * pcfg["stop_mult"], 2)
            tp = round(price * pcfg["take_profit_mult"], 2)

        ret10 = _ret10_live(c, ts, price)
        # 心法: 个股 ret10 双向 — 既不能在下跌通道 (≤ret10_min), 也不能充分演绎 (≥ret10_max)
        if path == "buy_divergence":
            ret10_min = pcfg.get("ret10_min", -999)
            if ret10 is None or ret10 <= ret10_min:
                continue
        streak, blast = _lub(c, code, lub_date)
        tier = _classify_tier(path, pct, ret10, streak, blast, tier_cfg, ret10_max,
                              pcfg.get("blast_max", 99))
        if tier not in ("core", "strength_watch"):
            continue  # sentiment_gauge/avoid 不进盘中看板

        top_theme, top_pct = _top_board(c, ts, board_live, theme_of.get(ts))
        kept.append({
            "code": code, "ts_code": ts, "name": name,
            "industry": theme_of.get(ts) or "主线",
            "price": price, "pct": pct, "vol_ratio": vr,
            "turnover": rt.get("turnover"), "amount": rt.get("amount"),
            "speed_5min": rt.get("speed_5min"), "net_main": net,
            "entry_low": entry_low, "entry_high": entry_high, "stop": stop,
            "target_1": tp, "position_pct": plimit,
            "signal": PATH_LABEL[path], "top_theme": top_theme, "top_pct": top_pct,
            "tier": tier, "score": net,
        })

    kept.sort(key=lambda k: (-(k["score"] or 0), k["code"]))
    kept = kept[:int(cfg.get("max_board_candidates") or 15)]

    rows, meta = [], {}
    for k in kept:
        rows.append((
            snapshot_time, "s8", k["code"], k["ts_code"], k["name"], k["industry"], today_iso,
            k["price"], k["pct"], k["vol_ratio"], k["turnover"], k["amount"], k["speed_5min"],
            k["net_main"], k["entry_low"], k["entry_high"], k["stop"], k["target_1"], None,
            k["position_pct"], 0.0, k["signal"], k["top_theme"], k["top_pct"],
        ))
        meta[k["code"]] = {"tone": tone["tone"], "path": path,
                           "mainline": k["top_theme"] or k["industry"]}

    info = {
        "path": path, "tone": tone["tone"], "reasons": tone["reasons"],
        "gauge": tone["gauge"], "universe": len(constituents), "kept": len(rows),
        "regime": (regime or {}).get("regime_name"), "mainline": ml_names[:6],
    }
    return rows, meta, info
