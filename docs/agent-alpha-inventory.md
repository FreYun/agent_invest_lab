# Agent 投资有效手段台账（单指数 / 多指数）

> 汇总项目至今**已验证有效**与**已证伪**的投资手段，用于指导 bot 方法论设计。
> 「具体 case」的数字为研究/回测结论口径，**不得原样写进方法论文件**（见 `feedback_no_backtest_numbers_in_methodology`）。
> 最后更新：2026-07-29

---

## 一、已验证有效的手段

### A. 认知 / 流程机制类（agent 专属 edge）

#### A1. rsloop —— 研究循环深研引擎

- **方法类型**：流程引擎（多阶段确定性研究状态机）。
- **生效方式**：一次决策日跑 5 阶段深研 SCOUT→DIG→CHALLENGE→SYNTHESIZE→OUTPUT，出口硬门控（无当日「待验证」台账条目直接拒），交割物是行动卡 + 预测台账。它真正的价值在**「选中谁」和执行审计**，不在给出的方向标签本身。
- **有效范围**：多基金选基的**筛选层**，以及**长视界**的相对超额；短视界方向判断不承重。
- **具体 case**：被研究板块 t+63 前瞻收益，比同日未研究的 top5/top15 池高 **+3.2~3.5pp**（t+20 无此差）；charter 口径 t+5 命中 65% > 基准 59%，Brier 0.215 < 气候基准 0.242。
- **真源**：`reference_deep_research_mode`。

#### A2. 深度思考 —— 深研的方向判断

- **方法类型**：agent 认知（模型在决策中做扩展推理，产出 stance / p_up / 证伪条件）。
- **生效方式**：深研给出看多/看空/中性 + 每个 horizon 的数值 p_up + 证伪条件。
- **有效范围**：**只在「相对超额 × 长视界」这一格里有 alpha**；短视界的方向标签在任何视界都零区分度。这条要给你的前提打个折——深度思考「有效」是**受限有效**，不是全场有效。
- **具体 case**：63d 看多标的相对 HS300 超额 **+8~15pp**；但 t+20 看多命中仅 50%，中期结论放在 ≤20d 反而比基准低 11pp。**硬约束：命题与结论的期限必须匹配**（拿中期看空去驱动 20 日内减仓是最贵的亏损来源）。
- **真源**：`reference_deep_research_mode`、`project_bot105d_multi_equity_agent`。

#### A3. 催化剂 / 政策面研判

- **方法类型**：agent 专属（主动检索研报/资讯判断 regime 级政策与流动性催化，factor 编码不了）。
- **生效方式**：用 `research_search` 检索政策 onset，且天然 PIT-clean（end_date≤T 闸门真生效，社媒拉不到历史所以进不了回测、研报可以）。纪律：利多要叠「涨幅闸」只许 hold，利空只信 news 事件 onset、不信卖方研报观点。
- **有效范围**：regime 级政策事件日，价值高度集中在每年**个位数**的大事件日（924 / 政治局 / 关税等）。
- **具体 case**：T=2024-09-24 降准包当晚 research score 0.93-0.99、前一日哨兵零泄漏——可干净入回测。⚠️高危反例：T=2024-10-08 局部顶（次日 −7% 反转前），research 通道清一色 0.5-0.75 看多确认，最该 risk-off 时给最强 add 信号——所以卖方 perma-bull 顶部反向必须硬规则化。
- **真源**：`project_catalyst_dimension`、`research/catalyst_dimension/FINDINGS.md`。

#### A4. 主线研判 —— 五层框架 v5

