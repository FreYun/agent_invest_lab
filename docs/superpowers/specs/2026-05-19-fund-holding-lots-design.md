# Fund holding lots: per-lot redemption fee with FIFO consumption

**Status:** design approved, pending implementation plan
**Date:** 2026-05-19
**Affects:** `fund-portfolio-mcp/server.py`, `fund-portfolio-mcp/db.py`, new migration script, new tests
**Driver:** Existing redemption-fee logic computes a single `holding_days` per `fund_bot_holdings` row, so when a bot buys the same fund multiple times the second purchase inherits the first lot's holding period (or vice versa, depending on whether the holding was previously closed). The recorded fee for many real orders is wrong relative to the regulatory intent (fee should reflect *that specific batch's* time-on-book).

## Goal

When the same `(bot_id, fund_code, run_id)` has multiple separate purchases (batches/lots) and is later redeemed:

1. Each lot's redemption fee uses **its own** `holding_days = sell_date - lot.entry_date` (calendar days), and its own tier rate via `_redeem_fee_rate`.
2. A SELL consumes lots **FIFO by `entry_date` (oldest first)** — meaning longer-held shares are redeemed first, which favors the investor (lower or zero fee tier).
3. Audit trail makes the per-lot breakdown queryable.

Out of scope: subscription fee (no change), management/custody/sales-service fees (still NAV-embedded, no change), 申购费 holding-tier (申购费目前无阶梯).

## Background and current behavior

- `fund_bot_holdings`: one row per `(bot_id, fund_code, run_id, lifecycle)`. Has a single `entry_date` set on the first BUY into that row; subsequent BUYs of the same fund update `shares` / `amount_invested` but **do not** touch `entry_date`. When the row is fully sold it becomes `status='closed'`; the next BUY opens a new row with the new BUY's date as `entry_date`.
- `fund_bot_actions`: append-only ADD/REDUCE ledger keyed by `(bot_id, fund_code, action_date, run_id)`. The `nav_used`, `shares`, `amount`, `fee` columns record per-event values.
- `fund_bot_orders`: per-trade ticket. New SELL path stores `confirmed_shares`/`confirmed_amount`/`fee` at T-day (locks fee at placement); old SELL path leaves them NULL until T+1 settle.
- SELL fee today: `holding_days = T_sell - holding.entry_date` (single value), `fee = shares × nav × _redeem_fee_rate(tiers, holding_days)`.

This is wrong whenever a holding row has two or more BUY events behind it.

## Design

### §1 Data model

New table `fund_bot_holding_lots`:

```
fund_bot_holding_lots
  lot_id           INTEGER PRIMARY KEY AUTOINCREMENT
  bot_id           TEXT    NOT NULL
  fund_code        TEXT    NOT NULL
  run_id           TEXT    NOT NULL
  holding_id       INTEGER             -- FK to fund_bot_holdings.holding_id (the lot's parent)
  entry_date       TEXT    NOT NULL    -- BUY trade_date (T-day), source of holding_days
  entry_nav        REAL    NOT NULL
  shares_initial   REAL    NOT NULL
  shares_remaining REAL    NOT NULL
  cost_initial     REAL    NOT NULL    -- order_amount (含申购费), matches holdings.amount_invested semantics
  cost_remaining   REAL    NOT NULL
  source_order_id  INTEGER             -- the BUY order_id that created this lot
  status           TEXT    DEFAULT 'open'  -- 'open' | 'closed'
  created_at       TEXT    DEFAULT (datetime('now'))

INDEX idx_lots_fifo  ON fund_bot_holding_lots(bot_id, fund_code, run_id, status, entry_date, lot_id)
INDEX idx_lots_holding ON fund_bot_holding_lots(holding_id)
```

Conventions:
- `entry_date` is the BUY's `order_date` (T-day, same string format as `holdings.entry_date`).
- `cost_initial = order_amount` (含申购费). This preserves the invariant `SUM(open lot.cost_remaining) == holdings.amount_invested`, simplifying tests and migration verification.
- `status` flips to `'closed'` when `shares_remaining` drops within 1e-6 of zero.
- A lot belongs to exactly one `run_id` (same isolation rule as holdings).

