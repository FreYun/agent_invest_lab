"""真实 tushare index_daily 契约: 验证取数返满字段。
无 token / tushare 不可用 → skip(非生产机 CI 友好)。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backfill_index_daily as B  # noqa: E402


def _has_token():
    return bool(os.environ.get("TUSHARE_TOKEN")
                or os.path.exists(os.path.expanduser("~/.openclaw/.tushare-token")))


def test_real_tushare_returns_full_fields():
    import pytest
    if not _has_token():
        pytest.skip("无 tushare token")
    try:
        df = B.fetch_index_daily_full("932000.CSI", "20260601", "20260630")  # 中证2000
    except Exception as e:  # noqa: BLE001  网络/代理波动
        pytest.skip(f"tushare 不可用: {e}")
    assert len(df) > 0
    for col in ("trade_date", "open", "high", "low", "close",
                "pre_close", "pct_chg", "vol", "amount"):
        assert col in df.columns, f"缺列 {col}"
