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

test('POST /search excludes records starting with # MY_STRATEGY', async () => {
  // cross-day 记忆生效的关键：策略副本不该挤占 mem0_search 的命中位。
  let day = '2024-03-15'
  await withServer(() => day, async (url) => {
    // Day 1：bot 写策略
    let r = await fetch(`${url}/memories`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'system', content: '# MY_STRATEGY\n看好半导体复苏，重仓封测' }], user_id: 'bot7', agent_id: 'bot7' }),
    })
    assert.equal(r.status, 200)
    day = '2024-03-16'
    // Day 2：bot 写一条实际判断
    r = await fetch(`${url}/memories`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'system', content: '今天买入半导体ETF 30%仓位' }], user_id: 'bot7', agent_id: 'bot7' }),
    })
    assert.equal(r.status, 200)
    // Day 3：search 应只返回 Day 2 的实际判断
    r = await fetch(`${url}/search`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ query: '半导体', user_id: 'bot7', agent_id: 'bot7', limit: 5 }),
    })
    assert.equal(r.status, 200)
    const found = await r.json()
    assert.equal(found.results.length, 1)
    assert.match(found.results[0].memory, /今天买入半导体/)
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
