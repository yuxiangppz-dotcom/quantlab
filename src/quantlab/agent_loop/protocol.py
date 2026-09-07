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
        return connection

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

    def publish_task(
        self,
        task_file: Path,
        *,
        title: str,
        actor: str = "codex-reviewer",
        expected_head: str | None = None,
        resume_blocked: bool = False,
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
                allowed = metadata["phase"] == "IDLE" or (
                    resume_blocked and metadata["phase"] == "BLOCKED"
                )
                if not allowed:
                    raise AgentLoopError(
                        f"publish_task requires IDLE (or BLOCKED with --resume-blocked), "
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
                self._append_event(
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
            if generation > 0:
                required_artifacts.append(("task", generation))
            if phase in {"REVIEW_READY", "COMPLETE"}:
                required_artifacts.append(("report", generation))
            artifacts: dict[str, Any] = {}
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
        snapshot = git_snapshot(self.repo_root)
        return {
            **status,
            "artifact_count": artifact_count,
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
