## Layer 3 第七维「催化剂/政策面」—— 可直接粘贴段落

> 下面三块按位置插入现有 `market-context/SKILL.md`：
> (A) 插到 Layer 3 六维之后，作为第七个 H3 `### 催化剂`；
> (B) 「数据采集」章节新增一个 `### 催化剂采集流程`；
> (C) Layer 5「择时口径」里新增 `catalyst_stance` 字段及其联动框架。
> 同时「正文硬约束」一节要把 H3 计数从 6 改 7（见 integration_notes）。

---

### （A）插入到 Layer 3，作为第七个 H3

```markdown
### 催化剂

> 这是第七维，与上面六维同级，**必填**。回答一个 factor 永远答不出的问题：**当前有没有 regime 级别的政策/流动性/产业/监管/外围催化正在发生或正在被消化？** 输出的不是一个数字，是一段**带 INFOCODE 出处的推理**。

输出要求（逐条）：

- 给出一份**近期 regime 级催化清单**（0~3 条，宁缺毋滥；安静期就写"本窗口无 regime 级催化（哨兵 query top score=0.XX，低于门控）"）。
- **每一条催化必须带 INFOCODE 出处**（如 `AP202409271640066814` 或 news 的 `202504033364930177`），后面跟一句**方向判断**。无 INFOCODE 出处的催化判断**一律不许写**——没查到就是没有，不许凭记忆/常识脑补"最近应该有降准"。
- 每条催化的方向判断套用**四问框架**（读出处自己推，不背结论）：
  1. **类别**：货币流动性 / 外围地缘冲击 / 政治局-中央经济工作会议定调 / 维稳平准资金 / 产业战略 / 制度供给 / 宏观数据（前四类才有 regime 级择时价值，后三类"读但不作宽基加减仓扳机"）。
  2. **方向**：利多 / 利空（外围冲击是少数能让你主动转 defensive/risk_off 的利空催化）。
  3. **onset vs confirmation**：这条是**催化落地当天/次日的 onset**（可择时），还是研报已在喊"政策底确认/牛市启航/前 N 日涨 X%"的 **confirmation**（指数多半已大涨、利好已 price-in，是事后解释不是入场点）？判据：看 INFOCODE 日期是否贴近事件爆发日、SUMMARY 里有没有"已涨/确认/启航"字样。
  4. **lead-lag / price-in**：领先还是同步？直接给买股资金的结构工具（SFISF 互换便利 / 回购增持再贷款 / 平准基金）领先性最强；普通降准降息常已同步 price-in；主题/制度类扩散慢且对宽基弱。
- 一句**催化温度计**收尾：本窗口催化整体偏 强正向 / 弱正向 / 中性 / 弱负向 / 强负向（= 下面 Layer 5 的 `catalyst_stance`，此处先给口径，frontmatter 再落字段）。

示例（仅示范格式，不是固定模板，bot 按当日实捞内容写）：
- `AP202409271640066814`（政治局会议通稿解读）：D 类定调，利多；首入"稳住楼市股市"+"超常规逆周期"=边际新提法，**onset**，领先信号。
- `AP202410031640162380`：同一 924 政策包的 confirmation——已写"全球最热门话题/轮番上涨"，指数主升已走大半，**不作新的加仓扳机**，只作背景。
- 安静期示例：本窗口无 regime 级催化（哨兵 research top score=0.22，低于 0.5 门控）；催化温度计=中性。
```

---

### （B）插入到「数据采集」章节，新增子节

