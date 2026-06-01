import {
  HORIZONS,
  HORIZON_DAYS,
  ACTIVITY_ALIVE_THRESHOLD,
  ACTIVITY_DEAD_THRESHOLD,
  type Horizon,
  type IndexSymbol,
} from "./schema.ts";
import type { BeliefRecord } from "./md-history.ts";
import { getActualUp } from "./csv-loader.ts";

// Belief calibration 是工具 (帮你看自己 reasoning 是不是漂)，**不是计分卡**。
// 计分卡只有一个第一原则: **赚取绝对收益 + 控制回撤**。
// 这里只算 Brier (校准误差) 和活性 (信念是否在动)，不做"跑输基线 / 跑赢基线"的比较。

export interface HorizonStats {
  n: number;
  brier: number | null;       // 越低越好; ≤0.20 校准较好, 0.20-0.25 一般, >0.25 系统性偏差
  bias: number | null;        // mean(p_up) - mean(actual); >0 偏多, <0 偏空
}

export interface ActivityStats {
  abs_delta_t1_median: number | null;
  status: "alive" | "warn" | "dead" | "no_data";
}

export interface CalibrationStats {
  n_records: number;
  target_index: IndexSymbol | null;
  by_horizon: Record<Horizon, HorizonStats>;
  activity: ActivityStats;
}

function emptyHorizon(): HorizonStats {
  return { n: 0, brier: null, bias: null };
}

function median(xs: number[]): number | null {
  if (xs.length === 0) return null;
  const s = [...xs].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 === 0 ? (s[mid - 1]! + s[mid]!) / 2 : s[mid]!;
}

export function computeCalibration(
  records: BeliefRecord[],
  target?: IndexSymbol,
): CalibrationStats {
  const emptyByHorizon: Record<Horizon, HorizonStats> = {
    "t+1": emptyHorizon(),
    "t+5": emptyHorizon(),
    "t+20": emptyHorizon(),
  };

  if (records.length === 0) {
    return {
      n_records: 0,
      target_index: target ?? null,
      by_horizon: emptyByHorizon,
      activity: { abs_delta_t1_median: null, status: "no_data" },
    };
  }

  let chosenTarget: IndexSymbol;
  if (target) {
    chosenTarget = target;
  } else {
    const counts = new Map<string, number>();
    for (const r of records) {
      counts.set(r.belief.target_index, (counts.get(r.belief.target_index) ?? 0) + 1);
    }
    let best: string | null = null;
    let bestN = -1;
    for (const [k, v] of counts.entries()) {
      if (v > bestN) {
        best = k;
        bestN = v;
      }
    }
    chosenTarget = best ?? "hs300";
  }

  const relevant = records.filter((r) => r.belief.target_index === chosenTarget);

  const by_horizon: Record<Horizon, HorizonStats> = {
    "t+1": emptyHorizon(),
    "t+5": emptyHorizon(),
    "t+20": emptyHorizon(),
  };

  for (const h of HORIZONS) {
    const hDays = HORIZON_DAYS[h];
    const pairs: { p: number; actual: 0 | 1 }[] = [];
    for (const r of relevant) {
      const hb = r.belief.horizons[h];
      if (!hb || typeof hb.p_up !== "number") continue;
      const actual = getActualUp(chosenTarget, r.date, hDays);
      if (actual === null) continue;
      pairs.push({ p: hb.p_up, actual });
    }
    if (pairs.length === 0) {
      by_horizon[h] = emptyHorizon();
      continue;
    }
    const n = pairs.length;
    const brier = pairs.reduce((s, x) => s + (x.p - x.actual) ** 2, 0) / n;
    const meanP = pairs.reduce((s, x) => s + x.p, 0) / n;
    const meanActual = pairs.reduce((s, x) => s + x.actual, 0) / n;
    const bias = meanP - meanActual;
    by_horizon[h] = { n, brier, bias };
  }

  const deltas: number[] = [];
  for (const r of relevant) {
    const d = r.belief.horizons["t+1"]?.delta;
    if (typeof d === "number" && Number.isFinite(d)) {
      deltas.push(Math.abs(d));
    }
  }
  let status: ActivityStats["status"] = "no_data";
  const med = median(deltas);
  if (med === null) {
    status = "no_data";
  } else if (med >= ACTIVITY_ALIVE_THRESHOLD) {
    status = "alive";
  } else if (med >= ACTIVITY_DEAD_THRESHOLD) {
    status = "warn";
  } else {
    status = "dead";
  }

  return {
    n_records: relevant.length,
    target_index: chosenTarget,
    by_horizon,
    activity: { abs_delta_t1_median: med, status },
  };
}
