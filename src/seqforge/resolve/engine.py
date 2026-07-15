"""``resolve score`` orchestration: probe -> per-tech evidence -> escalation -> ResolveResult.

Deterministic and LLM-free. The optional ``hypothesis`` (a span-verified metadata assertion) is a
control-flow input only — it selects/orders and can break a genuinely-non-decisive tie, but never
enters the evidence matrix. Every stage is content-addressed under ``.seqforge/`` (R7): the per-file
Observation and the dataset ResolveResult are cached, so a killed run resumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..io import DEFAULT_REGISTRY, OnlistRegistry
from ..kb import KB_VERSION, load_all_specs
from ..kb.schema import Spec
from ..models.resolve import ResolveResult
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
    """The engine's return: the wire :class:`ResolveResult` plus the JSON-safe evidence matrices."""

    result: ResolveResult
    matrices: dict[str, dict[str, dict[str, dict[str, object]]]]

    def exit_code(self) -> int:
        return exit_code_for(self.result)


def exit_code_for(result: ResolveResult) -> int:
    """Uniform exit contract: 3 BLOCKED (>=1 Blocker), 4 NEEDS_HUMAN (open Conflict/question), else 0."""
    if result.blockers:
        return 3
    if result.questions or any(c.status == "open" for c in result.conflicts):
        return 4
    return 0


def resolve_dataset(
    paths: list[str | Path],
    *,
    registry: OnlistRegistry | None = None,
    specs: dict[str, Spec] | None = None,
    hypothesis: Hypothesis | None = None,
    workspace: str | Path = ".",
    max_reads: int = DEFAULT_MAX_READS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    use_cache: bool = True,
) -> ResolveOutput:
    """Score a dataset's FASTQ files against the KB and return the ranked, escalated verdict."""
    registry = registry if registry is not None else DEFAULT_REGISTRY
    kb_specs = specs if specs is not None else load_all_specs()
    cache = Cache(workspace)

    observations = []
    wps: list[WindowProbe] = []
    for path in paths:
        obs, seqs = probe_sample(path, max_reads=max_reads, max_bytes=max_bytes)
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
    return ResolveOutput(result=result, matrices=matrices)