```markdown
### 催化剂采集流程（预算内可执行）

硬约束：world 内 `web_search`/`web_fetch` 被 deny，催化剂**只能走 research-mcp 的 `research_search` / `research_view`**（因此天然 PIT-clean，无外网泄漏风险）。`end_date` 必须 ≤ trade_date，否则报错。INFOCODE 带 YYYYMMDD 前缀，可自查时点有没有泄漏未来。

预算纪律：12 轮 / 900k token 的瓶颈是**轮次不是 token**。催化剂维度**默认 1 次哨兵，至多再 1 次定向深挖**，把剩余轮次留给仓位决策。不要为这一维烧掉 3+ 轮。

**第 1 次（必打）— 哨兵温度计**，判有无 regime 级催化：
```
research_search(
  query = "A股 策略 政策 货币 财政 监管 拐点",
  search_type = "research",          # 券商研报是 regime 判断主力 channel，多券商对同一事件出报告→高分共识簇，天然抑噪
  simulated_datetime = trade_date+" 09:00:00",
  start_date = trade_date - 30 自然日,
  end_date   = trade_date - 1 自然日,  # 必须 ≤ T
  top_k = 6                            # 甜点；upstream 固定 fetch 10，前 6 条已覆盖多券商共识
)
```
读 **top score 量级**做硬门控（这是最强的有无催化判别信号）：
- top ≥ 0.85 且前 3 条成簇（按 INFOCODE 日期挤在同一两天）→ **有 regime 级催化**，进第 2 次定向深挖。
- top ≤ 0.30 → **安静期**，无新催化，催化温度计直接写中性，**不打第 2 次**，省轮次。
- 0.4~0.7 → 相关但非新催化，或 query 偏模糊；写"边际信号，非 regime 级"。

**第 2 次（按需，仅哨兵触发时）— 定向深挖**，按哨兵命中的类别从 query 模板库选 1 条精确 query：
- 货币/流动性："货币政策 降准 降息 SFISF 互换便利 回购增持再贷款 资本市场"（`search_type=research`）
- 外围/地缘冲击："对等关税 美国 加征 A股 冲击 全球市场"（重大冲击用 `search_type=all`：实时拿 news 时效 + research 深度；注意 all 模式 `data.items` 是按类型分组的 dict，非扁平 list）
- 政治局/中央经济工作会议："政治局会议 定调 稳住楼市股市 超常规逆周期"（`search_type=research`）
- 维稳/平准："中央汇金 增持 ETF 平准基金 入场 托底 资本市场"（必须收窄到 ETF/平准/A股，否则被无关增持新闻污染；`search_type=news` 拿 onset 时效）

query 措辞铁律（实测精确 vs 模糊 top score 差 ~0.5 且模糊 query 串味）：**名词堆叠 + 具体政策工具词 + "A股/资本市场/央行" 锚定**；禁用"怎么看/后续/影响"虚词。外围 query 必须放"中国/央行/A股"压制美联储宏观月报串味。

**可选第 3 次（一般别打，预算紧时跳过）— 机构确认**：已确认催化后，想知道机构是择机加仓还是守势，打 `search_type=managerview, query="<催化关键词> 加仓 减仓 操作 A股 观点"`。score 天然低（0.10~0.24，是观点非事件），只用来确认/证伪，不是发现催化的入口。

宏观数据催化**不走 research_search**，走 `get_cn_macro_data` / macro_nowcasting（PIT）；且宏观数据是滞后确认，不作独立择时信号。
```

---

### （C）插入到 Layer 5「择时口径」，新增 catalyst_stance 及联动框架

```markdown
### catalyst_stance（第七维的离散输出）+ 如何微调 timing_stance

`catalyst_stance` ∈ { **强正向 / 弱正向 / 中性 / 弱负向 / 强负向** }，写进 frontmatter（字段名 `catalyst_stance`），并在 `### 催化剂` 正文用一句话给出。判定口径：

| catalyst_stance | 典型情形 |
|---|---|
| 强正向 | 货币流动性结构工具 / 政治局超预期新提法 / 平准资金入场的 **onset**，多券商高分共识簇（top≥0.85） |
| 弱正向 | 利多催化但已 confirmation / price-in，或产业-制度类对宽基偏弱的利好 |
| 中性 | 哨兵安静期（top≤0.3），或催化方向不明 / 互相对冲 |
| 弱负向 | 边际收紧信号、外围扰动但未成冲击、利好兑现后的真空期 |
| 强负向 | 外围/地缘冲击 onset（对等关税行政令、地缘黑天鹅），双向 risk_off 扳机 |

**它如何作用于 timing_stance —— 是微调不是硬覆盖**：

`catalyst_stance` 不直接决定 `timing_stance`。先由六维证据 + regime 得出一个**基线 timing_stance**，再用催化剂维度**最多在相邻一档内微调**，并写出推理：

- 强正向 onset + 六维不矛盾 → 可把基线**上调一档**（如 hold→add_on_pullback，add_on_pullback→aggressively_add）。但若该催化是 **confirmation/已 price-in**，不上调，只在叙事里记"利好已反映"。
- 弱正向 / 中性 → 不动 timing_stance，仅作背景。
- 强负向（外围冲击 onset）→ 可把基线**下调一档甚至直接 risk_off**，**这是催化剂维度唯一允许较强干预的方向**（保命优先）。但要记住实测规律：**外围冲击底往往是国内对冲政策包的起点**（已验证关税当天研报即预判降准降息对冲）——恐慌刷屏的顶端是博"政策对冲反弹"而非追杀，所以"冲击 onset 当晚减仓"与"恐慌顶端别追空"要分清。

**铁律**：催化剂只调 `timing_stance` 这个口径，**不输出任何大类资产比例**；比例由后续选品环节联动 timing_stance 推导。catalyst_stance 与基线的偏差超过一档时，必须在 `### 催化剂` 写明为什么（哪条 INFOCODE 支撑这么大的干预）。
```

---

### （D）诚实纪律落地（写进 `### 催化剂` 章节末尾的"读法约束"小段，或 SKILL.md「注意事项」）

