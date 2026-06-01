"""Check market-level retail contrarian anchors on nonferrous proxies."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests


BASE = Path("/home/rooot/agent_invest_lab/research/nonferrous_sr")
DATA = BASE / "data"
MCP = "http://127.0.0.1:18078/mcp"
SIMULATED_DATETIME = "2026-05-29 15:30:00"
COMMISSION = 0.0005
WARMUP = 60

PROXY_FILES = {
    "512400": "etf_512400.csv",
    "000819": "swnf_000819.csv",
    "930708": "csnf_930708.csv",
    "399395": "gznf_399395.csv",
}


def call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    response = requests.post(
        MCP,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        data=json.dumps(payload),
        timeout=60,
    )
    response.raise_for_status()
    text = response.text
    if text.startswith("event:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    body = json.loads(text)
    if "error" in body:
        raise RuntimeError(body["error"])
    return json.loads(body["result"]["content"][0]["text"])


def fetch_quant_factor(force: bool = False) -> pd.DataFrame:
    path = DATA / "market_anchor_quant_factor.csv"
    if path.exists() and not force:
        return pd.read_csv(path, parse_dates=["date"])
    result = call("quant_factor", {"simulated_datetime": SIMULATED_DATETIME, "history_days": 1200})
    rows: list[dict[str, Any]] = []
    for factor, payload in result.get("factors", {}).items():
        for record in payload.get("仓位序列", []):
            rows.append({"date": record.get("日期"), "factor": factor, "position": record.get("仓位")})
    frame = pd.DataFrame(rows)
    frame.to_csv(path, index=False)
    return pd.read_csv(path, parse_dates=["date"])


def read_close(file_name: str) -> pd.Series:
    price = pd.read_csv(DATA / file_name, parse_dates=["date"])
    price = price.set_index("date").sort_index()
    price.index = pd.to_datetime(price.index).normalize()
    return price["close"].astype(float)


def load_market_signals(index: pd.DatetimeIndex) -> dict[str, pd.Series]:
    quant = fetch_quant_factor()
    quant["date"] = pd.to_datetime(quant["date"]).dt.normalize()
    pivot = quant.pivot_table(index="date", columns="factor", values="position", aggfunc="last").sort_index()
    pivot = pivot.reindex(index, method="ffill")
    market15 = pivot["market_retail_contrarian_15_90"].astype(float).ffill().fillna(0.5)
    market20 = pivot["market_retail_contrarian_20_90"].astype(float).ffill().fillna(0.5)
    return {
        "market_retail_contrarian_15_90": market15,
        "market_retail_contrarian_20_90": market20,
        "market_retail_min": pd.concat([market15, market20], axis=1).min(axis=1),
        "market_retail_avg": pd.concat([market15, market20], axis=1).mean(axis=1),
        "market_retail_max": pd.concat([market15, market20], axis=1).max(axis=1),
    }


def metrics(returns: pd.Series) -> dict[str, float]:
    returns = returns.dropna()
    bars = len(returns)
    if bars == 0:
        return {"ret": 0.0, "ann": 0.0, "shp": 0.0, "dd": 0.0, "cal": 0.0}
    equity = (1 + returns).cumprod()
    total_return = float(equity.iloc[-1] - 1)
    annualized = float((1 + total_return) ** (252 / bars) - 1)
    std = float(returns.std(ddof=1)) if bars > 1 else 0.0
    sharpe = float(returns.mean() / std * np.sqrt(252)) if std > 0 else 0.0
    drawdown = float((equity / equity.cummax() - 1).min())
    calmar = float(annualized / abs(drawdown)) if drawdown < 0 else 0.0
    return {"ret": total_return, "ann": annualized, "shp": sharpe, "dd": drawdown, "cal": calmar}


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
        "bh_ret": benchmark_metrics["ret"],
        "bh_ann": benchmark_metrics["ann"],
        "bh_shp": benchmark_metrics["shp"],
        "bh_dd": benchmark_metrics["dd"],
        "bh_cal": benchmark_metrics["cal"],
        "dcal": strategy_metrics["cal"] - benchmark_metrics["cal"],
        "dret": strategy_metrics["ret"] - benchmark_metrics["ret"],
        "h1_cal": metrics(strategy_returns.iloc[:split])["cal"],
        "h2_cal": metrics(strategy_returns.iloc[split:])["cal"],
        "exp": float(position_eval.mean()),
        "flips_yr": float((turnover_eval > 0.001).sum() / (len(strategy_returns) / 252)) if len(strategy_returns) else 0.0,
        "month_win": float((monthly_excess > 0).mean()),
        "cum_excess": float((1 + monthly_excess).prod() - 1),
        "last_pos": float(signal.reindex(close.index).ffill().iloc[-1]),
    }


def main() -> None:
    rows = []
    for proxy, file_name in PROXY_FILES.items():
        close = read_close(file_name)
        signals = load_market_signals(close.index)
        for factor, signal in signals.items():
            result = backtest(close, signal)
            rows.append({"proxy": proxy, "factor": factor, **result})
    frame = pd.DataFrame(rows)
    frame.to_csv(BASE / "market_anchor_proxy_check.csv", index=False)
    print("══ Market-level retail anchors on nonferrous proxies ══")
    print(frame.to_string(index=False, float_format=lambda value: f"{value:+.3f}"))
    print()
    summary = frame.assign(beat=frame["cal"] > frame["bh_cal"]).groupby("factor").agg(
        beat_rate=("beat", "mean"),
        median_dcal=("dcal", "median"),
        min_dcal=("dcal", "min"),
        mean_cal=("cal", "mean"),
        median_h1_cal=("h1_cal", "median"),
        median_h2_cal=("h2_cal", "median"),
        median_exp=("exp", "median"),
        median_flips_yr=("flips_yr", "median"),
        median_month_win=("month_win", "median"),
    ).sort_values(["beat_rate", "median_dcal"], ascending=False)
    summary.to_csv(BASE / "market_anchor_summary.csv")
    print("══ Summary ══")
    print(summary.to_string(float_format=lambda value: f"{value:+.3f}"))


if __name__ == "__main__":
    main()