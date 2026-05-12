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
