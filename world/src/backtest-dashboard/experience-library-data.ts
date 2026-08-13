import { existsSync } from 'node:fs'
import { DatabaseSync } from 'node:sqlite'

type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }
type SqlRow = Record<string, unknown>

const JSON_FIELDS = new Set([
  'decision_context_json', 'mechanism_json', 'falsifiers_json', 'alternative_explanations_json',
  'applicability_json', 'persona_scope_json', 'action_policy_json',
  'provenance_json', 'temporal_json', 'distribution_policy_json',
  'governance_json', 'persona_json', 'sections_json', 'account_snapshot_json',
  'actual_actions_json', 'actual_orders_json', 'effect_json', 'review_json',
  'overlap_json', 'discriminating_evidence_json',
])

function parseJson(value: unknown): JsonValue | unknown {
  if (typeof value !== 'string') return value
  try { return JSON.parse(value) as JsonValue }
  catch { return value }
}

function normalizeRow(row: SqlRow): SqlRow {
  const out: SqlRow = {}
  for (const [key, value] of Object.entries(row)) {
    const cleanKey = key.endsWith('_json') ? key.slice(0, -5) : key
    if (JSON_FIELDS.has(key)) out[cleanKey] = parseJson(value)
    else if (key.endsWith('_verified') || key === 'counts_as_independent_episode') out[cleanKey] = Boolean(value)
    else out[cleanKey] = value
  }
  return out
}

function unavailable(dbPath: string, reason: string): Record<string, unknown> {
  return {
    available: false,
    reason,
    source: { filename: dbPath.split('/').pop() ?? dbPath, readOnly: true },
    summary: {
      cases: 0, cards: 0, verifiedCards: 0, evidence: 0,
      unresolvedConflicts: 0, personas: 0, snapshots: 0,
    },
    cards: [], cases: [], personas: [], filters: { statuses: [], types: [], topics: [], bots: [], runs: [] },
  }
}

