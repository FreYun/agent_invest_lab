# world run 暂停 / 续跑 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `world` 编排器加「暂停(可续)」语义,让优雅停止的 run 也能 resume,修正当前「崩溃残留可续、`world stop` 反而不可续」的拧巴状态机。

**Architecture:** 新增 `paused` 状态;`world pause` 写 PAUSE 哨兵,runLoop 用一个 1s poller 轮询该哨兵——发现即 shutdown 所有 bot server(解除 in-flight chat),循环在推进 cursor 前 `teardown('paused')`,**立即终止当前交易日**(不等当天跑完);`resumeWorld` 放宽门禁接受 `running|paused` 并在续跑前清掉 STOP/PAUSE 哨兵,`fromCursor=state.cursor` 即从被中断那天整段重跑;`world stop`/SIGINT 维持原语义(等当天跑完→`aborted` 终态),`world stop` 对已 `paused` 的 run 直接翻成 `aborted`。改动面纯在 `world/`(state / paths / run / cli),无 dashboard / 脚本依赖。

> **注**:Task 3 的最终实现与下文初稿不同——改为 poller 立即杀当天(见 Architecture 与设计 spec 的「立即暂停」节),而非"等日界"。其余 Task 与初稿一致。

**Tech Stack:** TypeScript (Node ≥22.6 `--experimental-strip-types`),`node:test` 测试,无第三方测试库。

设计来源:[docs/superpowers/specs/2026-05-21-world-run-pause-resume-design.md](../specs/2026-05-21-world-run-pause-resume-design.md)

**测试命令约定**(在 `world/` 目录下执行):
- 全量:`npm test`
- 单文件:`node --experimental-strip-types --test test/run.test.ts`
- 按名过滤:`node --experimental-strip-types --test --test-name-pattern "<pattern>" test/run.test.ts`
- 类型检查 + CLI 自检:`npm run check`

---

### Task 1: 状态枚举 + PAUSE 哨兵路径(scaffolding)

纯类型/常量改动,没有独立行为测试;用 `npm run check` 验证编译通过。

**Files:**
- Modify: `world/src/state.ts`(`RunStatus` 联合类型)
- Modify: `world/src/paths.ts`(新增 `pauseFile`)

- [ ] **Step 1: 给 `RunStatus` 加 `'paused'`**

`world/src/state.ts`,把:

```ts
export type RunStatus = 'setup' | 'running' | 'done' | 'failed' | 'aborted'
```

改为:

```ts
export type RunStatus = 'setup' | 'running' | 'paused' | 'done' | 'failed' | 'aborted'
```

- [ ] **Step 2: 新增 `pauseFile` 路径**

`world/src/paths.ts`,在 `stopFile` 那一行下面新增 `pauseFile`(与 STOP 并列):

```ts
export const stopFile = (w: string, runId: string) => join(runDir(w, runId), 'STOP')
// PAUSE 哨兵：world pause 写它，runLoop 在交易日界检测到 → teardown 'paused'(可 resume)。
// 与 STOP 区分：STOP → aborted(终态)，PAUSE → paused(可续)。resume 时两者都清。
export const pauseFile = (w: string, runId: string) => join(runDir(w, runId), 'PAUSE')
```

- [ ] **Step 3: 编译验证**

Run(在 `world/` 目录):`npm run check`
Expected: PASS(`--help` 正常输出 + `tsc --noEmit` 无报错)

- [ ] **Step 4: Commit**

```bash
git add world/src/state.ts world/src/paths.ts
git commit -m "feat(world): add paused status + PAUSE sentinel path"
```

---

### Task 2: `requestPause` + `requestStop` 接受 paused

**Files:**
- Modify: `world/src/run.ts`(新增 `requestPause`,改 `requestStop`)
- Test: `world/test/run.test.ts`

- [ ] **Step 1: 写失败测试**

`world/test/run.test.ts`,先更新顶部 import:

把:

```ts
import { runWorld, botServerArgv, openclawJsonSource, loopConfigPath, patchPiOpenclawJsonMemory, seedPiAgentBot, isResearchDay, proxyEnvSupplement } from '../src/run.ts'
```

改为:

```ts
import { runWorld, requestPause, requestStop, botServerArgv, openclawJsonSource, loopConfigPath, patchPiOpenclawJsonMemory, seedPiAgentBot, isResearchDay, proxyEnvSupplement } from '../src/run.ts'
```

把:

```ts
import { readState } from '../src/state.ts'
```

