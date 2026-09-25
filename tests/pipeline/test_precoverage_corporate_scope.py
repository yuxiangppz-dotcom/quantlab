"""Old account-independent actions can be scoped only with bounded evidence."""

import hashlib
import importlib.util
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts/scope_csi800_precoverage_actions.py"
_SPEC = importlib.util.spec_from_file_location("precoverage_scope", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_precoverage_scoping_keeps_issue_and_never_scopes_current_action(tmp_path):
    facts = []
    issues = []
    for code, record, delivered in (
        ("000403.SZ", "20130108", "20130208"),
        ("000703.SZ", "20140516", "20140519"),
        ("600176.SH", "20140418", "20140820"),
        ("600537.SH", "20141204", "20150129"),
    ):
        source = tmp_path / f"{code}.pdf"
        source.write_bytes(code.encode())
        raw = tmp_path / f"{code}.parquet"
        pd.DataFrame(
            [
                {
                    "div_proc": "实施",
                    "record_date": record,
                    "ex_date": None,
                    "div_listdate": delivered,
                }
            ]
        ).to_parquet(raw)
        facts.append(
            {
                "instrument_id": code,
                "record_date": record,
                "record_date_iso": f"{record[:4]}-{record[4:6]}-{record[6:]}",
                "completed_by": f"{delivered[:4]}-{delivered[4:6]}-{delivered[6:]}",
                "source_file": source.name,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "vendor_rows_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
                "status": "issuer_verified_completed_precoverage",
            }
        )
        issues.append(
            {
                "instrument_id": code,
                "record_date": record,
                "ex_date": None,
                "reason": "missing_ex_date",
            }
        )
    old_source = tmp_path / "600556.SH.pdf"
    old_source.write_bytes(b"issuer says old shareholders receive no new shares")
    old_raw = tmp_path / "600556.SH.parquet"
    pd.DataFrame(
        [
            {
                "div_proc": "实施",
                "end_date": "20081218",
                "record_date": None,
                "ex_date": None,
                "div_listdate": "20130208",
            }
        ]
    ).to_parquet(old_raw)
    facts.append(
        {
            "instrument_id": "600556.SH",
            "record_date": None,
            "record_date_iso": None,
            "end_date": "20081218",
            "completed_by": "2013-02-08",
            "source_file": old_source.name,
            "source_sha256": hashlib.sha256(old_source.read_bytes()).hexdigest(),
            "vendor_rows_sha256": hashlib.sha256(old_raw.read_bytes()).hexdigest(),
            "status": "issuer_verified_completed_precoverage",
        }
    )
    issues.append(
        {
            "instrument_id": "600556.SH",
            "record_date": None,
            "ex_date": None,
            "reason": "missing_ex_date",
        }
    )
    current = {
        "instrument_id": "002192.SZ",
        "record_date": "20180424",
        "ex_date": None,
        "reason": "missing_ex_date",
    }
    prior = {
        "instrument_id": "002131.SZ",
        "record_date": "20171213",
        "ex_date": None,
        "reason": "missing_ex_date",
    }
    prior_source = tmp_path / "002131.SZ.pdf"
    prior_source.write_bytes(b"issuer says recipients are December record holders")
    prior_raw = tmp_path / "002131.SZ.parquet"
    pd.DataFrame([{"div_proc": "实施", "record_date": "20171213"}]).to_parquet(prior_raw)
    prior_fact = {
        "instrument_id": "002131.SZ",
        "record_date": "2017-12-13",
        "source_file": prior_source.name,
        "source_sha256": hashlib.sha256(prior_source.read_bytes()).hexdigest(),
        "vendor_rows_sha256": hashlib.sha256(prior_raw.read_bytes()).hexdigest(),
        "status": "issuer_verified_account_flat_on_record_date",
        "vendor_row_count": 1,
        "issue_count": 1,
        "reason": "missing_ex_date",
    }
    doc = {
        "events": [{"instrument_id": "000403.SZ", "record_date": "2020-01-01"}],
        "coverage": {"start": "2017-01-01", "unresolved": [*issues, prior, current]},
    }
    result, scoped = _MODULE.scope(doc, {"facts": facts}, tmp_path, tmp_path)
    assert result["coverage"]["unresolved"] == [prior, current]
    assert [x["issue"] for x in scoped] == issues
    assert doc["coverage"]["unresolved"] == [*issues, prior, current]
    assert result["events"] == doc["events"]

    cash_source = tmp_path / "600699.SH.pdf"
    cash_source.write_bytes(b"issuer confirms precoverage cash paid")
    cash_raw = tmp_path / "600699.SH.parquet"
    pd.DataFrame(
        [
            {
                "div_proc": "实施",
                "end_date": "19931231",
                "imp_ann_date": "19940401",
                "record_date": None,
                "ex_date": None,
                "cash_div_tax": 0.12,
            }
        ]
    ).to_parquet(cash_raw)
    cash_fact = {
        "instrument_id": "600699.SH",
        "end_date": "19931231",
        "imp_ann_date": "19940401",
        "cash_div_tax": 0.12,
        "paid_by": "1994-06-30",
        "source_file": cash_source.name,
        "source_sha256": hashlib.sha256(cash_source.read_bytes()).hexdigest(),
        "vendor_rows_sha256": hashlib.sha256(cash_raw.read_bytes()).hexdigest(),
        "status": "issuer_verified_paid_precoverage_cash",
    }
    cash_issue = {
        "instrument_id": "600699.SH",
        "record_date": None,
        "ex_date": None,
        "reason": "missing_ex_date",
    }
    cash_doc = {
        **doc,
        "coverage": {**doc["coverage"], "unresolved": [*doc["coverage"]["unresolved"], cash_issue]},
    }
    cash_result, cash_scoped = _MODULE.scope(
        cash_doc,
        {"facts": facts, "historical_cash_facts": [cash_fact]},
        tmp_path,
        tmp_path,
    )
    assert cash_result["coverage"]["unresolved"] == [prior, current]
    assert cash_scoped[-1]["classification"] == (
        "issuer_verified_cash_paid_before_corporate_coverage_start"
    )
    with pytest.raises(ValueError, match="historical cash overlaps coverage"):
        _MODULE.scope(
            cash_doc,
            {"facts": facts, "historical_cash_facts": [{**cash_fact, "paid_by": "2018-01-01"}]},
            tmp_path,
            tmp_path,
        )

    # The 1992 entitlement is outside the modeled account despite the
    # vendor record/ex fields being empty. Keep the raw issue in scoped audit.
    legacy_issue = {
        "instrument_id": "600602.SH",
        "record_date": None,
        "ex_date": None,
        "reason": "missing_ex_date",
    }
    legacy_raw = tmp_path / "600602.SH.parquet"
    pd.DataFrame(
        [
            {
                "div_proc": "实施",
                "end_date": "19911231",
                "imp_ann_date": "19920329",
                "record_date": None,
                "ex_date": None,
                "cash_div_tax": 10.0,
            }
        ]
    ).to_parquet(legacy_raw)
    listings = []
    for name, url, body in (
        (
            "sina",
            "https://money.finance.sina.com.cn/corp/view/vISSUE_ShareBonusDetail.php?end_date=1992-03-29&stockid=600602&type=1",
            b"1992-03-09 100.00",
        ),
        ("aichagu", "https://m.aichagu.com/fh/600602.html", b"1992-03-06 1992-03-09 100"),
    ):
        source = tmp_path / f"600602-{name}.html"
        source.write_bytes(body)
        listings.append(
            {
                "name": name,
                "file": source.name,
                "url": url,
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    legacy_fact = {
        "instrument_id": "600602.SH",
        "status": "cross_source_verified_preinception_record_date",
        "external_record_date": "1992-03-06",
        "external_ex_date": "1992-03-09",
        "end_date": "19911231",
        "imp_ann_date": "19920329",
        "cash_div_tax": 10.0,
        "sources": listings,
        "vendor_rows_sha256": hashlib.sha256(legacy_raw.read_bytes()).hexdigest(),
    }
    legacy_doc = {
        **cash_doc,
        "coverage": {
            **cash_doc["coverage"],
            "unresolved": [*cash_doc["coverage"]["unresolved"], legacy_issue],
        },
    }
    all_facts = {
        "facts": facts,
        "historical_cash_facts": [cash_fact],
        "flat_inception_facts": [prior_fact],
        "cross_source_flat_inception_facts": [legacy_fact],
    }
    legacy_result, legacy_scoped = _MODULE.scope(
        legacy_doc,
        all_facts,
        tmp_path,
        tmp_path,
        date(2018, 1, 1),
    )
    assert legacy_result["coverage"]["unresolved"] == [current]
    assert legacy_scoped[-1]["issue"] == legacy_issue
    assert legacy_scoped[-1]["issuer_source_available"] is False
    assert legacy_result["coverage"]["minimum_replay_date"] == "2018-01-01"
    with pytest.raises(ValueError, match="historical listing changed"):
        _MODULE.scope(
            legacy_doc,
            {
                **all_facts,
                "cross_source_flat_inception_facts": [
                    {**legacy_fact, "sources": [{**listings[0], "sha256": "0" * 64}, listings[1]]}
                ],
            },
            tmp_path,
            tmp_path,
            date(2018, 1, 1),
        )
    with pytest.raises(ValueError, match="cross-source event overlaps"):
        _MODULE.scope(
            legacy_doc,
            {
                **all_facts,
                "cross_source_flat_inception_facts": [
                    {**legacy_fact, "external_ex_date": "2018-03-09"}
                ],
            },
            tmp_path,
            tmp_path,
            date(2018, 1, 1),
        )

    late = [*facts]
    late[0] = {**late[0], "completed_by": "2017-02-08"}
    with pytest.raises(ValueError, match="delivery date changed"):
        _MODULE.scope(doc, {"facts": late}, tmp_path, tmp_path)
    tampered = [*facts]
    tampered[0] = {**tampered[0], "source_sha256": "0" * 64}
    with pytest.raises(ValueError, match="issuer source changed"):
        _MODULE.scope(doc, {"facts": tampered}, tmp_path, tmp_path)
    with pytest.raises(ValueError, match="already scoped"):
        _MODULE.scope(result, {"facts": facts}, tmp_path, tmp_path)

    scoped_result, scoped_issues = _MODULE.scope(
        doc,
        {"facts": facts, "flat_inception_facts": [prior_fact]},
        tmp_path,
        tmp_path,
        date(2018, 1, 1),
    )
    assert scoped_result["coverage"]["unresolved"] == [current]
    assert len(scoped_issues) == len(issues) + 1
    assert scoped_result["coverage"]["minimum_replay_date"] == "2018-01-01"
    from quantlab.research.ml.io import read_corporate_actions

    artifact = tmp_path / "corporate.json"
    scoped_result["coverage"].update({"source_id": "fixture", "end": "2019-01-01"})
    artifact.write_text(json.dumps({**scoped_result, "events": []}))
    with pytest.raises(ValueError, match="later flat-account inception"):
        read_corporate_actions(artifact, date(2017, 12, 13), date(2017, 12, 13))
    assert read_corporate_actions(artifact, date(2018, 1, 2), date(2018, 1, 2)) == ()
    with pytest.raises(ValueError, match="record date is not before flat inception"):
        _MODULE.scope(
            doc,
            {"facts": facts, "flat_inception_facts": [prior_fact]},
            tmp_path,
            tmp_path,
            date(2017, 12, 13),
        )
