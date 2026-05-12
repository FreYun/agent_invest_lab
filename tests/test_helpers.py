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
