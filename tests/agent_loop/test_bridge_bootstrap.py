"""Dedicated reviewer bootstrap and fail-closed delivery correctness.

These tests use protocol-faithful fake App Server processes and never call the
real Codex service.  They distinguish exactly the three generation-3 failure
classes:

1. ``thread/start`` without any completed turn is not persisted and must never
   be accepted as a ready reviewer target;
2. a foreign/interactive active-writer thread is a deterministic configuration
   failure, not an indefinitely retryable transient;
3. a dedicated thread with one completed bootstrap turn survives creator exit
   and can be resumed by a fresh App Server process for a later review turn.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from quantlab.agent_loop import AgentLoop, AgentLoopError, codex_bridge
from quantlab.agent_loop.codex_bridge import (
    BOOTSTRAP_PROMPT,
    CodexBridgeError,
    bootstrap_codex_reviewer,
    bridge_config_path,
    bridge_status,
    compute_config_fingerprint,
    kick_reviewer_notification,
    probe_codex_reviewer,
    read_bootstrap_record,
)
from quantlab.agent_loop.protocol import _atomic_write, _utc_now

_THREAD = "01a0749b-b253-7133-87d7-683ace12c634"
_THREAD_B = "01bffffff-b253-7133-87d7-683ace12c634"

_FAKE_PREFIX = '''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

COUNT_FILE = Path(@COUNT_FILE@)
THREAD_ID = @THREAD_ID@

def send(value):
    print(json.dumps(value), flush=True)

def reply(request_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result if result is not None else {}
    send(payload)

count = int(COUNT_FILE.read_text()) if COUNT_FILE.exists() else 0
COUNT_FILE.write_text(str(count + 1))
@PROLOGUE@
for line in sys.stdin:
    message = json.loads(line)
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        continue
    if method == "initialize":
        reply(request_id)
        continue
@HANDLERS@
'''

_THREE_PHASE = '''    if count == 0:
        if method == "thread/start":
            reply(request_id, result={"thread": {"id": THREAD_ID}})
        elif method == "turn/start":
@CAPTURE@            reply(request_id, result={"turn": {"id": "boot-turn-1"}})
            send({"jsonrpc": "2.0", "method": "turn/completed", "params": {
                "threadId": THREAD_ID,
                "turn": {"id": "boot-turn-1", "status": "completed"}}})
        else:
            reply(request_id, error={"code": -32601, "message": f"unsupported {method}"})
    elif count == 1:
        if method == "thread/read":
            reply(request_id, result={"thread": {"id": THREAD_ID}})
        elif method == "thread/resume":
            reply(request_id, result={"thread": {"id": THREAD_ID, "status": {"type": "idle"}}})
        else:
            reply(request_id, error={"code": -32601, "message": f"unsupported {method}"})
    else:
        if method == "thread/resume":
            reply(request_id, result={"thread": {"id": THREAD_ID, "status": {"type": "idle"}}})
        elif method == "turn/start":
            turn_id = f"review-turn-{count}"
            reply(request_id, result={"turn": {"id": turn_id}})
            send({"jsonrpc": "2.0", "method": "turn/completed", "params": {
                "threadId": THREAD_ID,
                "turn": {"id": turn_id, "status": "completed"}}})
        else:
            reply(request_id, error={"code": -32601, "message": f"unsupported {method}"})
'''

_CREATOR_NO_COMPLETED = '''    if count == 0:
        if method == "thread/start":
            reply(request_id, result={"thread": {"id": THREAD_ID}})
        elif method == "turn/start":
            reply(request_id, result={"turn": {"id": "boot-turn-1"}})
            sys.exit(0)
        else:
            reply(request_id, error={"code": -32601, "message": f"unsupported {method}"})
    else:
        if method == "thread/read":
            reply(request_id, result={"thread": {"id": THREAD_ID}})
        elif method == "thread/resume":
            reply(request_id, result={"thread": {"id": THREAD_ID, "status": {"type": "idle"}}})
        else:
            reply(request_id, error={"code": -32601, "message": f"unsupported {method}"})
'''

_VERIFIER_NO_ROLLOUT = '''    if count == 0:
        if method == "thread/start":
            reply(request_id, result={"thread": {"id": THREAD_ID}})
        elif method == "turn/start":
            reply(request_id, result={"turn": {"id": "boot-turn-1"}})
            send({"jsonrpc": "2.0", "method": "turn/completed", "params": {
                "threadId": THREAD_ID,
                "turn": {"id": "boot-turn-1", "status": "completed"}}})
        else:
            reply(request_id, error={"code": -32601, "message": f"unsupported {method}"})
    else:
        detail = f"no rollout found for thread id {THREAD_ID}"
        reply(request_id, error={"code": -32600, "message": detail})
'''

_ACTIVE_WRITER = '''    if method == "thread/resume":
        detail = f"thread {THREAD_ID} already has an active writer"
        reply(request_id, error={"code": -32600, "message": detail})
    else:
        reply(request_id, error={"code": -32601, "message": f"unsupported {method}"})
'''

_DELIVERY_TURN = '''    if method == "thread/resume":
        reply(request_id, result={"thread": {"id": THREAD_ID, "status": {"type": "idle"}}})
    elif method == "turn/start":
        reply(request_id, result={"turn": {"id": "review-turn-1"}})
        send({"jsonrpc": "2.0", "method": "turn/completed", "params": {
            "threadId": THREAD_ID,
            "turn": {"id": "@COMPLETED_TURN@", "status": "@COMPLETED_STATUS@"}}})
        sys.exit(0)
    else:
        reply(request_id, error={"code": -32601, "message": f"unsupported {method}"})
'''

_ELICITATION = '''    if method == "thread/resume":
        reply(request_id, result={"thread": {"id": THREAD_ID, "status": {"type": "idle"}}})
    elif method == "turn/start":
        reply(request_id, result={"turn": {"id": "review-turn-1"}})
        send({"jsonrpc": "2.0", "id": 99, "method": "elicitation/create", "params": {}})
        sys.exit(0)
    else:
        reply(request_id, error={"code": -32601, "message": f"unsupported {method}"})
'''


def _fake_codex(
    path: Path,
    *,
    handlers: str = "",
    prologue: str = "",
    prompt_capture: Path | None = None,
    completed_turn: str = "review-turn-1",
    completed_status: str = "completed",
    thread_id: str = _THREAD,
) -> Path:
    capture = ""
    if prompt_capture is not None:
        capture = (
            "            Path("
            + repr(str(prompt_capture))
            + ').write_text(message["params"]["input"][0]["text"], encoding="utf-8")\n'
        )
    if not handlers:
        handlers = "        pass\n"
    script = (
        _FAKE_PREFIX.replace("@COUNT_FILE@", repr(str(path.with_suffix(".count"))))
        .replace("@THREAD_ID@", repr(thread_id))
        .replace("@PROLOGUE@", prologue)
        .replace("@HANDLERS@", handlers)
        .replace("@CAPTURE@", capture)
        .replace("@COMPLETED_TURN@", completed_turn)
        .replace("@COMPLETED_STATUS@", completed_status)
    )
    path.write_text(script, encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def _write_bridge_config(
    loop: AgentLoop,
    *,
    thread_id: str = _THREAD,
    codex_executable: Path,
    probe_status: str = "verified",
) -> dict[str, object]:
    payload = codex_bridge.compose_bridge_config(
        loop,
        thread_id=thread_id,
        codex_executable=codex_executable,
        bootstrapped_at=_utc_now(),
        probe_status=probe_status,
        probe_evidence={},
    )
    _atomic_write(
        bridge_config_path(loop),
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return payload


def test_bootstrap_survives_creator_exit_and_writes_verified_config(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    prompt_out = tmp_path / "captured-prompt.txt"
    executable = _fake_codex(
        tmp_path / "codex-three-phase",
        handlers=_THREE_PHASE,
        prompt_capture=prompt_out,
    )

    result = bootstrap_codex_reviewer(
        loop, codex_executable=executable, bootstrap_timeout_seconds=120
    )

    assert result["status"] == "ready"
    assert result["reviewer_kind"] == "dedicated"
    assert result["thread_id"] == _THREAD
    assert result["persistence"]["thread_read_after_restart"] is True
    assert result["persistence"]["thread_resume_after_restart"] is True
    assert result["persistence"]["creator_process_exited"] is True
    assert result["config_fingerprint"] == compute_config_fingerprint(
        thread_id=_THREAD, codex_executable=executable, repo_root=repository
    )
    config = codex_bridge.load_bridge_config(loop)
    assert config is not None
    assert config.probe_status == "verified"
    assert config.reviewer_kind == "dedicated"
    assert config.config_fingerprint == result["config_fingerprint"]
    assert read_bootstrap_record(loop)["status"] == "ready"  # type: ignore[index]
    assert prompt_out.read_text(encoding="utf-8") == BOOTSTRAP_PROMPT


def test_probe_proves_persistence_without_writing_config(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    executable = _fake_codex(tmp_path / "codex-three-phase", handlers=_THREE_PHASE)

    result = probe_codex_reviewer(
        loop, codex_executable=executable, bootstrap_timeout_seconds=120
    )

    assert result["status"] == "ready"
    assert result["config_path"] is None
    assert not bridge_config_path(loop).exists()
    assert bridge_status(loop)["enabled"] is False
    assert read_bootstrap_record(loop)["wrote_config"] is False  # type: ignore[index]


def test_bootstrap_rejects_thread_without_completed_turn(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    executable = _fake_codex(tmp_path / "codex", handlers=_CREATOR_NO_COMPLETED)

    with pytest.raises(CodexBridgeError, match="bootstrap failed"):
        bootstrap_codex_reviewer(
            loop, codex_executable=executable, bootstrap_timeout_seconds=120
        )

    assert not bridge_config_path(loop).exists()
    record = read_bootstrap_record(loop)
    assert record is not None
    assert record["status"] == "incomplete"
    assert record["wrote_config"] is False
    assert record["thread_id"] == _THREAD


def test_bootstrap_rejects_unpersisted_thread_found_on_restart(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    executable = _fake_codex(tmp_path / "codex", handlers=_VERIFIER_NO_ROLLOUT)

    with pytest.raises(CodexBridgeError, match="no rollout found"):
        bootstrap_codex_reviewer(
            loop, codex_executable=executable, bootstrap_timeout_seconds=120
        )

    assert not bridge_config_path(loop).exists()
    record = read_bootstrap_record(loop)
    assert record is not None
    assert record["status"] == "incomplete"


def test_load_bridge_config_rejects_legacy_and_tampered_configs(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    executable = tmp_path / "codex"
    executable.write_text("placeholder\n", encoding="utf-8")
    config_path = bridge_config_path(loop)

    legacy = {
        "protocol_version": "quantlab_codex_review_bridge_v1",
        "enabled": True,
        "thread_id": _THREAD,
        "codex_executable": str(executable),
        "turn_timeout_seconds": 21_600,
        "stale_delivery_seconds": 25_200,
    }
    config_path.write_text(json.dumps(legacy), encoding="utf-8")
    with pytest.raises(CodexBridgeError, match="keys mismatch"):
        codex_bridge.load_bridge_config(loop)

    legacy["reviewer_kind"] = "dedicated"
    legacy["config_fingerprint"] = "f" * 64
    legacy["bootstrapped_at"] = _utc_now()
    legacy["probe_status"] = "verified"
    legacy["probe_evidence"] = {}
    config_path.write_text(json.dumps(legacy), encoding="utf-8")
    with pytest.raises(CodexBridgeError, match="bootstrap-codex-reviewer"):
        codex_bridge.load_bridge_config(loop)

    payload = _write_bridge_config(loop, codex_executable=executable)
    payload["config_fingerprint"] = "0" * 64
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CodexBridgeError, match="fingerprint mismatch"):
        codex_bridge.load_bridge_config(loop)

    payload["config_fingerprint"] = "x" * 64
    payload["reviewer_kind"] = "interactive"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CodexBridgeError, match="dedicated"):
        codex_bridge.load_bridge_config(loop)


def test_dedicated_thread_completes_a_later_review_turn(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    executable = _fake_codex(tmp_path / "codex-three-phase", handlers=_THREE_PHASE)
    bootstrap_codex_reviewer(
        loop, codex_executable=executable, bootstrap_timeout_seconds=120
    )
    make_review_ready(loop, tmp_path)

    result = kick_reviewer_notification(loop, synchronous=True)

    assert result["status"] == "delivered"
    assert result["turn_id"].startswith("review-turn-")
    pending = loop.pending_review_notification()
    assert pending is not None
    assert pending["state"] == "delivered"
    with sqlite3.connect(loop.database) as connection:
        attempts = connection.execute(
            "SELECT state, turn_id FROM delivery_attempts"
        ).fetchall()
    assert attempts == [("delivered", result["turn_id"])]


def test_active_writer_failure_is_deterministic_and_blocks_retry(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    executable = _fake_codex(tmp_path / "codex", handlers=_ACTIVE_WRITER)
    payload = _write_bridge_config(loop, codex_executable=executable)
    fingerprint = payload["config_fingerprint"]

    first = kick_reviewer_notification(loop, synchronous=True)

    assert first["status"] == "failed"
    assert first["classification"] == "configuration"
    pending = loop.pending_review_notification()
    assert pending is not None
    assert pending["state"] == "failed"
    assert pending["retry_class"] == "configuration"
    assert pending["blocked_fingerprint"] == fingerprint
    assert pending["next_retry_at"] == ""

    second = kick_reviewer_notification(loop, synchronous=True)

    assert second["status"] == "configuration_blocked"
    assert second["launched"] is False
    assert second["blocked_fingerprint"] == fingerprint
    assert loop.doctor()["delivery_attempt_count"] == 1


def test_new_probed_configuration_resumes_delivery_after_block(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    blocked_executable = _fake_codex(tmp_path / "codex-blocked", handlers=_ACTIVE_WRITER)
    _write_bridge_config(loop, codex_executable=blocked_executable)
    assert kick_reviewer_notification(loop, synchronous=True)["status"] == "failed"

    good_executable = _fake_codex(
        tmp_path / "codex-good",
        handlers=_DELIVERY_TURN,
        completed_status="completed",
        thread_id=_THREAD_B,
    )
    _write_bridge_config(loop, thread_id=_THREAD_B, codex_executable=good_executable)

    result = kick_reviewer_notification(loop, synchronous=True)

    assert result["status"] == "delivered"


def test_recover_bridge_delivery_clears_blocked_fingerprint(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    executable = _fake_codex(tmp_path / "codex", handlers=_ACTIVE_WRITER)
    _write_bridge_config(loop, codex_executable=executable)
    assert kick_reviewer_notification(loop, synchronous=True)["status"] == "failed"
    assert kick_reviewer_notification(loop, synchronous=True)["status"] == (
        "configuration_blocked"
    )

    recovered = loop.recover_bridge_delivery(reason="operator decision after inspection")

    assert recovered["state"] == "queued"
    assert recovered["blocked_fingerprint"] == ""
    retry = kick_reviewer_notification(loop, synchronous=True)
    assert retry["status"] == "failed"
    assert retry["classification"] == "configuration"


def test_transient_failure_schedules_backoff_and_kick_stays_idle(
    repository: Path, tmp_path: Path, make_review_ready, monkeypatch
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    executable = _fake_codex(tmp_path / "codex", prologue="sys.exit(7)\n")
    _write_bridge_config(loop, codex_executable=executable)

    first = kick_reviewer_notification(loop, synchronous=True)

    assert first["status"] == "failed"
    assert first["classification"] == "transient"
    pending = loop.pending_review_notification()
    assert pending is not None
    assert pending["retry_class"] == "transient"
    assert pending["blocked_fingerprint"] == ""
    due = pending["next_retry_at"]
    assert due > _utc_now()

    launches: list[list[str]] = []
    monkeypatch.setattr(
        codex_bridge.subprocess,
        "Popen",
        lambda command, **kwargs: launches.append(command) or type("P", (), {"pid": 1})(),
    )
    second = kick_reviewer_notification(loop)

    assert second["status"] == "backoff"
    assert second["launched"] is False
    assert launches == []


def test_backoff_expiry_via_recovery_allows_delivery(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    failing = _fake_codex(tmp_path / "codex-failing", prologue="sys.exit(7)\n")
    _write_bridge_config(loop, codex_executable=failing)
    assert kick_reviewer_notification(loop, synchronous=True)["status"] == "failed"
    assert kick_reviewer_notification(loop)["status"] == "backoff"

    good = _fake_codex(tmp_path / "codex-good", handlers=_DELIVERY_TURN)
    _write_bridge_config(loop, codex_executable=good)
    assert kick_reviewer_notification(loop)["status"] == "backoff"
    loop.recover_bridge_delivery(reason="transient fault cleared")

    result = kick_reviewer_notification(loop, synchronous=True)
    assert result["status"] == "delivered"


def test_stale_worker_cannot_finish_or_overwrite_newer_attempt(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    first = loop.claim_review_notification()
    assert first is not None
    with sqlite3.connect(loop.database) as connection:
        connection.execute(
            "UPDATE review_notifications SET updated_at = '2020-01-01T00:00:00Z'"
        )

    second_loop = AgentLoop(repository)
    second = second_loop.claim_review_notification()
    assert second is not None
    assert second["delivery_token"] != first["delivery_token"]
    assert second["attempt_count"] == 2

    with pytest.raises(AgentLoopError, match="stale"):
        second_loop.finish_review_notification(
            event_sha256=str(first["event_sha256"]),
            delivery_token=str(first["delivery_token"]),
            delivered=True,
            turn_id="stale-turn",
        )

    finished = second_loop.finish_review_notification(
        event_sha256=str(second["event_sha256"]),
        delivery_token=str(second["delivery_token"]),
        delivered=True,
        turn_id="review-turn-2",
    )
    assert finished["state"] == "delivered"
    with sqlite3.connect(loop.database) as connection:
        states = [
            row[0]
            for row in connection.execute(
                "SELECT state FROM delivery_attempts ORDER BY attempt_number"
            )
        ]
    assert states == ["superseded", "delivered"]


def test_delivered_requires_matching_turn_id_and_completed_status(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)

    mismatched = _fake_codex(
        tmp_path / "codex-mismatch", handlers=_DELIVERY_TURN, completed_turn="other-turn"
    )
    _write_bridge_config(loop, codex_executable=mismatched)
    failed = kick_reviewer_notification(loop, synchronous=True)
    assert failed["status"] == "failed"
    assert loop.pending_review_notification()["state"] == "failed"  # type: ignore[index]

    for index, status in enumerate(("failed", "complete"), start=1):
        executable = _fake_codex(
            tmp_path / f"codex-{index}",
            handlers=_DELIVERY_TURN,
            completed_status=status,
        )
        _write_bridge_config(loop, codex_executable=executable)
        loop.recover_bridge_delivery(reason="retry non-completed status")
        result = kick_reviewer_notification(loop, synchronous=True)
        assert result["status"] == "failed"
        assert f"ended with status {status!r}" in result["error"]
    pending = loop.pending_review_notification()
    assert pending is not None
    assert pending["state"] == "failed"


def test_approval_request_fails_attempt_without_granting_authority(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    executable = _fake_codex(tmp_path / "codex", handlers=_ELICITATION)
    _write_bridge_config(loop, codex_executable=executable)

    result = kick_reviewer_notification(loop, synchronous=True)

    assert result["status"] == "failed"
    pending = loop.pending_review_notification()
    assert pending is not None
    assert pending["state"] == "failed"
    assert "approval or user interaction" in pending["last_error"]


def test_delivery_token_is_redacted_from_errors(
    repository: Path,
    tmp_path: Path,
    make_review_ready,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    src_root = str(Path(codex_bridge.__file__).resolve().parents[2])
    repo_root = Path(codex_bridge.__file__).resolve().parents[3]
    monkeypatch.setenv("PYTHONPATH", src_root)
    scripts_dir = repository / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    shutil.copy(repo_root / "scripts" / "agent_loop_notify.py", scripts_dir)
    leak = (
        "import os, sys\n"
        'print(os.environ.get("QUANTLAB_AGENT_LOOP_DELIVERY_TOKEN", ""), '
        "file=sys.stderr, flush=True)\n"
        "sys.exit(3)\n"
    )
    executable = _fake_codex(tmp_path / "codex-leak", handlers="", prologue=leak)
    _write_bridge_config(loop, codex_executable=executable)

    assert codex_bridge._redact_token("secret abc secret", "abc") == "secret <redacted> secret"

    launched = kick_reviewer_notification(loop)
    assert launched["status"] == "launching"

    log_path = Path(str(launched["log_path"]))
    raw_token = ""

    def _raw_token() -> str:
        with sqlite3.connect(loop.database) as connection:
            row = connection.execute(
                "SELECT delivery_token FROM review_notifications"
            ).fetchone()
        return str(row[0])

    state = ""
    for _ in range(100):
        raw_token = raw_token or _raw_token()
        pending = loop.pending_review_notification()
        assert pending is not None
        state = str(pending["state"])
        if state == "failed":
            break
        time.sleep(0.1)
    assert state == "failed"
    assert raw_token, "launching token was never observable"
    last_error = str(loop.pending_review_notification()["last_error"])
    assert "<redacted>" in last_error
    assert raw_token not in last_error
    assert raw_token not in log_path.read_text(encoding="utf-8")


def test_worker_launch_failure_keeps_report_committed(
    repository: Path, tmp_path: Path, make_review_ready, monkeypatch
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    executable = tmp_path / "codex"
    executable.write_text("placeholder\n", encoding="utf-8")
    _write_bridge_config(loop, codex_executable=executable)

    def failing_popen(command: list[str], **kwargs: object) -> object:
        raise OSError("cannot spawn worker")

    monkeypatch.setattr(codex_bridge.subprocess, "Popen", failing_popen)

    result = kick_reviewer_notification(loop)

    assert result["status"] == "failed"
    pending = loop.pending_review_notification()
    assert pending is not None
    assert pending["state"] == "failed"
    assert pending["retry_class"] == "transient"
    assert loop.status(role="reviewer")["artifacts"]["report"]["content"] == "report\n"


def test_executor_status_in_task_ready_never_touches_bridge(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    task = tmp_path / "task.md"
    task.write_text("task\n", encoding="utf-8")
    loop.publish_task(task, title="Task")
    bridge_config_path(loop).write_text("definitely not json", encoding="utf-8")

    cli = Path(codex_bridge.__file__).resolve().parents[3] / "scripts" / "agent_loop.py"
    src_root = str(Path(codex_bridge.__file__).resolve().parents[2])

    def run_status() -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, str(cli), "status", "--role", "executor"],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONPATH": src_root},
        )
        return json.loads(completed.stdout)

    result = run_status()
    assert result["action"] == "claim_task"
    assert "codex_review_delivery" not in result

    claim = loop.claim_task(agent="zcode")
    implementation = repository / "implementation.txt"
    implementation.write_text("done\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "implementation.txt"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "implementation"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "push"], cwd=repository, check=True, capture_output=True)
    report = tmp_path / "report.md"
    report.write_text("report\n", encoding="utf-8")
    loop.submit_report(
        report, claim_token=str(claim["claim_token"]), title="Report"
    )

    ready = run_status()
    assert ready["phase"] == "REVIEW_READY"
    assert ready["codex_review_delivery"]["status"] == "failed"


def test_migration_from_legacy_schema_is_idempotent_and_preserves_history(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    with sqlite3.connect(loop.database) as connection:
        columns = ", ".join(
            row[1]
            for row in connection.execute("PRAGMA table_info(review_notifications)")
            if row[1]
            in {
                "event_sha256",
                "generation",
                "event_action",
                "state",
                "attempt_count",
                "delivery_token",
                "process_id",
                "last_error",
                "created_at",
                "updated_at",
            }
        )
        legacy_row = connection.execute(
            f"SELECT {columns} FROM review_notifications"
        ).fetchone()
        connection.execute("DROP TABLE delivery_attempts")
        connection.execute("DROP TABLE review_notifications")
        connection.execute(
            """
            CREATE TABLE review_notifications (
                event_sha256 TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                event_action TEXT NOT NULL,
                state TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                delivery_token TEXT NOT NULL DEFAULT '',
                process_id INTEGER,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (event_sha256) REFERENCES events(event_sha256)
            )
            """
        )
        placeholders = ", ".join("?" for _ in legacy_row)
        connection.execute(
            f"INSERT INTO review_notifications ({columns}) VALUES ({placeholders})",
            legacy_row,
        )

    first = loop.status(role="reviewer")
    second = loop.status(role="reviewer")

    notification = first["review_notification"]
    assert notification["event_action"] == "report_submitted"
    assert notification["state"] == "queued"
    assert notification["config_fingerprint"] == ""
    assert notification["blocked_fingerprint"] == ""
    assert notification["next_retry_at"] == ""
    assert second["review_notification"] == notification
    healthy = loop.doctor()
    assert healthy["healthy"] is True
    assert healthy["delivery_attempt_count"] == 0


def test_doctor_rejects_delivery_attempt_binding_tamper(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    executable = _fake_codex(tmp_path / "codex", handlers=_ACTIVE_WRITER)
    _write_bridge_config(loop, codex_executable=executable)
    assert kick_reviewer_notification(loop, synchronous=True)["status"] == "failed"

    with sqlite3.connect(loop.database) as connection:
        connection.execute("UPDATE delivery_attempts SET event_action = 'forged'")

    with pytest.raises(AgentLoopError, match="delivery attempt event binding mismatch"):
        loop.doctor()
