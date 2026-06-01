import type { DailyContextData, AccountSnapshot, FundSeries, IndexQuote, PerformanceData, IntervalMetricRow, FundFee, BuyablePoolMeta, BuyablePoolMetaRow } from './daily-context.ts'

export interface DailyMessageContext {
  worldRoot: string
  date: string
  isFirstDay: boolean
  // 当前 bot 的稳定 ID（"bot2" / "bot7" ……）。daily message 头部直接把它写成"你的 bot_id = 'X'"
  // 硬锚——portfolio_* / mcp__strategy_mcp__update_my_strategy 等工具的 bot_id 参数必须按这个字面量传，proxy 不会
  // 帮你注入。少了这一锚，LLM 会瞎填 "me" 之类语义占位符，服务端按字符串精确匹配查不到账户，
  // 整天的下单全打空（2026-05-26 dash-2026-05-26T06-34-04 bot2 实测过）。
  botId: string
  quotesPath: string        // 绝对路径，保留 forward-compat（未来 quotes-MCP 可能按路径暴露）
  // 本轮 user 选定的可买基金白名单。每天都显式播报（含仓位调整指引：单只 → 纯单指数
  // 择时，多只 → 可在池内配置/轮动）；server 端 place_buy_order 也按同一份 curated 校验。
  buyableFundCodes?: string[]
  // simworld-data 的全部工具（name + 一句话描述）。每天都注入，bot 看到全貌再
  // 决定 discover_tools 拉哪几个，避免"只看行情不研究"。空数组 → 跳过该 block。
  simworldTools?: { name: string; description: string }[]
  // World 在 chat 前预取的当日上下文：账户快照、近 N 日 PnL 走势、持仓基金 NAV
  // 走势、主要指数 MA。每天必看的几样直接灌进 prompt，bot 不必再调 tool 自查。
  // 任一字段缺失（fetcher 失败 / 无持仓 / day 1 没历史）就跳过对应渲染块。
  dailyContext?: DailyContextData
  // 本次回测总交易日数。仅 Day 1 注入到 fullRules，让 bot 按这个时间窗口规划策略
  // （短线 / 波段 / 长持）。具体起止日期不暴露——只给"今天 + N 个交易日"——以减少
  // LLM 训练记忆按已知历史时间段反向决策的泄漏面。Day N 即便传了也不注入，避免给 bot
  // 一个"剩余天数"的 endgame 倒计时（会引发临近终点的窗口式抛售等非真实行为）。
  tradingDaysTotal?: number
  // 滚动 history window：前 N 个交易日的 session digest（去掉工具结果），由 history-window 模块
  // 按 20000 字符预算切割，超 budget 时按 4 维度（踏空 / 反复被收割 / 范式冲击 / thesis 演变）
  // 用主模型压缩老的 60%。位置：渲染在 SOUL 等核心 md（在 system prompt 里）与 daily rules / dailyContext
  // 之间——即 daily user message 的最顶部。空串 → 跳过整块（Day 1 没有 prior session 时即此）。
  historyWindow?: string
  // Belief context block: pre-rendered belief schema (today's required fields) + 21d 校准反馈
  // （把 bot 历史 belief vs 实际市场轨迹的偏差摆出来），由 belief-context 模块异步构造。
  // renderDailyMessage 是同步函数——caller (run.ts) 先 await buildBeliefContext(botId, runId, date)
  // 把整块字符串塞进来；本函数只做 string concat 不再 await。空/undefined → 跳过整块。
  // 位置：渲染在 dailyContext 数据块之后、METHODOLOGY 提示之前——让 bot 看完今日数据再被要求"今天你
  // 的 belief 是什么 + 上次预测错在哪"，配合 footer 的 termination contract 落库到 mem0。
  beliefBlock?: string
}

export function weekdayOf(isoDate: string): string {
  return new Date(isoDate + 'T00:00:00Z').toLocaleString('en-US', { weekday: 'long', timeZone: 'UTC' })
}

// Bot 类型：决定 buyable 池播报的指引文案。**和池子大小解耦**——
// 即使 user 在新建回测时给单基 bot 勾了 10 只池子，单基 bot 的决策风格也不变（它只盯自己的标的择时）；
// 反之 multi-fund bot 即使只勾了 1 只，文案也不再退化成"纯择时"。决策风格本来就是 bot 的内在属性。
//
// 命名约定：bot1..bot20 = 单基金；bot101+（三位数）= 多基金。新增风格 bot 时扩展这个规则。
export type BotKind = 'single-fund' | 'multi-fund'

export function botKindOf(botId: string): BotKind {
  return /^bot1\d{2}$/.test(botId) ? 'multi-fund' : 'single-fund'
}

