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

## 日界检查(runLoop 顶部)

现状([run.ts:561](../../../world/src/run.ts#L561)):

```
if (aborted || stopFile存在) { teardown 'aborted'; return }
```

改为(abort 优先于 pause):

```
if (aborted || stopFile存在) { teardown 'aborted'; return }
if (pauseFile存在)            { teardown 'paused';  return }
```

暂停在**下一个交易日开头**生效:当前交易日完整跑完(chat + settle + close_my_day +
state.cursor 推进)才停,保证当天产物落盘、cursor 不倒退。SIGINT 行为**不变**(仍 abort)。

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
  「pause requested — run "<id>" will halt after the current trading day.」。
- `main` switch 加 `case 'pause'`。
- HELP 文本加一行 `world pause --run-id <id> [--world-dir <dir>]   请求在当前交易日后暂停(可 resume)`。

## 测试

- `world/test/resume.test.ts`
  - 新增:跑若干天 → 写 PAUSE 哨兵让 runLoop 在日界 teardown 为 `paused` →
    `resumeWorld` 从 cursor 续完,断言 `status==='done'`、cursor 到底、被跳过的日子产物补齐。
  - 调整:现有「resumeWorld refuses when state status is not running」用例覆盖到 `done`
    与 `aborted` 仍被拒(`paused` 不再属于被拒集合)。
- `world/test/run.test.ts`(或新增 `world/test/pause.test.ts`)
  - `requestPause` 在 `running` 时写 PAUSE 哨兵、返回 ok;非 running 时拒绝。
  - runLoop 见 PAUSE 后 teardown 状态为 `paused` 且 `cursor` 不倒退。
  - `requestStop` 对 `paused` run 直接写 `aborted`。
- `world/test/cli.test.ts`
  - `parseCliArgs(['pause','--run-id','r1'])` → `command: 'pause'`。

## 不做(YAGNI)

- dashboard 暂停按钮(当前无 stop/resume UI)。
- 交易日中途(mid-day)暂停——隔离单元是「天」,中途暂停会丢当天 session 进度。
- orphan 自愈改写(当前未实现,不在本设计范围)。
