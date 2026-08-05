"""Smoke tests for the ``seqforge`` CLI (schema export is the first live verb)."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from conftest import SrcTrees, SynthDataset, real_cbs, write_fastq_gz
from seqforge import __version__, kb
from seqforge.cli import app

runner = CliRunner()


#: ``(argv, exit_code, substrings that must appear in stdout)``.
#:
#: One one-liner function per case, each invoking a verb and pinning its exit code. The CLI is the
#: API, so what is under test is the SURFACE — and a surface reads as a table. A verb that starts
#: refusing, or stops naming what it lists, goes red as a named case.
#:
#: The verbs whose claim is structural rather than textual (``schema export --all`` covering every
#: model, ``kb lint``/``kb roundtrip`` returning ``ok``/``passed``) keep their own functions below:
#: a substring is not that claim.
CLI_SURFACE = [
    pytest.param(["version"], 0, (__version__,), id="version-prints-the-version"),
    pytest.param(["schema", "export", "NopeModel"], 2, (), id="schema-export-unknown-model-exits-2"),
    pytest.param(
        ["schema", "list"], 0, ("DatasetManifest", "ProcessingManifest"),
        id="schema-list-lists-both-manifests",
    ),
    pytest.param(["kb", "list"], 0, ("10x-3p-gex-v3",), id="kb-list-shows-10x"),
    pytest.param(["kb", "show", "nope-tech"], 2, (), id="kb-show-unknown-exits-2"),
    pytest.param(
        ["io", "onlist", "list"], 0, ("3M-february-2018",), id="io-onlist-list-shows-known-lists"
    ),
    pytest.param(
        ["io", "peek", "s3://bucket/reads.fastq.gz"], 1, (), id="io-peek-not-implemented-exits-1"
    ),
    # STAR now writes the BAM already coordinate-sorted (the only output it will put CB/UB in), so
    # `io cram` runs no sort and has no memory budget to split across threads. A caller still passing
    # the old knob — a stale rule, a script somebody kept — must be REFUSED at the gate: a sort budget
    # silently accepted and ignored reads as a tuned pipeline and is a lie about what ran.
    pytest.param(
        ["io", "cram", "--bam", "in.bam", "--assembly", "hg38", "--out", "out.cram",
         "--sort-mem-mb", "8000"], 2, (), id="io-cram-has-no-sort-memory-knob",
    ),
    # Each cell's BAM arrives with the sample id that names its h5ad row, so a bare path is a bad
    # invocation — refused before the assembly is looked up, since a typo should not first cost a
    # genome resolution that may not be possible on this host at all.
    pytest.param(
        ["io", "umi-count", "/x/cell.bam", "--assembly", "mm10", "--annotation", "gencode_vM23",
         "--out", "plate.h5ad"], 2, (), id="io-umi-count-refuses-a-bam-with-no-sample-id",
    ),
]  # fmt: skip


@pytest.mark.parametrize("argv, exit_code, contains", CLI_SURFACE)
def test_the_cli_surface_exits_and_answers_as_documented(
    argv: list[str], exit_code: int, contains: tuple[str, ...]
) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == exit_code, result.stdout
    for needle in contains:
        assert needle in result.stdout


def test_io_umi_count_finds_the_annotation_database_beside_the_gtf_liulab_genome_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verb's whole job: marshal arguments, resolve the annotation, and answer on stdout.

    liulab-genome registers an annotation as `<name>.gtf` with the gffutils `<name>.db` it builds
    from it in the same directory, and exposes only the first — so the verb derives the second, and
    this is the test that goes red if that layout ever moves. `Genome` is stubbed because resolving
    a real assembly needs a genome store this box may not have; what is under test is the
    derivation and the wiring, not liulab-genome.
    """
    import anndata as ad
    import genome as liulab_genome
    import gffutils
    import pysam

    gtf = tmp_path / "synthetic.gtf"
    gtf.write_text(
        'chr1\tsynthetic\tgene\t1\t1000\t.\t+\t.\tgene_id "GENE_A";\n'
        'chr1\tsynthetic\texon\t101\t200\t.\t+\t.\tgene_id "GENE_A"; transcript_id "GENE_A.1";\n'
    )
    built = gffutils.create_db(
        str(gtf),
        str(gtf.with_suffix(".db")),
        keep_order=True,
        merge_strategy="create_unique",
        sort_attribute_values=True,
        disable_infer_genes=True,
        disable_infer_transcripts=True,
    )
    built.conn.close()

    header = pysam.AlignmentHeader.from_dict(
        {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": [{"SN": "chr1", "LN": 10000}]}
    )
    record = pysam.AlignedSegment(header)
    record.query_name = "one_read"
    record.query_sequence = "A" * 20
    record.query_qualities = pysam.qualitystring_to_array("I" * 20)
    record.reference_id = 0
    record.reference_start = 120  # inside the exon, 0-based
    record.mapping_quality = 255
    record.cigarstring = "20M"
    record.set_tags([("NH", 1, "i"), ("UB", "AAAAAAAA", "Z")])
    bam = tmp_path / "cell.bam"
    with pysam.AlignmentFile(str(bam), "wb", header=header) as out:
        out.write(record)

    class _StubGenome:
        def __init__(self, assembly: str) -> None:
            self.assembly = assembly

        def get_gtf_path(self, name: str) -> Path:
            return gtf

    monkeypatch.setattr(liulab_genome, "Genome", _StubGenome)

    written = tmp_path / "plate.h5ad"
    result = runner.invoke(
        app,
        ["io", "umi-count", f"cell_a={bam}", "--assembly", "mm10",
         "--annotation", "synthetic", "--out", str(written)],
    )  # fmt: skip

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["written"] == str(written)
    adata = ad.read_h5ad(written)
    assert list(adata.obs_names) == ["cell_a"]
    # `X` is declared as a union that includes a lazy on-disk dataset; on an object just read back
    # it is the sparse matrix that was written, and only the cast says so to the checker.
    counts = cast("Any", adata.X)
    assert int(counts[0, adata.var_names.get_loc("GENE_A")]) == 1


def test_schema_export_is_valid_json_per_model_and_over_all() -> None:
    """`schema export` is valid JSON per model AND under `--all`: one surface, one test.

    Per model, each manifest exports a document titled by its own name and carrying `$defs`; `--all`
    emits every model in one document (a split that exported only one would silently lose coverage).
    """
    for model in ("DatasetManifest", "ProcessingManifest"):
        result = runner.invoke(app, ["schema", "export", model])
        assert result.exit_code == 0
        doc = json.loads(result.stdout)
        assert doc["title"] == model
        assert "$defs" in doc

    allresult = runner.invoke(app, ["schema", "export", "--all"])
    assert allresult.exit_code == 0
    alldoc = json.loads(allresult.stdout)
    assert {"DatasetManifest", "ProcessingManifest", "Observation"} <= set(alldoc)


def test_kb_lint_is_clean() -> None:
    result = runner.invoke(app, ["kb", "lint"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["ok"] is True


def test_kb_roundtrip_passes() -> None:
    result = runner.invoke(app, ["kb", "roundtrip", "10x-3p-gex-v3"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["passed"] is True


def test_manifest_fill_validate_hash_compose_spine(tmp_path: Path) -> None:
    """The whole deterministic spine, driven through the real CLI: probe->resolve->manifest->compose.

    Uses the no-barcode bulk branch so it needs no onlist: the default registry deliberately
    materializes no real whitelist (they are license-restricted), which is exactly why the 10x path
    refuses to compose until one is registered.
    """
    spec = kb.load_spec("bulk-rnaseq")
    reads = kb.generate_reads(spec, n=600, seed=0)
    f1 = tmp_path / "s_R1.fastq.gz"
    f2 = tmp_path / "s_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])

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
    # manifest.yaml exists only because validate came back clean
    manifest_path = tmp_path / "seqforge" / "manifest.yaml"
    assert manifest_path.is_file()
    assert not (tmp_path / "seqforge" / "manifest.draft.yaml").exists()

    validated = runner.invoke(app, ["manifest", "validate", str(manifest_path)])
    assert validated.exit_code == 0
    assert json.loads(validated.stdout)["ok"] is True

    hashed = runner.invoke(app, ["manifest", "hash", str(manifest_path)])
    assert hashed.exit_code == 0
    assert json.loads(hashed.stdout)["matches"] is True

    # a genome has no safe default, and compose must refuse rather than guess one
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
    # whatever decided the run is recoverable from disk, bound to this dataset
    assert ((tmp_path / doc["config_path"]).parent / "processing.lock.yaml").is_file()


def test_run_compiles_the_whole_spine_in_one_pass(tmp_path: Path) -> None:
    """`seqforge run` chains probe->resolve->manifest->processing->compose and emits one summary.

    The same deterministic spine as `test_manifest_fill_validate_hash_compose_spine`, but driven
    through the single verb an agent (or `claude -p`) actually calls. `--no-llm` keeps it network- and
    provider-free, which is the branch CI can run; the bulk path needs no onlist. It proves the one
    thing a chain of separately-green stages does not: that the composition itself produces every
    artifact.
    """
    spec = kb.load_spec("bulk-rnaseq")
    reads = kb.generate_reads(spec, n=600, seed=0)
    f1 = tmp_path / "s_R1.fastq.gz"
    f2 = tmp_path / "s_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])

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
    # one summary, keyed by stage — records was skipped (no accession), harvest skipped (--no-llm);
    # `project` is the manifest-derived sample table + assay index, always written; `report` is the
    # best-effort HTML glance layer, emitted after compose and never able to fail the compile.
    assert set(summary["stages"]) == {"manifest", "processing", "compose", "project", "report"}
    assert summary["stages"]["compose"]["gate"]["params"] == "pass"

    manifest_path = tmp_path / "seqforge" / "manifest.yaml"
    assert manifest_path.is_file() and summary["manifest"] == str(manifest_path)
    assert (tmp_path / "seqforge" / "processing.yaml").is_file()
    assert (tmp_path / "seqforge" / "sample_metadata.tsv").is_file()  # the one-study view
    # the deliverable, and it is where the summary says it is
    assert (tmp_path / summary["snakefile"]).is_file()
    # the recipe file did not perturb the dataset — validate still comes back clean by name
    assert runner.invoke(app, ["manifest", "validate", str(manifest_path)]).exit_code == 0


def test_common_fastq_root_mirrors_dataset_uris(tmp_path: Path) -> None:
    from seqforge.cli.run import _common_fastq_root

    sub = tmp_path / "SRX1"
    sub.mkdir()
    (sub / "a_1.fastq.gz").write_bytes(b"")
    (sub / "a_2.fastq.gz").write_bytes(b"")
    # all reads in one accession subdir -> that subdir IS the common root (URIs become basenames)
    assert _common_fastq_root([sub / "a_1.fastq.gz", sub / "a_2.fastq.gz"]) == sub.resolve()
    # reads split across two subdirs -> the parent is the root (URIs keep the SRX prefix)
    sub2 = tmp_path / "SRX2"
    sub2.mkdir()
    (sub2 / "b_1.fastq.gz").write_bytes(b"")
    assert _common_fastq_root([sub / "a_1.fastq.gz", sub2 / "b_1.fastq.gz"]) == tmp_path.resolve()


def test_run_defaults_fastq_dir_to_the_common_root_for_a_subdir_layout(tmp_path: Path) -> None:
    """A dataset whose reads sit in one accession subdir must compile without the caller knowing the
    `--fastq-dir`-is-the-common-root contract: `run` defaults it to the computed root, so units.tsv
    points at the real files instead of the dataset dir (the GSE274290 wiring-gate failure)."""
    spec = kb.load_spec("bulk-rnaseq")
    reads = kb.generate_reads(spec, n=600, seed=0)
    sub = tmp_path / "SRX9"
    sub.mkdir()
    f1, f2 = sub / "s_R1.fastq.gz", sub / "s_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])

    result = runner.invoke(
        app,
        ["run", str(f1), str(f2), "--organism", "559292", "--assembly", "sacCer3",
         "--annotation", "ensembl", "--no-llm", "-C", str(tmp_path)],
    )  # fmt: skip
    assert result.exit_code == 0, result.stdout
    units = next(iter(tmp_path.rglob("units.tsv")))
    rows = [ln for ln in units.read_text().splitlines()[1:] if ln.strip()]
    assert rows, "units.tsv has a row per read"
    for ln in rows:
        path = Path(ln.split("\t")[-1])
        assert path.parent == sub.resolve(), f"units path lost the subdir: {path}"
        assert path.is_file(), f"units path does not exist: {path}"


def test_run_refuses_without_a_genome(tmp_path: Path) -> None:
    """The one real decision has no safe default: no --assembly, no instruction -> exit 2, not a guess.

    And the manifest is still written — the IR is what the data IS, independent of what you do with
    it — so the refusal is precisely at the `processing` stage, with an actionable message.
    """
    spec = kb.load_spec("bulk-rnaseq")
    reads = kb.generate_reads(spec, n=600, seed=0)
    f1 = tmp_path / "s_R1.fastq.gz"
    f2 = tmp_path / "s_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])

    result = runner.invoke(
        app, ["run", str(f1), str(f2), "--organism", "559292", "--no-llm", "-C", str(tmp_path)]
    )
    assert result.exit_code == 2, result.stdout
    summary = json.loads(result.stdout)
    assert summary["ok"] is False
    # Stopped at the genome (no Snakefile), but the manifest-derived sample table still lands: it is
    # what the data IS, independent of the genome, which is a choice. The HTML report lands too — it
    # renders the honest ir-ready state (a manifest, no pipeline), which is exactly what happened.
    assert set(summary["stages"]) == {"manifest", "processing", "project", "report"}
    assert "559292" in summary["stages"]["processing"]["error"], "the refusal must be actionable"
    assert (tmp_path / "seqforge" / "manifest.yaml").is_file()  # the IR still landed
    assert (tmp_path / "seqforge" / "sample_metadata.tsv").is_file()


def test_run_steps_past_a_rejected_reference_claim_but_halts_on_a_conflict() -> None:
    """`run` must complete one-pass on a real paper whose prose the span-checker cannot fully entail.

    A rejected reference claim (the pilot's "Single Cell 3' v3.1" prose the entailment could not tie to
    a KB id) never enters the manifest and the bytes decide chemistry, so it is surfaced, not fatal. A
    conflict (instructions disagreeing) and an unavailable provider still stop the pass.
    """
    from seqforge.cli.run import _harvest_halts_run

    assert _harvest_halts_run({"n_accepted": 9}, 0) is False  # clean
    assert (
        _harvest_halts_run({"rejected": [{"field": "library.chemistry"}], "conflicts": []}, 4)
        is False
    )
    assert _harvest_halts_run({"conflicts": [{"field": "processing.genome.assembly"}]}, 4) is True
    assert _harvest_halts_run({"error": "no_provider"}, 1) is True  # the LLM stage could not run
    assert _harvest_halts_run("some string payload", 4) is True  # not a dict -> cannot clear it


def test_parallel_probe_does_not_change_the_dataset_hash(tmp_path: Path) -> None:
    """`--cpus` is a speed knob, never a truth knob: cores are not a budget any more than the

    wall clock is. Probing the files across a process pool must produce the byte-identical manifest a
    sequential probe does — so the content hash is the same whether you used 1 core or 4.

    The FASTQs are written ONCE and reused across both runs: ``gzip`` stamps the current mtime into its
    header, so regenerating a "logically identical" file yields different bytes and a different (and
    correct) content hash. Same input bytes in, same hash out is precisely the property under test.
    """
    spec = kb.load_spec("bulk-rnaseq")
    reads = kb.generate_reads(spec, n=600, seed=0)
    data = tmp_path / "data"
    data.mkdir()
    f1 = data / "s_R1.fastq.gz"
    f2 = data / "s_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])

    def hash_with(cpus: int, ws: Path) -> str:
        ws.mkdir()
        # `manifest fill` is the stage that probes; the recipe stages (processing/compose/project/
        # report) add no probe and cannot perturb `dataset_hash` -- that invariance is owned by
        # `test_manifest.py`. So `fill` with a matched `--cpus` is the whole of what this asserts.
        result = runner.invoke(
            app,
            [
                "manifest",
                "fill",
                str(f1),
                str(f2),
                "--organism",
                "559292",
                "--cpus",
                str(cpus),
                "-C",
                str(ws),
            ],
        )
        assert result.exit_code == 0, result.stdout
        import yaml as _yaml

        manifest = _yaml.safe_load((ws / "seqforge" / "manifest.yaml").read_text())
        dataset_hash = manifest["provenance"]["dataset_hash"]
        assert isinstance(dataset_hash, str), f"dataset_hash is not a string: {dataset_hash!r}"
        return dataset_hash

    assert hash_with(1, tmp_path / "seq") == hash_with(4, tmp_path / "par")


def test_harvest_normalize_and_verify_cli(tmp_path: Path) -> None:
    doc = tmp_path / "methods.txt"
    doc.write_text("Libraries were prepared with the Chromium Single Cell 3' v3 kit.")
    norm = runner.invoke(app, ["harvest", "normalize", str(doc), "-C", str(tmp_path)])
    assert norm.exit_code == 0
    row = json.loads(norm.stdout)["normalized"][0]
    assert row["source"] == "methods.txt" and row["n_chars"] > 0
    # A readable name, not a bare 64-hex one. The hash stays -- it is the identity, and two documents
    # can share a name -- but `seqforge/records/documents/` used to be a directory in which nothing
    # said which file was the paper. It lives under records/ because that is what a document is
    # rendered from.
    written = (
        tmp_path / "seqforge" / "records" / "documents" / f"methods-{row['doc_sha256'][:12]}.txt"
    )
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
    # Give R1 REAL 3M-february-2018 barcodes: the CLI drives the shipped whitelist, and F1b now refuses
    # a barcoded winner whose read hits no whitelist -- random synthetic CBs would (correctly) trip it.
    real = real_cbs(128)
    rng = random.Random(0)
    reads["R1"] = [rng.choice(real) + r[16:] for r in reads["R1"]]
    f1 = tmp_path / "R1.fastq.gz"
    f2 = tmp_path / "R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])
    result = runner.invoke(
        app, ["resolve", "score", str(f1), str(f2), "-C", str(tmp_path), "--no-cache"]
    )
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["candidates"][0]["technology"] == "10x-3p-gex-v3"
    # Rung 3: the real `3M-february-2018` SHIPS, the onlist check runs, and (with real barcodes) it
    # POSITIVELY matches -- so v3 composes at exit 0 rather than tripping F1b's barcode-absent refusal.
    assert doc["rung_reached"] == 3


@pytest.mark.parametrize(
    "typed",
    [
        pytest.param("10x-3p-gex-v3", id="canonical"),
        pytest.param("10X-3P-GEX-V3", id="upper"),  # operator typed upper-case
    ],
)
def test_assert_chemistry_threads_an_operator_hypothesis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typed: str
) -> None:
    """`--assert-chemistry` must reach `resolve_runs` as an OPERATOR hypothesis that outranks prose.

    The tie-selection itself is proven elsewhere (a `Hypothesis` picks v3 over v2 on ambiguous
    barcodes); the new code is the wiring, so capture the hypothesis the fill pipeline hands the
    scorer and check its identity — id `operator`, confidence 1.0 — is what breaks the tie.

    `resolve score` matches chemistry ids case-insensitively, so `fill` must too: whatever casing the
    operator types, the hypothesis carries the CANONICAL id downstream, not the operator's casing.
    """
    from seqforge.cli import manifest as m
    from seqforge.resolve.engine import Hypothesis

    captured: dict[str, object] = {}

    def _capture(paths: object, *, hypothesis: object = None, **_: object) -> object:
        captured["hypothesis"] = hypothesis
        raise RuntimeError("stop after capturing the hypothesis")

    monkeypatch.setattr(m, "resolve_runs", _capture)
    with pytest.raises(RuntimeError, match="stop after capturing"):
        m._fill_manifest_pipeline(
            files=[tmp_path / "x_1.fastq.gz"],
            organism=None,
            records=None,
            assertions=None,
            offline=True,
            workspace=tmp_path,
            chemistry_override=typed,
        )
    hypo = captured["hypothesis"]
    assert isinstance(hypo, Hypothesis), f"the fill pipeline handed the scorer {hypo!r}"
    assert hypo.value == "10x-3p-gex-v3"  # canonicalized regardless of casing
    assert hypo.id == "operator"
    assert hypo.confidence == 1.0


def test_assert_chemistry_rejects_an_unknown_id(tmp_path: Path) -> None:
    """A typo'd chemistry would silently no-op (the hypothesis never matches a candidate) and the
    operator would only learn after a full compile still escalated. Fail fast with exit 2."""
    from seqforge.cli import manifest as m

    out = m._fill_manifest_pipeline(
        files=[tmp_path / "x_1.fastq.gz"],
        organism=None,
        records=None,
        assertions=None,
        offline=True,
        workspace=tmp_path,
        chemistry_override="10x-v3-typo",
    )
    assert out.code == 2
    assert isinstance(out.payload, dict)
    assert out.payload["error"] == "unknown_chemistry"


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


@pytest.mark.parametrize(
    "gb, over",
    [
        pytest.param(31.10, {"soloFeatures": ["Gene"]}, id="soloFeatures"),
        pytest.param(36.90, {"threads": 48}, id="threads"),
    ],
)
def test_e2e_fit_refuses_runs_that_are_not_comparable(
    tmp_path: Path, gb: float, over: dict[str, object]
) -> None:
    """Peak RSS depends on soloFeatures, assembly, threads and cells -- so a merge across them lies.

    This is the same class as the resume guard's features check: the number is only meaningful
    alongside the configuration that produced it, and a line fitted through two configurations is a
    plausible-looking artefact of nothing. The axis of incomparability -- a different soloFeatures set
    or a different thread count -- is the same refusal with the same exit code and message.
    """
    a = _cost_run(tmp_path, "a.json", 10_000_000, 34.57)
    b = _cost_run(tmp_path, "b.json", 40_000_000, gb, **over)
    result = runner.invoke(app, ["kb", "e2e-fit", str(a), str(b)])
    assert result.exit_code == 3
    assert "incomparable" in result.output or "incomparable" in str(result.exception)


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


def test_a_verbs_stdout_is_json_and_its_progress_goes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI emits JSON on stdout. Progress narration is not a result and must not go there.

    The incident: `kb e2e-cost` runs for tens of minutes, so it narrates -- via `print()`, which put
    `[cost] ...` lines straight through the middle of its own JSON. The first real run produced
    `cost-hg38-2681399.json` that `json.load` rejects at line 1 column 2, and `kb e2e-fit` (which
    reads exactly those files) would have choked on every one. Only `cost_sweep.partial.json`, written
    separately because disk is state, made the three measured points recoverable.

    Pinned on the primitive rather than the verb because the verb needs STAR and a 30 GB index; the
    property under test is one line of plumbing and does not.
    """
    import sys as _sys

    from seqforge.e2e import _progress

    _progress("hello")
    captured = _sys.stdout, _sys.stderr  # noqa: F841  (capsys owns the streams)
    out = capsys.readouterr()
    assert out.out == "", "progress on stdout would corrupt the JSON result"
    assert "[cost] hello" in out.err


@pytest.mark.xdist_group("src-trees")
def test_no_module_under_src_prints_to_stdout(src_trees: SrcTrees) -> None:
    """A bare print() in a library module lands in whatever a verb is emitting. Derive, don't declare.

    This is the general form of the bug above, and the reason it is a scan rather than a note in a
    docstring: the next `print()` someone adds for debugging is silent and corrupts a result the same
    way. `typer.echo` is how a verb speaks; everything else goes to stderr.
    """
    import ast

    offenders = []
    for py, tree in src_trees.items():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"):
                continue
            keywords = {k.arg for k in node.keywords}
            if "file" not in keywords:
                offenders.append(f"{py.name}:{node.lineno}")
    assert not offenders, (
        f"print() to stdout in a library module: {offenders}. stdout carries the JSON result; "
        f"send narration to stderr with file=sys.stderr."
    )


def test_manifest_fill_on_a_three_run_dataset_keeps_every_file(tmp_path: Path) -> None:
    """A multi-run dataset through the real CLI: 6 files, 3 runs, 3 samples, 0 files dropped.

    This is the pilot's shape in miniature -- three runs exercise every claim the six did; the extra
    three were narrative fidelity to the pilot, not coverage.

    Before this, `manifest fill` handed every file to one `resolve_dataset` call, which does one
    global role assignment: one run's two files got roles, every other file got `read_id=None`,
    `compose._units` skipped them in silence, and `validate` said ok. A clean, content-addressed
    manifest recording a wrong answer, exit 0 -- on a dataset that is many runs, as the pilot's is.

    Bulk paired-end so no onlist is needed; the multi-run machinery is chemistry-blind.

    **The files are one directory per accession, because that is the pilot's ACTUAL shape** -- it is
    how `fasterq-dump` wrote them. An earlier version laid them out flat while claiming the pilot's
    shape, and the gap was not cosmetic: a flat directory is its own dataset root, so every URI is a
    basename and the one code path that has to agree about URIs was never exercised. On the real
    dataset `manifest fill` refused its own manifest with referential-integrity Blockers, because
    `cli.py` built `SampleGroup.file_uris` from basenames while `fill_manifest` built relative paths.
    Every fixture in this repo was flat; that is why nothing saw it.
    """
    spec = kb.load_spec("bulk-rnaseq")
    accessions = [f"SRR2871655{i}" for i in range(3, 6)]
    paths: list[str] = []
    for i, acc in enumerate(accessions):
        reads = kb.generate_reads(spec, n=400, seed=i)
        run_dir = tmp_path / "data" / f"SRX2428313{i}"
        run_dir.mkdir(parents=True)
        for mate, role in (("1", "R1"), ("2", "R2")):
            p = run_dir / f"{acc}_{mate}.fastq.gz"
            write_fastq_gz(p, reads[role])
            paths.append(str(p))

    filled = runner.invoke(
        app, ["manifest", "fill", *paths, "--organism", "6239", "-C", str(tmp_path)]
    )
    assert filled.exit_code == 0, filled.stdout
    assert json.loads(filled.stdout)["report"]["ok"] is True

    import yaml

    manifest = yaml.safe_load((tmp_path / "seqforge" / "manifest.yaml").read_text())
    files = manifest["library"]["files"]
    assert len(files) == 6, "every input file is in the inventory"
    assert all(f["read_id"] is not None for f in files), "and every one of them has a role"

    samples = manifest["experiment"]["samples"]
    assert [s["sample_id"] for s in samples] == sorted(accessions), "one sample per RUN"
    assert sum(len(s["file_uris"]) for s in samples) == 6

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


def test_processing_new_takes_an_assembly_from_a_verified_instruction(
    tmp_path: Path, synth_10x_v3: SynthDataset
) -> None:
    """The last mile of a join that already existed and was unreachable.

    `resolve_processing` has always implemented flag > instruction > policy, and its PolicyError even
    says "Pass --assembly/--annotation, **or name an assembly in an --instruction document**". That
    branch could not be reached: `--assembly` was a REQUIRED option, and no production caller ever
    passed `instructions=`. So the instructable surface was real in the API and absent from the CLI.

    Note where the model is and is not. It FOUND `processing.genome.assembly: sacCer3` in a document
    the user handed us with `--instruction`, and code verified the quote greps back and entails the
    value. Applying precedence is code, here. No new LLM authority -- which is the whole reason the
    instructable path is allowed to exist.

    `processing new` needs A valid manifest on disk, not a freshly-CLI-filled one -- the manifest is
    setup this test never inspects -- so dump the shared `synth_10x_v3` fixture instead of paying a
    probe+resolve+fill to re-derive one. sacCer3 is named in the span because that fixture's organism
    is S. cerevisiae (taxid 559292), so `validate_processing`'s organism/genome check still passes.
    """
    import yaml as _yaml

    from seqforge.cli._common import _resolve_organism
    from seqforge.models.assertion import Assertion, ExtractorProvenance, SourceSpan

    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        _yaml.safe_dump(synth_10x_v3.manifest.model_dump(mode="json"), sort_keys=True)
    )

    # the `--organism`->taxid claim, proven directly: a NAME is resolved to a taxid by CODE, not
    # retyped by a human. This is the join `manifest fill` performs; it needs no filled manifest here.
    assert _resolve_organism("Caenorhabditis elegans", offline=True) == 6239

    doc_sha = "a" * 64
    span = SourceSpan(
        doc_sha256=doc_sha, quote="align this dataset against sacCer3", char_start=0, char_end=34
    )
    assertions = {
        "instruction_docs": [doc_sha],
        "assertions": [
            Assertion(
                id="a1",
                field="processing.genome.assembly",
                value="sacCer3",
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
            "ensembl",
            "--assertions",
            str(apath),
            "-o",
            str(out),
        ],
    )
    assert made.exit_code == 0, made.stdout
    doc = _yaml.safe_load(out.read_text())
    genome = doc["processing"]["genome"]
    assert genome["value"]["assembly"] == "sacCer3", "the instruction never reached the manifest"
    # basis records WHO DECIDED: a document the user authored for seqforge is the user talking
    assert genome["basis"] == "user_confirmed"


def test_processing_new_refuses_a_pre_2026_7_assertions_file(
    tmp_path: Path, synth_10x_v3: SynthDataset
) -> None:
    """A bare list cannot say which documents were --instruction, and only those may set processing.*.

    Silently treating every assertion as instructable would turn a downloaded GEO description into a
    path to --soloStrand: prompt injection from a database field into an aligner. Refuse.

    `processing new` needs A valid manifest on disk, so dump the shared fixture rather than fill one:
    the refusal is on the assertions FORMAT, reached before any genome check, so no fill is warranted.
    """
    import yaml as _yaml

    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        _yaml.safe_dump(synth_10x_v3.manifest.model_dump(mode="json"), sort_keys=True)
    )
    old = tmp_path / "old.json"
    old.write_text(json.dumps([{"field": "processing.genome.assembly", "value": "ce11"}]))
    res = runner.invoke(
        app,
        [
            "processing",
            "new",
            str(manifest_path),
            "--annotation",
            "ensembl",
            "--assertions",
            str(old),
        ],
    )
    assert res.exit_code == 2
    assert "harvest extract" in res.stdout + str(res.stderr or "")


# ---------------------------------------------------------------------------------------------
# _sync_questions — the questions.md writer (cli/manifest.py). Moved here from test_hooks.py (#113):
# an agent editing the writer follows the module->file table to test_cli.py. The tests assert THROUGH
# the Stop hook's `questions_outstanding` reader, pinning the writer<->hook contract.
# ---------------------------------------------------------------------------------------------


def test_sync_questions_writes_a_stop_hook_visible_file_and_clears_it(tmp_path: Path) -> None:
    """The `questions.md` writer feeds the Stop hook: an OPEN conflict blocks turn-end, resolving clears.

    This is the human-in-the-loop half of the family-level change — a genuine cross-family disagreement
    lands a visible, editable artifact, and a re-run that settles it removes the file so the hook stops
    wedging. A within-family difference is recorded `resolved`, so it is never `open` and never writes.
    """
    from types import SimpleNamespace

    from seqforge.cli.manifest import _sync_questions
    from seqforge.hooks import questions_outstanding
    from seqforge.models.conflict import Conflict, ConflictPosition
    from seqforge.workspace import state_dir

    def _run(conflicts: list[Conflict]) -> SimpleNamespace:
        result = SimpleNamespace(conflicts=conflicts, questions=[])
        return SimpleNamespace(run_id="run-1", output=SimpleNamespace(result=result))

    open_c = Conflict(
        id="conflict-single-cell-collapsed-to-bulk",
        field="library.chemistry",
        kind="observed_vs_asserted",
        positions=[
            ConflictPosition(value="10x-3p-gex-v2", basis="asserted", confidence=0.9),
            ConflictPosition(value="bulk-rnaseq", basis="observed", confidence=0.99),
        ],
        decidable_by=["reads", "user"],
        status="open",
    )
    state = state_dir(tmp_path)
    _sync_questions(state, [_run([open_c])])
    qmd = state / "questions.md"
    assert questions_outstanding(tmp_path) == [qmd]
    body = qmd.read_text()
    assert "10x-3p-gex-v2" in body and "bulk-rnaseq" in body

    # a resolved (non-open) conflict is not surfaced -> file cleared, the hook stops blocking
    _sync_questions(state, [_run([open_c.model_copy(update={"status": "resolved"})])])
    assert not qmd.exists()
    assert questions_outstanding(tmp_path) == []


def test_sync_questions_unlinks_a_stale_file_on_a_clean_run(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from seqforge.cli.manifest import _sync_questions
    from seqforge.workspace import state_dir

    state = state_dir(tmp_path)
    state.mkdir(parents=True)
    (state / "questions.md").write_text("- a stale question from a prior run\n")
    clean = SimpleNamespace(
        run_id="r", output=SimpleNamespace(result=SimpleNamespace(conflicts=[], questions=[]))
    )
    _sync_questions(state, [clean])
    assert not (state / "questions.md").exists()


def test_io_publish_package_answers_on_stdout_and_uploads_nothing_on_a_dry_run(
    tmp_path: Path,
) -> None:
    """`seqforge io publish-package` is the producer half `preflight` never had.

    Three things are the contract, and each has a failure mode that is silent without it. It emits
    the **public URL** an eval recipe's `hf:` key must equal, because a package uploaded under a name
    no recipe points at is a 404 nobody discovers until the benchmark next runs. `--dry-run` needs no
    credential and touches no network, so the question is answerable before spending a commit. And a
    tarball that is not a fingerprint package is refused with exit 2 rather than uploaded — a corrupt
    package in the public corpus is worse than an absent one, since an absent one skips with a reason.
    """
    from seqforge.fingerprint.build import build_fingerprint

    src = tmp_path / "src"
    src.mkdir()
    rng = random.Random(3)
    for read in ("R1", "R2"):
        write_fastq_gz(
            src / f"s_{read}.fastq.gz",
            ["".join(rng.choice("ACGT") for _ in range(60)) for _ in range(40)],
        )
    built = build_fingerprint(
        sorted(src.glob("*.fastq.gz")), workspace=tmp_path, reads=20, name="GSE110823"
    ).package

    result = runner.invoke(app, ["io", "publish-package", str(built), "--dry-run"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True and payload["commit_url"] is None
    assert payload["rel_path"] == "packages/GSE110823.fingerprint.tar.gz"
    assert payload["url"].endswith("/resolve/main/packages/GSE110823.fingerprint.tar.gz")
    assert payload["repo"] == "liuhlab/seqforge-benchmark"
    assert payload["n_files"] == 2, "the pin was read, so the summary says what is IN the package"

    missing = runner.invoke(app, ["io", "publish-package", str(tmp_path / "nope.tar.gz")])
    assert missing.exit_code == 2, missing.output


def test_io_publish_package_refuses_rather_than_guessing_when_no_token_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real upload with no credential refuses up front, the way provider selection does.

    Letting it through would mean the archive answers 401 halfway into a multi-megabyte POST, which
    reads as a network problem. `--dry-run` is named in the refusal because it is the thing the
    caller probably wanted.
    """
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "get_token", lambda: None)
    package = tmp_path / "x.fingerprint.tar.gz"
    package.write_bytes(b"")

    result = runner.invoke(app, ["io", "publish-package", str(package)])
    assert result.exit_code == 2
    assert "no_hf_token" in result.stderr
    assert "--dry-run" in result.stderr


# ---------- the token Ceiling on the verbs that reach a model ----------
class _CountingProvider:
    """A provider that always answers, and always costs the same. Enough to reach a ceiling."""

    name = "counting"

    def __init__(self, per_call: int = 1000) -> None:
        self.per_call = per_call
        self.n_calls = 0

    def default_model(self) -> str:
        return "counting-model-1"

    def complete_json(self, **kwargs: object) -> object:
        from seqforge.harvest import LLMResponse

        self.n_calls += 1
        return LLMResponse(text=json.dumps({"drafts": []}), usage={"input_tokens": self.per_call})


#: A ceiling one request wide for the documents `_prose_docs_apart` writes. The meter reserves a
#: request's estimated cost before issuing it, so what a ceiling buys is a number of REQUESTS, not a
#: number of documents — and a test that pinned the ceiling to a wave of the pool would pass only
#: while the pool stayed narrow. One of those documents is estimated at roughly 5.2k tokens, so the
#: first request is admitted and every other one is refused however many the pool offered at once.
_CEILING = 6000


def _prose_docs(tmp_path: Path, n: int) -> list[str]:
    """N one-line documents, which all receive the same ask and therefore travel in ONE request."""
    paths = []
    for i in range(n):
        doc = tmp_path / f"methods-{i}.txt"
        doc.write_text(f"Libraries were prepared with the Chromium Single Cell 3' v{i} kit.")
        paths.append(str(doc))
    return paths


def _prose_docs_apart(tmp_path: Path, n: int) -> list[str]:
    """N documents that cannot share a request, for the tests that are about a COUNT of requests.

    Same-ask documents batch, and one-line prose all receives the same ask — so a test about
    exchanges, or about a ceiling refusing partway through a fan-out, would otherwise be a test of a
    single request and its counts would all read 1. Past half the character budget no two documents
    fit together, which restores one request per document without any test having to say so.

    The distinction is deliberately a second helper rather than a wider `_prose_docs`: what
    `usage.n_calls` reports when several documents DO share a request is itself a thing worth
    pinning, and inflating every document would delete the only case that can show it.
    """
    from seqforge.harvest.plan import MAX_BATCH_CHARS

    filler = "Cells were fixed and washed. " * (MAX_BATCH_CHARS // 58 + 4)
    paths = []
    for i in range(n):
        doc = tmp_path / f"methods-{i}.txt"
        doc.write_text(
            f"Libraries were prepared with the Chromium Single Cell 3' v{i} kit. {filler}"
        )
        paths.append(str(doc))
    return paths


def test_harvest_extract_refuses_at_the_ceiling_with_a_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ceiling that only warned would be a number nobody sets, so it is a refusal: exit 3 with a
    `Blocker`, the same shape every other refusal in the compiler takes — never `llm_unavailable`
    at exit 1, which says the endpoint failed when it answered every request it was given.

    Six documents is deliberately more than one wave of a small pool: the refusal must not depend on
    the fan-out serialising. They are written too long to share a request, so six documents really
    are six requests — one request of this shape is estimated at roughly 5.2k tokens, so `_CEILING`
    covers one of the six and the rest are refused however many the pool offered at once.
    """
    import seqforge.harvest as harvest_pkg

    provider = _CountingProvider(per_call=1000)
    monkeypatch.setattr(harvest_pkg, "resolve_provider", lambda _name=None: provider)

    result = runner.invoke(
        app,
        [
            "harvest",
            "extract",
            *_prose_docs_apart(tmp_path, 6),
            "--ceiling",
            str(_CEILING),
            "-C",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 3, result.stdout
    payload = json.loads(result.stdout)
    assert payload["error"] == "token_ceiling_exceeded"
    blocker = payload["blockers"][0]
    assert blocker["code"] == "TOKEN_CEILING_EXCEEDED"
    assert "--ceiling" in blocker["remedy"]
    # The ledger is written anyway: the tokens up to the ceiling were really spent, and a reader
    # asking "on what?" is exactly the reader a breach produces.
    assert provider.n_calls >= 1, "a ceiling below one request's estimate would prove nothing here"
    totals = json.loads((tmp_path / "seqforge" / "logs" / "usage.json").read_text())["totals"]
    assert totals["n_calls"] == provider.n_calls
    assert totals["input_tokens"] == 1000 * provider.n_calls


def test_harvest_usage_counts_requests_not_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`n_calls` was `len(documents)`, so every retry was free in the one place a reader looks. It
    is the meter's count of real requests now, with the document count kept beside it.

    These three documents receive the same ask and travel in ONE request, which is what makes this
    the case worth pinning: the two numbers now differ in the ordinary run rather than only after a
    retry, so a reader who confuses them is wrong about the bill by the batching factor. Handing this
    test documents too long to batch would put the two numbers back in agreement and delete the only
    case that can tell them apart.
    """
    import seqforge.harvest as harvest_pkg

    provider = _CountingProvider(per_call=7)
    monkeypatch.setattr(harvest_pkg, "resolve_provider", lambda _name=None: provider)

    result = runner.invoke(
        app, ["harvest", "extract", *_prose_docs(tmp_path, 3), "-C", str(tmp_path)]
    )
    assert result.exit_code == 0, result.stdout
    usage = json.loads(result.stdout)["usage"]
    assert usage["n_calls"] == 1 and usage["n_documents"] == 3
    assert usage["input_tokens"] == 7, "one request's worth, not one per document"


def test_harvest_extract_writes_its_transcript_beside_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing anywhere used to persist a prompt or a response: `usage.json` records the shape of
    every call and no text, so "why did the model say that" became unanswerable the moment the
    process exited. The transcript is a PATH on stdout and a file on disk — a thousand exchanges
    cannot ride on a stream that is the result object.
    """
    import seqforge.harvest as harvest_pkg
    from seqforge.harvest import read_transcript

    provider = _CountingProvider(per_call=7)
    monkeypatch.setattr(harvest_pkg, "resolve_provider", lambda _name=None: provider)

    result = runner.invoke(
        app, ["harvest", "extract", *_prose_docs_apart(tmp_path, 3), "-C", str(tmp_path)]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)

    path = Path(payload["transcript_path"])
    assert path == tmp_path / "seqforge" / "logs" / "transcript.jsonl", "beside usage.json"
    assert Path(payload["usage_path"]).parent == path.parent

    transcript = read_transcript(path)
    assert transcript.n_exchanges == 3 == payload["usage"]["n_calls"]
    # the paths, not the contents: neither the system prompt nor a raw response is on stdout
    assert next(iter(transcript.prompts.values())) not in result.stdout
    assert transcript.exchanges[0].user not in result.stdout
    assert len(transcript.prompts) == 1, "the stable prefix is stored once, not once per exchange"
    assert [len(e.text) > 0 for e in transcript.exchanges] == [True] * 3
    # the three documents' own text is the volatile half, and each exchange holds its own
    assert len({e.user for e in transcript.exchanges}) == 3


def test_harvest_extract_writes_the_transcript_of_a_run_it_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run stopped at its ceiling is the one whose exchanges are most worth reading."""
    import seqforge.harvest as harvest_pkg
    from seqforge.harvest import read_transcript

    provider = _CountingProvider(per_call=1000)
    monkeypatch.setattr(harvest_pkg, "resolve_provider", lambda _name=None: provider)

    result = runner.invoke(
        app,
        [
            "harvest",
            "extract",
            *_prose_docs_apart(tmp_path, 6),
            "--ceiling",
            str(_CEILING),
            "-C",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 3
    transcript = read_transcript(Path(json.loads(result.stdout)["transcript_path"]))
    assert provider.n_calls >= 1 and transcript.n_exchanges == provider.n_calls


def test_harvest_extract_dry_run_costs_nothing_and_needs_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "What will this dataset cost" must be answerable before it is paid, and on a machine with no
    key at all — so the plan is returned before a provider is even resolved."""
    import seqforge.harvest as harvest_pkg
    from seqforge.harvest import ProviderUnavailable

    def _no_provider(_name: str | None = None) -> object:
        raise ProviderUnavailable("no credential")

    monkeypatch.setattr(harvest_pkg, "resolve_provider", _no_provider)

    result = runner.invoke(
        app, ["harvest", "extract", *_prose_docs(tmp_path, 3), "--dry-run", "-C", str(tmp_path)]
    )
    assert result.exit_code == 0, result.stdout
    plan = json.loads(result.stdout)
    assert plan["n_documents"] == 3
    # ...and what it will COST is a count of requests, not of documents: these three receive the
    # same ask, so the stable prefix is paid once. A plan charging it per document would overstate a
    # real run by twice the prefix, and a dry run that disagrees with the run it prices is worse
    # than none.
    assert plan["n_requests"] == 1
    assert plan["estimated_input_tokens"] > plan["system_prompt_chars"] // 4
    assert plan["estimated_input_tokens"] < 2 * plan["system_prompt_chars"] // 4
    assert [d["scope"] for d in plan["documents"]] == ["dataset"] * 3
    assert not (tmp_path / "seqforge").exists(), "a dry run writes nothing either"


def test_harvest_extract_pays_once_for_a_document_handed_to_it_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reassembly bug, from the outside.

    Outcomes were collected into a dict keyed by `doc_sha256`, so two documents that render
    identically each cost a call, one result survived, and the loop then read that one outcome once
    per collider — reporting twice its drafts, twice its rejections and twice its usage. One ask is
    one document now, and the results come back positionally.
    """
    import seqforge.harvest as harvest_pkg

    class _OneDraftEach:
        name = "one-draft"

        def __init__(self) -> None:
            self.n_calls = 0

        def default_model(self) -> str:
            return "one-draft-1"

        def complete_json(self, **kwargs: object) -> object:
            from seqforge.harvest import LLMResponse

            self.n_calls += 1
            draft = {
                "field": "experiment.organism",
                "value": "Caenorhabditis elegans",
                "span": {"doc_sha256": "0" * 64, "quote": "Caenorhabditis elegans"},
                "llm_confidence": 0.9,
            }
            return LLMResponse(text=json.dumps({"drafts": [draft]}), usage={"input_tokens": 100})

    provider = _OneDraftEach()
    monkeypatch.setattr(harvest_pkg, "resolve_provider", lambda _name=None: provider)

    doc = tmp_path / "methods.txt"
    doc.write_text("Worms are Caenorhabditis elegans, maintained at 20 C.")
    result = runner.invoke(
        app, ["harvest", "extract", str(doc), str(doc), str(doc), "-C", str(tmp_path)]
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert provider.n_calls == 1, "three copies of one document are one question"
    assert payload["usage"]["n_calls"] == 1 and payload["usage"]["n_documents"] == 1
    assert payload["usage"]["input_tokens"] == 100, "counted once, not once per collider"
    assert payload["n_drafts"] == 1 and payload["n_accepted"] == 1


def test_harvest_extract_asks_a_samples_runs_once_between_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fan-out, end to end through the verb.

    Every archive record with prose used to be its own call, so a series with twelve runs per sample
    spent twelve calls asking twelve one-line aliases the same nine questions. The runs are still
    READ — an alias is often the only place a WT-vs-mutant contrast is written — but a run belongs to
    exactly one sample, so they are one document, and it is the SAMPLE that document names.
    """
    import seqforge.harvest as harvest_pkg
    from seqforge.models.records import ArchiveRecord, ArchiveRecordSet, FreeText

    provider = _CountingProvider(per_call=7)
    monkeypatch.setattr(harvest_pkg, "resolve_provider", lambda _name=None: provider)

    records = ArchiveRecordSet(
        source="test",
        query="PRJNA9",
        records=[
            ArchiveRecord(
                level="project",
                accession="PRJNA9",
                free_text=[FreeText(label="abstract", text="Wild-type and daf-2 mutants.")],
            ),
            ArchiveRecord(level="sample", accession="SAMN1"),  # no prose: nothing to read
            ArchiveRecord(level="experiment", accession="SRX1", parent="SAMN1"),
            *(
                ArchiveRecord(
                    level="run",
                    accession=f"SRR{i}",
                    parent="SRX1",
                    free_text=[FreeText(label="run_alias", text=f"N2_wild_type_r{i}")],
                )
                for i in range(6)
            ),
        ],
    )
    records_path = tmp_path / "records.json"
    records_path.write_text(records.model_dump_json())

    result = runner.invoke(
        app,
        [
            "harvest",
            "extract",
            *_prose_docs(tmp_path, 1),
            "--records",
            str(records_path),
            "-C",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    usage = json.loads(result.stdout)["usage"]
    assert usage["n_documents"] == 2, "the paper, and the six runs as one document"
    assert usage["n_calls"] == 2
    # the project is asked nothing at all, and a record with no prose has nothing to read
    subjects = json.loads((tmp_path / "seqforge" / "logs" / "assertions.json").read_text())
    placed = {(d["scope"], d["subject"]) for d in subjects["document_subjects"]}
    assert placed == {("dataset", None), ("run", "SAMN1")}
    # the bytes a citation greps into are on disk under a name a human can read
    documents = tmp_path / "seqforge" / "records" / "documents"
    assert any(p.name.startswith("runs-SAMN1-") for p in documents.iterdir())
