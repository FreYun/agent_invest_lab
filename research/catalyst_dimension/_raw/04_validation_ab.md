# 第七维「催化剂/政策面」A/B 验证协议（48080 dashboard + world）

> 目标：在现有 world 回放 + 48080 backtest-dashboard 上，跑出一个**统计上诚实**的 A/B，证明「给 market-context 技能加催化剂第七维」是否真带来风险调整后超额（而不是 LLM 噪声、不是 agent 顺手看了价格、不是 headline-chasing overtrade）。
> 用户 SOP 纪律：单一 OOS 窗口 = 单次高方差抽样，永远不足以判定。所以本协议 = **4 窗口 × 多人设 × 重复抽样 × 配对差分**。

---

## 0. 机制依据（已读源码确认，不要再验证）

- 一次 run = `(bots 名单, replay.from/to, model, buyable_fund_codes, skillsRoot)`。`world/src/run.ts:setup()` 把每个 bot 的源工作区 **复制成 shadow workspace**（`buildShadowWorkspace`，按 `shadow_include` 选目录），所以 bot 自带的 `skills/market-context/SKILL.md` 进了 shadow。
- **催化剂第七维的载体 = 每个 bot 自己的 `bots/<id>/skills/market-context/SKILL.md`**（实测：共享 `skillsRoot` 只有 `research/`，market-context 是 per-bot 的，例 `bots/bot101/skills/market-context/`）。→ A/B 的「唯一变量」就是这个文件的版本。
- DB 一切按 `(bot_id, run_id)` 主键存（`fund_bot_daily_snapshots / _actions / _reviews / _position_snapshots`）。`initial_capital` 是 per-run 字段，`init_fund_account --reset` 保证每 run 干净起点 100 万全现金（`run.ts:402`）。
- **48080 已经原生支持「同 bot 多 run 对比」**：`/api/backtest/bot?bot_id=&run_id=` + `listRunsForBot` 返回该 bot 的全部 run。所以 A run 和 B run 在看板上天然并排可比，无需改 server。
- 指标全部已落库：`max_drawdown_pct`、`daily_return_pct`、`cumulative_return_pct`（→ Sharpe/Calmar）；`fund_bot_actions.side`(buy/sell/hold) + `fund_bot_reviews.turnover_ratio`（→ 换手/交易次数）；`fund_bot_reviews.regime`（→ regime 判定一致性）。
- benchmark = 该 bot 首买基金的 B&H（缺省 510300）。daily-context 也喂给 bot 一条单标的 B&H 基准。→ alpha 有现成锚。
- **关键约束（confounder 来源）**：`max_total_turns=12`、`max_time_minutes=30`、`chat_temperature=0.3`（非 0，**有** LLM 随机性，无显式 seed knob）、world 内 `web_search/web_fetch` 被 deny（催化剂只能走 research_*，天然 PIT-clean）。

---

## 1. 对照设计：同 bot，两个技能版本，配对差分

**采用「同一 bot × 两版 SKILL.md × 两个并行 run」**，而不是「两个不同 bot」。理由：人设/方法论/记忆都相同，唯一差异锁死在第七维。

### 1.1 两个技能版本（唯一变量）
- **arm C（control / 6 维）**：现网 `market-context/SKILL.md`，六维证据（趋势/宽度/量能/情绪/估值/宏观）→ regime + timing_stance。
- **arm X（experimental / 7 维）**：在 C 基础上**只追加**「### 催化剂/政策面」一节 + research_* 取数+onset/confirmation/lead-lag/price-in 四问框架（来自 Phase-1 分类学）。六维部分逐字不动，输出 schema 不变（regime + timing_stance + 证据章节），只多一节。

> 诚实纪律落地：arm X 的催化剂节里**只写判断框架（带 INFOCODE 出处的四问）**，禁写未回测硬数字（"冲击后 X 天反弹"这类）。否则验证的是"拍脑袋规则"不是"读催化剂这个动作"。

### 1.2 配置怎么做到「只差这一维」
两个 run 共享一切，只换 SKILL.md：

```
# A/B 唯一差异：把 arm 版本的 SKILL.md 放进各 bot 工作区，其余 world.yaml 完全相同
runC:  bots=[bot_persona×K], replay=<window>, model=qwen3.6-plus,
       buyable_fund_codes=<同一池>, 各 bot skills/market-context/SKILL.md = C 版
runX:  bots=[同一批 bot], replay=<同一 window>, model=同, buyable=同,
       各 bot skills/market-context/SKILL.md = X 版
```

