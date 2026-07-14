"""regime_rules.py 单元测试。

覆盖:
1. score_to_raw_regime — 5 档边界值
2. apply_3day_confirmation
   - 维持当前档位
   - 向更乐观方向: 相邻一档 + 3 日条件满足/不满足
   - 向更悲观方向: 相邻一档 + 3 日条件满足/不满足 + 缓冲区生效
   - 跨档一次 (只允许下行)
   - switch_warning 文案
   - bootstrap 模式 (scores < 3 日)
3. check_emergency_hatch
   - 下行 OR: 每条子条件单独触发
   - 下行缺数据时的降级
   - 上行 AND: 单条件不足 → 不触发; 三条件齐全 → 触发
   - 下行优先级高于上行
4. apply_emergency_switch — 端点裁剪
5. lookup_playbook — 5 档输出齐全, 含/不含 mode 字段
"""

import pytest

from regime_rules import (
    DEFAULT_REGIME,
    PLAYBOOKS,
    REGIME_ORDER,
    apply_3day_confirmation,
    apply_emergency_switch,
    check_emergency_hatch,
    lookup_playbook,
    score_to_raw_regime,
)


# --------------------------------------------------------------------------- #
# score_to_raw_regime
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "score,expected",
    [
        (12, "STRONG_BULL"),
        (7, "STRONG_BULL"),
        (6, "STRONG_RANGE"),
        (2, "STRONG_RANGE"),
        (1, "NEUTRAL_RANGE"),
        (0, "NEUTRAL_RANGE"),
        (-1, "NEUTRAL_RANGE"),
        (-2, "WEAK_RANGE"),
        (-5, "WEAK_RANGE"),
        (-6, "BEAR"),
        (-12, "BEAR"),
    ],
)
def test_score_to_raw_regime_boundaries(score, expected):
    assert score_to_raw_regime(score) == expected


# --------------------------------------------------------------------------- #
# apply_3day_confirmation: 维持
# --------------------------------------------------------------------------- #


class TestConfirmationStay:
    def test_today_same_as_last_no_warning(self):
        r = apply_3day_confirmation("NEUTRAL_RANGE", [0, 1, 0])
        assert r["regime"] == "NEUTRAL_RANGE"
        assert r["switched"] is False
        assert r["switch_warning"] is None

    def test_empty_history(self):
        r = apply_3day_confirmation("NEUTRAL_RANGE", [])
        assert r["regime"] == "NEUTRAL_RANGE"
        assert r["bootstrap"] is True


# --------------------------------------------------------------------------- #
# apply_3day_confirmation: 向乐观
# --------------------------------------------------------------------------- #


class TestConfirmationBullish:
    def test_single_day_in_new_zone_no_switch_with_warning(self):
        # 昨日及前日为 0 (NEUTRAL), 今日 +3 进入 STRONG_RANGE 区间
        r = apply_3day_confirmation("NEUTRAL_RANGE", [0, 0, 3])
        assert r["regime"] == "NEUTRAL_RANGE"
        assert r["switched"] is False
        assert r["switch_warning"] is not None
        assert "强势震荡" in r["switch_warning"]
        assert "+3" in r["switch_warning"]

    def test_three_days_in_new_zone_switch_one_step(self):
        # 3 日均 >= +2 → 切到 STRONG_RANGE
        r = apply_3day_confirmation("NEUTRAL_RANGE", [2, 3, 4])
        assert r["regime"] == "STRONG_RANGE"
        assert r["switched"] is True
        assert r["switch_warning"] is None

    def test_bullish_does_not_jump_two_tiers(self):
        # 3 日均 >= +7 (STRONG_BULL 区间), 但只能一次升一档
        r = apply_3day_confirmation("NEUTRAL_RANGE", [7, 8, 9])
        # 相邻档是 STRONG_RANGE (lower=+2), 3 日均 >= +2 满足 → 切 STRONG_RANGE
        assert r["regime"] == "STRONG_RANGE"
        assert r["switched"] is True

    def test_bullish_edge_exact_lower_bound(self):
        # 3 日均恰好 == 新档位下限 (+2) → 满足 (>=)
        r = apply_3day_confirmation("NEUTRAL_RANGE", [2, 2, 2])
        assert r["regime"] == "STRONG_RANGE"

    def test_bullish_one_day_below_lower_bound_no_switch(self):
        # 3 日里有一天 < +2 → 不切
        r = apply_3day_confirmation("NEUTRAL_RANGE", [2, 1, 4])
        assert r["regime"] == "NEUTRAL_RANGE"
        assert r["switched"] is False
        # 有 warning (今日 +4 在 STRONG_RANGE 区间)
        assert r["switch_warning"] is not None


