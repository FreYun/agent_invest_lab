import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { DatabaseSync, type SQLInputValue } from 'node:sqlite'
import { fileURLToPath } from 'node:url'
import {
  asNumber,
  extractAgentMetrics,
  extractCandidateActions,
  extractClaims,
  extractNarrativeConflicts,
  parseJsonObject,
  pickFinalReply,
  sha256,
  stableJson,
  summaryFromReply,
  type CandidateActionDraft,
  type ParsedReply,
} from './normalize.ts'
import { initializeDecisionEpisodeDatabase } from './schema.ts'

export const DECISION_EPISODE_EXTRACTOR_VERSION = 'decision-episode-normalizer-v1.2'

export interface ImportDecisionEpisodesOptions {
  worldRoot: string
  fundDbPath: string
  outputDbPath: string
  reportPath: string
  runId: string
  botId: string
  startDate?: string
  endDate?: string
  now?: string
}

export interface ImportDecisionEpisodesResult {
  batchId: string
  episodesInserted: number
  episodesSkipped: number
  factsInserted: number
  relationsInserted: number
  claimsInserted: number
  evidenceInserted: number
  candidateActionsInserted: number
  finalActionsInserted: number
  conflictsInserted: number
  qualityIssuesInserted: number
  outputDbPath: string
  reportPath: string
}

interface SourceArtifact {
  kind: string
  path: string
  content: Buffer | null
  hash: string | null
  size: number | null
  integrity: 'sha256_verified' | 'record_hash_only' | 'missing'
  mediaType: string
}

interface EpisodeRows {
  snapshots: Record<string, unknown>[]
  actions: Record<string, unknown>[]
  orders: Record<string, unknown>[]
  funds: Record<string, unknown>[]
}

type CounterKey = Exclude<keyof ImportDecisionEpisodesResult, 'batchId' | 'outputDbPath' | 'reportPath'>

const json = (value: unknown): string => JSON.stringify(value)

function query(db: DatabaseSync, sql: string, ...params: SQLInputValue[]): Record<string, unknown>[] {
  return db.prepare(sql).all(...params) as Record<string, unknown>[]
}

function datesForRun(runRoot: string, botId: string, start?: string, end?: string): string[] {
  return readdirSync(runRoot, { withFileTypes: true })
    .filter(entry => entry.isDirectory() && /^\d{4}-\d{2}-\d{2}$/.test(entry.name))
    .map(entry => entry.name)
    .filter(date => (!start || date >= start) && (!end || date <= end))
    .filter(date => existsSync(join(runRoot, date, botId, 'reply.json')))
    .sort()
}

function sourceArtifacts(dayRoot: string, worldRoot: string, fundDbPath: string): SourceArtifact[] {
  const names: Array<[string, string, string]> = [
    ['session_reply', 'reply.json', 'application/json'],
    ['session_input', 'sent.md', 'text/markdown'],
    ['close_snapshot', 'close_my_day.json', 'application/json'],
    ['belief_validation', 'belief_validation.json', 'application/json'],
    ['run_status', 'status.json', 'application/json'],
  ]
  const artifacts: SourceArtifact[] = names.map(([kind, name, mediaType]) => {
    const path = join(dayRoot, name)
    if (!existsSync(path)) {
      return { kind, path, content: null, hash: null, size: null, integrity: 'missing' as const, mediaType }
    }
    const content = readFileSync(path)
    return {
      kind, path, content, hash: sha256(content), size: content.length,
      integrity: 'sha256_verified' as const, mediaType,
    }
  })
  const fundStat = statSync(fundDbPath)
  artifacts.push({
    kind: 'portfolio_database',
    path: fundDbPath,
    content: null,
    hash: null,
    size: fundStat.size,
    integrity: 'record_hash_only',
    mediaType: 'application/x-sqlite3',
  })
  return artifacts.map(artifact => ({
    ...artifact,
    path: artifact.path.startsWith(worldRoot) ? relative(worldRoot, artifact.path) : artifact.path,
  }))
}

function parseArtifactJson(artifacts: SourceArtifact[], kind: string): Record<string, unknown> | null {
  const content = artifacts.find(artifact => artifact.kind === kind)?.content
  return content ? parseJsonObject(content.toString('utf8')) : null
}

function referencedFunds(parsed: ParsedReply, rows: Omit<EpisodeRows, 'funds'>): string[] {
  const values = new Set<string>()
  for (const row of [...rows.actions, ...rows.orders]) {
    if (typeof row.fund_code === 'string') values.add(row.fund_code)
  }
  for (const candidate of extractCandidateActions(parsed)) {
    if (candidate.instrument?.startsWith('fund:')) values.add(candidate.instrument.slice(5))
  }
  for (const snapshot of rows.snapshots) {
    if (typeof snapshot.holdings_json !== 'string') continue
    try {
      const holdings = JSON.parse(snapshot.holdings_json) as unknown
      if (!Array.isArray(holdings)) continue
      for (const holding of holdings) {
        const code = (holding as { fund_code?: unknown } | null)?.fund_code
        if (typeof code === 'string') values.add(code)
      }
    } catch { /* quality issue is emitted later */ }
  }
  return [...values].sort()
}

