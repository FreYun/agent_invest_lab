import sqlite3, os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import live_common as lc
import seed_live_run as seed


def _schema(conn):
    conn.executescript("""
    CREATE TABLE fund_bot_actions (action_id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT, fund_code TEXT, action_type TEXT, amount REAL, action_date TEXT, run_id TEXT);
    CREATE TABLE fund_bot_holdings (holding_id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT, fund_code TEXT, shares REAL, amount_invested REAL,
        entry_date TEXT, entry_nav REAL, latest_nav REAL, status TEXT, run_id TEXT);
    CREATE TABLE fund_bot_holding_lots (lot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT, fund_code TEXT, run_id TEXT, holding_id INTEGER,
        entry_date TEXT, entry_nav REAL, shares_initial REAL, shares_remaining REAL,
        cost_initial REAL, cost_remaining REAL, source_order_id INTEGER, status TEXT);
    CREATE TABLE fund_bot_orders (order_id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT, fund_code TEXT, order_type TEXT, order_date TEXT,
        order_amount REAL, status TEXT, order_run_id TEXT, settle_run_id TEXT);
    """)


def test_clone_run_rows_rekeys(tmp_path):
    conn = sqlite3.connect(":memory:"); _schema(conn)
    src = "dash-2026-07-20T15-04-39"
    conn.execute("INSERT INTO fund_bot_actions (bot_id,fund_code,action_type,amount,action_date,run_id) "
                 "VALUES ('bot18','000216','BUY',1000,'2026-07-20',?)", (src,))
    conn.execute("INSERT INTO fund_bot_holdings "
                 "(bot_id,fund_code,shares,amount_invested,entry_date,entry_nav,latest_nav,status,run_id) "
                 "VALUES ('bot18','000216',100,1000,'2026-07-20',10,10,'active',?)", (src,))
    conn.execute("INSERT INTO fund_bot_holdings "
                 "(bot_id,fund_code,shares,amount_invested,entry_date,entry_nav,latest_nav,status,run_id) "
                 "VALUES ('bot18','000216',0,0,'2026-07-10',10,10,'closed',?)", (src,))
    conn.execute("INSERT INTO fund_bot_holding_lots "
                 "(bot_id,fund_code,run_id,holding_id,entry_date,entry_nav,shares_initial,shares_remaining,cost_initial,cost_remaining,source_order_id,status) "
                 "VALUES ('bot18','000216',?,7,'2026-07-20',10,100,100,1000,1000,9,'open')", (src,))
    conn.execute("INSERT INTO fund_bot_orders (bot_id,fund_code,order_type,order_date,order_amount,status,order_run_id,settle_run_id) "
                 "VALUES ('bot18','000216','buy','2026-07-20',500,'pending',?,NULL)", (src,))
    conn.execute("INSERT INTO fund_bot_orders (bot_id,fund_code,order_type,order_date,order_amount,status,order_run_id,settle_run_id) "
                 "VALUES ('bot18','000216','buy','2026-07-10',500,'confirmed',?,?)", (src, src))
    conn.commit()

    dst = "live-bot18-20260720T150439"
    counts = seed.clone_run_rows(conn, "bot18", src, dst, "2026-07-21")
    conn.commit()

    assert counts == {"actions": 1, "holdings": 1, "lots": 1, "orders": 1,
                      "lot_repair": {"checked": 1, "rebuilt": 0}}
    # 新 run_id 下能读到复制行
    assert conn.execute("SELECT COUNT(*) FROM fund_bot_actions WHERE run_id=?", (dst,)).fetchone()[0] == 1
    assert conn.execute("SELECT shares FROM fund_bot_holdings WHERE run_id=? AND status='active'", (dst,)).fetchone()[0] == 100
    # closed 持仓不复制
    assert conn.execute("SELECT COUNT(*) FROM fund_bot_holdings WHERE run_id=?", (dst,)).fetchone()[0] == 1
    # lot 软外键置 NULL
    lot = conn.execute("SELECT holding_id, source_order_id FROM fund_bot_holding_lots WHERE run_id=?", (dst,)).fetchone()
    assert lot == (None, None)
    # 只复制 pending 单；order_run_id 重键、settle_run_id NULL
    o = conn.execute("SELECT order_run_id, settle_run_id, status FROM fund_bot_orders WHERE order_run_id=?", (dst,)).fetchone()
    assert o == (dst, None, "pending")
    # 源行不动
    assert conn.execute("SELECT COUNT(*) FROM fund_bot_actions WHERE run_id=?", (src,)).fetchone()[0] == 1


