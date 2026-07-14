"""周期股估值带 (valuation.py) 单测. 全部用临时库, 不碰生产 market.db."""
import json
import os
import sys
import tempfile
import pytest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402
import valuation as V  # noqa: E402


def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    scout_db.init_schema(path)
    return path


def test_schema_has_valuation_tables():
    path = tmp_db()
    c = scout_db.conn(path)
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"valuation_band_daily", "valuation_quarter_est",
            "valuation_agent_view"} <= tables
    band_cols = {r[1] for r in c.execute("PRAGMA table_info(valuation_band_daily)")}
    assert {"trade_date", "ts_code", "driver_center", "driver_source",
            "profit_year_lo", "profit_year_hi", "eps_lo", "eps_hi",
            "pe_lo", "pe_hi", "pe_source", "price_lo", "price_hi",
            "close", "band_pos", "meta_json"} <= band_cols
    q_cols = {r[1] for r in c.execute("PRAGMA table_info(valuation_quarter_est)")}
    assert {"ts_code", "quarter", "status", "profit_lo", "profit_hi",
            "basis_json", "updated_at"} <= q_cols
    a_cols = {r[1] for r in c.execute("PRAGMA table_info(valuation_agent_view)")}
    assert {"run_at", "ts_code", "reviewer", "driver_center", "profit_adj_json",
            "pe_lo_adj", "pe_hi_adj", "confidence", "rationale", "sources_json",
            "clamped_json", "valid_until", "trigger"} <= a_cols
    c.close()
    os.unlink(path)


def test_schema_v2_st_columns():
    """二期: 新库两表直接带 st 列."""
    path = tmp_db()
    c = scout_db.conn(path)
    band_cols = {r[1] for r in c.execute("PRAGMA table_info(valuation_band_daily)")}
    assert {"st_center", "st_lo", "st_hi", "st_pos", "st_source"} <= band_cols
    a_cols = {r[1] for r in c.execute("PRAGMA table_info(valuation_agent_view)")}
    assert {"st_center_adj", "st_width_pct"} <= a_cols
    c.close()
    os.unlink(path)


def test_schema_consensus_profit_daily():
    """多模型扩展: 一致预期净利本地缓存表(consensus_pe 模型远程失败兜底)."""
    path = tmp_db()
    c = scout_db.conn(path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(consensus_profit_daily)")}
    assert {"ts_code", "trade_date", "profit_yi"} <= cols
    # 主键幂等: 同 (ts_code, trade_date) INSERT OR REPLACE 不涨行
    c.execute("INSERT OR REPLACE INTO consensus_profit_daily VALUES (?,?,?)",
              ("603986.SH", "20260710", 11.2))
    c.execute("INSERT OR REPLACE INTO consensus_profit_daily VALUES (?,?,?)",
              ("603986.SH", "20260710", 11.5))
    assert c.execute("SELECT COUNT(*) FROM consensus_profit_daily").fetchone()[0] == 1
    c.close()
    os.unlink(path)


def test_migrate_adds_st_columns():
    """二期: 老库(无 st 列)经 init_schema 幂等补列, 两次跑不炸."""
    path = tmp_db()
    c = scout_db.conn(path)
    c.execute("DROP TABLE valuation_band_daily")
    c.execute("CREATE TABLE valuation_band_daily (trade_date TEXT NOT NULL, "
              "ts_code TEXT NOT NULL, meta_json TEXT, "
              "PRIMARY KEY (trade_date, ts_code))")
    c.execute("DROP TABLE valuation_agent_view")
    c.execute("CREATE TABLE valuation_agent_view (run_at TEXT NOT NULL, "
              "ts_code TEXT NOT NULL, trigger TEXT, "
              "PRIMARY KEY (run_at, ts_code))")
    c.commit()
    c.close()
    scout_db.init_schema(path)   # CREATE IF NOT EXISTS 跳过, _migrate ALTER 补
    scout_db.init_schema(path)   # 幂等
    c = scout_db.conn(path)
    band_cols = {r[1] for r in c.execute("PRAGMA table_info(valuation_band_daily)")}
    assert {"st_center", "st_lo", "st_hi", "st_pos", "st_source"} <= band_cols
    a_cols = {r[1] for r in c.execute("PRAGMA table_info(valuation_agent_view)")}
    assert {"st_center_adj", "st_width_pct"} <= a_cols
    c.close()
    os.unlink(path)


def test_quarter_utils():
    assert V.quarter_key("20260331") == "2026Q1"
    assert V.quarter_key("20261231") == "2026Q4"
    assert V.quarter_bounds("2026Q3") == ("20260701", "20260930")
    assert V.prev_quarter("2026Q1") == "2025Q4"
    assert V.year_quarters("2026") == ["2026Q1", "2026Q2", "2026Q3", "2026Q4"]


def test_load_targets_defaults(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"601899.SH": {
        "name": "紫金矿业", "driver": {"kind": "sge", "symbol": "Au99.99"}}}),
        encoding="utf-8")
    t = V.load_targets(str(p))["601899.SH"]
    assert t["pe_window"] == 250 and t["pe_lo_q"] == 0.10 and t["pe_hi_q"] == 0.90
    assert t["rule_tolerance"] == 0.10 and t["agent_ttl_days"] == 10
    assert t["driver_override"] is None and t["earnings_override"] is None


def _fake_fetch_income(rows):
    def fetch(api_name, params, fields, timeout=60):
        assert api_name == "income"
        return rows
    return fetch


def test_fetch_income_singles_dedup_diff():
    rows = [  # 含重复行(brze 实测) + 累计口径(元)
        {"end_date": "20260331", "ann_date": "20260422", "report_type": "1", "n_income_attr_p": 200e8},
        {"end_date": "20260331", "ann_date": "20260422", "report_type": "1", "n_income_attr_p": 200e8},
        {"end_date": "20251231", "ann_date": "20260321", "report_type": "1", "n_income_attr_p": 520e8},
        {"end_date": "20250930", "ann_date": "20251018", "report_type": "1", "n_income_attr_p": 380e8},
        {"end_date": "20250630", "ann_date": "20250827", "report_type": "1", "n_income_attr_p": 230e8},
        {"end_date": "20250331", "ann_date": "20250412", "report_type": "1", "n_income_attr_p": 100e8},
    ]
    s = V.fetch_income_singles("601899.SH", fetch=_fake_fetch_income(rows))
    by_q = {x["quarter"]: x for x in s}
    assert by_q["2025Q1"]["profit_yi"] == 100.0          # Q1=累计
    assert by_q["2025Q2"]["profit_yi"] == 130.0          # 230-100
    assert by_q["2025Q4"]["profit_yi"] == 140.0          # 520-380
    assert by_q["2026Q1"]["profit_yi"] == 200.0
    # PIT: Q4 单季可知时点 = max(年报ann, 三季报ann)
    assert by_q["2025Q4"]["ann_date"] == "20260321"


def test_series_helpers():
    gold = [(f"202601{d:02d}", 100.0 + d) for d in range(1, 31)]
    assert V.rolling_center(gold, "20260130", 5) == round(sum(126 + i for i in range(5)) / 5, 2)
    lo, hi = V.series_range(gold, "20260130", 10)
    assert (lo, hi) == (121.0, 130.0)
    assert V.rolling_center(gold, "20251231") is None
    q = V.quarter_avg(gold, "2026Q1")
    assert q == sum(101.0 + i for i in range(30)) / 30


FLAT_GOLD = [(f"2025{m:02d}{d:02d}", 800.0) for m in range(1, 13) for d in (10, 20)] + \
            [(f"2026{m:02d}{d:02d}", 800.0) for m in range(1, 8) for d in (10, 20)]

SINGLES = [
    {"quarter": "2025Q3", "end_date": "20250930", "ann_date": "20251018", "profit_yi": 150.0},
    {"quarter": "2025Q4", "end_date": "20251231", "ann_date": "20260321", "profit_yi": 140.0},
    {"quarter": "2026Q1", "end_date": "20260331", "ann_date": "20260422", "profit_yi": 200.0},
]

# PIT/run_daily 测试用: 更深历史, 保证回看窗口起点(2025-09)就有 >=2 个已知单季
RICH_SINGLES = [
    {"quarter": "2024Q3", "end_date": "20240930", "ann_date": "20241030", "profit_yi": 110.0},
    {"quarter": "2024Q4", "end_date": "20241231", "ann_date": "20250322", "profit_yi": 110.0},
    {"quarter": "2025Q1", "end_date": "20250331", "ann_date": "20250412", "profit_yi": 100.0},
    {"quarter": "2025Q2", "end_date": "20250630", "ann_date": "20250827", "profit_yi": 130.0},
] + SINGLES


def test_rule_estimate_flat_gold_g1():
    lo, hi, basis = V.rule_estimate(SINGLES, FLAT_GOLD, "20260710", 0.10)
    # 近两已知单季 = 25Q4(140) 26Q1(200); 金价横盘 g=1
    assert (lo, hi) == (round(140.0 * 0.9, 2), round(200.0 * 1.1, 2))
    assert basis["ref_quarters"] == ["2025Q4", "2026Q1"] and basis["g"] == 1.0


def test_rule_estimate_g_scales():
    gold_up = FLAT_GOLD + [(f"202607{d:02d}", 880.0) for d in range(1, 21)]
    lo, hi, basis = V.rule_estimate(SINGLES, gold_up, "20260720", 0.10)
    assert basis["g"] > 1.0 and hi > 200.0 * 1.1


def test_rule_estimate_insufficient():
    assert V.rule_estimate(SINGLES[:1], FLAT_GOLD, "20260710", 0.10) is None
    # asof 早于公告日 -> 已知单季不足
    assert V.rule_estimate(SINGLES, FLAT_GOLD, "20251001", 0.10) is None


def test_build_year_quarters_priority():
    events = [{"quarter": "2026Q2", "ann_date": "20260715",
               "cum_lo_yi": 420.0, "cum_hi_yi": 440.0}]  # 半年累计预告
    qs = V.build_year_quarters("2026", SINGLES, events, FLAT_GOLD, "20260720", 0.10,
                               overrides={"2026Q4": (100.0, 120.0)},
                               agent_adj={"2026Q3": (150.0, 170.0),
                                          "2026Q4": (999.0, 999.0)})
    by = {q["quarter"]: q for q in qs}
    assert by["2026Q1"]["status"] == "actual" and by["2026Q1"]["lo"] == 200.0
    # Q2 预告差分: 累计[420,440] - Q1实际200 = [220,240]
    assert by["2026Q2"]["status"] == "forecast"
    assert (by["2026Q2"]["lo"], by["2026Q2"]["hi"]) == (220.0, 240.0)
    # Q3 无预告无手动 -> agent 生效
    assert by["2026Q3"]["basis"]["method"] == "agent"
    assert (by["2026Q3"]["lo"], by["2026Q3"]["hi"]) == (150.0, 170.0)
    # Q4 手动覆盖优先于 agent
    assert by["2026Q4"]["basis"]["method"] == "manual"
    assert (by["2026Q4"]["lo"], by["2026Q4"]["hi"]) == (100.0, 120.0)


def test_forecast_requires_prior_actuals():
    # Q3 累计预告, 但 Q2 还只是估计 -> 预告不可差分, Q3 走规则法
    events = [{"quarter": "2026Q3", "ann_date": "20260710",
               "cum_lo_yi": 600.0, "cum_hi_yi": 650.0}]
    qs = V.build_year_quarters("2026", SINGLES, events, FLAT_GOLD, "20260710", 0.10)
    by = {q["quarter"]: q for q in qs}
    assert by["2026Q2"]["status"] == "estimated"
    assert by["2026Q3"]["status"] == "estimated"
    assert by["2026Q3"]["basis"]["method"] == "rule"


