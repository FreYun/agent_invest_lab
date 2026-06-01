import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { JsonRpcStdioClient } from '../src/jsonrpc.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const STUB = join(HERE, 'helpers', 'stubPiBotServer.ts')

function start(env: Record<string, string> = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'pi-stub-'))
  const oc = join(dir, 'openclaw.json'); writeFileSync(oc, '{}\n')
  const child = spawn(process.execPath, ['--experimental-strip-types', STUB, '--bot-id', 'bot7', '--workspace', '/ws/bot7', '--openclaw-json', oc], { stdio: ['pipe', 'pipe', 'inherit'], env: { ...process.env, ...env } })
  return { child, client: new JsonRpcStdioClient(child.stdin!, child.stdout!), oc }
}

test('pi stub emits server.ready, ping, chat → message.done + result', async () => {
  const { child, client } = start({ STUB_REPLY_TEXT: 'pi-hi' })
  const ready = await client.waitFor(n => n.method === 'server.ready', 2000)
  assert.equal(ready.params.bot_id, 'bot7')
  assert.equal((await client.request('ping', {}) as { pong: boolean }).pong, true)
  const done = client.waitFor(n => n.method === 'message.done', 2000)
  const r = await client.request('chat', { message: 'hi', session_key: 'tr-1' }) as { reply: string; session_id: string }
  assert.equal(r.reply, 'pi-hi')
  assert.equal(r.session_id, 'stub-pi-tr-1')
  assert.equal((await done).params.text, 'pi-hi')
  child.kill()
})

test('pi stub refuses to start without --openclaw-json', async () => {
  const child = spawn(process.execPath, ['--experimental-strip-types', STUB, '--bot-id', 'b', '--workspace', '/w'], { stdio: ['pipe', 'pipe', 'pipe'] })
  const code = await new Promise<number | null>(res => child.on('exit', c => res(c)))
  assert.equal(code, 2)
})

test('pi stub refuses to start when --openclaw-json does not exist', async () => {
  const child = spawn(process.execPath, ['--experimental-strip-types', STUB, '--bot-id', 'b', '--workspace', '/w', '--openclaw-json', '/tmp/definitely-does-not-exist-' + Date.now() + '.json'], { stdio: ['pipe', 'pipe', 'pipe'] })
  const code = await new Promise<number | null>(res => child.on('exit', c => res(c)))
  assert.equal(code, 2)
})
