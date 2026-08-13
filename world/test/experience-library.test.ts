import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { test } from 'node:test'
import {
  extractExperienceCandidates,
  parseMarkdownSections,
  pickDecisionText,
  renderThirdPersonCandidate,
  stripBeliefBlocks,
} from '../src/experience-library/extract.ts'
import { importExperienceBatch } from '../src/experience-library/batch.ts'

test('experience extractor: 只从治理段落抽候选，belief 不进入正文', () => {
  const md = `## 2026-01-05 决策完成

### 一句话总结
今天保持观望。

### 核心判断
这只是普通分析，不应自动成为经验卡。

### 加仓/减仓闸门
- **加仓条件**：流动性连续三日改善且趋势修复。
- **减仓触发**：流动性跌破阈值且价格确认破位。

### Belief
\`\`\`yaml
belief:
  schema: v1
\`\`\`
`
  const stripped = stripBeliefBlocks(md)
  assert.doesNotMatch(stripped, /schema: v1/)
  const candidates = extractExperienceCandidates(parseMarkdownSections(stripped))
  assert.equal(candidates.length, 2)
  assert.deepEqual(candidates.map(c => c.direction), ['increase', 'reduce'])
  assert.ok(candidates.every(c => c.topics.includes('liquidity')))
})

test('pickDecisionText: reply 是短收尾时回退到详细 assistant message', () => {
  const detailed = '## 决策\n' + '详细分析'.repeat(300)
  assert.equal(pickDecisionText({ reply: '决策完成', assistant_messages: [{ content: detailed }] }), detailed)
})


test("renderThirdPersonCandidate: 第三人称归属明确且原文保持为引文", () => {
  const candidate = {
    sourceSection: "加仓/减仓闸门", sourceText: "流动性跌破0.5 → 总仓降至15%",
    cardType: "risk_warning" as const, direction: "reduce" as const,
    topics: ["liquidity"], conflictSignature: "portfolio|unspecified|liquidity",
  }
  const rendered = renderThirdPersonCandidate(candidate, "2026-01-05")
  assert.match(rendered.title, /^候选风险规则（来源 Agent）/)
  assert.match(rendered.claim, /来源 Agent 基于其当时的人设、策略与风险预算提出/)
  assert.match(rendered.claim, /“流动性跌破0\.5 → 总仓降至15%”/)
  assert.doesNotMatch(rendered.claim, /(^|[，。；：])(?:我|我们|本 Agent)/)
  assert.doesNotMatch(rendered.claim, /尚未完成|通用交易权限|不应被视为/)
  assert.match(rendered.appliesIfText, /纳入研究/)
})

