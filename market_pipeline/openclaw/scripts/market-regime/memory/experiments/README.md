# 实验归档

历史研究脚本，结论已在别处落地实施，代码留作"结论证据"不再运行。**如果以后觉得不需要，整个目录 `rm -rf` 掉即可**。

移入日期：2026-04-17
移入前位置：`scripts/backfill/`

> 注意：脚本当年都从 `scripts/backfill/` 下的 `sys.path` 查找同层模块（db.py/rules_v2.py/derive_raw_data.py 等）。移到 memory 后 import 路径会断，想重跑需要先改 `sys.path`。

## 实验报告 (reports/)

| 文件 | 对应实验 |
|------|---------|
| `reports/ma_trend_filter_2026-04.md` | `ma_trend_filter_backtest.py` 的结论报告 |
| `reports/v2_regime_filter_2026-04.md` | `v2_regime_filter_backtest.py` 的结论报告 (v2 六维 → position-sizer v2_entry_only 依据) |
| `reports/signal_evaluation_2026-04.md` | `evaluate_signal.py` 的 forward-return 分析 |
| `reports/strategy_comparison.html` | `generate_strategy_chart.py` 产的净值+回撤对比图 |
| `reports/strategy_comparison_blended.html` | 同上, blended 版本 |

## 脚本清单

| 文件 | 当初目的 | 结论 / 产出 |
|------|---------|-----------|
| `backfill_raw_data.py` | 从 Tushare 拉 3 年历史数据入 `market.db`（日线/指数/涨停池/情绪） | 已跑完，`regime_raw_daily` / `daily` / `stk_limit` / `index_daily` 4 张表填满 2015-至今。**新机器上 market.db 重建时可复用** |
| `ma_trend_filter_backtest.py` | MA 趋势过滤器回测（HS300 MA200 当开关） | 单目标控回撤，结果用于对照组 |
| `v2_regime_filter_backtest.py` | v2 六维信号当"过滤器/仓位表"的 7 种策略回测 | 与 MA 过滤器横向比较，筛选出 `v2_entry_only` 规则（现已实装 [position-sizer](../../../position-sizer/SKILL.md)） |
| `simulate_strategies.py` | 按 v2 regime 跑策略净值模拟，产物写 `regime_strategy_nav` 表 | 净值数据留在 market.db，供 HTML 图表消费 |
| `evaluate_signal.py` | 严格无 look-ahead 的 v2 信号有效性评估（T+1 open 操作 + forward return） | 用于验证 classifier 六维打分的 IC，结论已并入 [mapping.md](../../references/mapping.md) 设计依据 |
| `generate_html_report.py` | HS300 + regime 彩色带 + 得分柱状图的单文件 HTML 可视化 | 一次性报告，用于人工看 regime 分段是否合理 |
| `generate_strategy_chart.py` | 策略净值 + 回撤对比图（Chart.js CDN） | 一次性报告，给研究部看的 |

## 仍在生产链路的 (保留在 scripts/backfill/)

`replay.py` / `load_to_db.py` / `db.py` / `derive_raw_data.py` / `rules_v2.py` / `__init__.py` — 这几个被 `/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/daily-regime-pipeline.sh` cron 调用，**不能删**。
