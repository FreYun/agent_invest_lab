import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildIntradayQuoteBasket, renderIntradayQuoteBlock } from '../src/intraday-market.ts'

test('intraday basket uses broad indices plus A-share mainline ETF proxies only', () => {
  const basket = buildIntradayQuoteBasket({
    mainline: 'BK1128.DC CPO概念 → 007818；BK1036.DC 半导体 → 007301；BK1137.DC 存储芯片 → 008888',
    rotation: 'BK0877.DC PCB → 017472；候选 BK1326.DC 半导体设备、BK1325.DC 半导体材料。res5 纳指不用管。',
  })
  const secids = basket.map(x => x.secid)
  assert.deepEqual(secids.slice(0, 5), ['1.000001', '1.000300', '0.399006', '1.000688', '1.000852'])
  assert.ok(secids.includes('1.515880'))
  assert.ok(secids.includes('1.512480'))
  assert.ok(secids.includes('0.159995'))
  assert.ok(secids.includes('0.159667'))
  assert.ok(secids.includes('0.159516'))
  assert.equal(secids.some(x => x.includes('IXIC') || x.includes('NDX')), false)
})

test('intraday quote block explains ETF proxies do not alter buyable pool', () => {
  const instruments = buildIntradayQuoteBasket({ mainline: 'BK1128.DC CPO概念 → 007818', rotation: '' })
  const block = renderIntradayQuoteBlock('2026-07-08', instruments, {
    success: true,
    items: [
      { secid: '1.000300', 证券名称: '沪深300', 最新价: 4755.53, 涨跌幅: -0.77, 振幅: 1.64, 量比: 0.8, 成交额: 783248978280.6, '5日涨跌幅': -4.1, '20日涨跌幅': -0.96, 行情时间: '2026-07-08 14:30:01' },
      { secid: '1.515880', 证券名称: '通信ETF国泰', 最新价: 0.757, 涨跌幅: -0.53, 振幅: 4.99, 量比: 1.15, 成交额: 4810678792, '5日涨跌幅': -11.87, '20日涨跌幅': -10.73, 行情时间: '2026-07-08 14:30:02' },
    ],
  })
  assert.match(block, /盘中实时行情/)
  assert.match(block, /跨市场资产不纳入/)
  assert.match(block, /不改变你的可买池/)
  assert.match(block, /通信ETF国泰/)
  assert.match(block, /7832\.5亿/)
})
