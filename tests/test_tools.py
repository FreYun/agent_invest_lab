from datetime import date

import pytest

import ttjj_data_pit_mcp as m


class TestPitWrap:
    def test_filters_and_stamps(self):
        result = {"items": [{"交易日期": "2023-12-31", "v": 1}, {"交易日期": "2099-01-01", "v": 2}]}
        out = m._pit_wrap(result, date(2024, 1, 1), "2024-01-01")
        assert out["items"] == [{"交易日期": "2023-12-31", "v": 1}]
        assert out["_simulated_today"] == "2024-01-01"

    def test_skips_generic_filter_when_disabled(self):
        result = {"items": [{"交易日期": "2099-01-01", "v": 2}]}
        out = m._pit_wrap(result, date(2024, 1, 1), "2024-01-01", generic_filter=False)
        assert out["items"] == [{"交易日期": "2099-01-01", "v": 2}]
        assert out["_simulated_today"] == "2024-01-01"

    def test_stamps_error_dicts_too(self):
        out = m._pit_wrap({"error": "api_error", "message": "x"}, date(2024, 1, 1), "2024-01-01")
        assert out["error"] == "api_error"
        assert out["_simulated_today"] == "2024-01-01"


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
        assert out["_simulated_today"] == "2024-01-01"

    def test_fund_nav_defaults_end_to_simulated_today(self, monkeypatch):
        calls = self._fake_post(monkeypatch)
        m.fund_nav("2024-01-01", ["110011"])
        assert calls[0][1]["end_date"] == "2024-01-01"

    def test_bad_simulated_today(self, monkeypatch):
        self._fake_post(monkeypatch)
        assert m.fund_nav("nope", ["110011"]) == {"error": "bad_simulated_today", "message": "nope"}

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

    def test_bond_yield_curve_passes_required_params_and_clamps(self, monkeypatch):
        calls = self._fake_post(monkeypatch)
        m.bond_yield_curve("2024-01-01", "cn", ["10Y", "30Y"], end_date="2025-12-31")
        assert calls[0][0] == "/api/bond/yield-curve"
        assert calls[0][1]["curve_type"] == "cn"
        assert calls[0][1]["maturities"] == ["10Y", "30Y"]
        assert calls[0][1]["end_date"] == "2024-01-01"  # clamped

    def test_commodity_market_passes_market_type_and_clamps(self, monkeypatch):
        calls = self._fake_post(monkeypatch)
        m.commodity_market("2024-01-01", "spot", ["上海银"], end_date="2099-01-01")
        assert calls[0][0] == "/api/commodity/market"
        assert calls[0][1]["market_type"] == "spot"
        assert calls[0][1]["symbols"] == ["上海银"]
        assert calls[0][1]["end_date"] == "2024-01-01"  # clamped

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


class TestBespokeTools:
    def test_fund_basic_info_drops_funds_established_after_cutoff_and_masks(self, monkeypatch):
        def fake(path, data):
            assert path == "/api/fund/basic-info"
            return {"success": True, "items": [
                {"基金代码": "110011", "基金公司": "易方达", "基金经理": "张三",
                 "成立时间": "2008-09-19", "最新定期报告时间": "2026-03-31"},
                {"基金代码": "999999", "基金公司": "新基金", "基金经理": "李四",
                 "成立时间": "2025-06-01", "最新定期报告时间": "2026-03-31"},
            ]}
        monkeypatch.setattr(m, "_post", fake)
        out = m.fund_basic_info("2024-01-01", ["110011", "999999"])
        assert len(out["items"]) == 1
        rec = out["items"][0]
        assert rec["基金代码"] == "110011"
        assert rec["基金经理"] is None
        assert rec["最新定期报告时间"] is None
        assert "_pit_note" in rec
        assert out["_simulated_today"] == "2024-01-01"

    def test_stock_profile_drops_stocks_listed_after_cutoff(self, monkeypatch):
        def fake(path, data):
            assert path == "/api/stock/profile"
            return {"success": True, "items": [
                {"股票代码": "600519", "上市日期": "2001-08-27"},
                {"股票代码": "688981", "上市日期": "2025-01-10"},
            ]}
        monkeypatch.setattr(m, "_post", fake)
        out = m.stock_profile("2024-01-01", ["600519", "688981"])
        assert [r["股票代码"] for r in out["items"]] == ["600519"]

    def test_bespoke_bad_simulated_today(self, monkeypatch):
        monkeypatch.setattr(m, "_post", lambda *a, **k: {"success": True, "items": []})
        assert m.fund_basic_info("xx", ["1"]) == {"error": "bad_simulated_today", "message": "xx"}
        assert m.stock_profile("xx", ["1"]) == {"error": "bad_simulated_today", "message": "xx"}


class TestSpecialTools:
    def test_research_search_filters_future_articles(self, monkeypatch):
        def fake(path, data):
            assert path == "/api/research/search"
            assert data["search_type"] == "news"
            return {"success": True, "results": [
                {"标题": "旧闻", "发布时间": "2023-12-20 10:00:00"},
                {"标题": "未来新闻", "发布时间": "2026-05-01 10:00:00"},
            ]}
        monkeypatch.setattr(m, "_post", fake)
        out = m.ttjj_research_search("2024-01-01", "贵州茅台")
        assert [r["标题"] for r in out["results"]] == ["旧闻"]
        assert out["_simulated_today"] == "2024-01-01"
        assert "_pit_note" in out

    def test_research_search_bad_simulated_today(self, monkeypatch):
        monkeypatch.setattr(m, "_post", lambda *a, **k: {"success": True})
        assert m.ttjj_research_search("xx", "q") == {"error": "bad_simulated_today", "message": "xx"}

    def test_realtime_quote_filters_future_timestamps(self, monkeypatch):
        def fake(path, data):
            assert path == "/api/market/realtime-quote"
            return {"success": True, "items": [
                {"代码": "600519", "时间": "2023-12-29 15:00:00", "最新价": 1700.0},
                {"代码": "000300", "时间": "2026-05-11 15:00:00", "最新价": 3500.0},
            ]}
        monkeypatch.setattr(m, "_post", fake)
        out = m.market_realtime_quote("2024-01-01", ["600519", "000300"])
        assert [r["代码"] for r in out["items"]] == ["600519"]
        assert out["_simulated_today"] == "2024-01-01"

    def test_realtime_quote_bad_simulated_today(self, monkeypatch):
        monkeypatch.setattr(m, "_post", lambda *a, **k: {"success": True})
        assert m.market_realtime_quote("xx", ["1"]) == {"error": "bad_simulated_today", "message": "xx"}

    def test_special_tools_registered(self):
        assert callable(m.ttjj_research_search)
        assert callable(m.market_realtime_quote)


def test_dropped_tools_are_absent():
    dropped = [
        "fund_select", "fund_performance", "fund_manager_profile", "fund_style_analysis",
        "fund_rate", "fund_theme_screening", "fund_stock_holdings_screen", "fund_index_tracking",
        "fund_top_holdings", "fund_invest_position", "fund_turnover_rate", "fund_industry_exposure",
        "entity_extract", "health_check",
    ]
    for name in dropped:
        assert not hasattr(m, name), name


def test_main_callable():
    assert callable(m.main)
