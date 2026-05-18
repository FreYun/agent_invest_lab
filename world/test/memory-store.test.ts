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

test('search excludes records starting with # MY_STRATEGY (server-side strategy filter)', () => {
  // 策略文档每天都会被 world 注入到 prompt（strategyBlock），让它再进 mem0_search 命中
  // 就成了无意义的"自己引用自己"，把真正昨天的判断挤出 hit list。在 store 层直接过滤掉。
  const { file, cleanup } = tmpStore()
  const s = new MemoryStore(file)
  // bot7：一份策略（含半导体关键词）+ 一条昨天的实际判断（半导体相关）
  s.add({ text: '# MY_STRATEGY\n核心信念：看好半导体复苏，重仓封测设备', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-15' })
  s.add({ text: '今天买入半导体ETF 30%仓位，理由：库存见底信号已现', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-16' })

  // 搜 "半导体" 应只命中实际判断，不命中策略副本
  const hits = s.search('半导体', { agent_id: 'bot7', limit: 5 })
  assert.equal(hits.length, 1)
  assert.match(hits[0].memory, /今天买入半导体/)
  assert.ok(!hits[0].memory.startsWith('# MY_STRATEGY'))

  // findLatestByPrefix 不受影响：策略抽取链路仍能拿到策略
  const strat = s.findLatestByPrefix('bot7', '# MY_STRATEGY')
  assert.ok(strat)
  assert.match(strat!.text, /核心信念/)
  cleanup()
})

test('search filter is startsWith, not contains: notes mentioning the prefix mid-text are kept', () => {
  const { file, cleanup } = tmpStore()
  const s = new MemoryStore(file)
  s.add({ text: '复盘：参考 # MY_STRATEGY 里写的止盈线，今天该减仓', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-16' })
  const hits = s.search('复盘 止盈', { agent_id: 'bot7', limit: 5 })
  assert.equal(hits.length, 1)
  assert.match(hits[0].memory, /复盘/)
  cleanup()
})

test('findLatestByPrefix returns the latest matching record for an agent; null when nothing matches', () => {
  const { file, cleanup } = tmpStore()
  const s = new MemoryStore(file)
  // bot7 写了两版策略（不同日期）+ 一个普通笔记；bot1 也写了一份策略
  s.add({ text: '# MY_STRATEGY\nv1: 第一版策略，激进风格', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-15' })
  s.add({ text: '今天看好半导体', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-15' })
  s.add({ text: '# MY_STRATEGY\nv2: 第二版策略，更稳健', agent_id: 'bot7', user_id: 'bot7', created_at: '2024-03-16' })
  s.add({ text: '# MY_STRATEGY\nbot1 的策略', agent_id: 'bot1', user_id: 'bot1', created_at: '2024-03-15' })

  // bot7 应该取到 v2（最新 created_at）
  const bot7Strategy = s.findLatestByPrefix('bot7', '# MY_STRATEGY')
  assert.ok(bot7Strategy)
  assert.match(bot7Strategy!.text, /v2: 第二版策略/)
  assert.equal(bot7Strategy!.agent_id, 'bot7')

  // bot1 独立，取自己的
  const bot1Strategy = s.findLatestByPrefix('bot1', '# MY_STRATEGY')
  assert.ok(bot1Strategy)
  assert.match(bot1Strategy!.text, /bot1 的策略/)

  // 不存在前缀的 agent → null
  assert.equal(s.findLatestByPrefix('bot7', '# NONEXISTENT'), null)

  // agent 不存在 → null（不会取到别的 agent 的策略——cross-agent isolation）
  assert.equal(s.findLatestByPrefix('bot999', '# MY_STRATEGY'), null)

  // 前缀必须是 startsWith，不是 contains
  s.add({ text: '中间嵌着 # MY_STRATEGY 的笔记', agent_id: 'bot8', user_id: 'bot8', created_at: '2024-03-15' })
  assert.equal(s.findLatestByPrefix('bot8', '# MY_STRATEGY'), null)
  cleanup()
})