// Prompt 不再注入静态 overview——行情走 simworld-data MCP 实时查；不再区分研究日 vs 交易日
// （budget 在 run.ts 单独管），统一让 bot 自己决定要不要展开。
function fullRules(date: string, weekday: string, botId: string, tradingDaysTotal: number | undefined): string {
  // 周期块：只在 Day 1 注入，让 bot 按窗口长度规划策略；不写终止日期，仅给"今天 + N 天"。
  // Bot 仍能从 today's date + N 推出终止日，但不直接告知，减少训练记忆的 hindsight 触发面。
  return `当前世界日期：${date}（${weekday}）。
你是天天基金为散户进行财富管理的交易员，无论什么策略，什么标的，你的核心是帮用户守住本金，赚取绝对收益，这是你的目标。
在今天开始之前，你的基金账户已被初始化，你有 100 万初始现金，盈亏从 0 开始累计。

【你的 bot_id】**${botId}**。\`mcp__fund_portfolio_mcp__portfolio_place_buy_order\` / \`mcp__fund_portfolio_mcp__portfolio_place_sell_order\` / \`mcp__fund_portfolio_mcp__portfolio_get_my_history\` / \`mcp__fund_portfolio_mcp__portfolio_get_my_trades\` / \`mcp__fund_portfolio_mcp__portfolio_get_my_performance\` / \`mcp__strategy_mcp__update_my_strategy\` 等工具的 \`bot_id\` 参数必须按字面量传 \`"${botId}"\`——不是 "me"、不是 "self"、不是空字符串。传错服务端会按字面字符串匹配，结果一律是"无账户"。

【你的任务】追求绝对收益，控制账户回撤（不是最大回撤，是绝对亏损）。

【决策框架】请参考你的 **AGENTS.md**（已注入到 system prompt 的 \`## AGENTS.md\` section）——这是你的角色定位、决策风格和操作边界的总纲。和 METHODOLOGY.md 配合使用：AGENTS.md 定"你是谁、怎么想"，METHODOLOGY.md 定"看什么信号、按什么规则下单"。

【可用工具范围】本会话开放：mem0_search / mem0_add、list_skills / load_skill，以及 simworld-data / fund-portfolio-mcp 的所有 mcp__* 工具——**全部已直接挂进工具列表**，看到就能调，无需任何激活步骤。文件读写、web_fetch、bash、子代理（spawn_skill_agent）、研究模式（start_research 等）全部禁用——调用会被直接拒。

【skill 体系】list_skills 看有哪些可加载的研究框架，load_skill <name> 把 skill 内容直接载入当前对话当思考脚手架。目前 workspace 里只有 tmt-research（TMT 行业研究指南）——研究科技/媒体/电信主题基金或个股时先 load 一下，按它的框架来思考再去查数据。

【数据预取】当日账户/持仓/累计绩效/区间业绩/已平仓 P&L/近 N 日 PnL 走势/持仓基金近 20 日 NAV/5 大指数 MA 已经在下方"【...】"块里全量灌好。**不要重复调用 mcp__fund_portfolio_mcp__portfolio_get_my_history / mcp__fund_portfolio_mcp__portfolio_get_my_performance / mcp__fund_portfolio_mcp__portfolio_get_my_trades** 查这些；也不要为持仓基金或这 5 大指数重复调 mcp__simworld_data__fund_nav / mcp__simworld_data__market_index_quote——直接读上下文。

【账户操作】下单走 mcp__fund_portfolio_mcp__portfolio_place_buy_order / mcp__fund_portfolio_mcp__portfolio_place_sell_order；可买基金白名单走 mcp__fund_portfolio_mcp__portfolio_get_buyable_funds（变化频率低，记下来就够）。

【研究 / 新基金 / 行业暴露 / 资金流 / 宏观 / 研报 / 商品 / 债券】这些没预取，按需直接调对应的 simworld-data 工具——它们都已在工具列表里。`
}

function briefRules(date: string, weekday: string, botId: string): string {
  return `当前世界日期：${date}（${weekday}）。
牢记：你的终极目标是追求绝对收益，控制账户回撤（不是最大回撤，是绝对亏损）。
你的 bot_id = **${botId}**——所有 portfolio_* / mcp__strategy_mcp__update_my_strategy 工具的 \`bot_id\` 参数都按字面量传 \`"${botId}"\`（proxy 不会自动注入，传 "me" / "self" / 空串都会被服务端按字符串匹配判成"无账户"）。
可用 mcp__* / mem0_search / mem0_add / list_skills / load_skill（所有 mcp__* 已直接挂进工具列表，无需激活），文件读写和 bash 都被禁。

今天的节奏（按顺序）：
  ① **先看下方【...】数据块**：找出账户回撤 / NAV 变化 / 指数趋势 / 区间业绩相对你昨日 thesis 有没有 drift。
  ② **调至少 1 个非 mem0 工具拉今日新数据**：工具从simworld-data 全部工具里选择，你需要按实际市场情况来选择工具调用，你 methodology 五视角里今天还没覆盖的那个——估值 / 趋势 / 景气度 / 资金面 / 证伪 **这一步缺，整天等于没做。**
  ③ mem0_search 拉过去 thesis / 决策，与今天的数据对比是 still valid 还是已破。**默认会按"最近优先"衰减打分（τ=30 天），近一周的记录天然浮在前面**；想只看最近几天就传 \`start_date=YYYY-MM-DD\`（比如今天往前 7 天），想关掉衰减拉全历史就传 \`recency_tau_days=0\`。
  ④ 决策（**请参考你的 AGENTS.md**——角色与决策风格总纲；以及 METHODOLOGY.md——仓位管理方法论） + 下单（如有）。
  ⑤ mem0_add 落库今天的判断 + 明天要带进来的事。

 铁律：严禁每天都进行一样的工具调用和决策流程——比如每天都只调同一个工具、每天都只看行情不研究、每天都只按技术面决策不考虑估值和资金面……**要根据实际市场情况和账户状态灵活调整**，不能变成机械的"每天都做一样的事"。
【数据预取】当日账户/绩效/PnL/持仓 NAV/5 大指数 MA 已在下方块内全量灌好。不要重复调 mcp__fund_portfolio_mcp__portfolio_get_my_history / mcp__fund_portfolio_mcp__portfolio_get_my_performance / mcp__fund_portfolio_mcp__portfolio_get_my_trades，也不要为持仓基金或这 5 大指数重复调 mcp__simworld_data__fund_nav / mcp__simworld_data__market_index_quote。下单直接用 mcp__fund_portfolio_mcp__portfolio_place_buy_order / mcp__fund_portfolio_mcp__portfolio_place_sell_order。研究新基金 / 行业 / 资金面 / 宏观 / 研报这些没预取，按需调对应 simworld-data 工具。`
}

