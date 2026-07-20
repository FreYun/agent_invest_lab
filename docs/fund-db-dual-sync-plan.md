# fund.db 三机迁移与备份方案（107→30 迁移，30→68 备份）

> 状态：**方案稿，待评审**。只做设计，不含可直接启用的生产脚本。
> 拓扑：107（旧主力，待废弃）→ 30（新主力）→ 68（纯备份）。

---

## 0. TL;DR（一页纸结论）

- **三台机新格局**：**107** 当前主力、数据最全、硬件不行待废弃；**30** 性能强、未来主力开发机；**68** 磁盘大、稳定、纯数据备份机。
- **两阶段任务**：
  1. **一次性全量迁移 `107 → 30`**：把 107 上最全的 `fund.db`（+ 必要运行时）搬到 30，让 30 成为新主力。
  2. **稳态单向备份 `30 → 68`**：30 成为主力后，每天 3 次（09:20 / 14:00 / 20:00）把数据单向备份到 68。
  3. 迁移验收通过后，**107 退役**，退出拓扑。
- **关键简化**：全程**单向**（迁移=整库灌入；备份=单向镜像），之前 107↔68 双向方案纠结的「自增 `id` 撞键 / 行级合并冲突」**不复存在**，方案大幅瘦身、可靠性更高。
- **核心交付物**：①阶段一迁移 runbook（§4）②阶段二备份引擎+调度（§5）③107 退役清单（§6）④校验/回滚（§7-8）。
- **必须你拍板的点**集中在 §10，多数关于「30 当前状态 / 运行时范围 / 107 退役时间线」。

---

## 1. 三机角色与任务

### 1.1 机器角色（你确认）
| 机器 | IP（推定） | 角色 | 硬件 | 去向 |
|---|---|---|---|---|
| **107** | 172.31.41.107（本机） | 当前主力，`agent_invest_lab` 在此，**数据最全**：含 live `oos_*`、全部回测 `fund_bot_*`、行情、研报 | 硬件不佳 | **迁移后废弃** |
| **30** | 172.31.41.30（主机名 `panyahui-...`） | **未来主力开发机**，承接 107 全部数据，成为新 active 主机 | 性能强劲 | 长期主力 |
| **68** | 172.31.41.1055 | 跑诸多其他服务，**磁盘大、稳定** | 稳定 | **纯数据备份机**（passive） |

### 1.2 目标拓扑
```
            一次性全量迁移                 每日 3 次单向备份
   ┌─────┐ ───────────────▶ ┌─────┐ ───────────────────▶ ┌─────┐
   │ 107 │  (fund.db 全量    │ 30  │  (09:20/14:00/20:00   │ 68  │
   │旧主力│   +运行时, 一次)  │新主力│   单向镜像, 持续)      │备份 │
   └─────┘                   └─────┘                       └─────┘
      │
      └──▶ 迁移验收通过后退役，退出拓扑
```

### 1.3 阶段划分
- **阶段一（一次性）**：`107 → 30` 全量迁移。一刀切式切换主力。
- **阶段二（稳态）**：`30 → 68` 单向定时备份。
- **阶段三（收尾）**：107 退役。

---

## 2. 现状研究结论（107 数据画像，作为迁移依据）

> 这些是迁移要完整搬走的「资产清单」。

### 2.1 库
- 107 `data/fund.db`：单文件 SQLite，**184MB**，`journal_mode=delete`（非 WAL），**50 张表**。
- 数据时点：`fund_nav`、`market_index_quote` 到 2026-06-26（最近交易日）。

### 2.2 数据五类（迁移要全搬）
| 类别 | 代表表（行数） | 来源 |
|---|---|---|
| 数据侧 | fund_nav(72万)、market_index_quote(18.8万)、fund_info(2870) 等 14 表 | 天天基金 API 刷新 |
| live 实盘 | oos_bot_daily_snapshots(532)、oos_bot_orders/actions… 7 表 | 107 每日 08:00 bot101/102 决策 |
| 报表 | market_reports(1483)、res_reports(412) | 107 prepass 生成 |
| 回测 | fund_bot_daily_snapshots(16万)、fund_bot_position_snapshots(10万)… 18 表 | 107 上手动跑的回测 |
| 用户/策略 | real_user_*、user_profiles、strategy_meta… 8 表 | 低频 |

