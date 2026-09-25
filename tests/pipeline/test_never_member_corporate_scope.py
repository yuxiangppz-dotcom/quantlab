"""A flat CSI800 account cannot own a security absent from every prior session."""

import hashlib
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/scope_csi800_never_member_actions.py"
_SPEC = importlib.util.spec_from_file_location("never_member_scope", _SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_never_member_scope_requires_every_prior_trading_day_and_bound_sources(tmp_path):
    calendar = pd.DataFrame(
        [
            {"trade_date": day, "exchange": exchange, "is_open": day.weekday() < 5}
            for day in pd.date_range("2018-01-01", "2019-04-23")
            for exchange in ("SSE", "SZSE")
        ]
    )
    members = [f"{n:06d}.SZ" for n in range(1, 801)]
    membership = {
        "schema": "quantlab_index_membership_v1",
        "index": "000906.SH",
        "semantics": "published_effective_intervals",
        "snapshots": [
            {
                "start": "2018-01-01",
                "end": "2019-04-23",
                "members": members,
                "complete": True,
                "known_at": "2017-12-01T18:00:00+08:00",
                "source_id": "official_notice",
                "revision_id": "fixture",
            }
        ],
    }
    corporate = {
        "coverage": {
            "minimum_replay_date": "2018-01-01",
            "unresolved": [
                {
                    "instrument_id": "002192.SZ",
                    "record_date": d,
                    "ex_date": None,
                    "reason": "missing_ex_date",
                }
                for d in ("20180424", "20190423")
            ],
        },
        "events": [],
    }
    raw = tmp_path / "002192.SZ.parquet"
    pd.DataFrame(
        [
            {"div_proc": "实施", "record_date": record, "ex_date": None, "pay_date": pay}
            for record, pay in (("20180424", "20181017"), ("20190423", "20191025"))
        ]
    ).to_parquet(raw)
    raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
    facts = []
    for record, pay in (("2018-04-24", "2018-10-17"), ("2019-04-23", "2019-10-25")):
        source = tmp_path / f"issuer-{record}.pdf"
        source.write_bytes(record.encode())
        facts.append(
            {
                "instrument_id": "002192.SZ",
                "record_date": record,
                "vendor_pay_date": pay,
                "issuer_file": source.name,
                "issuer_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "vendor_rows_sha256": raw_hash,
                "classification": (
                    "restructuring_performance_cash_compensation_not_ordinary_dividend"
                ),
                "production_applied": False,
            }
        )
    review = {"facts": facts}
    bindings = {"membership": "m", "calendar": "c", "code_changes": "x"}

    result, scoped = _MODULE.scope(
        corporate, membership, calendar, [], review, tmp_path, tmp_path, bindings
    )
    assert result["coverage"]["unresolved"] == []
    assert len(scoped) == 2
    assert scoped[0]["verified_trading_sessions"] > 0
    assert scoped[1]["verified_trading_sessions"] > scoped[0]["verified_trading_sessions"]
    assert all(x["settlement_and_tax_certified"] is False for x in scoped)
    assert all(x["issue"] in corporate["coverage"]["unresolved"] for x in scoped)

    with pytest.raises(ValueError, match="account could acquire"):
        _MODULE.scope(
            corporate,
            {
                **membership,
                "snapshots": [
                    {**membership["snapshots"][0], "members": [*members[:-1], "002192.SZ"]}
                ],
            },
            calendar,
            [],
            review,
            tmp_path,
            tmp_path,
            bindings,
        )
    with pytest.raises(ValueError, match="missing or overlapping membership"):
        _MODULE.scope(
            corporate,
            {**membership, "snapshots": [{**membership["snapshots"][0], "end": "2019-04-22"}]},
            calendar,
            [],
            review,
            tmp_path,
            tmp_path,
            bindings,
        )
    with pytest.raises(ValueError, match="security identity changed"):
        _MODULE.scope(
            corporate,
            membership,
            calendar,
            [{"old_instrument_id": "002192.SZ", "new_instrument_id": "999999.SZ"}],
            review,
            tmp_path,
            tmp_path,
            bindings,
        )
    with pytest.raises(ValueError, match="compensation source changed"):
        bad_review = {"facts": [{**facts[0], "issuer_sha256": "0" * 64}, facts[1]]}
        _MODULE.scope(corporate, membership, calendar, [], bad_review, tmp_path, tmp_path, bindings)

    # A later membership for 002192 cannot create entitlement at the older
    # record dates. 600720 remains unreachable until its 2024 record date.
    later = pd.DataFrame(
        [
            {"trade_date": day, "exchange": exchange, "is_open": day.weekday() < 5}
            for day in pd.date_range("2019-04-24", "2024-06-12")
            for exchange in ("SSE", "SZSE")
        ]
    )
    extended_calendar = pd.concat([calendar, later], ignore_index=True)
    extended_membership = {
        **membership,
        "snapshots": [
            {**membership["snapshots"][0], "end": "2022-12-11"},
            {
                **membership["snapshots"][0],
                "start": "2022-12-12",
                "end": "2024-06-12",
                "members": [*members[:-1], "002192.SZ"],
            },
        ],
    }
    cash_raw = tmp_path / "600720.SH.parquet"
    pd.DataFrame(
        [
            {
                "div_proc": "实施",
                "record_date": "20240612",
                "ex_date": "20240613",
                "pay_date": "20240613",
                "cash_div_tax": value,
            }
            for value in (0.1104, 0.1104, 0.257)
        ]
    ).to_parquet(cash_raw)
    cash_source = tmp_path / "600720-issuer.html"
    cash_source.write_bytes(b"issuer reviewed")
    cash_fact = {
        "instrument_id": "600720.SH",
        "record_date": "2024-06-12",
        "ex_date": "2024-06-13",
        "pay_date": "2024-06-13",
        "status": "issuer_terms_verified_not_accounting_implemented",
        "source_file": cash_source.name,
        "source_sha256": hashlib.sha256(cash_source.read_bytes()).hexdigest(),
        "vendor_rows_sha256": hashlib.sha256(cash_raw.read_bytes()).hexdigest(),
        "expected_vendor_cash_rates_cny": ["0.1104", "0.1104", "0.257"],
    }
    cash_issue = {
        "instrument_id": "600720.SH",
        "record_date": "2024-06-12",
        "ex_date": "2024-06-13",
        "reason": "conflicting_duplicate",
    }
    cash_doc = {
        **corporate,
        "coverage": {
            **corporate["coverage"],
            "unresolved": [*corporate["coverage"]["unresolved"], cash_issue, cash_issue],
        },
    }
    cash_result, cash_scoped = _MODULE.scope(
        cash_doc,
        extended_membership,
        extended_calendar,
        [],
        review,
        tmp_path,
        tmp_path,
        bindings,
        {"facts": [cash_fact]},
    )
    assert cash_result["coverage"]["unresolved"] == []
    assert len(cash_scoped) == 4
    assert [x["issue"] for x in cash_scoped[-2:]] == [cash_issue, cash_issue]
    with pytest.raises(ValueError, match="account could acquire cash security"):
        _MODULE.scope(
            cash_doc,
            {
                **extended_membership,
                "snapshots": [
                    extended_membership["snapshots"][0],
                    {
                        **extended_membership["snapshots"][1],
                        "members": [*members[:-2], "002192.SZ", "600720.SH"],
                    },
                ],
            },
            extended_calendar,
            [],
            review,
            tmp_path,
            tmp_path,
            bindings,
            {"facts": [cash_fact]},
        )
