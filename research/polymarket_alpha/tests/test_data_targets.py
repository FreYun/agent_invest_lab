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

if __name__ == "__main__":
    test_index_from_marketdb(); test_cached_etf_csv(); test_backfilled_us()
    print("OK")
