# agent_invest_lab 日度滚动金融世界系统 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `agent_invest_lab/` 里实现一个 TypeScript 的「日度滚动金融世界系统」：回放历史行情，按交易日日度滚动，每日并发把当天行情+交易指令喂给一批 agent（research-loop-ts），等所有 agent 跑完再翻篇；对 research-loop-ts 零改动。

**Architecture:** 一个 `world` CLI（长驻进程）+ 一个进程内的极简关键词记忆 HTTP 服务。`world run` 做：校验数据 → 起记忆服务 → 生成给 research-loop-ts 用的 rl-config → 为每个 bot 建影子 workspace → 起每个 bot 的 research-loop-ts JSON-RPC server → for 每个交易日：写 `state.json.current_date` → 渲染当天 chat message → 并发 `chat` → barrier 等齐 → 落盘 → cursor++ → 写 state.json → 跑完关进程写 summary.json。外部依赖（行情数据、交易引擎 MCP）通过约定的文件/配置契约接入，本计划不实现它们。

**Tech Stack:** Node >= 22.6（用 `node --experimental-strip-types` 跑 `.ts`，import 带 `.ts` 后缀，与 research-loop-ts 同栈）；`node:test` + `node:assert/strict` 做测试；`node:http` 做记忆服务；`yaml` 解析 world.yaml；无其它外部服务依赖（无 qdrant / mem0 / DB）。

**关键设计文档：** `docs/superpowers/specs/2026-05-11-agent-invest-lab-world-design.md`（已批准）。本计划是它的实现拆解。

---

## 文件结构

```
agent_invest_lab/
├── package.json                       # 同 research-loop-ts 风格：type=module, node>=22.6, scripts: start/test/check
├── tsconfig.json                      # 同 research-loop-ts：NodeNext + allowImportingTsExtensions + noEmit
├── .gitignore                         # /world/  /node_modules/  *.log  config/world.yaml
├── main.ts                            # CLI shim: #!/usr/bin/env -S node --experimental-strip-types ; import './src/cli.ts'
├── config/
│   ├── trading-rl-config.base.json    # research-loop.json 变体（无 yfinance/ttjj 等实时数据 MCP；dashboard 关；browser 关；memory-mem0 开）
│   └── world.example.yaml             # 示例 world 配置
├── src/
│   ├── cli.ts                         # 解析 argv，dispatch run/resume/status/stop/help
│   ├── config.ts                      # loadWorldConfig(path) → WorldConfig（解析 yaml + 默认值 + 校验）
│   ├── calendar.ts                    # loadCalendar(path) + computeTradingDates(cal, from, to)
│   ├── paths.ts                        # world 运行时目录/文件路径助手（全部相对一个 worldRoot）
│   ├── state.ts                       # WorldState 读写（snake_case JSON，原子写）
│   ├── concurrency.ts                 # mapWithConcurrency(items, limit, fn)
│   ├── jsonrpc.ts                     # JsonRpcStdioClient：行分隔 JSON-RPC over 子进程 stdio
│   ├── botServer.ts                   # BotServer：spawn research-loop-ts server.ts，await server.ready，ping/chat/shutdown
│   ├── shadowWorkspace.ts             # buildShadowWorkspace：从 workspace-botN 精选复制到 run 目录
│   ├── overview.ts                    # resolveOverview(worldRoot, date)：优先 overview.md，否则从 quotes.json 轻度生成，否则降级文案
│   ├── message.ts                     # renderDailyMessage(ctx)：世界规则块（完整/精简）+ 概览
│   ├── run.ts                         # runWorld / resumeWorld + setup/loop/teardown + botServerArgv
│   └── memory-server/
│       ├── tokenize.ts                # tokenize(text)：中英分词 + 停用词
│       ├── store.ts                   # MemoryStore：JSONL 存储，add（原样）/ search（关键词打分）
│       └── server.ts                  # createMemoryServer({store, getCurrentDate, port?}) → {port, url, close()}
├── test/
│   ├── helpers/stubBotServer.ts       # 假 research-loop-ts server（ping/chat/shutdown，行为可由 env 配）
│   ├── calendar.test.ts
│   ├── paths.test.ts
│   ├── config.test.ts
│   ├── state.test.ts
│   ├── concurrency.test.ts
│   ├── tokenize.test.ts
│   ├── memory-store.test.ts
│   ├── memory-server.test.ts
│   ├── jsonrpc.test.ts
│   ├── botServer.test.ts
│   ├── shadowWorkspace.test.ts
│   ├── overview.test.ts
│   ├── message.test.ts
│   ├── run.test.ts                    # e2e 小回放：2 stub bot × 2 交易日
│   ├── resume.test.ts
│   └── cli.test.ts                    # 仅测 arg 解析
└── docs/superpowers/{specs,plans}/...
```

**约定（所有任务遵守）：**
- `.ts` 之间 `import` 都带 `.ts` 后缀（Node strip-types 要求），如 `import { foo } from './bar.ts'`。
- 测试文件放 `test/`，文件名 `*.test.ts`；`npm test` = `node --experimental-strip-types --test test/*.test.ts`。
- 提交信息用英文 `feat:` / `test:` / `chore:` 前缀；每个任务结束提交一次。
- `worldRoot` = world 运行时根目录（生产环境是 `<projectDir>/world`，测试里是临时目录）。`paths.ts` 的所有函数都接收 `worldRoot`。

---

## Task 1: 项目脚手架

**Files:**
- Create: `package.json`
- Create: `tsconfig.json`
- Create: `.gitignore`
- Create: `main.ts`
- Create: `src/cli.ts`
- Create: `test/smoke.test.ts`

- [ ] **Step 1: 写 `package.json`**

```json
{
  "name": "agent-invest-lab-world",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "bin": { "world": "./main.ts" },
  "scripts": {
    "start": "node --experimental-strip-types main.ts",
    "test": "node --experimental-strip-types --test test/*.test.ts",
    "check": "node --experimental-strip-types main.ts --help && tsc -p tsconfig.json --noEmit"
  },
  "engines": { "node": ">=22.6.0" },
  "dependencies": { "yaml": "^2.8.4" },
  "devDependencies": { "@types/node": "^22.19.17", "typescript": "^5.9.3" }
}
```

- [ ] **Step 2: 写 `tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "allowImportingTsExtensions": true,
    "noEmit": true,
    "types": ["node"],
    "skipLibCheck": true
  },
  "include": ["src/**/*.ts", "test/**/*.ts", "main.ts"]
}
```

- [ ] **Step 3: 写 `.gitignore`**

```
/world/
/node_modules/
*.log
config/world.yaml
```

- [ ] **Step 4: 写 `main.ts`**

```ts
#!/usr/bin/env -S node --experimental-strip-types
import './src/cli.ts'
```

- [ ] **Step 5: 写 `src/cli.ts`（本任务只放占位 --help，后续 Task 18 补全）**

```ts
const HELP = `world — agent_invest_lab 日度滚动金融世界系统

用法:
  world run    --config <path> [--run-id <id>] [--world-dir <path>]
  world resume --config <path> [--world-dir <path>]
  world status [--world-dir <path>]
  world stop   [--world-dir <path>]
  world --help
`

const argv = process.argv.slice(2)
if (argv.length === 0 || argv.includes('-h') || argv.includes('--help')) {
  process.stdout.write(HELP)
  process.exit(0)
}
process.stderr.write(`未实现的命令: ${argv.join(' ')}\n`)
process.exit(2)
```

- [ ] **Step 6: 安装依赖**

Run: `npm install`
Expected: 生成 `node_modules/` 和 `package-lock.json`，无报错。

- [ ] **Step 7: 写冒烟测试 `test/smoke.test.ts`**

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'

test('smoke: test runner works', () => {
  assert.equal(1 + 1, 2)
})
```

- [ ] **Step 8: 跑测试 + check**

Run: `npm test && npm run check`
Expected: 测试通过；`world --help` 打印用法；`tsc --noEmit` 无错误。

- [ ] **Step 9: 提交**

```bash
git add package.json package-lock.json tsconfig.json .gitignore main.ts src/cli.ts test/smoke.test.ts
git commit -m "chore: scaffold agent_invest_lab world package"
```

---

## Task 2: 交易日历 — `src/calendar.ts`

**Files:**
- Create: `src/calendar.ts`
- Test: `test/calendar.test.ts`

- [ ] **Step 1: 写失败测试 `test/calendar.test.ts`**

```ts
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { loadCalendar, computeTradingDates } from '../src/calendar.ts'

function tmpFile(name: string, content: string): string {
  const dir = mkdtempSync(join(tmpdir(), 'cal-'))
  const p = join(dir, name)
  writeFileSync(p, content)
  return p
}

test('loadCalendar parses sorted trading_days', () => {
  const p = tmpFile('c.json', JSON.stringify({ trading_days: ['2024-01-02', '2024-01-03', '2024-01-04'] }))
  const cal = loadCalendar(p)
  assert.deepEqual(cal.tradingDays, ['2024-01-02', '2024-01-03', '2024-01-04'])
  rmSync(p, { force: true })
})

test('loadCalendar rejects unsorted or malformed dates', () => {
  const bad1 = tmpFile('b1.json', JSON.stringify({ trading_days: ['2024-01-03', '2024-01-02'] }))
  assert.throws(() => loadCalendar(bad1), /sorted/)
  const bad2 = tmpFile('b2.json', JSON.stringify({ trading_days: ['2024-1-2'] }))
  assert.throws(() => loadCalendar(bad2), /YYYY-MM-DD/)
  const bad3 = tmpFile('b3.json', JSON.stringify({ foo: 1 }))
  assert.throws(() => loadCalendar(bad3), /trading_days/)
})

test('computeTradingDates filters inclusive range', () => {
  const cal = { tradingDays: ['2024-01-02', '2024-01-03', '2024-01-04', '2024-01-05'] }
  assert.deepEqual(computeTradingDates(cal, '2024-01-03', '2024-01-04'), ['2024-01-03', '2024-01-04'])
  assert.deepEqual(computeTradingDates(cal, '2024-01-01', '2024-12-31'), cal.tradingDays)
})