const FOOTER_FULL = `

【记忆与连续性】每个世界日是独立会话，你不会自动记得昨天。
- 决策前：用 mem0_search 调取相关的历史交易/复盘记忆。
- 决策后：把今天的判断、操作、理由、要在下次想起的事用 mem0_add 落进记忆。

【边界】这是一次交易回合，不是研究项目；下了单 + mem0_add 写完今天的判断，就可以结束。`

// Day N 收尾合约：解决"列 todo 当 reflection 写但不执行 + 写了'明天再减仓'但今天不动 +
// 不 mem0_add 就 day-end + 纯 mem0 当一天做完"这几种早收。Day 1 已经在 FOOTER_FULL 里说过类似
// 的话，但 Day N 之前没 footer——LLM 默认"没事做了就停"，不会被"持仓未平 / 待办未做 / 没写复盘 /
// 没查新数据"这些条件拦住。这段补四条 termination contract：
// (0) 今天必须至少调一个非 mem0 的研究/数据工具——不然 mem0_search → mem0_add 会变成 reward
//     hack，bot 用过去的笔记复述出一篇看似 thoughtful 的 reply 就收工，而 dailyContext 里今天
//     的新数据完全没被验证，thesis 永远不会失效（dash-2026-05-19T08-52-36 实测：bot7 从 Day 6
//     起 9 iter → 3 iter，60% 仓位整月 HOLD，60 天没碰新数据）；
// (1) 今天的决策必须今天执行——把减/加仓推到下一日 = 决策蒸发（下一日新会话不继承"明天计划"）；
// (2) reply 里列出的"待办 / 要查的 / 要验证"必须执行掉，或显式放下并写明理由——不允许列了不做；
// (3) 结束前必须 mem0_add，否则下一日的 bot 看不到今天的判断和待办。
const FOOTER_BRIEF = `

【结束之前必做】
- **纯 mem0_search + mem0_add 不算完成一天**。今天必须至少 1 次调用非 mem0 的研究/行情/数据工具（mcp__* 任一，除 mcp__fund_portfolio_mcp__portfolio_get_my_history / mcp__fund_portfolio_mcp__portfolio_get_my_performance / mcp__fund_portfolio_mcp__portfolio_get_my_trades 外，那几个 dailyContext 已经灌好了）——验证 dailyContext 里某个数据点、拉一个 methodology 里今天还没覆盖的维度、或检验 thesis 是否破。不查就 mem0 落库 = 自欺欺人，下一日你 mem0_search 拉到的全是空想，回测就这么烂下去。
- **今天的决策今天就发生**：研究结论是减仓 → 调 mcp__fund_portfolio_mcp__portfolio_place_sell_order；加仓 → 调 mcp__fund_portfolio_mcp__portfolio_place_buy_order；保持 → 明确说"今日维持 X% 仓位，不动，理由是 ..."。把"明天减仓至 Y%"写进 mem0 ≠ 执行——下一日是新会话，看不到今日规划，等于决策从未发生。
- 如果你在思考里列出了"待办 / 要查的 / 要验证"，要么在结束前调工具做掉，要么明确说"这条今天先放下，理由是 X，明天再做"。列了不做 = 没列——明天的你会以为今天已经查过了。
- 结束前一次 mem0_add：今天的判断 + 做了什么 / 没做什么 + 明天要带着什么进来。没 mem0_add 就结束，下一日的你看不到今天，整天的研究就白做。`

// simworld-data 工具在真实工具表里的命名前缀。mcporter.json 把 server 注册为
// "simworld-data"，harness 暴露工具时统一加 `mcp__<server>__` 前缀并把 `-`→`_`，
// 所以模型看到/能调用的是 `mcp__simworld_data__market_index_quote` 这种带前缀的名字。
// 上游 tools/list 探到的是裸名（mcp__simworld_data__market_index_quote），若直接列进 prompt 会和工具表里
// 的带前缀名形成"同一工具两个名字"，故这里补齐前缀对齐。
const SIMWORLD_TOOL_PREFIX = 'mcp__simworld_data__'

