"""One append-only daily paper account: settle frozen orders, then freeze next decisions.

Inputs are local evidence snapshots. No provider, canonical write or broker authority.
A late catch-up day advances accounting but cannot authorize a new forward decision.
"""

from __future__ import annotations

import json
import shutil
from datetime import date
from uuid import uuid4

import pandas as pd

from quantlab.research.alpha158_store import exclusive_job
from quantlab.research.ml import serving
from quantlab.research.ml.artifacts import (
    checkpoint_read,
    checkpoint_write,
    complete,
    fingerprint,
    publish_ready,
    record_publication,
    verify_completed,
    verify_publication,
)
from quantlab.research.ml.config import MLConfig
from quantlab.research.ml.corporate import capture_entitlements, decode_event, new_state
from quantlab.research.ml.data import calendar_index
from quantlab.research.ml.io import decode_market_day, research_output, sha256, write_json
from quantlab.research.ml.replay import plan_orders, settle_plan
from quantlab.research.quantity_kernel import ResearchBook

INPUT_FILES = ("features.parquet", "calendar.json", "market.json", "corporate_actions.json")


def config_from(payload):
    return MLConfig(**{**payload, "models": tuple(payload["models"])})


def initialize(
    output,
    config,
    calendar,
    marks,
    asof,
    capital_fen,
    feature_contract_sha256,
    *,
    max_model_age_days=45,
):
    research_output(output)
    sessions = calendar_index(calendar)
    if (
        pd.Timestamp(asof) not in sessions
        or sessions.get_loc(pd.Timestamp(asof)) >= len(sessions) - 1
    ):
        raise ValueError("inception needs a following trading session")
    if type(capital_fen) is not int or capital_fen <= 0:
        raise ValueError("positive integer capital in fen required")
    if type(max_model_age_days) is not int or max_model_age_days <= 0:
        raise ValueError("positive maximum model age required")
    if len(feature_contract_sha256) != 64 or set(feature_contract_sha256) - set("0123456789abcdef"):
        raise ValueError("training feature contract SHA256 required")
    if (
        not marks
        or len({m.instrument_id for m in marks}) != len(marks)
        or any(m.session != asof or m.price_fen <= 0 for m in marks)
    ):
        raise ValueError("unique inception close marks required")
    output.mkdir(parents=True, exist_ok=False)
    payload = {
        "schema": "quantlab_paper_service_v1",
        "inception": str(asof),
        "capital_fen": capital_fen,
        "config": config.payload(),
        "calendar": sessions.strftime("%Y-%m-%d").tolist(),
        "feature_contract_sha256": feature_contract_sha256,
        "max_model_age_days": max_model_age_days,
        "created_at": serving.now().isoformat(),
        "execution_authority": False,
    }
    write_json(output / "service.json", payload)
    checkpoint_write(
        output / "initial.json",
        {
            "book": ResearchBook(asof, capital_fen),
            "marks": {m.instrument_id: m.price_fen for m in marks},
            "attempted": set(),
            "corporate_state": new_state(),
        },
    )
    complete(output)
    return payload


def load_service(root):
    verify_completed(root)
    payload = json.loads((root / "service.json").read_text())
    if payload["schema"] != "quantlab_paper_service_v1":
        raise ValueError("unknown paper service schema")
    return payload


