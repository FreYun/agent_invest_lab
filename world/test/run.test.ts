import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { runWorld, botServerArgv, openclawJsonSource, loopConfigPath, patchPiOpenclawJsonMemory, seedPiAgentBot, isResearchDay, proxyEnvSupplement } from '../src/run.ts'
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
    researchDayEvery: 0,
    // Tests don't exercise the actual research-day wall-clock; keep low so
    // first-day-extended-budget tests (cursor=0 → researchDayTimeoutSeconds)
    // don't drag a 1-day hanging-bot test out for 300s.
    researchDayTimeoutSeconds: 5,
    rlConfigBase: cfgBase,
    shadowInclude: ['SOUL.md'],
    loop: 'research-loop',
    // Stub bots never call MCP tools, so the proxy never makes upstream
    // requests. Any URL is fine — port 1 is deterministically closed locally.
    simworldUpstreamUrl: 'http://127.0.0.1:1/mcp',
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
    researchDayEvery: 0,
    researchDayTimeoutSeconds: 300,
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'research-loop',
    openclawRoot: undefined,
    piServerEntry: undefined,
    simworldUpstreamUrl: 'http://127.0.0.1:1/mcp',
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
  // 首日发完整规则（含 simworld-data + portfolio_* 提示），次日发精简规则 + AUTONOMY 自治块
  assert.match(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-14', 'bot1'), 'utf8'), /simworld-data/)
  assert.match(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-14', 'bot1'), 'utf8'), /portfolio_place_buy_order/)
  assert.match(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-15', 'bot1'), 'utf8'), /规则同前/)
  assert.match(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-15', 'bot1'), 'utf8'), /今天的节奏由你定/)
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
  config.researchDayTimeoutSeconds = 1 // first day reuses research-day budget; keep it tight
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

test('runWorld no longer fails when a day has no quotes.json (deprecated: prompt 走 simworld-data MCP，days/<d>/quotes.json 已不被消费)', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14', '2024-03-15'] })
  rmSync(P.quotesFile(worldRoot, '2024-03-15'), { force: true })
  // 之前会 reject /quotes\.json|missing/；现在 setup 不再校验 quotes.json，run 应该正常完成。
  await runWorld({ worldRoot, config, runId: 'r3-no-quotes', startBotServer: stubStartBotServer })
  assert.equal(readState(worldRoot).status, 'done')
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

test('runWorld pins WORLD_DATE_OVERRIDE_FILE to the current world day before each chat (research-loop reads this to keep its system-prompt date in the replay sandbox)', async () => {
  const dates = ['2024-03-14', '2024-03-15', '2024-03-18']
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates })
  // Custom starter: forwards WORLD_DATE_OVERRIDE_FILE (set by run.ts) into the
  // stub's env and turns on the "echo file contents into reply" spy.
  const start = (botId: string, _argv: string[]) => BotServer.start(botId, {
    argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`],
    env: {
      STUB_ECHO_WORLD_DATE_FILE: '1',
      // forward the file path the same way run.ts does (the path is deterministic from worldRoot+runId)
      WORLD_DATE_OVERRIDE_FILE: P.worldDateOverrideFile(worldRoot, 'rdate'),
    },
    readyTimeoutMs: 5000,
  })
  await runWorld({ worldRoot, config, runId: 'rdate', startBotServer: start })

  // After the run the file persists with the LAST day's value.
  assert.equal(readFileSync(P.worldDateOverrideFile(worldRoot, 'rdate'), 'utf8'), '2024-03-18')
  // Each day's reply was generated AFTER the file was pinned to that day → the spy
  // tag in the reply should match the day the chat fired on. Proves the write
  // happens before chatOneBot dispatches, not just at the end of the run.
  for (const d of dates) {
    const reply = JSON.parse(readFileSync(P.replyFile(worldRoot, 'rdate', d, 'bot1'), 'utf8'))
    assert.equal(reply.reply, `stub reply [world-date=${d}]`, `day ${d} reply should embed pinned date`)
  }
  cleanup()
})

test('isResearchDay: every=0 disables; every=5 fires on cursor 4,9,14 (1-based 5,10,15)', () => {
  // 0 = 关闭
  for (let i = 0; i < 20; i++) assert.equal(isResearchDay(i, 0), false)
  // every=5：(cursor+1) % 5 === 0
  const hits: number[] = []
  for (let i = 0; i < 16; i++) if (isResearchDay(i, 5)) hits.push(i)
  assert.deepEqual(hits, [4, 9, 14])
  // every=1：每天都是研究日
  for (let i = 0; i < 5; i++) assert.equal(isResearchDay(i, 1), true)
})

test('proxyEnvSupplement: returns {} when parent env has no proxy vars', () => {
  const env = { PATH: '/usr/bin', HOME: '/h' }
  assert.deepEqual(proxyEnvSupplement(env), {})
})

test('proxyEnvSupplement: enables --use-env-proxy and strips CIDR from NO_PROXY when proxy is set', () => {
  const env: NodeJS.ProcessEnv = {
    HTTPS_PROXY: 'http://127.0.0.1:7897',
    NO_PROXY: 'localhost,127.0.0.1,::1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12,example.com',
    NODE_OPTIONS: '--max-old-space-size=4096',
  }
  const sup = proxyEnvSupplement(env)
  // NODE_OPTIONS preserves existing flags + appends --use-env-proxy
  assert.match(sup.NODE_OPTIONS, /--max-old-space-size=4096/)
  assert.match(sup.NODE_OPTIONS, /--use-env-proxy/)
  // CIDR entries stripped, hostnames + plain IPs kept
  const noProxyParts = sup.NO_PROXY.split(',')
  assert.ok(noProxyParts.includes('localhost'))
  assert.ok(noProxyParts.includes('127.0.0.1'))
  assert.ok(noProxyParts.includes('::1'))
  assert.ok(noProxyParts.includes('example.com'))
  for (const cidr of ['192.168.0.0/16', '10.0.0.0/8', '172.16.0.0/12']) {
    assert.ok(!noProxyParts.includes(cidr), `CIDR ${cidr} should be stripped`)
  }
})

test('proxyEnvSupplement: empty NO_PROXY still gets default loopback bypass', () => {
  const sup = proxyEnvSupplement({ HTTP_PROXY: 'http://proxy:8080' })
  const parts = sup.NO_PROXY.split(',')
  assert.ok(parts.includes('localhost'))
  assert.ok(parts.includes('127.0.0.1'))
  assert.ok(parts.includes('::1'))
})

test('proxyEnvSupplement: idempotent on NODE_OPTIONS that already has --use-env-proxy', () => {
  const sup = proxyEnvSupplement({ ALL_PROXY: 'socks5://127.0.0.1:7897', NODE_OPTIONS: '--use-env-proxy' })
  // Don't double-append the flag.
  assert.equal((sup.NODE_OPTIONS.match(/--use-env-proxy/g) ?? []).length, 1)
})

test('runWorld: every-5 cadence still drives the budget split (first/research → extended; other days → per-bot), but the prompt is uniform (no day-type banner — bot self-paces via AUTONOMY)', async () => {
  // 用 5 个连续交易日 + 一个 dummy bot。Prompt 这层已经去掉研究日/交易日 banner（统一让 bot 自己定节奏）；
  // 但 budget 仍然受 researchDayEvery 控制——首日 + 第 5 天用 researchDayTimeoutSeconds，其余用 perBotTimeoutSeconds。
  const dates = ['2024-03-14', '2024-03-15', '2024-03-18', '2024-03-19', '2024-03-20']
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates })
  config.researchDayEvery = 5
  config.researchDayTimeoutSeconds = 7
  config.perBotTimeoutSeconds = 3
  await runWorld({ worldRoot, config, runId: 'rcad', startBotServer: stubStartBotServer })
  const sentOf = (d: string) => readFileSync(P.sentFile(worldRoot, 'rcad', d, 'bot1'), 'utf8')
  // First day: full rules (no AUTONOMY, no banners) — cold start, hand-holding mode.
  assert.doesNotMatch(sentOf(dates[0]), /今天的节奏由你定/)
  assert.doesNotMatch(sentOf(dates[0]), /今天是研究日/)
  assert.doesNotMatch(sentOf(dates[0]), /今天是普通交易日/)
  // Day 2..5 (non-first): AUTONOMY block present, identical regardless of research-day-ness.
  for (const d of dates.slice(1)) {
    assert.match(sentOf(d), /今天的节奏由你定/, `${d} should carry AUTONOMY block`)
    assert.doesNotMatch(sentOf(d), /今天是研究日/, `${d} prompt must not carry the deprecated research-day banner`)
    assert.doesNotMatch(sentOf(d), /今天是普通交易日/, `${d} prompt must not carry the deprecated trading-day banner`)
  }
  // run.log: budget split is unchanged — day 1 + day 5 get extended (7s), days 2-4 get the 3s per-bot budget.
  const runLog = readFileSync(P.runLogFile(worldRoot, 'rcad'), 'utf8')
  assert.match(runLog, /day 1\/5: 2024-03-14 \[first day\][^]*\(timeout=7s\)/, 'day 1 should be tagged [first day] with timeout=7s')
  assert.match(runLog, /day 2\/5: 2024-03-15[^]*\(timeout=3s\)/, 'day 2 should use the per-bot 3s budget')
  assert.match(runLog, /day 5\/5: 2024-03-20 \[research day\][^]*\(timeout=7s\)/, 'day 5 should be tagged [research day] with timeout=7s')
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
    researchDayEvery: 0,
    researchDayTimeoutSeconds: 300,
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'research-loop',
    openclawRoot: undefined,
    piServerEntry: undefined,
    simworldUpstreamUrl: 'http://127.0.0.1:1/mcp',
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
    researchDayEvery: 0,
    researchDayTimeoutSeconds: 300,
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'openclaw-pi',
    openclawRoot: '/tmp/oc',
    piServerEntry: '/tmp/oc/src/agents/agent_invest_pi_stdio_server.ts',
    simworldUpstreamUrl: 'http://127.0.0.1:1/mcp',
  }
  const argv = botServerArgv(cfg, 'bot7', '/tmp/ws/bot7', '/tmp/runs/r1/rl-openclaw/openclaw.json')
  assert.equal(argv[0], process.execPath)
  assert.equal(argv[1], '--import')
  assert.equal(argv[2], 'file:///tmp/oc/node_modules/tsx/dist/loader.mjs')
  assert.equal(argv[3], '/tmp/oc/src/agents/agent_invest_pi_stdio_server.ts')
  assert.deepEqual(argv.slice(4), ['--bot-id', 'bot7', '--workspace', '/tmp/ws/bot7', '--openclaw-json', '/tmp/runs/r1/rl-openclaw/openclaw.json'])
})

test('botServerArgv: openclaw-pi does NOT append --sessions-dir (piSessionsDir flows via env OPENCLAW_AGENTS_DIR, not argv)', () => {
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
    researchDayEvery: 0,
    researchDayTimeoutSeconds: 300,
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'openclaw-pi',
    openclawRoot: '/tmp/oc',
    piServerEntry: '/tmp/oc/src/agents/agent_invest_pi_stdio_server.ts',
    piSessionsDir: '/home/rooot/agent_invest_lab/session',
    simworldUpstreamUrl: 'http://127.0.0.1:1/mcp',
  }
  const argv = botServerArgv(cfg, 'bot7', '/tmp/ws/bot7', '/tmp/runs/r1/rl-openclaw/openclaw.json')
  assert.equal(argv.includes('--sessions-dir'), false)
})

test('seedPiAgentBot: copies auth files from source agents dir on first call, leaves existing dest alone', () => {
  const root = mkdtempSync(join(tmpdir(), 'seed-'))
  const source = join(root, 'src-agents'); mkdirSync(join(source, 'bot7/agent'), { recursive: true })
  writeFileSync(join(source, 'bot7/agent/auth-profiles.json'), '{"profile":"original"}')
  writeFileSync(join(source, 'bot7/agent/models.json'), '{}')
  const dest = join(root, 'pi-sessions')

  seedPiAgentBot(dest, source, 'bot7')
  assert.equal(readFileSync(join(dest, 'agents/bot7/agent/auth-profiles.json'), 'utf8'), '{"profile":"original"}')
  assert.equal(existsSync(join(dest, 'agents/bot7/agent/models.json')), true)

  // Second call: don't overwrite
  writeFileSync(join(dest, 'agents/bot7/agent/auth-profiles.json'), '{"profile":"local-edit"}')
  seedPiAgentBot(dest, source, 'bot7')
  assert.equal(readFileSync(join(dest, 'agents/bot7/agent/auth-profiles.json'), 'utf8'), '{"profile":"local-edit"}')

  rmSync(root, { recursive: true, force: true })
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
    researchDayEvery: 0,
    researchDayTimeoutSeconds: 300,
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'openclaw-pi',
    openclawRoot: undefined,
    piServerEntry: undefined,
    simworldUpstreamUrl: 'http://127.0.0.1:1/mcp',
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
    researchDayEvery: 0,
    researchDayTimeoutSeconds: 300,
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'openclaw-pi',
    openclawRoot: undefined,
    piServerEntry: '/tmp/oc/src/agents/agent_invest_pi_stdio_server.ts',
    simworldUpstreamUrl: 'http://127.0.0.1:1/mcp',
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

test('patchPiOpenclawJsonMemory: leaves file alone when mcp.mem0 is absent (openclaw schema would reject unknown mcp keys)', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pi-patch-'))
  const p = join(dir, 'openclaw.json')
  writeFileSync(p, JSON.stringify({ top: 'value' }) + '\n')
  patchPiOpenclawJsonMemory(dir, 'http://127.0.0.1:9999')
  const after = JSON.parse(readFileSync(p, 'utf8'))
  assert.equal(after.mcp, undefined)
  assert.equal(after.top, 'value')
})

test('patchPiOpenclawJsonMemory: leaves file alone when mcp exists but has no mem0', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pi-patch-'))
  const p = join(dir, 'openclaw.json')
  writeFileSync(p, JSON.stringify({ mcp: { servers: { foo: 'bar' } } }) + '\n')
  patchPiOpenclawJsonMemory(dir, 'http://127.0.0.1:9999')
  const after = JSON.parse(readFileSync(p, 'utf8'))
  assert.equal(after.mcp.servers.foo, 'bar')
  assert.equal('mem0' in after.mcp, false)
})
