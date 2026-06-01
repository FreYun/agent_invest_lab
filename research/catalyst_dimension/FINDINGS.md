# 第七维「催化剂/政策面」研究结论（agent 投资，非 quant）

> 给 market-context 技能加第七维：让 100+ agent bot 主动检索研报/资讯，判断当前有无 regime 级催化，输出**带 INFOCODE 出处**的定性推理。这是 factor 永远做不到的——"我们认为政策拐点正在形成"无法编码成 IC。
> 本文是顶层结论；五路 deep-dive 原料在 [`_raw/`](_raw/)（数据审计/分类学/技能设计/A·B验证/对抗审查），全部 INFOCODE/score 证据均为实测、已确认 PIT-clean。
> 日期：2026-06-01。研究方式：5-agent workflow（catalyst-dimension-design），每路自跑 curl 取证。

---

## 1. 一句话裁定：🟡 黄灯

**值得建，但必须从「alpha 引擎」重定位为「事件哨兵 / 风险闸」，且强制三道闸；缺第 2 闸（发现层共享、响应层分化）即转红。**

- 不是因为它是强 alpha 源——实测它在**顶部会反向带节奏**、在安静期冗余、在组合层同质化。
- 而是因为它在**两个其它六维结构性给不了的窄场景**里有真领先量：政策包 onset 当晚、外围冲击 onset。

---

## 2. 为什么是 agent 专属、factor 做不到（924 实测）

- factor 能编码的是"已发生、可量化的状态"（量/价/估值/动量/宽度）。924 当晚（2024-09-24）这些**还没反应**：情绪要等次日放量、宏观要等社融/M2 滞后 ~1 月。所以 T=924 当晚六维全是"磨底/缩量/估值低"的旧状态。
- catalyst 维度当晚就能读出"发生了什么"：实测 T=2024-09-24 18:00，research 返回 6 条全是降准降息组合拳，`AP202409241640006953`(score 0.986)写明"降准0.5%、降7天逆回购0.2%"，叠加 SFISF 5000亿 + 回购增持再贷款3000亿 + 研究平准基金。
- 这段"政策拐点正在形成"的判断，**证据是政策工具本身（SFISF/再贷款），不是价格**——而价格此刻尚未启动主升。任何 IC 都编码不了它。
- **PIT-clean 反证**：T 退到 2024-09-23（924 前一日）同哨兵 query，top 仅 0.58，全是陈旧内容，**零 924 泄漏**；退到 2024-09-20 只返回 9/19 前内容。真领先，非回看偏差。

---

## 3. 数据地基（[详见 _raw/01](_raw/01_data_pit_audit.md)）

- **后端**：`https://research.tiantianfunds.com.cn/strategy`，两件套 `research_search`(news/research/managerview/all) + `research_view`(sector/weekly/fund_related)。`end_date` 必须 ≤ T，INFOCODE 带 YYYYMMDD 前缀可自查时点。world 内 web_search/web_fetch 被 deny → 催化剂只能走 research_*，**天然无外网泄漏**。
- **历史深度** ≥ 回到 2024（OOS ≈ 2.4 年），但**真 regime 级事件是个位数**（924 / 2024-12 政治局 / 2025-04 关税）——这是 A/B 统计显著性的硬约束（见 §6）。

**channel 分层（实测信噪比）**：

| channel | 定位 | 信噪比 |
|---|---|---|
| **research 券商研报** | **regime 判断主力** | 最高。催化时多券商同事件成高分共识簇（924=0.90–0.97，关税=0.98–0.99）；安静期 top≤0.25–0.38 |
| **news 资讯** | 仅补 onset 当天原始口径 / **利空 risk_off 唯一可信源** | 噪声主体=每日盘面复盘+资金流水；危机利空 onset 走它 |
| **managerview** | 软信号，确认/分歧 | score 低（0.10–0.24，是观点非事件） |
| **research_view/sector** | 产业政策底色 | 月度结构化观点，不捕突发催化 |

