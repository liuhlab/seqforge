"""Smoke tests for the ``seqforge`` CLI (schema export is the first live verb)."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from typer.testing import CliRunner, Result

from conftest import (
    SrcTrees,
    SynthDataset,
    declare_read_floor,
    one_run_each,
    plate_of,
    real_cbs,
    write_fastq_gz,
)
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
    # Which annotation the plate is counted against has exactly one answer, and the command line
    # says it once: both flags is a caller who believes two different things about which annotation
    # this is, neither is a caller who has said nothing. Argv alone decides each, before anything
    # reaches a genome store, so each lands here with the malformed cell argument rather than with
    # what the environment refuses. The row that would go red is `--component` quietly winning over
    # `--annotation`, which was rejected: a rendered command line saying two contradictory things
    # about which GTF was used, in a repo whose wiring gate reads rendered commands.
    pytest.param(
        ["io", "umi-count", "cell_a=/x/cell.bam", "--assembly", "tinyCe_tinyEcDub",
         "--annotation", "wormbase_ws298", "--component", "tinyCe", "--out", "plate.h5ad"],
        2, (), id="io-umi-count-refuses-an-annotation-and-a-component-together",
    ),
    pytest.param(
        ["io", "umi-count", "cell_a=/x/cell.bam", "--assembly", "tinyCe_tinyEcDub",
         "--out", "plate.h5ad"], 2, (),
        id="io-umi-count-refuses-neither-an-annotation-nor-a-component",
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

    **`--threads` is the other thing it marshals, and it is the whole of what #397 changed here.**
    The counting rule asks the scheduler for threads and renders them into this command; a verb that
    accepted the option and dropped it would leave the plate on one core at exit 0, which is the
    state this ticket found. So the number is caught on its way into the counter rather than
    inferred from how fast one cell counted.
    """
    import anndata as ad
    import genome as liulab_genome
    import gffutils
    import pysam

    from seqforge.workflows.umite import count as counter

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

    class _StubRegistry:
        def path(self, name: str) -> Path:
            return gtf

    class _StubGenome:
        def __init__(self, assembly: str) -> None:
            self.assembly = assembly
            self.annotations = _StubRegistry()

    monkeypatch.setattr(liulab_genome, "Genome", _StubGenome)

    asked_for: list[int] = []
    write_counts = counter.write_umi_counts

    def note_the_width(cells: Any, db: Path, out: Path, workers: int = 1) -> Path:
        asked_for.append(workers)
        return write_counts(cells, db, out, workers)

    monkeypatch.setattr(counter, "write_umi_counts", note_the_width)

    written = tmp_path / "plate.h5ad"
    result = runner.invoke(
        app,
        ["io", "umi-count", f"cell_a={bam}", "--assembly", "mm10",
         "--annotation", "synthetic", "--out", str(written), "--threads", "3"],
    )  # fmt: skip

    assert result.exit_code == 0, result.stdout
    assert asked_for == [3], "the verb took a thread count and counted the plate on one core"
    assert json.loads(result.stdout)["written"] == str(written)
    adata = ad.read_h5ad(written)
    assert list(adata.obs_names) == ["cell_a"]
    # `X` is declared as a union that includes a lazy on-disk dataset; on an object just read back
    # it is the sparse matrix that was written, and only the cast says so to the checker.
    counts = cast("Any", adata.X)
    assert int(counts[0, adata.var_names.get_loc("GENE_A")]) == 1


