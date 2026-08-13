import type { DatabaseSync } from 'node:sqlite'

export const DECISION_EPISODE_SCHEMA_VERSION = 1

export const DECISION_EPISODE_SCHEMA_SQL = `
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS decision_episode_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
  batch_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  bot_id TEXT NOT NULL,
  start_date TEXT,
  end_date TEXT,
  source_root TEXT NOT NULL,
  fund_db_path TEXT NOT NULL,
  extractor_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  summary_json TEXT NOT NULL CHECK (json_valid(summary_json)),
  started_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
  episode_id TEXT PRIMARY KEY,
  logical_episode_key TEXT NOT NULL,
  source_bundle_hash TEXT NOT NULL,
  run_id TEXT NOT NULL,
  bot_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  session_id TEXT,
  decision_time TEXT,
  decision_time_precision TEXT NOT NULL CHECK (
    decision_time_precision IN ('exact', 'date_only', 'unknown')
  ),
  knowledge_cutoff TEXT NOT NULL,
  final_summary TEXT NOT NULL,
  extraction_status TEXT NOT NULL CHECK (
    extraction_status IN ('normalized', 'partial', 'rejected')
  ),
  extractor_version TEXT NOT NULL,
  import_batch_id TEXT NOT NULL REFERENCES import_batches(batch_id),
  imported_at TEXT NOT NULL,
  UNIQUE(logical_episode_key, source_bundle_hash)
);

CREATE INDEX IF NOT EXISTS idx_episodes_run_bot_date
  ON episodes(run_id, bot_id, trade_date);

CREATE TABLE IF NOT EXISTS source_artifacts (
  artifact_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  artifact_kind TEXT NOT NULL,
  source_path TEXT NOT NULL,
  sha256 TEXT,
  byte_size INTEGER,
  media_type TEXT NOT NULL,
  integrity_status TEXT NOT NULL CHECK (
    integrity_status IN ('sha256_verified', 'record_hash_only', 'missing')
  ),
  captured_at TEXT NOT NULL,
  UNIQUE(episode_id, artifact_kind, source_path)
);

CREATE TABLE IF NOT EXISTS facts (
  fact_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  subject TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object_text TEXT,
  value_number REAL,
  value_text TEXT,
  unit TEXT,
  event_time TEXT,
  available_at TEXT,
  valid_from TEXT,
  valid_to TEXT,
  fact_class TEXT NOT NULL CHECK (
    fact_class IN ('recorded_fact', 'reference_fact', 'derived_fact', 'agent_reported_observation')
  ),
  certainty TEXT NOT NULL CHECK (
    certainty IN ('hard_recorded', 'hard_reference', 'derived', 'agent_asserted')
  ),
  source_artifact_id TEXT REFERENCES source_artifacts(artifact_id),
  source_locator TEXT NOT NULL,
  source_record_hash TEXT NOT NULL,
  raw_excerpt TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_episode_predicate
  ON facts(episode_id, predicate);

CREATE TABLE IF NOT EXISTS relations (
  relation_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  subject TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object TEXT NOT NULL,
  direction TEXT NOT NULL,
  mechanism TEXT NOT NULL,
  valid_from TEXT,
  valid_to TEXT,
  certainty TEXT NOT NULL CHECK (
    certainty IN ('hard_recorded', 'hard_reference', 'derived', 'policy_defined', 'estimated')
  ),
  evidence_type TEXT NOT NULL,
  source_artifact_id TEXT REFERENCES source_artifacts(artifact_id),
  source_locator TEXT NOT NULL,
  source_record_hash TEXT NOT NULL,
  raw_excerpt TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_relations_episode_predicate
  ON relations(episode_id, predicate);
CREATE INDEX IF NOT EXISTS idx_relations_subject_object
  ON relations(subject, object);

CREATE TABLE IF NOT EXISTS claims (
  claim_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  statement TEXT NOT NULL,
  claim_type TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('active_at_decision', 'accepted_for_execution', 'overridden_for_execution', 'unresolved')
  ),
  confidence REAL,
  confidence_label TEXT,
  mechanism_json TEXT NOT NULL CHECK (json_valid(mechanism_json)),
  falsifiers_json TEXT NOT NULL CHECK (json_valid(falsifiers_json)),
  valid_from TEXT,
  expires_at TEXT,
  knowledge_cutoff TEXT NOT NULL,
  certainty TEXT NOT NULL CHECK (certainty IN ('policy_defined', 'estimated', 'agent_asserted')),
  source_artifact_id TEXT REFERENCES source_artifacts(artifact_id),
  source_locator TEXT NOT NULL,
  source_record_hash TEXT NOT NULL,
  raw_excerpt TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_episode_type
  ON claims(episode_id, claim_type);

CREATE TABLE IF NOT EXISTS claim_evidence (
  evidence_id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL REFERENCES claims(claim_id),
  relation TEXT NOT NULL CHECK (relation IN ('SUPPORTS', 'REFUTES', 'UNCERTAIN')),
  evidence_type TEXT NOT NULL,
  evidence_ref TEXT,
  verification_status TEXT NOT NULL CHECK (
    verification_status IN ('source_recorded', 'agent_asserted', 'unverified')
  ),
  source_artifact_id TEXT REFERENCES source_artifacts(artifact_id),
  source_locator TEXT NOT NULL,
  source_record_hash TEXT NOT NULL,
  raw_excerpt TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claim_evidence_claim
  ON claim_evidence(claim_id, relation);

CREATE TABLE IF NOT EXISTS candidate_actions (
  candidate_action_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  tool_call_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  action_type TEXT NOT NULL,
  instrument TEXT,
  requested_amount REAL,
  requested_shares REAL,
  reason TEXT,
  observed_tool_outcome TEXT NOT NULL CHECK (
    observed_tool_outcome IN ('accepted', 'rejected', 'transport_error', 'unknown')
  ),
  reconciled_outcome TEXT NOT NULL CHECK (
    reconciled_outcome IN ('database_confirmed', 'database_not_found', 'not_applicable')
  ),
  reconciled_final_action_id TEXT,
  source_artifact_id TEXT NOT NULL REFERENCES source_artifacts(artifact_id),
  source_locator TEXT NOT NULL,
  result_excerpt TEXT NOT NULL,
  source_record_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(episode_id, tool_call_id)
);

CREATE TABLE IF NOT EXISTS final_actions (
  final_action_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  source_table TEXT NOT NULL CHECK (source_table IN ('fund_bot_orders', 'fund_bot_actions')),
  source_primary_key TEXT NOT NULL,
  action_type TEXT NOT NULL,
  instrument TEXT,
  amount REAL,
  shares REAL,
  fee REAL,
  status TEXT,
  verification_status TEXT NOT NULL CHECK (
    verification_status IN ('database_recorded', 'database_confirmed')
  ),
  reason TEXT,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  source_artifact_id TEXT NOT NULL REFERENCES source_artifacts(artifact_id),
  source_locator TEXT NOT NULL,
  source_record_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(episode_id, source_table, source_primary_key)
);

CREATE INDEX IF NOT EXISTS idx_final_actions_episode
  ON final_actions(episode_id, source_table);

CREATE TABLE IF NOT EXISTS decision_conflicts (
  conflict_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  conflict_type TEXT NOT NULL,
  left_statement TEXT NOT NULL,
  right_statement TEXT NOT NULL,
  resolution TEXT,
  winner TEXT,
  status TEXT NOT NULL CHECK (status IN ('resolved', 'unresolved', 'data_quality_conflict')),
  source_artifact_id TEXT REFERENCES source_artifacts(artifact_id),
  source_locator TEXT NOT NULL,
  source_record_hash TEXT NOT NULL,
  raw_excerpt TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quality_issues (
  issue_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
  issue_type TEXT NOT NULL,
  field_path TEXT NOT NULL,
  description TEXT NOT NULL,
  observed_json TEXT NOT NULL CHECK (json_valid(observed_json)),
  source_artifact_id TEXT REFERENCES source_artifacts(artifact_id),
  source_locator TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quality_issues_episode
  ON quality_issues(episode_id, severity, issue_type);

CREATE VIEW IF NOT EXISTS latest_episodes AS
SELECT e.*
FROM episodes e
WHERE e.imported_at = (
  SELECT MAX(e2.imported_at)
  FROM episodes e2
  WHERE e2.logical_episode_key = e.logical_episode_key
);
`

export function initializeDecisionEpisodeDatabase(db: DatabaseSync): void {
  db.exec(DECISION_EPISODE_SCHEMA_SQL)
  db.prepare(`
    INSERT INTO decision_episode_meta(key, value) VALUES ('schema_version', ?)
    ON CONFLICT(key) DO UPDATE SET value = excluded.value
  `).run(String(DECISION_EPISODE_SCHEMA_VERSION))
}
