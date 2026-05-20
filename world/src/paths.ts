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
// 策略修订审计日志：append-only JSONL。每次 update_my_strategy 调用追加一条
// {ts, reason, new_size, prior_size}。bot 的"策略"= shadow workspace 的 METHODOLOGY.md；这里
// 只保留审计目录与文件。文件可能不存在（bot 一直没调过 update_my_strategy）—— reader 必须 best-effort。
export const strategiesDir = (w: string, runId: string) => join(runDir(w, runId), 'strategies')
export const strategyRevisionsFile = (w: string, runId: string, bot: string) => join(strategiesDir(w, runId), `${bot}.revisions.jsonl`)
// strategy-server 进程信息 runtime 描述符（dashboard 可读）。
export const strategyServerRuntimeFile = (w: string, runId: string) => join(runDir(w, runId), 'strategy-server.json')

// Per-run 可买基金白名单文件。注意这是 GLOBAL data 路径（不在 runDir 下）——fund-portfolio-mcp
// 服务通过 FUND_BUYABLE_CODES_DIR env 直接读这个目录，不需要也不应该知道 world/runtime/runs
// 的内部结构。worldRoot = <repo>/world/runtime → '..','..','data','buyable' 落到 <repo>/data/buyable。
// 该路径必须和 lab-fund-{bot-only,readonly}.service 的 FUND_BUYABLE_CODES_DIR 保持一致（手工 sync）。
//
// 早期是单一全局文件 lab-fund-buyable.json，两 run 并发会互相覆盖（bot11 半导体被 bot16 黄金污染）。
// 现按 run_id 物理隔离，每 run 一个文件。
export const buyableCodesDir = (w: string) => join(w, '..', '..', 'data', 'buyable')
export const buyableCodesFile = (w: string, runId: string) => join(buyableCodesDir(w), `${runId}.json`)
// 历史受污染 run 的标记文件（scripts/detect-universe-contamination.py 写入；dashboard 贴标读取）。
export const universeContaminationFile = (w: string, runId: string) => join(runDir(w, runId), 'universe-contamination.json')
