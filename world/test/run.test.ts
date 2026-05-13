import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { runWorld, botServerArgv, openclawJsonSource, loopConfigPath, patchPiOpenclawJsonMemory } from '../src/run.ts'
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
  // 源 bot 模板（research_loop 路径在 e2e 里不会被真的执行，但 buildShadowWorkspace 需要它存在）
  const botsRoot = join(root, 'bots')
  for (const b of opts.bots) {
    const ws = join(botsRoot, b)
    mkdirSync(ws, { recursive: true })
    writeFileSync(join(ws, 'SOUL.md'), `# soul ${b}`)
  }
  // 共享技能库（注入 extra_roots，存在即可）
  const skillsRoot = join(root, 'skills')
  mkdirSync(skillsRoot, { recursive: true })
  // 凭据 JSON（run.ts 会复制进 rl-openclaw 目录；空 JSON 足够通过）
  const openclawJson = join(root, 'openclaw.json')
  writeFileSync(openclawJson, '{}\n')
  // rl-config base
  const cfgBase = join(root, 'trading-rl-config.base.json')
  writeFileSync(cfgBase, JSON.stringify({ model: { primary: {} }, mcp: { servers: {} }, plugins: { 'memory-mem0': { enabled: true } } }))
  const config: WorldConfig = {
    researchLoop: '/nonexistent/research-loop',
    botsRoot,
    openclawJson,
    skillsRoot,
    bots: opts.bots,
    replay: { from: opts.dates[0], to: opts.dates[opts.dates.length - 1] },
    calendar: P.calendarFile(worldRoot),
    concurrency: 4,
    perBotTimeoutSeconds: 30,
    rlConfigBase: cfgBase,
    shadowInclude: ['SOUL.md'],
    loop: 'research-loop',
  }
  return { worldRoot, config, cleanup: () => rmSync(root, { recursive: true, force: true }) }
}

function piBaseConfig(overrides: Partial<WorldConfig> = {}): WorldConfig {
  return {
    researchLoop: '/tmp/research-loop/ts',
    botsRoot: '/tmp/bots',
    openclawJson: '/tmp/oc.json',
    skillsRoot: '/tmp/skills',
    bots: ['bot7'],
    replay: { from: '2024-01-02', to: '2024-01-03' },
    calendar: '/tmp/cal.json',
    concurrency: 1,
    perBotTimeoutSeconds: 30,
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'research-loop',
    openclawRoot: undefined,
    piServerEntry: undefined,
    ...overrides,
  }
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
  // 生成的 rl-config 写入了 mem0 url；openclaw_dir 指向 run 专属目录且已创建；
  // extra_roots 指向 world 的共享 skills 库
  const genCfg = JSON.parse(readFileSync(P.runConfigFile(worldRoot, 'r1'), 'utf8'))
  assert.match(genCfg.mcp.mem0, /^http:\/\/127\.0\.0\.1:\d+$/)
  assert.equal(genCfg.openclaw_dir, join(P.runDir(worldRoot, 'r1'), 'rl-openclaw'))
  assert.equal(existsSync(genCfg.openclaw_dir), true)
  assert.deepEqual(genCfg.extra_roots, [config.skillsRoot])
  // openclaw.json 已从 config.openclawJson 复制进 run 专属 rl-openclaw 目录
  assert.ok(existsSync(join(P.runDir(worldRoot, 'r1'), 'rl-openclaw', 'openclaw.json')))
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

test('runWorld writes a running status as soon as a bot chat is dispatched', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14'] })
  config.perBotTimeoutSeconds = 2
  const start = (botId: string, _argv: string[]) => BotServer.start(botId, {
    argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`],
    readyTimeoutMs: 5000,
    env: { STUB_CHAT_DELAY_MS: '300' },
  })
  const runPromise = runWorld({ worldRoot, config, runId: 'rrun', startBotServer: start })
  await new Promise(resolve => setTimeout(resolve, 80))
  const running = JSON.parse(readFileSync(P.statusFile(worldRoot, 'rrun', '2024-03-14', 'bot1'), 'utf8'))
  assert.equal(running.status, 'running')
  assert.equal(typeof running.started_at, 'string')
  assert.equal('finished_at' in running, false)
  await runPromise
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

test('runWorld: a bot whose process exits mid-chat is recorded dead and the run still finishes', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1', 'bot7'], dates: ['2024-03-14', '2024-03-15'] })
  const start = (botId: string, _argv: string[]) => BotServer.start(botId, {
    argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`],
    readyTimeoutMs: 5000,
    env: botId === 'bot7' ? { STUB_CHAT_MODE: 'exit' } : {},
  })
  await runWorld({ worldRoot, config, runId: 'rdead', startBotServer: start })
  assert.equal(readState(worldRoot).status, 'done')
  // bot1 ok on both days
  assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'rdead', '2024-03-14', 'bot1'), 'utf8')).status, 'ok')
  assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'rdead', '2024-03-15', 'bot1'), 'utf8')).status, 'ok')
  // bot7 dead on day1 (process exits during chat); day2 also dead (disabled)
  assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'rdead', '2024-03-14', 'bot7'), 'utf8')).status, 'dead')
  assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'rdead', '2024-03-15', 'bot7'), 'utf8')).status, 'dead')
  cleanup()
})

