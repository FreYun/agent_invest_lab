# 单指数「持有承诺块」Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给单指数择时 bot（bot1~20）加一个系统侧「持有承诺」软块，把「建仓=承诺持有到免赎档」前置到买入、把「窗内离场的确切早赎费」顶到决策面前，遏制 7 日内翻烙饼式频繁交易。

**Architecture:** 纯新增到 `world/src/message.ts`：`redeemPenaltyOf`（解析基金赎回阶梯）+ `computeInWindowLots`（从 recentOrders 算窗内持仓）+ `holdCommitmentBlock`（渲染），在 `renderDailyMessage` 里对 `botKindOf(botId)==='single-fund'` 装配（Day1/DayN 两路）。全部数据来自现成的 `dc.fundFees / dc.account.recentOrders / dc.account.holdings / dc.benchmark`，确定性、无未来函数、缺数据即空串。

**Tech Stack:** TypeScript（Node ≥22.6，`--experimental-strip-types` 直跑）；测试 `node --test`（`node:test` + `node:assert/strict`）。

## Global Constraints

- 运行：world 用 `node --experimental-strip-types`，无编译步骤；测试跑 `cd world && npm test`（= `node --test test/*.test.ts`）。
- 类型检查真 tsc = `/home/rooot/claude-code-best/node_modules/.bin/tsc -p .` 从 `world/` 跑（`world/` 下裸 `npx tsc` 是假占位包，勿信）；基线允许 2 条 `server.ts` 的 TS2352,不得新增其它报错。
- 只改单指数路径；**多基金（bot101/102/103）行为一字节不改**。
- 软块：不拦单，缺数据（无 fundFees / 无 recentOrders / 无早赎惩罚档）一律返回空串，零回归。
- 早赎惩罚按**自然日**判定（`asOfDate − order_date` 日历天数 vs `redeem_tiers.max_days`）——赎回费本就是按持有自然日收，别用交易日。
- 中文注释/文案（项目硬约束：全程中文，代码标识符除外）。

---

### Task 1: 承诺行 + 赎回阶梯解析 + 接线（骨架）

**Files:**
- Modify: `world/src/message.ts`（新增 3 个函数 + `renderDailyMessage` 两处接线，约 message.ts:1092/1134/1147 区域）
- Test: `world/test/message.test.ts`（新增用例）

**Interfaces:**
- Consumes：`renderDailyMessage(ctx)` 已存在；`botKindOf(botId): BotKind`（message.ts:108）；`fmtNum`（已存在）；类型 `DailyContextData`、`FundFee`（含 `fund_code / fund_name / found / redeem_tiers: {max_days:number|null; rate_pct:number}[]`）。
- Produces：
  - `redeemPenaltyOf(fee: FundFee | undefined): { windowDays: number; rateForDays: (calDays: number) => number } | null`
  - `holdCommitmentBlock(dc: DailyContextData | undefined, botId: string, asOfDate: string): string`

- [ ] **Step 1: 写失败测试**（承诺行渲染 / 多基金不渲染 / 无费率空串）

在 `world/test/message.test.ts` 末尾追加（复用文件顶部已 import 的 `renderDailyMessage / tmpdir / rmSync` 及本文件内的 `tmpWorldWithOverview`）：