def read_inputs(folder, asof, service):
    manifest = json.loads((folder / "manifest.json").read_text())
    if (
        manifest["asof"] != str(asof)
        or not manifest.get("source_id")
        or set(manifest["files"]) != set(INPUT_FILES)
    ):
        raise ValueError("daily input manifest identity/files/source mismatch")
    if manifest["feature_contract_sha256"] != service["feature_contract_sha256"]:
        raise ValueError("daily feature formula/version differs from service contract")
    available = pd.Timestamp(manifest["available_at"])
    if available.tzinfo is None or available > pd.Timestamp(serving.now()):
        raise ValueError("daily evidence unavailable at actual processing time")
    for name, digest in manifest["files"].items():
        if sha256(folder / name) != digest:
            raise ValueError(f"daily input fingerprint changed:{name}")
    sessions = calendar_index(json.loads((folder / "calendar.json").read_text()))
    declared = service["calendar"]
    if sessions.strftime("%Y-%m-%d").tolist()[: len(declared)] != declared:
        raise ValueError("calendar must preserve the service calendar prefix")
    if (
        pd.Timestamp(asof) not in sessions
        or sessions.get_loc(pd.Timestamp(asof)) >= len(sessions) - 1
    ):
        raise ValueError("daily input needs current and next trading session")
    raw_market = json.loads((folder / "market.json").read_text())
    if raw_market.get("orders"):
        raise ValueError("daily market evidence must not inject orders")
    market = decode_market_day(raw_market)
    if market.session != asof:
        raise ValueError("daily market evidence is from another session")
    actions = json.loads((folder / "corporate_actions.json").read_text())
    coverage = actions["coverage"]
    if (
        not coverage.get("source_id")
        or date.fromisoformat(coverage["start"]) > date.fromisoformat(service["inception"])
        or date.fromisoformat(coverage["end"]) < asof
    ):
        raise ValueError("corporate coverage must include account inception through today")
    events = tuple(decode_event(row) for row in actions["events"])
    if len({e.event_id for e in events}) != len(events):
        raise ValueError("duplicate corporate event")
    return manifest, tuple(d.date() for d in sessions), market, events


def account_head(root, service):
    """Validate append order and parent references, not just the newest filename."""
    state = checkpoint_read(root / "initial.json")
    parent = sha256(root / "initial.json")
    previous = None
    for folder in sorted((root / "sessions").glob("*")):
        if not folder.is_dir():
            continue
        verify_completed(folder)
        saved = checkpoint_read(folder / "account.json")
        if saved["service_sha256"] != sha256(root / "completed.json"):
            raise ValueError("paper account service binding mismatch")
        if saved["parent"] != parent:
            raise ValueError("paper account parent chain mismatch")
        day = saved["state"]["book"].asof_date
        expected = (
            saved["calendar"][saved["calendar"].index(state["book"].asof_date) + 1]
            if previous
            else date.fromisoformat(service["inception"])
        )
        if day != expected:
            raise ValueError("paper account has a missing session")
        if folder.name != str(day) or (previous and day <= state["book"].asof_date):
            raise ValueError("paper account session order mismatch")
        state, previous = saved["state"], saved
        parent = sha256(folder / "completed.json")
    return state, parent, previous


def set_paused(root, paused):
    research_output(root)
    load_service(root)
    with exclusive_job(root):
        event = {"paused": bool(paused), "recorded_at": serving.now().isoformat()}
        write_json(root / "controls" / f"{uuid4().hex}.json", event)
        return event


def is_paused(root):
    events = [json.loads(p.read_text()) for p in (root / "controls").glob("*.json")]
    return max(events, key=lambda e: e["recorded_at"])["paused"] if events else False


def run_day(root, inputs, registry, asof, *, code, verify_code=None):
    research_output(root)
    load_service(root)
    try:
        return _run_day(root, inputs, registry, asof, code=code, verify_code=verify_code)
    except Exception as exc:
        write_json(
            root / "failures" / f"{uuid4().hex}.json",
            {
                "asof": str(asof),
                "recorded_at": serving.now().isoformat(),
                "type": type(exc).__name__,
                "reason": str(exc),
            },
        )
        raise


