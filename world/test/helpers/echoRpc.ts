#!/usr/bin/env -S node --experimental-strip-types
// 行分隔 JSON-RPC 回声服务：启动即发一条 notification，然后对每个 {id, method, params} 回 {id, result}。
// method 'slow' → 延迟 200ms 再回；method 'boom' → 回 {id, error}；method 'note' → 先发一条 notification 再回 result。
import { createInterface } from 'node:readline'

process.stdout.write(JSON.stringify({ method: 'ready', params: { hello: true } }) + '\n')
const rl = createInterface({ input: process.stdin })
rl.on('line', (line) => {
  const t = line.trim()
  if (!t) return
  const req = JSON.parse(t) as { id?: unknown; method?: string; params?: Record<string, unknown> }
  const id = req.id
  const reply = (obj: Record<string, unknown>) => process.stdout.write(JSON.stringify(obj) + '\n')
  if (req.method === 'slow') { setTimeout(() => reply({ id, result: { ok: true } }), 200); return }
  if (req.method === 'boom') { reply({ id, error: { code: -32000, message: 'boom' } }); return }
  if (req.method === 'note') { reply({ method: 'progress', params: { step: 1 } }); reply({ id, result: { noted: true } }); return }
  reply({ id, result: { echo: req.params ?? null } })
})