test('computeTradingDates throws on empty result', () => {
  const cal = { tradingDays: ['2024-01-02'] }
  assert.throws(() => computeTradingDates(cal, '2025-01-01', '2025-12-31'), /no trading days/i)
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/calendar.test.ts`
Expected: FAIL（找不到模块 `../src/calendar.ts`）。

- [ ] **Step 3: 写 `src/calendar.ts`**

```ts
import { readFileSync } from 'node:fs'

export interface Calendar {
  tradingDays: string[]
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

export function loadCalendar(path: string): Calendar {
  const raw = JSON.parse(readFileSync(path, 'utf8')) as unknown
  const days = (raw as { trading_days?: unknown })?.trading_days
  if (!Array.isArray(days)) throw new Error(`calendar ${path}: missing array field "trading_days"`)
  const tradingDays: string[] = []
  for (const d of days) {
    if (typeof d !== 'string' || !ISO_DATE.test(d)) throw new Error(`calendar ${path}: bad date ${JSON.stringify(d)} (expected YYYY-MM-DD)`)
    if (tradingDays.length && d <= tradingDays[tradingDays.length - 1]) throw new Error(`calendar ${path}: trading_days must be strictly sorted ascending (offender: ${d})`)
    tradingDays.push(d)
  }
  if (tradingDays.length === 0) throw new Error(`calendar ${path}: trading_days is empty`)
  return { tradingDays }
}

export function computeTradingDates(cal: Calendar, from: string, to: string): string[] {
  if (!ISO_DATE.test(from) || !ISO_DATE.test(to)) throw new Error(`replay range must be YYYY-MM-DD (got ${from} .. ${to})`)
  if (from > to) throw new Error(`replay range start ${from} is after end ${to}`)
  const out = cal.tradingDays.filter(d => d >= from && d <= to)
  if (out.length === 0) throw new Error(`no trading days in calendar within range ${from} .. ${to}`)
  return out
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/calendar.test.ts`
Expected: PASS（4 个测试全过）。

- [ ] **Step 5: 提交**

```bash
git add src/calendar.ts test/calendar.test.ts
git commit -m "feat: trading calendar loader and date-range filter"
```

---

## Task 3: 路径助手 — `src/paths.ts`

**Files:**
- Create: `src/paths.ts`
- Test: `test/paths.test.ts`

- [ ] **Step 1: 写失败测试 `test/paths.test.ts`**

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as P from '../src/paths.ts'

const W = '/tmp/wr'
const R = 'run1'

test('path helpers compose under worldRoot', () => {
  assert.equal(P.stateFile(W), '/tmp/wr/state.json')
  assert.equal(P.calendarFile(W), '/tmp/wr/calendar.json')
  assert.equal(P.dayDir(W, '2024-03-15'), '/tmp/wr/days/2024-03-15')
  assert.equal(P.quotesFile(W, '2024-03-15'), '/tmp/wr/days/2024-03-15/quotes.json')
  assert.equal(P.overviewFile(W, '2024-03-15'), '/tmp/wr/days/2024-03-15/overview.md')
  assert.equal(P.eventsFile(W, '2024-03-15'), '/tmp/wr/days/2024-03-15/events.json')
  assert.equal(P.runDir(W, R), '/tmp/wr/runs/run1')
  assert.equal(P.runConfigFile(W, R), '/tmp/wr/runs/run1/trading-rl-config.json')
  assert.equal(P.shadowWorkspaceDir(W, R, 'bot7'), '/tmp/wr/runs/run1/workspaces/bot7')
  assert.equal(P.memoryStoreFile(W, R), '/tmp/wr/runs/run1/memory/store.jsonl')
  assert.equal(P.memoryRuntimeFile(W, R), '/tmp/wr/runs/run1/memory/runtime.json')
  assert.equal(P.botDayDir(W, R, '2024-03-15', 'bot7'), '/tmp/wr/runs/run1/2024-03-15/bot7')
  assert.equal(P.sentFile(W, R, '2024-03-15', 'bot7'), '/tmp/wr/runs/run1/2024-03-15/bot7/sent.md')
  assert.equal(P.replyFile(W, R, '2024-03-15', 'bot7'), '/tmp/wr/runs/run1/2024-03-15/bot7/reply.json')
  assert.equal(P.statusFile(W, R, '2024-03-15', 'bot7'), '/tmp/wr/runs/run1/2024-03-15/bot7/status.json')
  assert.equal(P.runLogFile(W, R), '/tmp/wr/runs/run1/run.log')
  assert.equal(P.summaryFile(W, R), '/tmp/wr/runs/run1/summary.json')
  assert.equal(P.stopFile(W, R), '/tmp/wr/runs/run1/STOP')
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/paths.test.ts`
Expected: FAIL（找不到 `../src/paths.ts`）。

- [ ] **Step 3: 写 `src/paths.ts`**

```ts
import { join } from 'node:path'

export const stateFile = (w: string) => join(w, 'state.json')
export const calendarFile = (w: string) => join(w, 'calendar.json')
export const dayDir = (w: string, date: string) => join(w, 'days', date)
export const quotesFile = (w: string, date: string) => join(dayDir(w, date), 'quotes.json')
export const overviewFile = (w: string, date: string) => join(dayDir(w, date), 'overview.md')
export const eventsFile = (w: string, date: string) => join(dayDir(w, date), 'events.json')
export const runDir = (w: string, runId: string) => join(w, 'runs', runId)
export const runConfigFile = (w: string, runId: string) => join(runDir(w, runId), 'trading-rl-config.json')
export const workspacesDir = (w: string, runId: string) => join(runDir(w, runId), 'workspaces')
export const shadowWorkspaceDir = (w: string, runId: string, bot: string) => join(workspacesDir(w, runId), bot)
export const memoryDir = (w: string, runId: string) => join(runDir(w, runId), 'memory')
export const memoryStoreFile = (w: string, runId: string) => join(memoryDir(w, runId), 'store.jsonl')
export const memoryRuntimeFile = (w: string, runId: string) => join(memoryDir(w, runId), 'runtime.json')
export const botDayDir = (w: string, runId: string, date: string, bot: string) => join(runDir(w, runId), date, bot)
export const sentFile = (w: string, runId: string, date: string, bot: string) => join(botDayDir(w, runId, date, bot), 'sent.md')
export const replyFile = (w: string, runId: string, date: string, bot: string) => join(botDayDir(w, runId, date, bot), 'reply.json')
export const statusFile = (w: string, runId: string, date: string, bot: string) => join(botDayDir(w, runId, date, bot), 'status.json')
export const runLogFile = (w: string, runId: string) => join(runDir(w, runId), 'run.log')
export const summaryFile = (w: string, runId: string) => join(runDir(w, runId), 'summary.json')
export const stopFile = (w: string, runId: string) => join(runDir(w, runId), 'STOP')
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/paths.test.ts`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/paths.ts test/paths.test.ts
git commit -m "feat: world runtime path helpers"
```

---

## Task 4: world 配置 — `src/config.ts`

**Files:**
- Create: `src/config.ts`
- Test: `test/config.test.ts`

- [ ] **Step 1: 写失败测试 `test/config.test.ts`**

```ts
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, isAbsolute } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { loadWorldConfig, DEFAULT_SHADOW_INCLUDE } from '../src/config.ts'

function tmpYaml(content: string): string {
  const dir = mkdtempSync(join(tmpdir(), 'wcfg-'))
  const p = join(dir, 'world.yaml')
  writeFileSync(p, content)
  return p
}

test('loadWorldConfig parses required fields and applies defaults', () => {
  const p = tmpYaml(`
research_loop_ts: /opt/rl
workspace_root: /opt/ws
bots: [bot1, bot7]
replay: { from: "2024-01-02", to: "2024-06-28" }
`)
  const c = loadWorldConfig(p)
  assert.equal(c.researchLoopTs, '/opt/rl')
  assert.equal(c.workspaceRoot, '/opt/ws')
  assert.deepEqual(c.bots, ['bot1', 'bot7'])
  assert.deepEqual(c.replay, { from: '2024-01-02', to: '2024-06-28' })
  // calendar 默认相对 world.yaml 所在目录
  assert.equal(c.calendar, join(dirname(p), 'calendar.json'))
  assert.equal(isAbsolute(c.calendar), true)
  assert.equal(c.concurrency, 4)
  assert.equal(c.perBotTimeoutSeconds, 1200)
  assert.equal(c.rlOpenclawDir, '/home/rooot/.openclaw')
  assert.deepEqual(c.shadowInclude, DEFAULT_SHADOW_INCLUDE)
  rmSync(p, { force: true })
})

test('loadWorldConfig honors overrides', () => {
  const p = tmpYaml(`
research_loop_ts: /opt/rl
workspace_root: /opt/ws
bots: [bot1]
replay: { from: "2024-01-02", to: "2024-01-03" }
calendar: /data/cal.json
concurrency: 8
per_bot_timeout_seconds: 600
rl_config_base: /cfgs/base.json
rl_openclaw_dir: /tmp/oc
shadow_include: [SOUL.md, skills]
`)
  const c = loadWorldConfig(p)
  assert.equal(c.calendar, '/data/cal.json')
  assert.equal(c.concurrency, 8)
  assert.equal(c.perBotTimeoutSeconds, 600)
  assert.equal(c.rlConfigBase, '/cfgs/base.json')
  assert.equal(c.rlOpenclawDir, '/tmp/oc')
  assert.deepEqual(c.shadowInclude, ['SOUL.md', 'skills'])
  rmSync(p, { force: true })
})

test('loadWorldConfig rejects missing/invalid required fields', () => {
  assert.throws(() => loadWorldConfig(tmpYaml(`workspace_root: /x\nbots: [bot1]\nreplay: {from: "2024-01-02", to: "2024-01-03"}`)), /research_loop_ts/)
  assert.throws(() => loadWorldConfig(tmpYaml(`research_loop_ts: /r\nworkspace_root: /x\nbots: []\nreplay: {from: "2024-01-02", to: "2024-01-03"}`)), /bots/)
  assert.throws(() => loadWorldConfig(tmpYaml(`research_loop_ts: /r\nworkspace_root: /x\nbots: [bot1]\nreplay: {from: "2024-13-99", to: "2024-01-03"}`)), /from/)
  assert.throws(() => loadWorldConfig(tmpYaml(`research_loop_ts: /r\nworkspace_root: /x\nbots: [bot1]\nreplay: {from: "2024-02-02", to: "2024-01-03"}`)), /after/)
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/config.test.ts`
Expected: FAIL（找不到 `../src/config.ts`）。

- [ ] **Step 3: 写 `src/config.ts`**

```ts
import { readFileSync } from 'node:fs'
import { dirname, isAbsolute, resolve } from 'node:path'
import { parse as parseYaml } from 'yaml'

export const DEFAULT_SHADOW_INCLUDE = [
  'IDENTITY.md', 'SOUL.md', 'AGENTS.md', 'USER.md',
  'METHODOLOGY.md', 'RESEARCH.md', 'MEMORY.md',
  'EQUIPPED_SKILLS.md', 'TOOLS.md', 'skills', 'config',
]

export interface WorldConfig {
  researchLoopTs: string
  workspaceRoot: string
  bots: string[]
  replay: { from: string; to: string }
  calendar: string
  concurrency: number
  perBotTimeoutSeconds: number
  rlConfigBase: string
  rlOpenclawDir: string
  shadowInclude: string[]
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

function reqString(obj: Record<string, unknown>, key: string): string {
  const v = obj[key]
  if (typeof v !== 'string' || v.trim() === '') throw new Error(`world config: "${key}" is required and must be a non-empty string`)
  return v
}

function validIsoDate(s: string, label: string): string {
  if (!ISO_DATE.test(s)) throw new Error(`world config: "${label}" must be YYYY-MM-DD (got ${JSON.stringify(s)})`)
  const d = new Date(s + 'T00:00:00Z')
  if (Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== s) throw new Error(`world config: "${label}" is not a real date (${s})`)
  return s
}

function resolveMaybe(base: string, p: string): string {
  return isAbsolute(p) ? p : resolve(base, p)
}

export function loadWorldConfig(path: string): WorldConfig {
  const text = readFileSync(path, 'utf8')
  const raw = (parseYaml(text) ?? {}) as Record<string, unknown>
  const baseDir = dirname(resolve(path))

  const researchLoopTs = reqString(raw, 'research_loop_ts')
  const workspaceRoot = reqString(raw, 'workspace_root')

  const botsRaw = raw.bots
  if (!Array.isArray(botsRaw) || botsRaw.length === 0 || !botsRaw.every(b => typeof b === 'string' && b.trim())) {
    throw new Error('world config: "bots" must be a non-empty array of bot ids (e.g. [bot1, bot7])')
  }
  const bots = botsRaw as string[]

  const replayRaw = (raw.replay ?? {}) as Record<string, unknown>
  const from = validIsoDate(reqString(replayRaw, 'from'), 'replay.from')
  const to = validIsoDate(reqString(replayRaw, 'to'), 'replay.to')
  if (from > to) throw new Error(`world config: replay.from (${from}) is after replay.to (${to})`)

  const calendar = typeof raw.calendar === 'string' && raw.calendar.trim()
    ? resolveMaybe(baseDir, raw.calendar)
    : resolveMaybe(baseDir, 'calendar.json')
  const concurrency = typeof raw.concurrency === 'number' && raw.concurrency >= 1 ? Math.floor(raw.concurrency) : 4
  const perBotTimeoutSeconds = typeof raw.per_bot_timeout_seconds === 'number' && raw.per_bot_timeout_seconds > 0 ? Math.floor(raw.per_bot_timeout_seconds) : 1200
  const rlConfigBase = typeof raw.rl_config_base === 'string' && raw.rl_config_base.trim()
    ? resolveMaybe(baseDir, raw.rl_config_base)
    : resolveMaybe(baseDir, '../config/trading-rl-config.base.json')
  const rlOpenclawDir = typeof raw.rl_openclaw_dir === 'string' && raw.rl_openclaw_dir.trim() ? raw.rl_openclaw_dir : '/home/rooot/.openclaw'
  const shadowInclude = Array.isArray(raw.shadow_include) && raw.shadow_include.every(x => typeof x === 'string')
    ? (raw.shadow_include as string[])
    : DEFAULT_SHADOW_INCLUDE

  return { researchLoopTs, workspaceRoot, bots, replay: { from, to }, calendar, concurrency, perBotTimeoutSeconds, rlConfigBase, rlOpenclawDir, shadowInclude }
}
```

> 说明：`rlConfigBase` 默认相对 world.yaml 的目录指向 `../config/trading-rl-config.base.json`（仓库里 `config/world.example.yaml` 与 `config/trading-rl-config.base.json` 同目录，用户复制 example 到 `config/world.yaml` 后该默认值即正确）。

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/config.test.ts`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/config.ts test/config.test.ts
git commit -m "feat: world.yaml config loader with defaults and validation"
```

---

## Task 5: 世界状态 — `src/state.ts`

**Files:**
- Create: `src/state.ts`
- Test: `test/state.test.ts`

- [ ] **Step 1: 写失败测试 `test/state.test.ts`**

```ts
import { mkdtempSync, rmSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { writeState, readState, stateExists, type WorldState } from '../src/state.ts'

function tmpWorldRoot(): string {
  return mkdtempSync(join(tmpdir(), 'wstate-'))
}

const sample: WorldState = {
  run_id: 'r1',
  status: 'running',
  current_date: '2024-03-15',
  trading_dates: ['2024-03-14', '2024-03-15', '2024-03-18'],
  cursor: 1,
  bots: ['bot1', 'bot7'],
  memory_port: 41873,
  started_at: '2026-05-11T10:00:00.000Z',
  updated_at: '2026-05-11T10:01:00.000Z',
}

test('writeState then readState round-trips, and on-disk JSON is snake_case', () => {
  const w = tmpWorldRoot()
  assert.equal(stateExists(w), false)
  writeState(w, sample)
  assert.equal(stateExists(w), true)
  const got = readState(w)
  assert.deepEqual(got, sample)
  const onDisk = JSON.parse(readFileSync(join(w, 'state.json'), 'utf8'))
  assert.ok('current_date' in onDisk && 'trading_dates' in onDisk)
  rmSync(w, { recursive: true, force: true })
})

test('readState throws when missing', () => {
  const w = tmpWorldRoot()
  assert.throws(() => readState(w), /state\.json/)
  rmSync(w, { recursive: true, force: true })
})

test('writeState bumps updated_at if not provided fresh? no — caller controls; but file is atomic (no partial)', () => {
  const w = tmpWorldRoot()
  writeState(w, sample)
  writeState(w, { ...sample, cursor: 2 })
  assert.equal(readState(w).cursor, 2)
  rmSync(w, { recursive: true, force: true })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/state.test.ts`
Expected: FAIL（找不到 `../src/state.ts`）。

- [ ] **Step 3: 写 `src/state.ts`**

```ts
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { stateFile } from './paths.ts'

export type RunStatus = 'setup' | 'running' | 'done' | 'failed' | 'aborted'

export interface WorldState {
  run_id: string
  status: RunStatus
  current_date: string
  trading_dates: string[]
  cursor: number
  bots: string[]
  memory_port: number
  started_at: string
  updated_at: string
}

export function stateExists(worldRoot: string): boolean {
  return existsSync(stateFile(worldRoot))
}

export function writeState(worldRoot: string, state: WorldState): void {
  const path = stateFile(worldRoot)
  mkdirSync(dirname(path), { recursive: true })
  const tmp = `${path}.tmp`
  writeFileSync(tmp, JSON.stringify(state, null, 2) + '\n')
  renameSync(tmp, path)
}

export function readState(worldRoot: string): WorldState {
  const path = stateFile(worldRoot)
  if (!existsSync(path)) throw new Error(`no state.json at ${path}`)
  return JSON.parse(readFileSync(path, 'utf8')) as WorldState
}

export function touchState(worldRoot: string, patch: Partial<WorldState>): WorldState {
  const cur = readState(worldRoot)
  const next: WorldState = { ...cur, ...patch, updated_at: new Date().toISOString() }
  writeState(worldRoot, next)
  return next
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/state.test.ts`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/state.ts test/state.test.ts
git commit -m "feat: world state read/write with atomic file"
```

---

## Task 6: 并发助手 — `src/concurrency.ts`

**Files:**
- Create: `src/concurrency.ts`
- Test: `test/concurrency.test.ts`

- [ ] **Step 1: 写失败测试 `test/concurrency.test.ts`**

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mapWithConcurrency } from '../src/concurrency.ts'

test('mapWithConcurrency preserves order and respects limit', async () => {
  let active = 0
  let maxActive = 0
  const fn = async (n: number) => {
    active++; maxActive = Math.max(maxActive, active)
    await new Promise(r => setTimeout(r, 10))
    active--
    return n * 2
  }
  const out = await mapWithConcurrency([1, 2, 3, 4, 5], 2, fn)
  assert.deepEqual(out, [2, 4, 6, 8, 10])
  assert.ok(maxActive <= 2, `maxActive=${maxActive}`)
})

test('mapWithConcurrency handles empty input', async () => {
  assert.deepEqual(await mapWithConcurrency<number, number>([], 4, async x => x), [])
})

test('mapWithConcurrency with limit >= length runs all', async () => {
  const out = await mapWithConcurrency([1, 2, 3], 10, async x => x + 1)
  assert.deepEqual(out, [2, 3, 4])
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/concurrency.test.ts`
Expected: FAIL。

- [ ] **Step 3: 写 `src/concurrency.ts`**

```ts
export async function mapWithConcurrency<T, R>(
  items: T[],
  limit: number,
  fn: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(items.length)
  let next = 0
  const worker = async (): Promise<void> => {
    for (;;) {
      const i = next++
      if (i >= items.length) return
      results[i] = await fn(items[i], i)
    }
  }
  const n = Math.max(1, Math.min(limit, items.length))
  await Promise.all(Array.from({ length: n }, () => worker()))
  return results
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/concurrency.test.ts`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/concurrency.ts test/concurrency.test.ts
git commit -m "feat: bounded-concurrency map helper"
```

---

## Task 7: 分词器 — `src/memory-server/tokenize.ts`

**Files:**
- Create: `src/memory-server/tokenize.ts`
- Test: `test/tokenize.test.ts`

- [ ] **Step 1: 写失败测试 `test/tokenize.test.ts`**

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { tokenize } from '../src/memory-server/tokenize.ts'

test('tokenize lowercases latin runs and keeps duplicates', () => {
  assert.deepEqual(tokenize('Buy NVDA, sell NVDA today'), ['buy', 'nvda', 'sell', 'nvda', 'today'])
})

test('tokenize splits CJK into single chars', () => {
  assert.deepEqual(tokenize('半导体周期'), ['半', '导', '体', '周', '期'])
})

test('tokenize mixes CJK + latin + numbers', () => {
  assert.deepEqual(tokenize('A股科技板块涨3%'), ['a', '股', '科', '技', '板', '块', '涨', '3'])
})

test('tokenize drops stopwords', () => {
  // 'the' and 'of' and '的' are stopwords
  assert.deepEqual(tokenize('the price of gold 的 走势'), ['price', 'gold', '走', '势'])
})

test('tokenize on empty / whitespace returns []', () => {
  assert.deepEqual(tokenize('   '), [])
  assert.deepEqual(tokenize(''), [])
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/tokenize.test.ts`
Expected: FAIL。

- [ ] **Step 3: 写 `src/memory-server/tokenize.ts`**

```ts
const STOPWORDS = new Set<string>([
  // latin
  'the', 'a', 'an', 'of', 'to', 'in', 'on', 'and', 'or', 'is', 'are', 'be', 'for', 'with', 'at', 'by', 'it', 'this', 'that',
  // 中文常见虚词（单字）
  '的', '了', '和', '是', '在', '我', '你', '他', '她', '它', '也', '都', '就', '与', '及', '等', '吧', '呢', '啊', '吗',
])

const TOKEN_RE = /[a-z0-9]+|[一-鿿]/g

export function tokenize(text: string): string[] {
  const lowered = text.toLowerCase()
  const out: string[] = []
  for (const m of lowered.matchAll(TOKEN_RE)) {
    const tok = m[0]
    if (STOPWORDS.has(tok)) continue
    out.push(tok)
  }
  return out
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/tokenize.test.ts`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/memory-server/tokenize.ts test/tokenize.test.ts
git commit -m "feat: simple CJK+latin tokenizer for keyword memory"
```

---

## Task 8: 记忆存储 — `src/memory-server/store.ts`

**Files:**
- Create: `src/memory-server/store.ts`
- Test: `test/memory-store.test.ts`

- [ ] **Step 1: 写失败测试 `test/memory-store.test.ts`**

```ts
import { mkdtempSync, rmSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { MemoryStore } from '../src/memory-server/store.ts'

function tmpStore(): { file: string; cleanup: () => void } {
  const dir = mkdtempSync(join(tmpdir(), 'mstore-'))
  return { file: join(dir, 'sub', 'store.jsonl'), cleanup: () => rmSync(dir, { recursive: true, force: true }) }
}

test('add stores verbatim text with given created_at and agent_id, persisted as JSONL', () => {
  const { file, cleanup } = tmpStore()
  const s = new MemoryStore(file)
  const rec = s.add({ text: '半导体周期见底，看好封测', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-15' })
  assert.equal(rec.text, '半导体周期见底，看好封测')
  assert.equal(rec.agent_id, 'bot7')
  assert.equal(rec.created_at, '2024-03-15')
  assert.ok(rec.id)
  const lines = readFileSync(file, 'utf8').trim().split('\n')
  assert.equal(lines.length, 1)
  assert.equal(JSON.parse(lines[0]).text, '半导体周期见底，看好封测')
  cleanup()
})

test('search ranks by keyword overlap count, filters by agent_id, respects limit', () => {
  const { file, cleanup } = tmpStore()
  const s = new MemoryStore(file)
  s.add({ text: '半导体 看好 封测', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-15' })
  s.add({ text: '半导体 库存 偏高', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-16' })
  s.add({ text: '光伏 周期 见底', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-17' })
  s.add({ text: '半导体 看好 封测 设备', agent_id: 'bot1', user_id: 'bot1', created_at: '2024-03-16' })

  const hits = s.search('半导体 封测', { agent_id: 'bot7', limit: 5 })
  assert.equal(hits.length, 2) // 第三条无重叠 → 排除
  assert.equal(hits[0].memory, '半导体 看好 封测') // 重叠 半/导/体/封/测 = 5
  assert.ok(hits[0].score > hits[1].score)
  assert.equal(hits.every(h => h.agent_id === 'bot7'), true)

  const limited = s.search('半导体', { agent_id: 'bot7', limit: 1 })
  assert.equal(limited.length, 1)

  const all = s.search('半导体 封测', { limit: 10 }) // 不过滤 agent
  assert.equal(all.length, 3)
  cleanup()
})

test('search with no overlapping tokens returns []', () => {
  const { file, cleanup } = tmpStore()
  const s = new MemoryStore(file)
  s.add({ text: '光伏 周期', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-15' })
  assert.deepEqual(s.search('白酒 估值', { agent_id: 'bot7', limit: 5 }), [])
  cleanup()
})

test('new MemoryStore loads existing file', () => {
  const { file, cleanup } = tmpStore()
  new MemoryStore(file).add({ text: '半导体', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-15' })
  const s2 = new MemoryStore(file)
  assert.equal(s2.search('半导体', { agent_id: 'bot7', limit: 5 }).length, 1)
  cleanup()
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/memory-store.test.ts`
Expected: FAIL。

- [ ] **Step 3: 写 `src/memory-server/store.ts`**

```ts
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { randomUUID } from 'node:crypto'
import { tokenize } from './tokenize.ts'

export interface MemoryRecord {
  id: string
  agent_id: string | null
  user_id: string | null
  text: string
  created_at: string
  metadata?: Record<string, unknown>
}

export interface AddInput {
  text: string
  agent_id?: string | null
  user_id?: string | null
  created_at: string
  metadata?: Record<string, unknown>
}

export interface SearchHit {
  memory: string
  agent_id: string | null
  score: number
  created_at: string
  id: string
}

function tokenCounts(text: string): Map<string, number> {
  const m = new Map<string, number>()
  for (const t of tokenize(text)) m.set(t, (m.get(t) ?? 0) + 1)
  return m
}

export class MemoryStore {
  private readonly file: string
  private readonly records: MemoryRecord[] = []

  constructor(file: string) {
    this.file = file
    if (existsSync(file)) {
      for (const line of readFileSync(file, 'utf8').split('\n')) {
        const trimmed = line.trim()
        if (!trimmed) continue
        try { this.records.push(JSON.parse(trimmed) as MemoryRecord) } catch { /* skip corrupt line */ }
      }
    } else {
      mkdirSync(dirname(file), { recursive: true })
    }
  }

  add(input: AddInput): MemoryRecord {
    const rec: MemoryRecord = {
      id: randomUUID(),
      agent_id: input.agent_id ?? null,
      user_id: input.user_id ?? null,
      text: input.text,
      created_at: input.created_at,
      ...(input.metadata ? { metadata: input.metadata } : {}),
    }
    appendFileSync(this.file, JSON.stringify(rec) + '\n')
    this.records.push(rec)
    return rec
  }

  search(query: string, opts: { agent_id?: string; limit: number }): SearchHit[] {
    const q = tokenize(query)
    if (q.length === 0) return []
    const qSet = new Set(q)
    const limit = Math.max(1, Math.min(opts.limit, 50))
    const scored: SearchHit[] = []
    for (const rec of this.records) {
      if (opts.agent_id !== undefined && rec.agent_id !== opts.agent_id) continue
      const counts = tokenCounts(rec.text)
      let score = 0
      for (const tok of qSet) score += counts.get(tok) ?? 0
      if (score <= 0) continue
      scored.push({ memory: rec.text, agent_id: rec.agent_id, score, created_at: rec.created_at, id: rec.id })
    }
    scored.sort((a, b) => b.score - a.score || (b.created_at < a.created_at ? -1 : b.created_at > a.created_at ? 1 : a.id < b.id ? -1 : 1))
    return scored.slice(0, limit)
  }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/memory-store.test.ts`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/memory-server/store.ts test/memory-store.test.ts
git commit -m "feat: JSONL keyword memory store (verbatim add, overlap-scored search)"
```

---

## Task 9: 记忆 HTTP 服务 — `src/memory-server/server.ts`

**Files:**
- Create: `src/memory-server/server.ts`
- Test: `test/memory-server.test.ts`

- [ ] **Step 1: 写失败测试 `test/memory-server.test.ts`**

```ts
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { MemoryStore } from '../src/memory-server/store.ts'
import { createMemoryServer } from '../src/memory-server/server.ts'

async function withServer(getDate: () => string, run: (url: string, store: MemoryStore) => Promise<void>) {
  const dir = mkdtempSync(join(tmpdir(), 'msrv-'))
  const store = new MemoryStore(join(dir, 'store.jsonl'))
  const h = await createMemoryServer({ store, getCurrentDate: getDate })
  try { await run(h.url, store) } finally { await h.close(); rmSync(dir, { recursive: true, force: true }) }
}

test('POST /memories stores verbatim with current date; POST /search returns mem0-shaped results', async () => {
  let day = '2024-03-15'
  await withServer(() => day, async (url) => {
    let r = await fetch(`${url}/memories`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'system', content: '半导体看好封测' }], user_id: 'bot7', agent_id: 'bot7', infer: false }),
    })
    assert.equal(r.status, 200)
    const added = await r.json()
    assert.ok(Array.isArray(added.results) && added.results[0].id)

    day = '2024-03-16'
    r = await fetch(`${url}/memories`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'system', content: '半导体库存偏高' }], user_id: 'bot7', agent_id: 'bot7' }),
    })
    assert.equal(r.status, 200)

    r = await fetch(`${url}/search`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ query: '半导体 封测', user_id: 'bot7', agent_id: 'bot7', limit: 5 }),
    })
    assert.equal(r.status, 200)
    const found = await r.json()
    assert.equal(found.results.length, 2)
    assert.equal(found.results[0].memory, '半导体看好封测')
    assert.equal(found.results[0].agent_id, 'bot7')
    assert.equal(found.results[0].created_at, '2024-03-15')
    assert.ok(typeof found.results[0].score === 'number')
  })
})

test('POST /memories without any identifier → 400', async () => {
  await withServer(() => '2024-03-15', async (url) => {
    const r = await fetch(`${url}/memories`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'system', content: 'x' }] }),
    })
    assert.equal(r.status, 400)
  })
})

