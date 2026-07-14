"""parser.py 的单元测试。

覆盖:
1. parse_daily_review — 真实复盘 MD fixture + 合成最小 MD 字符串
2. compute_index_ma / compute_volume_ratio — 合成 DataFrame, 不依赖网络
3. fetch_index_daily — 本地 CSV 缓存读取路径 + monkeypatch Tushare 拉取路径
4. fetch_full_market_volume — 缓存读取 + monkeypatch Tushare daily_info 路径
5. build_raw_market_data — 完整编排器, 含降级路径

不打真实 Tushare 网络 (网络路径通过 monkeypatch 打桩 tushare_client.call)。
"""

import os
import tempfile

import pandas as pd
import pytest

from parser import (
    RawMarketData,
    build_raw_market_data,
    compute_index_ma,
    compute_pct_change,
    compute_volume_ratio,
    fetch_full_market_volume,
    fetch_index_daily,
    parse_daily_review,
)


FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
REVIEW_MD_2026_03_20 = os.path.join(FIXTURE_DIR, "复盘_2026-03-20.md")
REVIEW_MD_2026_03_19_LEGACY = os.path.join(FIXTURE_DIR, "复盘_2026-03-19_legacy.md")


# --------------------------------------------------------------------------- #
# parse_daily_review: 真实 MD fixture
# --------------------------------------------------------------------------- #


class TestParseDailyReviewFixture:
    """用 workspace-bot11 实际产出的 2026-03-20 复盘作为 ground truth。"""

    @pytest.fixture(scope="class")
    def parsed(self):
        return parse_daily_review(REVIEW_MD_2026_03_20)

    def test_breadth_counts(self, parsed):
        assert parsed["advance_count"] == 662
        assert parsed["decline_count"] == 4786
        assert parsed["flat_count"] == 377

    def test_advance_decline_ratio(self, parsed):
        # 662 / (662 + 4786 + 377) = 662 / 5825
        expected = 662 / 5825
        assert parsed["advance_decline_ratio"] == pytest.approx(expected, rel=1e-9)

    def test_sentiment_delta(self, parsed):
        # 涨停:跌停 32:22 → delta = 10
        assert parsed["sentiment_delta"] == 10

    def test_sentiment_index(self, parsed):
        # "情绪评分 **25** / 100（偏空低迷）"
        assert parsed["sentiment_index"] == 25.0

    def test_max_streak_flagged_as_missing(self, parsed):
        # 2026-03-20 fixture 里 "涨停:跌停 32:22" + "最高连板 **0** 板" 是上游
        # tushare limit_list_d 被 rate-limit 的典型 silent failure, parser 的
        # 一致性检测应把 max_streak 标为 missing (不是 0)。
        assert "max_streak" not in parsed


# --------------------------------------------------------------------------- #
# parse_daily_review: 老格式 fallback (分市场多行)
# --------------------------------------------------------------------------- #


class TestParseDailyReviewLegacyFormat:
    """2026-03-19 是老版手写复盘 MD, 分上证/深证两列, 情绪/连板字段缺失。"""

    @pytest.fixture(scope="class")
    def parsed(self):
        return parse_daily_review(REVIEW_MD_2026_03_19_LEGACY)

    def test_advance_decline_recovered_via_fallback(self, parsed):
        # 上涨: 257+241=498; 下跌: 2075+2661=4736; 平盘: 11+13=24
        assert parsed["advance_count"] == 498
        assert parsed["decline_count"] == 4736
        assert parsed["flat_count"] == 24
        expected = 498 / (498 + 4736 + 24)
        assert parsed["advance_decline_ratio"] == pytest.approx(expected, rel=1e-9)

    def test_sentiment_fields_genuinely_missing(self, parsed):
        # 老 MD 里情绪评分章节写"待补充", 涨停:跌停 不存在
        assert "sentiment_delta" not in parsed
        assert "sentiment_index" not in parsed

    def test_max_streak_missing(self, parsed):
        # 老 MD 完全没有连板统计
        assert "max_streak" not in parsed


