"""Save fixed public primary sources with an immutable byte-level receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

MAX_BYTES = 20_000_000


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def intake(manifest: Path, output: Path) -> None:
    if output.exists():
        raise FileExistsError(output)
    raw_manifest = manifest.read_bytes()
    specification = json.loads(raw_manifest)
    if specification.get("schema") != "quantlab_public_source_intake_v1":
        raise ValueError("unsupported source manifest")
    sources = specification["sources"]
    if not sources or len({s["name"] for s in sources}) != len(sources):
        raise ValueError("nonempty unique source names required")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".csi800-source-", dir=output.parent) as tmp:
        stage = Path(tmp)
        receipt = {
            "schema": "quantlab_public_source_receipt_v1",
            "manifest_sha256": _sha(raw_manifest),
            "script_sha256": _sha(Path(__file__).read_bytes()),
            "sources": [],
        }
        for source in sources:
            name, url = source["name"], source["url"]
            if Path(name).name != name or name in {".", ".."}:
                raise ValueError("source name must be one filename")
            if urlparse(url).scheme != "https":
                raise ValueError("only HTTPS public sources are accepted")
            request = Request(url, headers={"User-Agent": "QuantLab evidence intake/1.0"})
            with urlopen(request, timeout=30) as response:
                if response.status != 200 or urlparse(response.url).scheme != "https":
                    raise ValueError(f"source fetch failed:{name}")
                raw = response.read(MAX_BYTES + 1)
                if not raw or len(raw) > MAX_BYTES:
                    raise ValueError(f"source empty or too large:{name}")
                detail = {
                    **source,
                    "final_url": response.url,
                    "http_status": response.status,
                    "content_type": response.headers.get("Content-Type"),
                    "downloaded_at_utc": datetime.now(UTC).isoformat(),
                    "sha256": _sha(raw),
                    "bytes": len(raw),
                }
            (stage / name).write_bytes(raw)
            receipt["sources"].append(detail)
        (stage / "receipt.json").write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        stage.rename(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    intake(args.manifest, args.output)