改为:

```ts
import { readState, writeState } from '../src/state.ts'
```

然后在文件末尾追加两个测试:

```ts
test('requestPause writes a PAUSE sentinel when running, rejects otherwise', () => {
  const { worldRoot, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14', '2024-03-15'] })
  writeState(worldRoot, 'rp', { run_id: 'rp', status: 'running', current_date: '2024-03-14', trading_dates: ['2024-03-14', '2024-03-15'], cursor: 1, bots: ['bot1'], memory_port: 0, started_at: new Date().toISOString(), updated_at: new Date().toISOString(), loop: 'research-loop' })
  const r = requestPause(worldRoot, 'rp')
  assert.equal(r.ok, true)
  assert.ok(existsSync(P.pauseFile(worldRoot, 'rp')))
  // 非 running → 拒绝
  writeState(worldRoot, 'rp', { ...readState(worldRoot, 'rp'), status: 'done' })
  assert.equal(requestPause(worldRoot, 'rp').ok, false)
  cleanup()
})

test('requestStop on a paused run flips it straight to aborted (no STOP sentinel)', () => {
  const { worldRoot, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14'] })
  writeState(worldRoot, 'rps', { run_id: 'rps', status: 'paused', current_date: '2024-03-14', trading_dates: ['2024-03-14'], cursor: 0, bots: ['bot1'], memory_port: 0, started_at: new Date().toISOString(), updated_at: new Date().toISOString(), loop: 'research-loop' })
  const r = requestStop(worldRoot, 'rps')
  assert.equal(r.ok, true)
  assert.equal(readState(worldRoot, 'rps').status, 'aborted')
  assert.equal(existsSync(P.stopFile(worldRoot, 'rps')), false)
  cleanup()
})
```

- [ ] **Step 2: 跑测试确认失败**

Run:`node --experimental-strip-types --test --test-name-pattern "requestPause|requestStop on a paused" test/run.test.ts`
Expected: FAIL(`requestPause` 未导出 / paused 分支不存在)

- [ ] **Step 3: 实现 `requestPause` + 改 `requestStop`**

`world/src/run.ts`,把现有 `requestStop` 整段:

```ts
/** 由独立的 `world stop` 进程调用：写一个 STOP 哨兵，正在跑的 runLoop 会在下一天开始前发现它。 */
export function requestStop(worldRoot: string, runId: string): { ok: boolean; reason?: string } {
  if (!existsSync(P.runStateFile(worldRoot, runId))) return { ok: false, reason: `no state.json for run ${runId}` }
  const state = readState(worldRoot, runId)
  if (state.status !== 'running') return { ok: false, reason: `state status is "${state.status}"` }
  mkdirSync(P.runDir(worldRoot, runId), { recursive: true })
  writeFileSync(P.stopFile(worldRoot, runId), `requested at ${new Date().toISOString()}\n`)
  return { ok: true }
}
```

替换为:

```ts
/** 由独立的 `world stop` 进程调用。
 *  - running：写 STOP 哨兵，正在跑的 runLoop 会在下一天开始前发现它 → teardown 'aborted'。
 *  - paused：没有进程在跑、没人消费哨兵，直接把 state 翻成终态 'aborted'(取消一个暂停的 run)。 */
export function requestStop(worldRoot: string, runId: string): { ok: boolean; reason?: string } {
  if (!existsSync(P.runStateFile(worldRoot, runId))) return { ok: false, reason: `no state.json for run ${runId}` }
  const state = readState(worldRoot, runId)
  if (state.status === 'paused') {
    writeState(worldRoot, runId, { ...state, status: 'aborted', updated_at: new Date().toISOString() })
    return { ok: true }
  }
  if (state.status !== 'running') return { ok: false, reason: `state status is "${state.status}"` }
  mkdirSync(P.runDir(worldRoot, runId), { recursive: true })
  writeFileSync(P.stopFile(worldRoot, runId), `requested at ${new Date().toISOString()}\n`)
  return { ok: true }
}

/** 由独立的 `world pause` 进程调用：写一个 PAUSE 哨兵，正在跑的 runLoop 会在下一天开始前发现它，
 *  优雅停在 'paused'(可 resume)。只接受 running——已 done/aborted/paused 的没有意义。 */
export function requestPause(worldRoot: string, runId: string): { ok: boolean; reason?: string } {
  if (!existsSync(P.runStateFile(worldRoot, runId))) return { ok: false, reason: `no state.json for run ${runId}` }
  const state = readState(worldRoot, runId)
  if (state.status !== 'running') return { ok: false, reason: `state status is "${state.status}"` }
  mkdirSync(P.runDir(worldRoot, runId), { recursive: true })
  writeFileSync(P.pauseFile(worldRoot, runId), `requested at ${new Date().toISOString()}\n`)
  return { ok: true }
}
```

