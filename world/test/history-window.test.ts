import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { extractDayDigestFromJsonl, renderDayDigest } from '../src/history-window/extract.ts'
import { buildHistoryWindow, listBotSessionsBefore } from '../src/history-window/window.ts'
import { resolveLlmEndpointFromRlConfig } from '../src/history-window/compact.ts'

// jsonl 帮手：构造一个最小可解析的 session jsonl
function writeJsonl(path: string, rows: object[]): void {
  writeFileSync(path, rows.map(r => JSON.stringify(r)).join('\n') + '\n')
}

function msg(role: 'user' | 'assistant', blocks: object[]): object {
  return { type: 'message', message: { role, content: blocks } }
}
function tu(name: string, input: Record<string, unknown> = {}): object {
  return { type: 'tool_use', id: 'tu_x', name, input }
}
function tr(content: string): object {
  return { type: 'tool_result', tool_use_id: 'tu_x', content }
}
function txt(t: string): object {
  return { type: 'text', text: t }
}

test('extractDayDigestFromJsonl: 收集 tool_use 名+关键 args，收集 last assistant text，完全跳过 tool_result', () => {
  const dir = mkdtempSync(join(tmpdir(), 'extract-'))
  const path = join(dir, 'a.jsonl')
  writeJsonl(path, [
    { type: 'session', cwd: '/x', id: 'sid', version: 3 },
    { type: 'custom', subtype: 'research-loop-init', data: { topic: 'huge daily prompt..' } },
    msg('user', [tr('巨长 dailyContext block 不应出现在 digest 里')]),
    msg('assistant', [
      tu('mem0_search', { query: '红利' }),
      tu('mcp__simworld_data__market_index_val', { index_code: '000922' }),
    ]),
    msg('assistant', [
      txt('中间思考过程'),
      tu('mcp__fund_portfolio_mcp__portfolio_place_buy_order', { fund_code: '090010', amount: 700000 }),
    ]),
    msg('assistant', [
      txt('最终决策：估值低位 + 市场温度偏冷，建底仓 70% on 中证红利。'),
      tu('mem0_add', { topic: 'today reflection' }),
    ]),
    { type: 'custom', subtype: 'research-loop-meta' },
  ])
  const d = extractDayDigestFromJsonl(path, '2026-01-05')
  // tool_use 全部收集，prefix 已 strip
  const names = d.toolCalls.map(t => t.name)
  assert.deepEqual(names, ['mem0_search', 'market_index_val', 'portfolio_place_buy_order', 'mem0_add'])
  // 关键 args 在
  assert.match(d.toolCalls[0].args, /query="红利"/)
  assert.match(d.toolCalls[1].args, /index_code="000922"/)
  assert.match(d.toolCalls[2].args, /fund_code="090010"/)
  assert.match(d.toolCalls[2].args, /amount=700000/)
  // 最后一条 text 被保留
  assert.match(d.finalText, /最终决策/)
  // tool_result 内容绝对不出现
  const rendered = renderDayDigest(d, 5)
  assert.doesNotMatch(rendered, /巨长 dailyContext/)
  assert.match(rendered, /## 2026-01-05 \(Day 5\)/)
  rmSync(dir, { recursive: true, force: true })
})

test('extractDayDigestFromJsonl: 无 tool_use / 无 text 的退化路径不崩', () => {
  const dir = mkdtempSync(join(tmpdir(), 'extract-'))
  const path = join(dir, 'empty.jsonl')
  writeJsonl(path, [
    { type: 'session', cwd: '/x', id: 'sid', version: 3 },
  ])
  const d = extractDayDigestFromJsonl(path, '2026-01-06')
  assert.deepEqual(d.toolCalls, [])
  assert.equal(d.finalText, '')
  const r = renderDayDigest(d, 1)
  assert.match(r, /## 2026-01-06/)
  assert.match(r, /（无工具调用）/)
  rmSync(dir, { recursive: true, force: true })
})

function fakeRunSetup(): { rlDir: string; botId: string; cleanup: () => void } {
  const dir = mkdtempSync(join(tmpdir(), 'hw-'))
  const botId = 'bot7'
  const sessDir = join(dir, 'agents', botId, 'sessions')
  mkdirSync(sessDir, { recursive: true })
  return {
    rlDir: dir, botId,
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  }
}

function addDay(rlDir: string, botId: string, runId: string, date: string, rows: object[]): void {
  const sessDir = join(rlDir, 'agents', botId, 'sessions')
  const sid = `sid-${date}`
  const jsonlPath = join(sessDir, `${sid}.jsonl`)
  writeJsonl(jsonlPath, [
    { type: 'session', cwd: '/x', id: sid, version: 3 },
    ...rows,
  ])
  const idxPath = join(sessDir, 'sessions.json')
  let idx: Record<string, { sessionFile: string; sessionId: string; updatedAt: number }> = {}
  if (existsSync(idxPath)) {
    idx = JSON.parse(readFileSync(idxPath, 'utf8'))
  }
  idx[`agent:${botId}:trading-${runId}-${date}`] = { sessionFile: jsonlPath, sessionId: sid, updatedAt: Date.now() }
  writeFileSync(idxPath, JSON.stringify(idx, null, 2))
}

test('listBotSessionsBefore: 严格 <beforeDate，按日期升序', () => {
  const { rlDir, botId, cleanup } = fakeRunSetup()
  addDay(rlDir, botId, 'r1', '2026-01-05', [msg('assistant', [tu('mem0_search')])])
  addDay(rlDir, botId, 'r1', '2026-01-07', [msg('assistant', [tu('mem0_add')])])
  addDay(rlDir, botId, 'r1', '2026-01-06', [msg('assistant', [tu('market_temperature')])])
  const got = listBotSessionsBefore(rlDir, botId, '2026-01-07')
  assert.deepEqual(got.map(g => g.date), ['2026-01-05', '2026-01-06'])
  const all = listBotSessionsBefore(rlDir, botId, '2026-02-01')
  assert.deepEqual(all.map(g => g.date), ['2026-01-05', '2026-01-06', '2026-01-07'])
  cleanup()
})

test('buildHistoryWindow: 合并源 run 与 live session，同日以 live 为准', async () => {
  const source = fakeRunSetup()
  const live = fakeRunSetup()
  addDay(source.rlDir, source.botId, 'source', '2026-01-05', [msg('assistant', [txt('source-only')])])
  addDay(source.rlDir, source.botId, 'source', '2026-01-06', [msg('assistant', [txt('source-overridden')])])
  addDay(live.rlDir, live.botId, 'live', '2026-01-06', [msg('assistant', [txt('live-wins')])])
  addDay(live.rlDir, live.botId, 'live', '2026-01-07', [msg('assistant', [txt('live-only')])])
  const r = await buildHistoryWindow({
    rlOpenclawDir: live.rlDir,
    priorRlOpenclawDirs: [source.rlDir],
    botId: live.botId,
    beforeDate: '2026-01-08',
    openclawJsonPath: '/nonexistent',
    skipLlmCompact: true,
  })
  assert.equal(r.dayCount, 3)
  assert.match(r.markdown, /source-only/)
  assert.match(r.markdown, /live-wins/)
  assert.match(r.markdown, /live-only/)
  assert.doesNotMatch(r.markdown, /source-overridden/)
  source.cleanup()
  live.cleanup()
})

test('resolveLlmEndpointFromRlConfig: 取 research-loop.yaml 的 model.primary', () => {
  const dir = mkdtempSync(join(tmpdir(), 'rlcfg-'))
  const p = join(dir, 'research-loop.yaml')
  // writeResearchLoopYaml 落的是 JSON.stringify（合法 YAML 子集）
  writeFileSync(p, JSON.stringify({
    model: { primary: { provider: 'openai_compatible', base_url: 'https://dd-ai-api.eastmoney.com/v1', model: 'qwen3.6-plus', api_key: 'sk-live-xyz' } },
  }))
  const ep = resolveLlmEndpointFromRlConfig(p)
  assert.deepEqual(ep, { baseUrl: 'https://dd-ai-api.eastmoney.com/v1', apiKey: 'sk-live-xyz', model: 'qwen3.6-plus' })
  // llm.compact_model 只覆盖 history-window 压缩模型，不改变主 chat 模型配置。
  writeFileSync(p, JSON.stringify({
    model: { primary: { provider: 'openai_compatible', base_url: 'https://dd-ai-api.eastmoney.com/v1', model: 'glm-5.2', api_key: 'sk-live-xyz' } },
    llm: { compact_model: 'qwen3.6-plus' },
  }))
  assert.deepEqual(resolveLlmEndpointFromRlConfig(p), { baseUrl: 'https://dd-ai-api.eastmoney.com/v1', apiKey: 'sk-live-xyz', model: 'qwen3.6-plus' })
  // 字段不全 → 抛错（让调用方回退到 openclaw.json）
  writeFileSync(p, JSON.stringify({ model: { primary: { base_url: 'x', model: 'm' } } }))
  assert.throws(() => resolveLlmEndpointFromRlConfig(p), /incomplete model.primary/)
  rmSync(dir, { recursive: true, force: true })
})

test('buildHistoryWindow: Day 1（无 prior session）→ 空 markdown', async () => {
  const { rlDir, botId, cleanup } = fakeRunSetup()
  const r = await buildHistoryWindow({
    rlOpenclawDir: rlDir, botId, beforeDate: '2026-01-05',
    openclawJsonPath: '/nonexistent', skipLlmCompact: true,
  })
  assert.equal(r.markdown, '')
  assert.equal(r.dayCount, 0)
  cleanup()
})

test('buildHistoryWindow: 总长 ≤ budget → 全部原样保留，无 compact', async () => {
  const { rlDir, botId, cleanup } = fakeRunSetup()
  for (let i = 5; i <= 9; i++) {
    addDay(rlDir, botId, 'r1', `2026-01-0${i}`, [
      msg('assistant', [tu('mem0_search', { query: 'q' }), tu('market_temperature'), txt(`第 ${i} 天决策摘要`)]),
    ])
  }
  const r = await buildHistoryWindow({
    rlOpenclawDir: rlDir, botId, beforeDate: '2026-01-10',
    openclawJsonPath: '/nonexistent', skipLlmCompact: true,
  })
  assert.equal(r.dayCount, 5)
  assert.equal(r.compactedDays, 0)
  // 5 天全部 recent
  assert.match(r.markdown, /最近 5 个交易日/)
  for (let i = 5; i <= 9; i++) assert.match(r.markdown, new RegExp(`2026-01-0${i}`))
  cleanup()
})

test('buildHistoryWindow: 超 budget → 触发分段 + compact（skip LLM, fallback 留存）', async () => {
  const { rlDir, botId, cleanup } = fakeRunSetup()
  // 制造 30 天，每天一大段 text，让总长远超 budget
  const bigText = 'x'.repeat(2000)
  for (let i = 0; i < 30; i++) {
    const day = String(i + 1).padStart(2, '0')
    addDay(rlDir, botId, 'r1', `2026-01-${day}`, [
      msg('assistant', [tu('mem0_search'), txt(bigText)]),
    ])
  }
  // budgetChars 调小到 4000：每个 day digest 约 430 字（finalText 被 400 截断 + 头/工具行）。
  // recent ≈ 30%*4000/430 ≈ 2-3 天，older ≈ 27 天 × 430 ≈ 11k > compactBudget 2400 → 触发 compact。
  const r = await buildHistoryWindow({
    rlOpenclawDir: rlDir, botId, beforeDate: '2026-02-01',
    openclawJsonPath: '/nonexistent',
    budgetChars: 4000,
    skipLlmCompact: true,
  })
  assert.equal(r.dayCount, 30)
  // 至少有一些天数被 compacted（超 budget）
  assert.ok(r.compactedDays > 0, 'expected some days to be compacted when over budget')
  assert.ok(r.recentDays > 0 && r.recentDays < 30, 'expected some recent days kept raw')
  // compact state 应被写入
  const statePath = join(rlDir, 'history-compact', botId, 'state.json')
  assert.ok(existsSync(statePath), 'expected compact state.json to be written')
  const state = JSON.parse(readFileSync(statePath, 'utf8')) as { compactText: string; compactedUpToDate: string }
  assert.ok(state.compactText.length > 0)
  assert.match(state.compactedUpToDate, /^2026-01-\d{2}$/)
  // 第二次同 beforeDate 调用：state 完全覆盖 older 段 → 不应再 compact（增量分支 candidate == compactText）
  const r2 = await buildHistoryWindow({
    rlOpenclawDir: rlDir, botId, beforeDate: '2026-02-01',
    openclawJsonPath: '/nonexistent',
    budgetChars: 4000,
    skipLlmCompact: true,
  })
  assert.equal(r2.compactedDays, r.compactedDays)
  assert.equal(r2.dayCount, r.dayCount)
  cleanup()
})

test('buildHistoryWindow: 预置 state.json + 次日新增 older → 完全不走 LLM（增量分支命中）', async () => {
  const { rlDir, botId, cleanup } = fakeRunSetup()
  // 30 天 older + 5 天 recent = 35 天，每天 digest 约 430 字
  const bigText = 'x'.repeat(2000)
  for (let i = 1; i <= 30; i++) {
    addDay(rlDir, botId, 'r1', `2026-01-${String(i).padStart(2, '0')}`, [msg('assistant', [tu('mem0_search'), txt(bigText)])])
  }
  for (let i = 1; i <= 5; i++) {
    addDay(rlDir, botId, 'r1', `2026-02-${String(i).padStart(2, '0')}`, [msg('assistant', [tu('mem0_search'), txt(bigText)])])
  }
  // 预置极短 state.json：声明前 28 天已被 compact 成一句话；2026-01-29、2026-01-30 是新增的 older raw
  mkdirSync(join(rlDir, 'history-compact', botId), { recursive: true })
  writeFileSync(
    join(rlDir, 'history-compact', botId, 'state.json'),
    JSON.stringify({
      compactText: '【预置压缩】前 28 天浓缩成一行——只保留这句话验证 state 被读到。',
      compactedUpToDate: '2026-01-28',
    }),
  )
  // budgetChars=4000：recent ~ 5 天（5×430+少量 ≈ 2200 > 1200=30%，触发 break，但至少 1 天总保留），
  // older = 30 天。candidate = compactText(~50) + 2 个新 raw(~860) ≈ 910 < compactBudget(2400) → 不走 LLM。
  // openclawJsonPath 指向不存在的文件：若错误地进 LLM 分支，resolveLlmEndpoint 会抛 ENOENT。
  const r = await buildHistoryWindow({
    rlOpenclawDir: rlDir, botId, beforeDate: '2026-02-06',
    openclawJsonPath: '/definitely-does-not-exist.json', budgetChars: 4000,
    // 不传 skipLlmCompact —— 验证增量分支真的没碰 LLM
  })
  assert.equal(r.dayCount, 35)
  assert.match(r.markdown, /预置压缩/, 'expected compactText from state.json to be reused verbatim')
  assert.match(r.markdown, /2026-01-29/, 'expected newly added older day to be raw-appended')
  assert.match(r.markdown, /2026-01-30/)
  // state.json 应未被改写（compactedUpToDate 还是 2026-01-28）
  const state = JSON.parse(readFileSync(join(rlDir, 'history-compact', botId, 'state.json'), 'utf8')) as { compactedUpToDate: string }
  assert.equal(state.compactedUpToDate, '2026-01-28', 'state should not be overwritten when no recompact needed')
  cleanup()
})