落地有两种等价做法（任选，推荐 b）：
- (a) 维护两套 bots 目录（bots-C / bots-X），只有 market-context/SKILL.md 不同，`bots_root` 指向不同根；
- (b) **脚本切换**：跑 runC 前把 C 版 SKILL.md 写入各 bot 工作区→起 run；跑完把 X 版写入→起 runX。因为 shadow 是**复制**（run.ts 在 setup 时一次性 cp），run 起来后改源文件不影响在跑的 run，两 run 串行无污染。

### 1.3 必须钉死保持相同的量（否则混入额外变量）
| 项 | 取值 | 出处 |
|---|---|---|
| model | qwen3.6-plus（同 base_url/key） | trading-rl-config.json |
| 初始资金 | 1,000,000，`--reset` | run.ts init |
| buyable_fund_codes | 同一池（用现网那份）| world.yaml |
| replay 窗口 | 同 from/to（每个测试窗一对）| world.yaml |
| 12 轮 / 30 分钟 / 900k token | 完全相同 | limits |
| chat_step_days / research_day_every | 相同 | world.yaml |
| 记忆 | 每 run 独立 MemoryStore（run.ts 每 run new），起点都空 | run.ts:300 |
| 人设/METHODOLOGY 起点 | 同一份（见 §4 update_my_strategy 处理）| shadow |

---

## 2. 窗口选择（4 段，全部 PIT-clean、研究端已实测）

见 windows 字段。四段缺一不可，覆盖催化剂的「有效 / 已price-in / 危机 / 贫乏」四种成色：

1. **924 onset（有效催化富集）**：测「读到 onset → 敢 add → 吃主升」是否带来正 alpha。这是 X 应该赢的段。
2. **confirmation 确认期（催化富集但已 price-in）**：研报此时喊"政策底确认/牛市启航"、指数已 +20~25%。测 X 会不会被 confirmation 研报骗去**追高接盘**（perma-bull 偏差陷阱）→ 这是 X 可能**输**的段，必须纳入，否则只挑 X 赢的段 = cherry-picking。
3. **关税 crisis（危机/risk_off 催化）**：测 X 能否更早 defensive/risk_off（A 股滞后海外 1 日的减仓窗口），以及能否识别「冲击底=国内对冲政策包起点」而不是恐慌追杀。看回撤差。
4. **磨人震荡（催化贫乏）**：哨兵已实测 top score 0.38、无 onset 簇。测 X 会不会**平白 overtrade**——明明没催化，却因为每天打 research_search 看到背景噪声而多调仓。这是「无害性」测试，X 在这段**不能显著加换手/降 Sharpe**，否则就是 net 负担。

> 为什么不能只用 1 个窗口：单窗 = 单次高方差抽样。X 在 924 赢可能纯运气；只有「924 赢 + confirmation 不被骗 + crisis 少回撤 + 震荡不 overtrade」四段同时立住，才是维度真有效。

---

## 3. 指标（全部从 48080 已落库字段算）

按 **(bot, arm, window)** 三元组算，再做 X−C 配对差分：

**收益/风险调整**
- `alpha_vs_bench` = bot cumulativeReturnPct − benchmark(首买基金 B&H) 区间收益。
- **Sharpe**（区间日收益年化）、**Calmar** = 区间年化收益 / |max_drawdown_pct|。**主指标 = ΔSharpe、ΔCalmar = X − C**（配对）。
- 用 Sharpe/Calmar 而非裸收益：裸收益在牛市段会奖励"无脑满仓"，无法区分"读催化剂"和"恰好满仓"。

**危机段**
- crisis 窗的 `max_drawdown_pct`：**ΔMaxDD = MaxDD_X − MaxDD_C**（越负越好，即 X 回撤更浅）。

**交易行为（防 headline-chasing）**
- 交易次数 = `COUNT(fund_bot_actions where side∈{buy,sell})`；换手 = `AVG(fund_bot_reviews.turnover_ratio)`。**ΔTurnover、ΔTradeCount**。
- 尤其看**震荡窗**的 ΔTurnover：X 在催化贫乏段若换手显著上升 = overtrade 反例。