`writeState` 已在 run.ts 顶部从 `./state.ts` 导入(与 `readState` 同行),无需改 import。

- [ ] **Step 4: 跑测试确认通过**

Run:`node --experimental-strip-types --test --test-name-pattern "requestPause|requestStop on a paused" test/run.test.ts`
Expected: PASS(2 个测试)

- [ ] **Step 5: Commit**

```bash
git add world/src/run.ts world/test/run.test.ts
git commit -m "feat(world): add requestPause; requestStop cancels paused runs"
```

---

### Task 3: runLoop 在日界检测 PAUSE → teardown 'paused'

**Files:**
- Modify: `world/src/run.ts`(runLoop 日界检查)
- Test: `world/test/run.test.ts`

- [ ] **Step 1: 写失败测试**

`world/test/run.test.ts` 末尾追加(镜像现有 "runWorld aborts ... STOP sentinel" 测试):

```ts
test('runWorld pauses (status=paused) when a PAUSE sentinel is present', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14', '2024-03-15', '2024-03-18'] })
  const pausePath = P.pauseFile(worldRoot, 'rpause')
  mkdirSync(dirname(pausePath), { recursive: true })
  writeFileSync(pausePath, 'pause\n')
  await runWorld({ worldRoot, config, runId: 'rpause', startBotServer: stubStartBotServer })
  const st = readState(worldRoot, 'rpause')
  assert.equal(st.status, 'paused')
  // 循环在第一次迭代顶部就发现 PAUSE → cursor 停在 0，没有任何当天产物
  assert.equal(st.cursor, 0)
  assert.equal(existsSync(P.sentFile(worldRoot, 'rpause', '2024-03-14', 'bot1')), false)
  cleanup()
})
```

(`mkdirSync` / `dirname` / `writeFileSync` / `existsSync` 已在 run.test.ts 顶部导入。)

- [ ] **Step 2: 跑测试确认失败**

Run:`node --experimental-strip-types --test --test-name-pattern "runWorld pauses" test/run.test.ts`
Expected: FAIL(`st.status` 是 `done` 而非 `paused`——PAUSE 当前未被检查)

- [ ] **Step 3: 在 runLoop 加 PAUSE 检查**

`world/src/run.ts`,找到 runLoop 里的日界检查行:

```ts
      if (aborted || existsSync(P.stopFile(worldRoot, runId))) { log(worldRoot, runId, 'stop requested — aborting'); await teardown(worldRoot, runId, setupRes, 'aborted', days); return }
```

在它**下面**新增一行(abort 优先,pause 其次):

```ts
      if (existsSync(P.pauseFile(worldRoot, runId))) { log(worldRoot, runId, 'pause requested — pausing (resumable)'); await teardown(worldRoot, runId, setupRes, 'paused', days); return }
```

- [ ] **Step 4: 跑测试确认通过**

Run:`node --experimental-strip-types --test --test-name-pattern "runWorld pauses" test/run.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add world/src/run.ts world/test/run.test.ts
git commit -m "feat(world): runLoop pauses to resumable state on PAUSE sentinel"
```

---

### Task 4: `resumeWorld` 接受 paused + 续跑时清 PAUSE 哨兵

**Files:**
- Modify: `world/src/run.ts`(`resumeWorld` 门禁 + 清哨兵)
- Test: `world/test/resume.test.ts`

- [ ] **Step 1: 写失败测试 + 收紧既有拒绝测试**

`world/test/resume.test.ts`,在文件末尾追加新测试:

