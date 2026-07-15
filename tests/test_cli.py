"""Smoke tests for the ``seqforge`` CLI (schema export is the first live verb)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from seqforge import __version__, kb
from seqforge.cli import app

runner = CliRunner()


def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    with gzip.open(path, "wt") as fh:
        for i, s in enumerate(seqs):
            fh.write(f"@SIM:{i}\n{s}\n+\n{'I' * len(s)}\n")


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


@pytest.mark.parametrize("model", ["DatasetManifest", "ProcessingManifest"])
def test_schema_export_each_manifest_is_valid_json(model: str) -> None:
    result = runner.invoke(app, ["schema", "export", model])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["title"] == model
    assert "$defs" in doc


def test_schema_export_unknown_model_exits_2() -> None:
    result = runner.invoke(app, ["schema", "export", "NopeModel"])
    assert result.exit_code == 2


def test_schema_export_all_covers_every_model() -> None:
    result = runner.invoke(app, ["schema", "export", "--all"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert {"DatasetManifest", "ProcessingManifest", "Observation"} <= set(doc)


def test_schema_list_lists_both_manifests() -> None:
    result = runner.invoke(app, ["schema", "list"])
    assert result.exit_code == 0
    assert "DatasetManifest" in result.stdout and "ProcessingManifest" in result.stdout


def test_kb_list_shows_10x() -> None:
    result = runner.invoke(app, ["kb", "list"])
    assert result.exit_code == 0
    assert "10x-3p-gex-v3" in result.stdout


def test_kb_show_unknown_exits_2() -> None:
    result = runner.invoke(app, ["kb", "show", "nope-tech"])
    assert result.exit_code == 2


def test_kb_lint_is_clean() -> None:
    result = runner.invoke(app, ["kb", "lint"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["ok"] is True


def test_kb_roundtrip_passes() -> None:
    result = runner.invoke(app, ["kb", "roundtrip", "10x-3p-gex-v3"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["passed"] is True


def test_io_onlist_list_shows_known_lists() -> None:
    result = runner.invoke(app, ["io", "onlist", "list"])
    assert result.exit_code == 0
    names = {o["name"] for o in json.loads(result.stdout)["onlists"]}
    assert "3M-february-2018" in names


def test_io_peek_not_implemented_exits_1() -> None:
    result = runner.invoke(app, ["io", "peek", "s3://bucket/reads.fastq.gz"])
    assert result.exit_code == 1


def test_manifest_fill_validate_hash_compose_spine(tmp_path: Path) -> None:
    """The whole deterministic spine, driven through the real CLI: probe->resolve->manifest->compose.

    Uses the no-barcode bulk branch so it needs no onlist: the default registry deliberately
    materializes no real whitelist (they are license-restricted), which is exactly why the 10x path
    refuses to compose until one is registered.
    """
    spec = kb.load_spec("bulk-rnaseq-pe")
    reads = kb.generate_reads(spec, n=600, seed=0)
    f1 = tmp_path / "s_R1.fastq.gz"
    f2 = tmp_path / "s_R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])

    filled = runner.invoke(
        app,
        [
            "manifest",
            "fill",
            str(f1),
            str(f2),
            "--organism",
            "559292",
            "-C",
            str(tmp_path),
        ],
    )
    assert filled.exit_code == 0, filled.stdout
    assert json.loads(filled.stdout)["report"]["ok"] is True
    # R7: manifest.yaml exists only because validate came back clean
    manifest_path = tmp_path / ".seqforge" / "manifest.yaml"
    assert manifest_path.is_file()
    assert not (tmp_path / ".seqforge" / "manifest.draft.yaml").exists()

    validated = runner.invoke(app, ["manifest", "validate", str(manifest_path)])
    assert validated.exit_code == 0
    assert json.loads(validated.stdout)["ok"] is True

    hashed = runner.invoke(app, ["manifest", "hash", str(manifest_path)])
    assert hashed.exit_code == 0
    assert json.loads(hashed.stdout)["matches"] is True

    # a genome has no safe default, and compose must refuse rather than guess one (R4/R12)
    naked = runner.invoke(app, ["compose", str(manifest_path), "-C", str(tmp_path)])
    assert naked.exit_code == 2
    assert "559292" in naked.stdout + naked.stderr, "the refusal must be actionable"

    proc_path = tmp_path / "processing.yaml"
    authored = runner.invoke(
        app,
        [
            "processing",
            "new",
            str(manifest_path),
            "--assembly",
            "sacCer3",
            "--annotation",
            "ensembl",
            "-o",
            str(proc_path),
        ],
    )
    assert authored.exit_code == 0, authored.stdout
    assert proc_path.is_file()
    assert (
        runner.invoke(
            app, ["processing", "validate", str(proc_path), "--dataset", str(manifest_path)]
        ).exit_code
        == 0
    )
    p_hashed = runner.invoke(app, ["processing", "hash", str(proc_path)])
    assert p_hashed.exit_code == 0
    assert json.loads(p_hashed.stdout)["matches"] is True

    composed = runner.invoke(
        app, ["compose", str(manifest_path), "--processing", str(proc_path), "-C", str(tmp_path)]
    )
    assert composed.exit_code == 0, composed.stdout
    doc = json.loads(composed.stdout)
    assert doc["modules"][0]["name"] == "map/star"
    assert doc["gate"]["params"] == "pass"
    assert doc["gate"]["e2e"] == "skip"  # honest: the count-matrix run needs STAR + liulab-genome
    assert (tmp_path / doc["config_path"]).is_file()
    assert (tmp_path / doc["units_path"]).is_file()
    # R7: whatever decided the run is recoverable from disk, bound to this dataset
    assert ((tmp_path / doc["config_path"]).parent / "processing.lock.yaml").is_file()


def test_harvest_normalize_and_verify_cli(tmp_path: Path) -> None:
    doc = tmp_path / "methods.txt"
    doc.write_text("Libraries were prepared with the Chromium Single Cell 3' v3 kit.")
    norm = runner.invoke(app, ["harvest", "normalize", str(doc), "-C", str(tmp_path)])
    assert norm.exit_code == 0
    row = json.loads(norm.stdout)["normalized"][0]
    assert row["source"] == "methods.txt" and row["n_chars"] > 0
    assert (tmp_path / ".seqforge" / "normalized" / f"{row['doc_sha256']}.txt").is_file()

    # one truthful draft + one with a real quote pinned to a wrong value
    drafts = tmp_path / "drafts.json"
    drafts.write_text(
        json.dumps(
            [
                {
                    "field": "library.chemistry",
                    "value": "10x-3p-gex-v3",
                    "span": {
                        "doc_sha256": row["doc_sha256"],
                        "quote": "Chromium Single Cell 3' v3",
                    },
                    "llm_confidence": 0.9,
                },
                {
                    "field": "experiment.organism",
                    "value": "Caenorhabditis elegans",
                    "span": {"doc_sha256": row["doc_sha256"], "quote": "Libraries were prepared"},
                    "llm_confidence": 0.9,
                },
            ]
        )
    )
    ver = runner.invoke(app, ["harvest", "verify", str(drafts), "--doc", str(doc)])
    assert ver.exit_code == 4  # a rejected claim needs a human, not a silent drop
    doc_out = json.loads(ver.stdout)
    assert doc_out["n_accepted"] == 1 and doc_out["n_rejected"] == 1
    assert doc_out["rejected"][0]["reason"] == "not_entailed"
    assert doc_out["assertions"][0]["span_verified"] is True


def test_compose_refuses_invalid_manifest(tmp_path: Path) -> None:
    bad = tmp_path / "nope.yaml"
    bad.write_text("library: {}\n")
    result = runner.invoke(app, ["compose", str(bad), "-C", str(tmp_path)])
    assert result.exit_code == 2  # unreadable/invalid manifest is a usage error, not a silent pass


def test_resolve_score_cli_decides_v3(tmp_path: Path) -> None:
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=800, seed=0)
    f1 = tmp_path / "R1.fastq.gz"
    f2 = tmp_path / "R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])
    result = runner.invoke(
        app, ["resolve", "score", str(f1), str(f2), "-C", str(tmp_path), "--no-cache"]
    )
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    # geometry-only path (default registry materializes no onlist) still decides v3 at rung 2
    assert doc["candidates"][0]["technology"] == "10x-3p-gex-v3"
    assert doc["rung_reached"] == 2


# --------------------------------------------------------------------------------------------
# `kb e2e-fit` -- the collector for a job-array cost sweep. The depths are independent, so they
# run as separate array tasks; this merges them. Its refusals are the interesting part, because
# a silent merge of incomparable runs would fit a clean line through meaningless points.
# --------------------------------------------------------------------------------------------

_FIVE = ["Gene", "GeneFull", "GeneFull_ExonOverIntron", "GeneFull_Ex50pAS", "Velocyto"]


def _cost_run(tmp_path: Path, name: str, depth: int, gb: float, **over: object) -> Path:
    run = {
        "assembly": "hg38",
        "annotation": "gencode_v50",
        "soloFeatures": _FIVE,
        "threads": 16,
        "n_cells": 5000,
        "points": [{"n_reads": depth, "star_peak_rss_gb": gb}],
        **over,
    }
    p = tmp_path / name
    p.write_text(json.dumps(run))
    return p


def test_e2e_fit_merges_array_tasks_into_one_line(tmp_path: Path) -> None:
    a = _cost_run(tmp_path, "a.json", 10_000_000, 34.57)
    b = _cost_run(tmp_path, "b.json", 40_000_000, 34.60)
    c = _cost_run(tmp_path, "c.json", 100_000_000, 34.66)
    result = runner.invoke(app, ["kb", "e2e-fit", str(a), str(b), str(c)])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["n_runs_merged"] == 3
    assert [p["n_reads"] for p in out["points"]] == [10_000_000, 40_000_000, 100_000_000]
    assert out["fit"]["ok"]
    # ~1 byte/read is the measured reality on hg38; the fit must reproduce it from these points
    assert 0 < out["fit"]["bytes_per_read"] < 5


def test_e2e_fit_refuses_runs_that_are_not_comparable(tmp_path: Path) -> None:
    """Peak RSS depends on soloFeatures, assembly, threads and cells -- so a merge across them lies.

    This is the same class as the resume guard's features check: the number is only meaningful
    alongside the configuration that produced it, and a line fitted through two configurations is a
    plausible-looking artefact of nothing.
    """
    a = _cost_run(tmp_path, "a.json", 10_000_000, 34.57)
    b = _cost_run(tmp_path, "b.json", 40_000_000, 31.10, soloFeatures=["Gene"])
    result = runner.invoke(app, ["kb", "e2e-fit", str(a), str(b)])
    assert result.exit_code == 3
    assert "incomparable" in result.output or "incomparable" in str(result.exception)


def test_e2e_fit_refuses_a_thread_count_mismatch(tmp_path: Path) -> None:
    a = _cost_run(tmp_path, "a.json", 10_000_000, 34.57)
    b = _cost_run(tmp_path, "b.json", 40_000_000, 36.90, threads=48)
    assert runner.invoke(app, ["kb", "e2e-fit", str(a), str(b)]).exit_code == 3


def test_e2e_fit_refuses_duplicate_depths(tmp_path: Path) -> None:
    """Two array tasks that measured the same depth is a bug in the array, not a second data point."""
    a = _cost_run(tmp_path, "a.json", 10_000_000, 34.57)
    b = _cost_run(tmp_path, "b.json", 10_000_000, 34.58)
    assert runner.invoke(app, ["kb", "e2e-fit", str(a), str(b)]).exit_code == 3


def test_e2e_fit_skips_a_failed_point(tmp_path: Path) -> None:
    """An OOM-ed top point must not enter the fit as a zero."""
    a = _cost_run(tmp_path, "a.json", 10_000_000, 34.57)
    b = tmp_path / "b.json"
    b.write_text(
        json.dumps(
            {
                "assembly": "hg38",
                "annotation": "gencode_v50",
                "soloFeatures": _FIVE,
                "threads": 16,
                "n_cells": 5000,
                "points": [
                    {"n_reads": 40_000_000, "star_peak_rss_gb": 34.60},
                    {"n_reads": 250_000_000, "failed": True, "error": "killed"},
                ],
            }
        )
    )
    result = runner.invoke(app, ["kb", "e2e-fit", str(a), str(b)])
    assert result.exit_code == 0, result.output
    assert [p["n_reads"] for p in json.loads(result.output)["points"]] == [10_000_000, 40_000_000]
