# S1 Regime Validation — 2026-05-14

Status: research only.

## Question

Does the latest S1 candidate only work in `STRONG_RANGE`, or can it also run in other market regimes?

## Setup

Latest S1 candidate:

- Platform width <= 14%
- Breakout >= 2%
- Volume ratio >= 1.5x
- Amount >= 5亿
- `ret20 >= 10%`
- `rel20 >= 5%`
- T+1 open-only entry
- Entry bands tested: `-3%~+2%`, `-3%~+1%`, `-3%~0%`
- Hold: 2 days

Windows:

- 2019
- 2020
- 2024-09 to 2025-03
- 2025-04 to 2026-05-13

Regime sets:

- `STRONG_BULL`
- `STRONG_RANGE`
- `NEUTRAL_RANGE`
- `STRONG_BULL,STRONG_RANGE`
- `STRONG_RANGE,NEUTRAL_RANGE`
- `STRONG_BULL,STRONG_RANGE,NEUTRAL_RANGE`
- Same broad set with `min_regime_score=3`

Outputs:

- `s1_regime_open_entry_20260514.csv`
- `s1_regime_open_entry_agg_20260514.csv`

## Aggregate Result

| Regime Set | Entry Band | Total Triggered | Weighted Avg | Avg Median | Worst Median | Avg P25 |
|---|---|---:|---:|---:|---:|---:|
| STRONG_RANGE | -3% to +2% | 362 | +0.97% | +0.41% | -0.21% | -1.54% |
| STRONG_RANGE | -3% to +1% | 315 | +0.90% | +0.33% | -0.21% | -1.79% |
| STRONG_BULL + STRONG_RANGE | -3% to +2% | 529 | +0.85% | +0.18% | -0.16% | -1.97% |
| STRONG_BULL + STRONG_RANGE | -3% to +1% | 466 | +0.77% | +0.17% | -0.12% | -2.13% |
| BULL/RANGE/NEUTRAL score>=3 | -3% to +2% | 472 | +0.91% | +0.06% | -0.43% | -1.75% |
| STRONG_RANGE + NEUTRAL_RANGE | -3% to +2% | 504 | +0.58% | +0.04% | -0.59% | -2.09% |
| STRONG_BULL | -3% to +2% | 167 | +0.58% | -0.14% | -1.65% | -2.35% |
| NEUTRAL_RANGE | -3% to +2% | 142 | -0.40% | -1.08% | -1.71% | -3.13% |

## Read

### STRONG_RANGE remains the cleanest regime

`STRONG_RANGE` has the best average median and best average P25. It is the only regime set with a clearly positive aggregate median while keeping all four windows reasonably populated.

### STRONG_BULL is unstable

`STRONG_BULL` worked in 2020 and partly in 2024-09~2025-03, but failed badly in 2019 and 2025-2026 YTD.

Examples:

- 2019 `STRONG_BULL -3%~+2%`: median -1.65%
- 2020 `STRONG_BULL -3%~+2%`: median +2.18%
- 2025-2026 YTD `STRONG_BULL -3%~+2%`: median -0.93%

Conclusion: strong bull alone is not reliable enough for live S1.

### NEUTRAL_RANGE should stay forbidden

`NEUTRAL_RANGE` is consistently weak:

- Aggregate weighted avg negative
- Average median around -1%
- Worst median below -1.7%
- P25 much worse than STRONG_RANGE

Conclusion: do not run S1 in neutral range.

### STRONG_BULL + STRONG_RANGE is possible as research mode

Adding `STRONG_BULL` to `STRONG_RANGE` increases trade count and keeps weighted average positive, but dilutes the median and worsens P25.

This can be watched in paper/research mode, but production candidate should remain `STRONG_RANGE` only.

## Recommendation

For S1 v0 observation skill:

```text
Allowed regime: STRONG_RANGE only
Research-only optional view: STRONG_BULL + STRONG_RANGE
Forbidden: NEUTRAL_RANGE, WEAK_RANGE, BEAR
```

Entry:

```text
T+1 open-only
preferred: -3% to +2%
conservative: -3% to +1%
```

If this becomes a live-observation skill, expose non-`STRONG_RANGE` signals only as "research candidates", not executable candidates.
