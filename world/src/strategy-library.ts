import { cpSync, existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { parse as parseYaml } from 'yaml'

const FUND_CODE_RE = /^\d{6}$/

export interface StrategyDefinition {
  id: string
  title: string
  methodology: string
  methodologyPath: string
  targetIndex: string
  defaultBuyableFundCodes: string[]
}

export interface StrategyLibrary {
  root: string
  strategies: Map<string, StrategyDefinition>
}

export interface ActiveStrategyRenderInput {
  botId: string
  strategy: StrategyDefinition
  buyableFundCodes: string[]
}

function reqString(obj: Record<string, unknown>, key: string, label: string): string {
  const value = obj[key]
  if (typeof value !== 'string' || !value.trim()) throw new Error(`${label}.${key} must be a non-empty string`)
  return value.trim()
}

function parseFundCodes(value: unknown, label: string): string[] {
  if (value === undefined) return []
  if (!Array.isArray(value) || !value.every(c => typeof c === 'string' && FUND_CODE_RE.test(c))) {
    throw new Error(`${label} must be an array of 6-digit fund code strings`)
  }
  return [...new Set(value as string[])].sort()
}

export function loadStrategyLibrary(root: string): StrategyLibrary {
  const manifestPath = join(root, 'manifest.yaml')
  if (!existsSync(manifestPath)) throw new Error(`strategy library manifest not found: ${manifestPath}`)
  const raw = (parseYaml(readFileSync(manifestPath, 'utf8')) ?? {}) as Record<string, unknown>
  if (raw.version !== 1) throw new Error(`strategy library manifest version must be 1: ${manifestPath}`)
  const strategiesRaw = raw.strategies
  if (!strategiesRaw || typeof strategiesRaw !== 'object' || Array.isArray(strategiesRaw)) {
    throw new Error(`strategy library manifest "strategies" must be an object: ${manifestPath}`)
  }

  const strategies = new Map<string, StrategyDefinition>()
  for (const [id, entry] of Object.entries(strategiesRaw as Record<string, unknown>)) {
    if (!/^[A-Za-z0-9_-]+$/.test(id)) throw new Error(`strategy id "${id}" contains illegal characters`)
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      throw new Error(`strategy "${id}" must be an object`)
    }
    const e = entry as Record<string, unknown>
    const title = reqString(e, 'title', `strategy "${id}"`)
    const methodology = reqString(e, 'methodology', `strategy "${id}"`)
    if (methodology.includes('/') || methodology.includes('\\') || methodology.includes('..') || methodology.includes('\0')) {
      throw new Error(`strategy "${id}" methodology must be a basename markdown file`)
    }
    const methodologyPath = join(root, methodology)
    if (!existsSync(methodologyPath)) throw new Error(`strategy "${id}" methodology file not found: ${methodologyPath}`)
    const targetIndex = reqString(e, 'target_index', `strategy "${id}"`)
    const defaultBuyableFundCodes = parseFundCodes(e.default_buyable_fund_codes, `strategy "${id}".default_buyable_fund_codes`)
    strategies.set(id, { id, title, methodology, methodologyPath, targetIndex, defaultBuyableFundCodes })
  }
  if (strategies.size === 0) throw new Error(`strategy library manifest contains no strategies: ${manifestPath}`)
  return { root, strategies }
}

// 任务头锚点：METHODOLOGY.md 顶部的「# 当前回测任务」块，pin 死 target_index / strategy_id /
// buyable_fund_codes。这是 bot 的物理约束（对哪个指数下注、能买哪些基金），**不可被 update_my_strategy
// 的自进化重写覆盖掉**——否则 bot 会在自改方法论时把指数代码锚弄丢、漂到错误指数（历史事故：robot bot
// 把 H30590 机器人漂成 000813 化工，拿化工估值给机器人仓位择时）。TASK_HEADER_SENTINEL 用于识别/剥离，
// TASK_HEADER_DELIM 是头与正文的分隔线。
export const TASK_HEADER_SENTINEL = '# 当前回测任务'
export const TASK_HEADER_DELIM = '---'

