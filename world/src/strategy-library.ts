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

export function renderActiveMethodology(input: ActiveStrategyRenderInput): string {
  const { botId, strategy, buyableFundCodes } = input
  const body = readFileSync(strategy.methodologyPath, 'utf8').trimEnd()
  const codes = buyableFundCodes.join(', ')
  return [
    '# 当前回测任务',
    '',
    `- bot_id: ${botId}`,
    `- strategy_id: ${strategy.id}`,
    `- strategy_title: ${strategy.title}`,
    `- target_index: ${strategy.targetIndex}`,
    `- buyable_fund_codes: ${codes}`,
    '',
    '---',
    '',
    body,
    '',
  ].join('\n')
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

