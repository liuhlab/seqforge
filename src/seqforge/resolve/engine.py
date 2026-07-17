"""``resolve score`` orchestration: probe -> per-tech evidence -> escalation -> ResolveResult.

Deterministic and LLM-free. The optional ``hypothesis`` (a span-verified metadata assertion) is a
control-flow input only — it selects/orders and can break a genuinely-non-decisive tie, but never
enters the evidence matrix. Every stage is content-addressed under ``.seqforge/``: the per-file
Observation and the dataset ResolveResult are cached, so a killed run resumes.
"""

from __future__ import annotations

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

    evaluations: list[TechEvaluation] = [
        build_tech_evaluation(spec, wps, registry) for spec in kb_specs.values()
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


def index_tagged_roles(winner: Candidate, observations: Iterable[Observation]) -> dict[str, str]:
    """Invert a winner's role assignment to ``sha -> role``, tagging short leftovers as index reads.

    The base map is ``assignment`` (role -> sha) inverted. Then, **only for a run the bytes actually
    decided** (a ``scored`` winner), each unassigned leftover whose observed read length is
    index-sized is tagged :data:`~seqforge.models.dataset.INDEX_ROLE` — a 10x sample-index file
    STARsolo never consumes, set aside rather than left to block. A ``forbidden`` winner decided
    nothing, so its leftovers are not reinterpreted; a cDNA-length leftover stays unassigned and
    ``validate`` blocks it. A clean run has no leftovers and comes back byte-identical to before.
    """
    roles = {sha: role for role, sha in winner.role_assignment.assignment.items()}
    if winner.score.status == "scored":
        mode_of = {o.file.sha256: o.read_length.mode for o in observations}
        for sha in winner.role_assignment.unassigned:
            mode = mode_of.get(sha)
            if mode is not None and mode <= INDEX_MAX_LEN:
                roles[sha] = INDEX_ROLE
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


@dataclass(frozen=True)
class MultiRunOutput:
    """Every run in a dataset, resolved independently, plus the cross-run agreement check."""

    runs: list[RunResolution]
    blockers: list[Blocker] = field(default_factory=list)

    @property
    def observations(self) -> list[Observation]:
        return [o for r in self.runs for o in r.output.observations]

    def role_of_sha(self) -> dict[str, str]:
        """Merged file-sha -> role across every run. The manifest's inventory is built from this.

        A `RoleAssignment` maps role -> ONE sha, because it describes one library's reads. Six runs of
        one library have six R1s, so the dataset-level fact is the inverse map, and it only exists
        once each run has been assigned on its own bytes. A run's short leftovers (10x I1/I2 index
        files) are tagged ``index`` here — set aside, not dropped — gated on read length per run.
        """
        merged: dict[str, str] = {}
        for run in self.runs:
            for cand in run.output.result.candidates[:1]:
                merged.update(index_tagged_roles(cand, run.output.observations))
        return merged

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
    rung-1 prior about *identity*, never about role — see `group.py`), resolves each group, and then
    checks that the runs agree with each other.

    **Disagreement is a Blocker, not a vote.** Two runs of one library resolve to the same chemistry;
    if they do not, either the grouping is wrong or these files are not one dataset, and both are
    things a human must look at. Picking the majority would be exactly the silent guess this project
    refuses — and it is also the check that makes filename-grouping safe, because a mis-grouped pair
    fails it loudly.
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

    return MultiRunOutput(runs=runs, blockers=_disagreements(runs))


def _disagreements(runs: list[RunResolution]) -> list[Blocker]:
    """Every run must decide the same chemistry. Surface it; never pick a winner."""
    decided = {r.run_id: r.winner for r in runs if r.winner is not None}
    distinct = sorted({t for t in decided.values() if t is not None})
    if len(distinct) < 2:
        return []
    by_tech: dict[str, list[str]] = {}
    for run_id, tech in decided.items():
        if tech is not None:
            by_tech.setdefault(tech, []).append(run_id)
    detail = "; ".join(
        f"{tech} <- {', '.join(sorted(ids))}" for tech, ids in sorted(by_tech.items())
    )
    return [
        Blocker(
            id="blk-chemistry-disagreement",
            code=BlockerCode.UNRESOLVED_CONFLICT,
            message=(
                f"the runs in this dataset do not agree on a chemistry: {detail}. Runs of one "
                f"library resolve to one chemistry, so either these files are not one dataset or "
                f"they were grouped into runs incorrectly."
            ),
            remedy=(
                "Check the grouping (`seqforge resolve score` one run at a time to see each "
                "verdict), or compose the runs as separate datasets. Do not merge them: a manifest "
                "that averages two chemistries describes neither."
            ),
            subject=BlockerSubject(kind="dataset", ref="library.chemistry"),
            evidence=sorted(decided),
        )
    ]
