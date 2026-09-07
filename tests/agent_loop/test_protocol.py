from __future__ import annotations

import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from quantlab.agent_loop import AgentLoop, AgentLoopError


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "master", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "Agent Loop Test")
    _git(repo, "config", "user.email", "agent-loop@example.invalid")
    (repo / ".gitignore").write_text(".agent-loop/\n", encoding="utf-8")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "master")
    return repo


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _publish_and_claim(loop: AgentLoop, tmp_path: Path) -> tuple[str, str]:
    task = _write(tmp_path / "task.md", "# Task\n\nImplement the bounded change.\n")
    published = loop.publish_task(task, title="Bounded task")
    claimed = loop.claim_task(agent="zcode", lease_hours=1)
    assert published["action"] == "claim_task"
    return claimed["claim_token"], published["expected_head"]


def _commit_and_push(repo: Path, message: str = "implementation") -> str:
    target = repo / "implementation.txt"
    target.write_text(target.read_text(encoding="utf-8") + "x\n" if target.exists() else "x\n")
    _git(repo, "add", "implementation.txt")
    _git(repo, "commit", "-m", message)
    _git(repo, "push")
    return _git(repo, "rev-parse", "HEAD")


def test_full_advance_cycle_is_head_bound_and_append_only(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    initialized = loop.initialize()
    assert initialized["phase"] == "IDLE"

    claim_token, starting_head = _publish_and_claim(loop, tmp_path)
    final_head = _commit_and_push(repository)
    report_file = _write(tmp_path / "report.md", "# Report\n\nTests passed.\n")
    report = loop.submit_report(
        report_file, claim_token=claim_token, title="Executor report"
    )

    assert report["phase"] == "REVIEW_READY"
    assert report["artifacts"]["report"]["git_head"] == final_head
    assert report["expected_head"] == starting_head

    review_file = _write(tmp_path / "review.md", "# Review\n\nAccepted.\n")
    next_task = _write(tmp_path / "next.md", "# Next\n\nContinue safely.\n")
    advanced = loop.submit_review(
        review_file,
        decision="advance",
        title="Review 1",
        next_task_file=next_task,
        next_task_title="Next task",
    )

    assert advanced["phase"] == "TASK_READY"
    assert advanced["generation"] == 2
    assert advanced["expected_head"] == final_head
    assert loop.artifact_content("task", 1).startswith("# Task")
    assert loop.artifact_content("report", 1).startswith("# Report")
    assert loop.artifact_content("review", 1).startswith("# Review")
    assert loop.artifact_content("task", 2).startswith("# Next")
    assert loop.doctor()["healthy"] is True


def test_two_executors_cannot_claim_the_same_task(repository: Path, tmp_path: Path) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    task = _write(tmp_path / "task.md", "one task\n")
    loop.publish_task(task, title="One task")

    def claim(agent: str) -> str:
        return AgentLoop(repository).claim_task(agent=agent)["claim_token"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim, name) for name in ("zcode-a", "zcode-b")]
    successes = [future.result() for future in futures if future.exception() is None]
    failures = [future.exception() for future in futures if future.exception() is not None]

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], AgentLoopError)
    assert loop.status(role="reviewer")["phase"] == "EXECUTING"


def test_wrong_claim_token_cannot_mutate_state(repository: Path, tmp_path: Path) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    _publish_and_claim(loop, tmp_path)
    _commit_and_push(repository)
    report = _write(tmp_path / "report.md", "report\n")
    before = loop.status(role="reviewer")

    with pytest.raises(AgentLoopError, match="claim token mismatch"):
        loop.submit_report(report, claim_token="wrong", title="Bad report")

    after = loop.status(role="reviewer")
    assert after["phase"] == "EXECUTING"
    assert after["event_count"] == before["event_count"]