### 2.3 运行时画像（30 接管主力需要的配套）
107 上让 fund.db「每天长出新数据」的那套东西：
- **systemd --user timers**：`oos-bot101-daily`(08:00)、`oos-nav-refresh/snapshot`(06:15/06:17)、`oos-bot101-nav`(06:42)、`oos-prepass-daily`(06:25)、`oos-market-reports-daily`(06:30)、`backfill-macro-news`(07:30)、`res-reports-daily`(09:00)、`refresh-calendar`、`cron-health-report`(08:30) 等。
- **脚本**：`scripts/run-oos-bot101-daily.sh`、`refresh-oos-*.sh`、`run-oos-market-reports-daily.sh` 等。
- **MCP 服务**：`ttjj-data-pit`(:18078)、`fund-portfolio`(readonly :28071 / bot-only :28172)。
- **world 运行时**：`world/runtime/`（calendar.json、runs/）、`world/config/*.yaml`。
- **代码**：git 仓库（30 上 panyahui 可能已有 `feat/panyh/agent_invest_lab` 分支副本）。

> **范围提示**：你的核心诉求是「数据迁移」。但 30 要当主力、107 要废弃 → **live 生产必须有机器继续跑**，否则没有新数据。所以 30 大概率需要「数据 + 运行时」一起到位。本方案把运行时迁移列为阶段一的一部分，但标注为**待你确认是否本期纳入**（§10-Q2）。

---

## 3. 设计模型：一次性整库迁移 + 单向备份

### 3.1 为什么新拓扑能甩掉旧方案的复杂度
旧方案（107↔68 双向）要处理：自增 `id` 撞键、行级合并冲突、双向 upsert 矩阵。新拓扑下：
- **107→30 一次性**：30 接收 107 的**整库**（直接 restore/覆盖），不是行级合并 → 无冲突。
- **30→68 单向**：68 只接收、不写业务数据 → **单向镜像，自增 id 照搬，永不撞键**。
- 结论：**不需要双向合并矩阵**。每张表都「以源为准、整体覆盖」即可，简单且强一致。

### 3.2 两条数据通道的原语
| 通道 | 方法 | 原语 |
|---|---|---|
| 107 → 30（一次性） | 一致快照整库迁移 | `sqlite3 .backup` → scp → 30 端原子替换 / `.restore` |
| 30 → 68（每日） | 单向整库镜像 | `sqlite3 .backup` → scp → 68 端原子替换；或表级 `INSERT OR REPLACE` 全表覆盖 |

> 两者都用 `.backup` 出**事务一致快照**，避免源库并发写时拷到撕裂页（delete 模式下尤其重要）。

---

## 4. 阶段一：107 → 30 全量迁移（详细 runbook）

### 4.1 前置确认（迁移前必须搞清）
1. **30 当前状态**（§10-Q1）：30 上是否已有 `agent_invest_lab` / `fund.db`？
   - 若 30 是干净环境 / 无需保留的数据 → **直接整库灌入**（最简单）。
   - 若 30 已有 panyahui 的独有数据要保留 → 需先界定保留范围，改为「选择性合并」。
2. **30 的目标路径**：确认 `fund.db` 落点（默认 `/home/rooot/agent_invest_lab/data/fund.db`）。
3. **磁盘空间**：30 上 ≥ 1GB 余量（184MB 库 + 留底 + 历史快照）。
4. **免密通道**：107→30 的 ssh 免密（当前实测 107 能 ssh 通 30）。
5. **运行时范围**（§10-Q2）：本次是否同时搬 systemd/MCP/world runtime，让 30 立刻能跑 live。

### 4.2 迁移方式：推荐「切换日一刀切」
> 因 107 每天还在跑 live、数据在变，迁移要选一个干净的「定格点」，避免迁了又变。

