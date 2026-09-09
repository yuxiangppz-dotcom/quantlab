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
  -> local bridge wakes Codex for that committed event exactly once
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

Report/block submission also queues a reviewer notification in the same SQLite
transaction. Delivery happens only after commit and contains only generation,
event action, and event SHA-256—not task or report content. A private delivery
token makes concurrent or stale workers fail closed. Failed delivery remains in
SQLite and is retried by the next ZCode status check; a delivered event is never
relaunched. Notification failure cannot roll back or hide the submitted report.

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
uv run python scripts/agent_loop.py codex-bridge-status
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

## Event bridge setup: dedicated reviewer topology

The delivery target is always a **dedicated reviewer thread** created and
verified by `bootstrap-codex-reviewer`. Only a configuration whose
`probe_status` is `verified` may load as enabled: the load path rejects any
other status, any missing or malformed structured persistence evidence, and
any `thread/read`/`thread/resume` response that does not echo the requested
thread id. Configuration creation is available only through the bounded
bootstrap path; a failed or incomplete bootstrap stays recorded in
`.agent-loop/bridge/bootstrap.json` and is never advertised as ready. Never
bind the bridge to the current interactive desktop Codex task: an independent
App Server cannot resume a thread another process owns, and a freshly started
thread with no completed turn is not persisted (`no rollout found`). A
foreign/interactive `already has an active writer` target discovered during
bootstrap or validation is a deterministic configuration failure, not a
transient one.

```bash
uv run python scripts/agent_loop.py bootstrap-codex-reviewer \
  --codex-executable '<absolute local codex executable path>'
uv run python scripts/agent_loop.py codex-bridge-status
```

The bootstrap command:

1. starts a new App Server thread rooted at this repository;
2. runs exactly one fixed, content-free bootstrap turn and waits for
   `turn/completed` with status `completed`;
3. explicitly releases the creator's writer with `thread/unsubscribe`, closes
   that server, opens a fresh process, and proves `thread/read` and
   `thread/resume` succeed for the same thread id and echo it back;
4. explicitly releases the verifier's writer too; a target is not ready unless
   both unsubscribe operations are acknowledged as `unsubscribed`;
5. only then atomically writes the Git-ignored bridge config
   (`.agent-loop/codex_bridge.json`, protocol
   `quantlab_codex_review_bridge_v3`) containing the dedicated thread id, the
   executable path, a SHA-256 **configuration fingerprint** binding
   thread + executable + checkout, and probe evidence (no secrets);
6. records every attempt under `.agent-loop/bridge/bootstrap.json`;
   incomplete attempts are marked `incomplete` and never advertised as ready.

`probe-codex-reviewer` runs the same persistence proof without writing any
configuration; it is a bounded, opt-in, non-destructive check of the local
Codex installation. Tests never call the real service.

### Delivery and retry classes

Each delivery attempt is bound to the committed event SHA-256, generation,
dedicated thread id, and configuration fingerprint. Exactly one live attempt
exists per event, and — enforced by a partial unique index, transactionally at
claim time — at most one live attempt exists per dedicated reviewer target
(`config_fingerprint`, `thread_id`) across all events and generations. A
different event that finds a fresh live attempt on its target receives a
deterministic `target_busy` no-op and launches no process and no model turn;
the same-event duplicate stays a no-op. A live attempt older than the bounded
stale interval (`stale_delivery_seconds`) is superseded automatically. That
transition also moves the old notification out of `launching`, clears its raw
token and process id, and grants the new attempt in the same database
transaction. The old worker therefore cannot finish after a takeover, and a
crashed worker cannot wedge the target forever. `delivered` requires the exact
live attempt, the SHA-256 of its raw token, its target binding, the exact
returned turn id, and a `completed` status. The worker commits that turn id as
soon as `turn/start` succeeds, so status exposes `delivery_stage=starting`
before a turn exists and `delivery_stage=reviewing` only after the exact turn
has been created. Bootstrap, verification, and completed delivery all require
an acknowledged `thread/unsubscribe`, preventing an otherwise idle reviewer
from retaining an active-writer lock. Immediate App Server RPCs are bounded to
60 seconds independently of the longer model-turn timeout. While a model turn
is active, the worker periodically re-reads the exact turn: this refreshes its
mailbox heartbeat and detects a terminal `failed`, `interrupted`, or
`cancelled` turn even if `turn/completed` was not delivered on the original
stream. Exceptional process cleanup is also bounded. Failures are classified:

- **configuration** — `no rollout found`, a broken or fingerprint-mismatched
  configuration, or a foreign/interactive target discovered during bootstrap
  or validation. Automatic retries stop after the first failure for that
  fingerprint; a new successfully bootstrapped configuration, or an explicit
  `uv run python scripts/agent_loop.py recover-bridge-delivery --reason ...`,
  re-queues the event.
- **transient** — everything else (process exit, stream loss, timeout, launch
  failure), including an `already has an active writer`/busy overlap on the
  verified dedicated target. A transient overlap fails the attempt closed,
  schedules the normal exponential backoff (60 s base, 1 h cap), and never
  sets `blocked_fingerprint`, so a legitimate busy verified reviewer can never
  poison a valid configuration.

Worker logs live under `.agent-loop/bridge/logs/<event-sha256>.log`. The
delivery token is handed to the worker only through the
`QUANTLAB_AGENT_LOOP_DELIVERY_TOKEN` environment variable. The worker removes
the variable from its own environment immediately after reading the token and
keeps the value in memory only, and every Codex App Server process —
bootstrap, probe, and delivery — is launched with an explicitly sanitized
environment that excludes the variable. The raw token therefore never reaches
the reviewer App Server or any descendant process, stays out of argv, logs,
projections, and public status JSON, and is redacted from stored errors as a
final defense in depth. A report/block transaction always commits even if
delivery later fails; a failed or blocked delivery is retried by the next
ZCode `status` check. The wake prompt carries only fixed reviewer instructions
plus generation, action, and event digest; artifact bodies are read by the
reviewer from the authoritative mailbox.

The bridge uses the official Codex App Server to resume the dedicated thread
and start one review turn. Keep the Codex scheduled reviewer automation paused:
it is no longer part of the normal loop. The 20-minute ZCode schedule remains
useful for finding new implementation tasks and retrying a failed local
notification, but it does not wake or spend a Codex turn while nothing is
actionable.

For deliberate delivery recovery, inspect `codex-bridge-status` and the logs
under `.agent-loop/bridge/logs/`, then either re-run `bootstrap-codex-reviewer`
(new probed configuration) or run:

```bash
uv run python scripts/agent_loop.py recover-bridge-delivery \
  --reason '<why the previous worker is no longer authoritative>' \
  --actor '<operator identity>'
```

Recovery requires the currently enabled, verified bridge configuration. It
atomically supersedes any live holder of that exact reviewer target, clears the
old notification token/process authority, re-queues the current event, and
appends an immutable audit record containing the actor, reason, timestamp,
current event, revoked event/attempt when present, configuration fingerprint,
and thread id. `codex-bridge-status` exposes recent audit records without raw
tokens. A missing or ambiguous binding fails closed. `notify-reviewer
--synchronous` is a diagnostic mode and waits for the Codex turn to complete.

### Recovery ownership validation

Before any write, and inside the same transaction, recovery validates the
current notification's ownership:

- a `launching` current notification must own exactly one live attempt whose
  generation, action, raw-token hash, fingerprint, and thread match that
  notification, and that attempt's target must equal the requested verified
  target;
- a `queued` or `failed` current notification must own no live attempt for its
  event before any different event's holder is considered.

When the current event is launching on a target that differs from the active
verified configuration — reachable through ordinary operations after a bridge
rotation — recovery is rejected with a precise configuration/ownership
mismatch and performs zero mutations: no attempt is superseded, no capability
is cleared, no audit row is written, and `doctor` remains healthy. The audit
schema records a single revoked attempt, so recovery never performs
multi-target revocation; mismatched multi-target ownership fails closed and
stays available for explicit operator resolution. Recovery remains permitted
for a launching attempt on the exact verified target, a queued or failed
current event behind a prior-event holder on that target, and a queued or
failed current event with no holder.

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
scheduled work. Codex is invoked only after a report or blocker commits; it no
longer runs a 20-minute polling turn.

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
