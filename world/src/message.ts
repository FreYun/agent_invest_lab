import type { DailyContextData, AccountSnapshot, FundSeries, IndexQuote, PerformanceData, IntervalMetricRow, FundFee, BuyablePoolMeta, BuyablePoolMetaRow, BenchmarkSeries } from './daily-context.ts'

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
  // 本 bot 当前产品可买基金白名单。每天都显式播报（含仓位调整指引：单只 → 纯单指数
  // 择时，多只 → 可在池内配置/轮动）；server 端 place_buy_order 也按同一份 curated 校验。
  buyableFundCodes?: string[]
  // World 在 chat 前预取的当日上下文：账户快照、近 N 日 PnL 走势、持仓基金 NAV
  // 走势、主要指数 MA。每天必看的几样直接灌进 prompt，bot 不必再调 tool 自查。
  // 任一字段缺失（fetcher 失败 / 无持仓 / day 1 没历史）就跳过对应渲染块。
  dailyContext?: DailyContextData
  /** 系统持久化的深研持仓承诺块；由 run.ts 生成，普通日同时受交易代理硬闸保护。 */
  deepResearchCommitmentBlock?: string
  // 本 run 的完整交易日历（run.ts 传 setupRes.calendarDates，即 runtime 日历全量，与 replay
  // 区间无关）。交易纪律核对块用它数「清仓后过了几个交易日」。缺省时回退 benchmark 序列，
  // 但那条路会因 runStartDate 退化或整池 NAV 超时而变 null（见 tradingCalendarOf 注释）。
  tradingCalendar?: string[]
  // 本次回测总交易日数。仅 Day 1 注入到 fullRules，让 bot 按这个时间窗口规划策略
  // （短线 / 波段 / 长持）。具体起止日期不暴露——只给"今天 + N 个交易日"——以减少
  // LLM 训练记忆按已知历史时间段反向决策的泄漏面。Day N 即便传了也不注入，避免给 bot
  // 一个"剩余天数"的 endgame 倒计时（会引发临近终点的窗口式抛售等非真实行为）。
  tradingDaysTotal?: number
  // 滚动 history window：前 N 个交易日的 session digest（去掉工具结果），由 history-window 模块
  // 按 20000 字符预算切割，超 budget 时按 5 维度（踏空 / 反复被收割 / 验证成功的打法 / 范式冲击 / thesis 演变）
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
  // 周期感知信息：仅当本决策日相对上次决策跳过了交易日（周/月度调仓、或 chat_step_days>1）才传。
  // 让 bot 明白「这是周期再平衡、下方数据覆盖整段区间」，避免低频 bot 把累计涨跌误读成单日波动。
  // tradingDays = 距上次决策的交易日数；sinceDate = 上次决策日；benchMovePct = 期间基准篮子累计涨跌（可空）。
  // undefined / tradingDays<=1 → 跳过整块（日度运行即此，行为不变）。
  periodInfo?: { tradingDays: number; sinceDate: string; benchMovePct: number | null }
  // 直接注入进 daily prompt 的"判断管线 skill"全文（不靠 load_skill——research-loop 的 system prompt
  // 每文件截 10000 字，4 个 skill 合计 ~27k 字塞不下且会被截断；改在 daily message 里整篇注入，
  // 保证 bot 每个决策日开头就读到完整 skill 算法/工具表）。由 run.ts 按 bot 读取 shadow skills 填充。
  // 空/缺省 → 跳过整块（绝大多数 bot 即此，行为不变）。
  injectedSkills?: { id: string; content: string }[]
  // 系统预读注入的市场研报 content_md（PIT：as_of_date<=世界日的最新一期）。bot101/102/103
  // 用：替代旧的 skill 注入自跑流水线——主线/regime/组合骨架由系统预生成，bot 直接消费、不自己识别。
  // 由 run.ts 从 fund.db 读出填充（4 份 market_reports + 四研判室 res1/2/4/5 的 res_reports）。空/缺省 → 跳过整块。
  marketReports?: {
    context: string; mainline: string; rotation: string; macroNews?: string
    // 四研判室宏观背景研判（res1 市场策略 / res2 政策 / res4 国际关系 / res5 跨市场）。缺省/全空 → 不渲染该子段。
    res?: { market_strategy: string; policy_analysis: string; intl_relations: string; cross_market_linkage: string }
  }
  // 系统在 chat 前预取的盘中实时行情块。仅同一天 14:30 一类实盘 OOS 决策注入；
  // 历史回测 / T+1 早盘跑昨日时为空，避免把 host 当天实时行情污染历史世界日。
  intradayMarketBlock?: string
  // 「当日研究室简报」：单指数 run 从 fund.db 按 PIT 取 res 四研判室 + macro_news + market_context
  // 拼成的 markdown，由 run.ts 的 assembleBriefing() 填充。定位＝**参考信号（宏观策略/政策/国际/
  // 跨市场/regime 研判）不是指令**。空/缺省 → 跳过整块。仅单指数 bot 注入。
  briefing?: string
  // 末条 standing belief 的关键 horizon 上涨概率（t+5 / t+20 的 p_up），由 caller 从
  // buildBeliefContext 一并取出（同一次扫盘，不二次 IO）。用于「belief ↔ 仓位 言行一致」核对块：
  // bot 上次说看多却空仓 / 说看空却重仓 = 言行不一，每天硬拦。null/缺省（无历史 belief）→ 不核对。
  latestBelief?: { tPlus5: number | null; tPlus20: number | null } | null
  // 深度研究实验（deep_research_every > 0 的 run）：
  // - deepResearchEnabled：run 级开关。research-loop 的工具集是进程级静态的——rl_config_base 放开
  //   deny 后 start_research 等工具**每天**都挂在工具列表里，只能靠 message 措辞门控使用时机。
  //   开了 → fullRules/briefRules 的"研究模式全部禁用"措辞换成"以尾部调度块为准"
  //   （【深度研究日 · 强制】必须调；【深研触发提示 · 授权可选】可按异常自主调；都没有才禁止）。
  // - deepResearchDay：本决策日是否深度研究日（第 N/2N/3N 个决策日）→ 注入【深度研究日】块。
  deepResearchEnabled?: boolean
  /** 老字段：ordinal 模式的日历强制日。等价于 deepResearchForced（老 caller 保持兼容）。 */
  deepResearchDay?: boolean
  /** 深研调度模式。'ordinal' 保持历史；'agent-triggered' 走固定间隔 + 崩盘提前触发。 */
  deepResearchMode?: 'ordinal' | 'agent-triggered'
  /** 系统强制：本决策日必须 start_research，跳过 = 违反调度纪律。 */
  deepResearchForced?: boolean
  /** 系统授权：agent-triggered 普通日也可按异常条件自主 start_research。 */
  deepResearchAuthorized?: boolean
  /** 距上次深研的交易日 gap（含今日的偏移；MAX_SAFE_INTEGER = 从未深研过）。 */
  deepResearchGapDays?: number
  /** agent-triggered 模式固定交易日间隔。 */
  deepResearchMaxGapDays?: number
  /** 上次深研日期（ISO），null/undefined = 从未。 */
  deepResearchLastDate?: string
  /** 本次强制深研的具体原因，由 run.ts 判定。 */
  deepResearchReasons?: Array<'max-gap' | 'ordinal' | 'target-move' | 'account-drawdown' | 'portfolio-loss' | 'holding-drop' | 'risk-basket'>
  // 【配置宪章】提示块：宪章未声明（Day 1）→ 声明指引；卫星复评到期/过期 → 复评提醒。
  // 由 run.ts 每日调 cli charter_status 组装；空/缺省 → 跳过整块（宪章未启用的 run 即此）。
  charterBlock?: string
}

export function weekdayOf(isoDate: string): string {
  return new Date(isoDate + 'T00:00:00Z').toLocaleString('en-US', { weekday: 'long', timeZone: 'UTC' })
}

// Bot 类型：决定 buyable 池播报的指引文案。**和池子大小解耦**——
// 即使 user 在新建回测时给单基 bot 勾了 10 只池子，单基 bot 的决策风格也不变（它只盯自己的标的择时）；
// 反之 multi-fund bot 即使只勾了 1 只，文案也不再退化成"纯择时"。决策风格本来就是 bot 的内在属性。
//
// 命名约定：
// - bot1..bot20 = 单基金
// - bot101+（三位数）= 多基金权益组合
// - bot_multi = 大类资产配置
export type BotKind = 'single-fund' | 'multi-fund' | 'multi-asset'

export function botKindOf(botId: string): BotKind {
  if (botId === 'bot_multi') return 'multi-asset'
  // bot1XX 多基金家族 + 其测试分身（带后缀的克隆，如 bot101t_k1）都算多基金。
  // `(?:[^0-9].*)?` 只允许「bot1+2位数字」后跟非数字分隔再接任意 → bot101t_k1✓、
  // bot1011✗（不误吞 4 位 id）、bot20/bot10✗（保持单基金）。
  return /^bot1\d{2}(?:[^0-9].*)?$/.test(botId) ? 'multi-fund' : 'single-fund'
}

// Prompt 不再注入静态 overview——行情走 simworld-data MCP 实时查；不再区分研究日 vs 交易日
// （budget 在 run.ts 单独管），统一让 bot 自己决定要不要展开。
function fullRules(date: string, weekday: string, botId: string, tradingDaysTotal: number | undefined, deepResearchEnabled?: boolean): string {
  // 周期块：只在 Day 1 注入，让 bot 按窗口长度规划策略；不写终止日期，仅给"今天 + N 天"。
  // Bot 仍能从 today's date + N 推出终止日，但不直接告知，减少训练记忆的 hindsight 触发面。
  return `当前世界日期：${date}（${weekday}）。
你是天天基金为散户进行财富管理的交易员，无论什么策略，什么标的，你的核心是帮用户守住本金，赚取绝对收益，这是你的目标。
在今天开始之前，你的基金账户已被初始化，你有 100 万初始现金，盈亏从 0 开始累计。

【你的 bot_id】**${botId}**。\`mcp__fund_portfolio_mcp__portfolio_place_buy_order\` / \`mcp__fund_portfolio_mcp__portfolio_place_sell_order\` / \`mcp__fund_portfolio_mcp__portfolio_get_my_history\` / \`mcp__fund_portfolio_mcp__portfolio_get_my_trades\` / \`mcp__fund_portfolio_mcp__portfolio_get_my_performance\` / \`mcp__strategy_mcp__update_my_strategy\` 等工具的 \`bot_id\` 参数必须按字面量传 \`"${botId}"\`——不是 "me"、不是 "self"、不是空字符串。传错服务端会按字面字符串匹配，结果一律是"无账户"。

【你的任务】追求绝对收益，控制账户回撤（不是最大回撤，是绝对亏损）。

【决策框架】请参考你的 **AGENTS.md**（已注入到 system prompt 的 \`## AGENTS.md\` section）——这是你的角色定位、决策风格和操作边界的总纲；再参考 **METHODOLOGY.md**（\`## METHODOLOGY.md\` section）——这是本轮 assignment 绑定的 active 产品策略，定义当前产品看什么信号、按什么规则下单。

【可用工具范围】本会话开放：mem0_search / mem0_add、list_skills / load_skill，以及 simworld-data / fund-portfolio-mcp 的所有 mcp__* 工具——**全部已直接挂进工具列表**，看到就能调，无需任何激活步骤。注意：mem0_search / mem0_add / list_skills / load_skill 是裸名工具，**不带 mcp__ 前缀**（\`mcp__simworld_data__mem0_search\` 这种名字不存在，调了必报错）。${deepResearchEnabled
    ? '文件读写、web_fetch、bash、子代理（spawn_skill_agent）全部禁用——调用会被直接拒。研究模式工具（start_research 等）虽在工具列表里，但使用时机以 message 尾部的调度块为准：【深度研究日 · 强制】= 今天必须调用一次；【深研触发提示 · 授权可选】= 可按异常条件自主调用；两种标注都没有 = 禁止调用。'
    : '文件读写、web_fetch、bash、子代理（spawn_skill_agent）、研究模式（start_research 等）全部禁用——调用会被直接拒。'}

【skill 体系】list_skills 看本 bot 装了哪些可加载的研究/判断框架，load_skill <name>（参数名 \`skill_id\`，传 skill 目录名）把 skill 内容直接载入当前对话当思考脚手架。**如果你的 METHODOLOGY 顶部标了「技能驱动」判断管线，每个决策日必须先按它列的顺序 load_skill 把那几个 skill 读进来照做，再做判断和下单——没 load 就凭印象决策 = 没按流程。** 不确定本 bot 装了哪些就先 list_skills 确认。

【数据预取】当日账户/持仓/累计绩效/区间业绩/已平仓 P&L/近 N 日 PnL 走势/持仓基金近 20 日 NAV/5 大指数 MA 已经在下方"【...】"块里全量灌好。**不要重复调用 mcp__fund_portfolio_mcp__portfolio_get_my_history / mcp__fund_portfolio_mcp__portfolio_get_my_performance / mcp__fund_portfolio_mcp__portfolio_get_my_trades** 查这些；也不要为持仓基金或这 5 大指数重复调 mcp__simworld_data__fund_nav / mcp__simworld_data__market_index_quote——直接读上下文。

【账户操作】下单走 mcp__fund_portfolio_mcp__portfolio_place_buy_order / mcp__fund_portfolio_mcp__portfolio_place_sell_order；可买基金白名单走 mcp__fund_portfolio_mcp__portfolio_get_buyable_funds（变化频率低，记下来就够）。

【研究 / 新基金 / 行业暴露 / 资金流 / 宏观 / 研报 / 商品 / 债券】这些没预取，按需直接调对应的 simworld-data 工具——它们都已在工具列表里。`
}

