# S5-B v1.9 content-addressed diagnostic delivery bundle

Issue: #157. Starting master:
`62a68740c30642d8e33de08d6fd3718784e86dd6`.

This task packages the typed S5-B metrics, single-run seal and deterministic
Chinese review into immutable in-memory files. Stable JSON and Markdown bytes,
lengths and SHA-256 identities are bound by one bundle fingerprint and can be
verified before a later local runner writes anything.

The builder independently rebuilds the review from metrics and seal. The
verifier checks exact filenames, media types, byte lengths, hashes, JSON syntax
and embedded evidence fingerprints. It supplies content integrity, not
cryptographic signer identity.

No provider access, Canonical read/write, local data, real outcome
materialization, filesystem persistence, parameter scan, holding-policy choice,
performance verdict, promotion, account mutation or order is authorized.
Synthetic tests only.
