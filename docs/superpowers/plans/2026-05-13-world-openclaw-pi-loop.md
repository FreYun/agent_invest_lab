# world: 同时支持 research-loop 与 openclaw-pi loop — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `world` 在 `world.yaml` 里通过一个开关选 `research-loop` 或 `openclaw-pi` loop；新建 `agent_invest_pi_stdio_server.ts` 作为 pi 的 stdio JSON-RPC 包装，协议与 research-loop server 完全一致，world 端 BotServer 不动。

**Architecture:** world.yaml 顶层加 `loop: research-loop | openclaw-pi`（默认 `research-loop`，向后兼容）。pi 分支由 world spawn 新建的 `agent_invest_pi_stdio_server.ts`，文件落在 `.openclaw/openclaw/src/agents/`，但文件头明确标注"agent_invest_lab 专用、非 openclaw 产品组件"。Pi-server 内部薄包装 `runEmbeddedPiAgent`，把它的结果映射成 research-loop chat 协议形态。整 run 一种 loop，不混用；resume 跨 loop 会被拒绝。

**Tech Stack:** TypeScript（node `--experimental-strip-types`）、Node 内置 test runner（`node:test`）、NDJSON over stdin/stdout JSON-RPC、`runEmbeddedPiAgent` from openclaw `agents/pi-embedded-runner.ts:20-22`。

**关联 spec:** [docs/superpowers/specs/2026-05-13-world-openclaw-pi-loop-design.md](../specs/2026-05-13-world-openclaw-pi-loop-design.md)（commit 5354b36）。

---

## 协议契约 reminder

pi-server 必须与 research-loop server（[/home/rooot/.openclaw/research-loop/ts/src/server.ts](file:///home/rooot/.openclaw/research-loop/ts/src/server.ts)）完全等价：

- 输出：NDJSON（每行一条 JSON，`\n` 结尾）。stderr 用于日志/错误打印。
- 启动后立刻发通知 `{method: "server.ready", params: {bot_id, workspace, model, pid}}`。
- RPC 方法：
  - `ping({}) → {pong: true, bot_id, workspace, model}`
  - `chat({message, session_key?, history?, channel?, metadata?}) → {reply, session_id, assistant_messages, tool_trace, usage, iterations, truncated_by_iterations}`
  - `shutdown({}) → {ok: true}` 然后进程退出（exit code 0）
- 中途通知（顺序无强约束）：`research.started` / `research.progress`（event ∈ {phase, nudge, done}）/ `tool.call` / `tool.result` / `message.done` / `log`。

参考研究 loop 的 stub 实现：[world/test/helpers/stubBotServer.ts](../../../world/test/helpers/stubBotServer.ts)。

---

## File Structure

**Modified（world 端）:**
- [world/src/config.ts](../../../world/src/config.ts) — 扩展 `WorldConfig` 与 `loadWorldConfig`
- [world/src/run.ts](../../../world/src/run.ts) — 按 `loop` 分支 `botServerArgv` 与 setup 时的配置生成
- [world/src/state.ts](../../../world/src/state.ts) — `WorldState` 加 `loop` 字段（持久化用于 resume 一致性校验）
- [world/config/world.example.yaml](../../../world/config/world.example.yaml) — 文档化两种 loop
- [world/test/config.test.ts](../../../world/test/config.test.ts) — 加 loop 字段相关测试
- [world/test/run.test.ts](../../../world/test/run.test.ts) — 加 loop 分支 argv 测试
- [world/test/resume.test.ts](../../../world/test/resume.test.ts) — 加 loop 切换拒绝测试

**Created（world 端）:**
- `world/test/helpers/stubPiBotServer.ts` — pi-stdio-server 的 stub，与 stubBotServer 同协议但接 `--openclaw-json`
- `world/test/stubPiBotServer.test.ts` — stub 自身的契约测试
- `world/test/pi-smoke.test.ts` — 用 stubPiBotServer 跑 runWorld 的烟雾

**Created（openclaw 端，`/home/rooot/.openclaw/openclaw/`）:**
- `src/agents/agent_invest_pi_stdio_server.ts` — pi-stdio-server 入口
- `test/agent_invest_pi_stdio_server.test.ts`（或就近 agents/ 测试目录）— 启动/ping/shutdown 契约测试
- `test/agent_invest_pi_stdio_server.chat.test.ts` — chat 路径测试（用 mock 或 opt-in 真实）

---

## Phase A: world 端 loop-aware 化（不动 openclaw）

### Task A1: 扩展 WorldConfig 与 loadWorldConfig

**Files:**
- Modify: [world/src/config.ts](../../../world/src/config.ts)
- Test: [world/test/config.test.ts](../../../world/test/config.test.ts)

- [ ] **Step 1: 在 config.test.ts 顶部读现有测试结构** —— 不动测试代码，只是为了写新的测试用例时与现有 import/写法一致

Run: `cat world/test/config.test.ts | head -30`

- [ ] **Step 2: 写失败测试（默认 loop = research-loop）**

加到 `world/test/config.test.ts` 末尾：

```ts
test('loop defaults to research-loop when field omitted', () => {
  const yaml = [
    'research_loop_ts: /tmp/rl-ts',
    'workspace_root: /tmp/ws',
    'bots: [bot1]',
    'replay:',
    '  from: "2024-01-02"',
    '  to: "2024-01-03"',
  ].join('\n') + '\n'
  const p = mkTmpYaml(yaml)
  const cfg = loadWorldConfig(p)
  assert.equal(cfg.loop, 'research-loop')
  assert.equal(cfg.piOpenclawJson, undefined)
  assert.equal(cfg.openclawRoot, undefined)
  assert.equal(cfg.piServerEntry, undefined)
})

test('loop=openclaw-pi requires pi_openclaw_json file to exist', () => {
  const yaml = [
    'research_loop_ts: /tmp/rl-ts',
    'workspace_root: /tmp/ws',
    'bots: [bot1]',
    'replay:',
    '  from: "2024-01-02"',
    '  to: "2024-01-03"',
    'loop: openclaw-pi',
    'pi_openclaw_json: /tmp/does-not-exist-pi.json',
  ].join('\n') + '\n'
  const p = mkTmpYaml(yaml)
  assert.throws(() => loadWorldConfig(p), /pi_openclaw_json/)
})

test('loop=openclaw-pi with valid paths populates pi fields', () => {
  const tmpDir = mkdtempSync(join(tmpdir(), 'world-cfg-'))
  const pij = join(tmpDir, 'openclaw.json'); writeFileSync(pij, '{}\n')
  const ocRoot = join(tmpDir, 'oc'); mkdirSync(ocRoot, { recursive: true })
  const piEntry = join(ocRoot, 'src/agents/agent_invest_pi_stdio_server.ts')
  mkdirSync(dirname(piEntry), { recursive: true }); writeFileSync(piEntry, '// stub\n')
  const yaml = [
    'research_loop_ts: /tmp/rl-ts',
    'workspace_root: /tmp/ws',
    'bots: [bot1]',
    'replay:',
    '  from: "2024-01-02"',
    '  to: "2024-01-03"',
    'loop: openclaw-pi',
    `pi_openclaw_json: ${pij}`,
    `openclaw_root: ${ocRoot}`,
  ].join('\n') + '\n'
  const p = mkTmpYaml(yaml)
  const cfg = loadWorldConfig(p)
  assert.equal(cfg.loop, 'openclaw-pi')
  assert.equal(cfg.piOpenclawJson, pij)
  assert.equal(cfg.openclawRoot, ocRoot)
  assert.equal(cfg.piServerEntry, piEntry)
})

test('loop=openclaw-pi rejects unknown values', () => {
  const yaml = [
    'research_loop_ts: /tmp/rl-ts',
    'workspace_root: /tmp/ws',
    'bots: [bot1]',
    'replay:',
    '  from: "2024-01-02"',
    '  to: "2024-01-03"',
    'loop: not-a-loop',
  ].join('\n') + '\n'
  const p = mkTmpYaml(yaml)
  assert.throws(() => loadWorldConfig(p), /loop/)
})
```

并在文件顶部 import 段加：

```ts
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
```

（如果已存在则不重复）

- [ ] **Step 3: 运行测试，确认失败**

Run: `cd world && node --experimental-strip-types --test test/config.test.ts 2>&1 | tail -30`
Expected: 至少有"loop defaults..."等 4 个新测试 FAIL（字段未定义）

- [ ] **Step 4: 实现 — 修改 [world/src/config.ts](../../../world/src/config.ts)**

a) 在 `WorldConfig` 接口里加：