**跨人设一致性**
- 同窗内多个 bot 人设，X−C 差的**符号一致率**（多少比例 bot 上 ΔSharpe>0）。维度有效应表现为**方向稳定**，而非个别 bot 暴赚拉平均。
- regime 判定：X 的 `regime` 在 onset 日是否比 C 更早切到"牛/危机"（对齐已知 onset 日 924/0403）。

---

## 4. 混淆控制（confounds 详见 confounds 字段，这里给操作手段）

**C1 「读催化剂」vs「顺手看了价格」**
- 两 arm 拿到**完全相同的 daily-context（含价格/PnL/指数）和相同的 simworld 价格工具集**。唯一差异是 X 多了催化剂节 + research_* 取数引导。→ 任何 alpha 差不可能来自价格信息差，只能来自催化剂维度。
- 审计：从 `fund_bot_reviews.reason` / `fund_bot_actions.bot_reason` 抽样，确认 X 的加减仓**引用了 INFOCODE/政策事件**而非纯价格动量。引用率低 → alpha 即便存在也不是"读催化剂"贡献，判为伪相关。

**C2 LLM 随机性 vs 维度差异**
- temperature=0.3 非 0、无 seed knob → 单 bot 单 run 的 X−C 差里混着采样噪声。对策：**N≥3 人设 × R≥3 重复 run/arm/window**（同配置重跑，run_id 不同）。把 X−C 当配对样本，对 ΔSharpe 做**配对符号检验 / Wilcoxon**（小样本、不假设正态）。
- 用**配对差分**而非两组独立均值比较：同 bot 同窗 X 减 C，消掉人设固定效应，噪声只剩采样项，功效高得多。

**C3 资源/轮次预算混淆（重要，源码确认）**
- 12 轮硬顶下，X 每天要花 1-2 轮打 research_search，等于**从仓位决策轮次里挪走**。若 X 输，可能不是维度无用，而是"催化剂挤占了决策轮次"。对策：
  - (i) 记录两 arm 的 `iterations`/`toolCalls`（status 文件已存），确认 X 没系统性撞 12 轮顶。
  - (ii) 备选 arm X′：把 research_search 预算从 12 轮里**额外加 1 轮**给催化剂（research_day 已有更宽预算可借），单独对比 X′ vs X，隔离"维度价值"与"轮次成本"。

**C4 update_my_strategy 漂移**
- bot 跑中可用 `update_my_strategy` 改自己 METHODOLOGY.md（revisions 落 jsonl）。X 和 C 会各自漂移，长窗里方法论可能分叉 → 不再是"只差第七维"。对策：
  - 短窗优先（每个测试窗 ≤ 4 周，漂移有限）；
  - 审计 `runDir/strategies/<bot>.revisions.jsonl`，若某 arm 发生 regime 级方法论改写则该 run 作废重跑；
  - 或本实验**禁用 update_my_strategy**（两 arm 都禁，保持对称），把"方法论自进化"留到维度证实后的二期。

**C5 PIT 泄漏 / 前视**
- end_date 必须 ≤ T（源码 simworld-proxy 注入 simulated_datetime；研究端 end_date>T 报错）。X 的 research_* 全程经 proxy，PIT-clean 已验证（T 退到 924 之前同 query 零 924）。web_search/web_fetch 被 deny → 无外网泄漏。无需额外控制，但**抽查 X 的 bot_reason 不得引用晚于 T 的 INFOCODE**（INFOCODE 带 YYYYMMDD，可直接核）。

**C6 池/对照污染**
- 两 run 用同一 buyable_fund_codes；benchmark 同为各 bot 首买 B&H。检查 dashboard 的 `contaminated` marker（universe-contamination.json）——若任一 run 被标污染（看到了别 run 的池子），作废重跑。

---

## 5. 样本量 & go/no-go（诚实判据）

**最小阵列**：4 窗口 × N=3 人设 × R=3 重复 × 2 arm = **72 个 run**（配对后 = 4×3×3 = 36 个 X−C 配对差）。每窗 ≤4 周 × ~1 run 时长，串行可控；concurrency 内多人设并行。

判据见 pass_criteria 字段。核心逻辑：**「赢得多」不够，必须「不在该输的地方乱输」**——
- onset 段要显著正（维度有 alpha）；
- confirmation 段**不显著为负**（没被 perma-bull 研报骗去追高）；
- crisis 段回撤**不更差**（最好更浅）；
- 震荡段换手**不显著上升**（无 overtrade 副作用）；
- 且跨人设方向一致（不是单 bot 运气）。