def test_load_events(tmp_path):
    path = tmp_db()
    c = scout_db.conn(path)
    c.execute("INSERT INTO earnings_events (ts_code, ann_date, source, end_date, "
              "net_profit_min, net_profit_max, is_hard_hit) VALUES "
              "('601899.SH','20260715','forecast','20260630', 4200000.0, 4400000.0, 1)")
    c.commit()
    ev = V.load_events(c, "601899.SH")
    assert ev == [{"quarter": "2026Q2", "ann_date": "20260715",
                   "cum_lo_yi": 420.0, "cum_hi_yi": 440.0}]
    c.close()
    os.unlink(path)


# ---------- Task 4: PIT 动态 PE 序列 + 分位带 + 回归旁证 + 钳制函数 ----------

def _mk_basics(n=200, start_close=20.0, total_mv=4_000_000.0):
    """构造 n 天 daily_basic: 收盘缓涨, total_mv(万元)随收盘等比."""
    out, d = [], datetime(2025, 9, 1)
    close = start_close
    for i in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append((d.strftime("%Y%m%d"), round(close, 2),
                    round(total_mv * close / start_close, 1)))
        close *= 1.001
        d += timedelta(days=1)
    return out


def test_pit_pe_series_no_future_function():
    basics = _mk_basics(200)   # 2025-09 ~ 2026-06, 覆盖 26Q1 公告日 20260422
    pes = dict(V.pit_pe_series(basics, RICH_SINGLES, [], FLAT_GOLD, 0.10))
    days = sorted(pes)
    before = [d for d in days if d < "20260422"]
    after = [d for d in days if d >= "20260422"]
    assert before and after
    # 26Q1 实际 200亿 公告后年化盈利上修 -> 同市值下 PE 下移(由 pe 反推年化核对)
    mv = {td: tm for td, _, tm in basics}
    ann_before = mv[before[-1]] / pes[before[-1]] / 1e4
    ann_after = mv[after[0]] / pes[after[0]] / 1e4
    assert ann_after > ann_before


def test_pit_pe_series_skips_insufficient():
    # 只有 1 个已知单季 -> 规则法 None -> 全序列为空
    assert V.pit_pe_series(_mk_basics(20), SINGLES[:1], [], FLAT_GOLD, 0.10) == []


# 深周期塌陷: 2024~2025 年季度极小(年化~8亿), 2026Q1 骤增 -> 2026 日年化~百亿
_TROUGH_SINGLES = [
    {"quarter": "2024Q3", "end_date": "20240930", "ann_date": "20241030", "profit_yi": 2.0},
    {"quarter": "2024Q4", "end_date": "20241231", "ann_date": "20250322", "profit_yi": 2.0},
    {"quarter": "2025Q1", "end_date": "20250331", "ann_date": "20250412", "profit_yi": 2.0},
    {"quarter": "2025Q2", "end_date": "20250630", "ann_date": "20250827", "profit_yi": 2.0},
    {"quarter": "2025Q3", "end_date": "20250930", "ann_date": "20251018", "profit_yi": 2.0},
    {"quarter": "2025Q4", "end_date": "20251231", "ann_date": "20260321", "profit_yi": 2.0},
    {"quarter": "2026Q1", "end_date": "20260331", "ann_date": "20260422", "profit_yi": 50.0},
]


def test_pit_pe_series_regime_filter():
    """min_annual: 剔除估计年化盈利<阈值的塌陷regime日(深周期畸高PE的来源)."""
    basics = _mk_basics(200)                       # 2025-09 ~ 2026-06
    full = V.pit_pe_series(basics, _TROUGH_SINGLES, [], FLAT_GOLD, 0.10)
    # 2026 公告后年化~百亿, 阈值 60 剔除 2025/年初塌陷日(年化~8亿)
    filt = V.pit_pe_series(basics, _TROUGH_SINGLES, [], FLAT_GOLD, 0.10, min_annual=60.0)
    assert 0 < len(filt) < len(full)               # 塌陷日被剔, 但仍留可比日
    assert min(d for d, _ in filt) > min(d for d, _ in full)   # 只剩靠后可比regime
    # 塌陷日 PE 畸高, 过滤后上界大幅回落
    assert max(v for _, v in filt) < max(v for _, v in full)
    # min_annual=None 时行为与旧签名一致(不过滤)
    assert V.pit_pe_series(basics, _TROUGH_SINGLES, [], FLAT_GOLD, 0.10,
                           min_annual=None) == full


def test_quantile():
    vals = list(range(1, 101))
    assert V.quantile(vals, 0.10) == 10.9
    assert V.quantile(vals, 0.90) == 90.1
    assert V.quantile([5.0], 0.5) == 5.0
    assert V.quantile([], 0.5) is None


def test_regress_gold_profit():
    # 完美线性: profit = 0.5 * gold_avg -> beta=0.5, r2=1
    singles, gold = [], []
    for i, q in enumerate(["2024Q1", "2024Q2", "2024Q3", "2024Q4",
                           "2025Q1", "2025Q2", "2025Q3", "2025Q4"]):
        px = 600.0 + i * 20
        s, e = V.quarter_bounds(q)
        gold += [(s, px), (e, px)]
        singles.append({"quarter": q, "end_date": e, "ann_date": e,
                        "profit_yi": 0.5 * px})
    r = V.regress_gold_profit(singles, gold)
    assert abs(r["beta"] - 0.5) < 1e-6 and r["r2"] == 1.0 and r["n"] == 8
    assert V.regress_gold_profit(singles[:3], gold) is None


CTX = {"rule_quarters": {"2026Q3": [126.0, 220.0], "2026Q4": [126.0, 220.0]},
       "pe_p05": 8.0, "pe_p95": 15.0,
       "driver_min120": 700.0, "driver_max120": 900.0}


def test_clamp_agent():
    p, clamped = V.clamp_agent({
        "driver_center": 2000.0,                 # 超上界 900*1.1=990
        "profit_adj": {"2026Q3": [50.0, 400.0],  # 超 [0.5*126, 1.5*220]=[63,330]
                       "2026Q1": [1.0, 2.0]},    # 非 estimated 季 -> 丢弃
        "pe_lo_adj": 5.0, "pe_hi_adj": 20.0,     # 超 [8,15]
    }, CTX)
    assert p["driver_center"] == 990.0
    assert p["profit_adj"] == {"2026Q3": [63.0, 330.0]}
    assert (p["pe_lo_adj"], p["pe_hi_adj"]) == (8.0, 15.0)
    assert {"driver_center", "profit_adj.2026Q3", "profit_adj.2026Q1",
            "pe_lo_adj", "pe_hi_adj"} <= set(clamped)


def test_clamp_agent_passthrough():
    p, clamped = V.clamp_agent({"driver_center": 850.0,
                                "profit_adj": {"2026Q3": [130.0, 200.0]},
                                "pe_lo_adj": None, "pe_hi_adj": 12.0}, CTX)
    assert clamped == {} and p["driver_center"] == 850.0
    assert p["profit_adj"] == {"2026Q3": [130.0, 200.0]} and p["pe_hi_adj"] == 12.0


def test_clamp_agent_anchor_adj():
    """新模型锚修正: profit_adj={'anchor':[lo,hi]} 钳 [0.7,1.3]×机械锚净利."""
    ctx = {"anchor_profit_mech": 100.0, "pe_p05": 10, "pe_p95": 40,
           "st_center_mech": 50.0}
    # 带内: 原样保留
    p, cl = V.clamp_agent({"profit_adj": {"anchor": [90.0, 120.0]}}, ctx)
    assert p["profit_adj"] == {"anchor": [90.0, 120.0]} and not cl
    # 越界: 钳到 [70, 130]
    p, cl = V.clamp_agent({"profit_adj": {"anchor": [50.0, 200.0]}}, ctx)
    assert p["profit_adj"]["anchor"] == [70.0, 130.0]
    assert "profit_adj.anchor" in cl
    # ctx 无 anchor_profit_mech(如 pb_band): 丢弃并记录
    p, cl = V.clamp_agent({"profit_adj": {"anchor": [90.0, 120.0]}},
                          {"pe_p05": 10, "pe_p95": 40})
    assert p["profit_adj"] is None
    assert "profit_adj.anchor" in cl
    # 商品 ctx(rule_quarters)下季度键行为不变
    ctx_c = {"rule_quarters": {"2026Q3": [100.0, 120.0]}, "pe_p05": 10, "pe_p95": 40}
    p, cl = V.clamp_agent({"profit_adj": {"2026Q3": [110.0, 115.0]}}, ctx_c)
    assert p["profit_adj"] == {"2026Q3": [110.0, 115.0]} and not cl


# ---------- run_daily 测试 (Task 5) ----------

def _seed_market(path, basics, events=()):
    """把构造的 daily_basic/earnings_events 灌进临时库.

    注意: daily_basic/daily 不在 scout_db SCHEMA 里(生产库由选股流水线建),
    临时库要自己建最小结构."""
    c = scout_db.conn(path)
    c.execute("CREATE TABLE IF NOT EXISTS daily_basic (trade_date TEXT, ts_code TEXT, "
              "close REAL, total_mv REAL, PRIMARY KEY (trade_date, ts_code))")
    c.execute("CREATE TABLE IF NOT EXISTS daily (trade_date TEXT, ts_code TEXT, "
              "open REAL, high REAL, low REAL, close REAL, pre_close REAL, "
              "vol REAL, PRIMARY KEY (trade_date, ts_code))")
    for td, close, tmv in basics:
        c.execute("INSERT OR REPLACE INTO daily_basic (trade_date, ts_code, close, total_mv) "
                  "VALUES (?,?,?,?)", (td, "601899.SH", close, tmv))
    for ev in events:
        c.execute("INSERT INTO earnings_events (ts_code, ann_date, source, end_date, "
                  "net_profit_min, net_profit_max, is_hard_hit) VALUES (?,?,?,?,?,?,0)", ev)
    c.commit()
    c.close()


def _fetcher(income_rows, gold_rows):
    def fetch(api_name, params, fields, timeout=60):
        if api_name == "income":
            return income_rows
        if api_name == "sge_daily":
            return gold_rows
        raise AssertionError(api_name)
    return fetch


# 累计口径(元). 早期 4 行保证差分链完整、PIT 序列从窗口起点(2025-09)就有 >=2 已知单季
INCOME_ROWS = [
    {"end_date": "20240630", "ann_date": "20240830", "report_type": "1", "n_income_attr_p": 180e8},
    {"end_date": "20240930", "ann_date": "20241030", "report_type": "1", "n_income_attr_p": 290e8},
    {"end_date": "20241231", "ann_date": "20250322", "report_type": "1", "n_income_attr_p": 400e8},
    {"end_date": "20250331", "ann_date": "20250412", "report_type": "1", "n_income_attr_p": 100e8},
    {"end_date": "20250630", "ann_date": "20250827", "report_type": "1", "n_income_attr_p": 230e8},
    {"end_date": "20250930", "ann_date": "20251018", "report_type": "1", "n_income_attr_p": 380e8},
    {"end_date": "20251231", "ann_date": "20260321", "report_type": "1", "n_income_attr_p": 520e8},
    {"end_date": "20260331", "ann_date": "20260422", "report_type": "1", "n_income_attr_p": 200e8},
]
GOLD_ROWS = [{"trade_date": d, "close": v} for d, v in FLAT_GOLD]

# 深周期塌陷累计口径(元): 2024~2025 年化~8亿, 2026Q1 骤增至 15亿单季
TROUGH_INCOME = [
    {"end_date": "20240630", "ann_date": "20240830", "report_type": "1", "n_income_attr_p": 4e8},
    {"end_date": "20240930", "ann_date": "20241030", "report_type": "1", "n_income_attr_p": 6e8},
    {"end_date": "20241231", "ann_date": "20250322", "report_type": "1", "n_income_attr_p": 8e8},
    {"end_date": "20250331", "ann_date": "20250412", "report_type": "1", "n_income_attr_p": 2e8},
    {"end_date": "20250630", "ann_date": "20250827", "report_type": "1", "n_income_attr_p": 4e8},
    {"end_date": "20250930", "ann_date": "20251018", "report_type": "1", "n_income_attr_p": 6e8},
    {"end_date": "20251231", "ann_date": "20260321", "report_type": "1", "n_income_attr_p": 8e8},
    {"end_date": "20260331", "ann_date": "20260422", "report_type": "1", "n_income_attr_p": 15e8},
]


