import { mkdirSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildBuyableCodesByBot, buildBuyableWritePayload, loadShareClassMap } from '../src/run.ts'
import { shareClassMapFile } from '../src/paths.ts'
import type { WorldConfig } from '../src/config.ts'

// 镜像真实布局：worldRoot 是 <repo>/world/runtime，shareClassMapFile 走 '..','..','data'。
function tmpWorldRoot(mapJson?: string): { worldRoot: string; cleanup: () => void } {
  const repo = mkdtempSync(join(tmpdir(), 'share-class-'))
  const worldRoot = join(repo, 'world', 'runtime')
  mkdirSync(worldRoot, { recursive: true })
  if (mapJson !== undefined) {
    mkdirSync(join(repo, 'data'), { recursive: true })
    writeFileSync(shareClassMapFile(worldRoot), mapJson)
  }
  return { worldRoot, cleanup: () => rmSync(repo, { recursive: true, force: true }) }
}

// buildBuyableCodesByBot 只读 bots / botAssignments / buyableFundCodes 三个字段。
function cfg(partial: Partial<WorldConfig>): WorldConfig {
  return partial as WorldConfig
}

const MAP = JSON.stringify({ map: { '000051': '005658', '167301': '018099' } })

test('loadShareClassMap returns {} when the map file is missing or malformed', () => {
  const missing = tmpWorldRoot()
  assert.deepEqual(loadShareClassMap(missing.worldRoot), {})
  missing.cleanup()

  const broken = tmpWorldRoot('{ not json')
  assert.deepEqual(loadShareClassMap(broken.worldRoot), {})
  broken.cleanup()

  const noMapKey = tmpWorldRoot(JSON.stringify({ names: {} }))
  assert.deepEqual(loadShareClassMap(noMapKey.worldRoot), {})
  noMapKey.cleanup()
})

test('loadShareClassMap drops non-string targets', () => {
  const t = tmpWorldRoot(JSON.stringify({ map: { '000051': '005658', '000001': null, '000002': 42, '000003': '' } }))
  assert.deepEqual(loadShareClassMap(t.worldRoot), { '000051': '005658' })
  t.cleanup()
})

test('A is dropped when its C sibling is already in the pool', () => {
  const t = tmpWorldRoot(MAP)
  const out = buildBuyableCodesByBot(cfg({
    bots: ['bot105g'],
    botAssignments: { bot105g: { strategyId: 's', buyableFundCodes: ['000051', '005658', '011609'] } },
  }), null, t.worldRoot)
  assert.deepEqual(out.bot105g, ['005658', '011609'])
  t.cleanup()
})

test('A is substituted 1:1 when its C sibling is not in the pool', () => {
  const t = tmpWorldRoot(MAP)
  const out = buildBuyableCodesByBot(cfg({
    bots: ['bot19'],
    botAssignments: { bot19: { strategyId: 'insurance', buyableFundCodes: ['167301'] } },
  }), null, t.worldRoot)
  // 单指数 bot 的池只有一只：A 被顶替成 C，池子大小不变。
  assert.deepEqual(out.bot19, ['018099'])
  t.cleanup()
})

test('unmapped codes pass through untouched, and a missing map is a no-op', () => {
  const withMap = tmpWorldRoot(MAP)
  const pool = ['168203', '000826', '011609']
  assert.deepEqual(
    buildBuyableCodesByBot(cfg({ bots: ['bot1'], buyableFundCodes: pool }), null, withMap.worldRoot).bot1,
    ['000826', '011609', '168203'],
  )
  withMap.cleanup()

  // 映射表缺失 → 原样返回，零回归。
  const noMap = tmpWorldRoot()
  assert.deepEqual(
    buildBuyableCodesByBot(cfg({ bots: ['bot1'], buyableFundCodes: ['000051', '011609'] }), null, noMap.worldRoot).bot1,
    ['000051', '011609'],
  )
  noMap.cleanup()
})

// 回归：写盘 payload 是 MCP 买入闸门真正读的那一份。没有 botAssignments 的 config
// （world-all-bots.yaml 这类，覆盖 bot1~20 + bot101~103）走的是全局扁平池分支，
// 它绕开 buildBuyableCodesByBot——曾经因此把未替换的 A 写进闸门，同时 shadow
// METHODOLOGY 宣传 C，两边打架：bot 被告知买 C 却下单必被拒，旧 A 反而照买。
test('the buy-gate payload is share-class swapped on the global-pool branch too', () => {
  const swap = { '000051': '005658', '167301': '018099' }
  const byBot = { bot1: ['005658'] }

  const global = buildBuyableWritePayload(
    cfg({ bots: ['bot1'], buyableFundCodes: ['000051', '167301', '011609'] }), byBot, swap,
  )
  assert.deepEqual(global, ['005658', '011609', '018099'])

  // 有 botAssignments → 用 per-bot 那份（已在 buildBuyableCodesByBot 里替换过）。
  const perBot = buildBuyableWritePayload(
    cfg({ bots: ['bot1'], botAssignments: { bot1: { strategyId: 's', buyableFundCodes: ['000051'] } } }), byBot, swap,
  )
  assert.deepEqual(perBot, byBot)

  // 映射表为空 → 全局池原样写盘，零回归。
  assert.deepEqual(
    buildBuyableWritePayload(cfg({ bots: ['bot1'], buyableFundCodes: ['000051', '011609'] }), byBot, {}),
    ['000051', '011609'],
  )
})
