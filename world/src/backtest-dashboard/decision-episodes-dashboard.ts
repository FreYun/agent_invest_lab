import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { createServer, type ServerResponse } from 'node:http'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadDecisionEpisodeCatalog, loadDecisionEpisodeDetail } from './canonical-decision-data.ts'
import {
  loadDecisionEpisodeCatalog as loadOriginalCatalog,
  loadDecisionEpisodeDetail as loadOriginalDetail,
} from './decision-episodes-data.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const DEFAULT_HTML = join(HERE, 'decision-episodes-v2.html')
const DEFAULT_DB = resolve(HERE, '../../runtime/decision-episodes/decision-episodes-v2.db')
const DEFAULT_ORIGINAL_DB = resolve(HERE, '../../runtime/decision-episodes/decision-episodes-v1.db')
const DEFAULT_QWEN_DIR = resolve(HERE, '../../runtime/decision-episodes/qwen3.7-plus')

function arg(name: string): string | undefined {
  const index = process.argv.indexOf(name)
  return index >= 0 ? process.argv[index + 1] : undefined
}

function sendJson(res: ServerResponse, status: number, value: unknown): void {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
  res.end(JSON.stringify(value))
}

const host = arg('--host') ?? '0.0.0.0'
const port = Number(arg('--port') ?? '48081')
const dbPath = resolve(arg('--db') ?? DEFAULT_DB)
const originalDbPath = resolve(arg('--original-db') ?? DEFAULT_ORIGINAL_DB)
const qwenDirPath = resolve(arg('--qwen-dir') ?? DEFAULT_QWEN_DIR)
if (!existsSync(dbPath)) throw new Error(`decision episode database not found: ${dbPath}`)
if (!existsSync(originalDbPath)) throw new Error(`original decision episode database not found: ${originalDbPath}`)

function loadQwenCandidate(caseId: string): unknown {
  const candidatePath = join(qwenDirPath, caseId + '.validated.candidate.json')
  const validationPath = join(qwenDirPath, caseId + '.validated.validation.json')
  if (!existsSync(candidatePath)) return { available: false, reason: 'Qwen candidate not found' }
  const envelope = JSON.parse(readFileSync(candidatePath, 'utf8')) as Record<string, unknown>
  const validation = existsSync(validationPath)
    ? JSON.parse(readFileSync(validationPath, 'utf8')) as unknown
    : null
  return { available: true, ...envelope, validation }
}

function loadQwenCatalog(): unknown {
  if (!existsSync(qwenDirPath)) return { available: false, candidates: [] }
  const candidates = readdirSync(qwenDirPath)
    .filter(name => name.endsWith('.validated.candidate.json'))
    .map(name => {
      const caseId = name.slice(0, -'.validated.candidate.json'.length)
      const payload = loadQwenCandidate(caseId) as { extraction_metadata?: { model?: string }, candidate?: { claims?: Array<{ statement?: string }>, relation_candidates?: unknown[], conflicts?: unknown[] }, validation?: { valid?: boolean } }
      const tradeDate = caseId.slice(-10)
      return {
        episode_id: caseId, trade_date: tradeDate, model: payload.extraction_metadata?.model ?? null,
        valid: payload.validation?.valid === true, summary: payload.candidate?.claims?.[0]?.statement ?? 'Qwen candidate',
        claim_count: payload.candidate?.claims?.length ?? 0, relation_count: payload.candidate?.relation_candidates?.length ?? 0,
        conflict_count: payload.candidate?.conflicts?.length ?? 0, canonical_available: caseId === 'bot105d_2026-08-05',
      }
    })
    .sort((a, b) => b.trade_date.localeCompare(a.trade_date))
  return { available: true, candidates }
}

const server = createServer((req, res) => {
  const url = new URL(req.url ?? '/', `http://${req.headers.host ?? `${host}:${port}`}`)
  if (req.method === 'GET' && (url.pathname === '/' || url.pathname === '/decision-episodes.html')) {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' })
    res.end(readFileSync(DEFAULT_HTML, 'utf8'))
    return
  }
  if (req.method === 'GET' && url.pathname === '/api/catalog') {
    sendJson(res, 200, loadDecisionEpisodeCatalog(dbPath))
    return
  }
  if (req.method === 'GET' && url.pathname === '/api/episode') {
    const episodeId = url.searchParams.get('episode_id') ?? ''
    if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(episodeId)) {
      sendJson(res, 400, { error: 'valid episode_id is required' })
      return
    }
    const detail = loadDecisionEpisodeDetail(dbPath, episodeId)
    sendJson(res, detail ? 200 : 404, detail ?? { error: 'episode not found' })
    return
  }
  if (req.method === "GET" && url.pathname === "/api/original-catalog") {
    sendJson(res, 200, loadOriginalCatalog(originalDbPath))
    return
  }
  if (req.method === "GET" && url.pathname === "/api/original-by-date") {
    const tradeDate = url.searchParams.get("trade_date") ?? ""
    const botId = url.searchParams.get("bot_id") ?? "bot105d"
    if (!/^\d{4}-\d{2}-\d{2}$/.test(tradeDate) || !/^[A-Za-z0-9_.:-]{1,128}$/.test(botId)) {
      sendJson(res, 400, { error: "valid trade_date and bot_id are required" })
      return
    }
    const catalog = loadOriginalCatalog(originalDbPath) as { available?: boolean, episodes?: Array<Record<string, unknown>> }
    const match = (catalog.episodes ?? []).find(item => item.trade_date === tradeDate && item.bot_id === botId)
    if (!match?.episode_id) {
      sendJson(res, 404, { error: "original v1 episode not found for date and bot" })
      return
    }
    const detail = loadOriginalDetail(originalDbPath, String(match.episode_id))
    sendJson(res, detail ? 200 : 404, detail ?? { error: "original v1 episode not found" })
    return
  }
  if (req.method === 'GET' && url.pathname === '/api/qwen-catalog') {
    sendJson(res, 200, loadQwenCatalog())
    return
  }
  if (req.method === 'GET' && url.pathname === '/api/qwen-candidate') {
    const episodeId = url.searchParams.get('episode_id') ?? ''
    if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(episodeId)) {
      sendJson(res, 400, { error: 'valid episode_id is required' })
      return
    }
    const payload = loadQwenCandidate(episodeId) as { candidate?: { case_id?: string } }
    if (payload.candidate?.case_id !== episodeId) {
      sendJson(res, 404, { error: 'Qwen candidate not found for episode' })
      return
    }
    sendJson(res, 200, payload)
    return
  }
  if (req.method === 'GET' && url.pathname === '/health') {
    sendJson(res, 200, { status: 'ok', dbPath, originalDbPath, qwenDirPath, readOnly: true })
    return
  }
  sendJson(res, 404, { error: 'not found' })
})

server.listen(port, host, () => {
  process.stdout.write(`decision episode dashboard listening on http://${host}:${port}\n`)
  process.stdout.write(`read-only database: ${dbPath}\n`)
  process.stdout.write(`read-only original database: ${originalDbPath}\n`)
})
