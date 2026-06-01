"""Validate market-level retail contrarian anchors on innovative-drug proxies."""
from __future__ import annotations

import pandas as pd

from alt_data_mine import (
    ETF_START,
    backtest,
    load_alt_features,
    load_retail_sector_features,
    position_from_score,
    read_close,
)


PROXY_FILES = {
    "159992": "159992.SZ.csv",
    "516080": "516080.SH.csv",
    "159839": "159839.SZ.csv",
    "512010": "512010.SH.csv",
}


def proxy_signals(features: pd.DataFrame) -> dict[str, pd.Series]:
    market15 = features["qf_market_retail_contrarian_15_90"].ffill().fillna(0.5)
    market20 = features["qf_market_retail_contrarian_20_90"].ffill().fillna(0.5)
    local_combo = position_from_score(
        features["retail_imb_z15_60"] - 0.5 * features["ir_mom_z60"] - 0.5 * features["conc_z60"],
        1.0,
        positive_is_long=False,
    )
    return {
        "market_retail_contrarian_15_90": market15,
        "market_retail_contrarian_20_90": market20,
        "market_retail_min": pd.concat([market15, market20], axis=1).min(axis=1),
        "market_retail_avg": pd.concat([market15, market20], axis=1).mean(axis=1),
        "market_retail_max": pd.concat([market15, market20], axis=1).max(axis=1),
        "local_sr_trend_combo": local_combo,
        "avg_market20_local": (market20 + local_combo) / 2,
        "min_market20_local": pd.concat([market20, local_combo], axis=1).min(axis=1),
    }


def main() -> None:
    rows = []
    for proxy_name, file_name in PROXY_FILES.items():
        close = read_close(file_name).loc[ETF_START:]
        features = load_alt_features(close.index).join(load_retail_sector_features(close.index))
        for factor_name, signal in proxy_signals(features).items():
            result = backtest(close, signal)
            rows.append({
                "proxy": proxy_name,
                "factor": factor_name,
                "ann": result["ann"],
                "shp": result["shp"],
                "cal": result["cal"],
                "dd": result["dd"],
                "bh_cal": result["bh_cal"],
                "dcal": result["cal"] - result["bh_cal"],
                "h1_cal": result["h1_cal"],
                "h2_cal": result["h2_cal"],
                "exp": result["exp"],
                "flips_yr": result["flips_yr"],
                "month_win": result["month_win"],
                "cum_excess": result["cum_excess"],
            })
    frame = pd.DataFrame(rows)
    frame.to_csv("data/market_anchor_proxy_check.csv", index=False)
    print("══ Market-level retail anchors on innovative-drug proxies ══")
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
    summary.to_csv("data/market_anchor_summary.csv")
    print("══ Summary ══")
    print(summary.to_string(float_format=lambda value: f"{value:+.3f}"))


if __name__ == "__main__":
    main()