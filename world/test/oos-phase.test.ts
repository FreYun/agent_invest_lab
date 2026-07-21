import { test } from 'node:test'
import assert from 'node:assert/strict'

// config.ts：parseWorldConfig 从 raw object 解析（含 skip_close/skip_chat 映射）。
import { parseWorldConfig } from '../src/config.ts'
// oos-daily-driver.ts：phaseToFlags 纯映射函数。
import { phaseToFlags } from '../src/oos-daily-driver.ts'

test('parseWorldConfig 映射 skip_close/skip_chat', () => {
  const cfg = parseWorldConfig({
    research_loop: '/opt/rl',
    bots: ['bot1'], replay: { from: '2026-07-21', to: '2026-07-21' },
    calendar: '../runtime/calendar.json',
    skip_close: true, skip_chat: false,
  } as any)
  assert.equal(cfg.skipClose, true)
  assert.equal(cfg.skipChat, false)
})

test('phaseToFlags: decide/settle/缺省', () => {
  assert.deepEqual(phaseToFlags('decide'), { skipClose: true, skipChat: false })
  assert.deepEqual(phaseToFlags('settle'), { skipClose: false, skipChat: true })
  assert.deepEqual(phaseToFlags(undefined), { skipClose: false, skipChat: false })
})

test('parseWorldConfig 省略 skip_close/skip_chat → undefined', () => {
  const cfg = parseWorldConfig({
    research_loop: '/opt/rl',
    bots: ['bot1'], replay: { from: '2026-07-21', to: '2026-07-21' },
    calendar: '../runtime/calendar.json',
  } as any)
  assert.equal(cfg.skipClose, undefined)
  assert.equal(cfg.skipChat, undefined)
})

test('phaseToFlags: 非法值抛错', () => {
  assert.throws(() => phaseToFlags('bogus'), /--phase 只能是/)
  assert.throws(() => phaseToFlags('desicde'), /--phase 只能是/)
})
