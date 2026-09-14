"""Local sealed-evidence admission for the S4-A first historical replay.

Turns the already-sealed first-entry materials into a consumable input
package for :mod:`quantlab.research.risk_ledger_loop`, admitting only what
the local evidence actually supports. Sealed sources are validated against
frozen fingerprints via :func:`quantlab.research.round2_dataset.sealed_read`
and bound with :class:`quantlab.research.round2_dataset.InputBinding`; the
suspension classification reads the raw attempt evidence (intent, response
body, result) rather than trusting labels; the entry-day corporate flag is
written into the derived contexts only after the frozen entry contract
holds. This module never downloads, writes canonical data, reruns sealed
programs, simulates fills or claims returns; the first replay run itself
needs a separate limited card.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from quantlab.research.quantity_kernel import (
    ResearchFeeScenario,
    ResearchOrder,
    ResearchQuantityRules,
    ResearchSession,
)
from quantlab.research.round2_dataset import InputBinding, sealed_read

DECISION_DATE = "2021-12-31"
EXECUTION_DATE = "2022-01-04"
FOLLOWING_DATE = "2022-01-05"
VOLUME_PRECISION_PURPOSE = "execution_volume_precision"
LOT_SHARES = 100  # raw provider volume text is in lots of 100 shares
AMOUNT_KILO_YUAN = 1000  # raw provider amount text is in kilo-CNY
YUAN_TO_FEN = 100

STATUS_ADMITTED = "admitted"
STATUS_UNKNOWN = "unknown"

# Frozen sealed identity (programmatic reads frozen in
# docs/development_tasks/s4_replay_admission_fix_v1.md).
FROZEN_FINGERPRINTS = {
    "plan": "6359d58599328340b43a84215ffa2f15725944171edea0b0f2786cf0cadfce92",
    "reconciliation": "a9e30b671ace89d59b272d5558295a1b1ee68f75e93c79309bd310d8951c3e10",
    "profiles": "6e5fbc67e4d8169dfa5797eaca313d525c7106fafb42af282c188abe6845f0de",
}

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


def _decimal_or_none(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"not a decimal: {value!r}") from error


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
    request_identity_verified: bool
    response_verified_empty: bool
    response_sha256: str | None
    local_bar_present: bool | None
    suspension_on_date: bool | None
    suspend_timing_empty: bool | None
    suspension_source_record_ids: tuple[str, ...]
    classification: str
    note: str


# --------------------------------------------------------------------------
# R3: sealed source validation against the frozen identity
# --------------------------------------------------------------------------


MANIFEST_VERSION = 2


def validate_sealed_sources(source_dir: Path, binding: InputBinding) -> dict:
    """Recompute embedded fingerprints, compare with the frozen identity and
    return the version-2 manifest fragment for the three base artifacts:
    every file under its exact ``root:relative`` key, plus a separate
    ``fingerprint_checks`` section carrying each file's provenance."""
    consumed = {
        "plan": source_dir / "s4_first_entry_plan/plan.json",
        "reconciliation": source_dir / "s4_entry_raw_precision/reconciliation.json",
        "profiles": source_dir / "cohort_dividend_readiness/profiles.json",
    }
    files: dict[str, dict] = {}
    fingerprint_checks: dict[str, dict] = {}
    for name, path in consumed.items():
        if not path.is_file():
            raise FileNotFoundError(f"sealed source missing: {path}")
        payload = sealed_read(path)  # recomputes the embedded fingerprint
        embedded = payload.get("fingerprint")
        if embedded != FROZEN_FINGERPRINTS[name]:
            raise ValueError(
                f"{name} embedded fingerprint {embedded!r} does not match the "
                f"frozen identity {FROZEN_FINGERPRINTS[name]!r}"
            )
        binding.read(path)
        relative = path.resolve().relative_to(binding.root.resolve()).as_posix()
        key = f"sealed:{relative}"
        files[key] = {
            "path": str(path),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
            "root": "sealed",
        }
        fingerprint_checks[key] = {
            "embedded_fingerprint": embedded,
            "frozen_identity": FROZEN_FINGERPRINTS[name],
            "match": True,
        }
    return {"files": files, "fingerprint_checks": fingerprint_checks}


