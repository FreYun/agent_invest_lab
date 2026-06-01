import os
from datetime import date

import pytest

import ttjj_data_pit_mcp as m

pytestmark = pytest.mark.skipif(
    os.environ.get("TTJJ_PIT_LIVE") != "1",
    reason="set TTJJ_PIT_LIVE=1 to run live upstream smoke tests",
)


def _all_dates_le(obj, cutoff):
    """Assert no recognized date-field value anywhere in obj is > cutoff."""
    if isinstance(obj, list):
        for el in obj:
            _all_dates_le(el, cutoff)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if m._is_date_field(k):
                d = m._parse_date(v)
                assert d is None or d <= cutoff, f"{k}={v} > {cutoff}"
            else:
                _all_dates_le(v, cutoff)


def test_fund_nav_live_respects_simulated_today():
    cutoff = date(2024, 1, 1)
    out = m.fund_nav("2024-01-01", ["110011"])
    assert out.get("_simulated_today") == "2024-01-01"
    assert "error" not in out, out
    _all_dates_le(out, cutoff)


def test_fund_nav_live_clamps_future_end_date():
    out = m.fund_nav("2024-01-01", ["110011"], end_date="2025-12-31")
    assert "error" not in out, out
    _all_dates_le(out, date(2024, 1, 1))


def test_stock_ownership_live_rejects_future_report_date():
    out = m.stock_ownership("2024-01-01", ["600519"], report_date="2025-12-31")
    assert out["error"] == "lookahead"
