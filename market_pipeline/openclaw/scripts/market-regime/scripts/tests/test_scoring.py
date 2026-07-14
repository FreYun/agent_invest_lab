"""六维打分的表驱动单元测试。

每个打分函数都需覆盖 5 个区间的典型值 + 边界值。边界值的归属以
`scoring.py` 的 docstring 为准(左闭右开, 带上强/弱不等号区分)。
"""

import pytest

from scoring import (
    _score_ma_for_index,
    build_scores_window,
    score_advance_decline,
    score_ma_position,
    score_sentiment_delta,
    score_sentiment_index,
    score_streak_height,
    score_volume_trend,
    total_score,
)


# --------------------------------------------------------------------------- #
# 维度 1: 指数 vs 均线
# --------------------------------------------------------------------------- #


def _make_index(close, ma5, ma20, ma60, ma250):
    return {"close": close, "ma5": ma5, "ma20": ma20, "ma60": ma60, "ma250": ma250}


class TestScoreMaForIndex:
    def test_plus_two_full_bull(self):
        # 站上 5/20/60/250 + MA 多头排列
        assert _score_ma_for_index(close=110, ma5=108, ma20=105, ma60=100, ma250=95) == 2

    def test_plus_two_close_equals_ma_not_strictly_above(self):
        # close == ma250: 不严格站上 → 退到 +1 (站上 5/20/60)
        assert _score_ma_for_index(close=100, ma5=99, ma20=98, ma60=97, ma250=100) == 1

    def test_plus_one_above_5_20_60_no_bull_align(self):
        # 站上 5/20/60, 但 MA250 混乱或未站上
        assert _score_ma_for_index(close=100, ma5=99, ma20=98, ma60=97, ma250=105) == 1

    def test_above_all_but_ma5_dead_cross_falls_to_zero(self):
        # 站上全部均线, 但 MA5 100 < MA20 101 (死叉). 2026-04-13 修订后:
        # +1 档要求 MA5 >= MA20, 所以这个 case 降到 0 (不再是 +1)。
        # 语义: 虽然 close 仍在均线之上, 短期动能已转弱 (顶部死叉信号)。
        assert _score_ma_for_index(close=110, ma5=100, ma20=101, ma60=99, ma250=95) == 0

    def test_plus_one_requires_ma5_not_below_ma20(self):
        # close 远上, MA5 刚好等于 MA20 → 不算死叉, 仍然 +1
        assert _score_ma_for_index(close=110, ma5=100, ma20=100, ma60=99, ma250=95) == 1

    def test_ma5_death_cross_blocks_plus_one_even_with_close_high(self):
        # 典型"顶部转折": close 还在 MA20 上, 但 MA5 跌穿 MA20
        # 实际案例: HS300 2026-03-12 (close 4688, MA5 4669, MA20 4685)
        assert _score_ma_for_index(close=4688, ma5=4669, ma20=4685, ma60=4676, ma250=4293) == 0

    def test_zero_above_20_not_60(self):
        assert _score_ma_for_index(close=100, ma5=101, ma20=98, ma60=102, ma250=95) == 0

    def test_zero_above_20_below_5(self):
        # 站上 20 但 close < ma5
        assert _score_ma_for_index(close=100, ma5=102, ma20=99, ma60=95, ma250=90) == 0

    def test_minus_one_below_20(self):
        # 跌破 20 但未空头排列
        assert _score_ma_for_index(close=98, ma5=100, ma20=99, ma60=95, ma250=105) == -1

    def test_minus_two_bear_alignment(self):
        # MA5 < MA20 < MA60 < MA250 → -2, 即使 close 位置不定
        assert _score_ma_for_index(close=80, ma5=85, ma20=90, ma60=95, ma250=100) == -2

    def test_minus_two_bear_alignment_even_if_close_above_ma5(self):
        # close 高于 ma5 也要 -2
        assert _score_ma_for_index(close=90, ma5=85, ma20=90, ma60=95, ma250=100) == -2


