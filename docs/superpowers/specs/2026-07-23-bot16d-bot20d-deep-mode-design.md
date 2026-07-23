# bot16d / bot20d 深度研究能力复刻 — 设计

- **日期**: 2026-07-23
- **状态**: 设计已批准，待写实现计划
- **目标读者**: 后续实现者（可能是我自己的后续会话）

## 一、背景与目标

现有 `bot105d` 是 `bot105` 的「深度研究版」：同样的多指数配置任务，但 agent 具备一套深度研究能力（research-loop 引擎驱动的 SCOUT→DIG→CHALLENGE→SYNTHESIZE→OUTPUT 状态机 + 选题算法 + 研究记分牌 + 行动卡）。

本设计要把这套深度研究能力**复刻到 bot16 和 bot20 上**，产出 `bot16d`、`bot20d` 两个新 agent，与现有 live 版 bot16/bot20 物理隔离。

外加一个引擎侧新功能：**崩盘阈值触发深度研究**——当基准指数出现单日急跌/急涨或深回撤时，系统强制当日开启深度研究，不依赖 agent 自主判断。

## 二、核心认知（解耦模型）

回测系统里三件事是**正交**的，不能再像早期版本那样绑死：

1. **跑什么指数 / 什么方法论** = 由 world config 的 `strategy_id` 决定（config 创建时点选择）。shadow-workspace（`world/src/run.ts:312`）用 `renderActiveMethodology()`（`world/src/strategy-library.ts:138`）从策略库读 `METHODOLOGY.md`，与 agent 无关。
2. **agent 是谁（性格 / 风险偏好）** = 由 bot 真实工作区的 `USER.md` / `IDENTITY.md` / `SOUL.md` / `AGENTS.md` 决定。
3. **是否具备深度研究能力** = 由「config/引擎接线」+「AGENTS.md 里的研究骨架」共同决定，**与跑哪个指数无关**。

已核实：深度模式的所有开关都不依赖策略文件——

- 引擎二进制 `research_loop_rust_bin`、调度参数 `deep_research_mode` / `deep_research_every` / `deep_research_max_gap_days`、`start_research` 工具放行（rl config deny-list）→ 全在 config / rl config；
- `world/src/message.ts` 的深研提示块由 config 的 deep 开关驱动；
- 「怎么做研究」（选题算法 / 研究记分牌 / 行动卡 / 每日 step 0）→ 在 `AGENTS.md`。

**结论**：不需要 per-index 的深度策略文件。`bot105d` 恰好搭配 `multi_equity_high_v2` 策略，但那是「更好的方法论」这个独立改进，不是深度模式的必要条件。跑哪个指数（`012323`/医疗、`011609`/双创，或其它）是 config 创建时的旋钮。

## 三、交付物总览（4 块）

| # | 交付物 | 来源 | 动作 |
|---|--------|------|------|
| 1 | `bots/bot16d`、`bots/bot20d` agent 工作区 | `bots/bot16`、`bots/bot20`（性格）+ `bots/bot105d`（深研骨架） | **新建** |
| 2 | 深度模式接线（引擎 + rl config） | `vendor/research-loop-105d`、`trading-rl-config-rsloop-105d.json` | **直接复用** |
| 3 | 崩盘阈值触发（新功能，指数无关） | — | **新代码**：`config.ts` + `daily-context.ts` + `run.ts` |
| 4 | 模板 world config | `world-bot105d-daily-agenticdeep-2025-01-2026-07.yaml` | **复制改**（示例填 bot16/bot20 当前策略，`strategy_id` 可换） |

**不做**（YAGNI）：不新建 `medical-deep.md` / `shuangchuang-deep.md` 等深度策略文件；不改策略库；不动 live bot16/bot20。

## 四、详细设计

### 4.1 交付物 #1 — agent 工作区 bot16d / bot20d

每个新工作区 `bots/bot16d`、`bots/bot20d` 含：

