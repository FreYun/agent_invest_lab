# 并发 World Runs 设计

**日期**：2026-05-15
**作者**：agent
**目标**：让 dashboard `+新建回测` 在已有 run 在跑时也能启动新 run，任意 bot 都可以同时跑多份。

---

## 背景

当前 dashboard 的 `+新建回测` 按钮在 `state.json.status === 'running'` 时被 disable；服务端起 run 接口在同样条件下返回 409。再往里看，单例 `worldRoot/state.json` 是单 run 假设的根因——run.ts 在多处写它，cli resume 默认读它，dashboard status 也只读它。

需求是"完全并发"：任意 bot 都可以同时跑多份 run。

## 总思路

消灭全局 `worldRoot/state.json` 单例：
- 每个 run 只写自己的 `runtime/runs/<runId>/state.json`
- 通过扫描 run 目录得到"活动 run 列表"
- piSessionsDir 也按 runId 隔离（同 bot 两 run 各自独立 session 树）
- memory store 保持 per-run 现状（同 bot 跨 run 不共享记忆，符合 backtest 复现性原则）

## 模块改动

### 1. `world/src/state.ts`

- 删除"全局 stateFile"概念
- `writeState(worldRoot, runId, state)`：写 `runtime/runs/<runId>/state.json`
- `readState(worldRoot, runId)`：读同上
- `stateExists(worldRoot, runId)`：检查同上
- 新增 `listActiveRuns(worldRoot): WorldState[]`：扫 `runtime/runs/*/state.json`，filter `status === 'running'`，按 `started_at` 降序

### 2. `world/src/paths.ts`

- 删除 `stateFile(w)`（全局版本）
- 新增 `runStateFile(w, runId) = join(runDir(w, runId), 'state.json')`

### 3. `world/src/run.ts`

- 所有 `writeState(worldRoot, ...)` / `readState(worldRoot)` 调用加 `runId` 参数
- 删除 finalize 阶段对全局 state.json 的写（line 432）；保留 line 433 对 runDir state.json 的写
- 失败路径同理（line 458）

### 4. `world/src/cli.ts`

- `world run`：删除 cli.ts:49-53 那段"归档上一个全局 state"的逻辑
- `world resume`：`--run-id <id>` 改为必填；找不到 runDir/state.json 直接 fail-fast；删除 stateExists(worldRoot) 调用
- usage 文本同步更新

### 5. `world/src/config.ts` / `world/src/run.ts`（piSessionsDir）

- 删除 `config.piSessionsDir` 全局字段
- 在 setup() 里直接计算：`piSessionsDirForRun = join(runDir, 'pi-sessions')`
- 替换所有 `config.piSessionsDir` 引用为这个 per-run 路径
- seed-once 仍从 `~/.openclaw/agents/<bot>` 拷一份进 per-run 目录
- 注意：world.yaml 里如果还有 `pi_sessions_dir`，给 deprecation warning，不再生效

### 6. `dashboard/server.js`

- `/api/world-backtest-start`：删 5086-5096 的 409 检查
- `/api/world-backtest-status`：
  - 输出加 `activeRuns: [{ runId, cursor, total, bots, started_at }, ...]`（按 started_at 降序）
  - 保留 `running/runId/cursor/total` 字段为 backward-compat，值取 activeRuns[0]（最新的 running run）
  - 数据源从 `runtime/state.json` 改成枚举 `runtime/runs/*/state.json`

### 7. `dashboard/world.html`

- `refreshBacktestStatus`：用 `d.activeRuns`，永远不 disable `$btOpen`
- hint：
  - 0 个 active：保持现状（显示 default range 提示）
  - ≥1 个 active：`N 个 run 正在跑（最早：runId）` —— 不展开列表，左侧 sidebar 已经在显示

## 兼容性 & 风险

### 兼容性
- 旧脚本 / shell alias 调用 `world resume --config xx`（不带 --run-id）会失败。Acceptance：在 PR 描述里高亮，让用户更新调用方。
- world.yaml 的 `pi_sessions_dir` 字段：保留解析但不生效，加 deprecation warning 到 setup log。
- dashboard `/api/world-backtest-status` 返回字段加了 activeRuns，没删旧字段，前端老缓存仍可读。

### 风险
- 扫描 `runtime/runs/*/state.json` 在 run 目录数量大时（>1000）有 IO 开销。当前 run 目录数 ≈30，无须优化；如果以后膨胀考虑加个 active runs 索引文件。
- 同 bot 两 run 并发会争抢同一 bot 的 ANTHROPIC_API_KEY rate limit。Acceptance：用户自行控制并发数。
- 同 bot 两 run 写各自 piSessionsDir 互不干扰，但 `~/.openclaw/agents/<bot>` 仍是同源 seed，第一次起 run 时 seed 拷贝可能竞争——加文件锁或 mkdir 原子性兜底。

## 测试

- `state.test.ts`：
  - `listActiveRuns` 在 0 / 1 / N 个 running run 时正确
  - status 不是 'running' 的 run 被排除
  - 损坏的 state.json 不破坏整次扫描
- `run.test.ts`：
  - run 不再写 `worldRoot/state.json`
  - finalize 后 runDir/state.json 状态正确
- `cli.test.ts`：
  - `world resume` 不带 --run-id 时 fail with 明确错误信息
  - 带 --run-id 但 dir 不存在时同样 fail
- 新增 `concurrent-runs.test.ts`（用 stubBotServer）：
  - 两个 runId 同时调用 runWorld，stub bot 都正常工作，state 互不踩
  - 第二个 run start 时 cli 不会把第一个 run 的 state 归档掉

## 不在本次范围

- dashboard 同时展示多个 run 的 transcript 视图——sidebar 已经有列表，单 transcript 区域仍然是"选哪个看哪个"
- 任何形式的 cross-run 数据聚合 / 比较视图
- 并发数上限 / quota 控制
- Memory mem0 跨 run 共享（明确 out of scope）
