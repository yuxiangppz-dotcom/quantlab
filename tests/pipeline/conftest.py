"""Load the real evidence CLI without executing its main function."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def evidence_builder():
    path = Path(__file__).resolve().parents[2] / "scripts/build_csi800_evidence.py"
    spec = importlib.util.spec_from_file_location("csi800_evidence_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cached_classifications():
    def write(root):
        folder = root / "evidence/raw/provider_archive/index_classify"
        folder.mkdir(parents=True, exist_ok=True)
        for taxonomy in ("SW2014", "SW2021"):
            frame = pd.DataFrame({"index_code": ["801780.SI", "801750.SI"],
                                  "industry_name": ["bank", "software"]})
            payload = frame.to_json(orient="split", date_format="iso", force_ascii=False)
            (folder / f"{taxonomy}.json").write_text(json.dumps({
                "endpoint": "index_classify", "parameters": {"level": "L1", "src": taxonomy},
                "observed_at": "2026-09-19T00:00:00+00:00", "row_count": len(frame),
                "response_sha256": hashlib.sha256(payload.encode()).hexdigest(),
                "response": json.loads(payload), "historical_publication_certified": False,
            }))
        return folder
    return write
