"""Tests for ``seqforge.workflows`` — what each hand-written module hands back.

One file per package, so an agent editing ``workflows/`` knows which file to run. This was four
per-submodule files (``test_h5ad``/``test_qc``/``test_cram``/``test_fragments``) for one package,
named after the PR that added them rather than the module they cover.

Two views of ``workflows`` live here. What each hand-written module IS — the registry
(``MODULES``/``list_modules``) and the ``.smk`` SOURCE invariants read off the module text (no
generated rule source, the modules are hand-written, the derived ``required_config``/``parse_keys``,
the per-pipeline param block, the aligner name) — moved here from the deleted ``test_compile.py``,
because reading ``MODULES`` and the module text is the natural neighbour of what the modules DO once
snakemake runs them (h5ad/qc/cram/fragments). The composer's own view — the emitted config and the
params gate — is ``test_compose.py``.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import get_args

import anndata as ad
import pytest
from scipy.sparse import csr_matrix

from conftest import SrcTrees, _build, _processing, _src_root
from seqforge import kb
from seqforge.compose import compose, core
from seqforge.models.processing import RuntimeEnv, SoloFeature
from seqforge.workflows import WORKFLOW_VERSION, get_module, keys_read_by, list_modules
from seqforge.workflows.cram import CramError, _sort_mem_per_thread_mb, bam_to_cram
from seqforge.workflows.fragments import (
    FragmentsError,
    build_fragments_qc,
    fragments_suffixes,
    write_fragments,
    write_fragments_qc,
)
from seqforge.workflows.h5ad import (
    SOLO_FEATURE_OUTPUT,
    STAR_BAM,
    STAR_LOG_FILES,
    H5adError,
    h5ad_suffixes,
    raw_files,
    solo_filtered_files,
    solo_raw_files,
    solo_stats_files,
    write_h5ad,
)
from seqforge.workflows.qc import QcError, build_qc_bundle, write_qc_bundle

# ================================================================================================
# h5ad — packaging Solo.out as the deliverable
# ================================================================================================
#
# ``.h5ad`` packaging — the pilot's deliverable format.
#
# The gates here are the two that can fail *silently*: the feature table going stale as
# ``SoloFeature`` grows, and four features being stacked onto axes that are not the same axes.


def _layer_names(adata: ad.AnnData) -> set[str]:
    """`adata.layers` carries a `None` key: anndata's alias for `X`. It is not a stray layer."""
    return {k for k in adata.layers.keys() if k is not None}


def _counts(adata: ad.AnnData, layer: str | None = None) -> csr_matrix:
    """One count matrix, narrowed to what an ``.mtx`` round-tripped through anndata actually is.

    ``X`` and ``layers[...]`` are declared as a union of array protocols — a dense array, a lazy
    on-disk dataset, ``None`` — so a bare ``[i, j]`` on either reads through something that may not
    be a matrix at all. Packaging writes sparse, so a dense or absent one is a regression this says
    out loud rather than an index error three lines later.
    """
    matrix = adata.X if layer is None else adata.layers[layer]
    assert isinstance(matrix, csr_matrix), f"expected a sparse count matrix, got {type(matrix)}"
    return matrix


GENES = ["ENSG01", "ENSG02", "ENSG03"]
BARCODES = ["AAAA", "CCCC"]


def _mtx(path: Path, entries: dict[tuple[int, int], int], n_genes: int, n_cells: int) -> None:
    """Write STARsolo's shape: rows are GENES, columns are BARCODES, both 1-based."""
    lines = [
        "%%MatrixMarket matrix coordinate integer general",
        "%",
        f"{n_genes} {n_cells} {len(entries)}",
    ]
    lines += [f"{g} {c} {v}" for (g, c), v in sorted(entries.items())]
    path.write_text("\n".join(lines) + "\n")


def _feature_dir(
    solo: Path,
    feature: SoloFeature,
    *,
    genes: list[str] = GENES,
    barcodes: list[str] = BARCODES,
    base: int,
) -> None:
    """One ``Solo.out/<feature>/raw/``. ``base`` makes each feature's counts distinguishable."""
    raw = solo / feature / "raw"
    raw.mkdir(parents=True)
    raw.joinpath("features.tsv").write_text(
        "".join(f"{g}\t{g}-name\tGene Expression\n" for g in genes)
    )
    raw.joinpath("barcodes.tsv").write_text("".join(f"{b}\n" for b in barcodes))
    entries = {(1, 1): base + 1, (2, 2): base + 2}
    for name in SOLO_FEATURE_OUTPUT[feature].matrices:
        _mtx(raw / name, entries, len(genes), len(barcodes))


def _solo_out(tmp_path: Path, features: list[SoloFeature], **kwargs: list[str]) -> Path:
    solo = tmp_path / "Solo.out"
    for i, feature in enumerate(features):
        _feature_dir(solo, feature, base=i * 10, **kwargs)
    return solo


def test_every_solo_feature_is_classified() -> None:
    """A new ``SoloFeature`` must say what STAR writes for it, or the h5ad step ignores it silently.

    Collected from ``SoloFeature`` itself, so a new member is covered *because it exists* rather than
    because someone remembered — the same discipline the KB's roundtrip tests use. Without this, the
    failure mode is: someone adds a feature, policy counts it, STAR writes it, and it never reaches
    an .h5ad. No error anywhere; just a matrix that is not there.
    """
    assert set(SOLO_FEATURE_OUTPUT) == set(get_args(SoloFeature))


