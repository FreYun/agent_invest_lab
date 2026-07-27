# Allocation Charter（配置宪章）机制设计

日期：2026-07-27 ｜ 分支：feat/world-system ｜ 状态：已与用户对齐待实现

## 1. 背景与问题

bot105d 日度回测 run（`bot105d-daily-agenticdeep-20260723`）出现行为退化：2026-01 起连续 7 个月所有交易只剩 000051（华夏沪深300联接），卫星持仓（007818/014881）11 个月未被重新评估。多基金大类配置 bot 退化成沪深300 单基金择时器。

根因诊断（对比同 bot 的 weekly run 未退化，确认为日度管线的结构性动力学，非偶发）：

1. **记忆压缩单向棘轮**：`world/src/history-window/compact.ts` 的四维度压缩把每次亏损沉淀成只增不减的硬规则，约束空间单调收缩；
2. **风险框架单维化**：行动卡只输出一个总仓位目标，年线/PE/ERP/温度全部锚定 000300.SH，问题被降维成单指数择时；
3. **摩擦不对称**：调核心宽基零摩擦（长持免赎回费、免深研准入），动卫星处处闸门，理性解永远是只动 000051。

已验证 prompt 层劝导无效（daily prompt 中"别再只看宽基"的告诫被记忆铁律压倒）。

## 2. 目标与非目标

**目标**：保留宽基择时自由（这被证明有效：该 run +25.5%/-6.5%maxDD/Sharpe1.56），同时系统侧硬性保证卫星/主题配置真实存在且被周期性评估轮动。

**非目标**（本期不做）：
- 不改记忆压缩机制（compact.ts 规则退休等留作后续独立实验，保持单变量验证）；
- 不做行动卡整体 schema 结构化（方案 C）；
- 不影响单基金 bot（bot1~20）与多资产 bot（bot101~103）现有行为——宪章仅对多基金类 bot 启用。

## 3. 总体方案

**宪章机制**：约束数值不由系统统一规定，由 bot 依据自己的 IDENTITY/METHODOLOGY 自声明；系统只校验类别底线；一经生效由交易层物理执行。"bot 自己的承诺变成物理法则"。

三个组件：宪章生命周期（world 调度器）、交易结构硬闸门（fund-portfolio-mcp）、卫星复评验收（fund-portfolio-mcp 新工具 + world 到期提示）。

## 4. 宪章数据模型与生命周期

宪章为结构化 JSON（示例值为 bot105d 记忆中自己沉淀的架构）：

```yaml
core_fund_codes: ["000051", ...]     # 池内哪些算「核心宽基」，其余自动归卫星
single_fund_max_ratio: 0.60          # 单基 ≤ 权益市值 60%（类别底线 ≤ 0.75）
satellite_min_ratio: 0.25            # 卫星桶 ≥ 权益市值 25%（类别底线 ≥ 0.15）
min_equity_threshold: 0.30           # 权益/总资产 < 30% 时结构约束豁免（防御态不强拆）
satellite_review_cadence_days: 5     # 卫星复评周期，交易日（类别底线 ≤ 10）
```

关键设计点：

- **所有比例相对权益市值而非总资产**——宽基择时（总仓位 0%~85%）完全自由，约束只作用于权益内部结构。
- **生命周期**：run 启动时调度器向 bot 下发一次性声明任务（依据其 METHODOLOGY/IDENTITY 产出宪章）→ 调度器校验类别底线 → 写入 run 工作区与 fund.db 生效。
- **修订**：仅深研日允许带理由修订，同样过底线校验；两次修订间隔 ≥ 20 个交易日（防棘轮反复收紧）。
- **类别底线**（多基金类，写死在系统侧）：`single_fund_max_ratio ≤ 0.75`、`satellite_min_ratio ≥ 0.15`、`satellite_review_cadence_days ≤ 10`。

> **实现注记（2026-07-27）**：修订不再限定"深研日"——daily-agenticdeep 管线的 deep research 为 agent-triggered（bot 会话内自触发），调度器无法预先标定深研日。改为仅以 20 交易日冷却 + reason 必填约束修订频率。防棘轮效果等价（冷却是主要约束），实现大幅简化。

