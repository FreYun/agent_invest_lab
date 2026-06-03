"""Per-run 可买基金白名单隔离测试。

背景：之前是单一全局文件 FUND_BUYABLE_CODES_FILE，两 run 并发会被后启动的 run 覆盖。
修复：改成 FUND_BUYABLE_CODES_DIR 目录 + <dir>/<run_id>.json 按 run 隔离，_load_curated_buyable_codes
接受 run_id 参数；空 run_id 或文件不存在 → None（保持 lab-no-world 的"不限制"语义）。

覆盖：
- _load_curated_buyable_codes(run_id) 按 run_id 选文件
- 空 run_id 走 fallback（None）
- 文件不存在走 fallback（None）
- run_id 含 '/'、'..' 等被拒（防 path-traversal）
- portfolio_get_buyable_funds(run_id=...) 按 run_id 收窄；空 run_id 返全集 + curated=false
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
def tmp_buyable_dir(monkeypatch, tmp_path):
    """临时 FUND_BUYABLE_CODES_DIR。返回 dir 路径；测试自己 write 子文件。"""
    d = tmp_path / "buyable"
    d.mkdir()
    monkeypatch.setenv("FUND_BUYABLE_CODES_DIR", str(d))
    return d


@pytest.fixture
def reload_server(tmp_db, tmp_buyable_dir):
    """server.py import 时绑定 DB_PATH 和 FUND_BUYABLE_CODES_DIR，monkeypatch 后必须 reload。"""
    import server
    importlib.reload(server)
    return server


def _write_codes(d, run_id: str, codes: list[str]) -> None:
    (d / f"{run_id}.json").write_text(json.dumps({"fund_codes": codes}) + "\n")


def _write_by_bot(d, run_id: str, by_bot: dict[str, list[str]]) -> None:
    union = sorted({code for codes in by_bot.values() for code in codes})
    (d / f"{run_id}.json").write_text(json.dumps({"fund_codes": union, "by_bot": by_bot}) + "\n")


def _seed_fund_nav(db_path: str, codes: list[str]) -> None:
    """fund_nav 需要至少一行净值数据；portfolio_get_buyable_funds 从 DISTINCT fund_code 取全集。"""
    conn = sqlite3.connect(db_path)
    # 先建 fund_info（fund_nav 外键参考；很多测试都补的）
    for code in codes:
        conn.execute(
            "INSERT OR IGNORE INTO fund_info (fund_code, fund_name) VALUES (?, ?)",
            (code, f"fund-{code}"),
        )
        conn.execute(
            "INSERT INTO fund_nav (fund_code, nav_date, nav) VALUES (?, '2026-01-02', 1.0)",
            (code,),
        )
    conn.commit()
    conn.close()


# === _load_curated_buyable_codes 直接 unit test ===

def test_load_returns_codes_for_known_run(reload_server, tmp_buyable_dir):
    _write_codes(tmp_buyable_dir, "runA", ["A1", "A2"])
    assert reload_server._load_curated_buyable_codes("runA") == ["A1", "A2"]


def test_load_isolates_two_runs(reload_server, tmp_buyable_dir):
    _write_codes(tmp_buyable_dir, "runA", ["A1"])
    _write_codes(tmp_buyable_dir, "runB", ["B1"])
    assert reload_server._load_curated_buyable_codes("runA") == ["A1"]
    assert reload_server._load_curated_buyable_codes("runB") == ["B1"]


def test_load_empty_run_id_returns_none(reload_server, tmp_buyable_dir):
    _write_codes(tmp_buyable_dir, "runA", ["A1"])
    assert reload_server._load_curated_buyable_codes("") is None


def test_load_missing_file_returns_none(reload_server, tmp_buyable_dir):
    # 目录设了但本 run 没文件 → 走 fallback（不限制）
    assert reload_server._load_curated_buyable_codes("runMissing") is None


def test_load_rejects_path_traversal(reload_server, tmp_buyable_dir):
    """run_id 不应该能跳出 dir。"""
    # 即使 ../etc/passwd 物理存在也不会读
    assert reload_server._load_curated_buyable_codes("../etc/passwd") is None
    assert reload_server._load_curated_buyable_codes("a/b") is None
    assert reload_server._load_curated_buyable_codes("..") is None


def test_load_dir_not_set_returns_none(monkeypatch, tmp_db):
    """没设 FUND_BUYABLE_CODES_DIR 时（lab-no-world）→ None。"""
    monkeypatch.delenv("FUND_BUYABLE_CODES_DIR", raising=False)
    import server
    importlib.reload(server)
    assert server._load_curated_buyable_codes("runA") is None


def test_load_empty_list_returns_none(reload_server, tmp_buyable_dir):
    """空 fund_codes 列表也视作 None（不限制）。否则 bot 就完全没基金可买了。"""
    _write_codes(tmp_buyable_dir, "runA", [])
    assert reload_server._load_curated_buyable_codes("runA") is None


# === portfolio_get_buyable_funds(run_id=...) 端到端 ===

def test_get_buyable_funds_curated_to_run(reload_server, tmp_db, tmp_buyable_dir):
    _seed_fund_nav(tmp_db, ["A1", "B1", "C1"])
    _write_codes(tmp_buyable_dir, "runA", ["A1"])
    _write_codes(tmp_buyable_dir, "runB", ["B1"])
    s = reload_server

    payload = asyncio.run(s.portfolio_get_buyable_funds(run_id="runA"))
    data = json.loads(payload)
    assert data["success"] is True
    assert data["fund_codes"] == ["A1"], data
    assert data["curated"] is True

    payload = asyncio.run(s.portfolio_get_buyable_funds(run_id="runB"))
    data = json.loads(payload)
    assert data["fund_codes"] == ["B1"], data
    assert data["curated"] is True


def test_get_buyable_funds_no_run_id_returns_all_uncurated(reload_server, tmp_db, tmp_buyable_dir):
    _seed_fund_nav(tmp_db, ["A1", "B1", "C1"])
    _write_codes(tmp_buyable_dir, "runA", ["A1"])

    payload = asyncio.run(s := reload_server.portfolio_get_buyable_funds())
    data = json.loads(payload)
    assert data["success"] is True
    assert set(data["fund_codes"]) == {"A1", "B1", "C1"}, data
    assert data["curated"] is False


def test_get_buyable_funds_run_with_no_file_returns_all_uncurated(reload_server, tmp_db, tmp_buyable_dir):
    """目录在但 run 没写文件——dashboards 等场景。"""
    _seed_fund_nav(tmp_db, ["A1", "B1"])
    payload = asyncio.run(reload_server.portfolio_get_buyable_funds(run_id="runMissing"))
    data = json.loads(payload)
    assert data["fund_codes"] == ["A1", "B1"]
    assert data["curated"] is False


# === portfolio_place_buy_order(run_id=...) 用 run-specific curated ===

def _seed_account(db_path: str, bot_id: str = "botX", cash: float = 1_000_000.0) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash, cash_in_transit, run_id) "
        "VALUES (?, ?, ?, 0, '')",
        (bot_id, cash, cash),
    )
    conn.commit()
    conn.close()


def test_place_buy_order_respects_per_run_curated(reload_server, tmp_db, tmp_buyable_dir):
    """runA 可买 A1，runB 可买 B1。runA 下 B1 单要被拒（不在本 run 池）。"""
    _seed_fund_nav(tmp_db, ["A1", "B1"])
    _seed_account(tmp_db)
    _write_codes(tmp_buyable_dir, "runA", ["A1"])
    _write_codes(tmp_buyable_dir, "runB", ["B1"])
    s = reload_server

    # runA 下 A1 → 不被白名单拒（白名单允许）；至少不会出 "不在本 bot 当前产品可买池"
    payload = asyncio.run(s.portfolio_place_buy_order(
        bot_id="botX", fund_code="A1", amount=1000, trade_date="2026-01-02", reason="allowed", run_id="runA"
    ))
    data = json.loads(payload)
    assert "不在本 bot 当前产品可买池" not in (data.get("message") or ""), data

    # runA 下 B1 → 应被白名单拒
    payload = asyncio.run(s.portfolio_place_buy_order(
        bot_id="botX", fund_code="B1", amount=1000, trade_date="2026-01-02", reason="denied", run_id="runA"
    ))
    data = json.loads(payload)
    assert data["success"] is False
    assert "不在本 bot 当前产品可买池" in data["message"], data


def test_load_by_bot_prefers_bot_specific_pool(reload_server, tmp_buyable_dir):
    _write_by_bot(tmp_buyable_dir, "runC", {"bot7": ["A1"], "bot11": ["B1"]})
    assert reload_server._load_curated_buyable_codes("runC") == ["A1", "B1"]
    assert reload_server._load_curated_buyable_codes("runC", "bot7") == ["A1"]
    assert reload_server._load_curated_buyable_codes("runC", "bot11") == ["B1"]
    assert reload_server._load_curated_buyable_codes("runC", "botMissing") == []


def test_get_buyable_funds_with_bot_id_returns_bot_pool(reload_server, tmp_db, tmp_buyable_dir):
    _seed_fund_nav(tmp_db, ["A1", "B1", "C1"])
    _write_by_bot(tmp_buyable_dir, "runC", {"bot7": ["A1"], "bot11": ["B1"]})

    data = json.loads(asyncio.run(reload_server.portfolio_get_buyable_funds(run_id="runC", bot_id="bot7")))
    assert data["fund_codes"] == ["A1"], data
    assert data["bot_id"] == "bot7"
    assert data["curated"] is True

    union = json.loads(asyncio.run(reload_server.portfolio_get_buyable_funds(run_id="runC")))
    assert union["fund_codes"] == ["A1", "B1"], union


def test_place_buy_order_respects_per_bot_curated_pool(reload_server, tmp_db, tmp_buyable_dir):
    _seed_fund_nav(tmp_db, ["A1", "B1"])
    _seed_account(tmp_db, bot_id="bot7")
    _seed_account(tmp_db, bot_id="bot11")
    _write_by_bot(tmp_buyable_dir, "runC", {"bot7": ["A1"], "bot11": ["B1"]})
    s = reload_server

    ok = json.loads(asyncio.run(s.portfolio_place_buy_order(
        bot_id="bot7", fund_code="A1", amount=1000, trade_date="2026-01-02", reason="own pool", run_id="runC"
    )))
    assert ok.get("success") is True, ok

    denied = json.loads(asyncio.run(s.portfolio_place_buy_order(
        bot_id="bot7", fund_code="B1", amount=1000, trade_date="2026-01-02", reason="other pool", run_id="runC"
    )))
    assert denied["success"] is False
    assert "不在本 bot 当前产品可买池" in denied["message"], denied
    assert denied["bot_id"] == "bot7"
