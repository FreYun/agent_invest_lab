import type { DatabaseSync } from 'node:sqlite'

export const CANONICAL_DECISION_SCHEMA_VERSION = 'decision_episode_v0.1'

export function initializeCanonicalDecisionDatabase(db: DatabaseSync): void {
  db.exec(`
    PRAGMA foreign_keys = ON;
    PRAGMA journal_mode = WAL;

    CREATE TABLE canonical_meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );

    CREATE TABLE canonical_cases (
      case_id TEXT PRIMARY KEY,
      schema_version TEXT NOT NULL,
      extraction_status TEXT NOT NULL,
      bot_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      world_date TEXT NOT NULL,
      decision_time TEXT,
      decision_time_reason TEXT NOT NULL,
      knowledge_cutoff TEXT NOT NULL,
      knowledge_rule TEXT NOT NULL,
      decision_summary TEXT NOT NULL,
      source_bundle_hash TEXT NOT NULL,
      created_at TEXT NOT NULL
    );

    CREATE TABLE canonical_sources (
      source_id TEXT PRIMARY KEY,
      case_id TEXT NOT NULL REFERENCES canonical_cases(case_id),
      source_kind TEXT NOT NULL,
      path TEXT NOT NULL,
      sha256 TEXT,
      tables_json TEXT NOT NULL CHECK (json_valid(tables_json)),
      UNIQUE(case_id, source_kind)
    );

    CREATE TABLE canonical_contexts (
      case_id TEXT PRIMARY KEY REFERENCES canonical_cases(case_id),
      account_before_json TEXT NOT NULL CHECK (json_valid(account_before_json)),
      constraints_json TEXT NOT NULL CHECK (json_valid(constraints_json))
    );

    CREATE TABLE canonical_facts (
      fact_id TEXT PRIMARY KEY,
      case_id TEXT NOT NULL REFERENCES canonical_cases(case_id),
      subject TEXT NOT NULL,
      predicate TEXT NOT NULL,
      value_json TEXT NOT NULL CHECK (json_valid(value_json)),
      unit TEXT,
      event_time TEXT,
      producer TEXT,
      fact_class TEXT NOT NULL,
      source_locator TEXT NOT NULL,
      reproducible TEXT,
      metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json))
    );

    CREATE TABLE canonical_relations (
      relation_id TEXT PRIMARY KEY,
      case_id TEXT NOT NULL REFERENCES canonical_cases(case_id),
      subject TEXT NOT NULL,
      predicate TEXT NOT NULL,
      object TEXT NOT NULL,
      direction TEXT NOT NULL,
      mechanism TEXT NOT NULL,
      valid_from TEXT,
      valid_to TEXT,
      evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
      certainty TEXT NOT NULL
    );

    CREATE TABLE canonical_claims (
      claim_id TEXT PRIMARY KEY,
      case_id TEXT NOT NULL REFERENCES canonical_cases(case_id),
      statement TEXT NOT NULL,
      claim_type TEXT NOT NULL,
      status TEXT NOT NULL,
      confidence_label TEXT NOT NULL,
      mechanism_json TEXT NOT NULL CHECK (json_valid(mechanism_json)),
      supporting_evidence_json TEXT NOT NULL CHECK (json_valid(supporting_evidence_json)),
      contradicting_evidence_json TEXT NOT NULL CHECK (json_valid(contradicting_evidence_json)),
      falsifiers_json TEXT NOT NULL CHECK (json_valid(falsifiers_json)),
      certainty TEXT NOT NULL
    );

    CREATE TABLE canonical_conflicts (
      conflict_id TEXT PRIMARY KEY,
      case_id TEXT NOT NULL REFERENCES canonical_cases(case_id),
      left_json TEXT NOT NULL CHECK (json_valid(left_json)),
      right_json TEXT NOT NULL CHECK (json_valid(right_json)),
      winner TEXT NOT NULL,
      resolution TEXT NOT NULL,
      resolution_class TEXT NOT NULL,
      unresolved_issue TEXT
    );

    CREATE TABLE canonical_candidate_actions (
      candidate_id TEXT PRIMARY KEY,
      case_id TEXT NOT NULL REFERENCES canonical_cases(case_id),
      action TEXT NOT NULL,
      requested_amount REAL,
      requested_shares REAL,
      result TEXT NOT NULL,
      order_id TEXT,
      source_tool_call TEXT,
      reason TEXT,
      execution_note TEXT
    );

    CREATE TABLE canonical_final_decisions (
      case_id TEXT PRIMARY KEY REFERENCES canonical_cases(case_id),
      decision TEXT NOT NULL,
      target_structure_json TEXT NOT NULL CHECK (json_valid(target_structure_json)),
      rationale_json TEXT NOT NULL CHECK (json_valid(rationale_json))
    );

    CREATE TABLE canonical_actual_orders (
      order_id TEXT PRIMARY KEY,
      case_id TEXT NOT NULL REFERENCES canonical_cases(case_id),
      action TEXT NOT NULL,
      instrument TEXT NOT NULL,
      requested_amount REAL,
      requested_shares REAL,
      confirmed_amount REAL,
      confirmed_shares REAL,
      fee REAL NOT NULL,
      order_status_at_decision TEXT NOT NULL,
      database_status_now TEXT NOT NULL
    );

    CREATE TABLE canonical_warnings (
      warning_id TEXT PRIMARY KEY,
      case_id TEXT NOT NULL REFERENCES canonical_cases(case_id),
      warning_text TEXT NOT NULL
    );
  `)
  db.prepare('INSERT INTO canonical_meta(key,value) VALUES (?,?)').run('schema_version', CANONICAL_DECISION_SCHEMA_VERSION)
}