test('GET /health → ok; unknown route → 404', async () => {
  await withServer(() => '2024-03-15', async (url) => {
    assert.equal((await fetch(`${url}/health`)).status, 200)
    assert.equal((await fetch(`${url}/nope`)).status, 404)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/memory-server.test.ts`
Expected: FAIL（找不到 `../src/memory-server/server.ts`）。

- [ ] **Step 3: 写 `src/memory-server/server.ts`**

```ts
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import type { MemoryStore } from './store.ts'

export interface MemoryServerHandle {
  port: number
  url: string
  close(): Promise<void>
}

export interface CreateMemoryServerOpts {
  store: MemoryStore
  getCurrentDate: () => string
  port?: number
  host?: string
}

function send(res: ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body)
  res.writeHead(status, { 'content-type': 'application/json' })
  res.end(text)
}

async function readJsonBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = []
  for await (const c of req) chunks.push(c as Buffer)
  const text = Buffer.concat(chunks).toString('utf8')
  if (!text.trim()) return {}
  return JSON.parse(text)
}

export async function createMemoryServer(opts: CreateMemoryServerOpts): Promise<MemoryServerHandle> {
  const host = opts.host ?? '127.0.0.1'
  const server = createServer((req, res) => {
    void (async () => {
      try {
        const url = req.url ?? '/'
        if (req.method === 'GET' && url === '/health') return send(res, 200, { status: 'ok' })

        if (req.method === 'POST' && url === '/memories') {
          const body = (await readJsonBody(req)) as Record<string, unknown>
          const messages = Array.isArray(body.messages) ? body.messages : []
          const text = messages.map(m => (m && typeof m === 'object' ? String((m as Record<string, unknown>).content ?? '') : '')).filter(Boolean).join('\n')
          const user_id = typeof body.user_id === 'string' ? body.user_id : null
          const agent_id = typeof body.agent_id === 'string' ? body.agent_id : null
          if (!user_id && !agent_id) return send(res, 400, { detail: 'at least one of user_id / agent_id required' })
          if (!text) return send(res, 400, { detail: 'messages[].content is empty' })
          const rec = opts.store.add({ text, user_id, agent_id, created_at: opts.getCurrentDate(), metadata: body.metadata && typeof body.metadata === 'object' ? (body.metadata as Record<string, unknown>) : undefined })
          return send(res, 200, { results: [{ id: rec.id }] })
        }

        if (req.method === 'POST' && url === '/search') {
          const body = (await readJsonBody(req)) as Record<string, unknown>
          const query = typeof body.query === 'string' ? body.query : ''
          if (!query) return send(res, 400, { detail: 'query required' })
          const agent_id = typeof body.agent_id === 'string' ? body.agent_id : undefined
          const limit = typeof body.limit === 'number' ? body.limit : 5
          const hits = opts.store.search(query, { agent_id, limit })
          return send(res, 200, { results: hits })
        }

        if (url === '/memories' || url === '/search') return send(res, 405, { detail: 'method not allowed' })
        return send(res, 404, { detail: 'not found' })
      } catch (err) {
        return send(res, 400, { detail: err instanceof Error ? err.message : String(err) })
      }
    })()
  })

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(opts.port ?? 0, host, () => { server.off('error', reject); resolve() })
  })
  const port = (server.address() as AddressInfo).port
  return {
    port,
    url: `http://${host}:${port}`,
    close: () => new Promise<void>((resolve, reject) => server.close(err => (err ? reject(err) : resolve()))),
  }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/memory-server.test.ts`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/memory-server/server.ts test/memory-server.test.ts
git commit -m "feat: in-process mem0-compatible keyword memory HTTP server"
```

---

## Task 10: JSON-RPC stdio 客户端 — `src/jsonrpc.ts`

**Files:**
- Create: `src/jsonrpc.ts`
- Test: `test/jsonrpc.test.ts`
- Create (test helper): `test/helpers/echoRpc.ts`

- [ ] **Step 1: 写测试用的迷你回声 RPC 子进程 `test/helpers/echoRpc.ts`**

```ts
#!/usr/bin/env -S node --experimental-strip-types
// 行分隔 JSON-RPC 回声服务：启动即发一条 notification，然后对每个 {id, method, params} 回 {id, result}。
// method 'slow' → 延迟 200ms 再回；method 'boom' → 回 {id, error}；method 'note' → 先发一条 notification 再回 result。
import { createInterface } from 'node:readline'

process.stdout.write(JSON.stringify({ method: 'ready', params: { hello: true } }) + '\n')
const rl = createInterface({ input: process.stdin })
rl.on('line', (line) => {
  const t = line.trim()
  if (!t) return
  const req = JSON.parse(t) as { id?: unknown; method?: string; params?: Record<string, unknown> }
  const id = req.id
  const reply = (obj: Record<string, unknown>) => process.stdout.write(JSON.stringify(obj) + '\n')
  if (req.method === 'slow') { setTimeout(() => reply({ id, result: { ok: true } }), 200); return }
  if (req.method === 'boom') { reply({ id, error: { code: -32000, message: 'boom' } }); return }
  if (req.method === 'note') { reply({ method: 'progress', params: { step: 1 } }); reply({ id, result: { noted: true } }); return }
  reply({ id, result: { echo: req.params ?? null } })
})
```

- [ ] **Step 2: 写失败测试 `test/jsonrpc.test.ts`**

```ts
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { JsonRpcStdioClient } from '../src/jsonrpc.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const ECHO = join(HERE, 'helpers', 'echoRpc.ts')

function startEcho() {
  const child = spawn(process.execPath, ['--experimental-strip-types', ECHO], { stdio: ['pipe', 'pipe', 'inherit'] })
  return { child, client: new JsonRpcStdioClient(child.stdin!, child.stdout!) }
}

test('request returns result; notifications are delivered; waitFor catches the ready notification', async () => {
  const { child, client } = startEcho()
  const ready = await client.waitFor(n => n.method === 'ready', 2000)
  assert.deepEqual(ready.params, { hello: true })
  const notes: string[] = []
  client.onNotification(n => notes.push(n.method))
  const r1 = await client.request('echo', { a: 1 })
  assert.deepEqual(r1, { echo: { a: 1 } })
  const r2 = await client.request('note', {})
  assert.deepEqual(r2, { noted: true })
  assert.deepEqual(notes, ['progress'])
  child.kill()
})

test('request rejects on error response', async () => {
  const { child, client } = startEcho()
  await client.waitFor(n => n.method === 'ready', 2000)
  await assert.rejects(() => client.request('boom', {}), /boom/)
  child.kill()
})

test('request times out and a late reply is ignored', async () => {
  const { child, client } = startEcho()
  await client.waitFor(n => n.method === 'ready', 2000)
  await assert.rejects(() => client.request('slow', {}, { timeoutMs: 50 }), /timeout/i)
  // 等晚到的回复落地，不应抛未捕获异常
  await new Promise(r => setTimeout(r, 250))
  // 后续请求仍正常
  assert.deepEqual(await client.request('echo', { b: 2 }), { echo: { b: 2 } })
  child.kill()
})
```

- [ ] **Step 3: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/jsonrpc.test.ts`
Expected: FAIL（找不到 `../src/jsonrpc.ts`）。

- [ ] **Step 4: 写 `src/jsonrpc.ts`**

```ts
import { createInterface } from 'node:readline'
import type { Readable, Writable } from 'node:stream'

export interface JsonRpcNotification {
  method: string
  params: Record<string, unknown>
}

interface Pending {
  resolve: (v: unknown) => void
  reject: (e: Error) => void
  timer: NodeJS.Timeout | null
}

export class JsonRpcStdioClient {
  private readonly stdin: Writable
  private nextId = 1
  private readonly pending = new Map<number, Pending>()
  private notificationHandler: ((n: JsonRpcNotification) => void) | null = null
  private readonly queuedNotifications: JsonRpcNotification[] = []
  private readonly waiters: { predicate: (n: JsonRpcNotification) => boolean; resolve: (n: JsonRpcNotification) => void; reject: (e: Error) => void; timer: NodeJS.Timeout }[] = []
  private closed = false

  constructor(stdin: Writable, stdout: Readable) {
    this.stdin = stdin
    const rl = createInterface({ input: stdout })
    rl.on('line', (line) => this.handleLine(line))
    stdout.on('close', () => this.failAll(new Error('stdout closed')))
  }

  private handleLine(line: string): void {
    const t = line.trim()
    if (!t) return
    let msg: { id?: unknown; result?: unknown; error?: { code?: number; message?: string }; method?: string; params?: Record<string, unknown> }
    try { msg = JSON.parse(t) } catch { return }
    if (typeof msg.id === 'number' && (('result' in msg) || ('error' in msg))) {
      const p = this.pending.get(msg.id)
      if (!p) return // 晚到的、已超时的回复 → 丢弃
      this.pending.delete(msg.id)
      if (p.timer) clearTimeout(p.timer)
      if (msg.error) p.reject(new Error(`JSON-RPC error ${msg.error.code ?? ''}: ${msg.error.message ?? 'unknown'}`))
      else p.resolve(msg.result)
      return
    }
    if (typeof msg.method === 'string' && msg.id === undefined) {
      const n: JsonRpcNotification = { method: msg.method, params: msg.params ?? {} }
      // 满足某个 waiter？
      for (let i = this.waiters.length - 1; i >= 0; i--) {
        if (this.waiters[i].predicate(n)) {
          const w = this.waiters.splice(i, 1)[0]
          clearTimeout(w.timer)
          w.resolve(n)
          return
        }
      }
      if (this.notificationHandler) this.notificationHandler(n)
      else this.queuedNotifications.push(n)
    }
  }

  onNotification(handler: (n: JsonRpcNotification) => void): void {
    this.notificationHandler = handler
    while (this.queuedNotifications.length) handler(this.queuedNotifications.shift()!)
  }

  waitFor(predicate: (n: JsonRpcNotification) => boolean, timeoutMs: number): Promise<JsonRpcNotification> {
    // 先看已经排队的
    for (let i = 0; i < this.queuedNotifications.length; i++) {
      if (predicate(this.queuedNotifications[i])) return Promise.resolve(this.queuedNotifications.splice(i, 1)[0])
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const idx = this.waiters.findIndex(w => w.timer === timer)
        if (idx >= 0) this.waiters.splice(idx, 1)
        reject(new Error(`timeout waiting for notification (${timeoutMs}ms)`))
      }, timeoutMs)
      this.waiters.push({ predicate, resolve, reject, timer })
    })
  }

  request<T = unknown>(method: string, params: Record<string, unknown>, opts?: { timeoutMs?: number }): Promise<T> {
    if (this.closed) return Promise.reject(new Error('client closed'))
    const id = this.nextId++
    return new Promise<T>((resolve, reject) => {
      const timer = opts?.timeoutMs
        ? setTimeout(() => { this.pending.delete(id); reject(new Error(`request timeout: ${method} (${opts.timeoutMs}ms)`)) }, opts.timeoutMs)
        : null
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject, timer })
      this.stdin.write(JSON.stringify({ id, method, params }) + '\n')
    })
  }

  private failAll(err: Error): void {
    this.closed = true
    for (const [, p] of this.pending) { if (p.timer) clearTimeout(p.timer); p.reject(err) }
    this.pending.clear()
    for (const w of this.waiters) { clearTimeout(w.timer); w.reject(err) }
    this.waiters.length = 0
  }
}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/jsonrpc.test.ts`
Expected: PASS（3 个测试）。

- [ ] **Step 6: 提交**

```bash
git add src/jsonrpc.ts test/jsonrpc.test.ts test/helpers/echoRpc.ts
git commit -m "feat: line-delimited JSON-RPC stdio client"
```

---

## Task 11: 假 bot server — `test/helpers/stubBotServer.ts`

**Files:**
- Create: `test/helpers/stubBotServer.ts`
- Test: `test/stubBotServer.test.ts`

- [ ] **Step 1: 写 `test/helpers/stubBotServer.ts`**

```ts
#!/usr/bin/env -S node --experimental-strip-types
// 假 research-loop-ts JSON-RPC server。行为由环境变量控制：
//   STUB_CHAT_MODE = reply (默认) | error | exit | hang
//   STUB_CHAT_DELAY_MS = 回复前延迟毫秒数（默认 0）
//   STUB_REPLY_TEXT    = 自定义 reply 文本（默认 "stub reply"）
// 启动即发 server.ready。支持 ping / chat / shutdown。
import { createInterface } from 'node:readline'

