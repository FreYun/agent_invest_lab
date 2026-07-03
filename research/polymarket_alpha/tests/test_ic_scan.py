import sys; sys.path.insert(0, "/home/rooot/agent_invest_lab/research/polymarket_alpha")
import pandas as pd, numpy as np
from ic_scan import spearman_ic, quantile_monotonicity, detrend

def test_ic_perfect_positive():
    df = pd.DataFrame({"sig": np.arange(50.0), "fwd_ret": np.arange(50.0)})
    assert spearman_ic(df) > 0.99

def test_ic_perfect_negative():
    df = pd.DataFrame({"sig": np.arange(50.0), "fwd_ret": -np.arange(50.0)})
    assert spearman_ic(df) < -0.99

def test_quantile_monotonic():
    df = pd.DataFrame({"sig": np.arange(50.0), "fwd_ret": np.arange(50.0)})
    assert quantile_monotonicity(df, 5) > 0.9

def test_detrend_removes_linear_trend():
    s = pd.Series(np.arange(100.0))   # 纯线性趋势
    d = detrend(s, 5).dropna()
    assert (d == 5.0).all()           # 差分后为常数

if __name__ == "__main__":
    test_ic_perfect_positive(); test_ic_perfect_negative()
    test_quantile_monotonic(); test_detrend_removes_linear_trend(); print("OK")
