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
    manifest_path = tmp_path / "seqforge" / "manifest.yaml"
    assert manifest_path.is_file()
    assert not (tmp_path / "seqforge" / "manifest.draft.yaml").exists()

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


def test_run_compiles_the_whole_spine_in_one_pass(tmp_path: Path) -> None:
    """`seqforge run` chains probe->resolve->manifest->processing->compose and emits one summary.

    The same deterministic spine as `test_manifest_fill_validate_hash_compose_spine`, but driven
    through the single verb an agent (or `claude -p`) actually calls. `--no-llm` keeps it network- and
    provider-free, which is the branch CI can run; the bulk path needs no onlist. It proves the one
    thing a chain of separately-green stages does not: that the composition itself produces every
    artifact.
    """
    spec = kb.load_spec("bulk-rnaseq-pe")
    reads = kb.generate_reads(spec, n=600, seed=0)
    f1 = tmp_path / "s_R1.fastq.gz"
    f2 = tmp_path / "s_R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])

    result = runner.invoke(
        app,
        [
            "run",
            str(f1),
            str(f2),
            "--organism",
            "559292",
            "--assembly",
            "sacCer3",
            "--annotation",
            "ensembl",
            "--no-llm",
            "--fastq-dir",
            str(tmp_path),
            "-C",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    summary = json.loads(result.stdout)
    assert summary["ok"] is True
    # one summary, keyed by stage — records was skipped (no accession), harvest skipped (--no-llm)
    assert set(summary["stages"]) == {"manifest", "processing", "compose"}
    assert summary["stages"]["compose"]["gate"]["params"] == "pass"

    manifest_path = tmp_path / "seqforge" / "manifest.yaml"
    assert manifest_path.is_file() and summary["manifest"] == str(manifest_path)
    assert (tmp_path / "seqforge" / "processing.yaml").is_file()
    # the deliverable, and it is where the summary says it is
    assert (tmp_path / summary["snakefile"]).is_file()
    # R13: the recipe file did not perturb the dataset — validate still comes back clean by name
    assert runner.invoke(app, ["manifest", "validate", str(manifest_path)]).exit_code == 0


def test_run_refuses_without_a_genome(tmp_path: Path) -> None:
    """The one real decision has no safe default: no --assembly, no instruction -> exit 2, not a guess.

    And the manifest is still written — the IR is what the data IS, independent of what you do with
    it — so the refusal is precisely at the `processing` stage, with an actionable message (R4/R12).
    """
    spec = kb.load_spec("bulk-rnaseq-pe")
    reads = kb.generate_reads(spec, n=600, seed=0)
    f1 = tmp_path / "s_R1.fastq.gz"
    f2 = tmp_path / "s_R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])

    result = runner.invoke(
        app, ["run", str(f1), str(f2), "--organism", "559292", "--no-llm", "-C", str(tmp_path)]
    )
    assert result.exit_code == 2, result.stdout
    summary = json.loads(result.stdout)
    assert summary["ok"] is False
    assert set(summary["stages"]) == {"manifest", "processing"}  # stopped exactly at the genome
    assert "559292" in summary["stages"]["processing"]["error"], "the refusal must be actionable"
    assert (tmp_path / "seqforge" / "manifest.yaml").is_file()  # the IR still landed


def test_run_steps_past_a_rejected_reference_claim_but_halts_on_a_conflict() -> None:
    """`run` must complete one-pass on a real paper whose prose the span-checker cannot fully entail.

    A rejected reference claim (the pilot's "Single Cell 3' v3.1" prose the entailment could not tie to
    a KB id) never enters the manifest and the bytes decide chemistry, so it is surfaced, not fatal. A
    conflict (instructions disagreeing) and an unavailable provider still stop the pass.
    """
    from seqforge.cli import _harvest_halts_run

    assert _harvest_halts_run({"n_accepted": 9}, 0) is False  # clean
    assert (
        _harvest_halts_run({"rejected": [{"field": "library.chemistry"}], "conflicts": []}, 4)
        is False
    )
    assert _harvest_halts_run({"conflicts": [{"field": "processing.genome.assembly"}]}, 4) is True
    assert _harvest_halts_run({"error": "no_provider"}, 1) is True  # the LLM stage could not run
    assert _harvest_halts_run("some string payload", 4) is True  # not a dict -> cannot clear it


def test_parallel_probe_does_not_change_the_dataset_hash(tmp_path: Path) -> None:
    """`--cpus` is a speed knob, never a truth knob (R3): cores are not a budget any more than the

    wall clock is. Probing the files across a process pool must produce the byte-identical manifest a
    sequential probe does — so the content hash is the same whether you used 1 core or 4.

    The FASTQs are written ONCE and reused across both runs: ``gzip`` stamps the current mtime into its
    header, so regenerating a "logically identical" file yields different bytes and a different (and
    correct) content hash. Same input bytes in, same hash out is precisely the property under test.
    """
    spec = kb.load_spec("bulk-rnaseq-pe")
    reads = kb.generate_reads(spec, n=600, seed=0)
    data = tmp_path / "data"
    data.mkdir()
    f1 = data / "s_R1.fastq.gz"
    f2 = data / "s_R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])

    def hash_with(cpus: int, ws: Path) -> str:
        ws.mkdir()
        result = runner.invoke(
            app,
            [
                "run",
                str(f1),
                str(f2),
                "--organism",
                "559292",
                "--assembly",
                "sacCer3",
                "--annotation",
                "ensembl",
                "--no-llm",
                "--fastq-dir",
                str(data),
                "--cpus",
                str(cpus),
                "-C",
                str(ws),
            ],
        )
        assert result.exit_code == 0, result.stdout
        import yaml as _yaml

        manifest = _yaml.safe_load((ws / "seqforge" / "manifest.yaml").read_text())
        return manifest["provenance"]["dataset_hash"]

    assert hash_with(1, tmp_path / "seq") == hash_with(4, tmp_path / "par")


def test_harvest_normalize_and_verify_cli(tmp_path: Path) -> None:
    doc = tmp_path / "methods.txt"
    doc.write_text("Libraries were prepared with the Chromium Single Cell 3' v3 kit.")
    norm = runner.invoke(app, ["harvest", "normalize", str(doc), "-C", str(tmp_path)])
    assert norm.exit_code == 0
    row = json.loads(norm.stdout)["normalized"][0]
    assert row["source"] == "methods.txt" and row["n_chars"] > 0
    # A readable name, not a bare 64-hex one. The hash stays -- it is the identity, and two documents
    # can share a name -- but `seqforge/documents/` used to be a directory in which nothing said
    # which file was the paper.
    written = tmp_path / "seqforge" / "documents" / f"methods-{row['doc_sha256'][:12]}.txt"
    assert written.is_file()
    assert row["path"] == str(written.relative_to(tmp_path))
    # ...and a human-supplied document is about the whole dataset. It is the only honest reading of
    # "here is the paper", and it is what stops its sample claims being recorded as declarations.
    assert row["scope"] == "dataset" and row["subject"] is None

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
    assert doc["candidates"][0]["technology"] == "10x-3p-gex-v3"
    # Rung 3, because the real `3M-february-2018` now SHIPS and the onlist check actually runs. This
    # asserted `== 2` and was right at the time: every registry entry carried `uri=""`/`sha256=""`,
    # so nothing could be materialized and the ladder stopped at geometry. These reads are synthetic,
    # so their random barcodes miss the real whitelist and rung 3 contributes no support -- v3 still
    # wins on geometry. What changed is that the rung is REACHED, which is the difference between a
    # 10x dataset composing and `compose` exiting 3.
    assert doc["rung_reached"] == 3


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


def test_a_verbs_stdout_is_json_and_its_progress_goes_to_stderr(capsys: object) -> None:
    """R8: the CLI emits JSON on stdout. Progress narration is not a result and must not go there.

    The incident: `kb e2e-cost` runs for tens of minutes, so it narrates -- via `print()`, which put
    `[cost] ...` lines straight through the middle of its own JSON. The first real run produced
    `cost-hg38-2681399.json` that `json.load` rejects at line 1 column 2, and `kb e2e-fit` (which
    reads exactly those files) would have choked on every one. Only `cost_sweep.partial.json`, written
    separately because R7 says disk is state, made the three measured points recoverable.

    Pinned on the primitive rather than the verb because the verb needs STAR and a 30 GB index; the
    property under test is one line of plumbing and does not.
    """
    import sys as _sys

    from seqforge.e2e import _progress

    _progress("hello")
    captured = _sys.stdout, _sys.stderr  # noqa: F841  (capsys owns the streams)
    out = capsys.readouterr()  # type: ignore[attr-defined]
    assert out.out == "", "progress on stdout would corrupt the JSON result"
    assert "[cost] hello" in out.err


def test_no_module_under_src_prints_to_stdout() -> None:
    """A bare print() in a library module lands in whatever a verb is emitting. Derive, don't declare.

    This is the general form of the bug above, and the reason it is a scan rather than a note in a
    docstring: the next `print()` someone adds for debugging is silent and corrupts a result the same
    way. `typer.echo` is how a verb speaks; everything else goes to stderr.
    """
    import ast
    from pathlib import Path as _P

    offenders = []
    for py in sorted((_P(__file__).parent.parent / "src" / "seqforge").rglob("*.py")):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"):
                continue
            keywords = {k.arg for k in node.keywords}
            if "file" not in keywords:
                offenders.append(f"{py.name}:{node.lineno}")
    assert not offenders, (
        f"print() to stdout in a library module: {offenders}. stdout carries the JSON result (R8); "
        f"send narration to stderr with file=sys.stderr."
    )


def test_manifest_fill_on_a_six_run_dataset_keeps_every_file(tmp_path: Path) -> None:
    """The pilot's shape, through the real CLI: 12 files, 6 runs, 6 samples, 0 files dropped.

    Before this, `manifest fill` handed every file to one `resolve_dataset` call, which does one
    global role assignment: two files got roles, TEN got `read_id=None`, `compose._units` skipped
    them in silence, and `validate` said ok. A clean, content-addressed manifest recording a wrong
    answer, exit 0 -- on a dataset that is 6 runs, which the pilot's is.

    Bulk paired-end so no onlist is needed; the multi-run machinery is chemistry-blind.

    **The files are one directory per accession, because that is the pilot's ACTUAL shape** -- it is
    how `fasterq-dump` wrote them. This test laid them out flat while claiming to be the pilot's
    shape, and the gap was not cosmetic: a flat directory is its own dataset root, so every URI is a
    basename and the one code path that has to agree about URIs was never exercised. On the real
    dataset `manifest fill` refused its own manifest with six referential-integrity Blockers, because
    `cli.py` built `SampleGroup.file_uris` from basenames while `fill_manifest` built relative paths.
    Every fixture in this repo was flat; that is why nothing saw it.
    """
    spec = kb.load_spec("bulk-rnaseq-pe")
    accessions = [f"SRR2871655{i}" for i in range(3, 9)]
    paths: list[str] = []
    for i, acc in enumerate(accessions):
        reads = kb.generate_reads(spec, n=400, seed=i)
        run_dir = tmp_path / "data" / f"SRX2428313{i}"
        run_dir.mkdir(parents=True)
        for mate, role in (("1", "R1"), ("2", "R2")):
            p = run_dir / f"{acc}_{mate}.fastq.gz"
            _write_fastq_gz(p, reads[role])
            paths.append(str(p))

    filled = runner.invoke(
        app, ["manifest", "fill", *paths, "--organism", "6239", "-C", str(tmp_path)]
    )
    assert filled.exit_code == 0, filled.stdout
    assert json.loads(filled.stdout)["report"]["ok"] is True

    import yaml

    manifest = yaml.safe_load((tmp_path / "seqforge" / "manifest.yaml").read_text())
    files = manifest["library"]["files"]
    assert len(files) == 12, "every input file is in the inventory"
    assert all(f["read_id"] is not None for f in files), "and every one of them has a role"

    samples = manifest["experiment"]["samples"]
    assert [s["sample_id"] for s in samples] == sorted(accessions), "one sample per RUN"
    assert sum(len(s["file_uris"]) for s in samples) == 12

    # Every sample URI is an inventory URI. `validate`'s referential-integrity check says this too,
    # and said it on arc -- but only once the layout had subdirectories for the two builders to
    # disagree about. Asserted here so the disagreement is a unit-test failure, not a cluster one.
    assert {u for s in samples for u in s["file_uris"]} == {f["uri"] for f in files}
    # ...and the URIs kept the directory, which is what makes `compose --fastq-dir <root>` resolve
    assert all(f["uri"].startswith("SRX2428313") for f in files), (
        f"the per-accession directory was dropped: {sorted(f['uri'] for f in files)[:2]}"
    )

    # the roles came from BYTES: _1/_2 is fasterq-dump's dump order and means nothing
    roles = {f["basename"]: f["read_id"] for f in files}
    assert set(roles.values()) == {"R1", "R2"}
    # ...and each file states its role once, as a string. It used to carry a full Evidenced envelope
    # holding a copy of the chemistry's confidence -- twelve copies of one number, because the role
    # assignment and the chemistry are two halves of ONE joint optimization. That number lives on
    # `library.chemistry`, which is the decision it is about.
    assert manifest["library"]["chemistry"]["confidence"] is not None
    assert all(isinstance(f["read_id"], str) for f in files)

    # No accession was given, so nothing was fetched and no sample carries a fact. That is not a
    # degraded mode -- most sequencing data never had an accession -- and it must not be a refusal.
    assert all(s["attributes"] == {} for s in samples)
    assert all(s["accession"] is None for s in samples)
    assert manifest["experiment"]["study"] is None


def test_processing_new_takes_an_assembly_from_a_verified_instruction(tmp_path: Path) -> None:
    """The last mile of a join that already existed and was unreachable.

    `resolve_processing` has always implemented flag > instruction > policy, and its PolicyError even
    says "Pass --assembly/--annotation, **or name an assembly in an --instruction document**". That
    branch could not be reached: `--assembly` was a REQUIRED option, and no production caller ever
    passed `instructions=`. So the instructable surface was real in the API and absent from the CLI.

    Note where the model is and is not. It FOUND `processing.genome.assembly: ce11` in a document the
    user handed us with `--instruction`, and code verified the quote greps back and entails the value
    (R5). Applying precedence is code, here. No new LLM authority -- which is the whole reason the
    instructable path is allowed to exist.
    """
    import yaml as _yaml

    from seqforge.models.assertion import Assertion, ExtractorProvenance, SourceSpan

    spec = kb.load_spec("bulk-rnaseq-pe")
    reads = kb.generate_reads(spec, n=400, seed=0)
    for k in ("R1", "R2"):
        _write_fastq_gz(tmp_path / f"s_{k}.fastq.gz", reads[k])
    filled = runner.invoke(
        app,
        [
            "manifest",
            "fill",
            str(tmp_path / "s_R1.fastq.gz"),
            str(tmp_path / "s_R2.fastq.gz"),
            "--organism",
            "Caenorhabditis elegans",
            "--offline",
            "-C",
            str(tmp_path),
        ],
    )
    assert filled.exit_code == 0, filled.stdout
    manifest_path = tmp_path / "seqforge" / "manifest.yaml"

    # the organism arrived as a NAME and was resolved to a taxid by code, not retyped by a human
    manifest = _yaml.safe_load(manifest_path.read_text())
    assert manifest["experiment"]["organism"]["value"] == 6239

    doc_sha = "a" * 64
    span = SourceSpan(
        doc_sha256=doc_sha, quote="align this dataset against ce11", char_start=0, char_end=31
    )
    assertions = {
        "instruction_docs": [doc_sha],
        "assertions": [
            Assertion(
                id="a1",
                field="processing.genome.assembly",
                value="ce11",
                span=span,
                span_verified=True,
                entailment_ok=True,
                llm_confidence=0.9,
                extractor=ExtractorProvenance(model_id="test/fixture", prompt_version="v1"),
            ).model_dump(mode="json")
        ],
    }
    apath = tmp_path / "assertions.json"
    apath.write_text(json.dumps(assertions))

    out = tmp_path / "processing.yaml"
    made = runner.invoke(
        app,
        [
            "processing",
            "new",
            str(manifest_path),
            "--annotation",
            "WS298",
            "--assertions",
            str(apath),
            "-o",
            str(out),
        ],
    )
    assert made.exit_code == 0, made.stdout
    doc = _yaml.safe_load(out.read_text())
    genome = doc["processing"]["genome"]
    assert genome["value"]["assembly"] == "ce11", "the instruction never reached the manifest"
    # basis records WHO DECIDED: a document the user authored for seqforge is the user talking (§7)
    assert genome["basis"] == "user_confirmed"


def test_processing_new_refuses_a_pre_2026_7_assertions_file(tmp_path: Path) -> None:
    """A bare list cannot say which documents were --instruction, and only those may set processing.*.

    Silently treating every assertion as instructable would turn a downloaded GEO description into a
    path to --soloStrand: prompt injection from a database field into an aligner (R14). Refuse.
    """
    spec = kb.load_spec("bulk-rnaseq-pe")
    reads = kb.generate_reads(spec, n=400, seed=0)
    for k in ("R1", "R2"):
        _write_fastq_gz(tmp_path / f"s_{k}.fastq.gz", reads[k])
    runner.invoke(
        app,
        [
            "manifest",
            "fill",
            str(tmp_path / "s_R1.fastq.gz"),
            str(tmp_path / "s_R2.fastq.gz"),
            "--organism",
            "6239",
            "-C",
            str(tmp_path),
        ],
    )
    old = tmp_path / "old.json"
    old.write_text(json.dumps([{"field": "processing.genome.assembly", "value": "ce11"}]))
    res = runner.invoke(
        app,
        [
            "processing",
            "new",
            str(tmp_path / "seqforge" / "manifest.yaml"),
            "--annotation",
            "WS298",
            "--assertions",
            str(old),
        ],
    )
    assert res.exit_code == 2
    assert "harvest extract" in res.stdout + str(res.stderr or "")