def test_report_requires_commit_and_push(repository: Path, tmp_path: Path) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    token, _ = _publish_and_claim(loop, tmp_path)
    report = _write(tmp_path / "report.md", "report\n")

    with pytest.raises(AgentLoopError, match="produced no commit"):
        loop.submit_report(report, claim_token=token, title="No commit")

    target = repository / "implementation.txt"
    target.write_text("local only\n", encoding="utf-8")
    _git(repository, "add", "implementation.txt")
    _git(repository, "commit", "-m", "not pushed")
    with pytest.raises(AgentLoopError, match="HEAD equals its upstream"):
        loop.submit_report(report, claim_token=token, title="Not pushed")

    _git(repository, "push")
    assert loop.submit_report(report, claim_token=token, title="Pushed")["phase"] == "REVIEW_READY"


def test_task_publish_rejects_dirty_or_unpushed_repository(
    repository: Path, tmp_path: Path
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    task = _write(tmp_path / "task.md", "task\n")
    (repository / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(AgentLoopError, match="dirty git workspace"):
        loop.publish_task(task, title="Dirty")

    (repository / "dirty.txt").unlink()
    (repository / "README.md").write_text("changed\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "local commit")
    with pytest.raises(AgentLoopError, match="HEAD equals its upstream"):
        loop.publish_task(task, title="Unpushed")


def test_projection_tampering_is_repaired_from_database(repository: Path, tmp_path: Path) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    task = _write(tmp_path / "task.md", "authoritative task\n")
    loop.publish_task(task, title="Task")
    projection = repository / ".agent-loop" / "tasks" / "000001.md"
    projection.write_text("tampered\n", encoding="utf-8")
    state_projection = repository / ".agent-loop" / "state.json"
    state_projection.write_text("{}\n", encoding="utf-8")

    loop.status(role="executor")

    assert projection.read_text(encoding="utf-8") == "authoritative task\n"
    assert json.loads(state_projection.read_text(encoding="utf-8"))["phase"] == "TASK_READY"


def test_database_artifact_tampering_fails_closed(repository: Path, tmp_path: Path) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    task = _write(tmp_path / "task.md", "task\n")
    loop.publish_task(task, title="Task")
    with sqlite3.connect(loop.database) as connection:
        connection.execute(
            "UPDATE artifacts SET content = 'tampered' WHERE kind = 'task' AND generation = 1"
        )

    with pytest.raises(AgentLoopError, match="artifact hash mismatch"):
        loop.status(role="executor")


def test_event_chain_tampering_fails_closed(repository: Path) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    with sqlite3.connect(loop.database) as connection:
        connection.execute("UPDATE events SET actor = 'forged' WHERE sequence = 1")

    with pytest.raises(AgentLoopError, match="event hash mismatch"):
        loop.doctor()


@pytest.mark.parametrize("decision", ["advance", "rework"])
def test_nonterminal_review_requires_next_task(
    repository: Path, tmp_path: Path, decision: str
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    token, _ = _publish_and_claim(loop, tmp_path)
    _commit_and_push(repository)
    report = _write(tmp_path / "report.md", "report\n")
    loop.submit_report(report, claim_token=token, title="Report")
    review = _write(tmp_path / "review.md", "review\n")

    with pytest.raises(AgentLoopError, match="require --next-task-file"):
        loop.submit_review(review, decision=decision, title="Review")  # type: ignore[arg-type]


@pytest.mark.parametrize("decision, expected", [("complete", "COMPLETE"), ("blocked", "BLOCKED")])
def test_terminal_review_has_no_next_task(
    repository: Path, tmp_path: Path, decision: str, expected: str
) -> None:
    loop = AgentLoop(repository)
    loop.initialize()
    token, _ = _publish_and_claim(loop, tmp_path)
    _commit_and_push(repository)
    report = _write(tmp_path / "report.md", "report\n")
    loop.submit_report(report, claim_token=token, title="Report")
    review = _write(tmp_path / "review.md", "review\n")

    result = loop.submit_review(
        review,
        decision=decision,  # type: ignore[arg-type]
        title="Terminal review",
    )

    assert result["phase"] == expected
    assert result["action"] == "noop"
