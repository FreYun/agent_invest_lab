import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import yaml from "yaml";
import { parseBelief, type Belief } from "./schema.ts";

export interface BeliefRecord {
  date: string;
  belief: Belief;
  runId: string;
  source: "md" | "reply"; // 多基金 bot 从 MD frontmatter, 单基金 bot 从 reply.json
}

const RUNS_ROOT = "/home/rooot/agent_invest_lab/world/runtime/runs";
const MAX_RUN_DIRS = 30;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

async function safeReaddir(p: string): Promise<string[]> {
  try {
    return await readdir(p);
  } catch {
    return [];
  }
}

/** Extract YAML frontmatter (between leading `---` ... `---`). */
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

async function loadBeliefFromMd(mdPath: string): Promise<Belief | null> {
  try {
    const text = await readFile(mdPath, "utf8");
    const fm = extractFrontmatter(text);
    if (!fm) return null;
    let parsed: unknown;
    try {
      parsed = yaml.parse(fm);
    } catch (err) {
      console.warn(`[belief-context/md-history] yaml parse failed ${mdPath}: ${String(err)}`);
      return null;
    }
    return parseBelief(parsed);
  } catch {
    return null;
  }
}

/** Parse a YAML text block to Belief (handles both raw and ---wrapped frontmatter style). */
function tryParseBeliefYamlBlock(yamlText: string): Belief | null {
  let payload = yamlText.trim();
  if (payload.startsWith("---")) {
    const lines = payload.split(/\r?\n/);
    let endIdx = -1;
    for (let i = 1; i < lines.length; i++) {
      if (lines[i]?.trim() === "---") { endIdx = i; break; }
    }
    if (endIdx > 0) {
      payload = lines.slice(1, endIdx).join("\n");
    }
  }
  let parsed: unknown;
  try {
    parsed = yaml.parse(payload);
  } catch {
    return null;
  }
  return parseBelief(parsed);
}

/** Find first fenced ```yaml block containing `belief:` in a text body, parse it. */
function extractBeliefFromText(body: string): Belief | null {
  if (!body || !body.includes("belief:")) return null;
  const fenceRe = /```(?:ya?ml)?\s*\n([\s\S]*?)```/gi;
  let m: RegExpExecArray | null;
  while ((m = fenceRe.exec(body)) !== null) {
    const block = m[1] ?? "";
    if (!block.includes("belief:")) continue;
    const belief = tryParseBeliefYamlBlock(block);
    if (belief) return belief;
  }
  // Fallback: try parsing from "belief:" onwards (no fence case).
  const idx = body.indexOf("belief:");
  if (idx >= 0) {
    const chunk = body.slice(idx, idx + 4000);
    const belief = tryParseBeliefYamlBlock(chunk);
    if (belief) return belief;
  }
  return null;
}

/** Single-fund bots write YAML belief inside reply.json's `reply` field (no MD write tool). */
async function loadBeliefFromReplyJson(replyJsonPath: string): Promise<Belief | null> {
  try {
    const text = await readFile(replyJsonPath, "utf8");
    const parsed = JSON.parse(text) as { reply?: string };
    if (!parsed?.reply) return null;
    return extractBeliefFromText(parsed.reply);
  } catch {
    return null;
  }
}

function daysBeforeISO(currentDate: string, days: number): string {
  const d = new Date(currentDate + "T00:00:00Z");
  if (isNaN(d.getTime())) return currentDate;
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

/**
 * Scan recent dash-* runs for this bot's beliefs. Two sources:
 *   1. memory/portfolio/fund/市场环境判断.md frontmatter (多基金 bot)
 *   2. reply.json's `reply` field, ```yaml fence (单基金 bot)
 */
export async function scanRecentBeliefs(
  botId: string,
  runId: string,
  currentDate: string,
  windowDays: number,
): Promise<BeliefRecord[]> {
  void runId; // collect across all runs, not just this one
  try {
    const runDirs = await safeReaddir(RUNS_ROOT);
    const dashRuns = runDirs.filter((n) => n.startsWith("dash-"));
    dashRuns.sort((a, b) => b.localeCompare(a));
    const recentRuns = dashRuns.slice(0, MAX_RUN_DIRS);

    const calendarLimit = Math.ceil(windowDays * 1.5);
    const minDate = daysBeforeISO(currentDate, calendarLimit);

    // dateISO -> {runId, mtime, belief, source}
    const byDate = new Map<string, { runId: string; mtime: number; belief: Belief; source: "md" | "reply" }>();

    for (const run of recentRuns) {
      const runPath = path.join(RUNS_ROOT, run);
      const dateDirs = await safeReaddir(runPath);
      const candidateDates = dateDirs
        .filter((d) => ISO_DATE.test(d))
        .filter((d) => d >= minDate && d < currentDate);
      candidateDates.sort();
      const slice = candidateDates.slice(-Math.ceil(windowDays * 1.5));

      for (const dateDir of slice) {
        const mdPath = path.join(
          runPath, dateDir, botId, "memory", "portfolio", "fund", "市场环境判断.md",
        );
        const replyPath = path.join(runPath, dateDir, botId, "reply.json");

        let belief: Belief | null = null;
        let source: "md" | "reply" = "md";
        let mtime = 0;

        const mdBelief = await loadBeliefFromMd(mdPath);
        if (mdBelief) {
          belief = mdBelief;
          source = "md";
          try { mtime = (await stat(mdPath)).mtimeMs; } catch { /* ignore */ }
        } else {
          const replyBelief = await loadBeliefFromReplyJson(replyPath);
          if (replyBelief) {
            belief = replyBelief;
            source = "reply";
            try { mtime = (await stat(replyPath)).mtimeMs; } catch { /* ignore */ }
          }
        }
        if (!belief) continue;

        const prev = byDate.get(dateDir);
        if (!prev || mtime > prev.mtime) {
          byDate.set(dateDir, { runId: run, mtime, belief, source });
        }
      }
    }

    const records: BeliefRecord[] = [];
    for (const [date, entry] of byDate.entries()) {
      records.push({ date, belief: entry.belief, runId: entry.runId, source: entry.source });
    }
    records.sort((a, b) => a.date.localeCompare(b.date));
    return records.slice(-windowDays);
  } catch (err) {
    console.warn(`[belief-context/md-history] scanRecentBeliefs failed: ${String(err)}`);
    return [];
  }
}
