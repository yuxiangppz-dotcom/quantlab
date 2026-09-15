# Direct Codex repair of PR #188

Expected head: `2400aa7b8fb4ad3161c055094b65ab0a8374b782`.
User directly commissions Codex to implement the remaining repairs. No concurrent executor detected; use an isolated checkout and append commits to the existing PR branch.

Scope: structured CLI failure output; actual CLI exit/output tests; exact known legacy identities and explicit malformed parsing; preserve strict evidence semantics. Read-only local status verification. No data downloads, canonical writes, historical replay, models, orders or automation changes.

Checks: targeted tests, full pytest, ruff, diff check, local read-only status, final-head CI. Keep old task cards as historical records; PR is #188 and main baseline is ad5e161.