def _mk_targets(tmp_path, extra=None):
    cfg = {"601899.SH": {"name": "紫金矿业",
                         "driver": {"kind": "sge", "symbol": "Au99.99", "label": "上海金"}}}
    if extra:
        cfg["601899.SH"].update(extra)
    p = tmp_path / "targets.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_run_daily_writes_band_and_quarters(tmp_path):
    path = tmp_db()
    basics = _mk_basics(200)
    _seed_market(path, basics)
    rc = V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS),
                     targets_path=_mk_targets(tmp_path))
    assert rc == 0
    c = scout_db.conn(path)
    band = c.execute("SELECT * FROM valuation_band_daily WHERE ts_code='601899.SH' "
                     "ORDER BY trade_date DESC LIMIT 1").fetchone()
    assert band["trade_date"] == basics[-1][0]
    assert band["driver_source"] == "auto" and band["pe_source"] == "quantile"
    assert band["earnings_source"] == "rule"
    assert 0 < band["price_lo"] < band["price_hi"]
    assert band["pe_lo"] < band["pe_hi"]
    # 年化 = Q1 实际 200 + Q2/Q3/Q4 规则区间 (g=1): lo=200+3*126=578, hi=200+3*220=860
    assert (band["profit_year_lo"], band["profit_year_hi"]) == (578.0, 860.0)
    meta = json.loads(band["meta_json"])
    assert meta["clamp_ctx"]["pe_p05"] and meta["clamp_ctx"]["rule_quarters"]
    qs = {r["quarter"]: r for r in c.execute(
        "SELECT * FROM valuation_quarter_est WHERE ts_code='601899.SH'")}
    assert qs["2026Q1"]["status"] == "actual" and qs["2026Q1"]["profit_lo"] == 200.0
    assert qs["2026Q3"]["status"] == "estimated"
    assert qs["2025Q4"]["status"] == "actual"      # 历史季也留档
    c.close()
    os.unlink(path)


def test_run_daily_price_band_single_anchor(tmp_path):
    """长期价格带用锚中点(单锚), 不再 pe_lo×eps_lo / pe_hi×eps_hi 外积复合宽度."""
    path = tmp_db()
    _seed_market(path, _mk_basics(200))
    rc = V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS),
                     targets_path=_mk_targets(tmp_path))
    assert rc == 0
    c = scout_db.conn(path)
    b = dict(c.execute("SELECT * FROM valuation_band_daily WHERE ts_code='601899.SH' "
                       "ORDER BY trade_date DESC LIMIT 1").fetchone())
    assert b["eps_lo"] < b["eps_hi"]                       # 商品模型锚是区间
    anchor_mid = (b["eps_lo"] + b["eps_hi"]) / 2
    assert abs(b["price_lo"] - round(b["pe_lo"] * anchor_mid, 2)) < 0.05
    assert abs(b["price_hi"] - round(b["pe_hi"] * anchor_mid, 2)) < 0.05
    # 旧外积会把上沿抬到 pe_hi×eps_hi, 单锚下应显著更低
    assert b["price_hi"] < round(b["pe_hi"] * b["eps_hi"], 2)
    c.close()
    os.unlink(path)


def test_run_daily_regime_min_history_band(tmp_path):
    """深周期短史: 塌陷年畸高PE被regime过滤, 可比样本<MIN_HISTORY但>=REGIME_MIN仍出带."""
    path = tmp_db()
    _seed_market(path, _mk_basics(240))
    rc = V.run_daily(path, fetch=_fetcher(TROUGH_INCOME, GOLD_ROWS),
                     targets_path=_mk_targets(tmp_path))
    assert rc == 0
    c = scout_db.conn(path)
    b = dict(c.execute("SELECT pe_hi, meta_json FROM valuation_band_daily "
                       "WHERE ts_code='601899.SH' ORDER BY trade_date DESC LIMIT 1").fetchone())
    meta = json.loads(b["meta_json"])
    # 可比regime样本数落在 [REGIME_MIN_HISTORY, MIN_HISTORY) 区间(旧代码会因<120抛错)
    assert V.REGIME_MIN_HISTORY <= meta["pe_n"] < V.MIN_HISTORY
    # 塌陷日剔除后 PE 上界回到健康regime(混入塌陷日会畸高)
    assert b["pe_hi"] < 20
    c.close()
    os.unlink(path)


def test_run_daily_agent_and_manual_priority(tmp_path):
    path = tmp_db()
    basics = _mk_basics(200)
    _seed_market(path, basics)
    today = basics[-1][0]
    c = scout_db.conn(path)
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, driver_center, "
              "driver_low, driver_high, profit_adj_json, pe_lo_adj, pe_hi_adj, "
              "confidence, rationale, valid_until, trigger) VALUES "
              "('2026-07-01 22:00:00','601899.SH',850.0,820.0,880.0,"
              "'{\"2026Q3\": [180.0, 210.0]}',NULL,12.5,0.8,'测试',?,'weekly')", (today,))
    c.commit()
    c.close()
    # agent 生效: driver=agent, Q3 用 agent 区间, pe_hi 用 agent 值
    rc = V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS),
                     targets_path=_mk_targets(tmp_path))
    assert rc == 0
    c = scout_db.conn(path)
    band = c.execute("SELECT * FROM valuation_band_daily ORDER BY trade_date DESC "
                     "LIMIT 1").fetchone()
    assert band["driver_source"] == "agent" and band["driver_center"] == 850.0
    assert band["pe_source"] == "agent" and band["pe_hi"] == 12.5
    assert band["earnings_source"] == "agent"
    qs = {r["quarter"]: r for r in c.execute(
        "SELECT * FROM valuation_quarter_est WHERE status='estimated'")}
    assert (qs["2026Q3"]["profit_lo"], qs["2026Q3"]["profit_hi"]) == (180.0, 210.0)
    c.close()
    # 手动覆盖 > agent
    rc = V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS),
                     targets_path=_mk_targets(tmp_path, {
                         "driver_override": {"center": 800.0, "low": 780.0, "high": 820.0}}))
    assert rc == 0
    c = scout_db.conn(path)
    band = c.execute("SELECT * FROM valuation_band_daily ORDER BY trade_date DESC "
                     "LIMIT 1").fetchone()
    assert band["driver_source"] == "manual" and band["driver_center"] == 800.0
    c.close()
    os.unlink(path)


def test_run_daily_expired_agent_falls_back(tmp_path):
    path = tmp_db()
    basics = _mk_basics(200)
    _seed_market(path, basics)
    today = basics[-1][0]
    c = scout_db.conn(path)
    # valid_until 早于 today, agent 过期
    yesterday = (datetime.strptime(today, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, driver_center, "
              "confidence, rationale, valid_until, trigger) VALUES "
              "(?,?,?,?,?,?,?)",
              ('2026-06-01 22:00:00', '601899.SH', 850.0, 0.8, '过期', yesterday, 'weekly'))
    c.commit()
    c.close()
    V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS),
                targets_path=_mk_targets(tmp_path))
    c = scout_db.conn(path)
    band = c.execute("SELECT driver_source FROM valuation_band_daily "
                     "ORDER BY trade_date DESC LIMIT 1").fetchone()
    assert band["driver_source"] == "auto"     # 过期回落机械
    c.close()
    os.unlink(path)


def test_run_daily_insufficient_history(tmp_path):
    path = tmp_db()
    _seed_market(path, _mk_basics(50))         # < MIN_HISTORY
    rc = V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS),
                     targets_path=_mk_targets(tmp_path))
    assert rc == 1
    c = scout_db.conn(path)
    n = c.execute("SELECT COUNT(*) FROM valuation_band_daily").fetchone()[0]
    assert n == 0                              # 不出带, 也无旧行可 stale
    c.close()
    os.unlink(path)


def test_run_daily_stale_on_fetch_error(tmp_path):
    path = tmp_db()
    basics = _mk_basics(200)
    _seed_market(path, basics)
    tp = _mk_targets(tmp_path)
    V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS), targets_path=tp)
    # 追加一天行情后, 取数挂掉 -> 复制昨日带子并标 stale
    last = datetime.strptime(basics[-1][0], "%Y%m%d")
    nxt = last + timedelta(days=3 if last.weekday() == 4 else 1)
    _seed_market(path, [(nxt.strftime("%Y%m%d"), 21.0, 4_200_000.0)])

    def boom(api_name, params, fields, timeout=60):
        raise RuntimeError("网络挂了")
    rc = V.run_daily(path, fetch=boom, targets_path=tp)
    assert rc == 1
    c = scout_db.conn(path)
    band = c.execute("SELECT * FROM valuation_band_daily ORDER BY trade_date DESC "
                     "LIMIT 1").fetchone()
    assert band["trade_date"] == nxt.strftime("%Y%m%d")
    assert "stale" in json.loads(band["meta_json"])
    assert band["st_center"] is not None            # stale 复制也要带上 st 列
    c.close()
    os.unlink(path)


# ---------- 二期: 短期做T带 + 模型分发 ----------

def test_load_targets_model(tmp_path):
    mk = lambda extra: json.dumps({"601899.SH": {
        "name": "紫金矿业", "driver": {"kind": "sge", "symbol": "Au99.99"}, **extra}},
        ensure_ascii=False)
    p = tmp_path / "t.json"
    p.write_text(mk({"model": {"kind": "commodity_pe", "brief": "金铜双驱动"}}),
                 encoding="utf-8")
    t = V.load_targets(str(p))["601899.SH"]
    assert t["model"] == {"kind": "commodity_pe", "brief": "金铜双驱动"}
    p.write_text(mk({}), encoding="utf-8")          # 缺省补 commodity_pe
    assert V.load_targets(str(p))["601899.SH"]["model"] == \
        {"kind": "commodity_pe", "brief": ""}
    p.write_text(mk({"model": {"kind": "pb_roe"}}), encoding="utf-8")
    with pytest.raises(ValueError):                  # 未实现模型直接报错
        V.load_targets(str(p))


def test_agent_view_split_excludes_daily_st():
    path = tmp_db()
    c = scout_db.conn(path)
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, driver_center, "
              "rationale, valid_until, trigger) VALUES "
              "('2026-07-01 22:00:00','601899.SH',850.0,'深度','20991231','weekly')")
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, st_center_adj, "
              "st_width_pct, rationale, valid_until, trigger) VALUES "
              "('2026-07-09 22:20:00','601899.SH',28.5,0.06,'轻量','20991231','daily_st')")
    c.commit()
    deep = V.latest_agent_view(c, "601899.SH", "20260710")
    assert deep["trigger"] == "weekly" and deep["driver_center"] == 850.0
    stv = V.latest_st_view(c, "601899.SH", "20260710")
    assert stv["trigger"] == "daily_st" and stv["st_center_adj"] == 28.5
    assert V.latest_st_view(c, "601899.SH", "20991232") is None   # 过期
    c.close()
    os.unlink(path)


def test_clamp_agent_st():
    ctx = {**CTX, "st_center_mech": 30.0}
    p, cl = V.clamp_agent({"st_center_adj": 40.0, "st_width_pct": 0.30}, ctx)
    assert p["st_center_adj"] == round(30.0 * 1.08, 2)   # 32.4
    assert p["st_width_pct"] == 0.15
    assert {"st_center_adj", "st_width_pct"} <= set(cl)
    p2, cl2 = V.clamp_agent({"st_center_adj": 29.0, "st_width_pct": 0.06}, ctx)
    assert cl2 == {} and p2["st_center_adj"] == 29.0 and p2["st_width_pct"] == 0.06
    p3, cl3 = V.clamp_agent({"st_center_adj": 29.0}, CTX)   # 老 ctx 无 st 基准
    assert p3["st_center_adj"] is None and "st_center_adj" in cl3


