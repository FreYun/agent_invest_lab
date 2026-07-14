import os
import sqlite3
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backfill_index_daily as B  # noqa: E402


def _mkdb(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE index_daily (
            trade_date TEXT NOT NULL, ts_code TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            pre_close REAL, pct_chg REAL, vol REAL, amount REAL,
            PRIMARY KEY (trade_date, ts_code))"""
    )
    conn.commit()
    return conn


def _df(rows):
    return pd.DataFrame(rows)


def test_upsert_writes_all_ten_columns(tmp_path):
    conn = _mkdb(str(tmp_path / "m.db"))
    df = _df([{"trade_date": "20260630", "open": 10.0, "high": 11.0, "low": 9.5,
               "close": 10.5, "pre_close": 10.2, "pct_chg": 2.94,
               "vol": 12345.0, "amount": 67890.0}])
    n = B.upsert_index_daily_full(conn, "000905.SH", df)
    assert n == 1
    row = conn.execute(
        "SELECT open,high,low,close,pre_close,pct_chg,vol,amount FROM index_daily "
        "WHERE ts_code='000905.SH' AND trade_date='20260630'").fetchone()
    # 关键: pct_chg/pre_close/amount 都有值, 与丢列版分道
    assert row == (10.0, 11.0, 9.5, 10.5, 10.2, 2.94, 12345.0, 67890.0)


def test_nan_becomes_sql_null(tmp_path):
    conn = _mkdb(str(tmp_path / "m.db"))
    df = _df([{"trade_date": "20131231", "open": 1000.0, "high": 1000.0, "low": 1000.0,
               "close": 1000.0, "pre_close": float("nan"), "pct_chg": float("nan"),
               "vol": 0.0, "amount": 0.0}])  # inception 日无前收
    B.upsert_index_daily_full(conn, "932000.CSI", df)
    row = conn.execute(
        "SELECT pre_close, pct_chg FROM index_daily WHERE ts_code='932000.CSI'").fetchone()
    assert row == (None, None)  # NaN → SQL NULL, 不是字符串 'nan'


def test_upsert_idempotent_overwrites(tmp_path):
    conn = _mkdb(str(tmp_path / "m.db"))
    base = {"trade_date": "20260630", "open": 10.0, "high": 11.0, "low": 9.5,
            "close": 10.5, "pre_close": 10.2, "pct_chg": 2.94, "vol": 1.0, "amount": 2.0}
    B.upsert_index_daily_full(conn, "000688.SH", _df([base]))
    B.upsert_index_daily_full(conn, "000688.SH", _df([{**base, "close": 99.9}]))
    rows = conn.execute("SELECT close FROM index_daily WHERE ts_code='000688.SH'").fetchall()
    assert len(rows) == 1 and rows[0][0] == 99.9  # 行数稳定 + 值被覆盖


def test_trade_date_int_stored_as_yyyymmdd(tmp_path):
    conn = _mkdb(str(tmp_path / "m.db"))
    df = _df([{"trade_date": 20260630, "open": 1, "high": 1, "low": 1, "close": 1,
               "pre_close": 1, "pct_chg": 0, "vol": 1, "amount": 1}])  # 整数 trade_date
    B.upsert_index_daily_full(conn, "399006.SZ", df)
    td = conn.execute(
        "SELECT trade_date FROM index_daily WHERE ts_code='399006.SZ'").fetchone()[0]
    assert td == "20260630"  # str() 后无 '.0', 与既有 YYYYMMDD 一致


def test_resolve_db_path_precedence(monkeypatch):
    monkeypatch.delenv("MARKET_DB_PATH", raising=False)
    default = B.resolve_db_path()
    assert default.endswith(os.path.join("database", "market.db"))
    assert "~" not in default  # expanduser 已展开, 非硬编码 /home
    assert B.resolve_db_path("/x/y.db") == "/x/y.db"  # explicit 覆盖默认
    monkeypatch.setenv("MARKET_DB_PATH", "/env/m.db")
    assert B.resolve_db_path("/x/y.db") == "/env/m.db"  # env 最高优先


def test_backfill_serial_all_codes(tmp_path, monkeypatch):
    dbp = str(tmp_path / "m.db")
    _mkdb(dbp).close()
    calls = []

    def fake_fetch(code, start, end):
        calls.append(code)
        return _df([{"trade_date": "20260630", "open": 1, "high": 1, "low": 1,
                     "close": 1, "pre_close": 1, "pct_chg": 0, "vol": 1, "amount": 1}])

    monkeypatch.setattr(B, "fetch_index_daily_full", fake_fetch)
    res = B.backfill(["000905.SH", "000688.SH"], "20260601", "20260630", dbp, sleep_s=0)
    assert res == {"000905.SH": 1, "000688.SH": 1}
    assert calls == ["000905.SH", "000688.SH"]  # 串行、按序调用


def test_backfill_failsoft_bad_code(tmp_path, monkeypatch):
    dbp = str(tmp_path / "m.db")
    _mkdb(dbp).close()

    def fake_fetch(code, start, end):
        if code == "BAD":
            raise ValueError("0 行")
        return _df([{"trade_date": "20260630", "open": 1, "high": 1, "low": 1,
                     "close": 1, "pre_close": 1, "pct_chg": 0, "vol": 1, "amount": 1}])

    monkeypatch.setattr(B, "fetch_index_daily_full", fake_fetch)
    res = B.backfill(["BAD", "000905.SH"], "20260601", "20260630", dbp, sleep_s=0)
    assert res["BAD"] == 0 and res["000905.SH"] == 1  # 单 code 失败不影响其余


def test_verify_reports_coverage(tmp_path):
    dbp = str(tmp_path / "m.db")
    conn = _mkdb(dbp)
    conn.executemany(
        "INSERT INTO index_daily (trade_date, ts_code, close) VALUES (?,?,?)",
        [("20260629", "000905.SH", 1), ("20260630", "000905.SH", 2)])
    conn.commit()
    conn.close()
    out = B.verify(dbp, ["000905.SH", "899050.BJ"])
    assert out["000905.SH"] == (2, "20260629", "20260630")
    assert out["899050.BJ"] == (0, None, None)


def test_cli_verify_smoke(tmp_path, capsys):
    dbp = str(tmp_path / "m.db")
    conn = _mkdb(dbp)
    conn.execute("INSERT INTO index_daily (trade_date, ts_code, close) "
                 "VALUES ('20260630','000905.SH',1)")
    conn.commit()
    conn.close()
    rc = B.main(["--codes", "000905.SH", "--db", dbp, "--verify"])
    assert rc == 0
    assert "000905.SH" in capsys.readouterr().out