- **`USER.md` / `IDENTITY.md` / `SOUL.md`**：分别拷自 `bots/bot16`、`bots/bot20`（保留各自性格与风险偏好，如 bot20「风险极高、机会导向、容忍 -20%」）。
- **`AGENTS.md`**：以 bot16/bot20 现有的 COMMON 块为基底，**追加** `bots/bot105d/AGENTS.md` 的深研骨架小节：
  - `## 深度研究（仅深度研究日）`（约 105d 的 93-100 行）
  - `### 选题算法`、`### 研究记分牌`、`### 全文消化`、`### 行动卡`、`### 研究维度权重矩阵`（101-141 行）
  - `## 每日执行顺序` 里的 step 0（深研日先做研究）（142-155 行）
  - 高风险/进攻口径按 bot 性格保留或裁剪，不照搬 105d 的「多指数比较维度」（bot16/bot20 是单指数择时，不是多指数配置）。
- **`config/research-loop.yaml`**：指向 qwen3.6-plus 端点（`https://dd-ai-api.eastmoney.com/v1`，`api_key_from_openclaw: zai-coding-plan`），结构比照 `bots/bot105d/config/research-loop.yaml`。
- **`memory/` / `skills/` / `TOOLS.md` / `METHODOLOGY.md` / `MEMORY.md`**：按 bot105d 的最小结构复制骨架（`METHODOLOGY.md` 在 shadow-workspace 会被策略覆盖，真实工作区里保留占位即可）。

> 注意：bot16/bot20 是**单指数择时** bot（买池单只基金），105d 是多指数配置。搬深研骨架时，凡是「多指数横向比较 / 主线切换」的措辞要改成「对单一指数的多维深研」，避免语义错位。这是 AGENTS.md 里唯一需要人工改写、不能机械复制的部分。

### 4.2 交付物 #2 — 深度模式接线（复用）

- **引擎**：`research_loop_rust_bin: /home/rooot/agent_invest_lab/vendor/research-loop-105d/rust/target/release/research-loop-rust2`（bot 无关，直接复用）。
- **rl config**：`rl_config_base: trading-rl-config-rsloop-105d.json`（deny-list 已放行 `start_research`；预算已标定：max_total_turns 32、token_budget 3.5M 等）。直接复用，不复制。

### 4.3 交付物 #3 — 崩盘阈值触发（唯一引擎改动）

**语义**：在 agent-triggered 深研调度之上，追加一条系统强制条件——基准指数出现「单日 |涨跌| ≥ 阈值」或「距近高回撤 ≥ 阈值」时，当日**强制**深度研究（等同 `forced=true`），不管 agent 是否自愿、也不管距上次深研的 gap。

**4.3.1 `world/src/config.ts` — 新增 4 字段**

在 deep 字段区（现 43-55 行附近）加：

```ts
crashTriggerEnabled?: boolean            // 默认 false（不开则完全等价现有行为）
crashTriggerDailyMovePct?: number        // 默认 3   —— |单日涨跌%| ≥ 此值触发
crashTriggerDrawdownPct?: number         // 默认 8   —— 距近高回撤 ≥ 此值触发（正数，内部按 <= -值 比较）
crashTriggerBenchmark?: { code: string; name: string }  // 默认 { code: '000300.SH', name: '沪深300' }
```

解析（现 249-253 行附近）比照现有 deep 字段的健壮性写法（类型检查 + 下限约束），并把 4 个字段加进 `parseConfig` 末尾的 return 对象（现 389 行）。

**4.3.2 `world/src/daily-context.ts` — 抽 `fetchBenchmarkDailyState()`**

复用现成的 `fetchIndexBenchmark`（698 行）同一条 `market_index_quote` 调用路径，新增一个轻量函数只返回崩盘判定所需的两个标量：