**只有四段全过 + 配对检验显著 + bot_reason 审计证明 alpha 真来自催化剂引用，才判「维度有效，推广全体 100+ bot」。** 任一段翻车 → no-go 或仅"局部启用"（如只在危机/onset 段开第七维，震荡段关）。这正是用户 SOP 要的"多窗口 + 跨人设 + 明确判据"，杜绝单窗 cherry-pick。

---

## 6. 执行 checklist
1. 写 X 版 SKILL.md（C + 催化剂节，schema 不变，只追加一节，无硬数字）。
2. 选 N=3 人设 bot（覆盖激进/稳健/逆向，跨 index），两 arm 禁用 update_my_strategy（C4 对称）。
3. 对每个 (window, persona, repeat)：先跑 runC 再跑 runX（串行，源文件切换，shadow 复制隔离）。
4. 等 run done，从 48080 `/api/backtest/bot` 拉 (bot,run) 序列，算 Sharpe/Calmar/MaxDD/Turnover/TradeCount。
5. 配对差分 + Wilcoxon/符号检验；抽样审计 bot_reason 的 INFOCODE 引用率 & PIT 合规。
6. 对照 pass_criteria 出 go/no-go。

## [windows]
[
  {
    "name": "924 反转 onset（催化富集且有效）",
    "date_range": "2024-09-23 → 2024-10-11",
    "why": "924 政策包（降准50bp+SFISF 5000亿+回购增持再贷款3000亿+平准基金表态）onset 段。research 端实测 top 0.90-0.97 且前 3 条成簇，PIT-clean（T 退到 9-20 同 query 零 924）。这是 X 应该赢的段：读到 onset → 敢 add_on_pullback/aggressively_add → 吃国庆前后主升。检验维度有没有正 alpha 的主战场。"
  },
  {
    "name": "confirmation 确认期（催化富集但已 price-in）",
    "date_range": "2024-10-14 → 2024-11-08",
    "why": "节后指数已较 924 前 +20~25%，research 此时白纸黑字写'政策底确认/牛市启航/前4日涨超10%'（实测 AP202410031640156587）。测 X 会不会被 confirmation 研报的 perma-bull 偏差骗去追高接盘。这是 X 可能输的段，必须纳入以防只挑赢的段 cherry-pick——'不被骗'与'924 赢'同等重要。"
  },
  {
    "name": "关税 crisis（危机/risk_off 催化）",
    "date_range": "2025-04-02 → 2025-04-30",
    "why": "对等关税行政令冲击，research/news 端实测 onset 当天 top 0.98-0.99（8 条多券商同事件），PIT-clean 已验证（202504033364930177）。A股因时差/节假日滞后海外约 1 交易日，给 onset 当晚 defensive/risk_off 窗口。同时测 X 能否识别'冲击底=国内对冲政策包起点'（4-03 当天开源已预判降准降息稳地产）而非恐慌追杀。主看 ΔMaxDD。"
  },
  {
    "name": "磨人震荡（催化贫乏）",
    "date_range": "2025-05-21 → 2025-06-20",
    "why": "本次实测哨兵 query 'A股 策略 政策 货币 财政 监管 拐点' top score 仅 0.38、无 onset 簇（只有背景关税博弈/政策节奏评论）。催化剂贫乏段，用来测 X 的'无害性'：没催化时它会不会因每天打 research_search 看到背景噪声而平白 overtrade/多调仓、拉低 Sharpe。X 在此段换手不能显著高于 C，否则维度是 net 负担。"
  }
]