def test_run_daily_st_band_mech(tmp_path):
    path = tmp_db()
    basics = _mk_basics(200)
    _seed_market(path, basics)
    rc = V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS),
                     targets_path=_mk_targets(tmp_path))
    assert rc == 0
    c = scout_db.conn(path)
    band = c.execute("SELECT * FROM valuation_band_daily "
                     "ORDER BY trade_date DESC LIMIT 1").fetchone()
    assert band["st_source"] == "auto"
    assert 0 < band["st_lo"] < band["st_center"] < band["st_hi"]
    # 短期带必然窄于长期带
    assert band["st_hi"] - band["st_lo"] < band["price_hi"] - band["price_lo"]
    meta = json.loads(band["meta_json"])
    assert meta["st"]["center_mech"] == band["st_center"]
    assert meta["clamp_ctx"]["st_center_mech"] == band["st_center"]
    assert V.CLAMP_ST_WIDTH_LO <= meta["st"]["width_mech"] <= V.CLAMP_ST_WIDTH_HI
    c.close()
    os.unlink(path)


def test_run_daily_st_agent_effective(tmp_path):
    """daily_st 轻量行只影响短期带, 不得污染长期锚点."""
    path = tmp_db()
    basics = _mk_basics(200)
    _seed_market(path, basics)
    today = basics[-1][0]
    c = scout_db.conn(path)
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, st_width_pct, "
              "rationale, valid_until, trigger) VALUES "
              "('2026-07-09 22:20:00','601899.SH',0.05,'情绪平静收窄',?,'daily_st')",
              (today,))
    c.commit()
    c.close()
    rc = V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS),
                     targets_path=_mk_targets(tmp_path))
    assert rc == 0
    c = scout_db.conn(path)
    band = c.execute("SELECT * FROM valuation_band_daily "
                     "ORDER BY trade_date DESC LIMIT 1").fetchone()
    assert band["st_source"] == "agent"
    meta = json.loads(band["meta_json"])
    center = meta["st"]["center_mech"]               # 中枢未修 -> 沿用机械
    assert band["st_center"] == center
    assert band["st_lo"] == round(center * 0.95, 2)  # 半宽 0.05 生效
    assert band["st_hi"] == round(center * 1.05, 2)
    assert band["driver_source"] == "auto"           # 长期锚点不受 daily_st 行影响
    assert band["pe_source"] == "quantile"
    c.close()
    os.unlink(path)


def test_api_targets_and_band(tmp_path):
    path = tmp_db()
    basics = _mk_basics(200)
    _seed_market(path, basics)
    tp = _mk_targets(tmp_path)
    V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS), targets_path=tp)
    # daily K 线数据
    c = scout_db.conn(path)
    for td, close, _ in basics[-30:]:
        c.execute("INSERT OR REPLACE INTO daily (trade_date, ts_code, open, high, "
                  "low, close) VALUES (?,?,?,?,?,?)",
                  (td, "601899.SH", close * 0.99, close * 1.01, close * 0.98, close))
    c.commit(); c.close()

    t = V.api_targets(db_path=path, targets_path=tp)
    assert t["targets"][0]["ts_code"] == "601899.SH"
    assert t["targets"][0]["price_lo"] > 0 and "band_pos" in t["targets"][0]
    assert t["targets"][0]["st_lo"] is not None and t["targets"][0]["st_source"] == "auto"
    assert t["targets"][0]["enabled"] is True

    b = V.api_band("601899.SH", days=100, db_path=path, targets_path=tp)
    assert b["series"] and b["series"][-1]["trade_date"] == basics[-1][0]
    assert "st_lo" in b["series"][-1] and b["series"][-1]["st_lo"] is not None
    assert b["latest"]["st_center"] is not None
    assert b["latest"]["meta"]["clamp_ctx"]
    assert len(b["kline"]) == 30 and b["kline"][0]["trade_date"] < b["kline"][-1]["trade_date"]
    assert b["quarters"] and b["agent"] is None
    assert "error" in V.api_band("999999.SH", db_path=path, targets_path=tp)

    a = V.api_agent("601899.SH", db_path=path)
    assert a["deep"] == [] and a["daily"] == []
    os.unlink(path)


def test_api_band_agent_excludes_daily_st(tmp_path):
    """api_band 的 agent 卡取深度观点, 不被每日轻量行顶替."""
    path = tmp_db()
    basics = _mk_basics(200)
    _seed_market(path, basics)
    tp = _mk_targets(tmp_path)
    V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS), targets_path=tp)
    c = scout_db.conn(path)
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, driver_center, "
              "rationale, valid_until, trigger) VALUES "
              "('2026-07-01 22:00:00','601899.SH',850.0,'深度','20991231','weekly')")
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, st_width_pct, "
              "rationale, valid_until, trigger) VALUES "
              "('2026-07-09 22:20:00','601899.SH',0.05,'轻量','20991231','daily_st')")
    c.commit(); c.close()
    b = V.api_band("601899.SH", db_path=path, targets_path=tp)
    assert b["agent"]["trigger"] == "weekly"
    os.unlink(path)


# ---------- 二期: 技术面事实 ----------

def _seed_daily_tech(path, ts_code="601899.SH"):
    """60 天构造行情: 前 59 天收盘恒 20, 最后一天 22; 全程 high=close+0.4/low=close-0.4,
    pre_close 恒 20; 前 55 天 vol=100, 后 5 天 vol=200. 各指标可手算."""
    c = scout_db.conn(path)
    c.execute("CREATE TABLE IF NOT EXISTS daily (trade_date TEXT, ts_code TEXT, "
              "open REAL, high REAL, low REAL, close REAL, pre_close REAL, "
              "vol REAL, PRIMARY KEY (trade_date, ts_code))")
    d = datetime(2026, 4, 1)
    rows = []
    for i in range(60):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        close = 22.0 if i == 59 else 20.0
        rows.append((d.strftime("%Y%m%d"), ts_code, close, close + 0.4, close - 0.4,
                     close, 20.0, 200.0 if i >= 55 else 100.0))
        d += timedelta(days=1)
    c.executemany("INSERT OR REPLACE INTO daily (trade_date, ts_code, open, high, "
                  "low, close, pre_close, vol) VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return rows[-1][0]      # asof


def test_tech_facts_values():
    path = tmp_db()
    asof = _seed_daily_tech(path)
    c = scout_db.conn(path)
    t = V.tech_facts(c, "601899.SH", asof)
    assert t["close"] == 22.0
    assert t["ma5"] == round((20.0 * 4 + 22.0) / 5, 2)          # 20.4
    assert t["ma20"] == round((20.0 * 19 + 22.0) / 20, 2)       # 20.1
    assert t["ma60"] == round((20.0 * 59 + 22.0) / 60, 2)
    assert t["bias20"] == round(22.0 / t["ma20"] - 1, 4)
    assert t["ret5"] == round(22.0 / 20.0 - 1, 4)               # 0.1
    # 20日高低位: lo20=19.6, hi20=22.4 -> (22-19.6)/2.8
    assert t["pos20"] == round(2.4 / 2.8, 4)
    assert t["dist_hi60"] == round(22.0 / 22.4 - 1, 4)
    assert t["vol_ratio5_20"] == round(200.0 / 125.0, 3)        # 1.6
    assert t["amp5_avg"] == round(0.8 / 20.0, 4)                # 0.04
    c.close()
    os.unlink(path)


def test_tech_facts_missing_or_thin():
    path = tmp_db()
    c = scout_db.conn(path)
    assert V.tech_facts(c, "601899.SH", "20260710") is None     # 无 daily 表
    c.execute("CREATE TABLE daily (trade_date TEXT, ts_code TEXT, open REAL, "
              "high REAL, low REAL, close REAL, pre_close REAL, vol REAL, "
              "PRIMARY KEY (trade_date, ts_code))")
    c.execute("INSERT INTO daily VALUES ('20260710','601899.SH',20,20.4,19.6,20,20,100)")
    c.commit()
    assert V.tech_facts(c, "601899.SH", "20260710") is None     # 样本 < 20
    c.close()
    os.unlink(path)


def test_tech_facts_vol_ratio_null_safe():
    """中间有 NULL vol 行时, 量比仍按最近窗口就地过滤, 不错位混入更早日期."""
    path = tmp_db()
    asof = _seed_daily_tech(path)
    c = scout_db.conn(path)
    # 把最近第 3 天的 vol 置 NULL: 最近5日有效量 = [200,200,200,200] 均值仍 200
    d3 = c.execute("SELECT trade_date FROM daily ORDER BY trade_date DESC "
                   "LIMIT 1 OFFSET 2").fetchone()[0]
    c.execute("UPDATE daily SET vol=NULL WHERE trade_date=?", (d3,))
    c.commit()
    t = V.tech_facts(c, "601899.SH", asof)
    # 近20日: 15×100 + 4×200(1条NULL被剔) -> 均值 2300/19
    assert t["vol_ratio5_20"] == round(200.0 / (2300.0 / 19), 3)
    c.close()
    os.unlink(path)


def test_run_daily_meta_tech(tmp_path):
    path = tmp_db()
    basics = _mk_basics(200)
    _seed_market(path, basics)
    c = scout_db.conn(path)
    for td, close, _ in basics[-30:]:
        c.execute("INSERT OR REPLACE INTO daily (trade_date, ts_code, open, high, "
                  "low, close, pre_close, vol) VALUES (?,?,?,?,?,?,?,?)",
                  (td, "601899.SH", close * 0.99, close * 1.01, close * 0.98,
                   close, close, 100.0))
    c.commit()
    c.close()
    V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS),
                targets_path=_mk_targets(tmp_path))
    c = scout_db.conn(path)
    meta = json.loads(c.execute("SELECT meta_json FROM valuation_band_daily "
                                "ORDER BY trade_date DESC LIMIT 1").fetchone()[0])
    assert meta["tech"] and meta["tech"]["ma20"] is not None
    c.close()
    os.unlink(path)


# ---------- 三期: enabled 停用 + api_agent 分轨 ----------

def test_load_targets_enabled_normalization(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({
        "601899.SH": {"name": "紫金矿业",
                      "driver": {"kind": "sge", "symbol": "Au99.99"}},
        "600111.SH": {"name": "北方稀土", "enabled": False,
                      "driver": {"kind": "sge", "symbol": "Au99.99"}},
    }), encoding="utf-8")
    t = V.load_targets(str(p))
    assert t["601899.SH"]["enabled"] is True      # 缺省启用
    assert t["600111.SH"]["enabled"] is False


def test_run_daily_skips_disabled(tmp_path):
    path = tmp_db()
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"601899.SH": {
        "name": "紫金矿业", "enabled": False,
        "driver": {"kind": "sge", "symbol": "Au99.99"}}}), encoding="utf-8")
    calls = []
    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("停用标的不该取数")
    rc = V.run_daily(db_path=path, fetch=boom, targets_path=str(p))
    assert rc == 0 and not calls
    os.unlink(path)


def test_api_agent_split_tracks():
    path = tmp_db()
    c = scout_db.conn(path)
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, rationale, "
              "valid_until, trigger) VALUES "
              "('2026-07-01 22:00:00','601899.SH','深度','20991231','weekly')")
    for d in range(2, 9):
        c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, rationale, "
                  "valid_until, trigger, st_width_pct) VALUES (?,?,?,?,?,?)",
                  (f"2026-07-0{d} 22:20:00", "601899.SH", "轻量", "20991231",
                   "daily_st", 0.05))
    c.commit(); c.close()
    a = V.api_agent("601899.SH", db_path=path)
    assert len(a["deep"]) == 1 and a["deep"][0]["trigger"] == "weekly"
    assert len(a["daily"]) == 7
    assert a["daily"][0]["run_at"] > a["daily"][-1]["run_at"]   # 新在前
    assert "rows" not in a
    os.unlink(path)