// 只渲染任务头（含结尾分隔线），不含方法论正文。update_my_strategy 每次重写都会重新拼上这一块。
export function renderTaskHeader(input: ActiveStrategyRenderInput): string {
  const { botId, strategy, buyableFundCodes } = input
  const codes = buyableFundCodes.join(', ')
  return [
    TASK_HEADER_SENTINEL,
    '',
    `- bot_id: ${botId}`,
    `- strategy_id: ${strategy.id}`,
    `- strategy_title: ${strategy.title}`,
    `- target_index: ${strategy.targetIndex}`,
    `- buyable_fund_codes: ${codes}`,
    `- **锚定约束**：上面的 target_index / buyable_fund_codes 是本 bot 的物理标的，**取数、估值、择时、belief 的 target_index 都必须对准 target_index 这一个指数**；即使你用 update_my_strategy 重写方法论，这一块也会被系统重新锚定，不接受改标的。`,
    '',
    TASK_HEADER_DELIM,
    '',
  ].join('\n')
}

// 从一份 METHODOLOGY.md 全文里剥掉顶部的任务头块（含分隔线），只留正文。找不到任务头就原样返回。
// 用于 update_my_strategy：bot 提交的 strategy 可能带也可能不带旧任务头，剥干净后再拼上权威新头，避免重复。
export function stripTaskHeader(full: string): string {
  const text = full.replace(/^﻿/, '')
  if (!text.trimStart().startsWith(TASK_HEADER_SENTINEL)) return full
  const lines = text.split('\n')
  // 找到任务头之后的第一条独占分隔线（---），其后即正文。
  let start = 0
  while (start < lines.length && !lines[start].startsWith(TASK_HEADER_SENTINEL)) start++
  for (let i = start + 1; i < lines.length; i++) {
    if (lines[i].trim() === TASK_HEADER_DELIM) {
      return lines.slice(i + 1).join('\n').replace(/^\n+/, '')
    }
  }
  return full
}

/** 注入 bot 前从 methodology 正文里剥掉 <!-- INJECT_SKIP_START --> ... <!-- INJECT_SKIP_END -->
 *  之间的段（含标记本身）。用于"引擎已接管、bot 不必读"的章节——比如日度状态机接管后的
 *  '主线识别 4 维度评分'、'主线→候选→选品三步收敛'——文件里保留作为背景/文档，但每天不注入
 *  bot（省 daily prompt token）。多对标记按顺序处理，不支持嵌套。
 *  没标记 → 原样返回。 */
export function stripInjectSkipBlocks(text: string): string {
  const START = '<!-- INJECT_SKIP_START -->'
  const END = '<!-- INJECT_SKIP_END -->'
  let out = text
  while (true) {
    const s = out.indexOf(START)
    if (s === -1) break
    const e = out.indexOf(END, s + START.length)
    if (e === -1) break  // 未闭合 → 保守起见不动，避免整段吃掉
    // 保留 START 之前 + END 之后（不额外补换行，让原文自然衔接）
    out = out.slice(0, s) + out.slice(e + END.length)
  }
  // 清一下相邻空行（策略市里连续三个 \n 会显得很奇怪）
  return out.replace(/\n{3,}/g, '\n\n')
}

export function renderActiveMethodology(input: ActiveStrategyRenderInput): string {
  const rawBody = readFileSync(input.strategy.methodologyPath, 'utf8').trimEnd()
  const body = stripInjectSkipBlocks(rawBody)
  return renderTaskHeader(input) + body + '\n'
}

export function renderStrategyCatalog(lib: StrategyLibrary): string {
  const rows = [...lib.strategies.values()]
    .sort((a, b) => a.id.localeCompare(b.id))
    .map(s => `| ${s.id} | ${s.title} | ${s.targetIndex} | ${s.defaultBuyableFundCodes.join(', ')} |`)
  return [
    '# STRATEGY_LIBRARY.md',
    '',
    '本文件列出本轮可见的共享指数产品策略。active 策略全文会注入当前 bot 的 METHODOLOGY.md；其他策略可通过 strategy-server 查询。',
    '',
    '| strategy_id | title | target_index | default_buyable_fund_codes |',
    '|---|---|---|---|',
    ...rows,
    '',
  ].join('\n')
}

export function copyStrategyLibraryToWorkspace(lib: StrategyLibrary, destWorkspace: string): void {
  cpSync(lib.root, join(destWorkspace, 'strategies', 'index-products'), { recursive: true })
}