## [pass_criteria]
[
  "onset 段（924）：配对 ΔSharpe = Sharpe_X − Sharpe_C 中位数 > 0 且 Wilcoxon/符号检验在 36 配对上 p<0.05；且 ΔCalmar 中位数 > 0。证明维度在'有效催化富集'段确有风险调整后 alpha。",
  "confirmation 段（已 price-in）：ΔSharpe 中位数 **不显著为负**（双侧 p>0.10 或 95%CI 含 0）。即 X 没有被'政策底确认/牛市启航'研报系统性骗去追高接盘。若此段 X 显著输 → perma-bull 偏差已落地，no-go。",
  "crisis 段（关税）：ΔMaxDD = MaxDD_X − MaxDD_C 中位数 ≤ 0（X 回撤不更深，最好更浅）且统计上不显著为正。证明第七维至少不损害、理想情况下改善危机防御。",
  "震荡段（催化贫乏）：ΔTurnover 与 ΔTradeCount 中位数不显著为正（X 不 overtrade）；且 ΔSharpe 不显著为负。证明维度在无催化时'无害'，不会平白增加交易摩擦/噪声。",
  "跨人设一致性：onset+crisis 两段合并，ΔSharpe>0 的 (bot,window) 占比 ≥ 2/3（方向稳定），而非单一 bot 暴赚拉平均。维度有效应是普适方向信号。",
  "归因证据（防伪相关）：抽样审计 X 的 fund_bot_actions.bot_reason / fund_bot_reviews.reason，onset+crisis 段的加减仓决策引用 INFOCODE/政策事件的比例 ≥ 50%，且无引用晚于 T 的 INFOCODE（PIT 合规）。若 alpha 存在但决策不引用催化剂 → 判为价格动量伪相关，不算维度功劳。",
  "资源公平（C3）：两 arm 的 iterations 分布无系统性差异，X 未系统性撞 12 轮顶（撞顶率差<10pct）。否则需用 arm X′（额外 1 轮给 research）重测，隔离'维度价值'与'轮次成本'后再判。",
  "总判据（推广全体）：上述 1-7 全部满足 → 'GO，第七维有效，推广 100+ bot'。若 onset 赢但 confirmation/震荡段翻车 → 'GO-partial，仅 onset/crisis 段启用催化剂，震荡段降级为六维'。若 onset 段都不显著 → 'NO-GO，维度无 alpha'。"
]

## [confounds]
[
  "价格信息泄漏（C1）：超额可能来自 agent 顺手看了价格而非读催化剂。控制=两 arm 共享完全相同的 daily-context（价格/PnL/指数）与相同 simworld 价格工具集，唯一差异是催化剂节 + research_* 引导；并审计 bot_reason 的 INFOCODE 引用率，引用率低则判伪相关。",
  "LLM 采样随机性（C2）：chat_temperature=0.3 非 0 且无显式 seed knob，单 run 的 X−C 差混着采样噪声。控制=N≥3 人设 × R≥3 重复 run，用同 bot 同窗的配对差分（消掉人设固定效应）+ Wilcoxon/符号检验把噪声与维度信号分开。",
  "轮次/资源预算挤占（C3，源码确认）：12 轮硬顶下 X 每天花 1-2 轮打 research_search，从仓位决策轮次挪走；X 若输可能是轮次成本而非维度无用。控制=记录 iterations/toolCalls 确认未撞顶；备选 arm X′ 额外给 1 轮 research，隔离维度价值与轮次成本。",
  "update_my_strategy 方法论漂移（C4）：两 arm 跑中各自改 METHODOLOGY.md，长窗里方法论分叉，不再'只差第七维'。控制=测试窗 ≤4 周限制漂移 + 审计 revisions.jsonl 发现 regime 级改写则作废；或两 arm 对称禁用 update_my_strategy。",
  "PIT 前视泄漏（C5）：催化剂若读到晚于 T 的研报即前视作弊。控制=research_* 全程经 simworld-proxy 注入 simulated_datetime（end_date>T 报错），web_search/web_fetch 被 deny，已验证 PIT-clean；额外抽查 bot_reason 不引用晚于 T 的 INFOCODE（INFOCODE 带 YYYYMMDD 可直接核）。",
  "可买池/对照污染（C6）：两 run 池子或 benchmark 不一致会引入额外变量；dashboard 的 contaminated marker 表示某 run 看到了别 run 的池。控制=两 arm 同一 buyable_fund_codes、同首买 B&H 基准；任一 run 被标 contaminated 则作废重跑。",
  "窗口择段偏差（cherry-pick）：只选 X 赢的 onset 段会高估维度价值。控制=强制四段同测，含 X 可能输的 confirmation 段和可能 overtrade 的震荡段，go 判据要求'该输的段不乱输'，而非只看赢的段。",
  "牛市满仓伪 alpha：onset 段裸收益会奖励'恰好满仓'而非'读催化剂'。控制=主指标用 Sharpe/Calmar（风险调整）而非裸收益，并结合 regime 判定时点（X 是否在 924/0403 onset 日更早切 regime）与 bot_reason 归因交叉确认。"
]