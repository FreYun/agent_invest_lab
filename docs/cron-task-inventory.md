# agent_invest_lab 定时任务清单

整理时间：2026-07-22

范围：当前机器上与 `/home/rooot/agent_invest_lab` 项目直接相关的 crontab 任务和 systemd user timer。`.openclaw` 自身任务不展开；只在项目任务复用 `.openclaw/scripts/cron-wrapper.sh` 时作为 wrapper 记录。

## 关键结论

- `run-oos-bot101-daily.sh` 和 `refresh-oos-bot101-nav.sh` 保留了 `bot101` 文件名和部分注释，但实际默认处理 `bot101 bot102 bot103`。
- 14:30 OOS bot 决策阶段是并行执行三个 bot；08:00 NAV refresh 当前按 bot 顺序串行。
- 项目主要写两个 SQLite 库：
  - `data/fund.db`：基金池、OOS bot、前端 market reports / oos 镜像。
  - `data/market.db`：市场行情、选股、scout、主线、业绩、估值、融资融券等。
- 当前 crontab 中 `scripts/sync-fund-data-with-68-cron-FULL-PINGPONG.sh` 在项目目录不存在，是需要处理的风险项。

## fund.db 相关任务

| 调度 | 入口 | 更新主体 | 写入库/文件 | 主要写入表 |
|---|---|---|---|---|
| 每天 06:17 | `scripts/fund-lab-daily-refresh.sh` | lab 基金池 NAV/业绩刷新 | `data/fund.db` | `fund_nav`, `fund_performance`, `fund_nav_performance` |
| 工作日 14:30 | `scripts/run-oos-bot101-daily.sh` | OOS `bot101/bot102/bot103` 每日决策，bot 阶段并行 | `data/fund.db` | `market_reports`, `fund_bot_accounts`, `fund_bot_orders`, `fund_bot_actions`, `fund_bot_holdings`, `fund_bot_reviews`, `fund_bot_daily_snapshots`, `fund_bot_position_snapshots`, `fund_bot_performance`, `fund_paradigm_runs`, `fund_allocation_runs`, `fund_selection_runs`, `oos_market_report_status`, `oos_bot_daily_snapshots`, `oos_bot_position_snapshots`, `oos_bot_orders`, `oos_bot_actions` |
| 每天 08:00 | `scripts/refresh-oos-bot101-nav.sh` | OOS `bot101/bot102/bot103` 订单结算、净值/持仓重算、前端镜像 | `data/fund.db` | `fund_bot_orders`, `fund_bot_actions`, `fund_bot_holdings`, `fund_bot_accounts`, `fund_bot_daily_snapshots`, `fund_bot_position_snapshots`, `fund_bot_performance`, `oos_market_report_status`, `oos_bot_daily_snapshots`, `oos_bot_position_snapshots`, `oos_bot_orders`, `oos_bot_actions` |
| 每天 08:30 | `scripts/run-mainline-daily.sh` | 日度主线报告生成与补漏 | `data/fund.db` | `market_reports`，`report_type` 为 `market_mainline_daily`, `mainline_rotation_daily` |

### OOS daily 细节

`scripts/run-oos-bot101-daily.sh` 的默认 bot 列表：

```bash
OOS_BOTS="bot101 bot102 bot103"
```

每个 bot 默认 run_id：

| bot | run_id |
|---|---|
| `bot101` | `oos-bot101-daily` |
| `bot102` | `oos-bot102-daily` |
| `bot103` | `oos-bot103-daily` |

14:30 任务主要流程：

1. 生成或确认当日三份 market reports：`market_context`, `market_mainline`, `mainline_rotation`。
2. 重启 `lab-fund-bot-only.service`，避免 fund MCP 长时间运行状态污染。
3. 并行运行 `world/src/oos-daily-driver.ts`，分别处理 `bot101/bot102/bot103`。
4. 将 `fund_bot_*` 执行账本镜像到 `oos_*` 前端展示表。

OOS 镜像表由 `scripts/sql/oos_bot101_daily.sql` 创建：

