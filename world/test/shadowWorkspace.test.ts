import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildShadowWorkspace } from '../src/shadowWorkspace.ts'
import { DEFAULT_SHADOW_INCLUDE } from '../src/config.ts'

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

test('buildShadowWorkspace copies included paths, skips others', () => {
  const src = fakeWorkspace()
  const dest = join(mkdtempSync(join(tmpdir(), 'dstws-')), 'bot7')
  buildShadowWorkspace({ sourceDir: src, destDir: dest, include: ['SOUL.md', 'IDENTITY.md', 'skills'] })

  assert.equal(readFileSync(join(dest, 'SOUL.md'), 'utf8'), '# soul')
  assert.equal(readFileSync(join(dest, 'IDENTITY.md'), 'utf8'), '# id')
  assert.equal(readFileSync(join(dest, 'skills', 'foo', 'SKILL.md'), 'utf8'), '# skill')
  assert.equal(existsSync(join(dest, 'avatar.png')), false)
  assert.equal(existsSync(join(dest, 'sessions')), false)

  rmSync(src, { recursive: true, force: true })
  rmSync(dest, { recursive: true, force: true })
})

test('buildShadowWorkspace tolerates missing source entries', () => {
  const src = mkdtempSync(join(tmpdir(), 'srcws2-'))
  writeFileSync(join(src, 'SOUL.md'), 's')
  const dest = join(mkdtempSync(join(tmpdir(), 'dstws2-')), 'bot1')
  buildShadowWorkspace({ sourceDir: src, destDir: dest, include: ['SOUL.md', 'DOES_NOT_EXIST.md', 'skills'] })
  assert.equal(readFileSync(join(dest, 'SOUL.md'), 'utf8'), 's')
  rmSync(src, { recursive: true, force: true })
  rmSync(dest, { recursive: true, force: true })
})

test('buildShadowWorkspace default copies mcporter but not research-loop workspace overrides', () => {
  const src = fakeWorkspace()
  mkdirSync(join(src, 'config'), { recursive: true })
  writeFileSync(join(src, 'config', 'mcporter.json'), '{"mcpServers":{"research-mcp":{"url":"http://example/mcp"}}}')
  writeFileSync(join(src, 'config', 'research-loop.yaml'), 'workspace: /home/rooot/.openclaw/workspace-bot7\n')
  const dest = join(mkdtempSync(join(tmpdir(), 'dstws3-')), 'bot7')

  buildShadowWorkspace({ sourceDir: src, destDir: dest })

  assert.deepEqual(DEFAULT_SHADOW_INCLUDE.includes('config/mcporter.json'), true)
  assert.equal(existsSync(join(dest, 'config', 'mcporter.json')), true)
  assert.equal(existsSync(join(dest, 'config', 'research-loop.yaml')), false)

  rmSync(src, { recursive: true, force: true })
  rmSync(dest, { recursive: true, force: true })
})

test('buildShadowWorkspace filters research-loop workspace overrides even when config directory is included', () => {
  const src = fakeWorkspace()
  mkdirSync(join(src, 'config'), { recursive: true })
  writeFileSync(join(src, 'config', 'mcporter.json'), '{}')
  writeFileSync(join(src, 'config', 'research-loop.json'), '{"workspace":"/home/rooot/.openclaw/workspace-bot7"}')
  writeFileSync(join(src, 'config', 'tools-policy.json'), '{"deny":["bash"]}')
  const dest = join(mkdtempSync(join(tmpdir(), 'dstws4-')), 'bot7')

  buildShadowWorkspace({ sourceDir: src, destDir: dest, include: ['config'] })

  assert.equal(existsSync(join(dest, 'config', 'mcporter.json')), true)
  assert.equal(existsSync(join(dest, 'config', 'tools-policy.json')), true)
  assert.equal(existsSync(join(dest, 'config', 'research-loop.json')), false)

  rmSync(src, { recursive: true, force: true })
  rmSync(dest, { recursive: true, force: true })
})

test('buildShadowWorkspace substitutes ${VAR} placeholders in config/mcporter.json when templateVars provided (and only that file)', () => {
  const src = fakeWorkspace()
  mkdirSync(join(src, 'config'), { recursive: true })
  writeFileSync(join(src, 'config', 'mcporter.json'), '{"mcpServers":{"sim":{"url":"${SIMWORLD_PROXY_URL}","transport":"streamable-http"},"keep":{"url":"http://x/${UNSET_VAR}/y"}}}')
  // Other config files must not be templated.
  writeFileSync(join(src, 'config', 'other.json'), '{"placeholder":"${SIMWORLD_PROXY_URL}"}')
  const dest = join(mkdtempSync(join(tmpdir(), 'dstws-tpl-')), 'bot7')

  buildShadowWorkspace({ sourceDir: src, destDir: dest, include: ['config'], templateVars: { SIMWORLD_PROXY_URL: 'http://127.0.0.1:99999/mcp' } })

  const got = JSON.parse(readFileSync(join(dest, 'config', 'mcporter.json'), 'utf8')) as { mcpServers: Record<string, { url: string }> }
  assert.equal(got.mcpServers.sim.url, 'http://127.0.0.1:99999/mcp')
  // Unset variable is replaced with empty string (so downstream consumer sees a syntactically valid url field, even if broken).
  assert.equal(got.mcpServers.keep.url, 'http://x//y')

  // Other config files are copied verbatim — no template substitution.
  assert.equal(readFileSync(join(dest, 'config', 'other.json'), 'utf8'), '{"placeholder":"${SIMWORLD_PROXY_URL}"}')

  rmSync(src, { recursive: true, force: true })
  rmSync(dest, { recursive: true, force: true })
})

test('buildShadowWorkspace skips direct research-loop config include entries', () => {
  const src = fakeWorkspace()
  mkdirSync(join(src, 'config'), { recursive: true })
  writeFileSync(join(src, 'config', 'research-loop.yaml'), 'workspace: /real/workspace\n')
  const dest = join(mkdtempSync(join(tmpdir(), 'dstws5-')), 'bot7')

  buildShadowWorkspace({ sourceDir: src, destDir: dest, include: ['config/research-loop.yaml'] })

  assert.equal(existsSync(join(dest, 'config', 'research-loop.yaml')), false)

  rmSync(src, { recursive: true, force: true })
  rmSync(dest, { recursive: true, force: true })
})
