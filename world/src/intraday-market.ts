import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join } from 'node:path'

export interface IntradayReportInputs {
  context?: string
  mainline?: string
  rotation?: string
}

export interface IntradayInstrument {
  secid: string
  name: string
  group: '宽基' | '主线代理'
  source: string
  note: string
}

type QuoteRecord = Record<string, unknown>

const BROAD_INDICES: IntradayInstrument[] = [
  { secid: '1.000001', name: '上证指数', group: '宽基', source: 'market_context 主要指数', note: '宽基指数' },
  { secid: '1.000300', name: '沪深300', group: '宽基', source: 'market_context 主要指数', note: '宽基指数' },
  { secid: '0.399006', name: '创业板指', group: '宽基', source: 'market_context 主要指数', note: '宽基指数' },
  { secid: '1.000688', name: '科创50', group: '宽基', source: 'market_context 主要指数', note: '宽基指数' },
  { secid: '1.000852', name: '中证1000', group: '宽基', source: 'market_context 主要指数', note: '宽基指数' },
]

const FUND_PROXY: Record<string, IntradayInstrument> = {
  '007818': { secid: '1.515880', name: '通信ETF国泰', group: '主线代理', source: '007818 通信设备联接', note: '同跟踪指数 931160 通信设备的场内 ETF 代理' },
  '007301': { secid: '1.512480', name: '半导体ETF国联安', group: '主线代理', source: '007301 半导体联接', note: '同基金公司/同跟踪指数 H30184 半导体的场内 ETF 代理' },
  '008888': { secid: '0.159995', name: '芯片ETF华夏', group: '主线代理', source: '008888 国证芯片联接', note: '同基金公司/同跟踪指数 980017 国证芯片的场内 ETF 代理' },
  '017472': { secid: '0.159667', name: '工业母机ETF国泰', group: '主线代理', source: '017472 中证机床联接', note: '同基金公司/同跟踪指数 931866 中证机床的场内 ETF 代理' },
  '019633': { secid: '0.159516', name: '半导体设备ETF国泰', group: '主线代理', source: '019633 半导体设备联接', note: '同基金公司/同跟踪指数 931743 半导体材料设备的场内 ETF 代理' },
}

const SECTOR_PROXY: Record<string, IntradayInstrument> = {
  'BK1128.DC': { secid: '1.515880', name: '通信ETF国泰', group: '主线代理', source: 'BK1128.DC CPO概念', note: 'CPO/光通信主线的通信设备 ETF 代理' },
  'BK1136.DC': { secid: '1.515880', name: '通信ETF国泰', group: '主线代理', source: 'BK1136.DC 光通信模块', note: '光通信主线的通信设备 ETF 代理' },
  'BK0877.DC': { secid: '0.159667', name: '工业母机ETF国泰', group: '主线代理', source: 'BK0877.DC PCB', note: '沿报告选出的中证机床载体做代理' },
  'BK1036.DC': { secid: '1.512480', name: '半导体ETF国联安', group: '主线代理', source: 'BK1036.DC 半导体', note: '半导体主线场内 ETF 代理' },
  'BK1137.DC': { secid: '0.159995', name: '芯片ETF华夏', group: '主线代理', source: 'BK1137.DC 存储芯片', note: '芯片/存储主线场内 ETF 代理' },
  'BK1152.DC': { secid: '0.159995', name: '芯片ETF华夏', group: '主线代理', source: 'BK1152.DC 高带宽内存', note: '芯片/存储主线场内 ETF 代理' },
  'BK1325.DC': { secid: '0.159516', name: '半导体设备ETF国泰', group: '主线代理', source: 'BK1325.DC 半导体材料', note: '半导体材料设备 ETF 代理' },
  'BK1326.DC': { secid: '0.159516', name: '半导体设备ETF国泰', group: '主线代理', source: 'BK1326.DC 半导体设备', note: '半导体设备主线场内 ETF 代理' },
  'BK1331.DC': { secid: '0.159995', name: '芯片ETF华夏', group: '主线代理', source: 'BK1331.DC 数字芯片设计', note: '芯片设计主线场内 ETF 代理' },
}

const KEYWORD_PROXY: Array<{ re: RegExp; instrument: IntradayInstrument }> = [
  { re: /集成电路/, instrument: { secid: '0.159546', name: '集成电路ETF国泰', group: '主线代理', source: '集成电路', note: '集成电路主线场内 ETF 代理' } },
  { re: /消费电子/, instrument: { secid: '0.159732', name: '消费电子ETF华夏', group: '主线代理', source: '消费电子', note: '消费电子主线场内 ETF 代理' } },
]

export function buildIntradayQuoteBasket(reports?: IntradayReportInputs): IntradayInstrument[] {
  const bySecid = new Map<string, IntradayInstrument>()
  const add = (x: IntradayInstrument): void => {
    const old = bySecid.get(x.secid)
    if (!old) bySecid.set(x.secid, x)
    else if (!old.source.includes(x.source)) bySecid.set(x.secid, { ...old, source: `${old.source}；${x.source}` })
  }
  for (const x of BROAD_INDICES) add(x)

  const text = [reports?.mainline ?? '', reports?.rotation ?? ''].join('\n')
  if (!text.trim()) return [...bySecid.values()]

  const fundRe = /(?:^|[^0-9])(\d{6})(?![0-9])/g
  let m: RegExpExecArray | null
  while ((m = fundRe.exec(text)) !== null) {
    const proxy = FUND_PROXY[m[1]]
    if (proxy) add(proxy)
  }
  for (const [sector, proxy] of Object.entries(SECTOR_PROXY)) {
    if (text.includes(sector)) add(proxy)
  }
  for (const { re, instrument } of KEYWORD_PROXY) {
    if (re.test(text)) add(instrument)
  }
  return [...bySecid.values()]
}