def test_velocyto_has_no_matrix_mtx_and_the_others_have_nothing_else() -> None:
    """Checked against real STARsolo output on 2026-07-15, because I had assumed otherwise."""
    assert SOLO_FEATURE_OUTPUT["Velocyto"].matrices == (
        "spliced.mtx",
        "unspliced.mtx",
        "ambiguous.mtx",
    )
    assert "matrix.mtx" not in SOLO_FEATURE_OUTPUT["Velocyto"].matrices
    for feature, out in SOLO_FEATURE_OUTPUT.items():
        if feature != "Velocyto":
            assert out.matrices == ("matrix.mtx",)


def test_the_axis_files_are_demanded_for_every_feature() -> None:
    """`features.tsv`/`barcodes.tsv` are outputs too: a matrix without its axes is unreadable."""
    for feature in SOLO_FEATURE_OUTPUT:
        assert set(raw_files(feature)) >= {"features.tsv", "barcodes.tsv"}
    assert "Gene/raw/matrix.mtx" in solo_raw_files(["Gene"])


def test_sj_yields_no_h5ad_but_the_gene_features_still_do() -> None:
    """SJ's var axis is splice junctions, so it is not a layer of a gene object at any price."""
    from seqforge.manifest.policy import DEFAULT_SOLO_FEATURES

    assert h5ad_suffixes(["SJ"]) == []
    assert h5ad_suffixes(["Gene", "SJ"]) == [".h5ad"]
    assert h5ad_suffixes(["Gene", "Velocyto"]) == [".h5ad", ".velocyto.h5ad"]
    # The shipped default (all five features) is one more row of the same table: two files.
    assert h5ad_suffixes(list(DEFAULT_SOLO_FEATURES)) == [".h5ad", ".velocyto.h5ad"]


def test_stats_files_are_per_feature_but_umi_per_cell_only_for_the_stackable_ones() -> None:
    """The finalize temp() declaration must match what STAR writes exactly, or the rule fails.

    Every feature gets a Summary.csv + Features.stats; Barcodes.stats is once at the top; but only the
    cell-filtered gene features (Gene/GeneFull*) get a UMIperCellSorted knee vector — Velocyto and SJ
    do not (confirmed against real output). Over-declaring a file STAR never wrote breaks the run.
    """
    features: list[SoloFeature] = ["Gene", "GeneFull", "Velocyto"]
    stats = solo_stats_files(features)
    assert "Barcodes.stats" in stats
    for feat in features:
        assert f"{feat}/Summary.csv" in stats
        assert f"{feat}/Features.stats" in stats
    assert "Gene/UMIperCellSorted.txt" in stats
    assert "GeneFull/UMIperCellSorted.txt" in stats
    assert "Velocyto/UMIperCellSorted.txt" not in stats
    # SJ (junction axis) is not cell-filtered, so it gets no knee vector either.
    assert "SJ/UMIperCellSorted.txt" not in solo_stats_files(["Gene", "SJ"])


def test_filtered_files_cover_every_gene_axis_feature_but_not_sj() -> None:
    """STAR writes a filtered/ copy for each gene-axis feature (incl. Velocyto's three matrices).

    We declare only what real output confirms; SJ's filtered layout is unconfirmed, so it is left out
    — under-declaring merely leaves a file uncleaned, while over-declaring is a hard rule failure.
    """
    filtered = solo_filtered_files(["Gene", "Velocyto", "SJ"])
    assert "Gene/filtered/matrix.mtx" in filtered
    assert "Gene/filtered/barcodes.tsv" in filtered
    # Velocyto's filtered dir carries the same three matrices as raw, plus the axis files.
    assert "Velocyto/filtered/spliced.mtx" in filtered
    assert "Velocyto/filtered/ambiguous.mtx" in filtered
    assert "Velocyto/filtered/barcodes.tsv" in filtered
    assert not any(f.startswith("SJ/") for f in filtered)


def test_star_run_files_are_the_logs_the_bundle_reads_and_the_bam_is_separate() -> None:
    """The log/table set feeds qc_bundle; the BAM is its own constant (solo_to_cram consumes it)."""
    assert set(STAR_LOG_FILES) == {"Log.final.out", "Log.out", "Log.progress.out", "SJ.out.tab"}
    assert STAR_BAM == "Aligned.out.bam"
    assert STAR_BAM not in STAR_LOG_FILES


def test_write_h5ad_writes_exactly_what_h5ad_suffixes_promised(tmp_path: Path) -> None:
    """One function decides both what the rule declares and what the verb writes (no drift)."""
    features: list[SoloFeature] = ["Gene", "GeneFull", "Velocyto"]
    solo = _solo_out(tmp_path, features)
    written = write_h5ad(solo, features, "Gene", tmp_path / "s1")
    assert [p.name for p in written] == [f"s1{s}" for s in h5ad_suffixes(features)]
    assert all(p.exists() for p in written)


def test_the_primary_feature_is_x_and_the_rest_are_layers(tmp_path: Path) -> None:
    features: list[SoloFeature] = ["Gene", "GeneFull", "GeneFull_Ex50pAS"]
    solo = _solo_out(tmp_path, features)
    write_h5ad(solo, features, "GeneFull", tmp_path / "s1")
    adata = ad.read_h5ad(tmp_path / "s1.h5ad")

    assert adata.uns["primary_feature"] == "GeneFull"
    assert _layer_names(adata) == {"Gene", "GeneFull_Ex50pAS"}
    # `_feature_dir` gives feature i the counts (base+1, base+2) with base=10*i, so which matrix
    # landed in X is checkable rather than merely plausible: GeneFull is features[1] => base 10.
    assert _counts(adata)[0, 0] == 11
    assert _counts(adata, "Gene")[0, 0] == 1
    assert _counts(adata, "GeneFull_Ex50pAS")[0, 0] == 21


