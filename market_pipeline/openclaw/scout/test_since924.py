"""since924 单测: 复权累乘正确(含送转)/次新/退市剔除/分段/基准/空数据. 全程临时库."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import since924 as s


def _mkdb(path):
    """构造小样本 market.db. latest=20240926. 见计划注释里的预期收益."""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE daily (trade_date TEXT, ts_code TEXT, open REAL, high REAL, "
              "low REAL, close REAL, pre_close REAL, pct_chg REAL, vol REAL, amount REAL)")
    c.execute("CREATE TABLE stock_names (code TEXT, ts_code TEXT, name TEXT, updated_at TEXT)")
    c.execute("CREATE TABLE sw_industry_member (ts_code TEXT PRIMARY KEY, l1_code TEXT, "
              "l1_name TEXT, l2_code TEXT, l2_name TEXT, updated_at TEXT)")
    c.execute("CREATE TABLE stock_concept_map (snapshot_date TEXT, board_code TEXT, "
              "board_name TEXT, ts_code TEXT, name TEXT)")
    c.execute("CREATE TABLE index_daily (trade_date TEXT, ts_code TEXT, close REAL, "
              "pre_close REAL, pct_chg REAL)")

    def d(date, ts, close, pct, pre=None):
        # pre = pre_close（除权调整后的昨收）; price 口径需要它, 默认=close(窗口外的基准日行无所谓)
        c.execute("INSERT INTO daily(trade_date,ts_code,close,pre_close,pct_chg) VALUES (?,?,?,?,?)",
                  (date, ts, close, close if pre is None else pre, pct))

    # A 浦发银行 600000.SH: +10% 后 10送10(裸价腰斩但 pct_chg≈0); first=20200101
    #   adj(含分红)=+10%; price(送转还原)=+10% (纯送转无分红, 不该假摔成 -45%)
    d("20200101", "600000.SH", 10.0, 0.0)
    d("20240924", "600000.SH", 11.0, 10.0, pre=10.0)
    d("20240925", "600000.SH", 5.5, 0.0, pre=5.5)    # 10送10: pre_close=5.5 vs 前日11 -> 机械缺口, 还原
    d("20240926", "600000.SH", 5.5, 0.0, pre=5.5)
    # F 工商银行 601398.SH: +2%,+1%,0 -> adj≈+3.02%; price≈+3.0%
    d("20200101", "601398.SH", 5.0, 0.0)
    d("20240924", "601398.SH", 5.1, 2.0, pre=5.0)
    d("20240925", "601398.SH", 5.15, 1.0, pre=5.1)
    d("20240926", "601398.SH", 5.15, 0.0, pre=5.15)
    # B 华兴源创 688001.SH 次新: first=20240925(晚于基准); +10%,+10% -> adj=price=+21%
    d("20240925", "688001.SH", 22.0, 10.0, pre=20.0)
    d("20240926", "688001.SH", 24.2, 10.0, pre=22.0)
    # C 万科A 000002.SZ: 退市 — 最新交易日(0926)无行 -> 应剔除
    d("20200101", "000002.SZ", 20.0, 0.0)
    d("20240924", "000002.SZ", 21.0, 5.0, pre=20.0)
    d("20240925", "000002.SZ", 20.4, -3.0, pre=21.0)
    # D 宁德时代 300750.SZ: -2,-3,-1 -> adj≈-5.89%
    d("20200101", "300750.SZ", 200.0, 0.0)
    d("20240924", "300750.SZ", 196.0, -2.0, pre=200.0)
    d("20240925", "300750.SZ", 190.0, -3.0, pre=196.0)
    d("20240926", "300750.SZ", 188.0, -1.0, pre=190.0)
    # E *ST示例 600519.SH: -5,-5,-5 -> adj≈-14.26%
    d("20200101", "600519.SH", 100.0, 0.0)
    d("20240924", "600519.SH", 95.0, -5.0, pre=100.0)
    d("20240925", "600519.SH", 90.25, -5.0, pre=95.0)
    d("20240926", "600519.SH", 85.74, -5.0, pre=90.25)
    # H 中国电影 600977.SH: 分红股. 每日 pct_chg=0(相对除息后 pre_close 不动),
    #   但 0925 除息(pre=9.8 < 前日10) -> adj=0%(含分红总回报), price=-2%(不含分红, 除息价跌计入)
    d("20200101", "600977.SH", 10.0, 0.0)
    d("20240924", "600977.SH", 10.0, 0.0, pre=10.0)
    d("20240925", "600977.SH", 9.8, 0.0, pre=9.8)    # 除息: pre 9.8 vs 前日10 (缺口仅2%, 当分红非送转)
    d("20240926", "600977.SH", 9.8, 0.0, pre=9.8)

    c.executemany("INSERT INTO stock_names(ts_code,name) VALUES (?,?)", [
        ("600000.SH", "浦发银行"), ("601398.SH", "工商银行"), ("688001.SH", "华兴源创"),
        ("000002.SZ", "万科A"), ("300750.SZ", "宁德时代"), ("600519.SH", "*ST示例"),
        ("600977.SH", "中国电影"),
    ])
    c.executemany("INSERT INTO sw_industry_member(ts_code,l1_code,l1_name,l2_code,l2_name) "
                  "VALUES (?,?,?,?,?)", [
        ("600000.SH", "801780.SI", "银行", "801781.SI", "股份制银行"),
        ("601398.SH", "801780.SI", "银行", "801782.SI", "国有大型银行"),
        ("688001.SH", "801080.SI", "电子", "801081.SI", "半导体"),
        ("300750.SZ", "801730.SI", "电力设备", "801737.SI", "电池"),
        ("600519.SH", "801120.SI", "食品饮料", "801124.SI", "白酒"),
        ("600977.SH", "801760.SI", "传媒", "801761.SI", "影视院线"),
    ])
    # stock_concept_map.board_name 故意填个股名（模拟脏数据：本库该列实际装的是个股名而非板块名）
    # 板块真实名称改从 concept_board_daily 取，see _board_names()
    c.executemany("INSERT INTO stock_concept_map(snapshot_date,board_code,board_name,ts_code) "
                  "VALUES (?,?,?,?)", [
        ("20240926", "BK001", "华兴源创", "688001.SH"),
        ("20240926", "BK002", "浦发银行", "600000.SH"),
        ("20240926", "BK002", "工商银行", "601398.SH"),
    ])
    # concept_board_daily：权威板块名来源（故意与上面脏 board_name 不同，确保测试能抓回归）
    c.execute("CREATE TABLE concept_board_daily (trade_date TEXT, board_code TEXT, board_name TEXT)")
    c.executemany("INSERT INTO concept_board_daily(trade_date,board_code,board_name) VALUES (?,?,?)", [
        ("20240926", "BK001", "半导体"),
        ("20240926", "BK002", "银行精选"),
    ])
    c.executemany("INSERT INTO index_daily(trade_date,ts_code,pct_chg) VALUES (?,?,?)", [
        ("20240924", "000300.SH", 5.0), ("20240925", "000300.SH", 0.0), ("20240926", "000300.SH", 0.0),
        ("20240924", "000001.SH", 3.0), ("20240925", "000001.SH", 0.0), ("20240926", "000001.SH", 0.0),
    ])
    c.commit()
    c.close()


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "market.db")
    _mkdb(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_segment_of():
    assert s.segment_of("688001.SH") == "科创板"
    assert s.segment_of("600000.SH") == "沪主板"
    assert s.segment_of("300750.SZ") == "创业板"
    assert s.segment_of("000002.SZ") == "深主板"
    assert s.segment_of("830001.BJ") == "北交所"


def test_compound_handles_split_no_false_crash(conn):
    rm = s._compute_ret_map(conn)
    # 浦发: 送转日裸价腰斩但 pct_chg≈0 -> 累计仍 +10%, 绝不虚假腰斩
    assert rm["600000.SH"]["ret"] == pytest.approx(0.10, abs=1e-9)
    assert rm["600000.SH"]["days"] == 3      # 0924/0925/0926 三天 (含基准日后首日)


def test_new_listing_flagged_and_old_not(conn):
    rm = s._compute_ret_map(conn)
    assert rm["688001.SH"]["is_new"] is True      # first=20240925 晚于基准
    assert rm["688001.SH"]["ret"] == pytest.approx(0.21, abs=1e-9)
    assert rm["600000.SH"]["is_new"] is False


def test_delisted_excluded(conn):
    rm = s._compute_ret_map(conn)
    assert "000002.SZ" not in rm                   # 最新交易日无行 -> 剔除


def test_price_is_latest_close(conn):
    rm = s._compute_ret_map(conn)
    assert rm["300750.SZ"]["price"] == pytest.approx(188.0)


def test_benchmarks_failsoft(conn):
    bms = s._benchmarks(conn)
    names = {b["name"]: b["ret"] for b in bms}
    assert names["沪深300"] == pytest.approx(0.05, abs=1e-9)
    assert names["上证指数"] == pytest.approx(0.03, abs=1e-9)
    assert "中证1000" not in names                 # index_daily 无该指数 -> 跳过, 不报错


def test_empty_db(tmp_path):
    db = str(tmp_path / "empty.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE daily (trade_date TEXT, ts_code TEXT, close REAL, pct_chg REAL)")
    c.row_factory = sqlite3.Row
    try:
        assert s._compute_ret_map(c) == {}
    finally:
        c.close()


def test_stocks_payload_overview(conn):
    rm = s._compute_ret_map(conn)
    p = s.build_stocks_payload(conn, rm)
    assert p["base_date"] == "20240923"
    assert p["latest_date"] == "20240926"
    assert p["count"] == 6                          # A,B,D,E,F,H (C 退市剔除)
    o = p["overview"]
    # 6 只 adj 收益排序 [-0.1426,-0.0589,0(H),0.0302,0.10,0.21] -> 中位=(0+0.0302)/2
    assert o["median"] == pytest.approx((0.0 + 0.0302) / 2, abs=1e-4)
    assert o["up"] == 3 and o["down"] == 2          # H adj=0 既不算涨也不算跌
    assert o["double"] == 0 and o["halve"] == 0
    # 个股按 ret 降序; 榜首是华兴源创(+21%)
    assert p["stocks"][0]["code"] == "688001.SH"
    assert p["stocks"][0]["is_new"] is True


def test_stocks_payload_st_and_fields(conn):
    rm = s._compute_ret_map(conn)
    p = s.build_stocks_payload(conn, rm)
    e = [x for x in p["stocks"] if x["code"] == "600519.SH"][0]
    assert e["is_st"] is True
    assert e["segment"] == "沪主板"
    assert e["l1_name"] == "食品饮料" and e["l2_name"] == "白酒"


def test_sw_agg_grouping(conn):
    rm = s._compute_ret_map(conn)
    agg = s.sw_agg(conn, rm)
    bank = [g for g in agg if g["l1_name"] == "银行"][0]
    assert bank["count"] == 2                         # 浦发 + 工行
    assert bank["median"] == pytest.approx((0.0302 + 0.10) / 2, abs=1e-4)
    assert bank["up_ratio"] == 1.0
    assert bank["leader"]["code"] == "600000.SH"      # 浦发 +10% 领涨
    assert bank["laggard"]["code"] == "601398.SH"     # 工行 +3% 垫底
    l2names = {x["l2_name"] for x in bank["l2"]}
    assert l2names == {"股份制银行", "国有大型银行"}


def test_stats_quantiles():
    """_stats 分位: 两元素 [0.0302,0.10] 线性插值 + 单元素/空退化."""
    st = s._stats([0.0302, 0.10])
    assert st["median"] == pytest.approx(0.0651, abs=1e-4)
    assert st["p10"] == pytest.approx(0.03718, abs=1e-4)
    assert st["p25"] == pytest.approx(0.04765, abs=1e-4)
    assert st["p75"] == pytest.approx(0.08255, abs=1e-4)
    assert st["p90"] == pytest.approx(0.09302, abs=1e-4)
    assert s._stats([0.5])["p25"] == 0.5            # 单元素: 所有分位=该值
    assert s._stats([])["p75"] is None              # 空: None


def test_sw_agg_carries_quantiles(conn):
    """sw_agg 的 L1/L2 都应带上 p10/p25/p75/p90(由 _stats 透出)."""
    agg = s.sw_agg(conn, s._compute_ret_map(conn))
    bank = [g for g in agg if g["l1_name"] == "银行"][0]
    assert bank["p25"] == pytest.approx(0.04765, abs=1e-4)
    assert bank["p75"] == pytest.approx(0.08255, abs=1e-4)
    assert all(k in bank["l2"][0] for k in ("p10", "p25", "p75", "p90"))


def test_concept_agg(conn):
    rm = s._compute_ret_map(conn)
    cp = s.concept_agg(conn, rm)
    assert cp["snapshot_date"] == "20240926"
    bank = [c for c in cp["concepts"] if c["board_code"] == "BK002"][0]
    assert bank["board_name"] == "银行精选"
    assert bank["count"] == 2                          # 浦发 + 工行
    assert bank["median"] == pytest.approx((0.0302 + 0.10) / 2, abs=1e-4)
    assert bank["leader"]["code"] == "600000.SH"


def test_concept_members(conn):
    rm = s._compute_ret_map(conn)
    m = s.concept_members(conn, rm, "BK002")
    assert m["board_name"] == "银行精选" and m["count"] == 2
    assert m["members"][0]["code"] == "600000.SH"      # 按 ret 降序, 浦发居首
    assert m["members"][0]["ret"] == pytest.approx(0.10, abs=1e-9)


def test_price_mode_split_not_false_crash(conn):
    """price 口径: 送转股不假摔 + 分红被扣除; 对照 adj 口径差异."""
    adj = s._compute_ret_map(conn, "adj")
    price = s._compute_ret_map(conn, "price")
    # 浦发 10送10: 裸价比会假摔到 ~-45%, 送转还原后应=+10%(无分红, 同 adj)
    assert price["600000.SH"]["ret"] == pytest.approx(0.10, abs=1e-6)
    assert adj["600000.SH"]["ret"] == pytest.approx(0.10, abs=1e-6)
    # 华兴(次新) 两口径同 +21%
    assert price["688001.SH"]["ret"] == pytest.approx(0.21, abs=1e-6)
    # 中国电影 分红股: adj(含分红)=0%, price(不含分红, 除息价跌计入)=-2%
    assert adj["600977.SH"]["ret"] == pytest.approx(0.0, abs=1e-6)
    assert price["600977.SH"]["ret"] == pytest.approx(-0.02, abs=1e-6)


def test_price_mode_via_cached_payload(conn):
    """缓存包装 stocks_payload(mode) 切换口径: 中国电影 adj=0% / price=-2%."""
    # 重置全局缓存: 所有 fixture latest 都是 20240926, 否则会读到别的测试库的缓存
    s._CACHE["key"] = None
    s._CACHE["modes"] = {}
    pa = s.stocks_payload(conn, "adj")
    pp = s.stocks_payload(conn, "price")
    a = [x for x in pa["stocks"] if x["code"] == "600977.SH"][0]
    p = [x for x in pp["stocks"] if x["code"] == "600977.SH"][0]
    assert a["ret"] == pytest.approx(0.0, abs=1e-6)
    assert p["ret"] == pytest.approx(-0.02, abs=1e-6)
    # 两份 payload 是不同对象(分模式缓存), 不串
    assert pa is not pp
