import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { DEFAULT_SHADOW_INCLUDE } from './config.ts'

export interface BuildShadowWorkspaceOptions {
  sourceDir: string
  destDir: string
  include?: string[]
  // ${KEY} -> value substitution applied to mcporter.json after copy. Keys not
  // present in this map are left as empty string in the output (so a bot config
  // referencing ${SIMWORLD_PROXY_URL} doesn't accidentally ship the literal
  // placeholder downstream if the proxy is disabled).
  templateVars?: Record<string, string>
  // 已存在时不从模板覆盖的 run 内可演化文件（相对 workspace 路径）。
  preserveExisting?: string[]
}

const RESEARCH_LOOP_CONFIG = new Set([
  'research-loop.yaml',
  'research-loop.yml',
  'research-loop.json',
])

function isResearchLoopConfigRel(rel: string): boolean {
  return rel.startsWith('config/') && RESEARCH_LOOP_CONFIG.has(rel.slice('config/'.length))
}

function applyTemplate(text: string, vars: Record<string, string>): string {
  return text.replace(/\$\{([A-Z0-9_]+)\}/g, (_, key) => (key in vars ? vars[key] : ''))
}

function copyMcporterWithTemplate(src: string, dst: string, vars: Record<string, string>): void {
  const text = readFileSync(src, 'utf8')
  writeFileSync(dst, applyTemplate(text, vars))
}

function copyConfigDirWithoutRuntimeOverrides(sourceDir: string, destDir: string, templateVars?: Record<string, string>): void {
  for (const entry of readdirSync(sourceDir)) {
    if (RESEARCH_LOOP_CONFIG.has(entry)) continue
    const src = join(sourceDir, entry)
    const dst = join(destDir, entry)
    const st = statSync(src)
    if (st.isDirectory()) {
      mkdirSync(dst, { recursive: true })
      copyConfigDirWithoutRuntimeOverrides(src, dst, templateVars)
    } else {
      mkdirSync(dirname(dst), { recursive: true })
      if (entry === 'mcporter.json' && templateVars) copyMcporterWithTemplate(src, dst, templateVars)
      else cpSync(src, dst)
    }
  }
}

export function buildShadowWorkspace(opts: BuildShadowWorkspaceOptions): void {
  const include = opts.include ?? DEFAULT_SHADOW_INCLUDE
  const preserveExisting = new Set(opts.preserveExisting ?? [])
  mkdirSync(opts.destDir, { recursive: true })
  for (const rel of include) {
    if (isResearchLoopConfigRel(rel)) continue
    const src = join(opts.sourceDir, rel)
    if (!existsSync(src)) continue
    const dst = join(opts.destDir, rel)
    if (preserveExisting.has(rel) && existsSync(dst)) continue
    mkdirSync(dirname(dst), { recursive: true })
    if (rel === 'config') {
      mkdirSync(dst, { recursive: true })
      copyConfigDirWithoutRuntimeOverrides(src, dst, opts.templateVars)
      continue
    }
    if (rel === 'config/mcporter.json' && opts.templateVars) {
      copyMcporterWithTemplate(src, dst, opts.templateVars)
      continue
    }
    cpSync(src, dst, { recursive: true })
  }
}