```ts
loop: 'research-loop' | 'openclaw-pi'
piOpenclawJson?: string
openclawRoot?: string
piServerEntry?: string
```

b) 在 `loadWorldConfig` 末尾 `return` 前插入：

```ts
const loopRaw = raw.loop
let loop: 'research-loop' | 'openclaw-pi' = 'research-loop'
if (loopRaw !== undefined) {
  if (loopRaw !== 'research-loop' && loopRaw !== 'openclaw-pi') {
    throw new Error(`world config: "loop" must be "research-loop" or "openclaw-pi" (got ${JSON.stringify(loopRaw)})`)
  }
  loop = loopRaw
}

let piOpenclawJson: string | undefined
let openclawRoot: string | undefined
let piServerEntry: string | undefined
if (loop === 'openclaw-pi') {
  piOpenclawJson = typeof raw.pi_openclaw_json === 'string' && raw.pi_openclaw_json.trim()
    ? resolveMaybe(baseDir, raw.pi_openclaw_json)
    : '/home/rooot/.openclaw/openclaw.json'
  if (!existsSync(piOpenclawJson)) {
    throw new Error(`world config: pi_openclaw_json not found: ${piOpenclawJson}`)
  }
  openclawRoot = typeof raw.openclaw_root === 'string' && raw.openclaw_root.trim()
    ? resolveMaybe(baseDir, raw.openclaw_root)
    : '/home/rooot/.openclaw/openclaw'
  piServerEntry = typeof raw.pi_server_entry === 'string' && raw.pi_server_entry.trim()
    ? resolveMaybe(baseDir, raw.pi_server_entry)
    : join(openclawRoot, 'src/agents/agent_invest_pi_stdio_server.ts')
  if (!existsSync(piServerEntry)) {
    throw new Error(`world config: pi_server_entry not found: ${piServerEntry}`)
  }
}
```

c) 在文件顶部 import 区加：

```ts
import { existsSync, readFileSync } from 'node:fs'
import { dirname, isAbsolute, join, resolve } from 'node:path'
```

（`existsSync` 和 `join` 是新增）

d) 把 `return` 语句更新为：

```ts
return { researchLoopTs, workspaceRoot, bots, replay: { from, to }, calendar, concurrency, perBotTimeoutSeconds, rlConfigBase, rlOpenclawDir, shadowInclude, loop, piOpenclawJson, openclawRoot, piServerEntry }
```

- [ ] **Step 5: 跑测试确认绿**

Run: `cd world && node --experimental-strip-types --test test/config.test.ts 2>&1 | tail -30`
Expected: 全部 PASS（含原有测试）

- [ ] **Step 6: 跑整套 world 测试，确认没破坏其他文件**

Run: `cd world && node --experimental-strip-types --test test/*.test.ts 2>&1 | tail -10`
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add world/src/config.ts world/test/config.test.ts
git commit -m "$(cat <<'EOF'
feat(world): add loop config field (research-loop | openclaw-pi) with pi paths

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A2: Run.ts — 抽出 loop-aware 的 source/path/argv 辅助函数

**Files:**
- Modify: [world/src/run.ts](../../../world/src/run.ts)
- Test: [world/test/run.test.ts](../../../world/test/run.test.ts)

- [ ] **Step 1: 读现有 run.test.ts 找现有 `botServerArgv` 测试**

Run: `grep -n "botServerArgv\|loop" world/test/run.test.ts | head -20`

- [ ] **Step 2: 写失败测试**

加到 `world/test/run.test.ts` 末尾：

```ts
test('botServerArgv: research-loop branch points at researchLoopTs/server.ts with --config', () => {
  const cfg = baseConfig({ loop: 'research-loop' })
  const argv = botServerArgv(cfg, 'bot7', '/tmp/ws/bot7', '/tmp/runs/r1/trading-rl-config.json')
  assert.equal(argv[0], process.execPath)
  assert.equal(argv[1], '--experimental-strip-types')
  assert.match(argv[2], /research-loop[^/]*\/server\.ts$/)
  assert.deepEqual(argv.slice(3), ['--bot-id', 'bot7', '--workspace', '/tmp/ws/bot7', '--config', '/tmp/runs/r1/trading-rl-config.json'])
})

test('botServerArgv: openclaw-pi branch points at piServerEntry with --openclaw-json', () => {
  const cfg = baseConfig({
    loop: 'openclaw-pi',
    piOpenclawJson: '/tmp/oc.json',
    openclawRoot: '/tmp/oc',
    piServerEntry: '/tmp/oc/src/agents/agent_invest_pi_stdio_server.ts',
  })
  const argv = botServerArgv(cfg, 'bot7', '/tmp/ws/bot7', '/tmp/runs/r1/rl-openclaw/openclaw.json')
  assert.equal(argv[0], process.execPath)
  assert.equal(argv[1], '--experimental-strip-types')
  assert.equal(argv[2], '/tmp/oc/src/agents/agent_invest_pi_stdio_server.ts')
  assert.deepEqual(argv.slice(3), ['--bot-id', 'bot7', '--workspace', '/tmp/ws/bot7', '--openclaw-json', '/tmp/runs/r1/rl-openclaw/openclaw.json'])
})
```

如果文件里没有 `baseConfig` 工厂，加：

```ts
function baseConfig(overrides: Partial<WorldConfig> = {}): WorldConfig {
  return {
    researchLoopTs: '/tmp/research-loop/ts',
    workspaceRoot: '/tmp/ws',
    bots: ['bot7'],
    replay: { from: '2024-01-02', to: '2024-01-03' },
    calendar: '/tmp/cal.json',
    concurrency: 1,
    perBotTimeoutSeconds: 30,
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'research-loop',
    piOpenclawJson: undefined,
    openclawRoot: undefined,
    piServerEntry: undefined,
    ...overrides,
  }
}
```

import 段需要有 `import { botServerArgv } from '../src/run.ts'` 与 `import type { WorldConfig } from '../src/config.ts'`。

- [ ] **Step 3: 跑测试确认 openclaw-pi 分支 FAIL**

Run: `cd world && node --experimental-strip-types --test test/run.test.ts 2>&1 | tail -20`
Expected: `openclaw-pi branch` FAIL（仍走 research-loop 路径）

- [ ] **Step 4: 改 [world/src/run.ts](../../../world/src/run.ts) 的 `botServerArgv`（line 26-29）**

替换为：

```ts
export function botServerArgv(config: WorldConfig, botId: string, workspace: string, loopConfigPath: string): string[] {
  if (config.loop === 'openclaw-pi') {
    if (!config.piServerEntry) throw new Error('botServerArgv: piServerEntry required for openclaw-pi loop')
    return [process.execPath, '--experimental-strip-types', config.piServerEntry, '--bot-id', botId, '--workspace', workspace, '--openclaw-json', loopConfigPath]
  }
  const serverEntry = join(config.researchLoopTs, 'server.ts')
  return [process.execPath, '--experimental-strip-types', serverEntry, '--bot-id', botId, '--workspace', workspace, '--config', loopConfigPath]
}
```

- [ ] **Step 5: 跑测试确认全绿**

