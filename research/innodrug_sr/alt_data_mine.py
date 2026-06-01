"""Mine additional non-retail data for innovative-drug timing.

Fetches cached PIT simworld data beyond the first five rounds, then tests whether
any market/rate/growth-style dimension can upgrade the existing candidate.
"""
from __future__ import annotations

import json
import time
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests


BASE = Path("/home/rooot/agent_invest_lab/research/innodrug_sr")
DATA = BASE / "data"
MCP = "http://127.0.0.1:18078/mcp"
SIM = "2026-05-26 15:30:00"
START = "2019-04-23"
END = "2026-05-26"
ETF_START = "2024-08-01"
COMMISSION = 0.0005
FORWARD_DAYS = 20
WARMUP = 120
OOS_START = "2023-01-01"


def call(tool: str, args: dict[str, Any], retries: int = 4) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    last_error: str | None = None
    for _ in range(retries):
        try:
            response = requests.post(
                MCP,
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                data=json.dumps(payload, ensure_ascii=False),
                timeout=120,
            )
            response.raise_for_status()
            text = response.text
            if text.startswith("event:"):
                for line in text.splitlines():
                    if line.startswith("data:"):
                        text = line[5:].strip()
                        break
            result_text = json.loads(text)["result"]["content"][0]["text"]
            result = json.loads(result_text)
            if isinstance(result, dict) and result.get("error"):
                last_error = f"{tool}: {result.get('message') or result.get('error')}"
                time.sleep(2)
                continue
            return result
        except Exception as exc:  # noqa: BLE001
            last_error = f"{tool}: {exc}"
            time.sleep(2)
    raise RuntimeError(last_error or f"{tool} failed")


def cache_csv(path: Path, frame: pd.DataFrame, force: bool) -> pd.DataFrame:
    if path.exists() and not force:
        return pd.read_csv(path)
    frame.to_csv(path, index=False)
    return frame


def fetch_market_temperature(force: bool = False) -> pd.DataFrame:
    path = DATA / "alt_market_temperature.csv"
    if path.exists() and not force:
        return pd.read_csv(path)
    result = call("market_temperature", {"simulated_datetime": SIM, "start_date": START, "end_date": END})
    rows = result["items"][0].get("历史序列", [])
    frame = pd.DataFrame(rows).rename(columns={
        "交易日期": "date",
        "综合得分": "temp_score",
        "三月分位数": "temp_3m_pct",
        "创新高低得分": "newhigh_score",
        "股债回报差得分": "equity_bond_score",
        "升贴水率得分": "basis_score",
        "VIX得分": "vix_score",
        "价量偏离得分": "price_volume_score",
        "北向RSI得分": "north_rsi_score",
    })
    return cache_csv(path, frame, True)


def fetch_index_quote(force: bool = False) -> pd.DataFrame:
    path = DATA / "alt_index_quote.csv"
    if path.exists() and not force:
        return pd.read_csv(path)
    symbols = ["000688", "399006", "000300", "000852", "000905", "931152"]
    result = call("market_index_quote", {
        "market": "cn",
        "symbols": symbols,
        "simulated_datetime": SIM,
        "start_date": START,
        "end_date": END,
    })
    rows: list[dict[str, Any]] = []
    for item in result.get("items", []):
        symbol = item.get("指数标识")
        for record in item.get("行情记录", []):
            rows.append({
                "date": record.get("日期"),
                "symbol": symbol,
                "close": record.get("收盘"),
                "ret_pct": record.get("日涨跌幅"),
            })
    frame = pd.DataFrame(rows)
    return cache_csv(path, frame, True)


