"""Independent raw calendar enumeration, no production calendar parser or window helper."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from quantlab.research.alpha158_store import atomic_seal, exclusive_job
from quantlab.research.round2_dataset import sealed_read, verify_entries
from quantlab.research.s2_calendar import CONFIG, OBSERVATION, OUTPUT, PROOF


def main():
    root = Path(__file__).resolve().parents[1]
    c = json.loads((root / CONFIG).read_text())
    out = root / OUTPUT
    report = sealed_read(out / "calendar_plan.json")
    verify_entries(root, c["inputs"])
    verify_entries(out, report["raw_files"])
    assert report["observation_fingerprint"] == OBSERVATION
    assert report["original_proof_fingerprint"] == PROOF
    required = {(date(2026, 9, 11) + timedelta(days=n)).strftime("%Y%m%d") for n in range(51)}
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        atomic_seal(
            out / "proof_started.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "report_fingerprint": report["fingerprint"],
                "proof_attempt": 1,
            },
        )
        calendars = []
        for exchange in ("SSE", "SZSE"):
            d = json.loads((out / "attempts" / exchange / "response.body").read_bytes())["data"]
            rows = [dict(zip(d["fields"], row, strict=True)) for row in d["items"]]
            assert len(rows) == 51 and {r["cal_date"] for r in rows} == required
            assert all(
                r["exchange"] == exchange
                and type(r["is_open"]) in (int, str)
                and r["is_open"] in (0, 1, "0", "1")
                for r in rows
            )
            calendars.append(sorted(r["cal_date"] for r in rows if str(r["is_open"]) == "1"))
        assert calendars[0] == calendars[1]
        index = calendars[0].index("20260914")
        window = [
            datetime.strptime(s, "%Y%m%d").date().isoformat()
            for s in calendars[0][index : index + 21]
        ]
        assert len(window) == 21 and report["planned_label_dates"] == window
        assert report["planned_entry"] == window[0] and report["planned_end"] == window[-1]
        assert report["subsequent_sessions"] == 20
        for key in (
            "future_actual_openings_certified",
            "future_labels_evaluated",
            "execution_authority",
            "performance_evidence",
        ):
            assert report[key] is False
        assert report["requires_calendar_revalidation_at_evaluation"] is True
        result = atomic_seal(
            out / "proof.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "report_fingerprint": report["fingerprint"],
                "all_checks_passed": True,
                "source_rows_checked": 102,
                "planned_end": window[-1],
                "future_labels_evaluated": False,
                "execution_authority": False,
            },
        )
        print(json.dumps(result))


if __name__ == "__main__":
    main()