function briefRules(date: string, weekday: string, botId: string, deepResearchEnabled?: boolean): string {
  return `当前世界日期：${date}（${weekday}）。
牢记：你的终极目标是追求绝对收益，控制账户回撤（不是最大回撤，是绝对亏损）。
你的 bot_id = **${botId}**——所有 portfolio_* / mcp__strategy_mcp__update_my_strategy 工具的 \`bot_id\` 参数都按字面量传 \`"${botId}"\`（proxy 不会自动注入，传 "me" / "self" / 空串都会被服务端按字符串匹配判成"无账户"）。
可用 mcp__* / mem0_search / mem0_add / list_skills / load_skill（所有 mcp__* 已直接挂进工具列表，无需激活；mem0_* 和 list_skills / load_skill 是裸名，**不带 mcp__ 前缀**），文件读写和 bash 都被禁。${deepResearchEnabled ? '研究模式工具（start_research 等）使用时机以 message 尾部调度块为准：【深度研究日 · 强制】= 必须调用一次；【深研触发提示 · 授权可选】= 可按异常条件自主调用；都没有 = 禁止调用。' : ''}

今天的节奏（按顺序）：
  ① **先看下方【...】数据块**：找出账户回撤 / NAV 变化 / 指数趋势 / 区间业绩相对你昨日 thesis 有没有 drift。
  ①′ **若下方有【判断管线 skill】块**（已直接注入 skill 全文）：**严格按那些 skill 的算法和工具表实操判断**（主线/市场环境要真的调 skill 里写的 sector_*/market_temperature 等工具做出来，别只看预注入数据块拍脑袋）。没有该块、但 METHODOLOGY 标了管线的 bot，用 \`load_skill\` 读进来照做。都没有就跳过这步。
  ② **调至少 1 个非 mem0 工具拉今日新数据**：从工具列表里的 mcp__simworld_data__* 工具中选择，你需要按实际市场情况来选择工具调用，你 methodology 五视角里今天还没覆盖的那个——估值 / 趋势 / 景气度 / 资金面 / 证伪 **这一步缺，整天等于没做。**
  ③ mem0_search 拉过去 thesis / 决策，与今天的数据对比是 still valid 还是已破。**默认会按"最近优先"衰减打分（τ=30 天），近一周的记录天然浮在前面**；想只看最近几天就传 \`start_date=YYYY-MM-DD\`（比如今天往前 7 天），想关掉衰减拉全历史就传 \`recency_tau_days=0\`。
  ④ 决策（**请参考你的 AGENTS.md**——角色与决策风格总纲；以及 METHODOLOGY.md——本轮 active 产品策略与仓位管理方法论） + 下单（如有）。
  ⑤ mem0_add 落库今天的判断 + 明天要带进来的事。

 铁律：严禁每天都进行一样的工具调用和决策流程——比如每天都只调同一个工具、每天都只看行情不研究、每天都只按技术面决策不考虑估值和资金面……**要根据实际市场情况和账户状态灵活调整**，不能变成机械的"每天都做一样的事"。
【数据预取】当日账户/绩效/PnL/持仓 NAV/5 大指数 MA 已在下方块内全量灌好。不要重复调 mcp__fund_portfolio_mcp__portfolio_get_my_history / mcp__fund_portfolio_mcp__portfolio_get_my_performance / mcp__fund_portfolio_mcp__portfolio_get_my_trades，也不要为持仓基金或这 5 大指数重复调 mcp__simworld_data__fund_nav / mcp__simworld_data__market_index_quote。下单直接用 mcp__fund_portfolio_mcp__portfolio_place_buy_order / mcp__fund_portfolio_mcp__portfolio_place_sell_order。研究新基金 / 行业 / 资金面 / 宏观 / 研报这些没预取，按需调对应 simworld-data 工具。`
}

const FOOTER_FULL = `

【记忆与连续性】每个世界日是独立会话，你不会自动记得昨天。
- 决策前：用 mem0_search 调取相关的历史交易/复盘记忆。
- 决策后：把今天的判断、操作、理由、要在下次想起的事用 mem0_add 落进记忆。

【每日决策硬契约】
- **第一轮先用正文写出当日计划**（\`todo_write\` / \`update_todo_status\` 已停用，不要去调）：列出“风险审计 / 持仓与候选核验 / 下单或明确 hold / 收尾复盘”四项，各一行写在正文里，然后在同一轮就开始拉数据。计划写成正文而不是待办工具，是因为正文会进入后续每一轮的上下文，待办工具则要额外烧掉一整轮往返。
- 最后一条 assistant message 的买入侧、卖出侧必须按当日实际动作动态输出，不要每天固定写四项：有买入时只写 \`### 为什么买\` + \`### 买入动作\`；无买入时只写 \`### 为什么不买\`。有卖出时只写 \`### 为什么卖\` + \`### 卖出动作\`；无卖出时只写 \`### 为什么不卖\`。动作栏写标的、金额/份额与实际结果；不动作栏写被否决的候选、未满足的触发器或继续持有依据。

【边界】这是一次交易回合，不是研究项目；下了单 + mem0_add 写完今天的判断，就可以结束。`

// Day N 收尾合约：解决"列 todo 当 reflection 写但不执行 + 写了'明天再减仓'但今天不动 +
// 不 mem0_add 就 day-end + 纯 mem0 当一天做完"这几种早收。Day 1 已经在 FOOTER_FULL 里说过类似
// 的话，但 Day N 之前没 footer——LLM 默认"没事做了就停"，不会被"持仓未平 / 待办未做 / 没写复盘 /
// 没查新数据"这些条件拦住。这段补五条 termination contract：
// (0) 今天必须至少调一个非 mem0 的研究/数据工具——不然 mem0_search → mem0_add 会变成 reward
//     hack，bot 用过去的笔记复述出一篇看似 thoughtful 的 reply 就收工，而 dailyContext 里今天
//     的新数据完全没被验证，thesis 永远不会失效（dash-2026-05-19T08-52-36 实测：bot7 从 Day 6
//     起 9 iter → 3 iter，60% 仓位整月 HOLD，60 天没碰新数据）；
// (1) 今天的决策必须今天执行——把减/加仓推到下一日 = 决策蒸发（下一日新会话不继承"明天计划"）；
// (2) reply 里列出的"待办 / 要查的 / 要验证"必须执行掉，或显式放下并写明理由——不允许列了不做；
// (3) 结束前必须 mem0_add，否则下一日的 bot 看不到今天的判断和待办；
// (4) **最后一条 assistant message 必须是结构化当日决策 markdown**——不然详细分析被 bot 写在
//     中间轮（mem0_add / update_my_strategy / place_order 这些工具调用之前的 reasoning turn），
//     最后 reply 只留"决策完成"/"methodology 更新完成"/单个 belief yaml 块。dashboard 端
//     pickDecisionText 有 fallback 能兜一部分，但从 bot 侧稳定输出结构化 reply 才是根治。
//     bot102 2026-07-28 实测：reply.json 里 reply 字段 791 字全是 belief yaml，详细决策留在
//     msg[2] 的 540 字中间轮，market-reports 页面「当日思考」卡片没内容可展示。
const FOOTER_BRIEF = `

【结束之前必做】
- **每一轮都要留下痕迹**：每次调用工具的那条消息里，同时用 2-4 行正文写清「本轮得出了什么结论 / 下一步要验证什么」。你的推理过程不会保留到下一轮，只有正文会——结论留在思考里等于没做，下一轮的你要从头再推一遍。风险审计、持仓比对、费率权衡这类算完就走的分析尤其要落到正文：把关键数字和结论写出来，哪怕只有三行。
- **当日计划是每日必做，但写成正文、不要调待办工具**（\`todo_write\` / \`update_todo_status\` 已停用）：第一轮正文里列出“风险审计 / 持仓与候选核验 / 下单或明确 hold / 收尾复盘”四项，各一行；后续轮次在正文里顺带更新进度。不得以“今天任务简单”为由跳过计划。
- **纯 mem0_search + mem0_add 不算完成一天**。今天必须至少 1 次调用非 mem0 的研究/行情/数据工具（mcp__* 任一，除 mcp__fund_portfolio_mcp__portfolio_get_my_history / mcp__fund_portfolio_mcp__portfolio_get_my_performance / mcp__fund_portfolio_mcp__portfolio_get_my_trades 外，那几个 dailyContext 已经灌好了）——验证 dailyContext 里某个数据点、拉一个 methodology 里今天还没覆盖的维度、或检验 thesis 是否破。不查就 mem0 落库 = 自欺欺人，下一日你 mem0_search 拉到的全是空想，回测就这么烂下去。
- **今天的决策今天就发生**：研究结论是减仓 → 调 mcp__fund_portfolio_mcp__portfolio_place_sell_order；加仓 → 调 mcp__fund_portfolio_mcp__portfolio_place_buy_order；保持 → 明确说"今日维持 X% 仓位，不动，理由是 ..."。把"明天减仓至 Y%"写进 mem0 ≠ 执行——下一日是新会话，看不到今日规划，等于决策从未发生。
- 如果你在思考里列出了"待办 / 要查的 / 要验证"，要么在结束前调工具做掉，要么明确说"这条今天先放下，理由是 X，明天再做"。列了不做 = 没列——明天的你会以为今天已经查过了。
- 结束前一次 mem0_add：今天的判断 + 做了什么 / 没做什么 + 明天要带着什么进来。没 mem0_add 就结束，下一日的你看不到今天，整天的研究就白做。
- **最后一条 assistant message = 结构化当日决策 markdown**。所有工具调用（下单 / mem0_add / update_my_strategy / update_methodology 等）全部结束之后，你还要再输出一段独立的最终回复，内容必须包含：**至少 1 个 markdown 表格**（持仓 / 动作 / 目标权重 任一），加上覆盖 **风险状态 / 市场环境 / 主线判断 / 今日动作 / 执行结果 / 总仓位 / 关键观察** 的分析要点。决策栏目按实际动作二选一，**禁止每天固定写四项**：买入侧——有买入写 \`### 为什么买\` + \`### 买入动作\`，无买入只写 \`### 为什么不买\`；卖出侧——有卖出写 \`### 为什么卖\` + \`### 卖出动作\`，无卖出只写 \`### 为什么不卖\`。动作栏记录标的、金额/份额和实际结果；不动作栏说明候选为何被否决、触发器为何未满足或为何继续持有。这条最终消息是外部 dashboard「当日思考」卡片唯一展示的入口——**不允许**把详细分析全写在中间轮然后最后只回一句"决策完成"/"methodology 已更新"/"mem0_add 已存"/一个孤立的 belief yaml 块。即便中间轮已经写过完整推理，收尾时也要把当日决策的**核心版本**再落一遍作为最后一条 reply，让复盘能看到你今天真正想了什么、做了什么。`

// 注意：这里**不再**注入 simworld-data 工具目录文本块。2026-06-10 起所有 mcp__simworld_data__*
// 经 run.ts 的 tools.always_load 原生挂进工具列表——name + description（来自上游 docstring）+ 参数
// schema 模型每轮天然可见，文字目录是纯重复（实测 ~2.4K 字/日，占 daily message 8%+），且"带前缀
// 直接照抄"的措辞还诱发过 bot 把前缀泛化到 mem0_search 上（mcp__simworld_data__mem0_search 幻觉，
// dash-2026-06-09T15-31-28 全程 29 次）。"研究类工具也存在"的提示由 fullRules/briefRules 的
// 【可用工具范围】和 FOOTER_BRIEF 的非 mem0 工具硬约束承担。别把目录块加回来。