Run: `cd world && node --experimental-strip-types --test test/run.test.ts 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add world/src/run.ts world/test/run.test.ts
git commit -m "$(cat <<'EOF'
feat(world): botServerArgv switches on loop (research-loop | openclaw-pi)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A3: Run.ts — 让 setup 根据 loop 选不同的 openclaw.json 源 + 生成 loop config

**Files:**
- Modify: [world/src/run.ts](../../../world/src/run.ts)
- Test: [world/test/run.test.ts](../../../world/test/run.test.ts)

- [ ] **Step 1: 写失败测试 — openclaw.json 源选择**

加到 `world/test/run.test.ts` 末尾。需要从 run.ts 导出新辅助函数（下一步再实现）：

```ts
test('openclawJsonSource: research-loop returns workspace_root/openclaw.json', () => {
  const cfg = baseConfig({ loop: 'research-loop', workspaceRoot: '/tmp/ws' })
  assert.equal(openclawJsonSource(cfg), '/tmp/ws/openclaw.json')
})

test('openclawJsonSource: openclaw-pi returns piOpenclawJson', () => {
  const cfg = baseConfig({ loop: 'openclaw-pi', piOpenclawJson: '/tmp/pi/openclaw.json' })
  assert.equal(openclawJsonSource(cfg), '/tmp/pi/openclaw.json')
})

test('loopConfigPath: research-loop points at runConfigFile', () => {
  const cfg = baseConfig({ loop: 'research-loop' })
  const p = loopConfigPath(cfg, '/tmp/world', 'r1')
  assert.match(p, /\/runs\/r1\/trading-rl-config\.json$/)
})

test('loopConfigPath: openclaw-pi points at run rl-openclaw/openclaw.json', () => {
  const cfg = baseConfig({ loop: 'openclaw-pi' })
  const p = loopConfigPath(cfg, '/tmp/world', 'r1')
  assert.match(p, /\/runs\/r1\/rl-openclaw\/openclaw\.json$/)
})
```

import 段加：`import { botServerArgv, openclawJsonSource, loopConfigPath } from '../src/run.ts'`

- [ ] **Step 2: 跑测试 — 两个新函数缺失，FAIL**

Run: `cd world && node --experimental-strip-types --test test/run.test.ts 2>&1 | tail -20`

- [ ] **Step 3: 在 [world/src/run.ts](../../../world/src/run.ts) 中实现两个新导出**

在文件中（建议放在 `botServerArgv` 上方）加：

```ts
/** 选 openclaw.json 源路径：research-loop 用 workspace_root/openclaw.json；pi 用 piOpenclawJson。 */
export function openclawJsonSource(config: WorldConfig): string {
  if (config.loop === 'openclaw-pi') {
    if (!config.piOpenclawJson) throw new Error('openclawJsonSource: piOpenclawJson required for openclaw-pi loop')
    return config.piOpenclawJson
  }
  return join(config.workspaceRoot, 'openclaw.json')
}

