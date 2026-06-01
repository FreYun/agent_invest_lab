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
  const botsRoot = join(root, 'bots')
  for (const b of bots) { mkdirSync(join(botsRoot, b), { recursive: true }); writeFileSync(join(botsRoot, b, 'SOUL.md'), b) }
  const skillsRoot = join(root, 'skills'); mkdirSync(skillsRoot, { recursive: true })
  const openclawJson = join(root, 'openclaw.json'); writeFileSync(openclawJson, '{}\n')
  const cfgBase = join(root, 'base.json'); writeFileSync(cfgBase, JSON.stringify({ mcp: { servers: {} } }))
  const config: WorldConfig = { researchLoop: '/no', botsRoot, openclawJson, skillsRoot, bots, replay: { from: dates[0], to: dates[dates.length - 1] }, calendar: P.calendarFile(worldRoot), concurrency: 4, perBotTimeoutSeconds: 30, researchDayEvery: 0, researchDayTimeoutSeconds: 300, rlConfigBase: cfgBase, rlOpenclawDir: join(root, 'oc'), shadowInclude: ['SOUL.md'], loop: 'research-loop', simworldUpstreamUrl: 'http://127.0.0.1:1/mcp' }
  return { worldRoot, config, cleanup: () => rmSync(root, { recursive: true, force: true }) }
}

test('resumeWorld continues from state.cursor without re-running completed days', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir(['bot1'], ['2024-03-14', '2024-03-15', '2024-03-18'])
  // 先正常跑一遍但人为把它"中断"在第 1 天后：直接 runWorld 跑完，然后改 state 模拟中断
  await runWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) })
  // 模拟"只完成了 1 天、状态 running"的中断态：删掉 day2/day3 的产物，回退 cursor
  rmSync(P.botDayDir(worldRoot, 'r1', '2024-03-15', 'bot1'), { recursive: true, force: true })
  rmSync(P.botDayDir(worldRoot, 'r1', '2024-03-18', 'bot1'), { recursive: true, force: true })
  // Stale pid simulates "previous orchestrator died" — resume must adopt the
  // run for THIS process so future orphan checks see a live owner, not a ghost.
  const s = readState(worldRoot, 'r1'); writeState(worldRoot, 'r1', { ...s, status: 'running', cursor: 1, current_date: '2024-03-14', pid: 1 })

  await resumeWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) })
  const st = readState(worldRoot, 'r1')
  assert.equal(st.status, 'done')
  assert.equal(st.cursor, 3)
  assert.equal(st.pid, process.pid)
  assert.ok(existsSync(P.replyFile(worldRoot, 'r1', '2024-03-15', 'bot1')))
  assert.ok(existsSync(P.replyFile(worldRoot, 'r1', '2024-03-18', 'bot1')))
  cleanup()
})

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

test('resumeWorld refuses when state status is a terminal (done / aborted)', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir(['bot1'], ['2024-03-14'])
  await runWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) }) // status=done
  await assert.rejects(() => resumeWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) }), /nothing to resume/i)
  const s = readState(worldRoot, 'r1'); writeState(worldRoot, 'r1', { ...s, status: 'aborted' })
  await assert.rejects(() => resumeWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) }), /nothing to resume/i)
  cleanup()
})

test('resumeWorld refuses when state.loop differs from config.loop', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir(['bot1'], ['2024-03-14'])
  await runWorld({ worldRoot, config, runId: 'r1', startBotServer: (b) => stubStart(b) }) // writes state with loop=research-loop
  // Force state.status back to running so the loop-mismatch check is the rejector (not the status check)
  const s = readState(worldRoot, 'r1')
  writeState(worldRoot, 'r1', { ...s, status: 'running' })
  // Now resume with loop=openclaw-pi
  const piConfig: WorldConfig = { ...config, loop: 'openclaw-pi', openclawRoot: '/tmp/oc', piServerEntry: '/tmp/oc/pi.ts' }
  await assert.rejects(() => resumeWorld({ worldRoot, config: piConfig, runId: 'r1', startBotServer: (b) => stubStart(b) }), /loop/)
  cleanup()
})