```ts
// ── 单指数持有承诺块 ──────────────────────────────────────────────
const HC_FEES = [
  { fund_code: '019875', fund_name: 'CS稀金属ETF联接C', found: true,
    purchase_fee_pct: 0, redeem_tiers: [{ max_days: 7, rate_pct: 1.5 }, { max_days: null, rate_pct: 0 }],
    mgmt_fee_pct_annual: 0.5, custody_fee_pct_annual: 0.1, sales_service_fee_pct_annual: 0.3 },
]
const HC_BENCH = {
  code: 'buyable-pool', name: '买池等权',
  pointsByDate: { '2024-03-11': 0, '2024-03-12': 0.1, '2024-03-13': 0.2, '2024-03-14': 0.1, '2024-03-15': 0.3 },
  latestCumulativePct: 0.3,
}
function hcAccount(recentOrders: unknown, holdings: unknown) {
  return {
    asOfDate: '2024-03-18',
    account: { initial_capital: 1_000_000, cash_available: 500_000, cash_in_transit: 0, market_value: 500_000, total_value: 1_000_000 },
    holdings, pendingOrders: [], recentOrders,
  }
}

test('持有承诺块：单指数 bot 恒渲染建仓承诺 + 免赎档；多基金不渲染；无费率空串', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'x')
  const single = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: {
      fundFees: HC_FEES, benchmark: HC_BENCH,
      account: hcAccount(
        [{ fund_code: '019875', order_type: 'buy', order_date: '2024-03-01', status: 'confirmed', order_amount: 300000 }],
        [{ fund_code: '019875', fund_name: 'CS稀金属ETF联接C', shares: 140000, amount_invested: 300000, latest_nav: 2.14, market_value: 300000, weight: 0.3 }],
      ),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(single, /持有承诺核对（系统核算 · 早赎红线）/)
  assert.match(single, /建仓 = 持有承诺/)
  assert.match(single, /019875（CS稀金属ETF联接C）：持满 7 自然日免赎；不足确定亏 1\.50% 早赎费/)

  // 多基金 bot101 → 不渲染（门控）
  const multi = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot101', quotesPath: '/q.json',
    dailyContext: {
      fundFees: HC_FEES, benchmark: HC_BENCH,
      account: hcAccount([], []),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.doesNotMatch(multi, /持有承诺核对/)

  // 无 fundFees → 空串
  const noFees = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: { benchmark: HC_BENCH, account: hcAccount([], []) },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.doesNotMatch(noFees, /持有承诺核对/)
  rmSync(w, { recursive: true, force: true })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --test --test-name-pattern='持有承诺块：单指数' test/message.test.ts`
Expected: FAIL（`持有承诺核对` 未出现）。

- [ ] **Step 3: 实现三函数 + 接线**

在 `world/src/message.ts`（建议紧接 `tradeDisciplineBlock` 之后、`vehicleCheckBlock` 之前）新增：

```ts
// ── 单指数持有承诺块（早赎红线可见化 · 防翻烙饼）──────────────────────────
// 只对单指数 bot（bot1~20）渲染。牙齿①：建仓=承诺持有到免赎档（前置到买入）；
// 牙齿②：窗内持仓今日卖出的确切早赎费 + 思考闸（窗内离场须 mem0 写充分理由，审计兜底）。
// 数据全部现成、确定性、无未来函数；任一缺失→空串，零回归。赎回费按自然日判定。

// 自然日差（含跨月/跨年，按 UTC ISO 日期）。
function calendarDaysBetween(from: string, to: string): number {
  const a = Date.parse(`${from}T00:00:00Z`), b = Date.parse(`${to}T00:00:00Z`)
  if (!Number.isFinite(a) || !Number.isFinite(b)) return -1
  return Math.round((b - a) / 86400000)
}

// 从赎回阶梯求「免赎档天数 windowDays」与「持有 calDays 自然日的命中费率」。
// tiers 例：[{max_days:7,rate_pct:1.5},{max_days:null,rate_pct:0}] → windowDays=7；
//   rateForDays(k<7)=1.5、rateForDays(k≥7)=0。全程零赎回费（无 rate>0 档）→ null（不渲染）。
function redeemPenaltyOf(fee: FundFee | undefined): { windowDays: number; rateForDays: (calDays: number) => number } | null {
  const tiers = fee?.redeem_tiers
  if (!tiers || !tiers.length) return null
  let windowDays = 0
  for (const t of tiers) if (t.rate_pct > 0 && t.max_days != null && t.max_days > windowDays) windowDays = t.max_days
  if (windowDays <= 0) return null
  const rateForDays = (calDays: number): number => {
    let best = 0, bestMax = Infinity
    for (const t of tiers) {
      const md = t.max_days == null ? Infinity : t.max_days
      if (calDays < md && md <= bestMax) { bestMax = md; best = t.rate_pct }
    }
    return best
  }
  return { windowDays, rateForDays }
}

function holdCommitmentBlock(dc: DailyContextData | undefined, botId: string, asOfDate: string): string {
  if (botKindOf(botId) !== 'single-fund') return ''
  const fees = dc?.fundFees
  if (!fees?.length) return ''
  // 承诺行（恒定）：对每只"有早赎惩罚档"的基金列免赎档。
  const commit: string[] = []
  for (const f of fees) {
    const pen = redeemPenaltyOf(f)
    if (!pen) continue
    commit.push(`  - ${f.fund_code}${f.fund_name ? `（${f.fund_name}）` : ''}：持满 ${pen.windowDays} 自然日免赎；不足确定亏 ${fmtNum(pen.rateForDays(0), 2)}% 早赎费。`)
  }
  if (!commit.length) return '' // 全程零赎回费的 bot：无翻烙饼成本，不渲染。
  const lines: string[] = []
  lines.push('────────── 持有承诺核对（系统核算 · 早赎红线） ──────────')
  lines.push('【建仓 = 持有承诺】买入/加仓即承诺持有到免赎档，窗内离场确定吃早赎费——买之前就想清楚能不能拿住：')
  lines.push(...commit)
  // 牙齿②窗内持仓思考闸（Task 2 填充）。
  const gate = inWindowGateLines(dc, asOfDate, fees)
  if (gate.length) lines.push(...gate)
  return `\n\n${lines.join('\n')}`
}

// Task 2 之前先给个空实现，保证 Task 1 可独立通过。
function inWindowGateLines(_dc: DailyContextData | undefined, _asOfDate: string, _fees: FundFee[]): string[] {
  return []
}
```

