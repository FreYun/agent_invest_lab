import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { DatabaseSync, type SQLInputValue } from 'node:sqlite'
import { fileURLToPath } from 'node:url'
import { initializeExperienceLibrary } from './schema.ts'
import {
  extractExperienceCandidates,
  extractSummary,
  parseMarkdownSections,
  pickDecisionText,
  renderThirdPersonCandidate,
  sha256,
  type CandidateDirection,
  type ExperienceCandidate,
} from './extract.ts'

export interface ImportBatchOptions {
  worldRoot: string
  fundDbPath: string
  outputDbPath: string
  reportPath: string
  runId: string
  botId: string
  startDate?: string
  endDate?: string
  now?: string
  materialMode?: 'all' | 'action_or_governance' | 'none'
  skipReport?: boolean
}

function shouldExtractMaterials(
  mode: ImportBatchOptions['materialMode'],
  decisionText: string,
  actions: Array<Record<string, unknown>>,
): boolean {
  if (mode === 'none') return false
  if (!mode || mode === 'all') return true
  if (actions.length > 0) return true
  return /反思|复盘|教训|失效|错误|改进|故障|未执行|冲突|闸门|证伪|回撤|切换|减仓|加仓|清仓|调仓/.test(decisionText)
}

export interface ImportBatchResult {
  personaSnapshotId: string
  casesInserted: number
  cardsInserted: number
  evidenceInserted: number
  conflictsInserted: number
  reportPath: string
  outputDbPath: string
}

interface Assignment {
  strategy_id?: string
  strategy_title?: string
  target_index?: string
  buyable_fund_codes?: unknown
}

interface Persona {
  id: string
  title: string
  strategyId: string | null
  strategyTitle: string | null
  strategyFamily: string
  objective: string
  riskProfile: string
  cadence: string
  lineageId: string
  agentsHash: string | null
  userHash: string | null
  methodologyHash: string | null
  payload: Record<string, unknown>
}

interface ImportedCard {
  id: string
  direction: CandidateDirection
  topics: string[]
  conflictSignature: string | null
}

const asJson = (value: unknown): string => JSON.stringify(value)
const readText = (path: string): string => existsSync(path) ? readFileSync(path, 'utf8') : ''
const fileHash = (path: string): string | null => existsSync(path) ? sha256(readFileSync(path)) : null

function loadAssignment(runRoot: string, botId: string): Assignment {
  const path = join(runRoot, 'strategy-assignments.json')
  if (!existsSync(path)) return {}
  try {
    return (JSON.parse(readFileSync(path, 'utf8')) as { bots?: Record<string, Assignment> }).bots?.[botId] ?? {}
  } catch { return {} }
}

function buildPersona(runRoot: string, runId: string, botId: string, assignment: Assignment): Persona {
  const workspace = join(runRoot, 'workspaces', botId)
  const agentsPath = join(workspace, 'AGENTS.md')
  const userPath = join(workspace, 'USER.md')
  const methodologyPath = join(workspace, 'METHODOLOGY.md')
  const agents = readText(agentsPath)
  const methodology = readText(methodologyPath)
  const hashes = {
    agents: fileHash(agentsPath), user: fileHash(userPath), methodology: fileHash(methodologyPath),
  }
  const title = /^#\s+AGENTS\.md\s*[—-]\s*(.+)$/m.exec(agents)?.[1]?.trim() ?? `${botId} persona`
  const roleBlock = /##\s+角色定义\s*\n([\s\S]*?)(?=\n##\s+)/.exec(agents)?.[1] ?? ''
  const strategyId = assignment.strategy_id ?? null
  const strategyFamily = strategyId ?? 'unknown'
  const objective = /绝对收益/.test(agents + methodology) ? 'absolute_return' : 'unspecified'
  const riskProfile = /高风险偏好|激进型|进攻型/.test(title + roleBlock)
    ? 'high' : (/低风险|保本优先/.test(title + roleBlock) ? 'low' : 'unspecified')
  const cadence = /weekly/i.test(runId) || /周度|每周|周期再平衡\s*5日/.test(agents + methodology) ? 'weekly' : 'unspecified'
  const lineageId = `lineage_${sha256([strategyFamily, hashes.agents, hashes.methodology].join('|')).slice(0, 16)}`
  const id = `persona_${sha256([runId, botId, hashes.agents, hashes.user, hashes.methodology].join('|')).slice(0, 20)}`
  return {
    id, title, strategyId, strategyTitle: assignment.strategy_title ?? null,
    strategyFamily, objective, riskProfile, cadence, lineageId,
    agentsHash: hashes.agents, userHash: hashes.user, methodologyHash: hashes.methodology,
    payload: {
      bot_id: botId, run_id: runId, title,
      role_summary: roleBlock.split('\n').filter(line => /^\s*[-*]\s+/.test(line)).slice(0, 6),
      strategy_id: strategyId, strategy_title: assignment.strategy_title ?? null,
      target_index: assignment.target_index ?? null,
      buyable_fund_count: Array.isArray(assignment.buyable_fund_codes) ? assignment.buyable_fund_codes.length : null,
      objective, risk_profile: riskProfile, rebalance_cadence: cadence,
      source_files: {
        agents: { path: relative(dirname(runRoot), agentsPath), sha256: hashes.agents },
        user: { path: relative(dirname(runRoot), userPath), sha256: hashes.user },
        methodology: { path: relative(dirname(runRoot), methodologyPath), sha256: hashes.methodology },
      },
    },
  }
}

