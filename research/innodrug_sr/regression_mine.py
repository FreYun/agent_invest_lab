"""Regression-style mining for innovative-drug timing factors.

Uses only cached simworld/PIT exports in this directory. The goal is diagnostic:
confirm which components carry incremental information, then compare a few
low-parameter timing rules. Short retail-flow history means all retail-linked
outputs remain exploratory.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


BASE = Path("/home/rooot/agent_invest_lab/research/innodrug_sr")
DATA = BASE / "data"
COMMISSION = 0.0005
FORWARD_DAYS = 20
WARMUP = 90
BOOTSTRAP_SEED = 42


def rolling_z(series: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    min_obs = min_periods or max(20, window // 3)
    mean = series.rolling(window, min_periods=min_obs).mean()
    std = series.rolling(window, min_periods=min_obs).std()
    return (series - mean) / std


def rolling_rank(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=max(20, window // 3)).rank(pct=True)


def cumulative_z(series: pd.Series, lookback: int, z_window: int) -> pd.Series:
    cumulative = series.rolling(lookback, min_periods=max(3, lookback // 2)).sum()
    return rolling_z(cumulative, z_window)


def load_close(file_name: str = "159992.SZ.csv") -> pd.Series:
    price = pd.read_csv(DATA / file_name, parse_dates=[0])
    price.columns = [column.lower() for column in price.columns]
    price = price.set_index(price.columns[0]).sort_index()
    price.index = pd.to_datetime(price.index).normalize()
    return price["close"].astype(float)


def load_panel(file_name: str = "159992.SZ.csv") -> pd.DataFrame:
    close = load_close(file_name)
    index = close.loc["2024-08-01":].index

    sr = pd.read_csv(BASE / "innodrug_sr_daily.csv", parse_dates=["date"])
    sr = sr[sr["persona"] == "个人"].set_index("date").sort_index()
    sr.index = pd.to_datetime(sr.index).normalize()
    sr["imbalance"] = (sr["applied"] - sr["redeemed"]) / (sr["applied"] + sr["redeemed"]).replace(0, np.nan)

    sector_market = pd.read_csv(DATA / "sector_BK000208_market.csv", parse_dates=["date"]).set_index("date")
    sector_factor = pd.read_csv(DATA / "sector_BK000208_factor.csv", parse_dates=["date"]).set_index("date")
    sector_detail = pd.read_csv(DATA / "sector_BK000208_factor_detail.csv", parse_dates=["date"]).set_index("date")
    sector = sector_market.join(sector_factor).join(sector_detail).sort_index()
    sector["inflow20_z90"] = cumulative_z(sector["main_inflow"], 20, 90)
    sector["inflow120_rank252"] = rolling_rank(sector["main_inflow"].rolling(120, min_periods=60).sum(), 252)
    sector["ir_mom_z60"] = rolling_z(sector["信息比率动量"], 60)
    sector["conc_z60"] = rolling_z(sector["集中度分位"], 60)
    sector["dev_z60"] = rolling_z(sector["乖离分位"], 60)
    sector["pe_z252"] = rolling_z(sector["pe"], 252)
    sector["pb_z252"] = rolling_z(sector["pb"], 252)

    panel = pd.DataFrame(index=index)
    panel["close"] = close.reindex(index)
    sr_aligned = sr.reindex(index, method="ffill")
    panel["retail_net_z10_60"] = cumulative_z(sr_aligned["net"], 10, 60)
    panel["retail_net_z15_90"] = cumulative_z(sr_aligned["net"], 15, 90)
    panel["retail_imb_z15_60"] = cumulative_z(sr_aligned["imbalance"], 15, 60)
    panel["retail_imb_z15_90"] = cumulative_z(sr_aligned["imbalance"], 15, 90)
    for column in ["inflow20_z90", "inflow120_rank252", "ir_mom_z60", "conc_z60", "dev_z60", "pe_z252", "pb_z252"]:
        panel[column] = sector[column].reindex(index, method="ffill")
    panel["fwd20"] = panel["close"].shift(-FORWARD_DAYS) / panel["close"] - 1
    return panel


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


def position_from_bear_score(score: pd.Series, threshold: float) -> pd.Series:
    state = pd.Series(np.nan, index=score.index)
    state = state.where(~(score > threshold), 0.0)
    state = state.where(~(score < -threshold), 1.0)
    return state.ffill().fillna(0.5)


def position_from_return_score(score: pd.Series, threshold: float) -> pd.Series:
    state = pd.Series(np.nan, index=score.index)
    state = state.where(~(score > threshold), 1.0)
    state = state.where(~(score < -threshold), 0.0)
    return state.ffill().fillna(0.5)


def backtest(panel: pd.DataFrame, signal: pd.Series) -> dict[str, float]:
    signal = signal.clip(0.0, 1.0).reindex(panel.index).ffill().fillna(0.5)
    returns = panel["close"].pct_change().fillna(0.0)
    position = signal.shift(1).fillna(0.0)
    turnover = position.diff().abs().fillna(0.0)
    strategy_returns = position * returns - turnover * COMMISSION

    strategy_returns = strategy_returns.iloc[WARMUP:]
    benchmark_returns = returns.iloc[WARMUP:]
    position_eval = position.iloc[WARMUP:]
    turnover_eval = turnover.iloc[WARMUP:]
    split = len(strategy_returns) // 2

    strategy_metrics = metrics(strategy_returns)
    benchmark_metrics = metrics(benchmark_returns)
    first_half = metrics(strategy_returns.iloc[:split])
    second_half = metrics(strategy_returns.iloc[split:])
    monthly_excess = (strategy_returns - benchmark_returns).resample("ME").apply(lambda month: (1 + month).prod() - 1)

    return {
        **strategy_metrics,
        "bh_ann": benchmark_metrics["ann"],
        "bh_cal": benchmark_metrics["cal"],
        "h1_cal": first_half["cal"],
        "h2_cal": second_half["cal"],
        "exp": float(position_eval.mean()),
        "flips_yr": float((turnover_eval > 0.001).sum() / (len(strategy_returns) / 252)),
        "month_win": float((monthly_excess > 0).mean()),
        "cum_excess": float((1 + monthly_excess).prod() - 1),
    }


def ridge_coefficients(frame: pd.DataFrame, feature_columns: list[str], alpha: float = 5.0) -> tuple[pd.Series, pd.DataFrame]:
    regression_frame = frame[feature_columns + ["fwd20"]].dropna()
    features = regression_frame[feature_columns]
    target = regression_frame["fwd20"]
    standardized = (features - features.mean()) / features.std(ddof=0)
    centered_target = target - target.mean()
    matrix = standardized.to_numpy()
    coefficients = np.linalg.solve(matrix.T @ matrix + alpha * np.eye(matrix.shape[1]), matrix.T @ centered_target.to_numpy())
    coefficient_series = pd.Series(coefficients, index=feature_columns)

    rng = np.random.default_rng(BOOTSTRAP_SEED)
    block_size = FORWARD_DAYS
    bootstrap_rows = []
    values = features.to_numpy()
    target_values = target.to_numpy()
    sample_size = len(regression_frame)
    for _ in range(1000):
        sample_indices: list[int] = []
        while len(sample_indices) < sample_size:
            start = int(rng.integers(0, max(1, sample_size - block_size + 1)))
            sample_indices.extend(range(start, start + block_size))
        sample_indices_array = np.array(sample_indices[:sample_size])
        sample_features = values[sample_indices_array]
        sample_target = target_values[sample_indices_array]
        mean = sample_features.mean(axis=0)
        std = sample_features.std(axis=0)
        std[std == 0] = 1.0
        sample_standardized = (sample_features - mean) / std
        sample_centered_target = sample_target - sample_target.mean()
        sample_coefficients = np.linalg.solve(
            sample_standardized.T @ sample_standardized + alpha * np.eye(sample_standardized.shape[1]),
            sample_standardized.T @ sample_centered_target,
        )
        bootstrap_rows.append(sample_coefficients)
    bootstrap = pd.DataFrame(bootstrap_rows, columns=feature_columns)
    summary = pd.DataFrame({
        "coef": coefficient_series,
        "boot_median": bootstrap.median(),
        "p_pos": (bootstrap > 0).mean(),
        "q05": bootstrap.quantile(0.05),
        "q95": bootstrap.quantile(0.95),
    })
    return coefficient_series, summary


def walk_forward_ridge_score(frame: pd.DataFrame, feature_columns: list[str], alpha: float = 5.0, min_train: int = 160) -> pd.Series:
    regression_frame = frame[feature_columns + ["fwd20"]].dropna()
    feature_values = regression_frame[feature_columns].to_numpy()
    target_values = regression_frame["fwd20"].to_numpy()
    dates = regression_frame.index
    predictions: list[float] = []
    prediction_dates: list[pd.Timestamp] = []
    for position in range(len(regression_frame)):
        train_end = position - FORWARD_DAYS
        if train_end < min_train:
            continue
        train_features = feature_values[:train_end]
        train_target = target_values[:train_end]
        mean = train_features.mean(axis=0)
        std = train_features.std(axis=0)
        std[std == 0] = 1.0
        train_standardized = (train_features - mean) / std
        train_centered_target = train_target - train_target.mean()
        coefficients = np.linalg.solve(
            train_standardized.T @ train_standardized + alpha * np.eye(train_standardized.shape[1]),
            train_standardized.T @ train_centered_target,
        )
        prediction = ((feature_values[position] - mean) / std) @ coefficients + train_target.mean()
        predictions.append(float(prediction))
        prediction_dates.append(dates[position])
    return pd.Series(predictions, index=prediction_dates)


def print_ic_table(panel: pd.DataFrame, feature_columns: list[str]) -> None:
    rows = []
    for column in feature_columns:
        sample = panel[[column, "fwd20"]].dropna()
        rows.append({
            "feature": column,
            "spearman_fwd20": sample[column].corr(sample["fwd20"], method="spearman"),
            "n": len(sample),
        })
    table = pd.DataFrame(rows).sort_values("spearman_fwd20")
    print(table.to_string(index=False, float_format=lambda value: f"{value:+.3f}"))


def main() -> None:
    panel = load_panel()
    print(f"window {panel.index.min().date()}..{panel.index.max().date()} rows={len(panel)}")
    print(f"eval starts after warmup={WARMUP}; fwd horizon={FORWARD_DAYS}d; cost={COMMISSION:.4f} one-way")
    print()

    feature_columns = [
        "retail_net_z10_60", "retail_net_z15_90", "retail_imb_z15_60", "retail_imb_z15_90",
        "ir_mom_z60", "conc_z60", "inflow20_z90", "inflow120_rank252", "dev_z60", "pe_z252", "pb_z252",
    ]
    print("══ Feature IC vs fwd20 (Spearman; negative retail = contrarian) ══")
    print_ic_table(panel, feature_columns)
    print()

    core_features = ["retail_imb_z15_60", "retail_net_z10_60", "ir_mom_z60", "conc_z60", "inflow20_z90", "pe_z252", "pb_z252"]
    coefficients, coefficient_summary = ridge_coefficients(panel, core_features)
    print("══ Full-window ridge coefficients + 20d block bootstrap signs ══")
    print(coefficient_summary.to_string(float_format=lambda value: f"{value:+.4f}"))
    print("note: full-window coefficients are diagnostic only; retail history is too short for production ML.")
    print()

    standardized_core = (panel[core_features] - panel[core_features].mean()) / panel[core_features].std(ddof=0)
    ridge_score = (standardized_core @ coefficients).rename("ridge_full_window_score")
    ridge_score = ridge_score / ridge_score.std(ddof=0)

    candidate_signals = {
        "retail_net_contra_10_60_thr1.5": position_from_bear_score(panel["retail_net_z10_60"], 1.5),
        "retail_imb_contra_15_90_thr1.0": position_from_bear_score(panel["retail_imb_z15_90"], 1.0),
        "reg_combo_retail_mom_conc_EW": position_from_bear_score(
            panel["retail_imb_z15_60"] - 0.5 * panel["ir_mom_z60"] - 0.5 * panel["conc_z60"], 1.0
        ),
        "ridge_full_window_research_only": position_from_return_score(ridge_score, 1.0),
    }
    print("══ Candidate timing backtests on 159992.SZ ══")
    rows = []
    for name, signal in candidate_signals.items():
        result = backtest(panel, signal)
        result["factor"] = name
        rows.append(result)
    result_table = pd.DataFrame(rows)[[
        "factor", "ann", "shp", "cal", "dd", "bh_ann", "bh_cal", "h1_cal", "h2_cal", "exp", "flips_yr", "month_win", "cum_excess",
    ]]
    print(result_table.to_string(index=False, float_format=lambda value: f"{value:+.3f}"))
    print()

    print("══ Cross-proxy check: combo vs retail-only on ETF proxies ══")
    proxy_files = {
        "159992": "159992.SZ.csv",
        "516080": "516080.SH.csv",
        "159839": "159839.SZ.csv",
        "512010": "512010.SH.csv",
    }
    proxy_rows = []
    for proxy_name, proxy_file in proxy_files.items():
        proxy_panel = load_panel(proxy_file)
        retail_signal = position_from_bear_score(proxy_panel["retail_imb_z15_60"], 1.0)
        combo_signal = position_from_bear_score(
            proxy_panel["retail_imb_z15_60"] - 0.5 * proxy_panel["ir_mom_z60"] - 0.5 * proxy_panel["conc_z60"], 1.0
        )
        for factor_name, proxy_signal in [("retail_imb", retail_signal), ("combo_EW", combo_signal)]:
            result = backtest(proxy_panel, proxy_signal)
            proxy_rows.append({
                "proxy": proxy_name,
                "factor": factor_name,
                "ann": result["ann"],
                "cal": result["cal"],
                "bh_cal": result["bh_cal"],
                "dcal": result["cal"] - result["bh_cal"],
                "h2_cal": result["h2_cal"],
                "flips_yr": result["flips_yr"],
            })
    proxy_table = pd.DataFrame(proxy_rows)
    print(proxy_table.to_string(index=False, float_format=lambda value: f"{value:+.3f}"))
    beat_rate = proxy_table.assign(beat=proxy_table["cal"] > proxy_table["bh_cal"]).groupby("factor")["beat"].mean()
    print("beat buyhold rate: " + ", ".join(f"{name}={rate:.0%}" for name, rate in beat_rate.items()))
    print()

    walk_features = ["retail_imb_z15_60", "retail_net_z10_60", "ir_mom_z60", "conc_z60"]
    walk_score = walk_forward_ridge_score(panel, walk_features)
    walk_rank = walk_score.rolling(126, min_periods=60).rank(pct=True)
    walk_signal = position_from_return_score(walk_rank - 0.5, 0.2)
    walk_result = backtest(panel.loc[walk_score.index.min():], walk_signal.loc[walk_score.index.min():])
    walk_sample = panel.loc[walk_score.index, "fwd20"].dropna()
    walk_ic = walk_score.loc[walk_sample.index].corr(walk_sample, method="spearman")
    print("══ Purged expanding-ridge sanity check (train labels end before prediction day) ══")
    print(
        f"start={walk_score.index.min().date()} n={len(walk_score)} ic={walk_ic:+.3f} "
        f"cal={walk_result['cal']:+.3f} bh_cal={walk_result['bh_cal']:+.3f} "
        f"ann={walk_result['ann']:+.3f} exp={walk_result['exp']:+.2f} flips/yr={walk_result['flips_yr']:+.1f}"
    )
    print("verdict: regression model itself is not stable enough; keep the low-parameter combo as the tradable research candidate.")


if __name__ == "__main__":
    main()