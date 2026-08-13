import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderDailyMessage } from '../src/message.ts'

// 主仓位阶梯核对块（message.ts 的 mainPositionLadderBlock）此前没有任何测试覆盖。
// 它只在 multi-fund bot + 『抱主线』regime + 状态机有在管板块 + 存在 >40% 非载体主仓位时渲染。
// 集中度决定档位：≥6 → 主线强化（上限 40%），<6 → 主线初现（上限 60%）。
const mainline = (concentration: number) => `## ① regime
regime：**抱主线**，top15 集中度 ${concentration} 个

## ③ 组合状态机
| 板块 | 角色 | rank | 站MA60 | 已持 | 最小持有余 | 连续in_top5 | 连续out_top5 |
| BK0447 半导体 | 卫星 | 3 | 是 | 是 | 0 | 6 | 0 |

## ⑤ 可投基金池
| BK0447 半导体 | 卫星 | 011609 华夏国证半导体芯片C | 1.2 |
`

function render(weight: number, concentration: number): string {
  return renderDailyMessage({
    botId: 'bot105g',
    date: '2025-06-11',
    marketReports: { context: '', mainline: mainline(concentration), rotation: '' },
    dailyContext: {
      account: {
        account: { initial_capital: 1e6, cash_available: 5e5, cash_in_transit: 0, market_value: 5e5, total_value: 1e6 },
        holdings: [{
          fund_code: '000051', fund_name: '华夏沪深300ETF联接A', weight,
          shares: 1e5, market_value: 5e5, amount_invested: 5e5,
          unrealized_pnl_pct: 0, holding_days: 160, entry_date: '2025-01-02',
        }],
        pendingOrders: [],
      },
    },
  } as unknown as Parameters<typeof renderDailyMessage>[0])
}

test('over-cap main position renders the 0.5% step-down verdict, not the zero-fee deferral', () => {
  // 复刻 bot105g 2025-06-11 的现场：主线强化档上限 40%，000051 占 50%。
  const msg = render(0.5, 6)
  assert.match(msg, /应退坡/)
  assert.match(msg, /赎回费 ≤0\.5% 的份额/)
  assert.match(msg, /只受 ≤0\.5% 赎回费窗口与最小持有期约束/)
  // 旧口径把退坡锁死在零费日（000051 的零费档要等 365 天），必须彻底消失。
  assert.doesNotMatch(msg, /顺延至最近免费日/)
  assert.doesNotMatch(msg, /只动免赎回费份额/)
  assert.doesNotMatch(msg, /零费窗口/)
})

test('in-tier main position still renders no step-down', () => {
  // 主线初现档上限 60%；45% 在档内。仍需 >40% 才认定为主仓位。
  const msg = render(0.45, 4)
  assert.match(msg, /无需退坡/)
  assert.doesNotMatch(msg, /应退坡/)
})
