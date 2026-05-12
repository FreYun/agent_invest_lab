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


from datetime import date as _d


class TestFilterResponse:
    def test_drops_future_list_records(self):
        obj = {"items": [
            {"交易日期": "2023-12-29", "单位净值": 1.1},
            {"交易日期": "2024-01-02", "单位净值": 1.2},
            {"交易日期": "2024-06-01", "单位净值": 1.3},
        ]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["items"] == [{"交易日期": "2023-12-29", "单位净值": 1.1}]

    def test_keeps_records_without_date_field(self):
        obj = {"items": [{"code": "x"}, {"code": "y"}]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["items"] == [{"code": "x"}, {"code": "y"}]

    def test_keeps_non_dict_list_elements(self):
        obj = {"vals": [1, 2, "2099-01-01"]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["vals"] == [1, 2, "2099-01-01"]  # bare strings in lists are not filtered

    def test_any_date_field_over_cutoff_drops_the_record(self):
        obj = {"items": [
            {"分红日期": "2023-06-01", "权益登记日": "2023-06-02", "发放日": "2023-06-10"},
            {"分红日期": "2023-12-30", "权益登记日": "2023-12-31", "发放日": "2024-01-05"},
        ]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["items"] == [
            {"分红日期": "2023-06-01", "权益登记日": "2023-06-02", "发放日": "2023-06-10"}
        ]

    def test_nulls_standalone_future_date_field(self):
        obj = {"data": {"最新净值日期": "2026-05-01", "items": [{"交易日期": "2023-01-01", "v": 1}]}}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["data"]["最新净值日期"] is None
        assert out["data"]["_pit_truncated"] is True
        assert out["data"]["items"] == [{"交易日期": "2023-01-01", "v": 1}]

    def test_keeps_standalone_past_date_field(self):
        obj = {"data": {"最新净值日期": "2023-12-29"}}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["data"]["最新净值日期"] == "2023-12-29"
        assert "_pit_truncated" not in out["data"]

    def test_ignores_blocklisted_time_fields(self):
        obj = {"metadata": {"query_time": "2026-05-11 19:00:00", "service": "x"},
               "items": [{"交易日期": "2023-01-01"}]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["metadata"]["query_time"] == "2026-05-11 19:00:00"
        assert "_pit_truncated" not in out["metadata"]

    def test_nested_lists(self):
        obj = {"items": [
            {"基金代码": "A", "记录": [{"交易日期": "2023-05-01", "v": 1},
                                      {"交易日期": "2024-05-01", "v": 2}]},
        ]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["items"][0]["记录"] == [{"交易日期": "2023-05-01", "v": 1}]

    def test_unparseable_date_value_is_left_alone(self):
        obj = {"items": [{"交易日期": "未知", "v": 1}]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["items"] == [{"交易日期": "未知", "v": 1}]