在 `renderDailyMessage`（message.ts:1092）里，`kind` 已在 1097 算好。分别在 Day1 return（1134）与 DayN return（1147）的 `${contextBlocks}` 之后插入 `${holdCommit}`；在两个 return 之前各加一行：

```ts
  // 单指数持有承诺块（多基金返回空串，安全）。
  const holdCommit = holdCommitmentBlock(ctx.dailyContext, ctx.botId, ctx.date)
```

Day1（1134）：`...${contextBlocks}${holdCommit}${beliefStr}...`
DayN（1147）：`...${contextBlocks}${holdCommit}${beliefStr}...`

（放在 `contextBlocks` 后、`beliefStr` 前，紧挨费率块，语义连贯。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd world && node --test --test-name-pattern='持有承诺块：单指数' test/message.test.ts`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add world/src/message.ts world/test/message.test.ts
git commit -m "feat(world): 单指数持有承诺块骨架——建仓承诺行 + 赎回阶梯解析 + 接线"
```

---

### Task 2: 窗内持仓思考闸（computeInWindowLots + 渲染）

**Files:**
- Modify: `world/src/message.ts`（新增 `computeInWindowLots`，充实 `inWindowGateLines`）
- Test: `world/test/message.test.ts`

**Interfaces:**
- Consumes：`redeemPenaltyOf`、`calendarDaysBetween`、`fmtNum`；`dc.account.recentOrders`（`RecentOrderRow`：`fund_code/order_type/order_date/status/order_amount`）；`dc.account.holdings`（`HoldingRow`：`fund_code/market_value` 等）；`dc.benchmark.pointsByDate`（可选，算标的 move%）。
- Produces：
  - `computeInWindowLots(dc, asOfDate, feesByFund: Map<string,FundFee>): { fund_code: string; buyDate: string; calDays: number; windowDays: number; rate: number; amount: number }[]`
  - `inWindowGateLines(dc, asOfDate, fees): string[]`（替换 Task 1 空实现）

- [ ] **Step 1: 写失败测试**（窗内买入→费用+思考闸；满档→无闸行；FIFO 上限；off-by-one）

追加：