const args = process.argv.slice(2)
function arg(name: string): string {
  const i = args.indexOf(name)
  return i >= 0 ? (args[i + 1] ?? '') : ''
}
const botId = arg('--bot-id') || 'botX'
const workspace = arg('--workspace') || ''
const mode = process.env.STUB_CHAT_MODE || 'reply'
const delayMs = Number(process.env.STUB_CHAT_DELAY_MS || '0')
const replyText = process.env.STUB_REPLY_TEXT ?? 'stub reply'

const out = (obj: Record<string, unknown>) => process.stdout.write(JSON.stringify(obj) + '\n')
out({ method: 'server.ready', params: { bot_id: botId, workspace, model: 'stub', pid: process.pid } })

const rl = createInterface({ input: process.stdin })
rl.on('line', (line) => {
  const t = line.trim()
  if (!t) return
  const req = JSON.parse(t) as { id?: unknown; method?: string; params?: Record<string, unknown> }
  const id = req.id
  if (req.method === 'ping') { out({ id, result: { pong: true, bot_id: botId, workspace, model: 'stub' } }); return }
  if (req.method === 'shutdown') { out({ id, result: { ok: true } }); setTimeout(() => process.exit(0), 10); return }
  if (req.method === 'chat') {
    const message = String(req.params?.message ?? '')
    const sessionKey = String(req.params?.session_key ?? '')
    if (mode === 'exit') { setTimeout(() => process.exit(1), 5); return }
    if (mode === 'hang') return
    setTimeout(() => {
      if (mode === 'error') { out({ id, error: { code: -32000, message: 'stub chat error' } }); return }
      out({ method: 'message.delta', params: { text: replyText } })
      out({
        id,
        result: {
          reply: replyText,
          session_id: `stub-${sessionKey}`,
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

- [ ] **Step 2: 写测试 `test/stubBotServer.test.ts`（验证 stub 自身行为）**

```ts
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { JsonRpcStdioClient } from '../src/jsonrpc.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const STUB = join(HERE, 'helpers', 'stubBotServer.ts')

function start(env: Record<string, string> = {}) {
  const child = spawn(process.execPath, ['--experimental-strip-types', STUB, '--bot-id', 'bot7', '--workspace', '/ws/bot7'], { stdio: ['pipe', 'pipe', 'inherit'], env: { ...process.env, ...env } })
  return { child, client: new JsonRpcStdioClient(child.stdin!, child.stdout!) }
}

test('stub emits server.ready, answers ping and chat', async () => {
  const { child, client } = start({ STUB_REPLY_TEXT: 'hi' })
  const ready = await client.waitFor(n => n.method === 'server.ready', 2000)
  assert.equal(ready.params.bot_id, 'bot7')
  const pong = await client.request('ping', {})
  assert.equal((pong as { pong: boolean }).pong, true)
  const r = await client.request('chat', { message: 'today is 2024-03-15', session_key: 'trading-r1', history: [] }) as { reply: string; session_id: string }
  assert.equal(r.reply, 'hi')
  assert.equal(r.session_id, 'stub-trading-r1')
  child.kill()
})

test('stub error mode rejects chat; exit mode kills the process', async () => {
  const e = start({ STUB_CHAT_MODE: 'error' })
  await e.client.waitFor(n => n.method === 'server.ready', 2000)
  await assert.rejects(() => e.client.request('chat', { message: 'x' }), /stub chat error/)
  e.child.kill()

  const x = start({ STUB_CHAT_MODE: 'exit' })
  await x.client.waitFor(n => n.method === 'server.ready', 2000)
  const exited = new Promise<number | null>(res => x.child.on('exit', c => res(c)))
  void x.client.request('chat', { message: 'x' }).catch(() => {})
  assert.equal(await exited, 1)
})
```

- [ ] **Step 3: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/stubBotServer.test.ts`
Expected: PASS。

- [ ] **Step 4: 提交**

```bash
git add test/helpers/stubBotServer.ts test/stubBotServer.test.ts
git commit -m "test: stub research-loop-ts JSON-RPC server for integration tests"
```

---

## Task 12: bot 进程管理 — `src/botServer.ts`

**Files:**
- Create: `src/botServer.ts`
- Test: `test/botServer.test.ts`

- [ ] **Step 1: 写失败测试 `test/botServer.test.ts`**

```ts
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { BotServer } from '../src/botServer.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const STUB = join(HERE, 'helpers', 'stubBotServer.ts')

function argv(botId: string): string[] {
  return [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/ws/${botId}`]
}

test('BotServer.start awaits server.ready; ping and chat work; shutdown exits cleanly', async () => {
  const bs = await BotServer.start('bot7', { argv: argv('bot7'), readyTimeoutMs: 3000 })
  assert.equal(bs.botId, 'bot7')
  assert.equal((await bs.ping()).bot_id, 'bot7')
  const r = await bs.chat({ message: 'hello 2024-03-15', session_key: 'trading-r1', history: [] })
  assert.equal(r.reply, 'stub reply')
  assert.equal(r.iterations, 1)
  await bs.shutdown({ timeoutMs: 2000 })
  assert.equal(bs.alive, false)
})

test('chat timeout is reported but server stays alive', async () => {
  const bs = await BotServer.start('bot7', { argv: [...argv('bot7')], readyTimeoutMs: 3000, env: { STUB_CHAT_MODE: 'hang' } })
  await assert.rejects(() => bs.chat({ message: 'x' }, { timeoutMs: 80 }), /timeout/i)
  assert.equal(bs.alive, true)
  await bs.shutdown({ timeoutMs: 2000 })
})

test('chat error rejects; unexpected process exit flips alive to false and rejects pending', async () => {
  const errSrv = await BotServer.start('bot7', { argv: argv('bot7'), env: { STUB_CHAT_MODE: 'error' } })
  await assert.rejects(() => errSrv.chat({ message: 'x' }), /stub chat error/)
  await errSrv.shutdown({ timeoutMs: 2000 })

  const exitSrv = await BotServer.start('bot7', { argv: argv('bot7'), env: { STUB_CHAT_MODE: 'exit' } })
  await assert.rejects(() => exitSrv.chat({ message: 'x' }), /exit|closed/i)
  assert.equal(exitSrv.alive, false)
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/botServer.test.ts`
Expected: FAIL（找不到 `../src/botServer.ts`）。

- [ ] **Step 3: 写 `src/botServer.ts`**

```ts
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { createInterface } from 'node:readline'
import { JsonRpcStdioClient } from './jsonrpc.ts'

export interface BotChatParams {
  message: string
  session_key?: string
  history?: unknown[]
  channel?: string
  metadata?: Record<string, unknown>
}

export interface BotChatResult {
  reply: string
  session_id: string
  assistant_messages: unknown[]
  tool_trace: unknown[]
  usage: number
  iterations: number
  truncated_by_iterations: boolean
}

export interface BotServerOptions {
  argv: string[]               // argv[0] = 可执行文件（通常 process.execPath），其余是参数
  cwd?: string
  env?: Record<string, string>
  readyTimeoutMs?: number      // 等 server.ready 的超时，默认 30000
  onLog?: (line: string) => void   // 接收子进程 stderr 行
  onNotification?: (method: string, params: Record<string, unknown>) => void
}

export class BotServer {
  readonly botId: string
  private readonly child: ChildProcessWithoutNullStreams
  private readonly client: JsonRpcStdioClient
  private _alive = true
  private exitHandlers: ((code: number | null) => void)[] = []

  private constructor(botId: string, child: ChildProcessWithoutNullStreams, client: JsonRpcStdioClient, onLog?: (l: string) => void) {
    this.botId = botId
    this.child = child
    this.client = client
    const errRl = createInterface({ input: child.stderr })
    errRl.on('line', (l) => onLog?.(`[${botId}] ${l}`))
    child.on('exit', (code) => { this._alive = false; for (const h of this.exitHandlers) h(code) })
  }

  static async start(botId: string, opts: BotServerOptions): Promise<BotServer> {
    const [cmd, ...rest] = opts.argv
    const child = spawn(cmd, rest, { stdio: ['pipe', 'pipe', 'pipe'], cwd: opts.cwd, env: { ...process.env, ...opts.env } }) as ChildProcessWithoutNullStreams
    const client = new JsonRpcStdioClient(child.stdin, child.stdout)
    const bs = new BotServer(botId, child, client, opts.onLog)
    if (opts.onNotification) client.onNotification(n => opts.onNotification!(n.method, n.params))
    try {
      await client.waitFor(n => n.method === 'server.ready', opts.readyTimeoutMs ?? 30_000)
    } catch (err) {
      try { child.kill('SIGKILL') } catch { /* ignore */ }
      throw new Error(`bot ${botId}: server did not become ready: ${err instanceof Error ? err.message : String(err)}`)
    }
    return bs
  }

  get alive(): boolean { return this._alive }

  onExit(handler: (code: number | null) => void): void { this.exitHandlers.push(handler) }

  async ping(): Promise<{ pong: boolean; bot_id: string; workspace: string; model: string }> {
    return this.client.request('ping', {})
  }

  async chat(params: BotChatParams, opts?: { timeoutMs?: number }): Promise<BotChatResult> {
    if (!this._alive) throw new Error(`bot ${this.botId}: server process is not alive`)
    return this.client.request<BotChatResult>('chat', { ...params }, { timeoutMs: opts?.timeoutMs })
  }

  async shutdown(opts?: { timeoutMs?: number }): Promise<void> {
    if (!this._alive) return
    const timeoutMs = opts?.timeoutMs ?? 5000
    const exited = new Promise<void>((resolve) => { if (!this._alive) return resolve(); this.child.once('exit', () => resolve()) })
    try { await this.client.request('shutdown', {}, { timeoutMs: Math.min(timeoutMs, 2000) }) } catch { /* ignore */ }
    const killTimer = setTimeout(() => { try { this.child.kill('SIGKILL') } catch { /* ignore */ } }, timeoutMs)
    await exited
    clearTimeout(killTimer)
  }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/botServer.test.ts`
Expected: PASS（3 个测试）。

- [ ] **Step 5: 提交**

```bash
git add src/botServer.ts test/botServer.test.ts
git commit -m "feat: BotServer — spawn and drive a research-loop-ts JSON-RPC server"
```

---

## Task 13: 影子 workspace — `src/shadowWorkspace.ts`

**Files:**
- Create: `src/shadowWorkspace.ts`
- Test: `test/shadowWorkspace.test.ts`

- [ ] **Step 1: 写失败测试 `test/shadowWorkspace.test.ts`**

```ts
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildShadowWorkspace } from '../src/shadowWorkspace.ts'

function fakeWorkspace(): string {
  const dir = mkdtempSync(join(tmpdir(), 'srcws-'))
  writeFileSync(join(dir, 'SOUL.md'), '# soul')
  writeFileSync(join(dir, 'IDENTITY.md'), '# id')
  writeFileSync(join(dir, 'avatar.png'), 'BIGBINARY')
  mkdirSync(join(dir, 'skills', 'foo'), { recursive: true })
  writeFileSync(join(dir, 'skills', 'foo', 'SKILL.md'), '# skill')
  mkdirSync(join(dir, 'sessions'), { recursive: true })
  writeFileSync(join(dir, 'sessions', 'old.json'), '{}')
  return dir
}

test('buildShadowWorkspace copies included paths, skips others, creates empty memory/trading/journal.md', () => {
  const src = fakeWorkspace()
  const dest = join(mkdtempSync(join(tmpdir(), 'dstws-')), 'bot7')
  buildShadowWorkspace({ sourceDir: src, destDir: dest, include: ['SOUL.md', 'IDENTITY.md', 'skills'] })

  assert.equal(readFileSync(join(dest, 'SOUL.md'), 'utf8'), '# soul')
  assert.equal(readFileSync(join(dest, 'IDENTITY.md'), 'utf8'), '# id')
  assert.equal(readFileSync(join(dest, 'skills', 'foo', 'SKILL.md'), 'utf8'), '# skill')
  assert.equal(existsSync(join(dest, 'avatar.png')), false)
  assert.equal(existsSync(join(dest, 'sessions')), false)
  assert.equal(existsSync(join(dest, 'memory', 'trading', 'journal.md')), true)
  assert.ok(readFileSync(join(dest, 'memory', 'trading', 'journal.md'), 'utf8').length > 0)

  rmSync(src, { recursive: true, force: true })
  rmSync(dest, { recursive: true, force: true })
})

test('buildShadowWorkspace tolerates missing source entries and does not overwrite existing journal', () => {
  const src = mkdtempSync(join(tmpdir(), 'srcws2-'))
  writeFileSync(join(src, 'SOUL.md'), 's')
  const dest = join(mkdtempSync(join(tmpdir(), 'dstws2-')), 'bot1')
  mkdirSync(join(dest, 'memory', 'trading'), { recursive: true })
  writeFileSync(join(dest, 'memory', 'trading', 'journal.md'), 'PRE-EXISTING')
  buildShadowWorkspace({ sourceDir: src, destDir: dest, include: ['SOUL.md', 'DOES_NOT_EXIST.md', 'skills'] })
  assert.equal(readFileSync(join(dest, 'SOUL.md'), 'utf8'), 's')
  assert.equal(readFileSync(join(dest, 'memory', 'trading', 'journal.md'), 'utf8'), 'PRE-EXISTING')
  rmSync(src, { recursive: true, force: true })
  rmSync(dest, { recursive: true, force: true })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/shadowWorkspace.test.ts`
Expected: FAIL（找不到 `../src/shadowWorkspace.ts`）。

- [ ] **Step 3: 写 `src/shadowWorkspace.ts`**

```ts
import { cpSync, existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { DEFAULT_SHADOW_INCLUDE } from './config.ts'

const JOURNAL_TEMPLATE = `# 交易日志（trading journal）

这是你在金融世界沙盘里的交易日志。规则：
- 每个世界日：做决策**之前**先读这份文件，回顾你过去的判断和操作。
- 做完决策**之后**：把今天的世界日期、你的判断、具体操作、理由，追加到本文件末尾。

---
`

export interface BuildShadowWorkspaceOptions {
  sourceDir: string
  destDir: string
  include?: string[]
}

export function buildShadowWorkspace(opts: BuildShadowWorkspaceOptions): void {
  const include = opts.include ?? DEFAULT_SHADOW_INCLUDE
  mkdirSync(opts.destDir, { recursive: true })
  for (const rel of include) {
    const src = join(opts.sourceDir, rel)
    if (!existsSync(src)) continue
    const dst = join(opts.destDir, rel)
    mkdirSync(dirname(dst), { recursive: true })
    cpSync(src, dst, { recursive: true })
  }
  const journal = join(opts.destDir, 'memory', 'trading', 'journal.md')
  if (!existsSync(journal)) {
    mkdirSync(dirname(journal), { recursive: true })
    writeFileSync(journal, JOURNAL_TEMPLATE)
  }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/shadowWorkspace.test.ts`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/shadowWorkspace.ts test/shadowWorkspace.test.ts
git commit -m "feat: build per-run shadow workspace from workspace-botN"
```

---

## Task 14: 当日概览 — `src/overview.ts`

**Files:**
- Create: `src/overview.ts`
- Test: `test/overview.test.ts`

- [ ] **Step 1: 写失败测试 `test/overview.test.ts`**

```ts
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveOverview } from '../src/overview.ts'
import { dayDir } from '../src/paths.ts'

function tmpWorld(date: string, files: { overviewMd?: string; quotes?: unknown; events?: unknown }): string {
  const w = mkdtempSync(join(tmpdir(), 'ov-'))
  const d = dayDir(w, date)
  mkdirSync(d, { recursive: true })
  if (files.overviewMd !== undefined) writeFileSync(join(d, 'overview.md'), files.overviewMd)
  if (files.quotes !== undefined) writeFileSync(join(d, 'quotes.json'), JSON.stringify(files.quotes))
  if (files.events !== undefined) writeFileSync(join(d, 'events.json'), JSON.stringify(files.events))
  return w
}

test('prefers overview.md when present', () => {
  const w = tmpWorld('2024-03-15', { overviewMd: '上证 +1.2%，半导体领涨。', quotes: { whatever: 1 } })
  assert.match(resolveOverview(w, '2024-03-15'), /半导体领涨/)
  rmSync(w, { recursive: true, force: true })
})

test('falls back to quotes.json "summary" string and appends events digest', () => {
  const w = tmpWorld('2024-03-15', { quotes: { summary: '主要指数小幅收涨' }, events: { events: ['央行 MLF 续作', { title: 'AI 算力新政发布' }] } })
  const o = resolveOverview(w, '2024-03-15')
  assert.match(o, /主要指数小幅收涨/)
  assert.match(o, /央行 MLF 续作/)
  assert.match(o, /AI 算力新政发布/)
  rmSync(w, { recursive: true, force: true })
})

test('graceful degradation when no overview.md and quotes.json has unknown shape', () => {
  const w = tmpWorld('2024-03-15', { quotes: { rows: [[1, 2, 3]] } })
  assert.match(resolveOverview(w, '2024-03-15'), /概览不可用|直接读取/)
  rmSync(w, { recursive: true, force: true })
})

test('degradation when quotes.json missing entirely', () => {
  const w = mkdtempSync(join(tmpdir(), 'ov2-'))
  mkdirSync(dayDir(w, '2024-03-15'), { recursive: true })
  assert.match(resolveOverview(w, '2024-03-15'), /概览不可用/)
  rmSync(w, { recursive: true, force: true })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/overview.test.ts`
Expected: FAIL（找不到 `../src/overview.ts`）。

- [ ] **Step 3: 写 `src/overview.ts`**

```ts
import { existsSync, readFileSync } from 'node:fs'
import { overviewFile, quotesFile, eventsFile } from './paths.ts'

const FALLBACK = '（当日概览不可用：未提供 overview.md，且无法从 quotes.json 自动生成概览。请直接读取上面给出的 quotes.json 文件了解今日行情。）'

function readJson(path: string): unknown | undefined {
  if (!existsSync(path)) return undefined
  try { return JSON.parse(readFileSync(path, 'utf8')) } catch { return undefined }
}

function eventsDigest(worldRoot: string, date: string): string {
  const raw = readJson(eventsFile(worldRoot, date))
  let list: unknown[] = []
  if (Array.isArray(raw)) list = raw
  else if (raw && typeof raw === 'object' && Array.isArray((raw as { events?: unknown[] }).events)) list = (raw as { events: unknown[] }).events
  const titles = list
    .map(e => (typeof e === 'string' ? e : e && typeof e === 'object' ? String((e as Record<string, unknown>).title ?? (e as Record<string, unknown>).name ?? '') : ''))
    .filter(Boolean)
    .slice(0, 8)
  return titles.length ? `\n\n今日要点/事件:\n${titles.map(t => `- ${t}`).join('\n')}` : ''
}

export function resolveOverview(worldRoot: string, date: string): string {
  const md = overviewFile(worldRoot, date)
  if (existsSync(md)) {
    const text = readFileSync(md, 'utf8').trim()
    if (text) return text
  }
  const quotes = readJson(quotesFile(worldRoot, date))
  let body = FALLBACK
  if (quotes && typeof quotes === 'object' && !Array.isArray(quotes)) {
    const q = quotes as Record<string, unknown>
    if (typeof q.summary === 'string' && q.summary.trim()) {
      body = q.summary.trim()
    } else if (Array.isArray(q.indices)) {
      const lines = (q.indices as unknown[])
        .map(it => (it && typeof it === 'object' ? `${String((it as Record<string, unknown>).name ?? '?')}: ${String((it as Record<string, unknown>).change_pct ?? (it as Record<string, unknown>).pct ?? '?')}` : ''))
        .filter(Boolean)
      if (lines.length) body = `主要指数:\n${lines.map(l => `- ${l}`).join('\n')}`
    }
  }
  return body + eventsDigest(worldRoot, date)
}
```

> 说明：自动生成概览刻意只认两种约定字段（`summary` 字符串 或 `indices: [{name, change_pct}]`）；数据团队若想要自动概览，按这个约定写 `quotes.json` 即可，否则直接提供 `overview.md`。其余情况一律降级为「请直接读 quotes.json」。

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/overview.test.ts`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/overview.ts test/overview.test.ts
git commit -m "feat: daily overview resolver (overview.md > quotes.json summary > fallback)"
```

---

## Task 15: 当日 chat message — `src/message.ts`

**Files:**
- Create: `src/message.ts`
- Test: `test/message.test.ts`

- [ ] **Step 1: 写失败测试 `test/message.test.ts`**

```ts
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderDailyMessage, weekdayOf } from '../src/message.ts'
import { dayDir } from '../src/paths.ts'

function tmpWorldWithOverview(date: string, overviewMd: string): string {
  const w = mkdtempSync(join(tmpdir(), 'msg-'))
  mkdirSync(dayDir(w, date), { recursive: true })
  writeFileSync(join(dayDir(w, date), 'overview.md'), overviewMd)
  return w
}

test('weekdayOf returns English weekday for a UTC date', () => {
  assert.equal(weekdayOf('2024-03-15'), 'Friday')
  assert.equal(weekdayOf('2024-03-18'), 'Monday')
})

test('first-day message includes full world rules + overview + absolute quotes path', () => {
  const w = tmpWorldWithOverview('2024-03-15', '上证 +1.2%，半导体领涨。')
  const m = renderDailyMessage({ worldRoot: w, date: '2024-03-15', isFirstDay: true, quotesPath: '/abs/world/days/2024-03-15/quotes.json', journalRelPath: 'memory/trading/journal.md' })
  assert.match(m, /当前世界日期：2024-03-15（Friday）/)
  assert.match(m, /回放历史行情的沙盘/)
  assert.match(m, /discover_tools/)
  assert.match(m, /\/abs\/world\/days\/2024-03-15\/quotes\.json/)
  assert.match(m, /memory\/trading\/journal\.md/)
  assert.match(m, /mem0_search/)
  assert.match(m, /mem0_add/)
  assert.match(m, /上证 \+1\.2%，半导体领涨。/)
  assert.match(m, /不要开 start_research 大坑/)
  rmSync(w, { recursive: true, force: true })
})

test('non-first-day message uses the concise rules block but still has date + overview + paths', () => {
  const w = tmpWorldWithOverview('2024-03-18', '上证 -0.4%。')
  const full = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  const brief = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: false, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  assert.ok(brief.length < full.length)
  assert.match(brief, /当前世界日期：2024-03-18（Monday）/)
  assert.match(brief, /上证 -0\.4%。/)
  assert.match(brief, /\/q\.json/)
  assert.match(brief, /规则同前/)
  rmSync(w, { recursive: true, force: true })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/message.test.ts`
Expected: FAIL（找不到 `../src/message.ts`）。

- [ ] **Step 3: 写 `src/message.ts`**

```ts
import { resolveOverview } from './overview.ts'

export interface DailyMessageContext {
  worldRoot: string
  date: string
  isFirstDay: boolean
  quotesPath: string        // 绝对路径
  journalRelPath: string    // 相对 bot workspace，例如 memory/trading/journal.md
}

export function weekdayOf(isoDate: string): string {
  return new Date(isoDate + 'T00:00:00Z').toLocaleString('en-US', { weekday: 'long', timeZone: 'UTC' })
}

function fullRules(date: string, weekday: string, quotesPath: string, journalRelPath: string): string {
  return `你现在身处一个金融模拟世界。当前世界日期：${date}（${weekday}）。
这是一个回放历史行情的沙盘——你只能看到 ${date} 当天及之前的信息，不存在「未来数据」。不要使用任何能取到当前真实时间或未来行情的工具/知识。

【你的任务】根据今天的行情，按你自己的投资风格做出今天的交易决策并执行。

【交易系统】交易引擎以工具形式提供（查持仓、查可用资金、下单、查成交、撤单等）。
不要凭记忆猜工具名——先用 discover_tools 看清楚有哪些交易工具，再调用。
你的持仓、现金、累计盈亏都从交易系统的工具里查；本消息不会替你列出来。

【行情数据】今天的全量行情在文件：${quotesPath}（需要细节就读它）。
下面是当天精简概览：`
}

function briefRules(date: string, weekday: string, quotesPath: string): string {
  return `金融模拟世界——当前世界日期：${date}（${weekday}）。规则同前（回放沙盘，无未来数据；先 discover_tools 看交易工具再下单；持仓/盈亏自己查；决策前读 ${'`'}memory/trading/journal.md${'`'} 并 mem0_search，决策后追加 journal 并 mem0_add；这是交易回合不是研究项目）。
今天的全量行情在文件：${quotesPath}。
下面是当天精简概览：`
}

const FOOTER_FULL = (journalRelPath: string) => `

【记忆与连续性】每个世界日是独立会话，你不会自动记得昨天。
- 决策前：读你的交易日志 ${journalRelPath}；用 mem0_search 调取相关的历史交易记忆。
- 决策后：把今天的判断、操作、理由追加到 ${journalRelPath}；把关键结论用 mem0_add 存进记忆。

【边界】这是一次交易回合，不是一个研究项目——可以快速查证，但不要开 start_research 大坑。
今天结束前，确保该下的单都下了、journal 写了。`

export function renderDailyMessage(ctx: DailyMessageContext): string {
  const weekday = weekdayOf(ctx.date)
  const overview = resolveOverview(ctx.worldRoot, ctx.date)
  if (ctx.isFirstDay) {
    return `${fullRules(ctx.date, weekday, ctx.quotesPath, ctx.journalRelPath)}\n\n${overview}${FOOTER_FULL(ctx.journalRelPath)}\n`
  }
  return `${briefRules(ctx.date, weekday, ctx.quotesPath)}\n\n${overview}\n`
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/message.test.ts`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/message.ts test/message.test.ts
git commit -m "feat: daily chat message renderer (full vs concise world rules + overview)"
```

---

## Task 16: 主编排器 — `src/run.ts`（runWorld）+ e2e 小回放

**Files:**
- Create: `src/run.ts`
- Test: `test/run.test.ts`

> 本任务实现 `runWorld`（setup → loop → teardown）和它的辅助函数；`resumeWorld` 与 stop 见 Task 17；CLI 见 Task 18。

- [ ] **Step 1: 写失败测试 `test/run.test.ts`（用 stub bot server 的 e2e）**

```ts
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { runWorld } from '../src/run.ts'
import { BotServer } from '../src/botServer.ts'
import * as P from '../src/paths.ts'
import { readState } from '../src/state.ts'
import type { WorldConfig } from '../src/config.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const STUB = join(HERE, 'helpers', 'stubBotServer.ts')

// 用 stub 替换真实 research-loop-ts：忽略真实 argv，只用 --bot-id 起 stub。
function stubStartBotServer(botId: string, _argv: string[]): Promise<BotServer> {
  return BotServer.start(botId, { argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`], readyTimeoutMs: 5000 })
}

function setupWorldDir(opts: { bots: string[]; dates: string[]; withSrcWorkspaces?: boolean }): { worldRoot: string; config: WorldConfig; cleanup: () => void } {
  const root = mkdtempSync(join(tmpdir(), 'world-e2e-'))
  const worldRoot = join(root, 'world')
  // calendar
  mkdirSync(worldRoot, { recursive: true })
  writeFileSync(P.calendarFile(worldRoot), JSON.stringify({ trading_days: opts.dates }))
  // days/<date>/quotes.json (+ overview.md)
  for (const d of opts.dates) {
    mkdirSync(P.dayDir(worldRoot, d), { recursive: true })
    writeFileSync(P.quotesFile(worldRoot, d), JSON.stringify({ summary: `行情快照 ${d}` }))
    writeFileSync(P.overviewFile(worldRoot, d), `概览：${d} 上证小涨`)
  }
  // 源 workspace（research_loop_ts 路径在 e2e 里不会被真的执行，但 buildShadowWorkspace 需要它存在）
  const wsRoot = join(root, 'workspaces')
  for (const b of opts.bots) {
    const ws = join(wsRoot, `workspace-${b}`)
    mkdirSync(ws, { recursive: true })
    writeFileSync(join(ws, 'SOUL.md'), `# soul ${b}`)
  }
  // rl-config base
  const cfgBase = join(root, 'trading-rl-config.base.json')
  writeFileSync(cfgBase, JSON.stringify({ model: { primary: {} }, mcp: { servers: {} }, plugins: { 'memory-mem0': { enabled: true } } }))
  const config: WorldConfig = {
    researchLoopTs: '/nonexistent/research-loop-ts',
    workspaceRoot: wsRoot,
    bots: opts.bots,
    replay: { from: opts.dates[0], to: opts.dates[opts.dates.length - 1] },
    calendar: P.calendarFile(worldRoot),
    concurrency: 4,
    perBotTimeoutSeconds: 30,
    rlConfigBase: cfgBase,
    rlOpenclawDir: join(root, 'fake-openclaw'),
    shadowInclude: ['SOUL.md'],
  }
  return { worldRoot, config, cleanup: () => rmSync(root, { recursive: true, force: true }) }
}

test('runWorld replays 2 trading days for 2 bots: artifacts written, status done, generated rl-config has mem0 url', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1', 'bot7'], dates: ['2024-03-14', '2024-03-15'] })
  await runWorld({ worldRoot, config, runId: 'r1', startBotServer: stubStartBotServer })

  const st = readState(worldRoot)
  assert.equal(st.status, 'done')
  assert.equal(st.cursor, 2)
  assert.equal(st.run_id, 'r1')
  assert.ok(st.memory_port > 0)

  for (const d of ['2024-03-14', '2024-03-15']) {
    for (const b of ['bot1', 'bot7']) {
      assert.ok(existsSync(P.sentFile(worldRoot, 'r1', d, b)), `sent ${d} ${b}`)
      assert.match(readFileSync(P.sentFile(worldRoot, 'r1', d, b), 'utf8'), new RegExp(`当前世界日期：${d}`))
      const reply = JSON.parse(readFileSync(P.replyFile(worldRoot, 'r1', d, b), 'utf8'))
      assert.equal(reply.reply, 'stub reply')
      assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'r1', d, b), 'utf8')).status, 'ok')
    }
  }
  // 首日发完整规则，次日发精简规则
  assert.match(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-14', 'bot1'), 'utf8'), /回放历史行情的沙盘/)
  assert.match(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-15', 'bot1'), 'utf8'), /规则同前/)
  // 影子 workspace + journal
  assert.match(readFileSync(join(P.shadowWorkspaceDir(worldRoot, 'r1', 'bot7'), 'SOUL.md'), 'utf8'), /soul bot7/)
  assert.ok(existsSync(join(P.shadowWorkspaceDir(worldRoot, 'r1', 'bot7'), 'memory', 'trading', 'journal.md')))
  // 生成的 rl-config 写入了 mem0 url
  const genCfg = JSON.parse(readFileSync(P.runConfigFile(worldRoot, 'r1'), 'utf8'))
  assert.match(genCfg.mcp.mem0, /^http:\/\/127\.0\.0\.1:\d+$/)
  // summary.json
  const summary = JSON.parse(readFileSync(P.summaryFile(worldRoot, 'r1'), 'utf8'))
  assert.equal(summary.run_id, 'r1')
  assert.equal(summary.days.length, 2)
  cleanup()
})

test('runWorld: a hanging bot is recorded as timeout but does not block the other bot or the day advance', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1', 'bot7'], dates: ['2024-03-14'] })
  config.perBotTimeoutSeconds = 1 // 1s 超时
  const start = (botId: string, _argv: string[]) => BotServer.start(botId, {
    argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`],
    readyTimeoutMs: 5000,
    env: botId === 'bot7' ? { STUB_CHAT_MODE: 'hang' } : {},
  })
  await runWorld({ worldRoot, config, runId: 'r2', startBotServer: start })
  assert.equal(readState(worldRoot).status, 'done')
  assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'r2', '2024-03-14', 'bot1'), 'utf8')).status, 'ok')
  assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'r2', '2024-03-14', 'bot7'), 'utf8')).status, 'timeout')
  cleanup()
})

test('runWorld fails fast when a quotes.json is missing', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14', '2024-03-15'] })
  rmSync(P.quotesFile(worldRoot, '2024-03-15'), { force: true })
  await assert.rejects(() => runWorld({ worldRoot, config, runId: 'r3', startBotServer: stubStartBotServer }), /quotes\.json|missing/i)
  // state 应为 failed（若已写过）或不存在
  if (existsSync(P.stateFile(worldRoot))) assert.equal(readState(worldRoot).status, 'failed')
  cleanup()
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/run.test.ts`
Expected: FAIL（找不到 `../src/run.ts`）。

- [ ] **Step 3: 写 `src/run.ts`**

```ts
import { appendFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { isAbsolute, join, resolve } from 'node:path'
import type { WorldConfig } from './config.ts'
import { loadCalendar, computeTradingDates } from './calendar.ts'
import { mapWithConcurrency } from './concurrency.ts'
import { BotServer } from './botServer.ts'
import { buildShadowWorkspace } from './shadowWorkspace.ts'
import { renderDailyMessage } from './message.ts'
import { MemoryStore } from './memory-server/store.ts'
import { createMemoryServer, type MemoryServerHandle } from './memory-server/server.ts'
import { readState, writeState, type WorldState } from './state.ts'
import * as P from './paths.ts'

export type StartBotServer = (botId: string, argv: string[]) => Promise<BotServer>

export interface RunWorldOptions {
  worldRoot: string
  config: WorldConfig
  runId: string
  startBotServer?: StartBotServer
}

const SESSION_KEY = (runId: string) => `trading-${runId}`
const JOURNAL_REL = 'memory/trading/journal.md'

export function botServerArgv(config: WorldConfig, botId: string, workspace: string, rlConfigPath: string): string[] {
  const serverEntry = join(config.researchLoopTs, 'server.ts')
  return [process.execPath, '--experimental-strip-types', serverEntry, '--bot-id', botId, '--workspace', workspace, '--config', rlConfigPath]
}

function log(worldRoot: string, runId: string, msg: string): void {
  const line = `${new Date().toISOString()} ${msg}\n`
  appendFileSync(P.runLogFile(worldRoot, runId), line)
  process.stdout.write(`[world ${runId}] ${msg}\n`)
}

function generateRlConfig(config: WorldConfig, worldRoot: string, runId: string, memoryUrl: string): void {
  let base: Record<string, unknown>
  try { base = JSON.parse(readFileSync(config.rlConfigBase, 'utf8')) as Record<string, unknown> }
  catch (err) { throw new Error(`cannot read rl_config_base ${config.rlConfigBase}: ${err instanceof Error ? err.message : String(err)}`) }
  const mcp = (typeof base.mcp === 'object' && base.mcp ? base.mcp : {}) as Record<string, unknown>
  mcp.mem0 = memoryUrl
  base.mcp = mcp
  base.openclaw_dir = config.rlOpenclawDir
  writeFileSync(P.runConfigFile(worldRoot, runId), JSON.stringify(base, null, 2) + '\n')
}

interface SetupResult {
  tradingDates: string[]
  memory: MemoryServerHandle
  bots: { botId: string; server: BotServer }[]
  getCurrentDate: () => string
  currentDateRef: { value: string }
}

async function setup(opts: RunWorldOptions): Promise<SetupResult> {
  const { worldRoot, config, runId } = opts
  const startBotServer = opts.startBotServer ?? ((botId, argv) => BotServer.start(botId, { argv, readyTimeoutMs: 60_000, onLog: (l) => process.stderr.write(l + '\n') }))

  // 交易日序列
  const cal = loadCalendar(config.calendar)
  const tradingDates = computeTradingDates(cal, config.replay.from, config.replay.to)

  // 校验数据齐全
  const missing = tradingDates.filter(d => !existsSync(P.quotesFile(worldRoot, d)))
  if (missing.length) throw new Error(`missing quotes.json for ${missing.length} trading day(s): ${missing.slice(0, 5).join(', ')}${missing.length > 5 ? ', …' : ''}`)

  // 目录
  mkdirSync(P.runDir(worldRoot, runId), { recursive: true })
  mkdirSync(P.memoryDir(worldRoot, runId), { recursive: true })
  mkdirSync(P.workspacesDir(worldRoot, runId), { recursive: true })
  mkdirSync(config.rlOpenclawDir, { recursive: true })
  log(worldRoot, runId, `setup: ${tradingDates.length} trading days ${tradingDates[0]} .. ${tradingDates[tradingDates.length - 1]}, bots=[${config.bots.join(', ')}]`)

  // 记忆服务（进程内）
  const currentDateRef = { value: tradingDates[0] }
  const getCurrentDate = () => currentDateRef.value
  const store = new MemoryStore(P.memoryStoreFile(worldRoot, runId))
  const memory = await createMemoryServer({ store, getCurrentDate })
  writeFileSync(P.memoryRuntimeFile(worldRoot, runId), JSON.stringify({ port: memory.port, url: memory.url, collection: 'trading-memories' }, null, 2) + '\n')
  log(worldRoot, runId, `memory server at ${memory.url}`)

  // 生成 rl-config
  generateRlConfig(config, worldRoot, runId, memory.url)

  // 影子 workspace + bot server
  const bots: { botId: string; server: BotServer }[] = []
  try {
    for (const botId of config.bots) {
      const srcWs = isAbsolute(botId) ? botId : join(config.workspaceRoot, `workspace-${botId}`)
      if (!existsSync(srcWs)) throw new Error(`source workspace not found for ${botId}: ${srcWs}`)
      const shadow = P.shadowWorkspaceDir(worldRoot, runId, botId)
      buildShadowWorkspace({ sourceDir: srcWs, destDir: shadow, include: config.shadowInclude })
      const argv = botServerArgv(config, botId, shadow, P.runConfigFile(worldRoot, runId))
      const server = await startBotServer(botId, argv)
      bots.push({ botId, server })
      log(worldRoot, runId, `bot ${botId}: server ready`)
    }
  } catch (err) {
    // 启动阶段失败：关掉已起的 bot server + 记忆服务
    for (const b of bots) { try { await b.server.shutdown({ timeoutMs: 2000 }) } catch { /* ignore */ } }
    try { await memory.close() } catch { /* ignore */ }
    throw err
  }

  return { tradingDates, memory, bots, getCurrentDate, currentDateRef }
}

interface DayBotStatus { bot: string; status: 'ok' | 'error' | 'timeout' | 'dead'; iterations?: number; usage?: number; ms: number; error?: string }

async function chatOneBot(worldRoot: string, runId: string, date: string, message: string, perBotTimeoutMs: number, b: { botId: string; server: BotServer }): Promise<DayBotStatus> {
  const dir = P.botDayDir(worldRoot, runId, date, b.botId)
  mkdirSync(dir, { recursive: true })
  writeFileSync(P.sentFile(worldRoot, runId, date, b.botId), message)
  const startedAt = Date.now()
  const writeStatus = (s: DayBotStatus) => writeFileSync(P.statusFile(worldRoot, runId, date, b.botId), JSON.stringify({ status: s.status, started_at: new Date(startedAt).toISOString(), finished_at: new Date().toISOString(), ...(s.error ? { error: s.error } : {}) }, null, 2) + '\n')
  if (!b.server.alive) { const s: DayBotStatus = { bot: b.botId, status: 'dead', ms: 0, error: 'server process not alive' }; writeStatus(s); return s }
  try {
    const r = await b.server.chat({ message, session_key: SESSION_KEY(runId), history: [] }, { timeoutMs: perBotTimeoutMs })
    writeFileSync(P.replyFile(worldRoot, runId, date, b.botId), JSON.stringify(r, null, 2) + '\n')
    const s: DayBotStatus = { bot: b.botId, status: 'ok', iterations: r.iterations, usage: r.usage, ms: Date.now() - startedAt }
    writeStatus(s)
    return s
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    const status: DayBotStatus['status'] = /timeout/i.test(msg) ? 'timeout' : !b.server.alive ? 'dead' : 'error'
    const s: DayBotStatus = { bot: b.botId, status, ms: Date.now() - startedAt, error: msg }
    writeStatus(s)
    return s
  }
}

interface DaySummary { date: string; bots: DayBotStatus[] }

async function teardown(worldRoot: string, runId: string, setupRes: SetupResult, finalStatus: WorldState['status'], days: DaySummary[]): Promise<void> {
  for (const b of setupRes.bots) { try { await b.server.shutdown({ timeoutMs: 5000 }) } catch { /* ignore */ } }
  try { await setupRes.memory.close() } catch { /* ignore */ }
  writeFileSync(P.summaryFile(worldRoot, runId), JSON.stringify({ run_id: runId, status: finalStatus, days, finished_at: new Date().toISOString() }, null, 2) + '\n')
  const state = readState(worldRoot)
  writeState(worldRoot, { ...state, status: finalStatus, updated_at: new Date().toISOString() })
  // 也把最终 state 复制进 run 目录存档
  writeFileSync(join(P.runDir(worldRoot, runId), 'state.json'), JSON.stringify({ ...state, status: finalStatus }, null, 2) + '\n')
  log(worldRoot, runId, `teardown: status=${finalStatus}`)
}

export async function runWorld(opts: RunWorldOptions): Promise<void> {
  const { worldRoot, config, runId } = opts
  let setupRes: SetupResult
  try {
    setupRes = await setup(opts)
  } catch (err) {
    // 尽量记录 failed（state 可能还没建）
    try { mkdirSync(worldRoot, { recursive: true }); if (existsSync(P.stateFile(worldRoot))) { const s = readState(worldRoot); writeState(worldRoot, { ...s, status: 'failed', updated_at: new Date().toISOString() }) } } catch { /* ignore */ }
    throw err
  }

  const initial: WorldState = {
    run_id: runId, status: 'running', current_date: setupRes.tradingDates[0],
    trading_dates: setupRes.tradingDates, cursor: 0, bots: config.bots,
    memory_port: setupRes.memory.port, started_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  }
  writeState(worldRoot, initial)

  await runLoop({ worldRoot, runId, config, setupRes, fromCursor: 0 })
}

interface RunLoopArgs { worldRoot: string; runId: string; config: WorldConfig; setupRes: SetupResult; fromCursor: number }

export async function runLoop(args: RunLoopArgs): Promise<void> {
  const { worldRoot, runId, config, setupRes, fromCursor } = args
  const dates = setupRes.tradingDates
  const days: DaySummary[] = []
  const perBotTimeoutMs = config.perBotTimeoutSeconds * 1000
  let aborted = false
  const onSigint = () => { aborted = true; log(worldRoot, runId, 'SIGINT received — will abort after current day') }
  process.on('SIGINT', onSigint)
  try {
    for (let cursor = fromCursor; cursor < dates.length; cursor++) {
      if (aborted || existsSync(P.stopFile(worldRoot, runId))) { log(worldRoot, runId, 'stop requested — aborting'); await teardown(worldRoot, runId, setupRes, 'aborted', days); return }
      const date = dates[cursor]
      setupRes.currentDateRef.value = date
      const state = readState(worldRoot)
      writeState(worldRoot, { ...state, current_date: date, updated_at: new Date().toISOString() })
      log(worldRoot, runId, `day ${cursor + 1}/${dates.length}: ${date} — sending to ${config.bots.length} bot(s)`)
      const isFirstDay = cursor === 0
      const quotesAbs = resolve(P.quotesFile(worldRoot, date))
      const statuses = await mapWithConcurrency(setupRes.bots, config.concurrency, async (b) => {
        const message = renderDailyMessage({ worldRoot, date, isFirstDay, quotesPath: quotesAbs, journalRelPath: JOURNAL_REL })
        return chatOneBot(worldRoot, runId, date, message, perBotTimeoutMs, b)
      })
      days.push({ date, bots: statuses })
      log(worldRoot, runId, `day ${date} done: ${statuses.map(s => `${s.bot}=${s.status}`).join(' ')}`)
      const st = readState(worldRoot)
      writeState(worldRoot, { ...st, cursor: cursor + 1, updated_at: new Date().toISOString() })
    }
    await teardown(worldRoot, runId, setupRes, 'done', days)
  } catch (err) {
    log(worldRoot, runId, `loop error: ${err instanceof Error ? err.message : String(err)}`)
    await teardown(worldRoot, runId, setupRes, 'failed', days)
    throw err
  } finally {
    process.off('SIGINT', onSigint)
  }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/run.test.ts`
Expected: PASS（3 个测试）。

- [ ] **Step 5: 提交**

```bash
git add src/run.ts test/run.test.ts
git commit -m "feat: world orchestrator — setup, daily loop, teardown"
```

---

## Task 17: 续跑与停止 — `resumeWorld` + STOP sentinel

**Files:**
- Modify: `src/run.ts`（新增 `resumeWorld`，复用 `setup`/`runLoop`；并暴露 `requestStop`）
- Test: `test/resume.test.ts`

- [ ] **Step 1: 写失败测试 `test/resume.test.ts`**

```ts
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { runWorld, resumeWorld } from '../src/run.ts'
import { BotServer } from '../src/botServer.ts'
import * as P from '../src/paths.ts'
import { readState, writeState } from '../src/state.ts'
import type { WorldConfig } from '../src/config.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const STUB = join(HERE, 'helpers', 'stubBotServer.ts')
const stubStart = (botId: string) => BotServer.start(botId, { argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`], readyTimeoutMs: 5000 })

function setupWorldDir(bots: string[], dates: string[]) {
  const root = mkdtempSync(join(tmpdir(), 'world-resume-'))
  const worldRoot = join(root, 'world')
  mkdirSync(worldRoot, { recursive: true })
  writeFileSync(P.calendarFile(worldRoot), JSON.stringify({ trading_days: dates }))
  for (const d of dates) { mkdirSync(P.dayDir(worldRoot, d), { recursive: true }); writeFileSync(P.quotesFile(worldRoot, d), JSON.stringify({ summary: d })); writeFileSync(P.overviewFile(worldRoot, d), `ov ${d}`) }
  const wsRoot = join(root, 'workspaces')
  for (const b of bots) { mkdirSync(join(wsRoot, `workspace-${b}`), { recursive: true }); writeFileSync(join(wsRoot, `workspace-${b}`, 'SOUL.md'), b) }
  const cfgBase = join(root, 'base.json'); writeFileSync(cfgBase, JSON.stringify({ mcp: { servers: {} } }))
  const config: WorldConfig = { researchLoopTs: '/no', workspaceRoot: wsRoot, bots, replay: { from: dates[0], to: dates[dates.length - 1] }, calendar: P.calendarFile(worldRoot), concurrency: 4, perBotTimeoutSeconds: 30, rlConfigBase: cfgBase, rlOpenclawDir: join(root, 'oc'), shadowInclude: ['SOUL.md'] }
  return { worldRoot, config, cleanup: () => rmSync(root, { recursive: true, force: true }) }
}

test('resumeWorld continues from state.cursor without re-running completed days', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir(['bot1'], ['2024-03-14', '2024-03-15', '2024-03-18'])
  // 先正常跑一遍但人为把它"中断"在第 1 天后：直接 runWorld 跑完，然后改 state 模拟中断
  await runWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) })
  // 模拟"只完成了 1 天、状态 running"的中断态：删掉 day2/day3 的产物，回退 cursor
  rmSync(P.botDayDir(worldRoot, 'r1', '2024-03-15', 'bot1'), { recursive: true, force: true })
  rmSync(P.botDayDir(worldRoot, 'r1', '2024-03-18', 'bot1'), { recursive: true, force: true })
  const s = readState(worldRoot); writeState(worldRoot, { ...s, status: 'running', cursor: 1, current_date: '2024-03-14' })

  await resumeWorld({ worldRoot, config, startBotServer: (b) => stubStart(b) })
  const st = readState(worldRoot)
  assert.equal(st.status, 'done')
  assert.equal(st.cursor, 3)
  assert.ok(existsSync(P.replyFile(worldRoot, 'r1', '2024-03-15', 'bot1')))
  assert.ok(existsSync(P.replyFile(worldRoot, 'r1', '2024-03-18', 'bot1')))
  cleanup()
})

test('resumeWorld refuses when state status is not running', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir(['bot1'], ['2024-03-14'])
  await runWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) }) // status=done
  await assert.rejects(() => resumeWorld({ worldRoot, config, startBotServer: (b) => stubStart(b) }), /not running|nothing to resume/i)
  cleanup()
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/resume.test.ts`
Expected: FAIL（`resumeWorld` 未导出）。

- [ ] **Step 3: 在 `src/run.ts` 末尾新增 `resumeWorld` 和 `requestStop`**

```ts
export interface ResumeWorldOptions {
  worldRoot: string
  config: WorldConfig
  startBotServer?: StartBotServer
}

