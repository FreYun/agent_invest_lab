# 八种战法 S1-S8

> **⚠️ 2026-06-02 按结构择时改版**：`regime_rules.py` 的 `PLAYBOOKS.recommended` 已按深史回测(2014-2026)+ 扣成本 + IS/OOS 验证重写。结论见 [`research/s1-s8_final_report_2026-06-02.md`](../../../../agent_invest_lab/research/s1-s8_final_report_2026-06-02.md)。
> - **保留(对路环境真有 edge)**：**S2 龙头**(强牛/强势震荡，出场必须封板持有)、**S7 超跌反弹**(熊/弱势震荡，已补软门)、**S3 首板**(强势震荡，仅 screen)。
> - **移除(任何手段救不活)**：S1(时代产物 OOS 死)、S4(每个环境都亏)、S5(彩票，中位-6.6%)、S6(仅 2020 核心资产牛)。已从所有 `recommended` 移除，但 select.py 仍生成候选供研究/复盘。
> - 下方各战法的"适用环境"是**原始设计描述**，已部分被验证推翻；以 `PLAYBOOKS` 为准。

market-regime 不执行战法本身，只产出 `regime_code` 和 playbook 推荐。具体进场/止损/出场规则由下游 boots skill 和 `/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s*/` 实现。

当前已落地的下游执行器：

| 战法 | Skill | 代码 | 调度 |
|---|---|---|---|
| S1 | `workspace/skills/boots/s1-breakout/` | `scripts/s1/` | `scripts/s1s2s3s4-daily-cron.sh` |
| S2 | `workspace/skills/boots/s2-leader-relay/` | `scripts/s2/` | `scripts/s1s2s3s4-daily-cron.sh` |
| S3 | `workspace/skills/boots/s3-first-board-relay/` | `scripts/s3/` | `scripts/s1s2s3s4-daily-cron.sh` |
| S4 | `workspace/skills/boots/s4-pullback/` | `scripts/s4/` | `scripts/s1s2s3s4-daily-cron.sh` |
| S5 | `workspace/skills/boots/s5-dragon-pullback/` | `scripts/s5/` | `scripts/s5-daily-cron.sh` |

注意：当前 S2/S3/S4/S5 的 `select.py` 会把 regime 判断写入 `regime_gate_*` 字段；即使 gate 关闭，脚本也可能继续生成候选供研究/复盘。实盘执行必须以 `regime_gate_allowed = 1` 或 skill 纪律为准。

---

## S1 突破战法

**一句话**：强势股横盘平台放量突破，进场买突破位。

**适用环境**：强牛（趋势清晰）。震荡市箱体顶放量往往是出货信号，假突破远多于真突破 —— 震荡档必须禁用。

**下游 skill**：暂无。

---

## S2 龙头板接力

**一句话**：盯高度板 (3 板+) 梯队龙头，次日竞价接力。

**适用环境**：强牛（情绪高涨 + 连板高度起）。

**下游 skill**：`s2-leader-relay` 已落地，代码在 `scripts/s2/`。

---

## S3 首板接力

**一句话**：当日领涨板块首板，次日竞价低开进。

**适用环境**：强势震荡（有板块轮动但非全面上涨）。

**下游 skill**：`s3-first-board-relay` 已落地，代码在 `scripts/s3/`。

---

## S4 回踩战法

**一句话**：强势股回踩 5/10 日线不破，次日进。

**适用环境**：强势/中性震荡。中性档建议 `strict` 模式：只选最强的标的回踩。

**下游 skill**：`s4-pullback` 已落地，代码在 `scripts/s4/`。

---

## S5 龙回头

**一句话**：前期龙头冷却后二次放量反包。

**适用环境**：所有震荡档（强势/中性/弱势）。弱势震荡仅建议试错仓位。

**下游 skill**：`s5-dragon-pullback` 已落地，代码在 `scripts/s5/`。

---

## S6 趋势持有

**一句话**：指数强势 + 龙头股持仓 5-10 天。

**适用环境**：强牛。震荡/熊档禁用 —— 趋势持有在震荡市会被反复打脸。

**下游 skill**：暂无。

---

## S7 超跌反弹

**一句话**：连续大跌后的技术性反弹，一日游。

**适用环境**：熊市优先，但不作为生产硬闸门；regime 只给 agent 做风险判断。

**下游 skill**：`s7-oversold-rebound`，生产代码 `/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s7/`，写入 `market.db` 的 `s7_*` 表。

---

## S8 空仓观望

**一句话**：不开新仓，只处理已有持仓。

**适用环境**：熊市。

**下游 skill**：不需要 —— 空仓就是空仓。

---

## 备注

- 战法 ID（S1–S8）和顺序是 **下游契约**，不要改名或重新编号。下游 strategy skill 的 `playbook.recommended` 字段按 ID 匹配战法。
- 优先级 (`priority`) 在每个 regime 的映射中独立决定，见 [mapping.md](mapping.md)。
- `mode` 字段可选，用于给特定 regime 下的战法加限制（如 `strict` / `试错` / `1-2 成仓一日游`）。
