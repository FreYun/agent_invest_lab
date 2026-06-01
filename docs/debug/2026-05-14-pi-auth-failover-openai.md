# Pi loop 调用 runEmbeddedPiAgent 时 FailoverError → "openai" provider，求帮看

**日期**：2026-05-14
**仓库**：
- agent_invest_lab `feat/world-system` @ commit 003c5a6（已 push，世界系统侧）
- .openclaw master @ commit d2f8592e（pi-stdio-server 侧）

**复现命令**：

```bash
cd /home/rooot/agent_invest_lab
rm -rf /tmp/world-pi-e2e-runtime session && mkdir -p /tmp/world-pi-e2e-runtime && \
  cp -r world/runtime/days /tmp/world-pi-e2e-runtime/ && \
  cp world/runtime/calendar.json /tmp/world-pi-e2e-runtime/
node --experimental-strip-types world/main.ts run \
  --config /tmp/world-pi-e2e.yaml \
  --world-dir /tmp/world-pi-e2e-runtime --run-id e2e-debug
```

`/tmp/world-pi-e2e.yaml`：

```yaml
research_loop: /unused
openclaw_json: /home/rooot/agent_invest_lab/world/config/openclaw.json
openclaw_root: /home/rooot/.openclaw/openclaw
bots_root: /home/rooot/agent_invest_lab/bots
skills_root: /home/rooot/agent_invest_lab/skills
bots: [bot11]
replay: { from: "2024-03-14", to: "2024-03-14" }
calendar: /home/rooot/agent_invest_lab/world/runtime/calendar.json
concurrency: 1
per_bot_timeout_seconds: 600
loop: openclaw-pi
pi_sessions_dir: /home/rooot/agent_invest_lab/session
```

## 现象

跑到 chat 时报错（5x 都重现）：

```
[bot11] [diagnostic] lane task error: lane=main durationMs=57929
  error="FailoverError: No API key found for provider \"openai\".
   Auth store: /home/rooot/agent_invest_lab/session/bot11/agent/auth-profiles.json
   (agentDir: /home/rooot/agent_invest_lab/session/bot11/agent).
   Configure auth for this agent (openclaw agents add <id>) or copy
   auth-profiles.json from the main agentDir."
```

`durationMs=58~63s` —— pi 等了模型 timeout 后才 failover。

## 链路

1. world 起 pi-stdio-server，环境变量：
   - `OPENCLAW_AGENT_DIR=/home/rooot/agent_invest_lab/session/bot11/agent`
   - `PI_CODING_AGENT_DIR=...同上`
   - cwd=`/home/rooot/.openclaw/openclaw`
2. pi-server 调 `runEmbeddedPiAgent({ sessionId: <uuid>, agentId: 'bot11', workspaceDir, prompt, config, ... })`
3. config 来自 `/home/rooot/agent_invest_lab/world/config/openclaw.json`，已确认包含：
   ```jsonc
   {
     "agents": {
       "defaults": { "model": { "primary": "zai-coding-plan/qwen3.5-plus" } },
       "list": [{
         "id": "bot11",
         "agentDir": "/home/rooot/agent_invest_lab/session/bot11/agent",
         "workspace": "/home/rooot/agent_invest_lab/bots/bot11",
         "model": {
           "primary": "zai-coding-plan/qwen3.6-plus",
           "fallbacks": ["zai-coding-plan/qwen3.5-plus", "zai-coding-plan/kimi-k2.5"]
         }
       }, ...]
     },
     "models": {
       "providers": {
         "zai-coding-plan": {
           "baseUrl": "https://dd-ai-api.eastmoney.com/v1",
           "api": "openai-completions",
           "apiKey": "sk-REDACTED-ROTATE-ME",
           "models": [{ "id": "qwen3.5-plus", ... }, { "id": "qwen3.6-plus", ... }, ...]
         }
       }
     }
   }
   ```
4. auth-profiles.json（`/home/rooot/agent_invest_lab/session/bot11/agent/auth-profiles.json`）
   是从真实 `~/.openclaw/agents/bot11/agent/auth-profiles.json` seed 过来的 + 又手动补了 zai-coding-plan 条目：
   ```json
   {
     "profiles": {
       "kimi-coding:default": { "type":"api_key","provider":"kimi-coding","key":"sk-kimi-..." },
       "legacylands:default": { "type":"api_key","provider":"legacylands","key":"cr_..." },
       "glm:default":         { "type":"api_key","provider":"glm","key":"..." },
       "zai-coding-plan:default": {
         "type":"api_key","provider":"zai-coding-plan",
         "key":"sk-REDACTED-ROTATE-ME"
       }
     },
     "lastGood": { ..., "zai-coding-plan":"zai-coding-plan:default" }
   }
   ```
5. models.json（同目录）含 `kimi-coding / zai-coding-plan / legacylands / kimi / codex`，每个都有 inline `apiKey`，models 列表完整。

## 已验证

- **API 本身工作**：直接 curl `https://dd-ai-api.eastmoney.com/v1/chat/completions` 用同一个 apiKey + 同一个 model（`qwen3.5-plus`），200 OK 返回正常 reply：

  ```
  curl -s -X POST 'https://dd-ai-api.eastmoney.com/v1/chat/completions' \
    -H 'Content-Type: application/json' \
    -H 'Authorization: Bearer sk-REDACTED-ROTATE-ME' \
    -d '{"model":"qwen3.5-plus","messages":[{"role":"user","content":"hi"}],"max_tokens":10}'
  # → {"choices":[{"message":{"content":"Hi there! How can I help you today?",...}}],...}
  ```

