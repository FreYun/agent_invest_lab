import json, sqlite3, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import live_common as lc


def test_live_run_id():
    assert lc.live_run_id("dash-2026-07-20T15-04-39", "bot18") == "live-bot18-20260720T150439"


def test_is_weekday():
    assert lc.is_weekday("2026-07-21") is True   # 周二
    assert lc.is_weekday("2026-07-18") is False  # 周六


def test_ensure_calendar_appends_weekday(tmp_path):
    cal = tmp_path / "calendar.json"
    cal.write_text(json.dumps({"trading_days": ["2026-07-20"]}))
    changed = lc.ensure_calendar_has(str(cal), "2026-07-21")
    assert changed is True
    days = json.loads(cal.read_text())["trading_days"]
    assert days[-1] == "2026-07-21"
    # 幂等：再调不重复追加
    assert lc.ensure_calendar_has(str(cal), "2026-07-21") is False


def test_ensure_calendar_rejects_weekend(tmp_path):
    cal = tmp_path / "calendar.json"
    cal.write_text(json.dumps({"trading_days": ["2026-07-17"]}))
    with pytest.raises(ValueError):
        lc.ensure_calendar_has(str(cal), "2026-07-18")  # 周六


def _mk_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE fund_nav (fund_code TEXT, nav_date TEXT, nav REAL)")
    conn.execute("INSERT INTO fund_nav VALUES ('000216','2026-07-21',3.5)")
    conn.commit(); conn.close()


def test_nav_ready(tmp_path):
    db = str(tmp_path / "fund.db"); _mk_db(db)
    ok, missing = lc.nav_ready(db, ["000216"], "2026-07-21")
    assert ok is True and missing == []
    ok2, missing2 = lc.nav_ready(db, ["000216", "999999"], "2026-07-21")
    assert ok2 is False and missing2 == ["999999"]
