# Experience Library pilot

This pilot imports backtest reflections into an isolated SQLite database. It does not modify
`data/fund.db` and does not expose cards to trading agents.

## Safety defaults

- `sent.md` rolling-memory text is hashed for provenance but is never counted as evidence.
- Every extracted market card starts as `case_only` / `research_only`.
- `max_position_impact_pct` and `max_fleet_adoption_pct` start at zero.
- Evidence starts as `inconclusive`; agent narration never counts as support.
- Current `fund.db` values are marked as an unverified data vintage, not PIT truth.
- Persona, methodology, claimant, extractor and verifier identities are stored separately.
- Opposite actions sharing a coarse topic are only marked as *potential* conflicts.

## Run a shadow import

```bash
node --experimental-strip-types src/experience-library/batch.ts \
  --run-id RUN_ID \
  --bot-id BOT_ID \
  --start YYYY-MM-DD \
  --end YYYY-MM-DD
```

Default outputs are ignored runtime artifacts:

- `runtime/experience-library/experience-library.db`
- `runtime/experience-library/batch-RUN_ID-BOT_ID.md`

The importer currently treats explicit gate, falsification and deep-research conclusion sections
as candidate material. It intentionally does not perform semantic deduplication or promote cards;
those require a separate review/validation stage.

