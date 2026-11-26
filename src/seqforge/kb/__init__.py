"""``kb`` — the executable, self-testing knowledge base.

One directory per technology under ``specs/`` (``spec.yaml`` + ``README.md``). Each spec validates
against :class:`~seqforge.kb.schema.Spec`, generates its own synthetic fixtures, and round-trips
through the probe. ``KB_VERSION`` (CalVer) is folded into dataset-level cache keys.
"""

from __future__ import annotations

from .generate import all_cdna_spec, build_pools, generate_reads
from .loader import (
    KbTree,
    build_tree,
    list_spec_ids,
    load_all_specs,
    load_spec,
    load_tree,
    runnable_spec_ids,
)
from .match import curated_forms, resolve_chemistry, resolve_chemistry_id
from .roundtrip import roundtrip_checks, run_roundtrip
from .schema import Spec

#: CalVer YYYY.M.PATCH; bump when spec semantics change.
#: History: `docs/research/kb-version-history-2026-11-25.md`.
KB_VERSION = "2026.8.8"

__all__ = [
    "KB_VERSION",
    "KbTree",
    "Spec",
    "all_cdna_spec",
    "build_pools",
    "build_tree",
    "curated_forms",
    "generate_reads",
    "list_spec_ids",
    "load_all_specs",
    "load_spec",
    "load_tree",
    "resolve_chemistry",
    "resolve_chemistry_id",
    "roundtrip_checks",
    "run_roundtrip",
    "runnable_spec_ids",
]