def _run_day(root, inputs, registry, asof, *, code, verify_code=None):
    service = load_service(root)
    config = config_from(service["config"])
    with exclusive_job(root):
        manifest, calendar, market, events = read_inputs(inputs, asof, service)
        target = root / "sessions" / str(asof)
        input_hash = fingerprint(manifest)
        if target.exists():
            account_head(root, service)
            verify_completed(target)
            saved = checkpoint_read(target / "account.json")
            if saved["inputs"] != input_hash or saved["code"] != code:
                raise ValueError("already committed day differs; never rewrite account history")
            if not (target / "published.json").exists():
                record_publication(target, asof, serving.now)
            return decision_status(root, asof, saved["status"])
        state, parent, previous = account_head(root, service)
        inception = date.fromisoformat(service["inception"])
        first = calendar.index(inception)
        i = calendar.index(asof)
        expected = (
            inception if previous is None else calendar[calendar.index(state["book"].asof_date) + 1]
        )
        if asof != expected:
            raise ValueError(f"noncontiguous daily run; process {expected} first")
        if previous and previous["calendar"] != list(calendar[: len(previous["calendar"])]):
            raise ValueError("calendar extension changed previously committed sessions")
        # Previously seen event facts may not disappear or be silently revised.
        event_map = {e.event_id: fingerprint(e) for e in events}
        if previous and any(event_map.get(k) != v for k, v in previous["events"].items()):
            raise ValueError("corporate event revised/disappeared; reconcile before advancing")
        new_events = [e for e in events if previous is None or e.event_id not in previous["events"]]
        if previous and any(
            e.record_date < state["book"].asof_date and e.ex_date > inception for e in new_events
        ):
            raise ValueError("late corporate entitlement needs explicit historical reconciliation")
        state["corporate_state"] = capture_entitlements(
            state["corporate_state"], state["book"], events
        )
        record = decision = None
        if previous is None and state["marks"] != {
            m.instrument_id: m.price_fen for m in market.marks
        }:
            raise ValueError("inception market marks differ from initialized account")
        if previous:
            prior = root / "sessions" / str(state["book"].asof_date)
            receipt = verify_publication(prior, state["book"].asof_date, require_forward=False)
            frozen = previous["plan"]
            # A late/missing decision cannot be recreated after seeing execution prices.
            if frozen is None or not receipt["forward_eligible"]:
                frozen = {
                    "orders": (),
                    "industries": previous["industries"],
                    "signal_date": str(state["book"].asof_date),
                    "execution_date": str(asof),
                    "decision_missing": True,
                }
            state, record, decision = settle_plan(
                state, frozen, market, events, calendar, inception, config
            )
        plan, status, prediction = None, "account_settled_no_forward_decision", None
        next_day = calendar[i + 1]
        industries = previous["industries"] if previous else {}
        cutoff = pd.Timestamp(asof).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=16)
        clock = pd.Timestamp(serving.now())
        timely = clock.tz_convert("Asia/Shanghai").date() == asof and clock <= cutoff
        if is_paused(root):
            status = "paused_accounting_only"
        elif timely:
            try:
                _, model = serving.selected_model(registry, asof)
                age = (asof - pd.Timestamp(model["fit"]["fit_asof"]).date()).days
                if age > service["max_model_age_days"]:
                    raise ValueError("active model expired; retrain and explicitly activate")
                if model.get("feature_contract_sha256") != service["feature_contract_sha256"]:
                    raise ValueError("active model feature contract mismatch")
                if config_from(model["config"]).fingerprint != config.fingerprint:
                    raise ValueError("active model/service policy mismatch")
                signal = root / "signals" / str(asof)
                if signal.exists():
                    if not (signal / "published.json").exists():
                        record_publication(signal, asof, serving.now)
                    verify_publication(signal, asof)
                    prediction = json.loads((signal / "prediction.json").read_text())
                    if (
                        prediction["features_sha256"] != sha256(inputs / "features.parquet")
                        or prediction["model_id"] != model["model_id"]
                    ):
                        raise ValueError("existing signal differs from today's input/model")
                else:
                    prediction = serving.predict_day(
                        registry,
                        inputs / "features.parquet",
                        inputs / "calendar.json",
                        asof,
                        signal,
                        code=code,
                        verify_code=verify_code,
                    )
                verify_publication(signal, asof)
                if prediction["model_id"] != model["model_id"]:
                    raise ValueError("active model changed during daily processing")
                scores = pd.read_parquet(signal / "scores.parquet")
                universe = pd.read_parquet(
                    inputs / "features.parquet",
                    columns=["trade_date", "instrument_id", "eligible", "industry"],
                )
                plan = plan_orders(
                    state["book"],
                    state["marks"],
                    state["corporate_state"],
                    scores,
                    universe,
                    calendar,
                    next_day,
                    (i - first) % config.rebalance_sessions == 0,
                    config,
                )
                industries = plan["industries"]
                status = "decision_ready"
            except (ValueError, FileNotFoundError) as exc:
                status = f"decision_blocked:{exc}"
        # The account is allowed to settle even when prediction is blocked.
        if read_inputs(inputs, asof, service)[0] != manifest:
            raise ValueError("daily evidence changed during processing")
        state["corporate_state"] = capture_entitlements(
            state["corporate_state"], state["book"], events
        )
        work = root / ".work" / uuid4().hex
        work.mkdir(parents=True)
        for name in (*INPUT_FILES, "manifest.json"):
            shutil.copyfile(inputs / name, work / name)
        if read_inputs(work, asof, service)[0] != manifest:
            raise ValueError("daily evidence changed during snapshot copy")
        if verify_code is not None and verify_code() != code:
            raise ValueError("code/runtime changed during daily processing")
        checkpoint_write(
            work / "account.json",
            {
                "state": state,
                "record": record,
                "settlement": decision,
                "plan": plan,
                "industries": industries,
                "status": status,
                "inputs": input_hash,
                "parent": parent,
                "code": code,
                "calendar": list(calendar),
                "events": event_map,
                "service_sha256": sha256(root / "completed.json"),
                "signal_sha256": sha256(root / "signals" / str(asof) / "completed.json")
                if prediction
                else None,
            },
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        publish_ready(work, target, asof, serving.now)
        return decision_status(root, asof, status)


def decision_status(root, asof, status):
    receipt = verify_publication(root / "sessions" / str(asof), asof, require_forward=False)
    return {
        "asof": str(asof),
        "status": status,
        "forward_decision": status == "decision_ready" and receipt["forward_eligible"],
        "published_at": receipt["published_at"],
        "execution_authority": False,
    }


def inspect_service(root):
    service = load_service(root)
    state, _, previous = account_head(root, service)
    result = {
        "schema": service["schema"],
        "asof": str(state["book"].asof_date),
        "cash_fen": state["book"].cash_fen,
        "lots": len(state["book"].lots),
        "paused": is_paused(root),
        "sessions": len(list((root / "sessions").glob("*"))),
        "execution_authority": False,
    }
    if previous:
        result.update(decision_status(root, state["book"].asof_date, previous["status"]))
    else:
        result.update(status="initialized_awaiting_first_decision", forward_decision=False)
    clock = pd.Timestamp(serving.now()).tz_convert("Asia/Shanghai")
    sessions = (
        previous["calendar"] if previous else [date.fromisoformat(d) for d in service["calendar"]]
    )
    due = [d for d in sessions if d < clock.date() or (d == clock.date() and clock.hour >= 16)]
    result["expected_session"] = str(due[-1]) if due else None
    result["stale"] = bool(due and state["book"].asof_date < due[-1])
    result["calendar_expired"] = sessions[-1] < clock.date()
    failures = [json.loads(p.read_text()) for p in (root / "failures").glob("*.json")]
    if failures:
        latest = max(failures, key=lambda e: e["recorded_at"])
        # Retain the audit history; only surface unresolved failures as current alerts.
        if previous is None or pd.Timestamp(latest["recorded_at"]) > pd.Timestamp(
            result["published_at"]
        ):
            result["latest_failure"] = latest
    return result
