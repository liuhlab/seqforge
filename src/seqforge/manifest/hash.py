"""Canonical manifest hashing + the provenance id that binds a config to its inputs (R7).

The manifest hash is computed over the three *truth* sections only (``library`` / ``experiment`` /
``processing``) — never over ``provenance`` itself, which carries the hash. ``provenance_id`` folds in
the KB and workflow versions so a compiled config is reproducible and diffable across machines/years.
"""

from __future__ import annotations

import hashlib
import json

from ..models.manifest import Manifest


def manifest_content_hash(manifest: Manifest) -> str:
    """Deterministic sha256 over the manifest's three truth sections (canonical JSON)."""
    payload = {
        "library": manifest.library.model_dump(mode="json"),
        "experiment": manifest.experiment.model_dump(mode="json"),
        "processing": manifest.processing.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def provenance_id(manifest_hash: str, kb_version: str, workflow_version: str) -> str:
    """``H(manifest_hash ⊕ kb_version ⊕ workflow_version)`` — the run's content-addressed identity."""
    key = f"{manifest_hash}|kb={kb_version}|wf={workflow_version}"
    return hashlib.sha256(key.encode()).hexdigest()
