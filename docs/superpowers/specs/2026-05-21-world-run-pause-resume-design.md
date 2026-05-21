# world run 暂停 / 续跑设计

日期:2026-05-21
范围:`world/`(CLI + 核心 run 编排)

## 背景与问题

`world run` 把一段交易日区间逐日 replay。每个交易日是一个**隔离 chat session**:
session key = `agent:<bot>:trading-<run>-<date>`,且每天都以 `history: []` 起头
([run.ts:36](../../../world/src/run.ts#L36)、[run.ts:451](../../../world/src/run.ts#L451))。
`state.json` 的 `cursor` 记录「下一个要处理的交易日下标」,`resumeWorld` 已能从该
cursor 续跑([run.ts:689](../../../world/src/run.ts#L689))。也就是说,「停一下、之后接着跑」
所需的隔离性与进度记录**本来就具备**。

但当前状态机是拧的:

- 进程被 kill / 崩溃时,teardown 不执行,`state.status` 停在 `running` → `resume` 反而能用
  (resume.test.ts 用 stale pid 模拟这条路径)。
- 用 `world stop` 优雅停止时,runLoop 在日界发现 STOP 哨兵,`teardown(..., 'aborted')`
  ([run.ts:561](../../../world/src/run.ts#L561));而 `resumeWorld` 要求
  `status === 'running'`,于是 `aborted` 被拒,**优雅停下来的 run 反而续不了**。

补充事实:仓库目前**没有**实现 orphan 自愈逻辑——`state.ts` 里关于「dashboard 自愈到
aborted」只是注释/愿景,`pid` 字段只盖章未被消费。dashboard / 运维脚本里也没有任何
stop/resume 控件。所以本设计不与既有自愈逻辑冲突,改动面纯在 CLI + run 核心。

## 目标

给 `world run` 加一条「优雅暂停且保持可续」的语义,与既有「优雅中止不续」区分开。

## 状态模型

`RunStatus` 枚举新增 `'paused'`([state.ts:5](../../../world/src/state.ts#L5)):

| status    | 含义                              | 可 resume? |
|-----------|-----------------------------------|-----------|
| `setup`   | 启动中                            | 否        |
| `running` | 活着;或崩溃残留(teardown 未跑)  | **是**(保留崩溃恢复)|
| `paused`  | 用户暂停,等待续跑                | **是**(本设计新增)|
| `done`    | 跑完                              | 否        |
| `aborted` | 优雅中止(`world stop` / Ctrl-C) | 否        |
| `failed`  | 出错                              | 否        |

三分语义:`paused` = 我过会儿再续;`aborted` = 我不跑了;`running` = 活着或崩溃残留。

## 触发机制(哨兵文件)

沿用现有 STOP 哨兵的模式,新增 PAUSE 哨兵:

- `paths.pauseFile = runDir/PAUSE`,与 [paths.ts:25](../../../world/src/paths.ts#L25) 的
  `stopFile = runDir/STOP` 并列。
- 新增 `requestPause(worldRoot, runId)`(镜像 `requestStop`,
  [run.ts:715](../../../world/src/run.ts#L715)):仅当 `status === 'running'` 时写 PAUSE 哨兵;
  否则返回 `{ ok: false, reason }`。

## 立即暂停(runLoop:哨兵 poller + 日界检查)

设计取舍(关键):暂停**立即生效**,不等当天 chat 跑完。因为每个交易日是隔离 session,
当天可以整段废弃、resume 时从头重跑——所以暂停 = 杀掉当前在跑的那一天,cursor 不推进。

`world stop` / SIGINT 维持原语义:**等当天跑完**、在日界 `teardown 'aborted'`(终态、不可续)。

实现:在 runLoop 起一个 1s 间隔的 poller 轮询 PAUSE 哨兵:

```
let pauseRequested = false
const pausePoll = setInterval(() => {
  if (!pauseRequested && pauseFile存在) {
    pauseRequested = true
    for (const b of bots) b.server.shutdown()   // 杀 bot → in-flight chat 因 stdout 关闭而 reject
  }
}, 1000)
```

bot server 进程被 kill 后,正在 await 的 `chatOneBot` 请求 reject([botServer.ts:50-52](../../../world/src/botServer.ts#L50-L52)),
被捕获成 `dead` 状态,`mapWithConcurrency` 随即 resolve。然后循环在三处 bail 到 `paused`:

1. 日界顶部(处理"两日之间/settle 期间"请求的 pause):
   `if (pauseRequested || pauseFile存在) { teardown 'paused'; return }`(放在 STOP 检查**之后**,abort 优先)。
2. chat 之后、`close_my_day` 与 cursor 推进**之前**:`if (pauseRequested) { teardown 'paused'; return }`。

两处都在 `writeState(cursor+1)` 之前返回,所以 `state.cursor` 停在当前在跑的那一天;
resume `fromCursor = state.cursor` 即从该日整段重跑。poller 在 `finally` 里 `clearInterval`。

注意(已知取舍):若当天 bot 在被杀前已经下过单(order_date=today),resume 重跑当天可能重复下单。
fund run 需自行接受或在 resume 前清理当天残留单——本设计按用户决策"重跑当天"执行。

## 续跑(resumeWorld)

[run.ts:692](../../../world/src/run.ts#L692) 门禁放宽:

- 现:`if (state.status !== 'running') throw`
- 改:允许 `status === 'running' || status === 'paused'`;其余(`done`/`aborted`/`failed`/
  `setup`)继续拒,错误信息保持「nothing to resume」风格。

清哨兵([run.ts:697](../../../world/src/run.ts#L697)):resume 时把 STOP **和** PAUSE 一起
`rmSync(..., { force: true })`,否则刚 resume 又被残留哨兵在日界拦停。

其余逻辑不变:loop 一致性校验、交易日序列校验、收养 `pid`、`fromCursor: state.cursor`。

## `world stop` 对 paused run 的处理

`requestStop`([run.ts:715](../../../world/src/run.ts#L715))扩展为也接受 `paused`:

- `status === 'running'`:写 STOP 哨兵(由 runLoop 在日界消费),行为不变。
- `status === 'paused'`:**没有进程在跑**,没人会消费哨兵 → 直接把 `state.json` 写成
  `status: 'aborted'`(终态),语义即「取消一个暂停的 run」。
- 其余 status:拒绝(不变)。

## CLI

[cli.ts](../../../world/src/cli.ts) 新增 `pause` 命令,对称于 `stop`:

- `CliArgs.command` 联合类型加 `'pause'`。
- `parseCliArgs`:`cmd === 'pause'` 纳入已知命令分支。
- `cmdPause(args)`:校验 `--run-id`,调 `requestPause`,成功打印
  「pause requested — run "<id>" will halt the current trading day (resume re-runs it).」。
- `main` switch 加 `case 'pause'`。
- HELP 文本加一行 `world pause --run-id <id> [--world-dir <dir>]   请求暂停(立即终止当前交易日,可 resume,从该日重跑)`。

## 测试

- `world/test/resume.test.ts`
  - 新增:跑若干天 → 写 PAUSE 哨兵让 runLoop 在日界 teardown 为 `paused` →
    `resumeWorld` 从 cursor 续完,断言 `status==='done'`、cursor 到底、被跳过的日子产物补齐。
  - 调整:现有「resumeWorld refuses when state status is not running」用例覆盖到 `done`
    与 `aborted` 仍被拒(`paused` 不再属于被拒集合)。
- `world/test/run.test.ts`
  - `requestPause` 在 `running` 时写 PAUSE 哨兵、返回 ok;非 running 时拒绝。
  - 日界:PAUSE 哨兵在 run 前就存在 → teardown `paused`、cursor=0、无当天产物。
  - mid-day:`STUB_CHAT_MODE=hang` 的慢 chat 在飞行中写 PAUSE → poller 杀 bot →
    run 以 `paused` 结束、cursor 不推进(证明立即生效)。
  - `requestStop` 对 `paused` run 直接写 `aborted`。
- `world/test/cli.test.ts`
  - `parseCliArgs(['pause','--run-id','r1'])` → `command: 'pause'`。

## 不做(YAGNI)

- dashboard 暂停按钮(当前无 stop/resume UI)。
- 跨重跑的"当天残留单"清理——按用户决策接受重跑当天的副作用(见上文取舍)。
- orphan 自愈改写(当前未实现,不在本设计范围)。
