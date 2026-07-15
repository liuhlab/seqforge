"""Tests for ``resolve``: assignment, matrix JSON-safety, the §12 fixture, and escalation branches."""

from __future__ import annotations

import gzip
import json
import random
from math import inf
from pathlib import Path
from typing import Any

import pytest

from seqforge import kb
from seqforge.io import OnlistRegistry
from seqforge.kb.schema import Spec
from seqforge.models.resolve import TechScore
from seqforge.resolve import resolve_dataset
from seqforge.resolve.assign import AssignmentResult, _brute, _hungarian_assign, best_assignment
from seqforge.resolve.escalate import escalate
from seqforge.resolve.scoring import Cell, TechEvaluation


# ---------- fixtures ----------
def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    with gzip.open(path, "wt") as fh:
        for i, s in enumerate(seqs):
            fh.write(f"@SIM:{i}\n{s}\n+\n{'I' * len(s)}\n")


def _registry_for(spec: Spec, *, seed: int = 0, pool_size: int = 64) -> OnlistRegistry:
    """A synthetic registry whose registry-names are backed by the generator's barcode pools."""
    pools = kb.build_pools(spec, seed=seed, pool_size=pool_size)
    reg = OnlistRegistry(offline=True)
    for alias, ref in spec.onlists.items():
        if alias in pools:
            reg.register_synthetic(ref.registry, pools[alias])
    return reg


# ---------- assignment ----------
def test_hungarian_matches_brute_force() -> None:
    rng = random.Random(0)
    for _ in range(40):
        n = rng.randint(2, 5)
        score = [[rng.random() for _ in range(n)] for _ in range(n)]
        forbidden = [[rng.random() < 0.2 for _ in range(n)] for _ in range(n)]
        prior = [[0.0] * n for _ in range(n)]
        brute = _brute(n, n, score, forbidden, prior)
        hung = _hungarian_assign(n, n, score, forbidden, prior)
        if brute is None:
            assert hung is None
        else:
            assert hung is not None
            assert hung[1] == pytest.approx(brute[1])  # same optimal raw value


def test_assignment_forbidden_diagonal_forces_swap() -> None:
    # role0 forbidden on file0, role1 forbidden on file1 -> only the swap is valid
    res = best_assignment(
        2, 2, [[0.9, 0.5], [0.5, 0.9]], [[True, False], [False, True]], [[0, 0], [0, 0]]
    )
    assert res.valid and res.mapping == {0: 1, 1: 0}


def test_assignment_unfillable_role_is_reported() -> None:
    # role0 forbidden on every file -> structurally unfillable -> invalid + flagged
    res = best_assignment(
        2, 2, [[0.0, 0.0], [0.5, 0.5]], [[True, True], [False, False]], [[0, 0], [0, 0]]
    )
    assert not res.valid
    assert res.unfillable_roles == [0]


def test_assignment_leftover_file_is_unassigned() -> None:
    res = best_assignment(1, 3, [[0.9, 0.1, 0.1]], [[False, False, False]], [[0, 0, 0]])
    assert res.valid and res.mapping == {0: 0}
    assert set(res.unassigned_files) == {1, 2}


# ---------- §12 end-to-end ----------
def test_resolve_10x_fixture_decides_v3(tmp_path: Path) -> None:
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=1500, seed=0)
    f1 = tmp_path / "sample_R1.fastq.gz"  # 28 bp barcode read
    f2 = tmp_path / "sample_R2.fastq.gz"  # ~cDNA
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])

    out = resolve_dataset(
        [f1, f2],
        registry=_registry_for(spec),
        workspace=tmp_path,
        use_cache=True,
    )
    result = out.result
    assert out.exit_code() == 0
    assert not result.blockers and not result.questions
    winner = result.candidates[0]
    assert winner.technology == "10x-3p-gex-v3"
    assert winner.score.status == "scored"
    # benign twin recorded together (§12), 0 questions
    assert "10x-3p-gex-v3.1" in winner.equivalence_members
    # onlist evidence fired -> rung 3
    assert result.rung_reached == 3
    # both roles assigned to distinct files (R1 = barcode read, R2 = cDNA read)
    assigned = winner.role_assignment.assignment
    assert set(assigned) == {"R1", "R2"}
    assert assigned["R1"] != assigned["R2"]
    # a resumable artifact was written
    assert (tmp_path / ".seqforge" / "candidates" / f"{result.dataset_id}.json").is_file()