function loadEpisodeRows(fundDb: DatabaseSync, runId: string, botId: string, date: string, parsed: ParsedReply): EpisodeRows {
  const snapshots = query(fundDb, `
    SELECT * FROM fund_bot_daily_snapshots
    WHERE bot_id=? AND run_id=? AND trade_date=?
  `, botId, runId, date)
  const actions = query(fundDb, `
    SELECT * FROM fund_bot_actions
    WHERE bot_id=? AND run_id=? AND action_date=? ORDER BY action_id
  `, botId, runId, date)
  const orders = query(fundDb, `
    SELECT * FROM fund_bot_orders
    WHERE bot_id=? AND order_run_id=? AND order_date=? ORDER BY order_id
  `, botId, runId, date)
  const codes = referencedFunds(parsed, { snapshots, actions, orders })
  const funds = codes.length
    ? query(fundDb, `SELECT * FROM fund_info WHERE fund_code IN (${codes.map(() => '?').join(',')})`, ...codes)
    : []
  return { snapshots, actions, orders, funds }
}

function artifactId(episodeId: string, artifact: SourceArtifact): string {
  return `src_${sha256(`${episodeId}|${artifact.kind}|${artifact.path}`).slice(0, 24)}`
}

function insertArtifact(db: DatabaseSync, episodeId: string, artifact: SourceArtifact, now: string): string {
  const id = artifactId(episodeId, artifact)
  db.prepare(`
    INSERT OR IGNORE INTO source_artifacts(
      artifact_id,episode_id,artifact_kind,source_path,sha256,byte_size,media_type,integrity_status,captured_at
    ) VALUES (?,?,?,?,?,?,?,?,?)
  `).run(id, episodeId, artifact.kind, artifact.path, artifact.hash, artifact.size, artifact.mediaType, artifact.integrity, now)
  return id
}

function recordHash(row: unknown): string {
  return sha256(stableJson(row))
}

function insertFact(db: DatabaseSync, values: {
  episodeId: string
  subject: string
  predicate: string
  objectText?: string | null
  valueNumber?: number | null
  valueText?: string | null
  unit?: string | null
  eventTime?: string | null
  availableAt?: string | null
  validFrom?: string | null
  validTo?: string | null
  factClass: 'recorded_fact' | 'reference_fact' | 'derived_fact' | 'agent_reported_observation'
  certainty: 'hard_recorded' | 'hard_reference' | 'derived' | 'agent_asserted'
  sourceArtifactId: string | null
  sourceLocator: string
  rawExcerpt: string
  now: string
}): number {
  const sourceRecordHash = recordHash({
    subject: values.subject, predicate: values.predicate, objectText: values.objectText,
    valueNumber: values.valueNumber, valueText: values.valueText, source: values.sourceLocator,
    excerpt: values.rawExcerpt,
  })
  const id = `fact_${sha256(`${values.episodeId}|${sourceRecordHash}`).slice(0, 24)}`
  return Number(db.prepare(`
    INSERT OR IGNORE INTO facts(
      fact_id,episode_id,subject,predicate,object_text,value_number,value_text,unit,event_time,
      available_at,valid_from,valid_to,fact_class,certainty,source_artifact_id,source_locator,
      source_record_hash,raw_excerpt,created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
  `).run(
    id, values.episodeId, values.subject, values.predicate, values.objectText ?? null,
    values.valueNumber ?? null, values.valueText ?? null, values.unit ?? null,
    values.eventTime ?? null, values.availableAt ?? null, values.validFrom ?? null,
    values.validTo ?? null, values.factClass, values.certainty, values.sourceArtifactId,
    values.sourceLocator, sourceRecordHash, values.rawExcerpt, values.now,
  ).changes)
}

function insertRelation(db: DatabaseSync, values: {
  episodeId: string
  subject: string
  predicate: string
  object: string
  direction: string
  mechanism: string
  validFrom?: string | null
  validTo?: string | null
  certainty: 'hard_recorded' | 'hard_reference' | 'derived' | 'policy_defined' | 'estimated'
  evidenceType: string
  sourceArtifactId: string | null
  sourceLocator: string
  rawExcerpt: string
  now: string
}): number {
  const sourceRecordHash = recordHash(values.rawExcerpt)
  const id = `rel_${sha256(`${values.episodeId}|${values.subject}|${values.predicate}|${values.object}|${sourceRecordHash}`).slice(0, 24)}`
  return Number(db.prepare(`
    INSERT OR IGNORE INTO relations(
      relation_id,episode_id,subject,predicate,object,direction,mechanism,valid_from,valid_to,
      certainty,evidence_type,source_artifact_id,source_locator,source_record_hash,raw_excerpt,created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
  `).run(
    id, values.episodeId, values.subject, values.predicate, values.object, values.direction,
    values.mechanism, values.validFrom ?? null, values.validTo ?? null, values.certainty,
    values.evidenceType, values.sourceArtifactId, values.sourceLocator, sourceRecordHash,
    values.rawExcerpt, values.now,
  ).changes)
}

function insertIssue(db: DatabaseSync, values: {
  episodeId: string
  severity: 'info' | 'warning' | 'error'
  issueType: string
  fieldPath: string
  description: string
  observed: unknown
  sourceArtifactId: string | null
  sourceLocator: string
  now: string
}): number {
  const id = `issue_${sha256(stableJson(values)).slice(0, 24)}`
  return Number(db.prepare(`
    INSERT OR IGNORE INTO quality_issues(
      issue_id,episode_id,severity,issue_type,field_path,description,observed_json,
      source_artifact_id,source_locator,created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?)
  `).run(
    id, values.episodeId, values.severity, values.issueType, values.fieldPath,
    values.description, json(values.observed), values.sourceArtifactId, values.sourceLocator, values.now,
  ).changes)
}

