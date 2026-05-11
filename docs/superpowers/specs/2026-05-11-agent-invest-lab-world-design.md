# agent_invest_lab —— 日度滚动的金融世界系统（设计）

- **日期**：2026-05-11
- **状态**：已批准，待写实现计划
- **范围**：只设计/实现「日度滚动的外部世界系统」。交易引擎由他人写代码；行情数据由他人准备；本系统通过约定的文件契约接入它们。

---

## 1. 背景与目标

我们要一个**金融世界沙盘**：它按交易日**日度滚动**，回放一段历史行情；每个交易日，把当天的行情数据「注入」给一批 agent，并「注入」一段告诉 agent「今天该怎么交易」的指令；agent 在这一天里用交易系统下单；当天所有 agent 都跑完后，世界推进到下一个交易日。

- **世界时钟模式**：回放历史日（backtest）。世界跑在历史时间轴上，可以一天接一天连续快速推进，不等真实时间。一次实验 = 回放区间内的 N 个交易日。
- **agent 大脑**：`/home/rooot/.openclaw/research-loop-ts`，以 JSON-RPC server 模式运行（`server.ts --bot-id botN --workspace <dir> --config <rl-config>`，stdin/stdout line-delimited JSON-RPC，方法 `ping` / `chat` / `shutdown`）。**本系统不修改 research-loop-ts 的代码**，只通过 `chat` 调用和一份定制的 `research-loop.json`（下称 `trading-rl-config.json`）与它交互。
- **agent 人设/性格**：`/home/rooot/.openclaw/workspace-botN`（N = 1..19，或其子集）。本系统**只读**这些目录，并把它们「精选复制」成每个 run 专属的影子 workspace，回测不污染真实 workspace。
- **本系统不做**：撮合/成交、行情数据生成、持仓与盈亏记账、agent 的记忆内容管理、agent 的投资风格定义。这些分别由交易引擎、数据准备、交易系统的工具、关键词记忆服务、影子 workspace 里的人设文件承担。

### 非目标（YAGNI）

- 不做 live 模式（1 世界日 = 1 真实交易日、systemd 定时器驱动）。作为未来演进路径在 §10 提一句，本期不实现。
- 不做交易撮合 / 风控 / 清算逻辑。
- 不做向量检索 / LLM 记忆抽取 / 跨 run 的长期记忆。

---

## 2. 架构与职责边界

```
agent_invest_lab/                          ← 本仓库（TypeScript，与 research-loop-ts 同栈：Node >= 22.6 + tsx）
  ├── world (CLI)                          ← 长驻进程：管时钟 + 摆数据 + 拼当日消息 + 叫 agent + 等齐
  ├── src/
  │   ├── ...（world 主循环、配置、影子 workspace、bot 进程管理、消息模板、落盘、resume）
  │   └── memory-server/                   ← 极简关键词记忆服务（mem0-compatible 子集 HTTP API）
  ├── docs/superpowers/specs/...
  └── world/                               ← 运行时状态（git 忽略）

外部依赖（不在本仓库，本系统只约定接口）：
  ├── 行情数据      → 数据团队往  world/days/<YYYY-MM-DD>/  放文件
  ├── 交易引擎      → 他人写的 MCP server；读  world/state.json  的 current_date 知道「今天」；
  │                   挂在 trading-rl-config.json 的 mcp.servers 里
  ├── agent 大脑    → /home/rooot/.openclaw/research-loop-ts （零改动，JSON-RPC server 模式）
  └── agent 人设    → /home/rooot/.openclaw/workspace-botN   （只读，精选复制成影子 workspace）
```

**一句话**：world 只负责「日度滚动 + 把当天的料喂给 agent + 等所有 agent 跑完再翻篇」。它对「交易实际发生了什么」只从交易系统/agent 落盘的产物里读，不做强校验——单 agent 当天没动作 = 视为不交易。

---

## 3. 目录 / 状态契约

数据团队、交易引擎团队、调试者都照这个布局接入。