function openReadOnly(dbPath: string): DatabaseSync | null {
  if (!existsSync(dbPath)) return null
  return new DatabaseSync(dbPath, { readOnly: true })
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

/** Exact, deliberately minimized shape planned for agent consumption. */
function pickFields(row: unknown, fields: string[]): Record<string, unknown> {
  const source = objectValue(row)
  return Object.fromEntries(fields.map(field => [field, source[field] ?? null]))
}

/**
 * This is a whitelist projection of the frozen source case. It avoids feeding
 * system-authored claims, matches or quality judgements back to the consumer
 * Agent as if they were observations from the original episode.
 */
export function buildAgentPayloadPreview(
  card: SqlRow,
  sources: SqlRow[],
): Record<string, unknown> {
  const mechanism = objectValue(card.mechanism)
  const lesson = typeof mechanism.lesson === 'string' ? mechanism.lesson.trim() : ''
  const failurePattern = typeof mechanism.failure_pattern === 'string' ? mechanism.failure_pattern.trim() : ''
  const betterProcess = arrayValue(mechanism.better_process)
  const applicableWhen = arrayValue(mechanism.applicable_when)
  const invalidWhen = arrayValue(mechanism.invalid_when)
  const actualAction = typeof mechanism.actual_action === 'string' ? mechanism.actual_action.trim() : ''
  const laterOutcome = typeof mechanism.later_outcome === 'string' ? mechanism.later_outcome.trim() : ''
  const outcomeAvailableAt = typeof mechanism.outcome_available_at === 'string' ? mechanism.outcome_available_at : null
  const missing = [
    !lesson && 'lesson',
    !failurePattern && 'failure_pattern',
    betterProcess.length === 0 && 'better_process',
    applicableWhen.length === 0 && 'applicable_when',
    invalidWhen.length === 0 && 'invalid_when',
    !actualAction && 'actual_action',
    !laterOutcome && 'later_outcome',
    !outcomeAvailableAt && 'outcome_available_at',
  ].filter((field): field is string => Boolean(field))

  if (card.card_type !== 'lesson_candidate' || missing.length > 0) {
    return {
      delivery_state: 'withheld_not_reusable_experience',
      material_type: card.card_type === 'market_hypothesis' ? 'market_observation' : 'decision_rule_material',
      reason: '该记录只有当时观点或规则，没有形成完整、可迁移的复盘经验，因此不会提供给 Agent。',
      missing_for_lesson: missing.length > 0 ? missing : [
        'failure_pattern', 'better_process', 'applicable_when',
        'invalid_when', 'actual_action', 'later_outcome',
      ],
    }
  }

  const source = sources[0] ?? {}
  const actionPolicy = objectValue(card.action_policy)
  const temporal = objectValue(card.temporal)
  return {
    delivery_state: 'candidate_research_only',
    knowledge_cutoff: source.market_data_cutoff ?? temporal.market_data_cutoff ?? null,
    experience: {
      lesson,
      failure_pattern: failurePattern,
      better_process: betterProcess,
      applicable_when: applicableWhen,
      invalid_when: invalidWhen,
      maturity: 'single_case_hypothesis',
    },
    supporting_case: {
      decision_date: source.trade_date ?? null,
      actual_action: actualAction,
      later_outcome: laterOutcome,
      outcome_available_at: outcomeAvailableAt,
      original_excerpts: sources.slice(0, 3).map(row => ({
        section: row.source_section ?? null,
        text: row.source_text ?? null,
      })),
    },
    system_guard: {
      authority: actionPolicy.authority ?? 'research_only',
      max_position_impact_pct: Number(actionPolicy.max_position_impact_pct ?? 0),
      allowed_use: 'research_hypothesis_only',
    },
  }
}

/**
 * Read-only catalog used by the experience-library browser. It deliberately
 * returns quality gates alongside every card so a candidate cannot look like
 * a promoted trading rule merely because it appears in the library.
 */
export function loadExperienceLibraryCatalog(dbPath: string): Record<string, unknown> {
  const db = openReadOnly(dbPath)
  if (!db) return unavailable(dbPath, 'experience library database not found')
  try {
    const cards = (db.prepare(`
      SELECT c.*,
        COALESCE((SELECT COUNT(*) FROM experience_card_sources s
          WHERE s.card_id=c.card_id AND s.card_version=c.version), 0) AS source_count,
        COALESCE((SELECT COUNT(*) FROM experience_evidence e
          WHERE e.card_id=c.card_id AND e.card_version=c.version), 0) AS evidence_count,
        COALESCE((SELECT SUM(e.relation='supporting') FROM experience_evidence e
          WHERE e.card_id=c.card_id AND e.card_version=c.version), 0) AS supporting_count,
        COALESCE((SELECT SUM(e.relation='contradicting') FROM experience_evidence e
          WHERE e.card_id=c.card_id AND e.card_version=c.version), 0) AS contradicting_count,
        COALESCE((SELECT SUM(e.relation='inconclusive') FROM experience_evidence e
          WHERE e.card_id=c.card_id AND e.card_version=c.version), 0) AS inconclusive_count,
        COALESCE((SELECT SUM(e.counts_as_independent_episode) FROM experience_evidence e
          WHERE e.card_id=c.card_id AND e.card_version=c.version), 0) AS independent_episode_count,
        COALESCE((SELECT SUM(e.pit_verified) FROM experience_evidence e
          WHERE e.card_id=c.card_id AND e.card_version=c.version), 0) AS pit_verified_evidence_count,
        COALESCE((SELECT SUM(e.outcome_verified) FROM experience_evidence e
          WHERE e.card_id=c.card_id AND e.card_version=c.version), 0) AS outcome_verified_evidence_count,
        COALESCE((SELECT COUNT(*) FROM experience_conflicts x
          WHERE (x.left_card_id=c.card_id AND x.left_card_version=c.version)
             OR (x.right_card_id=c.card_id AND x.right_card_version=c.version)), 0) AS conflict_count,
        (SELECT MIN(ec.trade_date) FROM experience_card_sources s
          JOIN experience_cases ec ON ec.case_id=s.case_id
          WHERE s.card_id=c.card_id AND s.card_version=c.version) AS source_date,
        (SELECT ec.bot_id FROM experience_card_sources s
          JOIN experience_cases ec ON ec.case_id=s.case_id
          WHERE s.card_id=c.card_id AND s.card_version=c.version LIMIT 1) AS source_bot_id,
        (SELECT ec.run_id FROM experience_card_sources s
          JOIN experience_cases ec ON ec.case_id=s.case_id
          WHERE s.card_id=c.card_id AND s.card_version=c.version LIMIT 1) AS source_run_id
      FROM experience_card_versions c
      ORDER BY source_date DESC, c.created_at DESC, c.card_id
    `).all() as SqlRow[]).map(normalizeRow)

    const cases = (db.prepare(`
      SELECT ec.case_id,ec.episode_id,ec.run_id,ec.bot_id,ec.trade_date,ec.decision_at,
        ec.market_data_cutoff,ec.data_vintage,ec.persona_snapshot_id,ec.summary,
        ec.source_verified,ec.pit_verified,ec.execution_verified,ec.outcome_verified,
        ec.imported_at,p.persona_title,p.strategy_family,
        COUNT(DISTINCT s.card_id || ':' || s.card_version) AS card_count
      FROM experience_cases ec
      JOIN agent_persona_snapshots p ON p.persona_snapshot_id=ec.persona_snapshot_id
      LEFT JOIN experience_card_sources s ON s.case_id=ec.case_id
      GROUP BY ec.case_id
      ORDER BY ec.trade_date DESC,ec.case_id
    `).all() as SqlRow[]).map(normalizeRow)

    const personas = (db.prepare(`
      SELECT p.*,COUNT(DISTINCT ec.case_id) AS case_count,
        COUNT(DISTINCT s.card_id || ':' || s.card_version) AS card_count
      FROM agent_persona_snapshots p
      LEFT JOIN experience_cases ec ON ec.persona_snapshot_id=p.persona_snapshot_id
      LEFT JOIN experience_card_sources s ON s.case_id=ec.case_id
      GROUP BY p.persona_snapshot_id
      ORDER BY p.captured_at DESC
    `).all() as SqlRow[]).map(normalizeRow)

    const count = (sql: string): number => Number((db.prepare(sql).get() as { n: number }).n)
    const schemaVersion = (db.prepare("SELECT value FROM library_meta WHERE key='schema_version'").get() as { value?: string } | undefined)?.value ?? null
    const statuses = [...new Set(cards.map(c => String(c.status ?? '')).filter(Boolean))].sort()
    const types = [...new Set(cards.map(c => String(c.card_type ?? '')).filter(Boolean))].sort()
    const topics = [...new Set(cards.flatMap(c => {
      const a = c.applicability as { topics?: unknown } | undefined
      return Array.isArray(a?.topics) ? a.topics.map(String) : []
    }))].sort()

    const verifiedCards = cards.filter(card => {
      const status = String(card.status ?? '')
      return status === 'validated' || status === 'promoted' || status === 'active'
    }).length
    const dateRange = db.prepare('SELECT MIN(trade_date) min_date,MAX(trade_date) max_date FROM experience_cases').get() as SqlRow
    return {
      available: true,
      source: { filename: dbPath.split('/').pop() ?? dbPath, readOnly: true, schemaVersion },
      summary: {
        cases: count('SELECT COUNT(*) n FROM experience_cases'),
        cards: cards.length,
        verifiedCards,
        evidence: count('SELECT COUNT(*) n FROM experience_evidence'),
        unresolvedConflicts: count("SELECT COUNT(*) n FROM experience_conflicts WHERE status='unresolved'"),
        personas: personas.length,
        snapshots: count('SELECT COUNT(*) n FROM library_snapshots'),
        dateFrom: dateRange.min_date ?? null,
        dateTo: dateRange.max_date ?? null,
      },
      cards, cases, personas,
      filters: {
        statuses, types, topics,
        bots: [...new Set(cases.map(c => String(c.bot_id)))].sort(),
        runs: [...new Set(cases.map(c => String(c.run_id)))].sort(),
      },
    }
  } catch (error) {
    return unavailable(dbPath, error instanceof Error ? error.message : String(error))
  } finally {
    db.close()
  }
}

export function loadExperienceLibraryCard(dbPath: string, cardId: string, version: number): Record<string, unknown> | null {
  const db = openReadOnly(dbPath)
  if (!db) return null
  try {
    const rawCard = db.prepare('SELECT * FROM experience_card_versions WHERE card_id=? AND version=?').get(cardId, version) as SqlRow | undefined
    if (!rawCard) return null
    const sources = (db.prepare(`
      SELECT s.source_section,s.source_text,s.source_role,
        ec.*,p.persona_title,p.strategy_id,p.strategy_title,p.strategy_family,p.objective,
        p.risk_profile,p.rebalance_cadence,p.agent_lineage_id,p.persona_json
      FROM experience_card_sources s
      JOIN experience_cases ec ON ec.case_id=s.case_id
      JOIN agent_persona_snapshots p ON p.persona_snapshot_id=ec.persona_snapshot_id
      WHERE s.card_id=? AND s.card_version=?
      ORDER BY ec.trade_date,s.source_section
    `).all(cardId, version) as SqlRow[]).map(normalizeRow)
    const evidence = (db.prepare(`
      SELECT e.*,ec.trade_date,ec.bot_id,ec.run_id
      FROM experience_evidence e JOIN experience_cases ec ON ec.case_id=e.case_id
      WHERE e.card_id=? AND e.card_version=? ORDER BY e.created_at,e.evidence_id
    `).all(cardId, version) as SqlRow[]).map(normalizeRow)
    const conflicts = (db.prepare(`
      SELECT x.*,
        lc.title AS left_title,rc.title AS right_title
      FROM experience_conflicts x
      JOIN experience_card_versions lc ON lc.card_id=x.left_card_id AND lc.version=x.left_card_version
      JOIN experience_card_versions rc ON rc.card_id=x.right_card_id AND rc.version=x.right_card_version
      WHERE (x.left_card_id=? AND x.left_card_version=?)
         OR (x.right_card_id=? AND x.right_card_version=?)
      ORDER BY x.detected_at,x.conflict_id
    `).all(cardId, version, cardId, version) as SqlRow[]).map(normalizeRow)
    const card = normalizeRow(rawCard)
    return { card, sources, evidence, conflicts, agent_payload_preview: buildAgentPayloadPreview(card, sources) }
  } finally {
    db.close()
  }
}