/** 选 loop server 的配置文件路径：research-loop 用生成的 trading-rl-config.json；pi 直接用 rl-openclaw/openclaw.json 副本。 */
export function loopConfigPath(config: WorldConfig, worldRoot: string, runId: string): string {
  if (config.loop === 'openclaw-pi') return join(P.rlOpenclawDir(worldRoot, runId), 'openclaw.json')
  return P.runConfigFile(worldRoot, runId)
}
```

- [ ] **Step 4: 跑测试 — 全绿**

Run: `cd world && node --experimental-strip-types --test test/run.test.ts 2>&1 | tail -20`

- [ ] **Step 5: 改 setup() — 用新辅助函数替代硬编码**

在 [world/src/run.ts](../../../world/src/run.ts) 的 `setup()` 函数里：

替换 line 121 附近的：
```ts
const srcOpenclawJson = join(config.workspaceRoot, 'openclaw.json')
```
为：
```ts
const srcOpenclawJson = openclawJsonSource(config)
```

替换 line 137 附近的：
```ts
generateRlConfig(config, worldRoot, runId, memory.url, rlOpenclawDir)
```
为：
```ts
if (config.loop === 'research-loop') {
  generateRlConfig(config, worldRoot, runId, memory.url, rlOpenclawDir)
} else {
  // openclaw-pi: 把 mcp.mem0 patch 到本 run 的 memory server URL，写回 rl-openclaw/openclaw.json
  patchPiOpenclawJsonMemory(rlOpenclawDir, memory.url)
}
```

替换 line 147 附近的：
```ts
const argv = botServerArgv(config, botId, shadow, P.runConfigFile(worldRoot, runId))
```
为：
```ts
const argv = botServerArgv(config, botId, shadow, loopConfigPath(config, worldRoot, runId))
```

并加新的 helper（建议放在 `generateRlConfig` 旁边）：

```ts
/** openclaw-pi loop：把 rl-openclaw/openclaw.json 的 mcp.mem0 改写为本 run 的 memory URL，让 pi 用隔离的记忆服务。 */
function patchPiOpenclawJsonMemory(rlOpenclawDir: string, memoryUrl: string): void {
  const p = join(rlOpenclawDir, 'openclaw.json')
  let cfg: Record<string, unknown>
  try { cfg = JSON.parse(readFileSync(p, 'utf8')) as Record<string, unknown> }
  catch (err) { throw new Error(`cannot read ${p}: ${err instanceof Error ? err.message : String(err)}`) }
  const mcp = (typeof cfg.mcp === 'object' && cfg.mcp ? cfg.mcp : {}) as Record<string, unknown>
  mcp.mem0 = memoryUrl
  cfg.mcp = mcp
  writeFileSync(p, JSON.stringify(cfg, null, 2) + '\n')
}
```

- [ ] **Step 6: 跑全部 world 测试**

Run: `cd world && node --experimental-strip-types --test test/*.test.ts 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add world/src/run.ts world/test/run.test.ts
git commit -m "$(cat <<'EOF'
feat(world): setup branches on loop — pi uses piOpenclawJson and patches mcp.mem0

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A4: WorldState 加 `loop`，resume 拒绝跨 loop

**Files:**
- Modify: [world/src/state.ts](../../../world/src/state.ts)
- Modify: [world/src/run.ts](../../../world/src/run.ts)
- Test: [world/test/state.test.ts](../../../world/test/state.test.ts), [world/test/resume.test.ts](../../../world/test/resume.test.ts)

- [ ] **Step 1: 写失败测试 — state 包含 loop**

在 `world/test/state.test.ts` 末尾加：

```ts
test('WorldState round-trips loop field', () => {
  const dir = mkdtempSync(join(tmpdir(), 'world-state-'))
  const st: WorldState = {
    run_id: 'r1', status: 'running', current_date: '2024-01-02',
    trading_dates: ['2024-01-02'], cursor: 0, bots: ['bot1'],
    memory_port: 0, started_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
    loop: 'openclaw-pi',
  }
  writeState(dir, st)
  assert.equal(readState(dir).loop, 'openclaw-pi')
})

test('readState back-fills loop=research-loop for legacy state without the field', () => {
  const dir = mkdtempSync(join(tmpdir(), 'world-state-'))
  const path = join(dir, 'state.json')
  writeFileSync(path, JSON.stringify({
    run_id: 'r1', status: 'done', current_date: '2024-01-02',
    trading_dates: ['2024-01-02'], cursor: 1, bots: ['bot1'],
    memory_port: 0, started_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
  }))
  assert.equal(readState(dir).loop, 'research-loop')
})
```

如有缺失 import 补：`import { mkdtempSync, writeFileSync } from 'node:fs'`、`import { tmpdir } from 'node:os'`、`import { join } from 'node:path'`。

- [ ] **Step 2: 改 [world/src/state.ts](../../../world/src/state.ts)**

`WorldState` 接口加：
```ts
loop: 'research-loop' | 'openclaw-pi'
```

`readState` 改为：
```ts
export function readState(worldRoot: string): WorldState {
  const path = stateFile(worldRoot)
  if (!existsSync(path)) throw new Error(`no state.json at ${path}`)
  const raw = JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>
  // 向后兼容：旧 state 没有 loop 字段，默认 research-loop
  if (raw.loop !== 'research-loop' && raw.loop !== 'openclaw-pi') raw.loop = 'research-loop'
  return raw as WorldState
}
```

- [ ] **Step 3: 跑 state 测试**

Run: `cd world && node --experimental-strip-types --test test/state.test.ts 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 4: 把 loop 写进 initial state（[world/src/run.ts](../../../world/src/run.ts) `runWorld`）**

替换 `runWorld` 里的 `initial` 对象（约 line 240）：

```ts
const initial: WorldState = {
  run_id: runId, status: 'running', current_date: setupRes.tradingDates[0],
  trading_dates: setupRes.tradingDates, cursor: 0, bots: config.bots,
  memory_port: setupRes.memory.port, started_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  loop: config.loop,
}
```

- [ ] **Step 5: 在 resumeWorld 加 loop 一致性校验**

在 `resumeWorld`（约 line 302）的 `state.status !== 'running'` 检查后立刻加：

```ts
if (state.loop !== config.loop) {
  throw new Error(`resume: state loop="${state.loop}" but world.yaml loop="${config.loop}" — refuse to resume across loop change`)
}
```

- [ ] **Step 6: 写 resume 跨 loop 拒绝测试**

加到 `world/test/resume.test.ts` 末尾：

```ts
test('resumeWorld rejects when world.yaml loop differs from state.loop', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'world-resume-'))
  writeState(dir, {
    run_id: 'r1', status: 'running', current_date: '2024-01-02',
    trading_dates: ['2024-01-02'], cursor: 0, bots: ['bot1'],
    memory_port: 0, started_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
    loop: 'research-loop',
  })
  const cfg = baseConfig({ loop: 'openclaw-pi', piOpenclawJson: '/x.json', piServerEntry: '/x.ts' })
  await assert.rejects(() => resumeWorld({ worldRoot: dir, config: cfg }), /loop/)
})
```

（如 resume.test.ts 没有 baseConfig，参考 Task A2 复制一份本地实现。）

- [ ] **Step 7: 跑全部 world 测试**

Run: `cd world && node --experimental-strip-types --test test/*.test.ts 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add world/src/state.ts world/src/run.ts world/test/state.test.ts world/test/resume.test.ts
git commit -m "$(cat <<'EOF'
feat(world): persist loop in state.json; refuse to resume across loop change

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A5: 文档化 world.example.yaml

**Files:**
- Modify: [world/config/world.example.yaml](../../../world/config/world.example.yaml)

- [ ] **Step 1: 加注释段**

在文件末尾追加：

```yaml

# === Loop（agent runtime）选择 ===
# 整 run 一种 loop，不混用。值：research-loop（默认）| openclaw-pi
# loop: research-loop

# === openclaw-pi loop 专用配置（只在 loop=openclaw-pi 时生效）===
#
# openclaw.json 源路径。world 启动时把它复制进 runs/<run>/rl-openclaw/openclaw.json，
# 并把 mcp.mem0 改写为本 run 的隔离 memory server URL。pi-server 从该副本读配置。
# 默认：/home/rooot/.openclaw/openclaw.json
# pi_openclaw_json: /path/to/openclaw.json
#
# openclaw 仓根路径。用于推断 pi_server_entry，默认 /home/rooot/.openclaw/openclaw
# openclaw_root: /path/to/openclaw
#
# pi-stdio-server 入口路径覆盖（测试/非常规布局用）。默认从 openclaw_root 推断：
#   <openclaw_root>/src/agents/agent_invest_pi_stdio_server.ts
# pi_server_entry: /path/to/agent_invest_pi_stdio_server.ts
```

- [ ] **Step 2: Commit**

```bash
git add world/config/world.example.yaml
git commit -m "$(cat <<'EOF'
docs(world): document loop selection and openclaw-pi config in example yaml

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A6: Stub pi-server + 烟雾测试（不依赖真实 openclaw）

**Files:**
- Create: `world/test/helpers/stubPiBotServer.ts`
- Create: `world/test/stubPiBotServer.test.ts`
- Create: `world/test/pi-smoke.test.ts`

- [ ] **Step 1: 写 stubPiBotServer.ts（与 stubBotServer 同协议，但接 --openclaw-json）**

创建 `world/test/helpers/stubPiBotServer.ts`：

```ts
#!/usr/bin/env -S node --experimental-strip-types
// 假的 agent_invest_pi_stdio_server。与 stubBotServer 同协议，但接 --openclaw-json 而非 --config。
// 行为由环境变量控制，与 stubBotServer 完全一致（STUB_CHAT_MODE / STUB_CHAT_DELAY_MS / STUB_REPLY_TEXT）。
import { createInterface } from 'node:readline'

const args = process.argv.slice(2)
function arg(name: string): string {
  const i = args.indexOf(name)
  return i >= 0 ? (args[i + 1] ?? '') : ''
}
const botId = arg('--bot-id') || 'botX'
const workspace = arg('--workspace') || ''
const openclawJson = arg('--openclaw-json') || ''
const mode = process.env.STUB_CHAT_MODE || 'reply'
const delayMs = Number(process.env.STUB_CHAT_DELAY_MS || '0')
const replyText = process.env.STUB_REPLY_TEXT ?? 'stub pi reply'

if (!openclawJson) { process.stderr.write('stubPiBotServer: --openclaw-json required\n'); process.exit(2) }

const out = (obj: Record<string, unknown>) => process.stdout.write(JSON.stringify(obj) + '\n')
out({ method: 'server.ready', params: { bot_id: botId, workspace, model: 'stub-pi', pid: process.pid } })

const rl = createInterface({ input: process.stdin })
rl.on('line', (line) => {
  const t = line.trim()
  if (!t) return
  const req = JSON.parse(t) as { id?: unknown; method?: string; params?: Record<string, unknown> }
  const id = req.id
  if (req.method === 'ping') { out({ id, result: { pong: true, bot_id: botId, workspace, model: 'stub-pi' } }); return }
  if (req.method === 'shutdown') { out({ id, result: { ok: true } }); setTimeout(() => process.exit(0), 10); return }
  if (req.method === 'chat') {
    const message = String(req.params?.message ?? '')
    const sessionKey = String(req.params?.session_key ?? '')
    if (mode === 'exit') { setTimeout(() => process.exit(1), 5); return }
    if (mode === 'hang') return
    setTimeout(() => {
      if (mode === 'error') { out({ id, error: { code: -32000, message: 'stub pi chat error' } }); return }
      out({ method: 'message.done', params: { text: replyText } })
      out({
        id,
        result: {
          reply: replyText,
          session_id: `stub-pi-${sessionKey}`,
          assistant_messages: [{ role: 'assistant', content: replyText }],
          tool_trace: [],
          usage: message.length,
          iterations: 1,
          truncated_by_iterations: false,
        },
      })
    }, delayMs)
    return
  }
  if (id !== undefined) out({ id, error: { code: -32601, message: `unknown method ${req.method}` } })
})
```

- [ ] **Step 2: 写 stub 自身契约测试 `world/test/stubPiBotServer.test.ts`**

参考 [world/test/stubBotServer.test.ts](../../../world/test/stubBotServer.test.ts) 复制并改造：

```ts
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { JsonRpcStdioClient } from '../src/jsonrpc.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const STUB = join(HERE, 'helpers', 'stubPiBotServer.ts')

function start(env: Record<string, string> = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'pi-stub-'))
  const oc = join(dir, 'openclaw.json'); writeFileSync(oc, '{}\n')
  const child = spawn(process.execPath, ['--experimental-strip-types', STUB, '--bot-id', 'bot7', '--workspace', '/ws/bot7', '--openclaw-json', oc], { stdio: ['pipe', 'pipe', 'inherit'], env: { ...process.env, ...env } })
  return { child, client: new JsonRpcStdioClient(child.stdin!, child.stdout!), oc }
}

test('pi stub emits server.ready, ping, chat → message.done + result', async () => {
  const { child, client } = start({ STUB_REPLY_TEXT: 'pi-hi' })
  const ready = await client.waitFor(n => n.method === 'server.ready', 2000)
  assert.equal(ready.params.bot_id, 'bot7')
  assert.equal((await client.request('ping', {}) as { pong: boolean }).pong, true)
  const done = client.waitFor(n => n.method === 'message.done', 2000)
  const r = await client.request('chat', { message: 'hi', session_key: 'tr-1' }) as { reply: string; session_id: string }
  assert.equal(r.reply, 'pi-hi')
  assert.equal(r.session_id, 'stub-pi-tr-1')
  assert.equal((await done).params.text, 'pi-hi')
  child.kill()
})

test('pi stub refuses to start without --openclaw-json', async () => {
  const child = spawn(process.execPath, ['--experimental-strip-types', STUB, '--bot-id', 'b', '--workspace', '/w'], { stdio: ['pipe', 'pipe', 'pipe'] })
  const code = await new Promise<number | null>(res => child.on('exit', c => res(c)))
  assert.equal(code, 2)
})
```

- [ ] **Step 3: 跑 stub 测试**

Run: `cd world && node --experimental-strip-types --test test/stubPiBotServer.test.ts 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 4: 写 pi-smoke.test.ts — 用 stub 跑 runWorld**

参考 world 已有的 run / smoke 测试形态（应该已经有用 stubBotServer 跑 runWorld 的样板）。具体步骤：

1. 用 `mkdtempSync` 建一个 worldRoot
2. 写一个最小 calendar.json + 1 个 quotes.json
3. 写一个 workspace-bot1 目录（至少有 IDENTITY.md 才能通过 shadow 复制）
4. 写一个 openclaw.json 文件给 pi-server 用
5. 构造 WorldConfig（loop: openclaw-pi，piServerEntry 指向 stubPiBotServer.ts）
6. 调用 `runWorld({ worldRoot, config, runId: 'r1' })`
7. 断言 `runs/r1/2024-01-02/bot1/reply.json` 写出来了且包含 `stub pi reply`

伪代码骨架（按你 world 现有测试风格补全 import 和工具函数）：

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runWorld } from '../src/run.ts'
import type { WorldConfig } from '../src/config.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const STUB_PI = join(HERE, 'helpers', 'stubPiBotServer.ts')

test('runWorld with loop=openclaw-pi drives stub pi-server for 1 trading day', async () => {
  const root = mkdtempSync(join(tmpdir(), 'pi-smoke-'))
  // calendar + quotes
  const cal = join(root, 'calendar.json'); writeFileSync(cal, JSON.stringify({ trading_days: ['2024-01-02'] }))
  mkdirSync(join(root, 'days/2024-01-02'), { recursive: true })
  writeFileSync(join(root, 'days/2024-01-02/quotes.json'), '{}')
  // workspace
  const ws = join(root, 'workspace-bot1'); mkdirSync(ws, { recursive: true })
  writeFileSync(join(ws, 'IDENTITY.md'), '# bot1\n')
  // openclaw.json for pi
  const ocJson = join(root, 'openclaw.json'); writeFileSync(ocJson, '{"mcp":{}}\n')

  const cfg: WorldConfig = {
    researchLoopTs: '/unused',
    workspaceRoot: root,
    bots: ['bot1'],
    replay: { from: '2024-01-02', to: '2024-01-02' },
    calendar: cal,
    concurrency: 1,
    perBotTimeoutSeconds: 30,
    rlConfigBase: join(root, 'base.json'),
    rlOpenclawDir: undefined,
    shadowInclude: ['IDENTITY.md'],
    loop: 'openclaw-pi',
    piOpenclawJson: ocJson,
    openclawRoot: '/unused',
    piServerEntry: STUB_PI,
  }
  writeFileSync(cfg.rlConfigBase, '{}\n')

  await runWorld({ worldRoot: root, config: cfg, runId: 'r1' })

  const reply = JSON.parse(readFileSync(join(root, 'runs/r1/2024-01-02/bot1/reply.json'), 'utf8'))
  assert.match(reply.reply, /stub pi reply/)
  // 校验 mcp.mem0 被 patch 到 memory URL
  const patched = JSON.parse(readFileSync(join(root, 'runs/r1/rl-openclaw/openclaw.json'), 'utf8'))
  assert.match(String(patched.mcp.mem0), /^http:\/\//)
})
```

如果你 world 已有同形态测试（用 stubBotServer 跑 runWorld 的），把它当模板对齐细节（消息格式、quotes 内容是否够、shadow include 等）。

- [ ] **Step 5: 跑 smoke 测试**

Run: `cd world && node --experimental-strip-types --test test/pi-smoke.test.ts 2>&1 | tail -30`
Expected: PASS。如果失败请贴日志，常见原因：消息渲染需要更复杂的 quotes/overview 文件。按现有 run.test.ts 里的同款 fixture 补齐。

- [ ] **Step 6: 跑全套 world 测试**

Run: `cd world && node --experimental-strip-types --test test/*.test.ts 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add world/test/helpers/stubPiBotServer.ts world/test/stubPiBotServer.test.ts world/test/pi-smoke.test.ts
git commit -m "$(cat <<'EOF'
test(world): stub pi-stdio-server + smoke test for loop=openclaw-pi

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase B: openclaw 端 — agent_invest_pi_stdio_server.ts

工作目录切换到 `/home/rooot/.openclaw/openclaw`。这是另一个 git 仓，独立 commit。

### Task B1: 调研 runEmbeddedPiAgent 的最小参数集

**Files:** read-only

- [ ] **Step 1: 读 RunEmbeddedPiAgentParams 必填字段**

Run: `cat /home/rooot/.openclaw/openclaw/src/agents/pi-embedded-runner/run/params.ts | head -200`

记下哪些字段是非可选（没 `?`），以及 `prompt`/`sessionId`/`sessionKey`/`agentId`/`workspaceDir`/`config`/`timeoutMs`/`runId` 的具体名字与类型。

- [ ] **Step 2: 读现有调用方理解最小调用形态**

Run: `cat /home/rooot/.openclaw/openclaw/src/agents/research-loop-runner/run.ts` —— 这是 research-loop 走 HTTP 路径，不是 pi，但展示了同一类参数构造。

Run: `grep -nE "runEmbeddedPiAgent\(" /home/rooot/.openclaw/openclaw/src/agents --include="*.ts" -r | grep -v test | head -5` —— 找一个真实 pi 调用，比如 cron / command/delivery.ts。

读出来的最小必需参数集 → 写到一段注释里，留到 Task B3 拿。

- [ ] **Step 3: 读 EmbeddedPiRunResult 的结构（要 map 到 chat 协议）**

Run: `cat /home/rooot/.openclaw/openclaw/src/agents/pi-embedded-runner/types.ts | head -120`

记下 `payloads`/`meta` 里能拿到 reply、usage、session id、是否截断的字段路径。

- [ ] **Step 4: 读 openclaw config loader**

Run: `find /home/rooot/.openclaw/openclaw/src/config -name "*.ts" | xargs grep -lE "^export (async )?function load" | head -5`

记下"给定一个 openclaw.json 路径，怎么 load 出 `OpenClawConfig`"的具体 API。

- [ ] **Step 5: 记下笔记（暂存）**

把 4 个步骤的结论以注释形式写到 `world/.claude/plans/pi-runtime-surface-notes.md`（临时文件，不 commit），后续 Task B3 直接拿。

（这一步不需要 commit；只是为下一步铺路。）

---

### Task B2: pi-stdio-server 骨架 — CLI 解析、server.ready、ping、shutdown

**Files:**
- Create: `/home/rooot/.openclaw/openclaw/src/agents/agent_invest_pi_stdio_server.ts`
- Create: `/home/rooot/.openclaw/openclaw/test/agent_invest_pi_stdio_server.test.ts`

- [ ] **Step 1: 写骨架文件（含文件头注释）**

创建 `/home/rooot/.openclaw/openclaw/src/agents/agent_invest_pi_stdio_server.ts`：

```ts
#!/usr/bin/env -S node --experimental-strip-types
/**
 * agent_invest_pi_stdio_server
 *
 * 用途：agent_invest_lab 的 `world` 系统用的 stdio JSON-RPC bot server，
 *      薄薄一层包装 openclaw 的 `runEmbeddedPiAgent`。
 * 所有者：agent_invest_lab（不是 openclaw 产品组件）。
 * 不进 openclaw 的 dist、不进 docs、不暴露给 openclaw 用户。
 * 与 openclaw 的 dashboard/feishu/cron/loop-mode.json 等机制无关 —— loop 选择
 * 已经由 world.yaml 决定，本 server 强制走 pi。
 *
 * 若需求驱动力来自 openclaw 主产品（比如 dashboard 想 spawn pi-stdio-server），
 * 请另开一个 openclaw 自己的 server.ts、不要复用本文件。
 *
 * 协议：与 research-loop/ts/src/server.ts 完全一致（NDJSON, server.ready,
 *      ping, chat, shutdown, research.*/tool.*/message.done/log）。
 *
 * 用法：node --experimental-strip-types agent_invest_pi_stdio_server.ts \
 *           --bot-id <id> --workspace <abs-dir> --openclaw-json <abs-path>
 */

