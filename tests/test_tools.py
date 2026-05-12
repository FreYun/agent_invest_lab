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
