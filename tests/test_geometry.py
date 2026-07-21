"""Tests for the geometry feasibility predicate — the winner-invariance foundation.

The load-bearing property: ``length_feasible`` (and its pairwise wrapper ``geometry_could_accept``) is
a *necessary condition* for a valid score, so narrowing on it can never drop a spec the full scorer
would have accepted. We prove that over every shipped spec pair by asserting the implication
``accepts_at_rungs_0_2(a, probes[b]) => geometry_could_accept(a, probes[b])``.
"""

from __future__ import annotations

import gzip
from pathlib import Path

from seqforge import kb
from seqforge.io import DEFAULT_REGISTRY
from seqforge.kb.schema import Spec
from seqforge.probe import probe_file
from seqforge.resolve.confuse import accepts_at_rungs_0_2
from seqforge.resolve.geometry import (
    geometry_could_accept,
    geometry_fingerprint,
    length_feasible,
)
from seqforge.resolve.scoring import build_tech_evaluation
from seqforge.resolve.window import WindowProbe


def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    with gzip.open(path, "wt") as fh:
        for i, s in enumerate(seqs):
            fh.write(f"@SIM:{i}\n{s}\n+\n{'I' * len(s)}\n")


def _probes_for(spec: Spec, workdir: Path) -> list[object]:
    reads = kb.generate_reads(spec, n=400, seed=0)
    out: list[object] = []
    for read_id, seqs in reads.items():
        path = workdir / f"{spec.identity.id.replace('/', '_')}_{read_id}.fastq.gz"
        _write_fastq_gz(path, seqs)
        out.append(WindowProbe(observation=probe_file(path), seqs=seqs[:200]))
    return out


def test_fingerprint_is_deterministic() -> None:
    for tech_id in kb.list_spec_ids():
        spec = kb.load_spec(tech_id)
        assert geometry_fingerprint(spec) == geometry_fingerprint(spec)


def test_a_spec_is_length_feasible_against_its_own_reads(tmp_path: Path) -> None:
    for tech_id in kb.list_spec_ids():
        spec = kb.load_spec(tech_id)
        probes = [p for p in _probes_for(spec, tmp_path) if isinstance(p, WindowProbe)]
        assert length_feasible(spec, probes), f"{tech_id} must accept its own synthetic reads"


def test_geometry_could_accept_is_necessary_for_rung02_acceptance(tmp_path: Path) -> None:
    """The guarantee the confusability guard and the runtime shortlist rely on.

    If ``a`` accepts ``b``'s reads at rungs 0-2 (a real confusable), then ``a`` must be geometry-feasible
    against ``b``'s reads — so skipping geometry-infeasible pairs can never miss a real confusable. The
    founding cross-geometry collision (``bulk-rnaseq-pe`` accepts ``splitseq``) must therefore still be
    seen by ``geometry_could_accept``.
    """
    ids = kb.list_spec_ids()
    specs = {i: kb.load_spec(i) for i in ids}
    probes = {i: _probes_for(specs[i], tmp_path) for i in ids}

    for a in ids:
        for b in ids:
            if accepts_at_rungs_0_2(specs[a], probes[b]):
                assert geometry_could_accept(specs[a], probes[b]), (
                    f"{a!r} accepts {b!r}'s reads at rungs 0-2 but geometry_could_accept says no — "
                    "the necessary-condition guarantee is broken and the guard/shortlist would be unsound"
                )


def test_descent_narrowing_never_drops_a_valid_spec(tmp_path: Path) -> None:
    """WINNER-INVARIANCE: the descent pool (length-feasible specs) never excludes a spec that would
    score VALID with the full registry (rung 3 included) — so scoring the pool yields the identical
    winner as scoring the whole runnable KB. This is the property the whole "narrow, don't change the
    answer" design rests on, checked over every real leaf dataset against every runnable spec.
    """
    specs = kb.load_all_specs()
    runnable = [s for s in specs.values() if s.backend is not None]
    for tech in kb.runnable_spec_ids():
        wps = [p for p in _probes_for(specs[tech], tmp_path) if isinstance(p, WindowProbe)]
        for spec in runnable:
            if length_feasible(spec, wps):
                continue
            ev = build_tech_evaluation(spec, wps, DEFAULT_REGISTRY)
            assert not ev.valid, (
                f"length_feasible dropped {spec.identity.id!r} on {tech!r}'s reads, yet it scores "
                "VALID — narrowing would change the winner, breaking winner-invariance"
            )