class TestExtractMultiMarketBreadth:
    def test_two_markets_sum(self, tmp_path):
        md = tmp_path / "m.md"
        md.write_text(
            "**涨跌家数：**\n"
            "- 上涨：100 家（上证）/ 200 家（深证）\n"
            "- 下跌：500 家（上证）/ 800 家（深证）\n"
            "- 平盘：10 家（上证）/ 20 家（深证）\n",
            encoding="utf-8",
        )
        result = parse_daily_review(str(md))
        assert result["advance_count"] == 300
        assert result["decline_count"] == 1300
        assert result["flat_count"] == 30

    def test_three_markets_sum(self, tmp_path):
        md = tmp_path / "m.md"
        md.write_text(
            "- 上涨：100 家（上证）/ 200 家（深证）/ 50 家（北交所）\n"
            "- 下跌：400 家（上证）/ 500 家（深证）/ 100 家（北交所）\n",
            encoding="utf-8",
        )
        result = parse_daily_review(str(md))
        assert result["advance_count"] == 350
        assert result["decline_count"] == 1000

    def test_primary_format_wins_over_fallback(self, tmp_path):
        # 同一文件里同时出现两种格式 → 主格式优先
        md = tmp_path / "m.md"
        md.write_text(
            "- 上涨：**600** 家 / 下跌：**4000** 家 / 平盘：400 家\n"
            "- 上涨：100 家（上证）/ 200 家（深证）\n"
            "- 下跌：999 家（上证）/ 999 家（深证）\n",
            encoding="utf-8",
        )
        result = parse_daily_review(str(md))
        assert result["advance_count"] == 600  # 主格式的值
        assert result["decline_count"] == 4000


# --------------------------------------------------------------------------- #
# parse_daily_review: 合成最小 MD
# --------------------------------------------------------------------------- #


class TestParseDailyReviewConsistency:
    """数据一致性检测: 涨停>0 + 最高连板=0 是上游数据源失败的信号。

    任何涨停股至少是 1 板, 所以 max_streak=0 与 "有涨停" 矛盾。原因通常是
    daily_review.py 的 mod_limit_up_tracking 在被 tushare 限额 rate-limit
    后静默返回空列表 → 渲染成 "最高连板 0 板"。classifier 应标为 missing
    而不是误打 -2。
    """

    def test_contradiction_marks_streak_missing(self, tmp_path):
        md = tmp_path / "m.md"
        md.write_text(
            "| 涨停:跌停 | 60:15 |\n"
            "- 最高连板：**0** 板\n",
            encoding="utf-8",
        )
        result = parse_daily_review(str(md))
        assert result["sentiment_delta"] == 45  # 60-15
        assert "max_streak" not in result  # 被防御性检测判为 missing

    def test_consistent_zero_limit_up_allows_streak_zero(self, tmp_path):
        # 真实 0 涨停的日子 (极端熊市), max_streak=0 不触发矛盾, 正常保留
        md = tmp_path / "m.md"
        md.write_text(
            "| 涨停:跌停 | 0:30 |\n"
            "- 最高连板：**0** 板\n",
            encoding="utf-8",
        )
        result = parse_daily_review(str(md))
        assert result["sentiment_delta"] == -30
        assert result["max_streak"] == 0  # 一致, 保留

    def test_positive_streak_always_kept(self, tmp_path):
        md = tmp_path / "m.md"
        md.write_text(
            "| 涨停:跌停 | 60:15 |\n"
            "- 最高连板：**4** 板\n",
            encoding="utf-8",
        )
        result = parse_daily_review(str(md))
        assert result["max_streak"] == 4

    def test_no_limit_up_info_streak_still_kept(self, tmp_path):
        # 没有涨停:跌停 行 (老格式) → limit_up_count 未知 → 不触发检测
        md = tmp_path / "m.md"
        md.write_text("- 最高连板：**0** 板\n", encoding="utf-8")
        result = parse_daily_review(str(md))
        assert result["max_streak"] == 0


