"""Content-addressed identity for the two artifacts and for the run that pairs them (R7/R13).

Three hashes, because there are three things worth identifying and they have different lifetimes:

- :func:`dataset_content_hash` — over ``library`` + ``experiment``. **Invariant under any processing
  change**, which is the entire point of the split: re-running a dataset with a different aligner must
  not perturb what the dataset *is*.
- :func:`processing_content_hash` — over the intent + its dataset pin.
- :func:`run_id` — ``H(dataset ⊕ processing ⊕ kb ⊕ workflow)``. The pairing is recorded **here**, at
  compile time, and never inside either input. That is what lets one processing manifest stay a
  portable template across 10^4 datasets while each pairing still gets a distinct identity.

Neither content hash covers its own ``provenance``, which carries it.

**Why this shape, and not the old one.** ``provenance_id(manifest_hash, kb, workflow)`` folded intent
into the manifest hash, so it could not express "one dataset, N processing manifests" — the two runs
collided on a single id, and the composer's fixed output path meant the second silently overwrote the
first. The collision case was exactly the use case the split exists for.
"""

from __future__ import annotations

import hashlib
import json

from ..models.dataset import DatasetManifest
from ..models.processing import ProcessingManifest


def _canonical(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def dataset_content_hash(manifest: DatasetManifest) -> str:
    """Deterministic sha256 over the dataset's two truth sections (canonical JSON).

    Note what is NOT folded in: ``PROBE_VERSION``. This hashes serialized *values*, so a probe change
    that alters an observed value changes the hash (and a processing manifest pinned to the old one
    correctly refuses); a probe refactor that changes nothing observable leaves it identical (and the
    pin still resolves). Stamping the probe version in here would invalidate every pin on a no-op
    refactor — the version belongs in the ``.seqforge/`` cache key, where it already is.
    """
    return _canonical(
        {
            "library": manifest.library.model_dump(mode="json"),
            "experiment": manifest.experiment.model_dump(mode="json"),
        }
    )


def processing_content_hash(processing: ProcessingManifest) -> str:
    """Deterministic sha256 over the processing intent + its dataset pin (canonical JSON)."""
    return _canonical(
        {
            "processing_id": processing.processing_id,
            "dataset": processing.dataset.model_dump(mode="json") if processing.dataset else None,
            "processing": processing.processing.model_dump(mode="json"),
        }
    )


def run_id(
    *, dataset_hash: str, processing_hash: str, kb_version: str, workflow_version: str
) -> str:
    """``H(dataset ⊕ processing ⊕ kb ⊕ workflow)`` — one run's content-addressed identity.

    This is where the split pays: one dataset x N processing manifests = N distinct run ids over ONE
    stable dataset hash. Keying the pipeline output directory by this is what stops the second
    compose of a dataset from overwriting the first.
    """
    key = f"{dataset_hash}|proc={processing_hash}|kb={kb_version}|wf={workflow_version}"
    return hashlib.sha256(key.encode()).hexdigest()


__all__ = ["dataset_content_hash", "processing_content_hash", "run_id"]