def test_api_band_st_agent(tmp_path):
    """api_band.st_agent: 最新 daily_st 行或含 st 修正的深度行; 维持机械值也返回."""
    path = tmp_db()
    basics = _mk_basics(200)
    _seed_market(path, basics)
    tp = _mk_targets(tmp_path)
    V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS), targets_path=tp)

    # 无任何行 -> None
    b = V.api_band("601899.SH", db_path=path, targets_path=tp)
    assert b["st_agent"] is None

    c = scout_db.conn(path)
    # 含 st 修正的深度行命中; 更晚的"无 st 修正"深度行不顶替
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, st_width_pct, "
              "rationale, valid_until, trigger) VALUES "
              "('2026-07-01 22:00:00','601899.SH',0.05,'深度含st','20991231','weekly')")
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, driver_center, "
              "rationale, valid_until, trigger) VALUES "
              "('2026-07-02 22:00:00','601899.SH',850.0,'深度无st','20991231','weekly')")
    c.commit()
    b = V.api_band("601899.SH", db_path=path, targets_path=tp)
    assert b["st_agent"]["rationale"] == "深度含st"
    assert b["st_agent"]["active"] is True

    # 更晚的 daily_st 行(维持机械值, st 双 null)顶替 —— 区别于 latest_st_view
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, rationale, "
              "valid_until, trigger) VALUES (?,?,?,?,?)",
              ("2026-07-03 22:20:00", "601899.SH",
               "技术面: 贴MA20\n情绪面: 平稳\n结论: 维持机械值", "20991231", "daily_st"))
    c.commit()
    b = V.api_band("601899.SH", db_path=path, targets_path=tp)
    assert b["st_agent"]["trigger"] == "daily_st"
    assert b["st_agent"]["st_center_adj"] is None

    # 过期 -> 仍返回但 active=False
    c.execute("UPDATE valuation_agent_view SET valid_until='20200101' "
              "WHERE trigger='daily_st'")
    c.commit()
    b = V.api_band("601899.SH", db_path=path, targets_path=tp)
    assert b["st_agent"]["trigger"] == "daily_st"
    assert b["st_agent"]["active"] is False

    # 有修正的 daily_st 行 -> 返回且 active=True
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, st_center_adj, "
              "rationale, valid_until, trigger) VALUES "
              "('2026-07-04 22:20:00','601899.SH',21.0,'技术面: a\\n情绪面: b\\n结论: 上修中枢',"
              "'20991231','daily_st')")
    c.commit(); c.close()
    b = V.api_band("601899.SH", db_path=path, targets_path=tp)
    assert b["st_agent"]["st_center_adj"] == 21.0
    assert b["st_agent"]["active"] is True
    os.unlink(path)


# ---------- 四期: 加权商品篮子驱动(basket) ----------

def _drange(start: str, end: str, price):
    """日历日粒度序列 [(yyyymmdd, price)], price 可为常数或 f(yyyymmdd)->float."""
    d0, d1 = datetime.strptime(start, "%Y%m%d"), datetime.strptime(end, "%Y%m%d")
    out = []
    while d0 <= d1:
        ds = d0.strftime("%Y%m%d")
        out.append((ds, price(ds) if callable(price) else float(price)))
        d0 += timedelta(days=1)
    return out


def _mk_basket_driver(weights=(0.6, 0.4)):
    return {"kind": "basket", "label": "铜金篮子(指数, 基期=100)",
            "components": [
                {"kind": "fut", "symbol": "CU.SHF", "label": "沪铜主力(元/吨)",
                 "weight": weights[0]},
                {"kind": "sge", "symbol": "Au99.99", "label": "上海金(元/克)",
                 "weight": weights[1]},
            ]}


def _fetcher_basket(income_rows, series_by_symbol):
    """按 (api, params.ts_code) 分发多序列的 mock fetch."""
    def fetch(api_name, params, fields, timeout=60):
        if api_name == "income":
            return income_rows
        if api_name in ("sge_daily", "fut_daily"):
            rows = series_by_symbol[params["ts_code"]]
            return [{"trade_date": d, "close": v} for d, v in rows]
        raise AssertionError(api_name)
    return fetch


def test_validate_driver_basket():
    # 合法 + 容差内(和1.005)通过且内存归一化
    d = V.validate_driver("X", _mk_basket_driver((0.603, 0.402)))
    assert abs(sum(c["weight"] for c in d["components"]) - 1) < 1e-9
    # 权重和 0.98 / 1.02 超容差
    for w in ((0.6, 0.38), (0.6, 0.42)):
        with pytest.raises(ValueError):
            V.validate_driver("X", _mk_basket_driver(w))
    # 成分数 1 / 5
    bad = _mk_basket_driver()
    bad["components"] = bad["components"][:1]
    with pytest.raises(ValueError):
        V.validate_driver("X", bad)
    bad = _mk_basket_driver()
    bad["components"] = [dict(bad["components"][0], symbol=f"S{i}", weight=0.2)
                         for i in range(5)]
    with pytest.raises(ValueError):
        V.validate_driver("X", bad)
    # 嵌套 basket / 缺 label / 重复 symbol / 缺顶层 label
    for mutate in (lambda b: b["components"][0].update(kind="basket"),
                   lambda b: b["components"][0].update(label=""),
                   lambda b: b["components"][1].update(symbol="CU.SHF"),
                   lambda b: b.update(label="")):
        b = _mk_basket_driver()
        mutate(b)
        with pytest.raises(ValueError):
            V.validate_driver("X", b)
    # 单 driver: 原样透传; 缺 symbol 报错
    sd = {"kind": "sge", "symbol": "Au99.99"}
    assert V.validate_driver("X", sd) is sd
    with pytest.raises(ValueError):
        V.validate_driver("X", {"kind": "sge"})
    with pytest.raises(ValueError):
        V.validate_driver("X", {"kind": "etf", "symbol": "x"})


def test_build_basket_series_base100():
    sbs = {"CU.SHF": _drange("20260101", "20260331", 70000.0),
           "Au99.99": _drange("20260101", "20260331", 900.0)}
    idx, meta = V.build_driver_series(_mk_basket_driver(), "20260101", "20260331",
                                      _fetcher_basket([], sbs))
    assert all(v == 100.0 for _, v in idx)
    assert meta["base_start"] == "20260101" and meta["base_days"] == 20
    comps = {c["symbol"]: c for c in meta["components"]}
    assert comps["CU.SHF"]["base"] == 70000.0 and comps["Au99.99"]["base"] == 900.0
    assert comps["CU.SHF"]["weight"] == 0.6
    # 单 driver 透传: basket_meta 为 None
    s, m = V.build_driver_series({"kind": "sge", "symbol": "Au99.99"},
                                 "20260101", "20260331", _fetcher_basket([], sbs))
    assert m is None and len(s) == len(sbs["Au99.99"])


def test_build_basket_intersection():
    cu = _drange("20260101", "20260430", 70000.0)
    au_late = _drange("20260201", "20260430", 900.0)      # 晚上市
    au_holes = [x for x in _drange("20260101", "20260430", 900.0)
                if not x[0].endswith("5")]                 # 缺日
    # 缺日 -> 只留交集
    idx, _ = V.build_driver_series(
        _mk_basket_driver(), "20260101", "20260430",
        _fetcher_basket([], {"CU.SHF": cu, "Au99.99": au_holes}))
    assert {d for d, _ in idx} == {d for d, _ in au_holes}
    # 晚上市 -> 序列起点与基期窗随之后移
    idx, meta = V.build_driver_series(
        _mk_basket_driver(), "20260101", "20260430",
        _fetcher_basket([], {"CU.SHF": cu, "Au99.99": au_late}))
    assert idx[0][0] == "20260201" and meta["base_start"] == "20260201"
    # 交集 < BASKET_MIN_DAYS -> RuntimeError
    with pytest.raises(RuntimeError):
        V.build_driver_series(
            _mk_basket_driver(), "20260101", "20260430",
            _fetcher_basket([], {"CU.SHF": cu,
                                 "Au99.99": _drange("20260401", "20260430", 900.0)}))
    # 成分序列为空 -> RuntimeError
    with pytest.raises(RuntimeError):
        V.build_driver_series(
            _mk_basket_driver(), "20260101", "20260430",
            _fetcher_basket([], {"CU.SHF": cu, "Au99.99": []}))


def test_build_basket_weighting():
    # A 末日 +10%, B 持平, w=.5/.5 -> 指数末值 105.0
    a = _drange("20260101", "20260330", 100.0) + [("20260331", 110.0)]
    b = _drange("20260101", "20260331", 200.0)
    idx, _ = V.build_driver_series(
        _mk_basket_driver((0.5, 0.5)), "20260101", "20260331",
        _fetcher_basket([], {"CU.SHF": a, "Au99.99": b}))
    assert idx[-1] == ("20260331", 105.0)
    assert idx[-2][1] == 100.0


def test_basket_g_factor():
    # 参照两季(2025Q4/2026Q1)指数均值=100, asof 前20日指数=105 -> g=1.05
    a = _drange("20250101", "20260630", 800.0) + _drange("20260701", "20260720", 880.0)
    b = _drange("20250101", "20260720", 900.0)
    idx, _ = V.build_driver_series(
        _mk_basket_driver((0.5, 0.5)), "20250101", "20260720",
        _fetcher_basket([], {"CU.SHF": a, "Au99.99": b}))
    assert V.quarter_avg(idx, "2025Q4") == 100.0
    assert V.quarter_avg(idx, "2026Q1") == 100.0
    lo, hi, basis = V.rule_estimate(SINGLES, idx, "20260720", 0.10)
    assert basis["g"] == 1.05
    assert hi == round(200.0 * 1.05 * 1.1, 2)


def test_run_daily_basket_end_to_end(tmp_path):
    path = tmp_db()
    basics = _mk_basics(200)
    _seed_market(path, basics)
    cfg = {"601899.SH": {"name": "紫金矿业", "driver": _mk_basket_driver()}}
    tp = tmp_path / "targets.json"
    tp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    sbs = {"CU.SHF": _drange("20250101", "20260720", 70000.0),
           "Au99.99": _drange("20250101", "20260720", 800.0)}
    rc = V.run_daily(path, fetch=_fetcher_basket(INCOME_ROWS, sbs),
                     targets_path=str(tp))
    assert rc == 0
    c = scout_db.conn(path)
    band = c.execute("SELECT * FROM valuation_band_daily WHERE ts_code='601899.SH' "
                     "ORDER BY trade_date DESC LIMIT 1").fetchone()
    assert band["trade_date"] == basics[-1][0]
    assert band["driver_center"] == 100.0          # 指数量纲
    assert band["driver_source"] == "auto"
    assert 0 < band["price_lo"] < band["price_hi"]
    meta = json.loads(band["meta_json"])
    bk = meta["driver"]["basket"]
    assert bk["base_days"] == 20 and len(bk["components"]) == 2
    for comp in bk["components"]:
        for k in ("weight", "base", "close", "center20", "avg5"):
            assert comp[k] is not None
    assert {c_["symbol"] for c_ in bk["components"]} == {"CU.SHF", "Au99.99"}
    # clamp_ctx 的 120 日极值也是指数值 -> 钳制量纲自动正确
    assert meta["clamp_ctx"]["driver_min120"] == 100.0
    assert meta["clamp_ctx"]["driver_max120"] == 100.0
    c.close()
    os.unlink(path)


def test_band_meta_has_model_kind(tmp_path):
    """引擎抽取后 meta_json 必须带 model_kind 自描述(页面/agent 分支渲染依据)."""
    path = tmp_db()
    _seed_market(path, _mk_basics(200))
    V.run_daily(path, fetch=_fetcher(INCOME_ROWS, GOLD_ROWS),
                targets_path=_mk_targets(tmp_path))
    c = scout_db.conn(path)
    row = c.execute("SELECT meta_json FROM valuation_band_daily "
                    "ORDER BY trade_date DESC LIMIT 1").fetchone()
    meta = json.loads(row["meta_json"])
    assert meta["model_kind"] == "commodity_pe"
    assert "driver" in meta and "clamp_ctx" in meta   # 商品专属键仍在
    c.close()
    os.unlink(path)


