# market.db Localisation

`agent_invest_lab/data/market.db` is now the project-local copy of the market
database. It was created from the legacy production path with SQLite backup:

```bash
sqlite3 /home/rooot/database/market.db ".backup /home/rooot/agent_invest_lab/data/market.db"
```

The project ignores `data/`, so the 7 GB database is runtime state, not source.

## Current Project Defaults

These project consumers now default to the local database, while still allowing
`SCOUT_DB=/path/to/market.db` overrides:

- `scripts/run-mainline-daily.sh`
- `scripts/v5_mainline_plan.py`
- `scripts/intraday_board_snapshot.py`
- `world/src/market-reports/backfill-deterministic.ts`

## Full Cutover

The upstream update jobs still live mostly in `/home/rooot/.openclaw` and many
of them hard-code `/home/rooot/database/market.db`. The low-risk cutover is to
make the legacy path a compatibility symlink to the project-local database:

```bash
./scripts/cutover-market-db-into-project.sh
```

That script:

- validates `data/market.db` with `PRAGMA quick_check`;
- checkpoints and validates the legacy database;
- renames the old legacy database and sidecars as timestamped backups;
- creates compatibility symlinks for `market.db`, `market.db-wal`, and
  `market.db-shm`;
- verifies the legacy path can still read the `daily` table.

After this, existing cron entries can keep running unchanged, but their writes
land in the project-local database file.

## Longer-Term Cleanup

The cleaner final state is to move the `.openclaw` market update scripts into
this repository and replace hard-coded database paths with `SCOUT_DB` or
`MARKET_DB_PATH`. The compatibility symlink is meant to make that migration
safe and reversible rather than forcing all cron entries to change at once.

## Current Cutover State

Cutover was executed on 2026-07-13 16:14 +08. The legacy path now resolves to `agent_invest_lab/data/market.db`; the previous legacy files were retained as `/home/rooot/database/market.db.before-agent-invest-lab-20260713-161138` and matching `-wal`/`-shm` sidecar backups.