# --------------------------------------------------------------------------- #
# apply_3day_confirmation: 向悲观 + 缓冲区
# --------------------------------------------------------------------------- #


class TestConfirmationBearish:
    def test_bearish_one_step_with_buffer(self):
        # 从 NEUTRAL 切到 WEAK_RANGE 需要 3 日 <= -3 (WEAK 上限 -2, 缓冲 -1)
        r = apply_3day_confirmation("NEUTRAL_RANGE", [-3, -4, -3])
        assert r["regime"] == "WEAK_RANGE"
        assert r["switched"] is True

    def test_bearish_buffer_blocks_switch_at_upper_bound(self):
        # 3 日 == -2 (WEAK_RANGE 上限) 但缓冲要求 <= -3 → 不切
        r = apply_3day_confirmation("NEUTRAL_RANGE", [-2, -2, -2])
        assert r["regime"] == "NEUTRAL_RANGE"
        assert r["switched"] is False
        # 但今日 -2 在 WEAK_RANGE 区间 → 有 warning
        assert r["switch_warning"] is not None
        assert "弱势震荡" in r["switch_warning"]

    def test_bearish_one_day_above_buffer_no_switch(self):
        # 3 日里有一天 > -3 → 不切
        r = apply_3day_confirmation("NEUTRAL_RANGE", [-3, -2, -4])
        assert r["regime"] == "NEUTRAL_RANGE"
        assert r["switched"] is False

    def test_bearish_cross_tier_allowed(self):
        # 从 STRONG_RANGE 跨过 NEUTRAL 直接到 WEAK_RANGE
        # 需要: 3 日 <= NEUTRAL 上限-1 = 0 AND 3 日 <= WEAK 上限-1 = -3
        # 故所有 3 日都要 <= -3
        r = apply_3day_confirmation("STRONG_RANGE", [-3, -4, -5])
        assert r["regime"] == "WEAK_RANGE"
        assert r["switched"] is True

    def test_bearish_cross_tier_falls_back_to_one_step(self):
        # 3 日满足相邻档 (<=0) 但不满足跨档 (<=-3)
        r = apply_3day_confirmation("STRONG_RANGE", [0, -1, -2])
        # target1 = NEUTRAL_RANGE 上限=1, 缓冲-1 → 条件 <=0
        # [0, -1, -2] 全部 <= 0 → 满足 NEUTRAL 切换
        assert r["regime"] == "NEUTRAL_RANGE"
        assert r["switched"] is True

    def test_bearish_only_considers_cross_when_today_crosses(self):
        # 昨日 STRONG_RANGE, 今日 NEUTRAL (相邻), 不该触发 cross 判断
        r = apply_3day_confirmation("STRONG_RANGE", [0, 0, 0])
        # target1 = NEUTRAL 条件 <=0 → 满足 → 切 NEUTRAL
        assert r["regime"] == "NEUTRAL_RANGE"

    def test_bearish_cross_from_neutral_to_bear_needs_two_tiers(self):
        # NEUTRAL → BEAR (跨 WEAK): 需要 3 日 <= WEAK 上限-1 = -3 AND <= BEAR 上限-1 = -7
        # 即全部 <= -7
        r = apply_3day_confirmation("NEUTRAL_RANGE", [-7, -8, -9])
        assert r["regime"] == "BEAR"
        assert r["switched"] is True

    def test_bearish_cross_partial_fails_to_cross_but_does_one_step(self):
        # NEUTRAL → BEAR 不够, 但够 NEUTRAL → WEAK
        r = apply_3day_confirmation("NEUTRAL_RANGE", [-4, -5, -6])
        # target1=WEAK 条件 <=-3 → 全部满足
        # target2=BEAR 条件 <=-7 → 不满足
        # 结果: 降到 WEAK
        assert r["regime"] == "WEAK_RANGE"


# --------------------------------------------------------------------------- #
# apply_3day_confirmation: bootstrap
# --------------------------------------------------------------------------- #


class TestConfirmationBootstrap:
    def test_one_day_history_bootstrap(self):
        r = apply_3day_confirmation("NEUTRAL_RANGE", [5])
        assert r["regime"] == "NEUTRAL_RANGE"
        assert r["bootstrap"] is True
        assert r["switched"] is False
        assert r["switch_warning"] is not None  # today 在新区间应有提示

    def test_two_day_history_bootstrap(self):
        r = apply_3day_confirmation("NEUTRAL_RANGE", [3, 4])
        assert r["bootstrap"] is True
        assert r["regime"] == "NEUTRAL_RANGE"
        assert r["switched"] is False