// simworld-data 全工具清单：每天注入，让 bot 知道"研究类工具"（research_search /
// research_view / fund_industry_exposure / stock_alpha / macro_data ...）也都在
// 工具列表里，而不是只想到 mcp__simworld_data__market_index_quote / mcp__simworld_data__fund_nav。loop server 已开启
// chat_auto_activate_deferred → 所有 mcp__* 都直接挂进 chat 工具列表，bot 看到
// 函数描述就能直接调，无需 discover_tools。
function simworldToolsBlock(tools: { name: string; description: string }[]): string {
  if (!tools.length) return ''
  // 名字按真实工具表对齐：裸名补 `mcp__simworld_data__` 前缀；已带前缀的（防御性）不重复加。
  const lines = tools.map(t => {
    const name = t.name.startsWith('mcp__') ? t.name : `${SIMWORLD_TOOL_PREFIX}${t.name}`
    return `- ${name}${t.description ? ' — ' + t.description : ''}`
  }).join('\n')
  return `

【simworld-data 全部工具（${tools.length} 个，已全部直接可调用）】名字即真实工具名（带 \`${SIMWORLD_TOOL_PREFIX}\` 前缀，直接照抄调用）。决策不必都用，但要知道存在。除了行情之外，研究/估值/资金面/事件/宏观/基金底层暴露都在这里查。
${lines}`
}

// 每天都显式播报本轮可买池 + 仓位调整指引。指引文案**按 bot 类型决定**（不是按池子大小）：
// - single-fund bot：只做单指数择时（调仓位高低、无标的轮动），即便池子里有多只也只盯自己 methodology 指定的那只
//                    池子用扁平代码列表渲染（小，bot 看名字就够）。
// - multi-fund bot：组合配置 + 仓位 + 池内轮动 三件事。**按 theme 聚合 + top-K 候选** 视图渲染，
//                    bot 一眼能看到主题分布，不用 N 次 RPC 自己探索。
// 服务端 place_buy_order 按完整 curated 列表校验——prompt 里展示的是 top-K，bot 真买第 K+1 名也能买。
function buyableFundsBlock(codes: string[], botKind: BotKind, poolMeta?: BuyablePoolMeta): string {
  if (botKind === 'multi-fund' && poolMeta && poolMeta.rows.length > 0) {
    return multiFundPoolBlock(codes, poolMeta)
  }
  const guidance = botKind === 'single-fund'
    ? `**你是单基金 bot** —— 不论池子里有几只，你只做单指数择时：盯你 METHODOLOGY 里指定的那一只，决策就是调整它的仓位高低（空仓 ↔ 满仓之间），不要做标的轮动、不要把仓位分散到多只。`
    : `**你是多基金 bot** —— 在池内做三件事：① 组合配置（各基金目标权重）② 总仓位高低 ③ 池内轮动（换标的）。决策时考虑相关性、行业暴露、单基上限，不要把全部仓位押在一只上。`
  return `

【当前可买池（${codes.length} 只）】
${codes.join(', ')}

${guidance}
下单时 fund_code 必须从这份里选；不在这份里的会被 mcp__fund_portfolio_mcp__portfolio_place_buy_order 直接拒。`
}

