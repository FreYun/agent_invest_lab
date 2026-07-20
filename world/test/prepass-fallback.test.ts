import { test } from 'node:test'
import assert from 'node:assert/strict'
import { generateReportWithFallback, type ReporterFallbackDeps } from '../src/market-reports/prepass-driver.ts'

// 构造一个可编程的 deps：用 landAfter 控制「第几次 chat 之后报告才落库」，
// restartFails 控制硬重启是否失败。记录 chat 用的 session_key 和重启次数，便于断言。
function makeDeps(opts: {
  landAfterChats?: number      // N>0：第 N 次 chat 后 reportExists 才变 true；0=从一开始就有；Infinity=永不落库
  landAfterRestarts?: number   // 需要发生过多少次成功重启后才允许落库（与 landAfterChats 取「都满足」）
  retries: number
  hardRestarts: number
  restartFails?: boolean
  chatThrows?: boolean
}): { deps: ReporterFallbackDeps; calls: { sessionKeys: string[]; restarts: number; landed: boolean; logs: string[] } } {
  const calls = { sessionKeys: [] as string[], restarts: 0, landed: false, logs: [] as string[] }
  const landAfterChats = opts.landAfterChats ?? Infinity
  const landAfterRestarts = opts.landAfterRestarts ?? 0
  let chatCount = 0
  const deps: ReporterFallbackDeps = {
    reportExists: () => calls.landed,
    chat: async (sessionKey: string) => {
      calls.sessionKeys.push(sessionKey)
      chatCount++
      if (opts.chatThrows) throw new Error('simulated chat_llm timeout')
      if (chatCount >= landAfterChats && calls.restarts >= landAfterRestarts) calls.landed = true
      return {}
    },
    restart: async () => {
      if (opts.restartFails) return false
      calls.restarts++
      return true
    },
    sleep: async () => {},   // 测试里不真等
    log: (m: string) => { calls.logs.push(m) },
    retries: opts.retries,
    hardRestarts: opts.hardRestarts,
    retryBackoffMs: 0,
    restartBackoffMs: 0,
  }
  return { deps, calls }
}

test('幂等：已落库则直接成功，不发起任何 chat / restart', async () => {
  const { deps, calls } = makeDeps({ landAfterChats: 0, retries: 2, hardRestarts: 1 })
  calls.landed = true   // 一开始就有
  const ok = await generateReportWithFallback('reporter-context', 'base-key', deps)
  assert.equal(ok, true)
  assert.equal(calls.sessionKeys.length, 0)
  assert.equal(calls.restarts, 0)
})

test('首次 chat 即落库：成功，1 次 chat，0 次重启，用基础 session_key', async () => {
  const { deps, calls } = makeDeps({ landAfterChats: 1, retries: 2, hardRestarts: 1 })
  const ok = await generateReportWithFallback('reporter-context', 'base-key', deps)
  assert.equal(ok, true)
  assert.equal(calls.sessionKeys.length, 1)
  assert.equal(calls.sessionKeys[0], 'base-key')
  assert.equal(calls.restarts, 0)
})

test('同进程内重试落库：第 3 次（attempt=2）才成功，仍不触发硬重启', async () => {
  const { deps, calls } = makeDeps({ landAfterChats: 3, retries: 2, hardRestarts: 1 })
  const ok = await generateReportWithFallback('reporter-context', 'base-key', deps)
  assert.equal(ok, true)
  assert.equal(calls.sessionKeys.length, 3)               // 首次 + 2 次重试
  assert.deepEqual(calls.sessionKeys, ['base-key', 'base-key', 'base-key'])
  assert.equal(calls.restarts, 0)
})

// 这是 2026-06-23 market_context 缺失的回归用例：
// 进程级 MCP 初始化瞬时失败 → 同进程多少次重试都不落库；唯有硬重启子进程（全新 MCP init）
// 后换干净 session_key 才成功。验证兜底确实救回这一类故障。
test('硬重启兜底救回：同进程重试全失败，硬重启后用新 session_key 落库', async () => {
  const { deps, calls } = makeDeps({ retries: 2, hardRestarts: 1, landAfterChats: 4, landAfterRestarts: 1 })
  const ok = await generateReportWithFallback('reporter-context', 'agent:reporter-context:prepass-RUN-2026-06-23', deps)
  assert.equal(ok, true)
  // 阶段一 3 次（base），阶段二 1 次（带 -rs1 后缀）
  assert.equal(calls.sessionKeys.length, 4)
  assert.equal(calls.restarts, 1)
  const base = 'agent:reporter-context:prepass-RUN-2026-06-23'
  assert.deepEqual(calls.sessionKeys.slice(0, 3), [base, base, base])
  assert.equal(calls.sessionKeys[3], base + '-rs1')       // 硬重启后换干净 session_key
})

test('彻底失败：重试 + 硬重启兜底都不落库 → 返回 false，硬重启用满次数', async () => {
  const { deps, calls } = makeDeps({ retries: 2, hardRestarts: 2, landAfterChats: Infinity })
  const ok = await generateReportWithFallback('reporter-context', 'base-key', deps)
  assert.equal(ok, false)
  assert.equal(calls.restarts, 2)                          // hardRestarts=2 全用上
  assert.equal(calls.sessionKeys.length, 3 + 2)            // 阶段一 3 + 阶段二 2
  assert.equal(calls.sessionKeys[3], 'base-key-rs1')
  assert.equal(calls.sessionKeys[4], 'base-key-rs2')
})

test('chat 抛错不会冒泡：被 catch 后按未落库处理，继续走兜底', async () => {
  const { deps, calls } = makeDeps({ retries: 1, hardRestarts: 1, chatThrows: true })
  const ok = await generateReportWithFallback('reporter-context', 'base-key', deps)
  assert.equal(ok, false)                                  // 一直抛错 → 永不落库
  assert.equal(calls.restarts, 1)                          // 仍尝试了硬重启兜底
})

test('硬重启失败则放弃兜底，不再 chat', async () => {
  const { deps, calls } = makeDeps({ retries: 1, hardRestarts: 2, landAfterChats: Infinity, restartFails: true })
  const ok = await generateReportWithFallback('reporter-context', 'base-key', deps)
  assert.equal(ok, false)
  assert.equal(calls.restarts, 0)                          // restart() 返回 false，未计数
  assert.equal(calls.sessionKeys.length, 2)               // 仅阶段一的 1+1，阶段二第一次重启失败即 break
})

test('hardRestarts=0 时退化为纯同进程重试（保持旧行为）', async () => {
  const { deps, calls } = makeDeps({ retries: 2, hardRestarts: 0, landAfterChats: Infinity })
  const ok = await generateReportWithFallback('reporter-context', 'base-key', deps)
  assert.equal(ok, false)
  assert.equal(calls.restarts, 0)
  assert.equal(calls.sessionKeys.length, 3)               // 首次 + 2 重试，无硬重启
})
