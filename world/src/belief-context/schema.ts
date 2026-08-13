// === Belief Schema v1 (TS contract, shared by ALL belief-context modules) ===

export const BELIEF_SCHEMA_VERSION = "v1" as const;

export type IndexSymbol = "hs300" | "zz1000" | "csi500" | "chinext" | "sc50" | string;
export type EvidenceType = "research" | "news" | "macro" | "technical" | "flow";
export type Polarity = "+" | "-" | "neutral";
export type Horizon = "t+1" | "t+5" | "t+20";
export const HORIZONS: Horizon[] = ["t+1", "t+5", "t+20"];
export const HORIZON_DAYS: Record<Horizon, number> = { "t+1": 1, "t+5": 5, "t+20": 20 };

export interface BeliefHorizon {
  p_up: number;
  prior_p_up: number | null;
  delta: number | null;
}

export interface BeliefEvidence {
  type: EvidenceType;
  ref: string;
  summary: string;
  polarity: Polarity;
}

export interface BeliefActivitySelfCheck {
  abs_delta_t1: number;
  evidence_count: number;
  ok: boolean;
}

export interface Belief {
  schema: typeof BELIEF_SCHEMA_VERSION;
  target_index: IndexSymbol;
  horizons: Record<Horizon, BeliefHorizon>;
  evidence: BeliefEvidence[];
  activity_self_check?: BeliefActivitySelfCheck;
}

export const ACTIVITY_ALIVE_THRESHOLD = 0.05;
export const ACTIVITY_DEAD_THRESHOLD = 0.03;
export const BRIER_BASELINE_DEFAULT = 0.25;
// 42 个日历日：daily bot ≈ 30 条；weekly bot ≈ 6-8 条——保证 weekly 节奏也能攒够
// suggestions() 的 n≥5 触发阈值（21 天窗口下 weekly bot 只有 ~3 条，偏差建议永远不触发）。
export const HISTORY_WINDOW_DAYS = 42;

// === Parsers / Validators ===