**score 三态门控（核心方法论，但是软阈值）**：top≥0.85 且成簇 = regime 级催化；0.4–0.7 = 相关但非新催化；≤0.3 = 安静期噪声底。
⚠️ 这几个阈值是少样本归纳的**判别信号、非回测硬阈值**；更关键的是 **score 无法区分 onset 与顶部 late-confirmation**（见 §5 第 2 条）。

**query 铁律**：名词堆叠 + 具体政策工具词 + "A股/资本市场/央行"锚定；禁"怎么看/后续/影响"虚词（实测精确 vs 模糊 top score 差 ~0.5）。模板库见 _raw/01 §2。

---

## 4. 催化剂分类学（[详见 _raw/02](_raw/02_catalyst_taxonomy.md)）

核心纪律：**区分 onset（落地当天，可择时）vs confirmation（研报喊"政策底确认"，指数常已大涨，事后解释）。读方向不读结论。**

| 类别 | lead/lag（对宽基） | price-in 风险 | 择时价值 |
|---|---|---|---|
| **A 货币/流动性**（SFISF/再贷款/适度宽松） | 领先→同步 | 极高（confirmation 端） | **高** |
| **B 维稳/平准资金**（汇金增持ETF/平准） | 同步→略领先 | 中（辟谣噪声） | **高** |
| **C 外围/地缘冲击**（对等关税/美联储） | 领先（利空），A股滞后海外~1日 | 低(onset)/高(追跌) | **高（双向 risk_off）** |
| **D 政治局/中央经济工作会议定调** | 领先（基调，读边际新提法） | 中（预期差才是 alpha） | **高** |
| E 产业级国家战略 | 对宽基弱/对成长领先 | 高 | 中 |
| F 资本市场制度/供给 | 滞后→同步（慢变量） | 中 | 中→低 |
| G 宏观数据发布（走 macro_* 非 research） | 滞后确认 | 高 | 低（仅交叉验证） |

**前四类 A/B/C/D 才有 regime 级宽基择时价值；E/F/G 读但不作宽基加减仓扳机。** C 类是少数能让 agent 主动 risk_off 的利空催化。

---

## 5. 三个真风险（[详见 _raw/05 对抗审查](_raw/05_failure_modes.md)）—— 不粉饰

### 🔴 高危 1 · 卖方 perma-bull 在顶部反向带节奏
实测 **T=2024-10-08 09:00（节后第一天，前面 +25%，当天就要 −7% 大反转）**，哨兵 query 返回清一色看多确认：0.75「政策信号明确提振信心」、0.69「全A收涨15.13%」、0.50「增配中国资产成全球最热门话题」。**在最该 risk_off 的时刻，research 通道给最强 add 信号。** 这是与 factor 相反的失败模式（factor 崩盘时数值会恶化，叙事维度逆向恶化）。"读方向不读结论"救不了——方向就是"政策强情绪热"，没错，但它恰是反向指标。
> 缓解：(1) **利空/risk_off 只信 news 事件 onset，禁用 research 策略观点做 risk_off**（卖方不替你喊顶）；(2) **涨幅闸**——高分政策催化但近 N 日已大涨 → 只许 hold 不许 aggressively_add；(3) 读簇的存在性、不读簇的情绪色彩。**残余风险仍偏高。**

### 🔴 高危 2 · 100+ bot 同质化羊群
共用同一 KB + query 模板 + 判别规则 → 催化剂是最"响"的维度（有事件/出处/强 score），易盖过个性化六维解读，把用户最看重的人设多样性抹平，并把单一 timing 信号放大成 100 倍同步下注。
> 缓解（最关键设计约束）：**发现层共享、响应层分化**——第七维只输出客观层「事件 + INFOCODE + onset/confirmation 标签」，**把"据此加/减"裁决交还各 bot 人设**（激进 bot 见 924 onset 可 aggressively_add，保守 bot 同 onset 只 hold）。A/B 必须监控 bot 间日收益相关性，相关性显著上升即判负。发现层收敛无法根除，只能把伤害锁在发现层。