```ts
export interface BenchmarkDailyState {
  lastDate: string
  lastDayMovePct: number | null            // (close[n]-close[n-1])/close[n-1]*100
  drawdownFromRecentHighPct: number | null // <=0；复用 maxDrawdownPct(649 行) over 窗口
}
export async function fetchBenchmarkDailyState(opts: {
  simworldUrl: string; code: string; asOfDate: string; lookbackDays?: number
}): Promise<BenchmarkDailyState | null>
```

- 拉 `[asOfDate - lookbackDays, asOfDate-1]`（`lookbackDays` 默认 ~60，够算「近高」；`end_date = priorDay(asOfDate)` 与现有一致，保持 PIT）。
- `lastDayMovePct` = 最后两个收盘的日涨跌。
- `drawdownFromRecentHighPct` = `maxDrawdownPct(closes)`（已存在的函数，649 行）。
- best-effort：任何异常返回 `null`。

**4.3.3 `world/src/run.ts` — 日级触发 + 判定**

现在 `computeDeepResearchState`（928 行）签名与判定扩展：

```ts
export interface DeepResearchStateInput {
  // ...现有字段...
  crashSignal?: {
    dailyMovePct: number | null
    drawdownPct: number | null      // <=0
    dailyMoveThreshold: number      // 如 3
    drawdownThreshold: number       // 如 8（正数）
  }
}
```

判定里，先算 `crashForced`：

```ts
function isCrashForced(s?: DeepResearchStateInput['crashSignal']): boolean {
  if (!s) return false
  const moveHit = s.dailyMovePct != null && Math.abs(s.dailyMovePct) >= s.dailyMoveThreshold
  const ddHit   = s.drawdownPct  != null && s.drawdownPct <= -s.drawdownThreshold
  return moveHit || ddHit
}
```

然后：
- `agent-triggered` 分支：`forced = (gapDays >= maxGapDays) || crashForced`；`authorized` 恒 true。
- `ordinal` 分支：`forced = isDeepResearchDay(...) || crashForced`；`authorized = forced || 原 authorized`（崩盘日也要放行 `start_research`，否则强制了却没工具）。

调用点（现 1134 行，`mapWithConcurrency` **之前**，日级只调一次）：

```ts
let crashSignal: DeepResearchStateInput['crashSignal'] | undefined
if (config.crashTriggerEnabled) {
  const st = await fetchBenchmarkDailyState({
    simworldUrl: config.simworldUpstreamUrl,
    code: config.crashTriggerBenchmark.code,
    asOfDate: date,
  }).catch(() => null)
  if (st) crashSignal = {
    dailyMovePct: st.lastDayMovePct,
    drawdownPct: st.drawdownFromRecentHighPct,
    dailyMoveThreshold: config.crashTriggerDailyMovePct,
    drawdownThreshold: config.crashTriggerDrawdownPct,
  }
}
const drState = computeDeepResearchState({ /* ...现有... */, crashSignal })
```

- **每决策日一次**额外 `market_index_quote`，非每 bot（触发是市场级，与 bot 无关）。
- 日志 `tagBits`（1155 行）追加崩盘标记，如 `[deep-research#crash move=-3.4% dd=-8.2%]`，与现有 `#forced` / `#authorized` 并列，保证可追溯。
- 强制块用现有 `deepResearchBlock`（`message.ts:777`），bot 收到「今日强制深研」，措辞可点出诱因是市场条件（可选增强，非必需）。

**基准指数来源**：崩盘触发基准是 config 字段 `crash_trigger_benchmark`，默认沪深300。模板 config 里按各自目标指数填（bot16d = `399989.SZ`/中证医疗；bot20d = `931643.CSI`/双创）——单指数 bot 该用自己的指数回撤当诱因，这与 `medical.md`「深回撤体质，走出过七成量级回撤，择时纪律是这个指数能不能要的前提」、`shuangchuang.md`「熊市回撤可达 50%+」的自述风险直接对齐。

### 4.4 交付物 #4 — 模板 world config

复制 `world-bot105d-daily-agenticdeep-2025-01-2026-07.yaml` 到 `world-bot16d-*.yaml`、`world-bot20d-*.yaml`，改：