def test_io_umi_count_reads_a_components_annotation_off_the_chimeras_completion_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--component` counts one Component against what it contributed to the merge.

    A Chimera's merged annotation deliberately does not record which Components fed it, so the
    per-Component registered name is not offline-recoverable: nobody can type it on a command line
    and compose cannot put it in a config. The verb reads it off the completion record at run time,
    and that is the derivation under test — together with the assembly it then resolves the GTF
    under, which is the **Component's** own, because that is where a Component's GTF is registered.

    The stub's `default_gtf` raises, and that is the point of it: reading a Component's default
    *now* is the rejected shortcut, and it is not the same fact as what went into this merge. A
    Component that contributed nothing is named rather than counted against nothing — `tinyEcDub`
    ships no GTF, and neither would a spike-in or a plasmid.

    `Genome` is stubbed because a real Chimera needs a built genome store this box may not have, and
    the counter is stubbed out too: what a `.db` beside its `.gtf` actually counts is the
    neighbouring test's claim, proved there against a synthetic annotation. This one is about which.
    """
    import genome as liulab_genome

    from seqforge.workflows.umite import count as counter

    chimera = "tinyCe_tinyEcDub"
    record: dict[str, str | None] = {"tinyCe": "wormbase_ws298", "tinyEcDub": None}
    resolved: list[tuple[str, str]] = []

    class _StubRegistry:
        def __init__(self, assembly: str) -> None:
            self.assembly = assembly

        def path(self, name: str) -> Path:
            resolved.append((self.assembly, name))
            return tmp_path / f"{name}.gtf"

    class _StubGenome:
        def __init__(self, assembly: str) -> None:
            self.assembly = assembly
            self.annotations = _StubRegistry(assembly)

        @property
        def component_annotations(self) -> dict[str, str | None] | None:
            return dict(record) if self.assembly == chimera else None

        @property
        def default_gtf(self) -> str:
            raise AssertionError(
                "a Component's default annotation now is not necessarily what went into the merge"
            )

    monkeypatch.setattr(liulab_genome, "Genome", _StubGenome)
    monkeypatch.setattr(counter, "write_umi_counts", lambda cells, db, out, workers=1: out)

    written = tmp_path / "combined.tinyCe.h5ad"
    result = runner.invoke(
        app,
        ["io", "umi-count", f"cell_a={tmp_path / 'cell.bam'}", "--assembly", chimera,
         "--component", "tinyCe", "--out", str(written)],
    )  # fmt: skip

    assert result.exit_code == 0, result.stdout + result.stderr
    assert resolved == [("tinyCe", "wormbase_ws298")], (
        "the registered name comes off the Chimera's record and the GTF off the Component itself"
    )

    refused = runner.invoke(
        app,
        ["io", "umi-count", f"cell_a={tmp_path / 'cell.bam'}", "--assembly", chimera,
         "--component", "tinyEcDub", "--out", str(written)],
    )  # fmt: skip

    assert refused.exit_code == 3, refused.stdout + refused.stderr
    # A Chimera is named after its Components, so `in` would be satisfied by the Chimera alone: what
    # this pins is that the refusal's subject is the Component nothing can be counted against.
    assert json.loads(refused.stderr)["error"].startswith("tinyEcDub "), (
        "an uncountable Component is named, not just the Chimera it is part of"
    )


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


def _shipped_spec_raw(tech: str) -> dict[str, Any]:
    """A shipped `spec.yaml` as plain data, ready to be mutated into an entry that cannot ship."""
    from seqforge.kb.loader import SPECS_DIR

    return cast("dict[str, Any]", yaml.safe_load((SPECS_DIR / tech / "spec.yaml").read_text()))


def _lint_error_for(monkeypatch: pytest.MonkeyPatch, raw: dict[str, Any]) -> str:
    """Point `kb lint` at ONE off-disk spec and return the error it reported for it.

    Every caller below needs an entry that cannot ship, and each of their clauses fires at LOAD — so
    no file under `kb/specs/` can be made to violate one without failing every other test in the suite
    at import time. The verb is therefore pointed at a mutated copy of a real spec instead.
    `Spec.model_validate` is the real validator throughout; what is stubbed is only which files the
    verb walks. Exit 3, `ok: false` and the named entry are asserted here because they are the same
    claim every caller makes — what differs is only the message, which is what comes back.
    """
    from seqforge.cli import kb as kb_cli
    from seqforge.kb.schema import Spec

    monkeypatch.setattr(kb_cli, "list_spec_ids", lambda: ["impossible"])
    monkeypatch.setattr(kb_cli, "load_spec", lambda tech: Spec.model_validate(raw))

    result = runner.invoke(app, ["kb", "lint"])

    assert result.exit_code == 3
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["specs"][0]["tech"] == "impossible"
    return str(report["specs"][0]["error"])


def test_kb_lint_fires_on_a_spec_whose_cell_axis_and_module_disagree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verb, not just the validator: a violating entry makes `kb lint` exit 3 and say which.

    `test_kb_lint_is_clean` proves the shipped KB passes, which a lint that checked nothing would
    also prove. This is the other direction: a real shipped spec with one field flipped, claiming that
    one Sample is one cell beside a module that is per-sample end to end. That compiles a 1440-well
    plate to 1440 separate objects at exit 0, which is the answer the pairing exists to make unsayable.
    """
    raw = _shipped_spec_raw("10x-3p-gex-v3")
    raw["identity"] = {**raw["identity"], "sample_is_cell": True}

    assert "is per-sample end to end" in _lint_error_for(monkeypatch, raw)


def test_kb_lint_reports_a_width_that_lies_rather_than_tracebacking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verb, not just the validator: an element wider than its own window exits 3 and says so.

    `Element._addressable` raises a bare `ValueError`, which pydantic wraps into the `ValidationError`
    `kb lint` already catches — so this is a claim about the wrapping, not about the clause. Without
    it a mis-declared width would leave the verb by the uncaught path: a traceback on stderr and exit
    1, which reads as a broken tool rather than a spec that needs one number changed.
    """
    raw = _shipped_spec_raw("splitseq")
    linker = next(
        e for r in raw["reads"] if r["id"] == "bc" for e in r["elements"] if e["name"] == "linker1"
    )
    linker["end"] += 1

    error = _lint_error_for(monkeypatch, raw)
    assert "'linker1'" in error
    assert "30 bp" in error and "31 bp" in error


def test_kb_roundtrip_passes() -> None:
    result = runner.invoke(app, ["kb", "roundtrip", "10x-3p-gex-v3"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["passed"] is True


def test_manifest_fill_validate_hash_compose_spine(tmp_path: Path) -> None:
    """The whole deterministic spine, driven through the real CLI: probe->resolve->manifest->compose.

    Uses the no-barcode bulk branch so it needs no onlist: the default registry deliberately
    materializes no real whitelist (they are license-restricted), which is exactly why the 10x path
    refuses to compose until one is registered.

    It is also where `--mem-gb` is proved, because the claim is a JOURNEY rather than a field: a
    memory figure a recipe author types has to survive `processing new` -> `processing.yaml` ->
    `compose` -> the emitted config a mapping rule reads its request off. sacCer3 is the right place
    to prove it -- a small genome is the whole reason the option exists, since 48 GB against an index
    of a gigabyte or so is a figure nothing about residency argues for.
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
            "--mem-gb",
            "8",
            "-o",
            str(proc_path),
        ],
    )
    assert authored.exit_code == 0, authored.stdout
    assert proc_path.is_file()
    assert yaml.safe_load(proc_path.read_text())["processing"]["resources"]["mem_gb"] == 8, (
        "--mem-gb never reached the recipe, so the only way to size a small-genome run is still to "
        "hand-edit the generated file"
    )
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
    assert doc["gate"]["params"]["status"] == "pass"
    assert (
        doc["gate"]["e2e"]["status"] == "skip"
    )  # honest: the count-matrix run needs STAR + liulab-genome
    assert (tmp_path / doc["config_path"]).is_file()
    assert (tmp_path / doc["units_path"]).is_file()
    # whatever decided the run is recoverable from disk, bound to this dataset
    assert ((tmp_path / doc["config_path"]).parent / "processing.lock.yaml").is_file()

    # ...and the stated figure is what the pipeline's rules read their request off. `config["mem_mb"]`
    # is the one place a rule can see it, so this is the far end of the journey the option exists for.
    config = yaml.safe_load((tmp_path / doc["config_path"]).read_text())
    assert config["mem_mb"] == 8 * 1024

    # Omitting the flag leaves the SCHEMA's default, compared against the model rather than against
    # the literal 48: a recipe that says nothing must be byte-identical to what shipped before the
    # option existed, and the way that breaks is the CLI acquiring a default of its own.
    from seqforge.models.processing import ResourceHints

    bare_path = tmp_path / "processing.bare.yaml"
    bare = runner.invoke(
        app,
        [
            "processing", "new", str(manifest_path),
            "--assembly", "sacCer3", "--annotation", "ensembl", "-o", str(bare_path),
        ],
    )  # fmt: skip
    assert bare.exit_code == 0, bare.stdout
    resources = yaml.safe_load(bare_path.read_text())["processing"]["resources"]
    assert resources["mem_gb"] == ResourceHints().mem_gb
    assert resources["threads"] == ResourceHints().threads


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
    assert summary["stages"]["compose"]["gate"]["params"]["status"] == "pass"

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


def test_compose_says_on_the_human_stream_that_it_dropped_cells(
    synth_smartseq3: SynthDataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pipeline shorter than the manifest it was compiled from must SAY so where a person looks.

    The counts and the record path are on stdout with everything else, which is the machine surface —
    but a compile that quietly produces 1200 of 1440 samples is precisely the shape nobody goes
    looking for. One line on stderr, and it names the record rather than restating it.

    Driven through the real verb rather than the composer, because what is under test is the seam
    between them: `compose` decides and the CLI reports, and the failure this catches is the report
    going missing while the decision keeps working.

    On the plate chemistry, because the line asserted below says *cells* and that is the one entry
    whose Sample is one.
    """
    plate = plate_of(synth_smartseq3.manifest, one_run_each({"cell1": 4000, "cell2": 400}))
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(plate.model_dump(mode="json"), sort_keys=True))
    declare_read_floor(monkeypatch, plate.library.chemistry.value[0], 1000)

    result = runner.invoke(
        app,
        [
            "compose",
            str(manifest_path),
            "--assembly",
            "sacCer3",
            "--annotation",
            "ensembl",
            "-C",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    admission = json.loads(result.stdout)["admission"]
    assert admission["excluded"] == {"cell2": 400} and admission["declared"] == 2
    assert "1 of 2 cells dropped" in result.stderr
    assert admission["record_path"] in result.stderr
    assert (tmp_path / admission["record_path"]).is_file()


def test_compose_says_on_the_human_stream_which_knowledge_base_it_compiled_under(
    synth_smartseq3: SynthDataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest whose chemistry was decided under an older KB must SAY so — ADR-0037.

    An old manifest under a new KB exits 0, and — since `run_id` folds a hash of the deciding spec
    and no longer this version string — a bump that left this chemistry alone lands it in the SAME
    directory as before, which is the reuse that narrowing bought. What neither the key nor the
    directory can carry is that the chemistry was *decided* under the older KB: which spec a dataset
    resolves to is a whole-knowledge-base question, a signature edited anywhere can change the
    answer, and nothing on the machine surface says the two versions differ. So the disclosure is a
    line on the human stream, beside the admission line and for the same reason — not a gate, because
    there is no failure here to gate on.

    `KB_VERSION` is patched where `compose.core` binds it rather than on `kb` or on this CLI, because
    that is the one place the value still enters: `compose` records both versions on its result and
    every verb renders from those. It is patched *forward*, to a version the manifest cannot have
    been filled under: every manifest this suite builds is filled under the live KB, which is exactly
    why no fixture produced this divergence and exactly why the defect ADR-0037 fixes survived a
    suite that composes constantly.
    """
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(synth_smartseq3.manifest.model_dump(mode="json"), sort_keys=True)
    )
    recorded = synth_smartseq3.manifest.provenance.kb_version
    monkeypatch.setattr("seqforge.compose.core.KB_VERSION", "2099.1.1")

    result = runner.invoke(
        app,
        ["compose", str(manifest_path), "--assembly", "sacCer3", "--annotation", "ensembl",
         "-C", str(tmp_path)],
    )  # fmt: skip

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "2099.1.1" in result.stderr, (
        f"the KB actually compiled under must be named, or the reader cannot tell which of the two "
        f"produced the params: {result.stderr}"
    )
    assert recorded in result.stderr, (
        f"naming only the live KB says a version changed without saying from what: {result.stderr}"
    )
    assert "manifest fill" in result.stderr, (
        f"a disclosure the reader cannot act on is half a disclosure — say what closes the gap: "
        f"{result.stderr}"
    )


def test_compose_is_silent_about_the_knowledge_base_when_it_has_not_moved(
    synth_smartseq3: SynthDataset, tmp_path: Path
) -> None:
    """...and says nothing in the ordinary case, which is what makes the line above worth reading.

    The other half of the disclosure, and the half that decays first: a line printed on every compile
    is a line nobody sees. Every manifest the suite builds is filled under the live knowledge base, so
    this is the path every other test takes and the stream must stay clean on it.
    """
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(synth_smartseq3.manifest.model_dump(mode="json"), sort_keys=True)
    )

    result = runner.invoke(
        app,
        ["compose", str(manifest_path), "--assembly", "sacCer3", "--annotation", "ensembl",
         "-C", str(tmp_path)],
    )  # fmt: skip

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "knowledge base" not in result.stderr, (
        f"the manifest was filled under the live KB, so there is no divergence to disclose and the "
        f"human stream must stay quiet: {result.stderr}"
    )


def test_run_carries_the_knowledge_base_divergence_on_its_machine_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headless path discloses it too — and discloses it where a machine is looking.

    `run` chains the same `compose` call across ~10⁴ datasets, so it is the one path where this can
    matter at scale, and it was the one path that said nothing: the disclosure lived as a comparison
    inside `compose`'s CLI, which `run` does not go through. Copying that comparison into `run.py`
    would have been a second spelling of one fact; instead `compose` records both versions on its
    result, and `run` already dumps that result whole.

    So this asserts the *summary*, not the stream. `run`'s stdout contract is one JSON object and its
    stderr is all but unused — a warning printed once per dataset in a sweep of ten thousand is a
    warning nobody reads, while a field is one a filter can find. The two versions are what the JSON
    carries, and both are in `schema export`; `ComposeResult.kb_moved` is the same comparison for
    Python callers and is deliberately not serialised, so the machine surface holds no key the
    exported schema does not describe.

    Patching `compose.core.KB_VERSION` forward is what manufactures the divergence, and it works here
    for a reason worth naming: `run` fills the manifest in the same pass, and `manifest.fill` binds
    `KB_VERSION` from `kb` independently of the composer. So fill stamps the real version and the
    compile reads the patched one — which is the real-world shape (a manifest filled months ago,
    compiled today) reproduced without a stale fixture.
    """
    spec = kb.load_spec("bulk-rnaseq")
    reads = kb.generate_reads(spec, n=600, seed=0)
    f1 = tmp_path / "s_R1.fastq.gz"
    f2 = tmp_path / "s_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])
    monkeypatch.setattr("seqforge.compose.core.KB_VERSION", "2099.1.1")

    result = runner.invoke(
        app,
        ["run", str(f1), str(f2), "--organism", "559292", "--assembly", "sacCer3",
         "--annotation", "ensembl", "--no-llm", "--fastq-dir", str(tmp_path), "-C", str(tmp_path)],
    )  # fmt: skip

    assert result.exit_code == 0, result.stdout + result.stderr
    compose_stage = json.loads(result.stdout)["stages"]["compose"]
    assert compose_stage["kb_version"] != compose_stage["manifest_kb_version"], (
        f"the headless path compiled under a knowledge base that never saw this chemistry decided "
        f"and its summary does not say so -- which is the whole defect: {compose_stage}"
    )
    assert compose_stage["kb_version"] == "2099.1.1", (
        f"the KB the params actually came from must be named: {compose_stage}"
    )
    assert compose_stage["manifest_kb_version"] == kb.KB_VERSION, (
        f"the KB that decided the chemistry is the one fill stamped, and naming only the live one "
        f"says a version changed without saying from what: {compose_stage}"
    )


def test_run_says_the_knowledge_base_agrees_when_it_does(tmp_path: Path) -> None:
    """...and reports agreement on the ordinary path, which is what makes the field readable.

    The mirror of the test above, and the one that keeps the disclosure honest: two versions that
    differ whenever anyone looks carry no information. Every manifest this suite builds is filled
    under the live knowledge base, so this is the path every other `run` takes.
    """
    spec = kb.load_spec("bulk-rnaseq")
    reads = kb.generate_reads(spec, n=600, seed=0)
    f1 = tmp_path / "s_R1.fastq.gz"
    f2 = tmp_path / "s_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])

    result = runner.invoke(
        app,
        ["run", str(f1), str(f2), "--organism", "559292", "--assembly", "sacCer3",
         "--annotation", "ensembl", "--no-llm", "--fastq-dir", str(tmp_path), "-C", str(tmp_path)],
    )  # fmt: skip

    assert result.exit_code == 0, result.stdout + result.stderr
    compose_stage = json.loads(result.stdout)["stages"]["compose"]
    assert compose_stage["kb_version"] == compose_stage["manifest_kb_version"] == kb.KB_VERSION


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
    # These three points are fixture inputs, not measurements — hg38's real curve is flat. What is
    # pinned is that the fit recovers a small positive slope from whatever points it is handed.
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


# ---- io umi-extract: the per-cell half of the plate-assay counting engine -------------------------


def _plate_geometry() -> str:
    """The rendered read structure compose derives for a tagged-molecule layout, from its ELEMENTS.

    11 bp tag, 8 bp UMI, `GGG`, cDNA from 22 — written as a read the model validates and then
    derived, never as the string itself. The verb takes one derived value and has no way to be
    handed a number, so what a test must not do is type the answer it is checking.
    """
    from seqforge.models.dataset import ReadDef, ReadElement
    from seqforge.workflows.umite.extract import geometry_for_read

    tagged = ReadDef(
        read_id="R1",
        strand="pos",
        min_len=40,
        max_len=150,
        elements=[
            ReadElement(
                role="linker",
                region_type="custom_primer",
                start=0,
                length=11,
                sequence="ATTGCGCAATG",
            ),
            ReadElement(role="UMI", region_type="umi", start=11, length=8),
            ReadElement(role="linker", region_type="linker", start=19, length=3, sequence="GGG"),
            ReadElement(role="cDNA", region_type="cdna", start=22),
        ],
    )
    return geometry_for_read(tagged).render()


def _plate_fastqs(tmp_path: Path) -> tuple[Path, Path]:
    """One cell: two tagged reads (one of them at offset 13) and one internal read."""
    tag, cdna = "ATTGCGCAATG", "GATCACAGGTCTATCACCCTATTAACCACTCACGGGAGCTCTCCATGCATTTGG"
    r1 = [tag + "ACGTACGT" + "GGG" + cdna, "CTGTCTCTTATA" + tag + "TTTTGGCC" + "GGG" + cdna, cdna]
    r1_path, r2_path = tmp_path / "cell_R1.fastq.gz", tmp_path / "cell_R2.fastq.gz"
    write_fastq_gz(r1_path, r1, prefix="cell")
    write_fastq_gz(r2_path, [cdna] * 3, prefix="cell")
    return r1_path, r2_path


def test_umi_extract_takes_one_derived_geometry_and_offers_no_way_to_declare_a_number(
    tmp_path: Path,
) -> None:
    """The verb marshals arguments and decides nothing: the anchor, the UMI's offset and length, the
    trailing motif and the cDNA start all arrive as ONE value the composer read off the elements.

    The second half of the assertion is the part that keeps it true. A `--anchor` flag would let a
    caller hand the extractor a geometry assembled by hand, piece by piece, that disagrees with what
    the bytes were decided to be — so the absence of one is checked against the live app rather than
    left to review. `--geometry` is not that flag and is the reason there is no room for one: it is
    the composer's derivation, in the composer's derived key set, which a KB may not declare.
    """
    r1, r2 = _plate_fastqs(tmp_path)

    summary = tmp_path / "cell_42.umi-extract.json"
    result = runner.invoke(
        app,
        ["io", "umi-extract", "--r1", str(r1), "--r2", str(r2), "--geometry", _plate_geometry(),
         "--sample", "cell_42", "--out", str(tmp_path / "cell_42.bam"),
         "--summary", str(summary)],
    )  # fmt: skip

    assert result.exit_code == 0, result.stdout
    written = json.loads(result.stdout)
    assert written["read_id"] == "R1"
    assert (written["fragments"], written["tagged"], written["untagged"]) == (3, 2, 1)
    # The offset histogram is how a run reports whether the unanchored search still earns its keep.
    assert written["offsets"] == {"0": 1, "12": 1}
    assert (tmp_path / "cell_42.bam").exists()
    # Stdout is an ADDITION's peer here, not its replacement: the same payload lands on disk, where
    # it outlives the `temp()` uBAM, and stdout says where it went. A verb that printed and wrote
    # two different things would be two accounts of one extraction.
    assert written["summary"] == str(summary)
    assert json.loads(summary.read_text()) == {
        k: v for k, v in written.items() if k not in ("written", "summary", "read_id")
    }

    from typer.main import get_command

    verb = get_command(app).commands["io"].commands["umi-extract"]  # type: ignore[attr-defined]
    flags = {opt for param in verb.params for opt in param.opts}
    assert flags & {"--geometry", "--read-id"}
    assert not flags & {"--anchor", "--umi-offset", "--umi-length", "--trailing", "--window"}


def test_umi_extract_refuses_the_mate_rather_than_extracting_nothing_from_it(
    tmp_path: Path,
) -> None:
    """Handed the read the layout says is NOT tagged, it exits 3 instead of finding no tags.

    A composer that pairs the mates the wrong way round is the failure this catches, and it is not
    hypothetical — a units ordering that silently paired one lane's barcodes with another lane's
    cDNA is why `units.tsv` grew a lane column. Extracting from the wrong mate produces a uBAM with
    no `UB` anywhere, an empty count matrix, and exit 0 all the way down.
    """
    r1, r2 = _plate_fastqs(tmp_path)

    result = runner.invoke(
        app,
        ["io", "umi-extract", "--r1", str(r2), "--r2", str(r1), "--geometry", _plate_geometry(),
         "--read-id", "R2", "--sample", "cell_42", "--out", str(tmp_path / "cell_42.bam")],
    )  # fmt: skip

    assert result.exit_code == 3
    assert "this layout's UMI is on R1" in result.stderr


# ---- io umi-extract: the cell whose files the TABLE states (ADR-0036) ----------------------------
#
# These run the verb's OWN argument parsing, which is the layer `wiring_gate` structurally cannot
# reach: `snakemake -n -p` FORMATS every `shell:` block while planning and never runs one, so every
# arity, quoting and ordering fact in a rendered command plans clean and dies at job execution on a
# compute node, past handover. That is the layer that broke, and this is where it is held.


def _units_table(
    tmp_path: Path, rows: list[tuple[str, str, str, str]], name: str = "units.tsv"
) -> Path:
    """A units.tsv from `(run, lane, read_id, path)` rows for one cell. The columns compose writes."""
    lines = ["\t".join(("sample_id", "run", "lane", "read_id", "path"))]
    lines += ["\t".join(("cell_42", run, lane, read_id, path)) for run, lane, read_id, path in rows]
    table = tmp_path / name
    table.write_text("\n".join(lines) + "\n")
    return table


#: Three bases that say which run a read came from, written into every read's cDNA. What a uBAM
#: record was PAIRED with is then readable off the bases rather than inferred from a count — which is
#: the whole point, since two runs with equal totals and unequal per-file counts pair wrongly at exit
#: 0 under a concatenation and no count-based assertion can see it.
_RUN_MARK = {"runa": "AAA", "runb": "CCC"}


def _run_fastqs(tmp_path: Path, run: str, *, tagged: int, mate: int | None) -> None:
    """One run's files for `cell_42`: `tagged` tagged reads, and `mate` mate reads if there are any."""
    tag = "ATTGCGCAATG"
    body = "GATCACAGGTCTATCACCCTATTAACCACTCACGGGAGCTCTCCATGCATT" + _RUN_MARK[run]
    write_fastq_gz(
        tmp_path / f"{run}_R1.fastq.gz",
        [tag + "ACGTACGT" + "GGG" + body] * tagged,
        prefix=f"{run}:cell",
    )
    if mate is not None:
        write_fastq_gz(tmp_path / f"{run}_R2.fastq.gz", [body] * mate, prefix=f"{run}:cell")


def _extract(argv: list[str]) -> Result:
    """Invoke the verb with the shipped plate geometry and the standard cell/out arguments."""
    return runner.invoke(app, ["io", "umi-extract", *argv, "--geometry", _plate_geometry(),
                               "--sample", "cell_42"])  # fmt: skip


def test_umi_extract_reads_a_cell_spanning_two_runs_off_the_table_and_pairs_within_each_run(
    tmp_path: Path,
) -> None:
    """The defect this closes, at the boundary it broke on — and the pairing it is really about.

    Two runs top up one cell, which is the ordinary form of the 20 of 190 well-labelled plate
    deposits that are not strictly 1:1. Rendered as paths that expanded after a one-value option it
    was `exit 2, Got unexpected extra argument(s)`; handed the table it is one argument whatever the
    file count.

    The assertion is on CONTENT and not on counts, because counts cannot see the failure that
    matters. Each run's reads carry that run's own name in their bases, so a record paired against
    the other run's mate is visible — and it is exactly what a concatenate-then-zip implementation
    produces, silently, at a plausible size.
    """
    _run_fastqs(tmp_path, "runa", tagged=3, mate=3)
    _run_fastqs(tmp_path, "runb", tagged=2, mate=2)
    table = _units_table(tmp_path, [
        ("runb", "", "R1", str(tmp_path / "runb_R1.fastq.gz")),
        ("runa", "", "R2", str(tmp_path / "runa_R2.fastq.gz")),
        ("runa", "", "R1", str(tmp_path / "runa_R1.fastq.gz")),
        ("runb", "", "R2", str(tmp_path / "runb_R2.fastq.gz")),
    ])  # fmt: skip
    out = tmp_path / "cell_42.bam"

    result = _extract(["--units", str(table), "--out", str(out)])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["fragments"] == 5  # every fragment of BOTH files

    import pysam

    with pysam.AlignmentFile(str(out), "rb", check_sq=False) as ubam:
        records = list(ubam.fetch(until_eof=True))
    assert [r.is_read1 for r in records] == [True, False] * 5  # interleaved, tagged read first
    for tagged, mate in zip(records[::2], records[1::2], strict=True):
        assert str(tagged.query_sequence)[-3:] == str(mate.query_sequence)[-3:], (
            "a tagged read was paired with a mate from the OTHER run — which is what pairing two "
            "concatenated streams by record index does, at equal totals and exit 0"
        )
    # In the table's order (run a, then run b), which is `ordered_fastqs`' order, not the row order.
    assert [str(r.query_sequence)[-3:] for r in records[::2]] == (
        [_RUN_MARK["runa"]] * 3 + [_RUN_MARK["runb"]] * 2
    )


def test_umi_extract_refuses_two_runs_whose_totals_agree_and_whose_files_do_not(
    tmp_path: Path,
) -> None:
    """ADR-0036's worked example, and the ONLY shape that tells the two implementations apart.

    Over a well-formed cell, concatenating the two roles and zipping the streams gives exactly the
    pairing that pairing per run gives — every run holds as many mates as tagged reads, so the two
    agree file for file. They part company here:

        R1 = [runa (5 records), runb (2 records)]
        R2 = [runa (2 records), runb (5 records)]

    The totals agree at 7, so `zip_longest` yields no `None` and **no refusal fires**; every record
    past the second pairs run a's cDNA against run b's molecules. Exit 0, plausible size, wrong cell.
    Placing the files first turns it into an unequal PAIR, which the extractor already refuses — and
    the message it refuses with is the one it has always had.
    """
    _run_fastqs(tmp_path, "runa", tagged=5, mate=2)
    _run_fastqs(tmp_path, "runb", tagged=2, mate=5)
    table = _units_table(tmp_path, [
        ("runa", "", "R1", str(tmp_path / "runa_R1.fastq.gz")),
        ("runa", "", "R2", str(tmp_path / "runa_R2.fastq.gz")),
        ("runb", "", "R1", str(tmp_path / "runb_R1.fastq.gz")),
        ("runb", "", "R2", str(tmp_path / "runb_R2.fastq.gz")),
    ])  # fmt: skip

    result = _extract(["--units", str(table), "--out", str(tmp_path / "cell_42.bam")])

    assert result.exit_code == 3
    assert "runa_R2.fastq.gz" in result.stderr
    assert "paired by position" in result.stderr  # the existing refusal, applied within one pair


def test_umi_extract_refuses_a_run_whose_mate_the_table_does_not_carry(tmp_path: Path) -> None:
    """A tagged row with no mate row at its place: exit 3, naming the file, and no BAM behind it.

    The refusal fires while resolving the inputs, before the writer opens — so the failure leaves
    nothing for a downstream rule to consume. Pairing it with the OTHER run's mate instead is the
    silent alternative, and it is only ever caught by a record-count disagreement that a plate is
    under no obligation to produce.
    """
    _run_fastqs(tmp_path, "runa", tagged=3, mate=3)
    _run_fastqs(tmp_path, "runb", tagged=2, mate=None)
    table = _units_table(tmp_path, [
        ("runa", "", "R1", str(tmp_path / "runa_R1.fastq.gz")),
        ("runa", "", "R2", str(tmp_path / "runa_R2.fastq.gz")),
        ("runb", "", "R1", str(tmp_path / "runb_R1.fastq.gz")),
    ])  # fmt: skip
    out = tmp_path / "cell_42.bam"

    result = _extract(["--units", str(table), "--out", str(out)])

    assert result.exit_code == 3
    assert "runb_R1.fastq.gz" in result.stderr and "'runb'" in result.stderr
    assert not out.exists(), "a refused extraction left a uBAM for the aligner to read"


def test_umi_extract_runs_a_single_end_cell_off_a_table_that_carries_no_mate_row(
    tmp_path: Path,
) -> None:
    """No row carrying a second role IS the statement (ADR-0035): one unpaired record per fragment."""
    _run_fastqs(tmp_path, "runa", tagged=3, mate=None)
    table = _units_table(tmp_path, [("runa", "", "R1", str(tmp_path / "runa_R1.fastq.gz"))])
    out = tmp_path / "cell_42.bam"

    result = _extract(["--units", str(table), "--out", str(out)])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["fragments"] == 3

    import pysam

    with pysam.AlignmentFile(str(out), "rb", check_sq=False) as ubam:
        records = list(ubam.fetch(until_eof=True))
    assert len(records) == 3 and not any(r.is_paired for r in records)


def test_umi_extract_takes_the_table_or_the_paths_and_refuses_both_and_neither(
    tmp_path: Path,
) -> None:
    """The two forms are mutually exclusive, and each refusal is a Blocker rather than a usage error.

    Both is a caller holding two different beliefs about which files this cell is — the table states
    where each was sequenced and the paths state only an order, so there is nothing to reconcile.
    Neither is a caller who has said nothing. The repeated direct form still works, which is what
    keeps a hand invocation and a unit test possible without a table.
    """
    r1, r2 = _plate_fastqs(tmp_path)
    table = _units_table(tmp_path, [("runa", "", "R1", str(r1)), ("runa", "", "R2", str(r2))])

    both = _extract(["--units", str(table), "--r1", str(r1), "--out", str(tmp_path / "b.bam")])
    assert both.exit_code == 3
    assert "both name this cell's files" in both.stderr

    neither = _extract(["--out", str(tmp_path / "n.bam")])
    assert neither.exit_code == 3
    assert "no input files" in neither.stderr

    # The direct form, repeated, and it pairs in the order given rather than refusing an arity.
    direct = _extract(["--r1", str(r1), "--r1", str(r1), "--r2", str(r2), "--r2", str(r2),
                       "--out", str(tmp_path / "d.bam")])  # fmt: skip
    assert direct.exit_code == 0, direct.stdout + direct.stderr
    assert json.loads(direct.stdout)["fragments"] == 6  # both files, read in sequence

    lopsided = _extract(["--r1", str(r1), "--r1", str(r1), "--r2", str(r2),
                         "--out", str(tmp_path / "l.bam")])  # fmt: skip
    assert lopsided.exit_code == 3
    assert "2 --r1 files against 1 --r2 files" in lopsided.stderr


# ---------- records-only harvest, and what a collapsed member leaves on disk ----------


def _twin_records(n: int, *, prose: str = "whole worm, day3") -> object:
    """N sample records whose documents differ only in their accessions, plus their runs."""
    from seqforge.models.records import ArchiveRecord, ArchiveRecordSet, FreeText, SubmittedFile

    records = [ArchiveRecord(level="project", accession="PRJNA9")]
    for i in range(1, n + 1):
        accession = f"SAMN{str(i) * i}"
        records += [
            ArchiveRecord(
                level="sample",
                accession=accession,
                parent="PRJNA9",
                free_text=[FreeText(label="sample_alias", text=prose)],
            ),
            ArchiveRecord(level="experiment", accession=f"SRX{i}", parent=accession),
            ArchiveRecord(
                level="run",
                accession=f"SRR{i}",
                parent=f"SRX{i}",
                submitted_files=[SubmittedFile(filename=f"{accession}_1.fastq.gz")],
            ),
        ]
    return ArchiveRecordSet(source="test", query="PRJNA9", records=records)


def test_a_records_only_extraction_is_a_legal_invocation(tmp_path: Path) -> None:
    """The guard was written when a document was the only input harvest had.

    `--records` became a second one without it noticing, so `harvest extract --records dump.json
    --dry-run` exited 2 before the planner was ever called — on a dataset that is nothing but
    records, which is the shape of eleven of the eighteen benchmark packages. `plan_extraction` has
    accepted `documents=()` with records all along, and `evals/plan.py` calls it that way.
    """
    records_path = tmp_path / "records.json"
    records_path.write_text(_twin_records(3).model_dump_json())  # type: ignore[attr-defined]

    result = runner.invoke(
        app,
        ["harvest", "extract", "--records", str(records_path), "--dry-run", "-C", str(tmp_path)],
    )

    assert result.exit_code == 0, result.stdout
    plan = json.loads(result.stdout)
    assert plan["n_records_read"] == 3 and plan["n_documents"] == 1
    assert plan["documents"][0]["members"] == ["SAMN1", "SAMN22", "SAMN333"]


def test_harvest_extract_still_refuses_when_there_is_nothing_at_all_to_read(tmp_path: Path) -> None:
    """The refusal moved from "no document" to "no input", and the message names the third flag."""
    result = runner.invoke(app, ["harvest", "extract", "-C", str(tmp_path)])

    assert result.exit_code == 2
    assert "--records" in result.output and "--instruction" in result.output


def test_a_collapsed_members_bytes_and_subject_both_reach_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fanned assertion cites a document that was never sent.

    Its bytes exist nowhere else — we made them — so a span citation is checkable only while they
    survive; and `resolve` silently drops a claim whose document has no subject, so the whole
    mechanism would be lossy at the next stage if `document_subjects` listed only what was paid for.
    Both are written, and the ask still costs one request (ADR-0031).
    """
    import seqforge.harvest as harvest_pkg

    provider = _CountingProvider(per_call=7)
    monkeypatch.setattr(harvest_pkg, "resolve_provider", lambda _name=None: provider)
    records_path = tmp_path / "records.json"
    records_path.write_text(_twin_records(3).model_dump_json())  # type: ignore[attr-defined]

    result = runner.invoke(
        app, ["harvest", "extract", "--records", str(records_path), "-C", str(tmp_path)]
    )

    assert result.exit_code == 0, result.stdout
    assert provider.n_calls == 1, "three records, one ask"
    stored = json.loads((tmp_path / "seqforge" / "logs" / "assertions.json").read_text())
    placed = {(d["scope"], d["subject"]) for d in stored["document_subjects"]}
    assert placed == {("sample", "SAMN1"), ("sample", "SAMN22"), ("sample", "SAMN333")}
    written = {
        p.name.split("-")[1] for p in (tmp_path / "seqforge" / "records" / "documents").iterdir()
    }
    assert written == {"SAMN1", "SAMN22", "SAMN333"}


def test_a_records_only_compile_still_reaches_the_harvest_stage(tmp_path: Path) -> None:
    """`seqforge run` entered harvest only when a DOCUMENT was passed — the same defect `_roled`
    carried, one file over. A deposit whose whole metadata is its archive record silently skipped the
    one stage that could read it, and its manifest was then short every fact a sample record states
    in prose, with nothing in the summary saying so.

    Driven under `--no-llm`, so what is under test is the GUARD and not a model: the stage has to
    appear, announcing it was skipped by the flag, where before it was absent altogether.
    """
    spec = kb.load_spec("bulk-rnaseq")
    reads = kb.generate_reads(spec, n=600, seed=0)
    f1, f2 = tmp_path / "s_R1.fastq.gz", tmp_path / "s_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])
    from seqforge.models.records import ArchiveRecord, ArchiveRecordSet, FreeText, SubmittedFile

    records_path = tmp_path / "records.json"
    records_path.write_text(
        ArchiveRecordSet(
            source="test",
            query="PRJNA9",
            records=[
                ArchiveRecord(
                    level="sample",
                    accession="SAMN1",
                    free_text=[FreeText(label="sample_alias", text="whole worm, day3")],
                ),
                ArchiveRecord(
                    level="run",
                    accession="SRR1",
                    parent="SAMN1",
                    submitted_files=[SubmittedFile(filename=n) for n in (f1.name, f2.name)],
                ),
            ],
        ).model_dump_json()
    )

    argv = ["run", str(f1), str(f2), "--organism", "559292", "--assembly", "sacCer3",
            "--annotation", "ensembl", "--no-llm", "--fastq-dir", str(tmp_path),
            "-C", str(tmp_path)]  # fmt: skip
    without = runner.invoke(app, argv)
    with_records = runner.invoke(app, [*argv, "--records", str(records_path)])

    assert without.exit_code == 0 and with_records.exit_code == 0, with_records.stdout
    assert "harvest" not in json.loads(without.stdout)["stages"], "no prose, no stage"
    assert json.loads(with_records.stdout)["stages"]["harvest"] == {
        "skipped": "--no-llm: documents were not read"
    }, "records ARE prose, so the stage exists and says why it did not run"


@pytest.mark.parametrize("dialect", ["user", "archive"])
def test_a_record_set_with_no_prose_is_an_empty_extraction_and_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dialect: str
) -> None:
    """The shape a record set is FOR, and it used to end in a bare `AssertionError`.

    A `source: user` set declares which files compile together and never a fact, so it carries no
    free text: the plan comes back with no documents, the loop that builds the extractor never runs,
    and the verify step then asserted on the `None` it was left holding — a traceback out of a
    compiler whose whole contract is that a refusal is an exit code with a remedy. Nor is it new to
    the hand-written dialect, which is why this runs over both: an archive transcript whose records
    happen to carry no prose has always reached the identical state.

    Nothing was asked, so nothing failed: exit 0, and the same EMPTY artifact a real extraction
    writes. The artifact is the load-bearing half — `manifest fill --assertions` and `processing new`
    open that path rather than ask whether harvest had anything to say, so "no claims" and "no file"
    have to be the same thing on disk.

    **Decided before a provider is resolved**, which is what the tripwire asserts: a records-only
    compile on a machine with no credential must not fail on a credential it never needed, and a
    stage that resolved first would exit 1 with `no_provider` instead of 0 with nothing to say.
    """
    import seqforge.harvest as harvest_pkg
    from seqforge.harvest import ProviderUnavailable
    from seqforge.models.records import ArchiveRecord, ArchiveRecordSet, SubmittedFile

    def _no_provider(_name: str | None = None) -> object:
        raise ProviderUnavailable("no credential, and none should be wanted")

    monkeypatch.setattr(harvest_pkg, "resolve_provider", _no_provider)

    if dialect == "user":
        records_path = tmp_path / "records.yaml"
        records_path.write_text(
            yaml.safe_dump(
                {
                    "source": "user",
                    "query": "plateA",
                    "records": [
                        {"level": "sample", "id": "lib01"},
                        {
                            "level": "run",
                            "id": "plateA_S1",
                            "parent": "lib01",
                            "filenames": ["plateA_S1_L001_R1_001.fastq.gz"],
                        },
                    ],
                }
            )
        )
    else:
        records_path = tmp_path / "records.json"
        records_path.write_text(
            ArchiveRecordSet(
                source="test",
                query="PRJNA9",
                records=[
                    ArchiveRecord(level="sample", accession="SAMN1"),  # no free text anywhere
                    ArchiveRecord(
                        level="run",
                        accession="SRR1",
                        parent="SAMN1",
                        submitted_files=[SubmittedFile(filename="SRR1_1.fastq.gz")],
                    ),
                ],
            ).model_dump_json()
        )

    result = runner.invoke(
        app, ["harvest", "extract", "--records", str(records_path), "-C", str(tmp_path)]
    )

    assert result.exit_code == 0, result.stdout + str(result.stderr or "")
    payload = json.loads(result.stdout)
    assert payload["n_drafts"] == payload["n_accepted"] == payload["n_stored"] == 0
    assert payload["assertions"] == payload["conflicts"] == payload["rejected"] == []
    assert "no_documents" in payload, "a row of zeros does not say WHY there was nothing to read"
    stored = json.loads((tmp_path / "seqforge" / "logs" / "assertions.json").read_text())
    assert stored == {"instruction_docs": [], "document_subjects": [], "assertions": []}, (
        "the same artifact a real run writes, empty — the next stage opens a path, not a payload"
    )


def test_a_structure_only_records_compile_reaches_the_manifest_with_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The feature's primary use case, end to end, and without the caller knowing to pass a flag.

    An in-house dataset has no accession and no paper: its whole metadata is the grouping a human
    typed, which is exactly what a `source: user` record set is. `run` counts `--records` as prose
    and enters the LLM stage for it — correctly, since an archive transcript IS prose — but a
    hand-written set has none, so that stage found nothing to ask and used to take the whole compile
    down with it. The dataset the record set exists for could not be compiled unless its author
    happened to know to add `--no-llm`.

    So this is driven WITHOUT `--no-llm`, with no provider reachable, and it must still reach
    `manifest fill` and everything after it. And the fuse has to land: two runs the filenames kept
    apart compile into one `lib01`, with the warning that says the grouping was declared.
    """
    import seqforge.harvest as harvest_pkg
    from seqforge.harvest import ProviderUnavailable

    def _no_provider(_name: str | None = None) -> object:
        raise ProviderUnavailable("no credential, and none should be wanted")

    monkeypatch.setattr(harvest_pkg, "resolve_provider", _no_provider)

    spec = kb.load_spec("bulk-rnaseq")
    # A seed per batch: two runs of one library are two different sets of reads, and identical bytes
    # would be one content-addressed file claimed by two runs rather than the fuse under test.
    for batch, seed in (("S1", 0), ("S3", 1)):
        reads = kb.generate_reads(spec, n=600, seed=seed)
        for mate in ("R1", "R2"):
            write_fastq_gz(tmp_path / f"lib_{batch}_{mate}.fastq.gz", reads[mate])
    records_path = tmp_path / "records.yaml"
    records_path.write_text(
        yaml.safe_dump(
            {
                "source": "user",
                "query": "lib01",
                "records": [
                    {"level": "sample", "id": "lib01"},
                    *(
                        {
                            "level": "run",
                            "id": f"lib_{batch}",
                            "parent": "lib01",
                            "filenames": [f"lib_{batch}_R1.fastq.gz", f"lib_{batch}_R2.fastq.gz"],
                        }
                        for batch in ("S1", "S3")
                    ),
                ],
            }
        )
    )

    result = runner.invoke(
        app,
        ["run", *sorted(str(p) for p in tmp_path.glob("*.fastq.gz")),
         "--organism", "559292", "--assembly", "sacCer3", "--annotation", "ensembl",
         "--records", str(records_path), "--fastq-dir", str(tmp_path), "-C", str(tmp_path)],
    )  # fmt: skip

    assert result.exit_code == 0, result.stdout
    summary = json.loads(result.stdout)
    assert summary["ok"] is True
    stages = summary["stages"]
    assert "manifest" in stages, "the compile got past harvest, which is the whole point"
    assert set(stages) == {"records", "harvest", "manifest", "processing", "compose", "project",
                           "report"}  # fmt: skip
    assert stages["harvest"]["n_stored"] == 0, "structure only: there was nothing to harvest"
    assert "no_documents" in stages["harvest"], "and the summary says so rather than implying it"
    assert (tmp_path / "seqforge" / "manifest.yaml").is_file()
    assert (tmp_path / summary["snakefile"]).is_file()

    units = ((tmp_path / summary["snakefile"]).parent / "units.tsv").read_text().splitlines()
    assert {line.split("\t")[0] for line in units[1:] if line.strip()} == {"lib01"}, (
        "the declared fuse is what compiles: four files, one matrix"
    )
    assert len([line for line in units[1:] if line.strip()]) == 4


# ---------------------------------------------------------------------------------------------
# The submitted-file transcript where a HUMAN meets it (ADR-0033). Four remedies now point at
# `io records`, so what that verb prints is a contract: if the `sra-pub-src-*` URI does not come
# out here, every one of those pointers dead-ends.
# ---------------------------------------------------------------------------------------------


def _one_submitted_run() -> object:
    """A record set of one run declaring one submitted file, with all four fields populated.

    The values are ADR-0033's own worked example, so a reader can put the printed JSON next to the
    `<SRAFile>` element it came from.
    """
    from seqforge.models.records import ArchiveRecord, ArchiveRecordSet, SubmittedFile

    return ArchiveRecordSet(
        source="ncbi-sra+biosample",
        query="SRR19886090",
        records=[
            ArchiveRecord(
                level="run",
                accession="SRR19886090",
                submitted_files=[
                    SubmittedFile(
                        filename="NasalProx1_270_2.fastq.gz",
                        md5="993e02dd8079b30a23285828a8ee9982",
                        size_bytes=28543057,
                        uri="s3://sra-pub-src-15/SRR19886090/NasalProx1_270_2.fastq.gz.1",
                    )
                ],
            )
        ],
    )


def test_io_records_prints_each_submitted_files_md5_size_and_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one surface where the concrete bucket URI reaches a person.

    The blockers that send people here cannot carry it — they are byte-side, and a record set does
    not enter `score` (ADR-0033) — so this verb is the whole of that pointer's payoff. Printing the
    filename alone would leave a reader exactly where the old "may exist via the SDL API" left them.
    """
    import seqforge.io.archive as archive

    monkeypatch.setattr(archive, "fetch_records", lambda _acc: _one_submitted_run())

    result = runner.invoke(app, ["io", "records", "SRR19886090", "-C", str(tmp_path)])

    assert result.exit_code == 0, result.stdout
    out = json.loads(result.stdout)
    assert out["submitted_files"] == [
        {
            "run": "SRR19886090",
            "filename": "NasalProx1_270_2.fastq.gz",
            "md5": "993e02dd8079b30a23285828a8ee9982",
            "size_bytes": 28543057,
            "uri": "s3://sra-pub-src-15/SRR19886090/NasalProx1_270_2.fastq.gz.1",
        }
    ]


def test_a_freshly_fetched_record_set_is_stamped_and_one_off_disk_keeps_what_it_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`manifest fill --accession` merges fresh records into a set of its own, and it wrote no stamp.

    An unstamped set means "written before submitted files existed", which is why a run declaring
    none can still be trusted to publish none (ADR-0033) — so a set assembled around records fetched
    a second ago must say so, or the freshest possible fetch reads as the stalest possible cache.
    `--records`, by contrast, hands back what is on disk: re-stamping a file this process did not
    fetch would forge the signature the staleness check reads.
    """
    import seqforge.io.archive as archive
    from seqforge.cli import manifest as m
    from seqforge.io import IO_VERSION

    monkeypatch.setattr(archive, "fetch_records", lambda _acc: _one_submitted_run())

    fetched = m._load_records(["SRR19886090"], None, offline=False)
    assert fetched is not None and fetched.io_version == IO_VERSION

    stale = tmp_path / "records.json"
    stale.write_text(_one_submitted_run().model_dump_json())  # type: ignore[attr-defined]
    loaded = m._load_records([], stale, offline=False)
    assert loaded is not None and loaded.io_version is None


# ---------------------------------------------------------------------------------------------
# `seqforge records` — the record set as a CLI surface. Top-level and never under `io`: both verbs
# read a local directory and a local file, and `io` is the one group where a reader is entitled to
# assume a network call. `io records <accession>` fetches a transcript and stays exactly where it is.
#
# What is under test here is the SURFACE and its refusals; the loader's own dialect rules are
# `tests/test_recordset.py`'s. The one property that spans both is the draft's no-op guarantee, and
# it is asserted here because that is where the file the verb writes actually lands on disk.
# ---------------------------------------------------------------------------------------------


def _fastq_dir(tmp_path: Path, *stems: str) -> Path:
    """A directory of paired FASTQ named `<stem>_R1_001.fastq.gz` / `<stem>_R2_001.fastq.gz`.

    Real gzip rather than touched files: nothing in `records new` reads a byte today, and a fixture
    that would stop being a FASTQ the moment something did is one that quietly bounds what this block
    can grow into.
    """
    directory = tmp_path / "fastq"
    directory.mkdir(exist_ok=True)
    for stem in stems:
        for mate in ("R1", "R2"):
            write_fastq_gz(directory / f"{stem}_{mate}_001.fastq.gz", ["ACGTACGTAC"])
    return directory


def test_records_new_drafts_a_set_the_loader_and_validate_both_accept(tmp_path: Path) -> None:
    """The draft's first obligation: what it prints must load, and must name the open decision.

    `lib_S1` and `lib_S3` are two libraries on one flowcell or one library resequenced for depth, and
    the filenames cannot tell those apart — so the draft has to put both keys in front of a human
    rather than pick. The scan running and finding nothing looks identical to no scan at all, which is
    why the comment being present is asserted rather than assumed.
    """
    from seqforge.recordset import load_record_set

    directory = _fastq_dir(tmp_path, "lib_S1", "lib_S3")
    drafted = runner.invoke(app, ["records", "new", str(directory)])
    assert drafted.exit_code == 0, drafted.stdout

    flagged = [line for line in drafted.stdout.splitlines() if line.lstrip().startswith("#")]
    assert any("lib_S1" in line for line in flagged) and any(
        "lib_S3" in line for line in flagged
    ), "the _S<n> pair is the decision a filename cannot take; the draft must name both runs"

    path = tmp_path / "records.yaml"
    path.write_text(drafted.stdout)
    loaded = load_record_set(path)
    assert loaded.source == "user"
    assert [r.accession for r in loaded.at("run")] == ["lib_S1", "lib_S3"]

    validated = runner.invoke(app, ["records", "validate", str(path)])
    assert validated.exit_code == 0, validated.stdout
    summary = json.loads(validated.stdout)["summary"]
    assert summary["n"] == {"project": 0, "sample": 0, "experiment": 0, "run": 2}
    assert summary["n_filenames"] == 4, "every file in the directory is claimed by exactly one run"
    assert summary["fused"] == {}, "a draft declares no sample, so it fuses nothing"


def test_the_draft_applied_unedited_produces_the_samples_the_filenames_already_did(
    tmp_path: Path,
) -> None:
    """The property that makes it safe to write this file into somebody's dataset directory.

    A draft that could move a sample identity would be a guess wearing a file's clothes — and sample
    identity is inside `dataset_hash`, which is never rewritten. So the drafted set is run through the
    real metadata resolver and its samples compared against the same files resolved with no record set
    at all. Equal ids AND equal file membership, because a grouping that agrees on names while moving
    a file between them is the failure this is about.
    """
    from seqforge.models.observation import FileIdentity
    from seqforge.recordset import load_record_set
    from seqforge.resolve.records import resolve_metadata

    directory = _fastq_dir(tmp_path, "lib_S1", "lib_S3")
    out = tmp_path / "records.yaml"
    written = runner.invoke(app, ["records", "new", str(directory), "-o", str(out)])
    assert written.exit_code == 0, written.stdout

    files = [
        FileIdentity(sha256=f"{i:064x}", size_bytes=path.stat().st_size, basename=path.name)
        for i, path in enumerate(sorted(directory.iterdir()))
    ]
    by_filename = resolve_metadata(files=files)
    by_draft = resolve_metadata(files=files, records=load_record_set(out))

    assert [(s.sample_id, s.file_shas) for s in by_draft.samples] == [
        (s.sample_id, s.file_shas) for s in by_filename.samples
    ]
    assert not by_draft.blockers, "the draft claims every file the directory holds"
    assert not by_draft.warnings, "it fuses nothing, so there is nothing for it to report fusing"


def test_records_validate_refuses_a_typed_attribute_and_names_the_key(tmp_path: Path) -> None:
    """A hand-written set declares structure, never a fact — and the refusal has to say which key.

    An attribute typed here carries no quote, no span and nothing that greps back, yet it would
    outrank a harvested claim carrying all three, permanently. So this is a Blocker at exit 3 rather
    than a dropped key, and the stdout object names the offending field: a refusal a caller cannot act
    on is a refusal that gets routed around.
    """
    path = tmp_path / "records.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "source": "user",
                "records": [
                    {
                        "level": "run",
                        "id": "lib",
                        "filenames": ["lib_R1_001.fastq.gz"],
                        "attributes": [{"name": "strain", "value": "CQ758"}],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["records", "validate", str(path)])

    assert result.exit_code == 3, result.stdout
    out = json.loads(result.stdout)
    assert out["records"] == str(path)
    assert out["report"]["ok"] is False
    assert out["summary"] is None, "there is nothing truthful to say about a file that was refused"
    blockers = out["report"]["blockers"]
    assert any(b["subject"]["ref"] == "records[0].attributes" for b in blockers), blockers
    assert any("attributes" in b["evidence"] for b in blockers), blockers
    assert any("harvest" in b["remedy"] for b in blockers), (
        "it must name the path that keeps a span"
    )


def test_records_new_refuses_a_directory_with_no_fastq_and_prints_no_traceback(
    tmp_path: Path,
) -> None:
    """A directory with nothing to declare is a refusal, not a stack trace out of a drafter.

    stdout stays empty because on this branch there is no result object: the YAML never existed. The
    exit code is the refusal channel, and the human stream carries the remedy.
    """
    empty = tmp_path / "empty"
    empty.mkdir()

    result = runner.invoke(app, ["records", "new", str(empty)])

    assert result.exit_code == 3, result.stdout
    assert result.stdout == "", "stdout carries the drafted YAML or nothing at all"
    assert "Traceback" not in result.stderr
    assert "blk-record-set-no-fastq" in result.stderr
    assert "remedy:" in result.stderr, "a message with no remedy leaves a caller with nowhere to go"


def test_records_new_refuses_to_clobber_its_out_file_and_takes_force_to_replace_it(
    tmp_path: Path,
) -> None:
    """`records new` writes into a directory the caller chose, so it must not silently replace.

    The file this verb drafts is the one file in the compiler a human then EDITS, and the second run
    of the same command is exactly how somebody would lose that edit. Refused as a bad invocation —
    nothing about the data is wrong — and `--force` is the same escape hatch `hook install` already
    uses for the same shape of clobber.
    """
    directory = _fastq_dir(tmp_path, "lib_S1")
    out = tmp_path / "records.yaml"
    out.write_text("# the grouping somebody decided\n")

    refused = runner.invoke(app, ["records", "new", str(directory), "-o", str(out)])

    assert refused.exit_code == 2, refused.stdout
    assert out.read_text() == "# the grouping somebody decided\n", "the edit survives the refusal"
    assert "--force" in refused.stderr, "a refusal must name the flag that clears it"

    forced = runner.invoke(app, ["records", "new", str(directory), "-o", str(out), "--force"])

    assert forced.exit_code == 0, forced.stdout
    assert json.loads(forced.stdout)["records"] == str(out)
    assert "source: user" in out.read_text()


def test_records_is_a_top_level_group_and_io_records_is_left_where_it_was() -> None:
    """The group's placement is the decision, so it is pinned against the live app rather than prose.

    Two verbs at the top level because neither touches the network, and `io records` untouched because
    fetching a transcript from an archive is exactly what `io` is for. Introspected, never listed: a
    hand-written surface is the shape this repo keeps finding rotted.
    """
    from typer.main import get_command

    top = cast(dict[str, Any], getattr(get_command(app), "commands", {}))

    assert set(getattr(top["records"], "commands", {})) == {"new", "validate"}
    assert "records" in getattr(top["io"], "commands", {}), "io records is a fetch and stays there"
