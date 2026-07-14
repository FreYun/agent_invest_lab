"""valuation_agent 触发器单测: 四个触发条件 + 上下文打包. 全临时库."""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402
import valuation_agent as A  # noqa: E402
from test_valuation import tmp_db  # noqa: E402

CFG = {"name": "紫金矿业", "driver": {"kind": "sge", "symbol": "Au99.99"},
       "research_industries": ["有色金属", "黄金"], "agent_ttl_days": 10}


def _seed_band(c, close=27.0, lo=24.0, hi=30.0, avg5=800.0, center=800.0,
               st=False):
    meta = {"driver": {"avg5": avg5}, "clamp_ctx": {}}
    if st:
        meta["st"] = {"pe_med60": 11.6, "pe_q20": 10.9, "pe_q80": 12.4,
                      "center_mech": 28.9, "width_mech": 0.065}
        meta["tech"] = {"close": close, "ma5": 27.1, "ma10": 27.0, "ma20": 26.8,
                        "ma60": 25.5, "bias20": 0.0075, "pos20": 0.62,
                        "dist_hi60": -0.03, "ret5": 0.01, "ret10": 0.04,
                        "vol_ratio5_20": 1.2, "amp5_avg": 0.035}
    cols = ("trade_date, ts_code, driver_center, price_lo, price_hi, close, "
            "meta_json" + (", st_center, st_lo, st_hi, st_pos, st_source" if st else ""))
    vals = [center, lo, hi, close, json.dumps(meta)]
    if st:
        vals += [28.9, 27.0, 30.8, 0.33, "auto"]
    ph = ",".join("?" * (len(vals)))
    c.execute(f"INSERT OR REPLACE INTO valuation_band_daily ({cols}) "
              f"VALUES ('20260710','601899.SH',{ph})", vals)


def _seed_agent_run(c, run_at):
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, rationale, "
              "valid_until, trigger) VALUES (?,'601899.SH','x','20991231','weekly')",
              (run_at,))


def test_trigger_weekly_when_never_ran():
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c)
    c.commit()
    assert "weekly" in A.check_triggers(c, "601899.SH", CFG, "20260710")
    c.close(); os.unlink(path)


def test_trigger_weekly_after_7_days():
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c)
    _seed_agent_run(c, "2026-07-01 22:00:00")   # 9 天前
    c.commit()
    assert "weekly" in A.check_triggers(c, "601899.SH", CFG, "20260710")
    c.close(); os.unlink(path)


def test_no_trigger_recent_run_and_calm():
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c)                                # 现价在带内, 金价平稳
    _seed_agent_run(c, "2026-07-08 22:00:00")   # 2 天前
    c.commit()
    assert A.check_triggers(c, "601899.SH", CFG, "20260710") == []
    c.close(); os.unlink(path)


def test_trigger_earnings_on_quarter_update():
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c)
    _seed_agent_run(c, "2026-07-08 22:00:00")
    c.execute("INSERT INTO valuation_quarter_est (ts_code, quarter, status, "
              "profit_lo, profit_hi, updated_at) VALUES "
              "('601899.SH','2026Q2','forecast',220.0,240.0,'2026-07-09 21:40:00')")
    c.commit()
    assert "earnings" in A.check_triggers(c, "601899.SH", CFG, "20260710")
    c.close(); os.unlink(path)


def test_trigger_driver_shift_and_band_breach():
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c, close=31.0, lo=24.0, hi=30.0, avg5=850.0, center=800.0)
    _seed_agent_run(c, "2026-07-08 22:00:00")
    c.commit()
    got = A.check_triggers(c, "601899.SH", CFG, "20260710")
    assert "driver_shift" in got     # |850/800-1|=6.25% > 3%
    assert "band_breach" in got      # 31 > 30
    c.close(); os.unlink(path)


def test_build_context_includes_scout_outputs():
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c)
    c.execute("INSERT INTO valuation_quarter_est (ts_code, quarter, status, "
              "profit_lo, profit_hi) VALUES ('601899.SH','2026Q3','estimated',126.0,220.0)")
    yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    c.execute("INSERT INTO intraday_review (trade_date, code, name, logic_stars, "
              "action_stars, summary, reviewer) VALUES "
              "(?,'601899','紫金矿业',4,3,'金价强, 铜价企稳','bot11')", (yday,))
    c.execute("INSERT INTO intraday_board_review (trade_date, board_code, board_name, "
              "summary, continuation_stars, reviewer) VALUES "
              "(?,'BK1027','黄金概念','板块走强',4,'bot7')", (yday,))
    c.commit()
    ctx = A.build_context(c, "601899.SH", CFG)
    assert "机械层" in ctx and "2026Q3" in ctx
    assert "金价强, 铜价企稳" in ctx        # 个股点评进上下文
    assert "黄金概念" in ctx                # 行业关键词命中板块点评
    c.close(); os.unlink(path)


