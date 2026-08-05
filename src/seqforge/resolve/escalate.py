"""Escalation: ranked evaluations -> ``{Decision | Conflict | Question | Blocker}``.

Deterministic code owns the decision; the hypothesis only changes *which* candidates are computed and
can break a genuinely-non-decisive divergent tie (recorded ``basis: asserted``, surfaced). The three
terminal shapes:

- **Decision** — a clear winner (``margin > θ``, no divergent tie). Declared ``processing_equivalent``
  twins are recorded together into the chemistry equivalence class with **0** questions (benign).
- **Conflict** — an observed value contradicts an asserted one. Detected unconditionally, in parallel;
  the library always takes the observed value. A CROSS-family contradiction (single-cell asserted, bulk
  observed) is surfaced ``open`` — exit 4, a human decides. A WITHIN-family geometry difference
  (asserted v2 26 bp, observed v3 28 bp) is recorded ``resolved`` — the bytes decide the leaf and the
  paper's family-level claim still holds — so it is auditable but does not block (exit 0).
- **Question / Blocker** — a processing-*divergent* tie that metadata/onlist can't settle routes to a
  human (exit 4); a structural dead end (missing technical read, truncated gzip, unsupported tech)
  is a ``Blocker`` (exit 3).

Every guard here that reads the hypothesis first asks :func:`~seqforge.kb.match.resolve_chemistry_id`
what the asserted string NAMES, and that matcher is one-directional: a curated alias must sit inside
the value, never the value inside an alias. What that removes is a whole class of manufactured claim —
`library_strategy: RNA-Seq` used to name ``bulk-rnaseq``, so an archive's filing vocabulary became a
bulk assertion and every guard below then read a single-cell winner as a cross-family contradiction. A
string naming no node asserts nothing, and a term naming an ANCESTOR of the winner narrows to it rather
than disagreeing with it (``narrows_to``, ADR-0020).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..kb.match import resolve_chemistry_id
from ..kb.schema import Read, SegmentLength, Spec
from ..models.blocker import Blocker, BlockerCode, BlockerSubject
from ..models.conflict import Conflict, ConflictPosition, Resolution
from ..models.observation import Observation
from ..models.resolve import Candidate, Question, RoleAssignment
from .confuse import is_processing_equivalent, narrows_to, same_family, sibling_decided_by
from .scoring import TechEvaluation

_THETA = 0.02  # tie threshold: candidates within θ of the top are a "tie set"

#: Floor on the share of a fixed-cycle read's reads that must sit at its modal length before
#: `_pretrimmed_blockers` accepts that the library is where the chemistry says it is.
#:
#: This is `evaluators._CONSTANT_CARRIER_MIN`'s bar, and it is here for that bar's reason (2026.7.15):
#: "this file was pre-trimmed" is a claim about a POPULATION of reads, and its honest form is a
#: proportion — a majority is what "the reads are this length" means. The gate this replaced asked
#: `n_distinct == 1`, which is the same failure `has_segment kind: constant` had one layer over: a
#: statistic that cannot tell "every read carries this" from "most do and the rest of the head is
#: junk". There it forbade real SPLiT-seq; here it refused a whole dataset at exit 3, with no appeal,
#: for one read of two thousand that came back a base short (#190).
#:
#: What does NOT transfer is how much slack the number enjoys. A window with no fixed sequence in it
#: measures ~0, so that bar sits in a wide dead zone; read lengths have no such gap — a partly trimmed
#: file lands anywhere in (0, 1) — so this one is load-bearing at its value, and a majority is where
#: the argument already made in this repo puts it. Erring towards accepting is also the cheaper
#: mistake: a trimmed read's barcode misses the whitelist and its cell drops, so a minority costs
#: yield, while refusing costs the dataset.
_MODE_SHARE_MIN = 0.5


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
    # on (value, rung) exactly — benign twins do it BY CONSTRUCTION, since they are byte-identical
    # — and without a final tiebreak the ordering falls through to the KB dict's iteration order. The
    # representative of an equivalence class is arbitrary; it still has to be arbitrary the SAME way on
    # every run, or `candidates[0].technology` flips between runs of an unchanged input.
    valid = sorted((e for e in evaluations if e.valid), key=lambda e: (-e.value, -e.rung, e.tech))
    if not valid:
        blocker = _no_candidate_blocker(evaluations, hypothesis_value, specs)
        return Escalation(candidates=[], blockers=[blocker], rung_reached=2)

    # Within the score tie (candidates within θ of the best), the STRONGER evidence TIER wins: rung 3
    # (an onlist was consulted) outranks rung-2 geometry, so a lower-rung look-alike is DOMINATED, not a
    # divergent-tie question. That is how onlist evidence separates a specific chemistry from the generic
    # bulk fallback that merely failed to be forbidden — but only WITHIN θ. On OVER-LENGTH reads a 75 bp
    # barcode read is also a fine cDNA, so bulk edges the real chemistry out by more than θ and, without
    # help, a single-cell library whose whitelist *hit* (#7) collapses to bulk at exit 0. So anchor the
    # tie on the barcoded candidate whose onlist positively matched, letting its decisive rung-3
    # evidence win. The gate is ``barcode_onlist_hit`` (the whitelist ACTUALLY matched), NOT rung 3 (an
    # onlist was merely consulted): a random ~100 bp bulk read passes a barcode read's over-length
    # geometry gate and reaches rung 3 with a FAILING onlist, and must stay bulk. Canonical single-cell
    # already out-scores bulk (a short barcode read is a poor cDNA), so there the anchor is a no-op.
    #
    # A SECOND way a barcodeless top must yield, and it arrived with read sets (ADR-0029): a fallback
    # whose active read set consumes FEWER files can win while leaving one of them unexplained. A
    # single-end bulk set seats its one cDNA role on the cDNA mate of a 10x deposit, orphans the 28 bp
    # barcode read at ``λ/1`` and still scores 0.75 against a barcoded candidate whose whitelist MISSED
    # (0.57) — so a barcoded library whose barcodes match no shipped list resolved to bulk at exit 0
    # instead of refusing, which is a plausible gene-count matrix rather than a recoverable refusal.
    # The over-length case above must NOT be caught by this and is not: there bulk seats every file, so
    # it orphans nothing, and a random 100 bp read that merely passes a barcode read's over-length gate
    # still stays bulk. The distinguishing fact is EXPLANATION, not score — a deposit holding a read
    # this chemistry cannot seat at all is not this chemistry's deposit.
    anchor = valid[0]
    if _barcode_read_id(specs[anchor.tech]) is None:
        barcoded = [e for e in valid if _barcode_read_id(specs[e.tech]) is not None]
        hit = next((e for e in barcoded if e.barcode_onlist_hit), None)
        seats_dropped = next(
            (e for e in barcoded if _seats_a_file_the_fallback_dropped(e, anchor, specs[e.tech])),
            None,
        )
        anchor = hit or seats_dropped or anchor
    best_value = anchor.value
    tie = [e for e in valid if best_value - e.value <= _THETA]
    # The whitelist that HIT is the arbiter. Among the tie, a barcoded candidate whose onlist positively
    # matched (``barcode_onlist_hit``) dominates a same-rung sibling whose onlist did NOT — the failing
    # sibling reached rung 3 by consulting an onlist that came up empty, exactly the FAILING-onlist case
    # the anchor above already refuses to hand the win to. Without this, two chemistries that differ ONLY
    # by whitelist (10x 3' v3 vs Multiome GEX — 3M-february-2018 vs 737K-arc-v1) tie on an over-length
    # read where the barcode read is also a fine cDNA: the non-hitting sibling takes the swapped-role
    # seat, out-scores the honest whitelist-hitting one, and turns a settled call into a divergent
    # ask-human that collapses to bulk. So prefer ``barcode_onlist_hit`` in the ordering, and drop a
    # non-hitting sibling from the divergent set when the winner's whitelist hit — the bytes decided it.
    top = sorted(tie, key=lambda e: (-int(e.barcode_onlist_hit), -e.rung, -e.value, e.tech))[0]
    top_spec = specs[top.tech]
    rung = max(e.rung for e in tie)

    contenders = [
        e
        for e in tie
        if e.tech != top.tech
        and e.rung >= top.rung
        and not (top.barcode_onlist_hit and not e.barcode_onlist_hit)
    ]
    equivalent_ties = [
        e
        for e in contenders
        if is_processing_equivalent(top_spec, e.tech)
        or is_processing_equivalent(specs[e.tech], top.tech)
    ]
    divergent_ties = [e for e in contenders if e not in equivalent_ties]

    conflicts = _detect_conflicts(
        hypothesis_value,
        hypothesis_id,
        hypothesis_confidence,
        top,
        top_spec,
        observations,
        specs,
        rung,
    )
    collapse = _single_cell_collapse_conflict(
        hypothesis_value, hypothesis_id, hypothesis_confidence, top, top_spec, observations, specs
    )
    if collapse is not None:
        conflicts.append(collapse)
    reverse = _bulk_asserted_single_cell_observed(
        hypothesis_value, hypothesis_id, hypothesis_confidence, top, top_spec, observations, specs
    )
    if reverse is not None:
        conflicts.append(reverse)
    # Pre-trimming can only be judged once a role is known — it is variable length *on a read the
    # chemistry says is fixed*, so it needs the winner's assignment, not raw bytes. Hence here and
    # not in `_integrity_blockers`.
    trimmed = _pretrimmed_blockers(top, top_spec, observations)
    if trimmed:
        return Escalation(candidates=[], blockers=trimmed, conflicts=conflicts, rung_reached=rung)
    barcode_absent = _barcodeless_seated_blocker(top, top_spec, valid)
    if barcode_absent is not None:
        return Escalation(
            candidates=[], blockers=[barcode_absent], conflicts=conflicts, rung_reached=rung
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
    # `top` leads, exactly as it does on the two decided paths above. This branch used to hand back
    # `valid` in raw-score order, which on an over-sequenced read puts BULK first: the anchor and the
    # `barcode_onlist_hit` preference had already established that the whitelist-hitting barcoded
    # candidate is the one the library lands on, and then the question path threw that ordering away.
    # `candidates[0]` is what `fill` composes and what the grader reads as `library.chemistry`, so an
    # honest question about which of two chemistries applies was reporting a third one nobody was
    # asking about.
    candidates = [_candidate(top, top.equivalence_members, rung)]
    candidates += [_candidate(e, e.equivalence_members, rung) for e in valid if e is not top]
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
    """A fixed-cycle read whose reads have mostly LEFT that length => someone trimmed before uploading.

    This is the quiet failure the escalation ladder is built around, and it survives every other
    check by construction. ``read_length_compatible`` matches on the **mode**, and a mode says nothing
    about how many reads are at it — so a barcode read most of which has been trimmed off 28 bp scores
    exactly like a clean one and wins its candidate outright. Nothing downstream looks again: STARsolo
    reads the barcode from a fixed offset, and on a shifted read that offset is an arbitrary 16-mer. It
    matches no whitelist, the cell is dropped, the matrix comes out thin, and STAR exits 0.

    A fixed-cycle Illumina run does not move its reads off the length it was configured for. If most
    of the technical read has left that length, a trimmer ran — and cutadapt/trimmomatic do not know a
    barcode from an adapter.

    **Where "most" is** — ``_MODE_SHARE_MIN``. Asking for a share rather than for uniformity is the
    whole of #190. The gate used to be ``n_distinct == 1``: one read a single base short in a
    2 000-read head refused the dataset at exit 3, with no appeal and no remedy short of re-fetching a
    file that was never wrong — while the Observation carried the share that separates a 0.05% ragged
    tail from a library whose offsets moved, and nothing asked it.

    An OVER-LENGTH read (a barcode read sequenced past CB+UMI) is exempt whatever its share: its
    length varies only in the junk tail, while CB/UMI stay at their fixed offsets, so that variation is
    not a trimmed barcode. The canonical length is still enforced — a read sitting AT its declared
    length, most of whose reads have left it, is trimmed and blocks.

    **The WINNING read set's reads, not the maximal set's.** The claim is about a file this library
    actually has: a read the winning configuration does not carry has no seated file to measure, so a
    maximal-set loop would only ever reach the `continue` below. Saying which set is meant is the point
    — a predicate over a spec's reads has two readings now, and the type system asks neither.
    """
    by_sha = {o.file.sha256: o for o in observations}
    assigned = top.role_assignment_shas()
    blockers: list[Blocker] = []
    for read in spec.reads_in(top.read_set):
        fixed = _declared_fixed_length(spec, read)
        if fixed is None:
            continue
        declared, over_min = fixed
        sha = assigned.get(read.id)
        obs = by_sha.get(sha) if sha else None
        if obs is None or obs.read_length.mode_share >= _MODE_SHARE_MIN:
            continue
        if over_min is not None and obs.read_length.mode >= over_min:
            continue  # over-length: variation is in the junk tail, not the barcode
        role_id = read.id
        ref = obs.file.basename
        profile = obs.read_length
        blockers.append(
            Blocker(
                id=f"blk-pretrimmed-{obs.file.sha256[:8]}",
                code=BlockerCode.PRETRIMMED_VARIABLE_LENGTH,
                message=(
                    f"{ref}: {spec.identity.id} declares read {role_id!r} as fixed-cycle "
                    f"({declared} bp), but only {profile.mode_share:.0%} of the sampled reads sit at "
                    f"the modal length {profile.mode} — lengths span {profile.min_len}-"
                    f"{profile.max_len} bp across {profile.n_distinct} distinct values. A trimmer "
                    f"ran before upload, so barcode/UMI offsets may have shifted — counts would be "
                    f"silently wrong."
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


def _seats_a_file_the_fallback_dropped(
    barcoded: TechEvaluation, fallback: TechEvaluation, barcoded_spec: Spec
) -> bool:
    """Does ``barcoded`` seat its BARCODE role on a file the barcodeless ``fallback`` left unassigned?

    The predicate that stops a fewer-file read set from winning a deposit it does not explain. A
    barcodeless fallback that orphans a file another candidate calls a barcode read has not accounted
    for the deposit: a single-end bulk library produces one biological read per run and nothing else,
    so a 28 bp neighbour is evidence that this is not one — evidence that lives in the OTHER file, which
    is why no gate on the fallback's own read could ever see it.

    Deliberately about the orphan and not about the score. The leftover penalty (``λ/|R|`` per orphaned
    file) already prices an unexplained file, and at ``λ = 0.25`` on a one-role set that price is too low
    to beat a two-role candidate whose whitelist came up empty. Raising ``λ`` would re-price every
    assignment in the KB to fix one shape; asking whether the file was explained is the same question
    without the collateral.
    """
    bc = _barcode_read_id(barcoded_spec)
    if bc is None:
        return False
    sha = barcoded.role_assignment_shas().get(bc)
    if sha is None:
        return False
    dropped = {fallback.file_shas[f] for f in fallback.assignment.unassigned_files}
    return sha in dropped


def _barcodeless_seated_blocker(
    top: TechEvaluation, top_spec: Spec, valid: list[TechEvaluation]
) -> Blocker | None:
    """F1b — the winning chemistry is barcoded, its barcode role is FILLED, and NO byte-consistent
    barcoded candidate hits a whitelist though one WAS available to check (``barcode_onlist_available``).
    STARsolo would read barcodes from a read matching nothing and report ~0 valid barcodes at exit 0 — a
    silently empty matrix. Refuse instead.

    The gate is over ALL valid candidates, not just ``top``: if any barcoded leaf's whitelist positively
    matched, the data IS barcoded and the winner resolves to that leaf, so this must abstain. The case
    that forces it is the over-length v2/v3 tie — a 150 bp 10x v3 library where v2 edges v3 on raw score
    (so ``top`` is v2, whose 737K list misses) while v3's 3M list hits; blocking on ``top`` alone would
    refuse a perfectly good v3 dataset before the tie/hypothesis picks v3. Only a dataset where no
    barcoded chemistry matched at all is genuinely barcode-absent.

    Fires only where the whitelist is the arbiter: a bulk winner (no barcode role) is the
    ``_single_cell_collapse_conflict`` guard's job, and a chemistry whose whitelist was never consulted
    is not onlist-judgeable (abstain). Distinct from ``MISSING_TECHNICAL_READ``, where the barcode role
    is structurally UNFILLABLE — here the role is filled, the seated read just is not barcoded.
    """
    if _barcode_read_id(top_spec) is None:
        return None  # a bulk winner has no barcode role — the collapse guard's job, not this
    if any(e.barcode_onlist_hit for e in valid):
        return None  # some byte-consistent barcoded leaf DID hit — the data is barcoded, not absent
    if not top.barcode_onlist_available:
        return None  # no whitelist was consulted: absence is not decidable, defer to the geometry gates
    return Blocker(
        id=f"blk-barcode-absent-{top.tech}",
        code=BlockerCode.BARCODE_READ_ABSENT,
        message=(
            f"{top.tech} is barcoded, but no read carries whitelist-matchable barcodes: the seated "
            "barcode read matches the chemistry's whitelist only at chance. STARsolo would report "
            "near-zero valid barcodes and exit 0 with an empty matrix."
        ),
        remedy=(
            "Confirm the barcode/technical read was included — SRA drops it unless dumped with "
            "`fasterq-dump --include-technical`; re-fetch the original submitted files "
            "(`sra-pub-src-*` via the SDL API) if it was stripped, then re-probe."
        ),
        subject=BlockerSubject(kind="dataset", ref=top.tech),
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
    hyp_tech = resolve_chemistry_id(hypothesis_value, specs)
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
    rung: int,
) -> list[Conflict]:
    """Surface an observed-vs-asserted geometry contradiction (e.g. asserted v2 26 bp, observed 28 bp).

    A WITHIN-FAMILY difference (asserted v2, observed v3 — both ``10x-3p-gex`` leaves) is NOT a blocking
    conflict: a paper names the assay *family* reliably and the exact *leaf* vaguely, and the bytes
    decide the leaf (whitelist + UMI length). So it is recorded as a ``resolved`` conflict — the
    discarded claim survives for audit ("three truths, never merged"), but it does not block. A
    CROSS-FAMILY length difference stays an ``open`` conflict (exit 4, a human decides). This is the
    GSE229022 lesson: "10x 3' v2/v3" in prose, byte-provably v3, is agreement at the family level.
    """
    if not hypothesis_value:
        return []
    asserted_tech = resolve_chemistry_id(hypothesis_value, specs)
    if asserted_tech is not None and narrows_to(specs, asserted_tech, top.tech):
        # ADR-0020: the asserted term is an ANCESTOR of the winner — the prose named a node and the
        # bytes named one of its descendants, so the claim is satisfied and there is no delta to
        # surface. (Unfirable on today's KB: every family node declares a length RANGE rather than a
        # fixed one, so the comparison below returns early anyway. It is the invariant, not the
        # observation — a family that ever pinned one number would conflict with every leaf under it.)
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
    positions = [
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
    ]
    if asserted_tech is not None and same_family(specs, asserted_tech, top.tech):
        # Within-family leaf difference: harvest named the family (reliable), the bytes decide the leaf
        # (v2 vs v3). Resolve to the observed leaf; keep the asserted claim as a RESOLVED conflict so the
        # disagreement is auditable but does not block (exit 0).
        return [
            Conflict(
                id="conflict-barcode-length",
                field="library.read_layout.R1.length",
                kind="observed_vs_asserted",
                positions=positions,
                decidable_by=["reads"],
                status="resolved",
                resolution=Resolution(
                    chosen_value=str(observed_len),
                    basis="observed",
                    rung=rung,
                    decided_by="code",
                    note=(
                        f"within-family leaf difference; the bytes decide the leaf ({top.tech}) — "
                        "the paper's family-level claim is satisfied"
                    ),
                ),
            )
        ]
    return [
        Conflict(
            id="conflict-barcode-length",
            field="library.read_layout.R1.length",
            kind="observed_vs_asserted",
            positions=positions,
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
    is the quiet corpus-poisoning this stage exists to prevent (GSE126954's over-length SRX5411291;
    GSE274290
    before a BD Rhapsody spec exists).

    ``_detect_conflicts`` provably cannot see this: it compares barcode *lengths*, and a bulk winner
    has no barcode read, so ``_observed_barcode_length`` is ``None`` and that guard returns early. This
    one keys on structure — asserted-barcoded vs observed-barcodeless — not on a length delta. Like the
    length conflict it only surfaces (open Conflict, exit 4); it never arbitrates, because whether the
    data *is* single-cell or bulk is exactly the call code may not auto-pick.
    """
    if not hypothesis_value:
        return None
    hyp_tech = resolve_chemistry_id(hypothesis_value, specs)
    if hyp_tech is None:
        return None  # the asserted chemistry names no KB tech, so "single-cell" is not established
    if narrows_to(specs, hyp_tech, top.tech):
        return None  # ADR-0020: the winner lies under the asserted node — the term narrowed
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


