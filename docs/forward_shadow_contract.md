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