- `oos_market_report_status`
- `oos_bot_daily_snapshots`
- `oos_bot_position_snapshots`
- `oos_bot_actions`
- `oos_bot_orders`

镜像逻辑在 `scripts/oos-bot101-mirror.lib.sh`。其中 NAV 和持仓快照只镜像到 `trade_date <= MAX(fund_nav.nav_date)`，避免基金净值还没发布时前端曲线提前显示同日净值。

## market.db 相关任务

| 调度 | 入口 | 更新主体 | 写入库 | 主要写入表 |
|---|---|---|---|---|
| 工作日 21:07 | `market_pipeline/openclaw/scripts/daily-regime-pipeline.sh` | 市场 regime / 基础行情层 | `data/market.db` | `daily`, `stk_limit`, `regime_raw_daily`, `index_daily`, `regime_classify_daily`, `limit_up_pool`, `hot_industries_daily`, `s5_daily_universe`, `klines_cache`, `mkt_gvix_daily`, `mkt_northbound_daily`, `mkt_index_val_daily`, `mkt_bond_yield_daily` |
| 工作日 21:27 | `market_pipeline/openclaw/scripts/s5-daily-cron.sh` | S5 龙回头选股 + T+1 验证 | `data/market.db` | `s5_select_runs`, `s5_candidates`, `s5_candidate_rejects`, `s5_verifications` |
| 工作日 21:32 | `market_pipeline/openclaw/scripts/s1s2s3s4-daily-cron.sh` | S1/S2/S3/S6/S7 选股 + T+1 验证 | `data/market.db` | `s1_select_runs`, `s1_candidates`, `s1_candidate_rejects`, `s1_verifications`, `s2_select_runs`, `s2_candidates`, `s2_candidate_rejects`, `s2_verifications`, `s3_select_runs`, `s3_candidates`, `s3_candidate_rejects`, `s3_verifications`, `s6_select_runs`, `s6_candidates`, `s6_candidate_rejects`, `s6_verifications`, `s7_select_runs`, `s7_candidates`, `s7_candidate_rejects`, `s7_verifications` |
| 工作日 21:52 | `market_pipeline/openclaw/scripts/stock-select-daily.sh` | 选股框架数据层、主线、业绩、估值、融资融券派生 | `data/market.db` | `daily_basic`, `moneyflow_daily`, `concept_board_daily`, `kpl_theme_daily`, `stock_concept_map`, `backfill_progress`, `board_leader_daily`, `board_trend_daily`, `emerging_signal_daily`, `board_etf_daily`, `margin_security_daily`, `margin_index_daily`, `margin_board_daily`, `board_moneyflow_daily`, `market_moneyflow_daily`, `earnings_events`, `earnings_resonance_daily`, `valuation_band_daily`, `valuation_quarter_est`, `consensus_profit_daily` |
| 工作日 22:08 | `market_pipeline/openclaw/scripts/s8/s8-daily-cron.sh` | S8 主线分歧低吸选股 | `data/market.db` | `s8_select_runs`, `s8_candidates` |
| 工作日 22:13 | `market_pipeline/openclaw/scripts/s9/s9-daily-cron.sh` | S9 大市值 regime 自适应选股 | `data/market.db` | `s9_select_runs`, `s9_candidates` |
| 工作日盘中每 2 分钟 | `market_pipeline/openclaw/scout/collect.py` | scout 盘中快照、实时候选、板块、触发记录 | `data/market.db` | `intraday_snapshot`, `intraday_candidate_live`, `intraday_trigger_log`, `intraday_board`, `intraday_board_members`, `candidate_logic`, `lof_arbitrage_live` |
| 工作日 15:42 | `market_pipeline/openclaw/scout/prune.py` | scout 盘中数据保留清理 | `data/market.db` | 删除旧 `intraday_snapshot`, `intraday_board`, `intraday_candidate_live` |
| 工作日 09:35 | `market_pipeline/openclaw/scout/margin_flow.py` | 融资融券早间补跑 | `data/market.db` | `margin_security_daily`, `margin_index_daily`, `margin_board_daily` |
| 工作日 08:50 | `market_pipeline/openclaw/scripts/earnings-refresh.sh` | 业绩预告/快报刷新 + 业绩共振 | `data/market.db` | `earnings_events`, `earnings_resonance_daily` |
| 工作日 22:25 | `market_pipeline/openclaw/scout/sw_industry_sync.py` | 申万行业成分同步 | `data/market.db` | `sw_industry_member` |

