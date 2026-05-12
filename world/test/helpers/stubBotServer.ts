#!/usr/bin/env -S node --experimental-strip-types
// 假 research-loop-ts JSON-RPC server。行为由环境变量控制：
//   STUB_CHAT_MODE = reply (默认) | error | exit | hang
//   STUB_CHAT_DELAY_MS = 回复前延迟毫秒数（默认 0）
//   STUB_REPLY_TEXT    = 自定义 reply 文本（默认 "stub reply"）
// 启动即发 server.ready。支持 ping / chat / shutdown。
import { createInterface } from 'node:readline'

const args = process.argv.slice(2)
function arg(name: string): string {
  const i = args.indexOf(name)
  return i >= 0 ? (args[i + 1] ?? '') : ''
}
const botId = arg('--bot-id') || 'botX'
const workspace = arg('--workspace') || ''
const mode = process.env.STUB_CHAT_MODE || 'reply'
const delayMs = Number(process.env.STUB_CHAT_DELAY_MS || '0')
const replyText = process.env.STUB_REPLY_TEXT ?? 'stub reply'

const out = (obj: Record<string, unknown>) => process.stdout.write(JSON.stringify(obj) + '\n')
out({ method: 'server.ready', params: { bot_id: botId, workspace, model: 'stub', pid: process.pid } })

const rl = createInterface({ input: process.stdin })
rl.on('line', (line) => {
  const t = line.trim()
  if (!t) return
  const req = JSON.parse(t) as { id?: unknown; method?: string; params?: Record<string, unknown> }
  const id = req.id
  if (req.method === 'ping') { out({ id, result: { pong: true, bot_id: botId, workspace, model: 'stub' } }); return }
  if (req.method === 'shutdown') { out({ id, result: { ok: true } }); setTimeout(() => process.exit(0), 10); return }
  if (req.method === 'chat') {
    const message = String(req.params?.message ?? '')
    const sessionKey = String(req.params?.session_key ?? '')
    if (mode === 'exit') { setTimeout(() => process.exit(1), 5); return }
    if (mode === 'hang') return
    setTimeout(() => {
      if (mode === 'error') { out({ id, error: { code: -32000, message: 'stub chat error' } }); return }
      out({ method: 'message.delta', params: { text: replyText } })
      out({
        id,
        result: {
          reply: replyText,
          session_id: `stub-${sessionKey}`,
          assistant_messages: [{ role: 'assistant', content: replyText }],
          tool_trace: [],
          usage: message.length,
          iterations: 1,
          truncated_by_iterations: false,
        },
      })
    }, delayMs)
    return
  }
  if (id !== undefined) out({ id, error: { code: -32601, message: `unknown method ${req.method}` } })
})
