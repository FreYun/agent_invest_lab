import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { runWorld } from '../src/run.ts'
import * as P from '../src/paths.ts'
import type { WorldConfig } from '../src/config.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const STUB_PI = join(HERE, 'helpers', 'stubPiBotServer.ts')

test('runWorld with loop=openclaw-pi: stub pi-server drives 1 trading day end-to-end', async () => {
  const root = mkdtempSync(join(tmpdir(), 'pi-smoke-'))
  const worldRoot = join(root, 'world')
  mkdirSync(worldRoot, { recursive: true })
  // calendar + 1 trading day
  writeFileSync(P.calendarFile(worldRoot), JSON.stringify({ trading_days: ['2024-01-02'] }))
  mkdirSync(P.dayDir(worldRoot, '2024-01-02'), { recursive: true })
  writeFileSync(P.quotesFile(worldRoot, '2024-01-02'), JSON.stringify({ summary: 'snapshot' }))
  writeFileSync(P.overviewFile(worldRoot, '2024-01-02'), 'overview')
  // bots/<bot> dir
  const botsRoot = join(root, 'bots')
  const ws = join(botsRoot, 'bot1'); mkdirSync(ws, { recursive: true })
  writeFileSync(join(ws, 'SOUL.md'), '# bot1\n')
  // skills root (empty is fine)
  const skillsRoot = join(root, 'skills'); mkdirSync(skillsRoot, { recursive: true })
  // openclaw.json (must exist for pi loop; content can be minimal but include mcp.mem0 placeholder so we can verify patch)
  const openclawJson = join(root, 'openclaw.json')
  writeFileSync(openclawJson, JSON.stringify({ mcp: { mem0: 'http://placeholder:0' } }) + '\n')
  // rl-config base (not used by pi loop, but field is still required)
  const cfgBase = join(root, 'base.json')
  writeFileSync(cfgBase, '{}\n')

  const cfg: WorldConfig = {
    researchLoop: '/unused',
    botsRoot,
    openclawJson,
    skillsRoot,
    bots: ['bot1'],
    replay: { from: '2024-01-02', to: '2024-01-02' },
    calendar: P.calendarFile(worldRoot),
    concurrency: 1,
    perBotTimeoutSeconds: 30,
    researchDayEvery: 0,
    researchDayTimeoutSeconds: 300,
    rlConfigBase: cfgBase,
    rlOpenclawDir: undefined,
    shadowInclude: ['SOUL.md'],
    loop: 'openclaw-pi',
    // pi spawn uses <openclawRoot>/node_modules/tsx/dist/loader.mjs; point at real openclaw
    // so tsx resolves. The stub doesn't actually need tsx (no .js imports) but the world-side
    // spawn argv unconditionally references the loader.
    openclawRoot: '/home/rooot/.openclaw/openclaw',
    piServerEntry: STUB_PI,
    simworldUpstreamUrl: 'http://127.0.0.1:1/mcp',
  }

  await runWorld({ worldRoot, config: cfg, runId: 'r1' })

  // 1) reply.json written and contains stub reply text
  const replyPath = P.replyFile(worldRoot, 'r1', '2024-01-02', 'bot1')
  assert.ok(existsSync(replyPath), `reply.json missing at ${replyPath}`)
  const reply = JSON.parse(readFileSync(replyPath, 'utf8'))
  assert.match(reply.reply, /stub pi reply/)

  // 2) rl-openclaw/openclaw.json had mcp.mem0 patched to a real localhost URL
  const patched = JSON.parse(readFileSync(join(P.rlOpenclawDir(worldRoot, 'r1'), 'openclaw.json'), 'utf8'))
  assert.match(String(patched.mcp?.mem0 ?? ''), /^http:\/\/127\.0\.0\.1:\d+/)

  rmSync(root, { recursive: true, force: true })
})