- **方法类型**：组合策略状态机（月度 + 日度并行）。
- **生效方式**：第0层 regime → 第1层 景气/资金/叙事锚 → 第2层 生命周期 → 第3层 大主线 vs 题材 → 第4层 双测度（成分重叠 × 净值相关）选基；配合惯性持有 + 迟滞/确认窗口 + 最小持有 15 日 + 冷却 15 日，不按日历、不频繁切。
- **有效范围**：多基金大类配置 / 行业轮动（bot101 / 102 / 103）。
- **具体 case**：2025-01~2026-05 超额沪深300 的迭代链——v1 龙头 **−8.1%** → v2 扩散 −1.3% → v3 top3 分散 +8.6% → v4 大主线惯性持有 +19.2% → **v5 + regime 层 +26.8%（最优）**。关键跃迁来自「惯性持有」和「regime 层」，不是选标的更准。
- **真源**：`project_mainline_v5_progress`、`project_mainline_daily_pipeline`、`bots/bot101/主线方法论_五层框架_设计.md`。

---

### B. 量化因子 / 择时做法类（可回测硬验证）

#### B1. 散户申赎反向因子（对 agent 伪装成 market_sentiment）

- **方法类型**：反向情绪因子。
- **生效方式**：读「个人指数型股票基金」累计净申赎的 z 值——z 高 = 情绪过热（散户堆量）= 偏空，z 低 = 情绪冰点（散户割肉）= 偏多，落地为 `retail_z_step_10`。对 agent 整体伪装成「市场情绪指数」，它不知道底层是申赎数据。
- **有效范围**：**宽基 ETF**（HS300 / ZZ1000 等）。这正好印证你的前提——散户数据在宽基投资有效。
- **具体 case**：它是这条数据线的**最优变现**；后续在它基础上挖的 44 个分歧变体（个人-机构差 / 股债切换 / 主被动切换 / 联合状态）**全军覆没**，无一超过它。⚠️但方向假设要小心：「散户买主动基金 = 情绪热」这个 F4 假设是**错的**，hs300/zz1000 全负。
- **真源**：`project_market_sentiment_disguise`、`project_divergence_sr_negative`。

#### B2. 短窗时序动量 mom10-20

- **方法类型**：趋势跟随 / 防踏空因子。
- **生效方式**：看 20 日收盘涨幅正负号，>0 在场、转负空仓（信号 shift(1)、5bp 成本）。它同时防回撤（DD −17% vs −47%）又防踏空（只 46% 在场却年年不落后 BH）。
- **有效范围**：**高波动、深回撤的纯主题成长指数**；宽基中盘（zz500/hs300）上 whipsaw 无效；真业绩强 BH 的指数（半导体设备 931743）上也不加分。
- **具体 case**：机器人 H30590（限 2022+ 人形 regime）Cal 1.63 vs BH 0.06、wf 5/5、bootstrap CI[0.32,1.96]；横截面 7/7 过 BH（机器人 / 创新药 / 半导 / 新能源车 / 消费 / 军工 / 科创50）。⚠️注意：全样本 2015+ 测它是❌的，混进了 2022 前的工业自动化老 regime——**regime 混样会翻案**。
- **真源**：`project_robot_momentum_green_factor`、`project_donchian_index_screen`。

#### B3. 右侧趋势跟随 ma20（站上 20 日线持有 / 跌破清仓）

- **方法类型**：择时做法（趋势跟随）。
- **生效方式**：上涨趋势里**买强不买弱**（买突破/站上均线，别等回调买弱）。关键修正：趋势判断要**快、要内生于入场信号**，别外挂慢的 MA120/200 门——所有 gate_ 版本一律更差，慢门只让你错过新趋势起点。
- **有效范围**：行业指数（申万 31 验证）。「上涨做右侧」稳健成立；「下跌做左侧」证伪。
- **具体 case**：ma20 medCalmar 0.26、94% 击败 buy&hold；等权 31 行业组合 CAGR 14.2% / Sharpe 0.96 / MaxDD −27%；bootstrap Sharpe CI[0.56,1.35]、prob_positive 100%、walk-forward 5/5 子窗为正。「上涨做右侧」B>D 在 H=10 最强 +5~7.5pp 且显著。**右侧本质是低胜率（~37%）高盈亏比，靠砍回撤赢，别用胜率衡量。**
- **真源**：`research/trend_side_winrate/RESULT-trend-side-winrate.md`。

