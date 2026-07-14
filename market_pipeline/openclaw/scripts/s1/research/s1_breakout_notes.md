# S1 Breakout Research Notes

Research status: not ready for skill implementation.

## Execution model

- Signal is generated after T close.
- Buy is attempted on T+1 only.
- Sell is allowed from T+2 onward because A-share stocks are T+1.
- One-word limit-up on T+1 is treated as not buyable.
- One-word limit-down after entry is treated as not sellable.
- Stop loss that gaps through the stop exits at the next tradable open/price, not at an ideal stop price.

## 2024-09-01 to 2025-03-31 baseline

Default S1 platform breakout rule:

- Bull days: 40
- Candidates: 145 before the later strength-filter experiments
- Triggered: 102
- Win rate: 35.3%
- Average PnL: -1.43%

Conclusion: the raw platform breakout rule is negative under realistic T+1 execution.

## Small cached grid

Window: 2024-09-01 to 2025-03-31.

The cached 8-combo grid completed in about 11 seconds. All tested platform-only combinations had negative average PnL:

- Best platform-only combo: base 30, range <= 10%, breakout >= 2%
- Candidates: 35
- Triggered: 29
- Win rate: 20.7%
- Average PnL: -1.19%

Conclusion: tightening only the platform width/breakout threshold is not sufficient.

## Strength-filter experiment

Added "strong stock" filters:

- 20-day stock return
- 20-day excess return vs 000852.SH
- Optional 60-day stock return

Best in-sample candidate:

- Window: 2024-09-01 to 2025-03-31
- Rule: base 20, range <= 14%, breakout >= 2%, ret20 >= 10%, rel20 >= 5%
- Candidates: 54
- Triggered: 46
- Win rate: 37.0%
- Average PnL: +1.63%
- Median PnL: -1.86%
- P25/P75: -6.19% / +2.11%
- Max/min: +50.87% / -8.36%

Conclusion: relative strength helps, but the positive average is fragile because the median remains negative and the result is pulled up by a small number of large winners.

## Cross-sample checks

Same candidate rule: base 20, range <= 14%, breakout >= 2%, ret20 >= 10%, rel20 >= 5%.

| Window | Bull days | Triggered | Win rate | Avg PnL | Median PnL |
|---|---:|---:|---:|---:|---:|
| 2015-01-01 to 2015-12-31 | 82 | 11 | 27.3% | -3.21% | -4.48% |
| 2019-01-01 to 2019-12-31 | 51 | 74 | 31.1% | -2.08% | -2.71% |
| 2020-01-01 to 2020-12-31 | 47 | 64 | 40.6% | -0.16% | -1.54% |
| 2024-09-01 to 2025-03-31 | 40 | 46 | 37.0% | +1.63% | -1.86% |

Conclusion: the current S1 rule is not stable enough to develop into a boot skill.

## Regime relaxation experiment

I changed the research harness so the regime gate is configurable instead of hard-coded to `STRONG_BULL`.
This let me test whether S1 can work beyond strong bull days while keeping the same realistic A-share execution model.

### 2024-09-01 to 2025-03-31

Same stock filter: base 20, range <= 14%, breakout >= 2%, ret20 >= 10%, rel20 >= 5%.

| Regime gate | Min score | Eligible days | Triggered | Win rate | Avg PnL | Median PnL |
|---|---:|---:|---:|---:|---:|---:|
| STRONG_BULL | - | 40 | 46 | 37.0% | +1.63% | -1.86% |
| STRONG_RANGE | - | 58 | 109 | 40.4% | +0.41% | -1.54% |
| STRONG_BULL + STRONG_RANGE | - | 98 | 155 | 39.4% | +0.77% | -1.58% |
| STRONG_BULL + STRONG_RANGE + NEUTRAL_RANGE | 2 | 90 | 141 | 39.7% | +1.84% | -1.54% |
| STRONG_BULL + STRONG_RANGE + NEUTRAL_RANGE | 3 | 85 | 137 | 40.9% | +2.09% | -1.30% |

### Cross-sample sanity checks for the relaxed regime gate

Same rule as above, with `regimes = STRONG_BULL,STRONG_RANGE,NEUTRAL_RANGE` and `min_regime_score = 3`.

| Window | Triggered | Win rate | Avg PnL | Median PnL |
|---|---:|---:|---:|---:|
| 2015-01-01 to 2015-12-31 | 38 | 42.1% | +0.80% | -1.64% |
| 2019-01-01 to 2019-12-31 | 197 | 45.7% | -0.17% | -0.32% |
| 2020-01-01 to 2020-12-31 | 235 | 43.8% | +0.31% | -0.86% |

### Current conclusion

- S1 does not need to be hard-limited to `STRONG_BULL`.
- A broader gate of `STRONG_BULL + STRONG_RANGE + selected NEUTRAL_RANGE` is materially better than strong bull only.
- But the median PnL is still negative across samples, so the edge is still fragile and not yet good enough for a production skill.
- Next thing to test is exit design, not further loosening of the regime gate.

## Exit and entry tuning experiment

I tested the stricter execution assumptions the user asked for:

- T signal after close
- T+1 buy only
- T+2 sell only
- one-word limit-up blocks entry
- one-word limit-down blocks exit

### Candidate research settings

The most useful candidate so far is:

- Regime gate: `STRONG_RANGE`
- Entry filter: base 20, range <= 14%, breakout >= 2%, ret20 >= 10%, rel20 >= 5%
- T+1 entry band: `entry_low_pct = -2`, `entry_high_pct = 0`
- Exit: `max_hold_days = 2`

### Recent sample: 2024-09-01 to 2025-03-31

| Regime gate | Triggered | Win rate | Avg PnL | Median PnL |
|---|---:|---:|---:|---:|
| STRONG_BULL, STRONG_RANGE, NEUTRAL_RANGE | 138 | 43.5% | +1.18% | -0.43% |
| STRONG_RANGE, NEUTRAL_RANGE | 96 | 40.6% | +1.24% | -0.51% |
| STRONG_RANGE only | 81 | 40.7% | +1.30% | -0.59% |

### Cross-sample checks for `STRONG_RANGE` only

| Window | Triggered | Win rate | Avg PnL | Median PnL |
|---|---:|---:|---:|---:|
| 2015-01-01 to 2015-12-31 | 19 | 89.5% | +3.03% | +2.89% |
| 2019-01-01 to 2019-12-31 | 77 | 58.4% | +0.47% | +0.59% |
| 2020-01-01 to 2020-12-31 | 130 | 50.8% | +0.41% | +0.13% |

### Current conclusion

- The current S1 is not a pure strong-bull breakout.
- `STRONG_RANGE` is the clearest workable regime.
- `STRONG_BULL` is not consistently attractive cross-sample.
- Shortening the hold to 2 days is better than keeping positions for 5 days.
- The current candidate entry band should avoid chasing above the signal close; `entry_high_pct = 0` is better than the earlier `+2%`.
- This still needs more validation before skill implementation.

## Next research direction

- Keep S1 in research-only status.
- Continue validating `STRONG_RANGE` as the main S1 regime.
- Test whether a tiny profit-protection rule improves the 2-day hold version without cutting too much upside.
- Test whether sector leadership or industry momentum can reduce the remaining negative median.
- Do not skillize until the regime and exit behavior are stable across multiple samples.
