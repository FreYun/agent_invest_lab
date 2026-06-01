import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mapWithConcurrency } from '../src/concurrency.ts'

test('mapWithConcurrency preserves order and respects limit', async () => {
  let active = 0
  let maxActive = 0
  const fn = async (n: number) => {
    active++; maxActive = Math.max(maxActive, active)
    await new Promise(r => setTimeout(r, 10))
    active--
    return n * 2
  }
  const out = await mapWithConcurrency([1, 2, 3, 4, 5], 2, fn)
  assert.deepEqual(out, [2, 4, 6, 8, 10])
  assert.ok(maxActive <= 2, `maxActive=${maxActive}`)
})

test('mapWithConcurrency handles empty input', async () => {
  assert.deepEqual(await mapWithConcurrency<number, number>([], 4, async x => x), [])
})

test('mapWithConcurrency with limit >= length runs all', async () => {
  const out = await mapWithConcurrency([1, 2, 3], 10, async x => x + 1)
  assert.deepEqual(out, [2, 3, 4])
})