import { createInterface } from "node:readline";
import { existsSync } from "node:fs";

function argOf(name: string): string {
  const args = process.argv.slice(2);
  const i = args.indexOf(name);
  return i >= 0 ? (args[i + 1] ?? "") : "";
}

const botId = argOf("--bot-id");
const workspace = argOf("--workspace");
const openclawJson = argOf("--openclaw-json");

if (!botId || !workspace || !openclawJson) {
  process.stderr.write(
    "agent_invest_pi_stdio_server: --bot-id, --workspace, --openclaw-json are required\n",
  );
  process.exit(2);
}
if (!existsSync(openclawJson)) {
  process.stderr.write(`agent_invest_pi_stdio_server: openclaw-json not found: ${openclawJson}\n`);
  process.exit(2);
}
if (!existsSync(workspace)) {
  process.stderr.write(`agent_invest_pi_stdio_server: workspace not found: ${workspace}\n`);
  process.exit(2);
}

function writeJson(obj: Record<string, unknown>): void {
  process.stdout.write(`${JSON.stringify(obj)}\n`);
}
function notify(method: string, params: Record<string, unknown>): void {
  writeJson({ method, params });
}
function respond(id: unknown, result: Record<string, unknown>): void {
  writeJson({ id, result });
}
function respondError(id: unknown, code: number, message: string): void {
  writeJson({ id, error: { code, message } });
}