def test_resolve_bulk_pe_no_barcode(tmp_path: Path) -> None:
    spec = kb.load_spec("bulk-rnaseq-pe")
    reads = kb.generate_reads(spec, n=1200, seed=0)
    f1 = tmp_path / "bulk_R1.fastq.gz"
    f2 = tmp_path / "bulk_R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])
    out = resolve_dataset([f1, f2], use_cache=False)  # no onlist needed for the no-barcode branch
    assert out.exit_code() == 0
    assert out.result.candidates[0].technology == "bulk-rnaseq-pe"
    assert out.result.rung_reached == 2  # geometry-only: no onlist involved


def test_resolve_splitseq_beats_generic_bulk_via_onlist(tmp_path: Path) -> None:
    # SPLiT-seq's specific evidence (3 round onlists + fixed linkers, rung 3) must dominate the
    # generic bulk fallback that merely fails to be forbidden (rung 2) — a Decision, not a question.
    spec = kb.load_spec("splitseq")
    reads = kb.generate_reads(spec, n=1200, seed=0)
    f_cdna = tmp_path / "sp_cdna.fastq.gz"
    f_bc = tmp_path / "sp_bc.fastq.gz"
    _write_fastq_gz(f_cdna, reads["cdna"])
    _write_fastq_gz(f_bc, reads["bc"])
    out = resolve_dataset([f_cdna, f_bc], registry=_registry_for(spec), use_cache=False)
    assert out.exit_code() == 0
    assert not out.result.questions
    assert out.result.candidates[0].technology == "splitseq"
    assert out.result.rung_reached == 3


def test_resolve_matrix_is_json_safe(tmp_path: Path) -> None:
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=800, seed=1)
    f1 = tmp_path / "R1.fastq.gz"
    f2 = tmp_path / "R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])
    out = resolve_dataset([f1, f2], registry=_registry_for(spec, seed=1), use_cache=False)
    blob = json.dumps(out.matrices)  # must serialize: no inf/nan anywhere
    assert "Infinity" not in blob and "NaN" not in blob
    v3 = out.matrices["10x-3p-gex-v3"]
    # the cDNA-length file is forbidden for the barcode role R1 (segment_length gate)
    statuses = {cell["status"] for cell in v3["R1"].values()}
    assert statuses == {"scored", "forbidden"}


# ---------- escalation branches (synthetic candidates) ----------
def _mini_spec(tech_id: str, confusables: list[dict[str, Any]] | None = None) -> Spec:
    data: dict[str, Any] = {
        "schema_version": 1,
        "identity": {"id": tech_id, "version": "1", "name": tech_id, "modality": "rna"},
        "reads": [
            {
                "id": "R1",
                "seqspec_read_id": "R1",
                "min_len": 20,  # variable -> _spec_barcode_length is None -> no length conflict
                "max_len": 30,
                "elements": [
                    {
                        "type": "barcode",
                        "name": "CB",
                        "start": 0,
                        "end": 16,
                        "onlist": "wl",
                        "seqspec_region_type": "barcode",
                    },
                ],
            },
            {
                "id": "R2",
                "seqspec_read_id": "R2",
                "min_len": 25,
                "max_len": None,
                "elements": [
                    {
                        "type": "cdna",
                        "name": "cdna",
                        "start": 0,
                        "end": None,
                        "seqspec_region_type": "cdna",
                    },
                ],
            },
        ],
        "onlists": {"wl": {"registry": f"reg-{tech_id}", "role": "cell_barcode"}},
        "signature": {
            "requires": [{"test": "read_count", "roles": 2}],
            "supports": [],
            "excludes": [],
        },
        "backend": {"module": "map/starsolo", "params": {"soloType": "CB_UMI_Simple"}},
        "confusable_with": confusables or [],
    }
    return Spec.model_validate(data)


def _te(
    tech: str, value: float | None, *, rung: int = 3, equiv: list[str] | None = None
) -> TechEvaluation:
    scored = value is not None
    score = TechScore(technology=tech, status="scored" if scored else "forbidden", value=value)
    asg = AssignmentResult(
        valid=scored, mapping={0: 0, 1: 1}, unassigned_files=[], raw=(value or -inf)
    )
    return TechEvaluation(
        tech=tech,
        roles=["R1", "R2"],
        file_shas=["sha-bc", "sha-cdna"],
        matrix={"R1": [Cell(False, value or 0.0)], "R2": [Cell(False, value or 0.0)]},
        assignment=asg,
        score=score,
        rung=rung,
        used_onlist=True,
        equivalence_members=equiv or [],
        barcode_role_ids=["R1"],
        unfillable_role_ids=[],
        cdna_role_fillable=True,
    )


def test_escalate_benign_equivalent_tie_records_both() -> None:
    specs = {
        "techA": _mini_spec(
            "techA",
            [
                {
                    "id": "techB",
                    "relationship": "processing_equivalent",
                    "distinguishable_by": ["none"],
                }
            ],
        ),
        "techB": _mini_spec("techB"),
    }
    esc = escalate([_te("techA", 1.0), _te("techB", 1.0)], [], specs, None, None, 0.0)
    assert esc.winner == "techA"
    assert not esc.questions and not esc.conflicts  # benign: 0 questions
    assert "techB" in esc.candidates[0].equivalence_members


