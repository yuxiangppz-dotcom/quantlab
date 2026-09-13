"""A planned observation calendar, never evidence of future trading or prices."""

from datetime import date, timedelta

from quantlab.data.dividend_raw import strict_json
from quantlab.data.models import DataValidationError

CONFIG = "config/s2_observation_calendar_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/s2_observation_calendar"
DAYS = tuple(date(2026, 9, 11) + timedelta(days=i) for i in range(51))
OBSERVATION = "2b9a706a45d08443d9063ae180c7f8c5edd385a90144457d48ee0b28e14f105f"
PROOF = "e8ed5c36613c3316d705986f45d47a1a4ebb4f34317c1c8abd7111b77cd88ecc"


def requests_for():
    return [
        {
            "id": exchange,
            "parameters": {
                "api_name": "trade_cal",
                "params": {"exchange": exchange, "start_date": "20260911", "end_date": "20261031"},
                "fields": "exchange,cal_date,is_open",
            },
            "row_cap": 6000,
        }
        for exchange in ("SSE", "SZSE")
    ]


def parse_calendar(raw, exchange):
    try:
        obj = strict_json(raw)
        if exchange not in ("SSE", "SZSE") or type(obj["code"]) is not int or obj["code"] != 0:
            raise ValueError
        data = obj["data"]
        names, items = data["fields"], data["items"]
        if (
            not isinstance(names, list)
            or len(names) != 3
            or set(names) != {"exchange", "cal_date", "is_open"}
            or not isinstance(items, list)
            or len(items) != 51
        ):
            raise ValueError
        for part in (obj, data):
            if (
                part.get("has_more")
                or part.get("truncated")
                or isinstance(part.get("total"), (int, float))
                and part["total"] > 51
            ):
                raise ValueError
        states = {}
        for item in items:
            if not isinstance(item, list) or len(item) != 3:
                raise ValueError
            row = dict(zip(names, item, strict=True))
            day, state = row["cal_date"], row["is_open"]
            if (
                row["exchange"] != exchange
                or type(day) is not str
                or day in states
                or not (
                    (type(state) is int and state in (0, 1))
                    or (type(state) is str and state in ("0", "1"))
                )
            ):
                raise ValueError
            states[day] = int(state)
        if set(states) != {d.strftime("%Y%m%d") for d in DAYS}:
            raise ValueError
        return tuple(d for d in DAYS if states[d.strftime("%Y%m%d")] == 1)
    except (TypeError, KeyError, ValueError) as exc:
        raise DataValidationError("incomplete or invalid planned exchange calendar") from exc


def profile(raw, request):
    try:
        parse_calendar(raw, request["id"])
        return {"status": "nonempty", "rows": 51}
    except DataValidationError:
        return {"status": "schema_or_provider_error", "rows": None}


def joint_window(sse, szse):
    for days in (sse, szse):
        if (
            type(days) is not tuple
            or any(type(d) is not date or d not in DAYS for d in days)
            or tuple(sorted(set(days))) != days
        ):
            raise DataValidationError("planned sessions must be unique ordered explicit dates")
    entry = date(2026, 9, 14)
    if sse != szse or entry not in sse:
        raise DataValidationError("exchanges disagree or entry is not planned open")
    start = sse.index(entry)
    result = sse[start : start + 21]
    if len(result) != 21:
        raise DataValidationError("20 subsequent planned sessions unavailable")
    return result
