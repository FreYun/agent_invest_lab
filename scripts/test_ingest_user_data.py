import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ingest_user_data import classify, reconstruct_curve


def test_classify_buy():
    assert classify("申购确认") == "buy"
    assert classify("定时定额投资确认") == "buy"


def test_classify_sell():
    assert classify("赎回确认") == "sell"
    assert classify("强行赎回") == "sell"


def test_classify_xfer_in():
    assert classify("转入投资账户") == "xfer_in"
    assert classify("转托管入确认") == "xfer_in"
    assert classify("份额转卡转入") == "xfer_in"


def test_classify_xfer_out():
    assert classify("转出投资账户") == "xfer_out"
    assert classify("份额转卡转出") == "xfer_out"


def test_classify_skip():
    assert classify("设置分红方式确认") == "skip"
    assert classify("转换确认") == "skip"
    assert classify("转托管确认") == "skip"
    assert classify("") == "skip"
    assert classify("未知类型") == "skip"


def _buy(date, amount, vol):
    return {"busin_name": "申购确认", "amount": amount, "vol": vol, "txn_date": date}


def _sell(date, amount, vol):
    return {"busin_name": "赎回确认", "amount": amount, "vol": vol, "txn_date": date}


def _xfer_in(date, vol):
    return {"busin_name": "转入投资账户", "amount": 0.0, "vol": vol, "txn_date": date}


def test_reconstruct_dca_scale():
    nav = {"2025-01-02": 1.0, "2025-01-03": 1.1, "2025-01-04": 1.2}
    txns = [_buy("2025-01-02", 100.0, 100.0)]
    series, method = reconstruct_curve(txns, nav, "2025-01-02", "2025-01-04", 0.20)
    assert method == "scale"
    assert abs(series[0]["net_value"] - 1.0) < 1e-9
    assert abs(series[-1]["net_value"] - 1.20) < 1e-9
    assert [p["trade_date"] for p in series] == ["2025-01-02", "2025-01-03", "2025-01-04"]


def test_reconstruct_scale_to_authoritative_return():
    # raw roi_end = 0.20 but authoritative ClearReturn2 = 0.10 (fees/dividends) → f=0.5
    nav = {"2025-01-02": 1.0, "2025-01-03": 1.1, "2025-01-04": 1.2}
    series, method = reconstruct_curve([_buy("2025-01-02", 100.0, 100.0)], nav,
                                       "2025-01-02", "2025-01-04", 0.10)
    assert method == "scale"
    assert abs(series[-1]["net_value"] - 1.10) < 1e-9
    assert abs(series[1]["net_value"] - 1.05) < 1e-9


def test_reconstruct_partial_redeem():
    nav = {"d1": 1.0, "d2": 2.0, "d3": 2.0}
    txns = [_buy("d1", 100.0, 100.0), _sell("d2", 100.0, 50.0)]
    series, method = reconstruct_curve(txns, nav, "d1", "d3", 1.0)
    assert method == "scale"
    assert abs(series[-1]["net_value"] - 2.0) < 1e-9


def test_reconstruct_full_redeem():
    nav = {"d1": 1.0, "d2": 1.5, "d3": 1.5}
    txns = [_buy("d1", 100.0, 100.0), _sell("d2", 150.0, 100.0)]
    series, method = reconstruct_curve(txns, nav, "d1", "d3", 0.5)
    assert method == "scale"
    assert abs(series[-1]["net_value"] - 1.5) < 1e-9


def test_reconstruct_xfer_in_adds_shares_no_cost():
    nav = {"d1": 1.0, "d2": 1.0}
    txns = [_buy("d1", 100.0, 100.0), _xfer_in("d2", 100.0)]
    series, method = reconstruct_curve(txns, nav, "d1", "d2", 1.0)
    assert method == "scale"
    # 200 shares × nav 1.0, invested still 100 → raw roi_end = 1.0
    assert abs(series[-1]["net_value"] - 2.0) < 1e-9


def test_endpoint_pin_shift_on_signflip():
    nav = {"d1": 1.0, "d2": 1.2}
    series, method = reconstruct_curve([_buy("d1", 100.0, 100.0)], nav, "d1", "d2", -0.1)
    assert method == "shift"
    assert abs(series[-1]["net_value"] - 0.9) < 1e-9  # 1 + (-0.1)


def test_endpoint_pin_shift_on_zero_roi():
    # only a transfer-in, no buy → invested = 0 → raw roi all 0 → additive shift
    nav = {"d1": 1.0, "d2": 1.0}
    series, method = reconstruct_curve([_xfer_in("d1", 100.0)], nav, "d1", "d2", 0.05)
    assert method == "shift"
    assert all(abs(p["net_value"] - 1.05) < 1e-9 for p in series)


def test_reconstruct_empty_when_no_nav_in_window():
    series, method = reconstruct_curve([_buy("d1", 100.0, 100.0)], {}, "d1", "d2", 0.1)
    assert series == []
    assert method == "empty"