const VALID_EVIDENCE_TYPES: ReadonlySet<string> = new Set([
  "research",
  "news",
  "macro",
  "technical",
  "flow",
]);
const VALID_POLARITY: ReadonlySet<string> = new Set(["+", "-", "neutral"]);

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function asNumberOrNull(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function parseHorizon(obj: unknown): BeliefHorizon | null {
  if (!isPlainObject(obj)) return null;
  const p_up = asNumberOrNull(obj.p_up);
  if (p_up === null) return null;
  return {
    p_up,
    prior_p_up: asNumberOrNull(obj.prior_p_up),
    delta: asNumberOrNull(obj.delta),
  };
}

function parseEvidenceItem(obj: unknown): BeliefEvidence | null {
  if (!isPlainObject(obj)) return null;
  const type = obj.type;
  const ref = obj.ref;
  const summary = obj.summary;
  const polarity = obj.polarity;
  if (typeof type !== "string" || !VALID_EVIDENCE_TYPES.has(type)) return null;
  if (typeof ref !== "string") return null;
  if (typeof summary !== "string") return null;
  if (typeof polarity !== "string" || !VALID_POLARITY.has(polarity)) return null;
  return {
    type: type as EvidenceType,
    ref,
    summary,
    polarity: polarity as Polarity,
  };
}

/**
 * Extract a Belief from an already-yaml.parse'd object (typically a frontmatter
 * object that contains a `belief:` field, OR is itself the belief block).
 * Returns null + soft-warns on failure.
 */
export function parseBelief(yamlObj: unknown): Belief | null {
  try {
    if (!isPlainObject(yamlObj)) return null;
    // Accept either {belief: {...}} or the belief object directly
    const raw: unknown = "belief" in yamlObj ? yamlObj.belief : yamlObj;
    if (!isPlainObject(raw)) return null;

    const schema = raw.schema;
    if (schema !== BELIEF_SCHEMA_VERSION) {
      // Tolerate missing schema tag — log soft warning but still try to parse
      if (schema !== undefined) {
        console.warn(`[belief-context/schema] unexpected schema=${String(schema)}, expected ${BELIEF_SCHEMA_VERSION}`);
      }
    }

    const target_index = raw.target_index;
    if (typeof target_index !== "string" || target_index.trim() === "") {
      console.warn("[belief-context/schema] missing/invalid target_index");
      return null;
    }

    const horizonsRaw = raw.horizons;
    if (!isPlainObject(horizonsRaw)) {
      console.warn("[belief-context/schema] missing horizons block");
      return null;
    }
    const horizons: Partial<Record<Horizon, BeliefHorizon>> = {};
    for (const h of HORIZONS) {
      const parsed = parseHorizon(horizonsRaw[h]);
      if (parsed) horizons[h] = parsed;
    }
    // At minimum we need one horizon to be useful; downstream validateBelief enforces all three
    if (Object.keys(horizons).length === 0) {
      console.warn("[belief-context/schema] no parseable horizons");
      return null;
    }

    const evidenceRaw = raw.evidence;
    const evidence: BeliefEvidence[] = [];
    if (Array.isArray(evidenceRaw)) {
      for (const item of evidenceRaw) {
        const parsed = parseEvidenceItem(item);
        if (parsed) evidence.push(parsed);
      }
    }

    let activity_self_check: BeliefActivitySelfCheck | undefined;
    const acRaw = raw.activity_self_check;
    if (isPlainObject(acRaw)) {
      const abs_delta_t1 = asNumberOrNull(acRaw.abs_delta_t1);
      const evidence_count = asNumberOrNull(acRaw.evidence_count);
      const ok = acRaw.ok;
      if (abs_delta_t1 !== null && evidence_count !== null && typeof ok === "boolean") {
        activity_self_check = {
          abs_delta_t1,
          evidence_count,
          ok,
        };
      }
    }

    // Fill any missing horizon with neutral placeholder so the type is satisfied;
    // validateBelief will flag if needed.
    const horizonsFull: Record<Horizon, BeliefHorizon> = {
      "t+1": horizons["t+1"] ?? { p_up: 0.5, prior_p_up: null, delta: null },
      "t+5": horizons["t+5"] ?? { p_up: 0.5, prior_p_up: null, delta: null },
      "t+20": horizons["t+20"] ?? { p_up: 0.5, prior_p_up: null, delta: null },
    };

    const belief: Belief = {
      schema: BELIEF_SCHEMA_VERSION,
      target_index,
      horizons: horizonsFull,
      evidence,
      ...(activity_self_check ? { activity_self_check } : {}),
    };
    // Re-attach the actually-present horizons (we may have lost track above for
    // validation purposes, but we keep the trio for type safety).
    void horizons;
    return belief;
  } catch (err) {
    console.warn(`[belief-context/schema] parseBelief threw: ${String(err)}`);
    return null;
  }
}

export interface ValidationResult {
  ok: boolean;
  issues: string[];
}

const REF_PATTERN = /^[0-9]{8}_/;

export function validateBelief(b: Belief): ValidationResult {
  const issues: string[] = [];

  if (b.schema !== BELIEF_SCHEMA_VERSION) {
    issues.push(`schema must equal "${BELIEF_SCHEMA_VERSION}" (got ${String(b.schema)})`);
  }
  if (typeof b.target_index !== "string" || b.target_index.trim() === "") {
    issues.push("target_index required");
  }

  for (const h of HORIZONS) {
    const hh = b.horizons?.[h];
    if (!hh) {
      issues.push(`horizons.${h} missing`);
      continue;
    }
    if (typeof hh.p_up !== "number" || !Number.isFinite(hh.p_up) || hh.p_up < 0 || hh.p_up > 1) {
      issues.push(`horizons.${h}.p_up must be in [0,1] (got ${String(hh.p_up)})`);
    }
    if (hh.prior_p_up !== null && (typeof hh.prior_p_up !== "number" || hh.prior_p_up < 0 || hh.prior_p_up > 1)) {
      issues.push(`horizons.${h}.prior_p_up must be null or in [0,1]`);
    }
  }

  if (!Array.isArray(b.evidence) || b.evidence.length < 2) {
    issues.push(`evidence requires >=2 items (got ${b.evidence?.length ?? 0})`);
  } else {
    const hasResearchWithValidRef = b.evidence.some(
      (e) => e.type === "research" && typeof e.ref === "string" && REF_PATTERN.test(e.ref),
    );
    if (!hasResearchWithValidRef) {
      issues.push("evidence must include >=1 type=research with ref matching /^[0-9]{8}_/");
    }
  }

  return { ok: issues.length === 0, issues };
}

// === Prompt block ===

export const SCHEMA_REQUIREMENT_BLOCK = `【信念契约 · Belief Schema v1】

## 第一原则 (你为什么写这个)
**目的: 赚取绝对收益的同时控制回撤**。这就是你被评价的唯一标准——不是跑赢任何基线、不是 Brier 分数好看、不是预测准确率高，而是**实盘账户的绝对收益 + 回撤控制**。

下面这份 \`belief:\` 结构化输出不是计分卡，是**工具**：强迫你把"我今天怎么想"落成可验证、可回溯的概率与证据，让你自己（和未来的你）能看出推理是不是漂了——目的是更稳的决策、更小的回撤、更可持续的绝对收益。

每日盘前，你必须把当日对**目标标的**的方向判断写成结构化 YAML，落到下面 **两种位置之一** (按你的能力选):
- **多基金 bot (有 \`save_allocation_run\` 工具)**: 写进 \`memory/portfolio/fund/市场环境判断.md\` 的 frontmatter (顶部 \`---\` ... \`---\` 块) 中的 \`belief:\` 字段
- **单基金 bot (会话禁用文件写入)**: 把完整 yaml 块用 \`\`\`yaml 代码栅栏直接放进你今天的最终回复 (assistant 输出) 里, 系统会从 \`reply.json\` 解析. 一个回复里只放一个 belief 块就好.

两种方式校准系统都能解析, 不要又写 MD 又塞 reply, 选一种.

### 必填字段
- \`schema: v1\` — 版本号，固定
- \`target_index\` — 你这只 bot 最关心的标的代号 (自由字符串)。示例: \`hs300\` / \`zz1000\` / \`csi_dividend\` / \`csi_new_energy\` / \`fund:000051\` / \`sw_biotech\`。**应与你 bot 的实际操作池一致**，避免对一个指数下注却用另一个指数算校准。
- \`horizons.t+1\` / \`horizons.t+5\` / \`horizons.t+20\` — 三档时间窗，每档含 \`p_up\` ∈ [0,1] (上涨概率)、\`prior_p_up\` (昨日值，首日填 null)、\`delta\` (今日 - 昨日，首日填 null)
- \`evidence\` — 至少 **2 条**证据，且 **至少 1 条 \`type: research\`** 且 \`ref\` 形如 \`20260601_xxx\` (日期前缀+slug)，证明你看了真东西。**今天没做深度研究不是豁免**——引用最近一次深研笔记即可，\`ref\` 用**那次深研的日期**作前缀，并在 \`summary\` 里注明「距今 N 个交易日」。研究越旧，这条信念的支撑越弱，这正是要暴露出来的东西，不要靠省略掩盖。
- 每条 evidence: \`type\` ∈ {research,news,macro,technical,flow}、\`ref\`、\`summary\` (≤30 字)、\`polarity\` ∈ {+,-,neutral}

### 活性自查 (可选但强烈建议)
\`activity_self_check\` 字段: { abs_delta_t1(今日 t+1 p_up 变化绝对值), evidence_count, ok (你自评今天是否真有 update) }
若 |Δt+1| < 0.03 且无新证据 → 标 \`ok: false\` (诚实记账)，**胜过**捏造小数装样子。

### 防伪铁律
- **键名逐字照抄下面的示例**：\`schema\` / \`target_index\` / \`horizons\` / \`t+1\` / \`t+5\` / \`t+20\` / \`evidence\`，一个字都不要改写、意译或增删顶层键。**不要**用 \`p_up\` 当顶层键代替 \`horizons\`、**不要**把 \`t+20\` 写成 \`t_plus_20\` 或 \`t20\`、**不要**把概率拆成 tactical/strategic 两套。结构一改系统就解析不出来，这一天的信念等于没写，也进不了校准样本。
- p_up 用真实概率 (0.50 = 完全不知道；不准全填 0.55 装活)
- ref 不能编 — 若被反查发现日期前缀和文件名对不上，直接判信念失效
- evidence summary 必须能在你的研究 MD / 当日 news 工具调用里找到对应原文

### 示例 YAML
\`\`\`yaml
belief:
  schema: v1
  target_index: hs300              # 自由字符串; 跟你的操作池对齐
  horizons:
    t+1:  { p_up: 0.52, prior_p_up: 0.48, delta: 0.04 }
    t+5:  { p_up: 0.55, prior_p_up: 0.50, delta: 0.05 }
    t+20: { p_up: 0.58, prior_p_up: 0.56, delta: 0.02 }
  evidence:
    - { type: research,  ref: "20260601_pmi_review",      summary: "5月PMI回升至50.2",      polarity: "+" }
    - { type: macro,     ref: "20260601_cn_cpi",          summary: "CPI同比0.3%, 通缩压力小幅缓解", polarity: "+" }
    - { type: technical, ref: "20260601_hs300_ma_break",  summary: "沪深300站稳60日线",     polarity: "+" }
  activity_self_check: { abs_delta_t1: 0.04, evidence_count: 3, ok: true }
\`\`\`
`;
