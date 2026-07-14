# S1 Breakout Exploration — 2026-05-13

Status: research only. Do not skillize yet.

## Guardrails

- Ran serially in one Python process.
- Used `OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1`.
- Did not add production code, cron jobs, `SKILL.md`, or `scripts/s1/`.

## Files

- Driver: `s1_serial_explore_20260513.py`
- Recent parameter sweep: `s1_explore_recent_20260513.csv`
- Cross-window parameter checks: `s1_explore_cross_20260513.csv`
- Ranking / hot-industry filters: `s1_explore_filters_20260513.csv`

## Baseline Rechecked

Baseline candidate:

- Regime: `STRONG_RANGE`
- Base: 20 days
- Platform width: <= 14%
- Breakout: >= 2%
- Strength: `ret20 >= 10%`, `rel20 >= 5%`
- T+1 entry band: -2% to 0% vs signal close
- Hold: 2 days

Recent window `2024-09-01` to `2025-03-31`:

| Config | Triggered | Win Rate | Avg PnL | Median PnL | P25 |
|---|---:|---:|---:|---:|---:|
| baseline | 113 | 44.2% | +0.79% | -0.44% | -2.90% |
| platform <= 10% | 41 | 48.8% | +0.54% | +0.00% | -3.21% |
| take profit 3% | 113 | 48.7% | -0.47% | -0.32% | -2.90% |
| hold 3 days | 113 | 46.0% | +1.25% | -0.73% | -3.81% |
| ret20 >= 15%, rel20 >= 8% | 41 | 34.1% | -0.37% | -2.52% | -4.85% |

Read: stronger stock filters hurt. Longer hold increases average but worsens median and left tail. Small take-profit improves hit rate/median but cuts winners enough to make average negative in this recent sample.

## Cross-Window Baseline

| Window | Triggered | Win Rate | Avg PnL | Median PnL |
|---|---:|---:|---:|---:|
| 2015 | 25 | 88.0% | +3.97% | +2.89% |
| 2019 | 92 | 56.5% | +0.44% | +0.55% |
| 2020 | 180 | 50.6% | +0.10% | +0.11% |
| 2024-09 to 2025-03 | 113 | 44.2% | +0.79% | -0.44% |
| 2025-04 to 2026-05-13 | 394 | 47.0% | +0.41% | -0.21% |

Read: baseline is not dead, but the edge is thin after 2024. It remains “mean positive, median slightly negative” in recent regimes.

## Ranking Filter

Using the baseline signals, candidate score ranking by day was tested post-hoc.

| Window | Filter | Triggered | Win Rate | Avg PnL | Median PnL |
|---|---|---:|---:|---:|---:|
| 2020 | top1 | 66 | 53.0% | +0.18% | +0.63% |
| 2020 | top2 | 114 | 52.6% | +0.26% | +0.16% |
| 2025-2026 YTD | top1 | 99 | 55.6% | +0.70% | +0.25% |
| 2025-2026 YTD | top2 | 176 | 52.8% | +1.08% | +0.25% |
| 2024-09 to 2025-03 | top1 | 27 | 44.4% | +1.60% | -0.44% |
| 2024-09 to 2025-03 | top2 | 54 | 44.4% | +1.09% | -0.65% |

Read: daily rank pruning helps in 2020 and 2025-2026, but does not solve the tough 2024-09 to 2025-03 sample. `top2` looks more usable than `top1` because top1 can be too sparse and sometimes unstable.

## Hot-Industry Filter

Post-hoc industry mapping used:

- Candidate industry: `s5_daily_universe.industry`
- Daily hot industry rank: `hot_industries_daily.rank`

The intuitive filter “only buy hot industries” did not hold up.

| Window | Filter | Triggered | Win Rate | Avg PnL | Median PnL |
|---|---|---:|---:|---:|---:|
| 2020 | hot rank <= 3 | 15 | 33.3% | -1.96% | -2.20% |
| 2024-09 to 2025-03 | hot rank <= 1 | 7 | 28.6% | -1.95% | -1.08% |
| 2025-2026 YTD | hot rank <= 1 | 18 | 16.7% | -1.98% | -1.97% |
| 2025-2026 YTD | hot rank <= 3 | 36 | 33.3% | -0.26% | -1.69% |

Avoiding the hottest industry improved recent samples:

| Window | Filter | Triggered | Win Rate | Avg PnL | Median PnL |
|---|---|---:|---:|---:|---:|
| 2020 | not hot rank 1 | 170 | 51.8% | +0.25% | +0.16% |
| 2024-09 to 2025-03 | not hot rank 1 | 106 | 45.3% | +0.97% | -0.40% |
| 2025-2026 YTD | not hot rank 1 | 376 | 48.4% | +0.52% | -0.06% |
| 2025-2026 YTD | top2 + not hot rank 3 | 176 | 53.4% | +1.13% | +0.46% |

Read: for S1, chasing the day’s hottest limit-up industry often looks like late-stage heat, not healthy leadership. The better direction is likely “strong stock breakout, but not in the most crowded hot board.”

## Current Hypothesis

S1 should not be implemented as a generic “strong bull breakout” skill yet. The more promising research shape is:

1. Gate: `STRONG_RANGE`.
2. Candidate: base 20, platform <= 14%, breakout >= 2%, `ret20 >= 10%`, `rel20 >= 5%`.
3. Entry: T+1 only, -2% to 0%; do not chase above signal close.
4. Exit: 2-day hold is still the best default; 3-day hold worsens the median/tail.
5. Ranking: prefer top2 or top3 per day, not necessarily top1 only.
6. Crowding: avoid the daily hottest industry, especially rank 1; hot-industry-only is not supported.

## Next Step

Before skillization, test a production-like rule:

`STRONG_RANGE + baseline candidate + top2/day + exclude hot_industry_rank <= 1 or <= 3`

Needed validation:

- Run a combined rule directly inside the harness, not just post-hoc filtering.
- Add transaction-cost/slippage assumptions.
- Inspect losing clusters by date and industry.
- Compare against S3/S4/S5 candidate overlap to avoid duplicate exposure.
