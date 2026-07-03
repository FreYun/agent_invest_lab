import sys; sys.path.insert(0, "/home/rooot/agent_invest_lab/research/polymarket_alpha")
import pandas as pd
from data_targets import load_target

def test_index_from_marketdb():
    s = load_target("000300.SH")
    assert isinstance(s, pd.Series) and len(s) > 200
    assert s.index.is_monotonic_increasing
    assert s.notna().all()

def test_cached_etf_csv():
    s = load_target("510300.SH")
    assert len(s) > 200 and s.index.is_monotonic_increasing

def test_backfilled_us():
    s = load_target("QQQ")   # 若用了代理, 改成 513100
    assert len(s) > 100

def test_backfilled_gold():
    s = load_target("518880")
    assert len(s) > 100 and s.index.is_monotonic_increasing

def test_us_proxy():
    s = load_target("SPY")   # 经 PROXY_ALIAS 映射到 513500 代理数据
    assert len(s) > 100 and s.index.is_monotonic_increasing

if __name__ == "__main__":
    test_index_from_marketdb(); test_cached_etf_csv(); test_backfilled_us()
    test_backfilled_gold(); test_us_proxy()
    print("OK")