### §2 Helper functions

Added to `server.py` alongside `_fund_fee_rates` / `_redeem_fee_rate`:

```python
def _insert_lot(conn, *, bot_id, fund_code, run_id, holding_id,
                entry_date, entry_nav, shares, cost, source_order_id) -> int:
    """Insert a new lot row. Returns lot_id. Called from BUY settle path."""

def _consume_lots_fifo(conn, *, bot_id, fund_code, run_id, sell_shares,
                       as_of_date, nav, redeem_tiers) -> list[dict]:
    """Walk open lots ordered (entry_date ASC, lot_id ASC) and consume sell_shares.

    For each touched lot:
      take = min(lot.shares_remaining, sell_shares_left)
      holding_days = (as_of_date - lot.entry_date) calendar days
      rate = _redeem_fee_rate(redeem_tiers, holding_days)
      gross = take * nav
      fee = gross * rate
      cost_consumed = lot.cost_remaining * (take / lot.shares_remaining)
      UPDATE lot SET shares_remaining -= take, cost_remaining -= cost_consumed,
                     status = 'closed' if remaining <= 1e-6 else 'open'

    Returns:
      [{lot_id, entry_date, holding_days, rate,
        shares: take, cost_consumed, gross, fee}, ...]

    Raises ValueError('insufficient open lots: want=X have=Y')
      if SUM(open lots shares) < sell_shares.
    """
```

`nav` is passed in (caller knows the T-day NAV); helper does not re-fetch.

### §3 BUY flow

`portfolio_place_buy_order` (T-day placement): unchanged — no lot created yet because shares only land at T+1 settle.

