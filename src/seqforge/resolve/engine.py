"""``resolve score`` orchestration: probe -> per-tech evidence -> escalation -> ResolveResult.

Deterministic and LLM-free. The optional ``hypothesis`` (a span-verified metadata assertion) is a
control-flow input only — it selects/orders and can break a genuinely-non-decisive tie, but never
enters the evidence matrix. Every stage is content-addressed under ``.seqforge/``: the per-file
Observation and the dataset ResolveResult are cached, so a killed run resumes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel

from ..io import DEFAULT_REGISTRY, OnlistNotAvailable, OnlistRegistry
from ..kb import KB_VERSION, load_all_specs
from ..kb.match import carries, resolve_chemistry
from ..kb.schema import Spec
from ..models.assertion import Assertion
from ..models.blocker import Blocker, BlockerCode, BlockerSubject
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
# prevent the very conflict it is for.
from .escalate import _barcode_read_id, escalate
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
    # `forbidden` anyway (a proven necessary condition). The trailing `or runnable` is the mandatory
    # fallback — narrowing may never leave the pool empty. `escalate` still receives the FULL `kb_specs`
    # so id/confusable lookups resolve for unscored nodes.
    runnable = [spec for spec in kb_specs.values() if spec.backend is not None]
    pool = [spec for spec in runnable if length_feasible(spec, wps)] or runnable
    # A standalone call runs probe then score sequentially, so the per-spec pool may reuse the full
    # `cpus` budget; resolve_runs hands `_probed` + an explicit `score_threads` to stay bounded.
    if _probed is None:
        score_threads = max(score_threads, cpus)
    evaluations = _score_pool(pool, wps, registry, score_threads)
    hv = hypothesis.value if hypothesis else None
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

    def exit_code(self) -> int:
        if self.blockers:
            return 3
        return max((r.output.exit_code() for r in self.runs), default=0)


#: Which gate turned a dataset down, in the order :func:`reduce_dataset` asks them. ``run`` is a run
#: that did not resolve on its own bytes (or asked); ``metadata`` is the record join refusing;
#: ``sample`` is one sample's files spanning two chemistries; ``assay`` is the defensive floor —
#: nothing left to name an assay with, which the ``run`` gate has already caught in every case that
#: reaches it. Named rather than inferred from the exit code, because three of the four are exit 3
#: and a caller rendering a refusal has to say which one.
RefusalGate = Literal["run", "metadata", "sample", "assay"]


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
    #: The gate that turned this dataset down, or ``None`` when it got through all four.
    refused_at: RefusalGate | None
    #: The DATASET-level reasons behind ``refused_at``. A run's own blockers stay on that run — they
    #: are already in :attr:`result` and in the per-run payload a caller renders.
    blockers: list[Blocker]
    #: The uniform exit contract: 0 decide, 3 refuse, 4 ask.
    exit_code: int

    @property
    def observations(self) -> list[Observation]:
        return self.runs.observations

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

        It carries **every** run's conflicts, questions and blockers, which is the other half of what
        the front door does (``conflicts = [c for run in runs for c in ...]``). A question raised by
        run 12 is the dataset's question: a consumer handed run 0's result alone would see the
        dataset exit 4 with nothing open on it, and report a refusal it could not name.
        """
        runs = next(iter(self.assays.values()), None) or self.runs.runs
        if not runs:
            raise ValueError(
                "a dataset with no runs has no result — resolve_runs was given no files"
            )
        return runs[0].output.result.model_copy(
            update={
                "conflicts": _distinct(
                    c for r in self.runs.runs for c in r.output.result.conflicts
                ),
                "questions": _distinct(
                    q for r in self.runs.runs for q in r.output.result.questions
                ),
                "blockers": _distinct(b for r in self.runs.runs for b in r.output.result.blockers),
            }
        )


def reduce_dataset(multi: MultiRunOutput, metadata: MetadataResolution) -> DatasetResolution:
    """Reduce N independently-resolved runs + the metadata resolution to one dataset-level verdict.

    Four gates, asked in this order, each of which is a refusal a caller renders its own way:

    1. **a run did not resolve** — ``multi.exit_code()`` is the max over the runs, so one run's
       blocker (exit 3) or one run's open question (exit 4) is the dataset's;
    2. **the record join refused** — a record whose runs do not match the files on disk;
    3. **a sample spans two chemistries** — the relocated "runs must agree" invariant, per-SAMPLE
       (:meth:`MultiRunOutput.sample_disagreements`). Across *different* samples a difference is a
       legal partition into assays; within one it is a mis-grouping;
    4. **nothing named an assay** — the defensive floor. Every run whose bytes decided nothing
       carries its own blocker, so gate 1 has already caught this in practice.

    ``metadata`` is read for exactly one thing: the sample -> files map gate 3 needs. No attribute
    it resolved is consulted, and none may be — the two resolvers are not shown each other's input
    (ADR-0010), and this is their join, not a channel between them.
    """
    assays = multi.by_chemistry()

    def _refused(gate: RefusalGate, blockers: list[Blocker], code: int) -> DatasetResolution:
        return DatasetResolution(
            runs=multi, assays=assays, refused_at=gate, blockers=blockers, exit_code=code
        )

    if (code := multi.exit_code()) != 0:
        return _refused("run", list(multi.blockers), code)
    if metadata.blockers:
        return _refused("metadata", list(metadata.blockers), 3)
    sample_shas = {s.sample_id: list(s.file_shas) for s in metadata.samples}
    if sample_blockers := multi.sample_disagreements(sample_shas):
        return _refused("sample", sample_blockers, 3)
    if not assays:
        return _refused("assay", [], 3)
    return DatasetResolution(runs=multi, assays=assays, refused_at=None, blockers=[], exit_code=0)


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