function sourceDates(runRoot: string, botId: string, start?: string, end?: string): string[] {
  return readdirSync(runRoot, { withFileTypes: true })
    .filter(entry => entry.isDirectory() && /^\d{4}-\d{2}-\d{2}$/.test(entry.name))
    .map(entry => entry.name)
    .filter(date => (!start || date >= start) && (!end || date <= end))
    .filter(date => existsSync(join(runRoot, date, botId, 'reply.json')))
    .sort()
}

function queryRows(db: DatabaseSync | null, sql: string, params: SQLInputValue[]): Record<string, unknown>[] {
  if (!db) return []
  try { return db.prepare(sql).all(...params) as Record<string, unknown>[] }
  catch { return [] }
}

function reliableSummary(extracted: string, actions: Record<string, unknown>[]): string {
  if (!/^(?:Now|Let me|Actually|I\s)/i.test(extracted)) return extracted
  if (!actions.length) return '原回复缺少可靠的结构化收尾；需人工复核当日案例。'
  const rendered = actions.slice(0, 8).map(action => {
    const type = String(action.action_type ?? action.final_decision ?? 'action')
    const fund = String(action.fund_code ?? '')
    const decision = String(action.final_decision ?? '')
    return `${type} ${fund}${decision && decision !== type ? `（${decision.slice(0, 60)}）` : ''}`.trim()
  })
  return `数据库实际动作：${rendered.join('；')}`
}


