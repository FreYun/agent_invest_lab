import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseCliArgs, main } from '../src/cli.ts'

test('parseCliArgs: run with config + run-id + world-dir', () => {
  assert.deepEqual(parseCliArgs(['run', '--config', 'config/world.yaml', '--run-id', 'r1', '--world-dir', '/tmp/w']),
    { command: 'run', config: 'config/world.yaml', runId: 'r1', worldDir: '/tmp/w' })
})

test('parseCliArgs: run without run-id (derived later)', () => {
  assert.deepEqual(parseCliArgs(['run', '--config', 'c.yaml']), { command: 'run', config: 'c.yaml', runId: undefined, worldDir: undefined })
})

test('parseCliArgs: resume / status / stop', () => {
  assert.deepEqual(parseCliArgs(['resume', '--config', 'c.yaml']), { command: 'resume', config: 'c.yaml', runId: undefined, worldDir: undefined })
  assert.deepEqual(parseCliArgs(['status', '--world-dir', '/w']), { command: 'status', config: undefined, runId: undefined, worldDir: '/w' })
  assert.deepEqual(parseCliArgs(['stop']), { command: 'stop', config: undefined, runId: undefined, worldDir: undefined })
})

test('parseCliArgs: help and unknown', () => {
  assert.deepEqual(parseCliArgs(['--help']), { command: 'help', config: undefined, runId: undefined, worldDir: undefined })
  assert.deepEqual(parseCliArgs([]), { command: 'help', config: undefined, runId: undefined, worldDir: undefined })
  assert.deepEqual(parseCliArgs(['frobnicate']), { command: 'unknown', config: undefined, runId: undefined, worldDir: undefined })
})

test('parseCliArgs: run requires --config (validated by caller, but parser still returns it absent)', () => {
  assert.deepEqual(parseCliArgs(['run']), { command: 'run', config: undefined, runId: undefined, worldDir: undefined })
})

test('main: resume without --run-id exits 2 with clear error', async () => {
  const errs: string[] = []
  const orig = process.stderr.write.bind(process.stderr)
  process.stderr.write = (chunk: any) => { errs.push(String(chunk)); return true }
  try {
    const code = await main(['resume', '--config', 'whatever.yaml'])
    assert.equal(code, 2)
    assert.match(errs.join(''), /--run-id <id> is required/)
  } finally {
    process.stderr.write = orig
  }
})

test('main: status without --run-id exits 2', async () => {
  const errs: string[] = []
  const orig = process.stderr.write.bind(process.stderr)
  process.stderr.write = (chunk: any) => { errs.push(String(chunk)); return true }
  try {
    const code = await main(['status'])
    assert.equal(code, 2)
    assert.match(errs.join(''), /--run-id <id> is required/)
  } finally {
    process.stderr.write = orig
  }
})

test('main: stop without --run-id exits 2', async () => {
  const errs: string[] = []
  const orig = process.stderr.write.bind(process.stderr)
  process.stderr.write = (chunk: any) => { errs.push(String(chunk)); return true }
  try {
    const code = await main(['stop'])
    assert.equal(code, 2)
    assert.match(errs.join(''), /--run-id <id> is required/)
  } finally {
    process.stderr.write = orig
  }
})

test('parseCliArgs: pause', () => {
  assert.deepEqual(parseCliArgs(['pause', '--run-id', 'r1']),
    { command: 'pause', config: undefined, runId: 'r1', worldDir: undefined })
})

test('main: pause without --run-id exits 2', async () => {
  const errs: string[] = []
  const orig = process.stderr.write.bind(process.stderr)
  process.stderr.write = (chunk: any) => { errs.push(String(chunk)); return true }
  try {
    const code = await main(['pause'])
    assert.equal(code, 2)
    assert.match(errs.join(''), /--run-id <id> is required/)
  } finally {
    process.stderr.write = orig
  }
})