class TestParseDailyReviewSynthetic:
    def test_missing_fields_return_no_keys(self, tmp_path):
        md = tmp_path / "empty.md"
        md.write_text("# 空复盘\n没有任何字段\n", encoding="utf-8")
        result = parse_daily_review(str(md))
        assert "advance_decline_ratio" not in result
        assert "sentiment_delta" not in result
        assert "sentiment_index" not in result
        assert "max_streak" not in result

    def test_breadth_without_flat(self, tmp_path):
        md = tmp_path / "no_flat.md"
        md.write_text("- 上涨：**100** 家 / 下跌：**50** 家\n", encoding="utf-8")
        result = parse_daily_review(str(md))
        assert result["advance_count"] == 100
        assert result["decline_count"] == 50
        assert result["flat_count"] == 0
        # 100 / (100 + 50 + 0) = 0.6666...
        assert result["advance_decline_ratio"] == pytest.approx(100 / 150)

    def test_breadth_no_bold(self, tmp_path):
        md = tmp_path / "plain.md"
        md.write_text("上涨：600 家 / 下跌：4800 家 / 平盘：400\n", encoding="utf-8")
        result = parse_daily_review(str(md))
        assert result["advance_count"] == 600
        assert result["decline_count"] == 4800

    def test_chinese_colon_variants(self, tmp_path):
        md = tmp_path / "colon.md"
        md.write_text(
            "最高连板: **3** 板\n情绪评分 | 58 / 100（偏多）\n涨停:跌停 | 45:10\n",
            encoding="utf-8",
        )
        result = parse_daily_review(str(md))
        assert result["max_streak"] == 3
        assert result["sentiment_index"] == 58.0
        assert result["sentiment_delta"] == 35

    def test_sentiment_score_float(self, tmp_path):
        md = tmp_path / "f.md"
        md.write_text("情绪评分 | **67.5** / 100", encoding="utf-8")
        result = parse_daily_review(str(md))
        assert result["sentiment_index"] == 67.5


# --------------------------------------------------------------------------- #
# 合成 DataFrame 辅助工厂
# --------------------------------------------------------------------------- #


def _make_index_df(
    n_days: int = 260,
    close_series=None,
    volume_series=None,
    start_date: str = "2025-01-02",
):
    """构造一个合成的指数日线 DF, 用交易日(工作日)作为日期。

    close_series / volume_series 未传时使用常量 100 / 10000。
    """
    dates = pd.bdate_range(start=start_date, periods=n_days).strftime("%Y-%m-%d")
    close = [100.0] * n_days if close_series is None else list(close_series)
    volume = [10000.0] * n_days if volume_series is None else list(volume_series)
    assert len(close) == n_days and len(volume) == n_days
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": volume,
        }
    )


# --------------------------------------------------------------------------- #
# compute_index_ma
# --------------------------------------------------------------------------- #


class TestComputeIndexMa:
    def test_flat_series_all_equal(self):
        df = _make_index_df(n_days=260)
        target = df.iloc[-1]["date"]
        ma = compute_index_ma(df, target)
        assert ma["close"] == 100.0
        assert ma["ma5"] == 100.0
        assert ma["ma20"] == 100.0
        assert ma["ma60"] == 100.0
        assert ma["ma250"] == 100.0

    def test_ascending_series_bull_alignment(self):
        # close 从 1 递增到 260 → MA5 > MA20 > MA60 > MA250
        close = [float(i + 1) for i in range(260)]
        df = _make_index_df(n_days=260, close_series=close)
        target = df.iloc[-1]["date"]
        ma = compute_index_ma(df, target)
        assert ma["close"] == 260.0
        assert ma["ma5"] > ma["ma20"] > ma["ma60"] > ma["ma250"]
        # MA5 = 平均(256..260) = 258
        assert ma["ma5"] == pytest.approx(258.0)
        # MA20 = 平均(241..260) = 250.5
        assert ma["ma20"] == pytest.approx(250.5)

    def test_target_date_not_in_df_raises(self):
        df = _make_index_df(n_days=260)
        with pytest.raises(KeyError, match="not in index data"):
            compute_index_ma(df, "1999-01-01")

    def test_insufficient_history_raises(self):
        # 只给 100 天, 目标日是最后一天 → idx=99 < 249
        df = _make_index_df(n_days=100)
        target = df.iloc[-1]["date"]
        with pytest.raises(ValueError, match="not enough history"):
            compute_index_ma(df, target)

    def test_target_mid_series_still_computes(self):
        # 目标日是第 250 天 (idx=249), 刚好够 MA250
        df = _make_index_df(n_days=260)
        target = df.iloc[249]["date"]
        ma = compute_index_ma(df, target)
        assert ma["close"] == 100.0


