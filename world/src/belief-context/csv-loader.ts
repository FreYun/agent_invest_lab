import { readFileSync, existsSync } from "node:fs";
import type { IndexSymbol } from "./schema.ts";

const CSV_PATH_MAP: Record<string, string> = {
  hs300: "/home/rooot/agent_invest_lab/research/sr_factor/data/510300.SH.csv",
  zz1000: "/home/rooot/agent_invest_lab/research/sr_factor/data/512100.SH.csv",
  csi500: "/home/rooot/agent_invest_lab/research/sr_factor/data/512100.SH.csv", // placeholder
  chinext: "/home/rooot/agent_invest_lab/research/sr_factor/data/588800.SH.csv", // placeholder
  sc50: "/home/rooot/agent_invest_lab/research/sr_factor/data/588800.SH.csv",
};

// target -> {dateISO -> close}
const cache: Map<string, Map<string, number>> = new Map();
// target -> ordered date list (ISO strings, ascending)
const orderedDatesCache: Map<string, string[]> = new Map();

export function loadIndexClose(target: IndexSymbol): Map<string, number> | null {
  const cached = cache.get(target);
  if (cached) return cached;

  const path = CSV_PATH_MAP[target];
  if (!path) {
    console.warn(`[belief-context/csv-loader] no CSV path mapped for target=${target}`);
    return null;
  }
  if (!existsSync(path)) {
    console.warn(`[belief-context/csv-loader] CSV not found: ${path}`);
    return null;
  }

  try {
    const text = readFileSync(path, "utf8");
    const lines = text.split(/\r?\n/);
    if (lines.length < 2) {
      console.warn(`[belief-context/csv-loader] CSV too short: ${path}`);
      return null;
    }
    const header = lines[0]!.split(",").map((s) => s.trim().toLowerCase());
    const dateIdx = header.indexOf("trade_date");
    const closeIdx = header.indexOf("close");
    if (dateIdx < 0 || closeIdx < 0) {
      console.warn(`[belief-context/csv-loader] missing trade_date/close columns in ${path}`);
      return null;
    }

    const closeMap = new Map<string, number>();
    const dates: string[] = [];
    for (let i = 1; i < lines.length; i++) {
      const ln = lines[i];
      if (!ln || ln.trim() === "") continue;
      const cols = ln.split(",");
      const d = cols[dateIdx]?.trim();
      const cStr = cols[closeIdx]?.trim();
      if (!d || !cStr) continue;
      const c = Number(cStr);
      if (!Number.isFinite(c)) continue;
      closeMap.set(d, c);
      dates.push(d);
    }
    dates.sort();
    cache.set(target, closeMap);
    orderedDatesCache.set(target, dates);
    return closeMap;
  } catch (err) {
    console.warn(`[belief-context/csv-loader] failed to load ${path}: ${String(err)}`);
    return null;
  }
}

/**
 * Determine whether the index actually went up at beliefDate + horizonDays
 * **trading days** (using CSV sequence index offset, not calendar days).
 *
 * Returns 1 if close[t+h] > close[t], 0 if <=, null if data missing / future
 * not yet reached.
 */
export function getActualUp(
  target: IndexSymbol,
  beliefDate: string,
  horizonDays: number,
): 0 | 1 | null {
  const closeMap = loadIndexClose(target);
  if (!closeMap) return null;
  const dates = orderedDatesCache.get(target);
  if (!dates || dates.length === 0) return null;

  // Find the first trading day >= beliefDate (so if belief written on a weekend
  // we still anchor to the next session). Strictly: we want the close on or after.
  let baseIdx = -1;
  for (let i = 0; i < dates.length; i++) {
    if (dates[i]! >= beliefDate) {
      baseIdx = i;
      break;
    }
  }
  if (baseIdx < 0) {
    // beliefDate is after the last trading day we have data for
    return null;
  }
  // Use the trading day exactly at or after beliefDate as baseline
  const targetIdx = baseIdx + horizonDays;
  if (targetIdx >= dates.length) return null;

  const baseClose = closeMap.get(dates[baseIdx]!);
  const futureClose = closeMap.get(dates[targetIdx]!);
  if (baseClose === undefined || futureClose === undefined) return null;
  return futureClose > baseClose ? 1 : 0;
}
