"""Tests for capability_circle / paradigm_runs tables and MCP tools."""
import os
import sqlite3
import tempfile
import pytest


@pytest.fixture
def tmp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr("db.DB_PATH", path)
    import db as db_mod
    db_mod.init_db()
    yield path
    os.unlink(path)


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _column_names(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_init_db_creates_capability_circle_table(tmp_db):
    conn = sqlite3.connect(tmp_db)
    assert _table_exists(conn, "fund_capability_circle")
    cols = _column_names(conn, "fund_capability_circle")
    expected = {
        "circle_id", "bot_id", "as_of_date", "macro", "industry_rotation",
        "industry_focus", "fund_alpha", "default_paradigm",
        "secondary_paradigm", "switch_rules_json", "evidence_md",
        "next_assessment_due", "created_at",
    }
    assert expected.issubset(set(cols)), f"missing: {expected - set(cols)}"
    conn.close()


def test_init_db_creates_paradigm_runs_table(tmp_db):
    conn = sqlite3.connect(tmp_db)
    assert _table_exists(conn, "fund_paradigm_runs")
    cols = _column_names(conn, "fund_paradigm_runs")
    expected = {
        "paradigm_run_id", "bot_id", "trade_date", "run_id",
        "paradigm_active", "capability_field", "capability_value",
        "switched_from", "reason", "created_at",
    }
    assert expected.issubset(set(cols))
    conn.close()


def test_init_db_adds_paradigm_column_to_existing_tables(tmp_db):
    conn = sqlite3.connect(tmp_db)
    for table in ("fund_bot_reviews", "fund_bot_actions", "fund_allocation_runs"):
        cols = _column_names(conn, table)
        assert "paradigm" in cols, f"{table} missing paradigm column"
    conn.close()


def test_migration_idempotent_on_existing_db(tmp_db):
    """Re-running init_db on a populated DB must not fail or duplicate columns."""
    import db as db_mod
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO fund_bot_reviews (bot_id, review_date, regime, decision) "
        "VALUES (?, ?, ?, ?)", ("bot7", "2026-04-29", "range", "KEEP")
    )
    conn.commit()
    conn.close()
    db_mod.init_db()
    conn = sqlite3.connect(tmp_db)
    cnt = conn.execute("SELECT COUNT(*) FROM fund_bot_reviews").fetchone()[0]
    assert cnt == 1
    conn.close()


import asyncio
import importlib


@pytest.fixture
def reload_server(tmp_db):
    """server.py 在 import 时绑定 db_path,需要在 monkeypatch 后 reload。"""
    import server
    importlib.reload(server)
    return server


def test_save_and_get_capability_circle(reload_server):
    s = reload_server
    payload = {
        "bot_id": "bot7",
        "as_of_date": "2026-04-29",
        "macro": False,
        "industry_rotation": False,
        "industry_focus": "科技",
        "fund_alpha": True,
        "default_paradigm": "B2",
        "secondary_paradigm": "C",
        "switch_rules_json": '[{"trigger":"科技拥挤>95%","from":"B2","to":"C"}]',
        "evidence_md": "## B2 · 科技\n- 跟踪 8 个月\n",
        "next_assessment_due": "2026-07-29",
    }
    result = asyncio.run(s.save_capability_circle(**payload))
    assert '"success": true' in result
    got = asyncio.run(s.get_capability_circle(bot_id="bot7"))
    assert "industry_focus" in got and "科技" in got
    assert "B2" in got


def test_get_capability_circle_returns_none_when_missing(reload_server):
    s = reload_server
    out = asyncio.run(s.get_capability_circle(bot_id="bot999"))
    assert '"found": false' in out


def test_save_capability_circle_rejects_b1_b2_conflict(reload_server):
    s = reload_server
    out = asyncio.run(s.save_capability_circle(
        bot_id="bot7", as_of_date="2026-04-29",
        macro=False, industry_rotation=True, industry_focus="科技",
        fund_alpha=False,
        default_paradigm="B1",
    ))
    assert '"success": false' in out
    assert "B1" in out and "B2" in out


def test_save_and_get_paradigm_run(reload_server):
    s = reload_server
    out = asyncio.run(s.save_paradigm_run(
        bot_id="bot7", trade_date="2026-04-29",
        run_id="cron-2026-04-29",
        paradigm_active="B2",
        capability_field="industry_focus",
        capability_value="科技",
        reason="single_capability_auto",
    ))
    assert '"success": true' in out
    got = asyncio.run(s.get_latest_paradigm(bot_id="bot7"))
    assert '"paradigm_active": "B2"' in got
    assert "科技" in got


def test_save_paradigm_run_idempotent_same_day(reload_server):
    """Same (bot_id, trade_date) 写第二次应该 UPDATE 而不是 INSERT。"""
    s = reload_server
    asyncio.run(s.save_paradigm_run(
        bot_id="bot7", trade_date="2026-04-29",
        paradigm_active="B2", capability_field="industry_focus",
        capability_value="科技", reason="initial",
    ))
    asyncio.run(s.save_paradigm_run(
        bot_id="bot7", trade_date="2026-04-29",
        paradigm_active="C", capability_field="fund_alpha",
        capability_value="true", reason="switched", switched_from="B2",
    ))
    got = asyncio.run(s.get_latest_paradigm(bot_id="bot7"))
    assert '"paradigm_active": "C"' in got
    assert '"switched_from": "B2"' in got


def test_get_latest_paradigm_when_none(reload_server):
    s = reload_server
    out = asyncio.run(s.get_latest_paradigm(bot_id="bot999"))
    assert '"found": false' in out


def test_save_paradigm_run_with_skip(reload_server):
    s = reload_server
    out = asyncio.run(s.save_paradigm_run(
        bot_id="bot999", trade_date="2026-04-29",
        paradigm_active="SKIP", reason="no_capability_skip",
    ))
    assert '"success": true' in out
    got = asyncio.run(s.get_latest_paradigm(bot_id="bot999"))
    assert '"paradigm_active": "SKIP"' in got


def test_save_paradigm_run_rejects_invalid_paradigm(reload_server):
    s = reload_server
    out = asyncio.run(s.save_paradigm_run(
        bot_id="bot7", trade_date="2026-04-29",
        paradigm_active="X",  # invalid
    ))
    assert '"success": false' in out