# --------------------------------------------------------------------------- #
# compute_volume_ratio
# --------------------------------------------------------------------------- #


class TestComputeVolumeRatio:
    def test_flat_volume_ratio_one(self):
        df = _make_index_df(n_days=30)
        target = df.iloc[-1]["date"]
        assert compute_volume_ratio(df, target) == pytest.approx(1.0)

    def test_volume_spike_last_5_days(self):
        # 前 25 天 volume=1000, 后 5 天 volume=3000
        vol = [1000.0] * 25 + [3000.0] * 5
        df = _make_index_df(n_days=30, volume_series=vol)
        target = df.iloc[-1]["date"]
        # MA5 = 3000
        # MA20 = 平均(idx 10..29) = (15*1000 + 5*3000)/20 = 30000/20 = 1500
        # ratio = 3000 / 1500 = 2.0
        assert compute_volume_ratio(df, target) == pytest.approx(2.0)

    def test_insufficient_history_raises(self):
        df = _make_index_df(n_days=15)
        target = df.iloc[-1]["date"]
        with pytest.raises(ValueError, match="not enough history"):
            compute_volume_ratio(df, target)

    def test_target_not_in_df_raises(self):
        df = _make_index_df(n_days=30)
        with pytest.raises(KeyError, match="not in index data"):
            compute_volume_ratio(df, "1999-01-01")

    def test_zero_volume_raises(self):
        df = _make_index_df(n_days=25, volume_series=[0.0] * 25)
        target = df.iloc[-1]["date"]
        with pytest.raises(ValueError, match="non-positive"):
            compute_volume_ratio(df, target)


# --------------------------------------------------------------------------- #
# compute_pct_change
# --------------------------------------------------------------------------- #


class TestComputePctChange:
    def test_flat_series_zero_pct(self):
        df = _make_index_df(n_days=10)
        target = df.iloc[-1]["date"]
        assert compute_pct_change(df, target) == pytest.approx(0.0)

    def test_positive_pct(self):
        # close 从 100 涨到 103 → +3%
        close = [100.0] * 9 + [103.0]
        df = _make_index_df(n_days=10, close_series=close)
        target = df.iloc[-1]["date"]
        assert compute_pct_change(df, target) == pytest.approx(3.0)

    def test_negative_pct(self):
        close = [100.0] * 9 + [96.85]
        df = _make_index_df(n_days=10, close_series=close)
        target = df.iloc[-1]["date"]
        assert compute_pct_change(df, target) == pytest.approx(-3.15, rel=1e-3)

    def test_first_row_raises(self):
        df = _make_index_df(n_days=10)
        target = df.iloc[0]["date"]
        with pytest.raises(ValueError, match="no previous"):
            compute_pct_change(df, target)

    def test_target_not_found_raises(self):
        df = _make_index_df(n_days=10)
        with pytest.raises(KeyError):
            compute_pct_change(df, "1999-01-01")


# --------------------------------------------------------------------------- #
# fetch_full_market_volume: 缓存合成路径 (不打网络)
# --------------------------------------------------------------------------- #


