"""research_overlay 单测: fixture 临时双库, 验证桥映射 + 软语义 + fail-soft."""
import os
import sqlite3
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _make_market(path, scm_rows, names_rows):
    """scm_rows: [(snapshot_date, board_code, ts_code)]; names_rows: [(code, name)]."""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE stock_concept_map (snapshot_date TEXT, board_code TEXT, "
              "board_name TEXT, ts_code TEXT, name TEXT)")
    c.executemany("INSERT INTO stock_concept_map(snapshot_date,board_code,ts_code) "
                  "VALUES (?,?,?)", scm_rows)
    c.execute("CREATE TABLE stock_names (code TEXT, ts_code TEXT, name TEXT, updated_at TEXT)")
    c.executemany("INSERT INTO stock_names(code,name) VALUES (?,?)", names_rows)
    c.commit(); c.close()


def _make_lab(path, note_rows, map_rows):
    """note_rows: [(id, industry_id, industry_name, rating, valid_until, qc_status, conclusion)];
    map_rows: [(industry_id, board_code, board_name, source, weight)]."""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE industry (id INTEGER PRIMARY KEY, name TEXT)")
    c.execute("CREATE TABLE research_note (id INTEGER PRIMARY KEY, industry_id INTEGER, "
              "bot_id TEXT, rating TEXT, valid_until TEXT, qc_status TEXT, conclusion TEXT)")
    c.execute("CREATE TABLE industry_board_map (industry_id INTEGER, board_code TEXT, "
              "board_name TEXT, source TEXT, weight REAL)")
    seen = {}
    for nid, iid, iname, rating, vu, qc, concl in note_rows:
        if iid not in seen:
            c.execute("INSERT INTO industry(id,name) VALUES (?,?)", (iid, iname)); seen[iid] = 1
        c.execute("INSERT INTO research_note(id,industry_id,bot_id,rating,valid_until,qc_status,conclusion) "
                  "VALUES (?,?,?,?,?,?,?)", (nid, iid, "res11", rating, vu, qc, concl))
    c.executemany("INSERT INTO industry_board_map(industry_id,board_code,board_name,source,weight) "
                  "VALUES (?,?,?,?,?)", map_rows)
    c.commit(); c.close()


@pytest.fixture
def dbs(tmp_path, monkeypatch):
    mkt = str(tmp_path / "market.db"); lab = str(tmp_path / "lab.db")
    monkeypatch.setenv("SCOUT_DB_PATH", mkt)
    monkeypatch.setenv("SCOUT_RESEARCH_DB_PATH", lab)
    return mkt, lab


NOW = "2026-06-25 12:00:00"


def test_code_bullish_valid_pass_no_downgrade(dbs):
    import research_overlay as ro
    mkt, lab = dbs
    _make_market(mkt, [("20260624", "BK1303.DC", "300750.SZ")], [("300750", "宁德时代")])
    _make_lab(lab,
              [(1, 1, "锂电", "看多(置信度中)", "2026-07-09 00:00:00", "pass", "锂电底部磨底需求放量")],
              [(1, "BK1303.DC", "锂电池", "manual", 1.0)])
    e = ro.compute(codes=["300750.SZ"], now_str=NOW)[0]
    assert e["covered"] is True
    assert e["industry"] == "锂电" and e["stance"] == "看多"
    assert e["tier_delta"] == 0 and e["risk_flag"] is False
    assert e["name"] == "宁德时代"


def test_code_bearish_downgrade(dbs):
    import research_overlay as ro
    mkt, lab = dbs
    _make_market(mkt, [("20260624", "BK0479.DC", "600019.SH")], [("600019", "宝钢股份")])
    _make_lab(lab,
              [(72, 5, "钢铁", "看空(置信度中)", "2026-07-02 00:00:00", "pass", "周期下行吨钢利润压缩")],
              [(5, "BK0479.DC", "钢铁", "manual", 1.0)])
    e = ro.compute(codes=["600019.SH"], now_str=NOW)[0]
    assert e["stance"] == "看空"
    assert e["tier_delta"] == -1 and e["risk_flag"] is True


