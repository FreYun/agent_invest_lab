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
