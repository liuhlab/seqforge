"""Confusability helpers — the ``§12`` benign rule and its ``backend_identical`` biconditional.

Two technologies are **processing-equivalent** iff, after resolving every ``{onlist:alias}`` to its
registry name and normalizing key order + list order **and** the read->role placement, their
``backend.params`` canonical forms are byte-equal. Including role placement matters: two techs that
differ only in *which* read is biological must not be called benign. The CI biconditional is
``backend_identical(A, B) <=> declared processing_equivalent`` (§2.4); this module provides the
``backend_identical`` primitive and the declared-relationship lookups the resolver consults at
runtime to decide a benign record-both vs a divergent tie.
"""

from __future__ import annotations

import json

from ..kb.schema import Spec


def _role_placement(spec: Spec) -> list[str]:
    """Canonical biological/technical read ordering (``readFilesIn`` order is cDNA read first)."""
    kinds: list[str] = []
    for read in spec.reads:
        el_types = {el.type for el in read.elements}
        if el_types & {"cdna", "gdna"}:
            kinds.append(f"bio:{read.strand}")
        elif "barcode" in el_types:
            kinds.append("barcode")
        else:
            kinds.append("other")
    return sorted(kinds)


def canonical_backend(spec: Spec) -> str:
    """A canonical, onlist-resolved, role-aware serialization of a spec's ``backend``."""
    resolved: dict[str, object] = {}
    for key, value in spec.backend.params.items():
        resolved[key] = _resolve_value(value, spec)
    payload = {
        "module": spec.backend.module,
        "params": resolved,
        "placement": _role_placement(spec),
    }
    return json.dumps(payload, sort_keys=True)


def _resolve_value(value: object, spec: Spec) -> object:
    if isinstance(value, str):
        return _resolve_token(value, spec)
    if isinstance(value, list):
        # normalize list order so soloFeatures=[Gene,GeneFull] == [GeneFull,Gene]
        return sorted(_resolve_token(v, spec) if isinstance(v, str) else v for v in value)
    return value


def _resolve_token(value: str, spec: Spec) -> str:
    if value.startswith("{onlist:") and value.endswith("}"):
        alias = value[len("{onlist:") : -1]
        ref = spec.onlists.get(alias)
        return f"registry:{ref.registry}" if ref else value
    return value


def backend_identical(a: Spec, b: Spec) -> bool:
    """True iff two specs compile to byte-equal, onlist-resolved, role-aware backends."""
    return canonical_backend(a) == canonical_backend(b)


def declared_equivalents(spec: Spec) -> set[str]:
    """Ids the spec declares as ``processing_equivalent`` twins (benign, ``§12`` record-both)."""
    return {c.id for c in spec.confusable_with if c.relationship == "processing_equivalent"}


def declared_divergent(spec: Spec) -> set[str]:
    """Ids the spec declares as ``processing_divergent`` (a real disagreement to decide)."""
    return {c.id for c in spec.confusable_with if c.relationship == "processing_divergent"}


def is_processing_equivalent(a: Spec, b_id: str) -> bool:
    """Does ``a`` declare ``b_id`` as a processing-equivalent twin?"""
    return b_id in declared_equivalents(a)