// 启动通知。chat 路径在 Task B3 接入；这里先回 stub 表明 server 起来了。
notify("server.ready", { bot_id: botId, workspace, model: "openclaw-pi", pid: process.pid });

let shuttingDown = false;
async function handleShutdown(id: unknown): Promise<void> {
  shuttingDown = true;
  respond(id, { ok: true });
  setTimeout(() => process.exit(0), 50);
}

const rl = createInterface({ input: process.stdin });
rl.on("line", (line) => {
  const t = line.trim();
  if (!t) return;
  let req: { id?: unknown; method?: string; params?: Record<string, unknown> };
  try {
    req = JSON.parse(t);
  } catch {
    return;
  }
  const id = req.id;
  if (shuttingDown) {
    if (id !== undefined) respondError(id, -32000, "server is shutting down");
    return;
  }
  if (req.method === "ping") {
    respond(id, { pong: true, bot_id: botId, workspace, model: "openclaw-pi" });
    return;
  }
  if (req.method === "shutdown") {
    void handleShutdown(id);
    return;
  }
  if (req.method === "chat") {
    // 实现见 Task B3
    if (id !== undefined) respondError(id, -32601, "chat: not implemented yet");
    return;
  }
  if (id !== undefined) respondError(id, -32601, `unknown method ${req.method}`);
});

process.on("SIGINT", () => { void handleShutdown(undefined); });
process.on("SIGTERM", () => { void handleShutdown(undefined); });
```

- [ ] **Step 2: 写骨架测试**

创建 `/home/rooot/.openclaw/openclaw/test/agent_invest_pi_stdio_server.test.ts`：

（先确认 openclaw test runner —— `cat /home/rooot/.openclaw/openclaw/package.json | grep -A5 '"test"'` 看用 vitest 还是 node test。下面以 `node:test` 写，按实际 runner 调整 import。）

```ts
import { spawn } from "node:child_process";
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";

const SERVER = "/home/rooot/.openclaw/openclaw/src/agents/agent_invest_pi_stdio_server.ts";

function startServer() {
  const dir = mkdtempSync(join(tmpdir(), "pi-srv-"));
  const oc = join(dir, "openclaw.json"); writeFileSync(oc, "{}");
  const ws = dir;  // dir 自身已存在，做 workspace 够用
  const child = spawn(process.execPath, ["--experimental-strip-types", SERVER, "--bot-id", "bot1", "--workspace", ws, "--openclaw-json", oc], { stdio: ["pipe", "pipe", "inherit"] });
  return { child, dir };
}

function* lines(stream: NodeJS.ReadableStream) {
  return createInterface({ input: stream });
}

test("agent_invest_pi_stdio_server: emits server.ready, answers ping, exits on shutdown", async () => {
  const { child } = startServer();
  const rl = createInterface({ input: child.stdout! });

  const got = await new Promise<unknown>((res) => rl.once("line", (l) => res(JSON.parse(l))));
  assert.match(JSON.stringify(got), /server\.ready/);

  child.stdin!.write(JSON.stringify({ id: 1, method: "ping", params: {} }) + "\n");
  const pong = await new Promise<unknown>((res) => rl.once("line", (l) => res(JSON.parse(l))));
  assert.equal((pong as { result: { pong: boolean } }).result.pong, true);

  child.stdin!.write(JSON.stringify({ id: 2, method: "shutdown", params: {} }) + "\n");
  const ok = await new Promise<unknown>((res) => rl.once("line", (l) => res(JSON.parse(l))));
  assert.equal((ok as { result: { ok: boolean } }).result.ok, true);

  const code = await new Promise<number | null>((res) => child.on("exit", (c) => res(c)));
  assert.equal(code, 0);
});

test("agent_invest_pi_stdio_server: missing args exits with code 2", async () => {
  const child = spawn(process.execPath, ["--experimental-strip-types", SERVER, "--bot-id", "x"], { stdio: ["pipe", "pipe", "pipe"] });
  const code = await new Promise<number | null>((res) => child.on("exit", (c) => res(c)));
  assert.equal(code, 2);
});
```

- [ ] **Step 3: 跑测试**

Run: `cd /home/rooot/.openclaw/openclaw && node --experimental-strip-types --test test/agent_invest_pi_stdio_server.test.ts 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 4: Commit（在 .openclaw 仓里）**

