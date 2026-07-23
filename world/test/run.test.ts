import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { runWorld, requestPause, requestStop, botServerArgv, openclawJsonSource, loopConfigPath, patchPiOpenclawJsonMemory, seedPiAgentBot, isResearchDay, isChatDayAt, previousChatCursor, isoWeekKey, proxyEnvSupplement } from '../src/run.ts'
import { BotServer } from '../src/botServer.ts'
import * as P from '../src/paths.ts'
import { readState, writeState } from '../src/state.ts'
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
    researchDayTimeoutSeconds: 5, chatStepDays: 1, chatStepMode: 'trading_days',
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
    researchDayTimeoutSeconds: 300, chatStepDays: 1, chatStepMode: 'trading_days',
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

  const st = readState(worldRoot, 'r1')
  assert.equal(st.status, 'done')
  assert.equal(st.cursor, 2)
  assert.equal(st.run_id, 'r1')
  assert.ok(st.memory_port > 0)
  // pid stamped at run start (orphan-detection signal) and survives writeState
  // spreads through the loop + teardown.
  assert.equal(st.pid, process.pid)

  for (const d of ['2024-03-14', '2024-03-15']) {
    for (const b of ['bot1', 'bot7']) {
      assert.ok(existsSync(P.sentFile(worldRoot, 'r1', d, b)), `sent ${d} ${b}`)
      assert.match(readFileSync(P.sentFile(worldRoot, 'r1', d, b), 'utf8'), new RegExp(`当前世界日期：${d}`))
      const reply = JSON.parse(readFileSync(P.replyFile(worldRoot, 'r1', d, b), 'utf8'))
      assert.equal(reply.reply, 'stub reply')
      assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'r1', d, b), 'utf8')).status, 'ok')
    }
  }
  // 首日发完整规则（含 simworld-data + portfolio_* 提示 + methodology Day-1 hint）；
  // 次日发精简规则——不再注入 AUTONOMY（embedded strategy 已删），节奏由 bot 自己的 methodology 决定
  assert.match(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-14', 'bot1'), 'utf8'), /simworld-data/)
  assert.match(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-14', 'bot1'), 'utf8'), /portfolio_place_buy_order/)
  // Day 1 prompt 提示 bot 按 system prompt 里的 ## METHODOLOGY.md section 决策（methodology-only 模式）
  assert.match(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-14', 'bot1'), 'utf8'), /你的 active methodology 已就位/)
  assert.match(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-14', 'bot1'), 'utf8'), /## METHODOLOGY\.md/)
  assert.match(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-15', 'bot1'), 'utf8'), /今天的节奏（按顺序）/)
  // Day N 不再有 AUTONOMY block
  assert.doesNotMatch(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-15', 'bot1'), 'utf8'), /今天的节奏由你定/)
  // 影子 workspace
  assert.match(readFileSync(join(P.shadowWorkspaceDir(worldRoot, 'r1', 'bot7'), 'SOUL.md'), 'utf8'), /soul bot7/)
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

test('runWorld: a hanging bot with zero tool calls pauses the run (0s 垃圾日 path)', async () => {
  // hang 模式下 bot 一个 tool 都没调就挂了——typical 网络挂掉的样子，pause 在当天。
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1', 'bot7'], dates: ['2024-03-14', '2024-03-15'] })
  config.perBotTimeoutSeconds = 1
  config.researchDayTimeoutSeconds = 1
  const start = (botId: string, _argv: string[]) => BotServer.start(botId, {
    argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`],
    readyTimeoutMs: 5000,
    env: botId === 'bot7' ? { STUB_CHAT_MODE: 'hang' } : {},
  })
  await runWorld({ worldRoot, config, runId: 'r2', startBotServer: start })
  const st = readState(worldRoot, 'r2')
  assert.equal(st.status, 'paused')
  assert.equal(st.cursor, 0)
  assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'r2', '2024-03-14', 'bot1'), 'utf8')).status, 'ok')
  assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'r2', '2024-03-14', 'bot7'), 'utf8')).status, 'timeout')
  assert.equal(existsSync(P.statusFile(worldRoot, 'r2', '2024-03-15', 'bot1')), false)
  cleanup()
})

test('runWorld: a chat that times out AFTER making tool calls still advances (放松判定：bot14 1-06 擦边场景)', async () => {
  // 真实案例：bot14 在 1-06 chat 跑了 200000ms 客户端 timeout，但 server 端已经做了 18 轮 tool 调用、
  // 8ms 后就吐出 reply。修复前 → pause → resume 重跑同一天。修复后 → toolCalls>0 → 直接推进。
  // bot 仍会被 restartBot 在下一天前换掉，避免老 chat 继续残留 tool 调用串味。
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14', '2024-03-15'] })
  config.perBotTimeoutSeconds = 1
  config.researchDayTimeoutSeconds = 1
  let spawnCount = 0
  const start = (botId: string, _argv: string[]) => {
    spawnCount += 1
    // 第一次 spawn (1-06 那天) hang 但发 3 个 tool.call → timeout 但 toolCalls>0 → advance
    // 第二次 spawn (restart 后跑 1-07) 正常回复
    const env: Record<string, string> = spawnCount === 1 ? { STUB_CHAT_MODE: 'hang', STUB_NOTIFY_TOOL_CALLS: '3' } : {}
    return BotServer.start(botId, {
      argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`],
      readyTimeoutMs: 5000,
      env,
    })
  }
  await runWorld({ worldRoot, config, runId: 'rrelax', startBotServer: start })
  const st = readState(worldRoot, 'rrelax')
  assert.equal(st.status, 'done', 'timeout with tool calls should advance, not pause')
  assert.equal(st.cursor, 2)
  const day1 = JSON.parse(readFileSync(P.statusFile(worldRoot, 'rrelax', '2024-03-14', 'bot1'), 'utf8'))
  assert.equal(day1.status, 'timeout', 'day 1 still records timeout for audit')
  const day2 = JSON.parse(readFileSync(P.statusFile(worldRoot, 'rrelax', '2024-03-15', 'bot1'), 'utf8'))
  assert.equal(day2.status, 'ok', 'day 2 runs on the restarted server and succeeds')
  assert.equal(spawnCount, 2, 'bot was restarted between days because day 1 timed out')
  assert.match(readFileSync(P.runLogFile(worldRoot, 'rrelax'), 'utf8'), /chat timeout but 3 tool call\(s\) made before — will advance via 放松判定/)
  cleanup()
})
test('runWorld: per-day tool-call cap aborts a runaway chat and advances to the next day (postmortem r4 Day26 fix)', async () => {
  // 复现Day26 一 session 跑 104+ tool call、写 8 天未来日 mem0 + 6 单实盘的失控模式。
  // 世界侧硬闸门：默认非深研日 cap=40，poller 每 1s 采样 toolCallCount delta，越过 → server.shutdown()
  // → chat rejects → catch 分支识别 capExceeded → status=timeout（触发 needsRestart 冷启新 server）+
  // toolCalls>0（放松判定当日算已推进，close_my_day 正常收尾，cursor 前进）。次日跑正常 stub。
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14', '2024-03-15'] })
  config.perBotTimeoutSeconds = 30
  config.researchDayTimeoutSeconds = 30
  // 默认 ordinal + every=0 → 每天 drState.forced=drState.authorized=false → 世界侧 cap=40
  let spawnCount = 0
  const start = (botId: string, _argv: string[]) => {
    spawnCount += 1
    // 第一次 spawn: hang + 一次发 50 个 tool.call notification → 1s 后 cap poller 命中 (50 > 40) → shutdown
    // 第二次 spawn: 默认 reply → status=ok，用于验证 needsRestart 生效
    const env: Record<string, string> = spawnCount === 1 ? { STUB_CHAT_MODE: 'hang', STUB_NOTIFY_TOOL_CALLS: '50' } : {}
    return BotServer.start(botId, {
      argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`],
      readyTimeoutMs: 5000,
      env,
    })
  }
  await runWorld({ worldRoot, config, runId: 'rcap', startBotServer: start })
  const st = readState(worldRoot, 'rcap')
  assert.equal(st.status, 'done', 'cap-aborted day advances via 放松判定 (toolCalls>0)，不 pause')
  assert.equal(st.cursor, 2, 'cursor 前进到第二天末尾')
  const day1 = JSON.parse(readFileSync(P.statusFile(worldRoot, 'rcap', '2024-03-14', 'bot1'), 'utf8'))
  assert.equal(day1.status, 'timeout', 'cap-abort 记为 timeout（driver=needsRestart 语义）')
  assert.match(String(day1.error), /cap exceeded/, 'error 里含 cap exceeded 说明来源')
  const day2 = JSON.parse(readFileSync(P.statusFile(worldRoot, 'rcap', '2024-03-15', 'bot1'), 'utf8'))
  assert.equal(day2.status, 'ok', 'day 2 冷启的新 server 上正常回复')
  assert.equal(spawnCount, 2, 'day 1 timeout 后触发 needsRestart，day 2 冷启一次')
  const runLog = readFileSync(P.runLogFile(worldRoot, 'rcap'), 'utf8')
  assert.match(runLog, /tool-call cap exceeded on 2024-03-14/, '闸门触发写入 run log')
  assert.match(runLog, /cap-abort finished on 2024-03-14/, '收尾也留痕')
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
  assert.equal(readState(worldRoot, 'r3-no-quotes').status, 'done')
  cleanup()
})

test('runWorld: a bot whose process exits mid-chat is recorded dead and the run pauses on that day', async () => {
  // 新策略：dead 也是失败 → pause 在当天，下一天不再发起 chat。
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1', 'bot7'], dates: ['2024-03-14', '2024-03-15'] })
  const start = (botId: string, _argv: string[]) => BotServer.start(botId, {
    argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`],
    readyTimeoutMs: 5000,
    env: botId === 'bot7' ? { STUB_CHAT_MODE: 'exit' } : {},
  })
  await runWorld({ worldRoot, config, runId: 'rdead', startBotServer: start })
  const st = readState(worldRoot, 'rdead')
  assert.equal(st.status, 'paused')
  assert.equal(st.cursor, 0)
  assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'rdead', '2024-03-14', 'bot1'), 'utf8')).status, 'ok')
  assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'rdead', '2024-03-14', 'bot7'), 'utf8')).status, 'dead')
  assert.equal(existsSync(P.statusFile(worldRoot, 'rdead', '2024-03-15', 'bot1')), false, 'day 2 must not run after a dead-bot day 1')
  cleanup()
})

