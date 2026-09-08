#!/usr/bin/env python3
"""Internal detached worker for one claimed Codex reviewer notification.

The delivery token is handed over only through the
``QUANTLAB_AGENT_LOOP_DELIVERY_TOKEN`` environment variable; it never appears
in argv, logs, or projections.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from quantlab.agent_loop import AgentLoop, AgentLoopError
from quantlab.agent_loop.codex_bridge import (
    DELIVERY_TOKEN_ENV,
    _redact_token,
    deliver_claimed_notification,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--mailbox", type=Path, required=True)
    parser.add_argument("--event-sha256", required=True)
    args = parser.parse_args()
    token = os.environ.get(DELIVERY_TOKEN_ENV, "")
    if not token:
        print(
            json.dumps(
                {"status": "failed", "error": "delivery token missing from worker environment"}
            )
        )
        return 2
    try:
        result = deliver_claimed_notification(
            AgentLoop(args.repo, args.mailbox),
            event_sha256=args.event_sha256,
            delivery_token=token,
        )
    except AgentLoopError as exc:
        print(json.dumps({"status": "failed", "error": _redact_token(str(exc), token)}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("delivered") else 2


if __name__ == "__main__":
    sys.exit(main())