// 每天都显式播报本 bot 当前产品可买池 + 仓位调整指引。指引文案**按 bot 类型决定**（不是按池子大小）：
// - single-fund bot：只做单指数择时（调仓位高低、无标的轮动），即便池子里有多只也只盯本轮 assignment 指定的产品/基金
//                    池子用扁平代码列表渲染（小，bot 看名字就够）。
// - multi-fund bot：组合配置 + 仓位 + 池内轮动 三件事。**按 theme 聚合 + top-K 候选** 视图渲染，
//                    bot 一眼能看到主题分布，不用 N 次 RPC 自己探索。
// 服务端 place_buy_order 按完整 curated 列表校验——prompt 里展示的是 top-K，bot 真买第 K+1 名也能买。
function buyableFundsBlock(codes: string[], botKind: BotKind, poolMeta?: BuyablePoolMeta): string {
  if (botKind === 'multi-fund' && poolMeta && poolMeta.rows.length > 0) {
    return multiFundPoolBlock(codes, poolMeta)
  }
  const guidance = botKind === 'single-fund'
    ? `**你是单基金 bot** —— 不论池子里有几只，你只做单指数择时：盯本轮 assignment / 当前 active 产品策略指定的产品或基金，决策就是调整它的仓位高低（空仓 ↔ 满仓之间），不要做标的轮动、不要把仓位分散到多只。`
    : botKind === 'multi-asset'
      ? `**你是大类资产配置 bot** —— 先在 A股基金 / 债券基金 / 黄金基金 / 货币或现金 四类资产之间决定目标权重，再在每类资产里挑 1-2 只代表基金表达。先做资产配置，再做基金选择；如果可买池里没有货币基金，账户现金直接承担现金角色。不要把自己退化成单指数择时，也不要只做权益主题轮动。`
      : `**你是多基金 bot** —— 在池内做三件事：① 组合配置（各基金目标权重）② 总仓位高低 ③ 池内轮动（换标的）。决策时考虑相关性、行业暴露、单基上限，不要把全部仓位押在一只上。`
  return `

【本 bot 当前产品可买池（${codes.length} 只）】
${codes.join(', ')}

${guidance}
下单时 fund_code 必须从这份本 bot 产品池里选；不在这份里的会被 mcp__fund_portfolio_mcp__portfolio_place_buy_order 直接拒。`
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

  lines.push(`【本 bot 当前产品可买池主题分布（共 ${codes.length} 只）｜ ${piteline}】`)
  lines.push(...singleThemeRows)
  if (comboLines.length) {
    lines.push('')
    lines.push(`▼ 多类共振组合（${comboNames.length} 个组合，决策时谨慎——往往同时承担两条主线的风险）：`)
    lines.push(...comboLines)
  }

  return `

${lines.join('\n')}

**你是多基金 bot**——若下方有【判断管线 skill】块，**今天先按那些 skill 实操判断再决策**（主线识别要真的用 sector_* 工具选出主线板块，不能只看上面的主题分布拍脑袋）。然后按 METHODOLOGY 的"主线 → 候选 → 选品"三步收敛：先看上面的主题分布锁定主题（步 1），再按 size_style/invest_style 因子收敛（步 2），最后对剩 1-3 只候选调 \`get_fund_detail\` 拉 3y/5y 业绩深查（步 3）。**别再只看宽基相对强弱、PE 分位就下单——那是单基金 bot 的玩法。**
下单时 fund_code 必须从本 bot 当前产品可买池里选（实际校验的是完整 ${codes.length} 只池子，不限于上面展示的 top-K）；不在池里的会被 mcp__fund_portfolio_mcp__portfolio_place_buy_order 直接拒。`
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
    lines.push(`  近20交易日高水位回撤 ${fmtPct(s.rolling_20d_drawdown_pct)} ｜ 20日峰值 ¥${fmtNum(s.rolling_20d_peak_total_value, 0)}${s.rolling_20d_peak_total_value_date ? ` @ ${s.rolling_20d_peak_total_value_date}` : ''} ｜ 样本 ${fmtNum(s.rolling_20d_observations, 0)} 日`)
    lines.push('  规则口径：≥6% 降档只使用上一行“近20交易日高水位回撤”，禁止用全历史回撤代替。')
    lines.push(`  全历史高水位回撤 ${fmtPct(s.current_drawdown_pct)} ｜ 历史峰值 ¥${fmtNum(s.peak_total_value, 0)}${s.peak_total_value_date ? ` @ ${s.peak_total_value_date}` : ''}`)
    const givebackRatio = s.peak_profit_giveback_ratio_pct == null
      ? 'n/a（历史峰值尚无利润垫）'
      : fmtPct(s.peak_profit_giveback_ratio_pct)
    lines.push(`  收益回吐 ${fmtPct(s.profit_giveback_pct_of_initial)}（占初始本金，¥${fmtNum(s.profit_giveback_amount, 0)}）｜ 峰值利润回吐率 ${givebackRatio}`)
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

// 区间业绩 5 档（1m/3m/6m/1y/since_inception）。统一年化口径：收益/MDD 是区间原值，
// 年化收益/波动/Sharpe/Calmar 按 252 交易日年化——与 fund_bot_performance 的存储一致。
// fallback=true 标识窗口数据不足、退化为 since_inception，bot 看到 fallback 标记
// 就知道这一行别太当真。
function intervalMetricsBlock(rows: IntervalMetricRow[], asOfPerfDate: string | null, rfAnnualPct: number, bm?: BenchmarkSeries): string {
  if (rows.length === 0) return ''
  // 滚动超额（躺平%/超额pp）：pointsByDate 是「相对 run 起点的累计 %」，取窗口两端换算成区间收益。
  // 病根：只给「自 Day 1 累计超额」时，早期攒下的 alpha 会把后面连续数月的跑输掩盖住（实测滚动
  // 1m 转负后 3 个月、滚动 3m 转负后 1 个月，累计口径才转负），bot 于是次次复盘选"方法论仍有效"。
  // 对不上（无基准 / 日期错位 / 窗口不足）一律留 n/a，绝不外推。
  const bmDates = bm ? Object.keys(bm.pointsByDate).sort() : []
  let bmEnd = -1
  for (let k = 0; k < bmDates.length; k++) {
    if (!asOfPerfDate || bmDates[k] <= asOfPerfDate) bmEnd = k
  }
  const bmWindowRet = (r: IntervalMetricRow): number | null => {
    if (!bm || bmEnd < 0) return null
    const end = bm.pointsByDate[bmDates[bmEnd]]
    if (end === undefined) return null
    if (r.period === 'since_inception') return end
    const startIdx = bmEnd - (r.data_points - 1)
    if (startIdx < 0) return null
    const start = bm.pointsByDate[bmDates[startIdx]]
    if (start === undefined) return null
    return ((1 + end / 100) / (1 + start / 100) - 1) * 100
  }
  const header = '区间               收益%      躺平%     超额pp      年化收益%      MDD%      年化波动%     Sharpe       Calmar      样本数      备注'
  const lineRows = rows.map(r => {
    const annRet = r.annualized_return_pct == null ? 'n/a' : fmtPct(r.annualized_return_pct)
    const calmar = r.calmar_ratio === null ? 'n/a' : fmtNum(r.calmar_ratio, 4)
    const note = r.fallback ? '⚠ 窗口数据不足，退化 since_inception' : ''
    const bmRet = bmWindowRet(r)
    const bmCell = bmRet === null ? 'n/a' : fmtPct(bmRet)
    const exCell = bmRet === null ? 'n/a' : fmtSignedPP(r.return_pct - bmRet)
    return `  ${r.period.padEnd(16)} ${fmtPct(r.return_pct).padStart(8)}  ${bmCell.padStart(8)}  ${exCell.padStart(8)}   ${annRet.padStart(9)}   ${fmtPct(r.max_drawdown_pct).padStart(8)}   ${fmtPct(r.volatility_pct).padStart(9)}   ${fmtNum(r.sharpe_ratio, 4).padStart(8)}   ${calmar.padStart(8)}   ${String(r.data_points).padStart(6)}   ${note}`
  })
  const asOf = asOfPerfDate ? `截至 ${asOfPerfDate}` : ''
  return `

【区间业绩（${asOf}，年化收益/波动/Sharpe/Calmar 按252交易日年化，rf=${fmtPct(rfAnnualPct, 2)} 年化）${bm ? `｜躺平 = ${bm.name}，超额 = 你 − 躺平` : ''}】
${header}
${lineRows.join('\n')}${bm ? '\n  ↑ 判断"这一段择时在不在创造价值"，看【超额pp】那一列的 1m/3m/6m——不是看收益% 那列的绝对数。绝对收益为正但超额为负 = 大盘抬着你、你在减损。since_inception 的超额会被早期战果长期掩盖，它转负时通常已经连续跑输好几个月了。' : ''}`
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
    parts.push(intervalMetricsBlock(dc.performance.intervals.rows, dc.performance.intervals.as_of_perf_date, dc.performance.intervals.rf_annual_pct, dc.benchmark))
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
// 会话边界块：一次会话只处理一个交易日。曾观察到 bot 在 Day N 会话内自我推演 Day N+1
// （虚构次日行情继续"日度决策"），既污染 reply 提取（reply 取最后一条 assistant 消息）
// 又破坏逐日回放语义。放在 message 尾部；Day-N 复盘日仍让 coherence/review 收尾
// （"复盘是收工前最后一条硬约束"的既有契约不动）。
const CHAT_BOUNDARY = `

【会话边界 · 硬约束】本次会话只处理**当前世界日期**这一个交易日。完成当日 mem0_add 后即收尾结束——**未来日期的行情、消息、决策由系统在下一次会话注入**，你现在没有明天的数据、看不到明天的行情，任何针对未来日的推演/下单/mem0 记录都是**幻觉**。

**自我检测**（继续行动前先核对）：
- 你正要写的 mem0_add ref 日期 / decision-day label 必须 = 当前世界日期。若准备写 \`[YYYY-MM-DD DayN+? …]\` 而 YYYY-MM-DD 或 DayN+? 指向**未来某日**——立刻停止，只保留当前日期这条 mem0_add 收尾。
- 你正要调的 \`portfolio_place_buy_order\` / \`portfolio_place_sell_order\` 会以**当前世界日期**为 order_date 落到 fund.db。若你脑子里觉得"这是明天的加仓/减仓"——那是幻觉，撤单，改成当日执行或不下。
- 单次会话里下一份"次日决策"/"下周计划"/"分批分步执行" mem0 记录 → 违规级别 = **严重**。审计会抓，收尾结束时也会自查。

**为什么这条硬**：先前的 run 里 bot 曾在 Day 26 一个会话里连着写了 Day 27~35 的 mem0 决策 + 下了 6 单实盘，全部 order_date=Day26 世界日期。结果是**次日 settle 时全部一次性成交**，组合被过度交易到破。这是回测里最贵的失控模式，别当第二次教训。`

const METHODOLOGY_DAY1_HINT = `

【你的 active methodology 已就位】你的 system prompt 里的 \`## METHODOLOGY.md\` section 是本轮 assignment 绑定的 active 产品策略，不是你的固定人设。

如果跑了一段时间发现 active methodology 哪里失效 / 有漏洞，可以调 \`mcp__strategy_mcp__update_my_strategy(bot_id, strategy, reason)\` 工具完整重写当前 METHODOLOGY.md（不是 diff，是完整新版本）。reason 写清为什么改（会进审计日志）。修改下一交易日的 system prompt 生效。不轻易改——但发现 thesis 失效或风控漏洞，该改就改。`

const METHODOLOGY_DAYN_HINT = `

【你的 active methodology】已在 system prompt 的 \`## METHODOLOGY.md\` section 里——这是本轮 assignment 绑定的当前产品策略，按它决策。
发现 thesis 失效 / 风控漏洞 → \`mcp__strategy_mcp__update_my_strategy(bot_id, strategy, reason)\` 完整重写（不是 diff，整篇新版本），reason 写清为什么改，下一日 system prompt 注入新版。不轻易改——但该改就改。`

function authorityChainReminder(botId: string): string {
  if (botId !== 'bot105g') return ''
  return `

【bot105g 控制变量 · 系统风险报警器】
日常买卖、regime、主线、仓位与深研纠偏只按 active METHODOLOGY 的老 run 基线执行。新增的 CB20 / VIX 风险模块只负责异常报警，不成为新的日常决策层：

- \`normal\` 只表示报警器没有响，不是市场安全、risk_on、允许加仓或必须 hold 的证据；不得用它抵消 active METHODOLOGY 已有的情绪、主线、流动性、趋势、资金与深研判断。
- \`warning/hard\` 单独出现时只强制立即复核市场与组合、允许异常深研并在总结中显著报警；它本身不直接产生买卖单。
- 唯一联动动作沿用 bot105gr 账户风控口径：因子为 \`warning/hard\`，并且整个账户满足 \`DD_account <= -10%\`，或同时满足 \`GB_pp >= 10pp\` 且 \`GB_profit >= 20%\`，才触发联合强制降险；一次性降低当前权益风险敞口 35%–50%，且总权益绝对降幅不少于 20pp。同一基金/指数方向卖出后 7 个交易日内不得反向买回。
- 15 日最小持有期是独立硬约束：未满 15 个交易日的仓位，只有绝对生命线/重大证伪、市场风险 high/extreme 或账户回撤闸门触发时才允许提前卖；报警器本身、普通状态机剔除、一般技术波动或深研均不能单独突破。`
}

// ============================================================================
// 策略强制复盘条款（每 REVIEW_CADENCE_DAYS 个交易日触发一次）
// ----------------------------------------------------------------------------
// 背景：bot 几乎从不调 update_my_strategy（全历史仅 32 次，最近 8 个 run 里 7 个为 0、最新 run 为 0）。
// 根因——每日 FOOTER_BRIEF 的 termination contract 只约束"调研/下单/mem0_add"，从不要求 bot 审视
// 方法论本身是否失效；METHODOLOGY_DAYN_HINT 又用"不轻易改"把 update_my_strategy 劝退。于是 bot 走完
// checklist 就合法收工，策略漂了也没人改。
//
// 这块按 bot 自己的累计交易日数周期性（每 5 日）强制 bot：① 先对当前市场大趋势做定性判断（上行/下行/
// 震荡，依据长均线排列），作为"方法论是否还匹配当前 regime"的锚；② 看"你 vs 不择时躺平基准"的硬对照
// （累计收益 + 最大回撤——两个口径直接可比的数，刻意不放 Sharpe 避免 rf/年化口径错配误导）；③ 诚实判断
// 方法论是否失效，失效 → update_my_strategy 重写 OR mem0 书面论证为何维持（二选一，不允许沉默跳过）。
// 触发纯按周期（产品决定：到复盘日即视为需审视，不做 off-cadence 硬触发）；trading_days 直接取
// performance.summary，无需 caller 额外传参。summary 缺（Day 1 / 无历史）或非复盘日 → 返回空串跳过。
const REVIEW_CADENCE_DAYS = 5

function isReviewDay(dc: DailyContextData | undefined): boolean {
  const td = dc?.performance?.summary?.trading_days
  return typeof td === 'number' && td > 0 && td % REVIEW_CADENCE_DAYS === 0
}

// pct 差（百分点口径，带符号），用于"超额收益"这种 a-b 的差值——和 fmtPct 的"这是个百分比"语义区分开。
function fmtSignedPP(n: number): string {
  if (!Number.isFinite(n)) return 'n/a'
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}pct`
}

// 周期感知块：周/月度（或 chat_step_days>1）决策日才注入。让 bot 明确「这是一次跨越 N 个交易日的
// 周期再平衡」——下方所有数据块覆盖的是从上次决策到今天的整段区间，按区间视角判断趋势/回撤，不要把
// 累计涨跌误读成单日波动；也提醒中间交易日不能补做交易。daily 运行不传 periodInfo → 返回空串。
function periodBlock(p: DailyMessageContext['periodInfo']): string {
  if (!p || p.tradingDays <= 1) return ''
  const move = typeof p.benchMovePct === 'number'
    ? `，期间你的基准篮子累计 ${p.benchMovePct >= 0 ? '+' : ''}${p.benchMovePct.toFixed(2)}%`
    : ''
  return `

【⏱ 调仓周期：距上次决策已过 ${p.tradingDays} 个交易日（上次决策日 = ${p.sinceDate}${move}）】
这是一次**周期再平衡决策**，不是单日操作。下方所有数据块覆盖的是从上次决策到今天的**整段区间**——请按区间视角判断趋势与回撤，把"这段时间发生了什么"作为决策依据，不要把累计涨跌误读成单日波动。中间的交易日你没有被唤起，系统只做了结算与收盘核算；这段错过的行情不能补做交易，今天的决策要把整段区间一并考虑进去。`
}

// 复盘升级条款阈值：累计跑输躺平 ≥10pct 且当前仓位 <20% → 选项②（论证维持）失效，必须走①重写。
// 教训（dash-2026-06-10T03-20-05 bot5）：150+ 交易日 0% 仓位踏空 +30%，每次复盘都靠"回撤优于躺平"
// 选②混过去——对照只看回撤时，空仓永远自评有效。把"长期空仓踏空"对称纳入失效判据。
const ESCALATE_ALPHA_PP = -10
const ESCALATE_POS_WEIGHT = 0.2

// 复盘块入口：按 botKind 岔开。**本次"日常复盘软化"只作用单指数（bot1~20）**；多基金
// （bot101/102/103）走原版逻辑，一字未动（用户硬约束：要改只能改单指数）。
function strategyReviewBlock(dc: DailyContextData | undefined, botId: string): string {
  if (!isReviewDay(dc)) return ''
  return botKindOf(botId) === 'single-fund'
    ? singleFundReviewBlock(dc!, botId)
    : multiFundReviewBlock(dc!, botId)
}

// 多基金（bot101/102/103）原版强制复盘——保持改造前行为，请勿在此施加单指数的软化。
function multiFundReviewBlock(dc: DailyContextData, botId: string): string {
  const s = dc.performance!.summary!
  const bm = dc.benchmark
  const m = bm?.metrics
  const lines: string[] = []
  lines.push(`【⚠ 第 ${s.trading_days} 个交易日 · 策略强制复盘（每 ${REVIEW_CADENCE_DAYS} 个交易日一次，今天不可跳过）】`)
  lines.push('这是硬契约。按【第一步 定性大趋势 → 第二步 业绩对照 → 第三步 二选一】走完，诚实判断：你的方法论现在还成立吗，还是已经失效（跑输躺平 / 回撤失控 / thesis 被市场证伪）？')
  lines.push('')
  lines.push('▍ 第一步 · 先对当前市场大趋势做一句话定性判断：上行（牛市 / 主升段）｜ 下行（熊市 / 主跌段）｜ 震荡（盘整 / 磨底 / 筑顶）。依据已注入的主要指数长均线排列（MA60/120/200 多空）+ 你持仓标的所处位置，别用单日涨跌代替趋势。趋势 regime 变了而方法论没跟上，是最典型的失效——先锚定它，再看下面的业绩对照。')
  lines.push('')
  let escalate = false
  // 当前仓位权重（账户快照取自今日 settle 后）。account 缺失时无法判定，不触发升级。
  const acct = dc.account?.account
  const posWeight = acct && acct.total_value > 0 ? acct.market_value / acct.total_value : null
  if (m) {
    const alpha = s.total_return_pct - m.return_pct
    const ddGap = s.max_drawdown_pct - m.max_drawdown_pct  // 回撤都是负数；你的更负=回撤更深=ddGap<0
    const verdict = alpha < 0 ? `你已跑输躺平 ${Math.abs(alpha).toFixed(2)}pct` : `你领先躺平 ${alpha.toFixed(2)}pct`
    lines.push(`▍ 第二步 · 你 vs ${bm!.name}（不择时买入持有）· 自 Day 1 起累计`)
    lines.push(`  累计收益：你 ${fmtPct(s.total_return_pct)} ｜ 躺平 ${fmtPct(m.return_pct)} ｜ 超额 ${fmtSignedPP(alpha)}（${verdict}）`)
    lines.push(`  最大回撤：你 ${fmtPct(s.max_drawdown_pct)} ｜ 躺平 ${fmtPct(m.max_drawdown_pct)} ｜ 差 ${fmtSignedPP(ddGap)}（负=你回撤更深）`)
    lines.push('  → 你做了一通择时/选品，结果若既没跑赢躺平、回撤又更深，方法论大概率已失效。')
    lines.push('  → 对照是双向的："回撤比躺平浅"不能单独证明方法论有效——空仓时回撤天然小，代价是上面那行超额。**长期低仓位 + 大幅跑输 = 踏空，和持仓回撤一样是失效证据。**')
    escalate = alpha <= ESCALATE_ALPHA_PP && posWeight !== null && posWeight < ESCALATE_POS_WEIGHT
  } else {
    lines.push(`▍ 第二步 · 你 · 自 Day 1 起累计：收益 ${fmtPct(s.total_return_pct)} ｜ 最大回撤 ${fmtPct(s.max_drawdown_pct)}`)
    lines.push('  （本轮无躺平基准对照，按绝对收益是否达标、回撤是否失控自判方法论有效性。）')
  }
  lines.push('')
  if (escalate) {
    lines.push(`▍ 第三步 · ⛔ 升级条款已触发（累计跑输躺平 ≥${Math.abs(ESCALATE_ALPHA_PP)}pct 且当前仓位 ${fmtNum((posWeight ?? 0) * 100, 1)}% < ${ESCALATE_POS_WEIGHT * 100}%）——选项②今天不可用：`)
    lines.push('  长期不出手本身就是被证伪的 thesis："等条件满足再进场"的条件被市场反复路过而你从未进场，说明触发器定义有结构性问题（典型：分位/温度类指标"涨=贵=过热=偏空"，趋势市里永远投反对票，凑不齐同号确认）。')
    lines.push(`  今天必须调 \`mcp__strategy_mcp__update_my_strategy(bot_id="${botId}", strategy, reason)\` 完整重写 METHODOLOGY.md。新版必须回答三件事：① 哪个维度长期投反对票导致永不建仓，怎么改；② 中性档对应多少基准仓位（0% 不是中性，是满仓押注下跌）；③ 什么客观硬信号下允许右侧追入。若论证后仍认为该空仓，就把"为何此环境 0% 最优 + 何时必须重新进场的客观触发器"写进新版——空仓可以是结论，不能是惯性。`)
  } else {
    lines.push('▍ 第三步 · 今天必须二选一（不允许沉默跳过——既不改也不论证 = 违约）：')
    lines.push(`  ① 判断方法论已失效 → 调 \`mcp__strategy_mcp__update_my_strategy(bot_id="${botId}", strategy, reason)\` 完整重写 METHODOLOGY.md（整篇新版本，不是 diff）。reason 写清：哪条 thesis 破了、被什么数据证伪、新版怎么改。`)
    lines.push('  ② 判断方法论仍成立 → mem0_add 写下"复盘结论：方法论仍有效"，并逐条反驳上面每个负面信号（为什么跑输只是暂时、回撤在容忍内、thesis 仍未破），给出数据依据——不是空喊"再观察"。注意：连续多次复盘都选②而超额持续恶化，是"用纪律包装惯性"的红旗——跑输扩大到升级线（跑输 ≥10pct 且仓位 <20%）时②会被直接禁用。')
  }
  lines.push('判据别只盯一天涨跌：结合上方【信念校准】块的 Brier / 活性 + 第一步的趋势定性 + 第二步的累计对照一起判。该改就改，别用"不轻易改"麻痹自己。')
  return `\n\n${lines.join('\n')}`
}