class TestFetchFullMarketVolumeCacheRead:
    """fetch_full_market_volume 的缓存读取路径 (预写 CSV, 不打网络)。"""

    def _write_cache(self, cache_dir, dates, volumes):
        df = pd.DataFrame({"date": dates, "volume": volumes})
        df.to_csv(os.path.join(cache_dir, "full_market_amount.csv"), index=False)

    def test_reads_existing_cache_when_target_date_present(self, tmp_path):
        cd = str(tmp_path)
        self._write_cache(cd, ["2026-03-23", "2026-03-24", "2026-03-25"], [1000, 1100, 1200])

        df = fetch_full_market_volume(cd, target_date="2026-03-25")
        assert list(df.columns) == ["date", "volume"]
        assert len(df) == 3
        assert df.iloc[-1]["volume"] == pytest.approx(1200.0)

    def test_reads_existing_cache_when_target_date_none(self, tmp_path):
        cd = str(tmp_path)
        self._write_cache(cd, ["2026-03-23", "2026-03-24"], [1000, 1100])
        df = fetch_full_market_volume(cd)  # target_date=None
        assert len(df) == 2

    def test_cache_sorted_by_date(self, tmp_path):
        cd = str(tmp_path)
        self._write_cache(cd, ["2026-03-25", "2026-03-23", "2026-03-24"], [3, 1, 2])
        df = fetch_full_market_volume(cd, target_date="2026-03-25")
        assert list(df["date"]) == ["2026-03-23", "2026-03-24", "2026-03-25"]


class TestFetchFullMarketVolumeTushareStub:
    """Tushare 拉取路径 — monkeypatch tushare_client.call, 验证合成与合并逻辑。"""

    def _fake_call(self, daily_info_payloads):
        """构造一个打桩函数: 根据 params['ts_code'] 返回对应的 DataFrame。

        daily_info_payloads: {ts_code: DataFrame(trade_date, ts_code, amount)}
        """
        def _stub(api_name, params, fields=""):
            assert api_name == "daily_info"
            ts = params["ts_code"]
            return daily_info_payloads[ts].copy()
        return _stub

    def test_combines_sh_market_and_sz_market(self, tmp_path, monkeypatch):
        cd = str(tmp_path)
        # Tushare trade_date 是 YYYYMMDD 字符串
        sh = pd.DataFrame({
            "trade_date": ["20260323", "20260324", "20260325"],
            "ts_code": ["SH_MARKET"] * 3,
            "amount": [9000.0, 9500.0, 9678.0],  # 亿元
        })
        sz = pd.DataFrame({
            "trade_date": ["20260323", "20260324", "20260325"],
            "ts_code": ["SZ_MARKET"] * 3,
            "amount": [11000.0, 11500.0, 12120.0],
        })
        import tushare_client
        monkeypatch.setattr(
            tushare_client, "call",
            self._fake_call({"SH_MARKET": sh, "SZ_MARKET": sz}),
        )

        df = fetch_full_market_volume(cd, refresh=True)
        assert list(df.columns) == ["date", "volume"]
        assert len(df) == 3
        # 2026-03-25: 9678 + 12120 = 21798 亿
        row = df[df["date"] == "2026-03-25"].iloc[0]
        assert row["volume"] == pytest.approx(9678.0 + 12120.0)
        # 写入缓存
        assert os.path.exists(os.path.join(cd, "full_market_amount.csv"))

    def test_inner_join_drops_missing_dates(self, tmp_path, monkeypatch):
        cd = str(tmp_path)
        sh = pd.DataFrame({
            "trade_date": ["20260323", "20260324", "20260325"],
            "ts_code": ["SH_MARKET"] * 3,
            "amount": [1.0, 2.0, 3.0],
        })
        # 深证只有 2 天
        sz = pd.DataFrame({
            "trade_date": ["20260324", "20260325"],
            "ts_code": ["SZ_MARKET"] * 2,
            "amount": [10.0, 20.0],
        })
        import tushare_client
        monkeypatch.setattr(
            tushare_client, "call",
            self._fake_call({"SH_MARKET": sh, "SZ_MARKET": sz}),
        )

        df = fetch_full_market_volume(cd, refresh=True)
        assert len(df) == 2
        assert set(df["date"]) == {"2026-03-24", "2026-03-25"}

    def test_target_date_missing_triggers_refetch(self, tmp_path, monkeypatch):
        """缓存不含 target_date 时应重新拉取 (老 akshare 版本漏掉的 bug)。"""
        cd = str(tmp_path)
        # 预写缓存, 只含 03-23
        stale = pd.DataFrame({"date": ["2026-03-23"], "volume": [999.0]})
        stale.to_csv(os.path.join(cd, "full_market_amount.csv"), index=False)

        sh = pd.DataFrame({
            "trade_date": ["20260323", "20260324"],
            "ts_code": ["SH_MARKET"] * 2,
            "amount": [100.0, 150.0],
        })
        sz = pd.DataFrame({
            "trade_date": ["20260323", "20260324"],
            "ts_code": ["SZ_MARKET"] * 2,
            "amount": [200.0, 250.0],
        })
        import tushare_client
        monkeypatch.setattr(
            tushare_client, "call",
            self._fake_call({"SH_MARKET": sh, "SZ_MARKET": sz}),
        )

        # 请求 target_date=2026-03-24 → 应 refetch
        df = fetch_full_market_volume(cd, target_date="2026-03-24")
        assert len(df) == 2  # 不是 stale 的 1 行
        assert df[df["date"] == "2026-03-24"].iloc[0]["volume"] == pytest.approx(400.0)


