"""Per-technology scoring: evidence matrix -> injective assignment -> cardinality-normalized score.

For technology ``t`` with roles ``R_t`` (its reads) and files ``F``, cell ``M[r][f]`` is ``FORBIDDEN``
if any ``requires(r)`` gate FAILs or any ``excludes(r)`` gate PASSes, else the normalized weighted
``supports(r)`` sum in ``[0, 1]``. ``FORBIDDEN`` is an internal ``Cell(forbidden=True)`` flag, never a
``±inf`` — serialized it is ``{"status": "forbidden"}`` so no infinity ever crosses the JSON boundary.

``score(t) = raw / |R_t|  -  (λ / |R_t|)·|F \\ A*|`` is cardinality-normalized so a 2-role 10x and a
6-role SPLiT-seq are comparable. The filename prior enters as a sub-threshold ``β``-scaled nudge that
can only break an exact byte-tie.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..io import OnlistRegistry
from ..kb.schema import OnlistHitRate, Read, SegmentLength, Spec
from ..models.resolve import TechScore
from .assign import AssignmentResult, best_assignment
from .evaluators import Outcome, evaluate, read_length_compatible
from .window import WindowProbe

_LAMBDA = 0.25  # penalty per unassigned (leftover) file, cardinality-normalized
_BETA = 0.01  # filename-prior coefficient (<< min support weight -> tie-break only)
_GLOBAL_COEF = 0.001  # tech-global supports (header_index) contribute a sub-threshold tie-break


@dataclass(frozen=True)
class Cell:
    """One evidence-matrix cell: forbidden, or a finite support value in ``[0, 1]``."""

    forbidden: bool
    value: float
    reason: str = ""

    def to_json(self) -> dict[str, object]:
        if self.forbidden:
            return {"status": "forbidden", "reason": self.reason}
        return {"status": "scored", "value": round(self.value, 4)}


@dataclass(frozen=True)
class TechEvaluation:
    """The full scored verdict for one technology against the dataset's files."""

    tech: str
    roles: list[str]
    file_shas: list[str]
    matrix: dict[str, list[Cell]]
    assignment: AssignmentResult
    score: TechScore
    rung: int
    used_onlist: bool
    equivalence_members: list[str]
    barcode_role_ids: list[str]
    unfillable_role_ids: list[str]
    cdna_role_fillable: bool

    @property
    def valid(self) -> bool:
        return self.score.status == "scored"

    @property
    def value(self) -> float:
        return self.score.value if self.score.value is not None else float("-inf")

    def role_assignment_shas(self) -> dict[str, str]:
        """role_id -> file sha256 for the winning assignment (empty if forbidden)."""
        return {self.roles[r]: self.file_shas[f] for r, f in self.assignment.mapping.items()}

    def matrix_json(self) -> dict[str, dict[str, dict[str, object]]]:
        """JSON-safe evidence matrix: ``{role: {file_sha: {status, ...}}}`` — no ``±inf``."""
        out: dict[str, dict[str, dict[str, object]]] = {}
        for role, cells in self.matrix.items():
            out[role] = {self.file_shas[f]: cells[f].to_json() for f in range(len(cells))}
        return out


def filename_prior(read: Read, basename: str) -> float:
    """1.0 if the file's name carries the read's conventional token (``_R1_`` -> ``R1``), else 0.0."""
    if not read.file_hint:
        return 0.0
    token = read.file_hint.strip("_")
    return 1.0 if token and token in basename else 0.0


def _score_cell(
    read: Read,
    wp: WindowProbe,
    spec: Spec,
    registry: OnlistRegistry,
    requires: list[object],
    excludes: list[object],
    supports: list[tuple[object, float]],
) -> tuple[Cell, bool]:
    used_onlist = False
    if read_length_compatible(read, wp) == Outcome.FAIL:
        reason = (
            f"read-length incompatible (mode {wp.mode_length} vs {read.min_len}..{read.max_len})"
        )
        return Cell(forbidden=True, value=0.0, reason=reason), used_onlist
    for t in requires:
        ev = evaluate(t, read, wp, spec, registry)
        used_onlist = used_onlist or ev.used_onlist
        if ev.outcome == Outcome.FAIL:
            # A `segment_length` FAIL in the over-length DEAD ZONE (canonical < mode < over_length_min)
            # is not necessarily a wrong read: an R1 over-sequenced to e.g. 75 bp is a real barcode
            # read whose CB/UMI still sit at the fixed offsets — over_length_min is deliberately high
            # (100) so a 60-94 bp cDNA is not admitted on length alone. The WHITELIST is the
            # disambiguator: a genuine cDNA of the same length misses it, a real barcode hits it. So if
            # the barcode onlist hits, admit as over-length (rung 3); else keep the FAIL. This is the
            # one place a rung-3 result overrides a rung-0-2 length gate, and it only ever ADMITS
            # (#7 — GSE126954's over-sequenced SRX5411291, which the v2 length gate otherwise forbids).
            if _over_length_admitted_by_onlist(t, read, wp, spec, registry, supports):
                used_onlist = True
                continue
            return Cell(
                forbidden=True, value=0.0, reason=f"requires FAIL: {ev.detail}"
            ), used_onlist
    for t in excludes:
        ev = evaluate(t, read, wp, spec, registry)
        used_onlist = used_onlist or ev.used_onlist
        if ev.outcome == Outcome.PASS:
            return Cell(
                forbidden=True, value=0.0, reason=f"excludes matched: {ev.detail}"
            ), used_onlist
    total_w = sum(w for _, w in supports)
    value = 0.0
    if total_w > 0:
        acc = 0.0
        for when, weight in supports:
            ev = evaluate(when, read, wp, spec, registry)
            used_onlist = used_onlist or ev.used_onlist
            acc += weight * ev.score
        value = acc / total_w
    return Cell(forbidden=False, value=value, reason="scored"), used_onlist