export async function resumeWorld(opts: ResumeWorldOptions): Promise<void> {
  const { worldRoot, config } = opts
  const state = readState(worldRoot)
  if (state.status !== 'running') throw new Error(`cannot resume: state status is "${state.status}", nothing to resume`)
  // 清掉可能残留的 STOP 哨兵（否则 resume 会立刻被它中止）
  rmSync(P.stopFile(worldRoot, state.run_id), { force: true })
  // setup（重启记忆服务、重建/复用影子 workspace、重起 bot server），但 trading_dates 取自 state
  const setupRes = await setup({ worldRoot, config, runId: state.run_id, startBotServer: opts.startBotServer })
  // 若 calendar/replay 变了导致交易日序列对不上，拒绝
  if (setupRes.tradingDates.length !== state.trading_dates.length || setupRes.tradingDates[0] !== state.trading_dates[0] || setupRes.tradingDates[setupRes.tradingDates.length - 1] !== state.trading_dates[state.trading_dates.length - 1]) {
    for (const b of setupRes.bots) { try { await b.server.shutdown({ timeoutMs: 2000 }) } catch { /* ignore */ } }
    try { await setupRes.memory.close() } catch { /* ignore */ }
    throw new Error('resume: trading-date sequence changed since the run started; refuse to resume')
  }
  setupRes.currentDateRef.value = state.trading_dates[Math.min(state.cursor, state.trading_dates.length - 1)]
  await runLoop({ worldRoot, runId: state.run_id, config, setupRes, fromCursor: state.cursor })
}