def test_the_matrix_is_transposed_to_cells_by_genes_and_keeps_the_gene_name_column(
    tmp_path: Path,
) -> None:
    """STARsolo writes genes x barcodes; AnnData is cells x genes — and the gene-name column survives.

    Getting the transpose backwards yields an object that opens, plots, and is wrong — and with a
    square matrix it would not even be a shape error. Three genes and two cells on purpose. The same
    read confirms `var["gene_name"]`/`feature_type` carried through from features.tsv.
    """
    solo = _solo_out(tmp_path, ["Gene"])
    write_h5ad(solo, ["Gene"], "Gene", tmp_path / "s1")
    adata = ad.read_h5ad(tmp_path / "s1.h5ad")

    assert adata.shape == (len(BARCODES), len(GENES))
    assert list(adata.obs_names) == BARCODES
    assert list(adata.var_names) == GENES
    # entry (2, 2) = gene 2, cell 2 in STAR's file -> obs 1, var 1 here
    assert _counts(adata)[1, 1] == 2
    # The gene-name column and feature type survive from features.tsv.
    assert list(adata.var["gene_name"]) == [f"{g}-name" for g in GENES]
    assert set(adata.var["feature_type"]) == {"Gene Expression"}


def test_velocyto_carries_three_layers_x_is_spliced_and_is_not_a_gene_layer(tmp_path: Path) -> None:
    """One `write_h5ad`, both deliverables read: the velocyto object AND the plain gene object.

    The `.velocyto.h5ad` carries spliced/unspliced/ambiguous as three layers with X duplicating
    spliced (scVelo reads the layer, everything else reads X). Those three matrices only mean anything
    together, so they are NOT a fourth way to count genes: the plain `.h5ad` has no `Velocyto` layer.
    """
    solo = _solo_out(tmp_path, ["Gene", "Velocyto"])
    write_h5ad(solo, ["Gene", "Velocyto"], "Gene", tmp_path / "s1")

    velo = ad.read_h5ad(tmp_path / "s1.velocyto.h5ad")
    assert _layer_names(velo) == {"spliced", "unspliced", "ambiguous"}
    assert velo.shape == (len(BARCODES), len(GENES))
    # X duplicates layers["spliced"] on purpose: scVelo reads the layer, everything else reads X.
    assert (_counts(velo) != _counts(velo, "spliced")).nnz == 0

    gene = ad.read_h5ad(tmp_path / "s1.h5ad")
    assert "Velocyto" not in gene.layers  # not a fourth way to count genes


def test_stacking_refuses_features_whose_axes_disagree(tmp_path: Path) -> None:
    """THE assertion. Mismatched axes silently misalign every layer but the first.

    Each count would land on the wrong gene, in an object that opens fine and plots fine — the
    silent-plausible-wrong class this whole project is built against. So it is a refusal, not a
    warning, and it compares BYTES rather than trusting the table that says these axes should match.
    """
    solo = tmp_path / "Solo.out"
    _feature_dir(solo, "Gene", base=0)
    _feature_dir(solo, "GeneFull", genes=[*GENES, "ENSG04"], base=10)  # a fourth gene: shifted axis

    with pytest.raises(H5adError, match="GeneFull"):
        write_h5ad(solo, ["Gene", "GeneFull"], "Gene", tmp_path / "s1")
    assert not (tmp_path / "s1.h5ad").exists(), (
        "a refusal must not leave a half-written deliverable"
    )


def test_a_missing_matrix_is_a_refusal_not_an_empty_object(tmp_path: Path) -> None:
    """An exit-0 STAR run that wrote only some features must not become a thinner h5ad."""
    solo = _solo_out(tmp_path, ["Gene", "GeneFull"])
    (solo / "GeneFull" / "raw" / "matrix.mtx").unlink()

    with pytest.raises(H5adError, match="missing"):
        write_h5ad(solo, ["Gene", "GeneFull"], "Gene", tmp_path / "s1")


def test_a_primary_that_is_not_stackable_falls_back_rather_than_crashing(tmp_path: Path) -> None:
    """`soloFeatures[0]` names the primary, and nothing stops it being Velocyto or SJ."""
    features: list[SoloFeature] = ["Velocyto", "Gene"]
    solo = _solo_out(tmp_path, features)
    write_h5ad(solo, features, "Velocyto", tmp_path / "s1")
    assert ad.read_h5ad(tmp_path / "s1.h5ad").uns["primary_feature"] == "Gene"


