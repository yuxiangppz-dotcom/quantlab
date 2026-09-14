import pathlib
import sys
import tempfile

sys.path.insert(0, "tests/research")
sys.path.insert(0, "scripts")

import s4_admission_independent_verify as verifier  # noqa: E402
from test_s4_admission_verifier import (  # noqa: E402
    TestProducerToVerifierContract,
)

contract = TestProducerToVerifierContract()
tmp = pathlib.Path(tempfile.mkdtemp())
source, canonical, output, frozen = contract._generate(tmp, type("M", (), {"setattr": staticmethod(lambda o, k, v: setattr(o, k, v)), "undo": staticmethod(lambda: None)})())
payload = verifier.verify(output, source, canonical, frozen=frozen, expected_gap_count=6)
for key, value in payload["checks"].items():
    if not value["ok"]:
        print("FAIL:", key, "|", value["detail"][:260])