// multi-fund bot 的可买池视图：按 theme 聚合 + 每 theme 显示 top-K 候选（按 1y rank 升序，rank 缺失放底）。
// 单主题（含"科技"）和多类共振（"新能源,科技"）分两段展示——多类组合一般不入核心仓但是 bot 应该知道存在。
// 设计目标：bot 直接读这块就能跑"主线 → 候选 → 选品"三步法的步 1+2，不用调任何 RPC 来探索池子。
const POOL_TOP_K_PER_THEME = 5
const SINGLE_THEMES = ['全市场', '科技', '新能源', '医药', '消费', '金融', '周期', '制造', '基建地产']
function multiFundPoolBlock(codes: string[], meta: BuyablePoolMeta): string {
  // 1) 按 theme 分组
  const groups = new Map<string, BuyablePoolMetaRow[]>()
  const noTheme: BuyablePoolMetaRow[] = []
  for (const r of meta.rows) {
    const t = r.theme?.trim()
    if (!t) { noTheme.push(r); continue }
    if (!groups.has(t)) groups.set(t, [])
    groups.get(t)!.push(r)
  }
  // 2) 每组按 1y rank_pct 升序（低=好，缺失放最后）
  for (const arr of groups.values()) {
    arr.sort((a, b) => {
      const ar = a.p1y_rank_pct, br = b.p1y_rank_pct
      if (ar == null && br == null) return 0
      if (ar == null) return 1
      if (br == null) return -1
      return ar - br
    })
  }

  // 3) 渲染：单主题 9 类按固定顺序；多类共振合并展示
  const lines: string[] = []
  const singleThemeRows: string[] = []
  for (const theme of SINGLE_THEMES) {
    const arr = groups.get(theme)
    if (!arr || arr.length === 0) continue
    singleThemeRows.push(`▍ ${theme}（${arr.length} 只）｜ 1y rank 前 ${Math.min(POOL_TOP_K_PER_THEME, arr.length)}：`)
    for (const r of arr.slice(0, POOL_TOP_K_PER_THEME)) {
      singleThemeRows.push(`  ${formatPoolRow(r)}`)
    }
    groups.delete(theme)
  }

  // 剩下的都是多类共振组合（"新能源,科技" / "周期,新能源" 等）
  const comboNames = [...groups.keys()].sort()
  const comboLines: string[] = []
  for (const combo of comboNames) {
    const arr = groups.get(combo)!
    comboLines.push(`▍ ${combo}（${arr.length} 只）｜ 1y rank 前 ${Math.min(3, arr.length)}：`)
    for (const r of arr.slice(0, 3)) {
      comboLines.push(`  ${formatPoolRow(r)}`)
    }
  }

  const piteline = meta.perf1yAsOf
    ? `1y 业绩快照截至 ${meta.perf1yAsOf}（同类百分位 rank_pct 低 = 排名靠前）；style 截至 ${meta.styleAsOf ?? 'n/a'}。`
    : `style 截至 ${meta.styleAsOf ?? 'n/a'}；本回测日早于 fund_performance 最早日，1y 排名暂无（业绩深拉需要时调 get_fund_detail 看 since_inception 区间）。`

  lines.push(`【可买池主题分布（共 ${codes.length} 只）｜ ${piteline}】`)
  lines.push(...singleThemeRows)
  if (comboLines.length) {
    lines.push('')
    lines.push(`▼ 多类共振组合（${comboNames.length} 个组合，决策时谨慎——往往同时承担两条主线的风险）：`)
    lines.push(...comboLines)
  }

  return `

${lines.join('\n')}

**你是多基金 bot**——按 METHODOLOGY 的"主线 → 候选 → 选品"三步收敛：先看上面的主题分布锁定主题（步 1），再按 size_style/invest_style 因子收敛（步 2），最后对剩 1-3 只候选调 \`get_fund_detail\` 拉 3y/5y 业绩深查（步 3）。**别再只看宽基相对强弱、PE 分位就下单——那是单基金 bot 的玩法。**
下单时 fund_code 必须从可买池里选（实际校验的是完整 ${codes.length} 只池子，不限于上面展示的 top-K）；不在池里的会被 mcp__fund_portfolio_mcp__portfolio_place_buy_order 直接拒。`
}

function formatPoolRow(r: BuyablePoolMetaRow): string {
  const factor = (r.size_style && r.invest_style)
    ? `[${r.size_style}/${r.invest_style}]`
    : r.size_style ? `[${r.size_style}/?]`
    : r.invest_style ? `[?/${r.invest_style}]`
    : '[?/?]'
  const scaleStr = r.scale != null ? `${fmtNum(r.scale, 1)}亿` : 'n/a'
  const rankStr = r.p1y_rank_pct != null
    ? `rank ${fmtNum(r.p1y_rank_pct, 1)}%${r.p1y_rank_text ? `(${r.p1y_rank_text})` : ''}`
    : 'rank n/a'
  const sharpeStr = r.p1y_sharpe != null ? `sharpe ${fmtNum(r.p1y_sharpe, 2)}` : ''
  const parts = [
    r.fund_code,
    (r.fund_name ?? '').slice(0, 14).padEnd(14, ' '),
    factor,
    scaleStr,
    rankStr,
    sharpeStr,
  ].filter(Boolean)
  return parts.join('  ｜  ')
}

// ============================================================================
// 预取数据块（dailyContext）——每天必看的几样直接灌进 prompt，bot 不必再调 tool 自查。
// fetcher 任一失败就跳过对应块；prompt 不会因数据缺失而崩。
// ============================================================================

function fmtNum(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return 'n/a'
  return n.toFixed(digits)
}

function fmtPct(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return 'n/a'
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(digits)}%`
}

function accountSnapshotBlock(snap: AccountSnapshot): string {
  const a = snap.account
  const lines: string[] = [
    `初始本金 ¥${fmtNum(a.initial_capital, 0)} ｜ 可用现金 ¥${fmtNum(a.cash_available, 0)} ｜ 在途 ¥${fmtNum(a.cash_in_transit, 0)} ｜ 持仓市值 ¥${fmtNum(a.market_value, 0)} ｜ 总资产 ¥${fmtNum(a.total_value, 0)}`,
  ]
  // 账户级累计盈亏 = 持仓浮盈（active 仓 mark-to-market）+ 已实现（含费用、已平仓口径）。
  // 单基"浮盈"只反映单条 holding 当前未平仓部分；bot 之前会拿单基浮盈当成账户盈亏写进
  // 日报里——这一行直接把账户口径打出来，单基行同时改名 "单基浮盈" 来消歧。
  if (a.initial_capital > 0) {
    const cumPnl = a.total_value - a.initial_capital
    const cumPnlPct = cumPnl / a.initial_capital * 100
    const unrealizedTotal = snap.holdings.reduce((s, h) => s + (h.market_value - h.amount_invested), 0)
    const realizedTotal = cumPnl - unrealizedTotal
    lines.push(`账户累计盈亏 ${fmtPct(cumPnlPct)}（¥${fmtNum(cumPnl, 0)}）= 持仓浮盈 ¥${fmtNum(unrealizedTotal, 0)} + 已实现盈亏 ¥${fmtNum(realizedTotal, 0)}`)
  }
  if (snap.holdings.length === 0) {
    lines.push('当前持仓：（空）')
  } else {
    lines.push('当前持仓：')
    for (const h of snap.holdings) {
      lines.push(`  - ${h.fund_code}${h.fund_name ? `（${h.fund_name}）` : ''}：份额 ${fmtNum(h.shares, 2)}，市值 ¥${fmtNum(h.market_value, 0)}，仓位 ${fmtNum(h.weight * 100, 2)}%，单基浮盈 ${fmtPct(h.unrealized_pnl_pct)}，持有 ${h.holding_days}d（${h.entry_date} 起）`)
    }
  }
  if (snap.pendingOrders.length > 0) {
    lines.push('待结算订单：')
    for (const o of snap.pendingOrders) {
      lines.push(`  - #${o.order_id} ${o.order_type} ${o.fund_code} ${o.order_type === 'sell' ? `${fmtNum(o.order_amount, 2)} 份` : `¥${fmtNum(o.order_amount, 0)}`}（下单 ${o.order_date}，ref_nav ${fmtNum(o.reference_nav, 4)}）`)
    }
  }
  return `

【账户快照（今日 settle 后）】
${lines.join('\n')}`
}

