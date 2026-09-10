# QuantLab Mainline Task: Strategy ↔ Forward Shadow Binding v1

Status: active

## Goal

Make the strategy evidence registry mechanically bind each strategy that is in a forward-observation state to exactly one immutable Forward Shadow model identity. Prevent identifier drift such as a strategy name differing from its shadow model name from turning into orphan or misattributed evidence.

## Constraints

- No provider calls or Canonical writes.
- No historical performance recomputation.
- No automatic strategy promotion or approval.
- Do not rename existing Forward Shadow model identities; old immutable evidence must remain addressable.
- Keep the current Strategy Registry lifecycle semantics unchanged.

## Acceptance

- Registry entries in forward-observation states declare an explicit `forward_model_id`.
- A binding audit verifies `(forward_model_id, version)` exists exactly once in the configured Forward Shadow model set.
- The registered score source and direction exactly match the Forward Shadow model contract.
- Forward Shadow models cannot remain orphaned from the registry and one shadow model cannot be claimed by multiple strategies.
- The configured Forward Shadow file is present in the strategy evidence references.
- Invalid/missing/duplicate bindings fail closed.
- Synthetic tests cover the current baseline name mismatch, orphan models, duplicate claims, version drift, and score-direction/source drift.
- Full repository CI, Ruff, diff-check, and optional research runtime pass before merge.
