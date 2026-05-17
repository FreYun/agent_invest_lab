import { join } from 'node:path'

export const runDir = (w: string, runId: string) => join(w, 'runs', runId)
export const runStateFile = (w: string, runId: string) => join(runDir(w, runId), 'state.json')
export const calendarFile = (w: string) => join(w, 'calendar.json')
export const dayDir = (w: string, date: string) => join(w, 'days', date)
export const quotesFile = (w: string, date: string) => join(dayDir(w, date), 'quotes.json')
export const overviewFile = (w: string, date: string) => join(dayDir(w, date), 'overview.md')
export const eventsFile = (w: string, date: string) => join(dayDir(w, date), 'events.json')
export const runConfigFile = (w: string, runId: string) => join(runDir(w, runId), 'trading-rl-config.json')
export const rlOpenclawDir = (w: string, runId: string) => join(runDir(w, runId), 'rl-openclaw')
export const workspacesDir = (w: string, runId: string) => join(runDir(w, runId), 'workspaces')
export const shadowWorkspaceDir = (w: string, runId: string, bot: string) => join(workspacesDir(w, runId), bot)
export const memoryDir = (w: string, runId: string) => join(runDir(w, runId), 'memory')
export const memoryStoreFile = (w: string, runId: string) => join(memoryDir(w, runId), 'store.jsonl')
export const memoryRuntimeFile = (w: string, runId: string) => join(memoryDir(w, runId), 'runtime.json')
export const simworldProxyRuntimeFile = (w: string, runId: string) => join(runDir(w, runId), 'simworld-proxy.json')
export const fundPortfolioProxyRuntimeFile = (w: string, runId: string) => join(runDir(w, runId), 'fund-portfolio-proxy.json')
export const botDayDir = (w: string, runId: string, date: string, bot: string) => join(runDir(w, runId), date, bot)
export const sentFile = (w: string, runId: string, date: string, bot: string) => join(botDayDir(w, runId, date, bot), 'sent.md')
export const replyFile = (w: string, runId: string, date: string, bot: string) => join(botDayDir(w, runId, date, bot), 'reply.json')
export const statusFile = (w: string, runId: string, date: string, bot: string) => join(botDayDir(w, runId, date, bot), 'status.json')
export const runLogFile = (w: string, runId: string) => join(runDir(w, runId), 'run.log')
export const summaryFile = (w: string, runId: string) => join(runDir(w, runId), 'summary.json')
export const stopFile = (w: string, runId: string) => join(runDir(w, runId), 'STOP')
// One-line YYYY-MM-DD pin for loop processes (research-loop reads this via
// WORLD_DATE_OVERRIDE_FILE) so their system prompt shows the world day, not host wall-clock.
export const worldDateOverrideFile = (w: string, runId: string) => join(runDir(w, runId), 'world-date')
export const piSessionsDir = (w: string, runId: string) => join(runDir(w, runId), 'pi-sessions')
// Day 1 结束后，world 从 mem0 抽出 bot 写的策略文档（text 以 `# MY_STRATEGY` 开头的记录），
// 持久化为 markdown 文件；Day N 渲染 prompt 时读回来注入。每个 bot 一个文件，缺失就跳过 strategyBlock。
export const strategiesDir = (w: string, runId: string) => join(runDir(w, runId), 'strategies')
export const strategyFile = (w: string, runId: string, bot: string) => join(strategiesDir(w, runId), `${bot}.md`)
// 策略修订审计日志：append-only JSONL。每次 update_my_strategy 调用追加一条
// {ts, reason, new_size, prior_size}。用于事后审计 bot 在什么世界日做了什么改动、改的理由是什么。
// 文件可能不存在（Day 1 写完后 bot 一直没 update）—— reader 必须 best-effort。
export const strategyRevisionsFile = (w: string, runId: string, bot: string) => join(strategiesDir(w, runId), `${bot}.revisions.jsonl`)
// strategy-server 进程信息 runtime 描述符（dashboard 可读）。
export const strategyServerRuntimeFile = (w: string, runId: string) => join(runDir(w, runId), 'strategy-server.json')
