import { existsSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { initializeCanonicalDecisionDatabase } from './canonical-schema.ts'

export const AUG05_CANONICAL_CASE = {
  identity: {
    schemaVersion: 'decision_episode_v0.1',
    caseId: 'bot105d_2026-08-05',
    extractionStatus: 'reconstructed_from_session_and_database',
    botId: 'bot105d',
    runId: 'bot105d-daily-agenticdeep-charter-0424',
    sessionId: '739ce809-e76d-440a-ad0d-8a5a168feae8',
    worldDate: '2026-08-05',
    decisionTime: null,
    decisionTimeReason: 'session只可靠保存交易日，没有统一的本地决策时间戳',
    knowledgeCutoff: '2026-08-05',
    knowledgeRule: '禁止访问未来世界日期',
    decisionSummary: '采用 v5 mainline 的防御·红利 regime，从沪深300切换到红利+价值双基金结构；科技单日反弹不足以覆盖硬规则。',
    sourceBundleHash: 'e4b3d83ea6bd6900e031eefeb8c08d1dce2afc8bad4eef4b3f07e3c7e390eb1a',
  },
  sources: [
    {
      id: 'SRC_SPEC', kind: 'acceptance_spec',
      path: '/home/rooot/.codex/attachments/c73fd6fe-a3f8-4567-b318-c5f8d5d66f23/pasted-text.txt',
      sha256: 'de5e3a4e23e60bdbabc2be8c4f293727cc28900ca18b0ac29cf2e64295564cdd', tables: [],
    },
    {
      id: 'SRC_INPUT', kind: 'session_input',
      path: 'runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-05/bot105d/sent.md',
      sha256: 'de2283ebfb1b4c4f3a78443f8d3a09ca56b360728565821a8aa7a4c821995809', tables: [],
    },
    {
      id: 'SRC_REPLY', kind: 'session_reply',
      path: 'runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-05/bot105d/reply.json',
      sha256: 'ba1110f4236526e4a876ff316790362d80843af62b31a1417c5b9f3cbe63b688', tables: [],
    },
    {
      id: 'SRC_CLOSE', kind: 'close_my_day',
      path: 'runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-05/bot105d/close_my_day.json',
      sha256: 'e6c61dcca1774707297fad050c6a0844cc6451891ab1f0234e926fe7e7b4cf3d', tables: [],
    },
    {
      id: 'SRC_DB', kind: 'portfolio_database', path: '/home/rooot/agent_invest_lab/data/fund.db', sha256: null,
      tables: ['fund_bot_daily_snapshots', 'fund_bot_actions', 'fund_bot_orders', 'fund_info'],
    },
  ],
  context: {
    accountBefore: {
      source: 'sent.md#账户快照', snapshotLabel: '今日settle后、决策前注入',
      initialCapital: 1_000_000, cashAvailable: 968_987, investedValue: 574_046,
      totalValue: 1_543_034, cumulativeReturnPct: 54.30, maxDrawdownPct: -15.00,
      holdings: [{
        instrument: 'fund:000051', name: '华夏沪深300ETF联接A', shares: 327_035.84,
        marketValue: 574_046, portfolioWeightPct: 37.20, unrealizedReturnPct: 12.96, holdingDays: 483,
      }],
    },
    constraints: [
      { constraintId: 'max_single_fund', rule: '单基金占权益比例不得超过75%', class: 'hard_policy' },
      { constraintId: 'minimum_fund_count', rule: '多基金组合至少持有两只基金', class: 'hard_policy' },
    ],
  },
  facts: [
    { id: 'F1', subject: 'index:CSI300', predicate: 'DISTANCE_TO_MA120', value: -4.15, unit: 'percent', eventTime: '2026-08-05', producer: 'v5_mainline', factClass: 'derived_fact', source: 'sent.md#mainline_rotation.Regime判定依据', reproducible: 'unknown', metadata: {} },
    { id: 'F2', subject: 'index:CSI300', predicate: 'DISTANCE_TO_MA120', value: -2.93, unit: 'percent', eventTime: '2026-08-05', producer: 'mainline_daily', factClass: 'derived_fact', source: 'sent.md#market_mainline.regime', reproducible: 'unknown', metadata: {} },
    { id: 'F3', subject: 'market:CN_equity', predicate: 'TOP15_DOMINANT_GROUP_COUNT', value: 3, unit: null, eventTime: '2026-08-05', producer: 'v5_mainline', factClass: 'derived_fact', source: 'sent.md#mainline_rotation.Regime判定依据', reproducible: null, metadata: {} },
    { id: 'F4', subject: 'index:STAR50', predicate: 'INTRADAY_RETURN', value: 4.71, unit: 'percent', eventTime: '2026-08-05T14:26:03+08:00', producer: null, factClass: 'market_observation', source: 'sent.md#盘中实时行情', reproducible: null, metadata: {} },
    { id: 'F5', subject: 'sector:semiconductor_equipment', predicate: 'INTRADAY_RETURN', value: 9.06, unit: 'percent', eventTime: '2026-08-05T14:26:03+08:00', producer: null, factClass: 'market_observation', source: 'sent.md#盘中实时行情', reproducible: null, metadata: {} },
    { id: 'F6', subject: 'macro:CN_PMI', predicate: 'HAS_VALUE', value: 49.2, unit: null, eventTime: '2026-07', producer: null, factClass: 'reported_macro_fact', source: 'sent.md#market_context', reproducible: null, metadata: { originalReleaseId: null } },
    { id: 'F7', subject: 'portfolio:bot105d', predicate: 'MAX_DRAWDOWN', value: -15.00, unit: 'percent', eventTime: '2026-08-03', producer: null, factClass: 'account_fact', source: 'sent.md#整体绩效', reproducible: null, metadata: {} },
  ],
  relations: [
    { id: 'R1', subject: 'fund:000051', predicate: 'TRACKS_INDEX', object: 'index:CSI300', direction: 'fund_to_index', mechanism: '指数复制/联接基金', validFrom: null, validTo: null, evidence: [{ database: 'fund_info', key: '000051' }], certainty: 'hard_reference' },
    { id: 'R2', subject: 'fund:012762', predicate: 'EXPOSED_TO', object: 'factor:dividend', direction: 'fund_to_factor', mechanism: '跟踪上证红利指数', validFrom: null, validTo: null, evidence: [{ database: 'fund_info', key: '012762' }, { source: 'sent.md#mainline_rotation.防御仓' }], certainty: 'hard_reference' },
    { id: 'R3', subject: 'fund:510030', predicate: 'EXPOSED_TO', object: 'factor:large_cap_value', direction: 'fund_to_factor', mechanism: '跟踪180价值指数', validFrom: null, validTo: null, evidence: [{ database: 'fund_info', key: '510030' }], certainty: 'hard_reference' },
    { id: 'R4', subject: 'regime:defensive_dividend', predicate: 'SELECTS', object: 'fund:012762', direction: 'regime_to_instrument', mechanism: '防御regime下退出主线并配置红利底线产品', validFrom: '2026-08-05', validTo: 'until_regime_reversal', evidence: [{ source: 'sent.md#mainline_rotation' }], certainty: 'policy_defined' },
    { id: 'R5', subject: 'policy:single_fund_max_75', predicate: 'REQUIRES_DIVERSIFICATION_WITH', object: 'fund:510030', direction: 'policy_to_action', mechanism: '仅持012762将构成权益仓100%单基金集中', validFrom: '2026-08-05', validTo: null, evidence: [{ toolCall: 'call_5f1ab2818c3d4a2890d4fdd1', result: 'rejected', message: '012762成交后将占权益100% > 75%' }], certainty: 'hard_execution_constraint' },
  ],
  claims: [
    {
      id: 'C1', statement: '当前市场已进入防御·红利regime', claimType: 'rule_derived_classification', status: 'accepted_for_execution', confidenceLabel: 'authoritative_for_this_bot', certainty: 'policy_defined',
      mechanism: ['HS300跌破MA120防御阈值', 'Top15动量缺少主导主题', '方法论第0层要求防御状态退出主线'],
      supportingEvidence: [
        { relation: 'SUPPORTS', factId: 'F1', explanation: 'v5口径下HS300较MA120为-4.15%，低于-3%阈值' },
        { relation: 'SUPPORTS', factId: 'F3', explanation: 'dominant_count=3，未达到强主线门槛4' },
        { relation: 'SUPPORTS', source: 'sent.md#mainline_rotation.今日动作', explanation: 'v5报告要求切换防御红利' },
      ],
      contradictingEvidence: [
        { relation: 'CONTRADICTS', factId: 'F2', explanation: '日度引擎口径为-2.93%，没有明显低于-3%' },
        { relation: 'CONTRADICTS', factId: 'F4', explanation: '科创50盘中上涨4.71%' },
        { relation: 'CONTRADICTS', factId: 'F5', explanation: '半导体设备盘中上涨9.06%' },
        { relation: 'CONTRADICTS', source: 'sent.md#market_context', explanation: 'risk_state为risk_on_overweight' },
      ],
      falsifiers: ['HS300连续至少5日站回MA120的-1%以上', 'Top15出现至少4个同组板块'],
    },
    {
      id: 'C2', statement: '当日科技上涨更可能是超跌反弹，尚不足以确认趋势反转', claimType: 'market_interpretation', status: 'active_at_decision', confidenceLabel: 'medium_low', certainty: 'estimated',
      mechanism: ['科技方向单日涨幅很强', 'HS300长期趋势仍未获得确认', '主线集中度不足', '单日反弹不满足连续确认条件'],
      supportingEvidence: [{ factId: 'F1' }, { factId: 'F3' }, { source: 'reply.json#/reply/关键观察/1' }],
      contradictingEvidence: [{ factId: 'F4' }, { factId: 'F5' }, { source: 'sent.md#market_context', explanation: '流动性宽松且risk_state偏多' }], falsifiers: [],
    },
    {
      id: 'C3', statement: '红利加价值的双基金结构比单独持有012762更符合当前宪章', claimType: 'portfolio_construction', status: 'accepted_for_execution', confidenceLabel: 'high', certainty: 'hard_constraint_plus_estimated_portfolio_design',
      mechanism: ['012762单独持有会占权益仓100%', '510030提供第二个价值风格载体', '35万与15万形成约70/30权益结构'],
      supportingEvidence: [
        { toolCall: 'call_5f1ab2818c3d4a2890d4fdd1', relation: 'SUPPORTS', explanation: '单基金集中度拒单' },
        { toolCall: 'call_1af1ef8167d44baf8e03c46b', relation: 'SUPPORTS', explanation: '510030订单成功受理' },
      ],
      contradictingEvidence: [], falsifiers: [],
    },
  ],
  conflicts: [
    {
      id: 'DC1',
      left: { producer: 'mainline_daily', recommendation: 'HOLD', basis: { HS300_vs_MA120: -2.93, regime_duration_days: 2 } },
      right: { producer: 'v5_mainline/mainline_rotation', recommendation: 'SWITCH_TO_DIVIDEND', basis: { HS300_vs_MA120: -4.15, dominant_count: 3 } },
      winner: 'v5_mainline/mainline_rotation', resolution: '系统输入将v5标记为权威组合骨架，Agent按其执行', resolutionClass: 'configured_precedence', unresolvedIssue: '两个引擎MA120值差异未在session中得到复算解释',
    },
    {
      id: 'DC2',
      left: { claim: 'C1', recommendation: '切换防御红利' },
      right: { claim: 'C2_counter_signal', observation: '科创50+4.71%，半导体设备+9.06%', implied_recommendation: '暂缓防御切换或考虑科技反转' },
      winner: 'C1', resolution: '单日反弹未满足覆盖硬规则的四类闸门', resolutionClass: 'policy_over_estimated_claim', unresolvedIssue: null,
    },
  ],
  candidateActions: [
    { id: 'A1', action: 'SELL fund:000051', requestedShares: 327_035.84, requestedAmount: null, result: 'rejected', orderId: null, sourceToolCall: 'call_3b77aa93bd6b410cafdd8871', reason: '请求份额略高于实际可卖份额327035.8382', executionNote: null },
    { id: 'A2', action: 'SELL fund:000051', requestedShares: 327_000, requestedAmount: null, result: 'accepted', orderId: '7163', sourceToolCall: 'call_9adb6586f01f4ebeb3dba074', reason: null, executionNote: null },
    { id: 'A3', action: 'BUY fund:012762', requestedShares: null, requestedAmount: 574_000, result: 'rejected', orderId: null, sourceToolCall: 'call_5f1ab2818c3d4a2890d4fdd1', reason: '成交后单基金占权益100%，违反75%上限', executionNote: null },
    { id: 'A4', action: 'BUY fund:012762', requestedShares: null, requestedAmount: 350_000, result: 'accepted', orderId: '7164', sourceToolCall: null, reason: null, executionNote: 'session中部分调用返回传输失败；后续duplicate响应和fund.db均确认order 7164已经存在，因此以数据库为最终执行事实' },
    { id: 'A5', action: 'BUY fund:510030', requestedShares: null, requestedAmount: 150_000, result: 'accepted', orderId: '7165', sourceToolCall: 'call_1af1ef8167d44baf8e03c46b', reason: null, executionNote: null },
  ],
  finalDecision: {
    decision: 'rotate_from_hs300_to_dividend_value',
    targetStructure: { equityWeightPctEstimatedAtDecision: 32, cashWeightPctEstimatedAtDecision: 68, withinEquity: { 'fund:012762': 'approximately_70_percent', 'fund:510030': 'approximately_30_percent' } },
    rationale: ['采用v5 mainline的防御·红利regime', '科技单日反弹不足以覆盖趋势规则', '单基金75%上限要求加入第二只防御基金', '账户最大回撤已接近-15%，不扩大总权益风险'],
  },
  actualOrders: [
    { orderId: '7163', action: 'SELL', instrument: 'fund:000051', requestedAmount: null, requestedShares: 327_000, confirmedAmount: 579_449.61, confirmedShares: null, fee: 1_269.69, orderStatusAtDecision: 'pending', databaseStatusNow: 'confirmed' },
    { orderId: '7164', action: 'BUY', instrument: 'fund:012762', requestedAmount: 350_000, requestedShares: null, confirmedAmount: null, confirmedShares: 261_632.948409, fee: 0, orderStatusAtDecision: 'pending', databaseStatusNow: 'confirmed' },
    { orderId: '7165', action: 'BUY', instrument: 'fund:510030', requestedAmount: 150_000, requestedShares: null, confirmedAmount: null, confirmedShares: 52_413.703458, fee: 0, orderStatusAtDecision: 'pending', databaseStatusNow: 'confirmed' },
  ],
  warnings: [
    '两套引擎对HS300相对MA120给出-2.93%和-4.15%，不可合并',
    'session缺少每个预生成报告的独立版本ID和内容hash',
    'PMI等宏观数据缺少原始发布记录ID',
    '关系有效期大多没有显式valid_from/valid_to',
    '订单created_at由SQLite生成，但时区没有写入字段',
    '部分成功下单在客户端表现为传输失败，必须用订单数据库对账',
    '“红利/价值具有防御属性”包含策略判断，不能标成纯硬事实',
    '最终回复中的belief数值与mem0_add中的belief数值不完全一致',
  ],
} as const


const FOLLOWUP_CONSTRAINTS = [
  { constraintId: "max_single_fund", rule: "单基金占权益比例不得超过75%", class: "hard_policy" },
  { constraintId: "regime_confirmation", rule: "解除防御需HS300连续至少5日站回MA120的-1%以上，并由Top15集中度确认", class: "hard_policy" },
  { constraintId: "anti_churn", rule: "同向交易5日内不得重复执行，避免拆单与追涨杀跌", class: "hard_policy" },
]

function followupCase(config: any): any {
  return {
    identity: {
      schemaVersion: "decision_episode_v0.1", caseId: `bot105d_${config.date}`,
      extractionStatus: "canonical_hybrid_validated", botId: "bot105d",
      runId: "bot105d-daily-agenticdeep-charter-0424", sessionId: config.sessionId,
      worldDate: config.date, decisionTime: null,
      decisionTimeReason: "session只可靠保存交易日，没有统一的本地决策时间戳",
      knowledgeCutoff: config.date, knowledgeRule: "禁止访问未来世界日期",
      decisionSummary: config.summary, sourceBundleHash: config.bundleHash,
    },
    sources: config.sources,
    context: { accountBefore: config.account, constraints: FOLLOWUP_CONSTRAINTS },
    facts: config.facts,
    relations: config.relations,
    claims: config.claims,
    conflicts: config.conflicts,
    candidateActions: [{ id: `${config.prefix}A1`, action: "HOLD portfolio:bot105d", requestedShares: null, requestedAmount: null, result: "accepted", orderId: null, sourceToolCall: null, reason: config.holdReason, executionNote: "当日没有新订单；HOLD表示维持既有持仓，不等同于交易成交。" }],
    finalDecision: {
      decision: "hold_defensive_dividend_value",
      targetStructure: { equityWeightPctEstimatedAtDecision: config.equityWeight, cashWeightPctEstimatedAtDecision: config.cashWeight, withinEquity: { "fund:012762": "approximately_70_percent", "fund:510030": "approximately_30_percent" } },
      rationale: config.rationale,
    },
    actualOrders: [],
    warnings: config.warnings,
  }
}

export const ADDITIONAL_CANONICAL_CASES = [
  followupCase({
    date: "2026-08-06", prefix: "D06", sessionId: "e5acb019-f515-44e4-970c-3243327e912d",
    bundleHash: "8ee3e220c0ceabca8a81b83fb319268fb2167af21080b87a5ae8e233b09b0e5b",
    summary: "维持防御·红利配置并执行HOLD；日度与v5引擎对HS300距MA120给出-1.72%和约-3.8%的不同口径，但两者都未满足连续5日站回-1%以上的解除条件。",
    sources: [
      { id: "D06_SRC_INPUT", kind: "session_input", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-06/bot105d/sent.md", sha256: "9753b260cb7d5acb2b996fb3f8cceec07c0ee8646c8a3952cd28a2994a45f65b", tables: [] },
      { id: "D06_SRC_REPLY", kind: "session_reply", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-06/bot105d/reply.json", sha256: "cbb70a653ed8457cdb59e59d1cd7fafdafd7f8464f648ccbda008d08bfad30a8", tables: [] },
      { id: "D06_SRC_CLOSE", kind: "close_my_day", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-06/bot105d/close_my_day.json", sha256: "2337de66b7a8ef64bec4a8a9c619cbdf4afc5613f1eb009f74e7757a05bc09c7", tables: [] },
      { id: "D06_SRC_DB", kind: "portfolio_database", path: "/home/rooot/agent_invest_lab/data/fund.db", sha256: null, tables: ["fund_bot_daily_snapshots", "fund_bot_actions", "fund_bot_orders", "fund_info"] },
      { id: "D06_SRC_QWEN", kind: "qwen_candidate", path: "runtime/decision-episodes/qwen3.7-plus/bot105d_2026-08-06.validated.candidate.json", sha256: "da06958d927b27669ef0e50e7d2174f9258bcdfe9a2a2eefaf0871603870afdf", tables: [] },
    ],
    account: { source: "sent.md#账户快照", snapshotLabel: "2026-08-05 settle后、8月6日决策前注入", initialCapital: 1000000, cashAvailable: 1048437, investedValue: 500064, totalValue: 1548501, cumulativeReturnPct: 54.85, maxDrawdownPct: -15.00, holdings: [
      { instrument: "fund:000051", name: "华夏沪深300ETF联接A", shares: 35.84, marketValue: 64, portfolioWeightPct: 0.00, unrealizedReturnPct: 14.29, holdingDays: 484 },
      { instrument: "fund:012762", name: "华泰柏瑞上证红利ETF联接C", shares: 261632.95, marketValue: 350000, portfolioWeightPct: 22.60, unrealizedReturnPct: 0.00, holdingDays: 0 },
      { instrument: "fund:510030", name: "价值ETF华宝", shares: 52413.70, marketValue: 150000, portfolioWeightPct: 9.69, unrealizedReturnPct: 0.00, holdingDays: 0 },
    ] },
    equityWeight: 32.29, cashWeight: 67.71,
    facts: [
      { id: "D06F1", subject: "index:CSI300", predicate: "DISTANCE_TO_MA120", value: -1.72, unit: "percent", eventTime: "2026-08-06", producer: "mainline_daily", factClass: "derived_fact", source: "sent.md#market_mainline.regime", reproducible: "unknown", metadata: {} },
      { id: "D06F2", subject: "index:CSI300", predicate: "DISTANCE_TO_MA120", value: -3.8, unit: "percent", eventTime: "2026-08-06", producer: "v5_mainline", factClass: "derived_fact", source: "sent.md#mainline_rotation.Regime判定依据", reproducible: "unknown", metadata: { approximate: true } },
      { id: "D06F3", subject: "market:CN_equity", predicate: "TOP15_DOMINANT_GROUP_COUNT", value: 3, unit: null, eventTime: "2026-08-06", producer: "v5_mainline", factClass: "derived_fact", source: "sent.md#mainline_rotation.Top15集中度", reproducible: null, metadata: {} },
      { id: "D06F4", subject: "macro:CN_PMI", predicate: "HAS_VALUE", value: 49.2, unit: "index", eventTime: "2026-07", producer: null, factClass: "reported_macro_fact", source: "sent.md#market_context", reproducible: null, metadata: {} },
      { id: "D06F5", subject: "macro:CN_GDP", predicate: "YOY_GROWTH", value: 4.3, unit: "percent", eventTime: "2026-Q2", producer: null, factClass: "reported_macro_fact", source: "sent.md#market_context", reproducible: null, metadata: {} },
      { id: "D06F6", subject: "bond:CN10Y", predicate: "YIELD", value: 1.71, unit: "percent", eventTime: "2026-08-06", producer: null, factClass: "market_observation", source: "sent.md#market_context", reproducible: null, metadata: {} },
      { id: "D06F7", subject: "portfolio:bot105d", predicate: "EQUITY_WEIGHT", value: 32.29, unit: "percent", eventTime: "2026-08-06", producer: "session_snapshot", factClass: "account_fact", source: "reply.json#/reply/今日动作", reproducible: null, metadata: {} },
    ],
    relations: [
      { id: "D06R1", subject: "index:CSI300", predicate: "DETERMINES", object: "regime:defensive_dividend", direction: "signal_to_regime", mechanism: "HS300相对MA120的位置决定是否触发或解除防御；当日两套引擎口径不同，因此保留两值并使用既定优先级。", validFrom: "2026-08-06", validTo: null, evidence: [{ factId: "D06F1" }, { factId: "D06F2" }], certainty: "policy_defined" },
      { id: "D06R2", subject: "market:CN_equity", predicate: "VALIDATES", object: "regime:defensive_dividend", direction: "market_to_regime", mechanism: "Top15主导组数量只有3，未达到4个板块的强主线确认门槛。", validFrom: "2026-08-06", validTo: null, evidence: [{ factId: "D06F3" }], certainty: "policy_defined" },
      { id: "D06R3", subject: "regime:defensive_dividend", predicate: "SELECTS", object: "fund:012762", direction: "regime_to_instrument", mechanism: "防御·红利Regime继续使用上证红利ETF联接C作为底线防御产品。", validFrom: "2026-08-06", validTo: null, evidence: [{ source: "sent.md#mainline_rotation.今日动作" }], certainty: "policy_defined" },
      { id: "D06R4", subject: "policy:anti_churn_5d", predicate: "BLOCKS", object: "action:increase_position", direction: "policy_to_action", mechanism: "012762和510030于前一日建仓，5日同向交易限制阻止立即加仓。", validFrom: "2026-08-06", validTo: "2026-08-10", evidence: [{ source: "reply.json#/reply/今日动作" }], certainty: "hard_execution_constraint" },
    ],
    claims: [
      { id: "D06C1", statement: "防御·红利Regime继续有效，但HS300距MA120存在-1.72%与约-3.8%两套计算口径。", claimType: "rule_derived_classification", status: "accepted_for_execution", confidenceLabel: "authoritative_for_this_bot", certainty: "policy_defined", mechanism: ["日度引擎落在迟滞带内，不能直接解除既有防御状态", "v5引擎仍低于-3%防御线", "Top15集中度只有3，未确认强主线"], supportingEvidence: [{ relation: "SUPPORTS", factId: "D06F2" }, { relation: "SUPPORTS", factId: "D06F3" }], contradictingEvidence: [{ relation: "CONTRADICTS", factId: "D06F1", explanation: "日度引擎并未低于-3%触发线，但仍未站回-1%解除线" }], falsifiers: ["HS300连续至少5日站回MA120的-1%以上", "Top15同一主题至少4个板块进入前15"] },
      { id: "D06C2", statement: "增长放缓、PMI收缩和债券走强共同支持risk_off判断。", claimType: "market_interpretation", status: "active_at_decision", confidenceLabel: "medium", certainty: "estimated", mechanism: ["PMI低于荣枯线且GDP增速降至4.3%", "10年国债收益率降至约1.71%，资金偏好防守", "弱增长与宽流动性尚未转化为权益趋势"], supportingEvidence: [{ factId: "D06F4" }, { factId: "D06F5" }, { factId: "D06F6" }], contradictingEvidence: [{ source: "sent.md#market_context", explanation: "PPI阶段性回升，存在再通胀反向信号" }], falsifiers: ["PMI重回50以上、CPI温和回升且社融同步改善"] },
      { id: "D06C3", statement: "当日应HOLD，维持约32%权益仓位，不新增进攻仓。", claimType: "portfolio_construction", status: "accepted_for_execution", confidenceLabel: "high", certainty: "hard_constraint_plus_estimated_portfolio_design", mechanism: ["Regime未解除", "新建仓处于5日防拆单期", "账户刚经历-15%最大回撤，优先控制风险"], supportingEvidence: [{ factId: "D06F7" }, { source: "reply.json#/reply/今日动作", explanation: "明确输出HOLD" }], contradictingEvidence: [], falsifiers: ["Regime完成解除确认且防拆单期结束"] },
    ],
    conflicts: [
      { id: "D06DC1", left: { producer: "mainline_daily", HS300_vs_MA120: -1.72 }, right: { producer: "v5_mainline", HS300_vs_MA120: -3.8, approximate: true }, winner: "v5_mainline用于组合骨架；两值同时保留", resolution: "系统将v5设为组合骨架的权威来源，但不删除日度引擎数值。两种口径都未满足连续5日站回-1%以上的解除条件。", resolutionClass: "configured_precedence", unresolvedIssue: "MA120计算窗口或数据源差异未在session中解释" },
      { id: "D06DC2", left: { signal: "ERP估值吸引力" }, right: { signal: "趋势弱、增长放缓" }, winner: "趋势与Regime纪律", resolution: "便宜不等于立即上涨；在弱增长环境中优先遵守趋势与防御规则。", resolutionClass: "policy_over_estimated_signal", unresolvedIssue: null },
    ],
    holdReason: "Regime未解除，且前一日新建仓仍处于5日同向交易限制。",
    rationale: ["防御·红利Regime未满足解除条件", "Top15缺少强主线集中度", "新建仓处于防拆单期", "账户刚经历-15%最大回撤"],
    warnings: ["HS300距MA120存在-1.72%与约-3.8%两套口径，禁止合并", "两套MA120口径的计算基准未在session中解释", "当日HOLD没有产生新订单"],
  }),
  followupCase({
    date: "2026-08-07", prefix: "D07", sessionId: "48b35e91-126a-474b-9c82-4d7283ac29ec",
    bundleHash: "17c767045bbee3d629253fa5b1a85e6200dbabeb742a4fabc80feb4db531d085",
    summary: "HS300距MA120为-1.86%，位于-3%至-1%的迟滞带内而非跌破下沿；既有防御状态尚未满足解除确认，叠加回撤警戒和防拆单约束，当日执行HOLD。",
    sources: [
      { id: "D07_SRC_INPUT", kind: "session_input", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-07/bot105d/sent.md", sha256: "78186b23c8bf2b6522bd2db8c8151f60151615f5f63c89f713dda3de5d8418d3", tables: [] },
      { id: "D07_SRC_REPLY", kind: "session_reply", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-07/bot105d/reply.json", sha256: "ba6465fc4a93761eb4f44977efd43f5d75cecfc6f027e26f052ef11571e745ff", tables: [] },
      { id: "D07_SRC_CLOSE", kind: "close_my_day", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-07/bot105d/close_my_day.json", sha256: "ecb30782884b27cade2559907062c0616a65bf1de6b1c9261b359308af10c395", tables: [] },
      { id: "D07_SRC_DB", kind: "portfolio_database", path: "/home/rooot/agent_invest_lab/data/fund.db", sha256: null, tables: ["fund_bot_daily_snapshots", "fund_bot_actions", "fund_bot_orders", "fund_info"] },
      { id: "D07_SRC_QWEN", kind: "qwen_candidate", path: "runtime/decision-episodes/qwen3.7-plus/bot105d_2026-08-07.validated.candidate.json", sha256: "2e3c4c019f9a2c007c80e05a967144475ab16a430e1e9da7b113321d3822afda", tables: [] },
    ],
    account: { source: "sent.md#账户快照", snapshotLabel: "2026-08-06 settle后、8月7日决策前注入", initialCapital: 1000000, cashAvailable: 1048437, investedValue: 506617, totalValue: 1555054, cumulativeReturnPct: 55.51, maxDrawdownPct: -15.00, holdings: [
      { instrument: "fund:000051", name: "华夏沪深300ETF联接A", shares: 35.84, marketValue: 64, portfolioWeightPct: 0.00, unrealizedReturnPct: 14.13, holdingDays: 485 },
      { instrument: "fund:012762", name: "华泰柏瑞上证红利ETF联接C", shares: 261632.95, marketValue: 356266, portfolioWeightPct: 22.91, unrealizedReturnPct: 1.79, holdingDays: 1 },
      { instrument: "fund:510030", name: "价值ETF华宝", shares: 52413.70, marketValue: 150287, portfolioWeightPct: 9.66, unrealizedReturnPct: 0.19, holdingDays: 1 },
    ] },
    equityWeight: 32.57, cashWeight: 67.43,
    facts: [
      { id: "D07F1", subject: "index:CSI300", predicate: "DISTANCE_TO_MA120", value: -1.86, unit: "percent", eventTime: "2026-08-07", producer: "mainline_daily", factClass: "derived_fact", source: "sent.md#mainline_rotation.Regime判定依据", reproducible: "unknown", metadata: { hysteresisBand: [-3, -1] } },
      { id: "D07F2", subject: "market:CN_equity", predicate: "TOP15_DOMINANT_GROUP_COUNT", value: 3, unit: null, eventTime: "2026-08-07", producer: "v5_mainline", factClass: "derived_fact", source: "sent.md#mainline_rotation.Top15集中度", reproducible: null, metadata: {} },
      { id: "D07F3", subject: "macro:CN_PMI", predicate: "HAS_VALUE", value: 49.2, unit: "index", eventTime: "2026-07", producer: null, factClass: "reported_macro_fact", source: "sent.md#market_context", reproducible: null, metadata: {} },
      { id: "D07F4", subject: "model:hs300_erp_vix_timing", predicate: "SUGGESTED_EQUITY_WEIGHT", value: 78.5, unit: "percent", eventTime: "2026-08-07", producer: "quant_factor", factClass: "derived_fact", source: "reply.json#/reply/今日动作", reproducible: "unknown", metadata: {} },
      { id: "D07F5", subject: "portfolio:bot105d", predicate: "CURRENT_DRAWDOWN", value: -13.96, unit: "percent", eventTime: "2026-08-07", producer: "session_snapshot", factClass: "account_fact", source: "sent.md#整体绩效", reproducible: null, metadata: {} },
      { id: "D07F6", subject: "portfolio:bot105d", predicate: "EQUITY_WEIGHT", value: 32.57, unit: "percent", eventTime: "2026-08-07", producer: "session_snapshot", factClass: "account_fact", source: "reply.json#/reply/持仓与仓位", reproducible: null, metadata: {} },
      { id: "D07F7", subject: "market:VIX", predicate: "LEVEL", value: 19.93, unit: "index", eventTime: "2026-08-07", producer: null, factClass: "market_observation", source: "reply.json#/reply/今日动作", reproducible: null, metadata: {} },
    ],
    relations: [
      { id: "D07R1", subject: "index:CSI300", predicate: "DETERMINES", object: "regime:defensive_dividend", direction: "signal_to_regime", mechanism: "-1.86%位于-3%触发线与-1%解除线之间；迟滞规则要求保持原状态，不能描述为再次跌破-3%。", validFrom: "2026-08-07", validTo: null, evidence: [{ factId: "D07F1" }], certainty: "policy_defined" },
      { id: "D07R2", subject: "market:CN_equity", predicate: "VALIDATES", object: "regime:defensive_dividend", direction: "market_to_regime", mechanism: "Top15主导组数量为3，尚未达到4个板块的主线确认门槛。", validFrom: "2026-08-07", validTo: null, evidence: [{ factId: "D07F2" }], certainty: "policy_defined" },
      { id: "D07R3", subject: "policy:regime_precedence", predicate: "OVERRIDES", object: "model:hs300_erp_vix_timing", direction: "policy_to_signal", mechanism: "防御Regime和回撤警戒优先于量化模型的78.5%建议仓位。", validFrom: "2026-08-07", validTo: null, evidence: [{ factId: "D07F4" }, { factId: "D07F5" }], certainty: "hard_execution_constraint" },
      { id: "D07R4", subject: "regime:defensive_dividend", predicate: "SELECTS", object: "fund:012762", direction: "regime_to_instrument", mechanism: "在解除条件满足前继续持有红利底线产品，不追逐短期成长反弹。", validFrom: "2026-08-07", validTo: null, evidence: [{ source: "reply.json#/reply/今日动作" }], certainty: "policy_defined" },
    ],
    claims: [
      { id: "D07C1", statement: "HS300距MA120为-1.86%，处于-3%至-1%迟滞带内；既有防御·红利Regime继续维持。", claimType: "rule_derived_classification", status: "accepted_for_execution", confidenceLabel: "authoritative_for_this_bot", certainty: "policy_defined", mechanism: ["-1.86%没有跌破-3%下沿", "但尚未站回-1%解除线", "Top15集中度3也未达到主线确认门槛4"], supportingEvidence: [{ factId: "D07F1" }, { factId: "D07F2" }], contradictingEvidence: [{ factId: "D07F7", explanation: "VIX回落显示恐慌边际缓解" }], falsifiers: ["HS300连续5日站回MA120的-1%以上，且Top15同组数量达到4"] },
      { id: "D07C2", statement: "当日应HOLD并维持约32.6%权益仓位，不能按量化模型建议加至78.5%。", claimType: "portfolio_construction", status: "accepted_for_execution", confidenceLabel: "high", certainty: "hard_constraint_plus_estimated_portfolio_design", mechanism: ["当前回撤-13.96%，靠近-15%风控线", "012762和510030仍在5日同向交易限制内", "Regime纪律优先于量化仓位建议"], supportingEvidence: [{ factId: "D07F5" }, { factId: "D07F6" }, { source: "reply.json#/reply/今日动作" }], contradictingEvidence: [{ factId: "D07F4", explanation: "量化因子建议78.5%权益" }], falsifiers: ["Regime解除、回撤明显收窄且防拆单期结束"] },
      { id: "D07C3", statement: "市场更接近震荡而非单边趋势，risk_state为neutral。", claimType: "market_interpretation", status: "active_at_decision", confidenceLabel: "medium", certainty: "estimated", mechanism: ["PMI低于荣枯线，增长偏弱", "VIX持续回落，恐慌边际缓解", "正负信号并存，无法确认单一主导趋势"], supportingEvidence: [{ factId: "D07F3" }, { factId: "D07F7" }], contradictingEvidence: [{ source: "sent.md#market_context", explanation: "政策托底可能缩小进一步下探空间" }], falsifiers: ["出现连续的趋势性上涨或下跌并得到宏观数据确认"] },
    ],
    conflicts: [
      { id: "D07DC1", left: { producer: "quant_factor", suggested_equity_weight_pct: 78.5 }, right: { regime: "防御·红利", actual_equity_weight_pct: 32.57, current_drawdown_pct: -13.96 }, winner: "Regime防御规则与账户风控", resolution: "Regime优先，且账户接近-15%风控线并处于防拆单期，因此维持低仓位HOLD。", resolutionClass: "hard_constraint_over_model_signal", unresolvedIssue: "若指数快速反弹，确认窗口可能带来短期踏空" },
      { id: "D07DC2", left: { signal: "VIX回落，恐慌缓解" }, right: { signal: "PMI收缩，增长偏弱" }, winner: "维持neutral与防御，不做方向性加仓", resolution: "两类信号互相抵消，不能仅凭VIX回落改变Regime。", resolutionClass: "deferred_until_confirmation", unresolvedIssue: "风险偏好改善能否持续需要后续数据确认" },
    ],
    holdReason: "HS300仍在迟滞带内，账户回撤接近风控线，且持仓处于5日同向交易限制。",
    rationale: ["-1.86%位于迟滞带内而非解除区", "Top15集中度未达到主线门槛", "账户回撤接近-15%风控线", "防拆单约束仍有效"],
    warnings: ["Qwen候选曾把-1.86%错述为跌破-3%下沿，Canonical已更正", "量化仓位建议与Regime目标冲突，禁止直接合并", "VIX原始数据表未在输入工件中单独登记"],
  }),
  followupCase({
    date: "2026-08-10", prefix: "D10", sessionId: "9622203d-39a7-4d64-bdf0-cc62b21863cb",
    bundleHash: "1a00a709ad7f53a1dd43608dcafbafa863d17cd70ef44b51da009212eab397e7",
    summary: "日度引擎已给出HS300距MA120为-0.96%的初步解除信号，但仅完成1/5日确认；v5引擎仍为-4.15%。在口径未解决且确认窗口未完成时，继续防御·红利并HOLD。",
    sources: [
      { id: "D10_SRC_INPUT", kind: "session_input", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-10/bot105d/sent.md", sha256: "12fd4d04b8c0f1583fde47e266af7cc0a31ccc58b7e76612983b55e24dfbde0c", tables: [] },
      { id: "D10_SRC_REPLY", kind: "session_reply", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-10/bot105d/reply.json", sha256: "bf2eb37c23f63b9a2359d3027e4f0f46011c522d38a603965c4abc35ec90fcf1", tables: [] },
      { id: "D10_SRC_CLOSE", kind: "close_my_day", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-10/bot105d/close_my_day.json", sha256: "7a083930e6661662ce7f0de3d7153797f28b07916fa06c27b40f173beb070d76", tables: [] },
      { id: "D10_SRC_DB", kind: "portfolio_database", path: "/home/rooot/agent_invest_lab/data/fund.db", sha256: null, tables: ["fund_bot_daily_snapshots", "fund_bot_actions", "fund_bot_orders", "fund_info"] },
      { id: "D10_SRC_QWEN", kind: "qwen_candidate", path: "runtime/decision-episodes/qwen3.7-plus/bot105d_2026-08-10.validated.candidate.json", sha256: "b4e8e2cb6f7d8220232a7306218a31b448810391b7ab9aa5cb30aff20ed56089", tables: [] },
    ],
    account: { source: "sent.md#账户快照", snapshotLabel: "2026-08-07 settle后、8月10日决策前注入", initialCapital: 1000000, cashAvailable: 1048437, investedValue: 505899, totalValue: 1554336, cumulativeReturnPct: 55.43, maxDrawdownPct: -15.00, holdings: [
      { instrument: "fund:000051", name: "华夏沪深300ETF联接A", shares: 35.84, marketValue: 64, portfolioWeightPct: 0.00, unrealizedReturnPct: 15.16, holdingDays: 486 },
      { instrument: "fund:012762", name: "华泰柏瑞上证红利ETF联接C", shares: 261632.95, marketValue: 355978, portfolioWeightPct: 22.90, unrealizedReturnPct: 1.71, holdingDays: 2 },
      { instrument: "fund:510030", name: "价值ETF华宝", shares: 52413.70, marketValue: 149857, portfolioWeightPct: 9.64, unrealizedReturnPct: -0.10, holdingDays: 2 },
    ] },
    equityWeight: 32.54, cashWeight: 67.46,
    facts: [
      { id: "D10F1", subject: "index:CSI300", predicate: "DISTANCE_TO_MA120", value: -0.96, unit: "percent", eventTime: "2026-08-10", producer: "mainline_daily", factClass: "derived_fact", source: "sent.md#market_mainline.regime", reproducible: "unknown", metadata: { confirmationProgress: "1/5" } },
      { id: "D10F2", subject: "index:CSI300", predicate: "DISTANCE_TO_MA120", value: -4.15, unit: "percent", eventTime: "2026-08-10", producer: "v5_mainline", factClass: "derived_fact", source: "sent.md#mainline_rotation.Regime判定依据", reproducible: "unknown", metadata: {} },
      { id: "D10F3", subject: "market:CN_equity", predicate: "TOP15_DOMINANT_GROUP_COUNT", value: 4, unit: null, eventTime: "2026-08-10", producer: "mainline_daily", factClass: "derived_fact", source: "sent.md#market_mainline.regime", reproducible: null, metadata: {} },
      { id: "D10F4", subject: "market:CN_equity", predicate: "TOP15_DOMINANT_GROUP_COUNT", value: 3, unit: null, eventTime: "2026-08-10", producer: "v5_mainline", factClass: "derived_fact", source: "sent.md#mainline_rotation.Top15集中度", reproducible: null, metadata: {} },
      { id: "D10F5", subject: "macro:CN_PMI", predicate: "HAS_VALUE", value: 49.2, unit: "index", eventTime: "2026-07", producer: null, factClass: "reported_macro_fact", source: "sent.md#market_context", reproducible: null, metadata: {} },
      { id: "D10F6", subject: "market:VIX", predicate: "LEVEL", value: 18.9, unit: "index", eventTime: "2026-08-10", producer: null, factClass: "market_observation", source: "reply.json#/reply/市场环境判断", reproducible: null, metadata: {} },
      { id: "D10F7", subject: "market:CN_equity", predicate: "MARKET_TEMPERATURE", value: 42.54, unit: "index", eventTime: "2026-08-10", producer: null, factClass: "market_observation", source: "reply.json#/reply/市场环境判断", reproducible: null, metadata: {} },
      { id: "D10F8", subject: "portfolio:bot105d", predicate: "EQUITY_WEIGHT", value: 32.54, unit: "percent", eventTime: "2026-08-10", producer: "session_snapshot", factClass: "account_fact", source: "reply.json#/reply/今日动作", reproducible: null, metadata: {} },
      { id: "D10F9", subject: "portfolio:bot105d", predicate: "CURRENT_DRAWDOWN", value: -14.00, unit: "percent", eventTime: "2026-08-10", producer: "session_snapshot", factClass: "account_fact", source: "sent.md#整体绩效", reproducible: null, metadata: {} },
    ],
    relations: [
      { id: "D10R1", subject: "index:CSI300", predicate: "DETERMINES", object: "regime:defensive_dividend", direction: "signal_to_regime", mechanism: "日度引擎出现-0.96%的初步解除信号，但只完成1/5日确认；v5仍为-4.15%，因此不能立即切换。", validFrom: "2026-08-10", validTo: null, evidence: [{ factId: "D10F1" }, { factId: "D10F2" }], certainty: "policy_defined" },
      { id: "D10R2", subject: "market:CN_equity", predicate: "VALIDATES", object: "regime:switch_candidate", direction: "market_to_regime", mechanism: "日度Top15集中度达到4，而v5仍为3；信号改善但不同引擎尚未一致。", validFrom: "2026-08-10", validTo: null, evidence: [{ factId: "D10F3" }, { factId: "D10F4" }], certainty: "estimated" },
      { id: "D10R3", subject: "policy:five_day_confirmation", predicate: "BLOCKS", object: "action:switch_regime", direction: "policy_to_action", mechanism: "解除信号必须连续保持5日，当前仅为第1日，因此阻止立即换仓。", validFrom: "2026-08-10", validTo: null, evidence: [{ factId: "D10F1" }], certainty: "hard_execution_constraint" },
      { id: "D10R4", subject: "regime:defensive_dividend", predicate: "SELECTS", object: "fund:012762", direction: "regime_to_instrument", mechanism: "确认窗口完成前继续持有红利与价值组合，不因单日改善追涨。", validFrom: "2026-08-10", validTo: null, evidence: [{ source: "reply.json#/reply/今日动作" }], certainty: "policy_defined" },
    ],
    claims: [
      { id: "D10C1", statement: "防御·红利Regime暂时维持：日度引擎出现-0.96%的初步解除信号，但仅完成1/5日确认，且v5仍为-4.15%。", claimType: "rule_derived_classification", status: "accepted_for_execution", confidenceLabel: "authoritative_for_this_bot", certainty: "policy_defined", mechanism: ["日度信号首次站上-1%解除线附近", "解除需要连续5日确认，当前只有1日", "v5引擎和Top15口径仍未与日度引擎收敛"], supportingEvidence: [{ factId: "D10F2" }, { factId: "D10F4" }, { source: "sent.md#market_mainline.regime", explanation: "原始信号抱主线v4仅1/5日" }], contradictingEvidence: [{ factId: "D10F1" }, { factId: "D10F3" }], falsifiers: ["日度解除信号连续保持5日且不同引擎趋于一致"] },
      { id: "D10C2", statement: "市场处于震荡修复，情绪改善快于基本面，尚不能视为趋势反转。", claimType: "market_interpretation", status: "active_at_decision", confidenceLabel: "medium", certainty: "estimated", mechanism: ["VIX降至18.9且市场温度升至42.54，恐慌明显消退", "PMI仍为49.2，增长处于收缩区间", "成长风格反弹尚未获得基本面与多引擎共同确认"], supportingEvidence: [{ factId: "D10F5" }, { factId: "D10F6" }, { factId: "D10F7" }], contradictingEvidence: [{ source: "sent.md#market_context", explanation: "创业板周涨幅6.55%，短期成长反弹较强" }], falsifiers: ["PMI重回50以上且趋势信号在多个引擎中持续确认"] },
      { id: "D10C3", statement: "当日应HOLD，维持约32.5%权益与67.5%现金，不新增、不切换。", claimType: "portfolio_construction", status: "accepted_for_execution", confidenceLabel: "high", certainty: "hard_constraint_plus_estimated_portfolio_design", mechanism: ["Regime解除确认窗口未完成", "不同引擎指标差异很大", "账户当前回撤约-14%，不适合依据单日信号提高风险"], supportingEvidence: [{ factId: "D10F8" }, { factId: "D10F9" }, { source: "reply.json#/reply/今日动作" }], contradictingEvidence: [{ factId: "D10F1", explanation: "日度引擎出现初步解除信号" }], falsifiers: ["解除信号完成5日确认且账户风险允许切换"] },
    ],
    conflicts: [
      { id: "D10DC1", left: { producer: "mainline_daily", HS300_vs_MA120: -0.96, confirmation: "1/5", dominant_count: 4 }, right: { producer: "v5_mainline", HS300_vs_MA120: -4.15, dominant_count: 3 }, winner: "维持当前防御状态，等待确认", resolution: "不强行合并指标；由于日度信号仅1/5日，按确认窗口维持原Regime。", resolutionClass: "confirmation_window_over_raw_signal", unresolvedIssue: "两套引擎MA120与Top15分组口径差异未复算解释" },
      { id: "D10DC2", left: { signal: "成长与科技强劲反弹" }, right: { policy: "防御Regime与确认窗口" }, winner: "Regime纪律", resolution: "短期反弹不足以覆盖5日确认和跨引擎一致性要求。", resolutionClass: "policy_over_estimated_signal", unresolvedIssue: "确认窗口可能造成短期踏空" },
    ],
    holdReason: "日度解除信号仅1/5日，v5仍维持防御，跨引擎冲突尚未解决。",
    rationale: ["日度信号仅完成1/5日确认", "v5仍为-4.15%且Top15集中度仅3", "情绪修复尚未获得基本面确认", "账户回撤约-14%，优先控制风险"],
    warnings: ["日度与v5引擎分别报告-0.96%和-4.15%，禁止合并", "日度与v5的Top15主导组数量分别为4和3", "Qwen候选漏掉双引擎数值冲突，Canonical已补回", "深度研究触发状态属于流程信息，未作为核心投资Claim保留"],
  }),
  followupCase({
    date: "2026-08-11", prefix: "D11", sessionId: "7ec46459-52de-4d7f-bbfc-da5afa764dd4",
    bundleHash: "0b72e8c271145beec7b5b6b545e624ba0557a627e8af6e869ce74fd660031a9a",
    summary: "日度引擎给出HS300距MA120为-0.81%，原始切换信号进入2/5日确认，但v5仍为-4.15%、Top15集中度仍为3；在情绪极热与宏观疲弱背离下继续防御配置并HOLD。",
    sources: [
      { id: "D11_SRC_INPUT", kind: "session_input", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-11/bot105d/sent.md", sha256: "3f5fd6b9d64c74b30fdb6a6ababd8cfe4f2e49d4b597d0f3446e702caee017f0", tables: [] },
      { id: "D11_SRC_REPLY", kind: "session_reply", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-11/bot105d/reply.json", sha256: "8f31d70399f615e864ef97498d54bff6612e4561b061ab4faba368bdc567dcc5", tables: [] },
      { id: "D11_SRC_CLOSE", kind: "close_my_day", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-11/bot105d/close_my_day.json", sha256: "f33399f1bcafdfa696c2129416e699276691739c2731ac5af7f7caa45ea4b96b", tables: [] },
      { id: "D11_SRC_DB", kind: "portfolio_database", path: "/home/rooot/agent_invest_lab/data/fund.db", sha256: null, tables: ["fund_bot_daily_snapshots", "fund_bot_actions", "fund_bot_orders", "fund_info"] },
      { id: "D11_SRC_QWEN", kind: "qwen_candidate", path: "runtime/decision-episodes/qwen3.7-plus/bot105d_2026-08-11.validated.candidate.json", sha256: "4c364ef41240aeeab05bdd75669f7166068a3467a2b99e5e358301f494d9f807", tables: [] },
    ],
    account: { source: "sent.md#账户快照", snapshotLabel: "2026-08-10 settle后、8月11日决策前注入", initialCapital: 1000000, cashAvailable: 1048437, investedValue: 510213, totalValue: 1558650, cumulativeReturnPct: 55.87, maxDrawdownPct: -15.00, holdings: [
      { instrument: "fund:000051", name: "华夏沪深300ETF联接A", shares: 35.84, marketValue: 64, portfolioWeightPct: 0.00, unrealizedReturnPct: 15.34, holdingDays: 489 },
      { instrument: "fund:012762", name: "华泰柏瑞上证红利ETF联接C", shares: 261632.95, marketValue: 359718, portfolioWeightPct: 23.08, unrealizedReturnPct: 2.78, holdingDays: 5 },
      { instrument: "fund:510030", name: "价值ETF华宝", shares: 52413.70, marketValue: 150431, portfolioWeightPct: 9.65, unrealizedReturnPct: 0.29, holdingDays: 5 },
    ] },
    equityWeight: 32.73, cashWeight: 67.27,
    facts: [
      { id: "D11F1", subject: "index:CSI300", predicate: "DISTANCE_TO_MA120", value: -0.81, unit: "percent", eventTime: "2026-08-11", producer: "mainline_daily", factClass: "derived_fact", source: "sent.md#market_mainline.regime", reproducible: "unknown", metadata: { confirmationProgress: "2/5", hysteresisBand: [-3, -1] } },
      { id: "D11F2", subject: "index:CSI300", predicate: "DISTANCE_TO_MA120", value: -4.15, unit: "percent", eventTime: "2026-08-11", producer: "v5_mainline", factClass: "derived_fact", source: "sent.md#mainline_rotation.Regime判定依据", reproducible: "unknown", metadata: {} },
      { id: "D11F3", subject: "market:CN_equity", predicate: "TOP15_DOMINANT_GROUP_COUNT", value: 4, unit: null, eventTime: "2026-08-11", producer: "mainline_daily", factClass: "derived_fact", source: "sent.md#market_mainline.regime", reproducible: null, metadata: { dominantGroup: "cycle_resources" } },
      { id: "D11F4", subject: "market:CN_equity", predicate: "TOP15_DOMINANT_GROUP_COUNT", value: 3, unit: null, eventTime: "2026-08-11", producer: "v5_mainline", factClass: "derived_fact", source: "sent.md#mainline_rotation.Top15集中度", reproducible: null, metadata: {} },
      { id: "D11F5", subject: "market:CN_equity", predicate: "MARKET_TEMPERATURE", value: 54.89, unit: "index", eventTime: "2026-08-11", producer: "market_context", factClass: "market_observation", source: "sent.md#market_context", reproducible: null, metadata: { threeMonthPercentilePct: 93.65, priceVolumeDivergence: 88.89 } },
      { id: "D11F6", subject: "macro:CN_PMI", predicate: "HAS_VALUE", value: 49.2, unit: "index", eventTime: "2026-07", producer: null, factClass: "reported_macro_fact", source: "sent.md#market_context", reproducible: null, metadata: {} },
      { id: "D11F7", subject: "market:VIX", predicate: "LEVEL", value: 19.1, unit: "index", eventTime: "2026-08-11", producer: null, factClass: "market_observation", source: "reply.json#/reply/市场环境判断", reproducible: null, metadata: {} },
      { id: "D11F8", subject: "portfolio:bot105d", predicate: "EQUITY_WEIGHT", value: 32.73, unit: "percent", eventTime: "2026-08-11", producer: "session_snapshot", factClass: "account_fact", source: "sent.md#账户快照", reproducible: null, metadata: {} },
      { id: "D11F9", subject: "portfolio:bot105d", predicate: "CURRENT_DRAWDOWN", value: -13.76, unit: "percent", eventTime: "2026-08-11", producer: "session_snapshot", factClass: "account_fact", source: "sent.md#整体绩效", reproducible: null, metadata: { basis: "all_history_high_water_mark" } },
    ],
    relations: [
      { id: "D11R1", subject: "index:CSI300", predicate: "DETERMINES", object: "regime:defensive_dividend", direction: "signal_to_regime", mechanism: "日度引擎的-0.81%已高于-1%解除线附近并进入2/5日确认，但v5仍为-4.15%；确认期未完成且口径不一致，因此维持原Regime。", validFrom: "2026-08-11", validTo: null, evidence: [{ factId: "D11F1" }, { factId: "D11F2" }], certainty: "policy_defined" },
      { id: "D11R2", subject: "market:CN_equity", predicate: "VALIDATES", object: "regime:switch_candidate", direction: "market_to_regime", mechanism: "日度Top15主导组数量达到4，而v5仍为3；存在切换候选信号，但跨引擎尚未收敛。", validFrom: "2026-08-11", validTo: null, evidence: [{ factId: "D11F3" }, { factId: "D11F4" }], certainty: "estimated" },
      { id: "D11R3", subject: "policy:five_day_confirmation", predicate: "BLOCKS", object: "action:switch_regime", direction: "policy_to_action", mechanism: "日度原始信号只完成2/5日，确认窗口阻止依据单日或两日改善立即换仓。", validFrom: "2026-08-11", validTo: null, evidence: [{ factId: "D11F1" }], certainty: "hard_execution_constraint" },
      { id: "D11R4", subject: "regime:defensive_dividend", predicate: "SELECTS", object: "fund:012762", direction: "regime_to_instrument", mechanism: "确认完成前继续以012762和价值ETF维持防御结构，不追逐情绪过热。", validFrom: "2026-08-11", validTo: null, evidence: [{ source: "reply.json#/reply/执行结果" }], certainty: "policy_defined" },
    ],
    claims: [
      { id: "D11C1", statement: "防御·红利Regime继续维持：日度引擎为-0.81%并完成2/5日确认，但v5仍为-4.15%，Top15也存在4与3的口径差异。", claimType: "rule_derived_classification", status: "accepted_for_execution", confidenceLabel: "authoritative_for_this_bot", certainty: "policy_defined", mechanism: ["日度解除候选信号尚未完成5日确认", "v5仍远低于解除线", "Top15跨引擎尚未一致"], supportingEvidence: [{ factId: "D11F2" }, { factId: "D11F4" }, { source: "sent.md#market_mainline.regime", explanation: "原始切换信号为2/5日" }], contradictingEvidence: [{ factId: "D11F1", explanation: "日度口径已高于-1%附近" }, { factId: "D11F3", explanation: "日度Top15已出现4个同组板块" }], falsifiers: ["日度解除信号连续保持5日且不同引擎趋于一致"] },
      { id: "D11C2", statement: "市场仍属震荡，情绪极热与宏观疲弱形成显著背离，不能把反弹直接解释为趋势反转。", claimType: "market_interpretation", status: "active_at_decision", confidenceLabel: "medium", certainty: "estimated", mechanism: ["市场温度54.89且三月分位93.65%", "PMI 49.2仍在收缩区间", "VIX回落显示恐慌消退，但增长与风险偏好没有同步确认"], supportingEvidence: [{ factId: "D11F5" }, { factId: "D11F6" }, { factId: "D11F7" }], contradictingEvidence: [{ source: "sent.md#market_mainline", explanation: "日度原始切换信号已连续2日改善" }], falsifiers: ["增长、情绪和多资产价格出现持续一致的方向信号"] },
      { id: "D11C3", statement: "当日应HOLD，维持约32.7%权益与67.3%现金，不新增、不切换。", claimType: "portfolio_construction", status: "accepted_for_execution", confidenceLabel: "high", certainty: "hard_constraint_plus_estimated_portfolio_design", mechanism: ["Regime确认窗口未完成", "跨引擎指标仍冲突", "账户全历史高水位回撤仍为-13.76%", "情绪已处极热区，不适合追涨"], supportingEvidence: [{ factId: "D11F8" }, { factId: "D11F9" }, { source: "reply.json#/reply/执行结果", explanation: "所有持仓明确HOLD" }], contradictingEvidence: [{ factId: "D11F1", explanation: "日度解除候选信号继续改善" }], falsifiers: ["解除信号完成5日确认、跨引擎收敛且账户风险允许切换"] },
    ],
    conflicts: [
      { id: "D11DC1", left: { producer: "mainline_daily", HS300_vs_MA120: -0.81, confirmation: "2/5", dominant_count: 4 }, right: { producer: "v5_mainline", HS300_vs_MA120: -4.15, dominant_count: 3 }, winner: "维持当前防御状态，等待确认", resolution: "不合并冲突指标；日度信号仍未完成5日确认，按既定确认窗口保持原Regime。", resolutionClass: "confirmation_window_over_raw_signal", unresolvedIssue: "两套引擎MA120窗口与Top15分组口径差异未复算解释" },
      { id: "D11DC2", left: { signal: "市场温度三月分位93.65%极热" }, right: { signal: "PMI收缩且风险偏好偏弱" }, winner: "维持neutral与防御，不追涨", resolution: "情绪与宏观背离不能提供稳定方向，等待一致性信号。", resolutionClass: "deferred_until_confirmation", unresolvedIssue: "极热情绪会继续上冲还是回落尚未解决" },
    ],
    holdReason: "日度切换信号仅2/5日，v5仍维持防御且指标口径未收敛。",
    rationale: ["日度信号仅完成2/5日确认", "v5仍为-4.15%且Top15仅3", "市场情绪极热与宏观疲弱背离", "账户仍处于较深全历史回撤"],
    warnings: ["日度与v5分别报告-0.81%和-4.15%，禁止合并", "日度与v5的Top15主导组数量分别为4和3", "Qwen候选把-0.81%误写成触发防御，Canonical已修正为解除候选信号", "当日HOLD没有产生新订单"],
  }),
  followupCase({
    date: "2026-08-12", prefix: "D12", sessionId: "cc8acf4e-57f9-470c-ab37-458aa656e4e1",
    bundleHash: "4fcbc167264ab34497d6be152607c923e51ab795e7a41304cf183ef739053385",
    summary: "日度、rotation和v5对HS300距MA120分别给出-1.60%、约-4.90%和-4.15%；三者都未满足连续5日站回-1%以上的解除条件。Regime纪律覆盖77%量化仓位建议，当日HOLD且零新订单。",
    sources: [
      { id: "D12_SRC_INPUT", kind: "session_input", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-12/bot105d/sent.md", sha256: "53f1f54cd04a132f8281c0479260d533a52fdeb564c6fecc0769b394924fb48a", tables: [] },
      { id: "D12_SRC_REPLY", kind: "session_reply", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-12/bot105d/reply.json", sha256: "1298b279666e28bffda4f0112549fea89899a97e80f24b180f4a5468fe7cae2f", tables: [] },
      { id: "D12_SRC_CLOSE", kind: "close_my_day", path: "runtime/runs/bot105d-daily-agenticdeep-charter-0424/2026-08-12/bot105d/close_my_day.json", sha256: "a5c26b9a5614252c4c20387c5677221273d0e7f17f50a1bb6ea9f15d20526d14", tables: [] },
      { id: "D12_SRC_DB", kind: "portfolio_database", path: "/home/rooot/agent_invest_lab/data/fund.db", sha256: null, tables: ["fund_bot_daily_snapshots", "fund_bot_actions", "fund_bot_orders", "fund_info"] },
      { id: "D12_SRC_QWEN", kind: "qwen_candidate", path: "runtime/decision-episodes/qwen3.7-plus/bot105d_2026-08-12.validated.candidate.json", sha256: "49a07c0deca6dd0319c779e6682c85140644380d6936d6267f0c4c76bd8c684c", tables: [] },
    ],
    account: { source: "sent.md#账户快照", snapshotLabel: "2026-08-11 settle后、8月12日决策前注入", initialCapital: 1000000, cashAvailable: 1048437, investedValue: 508552, totalValue: 1556989, cumulativeReturnPct: 55.70, maxDrawdownPct: -15.00, holdings: [
      { instrument: "fund:000051", name: "华夏沪深300ETF联接A", shares: 35.84, marketValue: 64, portfolioWeightPct: 0.00, unrealizedReturnPct: 14.48, holdingDays: 490 },
      { instrument: "fund:012762", name: "华泰柏瑞上证红利ETF联接C", shares: 261632.95, marketValue: 358631, portfolioWeightPct: 23.03, unrealizedReturnPct: 2.47, holdingDays: 6 },
      { instrument: "fund:510030", name: "价值ETF华宝", shares: 52413.70, marketValue: 149856, portfolioWeightPct: 9.62, unrealizedReturnPct: -0.10, holdingDays: 6 },
    ] },
    equityWeight: 32.66, cashWeight: 67.34,
    facts: [
      { id: "D12F1", subject: "index:CSI300", predicate: "DISTANCE_TO_MA120", value: -1.60, unit: "percent", eventTime: "2026-08-12", producer: "mainline_daily", factClass: "derived_fact", source: "sent.md#market_mainline.regime", reproducible: "unknown", metadata: { hysteresisBand: [-3, -1] } },
      { id: "D12F2", subject: "index:CSI300", predicate: "DISTANCE_TO_MA120", value: -4.9, unit: "percent", eventTime: "2026-08-12", producer: "mainline_rotation", factClass: "derived_fact", source: "sent.md#mainline_rotation.Regime判定依据", reproducible: "unknown", metadata: { approximate: true, ma120Approx: 4906 } },
      { id: "D12F3", subject: "index:CSI300", predicate: "DISTANCE_TO_MA120", value: -4.15, unit: "percent", eventTime: "2026-08-12", producer: "v5_mainline", factClass: "derived_fact", source: "sent.md#mainline_rotation.Regime判定依据", reproducible: "unknown", metadata: { reportedAsAuthoritative: true } },
      { id: "D12F4", subject: "market:CN_equity", predicate: "TOP15_DOMINANT_GROUP_COUNT", value: 4, unit: null, eventTime: "2026-08-12", producer: "mainline_daily", factClass: "derived_fact", source: "sent.md#market_mainline.regime", reproducible: null, metadata: { dominantGroup: "cycle_resources" } },
      { id: "D12F5", subject: "market:CN_equity", predicate: "TOP15_DOMINANT_GROUP_COUNT", value: 3, unit: null, eventTime: "2026-08-12", producer: "v5_mainline", factClass: "derived_fact", source: "sent.md#mainline_rotation.Top15集中度", reproducible: null, metadata: {} },
      { id: "D12F6", subject: "market:systemic_risk", predicate: "RISK_SCORE", value: 0.119, unit: "score", eventTime: "2026-08-12", producer: "market_systemic_risk_cb20", factClass: "derived_fact", source: "reply.json#/reply/市场环境判断", reproducible: "unknown", metadata: { state: "normal" } },
      { id: "D12F7", subject: "model:hs300_erp_vix_timing", predicate: "SUGGESTED_EQUITY_WEIGHT", value: 77, unit: "percent", eventTime: "2026-08-12", producer: "quant_factor", factClass: "derived_fact", source: "reply.json#/reply/市场环境判断", reproducible: "unknown", metadata: { valuation: "expensive" } },
      { id: "D12F8", subject: "portfolio:bot105d", predicate: "DRAWDOWN_20D_HIGH_WATER", value: -3.65, unit: "percent", eventTime: "2026-08-12", producer: "session_snapshot", factClass: "account_fact", source: "sent.md#整体绩效", reproducible: null, metadata: { riskGatePct: -6 } },
      { id: "D12F9", subject: "portfolio:bot105d", predicate: "DRAWDOWN_ALL_HISTORY_HIGH_WATER", value: -13.85, unit: "percent", eventTime: "2026-08-12", producer: "session_snapshot", factClass: "account_fact", source: "sent.md#整体绩效", reproducible: null, metadata: { riskGatePct: -15 } },
      { id: "D12F10", subject: "portfolio:bot105d", predicate: "EQUITY_WEIGHT", value: 32.66, unit: "percent", eventTime: "2026-08-12", producer: "session_snapshot", factClass: "account_fact", source: "sent.md#账户快照", reproducible: null, metadata: {} },
    ],
    relations: [
      { id: "D12R1", subject: "index:CSI300", predicate: "DETERMINES", object: "regime:defensive_dividend", direction: "signal_to_regime", mechanism: "日度-1.60%位于迟滞带内，rotation约-4.90%与v5 -4.15%低于防御线；虽然口径冲突，但没有任何口径满足连续5日站回-1%以上。", validFrom: "2026-08-12", validTo: null, evidence: [{ factId: "D12F1" }, { factId: "D12F2" }, { factId: "D12F3" }], certainty: "policy_defined" },
      { id: "D12R2", subject: "market:CN_equity", predicate: "VALIDATES", object: "regime:switch_candidate", direction: "market_to_regime", mechanism: "日度Top15主导组数量为4，而v5为3；主线集中度尚未跨引擎一致确认。", validFrom: "2026-08-12", validTo: null, evidence: [{ factId: "D12F4" }, { factId: "D12F5" }], certainty: "estimated" },
      { id: "D12R3", subject: "policy:regime_precedence", predicate: "OVERRIDES", object: "model:hs300_erp_vix_timing", direction: "policy_to_signal", mechanism: "防御Regime与确认窗口覆盖量化模型77%的建议仓位；量化信号不能单独触发加仓。", validFrom: "2026-08-12", validTo: null, evidence: [{ factId: "D12F7" }, { factId: "D12F10" }], certainty: "hard_execution_constraint" },
      { id: "D12R4", subject: "regime:defensive_dividend", predicate: "SELECTS", object: "fund:012762", direction: "regime_to_instrument", mechanism: "Regime解除前继续持有红利与价值防御结构，不新增主线仓。", validFrom: "2026-08-12", validTo: null, evidence: [{ source: "reply.json#/reply/执行结果" }], certainty: "policy_defined" },
    ],
    claims: [
      { id: "D12C1", statement: "防御·红利Regime继续维持：HS300距MA120存在-1.60%、约-4.90%和-4.15%三套口径，但均未满足连续5日站回-1%以上的解除条件。", claimType: "rule_derived_classification", status: "accepted_for_execution", confidenceLabel: "authoritative_for_this_bot", certainty: "policy_defined", mechanism: ["日度-1.60%仍位于-3%至-1%迟滞带内", "rotation与v5仍低于-3%防御线", "Top15集中度4与3也未跨引擎一致"], supportingEvidence: [{ factId: "D12F1" }, { factId: "D12F2" }, { factId: "D12F3" }, { factId: "D12F5" }], contradictingEvidence: [{ factId: "D12F4", explanation: "日度Top15已出现4个同组板块" }], falsifiers: ["HS300连续5日站回MA120的-1%以上且Top15在不同引擎中一致达到门槛"] },
      { id: "D12C2", statement: "账户风控闸门当日未触发，但20日与全历史回撤必须分口径管理。", claimType: "risk_control_interpretation", status: "accepted_for_execution", confidenceLabel: "high", certainty: "hard_constraint", mechanism: ["20日高水位回撤-3.65%未触及-6%降档线", "全历史高水位回撤-13.85%未触及-15%强风控线", "两条闸门窗口不同，禁止相互替代"], supportingEvidence: [{ factId: "D12F8" }, { factId: "D12F9" }, { factId: "D12F6" }], contradictingEvidence: [], falsifiers: ["20日回撤达到-6%或全历史回撤达到-15%"] },
      { id: "D12C3", statement: "当日应HOLD，维持约32.7%权益，不按量化模型建议提高至77%，且数据库确认零新订单。", claimType: "portfolio_construction", status: "accepted_for_execution", confidenceLabel: "high", certainty: "hard_constraint_plus_estimated_portfolio_design", mechanism: ["Regime仍未解除", "量化模型建议与Regime纪律冲突", "反弹动能尚未形成连续确认", "下一交易日将触发深研硬上限，当前不抢跑"], supportingEvidence: [{ factId: "D12F10" }, { source: "reply.json#/reply/执行结果", explanation: "明确HOLD且无交易" }, { source: "fund.db#fund_bot_orders", explanation: "8月12日无新订单" }], contradictingEvidence: [{ factId: "D12F7", explanation: "量化模型建议77%权益仓位" }], falsifiers: ["Regime完成解除确认且账户风险允许提高仓位"] },
    ],
    conflicts: [
      { id: "D12DC1", left: { producer: "mainline_daily", HS300_vs_MA120: -1.60 }, right: { producer: "mainline_rotation/v5", HS300_vs_MA120: [-4.90, -4.15] }, winner: "维持防御；三值全部保留", resolution: "不合并三套MA120口径；它们共同结论只是均未满足连续5日站回-1%以上的解除要求。", resolutionClass: "shared_decision_despite_measurement_conflict", unresolvedIssue: "MA120计算窗口、数据版本或报告日期差异未在session中解释" },
      { id: "D12DC2", left: { producer: "mainline_daily", dominant_count: 4 }, right: { producer: "v5_mainline", dominant_count: 3 }, winner: "未确认主线切换", resolution: "主线集中度没有跨引擎一致，且MA120解除条件未满足。", resolutionClass: "cross_engine_confirmation_required", unresolvedIssue: "Top15主题分组口径差异未复算解释" },
      { id: "D12DC3", left: { model: "hs300_erp_vix_timing", suggested_equity_weight_pct: 77 }, right: { regime: "defensive_dividend", actual_equity_weight_pct: 32.66 }, winner: "Regime纪律", resolution: "Regime状态机和确认窗口优先于单一量化仓位建议。", resolutionClass: "hard_policy_over_model_signal", unresolvedIssue: "确认窗口可能带来快速反转中的踏空" },
    ],
    holdReason: "Regime未解除，跨引擎指标不一致，量化仓位建议被Regime纪律覆盖。",
    rationale: ["三套MA120口径均未满足解除条件", "Top15集中度未跨引擎一致", "20日与全历史风险闸门均未触发", "量化77%仓位建议不能覆盖Regime纪律"],
    warnings: ["HS300距MA120存在-1.60%、约-4.90%和-4.15%三套口径，禁止合并", "20日高水位回撤与全历史高水位回撤对应不同闸门，禁止替代", "Qwen候选把rotation的-4.90%直接描述为唯一触发依据，Canonical已补回日度和v5口径", "深研计时属于流程信息，未单独作为核心投资Claim", "当日HOLD没有产生新订单"],
  }),
] as const

const json = (value: unknown): string => JSON.stringify(value)

function buildCanonicalCases(outputPath: string, cases: readonly any[], createdAt = new Date().toISOString()): string {
  const target = resolve(outputPath)
  if (existsSync(target)) throw new Error(`refusing to overwrite existing canonical database: ${target}`)
  mkdirSync(dirname(target), { recursive: true })
  const db = new DatabaseSync(target)
  try {
    initializeCanonicalDecisionDatabase(db)
    db.exec('BEGIN IMMEDIATE')
    for (const c of cases) {
    db.prepare(`INSERT INTO canonical_cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
      c.identity.caseId, c.identity.schemaVersion, c.identity.extractionStatus, c.identity.botId,
      c.identity.runId, c.identity.sessionId, c.identity.worldDate, c.identity.decisionTime,
      c.identity.decisionTimeReason, c.identity.knowledgeCutoff, c.identity.knowledgeRule,
      c.identity.decisionSummary, c.identity.sourceBundleHash, createdAt,
    )
    const sourceInsert = db.prepare('INSERT INTO canonical_sources VALUES (?,?,?,?,?,?)')
    for (const source of c.sources) sourceInsert.run(source.id, c.identity.caseId, source.kind, source.path, source.sha256, json(source.tables))
    db.prepare('INSERT INTO canonical_contexts VALUES (?,?,?)').run(c.identity.caseId, json(c.context.accountBefore), json(c.context.constraints))
    const factInsert = db.prepare('INSERT INTO canonical_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)')
    for (const fact of c.facts) factInsert.run(fact.id, c.identity.caseId, fact.subject, fact.predicate, json(fact.value), fact.unit, fact.eventTime, fact.producer, fact.factClass, fact.source, fact.reproducible, json(fact.metadata))
    const relationInsert = db.prepare('INSERT INTO canonical_relations VALUES (?,?,?,?,?,?,?,?,?,?,?)')
    for (const relation of c.relations) relationInsert.run(relation.id, c.identity.caseId, relation.subject, relation.predicate, relation.object, relation.direction, relation.mechanism, relation.validFrom, relation.validTo, json(relation.evidence), relation.certainty)
    const claimInsert = db.prepare('INSERT INTO canonical_claims VALUES (?,?,?,?,?,?,?,?,?,?,?)')
    for (const claim of c.claims) claimInsert.run(claim.id, c.identity.caseId, claim.statement, claim.claimType, claim.status, claim.confidenceLabel, json(claim.mechanism), json(claim.supportingEvidence), json(claim.contradictingEvidence), json(claim.falsifiers), claim.certainty)
    const conflictInsert = db.prepare('INSERT INTO canonical_conflicts VALUES (?,?,?,?,?,?,?,?)')
    for (const conflict of c.conflicts) conflictInsert.run(conflict.id, c.identity.caseId, json(conflict.left), json(conflict.right), conflict.winner, conflict.resolution, conflict.resolutionClass, conflict.unresolvedIssue)
    const candidateInsert = db.prepare('INSERT INTO canonical_candidate_actions VALUES (?,?,?,?,?,?,?,?,?,?)')
    for (const action of c.candidateActions) candidateInsert.run(action.id, c.identity.caseId, action.action, action.requestedAmount, action.requestedShares, action.result, action.orderId, action.sourceToolCall, action.reason, action.executionNote)
    db.prepare('INSERT INTO canonical_final_decisions VALUES (?,?,?,?)').run(c.identity.caseId, c.finalDecision.decision, json(c.finalDecision.targetStructure), json(c.finalDecision.rationale))
    const orderInsert = db.prepare('INSERT INTO canonical_actual_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)')
    for (const order of c.actualOrders) orderInsert.run(order.orderId, c.identity.caseId, order.action, order.instrument, order.requestedAmount, order.requestedShares, order.confirmedAmount, order.confirmedShares, order.fee, order.orderStatusAtDecision, order.databaseStatusNow)
    const warningInsert = db.prepare('INSERT INTO canonical_warnings VALUES (?,?,?)')
    c.warnings.forEach((warning: string, index: number) => warningInsert.run(`${c.identity.caseId}_W${index + 1}`, c.identity.caseId, warning))
    }
    db.exec('COMMIT')
  } catch (error) {
    try { db.exec('ROLLBACK') } catch { /* transaction may not have started */ }
    throw error
  } finally {
    db.close()
  }
  return target
}

export function buildAug05CanonicalDatabase(outputPath: string, createdAt = new Date().toISOString()): string {
  return buildCanonicalCases(outputPath, [AUG05_CANONICAL_CASE], createdAt)
}

export function buildCanonicalDecisionDatabase(outputPath: string, createdAt = new Date().toISOString()): string {
  return buildCanonicalCases(outputPath, [AUG05_CANONICAL_CASE, ...ADDITIONAL_CANONICAL_CASES], createdAt)
}