test('runWorld aborts (status=aborted) when a STOP sentinel is present', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14', '2024-03-15', '2024-03-18'] })
  // 在 runWorld 之前就把 STOP 哨兵放好（直接写 P.stopFile 路径）
  const stopPath = P.stopFile(worldRoot, 'rstop')
  mkdirSync(dirname(stopPath), { recursive: true })
  writeFileSync(stopPath, 'stop\n')
  await runWorld({ worldRoot, config, runId: 'rstop', startBotServer: stubStartBotServer })
  const st = readState(worldRoot)
  assert.equal(st.status, 'aborted')
  // 循环在第一次迭代顶部就发现 STOP → cursor 停在 0，没有任何当天产物
  assert.equal(st.cursor, 0)
  assert.equal(existsSync(P.sentFile(worldRoot, 'rstop', '2024-03-14', 'bot1')), false)
  cleanup()
})

test('botServerArgv: research-loop branch points at researchLoop/server.ts with --config', () => {
  const cfg: WorldConfig = {
    researchLoop: '/tmp/research-loop/ts',
    botsRoot: '/tmp/bots',
    openclawJson: '/tmp/oc.json',
    skillsRoot: '/tmp/skills',
    bots: ['bot7'],
    replay: { from: '2024-01-02', to: '2024-01-03' },
    calendar: '/tmp/cal.json',
    concurrency: 1,
    perBotTimeoutSeconds: 30,
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'research-loop',
    openclawRoot: undefined,
    piServerEntry: undefined,
  }
  const argv = botServerArgv(cfg, 'bot7', '/tmp/ws/bot7', '/tmp/runs/r1/trading-rl-config.json')
  assert.equal(argv[0], process.execPath)
  assert.equal(argv[1], '--experimental-strip-types')
  assert.equal(argv[2], '/tmp/research-loop/ts/server.ts')
  assert.deepEqual(argv.slice(3), ['--bot-id', 'bot7', '--workspace', '/tmp/ws/bot7', '--config', '/tmp/runs/r1/trading-rl-config.json'])
})

test('botServerArgv: openclaw-pi branch points at piServerEntry with --openclaw-json', () => {
  const cfg: WorldConfig = {
    researchLoop: '/tmp/research-loop/ts',
    botsRoot: '/tmp/bots',
    openclawJson: '/tmp/oc.json',
    skillsRoot: '/tmp/skills',
    bots: ['bot7'],
    replay: { from: '2024-01-02', to: '2024-01-03' },
    calendar: '/tmp/cal.json',
    concurrency: 1,
    perBotTimeoutSeconds: 30,
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'openclaw-pi',
    openclawRoot: '/tmp/oc',
    piServerEntry: '/tmp/oc/src/agents/agent_invest_pi_stdio_server.ts',
  }
  const argv = botServerArgv(cfg, 'bot7', '/tmp/ws/bot7', '/tmp/runs/r1/rl-openclaw/openclaw.json')
  assert.equal(argv[0], process.execPath)
  assert.equal(argv[1], '--import')
  assert.equal(argv[2], 'file:///tmp/oc/node_modules/tsx/dist/loader.mjs')
  assert.equal(argv[3], '/tmp/oc/src/agents/agent_invest_pi_stdio_server.ts')
  assert.deepEqual(argv.slice(4), ['--bot-id', 'bot7', '--workspace', '/tmp/ws/bot7', '--openclaw-json', '/tmp/runs/r1/rl-openclaw/openclaw.json'])
})

