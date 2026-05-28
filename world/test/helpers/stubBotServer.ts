#!/usr/bin/env -S node --experimental-strip-types
// 假 research-loop-ts JSON-RPC server。行为由环境变量控制：
//   STUB_CHAT_MODE = reply (默认) | error | exit | hang | chat_error
//     chat_error 模拟 rs research-loop 的 mid-flow LLM 错误：chat 正常 return（带 reply），
//     但 result.chat_error 被填上——run.ts 据此把当天标 'error' 而不是 'ok'。这是真实事故
//     里反复刷出"0s 垃圾日"的根因，单测必须能复现。
//   STUB_CHAT_DELAY_MS = 回复前延迟毫秒数（默认 0）
//   STUB_REPLY_TEXT    = 自定义 reply 文本（默认 "stub reply"）
//   STUB_TOOL_TRACE_LEN = 在 result.tool_trace 里塞这么多个假 tool entry（默认 1，模拟正常 bot
//     至少会调一次工具——run.ts 的"放松"判定按 tool_trace.length > 0 算当日推进；要测失败分支
//     必须显式设为 0）。
//   STUB_NOTIFY_TOOL_CALLS = chat handler 在响应之前先发这么多个 tool.call notification（默认 0）。
//     用于让 hang / exit 模式也能模拟"超时/挂掉前已经做过 tool 调用"的场景——BotServer 内部
//     listener 会计数，run.ts catch 分支据此走"放松"判定。
//   STUB_ECHO_WORLD_DATE_FILE = 1 → reply 末尾追加 "[world-date=<file 内容>]"，
//     用于断言 runLoop 在 chat 之前已把当天日期写进 WORLD_DATE_OVERRIDE_FILE。
// 启动即发 server.ready。支持 ping / chat / shutdown。
import { readFileSync } from 'node:fs'
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
    const notifyToolCalls = Math.max(0, Number(process.env.STUB_NOTIFY_TOOL_CALLS || '0') | 0)
    for (let i = 0; i < notifyToolCalls; i++) out({ method: 'tool.call', params: { name: `stub_tool_${i}` } })
    if (mode === 'exit') { setTimeout(() => process.exit(1), 5); return }
    if (mode === 'hang') return
    setTimeout(() => {
      if (mode === 'error') { out({ id, error: { code: -32000, message: 'stub chat error' } }); return }
      let echoed = replyText
      if (process.env.STUB_ECHO_WORLD_DATE_FILE === '1') {
        const path = process.env.WORLD_DATE_OVERRIDE_FILE
        let pinned = ''
        if (path) { try { pinned = readFileSync(path, 'utf8').trim() } catch { /* missing */ } }
        echoed = `${replyText} [world-date=${pinned}]`
      }
      out({ method: 'message.delta', params: { text: echoed } })
      const toolTraceLen = Math.max(0, Number(process.env.STUB_TOOL_TRACE_LEN ?? '1') | 0)
      const toolTrace = Array.from({ length: toolTraceLen }, (_, i) => ({ name: `stub_tool_${i}`, args: {}, result: 'ok' }))
      const result: Record<string, unknown> = {
        reply: echoed,
        session_id: `stub-${sessionKey}`,
        assistant_messages: [{ role: 'assistant', content: echoed }],
        tool_trace: toolTrace,
        usage: message.length,
        iterations: 1,
        truncated_by_iterations: false,
      }
      if (mode === 'chat_error') result.chat_error = process.env.STUB_CHAT_ERROR_TEXT || 'stub chat_error (LLM mid-flow)'
      out({ id, result })
    }, delayMs)
    return
  }
  if (id !== undefined) out({ id, error: { code: -32601, message: `unknown method ${req.method}` } })
})