/** 由独立的 `world stop` 进程调用：写一个 STOP 哨兵，正在跑的 runLoop 会在下一天开始前发现它。 */
export function requestStop(worldRoot: string): { ok: boolean; reason?: string } {
  if (!existsSync(P.stateFile(worldRoot))) return { ok: false, reason: 'no state.json' }
  const state = readState(worldRoot)
  if (state.status !== 'running') return { ok: false, reason: `state status is "${state.status}"` }
  mkdirSync(P.runDir(worldRoot, state.run_id), { recursive: true })
  writeFileSync(P.stopFile(worldRoot, state.run_id), `requested at ${new Date().toISOString()}\n`)
  return { ok: true }
}
```

> 注意：`runWorld` 进行新 run 前，CLI 层（Task 18）会先检查 `world/state.json` 是否存在且 status==='running' → 若是则拒绝（提示用 `resume` 或 `stop`）；否则若存在旧 state（done/failed/aborted），CLI 会先把它存档到 `runs/<old_run_id>/state.json.prev`（若该文件尚不存在）再让 `runWorld` 覆盖。`runWorld` 本身不做这个检查（保持纯粹），由 CLI 负责。

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/resume.test.ts`
Expected: PASS（2 个测试）。

- [ ] **Step 5: 跑全量测试**

