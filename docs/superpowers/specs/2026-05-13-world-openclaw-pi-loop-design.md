# world: 同时支持 research-loop 与 openclaw-pi loop

**日期**：2026-05-13
**作者**：agent_invest_lab
**关联代码**：[world/](../../../world/)、[/home/rooot/.openclaw/openclaw/src/agents/](file:///home/rooot/.openclaw/openclaw/src/agents/)

## 目标

`world` 是 agent_invest_lab 的日度滚动金融世界系统：把交易日序列一天天喂给一组 bot，让每个 bot 用自己的 loop（agent runtime）回复。今天 world 只能起 `research-loop/ts/server.ts`；这个 spec 把 openclaw 的"pi"内嵌 agent runtime 也接进来，作为 world 的第二种 loop，并通过 world.yaml 的一个开关选哪种。

非目标：

- 不在同一 run 内混用两种 loop（per-run 粒度，不是 per-bot）。
- 不把 openclaw-pi 作为 world 的默认（保持 research-loop 是默认）。
- 不改 `runEmbeddedAttempt` 自身、不改 openclaw 主入口、不进 openclaw 的 dist/docs。
- 不引入 openclaw 的 dashboard/gateway/feishu 等附带能力。

## 背景

world 现状（见 [world/src/run.ts](../../../world/src/run.ts)）：

- 每个 bot 起一个 `research-loop/ts/server.ts` stdio JSON-RPC 子进程。
- 协议（research-loop 端定义、world 端在 [botServer.ts](../../../world/src/botServer.ts) 消费）：
  - 启动后通知 `server.ready`
  - `chat({message, session_key, history})` → `{reply, session_id, assistant_messages, tool_trace, usage, iterations, truncated_by_iterations}`
  - `ping`, `shutdown`
  - 运行中流式通知：`research.started` / `research.progress`（含 `phase`/`nudge`/`done`）/ `tool.call` / `tool.result` / `message.done` / `log`
- 每个 run 有一个隔离目录 `runs/<run>/rl-openclaw/`，world 把 `workspace_root/openclaw.json` 复制进去，研究 loop 的 session/事件存档写到这里，与真实 `.openclaw/` 解耦。

openclaw 的"pi" runtime（见 [/home/rooot/.openclaw/openclaw/src/agents/](file:///home/rooot/.openclaw/openclaw/src/agents/)）：

- "pi" 是 openclaw 的内嵌 agent runtime，入口函数是 `runEmbeddedAttempt`（[pi-embedded-runner/run/attempt.ts](file:///home/rooot/.openclaw/openclaw/src/agents/pi-embedded-runner/run/attempt.ts)）。
- openclaw 主进程内部通过 `resolveAgentRuntime` 在 "pi" 与 "research-loop" 之间选 loop（[runtime-config.ts](file:///home/rooot/.openclaw/openclaw/src/agents/runtime-config.ts)），由 `agents/<id>/loop-mode.json` 决定。
- 没有现成的 per-agent stdio JSON-RPC server 形态可以让 world 直接 spawn。

## 设计概览

```
world (per-run loop=research-loop)              world (per-run loop=openclaw-pi)
  ├─ rl-openclaw/openclaw.json (copy)             ├─ rl-openclaw/openclaw.json (copy of pi_openclaw_json)
  ├─ trading-rl-config.json    (gen)              └─ spawn agent_invest_pi_stdio_server.ts
  └─ spawn research-loop/ts/server.ts                     │
        │                                                 │ same JSON-RPC over stdio
        │ JSON-RPC over stdio                             │  (server.ready, chat, ping, shutdown,
        │  (server.ready, chat, ping, shutdown,           │   research.*/tool.*/message.done/log)
        │   research.*/tool.*/message.done/log)           ▼
        ▼                                          runEmbeddedAttempt
   research-loop attempt
```

要点：

1. world.yaml 顶层加 `loop: research-loop | openclaw-pi`（默认 `research-loop`）。
2. world.yaml 加 `pi_openclaw_json: <path>`（默认 `/home/rooot/.openclaw/openclaw.json`），world 启动时把它复制进 `runs/<run>/rl-openclaw/openclaw.json`，pi-server 从该副本读。复用现有的 `rlOpenclawDir` 命名（隔离机制不变）。
3. 新增 `agent_invest_pi_stdio_server.ts`（位置：`.openclaw/openclaw/src/agents/agent_invest_pi_stdio_server.ts`），是 `runEmbeddedAttempt` 的 stdio JSON-RPC 薄包装。文件头清楚标注：**不属于 openclaw 产品组件，agent_invest_lab/world 专用，不进 dist、不进 docs**。
4. pi-server 对外协议与 research-loop 完全一致 → world 端 `BotServer` / `formatBotNotification` / `chatOneBot` / `teardown` 一行不动。
5. 整 run 一种 loop（per-run 粒度），不混用。

## 各组件改动

### world：[world/src/config.ts](../../../world/src/config.ts)

`WorldConfig` 加字段：

- `loop: 'research-loop' | 'openclaw-pi'`：yaml 字段名 `loop`，缺省值 `'research-loop'`。
- `piOpenclawJson?: string`：openclaw config 源路径；缺省 `/home/rooot/.openclaw/openclaw.json`。仅 `loop === 'openclaw-pi'` 时使用。
- `openclawRoot?: string`：openclaw 仓根；缺省 `/home/rooot/.openclaw/openclaw`。仅 `loop === 'openclaw-pi'` 时使用。
- `piServerEntry?: string`：pi-server 入口路径覆盖（测试/非常规布局用）；缺省 `<openclawRoot>/src/agents/agent_invest_pi_stdio_server.ts`。

校验（仅 `loop === 'openclaw-pi'` 时执行，fail-fast）：

- `piOpenclawJson` 解析后存在。
- `piServerEntry` 解析后存在。
- `loop === 'research-loop'` 时，pi 相关字段被忽略（不校验、不警告）。

### world：[world/src/paths.ts](../../../world/src/paths.ts)

不新增路径。复用 `rlOpenclawDir(w, runId)` 作为两种 loop 的 openclaw 隔离目录（命名保留，避免影响已存在的 run 目录布局）。在 spec 注释里点明：这个目录名带"rl"前缀只是历史包袱，两种 loop 都写到这里。

### world：[world/src/run.ts](../../../world/src/run.ts)

1. 把现有 `generateRlConfig` 抽象/分流为两条路径：
   - `loop === 'research-loop'`：保持现状（复制 `workspace_root/openclaw.json` 进 `rl-openclaw/openclaw.json`、生成 `runs/<run>/trading-rl-config.json`）。
   - `loop === 'openclaw-pi'`：复制 `piOpenclawJson` → `rl-openclaw/openclaw.json`；**不**生成 `trading-rl-config.json`（pi-server 不需要）。
2. `botServerArgv` 改为按 `config.loop` 分两条 argv：
   - research-loop：保持 `node --experimental-strip-types <researchLoopTs>/server.ts --bot-id ... --workspace ... --config <runs/<run>/trading-rl-config.json>`。
   - openclaw-pi：`node --experimental-strip-types <piServerEntry> --bot-id ... --workspace ... --openclaw-json <runs/<run>/rl-openclaw/openclaw.json>`。
3. `setup` / `chatOneBot` / `runLoop` / `teardown` / `resumeWorld` / 状态写入 / 通知格式化：**不动**。

### world：[world/config/world.example.yaml](../../../world/config/world.example.yaml)

加注释段说明 `loop` 开关、`pi_openclaw_json`、`openclaw_root`、`pi_server_entry`。给一个 openclaw-pi 的最小示例块。

### world：[world/test/](../../../world/test/)

加一个 stub pi-server 烟雾测试：

- 用一个最简的 Node 脚本伪装成 pi-stdio-server（发 `server.ready`、`chat` 直接回固定字符串、收 `shutdown` 退出），通过 `world.yaml: loop: openclaw-pi` + `pi_server_entry: <stub>` 跑一遍 `runWorld`。
- 断言：world 起得来、chat 能完成、reply.json/status.json 写得对、shutdown 干净退出。
- 这层测试是 world 端的契约测试，不依赖真实 openclaw。

### openclaw：`.openclaw/openclaw/src/agents/agent_invest_pi_stdio_server.ts`（新增）

文件头注释（强制项）：

```ts
/**
 * agent_invest_pi_stdio_server
 *
 * 用途：agent_invest_lab 的 `world` 系统用的 stdio JSON-RPC bot server，
 *      薄薄一层包装 openclaw 的 `runEmbeddedAttempt`。
 * 所有者：agent_invest_lab（不是 openclaw 产品组件）。
 * 不进 openclaw 的 dist、不进 docs、不暴露给 openclaw 用户。
 * 与 openclaw 的 dashboard/feishu/cron/loop-mode.json 等机制无关。
 *
 * 若需求驱动力来自 openclaw 主产品（比如 dashboard 想 spawn pi-stdio-server），
 * 请另开一个 openclaw 自己的 server.ts、不要复用本文件。
 */
```

CLI：`--bot-id <id> --workspace <abs-dir> --openclaw-json <abs-path>`。所有三参必填。

行为：

1. 装载 `--openclaw-json` 指向的 openclaw config（复用 openclaw 的 config loader）。
2. 用 `--bot-id` 解析 agentId 对应的 runtime，**但强制走 pi**（不读 `loop-mode.json`、不调 `resolveAgentRuntime`）—— world 已经在外层选好 loop。
3. 创建 `JsonRpcStdioServer` 实例（与 research-loop server 同形态，从 stdin 读 LSP 风格帧 `Content-Length: ...\r\n\r\n<body>`，往 stdout 写）。
4. 启动完成发 `server.ready` 通知。
5. 注册 RPC method：
   - `ping` → `{pong: true, bot_id, workspace, model}`
   - `chat({message, session_key?, history?, channel?, metadata?})` → `{reply, session_id, assistant_messages, tool_trace, usage, iterations, truncated_by_iterations}`。内部调用 `runEmbeddedAttempt`，期间订阅其内部事件流并翻译成 `research.*` / `tool.*` / `message.done` / `log` 通知（具体映射见下）。
   - `shutdown` → 触发优雅退出（取消正在跑的 attempt、关掉 MCP/缓存、`process.exit(0)`）。
6. 接收 SIGTERM/SIGINT → 与 `shutdown` 同语义。

事件翻译表（pi 内部 → research-loop 协议）：

| pi 事件                       | 发送的通知                          |
| ----------------------------- | ----------------------------------- |
| attempt.started / session.spawn | `research.started`（带 topic=message 头 N 字符） |
| phase 切换                    | `research.progress` event=`phase`   |
| 进度文本/中间消息             | `research.progress` event=`nudge`   |
| tool call 开始                | `tool.call` 带 `name`/`args` 简版   |
| tool call 结束                | `tool.result` 带 `name`/`is_error`  |
| 最终 assistant message        | `message.done` 带 `text`            |
| attempt.done                  | `research.progress` event=`done`    |
| 异常 / 错误日志               | `log` level=`error` 带 `message`    |

返回值字段映射：

- `reply`：最终 assistant message 文本（多段则取最后一段，或拼接）。
- `session_id`：`runEmbeddedAttempt` 返回的 sessionId / 内部 attempt id。
- `iterations`：turn 计数（pi 内部 maintains）。
- `usage`：累计 token 数（pi 的 usage accumulator 已经在跟）。
- `truncated_by_iterations`：是否撞到 `max_total_turns` 上限。
- `assistant_messages`：本轮 assistant 消息数组（取 pi session 里的最终 assistant frames）。
- `tool_trace`：本轮 tool 调用与结果的精简数组 `[{name, args, result, is_error}]`。

错误：

- chat 失败（运行时异常、超时）→ JSON-RPC `error` 响应，`message` 是简明错误。
- 启动失败（config 缺、bot 不存在）→ stderr 写理由，进程退码非 0，**不**发 `server.ready` 通知（world 端 `BotServer.start` 会在 `readyTimeoutMs` 内 reject）。

### openclaw：测试

在 `.openclaw/openclaw/test/`（与现有 agents 测试同目录约定）加一个 e2e：

- 起 `agent_invest_pi_stdio_server.ts` 子进程（用 stub openclaw config + stub LLM/MCP）。
- 发 `ping`、`chat`、`shutdown`，断言响应形态。
- 断言至少一条 `research.*` / `message.done` 通知发出。

## 错误处理与边界

- pi-server 自身启动失败 → world 的 `BotServer.start` 在 60s 内 reject，runWorld 的 setup 阶段失败，已起的 bot 全关，状态写 failed（既有逻辑）。
- pi-server 中途崩溃 → world 端 `BotServer._alive` 翻 false，当天的 chat 报 `dead`，后续日子直接记 dead 不再发请求（[run.ts:259](../../../world/src/run.ts#L259) `brokenBots` 集合既有逻辑）。
- pi 内部超时（>`per_bot_timeout_seconds`）→ world 端 RPC 超时 reject，记 `timeout`、加入 `brokenBots`，pi-server 可能仍在跑上一条；world 不串行化的既有行为延续。
- pi-server 接 SIGINT/SIGTERM → 走 `shutdown` 路径，取消 attempt、关 MCP；world 的 SIGINT abort 路径（[run.ts:261](../../../world/src/run.ts#L261)）依旧生效。
- `pi_openclaw_json` 不存在 → config 校验阶段 fail-fast。
- 同 run 内 `loop` 切换 → resume 阶段 detect `world.yaml` 与 state 的 loop 不一致 → 拒绝 resume（新增校验）。

## 不做的事（YAGNI）

- 不做 per-bot loop 混合调度。
- 不在 pi-server 里读 `agents/<id>/loop-mode.json`（loop 由 world.yaml 强制）。
- 不复用 openclaw 的 gateway/dashboard/feishu。
- 不为 pi 端单独写一份 MCP / model 配置 schema（直接用 openclaw.json 现有形态）。
- 不改 research-loop 端的协议或行为。
- 不调整 `runs/<run>/rl-openclaw/` 的命名（即便对 pi 来说"rl"前缀名不副实，重命名收益不抵改动成本）。

## 验收

1. `world.yaml: loop: research-loop` 时，行为与改造前完全一致（既有测试全绿）。
2. `world.yaml: loop: openclaw-pi` + 真实 openclaw.json 时，world 能跑通一个 2~3 个交易日的小回放，每个 bot 在每天写出 sent.md / reply.json / status.json，summary.json 形态与 research-loop 路径相同。
3. world 端 stub pi-server 烟雾测试绿。
4. openclaw 端 pi-server e2e 测试绿。
5. resume 跨 loop 切换被拒绝。
6. `agent_invest_pi_stdio_server.ts` 文件头标注完整（用途/所有者/不进 dist 三条）。
