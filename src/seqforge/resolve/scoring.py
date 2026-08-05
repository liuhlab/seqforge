"""Per-technology scoring: evidence matrix -> injective assignment -> cardinality-normalized score.

For technology ``t`` with roles ``R_t`` (the reads of its active **read set**) and files ``F``, cell
``M[r][f]`` is ``FORBIDDEN`` if any ``requires(r)`` gate FAILs or any ``excludes(r)`` gate PASSes, else
the normalized weighted ``supports(r)`` sum in ``[0, 1]``. ``FORBIDDEN`` is an internal
``Cell(forbidden=True)`` flag, never a ``±inf`` — serialized it is ``{"status": "forbidden"}`` so no
infinity ever crosses the JSON boundary.

``score(t) = raw / |R_t|  -  (λ / |R_t|)·|F \\ A*|`` is cardinality-normalized so a 2-role 10x and a
6-role SPLiT-seq are comparable. The filename prior enters as a sub-threshold ``β``-scaled nudge that
can only break an exact byte-tie.

**A spec may declare more than one read set, and the loop over them lives HERE** — inside the
technology evaluation, never above it. Every set is scored and the best one is kept, so there is still
exactly ONE ``TechEvaluation`` per spec: ranking, equivalence, escalation and the divergent-tie
machinery need no new "a spec does not tie with itself" rule, and a chemistry never competes with
itself for a place in the candidate list.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..io import OnlistNotAvailable, OnlistRegistry
from ..kb.schema import OnlistHitRate, Read, SegmentLength, Spec, Test
from ..models.resolve import TechScore
from .assign import AssignmentResult, best_assignment
from .evaluators import Outcome, evaluate, onlist_admits_over_length, read_length_compatible
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
    """The full scored verdict for one technology against the dataset's files.

    One per spec, and it names the **read set** that produced it: ``roles`` is that set's reads, so on a
    spec declaring alternatives the verdict answers "which configuration of this chemistry, scored how"
    rather than only "this chemistry". The name reaches the resolve artifacts (a ``Candidate``), where
    "how this was decided" lives, and NOT the manifest — the manifest's read layout already lists
    exactly this set's reads, and the composer reads the reads and never the name.
    """

    tech: str
    #: Which read set was scored: :data:`~seqforge.kb.schema.FULL_READ_SET` for the maximal one.
    read_set: str
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
    #: A barcode role's onlist POSITIVELY hit — the whitelist actually identified barcodes in the data
    #: (best hit clears the floor-anchored admission bar), not merely that an onlist was consulted. This
    #: is the honest "this data IS barcoded" signal escalate uses so the barcodeless bulk fallback never
    #: shadows a real single-cell library, WITHOUT hijacking genuine bulk (whose barcode window sits at
    #: the whitelist floor, so this stays False even when a barcode read passes on over-length geometry).
    barcode_onlist_hit: bool = False
    #: A barcode role's onlist whitelist was REGISTERED and materializable — we had a list to check
    #: against. Lets F1b (escalate) tell "checked, missed" (refuse) from "never checked" (abstain).
    barcode_onlist_available: bool = False

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
    requires: list[Test],
    excludes: list[Test],
    supports: list[tuple[Test, float]],
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
    # A support the BYTES could not answer leaves the normalizer as well as the numerator (#307).
    # Normalizing by every DECLARED weight marked a spec down for a question nobody could have
    # answered: the test contributed `weight * 0.0` to the numerator while keeping its full weight in
    # the denominator, exactly as if it had been asked and come back negative. Same rule as #177 (a
    # dark cycle costs `motif_present` coverage, not rate), #255 (an uncalled base is not a
    # substitution) and #277 (a test addressed to a read outside the active set has no cell).
    #
    # `answerable`, NEVER the ABSTAIN outcome — `distinct_ratio` abstains on every input by design so
    # it can never gate, while measuring on every input; and a whitelist we could not obtain is
    # answerable and keeps its weight. Both traps are argued at `evaluators.Evaluation.answerable`.
    #
    # Every support is still evaluated: `used_onlist` is a fact about what was consulted, not about
    # what scored, and skipping the call would lower the reached rung.
    answered: list[tuple[float, float]] = []
    for when, weight in supports:
        ev = evaluate(when, read, wp, spec, registry)
        used_onlist = used_onlist or ev.used_onlist
        if ev.answerable:
            answered.append((weight, ev.score))
    return Cell(forbidden=False, value=_weighted_mean(answered), reason="scored"), used_onlist


def _weighted_mean(weighted: list[tuple[float, float]]) -> float:
    """``Σ w·s / Σ w`` over the supports that were answered, or 0.0 when none were.

    **Nothing left to average is NO evidence, which is 0.0** — the same value a role declaring no
    supports at all has always taken, and the two say the one thing: this role offers no positive
    evidence about this file. Deliberately not the other reading, which would be #277's rule applied
    one level up — dropping the ROLE from the tech score's ``|R|`` normalizer. That rewards blindness:
    a spec that could answer nothing would score the mean of its remaining roles and so beat a spec
    that answered and got 0.5. An unanswerable support must not count against a spec, and it must not
    count for one either.

    Shared by :func:`_score_cell` and :func:`_global_support` because they had the same defect for the
    same reason, and a normalizer written twice is a normalizer that gets fixed once.
    """
    total = sum(w for w, _ in weighted)
    return sum(w * s for w, s in weighted) / total if total > 0 else 0.0


def _over_length_admitted_by_onlist(
    test: Test,
    read: Read,
    wp: WindowProbe,
    spec: Spec,
    registry: OnlistRegistry,
    supports: list[tuple[Test, float]],
) -> bool:
    """Admit a barcode read over-sequenced into the length dead zone IFF its barcode prefix hits the
    whitelist. Deliberately narrow and additive: it fires ONLY on a ``segment_length`` FAIL whose mode
    is strictly between the canonical ``length`` and ``over_length_min`` (a read at/below the canonical
    length, or already ``>= over_length_min``, does not reach here), and ONLY when an ``onlist_hit_rate``
    support clears the FLOOR-ANCHORED admission bar (:func:`onlist_admits_over_length`) — a LOWER bar
    than the support's own ``min`` PASS threshold, because admission asks "barcode or cDNA?" not
    "confident barcode?", so it can admit a read whose exact hit rate sits below ``min`` yet far above
    chance. A cDNA read of the same length hits the whitelist at its floor and stays forbidden, so
    rung-0-2 separability between single-cell and cDNA-only chemistries is preserved.
    """
    if not isinstance(test, SegmentLength) or test.over_length_min is None:
        return False
    if not (test.length < wp.mode_length < test.over_length_min):
        return False  # not the dead zone: canonical is exact-checked, >= over_length_min already PASSes
    for when, _weight in supports:
        # A FLOOR-ANCHORED bar, not the support `min`: admission asks "barcode or cDNA?", not
        # "confident barcode?". Seqforge matches exactly (no 1MM correction), so a real over-sequenced
        # barcode read with ordinary error hits below the 0.6 support gate yet far above chance -- the
        # gate rejected SRX5411291 and it fell to bulk. See `onlist_admits_over_length`.
        if isinstance(when, OnlistHitRate) and onlist_admits_over_length(
            when, read, wp, spec, registry
        ):
            return True
    return False


def _clears_onlist_bar(
    read: Read,
    wp: WindowProbe,
    supports: list[tuple[Test, float]],
    spec: Spec,
    registry: OnlistRegistry,
) -> bool:
    """True iff some onlist support for this barcode ``read`` clears the floor-anchored admission bar on
    ``wp`` — the file's barcode prefix hits the whitelist far above the ~1e-4..1e-3 random floor, i.e.
    this read LOOKS barcoded. The predicate both the F1a seating constraint and ``barcode_onlist_hit``
    are built from."""
    return any(
        isinstance(when, OnlistHitRate)
        and onlist_admits_over_length(when, read, wp, spec, registry)
        for when, _w in supports
    )


def _barcode_onlist_available(
    spec: Spec,
    registry: OnlistRegistry,
    barcode_role_ids: list[str],
    sup_by: dict[str, list[tuple[Test, float]]],
) -> bool:
    """True iff at least one barcode role's onlist whitelist is REGISTERED and materializable — we had a
    list to check against. When False the whitelist was never consulted, so a ``barcode_onlist_hit`` of
    False means 'could not check', not 'barcode absent': F1b must abstain, not refuse."""
    for rid in barcode_role_ids:
        for when, _w in sup_by[rid]:
            if not isinstance(when, OnlistHitRate):
                continue
            ref = spec.onlists.get(when.onlist)
            if ref is None or not registry.has(ref.registry):
                continue
            try:
                registry.packed(ref.registry)
            except OnlistNotAvailable:
                continue
            return True
    return False


def _global_support(
    global_supports: list[tuple[Test, float]],
    reads: list[Read],
    wps: list[WindowProbe],
    spec: Spec,
    registry: OnlistRegistry,
) -> float:
    """Normalized score of read-less supports (e.g. ``header_index``), max over files.

    Carries :func:`_score_cell`'s answerability rule through the same :func:`_weighted_mean`, because
    it had the same defect for the same reason (#307): a support no file could answer kept its weight
    in the normalizer and halved a readable one beside it. A test is taken at its best over the files
    that COULD answer it, and is dropped entirely when none could.

    Latent rather than live in the shipped KB, and worth saying which: every signature declaring a
    read-less support declares exactly one (``header_index``), so the mixed case has no instance and
    an all-unreadable one already returned 0.0 by arithmetic. The fix is here so that adding a second
    read-less support is not a silent re-weighting — an SRA-normalized header, which is most deposits
    and is how probe DETECTS the normalization, answers nothing on any file.
    """
    if not global_supports or not wps:
        return 0.0
    answered: list[tuple[float, float]] = []
    for when, weight in global_supports:
        scores = [
            ev.score
            for wp in wps
            if (ev := evaluate(when, reads[0], wp, spec, registry)).answerable
        ]
        if scores:
            answered.append((weight, max(scores)))
    return _weighted_mean(answered)


def build_tech_evaluation(
    spec: Spec, wps: list[WindowProbe], registry: OnlistRegistry
) -> TechEvaluation:
    """Score one technology against the dataset's files — the best of its read sets, and only that one.

    A spec declares a maximal read set and may name subsets of it, so "score this chemistry" is really
    "score each configuration it publishes and keep the one the bytes support". The loop is here rather
    than in the engine above so that **one spec still yields one Candidate**: a chemistry that appeared
    twice in the ranking would tie with itself, and every downstream rule — the θ tie set, the
    equivalence class, the divergent-tie question, the alphabetical determinism tiebreak — would need a
    new clause saying that particular tie is not a disagreement.

    **The comparison, and why it needs no special case for validity.** A forbidden set scores ``-inf``
    (``TechEvaluation.value``), so a set that cannot be seated loses to any set that can, and a spec all
    of whose sets are forbidden keeps the maximal one's verdict — the reason string and
    ``unfillable_role_ids`` a caller renders stay the ones it always got. An exact tie prefers the LARGER
    set: it explains more of the data, and preferring it falls out of visiting the maximal set first
    (``Spec.read_set_names``) and replacing only on a strict improvement. Nothing here needs a
    thumb on the scale to make a subset lose where it should — the score is normalized by role count and
    charges ``λ/|R|`` per orphaned file, so on a two-file deposit the one-role set pays 0.25 for the mate
    it declined to explain.
    """
    best: TechEvaluation | None = None
    for name in spec.read_set_names():
        evaluation = _evaluate_read_set(spec, name, wps, registry)
        if best is None or (evaluation.value, len(evaluation.roles)) > (
            best.value,
            len(best.roles),
        ):
            best = evaluation
    if best is None:  # unreachable: `read_set_names` always yields the maximal set
        raise ValueError(f"{spec.identity.id!r} declares no read set to score")
    return best


def read_set_evaluations(
    spec: Spec, wps: list[WindowProbe], registry: OnlistRegistry
) -> list[TechEvaluation]:
    """Every read set's verdict, in :meth:`~seqforge.kb.schema.Spec.read_set_names` order.

    What :func:`build_tech_evaluation` takes the best of. Exposed because a claim about ONE set is not
    checkable through the maximum — "the single-end set does not outrank a real single-cell chemistry on
    that chemistry's own data" is the plausible-matrix failure this feature could introduce, and on data
    where the maximal set is forbidden the maximum IS the subset, so a test reading only the winner
    could not tell which set it was measuring. Nothing in ``src/`` consumes this: production always
    wants the best set, and a caller that could pick a set would be a second policy.
    """
    return [_evaluate_read_set(spec, name, wps, registry) for name in spec.read_set_names()]


def _evaluate_read_set(
    spec: Spec, read_set: str, wps: list[WindowProbe], registry: OnlistRegistry
) -> TechEvaluation:
    """Score ONE read set of one technology (the evidence matrix + joint assignment).

    Every ``spec.reads`` read of the maximal set is a role here; a subset's reads are its roles and the
    rest of the spec's reads simply have no row. **A signature test addressed to a read outside the
    active set is therefore inapplicable** — it has no cell, so it enters neither the score numerator
    nor its normalizer (``total_w`` sums the ACTIVE role's supports), which is already how a nonexistent
    cell behaves. That is why one signature serves every set and no set-specific signature exists.
    """
    reads = spec.reads_in(read_set)
    reads_by_id = {r.id: r for r in reads}
    roles = [r.id for r in reads]
    n_files = len(wps)
    file_shas = [wp.observation.file.sha256 for wp in wps]
    # The ACTIVE set's barcode roles, not the spec's: this list seats a constraint (F1a) and indexes
    # into `roles`, so a barcode read the active set does not carry has no seat to constrain.
    barcode_role_ids = [r.id for r in reads if any(el.type == "barcode" for el in r.elements)]

    req_by: dict[str, list[Test]] = defaultdict(list)
    exc_by: dict[str, list[Test]] = defaultdict(list)
    sup_by: dict[str, list[tuple[Test, float]]] = defaultdict(list)
    global_sup: list[tuple[Test, float]] = []
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

    # F1a — a barcode role must be seated on a read that LOOKS barcoded. Per barcode role, which files
    # clear the floor-anchored onlist bar (prefix hits the whitelist far above chance)? Computed once and
    # reused for the seating constraint here and for the `barcode_onlist_hit` signal below.
    barcode_clears: dict[str, list[bool]] = {
        rid: [_clears_onlist_bar(reads_by_id[rid], wp, sup_by[rid], spec, registry) for wp in wps]
        for rid in barcode_role_ids
    }
    # When some file clears, forbid seating the role on any file that does NOT — so sum-maximization
    # cannot park the barcode role on a cDNA-length mate that merely out-scored the real barcode read on
    # other supports. The swap it prevents: a real barcode read carrying ordinary sequencing error can
    # score higher as cDNA than as its own barcode, and the true cDNA read of a low-diversity library
    # scores low on both roles, so `(barcode->cDNA)+(cDNA->barcode)` beats the honest seat (PRJNA658829
    # SRR12575567). A file that clears the bar is always a barcode — a real cDNA hits the whitelist at the
    # ~1e-4..1e-3 random floor, far below the bar — so forcing a clearing read into barcode is never
    # wrong. No file clearing => a no-op here; escalate's F1b then refuses rather than compose a barcode
    # read that matches no whitelist.
    for rid, clears in barcode_clears.items():
        if not any(clears):
            continue
        ri = roles.index(rid)
        for f in range(n_files):
            if not clears[f] and not forbidden_m[ri][f]:
                forbidden_m[ri][f] = True
                matrix[rid][f] = Cell(
                    forbidden=True,
                    value=0.0,
                    reason="barcode role: another read hits the onlist and this one does not",
                )

    assignment = best_assignment(len(roles), n_files, score_m, forbidden_m, prior_m)
    global_bonus = _global_support(global_sup, list(reads_by_id.values()), wps, spec, registry)

    # Did a barcode role's onlist actually hit (not just get consulted)? True iff some file cleared the
    # floor-anchored admission bar above (reusing F1a's `barcode_clears`) — the "this data is barcoded"
    # signal escalate uses to keep the barcodeless fallback from shadowing a real single-cell library,
    # and to refuse (F1b) when a barcoded winner has no whitelist-hitting read. Genuine bulk sits at the
    # whitelist floor and stays False even when its read passes over-length geometry.
    barcode_onlist_hit = any(any(clears) for clears in barcode_clears.values())
    barcode_onlist_available = _barcode_onlist_available(spec, registry, barcode_role_ids, sup_by)
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
        read_set=read_set,
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
        barcode_onlist_hit=barcode_onlist_hit,
        barcode_onlist_available=barcode_onlist_available,
    )
