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

// History window 压缩端点的首选来源：每个 bot 影子 workspace 的 research-loop.yaml
// （writeResearchLoopYaml 落的明文，内容是 JSON.stringify → 可直接 JSON.parse）。
// 默认取 model.primary.{base_url, model, api_key}；如果 llm.compact_model 存在，
// 压缩使用该模型，但仍复用 primary 的 provider/base_url/api_key，避免和 bot chat 主模型耦合。
export function resolveLlmEndpointFromRlConfig(rlConfigPath: string): LlmEndpoint {
  const cfg = JSON.parse(readFileSync(rlConfigPath, 'utf8')) as Record<string, unknown>
  const model = (typeof cfg.model === 'object' && cfg.model ? cfg.model : {}) as Record<string, unknown>
  const primary = (typeof model.primary === 'object' && model.primary ? model.primary : {}) as Record<string, unknown>
  const llm = (typeof cfg.llm === 'object' && cfg.llm ? cfg.llm : {}) as Record<string, unknown>
  const baseUrl = typeof primary.base_url === 'string' ? primary.base_url : ''
  const apiKey = typeof primary.api_key === 'string' ? primary.api_key : ''
  const modelId = typeof llm.compact_model === 'string' && llm.compact_model
    ? llm.compact_model
    : (typeof primary.model === 'string' ? primary.model : '')
  if (!baseUrl || !apiKey || !modelId) throw new Error(`resolveLlmEndpointFromRlConfig: incomplete model.primary in ${rlConfigPath} (need base_url/api_key/model)`)
  return { baseUrl, apiKey, model: modelId }
}

const COMPACT_SYSTEM_PROMPT = `你在帮一个量化交易 agent 维护"历史交易记忆"。
我会给你这个 agent 过去 N 个交易日的 session 记录（包含工具调用链 + 决策反思，已去掉工具结果原文）。

你的任务：把这些历史压缩成不超过 \${TARGET_CHARS} 中文字符的浓缩笔记，按以下 5 个维度组织——

1. **踏空 / 大回撤经验**：哪些日错过了机会 / 哪些日吃了大亏，原因是什么，下次如何避免。
2. **反复被收割的经验**：同一个错误模式（追高被套 / 抄底抄半山 / 频繁切换被双杀 / etc）出现了几次，what's the recurring trap。
3. **验证成功的打法（可复用条件）**：哪些操作事后被证明做对了。每条必须写齐三件事：当时的**触发条件**（什么信号组合）、**动作**（进/出多少）、**边界条件**（为什么下次仍可复用、什么情况下不适用）。纯运气的盈利不许入册——拿着不动恰好涨了不算，除非当时有明确的持有论据事后被验证。
4. **新范式出现的冲击**：宏观/政策/事件级别的非线性变化（如 2025-04 TACO 交易），agent 是否捕捉到、有没有及时调整 thesis。
5. **历次交易日的核心交易逻辑**：最重要的几个 thesis（"为什么这段时间持仓 X"），按时间排序简述演变。

输出格式（严格 markdown）：

### 踏空 / 大回撤经验
- ...

### 反复被收割的经验
- ...

### 验证成功的打法（可复用条件）
- ...

### 新范式 / 范式冲击
- ...

### 交易逻辑演变
- ...

规则：
- 不要重述工具调用细节；只提炼出"经验 / 教训 / thesis"。
- 不要超过 \${TARGET_CHARS} 字符（严格）。
- 每一维度都要写，没有就写"暂无显著记录"。
- 成功经验与失败教训对称对待：只记损失不记制胜打法会把 agent 推向过度保守。
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