```
agent_invest_lab/world/
  config/
    world.yaml                  ← 世界配置（见 §7）：回放区间、交易日历来源、bot 列表、并发度、单 bot 单日超时、
                                   research-loop-ts 路径、生成 trading-rl-config 的模板/覆盖项
    trading-rl-config.base.json ← research-loop.json 的基底；world 在每个 run 启动时复制一份并填入
                                   该 run 的 memory 服务 URL，生成  world/runs/<run_id>/trading-rl-config.json
  calendar.json                 ← 交易日历：交易日列表。本期 world 只消费它、不生成它（由数据团队或人工提供）
  state.json                    ← 当前世界状态（见下）；交易引擎 & 记忆服务从这里读 current_date
  days/<YYYY-MM-DD>/            ← 【数据团队写】当日全量行情快照
    quotes.json                 ← 标的报价 / OHLCV / 成交量等；内部 schema 由数据团队定，world 不解析其内部结构
    overview.md                 ← 可选：数据团队预写的当日精简概览（指数涨跌、板块表现、要点）
    events.json                 ← 可选：当日公告 / 宏观事件
  runs/<run_id>/
    trading-rl-config.json      ← 本 run 生成的 rl-config（填了 memory 服务 URL）
    workspaces/botN/            ← 本 run 的影子 workspace（§5）
    memory/
      store.jsonl               ← 本 run 的关键词记忆库（所有 bot 共一个文件，按 agent_id 区分；§6）
      runtime.json              ← 本 run 记忆服务实际分配的端口
    <YYYY-MM-DD>/botN/
      sent.md                   ← 当天发给 botN 的完整 chat message（存档）
      reply.json                ← chat 返回（reply / tool_trace / iterations / usage / truncated_by_iterations）
      status.json               ← { status: ok|error|timeout, started_at, finished_at, error? }
    run.log                     ← 全程时间线日志
    summary.json                ← 全程汇总：每交易日 × 每 bot 的状态、耗时、迭代数
```

### `state.json` 结构

```jsonc
{
  "run_id": "2024H1-baseline",
  "status": "running",            // setup | running | done | failed | aborted
  "current_date": "2024-03-15",   // 当前世界日期；交易引擎 & 记忆服务读它
  "trading_dates": ["2024-01-02", ...],  // 本 run 的完整交易日序列
  "cursor": 51,                   // 已完成到 trading_dates[cursor-1]，下一步处理 trading_dates[cursor]
  "bots": ["bot1", "bot7", ...],
  "memory_port": 41873,
  "started_at": "2026-05-11T10:00:00Z",
  "updated_at": "2026-05-11T10:42:00Z"
}
```

### 给外部团队的约定

- **数据团队**：回放区间内每个交易日，必须在 `world/days/<date>/quotes.json` 放好当天数据。world 在每个 run 开跑前会校验区间内每个交易日的 `quotes.json` 都存在；缺任何一个都 **fail-fast**，不开跑。`overview.md` / `events.json` 可选。
- **交易引擎团队**：你的 MCP server 通过读 `world/state.json` 的 `current_date` 来决定「今天能按什么价成交」。持仓 / 订单存哪由你定（建议放 `world/runs/<run_id>/portfolios/` 下，但不强制）。world 保证每天先确认所有 bot 当天 `chat` 都返回（或超时）后，才更新 `state.json.current_date` —— 给你一个明确、单调递增的「日切点」。你的 MCP server 由 world 按 `trading-rl-config.base.json` 里的 `mcp.servers` 配置启动，或你自己常驻让 world 连——这条在写实现计划时与交易引擎团队对齐（默认假设：MCP server 由你自己常驻，world 只在 rl-config 里引用它的地址）。

---

## 4. 日度滚动主循环

CLI 入口（命令名与精确参数在实现计划里定稿，这里给意图）：

```
world run    --config world/config/world.yaml --run-id <id>   # 开新 run
world resume --run-id <id>                                     # 从 state.json.cursor 续跑
world status --run-id <id>                                     # 查进度
world stop   --run-id <id>                                     # 优雅终止（写 status=aborted）
```

### `world run` 流程