#### B4. donchian_60 通道突破

- **方法类型**：择时因子（通道突破）。
- **生效方式**：突破 60 日通道在场。
- **有效范围**：**只在高波动、强趋势的成长制造业** work；大盘价值 / 宽基 / 震荡行业一律 whipsaw 跑输。
- **具体 case**：✅绿灯——半导体设备 931743 CI[0.28,1.70] wf4/5、中证光伏 931151、中证新能源(深) 399808；❌全毙（别重挖）——中证500/A500、军工、白酒、券商、银行、煤炭、医疗、创新药、消费、科创50、双创50、创业板指。坑：ETF 短样本会造假绿灯，必用全周期指数。
- **真源**：`project_donchian_index_screen`、`research/index_timing/RESULT-index-timing-factor-screen.md`。

#### B5. ERP×VIX 极值组合（黄灯，风险管理器）

- **方法类型**：风险管理 overlay（**不是收益增强器**）。
- **生效方式**：慢估值锚（ERP 股债性价比）定方向 × 快 VIX 极值修 regime 时点。公式 base=clip(0.35+0.6·ERP3年分位) + 0.4·[VIXz>1.5后20日] − 0.35·[VIXz<−1.5]。单用 ERP 会在牛市全程喊「贵」踏空，VIX 极值修好这个病（「贵+恐慌」仍要买、只有「贵+自满」才真卖）。
- **有效范围**：5 个宽基（000300 / 000852 / 000905 / 000906 / 000016）；双创50 无此因子。
- **具体 case**：243 参数 100% OOS 双指数击败 buyhold Sharpe；定级黄灯——OOS Sharpe +0.21~0.34、回撤减半，但 2025 牛市让出 ~8% 绝对收益。大部分超额来自 2024 V 型年。权重必须 ≤ 主维度。
- **真源**：`project_erp_vix_timing_factor`。

#### B6. 券商 mom_20 + VIX>90% 逆向 hold20

- **方法类型**：组合因子（承重 + 逆向增益）。
- **生效方式**：mom_20 承重在场，叠加 VIX 3 年分位 >90% 时逆向 hold20 加仓；PE 分位 <10% 强制 30% baseline 作底部试探（黄灯，不承重）。
- **有效范围**：券商 399975。
- **具体 case**：mom_20 + VIX>90% 逆向是最强增益，OS Cal +0.72、dd −16.6% vs BH −37.8%、wf 4/5。⚠️反例：bot13 自造的「板块风险分>0.7 才在场」把自己从 1 月空到 6 月共 141 交易日，回测证伪——板块风险分是 mom_20 的**负 alpha 稀释器**。
- **真源**：`project_securities_composite_factors`、`research/index_timing/RESULT-securities-composite-factors.md`。

---

### C. Framing / 行为约束类（治病而非增收）

#### C1. 无 edge 默认持 beta

- **方法类型**：framing / 默认态设计。
- **生效方式**：把「没信号」翻译成「在场」而不是「离场」——看不清时偏高 baseline 贴近 beta，🟡择时工具只从高 baseline 削尾部回撤、**绝不 gate 入场**。踏空病根就是把「没信号」错译成「离场」。
- **有效范围**：没有择时 edge 的宽基（zz500 / hs300 / a500）。
- **具体 case**：zz500 三大因子族（价格/估值/中盘本命）挖穷，无一到✅绿灯——解法不是加信号，是改默认。bot16 在此 framing 下 v2.0 +16.62% / MDD −3.4% / Sharpe 2.17，急性踏空已自愈。
- **真源**：`project_zz500_no_edge_hold_beta`。

#### C2. belief↔仓位 言行一致硬约束

