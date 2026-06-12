import { readFileSync, existsSync } from "node:fs";
import { request } from "node:http";
import path from "node:path";
import type { IndexSymbol } from "./schema.ts";

// CSV 数据目录从模块位置自推导（world/src/belief-context → <repo>/research/sr_factor/data），
// 不依赖部署机的 HOME 布局（曾硬编码 /home/rooot/...，迁移后是死路径 → getActualUp 永远 null）。
const SR_FACTOR_DATA_DIR = path.resolve(
  import.meta.dirname, "..", "..", "..", "research", "sr_factor", "data",
);

// 1) 硬编码 index → CSV 路径（宽基指数走本地 CSV）
const CSV_PATH_MAP: Record<string, string> = {
  hs300: path.join(SR_FACTOR_DATA_DIR, "510300.SH.csv"),
  zz1000: path.join(SR_FACTOR_DATA_DIR, "512100.SH.csv"),
  chinext: path.join(SR_FACTOR_DATA_DIR, "588800.SH.csv"), // placeholder
  sc50: path.join(SR_FACTOR_DATA_DIR, "588800.SH.csv"),
};

// 1b) 无本地 CSV 的指数 → simworld fund_nav 代理序列（跟踪基金复权净值近似指数方向）。
//     zz500/csi500：本地无 510500 CSV，旧映射借 512100（中证1000 ETF）是错误标的，
//     改用天弘中证500联接 000962（zz500 bot 的实际操作标的）。
const FUND_NAV_FALLBACK: Record<string, string> = {
  zz500: "000962",
  csi500: "000962",
};

// 2) target → {dateISO → close/nav}; orderedDates 是按日期升序
const cache: Map<string, Map<string, number> | null> = new Map();
const orderedDatesCache: Map<string, string[]> = new Map();

// simworld-data MCP backend (跑在固定端口)。模拟时间取 9999 确保拉到完整 NAV
// 序列（calibration 是事后校准, 用"未来"NAV 合理; bot 自己写 belief 时看不到这条路径）
const SIMWORLD_MCP_URL = "http://127.0.0.1:18078/mcp";
const SIMULATED_DATETIME_FOR_CALIBRATION = "9999-12-31 23:59:59";

function parseCsvCloses(csvPath: string): Map<string, number> | null {
  try {
    if (!existsSync(csvPath)) return null;
    const text = readFileSync(csvPath, "utf8");
    const lines = text.split(/\r?\n/);
    if (lines.length < 2) return null;
    const header = lines[0]!.split(",").map((s) => s.trim().toLowerCase());
    const dateIdx = header.indexOf("trade_date");
    const closeIdx = header.indexOf("close");
    if (dateIdx < 0 || closeIdx < 0) return null;
    const m = new Map<string, number>();
    for (let i = 1; i < lines.length; i++) {
      const ln = lines[i];
      if (!ln || ln.trim() === "") continue;
      const cols = ln.split(",");
      const d = cols[dateIdx]?.trim();
      const cStr = cols[closeIdx]?.trim();
      if (!d || !cStr) continue;
      const c = Number(cStr);
      if (!Number.isFinite(c)) continue;
      m.set(d.slice(0, 10), c);
    }
    return m.size > 0 ? m : null;
  } catch (err) {
    console.warn(`[belief-context/csv-loader] failed to parse CSV ${csvPath}: ${String(err)}`);
    return null;
  }
}

/** 从 target 字符串里提取 6 位数字基金代码（前后无相邻数字）。 */
function extractFundCode(target: string): string | null {
  const m = target.match(/(?<!\d)(\d{6})(?!\d)/);
  return m ? m[1]! : null;
}