# ---------- 二期: daily_st 双轨 ----------

CFG_M = {**CFG, "model": {"kind": "commodity_pe", "brief": "金铜双驱动, 牛市按成长给估值"}}


def _seed_daily_st_run(c, run_at):
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, rationale, "
              "valid_until, trigger, st_width_pct) VALUES "
              "(?,'601899.SH','st','20991231','daily_st',0.05)", (run_at,))


def test_check_triggers_ignores_daily_st_rows():
    """轻量行天天新增, 不得吸收 weekly/earnings 的基线."""
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c)
    _seed_agent_run(c, "2026-07-01 22:00:00")     # 深度 9 天前
    _seed_daily_st_run(c, "2026-07-09 22:20:00")  # 昨天的轻量行
    c.commit()
    assert "weekly" in A.check_triggers(c, "601899.SH", CFG, "20260710")
    c.close(); os.unlink(path)


def test_weekly_boundary_exactly_7_days():
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c)
    _seed_agent_run(c, "2026-07-03 22:00:00")     # 正好 7 天 -> 触发
    c.commit()
    assert "weekly" in A.check_triggers(c, "601899.SH", CFG, "20260710")
    c.close(); os.unlink(path)
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c)
    _seed_agent_run(c, "2026-07-04 22:00:00")     # 6 天 -> 不触发
    c.commit()
    assert "weekly" not in A.check_triggers(c, "601899.SH", CFG, "20260710")
    c.close(); os.unlink(path)


def test_build_st_context_blocks():
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c, st=True)
    _seed_daily_st_run(c, "2026-07-09 22:20:00")
    c.commit()
    ctx = A.build_st_context(c, "601899.SH", CFG_M)
    assert "短期做T带现状" in ctx and "28.9" in ctx
    assert "金铜双驱动" in ctx                    # model.brief 注入
    assert "技术面事实" in ctx
    assert "你上次的短期研判" in ctx
    assert "市场情绪" not in ctx                  # 临时库无 regime 表 -> 静默缺席
    c.close(); os.unlink(path)


def test_build_st_context_without_baseline():
    """无带子/无 st 列值时 st 块缺席(main 据此跳过 daily_st)."""
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c, st=False)                        # 有带子但无 st 值
    c.commit()
    ctx = A.build_st_context(c, "601899.SH", CFG_M)
    assert "短期做T带现状" not in ctx
    c.close(); os.unlink(path)


def test_sentiment_block_with_tables():
    path = tmp_db()
    c = scout_db.conn(path)
    c.execute("CREATE TABLE regime_classify_daily (trade_date TEXT, regime_name TEXT, "
              "total_score INTEGER, confidence TEXT)")
    c.execute("INSERT INTO regime_classify_daily VALUES ('2026-07-09','强势震荡',5,'high')")
    c.execute("CREATE TABLE regime_raw_daily (trade_date TEXT, sentiment_index REAL, "
              "limit_up_count INTEGER, limit_down_count INTEGER, "
              "advance_decline_ratio REAL, total_amount_yi REAL)")
    c.execute("INSERT INTO regime_raw_daily VALUES ('20260709',55,75,15,0.42,29323)")
    c.commit()
    blk = A._sentiment_block(c)
    assert "强势震荡" in blk and "75" in blk
    c.close(); os.unlink(path)


def test_deep_context_includes_new_blocks():
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c, st=True)
    c.commit()
    ctx = A.build_context(c, "601899.SH", CFG_M)
    assert "金铜双驱动" in ctx and "短期做T带现状" in ctx and "技术面事实" in ctx
    c.close(); os.unlink(path)


def test_build_context_last_view_excludes_daily_st():
    """深度上下文"上次观点"不被 daily_st 轻量行顶替."""
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c)
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, rationale, "
              "valid_until, trigger) VALUES "
              "('2026-07-01 22:00:00','601899.SH','深度结论ABC','20991231','weekly')")
    _seed_daily_st_run(c, "2026-07-09 22:20:00")
    c.commit()
    ctx = A.build_context(c, "601899.SH", CFG)
    assert "深度结论ABC" in ctx
    c.close(); os.unlink(path)


# ---------- 三期: --onboard 建模研判 ----------

def _seed_draft(c, ts_code="600111.SH", status="drafting"):
    c.execute("INSERT INTO valuation_target_draft (ts_code, name, status, "
              "created_at, updated_at) VALUES (?,?,?,datetime('now'),datetime('now'))",
              (ts_code, "北方稀土", status))


