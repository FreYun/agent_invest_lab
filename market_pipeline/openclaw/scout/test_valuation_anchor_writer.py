"""valuation_anchor_writer 单测: 校验/钳制/valid_until/落库."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402
import valuation_anchor_writer as W  # noqa: E402
from test_valuation import tmp_db  # noqa: E402

CTX = {"rule_quarters": {"2026Q3": [126.0, 220.0]},
       "pe_p05": 8.0, "pe_p95": 15.0,
       "driver_min120": 700.0, "driver_max120": 900.0,
       "st_center_mech": 30.0}


def _seed_band(path, with_ctx=True):
    c = scout_db.conn(path)
    meta = {"clamp_ctx": CTX} if with_ctx else {}
    c.execute("INSERT INTO valuation_band_daily (trade_date, ts_code, meta_json) "
              "VALUES ('20260710','601899.SH',?)", (json.dumps(meta),))
    c.commit()
    c.close()


BASE = {"ts_code": "601899.SH", "trigger": "weekly", "confidence": 0.7,
        "rationale": "金价台阶上移, 一致预期上修", "sources": ["research-mcp 一致预期"]}


def test_write_clamps_and_inserts():
    path = tmp_db()
    _seed_band(path)
    out = W.write({**BASE, "driver_center": 2000.0,
                   "profit_adj": {"2026Q3": [50.0, 400.0]},
                   "pe_hi_adj": 20.0}, "bot11", db_path=path)
    c = scout_db.conn(path)
    row = c.execute("SELECT * FROM valuation_agent_view").fetchone()
    assert row["driver_center"] == 990.0                    # 900*1.1
    assert json.loads(row["profit_adj_json"]) == {"2026Q3": [63.0, 330.0]}
    assert row["pe_hi_adj"] == 15.0
    clamped = json.loads(row["clamped_json"])
    assert "driver_center" in clamped and "pe_hi_adj" in clamped
    assert row["valid_until"] >= "20260710" and row["trigger"] == "weekly"
    assert out["valid_until"] == row["valid_until"]
    c.close()
    os.unlink(path)


def test_write_all_null_is_ok():
    path = tmp_db()
    _seed_band(path)
    W.write(dict(BASE), "bot11", db_path=path)              # 无任何数值修正
    c = scout_db.conn(path)
    row = c.execute("SELECT * FROM valuation_agent_view").fetchone()
    assert row["driver_center"] is None and row["profit_adj_json"] is None
    assert row["clamped_json"] is None                      # 全 null 不该有钳制记录
    c.close()
    os.unlink(path)


def test_write_rejects_numeric_without_ctx():
    path = tmp_db()
    _seed_band(path, with_ctx=False)
    with pytest.raises(ValueError):
        W.write({**BASE, "driver_center": 850.0}, "bot11", db_path=path)
    # 全 null 仍允许
    W.write(dict(BASE), "bot11", db_path=path)
    os.unlink(path)


def test_write_validates_required():
    path = tmp_db()
    _seed_band(path)
    with pytest.raises(ValueError):
        W.write({"ts_code": "601899.SH"}, "bot11", db_path=path)   # 缺 rationale
    with pytest.raises(ValueError):
        W.write({**BASE, "ts_code": "999999.SH"}, "bot11", db_path=path)  # 不在配置
    with pytest.raises(ValueError):
        W.write({**BASE, "profit_adj": {"2026Q3": [200.0, 100.0]}}, "bot11",
                db_path=path)                                       # lo>hi
    os.unlink(path)


def test_write_daily_st_ttl_and_clamp():
    from datetime import datetime, timedelta
    path = tmp_db()
    _seed_band(path)
    out = W.write({**BASE, "trigger": "daily_st",
                   "st_center_adj": 60.0, "st_width_pct": 0.30}, "bot11", db_path=path)
    c = scout_db.conn(path)
    row = c.execute("SELECT * FROM valuation_agent_view").fetchone()
    assert row["trigger"] == "daily_st"
    assert row["st_center_adj"] == round(30.0 * 1.08, 2)     # 钳 机械±8%
    assert row["st_width_pct"] == 0.15                       # 钳 [0.03,0.15]
    clamped = json.loads(row["clamped_json"])
    assert {"st_center_adj", "st_width_pct"} <= set(clamped)
    # daily_st TTL = 2 交易日 -> round(2*1.45)=3 自然日
    expect = (datetime.now() + timedelta(days=3)).strftime("%Y%m%d")
    assert out["valid_until"] == expect == row["valid_until"]
    c.close()
    os.unlink(path)


def test_write_st_passthrough_and_validation():
    path = tmp_db()
    _seed_band(path)
    W.write({**BASE, "trigger": "daily_st",
             "st_center_adj": 29.0, "st_width_pct": 0.06}, "bot11", db_path=path)
    c = scout_db.conn(path)
    row = c.execute("SELECT * FROM valuation_agent_view").fetchone()
    assert row["st_center_adj"] == 29.0 and row["st_width_pct"] == 0.06
    assert row["clamped_json"] is None
    c.close()
    with pytest.raises(ValueError):                 # st 字段必须数值或 null
        W.write({**BASE, "st_width_pct": "宽一点"}, "bot11", db_path=path)
    os.unlink(path)


# ---------- 三期: --onboard 建模草案 ----------

def _seed_draft(path, ts_code="600111.SH", status="drafting"):
    c = scout_db.conn(path)
    c.execute("INSERT INTO valuation_target_draft (ts_code, name, status, "
              "created_at, updated_at) VALUES (?,?,?,datetime('now'),datetime('now'))",
              (ts_code, "北方稀土", status))
    c.commit(); c.close()


OB = {"ts_code": "600111.SH", "applicable": True,
      "model": {"kind": "commodity_pe",
                "brief": "稀土价格是单一最强驱动, 盈利=量×(价-成本), 用氧化镨钕价格因子"
                         "外推单季净利, 动态PE带为估值尺子, 供给侧配额决定弹性上限。"},
      "driver": {"kind": "fut", "symbol": "PM", "label": "氧化镨钕"},
      "research_industries": ["稀土", "有色金属"],
      ".openclaw": "接入注意配额政策", "rationale": "建模分析全文…"}


def test_onboard_writes_draft_ready():
    path = tmp_db()
    _seed_draft(path)
    out = W.write_onboard(dict(OB), "bot11", db_path=path)
    assert out["status"] == "draft_ready"
    c = scout_db.conn(path)
    row = c.execute("SELECT * FROM valuation_target_draft WHERE ts_code='600111.SH'").fetchone()
    assert row["status"] == "draft_ready" and row["error"] is None
    d = json.loads(row["draft_json"])
    assert d["driver"]["symbol"] == "PM" and d["applicable"] is True
    c.close(); os.unlink(path)


def test_onboard_unsupported_needs_no_model():
    path = tmp_db()
    _seed_draft(path)
    out = W.write_onboard({"ts_code": "600111.SH", "applicable": False,
                           "rationale": "银行股无商品驱动, 适合 PB-ROE 框架"},
                          "bot11", db_path=path)
    assert out["status"] == "unsupported"
    os.unlink(path)


def test_onboard_validation():
    path = tmp_db()
    _seed_draft(path)
    with pytest.raises(ValueError):    # applicable 必须是 bool
        W.write_onboard({"ts_code": "600111.SH", "rationale": "x"}, "bot11", db_path=path)
    with pytest.raises(ValueError):    # brief 太短
        W.write_onboard({**OB, "model": {"kind": "commodity_pe", "brief": "短"}},
                        "bot11", db_path=path)
    with pytest.raises(ValueError):    # driver.kind 枚举
        W.write_onboard({**OB, "driver": {"kind": "api", "symbol": "X"}},
                        "bot11", db_path=path)
    with pytest.raises(ValueError):    # research_industries 非空列表
        W.write_onboard({**OB, "research_industries": []}, "bot11", db_path=path)
    with pytest.raises(ValueError):    # kind 只支持 commodity_pe
        W.write_onboard({**OB, "model": {"kind": "pb_roe", "brief": OB["model"]["brief"]}},
                        "bot11", db_path=path)
    os.unlink(path)


def test_onboard_requires_draft_row():
    path = tmp_db()                    # 无 draft 行
    with pytest.raises(ValueError):
        W.write_onboard(dict(OB), "bot11", db_path=path)
    os.unlink(path)


# ---------- F3: write_onboard 状态守卫 ----------

def test_onboard_rejects_non_drafting_status():
    """草案状态非 drafting(如 enabled)时, write_onboard 应抛 ValueError(迟到/重复保护)."""
    path = tmp_db()
    _seed_draft(path, status="enabled")   # 行已是 enabled, 不是 drafting
    with pytest.raises(ValueError, match="草案状态已不是 drafting"):
        W.write_onboard(dict(OB), "bot11", db_path=path)
    os.unlink(path)


# ---------- 四期: basket 草案 ----------

BASKET_DRV = {"kind": "basket", "label": "铜金篮子(指数, 基期=100)",
              "components": [
                  {"kind": "fut", "symbol": "CU.SHF", "label": "沪铜主力(元/吨)",
                   "weight": 0.6},
                  {"kind": "sge", "symbol": "Au99.99", "label": "上海金(元/克)",
                   "weight": 0.4}]}


def test_onboard_basket_draft():
    path = tmp_db()
    _seed_draft(path)
    out = W.write_onboard({**OB, "driver": BASKET_DRV}, "bot11", db_path=path)
    assert out["status"] == "draft_ready"
    c = scout_db.conn(path)
    d = json.loads(c.execute("SELECT draft_json FROM valuation_target_draft "
                             "WHERE ts_code='600111.SH'").fetchone()["draft_json"])
    assert d["driver"]["kind"] == "basket" and len(d["driver"]["components"]) == 2
    c.close(); os.unlink(path)


def test_onboard_basket_validation():
    path = tmp_db()
    _seed_draft(path)
    # 坏权重(和!=1)
    bad = {**BASKET_DRV, "components": [
        {**BASKET_DRV["components"][0], "weight": 0.9},
        BASKET_DRV["components"][1]]}
    with pytest.raises(ValueError):
        W.write_onboard({**OB, "driver": bad}, "bot11", db_path=path)
    # 成分只有 1 个
    bad = {**BASKET_DRV, "components": BASKET_DRV["components"][:1]}
    with pytest.raises(ValueError):
        W.write_onboard({**OB, "driver": bad}, "bot11", db_path=path)
    os.unlink(path)


# ---------- Task 9: writer 校验解锁(多模型 + consensus_shift) ----------

def _onboard_payload(kind, with_driver=False):
    p = {"ts_code": "600519.SH", "applicable": True,
         "model": {"kind": kind, "brief": "测" * 60},
         "research_industries": ["白酒"], "rationale": "分析全文"}
    if with_driver:
        p["driver"] = {"kind": "fut", "symbol": "CU.SHF", "label": "沪铜"}
    return p


def test_onboard_accepts_new_kinds():
    import valuation_anchor_writer as W
    for kind in ("pe_band", "pb_band", "consensus_pe"):
        W._validate_onboard(_onboard_payload(kind))    # 不抛=通过


def test_onboard_rejects_unknown_kind():
    import valuation_anchor_writer as W
    import pytest
    with pytest.raises(ValueError, match="model.kind"):
        W._validate_onboard(_onboard_payload("ps_band"))


def test_onboard_driver_rules_by_kind():
    import valuation_anchor_writer as W
    import pytest
    # commodity_pe 缺 driver 报错
    p = _onboard_payload("commodity_pe")
    with pytest.raises(ValueError):
        W._validate_onboard(p)
    # commodity_pe 带合法 driver 通过
    W._validate_onboard(_onboard_payload("commodity_pe", with_driver=True))
    # 非商品 kind 带 driver 报错
    with pytest.raises(ValueError, match="不接受 driver"):
        W._validate_onboard(_onboard_payload("pe_band", with_driver=True))


def test_valid_triggers_has_consensus_shift():
    import valuation_anchor_writer as W
    assert "consensus_shift" in W.VALID_TRIGGERS
