# calibration — 周度跑 bot belief 校准

```
python calibration.py            # 扫 bots/*/memory/portfolio/fund/市场环境判断.md
python calibration.py --mock     # 临时目录造 5 份合成 belief 跑通 pipeline
```

依赖 Belief Schema v1: frontmatter 里有 `belief.target_index / horizons{t+1,t+5,t+20}.{p_up,prior_p_up,delta} / evidence / activity_self_check`.

输出 (`results/`):

- `brier_summary.csv` — (bot, index, horizon, n, brier, baseline_brier, improvement). improvement>0 表示 bot 比 base rate 强.
- `activity_summary.csv` — 滚动 21 日 |Δp_up(t+1)| 中位数 + status (alive ≥0.05 / warn 0.03~0.05 / dead <0.03).
- `calibration_bins.csv` — 10 bin 的 mean_p vs mean_actual, 看校准曲线偏离 45°.

收盘价复用 `research/sr_factor/data/*.csv` (hs300/csi500/zz1000/chinext).
