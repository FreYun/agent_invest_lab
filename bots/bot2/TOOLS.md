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
2. **research-mcp** → financial data
3. **browser** → Xueqiu, EastMoney research reports, etc.
4. **MCP search** → supplementary search, overseas data
5. **xiaohongshu-mcp** → note management, interactions
6. **message bus** → inter-agent communication
<!-- TOOLS_COMMON:END -->










# TOOLS.md - bot2 工具配置


---

## Bot 专属配置

- **account_id**: `bot2`
- **小红书 MCP**: 单进程多租户，所有 bot 共用 `:18060`，URL path 自动识别身份（已配置在 mcporter.json）

---

## 工具使用

**完整工具优先级和使用方式见 `memory/research/产业链研究流程.md` 的"工具优先级速查"章节。**

Browser 工具必须传 `profile: "bot2"`，不要省略。

---

## TMT 专业信息源

### 半导体

| 站点 | URL | 关注内容 |
|------|-----|---------|
| 集微网 | jiwei.com | 芯片产业链新闻、国产替代进度、晶圆厂扩产 |
| 芯智讯 | icsmart.cn | 半导体行业深度分析、设备材料动态 |
| 半导体行业观察 | semiinsights.com | 行业趋势、技术路线、制程演进 |
| DIGITIMES | digitimes.com | 亚太半导体供应链数据、产能追踪 |

### AI/算力

| 站点 | URL | 关注内容 |
|------|-----|---------|
| 量子位 | qbitai.com | AI 芯片、大模型、算力基础设施 |
| 机器之心 | jiqizhixin.com | AI 技术趋势、行业应用 |
| The Information | theinformation.com | 硅谷大厂 AI 战略、算力采购 |

### 消费电子 & 通信

| 站点 | URL | 关注内容 |
|------|-----|---------|
| 电子工程专辑 | eet-china.com | 消费电子零部件、新品拆解 |
| C114 通信网 | c114.com.cn | 5G/光通信/运营商资本开支 |
| 36氪 | 36kr.com | TMT 公司动态、产业趋势 |

### 综合（雪球/东方财富优先）

| 站点 | URL | 关注内容 |
|------|-----|---------|
| 雪球 | xueqiu.com | TMT 板块讨论、机构观点、市场情绪 |
| 东方财富 | eastmoney.com | 行业研报、板块资金流向 |
| 同花顺 | 10jqka.com.cn | TMT 板块新闻、概念股梳理 |

---

## 内容制作

- 产业链图/技术路线图 → 大模型生图，prompt 规范见 `CONTENT_STYLE.md`
- 结构化数据（竞争格局/财务对比/国产替代进度） → `text_to_image` 文字卡片
- 发帖 → `skills/submit-to-publisher/SKILL.md`