function fundSeriesBlock(series: FundSeries[]): string {
  if (series.length === 0) return ''
  const parts: string[] = []
  for (const f of series) {
    parts.push(`▍ ${f.fund_code}${f.fund_name ? `（${f.fund_name}）` : ''}：近 1m ${fmtPct(f.return_1m_pct)} ｜ 近 3m ${fmtPct(f.return_3m_pct)}`)
    const navLines = f.nav_series.map(n =>
      `  ${n.date}  nav=${fmtNum(n.nav, 4).padStart(8)}  日收益 ${fmtPct(n.daily_return_pct).padStart(8)}`
    )
    parts.push(navLines.join('\n'))
  }
  return `

【持仓基金近 20 交易日 NAV + 区间收益】
${parts.join('\n')}`
}

// mcp__fund_portfolio_mcp__portfolio_get_my_performance 的 summary + trades_summary + completed_positions
// 全量摊开。原则：performance 里有的指标都给 bot 看，不挑。bot 自己决定哪个有用。
function performanceBlock(perf: PerformanceData): string {
  const lines: string[] = []
  const s = perf.summary
  const t = perf.trades

  if (s) {
    lines.push('▍ 自 Day 1 起累计')
    lines.push(`  期间：${s.first_date} → ${s.last_date}（${s.trading_days} 个交易日）`)
    lines.push(`  起始本金 ¥${fmtNum(s.initial_capital, 0)} → 最新 ¥${fmtNum(s.latest_total_value, 0)}（net_value ${fmtNum(s.latest_net_value, 4)}）`)
    lines.push(`  累计收益 ${fmtPct(s.total_return_pct)} ｜ 年化波动 ${fmtPct(s.volatility_pct_annualized)} ｜ Sharpe (rf=0) ${fmtNum(s.sharpe_ratio_rf0, 4)}`)
    lines.push(`  最大回撤 ${fmtPct(s.max_drawdown_pct)}${s.max_drawdown_date ? ` @ ${s.max_drawdown_date}` : ''}`)
  }
  if (t) {
    lines.push('')
    lines.push('▍ 交易统计（confirmed orders）')
    lines.push(`  买入 ${t.buy_count} 笔，总金额 ¥${fmtNum(t.total_buy_amount, 0)} ｜ 卖出 ${t.sell_count} 笔，总回款 ¥${fmtNum(t.total_sell_proceeds, 0)} ｜ 累计手续费 ¥${fmtNum(t.total_fees, 2)} ｜ 完整 round-trip ${t.round_trips_count} 次`)
  }
  if (perf.completedPositions.length > 0) {
    lines.push('')
    lines.push('▍ 已平仓持仓（round-trip P&L）')
    for (const p of perf.completedPositions) {
      const days = p.holding_days === null ? '?' : `${p.holding_days}d`
      lines.push(`  ${p.fund_code}${p.fund_name ? `（${p.fund_name}）` : ''}：${p.entry_date} → ${p.exit_date}（${days}），本金 ¥${fmtNum(p.total_invested, 0)} → 回款 ¥${fmtNum(p.total_proceeds, 0)}，净盈亏 ¥${fmtNum(p.net_pl, 0)}（${fmtPct(p.return_pct)}），手续费 ¥${fmtNum(p.fees, 2)}`)
    }
  }
  if (lines.length === 0) return ''
  return `

【整体绩效】
${lines.join('\n')}`
}