class TestScoreMaPosition:
    """两指数合并 = min() (2026-04-13 修订, 原为 avg+round)。

    背后原因: 避免"一强一弱"被算术平均抹平成假强势, 与 spec §6
    "宁可踏空不可站岗" 的设计原则一致。
    """

    def test_both_bull_returns_plus_two(self):
        bull = _make_index(110, 108, 105, 100, 95)
        assert score_ma_position(bull, bull) == 2

    def test_divergent_plus2_and_plus1_takes_min(self):
        # HS300 +2, CSI1000 +1 → min = +1 (修订前是 avg → +2)
        hs300 = _make_index(110, 108, 105, 100, 95)  # +2
        csi1000 = _make_index(100, 99, 98, 97, 105)  # +1 (站 5/20/60 未站 250)
        assert score_ma_position(hs300, csi1000) == 1

    def test_divergent_minus1_and_zero_takes_min(self):
        weak = _make_index(98, 100, 99, 95, 105)  # -1
        zero = _make_index(100, 101, 98, 102, 95)  # 0
        assert score_ma_position(weak, zero) == -1

    def test_both_zero(self):
        zero = _make_index(100, 101, 98, 102, 95)  # 0
        assert score_ma_position(zero, zero) == 0

    def test_both_bear_returns_minus_two(self):
        bear = _make_index(80, 85, 90, 95, 100)
        assert score_ma_position(bear, bear) == -2

    def test_one_strong_bull_one_bear_takes_bear(self):
        # 极端背离: HS300 多头+2 + CSI1000 空头-2 → min = -2
        # 大盘强中小盘崩 也算危险信号, 不应被 avg=0 掩盖
        bull = _make_index(110, 108, 105, 100, 95)  # +2
        bear = _make_index(80, 85, 90, 95, 100)  # -2
        assert score_ma_position(bull, bear) == -2

    def test_real_case_hs300_dead_cross_csi1000_bull_2026_03_12(self):
        # 实际数据: 2026-03-12
        # HS300 有 MA5 死叉 MA20 → 0 (修订后)
        # CSI1000 真·多头排列 → +2
        # 合成: min(0, 2) = 0 (修订前是 avg (+1,+2)=+1.5→+2, 假强势)
        hs300 = _make_index(close=4688, ma5=4669, ma20=4685, ma60=4676, ma250=4293)
        csi1000 = _make_index(close=8336, ma5=8300, ma20=8276, ma60=8003, ma250=7035)
        assert score_ma_position(hs300, csi1000) == 0


# --------------------------------------------------------------------------- #
# 维度 2: 涨跌家数比
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "ratio,expected",
    [
        (0.80, 2),
        (0.7501, 2),
        (0.75, 1),  # 闭区间上界归 +1
        (0.65, 1),
        (0.60, 1),  # 闭区间下界
        (0.59, 0),
        (0.50, 0),
        (0.40, 0),
        (0.3999, -1),
        (0.30, -1),
        (0.25, -1),
        (0.2499, -2),
        (0.10, -2),
        (0.0, -2),
    ],
)
def test_score_advance_decline(ratio, expected):
    assert score_advance_decline(ratio) == expected


# --------------------------------------------------------------------------- #
# 维度 3: 情绪(涨停-跌停)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "delta,expected",
    [
        (120, 2),
        (81, 2),
        (80, 1),
        (60, 1),
        (40, 1),
        (39, 0),
        (10, 0),
        (9, -1),
        (0, -1),
        (-40, -1),
        (-41, -2),
        (-100, -2),
    ],
)
def test_score_sentiment_delta(delta, expected):
    assert score_sentiment_delta(delta) == expected


