# S1 Base Window / Platform Width Grid — 2026-05-14

Status: research only.

## Question

Explore more platform lookback periods beyond the original 20/30-day tests.

## Setup

Latest S1 candidate:

- Regime: `STRONG_RANGE`
- Breakout >= 2%
- Volume ratio >= 1.5x
- Amount >= 5亿
- `ret20 >= 10%`
- `rel20 >= 5%`
- T+1 open-only entry
- Entry bands: `-3%~+2%` and `-3%~+1%`
- Hold: 2 days

Grid:

- Base window: `10, 15, 20, 25, 30, 40, 60` trading days
- Max platform width: `8%, 10%, 12%, 14%, 16%, 18%`
- Windows: `2019`, `2020`, `2024-09~2025-03`, `2025-04~2026-05-14`

Outputs:

- `s1_base_window_grid_20260514.py`
- `s1_base_window_grid_20260514.csv`
- `s1_base_window_grid_agg_20260514.csv`

## Top Aggregate Results

| Config | Total Triggered | Weighted Avg | Avg Median | Worst Median | Avg P25 |
|---|---:|---:|---:|---:|---:|
| base 10, width <=10%, entry -3~+2 | 430 | +0.48% | +0.44% | -0.09% | -1.67% |
| base 20, width <=14%, entry -3~+2 | 362 | +0.97% | +0.41% | -0.21% | -1.54% |
| base 10, width <=10%, entry -3~+1 | 386 | +0.37% | +0.37% | -0.28% | -1.75% |
| base 15, width <=12%, entry -3~+2 | 338 | +0.94% | +0.34% | +0.17% | -1.38% |
| base 20, width <=14%, entry -3~+1 | 315 | +0.90% | +0.33% | -0.21% | -1.79% |
| base 20, width <=18%, entry -3~+1 | 572 | +0.86% | +0.24% | -0.44% | -1.82% |
| base 20, width <=16%, entry -3~+2 | 504 | +0.91% | +0.22% | -0.63% | -1.82% |

## Read

### 1. 20-day / 14% remains a strong default

The existing `base_window=20`, `width<=14%` remains a very good candidate:

- Weighted average is high: +0.97%
- Average median is positive: +0.41%
- Average P25 is among the best: -1.54%
- Sample count is adequate: 362 triggered

This supports keeping it as the default S1 platform definition.

### 2. 15-day / 12% is the cleanest robustness challenger

`base_window=15`, `width<=12%`, entry `-3~+2` is interesting:

- Weighted avg: +0.94%
- Avg median: +0.34%
- Worst median: +0.17% (best among populated configs)
- Avg P25: -1.38% (best top-tier left-tail)

This is the most robust challenger because no window-level median went negative in the aggregate run.

Potential downside: fewer trades than 20/14 and slightly lower average median.

### 3. 10-day / 10% has high median but lower quality of average edge

`base_window=10`, `width<=10%` ranked first by average median:

- Avg median: +0.44%
- Weighted avg: only +0.48%

This looks like a short consolidation / momentum continuation variant, not the same “proper platform breakout” as 20-day S1. It may be useful, but it probably needs separate treatment and additional crowding/false-break filters.

### 4. Very long windows are not attractive

`40` and `60` day windows often had too few signals or weak recent performance.

Recent YTD was especially bad for `60` day windows:

- `base60 width<=14`: median around -1%
- `base60 width<=16/18`: median around -1.1%

Conclusion: long platform breakouts may be too stale for this T+1 2-day execution model.

### 5. Wider is not always better

For a fixed base window, widening the platform often increases trade count but dilutes quality:

- `base10 width<=18` generated many trades but poor recent left-tail.
- `base20 width<=18` generated many trades and remained usable, but median quality was weaker than `base20 width<=14`.
- `base15 width<=18` also became diluted.

## Updated Recommendation

For S1 v0 observation skill:

Primary executable candidate:

```text
base_window = 20
max_base_range_pct = 14
entry = T+1 open -3% to +2%
```

Robust alternative candidate:

```text
base_window = 15
max_base_range_pct = 12
entry = T+1 open -3% to +2%
```

Research-only variant:

```text
base_window = 10
max_base_range_pct = 10
```

Do not use as default:

```text
base_window >= 40
base_window = 60
```

## Next Validation

Before production skillization, compare:

1. `20/14` default
2. `15/12` robust challenger
3. `10/10` short-platform variant

with:

- top2/day pruning
- hot-industry exclusion
- transaction cost and slippage
- overlap with S2/S3/S4/S5 candidates
