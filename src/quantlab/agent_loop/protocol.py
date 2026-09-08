"""Transactional local mailbox for sequential ZCode/Codex hand-offs.

The SQLite database is authoritative.  Human-readable files under the mailbox
are projections which are recreated from the database on every successful
command.  This avoids treating a partially-written Markdown file as a commit
point when either desktop agent is interrupted.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

PROTOCOL_VERSION = "quantlab_agent_loop_v1"
PHASES = {
    "IDLE",
    "TASK_READY",
    "EXECUTING",
    "REVIEW_READY",
    "BLOCKED",
    "COMPLETE",
}
DECISIONS = {"advance", "rework", "blocked", "complete"}
ARTIFACT_KINDS = {"task", "report", "review"}
NOTIFICATION_STATES = {"queued", "launching", "delivered", "failed"}
RETRY_CLASSES = {"", "transient", "configuration"}
DELIVERY_ATTEMPT_STATES = {
    "live",
    "delivered",
    "failed_configuration",
    "failed_transient",
    "superseded",
}
RETRY_BACKOFF_BASE_SECONDS = 60
RETRY_BACKOFF_MAX_SECONDS = 3_600


class AgentLoopError(RuntimeError):
    """A fail-closed protocol or repository validation error."""


@dataclass(frozen=True)
class GitSnapshot:
    head: str
    branch: str
    upstream: str | None
    upstream_head: str | None
    clean: bool

    @property
    def pushed(self) -> bool:
        return self.upstream_head == self.head if self.upstream is not None else False


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_nonempty(path: Path, label: str) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AgentLoopError(f"cannot read {label}: {path}: {exc}") from exc
    if not content.strip():
        raise AgentLoopError(f"{label} must not be empty: {path}")
    return content


def _git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise AgentLoopError(f"git {' '.join(args)} failed: {detail}")
    return result


def discover_repo_root(start: Path | None = None) -> Path:
    base = (start or Path.cwd()).resolve()
    result = _git(base, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve()


def git_snapshot(repo_root: Path) -> GitSnapshot:
    head = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
    branch = _git(repo_root, "branch", "--show-current").stdout.strip()
    if not branch:
        raise AgentLoopError("detached HEAD is not supported by the shared agent loop")
    status = _git(repo_root, "status", "--porcelain", "--untracked-files=normal").stdout
    upstream_result = _git(
        repo_root,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        check=False,
    )
    upstream = upstream_result.stdout.strip() if upstream_result.returncode == 0 else None
    upstream_head = _git(repo_root, "rev-parse", upstream).stdout.strip() if upstream else None
    return GitSnapshot(
        head=head,
        branch=branch,
        upstream=upstream,
        upstream_head=upstream_head,
        clean=not status.strip(),
    )


class AgentLoop:
    """Transactional state machine backed by a local SQLite database."""

    def __init__(self, repo_root: Path, mailbox: Path | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.mailbox = (mailbox or self.repo_root / ".agent-loop").resolve()
        self.database = self.mailbox / "loop.sqlite3"

    def _connect(self, *, require_initialized: bool = True) -> sqlite3.Connection:
        if require_initialized and not self.database.is_file():
            raise AgentLoopError(
                f"agent loop is not initialized; run init first ({self.database})"
            )
        self.mailbox.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        if require_initialized:
            self._ensure_notification_schema(connection)
        return connection

    @staticmethod
    def _ensure_notification_schema(connection: sqlite3.Connection) -> None:
        """Idempotently migrate delivery state; legacy rows are preserved."""

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_attempts (
                attempt_id TEXT PRIMARY KEY,
                event_sha256 TEXT NOT NULL,
                generation INTEGER NOT NULL,
                event_action TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL DEFAULT '',
                thread_id TEXT NOT NULL DEFAULT '',
                attempt_number INTEGER NOT NULL,
                delivery_token_sha256 TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                turn_id TEXT NOT NULL DEFAULT '',
                classification TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (event_sha256) REFERENCES events(event_sha256)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_recoveries (
                recovery_id TEXT PRIMARY KEY,
                current_event_sha256 TEXT NOT NULL,
                revoked_event_sha256 TEXT,
                revoked_attempt_id TEXT,
                config_fingerprint TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (current_event_sha256) REFERENCES events(event_sha256),
                FOREIGN KEY (revoked_event_sha256) REFERENCES events(event_sha256),
                FOREIGN KEY (revoked_attempt_id) REFERENCES delivery_attempts(attempt_id)
            )
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS one_live_delivery_attempt_per_event
            ON delivery_attempts(event_sha256) WHERE state = 'live'
            """
        )
        try:
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_live_delivery_attempt_per_target
                ON delivery_attempts(config_fingerprint, thread_id) WHERE state = 'live'
                """
            )
        except sqlite3.IntegrityError as exc:
            raise AgentLoopError(
                "ambiguous live delivery history: historical rows leave more "
                "than one live delivery attempt on a single reviewer target "
                "(config_fingerprint, thread_id); resolve them explicitly "
                "without discarding attempt history"
            ) from exc
        existing = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(review_notifications)")
        }
        if not existing:
            return
        for column, definition in (
            ("config_fingerprint", "TEXT NOT NULL DEFAULT ''"),
            ("retry_class", "TEXT NOT NULL DEFAULT ''"),
            ("next_retry_at", "TEXT NOT NULL DEFAULT ''"),
            ("blocked_fingerprint", "TEXT NOT NULL DEFAULT ''"),
            ("thread_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE review_notifications ADD COLUMN {column} {definition}"
                )

    def initialize(self, *, project_id: str = "quantlab") -> dict[str, Any]:
        if not project_id.strip():
            raise AgentLoopError("project_id must not be empty")
        self.mailbox.mkdir(parents=True, exist_ok=True)
        with self._connect(require_initialized=False) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    kind TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    git_head TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (kind, generation)
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_sha256 TEXT,
                    event_sha256 TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS review_notifications (
                    event_sha256 TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    event_action TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    delivery_token TEXT NOT NULL DEFAULT '',
                    process_id INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    config_fingerprint TEXT NOT NULL DEFAULT '',
                    retry_class TEXT NOT NULL DEFAULT '',
                    next_retry_at TEXT NOT NULL DEFAULT '',
                    blocked_fingerprint TEXT NOT NULL DEFAULT '',
                    thread_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (event_sha256) REFERENCES events(event_sha256)
                );
                CREATE TABLE IF NOT EXISTS delivery_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    event_sha256 TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    event_action TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL DEFAULT '',
                    thread_id TEXT NOT NULL DEFAULT '',
                    attempt_number INTEGER NOT NULL,
                    delivery_token_sha256 TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    turn_id TEXT NOT NULL DEFAULT '',
                    classification TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (event_sha256) REFERENCES events(event_sha256)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_live_delivery_attempt_per_event
                ON delivery_attempts(event_sha256) WHERE state = 'live';
                CREATE UNIQUE INDEX IF NOT EXISTS one_live_delivery_attempt_per_target
                ON delivery_attempts(config_fingerprint, thread_id) WHERE state = 'live';
                CREATE TABLE IF NOT EXISTS delivery_recoveries (
                    recovery_id TEXT PRIMARY KEY,
                    current_event_sha256 TEXT NOT NULL,
                    revoked_event_sha256 TEXT,
                    revoked_attempt_id TEXT,
                    config_fingerprint TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (current_event_sha256) REFERENCES events(event_sha256),
                    FOREIGN KEY (revoked_event_sha256) REFERENCES events(event_sha256),
                    FOREIGN KEY (revoked_attempt_id) REFERENCES delivery_attempts(attempt_id)
                );
                """
            )
            existing = connection.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]
            if existing:
                self._validate_metadata(connection)
            else:
                now = _utc_now()
                connection.execute("BEGIN IMMEDIATE")
                try:
                    values = {
                        "protocol_version": PROTOCOL_VERSION,
                        "project_id": project_id.strip(),
                        "phase": "IDLE",
                        "generation": "0",
                        "expected_head": "",
                        "claim_agent": "",
                        "claim_token": "",
                        "claim_expires_at": "",
                        "updated_at": now,
                    }
                    connection.executemany(
                        "INSERT INTO metadata(key, value) VALUES (?, ?)", values.items()
                    )
                    self._append_event(
                        connection,
                        actor="system",
                        action="initialized",
                        generation=0,
                        payload={"project_id": project_id.strip()},
                        occurred_at=now,
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        return self.status(role="reviewer")

    def _metadata(self, connection: sqlite3.Connection) -> dict[str, str]:
        return {row["key"]: row["value"] for row in connection.execute("SELECT * FROM metadata")}

    def _validate_metadata(self, connection: sqlite3.Connection) -> dict[str, str]:
        metadata = self._metadata(connection)
        required = {
            "protocol_version",
            "project_id",
            "phase",
            "generation",
            "expected_head",
            "claim_agent",
            "claim_token",
            "claim_expires_at",
            "updated_at",
        }
        if set(metadata) != required:
            raise AgentLoopError(
                f"metadata keys mismatch: expected {sorted(required)}, got {sorted(metadata)}"
            )
        if metadata["protocol_version"] != PROTOCOL_VERSION:
            raise AgentLoopError(
                f"unsupported protocol_version: {metadata['protocol_version']!r}"
            )
        if metadata["phase"] not in PHASES:
            raise AgentLoopError(f"invalid phase: {metadata['phase']!r}")
        try:
            generation = int(metadata["generation"])
        except ValueError as exc:
            raise AgentLoopError("generation is not an integer") from exc
        if generation < 0:
            raise AgentLoopError("generation must be non-negative")
        return metadata

    @staticmethod
    def _set_metadata(connection: sqlite3.Connection, **values: str | int) -> None:
        connection.executemany(
            "UPDATE metadata SET value = ? WHERE key = ?",
            [(str(value), key) for key, value in values.items()],
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        actor: str,
        action: str,
        generation: int,
        payload: dict[str, Any],
        occurred_at: str | None = None,
    ) -> str:
        if not actor.strip() or not action.strip():
            raise AgentLoopError("event actor and action must not be empty")
        previous_row = connection.execute(
            "SELECT event_sha256 FROM events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous = previous_row["event_sha256"] if previous_row else None
        timestamp = occurred_at or _utc_now()
        payload_json = _canonical_json(payload)
        digest_input = {
            "occurred_at": timestamp,
            "actor": actor,
            "action": action,
            "generation": generation,
            "payload": json.loads(payload_json),
            "previous_sha256": previous,
        }
        event_sha256 = _sha256_text(_canonical_json(digest_input))
        connection.execute(
            """
            INSERT INTO events(
                occurred_at, actor, action, generation, payload_json,
                previous_sha256, event_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (timestamp, actor, action, generation, payload_json, previous, event_sha256),
        )
        return event_sha256

    @staticmethod
    def _insert_artifact(
        connection: sqlite3.Connection,
        *,
        kind: Literal["task", "report", "review"],
        generation: int,
        title: str,
        content: str,
        git_head: str,
    ) -> str:
        if kind not in ARTIFACT_KINDS:
            raise AgentLoopError(f"invalid artifact kind: {kind}")
        if not title.strip() or not content.strip():
            raise AgentLoopError(f"{kind} title and content must not be empty")
        digest = _sha256_text(content)
        try:
            connection.execute(
                """
                INSERT INTO artifacts(
                    kind, generation, title, content, sha256, git_head, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (kind, generation, title.strip(), content, digest, git_head, _utc_now()),
            )
        except sqlite3.IntegrityError as exc:
            raise AgentLoopError(
                f"immutable {kind} artifact already exists for {generation}"
            ) from exc
        return digest

    @staticmethod
    def _queue_review_notification(
        connection: sqlite3.Connection,
        *,
        event_sha256: str,
        generation: int,
        event_action: str,
        occurred_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO review_notifications(
                event_sha256, generation, event_action, state, created_at, updated_at
            ) VALUES (?, ?, ?, 'queued', ?, ?)
            """,
            (event_sha256, generation, event_action, occurred_at, occurred_at),
        )

    def publish_task(
        self,
        task_file: Path,
        *,
        title: str,
        actor: str = "codex-reviewer",
        expected_head: str | None = None,
        resume_blocked: bool = False,
        resume_complete: bool = False,
    ) -> dict[str, Any]:
        content = _read_nonempty(task_file, "task file")
        snapshot = git_snapshot(self.repo_root)
        if not snapshot.clean:
            raise AgentLoopError("cannot publish a task from a dirty git workspace")
        if not snapshot.pushed:
            raise AgentLoopError("cannot publish a task until HEAD equals its upstream")
        expected = expected_head or snapshot.head
        if expected != snapshot.head:
            raise AgentLoopError(
                f"expected_head {expected} does not equal current HEAD {snapshot.head}"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                metadata = self._validate_metadata(connection)
                allowed = (
                    metadata["phase"] == "IDLE"
                    or (resume_blocked and metadata["phase"] == "BLOCKED")
                    or (resume_complete and metadata["phase"] == "COMPLETE")
                )
                if not allowed:
                    raise AgentLoopError(
                        "publish_task requires IDLE, BLOCKED with --resume-blocked, "
                        "or COMPLETE with --resume-complete; "
                        f"got {metadata['phase']}"
                    )
                generation = int(metadata["generation"]) + 1
                digest = self._insert_artifact(
                    connection,
                    kind="task",
                    generation=generation,
                    title=title,
                    content=content,
                    git_head=expected,
                )
                now = _utc_now()
                self._set_metadata(
                    connection,
                    phase="TASK_READY",
                    generation=generation,
                    expected_head=expected,
                    claim_agent="",
                    claim_token="",
                    claim_expires_at="",
                    updated_at=now,
                )
                self._append_event(
                    connection,
                    actor=actor,
                    action="task_published",
                    generation=generation,
                    payload={"task_sha256": digest, "expected_head": expected, "title": title},
                    occurred_at=now,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.status(role="executor")

    def claim_task(
        self,
        *,
        agent: str,
        lease_hours: int = 24,
    ) -> dict[str, Any]:
        if not agent.strip():
            raise AgentLoopError("agent must not be empty")
        if lease_hours < 1 or lease_hours > 168:
            raise AgentLoopError("lease_hours must be between 1 and 168")
        snapshot = git_snapshot(self.repo_root)
        if not snapshot.clean:
            raise AgentLoopError("executor workspace must be clean before claiming a task")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                metadata = self._validate_metadata(connection)
                if metadata["phase"] != "TASK_READY":
                    raise AgentLoopError(
                        f"claim_task requires TASK_READY, got {metadata['phase']}"
                    )
                if snapshot.head != metadata["expected_head"]:
                    raise AgentLoopError(
                        f"executor HEAD {snapshot.head} does not match expected_head "
                        f"{metadata['expected_head']}"
                    )
                token = secrets.token_urlsafe(32)
                expiry = (datetime.now(UTC) + timedelta(hours=lease_hours)).isoformat().replace(
                    "+00:00", "Z"
                )
                generation = int(metadata["generation"])
                now = _utc_now()
                self._set_metadata(
                    connection,
                    phase="EXECUTING",
                    claim_agent=agent.strip(),
                    claim_token=token,
                    claim_expires_at=expiry,
                    updated_at=now,
                )
                self._append_event(
                    connection,
                    actor=agent.strip(),
                    action="task_claimed",
                    generation=generation,
                    payload={"claim_token_sha256": _sha256_text(token), "lease_expires_at": expiry},
                    occurred_at=now,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        result = self.status(role="executor", include_private=True)
        result["claim_token"] = token
        self._write_projections(result)
        return result

    def submit_report(
        self,
        report_file: Path,
        *,
        claim_token: str,
        title: str,
        require_commit: bool = True,
    ) -> dict[str, Any]:
        content = _read_nonempty(report_file, "report file")
        if not claim_token:
            raise AgentLoopError("claim_token must not be empty")
        snapshot = git_snapshot(self.repo_root)
        if not snapshot.clean:
            raise AgentLoopError("cannot submit a report from a dirty git workspace")
        if not snapshot.pushed:
            raise AgentLoopError("cannot submit a report until HEAD equals its upstream")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                metadata = self._validate_metadata(connection)
                if metadata["phase"] != "EXECUTING":
                    raise AgentLoopError(
                        f"submit_report requires EXECUTING, got {metadata['phase']}"
                    )
                if not secrets.compare_digest(metadata["claim_token"], claim_token):
                    raise AgentLoopError("claim token mismatch")
                expected = metadata["expected_head"]
                if require_commit and snapshot.head == expected:
                    raise AgentLoopError(
                        "task produced no commit; use --allow-no-commit explicitly"
                    )
                ancestry = _git(
                    self.repo_root,
                    "merge-base",
                    "--is-ancestor",
                    expected,
                    snapshot.head,
                    check=False,
                )
                if ancestry.returncode != 0:
                    raise AgentLoopError(
                        f"final HEAD {snapshot.head} does not descend from expected_head {expected}"
                    )
                generation = int(metadata["generation"])
                digest = self._insert_artifact(
                    connection,
                    kind="report",
                    generation=generation,
                    title=title,
                    content=content,
                    git_head=snapshot.head,
                )
                commit_count = int(
                    _git(self.repo_root, "rev-list", "--count", f"{expected}..{snapshot.head}")
                    .stdout.strip()
                    or "0"
                )
                now = _utc_now()
                self._set_metadata(
                    connection,
                    phase="REVIEW_READY",
                    claim_token="",
                    claim_expires_at="",
                    updated_at=now,
                )
                event_sha256 = self._append_event(
                    connection,
                    actor=metadata["claim_agent"],
                    action="report_submitted",
                    generation=generation,
                    payload={
                        "report_sha256": digest,
                        "starting_head": expected,
                        "final_head": snapshot.head,
                        "commit_count": commit_count,
                        "upstream": snapshot.upstream,
                    },
                    occurred_at=now,
                )
                self._queue_review_notification(
                    connection,
                    event_sha256=event_sha256,
                    generation=generation,
                    event_action="report_submitted",
                    occurred_at=now,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.status(role="reviewer")

    def block_execution(
        self,
        blocker_file: Path,
        *,
        claim_token: str,
        title: str,
    ) -> dict[str, Any]:
        """Publish a blocker without pretending the workspace is clean or pushed.

        A blocked executor may have partial local work.  The protocol records that
        fact for the reviewer instead of hiding the blocker or waiting for lease
        expiry.  Resuming still requires the reviewer to restore a clean, pushed
        repository state before publishing a replacement task.
        """

        content = _read_nonempty(blocker_file, "blocker file")
        if not claim_token:
            raise AgentLoopError("claim_token must not be empty")
        snapshot = git_snapshot(self.repo_root)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                metadata = self._validate_metadata(connection)
                if metadata["phase"] != "EXECUTING":
                    raise AgentLoopError(
                        f"block_execution requires EXECUTING, got {metadata['phase']}"
                    )
                if not secrets.compare_digest(metadata["claim_token"], claim_token):
                    raise AgentLoopError("claim token mismatch")
                generation = int(metadata["generation"])
                digest = self._insert_artifact(
                    connection,
                    kind="report",
                    generation=generation,
                    title=title,
                    content=content,
                    git_head=snapshot.head,
                )
                now = _utc_now()
                self._set_metadata(
                    connection,
                    phase="BLOCKED",
                    claim_token="",
                    claim_expires_at="",
                    updated_at=now,
                )
                event_sha256 = self._append_event(
                    connection,
                    actor=metadata["claim_agent"],
                    action="execution_blocked",
                    generation=generation,
                    payload={
                        "blocker_sha256": digest,
                        "head": snapshot.head,
                        "clean": snapshot.clean,
                        "pushed": snapshot.pushed,
                    },
                    occurred_at=now,
                )
                self._queue_review_notification(
                    connection,
                    event_sha256=event_sha256,
                    generation=generation,
                    event_action="execution_blocked",
                    occurred_at=now,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.status(role="reviewer")

    def submit_review(
        self,
        review_file: Path,
        *,
        decision: Literal["advance", "rework", "blocked", "complete"],
        title: str,
        next_task_file: Path | None = None,
        next_task_title: str | None = None,
        actor: str = "codex-reviewer",
    ) -> dict[str, Any]:
        if decision not in DECISIONS:
            raise AgentLoopError(f"invalid review decision: {decision}")
        review_content = _read_nonempty(review_file, "review file")
        needs_task = decision in {"advance", "rework"}
        if needs_task != (next_task_file is not None):
            raise AgentLoopError(
                "advance/rework require --next-task-file; blocked/complete forbid it"
            )
        next_content = (
            _read_nonempty(next_task_file, "next task file") if next_task_file is not None else None
        )
        if needs_task and not (next_task_title or "").strip():
            raise AgentLoopError("advance/rework require --next-task-title")
        snapshot = git_snapshot(self.repo_root)
        if not snapshot.clean:
            raise AgentLoopError("review requires a clean git workspace")
        if not snapshot.pushed:
            raise AgentLoopError("review requires HEAD to equal its upstream")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                metadata = self._validate_metadata(connection)
                if metadata["phase"] != "REVIEW_READY":
                    raise AgentLoopError(
                        f"submit_review requires REVIEW_READY, got {metadata['phase']}"
                    )
                generation = int(metadata["generation"])
                report = connection.execute(
                    "SELECT * FROM artifacts WHERE kind = 'report' AND generation = ?",
                    (generation,),
                ).fetchone()
                if report is None:
                    raise AgentLoopError("current report artifact is missing")
                if snapshot.head != report["git_head"]:
                    raise AgentLoopError(
                        "reviewed HEAD drifted: "
                        f"report={report['git_head']}, current={snapshot.head}"
                    )
                review_digest = self._insert_artifact(
                    connection,
                    kind="review",
                    generation=generation,
                    title=title,
                    content=review_content,
                    git_head=snapshot.head,
                )
                now = _utc_now()
                payload: dict[str, Any] = {
                    "decision": decision,
                    "review_sha256": review_digest,
                    "reviewed_head": snapshot.head,
                }
                if needs_task:
                    next_generation = generation + 1
                    task_digest = self._insert_artifact(
                        connection,
                        kind="task",
                        generation=next_generation,
                        title=next_task_title or "",
                        content=next_content or "",
                        git_head=snapshot.head,
                    )
                    payload.update(
                        {
                            "next_generation": next_generation,
                            "next_task_sha256": task_digest,
                            "next_task_title": next_task_title,
                        }
                    )
                    self._set_metadata(
                        connection,
                        phase="TASK_READY",
                        generation=next_generation,
                        expected_head=snapshot.head,
                        claim_agent="",
                        claim_token="",
                        claim_expires_at="",
                        updated_at=now,
                    )
                else:
                    terminal_phase = "COMPLETE" if decision == "complete" else "BLOCKED"
                    self._set_metadata(
                        connection,
                        phase=terminal_phase,
                        claim_agent="",
                        claim_token="",
                        claim_expires_at="",
                        updated_at=now,
                    )
                self._append_event(
                    connection,
                    actor=actor,
                    action="review_submitted",
                    generation=generation,
                    payload=payload,
                    occurred_at=now,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.status(role="executor" if needs_task else "reviewer")

    def mark_expired_claim_blocked(
        self,
        *,
        actor: str = "codex-reviewer",
        reason: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise AgentLoopError("reason must not be empty")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                metadata = self._validate_metadata(connection)
                if metadata["phase"] != "EXECUTING":
                    raise AgentLoopError(
                        f"expire_claim requires EXECUTING, got {metadata['phase']}"
                    )
                expiry_text = metadata["claim_expires_at"]
                if not expiry_text:
                    raise AgentLoopError("executing state has no claim expiry")
                expiry = datetime.fromisoformat(expiry_text.replace("Z", "+00:00"))
                if datetime.now(UTC) < expiry:
                    raise AgentLoopError(f"claim is not expired; lease ends at {expiry_text}")
                generation = int(metadata["generation"])
                now = _utc_now()
                self._set_metadata(
                    connection,
                    phase="BLOCKED",
                    claim_token="",
                    claim_expires_at="",
                    updated_at=now,
                )
                self._append_event(
                    connection,
                    actor=actor,
                    action="expired_claim_blocked",
                    generation=generation,
                    payload={"reason": reason.strip(), "previous_agent": metadata["claim_agent"]},
                    occurred_at=now,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.status(role="reviewer")

    def requeue_abandoned_claim(
        self,
        *,
        reason: str,
        expected_event_sha256: str,
        expected_claim_agent: str,
        actor: str = "human-recovery",
    ) -> dict[str, Any]:
        """Abandon a known dead executor and immutably requeue its task.

        This is intentionally stricter than lease expiry.  A human must identify
        the exact last event and claimed agent, while Git must be clean, pushed,
        and descended from the abandoned task's expected HEAD.  The old
        generation is never rewritten; the same task content is copied into a
        new generation bound to the current HEAD.
        """

        if not reason.strip():
            raise AgentLoopError("reason must not be empty")
        if len(expected_event_sha256) != 64:
            raise AgentLoopError("expected_event_sha256 must be a SHA-256 hex digest")
        if not expected_claim_agent.strip():
            raise AgentLoopError("expected_claim_agent must not be empty")
        snapshot = git_snapshot(self.repo_root)
        if not snapshot.clean:
            raise AgentLoopError("claim recovery requires a clean git workspace")
        if not snapshot.pushed:
            raise AgentLoopError("claim recovery requires HEAD to equal its upstream")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                metadata = self._validate_metadata(connection)
                if metadata["phase"] != "EXECUTING":
                    raise AgentLoopError(
                        f"requeue_abandoned_claim requires EXECUTING, got {metadata['phase']}"
                    )
                if metadata["claim_agent"] != expected_claim_agent:
                    raise AgentLoopError(
                        "claimed agent mismatch: "
                        f"expected {expected_claim_agent!r}, got {metadata['claim_agent']!r}"
                    )
                last_event = connection.execute(
                    "SELECT event_sha256 FROM events ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                actual_event_sha256 = last_event["event_sha256"] if last_event else None
                if actual_event_sha256 != expected_event_sha256:
                    raise AgentLoopError(
                        "last event mismatch: "
                        f"expected {expected_event_sha256}, got {actual_event_sha256}"
                    )
                old_generation = int(metadata["generation"])
                old_task = self._artifact(connection, "task", old_generation)
                if old_task is None:
                    raise AgentLoopError(
                        f"missing abandoned task artifact for generation {old_generation}"
                    )
                ancestry = _git(
                    self.repo_root,
                    "merge-base",
                    "--is-ancestor",
                    metadata["expected_head"],
                    snapshot.head,
                    check=False,
                )
                if ancestry.returncode != 0:
                    raise AgentLoopError(
                        f"recovery HEAD {snapshot.head} does not descend from abandoned "
                        f"expected_head {metadata['expected_head']}"
                    )
                new_generation = old_generation + 1
                task_digest = self._insert_artifact(
                    connection,
                    kind="task",
                    generation=new_generation,
                    title=old_task["title"],
                    content=old_task["content"],
                    git_head=snapshot.head,
                )
                now = _utc_now()
                abandoned_token_sha256 = _sha256_text(metadata["claim_token"])
                self._set_metadata(
                    connection,
                    phase="TASK_READY",
                    generation=new_generation,
                    expected_head=snapshot.head,
                    claim_agent="",
                    claim_token="",
                    claim_expires_at="",
                    updated_at=now,
                )
                self._append_event(
                    connection,
                    actor=actor,
                    action="abandoned_claim_requeued",
                    generation=new_generation,
                    payload={
                        "reason": reason.strip(),
                        "abandoned_generation": old_generation,
                        "abandoned_claim_agent": expected_claim_agent,
                        "abandoned_claim_token_sha256": abandoned_token_sha256,
                        "abandoned_event_sha256": expected_event_sha256,
                        "abandoned_expected_head": metadata["expected_head"],
                        "new_generation": new_generation,
                        "new_expected_head": snapshot.head,
                        "task_sha256": task_digest,
                    },
                    occurred_at=now,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.status(role="executor")

    def pending_review_notification(self) -> dict[str, Any] | None:
        """Return the newest actionable reviewer notification, if any.

        Notification delivery is deliberately separate from the state transition:
        a committed report remains authoritative even when launching Codex fails.
        """

        with self._connect() as connection:
            metadata = self._validate_metadata(connection)
            if metadata["phase"] not in {"REVIEW_READY", "BLOCKED"}:
                return None
            row = connection.execute(
                """
                SELECT * FROM review_notifications
                WHERE generation = ?
                ORDER BY created_at DESC, event_sha256 DESC
                LIMIT 1
                """,
                (int(metadata["generation"]),),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            if result["state"] not in NOTIFICATION_STATES:
                raise AgentLoopError(
                    f"invalid review notification state: {result['state']!r}"
                )
            result["delivery_token"] = "" if not result["delivery_token"] else "<redacted>"
            return result

    @staticmethod
    def _supersede_live_delivery_attempt(
        connection: sqlite3.Connection,
        *,
        attempt: sqlite3.Row,
        reason: str,
        occurred_at: str,
    ) -> None:
        """Atomically revoke one live attempt and its raw notification capability."""

        if attempt["state"] != "live":
            raise AgentLoopError("delivery attempt is no longer live")
        notification = connection.execute(
            "SELECT * FROM review_notifications WHERE event_sha256 = ?",
            (attempt["event_sha256"],),
        ).fetchone()
        if notification is None:
            raise AgentLoopError("live delivery attempt has no notification")
        token = notification["delivery_token"]
        if (
            notification["state"] != "launching"
            or not token
            or not notification["config_fingerprint"]
            or not notification["thread_id"]
            or notification["config_fingerprint"] != attempt["config_fingerprint"]
            or notification["thread_id"] != attempt["thread_id"]
            or _sha256_text(token) != attempt["delivery_token_sha256"]
        ):
            raise AgentLoopError(
                "live delivery attempt cannot be superseded because its notification "
                "binding is incomplete or mismatched"
            )
        detail = reason.strip()[:4000] or "delivery attempt superseded"
        changed_attempt = connection.execute(
            """
            UPDATE delivery_attempts
            SET state = 'superseded', classification = 'transient',
                last_error = ?, updated_at = ?
            WHERE attempt_id = ? AND state = 'live'
              AND delivery_token_sha256 = ?
            """,
            (
                detail,
                occurred_at,
                attempt["attempt_id"],
                attempt["delivery_token_sha256"],
            ),
        ).rowcount
        changed_notification = connection.execute(
            """
            UPDATE review_notifications
            SET state = 'failed', delivery_token = '', process_id = NULL,
                last_error = ?, retry_class = 'transient', next_retry_at = '',
                blocked_fingerprint = '', updated_at = ?
            WHERE event_sha256 = ? AND state = 'launching' AND delivery_token = ?
            """,
            (detail, occurred_at, attempt["event_sha256"], token),
        ).rowcount
        if changed_attempt != 1 or changed_notification != 1:
            raise AgentLoopError("delivery supersession lost its atomic ownership CAS")

    def claim_review_notification(
        self,
        *,
        event_sha256: str | None = None,
        stale_after_seconds: int = 25_200,
        config_fingerprint: str = "",
        thread_id: str = "",
    ) -> dict[str, Any] | None:
        """Atomically claim one actionable notification for bridge delivery.

        Returns ``None`` when the event must not be attempted now: it is
        delivered, a fresh live attempt exists, automatic retry is blocked for
        the supplied configuration fingerprint, the exponential backoff from a
        transient failure is not due yet, or another event's fresh live
        attempt holds the same dedicated reviewer target
        (``config_fingerprint`` + ``thread_id``).  A live attempt older than
        ``stale_after_seconds`` is superseded so a crashed worker cannot wedge
        the target forever.
        """

        if stale_after_seconds < 60:
            raise AgentLoopError("stale_after_seconds must be at least 60")
        if not config_fingerprint or not thread_id:
            raise AgentLoopError(
                "delivery claims require a non-empty configuration fingerprint and thread id"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                metadata = self._validate_metadata(connection)
                if metadata["phase"] not in {"REVIEW_READY", "BLOCKED"}:
                    connection.commit()
                    return None
                parameters: list[Any] = [int(metadata["generation"])]
                selector = "generation = ?"
                if event_sha256 is not None:
                    selector += " AND event_sha256 = ?"
                    parameters.append(event_sha256)
                row = connection.execute(
                    f"""
                    SELECT * FROM review_notifications
                    WHERE {selector}
                    ORDER BY created_at DESC, event_sha256 DESC
                    LIMIT 1
                    """,
                    parameters,
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                state = row["state"]
                if state not in NOTIFICATION_STATES:
                    raise AgentLoopError(f"invalid review notification state: {state!r}")
                if state == "delivered":
                    connection.commit()
                    return None
                now_dt = datetime.now(UTC)
                if state == "launching":
                    updated = datetime.fromisoformat(
                        row["updated_at"].replace("Z", "+00:00")
                    )
                    if now_dt - updated < timedelta(seconds=stale_after_seconds):
                        connection.commit()
                        return None
                else:
                    if row["blocked_fingerprint"] and (
                        row["blocked_fingerprint"] == config_fingerprint
                    ):
                        connection.commit()
                        return None
                    if row["next_retry_at"]:
                        due = datetime.fromisoformat(
                            row["next_retry_at"].replace("Z", "+00:00")
                        )
                        if now_dt < due:
                            connection.commit()
                            return None
                token = secrets.token_urlsafe(32)
                attempt_id = secrets.token_hex(16)
                now = _utc_now()
                attempt_number = int(row["attempt_count"]) + 1
                if state == "launching":
                    # Bounded stale recovery for the same event: the crashed
                    # worker's live attempt is superseded before the takeover.
                    own_attempts = connection.execute(
                        "SELECT * FROM delivery_attempts "
                        "WHERE event_sha256 = ? AND state = 'live'",
                        (row["event_sha256"],),
                    ).fetchall()
                    if len(own_attempts) != 1:
                        raise AgentLoopError(
                            "stale launching notification has no unique live attempt"
                        )
                    self._supersede_live_delivery_attempt(
                        connection,
                        attempt=own_attempts[0],
                        reason="bounded stale takeover by a newer attempt for the same event",
                        occurred_at=now,
                    )
                live = connection.execute(
                    """
                    SELECT *
                    FROM delivery_attempts
                    WHERE state = 'live' AND config_fingerprint = ?
                      AND thread_id = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (config_fingerprint, thread_id),
                ).fetchone()
                if live is not None:
                    # Another event already holds this dedicated reviewer
                    # target.  A fresh live attempt keeps the target busy; a
                    # crash cannot wedge it forever because an attempt older
                    # than the bounded stale interval is superseded here.
                    live_updated = datetime.fromisoformat(
                        live["updated_at"].replace("Z", "+00:00")
                    )
                    if now_dt - live_updated < timedelta(seconds=stale_after_seconds):
                        connection.commit()
                        return None
                    self._supersede_live_delivery_attempt(
                        connection,
                        attempt=live,
                        reason=(
                            "bounded stale cross-event takeover by event "
                            f"{row['event_sha256']}"
                        ),
                        occurred_at=now,
                    )
                connection.execute(
                    """
                    UPDATE review_notifications
                    SET state = 'launching', attempt_count = ?, delivery_token = ?,
                        process_id = NULL, last_error = '', config_fingerprint = ?,
                        thread_id = ?, updated_at = ?
                    WHERE event_sha256 = ?
                    """,
                    (
                        attempt_number,
                        token,
                        config_fingerprint,
                        thread_id,
                        now,
                        row["event_sha256"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO delivery_attempts(
                        attempt_id, event_sha256, generation, event_action,
                        config_fingerprint, thread_id, attempt_number,
                        delivery_token_sha256, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'live', ?, ?)
                    """,
                    (
                        attempt_id,
                        row["event_sha256"],
                        row["generation"],
                        row["event_action"],
                        config_fingerprint,
                        thread_id,
                        attempt_number,
                        _sha256_text(token),
                        now,
                        now,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {
            "event_sha256": row["event_sha256"],
            "generation": row["generation"],
            "event_action": row["event_action"],
            "delivery_token": token,
            "attempt_count": attempt_number,
            "attempt_id": attempt_id,
            "config_fingerprint": config_fingerprint,
            "thread_id": thread_id,
        }

    def live_target_attempt(
        self, *, config_fingerprint: str, thread_id: str
    ) -> dict[str, Any] | None:
        """Return the live delivery attempt holding this reviewer target, if any.

        At most one live attempt exists per ``(config_fingerprint, thread_id)``
        across all events and generations; this read-only view lets delivery
        callers report a deterministic busy result without launching anything.
        """

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM delivery_attempts
                WHERE state = 'live' AND config_fingerprint = ? AND thread_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (config_fingerprint, thread_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_review_notification_process(
        self,
        *,
        event_sha256: str,
        delivery_token: str,
        process_id: int,
    ) -> None:
        if process_id <= 0:
            raise AgentLoopError("process_id must be positive")
        with self._connect() as connection:
            bound = connection.execute(
                """
                SELECT a.attempt_id
                FROM review_notifications AS n
                JOIN delivery_attempts AS a
                  ON a.event_sha256 = n.event_sha256 AND a.state = 'live'
                WHERE n.event_sha256 = ? AND n.state = 'launching'
                  AND n.delivery_token = ? AND n.delivery_token != ''
                  AND n.config_fingerprint = a.config_fingerprint
                  AND n.thread_id = a.thread_id
                  AND a.delivery_token_sha256 = ?
                """,
                (event_sha256, delivery_token, _sha256_text(delivery_token)),
            ).fetchall()
            if len(bound) != 1:
                raise AgentLoopError("notification delivery claim is stale")
            changed = connection.execute(
                """
                UPDATE review_notifications
                SET process_id = ?, updated_at = ?
                WHERE event_sha256 = ? AND state = 'launching' AND delivery_token = ?
                """,
                (process_id, _utc_now(), event_sha256, delivery_token),
            ).rowcount
            if changed != 1:
                raise AgentLoopError("notification delivery claim is stale")

    def claimed_review_notification(
        self, *, event_sha256: str, delivery_token: str
    ) -> dict[str, Any]:
        """Validate and return the exact currently launching delivery claim."""

        if not delivery_token:
            raise AgentLoopError("delivery_token must not be empty")
        with self._connect() as connection:
            metadata = self._validate_metadata(connection)
            rows = connection.execute(
                """
                SELECT n.* FROM review_notifications AS n
                JOIN delivery_attempts AS a
                  ON a.event_sha256 = n.event_sha256 AND a.state = 'live'
                WHERE n.event_sha256 = ? AND n.state = 'launching'
                  AND n.delivery_token = ? AND n.delivery_token != ''
                  AND n.config_fingerprint = a.config_fingerprint
                  AND n.thread_id = a.thread_id
                  AND a.delivery_token_sha256 = ?
                """,
                (event_sha256, delivery_token, _sha256_text(delivery_token)),
            ).fetchall()
            if len(rows) != 1:
                raise AgentLoopError("notification delivery claim is stale")
            row = rows[0]
            if int(metadata["generation"]) != row["generation"] or metadata["phase"] not in {
                "REVIEW_READY",
                "BLOCKED",
            }:
                raise AgentLoopError("notification no longer matches current review state")
            result = dict(row)
            result["delivery_token"] = delivery_token
            return result

    def finish_review_notification(
        self,
        *,
        event_sha256: str,
        delivery_token: str,
        delivered: bool,
        error: str = "",
        classification: str = "transient",
        turn_id: str = "",
        backoff_base_seconds: int = RETRY_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: int = RETRY_BACKOFF_MAX_SECONDS,
    ) -> dict[str, Any]:
        """Finish exactly the delivery attempt identified by its private token.

        Transient failures schedule a bounded exponential backoff.  A
        configuration failure additionally blocks automatic retries for the
        exact configuration fingerprint that failed until a new, successfully
        probed configuration is written or recovery is requested explicitly.
        """

        if not delivery_token:
            raise AgentLoopError("delivery_token must not be empty")
        if classification not in RETRY_CLASSES or not classification:
            raise AgentLoopError(f"invalid delivery failure classification: {classification!r}")
        if backoff_base_seconds < 0 or backoff_max_seconds < backoff_base_seconds:
            raise AgentLoopError("invalid backoff bounds")
        detail = "" if delivered else (error.strip() or "unspecified delivery failure")
        token_sha256 = _sha256_text(delivery_token)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM review_notifications
                    WHERE event_sha256 = ? AND state = 'launching'
                      AND delivery_token = ?
                    """,
                    (event_sha256, delivery_token),
                ).fetchone()
                if row is None:
                    raise AgentLoopError("notification delivery claim is stale")
                if (
                    not row["delivery_token"]
                    or not row["config_fingerprint"]
                    or not row["thread_id"]
                ):
                    raise AgentLoopError(
                        "notification delivery claim has incomplete mandatory bindings"
                    )
                attempts = connection.execute(
                    """
                    SELECT * FROM delivery_attempts
                    WHERE event_sha256 = ? AND state = 'live'
                      AND config_fingerprint = ? AND thread_id = ?
                      AND delivery_token_sha256 = ?
                    """,
                    (
                        event_sha256,
                        row["config_fingerprint"],
                        row["thread_id"],
                        token_sha256,
                    ),
                ).fetchall()
                if len(attempts) != 1:
                    raise AgentLoopError(
                        "notification delivery claim is stale or its live attempt "
                        "token binding mismatches"
                    )
                attempt = attempts[0]
                if delivered:
                    if classification != "transient":
                        raise AgentLoopError(
                            "delivered attempts must not carry a failure classification"
                        )
                    if turn_id == "":
                        raise AgentLoopError("delivered attempts must record their turn id")
                now = _utc_now()
                if delivered:
                    attempt_state = "delivered"
                    stored_class = ""
                    changed_notification = connection.execute(
                        """
                        UPDATE review_notifications
                        SET state = 'delivered', delivery_token = '', process_id = NULL,
                            last_error = '', retry_class = '', next_retry_at = '',
                            updated_at = ?
                        WHERE event_sha256 = ? AND state = 'launching'
                          AND delivery_token = ?
                        """,
                        (now, event_sha256, delivery_token),
                    ).rowcount
                elif classification == "configuration":
                    attempt_state = "failed_configuration"
                    stored_class = "configuration"
                    changed_notification = connection.execute(
                        """
                        UPDATE review_notifications
                        SET state = 'failed', delivery_token = '', process_id = NULL,
                            last_error = ?, retry_class = 'configuration',
                            next_retry_at = '', blocked_fingerprint = config_fingerprint,
                            updated_at = ?
                        WHERE event_sha256 = ? AND state = 'launching'
                          AND delivery_token = ?
                        """,
                        (detail[:4000], now, event_sha256, delivery_token),
                    ).rowcount
                else:
                    backoff_seconds = min(
                        backoff_base_seconds
                        * (2 ** max(0, int(row["attempt_count"]) - 1)),
                        backoff_max_seconds,
                    )
                    next_retry_at = (
                        datetime.now(UTC) + timedelta(seconds=backoff_seconds)
                    ).isoformat().replace("+00:00", "Z")
                    attempt_state = "failed_transient"
                    stored_class = "transient"
                    changed_notification = connection.execute(
                        """
                        UPDATE review_notifications
                        SET state = 'failed', delivery_token = '', process_id = NULL,
                            last_error = ?, retry_class = 'transient',
                            next_retry_at = ?, updated_at = ?
                        WHERE event_sha256 = ? AND state = 'launching'
                          AND delivery_token = ?
                        """,
                        (
                            detail[:4000],
                            next_retry_at,
                            now,
                            event_sha256,
                            delivery_token,
                        ),
                    ).rowcount
                changed_attempt = connection.execute(
                    """
                    UPDATE delivery_attempts
                    SET state = ?, turn_id = ?, classification = ?, last_error = ?,
                        updated_at = ?
                    WHERE attempt_id = ? AND state = 'live'
                      AND delivery_token_sha256 = ?
                    """,
                    (
                        attempt_state,
                        turn_id,
                        stored_class,
                        detail[:4000],
                        now,
                        attempt["attempt_id"],
                        token_sha256,
                    ),
                ).rowcount
                if changed_notification != 1 or changed_attempt != 1:
                    raise AgentLoopError(
                        "notification completion lost its exact live-attempt ownership CAS"
                    )
                row = connection.execute(
                    "SELECT * FROM review_notifications WHERE event_sha256 = ?",
                    (event_sha256,),
                ).fetchone()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        result = dict(row)
        result["delivery_token"] = "" if not result["delivery_token"] else "<redacted>"
        return result

    def recover_bridge_delivery(
        self,
        *,
        reason: str,
        config_fingerprint: str,
        thread_id: str,
        actor: str = "human-recovery",
    ) -> dict[str, Any]:
        """Explicitly re-queue a blocked or backed-off reviewer notification.

        This is an operator decision: it clears the configuration block and any
        pending backoff for the current notification, never touches delivered
        events, and never discards recorded failure history.
        """

        if not reason.strip():
            raise AgentLoopError("reason must not be empty")
        if not actor.strip():
            raise AgentLoopError("recovery actor must not be empty")
        if not config_fingerprint or not thread_id:
            raise AgentLoopError(
                "recovery requires the active verified configuration fingerprint and thread id"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                metadata = self._validate_metadata(connection)
                if metadata["phase"] not in {"REVIEW_READY", "BLOCKED"}:
                    raise AgentLoopError(
                        f"bridge recovery requires REVIEW_READY or BLOCKED, "
                        f"got {metadata['phase']}"
                    )
                row = connection.execute(
                    """
                    SELECT * FROM review_notifications
                    WHERE generation = ?
                    ORDER BY created_at DESC, event_sha256 DESC
                    LIMIT 1
                    """,
                    (int(metadata["generation"]),),
                ).fetchone()
                if row is None:
                    raise AgentLoopError("no reviewer notification to recover")
                if row["state"] == "delivered":
                    raise AgentLoopError("reviewer notification is already delivered")
                now = _utc_now()
                holders = connection.execute(
                    """
                    SELECT * FROM delivery_attempts
                    WHERE state = 'live' AND config_fingerprint = ? AND thread_id = ?
                    """,
                    (config_fingerprint, thread_id),
                ).fetchall()
                if len(holders) > 1:
                    raise AgentLoopError(
                        "recovery target has ambiguous live delivery ownership"
                    )
                revoked_event: str | None = None
                revoked_attempt: str | None = None
                if holders:
                    holder = holders[0]
                    revoked_event = str(holder["event_sha256"])
                    revoked_attempt = str(holder["attempt_id"])
                    self._supersede_live_delivery_attempt(
                        connection,
                        attempt=holder,
                        reason=(
                            "explicit operator recovery for current event "
                            f"{row['event_sha256']}: {reason.strip()}"
                        ),
                        occurred_at=now,
                    )
                elif row["state"] == "launching":
                    raise AgentLoopError(
                        "launching current notification is not owned by the requested target"
                    )
                changed = connection.execute(
                    """
                    UPDATE review_notifications
                    SET state = 'queued', delivery_token = '', process_id = NULL,
                        next_retry_at = '', blocked_fingerprint = '',
                        config_fingerprint = ?, thread_id = ?, updated_at = ?
                    WHERE event_sha256 = ?
                    """,
                    (config_fingerprint, thread_id, now, row["event_sha256"]),
                ).rowcount
                if changed != 1:
                    raise AgentLoopError("recovery lost the current notification")
                connection.execute(
                    """
                    INSERT INTO delivery_recoveries(
                        recovery_id, current_event_sha256, revoked_event_sha256,
                        revoked_attempt_id, config_fingerprint, thread_id,
                        actor, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        secrets.token_hex(16),
                        row["event_sha256"],
                        revoked_event,
                        revoked_attempt,
                        config_fingerprint,
                        thread_id,
                        actor.strip(),
                        reason.strip(),
                        now,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        pending = self.pending_review_notification()
        if pending is None:  # pragma: no cover - row was just updated
            raise AgentLoopError("recovered notification disappeared")
        return pending

    def delivery_recoveries(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Return recent immutable operator recovery audit records."""

        if limit < 1 or limit > 100:
            raise AgentLoopError("delivery recovery limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM delivery_recoveries "
                "ORDER BY created_at DESC, recovery_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _verify_event_chain(self, connection: sqlite3.Connection) -> tuple[int, str | None]:
        previous: str | None = None
        count = 0
        for row in connection.execute("SELECT * FROM events ORDER BY sequence"):
            if row["previous_sha256"] != previous:
                raise AgentLoopError(f"event chain break at sequence {row['sequence']}")
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError as exc:
                raise AgentLoopError(
                    f"invalid event payload JSON at sequence {row['sequence']}"
                ) from exc
            digest_input = {
                "occurred_at": row["occurred_at"],
                "actor": row["actor"],
                "action": row["action"],
                "generation": row["generation"],
                "payload": payload,
                "previous_sha256": previous,
            }
            expected = _sha256_text(_canonical_json(digest_input))
            if row["event_sha256"] != expected:
                raise AgentLoopError(f"event hash mismatch at sequence {row['sequence']}")
            previous = expected
            count += 1
        if count == 0:
            raise AgentLoopError("event chain is empty")
        return count, previous

    def _artifact(
        self,
        connection: sqlite3.Connection,
        kind: str,
        generation: int,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM artifacts WHERE kind = ? AND generation = ?", (kind, generation)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result["sha256"] != _sha256_text(result["content"]):
            raise AgentLoopError(f"{kind} artifact hash mismatch for generation {generation}")
        result["path"] = str(self.mailbox / f"{kind}s" / f"{generation:06d}.md")
        return result

    def status(
        self,
        *,
        role: Literal["executor", "reviewer"] = "reviewer",
        include_private: bool = False,
    ) -> dict[str, Any]:
        if role not in {"executor", "reviewer"}:
            raise AgentLoopError(f"invalid role: {role}")
        with self._connect() as connection:
            metadata = self._validate_metadata(connection)
            event_count, last_event = self._verify_event_chain(connection)
            generation = int(metadata["generation"])
            phase = metadata["phase"]
            required_artifacts: list[tuple[str, int]] = []
            artifacts: dict[str, Any] = {}
            if generation > 0:
                required_artifacts.append(("task", generation))
            if phase in {"REVIEW_READY", "COMPLETE"}:
                required_artifacts.append(("report", generation))
            if phase == "BLOCKED":
                blocked_report = self._artifact(connection, "report", generation)
                if blocked_report is not None:
                    artifacts["report"] = blocked_report
            for kind, artifact_generation in required_artifacts:
                artifact = self._artifact(connection, kind, artifact_generation)
                if artifact is None:
                    raise AgentLoopError(
                        f"missing {kind} artifact for generation {artifact_generation}"
                    )
                artifacts[kind] = artifact
            action = "noop"
            if role == "executor" and phase == "TASK_READY":
                action = "claim_task"
            elif role == "reviewer" and phase == "REVIEW_READY":
                action = "review_report"
            elif role == "reviewer" and phase == "EXECUTING":
                expiry_text = metadata["claim_expires_at"]
                if expiry_text:
                    expiry = datetime.fromisoformat(expiry_text.replace("Z", "+00:00"))
                    if datetime.now(UTC) >= expiry:
                        action = "block_expired_claim"
            elif role == "reviewer" and phase == "BLOCKED":
                action = "inspect_blocker"
            elif role == "reviewer" and phase == "COMPLETE":
                action = "loop_complete"
            result: dict[str, Any] = {
                "protocol_version": metadata["protocol_version"],
                "project_id": metadata["project_id"],
                "phase": phase,
                "generation": generation,
                "expected_head": metadata["expected_head"] or None,
                "claim": None,
                "artifacts": artifacts,
                "event_count": event_count,
                "last_event_sha256": last_event,
                "updated_at": metadata["updated_at"],
                "role": role,
                "action": action,
            }
            notification = connection.execute(
                """
                SELECT event_sha256, generation, event_action, state, attempt_count,
                       config_fingerprint, retry_class, next_retry_at,
                       blocked_fingerprint, thread_id, process_id, last_error, created_at,
                       updated_at
                FROM review_notifications
                WHERE generation = ?
                ORDER BY created_at DESC, event_sha256 DESC
                LIMIT 1
                """,
                (generation,),
            ).fetchone()
            result["review_notification"] = dict(notification) if notification else None
            if phase == "EXECUTING":
                result["claim"] = {
                    "agent": metadata["claim_agent"],
                    "expires_at": metadata["claim_expires_at"],
                }
                if include_private:
                    result["claim"]["token"] = metadata["claim_token"]
            self._write_projections(result)
            return result

    def doctor(self) -> dict[str, Any]:
        status = self.status(role="reviewer")
        with self._connect() as connection:
            artifact_count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            for row in connection.execute("SELECT * FROM artifacts"):
                if row["kind"] not in ARTIFACT_KINDS:
                    raise AgentLoopError(f"unknown artifact kind in database: {row['kind']}")
                if row["sha256"] != _sha256_text(row["content"]):
                    raise AgentLoopError(
                        f"artifact hash mismatch: {row['kind']} generation {row['generation']}"
                    )
            notification_count = 0
            for row in connection.execute("SELECT * FROM review_notifications"):
                notification_count += 1
                if row["state"] not in NOTIFICATION_STATES:
                    raise AgentLoopError(
                        f"invalid review notification state: {row['state']!r}"
                    )
                if row["retry_class"] not in RETRY_CLASSES:
                    raise AgentLoopError(
                        f"invalid review notification retry class: {row['retry_class']!r}"
                    )
                event = connection.execute(
                    "SELECT action, generation FROM events WHERE event_sha256 = ?",
                    (row["event_sha256"],),
                ).fetchone()
                if event is None:
                    raise AgentLoopError("review notification references a missing event")
                if (
                    event["action"] != row["event_action"]
                    or event["generation"] != row["generation"]
                ):
                    raise AgentLoopError("review notification event binding mismatch")
                live_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM delivery_attempts
                    WHERE event_sha256 = ? AND state = 'live'
                    """,
                    (row["event_sha256"],),
                ).fetchone()[0]
                if row["state"] == "launching":
                    if live_count != 1:
                        raise AgentLoopError(
                            "launching notification has no unique live delivery attempt"
                        )
                    if (
                        not row["delivery_token"]
                        or not row["config_fingerprint"]
                        or not row["thread_id"]
                    ):
                        raise AgentLoopError(
                            "launching notification has empty mandatory delivery binding"
                        )
                    attempt = connection.execute(
                        "SELECT * FROM delivery_attempts "
                        "WHERE event_sha256 = ? AND state = 'live'",
                        (row["event_sha256"],),
                    ).fetchone()
                    if attempt is None:
                        raise AgentLoopError(
                            "launching notification has no live delivery attempt"
                        )
                    if attempt["delivery_token_sha256"] != _sha256_text(
                        row["delivery_token"]
                    ):
                        raise AgentLoopError(
                            "launching notification binding does not match its live "
                            "delivery attempt"
                        )
                    if (
                        attempt["config_fingerprint"] != row["config_fingerprint"]
                        or attempt["thread_id"] != row["thread_id"]
                        or attempt["generation"] != row["generation"]
                        or attempt["event_action"] != row["event_action"]
                    ):
                        raise AgentLoopError(
                            "live delivery attempt target is not bound to its notification"
                        )
                elif live_count:
                    raise AgentLoopError(
                        "live delivery attempt exists without a launching notification"
                    )
                elif row["delivery_token"] or row["process_id"] is not None:
                    raise AgentLoopError(
                        "non-launching notification retains delivery authority"
                    )
            delivery_attempt_count = 0
            live_targets: dict[tuple[str, str], int] = {}
            for row in connection.execute("SELECT * FROM delivery_attempts"):
                delivery_attempt_count += 1
                if row["state"] not in DELIVERY_ATTEMPT_STATES:
                    raise AgentLoopError(
                        f"invalid delivery attempt state: {row['state']!r}"
                    )
                event = connection.execute(
                    "SELECT action, generation FROM events WHERE event_sha256 = ?",
                    (row["event_sha256"],),
                ).fetchone()
                if event is None:
                    raise AgentLoopError("delivery attempt references a missing event")
                if (
                    event["action"] != row["event_action"]
                    or event["generation"] != row["generation"]
                ):
                    raise AgentLoopError("delivery attempt event binding mismatch")
                if row["state"] == "live":
                    if (
                        not row["config_fingerprint"]
                        or not row["thread_id"]
                        or len(row["delivery_token_sha256"]) != 64
                    ):
                        raise AgentLoopError(
                            "live delivery attempt has empty mandatory binding"
                        )
                    target = (row["config_fingerprint"], row["thread_id"])
                    live_targets[target] = live_targets.get(target, 0) + 1
            for (fingerprint, thread_id), count in live_targets.items():
                if count > 1:
                    raise AgentLoopError(
                        "multiple live delivery attempts share one reviewer "
                        f"target: fingerprint {fingerprint[:12]}… thread {thread_id}"
                    )
            delivery_recovery_count = 0
            for row in connection.execute("SELECT * FROM delivery_recoveries"):
                delivery_recovery_count += 1
                if (
                    not row["current_event_sha256"]
                    or not row["config_fingerprint"]
                    or not row["thread_id"]
                    or not row["actor"]
                    or not row["reason"]
                    or not row["created_at"]
                ):
                    raise AgentLoopError("delivery recovery audit has empty mandatory binding")
                current_event = connection.execute(
                    "SELECT 1 FROM events WHERE event_sha256 = ?",
                    (row["current_event_sha256"],),
                ).fetchone()
                if current_event is None:
                    raise AgentLoopError("delivery recovery references a missing current event")
                revoked_event = row["revoked_event_sha256"]
                revoked_attempt_id = row["revoked_attempt_id"]
                if bool(revoked_event) != bool(revoked_attempt_id):
                    raise AgentLoopError(
                        "delivery recovery revoked event/attempt binding is incomplete"
                    )
                if revoked_attempt_id:
                    revoked = connection.execute(
                        "SELECT * FROM delivery_attempts WHERE attempt_id = ?",
                        (revoked_attempt_id,),
                    ).fetchone()
                    if (
                        revoked is None
                        or revoked["event_sha256"] != revoked_event
                        or revoked["state"] != "superseded"
                        or revoked["config_fingerprint"] != row["config_fingerprint"]
                        or revoked["thread_id"] != row["thread_id"]
                    ):
                        raise AgentLoopError(
                            "delivery recovery revoked attempt binding is invalid"
                        )
        snapshot = git_snapshot(self.repo_root)
        return {
            **status,
            "artifact_count": artifact_count,
            "notification_count": notification_count,
            "delivery_attempt_count": delivery_attempt_count,
            "delivery_recovery_count": delivery_recovery_count,
            "git": {
                "head": snapshot.head,
                "branch": snapshot.branch,
                "upstream": snapshot.upstream,
                "upstream_head": snapshot.upstream_head,
                "clean": snapshot.clean,
                "pushed": snapshot.pushed,
            },
            "healthy": True,
        }

    def artifact_content(self, kind: str, generation: int | None = None) -> str:
        if kind not in ARTIFACT_KINDS:
            raise AgentLoopError(f"invalid artifact kind: {kind}")
        with self._connect() as connection:
            metadata = self._validate_metadata(connection)
            resolved_generation = (
                generation if generation is not None else int(metadata["generation"])
            )
            artifact = self._artifact(connection, kind, resolved_generation)
            if artifact is None:
                raise AgentLoopError(
                    f"missing {kind} artifact for generation {resolved_generation}"
                )
            return str(artifact["content"])

    def _write_projections(self, status: dict[str, Any]) -> None:
        public_status = json.loads(json.dumps(status))
        if public_status.get("claim"):
            public_status["claim"].pop("token", None)
        public_status.pop("claim_token", None)
        _atomic_write(
            self.mailbox / "state.json",
            json.dumps(public_status, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        with self._connect() as connection:
            for row in connection.execute("SELECT * FROM artifacts ORDER BY generation, kind"):
                path = self.mailbox / f"{row['kind']}s" / f"{row['generation']:06d}.md"
                _atomic_write(path, row["content"])