| 字段 | bot16d | bot20d |
|------|--------|--------|
| `bots` | `[bot16d]` | `[bot20d]` |
| `bot_assignments.<bot>.strategy_id` | `medical`（示例，可换） | `shuangchuang`（示例，可换） |
| `buyable_fund_codes` | `['012323']` | `['011609']` |
| `crash_trigger_benchmark` | `{code: '399989.SZ', name: 中证医疗}` | `{code: '931643.CSI', name: 双创}` |

公共字段：`research_loop_rust_bin`（105d 引擎）、`rl_config_base: trading-rl-config-rsloop-105d.json`、`deep_research_every: 0`、`deep_research_mode: agent-triggered`、`deep_research_max_gap_days: 5`、`deep_research_timeout_seconds: 3900`、`chat_step_mode: weekly`、`chat_step_days: 1`、`replay: 2025-01-01 → 2026-07-17`、`bot_models`（qwen3.6-plus / dd-ai-api / zai-coding-plan）、新增 `crash_trigger_enabled: true` + 阈值 3/8。

> `strategy_id` 与 `buyable_fund_codes` 是 config 创建时的旋钮；模板里预填 bot16/bot20 当前跑的值只是为了「开箱即用」，换指数只改这两行 + `crash_trigger_benchmark`。

## 五、触发判定汇总

每个决策日，深研是否强制 `forced` = 以下任一为真：

1. **调度上限**：`gapDays >= deep_research_max_gap_days`（=5 交易日）——保证至少每 5 日一次。
2. **崩盘急变**：基准 `|单日涨跌%| >= 3`。
3. **崩盘深跌**：基准 `距近高回撤 <= -8%`。

`authorized`（是否放行 `start_research`）在 agent-triggered 下恒 true——agent 任何一天都可自愿深研；上面三条只决定「哪天系统**强制**」。

## 六、测试计划

- **单元**：`computeDeepResearchState` 加崩盘用例——(a) 无 crashSignal 等价现有行为；(b) move=-3.5% 触发；(c) move=+3.1% 触发（急涨也算）；(d) dd=-8.2% 触发；(e) move=-2%/dd=-5% 不触发；(f) crashForced 与 gapDays 上限的 OR 关系。放 `world/test/`（`node --test`）。
- **`fetchBenchmarkDailyState`**：mock `market_index_quote` 返回，验证日涨跌与回撤计算；上游失败返回 null。
- **config 解析**：`parseConfig` 读取 4 个崩盘字段的默认值与边界（负数/缺省）。
- **烟测**：仿 `world-bot105d-smoke10d-agenticdeep.yaml` 建 `world-bot16d-smoke*.yaml` 跑 ~10 天，确认深研在崩盘日被强制、日志出现 `#crash` 标记、qwen3.6-plus 端点连通。

## 七、非目标 / YAGNI

- 不建深度策略文件（`medical-deep.md` 等）——深度能力与指数正交。
- 不改策略库 / `manifest.yaml`。
- 不动 live bot16/bot20 及其 config。
- 崩盘基准不做「从 manifest 自动推导目标指数」的花活——一个 config 字段足够，默认 HS300。
- 不给普通日加压（timeout 维持现状），只在强制日给宽预算（现有逻辑已覆盖）。

## 八、风险 / 未决

- **AGENTS.md 语义错位**：105d 是多指数配置，bot16d/bot20d 是单指数择时。搬深研骨架时「多指数比较」措辞必须改写为「单指数多维深研」，否则 agent 会被误导去做不存在的横向比较。这是唯一需人工把关的改写点。
- **崩盘基准的 lookback 窗口**：`drawdownFromRecentHighPct` 的「近高」取多长窗口（默认 ~60 交易日）会影响触发灵敏度；实现时可先 60，烟测后按需调，做成常量便于改。
- **qwen3.6-plus 端点**：沿用 105d 的 key/endpoint，需在烟测确认额度与连通（105d 已在跑，风险低）。
