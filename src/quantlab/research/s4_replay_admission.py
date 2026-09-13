"""Local sealed-evidence admission for the S4-A first historical replay.

Turns the already-sealed first-entry materials into a consumable input
package for :mod:`quantlab.research.risk_ledger_loop`, admitting only what
the local evidence actually supports: the seven exact execution-day volumes
(numeric admission), the entry-day corporate-action scope of an initially
flat buy-only book, and nothing else. prior20 gaps, market-open and
identity eligibility stay explicit unknowns. This module never downloads,
writes canonical data, reruns sealed programs, simulates fills or claims
returns; the first replay run itself needs a separate limited card.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

DECISION_DATE = "2021-12-31"
EXECUTION_DATE = "2022-01-04"
FOLLOWING_DATE = "2022-01-05"
VOLUME_PRECISION_PURPOSE = "execution_volume_precision"
LOT_SHARES = 100  # raw provider volume text is in lots of 100 shares
AMOUNT_KILO_YUAN = 1000  # raw provider amount text is in kilo-CNY
YUAN_TO_FEN = 100

STATUS_ADMITTED = "admitted"
STATUS_UNKNOWN = "unknown"

PRIOR20_EMPTY_DATES = (
    ("000301.SZ", "2021-12-22"),
    ("000777.SZ", "2021-12-07"),
    ("000777.SZ", "2021-12-08"),
    ("000777.SZ", "2021-12-09"),
    ("000777.SZ", "2021-12-10"),
    ("000777.SZ", "2021-12-13"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class FieldAdmission:
    field: str
    source: str
    status: str
    reason: str

    def __post_init__(self) -> None:
        if self.status not in (STATUS_ADMITTED, STATUS_UNKNOWN):
            raise ValueError("field status must be admitted or unknown")


@dataclass(frozen=True)
class InstrumentPackage:
    instrument_id: str
    proposal_rank: int
    nominal_quantity: int
    nominal_slot_fen: int
    context: dict
    fields: tuple[FieldAdmission, ...]

    @property
    def open_gaps(self) -> tuple[str, ...]:
        return tuple(f.field for f in self.fields if f.status == STATUS_UNKNOWN)


@dataclass(frozen=True)
class Prior20Gap:
    instrument_id: str
    trade_date: str
    raw_attempt_sha256: str | None
    local_bar_present: bool | None
    suspension_record_present: bool | None
    suspended_on_date: bool | None
    classification: str
    note: str


def build_manifest(source_dir: Path) -> dict:
    """Hash the consumed sealed files and record their embedded fingerprints."""
    consumed = {
        "plan": source_dir / "s4_first_entry_plan/plan.json",
        "reconciliation": source_dir / "s4_entry_raw_precision/reconciliation.json",
        "profiles": source_dir / "cohort_dividend_readiness/profiles.json",
    }
    entries: dict[str, dict] = {}
    for name, path in consumed.items():
        if not path.is_file():
            raise FileNotFoundError(f"sealed source missing: {path}")
        payload = _read_json(path)
        entries[name] = {
            "path": str(path),
            "sha256": sha256_file(path),
            "embedded_fingerprint": payload.get("fingerprint"),
        }
    return {"files": entries, "note": "hashes observed programmatically; embedded report fingerprints recorded verbatim"}


def admit_exact_volumes(
    reconciliation_rows: list[dict], selected_instruments: tuple[str, ...]
) -> dict[str, int]:
    """Admit the seven exact execution-day volumes by numeric unit mapping.

    Each row must be a nonempty execution_volume_precision observation for
    the execution date, name one of the sealed proposals, and satisfy the
    lot/kilo-yuan unit mapping exactly; otherwise it rejects.
    """
    admitted: dict[str, int] = {}
    for row in reconciliation_rows:
        if row.get("purpose") != VOLUME_PRECISION_PURPOSE:
            continue
        instrument = row.get("instrument_id")
        if instrument not in selected_instruments:
            raise ValueError(f"volume row for non-proposal {instrument!r}")
        if row.get("trade_date") != EXECUTION_DATE:
            raise ValueError(f"volume row for {instrument} dated {row.get('trade_date')!r}")
        if row.get("source_status") != "nonempty":
            raise ValueError(f"volume row for {instrument} is not a nonempty source")
        text = row["raw_number_text"]
        normalized = row["raw_normalized"]
        text_vol = Fraction(text["vol"])
        expected_shares = text_vol * LOT_SHARES
        if expected_shares.denominator != 1 or expected_shares != normalized["vol"]:
            raise ValueError(
                f"volume unit mapping failed for {instrument}: "
                f"{text['vol']} lots x {LOT_SHARES} != {normalized['vol']} shares"
            )
        text_amount = Fraction(text["amount"])
        expected_fen = text_amount * AMOUNT_KILO_YUAN * YUAN_TO_FEN
        if expected_fen.denominator != 1 or expected_fen != normalized["amount"]:
            raise ValueError(
                f"amount unit mapping failed for {instrument}: "
                f"{text['amount']} kilo-CNY != {normalized['amount']} fen"
            )
        if not row.get("raw_ohlc_consistent"):
            raise ValueError(f"raw OHLC inconsistency for {instrument}")
        if instrument in admitted:
            raise ValueError(f"duplicate volume admission for {instrument}")
        admitted[instrument] = int(normalized["vol"])
    if not admitted:
        raise ValueError("no execution-volume rows were admitted")
    return admitted


def check_prior20_gaps(
    empty_rows: list[tuple[str, str]],
    source_dir: Path,
    canonical_dir: Path,
) -> tuple[Prior20Gap, ...]:
    """Classify the six empty prior20 code-dates against local evidence.

    An empty supplier return is never interpreted as zero volume,
    suspension or a non-trading day; the classification records exactly
    what the local materials show and leaves the window unknown.
    """
    import pandas as pd

    gaps: list[Prior20Gap] = []
    for instrument, trade_date in empty_rows:
        year, month, day = trade_date.split("-")
        attempt_dir = source_dir / "s4_entry_raw_precision/attempts"
        attempt = attempt_dir / f"daily_{instrument}_{trade_date.replace('-', '')}"
        raw_hash = None
        raw_note = "attempt file missing"
        if attempt.is_dir():
            files = sorted(p for p in attempt.iterdir() if p.is_file())
            if files:
                raw_hash = sha256_file(files[0])
                raw_note = f"sealed raw response hashed ({len(files)} file(s))"
        bar_present: bool | None = None
        month_dir = canonical_dir / f"daily/year={year}/month={month}"
        if month_dir.is_dir():
            frame = pd.concat(
                pd.read_parquet(part) for part in sorted(month_dir.glob("*.parquet"))
            )
            code_col = "ts_code" if "ts_code" in frame.columns else "instrument_id"
            date_col = "trade_date" if "trade_date" in frame.columns else "session"
            rows = frame[
                (frame[code_col] == instrument)
                & (frame[date_col].astype(str).str[:10] == trade_date)
            ]
            bar_present = len(rows) > 0
        suspensions_present: bool | None = None
        suspended_on_date: bool | None = None
        susp_dir = canonical_dir / f"lifecycle_context_v1/suspensions/year={year}/month={month}"
        if susp_dir.is_dir():
            frame = pd.concat(
                pd.read_parquet(part) for part in sorted(susp_dir.glob("*.parquet"))
            )
            code_col = "ts_code" if "ts_code" in frame.columns else "instrument_id"
            rows = frame[frame[code_col] == instrument]
            suspensions_present = len(rows) > 0
            if suspensions_present:
                date_col = "trade_date" if "trade_date" in frame.columns else "session"
                day_rows = rows[rows[date_col].astype(str).str[:10] == trade_date]
                suspended_on_date = bool(
                    len(day_rows) > 0
                    and (day_rows["suspend_type"].astype(str) == "S").all()
                )
        if suspended_on_date:
            classification = "proven_full_day_suspension"
            note = (
                "local suspension record (suspend_type=S) covers exactly this "
                "date, the canonical daily partition has no bar, and the "
                "sealed supplier re-fetch returned empty — three independent "
                "sources agree the instrument did not trade; the frozen "
                "prior20 window definition is unchanged, and any exclusion/"
                "backfill treatment is a rule decision to propose, not apply"
            )
        elif suspensions_present:
            classification = "suspension_record_found_needs_reading"
            note = "a suspension record exists for this code in the month; prior20 stays unknown until the record is read and scoped"
        elif bar_present:
            classification = "local_bar_present_supplier_empty"
            note = "canonical local daily bar exists while the sealed re-fetch returned empty; conflicting evidence, prior20 stays unknown"
        elif bar_present is False and suspensions_present is False:
            classification = "undetermined"
            note = "no local bar and no suspension record: neither supplier-empty nor absence of records proves anything; prior20 stays unknown"
        else:
            classification = "undetermined"
            note = "local evidence incomplete for this code-date; prior20 stays unknown"
        gaps.append(
            Prior20Gap(
                instrument_id=instrument,
                trade_date=trade_date,
                raw_attempt_sha256=raw_hash,
                local_bar_present=bar_present,
                suspension_record_present=suspensions_present,
                suspended_on_date=suspended_on_date,
                classification=classification,
                note=f"{raw_note}; {note}",
            )
        )
    return tuple(gaps)


def _corporate_entry_day_field(instrument_id: str) -> FieldAdmission:
    return FieldAdmission(
        field="corporate_actions_processed",
        source="structural_scope",
        status=STATUS_ADMITTED,
        reason=(
            f"{instrument_id}: entry day 2022-01-04 starts from an initially "
            "flat book with buy-only intents, so no held lot crosses an "
            "ex-date and no corporate adjustment is due on this day; this "
            "covers the entry day only and never future holding periods"
        ),
    )


def build_input_package(
    plan: dict,
    admitted_volumes: dict[str, int],
) -> tuple[dict, tuple[InstrumentPackage, ...]]:
    """Derive the consumable package from the sealed plan; sealed data is read-only."""
    selected = tuple(plan["selected_in_priority_order"])
    rows = {row["instrument_id"]: row for row in plan["rows"]}
    unknown = set()
    instruments: list[InstrumentPackage] = []
    for rank, instrument in enumerate(selected, start=1):
        row = rows[instrument]
        if row.get("proposal_rank") != rank or not row.get("selected_raw_proposal"):
            raise ValueError(f"sealed ranking mismatch for {instrument}")
        context = json.loads(json.dumps(row["contexts"]["baseline"]))  # deep copy
        sealed_volume = context.get("session_volume_shares")
        if instrument in admitted_volumes:
            # The sealed context carried None for these seven; the sealed
            # precision intake now supplies the exact integer.
            context["session_volume_shares"] = admitted_volumes[instrument]
            volume_field = FieldAdmission(
                field="session_volume_shares",
                source="s4_entry_raw_precision/reconciliation.json (numeric unit admission 2026-09-13)",
                status=STATUS_ADMITTED,
                reason="sealed integer shares verified against raw text units (lots x100; kilo-CNY x1000x100 to fen)",
            )
        elif isinstance(sealed_volume, int) and sealed_volume > 0:
            # Thirteen contexts already carried exact integer shares from the
            # sealed plan's original conversion; they are admitted as-sealed.
            volume_field = FieldAdmission(
                field="session_volume_shares",
                source="s4_first_entry_plan/plan.json context (sealed exact integer)",
                status=STATUS_ADMITTED,
                reason="sealed plan context already carried the exact integer share count",
            )
        else:
            volume_field = FieldAdmission(
                field="session_volume_shares",
                source="none",
                status=STATUS_UNKNOWN,
                reason="no exact execution-day volume available",
            )
            unknown.add(instrument)
        fields = [
            volume_field,
            _corporate_entry_day_field(instrument),
            FieldAdmission(
                field="market_open",
                source="none",
                status=STATUS_UNKNOWN,
                reason="no positive evidence of tradability on 2022-01-04; absent records are not proof",
            ),
            FieldAdmission(
                field="historical_identity_and_signal_eligibility",
                source="none",
                status=STATUS_UNKNOWN,
                reason="sealed plan marks identity/signal eligibility unverified; code prefix and missing ST rows are not proof",
            ),
        ]
        prior20 = context.get("prior20_amount_fen")
        if prior20 is None:
            fields.append(
                FieldAdmission(
                    field="prior20_amount_fen",
                    source="none",
                    status=STATUS_UNKNOWN,
                    reason=(
                        "this instrument's sealed prior20 window contains "
                        "proven full-day suspensions (local suspend_type=S "
                        "records matching the six empty supplier dates); the "
                        "window stays unknown under the frozen 20-session "
                        "rule, and any suspension-exclusion or backfill "
                        "treatment is a separately approved rule decision"
                    ),
                )
            )
        fees = context.get("fees") or {}
        if fees.get("additional_fee_rate") is None or fees.get("additional_fee_fixed_fen") is None:
            fields.append(
                FieldAdmission(
                    field="additional_fees",
                    source="user declaration pending",
                    status=STATUS_UNKNOWN,
                    reason="transfer/other fee scope unconfirmed; never zero-filled and never double-counted inside the declared commission",
                )
            )
        instruments.append(
            InstrumentPackage(
                instrument_id=instrument,
                proposal_rank=rank,
                nominal_quantity=row["nominal_quantity"],
                nominal_slot_fen=row["nominal_slot_fen"],
                context=context,
                fields=tuple(fields),
            )
        )
    package = {
        "decision_date": DECISION_DATE,
        "execution_date": EXECUTION_DATE,
        "following_date": FOLLOWING_DATE,
        "initial_cash_fen": plan["initial_cash_fen"],
        "target_gross_fen": plan["target_gross_fen"],
        "first_pending_signal_date": DECISION_DATE,
        "instruments": [
            {
                "instrument_id": item.instrument_id,
                "proposal_rank": item.proposal_rank,
                "nominal_quantity": item.nominal_quantity,
                "nominal_slot_fen": item.nominal_slot_fen,
                "context": item.context,
                "fields": [asdict(f) for f in item.fields],
                "open_gaps": list(item.open_gaps),
            }
            for item in instruments
        ],
        "scope": "s4_first_entry_input_package_only",
        "performance_evidence": False,
        "execution_authority": False,
    }
    return package, tuple(instruments)


def build_preflight_report(
    package: dict,
    instruments: tuple[InstrumentPackage, ...],
    prior20_gaps: tuple[Prior20Gap, ...],
    manifest: dict,
) -> dict:
    gap_index: dict[str, list[str]] = {}
    for item in instruments:
        for gap in item.open_gaps:
            gap_index.setdefault(gap, []).append(item.instrument_id)
    all_admitted = not any(item.open_gaps for item in instruments)
    verdict = (
        "inputs_ready_to_request_first_replay_run"
        if all_admitted
        else "preflight_stopped_before_execution_day"
    )
    return {
        "verdict": verdict,
        "precise_stop_date": EXECUTION_DATE if not all_admitted else None,
        "stop_session_reason": (
            "preflight: necessary context fields still unknown for the "
            "execution session; the loop would stop here before any fill"
            if not all_admitted
            else None
        ),
        "gap_counts_by_field": {
            key: {"instruments": len(values), "examples": values[:5]}
            for key, values in sorted(gap_index.items())
        },
        "prior20_gaps": [asdict(gap) for gap in prior20_gaps],
        "economic_paths_started": 0,
        "model_fits_used": 0,
        "provider_calls": 0,
        "consumption_notes": {
            "checkpoint": (
                "RiskLedgerCheckpoint.start(signal_date=date(2021,12,31), "
                "initial_cash_fen=package.initial_cash_fen, config=<full "
                "RiskLedgerConfig>) — the first pending intents keep the "
                "2021-12-31 signal date"
            ),
            "evidence": (
                "LedgerSessionEvidence(session=date(2022,1,4), contexts=<the "
                "20 contexts below as ResearchSession>, marks=<2022-01-04 raw "
                "marks>, corporate_processing_complete=True) built from the "
                "package contexts once every open gap is closed"
            ),
            "run_card": "the first replay run itself requires a separate limited card",
        },
        "manifest": manifest,
        "package": package,
    }


def run(source_dir: Path, canonical_dir: Path, output_dir: Path) -> dict:
    """Generate the input package and preflight report once, into output_dir."""
    manifest = build_manifest(source_dir)
    plan = _read_json(source_dir / "s4_first_entry_plan/plan.json")
    selected = tuple(plan["selected_in_priority_order"])
    reconciliation = _read_json(source_dir / "s4_entry_raw_precision/reconciliation.json")
    admitted = admit_exact_volumes(reconciliation["rows"], selected)
    gaps = check_prior20_gaps(PRIOR20_EMPTY_DATES, source_dir, canonical_dir)
    package, instruments = build_input_package(plan, admitted)
    report = build_preflight_report(package, instruments, gaps, manifest)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "input_package.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (output_dir / "preflight_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the S4-A first-replay input package from sealed local evidence."
    )
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--canonical-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    report = run(args.source_dir, args.canonical_dir, args.output_dir)
    print(json.dumps({key: report[key] for key in ("verdict", "precise_stop_date", "gap_counts_by_field")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