```
1. 加载 world.yaml + calendar.json，按回放区间算出交易日序列 trading_dates = [d0, d1, ..., dN]

2. 启动阶段（一次性，任何一步失败即 fail-fast，不进循环）：
   a. 校验 trading_dates 里每个 di 的 world/days/<di>/quotes.json 都存在
   b. 起本 run 的关键词记忆服务（动态空闲端口）；写 runs/<run_id>/memory/runtime.json
   c. 复制 trading-rl-config.base.json → runs/<run_id>/trading-rl-config.json，把 mcp.mem0 设为记忆服务 URL
   d. 为每个 bot 建影子 workspace（§5）
   e. 为每个 bot 起一个 research-loop-ts JSON-RPC server：
        server.ts --bot-id botN --workspace runs/<run_id>/workspaces/botN \
                  --config runs/<run_id>/trading-rl-config.json
      等每个 server 的 server.ready 通知
   f. 写 state.json: status=running, current_date=d0, cursor=0, bots=[...], memory_port=...

3. for di in trading_dates:
   a. state.json.current_date = di; 写盘    ← 交易引擎 & 记忆服务从这里读「今天」
   b. 为每个 bot 渲染当天 chat message（§4-msg）
   c. 并发（受 world.yaml.concurrency 限流）：对每个 bot 的 server 发
        chat(message=<当天消息>, session_key="trading-<run_id>", history=[])
      —— 每天 history 传空：每个世界日 = 干净 session（§5），跨日连续性靠 journal.md + 关键词记忆服务
   d. barrier：等所有 bot 的 chat 返回或超时（world.yaml.per_bot_timeout）。
      逐 bot 落盘 sent.md / reply.json / status.json；单 bot 失败/超时 → status=error|timeout，
      不阻塞当天其它 bot，也不阻塞推进到下一天（注：agent 当天没动作 = 视为不交易）
   e. cursor++; 写 state.json; 追加 run.log

4. 收尾：
   - 关掉所有 research-loop-ts server（shutdown 方法 + 超时强杀）
   - 关掉本 run 的记忆服务（store.jsonl 保留）
   - status=done; 写 summary.json
```

### 失败 / 中断 / 续跑

- 启动阶段失败：清理已起的子进程，`state.json.status=failed`，退出非零。
- 跑中收到 SIGINT / `world stop`：当前正在等的这天 barrier 走完（或强制超时），写 `status=aborted`，关子进程，退出。
- `world resume --run-id <id>`：读 `state.json`，从 `cursor` 重新走启动阶段 b–e（记忆服务重启但 `store.jsonl` 续用；影子 workspace 已存在则复用），然后从 `trading_dates[cursor]` 继续主循环。

### <a name="4-msg"></a>当天 chat message 模板（核心交付物）

按已确认的决定：「告诉 agent 怎么交易的 sys prompt」放在每日 `chat` 的 `message`（user 消息）里——不改 research-loop-ts，不动它的 system prompt（人设/方法论由影子 workspace 的 SOUL.md/IDENTITY.md/AGENTS.md/METHODOLOGY.md 经 research-loop-ts 的 `buildSystemPrompt` 注入，照旧生效）。

`message` = **世界规则块** + **当日数据概览块**。对所有 bot 内容一样（不含人设/风格——那来自影子 workspace）。

**世界规则块**（首个交易日发完整版；之后每天发精简版 + 「规则同前」提示，以利上游 prefix 缓存）：

```
你现在身处一个金融模拟世界。当前世界日期：{date}（{weekday}）。
这是一个回放历史行情的沙盘——你只能看到 {date} 当天及之前的信息，不存在「未来数据」。

【你的任务】根据今天的行情，按你自己的投资风格做出今天的交易决策并执行。

【交易系统】交易引擎以工具形式提供（查持仓、查可用资金、下单、查成交、撤单等）。
不要凭记忆猜工具名——先用 discover_tools 看清楚有哪些交易工具，再调用。
你的持仓、现金、累计盈亏都从交易系统的工具里查；本消息不会替你列出来。

【行情数据】今天的全量行情在文件 {world/days/<date>/quotes.json}（绝对路径会给全），需要细节就读它。
下面是当天精简概览：
{overview}

【记忆与连续性】每个世界日是独立会话，你不会自动记得昨天。
- 决策前：读你的交易日志 memory/trading/journal.md；用 mem0_search 调取相关的历史交易记忆。
- 决策后：把今天的判断、操作、理由追加到 memory/trading/journal.md；把关键结论用 mem0_add 存进记忆。

【边界】这是一次交易回合，不是一个研究项目——可以快速查证，但不要开 start_research 大坑。
今天结束前，确保该下的单都下了、journal 写了。
```