// 单指数（bot1~20）软化版：升级（踏空闸门）保持原样硬约束；日常复盘去掉"自 Day 1 累计 vs 躺平"
// 的全程记分牌，改成滚动近一段自评 + "无具体证伪就维持"，掐掉回测里"偷看整条已知净值→把趋势/情绪
// 权重拉满"的过拟合重写（教训：bot19 化工照此把价差/油煤框架掀成大盘+情绪择时器，把 +52% 躺平做成 +10%）。
function singleFundReviewBlock(dc: DailyContextData, botId: string): string {
  const s = dc.performance!.summary!
  const bm = dc.benchmark
  const m = bm?.metrics
  const lines: string[] = []
  lines.push(`【⚠ 第 ${s.trading_days} 个交易日 · 策略强制复盘（每 ${REVIEW_CADENCE_DAYS} 个交易日一次，今天不可跳过）】`)
  lines.push('这是硬契约。按【第一步 定性大趋势 → 第二步 业绩对照 → 第三步 二选一】走完，诚实判断：你的方法论现在还成立吗，还是已经失效（趋势 regime 切换没跟上 / 某条 thesis 被市场证伪 / 长期空仓踏空）？')
  lines.push('')
  lines.push('▍ 第一步 · 先对当前市场大趋势做一句话定性判断：上行（牛市 / 主升段）｜ 下行（熊市 / 主跌段）｜ 震荡（盘整 / 磨底 / 筑顶）。依据已注入的主要指数长均线排列（MA60/120/200 多空）+ 你持仓标的所处位置，别用单日涨跌代替趋势。趋势 regime 变了而方法论没跟上，是最典型的失效——先锚定它，再看下面的业绩对照。')
  lines.push('')
  // 当前仓位权重（账户快照取自今日 settle 后）。account 缺失时无法判定，不触发升级。
  const acct = dc.account?.account
  const posWeight = acct && acct.total_value > 0 ? acct.market_value / acct.total_value : null
  // 升级（踏空闸门）只在「累计大幅跑输躺平 + 当前低仓位」= 现金囤积型踏空时触发：仍按累计口径，
  // 此时才把"自 Day 1 累计 vs 躺平"的硬对照亮出来（踏空错过的涨幅就是逼它动手的证据）。
  const alpha = m ? s.total_return_pct - m.return_pct : null
  const escalate = alpha !== null && alpha <= ESCALATE_ALPHA_PP && posWeight !== null && posWeight < ESCALATE_POS_WEIGHT
  if (escalate) {
    const ddGap = s.max_drawdown_pct - m!.max_drawdown_pct  // 回撤都是负数；你的更负=回撤更深=ddGap<0
    lines.push(`▍ 第二步 · 你 vs ${bm!.name}（不择时买入持有）· 自 Day 1 起累计`)
    lines.push(`  累计收益：你 ${fmtPct(s.total_return_pct)} ｜ 躺平 ${fmtPct(m!.return_pct)} ｜ 超额 ${fmtSignedPP(alpha!)}（你已跑输躺平 ${Math.abs(alpha!).toFixed(2)}pct）`)
    lines.push(`  最大回撤：你 ${fmtPct(s.max_drawdown_pct)} ｜ 躺平 ${fmtPct(m!.max_drawdown_pct)} ｜ 差 ${fmtSignedPP(ddGap)}（负=你回撤更深）`)
    lines.push('  → 长期低仓位 + 大幅跑输 = 踏空：空仓时回撤天然小，"回撤比躺平浅"不能拿来自证有效，代价就是上面那行被你让掉的超额。')
    lines.push('')
    lines.push(`▍ 第三步 · ⛔ 升级条款已触发（累计跑输躺平 ≥${Math.abs(ESCALATE_ALPHA_PP)}pct 且当前仓位 ${fmtNum((posWeight ?? 0) * 100, 1)}% < ${ESCALATE_POS_WEIGHT * 100}%）——选项②今天不可用：`)
    lines.push('  长期不出手本身就是被证伪的 thesis："等条件满足再进场"的条件被市场反复路过而你从未进场，说明触发器定义有结构性问题（典型：分位/温度类指标"涨=贵=过热=偏空"，趋势市里永远投反对票，凑不齐同号确认）。')
    lines.push(`  今天必须调 \`mcp__strategy_mcp__update_my_strategy(bot_id="${botId}", strategy, reason)\` 完整重写 METHODOLOGY.md。新版必须回答三件事：① 哪个维度长期投反对票导致永不建仓，怎么改；② 中性档对应多少基准仓位（0% 不是中性，是满仓押注下跌）；③ 什么客观硬信号下允许右侧追入。若论证后仍认为该空仓，就把"为何此环境 0% 最优 + 何时必须重新进场的客观触发器"写进新版——空仓可以是结论，不能是惯性。`)
  } else {
    // 在场/非踏空的日常复盘：只看「滚动近一段」自评，不亮"自 Day 1 累计 vs 躺平"的全程记分牌。
    lines.push('▍ 第二步 · 看你自己近一段的滚动表现（上方【区间业绩】块已注入近 1m / 3m / 6m 的区间收益与回撤）——按区间视角判断你的择时这一段在不在创造价值。**刻意不在这里摆"自 Day 1 累计跑赢没跑赢躺平"的全程记分**：一整条已知净值最容易诱发"跑输就把趋势/动量权重拉满"的过拟合，那不是复盘是追涨。')
    lines.push('  → 阶段性跑输躺平、或某一段回撤，单独都不是方法论失效的证据：你的标的本就大开大合，跑输买入持有的某一段很正常，强行解释成"方法论错了"再重写，多半是噪声。')
    lines.push('  → 真正的失效信号只有三类：① 趋势 regime 已切换而你的框架没跟上；② 某条具体 thesis 被硬数据证伪；③ 你长期空仓踏空（这条会单独触发上面的升级条款）。对不上这三类，就是还成立。')
    lines.push('')
    lines.push('▍ 第三步 · 今天必须做完这次审视（不允许沉默跳过——既不审也不记 = 违约），但结论可以是"维持"：')
    lines.push(`  ① 命中上面三类失效之一 → 调 \`mcp__strategy_mcp__update_my_strategy(bot_id="${botId}", strategy, reason)\` 完整重写 METHODOLOGY.md（整篇新版本）。reason 写清：哪条 thesis / regime 判断破了、被什么硬数据证伪。`)
    lines.push('  ② 没命中失效信号 → mem0_add 写一句"复盘结论：方法论仍有效"，并点明当前处在你 thesis 的哪一段、下一个会让你改主意的客观信号是什么。**不必为了"做点什么"而改——无具体证伪就维持原方法论，频繁重写本身就是过拟合噪声。**')
  }
  lines.push('判据别只盯一天涨跌：结合上方【信念校准】块的 Brier / 活性 + 第一步的趋势定性一起判。该改就改、该守就守，别用"不轻易改"麻痹自己，也别用"必须做点什么"逼自己乱改。')
  return `\n\n${lines.join('\n')}`
}