test('botServerArgv: openclaw-pi without piServerEntry throws', () => {
  const cfg: WorldConfig = {
    researchLoop: '/tmp/research-loop/ts',
    botsRoot: '/tmp/bots',
    openclawJson: '/tmp/oc.json',
    skillsRoot: '/tmp/skills',
    bots: ['bot7'],
    replay: { from: '2024-01-02', to: '2024-01-03' },
    calendar: '/tmp/cal.json',
    concurrency: 1,
    perBotTimeoutSeconds: 30,
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'openclaw-pi',
    openclawRoot: undefined,
    piServerEntry: undefined,
  }
  assert.throws(() => botServerArgv(cfg, 'bot7', '/tmp/ws/bot7', '/tmp/oc.json'), /piServerEntry/)
})

test('botServerArgv: openclaw-pi without openclawRoot throws', () => {
  const cfg: WorldConfig = {
    researchLoop: '/tmp/research-loop/ts',
    botsRoot: '/tmp/bots',
    openclawJson: '/tmp/oc.json',
    skillsRoot: '/tmp/skills',
    bots: ['bot7'],
    replay: { from: '2024-01-02', to: '2024-01-03' },
    calendar: '/tmp/cal.json',
    concurrency: 1,
    perBotTimeoutSeconds: 30,
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'openclaw-pi',
    openclawRoot: undefined,
    piServerEntry: '/tmp/oc/src/agents/agent_invest_pi_stdio_server.ts',
  }
  assert.throws(() => botServerArgv(cfg, 'bot7', '/tmp/ws/bot7', '/tmp/oc.json'), /openclawRoot/)
})

test('openclawJsonSource: research-loop returns config.openclawJson', () => {
  const cfg = piBaseConfig({ loop: 'research-loop' })
  assert.equal(openclawJsonSource(cfg), cfg.openclawJson)
})

test('openclawJsonSource: openclaw-pi returns config.openclawJson (same source for both loops)', () => {
  const cfg = piBaseConfig({ loop: 'openclaw-pi' })
  assert.equal(openclawJsonSource(cfg), cfg.openclawJson)
})

test('loopConfigPath: research-loop points at runConfigFile (trading-rl-config.json)', () => {
  const cfg = piBaseConfig({ loop: 'research-loop' })
  const p = loopConfigPath(cfg, '/tmp/world', 'r1')
  assert.match(p, /\/runs\/r1\/trading-rl-config\.json$/)
})

test('loopConfigPath: openclaw-pi points at run rl-openclaw/openclaw.json', () => {
  const cfg = piBaseConfig({ loop: 'openclaw-pi' })
  const p = loopConfigPath(cfg, '/tmp/world', 'r1')
  assert.match(p, /\/runs\/r1\/rl-openclaw\/openclaw\.json$/)
})

test('patchPiOpenclawJsonMemory: rewrites mcp.mem0 to the given URL', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pi-patch-'))
  const p = join(dir, 'openclaw.json')
  writeFileSync(p, JSON.stringify({ mcp: { mem0: 'http://old:1234', other: 'keep' }, top: 'keep' }) + '\n')
  patchPiOpenclawJsonMemory(dir, 'http://127.0.0.1:9999')
  const after = JSON.parse(readFileSync(p, 'utf8'))
  assert.equal(after.mcp.mem0, 'http://127.0.0.1:9999')
  assert.equal(after.mcp.other, 'keep')
  assert.equal(after.top, 'keep')
})

test('patchPiOpenclawJsonMemory: creates mcp object if missing', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pi-patch-'))
  const p = join(dir, 'openclaw.json')
  writeFileSync(p, JSON.stringify({ top: 'value' }) + '\n')
  patchPiOpenclawJsonMemory(dir, 'http://127.0.0.1:9999')
  const after = JSON.parse(readFileSync(p, 'utf8'))
  assert.equal(after.mcp.mem0, 'http://127.0.0.1:9999')
  assert.equal(after.top, 'value')
})