`_settle_t1` BUY branch ([server.py:2648-2702](fund-portfolio-mcp/server.py#L2648-L2702)): after creating/updating the holding row and before `cash_in_transit -= order_amount`, insert one lot:

```python
_insert_lot(conn,
    bot_id=bot_id, fund_code=fc, run_id=holding_run_id,
    holding_id=<resolved holding_id (existing or freshly inserted)>,
    entry_date=order_date,
    entry_nav=nav,                # = order.reference_nav
    shares=add_shares,
    cost=order_amount,            # 含申购费 to match holdings.amount_invested
    source_order_id=oid)
```

`fund_bot_actions` ADD row: unchanged (still one per BUY).

### §4 SELL flow

`portfolio_place_sell_order` (T-day) ([server.py:1431-1558](fund-portfolio-mcp/server.py#L1431-L1558)):

Replace the single-rate block at 1481-1487 with lot consumption:

```python
_, redeem_tiers = _fund_fee_rates(conn, fund_code)
try:
    consumptions = _consume_lots_fifo(
        conn, bot_id=bot_id, fund_code=fund_code, run_id=run_id,
        sell_shares=shares, as_of_date=trade_date, nav=nav,
        redeem_tiers=redeem_tiers)
except ValueError as e:
    return json.dumps({"success": False, "message": str(e)}, ensure_ascii=False)

gross         = sum(c["gross"]         for c in consumptions)
fee           = sum(c["fee"]           for c in consumptions)
proceeds      = gross - fee
cost_consumed = sum(c["cost_consumed"] for c in consumptions)
```

Holding update (replaces 1490-1508):
- `new_shares = total_shares - shares`
- `new_cost   = holding.amount_invested - cost_consumed` ← **changes** from the current `ratio_left * old_cost` behavior. Per-lot cost basis is exact; the current proportional formula is wrong whenever lots have different per-share cost.
- `entry_date` field: set to `SELECT MIN(entry_date) FROM fund_bot_holding_lots WHERE holding_id=? AND status='open'`. If holding fully closes (`new_shares <= 1e-6`), the existing close branch runs and `exit_date` is set.

Actions (one row per touched lot, replaces the single REDUCE insert at 1519-1527):

```python
for c in consumptions:
    reason = (f"T 日赎回 lot#{c['lot_id']} "
              f"(entry={c['entry_date']} 持有 {c['holding_days']} 天 "
              f"赎回费 {c['rate']*100:.2f}%)")
    INSERT INTO fund_bot_actions (...action_type='REDUCE',
        amount=c['gross'], shares=c['shares'], fee=c['fee'],
        nav_used=nav, action_date=trade_date, reason=reason, run_id=run_id)
```

Orders row: unchanged structure — one row per SELL — but `fee` is the total of consumptions, `confirmed_shares=shares`, `confirmed_amount=proceeds`.

`_settle_t1` SELL new path (line 2708-2723): unchanged — lots were already consumed at T-day; settle just transfers `cash_receivable → cash`.

`_settle_t1` SELL legacy path (line 2725-2777): also rewritten to call `_consume_lots_fifo`, with `as_of_date=order_date` (preserves the current "fee fixed at apply-day" semantics). Result: a single code path for fee math, eliminating the duplicate formula.

### §5 Migration

`scripts/migrate_holding_lots.py` (idempotent, manual run after deploy):

```
1. Ensure table + indexes exist (CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS).
2. DELETE FROM fund_bot_holding_lots  -- clean slate; rebuild from authoritative ledger.
3. SELECT DISTINCT (bot_id, fund_code, run_id) FROM fund_bot_actions.
4. For each triple, fetch actions ORDER BY action_date ASC, action_id ASC:
     lots_queue = []          # in-memory FIFO of {entry_date, shares, cost, nav, src_order_id}
     for a in actions:
         if a.action_type == 'ADD':
             lots_queue.append({entry_date: a.action_date,
                                shares: a.shares, cost: a.amount,
                                nav: a.nav_used,
                                src_order_id: resolve_buy_order(a)})
         elif a.action_type == 'REDUCE':
             remaining = a.shares
             while remaining > 1e-6 and lots_queue:
                 head = lots_queue[0]
                 original_shares = head.shares
                 take = min(original_shares, remaining)
                 cost_consumed = head.cost * (take / original_shares)
                 head.shares -= take
                 head.cost   -= cost_consumed
                 remaining   -= take
                 if head.shares <= 1e-6:
                     lots_queue.pop(0)
     # Resolve holding_id by SELECTing the active holding for that (bot,fund,run).
     INSERT remaining lots_queue entries as status='open' rows.
     # Closed/consumed lots: also insert with status='closed' so audit history is preserved.
5. Post-checks:
     a. SUM(open lot.shares_remaining)  == holdings.shares           (tol 1e-3)
     b. SUM(open lot.cost_remaining)    == holdings.amount_invested  (tol 0.01)
     c. Holdings with active status but no matching open lots → warning row.
6. Print summary: groups scanned, lots inserted, warnings.
```

`resolve_buy_order(a)`: match `fund_bot_orders` by `(bot_id=a.bot_id, fund_code=a.fund_code, order_run_id=a.run_id, order_date=a.action_date, order_type='buy')`; if multiple matches, use min order_id; if none, leave `source_order_id=NULL`.

`holding_id` on lots is **best-effort, nullable**. For the migration: resolve by `SELECT holding_id FROM fund_bot_holdings WHERE bot_id=? AND fund_code=? AND run_id=? AND entry_date <= a.action_date AND (exit_date IS NULL OR exit_date >= a.action_date)` — i.e., the holdings row whose lifecycle interval contains the ADD's date. If no match, leave NULL. Lots are looked up primarily by `(bot_id, fund_code, run_id, status)`, so a NULL `holding_id` does not affect correctness of fee calculation.

Risks called out in the warning list:
- Pre-run_id actions (NULL `run_id`) — these legacy rows are bundled into a synthetic `run_id='legacy'` group during replay, since they all predate the run-id era.
- ADD without matching BUY order (rare; can happen for manual admin inserts).
- REDUCE that drains more than the queue has → warn and clamp (means the action ledger itself is inconsistent; surface for manual review).

### §6 Invariants

After every BUY-settle and every SELL (place or legacy-settle), the following must hold per `(bot_id, fund_code, run_id)`:

- I1: `SUM(lot.shares_remaining WHERE status='open') == holdings.shares` (tol 1e-3)
- I2: `SUM(lot.cost_remaining   WHERE status='open') == holdings.amount_invested` (tol 0.01)
- I3: `MIN(lot.entry_date       WHERE status='open') == holdings.entry_date`

A new helper `_assert_lot_holding_consistency(conn, bot_id, fund_code, run_id)` performs these checks and raises with a diagnostic message. Called at the end of BUY-settle and SELL-place when an env var `FUND_LOT_DEBUG=1` is set, to keep production fast but make CI/dev catch drift immediately.

### §7 Tests

New file `fund-portfolio-mcp/test_holding_lots.py`:

| Test | Verifies |
|---|---|
| `test_buy_creates_lot` | One BUY settle → one lot row with correct fields, `status='open'`. |
| `test_two_buys_two_lots_same_fund` | Two BUYs on different days → two open lots; holdings row unchanged in count. |
| `test_sell_single_lot_partial` | Single lot, partial sell → lot shares/cost halved, holding cost = lot cost. |
| `test_sell_fifo_crosses_lots` | Two lots (old=35d held, new=5d), sell amount > old lot's shares → old lot at 0% drained, new lot at 1.5% partially consumed, fee = sum of both. |
| `test_sell_fifo_exact_old_lot_boundary` | Sell exactly old lot's shares → old lot closed, new lot untouched. |
| `test_sell_drains_all_lots` | Sell all → all lots closed, holding closed with `exit_date`. |
| `test_consume_more_than_available_rejected` | sell_shares > SUM(open) → ValueError, no DB writes. |
| `test_invariants_after_sell` | I1/I2/I3 hold after a multi-lot partial sell. |
| `test_legacy_settle_path_uses_lots` | Old order (confirmed_amount IS NULL) settles via FIFO too, single code path. |
| `test_migration_replay_matches_live_path` | Fixture: BUY → BUY → REDUCE → BUY → REDUCE. Run live code, snapshot lots; then DELETE lots and run migration; assert lot table is identical (mod created_at). |
| `test_holding_days_boundary_7_30` | Lot at exactly 7 days → 0.5% tier (not 1.5%); exactly 30 days → 0% (not 0.5%). |
| `test_cost_invariant_after_sell` | After partial sell across lots, SUM(open lot.cost_remaining) equals holdings.amount_invested exactly. |

### §8 Breaking changes and rollout

Code that reads `fund_bot_actions` for SELL events will see **multiple rows per SELL order** instead of one. Affected callers:
- `_replay` / snapshot reconstruction: must be checked — current logic likely sums by date already, but verify.
- `portfolio_get_my_history`: history view aggregates by order, not action, so unaffected at the API level. Inner debug detail may show extra rows; expected.
- Any external tooling that joins orders 1:1 with actions: must switch to a 1:N model.

Rollout order:
1. Add table + helpers + migration script (no behavior change yet).
2. Run migration on lab DB; verify invariants.
3. Switch BUY-settle to insert lot.
4. Switch SELL-place to consume lots; legacy-settle path same.
5. Run verification (`fund-portfolio-mcp/verify_fund_fees.py`, moved into repo and updated for per-lot semantics) on lab DB — expect 0 mismatches.
6. Delete old single-rate code paths.

## Verification

After implementation, re-run the verification harness against `/home/rooot/agent_invest_lab/data/fund.db`:

- For every confirmed SELL: re-derive the FIFO consumption from actions, compute expected fee, compare to recorded. Target: 0 mismatches (vs. 12 found in pre-change run, all caused by entry_date-after-rebuy edge cases).
- For every active holding: assert I1/I2/I3.
- Boundary unit tests pass.

## Open questions

None — design accepted as of 2026-05-19.
