# S4 admission fix card v7 (frozen gap identities + manifest fact checks)

Direct commission continuation on `codex/s4-first-replay-admission-v1`.
Reviewed base `197f147a1bca3ce4a6836c7523cc623429f73b67`; mainline
`eb6a832`; append-only; Codex tree read-only; automation paused. The real
input-package generator and any migration stay untouched this round;
v1-v5 and all proofs preserved.

## A. The six gap identities are frozen in the verifier

The verifier currently derives its required sources from the report's own
prior20_gaps and only checks the count is six — six copies of one gap plus
five deleted request sets pass. The verifier now carries the frozen gap
identity set (000301.SZ 2021-12-22; 000777.SZ 2021-12-07/08/09/10/13) as
an independent constant: the report's pairs must match it exactly — no
duplicates, no omissions, no substitutions, no extra dates — and the
required request files and consumed partition set are derived from that
frozen set, never narrowed by the report. The independent re-derivation
logic is unchanged; production functions are still not called.

## B. Manifest v2 fact checks completed

- All three base artifacts (plan, reconciliation, profiles) get their
  embedded fingerprints recomputed from the file bytes and compared with
  the frozen identities — profiles was previously unchecked.
- `fingerprint_checks` must contain exactly the three base keys, and each
  recorded fingerprint must equal both the recomputed value and the frozen
  identity; missing, forged or mismatching entries fail.
- Every files entry's `bytes` must equal the actual file size; root must
  agree with key and declared root (already enforced, now also byte-checked).
- `bound_files` has an exact expected membership derived from the producer
  binding contract: the attempt triples for all six gap dates plus every
  consumed partition file — base artifacts are bound through the main
  binding, not through bound_files. Exact set equality plus per-entry
  hash/byte agreement; deleting a member while keeping it non-empty fails.

## C. Regressions

Six tamper regressions with completed hashes kept internally consistent:
duplicated gap with the other five request sets deleted; one substituted
securities/date; profiles content changed with outer hashes synchronised;
fingerprint_checks removed or forged; bytes falsified to 0 in both files
and bound_files; one bound_files member deleted while staying non-empty.
The clean producer-to-verifier pass is retained.

## D. Budget

Code, tests and documentation only. No real package regeneration, no
downloads, canonical writes, models, economic paths, orders or
promotions. Full pytest/ruff/diff at the final head with PR CI verified.
