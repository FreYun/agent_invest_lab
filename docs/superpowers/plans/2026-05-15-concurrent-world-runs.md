# Concurrent World Runs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the dashboard `+新建回测` button to start a new world run even while another is in progress; any bot may run in parallel across multiple concurrent runs.

**Architecture:** Eliminate the singleton `worldRoot/state.json`. Each run owns its state at `runtime/runs/<runId>/state.json`. Dashboard derives "is anything running" by enumerating run-dir state files. `piSessionsDir` becomes per-run so the same bot can run in two parallel runs without session-tree collisions.

**Tech Stack:** TypeScript (Node test runner), Node.js HTTP server (`dashboard/server.js`), vanilla JS (`dashboard/world.html`).

**Spec:** [docs/superpowers/specs/2026-05-15-concurrent-world-runs-design.md](../specs/2026-05-15-concurrent-world-runs-design.md)

---

## Task 1: paths.ts — add `runStateFile`, remove global `stateFile`

**Files:**
- Modify: `world/src/paths.ts:3`
- Test: `world/test/paths.test.ts`

- [ ] **Step 1: Read existing paths.test.ts to follow its style**

Run: `cat world/test/paths.test.ts | head -30`

- [ ] **Step 2: Write the failing test**

Append to `world/test/paths.test.ts`:

```typescript
test('runStateFile lives under runDir, not worldRoot', () => {
  const w = '/tmp/wroot'
  const runId = 'dash-2026-05-15T09-13-27'
  const p = runStateFile(w, runId)
  assert.equal(p, '/tmp/wroot/runs/dash-2026-05-15T09-13-27/state.json')
})

test('stateFile (legacy) no longer exists as an export', async () => {
  const mod = await import('../src/paths.ts')
  assert.equal((mod as Record<string, unknown>).stateFile, undefined)
})
```

Make sure the test file imports `runStateFile`:

```typescript
import { runStateFile } from '../src/paths.ts'
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd world && npx node --experimental-strip-types --test test/paths.test.ts`
Expected: FAIL — `runStateFile is not exported` and/or `stateFile is still exported`

- [ ] **Step 4: Update `world/src/paths.ts`**

Replace line 3 (`export const stateFile = ...`) with:

```typescript
export const runStateFile = (w: string, runId: string) => join(runDir(w, runId), 'state.json')
```

Note: `runDir` is defined later in the file. Move `runDir` above `runStateFile`, or forward-declare. Move `runDir` to line 3 and `runStateFile` to line 4:

```typescript
import { join } from 'node:path'

export const runDir = (w: string, runId: string) => join(w, 'runs', runId)
export const runStateFile = (w: string, runId: string) => join(runDir(w, runId), 'state.json')
export const calendarFile = (w: string, runId: string) => join(w, 'calendar.json')
// ... rest unchanged, but DELETE the OLD `runDir` line further down to avoid duplicate exports
```

Then delete the original `runDir` line further down (was line 9).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd world && npx node --experimental-strip-types --test test/paths.test.ts`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add world/src/paths.ts world/test/paths.test.ts
git commit -m "$(cat <<'EOF'
feat(world): add per-run runStateFile path, drop global stateFile

Each run now owns its state at runtime/runs/<runId>/state.json. The
worldRoot/state.json singleton is going away.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: state.ts — per-run signatures + `listActiveRuns`

**Files:**
- Modify: `world/src/state.ts`
- Test: `world/test/state.test.ts`

- [ ] **Step 1: Rewrite `world/test/state.test.ts` for new signatures**

Replace the file with:

```typescript
import { mkdtempSync, rmSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { writeState, readState, stateExists, listActiveRuns, type WorldState } from '../src/state.ts'
import { runDir, runStateFile } from '../src/paths.ts'

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
  loop: 'research-loop',
}

test('writeState/readState round-trip lives under runDir(runId)', () => {
  const w = tmpWorldRoot()
  assert.equal(stateExists(w, 'r1'), false)
  writeState(w, 'r1', sample)
  assert.equal(stateExists(w, 'r1'), true)
  assert.deepEqual(readState(w, 'r1'), sample)
  const onDisk = JSON.parse(readFileSync(runStateFile(w, 'r1'), 'utf8'))
  assert.ok('current_date' in onDisk && 'trading_dates' in onDisk)
  rmSync(w, { recursive: true, force: true })
})

test('readState throws when missing', () => {
  const w = tmpWorldRoot()
  assert.throws(() => readState(w, 'missing'), /state\.json/)
  rmSync(w, { recursive: true, force: true })
})

test('two runs write independent state files', () => {
  const w = tmpWorldRoot()
  writeState(w, 'r1', sample)
  writeState(w, 'r2', { ...sample, run_id: 'r2', cursor: 9 })
  assert.equal(readState(w, 'r1').cursor, 1)
  assert.equal(readState(w, 'r2').cursor, 9)
  rmSync(w, { recursive: true, force: true })
})

test('readState back-fills loop=research-loop for legacy state without the field', () => {
  const w = tmpWorldRoot()
  mkdirSync(runDir(w, 'r1'), { recursive: true })
  const legacy = { ...sample } as Record<string, unknown>
  delete legacy.loop
  writeFileSync(runStateFile(w, 'r1'), JSON.stringify(legacy))
  assert.equal(readState(w, 'r1').loop, 'research-loop')
  rmSync(w, { recursive: true, force: true })
})