### 🟡 中危 3 · onset/confirmation 混淆 + 安静期冗余 + 过度交易
score 无法区分"924 当晚真领先"与"顶部卖方大合唱"；安静期（实测 2025-06-20 top=0.38）第七维与 ###情绪/###宏观 增量≈0 但每天付 query 成本。
> 缓解：onset/confirmation 写成**硬读数规则**（INFOCODE 日期=T/T-1 为 onset vs 散布 T-5~T-10 为 confirmation；SUMMARY 含"已涨X%/政策底确认"即降级）；**硬 score 门控** top<0.5 强制"无催化、维度静默"；限定作用域到 regime 切换、禁下沉到日内微调；定位成"平时静默、事件日发声"的哨兵。

---

## 6. 48080 A/B 验证方案（[详见 _raw/04](_raw/04_validation_ab.md)）

- **对照设计**：同一 bot × 两版 SKILL.md（C=六维 / X=七维，逐字只追加一节）× 两个串行 run。shadow 是复制，源文件切换即可隔离。唯一变量 = market-context/SKILL.md 版本。48080 `/api/backtest/bot` 原生支持同 bot 多 run 并排比。
- **4 窗口（缺一不可，杜绝 cherry-pick）**：
  1. 924 onset（2024-09-23→10-11）— X 应赢，测有无正 alpha
  2. confirmation 确认期（2024-10-14→11-08）— X 可能输，测会不会被 perma-bull 骗去追高
  3. 关税 crisis（2025-04-02→04-30）— 测 ΔMaxDD，能否更早 risk_off
  4. 磨人震荡（2025-05-21→06-20）— 无害性测试，X 不能 overtrade
- **指标**：ΔSharpe/ΔCalmar 配对差分（主）、crisis 段 ΔMaxDD、ΔTurnover/ΔTradeCount（防 headline-chasing）、跨人设符号一致率、**bot 间日收益相关性**（防同质化）。
- **混淆控制**：temperature=0.3 无 seed → N≥3 人设 × R≥3 重复 + 配对 Wilcoxon；两 arm 对称禁 update_my_strategy（防方法论漂移）；审计 bot_reason 的 INFOCODE 引用率（防"读催化"实为"看价格"伪相关）。
- **go/no-go（诚实）**：**「赢得多」不够，必须「不在该输的地方乱输」**——onset 显著正 + confirmation 不显著负 + crisis 回撤不更差 + 震荡不 overtrade + 跨人设方向一致 + 归因证据 ≥50% 引用 INFOCODE。任一翻车 → no-go 或仅局部启用。
- **样本稀疏诚实**：regime 级事件个位数 → A/B 几乎不可能有全样本统计显著性。**预先承诺**主指标 = 危机段回撤 + 事件日逐案复盘，附组合相关性约束，**接受 null**（参考 VIX/regime 因子三关全挂即排除的诚实文化）。

---

## 7. 分阶段落地（建议）

1. **写 X 版 SKILL.md**：在 1 份 bot（如 bot102）的 market-context 追加「### 催化剂」节（含四问框架 + 采集流程 + catalyst_stance→timing_stance 微调 + 诚实纪律），schema 不变只追加一节。可粘贴 MD 见 [_raw/03](_raw/03_skill_design.md)。
2. **改落库校验器**：H3 计数 6→7（`### 催化剂` 一字不差）+ A股证据词白名单加 INFOCODE/催化/降准/平准/关税/政治局 + frontmatter 加选填 `catalyst_stance`。校验器在 `tougu_md_to_db.process_bot()` / `fund_md_to_db.process_bot()`，**SKILL.md 改了不够，校验器代码的 H3 列表也要同改**否则落库被拒。
3. **跑最小 A/B**：N=3 人设 × 4 窗口 × R=3，先在 48080 上量化"带 vs 不带"。**重点盯三道闸**：顶部不追高、危机段回撤、bot 间相关性不上升。
4. **接受 null 或推广**：三道闸全过且归因证据成立 → common-sync 铺全体；任一闸（尤其同质化）翻车 → 接受 null 或仅 onset/crisis 段局部启用。

> 关键判断：这维度的价值**稀疏但真实**（集中在每年个位数事件日）。别当每日 alpha 源，当"平时静默、事件日发声、且只在发现层共享"的风险哨兵。