function insertFinalActions(
  db: DatabaseSync,
  episodeId: string,
  rows: EpisodeRows,
  fundDbArtifactId: string,
  now: string,
): { count: number; orderIds: Map<string, string> } {
  let count = 0
  const orderIds = new Map<string, string>()
  const insert = db.prepare(`
    INSERT OR IGNORE INTO final_actions(
      final_action_id,episode_id,source_table,source_primary_key,action_type,instrument,
      amount,shares,fee,status,verification_status,reason,payload_json,source_artifact_id,
      source_locator,source_record_hash,created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
  `)
  for (const [table, primaryKey, sourceRows] of [
    ['fund_bot_orders', 'order_id', rows.orders],
    ['fund_bot_actions', 'action_id', rows.actions],
  ] as const) {
    for (const row of sourceRows) {
      const pk = String(row[primaryKey])
      const sourceRecordHash = recordHash(row)
      const id = `action_${sha256(`${episodeId}|${table}|${pk}|${sourceRecordHash}`).slice(0, 24)}`
      const isOrder = table === 'fund_bot_orders'
      const status = String(row.status ?? '')
      count += Number(insert.run(
        id, episodeId, table, pk,
        String(isOrder ? row.order_type ?? 'unknown' : row.action_type ?? 'unknown').toUpperCase(),
        typeof row.fund_code === 'string' ? `fund:${row.fund_code}` : null,
        asNumber(isOrder ? row.order_amount ?? row.confirmed_amount : row.amount),
        asNumber(isOrder ? row.confirmed_shares : row.shares), asNumber(row.fee), status || null,
        isOrder && /confirmed|done|priced/i.test(`${status} ${String(row.pricing_status ?? '')}`)
          ? 'database_confirmed' : 'database_recorded',
        String(isOrder ? row.action_reason ?? '' : row.reason ?? '') || null,
        json(row), fundDbArtifactId, `${table}:${primaryKey}=${pk}`, sourceRecordHash, now,
      ).changes)
      if (isOrder) orderIds.set(pk, id)
    }
  }
  return { count, orderIds }
}

function sameNumber(left: number | null, right: unknown): boolean {
  const candidate = asNumber(right)
  if (left === null || candidate === null) return false
  return Math.abs(left - candidate) <= Math.max(0.01, Math.abs(candidate) * 1e-8)
}

function reconcileCandidate(candidate: CandidateActionDraft, orders: Record<string, unknown>[]): Record<string, unknown> | null {
  const code = candidate.instrument?.replace(/^fund:/, '')
  const eligible = orders.filter(order =>
    order.fund_code === code && String(order.order_type ?? '').toUpperCase() === candidate.actionType,
  )
  const directId = /"?order_id"?\s*[:=]\s*(\d+)/i.exec(candidate.resultExcerpt)?.[1]
  if (directId) return eligible.find(order => String(order.order_id) === directId) ?? null
  if (candidate.reason) {
    const hasRequestedValue = candidate.requestedAmount !== null || candidate.requestedShares !== null
    const exactReason = eligible.filter(order =>
      order.action_reason === candidate.reason
        && (!hasRequestedValue
          || sameNumber(candidate.requestedAmount, order.order_amount)
          || sameNumber(candidate.requestedShares, order.confirmed_shares)),
    )
    if (exactReason.length === 1) return exactReason[0]
  }
  const valueMatch = eligible.filter(order =>
    sameNumber(candidate.requestedAmount, order.order_amount)
      || sameNumber(candidate.requestedShares, order.confirmed_shares),
  )
  return valueMatch.length === 1 ? valueMatch[0] : null
}

function insertCandidateActions(
  db: DatabaseSync,
  episodeId: string,
  candidates: CandidateActionDraft[],
  orders: Record<string, unknown>[],
  orderIds: Map<string, string>,
  replyArtifactId: string,
  now: string,
): { count: number; reconciledTransportErrors: CandidateActionDraft[] } {
  let count = 0
  const reconciledTransportErrors: CandidateActionDraft[] = []
  const insert = db.prepare(`
    INSERT OR IGNORE INTO candidate_actions(
      candidate_action_id,episode_id,tool_call_id,tool_name,action_type,instrument,
      requested_amount,requested_shares,reason,observed_tool_outcome,reconciled_outcome,
      reconciled_final_action_id,source_artifact_id,source_locator,result_excerpt,
      source_record_hash,created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
  `)
  for (const candidate of candidates) {
    const order = reconcileCandidate(candidate, orders)
    const finalActionId = order ? orderIds.get(String(order.order_id)) ?? null : null
    const sourceRecordHash = recordHash(candidate)
    const id = `candidate_${sha256(`${episodeId}|${candidate.toolCallId}|${sourceRecordHash}`).slice(0, 24)}`
    count += Number(insert.run(
      id, episodeId, candidate.toolCallId, candidate.toolName, candidate.actionType,
      candidate.instrument, candidate.requestedAmount, candidate.requestedShares, candidate.reason,
      candidate.observedOutcome, order ? 'database_confirmed' : 'database_not_found', finalActionId,
      replyArtifactId, candidate.sourceLocator, candidate.resultExcerpt, sourceRecordHash, now,
    ).changes)
    if (candidate.observedOutcome === 'transport_error' && order) reconciledTransportErrors.push(candidate)
  }
  return { count, reconciledTransportErrors }
}