```ts
test('持有承诺块·思考闸：窗内买入渲染早赎费+距免赎+思考闸；持满7日不渲染闸行', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'x')
  // 03-15 买入（asOfDate 03-18，自然日差=3 <7 → 在窗），持仓市值 300000
  const inWin = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: {
      fundFees: HC_FEES, benchmark: HC_BENCH,
      account: hcAccount(
        [{ fund_code: '019875', order_type: 'buy', order_date: '2024-03-15', status: 'confirmed', order_amount: 300000 }],
        [{ fund_code: '019875', fund_name: 'CS稀金属ETF联接C', shares: 140000, amount_invested: 300000, latest_nav: 2.14, market_value: 300000, weight: 0.3 }],
      ),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(inWin, /窗内持仓 · 今日若卖出的确切成本/)
  assert.match(inWin, /019875：2024-03-15 买入（已持 3 自然日，距免赎还剩 4 天）/)
  assert.match(inWin, /今日卖出早赎费 ≈ ¥4,?500（1\.50%）/) // 1.5% × 300000 = 4500
  assert.match(inWin, /先在 mem0 写下经过思考的充分理由再下单/)
  assert.match(inWin, /审计会核：窗内卖出而无实质论证 = 违规/)

  // 持满 7 自然日（02-01 买）→ 无闸行，但承诺行仍在
  const held = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: {
      fundFees: HC_FEES, benchmark: HC_BENCH,
      account: hcAccount(
        [{ fund_code: '019875', order_type: 'buy', order_date: '2024-02-01', status: 'confirmed', order_amount: 300000 }],
        [{ fund_code: '019875', fund_name: 'CS稀金属ETF联接C', shares: 140000, amount_invested: 300000, latest_nav: 2.14, market_value: 300000, weight: 0.3 }],
      ),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(held, /持满 7 自然日免赎/)      // 承诺行在
  assert.doesNotMatch(held, /窗内持仓 · 今日若卖出/) // 无闸行
  rmSync(w, { recursive: true, force: true })
})

test('持有承诺块·思考闸：off-by-one（第7自然日=已免赎，不渲染）+ 已清仓不警示', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'x')
  // 03-11 买，到 03-18 = 7 自然日 → calDays=7 不 <7 → 免赎，无闸行
  const day7 = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: {
      fundFees: HC_FEES, benchmark: HC_BENCH,
      account: hcAccount(
        [{ fund_code: '019875', order_type: 'buy', order_date: '2024-03-11', status: 'confirmed', order_amount: 300000 }],
        [{ fund_code: '019875', fund_name: 'CS稀金属ETF联接C', shares: 140000, amount_invested: 300000, latest_nav: 2.14, market_value: 300000, weight: 0.3 }],
      ),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.doesNotMatch(day7, /窗内持仓 · 今日若卖出/)

  // 已清仓（holdings 无该基金）→ 无在窗份额可警示
  const sold = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: {
      fundFees: HC_FEES, benchmark: HC_BENCH,
      account: hcAccount(
        [{ fund_code: '019875', order_type: 'buy', order_date: '2024-03-15', status: 'confirmed', order_amount: 300000 }],
        [],
      ),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.doesNotMatch(sold, /窗内持仓 · 今日若卖出/)
  rmSync(w, { recursive: true, force: true })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --test --test-name-pattern='持有承诺块·思考闸' test/message.test.ts`
Expected: FAIL（闸行未出现）。

- [ ] **Step 3: 实现 computeInWindowLots + inWindowGateLines**

替换 Task 1 的 `inWindowGateLines` 空实现，并新增 `computeInWindowLots`：

