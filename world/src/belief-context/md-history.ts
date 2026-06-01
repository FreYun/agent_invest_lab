import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import yaml from "yaml";
import { parseBelief, type Belief } from "./schema.ts";

export interface BeliefRecord {
  date: string;
  belief: Belief;
  runId: string;
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

/**
 * Extract the YAML frontmatter (between leading `---` ... `---`) from an MD
 * file body. Returns null if no frontmatter present.
 */
function extractFrontmatter(text: string): string | null {
  if (!text.startsWith("---")) return null;
  // Find the second `---` line
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

function daysBeforeISO(currentDate: string, days: number): string {
  const d = new Date(currentDate + "T00:00:00Z");
  if (isNaN(d.getTime())) return currentDate;
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

/**
 * Scan ~/runtime/runs/dash-*\/<date>/<botId>/memory/portfolio/fund/市场环境判断.md
 * for the windowDays trading days before currentDate (cap at windowDays * 1.5
 * calendar days).
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
    // Sort descending (newest first) — names contain ISO-ish timestamps so lexicographic works.
    dashRuns.sort((a, b) => b.localeCompare(a));
    const recentRuns = dashRuns.slice(0, MAX_RUN_DIRS);

    const calendarLimit = Math.ceil(windowDays * 1.5);
    const minDate = daysBeforeISO(currentDate, calendarLimit);

    // dateISO -> {runId, mtime, belief}
    const byDate = new Map<string, { runId: string; mtime: number; belief: Belief }>();

    for (const run of recentRuns) {
      const runPath = path.join(RUNS_ROOT, run);
      const dateDirs = await safeReaddir(runPath);
      const candidateDates = dateDirs
        .filter((d) => ISO_DATE.test(d))
        .filter((d) => d >= minDate && d < currentDate);
      // Take at most windowDays * 1.5 most recent
      candidateDates.sort();
      const slice = candidateDates.slice(-Math.ceil(windowDays * 1.5));

      for (const dateDir of slice) {
        const mdPath = path.join(
          runPath,
          dateDir,
          botId,
          "memory",
          "portfolio",
          "fund",
          "市场环境判断.md",
        );
        const belief = await loadBeliefFromMd(mdPath);
        if (!belief) continue;
        let mtime = 0;
        try {
          const st = await stat(mdPath);
          mtime = st.mtimeMs;
        } catch {
          // ignore
        }
        const prev = byDate.get(dateDir);
        if (!prev || mtime > prev.mtime) {
          byDate.set(dateDir, { runId: run, mtime, belief });
        }
      }
    }

    const records: BeliefRecord[] = [];
    for (const [date, entry] of byDate.entries()) {
      records.push({ date, belief: entry.belief, runId: entry.runId });
    }
    records.sort((a, b) => a.date.localeCompare(b.date));
    // Trim to windowDays most recent records
    return records.slice(-windowDays);
  } catch (err) {
    console.warn(`[belief-context/md-history] scanRecentBeliefs failed: ${String(err)}`);
    return [];
  }
}
