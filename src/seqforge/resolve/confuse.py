"""Confusability helpers — the ``§12`` benign rule and its ``backend_identical`` biconditional.

Two technologies are **processing-equivalent** iff, after resolving every ``{onlist:alias}`` to its
registry name and normalizing key order **and** the read->role placement, their ``backend.params``
canonical forms are byte-equal. Including role placement matters: two techs that differ only in
*which* read is biological must not be called benign. The CI biconditional is
``backend_identical(A, B) <=> declared processing_equivalent`` (§2.4); this module provides the
``backend_identical`` primitive and the declared-relationship lookups the resolver consults at
runtime to decide a benign record-both vs a divergent tie.

Since counting moved out of ``backend.params``, this predicate means exactly *"these two
chemistries parse reads identically"* — which is what ``processing_equivalent`` should have meant all
along, and it makes the rule **stronger**, not weaker: two specs differing only in what they count are
no longer distinguishable here, because that difference is no longer a chemistry fact at all. It is
the processing manifest's to make, per dataset.

**List order is significant** and is never normalized; see :func:`_resolve_value`.
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
    """A canonical, onlist-resolved, role-aware serialization of a spec's ``backend``.

    An ABSTRACT family node has no backend, so it canonicalizes to a per-id sentinel: no two nodes
    share it, so a classifier is never ``backend_identical`` to — and thus never a false
    processing-equivalent twin of — a leaf.
    """
    if spec.backend is None:
        return json.dumps({"abstract_node": spec.identity.id}, sort_keys=True)
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
        # ORDER IS PRESERVED, and it must be. This used to sort, justified by exactly one comment:
        # "normalize list order so soloFeatures=[Gene,GeneFull] == [GeneFull,Gene]". soloFeatures has
        # since moved to the processing manifest (it says what to COUNT, not how to parse), and
        # with it the only reason the sort existed.
        #
        # What it would sort NOW is the only list-valued parse param left: splitseq's
        # `soloCBwhitelist: [round1, round2, round3]` — which is POSITIONAL. The rounds map to CB
        # positions in order. Sorting it made `backend_identical` return True for a spec against
        # itself-with-rounds-permuted: two chemistries that parse reads DIFFERENTLY, declared byte-
        # equal, hence §12-benign, hence one config emitted for both. It never fired only by the
        # alphabetical accident that round1 < round2 < round3.
        return [_resolve_token(v, spec) if isinstance(v, str) else v for v in value]
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


def accepts_at_rungs_0_2(spec: Spec, probes: list[object]) -> bool:
    """Would ``spec`` claim this data using only the CHEAP probes — no onlist, no network?

    The onlist is withheld by handing the evaluator an **empty registry**, so every
    ``onlist_hit_rate`` test abstains and the verdict rests on geometry, segmentation, distinct-value
    ratios and header grammar alone. That is precisely rungs 0-2 (§5), expressed by removing the
    rung-3 evidence rather than by reimplementing the scorer without it.

    This is the primitive behind :func:`rung02_separable`, and it is why "ask the human" can be a
    computed property instead of a prompt hope.
    """
    from ..io import OnlistRegistry
    from .scoring import build_tech_evaluation
    from .window import WindowProbe

    wps = [p for p in probes if isinstance(p, WindowProbe)]
    return build_tech_evaluation(spec, wps, OnlistRegistry(offline=True)).valid


def rung02_separable(a: Spec, a_probes: list[object], b: Spec, b_probes: list[object]) -> bool:
    """Do the cheap probes tell these two chemistries apart at all? (design §2.4, fact 1)

    Separable iff **neither** spec accepts the other's data on geometry alone. If A would happily
    claim B's reads, no amount of scoring rigour separates them below rung 3 — the honest thing is
    for the KB to *say so* via ``confusable_with``, so the resolver knows to reach for the onlist or
    a human rather than picking the alphabetically-luckier entry.

    Some distinctions are provably undecidable from reads (10x 3' and 5' share CB/UMI geometry;
    inDrop v2 and v3 share oligos). The system must KNOW that rather than guess, which is the whole
    point of computing this instead of hand-maintaining a truth table.
    """
    return not (accepts_at_rungs_0_2(a, b_probes) or accepts_at_rungs_0_2(b, a_probes))


def declared_equivalents(spec: Spec) -> set[str]:
    """Ids the spec declares as ``processing_equivalent`` twins (benign, ``§12`` record-both)."""
    return {c.id for c in spec.confusable_with if c.relationship == "processing_equivalent"}


def declared_divergent(spec: Spec) -> set[str]:
    """Ids the spec declares as ``processing_divergent`` (a real disagreement to decide)."""
    return {c.id for c in spec.confusable_with if c.relationship == "processing_divergent"}


def is_processing_equivalent(a: Spec, b_id: str) -> bool:
    """Does ``a`` declare ``b_id`` as a processing-equivalent twin?"""
    return b_id in declared_equivalents(a)


# ---- tree-sourced confusability: siblings replace hand-declared divergent cliques ----
def share_parent(specs: dict[str, Spec], a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` are siblings — the same non-null parent in the KB tree."""
    pa = specs[a].parent if a in specs else None
    pb = specs[b].parent if b in specs else None
    return pa is not None and pa == pb


def is_tree_kin(specs: dict[str, Spec], a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` are parent-child or siblings — a confusability the tree DECLARES.

    A divergent sibling clique (v2/v3/v3.1) collapses to one ``parent`` link, so the under-declaration
    guard treats tree kin the way it treats an explicit ``confusable_with`` edge: already declared.
    """
    if a not in specs or b not in specs:
        return False
    if specs[a].parent == b or specs[b].parent == a:
        return True
    return share_parent(specs, a, b)


def sibling_decided_by(specs: dict[str, Spec], a: str, b: str) -> list[str]:
    """If ``a`` and ``b`` are siblings, the mechanisms their parent declares separate its children.

    This is where the divergent-tie question now reads ``decidable_by`` from — the parent's
    ``children_decided_by`` — instead of the per-sibling ``distinguishable_by`` edge that was deleted.
    """
    if not share_parent(specs, a, b):
        return []
    parent = specs[a].parent
    if parent is None or parent not in specs:
        return []
    return [m for m in specs[parent].children_decided_by if m != "none"]
