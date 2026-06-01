"""Cross-sectional scan for industry-index subscription/redemption signals.

The script builds an index universe from the passive-index fund pool, fetches
index-level subscription/redemption data when the upstream endpoint is healthy,
and runs a uniform T+1/5bp parameter sweep against representative fund NAVs.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests


ROOT = Path("/home/rooot/agent_invest_lab")
BASE = ROOT / "research" / "industry_sr_scan"
DATA = BASE / "data"
POOL_XLSX = ROOT / "data" / "被动指数型基金池（权益黄金）.xlsx"
FUND_DB = ROOT / "data" / "fund.db"
BASE_URL = "https://research.tiantianfunds.com.cn/strategy"

START = date(2024, 8, 1)
END = date(2026, 5, 28)
COMMISSION = 0.0005
WARMUP = 110
NAV_MIN_BARS = 300

SOURCES = ["net", "net_ex_aip", "imbalance"]
CUM_NS = [5, 10, 15, 20]
Z_WINS = [45, 60, 90]
THRESHOLDS = [0.5, 1.0, 1.5]
DIRECTIONS = ["contrarian", "trend"]

HEADER = ["date", "index_code", "persona", "applied", "applied_ex_aip", "redeemed", "net", "net_ex_aip"]


def clean_index_code(value: Any) -> str:
    code = str(value).strip()
    for suffix in [".CSI", ".SH", ".SZ", ".GI", ".HI"]:
        if code.endswith(suffix):
            return code[: -len(suffix)]
    return code


def build_universe() -> pd.DataFrame:
    DATA.mkdir(parents=True, exist_ok=True)
    pool = pd.read_excel(POOL_XLSX, dtype={"基金代码": str, "指数代码": str})
    pool["基金代码"] = pool["基金代码"].astype(str).str.zfill(6)
    with sqlite3.connect(FUND_DB) as connection:
        nav = pd.read_sql_query(
            """
            select fund_code, count(*) nav_n, min(nav_date) nav_start, max(nav_date) nav_end
            from fund_nav
            group by fund_code
            """,
            connection,
        )
    nav["fund_code"] = nav["fund_code"].astype(str).str.zfill(6)
    frame = pool.merge(nav, left_on="基金代码", right_on="fund_code", how="left")
    frame = frame[
        frame["基金分类"].astype(str).str.contains("指数型-股票", na=False)
        & (frame["主题(近一年)"].fillna("") != "全市场")
        & frame["指数代码"].notna()
        & (frame["nav_n"].fillna(0) >= NAV_MIN_BARS)
    ].copy()
    frame["index_code"] = frame["指数代码"].map(clean_index_code)
    frame = frame.sort_values(["index_code", "最新规模"], ascending=[True, False]).groupby("index_code").head(1)
    out = frame.rename(
        columns={
            "基金代码": "fund_code",
            "基金简称": "fund_name",
            "最新规模": "scale",
            "指数名称": "index_name",
            "主题(近一年)": "theme",
            "入池情况": "pool_status",
        }
    )[
        [
            "index_code",
            "index_name",
            "theme",
            "fund_code",
            "fund_name",
            "scale",
            "nav_n",
            "nav_start",
            "nav_end",
            "pool_status",
        ]
    ].sort_values("scale", ascending=False)
    out.to_csv(DATA / "industry_universe.csv", index=False)
    return out


def request_sr(session: requests.Session, codes: list[str], calc_date: str) -> tuple[list[dict[str, Any]], str | None]:
    payload = {"index_code": ",".join(codes), "calc_date": calc_date, "cutoff_time": "15:00:00"}
    try:
        response = session.post(
            f"{BASE_URL}/api/fund/index-subscription-redemption",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=45,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("success"):
            return [], str(body.get("message") or "success=false")
        return body.get("items") or [], None
    except Exception as exc:  # noqa: BLE001 - keep endpoint diagnostics in CSV logs.
        return [], str(exc)


def fetch_history(
    batch_size: int = 1,
    sleep_seconds: float = 2.0,
    force: bool = False,
    confirm_live_fetch: bool = False,
    max_requests: int = 0,
) -> None:
    if not confirm_live_fetch:
        raise RuntimeError("live fetch is disabled by default; pass --confirm-live-fetch after ops approval")
    if max_requests <= 0:
        raise RuntimeError("set --max-requests to an explicit positive cap for every live fetch run")
    DATA.mkdir(parents=True, exist_ok=True)
    universe_path = DATA / "industry_universe.csv"
    universe = pd.read_csv(universe_path) if universe_path.exists() else build_universe()
    codes = universe["index_code"].dropna().astype(str).tolist()
    out_path = DATA / "industry_sr_daily.csv"
    err_path = DATA / "industry_sr_errors.csv"
    existing_keys: set[tuple[str, str]] = set()
    if out_path.exists() and not force:
        with out_path.open() as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                existing_keys.add((row["date"], row["index_code"]))
    if force and out_path.exists():
        out_path.unlink()
    if force and err_path.exists():
        err_path.unlink()
    new_file = not out_path.exists()
    err_new_file = not err_path.exists()
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"Content-Type": "application/json"})
    with out_path.open("a", newline="") as out_handle, err_path.open("a", newline="") as err_handle:
        writer = csv.writer(out_handle)
        err_writer = csv.writer(err_handle)
        if new_file:
            writer.writerow(HEADER)
        if err_new_file:
            err_writer.writerow(["date", "codes", "error"])
        current = START
        total_days = (END - START).days + 1
        request_count = 0
        for day_index in range(total_days):
            ds = current.isoformat()
            remaining = [code for code in codes if (ds, code) not in existing_keys]
            for offset in range(0, len(remaining), batch_size):
                if request_count >= max_requests:
                    print(f"stopped at explicit max_requests={max_requests}", flush=True)
                    return
                batch = remaining[offset : offset + batch_size]
                items, error = request_sr(session, batch, ds)
                request_count += 1
                if error:
                    err_writer.writerow([ds, ",".join(batch), error])
                else:
                    for item in items:
                        writer.writerow(
                            [
                                ds,
                                item.get("指数代码"),
                                item.get("客户类型"),
                                item.get("申请"),
                                item.get("申请_除定投"),
                                item.get("赎回"),
                                item.get("净申赎"),
                                item.get("净申赎_除定投"),
                            ]
                        )
                out_handle.flush()
                err_handle.flush()
                time.sleep(sleep_seconds)
            current += timedelta(days=1)
            if (day_index + 1) % 20 == 0:
                print(f"fetch progress {day_index + 1}/{total_days}", flush=True)


def load_nav(fund_code: str) -> pd.Series:
    with sqlite3.connect(FUND_DB) as connection:
        nav = pd.read_sql_query(
            "select nav_date, nav from fund_nav where fund_code = ? order by nav_date",
            connection,
            params=[fund_code],
            parse_dates=["nav_date"],
        )
    series = nav.set_index("nav_date")["nav"].astype(float)
    series.index = pd.to_datetime(series.index).normalize()
    return series.loc[str(START) : str(END)]


def rolling_z(series: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    min_obs = min_periods or max(20, window // 3)
    mean = series.rolling(window, min_periods=min_obs).mean()
    std = series.rolling(window, min_periods=min_obs).std()
    return (series - mean) / std.replace(0, np.nan)


def cum_z(series: pd.Series, cum_n: int, z_win: int) -> pd.Series:
    cumulative = series.rolling(cum_n, min_periods=max(3, cum_n // 2)).sum()
    return rolling_z(cumulative, z_win)


def make_signal(close: pd.Series, raw: pd.Series, cum_n: int, z_win: int, threshold: float, direction: str) -> pd.Series:
    score = cum_z(raw, cum_n, z_win).reindex(close.index, method="ffill")
    state = pd.Series(np.nan, index=close.index)
    if direction == "contrarian":
        state = state.where(~(score > threshold), 0.0)
        state = state.where(~(score < -threshold), 1.0)
    else:
        state = state.where(~(score > threshold), 1.0)
        state = state.where(~(score < -threshold), 0.0)
    return state.ffill().fillna(0.5)


def metrics(returns: pd.Series) -> dict[str, float]:
    returns = returns.dropna()
    bars = len(returns)
    if bars == 0:
        return {"ret": 0.0, "ann": 0.0, "shp": 0.0, "dd": 0.0, "cal": 0.0}
    equity = (1 + returns).cumprod()
    total = float(equity.iloc[-1] - 1)
    annualized = float((1 + total) ** (252 / bars) - 1)
    std = float(returns.std(ddof=1)) if bars > 1 else 0.0
    sharpe = float(returns.mean() / std * np.sqrt(252)) if std > 0 else 0.0
    drawdown = float((equity / equity.cummax() - 1).min())
    calmar = float(annualized / abs(drawdown)) if drawdown < 0 else 0.0
    return {"ret": total, "ann": annualized, "shp": sharpe, "dd": drawdown, "cal": calmar}


def backtest(close: pd.Series, signal: pd.Series) -> dict[str, float]:
    signal = signal.clip(0.0, 1.0).reindex(close.index).ffill().fillna(0.0)
    returns = close.pct_change().fillna(0.0)
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
    return {
        **strategy_metrics,
        "bh_ret": benchmark_metrics["ret"],
        "bh_ann": benchmark_metrics["ann"],
        "bh_shp": benchmark_metrics["shp"],
        "bh_dd": benchmark_metrics["dd"],
        "bh_cal": benchmark_metrics["cal"],
        "dcal": strategy_metrics["cal"] - benchmark_metrics["cal"],
        "dshp": strategy_metrics["shp"] - benchmark_metrics["shp"],
        "h1_cal": metrics(strategy_returns.iloc[:split])["cal"],
        "h2_cal": metrics(strategy_returns.iloc[split:])["cal"],
        "exp": float(position_eval.mean()) if len(position_eval) else 0.0,
        "flips_yr": float((turnover_eval > 0.001).sum() / (len(strategy_returns) / 252)) if len(strategy_returns) else 0.0,
        "bars": float(len(strategy_returns)),
    }


def prepare_sr(index_code: str, close_index: pd.DatetimeIndex, sr_all: pd.DataFrame) -> pd.DataFrame:
    block = sr_all[(sr_all["index_code"].astype(str) == index_code) & (sr_all["persona"] == "个人")].copy()
    if block.empty:
        return pd.DataFrame(index=close_index)
    block = block.sort_values("date").drop_duplicates("date", keep="last").set_index("date")
    block.index = pd.to_datetime(block.index).normalize()
    for column in ["applied", "applied_ex_aip", "redeemed", "net", "net_ex_aip"]:
        block[column] = pd.to_numeric(block[column], errors="coerce")
    block["imbalance"] = (block["applied"] - block["redeemed"]) / (block["applied"] + block["redeemed"]).replace(0, np.nan)
    aligned = block.reindex(close_index, method="ffill")
    return aligned[["net", "net_ex_aip", "imbalance"]]


def classify(summary: pd.Series) -> str:
    if summary["contra_beat_rate"] >= 0.4 and summary["contra_median_dcal"] > 0.3 and summary["contra_median_h2_cal"] > 0:
        return "green_candidate"
    if summary["contra_beat_rate"] >= 0.25 and summary["contra_median_dcal"] > 0:
        return "yellow_hint"
    return "red"


def run_backtests() -> None:
    universe_path = DATA / "industry_universe.csv"
    sr_path = DATA / "industry_sr_daily.csv"
    if not universe_path.exists():
        build_universe()
    if not sr_path.exists():
        raise FileNotFoundError(f"missing {sr_path}; run --fetch after the endpoint recovers")
    universe = pd.read_csv(universe_path, dtype={"index_code": str, "fund_code": str})
    sr_all = pd.read_csv(sr_path, parse_dates=["date"], dtype={"index_code": str})
    rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for record in universe.to_dict("records"):
        index_code = str(record["index_code"])
        close = load_nav(str(record["fund_code"]).zfill(6))
        if len(close) < WARMUP + 60:
            continue
        sr = prepare_sr(index_code, close.index, sr_all)
        if sr.empty or sr["net"].notna().sum() < 120:
            continue
        for source in SOURCES:
            raw = sr[source]
            if raw.notna().sum() < 120:
                continue
            for cum_n in CUM_NS:
                for z_win in Z_WINS:
                    for threshold in THRESHOLDS:
                        for direction in DIRECTIONS:
                            signal = make_signal(close, raw, cum_n, z_win, threshold, direction)
                            result = backtest(close, signal)
                            rows.append({
                                **record,
                                "source": source,
                                "cum_n": cum_n,
                                "z_win": z_win,
                                "threshold": threshold,
                                "direction": direction,
                                **result,
                            })
    full = pd.DataFrame(rows)
    full.to_csv(DATA / "industry_sr_sweep_full.csv", index=False)
    if full.empty:
        pd.DataFrame().to_csv(DATA / "industry_sr_summary.csv", index=False)
        return
    for (index_code, fund_code), block in full.groupby(["index_code", "fund_code"]):
        contra = block[block["direction"] == "contrarian"]
        trend = block[block["direction"] == "trend"]
        best = block.sort_values("dcal", ascending=False).iloc[0]
        summary = {
            "index_code": index_code,
            "index_name": best["index_name"],
            "theme": best["theme"],
            "fund_code": fund_code,
            "fund_name": best["fund_name"],
            "scale": best["scale"],
            "bars": best["bars"],
            "bh_ret": best["bh_ret"],
            "bh_cal": best["bh_cal"],
            "bh_dd": best["bh_dd"],
            "contra_beat_rate": float((contra["dcal"] > 0).mean()),
            "contra_median_dcal": float(contra["dcal"].median()),
            "contra_mean_dcal": float(contra["dcal"].mean()),
            "contra_median_h1_cal": float(contra["h1_cal"].median()),
            "contra_median_h2_cal": float(contra["h2_cal"].median()),
            "contra_median_exp": float(contra["exp"].median()),
            "contra_median_flips_yr": float(contra["flips_yr"].median()),
            "trend_beat_rate": float((trend["dcal"] > 0).mean()),
            "trend_median_dcal": float(trend["dcal"].median()),
            "best_direction": best["direction"],
            "best_source": best["source"],
            "best_cum_n": best["cum_n"],
            "best_z_win": best["z_win"],
            "best_threshold": best["threshold"],
            "best_dcal": best["dcal"],
            "best_cal": best["cal"],
            "best_exp": best["exp"],
            "best_flips_yr": best["flips_yr"],
        }
        summary["rating"] = classify(pd.Series(summary))
        summaries.append(summary)
    result = pd.DataFrame(summaries).sort_values(["rating", "contra_median_dcal"], ascending=[True, False])
    result.to_csv(DATA / "industry_sr_summary.csv", index=False)
    print(result.to_string(index=False, float_format=lambda value: f"{value:+.3f}"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-universe", action="store_true")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--confirm-live-fetch", action="store_true")
    parser.add_argument("--max-requests", type=int, default=0)
    args = parser.parse_args()
    if args.build_universe or not any([args.fetch, args.backtest]):
        universe = build_universe()
        print(f"wrote {DATA / 'industry_universe.csv'} rows={len(universe)}")
    if args.fetch:
        fetch_history(args.batch_size, args.sleep, args.force, args.confirm_live_fetch, args.max_requests)
    if args.backtest:
        run_backtests()


if __name__ == "__main__":
    main()