def fetch_gzxjb(force: bool = False) -> pd.DataFrame:
    path = DATA / "alt_gzxjb.csv"
    if path.exists() and not force:
        return pd.read_csv(path)
    result = call("market_index_gzxjb", {
        "symbols": ["000300", "000852"],
        "simulated_datetime": SIM,
        "calmodel": "pe",
        "caltype": 1,
        "start_date": START,
        "end_date": END,
    })
    rows: list[dict[str, Any]] = []
    for item in result.get("items", []):
        symbol = item.get("指数标识")
        for record in item.get("历史序列", []):
            row = {"symbol": symbol, **record}
            rows.append(row)
    frame = pd.DataFrame(rows).rename(columns={
        "交易日期": "date",
        "股债性价比": "erp",
        "近1年百分位": "erp_pct_1y",
        "近3年百分位": "erp_pct_3y",
        "近5年百分位": "erp_pct_5y",
        "近10年百分位": "erp_pct_10y",
        "近1年评级": "erp_rating_1y",
        "近3年评级": "erp_rating_3y",
        "近5年评级": "erp_rating_5y",
        "近10年评级": "erp_rating_10y",
    })
    return cache_csv(path, frame, True)


def fetch_vix(force: bool = False) -> pd.DataFrame:
    path = DATA / "alt_vix50.csv"
    if path.exists() and not force:
        return pd.read_csv(path)
    result = call("macro_50etf_vix", {"simulated_datetime": SIM, "start_date": START, "end_date": END})
    frame = pd.DataFrame(result.get("items", [])).rename(columns={"交易日期": "date", "ivix": "vix50"})
    return cache_csv(path, frame, True)


def fetch_us_yield(force: bool = False) -> pd.DataFrame:
    path = DATA / "alt_us_yield.csv"
    if path.exists() and not force:
        return pd.read_csv(path)
    result = call("bond_yield_curve", {
        "simulated_datetime": SIM,
        "curve_type": "us",
        "maturities": ["2Y", "10Y"],
        "start_date": START,
        "end_date": END,
    })
    series: list[pd.DataFrame] = []
    for item in result.get("items", []):
        maturity = str(item.get("期限", "")).lower()
        records = pd.DataFrame(item.get("收益率记录", []))
        if records.empty:
            continue
        value_column = [column for column in records.columns if column != "日期"][0]
        records = records.rename(columns={"日期": "date", value_column: f"us_{maturity}"})
        series.append(records[["date", f"us_{maturity}"]])
    frame = series[0]
    for next_frame in series[1:]:
        frame = frame.merge(next_frame, on="date", how="outer")
    return cache_csv(path, frame, True)


def fetch_quant_factor(force: bool = False) -> pd.DataFrame:
    path = DATA / "alt_quant_factor.csv"
    if path.exists() and not force:
        return pd.read_csv(path)
    result = call("quant_factor", {"simulated_datetime": SIM, "history_days": 1200})
    rows: list[dict[str, Any]] = []
    for factor, payload in result.get("factors", {}).items():
        for record in payload.get("仓位序列", []):
            rows.append({"date": record.get("日期"), "factor": factor, "position": record.get("仓位")})
    frame = pd.DataFrame(rows)
    return cache_csv(path, frame, True)


def fetch_fund_snapshot(force: bool = False) -> pd.DataFrame:
    path = DATA / "alt_fund_top_holdings_current.csv"
    if path.exists() and not force:
        return pd.read_csv(path)
    result = call("fund_top_holdings", {"fund_codes": ["159992", "516080", "159839"], "simulated_datetime": SIM})
    rows: list[dict[str, Any]] = []
    for item in result.get("items", []):
        for record in item.get("重仓股列表", []):
            rows.append({
                "fund_code": item.get("基金代码"),
                "report_date": item.get("报告日期"),
                "stock_code": record.get("股票代码"),
                "stock_name": record.get("股票名称"),
                "weight": record.get("占净值比例"),
            })
    frame = pd.DataFrame(rows)
    return cache_csv(path, frame, True)


