import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ingest_user_data import classify


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
