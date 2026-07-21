"""``kb`` — the executable, self-testing knowledge base.

One directory per technology under ``specs/`` (``spec.yaml`` + ``README.md``). Each spec validates
against :class:`~seqforge.kb.schema.Spec`, generates its own synthetic fixtures, and round-trips
through the probe. ``KB_VERSION`` (CalVer) is folded into dataset-level cache keys.
"""

from __future__ import annotations

from .generate import build_pools, generate_reads
from .loader import (
    KbTree,
    build_tree,
    list_spec_ids,
    load_all_specs,
    load_spec,
    load_tree,
    runnable_spec_ids,
)
from .roundtrip import run_roundtrip
from .schema import Spec

#: CalVer YYYY.M.PATCH; bump when spec semantics change. Folded into dataset candidate cache keys.
#: 2026.7.3 — 10x 3' v2/v3/v3.1 accept an OVER-LENGTH barcode read (R1 max_len null +
#: segment_length over_length_min 100) and add soloBarcodeReadLength:0; v2<->v3 and v2<->v3.1 declared
#: processing_divergent (whitelist-decided) for the over-length case length can no longer separate.
#: (over_length_min is 100, not 40, so a 60-94 bp cDNA/split-pool read is not mistaken for an
#: over-sequenced barcode read -> the rung-0-2 separability guard stays green without over-declaring.)
#: 2026.7.2 — bulk-rnaseq-pe <-> splitseq declared processing_divergent, distinguishable_by onlist.
#: Found by the new rung-0-2 separability guard on its first run: the generic paired-end fallback
#: accepts SPLiT-seq's cdna+bc pair on geometry alone and had declared nothing.
#: 2026.7.1 — the parse/count line: soloFeatures / quantMode / outSAMtype left backend.params,
#: which now declares ONLY byte-decided parse keys. Also adds the 10x-3p-gex-v3.1 benign twin.
#: 2026.7.4 — added the bd-rhapsody-wta spec (BD Rhapsody WTA, original fixed-offset cell-label bead:
#: CB_UMI_Complex, three 97 x 9 bp CLS whitelists SHIPPED, two fixed linkers). bulk-rnaseq-pe <->
#: bd-rhapsody-wta declared processing_divergent, distinguishable_by onlist (same rung-0-2 collision
#: SPLiT-seq has with the generic paired-end fallback).
#: 2026.7.5 — the KB became a TREE: added the abstract family node 10x-3p-gex (node_kind: family, no
#: backend, children_decided_by: [onlist]) that recognizes v2/v3/v3.1 and rejects bulk; the three 10x
#: leaves gained parent: 10x-3p-gex and dropped their divergent-sibling confusable_with cliques (the
#: shared parent now implies them); v3<->v3.1 processing_equivalent edges kept. Descent narrows to a
#: length-feasible pool before scoring, so adding the Nth 10x chemistry is one parent link, not a clique.
KB_VERSION = "2026.7.5"

__all__ = [
    "KB_VERSION",
    "Spec",
    "KbTree",
    "load_spec",
    "load_all_specs",
    "load_tree",
    "build_tree",
    "list_spec_ids",
    "runnable_spec_ids",
    "generate_reads",
    "build_pools",
    "run_roundtrip",
]