def test_escalate_divergent_tie_asks_a_question() -> None:
    specs = {
        "techA": _mini_spec(
            "techA",
            [
                {
                    "id": "techB",
                    "relationship": "processing_divergent",
                    "distinguishable_by": ["onlist"],
                }
            ],
        ),
        "techB": _mini_spec("techB"),
    }
    esc = escalate([_te("techA", 1.0), _te("techB", 1.0)], [], specs, None, None, 0.0)
    assert esc.winner is None  # unresolved -> a human question
    assert len(esc.questions) == 1
    assert set(esc.questions[0].options) == {"techA", "techB"}
    assert esc.rung_reached == 7


def test_escalate_metadata_disambiguates_divergent_tie() -> None:
    specs = {
        "techA": _mini_spec(
            "techA",
            [
                {
                    "id": "techB",
                    "relationship": "processing_divergent",
                    "distinguishable_by": ["metadata"],
                }
            ],
        ),
        "techB": _mini_spec("techB"),
    }
    # the span-verified hypothesis names techB -> code picks it (rung 0, surfaced)
    esc = escalate(
        [_te("techA", 1.0), _te("techB", 1.0)],
        [],
        specs,
        "techB",
        "h1",
        0.9,
    )
    assert esc.winner == "techB"
    assert not esc.questions


# ---------- §12 benign twins tie EXACTLY, so the representative must be deterministic ----------
def test_escalate_breaks_an_exact_tie_deterministically_regardless_of_input_order() -> None:
    """Two processing-equivalent specs score identically BY CONSTRUCTION — they are byte-identical.

    The old key was ``max(tie, key=lambda e: (e.rung, e.value))``. On an exact tie ``max`` returns the
    first maximal element in ITERATION order, which here traces back to the KB dict — so
    ``candidates[0].technology`` could flip between runs of an unchanged input, and with it the
    manifest's winner field. R7 says a run is resumable and content-addressed; a winner that depends
    on dict ordering is neither.

    Which twin represents the class is arbitrary — that is what "equivalent" means. It still has to be
    arbitrary the SAME way every time, so `tech` is the last key and only after rung and score.
    """
    specs = {
        "techA": _mini_spec(
            "techA",
            [
                {
                    "id": "techB",
                    "relationship": "processing_equivalent",
                    "distinguishable_by": ["none"],
                }
            ],
        ),
        "techB": _mini_spec("techB"),
    }
    a, b = _te("techA", 1.0, equiv=["techB"]), _te("techB", 1.0)
    forward = escalate([a, b], [], specs, None, None, 0.0)
    reverse = escalate([b, a], [], specs, None, None, 0.0)

    assert forward.winner == reverse.winner == "techA"  # lexicographically first, both orders
    assert [c.technology for c in forward.candidates] == [c.technology for c in reverse.candidates]
    # ...and it is still benign: both recorded, zero questions (§12)
    assert not forward.questions and not reverse.questions
    assert forward.candidates[0].equivalence_members == ["techB"]


def test_the_real_kb_benign_twins_tie_and_ask_nothing(tmp_path: Path) -> None:
    """End-to-end on the SHIPPED specs: v3 and v3.1 are the §12 rule's flagship, and now they exist.

    Before the twin was written this path was unreachable — v3 declared a `processing_equivalent` edge
    to a spec that was not in the KB, so the benign branch of `escalate` never once fired on real
    data. It fires here: identical scores, both recorded, zero questions, exit 0.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    pools = kb.build_pools(spec, seed=0)
    reg = OnlistRegistry(offline=True)
    for alias, ref in spec.onlists.items():
        if alias in pools:
            reg.register_synthetic(ref.registry, pools[alias])
    reads = kb.generate_reads(spec, n=600, seed=0)
    paths = []
    for k in ("R1", "R2"):
        p = tmp_path / f"s_{k}.fastq.gz"
        _write_fastq_gz(p, reads[k])
        paths.append(p)

    out = resolve_dataset(paths, registry=reg, use_cache=False)
    scores = {c.technology: c.score.value for c in out.result.candidates}
    assert scores["10x-3p-gex-v3"] == scores["10x-3p-gex-v3.1"], "twins must tie exactly"
    assert out.result.candidates[0].technology == "10x-3p-gex-v3"
    assert out.result.candidates[0].equivalence_members == ["10x-3p-gex-v3.1"]
    assert not out.result.questions, "§12: a benign ambiguity asks NOTHING"
    assert out.exit_code() == 0
