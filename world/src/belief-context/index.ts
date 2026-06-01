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
    const stats = computeCalibration(records);
    const block = formatCalibrationBlock(stats, botId);
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
 * Post-chat validator: read a MD file with belief frontmatter and confirm it
 * passes schema rules. Returns null if file or frontmatter absent ("undecidable");
 * returns {ok:false, issues} if the frontmatter exists but the belief block is
 * missing or invalid; returns {ok:true} if everything checks out.
 */
export async function validateBeliefMd(
  mdPath: string,
): Promise<ValidationResult | null> {
  try {
    if (!existsSync(mdPath)) return null;
    const text = await readFile(mdPath, "utf8");
    const fm = extractFrontmatter(text);
    if (!fm) return null;
    let parsed: unknown;
    try {
      parsed = yaml.parse(fm);
    } catch (err) {
      return { ok: false, issues: [`yaml parse error: ${String(err)}`] };
    }
    if (
      !parsed ||
      typeof parsed !== "object" ||
      !("belief" in (parsed as Record<string, unknown>))
    ) {
      return { ok: false, issues: ["missing belief block"] };
    }
    const belief = parseBelief(parsed);
    if (!belief) {
      return { ok: false, issues: ["belief block could not be parsed (shape mismatch)"] };
    }
    return validateBelief(belief);
  } catch (err) {
    console.warn(`[belief-context] validateBeliefMd failed for ${mdPath}: ${String(err)}`);
    return null;
  }
}