```ts
// 窗内持仓 lot：某基金在早赎窗内买入且当前仍持有（用当前持仓市值作粗略上限）的份额。
// 近似口径（软块够用）：取在窗买单（calDays<windowDays），按买入日倒序（新仓最可能仍在持有）
// 用当前持仓市值封顶——已部分/全部卖出的自然被市值上限截掉；基金不在持仓即无 lot。
function computeInWindowLots(
  dc: DailyContextData | undefined, asOfDate: string, feesByFund: Map<string, FundFee>,
): { fund_code: string; buyDate: string; calDays: number; windowDays: number; rate: number; amount: number }[] {
  const orders = dc?.account?.recentOrders
  if (!orders?.length) return []
  const mvByFund = new Map((dc?.account?.holdings ?? []).map(h => [h.fund_code, h.market_value]))
  const buysByFund = new Map<string, { order_date: string; order_amount: number }[]>()
  for (const o of orders) {
    if (o.order_type !== 'buy') continue
    if (o.status !== 'confirmed' && o.status !== 'pending') continue
    const arr = buysByFund.get(o.fund_code) ?? []
    arr.push({ order_date: o.order_date, order_amount: Math.max(0, o.order_amount) })
    buysByFund.set(o.fund_code, arr)
  }
  const out: { fund_code: string; buyDate: string; calDays: number; windowDays: number; rate: number; amount: number }[] = []
  for (const [code, buys] of buysByFund) {
    const pen = redeemPenaltyOf(feesByFund.get(code))
    if (!pen) continue
    let cap = mvByFund.get(code) ?? 0
    if (cap <= 0) continue // 已清仓，无在窗份额
    const inWin = buys
      .map(b => ({ ...b, calDays: calendarDaysBetween(b.order_date, asOfDate) }))
      .filter(b => b.calDays >= 0 && b.calDays < pen.windowDays)
      .sort((a, b) => b.order_date.localeCompare(a.order_date)) // 新仓优先（仍在持有）
    for (const b of inWin) {
      if (cap <= 0) break
      const amount = Math.min(b.order_amount, cap)
      cap -= amount
      out.push({ fund_code: code, buyDate: b.order_date, calDays: b.calDays, windowDays: pen.windowDays, rate: pen.rateForDays(b.calDays), amount })
    }
  }
  return out.sort((a, b) => a.fund_code.localeCompare(b.fund_code) || a.buyDate.localeCompare(b.buyDate))
}

function inWindowGateLines(dc: DailyContextData | undefined, asOfDate: string, fees: FundFee[]): string[] {
  const feesByFund = new Map(fees.map(f => [f.fund_code, f]))
  const lots = computeInWindowLots(dc, asOfDate, feesByFund)
  if (!lots.length) return []
  const pts = dc?.benchmark?.pointsByDate
  const lines: string[] = ['【窗内持仓 · 今日若卖出的确切成本】']
  for (const lot of lots) {
    const fee = (lot.rate / 100) * lot.amount
    let moveStr = ''
    if (pts && pts[asOfDate] != null && pts[lot.buyDate] != null) {
      const mv = pts[asOfDate] - pts[lot.buyDate]
      moveStr = `；标的自买入 ${mv >= 0 ? '+' : ''}${fmtNum(mv, 2)}%`
    }
    lines.push(`  - ${lot.fund_code}：${lot.buyDate} 买入（已持 ${lot.calDays} 自然日，距免赎还剩 ${lot.windowDays - lot.calDays} 天）。今日卖出早赎费 ≈ ¥${fmtNum(fee, 0)}（${fmtNum(lot.rate, 2)}%）${moveStr}`)
  }
  lines.push('⚠️ 今日若要在窗内卖出上述份额：**先在 mem0 写下经过思考的充分理由再下单**——不是「指标破位」，不是「感觉风险大」，而是论证为什么这个临时情况足以推翻你建仓时的持有承诺。审计会核：窗内卖出而无实质论证 = 违规。')
  return lines
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd world && node --test --test-name-pattern='持有承诺块' test/message.test.ts`
Expected: PASS（Task 1 + Task 2 全部用例）。

- [ ] **Step 5: 提交**

```bash
git add world/src/message.ts world/test/message.test.ts
git commit -m "feat(world): 单指数持有承诺块·窗内思考闸——早赎费明算 + mem0 论证义务"
```

---

### Task 3: 边界回归 + Day1 路径 + 多档基金 + 全量验证

**Files:**
- Modify: `world/test/message.test.ts`（补边界用例）
- Test: 全量 `npm test` + 真 tsc

**Interfaces:**
- Consumes：Task 1/2 的 `holdCommitmentBlock`（经 `renderDailyMessage`）。
- Produces：无新导出，仅测试与验证。

- [ ] **Step 1: 写边界测试**（无 recentOrders 承诺行仍在 / Day1 路径 / 多档基金锚首个免赎档）

追加：

