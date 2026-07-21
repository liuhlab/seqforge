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

from ..io import DEFAULT_REGISTRY, OnlistRegistry
from ..kb import KB_VERSION, load_all_specs
from ..kb.schema import Spec
from ..models.blocker import Blocker, BlockerCode, BlockerSubject
from ..models.dataset import INDEX_ROLE
from ..models.observation import Observation
from ..models.resolve import Candidate, ResolveResult
from ..probe import DEFAULT_MAX_BYTES, DEFAULT_MAX_READS, PROBE_VERSION, probe_sample
from . import RESOLVE_VERSION
from .cache import Cache, dataset_id
from .escalate import escalate
from .geometry import length_feasible
from .scoring import TechEvaluation, build_tech_evaluation
from .window import WindowProbe


@dataclass(frozen=True)
class Hypothesis:
    """A span-verified metadata assertion entering ``score`` as a selector / tie-break (§3.4)."""

    value: str
    id: str = "hypothesis"
    confidence: float = 0.8


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
    _probed: dict[str, tuple[Observation, list[str]]] | None = None,
) -> ResolveOutput:
    """Score a dataset's FASTQ files against the KB and return the ranked, escalated verdict.

    ``cpus`` bounds a per-file probe pool; ``_probed`` lets a caller (``resolve_runs``) hand in a probe
    map it already computed across the whole dataset, so the files are not probed twice.
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
    evaluations: list[TechEvaluation] = [
        build_tech_evaluation(spec, wps, registry) for spec in pool
    ]
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
    if use_cache:
        cache.write_resolve(ds_id, result)

    matrices = {e.tech: e.matrix_json() for e in evaluations}
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
    """
    from .group import group_runs

    grouped = group_runs(paths)
    # Probe every file of every run ONCE, in one pool across the whole dataset (12 files, not 2 a
    # run), then hand each run its slice. Probing per-run would cap parallelism at a run's file count.
    probed = _probe_paths(
        [p for run_paths in grouped.values() for p in run_paths],
        max_reads=max_reads,
        max_bytes=max_bytes,
        cpus=cpus,
    )
    runs: list[RunResolution] = []
    for run_id, run_paths in grouped.items():
        output = resolve_dataset(
            run_paths,
            registry=registry,
            specs=specs,
            hypothesis=hypothesis,
            workspace=workspace,
            max_reads=max_reads,
            max_bytes=max_bytes,
            use_cache=use_cache,
            _probed=probed,
        )
        runs.append(RunResolution(run_id=run_id, paths=list(run_paths), output=output))

    return MultiRunOutput(runs=runs)
