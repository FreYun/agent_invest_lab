import { test } from 'node:test'
import assert from 'node:assert/strict'
import { stripInjectSkipBlocks } from '../src/strategy-library.ts'

test('stripInjectSkipBlocks removes a single well-formed skip block', () => {
  const input = [
    '# Head',
    '',
    'keep before',
    '',
    '<!-- INJECT_SKIP_START -->',
    '## dropped section',
    'body of dropped',
    '<!-- INJECT_SKIP_END -->',
    '',
    'keep after',
  ].join('\n')
  const out = stripInjectSkipBlocks(input)
  assert.ok(!out.includes('INJECT_SKIP'), 'marker leaked')
  assert.ok(!out.includes('dropped section'), 'body leaked')
  assert.ok(out.includes('keep before'))
  assert.ok(out.includes('keep after'))
})

test('stripInjectSkipBlocks handles multiple non-nested blocks', () => {
  const input = [
    'a',
    '<!-- INJECT_SKIP_START -->',
    'x1',
    '<!-- INJECT_SKIP_END -->',
    'b',
    '<!-- INJECT_SKIP_START -->',
    'x2',
    '<!-- INJECT_SKIP_END -->',
    'c',
  ].join('\n')
  const out = stripInjectSkipBlocks(input)
  assert.ok(!out.includes('x1'))
  assert.ok(!out.includes('x2'))
  assert.ok(out.includes('a'))
  assert.ok(out.includes('b'))
  assert.ok(out.includes('c'))
})

test('stripInjectSkipBlocks leaves text unchanged when no markers present', () => {
  const input = '# Title\n\nsome body\n\n## Section\nmore body\n'
  assert.equal(stripInjectSkipBlocks(input), input)
})

test('stripInjectSkipBlocks tolerates unclosed START marker (保守，不删)', () => {
  const input = 'keep\n<!-- INJECT_SKIP_START -->\ntail body without end\n'
  const out = stripInjectSkipBlocks(input)
  assert.ok(out.includes('tail body without end'), 'unclosed block should not eat everything')
  assert.ok(out.includes('INJECT_SKIP_START'), '未闭合的 START 保留原文')
})

test('stripInjectSkipBlocks collapses ≥3 consecutive newlines', () => {
  const input = 'a\n<!-- INJECT_SKIP_START -->\nx\n<!-- INJECT_SKIP_END -->\nb'
  const out = stripInjectSkipBlocks(input)
  assert.ok(!/\n{3,}/.test(out), 'triple newlines should be collapsed to double')
})