# ================================================================================================
# qc — the STAR stats bundle
# ================================================================================================
#
# The STAR stats bundle — one gzipped JSON per sample, built from a dozen scattered text files.
#
# The gate that matters: the bundle is built from exactly the files the ``qc_bundle`` rule hands it
# (the ``temp()`` outputs), every value round-trips through gzipped JSON, and a missing file is a loud
# refusal rather than a silent gap — because once the raw matrices are gone this bundle is the only
# surviving record of the run's QC.


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _fake_run(tmp_path: Path, features: list[SoloFeature]) -> tuple[Path, Path]:
    """A minimal STARsolo output tree: Solo.out + the top-level logs. Returns (solo_dir, run_dir)."""
    run_dir = tmp_path / "S1"
    solo = run_dir / "Solo.out"
    _write(solo / "Barcodes.stats", "nNoAdapter\t0\nnMatch\t900\n")
    for feat in features:
        _write(solo / feat / "Summary.csv", "Number of Reads,1000\nSequencing Saturation,0.42\n")
        _write(solo / feat / "Features.stats", "noUnmapped 5\nyesWLmatch 900\n")
        # filtered/ exists for gene-axis features; here all of these are gene-axis.
        _write(solo / feat / "filtered" / "barcodes.tsv", "AAAA\nCCCC\n")
    # UMIperCellSorted only for the stackable (single-matrix gene) features, not Velocyto.
    for feat in features:
        if feat != "Velocyto":
            _write(solo / feat / "UMIperCellSorted.txt", "50\n30\n10\n")
    _write(
        run_dir / "Log.final.out",
        "     Number of input reads |\t1000\n  Uniquely mapped % |\t95.00%\n",
    )
    _write(run_dir / "Log.out", "STAR version 2.7.11b\nstarted mapping\n")
    _write(run_dir / "Log.progress.out", "Time Speed Read Mapped\n")
    _write(run_dir / "SJ.out.tab", "chrI\t100\t200\t1\t1\t1\t10\t0\t30\n")
    return solo, run_dir


def test_the_bundle_carries_every_stat_and_log_keyed_by_feature(tmp_path: Path) -> None:
    features: list[SoloFeature] = ["Gene", "GeneFull", "Velocyto"]
    solo, run_dir = _fake_run(tmp_path, features)
    bundle = build_qc_bundle(solo, run_dir, features, sample="S1", assembly="ce11")

    assert bundle["sample"] == "S1"
    assert bundle["assembly"] == "ce11"
    assert bundle["soloFeatures"] == features
    # The bundle is JSON on its way to a gzip file, so every entry arrives as an untyped value. Each
    # block is narrowed once here, which is also the shape claim: a block that stopped being keyed by
    # feature fails by name rather than as an index error on the line that reads it.
    summary, features_stats = bundle["summary"], bundle["features_stats"]
    umi_per_cell, filtered = bundle["umi_per_cell"], bundle["default_filtered_barcodes"]
    barcodes_stats, log_final = bundle["barcodes_stats"], bundle["log_final"]
    log_out, splice_junctions = bundle["log_out"], bundle["splice_junctions"]
    assert isinstance(summary, dict) and isinstance(features_stats, dict)
    assert isinstance(umi_per_cell, dict) and isinstance(filtered, dict)
    assert isinstance(barcodes_stats, dict) and isinstance(log_final, dict)
    assert isinstance(log_out, str) and isinstance(splice_junctions, list)

    # Summary.csv coerced to typed values, per feature.
    assert summary["Gene"]["Number of Reads"] == 1000
    assert summary["Gene"]["Sequencing Saturation"] == 0.42
    # Whitespace .stats files.
    assert barcodes_stats["nMatch"] == 900
    assert features_stats["Velocyto"]["yesWLmatch"] == 900
    # UMIperCellSorted only for the stackable features.
    assert umi_per_cell["Gene"] == [50, 30, 10]
    assert "Velocyto" not in umi_per_cell
    # filtered/barcodes.tsv kept as provenance of STAR's default cell call, for every gene-axis feat.
    assert filtered["Velocyto"] == ["AAAA", "CCCC"]
    # Log.final.out parsed on `|`; free-text logs kept whole; SJ rows split on tab.
    assert log_final["Number of input reads"] == 1000
    assert log_final["Uniquely mapped %"] == "95.00%"
    assert "STAR version" in log_out
    assert splice_junctions[0] == ["chrI", "100", "200", "1", "1", "1", "10", "0", "30"]