// 区间业绩 5 档（1m/3m/6m/1y/since_inception）。所有指标"区间口径不年化"，
// 与 fund_bot_performance 的存储一致。fallback=true 标识窗口数据不足、退化为
// since_inception，bot 看到 fallback 标记就知道这一行别太当真。
function intervalMetricsBlock(rows: IntervalMetricRow[], asOfPerfDate: string | null, rfAnnualPct: number): string {
  if (rows.length === 0) return ''
  const header = '区间               收益%       MDD%        波动%       Sharpe       Calmar      样本数      备注'
  const lineRows = rows.map(r => {
    const calmar = r.calmar_ratio === null ? 'n/a' : fmtNum(r.calmar_ratio, 4)
    const note = r.fallback ? '⚠ 窗口数据不足，退化 since_inception' : ''
    return `  ${r.period.padEnd(16)} ${fmtPct(r.return_pct).padStart(8)}   ${fmtPct(r.max_drawdown_pct).padStart(8)}   ${fmtPct(r.volatility_pct).padStart(8)}   ${fmtNum(r.sharpe_ratio, 4).padStart(8)}   ${calmar.padStart(8)}   ${String(r.data_points).padStart(6)}   ${note}`
  })
  const asOf = asOfPerfDate ? `截至 ${asOfPerfDate}` : ''
  return `

【区间业绩（${asOf}，区间口径不年化，rf=${fmtPct(rfAnnualPct, 2)} 年化）】
${header}
${lineRows.join('\n')}`
}

function indexBlock(indices: IndexQuote[]): string {
  if (indices.length === 0) return ''
  const rows = indices.map(i => {
    // 长趋势锚先行：趋势标签 + vs MA60/200。这是判方向该看的尺度。
    const trendTag = i.trend ? `【${i.trend}】` : ''
    const longParts = [
      `MA60 ${fmtNum(i.ma60, 2)}`,
      `MA120 ${fmtNum(i.ma120, 2)}`,
      `MA200 ${fmtNum(i.ma200, 2)}`,
      i.vs_ma60_pct === null ? '' : `vs MA60 ${fmtPct(i.vs_ma60_pct)}`,
      i.vs_ma200_pct === null ? '' : `vs MA200 ${fmtPct(i.vs_ma200_pct)}`,
    ].filter(Boolean).join(' ｜ ')
    // 短均线退到末尾，明确标注为"短期情绪、非趋势扳机"。
    const shortParts = [
      i.vs_ma5_pct === null ? '' : `vs MA5 ${fmtPct(i.vs_ma5_pct)}`,
      i.vs_ma20_pct === null ? '' : `vs MA20 ${fmtPct(i.vs_ma20_pct)}`,
    ].filter(Boolean).join(' ｜ ')
    return `  ${i.code} ${i.name}：${i.latest_date} 收 ${fmtNum(i.latest_close, 2)} ${trendTag}\n      趋势锚（判方向看这个）：${longParts}\n      短期情绪（非趋势扳机）：${shortParts}`
  })
  return `

【主要指数（5 个）｜ 趋势看长均线 MA60/120/200，MA5/MA20 只是短期情绪，别拿它单独翻仓】
${rows.join('\n')}`
}

// 交易费率（每日注入）：申购 / 赎回阶梯 / 管理+托管+销服年化（NAV 已扣）。
// 短线择时 round-trip = 申购费 + 早赎惩罚 + 双向 spread——这块缺位时 bot 倾向频繁交易，
// 7 日内进出会被 1.5% 赎回费咬掉短线 alpha。把费率显式摆出来，让 bot 算清"是否值得换仓"。
function tradingFeesBlock(fees: FundFee[]): string {
  if (fees.length === 0) return ''
  const lines: string[] = []
  for (const f of fees) {
    if (!f.found) { lines.push(`  - ${f.fund_code}：（未找到费率信息）`); continue }
    const purchase = f.purchase_fee_pct === undefined ? 'n/a' : `${fmtNum(f.purchase_fee_pct, 4)}%`
    // 赎回阶梯：[{max_days:7,rate_pct:1.5},{max_days:30,rate_pct:0.5},{max_days:null,rate_pct:0}]
    //   → "<7d 1.5% / <30d 0.5% / ≥30d 0%"
    const tiers = (f.redeem_tiers ?? [])
    const tierStrs: string[] = []
    for (let i = 0; i < tiers.length; i++) {
      const t = tiers[i]
      const rateStr = `${fmtNum(t.rate_pct, 4)}%`
      if (t.max_days === null || t.max_days === undefined) {
        const prev = i > 0 ? tiers[i - 1].max_days : null
        tierStrs.push(prev === null || prev === undefined ? `全程 ${rateStr}` : `≥${prev}d ${rateStr}`)
      } else {
        tierStrs.push(`<${t.max_days}d ${rateStr}`)
      }
    }
    const redeem = tierStrs.length === 0 ? 'n/a' : tierStrs.join(' / ')
    const mgmt = f.mgmt_fee_pct_annual ?? 0
    const custody = f.custody_fee_pct_annual ?? 0
    const sales = f.sales_service_fee_pct_annual ?? 0
    lines.push(`  - ${f.fund_code}${f.fund_name ? `（${f.fund_name}）` : ''}：申购 ${purchase} ｜ 赎回 ${redeem}`)
    lines.push(`    年化（NAV 已扣）：管理 ${fmtNum(mgmt, 2)}% + 托管 ${fmtNum(custody, 2)}% + 销服 ${fmtNum(sales, 2)}%`)
    if ((f.purchase_status && f.purchase_status !== 'open') || (f.redeem_status && f.redeem_status !== 'open')) {
      lines.push(`    状态：申购 ${f.purchase_status || 'open'}，赎回 ${f.redeem_status || 'open'}`)
    }
  }
  return `

【交易费率（决策前算 round-trip cost：申购 + 早赎惩罚 + 时间成本）】
${lines.join('\n')}`
}

