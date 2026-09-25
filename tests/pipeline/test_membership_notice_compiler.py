"""The official-event compiler must not convert later names into earlier members."""

import importlib.util
from datetime import date
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/compile_csi800_membership_from_notices.py"
spec = importlib.util.spec_from_file_location("membership_notice_compiler", SCRIPT)
compiler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compiler)


def test_successor_code_only_applies_after_issuer_effective_day():
    changes = [
        {
            "old_instrument_id": "300114.SZ",
            "new_instrument_id": "302132.SZ",
            "effective_date": date(2025, 2, 17),
        }
    ]
    assert compiler.normalize_observed("302132.SZ", date(2023, 12, 29), changes) == "300114.SZ"
    assert compiler.normalize_observed("302132.SZ", date(2025, 2, 17), changes) == "302132.SZ"


def test_notice_publication_requires_identified_official_notice():
    notices = {"15044": {"publishDate": "2023-11-24"}}
    assert compiler.publication("v4-csi:15044", date(2023, 12, 11), notices, {}) == date(
        2023, 11, 24
    )
    with pytest.raises(ValueError, match="absent from full official index"):
        compiler.publication("v4-csi:99999", date(2023, 12, 11), notices, {})