class TestBuildRawWithFullMarketDf:
    def test_injected_full_market_df_used_for_volume(self):
        # 构造 hs300/csi1000 DF 用于 MA, 另构造 full_market_df 用于 volume
        hs_df = _make_index_df(n_days=260, volume_series=[1000.0] * 260)
        cs_df = _make_index_df(n_days=260, volume_series=[1000.0] * 260)
        # 全市场 DF 的 volume 非常放量: 最近 5 日 3x 于前 15 天
        vol_series = [100.0] * 255 + [300.0] * 5
        full_df = _make_index_df(n_days=260, volume_series=vol_series)

        target = hs_df.iloc[-1]["date"]
        r = build_raw_market_data(
            date=target,
            md_path=None,
            hs300_df=hs_df,
            csi1000_df=cs_df,
            full_market_df=full_df,
        )
        # volume_ratio 应该基于 full_df 算出: 5日均 300, 20日均 = (15*100+5*300)/20 = 150
        # ratio = 300 / 150 = 2.0
        assert r.volume_ratio_5_20 == pytest.approx(2.0)

    def test_no_full_market_df_falls_back_to_hs300_with_warning(self):
        hs_df = _make_index_df(n_days=260, volume_series=[2000.0] * 260)
        cs_df = _make_index_df(n_days=260, volume_series=[2000.0] * 260)
        target = hs_df.iloc[-1]["date"]
        r = build_raw_market_data(
            date=target,
            md_path=None,
            hs300_df=hs_df,
            csi1000_df=cs_df,
            full_market_df=None,
            cache_dir=None,  # 不触发自动 fetch
        )
        # fallback 到 hs300, volume_ratio 算出 1.0 (flat)
        assert r.volume_ratio_5_20 == pytest.approx(1.0)
        assert any("hs300_proxy" in w for w in r.warnings)


# --------------------------------------------------------------------------- #
# fetch_index_daily: 只测缓存读取路径, 不打网络
# --------------------------------------------------------------------------- #


