<!-- TOOLS_COMMON:START -->

---

## System Admin — Strictly Forbidden

**Only HQ (mag1) may execute these. All sub-bots are prohibited:**

- `openclaw gateway restart/stop/start`, `kill/pkill/killall`, `systemctl/service`
- `rm -rf`, `trash` on system directories or other bots' files

**Infrastructure issues (timeout, connection failure) → report to HQ, do not troubleshoot yourself.**

---

## Inter-Agent Communication (Message Bus)

### Rules

1. **Only channel**: `send_message` / `reply_message` / `forward_message` — no CLI calls, legacy `message()`, or shell scripts
2. **Every message must include `trace`** (provenance chain); `reply_message` auto-routes based on trace
3. **Strict single-round**: request → process → `reply_message` → **done**. One request = one reply. Put all data in the reply — never split into multiple messages

### Tools

| Tool | Purpose |
|------|---------|
| `send_message` | Start a new conversation/request |
| `reply_message` | Return results (defaults to Feishu user; add `also_notify_agent: true` to also wake upstream agent) |
| `forward_message` | Forward to another agent (trace auto-appended) |
| `get_message` / `list_messages` | Query message details / inbox |

### Trace Construction

```
send_message(to: "target_agent", content: "...", trace: [{
  agent: "your_account_id", session_id: "current_session_id",
  reply_channel: "feishu", reply_to: "ou_xxx", reply_account: "your_account_id"
}])
```

`reply_channel/reply_to/reply_account`: only set at the origin hop. Intermediate forwards auto-append trace.

### Incoming `[MSG:xxx]` Messages

`xxx` is the message_id → process → call `reply_message(message_id: "xxx", content: "all results here")` → done.

**Never use `[[reply_to_current]]` or plain text replies** — the sender won't receive them. Always `reply_message`, whether success or failure.

---

## Image Generation: image-gen-mcp

生图用 `image-gen-mcp.generate_image(style, content)`。模型可选 `banana`（默认）或 `banana2`。

```
npx mcporter call 'image-gen-mcp.generate_image(style: "扁平插画风", content: "一只猫在看股票K线图")'
```

---

## Memory Recall: mem0_search

语义记忆检索——跨历史会话、日记、发帖、研究报告做语义搜索。当你需要回忆"我之前对 X 说过/想过/做过什么"时调用，替代手动 grep 文件。

| 参数 | 说明 |
|------|------|
| `query` | 自然语言检索词 |
| `scope` | `self`（默认，仅查自己的记忆）/ `all`（跨 bot 查询） |

```
mem0_search(query: "黄金ETF写过哪些角度", scope: "self")
```

典型场景：发文前查重、承接上篇话题、回忆过往研究结论、避免重复踩坑。

---

## Tool Priority

1. **memory** → check history first, update incrementally
2. **simworld-data**（ttjj 研究数据）→ financial data
3. **browser** → Xueqiu, EastMoney research reports, etc.
4. **MCP search** → supplementary search, overseas data
5. **xiaohongshu-mcp** → note management, interactions
6. **message bus** → inter-agent communication
<!-- TOOLS_COMMON:END -->










# TOOLS.md - bot105d（高风险多指数权益基金配置 bot）工具配置


---

## Bot 专属配置

- **account_id**: `bot105d`
- **小红书 MCP**: 单进程多租户，所有 bot 共用 `:18060`，URL path 自动识别身份（已配置在 mcporter.json）

## [临时覆盖] 合规审核暂停

**覆盖 TOOLS_COMMON.md 中的合规审核要求：** bot105d 当前无需调用 `compliance-mcp.review_content` 或 `compliance-mcp.review_comment`。发布前由研究部人工确认即可，不需要走 compliance-mcp 审核流程。

> 恢复时删除本节即可。

---

## 基金组合管理: fund-portfolio-mcp

当前配置加载的是 `fund-portfolio-mcp`，不是旧产品池工具。多指数权益组合执行时优先使用下面这些工具：

| 工具 | 功能 |
|------|------|
| `portfolio_get_buyable_funds` | 查询本轮 run 实际允许交易的基金代码池 |
| `get_fund_detail` | 查询单只基金主题、风格因子、规模、长期业绩、回撤、Sharpe 和同类排名 |
| `portfolio_get_my_history` | 查询当前账户、持仓、订单和历史动作 |
| `portfolio_get_my_trades` | 查询交易记录 |
| `portfolio_get_my_performance` | 查询账户绩效和回撤序列 |
| `portfolio_place_buy_order` | 下买入订单，必须写清 `reason` |
| `portfolio_place_sell_order` | 下卖出订单，必须写清 `reason` |

## 数据与研究

- `simworld-data`：市场状态、指数行情、股债性价比、估值、情绪、宏观、板块因子、资金流、研报搜索。
- `strategy-mcp`：只作为策略辅助和复核，不替代 `METHODOLOGY.md` 的主流程。

## 执行原则

- 每日先读 prompt 注入的可买池主题分布，再按方法论收敛候选。
- 不用 `get_fund_detail` 扫全池，只对最终 1-3 只候选拉全量画像。
- 每笔交易都必须留下可审计理由，理由要包含风险状态、主线判断、仓位结构变化。

---

## 联网搜索

- 联网搜索通过 browser 工具访问搜索引擎或目标站点（使用前先读 `skills/browser-base/SKILL.md`）