### market pipeline 任务依赖

市场侧的日终任务存在明确顺序：

1. `daily-regime-pipeline.sh` 先写基础行情、regime、S5 缓存。
2. `s5-daily-cron.sh`、`s1s2s3s4-daily-cron.sh` 读取基础行情与 regime，写各策略候选和验证表。
3. `stock-select-daily.sh` 补选股框架数据层，并派生主线、业绩、估值、融资融券等表。
4. `s8-daily-cron.sh`、`s9-daily-cron.sh` 读取 `daily_basic`、`kpl_theme_daily`、`index_daily` 等数据后写各自候选。

盘中 `collect.py` 独立高频运行，每 2 分钟采集实时行情和候选状态，盘后 `prune.py` 清理旧盘中帧。

## systemd user timer

这些不是 crontab，但属于项目定时任务。

| 调度 | timer/service | 更新主体 | 写入 |
|---|---|---|---|
| 工作日 07:45、15:20；周二到周六 09:30 | `world-calendar-refresh.timer` -> `world-calendar-refresh.service` -> `world/scripts/refresh-calendar.ts` | 同步 SimWorld 当前可用交易日 | 写 `world/runtime/calendar.json`，不写数据库 |
| 工作日 14:00 | `live-decide.timer` -> `live-decide.service` -> `world/live/live_decide.py` | live run 盘中决策，并发 5 个 run | 通过 `oos-daily-driver --phase decide` 写 `fund_bot_orders` 等交易决策表，并写 `world/runtime/runs/...` 运行文件 |
| 工作日 18:30 | `live-settle.timer` -> `live-settle.service` -> `world/live/live_settle.py` | live run 盘后结算，并发 5 个 run | 写 `fund_bot_orders`, `fund_bot_actions`, `fund_bot_holdings`, `fund_bot_accounts`, `fund_bot_daily_snapshots`, `fund_bot_position_snapshots`, `fund_bot_performance` |

## 68 同步任务风险项

当前 crontab 中存在：

```cron
*/10 * * * * /home/rooot/agent_invest_lab/scripts/sync-fund-data-with-68-cron-FULL-PINGPONG.sh >> /home/rooot/agent_invest_lab/logs/sync-fund-68-FULL.log 2>&1
```

但当前项目目录下没有这个文件。当前实际存在的是：

```text
scripts/sync-fund-data-with-68.sh
```

`scripts/sync-fund-data-with-68.sh` 的同步白名单表：

- `fund_info`
- `fund_nav`
- `fund_nav_performance`
- `fund_performance`
- `fund_style`
- `fund_top_stocks`
- `fund_industry`
- `fund_capability_circle`
- `real_user_cycles`
- `real_user_txns`
- `real_user_curves`
- `market_reports`

该脚本明确不同步以下回测/run 账本，避免两台机器互相覆盖：

- `fund_bot_*`
- `fund_allocation_runs`
- `fund_selection_runs`
- `fund_system_runs`
- `fund_paradigm_runs`

如果 68 同步仍然需要保留，建议把 crontab 改到真实存在的脚本，或补回 `sync-fund-data-with-68-cron-FULL-PINGPONG.sh` 包装脚本。

## 当前 crontab 中项目相关入口