class TestFetchIndexDailyCache:
    def test_reads_existing_cache_when_target_date_present(self, tmp_path):
        cache_dir = str(tmp_path)
        # 手工写一个缓存 CSV (新前缀: index_ts_{name}.csv)
        df_in = _make_index_df(n_days=10)
        cache_path = os.path.join(cache_dir, "index_ts_hs300.csv")
        df_in.to_csv(cache_path, index=False)

        target = df_in.iloc[-1]["date"]
        df_out = fetch_index_daily("hs300", cache_dir, target_date=target)
        assert len(df_out) == 10
        assert list(df_out.columns) == ["date", "open", "high", "low", "close", "volume"]
        assert df_out.iloc[0]["date"] == df_in.iloc[0]["date"]

    def test_reads_existing_cache_when_target_date_none(self, tmp_path):
        cache_dir = str(tmp_path)
        df_in = _make_index_df(n_days=5)
        df_in.to_csv(os.path.join(cache_dir, "index_ts_csi1000.csv"), index=False)

        df_out = fetch_index_daily("csi1000", cache_dir)  # target_date=None
        assert len(df_out) == 5

    def test_cache_sorted_by_date(self, tmp_path):
        cache_dir = str(tmp_path)
        # 写入一个乱序的缓存
        df_in = _make_index_df(n_days=10)
        df_in = df_in.sample(frac=1, random_state=42).reset_index(drop=True)
        df_in.to_csv(os.path.join(cache_dir, "index_ts_hs300.csv"), index=False)

        df_out = fetch_index_daily("hs300", cache_dir)
        dates = df_out["date"].tolist()
        assert dates == sorted(dates)

    def test_unknown_name_raises(self, tmp_path):
        with pytest.raises(KeyError, match="unknown index name"):
            fetch_index_daily("not_a_real_index", str(tmp_path))


class TestFetchIndexDailyTushareStub:
    """Tushare 拉取路径 — monkeypatch tushare_client.call, 验证列规范化。"""

    def test_fetches_and_normalizes_columns(self, tmp_path, monkeypatch):
        cd = str(tmp_path)
        # Tushare 原生列: ts_code, trade_date (YYYYMMDD), open, high, low, close, vol
        raw = pd.DataFrame({
            "ts_code": ["000300.SH"] * 3,
            "trade_date": ["20260410", "20260413", "20260414"],
            "open": [4600.0, 4620.0, 4676.12],
            "high": [4650.0, 4680.0, 4701.28],
            "low": [4580.0, 4610.0, 4654.85],
            "close": [4636.57, 4646.15, 4701.28],
            "vol": [2.08e8, 1.97e8, 2.05e8],
        })
        import tushare_client

        def _stub(api_name, params, fields=""):
            assert api_name == "index_daily"
            assert params["ts_code"] == "000300.SH"
            return raw.copy()

        monkeypatch.setattr(tushare_client, "call", _stub)

        df = fetch_index_daily("hs300", cd, refresh=True)
        assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
        assert list(df["date"]) == ["2026-04-10", "2026-04-13", "2026-04-14"]
        assert df.iloc[-1]["close"] == pytest.approx(4701.28)
        assert df.iloc[-1]["volume"] == pytest.approx(2.05e8)
        # 写入了新前缀缓存
        assert os.path.exists(os.path.join(cd, "index_ts_hs300.csv"))

    def test_target_date_missing_triggers_refetch(self, tmp_path, monkeypatch):
        cd = str(tmp_path)
        # 预写陈旧缓存, 只含 04-10
        stale = pd.DataFrame({
            "date": ["2026-04-10"],
            "open": [4600.0], "high": [4650.0], "low": [4580.0],
            "close": [4636.57], "volume": [2.08e8],
        })
        stale.to_csv(os.path.join(cd, "index_ts_hs300.csv"), index=False)

        raw = pd.DataFrame({
            "ts_code": ["000300.SH"] * 2,
            "trade_date": ["20260410", "20260414"],
            "open": [4600.0, 4676.12],
            "high": [4650.0, 4701.28],
            "low":  [4580.0, 4654.85],
            "close": [4636.57, 4701.28],
            "vol":   [2.08e8, 2.05e8],
        })
        import tushare_client
        monkeypatch.setattr(
            tushare_client, "call",
            lambda api, params, fields="": raw.copy(),
        )

        # 请求 target_date=2026-04-14 → 应 refetch (陈旧缓存里没有)
        df = fetch_index_daily("hs300", cd, target_date="2026-04-14")
        assert "2026-04-14" in set(df["date"].values)
        assert len(df) == 2