**切换日当天流程：**
1. **停 107 的 live 写入**：停 107 上 oos 全家桶 + sync timer（`systemctl --user stop oos-*.timer fund-sync-68.timer`），确认无 driver 进程在写（flock/进程检查）。
2. **107 出一致快照**：`sqlite3 data/fund.db ".backup /tmp/fund-migrate.db"`，校验快照可打开、表数=50、关键表行数符合预期。
3. **传输**：`scp /tmp/fund-migrate.db rooot@30:/tmp/`。
4. **30 端就位**：
   - 备份 30 现有库（若有）：`mv data/fund.db data/fund.db.before-migrate.bak`。
   - 落位：`mv /tmp/fund-migrate.db data/fund.db`（或 `.restore`）。
5. **校验**：在 30 上跑指纹（§7），与 107 快照逐表 hash 比对，必须全等。
6.（若纳入运行时）**搬运行时**：见 §4.4。
7. **30 接管**：在 30 上启用 live timer + MCP 服务，确认能正常跑当日管线。
8. **107 转入只读/停跑**：107 不再产新数据（进入退役准备）。

### 4.3 备选方式：「迁移 + 追增量」（若不能停 107）
若切换日不能让 107 停 live：
1. 先做一次全量迁移（107 不停，用 `.backup` 一致快照）。
2. 切换日再做一次「增量追平」：只补迁移点之后新增的行——
   - 带时间列的表（`oos_*` 按 `trade_date`、行情按 `nav_date`/`trade_date`）：`WHERE date > 迁移水位` 增量 upsert。
   - 自增 id 表：`WHERE id > 迁移时最大 id` 追加。
3. 最后做一次指纹校验确保追平。
> 比一刀切复杂，仅当 107 无法停机时采用。**推荐一刀切**。

### 4.4 运行时迁移（若本期纳入，§10-Q2 = 是）
- **代码**：30 用 git 拉同一仓库到对应分支（与 107 对齐 commit）。优先 git，不用 fund.db 通道传代码。
- **systemd units**：拷贝 `~/.config/systemd/user/*.{service,timer}`（oos 全家桶、sync、calendar、health），按需改路径/启用。
- **MCP 服务**：在 30 起 `ttjj-data-pit`、`fund-portfolio`（确认端口、Python 解释器 `/usr/bin/python3.12` 与 `/opt/MCP/.venv`、依赖 `import mcp` 自检）。
- **world runtime**：迁 `world/runtime/calendar.json` + `runs/`（或让 30 重新生成 calendar）。
- **凭证/配置**：openclaw.json、research-loop.yaml（含网关 key）、各 `model.yaml`。
- 注：把 `30→68` 备份的发起方也设在 30（接替 107 原来的 `fund-sync-68` 角色）。

---

## 5. 阶段二：30 → 68 每日单向备份（详细）

### 5.1 引擎（沿用 107 现有 sync 的可靠骨架，简化为单向）
**由 30 发起**，每次：
```
1. flock 单实例锁
2. 30 .backup 出一致快照 S.db                    ← 一致性关键
3. 30 留底：上一份备份滚动保存
4. scp S.db → 68:/tmp/
5. 68 端：备份现有 → 原子替换 fund.db（或表级全表覆盖）
6. 指纹校验：30 与 68 逐表 hash 必须全等
7. 记录行数/最新日期/hash/耗时 → 日志 + 告警
```
- **为什么可整库替换**：68 是纯备份机，不在其上跑会写 `fund.db` 的业务（68 跑的「别的服务」用的不是这个库）。整库替换最简单、强一致。**前提需确认**（§10-Q3）：68 上无任何进程写这个 `fund.db`。
- 若将来 68 上也需保留独有数据 → 退回「表级单向覆盖」（每表 `DELETE`+`INSERT`，以 30 为准）。

### 5.2 历史快照（备份机的价值在这）
- 68 磁盘大 → **保留多份带日期的历史快照**：`backups/fund-YYYYMMDD.db`，保留 N 天（如 30 天）。
- 防「30 上误操作/损坏被当天备份扩散」——可从任意历史日 restore。
- 这是 68 作为「稳定大磁盘备份机」的核心增值。