```bash
cd /home/rooot/.openclaw/openclaw
git add src/agents/agent_invest_pi_stdio_server.ts test/agent_invest_pi_stdio_server.test.ts
git commit -m "$(cat <<'EOF'
feat(agents): add agent_invest_pi_stdio_server skeleton for agent_invest_lab

Stdio JSON-RPC server that wraps runEmbeddedPiAgent for the agent_invest_lab
world system. Skeleton implements server.ready, ping, shutdown; chat is
stubbed and lands in a follow-up commit.

Not part of openclaw product surface — see file header.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task B3: chat 接入 runEmbeddedPiAgent

**Files:**
- Modify: `/home/rooot/.openclaw/openclaw/src/agents/agent_invest_pi_stdio_server.ts`
- Modify/Create: `/home/rooot/.openclaw/openclaw/test/agent_invest_pi_stdio_server.test.ts`

- [ ] **Step 1: 写 chat 处理函数**

把 server.ts 里 `chat` 分支替换为：

```ts
if (req.method === "chat") {
  void handleChat(id, req.params ?? {});
  return;
}
```

并在文件中新增：

```ts
import { runEmbeddedPiAgent } from "./pi-embedded-runner.js";
import { loadOpenClawConfig } from "../config/load.js"; // 占位：根据 Task B1 调研结果填写真实模块路径
import { randomUUID } from "node:crypto";

let configPromise: Promise<unknown> | null = null;
async function getConfig(): Promise<unknown> {
  if (!configPromise) configPromise = loadOpenClawConfig(openclawJson);
  return configPromise;
}

interface ChatParams {
  message: string;
  session_key?: string;
  history?: unknown[];
  channel?: string;
  metadata?: Record<string, unknown>;
}

