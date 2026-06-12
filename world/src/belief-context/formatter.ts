import { HORIZONS, HISTORY_WINDOW_DAYS, type Horizon } from "./schema.ts";
import type { CalibrationStats, HorizonStats } from "./brier.ts";
import type { BeliefRecord } from "./md-history.ts";

// 第一原则提醒: 这个反馈块**不是计分卡**, 真目标是绝对收益 + 控回撤 (账户面板自见).
// 这里展示的是 belief 质量的诊断数据, 帮你看自己 reasoning 是不是漂; 别为优化 Brier 而扭曲决策.

function fmtNum(n: number | null, digits = 2): string {
  if (n === null || !Number.isFinite(n)) return "n/a";
  return n.toFixed(digits);
}

function fmtSignedNum(n: number | null, digits = 2): string {
  if (n === null || !Number.isFinite(n)) return "n/a";
  return (n >= 0 ? "+" : "") + n.toFixed(digits);
}

function brierGrade(b: number | null): string {
  if (b === null) return "";
  if (b <= 0.20) return " (校准较好)";
  if (b <= 0.25) return " (一般)";
  return " (偏差较大)";
}

function horizonLine(h: Horizon, stats: HorizonStats): string {
  if (stats.n === 0) {
    return `  • ${h.padEnd(4)} : n/a (无重叠样本)`;
  }
  const brier = fmtNum(stats.brier);
  return `  • ${h.padEnd(4)} : Brier ${brier}${brierGrade(stats.brier)},  n=${stats.n}`;
}

function biasComment(stats: CalibrationStats): string {
  let worst: { h: Horizon; bias: number } | null = null;
  for (const h of HORIZONS) {
    const s = stats.by_horizon[h];
    if (!s || s.bias === null || s.n < 3) continue;
    const absB = Math.abs(s.bias);
    if (!worst || absB > Math.abs(worst.bias)) {
      worst = { h, bias: s.bias };
    }
  }
  if (!worst) return "▍ 偏差: 样本不足 (n<3) 或无显著系统性偏差";
  const dir = worst.bias > 0 ? "偏多 (实际涨幅没你想的高)" : "偏空 (你低估了上涨概率)";
  return `▍ 偏差: ${worst.h} 上系统性${dir}, mean(p_up - actual) = ${fmtSignedNum(worst.bias)} → 该窗口置信幅度可能要往反方向调`;
}

function activityLine(stats: CalibrationStats): string {
  const a = stats.activity;
  if (a.status === "no_data") {
    return "▍ 活性: 无 Δt+1 数据 (首日或未填 prior_p_up)";
  }
  const med = fmtNum(a.abs_delta_t1_median);
  const label = a.status === "alive" ? "ALIVE" : a.status === "warn" ? "WARN" : "DEAD";
  const note =
    a.status === "alive"
      ? "概率在动, 信念活着"
      : a.status === "warn"
      ? "接近 dead 阈值, 最近概率变化偏小"
      : "概率几乎不动, 信念名存实亡";
  return `▍ 活性: median|Δt+1| = ${med} → ${label} (${note})`;
}

function suggestions(stats: CalibrationStats): string {
  // 第一原则: 建议都围绕"更稳的决策、更小的回撤"。
  // 不出现"Brier 跑赢/跑输基线"这种比赛框架。
  const items: string[] = [];

  // 系统性偏差: 提示在持仓决策上调节幅度
  for (const h of HORIZONS) {
    const s = stats.by_horizon[h];
    if (s.n >= 5 && s.bias !== null && Math.abs(s.bias) >= 0.08) {
      const dir = s.bias > 0 ? "你系统性高估上涨概率" : "你系统性低估上涨概率";
      items.push(`${h}: ${dir} (bias=${fmtSignedNum(s.bias)}) → 仓位决策时把对应方向的执行力度收一档, 控回撤`);
    }
  }

  // 校准较差: 提示别让信念直接驱动重仓
  for (const h of HORIZONS) {
    const s = stats.by_horizon[h];
    if (s.n >= 5 && s.brier !== null && s.brier > 0.25) {
      items.push(`${h}: Brier ${fmtNum(s.brier)} 偏高, 当前推理在该窗口噪声大 → 这条 horizon 的方向暂别下重注, 等校准回到 0.20 以下再加力`);
    }
  }

  // 活性问题
  if (stats.activity.status === "warn") {
    items.push("活性 WARN → 今日若无新证据就诚实保留昨日 p_up + activity_self_check.ok=false; 别为了让数据好看而瞎调");
  } else if (stats.activity.status === "dead") {
    items.push("活性 DEAD → 信念几乎不动 = 你在装懂; 要么真去做调研拿新证据, 要么 update_my_strategy 反思框架本身");
  }

  if (items.length === 0) {
    items.push("校准与活性都在容差内. 继续按第一原则做决策: 不冒不必要风险, 抓有把握的绝对收益, 严守回撤底线.");
  }

  return ["▍ 建议 (服务于绝对收益 + 控回撤这个第一原则):", ...items.map((s) => `  - ${s}`)].join("\n");
}

export function formatCalibrationBlock(
  stats: CalibrationStats,
  botId: string,
  lastRecord?: BeliefRecord,
): string {
  void botId;
  if (stats.n_records === 0) {
    return [
      `【信念校准 · 近 ${HISTORY_WINDOW_DAYS} 日反馈】`,
      "",
      "首日运行: 尚无 belief 历史, 今天按 schema 输出第一份.",
      "提醒: 这份信念是工具不是 KPI; 你的真目标是**账户的绝对收益 + 回撤控制**, 信念帮你想清楚, 决策再服务这个目标.",
    ].join("\n");
  }
  const lines: string[] = [];
  lines.push(`【信念校准 · 近 ${HISTORY_WINDOW_DAYS} 日反馈】`);
  lines.push("");
  lines.push(`▍ 历史记录: ${stats.n_records} 条 belief (target = ${stats.target_index ?? "n/a"})`);
  if (lastRecord) {
    // 每日会话无状态——bot 看不到自己昨天写了什么, prior_p_up/delta 只能瞎填 null。
    // 这里回显上一条 belief 的三档 p_up, 让 prior/delta 有据可填, 活性检测才有数据。
    const h = lastRecord.belief.horizons;
    lines.push(
      `▍ 上一条 belief (${lastRecord.date}): t+1 p_up=${fmtNum(h["t+1"]?.p_up ?? null)}, ` +
      `t+5 p_up=${fmtNum(h["t+5"]?.p_up ?? null)}, t+20 p_up=${fmtNum(h["t+20"]?.p_up ?? null)} ` +
      `→ 今天的 prior_p_up 按这个填, delta = 今日 − 上述值`,
    );
  }
  lines.push("");
  lines.push("▍ Brier 评分 (校准误差, 0=完美, 0.25≈随机, 越小说明你 ex-ante 概率越贴近事后实际)");
  for (const h of HORIZONS) {
    lines.push(horizonLine(h, stats.by_horizon[h]));
  }
  lines.push("");
  lines.push(biasComment(stats));
  lines.push("");
  lines.push(activityLine(stats));
  lines.push("");
  lines.push(suggestions(stats));
  return lines.join("\n");
}
