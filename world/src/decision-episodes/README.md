# Decision episode database

This module normalizes World run output into a separate, append-only SQLite database. It does not
write to `data/fund.db` or to the existing Experience Library.

The storage boundary is deliberate:

- `facts` records sourced observations without turning them into explanations;
- `relations` requires a predicate, direction, mechanism, validity, source and certainty class;
- `claims` stores predictions and interpretations as rebuttable assertions;
- `claim_evidence` distinguishes support, refutation and unresolved narrative evidence;
- `candidate_actions` preserves failed calls and retries;
- `final_actions` records database-backed orders and settlement actions;
- `decision_conflicts` keeps incompatible values or recommendations side by side;
- `quality_issues` prevents incomplete provenance from being silently promoted.

Agent narration is never labeled as a hard fact. A tool transport error can be reconciled to an
order in `fund.db`, but both the original error and the database-confirmed action remain visible.

Run a batch from the `world` directory:

```bash
node --experimental-strip-types src/decision-episodes/import.ts \
  --run-id bot105d-daily-agenticdeep-charter-0424 \
  --bot-id bot105d \
  --start 2026-08-03 \
  --end 2026-08-11
```

Default output:

- `runtime/decision-episodes/decision-episodes-v1.db`
- `runtime/decision-episodes/batch-RUN-BOT.md`

Re-importing an unchanged source bundle is idempotent. If either the session artifacts or the
relevant `fund.db` records change, the importer creates a new immutable episode revision.
