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
from seqforge.resolve import resolve_dataset, role_of_sha_for
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
    assert (tmp_path / "seqforge" / "cache" / "candidates" / f"{result.dataset_id}.json").is_file()


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
    manifest's winner field. A run is resumable and content-addressed; a winner that depends
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


# ---------- multi-run: filenames GROUP, bytes ASSIGN ----------
def _six_run_dataset(tmp_path: Path) -> tuple[list[Path], OnlistRegistry]:
    """12 files shaped exactly like the pilot: 6 runs x (_1, _2), SRA-style names.

    `_1`/`_2` come from `fasterq-dump`'s dump order and say NOTHING about which read is the barcode.
    The generator writes the barcode read to `_1` here only because something must go first; every
    assertion below is about roles resolve derived from bytes.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reg = _registry_for(spec)
    paths: list[Path] = []
    for i, acc in enumerate(
        ["SRR28716553", "SRR28716554", "SRR28716555", "SRR28716556", "SRR28716557", "SRR28716558"]
    ):
        reads = kb.generate_reads(spec, n=400, seed=i)
        for mate, role in (("1", "R1"), ("2", "R2")):
            p = tmp_path / f"{acc}_{mate}.fastq.gz"
            _write_fastq_gz(p, reads[role])
            paths.append(p)
    return paths, reg


def test_run_key_groups_by_accession_and_never_by_role() -> None:
    from seqforge.resolve import group_runs, run_key

    assert run_key("SRR28716558_1.fastq.gz") == "SRR28716558"
    assert run_key("SRR28716558_2.fastq.gz") == "SRR28716558"
    # Illumina's lane/chunk naming, and the `_R1_001` suffix that a naive end-anchor misses
    assert run_key("x_S1_L001_R1_001.fastq.gz") == "x_S1_L001"
    assert run_key("s_R1.fastq.gz") == "s"
    # `--include-technical` dumps _1.._4; a _3 that failed to match would become its own bogus run
    assert run_key("SRR1_3.fastq.gz") == "SRR1"
    # single-end: no mate token, so the file is its own run
    assert run_key("reads.fastq.gz") == "reads"

    # #6 (GSE310667): an original-format download keeps the submitter's lane naming AFTER the
    # accession, so the mate token (`_R1_`/`_R2_`) is buried mid-name where the end-anchored strip
    # cannot reach it. The leading accession must still win, or the two mates split into singleton
    # runs and the record join misses every file.
    assert run_key("SRR36109512_11314-RM-1_S1_L005_R1_001.fastq.gz") == "SRR36109512"
    assert run_key("SRR36109512_11314-RM-1_S1_L005_R2_001.fastq.gz") == "SRR36109512"
    # DDBJ/ENA accessions share the shape; a bare accession with no suffix is still its own run
    assert run_key("ERR123_S2_L001_I1_001.fastq.gz") == "ERR123"
    assert run_key("SRR9999999.fastq.gz") == "SRR9999999"

    groups = group_runs(["a_1.fastq.gz", "b_1.fastq.gz", "a_2.fastq.gz"])
    assert groups == {
        "a": [Path("a_1.fastq.gz"), Path("a_2.fastq.gz")],
        "b": [Path("b_1.fastq.gz")],
    }
    # the GSE310667 shape: two mates per accession collapse to one run each, not four singletons
    joined = group_runs(
        [
            "SRR36109512_11314-RM-1_S1_L005_R1_001.fastq.gz",
            "SRR36109512_11314-RM-1_S1_L005_R2_001.fastq.gz",
            "SRR36109513_11314-RM-2_S2_L005_R1_001.fastq.gz",
            "SRR36109513_11314-RM-2_S2_L005_R2_001.fastq.gz",
        ]
    )
    assert set(joined) == {"SRR36109512", "SRR36109513"}
    assert all(len(v) == 2 for v in joined.values())


def test_resolving_six_runs_as_one_library_drops_ten_of_twelve_files(tmp_path: Path) -> None:
    """The bug, pinned. This is what `resolve_dataset` does when handed a whole dataset.

    Not a regression test — `resolve_dataset` is CORRECT here and always was. It answers "what is
    this ONE library?", and 12 files from 6 runs is not one library. The bug was the call, not the
    callee, and this test exists so that stays visible: if someone points a CLI at `resolve_dataset`
    with a multi-run dataset again, this is the behaviour they get.
    """
    paths, reg = _six_run_dataset(tmp_path)
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    winner = out.result.candidates[0]
    assert len(winner.role_assignment.assignment) == 2, "one global (R1, R2) pair out of twelve"
    assert len(winner.role_assignment.unassigned) == 10, "and ten files with no role at all"


def test_resolve_runs_assigns_every_file_in_a_six_run_dataset(tmp_path: Path) -> None:
    """The fix: group by run, assign per run, and every one of the 12 files gets a role."""
    from seqforge.resolve import resolve_runs

    paths, reg = _six_run_dataset(tmp_path)
    multi = resolve_runs(paths, registry=reg, use_cache=False)

    assert len(multi.runs) == 6, "6 accessions -> 6 runs"
    assert [r.run_id for r in multi.runs] == sorted(r.run_id for r in multi.runs)
    assert all(len(r.paths) == 2 for r in multi.runs)
    assert all(r.winner == "10x-3p-gex-v3" for r in multi.runs), "each run decided on its own bytes"
    assert not multi.blockers
    assert multi.exit_code() == 0

    roles = multi.role_of_sha()
    assert len(roles) == 12, "every file has a role -- this is the whole point"
    assert sorted(roles.values()) == ["R1"] * 6 + ["R2"] * 6

    # and no run left anything behind
    for run in multi.runs:
        assert not run.output.result.candidates[0].role_assignment.unassigned


def test_runs_of_different_chemistries_partition_rather_than_block(tmp_path: Path) -> None:
    """Two runs, two chemistries is a legal multi-assay PROJECT now, not a dataset-wide refusal.

    The old "all runs must agree" block moved to per-sample (:meth:`sample_disagreements`): different
    chemistries across different samples partition into assays; only a single sample split across
    chemistries blocks. So resolve_runs itself no longer blocks -- it just resolves each run.
    """
    from seqforge.resolve import resolve_runs

    v3 = kb.load_spec("10x-3p-gex-v3")
    bulk = kb.load_spec("bulk-rnaseq-pe")
    reg = _registry_for(v3)
    paths: list[Path] = []
    for acc, spec, keys in (("SRR1", v3, ("R1", "R2")), ("SRR2", bulk, ("R1", "R2"))):
        reads = kb.generate_reads(spec, n=400, seed=0)
        for mate, role in zip(("1", "2"), keys, strict=True):
            p = tmp_path / f"{acc}_{mate}.fastq.gz"
            _write_fastq_gz(p, reads[role])
            paths.append(p)

    multi = resolve_runs(paths, registry=reg, use_cache=False)
    techs = {r.winner for r in multi.runs}
    if len(techs) < 2:  # pragma: no cover - the fixtures happened to agree; nothing to partition
        pytest.skip(f"both runs resolved to {techs}; this fixture cannot exercise a partition")
    assert not multi.blockers, "a 2-assay project is not a refusal"
    assert set(multi.by_chemistry()) == techs  # it partitions into one group per chemistry


def _two_chemistry_multi(tmp_path: Path):
    """Two runs, two chemistries: SRR1 -> v3, SRR2 -> bulk. A real 2-assay project (skips if they
    happen to agree)."""
    from seqforge.resolve import resolve_runs

    v3 = kb.load_spec("10x-3p-gex-v3")
    bulk = kb.load_spec("bulk-rnaseq-pe")
    reg = _registry_for(v3)
    paths: list[Path] = []
    for acc, spec, keys in (("SRR1", v3, ("R1", "R2")), ("SRR2", bulk, ("R1", "R2"))):
        reads = kb.generate_reads(spec, n=400, seed=0)
        for mate, role in zip(("1", "2"), keys, strict=True):
            p = tmp_path / f"{acc}_{mate}.fastq.gz"
            _write_fastq_gz(p, reads[role])
            paths.append(p)
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    if len({r.winner for r in multi.runs}) < 2:  # pragma: no cover
        pytest.skip("fixtures agreed; cannot exercise a 2-assay partition")
    return multi


def test_by_chemistry_partitions_the_runs_into_assays(tmp_path: Path) -> None:
    multi = _two_chemistry_multi(tmp_path)
    groups = multi.by_chemistry()
    assert set(groups) == {"10x-3p-gex-v3", "bulk-rnaseq-pe"}
    assert [r.run_id for r in groups["10x-3p-gex-v3"]] == ["SRR1"]
    assert [r.run_id for r in groups["bulk-rnaseq-pe"]] == ["SRR2"]
    # Every run lands in exactly one assay, and no run is lost.
    assert sum(len(v) for v in groups.values()) == len(multi.runs)


def test_role_of_sha_for_scopes_to_one_assays_runs(tmp_path: Path) -> None:
    multi = _two_chemistry_multi(tmp_path)
    groups = multi.by_chemistry()
    v3_map = role_of_sha_for(groups["10x-3p-gex-v3"])
    # The v3 assay's role map covers only SRR1's files, none of SRR2's.
    srr1_shas = {o.file.sha256 for o in groups["10x-3p-gex-v3"][0].output.observations}
    assert set(v3_map) <= srr1_shas
    assert set(v3_map) == srr1_shas  # both reads assigned, nothing dropped


def test_chemistry_of_sha_maps_each_file_to_its_runs_chemistry(tmp_path: Path) -> None:
    multi = _two_chemistry_multi(tmp_path)
    chem = multi.chemistry_of_sha()
    for run in multi.runs:
        for obs in run.output.observations:
            assert chem[obs.file.sha256] == run.winner


def test_a_sample_spanning_two_chemistries_blocks_but_two_samples_do_not(tmp_path: Path) -> None:
    multi = _two_chemistry_multi(tmp_path)
    by_run = {r.run_id: [o.file.sha256 for o in r.output.observations] for r in multi.runs}

    # One sample owning BOTH runs' files spans two chemistries -> a mis-grouping, blocks.
    one_sample = {"mixed": by_run["SRR1"] + by_run["SRR2"]}
    blockers = multi.sample_disagreements(one_sample)
    assert len(blockers) == 1
    assert "mixed" in blockers[0].message
    assert blockers[0].remedy

    # Two samples, one chemistry each -> a legal 2-assay project, no block.
    two_samples = {"s1": by_run["SRR1"], "s2": by_run["SRR2"]}
    assert multi.sample_disagreements(two_samples) == []
