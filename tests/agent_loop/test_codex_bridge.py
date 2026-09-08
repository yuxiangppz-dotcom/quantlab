from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from quantlab.agent_loop import AgentLoop, AgentLoopError, codex_bridge
from quantlab.agent_loop.codex_bridge import (
    CodexBridgeConfig,
    CodexBridgeError,
    bridge_config_path,
    bridge_status,
    compose_bridge_config,
    deliver_review_event,
    kick_reviewer_notification,
)
from quantlab.agent_loop.protocol import _atomic_write, _utc_now


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _configure_bridge(
    loop: AgentLoop,
    *,
    thread_id: str,
    codex_executable: Path,
    probe_status: str = "verified",
    turn_timeout_seconds: int = 21_600,
    stale_delivery_seconds: int = 25_200,
) -> dict[str, object]:
    payload = compose_bridge_config(
        loop,
        thread_id=thread_id,
        codex_executable=codex_executable,
        bootstrapped_at=_utc_now(),
        probe_status=probe_status,
        probe_evidence={},
        turn_timeout_seconds=turn_timeout_seconds,
        stale_delivery_seconds=stale_delivery_seconds,
    )
    _atomic_write(
        bridge_config_path(loop),
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return payload


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "master", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "Bridge Test")
    _git(repo, "config", "user.email", "bridge@example.invalid")
    (repo / ".gitignore").write_text(".agent-loop/\n", encoding="utf-8")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "master")
    return repo


def _review_ready(loop: AgentLoop, tmp_path: Path) -> dict[str, object]:
    task = tmp_path / "task.md"
    task.write_text("task\n", encoding="utf-8")
    loop.publish_task(task, title="Task")
    claim = loop.claim_task(agent="zcode")
    implementation = loop.repo_root / "implementation.txt"
    implementation.write_text("done\n", encoding="utf-8")
    _git(loop.repo_root, "add", "implementation.txt")
    _git(loop.repo_root, "commit", "-m", "implementation")
    _git(loop.repo_root, "push")
    report = tmp_path / "report.md"
    report.write_text("report\n", encoding="utf-8")
    return loop.submit_report(
        report,
        claim_token=str(claim["claim_token"]),
        title="Report",
    )


