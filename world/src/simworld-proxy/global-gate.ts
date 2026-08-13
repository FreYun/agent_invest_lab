/**
 * 跨进程的 simworld 并发闸门。
 *
 * 为什么需要跨进程:每个 run 是独立的 driver 进程,各起一个 simworld-proxy 实例,
 * 各有各的 per-bot 串行闸门。那个闸门只保证"单个 bot 不并发",对"N 个 bot 之间
 * 的并发"完全无效 —— 这正是 50 并发下 simworld 崩掉的直接原因。
 *
 * 实测依据(2026-08-13 直接压 :18078):
 *   并发 10 → p50 444ms,吞吐 42/s
 *   并发 20 → p50 5262ms   ← 悬崖
 *   并发 40 → p50 11099ms,100% 超过 7 秒预算
 * 所以把全局在途数钳在 12(悬崖前的安全区)。
 *
 * 实现用 mkdir 做原子槽位(POSIX 下 mkdir 是原子的,且无需额外依赖)。
 * ★ 全程 fail-open:限流器自身任何异常都退化为"不限流",绝不阻塞业务。
 */
import { mkdirSync, rmdirSync, readdirSync, writeFileSync, readFileSync, unlinkSync } from 'node:fs'
import { join } from 'node:path'

const GATE_DIR = process.env.SIMWORLD_GATE_DIR ?? '/tmp/simworld-gate'
const SLOTS = Number(process.env.SIMWORLD_GATE_SLOTS ?? 12)
// 等不到槽位时的最长等待。超过就 fail-open 放行 —— 宁可偶尔超发,
// 也不能让 agent 卡死在闸门上(那会直接吃掉 15:00 的时间预算)。
const MAX_WAIT_MS = Number(process.env.SIMWORLD_GATE_MAX_WAIT_MS ?? 8000)
const POLL_MS = 20
// 槽位持有超过这个时长即视为进程已死留下的僵尸,可被回收
const STALE_MS = Number(process.env.SIMWORLD_GATE_STALE_MS ?? 60000)

let enabled = true
const stats = { acquired: 0, waited: 0, waitMsTotal: 0, failOpen: 0, reaped: 0 }

function slotPath(i: number): string {
  return join(GATE_DIR, `slot-${String(i).padStart(2, '0')}`)
}

function ensureDir(): void {
  try {
    mkdirSync(GATE_DIR, { recursive: true })
  } catch {
    enabled = false // 建不出目录就别限流了
  }
}
ensureDir()

/** 回收僵尸槽位:持有进程已不存在,或持有时间超过 STALE_MS */
function reapStale(i: number): boolean {
  const p = slotPath(i)
  try {
    const meta = JSON.parse(readFileSync(join(p, 'owner.json'), 'utf8')) as { pid: number; ts: number }
    const dead = !isAlive(meta.pid)
    const stale = Date.now() - meta.ts > STALE_MS
    if (dead || stale) {
      release(i)
      stats.reaped++
      return true
    }
  } catch {
    // 没有 owner.json 或读不动:可能是刚创建到一半。只有明显过期才回收。
    try {
      const st = readdirSync(p)
      if (st.length === 0) { release(i); stats.reaped++; return true }
    } catch { /* 目录已消失 */ }
  }
  return false
}

function isAlive(pid: number): boolean {
  if (!pid || !Number.isInteger(pid)) return false
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

function tryAcquire(i: number): boolean {
  const p = slotPath(i)
  try {
    mkdirSync(p) // 原子:已存在会抛 EEXIST
    writeFileSync(join(p, 'owner.json'), JSON.stringify({ pid: process.pid, ts: Date.now() }))
    return true
  } catch (e) {
    const code = (e as NodeJS.ErrnoException).code
    if (code === 'EEXIST') return false
    enabled = false // 其它错误(权限/磁盘满)→ 直接停用限流,fail-open
    return false
  }
}

function release(i: number): void {
  const p = slotPath(i)
  try { unlinkSync(join(p, 'owner.json')) } catch { /* 可能已不在 */ }
  try { rmdirSync(p) } catch { /* 已被别人回收 */ }
}

function shuffled(n: number): number[] {
  const a = Array.from({ length: n }, (_, i) => i)
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
      ;[a[i], a[j]] = [a[j], a[i]]
  }
  return a
}

export interface GateHandle { release: () => void; waitedMs: number; failOpen: boolean }

/**
 * 取一个全局槽位。永远会返回(可能是 fail-open 的空槽),调用方必须 release。
 */
export async function acquireSlot(): Promise<GateHandle> {
  if (!enabled || SLOTS <= 0) {
    return { release: () => {}, waitedMs: 0, failOpen: true }
  }
  const t0 = Date.now()
  let reapedOnce = false
  for (;;) {
    for (const i of shuffled(SLOTS)) {
      if (tryAcquire(i)) {
        const waitedMs = Date.now() - t0
        stats.acquired++
        if (waitedMs > 0) { stats.waited++; stats.waitMsTotal += waitedMs }
        let released = false
        return {
          waitedMs,
          failOpen: false,
          release: () => { if (!released) { released = true; release(i) } },
        }
      }
    }
    // 全满:先尝试回收一轮僵尸,再等
    if (!reapedOnce) {
      reapedOnce = true
      for (let i = 0; i < SLOTS; i++) reapStale(i)
      continue
    }
    if (Date.now() - t0 > MAX_WAIT_MS) {
      // ★ fail-open:等太久就放行。宁可偶尔超发,也不能卡死 agent。
      stats.failOpen++
      return { release: () => {}, waitedMs: Date.now() - t0, failOpen: true }
    }
    await new Promise(r => setTimeout(r, POLL_MS))
  }
}

/** 当前占用槽位数(给看板读) */
export function occupancy(): { used: number; slots: number } {
  if (!enabled) return { used: 0, slots: 0 }
  try {
    const used = readdirSync(GATE_DIR).filter(d => d.startsWith('slot-')).length
    return { used, slots: SLOTS }
  } catch {
    return { used: 0, slots: SLOTS }
  }
}

export function gateStats() {
  return { ...stats, enabled, slots: SLOTS, dir: GATE_DIR, ...occupancy() }
}