def _verify_plan_contract(plan: dict) -> None:
    rows = plan.get("rows")
    if not isinstance(rows, list) or len(rows) != 256:
        raise ValueError("plan cohort must be exactly 256 rows")
    seen: dict[str, int] = {}
    for row in rows:
        code = row.get("instrument_id")
        if not isinstance(code, str) or not code:
            raise ValueError("plan row without instrument id")
        seen[code] = seen.get(code, 0) + 1
        context = (row.get("contexts") or {}).get("baseline") or {}
        if context.get("instrument_id", code) != code:
            raise ValueError(f"context instrument mismatch for {code}")
        if context.get("execution_date") != EXECUTION_DATE:
            raise ValueError(f"context execution date mismatch for {code}")
    duplicates = sorted(code for code, count in seen.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate plan rows for {duplicates[:5]}")
    selected = plan.get("selected_in_priority_order")
    if not isinstance(selected, list) or len(selected) != 20 or len(set(selected)) != 20:
        raise ValueError("plan must select exactly 20 unique proposals")
    by_id = {row["instrument_id"]: row for row in rows}
    for rank, code in enumerate(selected, start=1):
        row = by_id[code]
        if row.get("proposal_rank") != rank or not row.get("selected_raw_proposal"):
            raise ValueError(f"sealed ranking mismatch for {code}")
    if plan.get("initial_cash_fen") != 20_000_000:
        raise ValueError("plan capital is not the frozen 200,000 CNY")


# --------------------------------------------------------------------------
# G2: exact volume admission by numeric unit mapping
# --------------------------------------------------------------------------


def admit_exact_volumes(
    reconciliation_rows: list[dict], selected_instruments: tuple[str, ...]
) -> dict[str, int]:
    """Admit exact execution-day volumes by numeric unit mapping.

    Each row must be a nonempty execution_volume_precision observation for
    the execution date naming one of the sealed proposals and satisfy the
    lot/kilo-yuan unit mapping exactly; otherwise it rejects.
    """
    from fractions import Fraction

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
        shares = Fraction(text["vol"]) * LOT_SHARES
        if shares.denominator != 1 or shares != normalized["vol"]:
            raise ValueError(
                f"volume unit mapping failed for {instrument}: "
                f"{text['vol']} lots x {LOT_SHARES} != {normalized['vol']} shares"
            )
        amount = Fraction(text["amount"]) * AMOUNT_KILO_YUAN * YUAN_TO_FEN
        if amount.denominator != 1 or amount != normalized["amount"]:
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


# --------------------------------------------------------------------------
# R2: raw attempt evidence + suspension classification
# --------------------------------------------------------------------------


def verify_attempt(attempt_dir: Path, instrument: str, trade_date: str) -> dict:
    """Verify the sealed raw attempt actually requested this code/date and
    returned a successful empty body; report anything else explicitly."""
    evidence = {
        "request_identity_verified": False,
        "response_verified_empty": False,
        "response_sha256": None,
        "detail": "",
    }
    if not attempt_dir.is_dir():
        evidence["detail"] = "attempt directory missing"
        return evidence
    intent_path = attempt_dir / "intent.json"
    body_path = attempt_dir / "response.body"
    result_path = attempt_dir / "result.json"
    if not (intent_path.is_file() and body_path.is_file() and result_path.is_file()):
        evidence["detail"] = "intent/response.body/result.json incomplete"
        return evidence
    intent = _read_json(intent_path)
    request = intent.get("request") or {}
    params = request.get("parameters") or {}
    inner = params.get("params") or {}
    stamp = trade_date.replace("-", "")
    identity_ok = (
        request.get("id") == f"daily_{instrument}_{stamp}"
        and params.get("api_name") == "daily"
        and inner.get("ts_code") == instrument
        and inner.get("start_date") == stamp
        and inner.get("end_date") == stamp
    )
    if not identity_ok:
        evidence["detail"] = "intent request identity does not match this code/date"
        return evidence
    result = _read_json(result_path)
    # Archival chain: the sealed result must reference exactly this intent.
    if result.get("intent_fingerprint") != intent.get("fingerprint"):
        evidence["detail"] = "result does not bind the sealed intent fingerprint"
        return evidence
    evidence["request_identity_verified"] = True
    body_bytes = body_path.read_bytes()
    body_sha = hashlib.sha256(body_bytes).hexdigest()
    artifacts = (result.get("artifacts") or {}).get("response.body") or {}
    recorded = artifacts.get("sha256")
    wire = result.get("wire_sha256")
    if not recorded or recorded != body_sha:
        evidence["detail"] = "response.body artifact binding missing or disagrees"
        return evidence
    if wire is not None and wire != body_sha:
        evidence["detail"] = "wire hash disagrees with the response body"
        return evidence
    evidence["response_sha256"] = body_sha
    if result.get("transport_status") != "received" or result.get("http_status") != 200:
        evidence["detail"] = (
            f"transport not a clean success: {result.get('transport_status')}"
            f"/{result.get('http_status')}"
        )
        return evidence
    if result.get("server_code") not in (None, 0):
        evidence["detail"] = f"server_code {result.get('server_code')!r} is not a success"
        return evidence
    # Parse the real body: a self-consistent hash never substitutes for the
    # success-empty semantics of the payload itself.
    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        evidence["detail"] = f"response body is not valid JSON: {error}"
        return evidence
    if body.get("code") != 0:
        evidence["detail"] = f"body service code {body.get('code')!r} is not a success"
        return evidence
    items = ((body.get("data") or {}).get("items")) if isinstance(body.get("data"), dict) else None
    if not isinstance(items, list) or items:
        evidence["detail"] = (
            "body data.items is not an empty list; the metadata claim of an "
            "empty return is not confirmed by the payload"
        )
        return evidence
    if result.get("rows") != 0 or result.get("status") != "empty":
        evidence["detail"] = (
            f"result metadata contradicts the empty body: rows={result.get('rows')} "
            f"status={result.get('status')}"
        )
        return evidence
    evidence["response_verified_empty"] = True
    evidence["detail"] = "verified successful empty daily response for exactly this code/date"
    return evidence


def check_prior20_gaps(
    empty_rows: list[tuple[str, str]],
    source_dir: Path,
    canonical_dir: Path,
    binding: InputBinding,
    canonical_binding: InputBinding,
    bound_files: dict,
) -> tuple[Prior20Gap, ...]:
    """Classify the six empty prior20 code-dates against read raw evidence.

    A supplier-basis full-day suspension requires, for exactly this
    code/date: a verified successful empty daily response, confirmed daily
    coverage with no bar, and an S record with empty suspend_timing.
    Intraday timing, conflicting bars, coverage gaps and missing or invalid
    responses get distinct classifications; notes state only verified
    facts, and Tushare-derived records are never counted as independent
    sources. Every consumed file lands in ``bound_files`` keyed by root.
    """
    import pandas as pd

    def bind(root_label: str, binding_: InputBinding, path: Path) -> None:
        binding_.read(path)
        relative = path.resolve().relative_to(binding_.root.resolve()).as_posix()
        entry = dict(binding_.entries[relative])
        bound_files[f"{root_label}:{relative}"] = {**entry, "root": root_label}

    gaps: list[Prior20Gap] = []
    for instrument, trade_date in empty_rows:
        year, month, _ = trade_date.split("-")
        attempt_dir = (
            source_dir
            / f"s4_entry_raw_precision/attempts/daily_{instrument}_{trade_date.replace('-', '')}"
        )
        for name in ("intent.json", "result.json", "response.body"):
            path = attempt_dir / name
            if path.is_file():
                bind("sealed", binding, path)
        evidence = verify_attempt(attempt_dir, instrument, trade_date)

        bar_present: bool | None = None
        coverage = "directory_missing"
        month_dir = canonical_dir / f"daily/year={year}/month={month}"
        parts = sorted(month_dir.glob("*.parquet")) if month_dir.is_dir() else []
        if parts:
            coverage = "partitions_present"
            for part in parts:
                bind("canonical", canonical_binding, part)
            frame = pd.concat(pd.read_parquet(p) for p in parts)
            code_col = "ts_code" if "ts_code" in frame.columns else "instrument_id"
            date_col = "trade_date" if "trade_date" in frame.columns else "session"
            if code_col in frame.columns and date_col in frame.columns:
                date_rows = frame[
                    frame[date_col].astype(str).str[:10] == trade_date
                ]
                if date_rows.empty:
                    # A month partition holding only other dates does not
                    # prove the target session was ever ingested locally.
                    coverage = "target_date_absent"
                else:
                    coverage = "full"
                    matched = date_rows[date_rows[code_col] == instrument]
                    bar_present = len(matched) > 0
            else:
                coverage = "missing_columns"

        timing_empty: bool | None = None
        on_date: bool | None = None
        record_ids: tuple[str, ...] = ()
        susp_dir = canonical_dir / (
            f"lifecycle_context_v1/suspensions/year={year}/month={month}"
        )
        if susp_dir.is_dir():
            parts = sorted(susp_dir.glob("*.parquet"))
            for part in parts:
                bind("canonical", canonical_binding, part)
            frame = pd.concat(pd.read_parquet(p) for p in parts)
            code_col = "ts_code" if "ts_code" in frame.columns else "instrument_id"
            date_col = "trade_date" if "trade_date" in frame.columns else "session"
            rows = frame[frame[code_col] == instrument]
            day_rows = rows[rows[date_col].astype(str).str[:10] == trade_date]
            on_date = len(day_rows) > 0
            if "source_record_id" in day_rows.columns:
                record_ids = tuple(str(x) for x in day_rows["source_record_id"])
            if on_date:
                types = day_rows["suspend_type"].astype(str)
                timings = [
                    None
                    if pd.isna(v) or str(v).strip() in ("", "None")
                    else str(v)
                    for v in day_rows["suspend_timing"]
                ]
                if not (types == "S").all():
                    on_date = False
                else:
                    timing_empty = all(v is None for v in timings)

        if not evidence["request_identity_verified"] or not evidence["response_verified_empty"]:
            classification = "response_missing_or_invalid"
            note = f"raw daily attempt not a verified empty return: {evidence['detail']}"
        elif coverage != "full":
            classification = "local_coverage_incomplete"
            note = (
                f"local daily coverage for {year}-{month} is {coverage}; "
                "“no local bar” cannot be claimed without confirmed "
                "coverage, prior20 stays unknown"
            )
        elif bar_present:
            classification = "conflicting_local_bar"
            note = (
                "local daily bar exists while the verified response was empty; "
                "conflicting evidence, prior20 stays unknown"
            )
        elif on_date and timing_empty:
            classification = "proven_full_day_suspension_supplier_basis"
            note = (
                "verified empty daily response for exactly this code/date, "
                "confirmed daily coverage with no local bar, and an S record "
                "with empty suspend_timing on the date — under the supplier "
                "field convention this supports a full-day suspension "
                "reading; the Tushare-derived records are corroborating, not "
                "independent sources"
            )
        elif on_date:
            classification = "suspension_timing_present"
            note = (
                "an S record covers the date but carries non-empty timing "
                "(possible intraday suspension); prior20 stays unknown"
            )
        else:
            classification = "undetermined"
            note = (
                "no local bar and no suspension record for the date; nothing "
                "is proven, prior20 stays unknown"
            )
        gaps.append(
            Prior20Gap(
                instrument_id=instrument,
                trade_date=trade_date,
                request_identity_verified=evidence["request_identity_verified"],
                response_verified_empty=evidence["response_verified_empty"],
                response_sha256=evidence["response_sha256"],
                local_bar_present=bar_present,
                suspension_on_date=on_date,
                suspend_timing_empty=timing_empty,
                suspension_source_record_ids=record_ids,
                classification=classification,
                note=note,
            )
        )
    canonical_binding.check()
    binding.check()
    return tuple(gaps)


# --------------------------------------------------------------------------
# R4: necessary-field validation over each derived context
# --------------------------------------------------------------------------


def _is_positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _is_bool_or_none(value: object) -> bool:
    return value is None or type(value) is bool


def necessary_field_gaps(context: dict) -> list[str]:
    """Every kernel-necessary field problem in one derived context.

    One systematic sweep over the frozen contracts (`ResearchSession`,
    `ResearchFeeScenario`, `ResearchQuantityRules`, scheduler
    preconditions). ``None`` on a kernel-required field is an explicit
    unknown and counts as a gap; the three boolean flags additionally
    accept None (their dedicated fields already carry that state). Zero
    volume, zero amount and zero minimum commission are legal known
    no-trade observations, never gaps; negative values and out-of-range
    rates are invalid. Every finding is reported; the first problem never
    masks the rest.
    """
    gaps: list[str] = []

    def add(gap: str) -> None:
        if gap not in gaps:
            gaps.append(gap)

    def parse_iso(value: object) -> date | None:
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
        return None

    def rate_gap(prefix: str, value: object, *, allow_one: bool) -> None:
        name = f"{prefix}: "
        if value is None:
            add(f"{name}missing (unknown)")
            return
        if not isinstance(value, str):
            add(f"{name}must be a decimal string")
            return
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            add(f"{name}not a finite decimal string")
            return
        if not parsed.is_finite() or parsed < 0 or parsed > 1:
            add(f"{name}rate outside the modeled 0-1 range")
        elif not allow_one and parsed == 1:
            add(f"{name}must stay below 1 so the modeled sell price stays positive")

    # --- identity flags ---------------------------------------------------
    for name in ("calendar_verified", "market_open", "corporate_actions_processed"):
        if not _is_bool_or_none(context.get(name)):
            add(f"{name}: must be boolean or explicit unknown")
    if context.get("calendar_verified") is not True:
        add("calendar_verified: the execution session is not verified")

    # --- dates: real parsing first, relations only on parseable values ----
    date_fields = {}
    for name in ("next_session", "evidence_date", "prior20_asof"):
        value = context.get(name)
        if value is None:
            add(f"{name}: missing (unknown)")
        elif not isinstance(value, str):
            add(f"{name}: must be a date string")
        else:
            parsed = parse_iso(value)
            if parsed is None:
                add(f"{name}: not a valid ISO date")
            else:
                date_fields[name] = parsed
    execution = parse_iso(EXECUTION_DATE)
    decision = parse_iso(DECISION_DATE)
    if "evidence_date" in date_fields and execution is not None:
        if date_fields["evidence_date"] != execution:
            add("evidence_date: must be the execution date")
    if "next_session" in date_fields and execution is not None:
        if date_fields["next_session"] <= execution:
            add("next_session: must follow the execution date")
    if "prior20_asof" in date_fields and decision is not None:
        if date_fields["prior20_asof"] > decision:
            add("prior20_asof: later than the decision date (future information)")

    # --- integer money quantities: zero is legal known no-trade -----------
    for name in ("raw_close_fen", "low_fen", "high_fen", "down_limit_fen", "up_limit_fen"):
        value = context.get(name)
        if value is None:
            add(f"{name}: missing (unknown)")
        elif not _is_positive_int(value):
            add(f"{name}: must be a positive integer fen")
    for name in ("prior20_amount_fen", "session_amount_fen"):
        value = context.get(name)
        if value is None:
            add(f"{name}: missing (unknown)")
        elif type(value) is not int:
            add(f"{name}: must be an integer fen")
        elif value < 0:
            add(f"{name}: negative amounts are invalid")
    volume = context.get("session_volume_shares")
    if volume is None:
        add("session_volume_shares: missing (unknown)")
    elif type(volume) is not int:
        add("session_volume_shares: must be an integer")
    elif volume < 0:
        add("session_volume_shares: negative volumes are invalid")
    bounds = [
        context.get(name)
        for name in ("down_limit_fen", "low_fen", "raw_close_fen", "high_fen", "up_limit_fen")
    ]
    if all(isinstance(b, int) for b in bounds) and not (
        bounds[0] <= bounds[1] <= bounds[2] <= bounds[3] <= bounds[4]
    ):
        add("price bounds: down<=low<=close<=high<=up violated")
    sessions = context.get("prior20_sessions")
    if sessions is None:
        add("prior20_sessions: missing (unknown)")
    elif type(sessions) is not int or sessions != 20:
        add("prior20_sessions: must be the integer 20")

    # --- participation: one finite fraction in (0, 1] ---------------------
    participation = context.get("participation")
    if participation is None:
        add("participation: missing (unknown)")
    else:
        try:
            level = Decimal(str(participation))
        except InvalidOperation:
            add("participation: must be a finite fraction or explicit unknown")
        else:
            if not level.is_finite() or not 0 < level <= 1:
                add("participation: must be a finite fraction or explicit unknown")

    # --- quantity rules ----------------------------------------------------
    rules = context.get("rules")
    if not isinstance(rules, dict):
        add("rules: missing quantity-rule scenario")
    else:
        if not rules.get("scenario_id"):
            add("rules.scenario_id: missing (unknown)")
        for name in (
            "buy_minimum",
            "buy_increment",
            "sell_minimum",
            "sell_increment",
            "max_order_quantity",
        ):
            if not _is_positive_int(rules.get(name)):
                add(f"rules.{name}: must be a positive integer")
        if type(rules.get("full_position_odd_exit")) is not bool:
            add("rules.full_position_odd_exit: must be boolean")
        raw_from = rules.get("effective_from")
        raw_through = rules.get("effective_through")
        rule_from = parse_iso(raw_from)
        rule_through = parse_iso(raw_through)
        if raw_from is None:
            add("rules.effective_from: missing (unknown)")
        elif rule_from is None:
            add("rules.effective_from: not a valid ISO date")
        if raw_through is None:
            add("rules.effective_through: missing (unknown)")
        elif rule_through is None:
            add("rules.effective_through: not a valid ISO date")
        if rule_from is not None and rule_through is not None:
            if rule_from > rule_through:
                add("rules: interval reversed")
            elif execution is not None and not rule_from <= execution <= rule_through:
                add("rules: interval does not cover the execution date")

    # --- fees ---------------------------------------------------------------
    fees = context.get("fees")
    if not isinstance(fees, dict):
        add("fees: missing fee scenario")
    else:
        if not fees.get("scenario_id"):
            add("fees.scenario_id: missing (unknown)")
        for name in (
            "commission_rate",
            "buy_stamp_rate",
            "sell_stamp_rate",
            "adverse_slippage_rate",
            "additional_fee_rate",
        ):
            rate_gap(f"fees.{name}", fees.get(name), allow_one=(name != "adverse_slippage_rate"))
        minimum = fees.get("minimum_commission_fen")
        if minimum is None:
            add("fees.minimum_commission_fen: missing (unknown)")
        elif type(minimum) is not int or minimum < 0:
            add("fees.minimum_commission_fen: must be a nonnegative integer")
        fixed = fees.get("additional_fee_fixed_fen")
        if fixed is None:
            add("fees.additional_fee_fixed_fen: missing (unknown)")
        elif type(fixed) is not int or fixed < 0:
            add("fees.additional_fee_fixed_fen: must be a nonnegative integer")
        raw_fee_from = fees.get("effective_from")
        raw_fee_through = fees.get("effective_through")
        fee_from = parse_iso(raw_fee_from)
        fee_through = parse_iso(raw_fee_through)
        if raw_fee_from is None:
            add("fees.effective_from: missing (unknown)")
        elif fee_from is None:
            add("fees.effective_from: not a valid ISO date")
        if raw_fee_through is None:
            add("fees.effective_through: missing (unknown)")
        elif fee_through is None:
            add("fees.effective_through: not a valid ISO date")
        if fee_from is not None and fee_through is not None:
            if fee_from > fee_through:
                add("fees: interval reversed")
            elif execution is not None and not fee_from <= execution <= fee_through:
                add("fees: interval does not cover the execution date")

    seen: set[str] = set()
    unique: list[str] = []
    for gap in gaps:
        if gap not in seen:
            seen.add(gap)
            unique.append(gap)
    return unique


def research_session_from_context(context: dict) -> ResearchSession:
    """Type-checked assembly of a package context into the kernel's session."""
    rules = context["rules"]
    fees = context["fees"]
    return ResearchSession(
        instrument_id=context["instrument_id"],
        execution_date=date.fromisoformat(context["execution_date"]),
        next_session=date.fromisoformat(context["next_session"]),
        evidence_date=date.fromisoformat(context["evidence_date"]),
        calendar_verified=context["calendar_verified"],
        market_open=context["market_open"],
        corporate_actions_processed=context["corporate_actions_processed"],
        raw_close_fen=context["raw_close_fen"],
        low_fen=context["low_fen"],
        high_fen=context["high_fen"],
        down_limit_fen=context["down_limit_fen"],
        up_limit_fen=context["up_limit_fen"],
        prior20_amount_fen=context["prior20_amount_fen"],
        prior20_asof=date.fromisoformat(context["prior20_asof"]),
        prior20_sessions=context["prior20_sessions"],
        session_amount_fen=context["session_amount_fen"],
        session_volume_shares=context["session_volume_shares"],
        participation=_decimal_or_none(context["participation"]),
        rules=ResearchQuantityRules(
            scenario_id=rules["scenario_id"],
            effective_from=date.fromisoformat(rules["effective_from"]),
            effective_through=date.fromisoformat(rules["effective_through"]),
            buy_minimum=rules["buy_minimum"],
            buy_increment=rules["buy_increment"],
            sell_minimum=rules["sell_minimum"],
            sell_increment=rules["sell_increment"],
            max_order_quantity=rules["max_order_quantity"],
            full_position_odd_exit=rules["full_position_odd_exit"],
        ),
        fees=ResearchFeeScenario(
            scenario_id=fees["scenario_id"],
            effective_from=date.fromisoformat(fees["effective_from"]),
            effective_through=date.fromisoformat(fees["effective_through"]),
            commission_rate=_decimal_or_none(fees["commission_rate"]),
            minimum_commission_fen=fees["minimum_commission_fen"],
            buy_stamp_rate=_decimal_or_none(fees["buy_stamp_rate"]),
            sell_stamp_rate=_decimal_or_none(fees["sell_stamp_rate"]),
            additional_fee_rate=_decimal_or_none(fees["additional_fee_rate"]),
            additional_fee_fixed_fen=fees["additional_fee_fixed_fen"],
            adverse_slippage_rate=_decimal_or_none(fees["adverse_slippage_rate"]),
        ),
    )


# --------------------------------------------------------------------------
# Package assembly
# --------------------------------------------------------------------------


def build_input_package(
    plan: dict,
    admitted_volumes: dict[str, int],
    prior20_findings: tuple[Prior20Gap, ...] = (),
) -> tuple[dict, tuple[InstrumentPackage, ...]]:
    """Derive the consumable package; sealed data stays read-only."""
    _verify_plan_contract(plan)
    selected = tuple(plan["selected_in_priority_order"])
    rows: dict[str, dict] = {}
    for row in plan["rows"]:
        if row["instrument_id"] in rows:
            raise ValueError(f"duplicate plan row for {row['instrument_id']}")
        rows[row["instrument_id"]] = row
    findings: dict[str, list[Prior20Gap]] = {}
    for gap in prior20_findings:
        findings.setdefault(gap.instrument_id, []).append(gap)

    instruments: list[InstrumentPackage] = []
    for rank, instrument in enumerate(selected, start=1):
        row = rows[instrument]
        context = json.loads(json.dumps(row["contexts"]["baseline"]))  # deep copy
        context["instrument_id"] = instrument
        # R1: the frozen entry contract (initially flat book, buy-only
        # proposals, fixed execution date) lets the derived entry-day
        # context carry the processed flag; the sealed context stays None.
        context["corporate_actions_processed"] = True
        fields: list[FieldAdmission] = []
        volume = context.get("session_volume_shares")
        if instrument in admitted_volumes:
            if volume is not None:
                raise ValueError(
                    f"{instrument}: refusing to overwrite a non-None sealed volume"
                )
            context["session_volume_shares"] = admitted_volumes[instrument]
            fields.append(
                FieldAdmission(
                    field="session_volume_shares",
                    source=(
                        "s4_entry_raw_precision/reconciliation.json "
                        "(numeric unit admission)"
                    ),
                    status=STATUS_ADMITTED,
                    reason=(
                        "sealed-None slot filled from the precision intake; "
                        "integer verified against raw text units"
                    ),
                )
            )
        elif isinstance(volume, int) and volume > 0:
            fields.append(
                FieldAdmission(
                    field="session_volume_shares",
                    source="s4_first_entry_plan/plan.json context (sealed exact integer)",
                    status=STATUS_ADMITTED,
                    reason="sealed plan context already carried the exact integer share count",
                )
            )
        else:
            fields.append(
                FieldAdmission(
                    field="session_volume_shares",
                    source="none",
                    status=STATUS_UNKNOWN,
                    reason="no exact execution-day volume available",
                )
            )
        fields.append(
            FieldAdmission(
                field="corporate_actions_processed",
                source="structural_scope",
                status=STATUS_ADMITTED,
                reason=(
                    "entry day 2022-01-04 starts from an initially flat book "
                    "with buy-only intents, so no held lot crosses an "
                    "ex-date; the derived context carries true for this day "
                    "only and never future holding periods"
                ),
            )
        )
        fields.append(
            FieldAdmission(
                field="market_open",
                source="none",
                status=STATUS_UNKNOWN,
                reason=(
                    "no positive evidence of tradability on 2022-01-04; "
                    "absent records are not proof"
                ),
            )
        )
        fields.append(
            FieldAdmission(
                field="historical_identity_and_signal_eligibility",
                source="none",
                status=STATUS_UNKNOWN,
                reason=(
                    "sealed plan marks identity/signal eligibility "
                    "unverified; code prefix and missing ST rows are not proof"
                ),
            )
        )
        if context.get("prior20_amount_fen") is None:
            own = findings.get(instrument, [])
            if own and all(
                g.classification == "proven_full_day_suspension_supplier_basis"
                for g in own
            ):
                reason = (
                    "this window's empty dates are proven full-day "
                    "suspensions on the supplier basis (verified empty "
                    "responses for exactly these code/dates, no local bars, "
                    "S records with empty timing); how suspended sessions "
                    "enter the frozen 20-session window is an approved-rule "
                    "decision, so the window stays unknown"
                )
            else:
                reason = (
                    "prior20 window evidence incomplete for this instrument; "
                    "the window stays unknown and is never re-windowed or filled"
                )
            fields.append(
                FieldAdmission(
                    field="prior20_amount_fen",
                    source="none",
                    status=STATUS_UNKNOWN,
                    reason=reason,
                )
            )
        stable_fee_ids = {
            "fees.additional_fee_rate: missing (unknown)",
            "fees.additional_fee_fixed_fen: missing (unknown)",
        }
        for gap in necessary_field_gaps(context):
            if gap in stable_fee_ids:
                # These two carry their own stable dedicated entries below.
                continue
            fields.append(
                FieldAdmission(
                    field=f"context.{gap}",
                    source="derived context validation",
                    status=STATUS_UNKNOWN,
                    reason=gap,
                )
            )
        for fee_id in ("fees.additional_fee_rate", "fees.additional_fee_fixed_fen"):
            fields.append(
                FieldAdmission(
                    field=fee_id,
                    source="none",
                    status=STATUS_UNKNOWN,
                    reason=(
                        "additional-fee scope is unconfirmed for this "
                        "historical scenario; never zero-filled"
                    ),
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


def build_first_pending_orders(package: dict) -> tuple[ResearchOrder, ...]:
    """Read-only assembly of the first-day buy intents at nominal sizes.

    The intents keep the 2021-12-31 signal date and each sealed slot's
    nominal quantity; affordability, limits and capacity stay the kernel's
    decision on the execution session.
    """
    return tuple(
        ResearchOrder(
            f"{package['first_pending_signal_date']}|buy|{item['instrument_id']}",
            item["instrument_id"],
            "buy",
            item["nominal_quantity"],
            date.fromisoformat(package["first_pending_signal_date"]),
        )
        for item in package["instruments"]
    )


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
                "initial_cash_fen=package.initial_cash_fen, "
                "pending_orders=build_first_pending_orders(package), "
                "config=<full RiskLedgerConfig>) — pending orders carry the "
                "2021-12-31 signal date explicitly"
            ),
            "evidence": (
                "LedgerSessionEvidence(session=date(2022,1,4), "
                "contexts=<research_session_from_context over the 20 package "
                "contexts>, marks=<2022-01-04 raw marks>, "
                "corporate_processing_complete=True) once every open gap is "
                "closed"
            ),
            "run_card": "the first replay run itself requires a separate limited card",
        },
        "manifest": manifest,
        "package": package,
    }


