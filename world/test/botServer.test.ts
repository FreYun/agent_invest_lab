import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { BotServer } from '../src/botServer.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const STUB = join(HERE, 'helpers', 'stubBotServer.ts')

function argv(botId: string): string[] {
  return [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/ws/${botId}`]
}

test('BotServer.start awaits server.ready; ping and chat work; shutdown exits cleanly', async () => {
  const bs = await BotServer.start('bot7', { argv: argv('bot7'), readyTimeoutMs: 3000 })
  assert.equal(bs.botId, 'bot7')
  assert.equal((await bs.ping()).bot_id, 'bot7')
  const r = await bs.chat({ message: 'hello 2024-03-15', session_key: 'trading-r1', history: [] })
  assert.equal(r.reply, 'stub reply')
  assert.equal(r.iterations, 1)
  await bs.shutdown({ timeoutMs: 2000 })
  assert.equal(bs.alive, false)
})

test('chat timeout is reported but server stays alive', async () => {
  const bs = await BotServer.start('bot7', { argv: [...argv('bot7')], readyTimeoutMs: 3000, env: { STUB_CHAT_MODE: 'hang' } })
  await assert.rejects(() => bs.chat({ message: 'x' }, { timeoutMs: 80 }), /timeout/i)
  assert.equal(bs.alive, true)
  await bs.shutdown({ timeoutMs: 2000 })
})

test('chat error rejects; unexpected process exit flips alive to false and rejects pending', async () => {
  const errSrv = await BotServer.start('bot7', { argv: argv('bot7'), env: { STUB_CHAT_MODE: 'error' } })
  await assert.rejects(() => errSrv.chat({ message: 'x' }), /stub chat error/)
  await errSrv.shutdown({ timeoutMs: 2000 })

  const exitSrv = await BotServer.start('bot7', { argv: argv('bot7'), env: { STUB_CHAT_MODE: 'exit' } })
  await assert.rejects(() => exitSrv.chat({ message: 'x' }), /exit|closed/i)
  assert.equal(exitSrv.alive, false)
})
