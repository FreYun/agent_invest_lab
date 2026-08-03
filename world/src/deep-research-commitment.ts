export interface DeepResearchCommitment {
  bot_id: string
  fund_code: string
  committed_on: string
  commit_until: string
  min_holding_days: number
  thesis: string
  /** 承诺类型：deep_research=深研日建仓（重研可证伪平仓）；min_hold=普通建仓（仅硬风控/到期解锁）。
   *  可选：老 run 的 state.json 无此字段，读取时缺省视作 deep_research。 */
  kind?: 'deep_research' | 'min_hold'
  source_amount?: number
}

export type DeepResearchCommitmentsByBot = Record<string, Record<string, DeepResearchCommitment>>

export interface SuccessfulFundAction {
  fundCode: string
  reason: string
  amount?: number
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function actionKind(name: unknown): 'buy' | 'sell' | null {
  if (typeof name !== 'string') return null
  if (name.endsWith('portfolio_place_buy_order')) return 'buy'
  if (name.endsWith('portfolio_place_sell_order')) return 'sell'
  return null
}

function toolSucceeded(event: Record<string, unknown>): boolean {
  const preview = event.content_preview
  if (typeof preview !== 'string') return false
  try {
    const parsed = JSON.parse(preview) as unknown
    return isObject(parsed) && parsed.success === true
  } catch {
    return false
  }
}

/** 从 research-loop 的 tool_trace 提取真正成功的基金买卖。
 *
 * tool_start 与 tool_end 成对出现；失败后重试会产生多对。只有对应 tool_end
 * 明确返回 success=true 才计入，避免把被交易服务拒绝的尝试写成持仓承诺。 */
export function extractSuccessfulFundActions(toolTrace: unknown[]): { buys: SuccessfulFundAction[]; sells: SuccessfulFundAction[] } {
  const pending: Array<{ kind: 'buy' | 'sell'; name: string; action: SuccessfulFundAction }> = []
  const buys: SuccessfulFundAction[] = []
  const sells: SuccessfulFundAction[] = []
  for (const raw of toolTrace) {
    if (!isObject(raw)) continue
    const kind = actionKind(raw.name)
    if (!kind) continue
    if (raw.type === 'tool_start') {
      const args = isObject(raw.arguments) ? raw.arguments : {}
      const fundCode = typeof args.fund_code === 'string' ? args.fund_code.trim() : ''
      if (!fundCode) continue
      const amountRaw = kind === 'buy' ? args.amount : args.shares
      const amount = typeof amountRaw === 'number' ? amountRaw : Number(amountRaw)
      pending.push({
        kind,
        name: String(raw.name),
        action: {
          fundCode,
          reason: typeof args.reason === 'string' ? args.reason.trim() : '',
          ...(Number.isFinite(amount) && amount > 0 ? { amount } : {}),
        },
      })
      continue
    }
    if (raw.type !== 'tool_end') continue
    const idx = pending.findIndex(p => p.name === raw.name)
    if (idx < 0) continue
    const [matched] = pending.splice(idx, 1)
    if (!toolSucceeded(raw)) continue
    if (matched.kind === 'buy') buys.push(matched.action)
    else sells.push(matched.action)
  }
  return { buys, sells }
}

export function addCalendarDays(isoDate: string, days: number): string {
  const time = Date.parse(`${isoDate}T00:00:00Z`)
  if (!Number.isFinite(time)) return isoDate
  return new Date(time + Math.max(0, Math.floor(days)) * 86_400_000).toISOString().slice(0, 10)
}

export function isCommitmentActive(commitment: DeepResearchCommitment | undefined, tradeDate: string): boolean {
  return Boolean(commitment && tradeDate < commitment.commit_until)
}

export function upsertDeepResearchCommitments(
  current: DeepResearchCommitmentsByBot | undefined,
  botId: string,
  tradeDate: string,
  buys: Array<SuccessfulFundAction & { minHoldingDays: number }>,
  kind: 'deep_research' | 'min_hold',
): DeepResearchCommitmentsByBot {
  const next: DeepResearchCommitmentsByBot = structuredClone(current ?? {})
  const byFund = { ...(next[botId] ?? {}) }
  for (const buy of buys) {
    const minHoldingDays = Math.max(1, Math.floor(buy.minHoldingDays))
    const existing = byFund[buy.fundCode]
    const candidateUntil = addCalendarDays(tradeDate, minHoldingDays)
    // 同一基金已有尚未到期的承诺时，加仓只能延长、不能缩短承诺。
    const commitUntil = existing && existing.commit_until > candidateUntil ? existing.commit_until : candidateUntil
    // kind 只升不降：已有活跃 deep_research 承诺时，min_hold 加仓不把它降级为 min_hold
    // （深研标的的重研平仓通道要保留）；min_hold 承诺遇 deep_research 买入则升级。
    const existingActiveDeep = Boolean(existing && (existing.kind ?? 'deep_research') === 'deep_research' && isCommitmentActive(existing, tradeDate))
    const resolvedKind: 'deep_research' | 'min_hold' = kind === 'deep_research' || existingActiveDeep ? 'deep_research' : 'min_hold'
    byFund[buy.fundCode] = {
      bot_id: botId,
      fund_code: buy.fundCode,
      committed_on: tradeDate,
      commit_until: commitUntil,
      min_holding_days: minHoldingDays,
      kind: resolvedKind,
      thesis: buy.reason || existing?.thesis || (resolvedKind === 'deep_research'
        ? '深研日建仓；持有至承诺到期或下一次深研明确证伪。'
        : '普通建仓最短持有承诺；持有至承诺到期或系统硬风控放行。'),
      ...(buy.amount !== undefined ? { source_amount: buy.amount } : {}),
    }
  }
  if (Object.keys(byFund).length) next[botId] = byFund
  return next
}

export function activeCommitmentsForBot(
  all: DeepResearchCommitmentsByBot | undefined,
  botId: string,
  tradeDate: string,
  heldFundCodes?: Iterable<string>,
): DeepResearchCommitment[] {
  const held = heldFundCodes ? new Set(heldFundCodes) : null
  return Object.values(all?.[botId] ?? {})
    .filter(c => isCommitmentActive(c, tradeDate) && (!held || held.has(c.fund_code)))
    .sort((a, b) => a.commit_until.localeCompare(b.commit_until) || a.fund_code.localeCompare(b.fund_code))
}

export function renderDeepResearchCommitmentBlock(
  commitments: DeepResearchCommitment[],
  tradeDate: string,
  deepResearchUnlocked: boolean,
): string {
  if (!commitments.length) return ''
  const lines = [
    '────────── 深研持仓承诺（系统状态 · 跨日生效） ──────────',
    deepResearchUnlocked
      ? '今天是深度研究日：可以用新的完整深研结论维持、修改或解除下列承诺。'
      : '今天不是深度研究日：下列承诺期内，普通指标走弱、单日反转或 belief 小幅变化均不能推翻深研建仓结论；交易代理会拒绝卖单。',
  ]
  for (const c of commitments) {
    const left = Math.max(0, Math.ceil((Date.parse(`${c.commit_until}T00:00:00Z`) - Date.parse(`${tradeDate}T00:00:00Z`)) / 86_400_000))
    const thesis = c.thesis.length > 180 ? `${c.thesis.slice(0, 180)}…` : c.thesis
    lines.push(`- ${c.fund_code}：${c.committed_on} 深研建仓，承诺至 ${c.commit_until}（还剩 ${left} 自然日）；建仓 thesis：${thesis}`)
  }
  lines.push('退出规则：承诺到期自动解锁；到期前只有在新的深研日重新研究并明确证伪后才能卖出。系统识别到急跌或账户回撤越线时，会把当日升级为强制深研日，不延误硬风控。')
  return lines.join('\n')
}