function dailyContextBlocks(dc: DailyContextData | undefined): string {
  if (!dc) return ''
  const parts: string[] = []
  if (dc.account) parts.push(accountSnapshotBlock(dc.account))
  if (dc.performance) parts.push(performanceBlock(dc.performance))
  if (dc.performance?.intervals) {
    parts.push(intervalMetricsBlock(dc.performance.intervals.rows, dc.performance.intervals.as_of_perf_date, dc.performance.intervals.rf_annual_pct))
  }
  if (dc.fundSeries && dc.fundSeries.length) parts.push(fundSeriesBlock(dc.fundSeries))
  if (dc.indices && dc.indices.length) parts.push(indexBlock(dc.indices))
  if (dc.fundFees && dc.fundFees.length) parts.push(tradingFeesBlock(dc.fundFees))
  return parts.join('')
}

// Bot 的 methodology 已经在 system prompt 的 ## METHODOLOGY.md section 里（research-loop 每次
// chat 都 splice），daily message 不重复注入正文，只附一段短提示告诉 bot：按 methodology 决策，
// 发现失效用 mcp__strategy_mcp__update_my_strategy 重写。Day 1 / Day N 文案略有差别——Day 1 强调"直接按它交易"，
// Day N 只一行 reminder。
const METHODOLOGY_DAY1_HINT = `

【你的 methodology 已就位】你的 system prompt 里的 \`## METHODOLOGY.md\` section 就是你的投资框架。

如果跑了一段时间发现 methodology 哪里失效 / 有漏洞，可以调 \`mcp__strategy_mcp__update_my_strategy(bot_id, strategy, reason)\` 工具完整重写 METHODOLOGY.md（不是 diff，是完整新版本）。reason 写清为什么改（会进审计日志）。修改下一交易日的 system prompt 生效。不轻易改——但发现 thesis 失效或风控漏洞，该改就改。`

const METHODOLOGY_DAYN_HINT = `

【你的 methodology】已在 system prompt 的 \`## METHODOLOGY.md\` section 里——按它决策。
发现 thesis 失效 / 风控漏洞 → \`mcp__strategy_mcp__update_my_strategy(bot_id, strategy, reason)\` 完整重写（不是 diff，整篇新版本），reason 写清为什么改，下一日 system prompt 注入新版。不轻易改——但该改就改。`

export function renderDailyMessage(ctx: DailyMessageContext): string {
  const weekday = weekdayOf(ctx.date)
  const toolsBlock = simworldToolsBlock(ctx.simworldTools ?? [])
  const contextBlocks = dailyContextBlocks(ctx.dailyContext)
  // 可买池每天都播报；single-fund / multi-fund 文案分两套，由 botId 推断（解耦池子大小与 bot 决策风格）。
  const kind = botKindOf(ctx.botId)
  const buyable = ctx.buyableFundCodes && ctx.buyableFundCodes.length
    ? buyableFundsBlock(ctx.buyableFundCodes, kind, ctx.dailyContext?.buyablePoolMeta)
    : ''
  // History window 放在 daily message 的最顶部——它已经包含自己的"【交易记忆窗口】"标头，
  // 直接拼到 rules block 之前即可。空串（Day 1 / 无 prior session）→ 跳过。
  const history = ctx.historyWindow && ctx.historyWindow.trim() ? `${ctx.historyWindow.trim()}\n\n` : ''
  // Belief block 由 caller (run.ts) 先 await buildBeliefContext(...) 渲染成完整字符串塞进来；
  // 已自带 header / schema 要求 / 21d 校准反馈，本函数只前置两个换行做分隔即可。空/缺省 → 跳过。
  const beliefStr = ctx.beliefBlock && ctx.beliefBlock.trim() ? `\n\n${ctx.beliefBlock.trim()}` : ''
  if (ctx.isFirstDay) {
    // Day 1 = 冷启动：完整规则 + 工具/可买池/预取上下文 + belief（含 schema + 校准）+ methodology 提示 + 记忆边界。
    // bot 的 methodology 已被 research-loop splice 进 system prompt，daily message 只附短提示。
    return `${history}${fullRules(ctx.date, weekday, ctx.botId, ctx.tradingDaysTotal)}${toolsBlock}${buyable}${contextBlocks}${beliefStr}${METHODOLOGY_DAY1_HINT}${FOOTER_FULL}\n`
  }
  // Day N：briefRules + 工具/可买池/数据 + belief + methodology 短提示 + FOOTER_BRIEF（termination contract）。
  // FOOTER_BRIEF 的"列了 todo 就要做 + 结束前 mem0_add"对所有 bot 都适用。
  return `${history}${briefRules(ctx.date, weekday, ctx.botId)}${toolsBlock}${buyable}${contextBlocks}${beliefStr}${METHODOLOGY_DAYN_HINT}${FOOTER_BRIEF}\n`
}