/** 调 simworld-data MCP 的 fund_nav 拉一只基金的 NAV 时间序列。 */
function loadFundNavFromSimworld(fundCode: string): Promise<Map<string, number> | null> {
  return new Promise((resolve) => {
    const payload = JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: {
        name: "fund_nav",
        arguments: {
          fund_codes: [fundCode],
          simulated_datetime: SIMULATED_DATETIME_FOR_CALIBRATION,
        },
      },
    });
    const url = new URL(SIMWORLD_MCP_URL);
    const req = request(
      {
        hostname: url.hostname,
        port: url.port || 80,
        path: url.pathname,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json, text/event-stream",
          "Content-Length": Buffer.byteLength(payload).toString(),
        },
        timeout: 10_000,
      },
      (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk: string) => {
          body += chunk;
        });
        res.on("end", () => {
          try {
            // SSE 格式：找首个 data: 行
            let text = body.trim();
            if (text.startsWith("event:")) {
              const dataLine = text.split(/\r?\n/).find((l) => l.startsWith("data:"));
              if (!dataLine) return resolve(null);
              text = dataLine.slice(5).trim();
            }
            const parsed = JSON.parse(text);
            const contentArr = parsed?.result?.content;
            if (!Array.isArray(contentArr)) return resolve(null);
            const inner = contentArr[0]?.text;
            if (!inner) return resolve(null);
            const data = JSON.parse(inner);
            // simworld fund_nav 返回结构 (中文 key):
            //   { "success": true, "items": [{ "基金代码": "159570", "净值记录": [
            //       { "交易日期": "2025-01-02", "复权单位净值": 0.9318, "日收益率": -2.75 }, ... ]}] }
            const m = new Map<string, number>();
            const outer = Array.isArray(data?.items) ? data.items : Array.isArray(data) ? data : [];
            for (const fund of outer as Array<Record<string, unknown>>) {
              const navRecords = fund?.["净值记录"] ?? fund?.nav_records ?? fund?.records;
              if (!Array.isArray(navRecords)) continue;
              for (const r of navRecords as Array<Record<string, unknown>>) {
                const dRaw = r?.["交易日期"] ?? r?.nav_date ?? r?.trade_date ?? r?.date;
                const vRaw = r?.["复权单位净值"] ?? r?.["单位净值"] ?? r?.nav ?? r?.unit_nav ?? r?.close;
                if (typeof dRaw !== "string") continue;
                const v = typeof vRaw === "number" ? vRaw : parseFloat(String(vRaw));
                if (!Number.isFinite(v)) continue;
                m.set(dRaw.slice(0, 10), v);
              }
            }
            resolve(m.size > 0 ? m : null);
          } catch (err) {
            console.warn(`[belief-context/csv-loader] simworld fund_nav parse failed for ${fundCode}: ${String(err)}`);
            resolve(null);
          }
        });
      },
    );
    req.on("error", (err) => {
      console.warn(`[belief-context/csv-loader] simworld fund_nav request failed for ${fundCode}: ${String(err)}`);
      resolve(null);
    });
    req.on("timeout", () => {
      req.destroy();
      console.warn(`[belief-context/csv-loader] simworld fund_nav timeout for ${fundCode}`);
      resolve(null);
    });
    req.write(payload);
    req.end();
  });
}

/**
 * 拿 target 的 close/nav 时间序列。优先级:
 *   1. 硬编码宽基 CSV (sync 路径, 快)
 *   2. target 字符串里能提取 6 位基金代码 → simworld fund_nav
 *   3. 都失败返回 null
 *
 * 命中后缓存; 二次调用直接返回 cache.
 */
export async function loadIndexClose(target: IndexSymbol): Promise<Map<string, number> | null> {
  if (cache.has(target)) return cache.get(target) ?? null;

  let result: Map<string, number> | null = null;

  const csvPath = CSV_PATH_MAP[target];
  if (csvPath) {
    result = parseCsvCloses(csvPath);
  } else {
    const fundCode = FUND_NAV_FALLBACK[target] ?? extractFundCode(target);
    if (fundCode) {
      result = await loadFundNavFromSimworld(fundCode);
    } else {
      console.warn(`[belief-context/csv-loader] no CSV path mapped and no 6-digit fund code in target=${target}`);
    }
  }

  cache.set(target, result);
  if (result) {
    const dates = [...result.keys()].sort();
    orderedDatesCache.set(target, dates);
  }
  return result;
}

/**
 * 判断 target 在 beliefDate + horizonDays **交易日**（用 close 序列的下标位移
 * 而不是日历日 + horizonDays）后实际是否上涨.
 * 返回 1=涨, 0=平/跌, null=数据缺失 / 未来日尚未到.
 */
export async function getActualUp(
  target: IndexSymbol,
  beliefDate: string,
  horizonDays: number,
): Promise<0 | 1 | null> {
  const closeMap = await loadIndexClose(target);
  if (!closeMap) return null;
  const dates = orderedDatesCache.get(target);
  if (!dates || dates.length === 0) return null;

  // 锚到 ≥ beliefDate 的第一个交易日
  let baseIdx = -1;
  for (let i = 0; i < dates.length; i++) {
    if (dates[i]! >= beliefDate) {
      baseIdx = i;
      break;
    }
  }
  if (baseIdx < 0) return null;

  const targetIdx = baseIdx + horizonDays;
  if (targetIdx >= dates.length) return null;

  const baseClose = closeMap.get(dates[baseIdx]!);
  const futureClose = closeMap.get(dates[targetIdx]!);
  if (baseClose === undefined || futureClose === undefined) return null;
  return futureClose > baseClose ? 1 : 0;
}
