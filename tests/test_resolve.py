"""Tests for ``seqforge.resolve`` — the byte resolver: which chemistry, and which file is which read.

One file per package, so an agent editing ``resolve/`` knows which file to run. This was six files
(``test_geometry``/``test_negatives``/``test_over_length``/``test_index_reads``/``test_validate``
beside ``test_resolve``) named after the issue that added them, so "where are the tests for
``escalate.py``?" had no answer short of grepping all six.

The other resolver — records + prose, "which sample is each file" — is ``test_records.py``. The two
are siblings, and they part on disagreement (see ``resolve/records.py``); they do not share a file.
"""

from __future__ import annotations

import dataclasses
import json
import os
import random
from collections import Counter
from math import inf
from pathlib import Path
from typing import Any

import pytest

from conftest import KbProbes, real_cbs, registry_for, write_fastq_gz
from seqforge import __version__, kb
from seqforge import models as m
from seqforge.compose import core
from seqforge.io import DEFAULT_REGISTRY, OnlistRegistry, PackedOnlist
from seqforge.kb.generate import write_fastq_gz as write_reproducible_fastq_gz
from seqforge.kb.schema import HasSegment, MotifPresent, Read, Spec
from seqforge.manifest import (
    ExperimentInputs,
    exit_code_for_report,
    fill_manifest,
    validate_manifest,
)
from seqforge.manifest.validate import _CHEM_CONF_FLOOR_GEOMETRY, _CHEM_CONF_FLOOR_ONLIST
from seqforge.models.blocker import BlockerCode
from seqforge.models.dataset import INDEX_ROLE, SampleGroup
from seqforge.models.evidenced import EvidencedTaxid
from seqforge.models.observation import Observation
from seqforge.models.records import ArchiveRecord, RecordAttribute
from seqforge.models.resolve import MetadataResolution, ResolvedSample, TechScore
from seqforge.probe import probe_file
from seqforge.resolve import (
    Hypothesis,
    chemistry_hypothesis,
    exit_code_for,
    reduce_dataset,
    resolve_dataset,
    resolve_runs,
    role_of_sha_for,
)
from seqforge.resolve.assign import AssignmentResult, _brute, _hungarian_assign, best_assignment
from seqforge.resolve.confuse import accepts_at_rungs_0_2
from seqforge.resolve.engine import MultiRunOutput, index_tagged_roles
from seqforge.resolve.escalate import _pretrimmed_blockers, escalate
from seqforge.resolve.evaluators import Outcome, evaluate
from seqforge.resolve.geometry import (
    geometry_could_accept,
    length_feasible,
)
from seqforge.resolve.scoring import Cell, TechEvaluation, build_tech_evaluation
from seqforge.resolve.window import WindowProbe

# ================================================================================================
# resolve — assignment, the matrix, the benign-twin fixture, escalation
# ================================================================================================
#
# Tests for ``resolve``: assignment, matrix JSON-safety, the benign-twin fixture, and escalation
# branches.


def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    # Delegate to the REPRODUCIBLE writer (mtime=0, filename="") so synthetic reads are byte-stable
    # and content-addressed ids don't drift across runs — not the shared `conftest.write_fastq_gz`,
    # which is `gzip.open`-shaped and stamps the current mtime. Same `@SIM:i` record format.
    write_reproducible_fastq_gz(path, seqs)


# ---------- parallel per-run scoring (winner-invariance vs serial) ----------
def test_resolve_runs_parallel_matches_serial(tmp_path: Path) -> None:
    """``resolve_runs(cpus>1)`` forks per-run scoring with a copy-on-write-shared warm registry; the
    result must be byte-identical to the serial (``cpus=1``) resolution -- the same runs in the same
    order, each with the same winner and role assignment. Cores fold into no decision. Two distinct
    accessions -> two runs, which forces the fork path (it needs more than one run)."""
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=400, seed=1)
    reg = registry_for(
        spec, seed=1
    )  # whitelist matches the reads' seed so the barcodes actually hit
    paths: list[Path] = []
    for acc in ("SRR7000001", "SRR7000002"):
        for suffix, k in (("_1", "R1"), ("_2", "R2")):
            p = tmp_path / f"{acc}{suffix}.fastq.gz"
            _write_fastq_gz(p, reads[k])
            paths.append(p)

    def digest(multi: MultiRunOutput) -> list[object]:
        return [
            (
                r.run_id,
                r.output.result.candidates[0].technology,
                tuple(sorted(r.output.result.candidates[0].role_assignment.assignment.items())),
            )
            for r in multi.runs
        ]

    serial = resolve_runs(paths, registry=reg, use_cache=False, cpus=1)
    parallel = resolve_runs(paths, registry=reg, use_cache=False, cpus=2)
    assert len(serial.runs) == 2
    assert digest(serial) == digest(parallel)  # same decision, run for run


def _run_digest(multi: MultiRunOutput) -> list[object]:
    return [
        (
            r.run_id,
            r.output.result.candidates[0].technology,
            tuple(sorted(r.output.result.candidates[0].role_assignment.assignment.items())),
        )
        for r in multi.runs
    ]


def test_resolve_runs_resumes_from_cache_without_reprobing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#37: an unchanged re-run reads ZERO FASTQ bytes -- it rebuilds the answer from the
    content-addressed cache instead of probing. Proven by making the probe path fatal on the second
    call: if resume works, ``_probe_paths`` is never entered, and the decision is identical."""
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=800, seed=2)
    reg = registry_for(
        spec, seed=2
    )  # whitelist matches the reads' seed so the barcodes actually hit
    paths: list[Path] = []
    for acc in ("SRR8000001", "SRR8000002"):
        for suffix, k in (("_1", "R1"), ("_2", "R2")):
            p = tmp_path / f"{acc}{suffix}.fastq.gz"
            _write_fastq_gz(p, reads[k])
            paths.append(p)

    first = resolve_runs(paths, registry=reg, workspace=tmp_path, use_cache=True)

    import seqforge.resolve.engine as engine

    def _boom(*_a: object, **_k: object) -> dict[str, object]:
        raise AssertionError("resume failed: the probe path was entered on an unchanged re-run")

    monkeypatch.setattr(engine, "_probe_paths", _boom)
    second = resolve_runs(paths, registry=reg, workspace=tmp_path, use_cache=True)

    assert _run_digest(first) == _run_digest(second)  # identical decision, rebuilt from cache
    assert [o.file.sha256 for o in first.observations] == [
        o.file.sha256 for o in second.observations
    ]


def test_observation_cache_is_namespaced_by_probe_version(tmp_path: Path) -> None:
    """A probe-semantics bump (e.g. the N=2000 default) recomputes observations once — even for a
    provider-md5 address, which is N-invariant. The observation cache path folds in PROBE_VERSION, so a
    value cached under an older probe version is never re-served. The candidates/resume caches already
    fold PROBE_VERSION; this closes the same gap on the per-file observation cache.
    """
    from seqforge.probe import PROBE_VERSION, probe_file
    from seqforge.resolve.cache import Cache

    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=300, seed=5)
    fq = tmp_path / "R1.fastq.gz"
    _write_fastq_gz(fq, reads["R1"])
    obs = probe_file(fq)
    sha = obs.file.sha256

    cache = Cache(tmp_path)
    cache.write_observation(obs)

    on_disk = cache.root / "observations" / PROBE_VERSION / f"{sha}.json"
    assert on_disk.is_file()  # written under the LIVE probe version...
    assert cache.read_observation(sha) is not None  # ...and read back from there

    # A value cached under a DIFFERENT probe version must be invisible: no cross-version stale serve.
    stale = cache.root / "observations" / "1999.1.1" / f"{sha}.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(on_disk.read_text())
    on_disk.unlink()
    assert cache.read_observation(sha) is None


def test_resolve_dataset_scoring_threads_matches_serial(tmp_path: Path) -> None:
    """Per-spec scoring across a thread pool (sharing the read-only registry) is winner-invariant:
    same inputs, ``score_threads`` 1 vs N -> byte-identical ResolveResult AND evidence matrices.
    Threads fold into no decision, exactly as cores do for the per-run fork (#33). Each call gets a
    fresh (cold) registry, so the threaded run exercises the single-threaded pre-warm too."""
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=800, seed=3)
    f1 = tmp_path / "R1.fastq.gz"
    f2 = tmp_path / "R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])

    serial = resolve_dataset(
        [f1, f2], registry=registry_for(spec, seed=3), use_cache=False, score_threads=1
    )
    threaded = resolve_dataset(
        [f1, f2], registry=registry_for(spec, seed=3), use_cache=False, score_threads=6
    )

    assert serial.result.model_dump_json() == threaded.result.model_dump_json()
    assert serial.matrices == threaded.matrices
    assert threaded.result.candidates[0].technology == "10x-3p-gex-v3"  # threading changed nothing


def test_resolve_fingerprints_a_library_straight_from_a_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#39 end to end: fingerprint a library from a URL with NO local file, and resolve it to the same
    chemistry a local probe would. ``probe_remote`` range-reads a bounded head, the provider md5 is the
    content-address, and the ``(Observation, seqs)`` pair drops into ``resolve_dataset`` via ``_probed``.
    Proven over the real range->inflate->signals->resolve chain, offline: ``requests.get`` is faked to
    serve a 206 slice of genuine gzipped bytes (byte-stable, written by the generator)."""
    import hashlib
    import re
    import types

    import requests

    from seqforge.io import remote
    from seqforge.probe import content_key_from_md5

    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(
        spec, n=800, seed=3
    )  # same reads as the serial test above -> same winner
    reg = registry_for(
        spec, seed=3
    )  # whitelist matches the reads' seed so the barcodes actually hit

    blobs: dict[str, bytes] = {}
    md5s: dict[str, str] = {}
    urls: dict[str, str] = {}
    for suffix, role in (("_1", "R1"), ("_2", "R2")):
        p = tmp_path / f"SRR9100001{suffix}.fastq.gz"
        write_fastq_gz(p, reads[role])
        data = p.read_bytes()
        url = f"https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR910/001/SRR9100001/SRR9100001{suffix}.fastq.gz"
        blobs[url], md5s[url], urls[role] = data, hashlib.md5(data).hexdigest(), url

    def fake_get(
        url: str,
        headers: dict[str, str] | None = None,
        timeout: object = None,
        stream: object = None,
    ) -> object:
        data = blobs[url]
        match = re.search(r"bytes=0-(\d+)", (headers or {}).get("Range", ""))
        chunk = data[: int(match.group(1)) + 1] if match else data
        return types.SimpleNamespace(
            status_code=206,
            content=chunk,
            headers={"Content-Range": f"bytes 0-{len(chunk) - 1}/{len(data)}"},
            close=lambda: None,
        )

    monkeypatch.setattr(requests, "get", fake_get)  # the module `remote` calls through

    probed: dict[str, tuple[m.Observation, list[str]]] = {}
    for url in urls.values():
        obs, seqs = remote.probe_remote(url, md5=md5s[url])
        assert obs.file.sha256 == content_key_from_md5(md5s[url])  # provider md5 IS the address
        assert obs.file.local_uri is None  # nothing staged
        assert obs.probe.compressed_bytes_read <= len(blobs[url])  # bounded
        probed[url] = (obs, seqs)

    out = resolve_dataset([urls["R1"], urls["R2"]], registry=reg, use_cache=False, _probed=probed)
    assert out.result.candidates[0].technology == "10x-3p-gex-v3"  # a URL resolved to a library


# ---------- assignment ----------
def _exclusive_of(forbidden: list[list[bool]]) -> list[list[bool]]:
    """``exclusive[r][f]``, as ``best_assignment`` derives it: f eligible for r and no other role."""
    n_roles, n_files = len(forbidden), len(forbidden[0]) if forbidden else 0
    n_elig = [sum(not forbidden[r][f] for r in range(n_roles)) for f in range(n_files)]
    return [
        [(not forbidden[r][f]) and n_elig[f] == 1 for f in range(n_files)] for r in range(n_roles)
    ]


def test_hungarian_matches_brute_force() -> None:
    rng = random.Random(0)
    for _ in range(40):
        n = rng.randint(2, 5)
        score = [[rng.random() for _ in range(n)] for _ in range(n)]
        forbidden = [[rng.random() < 0.2 for _ in range(n)] for _ in range(n)]
        prior = [[0.0] * n for _ in range(n)]
        exclusive = _exclusive_of(forbidden)
        brute = _brute(n, n, score, forbidden, prior, exclusive)
        hung = _hungarian_assign(n, n, score, forbidden, prior, exclusive)
        if brute is None:
            assert hung is None
        else:
            assert hung is not None
            assert hung[1] == pytest.approx(brute[1])  # same optimal raw value


def test_a_single_role_eligible_file_claims_its_role_over_a_higher_scoring_rival() -> None:
    # Two roles, three files modelling GSE208154's per-role surplus: file0 (a barcode read) is eligible
    # for BOTH roles and out-scores everything for the cDNA role; file1 (a cDNA-length read) is eligible
    # ONLY for the cDNA role (forbidden for barcode); file2 is a second barcode read. Score alone would
    # take cDNA<-file0 and orphan file1 — coverage forces cDNA<-file1 (its sole possible home).
    score = [[0.90, 0.00, 0.88], [0.76, 0.26, 0.75]]  # role0=barcode, role1=cDNA
    forbidden = [[False, True, False], [False, False, False]]
    prior = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    res = best_assignment(2, 3, score, forbidden, prior)
    assert res.valid
    assert res.mapping[1] == 1  # cDNA claims the only cDNA-eligible file, not the barcode read
    assert res.mapping[0] in (0, 2)  # barcode takes a barcode read
    # The reported score is the honest Σ(score) of the chosen map — the coverage bonus is not folded in.
    assert res.raw == pytest.approx(score[0][res.mapping[0]] + 0.26)


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


# ---------- benign twins, end-to-end ----------
def test_resolve_10x_fixture_decides_v3(tmp_path: Path) -> None:
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=1500, seed=0)
    f1 = tmp_path / "sample_R1.fastq.gz"  # 28 bp barcode read
    f2 = tmp_path / "sample_R2.fastq.gz"  # ~cDNA
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])

    out = resolve_dataset(
        [f1, f2],
        registry=registry_for(spec),
        workspace=tmp_path,
        use_cache=True,
    )
    result = out.result
    assert out.exit_code() == 0
    assert not result.blockers and not result.questions
    winner = result.candidates[0]
    assert winner.technology == "10x-3p-gex-v3"
    assert winner.score.status == "scored"
    # benign twin recorded together, 0 questions
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
    spec = kb.load_spec("bulk-rnaseq")
    reads = kb.generate_reads(spec, n=1200, seed=0)
    f1 = tmp_path / "bulk_R1.fastq.gz"
    f2 = tmp_path / "bulk_R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])
    # A SYNTHETIC registry, not an absent one. No onlist is needed for the no-barcode branch, but
    # falling through to `DEFAULT_REGISTRY` ran `numpy.searchsorted` against the shipped 6 794 880
    # barcodes — 72% of the call in one profile, for a whitelist this test is not about. A synthetic
    # 10x list keeps it AVAILABLE and UNHIT, which is the same code path (`barcode_onlist_available`
    # stays True); an empty registry would switch the run to the abstention branch and quietly make
    # this a test of something else. Verified identical: same winner, same rung, same exit code, no
    # blocker, no conflict, no question.
    out = resolve_dataset(
        [f1, f2], registry=registry_for(kb.load_spec("10x-3p-gex-v3")), use_cache=False
    )
    assert out.exit_code() == 0
    assert out.result.candidates[0].technology == "bulk-rnaseq"
    assert out.result.rung_reached == 2  # geometry-only: no onlist involved
    winner = out.result.candidates[0]
    # The MAXIMAL set still wins a two-file deposit, and the score is the one it always had. Read sets
    # add an alternative, never a preference: the `se` set would seat one role and orphan the other
    # mate at `λ/|R|`, which is 0.25 against a one-role assignment — so it loses by a wide margin.
    assert winner.read_set == "full"
    assert sorted(winner.role_assignment.assignment) == ["R1", "R2"]
    assert winner.score.value == pytest.approx(1.01)


def test_a_single_end_bulk_deposit_resolves_and_records_the_se_read_set(tmp_path: Path) -> None:
    """The user-facing point of read sets: ONE bulk FASTQ decides, where it used to refuse.

    Before this, a single-end bulk RNA-seq deposit was `Blocker(UNSUPPORTED_TECHNOLOGY)` at exit 3 —
    not because a gate rejected it (bulk's `requires` is empty) but because the entry declared two
    reads against a role assignment that is injective AND total, so `n_files < n_roles` was invalid
    before any evidence was read. Single-end bulk RNA-seq is not exotic.

    Recognition is the unproven half of the feature — the round-trip is per READ, so a subset re-runs
    a strict subset of the same checks from the same seed and could not have caught a set that never
    gets selected. Hence both halves are asserted here: the spec is recognized, *and* the `se` set is
    the one recorded on the candidate.
    """
    spec = kb.load_spec("bulk-rnaseq")
    reads = kb.generate_reads(spec, n=1200, seed=0)
    only = tmp_path / "bulk_R1.fastq.gz"
    _write_fastq_gz(only, reads["R1"])

    out = resolve_dataset(
        [only], registry=registry_for(kb.load_spec("10x-3p-gex-v3")), use_cache=False
    )
    assert out.exit_code() == 0, out.result.blockers
    winner = out.result.candidates[0]
    assert winner.technology == "bulk-rnaseq"
    assert winner.read_set == "se", "the set that fits is the set the artifact must record"
    assert sorted(winner.role_assignment.assignment) == ["R1"]
    assert not winner.role_assignment.unassigned  # one file, one role, nothing orphaned


def test_resolve_splitseq_beats_generic_bulk_via_onlist(tmp_path: Path) -> None:
    # SPLiT-seq's specific evidence (3 round onlists + fixed linkers, rung 3) must dominate the
    # generic bulk fallback that merely fails to be forbidden (rung 2) — a Decision, not a question.
    spec = kb.load_spec("splitseq")
    reads = kb.generate_reads(spec, n=1200, seed=0)
    f_cdna = tmp_path / "sp_cdna.fastq.gz"
    f_bc = tmp_path / "sp_bc.fastq.gz"
    _write_fastq_gz(f_cdna, reads["cdna"])
    _write_fastq_gz(f_bc, reads["bc"])
    out = resolve_dataset([f_cdna, f_bc], registry=registry_for(spec), use_cache=False)
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
    out = resolve_dataset([f1, f2], registry=registry_for(spec, seed=1), use_cache=False)
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
        "signature": {"requires": [], "supports": [], "excludes": []},
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
        read_set="full",
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


# ---------- benign twins tie EXACTLY, so the representative must be deterministic ----------
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
    # ...and it is still benign: both recorded, zero questions
    assert not forward.questions and not reverse.questions
    assert forward.candidates[0].equivalence_members == ["techB"]


def test_the_real_kb_benign_twins_tie_and_ask_nothing(tmp_path: Path) -> None:
    """End-to-end on the SHIPPED specs: v3 and v3.1 are the benign rule's flagship, and now they
    exist.

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
    assert not out.result.questions, "a benign ambiguity asks NOTHING"
    assert out.exit_code() == 0


# ---------- multi-run: filenames GROUP, bytes ASSIGN ----------
def _six_run_dataset(tmp_path: Path) -> tuple[list[Path], OnlistRegistry]:
    """12 files shaped exactly like the pilot: 6 runs x (_1, _2), SRA-style names.

    `_1`/`_2` come from `fasterq-dump`'s dump order and say NOTHING about which read is the barcode.
    The generator writes the barcode read to `_1` here only because something must go first; every
    assertion below is about roles resolve derived from bytes.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    # Each run draws its CBs from build_pools(seed=i), so register the UNION of all six seeds' pools --
    # a single seed-0 registry matches only run 0, and F1b would then refuse the other five as
    # barcode-absent (their CBs miss the seed-0 whitelist).
    reg = OnlistRegistry(offline=True)
    merged: dict[str, list[str]] = {}
    for i in range(6):
        for alias, pool in kb.build_pools(spec, seed=i, pool_size=64).items():
            merged.setdefault(alias, []).extend(pool)
    for alias, ref in spec.onlists.items():
        if alias in merged:
            reg.register_synthetic(ref.registry, merged[alias])
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
    # Illumina's lane/chunk naming, and the `_R1_001` suffix that a naive end-anchor misses. The
    # lane goes with it -- a run spans its lanes (ADR-0027) -- but the `_S<n>` sample sheet entry
    # stays, and `test_the_lanes_of_one_library_are_one_run` holds that pair of rules.
    assert run_key("x_S1_L001_R1_001.fastq.gz") == "x_S1"
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


