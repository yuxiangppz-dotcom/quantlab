from pathlib import Path


def test_forward_shadow_contract_document_exists() -> None:
    path = Path("docs/forward_shadow_contract.md")
    text = path.read_text(encoding="utf-8")
    assert "exact `target_count`" in text
    assert "residual capital left as cash" in text