def test_run_daily_basket_component_fetch_fail(tmp_path):
    path = tmp_db()
    basics = _mk_basics(200)
    _seed_market(path, basics)
    cfg = {"601899.SH": {"name": "紫金矿业", "driver": _mk_basket_driver()}}
    tp = tmp_path / "targets.json"
    tp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    sbs = {"CU.SHF": _drange("20250101", "20260720", 70000.0),
           "Au99.99": _drange("20250101", "20260720", 800.0)}
    V.run_daily(path, fetch=_fetcher_basket(INCOME_ROWS, sbs), targets_path=str(tp))
    # 追加一天行情, 单成分序列变空 -> 整带 stale(不静默丢成分)
    last = datetime.strptime(basics[-1][0], "%Y%m%d")
    nxt = last + timedelta(days=3 if last.weekday() == 4 else 1)
    _seed_market(path, [(nxt.strftime("%Y%m%d"), 21.0, 4_200_000.0)])
    rc = V.run_daily(path, fetch=_fetcher_basket(INCOME_ROWS, {**sbs, "Au99.99": []}),
                     targets_path=str(tp))
    assert rc == 1
    c = scout_db.conn(path)
    band = c.execute("SELECT * FROM valuation_band_daily ORDER BY trade_date DESC "
                     "LIMIT 1").fetchone()
    assert band["trade_date"] == nxt.strftime("%Y%m%d")
    assert "stale" in json.loads(band["meta_json"])
    c.close()
    os.unlink(path)


def test_clamp_agent_basket_dims():
    # 指数量纲下钳制照旧: [95×0.9, 108×1.1]=[85.5, 118.8]
    ctx = {"driver_min120": 95.0, "driver_max120": 108.0}
    p, clamped = V.clamp_agent({"driver_center": 130.0, "driver_low": 90.0,
                                "driver_high": 140.0}, ctx)
    assert p["driver_center"] == 118.8 and "driver_center" in clamped
    assert p["driver_low"] == 90.0                  # 界内不动
    assert p["driver_high"] == 118.8


def test_load_targets_basket(tmp_path):
    cfg = {"601899.SH": {"name": "紫金矿业", "driver": _mk_basket_driver()}}
    p = tmp_path / "t.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    t = V.load_targets(str(p))["601899.SH"]
    assert t["driver"]["kind"] == "basket"
    assert abs(sum(c["weight"] for c in t["driver"]["components"]) - 1) < 1e-9
    # 坏权重直接在 load 时报错
    cfg["601899.SH"]["driver"]["components"][0]["weight"] = 0.9
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        V.load_targets(str(p))