def test_build_onboard_context_blocks():
    path = tmp_db()
    c = scout_db.conn(path)
    c.execute("CREATE TABLE IF NOT EXISTS stock_concept_map (code TEXT, "
              "board_code TEXT, snapshot_date TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS concept_board_daily (board_code TEXT, "
              "board_name TEXT, trade_date TEXT)")
    c.execute("INSERT INTO stock_concept_map VALUES ('600111','BK1015','2026-07-10')")
    c.execute("INSERT INTO concept_board_daily VALUES ('BK1015','稀土永磁','20260710')")
    # daily 表不在 scout_db SCHEMA 里(生产由回填脚本建), 测试自建(同 test_valuation 惯例)
    c.execute("CREATE TABLE IF NOT EXISTS daily (trade_date TEXT, ts_code TEXT, "
              "open REAL, high REAL, low REAL, close REAL, pct_chg REAL, "
              "amount REAL, PRIMARY KEY (trade_date, ts_code))")
    for i in range(60):
        td = f"202605{i % 30 + 1:02d}" if i < 30 else f"202606{i % 30 + 1:02d}"
        c.execute("INSERT OR REPLACE INTO daily (trade_date, ts_code, open, high, "
                  "low, close, pct_chg, amount) VALUES (?,?,?,?,?,?,?,?)",
                  (td, "600111.SH", 20, 21, 19, 20 + i * 0.05, 0.3, 500000))
    c.commit()
    ctx = A.build_onboard_context(c, "600111.SH", "北方稀土")
    assert "北方稀土 600111.SH" in ctx
    assert "稀土永磁" in ctx                 # 概念板块块
    assert "近60日量价" in ctx
    c.close(); os.unlink(path)


def test_build_onboard_context_fail_soft():
    """概念/行情表缺失也要能出最小上下文(不抛)."""
    path = tmp_db()
    c = scout_db.conn(path)
    ctx = A.build_onboard_context(c, "600111.SH", "北方稀土")
    assert "北方稀土 600111.SH" in ctx
    c.close(); os.unlink(path)


def test_run_onboard_requires_drafting_row():
    path = tmp_db()
    assert A.run_onboard("600111.SH", path) == 1     # 无 draft 行
    c = scout_db.conn(path)
    _seed_draft(c, status="draft_ready")             # 状态不对也拒绝
    c.commit(); c.close()
    assert A.run_onboard("600111.SH", path) == 1
    os.unlink(path)


def test_run_onboard_marks_failed_when_no_write(monkeypatch):
    """chat 200 但 bot 没落草案 -> status=failed, rc=2."""
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_draft(c)
    c.commit(); c.close()
    monkeypatch.setattr(A, "_call_onboard_agent", lambda *a, **k: 0)
    assert A.run_onboard("600111.SH", path) == 2
    c = scout_db.conn(path)
    row = c.execute("SELECT status, error FROM valuation_target_draft "
                    "WHERE ts_code='600111.SH'").fetchone()
    assert row["status"] == "failed" and "writer" in row["error"]
    c.close(); os.unlink(path)


def test_run_onboard_conn_error_marks_failed(monkeypatch):
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_draft(c)
    c.commit(); c.close()
    monkeypatch.setattr(A, "_call_onboard_agent", lambda *a, **k: 1)
    assert A.run_onboard("600111.SH", path) == 1
    c = scout_db.conn(path)
    assert c.execute("SELECT status FROM valuation_target_draft").fetchone()["status"] == "failed"
    c.close(); os.unlink(path)


def _run_main(monkeypatch, path, extra_argv=("--force",)):
    """驱动 main() 单标的: load_targets 单只 + spy run_daily, 返回 spy 收到的 only 列表."""
    monkeypatch.setattr(A.valuation, "load_targets", lambda *a, **k: {"601899.SH": CFG})
    calls = []
    monkeypatch.setattr(A.valuation, "run_daily",
                        lambda db_path=None, only=None, **k: calls.append(only) or 0)
    monkeypatch.setattr(sys, "argv", ["prog", "--db", path, *extra_argv])
    rc = A.main()
    return calls, rc


def test_main_recomputes_band_after_new_anchor(monkeypatch):
    """新锚点落库(after>before) -> 立即 run_daily(only=该标的), 消除次日才生效的延迟."""
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c)
    c.commit(); c.close()

    def fake_call(ts_code, cfg, triggers, context):
        cc = scout_db.conn(path)
        _seed_agent_run(cc, "2026-07-13 22:34:00")   # 模拟 bot 经 writer 落新行
        cc.commit(); cc.close()
        return 0
    monkeypatch.setattr(A, "_call_agent", fake_call)
    calls, rc = _run_main(monkeypatch, path)
    assert calls == ["601899.SH"], f"应即时重算该标的, 实得 {calls}"
    os.unlink(path)


