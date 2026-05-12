import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { JsonRpcStdioClient } from '../src/jsonrpc.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const ECHO = join(HERE, 'helpers', 'echoRpc.ts')

function startEcho() {
  const child = spawn(process.execPath, ['--experimental-strip-types', ECHO], { stdio: ['pipe', 'pipe', 'inherit'] })
  return { child, client: new JsonRpcStdioClient(child.stdin!, child.stdout!) }
}

test('request returns result; notifications are delivered; waitFor catches the ready notification', async () => {
  const { child, client } = startEcho()
  const ready = await client.waitFor(n => n.method === 'ready', 2000)
  assert.deepEqual(ready.params, { hello: true })
  const notes: string[] = []
  client.onNotification(n => notes.push(n.method))
  const r1 = await client.request('echo', { a: 1 })
  assert.deepEqual(r1, { echo: { a: 1 } })
  const r2 = await client.request('note', {})
  assert.deepEqual(r2, { noted: true })
  assert.deepEqual(notes, ['progress'])
  child.kill()
})

test('request rejects on error response', async () => {
  const { child, client } = startEcho()
  await client.waitFor(n => n.method === 'ready', 2000)
  await assert.rejects(() => client.request('boom', {}), /boom/)
  child.kill()
})

test('request times out and a late reply is ignored', async () => {
  const { child, client } = startEcho()
  await client.waitFor(n => n.method === 'ready', 2000)
  await assert.rejects(() => client.request('slow', {}, { timeoutMs: 50 }), /timeout/i)
  // 等晚到的回复落地，不应抛未捕获异常
  await new Promise(r => setTimeout(r, 250))
  // 后续请求仍正常
  assert.deepEqual(await client.request('echo', { b: 2 }), { echo: { b: 2 } })
  child.kill()
})