# --------------------------------------------------------------------------- #
# build_raw_market_data: 完整编排器
# --------------------------------------------------------------------------- #


class TestBuildRawMarketData:
    def test_with_fixture_md_and_injected_index_df(self):
        df = _make_index_df(n_days=260)
        # 目标日必须同时存在于 df 和能读到 MD
        target = df.iloc[-1]["date"]
        result = build_raw_market_data(
            date=target,
            md_path=REVIEW_MD_2026_03_20,
            hs300_df=df,
            csi1000_df=df,
        )
        assert isinstance(result, RawMarketData)
        # MD 侧
        assert result.sentiment_index == 25.0
        assert result.sentiment_delta == 10
        # max_streak 被一致性检测标为 missing (fixture 里 涨停>0 但最高连板=0)
        assert result.max_streak is None
        assert result.advance_decline_ratio == pytest.approx(662 / 5825)
        # 指数侧
        assert result.hs300 is not None
        assert result.csi1000 is not None
        assert result.volume_ratio_5_20 == pytest.approx(1.0)
        # pct_change 字段在平坦序列下为 0
        assert result.hs300_pct_change == pytest.approx(0.0)
        assert result.csi1000_pct_change == pytest.approx(0.0)
        # streak_height 作为 missing 维度记录
        assert result.missing_dims == ["streak_height"]

    def test_md_path_none_marks_md_dims_missing(self):
        df = _make_index_df(n_days=260)
        target = df.iloc[-1]["date"]
        result = build_raw_market_data(
            date=target,
            md_path=None,
            hs300_df=df,
            csi1000_df=df,
        )
        assert set(result.missing_dims) >= {
            "advance_decline",
            "sentiment_delta",
            "sentiment_index",
            "streak_height",
        }
        assert result.hs300 is not None  # 指数侧仍然成功

    def test_missing_index_df_marks_ma_and_volume_missing(self, tmp_path):
        md = tmp_path / "m.md"
        md.write_text("情绪评分 | 58 / 100\n", encoding="utf-8")
        result = build_raw_market_data(
            date="2025-06-01",
            md_path=str(md),
            hs300_df=None,
            csi1000_df=None,
            cache_dir=None,  # 不触发 Tushare
        )
        assert "ma_position" in result.missing_dims
        assert "volume_trend" in result.missing_dims
        assert result.sentiment_index == 58.0

    def test_target_date_not_in_index_df_graceful(self):
        df = _make_index_df(n_days=260)
        result = build_raw_market_data(
            date="1999-01-01",  # 不在 df 中
            md_path=REVIEW_MD_2026_03_20,
            hs300_df=df,
            csi1000_df=df,
        )
        assert result.hs300 is None
        assert result.csi1000 is None
        assert "ma_position" in result.missing_dims
        assert "volume_trend" in result.missing_dims
        # 有 warning 而非 raise
        assert any("hs300_ma_failed" in w for w in result.warnings)

    def test_short_history_df_degrades_gracefully(self):
        # 只给 30 天, 够算 volume 但不够算 MA250
        df = _make_index_df(n_days=30)
        target = df.iloc[-1]["date"]
        result = build_raw_market_data(
            date=target,
            md_path=None,
            hs300_df=df,
            csi1000_df=df,
        )
        assert result.hs300 is None
        assert "ma_position" in result.missing_dims
        # volume 能算出来 (30 天 >= 20)
        assert result.volume_ratio_5_20 == pytest.approx(1.0)
        assert "volume_trend" not in result.missing_dims
