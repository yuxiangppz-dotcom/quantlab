from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantlab.personal.account import create_demo_account, import_account_csv, load_account

HEADER = (
    "account_id,account_mode,as_of,cash_cny,instrument_id,quantity,"
    "sellable_quantity,reference_cost_cny,open_orders_declaration\n"
)


def test_import_account_is_exact_idempotent_and_uses_fen(tmp_path: Path) -> None:
    raw = (
        HEADER + "mine,manual_tracking,2026-09-10T09:00:00+08:00,1234.56,"
        "000001.SZ,300,200,10.01,none_declared\n"
    ).encode()
    first = import_account_csv(raw, account_root=tmp_path)
    first_bytes = first.read_bytes()
    second = import_account_csv(raw, account_root=tmp_path)
    assert second.read_bytes() == first_bytes
    snapshot = load_account("mine", account_root=tmp_path)
    assert snapshot["cash_fen"] == 123456
    assert snapshot["positions"][0]["reference_cost_fen"] == 1001
    assert snapshot["positions"][0]["sellable_quantity"] == 200


@pytest.mark.parametrize(
    "body, message",
    [
        ("../bad,manual_tracking,2026-09-10T09:00:00+08:00,1,,0,0,,none_declared", "account_id"),
        ("mine,manual_tracking,2026-09-10 09:00:00,1,,0,0,,none_declared", "timezone"),
        ("mine,manual_tracking,2026-09-10T09:00:00+08:00,1.001,,0,0,,none_declared", "2 decimals"),
        (
            "mine,manual_tracking,2026-09-10T09:00:00+08:00,1,000001.SZ,100,101,10,none_declared",
            "invalid quantity",
        ),
    ],
)
def test_import_rejects_unsafe_or_financially_invalid_rows(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        import_account_csv((HEADER + body + "\n").encode(), account_root=tmp_path)


def test_duplicate_instrument_rejected_without_overwriting_prior_snapshot(tmp_path: Path) -> None:
    valid = (
        HEADER + "mine,manual_tracking,2026-09-10T09:00:00+08:00,1,"
        "000001.SZ,100,100,10,none_declared\n"
    ).encode()
    path = import_account_csv(valid, account_root=tmp_path)
    before = json.loads(path.read_text())
    duplicate = valid + (
        b"mine,manual_tracking,2026-09-10T09:00:00+08:00,1,000001.SZ,100,100,10,none_declared\n"
    )
    with pytest.raises(ValueError, match="duplicate instrument"):
        import_account_csv(duplicate, account_root=tmp_path)
    assert json.loads(path.read_text()) == before


def test_stored_account_tampering_is_rejected(tmp_path: Path) -> None:
    raw = (
        HEADER + "mine,manual_tracking,2026-09-10T09:00:00+08:00,100.00,,0,0,,none_declared\n"
    ).encode()
    path = import_account_csv(raw, account_root=tmp_path)
    payload = json.loads(path.read_text())
    payload["cash_fen"] += 1
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        load_account("mine", account_root=tmp_path)


def test_demo_cash_is_user_adjustable_but_remains_demo_mode(tmp_path: Path) -> None:
    create_demo_account("demo", cash_cny="345678.90", account_root=tmp_path)
    account = load_account("demo", account_root=tmp_path)
    assert account["cash_fen"] == 34_567_890
    assert account["account_mode"] == "demo_simulation"


def test_replacing_basis_preserves_snapshots_and_old_journal(tmp_path: Path) -> None:
    raw = (
        HEADER + "mine,manual_tracking,2026-09-10T09:00:00+08:00,100,,0,0,,none_declared\n"
    ).encode()
    path = import_account_csv(raw, account_root=tmp_path)
    old = path.read_bytes()
    old_fp = load_account("mine", account_root=tmp_path)["account_fingerprint"]
    journal = path.parent / "tracking" / old_fp / "journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("old-journal-kept-as-is")
    import_account_csv(raw.replace(b",100,", b",200,"), account_root=tmp_path)
    new = load_account("mine", account_root=tmp_path)
    assert new["cash_fen"] == 20000
    assert (path.parent / "snapshots" / f"{old_fp}.json").read_bytes() == old
    assert (
        path.parent / "snapshots" / f"{new['account_fingerprint']}.json"
    ).read_bytes() == path.read_bytes()
    assert journal.read_text() == "old-journal-kept-as-is"


def test_demo_cannot_replace_a_manual_account(tmp_path: Path) -> None:
    raw = (
        HEADER + "demo_200k,manual_tracking,2026-09-10T09:00:00+08:00,100,,0,0,,none_declared\n"
    ).encode()
    path = import_account_csv(raw, account_root=tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="account_mode cannot change"):
        create_demo_account(account_root=tmp_path)
    assert path.read_bytes() == before


def test_corrupt_archive_blocks_replacement_without_losing_active_basis(tmp_path: Path) -> None:
    raw = (
        HEADER + "mine,manual_tracking,2026-09-10T09:00:00+08:00,100,,0,0,,none_declared\n"
    ).encode()
    path = import_account_csv(raw, account_root=tmp_path)
    before = path.read_bytes()
    archive = next((path.parent / "snapshots").glob("*.json"))
    archive.write_text("{}")
    with pytest.raises(ValueError, match="archive conflicts"):
        import_account_csv(raw.replace(b",100,", b",200,"), account_root=tmp_path)
    assert path.read_bytes() == before
    assert archive.read_text() == "{}"


def test_legacy_unarchived_basis_preserved_on_first_replacement(tmp_path: Path) -> None:
    raw = (
        HEADER + "mine,manual_tracking,2026-09-10T09:00:00+08:00,100,,0,0,,none_declared\n"
    ).encode()
    path = import_account_csv(raw, account_root=tmp_path)
    before = path.read_bytes()
    archive = next((path.parent / "snapshots").glob("*.json"))
    archive.unlink()
    import_account_csv(raw.replace(b",100,", b",200,"), account_root=tmp_path)
    assert archive.read_bytes() == before