// ── belief ↔ 仓位 言行一致硬约束 ──────────────────────────────────────────────────
// 病根（bot6 军工 / bot10 黄金 长期 0% 踏空）：bot 每天都写 belief（t+20 上涨概率），却让仓位与它
// 完全脱钩——可以"嘴上 p_up=0.60 看多、仓位 0% 空仓"两头都占。所有文字劝导都被 bot 自我辩解掉，因为
// 仓位决定权 100% 还在它手里，而"做错的痛具体、踏空的痛弥散"这条不对称会把它的自封闸门越拧越紧。
// 这块把仓位焊回 bot 自己说出口的信念：方向矛盾就每天硬拦，逼它当天要么改信念要么改仓位——断掉脱钩，
// 棘轮就转不动。对称两侧都拦：看多却空仓 = 踏空；看空却重仓 = 做错。不限复盘日，每天核对。
// 触发需同时有 ① 可解析的 standing belief（含 t+20 p_up）② 今日账户快照（算当前仓位权重）；
// 缺任一 → 无法判定 → 返回空串（不拦，行为不变）。信念被 Brier 校准 → bot 无法靠"嘴硬写低 p_up"给
// 空仓开脱（看空看错同样扣分），所以这条不是又一道能被辩解的文字，而是把"言"钉死在可计分的概率上。
const COHERENCE_BULL_P = 0.55   // t+20 p_up ≥ 此 = 净看多
const COHERENCE_BEAR_P = 0.45   // t+20 p_up ≤ 此 = 净看空
const COHERENCE_FLAT_POS = 0.20 // 仓位 < 20% ≈ 空仓（0/40 框架下基本就是 0）
const COHERENCE_HEAVY_POS = 0.40 // 仓位 ≥ 40% = 有承重底仓

function beliefPositionCoherenceBlock(
  dc: DailyContextData | undefined,
  latestBelief: DailyMessageContext['latestBelief'],
): string {
  if (!latestBelief) return ''
  const p20 = latestBelief.tPlus20
  if (typeof p20 !== 'number' || !Number.isFinite(p20)) return ''
  // 当前仓位权重（今日 settle 后账户快照）。account 缺失 → 无法判定，不拦。
  const acct = dc?.account?.account
  if (!acct || !(acct.total_value > 0)) return ''
  const posWeight = acct.market_value / acct.total_value

  const bullishFlat = p20 >= COHERENCE_BULL_P && posWeight < COHERENCE_FLAT_POS
  const bearishHeavy = p20 <= COHERENCE_BEAR_P && posWeight >= COHERENCE_HEAVY_POS
  if (!bullishFlat && !bearishHeavy) return ''

  const p5 = latestBelief.tPlus5
  const p5txt = typeof p5 === 'number' && Number.isFinite(p5) ? p5.toFixed(2) : 'n/a'
  const posPct = fmtNum(posWeight * 100, 1)
  const lines: string[] = []
  lines.push('【⚖ 言行一致核对（belief ↔ 仓位）】')
  if (bullishFlat) {
    lines.push(`你上一条 standing belief：t+20 上涨概率 p_up=${p20.toFixed(2)}（>${COHERENCE_BULL_P} = 净看多），t+5=${p5txt}；但你当前仓位 ${posPct}%（基本空仓）。`)
  } else {
    lines.push(`你上一条 standing belief：t+20 上涨概率 p_up=${p20.toFixed(2)}（<${COHERENCE_BEAR_P} = 净看空），t+5=${p5txt}；但你当前仓位 ${posPct}%（重仓）。`)
  }
  // 只报「方向不一致」这个事实，不给具体调仓处方：仓位纪律（目标权重表 / B4 权益带 /
  // 四个 override 口子）在 METHODOLOGY 里已经定死，提示词再开一条"建到 ≥40%／该清就清"
  // 会绕过那套纪律、且与 B4 下限的数值直接打架。这里只要求它把矛盾说清楚。
  lines.push('这两者方向不一致。今天的决策里请显式交代一句：是 **belief 该更新**（写明什么变了，并在 evidence 里给出依据），还是 **仓位该调整**。')
  lines.push('**仓位怎么动、动多少，一律按 METHODOLOGY 的目标权重表 / B4 权益带 / 四个 override 口子判断**——本提示不替你定方向、不给仓位数字，只负责让"想的"和"做的"不要不声不响地打架。')
  lines.push('（提醒：把 p_up 往空仓方向写低并不能给踏空开脱——看空看错了 t+20 一样扣 Brier 分，假装看空会在校准里露馅。）')
  return `\n\n${lines.join('\n')}`
}

// 把"判断管线 skill"整篇拼成一个 daily-message 区块。直接注入 = bot 无需 load_skill 即可读到，
// 内容就是各 skill 的 SKILL.md 全文（自算版：自己调 sector_*/regime 等工具做判断，不依赖外部研报）。
function injectedSkillsBlock(skills?: { id: string; content: string }[]): string {
  if (!skills || !skills.length) return ''
  const order = skills.map(s => s.id).join(' → ')
  const bodies = skills
    .map(s => `────────── skill: ${s.id} ──────────\n${s.content.trim()}`)
    .join('\n\n')
  return `\n\n【判断管线 skill（已直接注入，今天必须照这些 skill 的算法与工具表实操，无需 load_skill）】
下面 ${skills.length} 个 skill 是你的判断流程，按顺序照做：${order}。**主线/regime 判断要真的调用 skill 里写的工具（如 sector_search / sector_factor / market_temperature 等）做出来，不是只看预注入数据块拍脑袋。**

${bodies}
【判断管线 skill 结束】`
}