Run: `npm test`
Expected: 所有测试通过。

- [ ] **Step 6: 提交**

```bash
git add src/run.ts test/resume.test.ts
git commit -m "feat: resumeWorld and stop-sentinel support"
```

---

## Task 18: CLI — `src/cli.ts` + `main.ts`

**Files:**
- Modify: `src/cli.ts`（替换 Task 1 的占位实现）
- Test: `test/cli.test.ts`

- [ ] **Step 1: 写失败测试 `test/cli.test.ts`（只测 arg 解析）**

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseCliArgs } from '../src/cli.ts'

test('parseCliArgs: run with config + run-id + world-dir', () => {
  assert.deepEqual(parseCliArgs(['run', '--config', 'config/world.yaml', '--run-id', 'r1', '--world-dir', '/tmp/w']),
    { command: 'run', config: 'config/world.yaml', runId: 'r1', worldDir: '/tmp/w' })
})

test('parseCliArgs: run without run-id (derived later)', () => {
  assert.deepEqual(parseCliArgs(['run', '--config', 'c.yaml']), { command: 'run', config: 'c.yaml', runId: undefined, worldDir: undefined })
})

test('parseCliArgs: resume / status / stop', () => {
  assert.deepEqual(parseCliArgs(['resume', '--config', 'c.yaml']), { command: 'resume', config: 'c.yaml', runId: undefined, worldDir: undefined })
  assert.deepEqual(parseCliArgs(['status', '--world-dir', '/w']), { command: 'status', config: undefined, runId: undefined, worldDir: '/w' })
  assert.deepEqual(parseCliArgs(['stop']), { command: 'stop', config: undefined, runId: undefined, worldDir: undefined })
})

