# Task Card — Shared Agent Loop Handshake v1

## Starting contract

- Work from the exact `expected_head` stored in the agent-loop task artifact.
- This is a read-only/no-code handshake. It explicitly permits
  `submit-report --allow-no-commit` and requires no commit.
- Do not edit tracked files, create commits, push, call providers, write
  canonical data, submit orders, invent fills, or make performance claims.

## Required actions

1. Read `AGENTS.md`, `docs/agent_loop.md`, and the current immutable task.
2. Run `uv run python scripts/agent_loop.py doctor`.
3. Independently verify Git is clean, current HEAD equals `origin/master`, and
   the current task's `expected_head` equals that HEAD.
4. Verify the projected task Markdown and `state.json` agree with the CLI output
   for generation, phase, task SHA-256, and expected HEAD.
5. Run `uv run pytest tests/agent_loop/test_protocol.py`,
   `uv run ruff check src/quantlab/agent_loop scripts/agent_loop.py
   tests/agent_loop/test_protocol.py`, and `git diff --check`.
6. Write an Agent Run Report with the observed values and exact exit codes.
7. Submit it with `submit-report --allow-no-commit` and the claim token.

## Completion gates

- No tracked or untracked non-mailbox file changed.
- Doctor reports healthy.
- Targeted tests, lint, and diff check pass.
- The report is accepted by the protocol and phase becomes `REVIEW_READY`.

If any binding or test fails, do not repair the protocol during this task.
Submit a blocker with the exact evidence.
