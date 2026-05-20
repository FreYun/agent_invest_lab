import { mkdtempSync, rmSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { MemoryStore } from '../src/memory-server/store.ts'

function tmpStore(): { file: string; cleanup: () => void } {
  const dir = mkdtempSync(join(tmpdir(), 'mstore-'))
  return { file: join(dir, 'sub', 'store.jsonl'), cleanup: () => rmSync(dir, { recursive: true, force: true }) }
}

test('add stores verbatim text with given created_at and agent_id, persisted as JSONL', () => {
  const { file, cleanup } = tmpStore()
  const s = new MemoryStore(file)
  const rec = s.add({ text: '半导体周期见底，看好封测', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-15' })
  assert.equal(rec.text, '半导体周期见底，看好封测')
  assert.equal(rec.agent_id, 'bot7')
  assert.equal(rec.created_at, '2024-03-15')
  assert.ok(rec.id)
  const lines = readFileSync(file, 'utf8').trim().split('\n')
  assert.equal(lines.length, 1)
  assert.equal(JSON.parse(lines[0]).text, '半导体周期见底，看好封测')
  cleanup()
})

test('search ranks by keyword overlap count, filters by agent_id, respects limit', () => {
  const { file, cleanup } = tmpStore()
  const s = new MemoryStore(file)
  s.add({ text: '半导体 看好 封测', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-15' })
  s.add({ text: '半导体 库存 偏高', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-16' })
  s.add({ text: '光伏 周期 见底', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-17' })
  s.add({ text: '半导体 看好 封测 设备', agent_id: 'bot1', user_id: 'bot1', created_at: '2024-03-16' })

  const hits = s.search('半导体 封测', { agent_id: 'bot7', limit: 5 })
  assert.equal(hits.length, 2) // 第三条无重叠 → 排除
  assert.equal(hits[0].memory, '半导体 看好 封测') // 重叠 半/导/体/封/测 = 5
  assert.ok(hits[0].score > hits[1].score)
  assert.equal(hits.every(h => h.agent_id === 'bot7'), true)

  const limited = s.search('半导体', { agent_id: 'bot7', limit: 1 })
  assert.equal(limited.length, 1)

  const all = s.search('半导体 封测', { limit: 10 }) // 不过滤 agent
  assert.equal(all.length, 3)
  cleanup()
})

test('search with no overlapping tokens returns []', () => {
  const { file, cleanup } = tmpStore()
  const s = new MemoryStore(file)
  s.add({ text: '光伏 周期', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-15' })
  assert.deepEqual(s.search('白酒 估值', { agent_id: 'bot7', limit: 5 }), [])
  cleanup()
})

test('new MemoryStore loads existing file', () => {
  const { file, cleanup } = tmpStore()
  new MemoryStore(file).add({ text: '半导体', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-15' })
  const s2 = new MemoryStore(file)
  assert.equal(s2.search('半导体', { agent_id: 'bot7', limit: 5 }).length, 1)
  cleanup()
})

test('search start_date / end_date filter records inclusively by created_at', () => {
  const { file, cleanup } = tmpStore()
  const s = new MemoryStore(file)
  s.add({ text: '半导体 看好', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-10' })
  s.add({ text: '半导体 看好', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-15' })
  s.add({ text: '半导体 看好', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-20' })

  // both bounds (inclusive)
  let hits = s.search('半导体', { agent_id: 'bot7', limit: 10, start_date: '2024-03-15', end_date: '2024-03-20' })
  assert.deepEqual(hits.map(h => h.created_at).sort(), ['2024-03-15', '2024-03-20'])

  // start only
  hits = s.search('半导体', { agent_id: 'bot7', limit: 10, start_date: '2024-03-15' })
  assert.deepEqual(hits.map(h => h.created_at).sort(), ['2024-03-15', '2024-03-20'])

  // end only
  hits = s.search('半导体', { agent_id: 'bot7', limit: 10, end_date: '2024-03-15' })
  assert.deepEqual(hits.map(h => h.created_at).sort(), ['2024-03-10', '2024-03-15'])
  cleanup()
})

test('search applies recency decay when now is provided; tau=0 disables decay', () => {
  const { file, cleanup } = tmpStore()
  const s = new MemoryStore(file)
  // Old record has 2x raw TF (科创 50 appears twice → tokens 科,创,50 each ×2 = 6),
  // recent record has lower raw TF (3) but is the same day as `now`.
  s.add({ text: '科创 50 科创 50', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-01-01' })
  s.add({ text: '科创 50',           agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-30' })

  // Without decay: old record wins by raw TF.
  let hits = s.search('科创50', { agent_id: 'bot7', limit: 5, recency_tau_days: 0, now: '2024-03-30' })
  assert.equal(hits[0].created_at, '2024-01-01')

  // With τ=7 days and now=2024-03-30: old record (Δ≈89d) decays to ~6 * e^(-89/7) ≈ 0.002;
  // recent record (Δ=0) keeps full 3. Recent wins.
  hits = s.search('科创50', { agent_id: 'bot7', limit: 5, recency_tau_days: 7, now: '2024-03-30' })
  assert.equal(hits[0].created_at, '2024-03-30')

  // Default τ when `now` is provided is gentle (30d): old record (Δ≈89d → e^(-89/30) ≈ 0.052,
  // score ≈ 6 * 0.052 = 0.31) still loses to recent (3).
  hits = s.search('科创50', { agent_id: 'bot7', limit: 5, now: '2024-03-30' })
  assert.equal(hits[0].created_at, '2024-03-30')

  // When no `now` is passed, decay is disabled regardless of tau (PIT-safe default).
  hits = s.search('科创50', { agent_id: 'bot7', limit: 5 })
  assert.equal(hits[0].created_at, '2024-01-01')
  cleanup()
})