test('runWorld: chat_error + zero tool calls (the 0s 垃圾日 pattern) pauses the run', async () => {
  // 真实事故根因：connection refused 时 chat 1.5s 就 chat_error 返回、tool_trace 空——
  // 当日完全没碰到 LLM。修复前 loop 会连刷一片这样的"0s 垃圾日"；现在第一次就 pause。
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14', '2024-03-15', '2024-03-18'] })
  const start = (botId: string, _argv: string[]) => BotServer.start(botId, {
    argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`],
    readyTimeoutMs: 5000,
    env: { STUB_CHAT_MODE: 'chat_error', STUB_CHAT_ERROR_TEXT: 'fake upstream timeout', STUB_TOOL_TRACE_LEN: '0' },
  })
  await runWorld({ worldRoot, config, runId: 'rce', startBotServer: start })
  const st = readState(worldRoot, 'rce')
  assert.equal(st.status, 'paused')
  assert.equal(st.cursor, 0)
  const s = JSON.parse(readFileSync(P.statusFile(worldRoot, 'rce', '2024-03-14', 'bot1'), 'utf8'))
  assert.equal(s.status, 'error')
  assert.match(s.error, /no tool calls/)
  assert.match(s.error, /fake upstream timeout/)
  assert.equal(existsSync(P.statusFile(worldRoot, 'rce', '2024-03-15', 'bot1')), false)
  assert.equal(existsSync(P.statusFile(worldRoot, 'rce', '2024-03-18', 'bot1')), false)
  cleanup()
})

test('runWorld: chat_error WITH at least one tool call advances to next day (放松判定：tool 调过就算推进)', async () => {
  // 与上一条对照——chat_error 同样被 server 报上来，但 tool_trace 里有真实调用。
  // 说明 bot 已经走通了 LLM→tool 这一段，只是某次 LLM 调用挂了；明天可以接着跑。
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14', '2024-03-15'] })
  const start = (botId: string, _argv: string[]) => BotServer.start(botId, {
    argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`],
    readyTimeoutMs: 5000,
    env: { STUB_CHAT_MODE: 'chat_error', STUB_CHAT_ERROR_TEXT: 'mid-flow LLM hiccup', STUB_TOOL_TRACE_LEN: '3' },
  })
  await runWorld({ worldRoot, config, runId: 'rce-ok', startBotServer: start })
  const st = readState(worldRoot, 'rce-ok')
  assert.equal(st.status, 'done', 'tool_trace 非空时不应 pause，应该跑完两天')
  assert.equal(st.cursor, 2)
  for (const d of ['2024-03-14', '2024-03-15']) {
    assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'rce-ok', d, 'bot1'), 'utf8')).status, 'ok')
  }
  // run.log 留下了 "advancing" 痕迹，便于事后审阅这天确实是 chat_error 但放过的
  assert.match(readFileSync(P.runLogFile(worldRoot, 'rce-ok'), 'utf8'), /chat_error but 3 tool call\(s\) made — advancing/)
  cleanup()
})

