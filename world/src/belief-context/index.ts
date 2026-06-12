import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import yaml from "yaml";
import {
  SCHEMA_REQUIREMENT_BLOCK,
  HISTORY_WINDOW_DAYS,
  parseBelief,
  validateBelief,
  type ValidationResult,
} from "./schema.ts";
import { scanRecentBeliefs } from "./md-history.ts";
import { computeCalibration } from "./brier.ts";
import { formatCalibrationBlock } from "./formatter.ts";

export * from "./schema.ts";
export type { BeliefRecord } from "./md-history.ts";
export type { CalibrationStats, HorizonStats, ActivityStats } from "./brier.ts";

/**
 * Build the full belief-context string for injection into a bot's daily prompt.
 * Combines:
 *   1. Schema requirement block (always present)
 *   2. Calibration feedback block (or first-day fallback)
 *
 * Never throws: any failure falls back to schema-only output.
 */
export async function buildBeliefContext(
  botId: string,
  runId: string,
  currentDate: string,
): Promise<string> {
  try {
    const records = await scanRecentBeliefs(botId, runId, currentDate, HISTORY_WINDOW_DAYS);
    const stats = await computeCalibration(records);
    // 末条 record 回显给 bot（prior_p_up 依据）——会话无状态，没有这条 bot 只能填 null。
    const block = formatCalibrationBlock(stats, botId, records[records.length - 1]);
    return `${SCHEMA_REQUIREMENT_BLOCK}\n\n${block}`;
  } catch (err) {
    console.warn(`[belief-context] buildBeliefContext failed for ${botId}/${currentDate}: ${String(err)}`);
    return SCHEMA_REQUIREMENT_BLOCK;
  }
}

function extractFrontmatter(text: string): string | null {
  if (!text.startsWith("---")) return null;
  const lines = text.split(/\r?\n/);
  if (lines[0]?.trim() !== "---") return null;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i]?.trim() === "---") {
      return lines.slice(1, i).join("\n");
    }
  }
  return null;
}

/**
 * Post-chat validator: 多基金 bot 把 belief 写在 MD frontmatter, 单基金 bot 写在 reply.json 的
 * reply 字段的 ```yaml fence 里. 这里两个都试.
 *
 * 参数语义 (向后兼容): 第一个参数依然叫 mdPath, 但若文件后缀是 .json 也支持直接传 reply.json 路径.
 * 调用方一般两个路径都传, 优先 MD, 后 fallback 到 reply.
 *
 * Returns null  → 无法判定 (两个源都不存在 / 都没有 belief)
 * Returns ok:false → 找到了源但 belief 缺失或不合规
 * Returns ok:true  → 合规
 */
export async function validateBeliefMd(
  mdPath: string,
  replyJsonPath?: string,
): Promise<ValidationResult | null> {
  // Source 1: MD frontmatter
  try {
    if (existsSync(mdPath)) {
      const text = await readFile(mdPath, "utf8");
      const fm = extractFrontmatter(text);
      if (fm) {
        let parsed: unknown;
        try {
          parsed = yaml.parse(fm);
        } catch (err) {
          return { ok: false, issues: [`yaml parse error in MD: ${String(err)}`] };
        }
        const obj = parsed as Record<string, unknown> | null;
        if (obj && typeof obj === "object" && "belief" in obj) {
          const belief = parseBelief(obj);
          if (!belief) {
            return { ok: false, issues: ["belief block in MD could not be parsed (shape mismatch)"] };
          }
          return validateBelief(belief);
        }
      }
    }
  } catch (err) {
    console.warn(`[belief-context] validateBeliefMd MD branch failed for ${mdPath}: ${String(err)}`);
  }

  // Source 2: reply.json
  if (replyJsonPath && existsSync(replyJsonPath)) {
    try {
      const text = await readFile(replyJsonPath, "utf8");
      const parsed = JSON.parse(text) as { reply?: string };
      const body = parsed?.reply ?? "";
      if (!body) return null;
      // Try to find a fenced yaml block containing belief:
      const fenceRe = /```(?:ya?ml)?\s*\n([\s\S]*?)```/gi;
      let m: RegExpExecArray | null;
      while ((m = fenceRe.exec(body)) !== null) {
        const block = m[1] ?? "";
        if (!block.includes("belief:")) continue;
        let payload = block.trim();
        if (payload.startsWith("---")) {
          const lines = payload.split(/\r?\n/);
          let endIdx = -1;
          for (let i = 1; i < lines.length; i++) {
            if (lines[i]?.trim() === "---") { endIdx = i; break; }
          }
          if (endIdx > 0) payload = lines.slice(1, endIdx).join("\n");
        }
        let parsedYaml: unknown;
        try {
          parsedYaml = yaml.parse(payload);
        } catch (err) {
          return { ok: false, issues: [`yaml parse error in reply.json fence: ${String(err)}`] };
        }
        const obj = parsedYaml as Record<string, unknown> | null;
        if (!obj || typeof obj !== "object" || !("belief" in obj)) continue;
        const belief = parseBelief(obj);
        if (!belief) {
          return { ok: false, issues: ["belief block in reply.json could not be parsed (shape mismatch)"] };
        }
        return validateBelief(belief);
      }
      // No fence with belief: found
      if (body.includes("belief:")) {
        return { ok: false, issues: ["reply contains 'belief:' but not in a parseable ```yaml fence"] };
      }
      return { ok: false, issues: ["missing belief block in reply.json"] };
    } catch (err) {
      console.warn(`[belief-context] validateBeliefMd reply branch failed for ${replyJsonPath}: ${String(err)}`);
    }
  }

  // Both sources absent → undecidable
  return null;
}
