import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { JsonRpcStdioClient } from '../src/jsonrpc.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const STUB = join(HERE, 'helpers', 'stubBotServer.ts')

function start(env: Record<string, string> = {}) {
  const child = spawn(process.execPath, ['--experimental-strip-types', STUB, '--bot-id', 'bot7', '--workspace', '/ws/bot7'], { stdio: ['pipe', 'pipe', 'inherit'], env: { ...process.env, ...env } })
  return { child, client: new JsonRpcStdioClient(child.stdin!, child.stdout!) }
}

test('stub emits server.ready, answers ping and chat', async () => {
  const { child, client } = start({ STUB_REPLY_TEXT: 'hi' })
  const ready = await client.waitFor(n => n.method === 'server.ready', 2000)
  assert.equal(ready.params.bot_id, 'bot7')
  const pong = await client.request('ping', {})
  assert.equal((pong as { pong: boolean }).pong, true)
  const r = await client.request('chat', { message: 'today is 2024-03-15', session_key: 'trading-r1', history: [] }) as { reply: string; session_id: string }
  assert.equal(r.reply, 'hi')
  assert.equal(r.session_id, 'stub-trading-r1')
  child.kill()
})

test('stub error mode rejects chat; exit mode kills the process', async () => {
  const e = start({ STUB_CHAT_MODE: 'error' })
  await e.client.waitFor(n => n.method === 'server.ready', 2000)
  await assert.rejects(() => e.client.request('chat', { message: 'x' }), /stub chat error/)
  e.child.kill()

  const x = start({ STUB_CHAT_MODE: 'exit' })
  await x.client.waitFor(n => n.method === 'server.ready', 2000)
  const exited = new Promise<number | null>(res => x.child.on('exit', c => res(c)))
  void x.client.request('chat', { message: 'x' }).catch(() => {})
  assert.equal(await exited, 1)
})