test('runWorld: empty reply + zero tool calls pauses the run', async () => {
  // chat 正常返回但 reply 空 + tool_trace 空——bot 一步也没动，等同于失败。
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14', '2024-03-15'] })
  const start = (botId: string, _argv: string[]) => BotServer.start(botId, {
    argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`],
    readyTimeoutMs: 5000,
    env: { STUB_REPLY_TEXT: '', STUB_TOOL_TRACE_LEN: '0' },
  })
  await runWorld({ worldRoot, config, runId: 'rempty', startBotServer: start })
  const st = readState(worldRoot, 'rempty')
  assert.equal(st.status, 'paused')
  assert.equal(st.cursor, 0)
  const s14 = JSON.parse(readFileSync(P.statusFile(worldRoot, 'rempty', '2024-03-14', 'bot1'), 'utf8'))
  assert.equal(s14.status, 'error')
  assert.match(s14.error, /no tool calls/)
  assert.equal(existsSync(P.statusFile(worldRoot, 'rempty', '2024-03-15', 'bot1')), false)
  cleanup()
})

test('runWorld aborts (status=aborted) when a STOP sentinel is present', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14', '2024-03-15', '2024-03-18'] })
  // 在 runWorld 之前就把 STOP 哨兵放好（直接写 P.stopFile 路径）
  const stopPath = P.stopFile(worldRoot, 'rstop')
  mkdirSync(dirname(stopPath), { recursive: true })
  writeFileSync(stopPath, 'stop\n')
  await runWorld({ worldRoot, config, runId: 'rstop', startBotServer: stubStartBotServer })
  const st = readState(worldRoot, 'rstop')
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

test('isoWeekKey: maps any weekday to that ISO week Monday (2024-01-01 is a Monday)', () => {
  assert.equal(isoWeekKey('2024-01-02'), '2024-01-01') // Tue → Mon
  assert.equal(isoWeekKey('2024-01-07'), '2024-01-01') // Sun → same week Mon
  assert.equal(isoWeekKey('2024-01-08'), '2024-01-08') // Mon → itself
})

test('isChatDayAt trading_days: cursor % chatStepDays, cursor 0 always a chat day', () => {
  const dates = Array.from({ length: 8 }, (_, i) => `2024-01-${String(i + 2).padStart(2, '0')}`)
  const hits: number[] = []
  for (let c = 0; c < dates.length; c++) if (isChatDayAt(c, dates, 'trading_days', 5)) hits.push(c)
  assert.deepEqual(hits, [0, 5]) // step=5 → 0,5
})

test('isChatDayAt weekly: first trading day of each ISO week (holiday-robust, no drift)', () => {
  // 01-15 stands in for a post-holiday Monday; weekly fires on it regardless of how many
  // trading days the modulo would have counted — alignment is calendar-driven, not count-driven.
  const dates = ['2024-01-02', '2024-01-03', '2024-01-04', '2024-01-05', '2024-01-08', '2024-01-09', '2024-01-15', '2024-01-16']
  const hits: number[] = []
  for (let c = 0; c < dates.length; c++) if (isChatDayAt(c, dates, 'weekly', 1)) hits.push(c)
  assert.deepEqual(hits, [0, 4, 6]) // 01-02(cursor0), 01-08(new wk), 01-15(new wk)
})

test('isChatDayAt monthly: first trading day of each calendar month', () => {
  const dates = ['2024-01-30', '2024-01-31', '2024-02-01', '2024-02-02', '2024-03-01']
  const hits: number[] = []
  for (let c = 0; c < dates.length; c++) if (isChatDayAt(c, dates, 'monthly', 1)) hits.push(c)
  assert.deepEqual(hits, [0, 2, 4]) // 01-30(cursor0), 02-01(new month), 03-01(new month)
})

test('isChatDayAt weekly weekday=3: fires on Wednesday of each week (first partial week covered by cursor 0)', () => {
  // 三个完整周(周一~周五),weekday=3 → 每周三决策;首周已由 cursor 0 覆盖,故首周三被抑制。
  const dates = [
    '2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04', '2024-01-05', // 周一~周五
    '2024-01-08', '2024-01-09', '2024-01-10', '2024-01-11', '2024-01-12',
    '2024-01-15', '2024-01-16', '2024-01-17', '2024-01-18', '2024-01-19',
  ]
  const hits: number[] = []
  for (let c = 0; c < dates.length; c++) if (isChatDayAt(c, dates, 'weekly', 1, { weekday: 3 })) hits.push(c)
  assert.deepEqual(hits, [0, 7, 12]) // 01-01(cursor0), 01-10(周三), 01-17(周三)
})

test('isChatDayAt weekly weekday=3 holiday: 周三放假 → 顺延到本周下一个交易日(周四)', () => {
  // 第二周缺 01-10(周三) → weekday=3 顺延到 01-11(周四,首个 dow≥3)。
  const dates = [
    '2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04', '2024-01-05',
    '2024-01-08', '2024-01-09', '2024-01-11', '2024-01-12', // 无 01-10
  ]
  const hits: number[] = []
  for (let c = 0; c < dates.length; c++) if (isChatDayAt(c, dates, 'weekly', 1, { weekday: 3 })) hits.push(c)
  assert.deepEqual(hits, [0, 7]) // 01-01(cursor0), 01-11(周三放假→顺延周四)
})

test('isChatDayAt monthly nth=-1: last trading day of each month (first month covered by cursor 0)', () => {
  const dates = [
    '2024-01-02', '2024-01-30', '2024-01-31', // Jan
    '2024-02-01', '2024-02-28', '2024-02-29', // Feb
    '2024-03-01', '2024-03-28', '2024-03-29', // Mar
  ]
  const hits: number[] = []
  for (let c = 0; c < dates.length; c++) if (isChatDayAt(c, dates, 'monthly', 1, { monthlyNth: -1 })) hits.push(c)
  assert.deepEqual(hits, [0, 5, 8]) // 01-02(cursor0), 02-29(月末), 03-29(月末)
})

test('isChatDayAt monthly nth=5: 5th trading day of each month; out-of-range clamps to month end', () => {
  const dates = [
    '2024-01-02', '2024-01-03', '2024-01-04', '2024-01-05', '2024-01-08', // Jan(首月,被 cursor0 覆盖)
    '2024-02-01', '2024-02-02', '2024-02-05', '2024-02-06', '2024-02-07', // Feb 5个,第5个=02-07
  ]
  const nth5: number[] = []
  for (let c = 0; c < dates.length; c++) if (isChatDayAt(c, dates, 'monthly', 1, { monthlyNth: 5 })) nth5.push(c)
  assert.deepEqual(nth5, [0, 9]) // 01-02(cursor0), 02-07(Feb第5个交易日)
  // nth 超过当月交易日数 → 夹到月末(此处 Feb 共5天,nth=99 落到 02-07)
  const nthBig: number[] = []
  for (let c = 0; c < dates.length; c++) if (isChatDayAt(c, dates, 'monthly', 1, { monthlyNth: 99 })) nthBig.push(c)
  assert.deepEqual(nthBig, [0, 9])
})

test('previousChatCursor: trading-days-since-last-decision for weekly periods', () => {
  const dates = ['2024-01-02', '2024-01-03', '2024-01-04', '2024-01-05', '2024-01-08', '2024-01-09', '2024-01-15', '2024-01-16']
  assert.equal(previousChatCursor(0, dates, 'weekly', 1), 0) // first day → self
  assert.equal(previousChatCursor(4, dates, 'weekly', 1), 0) // 01-08 back to 01-02 → 4 trading days
  assert.equal(previousChatCursor(6, dates, 'weekly', 1), 4) // 01-15 back to 01-08 → 2 trading days
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

test('runWorld: every-5 cadence still drives the budget split (first/research → extended; other days → per-bot), but the prompt has no day-type banner or AUTONOMY (节奏由 bot 自己 Day 1 写的策略决定)', async () => {
  // 用 5 个连续交易日 + 一个 dummy bot。Prompt 已去掉 AUTONOMY 和研究日/交易日 banner；
  // budget 仍然受 researchDayEvery 控制——首日 + 第 5 天用 researchDayTimeoutSeconds，其余用 perBotTimeoutSeconds。
  const dates = ['2024-03-14', '2024-03-15', '2024-03-18', '2024-03-19', '2024-03-20']
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates })
  config.researchDayEvery = 5
  config.researchDayTimeoutSeconds = 7
  config.perBotTimeoutSeconds = 3
  await runWorld({ worldRoot, config, runId: 'rcad', startBotServer: stubStartBotServer })
  const sentOf = (d: string) => readFileSync(P.sentFile(worldRoot, 'rcad', d, 'bot1'), 'utf8')
  // First day: full rules (no AUTONOMY, no banners) — cold start; 含 methodology Day-1 hint
  assert.doesNotMatch(sentOf(dates[0]), /今天的节奏由你定/)
  assert.doesNotMatch(sentOf(dates[0]), /今天是研究日/)
  assert.doesNotMatch(sentOf(dates[0]), /今天是普通交易日/)
  // methodology-only 模式：Day 1 prompt 指向 system prompt 里的 ## METHODOLOGY.md section（不再要求 bot 现写 MY_STRATEGY）
  assert.match(sentOf(dates[0]), /你的 active methodology 已就位/, 'Day 1 must include methodology hint')
  assert.match(sentOf(dates[0]), /## METHODOLOGY\.md/, 'Day 1 must reference METHODOLOGY.md section')
  // Day 2..5 (non-first): 也没有 AUTONOMY，也没有 banner——uniform 精简 prompt
  for (const d of dates.slice(1)) {
    assert.doesNotMatch(sentOf(d), /今天的节奏由你定/, `${d} should NOT carry AUTONOMY (deleted as embedded strategy)`)
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
    researchDayTimeoutSeconds: 300, chatStepDays: 1, chatStepMode: 'trading_days',
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

test('botServerArgv: research-loop with researchLoopRustBin spawns rust binary "server" subcommand (chat_error path)', () => {
  const cfg: WorldConfig = {
    researchLoop: '/tmp/research-loop/ts',
    researchLoopRustBin: '/tmp/research-loop/rust/target/release/research-loop-rust2',
    botsRoot: '/tmp/bots',
    openclawJson: '/tmp/oc.json',
    skillsRoot: '/tmp/skills',
    bots: ['bot7'],
    replay: { from: '2024-01-02', to: '2024-01-03' },
    calendar: '/tmp/cal.json',
    concurrency: 1,
    perBotTimeoutSeconds: 30,
    researchDayEvery: 0,
    researchDayTimeoutSeconds: 300, chatStepDays: 1, chatStepMode: 'trading_days',
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'research-loop',
    openclawRoot: undefined,
    piServerEntry: undefined,
    simworldUpstreamUrl: 'http://127.0.0.1:1/mcp',
  }
  const argv = botServerArgv(cfg, 'bot7', '/tmp/ws/bot7', '/tmp/runs/r1/trading-rl-config.json')
  assert.deepEqual(argv, [
    '/tmp/research-loop/rust/target/release/research-loop-rust2',
    'server',
    '--bot-id', 'bot7',
    '--workspace', '/tmp/ws/bot7',
    '--config', '/tmp/runs/r1/trading-rl-config.json',
  ])
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
    researchDayTimeoutSeconds: 300, chatStepDays: 1, chatStepMode: 'trading_days',
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
    researchDayTimeoutSeconds: 300, chatStepDays: 1, chatStepMode: 'trading_days',
    rlConfigBase: '/tmp/base.json',
    rlOpenclawDir: undefined,
    shadowInclude: [],
    loop: 'openclaw-pi',
    openclawRoot: '/tmp/oc',
    piServerEntry: '/tmp/oc/src/agents/agent_invest_pi_stdio_server.ts',
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
    researchDayTimeoutSeconds: 300, chatStepDays: 1, chatStepMode: 'trading_days',
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
    researchDayTimeoutSeconds: 300, chatStepDays: 1, chatStepMode: 'trading_days',
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

test('cli_tools get_my_history requires --run-id (strict layer contract)', () => {
  // 验证 cli_tools.py 在 --run-id 缺失时退出非零，证明 strict 链路打通。
  // 跑真实 cli_tools.py，需要 fund-portfolio-mcp 的 uv 环境就绪。
  const fundDir = join(HERE, '..', '..', 'fund-portfolio-mcp')
  const r = spawnSync('uv', ['run', 'python', 'cli_tools.py', 'get_my_history', '--bot-id', 'bot_does_not_exist'],
                      { cwd: fundDir, encoding: 'utf8' })
  if (r.error && (r.error as NodeJS.ErrnoException).code === 'ENOENT') {
    // uv 不在 PATH（CI 环境可能没装）—— 跳过而不是 fail
    return
  }
  assert.notStrictEqual(r.status, 0, `expected non-zero exit when --run-id missing, got ${r.status}, stderr: ${r.stderr}`)
  assert.match(r.stderr, /--run-id/, `argparse error should mention --run-id, got: ${r.stderr}`)
})

test('runWorld pauses (status=paused) when a PAUSE sentinel is present at a day boundary', async () => {
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

test('runWorld pauses immediately mid-day: a PAUSE during an in-flight chat kills the day without advancing the cursor', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14', '2024-03-15'] })
  // hang mode: the stub never replies on its own, so the only way the day ends is the
  // pause poller killing the bot server. Proves pause interrupts an in-flight chat.
  const start = (botId: string) => BotServer.start(botId, {
    argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`],
    readyTimeoutMs: 5000,
    env: { STUB_CHAT_MODE: 'hang' },
  })
  // Drop the PAUSE sentinel shortly after the chat goes in flight; the 1s poller picks it up.
  const t = setTimeout(() => { writeFileSync(P.pauseFile(worldRoot, 'rmid'), 'pause\n') }, 300)
  await runWorld({ worldRoot, config, runId: 'rmid', startBotServer: start })
  clearTimeout(t)
  const st = readState(worldRoot, 'rmid')
  assert.equal(st.status, 'paused')
  // day 0 was killed mid-flight → cursor never advanced → resume re-runs day 0
  assert.equal(st.cursor, 0)
  cleanup()
})

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


