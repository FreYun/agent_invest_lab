const HELP = `world — agent_invest_lab 日度滚动金融世界系统

用法:
  world run    --config <path> [--run-id <id>] [--world-dir <path>]
  world resume --config <path> [--world-dir <path>]
  world status [--world-dir <path>]
  world stop   [--world-dir <path>]
  world --help
`

const argv = process.argv.slice(2)
if (argv.length === 0 || argv.includes('-h') || argv.includes('--help')) {
  process.stdout.write(HELP)
  process.exit(0)
}
process.stderr.write(`未实现的命令: ${argv.join(' ')}\n`)
process.exit(2)