async function handleChat(id: unknown, raw: Record<string, unknown>): Promise<void> {
  const params = raw as ChatParams;
  const message = String(params.message ?? "");
  if (!message) { respondError(id, -32602, "params.message is required"); return; }
  const sessionKey = params.session_key ?? botId;
  const channel = params.channel;
  const metadata = (params.metadata ?? {}) as Record<string, unknown>;
  const sessionId = `pi-${botId}-${randomUUID().slice(0, 12)}`;
  const runId = `world-${Date.now()}`;
  notify("research.started", { bot_id: botId, session_key: sessionKey, session_id: sessionId, topic: message.slice(0, 48) });

  try {
    const config = await getConfig();
    const result = await runEmbeddedPiAgent({
      // ↓ 字段按 Task B1 调研结果调整。下面是占位骨架；不确定的字段先用最小可运行集合。
      sessionId,
      sessionKey,
      agentId: botId,
      workspaceDir: workspace,
      prompt: message,
      trigger: "user",
      timeoutMs: 30 * 60 * 1000,
      config,
      runId,
      messageChannel: channel,
      senderId: metadata.sender_id as string | undefined,
      senderName: metadata.sender_name as string | undefined,
    } as Parameters<typeof runEmbeddedPiAgent>[0]);

    // 映射到 research-loop chat 协议
    const reply = (result.payloads ?? []).map(p => (typeof p === "object" && p && "text" in p ? String((p as { text: unknown }).text) : "")).filter(Boolean).join("\n");
    const meta = (result as { meta?: { agentMeta?: { usage?: { total?: number } }; stopReason?: string } }).meta ?? {};
    const usage = meta.agentMeta?.usage?.total ?? 0;
    const truncated = meta.stopReason === "tool_calls" || meta.stopReason === "length";
    notify("message.done", { bot_id: botId, session_key: sessionKey, session_id: sessionId, text: reply });
    notify("research.progress", { bot_id: botId, session_key: sessionKey, session_id: sessionId, event: "done" });
    respond(id, {
      reply,
      session_id: sessionId,
      assistant_messages: [{ role: "assistant", content: reply }],
      tool_trace: [],
      usage,
      iterations: 1,
      truncated_by_iterations: Boolean(truncated),
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    notify("log", { level: "error", message: msg });
    respondError(id, -32000, msg);
  }
}
```

**注意**：

- `import { runEmbeddedPiAgent } from "./pi-embedded-runner.js"` —— 实际路径来自 Task B1 调研。openclaw 的 import 用 `.js` 后缀（即便源文件是 `.ts`），看 [agents/runtime-config.ts](file:///home/rooot/.openclaw/openclaw/src/agents/runtime-config.ts) 的 import 写法照搬。
- `loadOpenClawConfig` 是占位 —— Task B1 应该已经记下了真实的 loader 名字和路径，按那个改。
- `runEmbeddedPiAgent` 的参数表很大；很多字段是 optional，先填 8 个核心字段（agentId/sessionId/sessionKey/workspaceDir/prompt/trigger/timeoutMs/config）+ 必要时补 runId/messageChannel/sender*。`as Parameters<...>[0]` 是为了在没补齐所有 optional 字段时通过 TS 检查；调研完成后改成显式构造而不要 cast。
- 工具调用 / 阶段事件目前是单条 `research.progress` `done`，不细分；丰富版在 Task B4。

- [ ] **Step 2: 写 chat 单元测试 — 用 mock 跑通 result mapping**

新增测试用例到 `/home/rooot/.openclaw/openclaw/test/agent_invest_pi_stdio_server.test.ts`：

由于 `runEmbeddedPiAgent` 真实跑需要 LLM，本测试**只覆盖**：

(a) chat 在 message 缺失时返回 -32602
(b) 真实 chat 走 opt-in：环境变量 `WORLD_PI_E2E=1` 且本机有可用 openclaw.json 时才跑。

```ts
test("chat: rejects empty message", async () => {
  const { child } = startServer();
  const rl = createInterface({ input: child.stdout! });
  await new Promise(res => rl.once("line", res)); // server.ready

  child.stdin!.write(JSON.stringify({ id: 7, method: "chat", params: {} }) + "\n");
  const r = await new Promise<{ error: { code: number; message: string } }>(res => rl.once("line", l => res(JSON.parse(l))));
  assert.equal(r.error.code, -32602);
  child.kill();
});

test("chat: e2e against real openclaw.json (opt-in)", { skip: process.env.WORLD_PI_E2E !== "1" }, async () => {
  // 用真实 /home/rooot/.openclaw/openclaw.json 起 server，发个"say hi"，断言 reply 非空
  // 注意：本测试会调用真实 LLM。
  const oc = process.env.WORLD_PI_E2E_OPENCLAW_JSON || "/home/rooot/.openclaw/openclaw.json";
  const ws = mkdtempSync(join(tmpdir(), "pi-e2e-ws-"));
  writeFileSync(join(ws, "IDENTITY.md"), "# test agent\n");
  const child = spawn(process.execPath, ["--experimental-strip-types", SERVER, "--bot-id", "bot1", "--workspace", ws, "--openclaw-json", oc], { stdio: ["pipe", "pipe", "inherit"] });
  const rl = createInterface({ input: child.stdout! });
  await new Promise(res => rl.once("line", res));
  child.stdin!.write(JSON.stringify({ id: 1, method: "chat", params: { message: "say 'hi'" } }) + "\n");
  // 累计读到带 id=1 的回复
  let result: { reply: string } | null = null;
  for await (const line of rl) {
    const m = JSON.parse(line) as { id?: number; result?: { reply?: string } };
    if (m.id === 1 && m.result?.reply) { result = m.result as { reply: string }; break; }
  }
  assert.ok(result && result.reply.length > 0);
  child.stdin!.write(JSON.stringify({ id: 2, method: "shutdown", params: {} }) + "\n");
});
```

- [ ] **Step 3: 跑非 e2e 测试**

Run: `cd /home/rooot/.openclaw/openclaw && node --experimental-strip-types --test test/agent_invest_pi_stdio_server.test.ts 2>&1 | tail -20`
Expected: PASS（含 chat: rejects empty message；e2e SKIP）

- [ ] **Step 4: 类型检查**

Run: `cd /home/rooot/.openclaw/openclaw && (npx --no-install tsc --noEmit src/agents/agent_invest_pi_stdio_server.ts 2>&1 || true) | head -40`

如果有类型错误，常见原因：
- `runEmbeddedPiAgent` 的 import 路径写错 → 按 Task B1 调研改
- `config` 没传 `OpenClawConfig` 类型 → 把 loader 返回类型 cast 上
- `payloads` 形状不对 → 看 `EmbeddedPiRunResult` 实际类型修

修到无错。

- [ ] **Step 5: Opt-in e2e 跑一遍（手动验证）**

Run: `cd /home/rooot/.openclaw/openclaw && WORLD_PI_E2E=1 node --experimental-strip-types --test test/agent_invest_pi_stdio_server.test.ts 2>&1 | tail -40`

如果走通：报告 reply 内容、耗时。如果失败：贴日志、看是不是 model/MCP 配置问题（这部分属于 openclaw 配置层问题，不需要在本 plan 范畴内解决）。

- [ ] **Step 6: Commit**

```bash
cd /home/rooot/.openclaw/openclaw
git add src/agents/agent_invest_pi_stdio_server.ts test/agent_invest_pi_stdio_server.test.ts
git commit -m "$(cat <<'EOF'
feat(agents): wire chat in agent_invest_pi_stdio_server to runEmbeddedPiAgent

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task B4: 丰富 chat 期间事件通知

**Files:**
- Modify: `/home/rooot/.openclaw/openclaw/src/agents/agent_invest_pi_stdio_server.ts`
- Modify: `/home/rooot/.openclaw/openclaw/test/agent_invest_pi_stdio_server.test.ts`

`runEmbeddedPiAgent` 内部有事件流（hook / subscribe 机制）。Task B1 调研时应该看到了 — 比如 `AgentInternalEvent` / `pi-embedded-subscribe` / hook-runner。具体接口因 openclaw 版本不同，请按现状对齐。

最小做法：先**不订阅内部事件**，只在 chat 开始/结束发 `research.started` 和 `message.done`（已在 B3 完成）。这一 Task 是**可选增强** —— 让 world 的日志更可读。

如果时间不够或接口复杂，可以直接跳到 Phase C，留 TODO 在文件头注释里。

- [ ] **Step 1: 探一下 pi 的 subscribe 接口**

Run: `grep -rE "subscribe|onEvent|EventEmitter" /home/rooot/.openclaw/openclaw/src/agents/pi-embedded-subscribe.ts 2>/dev/null | head -20`

如果接口可用，加上事件订阅，把 tool_call/tool_result/phase 翻译成对应通知。
如果接口私有/复杂，跳过本 Task。

- [ ] **Step 2: （视实现）补测试**

如果做了订阅，加测试断言至少一条 `tool.call` 或 `research.progress event=phase` 通知被发出（用 mock 或 opt-in 真实跑）。

- [ ] **Step 3: Commit（若有改动）**

```bash
cd /home/rooot/.openclaw/openclaw
git add src/agents/agent_invest_pi_stdio_server.ts test/agent_invest_pi_stdio_server.test.ts
git commit -m "$(cat <<'EOF'
feat(agents): translate pi internal events to research-loop-style notifications

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase C: 集成验证

### Task C1: 真实 pi-server + agent_invest_lab world 端到端（opt-in）

**Files:** 不增不改文件，只跑命令验证

- [ ] **Step 1: 准备一个最小 world.yaml**

新建 `/tmp/world-pi-e2e.yaml`：

```yaml
research_loop_ts: /unused
workspace_root: /home/rooot/.openclaw
bots: [bot1]
replay:
  from: "2024-01-02"
  to:   "2024-01-02"
calendar: /home/rooot/agent_invest_lab/world/config/calendar.json
concurrency: 1
per_bot_timeout_seconds: 600
loop: openclaw-pi
pi_openclaw_json: /home/rooot/.openclaw/openclaw.json
openclaw_root: /home/rooot/.openclaw/openclaw
```

（如果 `calendar.json` 不存在，看 world/config/ 里实际文件名调整。）

- [ ] **Step 2: 确保 `days/2024-01-02/quotes.json` 存在**

Run: `ls /tmp/world-pi-e2e-runtime/days/2024-01-02/quotes.json 2>/dev/null || echo missing`

如果 missing，从 agent_invest_lab 现有的 days 数据复制或现造一个最小的（参考既有 research-loop run 的 quotes.json 形态）。

- [ ] **Step 3: 跑 run**

Run: `cd /home/rooot/agent_invest_lab && node --experimental-strip-types world/main.ts run --config /tmp/world-pi-e2e.yaml --world-dir /tmp/world-pi-e2e-runtime --run-id e2e-1 2>&1 | tail -60`

观察：

- 启动日志里 bot1 server ready
- 收到 research.started/message.done/done 通知
- 1 个交易日跑完
- 写出 `runs/e2e-1/2024-01-02/bot1/reply.json` 且 `reply` 非空

- [ ] **Step 4: 校验 isolation**

Run: `cat /tmp/world-pi-e2e-runtime/runs/e2e-1/rl-openclaw/openclaw.json | jq .mcp.mem0`
Expected: 一个 `http://127.0.0.1:NNNN` 形态的本 run 的 memory server URL（不是源文件里的 18096）

- [ ] **Step 5: 写一行手动测试结果到 commit 备注**

不必 commit 代码。手动记录验证通过。

---

### Task C2: 回归 — research-loop loop 仍然能跑

**Files:** 不增不改文件

- [ ] **Step 1: 用一个 research-loop 的 world.yaml 跑一遍**

Run: `cd /home/rooot/agent_invest_lab && node --experimental-strip-types world/main.ts run --config world/config/world.yaml --world-dir /tmp/world-rl-regress --run-id rl-1 2>&1 | tail -40`

（如果 world/config/world.yaml 不存在或不适合本机，按既有的回归测试方式跑。）

- [ ] **Step 2: 确认行为与改造前一致**

期望：bot 起得来、chat 完成、reply.json 写得对。

---

## Self-Review 自查清单

实施过程中，每完成一个 Task 后自查：

1. **Spec 覆盖**
   - [x] `loop: research-loop | openclaw-pi` yaml 开关 — Task A1
   - [x] `pi_openclaw_json` 字段与默认 — Task A1
   - [x] world 端 botServerArgv 按 loop 分支 — Task A2
   - [x] setup 按 loop 选 openclaw.json 源 — Task A3
   - [x] mcp.mem0 patch — Task A3
   - [x] WorldState 记 loop，resume 拒绝跨 loop — Task A4
   - [x] 文档化 yaml — Task A5
   - [x] world stub 烟雾 — Task A6
   - [x] pi-stdio-server 文件头三段标注（用途/所有者/不进 dist）— Task B2
   - [x] CLI 接 `--bot-id --workspace --openclaw-json` — Task B2
   - [x] server.ready / ping / chat / shutdown 协议 — Task B2 + B3
   - [x] 事件翻译（research.*/tool.*/message.done/log）— Task B3（最小）+ Task B4（增强）
   - [x] e2e opt-in — Task B3 + C1
   - [x] 回归 research-loop — Task C2

2. **占位扫描**：以上所有 step 都给了具体代码 / 命令；除 Task B1 的"按调研结果填"和 Task B4 的"视接口实现"两处需在执行时凑齐——这是因为 openclaw 内部 API 形态需先观察才能定，符合 plan 范畴。

3. **类型一致性**：`WorldConfig.loop`、`WorldState.loop` 都是 `'research-loop' | 'openclaw-pi'`；`botServerArgv` 第 4 参数命名统一改为 `loopConfigPath`（A2 改完后下游 A3 使用一致）。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-13-world-openclaw-pi-loop.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 每个 Task 派一个 fresh subagent，task 之间回到主对话 review 后再放下一个。隔离强、可中途调头。

**2. Inline Execution** — 在当前会话里按 plan 逐步跑，checkpoint review。

哪个？