```ts
test('resumeWorld resumes a paused run from state.cursor and clears the PAUSE sentinel', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir(['bot1'], ['2024-03-14', '2024-03-15', '2024-03-18'])
  await runWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) })
  // 模拟「跑完第 1 天后被 pause」：删掉 day2/day3 产物，state 翻成 paused、cursor=1
  rmSync(P.botDayDir(worldRoot, 'r1', '2024-03-15', 'bot1'), { recursive: true, force: true })
  rmSync(P.botDayDir(worldRoot, 'r1', '2024-03-18', 'bot1'), { recursive: true, force: true })
  const s = readState(worldRoot, 'r1'); writeState(worldRoot, 'r1', { ...s, status: 'paused', cursor: 1, current_date: '2024-03-15' })
  // 残留 PAUSE 哨兵不能让刚 resume 的 run 立刻又停
  writeFileSync(P.pauseFile(worldRoot, 'r1'), 'pause\n')

  await resumeWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) })
  const st = readState(worldRoot, 'r1')
  assert.equal(st.status, 'done')
  assert.equal(st.cursor, 3)
  assert.equal(existsSync(P.pauseFile(worldRoot, 'r1')), false)
  assert.ok(existsSync(P.replyFile(worldRoot, 'r1', '2024-03-15', 'bot1')))
  assert.ok(existsSync(P.replyFile(worldRoot, 'r1', '2024-03-18', 'bot1')))
  cleanup()
})
```

然后把现有的:

```ts
test('resumeWorld refuses when state status is not running', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir(['bot1'], ['2024-03-14'])
  await runWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) }) // status=done
  await assert.rejects(() => resumeWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) }), /not running|nothing to resume/i)
  cleanup()
})
```

替换为(补充断言 `aborted` 也被拒,确认放宽门禁没有误放终态):

```ts
test('resumeWorld refuses when state status is a terminal (done / aborted)', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir(['bot1'], ['2024-03-14'])
  await runWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) }) // status=done
  await assert.rejects(() => resumeWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) }), /nothing to resume/i)
  const s = readState(worldRoot, 'r1'); writeState(worldRoot, 'r1', { ...s, status: 'aborted' })
  await assert.rejects(() => resumeWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) }), /nothing to resume/i)
  cleanup()
})
```

(`writeFileSync` / `readState` / `writeState` / `rmSync` / `existsSync` 已在 resume.test.ts 顶部导入。)

- [ ] **Step 2: 跑测试确认失败**

Run:`node --experimental-strip-types --test test/resume.test.ts`
Expected: FAIL(新 paused 测试报 `cannot resume: state status is "paused"`;门禁尚未放宽)

- [ ] **Step 3: 放宽门禁 + 清 PAUSE 哨兵**

`world/src/run.ts` 的 `resumeWorld`,把:

```ts
  if (state.status !== 'running') throw new Error(`cannot resume: state status is "${state.status}", nothing to resume`)
```

改为:

```ts
  if (state.status !== 'running' && state.status !== 'paused') throw new Error(`cannot resume: state status is "${state.status}", nothing to resume`)
```

并把下面这行:

```ts
  // 清掉可能残留的 STOP 哨兵（否则 resume 会立刻被它中止）
  rmSync(P.stopFile(worldRoot, runId), { force: true })
```

改为:

```ts
  // 清掉可能残留的 STOP / PAUSE 哨兵（否则 resume 会立刻被它中止/暂停）
  rmSync(P.stopFile(worldRoot, runId), { force: true })
  rmSync(P.pauseFile(worldRoot, runId), { force: true })
```

- [ ] **Step 4: 跑测试确认通过**

Run:`node --experimental-strip-types --test test/resume.test.ts`
Expected: PASS(全部用例,含新 paused-resume 与收紧后的终态拒绝)

- [ ] **Step 5: Commit**

```bash
git add world/src/run.ts world/test/resume.test.ts
git commit -m "feat(world): resumeWorld accepts paused runs and clears PAUSE on resume"
```

---

### Task 5: CLI `world pause` 命令

**Files:**
- Modify: `world/src/cli.ts`(命令解析 + handler + HELP)
- Test: `world/test/cli.test.ts`

- [ ] **Step 1: 写失败测试**

`world/test/cli.test.ts` 末尾追加:

```ts
test('parseCliArgs: pause', () => {
  assert.deepEqual(parseCliArgs(['pause', '--run-id', 'r1']),
    { command: 'pause', config: undefined, runId: 'r1', worldDir: undefined })
})

test('main: pause without --run-id exits 2', async () => {
  const errs: string[] = []
  const orig = process.stderr.write.bind(process.stderr)
  process.stderr.write = (chunk: any) => { errs.push(String(chunk)); return true }
  try {
    const code = await main(['pause'])
    assert.equal(code, 2)
    assert.match(errs.join(''), /--run-id <id> is required/)
  } finally {
    process.stderr.write = orig
  }
})
```

- [ ] **Step 2: 跑测试确认失败**