def _bulk_asserted_single_cell_observed(
    hypothesis_value: str | None,
    hypothesis_id: str | None,
    hypothesis_confidence: float,
    top: TechEvaluation,
    top_spec: Spec,
    observations: list[Observation],
    specs: dict[str, Spec],
) -> Conflict | None:
    """The mirror image of ``_single_cell_collapse_conflict``: a **bulk** chemistry was asserted, but the
    winning byte candidate is a **barcoded single-cell** library. Surface it (exit 4) rather than emit a
    single-cell manifest for a dataset the paper calls bulk.

    Same error class as the collapse, the other direction — a wrong data-vs-paper pairing or a mis-written
    methods section, and equally not something to let go. It is cross-family (bulk vs a single-cell
    family), so ``same_family`` never suppresses it. And ``_detect_conflicts`` cannot see it: an asserted
    *bulk* chemistry has no barcode read, so ``_asserted_barcode_length`` is ``None`` and that guard
    returns early — which is why this is a separate structural check. Like the collapse it only surfaces;
    it never arbitrates.
    """
    if not hypothesis_value:
        return None
    hyp_tech = resolve_chemistry_id(hypothesis_value, specs)
    if hyp_tech is None:
        return None  # the asserted chemistry names no KB tech, so "bulk" is not established
    if narrows_to(specs, hyp_tech, top.tech):
        return None  # ADR-0020: the winner lies under the asserted node — the term narrowed
    if _barcode_read_id(specs[hyp_tech]) is not None:
        return None  # a single-cell chemistry was asserted — the forward collapse guard's job, not this
    if _barcode_read_id(top_spec) is None:
        return None  # the winner is itself bulk (agreement) — nothing to surface
    return Conflict(
        id="conflict-bulk-asserted-single-cell-observed",
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
    """If a span-verified hypothesis names one tie member, pick it (rung 0, surfaced ``asserted``).

    Failing an exact name, fall back to the **family**, and only when the family picks out exactly one
    tie member. That is not a second-guess, it is the authority split this module already runs on and
    ``same_family`` was written for: a paper names the assay family reliably and the exact leaf
    vaguely, so ``_detect_conflicts`` treats an asserted-v2 / observed-v3 disagreement as agreement at
    the family level and lets the bytes pick the leaf.

    Without the fallback that policy stopped one step short of the case it was built for. The 26 bp tie
    — 10x 3' v2 versus 10x 5' v1/v2, identical geometry AND identical whitelist — is `[metadata,
    alignment]`-decidable by declaration, and prose that says "10x 3' v3" (or just "10x 3'") settles the
    3'-versus-5' question completely while naming the wrong leaf. Exact-name matching alone would ask a
    human a question the document already answered.

    Ambiguity still asks: two tie members under the asserted family (a 3' v3 claim against a v3-versus-
    Multiome tie) is exactly the case metadata cannot settle, so it returns ``None`` and escalates.
    """
    if not hypothesis_value:
        return None
    hyp_tech = resolve_chemistry_id(hypothesis_value, specs)
    if hyp_tech is None:
        return None
    members = [top, *divergent_ties]
    for e in members:
        if e.tech == hyp_tech:
            return e
    kin = [e for e in members if same_family(specs, hyp_tech, e.tech)]
    return kin[0] if len(kin) == 1 else None


def _divergent_question(
    top: TechEvaluation, divergent_ties: list[TechEvaluation], specs: dict[str, Spec]
) -> Question:
    options = sorted({top.tech, *(e.tech for e in divergent_ties)})
    decidable: set[str] = set()
    for c in specs[top.tech].confusable_with:
        if c.id in options and c.relationship == "processing_divergent":
            decidable.update(c.distinguishable_by)
    # Siblings no longer carry a per-pair edge: their separating mechanism lives in the shared parent's
    # `children_decided_by`, sourced here so a v2-vs-v3 over-length tie is still `decidable_by: onlist`.
    for opt in options:
        if opt != top.tech:
            decidable.update(sibling_decided_by(specs, top.tech, opt))
    decidable.discard("none")
    return Question(
        id="q-chemistry",
        field="library.chemistry",
        prompt=(
            "Reads are byte-consistent with multiple processing-divergent chemistries "
            f"({', '.join(options)}) that onlist/metadata could not separate. Which chemistry applies?"
        ),
        options=options,
        # `none` is discarded above, and every remaining KB mechanism is also a `Decidable`; the two
        # vocabularies are declared apart, so what is collected here stays a `set[str]`.
        decidable_by=sorted(decidable) or ["user"],  # type: ignore[arg-type]
        rung=7,
    )


def _candidate(e: TechEvaluation, equiv_members: list[str], rung: int) -> Candidate:
    return Candidate(
        technology=e.tech,
        score=e.score,
        read_set=e.read_set,
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
    """The id of this chemistry's barcode read, or ``None`` when it has none — i.e. when it is bulk.

    **The MAXIMAL read set**, deliberately: "is this chemistry barcoded?" is a property of the
    chemistry, not of the configuration one deposit was sequenced in, and every guard reading this
    (the collapse conflicts, F1b, the bulk-hint drop) is asking the chemistry-level question. A
    barcoded chemistry whose alternative set dropped the barcode read would still be single-cell.
    """
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
    tech = resolve_chemistry_id(value, specs)
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