test("runWorld 系统预读注入三份市场研报 for bot102，不再注入 skill", async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ["bot102"], dates: ["2024-03-14"] })
  // 即便 bot 仍带 skill 目录，也不该被注入（INJECT_PIPELINE_SKILLS 已清空）。
  const botRoot = join(dirname(worldRoot), "bots", "bot102")
  for (const id of ["market-context", "market-mainline", "mainline-rotation", "fund-screening"]) {
    const dir = join(botRoot, "skills", id)
    mkdirSync(dir, { recursive: true })
    writeFileSync(join(dir, "SKILL.md"), `# ${id}\nrequired body for ${id}\n`)
  }
  config.shadowInclude = ["SOUL.md", "skills"]

  // 在 fund.db(P.fundDbFile) 种三份 PIT 报告（as_of 2024-03-01 ≤ 决策日 2024-03-14）。
  const dbPath = P.fundDbFile(worldRoot)
  mkdirSync(dirname(dbPath), { recursive: true })
  const ddl = "CREATE TABLE IF NOT EXISTS market_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, report_type TEXT, as_of_date TEXT, scope TEXT DEFAULT 'global', content_md TEXT, structured_json TEXT, agent_run_id TEXT, generated_at TEXT, UNIQUE(report_type, as_of_date, scope));"
  const ins = (rt: string, body: string) => `INSERT OR REPLACE INTO market_reports(report_type,as_of_date,scope,content_md) VALUES('${rt}','2024-03-01','global','${body}');`
  const seed = ddl + ins("market_context", "CTX-REGIME-BODY") + ins("market_mainline", "MAINLINE-POOL-BODY") + ins("mainline_rotation", "ROTATION-SKELETON-BODY")
  const r = spawnSync("sqlite3", [dbPath], { input: seed, encoding: "utf8" })
  assert.equal(r.status, 0, `seed fund.db failed: ${r.stderr}`)

  await runWorld({ worldRoot, config, runId: "inject-r1", startBotServer: stubStartBotServer })

  const sent = readFileSync(P.sentFile(worldRoot, "inject-r1", "2024-03-14", "bot102"), "utf8")
  // 注入市场研报块 + 三份正文
  assert.match(sent, /【市场研究报告（系统预生成/)
  assert.match(sent, /CTX-REGIME-BODY/)
  assert.match(sent, /MAINLINE-POOL-BODY/)
  assert.match(sent, /ROTATION-SKELETON-BODY/)
  // 不再注入判断管线 skill 块
  assert.doesNotMatch(sent, /【判断管线 skill/)
  assert.doesNotMatch(sent, /────────── skill: market-context/)
  cleanup()
})


