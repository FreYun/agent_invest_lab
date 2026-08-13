import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { DatabaseSync, type SQLInputValue } from 'node:sqlite'
import { fileURLToPath } from 'node:url'
import { buildCanonicalDecisionDatabase } from './canonical-aug05.ts'

type Row = Record<string, unknown>
type Candidate = Record<string, any>

const ORIGINAL_RUN_ID = 'bot105d-daily-agenticdeep-charter-0424'
const ORIGINAL_BOT_ID = 'bot105d'
const MANUAL_DATES = new Set(['2026-08-05', '2026-08-06', '2026-08-07', '2026-08-10', '2026-08-11', '2026-08-12'])
const json = (value: unknown): string => JSON.stringify(value)

function option(name: string): string | undefined {
  const index = process.argv.indexOf(name)
  return index >= 0 ? process.argv[index + 1] : undefined
}

function rows(db: DatabaseSync, sql: string, ...params: SQLInputValue[]): Row[] {
  return db.prepare(sql).all(...params) as Row[]
}

function valueOfFact(row: Row): unknown {
  if (row.value_number !== null && row.value_number !== undefined) return row.value_number
  if (row.value_text !== null && row.value_text !== undefined) return row.value_text
  if (row.object_text !== null && row.object_text !== undefined) return row.object_text
  return null
}

function chineseMechanism(predicate: unknown, mechanism: unknown): string {
  if (predicate === 'TRACKS_INDEX') return '基金基础资料显示该基金通过指数复制或联接方式获得对应指数暴露。'
  if (predicate === 'OWNS') return '数据库日终持仓快照确认该组合在当日持有该基金。'
  return typeof mechanism === 'string' && mechanism.trim() ? mechanism : '原始关系记录未提供更具体的作用机制。'
}

function actionDecision(orders: Row[]): string {
  const actions = new Set(orders.map(row => String(row.action_type)))
  if (actions.has('BUY') && actions.has('SELL')) return 'rebalance_portfolio'
  if (actions.has('BUY')) return 'buy_funds'
  if (actions.has('SELL')) return 'sell_funds'
  return 'hold_no_new_order'
}

function parsePayload(value: unknown): Row {
  try { return JSON.parse(String(value)) as Row } catch { return {} }
}

function sha256File(path: string): string {
  return createHash('sha256').update(readFileSync(path)).digest('hex')
}