test('importExperienceBatch: 写入案例、低权限卡、证据和潜在冲突', () => {
  const root = mkdtempSync(join(tmpdir(), 'experience-library-'))
  const worldRoot = join(root, 'runtime'), runId = 'run-one', botId = 'botx'
  const runRoot = join(worldRoot, 'runs', runId)
  const workspace = join(runRoot, 'workspaces', botId)
  const dayRoot = join(runRoot, '2026-01-05', botId)
  mkdirSync(workspace, { recursive: true }); mkdirSync(dayRoot, { recursive: true })
  writeFileSync(join(workspace, 'AGENTS.md'), '# AGENTS.md — 高风险偏好·趋势型\n\n## 角色定义\n- 追求绝对收益。\n- 周度决策。\n')
  writeFileSync(join(workspace, 'USER.md'), '# 用户\n')
  writeFileSync(join(workspace, 'METHODOLOGY.md'), '# 方法论\n')
  writeFileSync(join(runRoot, 'strategy-assignments.json'), JSON.stringify({
    bots: { botx: { strategy_id: 'trend', strategy_title: '趋势策略', target_index: 'x', buyable_fund_codes: ['1'] } },
  }))
  writeFileSync(join(dayRoot, 'sent.md'), '【交易记忆窗口】\n这段不能作为新证据。\n')
  writeFileSync(join(dayRoot, 'reply.json'), JSON.stringify({
    reply: `## 2026-01-05 决策完成

### 一句话总结
流动性转弱，暂不行动。

### 加仓/减仓闸门
- 加仓条件：流动性连续改善且趋势修复。
- 减仓触发：流动性继续恶化且价格确认破位。
`, assistant_messages: [],
  }))

  const fundDbPath = join(root, 'fund.db')
  const fundDb = new DatabaseSync(fundDbPath)
  fundDb.exec(`
    CREATE TABLE fund_bot_daily_snapshots (
      bot_id TEXT, run_id TEXT, trade_date TEXT, initial_capital REAL, cash REAL,
      invested_value REAL, total_value REAL, net_value REAL, daily_return_pct REAL,
      cumulative_return_pct REAL, max_drawdown_pct REAL, equity_weight REAL,
      bond_weight REAL, gold_weight REAL, cash_weight REAL);
    CREATE TABLE fund_bot_actions (
      action_id INTEGER, bot_id TEXT, run_id TEXT, action_date TEXT, fund_code TEXT,
      action_type TEXT, trigger TEXT, final_decision TEXT, before_weight REAL,
      after_weight REAL, amount REAL, shares REAL, fee REAL, reason TEXT, paradigm TEXT);
    CREATE TABLE fund_bot_orders (
      order_id INTEGER, bot_id TEXT, order_run_id TEXT, order_date TEXT, fund_code TEXT,
      fund_name TEXT, order_type TEXT, confirm_date TEXT, order_amount REAL,
      confirmed_amount REAL, fee REAL, action_reason TEXT, status TEXT, pricing_status TEXT);
    INSERT INTO fund_bot_daily_snapshots VALUES
      ('botx','run-one','2026-01-05',100,50,50,100,1,0,0,0,0.5,0,0,0.5);
  `)
  fundDb.close()

  const outputDbPath = join(root, 'experience.db'), reportPath = join(root, 'report.md')
  const result = importExperienceBatch({
    worldRoot, fundDbPath, outputDbPath, reportPath, runId, botId,
    startDate: '2026-01-05', endDate: '2026-01-05', now: '2026-08-07T00:00:00.000Z',
  })
  assert.deepEqual(
    [result.casesInserted, result.cardsInserted, result.evidenceInserted, result.conflictsInserted],
    [1, 2, 2, 1],
  )
  const db = new DatabaseSync(outputDbPath, { readOnly: true })
  assert.equal((db.prepare('SELECT COUNT(*) n FROM agent_persona_snapshots').get() as { n: number }).n, 1)
  const cards = db.prepare('SELECT status,action_policy_json FROM experience_card_versions').all() as Array<{status:string;action_policy_json:string}>
  assert.equal(cards.length, 2)
  assert.ok(cards.every(card => card.status === 'case_only' && JSON.parse(card.action_policy_json).max_position_impact_pct === 0))
  const wording = db.prepare("SELECT title,claim,provenance_json FROM experience_card_versions LIMIT 1").get() as {title:string;claim:string;provenance_json:string}
  assert.match(wording.title, /^候选/); assert.match(wording.claim, /来源 Agent/)
  assert.equal(JSON.parse(wording.provenance_json).normalizer.voice, "third_person_attributed")
  const context = JSON.parse((db.prepare("SELECT decision_context_json FROM experience_card_versions LIMIT 1").get() as {decision_context_json:string}).decision_context_json)
  assert.equal(context.source_account_state.equity_weight_pct, 50)
  assert.equal(context.source_account_state.cash_weight_pct, 50)
  assert.equal(context.source_decision.action_count, 0)
  assert.equal(context.interpretation.status, "descriptive_context_only")
  assert.equal(context.source_agent_profile.strategy_family, "trend")
  assert.equal(context.source_agent_profile.risk_profile, "high")
  assert.equal(context.source_agent_profile.rebalance_cadence, "weekly")
  assert.equal(context.source_agent_profile.source_identity_exposed, false)
  const evidence = db.prepare('SELECT relation,pit_verified,outcome_verified FROM experience_evidence').all() as Array<Record<string,unknown>>
  assert.ok(evidence.every(row => row.relation === 'inconclusive' && row.pit_verified === 0 && row.outcome_verified === 0))
  db.close()
  const report = readFileSync(reportPath, 'utf8')
  assert.match(report, /case_only/); assert.doesNotMatch(report, /这段不能作为新证据/)
  rmSync(root, { recursive: true, force: true })
})