def test_load_targets_driver_by_kind(tmp_path, monkeypatch):
    """driver 从全局必填改为 commodity_pe 专属; 其他 kind 禁带 driver."""
    monkeypatch.setitem(V.MODEL_RUNNERS, "pe_band", lambda *a: None)  # 先占位注册
    p = tmp_path / "t.json"
    # 1) pe_band 无 driver 合法, driver 归一化为 None
    p.write_text(json.dumps({"600519.SH": {
        "name": "贵州茅台", "model": {"kind": "pe_band", "brief": "x"}}}),
        encoding="utf-8")
    t = V.load_targets(str(p))["600519.SH"]
    assert t["driver"] is None and t["model"]["kind"] == "pe_band"
    # 2) commodity_pe 缺 driver 报错
    p.write_text(json.dumps({"601899.SH": {"name": "紫金矿业"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="commodity_pe 必须配置 driver"):
        V.load_targets(str(p))
    # 3) 非商品 kind 带 driver 报错(防语义混淆)
    p.write_text(json.dumps({"600519.SH": {
        "name": "贵州茅台", "model": {"kind": "pe_band", "brief": "x"},
        "driver": {"kind": "fut", "symbol": "CU.SHF"}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="不接受 driver"):
        V.load_targets(str(p))


def test_api_targets_none_driver(tmp_path, monkeypatch):
    """api_targets 对 driver=None 的标的 driver_label 回空串不炸."""
    monkeypatch.setitem(V.MODEL_RUNNERS, "pe_band", lambda *a: None)
    path = tmp_db()
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"600519.SH": {
        "name": "贵州茅台", "model": {"kind": "pe_band", "brief": "x"}}}),
        encoding="utf-8")
    out = V.api_targets(db_path=path, targets_path=str(p))
    assert out["targets"][0]["driver_label"] == ""
    os.unlink(path)


# ---------- Task 5: pe_band 白马PE-TTM分位带 ----------

def _seed_daily_basic(c, ts_code, n=300, close=100.0, mv=1e7,
                      pe_fn=lambda i: 15 + (i % 100) * 0.1,
                      pb_fn=lambda i: 1.0 + (i % 100) * 0.01):
    """种 n 个交易日的 daily_basic(pe/pb 周期波动, 分位带有散度). 返回最后交易日.

    注意: daily_basic 不在 scout_db SCHEMA(生产库由选股流水线建), 临时库自建
    含 pe_ttm/pb 的最小结构(与 _seed_market 的4列版分属不同临时库, 不冲突)."""
    c.execute("CREATE TABLE IF NOT EXISTS daily_basic (trade_date TEXT, ts_code TEXT, "
              "close REAL, pe_ttm REAL, pb REAL, total_mv REAL, "
              "PRIMARY KEY (trade_date, ts_code))")
    d = datetime(2025, 1, 1)
    i = 0
    last = None
    while i < n:
        if d.weekday() < 5:
            last = d.strftime("%Y%m%d")
            c.execute("INSERT OR REPLACE INTO daily_basic (trade_date, ts_code, "
                      "close, pe_ttm, pb, total_mv) VALUES (?,?,?,?,?,?)",
                      (last, ts_code, close, pe_fn(i), pb_fn(i), mv))
            i += 1
        d += timedelta(days=1)
    c.commit()
    return last


def _pe_band_cfg(brief="盈利稳定白马, PE-TTM分位带做T参考, 测试用方法论五十字凑够长度补丁补丁补丁补丁"):
    return {"name": "测试白马", "model": {"kind": "pe_band", "brief": brief},
            "driver": None, "pe_window": 250, "pe_lo_q": 0.10, "pe_hi_q": 0.90,
            "rule_tolerance": 0.10, "agent_ttl_days": 10,
            "research_industries": [], "enabled": True, ".openclaw": "",
            "driver_override": None, "earnings_override": None}


def _fetch_none(api_name, params, fields, timeout=60):
    """新模型不许碰 tushare 主链路(dv_ttm 段允许失败 fail-soft)."""
    raise RuntimeError("network off")


def test_run_pe_band_basic():
    path = tmp_db()
    c = scout_db.conn(path)
    last = _seed_daily_basic(c, "600519.SH")
    V._run_pe_band(c, "600519.SH", _pe_band_cfg(), _fetch_none)
    c.commit()
    row = dict(c.execute("SELECT * FROM valuation_band_daily WHERE ts_code=? "
                         "AND trade_date=?", ("600519.SH", last)).fetchone())
    meta = json.loads(row["meta_json"])
    # 锚: 最后一日 pe_fn(299)=15+99*0.1=24.9 → eps=100/24.9
    assert abs(row["eps_lo"] - round(100 / 24.9, 3)) < 1e-9
    assert row["eps_lo"] == row["eps_hi"]
    # TTM净利 = mv/pe/1e4 = 1e7/24.9/1e4 ≈ 40.16 亿
    assert abs(row["profit_year_lo"] - round(1e7 / 24.9 / 1e4, 2)) < 0.01
    assert row["earnings_source"] == "ttm"
    # 比率带: pe 序列 [15,24.9] 的 10/90 分位, 且 driver 列全 NULL
    assert row["driver_center"] is None and row["driver_source"] is None
    assert 15 < row["pe_lo"] < row["pe_hi"] < 25
    # 短期带存在, meta 自描述完整
    assert row["st_center"] is not None and row["st_source"] == "auto"
    assert meta["model_kind"] == "pe_band"
    assert meta["anchor"]["kind"] == "ttm_eps"
    assert meta["clamp_ctx"]["anchor_profit_mech"] == row["profit_year_lo"]
    c.close()
    os.unlink(path)


def test_run_pe_band_null_pe_today_raises():
    """当日 pe_ttm NULL(亏损/未更新) → 抛错走 stale 兜底."""
    path = tmp_db()
    c = scout_db.conn(path)
    last = _seed_daily_basic(c, "600519.SH")
    c.execute("UPDATE daily_basic SET pe_ttm=NULL WHERE ts_code=? AND trade_date=?",
              ("600519.SH", last))
    c.commit()
    with pytest.raises(RuntimeError, match="pe_ttm 缺失"):
        V._run_pe_band(c, "600519.SH", _pe_band_cfg(), _fetch_none)
    c.close()
    os.unlink(path)


def test_run_pe_band_agent_anchor_adj():
    """agent 锚修正 profit_adj={'anchor':[lo,hi]} 改写 eps/profit_year/earnings_source."""
    path = tmp_db()
    c = scout_db.conn(path)
    last = _seed_daily_basic(c, "600519.SH")
    mech_profit = round(1e7 / 24.9 / 1e4, 2)
    adj_lo, adj_hi = round(mech_profit * 0.9, 2), round(mech_profit * 1.1, 2)
    c.execute("INSERT INTO valuation_agent_view (run_at, ts_code, reviewer, "
              "profit_adj_json, rationale, valid_until, trigger) VALUES (?,?,?,?,?,?,?)",
              ("2099-01-01 00:00:00", "600519.SH", "bot11",
               json.dumps({"anchor": [adj_lo, adj_hi]}), "test", "20990102", "weekly"))
    c.commit()
    V._run_pe_band(c, "600519.SH", _pe_band_cfg(), _fetch_none)
    c.commit()
    row = dict(c.execute("SELECT * FROM valuation_band_daily WHERE ts_code=? "
                         "AND trade_date=?", ("600519.SH", last)).fetchone())
    assert row["earnings_source"] == "agent"
    assert row["profit_year_lo"] == adj_lo and row["profit_year_hi"] == adj_hi
    shares = 1e7 * 1e4 / 100.0
    assert abs(row["eps_lo"] - round(adj_lo * 1e8 / shares, 3)) < 1e-9
    c.close()
    os.unlink(path)


def test_run_pe_band_insufficient_history():
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_daily_basic(c, "600519.SH", n=50)
    with pytest.raises(RuntimeError, match="不足"):
        V._run_pe_band(c, "600519.SH", _pe_band_cfg(), _fetch_none)
    c.close()
    os.unlink(path)


# ---------- Task 6: pb_band 金融/重资产 PB 分位带 ----------

def _pb_band_cfg():
    cfg = _pe_band_cfg()
    cfg.update({"name": "测试银行", "model": {"kind": "pb_band", "brief": cfg["model"]["brief"]}})
    return cfg


def test_run_pb_band_basic():
    path = tmp_db()
    c = scout_db.conn(path)
    last = _seed_daily_basic(c, "600036.SH")
    V._run_pb_band(c, "600036.SH", _pb_band_cfg(), _fetch_none)
    c.commit()
    row = dict(c.execute("SELECT * FROM valuation_band_daily WHERE ts_code=? "
                         "AND trade_date=?", ("600036.SH", last)).fetchone())
    meta = json.loads(row["meta_json"])
    # 锚: 最后一日 pb_fn(299)=1+99*0.01=1.99 → bps=100/1.99
    assert abs(row["eps_lo"] - round(100 / 1.99, 3)) < 1e-9
    assert row["profit_year_lo"] is None and row["earnings_source"] is None
    assert meta["model_kind"] == "pb_band"
    assert meta["anchor"]["kind"] == "bps"
    # 锚不可修的机制: clamp_ctx 无 anchor_profit_mech(writer 会丢弃 anchor 修正)
    assert "anchor_profit_mech" not in meta["clamp_ctx"]
    # 比率带落 pe_* 列(语义=PB分位)
    assert 1.0 < row["pe_lo"] < row["pe_hi"] < 2.0
    c.close()
    os.unlink(path)


def test_run_pb_band_null_pb_today_raises():
    path = tmp_db()
    c = scout_db.conn(path)
    last = _seed_daily_basic(c, "600036.SH")
    c.execute("UPDATE daily_basic SET pb=NULL WHERE ts_code=? AND trade_date=?",
              ("600036.SH", last))
    c.commit()
    with pytest.raises(RuntimeError, match="pb 缺失"):
        V._run_pb_band(c, "600036.SH", _pb_band_cfg(), _fetch_none)
    c.close()
    os.unlink(path)


# ---------- Task 7: research-mcp 一致预期客户端 fetch_consensus ----------
# fixture 行结构按 Step1 探针实测:
#   growth:    content[0].text JSON, data.{code}.{columns,data}
#              columns=["交易日期","证券代码","指标名称","预期值","当前值","变动率"]
#              日期带连字符 "2026-07-10", 预期值单位=元, DIV=1e8
#   consensus: columns=["交易日期","证券代码","预测指标代码","当年预测值","下年预测值","TTM预测值"]
#              指标码 001014=净利润, 单位=元

def _fake_rpc_factory(growth_text_resp, consensus_text_resp):
    """模拟 research-mcp SSE 响应: result.content[0].text 是 JSON 字符串."""
    calls = []
    def rpc(payload, timeout=120):
        calls.append(payload)
        if payload.get("method") == "initialize":
            return {"result": {}}
        name = payload["params"]["name"]
        text = (growth_text_resp if name == "get_stock_consensus_growth"
                else consensus_text_resp)
        return {"result": {"content": [{"type": "text", "text": json.dumps(text)}]}}
    rpc.calls = calls
    return rpc


def test_fetch_consensus_parse():
    # fixture 按探针实测形状(元单位, DIV=1e8); 日期带连字符
    growth = {"success": True, "data": {"603986": {"columns":
        ["交易日期", "证券代码", "指标名称", "预期值", "当前值", "变动率"],
        "data": [["2026-07-09", "603986", "par_net_profit", 1.10e9, 8.8e8, 0.02],
                 ["2026-07-10", "603986", "par_net_profit", 1.12e9, 8.8e8, 0.018]]}}}
    cons = {"success": True, "data": {"603986": {"columns":
        ["交易日期", "证券代码", "预测指标代码", "当年预测值", "下年预测值", "TTM预测值"],
        "data": [["2026-07-10", "603986", "001014", 1.30e9, 1.80e9, 1.12e9]]}}}
    out = V.fetch_consensus("603986", rpc=_fake_rpc_factory(growth, cons))
    assert out["series"] == [("20260709", 11.0), ("20260710", 11.2)]
    assert out["fy"]["fy1_yi"] == 13.0 and out["fy"]["fy2_yi"] == 18.0


def test_fetch_consensus_empty_raises():
    empty = {"success": True, "data": {}}
    with pytest.raises(RuntimeError, match="一致预期序列为空"):
        V.fetch_consensus("603986", rpc=_fake_rpc_factory(empty, empty))


def test_fetch_consensus_fy_failsoft():
    """FY 旁证工具挂掉不阻断: series 是硬依赖, fy 允许 None."""
    growth = {"success": True, "data": {"603986": {"columns":
        ["交易日期", "证券代码", "指标名称", "预期值", "当前值", "变动率"],
        "data": [["2026-07-10", "603986", "par_net_profit", 1.12e9, 0, 0]]}}}
    def rpc(payload, timeout=120):
        if payload.get("method") == "initialize":
            return {"result": {}}
        if payload["params"]["name"] == "get_stock_consensus":
            raise RuntimeError("doris timeout")
        return {"result": {"content": [{"type": "text",
                                        "text": json.dumps(growth)}]}}
    out = V.fetch_consensus("603986", rpc=rpc)
    assert out["series"] and out["fy"] is None


# ---------- Task 8: consensus_pe 成长股前瞻PE分位带 ----------

def _seed_consensus(c, ts_code, dates, profit_fn=lambda i: 10.0 + i * 0.01):
    for i, td in enumerate(dates):
        c.execute("INSERT OR REPLACE INTO consensus_profit_daily VALUES (?,?,?)",
                  (ts_code, td, round(profit_fn(i), 2)))
    c.commit()


def test_run_consensus_pe_with_remote():
    """远程成功: 序列 upsert 缓存, 前瞻PE带 + 预期锚 + fy 旁证落 meta."""
    path = tmp_db()
    c = scout_db.conn(path)
    last = _seed_daily_basic(c, "603986.SH")
    dates = [r[0] for r in c.execute(
        "SELECT trade_date FROM daily_basic WHERE ts_code=? ORDER BY trade_date",
        ("603986.SH",))]
    series = [(td, round(10.0 + i * 0.01, 2)) for i, td in enumerate(dates)]
    def fake_fc(bare, rpc=None):
        assert bare == "603986"          # 必须裸代码
        return {"series": series, "fy": {"date": last, "fy1_yi": 13.0, "fy2_yi": 18.0}}
    V._run_consensus_pe(c, "603986.SH", _consensus_cfg(), _fetch_none,
                        fetch_consensus_fn=fake_fc)
    c.commit()
    # 缓存已 upsert
    n = c.execute("SELECT COUNT(*) FROM consensus_profit_daily WHERE ts_code=?",
                  ("603986.SH",)).fetchone()[0]
    assert n == len(series)
    row = dict(c.execute("SELECT * FROM valuation_band_daily WHERE ts_code=? "
                         "AND trade_date=?", ("603986.SH", last)).fetchone())
    meta = json.loads(row["meta_json"])
    profit_now = series[-1][1]
    assert abs(meta["anchor"]["profit_consensus_yi"] - profit_now) < 1e-9
    # 有 FY 明细 -> 锚用 [FY1,FY2]=[13,18] 前瞻区间(非单点), src=consensus_fy
    assert row["earnings_source"] == "consensus_fy"
    assert (row["profit_year_lo"], row["profit_year_hi"]) == (13.0, 18.0)
    shares = 1e7 * 1e4 / 100.0
    assert abs(row["eps_lo"] - round(13.0 * 1e8 / shares, 3)) < 1e-9
    assert abs(row["eps_hi"] - round(18.0 * 1e8 / shares, 3)) < 1e-9
    assert row["eps_lo"] < row["eps_hi"]            # 锚是真区间, 不再塌成点
    assert meta["model_kind"] == "consensus_pe"
    assert meta["anchor"]["fy"]["fy1_yi"] == 13.0
    assert meta["anchor"]["cache_only"] is False
    assert meta["clamp_ctx"]["anchor_profit_mech"] == profit_now
    c.close()
    os.unlink(path)


def _consensus_cfg():
    cfg = _pe_band_cfg()
    cfg.update({"name": "测试成长",
                "model": {"kind": "consensus_pe", "brief": cfg["model"]["brief"]}})
    return cfg


def test_run_consensus_pe_cache_fallback():
    """远程抛错: 用本地缓存出带, meta.anchor.cache_only=True."""
    path = tmp_db()
    c = scout_db.conn(path)
    last = _seed_daily_basic(c, "603986.SH")
    dates = [r[0] for r in c.execute(
        "SELECT trade_date FROM daily_basic WHERE ts_code=? ORDER BY trade_date",
        ("603986.SH",))]
    _seed_consensus(c, "603986.SH", dates)
    def boom(bare, rpc=None):
        raise RuntimeError("doris timeout")
    V._run_consensus_pe(c, "603986.SH", _consensus_cfg(), _fetch_none,
                        fetch_consensus_fn=boom)
    c.commit()
    row = c.execute("SELECT meta_json FROM valuation_band_daily WHERE ts_code=? "
                    "AND trade_date=?", ("603986.SH", last)).fetchone()
    meta = json.loads(row["meta_json"])
    assert meta["anchor"]["cache_only"] is True
    # 无 FY 明细(远程失败) -> 锚回退单点 consensus, profit_year lo==hi
    r2 = dict(c.execute("SELECT earnings_source, profit_year_lo, profit_year_hi "
                        "FROM valuation_band_daily WHERE ts_code=? AND trade_date=?",
                        ("603986.SH", last)).fetchone())
    assert r2["earnings_source"] == "consensus"
    assert r2["profit_year_lo"] == r2["profit_year_hi"]
    c.close()
    os.unlink(path)


def test_run_consensus_pe_no_data_raises():
    """远程失败且缓存为空 → 抛错走 _mark_stale 兜底."""
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_daily_basic(c, "603986.SH")
    def boom(bare, rpc=None):
        raise RuntimeError("doris timeout")
    with pytest.raises(RuntimeError, match="无一致预期数据"):
        V._run_consensus_pe(c, "603986.SH", _consensus_cfg(), _fetch_none,
                            fetch_consensus_fn=boom)
    c.close()
    os.unlink(path)


def test_run_consensus_pe_lag_days():
    """预期序列断更: lag_days = 缓存最新日期之后的交易日数."""
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_daily_basic(c, "603986.SH")
    dates = [r[0] for r in c.execute(
        "SELECT trade_date FROM daily_basic WHERE ts_code=? ORDER BY trade_date",
        ("603986.SH",))]
    _seed_consensus(c, "603986.SH", dates[:-15])       # 断更 15 个交易日
    def boom(bare, rpc=None):
        raise RuntimeError("doris timeout")
    V._run_consensus_pe(c, "603986.SH", _consensus_cfg(), _fetch_none,
                        fetch_consensus_fn=boom)
    c.commit()
    meta = json.loads(c.execute(
        "SELECT meta_json FROM valuation_band_daily WHERE ts_code=? "
        "ORDER BY trade_date DESC LIMIT 1", ("603986.SH",)).fetchone()["meta_json"])
    assert meta["anchor"]["lag_days"] == 15
    c.close()
    os.unlink(path)


# ---------- Task 14: 三新模型 run_daily e2e 全链路 ----------

def test_run_daily_multi_model_e2e(tmp_path):
    """茅台 pe_band / 中信 pb_band / 兆易 consensus_pe(mock) 临时库全链路出带."""
    path = tmp_db()
    c = scout_db.conn(path)
    for code in ("600519.SH", "600030.SH", "603986.SH"):
        _seed_daily_basic(c, code)
    dates = [r[0] for r in c.execute(
        "SELECT trade_date FROM daily_basic WHERE ts_code=? ORDER BY trade_date",
        ("603986.SH",))]
    _seed_consensus(c, "603986.SH", dates)
    c.commit()
    c.close()
    brief = "测" * 60
    tp = tmp_path / "targets.json"
    tp.write_text(json.dumps({
        "600519.SH": {"name": "贵州茅台", "model": {"kind": "pe_band", "brief": brief}},
        "600030.SH": {"name": "中信证券", "model": {"kind": "pb_band", "brief": brief}},
        "603986.SH": {"name": "兆易创新", "model": {"kind": "consensus_pe", "brief": brief}},
    }, ensure_ascii=False), encoding="utf-8")
    # consensus 远程 mock 掉(fetch_consensus_fn 走 runner 默认参数, 这里靠缓存路径):
    # 缓存已种满, 让远程失败走 cache_only 路径即可全离线跑通
    import unittest.mock as um
    with um.patch.object(V, "fetch_consensus",
                         side_effect=RuntimeError("offline")):
        rc = V.run_daily(path, fetch=_fetch_none, targets_path=str(tp))
    assert rc == 0
    c = scout_db.conn(path)
    rows = {r["ts_code"]: dict(r) for r in c.execute(
        "SELECT * FROM valuation_band_daily")}
    assert len(rows) == 3
    for code, kind in (("600519.SH", "pe_band"), ("600030.SH", "pb_band"),
                       ("603986.SH", "consensus_pe")):
        r = rows[code]
        assert r["price_lo"] is not None and r["price_hi"] > r["price_lo"]
        assert r["st_center"] is not None and r["st_hi"] > r["st_lo"]
        assert json.loads(r["meta_json"])["model_kind"] == kind
        assert r["driver_center"] is None
    # api_targets/api_band 对新模型不炸
    out = V.api_targets(db_path=path, targets_path=str(tp))
    assert len(out["targets"]) == 3
    band = V.api_band("603986.SH", db_path=path, targets_path=str(tp))
    assert band["latest"]["meta"]["anchor"]["kind"] == "consensus_eps"
    c.close()
    os.unlink(path)


# ---------- 营收×净利率盈利模型 (earnings_model=revenue_margin) ----------

# 带 revenue 的累计口径(元): 各季净利率恒 20%, 隐含量随营收变化
REVENUE_INCOME = [
    {"end_date": "20240630", "ann_date": "20240830", "report_type": "1", "n_income_attr_p": 180e8, "revenue": 900e8},
    {"end_date": "20240930", "ann_date": "20241030", "report_type": "1", "n_income_attr_p": 290e8, "revenue": 1450e8},
    {"end_date": "20241231", "ann_date": "20250322", "report_type": "1", "n_income_attr_p": 400e8, "revenue": 2000e8},
    {"end_date": "20250331", "ann_date": "20250412", "report_type": "1", "n_income_attr_p": 100e8, "revenue": 500e8},
    {"end_date": "20250630", "ann_date": "20250827", "report_type": "1", "n_income_attr_p": 230e8, "revenue": 1150e8},
    {"end_date": "20250930", "ann_date": "20251018", "report_type": "1", "n_income_attr_p": 380e8, "revenue": 1900e8},
    {"end_date": "20251231", "ann_date": "20260321", "report_type": "1", "n_income_attr_p": 520e8, "revenue": 2600e8},
    {"end_date": "20260331", "ann_date": "20260422", "report_type": "1", "n_income_attr_p": 200e8, "revenue": 1000e8},
]
REV_SINGLES = V.fetch_income_singles("601899.SH", fetch=_fake_fetch_income(REVENUE_INCOME))


def test_fetch_income_singles_revenue():
    s = {x["quarter"]: x for x in REV_SINGLES}
    assert s["2025Q1"]["revenue_yi"] == 500.0 and s["2025Q1"]["margin"] == 0.2
    assert s["2025Q2"]["revenue_yi"] == 650.0            # 1150-500
    assert s["2026Q1"]["margin"] == 0.2
    # 缺 revenue 的行 -> revenue_yi/margin None, profit 不受影响, 不报错
    rows = [{"end_date": "20260331", "ann_date": "20260422", "report_type": "1",
             "n_income_attr_p": 200e8}]
    s2 = V.fetch_income_singles("x", fetch=_fake_fetch_income(rows))
    assert s2[0]["revenue_yi"] is None and s2[0]["margin"] is None
    assert s2[0]["profit_yi"] == 200.0


def test_revenue_margin_estimate_basic():
    est = V.revenue_margin_estimate(REV_SINGLES, FLAT_GOLD, "20260710", 0.10)
    assert est is not None
    lo, hi, basis = est
    assert basis["method"] == "revenue_margin" and lo < hi
    assert basis["margin_lo"] == 0.2 and basis["margin_hi"] == 0.2
    mid = basis["vol_smooth"] * 800 * 0.20                # 手算点估(价横盘800)
    assert lo < mid < hi


def test_revenue_margin_estimate_insufficient_falls_back():
    # 无 revenue 的 singles -> 估计器 None
    assert V.revenue_margin_estimate(SINGLES, FLAT_GOLD, "20260710", 0.10) is None
    # 经 build_year_quarters(revenue_margin) 该 estimated 季回退 rule
    by = {q["quarter"]: q for q in
          V.build_year_quarters("2026", SINGLES, [], FLAT_GOLD, "20260710", 0.10,
                                earnings_model="revenue_margin")}
    assert by["2026Q3"]["basis"]["method"] == "rule"


def test_revenue_margin_estimate_negative_margin_falls_back():
    # 最近 lookback 内有亏损季(margin<0) -> 穿零, 估计器 None 交给 rule
    neg = [dict(s) for s in REV_SINGLES]
    neg[-1] = dict(neg[-1], profit_yi=-50.0, margin=-0.05)
    assert V.revenue_margin_estimate(neg, FLAT_GOLD, "20260710", 0.10) is None


def test_build_year_quarters_model_selector():
    qd = {q["quarter"]: q for q in
          V.build_year_quarters("2026", REV_SINGLES, [], FLAT_GOLD, "20260710", 0.10)}
    assert qd["2026Q3"]["basis"]["method"] == "rule"            # 默认 rule
    qr = {q["quarter"]: q for q in
          V.build_year_quarters("2026", REV_SINGLES, [], FLAT_GOLD, "20260710", 0.10,
                                earnings_model="revenue_margin")}
    assert qr["2026Q3"]["basis"]["method"] == "revenue_margin"  # 翻转
    assert qr["2026Q1"]["status"] == "actual"                   # actual 季不受影响


def test_pit_pe_series_model_selector():
    basics = _mk_basics(200)
    # 默认与显式 rule 逐点一致(回归守护)
    assert (V.pit_pe_series(basics, RICH_SINGLES, [], FLAT_GOLD, 0.10)
            == V.pit_pe_series(basics, RICH_SINGLES, [], FLAT_GOLD, 0.10,
                               earnings_model="rule"))
    pes = V.pit_pe_series(basics, REV_SINGLES, [], FLAT_GOLD, 0.10,
                          earnings_model="revenue_margin")
    assert pes and all(p > 0 for _, p in pes)


def test_load_targets_earnings_model(tmp_path):
    drv = {"kind": "sge", "symbol": "Au99.99", "label": "上海金"}

    def w(cfg, name="t"):
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return str(p)
    # 默认 rule
    t = V.load_targets(w({"601899.SH": {"name": "x", "driver": drv}}, "d"))
    assert t["601899.SH"]["earnings_model"] == "rule"
    # commodity_pe 可开 revenue_margin
    t = V.load_targets(w({"601899.SH": {"name": "x", "driver": drv,
                                        "earnings_model": "revenue_margin"}}, "rm"))
    assert t["601899.SH"]["earnings_model"] == "revenue_margin"
    # pe_band 传 revenue_margin 报错
    with pytest.raises(ValueError):
        V.load_targets(w({"600519.SH": {"name": "x", "model": {"kind": "pe_band"},
                                        "earnings_model": "revenue_margin"}}, "pb"))
    # 非法值报错
    with pytest.raises(ValueError):
        V.load_targets(w({"601899.SH": {"name": "x", "driver": drv,
                                        "earnings_model": "bogus"}}, "bad"))


def test_run_daily_revenue_margin_e2e(tmp_path):
    path = tmp_db()
    _seed_market(path, _mk_basics(200))
    rc = V.run_daily(path, fetch=_fetcher(REVENUE_INCOME, GOLD_ROWS),
                     targets_path=_mk_targets(
                         tmp_path, extra={"earnings_model": "revenue_margin"}))
    assert rc == 0
    c = scout_db.conn(path)
    b = dict(c.execute("SELECT * FROM valuation_band_daily WHERE ts_code='601899.SH' "
                       "ORDER BY trade_date DESC LIMIT 1").fetchone())
    assert b["earnings_source"] == "revenue_margin"
    assert b["profit_year_lo"] > 578.0                    # 下沿高于 rule 基线
    meta = json.loads(b["meta_json"])
    assert meta["clamp_ctx"]["rule_quarters"]             # 钳制基准仍在(现为 rev-margin 区间)
    qs = {r["quarter"]: r for r in c.execute(
        "SELECT * FROM valuation_quarter_est WHERE ts_code='601899.SH'")}
    assert json.loads(qs["2026Q3"]["basis_json"])["method"] == "revenue_margin"
    c.close()
    os.unlink(path)


def test_run_daily_other_commodity_unchanged(tmp_path):
    """回归闸门: 未开 earnings_model 的标的即便供了营收数据, 仍走 rule, band 与基线一致."""
    path = tmp_db()
    _seed_market(path, _mk_basics(200))
    rc = V.run_daily(path, fetch=_fetcher(REVENUE_INCOME, GOLD_ROWS),
                     targets_path=_mk_targets(tmp_path))   # 无 earnings_model -> rule
    assert rc == 0
    c = scout_db.conn(path)
    b = dict(c.execute("SELECT earnings_source, profit_year_lo, profit_year_hi "
                       "FROM valuation_band_daily WHERE ts_code='601899.SH' "
                       "ORDER BY trade_date DESC LIMIT 1").fetchone())
    assert b["earnings_source"] == "rule"
    assert (b["profit_year_lo"], b["profit_year_hi"]) == (578.0, 860.0)
    c.close()
    os.unlink(path)


# ---------- 统一业绩明细模块 (非商品模型也落 实际+预告 季度) ----------

def test_actual_forecast_quarters():
    # SINGLES 2026 只有 Q1 实际, 无预告 -> 只出 Q1, 不猜估计季
    q = V.actual_forecast_quarters(SINGLES, [], "20260710", "2026")
    by = {x["quarter"]: x for x in q}
    assert by["2026Q1"]["status"] == "actual" and by["2026Q1"]["lo"] == 200.0
    assert "2026Q2" not in by                       # 无预告无估计 -> 停
    assert all(x["status"] in ("actual", "forecast") for x in q)   # 绝无 estimated
    # 加半年累计预告[420,440] -> Q2 forecast 差分=[220,240]
    ev = [{"quarter": "2026Q2", "ann_date": "20260715",
           "cum_lo_yi": 420.0, "cum_hi_yi": 440.0}]
    by2 = {x["quarter"]: x for x in
           V.actual_forecast_quarters(SINGLES, ev, "20260720", "2026")}
    assert by2["2026Q2"]["status"] == "forecast"
    assert (by2["2026Q2"]["lo"], by2["2026Q2"]["hi"]) == (220.0, 240.0)
    assert "2026Q3" not in by2                       # 预告后仍停


def _fetch_income_only(income_rows):
    def fetch(api_name, params, fields, timeout=60):
        if api_name == "income":
            return income_rows
        raise RuntimeError("network off")            # 其他 api fail-soft
    return fetch


def test_run_pe_band_writes_display_quarters():
    """非商品(pe_band)也落业绩明细: 实际季来自 income, 无 estimated."""
    path = tmp_db()
    c = scout_db.conn(path)
    _seed_daily_basic(c, "600519.SH")
    V._run_pe_band(c, "600519.SH", _pe_band_cfg(), _fetch_income_only(REVENUE_INCOME))
    c.commit()
    qs = {r["quarter"]: r["status"] for r in c.execute(
        "SELECT quarter, status FROM valuation_quarter_est WHERE ts_code='600519.SH'")}
    assert qs                                        # 有落库
    assert qs["2026Q1"] == "actual" and qs["2025Q4"] == "actual"
    assert set(qs.values()) <= {"actual", "forecast"}   # 绝无 estimated
    # api_band 近8季返回且含 actual
    band = V.api_band("600519.SH", db_path=path,
                      targets_path=_mk_targets_pe(path))
    assert 0 < len(band["quarters"]) <= 8
    c.close()
    os.unlink(path)


def _mk_targets_pe(_):
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"600519.SH": {"name": "测试白马",
                                 "model": {"kind": "pe_band", "brief": "x" * 60}}}, f)
    return p


def test_run_pe_band_display_quarters_failsoft():
    """income 取数失败 -> 不落业绩明细但带子照出(fail-soft)."""
    path = tmp_db()
    c = scout_db.conn(path)
    last = _seed_daily_basic(c, "600519.SH")
    V._run_pe_band(c, "600519.SH", _pe_band_cfg(), _fetch_none)   # 全 raise
    c.commit()
    assert c.execute("SELECT COUNT(*) FROM valuation_quarter_est "
                     "WHERE ts_code='600519.SH'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM valuation_band_daily WHERE ts_code=? "
                     "AND trade_date=?", ("600519.SH", last)).fetchone()[0] == 1
    c.close()
    os.unlink(path)