```ts
test('持有承诺块·边界：无 recentOrders 承诺行仍渲染无闸行；Day1 路径也渲染；多档基金锚首免赎档', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'x')
  // 无 recentOrders（老快照）：承诺行在、无闸行、不报错
  const legacy = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: { fundFees: HC_FEES, benchmark: HC_BENCH, account: hcAccount(undefined, []) },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(legacy, /持满 7 自然日免赎/)
  assert.doesNotMatch(legacy, /窗内持仓 · 今日若卖出/)

  // Day1 路径同样渲染
  const day1 = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: true, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: { fundFees: HC_FEES, benchmark: HC_BENCH, account: hcAccount([], []) },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(day1, /持有承诺核对（系统核算 · 早赎红线）/)

  // 多档赎回阶梯：<7d 1.5% / <30d 0.5% / ≥30d 0% → 免赎档=30、rate(0)=1.5
  const multiTier = [{ fund_code: '013403', fund_name: '某基', found: true, purchase_fee_pct: 0,
    redeem_tiers: [{ max_days: 7, rate_pct: 1.5 }, { max_days: 30, rate_pct: 0.5 }, { max_days: null, rate_pct: 0 }] }]
  const mt = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: {
      fundFees: multiTier, benchmark: HC_BENCH,
      account: hcAccount(
        // 03-01 买，calDays=17 <30 在窗，命中 <30d 档 0.5%
        [{ fund_code: '013403', order_type: 'buy', order_date: '2024-03-01', status: 'confirmed', order_amount: 200000 }],
        [{ fund_code: '013403', fund_name: '某基', shares: 100000, amount_invested: 200000, latest_nav: 2.0, market_value: 200000, weight: 0.2 }],
      ),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(mt, /013403（某基）：持满 30 自然日免赎；不足确定亏 1\.50% 早赎费/)
  assert.match(mt, /已持 17 自然日，距免赎还剩 13 天）。今日卖出早赎费 ≈ ¥1,?000（0\.50%）/) // 0.5%×200000=1000
  rmSync(w, { recursive: true, force: true })
})
```

- [ ] **Step 2: 跑新测试确认通过**

Run: `cd world && node --test --test-name-pattern='持有承诺块·边界' test/message.test.ts`
Expected: PASS。

- [ ] **Step 3: 全量测试**

Run: `cd world && npm test`
Expected: message.test 全绿；其余与基线一致（既有清单里的 flaky 除外，不得新增失败）。若某条断言的千分位/取整与实现不符，按实际输出微调断言（如 `¥4,500` vs `¥4500`——`fmtNum(x,0)` 是否带千分位以实际为准）。

- [ ] **Step 4: 真 tsc 类型检查**

Run: `cd world && /home/rooot/claude-code-best/node_modules/.bin/tsc -p .`
Expected: 仅既有 2 条 `server.ts` TS2352 基线错，无新增（尤其确认 `dc.fundFees`、`RecentOrderRow.order_amount`、`FundFee.redeem_tiers` 的类型引用无报错）。

- [ ] **Step 5: 提交**

```bash
git add world/test/message.test.ts
git commit -m "test(world): 单指数持有承诺块边界——无订单/Day1/多档基金回归"
```

---

## 上线备注（实现完成后交付用户，不在本计划内执行）

- 当前 run **不回溯**；改动需一次普通 `main.ts pause`/`resume` 重启进程，下一决策日起生效；不重启则只对未来 run 生效——重启与否 + 提交（连同工作区其它待定改动）由用户拍板。
- 软块先跑一个月；观察单指数 run `fee>0`（<免赎档）卖单笔数与累计早赎费是否下降；若 bot 明显滥用「充分理由」绕过，再把窗内卖出升为 fund-portfolio-mcp server 端硬闸（带客观硬止损标志）。

## Self-Review

- **Spec 覆盖**：§4.1 承诺行→Task1；§4.2 思考闸→Task2；§4.3 FIFO/市值封顶→Task2 `computeInWindowLots`；§4.4 早赎费成本基近似 + move%→Task2；§5 不做项（无硬闸/无反向冷却/不拦试探/不动多基金）→计划零涉及且 Task1 多基金门控测试守；§7 测试 7 项→Task1(承诺/多基金/无费率) + Task2(窗内/满档/FIFO 清仓/off-by-one) + Task3(无订单/Day1/多档) 全覆盖；§8 上线→末节备注。
- **占位符**：无 TBD/TODO；每步含完整代码或确切命令。
- **类型一致**：`redeemPenaltyOf`/`computeInWindowLots`/`inWindowGateLines`/`holdCommitmentBlock`/`calendarDaysBetween` 命名跨 Task 一致；lot 结构字段（fund_code/buyDate/calDays/windowDays/rate/amount）Task2 定义、Task2 渲染消费一致。
- **已知实现风险**：`fmtNum(fee,0)` 是否输出千分位逗号需以实际为准（Step3 已注明按实际微调断言）；`FundFee` 类型字段名以 message.ts `tradingFeesBlock` 现用为准（`redeem_tiers`/`fund_name`/`found`）。
