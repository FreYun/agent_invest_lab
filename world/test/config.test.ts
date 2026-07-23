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
  // sanity: baseValid string actually loads
  assert.doesNotThrow(() => loadWorldConfig(tmpYaml(baseValid)))
})

test('simworld_upstream_url defaults to 127.0.0.1 (local) when omitted', () => {
  // host 通过 config 注入：缺省回落本机 127.0.0.1，方便其他自起本地 MCP 的环境；
  // 本机的 world.yaml 显式注入远程数据机地址即指向远程。
  const c = loadWorldConfig(tmpYaml(`research_loop: /r\nbots: [bot1]\nreplay: {from: "2024-01-02", to: "2024-01-03"}`))
  assert.equal(c.simworldUpstreamUrl, 'http://127.0.0.1:18078/mcp')
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


test('loadWorldConfig parses strategy library root and bot assignments', () => {
  const dir = mkdtempSync(join(tmpdir(), 'wcfg-strategy-'))
  const lib = join(dir, 'strategies', 'index-products')
  mkdirSync(lib, { recursive: true })
  writeFileSync(join(lib, 'hs300.md'), '# HS300 strategy\n')
  writeFileSync(join(lib, 'semi.md'), '# Semi strategy\n')
  writeFileSync(join(lib, 'manifest.yaml'), [
    'version: 1',
    'strategies:',
    '  hs300:',
    '    title: 沪深300指数投资框架',
    '    methodology: hs300.md',
    '    target_index: "000300.SH"',
    '    default_buyable_fund_codes: ["000051", "510300"]',
    '  semiconductor:',
    '    title: 半导体设备指数投资框架',
    '    methodology: semi.md',
    '    target_index: "931865.CSI"',
    '    default_buyable_fund_codes: ["014854"]',
  ].join('\n') + '\n')
  const p = join(dir, 'world.yaml')
  writeFileSync(p, [
    'research_loop: /opt/rl',
    'bots: [bot7, bot11]',
    'replay: { from: "2024-01-02", to: "2024-01-03" }',
    'simworld_upstream_url: http://x/mcp',
    'fund_mcp_cli: /fake/cli.py',
    'strategy_library_root: ./strategies/index-products',
    'bot_assignments:',
    '  bot7:',
    '    strategy_id: hs300',
    '    buyable_fund_codes: ["000051"]',
    '  bot11:',
    '    strategy_id: semiconductor',
  ].join('\n') + '\n')

  const c = loadWorldConfig(p)
  assert.equal(c.strategyLibraryRoot, lib)
  assert.deepEqual(c.botAssignments, {
    bot7: { strategyId: 'hs300', buyableFundCodes: ['000051'] },
    bot11: { strategyId: 'semiconductor' },
  })
  rmSync(dir, { recursive: true, force: true })
})

test('loadWorldConfig validates strategy assignments fail-fast', () => {
  const dir = mkdtempSync(join(tmpdir(), 'wcfg-strategy-bad-'))
  const lib = join(dir, 'strategies', 'index-products')
  mkdirSync(lib, { recursive: true })
  writeFileSync(join(lib, 'hs300.md'), '# HS300 strategy\n')
  writeFileSync(join(lib, 'manifest.yaml'), [
    'version: 1',
    'strategies:',
    '  hs300:',
    '    title: 沪深300指数投资框架',
    '    methodology: hs300.md',
    '    target_index: "000300.SH"',
    '    default_buyable_fund_codes: ["000051"]',
  ].join('\n') + '\n')
  const base = [
    'research_loop: /opt/rl',
    'bots: [bot7]',
    'replay: { from: "2024-01-02", to: "2024-01-03" }',
    'simworld_upstream_url: http://x/mcp',
  ]
  const write = (name: string, extra: string[]) => {
    const f = join(dir, name)
    writeFileSync(f, [...base, ...extra].join('\n') + '\n')
    return f
  }

  assert.throws(() => loadWorldConfig(write('missing-root.yaml', [
    'bot_assignments:',
    '  bot7: { strategy_id: hs300 }',
  ])), /strategy_library_root/)
  assert.throws(() => loadWorldConfig(write('unknown-strategy.yaml', [
    'strategy_library_root: ./strategies/index-products',
    'bot_assignments:',
    '  bot7: { strategy_id: no_such }',
  ])), /not found in strategy library/)
  assert.throws(() => loadWorldConfig(write('bad-code.yaml', [
    'strategy_library_root: ./strategies/index-products',
    'bot_assignments:',
    '  bot7:',
    '    strategy_id: hs300',
    '    buyable_fund_codes: ["ABC"]',
  ])), /6-digit fund code/)
  rmSync(dir, { recursive: true, force: true })
})

test('loadWorldConfig 崩盘触发字段：缺省时安全默认', () => {
  const p = tmpYaml(`
research_loop: /opt/rl
bots: [bot16d]
replay: { from: "2025-01-02", to: "2025-06-28" }
simworld_upstream_url: http://127.0.0.1:18078/mcp
`)
  const c = loadWorldConfig(p)
  assert.equal(c.crashTriggerEnabled, false)
  assert.equal(c.crashTriggerDailyMovePct, 3)
  assert.equal(c.crashTriggerDrawdownPct, 8)
  assert.deepEqual(c.crashTriggerBenchmark, { code: '000300.SH', name: '沪深300' })
})

test('loadWorldConfig 崩盘触发字段：显式覆盖', () => {
  const p = tmpYaml(`
research_loop: /opt/rl
bots: [bot16d]
replay: { from: "2025-01-02", to: "2025-06-28" }
simworld_upstream_url: http://127.0.0.1:18078/mcp
crash_trigger_enabled: true
crash_trigger_daily_move_pct: 2.5
crash_trigger_drawdown_pct: 10
crash_trigger_benchmark: { code: "399989.SZ", name: "中证医疗" }
`)
  const c = loadWorldConfig(p)
  assert.equal(c.crashTriggerEnabled, true)
  assert.equal(c.crashTriggerDailyMovePct, 2.5)
  assert.equal(c.crashTriggerDrawdownPct, 10)
  assert.deepEqual(c.crashTriggerBenchmark, { code: '399989.SZ', name: '中证医疗' })
})
