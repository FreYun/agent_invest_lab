"""daily_pick_writer.attach_industry_view 单测."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def dbs(tmp_path, monkeypatch):
    mkt = str(tmp_path / "market.db"); lab = str(tmp_path / "lab.db")
    # market: stock_concept_map + stock_names
    c = sqlite3.connect(mkt)
    c.execute("CREATE TABLE stock_concept_map (snapshot_date TEXT, board_code TEXT, "
              "board_name TEXT, ts_code TEXT, name TEXT)")
    c.execute("INSERT INTO stock_concept_map(snapshot_date,board_code,ts_code) "
              "VALUES ('20260624','BK0479.DC','600019.SH')")
    c.execute("CREATE TABLE stock_names (code TEXT, ts_code TEXT, name TEXT, updated_at TEXT)")
    c.execute("INSERT INTO stock_names(code,name) VALUES ('600019','宝钢股份')")
    c.commit(); c.close()
    # lab: industry + research_note + industry_board_map
    c = sqlite3.connect(lab)
    c.execute("CREATE TABLE industry (id INTEGER PRIMARY KEY, name TEXT)")
    c.execute("INSERT INTO industry VALUES (5,'钢铁')")
    c.execute("CREATE TABLE research_note (id INTEGER PRIMARY KEY, industry_id INTEGER, "
              "bot_id TEXT, rating TEXT, valid_until TEXT, qc_status TEXT, conclusion TEXT)")
    c.execute("INSERT INTO research_note VALUES (72,5,'res11','看空(置信度中)',"
              "'2026-07-02 00:00:00','pass','周期下行吨钢利润压缩')")
    c.execute("CREATE TABLE industry_board_map (industry_id INTEGER, board_code TEXT, "
              "board_name TEXT, source TEXT, weight REAL)")
    c.execute("INSERT INTO industry_board_map VALUES (5,'BK0479.DC','钢铁','manual',1.0)")
    c.commit(); c.close()
    monkeypatch.setenv("SCOUT_DB_PATH", mkt)
    monkeypatch.setenv("SCOUT_RESEARCH_DB_PATH", lab)
    return mkt, lab


def test_attach_adds_industry_view(dbs):
    import daily_pick_writer as w
    mkt, _ = dbs
    items = [{"code": "600019", "deep_research_json": {"scout": {}, "verdict": {}}}]
    w.attach_industry_view(items)
    iv = items[0]["deep_research_json"]["industry_view"]
    assert iv["industry"] == "钢铁" and iv["stance"] == "看空"
    assert iv["tier_delta"] == -1 and iv["risk_flag"] is True


def test_attach_idempotent_when_present(dbs):
    import daily_pick_writer as w
    mkt, _ = dbs
    items = [{"code": "600019",
              "deep_research_json": {"industry_view": {"industry": "手填别覆盖"}}}]
    w.attach_industry_view(items)
    assert items[0]["deep_research_json"]["industry_view"]["industry"] == "手填别覆盖"


def test_attach_failsoft_no_throw(dbs, tmp_path, monkeypatch):
    import daily_pick_writer as w
    mkt, _ = dbs
    monkeypatch.setenv("SCOUT_RESEARCH_DB_PATH", str(tmp_path / "nope.db"))
    items = [{"code": "600019", "deep_research_json": {"scout": {}}}]
    w.attach_industry_view(items)  # 不抛错
    iv = items[0]["deep_research_json"]["industry_view"]
    assert iv["covered"] is False
