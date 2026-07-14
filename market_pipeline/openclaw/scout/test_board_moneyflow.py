"""board_moneyflow 单测: schema/行业聚合/全市场总量/5日窗口/top成分/缺数据. 全程临时库."""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scout_db  # noqa: E402


def test_init_schema_creates_moneyflow_tables(tmp_path):
    db = str(tmp_path / "t.db")
    scout_db.init_schema(db)
    c = sqlite3.connect(db)
    tabs = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    assert "board_moneyflow_daily" in tabs
    assert "market_moneyflow_daily" in tabs


def _mkdb(path):
    """构造小样本. latest=20260103, 共3个交易日(0101,0102,0103).
    行业: 半导体(A,B) / 银行(C). A每日净流入, C每日净流出."""
    c = sqlite3.connect(path)
    scout_db.init_schema(path)  # 建全部表(含两新表)
    c.execute("CREATE TABLE IF NOT EXISTS moneyflow_daily ("
              "trade_date TEXT, ts_code TEXT, net_main REAL, net_mf_amount REAL,"
              "PRIMARY KEY(trade_date, ts_code))")
    c.execute("CREATE TABLE IF NOT EXISTS sw_industry_member ("
              "ts_code TEXT PRIMARY KEY, l1_code TEXT, l1_name TEXT,"
              "l2_code TEXT, l2_name TEXT, updated_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS stock_names ("
              "code TEXT, ts_code TEXT, name TEXT, updated_at TEXT)")
    # 行业映射
    c.executemany("INSERT INTO sw_industry_member(ts_code,l1_code,l1_name) VALUES (?,?,?)", [
        ("000001.SZ", "S1", "半导体"), ("000002.SZ", "S1", "半导体"),
        ("600000.SH", "S2", "银行")])
    c.executemany("INSERT INTO stock_names(ts_code,name) VALUES (?,?)", [
        ("000001.SZ", "甲股"), ("000002.SZ", "乙股"), ("600000.SH", "丙银行")])
    # net_main(万元): 三日. 半导体A=+100/+100/+100, B=+10/+10/+50; 银行C=-30/-30/-30
    rows = [
        ("20260101", "000001.SZ", 100.0), ("20260101", "000002.SZ", 10.0), ("20260101", "600000.SH", -30.0),
        ("20260102", "000001.SZ", 100.0), ("20260102", "000002.SZ", 10.0), ("20260102", "600000.SH", -30.0),
        ("20260103", "000001.SZ", 100.0), ("20260103", "000002.SZ", 50.0), ("20260103", "600000.SH", -30.0),
    ]
    c.executemany("INSERT INTO moneyflow_daily(trade_date,ts_code,net_main) VALUES (?,?,?)", rows)
    c.commit()
    c.close()


def test_aggregate_boards_sums_by_industry(tmp_path):
    db = str(tmp_path / "t.db")
    _mkdb(db)
    import board_moneyflow as bm
    rows = {r["sw_l1_name"]: r for r in bm.aggregate_boards(db, "20260103")}
    # 半导体当日 = 100 + 50 = 150; 5日(0101+0102+0103) = (100+10)+(100+10)+(100+50)=370
    assert rows["半导体"]["net_1d"] == 150.0
    assert rows["半导体"]["net_5d"] == 370.0
    assert rows["半导体"]["member_n"] == 2
    # 银行当日 = -30; 5日 = -90
    assert rows["银行"]["net_1d"] == -30.0
    assert rows["银行"]["net_5d"] == -90.0
    # top_in 第一名是甲股(+100), top_out 第一名是乙股(+50<甲) — 半导体内 net 升序最小是乙股50
    assert rows["半导体"]["top_in"][0]["name"] == "甲股"
    assert rows["半导体"]["top_in"][0]["net"] == 100.0


def test_aggregate_market_total_dedup(tmp_path):
    db = str(tmp_path / "t.db")
    _mkdb(db)
    import board_moneyflow as bm
    boards = bm.aggregate_boards(db, "20260103")
    mkt = bm.aggregate_market(db, "20260103", boards)
    # 全市场当日 = 100+50-30 = 120; 5日 = 370 + (-90) = 280
    assert mkt["net_1d"] == 120.0
    assert mkt["net_5d"] == 280.0
    assert mkt["in_n"] == 1   # 半导体净流入
    assert mkt["out_n"] == 1  # 银行净流出


def test_run_writes_tables_idempotent(tmp_path):
    db = str(tmp_path / "t.db")
    _mkdb(db)
    import board_moneyflow as bm
    assert bm.run(db, "20260103") == "20260103"
    bm.run(db, "20260103")  # 重跑幂等
    c = sqlite3.connect(db)
    n_board = c.execute("SELECT COUNT(*) FROM board_moneyflow_daily WHERE trade_date='20260103'").fetchone()[0]
    n_mkt = c.execute("SELECT COUNT(*) FROM market_moneyflow_daily WHERE trade_date='20260103'").fetchone()[0]
    c.close()
    assert n_board == 2   # 两行业, 重跑不翻倍
    assert n_mkt == 1


def test_run_raises_on_missing_day(tmp_path):
    db = str(tmp_path / "t.db")
    _mkdb(db)
    import board_moneyflow as bm
    with pytest.raises(RuntimeError):
        bm.run(db, "20260109")  # 该日无 moneyflow 数据


def test_board_payload_shape_and_units(tmp_path):
    db = str(tmp_path / "t.db")
    _mkdb(db)
    import board_moneyflow as bm
    bm.run(db, "20260103")
    c = scout_db.conn(db)
    try:
        p = bm.board_payload(c)
    finally:
        c.close()
    assert p["date"] == "20260103"
    # 全市场 120万 -> 0.012 亿
    assert abs(p["market"]["net_1d"] - 0.012) < 1e-9
    # 净流入榜首=半导体(150万->0.015亿), 净流出榜首=银行(-30万->-0.003亿)
    assert p["inflow_top"][0]["name"] == "半导体"
    assert abs(p["inflow_top"][0]["net_1d"] - 0.015) < 1e-9
    assert p["outflow_top"][0]["name"] == "银行"
    assert abs(p["outflow_top"][0]["net_1d"] + 0.003) < 1e-9
    # top_in 透传成分(原始万元值不转亿, 仅做展示)
    assert p["inflow_top"][0]["top_in"][0]["name"] == "甲股"


def test_board_payload_empty(tmp_path):
    db = str(tmp_path / "t.db")
    scout_db.init_schema(db)
    import board_moneyflow as bm
    c = scout_db.conn(db)
    try:
        p = bm.board_payload(c)
    finally:
        c.close()
    assert p["date"] is None
    assert p["inflow_top"] == []
