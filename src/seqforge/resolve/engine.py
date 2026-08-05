"""``resolve score`` orchestration: probe -> per-tech evidence -> escalation -> ResolveResult.

Deterministic and LLM-free. The optional ``hypothesis`` (a span-verified metadata assertion) is a
control-flow input only — it selects/orders and can break a genuinely-non-decisive tie, but never
enters the evidence matrix. Every stage is content-addressed under ``.seqforge/``: the per-file
Observation and the dataset ResolveResult are cached, so a killed run resumes.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel

from ..io import DEFAULT_REGISTRY, OnlistNotAvailable, OnlistRegistry
from ..kb import KB_VERSION, load_all_specs
from ..kb.match import carries, resolve_chemistry, resolve_chemistry_id
from ..kb.schema import Spec
from ..models.assertion import Assertion
from ..models.blocker import Blocker, BlockerCode, BlockerSubject
from ..models.conflict import Conflict, ConflictPosition, Resolution
from ..models.dataset import INDEX_ROLE
from ..models.observation import Observation
from ..models.records import ArchiveRecord
from ..models.resolve import Candidate, MetadataResolution, ResolveResult
from ..probe import DEFAULT_MAX_BYTES, DEFAULT_MAX_READS, PROBE_VERSION, probe_sample
from . import RESOLVE_VERSION
from .cache import Cache, dataset_id, resume_key

# The SAME predicate the cross-family guards ask ("does this chemistry have a barcode role?"), taken
# rather than restated. The drop below exists to stop one of those guards firing on a hint it should
# never have been offered, so if the two disagreed about what "bulk" means the drop would fail to
# prevent the very conflict it is for. `_THETA` is taken for that reason too: a cell abstains when
# the plate's chemistry is in the TIE SET its own bytes left, and a tie set measured against a second
# threshold would be a different tie set from the one the escalator asked its question about.
from .escalate import _THETA, _barcode_read_id, escalate
from .geometry import length_feasible
from .scoring import TechEvaluation, build_tech_evaluation
from .window import WindowProbe

#: Any of the three surfaced-judgement models a run can carry (`Conflict`, `Question`, `Blocker`),
#: for the one helper that unions them across a dataset's runs.
_ModelT = TypeVar("_ModelT", bound=BaseModel)


@dataclass(frozen=True)
class Hypothesis:
    """A span-verified metadata assertion entering ``score`` as a selector / tie-break."""

    value: str
    id: str = "hypothesis"
    confidence: float = 0.8


#: The archive-neutral key ``io/archive.py`` normalizes a deposit's library descriptor into. It is the
#: NAME that is the contract here, never one archive's XML: an in-house record set built by hand may
#: carry the same key, and a record set that carries nothing at all is the ordinary case.
_LIBRARY_SOURCE = "library_source"
#: The one form whose presence in that value makes a bulk hint non-credible. ONE form, deliberately:
#: matching is tolerant of case, whitespace and hyphens because the KB's own entailment test is, and it
#: stops there — the answer to an unenumerable list of spellings is not a longer list. Its ABSENCE says
#: nothing at all, which is why nothing below reads absence.
_SINGLE_CELL_FORM = "single cell"


def _declares_single_cell(records: Sequence[ArchiveRecord] | None) -> bool:
    """Does any record's declared library source say the deposit is single-cell?

    Every level is scanned, not just ``experiment``: the attribute NAME is the archive-neutral thing,
    and which level an archive hangs its library descriptor off is that archive's shape. Reading
    ``attributes`` directly rather than through :meth:`ArchiveRecord.attribute` is likewise deliberate
    — that accessor answers only for HARMONIZED names, the curated sample-attribute namespace a
    manifest fact may come from, and a library descriptor is not one, so it would answer ``None`` on
    every real record.
    """
    return any(
        carries(attr.value, _SINGLE_CELL_FORM)
        for record in records or ()
        for attr in record.attributes
        if attr.name == _LIBRARY_SOURCE
    )


def _names_a_bulk_chemistry(value: str) -> bool:
    """Does ``value`` name a KB chemistry with no barcode role — the bulk shape?

    The KB answers this, never a list of names here: a spec declaring no ``barcode`` element has
    nothing to demultiplex, and that is what makes it bulk. A string naming no KB node asserts no
    chemistry at all, so there is nothing to rule out and the answer is ``False`` — the narrow reading,
    which costs at most a hint that was never credible anyway.
    """
    spec = resolve_chemistry(value)
    return spec is not None and _barcode_read_id(spec) is None


def chemistry_hypothesis(
    assertions: Sequence[Assertion], *, records: Sequence[ArchiveRecord] | None = None
) -> Hypothesis | None:
    """The chemistry the prose claims, entering `score` as a hypothesis. ``None`` when it cannot.

    **What this is allowed to do.** `score` builds a grid — one row per read role, one column per
    file — from eight byte-tests, and the hypothesis touches none of them. It orders the candidates
    (so the right whitelist is checked first) and it can break a tie the bytes genuinely cannot
    settle. For prose to move a *score* there would have to be a ninth test, `metadata_says`, and a
    spec could then declare a chemistry that identifies itself by being described rather than by
    what is in its reads. That is the thing we do not build.

    **Agreement or nothing.** Every chemistry claim in the dataset must say the same thing. Two
    experiments describing two protocols is a real dataset, and one dataset-level hypothesis would
    steer both — half of them wrongly. Dropping it costs only a hint: the bytes still decide, and if
    the runs really are two chemistries, `resolve_runs` blocks on the disagreement, which is the right
    answer arrived at honestly.

    **It lives here, beside the type it returns, because it has two callers.** `manifest fill` is
    one; `evals/run.py` — the harness that measures `manifest fill` — is the other, and it used to
    reduce the same list its own way (a last-wins ``by_field`` dict), so a dataset naming two
    chemistries steered the harness's scorer with whichever document was read last and the
    compiler's with nothing. A benchmark that reduces differently from production is measuring the
    benchmark; there is one reduction and both callers make it.

    **An unverified claim is not a claim** (R2). `verify_drafts` sets both flags itself, so a harvest
    run cannot arrive here unverified — but `manifest fill --assertions <file>` parses that file
    straight into `Assertion`s with no flag check, and this is a public function now. A claim whose
    quote does not grep back, or does not entail its value, is skipped rather than counted: ignoring
    it must not become a veto over a good one.

    **A record may rule a hint OUT, and that is its whole authority.** `records` is the deposit's own
    structured library descriptor — deterministic, no model, no network — and a value declaring a
    single-cell library on a *bulk* hint makes that hint non-credible, so the hint is dropped. It may
    never name a chemistry, never move a score, and never raise anything: the worst it can do is
    decline to offer a hint, and the bytes decide either way.

    Not a `Conflict`, and the asymmetry is the point. Most single-cell deposits carry a bare
    `TRANSCRIPTOMIC`, so ABSENCE of the single-cell reading carries no information whatever; treating
    the pair as two comparable claims would false-block correct datasets by the hundred. So presence
    withholds a hint and absence does nothing — which is also why an in-house dataset with no records
    at all, the ordinary case, is byte-identically unaffected.

    **An operator's `--assert-chemistry` is out of reach here, by construction.** `manifest fill`
    builds that `Hypothesis` itself and never calls this function for it, so a deliberate human
    selector cannot be dropped by a record — no guard states that, the call graph does. A hint is what
    this rule is entitled to withhold, and an operator override is not a hint.
    """
    values = {
        a.value
        for a in assertions
        if a.field == "library.chemistry" and a.span_verified and a.entailment_ok
    }
    if len(values) != 1:
        return None
    value = next(iter(values))
    # Records first: the check is free where there are none, and asking the KB what a string names
    # would otherwise load every spec on a question nothing is going to act on.
    if _declares_single_cell(records) and _names_a_bulk_chemistry(value):
        return None
    return Hypothesis(value=value, id="harvest", confidence=0.9)


@dataclass(frozen=True)
class ResolveOutput:
    """The engine's return: the wire :class:`ResolveResult`, the evidence matrices, and the probes.

    ``observations`` is carried so a downstream ``manifest fill`` can assemble the file inventory
    without re-probing the bytes (the sample is already within the budget; paying for it twice is
    the bug this avoids).
    """

    result: ResolveResult
    matrices: dict[str, dict[str, dict[str, dict[str, object]]]]
    observations: list[Observation] = field(default_factory=list)

    def exit_code(self) -> int:
        return exit_code_for(self.result)


def exit_code_for(result: ResolveResult) -> int:
    """Uniform exit contract: 3 BLOCKED (>=1 Blocker), 4 NEEDS_HUMAN (open Conflict/question), else 0."""
    if result.blockers:
        return 3
    if result.questions or any(c.status == "open" for c in result.conflicts):
        return 4
    return 0


def _probe_paths(
    paths: Sequence[str | Path], *, max_reads: int, max_bytes: int, cpus: int
) -> dict[str, tuple[Observation, list[str]]]:
    """Probe every file, across up to ``cpus`` processes, keyed by ``str(path)``.

    Each FASTQ is an independent, CPU-bound pure-Python fingerprint whose hot loop holds the GIL, so
    files parallelize across PROCESSES — threads would just serialize. The result is byte-identical to
    a sequential probe: ``probe_sample`` is deterministic over a head-bounded sample, order does not
    matter (the map is keyed by path and the manifest is assembled by content hash), and **core count
    is folded into no hash** — cores are not a budget any more than wall-clock is. One shared pool
    for the whole dataset is why a 12-file / 6-run study saturates the cores at once, rather than two
    files at a time inside each run.
    """
    keyed = list(dict.fromkeys(str(p) for p in paths))  # de-dup, order-preserving
    if cpus <= 1 or len(keyed) <= 1:
        return {p: probe_sample(p, max_reads=max_reads, max_bytes=max_bytes) for p in keyed}

    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor

    # Use a `fork` context where the OS has one (every POSIX box we run on). The probe stage is
    # single-threaded, so fork is safe here, and it sidesteps `spawn`'s footgun of re-importing the
    # caller's `__main__` — which is what makes a `--cpus 4` run explode under pytest or a bare script.
    ctx = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else None

    out: dict[str, tuple[Observation, list[str]]] = {}
    with ProcessPoolExecutor(max_workers=min(cpus, len(keyed)), mp_context=ctx) as pool:
        futures = {
            pool.submit(probe_sample, p, max_reads=max_reads, max_bytes=max_bytes): p for p in keyed
        }
        for fut in futures:
            out[futures[fut]] = fut.result()
    return out


def _score_pool(
    pool: list[Spec], wps: list[WindowProbe], registry: OnlistRegistry, score_threads: int
) -> list[TechEvaluation]:
    """Score every candidate spec — optionally across a thread pool sharing the read-only registry.

    The dominant cost is the onlist scan (``np.searchsorted`` over the packed whitelist), which
    releases the GIL, so threads parallelize it while sharing the one ~27 MB array with zero copies.
    The registry's lazy per-name materialization is NOT thread-safe, so every available onlist is
    pre-warmed single-threaded first; afterwards ``packed()`` is a read-only dict lookup (an
    unavailable onlist stays uncached and simply ABSTAINs, exactly as the serial path does).
    ``ThreadPoolExecutor.map`` preserves order, so the result is byte-identical to the serial list
    whatever the thread count — core count folds into no decision.
    """
    if score_threads <= 1 or len(pool) <= 1:
        return [build_tech_evaluation(spec, wps, registry) for spec in pool]
    for spec in pool:
        for ref in spec.onlists.values():
            if registry.has(ref.registry):
                try:
                    registry.packed(ref.registry)
                except OnlistNotAvailable:
                    pass  # scoring ABSTAINs on this onlist; nothing cached, so no thread races on it
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(score_threads, len(pool))) as ex:
        return list(ex.map(lambda spec: build_tech_evaluation(spec, wps, registry), pool))


def _with_asserted(
    pool: list[Spec], hypothesis_value: str | None, specs: dict[str, Spec]
) -> list[Spec]:
    """``pool``, plus the ASSERTED chemistry when descent narrowed it away. Order is preserved.

    Descent's proof is about the WINNER — a length-infeasible spec would have scored ``forbidden``, so
    dropping it cannot change which candidate wins (:mod:`.geometry`). It is not a proof about the
    *refusal*, and that gap is what this closes. ``escalate``'s ``MISSING_TECHNICAL_READ`` branch asks
    a question only the asserted chemistry's own evaluation can answer — is its BARCODE role
    structurally unfillable while its cDNA role is fillable? — so a spec that was never scored makes
    the branch unreachable and the refusal degrades to a generic one, or to a question.

    Read sets are what made it bite. A deposit of one cDNA file used to leave the narrowed pool EMPTY,
    and ``or runnable`` then handed back the whole KB, so the asserted spec was scored by accident;
    ``bulk-rnaseq``'s single-end set now keeps that pool non-empty and the accident stops happening
    (#309, GSE208154). Restoring the evaluation explicitly is what makes the branch depend on the
    assertion rather than on whether some other spec happened to fit.

    Cheap and winner-invariant by the same proof it repairs: at most ONE extra spec, scored only when
    a hypothesis names a runnable node the narrowing dropped, and length-infeasibility guarantees the
    result is ``forbidden`` — so it never enters ``valid``, never joins a tie set and never becomes a
    candidate. It reaches ``evaluations`` alone, which is exactly where the refusal reads.
    """
    tech = resolve_chemistry_id(hypothesis_value, specs)
    if tech is None:
        return pool
    asserted = specs[tech]
    if asserted.backend is None:  # an abstract family node classifies but never scores
        return pool
    return pool if any(spec is asserted for spec in pool) else [*pool, asserted]


def resolve_dataset(
    # Sequence, not list: the engine only iterates. `list` is invariant, so a caller holding a
    # perfectly good list[Path] could not pass it without a copy — an API defect, not a caller bug.
    paths: Sequence[str | Path],
    *,
    registry: OnlistRegistry | None = None,
    specs: dict[str, Spec] | None = None,
    hypothesis: Hypothesis | None = None,
    workspace: str | Path = ".",
    max_reads: int = DEFAULT_MAX_READS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    use_cache: bool = True,
    cpus: int = 1,
    score_threads: int = 1,
    _probed: dict[str, tuple[Observation, list[str]]] | None = None,
) -> ResolveOutput:
    """Score a dataset's FASTQ files against the KB and return the ranked, escalated verdict.

    ``cpus`` bounds a per-file probe pool; ``_probed`` lets a caller (``resolve_runs``) hand in a probe
    map it already computed across the whole dataset, so the files are not probed twice.
    ``score_threads`` bounds a per-spec scoring thread pool (the onlist scan releases the GIL): a
    standalone call reuses ``cpus`` for it, while ``resolve_runs`` hands an explicit value to
    coordinate with its per-run fork and stay within the core budget.
    """
    registry = registry if registry is not None else DEFAULT_REGISTRY
    kb_specs = specs if specs is not None else load_all_specs()
    cache = Cache(workspace)

    probed = (
        _probed
        if _probed is not None
        else _probe_paths(paths, max_reads=max_reads, max_bytes=max_bytes, cpus=cpus)
    )
    observations: list[Observation] = []
    wps: list[WindowProbe] = []
    for path in paths:
        obs, seqs = probed[str(path)]
        if use_cache:
            cache.write_observation(obs)
        observations.append(obs)
        wps.append(WindowProbe(observation=obs, seqs=seqs))

    ds_id = dataset_id(
        [o.file.sha256 for o in observations], KB_VERSION, PROBE_VERSION, RESOLVE_VERSION
    )

    # Descent narrows the scored pool WITHOUT changing the winner: (1) an ABSTRACT family node
    # classifies but has no runnable backend, so it is never a candidate and is excluded; (2)
    # `length_feasible` is the scorer's own length gate, so any spec it drops would have scored
    # `forbidden` anyway (a proven necessary condition) — and it asks that of EVERY read set the spec
    # declares, because `build_tech_evaluation` scores every set and keeps the best, so a spec is
    # forbidden only when all of them are. The trailing `or runnable` is the mandatory fallback —
    # narrowing may never leave the pool empty — and it is also why a maximal-set-only feasibility test
    # would have broken this quietly: a wrongly-dropped spec is simply never scored whenever some other
    # spec keeps the pool non-empty. `escalate` still receives the FULL `kb_specs` so id/confusable
    # lookups resolve for unscored nodes.
    runnable = [spec for spec in kb_specs.values() if spec.backend is not None]
    pool = [spec for spec in runnable if length_feasible(spec, wps)] or runnable
    hv = hypothesis.value if hypothesis else None
    pool = _with_asserted(pool, hv, kb_specs)
    # A standalone call runs probe then score sequentially, so the per-spec pool may reuse the full
    # `cpus` budget; resolve_runs hands `_probed` + an explicit `score_threads` to stay bounded.
    if _probed is None:
        score_threads = max(score_threads, cpus)
    evaluations = _score_pool(pool, wps, registry, score_threads)
    hid = hypothesis.id if hypothesis else None
    hconf = hypothesis.confidence if hypothesis else 0.0
    esc = escalate(evaluations, observations, kb_specs, hv, hid, hconf)

    result = ResolveResult(
        dataset_id=ds_id,
        kb_version=KB_VERSION,
        rung_reached=esc.rung_reached,
        candidates=esc.candidates,
        conflicts=esc.conflicts,
        questions=esc.questions,
        blockers=esc.blockers,
    )
    matrices = {e.tech: e.matrix_json() for e in evaluations}
    if use_cache:
        cache.write_resolve(ds_id, result)
        # The evidence matrix as a cache sidecar keyed by the same per-run ds_id: it is what the
        # human glance layer (`seqforge report`) reads to explain WHY this chemistry won. run/fill
        # never consume it, so it is written here and nowhere required — the resume fast-path leaves
        # matrices empty and the report degrades to per-candidate scores.
        cache.write_matrices(ds_id, matrices)
    return ResolveOutput(result=result, matrices=matrices, observations=observations)


#: A read at or below this many bases is a technical sample index (10x I1/I2 are 8-10 bp), well under
#: any CB+UMI read (>= 26 bp). The gate is a SAFETY, not decoration: a longer leftover — a stray
#: cDNA-length file — stays unassigned so ``validate`` still blocks it loudly.
INDEX_MAX_LEN = 20

#: The read designation a demultiplexed FASTQ carries — the mate the sequencer assigned it. Illumina/
#: bcl2fastq writes it as an ``R1``/``R2``/``I1``/``I2`` token between separators, before the trailing
#: ``_001`` set number (``..._S1_L001_R1_001.fastq.gz`` -> ``R1``). This is the identity a surplus lane
#: or flowcell file shares with its role representative: unlike a de-laned basename it carries NO
#: flowcell id, so it fuses the reads of one accession across every flowcell it was sequenced on — the
#: flowcell id legitimately differs between them (GSE208154), which a lane-token strip could not bridge.
_ILLUMINA_DESIGNATION = re.compile(r"[._]([RI][1-4])(?:[._]\d{3})?$", re.IGNORECASE)
#: fasterq-dump's numeric mate suffix (``SRR..._1`` / ``_2`` / ``_3``) — the SRA equivalent of the
#: Illumina token, mirroring ``group.py``'s ``_MATE`` shape. Tried only when no Illumina token is found.
_NUMERIC_DESIGNATION = re.compile(r"[._](?:read[-_]?)?([1-4])(?:[._]\d{3})?$", re.IGNORECASE)
#: A surplus lane/flowcell file must also match its role representative's read length (a sanity guard
#: beside the designation). Small on purpose: 10x roles sit far apart (index <= 20, barcode ~26-28,
#: cDNA >= 50), so the tolerance admits a lane's minor length jitter without ever bridging two roles.
_LANE_LEN_TOL = 3

#: Extensions stripped before reading the trailing designation token — longest first.
_FASTQ_EXTS = (".fastq.gz", ".fq.gz", ".fastq.bz2", ".fastq.xz", ".fastq", ".fq", ".gz")


def _read_designation(basename: str) -> str | None:
    """The mate/read designation a filename declares — ``R1``/``R2``/``I1`` (Illumina) or ``1``/``2``/
    ``3`` (fasterq-dump), or ``None`` when it declares none.

    This — not a de-laned basename — is what a surplus lane or flowcell file shares with its role
    representative. It carries no flowcell id, so it groups the reads of one accession sequenced across
    several flowcells (GSE208154), which stripping the ``_L\\d{3}`` lane token alone could not: the
    flowcell id differs between them, so their de-laned names differed and the surplus stayed unassigned.
    """
    name = basename
    lowered = name.lower()
    for ext in _FASTQ_EXTS:
        if lowered.endswith(ext):
            name = name[: -len(ext)]
            break
    illumina = _ILLUMINA_DESIGNATION.search(name)
    if illumina is not None:
        return illumina.group(1).upper()
    numeric = _NUMERIC_DESIGNATION.search(name)
    if numeric is not None:
        return numeric.group(1)
    return None


def index_tagged_roles(winner: Candidate, observations: Iterable[Observation]) -> dict[str, str]:
    """Invert a winner's role assignment to ``sha -> role``, absorbing surplus lane/flowcell files.

    The base map is ``assignment`` (role -> sha) inverted. Then, **only for a run the bytes actually
    decided** (a ``scored`` winner), each unassigned leftover is placed:

    - read length index-sized (<= :data:`INDEX_MAX_LEN`) -> :data:`~seqforge.models.dataset.INDEX_ROLE`,
      a 10x sample-index file STARsolo never consumes, set aside rather than left to block;
    - otherwise, if it carries the same **read designation** (R1/R2/…) as an assigned role's
      representative and matches its read length -> that role. An accession sequenced across 8 lanes of
      2 flowcells groups into one run holding 16 R1 + 16 R2 + 16 I1, but the injective assignment fills
      each role with ONE file, leaving the rest surplus. Every lane/flowcell of one read shares its
      designation — the flowcell id, which a de-laned name still carries, legitimately differs across the
      flowcells one accession spans — so a surplus file rejoins its role by designation + length.
      ``units.tsv`` then emits every lane and STARsolo comma-joins them (``--readFilesIn R2a,R2b ...``).

    Keying on the designation, not length alone, is deliberate: a stray leftover whose designation
    matches no role's representative (a dropped/mis-uploaded read, or an undesignated file) stays
    unassigned, so ``validate`` still blocks it loudly; and the ``len(matches) == 1`` gate refuses an
    ambiguous file that could fit two roles. A ``forbidden`` winner decided nothing, so its leftovers are
    not reinterpreted. A clean single-lane run has no leftovers and is byte-identical to before.
    """
    roles = {sha: role for role, sha in winner.role_assignment.assignment.items()}
    if winner.score.status == "scored":
        by_sha = {o.file.sha256: o for o in observations}
        rep = {
            role: (by_sha[sha].read_length.mode, _read_designation(by_sha[sha].file.basename))
            for role, sha in winner.role_assignment.assignment.items()
            if sha in by_sha
        }
        for sha in winner.role_assignment.unassigned:
            obs = by_sha.get(sha)
            if obs is None:
                continue
            mode = obs.read_length.mode
            if mode <= INDEX_MAX_LEN:
                roles[sha] = INDEX_ROLE
                continue
            designation = _read_designation(obs.file.basename)
            if designation is None:
                continue
            matches = [
                role
                for role, (rmode, rdesig) in rep.items()
                if rdesig == designation and abs(rmode - mode) <= _LANE_LEN_TOL
            ]
            if len(matches) == 1:
                roles[sha] = matches[0]
    return roles


@dataclass(frozen=True)
class RunResolution:
    """One run: the files that came from it, and what the bytes said they are."""

    run_id: str
    paths: list[Path]
    output: ResolveOutput

    @property
    def winner(self) -> str | None:
        cands = self.output.result.candidates
        return cands[0].technology if cands else None


def role_of_sha_for(runs: Iterable[RunResolution]) -> dict[str, str]:
    """Merged file-sha -> role across ``runs`` (all of a dataset, or just one assay's slice).

    A `RoleAssignment` maps role -> ONE sha, because it describes one library's reads. Six runs of one
    library have six R1s, so the dataset-level fact is the inverse map, and it only exists once each
    run has been assigned on its own bytes. A run's short leftovers (10x I1/I2 index files) are tagged
    ``index`` — set aside, not dropped — gated on read length per run.
    """
    merged: dict[str, str] = {}
    for run in runs:
        for cand in run.output.result.candidates[:1]:
            merged.update(index_tagged_roles(cand, run.output.observations))
    return merged


@dataclass(frozen=True)
class MultiRunOutput:
    """Every run in a dataset, resolved independently, plus the cross-run agreement check."""

    runs: list[RunResolution]
    blockers: list[Blocker] = field(default_factory=list)

    @property
    def observations(self) -> list[Observation]:
        return [o for r in self.runs for o in r.output.observations]

    def role_of_sha(self) -> dict[str, str]:
        """The dataset-wide file-sha -> role map. The manifest's inventory is built from this."""
        return role_of_sha_for(self.runs)

    def by_chemistry(self) -> dict[str, list[RunResolution]]:
        """Partition the runs by the chemistry each resolved to — one group per **assay**.

        A large project (study) naturally contains several assays: groups of samples that share one
        processing recipe (chemistry). Runs whose bytes decided nothing (``winner is None``) are
        omitted — they carry their own blocker and cannot name an assay. Keyed order is sorted so the
        partition is deterministic.
        """
        groups: dict[str, list[RunResolution]] = {}
        for run in self.runs:
            if run.winner is not None:
                groups.setdefault(run.winner, []).append(run)
        return {tech: groups[tech] for tech in sorted(groups)}

    def chemistry_of_sha(self) -> dict[str, str]:
        """file-sha -> the chemistry its run resolved to. The join for the per-sample agreement check."""
        out: dict[str, str] = {}
        for run in self.runs:
            if run.winner is None:
                continue
            for obs in run.output.observations:
                out[obs.file.sha256] = run.winner
        return out

    def sample_disagreements(self, sample_shas: dict[str, list[str]]) -> list[Blocker]:
        """A sample whose files span more than one chemistry blocks — that IS a mis-grouping.

        Runs of ONE sample resolve to one chemistry, always. Runs of *different* samples may resolve
        to different chemistries — that is a legal partition into assays (:meth:`by_chemistry`), not a
        disagreement. So the invariant is per-sample, checked against the sample->files map the
        metadata resolver builds; the byte resolver alone cannot see it (filenames group into runs,
        records join runs into samples).
        """
        chem_of = self.chemistry_of_sha()
        blockers: list[Blocker] = []
        for sample_id, shas in sorted(sample_shas.items()):
            techs = sorted({chem_of[s] for s in shas if s in chem_of})
            if len(techs) > 1:
                blockers.append(
                    Blocker(
                        id=f"blk-sample-chemistry-{sample_id}",
                        code=BlockerCode.UNRESOLVED_CONFLICT,
                        message=(
                            f"sample {sample_id!r} has files resolving to more than one chemistry "
                            f"({', '.join(techs)}). Runs of one sample are one library and must "
                            f"resolve to one chemistry, so either these files are not all this "
                            f"sample's or they were grouped into runs incorrectly."
                        ),
                        remedy=(
                            "Check the file->sample join (the archive records, or the filenames) and "
                            "the run grouping. Different chemistries across DIFFERENT samples are a "
                            "legal multi-assay project; within one sample they are not."
                        ),
                        subject=BlockerSubject(kind="dataset", ref=sample_id),
                        evidence=sorted(shas),
                    )
                )
        return blockers

    def exit_code(self, *, excluding: Collection[str] = ()) -> int:
        """The dataset's byte-gate exit code: 3 if the reduction itself blocked, else the max over
        the runs — one run's blocker or one run's open question is the dataset's.

        ``excluding`` drops named run ids from that max, and has exactly one caller: a plate whose
        cell ABSTAINED (:data:`CellOutcome`). That cell's question was answered by inheritance and is
        recorded as a resolved ``Conflict``, so leaving it in the max would refuse the whole plate at
        exit 4 over a cell that no longer asks anything. It is a keyword on the existing method
        rather than a second loop beside it so the two can never disagree about what a run's exit
        code is.
        """
        if self.blockers:
            return 3
        return max(
            (r.output.exit_code() for r in self.runs if r.run_id not in excluding), default=0
        )


#: Which gate turned a dataset down, in the order :func:`reduce_dataset` asks them. ``cell`` is one
#: cell of a plate dissenting outright from the chemistry the plate resolved to; ``run`` is a run
#: that did not resolve on its own bytes (or asked); ``metadata`` is the record join refusing;
#: ``sample`` is one sample's files spanning two chemistries; ``assay`` is the defensive floor —
#: nothing left to name an assay with, which the ``run`` gate has already caught in every case that
#: reaches it. Named rather than inferred from the exit code, because four of the five are exit 3
#: and a caller rendering a refusal has to say which one.
RefusalGate = Literal["cell", "run", "metadata", "sample", "assay"]

#: What one cell's own bytes said about the chemistry its plate resolved to, under a spec declaring
#: ``identity.sample_is_cell``. Three outcomes, and the third is not a hedge: a cell that neither
#: agrees nor dissents outright has ABSTAINED, and abstaining is a verdict about the cell rather than
#: about the plate. There is deliberately no fourth — and no fifth judgement TYPE either, since an
#: abstention rides the existing resolved-``Conflict`` channel (ADR-0006's ceiling of four).
CellOutcome = Literal["conforms", "contradicts", "abstains"]


def _distinct(items: Iterable[_ModelT]) -> list[_ModelT]:
    """``items`` with exact duplicates dropped, first occurrence kept.

    One question asked by fifty-six runs is one question. Ids here are stable strings the escalator
    writes (``q-chemistry``, ``conflict-barcode-length``), not run-scoped, so identical objects
    really are the same claim about the same dataset — while anything that differs, in any field,
    survives as its own row.
    """
    seen: set[str] = set()
    out: list[_ModelT] = []
    for item in items:
        key = item.model_dump_json()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def plate_chemistries(multi: MultiRunOutput, specs: dict[str, Spec] | None = None) -> list[str]:
    """The ``identity.sample_is_cell`` chemistries this dataset's runs actually DECIDED.

    Empty is the answer for every shipped spec but the plate one, and therefore for every deposit
    that is not a plate, which is what makes :func:`_plate_gate` inert rather than merely cheap.

    A run that ASKED names nothing here, however its candidate list is ordered: the plate's chemistry
    is what its cells DECIDED, and a plate asserted only by cells that declined to answer would be a
    chemistry the deposit inherits from nobody.

    It is public because `manifest fill` has to ask it *before* it decides whether to pay for the
    record join. That guard reads ``multi.exit_code() == 0`` — a dataset whose bytes did not decide
    has never paid for a join — and a plate is the one shape where a run asking is not the same as
    the bytes not deciding: the sibling cells decided, and the reduction cannot judge the asking one
    without the sample map. Asking here keeps the widening exactly that narrow. A deposit where NO
    cell decided names no plate, so it still pays nothing.
    """
    kb_specs = specs if specs is not None else load_all_specs()
    return sorted(
        {
            run.winner
            for run in multi.runs
            if run.winner is not None
            and not run.output.result.questions
            and (spec := kb_specs.get(run.winner)) is not None
            and spec.identity.sample_is_cell
        }
    )


@dataclass(frozen=True)
class _PlateGate:
    """One plate's cells, each judged against the chemistry the plate itself resolved to.

    **A conjunction, not a vote.** A single cell dissenting outright refuses the dataset, so no cell
    is ever outvoted by its siblings and the gate creates no new authority over the bytes: every
    verdict here is still the per-run verdict the byte resolver already reached. The consequence is
    accepted deliberately — a deposit genuinely holding a plate *and* a separate bulk library now
    refuses, which is the safe direction, and the remedy is to compile them separately.
    """

    #: The chemistry the plate resolved to — the one ``sample_is_cell`` id its cells decided.
    chemistry: str
    #: run id -> what that cell's bytes said. A run absent from this map was judged by no cell rule
    #: (it asked about chemistries the plate is not among), and the ``run`` gate still owns it.
    outcome: dict[str, CellOutcome]
    #: One resolved ``Conflict`` per abstaining cell — admitted without byte confirmation.
    conflicts: list[Conflict]
    #: One ``Blocker`` per outright dissent.
    blockers: list[Blocker]

    @property
    def abstained(self) -> frozenset[str]:
        return frozenset(rid for rid, o in self.outcome.items() if o == "abstains")


def _run_reads(run: RunResolution) -> int:
    """A run's read count: the **minimum** over its files.

    The minimum and not the sum or the mean, because a paired run's reads are FRAGMENTS — R1 and R2
    are two views of the same molecule, so summing would report a 901-read cell as 1802 and clear a
    1000-read floor on a cell that has 901 of anything. The minimum is what the shallowest file can
    support, which is what the aligner will actually see.
    """
    return min((o.estimated_total_reads for o in run.output.observations), default=0)


def _tie_set(result: ResolveResult) -> set[str]:
    """The chemistries a run's bytes left within θ of its best — what the run was ASKED about.

    Recomputed from the ranked candidates rather than carried on them: the escalator's tie is an
    ordering fact about one evaluation, and the reduction is the first thing that needs it a second
    time. ``equivalence_members`` are folded in because a benign twin recorded alongside a tie member
    is the same byte-level claim under another name.
    """
    values = [c.score.value for c in result.candidates if c.score.value is not None]
    if not values:
        return set()
    best = max(values)
    return {
        tech
        for c in result.candidates
        if c.score.value is not None and best - c.score.value <= _THETA
        for tech in (c.technology, *c.equivalence_members)
    }


def _cell_of_run(multi: MultiRunOutput, sample_shas: dict[str, list[str]]) -> dict[str, str]:
    """run id -> the id of the ``Sample`` (the CELL) it belongs to.

    Inverted from the sample -> files map, which is the *join* the reduction already owns and
    already materialises for its per-sample chemistry gate. Reading it does not cross ADR-0010: no
    attribute the metadata resolver DECIDED is consulted, only which files it grouped together.

    A run whose files no sample claims stands as its own cell. That is the honest fallback rather
    than a refusal, because a dataset with no accession has no records to join and `group.py`'s
    filename grouping IS the sample identity there — one run, one cell, which is the 1:1 plate.
    """
    sample_of_sha = {sha: sid for sid, shas in sample_shas.items() for sha in shas}
    cell: dict[str, str] = {}
    for run in multi.runs:
        found = next(
            (
                sample_of_sha[o.file.sha256]
                for o in run.output.observations
                if o.file.sha256 in sample_of_sha
            ),
            None,
        )
        cell[run.run_id] = found if found is not None else run.run_id
    return cell


def _starved_cells(
    multi: MultiRunOutput, cell_of_run: dict[str, str], floor: int | None
) -> dict[str, int]:
    """Cell id -> its read depth, for every cell that does not clear ``floor``. Empty when unset.

    **The threshold gates the Sample, summed over its runs** — per-run count is the minimum over its
    files (:func:`_run_reads`), per-cell is the sum over its runs. Gating the run instead would make
    a floor of 1000 silently mean 500 on exactly the 10.5% of plates that are not 1:1, which is the
    population the ``Sample`` wording of ``sample_is_cell`` exists for.
    """
    if floor is None:
        return {}
    depth: dict[str, int] = {}
    for run in multi.runs:
        cell = cell_of_run[run.run_id]
        depth[cell] = depth.get(cell, 0) + _run_reads(run)
    return {cell: n for cell, n in depth.items() if n < floor}


def _inherited_conflict(run: RunResolution, plate: str, note: str) -> Conflict:
    """A cell admitted to the plate **without byte confirmation**, recorded so it is not silent.

    ``status="resolved"`` puts it on the existing auditable-but-non-blocking channel: it surfaces in
    the report as "37 of 1440 cells were admitted without byte confirmation" and it moves no exit
    code and no hash. Rejected: a fifth judgement type — four is a deliberate ceiling (ADR-0006), and
    an inheritance is precisely a disagreement between two truths that code settled, which is what
    the fourth already is.

    The inherited position is ``inferred`` and not ``observed``: nothing was observed on THIS cell's
    bytes that says ``plate``. Its rung is 1 — the identity prior (which files are one sample) is
    what carried the answer across, not any measurement of these reads.
    """
    said = run.winner or "undecided"
    top = next((c.score.value for c in run.output.result.candidates), None)
    shas = sorted(o.file.sha256 for o in run.output.observations)
    return Conflict(
        id=f"conflict-cell-unconfirmed-{run.run_id}",
        field="library.chemistry",
        positions=[
            ConflictPosition(value=plate, basis="inferred", evidence=shas, confidence=0.0),
            ConflictPosition(
                value=said,
                basis="observed",
                evidence=shas,
                confidence=top if top is not None else 0.0,
            ),
        ],
        kind="other",
        decidable_by=["reads"],
        status="resolved",
        resolution=Resolution(
            chosen_value=plate, basis="inferred", rung=1, decided_by="code", note=note
        ),
    )


def _dissent_blocker(run: RunResolution, plate: str) -> Blocker:
    """One cell deciding a DIFFERENT chemistry outright — the refusal that kills the silent split.

    Without it, `by_chemistry` reads the difference as a legal partition into assays and the plate
    compiles as two, at exit 0, each half a study. That reading is correct for a real multi-assay
    project and catastrophic for a plate; ``sample_is_cell`` is the only thing that tells them apart.
    """
    return Blocker(
        id=f"blk-cell-chemistry-{run.run_id}",
        code=BlockerCode.UNRESOLVED_CONFLICT,
        message=(
            f"{plate} declares that one sample IS one cell, so every cell in this deposit is one "
            f"library of one chemistry — but cell {run.run_id!r} resolves to {run.winner} on its own "
            f"bytes, outright. A dissenting cell is not outvoted by its siblings: without this "
            f"refusal the plate would partition into two assays and compile at exit 0."
        ),
        remedy=(
            "If the deposit really does hold a plate AND a separate library, compile them "
            "separately — one --fastq-dir each. If it does not, this cell's files are mis-grouped "
            "or the wrong file was uploaded for it."
        ),
        subject=BlockerSubject(kind="dataset", ref=run.run_id),
        evidence=sorted(o.file.sha256 for o in run.output.observations),
    )


def _plate_gate(
    multi: MultiRunOutput, metadata: MetadataResolution | None, specs: dict[str, Spec]
) -> _PlateGate | None:
    """Judge every cell of a plate against the chemistry the plate resolved to, or ``None``.

    ``None`` — no run decided a ``sample_is_cell`` chemistry — is the answer for every dataset the
    sixteen non-plate specs can describe, and it is what makes the whole gate inert rather than
    merely cheap: :func:`reduce_dataset` then takes the byte-for-byte path it took before this
    existed.

    Scoring is untouched and stays per run (ADR-0010 is not crossed either way): every verdict read
    here is one a run already reached on its own bytes, and the only new thing is the sum, which
    happens in the reduction after every run has independently resolved. **Nothing pools** — a
    pooled winner's role assignment would map roles to the pool's pseudo-shas, leaving every real
    file role-less, so pooling does not remove the per-cell pass, it removes the honest one.
    """
    plates = plate_chemistries(multi, specs)
    if not plates:
        return None
    if metadata is None:
        raise ValueError(
            "reduce_dataset needs the metadata resolution for a dataset whose bytes named a "
            f"one-sample-is-one-cell chemistry ({', '.join(plates)}): the admission threshold is "
            "summed over a Sample's runs, and the sample -> files map is where that join lives"
        )
    if metadata.blockers:
        # The join REFUSED, so the sample -> files map is not one anything may be summed over — a
        # cell would be judged against a depth built from files nobody could place. Gate 2 refuses
        # the dataset a few lines below; this gate has nothing trustworthy to say first.
        return None
    sample_shas = {s.sample_id: list(s.file_shas) for s in metadata.samples}
    if len(plates) > 1:
        # Two plate chemistries is every cell of each dissenting from the other, so there is no
        # chemistry to inherit and nothing to arbitrate: naming one of them the plate's would be the
        # vote this gate refuses to hold.
        return _PlateGate(
            chemistry=plates[0],
            outcome={},
            conflicts=[],
            blockers=[
                Blocker(
                    id="blk-cell-chemistry-plates",
                    code=BlockerCode.UNRESOLVED_CONFLICT,
                    message=(
                        f"this deposit's cells resolve to more than one chemistry that declares one "
                        f"sample IS one cell ({', '.join(plates)}). Each is an outright dissent from "
                        f"the other, so there is no plate chemistry to inherit."
                    ),
                    remedy="Compile each plate separately — one --fastq-dir each.",
                    subject=BlockerSubject(kind="dataset", ref=plates[0]),
                    evidence=plates,
                ),
            ],
        )

    plate = plates[0]
    cell_of_run = _cell_of_run(multi, sample_shas)
    starved = _starved_cells(multi, cell_of_run, specs[plate].min_input_reads)
    floor = specs[plate].min_input_reads
    outcome: dict[str, CellOutcome] = {}
    conflicts: list[Conflict] = []
    blockers: list[Blocker] = []
    for run in multi.runs:
        cell = cell_of_run[run.run_id]
        if cell in starved:
            # The threshold is asked FIRST, and that order is the whole point of having it: a starved
            # cell that decided a different chemistry outright is the measured case (GSE207085's cell
            # 1291 decides bulk on 901 reads, and is proved unwinnable by any weighting). Asked
            # second, it would dissent and refuse the plate before its depth was ever consulted.
            outcome[run.run_id] = "abstains"
            conflicts.append(
                _inherited_conflict(
                    run,
                    plate,
                    f"cell {cell!r} carries {starved[cell]} reads, under {plate}'s "
                    f"min_input_reads of {floor}: too few for its bytes to speak for it, so it "
                    f"inherits the plate's chemistry rather than dissenting from it",
                )
            )
        elif run.output.result.questions:
            # A cell that ASKED did not decide anything, whatever leads its candidate list — so it is
            # asked about abstention BEFORE conformance, or a run whose question happens to top out
            # on the plate's chemistry would be counted as agreeing with an answer it declined to
            # give. It inherits only when the plate's answer is already one of the things its own
            # bytes said; a cell asking about a set the plate is not in has not abstained about THIS
            # plate at all, so it is left out of the map and the `run` gate still refuses the dataset
            # at exit 4 — a human, not this gate, decides what that one was.
            if plate in _tie_set(run.output.result):
                outcome[run.run_id] = "abstains"
                conflicts.append(
                    _inherited_conflict(
                        run,
                        plate,
                        f"cell {cell!r} could not separate {plate} from the rest of its tie set, so "
                        f"the plate's chemistry was already one of the answers its own bytes gave",
                    )
                )
        elif run.winner == plate:
            outcome[run.run_id] = "conforms"
        elif run.winner is not None:
            outcome[run.run_id] = "contradicts"
            blockers.append(_dissent_blocker(run, plate))
        # else: no candidate at all — the run's bytes named nothing, it carries its own Blocker, and
        # the `run` gate refuses at exit 3. There is no chemistry here to conform to or dissent from.
    return _PlateGate(chemistry=plate, outcome=outcome, conflicts=conflicts, blockers=blockers)


def _plate_assays(
    multi: MultiRunOutput, assays: dict[str, list[RunResolution]], gate: _PlateGate
) -> dict[str, list[RunResolution]]:
    """The partition with every abstaining cell moved into the plate's group — the "inherits" half.

    Abstainers are APPENDED, so the group's first run is a conforming one wherever any cell conforms.
    That matters: `manifest fill` builds the assay's manifest from ``runs[0].output.result``, and a
    cell that abstained is precisely the one whose result must not name the assay's chemistry.
    """
    moved = {r.run_id for r in multi.runs if gate.outcome.get(r.run_id) == "abstains"}
    out = {
        tech: kept
        for tech, runs in assays.items()
        if (kept := [r for r in runs if r.run_id not in moved])
    }
    out[gate.chemistry] = out.get(gate.chemistry, []) + [r for r in multi.runs if r.run_id in moved]
    return {tech: out[tech] for tech in sorted(out)}


@dataclass(frozen=True)
class DatasetResolution:
    """A dataset's independently-resolved runs, reduced to the ONE verdict a consumer acts on.

    **It exists because there are two consumers and there was one implementation.** `manifest fill`
    made this reduction inline; the eval harness that measures `manifest fill` skipped it entirely
    and called :func:`resolve_dataset` on a whole dataset's file list, which scores those files as
    ONE library and hands out one global (R1, R2) assignment. On any multi-run dataset the benchmark
    therefore graded a code path the product had abandoned — 11 of the 18 benchmark cases (#196),
    green only because those corpora happen to be homogeneous. That is the same shape as the
    divergence :func:`chemistry_hypothesis` closed, and the same cure: one reduction, both callers.

    Nothing here re-decides anything. Every field is read off :class:`MultiRunOutput` and the
    metadata resolution; the gates and their order are exactly the ones `manifest fill` asked.
    """

    #: The runs, each resolved on its own bytes. Observations and the role map are read off it.
    runs: MultiRunOutput
    #: One group per **assay** — the samples sharing one chemistry. More than one group is a legal
    #: partition of a large project, not an error; empty means no run named a chemistry at all.
    assays: dict[str, list[RunResolution]]
    #: The gate that turned this dataset down, or ``None`` when it got through all five.
    refused_at: RefusalGate | None
    #: The DATASET-level reasons behind ``refused_at``. A run's own blockers stay on that run — they
    #: are already in :attr:`result` and in the per-run payload a caller renders.
    blockers: list[Blocker]
    #: The uniform exit contract: 0 decide, 3 refuse, 4 ask.
    exit_code: int
    #: DATASET-level conflicts the reduction itself raised. Today that is exactly one kind: a cell of
    #: a plate admitted without byte confirmation, recorded ``resolved`` so it is auditable and
    #: non-blocking. Empty for every dataset no shipped spec's ``sample_is_cell`` describes.
    conflicts: list[Conflict] = field(default_factory=list)
    #: Run ids whose own questions and blockers the reduction set aside, because the cell abstained
    #: and inherited its plate's chemistry. Each one is answered by a row in :attr:`conflicts`, so
    #: dropping it from the verdict loses no judgement — it moves it to the channel that records a
    #: settled one.
    abstained: frozenset[str] = frozenset()

    @property
    def observations(self) -> list[Observation]:
        return self.runs.observations

    @property
    def _judging_runs(self) -> list[RunResolution]:
        """The runs whose surfaced judgements are still the dataset's. Every run, minus the cells
        that abstained — identical to ``self.runs.runs`` wherever no plate is in play."""
        return [r for r in self.runs.runs if r.run_id not in self.abstained]

    def role_of_sha(self) -> dict[str, str]:
        """The dataset-wide file-sha -> role map. A six-run dataset has six R1s; this is where they
        are, and a single run's `RoleAssignment` cannot express it."""
        return self.runs.role_of_sha()

    @property
    def result(self) -> ResolveResult:
        """One :class:`ResolveResult` for the whole dataset, for a consumer that grades or reports one.

        The representative is the first run of the first assay — the same run `manifest fill` builds
        an assay's manifest from (``result=runs[0].output.result``), because every run of an assay
        agreed on the chemistry and so any one of them is the assay's. A dataset that refused before
        it could name an assay falls back to its first run, which is where the refusal is written.

        It carries **every** run's conflicts and questions, which is the other half of what the front
        door does (``conflicts = [c for run in runs for c in ...]``). A question raised by run 12 is
        the dataset's question: a consumer handed run 0's result alone would see the dataset exit 4
        with nothing open on it, and report a refusal it could not name.

        Its blockers are every run's **and** :attr:`blockers` — the dataset's own, from the
        ``metadata`` and ``sample`` gates. Those refuse without any run refusing, so a result holding
        only the runs' blockers would be empty on exactly the two gates this reduction added: a
        consumer reading one result would see exit 3 and no code to name it by, which is the same
        unnameable refusal one run's-worth of conflicts would have been.

        An ABSTAINING cell contributes none of the three and contributes :attr:`conflicts` instead.
        Its question was answered by inheritance, and a question that has been answered must not
        still be asked: carried through, one cell of fourteen hundred asking would refuse the whole
        plate at exit 4 over a cell the reduction already admitted.
        """
        runs = next(iter(self.assays.values()), None) or self.runs.runs
        if not runs:
            raise ValueError(
                "a dataset with no runs has no result — resolve_runs was given no files"
            )
        judging = self._judging_runs
        return runs[0].output.result.model_copy(
            update={
                "conflicts": _distinct(
                    [c for r in judging for c in r.output.result.conflicts] + self.conflicts
                ),
                "questions": _distinct(q for r in judging for q in r.output.result.questions),
                "blockers": _distinct(
                    [b for r in judging for b in r.output.result.blockers] + self.blockers
                ),
            }
        )


def reduce_dataset(
    multi: MultiRunOutput,
    metadata: MetadataResolution | None = None,
    *,
    specs: dict[str, Spec] | None = None,
) -> DatasetResolution:
    """Reduce N independently-resolved runs + the metadata resolution to one dataset-level verdict.

    Five gates, asked in this order, each of which is a refusal a caller renders its own way:

    0. **a cell of a plate dissented** — under a chemistry declaring ``identity.sample_is_cell``,
       one cell deciding a *different* chemistry outright (:func:`_plate_gate`). Asked first because
       a dissent is the strongest claim available about a plate and must not be masked by a sibling
       cell's question; inert, and the four gates below are byte-for-byte what they were, wherever no
       run decided a ``sample_is_cell`` chemistry — which is every dataset the sixteen non-plate
       specs can describe;
    1. **a run did not resolve** — ``multi.exit_code()`` is the max over the runs, so one run's
       blocker (exit 3) or one run's open question (exit 4) is the dataset's. An abstaining cell is
       excluded from that max: it no longer asks anything, and what it inherited is recorded;
    2. **the record join refused** — a record whose runs do not match the files on disk;
    3. **a sample spans two chemistries** — the relocated "runs must agree" invariant, per-SAMPLE
       (:meth:`MultiRunOutput.sample_disagreements`). Across *different* samples a difference is a
       legal partition into assays; within one it is a mis-grouping;
    4. **nothing named an assay** — the defensive floor. Every run whose bytes decided nothing
       carries its own blocker, so gate 1 has already caught this in practice.

    Gates 0 and 3 read the SAME cross-sample difference and part on one declared fact. Across
    different samples that difference is a legal partition into assays — correct for a real
    multi-assay project, and catastrophic for a plate, where it splits one experiment in two at exit
    0. ``sample_is_cell`` is the only thing that tells the two apart, which is why it is declared.

    ``metadata`` is read for exactly two things: the sample -> files map gate 3 needs, and the same
    map summed into per-cell read depths for gate 0's threshold. No attribute it resolved is
    consulted, and none may be — the two resolvers are not shown each other's input (ADR-0010), and
    this is their join, not a channel between them.

    ``None`` means the caller has not run the join, which is legal only when a gate that needs no
    join refuses first — `manifest fill` has never paid for a record join over a dataset whose bytes
    did not decide, and making this function the sole caller of it would have changed that. Reaching
    gate 2 without one RAISES rather than proceeding: an empty resolution would sail through gate 3
    (no samples, no disagreements) and silently drop the per-sample invariant this reduction exists
    to apply. A dataset whose bytes named a plate raises at gate 0 for the same reason.
    """
    assays = multi.by_chemistry()
    plate = _plate_gate(multi, metadata, specs if specs is not None else load_all_specs())
    if plate is not None:
        assays = _plate_assays(multi, assays, plate)
    abstained = plate.abstained if plate is not None else frozenset()
    inherited = plate.conflicts if plate is not None else []

    def _refused(gate: RefusalGate, blockers: list[Blocker], code: int) -> DatasetResolution:
        return DatasetResolution(
            runs=multi,
            assays=assays,
            refused_at=gate,
            blockers=blockers,
            exit_code=code,
            conflicts=inherited,
            abstained=abstained,
        )

    if plate is not None and plate.blockers:
        return _refused("cell", plate.blockers, 3)
    if (code := multi.exit_code(excluding=abstained)) != 0:
        return _refused("run", list(multi.blockers), code)
    if metadata is None:
        raise ValueError(
            "reduce_dataset needs the metadata resolution for a dataset whose every run resolved: "
            "the per-sample chemistry-agreement gate is checked against the sample -> files map"
        )
    if metadata.blockers:
        return _refused("metadata", list(metadata.blockers), 3)
    sample_shas = {s.sample_id: list(s.file_shas) for s in metadata.samples}
    if sample_blockers := multi.sample_disagreements(sample_shas):
        return _refused("sample", sample_blockers, 3)
    if not assays:
        return _refused("assay", [], 3)
    return DatasetResolution(
        runs=multi,
        assays=assays,
        refused_at=None,
        blockers=[],
        exit_code=0,
        conflicts=inherited,
        abstained=abstained,
    )


def _resolve_one_run(
    item: tuple[str, list[Path]],
    *,
    registry: OnlistRegistry,
    specs: dict[str, Spec],
    hypothesis: Hypothesis | None,
    workspace: str | Path,
    max_reads: int,
    max_bytes: int,
    use_cache: bool,
    score_threads: int,
    probed: dict[str, tuple[Observation, list[str]]],
) -> RunResolution:
    """Resolve ONE run's files on their own bytes, reusing the dataset-wide probe map."""
    run_id, run_paths = item
    output = resolve_dataset(
        run_paths,
        registry=registry,
        specs=specs,
        hypothesis=hypothesis,
        workspace=workspace,
        max_reads=max_reads,
        max_bytes=max_bytes,
        use_cache=use_cache,
        score_threads=score_threads,
        _probed=probed,
    )
    return RunResolution(run_id=run_id, paths=list(run_paths), output=output)


#: Context a forked scoring worker inherits from its parent (see ``resolve_runs``). Set in the parent
#: right before the fork pool; carried by fork inheritance, never pickled, so the warm registry is not
#: rebuilt per worker (and its pages are shared copy-on-write where CPython's refcounting allows).
_RUN_CTX: dict[str, object] = {}


def _resolve_run_shared(item: tuple[str, list[Path]]) -> RunResolution:
    """Fork worker: resolve one run from the parent's COW-inherited ``_RUN_CTX`` (a warm registry whose
    packed whitelist is shared read-only). Only the run's own paths cross the process boundary."""
    # `_RUN_CTX` carries `_resolve_one_run`'s keywords across the fork, so it is heterogeneous by
    # construction; `**` on a `dict[str, object]` loses the per-key types.
    return _resolve_one_run(item, **_RUN_CTX)  # type: ignore[arg-type]


def _resume_payload(runs: list[RunResolution]) -> dict[str, object]:
    """The stat-keyed resume pointer: per run, its id, its dataset_id, and its file content-keys."""
    return {
        "runs": [
            {
                "run_id": r.run_id,
                "dataset_id": r.output.result.dataset_id,
                "file_keys": [o.file.sha256 for o in r.output.observations],
            }
            for r in runs
        ]
    }


def _try_resume_runs(
    grouped: dict[str, list[Path]], paths: Sequence[str | Path], cache: Cache
) -> MultiRunOutput | None:
    """Rebuild a :class:`MultiRunOutput` entirely from cache — reading ZERO FASTQ bytes — or ``None``.

    The stat key (:func:`resume_key`) says the input files are byte-for-byte the last run's; the run
    grouping is recomputed deterministically from filenames; each run's ``ResolveResult`` and its
    files' ``Observation``s are then loaded from the content-addressed cache. Any missing/stale piece
    aborts the resume (return ``None``) and the caller probes + scores afresh. ``matrices`` is scoring
    debug output the ``run``/``fill`` path does not consume, so it is left empty on resume.
    """
    rk = resume_key(paths, KB_VERSION, PROBE_VERSION, RESOLVE_VERSION)
    if rk is None:
        return None
    payload = cache.read_resume(rk)
    if payload is None:
        return None
    runs_meta = payload.get("runs")
    if not isinstance(runs_meta, list) or len(runs_meta) != len(grouped):
        return None
    runs: list[RunResolution] = []
    for meta in runs_meta:
        if not isinstance(meta, dict):
            return None
        run_id = meta.get("run_id")
        ds = meta.get("dataset_id")
        file_keys = meta.get("file_keys")
        if (
            not isinstance(run_id, str)
            or not isinstance(ds, str)
            or not isinstance(file_keys, list)
        ):
            return None
        run_paths = grouped.get(run_id)
        if run_paths is None or len(file_keys) != len(run_paths):
            return None
        obs_list = [cache.read_observation(k) if isinstance(k, str) else None for k in file_keys]
        if any(o is None for o in obs_list):
            return None
        result = cache.read_resolve(ds)
        if result is None:
            return None
        # Restore each file's own path: the observation cache is keyed by content-address, which
        # same-named identical-content files in different runs can share, so a resumed run must take
        # `local_uri` from its input path — not the (possibly another file's) cached value — to stay
        # byte-identical to a fresh run.
        observations = [
            o.model_copy(update={"file": o.file.model_copy(update={"local_uri": str(path)})})
            for path, o in zip(run_paths, obs_list, strict=True)
            if o is not None
        ]
        output = ResolveOutput(result=result, matrices={}, observations=observations)
        runs.append(RunResolution(run_id=run_id, paths=list(run_paths), output=output))
    if {r.run_id for r in runs} != set(grouped):
        return None  # duplicate/mismatched run ids -> the cached shape is stale
    order = list(grouped)
    runs.sort(key=lambda r: order.index(r.run_id))
    return MultiRunOutput(runs=runs)


def resolve_runs(
    paths: Sequence[str | Path],
    *,
    registry: OnlistRegistry | None = None,
    specs: dict[str, Spec] | None = None,
    hypothesis: Hypothesis | None = None,
    workspace: str | Path = ".",
    max_reads: int = DEFAULT_MAX_READS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    use_cache: bool = True,
    cpus: int = 1,
    _probed: dict[str, tuple[Observation, list[str]]] | None = None,
) -> MultiRunOutput:
    """Group `paths` into runs and resolve **each run on its own bytes**.

    This is the multi-run entry point, and it exists because `resolve_dataset` answers "what is this
    ONE library?" — correctly, and always did. Handing it a 6-run dataset's 12 files was the bug: one
    global assignment picks a single (R1, R2) pair out of twelve and leaves ten files with no role,
    which `_units` skips and `validate` blesses. Five sixths of the data, gone, exit 0.

    Nothing here re-decides roles or reads a byte differently. It splits the input by filename (a
    rung-1 prior about *identity*, never about role — see `group.py`) and resolves each group.

    **Runs may resolve to different chemistries, and that is a partition, not an error.** A large
    project contains several assays; :meth:`MultiRunOutput.by_chemistry` groups the runs into them.
    The safety the old dataset-wide "all runs must agree" block provided is now per-SAMPLE
    (:meth:`MultiRunOutput.sample_disagreements`): runs of ONE sample must resolve to one chemistry,
    but that check needs the sample->files map only the metadata resolver builds, so it is applied by
    the caller (never a majority vote — a sample split across chemistries blocks loudly).

    ``_probed`` lets a caller hand in a probe map it already built across the whole dataset — the
    fingerprint path passes pinned stand-in observations (``fingerprint.load.probed_from_fingerprint``)
    so a run over head-slices resolves exactly as the full FASTQs would. When it is given, the byte read
    is already done: the resume shortcut and the probe pool are both skipped.
    """
    from .group import group_runs

    grouped = group_runs(paths)
    cache = Cache(workspace)
    # Disk is state: if the input files are byte-for-byte the last run's, rebuild the whole answer from
    # the content-addressed cache and read ZERO FASTQ bytes (the "resumable" the design promises). A
    # caller-supplied `_probed` is already that answer's front half, so skip the resume probe.
    if use_cache and _probed is None:
        resumed = _try_resume_runs(grouped, paths, cache)
        if resumed is not None:
            return resumed
    # Probe every file of every run ONCE, in one pool across the whole dataset (12 files, not 2 a
    # run), then hand each run its slice. Probing per-run would cap parallelism at a run's file count.
    # A pre-built map (fingerprint consumption) is used as-is — the bytes were already read.
    probed = (
        _probed
        if _probed is not None
        else _probe_paths(
            [p for run_paths in grouped.values() for p in run_paths],
            max_reads=max_reads,
            max_bytes=max_bytes,
            cpus=cpus,
        )
    )
    registry = registry if registry is not None else DEFAULT_REGISTRY
    kb_specs = specs if specs is not None else load_all_specs()
    run_items = list(grouped.items())

    import multiprocessing as mp

    # Parallelism has two axes sharing ONE core budget: per-run forking (one worker per run) and
    # per-spec scoring threads inside each run. Fork is required for per-run, not just preferred: the
    # worker reads the warm registry from `_RUN_CTX`, which a forked child inherits copy-on-write but a
    # `spawn` child would re-import empty. Split the budget so the two axes never oversubscribe: when W
    # runs fork concurrently each gets cpus//W scoring threads; a serial or single run gets them all.
    parallel_runs = cpus > 1 and len(run_items) > 1 and "fork" in mp.get_all_start_methods()
    if parallel_runs:
        n_run_workers = min(cpus, len(run_items) - 1)  # first run warms in-process; the rest fork
        score_threads = max(1, cpus // n_run_workers)
    else:
        score_threads = max(1, cpus)
    common: dict[str, object] = dict(
        registry=registry,
        specs=kb_specs,
        hypothesis=hypothesis,
        workspace=workspace,
        max_reads=max_reads,
        max_bytes=max_bytes,
        use_cache=use_cache,
        score_threads=score_threads,
        probed=probed,
    )

    if not parallel_runs:
        # `common` is `_resolve_one_run`'s keyword set bundled once for both paths — heterogeneous,
        # so `dict[str, object]`, and `**` on that loses the per-key types.
        runs = [_resolve_one_run(it, **common) for it in run_items]  # type: ignore[arg-type]
    else:
        # Warm the shared registry in-process on the FIRST run (this parses the onlists once), then
        # fork workers for the rest: each inherits the warm registry, so the packed whitelist (millions
        # of barcodes) is not re-parsed per worker, and its pages are shared copy-on-write where
        # refcounting allows. Peak memory stays bounded by `--cpus`. Scoring is deterministic per run,
        # so the parallel result is identical to the serial one; runs are reassembled in the order
        # `group_runs` yields (sorted by run key) — the same order the serial path uses. Core count
        # folds into no hash — parallelism is not a budget (see `_probe_paths`).
        from concurrent.futures import ProcessPoolExecutor

        first = _resolve_one_run(run_items[0], **common)  # type: ignore[arg-type]
        rest_items = run_items[1:]
        results: dict[int, RunResolution] = {}
        # Publish the warm context for the fork workers to inherit, and clear it in `finally` so the
        # parent never pins the warm registry (millions of barcodes) past the pool — even on a raise.
        _RUN_CTX.update(common)
        try:
            with ProcessPoolExecutor(
                max_workers=min(cpus, len(rest_items)), mp_context=mp.get_context("fork")
            ) as pool:
                futures = {
                    pool.submit(_resolve_run_shared, it): i for i, it in enumerate(rest_items)
                }
                for fut in futures:
                    results[futures[fut]] = fut.result()
        finally:
            _RUN_CTX.clear()
        runs = [first, *(results[i] for i in range(len(rest_items)))]

    # Record the stat-keyed resume pointer so an unchanged re-run skips probe+score entirely.
    if use_cache:
        rk = resume_key(paths, KB_VERSION, PROBE_VERSION, RESOLVE_VERSION)
        if rk is not None:
            cache.write_resume(rk, _resume_payload(runs))
    return MultiRunOutput(runs=runs)