```markdown
### 催化剂维度的读法约束（诚实纪律）

- **读方向不读结论**：研报有卖方 / 前瞻 / perma-bull 偏差。捞到"政策底确认/牛市启航/反转"先警惕——这往往是 confirmation（指数已涨 10~25%），不是 onset。读"发生了什么政策/什么边际变化"，不抄研报的涨跌结论。
- **防幻觉（最硬一条）**：每条催化必须有 INFOCODE 出处，且 INFOCODE 日期 ≤ trade_date。**没查到 INFOCODE 就不许写催化**，不许用记忆/常识补"最近应该有 XX 政策"。宁可写"本窗口无 regime 级催化"。
- **防 recency**：哨兵 query 窗口固定 [T-30d, T-1d]，不要因为"印象里上个月有大事"就把旧催化当新 onset；按 INFOCODE 日期聚类，只有挤在最近一两天的爆发簇才算新 onset。
- **onset vs confirmation 必判**：这是本维度全部 alpha 所在。同一政策包，onset 当晚 research 捞到=可择时；几天后"已涨 X%/确认"的 research=事后解释，不作新扳机。
- **只写判断框架，不写硬数字规则**：禁止写"冲击后 X 天反弹""政策落地后 N 天加仓"这类未经回测的拍脑袋数字。本维度全部以四问框架（类别/方向/onset-confirmation/lead-lag）定性输出。
```

---

### （E）与 quant_factor 的关系（写进「注意事项」或独立小节）

```markdown
### 催化剂维度 × quant_factor：叙事 alpha 与护栏的权衡

- **定位分工**：`quant_factor`（如 `market_retail_contrarian_15_90/20_90`）= **护栏 / 客观先验**，是回测过的、可编码成 IC 的第二意见；**催化剂维度 = 叙事 alpha**，捕捉 factor 永远编不出来的东西（"我们认为政策拐点正在形成"无法编码成 IC）。两者是互补，不是替代。
- **一致时**：catalyst 强正向 onset + factor 也偏多 → 信号共振，可较有底气上调 timing_stance。
- **冲突时（必须写出推理，不机械执行任一方）**：
  - factor 偏空 / 拥挤，但出现**强正向 onset 催化**（如平准资金入场、超预期定调）→ 催化剂提供 factor 看不到的"为什么这次不一样"。倾向相信 onset 催化可主导短期 timing，但**仓位上调要克制**（因为 factor 护栏在示警拥挤/反转风险），写明"以催化为主、以 factor 为减速带"。
  - factor 偏多，但出现**强负向外围冲击 onset** → 保命优先，催化剂的 risk_off 信号压过 factor 的乐观先验。
  - 催化只是 **confirmation/已 price-in**，而 factor 示警反转 → **听 factor**，因为利好已反映、追高风险正是 factor 在量化的东西。
- **判据口径**：催化越是 onset、越是直接给买股资金的结构工具、券商共识簇分数越高，越敢让它盖过 factor；催化越是主题/制度/已 price-in，越回退到 factor 护栏。**永远写出这一步的推理链**，不要让 bot 机械地"催化 > factor"或"factor > 催化"。
```


