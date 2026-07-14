# S1 Open-Price Entry Grid — 2026-05-13

Status: research only.

## Question

Test entry bands using T+1 open only:

- Buy at T+1 open if open is inside the configured band versus T signal close.
- Do not assume intraday pullback/up-tick fills.

This differs from the original harness, which allowed intraday touch fills inside the entry band.

## Signal Setup

Used the current better signal candidate:

- `STRONG_RANGE`
- Platform width <= 14%
- Breakout >= 2%
- Volume ratio >= 1.5x
- Amount >= 5亿
- `ret20 >= 10%`
- `rel20 >= 5%`
- Hold 2 days

## Entry Bands Tested

Low side:

- `-3%`, `-2%`, `-1%`, `0%`

High side:

- `0%`, `+1%`, `+2%`, `+3%`

All prices are measured versus T signal close.

## Top Aggregate Results

| Entry Band | Total Triggered | Weighted Avg | Avg Median | Worst Median | Avg P25 |
|---|---:|---:|---:|---:|---:|
| open -3% to +2% | 362 | +0.97% | +0.41% | -0.21% | -1.54% |
| open -2% to +2% | 335 | +0.90% | +0.36% | -0.43% | -1.50% |
| open -1% to +2% | 265 | +0.82% | +0.34% | -0.51% | -1.42% |
| open -3% to +1% | 315 | +0.90% | +0.33% | -0.21% | -1.79% |
| open -2% to +1% | 288 | +0.82% | +0.29% | -0.43% | -1.64% |

Best aggregate: `open -3% to +2%`.

## Important Window Behavior

Hard recent sample `2024-09~2025-03` preferred not chasing too much:

- `open -3% to 0%`: median +0.12%
- `open -3% to +1%`: median -0.21%
- `open -3% to +2%`: median -0.21%
- `open 0% to +2%`: median -0.92%
- `open 0% to +3%`: median -1.45%

Recent YTD `2025-04~2026-05-13` preferred some positive open:

- `open 0% to +1%`: median +1.06%, but only 80 triggered
- `open -3% to +1%`: median +0.27%, 215 triggered
- `open -3% to +2%`: median +0.12%, 246 triggered

Read: opening strength can work in the 2025-2026 sample, but it performed poorly in the 2024-09~2025-03 hard sample. The safer aggregate band keeps downside entries allowed and caps chase at +2%.

## Current Interpretation

Compared with the old intraday-touch entry model, open-only entry is stricter and more realistic for a bot that executes at open.

The best practical candidate is:

```text
T+1 open entry only
open between -3% and +2% versus signal close
```

More conservative candidate:

```text
T+1 open entry only
open between -3% and +1%
```

Avoid:

```text
open-only chasing 0% to +3%
```

It looks good in 2025-2026 YTD but fails badly in the hard 2024-09~2025-03 window.

## Outputs

- `s1_open_entry_grid_20260513.py`
- `s1_open_entry_grid_20260513.csv`
- `s1_open_entry_grid_agg_20260513.csv`