function insertSnapshotFacts(
  db: DatabaseSync,
  episodeId: string,
  botId: string,
  date: string,
  rows: EpisodeRows,
  close: Record<string, unknown> | null,
  fundDbArtifactId: string,
  closeArtifactId: string | null,
  now: string,
): { count: number; mismatches: Array<{ field: string; database: number; close: number }> } {
  let count = 0
  const portfolio = `portfolio:${botId}`
  const snapshot = rows.snapshots[0]
  const databaseFields: Record<string, [string, string | null]> = {
    initial_capital: ['INITIAL_CAPITAL', 'currency'], cash: ['CASH', 'currency'],
    invested_value: ['INVESTED_VALUE', 'currency'], total_value: ['TOTAL_VALUE', 'currency'],
    net_value: ['NET_VALUE', null], daily_return_pct: ['DAILY_RETURN', 'percent'],
    cumulative_return_pct: ['CUMULATIVE_RETURN', 'percent'], max_drawdown_pct: ['MAX_DRAWDOWN', 'percent'],
    equity_weight: ['EQUITY_WEIGHT', 'ratio'], bond_weight: ['BOND_WEIGHT', 'ratio'],
    gold_weight: ['GOLD_WEIGHT', 'ratio'], cash_weight: ['CASH_WEIGHT', 'ratio'],
  }
  if (snapshot) for (const [field, [predicate, unit]] of Object.entries(databaseFields)) {
    const value = asNumber(snapshot[field])
    if (value === null) continue
    count += insertFact(db, {
      episodeId, subject: portfolio, predicate, valueNumber: value, unit, eventTime: date,
      availableAt: date, factClass: 'recorded_fact', certainty: 'hard_recorded',
      sourceArtifactId: fundDbArtifactId,
      sourceLocator: `fund_bot_daily_snapshots:(bot_id=${botId},run_id=${snapshot.run_id},trade_date=${date})#${field}`,
      rawExcerpt: json({ [field]: value }), now,
    })
  }

  const assets = close?.assets && typeof close.assets === 'object' ? close.assets as Record<string, unknown> : {}
  const allocation = close?.asset_allocation_pct && typeof close.asset_allocation_pct === 'object'
    ? close.asset_allocation_pct as Record<string, unknown> : {}
  const pnl = close?.pnl && typeof close.pnl === 'object' ? close.pnl as Record<string, unknown> : {}
  const closeFields: Array<[Record<string, unknown>, string, string, string | null]> = [
    [assets, 'initial_capital', 'INITIAL_CAPITAL', 'currency'],
    [assets, 'cash_available', 'CASH_AVAILABLE', 'currency'],
    [assets, 'cash_in_transit', 'CASH_IN_TRANSIT', 'currency'],
    [assets, 'cash_receivable', 'CASH_RECEIVABLE', 'currency'],
    [assets, 'market_value', 'MARKET_VALUE', 'currency'],
    [assets, 'total_value', 'TOTAL_VALUE', 'currency'],
    [assets, 'net_value', 'NET_VALUE', null],
    [allocation, 'equity', 'EQUITY_WEIGHT', 'percent'],
    [allocation, 'bond', 'BOND_WEIGHT', 'percent'],
    [allocation, 'gold', 'GOLD_WEIGHT', 'percent'],
    [allocation, 'cash', 'CASH_WEIGHT', 'percent'],
    [pnl, 'daily_return_pct', 'DAILY_RETURN', 'percent'],
    [pnl, 'cumulative_return_pct', 'CUMULATIVE_RETURN', 'percent'],
    [pnl, 'max_drawdown_pct', 'MAX_DRAWDOWN', 'percent'],
  ]
  for (const [container, field, predicate, unit] of closeFields) {
    const value = asNumber(container[field])
    if (value === null || !closeArtifactId) continue
    const parent = container === assets ? 'assets' : container === allocation ? 'asset_allocation_pct' : 'pnl'
    count += insertFact(db, {
      episodeId, subject: portfolio, predicate, valueNumber: value, unit, eventTime: date,
      availableAt: date, factClass: 'recorded_fact', certainty: 'hard_recorded',
      sourceArtifactId: closeArtifactId, sourceLocator: `close_my_day.json#/${parent}/${field}`,
      rawExcerpt: json({ [field]: value }), now,
    })
  }

  const mismatches: Array<{ field: string; database: number; close: number }> = []
  for (const [field, closeField] of [['equity_weight', 'equity'], ['cash_weight', 'cash']] as const) {
    const database = snapshot ? asNumber(snapshot[field]) : null
    const sessionClose = asNumber(allocation[closeField])
    if (database === null || sessionClose === null) continue
    const databasePercent = Math.abs(database) <= 1.5 ? database * 100 : database
    if (Math.abs(databasePercent - sessionClose) > 0.05) {
      mismatches.push({ field, database: databasePercent, close: sessionClose })
    }
  }
  return { count, mismatches }
}

function insertHoldingRelations(
  db: DatabaseSync,
  episodeId: string,
  botId: string,
  date: string,
  snapshots: Record<string, unknown>[],
  fundDbArtifactId: string,
  now: string,
): { facts: number; relations: number } {
  let facts = 0, relations = 0
  const snapshot = snapshots[0]
  if (!snapshot || typeof snapshot.holdings_json !== 'string') return { facts, relations }
  let holdings: unknown
  try { holdings = JSON.parse(snapshot.holdings_json) as unknown } catch { return { facts, relations } }
  if (!Array.isArray(holdings)) return { facts, relations }
  for (let index = 0; index < holdings.length; index += 1) {
    const holding = holdings[index] && typeof holdings[index] === 'object'
      ? holdings[index] as Record<string, unknown> : {}
    const code = typeof holding.fund_code === 'string' ? holding.fund_code : null
    const weight = asNumber(holding.weight)
    const marketValue = asNumber(holding.market_value)
    if (!code || ((weight ?? 0) === 0 && (marketValue ?? 0) === 0)) continue
    const excerpt = json(holding)
    relations += insertRelation(db, {
      episodeId, subject: `portfolio:${botId}`, predicate: 'OWNS', object: `fund:${code}`,
      direction: 'portfolio_to_instrument', mechanism: 'end-of-day portfolio position recorded in fund_bot_daily_snapshots',
      validFrom: date, validTo: date, certainty: 'hard_recorded', evidenceType: 'database_snapshot',
      sourceArtifactId: fundDbArtifactId,
      sourceLocator: `fund_bot_daily_snapshots:(bot_id=${botId},run_id=${snapshot.run_id},trade_date=${date})#/holdings/${index}`,
      rawExcerpt: excerpt, now,
    })
    facts += insertFact(db, {
      episodeId, subject: `portfolio:${botId}`, predicate: 'HOLDING_WEIGHT', objectText: `fund:${code}`,
      valueNumber: weight, unit: 'ratio', eventTime: date, availableAt: date,
      factClass: 'recorded_fact', certainty: 'hard_recorded', sourceArtifactId: fundDbArtifactId,
      sourceLocator: `fund_bot_daily_snapshots:(bot_id=${botId},run_id=${snapshot.run_id},trade_date=${date})#/holdings/${index}/weight`,
      rawExcerpt: json({ fund_code: code, weight, market_value: marketValue }), now,
    })
  }
  return { facts, relations }
}