def _over_length_admitted_by_onlist(
    test: object,
    read: Read,
    wp: WindowProbe,
    spec: Spec,
    registry: OnlistRegistry,
    supports: list[tuple[object, float]],
) -> bool:
    """Admit a barcode read over-sequenced into the length dead zone IFF its barcode prefix hits the
    whitelist. Deliberately narrow and additive: it fires ONLY on a ``segment_length`` FAIL whose mode
    is strictly between the canonical ``length`` and ``over_length_min`` (a read at/below the canonical
    length, or already ``>= over_length_min``, does not reach here), and ONLY when an ``onlist_hit_rate``
    support on this read PASSes. A cDNA read of the same length misses the whitelist and stays
    forbidden, so rung-0-2 separability between single-cell and cDNA-only chemistries is preserved.
    """
    if not isinstance(test, SegmentLength) or test.over_length_min is None:
        return False
    if not (test.length < wp.mode_length < test.over_length_min):
        return False  # not the dead zone: canonical is exact-checked, >= over_length_min already PASSes
    for when, _weight in supports:
        if isinstance(when, OnlistHitRate) and evaluate(when, read, wp, spec, registry).outcome == (
            Outcome.PASS
        ):
            return True
    return False


def _global_support(
    global_supports: list[tuple[object, float]],
    reads: list[Read],
    wps: list[WindowProbe],
    spec: Spec,
    registry: OnlistRegistry,
) -> float:
    """Normalized score of read-less supports (e.g. ``header_index``), max over files."""
    if not global_supports or not wps:
        return 0.0
    total_w = sum(w for _, w in global_supports)
    if total_w <= 0:
        return 0.0
    acc = 0.0
    for when, weight in global_supports:
        best = max(evaluate(when, reads[0], wp, spec, registry).score for wp in wps)
        acc += weight * best
    return acc / total_w


def build_tech_evaluation(
    spec: Spec, wps: list[WindowProbe], registry: OnlistRegistry
) -> TechEvaluation:
    """Score one technology against the dataset's files (the evidence matrix + joint assignment)."""
    reads_by_id = {r.id: r for r in spec.reads}
    roles = [r.id for r in spec.reads]
    n_files = len(wps)
    file_shas = [wp.observation.file.sha256 for wp in wps]

    req_by: dict[str, list[object]] = defaultdict(list)
    exc_by: dict[str, list[object]] = defaultdict(list)
    sup_by: dict[str, list[tuple[object, float]]] = defaultdict(list)
    global_sup: list[tuple[object, float]] = []
    for t in spec.signature.requires:
        rid = getattr(t, "read", None)
        if rid is not None:
            req_by[rid].append(t)
    for t in spec.signature.excludes:
        rid = getattr(t, "read", None)
        if rid is not None:
            exc_by[rid].append(t)
    for s in spec.signature.supports:
        rid = getattr(s.when, "read", None)
        if rid is not None:
            sup_by[rid].append((s.when, s.weight))
        else:
            global_sup.append((s.when, s.weight))

    matrix: dict[str, list[Cell]] = {}
    score_m: list[list[float]] = []
    forbidden_m: list[list[bool]] = []
    prior_m: list[list[float]] = []
    used_onlist = False
    for rid in roles:
        read = reads_by_id[rid]
        cells: list[Cell] = []
        row_score: list[float] = []
        row_forbid: list[bool] = []
        row_prior: list[float] = []
        for wp in wps:
            cell, uo = _score_cell(read, wp, spec, registry, req_by[rid], exc_by[rid], sup_by[rid])
            used_onlist = used_onlist or uo
            cells.append(cell)
            row_score.append(cell.value)
            row_forbid.append(cell.forbidden)
            row_prior.append(_BETA * filename_prior(read, wp.observation.file.basename))
        matrix[rid] = cells
        score_m.append(row_score)
        forbidden_m.append(row_forbid)
        prior_m.append(row_prior)

    assignment = best_assignment(len(roles), n_files, score_m, forbidden_m, prior_m)
    global_bonus = _global_support(global_sup, list(reads_by_id.values()), wps, spec, registry)

    barcode_role_ids = [r.id for r in spec.reads if any(el.type == "barcode" for el in r.elements)]
    unfillable_role_ids = [roles[i] for i in assignment.unfillable_roles]
    cdna_role_fillable = any(
        any(el.type in ("cdna", "gdna") for el in reads_by_id[rid].elements)
        and any(not c.forbidden for c in matrix[rid])
        for rid in roles
    )

    if assignment.valid:
        raw_norm = assignment.raw / len(roles)
        penalty = (_LAMBDA / len(roles)) * len(assignment.unassigned_files)
        value = raw_norm - penalty + _GLOBAL_COEF * global_bonus
        score = TechScore(technology=spec.identity.id, status="scored", value=round(value, 6))
    else:
        reason = (
            f"unfillable role(s): {unfillable_role_ids}"
            if unfillable_role_ids
            else "no valid injective role assignment"
        )
        score = TechScore(technology=spec.identity.id, status="forbidden", reason=reason)

    equivalence = [c.id for c in spec.confusable_with if c.relationship == "processing_equivalent"]
    return TechEvaluation(
        tech=spec.identity.id,
        roles=roles,
        file_shas=file_shas,
        matrix=matrix,
        assignment=assignment,
        score=score,
        rung=3 if used_onlist else 2,
        used_onlist=used_onlist,
        equivalence_members=equivalence,
        barcode_role_ids=barcode_role_ids,
        unfillable_role_ids=unfillable_role_ids,
        cdna_role_fillable=cdna_role_fillable,
    )