def test_write_qc_bundle_round_trips_through_gzipped_json(tmp_path: Path) -> None:
    features: list[SoloFeature] = ["Gene", "Velocyto"]
    solo, run_dir = _fake_run(tmp_path, features)
    out = tmp_path / "S1.qc.json.gz"
    written = write_qc_bundle(solo, run_dir, features, out, sample="S1", assembly="ce11")

    assert written == out and out.exists()
    with gzip.open(out, "rt", encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded["sample"] == "S1"
    assert loaded["summary"]["Gene"]["Number of Reads"] == 1000
    assert loaded["umi_per_cell"]["Gene"] == [50, 30, 10]


def test_a_missing_star_file_is_a_refusal_not_a_silent_gap(tmp_path: Path) -> None:
    features: list[SoloFeature] = ["Gene"]
    solo, run_dir = _fake_run(tmp_path, features)
    (solo / "Gene" / "Summary.csv").unlink()
    with pytest.raises(QcError, match="Summary.csv"):
        build_qc_bundle(solo, run_dir, features, sample="S1", assembly="ce11")


# ================================================================================================
# cram — BAM -> CRAM finalize
# ================================================================================================
#
# BAM -> CRAM finalize. The gates: the reference is passed (never embedded), the sort is
# multi-threaded with a real memory budget (never samtools' single-thread default), and the reference
# FASTA is resolved from the assembly id rather than baked as a path.
#
# samtools is stubbed so the test needs no binary and no genome: what is asserted is the *argv* we hand
# it, which is where the correctness lives (a stray ``embed_ref``, a missing ``-T``, a single thread).


def test_sort_memory_is_split_across_threads_with_headroom() -> None:
    """More cores and more memory both shrink per-thread ``-m`` sensibly; a floor stops thrashing."""
    # 32 GB budget, 8 threads -> 3/4 headroom, split 8 ways. The expected value is a LITERAL: it
    # used to be `(32768 * 3 // 4) // 8`, which is the function's own formula and so could not fail.
    assert _sort_mem_per_thread_mb(32768, 8) == 3072
    # A tiny budget never drops below the floor.
    assert _sort_mem_per_thread_mb(100, 8) == 256
    # No budget -> None, so samtools keeps its own default rather than us guessing.
    assert _sort_mem_per_thread_mb(None, 8) is None


class _Recorder:
    """Captures every samtools argv and makes the pipeline 'succeed' by touching the output file."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(cmd)
        # `samtools view -o <out>` and `samtools faidx <local>` must leave their file behind.
        if "view" in cmd and "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"CRAM\0")
        if cmd[:2] == ["samtools", "faidx"]:
            Path(cmd[-1] + ".fai").write_text("")
        return subprocess.CompletedProcess(cmd, 0)

    def popen(self, cmd: list[str], **kwargs: object) -> _FakePopen:
        self.calls.append(cmd)
        return _FakePopen(cmd)


class _FakePopen:
    def __init__(self, cmd: list[str]) -> None:
        self.stdout = subprocess.DEVNULL
        self.returncode = 0

    def __enter__(self) -> _FakePopen:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _stub_samtools(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec.run)
    monkeypatch.setattr(subprocess, "Popen", rec.popen)
    return rec


def test_cram_passes_the_reference_and_never_embeds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _stub_samtools(monkeypatch)
    bam = tmp_path / "Aligned.out.bam"
    bam.write_bytes(b"BAM\0")
    fasta = tmp_path / "ref.fa"
    fasta.write_text(">chr\nACGT\n")
    (tmp_path / "ref.fa.fai").write_text("")  # index already beside the fasta

    out = tmp_path / "S1" / "S1.cram"
    bam_to_cram(bam, fasta, out, threads=8, sort_mem_mb=32768)

    flat = " ".join(" ".join(c) for c in rec.calls)
    # CRAM against the reference, reference NOT embedded.
    assert "-C -T" in flat
    assert str(fasta) in flat
    assert "embed_ref" not in flat
    # Multi-threaded sort with a real per-thread budget, not a single-thread default.
    sort = next(c for c in rec.calls if c[:2] == ["samtools", "sort"])
    assert "-@" in sort and sort[sort.index("-@") + 1] == "8"
    assert "-m" in sort
    # The CRAM is indexed.
    assert any(c[:3] == ["samtools", "index", "-@"] for c in rec.calls)


def test_a_missing_bam_refuses_before_touching_samtools(tmp_path: Path) -> None:
    with pytest.raises(CramError, match="missing"):
        bam_to_cram(tmp_path / "nope.bam", tmp_path / "ref.fa", tmp_path / "o.cram")


def test_a_read_only_reference_store_gets_its_fai_written_somewhere_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No .fai beside the FASTA -> we mirror it into the output dir and index there, not in the store."""
    rec = _stub_samtools(monkeypatch)
    bam = tmp_path / "Aligned.out.bam"
    bam.write_bytes(b"BAM\0")
    fasta = tmp_path / "store" / "ref.fa"
    fasta.parent.mkdir()
    fasta.write_text(">chr\nACGT\n")  # deliberately no ref.fa.fai beside it

    out = tmp_path / "S1" / "S1.cram"
    bam_to_cram(bam, fasta, out, threads=2)

    faidx = next(c for c in rec.calls if c[:2] == ["samtools", "faidx"])
    indexed = Path(faidx[-1])
    assert indexed.parent == out.parent  # written into the writable run dir, not the store
    assert indexed.is_symlink()


# ================================================================================================
# fragments — the map/chromap deliverable
# ================================================================================================
#
# Tests for `workflows.fragments` — the `map/chromap` deliverable contract.
#
# The QC path is pure Python over the fragments text, so it runs anywhere; the finalize path shells to
# htslib (`bgzip`/`tabix`) and is skipped when those are absent, exactly as the STAR integration tests
# skip without STAR.


_HTSLIB = shutil.which("bgzip") is not None and shutil.which("tabix") is not None

# chrom  start  end  barcode  count -- three fragments across two cells, deliberately out of coordinate
# order so the finalize sort is exercised.
_RAW = "chr2\t50\t60\tCCC\t3\nchr1\t300\t400\tAAA\t1\nchr1\t100\t200\tAAA\t2\n"


def test_fragments_suffixes_are_the_three_deliverables_in_build_order() -> None:
    assert fragments_suffixes() == [
        ".fragments.tsv.gz",
        ".fragments.tsv.gz.tbi",
        ".fragments.qc.json.gz",
    ]


# -- QC (pure Python) -------------------------------------------------------


def _plain(path: Path) -> Path:
    raw = path / "fragments.raw.tsv"
    raw.write_text(_RAW)
    return raw


def _gzipped(path: Path) -> Path:
    gz = path / "fragments.tsv.gz"  # a .gz suffix is opened through gzip
    with gzip.open(gz, "wt") as fh:
        fh.write(_RAW)
    return gz


def _with_comments(path: Path) -> Path:
    raw = path / "fragments.raw.tsv"
    raw.write_text("# a header comment\n\n" + _RAW)  # a comment and a blank line, not fragments
    return raw


@pytest.mark.parametrize(
    "writer", [_plain, _gzipped, _with_comments], ids=["plain", "gzipped", "with-comments"]
)
def test_build_fragments_qc_counts_the_same_over_plain_gzipped_and_commented_input(
    tmp_path: Path, writer: Callable[[Path], Path]
) -> None:
    """The QC is a function of the FRAGMENTS, not the packaging: a `.gz` suffix is read through gzip,
    comment/blank lines are skipped, and all three yield the identical counts over the same `_RAW`."""
    qc = build_fragments_qc(writer(tmp_path), sample="s1", assembly="mm10")

    assert qc.sample == "s1"
    assert qc.assembly == "mm10"
    assert qc.n_fragments == 3
    assert qc.n_barcodes == 2  # AAA, CCC
    assert qc.total_reads == 6  # 3 + 1 + 2
    assert qc.max_fragments_per_barcode == 2  # AAA has two fragments
    assert qc.min_fragments_per_barcode == 1  # CCC has one


def test_build_fragments_qc_rejects_a_malformed_line(tmp_path: Path) -> None:
    raw = tmp_path / "fragments.raw.tsv"
    raw.write_text("chr1\t100\n")  # missing end/barcode
    with pytest.raises(FragmentsError, match="malformed fragments line"):
        build_fragments_qc(raw, sample="s1", assembly="mm10")


def test_write_fragments_qc_emits_a_gzipped_json(tmp_path: Path) -> None:
    raw = tmp_path / "fragments.raw.tsv"
    raw.write_text(_RAW)
    out = tmp_path / "s1.fragments.qc.json.gz"

    written = write_fragments_qc(raw, out, sample="s1", assembly="mm10")

    assert written == out
    with gzip.open(out, "rt") as fh:
        payload = json.load(fh)
    assert payload["sample"] == "s1"
    assert payload["n_fragments"] == 3
    assert payload["n_barcodes"] == 2


def test_write_fragments_raises_when_the_raw_output_is_missing(tmp_path: Path) -> None:
    with pytest.raises(FragmentsError, match="is missing"):
        write_fragments(tmp_path / "nope.tsv", tmp_path / "out.tsv.gz")


# -- finalize (requires htslib) ---------------------------------------------


@pytest.mark.external
@pytest.mark.skipif(not _HTSLIB, reason="bgzip/tabix (htslib) not on PATH")
def test_write_fragments_sorts_bgzips_and_tabix_indexes(tmp_path: Path) -> None:
    raw = tmp_path / "fragments.raw.tsv"
    raw.write_text(_RAW)
    out = tmp_path / "s1.fragments.tsv.gz"

    written = write_fragments(raw, out)

    assert written == out
    assert out.is_file()
    assert (tmp_path / "s1.fragments.tsv.gz.tbi").is_file()  # tabix index landed beside it
    # Coordinate-sorted: chr1:100 must precede chr1:300, and both precede chr2.
    with gzip.open(out, "rt") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln]
    starts = [(c.split("\t")[0], int(c.split("\t")[1])) for c in lines]
    assert starts == sorted(starts)


