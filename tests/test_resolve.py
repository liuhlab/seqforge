"""Tests for ``seqforge.resolve`` — the byte resolver: which chemistry, and which file is which read.

One file per package, so an agent editing ``resolve/`` knows which file to run. This was six files
(``test_geometry``/``test_negatives``/``test_over_length``/``test_index_reads``/``test_validate``
beside ``test_resolve``) named after the issue that added them, so "where are the tests for
``escalate.py``?" had no answer short of grepping all six.

The other resolver — records + prose, "which sample is each file" — is ``test_records.py``. The two
are siblings, and they part on disagreement (see ``resolve/records.py``); they do not share a file.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from math import inf
from pathlib import Path
from typing import Any

import pytest

from conftest import KbProbes, registry_for, write_fastq_gz
from seqforge import __version__, kb
from seqforge import models as m
from seqforge.compose import core
from seqforge.io import DEFAULT_REGISTRY, OnlistRegistry, PackedOnlist
from seqforge.kb.generate import write_fastq_gz as write_reproducible_fastq_gz
from seqforge.kb.schema import Read, Spec
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
from seqforge.models.resolve import TechScore
from seqforge.probe import probe_file
from seqforge.resolve import Hypothesis, resolve_dataset, resolve_runs, role_of_sha_for
from seqforge.resolve.assign import AssignmentResult, _brute, _hungarian_assign, best_assignment
from seqforge.resolve.confuse import accepts_at_rungs_0_2
from seqforge.resolve.engine import INDEX_MAX_LEN, index_tagged_roles
from seqforge.resolve.escalate import escalate
from seqforge.resolve.geometry import (
    geometry_could_accept,
    length_feasible,
)
from seqforge.resolve.scoring import Cell, TechEvaluation, build_tech_evaluation
from seqforge.resolve.window import WindowProbe

# ================================================================================================
# resolve — assignment, the matrix, the §12 fixture, escalation
# ================================================================================================
#
# Tests for ``resolve``: assignment, matrix JSON-safety, the §12 fixture, and escalation branches.


def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    # Delegate to the REPRODUCIBLE writer (mtime=0, filename="") so synthetic reads are byte-stable
    # and content-addressed ids don't drift across runs — not the shared `conftest.write_fastq_gz`,
    # which is `gzip.open`-shaped and stamps the current mtime. Same `@SIM:i` record format.
    write_reproducible_fastq_gz(path, seqs)


# ---------- parallel per-run scoring (winner-invariance vs serial) ----------
def test_resolve_runs_parallel_matches_serial(tmp_path: Path) -> None:
    """``resolve_runs(cpus>1)`` forks per-run scoring with a copy-on-write-shared warm registry; the
    result must be byte-identical to the serial (``cpus=1``) resolution -- the same runs in the same
    order, each with the same winner and role assignment. Cores fold into no decision. Three distinct
    accessions -> three runs, which forces the fork path (it needs more than one run)."""
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=800, seed=1)
    reg = registry_for(
        spec, seed=1
    )  # whitelist matches the reads' seed so the barcodes actually hit
    paths: list[Path] = []
    for acc in ("SRR7000001", "SRR7000002", "SRR7000003"):
        for suffix, k in (("_1", "R1"), ("_2", "R2")):
            p = tmp_path / f"{acc}{suffix}.fastq.gz"
            _write_fastq_gz(p, reads[k])
            paths.append(p)

    def digest(multi: object) -> list[object]:
        return [
            (
                r.run_id,
                r.output.result.candidates[0].technology,
                tuple(sorted(r.output.result.candidates[0].role_assignment.assignment.items())),
            )
            for r in multi.runs  # type: ignore[attr-defined]
        ]

    serial = resolve_runs(paths, registry=reg, use_cache=False, cpus=1)
    parallel = resolve_runs(paths, registry=reg, use_cache=False, cpus=3)
    assert len(serial.runs) == 3
    assert digest(serial) == digest(parallel)  # same decision, run for run


def _run_digest(multi: object) -> list[object]:
    return [
        (
            r.run_id,
            r.output.result.candidates[0].technology,
            tuple(sorted(r.output.result.candidates[0].role_assignment.assignment.items())),
        )
        for r in multi.runs  # type: ignore[attr-defined]
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

    monkeypatch.setattr(remote.requests, "get", fake_get)

    probed: dict[str, object] = {}
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
    reg = registry_for(v3)
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
    reg = registry_for(v3)
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
        assert length_feasible(spec, kb_probes[tech_id]), (
            f"{tech_id} must accept its own synthetic reads"
        )


@pytest.mark.xdist_group("kb-probes")
def test_geometry_could_accept_is_necessary_for_rung02_acceptance(kb_probes: KbProbes) -> None:
    """The guarantee the confusability guard and the runtime shortlist rely on.

    If ``a`` accepts ``b``'s reads at rungs 0-2 (a real confusable), then ``a`` must be geometry-feasible
    against ``b``'s reads — so skipping geometry-infeasible pairs can never miss a real confusable. The
    founding cross-geometry collision (``bulk-rnaseq-pe`` accepts ``splitseq``) must therefore still be
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
            if accepts_at_rungs_0_2(specs[a], kb_probes[b]):
                assert geometry_could_accept(specs[a], kb_probes[b]), (
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
        wps = kb_probes[tech]
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


def test_a_pretrimmed_technical_read_blocks(tmp_path: Path) -> None:
    """The quiet negative: it scores like a clean dataset, so nothing else catches it.

    `read_length_compatible` gates on the read-length **mode**, so a barcode read that is mostly
    28 bp with a trimmed tail passes every geometry check and wins its candidate outright. Downstream
    never looks again: STARsolo reads the barcode from a fixed offset, and on a shifted read that
    offset is an arbitrary 16-mer — it matches no whitelist, the cell is dropped, the matrix is thin,
    and STAR exits 0. That is the silent-garbage path §5 was written to close, and
    `PRETRIMMED_VARIABLE_LENGTH` sat declared-but-never-emitted while it stayed open.

    Note only R1 is trimmed here. R2 is cDNA — open-ended and *legitimately* variable — which is
    exactly why this cannot be "variable length is bad": it has to be variable length on a read the
    chemistry declares fixed.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=3000, seed=0)
    # cutadapt ran over the barcode read: most reads survive at 28 bp, a minority come back short.
    trimmed = [s[:20] if i % 20 == 0 else s for i, s in enumerate(reads["R1"])]
    assert len({len(s) for s in trimmed}) == 2  # the fixture really is variable...
    assert max(set(trimmed), key=len).__len__() == 28  # ...with the mode still at the declared 28

    f1 = tmp_path / "sample_R1.fastq.gz"
    f2 = tmp_path / "sample_R2.fastq.gz"
    write_fastq_gz(f1, trimmed)
    write_fastq_gz(f2, reads["R2"])

    out = resolve_dataset([f1, f2], registry=registry_for(spec), use_cache=False)

    assert out.exit_code() == 3
    assert not out.result.candidates  # refused: no manifest may be filled over this
    blk = next(b for b in out.result.blockers if b.code == BlockerCode.PRETRIMMED_VARIABLE_LENGTH)
    assert blk.subject.ref == "sample_R1.fastq.gz"  # names the trimmed file, not the clean cDNA
    assert "sra-pub-src" in blk.remedy  # actionable: where the untrimmed original lives


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
    assert not same_family(specs, "10x-3p-gex-v2", "bulk-rnaseq-pe")
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

    class _Top:  # the guard reads only `.tech`
        tech = "bulk-rnaseq-pe"

    class _TopSingleCell:
        tech = "10x-3p-gex-v3"

    conflict = _single_cell_collapse_conflict(
        "10x-3p-gex-v2", "harvest", 0.9, _Top(), specs["bulk-rnaseq-pe"], [], specs
    )
    assert conflict is not None
    assert conflict.kind == "observed_vs_asserted" and conflict.status == "open"
    assert {p.value: p.basis for p in conflict.positions} == {
        "10x-3p-gex-v2": "asserted",
        "bulk-rnaseq-pe": "observed",
    }
    # negatives — no collapse to surface:
    # a bulk chemistry was asserted and bulk won (agreement)
    assert (
        _single_cell_collapse_conflict(
            "bulk-rnaseq-pe", "harvest", 0.9, _Top(), specs["bulk-rnaseq-pe"], [], specs
        )
        is None
    )
    # the winner is itself barcoded (single-cell won or tied)
    assert (
        _single_cell_collapse_conflict(
            "10x-3p-gex-v2", "harvest", 0.9, _TopSingleCell(), specs["10x-3p-gex-v3"], [], specs
        )
        is None
    )
    # no hypothesis at all
    assert (
        _single_cell_collapse_conflict(None, None, 0.8, _Top(), specs["bulk-rnaseq-pe"], [], specs)
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
    exit 4, identical winner `bulk-rnaseq-pe`, identical `conflict-single-cell-collapsed-to-bulk`,
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
    assert out.result.candidates[0].technology == "bulk-rnaseq-pe"
    assert out.exit_code() == 4
    assert any(c.id == "conflict-single-cell-collapsed-to-bulk" for c in out.result.conflicts), [
        c.id for c in out.result.conflicts
    ]


def test_bulk_asserted_single_cell_observed_guard_is_structural() -> None:
    """The MIRROR of the collapse guard: an asserted bulk chemistry + a barcoded single-cell winner is a
    cross-family contradiction that must surface. Same error class, the other direction."""
    from seqforge.resolve.escalate import _bulk_asserted_single_cell_observed

    specs = kb.load_all_specs()

    class _TopSingleCell:  # the guard reads only `.tech`
        tech = "10x-3p-gex-v3"

    class _TopBulk:
        tech = "bulk-rnaseq-pe"

    conflict = _bulk_asserted_single_cell_observed(
        "bulk-rnaseq-pe", "harvest", 0.9, _TopSingleCell(), specs["10x-3p-gex-v3"], [], specs
    )
    assert conflict is not None
    assert conflict.id == "conflict-bulk-asserted-single-cell-observed"
    assert conflict.kind == "observed_vs_asserted" and conflict.status == "open"
    assert {p.value: p.basis for p in conflict.positions} == {
        "bulk-rnaseq-pe": "asserted",
        "10x-3p-gex-v3": "observed",
    }
    # negatives — no reverse conflict to surface:
    # a single-cell chemistry was asserted -> that is the FORWARD collapse guard's job, not this one
    assert (
        _bulk_asserted_single_cell_observed(
            "10x-3p-gex-v2", "harvest", 0.9, _TopSingleCell(), specs["10x-3p-gex-v3"], [], specs
        )
        is None
    )
    # the winner is itself bulk (agreement)
    assert (
        _bulk_asserted_single_cell_observed(
            "bulk-rnaseq-pe", "harvest", 0.9, _TopBulk(), specs["bulk-rnaseq-pe"], [], specs
        )
        is None
    )
    # no hypothesis at all
    assert (
        _bulk_asserted_single_cell_observed(
            None, None, 0.8, _TopSingleCell(), specs["10x-3p-gex-v3"], [], specs
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
        hypothesis=Hypothesis(value="bulk-rnaseq-pe", id="meta-1", confidence=0.9),
        use_cache=False,
    )
    assert out.result.candidates[0].technology == "10x-3p-gex-v3"
    assert out.exit_code() == 4
    assert any(
        c.id == "conflict-bulk-asserted-single-cell-observed" for c in out.result.conflicts
    ), [c.id for c in out.result.conflicts]


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
    # v3 and v3.1 are §12 twins recorded together; either is the right answer, v2 is not.
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
    assert winner.technology == "bulk-rnaseq-pe"


def test_genuine_bulk_still_resolves_to_bulk_with_barcode_whitelists_registered(
    tmp_path: Path,
) -> None:
    """Safety guard for the dominance anchor (a barcoded candidate that positively matched a whitelist
    is not shadowed by the barcodeless fallback): it must NEVER hijack genuine bulk. Canonical ~100 bp
    paired cDNA reads with NO barcode content, resolved with the v2 whitelist registered, must still
    resolve to bulk-rnaseq-pe. v2 IS consulted here (it reaches rung 3, and its barcode read even passes
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
    assert out.result.candidates[0].technology == "bulk-rnaseq-pe", [
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


def _filled_manifest(tmp_path: Path, spec: kb.Spec, reg: OnlistRegistry, paths: list[Path]):
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


def test_index_tagged_roles_tags_a_short_leftover_and_keeps_the_real_roles(tmp_path: Path) -> None:
    spec, reg, paths = _reads(tmp_path, extra="index")
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    winner = out.result.candidates[0]
    roles = index_tagged_roles(winner, out.observations)

    index_sha = next(o.file.sha256 for o in out.observations if o.file.basename == _INDEX_BASENAME)
    assert roles[index_sha] == INDEX_ROLE
    # The CB and cDNA files keep the roles the optimizer gave them — the index tag is additive.
    assert set(roles.values()) >= {INDEX_ROLE}
    assert any(role != INDEX_ROLE for role in roles.values())


def test_a_cdna_length_leftover_is_never_tagged_index(tmp_path: Path) -> None:
    """The gate is a safety: a stray full-length read is a DROPPED read, not a technical index."""
    spec, reg, paths = _reads(tmp_path, extra="cdna")
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    winner = out.result.candidates[0]
    roles = index_tagged_roles(winner, out.observations)
    assert INDEX_ROLE not in roles.values()
    # Its length is above the gate, so it stays a leftover with no role at all (validate will block).
    assert len(roles) < len(paths)


def test_the_gate_sits_below_a_barcode_read_and_above_an_index(tmp_path: Path) -> None:
    # A documentation guard: 8/10 bp index reads pass, a 26 bp v2 / 28 bp v3 CB read never would.
    assert 10 < INDEX_MAX_LEN < 26


# ------------------------------------------------------------ multi-run engine path


def test_the_multirun_role_map_tags_the_index_read(tmp_path: Path) -> None:
    spec, reg, paths = _reads(tmp_path, extra="index")
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    role_of_sha = multi.role_of_sha()
    index_sha = next(
        o.file.sha256 for o in multi.observations if o.file.basename == _INDEX_BASENAME
    )
    assert role_of_sha[index_sha] == INDEX_ROLE
    assert not multi.blockers  # one chemistry, no disagreement


# ------------------------------------------------------------ validate + compose


def test_the_index_read_validates_clean_and_becomes_no_unit(tmp_path: Path) -> None:
    spec, reg, paths = _reads(tmp_path, extra="index")
    manifest = _filled_manifest(tmp_path, spec, reg, paths)

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


def test_a_stray_cdna_length_file_still_blocks(tmp_path: Path) -> None:
    spec, reg, paths = _reads(tmp_path, extra="cdna")
    manifest = _filled_manifest(tmp_path, spec, reg, paths)

    assert not any(f.read_id == INDEX_ROLE for f in manifest.library.files)
    report = validate_manifest(manifest)
    assert not report.ok
    assert any(b.code == BlockerCode.NO_VALID_ROLE_ASSIGNMENT for b in report.blockers)


def test_a_clean_two_file_run_carries_no_index_role(tmp_path: Path) -> None:
    """The no-leftover case is byte-identical to before: nothing is ever tagged index."""
    spec, reg, paths = _reads(tmp_path, extra=None)
    manifest = _filled_manifest(tmp_path, spec, reg, paths)
    assert not any(f.read_id == INDEX_ROLE for f in manifest.library.files)
    report = validate_manifest(manifest)
    assert report.ok, [b.message for b in report.blockers]


# ------------------------------------------------------------ multi-lane surplus absorption


def _multilane_reads(tmp_path: Path, lanes: int = 3) -> tuple[kb.Spec, OnlistRegistry, list[Path]]:
    """One 10x v3 accession sequenced across ``lanes`` lanes: each lane an R1(28)+R2(90)+I1(8), named
    the bcl2fastq way (``SRR..._S1_L001_R1_001.fastq.gz``). The shared SRA accession groups every lane
    into ONE run -- the GSE208154 shape."""
    spec = kb.load_spec(TECH)
    reg = registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    rng = random.Random(0)
    paths: list[Path] = []
    for lane in range(1, lanes + 1):
        for mate, k in (("R1", "R1"), ("R2", "R2"), ("I1", None)):
            p = tmp_path / f"SRR9000001_S1_L{lane:03d}_{mate}_001.fastq.gz"
            if k is None:
                write_fastq_gz(
                    p, ["".join(rng.choice("ACGT") for _ in range(8)) for _ in range(600)]
                )
            else:
                write_fastq_gz(p, list(reads[k]))
            paths.append(p)
    return spec, reg, paths


def test_a_multilane_run_absorbs_every_lane_into_its_role(tmp_path: Path) -> None:
    """GSE208154: one accession across N lanes -> one run of N*(R1+R2+I1). The injective assignment
    fills each role ONCE, so the surplus lanes were left unassigned and the run blocked with
    NO_VALID_ROLE_ASSIGNMENT. Now each surplus lane rejoins its role (barcode/cDNA) or is set aside
    (index), so the run resolves and every file is placed."""
    spec, reg, paths = _multilane_reads(tmp_path, lanes=3)
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    assert not multi.blockers
    assert len(multi.runs) == 1  # one accession -> one run holding all 9 files
    assert multi.runs[0].winner in {"10x-3p-gex-v3", "10x-3p-gex-v3.1"}

    role_of_sha = multi.role_of_sha()
    assert len(role_of_sha) == len(paths)  # every file placed -- nothing left to block
    counts = Counter(role_of_sha.values())
    assert counts[INDEX_ROLE] == 3  # the 3 I1 lanes set aside
    non_index = sorted(c for r, c in counts.items() if r != INDEX_ROLE)
    assert non_index == [3, 3]  # barcode and cDNA each carry all 3 lanes


def test_multilane_units_emit_every_lane_and_exclude_index(tmp_path: Path) -> None:
    """The point of absorption: units.tsv carries one row per lane per counted role (so STARsolo
    comma-joins them), the index lanes are excluded, and the manifest validates clean."""
    spec, reg, paths = _multilane_reads(tmp_path, lanes=3)
    manifest = _filled_manifest(tmp_path, spec, reg, paths)

    assert all(f.read_id is not None for f in manifest.library.files)  # nothing unassigned
    assert sum(1 for f in manifest.library.files if f.read_id == INDEX_ROLE) == 3
    report = validate_manifest(manifest)
    assert report.ok, [b.message for b in report.blockers]
    assert exit_code_for_report(report) == 0

    rows = core._units(manifest)
    assert all(row["read_id"] != INDEX_ROLE for row in rows)
    # 3 lanes x 2 counted roles = 6 rows; each counted role appears once per lane.
    assert len(rows) == 6
    assert set(Counter(r["read_id"] for r in rows).values()) == {3}
    # Every lane is the SAME run, so `fastqs(sample, role)` collects and comma-joins them by path.
    assert len({r["run"] for r in rows}) == 1


# ------------------------------------------------------------ multi-flowcell surplus absorption


def _multiflowcell_reads(
    tmp_path: Path,
    flowcells: tuple[str, ...] = ("HCL2YBBXY", "HCL2KBBXY"),
    lanes: int = 2,
) -> tuple[kb.Spec, OnlistRegistry, list[Path]]:
    """One 10x v3 accession sequenced across several FLOWCELLS, each with ``lanes`` lanes of
    R1(28)+R2(90)+I1(8), named the bcl2fastq way with the flowcell id in the stem
    (``SRR..._<FC>_S1_L001_R1_001.fastq.gz``). The shared SRA accession groups every file into ONE run
    -- the GSE208154 shape (11 SRR runs x 2 flowcells x 8 lanes x {R1,R2,I1}). Because the flowcell id
    differs between files, de-laning left the cross-flowcell surplus with a different identity than its
    role representative, so it stayed unassigned and the run blocked; designation matching fuses them."""
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


def test_a_multiflowcell_run_absorbs_every_flowcell_into_its_role(tmp_path: Path) -> None:
    """GSE208154: one accession sequenced across 2 flowcells x 2 lanes -> one run of 4*(R1+R2+I1). The
    files differ by flowcell id, so 2026.7.4's de-lane equality could not fuse the cross-flowcell surplus
    (its de-laned name still carried the differing flowcell id) and the run blocked. Matching by read
    designation (R1/R2) + length fuses them, so every file is placed and the run resolves. FAILS before
    the fix (some files unassigned -> a blocker), passes after."""
    spec, reg, paths = _multiflowcell_reads(tmp_path, flowcells=("HCL2YBBXY", "HCL2KBBXY"), lanes=2)
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    assert not multi.blockers
    assert len(multi.runs) == 1  # one accession -> one run holding all 12 files
    assert multi.runs[0].winner in {"10x-3p-gex-v3", "10x-3p-gex-v3.1"}

    role_of_sha = multi.role_of_sha()
    assert len(role_of_sha) == len(paths)  # every file placed across BOTH flowcells
    counts = Counter(role_of_sha.values())
    assert counts[INDEX_ROLE] == 4  # 2 flowcells x 2 lanes of I1 set aside
    non_index = sorted(c for r, c in counts.items() if r != INDEX_ROLE)
    assert non_index == [4, 4]  # barcode and cDNA each carry all 4 (flowcell x lane) files


def test_multiflowcell_units_emit_every_file_and_validate_clean(tmp_path: Path) -> None:
    """End to end for the flowcell shape: every file placed -> the manifest validates clean and
    units.tsv carries one row per (flowcell x lane) per counted role (STARsolo comma-joins them),
    with the index files excluded."""
    spec, reg, paths = _multiflowcell_reads(tmp_path, flowcells=("HCL2YBBXY", "HCL2KBBXY"), lanes=2)
    manifest = _filled_manifest(tmp_path, spec, reg, paths)

    assert all(f.read_id is not None for f in manifest.library.files)  # nothing unassigned
    assert sum(1 for f in manifest.library.files if f.read_id == INDEX_ROLE) == 4
    report = validate_manifest(manifest)
    assert report.ok, [b.message for b in report.blockers]
    assert exit_code_for_report(report) == 0

    rows = core._units(manifest)
    assert all(row["read_id"] != INDEX_ROLE for row in rows)
    # 4 (flowcell x lane) x 2 counted roles = 8 rows; each counted role appears once per file.
    assert len(rows) == 8
    assert set(Counter(r["read_id"] for r in rows).values()) == {4}
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


def test_a_clean_single_lane_run_is_unaffected_by_absorption(tmp_path: Path) -> None:
    """Regression: the absorption only fires on surplus lane siblings. A normal single-lane run (one
    R1 + one R2, no leftovers) is byte-identical to before -- no role is duplicated, nothing tagged."""
    spec, reg, paths = _reads(tmp_path, extra=None)
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    roles = index_tagged_roles(out.result.candidates[0], out.observations)
    assert len(roles) == 2  # exactly the two assigned roles, nothing absorbed or tagged
    assert INDEX_ROLE not in roles.values()


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


def test_a_high_confidence_winner_raises_no_low_confidence_note() -> None:
    report = validate_manifest(_hand_built_manifest(confidence=0.98, rung=3))
    assert _low_conf_warnings(report) == []
    assert report.ok is True
    assert exit_code_for_report(report) == 0


def test_a_geometry_only_lonely_low_winner_is_flagged_but_still_compiles() -> None:
    # 0.44 is the compile audit's PRJNA658829 parental-sample class: geometry-only (rung 2), no onlist
    # hit. It must warn — and, crucially, must NOT block: the note rides along at exit 0.
    report = validate_manifest(_hand_built_manifest(confidence=0.44, rung=2))
    notes = _low_conf_warnings(report)
    assert len(notes) == 1
    assert "0.44" in notes[0].message
    assert notes[0].subject.ref == "library.chemistry"
    assert report.ok is True  # a warning never makes a manifest non-compilable
    assert exit_code_for_report(report) == 0


def test_the_floor_is_rung_aware_an_onlist_winner_is_trusted_lower() -> None:
    # A score that trips the geometry (rung 2) floor but clears the onlist (rung 3) floor: the same
    # 0.60 warns without an onlist and does not warn with one, because an onlist positively
    # participating is stronger evidence than bare geometry at the same number.
    assert _CHEM_CONF_FLOOR_ONLIST < 0.60 < _CHEM_CONF_FLOOR_GEOMETRY
    assert _low_conf_warnings(validate_manifest(_hand_built_manifest(confidence=0.60, rung=2)))
    assert not _low_conf_warnings(validate_manifest(_hand_built_manifest(confidence=0.60, rung=3)))


def test_a_null_confidence_never_warns() -> None:
    # `confidence=None` is a legal "no judgement was weighed" value; there is nothing to floor.
    report = validate_manifest(_hand_built_manifest(confidence=None, rung=2))
    assert _low_conf_warnings(report) == []
    assert report.ok is True


@pytest.mark.parametrize("rung", [2, 3])
def test_a_clean_certain_winner_never_warns_at_either_rung(rung: int) -> None:
    assert not _low_conf_warnings(
        validate_manifest(_hand_built_manifest(confidence=1.0, rung=rung))
    )