function todayLocalIso(): string {
  const now = new Date()
  const y = now.getFullYear()
  const m = String(now.getMonth() + 1).padStart(2, '0')
  const d = String(now.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

function val(row: QuoteRecord, key: string): string {
  const v = row[key]
  if (typeof v === 'number' && Number.isFinite(v)) return String(v)
  if (typeof v === 'string' && v.trim()) return v.trim()
  return '—'
}

function pct(row: QuoteRecord, key: string): string {
  const v = row[key]
  if (typeof v === 'number' && Number.isFinite(v)) return `${v.toFixed(2)}%`
  if (typeof v === 'string' && v.trim()) return v.trim().endsWith('%') ? v.trim() : `${v.trim()}%`
  return '—'
}

function fmtAmount(v: unknown): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  return `${(v / 100000000).toFixed(1)}亿`
}

export function renderIntradayQuoteBlock(date: string, instruments: IntradayInstrument[], apiResult: unknown): string {
  const result = apiResult as { success?: unknown; items?: unknown; message?: unknown; error?: unknown }
  if (!result || result.success !== true || !Array.isArray(result.items) || result.items.length === 0) return ''

  const meta = new Map(instruments.map(x => [x.secid, x]))
  const rows: string[] = []
  for (const item of result.items as QuoteRecord[]) {
    const secid = typeof item.secid === 'string' ? item.secid : String(item['输入代码'] ?? '')
    const info = meta.get(secid)
    if (!info) continue
    rows.push(`| ${info.group} | ${info.source} | ${item['证券名称'] ?? info.name} (${secid}) | ${val(item, '最新价')} | ${pct(item, '涨跌幅')} | ${pct(item, '振幅')} | ${val(item, '量比')} | ${fmtAmount(item['成交额'])} | ${pct(item, '5日涨跌幅')} | ${pct(item, '20日涨跌幅')} | ${val(item, '行情时间')} |`)
  }
  if (!rows.length) return ''

  return `\n\n【盘中实时行情（系统预取 · 仅宽基与主线相关）】
本块来自 ttjj-api \`market_realtime_quote\`，只覆盖宽基指数和 market_mainline/mainline_rotation 涉及的 A 股主线代理；跨市场资产不纳入。
联接基金本身通常没有实时行情，主线部分使用同跟踪指数或报告指定载体的场内 ETF 作为盘中代理信号；**这不改变你的可买池与报告给出的目标载体**。

| 类别 | 报告来源 | 行情标的 | 最新价 | 涨跌幅 | 振幅 | 量比 | 成交额 | 5日 | 20日 | 行情时间 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
${rows.join('\n')}
【盘中实时行情 结束】`
}

export async function fetchIntradayQuoteBlock(opts: {
  repoRoot: string
  date: string
  reports?: IntradayReportInputs
  timeoutMs?: number
}): Promise<{ block: string; skipped?: string; error?: string; instruments: IntradayInstrument[] }> {
  if (opts.date !== todayLocalIso()) return { block: '', skipped: 'not_today', instruments: [] }
  const instruments = buildIntradayQuoteBasket(opts.reports)
  const codes = instruments.map(x => x.secid)
  if (!codes.length) return { block: '', skipped: 'empty_basket', instruments }

  const python = process.env.TTJJ_PYTHON
    || (existsSync(join(opts.repoRoot, '.venv', 'bin', 'python')) ? join(opts.repoRoot, '.venv', 'bin', 'python') : 'python3')
  const py = [
    'import json, sys',
    'import ttjj_data_pit_mcp as m',
    'date = sys.argv[1]',
    'codes = json.loads(sys.argv[2])',
    'r = m.market_realtime_quote(date, codes, include=["quote"], raw=False, timeout=10)',
    'print(json.dumps(r, ensure_ascii=False))',
  ].join('\n')

  return new Promise(resolveP => {
    const child = spawn(python, ['-c', py, opts.date, JSON.stringify(codes)], {
      cwd: opts.repoRoot,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: process.env,
    })
    let stdout = ''
    let stderr = ''
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL') } catch { /* ignore */ }
      resolveP({ block: '', error: `timeout after ${opts.timeoutMs ?? 15000}ms`, instruments })
    }, opts.timeoutMs ?? 15000)
    child.stdout.on('data', d => { stdout += d.toString() })
    child.stderr.on('data', d => { stderr += d.toString() })
    child.on('error', err => {
      clearTimeout(timer)
      resolveP({ block: '', error: err.message, instruments })
    })
    child.on('close', code => {
      clearTimeout(timer)
      if (code !== 0) {
        resolveP({ block: '', error: stderr.trim() || `python exited ${code ?? -1}`, instruments })
        return
      }
      try {
        const parsed = JSON.parse(stdout) as unknown
        resolveP({ block: renderIntradayQuoteBlock(opts.date, instruments, parsed), instruments })
      } catch (err) {
        resolveP({ block: '', error: err instanceof Error ? err.message : String(err), instruments })
      }
    })
  })
}
