import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { shadowWorkspaceDir, runConfigFile, rlOpenclawDir } from './paths.ts'

/** 某个 run 里某个 bot「实际生效」的模型标识——只含供应商与模型名，绝不含 api_key。 */
export interface RunModelInfo {
  /** 友好供应商名（东方财富 / 火山引擎 / …）；认不出时回落到域名或原始 provider 字段。 */
  provider: string
  /** 模型名，如 qwen3.6-plus。 */
  model: string
  /** 原始 base_url（网关地址），可能为空；仅作 tooltip，不直接当主显示。 */
  baseUrl: string
}

/** base_url 域名 → 友好供应商名。认不出就返回域名本身（null 表示连域名都没有）。 */
function providerFromBaseUrl(baseUrl: string): string | null {
  if (!baseUrl) return null
  let host = baseUrl
  try { host = new URL(baseUrl).host } catch { /* 非法 URL，原样当 host */ }
  const h = host.toLowerCase()
  const table: [string, string][] = [
    ['eastmoney', '东方财富'],
    ['volceapi', '火山引擎'],
    ['volces', '火山引擎'],
    ['dashscope', '阿里云百炼'],
    ['aliyun', '阿里云百炼'],
    ['moonshot', 'Moonshot'],
    ['bigmodel', '智谱'],
    ['zhipu', '智谱'],
    ['deepseek', 'DeepSeek'],
    ['openai.com', 'OpenAI'],
    ['anthropic', 'Anthropic'],
  ]
  for (const [k, name] of table) if (h.includes(k)) return name
  return host || null
}

interface RawModel { provider?: string; model?: string; baseUrl?: string }

/** research-loop.yaml / trading-rl-config.json 实际都是 JSON 内容（YAML 是 JSON 超集，rust 也能读）。
 *  先按 JSON 解析取 model.primary；万一是真 YAML，回退到逐行抓 base_url/model/provider。 */
function parseModelPrimary(raw: string): RawModel | null {
  let obj: unknown = null
  try { obj = JSON.parse(raw) } catch { /* fall through to line scan */ }
  const primary = (obj as { model?: { primary?: Record<string, string> } })?.model?.primary
  if (primary && typeof primary === 'object') {
    return { provider: primary.provider, model: primary.model, baseUrl: primary.base_url }
  }
  const grab = (key: string): string | undefined => {
    const m = raw.match(new RegExp(`["']?${key}["']?\\s*[:=]\\s*["']?([^"'\\n,}]+)`))
    return m ? m[1].trim() : undefined
  }
  const model = grab('model')
  const baseUrl = grab('base_url')
  const provider = grab('provider')
  return (model || baseUrl) ? { provider, model, baseUrl } : null
}

function readJsonish(file: string): RawModel | null {
  if (!existsSync(file)) return null
  try { return parseModelPrimary(readFileSync(file, 'utf8')) } catch { return null }
}

/** rl-openclaw/openclaw.json 回退：agents.list 里匹配 botId 的 model.primary（"<provider>/<model>" 字符串），
 *  再用 models.providers[provider].baseUrl 还原网关域名 → 友好供应商名。仅用于没生成 research-loop.yaml 的老 run。 */
function fromOpenclawJson(worldRoot: string, runId: string, botId: string): RawModel | null {
  const f = join(rlOpenclawDir(worldRoot, runId), 'openclaw.json')
  if (!existsSync(f)) return null
  try {
    const j = JSON.parse(readFileSync(f, 'utf8'))
    const list: Array<Record<string, unknown>> = j?.agents?.list ?? []
    const hit = list.find((a) => {
      const id = String(a.id || a.agentId || '')
      const dir = String(a.agentDir || '')
      return id === botId || dir.endsWith('/' + botId) || dir.endsWith('/' + botId + '/agent')
    })
    const primary = (hit?.model as { primary?: string })?.primary
    if (typeof primary === 'string' && primary.includes('/')) {
      const [prov, ...rest] = primary.split('/')
      const baseUrl = j?.models?.providers?.[prov]?.baseUrl
      return { provider: prov, model: rest.join('/'), baseUrl: typeof baseUrl === 'string' ? baseUrl : undefined }
    }
  } catch { /* ignore */ }
  return null
}

/** 读出某个 run 里某个 bot「实际生效」的模型供应商 + 模型名（不含 key）。
 *  优先级：research-loop.yaml（rust 运行时真实读的）> trading-rl-config.json > rl-openclaw/openclaw.json。
 *  全部取不到返回 null（前端不渲染该块）。 */
export function readRunModel(worldRoot: string, runId: string, botId: string): RunModelInfo | null {
  const raw = readJsonish(join(shadowWorkspaceDir(worldRoot, runId, botId), 'config', 'research-loop.yaml'))
        ?? readJsonish(runConfigFile(worldRoot, runId))
        ?? fromOpenclawJson(worldRoot, runId, botId)
  if (!raw || (!raw.model && !raw.baseUrl)) return null
  const provider = providerFromBaseUrl(raw.baseUrl || '') || raw.provider || ''
  return { provider, model: raw.model || '', baseUrl: raw.baseUrl || '' }
}