# --------------------------------------------------------------------------- #
# check_emergency_hatch: 下行
# --------------------------------------------------------------------------- #


class TestEmergencyHatchDown:
    def test_total_drop_5_triggers(self):
        r = check_emergency_hatch(today_score=-3, yesterday_score=3)
        assert r["triggered"] is True
        assert r["direction"] == "down"
        assert "total_drop_5" in r["reasons"]

    def test_total_drop_4_does_not_trigger(self):
        r = check_emergency_hatch(today_score=-1, yesterday_score=3)
        assert r["triggered"] is False

    def test_index_crash_hs300(self):
        r = check_emergency_hatch(
            today_score=0,
            yesterday_score=0,
            hs300_pct_change=-3.5,
            csi1000_pct_change=-1.0,
            advance_decline_ratio=0.18,
        )
        assert r["triggered"] is True
        assert r["direction"] == "down"
        assert "index_crash" in r["reasons"]

    def test_index_crash_csi1000_alone_triggers(self):
        # OR: 只要一个指数跌 3%+
        r = check_emergency_hatch(
            today_score=0,
            yesterday_score=0,
            hs300_pct_change=-1.0,
            csi1000_pct_change=-3.2,
            advance_decline_ratio=0.20,
        )
        assert r["triggered"] is True
        assert "index_crash" in r["reasons"]

    def test_index_crash_needs_breadth_too(self):
        # 指数跌 3%+ 但涨跌比 > 0.25 → 不触发 index_crash
        r = check_emergency_hatch(
            today_score=0,
            yesterday_score=0,
            hs300_pct_change=-3.5,
            advance_decline_ratio=0.30,
        )
        assert r["triggered"] is False

    def test_sentiment_collapse_low_index(self):
        r = check_emergency_hatch(
            today_score=0, yesterday_score=0, sentiment_index=15
        )
        assert r["triggered"] is True
        assert "sentiment_collapse" in r["reasons"]

    def test_sentiment_collapse_delta(self):
        r = check_emergency_hatch(
            today_score=0, yesterday_score=0, sentiment_delta=-45
        )
        assert r["triggered"] is True
        assert "sentiment_collapse" in r["reasons"]

    def test_sentiment_index_21_does_not_collapse(self):
        r = check_emergency_hatch(
            today_score=0, yesterday_score=0, sentiment_index=21, sentiment_delta=-39
        )
        assert r["triggered"] is False

    def test_multiple_reasons_all_recorded(self):
        r = check_emergency_hatch(
            today_score=-6,
            yesterday_score=0,
            hs300_pct_change=-4,
            advance_decline_ratio=0.10,
            sentiment_index=15,
        )
        assert r["triggered"] is True
        assert set(r["reasons"]) >= {"total_drop_5", "index_crash", "sentiment_collapse"}

    def test_no_yesterday_still_allows_other_triggers(self):
        r = check_emergency_hatch(
            today_score=0,
            yesterday_score=None,
            sentiment_index=10,
        )
        assert r["triggered"] is True
        assert "total_drop_5" not in r["reasons"]
        assert "sentiment_collapse" in r["reasons"]


# --------------------------------------------------------------------------- #
# check_emergency_hatch: 上行
# --------------------------------------------------------------------------- #


class TestEmergencyHatchUp:
    def test_all_three_conditions_triggers(self):
        r = check_emergency_hatch(
            today_score=6,
            yesterday_score=0,
            hs300_pct_change=2.5,
            csi1000_pct_change=2.8,
            advance_decline_ratio=0.85,
        )
        assert r["triggered"] is True
        assert r["direction"] == "up"
        assert set(r["reasons"]) == {"total_surge_5", "index_rally", "breadth_confirm"}

    def test_score_jump_not_enough(self):
        r = check_emergency_hatch(
            today_score=4,
            yesterday_score=0,  # +4, 不够 5
            hs300_pct_change=2.5,
            csi1000_pct_change=2.8,
            advance_decline_ratio=0.85,
        )
        assert r["triggered"] is False

    def test_only_one_index_above_2_percent_fails(self):
        r = check_emergency_hatch(
            today_score=6,
            yesterday_score=0,
            hs300_pct_change=2.5,
            csi1000_pct_change=1.8,  # 不到 2
            advance_decline_ratio=0.85,
        )
        assert r["triggered"] is False

    def test_breadth_not_met_fails(self):
        r = check_emergency_hatch(
            today_score=6,
            yesterday_score=0,
            hs300_pct_change=2.5,
            csi1000_pct_change=2.8,
            advance_decline_ratio=0.75,
        )
        assert r["triggered"] is False

    def test_missing_data_fails(self):
        # 缺 yesterday_score → total_surge_5 不成立 → 整体不触发
        r = check_emergency_hatch(
            today_score=6,
            yesterday_score=None,
            hs300_pct_change=2.5,
            csi1000_pct_change=2.8,
            advance_decline_ratio=0.85,
        )
        assert r["triggered"] is False


