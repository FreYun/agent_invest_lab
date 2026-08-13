import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { loadDecisionEpisodeCatalog, loadDecisionEpisodeDetail } from '../src/backtest-dashboard/canonical-decision-data.ts'
import { buildAug05CanonicalDatabase, buildCanonicalDecisionDatabase } from '../src/decision-episodes/canonical-aug05.ts'

test('canonical 2026-08-05 case matches the acceptance example structure', () => {
  const root = mkdtempSync(join(tmpdir(), 'canonical-decision-'))
  const dbPath = join(root, 'decision-episodes-v2.db')
  buildAug05CanonicalDatabase(dbPath, '2026-08-12T00:00:00.000Z')

  const catalog = loadDecisionEpisodeCatalog(dbPath) as Record<string, any>
  assert.equal(catalog.available, true)
  assert.equal(catalog.canonical, true)
  assert.deepEqual(catalog.summary, {
    episodes: 1, facts: 7, relations: 5, claims: 3,
    evidence: 15, conflicts: 2, warnings: 8,
  })

  const detail = loadDecisionEpisodeDetail(dbPath, 'bot105d_2026-08-05') as Record<string, any>
  assert.equal(detail.identity.world_date, '2026-08-05')
  assert.equal(detail.context.constraints.length, 2)
  assert.equal(detail.facts.length, 7)
  assert.deepEqual(
    detail.facts.filter((fact: any) => fact.predicate === 'DISTANCE_TO_MA120').map((fact: any) => fact.value),
    [-4.15, -2.93],
  )
  assert.deepEqual(detail.relations.map((relation: any) => relation.predicate), [
    'TRACKS_INDEX', 'EXPOSED_TO', 'EXPOSED_TO', 'SELECTS', 'REQUIRES_DIVERSIFICATION_WITH',
  ])
  assert.equal(detail.claims.length, 3)
  assert.equal(detail.claims[0].supportingEvidence.length, 3)
  assert.equal(detail.claims[0].contradictingEvidence.length, 4)
  assert.equal(detail.claims[0].falsifiers.length, 2)
  assert.equal(detail.conflicts[0].winner, 'v5_mainline/mainline_rotation')
  assert.equal(detail.candidateActions.length, 5)
  assert.deepEqual(detail.actualOrders.map((order: any) => order.order_id), ['7163', '7164', '7165'])
  assert.equal(detail.warnings.length, 8)
  assert.ok(detail.sourceRegistry.some((source: any) => source.source_kind === 'acceptance_spec'))

  rmSync(root, { recursive: true, force: true })
})

test('multiday canonical database keeps only corrected final cases', () => {
  const root = mkdtempSync(join(tmpdir(), 'canonical-multiday-'))
  const dbPath = join(root, 'decision-episodes-v2.db')
  buildCanonicalDecisionDatabase(dbPath, '2026-08-12T00:00:00.000Z')

  const catalog = loadDecisionEpisodeCatalog(dbPath) as Record<string, any>
  assert.deepEqual(catalog.summary, {
    episodes: 6, facts: 49, relations: 25, claims: 18,
    evidence: 72, conflicts: 13, warnings: 27,
  })

  const aug07 = loadDecisionEpisodeDetail(dbPath, 'bot105d_2026-08-07') as Record<string, any>
  assert.equal(aug07.claims.length, 3)
  assert.match(aug07.claims[0].statement, /迟滞带内/)
  assert.ok(!aug07.claims[0].statement.includes('跌破-3%'))

  const aug10 = loadDecisionEpisodeDetail(dbPath, 'bot105d_2026-08-10') as Record<string, any>
  assert.deepEqual(aug10.facts.filter((fact: any) => fact.predicate === 'DISTANCE_TO_MA120').map((fact: any) => fact.value), [-0.96, -4.15])
  assert.match(aug10.conflicts[0].unresolved_issue, /口径差异/)
  assert.equal(aug10.actualOrders.length, 0)

  const aug11 = loadDecisionEpisodeDetail(dbPath, 'bot105d_2026-08-11') as Record<string, any>
  assert.deepEqual(aug11.facts.filter((fact: any) => fact.predicate === 'DISTANCE_TO_MA120').map((fact: any) => fact.value), [-0.81, -4.15])
  assert.ok(aug11.claims.some((claim: any) => claim.statement.includes('2/5')))
  assert.equal(aug11.actualOrders.length, 0)

  const aug12 = loadDecisionEpisodeDetail(dbPath, 'bot105d_2026-08-12') as Record<string, any>
  assert.deepEqual(aug12.facts.filter((fact: any) => fact.predicate === 'DISTANCE_TO_MA120').map((fact: any) => fact.value), [-1.6, -4.9, -4.15])
  assert.equal(aug12.facts.filter((fact: any) => fact.predicate === 'DRAWDOWN_20D_HIGH_WATER').length, 1)
  assert.equal(aug12.facts.filter((fact: any) => fact.predicate === 'DRAWDOWN_ALL_HISTORY_HIGH_WATER').length, 1)
  assert.equal(aug12.actualOrders.length, 0)

  rmSync(root, { recursive: true, force: true })
})