def test_main_skips_recompute_when_no_new_anchor(monkeypatch):
    """chat 完成但 bot 没落新行(after<=before) -> 不重算, rc=2."""
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_band(c)
    c.commit(); c.close()
    monkeypatch.setattr(A, "_call_agent", lambda *a, **k: 0)   # 不落新行
    calls, rc = _run_main(monkeypatch, path)
    assert calls == [], f"无新锚点不应重算, 实得 {calls}"
    assert rc == 2
    os.unlink(path)


# ---------- Task 11: 触发器/prompt/上下文按模型泛化 ----------

def _cfg_kind(kind):
    return {"name": "T", "model": {"kind": kind, "brief": "b"}, "driver": None,
            "research_industries": [], "enabled": True, "agent_ttl_days": 10}


def test_trigger_earnings_via_events_for_new_models():
    """非商品模型 earnings 触发改走 earnings_events 新公告."""
    import valuation_agent as A
    path = tmp_db()
    c = scout_db.conn(path)
    # 已有深度行(3天前) → weekly 不触发
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, reviewer, "
              "rationale, valid_until, trigger) VALUES (?,?,?,?,?,?)",
              ("2026-07-08 22:00:00", "600519.SH", "bot11", "r", "20990101", "weekly"))
    # 上次运行之后落了新公告
    c.execute("INSERT INTO earnings_events (ts_code, ann_date, source, end_date, "
              "net_profit_min, net_profit_max, is_hard_hit) VALUES (?,?,?,?,?,?,0)",
              ("600519.SH", "20260710", "forecast", "20260630", 4e6, 5e6))
    c.commit()
    out = A.check_triggers(c, "600519.SH", _cfg_kind("pe_band"), "20260711")
    assert "earnings" in out and "weekly" not in out
    c.close()
    os.unlink(path)


def test_trigger_consensus_shift():
    """consensus_pe: 一致预期净利 5 日变动超 ±5% → consensus_shift."""
    import valuation_agent as A
    path = tmp_db()
    c = scout_db.conn(path)
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, reviewer, "
              "rationale, valid_until, trigger) VALUES (?,?,?,?,?,?)",
              ("2026-07-08 22:00:00", "603986.SH", "bot11", "r", "20990101", "weekly"))
    for i, td in enumerate(["20260706", "20260707", "20260708", "20260709",
                            "20260710", "20260711"]):
        c.execute("INSERT INTO consensus_profit_daily VALUES (?,?,?)",
                  ("603986.SH", td, 10.0 if i < 5 else 11.0))   # +10%
    c.commit()
    out = A.check_triggers(c, "603986.SH", _cfg_kind("consensus_pe"), "20260711")
    assert "consensus_shift" in out
    # pe_band 不做该检查
    out2 = A.check_triggers(c, "603986.SH", _cfg_kind("pe_band"), "20260711")
    assert "consensus_shift" not in out2
    c.close()
    os.unlink(path)


def test_band_block_renders_by_model_kind():
    """_band_block: 非商品模型无商品价锚行, 有各自锚行; consensus 带预期旁证."""
    import valuation_agent as A
    path = tmp_db()
    c = scout_db.conn(path)
    meta = {"model_kind": "consensus_pe",
            "anchor": {"kind": "consensus_eps", "eps_fwd": 2.5,
                       "profit_consensus_yi": 16.6, "consensus_date": "20260710",
                       "lag_days": 0, "chg30": 0.08,
                       "fy": {"fy1_yi": 13.0, "fy2_yi": 18.0}},
            "pe_quantile_raw": {"lo": 40, "hi": 90}, "pe_n": 250}
    c.execute("INSERT INTO valuation_band_daily (trade_date, ts_code, pe_lo, pe_hi, "
              "pe_source, price_lo, price_hi, close, band_pos, meta_json) "
              "VALUES (?,?,?,?,?,?,?,?,?,?)",
              ("20260711", "603986.SH", 45.0, 85.0, "quantile",
               112.5, 212.5, 150.0, 0.375, json.dumps(meta, ensure_ascii=False)))
    c.commit()
    block = A._band_block(c, "603986.SH")
    assert "商品价锚" not in block
    assert "一致预期EPS" in block and "FY1" in block and "consensus_pe" in block
    c.close()
    os.unlink(path)


def test_judge_items_by_kind():
    import valuation_agent as A
    assert "商品价锚" in A.JUDGE_ITEMS["commodity_pe"]
    assert "anchor" in A.JUDGE_ITEMS["pe_band"]
    assert "不可修" in A.JUDGE_ITEMS["pb_band"]
    assert "一致预期" in A.JUDGE_ITEMS["consensus_pe"]
