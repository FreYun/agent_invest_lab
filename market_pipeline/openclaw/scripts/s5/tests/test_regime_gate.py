"""regime gate 单元测试 — 验证 regime 不允许时 select 仍然跑信号检测。

跑法: python3 scripts/s5/tests/test_regime_gate.py
任一 assertion 失败会抛 AssertionError, 全部通过打印 PASS。
"""

from __future__ import annotations

import importlib.util
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
S5_DIR = os.path.dirname(THIS_DIR)
ROOT_SCRIPTS = os.path.dirname(S5_DIR)
for p in (S5_DIR, ROOT_SCRIPTS, os.path.join(S5_DIR, "_lib")):
    if p not in sys.path:
        sys.path.insert(0, p)

from strategy_common import build_regime_gate


def _load_s5_select():
    # 显式加载 s5/select.py, 避开 Python stdlib select 模块的命名冲突
    spec = importlib.util.spec_from_file_location("s5_select", os.path.join(S5_DIR, "select.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["s5_select"] = module
    spec.loader.exec_module(module)
    return module


s5_select = _load_s5_select()


def test_build_regime_gate_allowed():
    gate = build_regime_gate(allowed=True, reason=None, playbook_mode="cooldown_v1")
    assert gate == {"allowed": True, "reason": None, "playbook_mode": "cooldown_v1"}, gate


def test_build_regime_gate_blocked():
    gate = build_regime_gate(
        allowed=False,
        reason="regime=BEAR, S5 not in playbook.recommended ([])",
        playbook_mode=None,
    )
    assert gate["allowed"] is False
    assert "BEAR" in gate["reason"]
    assert gate["playbook_mode"] is None


def test_run_select_emits_candidates_when_gate_closed(monkeypatch):
    """核心: regime 关门时, run_select 不应早返回, 应继续走信号检测."""

    fake_regime = {
        "date": "2026-04-30",
        "regime": "STRONG_BEAR",
        "regime_code": "STRONG_BEAR",
        "score": {"total": 10},
        "confidence": "high",
        "switched": False,
        "emergency_switch": False,
        "playbook": {
            "recommended": [],  # S5 not in there
            "position_limit": {"single": 0.05},
        },
        "source": {"kind": "test"},
    }

    detect_called = {"hit": False}
    write_called = {"hit": False}

    def fake_detect_s5(klines, streaks, t_date):
        detect_called["hit"] = True
        return {"passed": False, "stage_failed": "rebound", "reject_reason": "test stub"}

    def fake_extract_streaks(klines, t_date):
        return [{"max_streak": 2, "peak_date": "2026-04-25"}]

    def fake_get_universe(t_date):
        return (["000001"], [{"name": "Test"}], {"000001": "TestIndustry"})

    def fake_get_klines_batch(codes, start, end):
        return {"000001": [{"date": "2026-04-30", "close": 10.0}]}

    def fake_get_zt_pool(t_date):
        return None

    def fake_load_regime_from_db(t_date, rules_version="v2"):
        return fake_regime

    def fake_write_select_run(payload):
        write_called["hit"] = True
        write_called["payload"] = payload

    monkeypatch.setattr(s5_select, "load_regime_from_db", fake_load_regime_from_db)
    monkeypatch.setattr(s5_select, "get_universe", fake_get_universe)
    monkeypatch.setattr(s5_select, "get_klines_batch", fake_get_klines_batch)
    monkeypatch.setattr(s5_select, "get_zt_pool", fake_get_zt_pool)
    monkeypatch.setattr(s5_select, "extract_streaks_from_klines", fake_extract_streaks)
    monkeypatch.setattr(s5_select, "detect_s5", fake_detect_s5)

    import db_writer as s5_db_writer

    monkeypatch.setattr(s5_db_writer, "write_select_run", fake_write_select_run)
    monkeypatch.setattr(s5_select, "_write_outputs", lambda payload: fake_write_select_run(payload))

    payload = s5_select.run_select("2026-04-30")

    assert detect_called["hit"], "detect_s5 应在 regime 关门时仍被调用"
    gate = payload.get("regime_gate")
    assert gate is not None, "payload 应附带 regime_gate"
    assert gate["allowed"] is False, gate
    assert "STRONG_BEAR" in gate["reason"], gate
    # 数据通路完整, skipped_reason 不应再因 regime 被设置
    assert payload["skipped_reason"] is None, payload["skipped_reason"]


class _MonkeyPatch:
    """极简 monkeypatch (避免引入 pytest 依赖)."""

    def __init__(self):
        self._restore = []

    def setattr(self, target, name, value):
        original = getattr(target, name)
        self._restore.append((target, name, original))
        setattr(target, name, value)

    def undo(self):
        for target, name, original in reversed(self._restore):
            setattr(target, name, original)


def main():
    test_build_regime_gate_allowed()
    print("PASS test_build_regime_gate_allowed")
    test_build_regime_gate_blocked()
    print("PASS test_build_regime_gate_blocked")
    mp = _MonkeyPatch()
    try:
        test_run_select_emits_candidates_when_gate_closed(mp)
        print("PASS test_run_select_emits_candidates_when_gate_closed")
    finally:
        mp.undo()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
