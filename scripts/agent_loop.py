#!/usr/bin/env python3
"""Command-line interface for the local ZCode/Codex coordination mailbox."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from quantlab.agent_loop import AgentLoop, AgentLoopError
from quantlab.agent_loop.protocol import discover_repo_root


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, help="repository root (auto-detected by default)")
    parser.add_argument("--mailbox", type=Path, help="mailbox path (default: <repo>/.agent-loop)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="initialize the local mailbox")
    init.add_argument("--project-id", default="quantlab")

    status = subparsers.add_parser("status", help="validate and show current state")
    status.add_argument("--role", choices=("executor", "reviewer"), default="reviewer")

    subparsers.add_parser("doctor", help="verify database, artifacts, events, and git state")

    publish = subparsers.add_parser("publish-task", help="publish the first/resumed task")
    publish.add_argument("--task-file", type=Path, required=True)
    publish.add_argument("--title", required=True)
    publish.add_argument("--actor", default="codex-reviewer")
    publish.add_argument("--expected-head")
    publish.add_argument("--resume-blocked", action="store_true")

    claim = subparsers.add_parser("claim", help="atomically claim a ready task")
    claim.add_argument("--agent", required=True)
    claim.add_argument("--lease-hours", type=int, default=24)

    report = subparsers.add_parser("submit-report", help="submit a pushed implementation report")
    report.add_argument("--report-file", type=Path, required=True)
    report.add_argument("--claim-token", required=True)
    report.add_argument("--title", required=True)
    report.add_argument("--allow-no-commit", action="store_true")

    block = subparsers.add_parser("block", help="publish an executor blocker")
    block.add_argument("--blocker-file", type=Path, required=True)
    block.add_argument("--claim-token", required=True)
    block.add_argument("--title", required=True)

    review = subparsers.add_parser("submit-review", help="record review and optionally next task")
    review.add_argument("--review-file", type=Path, required=True)
    review.add_argument(
        "--decision",
        choices=tuple(sorted({"advance", "rework", "blocked", "complete"})),
        required=True,
    )
    review.add_argument("--title", required=True)
    review.add_argument("--next-task-file", type=Path)
    review.add_argument("--next-task-title")
    review.add_argument("--actor", default="codex-reviewer")

    expire = subparsers.add_parser("expire-claim", help="block an expired executor lease")
    expire.add_argument("--reason", required=True)
    expire.add_argument("--actor", default="codex-reviewer")

    requeue = subparsers.add_parser(
        "requeue-abandoned", help="abandon a known dead claim and requeue immutably"
    )
    requeue.add_argument("--reason", required=True)
    requeue.add_argument("--expected-event-sha256", required=True)
    requeue.add_argument("--expected-claim-agent", required=True)
    requeue.add_argument("--actor", default="human-recovery")

    show = subparsers.add_parser("show", help="print an immutable artifact")
    show.add_argument("kind", choices=("task", "report", "review"))
    show.add_argument("--generation", type=int)
    return parser


def _loop(args: argparse.Namespace) -> AgentLoop:
    repo_root = (args.repo or discover_repo_root()).resolve()
    mailbox = args.mailbox.resolve() if args.mailbox else None
    return AgentLoop(repo_root, mailbox)


def _print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main() -> int:
    args = _parser().parse_args()
    try:
        loop = _loop(args)
        if args.command == "init":
            result = loop.initialize(project_id=args.project_id)
        elif args.command == "status":
            result = loop.status(role=args.role)
        elif args.command == "doctor":
            result = loop.doctor()
        elif args.command == "publish-task":
            result = loop.publish_task(
                args.task_file,
                title=args.title,
                actor=args.actor,
                expected_head=args.expected_head,
                resume_blocked=args.resume_blocked,
            )
        elif args.command == "claim":
            result = loop.claim_task(agent=args.agent, lease_hours=args.lease_hours)
        elif args.command == "submit-report":
            result = loop.submit_report(
                args.report_file,
                claim_token=args.claim_token,
                title=args.title,
                require_commit=not args.allow_no_commit,
            )
        elif args.command == "block":
            result = loop.block_execution(
                args.blocker_file,
                claim_token=args.claim_token,
                title=args.title,
            )
        elif args.command == "submit-review":
            result = loop.submit_review(
                args.review_file,
                decision=args.decision,
                title=args.title,
                next_task_file=args.next_task_file,
                next_task_title=args.next_task_title,
                actor=args.actor,
            )
        elif args.command == "expire-claim":
            result = loop.mark_expired_claim_blocked(actor=args.actor, reason=args.reason)
        elif args.command == "requeue-abandoned":
            result = loop.requeue_abandoned_claim(
                reason=args.reason,
                expected_event_sha256=args.expected_event_sha256,
                expected_claim_agent=args.expected_claim_agent,
                actor=args.actor,
            )
        elif args.command == "show":
            print(loop.artifact_content(args.kind, args.generation), end="")
            return 0
        else:  # pragma: no cover - argparse enforces this
            raise AssertionError(args.command)
        _print_json(result)
        return 0
    except AgentLoopError as exc:
        print(f"AGENT_LOOP_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
