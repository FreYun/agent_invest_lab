#!/usr/bin/env node --experimental-strip-types
// 把回测里的 mem0 记忆服务单独拉出来长驻，给 rsloop 直连用。
//
// 回测里这个服务是 setup() 现起现关的，端口随机、生命周期跟着 run 走，getCurrentDate 返回
// 的是「世界当前日期」。人跟 bot 分身聊天时没有世界、也没有 run，日期就是真实今天。
//
//   node --experimental-strip-types src/memory-server/standalone.ts \
//     --store /path/to/store.jsonl --port 18196 [--readonly]
//
// 关掉 --port 或传 0 会随机选端口并把 URL 打到 stdout。
//
// --readonly：给「跟正在跑的 run 共用同一个 store」的旁观者用（bot105d 聊天分身）。
//   ① 拒绝所有写入。聊天写进去的东西会污染 OOS 实验的记忆，而且 MemoryStore.add 是
//      appendFileSync + 内存 push，跟 run 自己那个 server 各持一份内存副本，互相看不见
//      对方的追加，谁后写谁把对方的内存状态坐实成错的。
//   ② 按 mtime 热重载。MemoryStore 只在构造时读一次文件，run 白天一直在追加；不重载的话
//      分身看到的永远是服务启动那一刻的快照，越聊越旧。

import { statSync } from 'node:fs'
import { MemoryStore } from './store.ts'
import { createMemoryServer } from './server.ts'

function argValue(flag: string): string | undefined {
  const i = process.argv.indexOf(flag)
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : undefined
}

const storeFile = argValue('--store')
if (!storeFile) {
  console.error('usage: standalone.ts --store <store.jsonl> [--port N] [--host H] [--readonly]')
  process.exit(2)
}
const port = Number(argValue('--port') ?? 0)
const host = argValue('--host') ?? '127.0.0.1'
const readonly = process.argv.includes('--readonly')

function mtimeOf(file: string): number {
  try { return statSync(file).mtimeMs } catch { return -1 }
}

// backing 始终是真正的 MemoryStore；readonly 时对外暴露的是它上面的一层门面。
// 两个变量不能合一：门面赋回同一个变量的话，mtime 没变的那条路径会返回门面自己，search 无限递归。
let backing = new MemoryStore(storeFile)
let loadedMtime = mtimeOf(storeFile)

function currentBacking(): MemoryStore {
  const m = mtimeOf(storeFile)
  if (m !== loadedMtime) {
    backing = new MemoryStore(storeFile)
    loadedMtime = m
    console.log(`reloaded store (mtime changed) ${storeFile}`)
  }
  return backing
}

// createMemoryServer 只用到 add / search 两个方法，这里换一个同形状的门面。
// MemoryStore 有私有字段，结构化类型对不上，只能强转。
const store: MemoryStore = readonly
  ? ({
      add() { throw new Error(`memory store is read-only (${storeFile})`) },
      search: (q: string, opts: Parameters<MemoryStore['search']>[1]) => currentBacking().search(q, opts),
    } as unknown as MemoryStore)
  : backing

const handle = await createMemoryServer({
  store,
  // 真实今天。search 的 recency 衰减靠它，聊天场景下没有模拟日期可言。
  getCurrentDate: () => new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' }),
  port,
  host,
})
console.log(`memory server listening ${handle.url} store=${storeFile}${readonly ? ' [readonly, hot-reload on mtime]' : ''}`)

for (const sig of ['SIGINT', 'SIGTERM'] as const) {
  process.on(sig, () => { void handle.close().then(() => process.exit(0)) })
}
