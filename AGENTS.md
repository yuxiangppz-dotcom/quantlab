# QuantLab Agent Instructions

These rules apply to every coding agent working in this repository.

## Non-negotiable project boundaries

- Preserve point-in-time correctness and the frozen lifecycle/backtest semantics.
- Prefer framework, accounting, evidence, and workflow correctness over searching
  for a useful alpha.
- Do not call a data provider, write canonical data, submit an external order,
  invent a fill, or make a performance claim unless the active task card
  explicitly authorizes that exact action.
- Unknown evidence remains unknown. Do not silently replace it with a favorable
  assumption, zero fee, tradable status, or a synthetic fact presented as real.
- Keep research/backtest concepts separate from executable prices, orders,
  broker reports, and live authority.
- Never commit secrets, tokens, local agent-loop state, or generated experiment
  data.

## Shared ZCode/Codex loop

When running from the scheduled ZCode/Codex workflow, `.agent-loop/loop.sqlite3`
is the only coordination authority. Markdown and JSON files in that directory
are projections, not commit markers.

- Do not act unless `scripts/agent_loop.py status --role <role>` returns an action
  assigned to your role.
- The executor must claim exactly one task before editing. It may implement only
  the immutable task artifact for that generation.
- The reviewer never edits tracked project files. It independently checks the
  task, report, Git range, tests, and financial semantics, then records a review
  and either a rework card, the next card, `BLOCKED`, or `COMPLETE`.
- Do not hand-edit the SQLite database, `state.json`, projected task/report files,
  event chain, generation number, claim token, or expected HEAD.
- If protocol validation fails, stop and report the exact error. Do not bypass
  the gate or recreate the mailbox.

## Git and verification

- Start only from the task card's exact clean, pushed `expected_head`.
- Keep unrelated user changes intact.
- Complete, test, commit, and push each independently reviewable task part before
  proceeding to the next part. Do not amend or force-push a commit that has been
  handed to the reviewer.
- Finish with the task-card checks plus `uv run pytest`, `uv run ruff check .`,
  and `git diff --check`, unless the card explicitly narrows a read-only run.
- Reports must state starting/final HEAD, commits and push status, files changed,
  failures found and fixed, test results, deviations, unresolved limitations,
  prohibited actions not taken, and an honest freeze/next-step recommendation.

## Stop conditions

Publish a blocker instead of guessing when work requires credentials, provider
access, canonical writes, external order submission, destructive recovery,
unverified financial rules, a material change of research scope, or resolution
of unrelated dirty-worktree changes.