test('parseCliArgs: help and unknown', () => {
  assert.deepEqual(parseCliArgs(['--help']), { command: 'help', config: undefined, runId: undefined, worldDir: undefined })
  assert.deepEqual(parseCliArgs([]), { command: 'help', config: undefined, runId: undefined, worldDir: undefined })
  assert.deepEqual(parseCliArgs(['frobnicate']), { command: 'unknown', config: undefined, runId: undefined, worldDir: undefined })
})

test('parseCliArgs: run requires --config (validated by caller, but parser still returns it absent)', () => {
  assert.deepEqual(parseCliArgs(['run']), { command: 'run', config: undefined, runId: undefined, worldDir: undefined })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node --experimental-strip-types --test test/cli.test.ts`
Expected: FAIL（`parseCliArgs` 未导出）。

- [ ] **Step 3: 写 `src/cli.ts`（完整版）**

```ts
import { existsSync, mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { loadWorldConfig } from './config.ts'
import { runWorld, resumeWorld, requestStop } from './run.ts'
import { readState, stateExists } from './state.ts'
import * as P from './paths.ts'

export interface CliArgs {
  command: 'run' | 'resume' | 'status' | 'stop' | 'help' | 'unknown'
  config?: string
  runId?: string
  worldDir?: string
}

const HELP = `world — agent_invest_lab 日度滚动金融世界系统

用法:
  world run    --config <world.yaml> [--run-id <id>] [--world-dir <dir>]   开新 run（默认 --world-dir ./world）
  world resume --config <world.yaml> [--world-dir <dir>]                   从 state.json.cursor 续跑
  world status [--world-dir <dir>]                                        查看进度
  world stop   [--world-dir <dir>]                                        请求优雅终止当前 run
  world --help
`

export function parseCliArgs(argv: string[]): CliArgs {
  if (argv.length === 0 || argv[0] === '-h' || argv[0] === '--help') return { command: 'help' }
  const cmd = argv[0]
  const valueOf = (name: string): string | undefined => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : undefined }
  const base: CliArgs = { command: 'unknown', config: valueOf('--config'), runId: valueOf('--run-id'), worldDir: valueOf('--world-dir') }
  if (cmd === 'run' || cmd === 'resume' || cmd === 'status' || cmd === 'stop') return { ...base, command: cmd }
  return base
}

function resolveWorldRoot(args: CliArgs): string {
  return resolve(args.worldDir ?? join(process.cwd(), 'world'))
}

function deriveRunId(): string {
  return `run-${new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)}`
}

async function cmdRun(args: CliArgs): Promise<number> {
  if (!args.config) { process.stderr.write('run: --config <world.yaml> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  mkdirSync(worldRoot, { recursive: true })
  if (stateExists(worldRoot)) {
    const st = readState(worldRoot)
    if (st.status === 'running') { process.stderr.write(`run: a run "${st.run_id}" is already running (status=running). Use "world resume" or "world stop" first.\n`); return 2 }
  }
  const config = loadWorldConfig(args.config)
  const runId = args.runId ?? deriveRunId()
  await runWorld({ worldRoot, config, runId })
  return 0
}

async function cmdResume(args: CliArgs): Promise<number> {
  if (!args.config) { process.stderr.write('resume: --config <world.yaml> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  if (!stateExists(worldRoot)) { process.stderr.write(`resume: no state.json under ${worldRoot}\n`); return 2 }
  const config = loadWorldConfig(args.config)
  await resumeWorld({ worldRoot, config })
  return 0
}

function cmdStatus(args: CliArgs): number {
  const worldRoot = resolveWorldRoot(args)
  if (!stateExists(worldRoot)) { process.stdout.write(`(no run under ${worldRoot})\n`); return 0 }
  const st = readState(worldRoot)
  process.stdout.write(`run_id:       ${st.run_id}\nstatus:       ${st.status}\ncurrent_date: ${st.current_date}\nprogress:     ${st.cursor}/${st.trading_dates.length} trading days\nbots:         ${st.bots.join(', ')}\nmemory_port:  ${st.memory_port}\nstarted_at:   ${st.started_at}\nupdated_at:   ${st.updated_at}\n`)
  if (existsSync(P.summaryFile(worldRoot, st.run_id))) process.stdout.write(`summary:      ${P.summaryFile(worldRoot, st.run_id)}\n`)
  return 0
}

function cmdStop(args: CliArgs): number {
  const worldRoot = resolveWorldRoot(args)
  const r = requestStop(worldRoot)
  if (!r.ok) { process.stderr.write(`stop: ${r.reason}\n`); return 2 }
  process.stdout.write('stop requested — the running process will abort after the current trading day.\n')
  return 0
}

export async function main(argv = process.argv.slice(2)): Promise<number> {
  const args = parseCliArgs(argv)
  switch (args.command) {
    case 'help': process.stdout.write(HELP); return 0
    case 'run': return cmdRun(args)
    case 'resume': return cmdResume(args)
    case 'status': return cmdStatus(args)
    case 'stop': return cmdStop(args)
    default: process.stderr.write(`unknown command: ${argv.join(' ')}\n\n${HELP}`); return 2
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().then(code => { process.exitCode = code }).catch(err => { process.stderr.write((err instanceof Error ? err.stack || err.message : String(err)) + '\n'); process.exitCode = 1 })
}
```

> 注意：`main.ts` 已经是 `import './src/cli.ts'`；上面的 `if (import.meta.url === ...)` 守卫保证直接 `node main.ts` 时会执行 `main()`。`main.ts` 内容不变（Task 1 已写好）。

- [ ] **Step 4: 跑测试确认通过**

Run: `node --experimental-strip-types --test test/cli.test.ts`
Expected: PASS。

- [ ] **Step 5: 手动验证 CLI**

Run: `node --experimental-strip-types main.ts --help && node --experimental-strip-types main.ts status --world-dir /tmp/does-not-exist`
Expected: 打印用法；`status` 对不存在的目录打印 `(no run under /tmp/does-not-exist)`，退出码 0。

- [ ] **Step 6: 提交**

```bash
git add src/cli.ts test/cli.test.ts
git commit -m "feat: world CLI — run / resume / status / stop"
```

---

## Task 19: 配置模板 + 最终联调

**Files:**
- Create: `config/trading-rl-config.base.json`
- Create: `config/world.example.yaml`

- [ ] **Step 1: 写 `config/trading-rl-config.base.json`**

> 这是给 research-loop-ts 用的 rl-config 基底。关键点：**不包含** yfinance / ttjj-data / zsxq 等"实时数据"MCP server（回放沙盘里用它们 = 看未来 / 看错时代的数据）；`dashboard.enabled=false`（多个 bot server 同时跑会抢 18890 端口）；`browser.enabled=false`（默认关，需要的话改这里）；`memory-mem0.enabled=true`（world 会注入 `mcp.mem0` 指向 run 专属关键词记忆服务）。`mcp.servers` 留空 `{}`，交易引擎团队在这里加他们的交易 MCP。`openclaw_dir` 由 world 在生成时覆盖为 `rl_openclaw_dir`，这里写默认值。

```json
{
  "model": {
    "primary": {
      "provider": "openai_compatible",
      "base_url": "https://dd-ai-api.eastmoney.com/v1",
      "model": "qwen3.5-plus",
      "api_key_from_openclaw": "zai-coding-plan"
    }
  },
  "llm": {
    "chat_max_tokens": 8000,
    "chat_temperature": 0.3,
    "chat_max_tool_iterations": 100,
    "skill_max_tokens": 4000,
    "skill_temperature": 0.3,
    "compact_max_tokens": 2000,
    "compact_temperature": 0.1
  },
  "timeouts": { "chat_llm": 180, "compact_llm": 120, "skill_llm": 60 },
  "openclaw_dir": "/home/rooot/.openclaw",
  "mcp": {
    "servers": {}
  },
  "plugins": {
    "browser": { "enabled": false },
    "memory-mem0": { "enabled": true }
  },
  "extra_roots": ["/home/rooot/.openclaw/workspace/skills"],
  "limits": {
    "max_total_turns": 25,
    "max_time_minutes": 30,
    "token_budget": 900000,
    "compact_threshold_pct": 60,
    "min_sources_for_dig": 3,
    "context_window": 1000000
  },
  "dashboard": { "enabled": false }
}
```

- [ ] **Step 2: 写 `config/world.example.yaml`**

```yaml
# agent_invest_lab 世界配置示例。复制为 config/world.yaml 后按需修改。
# 用法: node --experimental-strip-types main.ts run --config config/world.yaml

# research-loop-ts 仓库路径（agent 大脑）
research_loop_ts: /home/rooot/.openclaw/research-loop-ts

# workspace-botN 的父目录（agent 人设/性格来源；world 会精选复制成每个 run 的影子 workspace）
workspace_root: /home/rooot/.openclaw

# 参与本世界的 bot 列表
bots: [bot1, bot7, bot9]

# 回放区间（含端点；非交易日会被交易日历过滤掉）
replay:
  from: "2024-01-02"
  to:   "2024-06-28"

# 交易日历 JSON：{ "trading_days": ["2024-01-02", ...] }（升序、YYYY-MM-DD）。
# 默认相对本文件 → ./calendar.json；world 只消费它、不生成它（由数据团队/人工提供）。
# calendar: /path/to/calendar.json

# 同一天里并发跑多少个 bot
concurrency: 4

# 单 bot 单日 chat 超时（秒）；超时记 status=timeout，不阻塞当天其它 bot、不阻塞翻篇
per_bot_timeout_seconds: 1200

# 给 research-loop-ts 用的 rl-config 基底；默认相对本文件 → ../config/trading-rl-config.base.json
# rl_config_base: /path/to/trading-rl-config.base.json

# research-loop-ts 的 openclaw_dir（用于解析模型 API key 等）。保持默认即可；
# 若想让 bot 的 session 存档也隔离，指向一个你自建了 openclaw.json 的影子目录。
# rl_openclaw_dir: /home/rooot/.openclaw

# 影子 workspace 从 workspace-botN 复制哪些条目（默认见 src/config.ts DEFAULT_SHADOW_INCLUDE）
# shadow_include: [IDENTITY.md, SOUL.md, AGENTS.md, USER.md, METHODOLOGY.md, RESEARCH.md, MEMORY.md, EQUIPPED_SKILLS.md, TOOLS.md, skills, config]
```

- [ ] **Step 3: 跑全量测试 + check**

Run: `npm test && npm run check`
Expected: 所有测试通过；`world --help` 正常；`tsc --noEmit` 无错误。

- [ ] **Step 4: 提交**

```bash
git add config/trading-rl-config.base.json config/world.example.yaml
git commit -m "chore: ship trading rl-config base and example world.yaml"
```

---

## 完成判据

- `npm test` 全绿；`npm run check` 通过。
- `world --help` / `world status` 可用。
- e2e 测试（`test/run.test.ts`）能用 stub bot server 完整跑 2 交易日 × 2 bot 的回放，产出 `runs/<run_id>/` 下的 `sent.md` / `reply.json` / `status.json` / `run.log` / `summary.json` / `state.json`、生成的 `trading-rl-config.json`（含注入的 `mcp.mem0`）、`memory/store.jsonl`、影子 workspace。
- `resumeWorld` 能从 `state.cursor` 续跑；`world stop` 写 STOP 哨兵让运行中的进程在下一天前优雅终止；超时/报错的 bot 被记录但不阻塞。

## 留给后续/外部团队（不在本计划范围）

- **数据团队**：往 `world/days/<date>/quotes.json` 放当天行情（schema 自定；想要自动概览就提供 `overview.md`，或让 `quotes.json` 顶层带 `summary` 字符串或 `indices: [{name, change_pct}]`）；可选 `events.json`（数组或 `{events:[...]}`，元素是字符串或带 `title`/`name` 的对象）。提供 `world/calendar.json`。
- **交易引擎团队**：写一个交易 MCP server，读 `world/state.json.current_date` 知道"今天"；把它的地址填进 `config/trading-rl-config.base.json` 的 `mcp.servers`；持仓/订单存哪自定（建议 `world/runs/<run_id>/portfolios/`）。
- **未来 live 模式**：把 `runLoop` 的"单日 step"拆出来由 systemd 定时器驱动（参考现有 `trading-daily-close.timer`）。
- **真实端到端**：用真实 research-loop-ts + 真实交易 MCP + 真实行情数据各跑一遍小回放，验证 MCP 发现、API key 解析、影子 workspace 下的 session 写入路径符合预期。