- **方法类型**：引擎行为约束（治踏空棘轮）。
- **生效方式**：bot 每天写的 belief（t+20 上涨概率）与仓位方向矛盾时每天硬拦——看多（p_up≥0.55）却空仓（<20%）拦、看空（≤0.45）却重仓（≥40%）拦；中性带/同向/缺 belief 不拦。因 belief 被 Brier 校准，bot 没法靠「嘴硬写低 p_up」给空仓开脱（看空看错同样扣分），所以这不是又一道能被辩解掉的文字劝导。
- **有效范围**：单指数 bot 防长期空仓踏空。
- **具体 case**：治 bot6 军工 / bot10 黄金那类长期 0% 空仓的单向棘轮。根因是「做错的痛具体、踏空的痛弥散」的不对称，纯文字劝导都被 bot 自我辩解掉，只有把仓位焊回自己说出口的信念才有牙齿。
- **真源**：`project_belief_position_coherence`、`feedback_single_index_underholding_fixes`。

#### C3. 入场即承诺 + 论证式早退

- **方法类型**：软约束（防翻烙饼）。
- **生效方式**：建仓即知须持有到免赎档；临时早退要求 agent 给出**经过思考的充分论证**，而**不写死技术指标逃生口**（不是「收盘破 MA60 就允许早赎」这种机械触发）。牙齿在于「裁量有成本」而非「裁量被禁止」。
- **有效范围**：单指数防频繁交易 / 翻烙饼。
- **具体 case**：用户 2026-07-29 定调——裁量清仓不禁，但清了 10 个交易日不许买回（看对是 alpha、看错自己吃踏空，翻烙饼的免费午餐没了）。禁止性修法（收回裁量权）会被否决，因为「用 bot 要的就是裁量 alpha」。
- **真源**：`feedback_bot_discretion_over_rules`。

---

## 二、已证伪 / 别重挖（对照的另一半，同样重要）

- **regime_score total_score 做择时** —— ❌三关全挂。它是当日状态描述器不是预测器（corr 当日 0.73 但 t+1 仅 0.04，漏 shift(1) 会凭空造 +0.8~1.0 Sharpe 同日泄漏）；去重叠后无 alpha，降回撤的功劳其实是静态低仓的功劳。真源 `project_regime_score_timing_negative`。
- **申赎分歧因子家族** —— ❌全军覆没。个人-机构差 / 股债切换 / 主被动切换等 44 变体无一超过 `retail_z_step_10` baseline。真源 `project_divergence_sr_negative`。
- **板块风险分 gate 择时** —— ❌负 alpha 稀释器。券商上所有 gate 变体都劣于裸 mom_20。真源 `project_securities_composite_factors`。
- **PE / 估值分位做择时扳机** —— ❌在中国趋势市是**右侧反向指标**。戴维斯双击估值一路扩张，拿「贵」封顶=砍在主升里；估值只提示风险刻度（该不该想），**永不给兑现扳机**（该不该卖），趋势转弱才是离场扳机。真源 `project_catalyst_dimension`。
- **多元回归 / 温度 / MA 乖离** —— ❌OOS 噪声 / 过拟合（多元回归 OOS R²=−0.12）。真源 `project_erp_vix_timing_factor`。
- **下跌趋势做左侧抄底** —— ❌独立样本上左侧不如右侧。A>C 只在 V 型反转段成立，而 V 型事前无法识别。真源 `research/trend_side_winrate/RESULT-trend-side-winrate.md`。
- **深研看空腿升级清仓** —— ❌严重不对称。判对仅避 −2~−5%、判错踏空 +6~8%（被看空标的还反跑赢大盘 +4.5pp）；看空只该用来「不买」+降档，升级清仓一次就吃掉全年规避总和。真源 `reference_deep_research_mode`。

---

## 三、三条贯穿性规律

