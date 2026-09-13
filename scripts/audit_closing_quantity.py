"""One actual 726-session x four-board lookup sweep; zero economic paths."""

import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.execution.models import OrderSession
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.closing_quantity import (
    CONFIG,
    MANIFEST,
    OUTPUT,
    fixed_sessions,
    load_catalogue,
)
from quantlab.research.cohort_dividends import pushed_head
from quantlab.research.input_audit import _sha
from quantlab.research.rule_evidence import SCOPES, values


def payload_fingerprint(rule):
    return canonical_payload_fingerprint(json.loads(json.dumps(asdict(rule), default=str)))


def main():
    root = Path(__file__).resolve().parents[1]
    catalogue = load_catalogue(root)
    days, head = fixed_sessions(root, catalogue), pushed_head(root)
    config_sha, manifest_sha = _sha(root / CONFIG), _sha(root / MANIFEST)
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        budget = Budget(out, catalogue.contract["resources"])
        budget.check(projected_bytes=4 * 1024**2)
        atomic_seal(
            out / "started.json",
            {"at": datetime.now(UTC).isoformat(), "source_head": head, "actual_attempt": 1},
        )
        with budget.watchdog():
            rows = []
            for day in days:
                for exchange, board in SCOPES:
                    old = catalogue.parent.resolve(exchange, board, day)
                    delegated = catalogue.resolve(exchange, board, day)
                    if asdict(old) != asdict(delegated):
                        raise DataValidationError("continuous delegation changed")
                    new = catalogue.resolve(
                        exchange, board, day, session=OrderSession.CLOSING_AUCTION
                    )
                    if new is None:
                        raise DataValidationError("reviewed closing subset unexpectedly unknown")
                    rows.append(
                        {
                            "date": day.isoformat(),
                            "exchange": exchange,
                            "board": board,
                            "parent_id": old.rule_id,
                            "closing_id": new.rule_id,
                            "values": values(new),
                            "effective_from": new.effective_from.isoformat(),
                            "effective_to": new.effective_to.isoformat(),
                            "parent_payload": payload_fingerprint(old),
                            "closing_payload": payload_fingerprint(new),
                        }
                    )
            load_catalogue(root)
            if pushed_head(root) != head or (_sha(root / CONFIG), _sha(root / MANIFEST)) != (
                config_sha,
                manifest_sha,
            ):
                raise DataValidationError("source changed during lookup sweep")
            report = atomic_seal(
                out / "report.json",
                {
                    "at": datetime.now(UTC).isoformat(),
                    "source_head": head,
                    "config_sha256": config_sha,
                    "manifest_sha256": manifest_sha,
                    "sessions": len(days),
                    "rows": rows,
                    "covered": len(rows),
                    "unchanged_continuous_payloads": len(rows),
                    "provider_requests": 0,
                    "economic_paths": 0,
                    "model_fits": 0,
                    "execution_authority": False,
                    "performance_evidence": False,
                    "limitations": catalogue.contract["limitations"],
                    "resources": {
                        "seconds": time.monotonic() - budget.started,
                        "peak_rss_bytes": peak_rss_bytes(),
                        "bytes_before_report": budget.check(projected_bytes=4 * 1024**2),
                    },
                },
            )
            budget.check()
            print(json.dumps({k: report[k] for k in ("fingerprint", "covered", "resources")}))


if __name__ == "__main__":
    main()