def test_workflow_modules_are_registered_and_present_on_disk() -> None:
    from types import SimpleNamespace

    from seqforge.workflows import MODULES, resolve_pipeline

    assert set(list_modules()) == {"map/starsolo", "map/star", "map/chromap"}
    valid_envs = set(get_args(RuntimeEnv))
    for name in list_modules():
        module = get_module(name)
        assert module.snakefile.is_file(), f"{name} snakefile missing"
        assert module.version == WORKFLOW_VERSION
        # Each module names the runtime env its aligner needs -- align-rna for the STAR modules,
        # align-dna for chromap. Assert it is a real `RuntimeEnv`, not a hardcoded `align-rna`: the
        # env is the module's own declaration (policy reads it off the pipeline), so a second aligner
        # in a different env is correct, not a test failure.
        assert module.env in valid_envs

    # The assay<->pipeline adapter (folded from test_resolve_pipeline_binds_a_chemistry_and_refuses_an
    # _unserved_modality): a chemistry binds to the module its backend selects, and a spec whose
    # modality the target pipeline does not serve is a loud refusal at compose time, not a wrong
    # command line -- the same silent fall-through read_layout_kind/param_block were built to kill.
    rna = kb.load_spec("10x-3p-gex-v3")
    assert resolve_pipeline(rna) is MODULES[rna.require_backend().module]
    unserved = SimpleNamespace(
        require_backend=lambda: SimpleNamespace(module="map/starsolo"),
        identity=SimpleNamespace(modality="atac", id="fake-atac"),
    )
    with pytest.raises(KeyError, match="serves modalities"):
        # The stand-in spec IS the subject: a duck-typed object is what lets an unserved modality
        # reach `resolve_pipeline` at all, so the suppression stays and the argument stays invalid.
        resolve_pipeline(unserved)  # type: ignore[arg-type]