def test_report_transaction_queues_one_head_bound_notification(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    result = _review_ready(loop, tmp_path)

    pending = loop.pending_review_notification()

    assert result["phase"] == "REVIEW_READY"
    assert pending is not None
    assert pending["generation"] == result["generation"]
    assert pending["event_action"] == "report_submitted"
    assert pending["event_sha256"] == result["last_event_sha256"]
    assert pending["state"] == "queued"
    assert loop.doctor()["notification_count"] == 1


def test_notification_queue_failure_rolls_back_report_transition(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    task = tmp_path / "task.md"
    task.write_text("task\n", encoding="utf-8")
    loop.publish_task(task, title="Task")
    claim = loop.claim_task(agent="zcode")
    implementation = repository / "implementation.txt"
    implementation.write_text("done\n", encoding="utf-8")
    _git(repository, "add", "implementation.txt")
    _git(repository, "commit", "-m", "implementation")
    _git(repository, "push")
    report = tmp_path / "report.md"
    report.write_text("report\n", encoding="utf-8")

    def injected_failure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected notification queue failure")

    monkeypatch.setattr(loop, "_queue_review_notification", injected_failure)
    with pytest.raises(RuntimeError, match="injected"):
        loop.submit_report(
            report,
            claim_token=str(claim["claim_token"]),
            title="Report",
        )

    assert loop.status(role="reviewer")["phase"] == "EXECUTING"
    with pytest.raises(AgentLoopError, match="missing report"):
        loop.artifact_content("report")


def test_blocker_transaction_also_queues_reviewer_notification(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    task = tmp_path / "task.md"
    task.write_text("task\n", encoding="utf-8")
    loop.publish_task(task, title="Task")
    claim = loop.claim_task(agent="zcode")
    blocker = tmp_path / "blocker.md"
    blocker.write_text("blocked\n", encoding="utf-8")

    result = loop.block_execution(
        blocker,
        claim_token=str(claim["claim_token"]),
        title="Blocked",
    )

    pending = loop.pending_review_notification()
    assert result["phase"] == "BLOCKED"
    assert pending is not None
    assert pending["event_action"] == "execution_blocked"
    assert pending["event_sha256"] == result["last_event_sha256"]


def test_delivery_claim_is_idempotent_and_private_token_is_cas_bound(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    _review_ready(loop, tmp_path)

    claimed = loop.claim_review_notification()

    assert claimed is not None
    assert loop.claim_review_notification() is None
    with pytest.raises(AgentLoopError, match="stale"):
        loop.finish_review_notification(
            event_sha256=str(claimed["event_sha256"]),
            delivery_token="wrong",
            delivered=True,
        )
    finished = loop.finish_review_notification(
        event_sha256=str(claimed["event_sha256"]),
        delivery_token=str(claimed["delivery_token"]),
        delivered=True,
        turn_id="turn-1",
    )
    assert finished["state"] == "delivered"
    assert finished["delivery_token"] == ""
    assert loop.claim_review_notification() is None


def test_failed_delivery_is_retryable_with_a_new_token(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    _review_ready(loop, tmp_path)
    first = loop.claim_review_notification()
    assert first is not None
    loop.finish_review_notification(
        event_sha256=str(first["event_sha256"]),
        delivery_token=str(first["delivery_token"]),
        delivered=False,
        error="injected launch failure",
        backoff_base_seconds=0,
        backoff_max_seconds=0,
    )

    second = loop.claim_review_notification()

    assert second is not None
    assert second["delivery_token"] != first["delivery_token"]
    assert second["attempt_count"] == 2

    with pytest.raises(AgentLoopError, match="stale"):
        loop.claimed_review_notification(
            event_sha256=str(first["event_sha256"]),
            delivery_token=str(first["delivery_token"]),
        )


def test_doctor_rejects_notification_to_event_binding_tamper(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    _review_ready(loop, tmp_path)
    with sqlite3.connect(loop.database) as connection:
        connection.execute(
            "UPDATE review_notifications SET event_action = 'forged'"
        )

    with pytest.raises(AgentLoopError, match="binding mismatch"):
        loop.doctor()


def test_invalid_bridge_config_fails_before_notification_claim(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    _review_ready(loop, tmp_path)
    config = loop.mailbox / "codex_bridge.json"
    config.write_text('{"enabled":true}\n', encoding="utf-8")

    with pytest.raises(CodexBridgeError, match="keys mismatch"):
        kick_reviewer_notification(loop)

    assert loop.pending_review_notification()["state"] == "queued"  # type: ignore[index]


def test_kick_launches_one_detached_worker_and_records_pid(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    _review_ready(loop, tmp_path)
    executable = tmp_path / "codex"
    executable.write_text("placeholder\n", encoding="utf-8")
    _configure_bridge(
        loop,
        thread_id="01a0749b-b253-7133-87d7-683ace12c634",
        codex_executable=executable,
    )
    launches: list[tuple[list[str], dict[str, object]]] = []

    class FakeProcess:
        pid = 4321

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        launches.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(codex_bridge.subprocess, "Popen", fake_popen)

    first = kick_reviewer_notification(loop)
    second = kick_reviewer_notification(loop)

    assert first["launched"] is True
    assert first["process_id"] == 4321
    assert second["launched"] is False
    assert second["status"] == "launching"
    assert len(launches) == 1
    assert launches[0][1]["start_new_session"] is True
    command, kwargs = launches[0]
    assert "--delivery-token" not in command
    worker_env = kwargs["env"]
    assert worker_env[codex_bridge.DELIVERY_TOKEN_ENV]
    assert loop.pending_review_notification()["process_id"] == 4321  # type: ignore[index]


def test_disabled_bridge_never_claims_notification(repository: Path, tmp_path: Path) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    _review_ready(loop, tmp_path)

    assert kick_reviewer_notification(loop) == {"status": "disabled", "launched": False}
    assert bridge_status(loop)["enabled"] is False
    assert loop.pending_review_notification()["state"] == "queued"  # type: ignore[index]


def test_app_server_client_resumes_thread_and_waits_for_completed_turn(tmp_path: Path) -> None:
    executable = tmp_path / "fake-codex"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    request_id = message.get("id")
    if request_id is None:
        continue
    method = message.get("method")
    result = {}
    if method == "thread/resume":
        result = {"thread": {"status": {"type": "idle"}}}
    if method == "turn/start":
        result = {"turn": {"id": "turn-1"}}
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
    if method == "turn/start":
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {
                "threadId": "01a0749b-b253-7133-87d7-683ace12c634",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        }), flush=True)
""",
        encoding="utf-8",
    )
    os.chmod(executable, 0o755)
    config = CodexBridgeConfig(
        thread_id="01a0749b-b253-7133-87d7-683ace12c634",
        codex_executable=executable,
        reviewer_kind="dedicated",
        config_fingerprint="f" * 64,
        bootstrapped_at=_utc_now(),
        probe_status="verified",
        probe_evidence={},
        turn_timeout_seconds=60,
        stale_delivery_seconds=120,
    )

    turn_id = deliver_review_event(
        config,
        repo_root=tmp_path,
        generation=3,
        event_action="report_submitted",
        event_sha256="a" * 64,
    )
    assert turn_id == "turn-1"


def test_config_rejects_stale_timeout_not_larger_than_turn_timeout(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    executable = tmp_path / "codex"
    executable.write_text("placeholder\n", encoding="utf-8")

    with pytest.raises(CodexBridgeError, match="must exceed"):
        compose_bridge_config(
            loop,
            thread_id="01a0749b-b253-7133-87d7-683ace12c634",
            codex_executable=executable,
            bootstrapped_at=_utc_now(),
            probe_status="verified",
            probe_evidence={},
            turn_timeout_seconds=120,
            stale_delivery_seconds=120,
        )


def test_complete_loop_can_only_restart_with_explicit_authority(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    _review_ready(loop, tmp_path)
    review = tmp_path / "review.md"
    review.write_text("accepted\n", encoding="utf-8")
    loop.submit_review(review, decision="complete", title="Complete")
    task = tmp_path / "next-task.md"
    task.write_text("next\n", encoding="utf-8")

    with pytest.raises(AgentLoopError, match="resume-complete"):
        loop.publish_task(task, title="Next")

    restarted = loop.publish_task(task, title="Next", resume_complete=True)
    assert restarted["phase"] == "TASK_READY"
    assert restarted["generation"] == 2
