import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildShadowWorkspace } from '../src/shadowWorkspace.ts'

function fakeWorkspace(): string {
  const dir = mkdtempSync(join(tmpdir(), 'srcws-'))
  writeFileSync(join(dir, 'SOUL.md'), '# soul')
  writeFileSync(join(dir, 'IDENTITY.md'), '# id')
  writeFileSync(join(dir, 'avatar.png'), 'BIGBINARY')
  mkdirSync(join(dir, 'skills', 'foo'), { recursive: true })
  writeFileSync(join(dir, 'skills', 'foo', 'SKILL.md'), '# skill')
  mkdirSync(join(dir, 'sessions'), { recursive: true })
  writeFileSync(join(dir, 'sessions', 'old.json'), '{}')
  return dir
}

test('buildShadowWorkspace copies included paths, skips others, creates empty memory/trading/journal.md', () => {
  const src = fakeWorkspace()
  const dest = join(mkdtempSync(join(tmpdir(), 'dstws-')), 'bot7')
  buildShadowWorkspace({ sourceDir: src, destDir: dest, include: ['SOUL.md', 'IDENTITY.md', 'skills'] })

  assert.equal(readFileSync(join(dest, 'SOUL.md'), 'utf8'), '# soul')
  assert.equal(readFileSync(join(dest, 'IDENTITY.md'), 'utf8'), '# id')
  assert.equal(readFileSync(join(dest, 'skills', 'foo', 'SKILL.md'), 'utf8'), '# skill')
  assert.equal(existsSync(join(dest, 'avatar.png')), false)
  assert.equal(existsSync(join(dest, 'sessions')), false)
  assert.equal(existsSync(join(dest, 'memory', 'trading', 'journal.md')), true)
  assert.ok(readFileSync(join(dest, 'memory', 'trading', 'journal.md'), 'utf8').length > 0)

  rmSync(src, { recursive: true, force: true })
  rmSync(dest, { recursive: true, force: true })
})

test('buildShadowWorkspace tolerates missing source entries and does not overwrite existing journal', () => {
  const src = mkdtempSync(join(tmpdir(), 'srcws2-'))
  writeFileSync(join(src, 'SOUL.md'), 's')
  const dest = join(mkdtempSync(join(tmpdir(), 'dstws2-')), 'bot1')
  mkdirSync(join(dest, 'memory', 'trading'), { recursive: true })
  writeFileSync(join(dest, 'memory', 'trading', 'journal.md'), 'PRE-EXISTING')
  buildShadowWorkspace({ sourceDir: src, destDir: dest, include: ['SOUL.md', 'DOES_NOT_EXIST.md', 'skills'] })
  assert.equal(readFileSync(join(dest, 'SOUL.md'), 'utf8'), 's')
  assert.equal(readFileSync(join(dest, 'memory', 'trading', 'journal.md'), 'utf8'), 'PRE-EXISTING')
  rmSync(src, { recursive: true, force: true })
  rmSync(dest, { recursive: true, force: true })
})