test('runWorld installs assigned strategy as active shadow METHODOLOGY and writes assignment audit', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot7', 'bot11'], dates: ['2024-03-14'] })
  const lib = join(dirname(worldRoot), 'strategies', 'index-products')
  mkdirSync(lib, { recursive: true })
  writeFileSync(join(lib, 'hs300.md'), '# HS300 body\nlegacy bot source must not appear here\n')
  writeFileSync(join(lib, 'semi.md'), '# Semi body\nactive semiconductor rules\n')
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
  config.strategyLibraryRoot = lib
  config.botAssignments = {
    bot7: { strategyId: 'hs300', buyableFundCodes: ['000051'] },
    bot11: { strategyId: 'semiconductor' },
  }

  await runWorld({ worldRoot, config, runId: 'strategy-r1', startBotServer: stubStartBotServer })

  const bot7Method = readFileSync(join(P.shadowWorkspaceDir(worldRoot, 'strategy-r1', 'bot7'), 'METHODOLOGY.md'), 'utf8')
  assert.match(bot7Method, /# 当前回测任务/)
  assert.match(bot7Method, /strategy_id: hs300/)
  assert.match(bot7Method, /target_index: 000300.SH/)
  assert.match(bot7Method, /buyable_fund_codes: 000051/)
  assert.match(bot7Method, /# HS300 body/)
  assert.ok(existsSync(join(P.shadowWorkspaceDir(worldRoot, 'strategy-r1', 'bot7'), 'STRATEGY_LIBRARY.md')))
  assert.ok(existsSync(join(P.shadowWorkspaceDir(worldRoot, 'strategy-r1', 'bot7'), 'strategies', 'index-products', 'manifest.yaml')))

  const bot11Method = readFileSync(join(P.shadowWorkspaceDir(worldRoot, 'strategy-r1', 'bot11'), 'METHODOLOGY.md'), 'utf8')
  assert.match(bot11Method, /strategy_id: semiconductor/)
  assert.match(bot11Method, /buyable_fund_codes: 014854/)
  assert.match(bot11Method, /active semiconductor rules/)

  const audit = JSON.parse(readFileSync(join(P.runDir(worldRoot, 'strategy-r1'), 'strategy-assignments.json'), 'utf8'))
  assert.deepEqual(audit.bots.bot7.buyable_fund_codes, ['000051'])
  assert.equal(audit.bots.bot11.strategy_id, 'semiconductor')
  cleanup()
})
