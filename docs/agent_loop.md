# ZCode + Codex Shared Agent Loop

## Purpose

This local-only protocol removes repeated copy/paste between a ZCode executor
and a Codex reviewer while preserving independent review. It coordinates work;
it does not change any research, backtest, execution, fee, fill, or performance
semantics.

The loop is deliberately sequential:

```text
Codex publishes immutable task + expected HEAD
  -> ZCode atomically claims
  -> ZCode implements, tests, commits, and pushes
  -> ZCode submits immutable report + final HEAD
  -> Codex independently reviews repository evidence
  -> Codex publishes rework/next task, or stops BLOCKED/COMPLETE
```

## Authority and failure model

`.agent-loop/loop.sqlite3` is the sole authority. The directory is Git-ignored.
`state.json` and the `tasks/`, `reports/`, and `reviews/` Markdown files are
human-readable projections regenerated from SQLite.

Every transition is an SQLite `BEGIN IMMEDIATE` transaction. The protocol binds:

- one monotonically increasing generation;
- one immutable task/report/review artifact per generation;
- SHA-256 for every artifact and a hash-chained event log;
- task `expected_head`, report `final_head`, and Git ancestry;
- clean workspace and `HEAD == upstream` at task publication, successful report
  submission, and review commit;
- one atomic executor claim with a bounded lease and an unguessable claim token.

A crash before transaction commit changes nothing. A crash after commit but
before projection writes is repaired by the next `status` or `doctor` command.
Projected-file tampering is overwritten from SQLite; database or event-chain
tampering fails closed.

## State machine

| Phase | Owner | Allowed next action |
|---|---|---|
| `IDLE` | reviewer | publish first task |
| `TASK_READY` | executor | claim task |
| `EXECUTING` | executor | submit successful report or blocker |
| `REVIEW_READY` | reviewer | review and advance/rework/block/complete |
| `BLOCKED` | human/reviewer | resolve cause, clean and push, then explicitly resume |
| `COMPLETE` | human | no further automatic work |

Agents must not infer work from a Markdown file alone. They first ask the CLI
for their role-specific action.

## Commands

Run all commands from the repository root:

```bash
uv run python scripts/agent_loop.py init --project-id quantlab
uv run python scripts/agent_loop.py status --role executor
uv run python scripts/agent_loop.py status --role reviewer
uv run python scripts/agent_loop.py doctor
uv run python scripts/agent_loop.py show task
uv run python scripts/agent_loop.py show report
```

Reviewer publishes the first task:

```bash
uv run python scripts/agent_loop.py publish-task \
  --task-file .agent-loop/drafts/task.md \
  --title "Task title" \
  --expected-head "$(git rev-parse HEAD)"
```

Executor claims and retains the returned `claim_token`:

```bash
uv run python scripts/agent_loop.py claim --agent zcode --lease-hours 24
```

After all required commits are clean and pushed:

```bash
uv run python scripts/agent_loop.py submit-report \
  --report-file .agent-loop/drafts/report.md \
  --title "Agent Run Report" \
  --claim-token '<token returned by claim>'
```

`--allow-no-commit` is permitted only when the immutable task card explicitly
defines a read-only/no-code run. A blocked executor instead uses:

```bash
uv run python scripts/agent_loop.py block \
  --blocker-file .agent-loop/drafts/blocker.md \
  --title "Execution blocker" \
  --claim-token '<token returned by claim>'
```

Reviewer atomically records the review and next task:

```bash
uv run python scripts/agent_loop.py submit-review \
  --review-file .agent-loop/drafts/review.md \
  --decision advance \
  --title "Independent review" \
  --next-task-file .agent-loop/drafts/next-task.md \
  --next-task-title "Next bounded task"
```

`rework` also requires a next-task file. `blocked` and `complete` forbid one.
An executing lease is never silently stolen; after expiry the reviewer may run
`expire-claim --reason ...`, which moves the loop to `BLOCKED`.

If the user has positively identified and deleted the ZCode run that owns a
still-live claim, do not wait for expiry and do not edit SQLite. Use the exact
last event hash and agent identity shown by `status`:

```bash
uv run python scripts/agent_loop.py requeue-abandoned \
  --reason "owning ZCode run was deleted by the user" \
  --expected-event-sha256 '<exact last_event_sha256>' \
  --expected-claim-agent zcode
```

This preserves the abandoned generation and creates a new immutable generation
bound to the current clean, pushed HEAD. A stale event hash or agent mismatch is
rejected without mutation.

## One-time ZCode setup

1. Keep this WSL project open in ZCode.
2. Open **Automations** and create a scheduled task for the QuantLab project.
3. Set the interval to **20 minutes**.
4. Use an execution mode that can edit files, run tests, commit, and push without
   waiting for confirmation. Limit it to this repository and keep the project
   boundaries in `AGENTS.md` enabled.
5. Paste the complete prompt from `docs/agent_loop_zcode_prompt.md`.
6. Run once manually after Codex has published the handshake task. Later runs
   are no-ops unless `status --role executor` returns `claim_task`.

Both desktop applications and the computer must remain running for local
scheduled work. The Codex reviewer runs in the current chat every 20 minutes;
it stays quiet when there is no new report or blocker.

## Operations and recovery

- Inspect: `uv run python scripts/agent_loop.py doctor`.
- Never delete/reinitialize the mailbox to clear an error; preserve it as
  evidence and diagnose the cause.
- If ZCode is legitimately still working, do not expire its lease.
- If ZCode blocks with partial files, the reviewer reports the dirty state and
  asks for a deliberate recovery decision. It does not discard work.
- If a task/report HEAD differs from the repository, stop. Do not rebase, amend,
  force-push, or rewrite the protocol record.
- Pause both scheduled tasks before manually editing the same checkout.

## Report minimum

Every successful executor report includes:

- generation and task title;
- starting and final HEAD;
- each part's commit SHA, complete message, and push status;
- files changed and design decisions;
- failing tests reproduced before fixes;
- targeted and full verification results;
- deviations and unresolved limitations;
- explicit confirmation of prohibited actions not taken;
- honest freeze or next-step recommendation.
