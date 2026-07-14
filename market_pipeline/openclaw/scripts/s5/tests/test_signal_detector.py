"""signal_detector 单元测试 — 用 mock K 线验证三段判定。

跑法: python3 scripts/tests/test_signal_detector.py
任一 assertion 失败会抛 AssertionError, 全部通过打印 PASS。
"""

from __future__ import annotations

import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(THIS_DIR))

from signal_detector import detect_dragon, detect_cooldown, detect_rebound, detect_s5


def make_bar(date, open_, high, low, close, pct_chg=None, streak=0):
    if pct_chg is None:
        pct_chg = 0.0
    return {
        "date": date,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "pct_chg": pct_chg,
        "volume": 1000000,
        "amount": 1000000,
        "is_limit_up": streak > 0,
        "is_limit_down": False,
        "limit_up_streak": streak,
        "limit_down_streak": 0,
    }


def test_dragon_pass():
    """近 30 日有 3 板 → dragon 通过, peak 选最高连板那天"""
    klines = [
        make_bar("2026-03-20", 10, 11, 10, 10.5, 5),
        make_bar("2026-03-21", 10.5, 11.5, 10.4, 11.55, 10, streak=1),
        make_bar("2026-03-24", 11.5, 12.7, 11.5, 12.7, 10, streak=2),
        make_bar("2026-03-25", 12.7, 14.0, 12.7, 14.0, 10, streak=3),
        make_bar("2026-03-26", 14.0, 14.0, 13.0, 13.5, -3),
    ]
    streaks = [
        {"date": "2026-03-21", "max_streak": 1, "close": 11.55},
        {"date": "2026-03-25", "max_streak": 3, "close": 14.0},
    ]
    r = detect_dragon(klines, streaks, "2026-04-08")
    assert r["passed"], r
    assert r["dragon_peak"]["date"] == "2026-03-25", r
    assert r["dragon_peak"]["max_streak"] == 3, r
    print("  ✓ test_dragon_pass")


def test_dragon_reject_no_streak():
    """无任何连板 → dragon 拒"""
    streaks = []
    r = detect_dragon([], streaks, "2026-04-08")
    assert not r["passed"]
    assert "无连板" in r["reject_reason"]
    print("  ✓ test_dragon_reject_no_streak")


def test_dragon_reject_only_1_board():
    """只有 1 板 → 拒 (要求 ≥2)"""
    streaks = [{"date": "2026-03-25", "max_streak": 1, "close": 10}]
    r = detect_dragon([], streaks, "2026-04-08")
    assert not r["passed"]
    assert "未达 2 板门槛" in r["reject_reason"]
    print("  ✓ test_dragon_reject_only_1_board")


def test_cooldown_pass():
    """龙头日 → T 日间隔 5 天, 阶段跌幅 -8% → 通过"""
    klines = [
        make_bar("2026-04-01", 10, 11, 10, 11.0, 10, streak=1),  # 龙头日 close=11
        make_bar("2026-04-02", 11, 11, 10.5, 10.7, -2.7),
        make_bar("2026-04-03", 10.7, 10.8, 10.3, 10.5, -1.9),
        make_bar("2026-04-07", 10.5, 10.5, 10.0, 10.2, -2.9),
        make_bar("2026-04-08", 10.2, 10.3, 10.0, 10.12, -0.78),  # T-1 close
        make_bar("2026-04-09", 10.1, 11.13, 10.1, 11.13, 10.0),  # T 日
    ]
    peak = {"date": "2026-04-01", "close": 11.0, "max_streak": 1}
    r = detect_cooldown(klines, peak, "2026-04-09")
    assert r["passed"], r
    assert r["cooldown"]["days"] == 4, r
    assert r["cooldown"]["drop_pct"] < -5, r
    print("  ✓ test_cooldown_pass")


def test_cooldown_reject_too_short():
    """龙头日 → T 日间隔 1 天 (太接近) → 拒"""
    klines = [
        make_bar("2026-04-01", 10, 11, 10, 11.0, 10, streak=1),  # 龙头
        make_bar("2026-04-02", 11, 11, 10.5, 10.5, -4.5),  # T-1
        make_bar("2026-04-03", 10.5, 11.5, 10.5, 11.55, 10),  # T
    ]
    peak = {"date": "2026-04-01", "close": 11.0, "max_streak": 1}
    r = detect_cooldown(klines, peak, "2026-04-03")
    assert not r["passed"]
    assert "少于" in r["reject_reason"], r
    print("  ✓ test_cooldown_reject_too_short")


