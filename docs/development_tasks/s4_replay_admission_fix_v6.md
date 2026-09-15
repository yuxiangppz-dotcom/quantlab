# S4 admission fix card v6 (manifest format v2 + full consumption-set closure)

Direct commission continuation on `codex/s4-first-replay-admission-v1`.
Reviewed base `8bc3f58a57ad0137084862fac8c5131948aa5f28`; mainline
`eb6a832`; append-only; Codex tree read-only; automation paused. The real
input-package generator is NOT run (v1-v5 and all proofs preserved); the
production-to-verifier contract is proven on hand-built worlds in temporary
directories.

## A. Versioned manifest format (v2) shared by producer and verifier

`validate_sealed_sources` returns the version-2 shape: every consumed file
is a `root:relative` key carrying `{path, sha256, bytes, root}`; the three
base artifacts' provenance moves to a separate `fingerprint_checks` section
(embedded fingerprint, frozen identity, match) so no file is represented
twice. `run()` emits `manifest_version: 2`, the roots, the merged `files`
map, `fingerprint_checks` and `bound_files`. The verifier requires
`manifest_version == 2` and explicitly rejects any other version as legacy
("regenerate with the current producer") — no silent relaxation. Docs and
tests updated to the v2 shape.

## B. Verifier consumes the full set and checks bound_files

The verifier no longer accepts prefix existence: for every gap date it
enumerates the daily and suspension partition parquet files itself and
requires a one-to-one manifest mapping (exact `root:relative` keys, exact
paths under the declared roots, recomputed hashes and byte counts), plus
the exact attempt triple keys. `bound_files` must be non-empty and every
entry must match the corresponding `files` entry's hash and byte count.
Missing consumed files, wrong aliases, forged hashes, contradictory
bound_files, wrong roots or a legacy/absent manifest version all fail.

## C. Production-to-verifier contract test

A new regression builds the hand world, runs the real producer entry
(`run()`), passes the untouched output straight to the independent
verifier (normal scenario must pass), then injects five faults — a deleted
manifest entry, a redirected path, a forged hash, a wrong root and a
contradictory bound_files entry — each of which must fail. The verifier
keeps its independent logic; no production classification function is
called to prove production output.

## D. Budget

Code, tests and documentation only. v1-v5 and their proofs stay as-is; the
existing v5 proof is explicitly flagged as verified by the older verifier
and a fresh proof (v6) awaits the next authorized run. Full pytest/ruff/
diff at the final head with PR CI verified.
