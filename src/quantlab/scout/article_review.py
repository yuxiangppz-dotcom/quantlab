"""Codex's source-bound daily review: no external model and no screenshots."""

from __future__ import annotations

from datetime import date, datetime

from quantlab.scout.models import SHANGHAI, fingerprint

MODE = "codex_browser_daily_v1"
DIMENSIONS = ("group_event", "setup", "lhb", "funds", "levels", "daily_visual", "peers")


def research_hash(research):
    return fingerprint(research)


def validate_review(document, research, *, now=None):
    """Collect all contract errors; never require flattering or numeric prose."""
    errors = []
    if not isinstance(document, dict):
        return [{"code": "review_object_required"}]
    if document.get("review_mode") != MODE or document.get("reviewer") != "main_agent":
        errors.append({"code": "codex_review_required"})
    if document.get("research_hash") != research_hash(research):
        errors.append({"code": "research_changed"})
    expected = {row["ts_code"]: row for row in research["deep"]}
    facts = {row["fact_id"]: row for row in research.get("facts", [])}
    dimension_sources = {
        "group_event": {"market", "group", "notices", "unlock", "risk_events"},
        "setup": {"setup", "metrics"},
        "lhb": {"lhb"},
        "funds": {"moneyflow"},
        "levels": {"levels", "entry"},
        "daily_visual": {"recent_chart", "metrics"},
        "peers": {"qualified_peer", "metrics", "setup"},
    }
    rows = document.get("reviews")
    if not isinstance(rows, list):
        return errors + [{"code": "review_list_required"}]
    seen = set()
    clock = now or datetime.now(SHANGHAI)
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("ts_code"), str):
            errors.append({"code": "stock_subject_required"})
            continue
        code = row["ts_code"]
        if code not in expected or code in seen:
            errors.append({"code": "unexpected_or_duplicate_stock", "ts_code": code})
            continue
        seen.add(code)
        stock = expected[code]
        if row.get("decision") not in ("priority", "watch", "reject"):
            errors.append({"code": "decision_required", "ts_code": code})
        for key in ("rationale", "next_day_hypothesis", "strongest_counter"):
            if not isinstance(row.get(key), str) or not row[key].strip() or len(row[key]) > 1600:
                errors.append({"code": "review_text_required", "ts_code": code, "field": key})
        peers = row.get("peer_codes", [])
        if not isinstance(peers, list) or any(peer not in stock["peer_codes"] for peer in peers):
            errors.append({"code": "unqualified_peer", "ts_code": code})
        dims = row.get("dimensions", {})
        if not isinstance(dims, dict):
            dims = {}
        for key in DIMENSIONS:
            value = dims.get(key)
            if not isinstance(value, dict):
                errors.append({"code": "dimension_missing", "ts_code": code, "field": key})
                continue
            if value.get("effect") not in ("retain", "downgrade", "reject", "unknown"):
                errors.append({"code": "dimension_effect", "ts_code": code, "field": key})
            refs = value.get("fact_ids", [])
            if not isinstance(refs, list) or any(ref not in stock["fact_ids"] for ref in refs):
                errors.append({"code": "fact_scope", "ts_code": code, "field": key})
            else:
                for ref in refs:
                    evidence = facts.get(ref) if isinstance(ref, str) else None
                    if evidence is None or evidence["dimension"] not in dimension_sources[key]:
                        errors.append({"code": "fact_dimension", "ts_code": code, "field": key})
                    elif (
                        key in {"setup", "lhb", "funds", "levels", "daily_visual"}
                        and evidence["subject"] != code
                    ):
                        errors.append({"code": "fact_subject", "ts_code": code, "field": key})
                    elif key == "peers" and evidence["subject"] not in stock["peer_codes"]:
                        errors.append({"code": "fact_subject", "ts_code": code, "field": key})
            for field in ("support", "counter", "unknown"):
                if not isinstance(value.get(field), str) or len(value[field]) > 1600:
                    errors.append({"code": "dimension_text", "ts_code": code, "field": key})
        daily = row.get("daily_review", {})
        if not isinstance(daily, dict):
            daily = {}
        symbol = code.rsplit(".", 1)[1] + code.split(".", 1)[0]
        if daily.get("source_url") not in (
            f"https://www.xueqiu.com/S/{symbol}",
            f"https://xueqiu.com/S/{symbol}",
        ):
            errors.append({"code": "daily_source", "ts_code": code})
        if daily.get("observed_code") != code or daily.get("view") != "daily":
            errors.append({"code": "daily_subject_or_view", "ts_code": code})
        if daily.get("status") not in ("complete", "partial", "unavailable"):
            errors.append({"code": "daily_status", "ts_code": code})
        if daily.get("quality", "unknown") not in ("good", "mixed", "weak", "unknown"):
            errors.append({"code": "daily_quality", "ts_code": code})
        dates = daily.get("visible_dates", [])
        try:
            if not isinstance(dates, list):
                raise ValueError
            parsed = [date.fromisoformat(day) for day in dates]
            if any(day > date.fromisoformat(research["signal_date"]) for day in parsed):
                raise ValueError
        except (ValueError, TypeError):
            errors.append({"code": "daily_future_or_invalid_date", "ts_code": code})
        if daily.get("status") == "complete" and (
            not isinstance(dates, list) or research["signal_date"] not in dates
        ):
            errors.append({"code": "daily_dates_unknown", "ts_code": code})
        try:
            observed = datetime.fromisoformat(daily["observed_at"])
            cutoff = datetime.fromisoformat(research.get("cutoff_at", "2020-01-01T00:00:00+08:00"))
            if observed.tzinfo is None or observed > clock or observed < cutoff:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            errors.append({"code": "daily_observation_time", "ts_code": code})
        visual = dims.get("daily_visual")
        visual = visual if isinstance(visual, dict) else {}
        support = visual.get("support")
        if daily.get("status") != "complete" and (
            visual.get("effect") == "retain"
            or isinstance(support, str)
            and support.strip()
            or daily.get("quality", "unknown") != "unknown"
        ):
            errors.append({"code": "incomplete_daily_not_support", "ts_code": code})
    for code in expected.keys() - seen:
        errors.append({"code": "review_missing", "ts_code": code})
    return errors


def bind_review(document, research):
    errors = validate_review(document, research)
    if errors:
        raise ValueError("Codex review errors: " + str(errors))
    return {**document, "calls": 0, "tokens": 0, "errors": [], "external_model_calls": 0}