## 5. 交易层硬闸门

执行点：**fund-portfolio-mcp 服务端**（不放 world 的 fund-portfolio-proxy——proxy 是薄注入层；集中度校验需查当前持仓，server 端有 DB）。复用现有"池外拒单"反馈路径（bot 已被证明能对结构化报错自我修正）。

world 在 run 初始化时经 `cli_tools.py` 把宪章写入账户配置。`portfolio_place_buy_order` 校验**成交后的模拟结构**（用 reference_nav 估算，T+1 结算口径）：

| 场景 | 行为 |
|---|---|
| 买核心 → 单基占权益比将超上限 | 拒单，报错含当前结构（"单基将达 68%>60%，请先/同时配置卫星仓"） |
| 权益 ≥ 豁免线 且 卫星桶已低于下限，继续买核心 | 拒单 |
| 卖单（任何方向） | **不拦**——择时降仓/风控动作永远放行 |
| 卖卫星致破下限后 | 不拦当笔，但后续核心买单被闸，形成"想加核心先修结构"闭环 |

容忍带 ±5pp，防止 NAV 日波动造成合规/违规抖动死锁。

## 6. 卫星复评验收

新增 bot-only MCP 工具 `portfolio_submit_satellite_review`，接收结构化 JSON：每只卫星持仓 keep/rotate/exit 结论 + ≥2 只池内候选对比 + 主线依据。server 端 schema 校验，落 DB 可追溯。

节奏与牙齿：

- 到期日（按宪章 cadence）当天 daily prompt 由 world 注入"今日卫星复评到期"提示块；
- 复评过期超过 cadence+2 交易日 → `portfolio_place_buy_order` 对**核心基金**拒单（"卫星复评过期，先提交复评"）；卖单与卫星买单不受影响；
- 选 MCP 工具而非解析 MD 产出物：schema 校验 100% 机械、与现有拒单反馈路径同构；
- **义务范围**：仅当存在至少一只卫星持仓时复评义务生效；空卫星仓（如权益低于豁免线的防御态）不欠复评，但此时买核心仍受 §5 结构闸门约束。

## 7. 改动面

| 部件 | 改动 |
|---|---|
| `fund-portfolio-mcp/server.py` + `db.py` | 宪章表、结构校验、`submit_satellite_review` 工具、拒单逻辑；配套 `test_*.py` 单测 |
| `fund-portfolio-mcp/cli_tools.py` | run 初始化写宪章 |
| `world/main.ts` / `src/run.ts` | 启动时宪章声明任务、到期日提示注入、深研日修订通道 |
| `world/src/message.ts` | 复评到期提示块 |
| bot 工作区（bots/bot105d 等） | METHODOLOGY 附宪章声明说明（轻量） |

## 8. 验证

1. fund-portfolio-mcp 单测：拒单矩阵（超上限/破下限/豁免线/容忍带/复评过期）、宪章底线校验、修订冷却；
2. 10 天 smoke run（`world-bot105d-smoke10d-agenticdeep.yaml` 启用宪章）验证端到端：声明→写库→拒单反馈→复评落库；
3. 重跑 daily r2，验收指标：
   - **每月 distinct fund_code 数曲线不再衰减到 1**（本次退化的直接量化指标）；
   - 卫星复评按 cadence 落库、无长期过期；
   - 绩效不显著劣化（对照本次 run 的 +25.5%/-6.5%maxDD 基线）。

## 9. 风险与开放问题

- **合规躺平残余**：宪章保证卫星有权重、复评有节奏，但不能保证复评质量（bot 可能敷衍 keep）。缓解：复评 JSON 落库后可离线审计；若普遍敷衍，后续再加 trace 校验（要求调过 sector_*/get_fund_detail）。
- **拒单循环**：bot 可能反复撞闸。缓解：报错信息给出明确修复路径；smoke run 观察撞闸次数。
- **宪章声明质量**：bot 首日声明可能偏离人设。缓解：底线校验 + 声明任务 prompt 要求引用自己 METHODOLOGY 的配置区间。
