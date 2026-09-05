from pathlib import Path

from quantlab.backtest.provenance import content_manifest, sha256_bytes, sha256_file


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_code_fingerprint_changes_on_modification(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    _write(a, "x = 1\n")
    _write(b, "y = 2\n")

    m1 = content_manifest([a, b], tmp_path)
    _write(a, "x = 2\n")
    m2 = content_manifest([a, b], tmp_path)

    assert m1["combined_sha256"] != m2["combined_sha256"]
    assert len(m1["files"]) == 2


def test_same_basename_different_paths_distinguished(tmp_path: Path) -> None:
    dir1 = tmp_path / "pkg1"
    dir2 = tmp_path / "pkg2"
    _write(dir1 / "mod.py", "VALUE = 1\n")
    _write(dir2 / "mod.py", "VALUE = 1\n")

    m1 = content_manifest([dir1 / "mod.py"], tmp_path)
    m2 = content_manifest([dir2 / "mod.py"], tmp_path)

    paths1 = {e["path"] for e in m1["files"]}
    paths2 = {e["path"] for e in m2["files"]}
    assert paths1 != paths2
    assert m1["combined_sha256"] != m2["combined_sha256"]


def test_input_change_detected(tmp_path: Path) -> None:
    data = tmp_path / "data.parquet"
    _write(data, "raw-content-v1")
    m1 = content_manifest([data], tmp_path)
    _write(data, "raw-content-v2")
    m2 = content_manifest([data], tmp_path)
    assert m1["combined_sha256"] != m2["combined_sha256"]


def test_sha256_helpers(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    _write(f, "abc")
    assert sha256_bytes(b"abc") == sha256_file(f)
    assert len(sha256_bytes(b"abc")) == 64