**当日数据概览块**（上面 `{overview}` 那段）：优先用 `world/days/<date>/overview.md`（数据团队预写）；没有就 world 从 `quotes.json` 跑一个简单 summarizer 生成（主要指数涨跌幅 + 若干大类板块涨跌幅 + `events.json` 摘要）。summarizer 只需对 `quotes.json` 的顶层字段做轻度提取，对其内部 schema 的依赖要尽量小、并在 schema 不符合预期时降级为「概览不可用，请直接读 quotes.json」。

模板里所有路径以**绝对路径**给出（影子 workspace 路径、quotes.json 路径），避免 agent 猜。

---

## 5. bot 进程 / 影子 workspace / 会话

- **bot 进程**：world 在 run 启动时为每个参与 bot 起一个 research-loop-ts JSON-RPC server，整个 run 期间常驻；run 结束统一 `shutdown`（带超时强杀）。world 通过它的 stdin/stdout 收发 JSON-RPC。每个 server 用该 run 生成的 `trading-rl-config.json` 和该 bot 的影子 workspace。
- **影子 workspace**：`world/runs/<run_id>/workspaces/botN/`，从 `/home/rooot/.openclaw/workspace-botN` **精选复制**：
  - 复制：人设/方法论/技能类文件与目录——`IDENTITY.md`、`SOUL.md`、`AGENTS.md`、`USER.md`、`METHODOLOGY.md`、`RESEARCH.md`、`MEMORY.md`、`EQUIPPED_SKILLS.md`、`skills/`、`config/`、以及其它小体积的人设相关文本（具体清单在实现计划里定稿，原则：够 research-loop-ts 的 `buildSystemPrompt` 读、够 agent 按人设和方法论行事即可）。
  - **不复制**：`sessions/`、`memory/`（旧的）、`media/`、大图（avatar.png 等）、`node_modules/`、cookies、各种 `.bak`。
  - 新建：空的 `memory/` 目录，内含空模板 `memory/trading/journal.md`（一两行说明：这是你在金融世界的交易日志，每天决策前读、决策后追加）。
  - research-loop-ts 的 `openclaw_dir` 等指向也要落在影子区内（通过 `--workspace` 即可让 `sessions/` 等写进影子目录），保证回测完全不碰真实 workspace。
- **会话**：固定 `session_key = "trading-<run_id>"`，但每天 `chat` 调用 `history=[]` —— 即每个世界日是干净上下文。跨日连续性完全靠：影子 workspace 的 `memory/trading/journal.md` + 本 run 的关键词记忆服务（§6）。
  - 副作用说明：research-loop-ts 的 `handleChat` 会用 `session_key` 解析出一个 session UUID 并把消息/状态写进影子 workspace 的 `sessions/` 下——这没问题，是「这个 bot 在这个世界 run 里的会话存档」，且在影子区内，不污染真实 workspace。

---

## 6. 关键词记忆服务

回测期间用、用完就扔、按 run 隔离、召回靠关键词足够——所以**不用 mem0 / qdrant / embedder / LLM 抽取**，换一个极简自研服务。

- **形态**：TS 写的小 HTTP 服务，是 world 包的一部分（`src/memory-server/`）。world 在每个 run 启动时拉起一个实例，绑到一个动态空闲端口，把 URL 写进该 run 的 `trading-rl-config.json` 的 `mcp.mem0`（顶层 `mem0` 键 → research-loop-ts 的 `config.mem0_url`）。这样 research-loop-ts 现有的 `memory_mem0` 插件（工具 `mem0_search` / `mem0_add`）**原样能用，research-loop-ts 零改动**。
- **API**（mem0 server 的子集，足够 `memory_mem0` 插件用）：
  - `POST /memories`：body `{ messages: [{role, content}], user_id, agent_id?, infer? }`。行为：取 `messages` 里的 `content` **原样**存一条记忆（**忽略 `infer`**，不做任何抽取/改写）；记录 `agent_id`（= `botN`，没有就归到 `user_id`）、`created_at` = 当前**世界日期**（服务从 `world/state.json.current_date` 读，与交易引擎共用同一个世界时钟）、`id`（自增/uuid）。返回 `{ results: [{ id }] }`。
  - `POST /search`：body `{ query, user_id, agent_id?, limit }`。行为：对 query 分词（中英混合，简单切分 + 去停用词），对该 `agent_id`（无则全体）下的每条记忆按关键词重合度打分（词频 / BM25-lite），返回 top-`limit`，每条形如 `{ memory, agent_id, score, created_at }`（字段名对齐 `memory_mem0` 插件的解析：它读 `memory`/`fact`、`agent_id`、`score`、`created_at`）。
  - 其它 mem0 端点（get/update/delete/history/reset）：本期不实现（`memory_mem0` 插件不调它们）；如需要可后补。