def test_code_expired_downgrade(dbs):
    import research_overlay as ro
    mkt, lab = dbs
    _make_market(mkt, [("20260624", "BK0478.DC", "601899.SH")], [("601899", "紫金矿业")])
    _make_lab(lab,
              [(68, 4, "有色金属", "看多(置信度中)", "2026-06-20 00:00:00", "pass", "周期过热铜铝高位")],
              [(4, "BK0478.DC", "有色金属", "auto", 1.0)])
    e = ro.compute(codes=["601899.SH"], now_str=NOW)[0]
    assert e["expired"] is True
    assert e["tier_delta"] == -1 and e["risk_flag"] is True


def test_code_qc_fail_downgrade(dbs):
    import research_overlay as ro
    mkt, lab = dbs
    _make_market(mkt, [("20260624", "BK1106.DC", "688235.SH")], [("688235", "百济神州")])
    _make_lab(lab,
              [(77, 3, "创新药", "看多(置信度中)", "2026-08-25 00:00:00", "fail", "BD出海加速")],
              [(3, "BK1106.DC", "创新药", "auto", 1.0)])
    e = ro.compute(codes=["688235.SH"], now_str=NOW)[0]
    assert e["qc_status"] == "fail"
    assert e["tier_delta"] == -1 and e["risk_flag"] is True


def test_code_qc_warn_no_downgrade(dbs):
    import research_overlay as ro
    mkt, lab = dbs
    _make_market(mkt, [("20260624", "BK1136.DC", "688981.SH")], [("688981", "中芯国际")])
    _make_lab(lab,
              [(63, 8, "半导体", "看多(置信度中)", "2026-07-25 00:00:00", "warn", "AI Capex超预期")],
              [(8, "BK1136.DC", "半导体", "manual", 1.0)])
    e = ro.compute(codes=["688981.SH"], now_str=NOW)[0]
    assert e["qc_status"] == "warn"
    assert e["tier_delta"] == 0 and e["risk_flag"] is False


def test_code_multi_industry_picks_highest_weight(dbs):
    import research_overlay as ro
    mkt, lab = dbs
    # 同一票命中两板块: BK_A(weight 0.6 看多) + BK_B(weight 1.0 看空) -> 主行业取 weight 高的看空
    _make_market(mkt,
                 [("20260624", "BK_A", "000001.SZ"), ("20260624", "BK_B", "000001.SZ")],
                 [("000001", "测试股")])
    _make_lab(lab,
              [(10, 1, "甲行业", "看多(置信度中)", "2026-08-01 00:00:00", "pass", "甲结论"),
               (20, 2, "乙行业", "看空(置信度中)", "2026-08-01 00:00:00", "pass", "乙结论")],
              [(1, "BK_A", "甲板块", "auto", 0.6), (2, "BK_B", "乙板块", "manual", 1.0)])
    e = ro.compute(codes=["000001.SZ"], now_str=NOW)[0]
    assert e["industry"] == "乙行业" and e["stance"] == "看空"
    assert {s["industry"] for s in e["secondary"]} == {"甲行业"}


def test_code_no_coverage(dbs):
    import research_overlay as ro
    mkt, lab = dbs
    _make_market(mkt, [("20260624", "BK_X", "600000.SH")], [("600000", "浦发银行")])
    _make_lab(lab, [(1, 1, "锂电", "看多(置信度中)", "2026-08-01 00:00:00", "pass", "x")],
              [(1, "BK1303.DC", "锂电池", "manual", 1.0)])  # 个股板块 BK_X 不在映射里
    e = ro.compute(codes=["600000.SH"], now_str=NOW)[0]
    assert e["covered"] is False and e["note"] == "无行业研究覆盖"
    assert e["tier_delta"] == 0 and e["risk_flag"] is False


def test_board_mode(dbs):
    import research_overlay as ro
    mkt, lab = dbs
    _make_market(mkt, [], [])
    _make_lab(lab,
              [(67, 11, "白酒", "看空(置信度中)", "2026-07-09 00:00:00", "pass", "白酒下行中段批价倒挂")],
              [(11, "BK0438.DC", "白酒", "manual", 1.0)])
    e = ro.compute(boards=["BK0438.DC"], now_str=NOW)[0]
    assert e["key"] == "BK0438.DC" and e["covered"] is True
    assert e["industry"] == "白酒" and e["stance"] == "看空" and e["tier_delta"] == -1


