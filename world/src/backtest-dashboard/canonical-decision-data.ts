import { existsSync } from 'node:fs'
import { DatabaseSync } from 'node:sqlite'

type Row = Record<string, unknown>

function parseJson(value: unknown): unknown {
  if (typeof value !== 'string') return value
  try { return JSON.parse(value) as unknown } catch { return value }
}

function normalize(row: Row): Row {
  const out: Row = {}
  for (const [key, value] of Object.entries(row)) {
    out[key.endsWith('_json') ? key.slice(0, -5) : key] = key.endsWith('_json') ? parseJson(value) : value
  }
  return out
}

function unavailable(dbPath: string, reason: string): Row {
  return {
    available: false, reason, canonical: true,
    source: { path: dbPath, readOnly: true },
    summary: { episodes: 0, facts: 0, relations: 0, claims: 0, evidence: 0, conflicts: 0, warnings: 0 },
    episodes: [], filters: { runs: [], bots: [], statuses: [] },
  }
}

export function loadDecisionEpisodeCatalog(dbPath: string): Row {
  if (!existsSync(dbPath)) return unavailable(dbPath, 'canonical decision database not found')
  const db = new DatabaseSync(dbPath, { readOnly: true })
  try {
    const count = (table: string): number => Number((db.prepare(`SELECT COUNT(*) n FROM ${table}`).get() as { n: number }).n)
    const episodes = (db.prepare(`
      SELECT c.case_id AS episode_id, c.case_id AS logical_episode_key, c.source_bundle_hash,
        c.run_id, c.bot_id, c.world_date AS trade_date, c.session_id, c.decision_time,
        CASE WHEN c.decision_time IS NULL THEN 'date_only' ELSE 'exact' END AS decision_time_precision,
        c.knowledge_cutoff, c.decision_summary AS final_summary,
        c.extraction_status, c.schema_version AS extractor_version, c.created_at AS imported_at,
        (SELECT COUNT(*) FROM canonical_facts f WHERE f.case_id=c.case_id) AS fact_count,
        (SELECT COUNT(*) FROM canonical_relations r WHERE r.case_id=c.case_id) AS relation_count,
        (SELECT COUNT(*) FROM canonical_claims x WHERE x.case_id=c.case_id) AS claim_count,
        (SELECT SUM(json_array_length(x.supporting_evidence_json) + json_array_length(x.contradicting_evidence_json)) FROM canonical_claims x WHERE x.case_id=c.case_id) AS evidence_count,
        (SELECT COUNT(*) FROM canonical_candidate_actions a WHERE a.case_id=c.case_id) AS candidate_action_count,
        (SELECT COUNT(*) FROM canonical_actual_orders a WHERE a.case_id=c.case_id) AS final_action_count,
        (SELECT COUNT(*) FROM canonical_conflicts x WHERE x.case_id=c.case_id) AS conflict_count,
        (SELECT COUNT(*) FROM canonical_warnings w WHERE w.case_id=c.case_id) AS issue_count,
        (SELECT COUNT(*) FROM canonical_warnings w WHERE w.case_id=c.case_id) AS warning_count
      FROM canonical_cases c ORDER BY c.world_date DESC
    `).all() as Row[]).map(normalize)
    const version = (db.prepare("SELECT value FROM canonical_meta WHERE key='schema_version'").get() as { value?: string } | undefined)?.value ?? null
    return {
      available: true, canonical: true,
      source: { path: dbPath, filename: dbPath.split('/').pop(), readOnly: true, schemaVersion: version },
      summary: {
        episodes: episodes.length, facts: count('canonical_facts'), relations: count('canonical_relations'), claims: count('canonical_claims'),
        evidence: Number((db.prepare('SELECT COALESCE(SUM(json_array_length(supporting_evidence_json) + json_array_length(contradicting_evidence_json)),0) n FROM canonical_claims').get() as { n: number }).n),
        conflicts: count('canonical_conflicts'), warnings: count('canonical_warnings'),
      },
      episodes,
      filters: {
        runs: [...new Set(episodes.map(row => String(row.run_id)))],
        bots: [...new Set(episodes.map(row => String(row.bot_id)))],
        statuses: [...new Set(episodes.map(row => String(row.extraction_status)))],
      },
    }
  } catch (error) {
    return unavailable(dbPath, error instanceof Error ? error.message : String(error))
  } finally {
    db.close()
  }
}

export function loadDecisionEpisodeDetail(dbPath: string, caseId: string): Row | null {
  if (!existsSync(dbPath)) return null
  const db = new DatabaseSync(dbPath, { readOnly: true })
  try {
    const identity = db.prepare('SELECT * FROM canonical_cases WHERE case_id=?').get(caseId) as Row | undefined
    if (!identity) return null
    const all = (sql: string): Row[] => (db.prepare(sql).all(caseId) as Row[]).map(normalize)
    const one = (sql: string): Row | undefined => {
      const row = db.prepare(sql).get(caseId) as Row | undefined
      return row ? normalize(row) : undefined
    }
    const context = one('SELECT * FROM canonical_contexts WHERE case_id=?')
    const finalDecision = one('SELECT * FROM canonical_final_decisions WHERE case_id=?')
    return {
      canonical: true, identity: normalize(identity),
      sourceRegistry: all('SELECT * FROM canonical_sources WHERE case_id=? ORDER BY source_kind'),
      context: context ? { accountBefore: context.account_before, constraints: context.constraints } : null,
      facts: all('SELECT * FROM canonical_facts WHERE case_id=? ORDER BY fact_id'),
      relations: all('SELECT * FROM canonical_relations WHERE case_id=? ORDER BY relation_id'),
      claims: all('SELECT * FROM canonical_claims WHERE case_id=? ORDER BY claim_id').map(row => ({ ...row, supportingEvidence: row.supporting_evidence, contradictingEvidence: row.contradicting_evidence })),
      conflicts: all('SELECT * FROM canonical_conflicts WHERE case_id=? ORDER BY conflict_id'),
      candidateActions: all('SELECT * FROM canonical_candidate_actions WHERE case_id=? ORDER BY candidate_id'),
      finalDecision: finalDecision ? { ...finalDecision, targetStructure: finalDecision.target_structure } : null,
      actualOrders: all('SELECT * FROM canonical_actual_orders WHERE case_id=? ORDER BY order_id'),
      warnings: all('SELECT * FROM canonical_warnings WHERE case_id=? ORDER BY warning_id'),
    }
  } finally {
    db.close()
  }
}
