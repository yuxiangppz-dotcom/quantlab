from datetime import date

import pytest

from quantlab.data.models import DataValidationError, Security, SecurityCodeChange
from quantlab.data.security_history import code_validity_interval, load_security_code_changes
from quantlab.research.dataset import _build_delist_dates, _build_list_dates


def _security(instrument_id, list_date, delist_date=None) -> Security:
    return Security(
        instrument_id=instrument_id,
        symbol=instrument_id.split(".")[0],
        name=instrument_id,
        exchange="SZSE",
        market="SZ",
        board="主板",
        list_status="L",
        list_date=list_date,
        delist_date=delist_date,
    )


def test_load_security_code_changes(tmp_path) -> None:
    csv = tmp_path / "changes.csv"
    csv.write_text(
        "old_instrument_id,new_instrument_id,effective_date,old_name,original_list_date,source,note\n"
        "000022.SZ,001872.SZ,2018-12-26,深赤湾Ａ,1993-05-05,s,n\n"
        "000043.SZ,001914.SZ,2019-12-16,中航善达,1994-09-28,s,n\n"
        "300114.SZ,302132.SZ,2025-02-17,中航电测,2010-08-27,s,n\n"
    )
    changes = load_security_code_changes(csv)
    assert len(changes) == 3
    assert changes[0].old_instrument_id == "000022.SZ"
    assert changes[0].new_instrument_id == "001872.SZ"
    assert changes[0].effective_date == date(2018, 12, 26)
    assert changes[0].original_list_date == date(1993, 5, 5)


def test_code_history_no_duplicate_old_code(tmp_path) -> None:
    csv = tmp_path / "changes.csv"
    csv.write_text(
        "old_instrument_id,new_instrument_id,effective_date,old_name,original_list_date,source,note\n"
        "000022.SZ,001872.SZ,2018-12-26,深赤湾Ａ,1993-05-05,s,n\n"
        "000043.SZ,001914.SZ,2019-12-16,中航善达,1994-09-28,s,n\n"
    )
    changes = load_security_code_changes(csv)
    old_ids = [c.old_instrument_id for c in changes]
    assert len(old_ids) == len(set(old_ids))


def _change() -> SecurityCodeChange:
    return SecurityCodeChange(
        old_instrument_id="000022.SZ",
        new_instrument_id="001872.SZ",
        effective_date=date(2018, 12, 26),
        old_name="深赤湾Ａ",
        original_list_date=date(1993, 5, 5),
    )


def test_code_validity_interval_clips_predecessor_and_successor():
    change = _change()
    assert code_validity_interval(change.old_instrument_id, [change])[1] == date(2018, 12, 25)
    assert code_validity_interval(change.new_instrument_id, [change])[0] == date(2018, 12, 26)
    assert code_validity_interval("600519.SH", [change]) == (date.min, date.max)


def test_old_code_valid_before_effective() -> None:
    securities = [_security("001872.SZ", date(1993, 5, 5))]
    changes = [_change()]
    list_dates = _build_list_dates(securities, changes)
    delist_dates = _build_delist_dates(securities, changes)
    assert list_dates["000022.SZ"] == date(1993, 5, 5)
    assert delist_dates["000022.SZ"] == date(2018, 12, 25)


def test_old_code_invalid_from_effective() -> None:
    securities = [_security("001872.SZ", date(1993, 5, 5))]
    changes = [_change()]
    delist_dates = _build_delist_dates(securities, changes)
    assert delist_dates["000022.SZ"] < date(2018, 12, 26)


def test_successor_code_valid() -> None:
    securities = [_security("001872.SZ", date(1993, 5, 5))]
    changes = [_change()]
    list_dates = _build_list_dates(securities, changes)
    delist_dates = _build_delist_dates(securities, changes)
    assert list_dates["001872.SZ"] == date(2018, 12, 26)
    assert delist_dates["001872.SZ"] is None


def test_old_and_new_not_merged() -> None:
    securities = [_security("001872.SZ", date(1993, 5, 5))]
    changes = [_change()]
    list_dates = _build_list_dates(securities, changes)
    assert "000022.SZ" in list_dates
    assert "001872.SZ" in list_dates
    assert list_dates["000022.SZ"] != list_dates["001872.SZ"]


_HEADER = (
    "old_instrument_id,new_instrument_id,effective_date,old_name,"
    "original_list_date,source,note\n"
)


def _write(tmp_path, *lines):
    csv = tmp_path / "changes.csv"
    csv.write_text(_HEADER + "\n".join(lines) + "\n")
    return csv


def test_duplicate_old_id_raises(tmp_path) -> None:
    csv = _write(
        tmp_path,
        "000022.SZ,001872.SZ,2018-12-26,深赤湾Ａ,1993-05-05,s,n",
        "000022.SZ,001999.SZ,2019-01-01,dup,1993-05-05,s,n",
    )
    with pytest.raises(DataValidationError):
        load_security_code_changes(csv)


def test_old_equals_new_raises(tmp_path) -> None:
    csv = _write(tmp_path, "000022.SZ,000022.SZ,2018-12-26,深赤湾Ａ,1993-05-05,s,n")
    with pytest.raises(DataValidationError):
        load_security_code_changes(csv)


def test_effective_before_original_raises(tmp_path) -> None:
    csv = _write(tmp_path, "000022.SZ,001872.SZ,1990-01-01,深赤湾Ａ,1993-05-05,s,n")
    with pytest.raises(DataValidationError):
        load_security_code_changes(csv)


def test_invalid_date_raises(tmp_path) -> None:
    csv = _write(tmp_path, "000022.SZ,001872.SZ,not-a-date,深赤湾Ａ,1993-05-05,s,n")
    with pytest.raises(DataValidationError):
        load_security_code_changes(csv)


def test_empty_instrument_raises(tmp_path) -> None:
    csv = _write(tmp_path, ",001872.SZ,2018-12-26,深赤湾Ａ,1993-05-05,s,n")
    with pytest.raises(DataValidationError):
        load_security_code_changes(csv)


def test_missing_column_raises(tmp_path) -> None:
    csv = tmp_path / "changes.csv"
    csv.write_text("old_instrument_id,new_instrument_id\n000022.SZ,001872.SZ\n")
    with pytest.raises(DataValidationError):
        load_security_code_changes(csv)


def test_code_changes_path_cwd_independent(tmp_path, monkeypatch) -> None:
    from quantlab.research.dataset import _CODE_CHANGES_PATH

    assert _CODE_CHANGES_PATH.is_absolute()
    monkeypatch.chdir(tmp_path)
    assert _CODE_CHANGES_PATH.exists()
    assert len(load_security_code_changes(_CODE_CHANGES_PATH)) == 3
