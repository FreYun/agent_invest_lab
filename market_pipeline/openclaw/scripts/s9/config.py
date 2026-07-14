"""S9 大市值 regime 自适应选股 — 阈值常量.

研究路径与结论存档: research/stock_select_factor/summary.md
- 8 年(2018-2026) 多次风格切换上验证, 全样本 bootstrap Sharpe CI [0.26,1.58] prob>0=100%
- robustness sweep 67% 配置超基准, 最优区 MA150-200 连续山脊
- 经济逻辑: 大票截面选股 alpha 随 risk-on/off regime 镜像翻转,
  风险偏好高(沪深300>MA150)时距高点动量得分, 偏好低时低特质波动得分
- 已知盲区:
  * MA 趋势滤波在风格拐点会慢半拍 (MA系统的数学性质)
  * 防守腿 idiovol 在强牛市跑输基准 (低 beta 拖累)
  * OOS 22-26 bootstrap CI 下沿 -0.10 略含0 (4年样本天然窄, 严格绿灯还差一口气)
  * 建议先 shadow 模拟盘 6-12 月再上仓位
"""

from __future__ import annotations

# ── Regime 开关 (analysis.compute_regime) ─────────────────────────────
REGIME = {
    "ma_window": 150,                 # 沪深300 vs 此窗口均线
    "index_code": "000300.SH",        # 趋势锚: 沪深300
    "offensive_threshold": 0.0,       # 偏离 > 此值 → 进攻 regime
}

# ── 候选池 (filter.build_universe) ────────────────────────────────────
BASE = {
    "min_total_mv_yi": 100,           # 总市值下限(亿元) → daily_basic.total_mv ≥ 1e6 万元
    "exclude_st": True,
    "min_warmup_bars": 250,           # 历史数据下限: 不足无法算因子
    "exclude_recent_listing_days": 60, # 上市 < 60 日新股剔除
}

# ── 进攻腿: 距 52 周高点 (filter.compute_factor) ───────────────────────
OFFENSIVE = {
    "factor_name": "high_prox",
    "window": 120,                    # close / max(close, 120日) 越接近1越强
    "label": "距52周高点·进攻",
}

# ── 防守腿: 低特质波动率 ─────────────────────────────────────────────────
DEFENSIVE = {
    "factor_name": "idiovol_low",
    "window": 40,                     # 40日特质波动(剔除池均值后的残差std)
    "label": "低特质波动·防守",
}

# ── 组合规模 ─────────────────────────────────────────────────────────
TOP_K = 20                           # 每次选 top 20 等权
REBALANCE_BARS = 10                  # 信息字段(周级调仓); 选股本身每日重算

# ── 看板上限(进 s9_candidates 的数量) ─────────────────────────────────
MAX_BOARD_CANDIDATES = 20


def deep_merge(base: dict, override: dict | None) -> dict:
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
        "regime": dict(REGIME),
        "base": dict(BASE),
        "offensive": dict(OFFENSIVE),
        "defensive": dict(DEFENSIVE),
        "top_k": TOP_K,
        "rebalance_bars": REBALANCE_BARS,
        "max_board_candidates": MAX_BOARD_CANDIDATES,
    }
    return deep_merge(cfg, override)
