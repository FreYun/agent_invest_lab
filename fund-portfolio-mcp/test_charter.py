"""Allocation Charter（配置宪章）机制测试。

覆盖：表结构、declare/amend 工具、卫星复评工具、买单结构闸门矩阵。
fixture 模式与 test_run_id_isolation.py 一致：tmp_db + reload_server。
"""
import asyncio
import importlib
import json
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


@pytest.fixture
def reload_server(tmp_db):
    import server
    importlib.reload(server)
    return server


def test_charter_tables_exist(tmp_db):
    conn = sqlite3.connect(tmp_db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "fund_bot_charters" in names
    assert "fund_bot_satellite_reviews" in names
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fund_bot_charters)")}
    assert {"bot_id", "run_id", "declared_date", "core_fund_codes",
            "single_fund_max_ratio", "satellite_min_ratio",
            "min_equity_threshold", "satellite_review_cadence_days",
            "status", "reason"} <= cols
    conn.close()