1. **趋势是一阶、方向 / 因子是二阶** —— 上涨环境本身就是胜率来源（上涨趋势里随便哪天买都高于下跌趋势里随便买）。择时做法主要靠**砍回撤**赢，右侧胜率仅 ~37% 但高盈亏比，别用胜率衡量右侧。
2. **regime 混样是最大陷阱** —— 机器人指数全样本 2015+ 判❌、限定 2022+ 人形 regime 后 mom 翻案；判「某指数无绿灯」之前先想有没有把两个 regime 混在一起。
3. **同一手段在宽基 / 主题上结论相反** —— 动量 / donchian 在高波动纯主题成长有效、在宽基中盘 whipsaw；宽基靠「持 beta + 散户反向 + ERP×VIX 风控」，主题靠「趋势动量防踏空」。

---

## 四、按投资对象速查

- **单指数（bot1~bot20）**：
  - 主题成长指数 → mom10-20 / donchian 防踏空（B2 / B4）；
  - 宽基 → 持 beta 默认态 + 散户反向 + ERP×VIX 风控（C1 / B1 / B5）；
  - 全类通用治病 → belief↔仓位一致 + 入场即承诺（C2 / C3）。
- **多指数（bot101~103）**：
  - 承重 → 主线研判五层框架 v5（A4，regime + 惯性持有）；
  - 选基 → rsloop 深研的筛选层与长视界相对超额（A1 / A2）；
  - 事件 → 催化剂维度做 regime 级政策哨兵（A3）。

---

## 五、公共 agent 注入（res 系列 / 研报注入侧）

> 上面第一~四部分是 bot **自己**用的手段；这一部分是**系统侧每天注入进 bot 决策提示的公共研判**——由 `.openclaw` 的 15 个 res 研究室 agent 产出，经 world 引擎按 bot 类型路由。这是「宏观 / regime / 政策 / 板块」这层信号的唯一来源（bot 在回测里 web_search/web_fetch 被 deny）。

### 5.1 res 研究室清单（15 室）

`/home/rooot/.openclaw/workspace-res{1..15}`，每个是一个 openclaw 心跳 agent，每天各产一份解读。按性质分四类：

- **研判室（落 `fund.db` 的 `res_reports` 表，进注入）**：
  - res1 宏观策略（`market_strategy`，stance=risk_on/risk_off/neutral）
  - res2 政策研究（`policy_analysis`，tighten/ease/neutral）
  - res4 国际关系（`intl_relations`，escalate/deescalate/stable）
  - res5 中美市场（`cross_market_linkage`，strong_linkage/decoupling/mixed）——⚠️**已停更**（卡在 2026-06-18），简报段头会自动标「已过期」由 bot 自判时效。
- **资讯室（只采集不研判，落 `market_reports` 表的 `macro_news`）**：
  - res3 资讯研究室——10 维宏观矩阵检索 simworld news 档，近一周资讯，PIT。
- **板块室（落文件系统 `workspace-resN/memory/*.md`，回测进不去）**：
  - res6 硬科技 · res7 能源材料 · res8 医药生物 · res9 大消费 · res10 金融地产 · res11 周期资源 · res12 高端制造。
- **技术 / 质控 / 行为室**：
  - res14 指数技术面（10 宽基技术面全景，择时核心信号室，文件系统）；
  - res13 质控室（对其它 res 室的内部质检，meta 信息，**不喂交易 bot**）；
  - res15 申赎行为（申赎因子/择时信号，**可选**，要申赎信号才纳入）。

### 5.2 两条存储路径 —— 决定「能不能进回测」

