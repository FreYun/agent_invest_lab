"""S8 主线分歧/借势 自适应选股 — 两路径阈值常量.

强结构(Path A borrow_strength)无历史样本, 阈值按廖峥/老于框架推演写死, 标注"待校准";
弱结构(Path B buy_divergence)阈值已用 2026-05-21 手工复盘验证集校准.
"""

from __future__ import annotations

# ── 大势定调路由 (analysis.compute_tone) ──────────────────────────────
TONE = {
    # 强结构触发: 同时满足
    "strong_regimes": ("强牛", "强势震荡"),
    "strong_min_score": 3,            # total_score ≥ 3
    "strong_min_advance": 2500,       # 涨家数 ≥ 2500 (赚钱效应健康)
    "strong_max_limitdown": 30,       # 跌停家数 ≤ 30
    # 弱结构触发: 任一满足
    "weak_regimes": ("弱势震荡", "熊"),
    "weak_max_advance": 1500,         # 涨家数 < 1500 → 赚钱效应塌陷
    "weak_min_limitdown": 50,         # 跌停家数 ≥ 50 → 塌陷
    "cliff_ratio": 0.5,               # 今日涨家数 < 0.5×近3日峰值 → 断崖
    "limitdown_pct": -9.5,            # daily.pct_chg ≤ 此值 计为跌停 proxy
}

# ── 主线识别 (analysis.identify_mainline) ─────────────────────────────
MAINLINE = {
    "top_n": 6,                       # 当日题材按 zt_num desc, hot_num desc 取 top_n
    "min_streak_for_top": 2,          # limit_up_pool streak ≥ 此值 才算连板
    # 心法: 钝化(fading)主线不再算主线 — 趋势没了, 不做低吸也不接力
    "fading_trend_days": 3,           # 回看近 N 个交易日 zt_num 趋势
    "fading_peak_min": 4,             # 前高(窗口内峰值)涨停家数 ≥ 此值 才有"钝化"概念
    "fading_ratio": 0.4,              # 今日 zt ≤ 前高×此比例 → 视为钝化, 剔除
}

# ── 候选基础门槛 (两路径共享) ──────────────────────────────────────────
BASE = {
    "circ_mv_yi_min": 20,             # 流通市值下限(亿)
    "exclude_st": True,
    "require_pe_positive_or_null": True,  # pe_ttm > 0 或为空
}

# ── Path B 弱结构·买分歧低吸 (2026-05-27 校准: 真回调心法) ──────────────
# 心法: ①主线 ②趋势还在 ③当日回调 → 才是"分歧低吸"。
# 原口径只判抗跌(pct>-3), 把追高涨停也算进来, 实测 51/71 都是 max_ret≈0 的追高。
PATH_B = {
    "net_main_min": 0,                # 主力净流入 > 0 (逆势资金回流)
    "vol_ratio_min": 0.8,             # 量比 > 0.8 (回调日量能本就该缩, 原 1.0 偏严)
    "circ_mv_yi_max": 600,            # 流通市值上限(亿)
    "pct_chg_min": -3.0,              # 抗跌下限: pct_chg > -3
    "pct_chg_max": 2.0,               # 回调上限: pct_chg ≤ 2 (涨停=追高, 不是低吸)
    "ret10_max": 25.0,                # 近10日累计涨幅 < 25% (排除充分演绎)
    "ret10_min": 0.0,                 # 近10日累计涨幅 > 0 (个股趋势还在, 非下跌通道)
    "trend_require_above_ma5": True,  # 个股趋势: close > MA5 或 ret5 > 0 (二选一)
    # 买点(回踩低吸, 不追高)
    "entry_low_ref": "low",           # entry_zone_low = 当日低点
    "entry_high_ref": "close",        # entry_zone_high = 当日收盘
    "stop_mult": 0.97,                # stop = 当日低点 × 0.97
    "take_profit_mult": 1.10,         # take_profit = 当日收盘 × 1.10
}

# ── Path A 强结构·借势接力 (⚠️ 待校准, 无历史样本) ─────────────────────
PATH_A = {
    "net_main_min": 0,                # 主力净流入 > 0 (资金进攻)
    "vol_ratio_min": 1.5,             # 量比 > 1.5 (放量进攻, 严于弱结构)
    "pct_chg_min": 3.0,              # 当日跟随主升: pct_chg ≥ 3 或涨停
    "circ_mv_yi_max": 800,            # 流通市值上限(亿, 强结构放宽)
    "streak_relay_max": 3,            # 优先连板梯队接力位 streak 1~3
    "blast_max": 2,                   # 排除高位放量见顶: 炸板数 > 此值 降级
    # 买点(接力, 可容忍小幅高开)
    "entry_low_mult": 0.98,           # entry_zone_low = 当日收盘 × 0.98
    "entry_high_mult": 1.05,          # entry_zone_high = 当日收盘 × 1.05
    "stop_mult": 0.93,                # stop = 当日收盘 × 0.93
    "take_profit_mult": 1.15,         # take_profit = 当日收盘 × 1.15
}

# ── 分级 (两路径共享语义, 阈值微调) ────────────────────────────────────
TIER = {
    "limit_up_pct": 9.5,              # pct_chg ≥ 此值 视为涨停(高强度)
    # core: 低位抗跌温和 + 逆势资金 → 进看板
    # strength_watch: 高强度涨停但偏弹性 → 进看板 (仅 Path A 借势接力)
    # sentiment_gauge: 连板龙头风向标(streak≥2 或 全市场最高连板) → 只记录
    # avoid: 充分演绎/过热(ret10 ≥ ret10_max) 或 Path B 下涨停(追高非低吸) → 只记录
}

# 写入 s8_candidates 的档位(actionable, 进看板)
ACTIONABLE_TIERS = ("core", "strength_watch")
# 仅记录在 s8_select_runs 的档位
RECORD_ONLY_TIERS = ("sentiment_gauge", "avoid")

# 看板候选数上限(按 signal_score 截断; select_runs 仍记全量)
MAX_BOARD_CANDIDATES = 15


def deep_merge(base: dict, override: dict | None) -> dict:
    """浅层 section + 内层 key 的覆盖合并, 用于 CLI/调用方覆盖默认阈值."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    if not override:
        return out
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def full_config(override: dict | None = None) -> dict:
    cfg = {
        "tone": dict(TONE),
        "mainline": dict(MAINLINE),
        "base": dict(BASE),
        "path_b": dict(PATH_B),
        "path_a": dict(PATH_A),
        "tier": dict(TIER),
        "max_board_candidates": MAX_BOARD_CANDIDATES,
    }
    return deep_merge(cfg, override)