def _root_for(label: str, source_dir: Path, canonical_dir: Path) -> Path:
    return source_dir if label == "sealed" else canonical_dir


def run(source_dir: Path, canonical_dir: Path, output_dir: Path) -> dict:
    """Generate the input package and preflight report once, exclusively."""
    source_dir = source_dir.resolve()
    canonical_dir = canonical_dir.resolve()
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise ValueError(f"output directory already exists: {output_dir}")
    resolved = output_dir.resolve()
    if resolved.is_relative_to(source_dir) or resolved.is_relative_to(canonical_dir):
        raise ValueError("output directory must live outside the source and canonical trees")
    output_dir.mkdir(parents=True)
    (output_dir / "started.json").write_text(
        json.dumps({"status": "started"}, sort_keys=True), encoding="utf-8"
    )
    binding = InputBinding(source_dir)
    canonical_binding = InputBinding(canonical_dir)
    bound_files: dict = {}
    try:
        manifest = validate_sealed_sources(source_dir, binding)
        plan = sealed_read(source_dir / "s4_first_entry_plan/plan.json")
        reconciliation = sealed_read(
            source_dir / "s4_entry_raw_precision/reconciliation.json"
        )
        selected = tuple(plan["selected_in_priority_order"])
        admitted = admit_exact_volumes(reconciliation["rows"], selected)
        gaps = check_prior20_gaps(
            PRIOR20_EMPTY_DATES,
            source_dir,
            canonical_dir,
            binding,
            canonical_binding,
            bound_files,
        )
        package, instruments = build_input_package(plan, admitted, gaps)
        manifest["files"] = dict(manifest.get("files", {}))
        for root_label, root_binding in (
            ("sealed", binding),
            ("canonical", canonical_binding),
        ):
            for name, entry in root_binding.entries.items():
                manifest["files"][f"{root_label}:{name}"] = {
                    "path": str(_root_for(root_label, source_dir, canonical_dir) / name),
                    "sha256": entry["sha256"],
                    "bytes": entry["bytes"],
                    "root": root_label,
                }
        manifest["bound_files"] = dict(bound_files)
        manifest["roots"] = {"sealed": str(source_dir), "canonical": str(canonical_dir)}
        manifest["manifest_version"] = MANIFEST_VERSION
        report = build_preflight_report(package, instruments, gaps, manifest)
    except Exception as error:
        (output_dir / "failed.json").write_text(
            json.dumps({"status": "failed", "reason": str(error)}, sort_keys=True),
            encoding="utf-8",
        )
        raise
    (output_dir / "input_package.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (output_dir / "preflight_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    binding.check()  # every consumed file byte-identical after the run
    (output_dir / "completed.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "input_package_sha256": sha256_file(output_dir / "input_package.json"),
                "preflight_report_sha256": sha256_file(output_dir / "preflight_report.json"),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
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
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("verdict", "precise_stop_date", "gap_counts_by_field")
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
