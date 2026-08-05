"""Confusability helpers — the benign-twin rule and its ``backend_identical`` biconditional.

Two technologies are **processing-equivalent** iff, after resolving every ``{onlist:alias}`` to its
registry name and normalizing key order **and** the read->role placement, their ``backend.params``
canonical forms are byte-equal. Including role placement matters: two techs that differ only in
*which* read is biological must not be called benign. The CI biconditional is
``backend_identical(A, B) <=> declared processing_equivalent``; this module provides the
``backend_identical`` primitive and the declared-relationship lookups the resolver consults at
runtime to decide a benign record-both vs a divergent tie.

Since counting moved out of ``backend.params``, this predicate means exactly *"these two
chemistries parse reads identically"* — which is what ``processing_equivalent`` should have meant all
along, and it makes the rule **stronger**, not weaker: two specs differing only in what they count are
no longer distinguishable here, because that difference is no longer a chemistry fact at all. It is
the processing manifest's to make, per dataset.

The other half of this module is the **rung-0-2** side of the same contract: whether the cheap probes
can order two chemistries at all. ``accepts_at_rungs_0_2`` answers "would this spec claim that data",
``could_outrank_at_rungs_0_2`` answers "could it WIN that data", and the CI under-declaration guard
asks the second — the danger a `confusable_with` edge exists to avert is an ordering claim, not a
feasibility one (#275; ADR-0029, the read-set record).

**List order is significant** and is never normalized; see :func:`_resolve_value`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

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
    # Fold in the DERIVED geometry (soloCB/UMIposition, soloAdapterSequence). Those are byte-decided
    # parse facts read off the element coordinates, not declared here — and they are exactly where two
    # chemistries with identical DECLARED params can still parse reads differently. The original BD bead
    # and the Enhanced-96 bead share soloType, whitelists and strand; only their geometry differs (fixed
    # offsets vs an adapter-anchored, diversity-insert-staggered frame). Comparing declared params alone
    # called them byte-identical -> benign -> one config for both. Local import: `compose` reads the
    # KB, so importing it at module load would knot resolve<->compose. (#43)
    from ..compose.params import derived_params

    resolved.update(derived_params(spec))
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
        # equal, hence benign, hence one config emitted for both. It never fired only by the
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


def accepts_at_rungs_0_2(spec: Spec, probes: Iterable[object]) -> bool:
    """Would ``spec`` claim this data using only the CHEAP probes — no onlist, no network?

    The onlist is withheld by handing the evaluator an **empty registry**, so every
    ``onlist_hit_rate`` test abstains and the verdict rests on geometry, segmentation, distinct-value
    ratios and header grammar alone. That is precisely rungs 0-2, expressed by removing the
    rung-3 evidence rather than by reimplementing the scorer without it.

    This is the primitive behind :func:`rung02_separable` and the *feasibility* half of
    :func:`could_outrank_at_rungs_0_2`, and it is why "ask the human" can be a computed property
    instead of a prompt hope. It is no longer the under-declaration guard's whole question: claiming
    the data and being able to WIN it are two facts, and only the second is a danger.
    """
    from ..io import OnlistRegistry
    from .scoring import build_tech_evaluation
    from .window import WindowProbe

    wps = [p for p in probes if isinstance(p, WindowProbe)]
    return build_tech_evaluation(spec, wps, OnlistRegistry(offline=True)).valid


def rung02_margin(a: Spec, b: Spec, b_probes: Iterable[object]) -> float | None:
    """``a``'s rung-0-2 score minus ``b``'s, both measured on ``b``'s OWN reads. ``None`` if ``a``
    scores nothing there (no valid injective assignment — an unscorable spec ranks nowhere).

    Positive means the challenger ``a`` beats the incumbent ``b`` on the incumbent's home ground,
    which is the number the under-declaration guard is really about; the guard's threshold on it is
    :func:`could_outrank_at_rungs_0_2`. Exposed separately because the *margin* is what an author has
    to be able to re-derive: a `confusable_with` edge that survives only by 0.001 of synthetic score
    is a different claim from one that survives by 0.45, and neither is legible from a boolean.

    Both sides are scored against the same probes with the onlist withheld — an empty offline
    registry, exactly as :func:`accepts_at_rungs_0_2` withholds it. Withholding it from BOTH is what
    makes the comparison fair rather than rigged: rung-3 evidence is precisely what a
    ``distinguishable_by: [onlist]`` edge promises will separate the pair later, so letting the
    incumbent keep its whitelist here would answer the question the edge exists to ask.
    """
    from ..io import OnlistRegistry
    from .scoring import build_tech_evaluation
    from .window import WindowProbe

    wps = [p for p in b_probes if isinstance(p, WindowProbe)]
    registry = OnlistRegistry(offline=True)
    challenger = build_tech_evaluation(a, wps, registry)
    if not challenger.valid:
        return None
    # `.value` is -inf for a forbidden incumbent, so a spec that cannot score its own synthetic reads
    # loses to every valid challenger — which is the honest ordering, and a KB defect the round-trip
    # and `test_a_spec_is_length_feasible_against_its_own_reads` catch first.
    return challenger.value - build_tech_evaluation(b, wps, registry).value


def could_outrank_at_rungs_0_2(a: Spec, b: Spec, b_probes: Iterable[object]) -> bool:
    """Could ``a`` come out on top of ``b`` on ``b``'s OWN data, using the cheap probes alone?

    The under-declaration guard's question, and it is an **ordering** one. It used to be a *validity*
    one (:func:`accepts_at_rungs_0_2`), which was a sound proxy for danger only while every spec
    consumed every file: a spec that seats every role somewhere and orphans nothing scores near the
    top by construction, so "valid" and "could win" were one fact wearing two names. A spec that
    consumes FEWER files breaks the identity — the leftover penalty is ``λ/|R|`` per orphaned file,
    so it bites harder the fewer roles the assignment has, and a one-role fallback seated on any
    long-enough cDNA read is *valid* against nearly every leaf in the KB while scoring far below all
    of them. Left as validity, the guard would demand an edge from that fallback to almost
    everything: honest boilerplate, and a gate that flags everything discriminates nothing — the
    defect this prevents is a guard decaying into a formality nobody reads (#275, ADR-0029).

    **Danger, stated exactly:** on ``b``'s own reads the cheap probes do not put ``a`` DECISIVELY
    below ``b``. "Decisively" is not a new number invented here — it is ``escalate``'s own tie
    threshold, the same θ that decides at runtime whether a candidate joins the tie set and gets
    asked about at all. A second copy of that constant would drift, and the copy that drifts is the
    one CI reads, so it is imported rather than restated. Above the band ``a`` is the winner the
    resolver hands back; inside it ``a`` and ``b`` are one tie the cheap rungs cannot order, and the
    KB is where "reach for the onlist or a human" has to be written down.

    ``a`` must still produce a valid assignment to rank at all, so this predicate **implies**
    :func:`accepts_at_rungs_0_2`. Every necessary condition of that one therefore remains a sound
    skip for this one — in particular ``geometry.geometry_could_accept``, which the guard uses to
    avoid scoring length-infeasible pairs.
    """
    from .escalate import _THETA  # the resolver's tie band, not a second copy of it

    margin = rung02_margin(a, b, b_probes)
    return margin is not None and margin >= -_THETA


def rung02_separable(
    a: Spec, a_probes: Iterable[object], b: Spec, b_probes: Iterable[object]
) -> bool:
    """Do the cheap probes tell these two chemistries apart at all?

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
    """Ids the spec declares as ``processing_equivalent`` twins (benign: record both)."""
    return {c.id for c in spec.confusable_with if c.relationship == "processing_equivalent"}


def declared_divergent(spec: Spec) -> set[str]:
    """Ids the spec declares as ``processing_divergent`` (a real disagreement to decide)."""
    return {c.id for c in spec.confusable_with if c.relationship == "processing_divergent"}


def is_processing_equivalent(a: Spec, b_id: str) -> bool:
    """Does ``a`` declare ``b_id`` as a processing-equivalent twin?"""
    return b_id in declared_equivalents(a)


# ---- tree-sourced confusability: siblings replace hand-declared divergent cliques ----
def share_parent(specs: Mapping[str, Spec], a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` are siblings — the same non-null parent in the KB tree."""
    pa = specs[a].parent if a in specs else None
    pb = specs[b].parent if b in specs else None
    return pa is not None and pa == pb


def is_tree_kin(specs: Mapping[str, Spec], a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` are parent-child or siblings — a confusability the tree DECLARES.

    A divergent sibling clique (v2/v3/v3.1) collapses to one ``parent`` link, so the under-declaration
    guard treats tree kin the way it treats an explicit ``confusable_with`` edge: already declared.
    """
    if a not in specs or b not in specs:
        return False
    if specs[a].parent == b or specs[b].parent == a:
        return True
    return share_parent(specs, a, b)


def sibling_decided_by(specs: Mapping[str, Spec], a: str, b: str) -> list[str]:
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


def _lineage(specs: Mapping[str, Spec], tech: str) -> list[str]:
    """``tech`` and every ancestor above it, nearest first — the one ``parent`` walk this file makes.

    Cycle-guarded rather than trusting the tree: ``build_tree`` rejects a cycle, but these predicates
    are handed whatever pool the caller is scoring against, which need not have been through it.
    """
    chain: list[str] = []
    cur: str | None = tech
    while cur is not None and cur not in chain:
        chain.append(cur)
        cur = specs[cur].parent if cur in specs else None
    return chain


def _root_of(specs: Mapping[str, Spec], tech: str) -> str:
    """The family-root ancestor of ``tech`` — the top of its ``parent`` chain."""
    return _lineage(specs, tech)[-1]


def same_family(specs: Mapping[str, Spec], a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` share a family root — the assay family a paper reliably names.

    The policy this encodes: harvest is trusted at the FAMILY level (10x 3' gene-expression), the bytes
    at the LEAF level (v2 vs v3). A within-family disagreement (asserted v2, observed v3) is therefore
    not a blocking conflict — the bytes decide the leaf. It is broader than ``is_tree_kin`` (which is
    parent-child OR siblings only): two cousins under a deeper tree are the same family yet not kin.

    Every assay family is its own root today (``10x-3p-gex``, ``bulk-rnaseq``, ``splitseq``,
    ``bd-rhapsody-wta`` are distinct roots), so a shared root IS the same family. A future super-root
    over two genuinely different families would need a KB-lint guard against over-suppression; none
    exists now, and none is needed while roots and families coincide.
    """
    if a not in specs or b not in specs:
        return False
    return _root_of(specs, a) == _root_of(specs, b)


def narrows_to(specs: Mapping[str, Spec], asserted: str, observed: str) -> bool:
    """True iff ``observed`` lies in ``asserted``'s subtree — the asserted term NARROWS to it.

    The predicate behind ADR-0020. A claim of "10x 3'" against an observed ``10x-3p-gex-v3`` is not a
    disagreement of any strength: the prose named a node, the bytes named one of its descendants, and
    the family term is *satisfied* by the leaf. Nothing was discarded, so there is nothing to surface.

    Strictly narrower than :func:`same_family`, and deliberately **directional**. Siblings do not
    narrow to each other (asserted v2, observed v3 is a real disagreement — the bytes win it and the
    discarded claim is kept as a ``resolved`` conflict, per 2026.7.8), and a leaf does not narrow to
    its own family node: "10x 3' v3" claims more than "10x 3'", so an observed family node would be
    the bytes saying LESS than the prose, which is not a narrowing.
    """
    if asserted not in specs or observed not in specs:
        return False
    return asserted in _lineage(specs, observed)
