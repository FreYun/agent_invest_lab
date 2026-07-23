import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { assembleBriefing, loadRouting, isStrategyRouted } from '../src/intraday-briefing.ts'

// 造一个假 res 根，覆盖：错位命名(市场环境/隔夜美股)、主题分片、_raw/MEMORY/子目录干扰、死室(旧日期)。
function makeResRoot(): string {
  const root = mkdtempSync(join(tmpdir(), 'res-'))
  const room = (n: string) => { const d = join(root, `workspace-res${n}`, 'memory'); mkdirSync(d, { recursive: true }); return d }
  const w = (dir: string, name: string, body: string) => writeFileSync(join(dir, name), body)

  const r1 = room('1')
  w(r1, '2026-07-20-市场环境.md', 'macro-old')
  w(r1, '2026-07-21-市场环境.md', 'macro-latest')      // 应取这份
  w(r1, '2026-07-21-政策专题.md', 'shard-ignore')       // 主题分片，排除
  w(r1, 'MEMORY.md', 'mem-ignore')

  const r14 = room('14')
  w(r14, '2026-07-21.md', 'index-old')
  w(r14, '2026-07-22.md', 'index-latest')              // 应取这份
  w(r14, '2026-07-22-指数专题.md', 'shard-ignore')
  w(r14, '_raw_dump.json', '{}')
  mkdirSync(join(r14, 'sub'), { recursive: true })

  const r7 = room('7')
  w(r7, '2026-07-22.md', 'energy-latest')

  const r5 = room('5')
  w(r5, '2026-06-18-隔夜美股A股传导.md', 'us-stale')     // 33 天前，应标已过期

  return root
}

const ROUTING = join(import.meta.dirname, '..', 'config', 'res-routing.json')

test('板块 run：res1+res14+对口板块，取规范主报最新一份', () => {
  const root = makeResRoot()
  try {
    const out = assembleBriefing({ strategyId: 'battery', resRoot: root, routingPath: ROUTING, asOfDate: '2026-07-22' })
    assert.match(out, /res1 · 宏观·市场环境 · 报告日 2026-07-21 · 距今 1 天/)
    assert.match(out, /macro-latest/)
    assert.ok(!out.includes('macro-old'), '不能取到旧日期主报')
    assert.ok(!out.includes('shard-ignore'), '不能取到主题分片')
    assert.match(out, /res14 · 指数技术面 · 报告日 2026-07-22 · 距今 0 天/)
    assert.match(out, /index-latest/)
    assert.match(out, /res7 · 能源材料 · 报告日 2026-07-22 · 距今 0 天/)
    assert.match(out, /energy-latest/)
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('海外 run：res5 停更 → 段头标已过期，仍原样附上', () => {
  const root = makeResRoot()
  try {
    const out = assembleBriefing({ strategyId: 'sp500', resRoot: root, routingPath: ROUTING, asOfDate: '2026-07-22' })
    assert.match(out, /res5 · 中美·隔夜美股A股传导 · 报告日 2026-06-18 · 距今 34 天 · 已过期/)
    assert.match(out, /us-stale/)
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('宽基 run：只 res1+res14，不挂板块室', () => {
  const root = makeResRoot()
  try {
    const out = assembleBriefing({ strategyId: 'hs300', resRoot: root, routingPath: ROUTING, asOfDate: '2026-07-22' })
    assert.match(out, /res1 ·/)
    assert.match(out, /res14 ·/)
    assert.ok(!out.includes('res7 ·') && !out.includes('res5 ·'), '宽基不该挂板块室')
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('对口板块室缺文件 → 降级注明，不抛错', () => {
  const root = makeResRoot()
  try {
    // res8 医药室在假根里根本没建 → 应注明「无可用主报」
    const out = assembleBriefing({ strategyId: 'medical', resRoot: root, routingPath: ROUTING, asOfDate: '2026-07-22' })
    assert.match(out, /res8 · 医药 · 无可用主报，已跳过/)
    assert.match(out, /res1 ·/)   // 常驻室仍在
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('路由覆盖：所有在用 strategy_id 都被路由表覆盖（板块或宽基）', () => {
  const routing = loadRouting(ROUTING)
  // 与 live config 实测在用集合一致（见 grep strategy_id）。
  const inUse = [
    'zz500','shuangchuang','gold_stocks','bse50','robot','medical','hs300','green_power','defense','chemical',
    'zz1000','telecom_tech','sse50','sp500','solar','semiconductor_industry','semiconductor','securities',
    'rare_metals','rare_earth','power_grid','power','nev','nasdaq100','liquor','internet50','insurance',
    'innodrug','hstech','hsi','hscei','gold_au9999','dividend','consumer800','cloud','chip','battery','bank','baijiu','ai',
  ]
  const unmapped = inUse.filter(s => !isStrategyRouted(s, routing))
  assert.deepEqual(unmapped, [], `未被路由覆盖的 strategy_id: ${unmapped.join(', ')}`)
})
