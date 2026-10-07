"""Typed, prompt-visible facts for bounded Scout market assertions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from quantlab.scout.technical import snapshot_facts


@dataclass(frozen=True)
class CoreClaim:
    field: str
    text: str
    subject_id: str
    metric: str
    period: str
    value: Decimal
    unit: str
    direction: str


def program_facts(candidate: dict, asof_session: str | None = None) -> list[dict]:
    """Describe only values carried by this candidate in the actual model packet."""
    code = candidate["instrument_id"]
    metrics = candidate.get("metrics") or {}
    summary = candidate.get("source_summary") or {}
    facts = []
    for key, period, unit in (
        ("return_1d", "1d", "ratio"),
        ("return_5d", "5d", "ratio"),
        ("amount_cny", "1d", "CNY"),
    ):
        value = metrics.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            facts.append(
                {
                    "fact_id": f"fact:{code}:market:{key}",
                    "subject_id": code,
                    "metric": key,
                    "period": period,
                    "asof_session": asof_session,
                    "value": str(value),
                    "unit": unit,
                    "source_location": f"candidate:{code}:metrics:{key}",
                }
            )
    moneyflow = summary.get("moneyflow") or {}
    for days in (1, 3, 5):
        key = f"net_{days}d_wan_cny"
        value = moneyflow.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            facts.append(
                {
                    "fact_id": f"fact:{code}:moneyflow:{key}",
                    "subject_id": code,
                    "metric": key,
                    "period": f"{days}d",
                    "asof_session": asof_session,
                    "value": str(value),
                    "unit": "wan_CNY",
                    "source_location": f"candidate:{code}:source_summary:moneyflow:{key}",
                }
            )
    for index, row in enumerate(summary.get("limit_history") or []):
        day = str(row.get("trade_date") or "")
        value = row.get("limit_times")
        if not re.fullmatch(r"\d{8}", day) or not isinstance(value, (int, float)):
            continue
        facts.append(
            {
                "fact_id": f"fact:{code}:limit:limit_times:{day}",
                "subject_id": code,
                "metric": "limit_times",
                "period": day,
                "asof_session": asof_session,
                "value": str(value),
                "unit": "boards",
                "source_location": f"candidate:{code}:source_summary:limit_history:{index}",
            }
        )
    for index, event in enumerate(candidate.get("opportunity_record", {}).get("events", [])):
        scale = event.get("nominal_scale", {})
        ratio = scale.get("nominal_ratio")
        if ratio is None:
            continue
        numerator, denominator = scale.get("numerator_cny"), scale.get("denominator_cny")
        if (
            not isinstance(numerator, (int, float))
            or not isinstance(denominator, (int, float))
            or denominator <= 0
            or numerator <= 0
            or ratio != numerator / denominator
        ):
            raise ValueError("Nominal event scale differs from positive denominator calculation")
        facts.append(
            {
                "fact_id": f"fact:{code}:event:nominal_contract_ratio:{event['record_id']}",
                "subject_id": code,
                "metric": "nominal_contract_ratio",
                "period": event["record_id"],
                "asof_session": asof_session,
                "value": str(ratio),
                "unit": "ratio",
                "source_location": f"candidate:{code}:opportunity_record:"
                f"events:{index}:nominal_scale",
            }
        )
    if "opportunity_record" in candidate:
        for key, period, unit in (
            ("return_20d", "20d", "ratio"),
            ("relative_return_1d", "1d", "ratio"),
            ("relative_return_5d", "5d", "ratio"),
            ("relative_return_20d", "20d", "ratio"),
            ("amount_ratio_5d", "5d", "times"),
            ("breakout_20d", "20d", "ratio"),
        ):
            value = metrics.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                facts.append(
                    {
                        "fact_id": f"fact:{code}:market:{key}",
                        "subject_id": code,
                        "metric": key,
                        "period": period,
                        "asof_session": asof_session,
                        "value": str(value),
                        "unit": unit,
                        "source_location": f"candidate:{code}:metrics:{key}",
                    }
                )
    facts.extend(snapshot_facts(candidate, asof_session))
    return facts


def fact_map(candidates: list[dict], asof_session: str | None = None) -> dict[str, dict]:
    facts = []
    for candidate in candidates:
        archived = candidate.get("program_facts")
        if archived is not None:
            if not isinstance(archived, list):
                raise ValueError("Prompt program fact table has invalid structure")
            visible_asof = archived[0].get("asof_session") if archived else asof_session
            expected = program_facts(candidate, visible_asof)
            if archived != expected:
                raise ValueError("Prompt program fact table differs from visible candidate values")
            facts.extend(archived)
        else:
            facts.extend(program_facts(candidate, asof_session))
    return {fact["fact_id"]: fact for fact in facts}


def _subject(text: str, start: int, own: dict, candidates: list[dict]) -> str:
    prefix = re.split(r"[。；，、]", text[:start])[-1][-32:]
    found = []
    for candidate in candidates:
        code = candidate["instrument_id"]
        names = [code, code[:6]]
        if len(candidate.get("name") or "") >= 2:
            names.append(candidate["name"])
        for name in names:
            pos = prefix.rfind(name)
            if pos >= 0:
                found.append((pos, code))
    for name in ("本股", "本公司", "该股"):
        pos = prefix.rfind(name)
        if pos >= 0:
            found.append((pos, own["instrument_id"]))
    return max(found)[1] if found else own["instrument_id"]


def extract_core_claims(
    field: str, text: str, own: dict, candidates: list[dict]
) -> list[CoreClaim]:
    """Parse only the four bounded quantitative domains; other prose stays inference."""
    claims = []
    periods = r"昨日|前一日|当日|近?1日|近?5日|五日|5日|日"
    if "opportunity_record" in own:
        periods = r"近?20日|二十日|" + periods
    returns = re.compile(
        rf"(?P<period>{periods})"
        r"(?:累计)?(?P<word>上涨|下跌|涨幅|跌幅|收益率?|涨|跌)"
        r"\s*(?:为|约|达)?\s*(?P<number>[+-]?\d+(?:\.\d+)?)\s*%"
    )
    for match in returns.finditer(text):
        period = (
            "20d"
            if "20" in match["period"] or "二十" in match["period"]
            else "5d"
            if "5" in match["period"] or "五" in match["period"]
            else "1d"
        )
        word = match["word"]
        value = Decimal(match["number"])
        direction = "neutral"
        if word in {"下跌", "跌幅", "跌"}:
            direction = "down"
            if not match["number"].startswith(("+", "-")):
                value = -value
        elif word in {"上涨", "涨幅", "涨"}:
            direction = "up"
        claims.append(
            CoreClaim(
                field,
                match.group(),
                _subject(text, match.start(), own, candidates),
                f"return_{period}",
                period,
                value,
                "%",
                direction,
            )
        )
    if "opportunity_record" in own:
        for match in re.finditer(
            r"近?(?P<days>1|5|20)日相对行业价格差\s*(?:为|约)?\s*(?P<number>[+-]?\d+(?:\.\d+)?)\s*%",
            text,
        ):
            subject = _subject(text, match.start(), own, candidates)
            claims.append(
                CoreClaim(
                    field,
                    match.group(),
                    subject,
                    f"relative_return_{match['days']}d",
                    f"{match['days']}d",
                    Decimal(match["number"]),
                    "%",
                    "neutral",
                )
            )
        for match in re.finditer(
            r"(?:额比|量比|成交额相对前5日均值)\s*(?:为|约)?\s*(?P<number>[+-]?\d+(?:\.\d+)?)\s*倍",
            text,
        ):
            claims.append(
                CoreClaim(
                    field,
                    match.group(),
                    _subject(text, match.start(), own, candidates),
                    "amount_ratio_5d",
                    "5d",
                    Decimal(match["number"]),
                    "times",
                    "neutral",
                )
            )
        for match in re.finditer(
            r"突破前20日高点幅度\s*(?:为|约)?\s*(?P<number>[+-]?\d+(?:\.\d+)?)\s*%", text
        ):
            claims.append(
                CoreClaim(
                    field,
                    match.group(),
                    _subject(text, match.start(), own, candidates),
                    "breakout_20d",
                    "20d",
                    Decimal(match["number"]),
                    "%",
                    "neutral",
                )
            )
    for match in re.finditer(
        r"(?:昨日|前一日|当日)?成交额\s*(?:约|为)?\s*"
        r"(?P<number>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>亿元|万元|元)",
        text,
    ):
        claims.append(
            CoreClaim(
                field,
                match.group(),
                _subject(text, match.start(), own, candidates),
                "amount_cny",
                "1d",
                Decimal(match["number"]),
                match["unit"],
                "neutral",
            )
        )
    for match in re.finditer(
        r"(?P<days>[135])日(?:供应商口径)?(?:.{0,8}?)?"
        r"(?:资金|大单)?净流(?P<direction>入|出)\s*(?:约|为)?"
        r"(?P<number>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>亿元|万元|元)",
        text,
    ):
        value = Decimal(match["number"])
        direction = "in" if match["direction"] == "入" else "out"
        if direction == "out" and not match["number"].startswith(("+", "-")):
            value = -value
        days = match["days"]
        claims.append(
            CoreClaim(
                field,
                match.group(),
                _subject(text, match.start(), own, candidates),
                f"net_{days}d_wan_cny",
                f"{days}d",
                value,
                match["unit"],
                direction,
            )
        )
    for match in re.finditer(r"(?:连续|第)(?P<number>\d+)(?:个?涨停|板)", text):
        subject = _subject(text, match.start(), own, candidates)
        history = next(
            (
                c.get("source_summary", {}).get("limit_history") or []
                for c in candidates
                if c["instrument_id"] == subject
            ),
            [],
        )
        day = str(history[-1].get("trade_date") or "") if history else "unknown"
        claims.append(
            CoreClaim(
                field,
                match.group(),
                subject,
                "limit_times",
                day,
                Decimal(match["number"]),
                "boards",
                "neutral",
            )
        )
    for match in re.finditer(
        r"名义合同金额占年收入\s*(?:约|为)?\s*(?P<number>[+-]?\d+(?:\.\d+)?)\s*%", text
    ):
        subject = _subject(text, match.start(), own, candidates)
        events = next(
            (
                c.get("opportunity_record", {}).get("events", [])
                for c in candidates
                if c["instrument_id"] == subject
            ),
            [],
        )
        known = [e for e in events if e.get("nominal_scale", {}).get("nominal_ratio") is not None]
        explicit = [e for e in known if e["record_id"] in text[: match.start()]]
        event = explicit[-1] if explicit else known[0] if len(known) == 1 else None
        claims.append(
            CoreClaim(
                field,
                match.group(),
                subject,
                "nominal_contract_ratio",
                event["record_id"] if event else "unknown",
                Decimal(match["number"]),
                "%",
                "neutral",
            )
        )
    return claims


def claim_fact_id(claim: CoreClaim) -> str:
    if claim.metric == "nominal_contract_ratio":
        return f"fact:{claim.subject_id}:event:nominal_contract_ratio:{claim.period}"
    if claim.metric == "limit_times":
        return f"fact:{claim.subject_id}:limit:limit_times:{claim.period}"
    if claim.metric.startswith("net_"):
        return f"fact:{claim.subject_id}:moneyflow:{claim.metric}"
    return f"fact:{claim.subject_id}:market:{claim.metric}"


def check_core_claim(claim: CoreClaim, facts: dict[str, dict]) -> tuple[str, object] | None:
    """Compare subject, period, signed value and explicit unit, never numeric tokens."""
    if (claim.direction in {"up", "in"} and claim.value < 0) or (
        claim.direction in {"down", "out"} and claim.value > 0
    ):
        return "core_claim_internal_direction_conflict", claim.text
    fact_id = claim_fact_id(claim)
    fact = facts.get(fact_id)
    if fact is None:
        return "core_fact_missing", fact_id
    if fact["subject_id"] != claim.subject_id or fact["period"] != claim.period:
        return "core_fact_wrong_object_or_period", fact_id
    if fact["unit"] == "ratio":
        actual = Decimal(fact["value"]) * 100
        if claim.unit != "%":
            return "core_fact_unit_mismatch", fact_id
    elif fact["unit"] in {"CNY", "wan_CNY"}:
        scale = {
            "CNY": {"元": 1, "万元": 10000, "亿元": 100000000},
            "wan_CNY": {"元": Decimal("0.0001"), "万元": 1, "亿元": 10000},
        }[fact["unit"]]
        if claim.unit not in scale:
            return "core_fact_unit_mismatch", fact_id
        actual = Decimal(fact["value"]) / Decimal(str(scale[claim.unit]))
    else:
        if fact["unit"] != claim.unit:
            return "core_fact_unit_mismatch", fact_id
        actual = Decimal(fact["value"])
    decimals = max(0, -claim.value.as_tuple().exponent)
    tolerance = Decimal(5).scaleb(-decimals - 1)
    if abs(actual - claim.value) > tolerance:
        return "core_fact_direction_or_value", {
            "fact_id": fact_id,
            "value": fact["value"],
            "unit": fact["unit"],
            "period": fact["period"],
        }
    return None


def validate_declared_claim(
    declared: dict, parsed: CoreClaim, facts: dict[str, dict]
) -> tuple[str, object] | None:
    """Require the structured model declaration to match both prose and prompt fact."""
    for key, expected in (
        ("field", parsed.field),
        ("text", parsed.text),
        ("subject_id", parsed.subject_id),
        ("metric", parsed.metric),
        ("period", parsed.period),
        ("unit", parsed.unit),
        ("direction", parsed.direction),
    ):
        if declared.get(key) != expected:
            return "core_claim_structure_mismatch", {key: expected}
    try:
        if Decimal(str(declared.get("value"))) != parsed.value:
            return "core_claim_structure_mismatch", {"value": str(parsed.value)}
    except (InvalidOperation, TypeError):
        return "core_claim_structure_mismatch", {"value": str(parsed.value)}
    expected_id = claim_fact_id(parsed)
    if declared.get("fact_id") != expected_id or expected_id not in facts:
        return "core_claim_fact_reference_mismatch", expected_id
    return check_core_claim(parsed, facts)