// 主仓位阶梯核对块（系统核算，确定性）：按 METHODOLOGY「主仓位（阶梯退坡）」把当前阶梯档位
// 算出来注入。动机与载体核对块相同——阶梯规则写在方法论里 bot 从不自己执行（知道建卫星、
// 不知道从主仓位换仓）。主仓位 = regime 上一轮配置的主载体（当前多为宽基），判定口径 =
// **单只持仓 >40% 默认为主仓位**（当前在管主线板块的指定载体除外——那是主线仓，归载体核对管）。
// 新主线成熟时由主仓位退坡让位给主线载体，这才是真正的轮动。系统解析 mainline 报告的
// regime / 集中度 / in_top5 计数判档，超上限就直接给出退坡指令。解析失败 / 非抱主线 regime /
// 无 >40% 主仓位 → 空串。
const MAIN_POSITION_MIN_W = 0.40 // 单只 >40% 默认视为主仓位
function mainPositionLadderBlock(
  mainline: string,
  holdings?: { fund_code: string; fund_name?: string | null; weight: number }[],
): string {
  if (!mainline || !holdings) return ''
  // 只在『抱主线』regime 下退坡；无主线·宽基 / 防御 regime 下主仓位本来就该重，不注入。
  if (!/regime：?\*{0,2}抱主线/.test(mainline)) return ''
  const concM = /top15 集中度\s*(\d+)\s*个/.exec(mainline)
  const concentration = concM ? Number(concM[1]) : 0
  // ③ 组合状态机表：| 板块 | 角色 | rank | 站MA60 | 已持 | 最小持有余 | 连续in_top5 | ...
  const smIdx = mainline.indexOf('组合状态机')
  if (smIdx < 0) return ''
  const smSection = mainline.slice(smIdx, mainline.indexOf('##', smIdx + 10) > 0 ? mainline.indexOf('##', smIdx + 10) : undefined)
  let hasCore = false
  let maxInTop5 = 0
  let boardCount = 0
  for (const line of smSection.split('\n')) {
    const cells = line.trim().split('|').map(s => s.trim()).filter((_, i, a) => !(i === 0 && a[0] === '') )
    if (cells.length < 8 || !/^BK/.test(cells[0])) continue
    boardCount++
    if (cells[1].includes('核心')) hasCore = true
    const t5 = Number(cells[6])
    if (Number.isFinite(t5)) maxInTop5 = Math.max(maxInTop5, t5)
  }
  if (boardCount === 0) return '' // 状态机无在管板块 → 主仓位不退坡
  let tier: string, lo: number, hi: number
  if (hasCore) { tier = '晋升核心已落地'; lo = 0; hi = 0.25 }
  else if (maxInTop5 >= 10 || concentration >= 6) { tier = `主线强化（在管板块最大连续in_top5=${maxInTop5}${maxInTop5 >= 10 ? '≥10' : ''}${concentration >= 6 ? `，集中度${concentration}≥6` : ''}）`; lo = 0.30; hi = 0.40 }
  else { tier = `主线初现（最大连续in_top5=${maxInTop5}<10，集中度${concentration}<6）`; lo = 0.50; hi = 0.60 }
  // 主仓位识别：单只 >40%，且不是当前『⑤ 可投基金池』指定载体（主线仓归载体核对块管）。
  const poolIdx = mainline.indexOf('可投基金池')
  const vehicleCodes = new Set<string>()
  if (poolIdx >= 0) {
    for (const line of mainline.slice(poolIdx).split('\n')) {
      const m = /^\|\s*BK[0-9A-Za-z.]+\s+[^|]+\|\s*[^|]+\|\s*(\d{6})\s/.exec(line.trim())
      if (m) vehicleCodes.add(m[1])
    }
  }
  const mains = holdings.filter(h => h.weight > MAIN_POSITION_MIN_W && !vehicleCodes.has(h.fund_code))
  if (!mains.length) return '' // 无 >40% 主仓位 → 无退坡对象
  const mainW = mains.reduce((s, h) => s + h.weight, 0)
  const detail = mains.map(h => `${h.fund_code}（${h.fund_name ?? ''}）${fmtNum(h.weight * 100, 1)}%`).join('、')
  const over = mainW > hi + 1e-9
  const verdict = over
    ? `**超出上限 ${fmtNum((mainW - hi) * 100, 1)}pct → 应退坡**：本次卖出主仓位 10%-15%（豁免"最小步长20%"），只动**赎回费 ≤0.5% 的份额**（不必等 0 费档——0.5% 是可接受的退坡摩擦）；只有命中费率 **>0.5%** 时（典型是 <7 日的 1.5% 超短线档）才允许顺延到费率降到 ≤0.5% 的最近日期，并在 mem0 写明顺延决定。释放资金等额置换到未建仓 / 未达标准档的主线载体（见上方载体核对表 ✗/△ 行），属等权益结构替换，**不受 sentiment 加仓闸门约束**——主线成熟、主仓位让位，这就是轮动。需要腾槽 / 补资金时也**可以直接清掉你最不看好的卫星**（不必等状态机剔除），只受 ≤0.5% 赎回费窗口与最小持有期约束。`
    : `在档内（≤${fmtNum(hi * 100, 0)}%），无需退坡。`
  return `\n\n────────── 主仓位阶梯核对（系统核算 · METHODOLOGY「主仓位（阶梯退坡）」） ──────────
- 主仓位判定（单只 >40% 且非主线载体）：${detail}，合计 **${fmtNum(mainW * 100, 1)}%**
- 当前主线阶段：**${tier}** → 主仓位目标上限 **${fmtNum(lo * 100, 0)}%-${fmtNum(hi * 100, 0)}%**
- 判定：${verdict}`
}

// ── 交易纪律核对（系统核算，确定性）──────────────────────────────────────────
// 平衡「卫星快进快出的引擎节奏」与「无谓交易摩擦」：不回收裁量清仓权（bot 保留探索
// alpha 的自由），但让清仓变成有后果的真决策——
//   · 载体再进冷却：任何基金被清仓（卖到 0）后 10 个交易日内不得买回同一基金，
//     堵「裁量清仓 → 次日载体核对逼回购」的翻烙饼回路；冷却中的指定载体在载体
//     核对表里渲染 ⏳（不亮 ✗ 施压买回，否则系统自己打架）。
//   · 防拆单：同一基金同方向 5 个交易日内只应有一单（引擎明令动作豁免）；
//     推论：避险/止损卖出必须一次到位，不许分批碎步卖。
// 全部从账户快照 recentOrders + benchmark 交易日历确定性推算，无未来函数；
// 数据缺失（老快照 / 无 benchmark 日历）→ 空串，行为完全不变。
const VEHICLE_REENTRY_COOLDOWN_TD = 10 // 清仓后再进冷却（交易日，含当日口径见 left 计算）
const SLICE_WINDOW_TD = 5              // 同向拆单窗口（交易日）

interface VehicleCooldown { soldOn: string; left: number }

// explicit（run.ts 传入的 runtime 交易日历）优先，benchmark 序列只是回退。
//
// 原先只有 benchmark 一条路，而 benchmark 依赖 runStartDate 且整池 NAV 拉取有 60s 超时——
// bot105d 的买池 1515 只 × 380 交易日实测 75.6s，超时后 catch 成 null（无日志），于是整块
// 交易纪律核对在它 381 天里一次都没渲染过。交易日历跟基准数据本来毫无关系，不该借它的道。
function tradingCalendarOf(dc: DailyContextData | undefined, asOfDate: string, explicit?: string[]): string[] | null {
  let days: string[]
  if (explicit?.length) {
    days = explicit.slice() // 不能就地 push：会污染调用方持有的 setupRes.calendarDates
  } else {
    const pts = dc?.benchmark?.pointsByDate
    if (!pts) return null
    days = Object.keys(pts).sort()
  }
  if (!days.length) return null
  // 今日是决策交易日，但序列可能只到昨日——补上，保证 (from, to] 计数含今日。
  if (days[days.length - 1] < asOfDate) days.push(asOfDate)
  return days
}

// (from, to] 之间的交易日数（from 不必在日历内，按字典序比较 ISO 日期）。
function tdBetween(cal: string[], from: string, to: string): number {
  let n = 0
  for (const d of cal) if (d > from && d <= to) n++
  return n
}

function computeVehicleCooldowns(dc: DailyContextData | undefined, asOfDate: string, tradingCalendar?: string[]): Map<string, VehicleCooldown> {
  const out = new Map<string, VehicleCooldown>()
  const orders = dc?.account?.recentOrders
  const cal = tradingCalendarOf(dc, asOfDate, tradingCalendar)
  if (!orders?.length || !cal) return out
  const held = new Set((dc?.account?.holdings ?? []).map(h => h.fund_code))
  for (const o of orders) {
    if (o.order_type !== 'sell' || o.status !== 'confirmed') continue
    if (held.has(o.fund_code)) continue // 仍在持仓 = 未清仓，不进冷却
    const prev = out.get(o.fund_code)
    if (prev && prev.soldOn >= o.order_date) continue
    // 清仓日后第 1..10 个交易日均封禁，第 11 个交易日解禁（与 min_hold 同口径，防 off-by-one）。
    const elapsed = tdBetween(cal, o.order_date, asOfDate)
    const left = Math.min(VEHICLE_REENTRY_COOLDOWN_TD, VEHICLE_REENTRY_COOLDOWN_TD + 1 - elapsed)
    if (left > 0) out.set(o.fund_code, { soldOn: o.order_date, left })
    else out.delete(o.fund_code)
  }
  return out
}

