from datetime import date, datetime

import ttjj_data_pit_mcp as m


class TestParseDate:
    def test_iso(self):
        assert m._parse_date("2024-01-01") == date(2024, 1, 1)

    def test_compact(self):
        assert m._parse_date("20240131") == date(2024, 1, 31)

    def test_slash(self):
        assert m._parse_date("2024/02/05") == date(2024, 2, 5)

    def test_datetime_string(self):
        assert m._parse_date("2024-03-04 09:30:00") == date(2024, 3, 4)

    def test_single_digit_parts(self):
        assert m._parse_date("2024-3-5") == date(2024, 3, 5)

    def test_year_month(self):
        assert m._parse_date("2024-01") == date(2024, 1, 1)

    def test_year_only(self):
        assert m._parse_date("2024") == date(2024, 1, 1)

    def test_date_object_passthrough(self):
        assert m._parse_date(date(2024, 5, 6)) == date(2024, 5, 6)

    def test_datetime_object(self):
        assert m._parse_date(datetime(2024, 5, 6, 12, 0)) == date(2024, 5, 6)

    def test_not_a_date(self):
        assert m._parse_date("hello") is None
        assert m._parse_date("12345") is None
        assert m._parse_date("") is None
        assert m._parse_date(None) is None
        assert m._parse_date(123) is None
        assert m._parse_date(12.5) is None

    def test_invalid_calendar_date(self):
        assert m._parse_date("2024-13-40") is None


class TestIsDateField:
    def test_chinese_date_names(self):
        for k in ["交易日期", "净值日期", "报告日期", "报告期", "分红日期",
                  "权益登记日", "发放日", "变动日期", "停牌日期", "复牌日期",
                  "成立时间", "上市日期", "时间", "更新时间"]:
            assert m._is_date_field(k), k

    def test_english_date_names(self):
        for k in ["date", "trade_date", "endDate", "datetime", "publish_time", "timestamp"]:
            assert m._is_date_field(k), k

    def test_blocklisted(self):
        for k in ["query_time", "update_time", "updated_at", "create_time",
                  "created_at", "_as_of_date", "_pit_note", "_pit_truncated"]:
            assert not m._is_date_field(k), k

    def test_non_date_names(self):
        for k in ["基金代码", "基金名称", "单位净值", "code", "name", "占净值比例"]:
            assert not m._is_date_field(k), k

    def test_权益登记日_matches_via_日(self):
        # NOTE: "日" alone is NOT in the regex; "权益登记日" must match — verify the
        # implementation includes "登记日"/"发放日" style names. If your regex is
        # `日期|时间|date|time`, "权益登记日" does NOT contain any of those substrings,
        # so this name needs explicit handling. See implementation below.
        assert m._is_date_field("权益登记日")
        assert m._is_date_field("发放日")