- **A 路：`fund.db` 表（PIT 安全，回测 + live 都能用）**。`market_reports`（`market_context` 行情/regime、`macro_news` res3、`market_mainline` 主线、`mainline_rotation` 组合）+ `res_reports`（res1/2/4/5 四研判室，**纯 append 无去重**，回读取 `as_of_date DESC, id DESC` 最新一份）。取数一律带 `AND as_of_date <= worldDate`，天然挡未来函数。
- **B 路：文件系统 `workspace-resN/memory/*.md`（只有 live 有历史）**。板块室 res6-12、技术面 res14 只落这里，**2025 全年 0 份**，所以喂回测=要么泄漏未来、要么恒为空——已弃用于回测。
- **一句话**：回测里能用的公共研判 = A 路那几张表；板块维度（res6-12 / res14）目前**只在 live 可得、回测拿不到**，这是已知且已被用户接受的缺口。

### 5.3 注入路由（world 引擎当前口径，`message.ts` / `run.ts`）

按 `botKindOf(botId)` 分流（`/^bot1\d{2}/`=multi-fund，其余=single-fund），两条互斥不叠加：

- **multi-fund（bot101/102/103）→ `marketReportsBlock`**：系统预读注入三份 `market_report`（`market_context` + `market_mainline` + `mainline_rotation`），带强指令「直接采用、不要自己再调 sector_* 重跑」。这根治了「靠 bot 自觉跑主线流水线、遵循度低」的老病（bot101 daily message 从带 4 skill 的 ~20 万字符降到 ~9千字）。
- **single-fund（bot1~bot20）→ `briefingBlock`（=`assembleBriefing`）**：读 `fund.db` PIT 装配「当日研究室简报」= `market_context` + `macro_news` + res 四研判室（`market_strategy` / `policy_analysis` / `intl_relations` / `cross_market_linkage`）。**刻意排除** `market_mainline`（主线板块+基金池）与 `mainline_rotation`（核心/卫星组合骨架）——那是多基金配置指令，单指数 bot 只买一只指数基金用不上。措辞=「参考信号，非指令」。
- multi-fund 的 briefing 置空（走上面 marketReportsBlock，不重复叠加）。

### 5.4 关键坑与教训

- **未来函数泄漏（严重，已修）**：`intraday-briefing` 早期为 live 设计、只取「文件名日期最大」那份而不卡 `asOfDate`，搬进回测后在 2025-01-02 决策日被注入 2026-07 的市场环境——bot 提前看了一年半盘面。**教训：任何「取最新一份」的注入器搬进回测前，必须确认它按 world date 卡上界，而不是取库/文件系统的真·最新。**
- **PIT 干净 ≠ 有正确历史信号**：给文件系统路径加日期过滤只能堵泄漏，但那些房间 2025 没有历史，结果是「修对了」= 变空。搬数据源时要确认新源在回测时段真有 PIT 历史，且覆盖的信号维度可接受（这次丢了板块维度）。
- **res1 日期错位一天**：今早生成的报告文件名却是昨天日期（内容是新鲜的）；各室命名不统一（res1=`YYYY-MM-DD-市场环境.md`、res5=`-隔夜美股A股传导.md`、其余=纯 `YYYY-MM-DD.md`）。别按「今天日期精确匹配」找。
- **回填现状**：`res_reports` 四类各周度 76/76 ISO 周闭合（2025-01~2026-06），**日度未做**（密度 ~22%）；`macro_news` 79 个交易日。回填 driver=`armor/_shared/backfill_res_report.py`（PIT 取数走 simworld 18078 + qwen3.5-plus 生成）。

### 5.5 有效性定位（诚实）

res 注入是**给 bot 补「宏观 / regime / 政策」这层它自己看不到的上下文**，属第 A 类「认知输入」而非可独立回测出 alpha 的因子。它与 A3 催化剂维度同源（都靠 research 检索），有效性受同样约束：政策 onset 有价值但稀疏、卖方 perma-bull 顶部反向是高危、利多要叠涨幅闸。**单指数简报是否真提升择时，端到端 A/B 尚未跑**（注入机制已落地、未合并/未端到端验证）。

**真源**：`reference_res_research_rooms`、`project_res_briefing_injection`、`project_res_reports_backfill_driver`、`project_res3_news_agent`、`project_bots_consume_reports`。
