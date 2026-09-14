import json
import pathlib
import sys
import tempfile

sys.path.insert(0, "tests/research")
sys.path.insert(0, "scripts")

from test_s4_admission_verifier import (  # noqa: E402
    _build_world,
    _hand_build_package,
    _write_completed,
)
import s4_admission_independent_verify as verifier  # noqa: E402

tmp = pathlib.Path(tempfile.mkdtemp())
source, canonical, plan, recon_body, frozen = _build_world(tmp)
out = tmp / "pkg"
_hand_build_package(out, plan, recon_body, source, canonical)
_write_completed(out)
payload = verifier.verify(out, source, canonical, frozen=frozen, expected_gap_count=6)
for key, value in payload["checks"].items():
    if not value["ok"]:
        print("FAIL:", key, "|", value["detail"][:300])