def test_the_lanes_of_one_library_are_one_run() -> None:
    """A run spans every lane it was loaded into (`docs/adr/0027`), so the lane token is stripped.

    Retaining it made a four-lane library four runs, and with no archive record a run IS the sample
    identity -- so four samples at a quarter depth each, at exit 0 (#263).
    """
    from seqforge.resolve import group_runs, run_key

    assert run_key("cell_42_S1_L001_R1_001.fastq.gz") == "cell_42_S1"
    assert run_key("cell_42_S1_L002_R1_001.fastq.gz") == "cell_42_S1"

    fused = group_runs(
        [
            f"cell_42_S1_L00{lane}_{read}_001.fastq.gz"
            for lane in (1, 2, 3, 4)
            for read in ("R1", "R2")
        ]
    )
    assert list(fused) == ["cell_42_S1"]
    assert len(fused["cell_42_S1"]) == 8


def test_the_sample_sheet_entry_is_never_stripped_with_the_lane() -> None:
    """`_S<n>` stays: it is the one token separating two libraries on one flowcell (ADR-0027).

    Stripping it would merge them, and a merge yields one plausible matrix that nobody notices --
    the failure direction a grouping rule may never take. A library resequenced under a second
    sample sheet is two runs of one sample here, and only a record may rejoin them.
    """
    from seqforge.resolve import group_runs, run_key

    assert run_key("cell_42_S1_L001_R1_001.fastq.gz") == "cell_42_S1"
    assert run_key("cell_42_S3_L001_R1_001.fastq.gz") == "cell_42_S3"

    split = group_runs(
        [
            "cell_42_S1_L001_R1_001.fastq.gz",
            "cell_42_S1_L002_R1_001.fastq.gz",
            "cell_42_S3_L001_R1_001.fastq.gz",
            "cell_42_S3_L002_R1_001.fastq.gz",
        ]
    )
    assert list(split) == ["cell_42_S1", "cell_42_S3"]


def test_a_lane_is_three_digits_because_bcl2fastq_pads() -> None:
    """Only a padded three-digit token is a lane, because `L<n>` is not a lane-only namespace.

    `XQTL_F4_N2PTM299_L2_1_S2_L004_R1_001.fastq.gz` -- 15 files on the benchmark tier -- spells the
    worm's larval stage `L2` in the same name it spells a lane `L004`. All 250 real lane tokens in
    the tier are three digits because bcl2fastq pads; a larval stage does not (ADR-0027). `_L\\d+`
    fuses the stages wherever the mate strip leaves one trailing, which is the second case here.
    """
    from seqforge.resolve import run_key

    assert run_key("XQTL_F4_N2PTM299_L2_1_S2_L004_R1_001.fastq.gz") == "XQTL_F4_N2PTM299_L2_1_S2"
    # a two-digit token is not a lane wherever it sits, trailing included
    assert run_key("worm_L2_R1_001.fastq.gz") == "worm_L2"
    assert run_key("worm_L0001_R1_001.fastq.gz") == "worm_L0001"


def test_a_name_that_is_only_a_lane_keeps_it() -> None:
    """The floor: a strip that would leave nothing keeps the name (ADR-0027).

    An empty run key is not a run -- every such file would collapse into one group.
    """
    from seqforge.resolve import group_runs, run_key

    assert run_key("L001_R1_001.fastq.gz") == "L001"
    assert run_key("L001.fastq.gz") == "L001"
    assert list(group_runs(["L001_R1_001.fastq.gz", "L002_R1_001.fastq.gz"])) == ["L001", "L002"]


def test_the_lanes_of_a_single_end_library_are_one_run_too() -> None:
    """No mate token to strip first, so the lane is trailing on the bare stem. It still comes off.

    A single-end library split four ways is the same quarter-depth failure as a paired one.
    """
    from seqforge.resolve import group_runs, run_key

    assert run_key("cell_42_S1_L001.fastq.gz") == "cell_42_S1"
    assert list(group_runs([f"cell_42_S1_L00{lane}.fastq.gz" for lane in (1, 2)])) == ["cell_42_S1"]


def test_the_lane_survives_as_data_from_the_same_token_the_run_key_dropped() -> None:
    """`lane_of` reads the lane the key stopped carrying, so one function owns what a lane IS.

    A second parse would be a second notion of a lane, free to disagree with the grouping. The
    accession branch is the case that forces the helper to stand on its own: GSE310378 puts lanes
    INSIDE one accession (`SRR36109512_..._S1_L005`), where `run_key` never reaches the lane token
    but the file still came from one (ADR-0027).
    """
    from seqforge.resolve import lane_of, run_key

    assert lane_of("cell_42_S1_L001_R1_001.fastq.gz") == "L001"
    assert lane_of("cell_42_S1_L002_R2_001.fastq.gz") == "L002"
    assert lane_of("cell_42_S1_L001.fastq.gz") == "L001"
    assert lane_of("SRR36109512_11314-RM-1_S1_L005_R1_001.fastq.gz") == "L005"

    # no lane in the name -- and the same notion of a lane as the strip, so a larval stage is none
    assert lane_of("reads.fastq.gz") == ""
    assert lane_of("SRR28716558_1.fastq.gz") == ""
    assert lane_of("worm_L2_R1_001.fastq.gz") == ""

    # the two functions never disagree: a reported lane is one the key does not carry
    for name in (
        "cell_42_S1_L001_R1_001.fastq.gz",
        "cell_42_S1_L001.fastq.gz",
        "worm_L2_R1_001.fastq.gz",
        "L001_R1_001.fastq.gz",
        "reads.fastq.gz",
    ):
        lane = lane_of(name)
        assert not lane or lane not in run_key(name), name


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


