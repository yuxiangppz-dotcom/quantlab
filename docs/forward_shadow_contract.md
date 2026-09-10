# Forward Shadow Portfolio Contract

Forward Shadow records immutable, non-trading predictions from a completed Daily snapshot.
Its target portfolio must use the same exact-count portfolio construction contract as the
Daily product instead of re-implementing ranking and weighting in the research layer.

## Construction semantics

For each configured model, Forward Shadow maps its score into the portfolio layer with:

- exact `target_count` rather than fractional universe selection;
- the model's declared `higher_is_better` or `lower_is_better` direction;
- deterministic score ordering with `instrument_id` as the tie-break;
- gross exposure of 1.0 for the current v1 shadow contract;
- the configured `max_weight_per_name`, with residual capital left as cash.

The immutable prediction manifest records the resolved portfolio contract, realized target
count, target weight sum, and residual cash weight. These fields are evidence about what was
frozen at prediction time; they do not imply broker submission, fills, or strategy promotion.

Historical research that keeps all ties at a fractional cutoff remains a separate contract.
Forward Shadow must not silently substitute that research constructor for the Daily exact-count
product semantics.

## Temporal admission v2

New predictions bind `created_at`, the source Daily `generated_at`, the exact
Daily report bytes, and a derived temporal admission decision into their content
hash. Readers recompute admission rather than trusting a declared boolean.
Changing either timestamp invalidates the prediction. Source bytes are captured
and revalidated before scores and their provenance are frozen.

The conservative research registration policy is
`same_signal_day_1600_to_midnight_shanghai_v1`: both the Daily source and prediction
must be observed on the signal day at or after 16:00 Asia/Shanghai, with prediction
creation strictly before next calendar-day midnight and no earlier than its source.
This deliberately excludes even next-day pre-open registration; it does not infer
an exchange opening time, trading permission, or provider publication completeness.
It changes no backtest execution lag, lifecycle convention, portfolio construction,
or label formula. A later policy requires an explicitly versioned contract.

Late or early observations are archived with an ineligible reason. They are not
evaluated as forward evidence, counted as pending eligible predictions, included
in complete-return aggregates or paired diagnostics, or used to satisfy strategy
readiness. `excluded_prediction_count` makes this exclusion visible. The CLI and
Daily UI also display temporal admission. Label maturity alone is insufficient.

Version 1 timestamps were not part of the hash. All v1 predictions therefore
remain readable, byte-preserved historical artifacts with
`blocked_legacy_unverified_time`, even if their stated timestamps look timely.
Old complete evaluations cannot upgrade their eligibility. Multiple legacy
artifacts are reported as excluded, without choosing a favorable winner. Distinct
eligible predictions for the same model/version/date still fail closed.

A retry reuses the first v2 observation time and fingerprint, including on a later
day; an earlier-clock retry is rejected. Changing inputs for an existing v2
model/version/date is a conflict. Publication remains a local single-writer
workflow; this contract is not a cross-process transactional registry.

These are local-clock and content-integrity guarantees, not a trusted timestamp
service or proof against an actor rewriting and rehashing the entire artifact.
Canonical input retention, historical identity versions and complete label lineage
remain separate reproducibility prerequisites. All labels remain overlapping
research diagnostics, never a compounded account NAV or executable return.