def test_state_stub_makes_run_discoverable(tmp_path):
    """seed 写的 state.json 存根能让 discover_live_runs 在首次 decide 前就发现该 live run。"""
    dst = "live-bot18-20260720T150439"
    runs_dir = tmp_path / "runs"
    state = runs_dir / dst / "state.json"
    seed.write_state_stub(str(state), dst, "bot18", "dash-2026-07-20T15-04-39")

    payload = json.loads(state.read_text())
    assert payload["bots"] == ["bot18"]
    assert payload["run_id"] == dst
    assert payload["status"] == "seeded"
    assert payload["source_run_id"] == "dash-2026-07-20T15-04-39"
    # discover_live_runs 只凭 state.json 即可发现（无需先跑引擎）
    assert lc.discover_live_runs(str(runs_dir)) == [(dst, "bot18")]


def test_clone_run_rows_repairs_stale_open_lots(tmp_path):
    conn = sqlite3.connect(":memory:"); _schema(conn)
    src = "dash-2026-07-20T15-04-39"
    conn.execute("INSERT INTO fund_bot_holdings "
                 "(bot_id,fund_code,shares,amount_invested,entry_date,entry_nav,latest_nav,status,run_id) "
                 "VALUES ('bot18','000216',100,1000,'2026-07-20',10,10,'active',?)", (src,))
    conn.execute("INSERT INTO fund_bot_holding_lots "
                 "(bot_id,fund_code,run_id,holding_id,entry_date,entry_nav,shares_initial,shares_remaining,cost_initial,cost_remaining,source_order_id,status) "
                 "VALUES ('bot18','000216',?,7,'2026-07-20',10,10,10,100,100,9,'open')", (src,))
    conn.commit()

    dst = "live-bot18-20260720T150439"
    counts = seed.clone_run_rows(conn, "bot18", src, dst, "2026-07-21")
    conn.commit()

    assert counts["lot_repair"] == {"checked": 1, "rebuilt": 1}
    h = conn.execute("SELECT holding_id, shares FROM fund_bot_holdings WHERE run_id=?", (dst,)).fetchone()
    lot = conn.execute(
        "SELECT holding_id, shares_remaining, cost_remaining, source_order_id "
        "FROM fund_bot_holding_lots WHERE run_id=? AND status='open'", (dst,)
    ).fetchone()
    assert lot == (h[0], h[1], 1000, None)


def test_backfill_existing_live_lineage_is_safe_and_idempotent(tmp_path):
    old_world = seed.WORLD
    seed.WORLD = str(tmp_path)
    try:
        runs = tmp_path / "runtime" / "runs"
        source_id = "dash-2026-07-20T15-04-39"
        live_id = "live-bot18-20260720T150439"
        source = runs / source_id
        live = runs / live_id
        source_ws = source / "workspaces" / "bot18"
        live_ws = live / "workspaces" / "bot18"
        source_ws.mkdir(parents=True)
        live_ws.mkdir(parents=True)
        (source / "strategies").mkdir()
        (source / "strategies" / "bot18.revisions.jsonl").write_text("{}\n")
        (source_ws / "METHODOLOGY.md").write_text("evolved source method\n")
        (live_ws / "METHODOLOGY.md").write_text("template method\n")
        (source_ws / "memory").mkdir()
        (source_ws / "memory" / "historical-note.md").write_text("carry me\n")
        source_compact = source / "rl-openclaw" / "history-compact" / "bot18"
        source_compact.mkdir(parents=True)
        (source_compact / "state.json").write_text(json.dumps({
            "compactText": "historical summary", "compactedUpToDate": "2026-07-18",
        }))
        live.mkdir(parents=True, exist_ok=True)
        (live / "state.json").write_text(json.dumps({
            "run_id": live_id, "bots": ["bot18"], "status": "done",
        }))

        preview = seed.backfill_existing_live_lineage(dry_run=True)
        assert preview["state_updated"] == 1
        assert preview["methodology_restored"] == 1
        assert preview["history_compact_seeded"] == 1
        assert "source_run_id" not in json.loads((live / "state.json").read_text())
        assert (live_ws / "METHODOLOGY.md").read_text() == "template method\n"

        applied = seed.backfill_existing_live_lineage()
        assert applied["missing_source"] == []
        assert json.loads((live / "state.json").read_text())["source_run_id"] == source_id
        assert (live_ws / "METHODOLOGY.md").read_text() == "evolved source method\n"
        assert (live_ws / "memory" / "historical-note.md").read_text() == "carry me\n"
        compact = json.loads((live / "rl-openclaw" / "history-compact" / "bot18" / "state.json").read_text())
        assert compact["compactText"] == "historical summary"
        assert compact["historyScope"] == str(source / "rl-openclaw")

        second = seed.backfill_existing_live_lineage(dry_run=True)
        assert second["state_updated"] == 0
        assert second["workspace_files_added"] == 0
        assert second["methodology_restored"] == 0
        assert second["history_compact_seeded"] == 0
    finally:
        seed.WORLD = old_world
