from datetime import date

import pytest

import ttjj_data_pit_mcp as m


class TestPitWrap:
    def test_filters_and_stamps(self):
        result = {"items": [{"交易日期": "2023-12-31", "v": 1}, {"交易日期": "2099-01-01", "v": 2}]}
        out = m._pit_wrap(result, date(2024, 1, 1), "2024-01-01")
        assert out["items"] == [{"交易日期": "2023-12-31", "v": 1}]
        assert out["_as_of_date"] == "2024-01-01"

    def test_skips_generic_filter_when_disabled(self):
        result = {"items": [{"交易日期": "2099-01-01", "v": 2}]}
        out = m._pit_wrap(result, date(2024, 1, 1), "2024-01-01", generic_filter=False)
        assert out["items"] == [{"交易日期": "2099-01-01", "v": 2}]
        assert out["_as_of_date"] == "2024-01-01"

    def test_stamps_error_dicts_too(self):
        out = m._pit_wrap({"error": "api_error", "message": "x"}, date(2024, 1, 1), "2024-01-01")
        assert out["error"] == "api_error"
        assert out["_as_of_date"] == "2024-01-01"


def test_post_uses_base_url_and_unwraps(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"success": True, "items": [1, 2]}

    class FakeSession:
        headers = {}
        def post(self, url, data=None, timeout=None):
            captured["url"] = url
            captured["body"] = data
            return FakeResp()

    monkeypatch.setattr(m, "_session", FakeSession())
    out = m._post("/api/fund/nav", {"fund_codes": ["110011"]})
    assert captured["url"] == "http://ttjj-data-api.jijinmima.cn/api/fund/nav"
    assert out == {"success": True, "items": [1, 2]}


def test_post_api_error(monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"success": False, "message": "boom"}

    class FakeSession:
        headers = {}
        def post(self, *a, **k): return FakeResp()

    monkeypatch.setattr(m, "_session", FakeSession())
    assert m._post("/x", {}) == {"error": "api_error", "message": "boom"}


class TestDatedTools:
    def _fake_post(self, monkeypatch):
        calls = []
        def fake(path, data):
            calls.append((path, dict(data)))
            return {"success": True, "items": [
                {"交易日期": "2023-12-31", "v": 1},
                {"交易日期": "2099-01-01", "v": 2},
            ]}
        monkeypatch.setattr(m, "_post", fake)
        return calls

    def test_fund_nav_clamps_end_and_filters(self, monkeypatch):
        calls = self._fake_post(monkeypatch)
        out = m.fund_nav("2024-01-01", ["110011"], start_date="2023-01-01", end_date="2025-09-09")
        assert calls[0][0] == "/api/fund/nav"
        assert calls[0][1]["end_date"] == "2024-01-01"          # clamped
        assert calls[0][1]["start_date"] == "2023-01-01"        # kept
        assert out["items"] == [{"交易日期": "2023-12-31", "v": 1}]   # future record dropped
        assert out["_as_of_date"] == "2024-01-01"

    def test_fund_nav_defaults_end_to_as_of(self, monkeypatch):
        calls = self._fake_post(monkeypatch)
        m.fund_nav("2024-01-01", ["110011"])
        assert calls[0][1]["end_date"] == "2024-01-01"

    def test_bad_as_of(self, monkeypatch):
        self._fake_post(monkeypatch)
        assert m.fund_nav("nope", ["110011"]) == {"error": "bad_as_of_date", "message": "nope"}

    def test_future_start_date_rejected(self, monkeypatch):
        self._fake_post(monkeypatch)
        out = m.fund_nav("2024-01-01", ["110011"], start_date="2025-01-01")
        assert out["error"] == "lookahead"

    def test_stock_ownership_future_report_date_rejected(self, monkeypatch):
        self._fake_post(monkeypatch)
        out = m.stock_ownership("2024-01-01", ["600519"], report_date="2025-12-31")
        assert out["error"] == "lookahead"

    def test_stock_ownership_defaults_report_date(self, monkeypatch):
        calls = self._fake_post(monkeypatch)
        m.stock_ownership("2024-01-01", ["600519"])
        assert calls[0][1]["report_date"] == "2024-01-01"

    def test_bond_yield_curve_defaults_date(self, monkeypatch):
        calls = self._fake_post(monkeypatch)
        m.bond_yield_curve("2024-01-01")
        assert calls[0][1] == {"date": "2024-01-01"}

    def test_macro_data_passthrough(self, monkeypatch):
        calls = self._fake_post(monkeypatch)
        m.macro_data("2024-01-01", "cn", categories=["cpi"], start_date="2020-01-01")
        assert calls[0][1]["region"] == "cn"
        assert calls[0][1]["categories"] == ["cpi"]
        assert calls[0][1]["end_date"] == "2024-01-01"

    def test_all_15_registered(self):
        names = {
            "fund_nav", "fund_index_return", "fund_bonus", "fund_abnormal_movement",
            "market_index_quote", "stock_market", "stock_capital_flow", "stock_ownership",
            "stock_financial_quality", "stock_alpha", "stock_events", "macro_data",
            "bond_yield_curve", "commodity_market", "research_view",
        }
        for n in names:
            assert callable(getattr(m, n)), n
