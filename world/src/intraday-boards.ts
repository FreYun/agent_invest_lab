import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join } from 'node:path'

// 方案 B+：给 48080 看板两张日度主线卡叠加「今日盘中实时板块快照」。
// 只读实时行情、不写库、不碰 mainline_daily_plan.py 状态机 → 无未来函数、无口径漂移。
// 数据活全在 scripts/intraday_board_snapshot.py（scout 板块全集 + fund.db 骨架 + ttjj 实时），
// 本模块只负责 spawn + 解析 + 短缓存（实时无需每次都打上游）。

export interface IntradaySnapshot {
  applicable: boolean
  reason?: string
  date: string
  [k: string]: unknown
}

const CACHE_TTL_MS = 60_000
const _cache = new Map<string, { ts: number; data: IntradaySnapshot }>()

function pickPython(repoRoot: string): string {
  // CLAUDE.md：本机裸 python3 会命中缺 mcp 的 uv 3.11；固定 3.12，可用 TTJJ_PYTHON 覆盖。
  if (process.env.TTJJ_PYTHON) return process.env.TTJJ_PYTHON
  if (existsSync('/usr/bin/python3.12')) return '/usr/bin/python3.12'
  return 'python3'
}

export async function fetchIntradayBoards(opts: {
  repoRoot: string
  dbPath: string
  date: string
  timeoutMs?: number
}): Promise<IntradaySnapshot> {
  const { repoRoot, dbPath, date } = opts
  // 空 date = 服务端今日（盘中快照只对「今天」有意义）；非空须是合法日期。
  if (date && !/^\d{4}-\d{2}-\d{2}$/.test(date)) return { applicable: false, reason: 'bad_date', date }

  const cacheKey = date || 'today'
  const cached = _cache.get(cacheKey)
  if (cached && Date.now() - cached.ts < CACHE_TTL_MS) return cached.data

  const python = pickPython(repoRoot)
  const script = join(repoRoot, 'scripts', 'intraday_board_snapshot.py')
  const args = date ? [script, '--date', date, '--fund-db', dbPath] : [script, '--fund-db', dbPath]

  const data = await new Promise<IntradaySnapshot>(resolveP => {
    const child = spawn(python, args, { cwd: repoRoot, stdio: ['ignore', 'pipe', 'pipe'], env: process.env })
    let stdout = ''
    let stderr = ''
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL') } catch { /* ignore */ }
      resolveP({ applicable: false, reason: `timeout after ${opts.timeoutMs ?? 30000}ms`, date })
    }, opts.timeoutMs ?? 30000)
    child.stdout.on('data', d => { stdout += d.toString() })
    child.stderr.on('data', d => { stderr += d.toString() })
    child.on('error', err => { clearTimeout(timer); resolveP({ applicable: false, reason: err.message, date }) })
    child.on('close', code => {
      clearTimeout(timer)
      if (code !== 0 && !stdout.trim()) {
        resolveP({ applicable: false, reason: stderr.trim() || `python exited ${code ?? -1}`, date })
        return
      }
      try {
        resolveP(JSON.parse(stdout) as IntradaySnapshot)
      } catch (err) {
        resolveP({ applicable: false, reason: err instanceof Error ? err.message : String(err), date })
      }
    })
  })

  // 只缓存「取到数」的结果；applicable=false（未取到/非今日）不缓存，便于下次重试。
  if (data.applicable) _cache.set(cacheKey, { ts: Date.now(), data })
  return data
}
