# Research-status classification fix card v2

Direct commission continuation on `codex/research-status-s4-verifier-fix-v1`.
Reviewed base `9c18f7e453905de62d55b90716fb8d4d076569e2`; mainline
`eb6a832`; append-only; Codex tree read-only; automation paused.

## A. Classification priority: current contract first

The current-format contract keys on `schema` (the production parser field),
not `evidence_schema` as earlier tests/docs wrongly assumed. Classification
order becomes: if a `schema` key is present, the artifact is validated
against the current contract first — a corrupt current-format artifact is
`malformed_evidence` even when it also carries an `experiment_schema`.
Only artifacts without any recognizable current-format declaration fall
through to legacy identification (`experiment_schema`,
`engine_schema_version` — the field lifecycle_admission summaries carry),
and artifacts with neither marker stay `unrecognized_format` — never
explained as data corruption, never granted eligibility. Related tests and
docs stop using `evidence_schema` as the current-format marker.

## B. Overall-status naming

`ready_with_corrupt_evidence` / `ready_with_legacy_artifacts` suggested
strategy readiness. Renamed to `report_has_evidence_issues` (corrupt
present), `report_has_legacy_artifacts` (legacy only) and `clean`; strategy
eligibility remains the per-strategy checks' business.

## C. JSON failure output

`research-status --json` emits parseable structured JSON (status
composition_failed plus reason) with exit code 2 when the status cannot be
composed. Four-path regressions cover normal, legacy-note, corrupt and
composition-failure exit codes and text/JSON agreement.

## D. Cleanup and honest records

Tracked `.fix_status.py` and `.fix_cls.py` removed by a follow-up commit; a
sweep confirms no other stray patch scripts. The local status query is
re-run read-only and the actual classification counts with representative
reasons are reported against real files.

## E. Budget

Code, tests and documentation only. No real package regeneration,
downloads, canonical writes, models, economic paths, orders or promotions.
