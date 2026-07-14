# S1 Breakout × Volume Grid — 2026-05-13

Status: research only. This is a focused response to the hypothesis that larger breakouts may need stronger volume confirmation, and that amount gates may be redundant with volume ratio.

## Setup

Serial one-process run:

- `OMP_NUM_THREADS=1`
- `OPENBLAS_NUM_THREADS=1`
- `MKL_NUM_THREADS=1`
- `NUMEXPR_NUM_THREADS=1`

Base rule:

- Regime: `STRONG_RANGE`
- Base window: 20
- Platform width: <= 14%
- Entry: T+1 only, -2% to 0%
- Hold: 2 days
- Strength: `ret20 >= 10%`, `rel20 >= 5%`

Grid:

- Breakout threshold: `1%, 2%, 3%, 4%, 5%`
- Volume ratio: `1.0, 1.25, 1.5, 2.0, 2.5`
- Amount gate: `0, 1, 3, 5` 亿
- Windows: `2019`, `2020`, `2024-09~2025-03`, `2025-04~2026-05-13`

Outputs:

- `s1_breakout_volume_grid_20260513.csv`
- `s1_breakout_volume_grid_agg_20260513.csv`

## Top Aggregate Configs

Ranked by: all 4 windows have >=20 triggered trades, then average median PnL, then weighted average PnL.

| Config | Total Triggered | Weighted Avg | Avg Median | Worst Median | Avg P25 |
|---|---:|---:|---:|---:|---:|
| breakout >=2%, vol >=1.5x, amount >=5亿 | 378 | +0.87% | +0.47% | +0.00% | -1.83% |
| breakout >=2%, vol >=2.0x, amount >=3亿 | 486 | +0.69% | +0.41% | -0.19% | -1.82% |
| breakout >=2%, vol >=1.25x, amount >=5亿 | 393 | +0.85% | +0.39% | -0.16% | -1.83% |
| breakout >=2%, vol >=1.5x, amount >=3亿 | 536 | +0.64% | +0.39% | -0.13% | -1.79% |
| breakout >=2%, vol >=2.5x, amount >=3亿 | 393 | +0.75% | +0.35% | -0.21% | -1.95% |

Best research candidate:

`breakout >= 2%, volume_ratio >= 1.5, amount >= 5亿`

This is the only top config with worst-window median at `>= 0`.

## Key Findings

### 1. Amount gate is not redundant

The amount gate changed results materially even with the same volume ratio.

Examples:

- 2019, breakout >=2%, vol >=1.25:
  - amount >=1亿: 92 triggered, avg +0.44%, median +0.55%, P25 -1.27%
  - amount >=3亿: 40 triggered, avg +1.05%, median +0.95%, P25 -0.52%
- 2020, breakout >=2%, vol >=1.25:
  - amount >=1亿: 180 triggered, avg +0.10%, median +0.11%, P25 -2.57%
  - amount >=3亿: 77 triggered, avg +0.86%, median +0.56%, P25 -1.21%
  - amount >=5亿: 51 triggered, avg +1.09%, median +0.56%, P25 -1.18%
- 2025-2026 YTD, breakout >=2%, vol >=1.25:
  - amount >=1亿: 394 triggered, avg +0.41%, median -0.21%, P25 -2.82%
  - amount >=5亿: 271 triggered, avg +0.63%, median +0.40%, P25 -2.43%

Interpretation: volume ratio confirms relative activity, but amount confirms liquidity / institutional-size participation. A small-cap can show high volume ratio on thin money; that is not equivalent to real participation.

### 2. Larger breakout is not automatically better

Breakout >=4% and >=5% often worsened recent samples.

Recent hard sample `2024-09~2025-03`:

- breakout >=2%, vol >=1.5, amount >=5亿: 43 triggered, win 46.5%, avg +1.94%, median +0.00%
- breakout >=4%, vol >=1.5, amount >=5亿: 23 triggered, win 39.1%, avg +0.26%, median -1.37%
- breakout >=5%, vol >=1.5, amount >=5亿: 15 triggered, win 33.3%, avg +0.12%, median -2.70%

Interpretation: in recent markets, a very large close-above-platform breakout behaves more like short-term exhaustion or next-day bad entry, not stronger confirmation.

### 3. Stronger volume ratio helps only up to a point

The best configs clustered around volume ratio `1.25x~2.0x`, especially `1.5x`.

Volume ratio `2.5x` sometimes improved selectivity, but it often reduced sample size and did not dominate. It can also select emotional blow-off bars.

### 4. The best shape is moderate breakout + real amount + moderate volume

Current best hypothesis:

`breakout >=2%, volume_ratio >=1.5, amount >=5亿`

Alternative if sample size matters more:

`breakout >=2%, volume_ratio >=1.5, amount >=3亿`

The 3亿 version has more trades and slightly better average P25 in aggregate, but a weaker worst-window median.

## Updated Answer to “Why Amount Gate?”

The amount gate is useful because it measures a different thing:

- `volume_ratio`: today is active relative to this stock’s own recent trading.
- `amount_yi`: today has enough absolute money involved.

S1 is trying to catch a breakout that can be followed by continuation. For that, relative activity alone is not enough. A low-liquidity stock can show high relative volume but still lack durable participation, suffer bigger slippage, and reverse faster.

## Updated Candidate Rule

Do not widen breakout threshold blindly.

Next candidate should test:

```text
STRONG_RANGE
base_window = 20
max_base_range_pct = 14
min_breakout_pct = 2
min_volume_ratio = 1.5
min_amount_yi = 5
entry band = -2% to 0%
max_hold_days = 2
ret20 >= 10
rel20 >= 5
top2/day
avoid hot_industry_rank <= 1 or <= 3
```

This combines the earlier crowding result with the new liquidity/volume result.

## Not Yet Done

- This grid did not combine top2/day and hot-industry exclusion.
- It did not include transaction cost or slippage.
- It did not inspect individual losers.
- It did not test dynamic rules like “if breakout >=4%, require amount >=5亿 and entry must be lower.”