Run:`node --experimental-strip-types --test --test-name-pattern "pause" test/cli.test.ts`
Expected: FAIL(`parseCliArgs(['pause',...])` 返回 `command: 'unknown'`;`main(['pause'])` 走 default 分支)

- [ ] **Step 3: 实现 pause 命令**

`world/src/cli.ts` 改动四处:

(a) import,把:

```ts
import { runWorld, resumeWorld, requestStop } from './run.ts'
```

改为:

```ts
import { runWorld, resumeWorld, requestStop, requestPause } from './run.ts'
```

(b) `CliArgs.command` 联合类型,把:

```ts
  command: 'run' | 'resume' | 'status' | 'stop' | 'help' | 'unknown'
```

改为:

```ts
  command: 'run' | 'resume' | 'status' | 'stop' | 'pause' | 'help' | 'unknown'
```

(c) `parseCliArgs` 的已知命令判断,把:

```ts
  if (cmd === 'run' || cmd === 'resume' || cmd === 'status' || cmd === 'stop') return { ...base, command: cmd }
```

改为:

```ts
  if (cmd === 'run' || cmd === 'resume' || cmd === 'status' || cmd === 'stop' || cmd === 'pause') return { ...base, command: cmd }
```

(d) 在 `cmdStop` 函数**下面**新增 `cmdPause`(镜像 `cmdStop`):

```ts
function cmdPause(args: CliArgs): number {
  if (!args.runId) { process.stderr.write('pause: --run-id <id> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  const r = requestPause(worldRoot, args.runId)
  if (!r.ok) { process.stderr.write(`pause: ${r.reason}\n`); return 2 }
  process.stdout.write(`pause requested — run "${args.runId}" will halt (resumable) after the current trading day.\n`)
  return 0
}
```

(e) `main` 的 switch,在 `case 'stop'` 那行下面新增:

```ts
    case 'pause': return cmdPause(args)
```

(f) HELP 文本,在 `world stop` 那一行下面新增一行:

```
  world pause  --run-id <id> [--world-dir <dir>]                            请求在当前交易日后暂停（可 resume）
```

- [ ] **Step 4: 跑测试确认通过**

Run:`node --experimental-strip-types --test --test-name-pattern "pause" test/cli.test.ts`
Expected: PASS(2 个测试)

- [ ] **Step 5: Commit**

```bash
git add world/src/cli.ts world/test/cli.test.ts
git commit -m "feat(world): add `world pause` CLI command"
```

---

### Task 6: 全量回归 + 类型检查

**Files:** 无(仅验证)

- [ ] **Step 1: 跑全量测试**

Run(在 `world/` 目录):`npm test`
Expected: PASS(全部 *.test.ts,无 fail)

- [ ] **Step 2: 类型检查 + CLI 自检**

Run:`npm run check`
Expected: PASS(`--help` 显示含 `world pause` 行;`tsc --noEmit` 无报错)

- [ ] **Step 3: 手动冒烟(可选但推荐)**

Run:`npm start -- --help`
Expected: 帮助文本包含 `world pause --run-id <id>` 一行。

---

## Self-Review

**Spec coverage:**
- 状态模型(加 `paused`)→ Task 1。
- 触发机制(`pauseFile` + `requestPause`)→ Task 1(路径)+ Task 2(函数)。
- 日界检查(PAUSE → teardown 'paused',abort 优先)→ Task 3。
- 续跑(门禁放宽 + 清 PAUSE 哨兵)→ Task 4。
- `world stop` 对 paused → aborted → Task 2。
- CLI `pause` 命令(解析 / handler / HELP)→ Task 5。
- 测试(resume / run / cli)→ 分散在 Task 2–5;Task 6 全量回归。
- 不做项(dashboard / mid-day / orphan 自愈)→ 计划未触及,符合。

**Placeholder scan:** 无 TBD/TODO;每个 code step 均给出完整替换前/替换后代码与精确命令。

**Type consistency:** `requestPause` / `requestStop` 返回 `{ ok: boolean; reason?: string }` 全程一致;`RunStatus` 在 state.ts 单点定义,run.ts/cli.ts 通过 `WorldState` 复用;`pauseFile(w, runId)` 签名与 `stopFile` 一致,所有调用点参数对齐。

**已知共享文件**(同一文件被多个 Task 触碰,按顺序执行无冲突):`world/src/run.ts`(Task 2/3/4)、`world/test/run.test.ts`(Task 2/3)。
