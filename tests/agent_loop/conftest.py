"""Shared fixtures for agent-loop protocol and bridge tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from quantlab.agent_loop import AgentLoop


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


@pytest.fixture
def make_review_ready():
    """Drive one full task -> claim -> commit -> push -> report cycle."""

    def _make(loop: AgentLoop, tmp_path: Path) -> None:
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
        loop.submit_report(
            report, claim_token=str(claim["claim_token"]), title="Report"
        )

    return _make