## [integration_notes]
"改哪些文件 / 怎么同步到多 bot：\n\n1) 主改文件（单一真相源）= `/home/rooot/agent_invest_lab/bots/<botN>/skills/market-context/SKILL.md`。每个 bot 各有一份独立副本（实测 `find` 出 100+ 份，含 worktree 镜像）。canonical 编辑入口建议以 bot101/102/103 这三份新 onboard 的为基准（git status 显示它们正在改），先在 bot102 落地验证，再机械同步到 bot1~bot20 / bot101~103。\n\n2) 必须同步改「正文 market_summary_md 硬约束」校验器——这是会卡住落库的硬门：\n   - 现状（SKILL.md L181）写死\"包含 Layer 3 的 6 个 H3\"。加入 `### 催化剂` 后要改成 **7 个 H3**：`### 趋势结构 / ### 市场宽度 / ### 量能 / ### 情绪 / ### 估值 / ### 宏观 / ### 催化剂`，一字不差。\n   - 落库校验器在 `tougu_md_to_db.process_bot()` 与 `fund_md_to_db.process_bot()`（cron 端 python），**SKILL.md 改了不够，校验器代码里的 H3 列表/计数也要同改**，否则 7 个 H3 会被旧校验当格式错误拒收。先 grep 出这两个 process_bot 里硬编码的 H3 列表再改。\n   - 「至少 N 个 A 股证据词」白名单（L182）建议追加催化剂证据词：`INFOCODE / 催化 / 政策 / 降准 / 降息 / 平准 / 关税 / 政治局`，让带出处的催化叙事不被当作\"缺 A 股证据\"。\n\n3) frontmatter schema 新增字段 `catalyst_stance`（强正向/弱正向/中性/弱负向/强负向）。frontmatter 解析器（同上两个 process_bot）若是白名单字段解析要把它加进去；若是宽松透传则无需改代码，只需 SKILL.md 文档化。建议 `catalyst_stance` 设为**选填**（默认中性），避免老 run / SKIP 范式的 bot 因缺字段报错。\n\n4) 多 bot 同步机制：仓库已有 `bots/common/agents_common.md` + \"bots/common sync\"（见近期 commit 374786c \"introduce bots/common sync\"）。第七维属于全 bot 通用能力，**正确做法是把这三块 MD（催化剂 H3 / 采集流程 / catalyst_stance 联动）放进 common 同步源**，由 sync 脚本铺到各 bot，而不是手工 100 份逐个粘——否则 worktree 镜像和后续新 bot 会漂移。query 模板库 + 分类学已是 Phase-1 资产，建议同样收进 common 的一个 `_shared_docs/catalyst/` 供 SKILL.md 引用，避免每份 SKILL.md 内联重复。\n\n5) 与 METHODOLOGY 的「方法论迭代机制」对齐：催化剂是定性维，update_my_strategy 这类策略迭代里，催化剂的\"四问框架/onset-confirmation 判据\"应归入**不变核（invariant）**（这是方法论不是参数），catalyst_stance 到 timing_stance 的微调档位可归**可变**。\n\n6) 落地顺序建议：先改 1 份（bot102）+ 2 个 process_bot 校验器 → 跑一个 48080 backtest-dashboard 的小 run（trading-rl-config.json 选 2~3 个 bot、含 924/关税这种已知催化日）验证 7-H3 能落库且 catalyst_stance 正确写入 fund_allocation_runs → 再 common-sync 全量铺开 → 用\"带催化剂 vs 不带\"的 A/B（dashboard 现成路由 /api/backtest/bot /benchmark）量化增量。"

## [risks]
[
  "confirmation 误判为 onset：四问框架是定性判据，依赖 bot 自己读 SUMMARY 里有没有'已涨/确认'。LLM 仍可能把高分 confirmation 簇当 onset 上调仓位，正好买在主升末端——这是本设计最大的残余风险，A/B 回测必须专门盯催化日后 5~10 日的回撤，而非只看命中率。",
  "幻觉防线靠'必须带 INFOCODE'，但 bot 可能编造一个看似合规的 INFOCODE（AP+8位日期+序列号格式很容易仿造）。校验器只能查格式不能查真伪。需要在落库侧或抽检侧做 INFOCODE 回查（拿 INFOCODE 反查 research_search 是否真存在），否则防幻觉是纸面的。",
  "哨兵 top score 门控阈值（0.85/0.30/0.5）是 Phase-1 少数样本（924/关税/2025-06-20安静期）归纳的经验值，不是回测出来的硬阈值——写进 SKILL.md 时已尽量措辞为'判别信号'而非铁律，但 bot 仍可能把它当硬规则机械执行，碰到 query 措辞略差导致 score 偏移时误门控。属于'软阈值被当硬数字用'的老坑变种。",
  "catalyst_stance→timing_stance 的'最多微调一档'本身就是一条软规则，强负向允许直接跳到 risk_off 又是例外，规则有内在张力。bot 可能要么过度保守（强正向 onset 也只敢调一档，吃不到主升），要么在'强负向例外'上过度 risk_off（恐慌顶端追空，正是设计里警告的反模式）。",
  "预算纪律（1 哨兵+1 深挖）在真有连环催化的大事件窗口（如 924 后连续政策包）可能不够，bot 为省轮次漏掉第二天的增量 onset；反之安静期 bot 也可能不甘心、硬打第 2~3 次浪费轮次。轮次自律完全靠 prompt 约束，无硬上限保护。",
  "100+ 份 SKILL.md + 两个 process_bot 校验器 + common-sync 三处必须同步改 H3 计数与 catalyst_stance 字段，任一处漏改都会导致落库被拒或字段丢失。多副本漂移（worktree 镜像、新 bot）是已知脆弱点，手工铺开几乎必然漏。",
  "research 研报的 perma-bull / 卖方偏差是系统性的：'读方向不读结论'是纪律但无法在校验层强制，强正向催化在牛市叙事下会被券商集体放大，catalyst_stance 可能系统性偏正向，缺一个对称的'利空催化'供给（除外围冲击外，研报很少主动喊政策转向收紧），导致第七维对下行 regime 的预警弱于对上行的助推。",
  "managerview/news 的串味与污染（'汇金增持'被无关新闻挤占、外围 query 混入美联储宏观月报）已知但只能靠 query 收窄缓解，不能根除；窄 query 又有漏召回风险（真催化用了非模板措辞就捞不到）。召回与精度的权衡没有回测背书的最优点。"
]