@pytest.fixture(scope="module")
def two_chemistry_multi(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Two runs, two chemistries resolved ONCE: SRR1 -> v3, SRR2 -> bulk. A real 2-assay project (skips
    all consumers if they happen to agree). The ``MultiRunOutput`` is an immutable resolve result, so
    the ~4 tests below read one build instead of each re-resolving the same dataset."""
    from seqforge.resolve import resolve_runs

    tmp = tmp_path_factory.mktemp("two_chemistry_multi")
    v3 = kb.load_spec("10x-3p-gex-v3")
    bulk = kb.load_spec("bulk-rnaseq")
    reg = registry_for(v3)
    paths: list[Path] = []
    for acc, spec, keys in (("SRR1", v3, ("R1", "R2")), ("SRR2", bulk, ("R1", "R2"))):
        reads = kb.generate_reads(spec, n=400, seed=0)
        for mate, role in zip(("1", "2"), keys, strict=True):
            p = tmp / f"{acc}_{mate}.fastq.gz"
            _write_fastq_gz(p, reads[role])
            paths.append(p)
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    if len({r.winner for r in multi.runs}) < 2:  # pragma: no cover
        pytest.skip("fixtures agreed; cannot exercise a 2-assay partition")
    return multi


def test_by_chemistry_partitions_the_runs_into_assays(two_chemistry_multi: Any) -> None:
    """Two runs of two chemistries is a legal multi-assay PROJECT, not a dataset-wide refusal: it
    partitions into one group per chemistry and ``resolve_runs`` itself never blocks (the old "all runs
    must agree" block moved to per-sample :meth:`sample_disagreements`)."""
    multi = two_chemistry_multi
    assert not multi.blockers  # a 2-assay project is not a refusal
    groups = multi.by_chemistry()
    assert set(groups) == {"10x-3p-gex-v3", "bulk-rnaseq"}
    assert [r.run_id for r in groups["10x-3p-gex-v3"]] == ["SRR1"]
    assert [r.run_id for r in groups["bulk-rnaseq"]] == ["SRR2"]
    # Every run lands in exactly one assay, and no run is lost.
    assert sum(len(v) for v in groups.values()) == len(multi.runs)


def test_role_of_sha_for_scopes_to_one_assays_runs(two_chemistry_multi: Any) -> None:
    multi = two_chemistry_multi
    groups = multi.by_chemistry()
    v3_map = role_of_sha_for(groups["10x-3p-gex-v3"])
    # The v3 assay's role map covers only SRR1's files, none of SRR2's.
    srr1_shas = {o.file.sha256 for o in groups["10x-3p-gex-v3"][0].output.observations}
    assert set(v3_map) <= srr1_shas
    assert set(v3_map) == srr1_shas  # both reads assigned, nothing dropped


def test_chemistry_of_sha_maps_each_file_to_its_runs_chemistry(two_chemistry_multi: Any) -> None:
    multi = two_chemistry_multi
    chem = multi.chemistry_of_sha()
    for run in multi.runs:
        for obs in run.output.observations:
            assert chem[obs.file.sha256] == run.winner


def test_a_sample_spanning_two_chemistries_blocks_but_two_samples_do_not(
    two_chemistry_multi: Any,
) -> None:
    multi = two_chemistry_multi
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


# ---------- the dataset-level reduction both front doors make (#196) ----------


def _metadata_over(**samples: list[str]) -> MetadataResolution:
    """A `MetadataResolution` carrying just the sample -> files map, which is all `reduce_dataset`
    reads. Built by hand rather than resolved: the join is `test_records.py`'s subject, and what is
    under test here is which gate a given map opens."""
    return MetadataResolution(
        samples=[
            ResolvedSample(sample_id=sid, file_shas=shas) for sid, shas in sorted(samples.items())
        ]
    )


def _shas_by_run(multi: MultiRunOutput) -> dict[str, list[str]]:
    return {r.run_id: [o.file.sha256 for o in r.output.observations] for r in multi.runs}


def test_reduce_dataset_lets_a_clean_multi_assay_project_through(two_chemistry_multi: Any) -> None:
    """Two samples of two chemistries pass all four gates: a partition is a verdict, not a refusal."""
    multi = two_chemistry_multi
    by_run = _shas_by_run(multi)
    resolution = reduce_dataset(multi, _metadata_over(s1=by_run["SRR1"], s2=by_run["SRR2"]))

    assert resolution.refused_at is None
    assert resolution.exit_code == 0
    assert resolution.blockers == []
    assert set(resolution.assays) == {"10x-3p-gex-v3", "bulk-rnaseq"}
    assert len(resolution.observations) == 4
    assert len(resolution.role_of_sha()) == 4, "every file of every run keeps its role"


def test_reduce_dataset_stops_at_the_sample_gate_on_a_mis_grouping(
    two_chemistry_multi: Any,
) -> None:
    """One sample owning both chemistries' files is the relocated "runs must agree" invariant."""
    multi = two_chemistry_multi
    by_run = _shas_by_run(multi)
    resolution = reduce_dataset(multi, _metadata_over(mixed=by_run["SRR1"] + by_run["SRR2"]))

    assert resolution.refused_at == "sample"
    assert resolution.exit_code == 3
    assert len(resolution.blockers) == 1 and "mixed" in resolution.blockers[0].message
    # The partition is still computed and reported — the refusal is about the JOIN, and a caller
    # rendering it should be able to say which two chemistries the sample was split across.
    assert set(resolution.assays) == {"10x-3p-gex-v3", "bulk-rnaseq"}


def test_reduce_dataset_stops_at_the_metadata_gate(two_chemistry_multi: Any) -> None:
    """A record whose runs do not match the files on disk refuses before any assay is named."""
    multi = two_chemistry_multi
    refused = MetadataResolution(
        blockers=[
            m.Blocker(
                id="blk-join",
                code=BlockerCode.RECORD_JOIN_INCOMPLETE,
                message="the record's runs are not these files",
                remedy="re-fetch",
                subject=m.BlockerSubject(kind="dataset", ref="d"),
            )
        ]
    )
    resolution = reduce_dataset(multi, refused)

    assert resolution.refused_at == "metadata"
    assert resolution.exit_code == 3
    assert [b.id for b in resolution.blockers] == ["blk-join"]
    # And it is READABLE off the one result. The metadata and sample gates refuse without any run
    # refusing, so a result carrying only the runs' blockers is empty on exactly the two gates this
    # reduction added — exit 3 with no code to name it by, which is a red a consumer cannot report.
    assert [b.id for b in resolution.result.blockers] == ["blk-join"]


def test_a_sample_gate_refusal_is_readable_off_the_one_result(two_chemistry_multi: Any) -> None:
    """The same for gate 3, whose blocker no run carries either — the byte resolver cannot see a
    sample, so a mis-grouping exists only at the dataset level."""
    multi = two_chemistry_multi
    by_run = _shas_by_run(multi)
    resolution = reduce_dataset(multi, _metadata_over(mixed=by_run["SRR1"] + by_run["SRR2"]))

    assert all(not r.output.result.blockers for r in multi.runs), "no run refused; the dataset did"
    assert [b.id for b in resolution.result.blockers] == [b.id for b in resolution.blockers]
    assert exit_code_for(resolution.result) == resolution.exit_code


def test_reduce_dataset_refuses_to_skip_the_per_sample_gate(two_chemistry_multi: Any) -> None:
    """No metadata is legal only where gate 1 already refused. Past it, it RAISES.

    An empty resolution would sail through the per-sample gate — no samples, no disagreements — and
    silently drop the invariant this reduction exists to apply, so a caller that forgot the join
    would get a clean verdict rather than an error.
    """
    with pytest.raises(ValueError, match="metadata resolution"):
        reduce_dataset(two_chemistry_multi)


def test_reduce_dataset_stops_at_the_run_gate_whatever_the_join_says(tmp_path: Path) -> None:
    """A run that did not resolve on its own bytes refuses first, and the record join cannot rescue
    it: the byte resolver blocks and the metadata resolver only warns (ADR-0010)."""
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=200, seed=0)
    lone = tmp_path / "SRR1_1.fastq.gz"  # one barcode read, no cDNA mate: nothing can fill R2
    _write_fastq_gz(lone, reads["R1"])
    multi = resolve_runs([lone], registry=registry_for(spec), use_cache=False)

    resolution = reduce_dataset(multi, _metadata_over(s1=_shas_by_run(multi)["SRR1"]))
    assert multi.exit_code() == 3
    assert resolution.refused_at == "run"
    assert resolution.exit_code == 3
    # The run's own blockers stay on the run — and reach the caller through `result`, not through
    # the dataset-level list, which is for what the DATASET decided.
    assert resolution.blockers == []
    assert resolution.result.blockers, "the refusal has to be readable off the one result"


def test_the_datasets_one_result_is_the_representative_run_of_the_first_assay(
    tmp_path: Path,
) -> None:
    """A homogeneous six-run dataset reduces to the run `manifest fill` would build its manifest
    from — and to the DATASET's role map, which one run's `RoleAssignment` cannot express."""
    paths, reg = _six_run_dataset(tmp_path)
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    resolution = reduce_dataset(multi, _metadata_over(**_shas_by_run(multi)))

    first = next(iter(resolution.assays.values()))[0]
    assert resolution.refused_at is None and resolution.exit_code == 0
    assert resolution.result.dataset_id == first.output.result.dataset_id
    assert resolution.result.candidates == first.output.result.candidates
    assert resolution.role_of_sha() == multi.role_of_sha()
    assert len(resolution.role_of_sha()) == 12, "the dataset-wide role map is still all 12"


def test_the_datasets_one_result_carries_every_runs_judgements_once(tmp_path: Path) -> None:
    """The one result unions what every run surfaced, deduplicated.

    Six runs of one library raise one library's question six times. A consumer that grades or
    reports ONE result must see it — run 0 alone would show the dataset exiting 4 with nothing open
    on it, a refusal it could not name — and must see it once, because six identical questions are
    one question. A judgement only ONE run raised must survive: that is the half run 0 loses.
    """
    paths, reg = _six_run_dataset(tmp_path)
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    shared = m.Question(
        id="q-chemistry",
        field="library.chemistry",
        prompt="which one?",
        options=["10x-3p-gex-v2", "10x-5p-gex-v2"],
        decidable_by=["user"],
        rung=7,
    )
    only_the_last = shared.model_copy(update={"id": "q-lane", "field": "library.lane"})

    def _asking(run: Any, questions: list[m.Question]) -> Any:
        result = run.output.result.model_copy(update={"questions": questions})
        return dataclasses.replace(run, output=dataclasses.replace(run.output, result=result))

    asked = [_asking(r, [shared]) for r in multi.runs[:-1]]
    asked.append(_asking(multi.runs[-1], [shared, only_the_last]))
    asking = MultiRunOutput(runs=asked)

    resolution = reduce_dataset(asking, _metadata_over(**_shas_by_run(asking)))
    assert resolution.exit_code == 4, "one run's open question is the dataset's"
    assert [q.id for q in resolution.result.questions] == ["q-chemistry", "q-lane"]


# ---------- the anchored measures, pinned by value ----------
# `VALUE_STABLE_DIGEST` (tests/test_probe.py) pins probe's structural fields, but probe never resolves a
# per-read frame -- so nothing pinned what `WindowProbe`'s ANCHORED measures return. The scoring tests
# above assert which spec wins, and a ratio can move materially without flipping a winner.
#
# The fixture is shaped like BD Rhapsody's Enhanced bead so both cases that make the anchored cutter's
# keep-guard observable are live: a read whose frame does not resolve at all, and an element that draws
# ZERO width (the diversity insert is 0-3 bp). Every expected value below is derivable from the
# construction -- read the arithmetic, don't re-pin the literal.

_VB_INSERT = ("", "A", "GT", "TCA")  # BD Enhanced's 0-3 bp diversity insert -> a per-read stagger
_CLS_POOL = 8  # cell labels per CLS block: small enough that recurrence is exact and countable
_N_STAGGERED = 200
_N_FRAMELESS = 40


def _acgt(rng: random.Random, k: int) -> str:
    return "".join(rng.choice("ACGT") for _ in range(k))


def _enhanced_bc_probe(tmp_path: Path) -> tuple[WindowProbe, Read, list[list[str]]]:
    """An Enhanced-bead R1 ``WindowProbe``, its declared ``Read``, and the CLS pools its reads drew from.

    240 reads. 200 carry the GTGA/GACA frame, of which **exactly 100 draw a 0 bp insert** -- so ``vb``
    is empty in half of them. The other 40 are ``ACAC...``, which no candidate phase can match (every
    4-mer is Hamming distance 3+ from both linkers, over a tolerance of 1), so they resolve to no frame
    and must contribute nothing rather than a wrong slice.
    """
    rng = random.Random(7)
    pools = [[_acgt(rng, 9) for _ in range(_CLS_POOL)] for _ in range(3)]
    seqs: list[str] = []
    for i in range(_N_STAGGERED):
        insert = "" if i % 2 == 0 else _VB_INSERT[1 + (i // 2) % 3]
        c1, c2, c3 = (rng.choice(p) for p in pools)
        seqs.append(insert + c1 + "GTGA" + c2 + "GACA" + c3 + _acgt(rng, 8) + "T" * 20)
    seqs += ["AC" * 45] * _N_FRAMELESS

    path = tmp_path / "enhanced_bc.fastq.gz"
    _write_fastq_gz(path, seqs)
    spec = kb.load_spec("bd-rhapsody-wta-enhanced-v1")
    bc = next(r for r in spec.reads if r.id == "bc")
    return WindowProbe(observation=probe_file(path), seqs=seqs), bc, pools


def test_the_anchored_distinct_ratio_counts_only_resolved_non_empty_elements(
    tmp_path: Path,
) -> None:
    """A frameless read contributes nothing; a zero-width element contributes nothing.

    Both are "this read tells us nothing here", and they are different causes: the frameless read has no
    window at all, while ``vb`` HAS a window that happens to be empty. The distinct ratio must drop both,
    and the two arithmetic below pin exactly that.
    """
    wp, bc, pools = _enhanced_bc_probe(tmp_path)

    # Each CLS block: 200 framed reads, each drawing from a pool of 8 -> 8 distinct over 200.
    for name, pool in zip(("cls1", "cls2", "cls3"), pools, strict=True):
        assert len({*pool}) == _CLS_POOL
        assert wp.anchored_distinct_ratio(bc, name) == pytest.approx(_CLS_POOL / _N_STAGGERED)

    # `vb`: 100 of the 200 framed reads draw a 0 bp insert. Dropping the empties leaves 3 distinct
    # inserts over 100 reads. Counting them would give 4 over 200 -- a different number, so this
    # assertion is what holds the empty-drop guard in place.
    assert wp.anchored_distinct_ratio(bc, "vb") == pytest.approx(3 / 100)
    assert wp.anchored_distinct_ratio(bc, "vb") != pytest.approx(4 / _N_STAGGERED)


def test_the_anchored_onlist_hit_tests_every_resolved_frame_and_only_those(tmp_path: Path) -> None:
    """The frame IS the offset: every framed read is tested, the 40 frameless ones are not."""
    wp, bc, pools = _enhanced_bc_probe(tmp_path)
    onlist = PackedOnlist.from_barcodes(pools[0])

    hit = wp.anchored_onlist_hit(bc, "cls1", onlist, orientation="forward")
    assert hit.n_tested == _N_STAGGERED  # not 240: a lost frame does not contribute
    assert hit.hit_rate == 1.0  # every cls1 slice came out of this very pool
    assert hit.offset == 0

    # A whitelist of the wrong width matches no frame at all -- the width check, not a silent 0-hit scan.
    wrong_width = PackedOnlist.from_barcodes([b + "A" for b in pools[0]])
    miss = wp.anchored_onlist_hit(bc, "cls1", wrong_width, orientation="forward")
    assert miss.n_tested == 0
    assert miss.hit_rate == 0.0


def test_the_anchored_onlist_hit_drops_a_frame_the_sequencer_never_called(tmp_path: Path) -> None:
    """An uncalled base inside a RESOLVED frame leaves the denominator, exactly as a lost frame does.

    The two twins must answer the same question the same way, and until #177 they did not: the
    fixed-offset path counted a non-ACGT window as a tested miss while this one counted it as tested
    too, so a dark cycle read as "these barcodes are not on the whitelist" on both. A frame the
    sequencer never called cannot hit any whitelist, so it says nothing about which whitelist the
    library came from — it is lost coverage, not a miss.
    """
    wp, bc, pools = _enhanced_bc_probe(tmp_path)
    onlist = PackedOnlist.from_barcodes(pools[0])
    clean = wp.anchored_onlist_hit(bc, "cls1", onlist, orientation="forward")

    # Blank one base of every OTHER framed read's cls1 slice, leaving the frame (and its anchors) intact.
    darkened = list(wp.seqs)
    frames = wp._frames(bc)
    blanked = 0
    for i, frame in enumerate(frames):
        if frame is None or i % 2:
            continue
        start, end = frame["cls1"]
        seq = darkened[i]
        darkened[i] = seq[:start] + "N" + seq[start + 1 : end] + seq[end:]
        blanked += 1
    assert blanked, "the fixture must have framed reads to darken, or this proves nothing"

    dark = WindowProbe(observation=wp.observation, seqs=darkened)
    hit = dark.anchored_onlist_hit(bc, "cls1", onlist, orientation="forward")

    assert hit.n_tested == clean.n_tested - blanked, "the blanked frames are coverage, not misses"
    assert hit.hit_rate == 1.0, "every frame that WAS called still came out of this very pool"


# ================================================================================================
# motif_present — an uncalled base is not a substitution
# ================================================================================================
#
# The same coverage policy the two onlist paths above already share, one test over. `motif_rate` was
# never brought along: an `N` failed the IUPAC membership check like any wrong base, so it ate the
# `max_mismatch` budget, and the read stayed in the denominator no matter how much of the searched
# span the sequencer never called. A dark cycle inside the window therefore read as "this library
# does not carry the motif" — the run measured instead of the library.
#
# The policy has two halves, and they are NOT the same:
#
# - An uncalled base costs nothing where the motif CONSTRAINS nothing. `GTGANNNNNNNNNGACA` asks about
#   8 of its 17 positions; a dark cycle under one of the nine `N`s was never evidence either way.
# - Where it does constrain, what the loss costs depends on what the search DECLARES. `read_start` /
#   `read_end` / a closed `window` name where the motif is, so their candidate positions are one claim
#   staggered, not independent chances — an uncalled base at any of their constrained offsets leaves
#   the read unable to answer, and it leaves `n_tested`. `anywhere`, and a `window` left open at the
#   end, declare nothing: each position is its own chance, and the read leaves `n_tested` only when
#   none of them survives.
#
# Only an UNCALLED base moves a read out of the denominator. A read the declared window does not fit
# is a length fact, already gated elsewhere, and stays a miss.

_DARK_CYCLE = 12  # under GTGA for most candidate phases: a cycle the Enhanced motif does constrain
_DONT_CARE_CYCLE = (
    18  # inside the CLS2 9-mer at EVERY candidate phase: a cycle it asks nothing about
)


def _enhanced_motif_gate() -> tuple[MotifPresent, Read, Spec]:
    """The shipped GTGA/GACA `requires` entry, its read and its spec — never a hand-built copy.

    A literal here would be a second spelling of a KB value and would keep passing after the shipped
    one moved. The cycles the tests darken are checked against `search_start`/`search_end` and the
    motif's own don't-care run, for the same reason.
    """
    spec = kb.load_spec("bd-rhapsody-wta-enhanced-v1")
    bc = next(r for r in spec.reads if r.id == "bc")
    return next(t for t in spec.signature.requires if isinstance(t, MotifPresent)), bc, spec


def _darken(seqs: list[str], cycle: int, which: range | None = None) -> list[str]:
    """Blank one cycle of the reads at ``which`` (all of them by default), lengths untouched."""
    hit = set(range(len(seqs)) if which is None else which)
    return [s[:cycle] + "N" + s[cycle + 1 :] if i in hit else s for i, s in enumerate(seqs)]


def _rate_of(wp: WindowProbe, gate: MotifPresent) -> float | None:
    """The shipped gate's own rate, its every parameter taken off the gate rather than restated."""
    return wp.motif_rate(
        gate.motif,
        where=gate.where,
        search_start=gate.search_start,
        search_end=gate.search_end,
        max_mismatch=gate.max_mismatch,
    )


def test_a_dark_cycle_across_the_motif_window_abstains_rather_than_failing_the_gate(
    tmp_path: Path,
) -> None:
    """A cycle the sequencer never called cannot refuse a spec: nothing measurable is left to fail on.

    The gate is a `requires` at a majority threshold, so before this the whole Enhanced family went
    invalid on a run artifact — every read carried the added mismatch, the budget of 2 was gone, the
    rate collapsed under `min_rate`, and a real BD Rhapsody dataset either fell to a lower candidate
    or refused. Only FAIL forbids a cell, so ABSTAIN is what "we could not look" has to be.
    """
    wp, _, _ = _enhanced_bc_probe(tmp_path)
    gate, bc, spec = _enhanced_motif_gate()
    assert gate.search_start is not None and gate.search_end is not None
    assert gate.search_start <= _DARK_CYCLE <= gate.search_end + len(gate.motif) - 1

    clean = evaluate(gate, bc, wp, spec, DEFAULT_REGISTRY)
    assert clean.outcome == Outcome.PASS, "the fixture must clear the shipped gate, or this is moot"

    dark = WindowProbe(observation=wp.observation, seqs=_darken(wp.seqs, _DARK_CYCLE))
    ev = evaluate(gate, bc, dark, spec, DEFAULT_REGISTRY)
    assert ev.outcome == Outcome.ABSTAIN, "a run artifact must never FAIL, and so forbid, the spec"


def test_the_motif_rate_is_measured_over_the_reads_that_were_actually_called(
    tmp_path: Path,
) -> None:
    """Darkening HALF the framed reads costs coverage, not rate — and the gate still passes.

    The arithmetic is the whole point and every term is derivable from the fixture: 200 of 240 reads
    carry the frame. Blank the searched span in every other framed read and 100 framed + 40 frameless
    stay callable, so the rate is 100/140 = 0.71 and clears the shipped 0.5. Counting the blanked
    reads as misses instead gives 100/240 = 0.42, which does not — a majority gate flipped by a run
    artifact. Both differ from the undarkened 200/240, so neither can pass by accident.
    """
    wp, _, _ = _enhanced_bc_probe(tmp_path)
    gate, bc, spec = _enhanced_motif_gate()

    blanked = range(0, _N_STAGGERED, 2)  # framed reads only: the frameless 40 stay readable misses
    dark = WindowProbe(observation=wp.observation, seqs=_darken(wp.seqs, _DARK_CYCLE, blanked))
    rate = _rate_of(dark, gate)
    framed_and_called = _N_STAGGERED // 2
    assert rate == pytest.approx(framed_and_called / (framed_and_called + _N_FRAMELESS))
    assert rate != pytest.approx(framed_and_called / (_N_STAGGERED + _N_FRAMELESS))
    assert evaluate(gate, bc, dark, spec, DEFAULT_REGISTRY).outcome == Outcome.PASS


def test_a_dark_cycle_the_motif_asks_nothing_about_costs_the_read_nothing(tmp_path: Path) -> None:
    """A cycle under a don't-care code is not evidence, so blanking it must not cost coverage either.

    The over-correction this guards against is the tempting one: mask the whole motif width, and a
    dark cycle in the Enhanced bead's 9 bp cell label — which `GTGANNNNNNNNNGACA` accepts any base at
    — throws away a read still showing GTGA and GACA intact. Coverage would collapse to nothing and
    the gate would abstain on a read it could have answered with. The rate is unmoved instead.
    """
    wp, _, _ = _enhanced_bc_probe(tmp_path)
    gate, bc, spec = _enhanced_motif_gate()
    assert gate.search_start is not None and gate.search_end is not None
    phases = range(gate.search_start, gate.search_end + 1)
    assert all(gate.motif[_DONT_CARE_CYCLE - p] == "N" for p in phases), "must be don't-care at ALL"

    dark = WindowProbe(observation=wp.observation, seqs=_darken(wp.seqs, _DONT_CARE_CYCLE))
    assert (
        _rate_of(dark, gate)
        == _rate_of(wp, gate)
        == pytest.approx(_N_STAGGERED / (_N_STAGGERED + _N_FRAMELESS))
    )
    assert evaluate(gate, bc, dark, spec, DEFAULT_REGISTRY).outcome == Outcome.PASS


_MOTIF = "GTGACGT"  # 7 bp, no ambiguity code: every position is a base the read must carry


def test_an_uncalled_base_is_evidence_for_nothing_under_an_unbounded_search(
    tmp_path: Path,
) -> None:
    """`anywhere`: an uncalled position is skipped, never scored — neither a match nor a mismatch.

    Three reads, each pinning one arm. The clean one matches. The dark one holds the motif at its one
    near-window with a single base blanked: scoring the `N` as a substitution admitted it within the
    tolerance, which reports a window nobody read as carrying the motif — so it is a non-match now,
    and it stays TESTED because its other positions were called. The all-dark read has no callable
    position at all and leaves the denominator, exactly as a read too short to hold the motif does.
    """
    pad = "AAAA"
    clean = pad + _MOTIF + pad
    dark = pad + _MOTIF[:5] + "N" + _MOTIF[6:] + pad
    unreadable = "N" * len(clean)
    wp = _probe_of(tmp_path, [clean, dark, unreadable], "motif_anywhere")

    # 1 match over the 2 reads that had a callable window; the all-N read is lost coverage. Scoring
    # the N as a substitution gives 2/3 instead — a rate the run moved, not the library.
    assert wp.motif_rate(_MOTIF, where="anywhere", max_mismatch=1) == pytest.approx(1 / 2)

    assert _probe_of(tmp_path, [unreadable], "motif_all_dark").motif_rate(_MOTIF) is None


def test_a_read_the_declared_window_does_not_fit_is_a_miss_not_lost_coverage(
    tmp_path: Path,
) -> None:
    """Length is not coverage. Only an UNCALLED base leaves the denominator.

    A read long enough to hold the motif but too short to reach the declared window offers no
    candidate position — and that is a fact about the read's layout, gated on length elsewhere,
    which must keep counting as "this is not that chemistry" rather than quietly vanishing into
    lost coverage and abstaining the gate away.
    """
    short = "ACGT" * 3  # 12 bp: holds the 7 bp motif, reaches no position in [8, 13]
    assert len(short) >= len(_MOTIF)
    wp = _probe_of(tmp_path, [short] * 4, "motif_window_unreachable")

    rate = wp.motif_rate(_MOTIF, where="window", search_start=8, search_end=13, max_mismatch=0)
    assert rate == 0.0, "a read that cannot reach the window is a miss, not lost coverage"


def test_a_window_left_open_at_the_end_declares_no_span_and_is_charged_per_position(
    tmp_path: Path,
) -> None:
    """An unclosed `window` runs to the end of the read, so it cannot be one claim staggered.

    Charging the whole read for any uncalled base past `search_start` would make a lone `N` in a
    150 bp tail cost the read entirely — the `anywhere` over-correction wearing a different `where`.
    A closed window is the opposite case and is charged whole, which is what the same read shows when
    the end is pinned just past the motif.
    """
    lo = 4
    seq = "AAAA" + _MOTIF + "AAAA" + "N" + "AAAA"  # the motif at `lo`, called; one dark cycle after
    dark_cycle = len(seq) - 5
    assert seq[dark_cycle] == "N" and dark_cycle > lo + len(_MOTIF)
    wp = _probe_of(tmp_path, [seq], "motif_open_window")

    assert wp.motif_rate(_MOTIF, where="window", search_start=lo) == 1.0
    closed = wp.motif_rate(_MOTIF, where="window", search_start=lo, search_end=dark_cycle)
    assert closed is None, "a closed span reaching the dark cycle is charged whole"


# ================================================================================================
# has_segment kind: constant — the SHARE OF READS carrying a fixed sequence
# ================================================================================================
#
# `constant` asks "is a fixed sequence here", and over a population of reads the honest form of that
# question is a PROPORTION: how many reads carry it. The gate used to average the per-cycle max-base
# fraction instead, which cannot tell "every read carries this linker" from "most do and the rest of
# the head is junk" — so a bar calibrated on the error-free reads `kb roundtrip` generates forbade
# real SPLiT-seq's barcode read and handed the dataset to the generic paired-end fallback, silently,
# at exit 0.
#
# What these tests exist to pin is that the replacement can still FAIL, because that is the whole
# risk in it: filter to the reads that agree with the consensus and then measure their agreement, and
# every window in every dataset scores ~1.0, noise included. The sweep below is the falsifiability.

_LINKER1 = (18, 48)  # SPLiT-seq's linker1: the 30 bp window this gate is measured on
_BC_LEN = 94  # SPLiT-seq read 2


def _linker1_seq() -> str:
    """linker1's sequence, read off the loaded spec — never a hand-built copy.

    A literal here would be a second spelling of a KB value, and the one that matters: base 8 of this
    linker is the base real reads and every published source disagree about. A copy would keep passing
    after the spec was corrected, testing the fixture against itself.
    """
    bc = next(r for r in kb.load_spec("splitseq").reads if r.id == "bc")
    seq = next(e.sequence for e in bc.elements if e.name == "linker1")
    assert seq is not None and len(seq) == _LINKER1[1] - _LINKER1[0]
    return seq


def _linker_reads(fraction: float, n: int = 600, seed: int = 11) -> list[str]:
    """``n`` 94 bp reads, of which ``fraction`` carry linker1 at its window; the rest are random.

    The carriers are *exact* — no injected error — so the fraction, and nothing else, is what the
    statistic under test has to recover.
    """
    rng = random.Random(seed)
    start, end = _LINKER1
    carriers = round(fraction * n)
    seqs = [
        _acgt(rng, start) + _linker1_seq() + _acgt(rng, _BC_LEN - end)
        if i < carriers
        else _acgt(rng, _BC_LEN)
        for i in range(n)
    ]
    rng.shuffle(seqs)  # so the statistic cannot depend on read order
    return seqs


def _probe_of(tmp_path: Path, seqs: list[str], name: str) -> WindowProbe:
    path = tmp_path / f"{name}.fastq.gz"
    _write_fastq_gz(path, seqs)
    return WindowProbe(observation=probe_file(path), seqs=seqs)


def _splitseq_linker1_gate() -> tuple[HasSegment, Read, Spec]:
    """The shipped `requires` entry for linker1, its read, and its spec — never a hand-built copy."""
    spec = kb.load_spec("splitseq")
    bc = next(r for r in spec.reads if r.id == "bc")
    test = next(
        t
        for t in spec.signature.requires
        if isinstance(t, HasSegment) and (t.start, t.end) == _LINKER1
    )
    assert test.kind == "constant"
    return test, bc, spec


@pytest.mark.parametrize("fraction", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_the_constant_statistic_is_the_share_of_reads_that_carry_the_sequence(
    tmp_path: Path, fraction: float
) -> None:
    """It measures the CONTAMINATED population, never a subset selected for agreeing with it.

    A conditional mean over the reads that already match the consensus would read ~1.0 at every one
    of these fractions, 0.0 included. That this tracks ``fraction`` — most of all that it is ~0 when
    no read carries the sequence — is what keeps the gate above it falsifiable.
    """
    wp = _probe_of(tmp_path, _linker_reads(fraction), f"carriers-{fraction}")
    rate = wp.consensus_match_rate(*_LINKER1, max_mismatch=3)
    assert rate == pytest.approx(fraction, abs=0.02)


def test_the_constant_gate_turns_over_at_a_majority_of_reads(tmp_path: Path) -> None:
    """The gate is a floor on that share: a majority carrying the sequence passes, a minority fails.

    The two ends are the ones that matter. 0% is pure noise and must fail — a gate that accepts noise
    is not a gate. 10% is the shape of a library where a handful of reads happen to carry a linker,
    and must also fail. Real SPLiT-seq sits at ~0.85/0.73 for its two linkers, well clear of the bar,
    which is why the fix is not a hair's-breadth loosening of the old one (it measured 0.905/0.827
    against 0.9 and turned on the second decimal).
    """
    test, bc, spec = _splitseq_linker1_gate()
    registry = registry_for(spec)
    passing = [
        pct
        for pct in (0, 10, 30, 45, 55, 61, 90, 100)
        if evaluate(
            test,
            bc,
            _probe_of(tmp_path, _linker_reads(pct / 100), f"turnover-{pct}"),
            spec,
            registry,
        ).outcome
        is Outcome.PASS
    ]
    assert passing == [55, 61, 90, 100]


def test_no_shipped_spec_asks_for_a_constant_window_that_never_closes() -> None:
    """The assumption the open-ended ABSTAIN rests on, made a mechanism instead of a comment.

    `_eval_has_segment` abstains on a `constant` gate with `end: null`, because a window running to
    whichever read happens to be longest has no fixed column to be constant over. ABSTAIN never
    gates — so if a spec ever declared such a window as a `requires`, it would silently stop being a
    requirement, which is the failure mode this whole issue was about. No spec does today; this is
    what keeps that true, and turns "we checked once" into "it cannot start being false".
    """
    from seqforge.kb.schema import HasSegment

    open_ended = [
        (spec_id, t.read, t.start)
        for spec_id in kb.list_spec_ids()
        for t in kb.load_spec(spec_id).signature.requires
        if isinstance(t, HasSegment) and t.kind == "constant" and t.end is None
    ]
    assert not open_ended, (
        f"a constant gate with no end abstains, so it is not a gate: {open_ended}. Give the element "
        "an explicit end, or make the claim with a test that can actually fail."
    )


@pytest.mark.xdist_group("kb-probes")
def test_the_constant_gate_fails_on_every_other_technologys_reads(kb_probes: KbProbes) -> None:
    """The second failure case the proportion has to survive: a window that is simply not there.

    SPLiT-seq's linker1 gate, run against every OTHER spec's own reads, must find nothing. This is
    the sweep that would go red if the statistic were computed over a self-selected subset, because
    then any 30 bp window of anything would look like a perfect linker.

    Every ``(spec, read set)`` key is swept, not just the maximal sets. A subset's probes are a strict
    subset of its own maximal set's, so it adds no new verdict — but the sweep is over "every file any
    configuration of any other chemistry produces", and writing it that way keeps it true if a future
    read set is ever built from its own reads rather than a narrowing.
    """
    test, bc, spec = _splitseq_linker1_gate()
    registry = registry_for(spec)
    verdicts = [
        (tech, evaluate(test, bc, wp, spec, registry).outcome)
        for (tech, _read_set), probes in kb_probes.items()
        if tech != "splitseq"
        for wp in probes
    ]
    assert not [t for t, o in verdicts if o is Outcome.PASS], (
        f"SPLiT-seq's linker1 was found in: {[t for t, o in verdicts if o is Outcome.PASS]}"
    )
    # FAIL, not merely "not PASS". ABSTAIN also satisfies "not PASS", and it means something else
    # entirely — no read reached the column, so nothing was measured. A sweep that accepted it would
    # pass just as happily if every probe were too short to see the window, which is the one way this
    # falsifiability check could quietly stop checking anything.
    reads_too_short = {t for t, o in verdicts if o is Outcome.ABSTAIN}
    assert any(o is Outcome.FAIL for _, o in verdicts), (
        "every spec abstained, so nothing was actually measured against the gate"
    )
    assert all(o is Outcome.FAIL for t, o in verdicts if t not in reads_too_short), (
        "a spec whose reads reach the window must FAIL the gate, not abstain"
    )


def test_resolve_splitseq_survives_a_head_that_is_two_fifths_junk(tmp_path: Path) -> None:
    """The regression that a synthetic round trip structurally cannot be, made hermetic.

    Real SPLiT-seq heads are ~39% off-structure — unligated product, primer dimer, whatever else got
    on the flowcell — and the linkers are essentially perfect in the rest. Generating reads from the
    spec and then replacing that share with junk reproduces the population the old gate could not
    read: the per-cycle mean drops to ~0.7 and forbids the `bc` role, while the barcodes still hit
    their whitelists at ~0.6 and rung 3 would have decided it outright.

    `test_resolve_splitseq_beats_generic_bulk_via_onlist` is the clean twin of this, and it stayed
    green throughout the defect — which is exactly why this one is here.
    """
    spec = kb.load_spec("splitseq")
    reads = kb.generate_reads(spec, n=1200, seed=0)
    rng = random.Random(3)
    junk = round(0.39 * len(reads["bc"]))
    bc = [_acgt(rng, _BC_LEN) for _ in range(junk)] + reads["bc"][junk:]
    rng.shuffle(bc)

    f_cdna, f_bc = tmp_path / "sp_cdna.fastq.gz", tmp_path / "sp_bc.fastq.gz"
    _write_fastq_gz(f_cdna, reads["cdna"])
    _write_fastq_gz(f_bc, bc)

    out = resolve_dataset([f_cdna, f_bc], registry=registry_for(spec), use_cache=False)
    assert out.result.candidates[0].technology == "splitseq", [
        c.technology for c in out.result.candidates[:3]
    ]
    assert out.result.candidates[0].rung_resolved == {"chemistry": 3}  # decided by the onlists
    assert out.exit_code() == 0
    assert not out.result.questions


#: A directory holding real GSE110823 reads (``SRR6750041_{1,2}.fastq.gz``). Real FASTQs stay out of
#: git — the committed guard against this defect is the hermetic junk-head test above plus
#: ``evals/benchmark/GSE110823``, which grades the same claim from a fingerprint package. This one is
#: the direct check, on the actual bytes the defect was found in, for whoever has them on disk.
_REAL_SPLITSEQ_DIR = os.environ.get("SEQFORGE_REAL_GSE110823")


@pytest.mark.skipif(
    not _REAL_SPLITSEQ_DIR,
    reason="set SEQFORGE_REAL_GSE110823=<dir with SRR6750041_{1,2}.fastq.gz> to run",
)
def test_real_splitseq_reads_resolve_to_splitseq() -> None:
    """The acceptance criterion of the fix, on the reads SPLiT-seq was published on."""
    d = Path(_REAL_SPLITSEQ_DIR or "")
    files = [d / "SRR6750041_1.fastq.gz", d / "SRR6750041_2.fastq.gz"]
    if not all(f.is_file() for f in files):
        pytest.skip(f"SRR6750041 mates not found under {d}")
    out = resolve_dataset(files, registry=DEFAULT_REGISTRY, use_cache=False)
    assert out.result.candidates[0].technology == "splitseq", [
        c.technology for c in out.result.candidates[:3]
    ]
    assert out.result.candidates[0].rung_resolved == {"chemistry": 3}
    assert out.exit_code() == 0
    assert not out.result.questions


# ================================================================================================
# geometry — the feasibility predicate (winner-invariance)
# ================================================================================================
#
# Tests for the geometry feasibility predicate — the winner-invariance foundation.
#
# The load-bearing property: ``length_feasible`` (and its pairwise wrapper ``geometry_could_accept``) is
# a *necessary condition* for a valid score, so narrowing on it can never drop a spec the full scorer
# would have accepted. We prove that over every shipped spec pair by asserting the implication
# ``accepts_at_rungs_0_2(a, probes[b]) => geometry_could_accept(a, probes[b])``.


# `test_fingerprint_is_deterministic` was deleted (#110): it asserted
# `geometry_fingerprint(spec) == geometry_fingerprint(spec)` -- a pure function called twice on the
# same object in the same process, which cannot fail. `geometry_fingerprint` is diagnostics-only and
# has no callers in `src/` (geometry.py names `length_feasible` as the correctness predicate); the
# feasibility predicate that DOES gate scoring is covered by the three feasibility tests below.


@pytest.mark.xdist_group("kb-probes")
def test_a_spec_is_length_feasible_against_its_own_reads(kb_probes: KbProbes) -> None:
    for tech_id in kb.list_spec_ids():
        spec = kb.load_spec(tech_id)
        assert length_feasible(spec, kb_probes[tech_id, "full"]), (
            f"{tech_id} must accept its own synthetic reads"
        )


def test_length_feasibility_is_true_when_only_an_alternative_read_set_fits(
    tmp_path: Path,
) -> None:
    """Feasibility is **any-set**, and that is a correctness fix rather than a nicety.

    `length_feasible` documents itself as a *proven* necessary condition for a valid score — a spec it
    rejects is one `build_tech_evaluation` would also reject, which is what lets descent narrow the
    scored pool without moving the winner. It computed the role count from the maximal set, so a spec
    whose alternative set fits was dropped from the pool while full scoring would have made it a
    winner. The engine's `pool = [...] or runnable` fallback would have HIDDEN that: it hands back the
    whole pool only when the narrowed one comes up empty, so on any dataset with one other feasible
    spec the falsification is silent. A latent break behind a fallback is worse than a loud one.

    One 60 bp cDNA read: bulk's maximal set needs two files and cannot be seated, its `se` set can.
    """
    spec = kb.load_spec("bulk-rnaseq")
    reads = kb.generate_reads(spec, n=400, seed=0)
    only = tmp_path / "one_mate.fastq.gz"
    _write_fastq_gz(only, reads["R1"])
    wps = [WindowProbe(observation=probe_file(only), seqs=reads["R1"][:200])]

    assert length_feasible(spec, wps), "the `se` set fits, so the spec is feasible"
    assert not _reads_are_assignable(spec.reads_in("full"), wps), "...and the maximal set does not"
    # The necessary-condition contract, on the very dataset that used to falsify it.
    assert build_tech_evaluation(spec, wps, DEFAULT_REGISTRY).valid


def _reads_are_assignable(reads: list[Read], wps: list[WindowProbe]) -> bool:
    """Is this exact read list one-to-one seatable on ``wps``? The per-set half of feasibility.

    Spelled out here rather than imported so the test states the OLD (maximal-only) question in its
    own words: importing the private per-set helper would let a refactor that broke the distinction
    keep this assertion green.
    """
    from seqforge.resolve.assign import best_assignment
    from seqforge.resolve.evaluators import read_length_compatible

    n_roles, n_files = len(reads), len(wps)
    if n_files < n_roles:
        return False
    forbidden = [[read_length_compatible(r, wp) == Outcome.FAIL for wp in wps] for r in reads]
    zeros = [[0.0] * n_files for _ in range(n_roles)]
    return best_assignment(n_roles, n_files, zeros, forbidden, zeros).valid


@pytest.mark.xdist_group("kb-probes")
def test_geometry_could_accept_is_necessary_for_rung02_acceptance(kb_probes: KbProbes) -> None:
    """The guarantee the confusability guard and the runtime shortlist rely on.

    If ``a`` accepts ``b``'s reads at rungs 0-2 (a real confusable), then ``a`` must be geometry-feasible
    against ``b``'s reads — so skipping geometry-infeasible pairs can never miss a real confusable. The
    founding cross-geometry collision (``bulk-rnaseq`` accepts ``splitseq``) must therefore still be
    seen by ``geometry_could_accept``.

    #112 asked whether the ``geometry_could_accept`` pre-gate the confusability guard uses could bound
    this O(n²) sweep too. It cannot, and the reason is CIRCULARITY: this test's subject IS
    ``geometry_could_accept`` (it proves ``accepts_at_rungs_0_2 => geometry_could_accept``). Pre-gating
    the ``accepts`` call on ``geometry_could_accept`` would only ever examine geometry-YES pairs, where
    the implication is vacuously true, and would stop covering the geometry-NO pairs the guarantee is
    about. So it stays ungated: n²·0.70ms, ~8.9s at 100 specs — survivable, and a circular gate is not.
    """
    ids = kb.list_spec_ids()
    specs = {i: kb.load_spec(i) for i in ids}

    for a in ids:
        for b in ids:
            if accepts_at_rungs_0_2(specs[a], kb_probes[b, "full"]):
                assert geometry_could_accept(specs[a], kb_probes[b, "full"]), (
                    f"{a!r} accepts {b!r}'s reads at rungs 0-2 but geometry_could_accept says no — "
                    "the necessary-condition guarantee is broken and the guard/shortlist would be unsound"
                )


@pytest.mark.xdist_group("kb-probes")
def test_descent_narrowing_never_drops_a_valid_spec(kb_probes: KbProbes) -> None:
    """WINNER-INVARIANCE: the descent pool (length-feasible specs) never excludes a spec that would
    score VALID with the full registry (rung 3 included) — so scoring the pool yields the identical
    winner as scoring the whole runnable KB. This is the property the whole "narrow, don't change the
    answer" design rests on, checked over every real leaf dataset against every runnable spec.

    #112 asked for the ``geometry_could_accept`` pre-gate here. It is already present, as the
    ``if length_feasible(spec, wps): continue`` below — ``geometry_could_accept`` IS ``length_feasible``
    over WindowProbes (geometry.py). But note the DIRECTION: the confusability guard skips the expensive
    scorer on geometry-NO pairs; this test scores exactly those pairs, because proving "an excluded spec
    never scores VALID" is its whole subject. So a ``geometry_could_accept`` SKIP-gate would make it
    vacuous. The scorer runs only on the length-infeasible minority, which is as narrow as the guarantee
    allows. Growth is bounded by that, not by n² (0.25s at 12 specs after #105's shared kb_probes).
    """
    specs = kb.load_all_specs()
    runnable = [s for s in specs.values() if s.backend is not None]
    for tech in kb.runnable_spec_ids():
        wps = kb_probes[tech, "full"]
        for spec in runnable:
            if length_feasible(spec, wps):  # == geometry_could_accept; the pre-gate, already here
                continue
            ev = build_tech_evaluation(spec, wps, DEFAULT_REGISTRY)
            assert not ev.valid, (
                f"length_feasible dropped {spec.identity.id!r} on {tech!r}'s reads, yet it scores "
                "VALID — narrowing would change the winner, breaking winner-invariance"
            )


# ================================================================================================
# negatives — refusal, not a guess
# ================================================================================================
#
# The day-one negatives: refusal (not a guess) is the correct answer.
#
# 1. truncated/corrupt gzip -> ``Blocker(TRUNCATED_GZIP)`` (exit 3)
# 2. an ONT run (technology absent from the KB) -> ``Blocker(UNSUPPORTED_TECHNOLOGY)`` (exit 3), never
#    a silent guess
# 3. metadata says v2 but the reads say v3 -> a surfaced ``Conflict`` (26 bp asserted vs 28 bp
#    observed) (exit 4), never a silent pick
# 4. a pre-trimmed technical read -> ``Blocker(PRETRIMMED_VARIABLE_LENGTH)`` (exit 3). The quiet one:
#    1-3 are all loud, and this one scores like a clean dataset.


def test_truncated_gzip_blocks(tmp_path: Path) -> None:
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=3000, seed=0)
    f1 = tmp_path / "sample_R1.fastq.gz"
    f2 = tmp_path / "sample_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])
    # cut R1's gzip mid-stream: valid records then an abrupt end -> truncated (not merely corrupt)
    data = f1.read_bytes()
    f1.write_bytes(data[: int(len(data) * 0.6)])

    out = resolve_dataset([f1, f2], registry=registry_for(spec), use_cache=False)
    assert out.exit_code() == 3
    assert not out.result.candidates
    codes = {b.code for b in out.result.blockers}
    assert BlockerCode.TRUNCATED_GZIP in codes
    blk = next(b for b in out.result.blockers if b.code == BlockerCode.TRUNCATED_GZIP)
    assert blk.remedy  # actionable, non-empty


#: Bases a 3' trimmer took off a 28 bp barcode read: 27, 26, 25, 24 bp under an intact peak. Spread
#: over four lengths rather than one so the trimmed reads can outnumber the intact ones without any
#: single short length becoming the mode.
_TRIM_BY = (1, 2, 3, 4)


def _partly_trimmed_v3(tmp_path: Path, at_mode: int, n: int, seed: int = 0) -> tuple[Path, Path]:
    """A v3 pair whose barcode read keeps ``at_mode`` of ``n`` reads at the declared 28 bp.

    The rest are shortened round-robin from the 3' end, which is the shape a trimmer leaves: a spread
    of shorter lengths under a peak that is still where the chemistry says it is. The peak has to stay
    the single most common length, because the **mode** is what seats a file in the barcode role at
    all (`read_length_compatible`) — a trimmer that moved the mode itself is a different failure,
    forbidden by the geometry gates before escalation ever runs. Every read keeps its full 16 bp CB,
    so the whitelist still hits and the chemistry still wins: length, and only length, is what these
    fixtures vary.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=n, seed=seed)
    trimmed = [
        s if i < at_mode else s[: -_TRIM_BY[i % len(_TRIM_BY)]] for i, s in enumerate(reads["R1"])
    ]
    random.Random(seed).shuffle(trimmed)  # so no statistic can depend on read order
    f1 = tmp_path / f"trimmed-{at_mode}-of-{n}_R1.fastq.gz"
    f2 = tmp_path / f"trimmed-{at_mode}-of-{n}_R2.fastq.gz"
    write_fastq_gz(f1, trimmed)
    write_fastq_gz(f2, reads["R2"])  # cDNA, untouched: variable by design
    return f1, f2


def _seated_v3(observations: list[Observation]) -> TechEvaluation:
    """A winning v3 evaluation seating R1 and R2 on the two observations, in order.

    `_pretrimmed_blockers` reads exactly one thing off an evaluation — which file the winner seated in
    each role — so everything else here is scenery. Driving the gate directly is what lets the
    turnover below be pinned at the single read that crosses it, without paying for a full scoring
    pass per point.
    """
    shas = [o.file.sha256 for o in observations]
    return TechEvaluation(
        tech="10x-3p-gex-v3",
        read_set="full",
        roles=["R1", "R2"],
        file_shas=shas,
        matrix={},
        assignment=AssignmentResult(valid=True, mapping={0: 0, 1: 1}, unassigned_files=[], raw=1.0),
        score=TechScore(technology="10x-3p-gex-v3", status="scored", value=1.0),
        rung=3,
        used_onlist=True,
        equivalence_members=[],
        barcode_role_ids=["R1"],
        unfillable_role_ids=[],
        cdna_role_fillable=True,
        barcode_onlist_hit=True,
        barcode_onlist_available=True,
    )


def _pretrimmed_gate(f1: Path, f2: Path) -> list[m.Blocker]:
    """The gate alone, over an already-seated v3 assignment: the blockers it emits, if any."""
    observations = [probe_file(f1), probe_file(f2)]
    return _pretrimmed_blockers(
        _seated_v3(observations), kb.load_spec("10x-3p-gex-v3"), observations
    )


def test_a_pretrimmed_technical_read_blocks(tmp_path: Path) -> None:
    """The quiet negative: it scores like a clean dataset, so nothing else catches it.

    `read_length_compatible` gates on the read-length **mode**, so a barcode read whose modal length
    is still 28 bp passes every geometry check and wins its candidate outright, however few of its
    reads are actually there. Downstream never looks again: STARsolo reads the barcode from a fixed
    offset, and on a shifted read that offset is an arbitrary 16-mer — it matches no whitelist, the
    cell is dropped, the matrix is thin, and STAR exits 0. That is the silent-garbage path this
    blocker was written to close, and `PRETRIMMED_VARIABLE_LENGTH` sat declared-but-never-emitted
    while it stayed open.

    A *majority* of the reads are off-length here, which is what the gate now asks about: the declared
    length is no longer where this library sits. Note only R1 is trimmed. R2 is cDNA — open-ended and
    *legitimately* variable — which is exactly why this cannot be "variable length is bad": it has to
    be variable length on a read the chemistry declares fixed.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    f1, f2 = _partly_trimmed_v3(tmp_path, at_mode=800, n=2000)

    out = resolve_dataset([f1, f2], registry=registry_for(spec), use_cache=False)

    assert out.exit_code() == 3
    assert not out.result.candidates  # refused: no manifest may be filled over this
    blk = next(b for b in out.result.blockers if b.code == BlockerCode.PRETRIMMED_VARIABLE_LENGTH)
    assert blk.subject.ref == f1.name  # names the trimmed file, not the clean cDNA
    assert "sra-pub-src" in blk.remedy  # actionable: where the untrimmed original lives
    # The message states the share it refused on, not merely that the lengths differ: a reader who
    # disagrees with the refusal has to be able to see the number that produced it.
    assert "40" in blk.message and "28 bp" in blk.message, blk.message


def test_a_single_read_a_base_short_in_a_two_thousand_read_head_does_not_block(
    tmp_path: Path,
) -> None:
    """The case the old gate could not survive (#190): `n_distinct == 1` made one stray read fatal.

    One read of 2 000 trimmed by a single base is 0.05% of the head — a ragged record, not a trimmer
    that moved a library's offsets — and it refused the whole dataset at exit 3, with no appeal and no
    escape short of re-fetching a file that was never wrong. The Observation could always tell the two
    apart; the gate just never asked it.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    f1, f2 = _partly_trimmed_v3(tmp_path, at_mode=1999, n=2000)

    out = resolve_dataset([f1, f2], registry=registry_for(spec), use_cache=False)

    assert not any(b.code == BlockerCode.PRETRIMMED_VARIABLE_LENGTH for b in out.result.blockers), [
        b.message for b in out.result.blockers
    ]
    assert out.result.candidates[0].technology in {"10x-3p-gex-v3", "10x-3p-gex-v3.1"}
    assert out.exit_code() == 0


def test_the_pretrimmed_gate_turns_over_at_a_majority_of_reads_at_the_declared_length(
    tmp_path: Path,
) -> None:
    """The floor is a majority — the bar `has_segment kind: constant` already settled on (2026.7.15).

    Both sides of it, at the single read that crosses: 300 of 600 reads at the declared length is a
    library that still sits there and passes, 299 is one that does not and refuses. The far ends are
    the ones that matter — a quarter of the reads at the declared length is a genuine offset shift and
    must refuse, and an untouched file must never refuse — but a boundary asserted only at the ends is
    a boundary asserted nowhere.

    25% is as low as this fixture can go while still *reaching* the gate: below ~20% a shortened
    length becomes the mode, the file stops matching the declared geometry, and the refusal that
    follows is `read_length_compatible`'s, not this one's.
    """
    blocked = [
        at_mode
        for at_mode in (150, 270, 299, 300, 330, 600)
        if _pretrimmed_gate(*_partly_trimmed_v3(tmp_path, at_mode=at_mode, n=600))
    ]
    assert blocked == [150, 270, 299]


def test_the_over_length_escape_survives_a_tail_too_ragged_for_the_share_floor(
    tmp_path: Path,
) -> None:
    """The escape below the gate is load-bearing, not a duplicate of the share floor.

    An over-sequenced barcode read varies in its junk tail, and that tail can be ragged enough to put
    the share at the modal length *under* the floor while CB and UMI sit untouched at their fixed
    offsets. Only 40% of these reads are 150 bp — the share floor alone would refuse them — and the
    over-length escape is the whole reason they are not refused. A test whose raggedness cleared the
    floor anyway would pass with the escape deleted.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    cb_pool = kb.build_pools(spec, seed=0)["cb_whitelist"]
    rng = random.Random(3)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    tails = (122, 112, 102, 92, 82)  # 150 bp mode, then four shorter junk tails
    barcode = [
        rng.choice(cb_pool) + rand(12) + rand(tails[0] if i < 240 else tails[1 + i % 4])
        for i in range(600)
    ]
    r1 = tmp_path / "ragged_over_length_R1.fastq.gz"
    r2 = tmp_path / "ragged_over_length_R2.fastq.gz"
    write_fastq_gz(r1, barcode)
    write_fastq_gz(r2, [rand(OVER_LEN) for _ in range(600)])

    obs = probe_file(r1)
    assert obs.read_length.mode == OVER_LEN  # over-length, and the peak is a minority of the head
    assert obs.read_length.mode_share == pytest.approx(0.4)

    assert not _pretrimmed_gate(r1, r2)


def test_an_untrimmed_dataset_does_not_trip_the_pretrimmed_blocker(tmp_path: Path) -> None:
    """The other half: cDNA is variable by design, and must not be mistaken for a trimmer's work.

    A guard that fired on every 10x dataset would be deleted within a day.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=3000, seed=0)
    f1 = tmp_path / "sample_R1.fastq.gz"
    f2 = tmp_path / "sample_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])  # open-ended cDNA: genuinely many distinct lengths

    out = resolve_dataset([f1, f2], registry=registry_for(spec), use_cache=False)

    codes = {b.code for b in out.result.blockers}
    assert BlockerCode.PRETRIMMED_VARIABLE_LENGTH not in codes
    assert out.result.candidates[0].technology == "10x-3p-gex-v3"


def test_ont_unsupported_technology_is_refused_not_guessed(tmp_path: Path) -> None:
    # A single long-read ONT file: no KB technology's read set can be satisfied -> refuse, don't guess.
    #
    # `offline=True` here, and this is the ONE place in this file where an empty registry is the
    # honest choice rather than a shortcut: no spec's read set can be satisfied at all, so no onlist
    # can participate in the answer. A synthetic list would be scenery. 60 reads of 500-1200 bp make
    # the same point as 200 of up to 3000 -- the refusal is about read-set satisfiability, not depth.
    rng = random.Random(0)
    long_reads = [
        "".join(rng.choice("ACGT") for _ in range(rng.randint(500, 1200))) for _ in range(60)
    ]
    f = tmp_path / "ont_run.fastq.gz"
    write_fastq_gz(f, long_reads)

    out = resolve_dataset([f], registry=OnlistRegistry(offline=True), use_cache=False)
    assert out.exit_code() == 3
    assert not out.result.candidates
    codes = {b.code for b in out.result.blockers}
    assert codes == {BlockerCode.UNSUPPORTED_TECHNOLOGY}


def _chem_assertion(
    value: str,
    *,
    field: str = "library.chemistry",
    span_verified: bool = True,
    entailment_ok: bool = True,
) -> m.Assertion:
    """An assertion carrying ``value`` on ``field``, verified unless a flag is turned off."""
    return m.Assertion(
        id=f"a-{field}-{value}",
        field=field,
        value=value,
        span=m.SourceSpan(doc_sha256="0" * 64, quote=value, char_start=0, char_end=len(value)),
        span_verified=span_verified,
        entailment_ok=entailment_ok,
        llm_confidence=0.9,
        extractor=m.ExtractorProvenance(model_id="test/fixture", prompt_version="v1"),
    )


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        pytest.param([], None, id="no-claim"),
        pytest.param(["10x-3p-gex-v3"], "10x-3p-gex-v3", id="one-claim"),
        pytest.param(["10x 5'", "10x 5'"], "10x 5'", id="two-documents-one-answer"),
        pytest.param(["10x-3p-gex-v2", "10x-3p-gex-v3"], None, id="two-answers-steers-nothing"),
        pytest.param(["bulk-rnaseq", "10x-3p-gex-v3", "bulk-rnaseq"], None, id="majority"),
    ],
)
def test_chemistry_hypothesis_is_agreement_or_nothing(
    values: list[str], expected: str | None
) -> None:
    """The ONE reduction from verified prose to a steering hypothesis: unanimity, or ``None``.

    Two callers reduce the same list — ``manifest fill`` (the compiler) and ``evals/run.py`` (the
    harness that measures it) — and they used to disagree: the harness took a last-wins
    ``by_field`` dict, so a dataset whose prose named two chemistries steered the scorer with
    whichever document happened to be read last, while the compiler over the identical prose
    steered with nothing. A harness that fails differently from production measures the harness.

    A MAJORITY is not agreement either. Two experiments describing two protocols is a real dataset,
    and one dataset-level hypothesis would steer both — half of them wrongly. Dropping it costs only
    a hint: the bytes still decide.
    """
    got = chemistry_hypothesis([_chem_assertion(v) for v in values])
    if expected is None:
        assert got is None
    else:
        assert got is not None
        assert (got.value, got.id, got.confidence) == (expected, "harvest", 0.9)


def test_chemistry_hypothesis_reads_only_the_chemistry_field() -> None:
    """An organism claim is not a chemistry claim, and must neither supply nor spoil the hypothesis."""
    claims = [
        _chem_assertion("Caenorhabditis elegans", field="experiment.organism"),
        _chem_assertion("10x-3p-gex-v3"),
        _chem_assertion("nuclei", field="library.prep_type"),
    ]
    got = chemistry_hypothesis(claims)
    assert got is not None and got.value == "10x-3p-gex-v3"
    assert chemistry_hypothesis([c for c in claims if c.field != "library.chemistry"]) is None


@pytest.mark.parametrize(
    ("span_verified", "entailment_ok"),
    [
        pytest.param(False, True, id="quote-does-not-grep-back"),
        pytest.param(True, False, id="quote-does-not-entail-the-value"),
        pytest.param(False, False, id="neither"),
    ],
)
def test_an_unverified_claim_steers_nothing(span_verified: bool, entailment_ok: bool) -> None:
    """R2's floor, enforced where the claim is *used* and not only where it was composed.

    `verify_drafts` sets both flags itself, so a harvest run cannot reach here unverified. The open
    door is `manifest fill --assertions <file>`: that file is parsed straight into `Assertion`s with
    no flag check, so a hand-written one could steer the scorer with a quote that greps back nowhere.
    Both flags are code-owned and fail closed; a claim missing either is not a claim.

    It must also not *spoil* a good one by counting as a second, disagreeing answer — an ignored
    claim is ignored, not a veto.
    """
    bad = _chem_assertion("bulk-rnaseq", span_verified=span_verified, entailment_ok=entailment_ok)
    assert chemistry_hypothesis([bad]) is None
    got = chemistry_hypothesis([_chem_assertion("10x-3p-gex-v3"), bad])
    assert got is not None and got.value == "10x-3p-gex-v3"


def _experiment_record(library_source: str | None) -> ArchiveRecord:
    """One experiment record, carrying ``library_source`` only when the deposit declared one.

    ``harmonized=False`` is not an oversight: ``io/archive.py`` files the library descriptor
    unharmonized (the harmonized namespace is NCBI's curated *sample* attribute list), so a rule that
    read it through ``ArchiveRecord.attribute`` would silently see nothing on every real record.
    """
    return ArchiveRecord(
        level="experiment",
        accession="SRX000001",
        attributes=(
            []
            if library_source is None
            else [RecordAttribute(name="library_source", value=library_source)]
        ),
    )


@pytest.mark.parametrize(
    ("library_source", "chemistry", "expected"),
    [
        pytest.param(
            "TRANSCRIPTOMIC SINGLE CELL", "bulk-rnaseq", None, id="single-cell-drops-bulk"
        ),
        pytest.param(
            "transcriptomic single-cell", "bulk RNA-seq", None, id="case-and-hyphen-tolerant"
        ),
        pytest.param(
            "TRANSCRIPTOMIC SINGLE CELL",
            "10x-3p-gex-v3",
            "10x-3p-gex-v3",
            id="single-cell-hint-untouched",
        ),
        pytest.param(
            "TRANSCRIPTOMIC",
            "bulk-rnaseq",
            "bulk-rnaseq",
            id="bare-transcriptomic-says-nothing",
        ),
        pytest.param(None, "bulk-rnaseq", "bulk-rnaseq", id="no-library-source-attribute-at-all"),
    ],
)
def test_a_single_cell_deposit_rules_a_bulk_hint_out(
    library_source: str | None, chemistry: str, expected: str | None
) -> None:
    """A record declaring a single-cell library makes a BULK hint non-credible, and nothing more.

    ``library_source`` is deterministic, needs no model and no network, and speaks on exactly the axis
    the cross-family guard fires on. Its authority is one-directional: it may decline to offer a hint,
    never supply one, so a single-cell reading leaves a single-cell hint exactly as it found it.

    Bare ``TRANSCRIPTOMIC`` must change nothing, and that is the load-bearing negative — most
    single-cell deposits carry it, so reading its absence as evidence of bulk would false-block
    correct datasets by the hundred. Matching is tolerant of case and of a hyphen (the same normalizing
    the KB's own entailment test does) and stops there: the answer to an unenumerable list of spellings
    is not a longer list.
    """
    got = chemistry_hypothesis(
        [_chem_assertion(chemistry)], records=[_experiment_record(library_source)]
    )
    if expected is None:
        assert got is None
    else:
        assert got is not None and got.value == expected


def test_a_dataset_with_no_records_keeps_every_hint() -> None:
    """Most sequencing data never had an accession, so the no-record path is the ORDINARY one.

    An archive-shaped column may only ever enrich; a rule that needed one would refuse the in-house
    plate on the lab filesystem, which has no records at all and never will.
    """
    claim = [_chem_assertion("bulk-rnaseq")]
    empty: list[list[ArchiveRecord] | None] = [None, []]
    for records in empty:
        got = chemistry_hypothesis(claim, records=records)
        assert got is not None and got.value == "bulk-rnaseq"
    got = chemistry_hypothesis(claim)  # the parameter is optional, and absent means absent
    assert got is not None and got.value == "bulk-rnaseq"


def test_a_single_cell_record_never_manufactures_a_hypothesis() -> None:
    """It rules OUT. Where the prose named no chemistry, a record leaves the answer at ``None``.

    Letting metadata *name* a chemistry would be the ninth evidence test this resolver does not
    build — a spec could then identify itself by being described rather than by what is in its reads.
    Silence is what a record is entitled to produce.
    """
    records = [_experiment_record("TRANSCRIPTOMIC SINGLE CELL")]
    assert chemistry_hypothesis([], records=records) is None
    two_protocols = [_chem_assertion("bulk-rnaseq"), _chem_assertion("10x-3p-gex-v3")]
    assert chemistry_hypothesis(two_protocols, records=records) is None


def test_a_single_cell_record_declines_the_hint_rather_than_blocking(tmp_path: Path) -> None:
    """End to end, against the exit-4 refusal this exists to prevent — and its own control.

    The bytes are 10x v3, the prose says bulk. With no record that is a cross-family contradiction and
    a human is asked (the control below, and the case
    ``test_bulk_metadata_but_single_cell_bytes_surfaces_a_reverse_conflict`` pins it on its own). Hand
    the same run a deposit declaring ``TRANSCRIPTOMIC SINGLE CELL`` and the bulk hint is simply not
    offered: same winner, no conflict, no blocker, exit 0.

    The worst this rule can do is withhold a hint — it raises nothing of its own, so there is no new
    way for it to refuse a dataset.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=1500, seed=0)
    f1 = tmp_path / "sample_R1.fastq.gz"
    f2 = tmp_path / "sample_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])
    prose = [_chem_assertion("bulk RNA-seq")]

    control = chemistry_hypothesis(prose)
    assert control is not None and control.value == "bulk RNA-seq"
    refused = resolve_dataset(
        [f1, f2], registry=registry_for(spec), hypothesis=control, use_cache=False
    )
    assert refused.exit_code() == 4
    assert [c.id for c in refused.result.conflicts] == [
        "conflict-bulk-asserted-single-cell-observed"
    ]

    hypothesis = chemistry_hypothesis(
        prose, records=[_experiment_record("TRANSCRIPTOMIC SINGLE CELL")]
    )
    assert hypothesis is None
    out = resolve_dataset(
        [f1, f2], registry=registry_for(spec), hypothesis=hypothesis, use_cache=False
    )
    assert out.result.candidates[0].technology == "10x-3p-gex-v3"
    assert out.result.conflicts == [] and out.result.blockers == []
    assert out.exit_code() == 0


def test_metadata_v2_vs_reads_v3_resolves_to_v3_at_the_leaf(tmp_path: Path) -> None:
    """Family-level authority (2026.7.8): asserted v2 vs observed v3 is a WITHIN-family leaf difference.

    A paper names the assay family (10x 3' GEX) reliably and the exact leaf (v2 vs v3) vaguely, and the
    bytes decide the leaf. So this is agreement at the family level, not a block: resolve to v3 at exit
    0, but keep the discarded v2 claim as a RESOLVED conflict (auditable, non-blocking) — "three truths,
    never merged". This is GSE229022 in miniature ("10x 3' v2/v3" in prose, byte-provably v3)."""
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=1500, seed=0)
    f1 = tmp_path / "sample_R1.fastq.gz"  # observed 28 bp
    f2 = tmp_path / "sample_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])

    out = resolve_dataset(
        [f1, f2],
        registry=registry_for(spec),
        hypothesis=Hypothesis(value="10x-3p-gex-v2", id="meta-1", confidence=0.9),
        use_cache=False,
    )
    # the library takes the observed leaf (v3), and — same family — this does NOT block
    assert out.result.candidates[0].technology == "10x-3p-gex-v3"
    assert out.exit_code() == 0
    # the disagreement is recorded, not silently dropped: one RESOLVED conflict decided by code
    assert len(out.result.conflicts) == 1
    conflict = out.result.conflicts[0]
    assert conflict.kind == "observed_vs_asserted"
    assert conflict.status == "resolved"
    assert conflict.resolution is not None
    assert conflict.resolution.decided_by == "code"
    assert conflict.resolution.chosen_value == "28"
    assert conflict.resolution.basis == "observed"
    values = {p.value: p.basis for p in conflict.positions}
    assert values == {"26": "asserted", "28": "observed"}


def test_metadata_v3_vs_reads_v2_also_resolves_at_the_leaf(tmp_path: Path) -> None:
    """The within-family suppression is symmetric — asserted v3 over observed v2 resolves to v2, exit 0.

    (The other direction of the GSE229022 case: whichever leaf the bytes show wins, and the prose's
    family claim is satisfied either way.)"""
    spec = kb.load_spec("10x-3p-gex-v2")
    reads = kb.generate_reads(spec, n=1500, seed=0)
    f1 = tmp_path / "sample_R1.fastq.gz"  # observed 26 bp
    f2 = tmp_path / "sample_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])

    out = resolve_dataset(
        [f1, f2],
        registry=registry_for(spec),
        hypothesis=Hypothesis(value="10x-3p-gex-v3", id="meta-1", confidence=0.9),
        use_cache=False,
    )
    assert out.result.candidates[0].technology == "10x-3p-gex-v2"
    assert out.exit_code() == 0
    assert [c.status for c in out.result.conflicts] == ["resolved"]


def test_same_family_groups_leaves_under_their_root() -> None:
    """The predicate the within-family suppression turns on: leaves of one family share a root."""
    from seqforge.resolve.confuse import same_family

    specs = kb.load_all_specs()
    assert same_family(specs, "10x-3p-gex-v2", "10x-3p-gex-v3")  # siblings
    assert same_family(specs, "10x-3p-gex-v2", "10x-3p-gex")  # a leaf and its family node
    assert same_family(specs, "10x-3p-gex-v3", "10x-3p-gex-v3.1")
    assert same_family(specs, "10x-3p-gex-v2", "10x-3p-gex-v2")  # reflexive
    # cross-family: a paper-vs-bytes disagreement here IS a real conflict, must NOT be suppressed
    assert not same_family(specs, "10x-3p-gex-v2", "bulk-rnaseq")
    assert not same_family(specs, "splitseq", "bd-rhapsody-wta")
    assert not same_family(specs, "10x-3p-gex-v2", "no-such-tech")  # unknown id


def test_single_cell_collapse_guard_is_structural_not_length() -> None:
    """#7/#11 unit: an asserted single-cell chemistry + a barcodeless bulk winner is a collapse.

    The length-only `_detect_conflicts` cannot see this — a bulk winner has no barcode read, so its
    observed barcode length is None and the guard returns early. This one keys on structure
    (asserted-barcoded vs observed-barcodeless), which is why it needs to exist separately.
    """
    from seqforge.resolve.escalate import _single_cell_collapse_conflict

    specs = kb.load_all_specs()

    # The guard reads only `.tech` off the winner, so `_te` (the file's evaluation builder) supplies it.
    top_bulk = _te("bulk-rnaseq", 0.8)
    top_single_cell = _te("10x-3p-gex-v3", 0.8)

    conflict = _single_cell_collapse_conflict(
        "10x-3p-gex-v2", "harvest", 0.9, top_bulk, specs["bulk-rnaseq"], [], specs
    )
    assert conflict is not None
    assert conflict.kind == "observed_vs_asserted" and conflict.status == "open"
    assert {p.value: p.basis for p in conflict.positions} == {
        "10x-3p-gex-v2": "asserted",
        "bulk-rnaseq": "observed",
    }
    # negatives — no collapse to surface:
    # a bulk chemistry was asserted and bulk won (agreement)
    assert (
        _single_cell_collapse_conflict(
            "bulk-rnaseq", "harvest", 0.9, top_bulk, specs["bulk-rnaseq"], [], specs
        )
        is None
    )
    # the winner is itself barcoded (single-cell won or tied)
    assert (
        _single_cell_collapse_conflict(
            "10x-3p-gex-v2", "harvest", 0.9, top_single_cell, specs["10x-3p-gex-v3"], [], specs
        )
        is None
    )
    # no hypothesis at all
    assert (
        _single_cell_collapse_conflict(None, None, 0.8, top_bulk, specs["bulk-rnaseq"], [], specs)
        is None
    )


def test_single_cell_metadata_but_bulk_bytes_surfaces_a_collapse_conflict(tmp_path: Path) -> None:
    """End-to-end #7/#11: reads only a bulk library matches, but metadata asserts 10x v2. The barcode
    read never validated, so the generic bulk fallback won by default — that must surface as an
    observed-vs-asserted conflict (exit 4), not compile a bulk manifest at exit 0 for a dataset the
    paper calls single-cell. This is the GSE126954 over-length-sample / GSE274290 pre-BD-spec path.

    The registry is SYNTHETIC rather than the shipped default, and rather than empty. This test's
    subject is a *silent* failure mode — a single-cell dataset compiling as bulk at exit 0 — so
    passing for a slightly different reason costs the corpus, not seconds. What was checked before
    swapping: `_barcode_onlist_available` is size- and content-blind (it asks only whether a list is
    registered and materializable), `_over_length_admitted_by_onlist` is anchored on a floor that
    SCALES with `n_entries` rather than keying on it, and a random 75-mer hits a 64-barcode synthetic
    list less often than it chance-hits a 6.8M one, not more. Then verified end to end: identical
    exit 4, identical winner `bulk-rnaseq`, identical `conflict-single-cell-collapsed-to-bulk`,
    identical rung, no blocker and no question either way."""
    rng = random.Random(0)
    r1 = [
        "".join(rng.choice("ACGT") for _ in range(75)) for _ in range(1500)
    ]  # no barcode geometry
    r2 = ["".join(rng.choice("ACGT") for _ in range(90)) for _ in range(1500)]
    f1 = tmp_path / "sample_R1.fastq.gz"
    f2 = tmp_path / "sample_R2.fastq.gz"
    write_fastq_gz(f1, r1)
    write_fastq_gz(f2, r2)

    out = resolve_dataset(
        [f1, f2],
        registry=registry_for(kb.load_spec("10x-3p-gex-v2")),
        hypothesis=Hypothesis(value="10x-3p-gex-v2", id="meta-1", confidence=0.9),
        use_cache=False,
    )
    assert out.result.candidates
    assert out.result.candidates[0].technology == "bulk-rnaseq"
    assert out.exit_code() == 4
    assert any(c.id == "conflict-single-cell-collapsed-to-bulk" for c in out.result.conflicts), [
        c.id for c in out.result.conflicts
    ]


def test_bulk_asserted_single_cell_observed_guard_is_structural() -> None:
    """The MIRROR of the collapse guard: an asserted bulk chemistry + a barcoded single-cell winner is a
    cross-family contradiction that must surface. Same error class, the other direction."""
    from seqforge.resolve.escalate import _bulk_asserted_single_cell_observed

    specs = kb.load_all_specs()

    # The guard reads only `.tech` off the winner, so `_te` (the file's evaluation builder) supplies it.
    top_single_cell = _te("10x-3p-gex-v3", 0.8)
    top_bulk = _te("bulk-rnaseq", 0.8)

    conflict = _bulk_asserted_single_cell_observed(
        "bulk-rnaseq", "harvest", 0.9, top_single_cell, specs["10x-3p-gex-v3"], [], specs
    )
    assert conflict is not None
    assert conflict.id == "conflict-bulk-asserted-single-cell-observed"
    assert conflict.kind == "observed_vs_asserted" and conflict.status == "open"
    assert {p.value: p.basis for p in conflict.positions} == {
        "bulk-rnaseq": "asserted",
        "10x-3p-gex-v3": "observed",
    }
    # negatives — no reverse conflict to surface:
    # a single-cell chemistry was asserted -> that is the FORWARD collapse guard's job, not this one
    assert (
        _bulk_asserted_single_cell_observed(
            "10x-3p-gex-v2", "harvest", 0.9, top_single_cell, specs["10x-3p-gex-v3"], [], specs
        )
        is None
    )
    # the winner is itself bulk (agreement)
    assert (
        _bulk_asserted_single_cell_observed(
            "bulk-rnaseq", "harvest", 0.9, top_bulk, specs["bulk-rnaseq"], [], specs
        )
        is None
    )
    # no hypothesis at all
    assert (
        _bulk_asserted_single_cell_observed(
            None, None, 0.8, top_single_cell, specs["10x-3p-gex-v3"], [], specs
        )
        is None
    )


def test_bulk_metadata_but_single_cell_bytes_surfaces_a_reverse_conflict(tmp_path: Path) -> None:
    """End-to-end mirror: the reads are single-cell 10x v3 but the metadata asserts bulk RNA-seq. That
    cross-family contradiction must surface (exit 4) rather than silently compile a single-cell manifest
    for a dataset the paper calls bulk. The library still takes the observed value (v3)."""
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=1500, seed=0)
    f1 = tmp_path / "sample_R1.fastq.gz"
    f2 = tmp_path / "sample_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])

    out = resolve_dataset(
        [f1, f2],
        registry=registry_for(spec),
        hypothesis=Hypothesis(value="bulk-rnaseq", id="meta-1", confidence=0.9),
        use_cache=False,
    )
    assert out.result.candidates[0].technology == "10x-3p-gex-v3"
    assert out.exit_code() == 4
    assert any(
        c.id == "conflict-bulk-asserted-single-cell-observed" for c in out.result.conflicts
    ), [c.id for c in out.result.conflicts]


def test_narrows_to_is_directional_subtree_membership() -> None:
    """The predicate ADR-0020 turns on: an asserted node CONTAINS the observed one, or it does not."""
    from seqforge.resolve.confuse import narrows_to

    specs = kb.load_all_specs()
    assert narrows_to(specs, "10x-3p-gex", "10x-3p-gex-v3")  # a family narrows to its leaf
    assert narrows_to(specs, "10x-3p-gex-v3", "10x-3p-gex-v3")  # reflexive
    assert narrows_to(specs, "bd-rhapsody-wta-enhanced", "bd-rhapsody-wta-enhanced-v2")
    # the direction matters: a leaf claims MORE than its family, so an observed family node is the
    # bytes saying less than the prose — not a narrowing.
    assert not narrows_to(specs, "10x-3p-gex-v3", "10x-3p-gex")
    # ...and siblings do not narrow to each other. Asserted v2 against observed v3 is a real
    # disagreement that `same_family` keeps as a RESOLVED conflict (2026.7.8); it is not suppressed.
    assert not narrows_to(specs, "10x-3p-gex-v2", "10x-3p-gex-v3")
    assert not narrows_to(specs, "bulk-rnaseq", "10x-3p-gex-v3")
    assert not narrows_to(specs, "no-such-tech", "10x-3p-gex-v3")


def test_a_family_hypothesis_is_agreement_with_the_leaf_the_bytes_decided(tmp_path: Path) -> None:
    """ADR-0020 end to end: prose says "10x 3'", the bytes say v3, and that is one answer, not two.

    The prose named a node and the bytes named a descendant of it, so the claim is *satisfied*: exit
    0, the leaf in the manifest, and nothing surfaced. This is the shape both operator doors take too
    (`manifest fill --chemistry 10x-3p-gex`), and neither of them passes through `verify_drafts`.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=1500, seed=0)
    f1 = tmp_path / "sample_R1.fastq.gz"
    f2 = tmp_path / "sample_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])

    out = resolve_dataset(
        [f1, f2],
        registry=registry_for(spec),
        hypothesis=Hypothesis(value="10x 3'", id="meta-1", confidence=0.9),
        use_cache=False,
    )
    assert out.result.candidates[0].technology == "10x-3p-gex-v3"
    assert out.exit_code() == 0
    assert out.result.conflicts == []


def test_an_archive_filing_word_asserts_no_chemistry_at_all(tmp_path: Path) -> None:
    """The defect this PR closes, at the resolver: `RNA-Seq` steered nothing, and now names nothing.

    Every transcriptomic run in SRA carries `library_strategy: RNA-Seq`, and the old matcher read that
    as `bulk-rnaseq` — so a single-cell library, byte-provably 10x v3, became an asserted-bulk /
    observed-single-cell contradiction and a decided dataset turned into an exit-4 question
    (GSE229022), or the bogus hypothesis displaced a real one (GSE317744). The guards are unchanged
    and still fire on a *real* bulk claim, which the test above pins; what changed is that a word
    naming a whole field of assays no longer makes one.
    """
    from seqforge.resolve.escalate import _bulk_asserted_single_cell_observed

    specs = kb.load_all_specs()
    top = _te("10x-3p-gex-v3", 0.8)
    for word in ("RNA-Seq", "Illumina", "transcriptome"):
        assert (
            _bulk_asserted_single_cell_observed(
                word, "harvest", 0.9, top, specs["10x-3p-gex-v3"], [], specs
            )
            is None
        ), word

    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=1500, seed=0)
    f1 = tmp_path / "sample_R1.fastq.gz"
    f2 = tmp_path / "sample_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])
    out = resolve_dataset(
        [f1, f2],
        registry=registry_for(spec),
        hypothesis=Hypothesis(value="RNA-Seq", id="harvest", confidence=0.9),
        use_cache=False,
    )
    assert out.result.candidates[0].technology == "10x-3p-gex-v3"
    assert out.exit_code() == 0, [c.id for c in out.result.conflicts]


# ================================================================================================
# over-length — an over-sequenced barcode read still resolves
# ================================================================================================
#
# Over-length barcode reads: an over-sequenced 10x R1 (CB/UMI in bp0-28, the rest junk) still
# resolves to a concrete chemistry, and when length can no longer separate v2 from v3 the WHITELIST does.
#
# The real GSE229022 has samples whose barcode read is sequenced to 150 bp. Length alone cannot tell a
# 150 bp v2 read from a 150 bp v3 read (both are "over-length") — so these tests prove the rung-3
# whitelist decides, and decides *correctly* (the read whose first 16 bp hit a chemistry's onlist is
# that chemistry's), and that an over-length read raises neither a blocker nor a spurious length conflict.


OVER_LEN = 150  # the run read length: an over-sequenced barcode read is this long, not 26/28


def test_a_whitelist_hitting_chemistry_dominates_a_geometric_sibling_that_missed(
    tmp_path: Path,
) -> None:
    """10x 3' v3 and Multiome GEX differ ONLY by whitelist (3M-february-2018 vs 737K-arc-v1). On an
    over-length v3 read the barcode read is also a fine cDNA, so a sibling whose whitelist did NOT hit
    can take the swapped-role seat and out-score the honest v3 — and, being processing-divergent from
    it, turn a settled call into an ask-human that collapses to bulk.

    The whitelist that HIT is the arbiter: v3's 3M matches, Multiome's ARC does not, so v3 dominates
    and Multiome is not a divergent contender. Regression guard for the escalate fix; it goes red if a
    non-hitting geometric sibling is allowed back into the divergent tie.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    cb_pool = kb.build_pools(spec, seed=0)["cb_whitelist"]
    rng = random.Random(1)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    # A clean-ish over-length v3 library: CB from the 3M pool, then UMI + junk out to 150 bp.
    barcode = [rng.choice(cb_pool) + rand(OVER_LEN - 16) for _ in range(600)]
    transcripts = [rand(OVER_LEN) for _ in range(150)]
    cdna = [rng.choice(transcripts) for _ in range(600)]
    r_bc = tmp_path / "sample_bc.fastq.gz"
    r_cd = tmp_path / "sample_cd.fastq.gz"
    write_fastq_gz(r_bc, barcode)
    write_fastq_gz(r_cd, cdna)

    # The full KB (Multiome GEX included) and a registry carrying BOTH whitelists — 3M hits, ARC misses.
    reg = registry_for(spec)
    out = resolve_dataset([r_bc, r_cd], registry=reg, use_cache=False)

    scored = {c.technology for c in out.result.candidates}
    assert "10x-multiome-gex" in scored, (
        "the ARC sibling must be a scored candidate, not filtered out"
    )
    assert out.result.candidates[0].technology in {"10x-3p-gex-v3", "10x-3p-gex-v3.1"}
    assert "10x-multiome-gex" not in out.result.candidates[0].equivalence_members
    assert not out.result.blockers, [b.message for b in out.result.blockers]


def _over_length(
    tmp_path: Path, tech: str, umi_len: int, total_len: int = OVER_LEN
) -> tuple[list[Path], dict[str, list[str]]]:
    """A barcode read (16 bp CB from the tech's whitelist + UMI + junk) and a cDNA read, both
    ``total_len`` bp. Default 150 bp (>= over_length_min, admitted on geometry); pass a dead-zone
    length (e.g. 75 bp) to exercise the onlist admission (#7).

    The CB is drawn from the tech's own pool so it hits that chemistry's whitelist and no other.
    """
    spec = kb.load_spec(tech)
    cb_pool = kb.build_pools(spec, seed=0)["cb_whitelist"]
    rng = random.Random(0)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    seqs = {
        "R1": [
            rng.choice(cb_pool) + rand(umi_len) + rand(total_len - 16 - umi_len) for _ in range(600)
        ],
        "R2": [rand(total_len) for _ in range(600)],
    }
    paths = []
    for rid in ("R1", "R2"):
        p = tmp_path / f"{tech}_{rid}.fastq.gz"
        write_fastq_gz(p, seqs[rid])
        paths.append(p)
    return paths, seqs


DEAD_LEN = 75  # in the over-length DEAD ZONE: > canonical 26/28 bp, < over_length_min (100)

#: ``(tech, umi_len, total_len, winners)`` — one row per shape of over-sequenced barcode read the
#: whitelist has to rescue. The three were separate functions differing only in these four values.
WHITELIST_ADMITS = [
    # v3 and v3.1 are benign twins recorded together; either is the right answer, v2 is not.
    pytest.param(
        "10x-3p-gex-v3", 12, None, {"10x-3p-gex-v3", "10x-3p-gex-v3.1"},
        id="an-over-length-v3-barcode-read-resolves-to-v3",
    ),
    # Same 150 bp geometry, but the CB hits 737K-august-2016 -> v2. The whitelist alone separates.
    pytest.param(
        "10x-3p-gex-v2", 10, None, {"10x-3p-gex-v2"},
        id="an-over-length-v2-barcode-read-resolves-to-v2-not-v3",
    ),
    # #7: an R1 over-sequenced to 75 bp sits in the DEAD ZONE — too long to be the canonical 26 bp v2
    # read, too short for the over_length_min (100) that admits a full-length over-sequenced read.
    # Length alone forbids it, and that is deliberate (a 60-94 bp cDNA must not pass as a barcode).
    # GSE126954's over-sequenced SRX5411291 is exactly this; before the fix it collapsed to bulk.
    pytest.param(
        "10x-3p-gex-v2", 10, DEAD_LEN, {"10x-3p-gex-v2"},
        id="a-dead-zone-barcode-read-is-admitted-by-its-whitelist",
    ),
]  # fmt: skip


@pytest.mark.parametrize("tech, umi_len, total_len, winners", WHITELIST_ADMITS)
def test_the_whitelist_admits_a_barcode_read_length_alone_would_refuse(
    tech: str, umi_len: int, total_len: int | None, winners: set[str], tmp_path: Path
) -> None:
    """An over-sequenced R1 is still a barcode read, and the onlist is what says so.

    The registry registers ONLY this chemistry's whitelist, so the decision is the whitelist's and
    nothing else's — which is why every row must resolve at rung 3 or above: length could not do it.
    """
    spec = kb.load_spec(tech)
    kwargs = {"total_len": total_len} if total_len is not None else {}
    paths, _ = _over_length(tmp_path, tech, umi_len=umi_len, **kwargs)
    reg = registry_for(spec)

    out = resolve_dataset(paths, registry=reg, use_cache=False)

    assert not out.result.blockers, [b.message for b in out.result.blockers]
    winner = out.result.candidates[0]
    assert winner.technology in winners, [c.technology for c in out.result.candidates[:3]]
    assert winner.score.status == "scored"
    # Both reads were assigned (the barcode read to R1, cDNA to R2) — nothing dropped despite the
    # over-length: the whole point is that an over-sequenced R1 is not left unassigned.
    assert set(winner.role_assignment.assignment) == {"R1", "R2"}
    assert not winner.role_assignment.unassigned
    # Decided BY the onlist, so the chemistry resolves at rung 3 (the length gate FAILed).
    assert winner.rung_resolved.get("chemistry", 0) >= 3


def test_a_dead_zone_barcode_read_below_the_support_gate_is_still_admitted(tmp_path: Path) -> None:
    """The REAL SRX5411291 case the perfect-whitelist fixtures never exposed. Those draw every CB from
    the pool, so the exact hit rate is ~1.0 — above BOTH the 0.6 support gate and the admission bar, so
    they pass whichever bar admission uses. Real over-sequenced barcode reads carry ordinary sequencing
    error; seqforge matches CBs EXACTLY (no 1MM correction, which STARsolo does), so the exact hit rate
    sits well below 0.6. Here ~60% of CBs carry a 1 bp error -> exact hit ~0.4: below the support gate,
    far above the whitelist floor. The support-`min` gate rejected it and the sample collapsed to bulk;
    the floor-anchored admission bar (barcode-vs-cDNA) admits it. This test FAILS under the old gate.
    """
    spec = kb.load_spec("10x-3p-gex-v2")
    cb_pool = kb.build_pools(spec, seed=0)["cb_whitelist"]
    rng = random.Random(0)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    def one_error(cb: str) -> str:
        """Flip one base -> misses the EXACT-match whitelist (a 1 bp mismatch STARsolo would correct).
        The chance the flip lands on another whitelist entry is ~n_entries/4^16 ≈ 2e-4, negligible."""
        i = rng.randrange(16)
        return cb[:i] + rng.choice([b for b in "ACGT" if b != cb[i]]) + cb[i + 1 :]

    barcode = []
    for i in range(600):
        cb = rng.choice(cb_pool)
        if i % 5 >= 2:  # ~60% carry a 1 bp error -> exact hit rate ~0.4 (0.05 < 0.4 < 0.6)
            cb = one_error(cb)
        barcode.append(cb + rand(10) + rand(DEAD_LEN - 26))
    cdna = [rand(DEAD_LEN) for _ in range(600)]
    r1 = tmp_path / "v2_R1.fastq.gz"
    r2 = tmp_path / "v2_R2.fastq.gz"
    write_fastq_gz(r1, barcode)
    write_fastq_gz(r2, cdna)
    reg = registry_for(spec)  # ONLY the 737K-august-2016 (v2) whitelist

    # Half one (the admission calibration): the v2 barcode role is admitted (not forbidden) at a
    # sub-0.6 hit rate. Fails under the old support-`min` gate.
    probes = [
        WindowProbe(observation=probe_file(p), seqs=s) for p, s in ((r1, barcode), (r2, cdna))
    ]
    ev = build_tech_evaluation(spec, probes, reg)
    assert ev.valid, (
        "a dead-zone barcode read hitting the whitelist below 0.6 must still be admitted"
    )

    # Half two (the dominance rule): end to end it resolves to v2 (rung 3), not bulk. At this hit rate
    # bulk has the higher RAW score (a 75 bp read is a fine cDNA), so admission alone is not enough --
    # the barcoded rung-3 candidate must not be shadowed by the barcodeless fallback (escalate anchor).
    out = resolve_dataset([r1, r2], registry=reg, use_cache=False)
    winner = out.result.candidates[0]
    assert winner.technology == "10x-3p-gex-v2", [c.technology for c in out.result.candidates[:3]]
    assert winner.rung_resolved.get("chemistry", 0) >= 3


def test_a_dead_zone_read_that_misses_every_whitelist_is_not_admitted(tmp_path: Path) -> None:
    """The safety half, and why the admission is keyed on the whitelist and not on length: a 75 bp read
    whose first 16 bp hit NO whitelist is a cDNA/junk read, not a barcode. The admission must NOT fire —
    the read stays forbidden for v2 and the data resolves to the generic bulk fallback. If this ever
    regressed, any 60-94 bp cDNA would be admitted as a barcode read and rungs 0-2 would stop being
    separable."""
    rng = random.Random(1)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    r1 = tmp_path / "x_R1.fastq.gz"
    r2 = tmp_path / "x_R2.fastq.gz"
    write_fastq_gz(r1, [rand(DEAD_LEN) for _ in range(600)])  # random 75 bp -> hits no whitelist
    write_fastq_gz(r2, [rand(DEAD_LEN) for _ in range(600)])
    reg = registry_for(
        kb.load_spec("10x-3p-gex-v2")
    )  # v2 whitelist IS registered; the reads miss it

    out = resolve_dataset([r1, r2], registry=reg, use_cache=False)
    winner = out.result.candidates[0] if out.result.candidates else None
    assert winner is not None
    assert winner.technology != "10x-3p-gex-v2", (
        "a whitelist-missing 75 bp read must not be admitted"
    )
    assert winner.technology == "bulk-rnaseq"


# `GSE282525` (Vijay Lab) declares "Chromium Next GEM Single Cell 5' Reagent Kit v2" verbatim and
# archives every run at 10/10/28/90 — a declared-26 bp kit sequenced two cycles long. Reading
# `10x-5p-gex-v2/spec.yaml`'s `{test: segment_length, length: 26, tolerance: 0, over_length_min: 100}`
# statically says the true leaf is exact-checked, fails, and is eliminated before scoring, so the spec
# needs a tolerance. RUNNING IT SAYS OTHERWISE, and that is why this is a test rather than a spec edit
# (#177): 26 < 28 < 100 is exactly the over-length DEAD ZONE, the whitelist admission fires, and the
# leaf is scored. `tolerance: 0` is doing no harm here, and it is load-bearing elsewhere — widening it
# to admit 28 would break the symmetry `10x-5p-gex-v2` keeps with `10x-3p-gex-v2` deliberately (the
# two are byte-identical, test for test and weight for weight, because they are the KB's one genuinely
# read-undecidable pair), and would collapse the 26-vs-28 UMI distinction the 5' v2/v3 split IS.
_GSE282525_R1 = (
    28  # the archived R1 length: 16 bp CB + 10 bp UMI + 2 cycles the kit does not declare
)


def test_a_declared_chemistry_sequenced_two_cycles_long_still_reaches_its_own_leaf(
    tmp_path: Path,
) -> None:
    """The `GSE282525` shape, against the REAL registry so every competing whitelist is loaded.

    Three things are asserted and each one is a claim the static reading of the spec gets wrong:
    the 5' v2 leaf is SCORED rather than forbidden; it ties at the top with `10x-3p-gex-v2`, which is
    the honest answer because those two share the 26 bp geometry AND the 737K-august-2016 file; and the
    question asked names exactly that pair. In particular `10x-5p-gex-v3` — the leaf a widened tolerance
    or an eliminated v2 would hand this library to, along with the wrong whitelist — is nowhere near
    the top, because its own `3M-5pgex-jan-2023` is loaded here and declines these barcodes.
    """
    cbs = real_cbs(4000, onlist="737K-august-2016")
    rng = random.Random(3)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    r1 = tmp_path / "GSE282525_R1.fastq.gz"
    r2 = tmp_path / "GSE282525_R2.fastq.gz"
    write_fastq_gz(r1, [rng.choice(cbs) + rand(_GSE282525_R1 - 16) for _ in range(600)])
    write_fastq_gz(r2, [rand(90) for _ in range(600)])

    out = resolve_dataset([r1, r2], registry=DEFAULT_REGISTRY, use_cache=False)
    scored = {c.technology: c.score for c in out.result.candidates}

    assert "10x-5p-gex-v2" in scored, "the declared leaf must be a candidate at all"
    assert scored["10x-5p-gex-v2"].status == "scored", scored["10x-5p-gex-v2"].reason
    assert not out.result.blockers, [b.message for b in out.result.blockers]

    top = out.result.candidates[0].score.value
    assert top is not None
    assert scored["10x-5p-gex-v2"].value == top, (
        "the declared leaf ties for the top, it is not a runner-up"
    )
    assert scored["10x-3p-gex-v2"].value == top, "and its read-undecidable partner ties with it"
    v3 = scored["10x-5p-gex-v3"].value
    assert v3 is not None and v3 < top, (
        "the 28 bp 5' sibling must NOT win on geometry: its whitelist declines these barcodes"
    )

    assert [q.options for q in out.result.questions] == [["10x-3p-gex-v2", "10x-5p-gex-v2"]], (
        "the surviving tie is the declared read-undecidable pair, and resolve asks rather than guesses"
    )


def test_the_two_extra_cycles_do_not_cost_the_leaf_its_metadata_decision(tmp_path: Path) -> None:
    """`GSE282525` with the claim its record actually makes: the prose says "10x 5'", so the tie above
    resolves — to `10x-5p-gex-v2` and its `737K-august-2016` whitelist, at exit 0.

    This is the half that would have been a SILENT wrong answer if the leaf really were eliminated:
    with v2 gone, the only 5' leaf left under the asserted family is `10x-5p-gex-v3`, and the resolver
    would have decided it and compiled a 12 bp UMI against `3M-5pgex-jan-2023`. Nothing would be red.
    """
    cbs = real_cbs(4000, onlist="737K-august-2016")
    rng = random.Random(3)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    r1 = tmp_path / "GSE282525_R1.fastq.gz"
    r2 = tmp_path / "GSE282525_R2.fastq.gz"
    write_fastq_gz(r1, [rng.choice(cbs) + rand(_GSE282525_R1 - 16) for _ in range(600)])
    write_fastq_gz(r2, [rand(90) for _ in range(600)])

    out = resolve_dataset(
        [r1, r2], registry=DEFAULT_REGISTRY, use_cache=False, hypothesis=Hypothesis(value="10x 5'")
    )

    assert out.exit_code() == 0, [q.prompt for q in out.result.questions]
    assert out.result.candidates[0].technology == "10x-5p-gex-v2"


def test_genuine_bulk_still_resolves_to_bulk_with_barcode_whitelists_registered(
    tmp_path: Path,
) -> None:
    """Safety guard for the dominance anchor (a barcoded candidate that positively matched a whitelist
    is not shadowed by the barcodeless fallback): it must NEVER hijack genuine bulk. Canonical ~100 bp
    paired cDNA reads with NO barcode content, resolved with the v2 whitelist registered, must still
    resolve to bulk-rnaseq. v2 IS consulted here (it reaches rung 3, and its barcode read even passes
    the over-length geometry gate at 100 bp = over_length_min) — but its onlist FAILS, so
    ``barcode_onlist_hit`` stays False, the anchor never promotes it, and bulk wins. That False is the
    invariant keeping every real bulk dataset (and any dataset whose barcodes are genuinely absent)
    unaffected: the anchor keys on the whitelist ACTUALLY matching, not on rung 3."""
    rng = random.Random(7)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    r1 = tmp_path / "bulk_R1.fastq.gz"
    r2 = tmp_path / "bulk_R2.fastq.gz"
    write_fastq_gz(r1, [rand(100) for _ in range(600)])  # canonical cDNA length, no barcode
    write_fastq_gz(r2, [rand(100) for _ in range(600)])
    reg = registry_for(kb.load_spec("10x-3p-gex-v2"))  # whitelist registered but never hit

    out = resolve_dataset([r1, r2], registry=reg, use_cache=False)
    assert out.result.candidates[0].technology == "bulk-rnaseq", [
        c.technology for c in out.result.candidates[:3]
    ]


def test_both_v2_and_v3_accept_the_over_length_read_on_geometry_alone(tmp_path: Path) -> None:
    """Why the whitelist is load-bearing: at rungs 0-2 (onlist withheld) BOTH chemistries accept the
    150 bp read, so neither length nor segmentation can pick — exactly the sub-rung-3 tie the
    v2<->v3 confusable_with declaration is honest about."""
    paths, seqs = _over_length(tmp_path, "10x-3p-gex-v3", umi_len=12)
    empty = OnlistRegistry(offline=True)  # withhold every whitelist -> rungs 0-2 only
    probes = [
        WindowProbe(observation=probe_file(p), seqs=seqs[rid])
        for p, rid in zip(paths, ("R1", "R2"), strict=True)
    ]
    for tech in ("10x-3p-gex-v2", "10x-3p-gex-v3"):
        ev = build_tech_evaluation(kb.load_spec(tech), probes, empty)
        assert ev.valid, f"{tech} should accept the over-length read on geometry alone"


def test_an_over_length_read_with_a_ragged_tail_is_not_flagged_as_pretrimmed(
    tmp_path: Path,
) -> None:
    """A trimmed barcode read blocks (its offsets shifted); an over-length read whose *junk tail* is
    ragged does not — CB/UMI are intact at the fixed offsets, so that variation is harmless."""
    spec = kb.load_spec("10x-3p-gex-v3")
    cb_pool = kb.build_pools(spec, seed=0)["cb_whitelist"]
    rng = random.Random(1)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    # mode 150 (over-length), but a minority of reads have a shorter junk tail -> n_distinct > 1.
    barcode = [rng.choice(cb_pool) + rand(12) + rand(122 if i % 10 else 100) for i in range(600)]
    cdna = [rand(OVER_LEN) for _ in range(600)]
    r1 = tmp_path / "v3_R1.fastq.gz"
    r2 = tmp_path / "v3_R2.fastq.gz"
    write_fastq_gz(r1, barcode)
    write_fastq_gz(r2, cdna)

    out = resolve_dataset([r1, r2], registry=registry_for(spec), use_cache=False)
    assert not any(b.code.name == "PRETRIMMED_VARIABLE_LENGTH" for b in out.result.blockers), [
        b.message for b in out.result.blockers
    ]
    assert out.result.candidates[0].technology in {"10x-3p-gex-v3", "10x-3p-gex-v3.1"}


def test_no_spurious_barcode_length_conflict_for_an_over_length_read(tmp_path: Path) -> None:
    """A v3 hypothesis on a 150 bp barcode read must NOT raise `conflict-barcode-length` (28 vs 150):
    the over-length read is expected geometry, not a contradiction to surface."""
    spec = kb.load_spec("10x-3p-gex-v3")
    paths, _ = _over_length(tmp_path, "10x-3p-gex-v3", umi_len=12)
    reg = registry_for(spec)

    out = resolve_dataset(
        paths, registry=reg, hypothesis=Hypothesis(value="10x-3p-gex-v3"), use_cache=False
    )
    assert not any(c.id == "conflict-barcode-length" for c in out.result.conflicts), [
        c.id for c in out.result.conflicts
    ]


def test_the_barcode_role_seats_on_the_whitelist_hitting_read_not_the_higher_scoring_mate(
    tmp_path: Path,
) -> None:
    """Two equal-length (over-length) reads where raw sum-maximization would SWAP the roles. The real
    barcode read carries ordinary sequencing error, so its exact-match onlist (~0.4) makes it score
    HIGHER as cDNA (its UMI keeps 20-mers distinct) than as its own barcode; the real cDNA read is a
    low-diversity library (a few dominant transcripts) and scores low on both roles. So the swap
    `(barcode->cDNA)+(cDNA->barcode)` out-sums the honest seat -- exactly PRJNA658829 SRR12575567. The
    barcode role must seat on the read that HITS the whitelist regardless: whitelist membership, not the
    score race, decides the barcode read. Without the constraint this seats R1 on the cDNA read."""
    spec = kb.load_spec("10x-3p-gex-v3")
    cb_pool = kb.build_pools(spec, seed=0)["cb_whitelist"]
    rng = random.Random(0)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    def one_error(cb: str) -> str:
        i = rng.randrange(16)
        return cb[:i] + rng.choice([b for b in "ACGT" if b != cb[i]]) + cb[i + 1 :]

    barcode = []
    for i in range(600):
        cb = rng.choice(cb_pool)
        if i % 5 >= 2:  # ~60% carry a 1 bp error -> exact-match onlist ~0.4 (below the 0.6 gate)
            cb = one_error(cb)
        barcode.append(cb + rand(12) + rand(OVER_LEN - 28))
    transcripts = [rand(OVER_LEN) for _ in range(150)]  # few dominant genes -> low distinct_ratio
    cdna = [rng.choice(transcripts) for _ in range(600)]

    r_bc = tmp_path / "sample_bc.fastq.gz"
    r_cd = tmp_path / "sample_cd.fastq.gz"
    write_fastq_gz(r_bc, barcode)
    write_fastq_gz(r_cd, cdna)
    reg = registry_for(spec)

    probes = [
        WindowProbe(observation=probe_file(r_bc), seqs=barcode),
        WindowProbe(observation=probe_file(r_cd), seqs=cdna),
    ]
    bc_sha = probes[0].observation.file.sha256
    ev = build_tech_evaluation(spec, probes, reg)
    # the barcode role (R1) seats on the whitelist-hitting read, not the higher-scoring cDNA mate
    assert ev.role_assignment_shas()["R1"] == bc_sha, ev.matrix_json()
    assert ev.barcode_onlist_hit

    # end to end: v3 wins and seats R1 on the barcode read -- no silent swap, and it composes clean
    out = resolve_dataset([r_bc, r_cd], registry=reg, use_cache=False)
    winner = out.result.candidates[0]
    assert winner.technology in {"10x-3p-gex-v3", "10x-3p-gex-v3.1"}
    assert winner.role_assignment.assignment["R1"] == bc_sha
    assert not out.result.blockers, [b.message for b in out.result.blockers]


def test_a_barcoded_winner_whose_read_hits_no_whitelist_is_refused(tmp_path: Path) -> None:
    """When a barcoded chemistry WINS but no read hits its (registered) whitelist, refuse rather than
    compose a pipeline STARsolo would run to ~0 valid barcodes at exit 0. Canonical 10x geometry -- a
    28 bp barcode read (so bulk, which needs both mates >=40 bp, is length-invalid and v3 wins) + a
    90 bp cDNA read -- but the barcodes are random and miss the registered whitelist. The whitelist WAS
    consulted (registered + materialized), so the miss is a real absence, not an un-checkable one:
    BARCODE_READ_ABSENT, exit 3. (Contrast the barcodeless bulk fallback, which is never refused.)"""
    spec = kb.load_spec("10x-3p-gex-v3")
    rng = random.Random(1)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    r1 = tmp_path / "bc_R1.fastq.gz"
    r2 = tmp_path / "cd_R2.fastq.gz"
    write_fastq_gz(
        r1, [rand(28) for _ in range(600)]
    )  # 28 bp barcode geometry, random -> miss list
    write_fastq_gz(r2, [rand(90) for _ in range(600)])  # cDNA
    reg = registry_for(spec)  # v3 whitelist REGISTERED (available), but the random barcodes miss it

    out = resolve_dataset([r1, r2], registry=reg, use_cache=False)
    assert out.exit_code() == 3, [c.technology for c in out.result.candidates[:3]]
    assert any(b.code == BlockerCode.BARCODE_READ_ABSENT for b in out.result.blockers), [
        b.code for b in out.result.blockers
    ]


def test_barcode_absent_refusal_abstains_when_a_sibling_barcoded_leaf_hits() -> None:
    """F1b must key on ALL valid candidates, not just ``top``. When a look-alike barcoded chemistry tops
    the score tie but its whitelist misses (v2 on a v3 library, both whitelists registered), while a
    sibling leaf's whitelist HITS (v3), the data IS barcoded and must not be refused — the tie/hypothesis
    resolves to the hitting leaf. Only when NO barcoded candidate hits any available whitelist is the
    data barcode-absent. This is the PRJNA658829 SRR12575567 regression: top=v2 (737K misses), v3 (3M)
    hits. The single-whitelist over-length fixtures can't see it — there v2's onlist is unavailable, so
    F1b abstains for the wrong reason (``barcode_onlist_available`` False, not because a sibling hit)."""
    from seqforge.models.resolve import TechScore
    from seqforge.resolve.assign import AssignmentResult
    from seqforge.resolve.escalate import _barcodeless_seated_blocker
    from seqforge.resolve.scoring import TechEvaluation

    def ev(tech: str, hit: bool, avail: bool = True) -> TechEvaluation:
        return TechEvaluation(
            tech=tech,
            read_set="full",
            roles=["R1", "R2"],
            file_shas=["a", "b"],
            matrix={},
            assignment=AssignmentResult(valid=True, mapping={0: 0, 1: 1}, raw=1.0),
            score=TechScore(technology=tech, status="scored", value=0.4),
            rung=3,
            used_onlist=True,
            equivalence_members=[],
            barcode_role_ids=["R1"],
            unfillable_role_ids=[],
            cdna_role_fillable=True,
            barcode_onlist_hit=hit,
            barcode_onlist_available=avail,
        )

    v2_spec = kb.load_spec("10x-3p-gex-v2")
    top = ev("10x-3p-gex-v2", hit=False)  # tops the tie, but its 737K list misses
    v3 = ev("10x-3p-gex-v3", hit=True)  # a sibling leaf whose 3M list hits
    # a barcoded leaf hit -> the data is barcoded -> abstain (do NOT refuse)
    assert _barcodeless_seated_blocker(top, v2_spec, [top, v3]) is None
    # nothing hit anywhere -> genuinely barcode-absent -> refuse
    blk = _barcodeless_seated_blocker(top, v2_spec, [top])
    assert blk is not None and blk.code == BlockerCode.BARCODE_READ_ABSENT


# ================================================================================================
# index reads — 10x I1/I2 set aside, not left unassigned
# ================================================================================================
#
# Technical sample-index reads (10x I1/I2): recognized, tagged ``index``, set aside from STARsolo.
#
# STARsolo consumes only the CB+UMI read and the cDNA read. An 8/10 bp sample-index file is a leftover
# of an *already-decided* run — it must be set aside, not left unassigned (which blocks a clean sample)
# and not forced into the layout (which would demand an index file of *every* sample). The resolver
# tags such a leftover ``index`` only when the bytes say it is index-sized; a longer stray leftover
# stays unassigned and still blocks loudly. These tests drive the real probe -> resolve -> fill ->
# validate -> compose path on synthetic reads, so the whole chain is exercised, not a hand-built model.


TECH = "10x-3p-gex-v3"


def _taxid(value: int) -> EvidencedTaxid:
    return EvidencedTaxid(value=value, basis="user_confirmed", rung=0)


def _reads(tmp_path: Path, *, extra: str | None) -> tuple[kb.Spec, OnlistRegistry, list[Path]]:
    """v3 R1(28)+R2 written as one run, optionally plus a third file.

    Files use ``fasterq-dump --include-technical``'s numeric mate suffixes (``_1`` / ``_2`` / ``_3``),
    which is how the real GSE229022 index reads arrive (``SRR..._1``, ``SRR..._2``) and what groups
    them into one run. ``extra="index"`` writes an 8 bp sample-index file; ``extra="cdna"`` writes a
    second cDNA-length file (a stray that must NOT be mistaken for an index read).
    """
    spec = kb.load_spec(TECH)
    reg = registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    paths: list[Path] = []
    for suffix, k in (("_1", "R1"), ("_2", "R2")):
        p = tmp_path / f"SRXidx{suffix}.fastq.gz"
        write_fastq_gz(p, reads[k])
        paths.append(p)
    if extra == "index":
        p = tmp_path / "SRXidx_3.fastq.gz"
        write_fastq_gz(p, [r[:8] for r in reads["R1"]])  # 8 bp, well under INDEX_MAX_LEN
        paths.append(p)
    elif extra == "cdna":
        p = tmp_path / "SRXidx_3.fastq.gz"
        write_fastq_gz(p, list(reads["R2"]))  # cDNA-length; a real dropped read, not an index
        paths.append(p)
    return spec, reg, paths


#: The synthetic index/stray file is always the third-mate file of the one run.
_INDEX_BASENAME = "SRXidx_3.fastq.gz"


# The three `_reads` variants are immutable products (nobody writes into the FASTQ dirs), so each is
# built once per module and shared across its consumers rather than rebuilt per test.
@pytest.fixture(scope="module")
def reads_plain(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[kb.Spec, OnlistRegistry, list[Path]]:
    return _reads(tmp_path_factory.mktemp("reads_plain"), extra=None)


@pytest.fixture(scope="module")
def reads_with_index(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[kb.Spec, OnlistRegistry, list[Path]]:
    return _reads(tmp_path_factory.mktemp("reads_index"), extra="index")


@pytest.fixture(scope="module")
def reads_with_cdna(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[kb.Spec, OnlistRegistry, list[Path]]:
    return _reads(tmp_path_factory.mktemp("reads_cdna"), extra="cdna")


def _filled_manifest(spec: kb.Spec, reg: OnlistRegistry, paths: list[Path]) -> m.DatasetManifest:
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    return fill_manifest(
        result=out.result,
        spec=spec,
        observations=[probe_file(p) for p in paths],
        registry=reg,
        experiment=ExperimentInputs(
            organism=_taxid(6239),
            accessions=["PRJNA1027859"],
            samples=[SampleGroup(sample_id="s1", file_uris=[p.name for p in paths])],
        ),
        seqforge_version=__version__,
    )


# ------------------------------------------------------------ the length gate itself


@pytest.mark.parametrize(
    ("leftover_len", "tagged"),
    [
        pytest.param(8, True, id="8bp-leftover-tagged-index"),
        pytest.param(26, False, id="26bp-above-the-gate-not-tagged"),
        pytest.param(90, False, id="90bp-cdna-length-not-tagged"),
    ],
)
def test_the_length_gate_tags_only_a_leftover_below_index_max_len(
    leftover_len: int, tagged: bool, tmp_path: Path
) -> None:
    """The index length gate, exercised at BOTH ends -- this folds in the old ``10 < INDEX_MAX_LEN < 26``
    shape assertion, behaviourally rather than by reading the constant.

    A short technical leftover (8 bp) is tagged ``INDEX_ROLE`` and set aside, and the CB/cDNA files keep
    the roles the optimizer gave them (the tag is additive). A 26 bp read (a v2 CB length, just above the
    gate) and a 90 bp cDNA-length read are NOT tagged -- a stray full-length read is a DROPPED read, not
    a technical index -- so they stay leftovers with no role at all, which ``validate`` then blocks. The
    26 bp row is what pins INDEX_MAX_LEN below a barcode read.
    """
    spec = kb.load_spec(TECH)
    reg = registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    paths: list[Path] = []
    for suffix, k in (("_1", "R1"), ("_2", "R2")):
        p = tmp_path / f"SRXidx{suffix}.fastq.gz"
        write_fastq_gz(p, reads[k])
        paths.append(p)
    leftover = tmp_path / _INDEX_BASENAME  # the third-mate file, designation "3" -> never absorbed
    rng = random.Random(0)
    write_fastq_gz(
        leftover, ["".join(rng.choice("ACGT") for _ in range(leftover_len)) for _ in range(600)]
    )
    paths.append(leftover)

    out = resolve_dataset(paths, registry=reg, use_cache=False)
    roles = index_tagged_roles(out.result.candidates[0], out.observations)
    index_sha = next(o.file.sha256 for o in out.observations if o.file.basename == _INDEX_BASENAME)
    if tagged:
        assert roles[index_sha] == INDEX_ROLE
        assert set(roles.values()) >= {INDEX_ROLE}
        assert any(
            role != INDEX_ROLE for role in roles.values()
        )  # real roles kept; tag is additive
    else:
        assert INDEX_ROLE not in roles.values()
        assert len(roles) < len(paths)  # the over-gate leftover stays with no role at all


# ------------------------------------------------------------ multi-run engine path


def test_the_multirun_role_map_tags_the_index_read(
    reads_with_index: tuple[kb.Spec, OnlistRegistry, list[Path]],
) -> None:
    spec, reg, paths = reads_with_index
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    role_of_sha = multi.role_of_sha()
    index_sha = next(
        o.file.sha256 for o in multi.observations if o.file.basename == _INDEX_BASENAME
    )
    assert role_of_sha[index_sha] == INDEX_ROLE
    assert not multi.blockers  # one chemistry, no disagreement


# ------------------------------------------------------------ validate + compose


def test_the_index_read_validates_clean_and_becomes_no_unit(
    reads_with_index: tuple[kb.Spec, OnlistRegistry, list[Path]],
) -> None:
    spec, reg, paths = reads_with_index
    manifest = _filled_manifest(spec, reg, paths)

    # The index file is in the inventory, tagged, and the pipeline reads never include it.
    index_items = [f for f in manifest.library.files if f.read_id == INDEX_ROLE]
    assert len(index_items) == 1
    assert index_items[0].basename == _INDEX_BASENAME

    report = validate_manifest(manifest)
    assert report.ok, [b.message for b in report.blockers]
    assert exit_code_for_report(report) == 0

    rows = core._units(manifest)
    assert all(row["read_id"] != INDEX_ROLE for row in rows)
    assert not any(r["path"].endswith("_3.fastq.gz") for r in rows)
    # The real reads still produce their units.
    assert {row["read_id"] for row in rows} == {f.read_id for f in manifest.library.files} - {
        INDEX_ROLE
    }


def test_a_stray_cdna_length_file_still_blocks(
    reads_with_cdna: tuple[kb.Spec, OnlistRegistry, list[Path]],
) -> None:
    spec, reg, paths = reads_with_cdna
    manifest = _filled_manifest(spec, reg, paths)

    assert not any(f.read_id == INDEX_ROLE for f in manifest.library.files)
    report = validate_manifest(manifest)
    assert not report.ok
    assert any(b.code == BlockerCode.NO_VALID_ROLE_ASSIGNMENT for b in report.blockers)


def test_a_clean_two_file_run_carries_no_index_role(
    reads_plain: tuple[kb.Spec, OnlistRegistry, list[Path]],
) -> None:
    """The no-leftover case is byte-identical to before: nothing is absorbed or tagged index.

    Folds in the single-lane-unaffected check — a normal two-file run has no surplus siblings, so the
    role map is exactly the two assigned roles and nothing is tagged — then carries it through to a
    clean-validating manifest.
    """
    spec, reg, paths = reads_plain
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    roles = index_tagged_roles(out.result.candidates[0], out.observations)
    assert len(roles) == 2  # exactly the two assigned roles, nothing absorbed or tagged
    assert INDEX_ROLE not in roles.values()

    manifest = _filled_manifest(spec, reg, paths)
    assert not any(f.read_id == INDEX_ROLE for f in manifest.library.files)
    report = validate_manifest(manifest)
    assert report.ok, [b.message for b in report.blockers]


# ------------------------------------------------------------ multi-lane / multi-flowcell absorption


def _multiflowcell_reads(
    tmp_path: Path,
    flowcells: tuple[str, ...] = ("HCL2YBBXY", "HCL2KBBXY"),
    lanes: int = 2,
) -> tuple[kb.Spec, OnlistRegistry, list[Path]]:
    """One 10x v3 accession sequenced across one or more FLOWCELLS, each with ``lanes`` lanes of
    R1(28)+R2(90)+I1(8), named the bcl2fastq way with the flowcell id in the stem
    (``SRR..._<FC>_S1_L001_R1_001.fastq.gz``). The shared SRA accession groups every file into ONE run
    -- the GSE208154 shape. The injective assignment fills each role once, leaving the other lanes and
    flowcells surplus; absorption rejoins them by read designation (R1/R2, which ignores the differing
    flowcell id) + length."""
    spec = kb.load_spec(TECH)
    reg = registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    rng = random.Random(0)
    paths: list[Path] = []
    for fc in flowcells:
        for lane in range(1, lanes + 1):
            for mate, k in (("R1", "R1"), ("R2", "R2"), ("I1", None)):
                p = tmp_path / f"SRR9000001_{fc}_S1_L{lane:03d}_{mate}_001.fastq.gz"
                if k is None:
                    write_fastq_gz(
                        p, ["".join(rng.choice("ACGT") for _ in range(8)) for _ in range(600)]
                    )
                else:
                    write_fastq_gz(p, list(reads[k]))
                paths.append(p)
    return spec, reg, paths


# One fact on two on-disk shapes: a single flowcell's lanes, and several flowcells whose differing
# flowcell id the read designation ignores. De-laning is gone; `_read_designation` + `_LANE_LEN_TOL` is
# the sole absorption mechanism, and both fixtures drive it identically.
_ABSORPTION_SHAPES = [
    pytest.param(("X",), 3, id="one-flowcell-three-lanes"),
    pytest.param(("A", "B"), 2, id="two-flowcells-two-lanes"),
]


@pytest.mark.parametrize(("flowcells", "lanes"), _ABSORPTION_SHAPES)
def test_a_multifile_run_absorbs_every_lane_and_flowcell_into_its_role(
    flowcells: tuple[str, ...], lanes: int, tmp_path: Path
) -> None:
    """GSE208154: one accession across N lanes and/or flowcells -> one run of N*(R1+R2+I1). The
    injective assignment fills each role ONCE, so the surplus lanes/flowcells were left unassigned and
    the run blocked with NO_VALID_ROLE_ASSIGNMENT. Now each surplus file rejoins its role (barcode/cDNA)
    by read designation + length, or is set aside (index), so the run resolves and every file is placed.
    """
    spec, reg, paths = _multiflowcell_reads(tmp_path, flowcells=flowcells, lanes=lanes)
    per_read = len(flowcells) * lanes  # (flowcell x lane) files carrying one read designation
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    assert not multi.blockers
    assert len(multi.runs) == 1  # one accession -> one run holding every file
    assert multi.runs[0].winner in {"10x-3p-gex-v3", "10x-3p-gex-v3.1"}

    role_of_sha = multi.role_of_sha()
    assert len(role_of_sha) == len(paths)  # every file placed -- nothing left to block
    counts = Counter(role_of_sha.values())
    assert counts[INDEX_ROLE] == per_read  # every (flowcell x lane) I1 set aside
    non_index = sorted(c for r, c in counts.items() if r != INDEX_ROLE)
    assert non_index == [
        per_read,
        per_read,
    ]  # barcode and cDNA each carry all (flowcell x lane) files


@pytest.mark.parametrize(("flowcells", "lanes"), _ABSORPTION_SHAPES)
def test_multifile_units_emit_every_file_and_exclude_index(
    flowcells: tuple[str, ...], lanes: int, tmp_path: Path
) -> None:
    """The point of absorption: units.tsv carries one row per (flowcell x lane) per counted role (so
    STARsolo comma-joins them), the index files are excluded, and the manifest validates clean."""
    spec, reg, paths = _multiflowcell_reads(tmp_path, flowcells=flowcells, lanes=lanes)
    per_read = len(flowcells) * lanes
    manifest = _filled_manifest(spec, reg, paths)

    assert all(f.read_id is not None for f in manifest.library.files)  # nothing unassigned
    assert sum(1 for f in manifest.library.files if f.read_id == INDEX_ROLE) == per_read
    report = validate_manifest(manifest)
    assert report.ok, [b.message for b in report.blockers]
    assert exit_code_for_report(report) == 0

    rows = core._units(manifest)
    assert all(row["read_id"] != INDEX_ROLE for row in rows)
    # (flowcell x lane) x 2 counted roles rows; each counted role appears once per file.
    assert len(rows) == per_read * 2
    assert set(Counter(r["read_id"] for r in rows).values()) == {per_read}
    assert len({r["run"] for r in rows}) == 1  # all one accession -> one run, comma-joined


# ------------------------------------------------------------ coverage over score (the real pathology)


#: A constant 25 bp head killing 5′ diversity in the cDNA role's ``distinct_ratio R2[0:20]`` window.
_FLAT_CDNA_HEAD = "ACGTACGTACGTACGTACGTACGTA"


def _barcode_role(spec: kb.Spec) -> str:
    return next(r.id for r in spec.reads if any(el.type == "barcode" for el in r.elements))


def _low_diversity_cdna_multilane(
    tmp_path: Path, lanes: int = 3
) -> tuple[kb.Spec, OnlistRegistry, list[Path]]:
    """One 10x v3 accession across ``lanes`` lanes, but every cDNA (R2) read shares a constant 5′ head
    -- the real GSE208154 shape the earlier synthetic missed. The cDNA role's only discriminator is
    ``distinct_ratio R2[0:20] high``; with a flat cDNA 5′ end, a whitelist-diverse 28 bp *barcode* read
    scores that discriminator ABOVE the 90 bp cDNA read. Score-max then seats a barcode read in the cDNA
    role and orphans every cDNA-length file (absorption cannot recover -- the cDNA rep is a barcode
    read), so the run blocks. The 90 bp reads are forbidden for the barcode role (dead zone), so the cDNA
    role is their only home; the coverage rule seats them there and the run resolves."""
    spec = kb.load_spec(TECH)
    reg = registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    flat_cdna = [
        _FLAT_CDNA_HEAD + r[len(_FLAT_CDNA_HEAD) :] for r in reads["R2"]
    ]  # length preserved
    rng = random.Random(0)
    paths: list[Path] = []
    for lane in range(1, lanes + 1):
        for mate, seqs in (("R1", reads["R1"]), ("R2", flat_cdna), ("I1", None)):
            p = tmp_path / f"SRR9000002_S1_L{lane:03d}_{mate}_001.fastq.gz"
            if seqs is None:
                write_fastq_gz(
                    p, ["".join(rng.choice("ACGT") for _ in range(8)) for _ in range(600)]
                )
            else:
                write_fastq_gz(p, list(seqs))
            paths.append(p)
    return spec, reg, paths


def test_a_low_diversity_cdna_multifile_run_resolves_by_coverage(tmp_path: Path) -> None:
    """The real GSE208154 pathology: low-diversity cDNA 5′ ends let a 28 bp barcode read out-score the
    90 bp cDNA read for the cDNA role. Score alone seats a barcode read in the cDNA role and orphans every
    cDNA-length file -> NO_VALID_ROLE_ASSIGNMENT. The coverage rule (the 90 bp reads are single-role
    eligible -- forbidden for barcode, so cDNA is their sole home) seats them and the run resolves.
    FAILS before the eligibility fix (cDNA-length files unassigned -> a blocker), passes after."""
    spec, reg, paths = _low_diversity_cdna_multilane(tmp_path, lanes=3)
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    assert not multi.blockers
    assert len(multi.runs) == 1
    assert multi.runs[0].winner in {"10x-3p-gex-v3", "10x-3p-gex-v3.1"}

    role_of_sha = multi.role_of_sha()
    assert len(role_of_sha) == len(paths)  # every file placed -- nothing orphaned
    counts = Counter(role_of_sha.values())
    assert counts[INDEX_ROLE] == 3  # the 3 I1 lanes set aside
    non_index = sorted(c for r, c in counts.items() if r != INDEX_ROLE)
    assert non_index == [3, 3]  # barcode and cDNA each carry all 3 lanes

    # The cDNA role is filled by the long cDNA reads, not the 28 bp barcode reads (the whole bug).
    obs = {o.file.sha256: o for o in multi.observations}
    cdna_role = next(r for r in counts if r != INDEX_ROLE and r != _barcode_role(spec))
    cdna_modes = {obs[s].read_length.mode for s, r in role_of_sha.items() if r == cdna_role}
    assert all(m > 28 for m in cdna_modes)  # long reads only -- no 28 bp barcode read slipped in


def test_read_designation_reads_the_mate_across_lanes_and_flowcells() -> None:
    """Absorption fuses a surplus file into a role by the read designation the sequencer wrote, so that
    token must be read precisely -- and identically across the lanes and flowcells of one accession,
    whose files differ only by a lane token and the flowcell id the designation ignores."""
    from seqforge.resolve.engine import _read_designation

    # The Illumina R/I token, read regardless of lane or flowcell id.
    assert _read_designation("SRR1_HCL2YBBXY_S1_L001_R1_001.fastq.gz") == "R1"
    assert _read_designation("SRR1_HCL2YBBXY_S1_L002_R2_001.fastq.gz") == "R2"
    assert _read_designation("SRR1_HCL2YBBXY_S1_L001_I1_001.fastq.gz") == "I1"
    # The whole point: two flowcells / two lanes of one read share ONE designation (de-laning could not
    # -- the flowcell id differs, so their de-laned names differed and the surplus stayed unassigned).
    assert _read_designation("SRR1_HCL2YBBXY_S1_L001_R1_001.fastq.gz") == _read_designation(
        "SRR1_HCL2KBBXY_S1_L005_R1_001.fastq.gz"
    )
    # fasterq-dump's numeric mate suffix (SRR..._1 / _2 / _3).
    assert _read_designation("SRXidx_1.fastq.gz") == "1"
    assert _read_designation("SRXidx_2.fastq.gz") == "2"
    assert _read_designation("SRXidx_3.fastq.gz") == "3"
    # R1 and R2 are DIFFERENT designations, so a barcode surplus never rejoins the cDNA role.
    assert _read_designation("x_R1.fastq.gz") != _read_designation("x_R2.fastq.gz")
    # A name that declares no mate designation -> None, so it is never absorbed (stays a blocker).
    assert _read_designation("sample_barcodes.fastq.gz") is None


# ================================================================================================
# validate — the checks Pydantic cannot do, and the advisory notes
# ================================================================================================
#
# ``validate_manifest`` — the checks Pydantic cannot do locally, and the advisory notes.
#
# Focus here: the low-confidence chemistry warning (#55). A winning chemistry composes identically
# whether its score is 0.95 or 0.44; nothing upstream gates on an *absolute* floor. The warning is
# non-blocking (exit 0) and rung-aware — an onlist-backed winner (rung 3) is trusted at a lower score
# than a geometry-only one (rung 2).


HEX64 = "a" * 64


def _hand_built_manifest(*, confidence: float | None, rung: int) -> m.DatasetManifest:
    """A minimal, structurally-valid single-cell manifest with a chosen chemistry confidence/rung.

    Everything but ``confidence``/``rung`` is fixed and clean, so the ONLY thing a validate can find
    is (or is not) the low-confidence note.
    """
    read_layout = m.ReadLayout(
        modality="rna",
        reads=[
            m.ReadDef(
                read_id="R1",
                strand="pos",
                min_len=28,
                max_len=28,
                elements=[
                    m.ReadElement(
                        role="CB",
                        region_type="barcode",
                        start=0,
                        length=16,
                        onlist_ref="3M-february-2018",
                    ),
                    m.ReadElement(role="UMI", region_type="umi", start=16, length=12),
                ],
            ),
            m.ReadDef(
                read_id="R2",
                strand="pos",
                min_len=25,
                max_len=91,
                elements=[m.ReadElement(role="cDNA", region_type="cdna", start=0)],
            ),
        ],
    )
    library = m.LibrarySection(
        chemistry=m.EvidencedChemistrySet(
            value=["10x-3p-gex-v3"],
            basis="observed",
            confidence=confidence,
            rung=rung,
        ),
        assay=[m.AssayLabel(chemistry="10x-3p-gex-v3", curie="EFO:0009922", name="10x 3' v3")],
        read_layout=read_layout,
        onlists=[
            m.Onlist(
                name="3M-february-2018",
                uri="onlists/3M-february-2018.txt",
                sha256=HEX64,
                length=16,
                orientation_hint="forward",
                n_entries=6_794_880,
            ),
        ],
        files=[
            m.FileInventoryItem(
                uri="reads/SRR000_1.fastq.gz",
                basename="SRR000_1.fastq.gz",
                sha256=HEX64,
                size_bytes=123,
                read_id="R1",
            ),
            m.FileInventoryItem(
                uri="reads/SRR000_2.fastq.gz",
                basename="SRR000_2.fastq.gz",
                sha256="b" * 64,
                size_bytes=456,
                read_id="R2",
            ),
        ],
    )
    experiment = m.ExperimentSection(
        organism=m.EvidencedTaxid(value=6239, basis="asserted", confidence=0.9, rung=0),
        accessions=m.EvidencedAccessionList(
            value=["PRJNA1027859"], basis="asserted", confidence=1.0, rung=0
        ),
        samples=[
            m.SampleGroup(
                sample_id="s1",
                file_uris=["reads/SRR000_1.fastq.gz", "reads/SRR000_2.fastq.gz"],
            )
        ],
    )
    return m.DatasetManifest(
        library=library,
        experiment=experiment,
        provenance=m.DatasetProvenance(
            dataset_hash=HEX64, kb_version="0.1", seqforge_version="2026.7.0"
        ),
    )


def _low_conf_warnings(report: m.ValidationReport) -> list[m.ValidationWarning]:
    return [w for w in report.warnings if w.code == "LOW_CONFIDENCE_CHEMISTRY"]


@pytest.mark.parametrize(
    ("confidence", "rung", "warns"),
    [
        pytest.param(0.98, 3, False, id="high-confidence-rung3"),
        pytest.param(0.44, 2, True, id="geometry-only-low-rung2"),
        pytest.param(0.60, 2, True, id="mid-score-geometry-warns"),
        pytest.param(0.60, 3, False, id="mid-score-onlist-trusted"),
        pytest.param(None, 2, False, id="null-confidence-never-warns"),
        pytest.param(1.0, 2, False, id="certain-rung2"),
        pytest.param(1.0, 3, False, id="certain-rung3"),
    ],
)
def test_the_low_confidence_chemistry_note_is_rung_aware(
    confidence: float | None, rung: int, warns: bool
) -> None:
    """One note, rung-aware, and never blocking. A winning chemistry composes identically whether its
    score is 0.95 or 0.44, so the low-confidence note is a non-blocking WARNING (exit 0), and it is
    trusted at a lower score when an onlist backed it (rung 3) than when only geometry did (rung 2): the
    same 0.60 warns at rung 2 and not at rung 3. ``confidence=None`` ("no judgement was weighed") has
    nothing to floor. The single warning row keeps the message/subject checks the old 0.44 test proved.
    """
    report = validate_manifest(_hand_built_manifest(confidence=confidence, rung=rung))
    notes = _low_conf_warnings(report)
    if warns:
        assert len(notes) == 1
        # 0.44 is the compile audit's PRJNA658829 parental-sample class (geometry-only, no onlist hit).
        assert f"{confidence:.2f}" in notes[0].message
        assert notes[0].subject.ref == "library.chemistry"
    else:
        assert notes == []
    # A warning never makes a manifest non-compilable: the note rides along at exit 0.
    assert report.ok is True
    assert exit_code_for_report(report) == 0


def test_the_onlist_floor_sits_below_the_geometry_floor() -> None:
    # Why the 0.60 row above flips on rung: an onlist positively participating (rung 3) is stronger
    # evidence than bare geometry (rung 2) at the same number, so its floor sits lower.
    assert _CHEM_CONF_FLOOR_ONLIST < 0.60 < _CHEM_CONF_FLOOR_GEOMETRY