def test_failsoft_lab_missing(dbs, tmp_path, monkeypatch):
    import research_overlay as ro
    mkt, _ = dbs
    _make_market(mkt, [("20260624", "BK1303.DC", "300750.SZ")], [("300750", "宁德时代")])
    monkeypatch.setenv("SCOUT_RESEARCH_DB_PATH", str(tmp_path / "nonexistent.db"))
    out = ro.compute(codes=["300750.SZ"], now_str=NOW)
    assert out[0]["covered"] is False  # 不抛错, 降级无覆盖


def test_from_reviews_top6(dbs):
    import research_overlay as ro
    mkt, lab = dbs
    _make_market(mkt, [("20260624", "BK1303.DC", "300750.SZ")], [("300750", "宁德时代")])
    _make_lab(lab, [(1, 1, "锂电", "看多(置信度中)", "2026-08-01 00:00:00", "pass", "锂电底部")],
              [(1, "BK1303.DC", "锂电池", "manual", 1.0)])
    # 注入今日 intraday_review (reviewer=bot7), 用 compute 的同一 market 库
    today = datetime.now().strftime("%Y-%m-%d")
    c = sqlite3.connect(mkt)
    c.execute("CREATE TABLE intraday_review (trade_date TEXT, code TEXT, name TEXT, sources TEXT, "
              "logic_stars INTEGER, action_stars INTEGER, summary TEXT, reviewed_at TEXT, "
              "reviewer TEXT)")
    c.execute("INSERT INTO intraday_review(trade_date,code,logic_stars,action_stars,reviewer) "
              "VALUES (?,?,?,?,?)", (today, "300750", 5, 4, "bot7"))
    c.commit(); c.close()
    out = ro.from_reviews("bot7", now_str="2026-06-25 12:00:00")
    assert len(out) == 1 and out[0]["key"] == "300750" and out[0]["industry"] == "锂电"


def test_mainlines_topn(dbs):
    import research_overlay as ro
    mkt, lab = dbs
    _make_market(mkt, [], [])
    _make_lab(lab, [(67, 11, "白酒", "看空(置信度中)", "2026-07-09 00:00:00", "pass", "白酒下行")],
              [(11, "BK0438.DC", "白酒", "manual", 1.0)])
    c = sqlite3.connect(mkt)
    c.execute("CREATE TABLE board_trend_daily (trade_date TEXT, board_code TEXT, rank INTEGER)")
    c.execute("INSERT INTO board_trend_daily VALUES ('2026-06-25','BK0438.DC',1)")
    c.commit(); c.close()
    out = ro.mainlines(10, now_str="2026-06-25 12:00:00")
    assert len(out) == 1 and out[0]["key"] == "BK0438.DC" and out[0]["stance"] == "看空"


def test_render_prompt_block_downgrade_marker():
    import research_overlay as ro
    entries = [{"key": "600019", "name": "宝钢股份", "covered": True, "industry": "钢铁",
                "stance": "看空", "valid_until": "2026-07-02 00:00:00", "expired": False,
                "qc_status": "pass", "snippet": "周期下行", "secondary": [],
                "tier_delta": -1, "risk_flag": True, "note": ""}]
    block = ro.render_prompt_block(entries, "标题:")
    assert "标题:" in block and "钢铁 看空" in block and "压一档" in block


def test_render_prompt_block_empty():
    import research_overlay as ro
    assert ro.render_prompt_block([], "标题:") == ""


def test_code_pure_date_valid_until_today_not_expired(dbs):
    import research_overlay as ro
    mkt, lab = dbs
    _make_market(mkt, [("20260624", "BK1303.DC", "300750.SZ")], [("300750", "宁德时代")])
    _make_lab(lab,
              [(1, 1, "锂电", "看多(置信度中)", "2026-06-25", "pass", "纯日期有效期到今天")],
              [(1, "BK1303.DC", "锂电池", "manual", 1.0)])
    e = ro.compute(codes=["300750.SZ"], now_str=NOW)[0]
    assert e["expired"] is False
    assert e["tier_delta"] == 0 and e["risk_flag"] is False
