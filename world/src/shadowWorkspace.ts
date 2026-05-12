import { cpSync, existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { DEFAULT_SHADOW_INCLUDE } from './config.ts'

const JOURNAL_TEMPLATE = `# 交易日志（trading journal）

这是你在金融世界沙盘里的交易日志。规则：
- 每个世界日：做决策**之前**先读这份文件，回顾你过去的判断和操作。
- 做完决策**之后**：把今天的世界日期、你的判断、具体操作、理由，追加到本文件末尾。

---
`

export interface BuildShadowWorkspaceOptions {
  sourceDir: string
  destDir: string
  include?: string[]
}

export function buildShadowWorkspace(opts: BuildShadowWorkspaceOptions): void {
  const include = opts.include ?? DEFAULT_SHADOW_INCLUDE
  mkdirSync(opts.destDir, { recursive: true })
  for (const rel of include) {
    const src = join(opts.sourceDir, rel)
    if (!existsSync(src)) continue
    const dst = join(opts.destDir, rel)
    mkdirSync(dirname(dst), { recursive: true })
    cpSync(src, dst, { recursive: true })
  }
  const journal = join(opts.destDir, 'memory', 'trading', 'journal.md')
  if (!existsSync(journal)) {
    mkdirSync(dirname(journal), { recursive: true })
    writeFileSync(journal, JOURNAL_TEMPLATE)
  }
}