function insertFundRelations(
  db: DatabaseSync,
  episodeId: string,
  funds: Record<string, unknown>[],
  fundDbArtifactId: string,
  now: string,
): number {
  let count = 0
  for (const fund of funds) {
    const code = typeof fund.fund_code === 'string' ? fund.fund_code : null
    const indexName = typeof fund.track_index_name === 'string' ? fund.track_index_name.trim() : ''
    if (!code || !indexName) continue
    count += insertRelation(db, {
      episodeId, subject: `fund:${code}`, predicate: 'TRACKS_INDEX', object: `index:${indexName}`,
      direction: 'fund_to_index', mechanism: 'index fund replication or feeder-fund exposure',
      validFrom: null, validTo: null, certainty: 'hard_reference', evidenceType: 'fund_master_record',
      sourceArtifactId: fundDbArtifactId, sourceLocator: `fund_info:fund_code=${code}#track_index_name`,
      rawExcerpt: json({
        fund_code: code, fund_name: fund.fund_name, fund_type: fund.fund_type,
        theme: fund.theme, track_index_name: indexName,
      }), now,
    })
  }
  return count
}

function insertClaimsAndEvidence(
  db: DatabaseSync,
  episodeId: string,
  parsed: ParsedReply,
  replyArtifactId: string,
  date: string,
  now: string,
): { claims: number; evidence: number; emptyMechanisms: number; emptyFalsifiers: number; unverifiedEvidence: number } {
  let claims = 0, evidence = 0, emptyMechanisms = 0, emptyFalsifiers = 0, unverifiedEvidence = 0
  const claimInsert = db.prepare(`
    INSERT OR IGNORE INTO claims(
      claim_id,episode_id,statement,claim_type,status,confidence,confidence_label,
      mechanism_json,falsifiers_json,valid_from,expires_at,knowledge_cutoff,certainty,
      source_artifact_id,source_locator,source_record_hash,raw_excerpt,created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
  `)
  const evidenceInsert = db.prepare(`
    INSERT OR IGNORE INTO claim_evidence(
      evidence_id,claim_id,relation,evidence_type,evidence_ref,verification_status,
      source_artifact_id,source_locator,source_record_hash,raw_excerpt,created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
  `)
  for (const draft of extractClaims(parsed)) {
    const sourceRecordHash = recordHash(draft)
    const claimId = `claim_${sha256(`${episodeId}|${draft.statement}|${sourceRecordHash}`).slice(0, 24)}`
    claims += Number(claimInsert.run(
      claimId, episodeId, draft.statement, draft.claimType, draft.status, draft.confidence,
      draft.confidenceLabel, json(draft.mechanisms), json(draft.falsifiers), date, null, date,
      draft.certainty, replyArtifactId, draft.sourceLocator, sourceRecordHash, draft.rawExcerpt, now,
    ).changes)
    if (!draft.mechanisms.length) emptyMechanisms += 1
    if (!draft.falsifiers.length) emptyFalsifiers += 1
    for (const item of draft.evidence) {
      const evidenceHash = recordHash(item)
      const evidenceId = `evidence_${sha256(`${claimId}|${evidenceHash}`).slice(0, 24)}`
      evidence += Number(evidenceInsert.run(
        evidenceId, claimId, item.relation, item.evidenceType, item.evidenceRef,
        item.verificationStatus, replyArtifactId, item.sourceLocator, evidenceHash,
        item.rawExcerpt, now,
      ).changes)
      if (item.verificationStatus === 'unverified') unverifiedEvidence += 1
    }
  }
  return { claims, evidence, emptyMechanisms, emptyFalsifiers, unverifiedEvidence }
}

