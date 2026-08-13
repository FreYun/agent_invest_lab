import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildCanonicalDecisionDatabase } from './canonical-aug05.ts'

function option(name: string): string | undefined {
  const index = process.argv.indexOf(name)
  return index >= 0 ? process.argv[index + 1] : undefined
}

const defaultOutput = resolve(fileURLToPath(new URL('../../runtime/decision-episodes/decision-episodes-v2.db', import.meta.url)))
const output = buildCanonicalDecisionDatabase(option('--output') ?? defaultOutput)
process.stdout.write(`canonical decision database created: ${output}\n`)
