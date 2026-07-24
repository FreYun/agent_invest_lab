import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { assembleBriefing } from '../src/intraday-briefing.ts'
import { runSqlite, sqlStr } from '../src/strategy-server/server.ts'

// 造一个临时 fund.db：res_reports + market_reports 两表，插入 2025 历史多份 + 2026 未来一份 + 主线/组合骨架，
// 用来验证 PIT 取数（as_of_date<=世界日 取最新）与「不注入 mainline/rotation」。
function makeDb(): { dir: string; db: string } {
  const dir = mkdtempSync(join(tmpdir(), 'briefing-db-'))
  const db = join(dir, 'fund.db')
  runSqlite(db,
    `CREATE TABLE res_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, report_type TEXT, scope TEXT, as_of_date TEXT, content_md TEXT);
     CREATE TABLE market_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, report_type TEXT, scope TEXT, as_of_date TEXT, content_md TEXT);`)
  const ins = (table: string, type: string, date: string, body: string) =>
    runSqlite(db, `INSERT INTO ${table} (report_type,scope,as_of_date,content_md) VALUES (${sqlStr(type)},'global',${sqlStr(date)},${sqlStr(body)});`)

  ins('res_reports', 'market_strategy', '2025-01-02', 'STRATEGY-2025-01-02')
  ins('res_reports', 'market_strategy', '2025-06-01', 'STRATEGY-2025-06-01')
  ins('res_reports', 'market_strategy', '2026-07-21', 'STRATEGY-FUTURE-2026') // live 才写的未来份，绝不能注入回测
  ins('res_reports', 'policy_analysis', '2025-01-02', 'POLICY-2025-01-02')
  ins('res_reports', 'intl_relations', '2025-01-02', 'INTL-2025-01-02')
  ins('res_reports', 'cross_market_linkage', '2025-01-02', 'CROSS-2025-01-02')
  ins('market_reports', 'market_context', '2025-01-02', 'CONTEXT-2025-01-02')
  ins('market_reports', 'macro_news', '2025-01-02', 'MACRO-2025-01-02')
  // 主线板块 + 组合骨架：即使在库也不该出现在单指数简报里
  ins('market_reports', 'market_mainline', '2025-01-02', 'MAINLINE-SHOULD-NOT-APPEAR')
  ins('market_reports', 'mainline_rotation', '2025-01-02', 'ROTATION-SHOULD-NOT-APPEAR')
  return { dir, db }
}

test('PIT：取 as_of_date<=世界日 的最新一份，绝不注入未来报告', () => {
  const { dir, db } = makeDb()
  try {
    const out = assembleBriefing({ fundDbPath: db, asOfDate: '2025-03-01' })
    assert.match(out, /STRATEGY-2025-01-02/)                              // <=世界日的最新
    assert.ok(!out.includes('STRATEGY-2025-06-01'), '不能取到晚于世界日的 6 月报告')
    assert.ok(!out.includes('STRATEGY-FUTURE-2026'), '绝不能注入 2026 未来报告')
    assert.match(out, /报告日 2025-01-02 · 距今 58 天/)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('世界日推进 → 取到更近的一份（2025-06-01）', () => {
  const { dir, db } = makeDb()
  try {
    const out = assembleBriefing({ fundDbPath: db, asOfDate: '2025-07-01' })
    assert.match(out, /STRATEGY-2025-06-01/)
    assert.ok(!out.includes('STRATEGY-2025-01-02'), '有更近的一份就不该取旧的')
    assert.ok(!out.includes('STRATEGY-FUTURE-2026'))
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('世界日早于全部报告 → 空串（不注入）', () => {
  const { dir, db } = makeDb()
  try {
    assert.equal(assembleBriefing({ fundDbPath: db, asOfDate: '2024-12-01' }), '')
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('注入 res 四研判室 + macro_news + market_context，排除主线/组合骨架', () => {
  const { dir, db } = makeDb()
  try {
    const out = assembleBriefing({ fundDbPath: db, asOfDate: '2025-03-01' })
    assert.match(out, /market_context/)
    assert.match(out, /macro_news/)
    assert.match(out, /市场策略研判/)
    assert.match(out, /政策分析/)
    assert.match(out, /国际关系/)
    assert.match(out, /跨市场联动/)
    assert.ok(!out.includes('MAINLINE-SHOULD-NOT-APPEAR'), '主线板块不该注入单指数简报')
    assert.ok(!out.includes('ROTATION-SHOULD-NOT-APPEAR'), '组合骨架不该注入单指数简报')
  } finally { rmSync(dir, { recursive: true, force: true }) }
})
