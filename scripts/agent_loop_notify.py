#!/usr/bin/env python3
"""Internal detached worker for one claimed Codex reviewer notification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from quantlab.agent_loop import AgentLoop, AgentLoopError
from quantlab.agent_loop.codex_bridge import deliver_claimed_notification


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--mailbox", type=Path, required=True)
    parser.add_argument("--event-sha256", required=True)
    parser.add_argument("--delivery-token", required=True)
    args = parser.parse_args()
    try:
        result = deliver_claimed_notification(
            AgentLoop(args.repo, args.mailbox),
            event_sha256=args.event_sha256,
            delivery_token=args.delivery_token,
        )
    except AgentLoopError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("delivered") else 2


if __name__ == "__main__":
    raise SystemExit(main())