@pytest.mark.parametrize("module", list_modules())
def test_every_registered_module_wires_into_a_runnable_dag(
    module: str, tmp_path: Path, real_wiring_gate: None
) -> None:
    """The wiring gate, paid once per workflow module — the only place in the suite that pays it.

    The gate's claim varies with the ``.smk`` MODULE, not with the dataset, but ``run_wiring_gate``
    sat on ``compose``'s dataset-shaped interface, so ~41 tests each spawned ``snakemake -n -p`` to
    re-prove one of three facts and only ``map/starsolo`` was ever asserted on. This is the same
    claim on the interface that owns it: every registered module, exhaustively.

    The tech comes from the KB, not a hand-written list — a fourth module gets a case the moment
    a spec targets it, and a module no spec reaches fails loudly rather than going untested.

    It also owns "the gate leaves no zero-byte FASTQ behind", which used to be a second 1.5s spawn of
    its own on `map/starsolo`. The gate stands in zero-byte FASTQs; they were touched straight into
    the run directory (`pipeline_dir / row["path"]`) and never removed, which was invisible only
    because the gate never ran — `snakemake` was undeclared. The moment it ran, the run directory
    would hold zero-byte files named exactly like the FASTQs, STAR would read them, and the pipeline
    would emit an empty matrix and **exit 0**: silent, plausible, wrong, and introduced by the very
    commit that made the gate work. Asserted here it holds for all three modules, not for starsolo
    alone.
    """
    techs = sorted(
        t for t in kb.runnable_spec_ids() if kb.load_spec(t).require_backend().module == module
    )
    assert techs, f"{module} is registered but no spec reaches it"
    manifest, reg = _build(tmp_path, techs[0])
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    # PASS, not skip. This used to read `in {"pass", "skip"}` and so forbade only the one value that
    # could not occur: `snakemake` was in no dependency table, `have("snakemake")` was False, and the
    # gate returned "skip" every time. A skip is green, so the gate was decorative for the life of the
    # repo. If it ever goes missing again, that is a broken environment and this says so.
    assert result.gate["wiring"] == "pass"
    run_dir = (tmp_path / result.snakefile_path).parent
    strays = [p for p in run_dir.rglob("*") if p.suffix == ".gz" and p.stat().st_size == 0]
    assert not strays, f"the gate left zero-byte stand-ins in the run dir: {strays}"


def test_every_seqforge_verb_a_shipped_module_shells_out_to_exists() -> None:
    """A module's `shell:` naming a verb we renamed fails hours into a run, on a compute node.

    Derived from the live Typer app on one side and the module source on the other, so neither can be
    kept true by hand. This is `test_skill_documents_only_real_cli_verbs` pointed at the other place
    that hardcodes our own CLI — and the shipped modules are the more expensive place to be wrong.
    """
    import typer

    from seqforge.cli import app

    def paths(a: typer.Typer, prefix: tuple[str, ...] = ()) -> set[str]:
        out = {
            " ".join((*prefix, c.name or (c.callback.__name__ if c.callback else "")))
            for c in a.registered_commands
        }
        for g in a.registered_groups:
            assert g.typer_instance is not None and g.name is not None
            out |= paths(g.typer_instance, (*prefix, g.name))
        return out

    known = paths(app)
    for name in list_modules():
        # `shell:` blocks only. Scanning the whole file reads the rule's own docstring — which says
        # "a `shell:` calling a seqforge verb" — and reports `seqforge verb` as missing. Same lesson
        # `keys_read_by` learned: a scanner pointed at prose cries wolf, and then gets deleted.
        for block in re.findall(
            r"shell:\s*\n\s*r?\"\"\"(.*?)\"\"\"", get_module(name).snakefile.read_text(), re.DOTALL
        ):
            # `[a-z0-9-]`, not `[a-z-]`: the first verb this test ever met was `h5ad`, and a
            # name-shaped regex that stops at a digit matches `io h` and reports *that* as missing.
            for verb in re.findall(r"\bseqforge ((?:[a-z][a-z0-9-]* ){0,2}[a-z][a-z0-9-]*)", block):
                # longest match first: `io h5ad` is a command; `io` alone is only its group
                words = verb.split()
                assert any(" ".join(words[:n]) in known for n in range(len(words), 0, -1)), (
                    f"{name} shells out to `seqforge {verb}`, which is not a registered verb"
                )


#: A Snakemake rule definition. `rule x:` / `checkpoint x:` are the only ways to introduce rule
#: source, so this is the whole vocabulary the composer is forbidden from emitting.
_RULE_DEF = re.compile(r"^\s*(rule|checkpoint)\s+\w+\s*:", re.M)


def test_the_generated_wrapper_contains_no_rule_source() -> None:
    """Emit data, never code, at the ONE place seqforge writes Snakemake syntax at all.

    `compose` generates a Snakefile — the deliverable — and its own header says "rule source is never
    generated". Everything the pipeline actually executes must come from the hand-written `.smk`
    modules; the moment the composer emits a `rule`, that guarantee is gone and nobody finds out from a comment.

    Asserted against the template rather than a rendered instance because the template is the thing
    a future edit would change.
    """
    wrapper = core._WRAPPER
    assert not _RULE_DEF.search(wrapper), f"the composer emits rule source:\n{wrapper}"
    assert "configfile:" in wrapper  # it parameterises by data...
    assert "module " in wrapper and "use rule * from" in wrapper  # ...and composes by reference
    # ...and it must reach the module's rules as DEFAULT targets (folded from
    # test_the_wrapper_makes_the_modules_rules_reachable_as_default_targets): an `include:`d rule is
    # not a default target, so `configfile:` + `include:` parses clean, lists every rule, and plans
    # ZERO jobs -- "Nothing to be done", exit 0. `use rule * from m as *` re-declares them so bare
    # `snakemake` reaches them.
    assert "include:" not in wrapper, (
        "an `include:`d rule is not a default target -- the wrapper would plan zero jobs and exit 0"
    )


def test_the_rule_source_check_can_actually_catch_generated_rules() -> None:
    """Prove the guard fires, and that it does not cry wolf on the words it must tolerate."""
    assert _RULE_DEF.search("rule starsolo_count:\n    shell: 'STAR'\n")
    assert _RULE_DEF.search("checkpoint split:\n")
    assert _RULE_DEF.search('include: "x.smk"\nrule all:\n    input: []\n')
    # ...but the prose and directives a legitimate wrapper contains are not rule source
    assert not _RULE_DEF.search("# includes the module whose rules we never generate\n")
    assert not _RULE_DEF.search('include: "map/starsolo.smk"\nconfigfile: "config.yaml"\n')
    assert not _RULE_DEF.search('config["ruleset"] = "x"\n')