# --------------------------------------------------------------------------- #
# 维度 4: 情绪评分
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value,expected",
    [
        (90, 2),
        (71, 2),
        (70, 1),
        (60, 1),
        (55, 1),
        (54.9, 0),
        (50, 0),
        (40, 0),
        (39.9, -1),
        (30, -1),
        (25, -1),
        (24.9, -2),
        (10, -2),
        (0, -2),
    ],
)
def test_score_sentiment_index(value, expected):
    assert score_sentiment_index(value) == expected


# --------------------------------------------------------------------------- #
# 维度 5: 最高连板高度
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "streak,expected",
    [
        (10, 2),
        (5, 2),
        (4, 1),
        (3, 0),
        (2, -1),
        (1, -2),
        (0, -2),
    ],
)
def test_score_streak_height(streak, expected):
    assert score_streak_height(streak) == expected


# --------------------------------------------------------------------------- #
# 维度 6: 成交量趋势
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "ratio,expected",
    [
        (1.80, 2),
        (1.31, 2),
        (1.30, 1),
        (1.20, 1),
        (1.10, 1),
        (1.09, 0),
        (1.0, 0),
        (0.90, 0),
        (0.89, -1),
        (0.80, -1),
        (0.70, -1),
        (0.69, -2),
        (0.50, -2),
    ],
)
def test_score_volume_trend(ratio, expected):
    assert score_volume_trend(ratio) == expected


# --------------------------------------------------------------------------- #
# 总分合成
# --------------------------------------------------------------------------- #


class TestTotalScore:
    def test_full_bull(self):
        scores = {k: 2 for k in [
            "ma_position", "advance_decline", "sentiment_delta",
            "sentiment_index", "streak_height", "volume_trend",
        ]}
        assert total_score(scores) == 12

    def test_full_bear(self):
        scores = {k: -2 for k in [
            "ma_position", "advance_decline", "sentiment_delta",
            "sentiment_index", "streak_height", "volume_trend",
        ]}
        assert total_score(scores) == -12

    def test_mixed(self):
        scores = {
            "ma_position": 1,
            "advance_decline": 0,
            "sentiment_delta": 1,
            "sentiment_index": 1,
            "streak_height": 0,
            "volume_trend": 0,
        }
        assert total_score(scores) == 3

    def test_missing_dims_treated_as_zero(self):
        # 缺失维度按 0 分计入 (spec §8)
        scores = {"ma_position": 2, "advance_decline": -1}
        assert total_score(scores) == 1

    def test_empty_dict(self):
        assert total_score({}) == 0

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError):
            total_score({"ma_position": 3})

    def test_non_int_raises(self):
        with pytest.raises(TypeError):
            total_score({"ma_position": 1.5})


# --------------------------------------------------------------------------- #
# build_scores_window — 3 日窗口构造, 以最近一次 emergency 为重置边界
# --------------------------------------------------------------------------- #


class TestBuildScoresWindow:
    def test_empty_history(self):
        assert build_scores_window([], 5) == [5]

    def test_no_emergency_takes_last_two(self):
        prior = [
            {"date": "2026-04-01", "total_score": 1, "emergency_switch": False},
            {"date": "2026-04-02", "total_score": 2, "emergency_switch": False},
            {"date": "2026-04-03", "total_score": 3, "emergency_switch": False},
        ]
        assert build_scores_window(prior, 4) == [2, 3, 4]

    def test_emergency_resets_window(self):
        # 4/2 有 emergency → 只取 4/3 + 今日 (不足 3 日)
        prior = [
            {"date": "2026-04-01", "total_score": 5, "emergency_switch": False},
            {"date": "2026-04-02", "total_score": 0, "emergency_switch": True},
            {"date": "2026-04-03", "total_score": 1, "emergency_switch": False},
        ]
        assert build_scores_window(prior, 2) == [1, 2]

    def test_emergency_at_last_day_resets_to_today_only(self):
        prior = [
            {"date": "2026-04-01", "total_score": 5, "emergency_switch": False},
            {"date": "2026-04-02", "total_score": 0, "emergency_switch": True},
        ]
        assert build_scores_window(prior, 1) == [1]