function insertConflicts(
  db: DatabaseSync,
  episodeId: string,
  parsed: ParsedReply,
  candidates: CandidateActionDraft[],
  metrics: ReturnType<typeof extractAgentMetrics>,
  replyArtifactId: string,
  now: string,
): number {
  let count = 0
  const insert = db.prepare(`
    INSERT OR IGNORE INTO decision_conflicts(
      conflict_id,episode_id,conflict_type,left_statement,right_statement,resolution,winner,
      status,source_artifact_id,source_locator,source_record_hash,raw_excerpt,created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
  `)
  const drafts = extractNarrativeConflicts(parsed)
  const metricGroups = new Map<string, number[]>()
  for (const metric of metrics) {
    const key = `${metric.subject}|${metric.predicate}`
    metricGroups.set(key, [...(metricGroups.get(key) ?? []), metric.value])
  }
  for (const [key, values] of metricGroups) {
    const unique = [...new Set(values)]
    if (unique.length < 2) continue
    const [subject, predicate] = key.split('|')
    drafts.push({
      conflictType: 'conflicting_reported_metric', leftStatement: `${subject}.${predicate}=${unique[0]}`,
      rightStatement: unique.slice(1).map(value => `${subject}.${predicate}=${value}`).join('；'),
      resolution: null, winner: null, status: 'data_quality_conflict',
      sourceLocator: 'reply.json#/reply', rawExcerpt: json({ subject, predicate, values: unique }),
    })
  }
  const groups = new Map<string, CandidateActionDraft[]>()
  for (const candidate of candidates) {
    const key = `${candidate.actionType}|${candidate.instrument ?? 'unknown'}`
    groups.set(key, [...(groups.get(key) ?? []), candidate])
  }
  for (const [key, attempts] of groups) {
    const failed = attempts.filter(item => item.observedOutcome === 'rejected' || item.observedOutcome === 'transport_error')
    const accepted = attempts.filter(item => item.observedOutcome === 'accepted')
    if (!failed.length || !accepted.length) continue
    drafts.push({
      conflictType: 'execution_retry', leftStatement: failed.map(item => `${item.toolCallId}:${item.observedOutcome}`).join('；'),
      rightStatement: accepted.map(item => `${item.toolCallId}:accepted`).join('；'),
      resolution: 'subsequent tool attempt was accepted', winner: accepted.at(-1)?.toolCallId ?? null,
      status: 'resolved', sourceLocator: 'reply.json#/messages', rawExcerpt: json({ key, attempts }),
    })
  }
  for (const draft of drafts) {
    const sourceRecordHash = recordHash(draft)
    const id = `conflict_${sha256(`${episodeId}|${sourceRecordHash}`).slice(0, 24)}`
    count += Number(insert.run(
      id, episodeId, draft.conflictType, draft.leftStatement, draft.rightStatement,
      draft.resolution, draft.winner, draft.status, replyArtifactId, draft.sourceLocator,
      sourceRecordHash, draft.rawExcerpt, now,
    ).changes)
  }
  return count
}

function writeReport(db: DatabaseSync, options: ImportDecisionEpisodesOptions, result: ImportDecisionEpisodesResult): void {
  const episodes = db.prepare(`
    SELECT e.trade_date,e.episode_id,e.extraction_status,
      (SELECT COUNT(*) FROM facts f WHERE f.episode_id=e.episode_id) facts,
      (SELECT COUNT(*) FROM relations r WHERE r.episode_id=e.episode_id) relations,
      (SELECT COUNT(*) FROM claims c WHERE c.episode_id=e.episode_id) claims,
      (SELECT COUNT(*) FROM candidate_actions c WHERE c.episode_id=e.episode_id) candidate_actions,
      (SELECT COUNT(*) FROM final_actions a WHERE a.episode_id=e.episode_id) final_actions,
      (SELECT COUNT(*) FROM decision_conflicts c WHERE c.episode_id=e.episode_id) conflicts,
      (SELECT COUNT(*) FROM quality_issues q WHERE q.episode_id=e.episode_id AND q.severity='warning') warnings
    FROM latest_episodes e WHERE e.run_id=? AND e.bot_id=?
      AND (? IS NULL OR e.trade_date>=?) AND (? IS NULL OR e.trade_date<=?)
    ORDER BY e.trade_date
  `).all(
    options.runId, options.botId,
    options.startDate ?? null, options.startDate ?? null,
    options.endDate ?? null, options.endDate ?? null,
  ) as Array<Record<string, unknown>>
  const lines = [
    '# Decision episode import report', '',
    `- Batch: \`${result.batchId}\``,
    `- Run: \`${options.runId}\``,
    `- Bot: \`${options.botId}\``,
    `- Database: \`${result.outputDbPath}\``,
    `- Inserted episodes: ${result.episodesInserted}; skipped unchanged: ${result.episodesSkipped}`,
    `- Facts: ${result.factsInserted}; relations: ${result.relationsInserted}; claims: ${result.claimsInserted}`,
    `- Candidate actions: ${result.candidateActionsInserted}; final action records: ${result.finalActionsInserted}`,
    `- Conflicts: ${result.conflictsInserted}; quality issues: ${result.qualityIssuesInserted}`,
    '',
    '| date | status | facts | relations | claims | candidates | final records | conflicts | warnings |',
    '|---|---:|---:|---:|---:|---:|---:|---:|---:|',
    ...episodes.map(row => `| ${row.trade_date} | ${row.extraction_status} | ${row.facts} | ${row.relations} | ${row.claims} | ${row.candidate_actions} | ${row.final_actions} | ${row.conflicts} | ${row.warnings} |`),
    '',
    'This database keeps source assertions, database records, inferred claims, and quality conflicts separate. It does not promote any claim into reusable trading experience.',
    '',
  ]
  mkdirSync(dirname(options.reportPath), { recursive: true })
  writeFileSync(options.reportPath, lines.join('\n'))
}