class TestEmergencyHatchPriority:
    def test_down_wins_when_ambiguous(self):
        # 构造一个诡异但可能的场景: score 大跳 + 也满足上行所有条件 (实际上不可能同时)
        # 这个测试是冗余的, 因为 score delta 只能是一个值。
        # 我们只测下行存在时, 函数立即返回下行, 不再检查上行。
        r = check_emergency_hatch(
            today_score=-3,
            yesterday_score=3,
            hs300_pct_change=-4.0,
            csi1000_pct_change=-4.0,
            advance_decline_ratio=0.10,
        )
        assert r["triggered"] is True
        assert r["direction"] == "down"


# --------------------------------------------------------------------------- #
# apply_emergency_switch
# --------------------------------------------------------------------------- #


class TestEmergencySwitchApply:
    def test_down_one_tier(self):
        assert apply_emergency_switch("NEUTRAL_RANGE", "down") == "WEAK_RANGE"
        assert apply_emergency_switch("STRONG_RANGE", "down") == "NEUTRAL_RANGE"

    def test_up_one_tier(self):
        assert apply_emergency_switch("NEUTRAL_RANGE", "up") == "STRONG_RANGE"
        assert apply_emergency_switch("WEAK_RANGE", "up") == "NEUTRAL_RANGE"

    def test_down_at_bear_clamps(self):
        assert apply_emergency_switch("BEAR", "down") == "BEAR"

    def test_up_at_strong_bull_clamps(self):
        assert apply_emergency_switch("STRONG_BULL", "up") == "STRONG_BULL"

    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError):
            apply_emergency_switch("NEUTRAL_RANGE", "sideways")


# --------------------------------------------------------------------------- #
# lookup_playbook
# --------------------------------------------------------------------------- #


class TestLookupPlaybook:
    def test_all_five_regimes_have_entries(self):
        for code in REGIME_ORDER:
            pb = lookup_playbook(code)
            assert "recommended" in pb
            assert "forbidden" in pb
            assert "position_limit" in pb
            assert pb["position_limit"]["total"] > 0
            assert pb["position_limit"]["single"] > 0

    def test_strong_bull_position_limit(self):
        pb = lookup_playbook("STRONG_BULL")
        assert pb["position_limit"]["total"] == 0.90
        assert pb["position_limit"]["single"] == 0.30
        # 2026-06-02 按结构择时改版: 强牛只推 S2(验证净+4.4%); S1/S6 已移除
        ids = [r["id"] for r in pb["recommended"]]
        assert "S2" in ids
        assert "S1" not in ids and "S6" not in ids

    def test_bear_playbook(self):
        pb = lookup_playbook("BEAR")
        assert pb["position_limit"]["total"] == 0.20
        assert "S8" in [r["id"] for r in pb["recommended"]]
        assert "S1" in pb["forbidden"]

    def test_neutral_range_has_strict_mode(self):
        # 2026-06-02 按结构择时改版: 中性档 S4 已移除, 改 S7 observe; 验证 mode 字段仍透传
        pb = lookup_playbook("NEUTRAL_RANGE")
        entries_with_mode = [r for r in pb["recommended"] if "mode" in r]
        assert any(r["id"] == "S7" and r["mode"] == "observe" for r in entries_with_mode)

    def test_recommended_entries_have_name(self):
        pb = lookup_playbook("STRONG_RANGE")
        for r in pb["recommended"]:
            assert "name" in r and r["name"]
            assert "priority" in r

    def test_unknown_regime_raises(self):
        with pytest.raises(KeyError):
            lookup_playbook("UNKNOWN")

    def test_default_regime_is_neutral(self):
        assert DEFAULT_REGIME == "NEUTRAL_RANGE"