- **存储**：`world/runs/<run_id>/memory/store.jsonl`，每行一条记忆。所有 bot 共用这一个文件，按 `agent_id` 字段区分；`search` 默认按 `agent_id` 过滤（`scope='self'`，插件已处理 scope 逻辑）。
- **生命周期**：run 启动起、run 结束停；`store.jsonl` **保留**（调试/复盘用）。下个 run 是全新空文件，与上个 run 互不可见。无需 GC。
- **依赖**：纯 Node，无外部服务。

---

## 7. 配置（`world/config/world.yaml`）

字段（精确命名与默认值在实现计划里定稿）：

```yaml
research_loop_ts: /home/rooot/.openclaw/research-loop-ts   # research-loop-ts 仓库路径
workspace_root:   /home/rooot/.openclaw                     # workspace-botN 的父目录
bots: [bot1, bot7, bot9]                                    # 参与本世界的 bot 列表
replay:
  from: "2024-01-02"
  to:   "2024-06-28"
calendar: world/calendar.json                               # 交易日历来源
concurrency: 4                                              # 同一天里并发跑多少个 bot
per_bot_timeout_seconds: 1200                               # 单 bot 单日 chat 超时
rl_config_base: world/config/trading-rl-config.base.json    # 生成每 run rl-config 的基底
# 影子 workspace 的精选复制清单也可在这里覆盖默认值
```

`world.yaml` 不再含 python_bin / qdrant_bin / mem0 模型 creds（已无 mem0/qdrant 依赖）。

---

## 8. 错误处理与可观测性

- **fail-fast**：缺数据（任一交易日的 `quotes.json` 不存在）、影子 workspace 建失败、记忆服务起不来、某个 bot 的 research-loop-ts server 起不来或 `ping` 不通——任一发生，开跑前就退出非零、`state.json.status=failed`、清理已起子进程。
- **跑中**：每 bot 每日的 `sent.md` / `reply.json` / `status.json` 落盘；`run.log` 记时间线（每天开始、每 bot 开始/结束/耗时、推进到下一天）；`state.json` 实时反映进度。`world status` 读 `state.json` + 扫 `runs/<run_id>/` 给出进度摘要。
- **单 bot 故障**：当天 chat 超时/报错 → 记 `status=timeout|error` + 错误信息，**不阻塞**当天其他 bot、不阻塞翻篇。该 bot 后续交易日照常继续叫（它的 server 还活着）；若 server 进程已死，world 检测到则在 `run.log` / `summary.json` 标记该 bot 后续全部 `status=dead`。
- **summary.json**：run 结束（或被 abort）时写，含每交易日 × 每 bot 的状态/耗时/迭代数/usage，便于一眼看哪天哪个 bot 出过问题。

---

## 9. 测试策略

- **单测**：
  - 交易日历推进：给定 `calendar.json` + `replay.from/to`，算出的 `trading_dates` 正确；边界（区间端点正好是/不是交易日）。
  - 消息模板渲染：完整版/精简版世界规则块、`overview.md` 存在 vs. 由 `quotes.json` 生成 vs. 生成失败降级；路径都是绝对路径。
  - barrier 逻辑：用一个**假 chat server**（实现 `ping`/`chat`/`shutdown` 的 stub，可注入「正常返回 / 慢返回触发超时 / 报错 / 进程退出」），验证 world 正确等齐、超时不卡死、单 bot 失败不阻塞、翻篇。
  - 缺数据校验：区间内缺某天 `quotes.json` → fail-fast。
  - resume：在第 K 天人为中断，`world resume` 从 `cursor=K` 继续，不重跑前 K 天。
  - 关键词记忆服务：`add` 原样存 + `created_at` 取自 `state.json.current_date`；`search` 按 `agent_id` 过滤 + 关键词打分排序 + `limit`；返回字段名与 `memory_mem0` 插件解析对齐。
