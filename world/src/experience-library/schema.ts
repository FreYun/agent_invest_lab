import type { DatabaseSync } from 'node:sqlite'

// 经验库与 fund.db 物理隔离。事实、卡片和证据均为 append-only；聚合统计查询时生成。
export const EXPERIENCE_LIBRARY_SCHEMA_VERSION = 2

export const EXPERIENCE_LIBRARY_SCHEMA_SQL = `
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS library_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_persona_snapshots (
  persona_snapshot_id TEXT PRIMARY KEY,
  bot_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  persona_title TEXT NOT NULL,
  strategy_id TEXT,
  strategy_title TEXT,
  strategy_family TEXT,
  objective TEXT,
  risk_profile TEXT,
  rebalance_cadence TEXT,
  agent_lineage_id TEXT NOT NULL,
  agents_sha256 TEXT,
  user_sha256 TEXT,
  methodology_sha256 TEXT,
  persona_json TEXT NOT NULL CHECK (json_valid(persona_json)),
  captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experience_cases (
  case_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  bot_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  decision_at TEXT NOT NULL,
  market_data_cutoff TEXT NOT NULL,
  data_vintage TEXT NOT NULL,
  persona_snapshot_id TEXT NOT NULL REFERENCES agent_persona_snapshots(persona_snapshot_id),
  summary TEXT NOT NULL,
  decision_text TEXT NOT NULL,
  sections_json TEXT NOT NULL CHECK (json_valid(sections_json)),
  account_snapshot_json TEXT NOT NULL CHECK (json_valid(account_snapshot_json)),
  actual_actions_json TEXT NOT NULL CHECK (json_valid(actual_actions_json)),
  actual_orders_json TEXT NOT NULL CHECK (json_valid(actual_orders_json)),
  source_reply_path TEXT NOT NULL,
  source_reply_sha256 TEXT NOT NULL,
  source_sent_path TEXT,
  source_sent_sha256 TEXT,
  source_verified INTEGER NOT NULL CHECK (source_verified IN (0, 1)),
  pit_verified INTEGER NOT NULL CHECK (pit_verified IN (0, 1)),
  execution_verified INTEGER NOT NULL CHECK (execution_verified IN (0, 1)),
  outcome_verified INTEGER NOT NULL CHECK (outcome_verified IN (0, 1)),
  imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_experience_cases_run_bot_date
  ON experience_cases(run_id, bot_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_experience_cases_episode
  ON experience_cases(episode_id);

CREATE TABLE IF NOT EXISTS experience_card_versions (
  card_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  supersedes_card_id TEXT,
  card_type TEXT NOT NULL,
  status TEXT NOT NULL,
  domain TEXT NOT NULL,
  title TEXT NOT NULL,
  claim TEXT NOT NULL,
  decision_context_json TEXT NOT NULL CHECK (json_valid(decision_context_json)),
  mechanism_json TEXT NOT NULL CHECK (json_valid(mechanism_json)),
  falsifiers_json TEXT NOT NULL CHECK (json_valid(falsifiers_json)),
  alternative_explanations_json TEXT NOT NULL CHECK (json_valid(alternative_explanations_json)),
  applicability_json TEXT NOT NULL CHECK (json_valid(applicability_json)),
  persona_scope_json TEXT NOT NULL CHECK (json_valid(persona_scope_json)),
  action_policy_json TEXT NOT NULL CHECK (json_valid(action_policy_json)),
  provenance_json TEXT NOT NULL CHECK (json_valid(provenance_json)),
  temporal_json TEXT NOT NULL CHECK (json_valid(temporal_json)),
  distribution_policy_json TEXT NOT NULL CHECK (json_valid(distribution_policy_json)),
  governance_json TEXT NOT NULL CHECK (json_valid(governance_json)),
  conflict_signature TEXT,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(card_id, version)
);

CREATE INDEX IF NOT EXISTS idx_experience_cards_status
  ON experience_card_versions(status, card_type);
CREATE INDEX IF NOT EXISTS idx_experience_cards_conflict
  ON experience_card_versions(conflict_signature);

CREATE TABLE IF NOT EXISTS experience_card_sources (
  card_id TEXT NOT NULL,
  card_version INTEGER NOT NULL,
  case_id TEXT NOT NULL REFERENCES experience_cases(case_id),
  source_section TEXT NOT NULL,
  source_text TEXT NOT NULL,
  source_role TEXT NOT NULL,
  PRIMARY KEY(card_id, card_version, case_id, source_section, source_text),
  FOREIGN KEY(card_id, card_version)
    REFERENCES experience_card_versions(card_id, version)
);

CREATE TABLE IF NOT EXISTS experience_evidence (
  evidence_id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL,
  card_version INTEGER NOT NULL,
  case_id TEXT NOT NULL REFERENCES experience_cases(case_id),
  relation TEXT NOT NULL CHECK (relation IN ('supporting', 'contradicting', 'inconclusive')),
  episode_id TEXT NOT NULL,
  agent_lineage_id TEXT NOT NULL,
  counts_as_independent_episode INTEGER NOT NULL CHECK (counts_as_independent_episode IN (0, 1)),
  pit_verified INTEGER NOT NULL CHECK (pit_verified IN (0, 1)),
  execution_verified INTEGER NOT NULL CHECK (execution_verified IN (0, 1)),
  outcome_verified INTEGER NOT NULL CHECK (outcome_verified IN (0, 1)),
  effect_json TEXT NOT NULL CHECK (json_valid(effect_json)),
  review_json TEXT NOT NULL CHECK (json_valid(review_json)),
  created_at TEXT NOT NULL,
  FOREIGN KEY(card_id, card_version)
    REFERENCES experience_card_versions(card_id, version)
);

CREATE INDEX IF NOT EXISTS idx_experience_evidence_card
  ON experience_evidence(card_id, card_version, relation);

CREATE TABLE IF NOT EXISTS experience_conflicts (
  conflict_id TEXT PRIMARY KEY,
  left_card_id TEXT NOT NULL,
  left_card_version INTEGER NOT NULL,
  right_card_id TEXT NOT NULL,
  right_card_version INTEGER NOT NULL,
  conflict_type TEXT NOT NULL,
  overlap_json TEXT NOT NULL CHECK (json_valid(overlap_json)),
  discriminating_evidence_json TEXT NOT NULL CHECK (json_valid(discriminating_evidence_json)),
  status TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  FOREIGN KEY(left_card_id, left_card_version)
    REFERENCES experience_card_versions(card_id, version),
  FOREIGN KEY(right_card_id, right_card_version)
    REFERENCES experience_card_versions(card_id, version)
);

CREATE TABLE IF NOT EXISTS library_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  knowledge_cutoff TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_cards (
  snapshot_id TEXT NOT NULL REFERENCES library_snapshots(snapshot_id),
  card_id TEXT NOT NULL,
  card_version INTEGER NOT NULL,
  PRIMARY KEY(snapshot_id, card_id, card_version),
  FOREIGN KEY(card_id, card_version)
    REFERENCES experience_card_versions(card_id, version)
);

CREATE TABLE IF NOT EXISTS experience_usage_logs (
  usage_id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL,
  card_version INTEGER NOT NULL,
  snapshot_id TEXT,
  consumer_run_id TEXT NOT NULL,
  consumer_bot_id TEXT NOT NULL,
  consumer_persona_snapshot_id TEXT,
  query_as_of TEXT NOT NULL,
  applicability TEXT CHECK (applicability IN ('applicable', 'not_applicable', 'uncertain')),
  changed_decision INTEGER CHECK (changed_decision IN (0, 1)),
  position_impact_pct REAL,
  rationale TEXT,
  logged_at TEXT NOT NULL,
  FOREIGN KEY(card_id, card_version)
    REFERENCES experience_card_versions(card_id, version)
);
`

export function initializeExperienceLibrary(db: DatabaseSync): void {
  db.exec(EXPERIENCE_LIBRARY_SCHEMA_SQL)
  const columns = db.prepare('PRAGMA table_info(experience_card_versions)').all() as Array<{ name: string }>
  if (!columns.some(column => column.name === 'decision_context_json')) {
    db.exec("ALTER TABLE experience_card_versions ADD COLUMN decision_context_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(decision_context_json))")
  }
  db.prepare(`
    INSERT INTO library_meta(key, value) VALUES ('schema_version', ?)
    ON CONFLICT(key) DO UPDATE SET value = excluded.value
  `).run(String(EXPERIENCE_LIBRARY_SCHEMA_VERSION))
}

