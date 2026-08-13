# Canonical decision case v0.1

`decision-episodes-v2.db` is intentionally separate from the generic v1 rule-extraction database.
It contains 389 finalized cases for the complete `bot105d-daily-agenticdeep-charter-0424` run, covering `2025-01-02` through `2026-08-12`. The run has 390 date directories; `2026-06-26` is excluded because its `reply.json` is missing.

The six dates from `2026-08-05` through `2026-08-12` keep their manually reviewed Canonical definitions. The other 383 cases use the same pipeline: deterministic rules and database facts, Qwen semantic organization, quote/schema/Chinese-language validation, then Canonical promotion.

The schema follows the user-provided reconstructed example and stores these layers separately:

- decision-time identity, source registry, account context, and hard constraints;
- selected atomic facts without merging conflicting producers;
- typed relations with mechanism, validity, evidence, and certainty;
- three to four readable decision-level claims per case with supporting and contradicting evidence;
- conflict sides, winner, resolution rule, and unresolved issue;
- rejected and accepted candidate actions;
- one final decision per case; database-confirmed orders are preserved, while HOLD days explicitly keep zero new orders;
- extraction warnings.

Build the complete run from the `world` directory:

```bash
node --experimental-strip-types src/decision-episodes/import.ts \
  --run-id bot105d-daily-agenticdeep-charter-0424 --bot-id bot105d \
  --start 2025-01-02 --end 2026-08-12 \
  --output runtime/decision-episodes/decision-episodes-rules-full.db

python3 src/decision-episodes/batch-extract-canonical-qwen.py \
  --start 2025-01-02 --end 2026-08-12

node --experimental-strip-types src/decision-episodes/build-complete-run.ts \
  --output runtime/decision-episodes/decision-episodes-v2.db
```

The builder refuses to overwrite an existing file. The final cases use deterministic facts and account records as the base, Qwen for semantic clustering, and explicit validation for numerical conflicts. Qwen candidates are retained only as source artifacts and are not displayed as final records.

The original v1 database remains available at
`runtime/decision-episodes/decision-episodes-v1.db`.
