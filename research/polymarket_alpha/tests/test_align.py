import sys; sys.path.insert(0, "/home/rooot/agent_invest_lab/research/polymarket_alpha")
import pandas as pd, numpy as np
from align import align, load_signal

def test_no_lookahead_signal_is_lagged():
    # price 每日 +0(常数收益方便验证), signal 在某日跳变, 断言对齐后 sig 用的是前一日值
    idx = pd.bdate_range("2025-01-01", periods=10)
    price = pd.Series(np.arange(10, 20, dtype=float), index=idx)   # 递增
    signal = pd.Series(0.0, index=idx); signal.iloc[5] = 1.0        # 第5日跳到1
    out = align(signal, price, horizon=1)
    # 第6日(iloc对齐后)的 sig 应等于第5日信号=1; 第5日的 sig 应=第4日信号=0
    assert out.loc[idx[6], "sig"] == 1.0
    assert out.loc[idx[5], "sig"] == 0.0

def test_fwd_ret_is_future():
    idx = pd.bdate_range("2025-01-01", periods=6)
    price = pd.Series([1,1,1,2,2,2], index=idx, dtype=float)
    signal = pd.Series(0.5, index=idx)
    out = align(signal, price, horizon=1)
    # idx[2]->idx[3] 收益=100%, 该行 fwd_ret 应≈1.0
    assert abs(out.loc[idx[2], "fwd_ret"] - 1.0) < 1e-9

def test_load_signal_reads_real_data():
    # 守回归: POLY_DB 路径若再错会立刻失败(读不到 252 行真实数据)
    s = load_signal("recession_2026")
    assert len(s) > 100
    assert s.index.is_monotonic_increasing

if __name__ == "__main__":
    test_no_lookahead_signal_is_lagged(); test_fwd_ret_is_future(); test_load_signal_reads_real_data(); print("OK")