function numberValue(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function rounded(value: number | null, digits = 4): number | null {
  if (value === null) return null
  const scale = 10 ** digits
  return Math.round(value * scale) / scale
}

function weightPercent(value: unknown): number | null {
  const number = numberValue(value)
  if (number === null) return null
  return rounded(Math.abs(number) <= 1.5 ? number * 100 : number, 4)
}

function actionDirection(value: unknown): string {
  const action = String(value ?? "").toUpperCase()
  if (/ADD|BUY|INCREASE/.test(action)) return "increase"
  if (/REDUCE|SELL|CLEAR|DELETE/.test(action)) return "reduce"
  if (/HOLD/.test(action)) return "hold"
  return "other"
}

export function buildDecisionContext(
  date: string,
  snapshots: Record<string, unknown>[],
  actions: Record<string, unknown>[],
  orders: Record<string, unknown>[],
): Record<string, unknown> {
  const snapshot = snapshots[0] ?? {}
  const totalValue = numberValue(snapshot.total_value)
  const investedValue = numberValue(snapshot.invested_value)
  const actionAmount = actions.reduce((sum, action) => sum + Math.abs(numberValue(action.amount) ?? 0), 0)
  const actionDirections = [...new Set(actions.map(action => actionDirection(action.action_type ?? action.final_decision)))]
  const beforeAfterAvailable = actions.some(action => numberValue(action.before_weight) !== null || numberValue(action.after_weight) !== null)
  const missingFields = [
    ...(snapshots.length ? [] : ["account_snapshot"]),
    ...(!beforeAfterAvailable ? ["position_before_after_pct"] : []),
    "position_concentration",
    "holding_period_distribution",
    "risk_budget_utilization",
  ]
  return {
    observed_at: date,
    snapshot_timing: 'same_day_database_snapshot_pre_post_sequence_unverified',
    source_account_state: {
      net_value: rounded(numberValue(snapshot.net_value), 6),
      daily_return_pct: rounded(numberValue(snapshot.daily_return_pct), 4),
      cumulative_return_pct: rounded(numberValue(snapshot.cumulative_return_pct), 4),
      max_drawdown_pct: rounded(numberValue(snapshot.max_drawdown_pct), 4),
      invested_weight_pct: totalValue && investedValue !== null ? rounded(investedValue / totalValue * 100, 4) : null,
      equity_weight_pct: weightPercent(snapshot.equity_weight),
      bond_weight_pct: weightPercent(snapshot.bond_weight),
      gold_weight_pct: weightPercent(snapshot.gold_weight),
      cash_weight_pct: weightPercent(snapshot.cash_weight),
    },
    source_decision: {
      action_count: actions.length,
      order_count: orders.length,
      directions: actionDirections,
      gross_action_value_pct: totalValue ? rounded(actionAmount / totalValue * 100, 4) : null,
      pending_order_count: orders.filter(order => !["confirmed", "priced", "done"].includes(String(order.status ?? "").toLowerCase())).length,
      exact_position_before_after_available: beforeAfterAvailable,
    },
    interpretation: {
      status: "descriptive_context_only",
      observed_facts_only: true,
      causal_relevance_verified: false,
    },
    matching_inputs: {
      comparable_fields: [
        "equity_weight_pct", "cash_weight_pct", "max_drawdown_pct",
        "cumulative_return_pct", "gross_action_value_pct",
      ],
      missing_fields: missingFields,
    },
  }
}

function cardPayload(candidate: ExperienceCandidate, caseId: string, date: string, now: string, opts: ImportBatchOptions, persona: Persona, snapshots: Record<string, unknown>[], actions: Record<string, unknown>[], orders: Record<string, unknown>[]) {
  const canonical = renderThirdPersonCandidate(candidate, date)
  const decisionContext = {
    ...buildDecisionContext(date, snapshots, actions, orders),
    source_agent_profile: {
      strategy_family: persona.strategyFamily,
      objective: persona.objective,
      risk_profile: persona.riskProfile,
      rebalance_cadence: persona.cadence,
      profile_basis: "frozen_persona_snapshot",
      source_identity_exposed: false,
    },
  }
  const actionPolicy = {
    authority: 'research_only', suggested_action: 'inspect_current_evidence',
    direction: candidate.direction, max_position_impact_pct: 0,
    required_current_evidence: [
      '确认当前条件与历史案例是否真正一致',
      '至少寻找一项独立的当期数据支持或否定该主张',
      '检查相反经验和替代解释',
    ],
  }
  const applicability = {
    target: 'portfolio', horizon: 'unspecified', topics: candidate.topics,
    applies_if_text: canonical.appliesIfText,
    invalid_if: ['当前条件无法确认', '只有单一叙事、没有当期数据支持', '来源 Agent 的人设、策略或风险预算与消费者不兼容'],
    structured_conditions_verified: false,
  }
  const temporal = {
    decision_at: `${date}T15:00:00+08:00`, market_data_cutoff: date,
    data_vintage: `unverified-current-db:${date}`, outcome_available_at: null,
    published_at: now, available_from: now, expires_at: null,
  }
  const provenance = {
    source_case_ids: [caseId],
    source_actor: {
      bot_id: opts.botId, persona_snapshot_id: persona.id,
      agent_lineage_id: persona.lineageId, methodology_hash: persona.methodologyHash,
    },
    claimant: { type: 'agent', id: opts.botId, source_section: candidate.sourceSection },
    extractor: { type: 'deterministic_parser', version: 'experience-import-v2-third-person', blind_to_future_outcome: true },
    normalizer: { voice: 'third_person_attributed', source_text_preserved_verbatim: true },
    verifier: { type: 'pipeline', result: 'source_present_outcome_unverified' },
    publisher: { decision: 'case_only', reason: 'single source case; PIT and outcome not independently verified' },
  }
  const personaScope = {
    source_persona_snapshot_id: persona.id,
    compatible_strategy_families: [persona.strategyFamily],
    incompatible_strategy_families: [], transfer_risk: 'high',
  }
  const distribution = {
    visibility: 'private_shadow', requires_countercard: true,
    max_fleet_adoption_pct: 0, expose_source_identity_to_agent: false,
    expose_source_performance_to_agent: false,
  }
  const governance = {
    ingestion_checks: {
      source_hash_verified: true, point_in_time_verified: false,
      data_vintage_verified: false, episode_deduplicated: false,
      persona_snapshot_present: true, conflict_scan_completed: true,
      third_person_attribution_present: true,
    },
    promotion_blockers: [
      'single_independent_episode', 'pit_unverified',
      'outcome_unverified', 'conditions_not_structured',
    ],
  }
  return { canonical, decisionContext, actionPolicy, applicability, temporal, provenance, personaScope, distribution, governance }
}

function opposite(a: CandidateDirection, b: CandidateDirection): boolean {
  return (a === 'increase' && b === 'reduce') || (a === 'reduce' && b === 'increase')
}

function insertConflicts(db: DatabaseSync, cards: ImportedCard[], now: string): number {
  let inserted = 0
  const stmt = db.prepare(`
    INSERT OR IGNORE INTO experience_conflicts(
      conflict_id, left_card_id, left_card_version, right_card_id, right_card_version,
      conflict_type, overlap_json, discriminating_evidence_json, status, detected_at
    ) VALUES (?, ?, 1, ?, 1, 'potential_opposite_action', ?, ?, 'unresolved', ?)
  `)
  for (let i = 0; i < cards.length; i++) for (let j = i + 1; j < cards.length; j++) {
    const left = cards[i], right = cards[j]
    if (!left.conflictSignature || left.conflictSignature !== right.conflictSignature || !opposite(left.direction, right.direction)) continue
    const ids = [left.id, right.id].sort()
    const changed = stmt.run(
      `conf_${sha256(ids.join('|')).slice(0, 20)}`, ids[0], ids[1],
      asJson({ signature: left.conflictSignature, topics: [...new Set([...left.topics, ...right.topics])] }),
      asJson([
        '当前条件是否真正重叠，而不只是出现相同关键词',
        '流动性、趋势和基本面是否共同确认',
        '两个主张针对的仓位角色与期限是否一致',
      ]), now,
    ).changes
    inserted += Number(changed)
  }
  return inserted
}

function writeReport(db: DatabaseSync, opts: ImportBatchOptions, persona: Persona, result: Omit<ImportBatchResult, 'reportPath' | 'outputDbPath'>): void {
  const cases = db.prepare(`
    SELECT c.case_id, c.trade_date, c.summary,
      json_array_length(c.actual_actions_json) action_count,
      json_array_length(c.actual_orders_json) order_count,
      COUNT(s.card_id) card_count
    FROM experience_cases c LEFT JOIN experience_card_sources s ON s.case_id=c.case_id
    WHERE c.run_id=? AND c.bot_id=? GROUP BY c.case_id ORDER BY c.trade_date
  `).all(opts.runId, opts.botId) as Array<Record<string, unknown>>
  const cards = db.prepare(`
    SELECT c.card_id,c.card_type,c.status,c.title,c.claim,c.action_policy_json,
      c.applicability_json,s.source_section,e.relation,e.pit_verified,e.outcome_verified
    FROM experience_card_versions c
    JOIN experience_card_sources s ON s.card_id=c.card_id AND s.card_version=c.version
    JOIN experience_cases x ON x.case_id=s.case_id
    LEFT JOIN experience_evidence e ON e.card_id=c.card_id AND e.card_version=c.version
    WHERE x.run_id=? AND x.bot_id=? ORDER BY x.trade_date,c.card_id
  `).all(opts.runId, opts.botId) as Array<Record<string, unknown>>
  const conflicts = db.prepare('SELECT * FROM experience_conflicts ORDER BY conflict_id').all() as Array<Record<string, unknown>>
  const lines = [
    '# 经验图书馆首批影子入库报告', '',
    `- Run：\`${opts.runId}\``, `- Agent：\`${opts.botId}\``,
    `- 日期范围：${opts.startDate ?? '不限'} ～ ${opts.endDate ?? '不限'}`,
    `- 人设快照：\`${persona.id}\``, `- 人设：${persona.title}`,
    `- 策略：${persona.strategyTitle ?? persona.strategyId ?? '未知'}`,
    `- 风险画像：${persona.riskProfile}；节奏：${persona.cadence}`, '',
    '## 入库结果', '', `- 新增案例：${result.casesInserted}`,
    `- 新增候选经验卡：${result.cardsInserted}`, `- 新增证据记录：${result.evidenceInserted}`,
    `- 检出的潜在冲突：${result.conflictsInserted}`, '',
    '> 所有市场经验均为 `case_only`、`research_only`、最大仓位影响 0。PIT vintage 和反事实结果尚未验证。', '',
    '## 案例', '', '| 日期 | 案例 | 动作 | 订单 | 候选卡 | 摘要 |', '|---|---|---:|---:|---:|---|',
  ]
  for (const row of cases) {
    const summary = String(row.summary ?? '').replace(/\|/g, '\\|').replace(/\s+/g, ' ').slice(0, 180)
    lines.push(`| ${row.trade_date} | \`${row.case_id}\` | ${row.action_count} | ${row.order_count} | ${row.card_count} | ${summary} |`)
  }
  lines.push('', '## 候选经验卡', '')
  for (const row of cards) {
    const action = JSON.parse(String(row.action_policy_json)) as Record<string, unknown>
    const applicability = JSON.parse(String(row.applicability_json)) as { topics?: string[] }
    lines.push(
      `### ${row.title}`, '',
      `- ID：\`${row.card_id}\`；类型：\`${row.card_type}\`；状态：\`${row.status}\``,
      `- 来源：${row.source_section}；主题：${(applicability.topics ?? []).join(', ') || 'unclassified'}`,
      `- 方向：\`${String(action.direction)}\`；权限：\`${String(action.authority)}\`；最大仓位影响：${String(action.max_position_impact_pct)}%`,
      `- 证据：\`${row.relation}\`；PIT：${Number(row.pit_verified) ? '已验证' : '未验证'}；结果：${Number(row.outcome_verified) ? '已验证' : '未验证'}`, '',
      `> ${String(row.claim).replace(/\n/g, ' ')}`, '',
    )
  }
  lines.push('## 潜在冲突', '')
  if (!conflicts.length) lines.push('本批没有自动确认的相反动作冲突。', '')
  else for (const c of conflicts) lines.push(`- \`${c.conflict_id}\`：\`${c.left_card_id}\` ↔ \`${c.right_card_id}\`（未解决）`)
  lines.push('', '## 当前质量闸门', '',
    '- 原始 reply/sent 已 hash 留痕，人设与方法论已生成不可变快照。',
    '- sent.md 的滚动记忆没有作为新证据重复入库。',
    '- 当前证据统一为 inconclusive，没有把 Agent 自述记成“支持”。',
    '- 未验证 PIT、结果窗口和反事实之前，卡片不能进入共享检索。', '')
  mkdirSync(dirname(opts.reportPath), { recursive: true })
  writeFileSync(opts.reportPath, lines.join('\n'))
}

export function importExperienceBatch(opts: ImportBatchOptions): ImportBatchResult {
  const now = opts.now ?? new Date().toISOString()
  const worldRoot = resolve(opts.worldRoot)
  const runRoot = join(worldRoot, 'runs', opts.runId)
  if (!existsSync(runRoot)) throw new Error(`run not found: ${runRoot}`)
  mkdirSync(dirname(opts.outputDbPath), { recursive: true })
  const db = new DatabaseSync(opts.outputDbPath)
  initializeExperienceLibrary(db)
  const fundDb = existsSync(opts.fundDbPath) ? new DatabaseSync(opts.fundDbPath, { readOnly: true }) : null
  const persona = buildPersona(runRoot, opts.runId, opts.botId, loadAssignment(runRoot, opts.botId))
  db.prepare(`INSERT OR IGNORE INTO agent_persona_snapshots(
    persona_snapshot_id,bot_id,run_id,persona_title,strategy_id,strategy_title,strategy_family,
    objective,risk_profile,rebalance_cadence,agent_lineage_id,agents_sha256,user_sha256,
    methodology_sha256,persona_json,captured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
      persona.id,opts.botId,opts.runId,persona.title,persona.strategyId,persona.strategyTitle,
      persona.strategyFamily,persona.objective,persona.riskProfile,persona.cadence,persona.lineageId,
      persona.agentsHash,persona.userHash,persona.methodologyHash,asJson(persona.payload),now)

  let casesInserted=0, cardsInserted=0, evidenceInserted=0
  const importedCards: ImportedCard[]=[]
  db.exec('BEGIN IMMEDIATE')
  try {
    for (const date of sourceDates(runRoot,opts.botId,opts.startDate,opts.endDate)) {
      const dayRoot=join(runRoot,date,opts.botId), replyPath=join(dayRoot,'reply.json'), sentPath=join(dayRoot,'sent.md')
      const replyRaw=readFileSync(replyPath,'utf8')
      let parsed: {reply?:unknown;assistant_messages?:unknown}
      try { parsed=JSON.parse(replyRaw) as typeof parsed } catch { continue }
      const decisionText=pickDecisionText(parsed)
      if (!decisionText.trim()) continue
      const sections=parseMarkdownSections(decisionText), replyHash=sha256(replyRaw)
      const caseId=`case_${sha256([opts.runId,opts.botId,date,replyHash].join('|')).slice(0,20)}`
      const episodeId=`episode_cn_equity_${date.replaceAll('-','')}`
      const snapshots=queryRows(fundDb,`SELECT trade_date,initial_capital,cash,invested_value,total_value,net_value,daily_return_pct,cumulative_return_pct,max_drawdown_pct,equity_weight,bond_weight,gold_weight,cash_weight FROM fund_bot_daily_snapshots WHERE bot_id=? AND run_id=? AND trade_date=?`,[opts.botId,opts.runId,date])
      const actions=queryRows(fundDb,`SELECT action_id,fund_code,action_type,trigger,final_decision,before_weight,after_weight,amount,shares,fee,reason,action_date,paradigm FROM fund_bot_actions WHERE bot_id=? AND run_id=? AND action_date=? ORDER BY action_id`,[opts.botId,opts.runId,date])
      const orders=queryRows(fundDb,`SELECT order_id,fund_code,fund_name,order_type,order_date,confirm_date,order_amount,confirmed_amount,fee,action_reason,status,pricing_status FROM fund_bot_orders WHERE bot_id=? AND order_run_id=? AND order_date=? ORDER BY order_id`,[opts.botId,opts.runId,date])
      const inserted=db.prepare(`INSERT OR IGNORE INTO experience_cases(
        case_id,episode_id,run_id,bot_id,trade_date,decision_at,market_data_cutoff,data_vintage,
        persona_snapshot_id,summary,decision_text,sections_json,account_snapshot_json,actual_actions_json,
        actual_orders_json,source_reply_path,source_reply_sha256,source_sent_path,source_sent_sha256,
        source_verified,pit_verified,execution_verified,outcome_verified,imported_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
          caseId,episodeId,opts.runId,opts.botId,date,`${date}T15:00:00+08:00`,date,
          `unverified-current-db:${date}`,persona.id,reliableSummary(extractSummary(sections,decisionText),actions),decisionText,
          asJson(sections),asJson(snapshots),asJson(actions),asJson(orders),relative(worldRoot,replyPath),replyHash,
          existsSync(sentPath)?relative(worldRoot,sentPath):null,fileHash(sentPath),1,0,fundDb?1:0,0,now).changes
      casesInserted+=Number(inserted)

      const materialCandidates = shouldExtractMaterials(opts.materialMode, decisionText, actions)
        ? extractExperienceCandidates(sections)
        : []
      for (const candidate of materialCandidates) {
        const id=`exp_${sha256([caseId,candidate.sourceSection,candidate.sourceText].join('|')).slice(0,20)}`
        const p=cardPayload(candidate,caseId,date,now,opts,persona,snapshots,actions,orders)
        const alternatives=['结果可能主要由同期整体市场或其他共同因素驱动','Agent 的文字可能是事后归因，尚未证明因果关系','可能不适用于不同风险预算、期限或方法论']
        const contentHash=sha256(asJson({candidate,...p}))
        const cardChanged=db.prepare(`INSERT OR IGNORE INTO experience_card_versions(
          card_id,version,supersedes_card_id,card_type,status,domain,title,claim,decision_context_json,mechanism_json,
          falsifiers_json,alternative_explanations_json,applicability_json,persona_scope_json,
          action_policy_json,provenance_json,temporal_json,distribution_policy_json,governance_json,
          conflict_signature,content_hash,created_at)
          VALUES (?,1,NULL,?,'case_only','portfolio_risk',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
            id,candidate.cardType,p.canonical.title,p.canonical.claim,asJson(p.decisionContext),asJson([]),asJson([]),asJson(alternatives),
            asJson(p.applicability),asJson(p.personaScope),asJson(p.actionPolicy),asJson(p.provenance),
            asJson(p.temporal),asJson(p.distribution),asJson(p.governance),candidate.conflictSignature,contentHash,now).changes
        cardsInserted+=Number(cardChanged)
        db.prepare(`INSERT OR IGNORE INTO experience_card_sources(card_id,card_version,case_id,source_section,source_text,source_role) VALUES (?,1,?,?,?,'claimant_text')`).run(id,caseId,candidate.sourceSection,candidate.sourceText)
        const evidenceId=`ev_${sha256([id,caseId,'inconclusive'].join('|')).slice(0,20)}`
        const evidenceChanged=db.prepare(`INSERT OR IGNORE INTO experience_evidence(
          evidence_id,card_id,card_version,case_id,relation,episode_id,agent_lineage_id,
          counts_as_independent_episode,pit_verified,execution_verified,outcome_verified,effect_json,review_json,created_at)
          VALUES (?,?,1,?,'inconclusive',?,?,0,0,?,0,?,?,?)`).run(
            evidenceId,id,caseId,episodeId,persona.lineageId,fundDb?1:0,
            asJson({baseline:null,utility_delta:null,reason_null:'counterfactual and outcome window not evaluated'}),
            asJson({reviewer:'pipeline',decision:'case_only',limitations:(p.governance as {promotion_blockers:string[]}).promotion_blockers}),now).changes
        evidenceInserted+=Number(evidenceChanged)
        importedCards.push({id,direction:candidate.direction,topics:candidate.topics,conflictSignature:candidate.conflictSignature})
      }
    }
    db.exec('COMMIT')
  } catch (error) {
    db.exec('ROLLBACK'); fundDb?.close(); db.close(); throw error
  }
  const conflictsInserted=insertConflicts(db,importedCards,now)
  const base={personaSnapshotId:persona.id,casesInserted,cardsInserted,evidenceInserted,conflictsInserted}
  if (!opts.skipReport) writeReport(db,opts,persona,base)
  fundDb?.close(); db.close()
  return {...base,reportPath:opts.reportPath,outputDbPath:opts.outputDbPath}
}

function arg(argv:string[],name:string):string|undefined { const i=argv.indexOf(name); return i>=0?argv[i+1]:undefined }

function cli(argv:string[]):number {
  const here=dirname(fileURLToPath(import.meta.url)), worldRoot=resolve(arg(argv,'--world-root')??resolve(here,'../../runtime'))
  const runId=arg(argv,'--run-id'), botId=arg(argv,'--bot-id')
  if (!runId||!botId) { process.stderr.write('usage: batch.ts --run-id ID --bot-id ID [--start DATE] [--end DATE]\n'); return 2 }
  const outRoot=join(worldRoot,'experience-library')
  const result=importExperienceBatch({
    worldRoot,runId,botId,startDate:arg(argv,'--start'),endDate:arg(argv,'--end'),
    materialMode:(arg(argv,'--material-mode') as ImportBatchOptions['materialMode'])??'all',
    skipReport:argv.includes('--skip-report'),
    fundDbPath:resolve(arg(argv,'--fund-db')??join(worldRoot,'..','..','data','fund.db')),
    outputDbPath:resolve(arg(argv,'--output-db')??join(outRoot,'experience-library.db')),
    reportPath:resolve(arg(argv,'--report')??join(outRoot,`batch-${runId}-${botId}.md`)),
  })
  process.stdout.write(JSON.stringify(result,null,2)+'\n'); return 0
}

if (process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url)) process.exitCode=cli(process.argv.slice(2))