- **端到端（小回放）**：2 个交易日 + 2 个 stub bot server + 一份 stub `quotes.json`/`overview.md` + 一个真实的关键词记忆服务实例，跑完整 `world run`，断言 `runs/<run_id>/` 下产物齐全、`state.json.status=done`、`summary.json` 内容正确。不依赖真实 research-loop-ts、真实交易引擎、真实行情。

---

## 10. 未来演进（非本期）

- **live 模式**：1 世界日 = 1 真实交易日，由 systemd 定时器（参考现有 `trading-daily-close.timer` 等）每日触发推进一格。需要把「主循环」拆成「单日 step」可独立调用，并接真实的当日收盘数据源。
- **交易引擎由 world 托管启动**：若交易引擎团队希望 world 负责拉起他们的 MCP server（而非他们常驻），在 `trading-rl-config.base.json` 的 `mcp.servers` 里配命令即可，无需改 world 架构。
- mem0 端点补全（get/update/delete/history）——只在 agent 真用得上时再加。

---

## 附录 A：research-loop-ts 接口要点（本系统依赖、不修改）

- 启动：`server.ts --bot-id <id> --workspace <dir> [--config <json>]`；line-delimited JSON-RPC over stdin/stdout。
- 启动后发 `server.ready` 通知（含 bot_id / workspace / model / pid）。
- 方法：
  - `ping` → `{ pong, bot_id, workspace, model }`
  - `chat` params `{ message, history?, session_key?, channel?, metadata? }` → 流式 `notify`（`message.delta` / `tool.call` / `tool.result` / `research.*` / `message.done` 等），最终 `result { reply, session_id, assistant_messages, tool_trace, usage, iterations, truncated_by_iterations }`
  - `shutdown` → `{ ok: true }`，进程随后退出
- system prompt 由 `buildSystemPrompt` 组装：core 研究 prompt + honesty 块 + subagent 指引 + 影子 workspace 的 `METHODOLOGY.md`/`RESEARCH.md`/`MEMORY.md` + `IDENTITY.md`/`SOUL.md`/`AGENTS.md`/`USER.md` + MCP server 概览 + 当前日期（用真实日期，非世界日期——这一点本期接受，agent 需要世界日期时从 `chat` message 里读，那里写的是世界日期）。
- 长期记忆插件 `memory_mem0`：HTTP 调 `config.mem0_url`（来自 rl-config 顶层 `mem0` 键）的 `POST /memories` 和 `POST /search`；工具名 `mem0_add` / `mem0_search`。本系统的关键词记忆服务实现这两个端点的兼容子集。
- MCP：`trading-rl-config.json` 的 `mcp.servers` 里列交易引擎 MCP；`mcp.mem0` 指向本 run 的关键词记忆服务。

## 附录 B：关键决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| 世界时钟 | 回放历史日（backtest），可连续快速推进 | 本期只要回测沙盘 |
| agent 调度 | 每交易日对每 bot 发一次 `chat`；server 常驻 | research-loop-ts 已有 JSON-RPC server 模式，零改动 |
| 行情注入 | 全量写 `world/days/<date>/quotes.json` 供工具读 + prompt 里嵌精简概览 | 大数据走文件，小概览走 prompt |
| 「怎么交易」指令注入 | 放每日 `chat` 的 user message | 零改动 research-loop-ts；接受其研究向 system prompt 并存 |
| 账户状态 | 不嵌 prompt，agent 自己调交易工具查 | world 与交易系统彻底解耦 |
| 多 bot 节奏 | 配置 bot 列表，同天并发，barrier 等齐再翻篇 | 同一天所有人看同一份行情 |
| 跨日记忆 | 每日干净 session；靠影子 workspace 的 journal.md + 关键词记忆服务 | 有界、解耦；回测期专用 |
| workspace 隔离 | 精选复制成每 run 影子 workspace | 不污染真实 workspace；避免复制大文件 |
| 记忆栈 | 自研极简关键词 HTTP 服务（mem0 子集 API），无 qdrant/embedder/LLM | 回测期用完即弃、隔离、关键词召回足够 |