test('listActiveRuns returns empty when no runs', () => {
  const w = tmpWorldRoot()
  assert.deepEqual(listActiveRuns(w), [])
  rmSync(w, { recursive: true, force: true })
})

test('listActiveRuns returns only status=running, sorted by started_at desc', () => {
  const w = tmpWorldRoot()
  writeState(w, 'old-done', { ...sample, run_id: 'old-done', status: 'done', started_at: '2026-05-10T10:00:00.000Z' })
  writeState(w, 'early', { ...sample, run_id: 'early', status: 'running', started_at: '2026-05-11T08:00:00.000Z' })
  writeState(w, 'late', { ...sample, run_id: 'late', status: 'running', started_at: '2026-05-11T12:00:00.000Z' })
  writeState(w, 'failed', { ...sample, run_id: 'failed', status: 'failed', started_at: '2026-05-11T13:00:00.000Z' })
  const active = listActiveRuns(w)
  assert.deepEqual(active.map(s => s.run_id), ['late', 'early'])
  rmSync(w, { recursive: true, force: true })
})

test('listActiveRuns skips run dirs without state.json and corrupt state.json', () => {
  const w = tmpWorldRoot()
  mkdirSync(runDir(w, 'no-state'), { recursive: true })
  mkdirSync(runDir(w, 'corrupt'), { recursive: true })
  writeFileSync(runStateFile(w, 'corrupt'), '{not json')
  writeState(w, 'ok', { ...sample, run_id: 'ok', status: 'running' })
  const active = listActiveRuns(w)
  assert.deepEqual(active.map(s => s.run_id), ['ok'])
  rmSync(w, { recursive: true, force: true })
})
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `cd world && npx node --experimental-strip-types --test test/state.test.ts`
Expected: FAIL — `writeState` accepts wrong args, `listActiveRuns` not exported

- [ ] **Step 3: Rewrite `world/src/state.ts`**

Replace entire file with:

```typescript
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { runDir, runStateFile } from './paths.ts'

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
  loop: 'research-loop' | 'openclaw-pi'
}

export function stateExists(worldRoot: string, runId: string): boolean {
  return existsSync(runStateFile(worldRoot, runId))
}

export function writeState(worldRoot: string, runId: string, state: WorldState): void {
  const path = runStateFile(worldRoot, runId)
  mkdirSync(dirname(path), { recursive: true })
  const tmp = `${path}.tmp`
  writeFileSync(tmp, JSON.stringify(state, null, 2) + '\n')
  renameSync(tmp, path)
}

export function readState(worldRoot: string, runId: string): WorldState {
  const path = runStateFile(worldRoot, runId)
  if (!existsSync(path)) throw new Error(`no state.json at ${path}`)
  const raw = JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>
  if (raw.loop !== 'research-loop' && raw.loop !== 'openclaw-pi') raw.loop = 'research-loop'
  return raw as unknown as WorldState
}

/** Scan runtime/runs/* for state.json files and return only the running ones,
 *  sorted by started_at descending. Corrupt or missing state files are skipped. */
export function listActiveRuns(worldRoot: string): WorldState[] {
  const runsBase = dirname(runDir(worldRoot, '_')) // runtime/runs
  if (!existsSync(runsBase)) return []
  let entries: string[]
  try { entries = readdirSync(runsBase) } catch { return [] }
  const out: WorldState[] = []
  for (const runId of entries) {
    try {
      const st = readState(worldRoot, runId)
      if (st.status === 'running') out.push(st)
    } catch { /* missing or corrupt — skip */ }
  }
  out.sort((a, b) => (a.started_at < b.started_at ? 1 : -1))
  return out
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd world && npx node --experimental-strip-types --test test/state.test.ts`
Expected: PASS (all 7 tests)

- [ ] **Step 5: Commit**

