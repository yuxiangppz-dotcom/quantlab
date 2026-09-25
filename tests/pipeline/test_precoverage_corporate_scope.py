"""Old account-independent actions can be scoped only with bounded evidence."""

import hashlib
import importlib.util
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
        pd.DataFrame([{
            "div_proc": "实施", "record_date": record, "ex_date": None,
            "div_listdate": delivered,
        }]).to_parquet(raw)
        facts.append({
            "instrument_id": code, "record_date": record,
            "record_date_iso": f"{record[:4]}-{record[4:6]}-{record[6:]}",
            "completed_by": f"{delivered[:4]}-{delivered[4:6]}-{delivered[6:]}",
            "source_file": source.name,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "vendor_rows_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
            "status": "issuer_verified_completed_precoverage",
        })
        issues.append({
            "instrument_id": code, "record_date": record,
            "ex_date": None, "reason": "missing_ex_date",
        })
    current = {
        "instrument_id": "002192.SZ", "record_date": "20180424",
        "ex_date": None, "reason": "missing_ex_date",
    }
    doc = {
        "events": [{"instrument_id": "000403.SZ", "record_date": "2020-01-01"}],
        "coverage": {"start": "2017-01-01", "unresolved": [*issues, current]},
    }
    result, scoped = _MODULE.scope(doc, {"facts": facts}, tmp_path, tmp_path)
    assert result["coverage"]["unresolved"] == [current]
    assert [x["issue"] for x in scoped] == issues
    assert doc["coverage"]["unresolved"] == [*issues, current]
    assert result["events"] == doc["events"]

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
