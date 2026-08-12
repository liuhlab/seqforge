"""Content-addressed identity for the two artifacts and for the run that pairs them.

Four hashes, because there are four things worth identifying and they have different lifetimes:

- :func:`dataset_content_hash` — over ``library`` + ``experiment``. **Invariant under any processing
  change**, which is the entire point of the split: re-running a dataset with a different aligner must
  not perturb what the dataset *is*.
- :func:`processing_content_hash` — over the intent + its dataset pin.
- :func:`spec_content_hash` — the knowledge base's contribution to a run's identity, narrowed from a
  repository-wide version string to the one spec that decided this config, and inside that spec to the
  half a config is a function of. What the narrowing buys is that recognising a chemistry and
  processing it stop sharing an invalidation: a signature retuned so the classifier reads the bytes
  better, or a fixed alias, decides nothing that was not already decided, and may no longer cost every
  dataset in the corpus the directory its alignments sit in.
- :func:`run_id` — ``H(dataset ⊕ processing ⊕ kb ⊕ workflow)``. The pairing is recorded **here**, at
  compile time, and never inside either input. That is what lets one processing manifest stay a
  portable template across 10^4 datasets while each pairing still gets a distinct identity.

Neither artifact's content hash covers its own ``provenance``, which carries it.

**Why this shape, and not the old one.** ``provenance_id(manifest_hash, kb, workflow)`` folded intent
into the manifest hash, so it could not express "one dataset, N processing manifests" — the two runs
collided on a single id, and the composer's fixed output path meant the second silently overwrote the
first. The collision case was exactly the use case the split exists for.
"""

from __future__ import annotations

import hashlib
import json

from ..kb.schema import Spec
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


def spec_content_hash(spec: Spec) -> str:
    """Deterministic sha256 over the PROCESSING half of one knowledge-base entry (canonical JSON).

    What survives the exclusion is exactly what a compile reads: ``backend``, ``reads``, ``onlists``,
    ``min_input_reads``, ``read_through`` and ``identity.{id, modality, sample_is_cell}``. What is
    dropped belongs to *recognising* a chemistry rather than to running one — ``signature``,
    ``confusable_with``, ``parent``, ``node_kind``, ``children_decided_by``, ``read_sets``, and the
    naming half of ``identity`` — and by the time anything asks for this hash the chemistry is already
    decided and written into a manifest, so no byte of the emitted config is a function of any of it.

    Spelled as an EXCLUSION and never as an inclusion, at the nested ``identity`` level too. A field
    added to ``Spec`` later is then hashed by default, so the cost of forgetting to classify one is a
    pipeline directory that moves when it need not — never a config that changes underneath an
    identity that did not, which is the failure nothing downstream can detect.

    ``identity.descriptive_aliases`` is dropped with the rest of the naming half although it is the
    newest name among them: only the matcher that picks a spec out of prose reads it, never ``compose``
    and never a workflow module, so excluding it cannot under-invalidate — and an alias fix is
    precisely the edit that has to leave every compiled directory where it is.
    """
    return _canonical(
        spec.model_dump(
            mode="json",
            exclude={
                "schema_version": True,
                "signature": True,
                "confusable_with": True,
                "parent": True,
                "node_kind": True,
                "children_decided_by": True,
                "read_sets": True,
                "identity": {"name", "version", "aliases", "descriptive_aliases", "assay_ontology"},
            },
        )
    )


def run_id(
    *, dataset_hash: str, processing_hash: str, spec_hash: str, workflow_version: str
) -> str:
    """``H(dataset ⊕ processing ⊕ kb ⊕ workflow)`` — one run's content-addressed identity.

    This is where the split pays: one dataset x N processing manifests = N distinct run ids over ONE
    stable dataset hash. Keying the pipeline output directory by this is what stops the second
    compose of a dataset from overwriting the first.

    The knowledge-base component is a :func:`spec_content_hash` and no longer the repository-wide
    ``KB_VERSION``, which is why the key string says ``spec=``: it is a different fact under the same
    heading, and landing that re-keyed every dataset in the corpus once, deliberately.
    """
    key = f"{dataset_hash}|proc={processing_hash}|spec={spec_hash}|wf={workflow_version}"
    return hashlib.sha256(key.encode()).hexdigest()


__all__ = ["dataset_content_hash", "processing_content_hash", "run_id", "spec_content_hash"]
