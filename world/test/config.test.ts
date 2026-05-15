import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, isAbsolute, resolve } from 'node:path'
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
research_loop: /opt/rl
bots: [bot1, bot7]
replay: { from: "2024-01-02", to: "2024-06-28" }
simworld_upstream_url: http://127.0.0.1:18078/mcp
`)
  const c = loadWorldConfig(p)
  assert.equal(c.researchLoop, '/opt/rl')
  // bots_root / skills_root / openclaw_json default to paths inside the world tree,
  // resolved relative to the world.yaml's parent dir.
  assert.equal(c.botsRoot, resolve(dirname(p), '../../bots'))
  assert.equal(c.skillsRoot, resolve(dirname(p), '../../skills'))
  assert.equal(c.openclawJson, resolve(dirname(p), 'openclaw.json'))
  assert.equal(isAbsolute(c.botsRoot), true)
  assert.equal(isAbsolute(c.skillsRoot), true)
  assert.equal(isAbsolute(c.openclawJson), true)
  assert.deepEqual(c.bots, ['bot1', 'bot7'])
  assert.deepEqual(c.replay, { from: '2024-01-02', to: '2024-06-28' })
  // calendar 默认相对 world.yaml 所在目录
  assert.equal(c.calendar, join(dirname(p), 'calendar.json'))
  assert.equal(isAbsolute(c.calendar), true)
  assert.equal(c.concurrency, 4)
  assert.equal(c.perBotTimeoutSeconds, 1200)
  assert.equal(c.rlOpenclawDir, undefined)
  assert.deepEqual(c.shadowInclude, DEFAULT_SHADOW_INCLUDE)
  rmSync(p, { force: true })
})

test('loadWorldConfig honors overrides', () => {
  const p = tmpYaml(`
research_loop: /opt/rl
bots_root: /opt/bots
skills_root: /opt/skills
openclaw_json: /opt/openclaw.json
bots: [bot1]
replay: { from: "2024-01-02", to: "2024-01-03" }
calendar: /data/cal.json
concurrency: 8
per_bot_timeout_seconds: 600
rl_config_base: /cfgs/base.json
rl_openclaw_dir: /tmp/oc
shadow_include: [SOUL.md, skills]
simworld_upstream_url: http://my-simworld:9999/mcp
`)
  const c = loadWorldConfig(p)
  assert.equal(c.botsRoot, '/opt/bots')
  assert.equal(c.skillsRoot, '/opt/skills')
  assert.equal(c.openclawJson, '/opt/openclaw.json')
  assert.equal(c.calendar, '/data/cal.json')
  assert.equal(c.concurrency, 8)
  assert.equal(c.perBotTimeoutSeconds, 600)
  assert.equal(c.rlConfigBase, '/cfgs/base.json')
  assert.equal(c.rlOpenclawDir, '/tmp/oc')
  assert.deepEqual(c.shadowInclude, ['SOUL.md', 'skills'])
  assert.equal(c.simworldUpstreamUrl, 'http://my-simworld:9999/mcp')
  rmSync(p, { force: true })
})

test('loadWorldConfig rejects missing/invalid required fields', () => {
  const baseValid = `research_loop: /r\nbots: [bot1]\nreplay: {from: "2024-01-02", to: "2024-01-03"}\nsimworld_upstream_url: http://x/mcp`
  assert.throws(() => loadWorldConfig(tmpYaml(`bots: [bot1]\nreplay: {from: "2024-01-02", to: "2024-01-03"}\nsimworld_upstream_url: http://x/mcp`)), /research_loop/)
  assert.throws(() => loadWorldConfig(tmpYaml(`research_loop: /r\nbots: []\nreplay: {from: "2024-01-02", to: "2024-01-03"}\nsimworld_upstream_url: http://x/mcp`)), /bots/)
  assert.throws(() => loadWorldConfig(tmpYaml(`research_loop: /r\nbots: [bot1]\nreplay: {from: "2024-13-99", to: "2024-01-03"}\nsimworld_upstream_url: http://x/mcp`)), /from/)
  assert.throws(() => loadWorldConfig(tmpYaml(`research_loop: /r\nbots: [bot1]\nreplay: {from: "2024-02-02", to: "2024-01-03"}\nsimworld_upstream_url: http://x/mcp`)), /after/)
  assert.throws(() => loadWorldConfig(tmpYaml(`research_loop: /r\nbots: [bot1]\nreplay: {from: "2024-01-02", to: "2024-01-03"}`)), /simworld_upstream_url/)
  // sanity: baseValid string actually loads
  assert.doesNotThrow(() => loadWorldConfig(tmpYaml(baseValid)))
})

test('loop defaults to research-loop when field omitted', () => {
  const yaml = [
    'research_loop: /tmp/rl',
    'bots: [bot1]',
    'replay:',
    '  from: "2024-01-02"',
    '  to: "2024-01-03"',
    'simworld_upstream_url: http://x/mcp',
  ].join('\n') + '\n'
  const p = tmpYaml(yaml)
  const cfg = loadWorldConfig(p)
  assert.equal(cfg.loop, 'research-loop')
  assert.equal(cfg.openclawRoot, undefined)
  assert.equal(cfg.piServerEntry, undefined)
})

test('loop=openclaw-pi requires openclaw_json file to exist', () => {
  const yaml = [
    'research_loop: /tmp/rl',
    'bots: [bot1]',
    'replay:',
    '  from: "2024-01-02"',
    '  to: "2024-01-03"',
    'simworld_upstream_url: http://x/mcp',
    'loop: openclaw-pi',
    'openclaw_json: /tmp/does-not-exist-pi.json',
  ].join('\n') + '\n'
  const p = tmpYaml(yaml)
  assert.throws(() => loadWorldConfig(p), /openclaw_json/)
})

test('loop=openclaw-pi with valid paths populates pi fields', () => {
  const tmpDir = mkdtempSync(join(tmpdir(), 'world-cfg-'))
  const ocJson = join(tmpDir, 'openclaw.json'); writeFileSync(ocJson, '{}\n')
  const ocRoot = join(tmpDir, 'oc'); mkdirSync(ocRoot, { recursive: true })
  const piEntry = join(ocRoot, 'src/agents/agent_invest_pi_stdio_server.ts')
  mkdirSync(dirname(piEntry), { recursive: true }); writeFileSync(piEntry, '// stub\n')
  const yaml = [
    'research_loop: /tmp/rl',
    'bots: [bot1]',
    'replay:',
    '  from: "2024-01-02"',
    '  to: "2024-01-03"',
    'simworld_upstream_url: http://x/mcp',
    'loop: openclaw-pi',
    `openclaw_json: ${ocJson}`,
    `openclaw_root: ${ocRoot}`,
  ].join('\n') + '\n'
  const p = tmpYaml(yaml)
  const cfg = loadWorldConfig(p)
  assert.equal(cfg.loop, 'openclaw-pi')
  assert.equal(cfg.openclawJson, ocJson)
  assert.equal(cfg.openclawRoot, ocRoot)
  assert.equal(cfg.piServerEntry, piEntry)
})

test('loop=openclaw-pi rejects unknown values', () => {
  const yaml = [
    'research_loop: /tmp/rl',
    'bots: [bot1]',
    'replay:',
    '  from: "2024-01-02"',
    '  to: "2024-01-03"',
    'simworld_upstream_url: http://x/mcp',
    'loop: not-a-loop',
  ].join('\n') + '\n'
  const p = tmpYaml(yaml)
  assert.throws(() => loadWorldConfig(p), /loop/)
})