def test_cooldown_reject_drop_insufficient():
    """间隔 5 天但只跌 2% → 拒"""
    klines = [
        make_bar("2026-04-01", 10, 11, 10, 11.0, 10, streak=1),
        make_bar("2026-04-02", 11, 11, 10.9, 10.95, -0.45),
        make_bar("2026-04-03", 11, 11, 10.9, 10.92, -0.27),
        make_bar("2026-04-07", 11, 11, 10.85, 10.85, -0.64),
        make_bar("2026-04-08", 11, 11, 10.8, 10.8, -0.46),  # T-1, 阶段跌幅仅 -1.8%
        make_bar("2026-04-09", 10.8, 12, 10.8, 11.88, 10),  # T
    ]
    peak = {"date": "2026-04-01", "close": 11.0, "max_streak": 1}
    r = detect_cooldown(klines, peak, "2026-04-09")
    assert not r["passed"]
    assert "回调不充分" in r["reject_reason"], r
    print("  ✓ test_cooldown_reject_drop_insufficient")


def test_rebound_pass():
    """T 日涨 8% 且收盘 ≥ T-1 最高 → 通过"""
    klines = [
        make_bar("2026-04-08", 10, 11, 9.5, 10.0, -3),  # T-1, high=11
        make_bar("2026-04-09", 10.1, 11.5, 10.0, 11.2, 12),  # T, close=11.2 > T-1 high=11
    ]
    r = detect_rebound(klines, "2026-04-09")
    assert r["passed"], r
    print("  ✓ test_rebound_pass")


def test_rebound_reject_pct_low():
    """T 日只涨 4% → 拒"""
    klines = [
        make_bar("2026-04-08", 10, 11, 9.5, 10.0, -3),
        make_bar("2026-04-09", 10.1, 10.6, 10.0, 10.4, 4),
    ]
    r = detect_rebound(klines, "2026-04-09")
    assert not r["passed"]
    assert "反包力度不够" in r["reject_reason"], r
    print("  ✓ test_rebound_reject_pct_low")


def test_rebound_reject_no_cover():
    """T 日涨 8% 但收盘 < T-1 最高 → 拒"""
    klines = [
        make_bar("2026-04-08", 10, 11.5, 9.5, 10.0, -3),  # T-1 high = 11.5
        make_bar("2026-04-09", 10.1, 11.0, 10.0, 10.8, 8),  # T close=10.8 < T-1 high
    ]
    r = detect_rebound(klines, "2026-04-09")
    assert not r["passed"]
    assert "未收复" in r["reject_reason"], r
    print("  ✓ test_rebound_reject_no_cover")


def test_detect_s5_full_pass():
    """三段全过 → detect_s5 通过"""
    klines = [
        make_bar("2026-04-01", 10, 11, 10, 11.0, 10, streak=1),
        make_bar("2026-04-02", 11, 12.1, 11, 12.1, 10, streak=2),  # 龙头日 2 板 close=12.1
        make_bar("2026-04-03", 12.1, 12.1, 11.5, 11.6, -4.13),
        make_bar("2026-04-07", 11.6, 11.7, 11.0, 11.2, -3.45),
        make_bar("2026-04-08", 11.2, 11.3, 10.5, 10.7, -4.46),  # T-1 阶段跌幅 -11.6% high=11.3
        make_bar("2026-04-09", 10.8, 11.8, 10.7, 11.55, 7.94),  # T 涨 7.94%, close > T-1 high 11.3
    ]
    streaks = [{"date": "2026-04-02", "max_streak": 2, "close": 12.1}]
    r = detect_s5(klines, streaks, "2026-04-09")
    assert r["passed"], r
    assert r["dragon_peak"]["date"] == "2026-04-02"
    # 04-02 (龙头) → 04-03/04-07/04-08 (冷却) → 04-09 (T) = 3 个交易日冷却
    assert r["cooldown"]["days"] == 3, r
    assert r["rebound"]["t_pct"] >= 7
    print("  ✓ test_detect_s5_full_pass")


def test_detect_s5_short_circuit():
    """龙头都没有 → 立刻在 dragon 阶段拒, 不再算后续"""
    klines = [make_bar("2026-04-09", 10, 11, 10, 11, 10)]
    streaks = []
    r = detect_s5(klines, streaks, "2026-04-09")
    assert not r["passed"]
    assert r["stage_failed"] == "dragon"
    assert r["cooldown"] is None
    assert r["rebound"] is None
    print("  ✓ test_detect_s5_short_circuit")


if __name__ == "__main__":
    print("Running signal_detector tests...")
    test_dragon_pass()
    test_dragon_reject_no_streak()
    test_dragon_reject_only_1_board()
    test_cooldown_pass()
    test_cooldown_reject_too_short()
    test_cooldown_reject_drop_insufficient()
    test_rebound_pass()
    test_rebound_reject_pct_low()
    test_rebound_reject_no_cover()
    test_detect_s5_full_pass()
    test_detect_s5_short_circuit()
    print("\n✅ All 11 tests PASS")
