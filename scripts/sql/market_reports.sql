-- market_reports：三类"市场研究报告"的全局共享存储（context / mainline / rotation）。
-- 由 pre-pass 的三个 reporter agent 经 strategy-server.submit_market_report 写入；
-- 回测时各 bot 经 strategy-server.get_market_report 读取（PIT：只返回 as_of_date <= 世界当前日 的最近一期）。
-- scope 目前恒为 'global'（报告与 bot 无关、与 run 无关，全历史只生成一次、所有 run/所有 bot 复用）；
-- 预留该列以便将来支持 per-bot / per-run 变体。
CREATE TABLE IF NOT EXISTS market_reports (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  report_type     TEXT NOT NULL,              -- 'market_context' | 'market_mainline' | 'mainline_rotation'
  as_of_date      TEXT NOT NULL,              -- YYYY-MM-DD，报告对应的世界交易日
  scope           TEXT NOT NULL DEFAULT 'global',
  content_md      TEXT NOT NULL,              -- 研报正文（markdown）
  structured_json TEXT,                       -- categorical 结构化字段（regime/risk_state/主线/基金池/组合/计数器…）
  agent_run_id    TEXT,                       -- 生成该报告的 reporter run/session 标识，便于审计回放
  generated_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(report_type, as_of_date, scope)
);

CREATE INDEX IF NOT EXISTS idx_market_reports_lookup
  ON market_reports (report_type, scope, as_of_date);
