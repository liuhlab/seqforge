"""``kb`` — the executable, self-testing knowledge base.

One directory per technology under ``specs/`` (``spec.yaml`` + ``README.md``). Each spec validates
against :class:`~seqforge.kb.schema.Spec`, generates its own synthetic fixtures, and round-trips
through the probe. ``KB_VERSION`` (CalVer) is folded into dataset-level cache keys.
"""

from __future__ import annotations

from .generate import build_pools, generate_reads
from .loader import list_spec_ids, load_all_specs, load_spec
from .roundtrip import run_roundtrip
from .schema import Spec

#: CalVer YYYY.M.PATCH; bump when spec semantics change. Folded into dataset candidate cache keys.
#: 2026.7.2 — bulk-rnaseq-pe <-> splitseq declared processing_divergent, distinguishable_by onlist.
#: Found by the new rung-0-2 separability guard on its first run: the generic paired-end fallback
#: accepts SPLiT-seq's cdna+bc pair on geometry alone and had declared nothing.
#: 2026.7.1 — the parse/count line: soloFeatures / quantMode / outSAMtype left backend.params,
#: which now declares ONLY byte-decided parse keys. Also adds the 10x-3p-gex-v3.1 benign twin.
KB_VERSION = "2026.7.2"

__all__ = [
    "KB_VERSION",
    "Spec",
    "load_spec",
    "load_all_specs",
    "list_spec_ids",
    "generate_reads",
    "build_pools",
    "run_roundtrip",
]
