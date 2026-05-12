import { test } from 'node:test'
import assert from 'node:assert/strict'
import { tokenize } from '../src/memory-server/tokenize.ts'

test('tokenize lowercases latin runs and keeps duplicates', () => {
  assert.deepEqual(tokenize('Buy NVDA, sell NVDA today'), ['buy', 'nvda', 'sell', 'nvda', 'today'])
})

test('tokenize splits CJK into single chars', () => {
  assert.deepEqual(tokenize('半导体周期'), ['半', '导', '体', '周', '期'])
})

test('tokenize mixes CJK + latin + numbers', () => {
  assert.deepEqual(tokenize('A股科技板块涨3%'), ['a', '股', '科', '技', '板', '块', '涨', '3'])
})

test('tokenize drops stopwords', () => {
  // 'the' and 'of' and '的' are stopwords
  assert.deepEqual(tokenize('the price of gold 的 走势'), ['price', 'gold', '走', '势'])
})

test('tokenize on empty / whitespace returns []', () => {
  assert.deepEqual(tokenize('   '), [])
  assert.deepEqual(tokenize(''), [])
})