### 5.3 调度
- 新建 `fund-db-backup-to-68.timer`（user systemd），**09:20 / 14:00 / 20:00**（首点 09:20 错开 30 上 live 管线的早间写入；若 30 的 live 管线时刻与 107 不同，按 30 实际调）。
- `flock` 防叠跑；发起方唯一（30）。

---

## 6. 阶段三：107 退役清单

迁移验收通过后：
1. 确认 30 已稳定接管 live ≥ 3 个交易日、数据正常增长、30→68 备份正常。
2. 107 上停所有 timer、停 MCP 服务。
3. 107 `fund.db` 留一份冷备（拷到 68 的 `backups/107-final-YYYYMMDD.db`）后即可下线。
4. 清理 107→68 的旧 `fund-sync-68`（已被 30→68 取代）。
5. 更新文档（CLAUDE.md、本方案）中「107 是主力」的表述为「30 主力」。

---

## 7. 一致性校验与监控

### 7.1 校验方法（本次调研已验证可用）
对每表取 `md5(按主键排序的行[,关键值])`，比 `(行数, hash)`。
- 迁移后：30 vs 107 快照，全表 hash 必须全等。
- 每日备份后：30 vs 68，全表 hash 必须全等（单向镜像 → 应严格相等）。

### 7.2 监控/告警
- 接入现有 `cron-health-report` 体系：新增「30→68 三次备份成功 + 末次一致性」一行。
- scp/ssh 失败、hash 不一致、schema 漂移 → 显式告警，不静默。
- 独立 `verify-fund-db-parity.sh` 每日巡检 30↔68 全表指纹。

---

## 8. 灾难恢复与回滚

- **迁移期**：30 落位前先备份 30 现有库（`*.before-migrate.bak`）；107 快照保留至验收通过。
- **备份期**：每次替换 68 前留底；68 上多日历史快照（§5.2）。
- **回滚**：迁移失败 → 30 恢复 `before-migrate.bak`，107 继续当主力（未停机损失）。
- **30 库损坏**：从 68 最近备份或历史快照 `.restore`。
- **彻底丢失**：68 的历史快照是最后防线。

---

## 9. 边界条件与风险（新拓扑下已大幅收敛）

| 风险 | 状态 | 处理 |
|---|---|---|
| 自增 `id` 撞键 | **已消除** | 全程单向，id 照搬源机 |
| 双向合并冲突 | **已消除** | 无双向 |
| 拷到撕裂页 | 受控 | 全程 `.backup` 一致快照，不裸拷文件 |
| 迁移期 107 仍在写 | 受控 | 一刀切停写 / 或 §4.3 追增量 |
| 30 已有需保留数据 | **待确认** | §10-Q1；若有则改选择性合并 |
| 68 上有进程写同名 fund.db | **待确认** | §10-Q3；若有则用表级覆盖而非整库替换 |
| 库增大传输成本 | 可忽略 | 184MB 局域网秒级；增大后可改增量 |
| `journal_mode=delete` | 不影响 | `.backup` 不要求 WAL |

---

## 10. 待你拍板的关键点（评审清单）

- **Q1｜30 当前状态**：30 上现在有没有 `agent_invest_lab` / `fund.db`？里面有没有**需要保留**的独有数据（panyahui 跑的回测等）？
  - 无需保留 → 直接整库灌入（推荐，最简单）。
  - 有需保留 → 我改成「选择性合并」并列出保留表。
- **Q2｜本期范围**：阶段一是否**同时迁运行时**（systemd timers / MCP 服务 / world runtime / 凭证），让 30 立刻能接管 live 生产？还是**先只迁 fund.db 数据**，运行时另行处理？
  - （提示：107 要废弃，live 必须有机器继续跑，否则无新数据——逻辑上运行时迟早要搬到 30。）
