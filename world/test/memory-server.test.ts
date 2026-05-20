import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { MemoryStore } from '../src/memory-server/store.ts'
import { createMemoryServer } from '../src/memory-server/server.ts'

async function withServer(getDate: () => string, run: (url: string, store: MemoryStore) => Promise<void>) {
  const dir = mkdtempSync(join(tmpdir(), 'msrv-'))
  const store = new MemoryStore(join(dir, 'store.jsonl'))
  const h = await createMemoryServer({ store, getCurrentDate: getDate })
  try { await run(h.url, store) } finally { await h.close(); rmSync(dir, { recursive: true, force: true }) }
}

test('POST /memories stores verbatim with current date; POST /search returns mem0-shaped results', async () => {
  let day = '2024-03-15'
  await withServer(() => day, async (url) => {
    let r = await fetch(`${url}/memories`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'system', content: '半导体看好封测' }], user_id: 'bot7', agent_id: 'bot7', infer: false }),
    })
    assert.equal(r.status, 200)
    const added = await r.json()
    assert.ok(Array.isArray(added.results) && added.results[0].id)

    day = '2024-03-16'
    r = await fetch(`${url}/memories`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'system', content: '半导体库存偏高' }], user_id: 'bot7', agent_id: 'bot7' }),
    })
    assert.equal(r.status, 200)

    r = await fetch(`${url}/search`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ query: '半导体 封测', user_id: 'bot7', agent_id: 'bot7', limit: 5 }),
    })
    assert.equal(r.status, 200)
    const found = await r.json()
    assert.equal(found.results.length, 2)
    assert.equal(found.results[0].memory, '半导体看好封测')
    assert.equal(found.results[0].agent_id, 'bot7')
    assert.equal(found.results[0].created_at, '2024-03-15')
    assert.ok(typeof found.results[0].score === 'number')
  })
})

test('POST /memories without any identifier → 400', async () => {
  await withServer(() => '2024-03-15', async (url) => {
    const r = await fetch(`${url}/memories`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'system', content: 'x' }] }),
    })
    assert.equal(r.status, 400)
  })
})

test('GET /health → ok; unknown route → 404', async () => {
  await withServer(() => '2024-03-15', async (url) => {
    assert.equal((await fetch(`${url}/health`)).status, 200)
    assert.equal((await fetch(`${url}/nope`)).status, 404)
  })
})

test('POST /search forwards start_date / end_date / recency_tau_days to store', async () => {
  let day = '2024-03-01'
  await withServer(() => day, async (url) => {
    // Seed 3 records on different days, all matching the query.
    for (const [d, text] of [['2024-03-01', '科创 50 科创 50'], ['2024-03-15', '科创 50'], ['2024-03-30', '科创 50']]) {
      day = d as string
      const r = await fetch(`${url}/memories`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ messages: [{ role: 'system', content: text }], user_id: 'bot7', agent_id: 'bot7' }),
      })
      assert.equal(r.status, 200)
    }

    // Date filter: only the middle record survives.
    day = '2024-03-30'
    let r = await fetch(`${url}/search`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ query: '科创50', agent_id: 'bot7', limit: 10, start_date: '2024-03-10', end_date: '2024-03-20', recency_tau_days: 0 }),
    })
    assert.equal(r.status, 200)
    let found = await r.json()
    assert.equal(found.results.length, 1)
    assert.equal(found.results[0].created_at, '2024-03-15')

    // Default decay uses getCurrentDate() = 2024-03-30 → recent record wins over the
    // higher-TF March-1 record. (Without this fix, March-1 would have won on raw TF.)
    r = await fetch(`${url}/search`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ query: '科创50', agent_id: 'bot7', limit: 5 }),
    })
    assert.equal(r.status, 200)
    found = await r.json()
    assert.equal(found.results[0].created_at, '2024-03-30')
  })
})
