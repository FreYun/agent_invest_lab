# Market Pipeline

This directory contains the project-local market database update pipeline for
`/home/rooot/agent_invest_lab/data/market.db`.

The legacy path `/home/rooot/database/market.db` is kept as a compatibility
symlink to the project database, including `-wal` and `-shm` sidecar files.

## Layout

- `openclaw/scripts/daily-regime-pipeline.sh` updates EOD market data, regime,
  S5 cache, and market facts.
- `openclaw/scripts/stock-select-daily.sh` updates stock-select, board, money
  flow, earnings, and valuation derived tables.
- `openclaw/scripts/s*/` contains S1/S2/S3/S5/S6/S7/S8/S9 strategy selectors.
- `openclaw/scout/` contains intraday, board, margin, earnings, valuation, and
  scout UI data writers.
- `openclaw/workspace-bot11/scripts/` contains `daily_review.py`, config, and
  `stock_select_backfill.py` dependencies used by the pipeline.

## Cron

`cron/market-db.crontab` is the project-local cron block installed in the user
crontab between these markers:

```
# === AGENT_INVEST_LAB_MARKET_PIPELINE BEGIN ===
# === AGENT_INVEST_LAB_MARKET_PIPELINE END ===
```

The legacy `.openclaw` market jobs are intentionally left active, so the old
line and this project-local line run in parallel. New job names use the
`lab-market-*` prefix and write logs under `/home/rooot/agent_invest_lab/logs/`.

## Runtime

The project line defaults to `/home/rooot/agent_invest_lab/.venv/bin/python`.
Install/update market dependencies with:

```bash
.venv/bin/pip install -r requirements-market.txt
```

The scripts support `MARKET_DB_PATH` and `SCOUT_DB_PATH` overrides for testing;
use those overrides to point at a temporary copy instead of the live database.
