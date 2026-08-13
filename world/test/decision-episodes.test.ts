import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { test } from 'node:test'
import { importDecisionEpisodes } from '../src/decision-episodes/import.ts'

test('decision episode importer keeps claims, relations, attempts, final actions and warnings separate', () => {
  const root = mkdtempSync(join(tmpdir(), 'decision-episodes-'))
  const worldRoot = join(root, 'world')
  const runId = 'run-pilot', botId = 'botx', date = '2026-08-05'
  const dayRoot = join(worldRoot, 'runtime', 'runs', runId, date, botId)
  mkdirSync(dayRoot, { recursive: true })

  const reason = '防御规则要求切换，但工具返回连接错误。'
  writeFileSync(join(dayRoot, 'sent.md'), '# 当日输入\n')
  writeFileSync(join(dayRoot, 'belief_validation.json'), JSON.stringify({ success: true }))
  writeFileSync(join(dayRoot, 'status.json'), JSON.stringify({ status: 'completed' }))
  writeFileSync(join(dayRoot, 'close_my_day.json'), JSON.stringify({
    success: true,
    assets: { initial_capital: 100, cash_available: 30, cash_in_transit: 40, total_value: 101, net_value: 1.01 },
    asset_allocation_pct: { equity: 70, bond: 0, gold: 0, cash: 30 },
    pnl: { daily_return_pct: 1, cumulative_return_pct: 1, max_drawdown_pct: -2 },
    holdings: [{ fund_code: '000001', holding_days: -3, status: 'closed' }],
  }))
  writeFileSync(join(dayRoot, 'reply.json'), JSON.stringify({
    session_id: 'session-one',
    reply: `\`\`\`yaml
belief:
  target_index: fund:000001
  horizons:
    t+5: { p_up: 0.48, prior_p_up: 0.42, delta: 0.06 }
  evidence:
    - { type: technical, ref: ma120-break, summary: below MA120, polarity: "-" }
\`\`\`

## 当日决策

### 主线判断
防御规则要求降低风险敞口，因此卖出指数基金。

### 关键观察
科技反弹 vs 长期趋势仍弱存在冲突，但硬规则优先执行。
`,
    assistant_messages: [],
    messages: [
      {
        role: 'assistant',
        tool_calls: [{
          id: 'call-one', type: 'function',
          function: {
            name: 'mcp__fund_portfolio_mcp__portfolio_place_buy_order',
            arguments: JSON.stringify({ bot_id: botId, fund_code: '000001', amount: 40, reason }),
          },
        }],
      },
      { role: 'tool', tool_call_id: 'call-one', content: '[tool failed] error sending request for url' },
    ],
  }))

  const fundDbPath = join(root, 'fund.db')
  const fundDb = new DatabaseSync(fundDbPath)
  fundDb.exec(`
    CREATE TABLE fund_bot_daily_snapshots (
      bot_id TEXT, trade_date TEXT, run_id TEXT, initial_capital REAL, cash REAL,
      invested_value REAL, total_value REAL, net_value REAL, daily_return_pct REAL,
      cumulative_return_pct REAL, max_drawdown_pct REAL, equity_weight REAL,
      bond_weight REAL, gold_weight REAL, cash_weight REAL, holdings_json TEXT,
      cash_receivable REAL, PRIMARY KEY(bot_id,trade_date,run_id));
    CREATE TABLE fund_bot_actions (
      action_id INTEGER PRIMARY KEY, review_id INTEGER, bot_id TEXT, fund_code TEXT,
      action_type TEXT, trigger TEXT, timing_state TEXT, momentum_state TEXT,
      matrix_suggestion TEXT, final_decision TEXT, before_weight REAL, after_weight REAL,
      nav_used REAL, amount REAL, shares REAL, fee REAL, reason TEXT, action_date TEXT,
      paradigm TEXT, run_id TEXT);
    CREATE TABLE fund_bot_orders (
      order_id INTEGER PRIMARY KEY, review_id INTEGER, bot_id TEXT, fund_code TEXT,
      fund_name TEXT, order_type TEXT, order_date TEXT, confirm_date TEXT,
      order_amount REAL, reference_nav REAL, confirm_nav REAL, confirmed_shares REAL,
      confirmed_amount REAL, fee REAL, action_reason TEXT, status TEXT, created_at TEXT,
      order_run_id TEXT, settle_run_id TEXT, pricing_status TEXT, pricing_nav_date TEXT, priced_at TEXT);
    CREATE TABLE fund_info (
      fund_code TEXT PRIMARY KEY, fund_name TEXT, fund_type TEXT, theme TEXT, track_index_name TEXT);
    INSERT INTO fund_bot_daily_snapshots VALUES
      ('botx','2026-08-05','run-pilot',100,20,80,100,1,0,0,0,0.8,0,0,0.2,
       '[{"fund_code":"000001","weight":0.8,"market_value":80}]',0);
    INSERT INTO fund_bot_orders VALUES
      (7,NULL,'botx','000001','测试指数基金','buy','2026-08-05',NULL,40,NULL,NULL,20,40,0,
       '${reason}','confirmed','2026-08-05 15:00:00','run-pilot',NULL,'priced',NULL,NULL);
    INSERT INTO fund_info VALUES ('000001','测试指数基金','指数型','宽基','测试指数');
  `)
  fundDb.close()

  const outputDbPath = join(root, 'decision-episodes.db')
  const reportPath = join(root, 'report.md')
  const first = importDecisionEpisodes({
    worldRoot, fundDbPath, outputDbPath, reportPath, runId, botId,
    startDate: date, endDate: date, now: '2026-08-11T00:00:00.000Z',
  })
  assert.equal(first.episodesInserted, 1)
  assert.ok(first.claimsInserted >= 2)
  assert.ok(first.relationsInserted >= 2)

  const db = new DatabaseSync(outputDbPath, { readOnly: true })
  assert.equal((db.prepare('SELECT COUNT(*) n FROM episodes').get() as { n: number }).n, 1)
  const candidate = db.prepare(`
    SELECT observed_tool_outcome,reconciled_outcome,reconciled_final_action_id
    FROM candidate_actions
  `).get() as Record<string, unknown>
  assert.equal(candidate.observed_tool_outcome, 'transport_error')
  assert.equal(candidate.reconciled_outcome, 'database_confirmed')
  assert.ok(candidate.reconciled_final_action_id)

  const relation = db.prepare(`
    SELECT predicate,direction,mechanism,valid_from,certainty,source_locator
    FROM relations WHERE predicate='TRACKS_INDEX'
  `).get() as Record<string, unknown>
  assert.equal(relation.direction, 'fund_to_index')
  assert.equal(relation.certainty, 'hard_reference')
  assert.ok(relation.mechanism)
  assert.ok(relation.source_locator)

  const prediction = db.prepare(`
    SELECT certainty,confidence FROM claims WHERE claim_type='market_prediction'
  `).get() as Record<string, unknown>
  assert.equal(prediction.certainty, 'estimated')
  assert.equal(prediction.confidence, 0.48)
  const issueTypes = db.prepare('SELECT issue_type FROM quality_issues').all() as Array<{ issue_type: string }>
  assert.ok(issueTypes.some(row => row.issue_type === 'snapshot_timing_mismatch'))
  assert.ok(issueTypes.some(row => row.issue_type === 'transport_error_database_reconciled'))
  assert.ok(issueTypes.some(row => row.issue_type === 'invalid_historical_holding_rows'))
  db.close()

  const second = importDecisionEpisodes({
    worldRoot, fundDbPath, outputDbPath, reportPath, runId, botId,
    startDate: date, endDate: date, now: '2026-08-11T00:01:00.000Z',
  })
  assert.equal(second.episodesInserted, 0)
  assert.equal(second.episodesSkipped, 1)
  const verify = new DatabaseSync(outputDbPath, { readOnly: true })
  assert.equal((verify.prepare('SELECT COUNT(*) n FROM episodes').get() as { n: number }).n, 1)
  verify.close()
  rmSync(root, { recursive: true, force: true })
})
