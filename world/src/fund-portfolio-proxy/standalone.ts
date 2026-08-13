#!/usr/bin/env node --experimental-strip-types
// 把回测里的 fund-portfolio-proxy 单独拉出来长驻，给 rsloop 直连用。
//
// 回测里这层是 run.ts 现起现关的，端口随机、runId 跟着 run 走。人跟 bot 分身聊天时没有 run
// 循环，但同样需要它——因为账本是按 (bot_id, run_id) 分区的，而 get_fund_holdings 这类工具
// 只要不传 run_id 就会把该 bot 所有 run 的持仓加在一起。bot105d 在库里有 7 个 run（1 个在跑
// 的 OOS + 6 个失败的），不钉死 run_id 的话分身看到的是 23 笔持仓、总资产 406 万；实际那个
// OOS run 只有 1 笔、154 万。
//
//   node --experimental-strip-types src/fund-portfolio-proxy/standalone.ts \
//     --upstream http://127.0.0.1:28271/mcp \
//     --run-id bot105d-daily-agenticdeep-charter-0424 [--port 28272]
//
// 关掉 --port 或传 0 会随机选端口并把 URL 打到 stdout。
//
// 刻意不接 getTradeDate：回测里那个是用来把 trade_date / as_of 钳到「世界当前日期」、防止
// bot 看到未来数据的。聊天场景没有世界日期，问的就是「现在」，钳了反而答不出实时持仓。
// 于是这里只做一件事：strip 掉 run_id 再强制注入，让分身既看不见也改不了它在查哪个 run。

import { createFundPortfolioProxy } from './server.ts'

function argValue(flag: string): string | undefined {
  const i = process.argv.indexOf(flag)
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : undefined
}

const upstreamUrl = argValue('--upstream')
const runId = argValue('--run-id')
if (!upstreamUrl || !runId) {
  console.error('usage: standalone.ts --upstream <url> --run-id <id> [--port N] [--host H]')
  process.exit(2)
}
const port = Number(argValue('--port') ?? 0)
const host = argValue('--host') ?? '127.0.0.1'

const handle = await createFundPortfolioProxy({ upstreamUrl, runId, port, host })
console.log(`fund-portfolio-proxy listening ${handle.url} upstream=${upstreamUrl} run_id=${runId}`)

for (const sig of ['SIGINT', 'SIGTERM'] as const) {
  process.on(sig, () => { void handle.close().then(() => process.exit(0)) })
}