export function importDecisionEpisodes(options: ImportDecisionEpisodesOptions): ImportDecisionEpisodesResult {
  const now = options.now ?? new Date().toISOString()
  const runRoot = join(options.worldRoot, 'runtime', 'runs', options.runId)
  if (!existsSync(runRoot)) throw new Error(`Run not found: ${runRoot}`)
  if (!existsSync(options.fundDbPath)) throw new Error(`Fund database not found: ${options.fundDbPath}`)
  const dates = datesForRun(runRoot, options.botId, options.startDate, options.endDate)
  if (!dates.length) throw new Error('No reply.json episodes matched the requested batch')

  mkdirSync(dirname(options.outputDbPath), { recursive: true })
  const output = new DatabaseSync(options.outputDbPath)
  const fundDb = new DatabaseSync(options.fundDbPath, { readOnly: true })
  initializeDecisionEpisodeDatabase(output)
  const firstDate = dates[0]!
  const lastDate = dates[dates.length - 1]!
  const batchId = `batch_${sha256(stableJson({
    runId: options.runId, botId: options.botId, start: firstDate, end: lastDate, now,
  })).slice(0, 24)}`
  output.prepare(`
    INSERT INTO import_batches(
      batch_id,run_id,bot_id,start_date,end_date,source_root,fund_db_path,
      extractor_version,status,summary_json,started_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
  `).run(
    batchId, options.runId, options.botId, firstDate, lastDate, runRoot,
    options.fundDbPath, DECISION_EPISODE_EXTRACTOR_VERSION, 'running', '{}', now,
  )

  const result: ImportDecisionEpisodesResult = {
    batchId, episodesInserted: 0, episodesSkipped: 0, factsInserted: 0,
    relationsInserted: 0, claimsInserted: 0, evidenceInserted: 0,
    candidateActionsInserted: 0, finalActionsInserted: 0, conflictsInserted: 0,
    qualityIssuesInserted: 0, outputDbPath: options.outputDbPath, reportPath: options.reportPath,
  }
  const add = (key: CounterKey, count: number): void => { result[key] += count }

  try {
    output.exec('BEGIN IMMEDIATE')
    for (const date of dates) {
      const dayRoot = join(runRoot, date, options.botId)
      const artifacts = sourceArtifacts(dayRoot, options.worldRoot, options.fundDbPath)
      const parsed = parseArtifactJson(artifacts, 'session_reply') as ParsedReply | null
      if (!parsed) continue
      const rows = loadEpisodeRows(fundDb, options.runId, options.botId, date, parsed)
      const relevantDatabaseHash = recordHash(rows)
      const sourceBundleHash = sha256(stableJson({
        extractorVersion: DECISION_EPISODE_EXTRACTOR_VERSION,
        artifacts: artifacts.map(item => ({ kind: item.kind, path: item.path, hash: item.hash, integrity: item.integrity })),
        relevantDatabaseHash,
      }))
      const logicalKey = `${options.runId}|${options.botId}|${date}`
      const existing = output.prepare(`
        SELECT episode_id FROM episodes WHERE logical_episode_key=? AND source_bundle_hash=?
      `).get(logicalKey, sourceBundleHash)
      if (existing) {
        result.episodesSkipped += 1
        continue
      }
      const episodeId = `episode_${sha256(`${logicalKey}|${sourceBundleHash}`).slice(0, 24)}`
      const summary = summaryFromReply(parsed)
      output.prepare(`
        INSERT INTO episodes(
          episode_id,logical_episode_key,source_bundle_hash,run_id,bot_id,trade_date,
          session_id,decision_time,decision_time_precision,knowledge_cutoff,final_summary,
          extraction_status,extractor_version,import_batch_id,imported_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      `).run(
        episodeId, logicalKey, sourceBundleHash, options.runId, options.botId, date,
        typeof parsed.session_id === 'string' ? parsed.session_id : null, null, 'date_only', date,
        summary, artifacts.some(item => item.kind !== 'portfolio_database' && item.integrity === 'missing') ? 'partial' : 'normalized',
        DECISION_EPISODE_EXTRACTOR_VERSION, batchId, now,
      )
      result.episodesInserted += 1

      const artifactIds = new Map<string, string>()
      for (const artifact of artifacts) artifactIds.set(artifact.kind, insertArtifact(output, episodeId, artifact, now))
      const replyArtifactId = artifactIds.get('session_reply')!
      const closeArtifactId = artifacts.find(item => item.kind === 'close_snapshot')?.integrity === 'missing'
        ? null : artifactIds.get('close_snapshot') ?? null
      const fundDbArtifactId = artifactIds.get('portfolio_database')!

      const final = insertFinalActions(output, episodeId, rows, fundDbArtifactId, now)
      add('finalActionsInserted', final.count)
      const candidates = extractCandidateActions(parsed)
      const candidateResult = insertCandidateActions(
        output, episodeId, candidates, rows.orders, final.orderIds, replyArtifactId, now,
      )
      add('candidateActionsInserted', candidateResult.count)

      const close = parseArtifactJson(artifacts, 'close_snapshot')
      const snapshots = insertSnapshotFacts(
        output, episodeId, options.botId, date, rows, close, fundDbArtifactId, closeArtifactId, now,
      )
      add('factsInserted', snapshots.count)
      const holdingRows = insertHoldingRelations(
        output, episodeId, options.botId, date, rows.snapshots, fundDbArtifactId, now,
      )
      add('factsInserted', holdingRows.facts)
      add('relationsInserted', holdingRows.relations)
      add('relationsInserted', insertFundRelations(output, episodeId, rows.funds, fundDbArtifactId, now))

      const metrics = extractAgentMetrics(parsed)
      for (const metric of metrics) {
        add('factsInserted', insertFact(output, {
          episodeId, subject: metric.subject, predicate: metric.predicate,
          valueNumber: metric.value, unit: metric.unit, eventTime: date, availableAt: date,
          factClass: 'agent_reported_observation', certainty: 'agent_asserted',
          sourceArtifactId: replyArtifactId, sourceLocator: metric.sourceLocator,
          rawExcerpt: metric.rawExcerpt, now,
        }))
      }

      const claimResult = insertClaimsAndEvidence(output, episodeId, parsed, replyArtifactId, date, now)
      add('claimsInserted', claimResult.claims)
      add('evidenceInserted', claimResult.evidence)
      if (claimResult.claims === 0) {
        output.prepare("UPDATE episodes SET extraction_status='partial' WHERE episode_id=?").run(episodeId)
        add('qualityIssuesInserted', insertIssue(output, {
          episodeId, severity: 'warning', issueType: 'no_claims_extracted', fieldPath: 'claims',
          description: 'No structured claim could be recovered from the final reply; manual review is required.',
          observed: { reply_length: pickFinalReply(parsed).length }, sourceArtifactId: replyArtifactId,
          sourceLocator: 'reply.json#/reply', now,
        }))
      }
      add('conflictsInserted', insertConflicts(
        output, episodeId, parsed, candidates, metrics, replyArtifactId, now,
      ))

      add('qualityIssuesInserted', insertIssue(output, {
        episodeId, severity: 'info', issueType: 'decision_time_date_only', fieldPath: 'episode.decision_time',
        description: 'The session establishes the world date but not one canonical local decision timestamp.',
        observed: { trade_date: date }, sourceArtifactId: replyArtifactId,
        sourceLocator: 'reply.json#/session_id', now,
      }))
      for (const artifact of artifacts.filter(item => item.integrity === 'missing')) {
        add('qualityIssuesInserted', insertIssue(output, {
          episodeId, severity: 'warning', issueType: 'missing_source_artifact', fieldPath: `sources.${artifact.kind}`,
          description: `Expected source artifact is missing: ${artifact.path}`, observed: artifact,
          sourceArtifactId: artifactIds.get(artifact.kind) ?? null, sourceLocator: artifact.path, now,
        }))
      }
      if (snapshots.mismatches.length) {
        add('qualityIssuesInserted', insertIssue(output, {
          episodeId, severity: 'warning', issueType: 'snapshot_timing_mismatch', fieldPath: 'facts.portfolio_snapshot',
          description: 'Session close snapshot and settled database snapshot differ; both are preserved and must not be merged.',
          observed: snapshots.mismatches, sourceArtifactId: closeArtifactId,
          sourceLocator: 'close_my_day.json#/asset_allocation_pct', now,
        }))
      }
      if (claimResult.emptyMechanisms || claimResult.emptyFalsifiers || claimResult.unverifiedEvidence) {
        add('qualityIssuesInserted', insertIssue(output, {
          episodeId, severity: 'warning', issueType: 'claim_structure_incomplete', fieldPath: 'claims',
          description: 'Some claims lack explicit mechanisms, falsifiers, or source-record-level evidence bindings.',
          observed: claimResult, sourceArtifactId: replyArtifactId, sourceLocator: 'reply.json#/reply', now,
        }))
      }
      if (candidateResult.reconciledTransportErrors.length) {
        add('qualityIssuesInserted', insertIssue(output, {
          episodeId, severity: 'info', issueType: 'transport_error_database_reconciled', fieldPath: 'candidate_actions',
          description: 'A tool call reported a transport error but a matching order exists in fund.db.',
          observed: candidateResult.reconciledTransportErrors.map(item => item.toolCallId),
          sourceArtifactId: replyArtifactId, sourceLocator: 'reply.json#/messages', now,
        }))
      }
      if (close && Array.isArray(close.holdings)) {
        const invalidHoldingDays = close.holdings.filter(item => {
          const days = asNumber((item as { holding_days?: unknown } | null)?.holding_days)
          return days !== null && days < 0
        }).length
        if (invalidHoldingDays) {
          add('qualityIssuesInserted', insertIssue(output, {
            episodeId, severity: 'warning', issueType: 'invalid_historical_holding_rows', fieldPath: 'close_my_day.holdings',
            description: 'The close snapshot contains historical holding rows with negative holding_days; they were not normalized as current positions.',
            observed: { invalid_holding_rows: invalidHoldingDays }, sourceArtifactId: closeArtifactId,
            sourceLocator: 'close_my_day.json#/holdings', now,
          }))
        }
      }
    }
    output.exec('COMMIT')
    const summary = Object.fromEntries(Object.entries(result).filter(([key]) => !['outputDbPath', 'reportPath'].includes(key)))
    output.prepare(`
      UPDATE import_batches SET status='completed',summary_json=?,completed_at=? WHERE batch_id=?
    `).run(json(summary), now, batchId)
    writeReport(output, options, result)
  } catch (error) {
    try { output.exec('ROLLBACK') } catch { /* no active transaction */ }
    output.prepare(`
      UPDATE import_batches SET status='failed',summary_json=?,completed_at=? WHERE batch_id=?
    `).run(json({ error: error instanceof Error ? error.message : String(error) }), now, batchId)
    throw error
  } finally {
    fundDb.close()
    output.close()
  }
  return result
}

function option(args: string[], name: string): string | undefined {
  const index = args.indexOf(name)
  return index >= 0 ? args[index + 1] : undefined
}

function cli(): void {
  const args = process.argv.slice(2)
  const runId = option(args, '--run-id')
  const botId = option(args, '--bot-id')
  if (!runId || !botId) {
    throw new Error('Usage: import.ts --run-id RUN --bot-id BOT [--start YYYY-MM-DD] [--end YYYY-MM-DD]')
  }
  const worldRoot = resolve(option(args, '--world-root') ?? process.cwd())
  const outputDbPath = resolve(option(args, '--output') ?? join(worldRoot, 'runtime', 'decision-episodes', 'decision-episodes-v1.db'))
  const reportPath = resolve(option(args, '--report') ?? join(worldRoot, 'runtime', 'decision-episodes', `batch-${runId}-${botId}.md`))
  const result = importDecisionEpisodes({
    worldRoot,
    fundDbPath: resolve(option(args, '--fund-db') ?? join(worldRoot, '..', 'data', 'fund.db')),
    outputDbPath,
    reportPath,
    runId,
    botId,
    startDate: option(args, '--start'),
    endDate: option(args, '--end'),
  })
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) cli()