function appendAutomaticCases(outputPath: string, rulesPath: string, qwenDir: string, createdAt: string, runId: string, botId: string): void {
  const output = new DatabaseSync(outputPath)
  const rules = new DatabaseSync(rulesPath, { readOnly: true })
  try {
    const episodes = rows(rules, `
      SELECT * FROM latest_episodes
      WHERE run_id=? AND bot_id=?
      ORDER BY trade_date
    `, runId, botId)
    output.exec('BEGIN IMMEDIATE')
    const insertCase = output.prepare('INSERT INTO canonical_cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)')
    const insertSource = output.prepare('INSERT INTO canonical_sources VALUES (?,?,?,?,?,?)')
    const insertContext = output.prepare('INSERT INTO canonical_contexts VALUES (?,?,?)')
    const insertFact = output.prepare('INSERT INTO canonical_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)')
    const insertRelation = output.prepare('INSERT INTO canonical_relations VALUES (?,?,?,?,?,?,?,?,?,?,?)')
    const insertClaim = output.prepare('INSERT INTO canonical_claims VALUES (?,?,?,?,?,?,?,?,?,?,?)')
    const insertConflict = output.prepare('INSERT INTO canonical_conflicts VALUES (?,?,?,?,?,?,?,?)')
    const insertCandidateAction = output.prepare('INSERT INTO canonical_candidate_actions VALUES (?,?,?,?,?,?,?,?,?,?)')
    const insertDecision = output.prepare('INSERT INTO canonical_final_decisions VALUES (?,?,?,?)')
    const insertOrder = output.prepare('INSERT INTO canonical_actual_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)')
    const insertWarning = output.prepare('INSERT INTO canonical_warnings VALUES (?,?,?)')

    for (const episode of episodes) {
      const date = String(episode.trade_date)
      if (runId === ORIGINAL_RUN_ID && botId === ORIGINAL_BOT_ID && MANUAL_DATES.has(date)) continue
      const qwenCaseId = `${botId}_${date}`
      const caseId = runId === ORIGINAL_RUN_ID && botId === ORIGINAL_BOT_ID ? qwenCaseId : `${botId}:${runId}:${date}`
      const episodeId = String(episode.episode_id)
      const candidatePath = resolve(qwenDir, `${qwenCaseId}.validated.candidate.json`)
      const validationPath = resolve(qwenDir, `${qwenCaseId}.validated.validation.json`)
      if (!existsSync(candidatePath) || !existsSync(validationPath)) throw new Error(`validated Qwen candidate missing: ${caseId}`)
      const validation = JSON.parse(readFileSync(validationPath, 'utf8')) as { valid?: boolean }
      if (validation.valid !== true) throw new Error(`Qwen validation is not valid: ${caseId}`)
      const envelope = JSON.parse(readFileSync(candidatePath, 'utf8')) as { candidate?: Candidate }
      const candidate = envelope.candidate
      if (!candidate || candidate.case_id !== qwenCaseId) throw new Error(`Qwen case_id mismatch: ${caseId}`)
      const claims = Array.isArray(candidate.claims) ? candidate.claims : []
      if (!claims.length) throw new Error(`Qwen candidate has no claims: ${caseId}`)

      const decisionSummary = String(claims[0].statement)
      insertCase.run(
        caseId, 'decision_episode_v0.1', 'canonical_hybrid_batch_validated', botId, runId,
        String(episode.session_id ?? 'unknown'), date, null,
        'session只可靠保存交易日，没有统一的本地决策时间戳', date, '禁止访问未来世界日期',
        decisionSummary, String(episode.source_bundle_hash), createdAt,
      )

      const sources = rows(rules, 'SELECT * FROM source_artifacts WHERE episode_id=? ORDER BY artifact_kind', episodeId)
      for (const source of sources) {
        insertSource.run(
          source.artifact_id, caseId, source.artifact_kind, source.source_path, source.sha256,
          json(source.artifact_kind === 'portfolio_database'
            ? ['fund_bot_daily_snapshots', 'fund_bot_actions', 'fund_bot_orders', 'fund_info'] : []),
        )
      }
      insertSource.run(`${caseId}_SRC_QWEN`, caseId, 'qwen_candidate', candidatePath, sha256File(candidatePath), json([]))

      const factRows = rows(rules, 'SELECT * FROM facts WHERE episode_id=? ORDER BY fact_id', episodeId)
      const accountFacts = factRows
        .filter(fact => ['CASH', 'CASH_AVAILABLE', 'EQUITY_WEIGHT', 'CASH_WEIGHT', 'TOTAL_VALUE', 'CUMULATIVE_RETURN', 'DAILY_RETURN', 'MAX_DRAWDOWN'].includes(String(fact.predicate)))
        .map(fact => ({ predicate: fact.predicate, value: valueOfFact(fact), unit: fact.unit, source: fact.source_locator }))
      insertContext.run(caseId, json({ source: '规则事实库', snapshotFacts: accountFacts }), json([
        { constraintId: 'pit_cutoff', rule: '只能使用当前世界日期及此前可得信息', class: 'hard_process_constraint' },
        { constraintId: 'database_execution_truth', rule: '订单和账户动作以 fund.db 记录为最终事实', class: 'hard_process_constraint' },
        { constraintId: 'producer_separation', rule: '不同生产者的冲突数值必须分别保留，不得平均或静默合并', class: 'hard_process_constraint' },
      ]))

      for (const fact of factRows) {
        insertFact.run(
          fact.fact_id, caseId, fact.subject, fact.predicate, json(valueOfFact(fact)), fact.unit,
          fact.event_time, fact.certainty === 'agent_asserted' ? 'agent_reply' : 'rules_and_database',
          fact.fact_class, fact.source_locator,
          fact.certainty === 'hard_recorded' || fact.certainty === 'hard_reference' ? 'yes' : 'unknown',
          json({
            certainty: fact.certainty, objectText: fact.object_text, availableAt: fact.available_at,
            validFrom: fact.valid_from, validTo: fact.valid_to, sourceRecordHash: fact.source_record_hash,
            rawExcerpt: fact.raw_excerpt,
          }),
        )
      }

      const ruleRelations = rows(rules, 'SELECT * FROM relations WHERE episode_id=? ORDER BY relation_id', episodeId)
      for (const relation of ruleRelations) {
        insertRelation.run(
          relation.relation_id, caseId, relation.subject, relation.predicate, relation.object,
          relation.direction, chineseMechanism(relation.predicate, relation.mechanism),
          relation.valid_from, relation.valid_to,
          json([{ source: relation.source_locator, excerpt: relation.raw_excerpt }]), relation.certainty,
        )
      }
      for (let index = 0; index < (candidate.relation_candidates ?? []).length; index += 1) {
        const relation = candidate.relation_candidates[index]
        insertRelation.run(
          `${caseId}_QR${index + 1}`, caseId, relation.subject, relation.predicate, relation.object,
          relation.direction, relation.mechanism, date, null,
          json([{ source: relation.source_locator, quote: relation.quote }]), relation.certainty,
        )
      }

      for (let index = 0; index < claims.length; index += 1) {
        const claim = claims[index]
        const supporting = (claim.supporting_evidence ?? []).map((item: Candidate) => ({
          relation: 'SUPPORTS', source: item.source_locator, quote: item.quote, explanation: item.explanation,
        }))
        const contradicting = (claim.contradicting_evidence ?? []).map((item: Candidate) => ({
          relation: 'CONTRADICTS', source: item.source_locator, quote: item.quote, explanation: item.explanation,
        }))
        insertClaim.run(
          `${caseId}_QC${index + 1}`, caseId, claim.statement, claim.claim_type, claim.status,
          claim.confidence_label, json((claim.mechanism ?? []).map((item: Candidate) => item.step)),
          json(supporting), json(contradicting), json((claim.falsifiers ?? []).map((item: Candidate) => item.condition)),
          claim.certainty,
        )
      }

      for (let index = 0; index < (candidate.conflicts ?? []).length; index += 1) {
        const conflict = candidate.conflicts[index]
        insertConflict.run(
          `${caseId}_QDC${index + 1}`, caseId, json(conflict.left), json(conflict.right),
          conflict.winner, conflict.resolution, conflict.resolution_class, conflict.unresolved_issue,
        )
      }

      const candidateActions = rows(rules, 'SELECT * FROM candidate_actions WHERE episode_id=? ORDER BY candidate_action_id', episodeId)
      for (const action of candidateActions) {
        const accepted = action.reconciled_outcome === 'database_confirmed' || action.observed_tool_outcome === 'accepted'
        insertCandidateAction.run(
          action.candidate_action_id, caseId, `${action.action_type}${action.instrument ? ` ${action.instrument}` : ''}`,
          action.requested_amount, action.requested_shares,
          accepted ? 'accepted' : action.observed_tool_outcome, null, action.tool_call_id, action.reason,
          `工具结果=${action.observed_tool_outcome}；数据库对账=${action.reconciled_outcome}`,
        )
      }

      const orderRows = rows(rules, "SELECT * FROM final_actions WHERE episode_id=? AND source_table='fund_bot_orders' ORDER BY CAST(source_primary_key AS INTEGER)", episodeId)
      const equityFacts = factRows.filter(fact => fact.predicate === 'EQUITY_WEIGHT').map(fact => ({ value: valueOfFact(fact), unit: fact.unit, source: fact.source_locator }))
      const cashFacts = factRows.filter(fact => fact.predicate === 'CASH_WEIGHT').map(fact => ({ value: valueOfFact(fact), unit: fact.unit, source: fact.source_locator }))
      insertDecision.run(caseId, actionDecision(orderRows), json({ equityWeightObservations: equityFacts, cashWeightObservations: cashFacts }), json(claims.map(claim => claim.statement)))

      for (const order of orderRows) {
        const payload = parsePayload(order.payload_json)
        insertOrder.run(
          String(order.source_primary_key), caseId, order.action_type, order.instrument,
          payload.order_amount ?? (order.action_type === 'BUY' ? order.amount : null),
          payload.order_shares ?? (order.action_type === 'SELL' ? order.shares : null),
          payload.confirmed_amount ?? (order.action_type === 'SELL' ? order.amount : null),
          payload.confirmed_shares ?? (order.action_type === 'BUY' ? order.shares : null),
          Number(order.fee ?? 0), 'unknown_at_decision', order.status ?? 'unknown',
        )
      }

      const warnings: string[] = []
      for (const issue of rows(rules, 'SELECT * FROM quality_issues WHERE episode_id=? ORDER BY issue_id', episodeId)) {
        warnings.push(`${issue.issue_type}: ${issue.description}`)
      }
      for (const unknown of candidate.unknowns ?? []) warnings.push(`Qwen待确认：${unknown}`)
      if (!orderRows.length) warnings.push('当日无数据库确认的新订单；HOLD仅表示维持组合，不等同于成交。')
      warnings.forEach((warning, index) => insertWarning.run(`${caseId}_W${index + 1}`, caseId, warning))
    }
    output.prepare("INSERT OR REPLACE INTO canonical_meta(key,value) VALUES (?,?)").run(`coverage:${runId}:${botId}`, json({ runId, botId, canonicalCases: episodes.length, dateStart: episodes[0]?.trade_date ?? null, dateEnd: episodes.at(-1)?.trade_date ?? null }))
    output.exec('COMMIT')
  } catch (error) {
    try { output.exec('ROLLBACK') } catch { /* transaction may not have started */ }
    throw error
  } finally {
    rules.close()
    output.close()
  }
}

const here = dirname(fileURLToPath(import.meta.url))
const outputPath = resolve(option('--output') ?? resolve(here, '../../runtime/decision-episodes/decision-episodes-v2.db'))
const rulesPath = resolve(option('--rules-db') ?? resolve(here, '../../runtime/decision-episodes/decision-episodes-rules-full.db'))
const qwenDir = resolve(option('--qwen-dir') ?? resolve(here, '../../runtime/decision-episodes/qwen3.7-plus'))
const createdAt = option('--created-at') ?? new Date().toISOString()
const runId = option('--run-id') ?? ORIGINAL_RUN_ID
const botId = option('--bot-id') ?? ORIGINAL_BOT_ID
const append = process.argv.includes('--append')
if (!existsSync(rulesPath)) throw new Error(`rules database not found: ${rulesPath}`)
if (append) {
  if (!existsSync(outputPath)) throw new Error(`canonical database not found for append: ${outputPath}`)
} else {
  buildCanonicalDecisionDatabase(outputPath, createdAt)
}
appendAutomaticCases(outputPath, rulesPath, qwenDir, createdAt, runId, botId)
process.stdout.write(`${append ? 'appended to' : 'created'} canonical database: ${outputPath} (${runId} / ${botId})\n`)