def fetch_all(force: bool = False) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    jobs = [
        fetch_market_temperature,
        fetch_index_quote,
        fetch_gzxjb,
        fetch_vix,
        fetch_us_yield,
        fetch_quant_factor,
        fetch_fund_snapshot,
    ]
    for job in jobs:
        frame = job(force)
        print(f"cached {job.__name__:<26} rows={len(frame)}")


def read_close(file_name: str) -> pd.Series:
    price = pd.read_csv(DATA / file_name, parse_dates=[0])
    price.columns = [column.lower() for column in price.columns]
    price = price.set_index(price.columns[0]).sort_index()
    price.index = pd.to_datetime(price.index).normalize()
    return price["close"].astype(float)


def rolling_z(series: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    min_obs = min_periods or max(20, window // 3)
    mean = series.rolling(window, min_periods=min_obs).mean()
    std = series.rolling(window, min_periods=min_obs).std()
    return (series - mean) / std.replace(0, np.nan)


def rolling_rank(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=max(20, window // 3)).rank(pct=True)


def metrics(returns: pd.Series) -> dict[str, float]:
    returns = returns.dropna()
    bars = len(returns)
    if bars == 0:
        return {"ann": 0.0, "shp": 0.0, "dd": 0.0, "cal": 0.0}
    equity = (1 + returns).cumprod()
    total_return = float(equity.iloc[-1] - 1)
    annualized = float((1 + total_return) ** (252 / bars) - 1)
    std = float(returns.std(ddof=1)) if bars > 1 else 0.0
    sharpe = float(returns.mean() / std * np.sqrt(252)) if std > 0 else 0.0
    drawdown = float((equity / equity.cummax() - 1).min())
    calmar = float(annualized / abs(drawdown)) if drawdown < 0 else 0.0
    return {"ann": annualized, "shp": sharpe, "dd": drawdown, "cal": calmar}


def position_from_score(score: pd.Series, threshold: float, positive_is_long: bool = True, neutral: float = 0.5) -> pd.Series:
    state = pd.Series(np.nan, index=score.index)
    if positive_is_long:
        state = state.where(~(score > threshold), 1.0)
        state = state.where(~(score < -threshold), 0.0)
    else:
        state = state.where(~(score > threshold), 0.0)
        state = state.where(~(score < -threshold), 1.0)
    return state.ffill().fillna(neutral)


def backtest(close: pd.Series, signal: pd.Series, warmup: int = WARMUP) -> dict[str, float]:
    signal = signal.clip(0.0, 1.0).reindex(close.index).ffill().fillna(0.0)
    returns = close.pct_change().fillna(0.0)
    position = signal.shift(1).fillna(0.0)
    turnover = position.diff().abs().fillna(0.0)
    strategy_returns = position * returns - turnover * COMMISSION
    strategy_returns = strategy_returns.iloc[warmup:]
    benchmark_returns = returns.iloc[warmup:]
    position_eval = position.iloc[warmup:]
    turnover_eval = turnover.iloc[warmup:]
    split = len(strategy_returns) // 2
    monthly_excess = (strategy_returns - benchmark_returns).resample("ME").apply(lambda values: (1 + values).prod() - 1)
    strategy_metrics = metrics(strategy_returns)
    benchmark_metrics = metrics(benchmark_returns)
    return {
        **strategy_metrics,
        "bh_ann": benchmark_metrics["ann"],
        "bh_shp": benchmark_metrics["shp"],
        "bh_cal": benchmark_metrics["cal"],
        "h1_cal": metrics(strategy_returns.iloc[:split])["cal"],
        "h2_cal": metrics(strategy_returns.iloc[split:])["cal"],
        "exp": float(position_eval.mean()),
        "flips_yr": float((turnover_eval > 0.001).sum() / (len(strategy_returns) / 252)) if len(strategy_returns) else 0.0,
        "month_win": float((monthly_excess > 0).mean()),
        "cum_excess": float((1 + monthly_excess).prod() - 1),
    }


def load_alt_features(index: pd.DatetimeIndex) -> pd.DataFrame:
    features = pd.DataFrame(index=index)

    mt = pd.read_csv(DATA / "alt_market_temperature.csv", parse_dates=["date"]).set_index("date").sort_index()
    mt.index = pd.to_datetime(mt.index).normalize()
    for column in ["temp_score", "temp_3m_pct", "newhigh_score", "equity_bond_score", "basis_score", "vix_score", "price_volume_score"]:
        series = mt[column].astype(float).reindex(index, method="ffill")
        features[f"{column}_z60"] = rolling_z(series, 60)
        features[column] = series

    quote = pd.read_csv(DATA / "alt_index_quote.csv", parse_dates=["date"])
    quote["date"] = pd.to_datetime(quote["date"]).dt.normalize()
    pivot = quote.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    pivot = pivot.reindex(index, method="ffill")
    symbol_map = {
        "000688.SH": "kc50",
        "399006.SZ": "chinext",
        "000300.SH": "hs300",
        "000852.SH": "zz1000",
        "000905.SH": "zz500",
        "931152.SH": "index931152",
    }
    for symbol, name in symbol_map.items():
        if symbol not in pivot:
            continue
        close = pivot[symbol].astype(float).reindex(index, method="ffill")
        features[f"{name}_mom20"] = close.pct_change(20)
        features[f"{name}_mom60"] = close.pct_change(60)
        features[f"{name}_dd60"] = close / close.rolling(60, min_periods=20).max() - 1
    if {"000688.SH", "399006.SZ", "000300.SH"}.issubset(set(pivot.columns)):
        kc50 = pivot["000688.SH"].astype(float).reindex(index, method="ffill")
        chinext = pivot["399006.SZ"].astype(float).reindex(index, method="ffill")
        hs300 = pivot["000300.SH"].astype(float).reindex(index, method="ffill")
        features["growth_rel20"] = ((kc50 / kc50.shift(20) - 1) + (chinext / chinext.shift(20) - 1)) / 2 - (hs300 / hs300.shift(20) - 1)
        features["growth_rel60"] = ((kc50 / kc50.shift(60) - 1) + (chinext / chinext.shift(60) - 1)) / 2 - (hs300 / hs300.shift(60) - 1)
        features["growth_mom_combo"] = (
            rolling_z(kc50.pct_change(20), 120) + rolling_z(chinext.pct_change(20), 120) + rolling_z(hs300.pct_change(20), 120)
        ) / 3

    gzxjb = pd.read_csv(DATA / "alt_gzxjb.csv", parse_dates=["date"])
    gzxjb["date"] = pd.to_datetime(gzxjb["date"]).dt.normalize()
    for symbol, prefix in [("000300.SH", "hs300"), ("000852.SH", "zz1000")]:
        block = gzxjb[gzxjb["symbol"] == symbol].set_index("date").sort_index()
        for column in ["erp", "erp_pct_1y", "erp_pct_3y", "erp_pct_10y"]:
            if column in block:
                features[f"{prefix}_{column}"] = block[column].astype(float).reindex(index, method="ffill")

    vix = pd.read_csv(DATA / "alt_vix50.csv", parse_dates=["date"]).set_index("date").sort_index()
    vix.index = pd.to_datetime(vix.index).normalize()
    vix50 = vix["vix50"].astype(float).reindex(index, method="ffill")
    features["vix50"] = vix50
    features["vix50_z252"] = rolling_z(vix50, 252)
    features["vix50_pct252"] = rolling_rank(vix50, 252)
    features["vix50_chg20"] = vix50.diff(20)

    us = pd.read_csv(DATA / "alt_us_yield.csv", parse_dates=["date"]).set_index("date").sort_index()
    us.index = pd.to_datetime(us.index).normalize()
    us = us.reindex(index, method="ffill")
    if "us_10y" in us:
        features["us10y"] = us["us_10y"].astype(float)
        features["us10y_chg20"] = features["us10y"].diff(20)
        features["us10y_z120"] = rolling_z(features["us10y"], 120)
    if {"us_10y", "us_2y"}.issubset(us.columns):
        features["us_2y10y_slope"] = us["us_10y"].astype(float) - us["us_2y"].astype(float)
        features["us_slope_chg20"] = features["us_2y10y_slope"].diff(20)

    quant = pd.read_csv(DATA / "alt_quant_factor.csv", parse_dates=["date"])
    quant["date"] = pd.to_datetime(quant["date"]).dt.normalize()
    qpivot = quant.pivot_table(index="date", columns="factor", values="position", aggfunc="last").sort_index()
    qpivot = qpivot.reindex(index, method="ffill")
    for column in qpivot.columns:
        features[f"qf_{column}"] = qpivot[column].astype(float)
    qcols = [column for column in qpivot.columns if column.startswith("market_retail")]
    if qcols:
        features["qf_market_retail_avg"] = qpivot[qcols].astype(float).mean(axis=1)
    erp_cols = [column for column in qpivot.columns if column.endswith("erp_vix_timing")]
    if erp_cols:
        features["qf_erp_vix_avg"] = qpivot[erp_cols].astype(float).mean(axis=1)

    return features


def load_retail_sector_features(index: pd.DatetimeIndex) -> pd.DataFrame:
    sr = pd.read_csv(BASE / "innodrug_sr_daily.csv", parse_dates=["date"])
    sr = sr[sr["persona"] == "个人"].set_index("date").sort_index()
    sr.index = pd.to_datetime(sr.index).normalize()
    sr["imbalance"] = (sr["applied"] - sr["redeemed"]) / (sr["applied"] + sr["redeemed"]).replace(0, np.nan)
    sr_aligned = sr.reindex(index, method="ffill")
    retail = pd.DataFrame(index=index)
    retail["retail_imb_z15_60"] = rolling_z(sr_aligned["imbalance"].rolling(15, min_periods=8).sum(), 60)
    retail["retail_net_z10_60"] = rolling_z(sr_aligned["net"].rolling(10, min_periods=5).sum(), 60)

    sector_factor = pd.read_csv(DATA / "sector_BK000208_factor.csv", parse_dates=["date"]).set_index("date").sort_index()
    sector_detail = pd.read_csv(DATA / "sector_BK000208_factor_detail.csv", parse_dates=["date"]).set_index("date").sort_index()
    sector = sector_factor.join(sector_detail)
    retail["ir_mom_z60"] = rolling_z(sector["信息比率动量"], 60).reindex(index, method="ffill")
    retail["conc_z60"] = rolling_z(sector["集中度分位"], 60).reindex(index, method="ffill")
    return retail


def feature_ic_table(close: pd.Series, features: pd.DataFrame, feature_columns: list[str], label: str) -> pd.DataFrame:
    panel = features.copy()
    panel["fwd20"] = close.shift(-FORWARD_DAYS) / close - 1
    rows = []
    for column in feature_columns:
        sample = panel[[column, "fwd20"]].dropna()
        if len(sample) < 80:
            continue
        rows.append({"sample": label, "feature": column, "spearman_fwd20": sample[column].corr(sample["fwd20"], method="spearman"), "n": len(sample)})
    return pd.DataFrame(rows).sort_values("spearman_fwd20")


def candidate_signals(features: pd.DataFrame) -> dict[str, pd.Series]:
    signals: dict[str, pd.Series] = {}
    signals["growth_mom_combo_thr0"] = position_from_score(features["growth_mom_combo"], 0.0, True)
    signals["growth_rel60_thr0"] = position_from_score(rolling_z(features["growth_rel60"], 120), 0.0, True)
    signals["risk_cool_temp_z60"] = position_from_score(features["temp_score_z60"], 0.75, False)
    signals["risk_hot_derisk_temp_z60"] = position_from_score(features["temp_score_z60"], 1.25, False, neutral=1.0)
    signals["vix_two_tail_z252"] = position_from_score(features["vix50_z252"], 1.5, True, neutral=0.5)
    signals["vix_complacency_derisk"] = position_from_score(-features["vix50_z252"], 1.5, False, neutral=1.0)
    signals["us10y_down20"] = position_from_score(-rolling_z(features["us10y_chg20"], 120), 0.25, True)
    signals["erp_hs300_pct3y"] = (features["hs300_erp_pct_3y"] / 100).clip(0, 1)
    signals["erp_zz1000_pct3y"] = (features["zz1000_erp_pct_3y"] / 100).clip(0, 1)
    if "qf_market_retail_avg" in features:
        signals["qf_market_retail_avg"] = features["qf_market_retail_avg"]
    if "qf_erp_vix_avg" in features:
        signals["qf_erp_vix_avg"] = features["qf_erp_vix_avg"]
    if {"qf_market_retail_avg", "qf_erp_vix_avg"}.issubset(features.columns):
        signals["qf_market_retail_x_erpvix"] = (features["qf_market_retail_avg"] + features["qf_erp_vix_avg"]) / 2

    risk_on = (
        0.40 * rolling_z(features["growth_rel60"], 120)
        + 0.30 * features["growth_mom_combo"]
        - 0.20 * features["temp_score_z60"]
        - 0.10 * rolling_z(features["us10y_chg20"], 120)
    )
    signals["alt_risk_on_score"] = position_from_score(risk_on, 0.0, True)
    signals["alt_risk_on_soft"] = (0.5 + risk_on / 4).clip(0, 1)
    return signals


def sweep_alt_combos(close: pd.Series, features: pd.DataFrame, base_signal: pd.Series | None = None) -> pd.DataFrame:
    columns = ["growth_rel60", "growth_mom_combo", "temp_score_z60", "vix50_z252", "us10y_chg20"]
    z = pd.DataFrame(index=features.index)
    z["growth_rel60"] = rolling_z(features["growth_rel60"], 120)
    z["growth_mom_combo"] = features["growth_mom_combo"]
    z["temp_score_z60"] = -features["temp_score_z60"]
    z["vix50_z252"] = features["vix50_z252"]
    z["us10y_chg20"] = -rolling_z(features["us10y_chg20"], 120)
    rows = []
    weights = [0.0, 0.5, 1.0]
    for wg, wm, wt, wv, wu in product(weights, repeat=5):
        if wg + wm + wt + wv + wu == 0:
            continue
        raw = (wg * z["growth_rel60"] + wm * z["growth_mom_combo"] + wt * z["temp_score_z60"] + wv * z["vix50_z252"] + wu * z["us10y_chg20"]) / (wg + wm + wt + wv + wu)
        for threshold in [0.0, 0.25, 0.5]:
            signal = position_from_score(raw, threshold, True)
            if base_signal is not None:
                signal = signal * base_signal.reindex(signal.index).ffill().fillna(0.5)
            result = backtest(close, signal)
            rows.append({
                "wgrowth_rel": wg,
                "wgrowth_mom": wm,
                "wtemp_cool": wt,
                "wvix_panic": wv,
                "wus10y_down": wu,
                "thr": threshold,
                **result,
            })
    return pd.DataFrame(rows)


def long_sector_close() -> pd.Series:
    sector = pd.read_csv(DATA / "sector_BK000208_market.csv", parse_dates=["date"]).set_index("date").sort_index()
    sector.index = pd.to_datetime(sector.index).normalize()
    returns = sector["ret_pct"].astype(float) / 100
    return (1 + returns).cumprod()


def print_backtest_table(close: pd.Series, features: pd.DataFrame, label: str) -> pd.DataFrame:
    rows = []
    for name, signal in candidate_signals(features).items():
        result = backtest(close, signal)
        result["factor"] = name
        result["sample"] = label
        rows.append(result)
    table = pd.DataFrame(rows).sort_values("cal", ascending=False)
    print(f"══ Candidate alt-data timing on {label} ══")
    print(table[["factor", "ann", "shp", "cal", "dd", "bh_ann", "bh_cal", "h1_cal", "h2_cal", "exp", "flips_yr", "month_win", "cum_excess"]].to_string(index=False, float_format=lambda value: f"{value:+.3f}"))
    print()
    return table


def main() -> None:
    fetch_all(force=False)
    print()

    sector_close = long_sector_close()
    sector_features = load_alt_features(sector_close.index)
    feature_columns = [
        "growth_rel60", "growth_mom_combo", "temp_score", "temp_score_z60", "vix_score_z60", "vix50_z252",
        "hs300_erp_pct_3y", "zz1000_erp_pct_3y", "us10y_chg20", "us_2y10y_slope", "qf_market_retail_avg", "qf_erp_vix_avg",
    ]
    feature_columns = [column for column in feature_columns if column in sector_features]
    print("══ Alt feature IC vs fwd20 on 7y BK000208 sector close ══")
    print(feature_ic_table(sector_close, sector_features, feature_columns, "sector7y").to_string(index=False, float_format=lambda value: f"{value:+.3f}"))
    print()
    sector_table = print_backtest_table(sector_close, sector_features, "BK000208 sector 2019-2026")

    close_159992 = read_close("159992.SZ.csv").loc[ETF_START:]
    etf_features = load_alt_features(close_159992.index)
    retail_features = load_retail_sector_features(close_159992.index)
    etf_features = etf_features.join(retail_features)
    print("══ Alt feature IC vs fwd20 on 159992 ETF window ══")
    etf_columns = feature_columns + ["retail_imb_z15_60", "retail_net_z10_60", "ir_mom_z60", "conc_z60"]
    print(feature_ic_table(close_159992, etf_features, [column for column in etf_columns if column in etf_features], "159992").to_string(index=False, float_format=lambda value: f"{value:+.3f}"))
    print()
    etf_table = print_backtest_table(close_159992, etf_features, "159992 ETF 2024-2026")

    base_combo = position_from_score(
        etf_features["retail_imb_z15_60"] - 0.5 * etf_features["ir_mom_z60"] - 0.5 * etf_features["conc_z60"],
        1.0,
        positive_is_long=False,
    )
    alt_score = (
        0.40 * rolling_z(etf_features["growth_rel60"], 120)
        + 0.30 * etf_features["growth_mom_combo"]
        - 0.20 * etf_features["temp_score_z60"]
        - 0.10 * rolling_z(etf_features["us10y_chg20"], 120)
    )
    alt_gate = position_from_score(alt_score, 0.0, True)
    soft_gate = (0.5 + alt_score / 4).clip(0, 1)
    combo_signals = {
        "old_sr_trend_combo": base_combo,
        "old_combo_x_alt_binary_gate": base_combo * alt_gate,
        "old_combo_x_alt_soft_gate": base_combo * soft_gate,
        "old_combo_alt_floor40": (0.4 * base_combo + 0.6 * base_combo * alt_gate),
        "alt_only_score": position_from_score(alt_score, 0.0, True),
    }
    rows = []
    for name, signal in combo_signals.items():
        result = backtest(close_159992, signal)
        result["factor"] = name
        rows.append(result)
    combo_table = pd.DataFrame(rows).sort_values("cal", ascending=False)
    print("══ Existing combo + alt-data gate on 159992 ══")
    print(combo_table[["factor", "ann", "shp", "cal", "dd", "bh_ann", "bh_cal", "h1_cal", "h2_cal", "exp", "flips_yr", "month_win", "cum_excess"]].to_string(index=False, float_format=lambda value: f"{value:+.3f}"))
    print()

    sweep = sweep_alt_combos(close_159992, etf_features, base_combo)
    sweep.to_csv(DATA / "alt_combo_sweep.csv", index=False)
    robust = {
        "n": len(sweep),
        "beat_old_combo_rate": float((sweep["cal"] > combo_table.loc[combo_table["factor"] == "old_sr_trend_combo", "cal"].iloc[0]).mean()),
        "beat_buyhold_rate": float((sweep["cal"] > sweep["bh_cal"]).mean()),
        "median_cal": float(sweep["cal"].median()),
        "p75_cal": float(sweep["cal"].quantile(0.75)),
        "best_cal": float(sweep["cal"].max()),
    }
    print("══ Alt gate weight perturbation on top of old combo ══")
    print(json.dumps(robust, ensure_ascii=False, indent=2))
    print(sweep.sort_values("cal", ascending=False).head(12)[[
        "wgrowth_rel", "wgrowth_mom", "wtemp_cool", "wvix_panic", "wus10y_down", "thr",
        "ann", "shp", "cal", "dd", "h1_cal", "h2_cal", "exp", "flips_yr", "month_win", "cum_excess",
    ]].to_string(index=False, float_format=lambda value: f"{value:+.3f}"))
    print()

    proxy_files = {"159992": "159992.SZ.csv", "516080": "516080.SH.csv", "159839": "159839.SZ.csv", "512010": "512010.SH.csv"}
    proxy_rows = []
    for proxy_name, file_name in proxy_files.items():
        close = read_close(file_name).loc[ETF_START:]
        features = load_alt_features(close.index).join(load_retail_sector_features(close.index))
        base = position_from_score(features["retail_imb_z15_60"] - 0.5 * features["ir_mom_z60"] - 0.5 * features["conc_z60"], 1.0, positive_is_long=False)
        score = 0.40 * rolling_z(features["growth_rel60"], 120) + 0.30 * features["growth_mom_combo"] - 0.20 * features["temp_score_z60"] - 0.10 * rolling_z(features["us10y_chg20"], 120)
        gate = position_from_score(score, 0.0, True)
        for factor_name, signal in [("old_combo", base), ("old_x_alt_gate", base * gate), ("alt_only", gate)]:
            result = backtest(close, signal)
            proxy_rows.append({
                "proxy": proxy_name,
                "factor": factor_name,
                "ann": result["ann"],
                "cal": result["cal"],
                "bh_cal": result["bh_cal"],
                "dcal": result["cal"] - result["bh_cal"],
                "h1_cal": result["h1_cal"],
                "h2_cal": result["h2_cal"],
                "exp": result["exp"],
                "flips_yr": result["flips_yr"],
            })
    proxy_table = pd.DataFrame(proxy_rows)
    print("══ Cross-proxy alt gate check ══")
    print(proxy_table.to_string(index=False, float_format=lambda value: f"{value:+.3f}"))
    print("beat buyhold rate: " + ", ".join(
        f"{name}={(group['cal'] > group['bh_cal']).mean():.0%}" for name, group in proxy_table.groupby("factor")
    ))
    print()

    sector_table.to_csv(DATA / "alt_sector_candidates.csv", index=False)
    etf_table.to_csv(DATA / "alt_etf_candidates.csv", index=False)
    combo_table.to_csv(DATA / "alt_combo_candidates.csv", index=False)
    proxy_table.to_csv(DATA / "alt_proxy_check.csv", index=False)

    holdings = pd.read_csv(DATA / "alt_fund_top_holdings_current.csv")
    summary = holdings.groupby("fund_code").agg(report_date=("report_date", "first"), top10_weight=("weight", "sum"), top1_weight=("weight", "max"), count=("stock_code", "count"))
    print("══ ETF current top-holding purity snapshot ══")
    print(summary.to_string(float_format=lambda value: f"{value:.2f}"))


if __name__ == "__main__":
    main()