function tradeDisciplineBlock(
  dc: DailyContextData | undefined,
  asOfDate: string,
  cooldowns: Map<string, VehicleCooldown>,
  tradingCalendar?: string[],
): string {
  const orders = dc?.account?.recentOrders
  const cal = tradingCalendarOf(dc, asOfDate, tradingCalendar)
  if (!orders?.length || !cal) return ''
  const lines: string[] = []
  lines.push('────────── 交易纪律核对（系统核算 · 载体冷却 + 防拆单） ──────────')
  // ① 载体再进冷却
  if (cooldowns.size) {
    lines.push(`- 载体再进冷却（清仓后 ${VEHICLE_REENTRY_COOLDOWN_TD} 个交易日内不得买回同一基金）：`)
    for (const [code, c] of [...cooldowns.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      lines.push(`  - ${code}：你于 ${c.soldOn} 清仓卖出，**余 ${c.left} 个交易日**内不得买回该基金。`)
    }
    lines.push('  冷却是裁量清仓的对价：清了就要空仓扛满冷却期——看对是你的 alpha，看错自己吃踏空；不许今天清、过两天又买回来翻烙饼。')
  } else {
    lines.push(`- 载体再进冷却：当前无冷却中基金（任何基金清仓后 ${VEHICLE_REENTRY_COOLDOWN_TD} 个交易日内不得买回同一基金）。`)
  }
  // ② 防拆单：近 5 交易日内已有订单的 基金×方向
  const seen = new Map<string, string>() // `${code}|${dir}` → 最近 order_date
  for (const o of orders) {
    if (o.order_type !== 'buy' && o.order_type !== 'sell') continue
    if (tdBetween(cal, o.order_date, asOfDate) >= SLICE_WINDOW_TD) continue
    const k = `${o.fund_code}|${o.order_type}`
    const prev = seen.get(k)
    if (!prev || prev < o.order_date) seen.set(k, o.order_date)
  }
  lines.push(`- 防拆单（同一基金同方向 ${SLICE_WINDOW_TD} 个交易日内只应有一单）：`)
  if (seen.size) {
    for (const [k, d] of [...seen.entries()].sort()) {
      const [code, dir] = k.split('|')
      lines.push(`  - ${code} 近 ${SLICE_WINDOW_TD} 个交易日已有${dir === 'buy' ? '买入' : '卖出'}单（${d}）——今天再下同向单 = 拆单违规。`)
    }
  } else {
    lines.push(`  - 近 ${SLICE_WINDOW_TD} 个交易日无订单，今天各基金同方向均可下 1 单。`)
  }
  lines.push('  豁免：状态机剔除 / 破 MA60 硬止损 / 主仓位退坡阶梯指令等**引擎明令动作**不受此限。')
  lines.push(`  ⚠️ 推论：因为同向 ${SLICE_WINDOW_TD} 日一单，**避险 / 止损卖出必须一次到位**——第一笔就直接卖到目标仓位，不要分 2-3 笔逐步卖（分批的后几笔会撞纪律）。`)
  return `\n\n${lines.join('\n')}`
}

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

function holdCommitmentBlock(dc: DailyContextData | undefined, botId: string, asOfDate: string, deepResearchEligible: boolean): string {
  if (botKindOf(botId) !== 'single-fund') return ''
  const fees = dc?.fundFees
  if (!fees?.length) return ''
  // 硬锁承诺行：对每只可买基金列锁定天数 = max(7, 赎回费窗口)。零赎费基金也列（仍锁 7 天）。
  const commit: string[] = []
  for (const f of fees) {
    const pen = redeemPenaltyOf(f)
    const windowDays = pen?.windowDays ?? 0
    const lockDays = Math.max(7, windowDays)
    const feeNote = pen ? `不足 ${windowDays} 天确定亏 ${fmtNum(pen.rateForDays(0), 2)}% 早赎费` : '无赎回费'
    commit.push(`  - ${f.fund_code}${f.fund_name ? `（${f.fund_name}）` : ''}：今日建仓/加仓 → 锁 ${lockDays} 自然日（${feeNote}）。`)
  }
  if (!commit.length) return ''
  const lines: string[] = []
  lines.push('────────── 建仓 = 最短持有承诺（系统硬闸 · 跨日生效） ──────────')
  lines.push('【买之前想清楚能不能拿住】今日买入/加仓即被锁定；锁定天数 = max(7, 赎回费窗口)。窗内普通交易日想卖，交易代理会直接拒单（blocked_by:deep_research_commitment）；只有系统硬风控（急跌/账户回撤越线）能提前放行，或等承诺到期。拿不住就别在今天建：')
  lines.push(...commit)
  if (deepResearchEligible) lines.push('  （今日若经 start_research 深研后建仓，该仓位属深研承诺：可在后续深研日重研明确证伪后平仓；普通日建仓不享此通道。）')
  // 牙齿②窗内持仓「今日卖出确切早赎费 + 思考闸」（赎回费维度，与硬锁并存）。
  const gate = inWindowGateLines(dc, asOfDate, fees)
  if (gate.length) lines.push(...gate)
  return `\n\n${lines.join('\n')}`
}

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

// 载体核对块（系统核算，确定性）：把 market_mainline『⑤ 可投基金池』的指定载体逐一对照
// 当日真实持仓，明确"该板块是否已按标准档建仓"。动机：bot 会把存量同主题旧持仓 / 低重叠代理
// 认领成板块载体从而跳过『新进[卫星]』建仓（每次换一个说法），方法论文本堵不住；这里由系统
// 每天把核对结果顶在报告里。纯字符串解析 + 持仓快照对照，无未来函数。解析不到基金池表 → 空串。
const VEHICLE_STANDARD_TIER_MIN = 0.08 // 标准档下限（METHODOLOGY 参考档 8%-15%）
function vehicleCheckBlock(
  mainline: string,
  holdings?: { fund_code: string; weight: number }[],
  cooldowns?: Map<string, VehicleCooldown>,
): string {
  if (!mainline || !holdings) return ''
  // 只解析『可投基金池』小节内的表格行，避免误吃其它表。
  const poolIdx = mainline.indexOf('可投基金池')
  if (poolIdx < 0) return ''
  const section = mainline.slice(poolIdx)
  // 行格式：| BK1106.DC 创新药 | 卫星 | 012738 广发创新药ETF联接C | ✓纯载体 | ...
  const rowRe = /^\|\s*(BK[0-9A-Za-z.]+)\s+([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(\d{6})\s*([^|]*?)\s*\|\s*([^|]+?)\s*\|/
  const weightOf = new Map(holdings.map(h => [h.fund_code, h.weight]))
  const rows: string[] = []
  let anyMissing = false
  for (const line of section.split('\n')) {
    const m = rowRe.exec(line.trim())
    if (!m) {
      // 表格结束（碰到非表行且已收集到数据）就停，防止解析到下一节。
      if (rows.length && !line.trim().startsWith('|')) break
      continue
    }
    const [, board, boardName, , fundCode, fundName, grade] = m
    if (/^-+$/.test(board)) continue
    const w = weightOf.get(fundCode) ?? 0
    let status: string
    if (w >= VEHICLE_STANDARD_TIER_MIN) status = '✓ 已建仓'
    else if (w > 0) status = `△ 仅 ${fmtNum(w * 100, 1)}%（低于标准档 ${VEHICLE_STANDARD_TIER_MIN * 100}%，不计为已建仓）`
    else {
      const cd = cooldowns?.get(fundCode)
      if (cd) {
        // 冷却中不亮 ✗ 施压买回——你自己清的仓，冷却期满信号还在才恢复建仓要求。
        status = `⏳ 冷却中（${cd.soldOn} 清仓，余 ${cd.left} 交易日不得买回）`
      } else { status = '✗ 未建仓'; anyMissing = true }
    }
    rows.push(`| ${board} ${boardName} | ${fundCode} ${fundName.trim()}（${grade.trim()}） | ${fmtNum(w * 100, 1)}% | ${status} |`)
  }
  if (!rows.length) return ''
  const note = anyMissing
    ? '\n> ⚠️ 上表有 ✗/△ 板块：该板块**尚未持有**——判断"某板块是否已持有"只认上表指定载体且仓位 ≥8%；**其它基金（同主题旧持仓、低重叠代理、装饰位）一律不计**，"已有类似持仓 / 成分重叠 / 切换摩擦"不构成跳过『新进[卫星]』建仓的理由（详见 METHODOLOGY 的拒绝理由封闭清单与「主仓位（阶梯退坡）」资金来源）。'
    : '\n> 口径：板块"已持有"只认指定载体且仓位 ≥8%；其它基金不计。'
  // 只数硬闸（绝对上限 6 只）：新建载体前先看槽位；满员就必须同日先清一只最不看好的卫星腾槽。
  const HOLDINGS_HARD_CAP = 6
  const n = holdings.length
  const capLine = n >= HOLDINGS_HARD_CAP
    ? `\n- 持仓数硬闸：**当前 ${n} 只 / 硬上限 ${HOLDINGS_HARD_CAP}（已满员${n > HOLDINGS_HARD_CAP ? '，超限违规，当日必须整合' : ''}）**——任何新建仓必须**同一决策日先清一只腾槽**（先清 <8% 残仓中 conviction 最低者；没有残仓就直接清你最不看好的标准档卫星）；硬上限绝对，不论什么理由都不得超过。`
    : `\n- 持仓数硬闸：当前 ${n} 只 / 硬上限 ${HOLDINGS_HARD_CAP}（剩 ${HOLDINGS_HARD_CAP - n} 个槽位）。`
  return `\n\n────────── 载体核对（系统核算 · 仅 fund_pool 指定载体计为板块持仓） ──────────
| 在管板块 | 指定载体 | 当前仓位 | 状态 |
|---|---|---|---|
${rows.join('\n')}${note}${capLine}`
}

// 系统预读注入三份市场研报（PIT）。bot101/102/103 用：主线/regime/组合骨架已由系统预生成，
// bot 直接消费报告结论做仓位与下单决策，不自己跑主线识别。全缺 → 空串（跳过整块）。
function marketReportsBlock(reports?: {
  context: string; mainline: string; rotation: string; macroNews?: string
  res?: { market_strategy: string; policy_analysis: string; intl_relations: string; cross_market_linkage: string }
}, holdings?: { fund_code: string; fund_name?: string | null; weight: number }[],
   dc?: DailyContextData, asOfDate?: string, tradingCalendar?: string[]): string {
  if (!reports) return ''
  const cooldowns = computeVehicleCooldowns(dc, asOfDate ?? '', tradingCalendar)
  const part = (label: string, body: string): string =>
    `────────── ${label} ──────────\n${body && body.trim() ? body.trim() : '（截至今日暂无该报告——按 METHODOLOGY 保守处理）'}`
  const hasMacro = !!(reports.macroNews && reports.macroNews.trim())
  // rotation 块保留，但 run.ts 已把它的来源从残缺的 mainline_rotation_daily 改成完整的月度
  // mainline_rotation（详见 run.ts readMarketReportsForInjection 的 2026-08-04 注释）。
  // 2025 回测里这块曾连续 227 个交易日注入 2025-01-22 的同一份快照，与同一 prompt 里新鲜的
  // market_mainline 直接矛盾；下面三个子块的入参一直是 reports.mainline，不受影响。
  const bodies = [
    part('market_context（行情 / regime / risk_state）', reports.context),
    part('market_mainline（主线板块 + 组合状态机 + 可投基金池）', reports.mainline),
    part('mainline_rotation（核心/卫星组合骨架 + 今日动作）', reports.rotation)
      + vehicleCheckBlock(reports.mainline, holdings, cooldowns)
      + mainPositionLadderBlock(reports.mainline, holdings)
      + tradeDisciplineBlock(dc, asOfDate ?? '', cooldowns, tradingCalendar),
    ...(hasMacro ? [part('macro_news（宏观 / 政策 / 事件资讯 · 当期）', reports.macroNews as string)] : []),
  ].join('\n\n')
  const n = hasMacro ? '四' : '三'
  // 四研判室·宏观背景研判（res1/2/4/5）：与上面 4 份同源 PIT 预注入，但定位＝「背景研判」，
  // 只丰富 regime/风险预算判断，不覆盖主线与组合骨架。全空 → 跳过该子段（旧行为零回归）。
  const r = reports.res
  const resPairs: Array<[string, string]> = r ? [
    ['res1 · 市场策略研判', r.market_strategy],
    ['res2 · 政策分析', r.policy_analysis],
    ['res4 · 国际关系', r.intl_relations],
    ['res5 · 跨市场联动', r.cross_market_linkage],
  ] : []
  const resShown = resPairs.filter(([, body]) => !!(body && body.trim()))
  const resSection = resShown.length
    ? `\n\n══════════ 四大研判室 · 宏观背景研判（res1/2/4/5）══════════
这四份是公共研究室的当期宏观研判，定位＝**背景研判**：用来校准 regime 信心与风险预算（尤其 res2 政策面 / res4 地缘 / res5 跨市场联动的冲击信号，与 macro_news 互补，帮你分清「一次性外部冲击 vs 可持续基本面恶化」）。
**操作性结论（regime / 主线 / 组合骨架）仍以上面 market_context / market_mainline / mainline_rotation 为准——res 研判不覆盖组合骨架**；但若 res2/res4 标出重大政策面 / 地缘风险，把它喂给你的风险预算与极端恐慌逆向闸门判断。

${resShown.map(([label, body]) => part(label, body)).join('\n\n')}`
    : ''
  return `\n\n【市场研究报告（系统预生成 · PIT · 全市场共享）】
下面${n}份报告是系统预生成的当期市场判断与资讯，**是你今天 regime / 主线 / 组合骨架的权威结论，直接采用**：
- **不要**自己再调 \`sector_search\`/\`sector_factor\`/\`market_temperature\` 去重跑主线识别或 regime 判断——那套流程系统已替你做完；
- market_mainline 给出主线板块、组合状态机与当日 top5 候选的触发距离；mainline_rotation 给出核心/卫星组合骨架与每板块双测度选好的基金（fund_pool），照它执行即可；
- **macro_news 是当期宏观 / 政策 / 事件资讯**：决策前必读，判断有没有重大政策面 / 事件面催化或冲击。**尤其遇到大跌：用它分清「一次性外部冲击（如关税 / 地缘黑天鹅，不可外推）」还是「可持续的基本面恶化」——一次性冲击扛住别恐慌转防守、更别把它写进长期记忆当永久教训；只有可持续恶化才真正降风险预算**；
- 你的职责 = 基于这${n}份报告 + 你的 METHODOLOGY（仓位/风险闸门/配置区间/回撤纪律）做**目标仓位与下单**决策。

${bodies}${resSection}
【市场研究报告 结束】`
}

// 深度研究「强制块」：run.ts 判定 forced=true 时注入（ordinal 模式的 N 倍决策日；agent-triggered
// 模式距上次深研 ≥ maxGapDays 交易日）。放在 message 尾部（recency 高），是 bot 当天必须调
// start_research 的硬指令。
function deepResearchBlock(forced: boolean | undefined, gapDays?: number, lastDate?: string, reasons: string[] = []): string {
  if (!forced) return ''
  const gapFinite = typeof gapDays === 'number' && gapDays < Number.MAX_SAFE_INTEGER
  const gapNote = lastDate
    ? (gapFinite ? `距上次深研（${lastDate}）已 ${gapDays} 交易日，达调度硬上限——` : `距上次深研（${lastDate}）已超硬上限——`)
    : (gapFinite ? `本 run 已进入第 ${gapDays + 1} 个交易日，达到首次深研调度上限——` : '本 run 尚未做过深研，已达调度硬上限——')
  const events = [
    reasons.includes('target-move') ? '投资目标上一交易日出现大幅涨跌' : '',
    reasons.includes('account-drawdown') ? '账户当前净值回撤跨入更深的阈值倍数档位' : '',
    reasons.includes('portfolio-loss') ? '组合上一可得交易日出现异常单日损失' : '',
    reasons.includes('holding-drop') ? '最差持仓/板块载体出现异常单日下跌' : '',
    reasons.includes('risk-basket') ? '多指数风险篮子出现异常单日下跌' : '',
  ].filter(Boolean)
  const triggerNote = events.length > 0 ? `触发原因：${events.join('；')}。${reasons.includes('max-gap') || reasons.includes('ordinal') ? gapNote : ''}` : gapNote
  return `

【深度研究日 · 强制】${triggerNote}今天**必须调用 \`start_research\` 一次、且只一次完成深度研究，这不是可选项**（单日上限 1 次，第 2 次调用会被系统拒绝）。**研究要在任何下单之前完成**——先研究、后决策下单，让结论直接进今天的仓位动作。跳过 = 违反调度纪律（mem0_add 记 \`[调度违规, forced-day-skip, ${lastDate ?? 'run起点'}→今日]\`）：
- **只研究一个命题**：从近期决策里挑最有价值的一个（主线持续性 / 某指数的趋势与资金结构 / 方法论某条规则是否有效），一次讲透，不摊开多个泛泛话题。
- **数据边界与日常一致**：研究内仍然只有 simworld（PIT）与组合工具，不假设任何实时外部数据。
- **结论必须落地**：研究结束后把「结论 → 对后续操作的具体影响」写进当日复盘，并 mem0_add 落库，供后续决策日直接引用。
- **研究不得挤掉当日决策**：先做研究、让结论直接服务今天的仓位决策；若研究耗时逼近预算，先回来完成今天的决策与下单再收尾——漏掉调仓比研究写得不完美严重得多。`
}

// 深度研究「触发提示块」：agent-triggered 模式下 authorized-not-forced 日注入（forced 日走强制块，
// 不叠加）。定位＝**软规则 + bot 自主判断**——列出经验触发条件供 bot 参考，不强凑；bot 综合判断
// 值得深研就自主发起，无异常就正常复盘。
function deepResearchTriggerBlock(ctx: {
  authorized: boolean | undefined
  forced: boolean | undefined
  mode: 'ordinal' | 'agent-triggered' | undefined
  gapDays?: number
  maxGap?: number
  lastDate?: string
}): string {
  if (!ctx.authorized || ctx.forced) return ''
  if (ctx.mode !== 'agent-triggered') return ''
  const gap = typeof ctx.gapDays === 'number' && ctx.gapDays < Number.MAX_SAFE_INTEGER ? ctx.gapDays : null
  const max = ctx.maxGap ?? 5
  const remaining = gap === null ? null : Math.max(0, max - gap)
  // forced 日不走本块，故 gap === null（日历异常）在此不可达；留兜底文案防御。
  const who = ctx.lastDate
    ? `距上次深研 ${gap ?? '?'} 交易日（上次=${ctx.lastDate}）`
    : `本 run 尚未做过深研（起点至今 ${gap ?? '?'} 交易日）`
  const gapNote = `${who}，硬上限 ${max}，还剩 ${remaining ?? '?'} 天缓冲`
  return `

【深研触发提示 · 授权可选】${gapNote}。今天**允许**调用 \`start_research\`，但**是否触发由你自主判断**——不强凑（"为触发而触发"是浪费）。

**顺序铁律：先判断是否深研 → 若深研，先研究后下单。** 读完当日数据后第一件事就是过一遍下方触发条件，决定今天研不研究；一旦决定深研，必须在**任何下单之前**完成研究，让结论直接进当天的仓位决策。先下单再研究 = 结论没机会影响今天的动作，研究白做、预算白烧。

**建议触发条件（软规则，任一命中即建议深研，多条共振更强）**：
1. 组合**单日损失 ≥ 2%**，或组合从 **20 日高点回撤 ≥ 5%**
2. 任一持仓/板块载体**单日跌幅 ≥ 4%**或 **5 日跌幅 ≥ 8%**
3. 多指数风险篮子（沪深300、创业板、科创50、中证1000）任一**单日跌幅 ≥ 2%**，或出现 ≥2σ 异动
4. 上下行波动同时放大、相关性骤升，或量价/波动结构明显变档
5. 状态机 top5 **变动 ≥ 2 席**（主线切换嫌疑）
6. 上次深研 **p_up ≤ 0.35 或 ≥ 0.65** 的 T+1~T+3 跟进（强信号复核）
7. 你综合判断的其他值得深研的情形（例如政策/消息面突变、方法论某条规则连错 2 次需要复盘）

**判定要点**：
- **数据你已经有**——上方 dailyContext / 市场研报块已经给了组合日收益、组合回撤、持仓 NAV 与多指数风险篮子，自查即可，不必再重复拉。
- **无异常就不发**——常规日按正常节奏做 settle 复核 + 状态机审阅 + 必要下单即可，不必为凑深研强上一课；系统会在 gap 达 ${max} 交易日时切换到"强制"档兜底。
- **强度选择**：真触发就按【深度研究日】的四点纪律执行（只研究一个命题、数据边界、结论落地、不挤掉决策）。**单日硬上限=1 次**——第 2 次 \`start_research\` 会被系统拒绝，值得研究的第二个命题请留到下个交易日。`
}

// 「当日研究室简报」块：单指数 run 用。内容由 run.ts 的 assembleBriefing() 从 fund.db PIT 拼好传进来
// （res 四研判室 + macro_news + market_context）。定位＝参考信号，不覆盖 METHODOLOGY 的仓位/闸门决策。
// 深研结论的使用口径：每决策日注入（仅深研 run），不分是否深研日。
// 动机：bot 会在自己写的深研报告里把几个观察指标写成「加仓条件（全部满足才执行）」，此后每天
// 从历史窗口里重读自己昨天的复述，把一次写作口误固化成不可逾越的执行闸门。bot105g 2025-03/04
// 即因此连续 20+ 交易日把权益压在风险预算带下限之下，同时自报 risk_on_overweight；而写下该
// 清单的那篇报告，其证伪条件恰恰是「任一命中即加仓」——AND 与 OR 在同一篇里并存。
// METHODOLOGY 早写明深研的定量数字/操作建议仅供参考，但那条在 system prompt 里，离决策点远，
// 敌不过历史窗口里天天复述的自造规则。这里不加禁令，只把「照做的后果」摆到决策点旁边。
const DEEP_RESEARCH_USAGE = `

【深研结论怎么用 · 每天适用】
- 深研里的**目标仓位、加仓/减仓门槛、条件清单都是参考量，不是执行闸门**。仓位下限由你 METHODOLOGY 的风险预算带定，不由某一篇深研的措辞定。
- **当心你自己写下的条件清单**：若某篇深研把几个指标写成「全部满足才加仓」，而同一篇的证伪条件里它们各自独立触发，以**证伪条件（任一命中即成立）**为准——AND 那版是写作口误，不是纪律。历史窗口里你前几天复述过的门槛，不因为复述过就变成事实，回原文核对。
- 深研的零基目标仓位是**写作当日**的参考，隔了几个交易日还拿它当今天的仓位上限，等于用过期结论交易。
- **后果**：因为「深研门槛没全满足」而让仓位停在风险预算带下限之下、当期又拿不出一级价格证据（研究层担忧不算确认），这会被计为一次纪律违反——回补到带内不需要额外理由，**不回补才需要理由**。真要维持，就在今天 mem0_add 记 \`[纪律违规, 深研门槛压仓, 低于下限N个百分点]\`，让它进你的复盘。`

// 空串 → 跳过整块（世界日早于全部报告日时为空）。
function briefingBlock(briefing?: string): string {
  if (!briefing || !briefing.trim()) return ''
  return `\n\n【当日研究室简报（系统预读 · PIT · 宏观策略/政策/国际/跨市场 + regime）】
下面是研究室对**市场策略、政策面、国际地缘、跨市场联动与市场 regime / 风险状态**的当期研判，作为你今天择时的**参考信号**：
- 这是**参考信号，不是指令**——仓位/风险闸门/配置纪律仍以你的 METHODOLOGY 为准，简报只帮你校准方向与力度；
- 段头标「报告日 X · 距今 N 天」，是截至世界当日 PIT 可得的最新一份；标了「已过期」的**自行判断时效**，别把旧结论当当日事实；
- 结合你自己的 PIT 行情与持仓做决策，简报与你的判断冲突时，写清理由后按你的方法论执行。

${briefing.trim()}
【当日研究室简报 结束】`
}

export function renderDailyMessage(ctx: DailyMessageContext): string {
  const weekday = weekdayOf(ctx.date)
  // bot 类型决定"判断管线块"怎么注入（单基金 vs 多基金 分开处理）：
  // - multi-fund（bot101/102/103，多基金权益组合）：注入系统预生成的三份市场研报，直接消费、不自跑主线识别；
  // - single-fund / multi-asset：走旧的 skill 注入路径（INJECT_PIPELINE_SKILLS 当前为空，故实际为空块）。
  const kind = botKindOf(ctx.botId)
  let pipelineBlock: string
  if (kind === 'multi-fund') {
    pipelineBlock = marketReportsBlock(ctx.marketReports, ctx.dailyContext?.account?.holdings, ctx.dailyContext, ctx.date, ctx.tradingCalendar)
  } else {
    pipelineBlock = injectedSkillsBlock(ctx.injectedSkills)
  }
  // 当日研究室简报：仅单指数 run 注入（multi-fund 走上面的 marketReportsBlock，不叠加）。
  const briefing = kind === 'multi-fund' ? '' : briefingBlock(ctx.briefing)
  const contextBlocks = dailyContextBlocks(ctx.dailyContext)
  // 可买池每天都播报；single-fund / multi-fund 文案分两套，复用上面的 kind（解耦池子大小与 bot 决策风格）。
  const buyable = ctx.buyableFundCodes && ctx.buyableFundCodes.length
    ? buyableFundsBlock(ctx.buyableFundCodes, kind, ctx.dailyContext?.buyablePoolMeta)
    : ''
  // History window 放在 daily message 的最顶部——它已经包含自己的"【交易记忆窗口】"标头，
  // 直接拼到 rules block 之前即可。空串（Day 1 / 无 prior session）→ 跳过。
  const intraday = ctx.intradayMarketBlock && ctx.intradayMarketBlock.trim() ? ctx.intradayMarketBlock : ""
  const history = ctx.historyWindow && ctx.historyWindow.trim() ? ctx.historyWindow.trim() + "\n\n" : ""
  // Belief block 由 caller (run.ts) 先 await buildBeliefContext(...) 渲染成完整字符串塞进来；
  // 已自带 header / schema 要求 / 21d 校准反馈，本函数只前置两个换行做分隔即可。空/缺省 → 跳过。
  const beliefStr = ctx.beliefBlock && ctx.beliefBlock.trim() ? `\n\n${ctx.beliefBlock.trim()}` : ''
  // 配置宪章块：run.ts 每日调 charter_status 组装；空/缺省 → 跳过（宪章未启用的 run 即此）。
  const charterPart = ctx.charterBlock ? '\n\n' + ctx.charterBlock : ''
  // 强制块：forced 优先取 deepResearchForced（新 caller），回退 deepResearchDay（老 caller/ordinal）。
  const forced = ctx.deepResearchForced ?? ctx.deepResearchDay
  const deepResearch = deepResearchBlock(forced, ctx.deepResearchGapDays, ctx.deepResearchLastDate, ctx.deepResearchReasons)
  const deepResearchTrigger = deepResearchTriggerBlock({
    authorized: ctx.deepResearchAuthorized,
    forced,
    mode: ctx.deepResearchMode,
    gapDays: ctx.deepResearchGapDays,
    maxGap: ctx.deepResearchMaxGapDays,
    lastDate: ctx.deepResearchLastDate,
  })
  const drUsage = ctx.deepResearchEnabled ? DEEP_RESEARCH_USAGE : ''
  // 单指数持有承诺块（多基金返回空串，安全）。
  const holdCommit = holdCommitmentBlock(ctx.dailyContext, ctx.botId, ctx.date, Boolean(ctx.deepResearchForced ?? ctx.deepResearchDay) || Boolean(ctx.deepResearchAuthorized))
  const deepCommit = ctx.deepResearchCommitmentBlock?.trim() ? "\n\n" + ctx.deepResearchCommitmentBlock.trim() : ""
  if (ctx.isFirstDay) {
    // Day 1 = 冷启动：完整规则 + 可买池/预取上下文 + belief（含 schema + 校准）+ methodology 提示 + 记忆边界。
    // bot 的 methodology 已被 research-loop splice 进 system prompt，daily message 只附短提示。
    return `${history}${fullRules(ctx.date, weekday, ctx.botId, ctx.tradingDaysTotal, ctx.deepResearchEnabled)}${CHAT_BOUNDARY}${buyable}${pipelineBlock}${briefing}${intraday}${contextBlocks}${holdCommit}${deepCommit}${beliefStr}${charterPart}${METHODOLOGY_DAY1_HINT}${FOOTER_FULL}${deepResearch}${deepResearchTrigger}${drUsage}\n`
  }
  // Day N：briefRules + 可买池/数据 + belief + methodology 短提示 + FOOTER_BRIEF（termination contract）
  //        + 策略强制复盘（每 5 个交易日，非复盘日为空串）。复盘块放在最后——最末尾的指令 recency 最高，
  //        让"先定性大趋势 → 业绩对照 → 改策略 or 书面论证维持"成为 bot 收工前读到的最后一条硬约束。
  // FOOTER_BRIEF 的"列了 todo 就要做 + 结束前 mem0_add"对所有 bot 都适用。
  const review = strategyReviewBlock(ctx.dailyContext, ctx.botId)
  // belief↔仓位 言行一致核对：每天（不限复盘日）查 standing belief 方向 vs 实际仓位是否打架。
  // 放在 review 之前——复盘日时 review（更大的"方法论是否失效"硬契约）压在最末尾保持最高 recency；
  // 非复盘日 review 为空串，本块即收尾的最后一条硬约束。无 latestBelief / 无账户 → 空串。
  const coherence = beliefPositionCoherenceBlock(ctx.dailyContext, ctx.latestBelief)
  const authorityReminder = authorityChainReminder(ctx.botId)
  // 周期块放在数据块之前——先把"这是跨 N 日的周期再平衡、下方数据是整段区间"的框架立住，bot 再读数据。
  const period = periodBlock(ctx.periodInfo)
  return `${history}${briefRules(ctx.date, weekday, ctx.botId, ctx.deepResearchEnabled)}${CHAT_BOUNDARY}${buyable}${pipelineBlock}${briefing}${intraday}${period}${contextBlocks}${holdCommit}${deepCommit}${beliefStr}${charterPart}${METHODOLOGY_DAYN_HINT}${FOOTER_BRIEF}${deepResearch}${deepResearchTrigger}${drUsage}${coherence}${review}${authorityReminder}\n`
}