@pytest.mark.parametrize("module_name", list_modules())
def test_shipped_modules_are_hand_written_not_generated(module_name: str) -> None:
    """The other half of emit-data-never-code: the rules that DO exist are checked-in source, not build artifacts.

    A module whose rules were generated would defeat the wrapper check by moving the generation one
    step earlier, so the modules must be real files under version control, carrying the header that
    says what they are.
    """
    module = get_module(module_name)
    snakefile = module.snakefile
    assert snakefile.is_file(), f"{module_name}: {snakefile} is not on disk"
    text = snakefile.read_text()
    assert _RULE_DEF.search(text), f"{module_name} defines no rules — is it really a module?"
    assert "HAND-WRITTEN" in text and "NEVER machine-generated" in text

    # `required_config` is COMPUTED from the module source, so neither direction can drift (folded
    # from test_required_config_is_exactly_what_the_module_reads). It once under-declared the four
    # soloCB/UMI keys `starsolo.smk` dereferences and over-declared `primary_feature`/`env` that no
    # rule reads. This identity reads as tautological but is not: it pins that `required_config` never
    # goes back to a hand-typed literal; test_the_required_config_scanner_can_catch_an_undeclared_key
    # is what proves the derivation itself is not vacuous.
    assert set(module.required_config) == set(keys_read_by(snakefile))
    assert module.required_config == tuple(sorted(module.required_config))


@pytest.mark.xdist_group("src-trees")
def test_the_config_block_is_read_off_the_module_not_matched_on_its_name(
    src_trees: SrcTrees,
) -> None:
    """The last `== "map/starsolo"` in the tree, and the last silent "everything else is bulk".

    `param_block_key` was `"solo" if spec.backend.module == "map/starsolo" else "bulk"`, which is the
    same bug `read_layout_kind` was created to kill one function earlier: a third module gets its
    params written into a `bulk:` block it never reads, and the params gate agrees with the composer
    because the gate calls this same function. Two things wrong identically look, from inside a test,
    exactly like two things right.

    Now it is read off what the module source actually dereferences.
    """
    import ast

    from seqforge.workflows import MODULES, list_modules

    assert MODULES["map/starsolo"].param_block == "solo"
    assert MODULES["map/star"].param_block == "bulk"

    # A COMPARISON against a module name, not a mention of one: the docstrings deliberately keep the
    # old line so the bug it names stays findable. Grepping the text would forbid the record of the
    # fix along with the fix, which is how a guard teaches people to delete their own history.
    names = set(list_modules())
    offenders: list[str] = []
    for py, tree in src_trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and any(
                isinstance(c, ast.Constant) and c.value in names for c in node.comparators
            ):
                offenders.append(f"{py.relative_to(_src_root())}:{node.lineno}")
    assert not offenders, (
        "a workflow module is being dispatched on by NAME. Every module that is not the one named "
        "silently takes the other branch, and nothing goes red — that is how `_read_files_in` "
        "emitted mate1/mate2 for a barcoded chemistry. Declare the fact on the module:\n"
        + "\n".join(offenders)
    )


def tmp_snakefile() -> Path:
    """A module that reads neither aligner-param block. Written to a real path: `required_config`
    scans the source, which is the whole point of it being derived."""
    import tempfile

    p = Path(tempfile.mkdtemp()) / "ghost.smk"
    p.write_text('rule x:\n    output: "y"\n    shell: "echo {config[outdir]}"\n')
    return p


def test_a_module_whose_config_contract_is_unreadable_refuses() -> None:
    """Guessing which block a module reads is how the wrong params reach an aligner."""
    from seqforge.workflows import WorkflowModule

    ghost = WorkflowModule(
        name="map/ghost",
        version="0.0.0",
        env="align-rna",
        snakefile=tmp_snakefile(),
        read_layout_kind="paired",
    )
    with pytest.raises(ValueError, match="exactly one of solo/bulk"):
        _ = ghost.param_block


def test_the_parse_namespace_is_per_pipeline_not_one_global_set() -> None:
    """Each pipeline owns the parse keys its backends may declare — the single source of truth.

    The KB DSL validator and the composer's params gate both consult `parse_keys_for(module)`, so a
    second aligner declares its own knobs without widening STARsolo's. A global set forced every
    pipeline to share one namespace, which is exactly what makes "instruction contradicts the bytes"
    inexpressible only by accident.
    """
    from seqforge.workflows import parse_keys_for

    # `MODULES["map/starsolo"].parse_keys == parse_keys_for("map/starsolo")` was dropped here: it is
    # tautological, since `parse_keys_for` IS `get_module(module).parse_keys`. The behavioural claims
    # below stay.
    assert "soloType" in parse_keys_for("map/starsolo")
    assert len(parse_keys_for("map/starsolo")) == 11
    # A bulk pipeline declares no parse params — empty, not degenerate (no barcode/UMI/whitelist).
    assert parse_keys_for("map/star") == frozenset()
    with pytest.raises(KeyError, match="unknown workflow module"):
        parse_keys_for("map/nonesuch")


def test_the_aligner_name_is_derived_from_the_module_id_not_a_mirror() -> None:
    """`aligner` is read off the module id, so it can never drift from a hand-kept `_ALIGNER_FOR_MODULE`.

    Every entry of the deleted dict equalled this rsplit, so the dict was a mirror of the ids that
    could only ever disagree with them.
    """
    from seqforge.workflows import MODULES

    assert MODULES["map/starsolo"].aligner == "starsolo"
    assert MODULES["map/star"].aligner == "star"
