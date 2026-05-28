import { readFileSync } from 'node:fs'

// History window 超 budget 时，用主模型把"老的 60%"压缩到目标字符数。
// 模型 endpoint 来自 openclaw.json：models.providers.<primary-provider>.{baseUrl, apiKey}。
// primary 走 models.routes.default 或第一个 provider 兜底。

interface LlmEndpoint {
  baseUrl: string
  apiKey: string
  model: string
}

// 解析 openclaw.json，挑 default route 指向的 provider/model。
export function resolveLlmEndpoint(openclawJsonPath: string): LlmEndpoint {
  const cfg = JSON.parse(readFileSync(openclawJsonPath, 'utf8')) as Record<string, unknown>
  const models = (cfg.models ?? {}) as Record<string, unknown>
  const providers = (models.providers ?? {}) as Record<string, Record<string, unknown>>
  const routes = (models.routes ?? {}) as Record<string, unknown>
  const def = (routes.default ?? {}) as Record<string, unknown>
  const primary = typeof def.primary === 'string' ? def.primary : null
  let providerKey: string
  let model: string
  if (primary && primary.includes('/')) {
    const idx = primary.indexOf('/')
    providerKey = primary.slice(0, idx)
    model = primary.slice(idx + 1)
  } else {
    const keys = Object.keys(providers)
    if (!keys.length) throw new Error(`resolveLlmEndpoint: no providers in ${openclawJsonPath}`)
    providerKey = keys[0]
    const provModels = providers[providerKey].models as { id?: string }[] | undefined
    if (!provModels || !provModels.length) throw new Error(`resolveLlmEndpoint: provider "${providerKey}" has no models`)
    model = provModels[0].id ?? ''
  }
  const prov = providers[providerKey]
  if (!prov) throw new Error(`resolveLlmEndpoint: provider "${providerKey}" not found`)
  const baseUrl = typeof prov.baseUrl === 'string' ? prov.baseUrl : ''
  const apiKey = typeof prov.apiKey === 'string' ? prov.apiKey : ''
  if (!baseUrl || !apiKey || !model) throw new Error(`resolveLlmEndpoint: incomplete config for "${providerKey}" (need baseUrl/apiKey/model)`)
  return { baseUrl, apiKey, model }
}

const COMPACT_SYSTEM_PROMPT = `你在帮一个量化交易 agent 维护"历史交易记忆"。
我会给你这个 agent 过去 N 个交易日的 session 记录（包含工具调用链 + 决策反思，已去掉工具结果原文）。

你的任务：把这些历史压缩成不超过 \${TARGET_CHARS} 中文字符的浓缩笔记，按以下 4 个维度组织——

1. **踏空 / 大回撤经验**：哪些日错过了机会 / 哪些日吃了大亏，原因是什么，下次如何避免。
2. **反复被收割的经验**：同一个错误模式（追高被套 / 抄底抄半山 / 频繁切换被双杀 / etc）出现了几次，what's the recurring trap。
3. **新范式出现的冲击**：宏观/政策/事件级别的非线性变化（如 2025-04 TACO 交易），agent 是否捕捉到、有没有及时调整 thesis。
4. **历次交易日的核心交易逻辑**：最重要的几个 thesis（"为什么这段时间持仓 X"），按时间排序简述演变。

输出格式（严格 markdown）：

### 踏空 / 大回撤经验
- ...

### 反复被收割的经验
- ...

### 新范式 / 范式冲击
- ...

### 交易逻辑演变
- ...

规则：
- 不要重述工具调用细节；只提炼出"经验 / 教训 / thesis"。
- 不要超过 \${TARGET_CHARS} 字符（严格）。
- 每一维度都要写，没有就写"暂无显著记录"。
- 中文输出。`

export interface CompactOptions {
  endpoint: LlmEndpoint
  digestsMarkdown: string  // 待压缩的历史 digest 拼接
  targetChars: number      // 期望压缩后字符上限
  timeoutMs?: number
}

export async function compactHistory(opts: CompactOptions): Promise<string> {
  const { endpoint, digestsMarkdown, targetChars } = opts
  const sys = COMPACT_SYSTEM_PROMPT.replace(/\$\{TARGET_CHARS\}/g, String(targetChars))
  const url = endpoint.baseUrl.replace(/\/+$/, '') + '/chat/completions'
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? 300_000)
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'authorization': `Bearer ${endpoint.apiKey}`,
      },
      body: JSON.stringify({
        model: endpoint.model,
        messages: [
          { role: 'system', content: sys },
          { role: 'user', content: digestsMarkdown },
        ],
        temperature: 0.3,
        stream: false,
      }),
      signal: controller.signal,
    })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      throw new Error(`compactHistory: HTTP ${res.status} ${res.statusText}: ${body.slice(0, 200)}`)
    }
    const data = await res.json() as { choices?: { message?: { content?: string } }[] }
    const content = data.choices?.[0]?.message?.content
    if (!content || typeof content !== 'string') throw new Error('compactHistory: empty content in response')
    // 硬截断兜底：模型偶尔超 budget。
    return content.length > targetChars + 500 ? content.slice(0, targetChars) + '\n…（已截断）' : content
  } finally {
    clearTimeout(timer)
  }
}
