"""六维打分 — 纯函数。

输入原始数据、输出 -2 ~ +2 的整数得分。规则来源见 spec §4 和
`references/scoring.md`。每个函数都是纯函数，便于测试。

维度:
  1. score_ma_position        指数 vs 均线（HS300 + CSI1000 平均）
  2. score_advance_decline    涨跌家数比
  3. score_sentiment_delta    情绪（涨停数 − 跌停数）
  4. score_sentiment_index    情绪评分（0-100 复合指标）
  5. score_streak_height      最高连板高度
  6. score_volume_trend       成交量趋势（MA5/MA20）

合成:
  total_score(dim_scores: dict) -> int     范围 [-12, +12]
  score_all_dims(raw)                      把 RawMarketData 转成六维 dict
  build_scores_window(prior_entries, ...)  逃生门重置后的 3 日窗口构造

所有阈值采用 "向下走格子" 的左闭右开区间, 与 spec §4 的表对齐:
如某维度 "≥ X" 归 +2, 则 X 归 +1 而非 +2, 除非 spec 明确标注 ">X"。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from parser import RawMarketData


# --------------------------------------------------------------------------- #
# 维度 1: 指数 vs 均线
# --------------------------------------------------------------------------- #


class IndexMA(TypedDict):
    """单个指数的收盘价与 5/20/60/250 日 SMA。"""

    close: float
    ma5: float
    ma20: float
    ma60: float
    ma250: float


def _score_ma_for_index(close: float, ma5: float, ma20: float, ma60: float, ma250: float) -> int:
    """单个指数按 spec §4 的均线规则打 -2 ~ +2 分。

    +2: 站上 5/20/60/250 且 MA 多头排列 (MA5>MA20>MA60>MA250)
    +1: 站上 5/20/60 且 MA5 >= MA20 (短期均线未死叉)
     0: 站上 20, 但短期均线已死叉 (MA5 < MA20) 或 60 位置不定
    -1: 跌破 20
    -2: MA 空头排列 (MA5<MA20<MA60<MA250)

    实现细节:
    - "站上" (above_X) 采用 >= 包含持平, 与中文金融约定一致 (close 刚好
      等于均线应判为 "站稳" 而非 "跌破")。合成 flat DF 因此得 +1。
    - "多头/空头排列" 采用严格 > / <, 相等的均线不构成层级。
    - +1 档新增 "MA5 >= MA20" 约束 (2026-04-13 修订): spec §4 原表只看
      close 位置, 无法识别 "顶部死叉但 close 还没破 MA20" 的见顶信号。
      例: HS300 2026-03-12 close 4688 站上全部均线但 MA5 4669 < MA20 4685,
      原规则给 +1, 新规则降到 0 (更真实反映趋势转弱)。
    """
    above_5 = close >= ma5
    above_20 = close >= ma20
    above_60 = close >= ma60
    above_250 = close >= ma250
    bull_alignment = ma5 > ma20 > ma60 > ma250
    bear_alignment = ma5 < ma20 < ma60 < ma250
    ma5_not_dead_cross = ma5 >= ma20  # 短期均线未死叉

    # 最悲观: 空头排列直接 -2, 不论价格位置
    if bear_alignment:
        return -2
    # 最乐观: 站上全部均线 + 多头排列
    if above_5 and above_20 and above_60 and above_250 and bull_alignment:
        return 2
    # +1: 站上 5/20/60, 且 MA5 未死叉 MA20 (新增约束)
    if above_5 and above_20 and above_60 and ma5_not_dead_cross:
        return 1
    # 0: 站上 20, 60 位置不定 (或短期死叉)
    if above_20:
        return 0
    # 其余 (跌破 20 但未空头排列) → -1
    return -1


def score_ma_position(hs300: IndexMA, csi1000: IndexMA) -> int:
    """HS300 与 CSI1000 各自打分, 取最小值 (2026-04-13 修订)。

    spec §4 原规定是"算术平均四舍五入", 示例 "+2 + +1 → +1.5 → +2"。
    但这会让一个强指数把另一个弱指数拉上去 (例: 2026-03-12 HS300 +1 +
    CSI1000 +2 → +2 假强势, 实际是普跌日)。

    改为 min() 的理由:
    - 与 spec §6 "宁可踏空不可站岗" 的设计原则一致 (3 日确认的悲观缓冲、
      上行逃生门的 AND 严格条件, 都是这个原则)
    - 两个指数任何一个转弱, MA 维度整体就降级
    - 大盘 (HS300) 与中小盘 (CSI1000) 背离本身就是风险信号, 不应被
      算术平均抹平

    此改动使 spec §4 的 "+2 + +1 → +2" 示例不再成立, 改为 → +1。
    """
    s1 = _score_ma_for_index(
        hs300["close"], hs300["ma5"], hs300["ma20"], hs300["ma60"], hs300["ma250"]
    )
    s2 = _score_ma_for_index(
        csi1000["close"], csi1000["ma5"], csi1000["ma20"], csi1000["ma60"], csi1000["ma250"]
    )
    return min(s1, s2)


# --------------------------------------------------------------------------- #
# 维度 2: 涨跌家数比
# --------------------------------------------------------------------------- #


def score_advance_decline(ratio: float) -> int:
    """涨跌家数比 → -2 ~ +2。

    >0.75 → +2 ; [0.60, 0.75] → +1 ; [0.40, 0.60) → 0
    [0.25, 0.40) → -1 ; <0.25 → -2

    注: spec 区间 "0.60-0.75" 视为闭区间 (含 0.75), 故 0.75 → +1 而非 +2。
    """
    if ratio > 0.75:
        return 2
    if ratio >= 0.60:
        return 1
    if ratio >= 0.40:
        return 0
    if ratio >= 0.25:
        return -1
    return -2


# --------------------------------------------------------------------------- #
# 维度 3: 情绪(涨停-跌停)
# --------------------------------------------------------------------------- #


def score_sentiment_delta(delta: int) -> int:
    """涨停数 − 跌停数 → -2 ~ +2。

    >80 → +2 ; [40, 80] → +1 ; [10, 40) → 0 ; [-40, 10) → -1 ; <-40 → -2
    """
    if delta > 80:
        return 2
    if delta >= 40:
        return 1
    if delta >= 10:
        return 0
    if delta >= -40:
        return -1
    return -2


# --------------------------------------------------------------------------- #
# 维度 4: 情绪评分 (0-100 复合指标)
# --------------------------------------------------------------------------- #


def score_sentiment_index(value: float) -> int:
    """复盘 MD 情绪温度计的 "情绪评分 N/100" → -2 ~ +2。

    >70 → +2 ; [55, 70] → +1 ; [40, 55) → 0 ; [25, 40) → -1 ; <25 → -2
    """
    if value > 70:
        return 2
    if value >= 55:
        return 1
    if value >= 40:
        return 0
    if value >= 25:
        return -1
    return -2


# --------------------------------------------------------------------------- #
# 维度 5: 最高连板高度
# --------------------------------------------------------------------------- #


def score_streak_height(streak: int) -> int:
    """最高连板高度 → -2 ~ +2。

    ≥5 → +2 ; 4 → +1 ; 3 → 0 ; 2 → -1 ; ≤1 → -2
    """
    if streak >= 5:
        return 2
    if streak == 4:
        return 1
    if streak == 3:
        return 0
    if streak == 2:
        return -1
    return -2


# --------------------------------------------------------------------------- #
# 维度 6: 成交量趋势 (MA5/MA20)
# --------------------------------------------------------------------------- #


def score_volume_trend(ratio: float) -> int:
    """5 日均量 / 20 日均量 → -2 ~ +2。

    >1.30 → +2 ; [1.10, 1.30] → +1 ; [0.90, 1.10) → 0
    [0.70, 0.90) → -1 ; <0.70 → -2
    """
    if ratio > 1.30:
        return 2
    if ratio >= 1.10:
        return 1
    if ratio >= 0.90:
        return 0
    if ratio >= 0.70:
        return -1
    return -2


# --------------------------------------------------------------------------- #
# 总分合成
# --------------------------------------------------------------------------- #


DIMENSION_KEYS = (
    "ma_position",
    "advance_decline",
    "sentiment_delta",
    "sentiment_index",
    "streak_height",
    "volume_trend",
)


def total_score(dim_scores: dict) -> int:
    """将六维得分加总, 返回总分。

    缺失维度按 0 分计入 (spec §8 降级策略)。未知键会被忽略但会抛警告,
    以防上游传错字段名。
    """
    total = 0
    for key in DIMENSION_KEYS:
        v = dim_scores.get(key, 0)
        if not isinstance(v, int):
            raise TypeError(f"dim_scores[{key!r}] must be int, got {type(v).__name__}")
        if v < -2 or v > 2:
            raise ValueError(f"dim_scores[{key!r}] out of range [-2, 2]: {v}")
        total += v
    return total


# --------------------------------------------------------------------------- #
# 编排辅助: RawMarketData → 六维 dict + 3 日窗口构造
# --------------------------------------------------------------------------- #


def score_all_dims(raw: "RawMarketData") -> dict:
    """把 RawMarketData 转成六维得分 dict。

    缺失维度按 0 分计入 (spec §8 降级策略)。
    """
    scores: dict = {}

    if raw.hs300 is not None and raw.csi1000 is not None:
        scores["ma_position"] = score_ma_position(raw.hs300, raw.csi1000)
    else:
        scores["ma_position"] = 0

    scores["advance_decline"] = (
        score_advance_decline(raw.advance_decline_ratio)
        if raw.advance_decline_ratio is not None
        else 0
    )
    scores["sentiment_delta"] = (
        score_sentiment_delta(raw.sentiment_delta)
        if raw.sentiment_delta is not None
        else 0
    )
    scores["sentiment_index"] = (
        score_sentiment_index(raw.sentiment_index)
        if raw.sentiment_index is not None
        else 0
    )
    scores["streak_height"] = (
        score_streak_height(raw.max_streak) if raw.max_streak is not None else 0
    )
    scores["volume_trend"] = (
        score_volume_trend(raw.volume_ratio_5_20)
        if raw.volume_ratio_5_20 is not None
        else 0
    )

    return scores


def build_scores_window(prior_entries: list, today_score: int) -> list:
    """从历史 log 条目构造 3 日窗口, 以最近一次 emergency 为重置边界。

    spec §6 规定: 逃生门触发后, 后续切换从新 regime 重新计算 3 日窗口。
    实现上等价于: 只取 emergency 之后的历史分数, 加上今日, 作为窗口。

    prior_entries 必须已按日期升序排列, 且不包含今日。
    """
    # 从后往前找最近的 emergency
    reset_idx = -1
    for i in range(len(prior_entries) - 1, -1, -1):
        if prior_entries[i]["emergency_switch"]:
            reset_idx = i
            break

    effective = prior_entries[reset_idx + 1 :] if reset_idx >= 0 else prior_entries
    # 最多取最近 2 条历史 + 今日 = 3 日窗口
    window = [e["total_score"] for e in effective[-2:]] + [today_score]
    return window