- **相同代码 1~2 小时前跑通过**：同样 commit (003c5a6/d2f8592e)，e2e `e2e-final2` 拿到 reply 408 字、usage 1568。
  期间没改 pi-server / world 的 chat-resolve 路径；唯一变化是用户在并行修改 `world/config/openclaw.json`（从 stripped credentials 扩成 full bak 内容）。

- **错误源**：[openclaw/src/agents/model-auth.ts:528-534](file:///home/rooot/.openclaw/openclaw/src/agents/model-auth.ts#L528-L534)
  里的 `throw new Error("No API key found for provider...")`。
  上游入口：[openclaw/src/agents/pi-embedded-runner/run.ts:290](file:///home/rooot/.openclaw/openclaw/src/agents/pi-embedded-runner/run.ts#L290) `throw new FailoverError(error ?? \`Unknown model: ${provider}/${modelId}\`, { reason: "model_not_found" })`。

- **errored provider 是 `openai` 而不是配置里的 `zai-coding-plan`**。说明 pi 的 primary + fallback 全失败，落到某个 last-resort `openai` 默认。

## 当前怀疑

1. **`api: "openai-completions"` 的 custom provider，pi 在 model lookup 时把 provider tag 改写成 `"openai"`**，然后 auth lookup 用了改写后的 "openai" 而不是原 `"zai-coding-plan"`。这能解释为什么 inline apiKey 没被命中、auth-profiles 里加了 zai-coding-plan 还是没用。

2. **pi-coding-agent SDK / pi-ai SDK 内部默认 "openai" 兜底**。从 [pi-ai/dist/models.generated.js:5218](file:///home/rooot/.openclaw/openclaw/node_modules/@mariozechner/pi-ai/dist/models.generated.js#L5218) 看到 SDK 内置 `openai` provider 多个 model；可能 SDK 在 lookup `zai-coding-plan/qwen3.5-plus` 时 fallback 到了内置 catalog 的 `openai`。

3. **agentDir override 与 config.agents.list[bot11].agentDir 冲突**：环境变量 `OPENCLAW_AGENT_DIR` 指向 lab；config 里 bot11 entry 的 `agentDir` 字段也指向 lab；但是 pi 可能用其中一个不一致地解析。

## 已排除

- 不是网络问题（curl 直连 OK）。
- 不是 OPENCLAW_AGENT_DIR 没生效——错误消息里 `agentDir` 字段确实显示的 lab 路径。
- 不是 mcp.mem0 patch 错（[run.ts:46](../../world/src/run.ts#L46) 加了 schema-safe 守卫，只在 `mcp.mem0` 已存在时改写）。
- 不是 tsx loader 问题（pi-server 起得来，server.ready/ping 都正常）。

## world 侧不变的部分

- `botServerArgv` pi 分支用 `--import file://<openclawRoot>/node_modules/tsx/dist/loader.mjs`。
- spawn cwd = `config.openclawRoot`。
- spawn env：`OPENCLAW_AGENT_DIR=<piSessionsDir>/<botId>/agent`、`PI_CODING_AGENT_DIR=` 同、`WORLD_PI_SESSIONS_DEST=<piSessionsDir>/<botId>/sessions`（用于 chat 结束后 pi-server 把 session jsonl 从 `~/.openclaw/agents/<resolved>/sessions/` 搬到 lab dir）。
- seed once：把 `~/.openclaw/agents/<botId>/agent/{auth-profiles.json,auth-state.json,models.json}` 拷到 `<piSessionsDir>/<botId>/agent/`，已存在不动。
- `rl-openclaw/openclaw.json` 是从 `config.openclawJson` 复制，再 patch `mcp.mem0` 为 run 隔离的 memory URL（仅当原 key 已存在）。

## pi-stdio-server 关键源码

`/home/rooot/.openclaw/openclaw/src/agents/agent_invest_pi_stdio_server.ts`

- chat 时构造 sessionId = `randomUUID()`，sessionFile = 临时文件，runId = `world-<ts>`。
- 调用 `runEmbeddedPiAgent({ sessionId, sessionKey, agentId: botId, workspaceDir, sessionFile, config, prompt, timeoutMs: 30*60*1000, runId })`。
- config 来自 `createConfigIO({ configPath: openclawJson }).loadConfig()`。
- 返回的 result mapping 取 `meta.finalAssistantVisibleText ?? payloads[*].text ?? messagingToolSentTexts.join('\n')` 作为 reply；`meta.agentMeta.usage.total ?? input+output` 作为 usage。

## 问题

希望资深同事帮看：

1. 给定上面 openclaw.json + auth-profiles.json + models.json 这套，pi 应该能 resolve `zai-coding-plan/qwen3.5-plus` → 找到 inline apiKey → 调通。**为什么会落到 "openai" 这个 provider？**
2. `api: "openai-completions"` 是不是在 pi 内部被映射成 provider tag `"openai"` 用于 auth lookup？
3. 有没有什么 env / config 字段能让 pi 不去搜索 hardcoded `"openai"` provider？

如能定位到 model-auth 的具体 resolveProvider 决策点，应该就好修了。

## 旁支：相同代码上午跑通过的证据

之前 commit 003c5a6 跑出来的 `reply.json`（不再保留，但 commit msg 提到了）：

```
feat(world): pass WORLD_PI_SESSIONS_DEST env so pi-server can relocate session jsonls to lab dir
```

那次 run 的 `reply.reply` 长度 408，`usage` 1568，`session_id` UUID。openclaw.json 当时是什么状态不能 100% 确定（用户在并行编辑）。

---

**重现路径在 `/tmp/world-pi-e2e.yaml` + 上面命令**，欢迎直接跑、改 yaml 字段、加 `console.log` 探针。
