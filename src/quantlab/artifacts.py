"""Domain-neutral formal artifact publication API.

Backtest-specific inventories and semantic validators intentionally remain in
``quantlab.backtest.artifacts``. New domains should import the contract,
publisher, and verifier from this module and supply their own contract.
"""

from quantlab.backtest.artifacts import (
    ARTIFACT_MANIFEST,
    COMPLETION_MARKER,
    INCOMPLETE_MARKER,
    ArtifactContract,
    ArtifactPublisher,
    atomic_write_json,
    atomic_write_text,
    sha256_file,
    stream_csv,
    verify_formal_artifact,
)

__all__ = [
    "ARTIFACT_MANIFEST",
    "COMPLETION_MARKER",
    "INCOMPLETE_MARKER",
    "ArtifactContract",
    "ArtifactPublisher",
    "atomic_write_json",
    "atomic_write_text",
    "sha256_file",
    "stream_csv",
    "verify_formal_artifact",
]
