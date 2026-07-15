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

from ..kb.schema import SegmentLength, Spec
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
    # every run, or `candidates[0].technology` flips between runs of an unchanged input (R7).
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
