"""估值带三期: 草案状态机(valuation_admin)与 schema 单测. 全临时库."""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402
from test_valuation import tmp_db  # noqa: E402


def test_schema_has_target_draft_table():
    path = tmp_db()
    c = scout_db.conn(path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(valuation_target_draft)")}
    assert {"ts_code", "name", "status", "draft_json", "error",
            "created_at", "updated_at"} <= cols
    c.close(); os.unlink(path)


import valuation_admin as M  # noqa: E402


def _seed_names(path):
    c = scout_db.conn(path)
    c.execute("CREATE TABLE IF NOT EXISTS stock_names (code TEXT PRIMARY KEY, "
              "ts_code TEXT, name TEXT, updated_at TEXT)")
    c.execute("INSERT OR REPLACE INTO stock_names VALUES "
              "('600111','600111.SH','北方稀土','2026-07-01')")
    c.execute("INSERT OR REPLACE INTO stock_names VALUES "
              "('601899','601899.SH','紫金矿业','2026-07-01')")
    c.commit()
    return c


def _mk_targets_file(extra=None):
    data = {"601899.SH": {"name": "紫金矿业",
                          "driver": {"kind": "sge", "symbol": "Au99.99"},
                          "model": {"kind": "commodity_pe", "brief": "x" * 60},
                          ".openclaw": "老标的"}}
    if extra:
        data.update(extra)
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return p


DRAFT = {"ts_code": "600111.SH", "applicable": True,
         "model": {"kind": "commodity_pe", "brief": "稀土价格驱动" + "x" * 50},
         "driver": {"kind": "fut", "symbol": "PM", "label": "氧化镨钕"},
         "research_industries": ["稀土"], ".openclaw": "n", "rationale": "r"}


def _seed_ready(c, ts_code="600111.SH", draft=None):
    c.execute("INSERT OR REPLACE INTO valuation_target_draft (ts_code, name, status, "
              "draft_json, created_at, updated_at) VALUES "
              "(?,?,?,?,datetime('now'),datetime('now'))",
              (ts_code, "北方稀土", "draft_ready",
               json.dumps(draft or DRAFT, ensure_ascii=False)))
    c.commit()


def test_add_target_normalizes_and_inserts():
    path = tmp_db()
    c = _seed_names(path)
    tp = _mk_targets_file()
    code, resp = M.add_target(c, {"code": "600111"}, targets_path=tp)
    assert code == 200 and resp["ts_code"] == "600111.SH"
    assert resp["name"] == "北方稀土" and resp["remodel"] is False
    row = c.execute("SELECT status FROM valuation_target_draft "
                    "WHERE ts_code='600111.SH'").fetchone()
    assert row["status"] == "drafting"
    c.close(); os.unlink(path); os.unlink(tp)


def test_add_target_rejects_bad_and_unknown():
    path = tmp_db()
    c = _seed_names(path)
    tp = _mk_targets_file()
    assert M.add_target(c, {"code": "abc"}, targets_path=tp)[0] == 400
    assert M.add_target(c, {"code": "999999"}, targets_path=tp)[0] == 400
    c.close(); os.unlink(path); os.unlink(tp)


def test_add_target_409_when_drafting_and_remodel_flag():
    path = tmp_db()
    c = _seed_names(path)
    tp = _mk_targets_file()
    M.add_target(c, {"code": "600111"}, targets_path=tp)
    assert M.add_target(c, {"code": "600111"}, targets_path=tp)[0] == 409
    code, resp = M.add_target(c, {"code": "601899"}, targets_path=tp)  # 已启用标的=重建模
    assert code == 200 and resp["remodel"] is True
    c.close(); os.unlink(path); os.unlink(tp)


def test_enable_merges_and_writes_targets():
    path = tmp_db()
    c = _seed_names(path)
    tp = _mk_targets_file()
    _seed_ready(c)
    code, resp = M.enable_target(
        c, {"ts_code": "600111.SH", "brief": "研究部改过的方法论" + "y" * 50},
        targets_path=tp)
    assert code == 200
    with open(tp, encoding="utf-8") as f:
        data = json.load(f)
    e = data["600111.SH"]
    assert e["enabled"] is True and e["driver"]["symbol"] == "PM"
    assert e["model"]["brief"].startswith("研究部改过的方法论")
    assert e["research_industries"] == ["稀土"] and e["name"] == "北方稀土"
    assert data["601899.SH"][".openclaw"] == "老标的"        # 旧标的不受影响
    st = c.execute("SELECT status FROM valuation_target_draft "
                   "WHERE ts_code='600111.SH'").fetchone()["status"]
    assert st == "enabled"
    c.close(); os.unlink(path); os.unlink(tp)


def test_enable_remodel_keeps_other_config():
    """重建模启用: 只替换 model/driver/research_industries, 不动其他键."""
    path = tmp_db()
    c = _seed_names(path)
    tp = _mk_targets_file()
    _seed_ready(c, ts_code="601899.SH",
                draft={**DRAFT, "ts_code": "601899.SH"})
    code, _ = M.enable_target(c, {"ts_code": "601899.SH"}, targets_path=tp)
    assert code == 200
    with open(tp, encoding="utf-8") as f:
        e = json.load(f)["601899.SH"]
    assert e[".openclaw"] == "老标的" and e["driver"]["symbol"] == "PM"
    c.close(); os.unlink(path); os.unlink(tp)


def test_enable_rejects_wrong_status_and_short_brief():
    path = tmp_db()
    c = _seed_names(path)
    tp = _mk_targets_file()
    assert M.enable_target(c, {"ts_code": "600111.SH"}, targets_path=tp)[0] == 400
    c.execute("INSERT INTO valuation_target_draft (ts_code, name, status, "
              "created_at, updated_at) VALUES ('600222.SH','x','unsupported',"
              "datetime('now'),datetime('now'))")
    c.commit()
    assert M.enable_target(c, {"ts_code": "600222.SH"}, targets_path=tp)[0] == 400
    _seed_ready(c)
    code, resp = M.enable_target(c, {"ts_code": "600111.SH", "brief": "太短"},
                                 targets_path=tp)
    assert code == 400 and "brief" in resp["error"]
    c.close(); os.unlink(path); os.unlink(tp)


def test_dismiss_and_list():
    path = tmp_db()
    c = _seed_names(path)
    _seed_ready(c)
    assert M.dismiss_draft(c, {"ts_code": "600111.SH"})[0] == 200
    d = M.list_drafts(c)
    assert d["drafts"][0]["status"] == "dismissed"
    assert d["drafts"][0]["draft"]["driver"]["symbol"] == "PM"   # draft_json 已解析
    assert M.dismiss_draft(c, {"ts_code": "600111.SH"})[0] == 400  # 已 dismissed 不可再
    c.close(); os.unlink(path)


def test_toggle_target():
    tp = _mk_targets_file()
    code, resp = M.toggle_target({"ts_code": "601899.SH", "enabled": False},
                                 targets_path=tp)
    assert code == 200 and resp["enabled"] is False
    with open(tp, encoding="utf-8") as f:
        assert json.load(f)["601899.SH"]["enabled"] is False
    assert M.toggle_target({"ts_code": "999999.SH", "enabled": True},
                           targets_path=tp)[0] == 400
    os.unlink(tp)


# ---------- F1: drafting 孤儿超时可覆盖 ----------

def test_add_target_stale_drafting_allows_resubmit():
    """drafting 且 updated_at 超 30 分钟视为孤儿, 可覆盖重发 → 返回 200."""
    path = tmp_db()
    c = _seed_names(path)
    tp = _mk_targets_file()
    # 与生产写入路径一致: updated_at 用 Python 本地时字符串(非 SQLite UTC datetime('now'))
    stale_ts = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO valuation_target_draft (ts_code, name, status, "
              "draft_json, error, created_at, updated_at) "
              "VALUES ('600111.SH','北方稀土','drafting',NULL,NULL,?,?)",
              (stale_ts, stale_ts))
    c.commit()
    code, resp = M.add_target(c, {"code": "600111"}, targets_path=tp)
    assert code == 200, f"期望 200, 实际 {code}: {resp}"
    assert resp["status"] == "drafting"
    c.close(); os.unlink(path); os.unlink(tp)


def test_add_target_fresh_drafting_still_409():
    """drafting 且 updated_at 在 30 分钟内, 仍返回 409."""
    path = tmp_db()
    c = _seed_names(path)
    tp = _mk_targets_file()
    # 与生产写入路径一致: updated_at 用 Python 本地时字符串(非 SQLite UTC datetime('now'))
    fresh_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO valuation_target_draft (ts_code, name, status, "
              "draft_json, error, created_at, updated_at) "
              "VALUES ('600111.SH','北方稀土','drafting',NULL,NULL,?,?)",
              (fresh_ts, fresh_ts))
    c.commit()
    code, resp = M.add_target(c, {"code": "600111"}, targets_path=tp)
    assert code == 409, f"期望 409, 实际 {code}: {resp}"
    c.close(); os.unlink(path); os.unlink(tp)


# ---------- 四期: enable 的 basket driver 防线 ----------

BASKET_DRAFT = {**DRAFT, "driver": {
    "kind": "basket", "label": "铜金篮子(指数, 基期=100)",
    "components": [
        {"kind": "fut", "symbol": "CU.SHF", "label": "沪铜主力(元/吨)", "weight": 0.6},
        {"kind": "sge", "symbol": "Au99.99", "label": "上海金(元/克)", "weight": 0.4}]}}


def test_enable_basket_writes_targets():
    path = tmp_db()
    c = _seed_names(path)
    tp = _mk_targets_file()
    _seed_ready(c, draft=BASKET_DRAFT)
    code, _ = M.enable_target(c, {"ts_code": "600111.SH"}, targets_path=tp)
    assert code == 200
    with open(tp, encoding="utf-8") as f:
        data = json.load(f)
    assert data["600111.SH"]["driver"]["kind"] == "basket"
    # 落盘后的 targets 必须能被 load_targets 读回(全体标的不被坏配置拖垮)
    import valuation as V
    t = V.load_targets(tp)
    assert t["600111.SH"]["driver"]["kind"] == "basket"
    c.close(); os.unlink(path); os.unlink(tp)


def test_enable_rejects_bad_basket_draft():
    path = tmp_db()
    c = _seed_names(path)
    tp = _mk_targets_file()
    bad = {**BASKET_DRAFT, "driver": {**BASKET_DRAFT["driver"], "components": [
        {**BASKET_DRAFT["driver"]["components"][0], "weight": 0.9},
        BASKET_DRAFT["driver"]["components"][1]]}}
    _seed_ready(c, draft=bad)
    code, resp = M.enable_target(c, {"ts_code": "600111.SH"}, targets_path=tp)
    assert code == 400 and "driver" in resp["error"]
    with open(tp, encoding="utf-8") as f:
        assert "600111.SH" not in json.load(f)      # 坏草案未落盘
    c.close(); os.unlink(path); os.unlink(tp)


# ---------- search_stocks(加股自动补全) ----------

def _seed_names3(path):
    """三只股: 两只名字含'金', 用于多命中场景."""
    c = scout_db.conn(path)
    c.execute("CREATE TABLE IF NOT EXISTS stock_names (code TEXT PRIMARY KEY, "
              "ts_code TEXT, name TEXT, updated_at TEXT)")
    c.executemany("INSERT OR REPLACE INTO stock_names VALUES (?,?,?,'2026-07-01')", [
        ("600111", "600111.SH", "北方稀土"),
        ("601899", "601899.SH", "紫金矿业"),
        ("600489", "600489.SH", "中金黄金"),
    ])
    c.commit()
    return c


def test_search_stocks_by_code_prefix_and_name():
    path = tmp_db()
    c = _seed_names3(path)
    r = M.search_stocks(c, "6001")
    assert [s["code"] for s in r["stocks"]] == ["600111"]
    r = M.search_stocks(c, "金")
    assert {s["name"] for s in r["stocks"]} == {"紫金矿业", "中金黄金"}
    assert all(set(s) == {"code", "ts_code", "name"} for s in r["stocks"])
    c.close(); os.unlink(path)


def test_search_stocks_pinyin_and_empty():
    path = tmp_db()
    c = _seed_names3(path)
    # 注入假拼音匹配器: zjky -> 紫金矿业
    fake = lambda name, q: name == "紫金矿业" and q == "zjky"
    r = M.search_stocks(c, "zjky", py_match=fake)
    assert [s["ts_code"] for s in r["stocks"]] == ["601899.SH"]
    # 不注入 py_match 时纯字母查询不报错, 只走名称模糊(无命中)
    assert M.search_stocks(c, "zjky")["stocks"] == []
    assert M.search_stocks(c, "")["stocks"] == []
    assert M.search_stocks(c, None)["stocks"] == []
    c.close(); os.unlink(path)


def test_search_stocks_limit():
    path = tmp_db()
    c = _seed_names3(path)
    r = M.search_stocks(c, "金", limit=1)
    assert len(r["stocks"]) == 1
    c.close(); os.unlink(path)


# ---------- add_target 名称路径 ----------

def test_add_target_by_exact_name():
    path = tmp_db()
    c = _seed_names(path)
    tp = _mk_targets_file()
    code, resp = M.add_target(c, {"code": "北方稀土"}, targets_path=tp)
    assert code == 200 and resp["ts_code"] == "600111.SH"
    c.close(); os.unlink(path); os.unlink(tp)


def test_add_target_by_fuzzy_unique_name():
    path = tmp_db()
    c = _seed_names(path)
    tp = _mk_targets_file()
    code, resp = M.add_target(c, {"code": "稀土"}, targets_path=tp)
    assert code == 200 and resp["ts_code"] == "600111.SH"
    c.close(); os.unlink(path); os.unlink(tp)


def test_add_target_name_multi_returns_candidates():
    path = tmp_db()
    c = _seed_names3(path)
    tp = _mk_targets_file()
    code, resp = M.add_target(c, {"code": "金"}, targets_path=tp)
    assert code == 400
    assert {x["name"] for x in resp["candidates"]} == {"紫金矿业", "中金黄金"}
    # 多命中不落草案
    assert c.execute("SELECT COUNT(*) FROM valuation_target_draft").fetchone()[0] == 0
    c.close(); os.unlink(path); os.unlink(tp)


def test_add_target_name_not_found():
    path = tmp_db()
    c = _seed_names(path)
    tp = _mk_targets_file()
    code, resp = M.add_target(c, {"code": "不存在的股票"}, targets_path=tp)
    assert code == 400 and "candidates" not in resp
    c.close(); os.unlink(path); os.unlink(tp)


# ---------- Task 10: enable 按草案 kind 落配置 ----------

def _seed_draft(c, ts_code, name, kind, driver=None, status="draft_ready"):
    draft = {"ts_code": ts_code, "applicable": True,
             "model": {"kind": kind, "brief": "测" * 60},
             "research_industries": ["测试"], "rationale": "r"}
    if driver:
        draft["driver"] = driver
    c.execute("INSERT OR REPLACE INTO valuation_target_draft "
              "(ts_code, name, status, draft_json, created_at, updated_at) "
              "VALUES (?,?,?,?,datetime('now'),datetime('now'))",
              (ts_code, name, status, json.dumps(draft, ensure_ascii=False)))
    c.commit()


def test_enable_pe_band_no_driver_key(tmp_path):
    """非商品 kind 启用: targets entry 不写 driver 键, model.kind 取草案."""
    path = tmp_db()
    c = scout_db.conn(path)
    tp = tmp_path / "targets.json"
    tp.write_text("{}", encoding="utf-8")
    _seed_draft(c, "600519.SH", "贵州茅台", "pe_band")
    code, resp = M.enable_target(
        c, {"ts_code": "600519.SH"}, targets_path=str(tp))
    assert code == 200, resp
    data = json.loads(tp.read_text(encoding="utf-8"))
    entry = data["600519.SH"]
    assert entry["model"]["kind"] == "pe_band"
    assert "driver" not in entry
    c.close()
    os.unlink(path)


def test_enable_missing_kind_rejected(tmp_path):
    """草案缺 model.kind → 400 硬拒(不再默认 commodity_pe)."""
    path = tmp_db()
    c = scout_db.conn(path)
    tp = tmp_path / "targets.json"
    tp.write_text("{}", encoding="utf-8")
    _seed_draft(c, "600519.SH", "贵州茅台", None)   # kind=None
    code, resp = M.enable_target(
        c, {"ts_code": "600519.SH"}, targets_path=str(tp))
    assert code == 400 and "model.kind" in resp["error"]
    c.close()
    os.unlink(path)


def test_enable_remodel_switch_kind_pops_driver(tmp_path):
    """重建模从 commodity 切 pe_band: 旧 driver 键被移除."""
    path = tmp_db()
    c = scout_db.conn(path)
    tp = tmp_path / "targets.json"
    tp.write_text(json.dumps({"600519.SH": {
        "name": "贵州茅台",
        "driver": {"kind": "fut", "symbol": "CU.SHF", "label": "沪铜"},
        "model": {"kind": "commodity_pe", "brief": "旧" * 60},
        ".openclaw": "保留我"}}), encoding="utf-8")
    _seed_draft(c, "600519.SH", "贵州茅台", "pe_band")
    code, resp = M.enable_target(
        c, {"ts_code": "600519.SH"}, targets_path=str(tp))
    assert code == 200, resp
    entry = json.loads(tp.read_text(encoding="utf-8"))["600519.SH"]
    assert "driver" not in entry and entry["model"]["kind"] == "pe_band"
    assert entry[".openclaw"] == "保留我"          # 其他字段保留
    c.close()
    os.unlink(path)
