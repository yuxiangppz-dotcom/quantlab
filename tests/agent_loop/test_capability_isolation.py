"""Generation-5 gap reproduction: capability isolation and serialization.

Every case here encodes a generation-4 review finding as a deterministic,
protocol-faithful fake App Server scenario (the real Codex service is never
called):

1. the raw delivery capability must stop at the detached worker: the Codex
   App Server and its descendants never see
   ``QUANTLAB_AGENT_LOOP_DELIVERY_TOKEN`` while delivery still completes
   through the worker's retained in-memory token;
2. delivery is serialized per dedicated reviewer target across events and
   generations: a different event must not launch while another event's
   attempt is live, with bounded stale recovery and exactly-once claiming;
3. an active-writer/busy overlap on the verified dedicated target is a
   transient retry that never poisons the fingerprint, while ``no rollout
   found`` stays a deterministic configuration failure;
4. only identity-matched, persistence-verified configurations load as
   enabled, ambiguous live history fails migration closed, and ``doctor``
   validates the target-level live-attempt bindings.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from quantlab.agent_loop import AgentLoop, AgentLoopError, codex_bridge
from quantlab.agent_loop.codex_bridge import (
    CodexBridgeError,
    bootstrap_codex_reviewer,
    bridge_config_path,
    compose_bridge_config,
    kick_reviewer_notification,
)
from quantlab.agent_loop.protocol import _atomic_write, _utc_now

_THREAD = "01a0749b-b253-7133-87d7-683ace12c634"
_THREAD_B = "01bffffff-b253-7133-87d7-683ace12c634"
_TOKEN_ENV = codex_bridge.DELIVERY_TOKEN_ENV

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
            reply(request_id, result={"turn": {"id": "boot-turn-1"}})
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

_DELIVERY_TURN = '''    if method == "thread/resume":
        reply(request_id, result={"thread": {"id": THREAD_ID, "status": {"type": "idle"}}})
    elif method == "turn/start":
@CAPTURE@        reply(request_id, result={"turn": {"id": "@COMPLETED_TURN@"}})
        send({"jsonrpc": "2.0", "method": "turn/completed", "params": {
            "threadId": THREAD_ID,
            "turn": {"id": "@COMPLETED_TURN@", "status": "@COMPLETED_STATUS@"}}})
        sys.exit(0)
    else:
        reply(request_id, error={"code": -32601, "message": f"unsupported {method}"})
'''

_ACTIVE_WRITER = '''    if method == "thread/resume":
        detail = f"thread {THREAD_ID} already has an active writer"
        reply(request_id, error={"code": -32600, "message": detail})
    else:
        reply(request_id, error={"code": -32601, "message": f"unsupported {method}"})
'''

_NO_ROLLOUT_RESUME = '''    if method == "thread/resume":
        detail = f"no rollout found for thread id {THREAD_ID}"
        reply(request_id, error={"code": -32600, "message": detail})
    else:
        reply(request_id, error={"code": -32601, "message": f"unsupported {method}"})
'''

_IDENTITY_PROOF = '''    if count == 0:
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
        if method == "thread/read":
            reply(request_id, result={"thread": {"id": @READ_ID@}})
        elif method == "thread/resume":
            reply(request_id, result={"thread": {"id": @RESUME_ID@, "status": {"type": "idle"}}})
        else:
            reply(request_id, error={"code": -32601, "message": f"unsupported {method}"})
'''


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _fake_codex(
    path: Path,
    *,
    handlers: str = "        pass\n",
    prologue: str = "",
    prompt_capture: Path | None = None,
    completed_turn: str = "review-turn-1",
    completed_status: str = "completed",
    thread_id: str = _THREAD,
) -> Path:
    capture = ""
    if prompt_capture is not None:
        capture = (
            "        Path("
            + repr(str(prompt_capture))
            + ').write_text(message["params"]["input"][0]["text"], encoding="utf-8")\n'
        )
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


def _verified_evidence(bootstrap_turn_id: str = "boot-turn-1") -> dict[str, object]:
    return {
        "bootstrap_turn_id": bootstrap_turn_id,
        "persistence": {
            "bootstrap_turn_completed": True,
            "creator_process_exited": True,
            "thread_read_after_restart": True,
            "thread_resume_after_restart": True,
        },
        "started_at": _utc_now(),
        "finished_at": _utc_now(),
    }


def _write_verified_config(
    loop: AgentLoop,
    *,
    thread_id: str = _THREAD,
    codex_executable: Path,
    probe_status: str = "verified",
    probe_evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = compose_bridge_config(
        loop,
        thread_id=thread_id,
        codex_executable=codex_executable,
        bootstrapped_at=_utc_now(),
        probe_status=probe_status,
        probe_evidence=(
            _verified_evidence() if probe_evidence is None else probe_evidence
        ),
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
    _git(repo, "config", "user.name", "Capability Test")
    _git(repo, "config", "user.email", "capability@example.invalid")
    (repo / ".gitignore").write_text(".agent-loop/\n", encoding="utf-8")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "master")
    return repo


def _wait_for_state(
    loop: AgentLoop, states: set[str], timeout_seconds: float = 20.0
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pending = loop.pending_review_notification()
        if pending is not None and pending["state"] in states:
            return pending
        time.sleep(0.1)
    raise AssertionError(f"notification never reached {sorted(states)}")


def _raw_delivery_token(loop: AgentLoop) -> str:
    with sqlite3.connect(loop.database) as connection:
        row = connection.execute(
            "SELECT delivery_token FROM review_notifications"
        ).fetchone()
    return str(row[0])


def _advance_to_next_generation(loop: AgentLoop, tmp_path: Path) -> None:
    """Drive generation N+1: review, claim, commit, push, report (event B)."""

    review = tmp_path / "review.md"
    review.write_text("accepted\n", encoding="utf-8")
    next_task = tmp_path / "next-task.md"
    next_task.write_text("next bounded task\n", encoding="utf-8")
    loop.submit_review(
        review,
        decision="advance",
        title="Review A",
        next_task_file=next_task,
        next_task_title="Task B",
    )
    claim = loop.claim_task(agent="zcode")
    implementation = loop.repo_root / "implementation.txt"
    implementation.write_text(
        implementation.read_text(encoding="utf-8") + "more\n", encoding="utf-8"
    )
    _git(loop.repo_root, "add", "implementation.txt")
    _git(loop.repo_root, "commit", "-m", "more implementation")
    _git(loop.repo_root, "push")
    report = tmp_path / "report-b.md"
    report.write_text("report b\n", encoding="utf-8")
    loop.submit_report(report, claim_token=str(claim["claim_token"]), title="Report B")


def _two_events_on_one_target(
    repository: Path, tmp_path: Path, make_review_ready
) -> tuple[AgentLoop, str, str, dict[str, object]]:
    """Event A live on the dedicated target while event B queues behind it."""

    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    executable = _fake_codex(tmp_path / "codex-deliver", handlers=_DELIVERY_TURN)
    payload = _write_verified_config(loop, codex_executable=executable)
    fingerprint = str(payload["config_fingerprint"])
    thread_id = str(payload["thread_id"])
    claim_a = loop.claim_review_notification(
        config_fingerprint=fingerprint, thread_id=thread_id
    )
    assert claim_a is not None
    _advance_to_next_generation(loop, tmp_path)
    return loop, fingerprint, thread_id, claim_a


# ---------------------------------------------------------------------------
# Gap 1: the delivery capability never reaches the Codex App Server tree.
# ---------------------------------------------------------------------------


def test_app_server_launch_environment_excludes_delivery_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_TOKEN_ENV, "raw-app-server-secret")
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 7
        stdin = None
        stdout = None
        stderr = None

        def kill(self) -> None:
            return None

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return FakeProcess()

    monkeypatch.setattr(codex_bridge.subprocess, "Popen", fake_popen)
    session = codex_bridge._AppServerSession(
        codex_executable=tmp_path / "codex",
        repo_root=tmp_path,
        timeout_seconds=60,
    )

    with pytest.raises(CodexBridgeError, match="stdio"):
        session._start()

    environment = captured["env"]
    assert environment is not None, "App Server must be launched with an explicit env"
    assert _TOKEN_ENV not in environment
    assert os.environ[_TOKEN_ENV] == "raw-app-server-secret"


def test_notify_worker_drops_token_from_environment_after_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(codex_bridge.__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "agent_loop_notify_under_test",
        repo_root / "scripts" / "agent_loop_notify.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured: dict[str, object] = {}

    def fake_deliver(loop: object, *, event_sha256: str, delivery_token: str) -> dict:
        captured["event"] = event_sha256
        captured["token"] = delivery_token
        captured["env_after_read"] = os.environ.get(_TOKEN_ENV)
        return {"delivered": True, "turn_id": "turn-1"}

    monkeypatch.setattr(module, "deliver_claimed_notification", fake_deliver)
    monkeypatch.setenv(_TOKEN_ENV, "raw-worker-secret")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent_loop_notify.py",
            "--repo",
            str(tmp_path),
            "--mailbox",
            str(tmp_path),
            "--event-sha256",
            "a" * 64,
        ],
    )

    assert module.main() == 0

    assert captured["token"] == "raw-worker-secret"
    assert captured["env_after_read"] is None


def test_delivery_completes_without_token_reaching_app_server_tree(
    repository: Path,
    tmp_path: Path,
    make_review_ready,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    prompt_out = tmp_path / "captured-prompt.txt"
    app_marker = tmp_path / "app-server-marker.json"
    child_marker = tmp_path / "descendant-marker.json"
    child_code = (
        "import json,os,sys;from pathlib import Path;"
        "Path(sys.argv[1]).write_text(json.dumps("
        "{'env_has_token': 'QUANTLAB_AGENT_LOOP_DELIVERY_TOKEN' in os.environ}))"
    )
    prologue = (
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        f"Path({str(app_marker)!r}).write_text(json.dumps({{\n"
        f"    'argv': sys.argv,\n"
        f"    'env_has_token': {_TOKEN_ENV!r} in os.environ,\n"
        "}))\n"
        f"subprocess.run([sys.executable, '-c', {child_code!r}, {str(child_marker)!r}],"
        " check=True)\n"
        "import time\n"
        "time.sleep(1)\n"
    )
    executable = _fake_codex(
        tmp_path / "codex-boundary",
        handlers=_DELIVERY_TURN,
        prologue=prologue,
        prompt_capture=prompt_out,
    )
    _write_verified_config(loop, codex_executable=executable)
    src_root = str(Path(codex_bridge.__file__).resolve().parents[2])
    monkeypatch.setenv("PYTHONPATH", src_root)
    scripts_dir = repository / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    shutil.copy(
        Path(codex_bridge.__file__).resolve().parents[3]
        / "scripts"
        / "agent_loop_notify.py",
        scripts_dir,
    )

    launched = kick_reviewer_notification(loop)
    assert launched["status"] == "launching"

    raw_token = ""
    pending = None
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if not raw_token:
            raw_token = _raw_delivery_token(loop)
        pending = loop.pending_review_notification()
        if pending is not None and pending["state"] == "delivered":
            break
        time.sleep(0.05)
    assert pending is not None and pending["state"] == "delivered"
    assert raw_token, "worker hand-off must keep the private token"

    app_marker_data = json.loads(app_marker.read_text(encoding="utf-8"))
    assert app_marker_data["env_has_token"] is False
    child_marker_data = json.loads(child_marker.read_text(encoding="utf-8"))
    assert child_marker_data["env_has_token"] is False
    for observed in (
        json.dumps(app_marker_data),
        prompt_out.read_text(encoding="utf-8"),
        Path(str(launched["log_path"])).read_text(encoding="utf-8"),
        (loop.mailbox / "state.json").read_text(encoding="utf-8"),
        str(pending["last_error"]),
    ):
        assert raw_token not in observed


def test_worker_launch_hands_token_by_environment_not_argv(
    repository: Path, tmp_path: Path, make_review_ready, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    executable = tmp_path / "codex"
    executable.write_text("placeholder\n", encoding="utf-8")
    _write_verified_config(loop, codex_executable=executable)
    launches: list[tuple[list[str], dict[str, object]]] = []

    class FakeProcess:
        pid = 4321

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        launches.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(codex_bridge.subprocess, "Popen", fake_popen)

    result = kick_reviewer_notification(loop)

    assert result["status"] == "launching"
    command, kwargs = launches[0]
    assert all(_TOKEN_ENV not in part for part in command)
    assert kwargs["env"] is not None
    assert kwargs["env"][_TOKEN_ENV]


def test_independently_generated_error_is_redacted_with_sentinel(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    sentinel = f"SENTINEL-{secrets.token_hex(8)}"
    executable = _fake_codex(
        tmp_path / "codex-sentinel",
        handlers=_DELIVERY_TURN,
        completed_turn=sentinel,
        completed_status="failed",
    )
    _write_verified_config(loop, codex_executable=executable)
    claimed = loop.claim_review_notification()
    assert claimed is not None

    result = codex_bridge.deliver_claimed_notification(
        loop,
        event_sha256=str(claimed["event_sha256"]),
        delivery_token=str(claimed["delivery_token"]),
    )

    assert result["status"] == "failed"
    pending = loop.pending_review_notification()
    assert pending is not None
    assert sentinel in str(pending["last_error"])
    assert codex_bridge._redact_token("boom raw-secret end", "raw-secret") == (
        "boom <redacted> end"
    )


# ---------------------------------------------------------------------------
# Gap 2: at most one live delivery per dedicated reviewer target.
# ---------------------------------------------------------------------------


def test_second_event_cannot_claim_while_target_is_live(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop, fingerprint, thread_id, claim_a = _two_events_on_one_target(
        repository, tmp_path, make_review_ready
    )

    assert (
        loop.claim_review_notification(
            config_fingerprint=fingerprint, thread_id=thread_id
        )
        is None
    )
    live = loop.live_target_attempt(
        config_fingerprint=fingerprint, thread_id=thread_id
    )
    assert live is not None
    assert live["event_sha256"] == claim_a["event_sha256"]
    pending = loop.pending_review_notification()
    assert pending is not None
    assert pending["state"] == "queued"
    assert pending["event_sha256"] != claim_a["event_sha256"]


def test_same_event_duplicate_claim_remains_a_no_op(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop, fingerprint, thread_id, claim_a = _two_events_on_one_target(
        repository, tmp_path, make_review_ready
    )

    duplicate = loop.claim_review_notification(
        event_sha256=str(claim_a["event_sha256"]),
        config_fingerprint=fingerprint,
        thread_id=thread_id,
    )

    assert duplicate is None
    live = loop.live_target_attempt(
        config_fingerprint=fingerprint, thread_id=thread_id
    )
    assert live is not None
    assert live["attempt_id"] == claim_a["attempt_id"]


def test_kick_reports_target_busy_and_launches_nothing(
    repository: Path,
    tmp_path: Path,
    make_review_ready,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, fingerprint, thread_id, claim_a = _two_events_on_one_target(
        repository, tmp_path, make_review_ready
    )
    launches: list[list[str]] = []
    monkeypatch.setattr(
        codex_bridge.subprocess,
        "Popen",
        lambda command, **kwargs: launches.append(command)
        or type("P", (), {"pid": 1})(),
    )

    result = kick_reviewer_notification(loop)

    assert result["status"] == "target_busy"
    assert result["launched"] is False
    assert result["live_event_sha256"] == claim_a["event_sha256"]
    assert launches == []
    pending = loop.pending_review_notification()
    assert pending is not None
    assert pending["state"] == "queued"


def test_finishing_live_attempt_releases_target_exactly_once(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop, fingerprint, thread_id, claim_a = _two_events_on_one_target(
        repository, tmp_path, make_review_ready
    )
    loop.finish_review_notification(
        event_sha256=str(claim_a["event_sha256"]),
        delivery_token=str(claim_a["delivery_token"]),
        delivered=True,
        turn_id="review-turn-a",
    )

    delivered = kick_reviewer_notification(loop, synchronous=True)

    assert delivered["status"] == "delivered"
    event_b = str(delivered["event_sha256"])
    with sqlite3.connect(loop.database) as connection:
        rows = connection.execute(
            """
            SELECT event_sha256, state, attempt_number
            FROM delivery_attempts ORDER BY created_at, attempt_id
            """
        ).fetchall()
    assert [(row[1], row[2]) for row in rows] == [("delivered", 1), ("delivered", 1)]
    assert {row[0] for row in rows} == {claim_a["event_sha256"], event_b}
    assert kick_reviewer_notification(loop)["status"] == "already_delivered"
    assert (
        loop.claim_review_notification(
            config_fingerprint=fingerprint, thread_id=thread_id
        )
        is None
    )


def test_stale_live_attempt_is_superseded_after_bounded_interval(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop, fingerprint, thread_id, claim_a = _two_events_on_one_target(
        repository, tmp_path, make_review_ready
    )
    with sqlite3.connect(loop.database) as connection:
        connection.execute(
            "UPDATE delivery_attempts SET updated_at = '2020-01-01T00:00:00Z'"
            " WHERE state = 'live'"
        )

    claimed_b = loop.claim_review_notification(
        config_fingerprint=fingerprint,
        thread_id=thread_id,
        stale_after_seconds=60,
    )

    assert claimed_b is not None
    with sqlite3.connect(loop.database) as connection:
        states = connection.execute(
            "SELECT state FROM delivery_attempts ORDER BY created_at, attempt_id"
        ).fetchall()
    assert [row[0] for row in states] == ["superseded", "live"]
    loop.finish_review_notification(
        event_sha256=str(claimed_b["event_sha256"]),
        delivery_token=str(claimed_b["delivery_token"]),
        delivered=True,
        turn_id="review-turn-b",
    )
    assert (
        loop.claim_review_notification(
            config_fingerprint=fingerprint, thread_id=thread_id
        )
        is None
    )


# ---------------------------------------------------------------------------
# Gap 3: busy on the verified target is transient; no rollout is configuration.
# ---------------------------------------------------------------------------


def test_active_writer_on_verified_target_is_transient_overlap(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    executable = _fake_codex(tmp_path / "codex-busy", handlers=_ACTIVE_WRITER)
    payload = _write_verified_config(loop, codex_executable=executable)
    fingerprint = str(payload["config_fingerprint"])

    first = kick_reviewer_notification(loop, synchronous=True)

    assert first["status"] == "failed"
    assert first["classification"] == "transient"
    pending = loop.pending_review_notification()
    assert pending is not None
    assert pending["retry_class"] == "transient"
    assert pending["blocked_fingerprint"] == ""
    assert pending["blocked_fingerprint"] != fingerprint
    assert pending["next_retry_at"] != ""

    second = kick_reviewer_notification(loop, synchronous=True)

    assert second["status"] == "backoff"
    assert second["launched"] is False
    assert loop.doctor()["healthy"] is True


def test_no_rollout_during_delivery_still_blocks_configuration(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    executable = _fake_codex(tmp_path / "codex-norollout", handlers=_NO_ROLLOUT_RESUME)
    payload = _write_verified_config(loop, codex_executable=executable)
    fingerprint = str(payload["config_fingerprint"])

    first = kick_reviewer_notification(loop, synchronous=True)

    assert first["status"] == "failed"
    assert first["classification"] == "configuration"
    pending = loop.pending_review_notification()
    assert pending is not None
    assert pending["retry_class"] == "configuration"
    assert pending["blocked_fingerprint"] == fingerprint
    assert pending["next_retry_at"] == ""

    second = kick_reviewer_notification(loop, synchronous=True)

    assert second["status"] == "configuration_blocked"
    assert second["launched"] is False


def test_bootstrap_rejects_foreign_active_writer_target(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    executable = _fake_codex(tmp_path / "codex-foreign", handlers=_ACTIVE_WRITER)

    with pytest.raises(CodexBridgeError, match="bootstrap failed"):
        bootstrap_codex_reviewer(
            loop, codex_executable=executable, bootstrap_timeout_seconds=120
        )

    assert not bridge_config_path(loop).exists()
    record = codex_bridge.read_bootstrap_record(loop)
    assert record is not None
    assert record["status"] == "incomplete"
    assert record["wrote_config"] is False


# ---------------------------------------------------------------------------
# Gap 4: only identity-matched, persistence-verified configs are admitted.
# ---------------------------------------------------------------------------


def test_compose_rejects_anything_but_verified_probe_status(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    executable = tmp_path / "codex"
    executable.write_text("placeholder\n", encoding="utf-8")

    with pytest.raises(CodexBridgeError, match="verified"):
        compose_bridge_config(
            loop,
            thread_id=_THREAD,
            codex_executable=executable,
            bootstrapped_at=_utc_now(),
            probe_status="unverified",
            probe_evidence=_verified_evidence(),
        )


def test_load_rejects_unverified_probe_status(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    executable = tmp_path / "codex"
    executable.write_text("placeholder\n", encoding="utf-8")
    payload = _write_verified_config(loop, codex_executable=executable)
    payload["probe_status"] = "unverified"
    bridge_config_path(loop).write_text(
        json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(CodexBridgeError, match="verified"):
        codex_bridge.load_bridge_config(loop)


@pytest.mark.parametrize(
    "evidence, fragment",
    [
        ({}, "bootstrap turn id"),
        ({"bootstrap_turn_id": "boot-turn-1"}, "persistence"),
        (
            {
                "bootstrap_turn_id": "boot-turn-1",
                "persistence": {
                    "bootstrap_turn_completed": True,
                    "creator_process_exited": True,
                    "thread_read_after_restart": False,
                    "thread_resume_after_restart": True,
                },
            },
            "persistence",
        ),
        (
            {
                "bootstrap_turn_id": "boot-turn-1",
                "persistence": {
                    "bootstrap_turn_completed": True,
                    "creator_process_exited": True,
                    "thread_read_after_restart": True,
                },
            },
            "persistence",
        ),
    ],
)
def test_load_rejects_incomplete_persistence_evidence(
    repository: Path, tmp_path: Path, evidence: dict, fragment: str
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    executable = tmp_path / "codex"
    executable.write_text("placeholder\n", encoding="utf-8")
    payload = _write_verified_config(loop, codex_executable=executable)
    payload["probe_evidence"] = evidence
    bridge_config_path(loop).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CodexBridgeError, match=fragment):
        codex_bridge.load_bridge_config(loop)


@pytest.mark.parametrize("mismatch", ["read", "resume"])
def test_bootstrap_rejects_identity_mismatched_thread_responses(
    repository: Path, tmp_path: Path, mismatch: str
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    foreign_id = _THREAD_B
    handlers = _IDENTITY_PROOF.replace(
        "@READ_ID@", repr(foreign_id if mismatch == "read" else _THREAD)
    ).replace("@RESUME_ID@", repr(foreign_id if mismatch == "resume" else _THREAD))
    executable = _fake_codex(tmp_path / f"codex-identity-{mismatch}", handlers=handlers)

    with pytest.raises(CodexBridgeError, match="identity"):
        bootstrap_codex_reviewer(
            loop, codex_executable=executable, bootstrap_timeout_seconds=120
        )

    assert not bridge_config_path(loop).exists()
    record = codex_bridge.read_bootstrap_record(loop)
    assert record is not None
    assert record["status"] == "incomplete"


def test_load_still_admits_a_fully_verified_bootstrap(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    executable = _fake_codex(
        tmp_path / "codex-good",
        handlers=_THREE_PHASE,
        thread_id=_THREAD_B,
    )
    bootstrap_codex_reviewer(
        loop,
        codex_executable=executable,
        bootstrap_timeout_seconds=120,
    )

    config = codex_bridge.load_bridge_config(loop)

    assert config is not None
    assert config.thread_id == _THREAD_B
    assert config.probe_status == "verified"


# ---------------------------------------------------------------------------
# Part C: migration and doctor enforcement of the target-level invariant.
# ---------------------------------------------------------------------------


def test_migration_fails_closed_on_ambiguous_live_history(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    fingerprint = "f" * 64
    claimed = loop.claim_review_notification(
        config_fingerprint=fingerprint, thread_id=_THREAD
    )
    assert claimed is not None
    with sqlite3.connect(loop.database) as connection:
        event_row = connection.execute(
            "SELECT event_sha256 FROM events WHERE action = 'task_published'"
        ).fetchone()
    foreign_event = str(event_row[0])
    with sqlite3.connect(loop.database) as connection:
        connection.execute("DROP INDEX IF EXISTS one_live_delivery_attempt_per_target")
        connection.execute(
            """
            INSERT INTO delivery_attempts(
                attempt_id, event_sha256, generation, event_action,
                config_fingerprint, thread_id, attempt_number,
                delivery_token_sha256, state, created_at, updated_at
            ) VALUES ('tampered-second', ?, 1, 'task_published', ?, ?, 1,
                      'aa', 'live', '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')
            """,
            (foreign_event, fingerprint, _THREAD),
        )

    with pytest.raises(AgentLoopError, match="ambiguous live delivery history"):
        loop.status(role="reviewer")

    with sqlite3.connect(loop.database) as connection:
        rows = connection.execute(
            "SELECT attempt_id, state FROM delivery_attempts ORDER BY attempt_id"
        ).fetchall()
    assert len(rows) == 2
    assert all(row[1] == "live" for row in rows)


def test_doctor_rejects_unbound_launching_notification(
    repository: Path, tmp_path: Path, make_review_ready
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    make_review_ready(loop, tmp_path)
    claimed = loop.claim_review_notification(
        config_fingerprint="f" * 64, thread_id=_THREAD
    )
    assert claimed is not None

    with sqlite3.connect(loop.database) as connection:
        connection.execute(
            "UPDATE delivery_attempts SET delivery_token_sha256 = ?"
            " WHERE state = 'live'",
            ("0" * 64,),
        )
    with pytest.raises(
        AgentLoopError, match="does not match its live delivery attempt"
    ):
        loop.doctor()

    token_hash = hashlib.sha256(
        str(claimed["delivery_token"]).encode("utf-8")
    ).hexdigest()
    with sqlite3.connect(loop.database) as connection:
        connection.execute(
            "UPDATE delivery_attempts SET delivery_token_sha256 = ?"
            " WHERE state = 'live'",
            (token_hash,),
        )
        connection.execute(
            "UPDATE review_notifications SET config_fingerprint = 'eeee'"
        )
    with pytest.raises(AgentLoopError, match="not bound to its notification"):
        loop.doctor()

    with sqlite3.connect(loop.database) as connection:
        connection.execute(
            "UPDATE review_notifications SET config_fingerprint = ?",
            (str(claimed["config_fingerprint"]),),
        )
        connection.execute(
            "UPDATE delivery_attempts SET state = 'superseded'"
            " WHERE state = 'live'"
        )
    with pytest.raises(AgentLoopError, match="no unique live delivery attempt"):
        loop.doctor()