```bash
git add world/src/state.ts world/test/state.test.ts
git commit -m "$(cat <<'EOF'
feat(world): per-run state.ts + listActiveRuns

writeState/readState/stateExists all take runId now. Each run's state
lives at runtime/runs/<runId>/state.json. listActiveRuns scans run dirs
and returns only status=running, sorted by started_at desc.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: run.ts — propagate runId through state read/write calls

**Files:**
- Modify: `world/src/run.ts:430,432,458,468,501-502,554-555,575,581,596-597`

- [ ] **Step 1: Update all state.ts callers in run.ts**

Make these exact substitutions (all are state-related; line numbers approximate, use search):

```diff
-  try { state = readState(worldRoot) } catch { /* state.json 可能 setup 都没写出来 */ }
+  try { state = readState(worldRoot, runId) } catch { /* state.json 可能 setup 都没写出来 */ }
```

```diff
-    try { writeState(worldRoot, { ...state, status: finalStatus, updated_at: new Date().toISOString() }) } catch { /* ignore */ }
-    try { writeFileSync(join(P.runDir(worldRoot, runId), 'state.json'), JSON.stringify({ ...state, status: finalStatus }, null, 2) + '\n') } catch { /* ignore */ }
+    try { writeState(worldRoot, runId, { ...state, status: finalStatus, updated_at: new Date().toISOString() }) } catch { /* ignore */ }
```

(The second line is the now-redundant duplicate that wrote into runDir; `writeState` already does that.)

```diff
-    try { if (existsSync(P.stateFile(worldRoot))) { const s = readState(worldRoot); writeState(worldRoot, { ...s, status: 'failed', updated_at: new Date().toISOString() }) } } catch { /* ignore */ }
+    try { if (existsSync(P.runStateFile(worldRoot, runId))) { const s = readState(worldRoot, runId); writeState(worldRoot, runId, { ...s, status: 'failed', updated_at: new Date().toISOString() }) } } catch { /* ignore */ }
```

```diff
-  writeState(worldRoot, initial)
+  writeState(worldRoot, runId, initial)
```

```diff
-        const state = readState(worldRoot)
-        writeState(worldRoot, { ...state, current_date: date, updated_at: new Date().toISOString() })
+        const state = readState(worldRoot, runId)
+        writeState(worldRoot, runId, { ...state, current_date: date, updated_at: new Date().toISOString() })
```

```diff
-      const st = readState(worldRoot)
-      writeState(worldRoot, { ...st, cursor: cursor + 1, updated_at: new Date().toISOString() })
+      const st = readState(worldRoot, runId)
+      writeState(worldRoot, runId, { ...st, cursor: cursor + 1, updated_at: new Date().toISOString() })
```

In `resumeWorld` (around line 573-592):

```diff
-export async function resumeWorld(opts: ResumeWorldOptions): Promise<void> {
-  const { worldRoot, config } = opts
-  const state = readState(worldRoot)
+export interface ResumeWorldOptions {
+  worldRoot: string
+  config: WorldConfig
+  runId: string
+  startBotServer?: StartBotServer
+}
+
+export async function resumeWorld(opts: ResumeWorldOptions): Promise<void> {
+  const { worldRoot, config, runId } = opts
+  const state = readState(worldRoot, runId)
```

(Note: this requires updating `ResumeWorldOptions` interface above the function definition. Add `runId: string` field; remove existing interface duplicate if there is one.)

Inside resumeWorld, also replace `state.run_id` with `runId` for `rmSync(P.stopFile(...))` and `runId: state.run_id` in `setup`:

```diff
-  rmSync(P.stopFile(worldRoot, state.run_id), { force: true })
-  const setupRes = await setup({ worldRoot, config, runId: state.run_id, startBotServer: opts.startBotServer })
+  rmSync(P.stopFile(worldRoot, runId), { force: true })
+  const setupRes = await setup({ worldRoot, config, runId, startBotServer: opts.startBotServer })
```

```diff
-  await runLoop({ worldRoot, runId: state.run_id, config, setupRes, fromCursor: state.cursor })
+  await runLoop({ worldRoot, runId, config, setupRes, fromCursor: state.cursor })
```

In `requestStop` (around line 594-602): change signature to take runId and remove the state.json reliance:

```diff
-/** 由独立的 `world stop` 进程调用：写一个 STOP 哨兵，正在跑的 runLoop 会在下一天开始前发现它。 */
-export function requestStop(worldRoot: string): { ok: boolean; reason?: string } {
-  if (!existsSync(P.stateFile(worldRoot))) return { ok: false, reason: 'no state.json' }
-  const state = readState(worldRoot)
-  if (state.status !== 'running') return { ok: false, reason: `state status is "${state.status}"` }
-  mkdirSync(P.runDir(worldRoot, state.run_id), { recursive: true })
-  writeFileSync(P.stopFile(worldRoot, state.run_id), `requested at ${new Date().toISOString()}\n`)
+/** 由独立的 `world stop` 进程调用：写一个 STOP 哨兵，正在跑的 runLoop 会在下一天开始前发现它。 */
+export function requestStop(worldRoot: string, runId: string): { ok: boolean; reason?: string } {
+  if (!existsSync(P.runStateFile(worldRoot, runId))) return { ok: false, reason: `no state.json for run ${runId}` }
+  const state = readState(worldRoot, runId)
+  if (state.status !== 'running') return { ok: false, reason: `state status is "${state.status}"` }
+  mkdirSync(P.runDir(worldRoot, runId), { recursive: true })
+  writeFileSync(P.stopFile(worldRoot, runId), `requested at ${new Date().toISOString()}\n`)
   return { ok: true }
 }
```

- [ ] **Step 2: Run the world test suite (most tests touch run.ts indirectly)**

Run: `cd world && npx node --experimental-strip-types --test test/run.test.ts test/resume.test.ts`
Expected: Most existing tests fail because they still call `readState(worldRoot)` without runId — that's OK; Task 3.5 fixes the tests. For now we only need run.ts to compile and pass type checks.

- [ ] **Step 3: Type-check the world package**

Run: `cd world && npx tsc --noEmit`
Expected: No errors. If `P.stateFile` references remain anywhere, fix them.

- [ ] **Step 4: Commit**

```bash
git add world/src/run.ts
git commit -m "$(cat <<'EOF'
refactor(world): thread runId through every state read/write in run.ts

All readState/writeState/stateExists calls now scoped to runId. Drop
the redundant runDir/state.json duplicate-write in teardown (writeState
already writes there). resumeWorld + requestStop take runId
explicitly.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3.5: Update run.test.ts and resume.test.ts to new signatures

**Files:**
- Modify: `world/test/run.test.ts`
- Modify: `world/test/resume.test.ts`

- [ ] **Step 1: Find every call site in tests**

Run: `cd world && grep -n "readState(worldRoot)\|writeState(worldRoot,\|stateExists(worldRoot)" test/*.test.ts`

- [ ] **Step 2: Update each call site**

For each line shown, the run_id is typically known in scope (often `'r1'` in tests). Examples to apply:

`world/test/run.test.ts:102`:
```diff
-  const st = readState(worldRoot)
+  const st = readState(worldRoot, 'r1')
```

`world/test/run.test.ts:151,180,192,209`:
```diff
-  assert.equal(readState(worldRoot).status, 'done')
+  assert.equal(readState(worldRoot, 'r1').status, 'done')
```

`world/test/resume.test.ts:39,42,61,62`:
Look at each test's runId (likely `'r1'`) and thread it through.

`world/test/resume.test.ts` also constructs `resumeWorld({ worldRoot, config })` — add `runId: 'r1'` to the options object.

- [ ] **Step 3: Run tests**

Run: `cd world && npx node --experimental-strip-types --test test/run.test.ts test/resume.test.ts`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add world/test/run.test.ts world/test/resume.test.ts
git commit -m "$(cat <<'EOF'
test(world): update run/resume tests to per-run state signatures

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: cli.ts — `resume`/`status`/`stop` require `--run-id`, `run` drops state-archive logic

**Files:**
- Modify: `world/src/cli.ts`
- Test: `world/test/cli.test.ts`

- [ ] **Step 1: Add failing tests to `world/test/cli.test.ts`**

Open `world/test/cli.test.ts` and add (or modify) these tests:

```typescript
test('resume without --run-id exits with code 2 and clear error', async () => {
  const { stderr, code } = await runCli(['resume', '--config', 'world.yaml'])
  assert.equal(code, 2)
  assert.match(stderr, /--run-id <id> is required/)
})

test('status without --run-id exits with code 2', async () => {
  const { stderr, code } = await runCli(['status'])
  assert.equal(code, 2)
  assert.match(stderr, /--run-id <id> is required/)
})

test('stop without --run-id exits with code 2', async () => {
  const { stderr, code } = await runCli(['stop'])
  assert.equal(code, 2)
  assert.match(stderr, /--run-id <id> is required/)
})

test('run no longer touches worldRoot/state.json on entry', async () => {
  // Test would create worldRoot/state.json from a fictitious prior run, then
  // attempt `world run --config` and verify the fictitious file is untouched.
  // (Use existing test helpers if they exist; otherwise this test can be a
  // unit-level check on a refactored `cmdRun` if you extract one.)
  // Implementation: see existing patterns in world/test/cli.test.ts.
})
```

Implementation note: if `runCli` doesn't already exist, look at `world/test/cli.test.ts` for the established test invocation pattern and use it. If the file only has imports for `parseCliArgs`, write the tests at that level instead — verify `parseCliArgs(['resume','--config','x']).runId === undefined`, then verify the `cmdResume` function exits 2 when runId is missing.

- [ ] **Step 2: Run — verify failures**

Run: `cd world && npx node --experimental-strip-types --test test/cli.test.ts`
Expected: FAIL with current error messages not matching the new ones.

- [ ] **Step 3: Update `world/src/cli.ts`**

Replace the file body:

```typescript
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
  world run    --config <world.yaml> [--run-id <id>] [--world-dir <dir>]   开新 run
  world resume --config <world.yaml> --run-id <id> [--world-dir <dir>]     续跑指定 run（从 state.json.cursor）
  world status --run-id <id> [--world-dir <dir>]                            查看某 run 的进度
  world stop   --run-id <id> [--world-dir <dir>]                            请求优雅终止某 run
  world --help

注：--run-id 现在是 resume/status/stop 的必填项。dashboard 通过扫描
runtime/runs/*/state.json 拿到活动 run 列表。
`

export function parseCliArgs(argv: string[]): CliArgs {
  if (argv.length === 0 || argv[0] === '-h' || argv[0] === '--help') return { command: 'help', config: undefined, runId: undefined, worldDir: undefined }
  const cmd = argv[0]
  const valueOf = (name: string): string | undefined => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : undefined }
  const base: CliArgs = { command: 'unknown', config: valueOf('--config'), runId: valueOf('--run-id'), worldDir: valueOf('--world-dir') }
  if (cmd === 'run' || cmd === 'resume' || cmd === 'status' || cmd === 'stop') return { ...base, command: cmd }
  return base
}

function resolveWorldRoot(args: CliArgs): string {
  return resolve(args.worldDir ?? join(process.cwd(), 'runtime'))
}

function deriveRunId(): string {
  return `run-${new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)}`
}

async function cmdRun(args: CliArgs): Promise<number> {
  if (!args.config) { process.stderr.write('run: --config <world.yaml> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  mkdirSync(worldRoot, { recursive: true })
  // 不再读/写 worldRoot/state.json；每个 run 自管 runDir/state.json，
  // dashboard 通过 listActiveRuns 扫描得到活动列表。
  const config = loadWorldConfig(args.config)
  const runId = args.runId ?? deriveRunId()
  await runWorld({ worldRoot, config, runId })
  return 0
}

async function cmdResume(args: CliArgs): Promise<number> {
  if (!args.config) { process.stderr.write('resume: --config <world.yaml> is required\n'); return 2 }
  if (!args.runId) { process.stderr.write('resume: --run-id <id> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  if (!stateExists(worldRoot, args.runId)) { process.stderr.write(`resume: no state.json for run "${args.runId}" under ${worldRoot}\n`); return 2 }
  const config = loadWorldConfig(args.config)
  await resumeWorld({ worldRoot, config, runId: args.runId })
  return 0
}

function cmdStatus(args: CliArgs): number {
  if (!args.runId) { process.stderr.write('status: --run-id <id> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  if (!stateExists(worldRoot, args.runId)) { process.stdout.write(`(no run "${args.runId}" under ${worldRoot})\n`); return 0 }
  const st = readState(worldRoot, args.runId)
  process.stdout.write(`run_id:       ${st.run_id}\nstatus:       ${st.status}\ncurrent_date: ${st.current_date}\nprogress:     ${st.cursor}/${st.trading_dates.length} trading days\nbots:         ${st.bots.join(', ')}\nmemory_port:  ${st.memory_port}\nstarted_at:   ${st.started_at}\nupdated_at:   ${st.updated_at}\n`)
  if (existsSync(P.summaryFile(worldRoot, st.run_id))) process.stdout.write(`summary:      ${P.summaryFile(worldRoot, st.run_id)}\n`)
  return 0
}

function cmdStop(args: CliArgs): number {
  if (!args.runId) { process.stderr.write('stop: --run-id <id> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  const r = requestStop(worldRoot, args.runId)
  if (!r.ok) { process.stderr.write(`stop: ${r.reason}\n`); return 2 }
  process.stdout.write(`stop requested — run "${args.runId}" will abort after the current trading day.\n`)
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
```

- [ ] **Step 4: Run tests**

Run: `cd world && npx node --experimental-strip-types --test test/cli.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add world/src/cli.ts world/test/cli.test.ts
git commit -m "$(cat <<'EOF'
feat(world): resume/status/stop require --run-id, run drops state archive

The implicit 'last run' semantics are gone — every CLI command targeting
a specific run takes --run-id. The cmdRun start-up dance that archived
worldRoot/state.json is deleted entirely (no global state to archive).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: piSessionsDir — per-run isolation

**Files:**
- Modify: `world/src/config.ts:34, 141, 157-159, 192`
- Modify: `world/src/run.ts` (every `config.piSessionsDir` reference around lines 211-227, 305-311)
- Modify: `world/src/paths.ts` — add `piSessionsDir(w, runId)`
- Test: `world/test/run.test.ts:381-410` (botServerArgv test referencing piSessionsDir)

- [ ] **Step 1: Add the path helper**

Append to `world/src/paths.ts`:

```typescript
export const piSessionsDir = (w: string, runId: string) => join(runDir(w, runId), 'pi-sessions')
```

- [ ] **Step 2: Deprecate `pi_sessions_dir` in config.ts**

In `world/src/config.ts`, change the piSessionsDir block (lines ~141, ~157-159) to:

```typescript
let openclawRoot: string | undefined
let piServerEntry: string | undefined
// piSessionsDir is now derived per-run in run.ts (join(runDir, 'pi-sessions'));
// world.yaml's pi_sessions_dir is deprecated and logged as a warning if set.
if (loop === 'openclaw-pi') {
  if (!existsSync(openclawJson)) {
    throw new Error(`world config: openclaw_json not found (required for openclaw-pi loop): ${openclawJson}`)
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
  if (typeof raw.pi_sessions_dir === 'string' && raw.pi_sessions_dir.trim()) {
    process.stderr.write(`world config WARNING: "pi_sessions_dir" is deprecated and ignored (now auto = <runDir>/pi-sessions per run).\n`)
  }
}
```

Remove `piSessionsDir?: string` from the `WorldConfig` interface (line 34). Remove `piSessionsDir` from the `return { ... }` at line 192.

- [ ] **Step 3: Update run.ts to use per-run piSessionsDir**

In `world/src/run.ts` `setup()` (around line 211-227 and 305-311), replace every `config.piSessionsDir` with a per-run computation.

Add this near the top of `setup()` after `const spawnCwd = ...`:

```typescript
// pi sessions dir is per-run now (not config-driven). Each run owns its own
// session tree so the same bot can run in two parallel runs without colliding.
const piSessionsDirForRun = config.loop === 'openclaw-pi' ? P.piSessionsDir(worldRoot, runId) : undefined
```

Then for the `piAgentDirFor` closure and the env block:

```typescript
const piAgentDirFor = (botId: string): string | undefined =>
  piSessionsDirForRun ? join(piSessionsDirForRun, 'agents', botId, 'agent') : undefined
```

```typescript
const piEnv: Record<string, string> = agentDir && piSessionsDirForRun
  ? {
      OPENCLAW_AGENT_DIR: agentDir,
      PI_CODING_AGENT_DIR: agentDir,
      OPENCLAW_STATE_DIR: piSessionsDirForRun,
    }
  : {}
```

In the seed-once block (around line 305-311):

```typescript
if (piSessionsDirForRun && config.openclawRoot) {
  mkdirSync(piSessionsDirForRun, { recursive: true })
  const sourceAgentsDir = join(config.openclawRoot, '..', 'agents')
  for (const botId of config.bots) {
    if (!isAbsolute(botId)) seedPiAgentBot(piSessionsDirForRun, sourceAgentsDir, botId)
  }
}
```

- [ ] **Step 4: Update the run.test.ts botServerArgv test**

Open `world/test/run.test.ts:381-410` and update the test fixture: it currently passes `piSessionsDir: '/home/rooot/agent_invest_lab/session'` as part of a `WorldConfig`-shaped object. Since `piSessionsDir` is no longer on `WorldConfig`, delete that property from the fixture. If the test asserts on env vars containing `/session`, update to the per-run path (e.g. `/<worldRoot>/runs/r1/pi-sessions`).

If the test is `botServerArgv`-style (asserts on argv, not env), the simpler fix is to delete the property and keep the assertions.

- [ ] **Step 5: Run the test suite end-to-end**

Run: `cd world && npx node --experimental-strip-types --test`
Expected: ALL PASS

Run: `cd world && npx tsc --noEmit`
Expected: No type errors

- [ ] **Step 6: Commit**

```bash
git add world/src/paths.ts world/src/config.ts world/src/run.ts world/test/run.test.ts
git commit -m "$(cat <<'EOF'
feat(world): per-run piSessionsDir under runDir/pi-sessions

config.piSessionsDir is gone — each run computes its own sessions dir
under runDir/<runId>/pi-sessions. Same bot can now run in two parallel
runs without trampling each other's session tree. pi_sessions_dir in
world.yaml is parsed but ignored with a deprecation warning.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Concurrent runs smoke test

**Files:**
- Create: `world/test/concurrent-runs.test.ts`

- [ ] **Step 1: Write the test**

Create `world/test/concurrent-runs.test.ts`:

```typescript
import { mkdtempSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { writeState, readState, listActiveRuns, type WorldState } from '../src/state.ts'
import { runStateFile } from '../src/paths.ts'

function tmpWorldRoot(): string {
  return mkdtempSync(join(tmpdir(), 'concurrent-runs-'))
}

const base: Omit<WorldState, 'run_id'> = {
  status: 'running',
  current_date: '2024-03-15',
  trading_dates: ['2024-03-14', '2024-03-15'],
  cursor: 0,
  bots: ['bot1'],
  memory_port: 0,
  started_at: '',
  updated_at: '',
  loop: 'research-loop',
}

test('two runs of the same bot have independent state files and dont overwrite each other', () => {
  const w = tmpWorldRoot()
  writeState(w, 'runA', { ...base, run_id: 'runA', started_at: '2026-05-15T10:00:00.000Z', updated_at: '2026-05-15T10:00:00.000Z' })
  writeState(w, 'runB', { ...base, run_id: 'runB', cursor: 5, started_at: '2026-05-15T10:05:00.000Z', updated_at: '2026-05-15T10:05:00.000Z' })

  // Both files exist independently
  assert.ok(existsSync(runStateFile(w, 'runA')))
  assert.ok(existsSync(runStateFile(w, 'runB')))

  // Reads are independent
  assert.equal(readState(w, 'runA').cursor, 0)
  assert.equal(readState(w, 'runB').cursor, 5)

  // listActiveRuns sees both, B first (newer started_at)
  const active = listActiveRuns(w)
  assert.deepEqual(active.map(s => s.run_id), ['runB', 'runA'])

  // Updating runA must not affect runB
  writeState(w, 'runA', { ...base, run_id: 'runA', cursor: 99, started_at: '2026-05-15T10:00:00.000Z', updated_at: '2026-05-15T10:30:00.000Z' })
  assert.equal(readState(w, 'runA').cursor, 99)
  assert.equal(readState(w, 'runB').cursor, 5)

  // Marking A done removes it from listActiveRuns but keeps B
  writeState(w, 'runA', { ...readState(w, 'runA'), status: 'done' })
  assert.deepEqual(listActiveRuns(w).map(s => s.run_id), ['runB'])

  rmSync(w, { recursive: true, force: true })
})
```

- [ ] **Step 2: Run — verify it passes (logic already in place)**

Run: `cd world && npx node --experimental-strip-types --test test/concurrent-runs.test.ts`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add world/test/concurrent-runs.test.ts
git commit -m "$(cat <<'EOF'
test(world): smoke test for two parallel runs sharing a bot

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Dashboard server.js — drop 409, expose activeRuns

**Files:**
- Modify: `/home/rooot/.openclaw/dashboard/server.js:4925-4979` (status route)
- Modify: `/home/rooot/.openclaw/dashboard/server.js:5086-5096` (start route)

- [ ] **Step 1: Find `LAB_WORLD_STATE` definition to confirm what it points at**

Run: `grep -n "LAB_WORLD_STATE\b" /home/rooot/.openclaw/dashboard/server.js | head -5`

Note the value — likely `path.join(LAB_WORLD_ROOT, 'state.json')`. We're going to stop using it for "is anything running" and instead scan `<LAB_WORLD_ROOT>/runs/*/state.json`.

- [ ] **Step 2: Replace the status route**

In `/home/rooot/.openclaw/dashboard/server.js`, replace the body of `if (url.pathname === "/api/world-backtest-status" && req.method === "GET")` block (lines 4925-4979). The new code:

```javascript
if (url.pathname === "/api/world-backtest-status" && req.method === "GET") {
  let defaultFrom = "", defaultTo = "", bots = [], defaultBuyable = [];
  let calendarFirstDay = "", calendarLastDay = "";
  let availableBots = [];
  try {
    const cfg = yaml.load(fs.readFileSync(LAB_WORLD_CONFIG, "utf8"));
    defaultFrom = (cfg && cfg.replay && cfg.replay.from) || "";
    defaultTo = (cfg && cfg.replay && cfg.replay.to) || "";
    bots = Array.isArray(cfg && cfg.bots) ? cfg.bots : [];
    if (Array.isArray(cfg && cfg.buyable_fund_codes)) {
      defaultBuyable = [...new Set(cfg.buyable_fund_codes.filter(c => typeof c === "string" && /^\d{6}$/.test(c)))].sort();
    }
    const botsRootRel = (cfg && cfg.bots_root) || "../../bots";
    const botsRoot = path.isAbsolute(botsRootRel) ? botsRootRel : path.join(path.dirname(LAB_WORLD_CONFIG), botsRootRel);
    try {
      availableBots = fs.readdirSync(botsRoot, { withFileTypes: true })
        .filter(e => e.isDirectory() && /^bot\d+$/.test(e.name))
        .map(e => e.name)
        .sort((a, b) => parseInt(a.slice(3), 10) - parseInt(b.slice(3), 10));
    } catch { /* bots_root missing — leave list empty */ }
    const calRel = (cfg && cfg.calendar) || "calendar.json";
    const calAbs = path.isAbsolute(calRel) ? calRel : path.join(path.dirname(LAB_WORLD_CONFIG), calRel);
    try {
      const cal = JSON.parse(fs.readFileSync(calAbs, "utf8"));
      const days = Array.isArray(cal.trading_days) ? cal.trading_days.filter(d => typeof d === "string" && /^\d{4}-\d{2}-\d{2}$/.test(d)) : [];
      if (days.length) {
        calendarFirstDay = days[0];
        calendarLastDay = days[days.length - 1];
      }
    } catch { /* calendar missing/unreadable */ }
  } catch { /* world.yaml may be missing */ }

  // Active runs: scan <LAB_WORLD_ROOT>/runs/<runId>/state.json for status=running.
  const runsBase = path.join(LAB_WORLD_ROOT, "runs");
  const activeRuns = [];
  try {
    for (const runId of fs.readdirSync(runsBase)) {
      try {
        const st = JSON.parse(fs.readFileSync(path.join(runsBase, runId, "state.json"), "utf8"));
        if (st && st.status === "running") {
          activeRuns.push({
            runId: st.run_id || runId,
            cursor: st.cursor,
            total: Array.isArray(st.trading_dates) ? st.trading_dates.length : undefined,
            bots: Array.isArray(st.bots) ? st.bots : [],
            current_date: st.current_date,
            started_at: st.started_at,
          });
        }
      } catch { /* skip corrupt/missing */ }
    }
  } catch { /* runs/ missing — no active runs */ }
  activeRuns.sort((a, b) => (a.started_at < b.started_at ? 1 : -1));

  // Backward-compat fields: running/runId/cursor/total now reflect the FIRST
  // (most-recent) active run, so existing frontends still get something sane.
  const head = activeRuns[0];
  const running = !!head;
  const runId = head ? head.runId : undefined;
  const cursor = head ? head.cursor : undefined;
  const total = head ? head.total : undefined;

  res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify({
    running, runId, cursor, total,
    activeRuns,
    defaultFrom, defaultTo, bots, defaultBuyable, availableBots,
    calendarFirstDay, calendarLastDay,
  }));
  return;
}
```

- [ ] **Step 3: Drop the 409 check in `/api/world-backtest-start`**

In the same file (around line 5086-5096), delete the block:

```javascript
// Refuse if a run is already in progress.
try {
  const st = JSON.parse(fs.readFileSync(LAB_WORLD_STATE, "utf8"));
  if (st && st.status === "running") {
    res.writeHead(409, { "Content-Type": "application/json" });
    res.end(JSON.stringify({
      error: `run already in progress: ${st.run_id} (cursor ${st.cursor}/${(st.trading_dates||[]).length})`,
    }));
    return;
  }
} catch { /* no state.json — ok to start */ }
```

Leave a one-line note in its place:

```javascript
// Concurrent runs allowed — no single-run gate. Each run owns its state in
// runtime/runs/<runId>/state.json; dashboard's status endpoint scans them.
```

- [ ] **Step 4: Manually probe the API to confirm shape**

Restart the dashboard server (or trust hot-reload if configured), then:

```bash
curl -sS http://127.0.0.1:8082/api/world-backtest-status | python3 -m json.tool | head -30
```

(Replace 8082 with whatever port the dashboard listens on — check `grep "listen" /home/rooot/.openclaw/dashboard/server.js`.)

Expected: JSON has `activeRuns: [...]` array AND backward-compat `running`/`runId` fields.

- [ ] **Step 5: Commit**

```bash
git -C /home/rooot/.openclaw add dashboard/server.js
git -C /home/rooot/.openclaw commit -m "$(cat <<'EOF'
feat(dashboard): support concurrent world runs

/api/world-backtest-status now returns activeRuns[] (sorted started_at desc)
in addition to legacy running/runId/cursor/total fields (mirroring the most
recent active run). /api/world-backtest-start no longer 409s when another
run is in progress.

Source of truth moves from singleton runtime/state.json to scanning
runtime/runs/<runId>/state.json per run.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Dashboard world.html — never disable button, show count

**Files:**
- Modify: `/home/rooot/.openclaw/dashboard/world.html:649-675` (`refreshBacktestStatus`)

- [ ] **Step 1: Replace `refreshBacktestStatus`**

Find the function around line 649 and replace its body:

```javascript
async function refreshBacktestStatus() {
  try {
    const r = await fetch('/api/world-backtest-status');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    lastBacktestStatus = d;
    // 始终允许新建 run。前端不再 disable 按钮。
    $btOpen.disabled = false;
    const n = Array.isArray(d.activeRuns) ? d.activeRuns.length : 0;
    if (n > 0) {
      $btHint.classList.remove('err');
      $btHint.textContent = `${n} 个 run 正在跑（左侧 sidebar 可切换）`;
    } else {
      $btHint.classList.remove('err');
      const range = (d.calendarFirstDay && d.calendarLastDay) ? ` ｜ 日历可选 ${d.calendarFirstDay} … ${d.calendarLastDay}` : '';
      $btHint.textContent = `world.yaml 默认：${d.defaultFrom || '?'} → ${d.defaultTo || '?'}（bots: ${(d.bots||[]).join(', ') || '?'}）${range}`;
      if (d.calendarFirstDay) { $btFrom.min = d.calendarFirstDay; $btTo.min = d.calendarFirstDay; }
      if (d.calendarLastDay) { $btFrom.max = d.calendarLastDay; $btTo.max = d.calendarLastDay; }
    }
  } catch (e) {
    $btOpen.disabled = false;
    $btHint.classList.add('err');
    $btHint.textContent = '状态读取失败：' + e.message;
  }
}
```

- [ ] **Step 2: Manually verify in browser**

1. Reload the dashboard tab.
2. Confirm the `+新建回测` button is enabled even while a run is in progress.
3. Hint text reads `N 个 run 正在跑（左侧 sidebar 可切换）`.
4. Click `+新建回测`, fill the modal, submit. Confirm a new run starts (check left sidebar shows two `running` entries OR check `curl /api/world-backtest-status | jq '.activeRuns | length'` ≥ 2).

- [ ] **Step 3: Commit**

```bash
git -C /home/rooot/.openclaw add dashboard/world.html
git -C /home/rooot/.openclaw commit -m "$(cat <<'EOF'
feat(dashboard): never disable +新建回测; show active run count

Frontend stops gating on lastBacktestStatus.running. Hint shows "N 个 run
正在跑" when ≥1 active, else falls back to world.yaml defaults + calendar
range.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Full regression sweep + manual end-to-end

**Files:** none modified

- [ ] **Step 1: Run the entire world test suite**

Run: `cd /home/rooot/agent_invest_lab/world && npx node --experimental-strip-types --test`
Expected: ALL PASS

- [ ] **Step 2: Type-check the world package**

Run: `cd /home/rooot/agent_invest_lab/world && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Manual end-to-end — two parallel runs sharing bot1**

In dashboard:

1. Click `+新建回测`. From=2024-03-14 To=2024-03-18, bots=[bot1], submit.
2. Wait ~10 seconds (run-A is now `running`).
3. Click `+新建回测` again (button should still be enabled). From=2024-03-19 To=2024-03-22, bots=[bot1], submit.
4. Confirm:
   - Both run dirs exist: `ls /home/rooot/agent_invest_lab/world/runtime/runs/ | tail -5`
   - Both states are `running`:
     ```bash
     for f in /home/rooot/agent_invest_lab/world/runtime/runs/dash-*/state.json; do
       jq -r '"\(.run_id) \(.status) \(.cursor)/\(.trading_dates|length)"' "$f"
     done
     ```
   - Two separate pi-sessions trees exist: `ls /home/rooot/agent_invest_lab/world/runtime/runs/dash-*/pi-sessions/agents/bot1/`
   - Dashboard sidebar shows both as `running`.

- [ ] **Step 4: Final commit (no-op marker, optional)**

If steps 1-3 all pass cleanly, no commit needed — work is done.

If anything failed, fix and re-commit per the specific task.

---

## Out of scope (explicit)

- Dashboard multi-run transcript view (sidebar already lists, but viewing pane still single-select)
- Cross-run aggregation / comparison
- Per-bot concurrency quotas / rate-limit handling
- Cross-run shared memory store (each run's mem0 store is independent — intentional)