- **Q3｜68 备份落位**：68 上这个 `fund.db` 是否纯备份、**没有任何进程会写它**？
  - 是 → 30→68 用整库替换（最简单强一致）。
  - 否 → 改表级单向覆盖。
- **Q4｜迁移时机**：切换日能否让 107 短暂停 live（几分钟）做「一刀切」迁移？还是必须不停机（走 §4.3 追增量）？
- **Q5｜30 的 live 管线时刻**：30 接管后，每日 live 管线跑在什么时间？（决定 30→68 备份首点错峰，默认 09:20。）
- **Q6｜旧表清理**：`allocation_run / bot_actions / bot_reviews`（疑似历史遗留）是否仍用？可否迁移时一并归档瘦身？

---

## 11. 分阶段落地路线图（暂不执行，给出顺序）

1. **评审**：确认 §10 Q1–Q6。
2. **30 就位准备**：确认路径、磁盘、免密、（若纳入）代码 git 对齐。
3. **迁移演练**：先做一次「107 → 30」全量迁移到 30 的**临时库**（不替换正用库），指纹校验，确认流程无误。
4. **切换日迁移**：按 §4.2 一刀切（或 §4.3 追增量），30 接管，指纹验收。
5. **建 30→68 备份**：脚本 + `fund-db-backup-to-68.timer`（09:20/14:00/20:00）+ 历史快照 + 校验。
6. **观察期**：30 主力 + 30→68 备份稳定运行 ≥ 3 交易日。
7. **107 退役**：按 §6 清单。
8. **校验常态化**：`verify-fund-db-parity.sh` 每日巡检 + 接入 cron-health-report。

---

## 附录 A：50 张表完整清单（迁移资产清单，按类）
- **数据侧(14)**：fund_info, fund_nav, fund_nav_performance, fund_performance, fund_style, fund_top_stocks, fund_industry, fund_capability_circle, market_index_quote, market_index_constituents, fund_pool, fund_index_quote(空), index_constituents(空), market_data_cache(空)
- **live(7)**：oos_bot_accounts, oos_bot_daily_snapshots, oos_bot_position_snapshots, oos_bot_orders, oos_bot_actions, oos_bot_reviews, oos_market_report_status
- **报表(2)**：market_reports, res_reports
- **回测(18)**：fund_bot_accounts, fund_bot_daily_snapshots, fund_bot_position_snapshots, fund_bot_performance, fund_bot_reviews, fund_bot_actions, fund_bot_orders, fund_bot_holdings, fund_bot_nav(空), fund_bot_positions(空), fund_bot_skill_runs(空), fund_allocation_runs, fund_selection_runs, fund_system_runs, fund_paradigm_runs, allocation_run, bot_actions, bot_reviews
- **用户(6)**：real_user_cycles, real_user_curves, real_user_txns(无PK), user_profiles, user_holdings, user_capability_snapshots
- **策略(2)**：strategy_meta, strategy_meta_history(空)

## 附录 B：迁移须知的特殊表
- **无主键**：real_user_txns（迁移整表搬即可；新拓扑单向，无原 sync 的翻倍问题）。
- **空表(7)**：fund_bot_nav/positions/skill_runs、fund_index_quote、index_constituents、market_data_cache、strategy_meta_history —— 迁移建表对齐即可。
- **自增 id 表**：新拓扑单向，id 照搬源机，无需特殊处理（旧双向方案的雷区已不适用）。

## 附录 C：实测命令留痕（可复现）
- 表盘点 / 数据画像：`/tmp/survey.py`、`/tmp/fresh.py`。
- 指纹对比：`/tmp/fp.py`（对每表 `SELECT 主键[,关键值] ORDER BY 主键` 取 md5，比 `(行数,hash)`）。

---

## 变更记录
- v2（本版）：拓扑由「107↔68 双向同步」改为「107→30 一次性迁移 + 30→68 单向备份 + 107 退役」。简化冲突模型（全程单向）。新增三机角色、迁移 runbook、退役清单。
- v1：107 主 / 68 备双向定时同步方案（已废弃，因机器格局变化）。
