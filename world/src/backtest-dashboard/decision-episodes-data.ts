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
    available: false,
    reason,
    source: { path: dbPath, readOnly: true },
    summary: { episodes: 0, facts: 0, relations: 0, claims: 0, conflicts: 0, warnings: 0 },
    episodes: [],
  }
}

export function loadDecisionEpisodeCatalog(dbPath: string): Row {
  if (!existsSync(dbPath)) return unavailable(dbPath, 'decision episode database not found')
  const db = new DatabaseSync(dbPath, { readOnly: true })
  try {
    const episodes = (db.prepare(`
      SELECT e.*,
        (SELECT COUNT(*) FROM facts f WHERE f.episode_id=e.episode_id) AS fact_count,
        (SELECT COUNT(*) FROM relations r WHERE r.episode_id=e.episode_id) AS relation_count,
        (SELECT COUNT(*) FROM claims c WHERE c.episode_id=e.episode_id) AS claim_count,
        (SELECT COUNT(*) FROM claim_evidence x JOIN claims c ON c.claim_id=x.claim_id
          WHERE c.episode_id=e.episode_id) AS evidence_count,
        (SELECT COUNT(*) FROM candidate_actions a WHERE a.episode_id=e.episode_id) AS candidate_action_count,
        (SELECT COUNT(*) FROM final_actions a WHERE a.episode_id=e.episode_id) AS final_action_count,
        (SELECT COUNT(*) FROM decision_conflicts c WHERE c.episode_id=e.episode_id) AS conflict_count,
        (SELECT COUNT(*) FROM quality_issues q WHERE q.episode_id=e.episode_id) AS issue_count,
        (SELECT COUNT(*) FROM quality_issues q WHERE q.episode_id=e.episode_id AND q.severity='warning') AS warning_count
      FROM latest_episodes e
      ORDER BY e.trade_date DESC
    `).all() as Row[]).map(normalize)
    const total = (table: string): number => Number((db.prepare(`
      SELECT COUNT(*) n FROM ${table} t JOIN latest_episodes e ON e.episode_id=t.episode_id
    `).get() as { n: number }).n)
    const evidence = Number((db.prepare(`
      SELECT COUNT(*) n FROM claim_evidence x JOIN claims c ON c.claim_id=x.claim_id
      JOIN latest_episodes e ON e.episode_id=c.episode_id
    `).get() as { n: number }).n)
    const version = (db.prepare("SELECT value FROM decision_episode_meta WHERE key='schema_version'").get() as { value?: string } | undefined)?.value ?? null
    return {
      available: true,
      source: { path: dbPath, filename: dbPath.split('/').pop(), readOnly: true, schemaVersion: version },
      summary: {
        episodes: episodes.length,
        facts: total('facts'),
        relations: total('relations'),
        claims: total('claims'),
        evidence,
        conflicts: total('decision_conflicts'),
        warnings: Number((db.prepare(`
          SELECT COUNT(*) n FROM quality_issues q JOIN latest_episodes e ON e.episode_id=q.episode_id
          WHERE q.severity='warning'
        `).get() as { n: number }).n),
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

export function loadDecisionEpisodeDetail(dbPath: string, episodeId: string): Row | null {
  if (!existsSync(dbPath)) return null
  const db = new DatabaseSync(dbPath, { readOnly: true })
  try {
    const episode = db.prepare('SELECT * FROM latest_episodes WHERE episode_id=?').get(episodeId) as Row | undefined
    if (!episode) return null
    const rows = (sql: string): Row[] => (db.prepare(sql).all(episodeId) as Row[]).map(normalize)
    const claims = rows('SELECT * FROM claims WHERE episode_id=? ORDER BY claim_type,claim_id')
    const evidence = claims.length ? (db.prepare(`
      SELECT x.* FROM claim_evidence x JOIN claims c ON c.claim_id=x.claim_id
      WHERE c.episode_id=? ORDER BY x.claim_id,x.relation,x.evidence_id
    `).all(episodeId) as Row[]).map(normalize) : []
    const evidenceByClaim = new Map<string, Row[]>()
    for (const item of evidence) {
      const key = String(item.claim_id)
      evidenceByClaim.set(key, [...(evidenceByClaim.get(key) ?? []), item])
    }
    return {
      episode: normalize(episode),
      sources: rows('SELECT * FROM source_artifacts WHERE episode_id=? ORDER BY artifact_kind'),
      facts: rows('SELECT * FROM facts WHERE episode_id=? ORDER BY predicate,subject,value_number'),
      relations: rows('SELECT * FROM relations WHERE episode_id=? ORDER BY predicate,subject,object'),
      claims: claims.map(claim => ({ ...claim, evidence: evidenceByClaim.get(String(claim.claim_id)) ?? [] })),
      conflicts: rows('SELECT * FROM decision_conflicts WHERE episode_id=? ORDER BY conflict_type,conflict_id'),
      candidateActions: rows('SELECT * FROM candidate_actions WHERE episode_id=? ORDER BY tool_call_id'),
      finalActions: rows('SELECT * FROM final_actions WHERE episode_id=? ORDER BY source_table,source_primary_key'),
      issues: rows("SELECT * FROM quality_issues WHERE episode_id=? ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,issue_type"),
    }
  } finally {
    db.close()
  }
}
