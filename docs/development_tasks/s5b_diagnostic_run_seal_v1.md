# S5-B v1.7 single-run diagnostic result seal

Issue: #153. Starting master:
`4949ffd6ab363b81a4d712453af9ade3213cbf0a`.

This task adds the pure completion seal between the deterministic metric kernel
and a later local persistence runner. It binds the frozen protocol, readiness,
admitted input and independently recomputed metrics, enforcing the protocol's
single-run budget without inventing a performance verdict.

An exact repeat is idempotent so infrastructure recovery does not spend a
second run. A changed input or result under the same protocol is a forbidden
rescan and fails closed. The caller supplies an aware timestamp, which is
normalized to UTC before fingerprinting.

No provider access, Canonical read/write, real outcome materialization,
persistence write, parameter rescan, holding-policy decision, performance
claim, promotion, account mutation or order is authorized. Synthetic tests
only.
