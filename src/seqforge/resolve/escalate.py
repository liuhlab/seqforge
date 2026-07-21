"""Escalation: ranked evaluations -> ``{Decision | Conflict | Question | Blocker}`` (§3.5).

Deterministic code owns the decision; the hypothesis only changes *which* candidates are computed and
can break a genuinely-non-decisive divergent tie (recorded ``basis: asserted``, surfaced). The three
terminal shapes:

- **Decision** — a clear winner (``margin > θ``, no divergent tie). Declared ``processing_equivalent``
  twins are recorded together into the chemistry equivalence class with **0** questions (§12 benign).
- **Conflict** — an observed value contradicts an asserted one (e.g. asserted 26 bp vs observed
  28 bp). Detected unconditionally, in parallel: surfaced, library takes the observed value, exit 4.
- **Question / Blocker** — a processing-*divergent* tie that metadata/onlist can't settle routes to a
  human (exit 4); a structural dead end (missing technical read, truncated gzip, unsupported tech)
  is a ``Blocker`` (exit 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..kb.schema import Read, SegmentLength, Spec
from ..models.blocker import Blocker, BlockerCode, BlockerSubject
from ..models.conflict import Conflict, ConflictPosition
from ..models.observation import Observation
from ..models.resolve import Candidate, Question, RoleAssignment
from .confuse import is_processing_equivalent
from .scoring import TechEvaluation

_THETA = 0.02  # tie threshold: candidates within θ of the top are a "tie set"


@dataclass(frozen=True)
class Escalation:
    """The escalation verdict: ranked candidates plus any conflicts / questions / blockers."""

    candidates: list[Candidate]
    conflicts: list[Conflict] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    rung_reached: int = 0
    winner: str | None = None


def escalate(
    evaluations: list[TechEvaluation],
    observations: list[Observation],
    specs: dict[str, Spec],
    hypothesis_value: str | None,
    hypothesis_id: str | None,
    hypothesis_confidence: float,
) -> Escalation:
    """Turn scored technologies into a single terminal verdict."""
    integrity = _integrity_blockers(observations)
    if integrity:
        return Escalation(candidates=[], blockers=integrity, rung_reached=2)

    # `tech` is the LAST key and it is here for determinism, not for judgement: two candidates can tie
    # on (value, rung) exactly — §12 benign twins do it BY CONSTRUCTION, since they are byte-identical
    # — and without a final tiebreak the ordering falls through to the KB dict's iteration order. The
    # representative of an equivalence class is arbitrary; it still has to be arbitrary the SAME way on
    # every run, or `candidates[0].technology` flips between runs of an unchanged input.
    valid = sorted((e for e in evaluations if e.valid), key=lambda e: (-e.value, -e.rung, e.tech))
    if not valid:
        blocker = _no_candidate_blocker(evaluations, hypothesis_value, specs)
        return Escalation(candidates=[], blockers=[blocker], rung_reached=2)

    # Within the score tie (candidates within θ of the best), the STRONGEST evidence wins: a rung-3
    # onlist PASS beats rung-2 geometry. So the top of a near-tie is its highest-rung member, and a
    # lower-rung look-alike is DOMINATED (not a divergent-tie question) — this is how onlist evidence
    # separates a specific chemistry from the generic bulk fallback that merely failed to be forbidden.
    best_value = valid[0].value
    tie = [e for e in valid if best_value - e.value <= _THETA]
    top = sorted(tie, key=lambda e: (-e.rung, -e.value, e.tech))[0]
    top_spec = specs[top.tech]
    rung = max(e.rung for e in tie)

    contenders = [e for e in tie if e.tech != top.tech and e.rung >= top.rung]
    equivalent_ties = [
        e
        for e in contenders
        if is_processing_equivalent(top_spec, e.tech)
        or is_processing_equivalent(specs[e.tech], top.tech)
    ]
    divergent_ties = [e for e in contenders if e not in equivalent_ties]

    conflicts = _detect_conflicts(
        hypothesis_value, hypothesis_id, hypothesis_confidence, top, top_spec, observations, specs
    )
    collapse = _single_cell_collapse_conflict(
        hypothesis_value, hypothesis_id, hypothesis_confidence, top, top_spec, observations, specs
    )
    if collapse is not None:
        conflicts.append(collapse)
    # Pre-trimming can only be judged once a role is known — it is variable length *on a read the
    # chemistry says is fixed*, so it needs the winner's assignment, not raw bytes. Hence here and
    # not in `_integrity_blockers`.
    trimmed = _pretrimmed_blockers(top, top_spec, observations)
    if trimmed:
        return Escalation(candidates=[], blockers=trimmed, conflicts=conflicts, rung_reached=rung)
    equiv_members = sorted(set(top.equivalence_members) | {e.tech for e in equivalent_ties})

    if not divergent_ties:
        candidates = [_candidate(top, equiv_members, rung)]
        candidates += [_candidate(e, e.equivalence_members, rung) for e in valid if e is not top]
        return Escalation(
            candidates=candidates, conflicts=conflicts, rung_reached=rung, winner=top.tech
        )

    # a processing-divergent tie: metadata (rung 0) may still disambiguate; else a human question.
    picked = _metadata_disambiguation(hypothesis_value, top, divergent_ties, specs)
    if picked is not None:
        candidates = [_candidate(picked, picked.equivalence_members, rung)]
        candidates += [_candidate(e, e.equivalence_members, rung) for e in valid if e is not picked]
        return Escalation(
            candidates=candidates,
            conflicts=conflicts,
            rung_reached=max(rung, 0),
            winner=picked.tech,
        )

    question = _divergent_question(top, divergent_ties, specs)
    candidates = [_candidate(e, e.equivalence_members, rung) for e in valid]
    return Escalation(
        candidates=candidates,
        conflicts=conflicts,
        questions=[question],
        rung_reached=7,
        winner=None,
    )


def _declared_fixed_length(spec: Spec, read: Read) -> tuple[int, int | None] | None:
    """A read's declared fixed length and its over-length escape, or ``None`` if it is not fixed-cycle.

    Fixed either by ``min_len == max_len`` (a bare geometry) OR by a ``segment_length`` requires —
    which is how an over-length-capable read (a 10x R1) declares its canonical length while ``max_len``
    stays null. Returning the ``over_length_min`` lets the caller exempt a genuinely over-length read.
    """
    if read.min_len is not None and read.min_len == read.max_len:
        return read.min_len, None
    for t in spec.signature.requires:
        if isinstance(t, SegmentLength) and t.read == read.id:
            return t.length, t.over_length_min
    return None


def _pretrimmed_blockers(
    top: TechEvaluation, spec: Spec, observations: list[Observation]
) -> list[Blocker]:
    """Variable length on a read the chemistry declares FIXED => someone trimmed before uploading.

    This is the quiet failure §5 is built around, and it survives every other check by construction.
    ``read_length_compatible`` matches on the **mode**, so a file whose reads are mostly 28 bp with a
    trimmed tail scores exactly like a clean one and wins its candidate outright. Nothing downstream
    looks again: STARsolo reads the barcode from a fixed offset, and on a shifted read that offset is
    an arbitrary 16-mer. It matches no whitelist, the cell is dropped, the matrix comes out thin, and
    STAR exits 0.

    A fixed-cycle Illumina run does not produce variable-length reads. If the technical read is
    variable, a trimmer ran — and cutadapt/trimmomatic do not know a barcode from an adapter.

    An OVER-LENGTH read (a barcode read sequenced past CB+UMI) is exempt: its length varies only in
    the junk tail, while CB/UMI stay at their fixed offsets, so that variation is not a trimmed
    barcode. The canonical length is still enforced — a read at its declared length that is *also*
    variable is trimmed and blocks, exactly as before.
    """
    by_sha = {o.file.sha256: o for o in observations}
    assigned = top.role_assignment_shas()
    blockers: list[Blocker] = []
    for read in spec.reads:
        fixed = _declared_fixed_length(spec, read)
        if fixed is None:
            continue
        declared, over_min = fixed
        sha = assigned.get(read.id)
        obs = by_sha.get(sha) if sha else None
        if obs is None or obs.read_length.n_distinct == 1:
            continue
        if over_min is not None and obs.read_length.mode >= over_min:
            continue  # over-length: variation is in the junk tail, not the barcode
        role_id = read.id
        ref = obs.file.basename
        blockers.append(
            Blocker(
                id=f"blk-pretrimmed-{obs.file.sha256[:8]}",
                code=BlockerCode.PRETRIMMED_VARIABLE_LENGTH,
                message=(
                    f"{ref}: {spec.identity.id} declares read {role_id!r} as fixed-cycle "
                    f"({declared} bp), but the file carries {obs.read_length.n_distinct} distinct "
                    f"read lengths (mode {obs.read_length.mode}). A trimmer ran before upload, so "
                    f"barcode/UMI offsets may have shifted — counts would be silently wrong."
                ),
                remedy=(
                    "Re-fetch the untrimmed original (SRA's sra-pub-src-* buckets preserve the "
                    "submitter's files), or confirm the technical read was excluded from trimming "
                    "and re-probe."
                ),
                subject=BlockerSubject(kind="file", ref=ref),
                evidence=[obs.file.sha256],
            )
        )
    return blockers


def _integrity_blockers(observations: list[Observation]) -> list[Blocker]:
    blockers: list[Blocker] = []
    for obs in observations:
        ref = obs.file.basename
        if obs.gzip.truncated:
            blockers.append(
                Blocker(
                    id=f"blk-truncated-{obs.file.sha256[:8]}",
                    code=BlockerCode.TRUNCATED_GZIP,
                    message=f"{ref}: gzip stream ends mid-record (truncated upload/transfer).",
                    remedy="Re-download the file and verify its checksum before re-probing.",
                    subject=BlockerSubject(kind="file", ref=ref),
                    evidence=[obs.file.sha256],
                )
            )
        elif not obs.gzip.ok:
            blockers.append(
                Blocker(
                    id=f"blk-corrupt-{obs.file.sha256[:8]}",
                    code=BlockerCode.CORRUPT_FASTQ,
                    message=f"{ref}: not a readable gzip FASTQ.",
                    remedy="Re-download the file; confirm it is gzip-compressed FASTQ.",
                    subject=BlockerSubject(kind="file", ref=ref),
                    evidence=[obs.file.sha256],
                )
            )
    return blockers


def _no_candidate_blocker(
    evaluations: list[TechEvaluation], hypothesis_value: str | None, specs: dict[str, Spec]
) -> Blocker:
    """No technology passed its requires: a missing technical read, or genuinely unsupported."""
    hyp_tech = _match_tech(hypothesis_value, specs) if hypothesis_value else None
    if hyp_tech is not None:
        e = next((ev for ev in evaluations if ev.tech == hyp_tech), None)
        if (
            e is not None
            and e.barcode_role_ids
            and set(e.unfillable_role_ids) & set(e.barcode_role_ids)
            and e.cdna_role_fillable
        ):
            return Blocker(
                id=f"blk-missing-technical-{hyp_tech}",
                code=BlockerCode.MISSING_TECHNICAL_READ,
                message=(
                    f"Metadata asserts {hyp_tech} (single-cell), but the technical/barcode read is "
                    "absent — only a cDNA-shaped read is present."
                ),
                remedy=(
                    "Re-fetch with `fasterq-dump --include-technical`, or pull the original submitted "
                    "files `sra-pub-src-*` via the SRA Data Locator / SDL API."
                ),
                subject=BlockerSubject(kind="dataset", ref=hyp_tech),
            )
    return Blocker(
        id="blk-unsupported",
        code=BlockerCode.UNSUPPORTED_TECHNOLOGY,
        message="No knowledge-base technology matches these reads' structure.",
        remedy="Add a KB entry for this technology, or verify the inputs are the expected FASTQs.",
        subject=BlockerSubject(kind="dataset", ref="dataset"),
    )


def _detect_conflicts(
    hypothesis_value: str | None,
    hypothesis_id: str | None,
    hypothesis_confidence: float,
    top: TechEvaluation,
    top_spec: Spec,
    observations: list[Observation],
    specs: dict[str, Spec],
) -> list[Conflict]:
    """Surface an observed-vs-asserted geometry contradiction (e.g. asserted v2 26 bp, observed 28 bp)."""
    if not hypothesis_value:
        return []
    asserted_len = _asserted_barcode_length(hypothesis_value, specs)
    observed_len = _observed_barcode_length(top, top_spec, observations)
    if asserted_len is None or observed_len is None or asserted_len == observed_len:
        return []
    over_min = _spec_over_length_min(top_spec)
    if over_min is not None and observed_len >= over_min:
        # An over-length barcode read is EXPECTED for this chemistry (CB/UMI at fixed offsets, the rest
        # junk), not a geometry contradiction — so 28-vs-150 is agreement, not a conflict to surface.
        return []
    return [
        Conflict(
            id="conflict-barcode-length",
            field="library.read_layout.R1.length",
            kind="observed_vs_asserted",
            positions=[
                ConflictPosition(
                    value=str(asserted_len),
                    basis="asserted",
                    evidence=[hypothesis_id] if hypothesis_id else [],
                    confidence=hypothesis_confidence,
                ),
                ConflictPosition(
                    value=str(observed_len),
                    basis="observed",
                    evidence=[o.file.sha256 for o in observations],
                    confidence=0.99,
                ),
            ],
            decidable_by=["reads"],
            status="open",
        )
    ]


def _single_cell_collapse_conflict(
    hypothesis_value: str | None,
    hypothesis_id: str | None,
    hypothesis_confidence: float,
    top: TechEvaluation,
    top_spec: Spec,
    observations: list[Observation],
    specs: dict[str, Spec],
) -> Conflict | None:
    """A single-cell chemistry was asserted, but the winning byte candidate is a **barcodeless bulk**
    library. Surface it (#7/#11) rather than let it collapse silently.

    The failure this catches: the asserted single-cell tech's barcode read was *forbidden* — trimmed,
    or over-sequenced past its length gate — so that tech dropped out of ``valid`` and the generic bulk
    fallback won by default. The result is a bulk manifest for a single-cell dataset, at exit 0. That
    is the quiet corpus-poisoning §5 exists to prevent (GSE126954's over-length SRX5411291; GSE274290
    before a BD Rhapsody spec exists).

    ``_detect_conflicts`` provably cannot see this: it compares barcode *lengths*, and a bulk winner
    has no barcode read, so ``_observed_barcode_length`` is ``None`` and that guard returns early. This
    one keys on structure — asserted-barcoded vs observed-barcodeless — not on a length delta. Like the
    length conflict it only surfaces (open Conflict, exit 4); it never arbitrates, because whether the
    data *is* single-cell or bulk is exactly the call code may not auto-pick.
    """
    if not hypothesis_value:
        return None
    hyp_tech = _match_tech(hypothesis_value, specs)
    if hyp_tech is None:
        return None  # the asserted chemistry names no KB tech, so "single-cell" is not established
    if _barcode_read_id(specs[hyp_tech]) is None:
        return None  # a bulk chemistry was asserted and bulk won — no collapse, agreement
    if _barcode_read_id(top_spec) is not None:
        return None  # the winner is itself barcoded (single-cell won or tied) — nothing collapsed
    return Conflict(
        id="conflict-single-cell-collapsed-to-bulk",
        field="library.chemistry",
        kind="observed_vs_asserted",
        positions=[
            ConflictPosition(
                value=hyp_tech,
                basis="asserted",
                evidence=[hypothesis_id] if hypothesis_id else [],
                confidence=hypothesis_confidence,
            ),
            ConflictPosition(
                value=top.tech,
                basis="observed",
                evidence=[o.file.sha256 for o in observations],
                confidence=0.99,
            ),
        ],
        decidable_by=["reads"],
        status="open",
    )


def _metadata_disambiguation(
    hypothesis_value: str | None,
    top: TechEvaluation,
    divergent_ties: list[TechEvaluation],
    specs: dict[str, Spec],
) -> TechEvaluation | None:
    """If a span-verified hypothesis names one tie member, pick it (rung 0, surfaced ``asserted``)."""
    if not hypothesis_value:
        return None
    hyp_tech = _match_tech(hypothesis_value, specs)
    if hyp_tech is None:
        return None
    for e in [top, *divergent_ties]:
        if e.tech == hyp_tech:
            return e
    return None


def _divergent_question(
    top: TechEvaluation, divergent_ties: list[TechEvaluation], specs: dict[str, Spec]
) -> Question:
    options = sorted({top.tech, *(e.tech for e in divergent_ties)})
    decidable: set[str] = set()
    for c in specs[top.tech].confusable_with:
        if c.id in options and c.relationship == "processing_divergent":
            decidable.update(c.distinguishable_by)
    decidable.discard("none")
    return Question(
        id="q-chemistry",
        field="library.chemistry",
        prompt=(
            "Reads are byte-consistent with multiple processing-divergent chemistries "
            f"({', '.join(options)}) that onlist/metadata could not separate. Which chemistry applies?"
        ),
        options=options,
        decidable_by=sorted(decidable) or ["user"],  # type: ignore[arg-type]
        rung=7,
    )


def _candidate(e: TechEvaluation, equiv_members: list[str], rung: int) -> Candidate:
    return Candidate(
        technology=e.tech,
        score=e.score,
        role_assignment=RoleAssignment(
            assignment=e.role_assignment_shas(),
            unassigned=[e.file_shas[f] for f in e.assignment.unassigned_files],
        ),
        rung_resolved={"chemistry": rung},
        equivalence_members=equiv_members,
        evidence=[],
    )


# ---- geometry helpers ----
def _barcode_read_id(spec: Spec) -> str | None:
    for read in spec.reads:
        if any(el.type == "barcode" for el in read.elements):
            return read.id
    return None


def _spec_barcode_length(spec: Spec) -> int | None:
    """The declared barcode-read length: a ``segment_length`` requires, else a fixed ``min_len``."""
    bc = _barcode_read_id(spec)
    if bc is None:
        return None
    for t in spec.signature.requires:
        if isinstance(t, SegmentLength) and t.read == bc:
            return t.length
    for read in spec.reads:
        if read.id == bc and read.min_len is not None and read.min_len == read.max_len:
            return read.min_len
    return None


def _spec_over_length_min(spec: Spec) -> int | None:
    """The barcode read's over-length escape, if it declares one (a mode >= this is expected)."""
    bc = _barcode_read_id(spec)
    if bc is None:
        return None
    for t in spec.signature.requires:
        if isinstance(t, SegmentLength) and t.read == bc:
            return t.over_length_min
    return None


def _asserted_barcode_length(value: str, specs: dict[str, Spec]) -> int | None:
    stripped = value.strip()
    if stripped.isdigit():
        return int(stripped)
    tech = _match_tech(value, specs)
    return _spec_barcode_length(specs[tech]) if tech else None


def _observed_barcode_length(
    top: TechEvaluation, top_spec: Spec, observations: list[Observation]
) -> int | None:
    bc = _barcode_read_id(top_spec)
    if bc is None:
        return None
    sha = top.role_assignment_shas().get(bc)
    if sha is None:
        return None
    for obs in observations:
        if obs.file.sha256 == sha:
            return obs.read_length.mode
    return None


def _match_tech(value: str | None, specs: dict[str, Spec]) -> str | None:
    """Match an asserted chemistry string to a KB tech id or alias (case-insensitive)."""
    if not value:
        return None
    needle = value.strip().lower()
    for tech_id in specs:
        if needle == tech_id.lower():
            return tech_id
    for tech_id, spec in specs.items():
        names = [spec.identity.id, spec.identity.name, *spec.identity.aliases]
        if any(needle == n.lower() for n in names):
            return tech_id
    for tech_id, spec in specs.items():
        names = [spec.identity.name, *spec.identity.aliases]
        if any(needle in n.lower() or n.lower() in needle for n in names):
            return tech_id
    return None