```cron
# Lab fund.db daily nav+performance refresh
17 6 * * * /home/rooot/.openclaw/scripts/cron-wrapper.sh lab-fund-daily-refresh /bin/bash /home/rooot/agent_invest_lab/scripts/fund-lab-daily-refresh.sh >> /home/rooot/agent_invest_lab/logs/fund-lab-daily-refresh.log 2>&1

# Lab OOS bot daily
30 14 * * 1-5 /home/rooot/.openclaw/scripts/cron-wrapper.sh oos-bot101-daily /bin/bash -lc 'cd /home/rooot/agent_invest_lab && OOS_BOT101_INTRADAY=1 ./scripts/run-oos-bot101-daily.sh' >> /home/rooot/agent_invest_lab/logs/oos-bot101-daily-cron.log 2>&1

# Lab OOS NAV sync
0 8 * * * /home/rooot/.openclaw/scripts/cron-wrapper.sh oos-bot101-nav-sync /bin/bash /home/rooot/agent_invest_lab/scripts/refresh-oos-bot101-nav.sh >> /home/rooot/agent_invest_lab/logs/oos-bot101-nav-sync.log 2>&1

# 68 sync risk item: script currently missing
*/10 * * * * /home/rooot/agent_invest_lab/scripts/sync-fund-data-with-68-cron-FULL-PINGPONG.sh >> /home/rooot/agent_invest_lab/logs/sync-fund-68-FULL.log 2>&1

# Lab mainline daily reports
30 8 * * * /home/rooot/.openclaw/scripts/cron-wrapper.sh mainline-daily /bin/bash -lc 'cd /home/rooot/agent_invest_lab && ./scripts/run-mainline-daily.sh' >> /home/rooot/agent_invest_lab/logs/mainline-daily-cron.log 2>&1

# Project-local market.db pipeline
7 21 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh lab-market-daily-regime-pipeline /bin/bash /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/daily-regime-pipeline.sh >> /home/rooot/agent_invest_lab/logs/lab-market-daily-regime-pipeline.log 2>&1
27 21 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh lab-market-s5-daily-cron /bin/bash /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s5-daily-cron.sh >> /home/rooot/agent_invest_lab/logs/lab-market-s5-daily.log 2>&1
32 21 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh lab-market-s1s2s3s4-daily-cron /bin/bash /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s1s2s3s4-daily-cron.sh >> /home/rooot/agent_invest_lab/logs/lab-market-s1s2s3s4-daily.log 2>&1
52 21 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh lab-market-stock-select-daily /bin/bash /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/stock-select-daily.sh >> /home/rooot/agent_invest_lab/logs/lab-market-stock-select-daily.log 2>&1
8 22 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh lab-market-s8-daily-cron /bin/bash /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s8/s8-daily-cron.sh >> /home/rooot/agent_invest_lab/logs/lab-market-s8-daily.log 2>&1
13 22 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh lab-market-s9-daily-cron /bin/bash /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s9/s9-daily-cron.sh >> /home/rooot/agent_invest_lab/logs/lab-market-s9-daily.log 2>&1
*/2 9-11,13-15 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh lab-market-scout-collect /usr/bin/flock -n -E 0 /tmp/lab-market-scout-collect.lock /usr/bin/timeout 100 /home/rooot/agent_invest_lab/.venv/bin/python /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/collect.py >> /home/rooot/agent_invest_lab/logs/lab-market-scout-collect.log 2>&1
42 15 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh lab-market-scout-prune /home/rooot/agent_invest_lab/.venv/bin/python /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/prune.py >> /home/rooot/agent_invest_lab/logs/lab-market-scout-prune.log 2>&1
35 9 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh lab-market-margin-flow-morning /home/rooot/agent_invest_lab/.venv/bin/python /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/margin_flow.py >> /home/rooot/agent_invest_lab/logs/lab-market-margin-flow-morning.log 2>&1
50 8 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh lab-market-earnings-refresh /bin/bash /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/earnings-refresh.sh >> /home/rooot/agent_invest_lab/logs/lab-market-earnings-refresh.log 2>&1
25 22 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh lab-market-sw-industry-sync /home/rooot/agent_invest_lab/.venv/bin/python /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/sw_industry_sync.py >> /home/rooot/agent_invest_lab/logs/lab-market-sw-industry-sync.log 2>&1
```
