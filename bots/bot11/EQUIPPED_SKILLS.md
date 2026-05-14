# 已装备技能（agent_invest_lab 基金链路精简版）

> bot11 在 lab 里只跑基金投资全流程，不做 xhs / 公众号 / 短线策略（S2/S3/S4/S5）/ 行业研究。所有非基金 skill 已卸载。

## 基金链路必读 skill（按 Phase 顺序）

| Phase | Skill | 位置 |
|---|---|---|
| B0 — 市场环境判断 | `market-context` | `bots/bot11/skills/market-context/SKILL.md` 或 `skills/portfolio/market-context/SKILL.md` |
| B   — 基金筛选与组合构建 | `fund-match` | `skills/portfolio/fund-match/SKILL.md` |
| C   — 基金巡检与调仓 | `skills/portfolio/fund-review/SKILL.md` |

## 数据工具参考

- `research-mcp`（参考文档，lab 数据走 `ttjj_data_pit_mcp`） — `bots/bot11/skills/research-mcp/SKILL.md`

## 共享文档

- 基金投资完整链路 — `skills/portfolio/_shared_docs/fund-investment/基金投资完整链路.md`
- 5 份 MD 的 frontmatter schema — `skills/portfolio/_shared_docs/fund-investment/基金MD-frontmatter-schema.md`
- 基金核心池 — `skills/portfolio/_shared_docs/fund-investment/基金核心池.xlsx`

## 工具调用规范

- 数据查询走 `ttjj_data_pit_mcp` (:18078)，必传 `simulated_today`
- 持仓/订单/快照/范式 run 走 `fund-portfolio-mcp` readonly (:28071)
- 写 MD 走 `memory/portfolio/fund/` 五份文件，schema 由 `get_fund_md_schema` 给
- bot 不调任何 `save_*` / `apply_*` / `init_*` / `record_*` 工具——落库是 lab 系统侧的事
