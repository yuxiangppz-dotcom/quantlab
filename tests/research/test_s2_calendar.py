import json
from datetime import date

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.s2_calendar import DAYS, joint_window, parse_calendar, profile, requests_for


def raw_calendar(exchange="SSE"):
    # Weekday fixture is synthetic, explicitly not the real Chinese holiday schedule.
    return {
        "code": 0,
        "data": {
            "fields": ["exchange", "cal_date", "is_open"],
            "items": [[exchange, d.strftime("%Y%m%d"), int(d.weekday() < 5)] for d in DAYS],
        },
    }


@pytest.mark.parametrize("encoding", ["integer", "string"])
def test_documented_state_encodings_and_reverse_provider_order(encoding):
    x = raw_calendar()
    if encoding == "string":
        for row in x["data"]["items"]:
            row[2] = str(row[2])
    x["data"]["items"].reverse()
    parsed = parse_calendar(json.dumps(x).encode(), "SSE")
    assert parsed == tuple(d for d in DAYS if d.weekday() < 5)


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "duplicate",
        "wrong_exchange",
        "outside",
        "state",
        "boolean",
        "float",
        "null",
        "extra_field",
        "has_more",
        "total",
        "provider_error",
        "bad_row",
    ],
)
def test_incomplete_or_ambiguous_calendar_rejected(defect):
    x = raw_calendar()
    d = x["data"]
    if defect == "missing":
        d["items"].pop()
    elif defect == "duplicate":
        d["items"][0] = d["items"][1][:]
    elif defect == "wrong_exchange":
        d["items"][0][0] = "SZSE"
    elif defect == "outside":
        d["items"][0][1] = "20260910"
    elif defect in ("state", "boolean", "float", "null"):
        d["items"][0][2] = {"state": "open", "boolean": True, "float": 1.0, "null": None}[defect]
    elif defect == "extra_field":
        d["fields"].append("other")
    elif defect == "has_more":
        d["has_more"] = True
    elif defect == "total":
        d["total"] = 52
    elif defect == "provider_error":
        x["code"] = 2002
    else:
        d["items"][0].pop()
    raw = json.dumps(x).encode()
    with pytest.raises(DataValidationError):
        parse_calendar(raw, "SSE")
    assert profile(raw, requests_for()[0])["status"] == "schema_or_provider_error"


def test_joint_window_uses20_successors_not20_calendar_days():
    s = tuple(d for d in DAYS if d.weekday() < 5)
    w = joint_window(s, s)
    assert len(w) == 21 and w[0] == date(2026, 9, 14)
    assert w[-1] == date(2026, 10, 12)  # synthetic weekday-only fixture


@pytest.mark.parametrize("defect", ["disagree", "entry_closed", "short", "duplicate", "unordered"])
def test_never_substitutes_weekdays_or_one_exchange(defect):
    s = tuple(d for d in DAYS if d.weekday() < 5)
    other = s
    if defect == "disagree":
        other = s[:-1]
    elif defect == "entry_closed":
        s = other = tuple(d for d in s if d != date(2026, 9, 14))
    elif defect == "short":
        s = other = s[:20]
    elif defect == "duplicate":
        s = (*s, s[-1])
    else:
        s = tuple(reversed(s))
    with pytest.raises(DataValidationError):
        joint_window(s, other)
