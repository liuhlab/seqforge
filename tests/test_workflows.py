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
from itertools import pairwise
from pathlib import Path
from typing import get_args

import anndata as ad
import pysam
import pytest
from scipy.sparse import csr_matrix

from conftest import SrcTrees, _build, _processing, _rule_blocks, _src_root
from seqforge import kb
from seqforge.compose import compose, core
from seqforge.models.dataset import ReadDef, ReadElement, ReadLayout
from seqforge.models.processing import RuntimeEnv, SoloFeature
from seqforge.workflows import WORKFLOW_VERSION, get_module, keys_read_by, list_modules
from seqforge.workflows.cram import CramError, bam_to_cram
from seqforge.workflows.fragments import (
    QC_SUFFIX,
    FragmentsError,
    build_fragments_qc,
    fragments_suffixes,
    write_fragments,
    write_fragments_qc,
)
from seqforge.workflows.fragments import metrics as fragments_metrics
from seqforge.workflows.fragments import read_metrics as read_fragments_metrics
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
from seqforge.workflows.memory import STARSOLO_RETRIES, bam_sort_ram, escalated_mem_mb
from seqforge.workflows.metrics import (
    MAX_KNEE_POINTS,
    SEVERITY_PHRASE,
    Decision,
    DecisionRef,
    Finding,
    Metric,
    SampleStats,
    Severity,
    fmt_int,
    fraction,
    gather_alerts,
    grade,
    knee_points,
)
from seqforge.workflows.qc import (
    HEALTHY_GENOME_MAPPING,
    INTRONIC_ONLY_READ_SHARE,
    NEAR_ZERO_VALID_BARCODES,
    POOR_GENE_ASSIGNMENT,
    QcError,
    build_qc_bundle,
    chemistry_rule,
    gene_model_rule,
    read_star_log,
    solo_features_rule,
    write_qc_bundle,
)
from seqforge.workflows.qc import metrics as starsolo_metrics
from seqforge.workflows.qc import read_metrics as read_starsolo_metrics
from seqforge.workflows.stats import (
    MODULES_WITHOUT_CROSS_CHECKS,
    MODULES_WITHOUT_STATS,
    modules_with_cross_checks,
    modules_with_stats,
    read_pipeline_stats,
)
from seqforge.workflows.umite.extract import (
    UmiExtractError,
    extract_umis,
    find_tag,
    geometry_for_read,
    tagged_read_geometry,
)

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
    entries: dict[str, dict[tuple[int, int], int]] | None = None,
) -> None:
    """One ``Solo.out/<feature>/raw/``. ``base`` makes each feature's counts distinguishable.

    ``entries`` overrides the counts per matrix FILE — ``{"spliced.mtx": {(gene, cell): n}}`` — which
    is what lets one feature, or one of Velocyto's three matrices, carry a barcode the others do not.
    Without it every matrix gets the same two ``base``-derived counts, so every barcode is nonzero
    everywhere and the all-zero trim has nothing to remove.
    """
    raw = solo / feature / "raw"
    raw.mkdir(parents=True)
    raw.joinpath("features.tsv").write_text(
        "".join(f"{g}\t{g}-name\tGene Expression\n" for g in genes)
    )
    raw.joinpath("barcodes.tsv").write_text("".join(f"{b}\n" for b in barcodes))
    default = {(1, 1): base + 1, (2, 2): base + 2}
    for name in SOLO_FEATURE_OUTPUT[feature].matrices:
        _mtx(raw / name, (entries or {}).get(name, default), len(genes), len(barcodes))


def _solo_out(
    tmp_path: Path,
    features: list[SoloFeature],
    *,
    genes: list[str] = GENES,
    barcodes: list[str] = BARCODES,
) -> Path:
    """Every feature over the same axes and the same counts — a `Solo.out` with nothing to trim.

    The axes are named rather than forwarded as `**kwargs`: `_feature_dir` now also takes per-matrix
    `entries`, which no whole-tree caller wants (the point of overriding entries is to make ONE
    feature differ from the others), and a `**kwargs` wide enough to carry it would let that through.
    """
    solo = tmp_path / "Solo.out"
    for i, feature in enumerate(features):
        _feature_dir(solo, feature, genes=genes, barcodes=barcodes, base=i * 10)
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
    """The log/table set feeds qc_bundle; the BAM is its own constant (solo_to_cram consumes it).

    The name is STAR's, and it follows from `--outSAMtype BAM SortedByCoordinate` — which the module
    must pass, because STAR refuses to put the `CB`/`UB` barcode tags in anything but the sorted BAM.
    Get this literal wrong and `starsolo_count` declares an output STAR never writes: the rule fails
    after the whole alignment has been paid for.
    """
    assert set(STAR_LOG_FILES) == {"Log.final.out", "Log.out", "Log.progress.out", "SJ.out.tab"}
    assert STAR_BAM == "Aligned.sortedByCoord.out.bam"
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


#: STARsolo's `raw/barcodes.tsv` is the entire whitelist, so most of it is empty: 6,794,880 rows on
#: 10x v3, of which 845,694 carried a count. Four rows here stand in for that shape -- `AAAA` counts
#: in both features, `GGGG` counts in `GeneFull` ONLY, and `CCCC`/`TTTT` are the silent 87.6%.
_WHITELIST = ["AAAA", "CCCC", "GGGG", "TTTT"]


def _mostly_empty_solo(tmp_path: Path) -> Path:
    """A `Solo.out` whose whitelist is four barcodes and whose counts reach only two of them."""
    solo = tmp_path / "Solo.out"
    _feature_dir(solo, "Gene", barcodes=_WHITELIST, base=0, entries={"matrix.mtx": {(1, 1): 5}})
    _feature_dir(
        solo,
        "GeneFull",
        barcodes=_WHITELIST,
        base=0,
        entries={"matrix.mtx": {(1, 1): 7, (2, 3): 9}},
    )
    return solo


def test_the_mask_is_the_union_across_features_not_the_primary_alone(tmp_path: Path) -> None:
    """The trap this trim is subtle enough to be worth a test for.

    `X` is `Gene` — exonic reads — while `GeneFull` counts introns too, so a barcode can be zero in
    the primary matrix and carry real counts in a layer. On the sample this was measured against,
    22,319 barcodes were exactly that, and trimming on `X.sum() > 0` deletes every one of them: cells
    that opened fine, plotted fine, and were simply not there. The mask has to be the UNION over the
    primary and every layer, which is what `GGGG` (counts in `GeneFull` only) pins down here.
    """
    write_h5ad(_mostly_empty_solo(tmp_path), ["Gene", "GeneFull"], "Gene", tmp_path / "s1")
    adata = ad.read_h5ad(tmp_path / "s1.h5ad")

    assert list(adata.obs_names) == ["AAAA", "GGGG"]
    assert _counts(adata)[1].nnz == 0, "GGGG is empty in the primary and must survive anyway"
    # entry (2, 3) = gene 2, cell 3 in STAR's file -> obs 1 (GGGG, once CCCC is gone), var 1
    assert _counts(adata, "GeneFull")[1, 1] == 9


def test_a_barcode_empty_in_every_feature_is_dropped_and_the_survivors_are_untouched(
    tmp_path: Path,
) -> None:
    """Lossless by construction: what goes is exactly what carried no information.

    A barcode with no count in any feature says nothing about the experiment, so dropping it changes
    no analysis — and the counts that stay must be bit-identical, not merely present, because a trim
    that quietly renumbered or reordered anything would be far worse than the 85% it saves. `var` is
    untouched: this is a cut along one axis.
    """
    write_h5ad(_mostly_empty_solo(tmp_path), ["Gene", "GeneFull"], "Gene", tmp_path / "s1")
    adata = ad.read_h5ad(tmp_path / "s1.h5ad")

    assert adata.shape == (2, len(GENES))  # CCCC and TTTT are gone; all three genes stay
    assert list(adata.var_names) == GENES
    assert _counts(adata)[0, 0] == 5
    assert _counts(adata, "GeneFull")[0, 0] == 7
    assert _counts(adata).nnz == 1 and _counts(adata, "GeneFull").nnz == 2  # nothing else appeared
    # Provenance: once `raw/` is deleted (it is a `temp()` output) this is all that says the obs axis
    # was cut down from a whitelist, and by how much.
    assert adata.uns["n_barcodes_whitelist"] == 4
    assert adata.uns["n_barcodes_retained"] == 2


def test_the_velocyto_mask_is_the_union_of_spliced_unspliced_and_ambiguous(tmp_path: Path) -> None:
    """Same union, three layers — and `X` duplicating `spliced` is what makes it easy to get wrong.

    A barcode with only unspliced counts is a real cell in the middle of transcription, and it is
    zero in `X`. Each of the three matrices here reaches a different barcode, so any mask narrower
    than their union loses one of them.
    """
    solo = tmp_path / "Solo.out"
    _feature_dir(
        solo,
        "Velocyto",
        barcodes=_WHITELIST,
        base=0,
        entries={
            "spliced.mtx": {(1, 1): 3},
            "unspliced.mtx": {(1, 2): 4},
            "ambiguous.mtx": {(1, 3): 5},
        },
    )
    write_h5ad(solo, ["Velocyto"], "Velocyto", tmp_path / "s1")
    velo = ad.read_h5ad(tmp_path / "s1.velocyto.h5ad")

    assert list(velo.obs_names) == ["AAAA", "CCCC", "GGGG"]  # only TTTT is empty in all three
    assert _counts(velo, "unspliced")[1, 0] == 4
    assert _counts(velo, "ambiguous")[2, 0] == 5
    assert _counts(velo)[1].nnz == 0, "CCCC is empty in spliced (= X) and must survive anyway"
    assert velo.uns["n_barcodes_whitelist"] == 4
    assert velo.uns["n_barcodes_retained"] == 3


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
# BAM -> CRAM finalize. The gates: the reference is passed (never embedded), NOTHING sorts (STAR
# already did, and the `samtools sort` that used to stand here is what leaked undeclared temp files
# into the pipeline dir), only primary alignments reach the encoder, the read names are rewritten,
# and every stage of the pipe has its exit status checked.
#
# samtools is stubbed so the test needs no binary and no genome: what is asserted is the *argv* we hand
# it, which is where the correctness lives (a stray ``embed_ref``, a missing ``-T``, a resurrected
# ``sort``, a single thread).


class _FakePipe:
    """Stands in for a `Popen.stdout`. It exists to have a `close()`: the real pipeline hands each
    read end to the next stage and then drops the parent's copy, which an `int` fd cannot model."""

    def close(self) -> None:
        return None


class _Recorder:
    """Captures every samtools/awk argv and makes the pipeline 'succeed' by touching the output file.

    `fails` names one argv token; whichever stage carries it exits 1 instead of 0. One token is
    enough to address any stage of the pipe — `-F` is the primary filter, `awk` the read-name
    rewrite, `-C` the encoder — so the three failure cases need no three near-identical fakes.
    """

    def __init__(self, fails: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fails = fails

    def _returncode(self, cmd: list[str]) -> int:
        return 1 if self.fails is not None and self.fails in cmd else 0

    def _side_effects(self, cmd: list[str], code: int) -> None:
        """What a successful stage leaves on disk, so the next step finds what it expects."""
        # `samtools view -o <out>` and `samtools faidx <local>` must leave their file behind.
        if "view" in cmd and "-o" in cmd and not code:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"CRAM\0")
        if cmd[:2] == ["samtools", "faidx"]:
            Path(cmd[-1] + ".fai").write_text("")

    def run(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(cmd)
        code = self._returncode(cmd)
        self._side_effects(cmd, code)
        return subprocess.CompletedProcess(cmd, code)

    def popen(self, cmd: list[str], **kwargs: object) -> _FakePopen:
        self.calls.append(cmd)
        code = self._returncode(cmd)
        # Every stage of the pipe is a `Popen` now, the encoder included — it is the one that writes
        # the CRAM, and the parent has to close its copy of awk's read end *while the encoder runs*,
        # which a blocking `subprocess.run` gives it no chance to do.
        self._side_effects(cmd, code)
        return _FakePopen(code)


class _FakePopen:
    def __init__(self, returncode: int = 0) -> None:
        self.stdout = _FakePipe()
        self.returncode = returncode

    def __enter__(self) -> _FakePopen:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _stub_samtools(monkeypatch: pytest.MonkeyPatch, fails: str | None = None) -> _Recorder:
    rec = _Recorder(fails)
    monkeypatch.setattr(subprocess, "run", rec.run)
    monkeypatch.setattr(subprocess, "Popen", rec.popen)
    return rec


def test_cram_passes_the_reference_and_never_embeds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole argv, in one place: filter, rename, encode — and no sort anywhere in it.

    The absent `samtools sort` is the load-bearing assertion. STAR now writes the BAM already
    coordinate-sorted, so re-sorting it here would be a wasted pass over every alignment AND would
    bring back the `-T`-less spill files that leaked 41.4 GiB into five pipeline dirs. The leak is
    meant to be impossible now, not merely configured away, so the stage may not come back.
    """
    rec = _stub_samtools(monkeypatch)
    bam = tmp_path / STAR_BAM
    bam.write_bytes(b"BAM\0")
    fasta = tmp_path / "ref.fa"
    fasta.write_text(">chr\nACGT\n")
    (tmp_path / "ref.fa.fai").write_text("")  # index already beside the fasta

    out = tmp_path / "S1" / "S1.cram"
    bam_to_cram(bam, fasta, out, threads=8)

    flat = " ".join(" ".join(c) for c in rec.calls)
    # CRAM against the reference, reference NOT embedded.
    assert "-C -T" in flat
    assert str(fasta) in flat
    assert "embed_ref" not in flat
    # STAR sorted it; nothing here re-sorts, and no `-T`-less spill file can be left behind.
    assert not any(c[:2] == ["samtools", "sort"] for c in rec.calls)
    # Primary alignments only, header kept (the encoder needs the @SQ lines), multi-threaded.
    primary = next(c for c in rec.calls if "-F" in c)
    assert primary[:2] == ["samtools", "view"] and "-h" in primary
    assert primary[primary.index("-F") + 1] == "0x100"
    assert primary[primary.index("--threads") + 1] == "8"
    # The read names are rewritten in the stream: `awk`, tab-delimited in AND out, headers passed
    # through untouched, and the new QNAME is a counter.
    rename = next(c for c in rec.calls if c[0] == "awk")
    assert r'FS=OFS="\t"' in rename[1]
    assert "/^@/" in rename[1] and '$1="r"' in rename[1]
    # The CRAM is indexed.
    assert any(c[:3] == ["samtools", "index", "-@"] for c in rec.calls)


@pytest.mark.parametrize(
    ("fails", "named"),
    [("-F", "primary alignments"), ("awk", "read-name rewrite"), ("-C", "CRAM encode")],
    ids=["primary-filter", "read-name-rewrite", "cram-encoder"],
)
def test_a_failure_in_any_stage_of_the_pipe_is_a_cram_error_that_names_it(
    fails: str, named: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pipe reports the exit status of its LAST stage, so two of these three would pass silently.

    `samtools view -C` happily encodes a truncated stream and exits 0, which is how a filter or a
    rewrite that died mid-file becomes a CRAM missing most of its reads — the silent-plausible-wrong
    class again, and expensive here because the BAM it came from is a `temp()` output that snakemake
    deletes the moment this rule succeeds. So each stage is waited on and named.
    """
    _stub_samtools(monkeypatch, fails=fails)
    bam = tmp_path / STAR_BAM
    bam.write_bytes(b"BAM\0")
    fasta = tmp_path / "ref.fa"
    fasta.write_text(">chr\nACGT\n")
    (tmp_path / "ref.fa.fai").write_text("")

    out = tmp_path / "S1" / "S1.cram"
    with pytest.raises(CramError, match=named):
        bam_to_cram(bam, fasta, out, threads=2)


def test_a_missing_bam_refuses_before_touching_samtools(tmp_path: Path) -> None:
    with pytest.raises(CramError, match="missing"):
        bam_to_cram(tmp_path / "nope.bam", tmp_path / "ref.fa", tmp_path / "o.cram")


def test_a_read_only_reference_store_gets_its_fai_written_somewhere_writable_and_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No .fai beside the FASTA -> mirror and index in scratch that is gone when the call returns.

    Two claims, and the second is the one that moved. It is not enough that the index avoids the
    read-only store; it must also avoid the rule's OUTPUT directory, where it was an undeclared file
    Snakemake could never clean — the same shape as the `samtools sort` spill this release deleted,
    only smaller. A per-call temp dir makes "the rule writes nothing it did not declare" true rather
    than argued, so the last assertion is that the output dir afterwards holds nothing but the CRAM.
    (`samtools index` is stubbed, so no `.crai` appears here; on a real run it is the rule's other
    declared output.)
    """
    rec = _stub_samtools(monkeypatch)
    bam = tmp_path / STAR_BAM
    bam.write_bytes(b"BAM\0")
    fasta = tmp_path / "store" / "ref.fa"
    fasta.parent.mkdir()
    fasta.write_text(">chr\nACGT\n")  # deliberately no ref.fa.fai beside it

    out = tmp_path / "S1" / "S1.cram"
    bam_to_cram(bam, fasta, out, threads=2)

    faidx = next(c for c in rec.calls if c[:2] == ["samtools", "faidx"])
    indexed = Path(faidx[-1])
    assert indexed.parent != fasta.parent  # never the store, which is frequently read-only
    assert indexed.parent != out.parent  # and never the output dir, where it would be undeclared
    assert not indexed.parent.exists()  # the scratch went with the call that made it
    assert {p.name for p in out.parent.iterdir()} == {out.name}


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
    # The one literal in this file the rule and the pipeline-stats registry both resolve to. Pinned
    # here so the constant has a spelling somewhere, and imported everywhere else so a rename shows
    # up as this test failing rather than as a report that quietly finds no artifact.
    assert fragments_suffixes()[-1] == QC_SUFFIX


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
        read_layout_kind="mates",
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
    # 12 since `soloCBmatchWLtype` moved out of starsolo.smk's `soloType` branch and into the KB.
    assert len(parse_keys_for("map/starsolo")) == 12
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


# ================================================================================================
# memory — what `starsolo_count` asks the scheduler for, and how much of it STAR may sort in
# ================================================================================================
#
# Two numbers that only mean anything together (`workflows/memory.py`), and one rule that has to keep
# them together across a retry. The arithmetic is unit-tested here because it finally can be: it used
# to be a constant and a closure INSIDE `starsolo.smk`, and a Snakefile is not importable, so the
# expression deciding whether a two-billion-read sample lived or died was only ever exercised by
# running STAR against a two-billion-read sample.
#
# What is NOT covered, anywhere, is that the escalation happens on a real retry: a dry run renders
# attempt 1 and only attempt 1, and this suite owns no scheduler and no sample large enough to fail
# one. So the split below is deliberate — the arithmetic as functions, and the WIRING as source shape.


#: The `config["mem_mb"]` the composer emits for a default recipe: ``ResourceHints.mem_gb`` (48) x
#: 1024. Written as the product so the connection to the recipe field stays readable, and used as the
#: base here rather than a round toy number because this arithmetic is scale-sensitive — a floor and a
#: cap-on-the-floor both live inside it, and neither is exercised the way a real job exercises it by a
#: base of 100.
_DEFAULT_MEM_MB = 48 * 1024


def test_the_sort_budget_follows_the_escalated_memory_request() -> None:
    """The composed value `bam_sort_ram(escalated_mem_mb(base, attempt))` — the pair, not each half.

    This is the arithmetic behind the defect #205 removed, and the defect is a *product* of the two
    functions rather than a fault in either: a rule that escalates its memory request while handing
    STAR a cap computed from the static `config["mem_mb"]` looks like it retried, spends the queue
    time of a retry, and then refuses in exactly the place attempt 1 refused, because the sort was
    never allowed to grow with the job around it. So what is asserted is the composition, evaluated
    across the attempts the rule can actually reach, and it must be STRICTLY increasing — a claim
    neither function makes on its own and neither can be inspected for.

    **Attempt 1 returning the request unchanged is an acceptance criterion, not an implementation
    detail.** Nearly every sample in a ~10^4-dataset corpus fits in the default request; making all of
    them more expensive to schedule in order to rescue the handful that do not is the outcome #205
    rejected, and `escalated_mem_mb(m, 1) == m` is the whole of what "a first attempt sized as today
    still succeeds on a normal sample" means in code.

    **The numbers are absolute because a test comparing these outputs only to each other would not
    catch the unit bug.** Monotonicity survives deleting the `* 1024 * 1024`; so does "attempt 2 is
    twice attempt 1"; so does any relation between two returns of the same function. STAR takes
    **bytes** and `mem_mb` is MiB, so a cap handed over in MiB is ~10^6 times too small, and STAR
    then FATALs on *every* sample instead of on a large one — the same flag, a different bug, and a
    green suite. Only an absolute value crosses that boundary, so absolute values are what is written.

    The retry count is pinned rather than parameterised away, and that is the point of importing it:
    `STARSOLO_RETRIES` and the linear multiplier are ONE fact (`workflows/memory.py` says so), so the
    worst case anybody reasoning about the queue actually needs is their product. Raising the count
    without restating what the last attempt is now given should go red here.
    """
    # Attempt 1 is today's request, byte for byte, at the shipped default and at any other size.
    assert escalated_mem_mb(_DEFAULT_MEM_MB, 1) == _DEFAULT_MEM_MB
    assert escalated_mem_mb(4096, 1) == 4096

    # snakemake's `attempt` is 1-based, so N retries means N+1 attempts.
    budgets = [
        bam_sort_ram(escalated_mem_mb(_DEFAULT_MEM_MB, attempt))
        for attempt in range(1, STARSOLO_RETRIES + 2)
    ]
    assert all(later > earlier for earlier, later in pairwise(budgets)), (
        f"the sort cap does not rise with the attempt, so a retry buys scheduler memory STAR is "
        f"still forbidden to sort in: {budgets}"
    )

    # Two retries, so three attempts, and 3/4 of 48 / 96 / 144 GiB IN BYTES. Read as GiB: 36, 72,
    # 108. Had the MiB->byte conversion been dropped, the first of these would read 36864.
    assert STARSOLO_RETRIES == 2, "the shipped retry count moved; restate the last attempt's budget"
    assert budgets == [38_654_705_664, 77_309_411_328, 115_964_116_992]

    # A job whose whole request is under the 1024 MiB floor gets the WHOLE REQUEST, not the floor.
    # The floor may not exceed the budget itself: authorising STAR to sort in more memory than the
    # job was granted trades STAR's legible refusal ("this is how many bytes I needed") for the
    # scheduler's OOM kill, which is the one failure mode #205 exists to remove.
    assert bam_sort_ram(512) == 536_870_912  # 512 MiB, the whole request, not 1024
    # ...and just above the floor the floor still binds: 3/4 of 1200 MiB is 900, under it.
    assert bam_sort_ram(1200) == 1_073_741_824  # 1024 MiB, the floor


def test_the_star_rule_escalates_its_memory_on_retry() -> None:
    """The WIRING, read off the shipped `.smk`: a `retries:`, and TWO numbers that follow `attempt`.

    What this reads is that the rule is *shaped* so the escalation can happen: a `retries:` directive,
    a `mem_mb` that is a function of `attempt` rather than a constant the retry re-submits unchanged,
    and a sort cap that is a second function of `attempt` — declared as a **resource**, because that
    is the only construct snakemake re-expands per attempt. `test_a_snakemake_retry_re_expands_a
    _resource_and_never_a_param` is the behavioural half; this is the half that names the rule.

    **The cap is the load-bearing assertion, and it is invisible to every other gate.** Before #205 it
    read `config["mem_mb"]` and was a parse-time constant. Nothing else in the suite can tell the two
    apart: the params gate only ever inspects the emitted config; `keys_read_by` is content either
    way, because the rule legitimately subscripts `config["mem_mb"]` on the line above; and the
    compose dry run cannot help, because on attempt 1 the escalated value and the config value are
    EQUAL — the argv is byte-identical under the fix and under the bug alike.

    **`params:` is rejected here, and that is a real bug this test caught rather than a stylistic
    preference.** The first implementation of #205 was `sort_ram=lambda wildcards, resources:
    bam_sort_ram(resources.mem_mb)`, which reads correctly, plans correctly, passes a dry run, and is
    wrong: snakemake memoizes `Job._params`, and `Job.attempt`'s setter clears `_resources` and not
    `_params`. So the cap was expanded once, on attempt 1, and every retry reused it — a job that
    asks for 3x and then refuses to sort in more than 1x's worth. A cap pinned that way is worse than
    no retry at all: it spends a second multi-hour queue slot to fail for the reason attempt 1 had
    already recorded.

    `retries:` naming the imported constant rather than a literal `2` is asserted for the reason
    `workflows/memory.py` gives for the constant existing: the retry count and the linear multiplier
    are one fact, and split across two files the count gets raised by someone who never reads the
    multiplier.
    """
    body = _rule_blocks(get_module("map/starsolo").snakefile)["starsolo_count"]

    retries = re.search(r"^\s+retries:\s*(\S+)\s*$", body, re.M)
    assert retries, (
        "`starsolo_count` declares no `retries:`, so a killed job is never re-run at all"
    )
    assert retries.group(1) == "STARSOLO_RETRIES", (
        "the retry count is a literal in the Snakefile, so it can now disagree with the escalation "
        "rule it is half of; declare it as `workflows/memory.STARSOLO_RETRIES`"
    )
    assert STARSOLO_RETRIES >= 1, "a retry count of 0 makes the escalation unreachable"

    request = re.search(r"^\s+mem_mb=(.*)$", body, re.M)
    assert request, "`starsolo_count` requests no `mem_mb`, so the scheduler gates nothing"
    assert request.group(1).startswith("lambda") and "attempt" in request.group(1), (
        f"`mem_mb` is not a function of `attempt`, so every retry re-submits the request that was "
        f"already killed: {request.group(1)}"
    )

    # The cap STAR is handed, and the `resources:` block it must be declared in. Both halves matter:
    # a `params:` entry of the identical text would satisfy the `attempt` check and still freeze.
    cap = re.search(r"^\s+bam_sort_ram_bytes=(.*?)^\s{4}\w+:", body, re.M | re.S)
    assert cap, "`starsolo_count` computes no sort budget; STAR's default 0 reuses the genome's"
    assert "attempt" in cap.group(1) and "config[" in cap.group(1), (
        f"the sort cap is not a function of `attempt` over the config's base request: {cap.group(1)}"
    )
    directives = re.findall(r"^\s{4}(\w+):", body, re.M)
    assert directives.index("resources") < directives.index("params"), (
        "the directive order moved; the assertion below reads the block between `resources:` and "
        "`params:` and needs rewriting"
    )
    resources_block = body.split("\n    resources:")[1].split("\n    params:")[0]
    assert "bam_sort_ram_bytes=" in resources_block, (
        "the sort cap is not a `resources:` entry. Snakemake memoizes `Job._params` and clears only "
        "`_resources` when `attempt` advances, so a `params:` callable is expanded on attempt 1 and "
        "reused verbatim by every retry — the request escalates and the cap does not"
    )
    # ...and the resource that follows the attempt is the one STAR is actually handed. A cap computed
    # correctly and never passed would satisfy every assertion above.
    assert "--limitBAMsortRAM {resources.bam_sort_ram_bytes}" in body


#: A three-attempt workflow in eleven lines: one rule that always fails, with `retries: 2`, declaring
#: the same escalation shape `starsolo_count` does — a `mem_mb` over `attempt`, a derived cap as a
#: RESOURCE and the identical arithmetic as a PARAM — and appending both to a trace on every attempt.
#: Synthetic on purpose: this is a test of snakemake's semantics, not of our module, so it must not
#: need a genome, an aligner, or a sample large enough to run out of memory.
_RETRY_PROBE = """
def cap(mem_mb):
    return mem_mb * 3 // 4

rule all:
    input: "out.txt"

rule x:
    output: "out.txt"
    retries: 2
    resources:
        mem_mb=lambda wildcards, attempt: 1000 * attempt,
        cap_resource=lambda wildcards, attempt: cap(1000 * attempt),
    params:
        cap_param=lambda wildcards, resources: cap(resources.mem_mb),
    shell:
        "echo {resources.mem_mb} {resources.cap_resource} {params.cap_param} >> trace.txt; false"
"""


@pytest.mark.external
@pytest.mark.skipif(shutil.which("snakemake") is None, reason="snakemake not on PATH")
def test_a_snakemake_retry_re_expands_a_resource_and_never_a_param(tmp_path: Path) -> None:
    """The behavioural half of the escalation, and the gate that caught #205's first implementation.

    The claim `starsolo_count` rests on is a claim about SNAKEMAKE, not about seqforge: that a value
    declared over `attempt` is recomputed on every retry. The first implementation assumed that held
    for `params:` as well as `resources:` — `sort_ram=lambda wildcards, resources:
    bam_sort_ram(resources.mem_mb)` — which reads correctly, plans correctly, passes `snakemake -n`,
    and silently freezes: `Job.attempt`'s setter clears `self._resources` and NOT `self._params`, and
    `reset_params_and_resources()` is one-shot behind a `_params_and_resources_resetted` flag. Every
    structural test in this file was green on that wiring, because the shape was right and only the
    semantics were wrong. Nothing short of a real retry distinguishes them.

    So this runs one, over a synthetic eleven-line workflow rather than our module: a rule that always
    fails, `retries: 2`, and the two constructs declared SIDE BY SIDE over identical arithmetic, so
    the trace is a controlled comparison rather than two runs to compare by hand. Three attempts, and
    the assertion is that the resource tracks the request while the param does not — a red test if
    snakemake ever memoized resources too (our escalation would silently stop), and equally a red test
    if it stopped memoizing params (the docstrings and the ADR would then be arguing for a workaround
    nobody needs any more). Both directions are worth knowing, which is why the param column is
    asserted rather than merely omitted.

    Deliberately not a test of `starsolo_count` itself: that rule needs a genome index, an aligner and
    a sample big enough to exhaust memory before it can be made to fail three times, none of which
    this suite owns (ADR-0002 — the ladder is a rule, and this is the cheapest thing that can go red).
    `test_the_star_rule_escalates_its_memory_on_retry` is what ties the proven construct to the rule.
    """
    (tmp_path / "Snakefile").write_text(_RETRY_PROBE)

    proc = subprocess.run(
        ["snakemake", "-d", str(tmp_path), "-s", str(tmp_path / "Snakefile"), "--cores", "1"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode != 0, "the probe rule must fail on every attempt, or nothing retried"

    trace = [ln.split() for ln in (tmp_path / "trace.txt").read_text().splitlines() if ln]
    assert len(trace) == 3, f"expected 3 attempts (retries: 2), got {len(trace)}: {trace}"

    requests = [int(mem) for mem, _, _ in trace]
    assert requests == [1000, 2000, 3000], (
        f"a `resources:` callable over `attempt` did not escalate, so `mem_mb` never grows and the "
        f"whole of #205 is inert: {requests}"
    )
    assert [int(res) for _, res, _ in trace] == [750, 1500, 2250], (
        "the cap declared as a RESOURCE did not track the escalated request — the construct "
        "`starsolo_count` relies on has changed behaviour, and the sort budget is now frozen"
    )
    assert [int(par) for _, _, par in trace] == [750, 750, 750], (
        "a `params:` callable over `resources` is no longer memoized across attempts. That is the "
        "trap #205 fell into; if snakemake has fixed it, `starsolo.smk` and ADR-0023 are arguing "
        "for a workaround that is no longer needed and should say so"
    )


#: A memory budget handed to an aligner: `--limitBAMsortRAM <x>`, `--limitGenomeGenerateRAM <x>`, ...
#: STAR ships eight `--limit*` knobs and the RAM-denominated ones are the class this rule is about.
_LIMIT_RAM_FLAG = re.compile(r"--limit\w*RAM\s+(\S+)")


def test_the_module_never_computes_a_star_memory_cap_from_the_config() -> None:
    """The same claim as the test above, generalised: no module may cap an aligner from the CONFIG.

    `test_the_star_rule_escalates_its_memory_on_retry` names one rule in one module, which is the
    right shape for the regression that actually happened and the wrong shape for the next one. In a
    rule that declares `retries`, `config["mem_mb"]` is the *first attempt's* number and nothing else,
    and only a `resources:` entry is re-expanded per attempt. That is a property of every RAM budget
    any module hands an aligner, not a fact about `starsolo_count` — and whoever adds the second one
    will be reading the neighbouring `--limit*` line, not this file.

    So the sweep is over every registered module and finds the flags by shape, exactly as
    `test_star_rules_clear_startmp_before_running_so_reruns_are_preemption_safe` sweeps for STAR
    itself. A module carrying no such flag passes vacuously and should; what may not pass silently is
    the sweep finding NO flag anywhere, so the anchor at the end is the assertion that keeps this
    from being green about nothing — today `starsolo.smk` is the only module that hands one over.

    Comments are stripped before scanning, the same lesson `keys_read_by` learned the hard way: the
    shell block's own prose names `--limitBAMsortRAM` while explaining which literals are the
    module's to own, and a scanner that reads prose as code cries wolf and then gets deleted.
    """
    seen: list[str] = []
    for name in list_modules():
        for rule, raw in _rule_blocks(get_module(name).snakefile).items():
            body = "\n".join(line.split("#")[0] for line in raw.splitlines())
            for argument in _LIMIT_RAM_FLAG.findall(body):
                seen.append(f"{name}:{rule}")
                resource = re.fullmatch(r"\{resources\.(\w+)\}", argument)
                assert resource, (
                    f"{name}:{rule} passes a RAM budget of {argument!r}. It has to come from a "
                    f"`resources:` callable over `attempt`, which is the only expression snakemake "
                    f"re-expands per attempt — a `params:` one is memoized at attempt 1, and a "
                    f"literal is fixed when the Snakefile is parsed"
                )
                binding = re.search(rf"^\s+{resource.group(1)}=(.*)$", body, re.M)
                assert binding, f"{name}:{rule} passes {argument} but declares no such resource"
                assert "attempt" in binding.group(1), (
                    f"{name}:{rule} caps the aligner with a number that does not move with the "
                    f"attempt, so a retry raises the request and leaves the cap where the first "
                    f"attempt died: {binding.group(1)}"
                )
    assert seen, (
        "no shipped module hands an aligner a `--limit*RAM` budget, so this sweep is looking at the "
        "wrong place — `starsolo_count` must pass one (STAR's default of 0 means 'reuse the genome "
        "allocation', which is too small on a small genome and FATALs)"
    )


# ================================================================================================
# metrics/stats/qc/fragments — reading a finished pipeline back
# ================================================================================================
#
# What ``seqforge report`` sees once the composed Snakefile has run. Three gates matter here and they
# are all silent failures:
#
#   * a fourth aligner is registered and reports NOTHING, because nobody added a reader;
#   * the writer renames a bundle key, the reader keeps looking up the old one, and the page quietly
#     loses a metric with nothing red;
#   * a value the tool never wrote is rendered as ``0.0`` — a number a reader will act on.
#
# The grading cases below are taken verbatim from a real STARsolo run that mapped the wrong read as
# the barcode. Telling that run apart from a healthy one is the entire point of the layer.


_HEALTHY_SUMMARY: dict[str, object] = {
    "Number of Reads": 412331205,
    "Reads With Valid Barcodes": 0.972113,
    "Sequencing Saturation": 0.6431,
    "Q30 Bases in CB+UMI": 0.966122,
    "Q30 Bases in RNA read": 0.94553,
    "Reads Mapped to Genome: Unique": 0.884221,
    "Reads Mapped to Gene: Unique Gene": 0.641902,
    "Estimated Number of Cells": 8842,
    "Fraction of Unique Reads in Cells": 0.8712,
    "Median UMI per Cell": 4213,
    "Median Gene per Cell": 1922,
    "Total Gene Detected": 21044,
}

_HEALTHY_LOG: dict[str, object] = {
    "Number of input reads": 412331205,
    "Uniquely mapped reads %": "88.42%",
    "% of reads mapped to multiple loci": "6.31%",
    "% of reads mapped to too many loci": "0.42%",
    "% of reads unmapped: too short": "3.90%",
}

#: A real STARsolo run in which the cDNA read was handed to STAR as the barcode read. Every value is
#: verbatim from its `Summary.csv` / `Log.final.out`; the four that must go red are the ones a human
#: used to catch by eye, and the reason this layer exists.
_BROKEN_SUMMARY: dict[str, object] = {
    "Number of Reads": 207946411,
    "Reads With Valid Barcodes": 0.000762759,
    "Sequencing Saturation": 0.0,
    "Q30 Bases in CB+UMI": 0.966122,
    "Q30 Bases in RNA read": 0.94553,
    "Reads Mapped to Genome: Unique": 0.259409,
    "Reads Mapped to Gene: Unique Gene": 1.68649e-05,
    "Estimated Number of Cells": 3,
    "Fraction of Unique Reads in Cells": 0.0016,
    "Median UMI per Cell": 2,
    "Median Gene per Cell": 2,
    "Total Gene Detected": 47,
}

_BROKEN_LOG: dict[str, object] = {
    "Number of input reads": 207946411,
    "Uniquely mapped reads %": "25.94%",
    "% of reads mapped to multiple loci": "5.30%",
    "% of reads mapped to too many loci": "10.90%",
    "% of reads unmapped: too short": "57.80%",
}

#: The four that separate the broken run above from the healthy one. Two come from `Summary.csv` as
#: bare fractions and two from `Log.final.out` as percent STRINGS, which is why the scale crossing has
#: a test of its own: a `"25.94%"` read as 25.94 grades `ok` against a 0.60 bar.
_CATCHES_A_BROKEN_RUN = (
    "valid_barcodes",
    "reads_in_genes",
    "uniquely_mapped",
    "unmapped_too_short",
)

#: Every metric a complete STARsolo bundle yields. Asserted as a SET, because that is the writer ->
#: reader contract: rename a key in `build_qc_bundle` and the lookup in `qc.metrics` stops resolving,
#: which costs a row here rather than failing anywhere. `input_reads` is absent on purpose — STARsolo's
#: own "reads" already reports it, and two columns of one number read as two facts.
_FULL_SOLO_METRICS = {
    "reads",
    "valid_barcodes",
    "reads_in_genes",
    "reads_in_genome",
    "cells",
    "reads_in_cells",
    "median_umi",
    "median_genes",
    "genes_detected",
    "saturation",
    "q30_cb_umi",
    "q30_rna",
    "uniquely_mapped",
    "multi_loci",
    "too_many_loci",
    "unmapped_too_short",
}


def _by_key(sample: SampleStats) -> dict[str, Metric]:
    return {m.key: m for m in sample.metrics}


def _levels(sample: SampleStats) -> dict[str, str]:
    return {m.key: m.level for m in sample.metrics}


def _bundle(summary: dict[str, object], log_final: dict[str, object]) -> dict[str, object]:
    """One bundle payload as a literal — the pure seam, with no file and no writer in the way.

    Shaped like what `build_qc_bundle` writes, which a literal cannot itself prove; that claim belongs
    to `test_the_bundle_the_writer_produces_is_the_one_the_reader_looks_up`, which goes through the
    real writer. Everything here is about judgement — thresholds, scales, absence — not about IO.
    """
    return {
        "sample": "S1",
        "summary": {"Gene": summary},
        "log_final": log_final,
        "umi_per_cell": {"Gene": [50, 30, 10]},
    }


def _finished_star_run(
    tmp_path: Path, *, summary: dict[str, object], log_final: dict[str, object]
) -> tuple[Path, Path]:
    """A STAR output tree carrying STAR's REAL `Summary.csv` / `Log.final.out` labels.

    `_fake_run` writes a two-row summary and a made-up log key, which is enough for the bundle-shape
    tests above and useless here: the reader looks rows up by STAR's exact label, so a fixture that
    invents labels can only ever prove the reader finds nothing.
    """
    solo, run_dir = _fake_run(tmp_path, ["Gene"])
    _write(solo / "Gene" / "Summary.csv", "".join(f"{k},{v}\n" for k, v in summary.items()))
    _write(run_dir / "Log.final.out", "".join(f"    {k} |\t{v}\n" for k, v in log_final.items()))
    return solo, run_dir


def _landed(results: Path, sample: str, bundle: object) -> Path:
    """Put one sample's STARsolo QC artifact where `read_pipeline_stats` looks for it."""
    path = results / sample / f"{sample}.qc.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(bundle, fh)
    return path


# -- the registry drift guard -----------------------------------------------


def test_every_registered_workflow_module_either_reports_or_says_it_does_not() -> None:
    """A fourth aligner must not land and silently report nothing on every page.

    This is the mechanism that replaces a `module == "map/starsolo"` branch in the collector — the
    same silent fall-through `read_layout_kind` and `param_block` exist to prevent. Registering a
    module without a reader is a build-time defect, so it is caught here rather than by a report that
    renders an empty results section and looks like a pipeline that has not started.

    `MODULES_WITHOUT_STATS` is now EMPTY — every shipped module reports — and both halves are still
    asserted by NAME rather than by size. "Some module is silent" was never the claim; which module is
    is, and a list that shrinks must shrink because an adapter landed rather than because a name was
    quietly dropped from the guard. Empty is also why the list must survive: it is what the guard
    compares a newly registered module against, so deleting it would delete the mechanism along with
    its backlog.
    """
    from seqforge.workflows import stats as stats_registry

    stats_registry._check_registry()
    assert set(modules_with_stats()) | MODULES_WITHOUT_STATS == set(list_modules())
    # And `MODULES_WITHOUT_STATS` is the OTHER half: a module may only be silent by saying so.
    assert not (set(modules_with_stats()) & MODULES_WITHOUT_STATS)
    assert set(modules_with_stats()) == {"map/starsolo", "map/chromap", "map/star"}
    assert MODULES_WITHOUT_STATS == frozenset()


def test_the_registry_guard_can_actually_catch_drift_in_both_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guard nobody has seen fail is a guard that may not be looking.

    Both directions are live: a module registered with no reader (the fourth aligner) and a reader
    registered for a module that no longer exists (a renamed module leaving a stale spec behind).
    `MODULES` and `_SPECS` are rebound rather than mutated, so the real registries are never touched.
    """
    from seqforge.workflows import MODULES
    from seqforge.workflows import stats as stats_registry

    monkeypatch.setattr(
        stats_registry, "MODULES", {**MODULES, "map/fourth": MODULES["map/star"]}, raising=True
    )
    with pytest.raises(AssertionError, match="map/fourth"):
        stats_registry._check_registry()

    monkeypatch.undo()
    monkeypatch.setattr(
        stats_registry,
        "_SPECS",
        {**stats_registry._SPECS, "map/ghost": stats_registry._SPECS["map/starsolo"]},
        raising=True,
    )
    with pytest.raises(AssertionError, match="unknown module"):
        stats_registry._check_registry()


def test_every_registered_workflow_module_either_cross_checks_or_says_it_does_not() -> None:
    """The stats guard again, one level in — and a module is silent only by saying so.

    A module that neither declares rules nor declares it has none is absent from every diagnosis, and
    a page that names no decision is indistinguishable from a page whose run was fine. Both halves are
    asserted BY NAME rather than by size, for the reason the stats guard gives: a list that shrinks
    must shrink because a rule landed, not because a name was quietly dropped from the guard.

    `map/chromap` and `map/star` are the shipped silence, and it is an argument rather than a backlog:
    a fragments summary has no whitelist-match rate to reason about, and bulk has no barcode at all.
    A module with no defensible rule declaring that it has none is a supported answer.
    """
    from seqforge.workflows import stats as stats_registry

    stats_registry._check_registry()
    assert set(modules_with_cross_checks()) | MODULES_WITHOUT_CROSS_CHECKS == set(
        modules_with_stats()
    )
    assert not (set(modules_with_cross_checks()) & MODULES_WITHOUT_CROSS_CHECKS)
    assert set(modules_with_cross_checks()) == {"map/starsolo"}
    assert MODULES_WITHOUT_CROSS_CHECKS == {"map/chromap", "map/star"}


def _stub_reader(path: Path, sample: str) -> SampleStats:
    """A reader the guard tests can hang a synthetic spec off. Never called; only registered."""
    return SampleStats(sample_id=sample)


def test_the_cross_check_guard_can_actually_catch_drift_in_both_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guard nobody has seen fail is a guard that may not be looking — for the second guard too.

    Both directions are live, and they are the two ways a maintainer gets this wrong: registering a
    reader and forgetting the rules (the fourth aligner), and adding a rule to a module that is still
    listed as having none (the day chromap earns one). `_SPECS` and the silence list are rebound
    rather than mutated, so the real registries are never touched.
    """
    from seqforge.workflows import stats as stats_registry

    silent_spec = stats_registry.StatsSpec(artifact="x", read=_stub_reader)
    monkeypatch.setattr(
        stats_registry, "_SPECS", {**stats_registry._SPECS, "map/star": silent_spec}, raising=True
    )
    monkeypatch.setattr(
        stats_registry,
        "MODULES_WITHOUT_CROSS_CHECKS",
        MODULES_WITHOUT_CROSS_CHECKS - {"map/star"},
        raising=True,
    )
    with pytest.raises(AssertionError, match="map/star.*declare no cross-checks"):
        stats_registry._check_registry()

    monkeypatch.undo()
    checked = stats_registry.StatsSpec(artifact="x", read=_stub_reader, checks=(chemistry_rule,))
    monkeypatch.setattr(
        stats_registry, "_SPECS", {**stats_registry._SPECS, "map/star": checked}, raising=True
    )
    with pytest.raises(AssertionError, match="both declare cross-checks"):
        stats_registry._check_registry()


def test_the_modules_findings_land_on_the_pipeline_and_never_on_the_sample(tmp_path: Path) -> None:
    """The registry runs the rules, and one-judgement-one-envelope decides where the answers go.

    `SampleStats` is what the artifact SAID; a finding is a judgement about a decision, so they are
    two envelopes and the finding rides on the pipeline. That is also what makes scope answerable at
    all: "did this fire on every sample" is a fact about the run, not about any one sample.

    Driven through `read_pipeline_stats` over real bytes rather than by calling the rule, because the
    wiring is the claim: a rule that is written and never registered fires in its own unit test and
    on nothing else.
    """
    results = tmp_path / "results"
    broken = {**_HEALTHY_SUMMARY, "Reads With Valid Barcodes": 0.000762759}
    _landed(results, "s1", {"sample": "s1", "summary": {"Gene": broken}})
    _landed(results, "s2", {"sample": "s2", "summary": {"Gene": _HEALTHY_SUMMARY}})

    stats = read_pipeline_stats("map/starsolo", results, ["s1", "s2"])

    assert stats is not None
    assert [f.sample_id for f in stats.findings] == ["s1"]
    assert stats.findings[0].alert_id == "starsolo.valid-barcodes-near-zero"
    assert not any(hasattr(s, "findings") for s in stats.samples), (
        "a finding is a judgement, and SampleStats carries what the artifact said"
    )
    # And the module that measures no barcode at all reports the same metrics it always did, with
    # nothing to say about them -- silence declared, not silence by omission.
    log = tmp_path / "bulk" / "s1" / "Log.final.out"
    log.parent.mkdir(parents=True)
    log.write_text("".join(f"    {k} |\t{v}\n" for k, v in _HEALTHY_LOG.items()))
    bulk = read_pipeline_stats("map/star", tmp_path / "bulk", ["s1"])
    assert bulk is not None
    assert bulk.samples[0].metrics and bulk.findings == []


# -- the writer/reader round trip -------------------------------------------


def test_the_bundle_the_writer_produces_is_the_one_the_reader_looks_up(tmp_path: Path) -> None:
    """`build_qc_bundle` decides the keys and `qc.metrics` looks them up — through the real writer.

    They live in one file precisely so they cannot drift, and this is what holds that shut. A test
    that hands `qc.metrics` a hand-written dict cannot catch a rename in the writer: the reader would
    keep resolving against the test's own dict while the page silently lost a column.
    """
    features: list[SoloFeature] = ["Gene"]
    solo, run_dir = _finished_star_run(tmp_path, summary=_HEALTHY_SUMMARY, log_final=_HEALTHY_LOG)
    out = write_qc_bundle(
        solo, run_dir, features, tmp_path / "S1.qc.json.gz", sample="S1", assembly="ce11"
    )

    sample = read_starsolo_metrics(out, "S1")

    assert set(_by_key(sample)) == _FULL_SOLO_METRICS
    got = _by_key(sample)
    assert got["reads"].value == 412331205
    assert got["valid_barcodes"].value == pytest.approx(0.972113)
    assert got["cells"].value == 8842
    assert got["uniquely_mapped"].value == pytest.approx(0.8842)  # "88.42%" from the text log
    assert all(m.level == "ok" for m in sample.metrics if m.key in _CATCHES_A_BROKEN_RUN)
    # The knee comes from UMIperCellSorted.txt, which only the writer knows the location of.
    assert sample.knee == [(1, 50), (2, 30), (3, 10)]
    # Which feature the headline numbers came from is stated, never implied: Gene and GeneFull
    # disagree by design, so a page that does not name the feature is showing an unlabelled number.
    assert "Gene" in sample.note


def test_the_fragments_summary_the_writer_produces_is_the_one_the_reader_looks_up(
    tmp_path: Path,
) -> None:
    """The chromap half of the same contract: `write_fragments_qc` writes, `fragments.metrics` reads.

    Driven through the real writer *and* through the registry, because those are two claims and both
    fail silently. `FragmentsQC.to_dict` decides the payload keys, so a rename there costs a row here
    instead of a column on the page; and `read_pipeline_stats` finds the file by the filename the
    registry holds, so a suffix that drifts from `fragments_suffixes` renders as a pipeline that
    looks like it never ran.

    The ATAC column set is genuinely smaller than STARsolo's — chromap's summary carries no
    whitelist-match rate, so there is no ATAC "valid barcodes" — and that is a property of the
    artifact, not an omission. Asserted as a SET, so a reader inventing a metric the writer never
    wrote fails here too.
    """
    results = tmp_path / "results"
    raw = tmp_path / "fragments.raw.tsv"
    raw.write_text(_RAW)
    out = write_fragments_qc(raw, results / "s1" / f"s1{QC_SUFFIX}", sample="s1", assembly="mm10")

    stats = read_pipeline_stats("map/chromap", results, ["s1"])

    assert stats is not None and stats.complete
    sample = stats.samples[0]
    got = _by_key(sample)
    assert set(got) == {
        "reads",
        "fragments",
        "barcodes",
        "reads_per_fragment",
        "mean_fragments_per_barcode",
        "max_fragments_per_barcode",
    }
    assert got["reads"].value == 6  # 3 + 1 + 2 read pairs behind the three fragments
    assert got["fragments"].value == 3
    assert got["barcodes"].value == 2  # AAA, CCC -- barcodes seen, NOT cells
    assert got["reads_per_fragment"].value == pytest.approx(2.0)
    assert sample.knee == []  # chromap keeps no per-barcode vector, so there is no knee to draw
    # And the registry dispatched to this module's own reader rather than to some other adapter that
    # happened to survive the same bytes -- one artifact, one owner.
    assert read_fragments_metrics(out, "s1").metrics == sample.metrics


# -- grading, on real values ------------------------------------------------


def test_the_broken_run_grades_bad_on_exactly_the_metrics_a_human_caught_it_by() -> None:
    """The whole point of the layer: telling a wrong-barcode-read run from a healthy one.

    The four values are verbatim from a run that handed STAR the cDNA read as the barcode. Both
    conventions are represented — `Summary.csv` fractions and `Log.final.out` percent strings — so a
    threshold applied to an unconverted `"25.94%"` (which reads as 25.94, comfortably above a 0.60
    bar) grades `ok` here and the test goes red.
    """
    broken = _levels(starsolo_metrics(_bundle(_BROKEN_SUMMARY, _BROKEN_LOG), "broken"))
    healthy = _levels(starsolo_metrics(_bundle(_HEALTHY_SUMMARY, _HEALTHY_LOG), "healthy"))

    assert [broken[k] for k in _CATCHES_A_BROKEN_RUN] == ["bad"] * 4
    assert [healthy[k] for k in _CATCHES_A_BROKEN_RUN] == ["ok"] * 4
    # A bar tight enough to flag ordinary biology is a bar that gets ignored: nothing in the healthy
    # run may be red, or the four above stop meaning anything.
    assert "bad" not in healthy.values()


def test_a_percent_string_and_a_bare_fraction_arrive_on_the_same_scale() -> None:
    """`Log.final.out` writes `"95.50%"`, `Summary.csv` writes `0.955`, and one threshold means one
    thing only if both land as a fraction.

    `_coerce` deliberately leaves the percent form a string — the bundle is a lossless archive and
    nothing there should be reshaped into a number it is not — which puts the whole burden of the
    crossing on the reader. An unparseable value takes the other branch: absent, never zero.
    """
    sample = starsolo_metrics(
        {
            "summary": {"Gene": {"Reads Mapped to Genome: Unique": 0.9550}},
            "log_final": {
                "Uniquely mapped reads %": "95.50%",
                "% of reads unmapped: too short": "N/A",
            },
        },
        "S1",
    )
    got = _by_key(sample)

    assert got["uniquely_mapped"].value == pytest.approx(got["reads_in_genome"].value)
    assert got["uniquely_mapped"].value == pytest.approx(0.955)
    assert got["uniquely_mapped"].display == "95.5%"
    assert got["uniquely_mapped"].level == "ok"
    assert "unmapped_too_short" not in got


def _reads_per_fragment(tenths: int) -> Metric:
    """The graded ratio at `tenths/10` read pairs per fragment, built by the real adapter."""
    payload = {"n_fragments": 10, "total_reads": tenths}
    return _by_key(fragments_metrics(payload, "s1"))["reads_per_fragment"]


def test_the_graded_ratio_is_shown_at_a_precision_that_keeps_its_verdicts_apart() -> None:
    """`reads / fragment` is graded at 2.0 and 4.0, so its display has to resolve a tenth.

    This is what `ratio` exists for beside `count`, and the fragments adapter is its first caller:
    an integer display rounds a graded ratio past its own bar, and the colour then contradicts the
    number sitting next to it. 1.9 is `ok` where 2.1 is `warn`, and 3.9 is `warn` where 4.1 is `bad`;
    under `count`'s formatter each pair collapses to ONE string — "2" and "4" — appearing in the
    table in two different colours, which a reader can only read as a rendering bug.

    One decimal is what these two bars need, and the claim is exactly that and no more: a step the
    metric can meaningfully take across a bar is visible on the page. No finite precision can promise
    that no two differently-graded values ever share a string, because values arbitrarily close to a
    bar exist on both sides of it — at a tenth the pair that still collapses has to agree with the
    bar to within 0.05, which is 2.5% of it rather than the 25% an integer would allow.
    """
    below_ok, above_ok = _reads_per_fragment(19), _reads_per_fragment(21)
    below_warn, above_warn = _reads_per_fragment(39), _reads_per_fragment(41)

    assert (below_ok.level, above_ok.level) == ("ok", "warn")
    assert (below_warn.level, above_warn.level) == ("warn", "bad")
    assert (below_ok.display, above_ok.display) == ("1.9", "2.1")
    assert (below_warn.display, above_warn.display) == ("3.9", "4.1")
    # The counterfactual, and the reason this metric is not built with `count`.
    assert fmt_int(below_ok.value) == fmt_int(above_ok.value) == "2"
    assert fmt_int(below_warn.value) == fmt_int(above_warn.value) == "4"


# -- absent degrades to absent ----------------------------------------------


def test_a_key_the_artifact_does_not_carry_becomes_an_absent_metric_never_a_zero() -> None:
    """A metric the tool never wrote must not be rendered as 0.0 — that is a number a reader acts on.

    This is the whole reason `fraction`/`count`/`ratio` return `Optional`, and it is what makes an
    old `WORKFLOW_VERSION`'s artifact render FEWER rows rather than wrong ones. The contrast is the
    point: an absent key yields no metric, while a zero the writer really wrote is data and stays —
    and it is still graded, so a genuine zero goes red rather than quietly reading as "no threshold".

    Both adapters, because the rule is the seam's and not one tool's, and because chromap's summary
    has the case STARsolo's does not: a DERIVED number whose inputs are present and whose divisor is
    zero. That one is absent for a second reason on top of the first — there is no answer to divide,
    and neither `0.0` nor `inf` may cross the JSON seam pretending there is.
    """
    thin = starsolo_metrics({"summary": {"Gene": {"Number of Reads": 1000}}}, "S1")
    assert set(_by_key(thin)) == {"reads"}

    written_zero = starsolo_metrics(
        {"summary": {"Gene": {"Number of Reads": 1000, "Reads With Valid Barcodes": 0.0}}}, "S1"
    )
    got = _by_key(written_zero)
    assert set(got) == {"reads", "valid_barcodes"}
    assert got["valid_barcodes"].value == 0.0
    assert got["valid_barcodes"].level == "bad"

    partial = fragments_metrics({"n_fragments": 10, "total_reads": 20}, "s1")
    assert set(_by_key(partial)) == {"reads", "fragments", "reads_per_fragment"}

    # A pipeline that produced no fragments: the counts are real zeros and stay, graded, while both
    # derived ratios would divide by zero and are therefore absent rather than 0.0 or inf.
    empty = fragments_metrics(
        {"n_fragments": 0, "n_barcodes": 0, "total_reads": 0, "max_fragments_per_barcode": 0}, "s1"
    )
    got = _by_key(empty)
    assert set(got) == {"reads", "fragments", "barcodes", "max_fragments_per_barcode"}
    assert got["fragments"].value == 0.0
    assert got["fragments"].level == "bad"  # a real zero is still graded


# -- read_pipeline_stats ----------------------------------------------------


def test_read_pipeline_stats_returns_none_when_there_is_nothing_to_render(tmp_path: Path) -> None:
    """Unknown module, absent results dir, nothing landed yet — for a reader all three are one fact.

    `None` and not an empty `PipelineStats`: an empty one renders a results section that says a
    pipeline produced nothing, which is a claim about the pipeline rather than about what is on disk.
    The unknown-module branch is the same one a name in `MODULES_WITHOUT_STATS` would take, which is
    what lets that list be a declaration rather than a special case in the collector; it is empty
    today, and this is the branch that would carry its next entry.
    """
    results = tmp_path / "results"
    _landed(results, "S1", _bundle(_HEALTHY_SUMMARY, _HEALTHY_LOG))

    assert read_pipeline_stats("map/nonesuch", results, ["S1"]) is None
    assert read_pipeline_stats("map/starsolo", tmp_path / "never-ran", ["S1"]) is None
    assert read_pipeline_stats("map/starsolo", results, ["S9"]) is None
    # A registered module whose OWN artifact is absent: `map/star` asks for `Log.final.out` and does
    # not read the STARsolo bundle lying beside it, however readable those bytes are. One artifact,
    # one owner — the registry dispatches on the module, never on whatever the sample directory holds.
    assert read_pipeline_stats("map/star", results, ["S1"]) is None


def test_a_partial_pipeline_reports_what_landed_and_says_how_much_did(tmp_path: Path) -> None:
    """Half a pipeline is a first-class answer, because half is what a preempted cluster leaves.

    `n_expected` comes from the composed config's own sample list — the artifact the pipeline
    consumed — so "did it finish" is answered by the files it was contracted to produce and not by
    parsing a snakemake log.
    """
    results = tmp_path / "results"
    for sample in ("S1", "S2"):
        _landed(results, sample, _bundle(_HEALTHY_SUMMARY, _HEALTHY_LOG))

    stats = read_pipeline_stats("map/starsolo", results, ["S1", "S2", "S3"])

    assert stats is not None
    assert (stats.n_found, stats.n_expected) == (2, 3)
    assert not stats.complete
    assert [s.sample_id for s in stats.samples] == ["S1", "S2"]
    # The counts, and NOT a "2 of 3 samples" sentence beside them. The reader renders the numbers, so
    # a note repeating them in words put one fact on the page twice — a heading and its own small
    # print. `notes` carries what the counts cannot say; how to phrase a count is the view's job.
    assert not any("of 3" in note for note in stats.notes), stats.notes


def test_one_unreadable_artifact_costs_its_own_row_and_not_the_whole_pipeline(
    tmp_path: Path,
) -> None:
    """A truncated bundle is what a killed job or a full disk leaves behind, and it must cost one row.

    All three corruptions arrive as different exception types — a half-written gzip stream ends in
    `EOFError`, a file that is not gzip at all in `BadGzipFile`, valid gzip holding half a JSON object
    in `JSONDecodeError` — and a reader that survives only the ones it happened to think of takes the
    whole report down for the samples that are perfectly fine.
    """
    results = tmp_path / "results"
    _landed(results, "S1", _bundle(_HEALTHY_SUMMARY, _HEALTHY_LOG))
    _landed(results, "S5", _bundle(_HEALTHY_SUMMARY, _HEALTHY_LOG))
    whole = (results / "S1" / "S1.qc.json.gz").read_bytes()
    _write(results / "S2" / "S2.qc.json.gz", "")  # placeholder so the parent dir exists
    (results / "S2" / "S2.qc.json.gz").write_bytes(whole[: len(whole) // 2])  # killed mid-write
    _write(results / "S3" / "S3.qc.json.gz", "not gzip at all")
    (results / "S4" / "S4.qc.json.gz").parent.mkdir(parents=True, exist_ok=True)
    (results / "S4" / "S4.qc.json.gz").write_bytes(gzip.compress(b'{"summary": {"Gene"'))

    stats = read_pipeline_stats("map/starsolo", results, ["S1", "S2", "S3", "S4", "S5"])

    assert stats is not None
    assert [s.sample_id for s in stats.samples] == ["S1", "S5"]
    assert stats.n_found == 2
    for broken in ("S2", "S3", "S4"):
        assert any(broken in note for note in stats.notes), (broken, stats.notes)


def test_a_caption_a_sample_carries_never_lands_in_the_could_not_be_read_list(
    tmp_path: Path,
) -> None:
    """`notes` is one kind of thing: an artifact nobody could parse. Captions stay on their sample.

    Both used to be folded into `notes`, which left the reader matching note strings back against
    `SampleStats.note` to tell them apart. That match is invisible coupling across a package seam —
    reword a caption in an adapter and it silently stops matching, so a caption reappears in the place
    a reader looks for failures. Landing a readable sample beside a corrupt one is what separates the
    two: the corrupt one must be named here, and the readable one's caption must not be, however the
    caption is worded.
    """
    results = tmp_path / "results"
    _landed(results, "S1", _bundle(_HEALTHY_SUMMARY, _HEALTHY_LOG))
    _write(results / "S2" / "S2.qc.json.gz", "not gzip at all")

    stats = read_pipeline_stats("map/starsolo", results, ["S1", "S2"])

    assert stats is not None
    carried = [s.note for s in stats.samples if s.note]
    assert carried, "this test is vacuous unless the readable sample actually carries a caption"
    assert any("S2" in note for note in stats.notes), stats.notes
    for note in carried:
        assert note not in stats.notes, (note, stats.notes)


def test_a_pipeline_whose_every_artifact_is_corrupt_does_not_read_as_never_run(
    tmp_path: Path,
) -> None:
    """Nothing readable is not the same fact as nothing written, and the page must not conflate them.

    `read_pipeline_stats` returned `None` whenever no sample parsed, and `None` is what the renderer
    turns into "this assay's pipeline has not been run yet". So the one run that most needs saying —
    it ran, it wrote, and every byte of it is unparseable — rendered as the run that never happened,
    and each per-sample failure the reader had already named was dropped on the way out. The
    partial-run test above cannot catch it: it always leaves two good samples behind.

    `None` is reserved for a results tree with nothing in it at all, which is checked here too, since
    a fix that returned stats for *both* cases would trade this defect for its mirror image.
    """
    results = tmp_path / "results"
    _write(results / "S1" / "S1.qc.json.gz", "not gzip at all")
    (results / "S2").mkdir(parents=True, exist_ok=True)
    (results / "S2" / "S2.qc.json.gz").write_bytes(gzip.compress(b'{"summary": {"Gene"'))

    stats = read_pipeline_stats("map/starsolo", results, ["S1", "S2", "S3"])

    assert stats is not None
    assert stats.samples == []
    assert (stats.n_found, stats.n_expected) == (0, 3)
    assert not stats.complete
    for broken in ("S1", "S2"):
        assert any(broken in note for note in stats.notes), (broken, stats.notes)
    # S3 never landed, so it is missing rather than broken — an absent file is not a failure to read.
    assert not any("S3" in note for note in stats.notes)

    empty = tmp_path / "empty"
    empty.mkdir()
    assert read_pipeline_stats("map/starsolo", empty, ["S1"]) is None


def test_a_bug_in_a_metric_table_is_raised_and_not_filed_as_a_corrupt_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad bytes are tolerated; bad CODE is not, and the `except` must not do both jobs.

    `KeyError`/`TypeError` are what a mistake in a metric table raises, and catching them alongside
    the corruption cases turned a logic error into a per-sample note reading "its QC artifact could
    not be read" — the report stayed green, the page silently dropped a sample, and the defect was in
    seqforge rather than on disk. The artifact here is perfectly good; only the reader is broken.
    """
    from seqforge.workflows import stats as stats_registry

    results = tmp_path / "results"
    _landed(results, "S1", _bundle(_HEALTHY_SUMMARY, _HEALTHY_LOG))

    def buggy(path: Path, sample: str) -> SampleStats:
        raise KeyError("a metric table asked for a key it never wrote")

    spec = stats_registry._SPECS["map/starsolo"]
    monkeypatch.setattr(
        stats_registry,
        "_SPECS",
        {"map/starsolo": stats_registry.StatsSpec(artifact=spec.artifact, read=buggy)},
        raising=True,
    )

    with pytest.raises(KeyError, match="never wrote"):
        read_pipeline_stats("map/starsolo", results, ["S1"])


def test_a_metric_one_sample_lacks_leaves_a_gap_rather_than_dropping_the_column(
    tmp_path: Path,
) -> None:
    """The column set is a first-seen-order UNION, so one thin sample cannot blank a column for all.

    An intersection would mean a single sample whose STAR run wrote no `Summary.csv` row silently
    deletes that metric from every other sample's row too — the whole pipeline degraded to its worst
    member, with nothing saying so.
    """
    results = tmp_path / "results"
    _landed(results, "S1", _bundle({"Number of Reads": 10, "Sequencing Saturation": 0.5}, {}))
    _landed(results, "S2", _bundle(_HEALTHY_SUMMARY, _HEALTHY_LOG))

    stats = read_pipeline_stats("map/starsolo", results, ["S1", "S2"])

    assert stats is not None
    keys = [k for k, _ in stats.columns]
    assert keys[:2] == ["reads", "saturation"]  # first-seen order, from the thin sample
    assert set(keys) == _FULL_SOLO_METRICS  # everything the full sample added is still a column
    assert len(keys) == len(set(keys))
    assert set(_by_key(stats.samples[0])) == {"reads", "saturation"}  # S1 keeps its gaps
    assert all(label for _, label in stats.columns)  # a column a human cannot name is not a column


def test_the_bulk_module_reports_from_stars_own_log_with_no_bundle_in_between(
    tmp_path: Path,
) -> None:
    """`map/star` reports with no rule in between, which is why a `StatsSpec` carries a FILENAME.

    STAR writes `Log.final.out` unasked, and no rule in the shipped `star.smk` declares, consumes or
    deletes it — asserted here rather than assumed, because that absence is the entire claim. So bulk
    reports with no `.smk` edit, hence no `WORKFLOW_VERSION` bump, hence no `run_id` invalidated and
    nothing already compiled reprocessed. A `{sample}.<suffix>` convention could not have expressed
    this artifact at all: it carries no sample name and no rule of ours names it.

    The filename is spelled out below rather than imported from `h5ad`, deliberately. A test reading
    the same constant the registry reads could only prove the two agree with each other; what has to
    hold is that both agree with what STAR itself writes, and only a literal states that separately.
    """
    blocks = _rule_blocks(get_module("map/star").snakefile)
    assert blocks, "the shipped bulk module should have rules to look at"
    assert not [name for name, body in blocks.items() if "Log.final.out" in body]

    results = tmp_path / "results"
    _write(
        results / "S1" / "Log.final.out", "".join(f"  {k} |\t{v}\n" for k, v in _BROKEN_LOG.items())
    )

    stats = read_pipeline_stats("map/star", results, ["S1"])

    assert stats is not None and stats.complete
    sample = stats.samples[0]
    # Bulk has no barcodes, no cells and no knee, so the adapter is the alignment half and nothing
    # else -- `input_reads` included, which STARsolo drops only because its own "Reads" repeats it.
    assert set(_by_key(sample)) == {
        "input_reads",
        "uniquely_mapped",
        "multi_loci",
        "too_many_loci",
        "unmapped_too_short",
    }
    assert sample.knee == []
    assert sample.note == ""  # no feature was chosen, so there is no feature to caption
    # And the grading crosses the same scale: "25.94%" read as 25.94 would sit above a 0.60 bar.
    assert _levels(sample)["uniquely_mapped"] == "bad"
    # One implementation of "what STAR's alignment log says", reached by both pipelines through it.
    assert read_star_log(results / "S1" / "Log.final.out", "S1").metrics == sample.metrics


# -- the knee vector --------------------------------------------------------


def test_the_knee_vector_is_capped_and_log_spaced_so_the_page_stays_in_budget() -> None:
    """One integer per whitelist barcode (~6.8M on 10x v3) against a 500 KB self-contained page.

    Log-spaced and not uniform because the plot is read on log axes: uniform sampling spends almost
    every point on the flat tail and draws the knee — the one feature anybody looks at — as two
    pixels. The middle POINT therefore sits near sqrt(n) in rank, not at n/2.
    """
    vector = list(range(50_000, 0, -1))

    points = knee_points(vector)
    ranks = [r for r, _ in points]

    assert len(points) <= MAX_KNEE_POINTS
    assert ranks[0] == 1 and ranks[-1] == len(vector)  # the curve's extent is exact
    assert ranks == sorted(set(ranks))
    assert all(value == vector[rank - 1] for rank, value in points)  # sampled, never interpolated
    assert ranks[len(ranks) // 2] < len(vector) // 10


def test_a_short_or_empty_knee_vector_passes_through_untouched() -> None:
    """Nothing to thin: a small sample keeps every point, and no vector at all is not a point at zero."""
    assert knee_points([]) == []
    assert knee_points([9, 4, 1]) == [(1, 9), (2, 4), (3, 1)]
    assert len(knee_points(list(range(MAX_KNEE_POINTS, 0, -1)))) == MAX_KNEE_POINTS


# -- determinism ------------------------------------------------------------


def test_which_feature_the_headline_metrics_come_from_never_depends_on_dict_order() -> None:
    """Two bundles carrying the same features must report the same numbers, whatever order they arrive.

    STAR writes one `Summary.csv` per `soloFeatures` entry and they disagree BY DESIGN (`Gene` is
    exonic, `GeneFull*` counts introns too), so picking whichever the dict yielded first would make
    the reported number a function of JSON key order — a page that changes without the pipeline
    changing.
    """
    preferred = {"GeneFull": {"Number of Reads": 1}, "Gene": {"Number of Reads": 2}}
    assert [m.value for m in starsolo_metrics({"summary": preferred}, "S1").metrics] == [2.0]

    # Neither is in the preference list, so the tie is broken by sorting rather than by insertion.
    unranked: dict[str, object] = {"Velocyto": {"Number of Reads": 5}, "SJ": {"Number of Reads": 9}}
    forward = starsolo_metrics({"summary": dict(unranked)}, "S1")
    backward = starsolo_metrics({"summary": dict(reversed(list(unranked.items())))}, "S1")

    assert forward.note == backward.note
    assert forward.metrics == backward.metrics


# ================================================================================================
# cross-checks — which decision does a bad number implicate?
# ================================================================================================
#
# The compiler holds both halves — what it decided, and what came back — and joins them here. A rule
# is a pure function over ONE sample's metrics, so every test below drives it with literal values and
# asserts the alert or asserts silence. No filesystem, no composed pipeline, no rendered page.
#
# The bar every one of these has to clear: **a rule that fires on a healthy run is worse than a rule
# that does not exist.** So each has a value that must fire, a value that must not, and the boundary.


def _fire(rule: Callable[[SampleStats], list[Finding]], **metrics: float) -> list[Finding]:
    """Drive one rule with literal metric values, through the builders production uses.

    Through `fraction()` rather than by constructing `Metric` directly, because a rule reads
    `metric.value` and the builders are what decide what lands there — a test that hand-builds the
    metric can agree with a rule that reads the wrong scale.
    """
    built = [fraction(key, key, value, group="barcode") for key, value in metrics.items()]
    return rule(SampleStats(sample_id="S1", metrics=[m for m in built if m is not None]))


def test_the_chemistry_rule_fires_on_the_real_run_that_had_the_wrong_read_as_the_barcode() -> None:
    """0.076% valid barcodes, verbatim from the run in #215 — and the whole reason this layer exists.

    What the rule must produce is not "bad": the metric already says bad, and a reader who does not
    know STARsolo reads that as a bad library. It must name the two DECISIONS that produce this
    number — which kit was called, and which file was handed over as the barcode read — because those
    are the only two things a reader can act on, and the compiler made both.
    """
    findings = _fire(chemistry_rule, **{"valid_barcodes": 0.000762759})

    assert len(findings) == 1
    (found,) = findings
    assert found.alert_id == "starsolo.valid-barcodes-near-zero"
    assert found.severity == "likely"
    assert set(found.implicates) == {"chemistry", "read_roles"}
    assert "0.1%" in found.measured  # the value it fired on, in the reader's units
    assert found.sample_id == "S1"


def test_the_chemistry_rule_stays_silent_on_a_healthy_run() -> None:
    """An alert that fires on a good run is noise, and noise is how every alert stops being read."""
    assert _fire(chemistry_rule, **{"valid_barcodes": 0.972113}) == []


def test_the_chemistry_rules_boundary_is_the_one_threshold_it_declares() -> None:
    """Below 1% no real library exists; at or above it the number is bad but the cause is not decided.

    A merely poor barcode rate — a degraded barcode read, a contaminated library, a related-but-wrong
    kit — is already tinted `bad` by the metric's own threshold, and the rule deliberately does not
    claim it: between 1% and the metric's 50% bar there is more than one explanation, so naming one
    decision would be a guess wearing a diagnosis.
    """
    assert _fire(chemistry_rule, **{"valid_barcodes": NEAR_ZERO_VALID_BARCODES}) == []
    assert len(_fire(chemistry_rule, **{"valid_barcodes": NEAR_ZERO_VALID_BARCODES - 1e-9})) == 1
    # Well inside "bad" by the metric's own grading, and still not this rule's claim to make.
    assert _fire(chemistry_rule, **{"valid_barcodes": 0.31}) == []


def test_a_module_that_never_measured_a_barcode_is_not_a_module_with_zero_barcodes() -> None:
    """An absent metric is absent, never a zero — the same rule the metric table already lives by.

    Bulk STAR has no barcode at all, and reading "no valid-barcode rate" as "a valid-barcode rate of
    zero" would fire the loudest alert this system has on every bulk run ever compiled.
    """
    assert _fire(chemistry_rule, **{"uniquely_mapped": 0.91}) == []
    assert chemistry_rule(SampleStats(sample_id="S1")) == []


# -- from findings to alerts ------------------------------------------------


def _found(sample: str, alert_id: str = "a", **kw: object) -> Finding:
    return Finding(
        alert_id=alert_id,
        sample_id=sample,
        title=kw.pop("title", "something looks wrong"),  # type: ignore[arg-type]
        severity=kw.pop("severity", "likely"),  # type: ignore[arg-type]
        measured=f"{sample} measured something",
        implicates=kw.pop("implicates", ["chemistry"]),  # type: ignore[arg-type]
        remedy="change something",
    )


def _resolves(decision: Decision) -> DecisionRef | None:
    return DecisionRef(decision=decision, label=decision, value="whatever it is set to")


def test_an_alert_firing_on_every_sample_is_a_different_claim_from_one_firing_on_a_well() -> None:
    """Systematic points at a decision; isolated points at a sample. The shape must say which.

    The distinction is the difference between "recompose this dataset" and "look at well B7", and a
    reader cannot draw it from a list of sample ids: on a 96-well plate, 96 ids and 94 ids look the
    same. So it is computed against what LANDED, and carried.
    """
    every = gather_alerts(
        [_found("S1"), _found("S2"), _found("S3")], n_samples=3, resolve=_resolves
    )
    some = gather_alerts([_found("S1")], n_samples=3, resolve=_resolves)

    assert [a.scope for a in every] == ["systematic"]
    assert [a.scope for a in some] == ["isolated"]
    assert every[0].samples == ["S1", "S2", "S3"] and every[0].n_samples == 3
    assert some[0].samples == ["S1"] and some[0].n_samples == 3
    # And what each sample measured survives the grouping, in sample order — an alert that collapsed
    # three samples to one number would have thrown away the evidence for its own claim.
    assert every[0].measured == [
        "S1 measured something",
        "S2 measured something",
        "S3 measured something",
    ]


def test_an_alert_names_the_decision_with_the_value_the_recipe_currently_carries() -> None:
    """ "Your chemistry call looks wrong" is only actionable once it says what the call currently IS.

    The rule cannot know that — it is pure over metrics — so attribution is injected. A decision the
    workspace cannot answer for (a manifest that was never composed) is dropped rather than rendered
    as an empty row, because a field name with no value beside it reads as a value of nothing.
    """
    alerts = gather_alerts([_found("S1")], n_samples=1, resolve=_resolves)
    assert [d.decision for d in alerts[0].implicates] == ["chemistry"]
    assert alerts[0].implicates[0].value == "whatever it is set to"

    unresolvable = gather_alerts([_found("S1")], n_samples=1, resolve=lambda _d: None)
    assert unresolvable[0].implicates == []


def test_alerts_come_back_in_one_total_order_however_the_findings_arrived() -> None:
    """The page is byte-deterministic, so two renders of one workspace must order alerts identically.

    Severity first — a reader triages the loudest — then the stable id, which is total because two
    findings sharing an id are one alert by construction.
    """
    findings = [
        _found("S2", "z-check", severity="possible"),
        _found("S1", "a-check"),
        _found("S1", "z-check", severity="possible"),
    ]
    forward = gather_alerts(findings, n_samples=2, resolve=_resolves)
    backward = gather_alerts(list(reversed(findings)), n_samples=2, resolve=_resolves)

    assert [a.id for a in forward] == ["a-check", "z-check"]
    assert forward == backward


def test_every_severity_an_alert_can_carry_says_what_it_means_in_words() -> None:
    """A closed set, guarded from its own `Literal` — a third severity must break this, never render.

    The same exhaustiveness shape `Basis`, `Level` and `MetricGroup` already use: derived with
    `get_args` rather than hand-listed, so adding a member and forgetting its phrase goes red here
    instead of shipping a badge whose word is a raw token.
    """
    assert set(get_args(Severity)) == set(SEVERITY_PHRASE)
    assert all(SEVERITY_PHRASE[s] for s in get_args(Severity))


# ================================================================================================
# per-feature counts, and the nuclear-library rule they make measurable
# ================================================================================================
#
# `build_qc_bundle` has always written one `Summary.csv` per `soloFeatures` feature, so the exonic
# versus full-length gap has been on disk since the first bundle ever produced. What hid it was the
# READER: `_pick_feature` selects one feature and reports its numbers. So nothing below changes the
# writer, no `.smk` file and no `WORKFLOW_VERSION` — the per-sample artifact does not grow by a byte,
# and the only thing that grows is a narrow per-feature mapping on `SampleStats`.
#
# The constraint that binds all of it: every value the metrics table showed before must be identical
# after. Carrying more is not licence to change what is already carried.


def _multi_feature_run(
    tmp_path: Path, summaries: dict[str, dict[str, object]]
) -> tuple[Path, Path]:
    """A STAR output tree with one real `Summary.csv` per feature — the multi-feature bundle.

    `_finished_star_run` writes exactly one feature, which is enough for every test above and useless
    here: the whole signal this section is about is the DISAGREEMENT between two features, and a
    fixture carrying one of them can only prove there is none.
    """
    features: list[SoloFeature] = list(summaries)  # type: ignore[arg-type]
    solo, run_dir = _fake_run(tmp_path, features)
    for feature, rows in summaries.items():
        _write(solo / feature / "Summary.csv", "".join(f"{k},{v}\n" for k, v in rows.items()))
    _write(run_dir / "Log.final.out", "".join(f"    {k} |\t{v}\n" for k, v in _HEALTHY_LOG.items()))
    return solo, run_dir


#: The nuclear library from #215, as STAR wrote it: 30.1% of reads land in an exon and 70.8% land
#: anywhere in the gene body. The 40.7-point difference is the share of the whole library that is
#: intronic-only, and it was invisible on the page because only one of the two columns was ever read.
_NUCLEAR_GENE: dict[str, object] = {**_HEALTHY_SUMMARY, "Reads Mapped to Gene: Unique Gene": 0.301}
_NUCLEAR_GENEFULL: dict[str, object] = {
    **_HEALTHY_SUMMARY,
    "Reads Mapped to GeneFull: Unique GeneFull": 0.708,
}


def test_the_bundle_always_carried_every_feature_and_the_reader_now_carries_the_counts_up(
    tmp_path: Path,
) -> None:
    """Per-feature counts reach `SampleStats` out of the bundle the real writer produced.

    Through `write_qc_bundle` rather than a literal dict, for the reason its neighbour above gives:
    the writer decides which `Summary.csv` lands under which key, and a hand-written dict cannot
    catch a rename there. `SJ` is written and must NOT come back — it has a `Summary.csv` with no
    cell-level rows in it, which is exactly what `_NO_CELL_SUMMARY` already names.
    """
    solo, run_dir = _multi_feature_run(
        tmp_path,
        {"Gene": _NUCLEAR_GENE, "GeneFull": _NUCLEAR_GENEFULL, "SJ": {"Number of Reads": 1}},
    )
    out = write_qc_bundle(
        solo, run_dir, ["Gene", "GeneFull", "SJ"], tmp_path / "S1.qc.json.gz", sample="S1"
    )

    sample = read_starsolo_metrics(out, "S1")

    assert sample.feature_reads_in_genes == {"Gene": 0.301, "GeneFull": 0.708}
    assert "SJ" not in sample.feature_reads_in_genes
    # The measurement behind "any growth in the per-sample artifact is bounded": there is NONE. The
    # writer emitted every feature's `Summary.csv` before this ticket and emits exactly the same keys
    # after it, so the bytes on disk did not move and no `run_id` was invalidated. Asserted as a set,
    # so a writer that started emitting a per-feature block to serve this reader goes red here rather
    # than quietly costing every already-compiled dataset a reprocess.
    with gzip.open(out, "rt", encoding="utf-8") as fh:
        written = json.load(fh)
    assert set(written) == {
        "sample",
        "assembly",
        "soloFeatures",
        "barcodes_stats",
        "summary",
        "features_stats",
        "umi_per_cell",
        "default_filtered_barcodes",
        "log_final",
        "log_out",
        "log_progress",
        "splice_junctions",
    }


def test_carrying_per_feature_counts_moves_no_value_the_metrics_table_already_showed(
    tmp_path: Path,
) -> None:
    """The hard criterion, asserted rather than assumed: the table is identical either way.

    Two bundles from the real writer over the SAME `Gene` summary, one of them carrying a second
    feature whose numbers disagree by 40 points. Same keys, same values, same order, same note — the
    headline metrics still come from `_pick_feature` and `_FEATURE_PREFERENCE` did not move. Reverse
    that preference and this goes red on every value at once, which is what makes it a test rather
    than a restatement of the implementation.

    The note is asserted too, because "which feature the headline numbers come from is still stated"
    is its own acceptance criterion, and a silent feature swap would satisfy a metric comparison on a
    page that had stopped saying so.
    """
    one, one_run = _multi_feature_run(tmp_path / "one", {"Gene": _NUCLEAR_GENE})
    two, two_run = _multi_feature_run(
        tmp_path / "two", {"Gene": _NUCLEAR_GENE, "GeneFull": _NUCLEAR_GENEFULL}
    )
    single = read_starsolo_metrics(
        write_qc_bundle(one, one_run, ["Gene"], tmp_path / "one.qc.json.gz", sample="S1"), "S1"
    )
    multi = read_starsolo_metrics(
        write_qc_bundle(
            two, two_run, ["Gene", "GeneFull"], tmp_path / "two.qc.json.gz", sample="S1"
        ),
        "S1",
    )

    assert [m.key for m in multi.metrics] == [m.key for m in single.metrics]
    assert multi.metrics == single.metrics
    assert set(_by_key(multi)) == _FULL_SOLO_METRICS
    assert multi.note == single.note == "counted from the Gene feature"
    # The exonic number is what the table shows, though the bundle also carries the other one.
    assert _by_key(multi)["reads_in_genes"].value == pytest.approx(0.301)
    # ...and the number the table does NOT show is the one that made the rule possible.
    assert multi.feature_reads_in_genes["GeneFull"] == pytest.approx(0.708)
    # The mapping and the metric read the SAME `Summary.csv` row, spelled in two places (the metric
    # definition is another ticket's in this PR). If they ever stop meaning the same measurement, a
    # rule comparing features would be subtracting a different number from the one on the page.
    assert multi.feature_reads_in_genes["Gene"] == _by_key(multi)["reads_in_genes"].value


def _gap(**per_feature: float) -> list[Finding]:
    """Drive the rule with literal per-feature counts. Pure — no filesystem, no bundle."""
    return solo_features_rule(SampleStats(sample_id="S1", feature_reads_in_genes=per_feature))


def test_the_nuclear_library_rule_fires_on_the_gap_measured_in_215() -> None:
    """40.7 points of the library are intronic-only, and the primary matrix throws them away.

    What the rule adds over the two numbers is the DECISION: `soloFeatures` is an ordered list and
    element 0 is the matrix everything downstream reads, so the reader's lever is which feature comes
    first. Severity is `possible` and not `likely` — counting exonically can be deliberate, so this
    says "you are probably counting the wrong feature", never "this run is wrong".
    """
    findings = _gap(Gene=0.301, GeneFull=0.708)

    assert len(findings) == 1
    (found,) = findings
    assert found.alert_id == "starsolo.intronic-reads-uncounted"
    assert found.severity == "possible"
    assert found.implicates == ["solo_features"]
    assert found.sample_id == "S1"
    assert "40.7%" in found.measured  # the gap it fired on, in the reader's units
    assert "GeneFull" in found.measured and "Gene" in found.measured
    # The remedy may only REORDER the list: `SoloQuant` rules that dropping a feature is the one
    # irreversible act available, so a remedy saying "replace Gene" would contradict a validator in
    # this same repo -- which is worse than no remedy.
    assert "first" in found.remedy
    assert "replace" not in found.remedy.lower() and "drop" not in found.remedy.lower()


def test_a_pipeline_that_counted_one_feature_has_no_gap_to_measure_and_never_fires() -> None:
    """The common case — `soloFeatures` is frequently just `Gene` — and it must report normally.

    Silence here is by construction rather than by a special case: the measurement is a DIFFERENCE
    between two features, and one feature is not two. Both directions are asserted, because a rule
    that read a missing exonic count as zero would fire on every `GeneFull`-only pipeline and claim
    the whole library was intronic.
    """
    assert _gap(Gene=0.301) == []
    assert _gap(GeneFull=0.708) == []
    assert _gap() == []
    assert solo_features_rule(SampleStats(sample_id="S1")) == []


def test_the_intronic_gap_an_ordinary_whole_cell_library_carries_stays_silent() -> None:
    """A whole-cell library carries a real intronic fraction, and a rule that fires on it is noise.

    Commonly ten to twenty points of the library, which is why the bar is not at 0.20: a rule that
    fires on a healthy run is worse than a rule that does not exist.
    """
    assert _gap(Gene=0.641902, GeneFull=0.7712) == []  # ~13 points, ordinary biology
    assert _gap(Gene=0.60, GeneFull=0.55) == []  # full-length counted LESS: not a claim to make


def test_the_nuclear_library_rules_boundary_is_the_one_threshold_it_declares() -> None:
    """At the bar exactly it fires, a hair under it does not — `>=`, and the number is written once.

    30 points clears an ordinary whole-cell intronic fraction with room, and the measured failure was
    40.7. Between the two there is no library this rule would rather stay quiet about.
    """
    assert len(_gap(Gene=0.30, GeneFull=0.30 + INTRONIC_ONLY_READ_SHARE)) == 1
    assert _gap(Gene=0.30, GeneFull=0.30 + INTRONIC_ONLY_READ_SHARE - 1e-9) == []


def test_the_full_length_features_the_rule_compares_come_from_the_aligners_own_vocabulary() -> None:
    """Derived from `SoloFeature`, so a seventh feature cannot silently fall out of the comparison.

    Every member of STARsolo's closed vocabulary that carries a cell-level summary is either THE
    exonic count or something broader than it, so the set is a complement over that vocabulary rather
    than three names typed out. `SJ` and `Velocyto` are excluded by `_NO_CELL_SUMMARY`, the constant
    the reader already uses for exactly this — one owner of "which features have cell-level rows".
    """
    from seqforge.workflows.qc import _NO_CELL_SUMMARY

    countable = set(get_args(SoloFeature)) - _NO_CELL_SUMMARY
    assert countable - {"Gene"}, "the vocabulary must carry something broader than exons"

    # Every full-length member of the vocabulary, one at a time: each is enough on its own.
    for feature in sorted(countable - {"Gene"}):
        assert len(_gap(**{"Gene": 0.30, feature: 0.65})) == 1, feature
    # And neither of the two that have no cell-level rows can contribute a gap.
    for feature in sorted(_NO_CELL_SUMMARY):
        assert _gap(**{"Gene": 0.30, feature: 0.99}) == []


def test_the_rule_takes_the_largest_gap_when_several_full_length_features_were_counted() -> None:
    """One alert per sample, reporting the widest disagreement the run actually measured.

    Counting three ways is legal and cheap, and averaging them would report a number no feature
    produced. The largest is the one that says how much of the library the primary matrix is missing.
    """
    (found,) = _gap(Gene=0.301, GeneFull_Ex50pAS=0.52, GeneFull=0.708)

    assert "40.7%" in found.measured and "GeneFull:" in found.measured


def test_the_feature_gap_rule_reaches_the_pipeline_through_the_registry(tmp_path: Path) -> None:
    """A rule that is written and never registered fires in its own unit test and on nothing else.

    Driven through `read_pipeline_stats` over real bytes, like its sibling above: the wiring is the
    claim. `map/starsolo` declares three rules, and each one has a guard of its own here.
    """
    from seqforge.workflows import stats as stats_registry

    results = tmp_path / "results"
    _landed(
        results,
        "s1",
        {"sample": "s1", "summary": {"Gene": _NUCLEAR_GENE, "GeneFull": _NUCLEAR_GENEFULL}},
    )
    _landed(results, "s2", {"sample": "s2", "summary": {"Gene": _HEALTHY_SUMMARY}})

    stats = read_pipeline_stats("map/starsolo", results, ["s1", "s2"])

    assert stats is not None
    assert [(f.sample_id, f.alert_id) for f in stats.findings] == [
        ("s1", "starsolo.intronic-reads-uncounted")
    ]
    assert solo_features_rule in stats_registry._SPECS["map/starsolo"].checks


# -- the gene model, and the strand -----------------------------------------
#
# The second rule on the same rails, and the one whose SILENCE is half its specification: it reads
# two numbers that already exist, and speaks only where their combination decides a cause.


def test_the_gene_model_rule_fires_when_the_reads_map_and_the_genes_do_not() -> None:
    """Mapping healthy, counting near-empty — the reads found the genome and the genome had no genes.

    That combination is not a bad library, and it is precisely the one a reader who does not know
    STARsolo cannot read: both numbers look like alignment and only one of them is. What the rule
    adds is the two DECISIONS that produce it — which GTF was registered, and which strand the
    counter was told this kit's cDNA read sits on — because those are the only two things a reader
    can act on, and the compiler made both.
    """
    findings = _fire(gene_model_rule, reads_in_genome=0.884221, reads_in_genes=0.031)

    assert len(findings) == 1
    (found,) = findings
    assert found.alert_id == "starsolo.reads-mapped-but-not-counted"
    assert found.severity == "likely"
    assert set(found.implicates) == {"annotation", "strand"}
    # Both values it fired on, in the reader's units — the evidence for its own claim.
    assert "88.4%" in found.measured and "3.1%" in found.measured
    assert found.sample_id == "S1"


def test_the_gene_model_rule_stays_silent_when_both_numbers_are_healthy() -> None:
    """An alert that fires on a good run is noise, and noise is how every alert stops being read."""
    assert _fire(gene_model_rule, reads_in_genome=0.884221, reads_in_genes=0.641902) == []


def test_the_gene_model_rule_stays_silent_on_the_run_whose_barcode_read_was_wrong() -> None:
    """Both numbers poor is a MAPPING problem, and this rule is not entitled to claim it.

    `_BROKEN_SUMMARY` is the real run that had the cDNA read handed to STAR as the barcode: 25.9% of
    reads mapped uniquely and essentially none reached a gene. That run belongs to the chemistry
    rule, and a page firing two contradictory diagnoses at one run is worse than either alone.

    So both directions are asserted rather than the silence alone: a build where this rule had been
    wired to the wrong comparison goes red on the first line, and a build where the fixture had
    stopped raising anything at all goes red on the second.
    """
    broken = starsolo_metrics(_bundle(_BROKEN_SUMMARY, _BROKEN_LOG), "broken")

    assert gene_model_rule(broken) == []
    assert len(chemistry_rule(broken)) == 1, (
        "the fixture must still raise the chemistry alert, or this asserts silence about nothing"
    )


def test_the_gene_model_rules_boundaries_are_the_two_bars_it_reuses() -> None:
    """Two numbers this file already argues elsewhere, reused rather than invented a third time.

    `0.60` is the bar `uniquely_mapped` uses for "the genome is the right genome", and this rule's
    precondition is exactly "mapping is not the problem"; `0.15` is `reads_in_genes`'s own `bad`
    boundary, so the rule fires precisely where the page already tints that cell red. Two different
    numbers for one claim is how they drift apart, so each bar is asserted AT its value and one step
    off it.
    """
    poor = POOR_GENE_ASSIGNMENT - 1e-9
    at_bar = _fire(gene_model_rule, reads_in_genome=HEALTHY_GENOME_MAPPING, reads_in_genes=poor)
    below = _fire(
        gene_model_rule, reads_in_genome=HEALTHY_GENOME_MAPPING - 1e-9, reads_in_genes=poor
    )
    assert len(at_bar) == 1 and below == []

    healthy = HEALTHY_GENOME_MAPPING
    assert (
        _fire(gene_model_rule, reads_in_genome=healthy, reads_in_genes=POOR_GENE_ASSIGNMENT) == []
    )
    assert len(_fire(gene_model_rule, reads_in_genome=healthy, reads_in_genes=poor)) == 1


def test_an_index_that_carries_no_gene_model_leaves_this_rule_nothing_to_read() -> None:
    """ "A pipeline whose aligner index carries no gene model never triggers it" — by construction.

    `GenomeRef.annotation_name` is `None` exactly when there is no GTF, and with no GTF STAR writes
    no gene rows into `Summary.csv` at all, so `reads_in_genes` is simply not a metric. The rule
    needs no annotation parameter to know that: it needs the number to be absent, which it is.
    Driving it with the metric missing is therefore the honest test of the claim — a rule handed the
    name of the annotation would be testing a different sentence, and would stop being pure over one
    sample's metrics.

    Both halves, because either number alone is half a comparison, and an absent metric is absent and
    never a zero. That is also what keeps every bulk run silent: bulk measures no gene assignment.
    """
    assert _fire(gene_model_rule, reads_in_genome=0.91) == []
    assert _fire(gene_model_rule, reads_in_genes=0.02) == []
    assert gene_model_rule(SampleStats(sample_id="S1")) == []


def test_the_gene_model_rule_is_registered_and_not_merely_written(tmp_path: Path) -> None:
    """A rule that is written and never registered fires in its own unit test and on nothing else.

    Driven through `read_pipeline_stats` over real bytes, exactly as the chemistry rule's wiring is:
    what is asserted here is the entry on `map/starsolo`'s `StatsSpec`, not the function above it.
    The healthy second sample is the discriminator — a rule wired to fire unconditionally would name
    it too.
    """
    results = tmp_path / "results"
    uncounted = {**_HEALTHY_SUMMARY, "Reads Mapped to Gene: Unique Gene": 0.021}
    _landed(results, "s1", {"sample": "s1", "summary": {"Gene": uncounted}})
    _landed(results, "s2", {"sample": "s2", "summary": {"Gene": _HEALTHY_SUMMARY}})

    stats = read_pipeline_stats("map/starsolo", results, ["s1", "s2"])

    assert stats is not None
    assert [(f.sample_id, f.alert_id) for f in stats.findings] == [
        ("s1", "starsolo.reads-mapped-but-not-counted")
    ]


# -- what the review found: two rules that fired on runs they had no claim on ------------------


def test_the_gene_assignment_bar_is_one_number_the_metric_and_the_rule_both_read() -> None:
    """The rule claims to fire where the page already tints red. This is what makes that true.

    Both docstrings said the rule reuses `reads_in_genes`' own `warn` floor, and both were *copied
    literals* — regrading the metric would have left the rule behind, silently, with the comment still
    claiming otherwise. A remembered rule is the thing this repo replaces with a mechanism, so the
    constant is now passed to `fraction()` and read by the rule, and this asserts they are one number
    rather than two that currently agree.
    """
    graded = _by_key(starsolo_metrics(_bundle(_HEALTHY_SUMMARY, _HEALTHY_LOG), "S1"))[
        "reads_in_genes"
    ]

    # The metric's own boundary, recovered from its behaviour rather than from its source: `warn` is
    # the value at which it stops grading `bad`.
    assert grade(POOR_GENE_ASSIGNMENT, ok=0.30, warn=POOR_GENE_ASSIGNMENT) == "warn"
    assert grade(POOR_GENE_ASSIGNMENT - 1e-9, ok=0.30, warn=POOR_GENE_ASSIGNMENT) == "bad"
    assert graded.level == "ok"  # and the healthy fixture is nowhere near it

    # The rule fires exactly below that boundary and not at it -- the two ends of one number.
    assert _fire(gene_model_rule, reads_in_genome=0.9, reads_in_genes=POOR_GENE_ASSIGNMENT) == []
    assert (
        len(_fire(gene_model_rule, reads_in_genome=0.9, reads_in_genes=POOR_GENE_ASSIGNMENT - 1e-9))
        == 1
    )


def test_a_nuclear_library_already_counted_full_length_is_not_told_to_do_what_it_did() -> None:
    """The gap survives the fix, so measuring it is not enough to speak about it.

    A nuclear prep still has intronic reads after `GeneFull` is made primary — the measurement is a
    fact about the library, not about the recipe. Firing on it anyway raised "you are counting the
    wrong feature" at a reader counting the right one, with a remedy telling them to reorder a list
    they had already reordered. `primary_feature` is what separates "counting exonically" from
    "counting exonically BY MISTAKE", and the claim is about the matrix, not about the biology.
    """
    gap = {"Gene": 0.301, "GeneFull": 0.708}
    counted_wrong = SampleStats(sample_id="s", feature_reads_in_genes=gap, primary_feature="Gene")
    counted_right = SampleStats(
        sample_id="s", feature_reads_in_genes=gap, primary_feature="GeneFull"
    )

    assert [f.alert_id for f in solo_features_rule(counted_wrong)] == [
        "starsolo.intronic-reads-uncounted"
    ]
    assert solo_features_rule(counted_right) == []
    # A bundle written before the field was carried says nothing about the recipe, so the rule keeps
    # its old behaviour rather than going silent on every archived run.
    assert len(solo_features_rule(SampleStats(sample_id="s", feature_reads_in_genes=gap))) == 1


def test_the_gene_model_rule_yields_to_the_feature_that_counted_the_same_reads_fine() -> None:
    """One run, two rules, and only one of them has a claim — the louder one had it wrong.

    The headline `reads_in_genes` comes from whichever feature `_pick_feature` selected, which is
    `Gene` wherever it exists. So a nuclear library counted exonically shows healthy mapping beside a
    poor exonic count and looks *exactly* like a wrong annotation. It is not one, and the bundle
    already proves it: a `GeneFull` count in the same artifact shows the reads did land in genes. The
    rule that fires must be the one naming the feature, not the one naming the annotation.
    """
    nuclear = SampleStats(
        sample_id="s",
        metrics=[
            m
            for m in (
                fraction("reads_in_genome", "g", 0.85, group="alignment"),
                fraction("reads_in_genes", "c", 0.08, group="counts"),
            )
            if m is not None
        ],
        feature_reads_in_genes={"Gene": 0.08, "GeneFull": 0.62},
        primary_feature="Gene",
    )

    assert gene_model_rule(nuclear) == []
    assert [f.alert_id for f in solo_features_rule(nuclear)] == [
        "starsolo.intronic-reads-uncounted"
    ]

    # And it still fires when NO other feature counted them: then the annotation really is suspect.
    only_exonic = nuclear.model_copy(update={"feature_reads_in_genes": {"Gene": 0.08}})
    assert [f.alert_id for f in gene_model_rule(only_exonic)] == [
        "starsolo.reads-mapped-but-not-counted"
    ]


def test_every_mate_of_one_sample_is_ordered_the_same_way() -> None:
    """The ordering all three mapping modules import, and the reason it is not `path` alone.

    Every aligner seqforge composes reads its mates in lockstep, so mate A's file list and mate B's
    must agree file-for-file. Since a run went lane-blind (`docs/adr/0027`) a multi-lane library is
    ONE run, so `run` ties across every file and only `lane` still separates them.

    The rows below are the shape that makes this load-bearing: the barcode filenames sort AGAINST
    lane order while the cDNA filenames sort with it, which is what a `(run, path)` sort would get
    wrong. Real bcl2fastq names happen to sort correctly, which is exactly why these are not real
    bcl2fastq names -- the guarantee must come from the column, not from a naming coincidence.
    """
    from seqforge.workflows.units import ordered_fastqs

    units = [
        {"sample_id": "s1", "run": "lib_S1", "lane": "L002", "read_id": "R2", "path": "d_cdna.fq"},
        {"sample_id": "s1", "run": "lib_S1", "lane": "L001", "read_id": "R2", "path": "c_cdna.fq"},
        {"sample_id": "s1", "run": "lib_S1", "lane": "L002", "read_id": "R1", "path": "a_bc.fq"},
        {"sample_id": "s1", "run": "lib_S1", "lane": "L001", "read_id": "R1", "path": "z_bc.fq"},
        {"sample_id": "s2", "run": "other_S2", "lane": "L001", "read_id": "R1", "path": "x_bc.fq"},
    ]

    cdna = ordered_fastqs(units, "s1", "R2")
    barcode = ordered_fastqs(units, "s1", "R1")
    assert cdna == ["c_cdna.fq", "d_cdna.fq"]  # L001, L002
    assert barcode == ["z_bc.fq", "a_bc.fq"]  # L001, L002 -- NOT lexical
    assert [u["lane"] for u in units if u["path"] in cdna] == ["L002", "L001"]

    # The other sample's file never leaks in, and an absent role is empty rather than an error.
    assert ordered_fastqs(units, "s1", "I1") == []
    assert ordered_fastqs(units, "s2", "R1") == ["x_bc.fq"]


# ---- the plate-assay UMI extractor ---------------------------------------------------------------
#
# `workflows/umite/` is seqforge's own re-implementation of the counting engine a plate assay needs,
# and this half is the extractor. Every number pinned below was measured against the reference
# package on ten published GSE207085 cells while it was still installed (2026-08-04); none of them is
# a preference, and none should be relaxed to make a test pass.

#: The tag as the element model declares it -- 11 bp off the template-switch oligo's 3' end. Spelled
#: HERE and nowhere under `src/`: the extractor DERIVES it from a read layout, so a test that
#: imported a module constant would only be checking that a literal equals itself.
_TAG = "ATTGCGCAATG"
#: What Tn5 mosaic-end read-through leaves in front of the tag, and the only reason a tag is ever at
#: a non-zero offset at all. Repeated so any prefix length can be cut from it; it shares no prefix
#: with the tag, so an offset case cannot match at the wrong place by luck.
_READ_THROUGH = "CTGTCTCTTATACACATCT" * 3
#: A cDNA tail long enough to be a read an aligner would accept.
_CDNA = "GATCACAGGTCTATCACCCTATTAACCACTCACGGGAGCTCTCCATGCATTTGGTATTTT"


def _smartseq3_r1() -> ReadDef:
    """R1 as an element model states it: an 11 bp tag, an 8 bp UMI, `GGG`, and cDNA from 22.

    Elements rather than numbers, because the numbers are exactly what the extractor must derive.
    """
    return ReadDef(
        read_id="R1",
        strand="pos",
        min_len=40,
        max_len=150,
        elements=[
            ReadElement(
                role="linker", region_type="custom_primer", start=0, length=11, sequence=_TAG
            ),
            ReadElement(role="UMI", region_type="umi", start=11, length=8),
            ReadElement(role="linker", region_type="linker", start=19, length=3, sequence="GGG"),
            ReadElement(role="cDNA", region_type="cdna", start=22),
        ],
    )


def _plain_read(read_id: str) -> ReadDef:
    """A read that is cDNA and nothing else -- R2 here, and both reads of a bulk layout."""
    return ReadDef(
        read_id=read_id,
        strand="neg",
        min_len=40,
        max_len=150,
        elements=[ReadElement(role="cDNA", region_type="cdna", start=0)],
    )


def _tagged(umi: str, *, offset: int = 0, trailing: str = "GGG", cdna: str = _CDNA) -> str:
    """One R1 sequence carrying the tag at `offset`, with `trailing` closing it."""
    return _READ_THROUGH[:offset] + _TAG + umi + trailing + cdna


def _quals(seq: str) -> str:
    """A quality string that differs at every position, so a trim that slips shows up as garbage."""
    return "".join(chr(33 + (i % 40)) for i in range(len(seq)))


def _fastq(path: Path, records: list[tuple[str, str, str, str]]) -> None:
    """Write `(header, sequence, plus-line, quality)` records verbatim.

    Not `conftest.write_fastq_gz`, which always writes a bare `+`: half of what the extractor's
    input gate is about is what a package wrote on that third line.
    """
    with gzip.open(path, "wt") as fh:
        for header, seq, plus, qual in records:
            fh.write(f"{header}\n{seq}\n{plus}\n{qual}\n")


def _write_pair(tmp: Path, reads: list[tuple[str, str]]) -> tuple[Path, Path]:
    """One cell's two FASTQs from `(r1, r2)` sequence pairs, with well-formed headers."""
    r1, r2 = tmp / "cell_R1.fastq.gz", tmp / "cell_R2.fastq.gz"
    _fastq(r1, [(f"@cell:{i}", s, "+", _quals(s)) for i, (s, _) in enumerate(reads)])
    _fastq(r2, [(f"@cell:{i}", s, "+", _quals(s)) for i, (_, s) in enumerate(reads)])
    return r1, r2


def _records(bam: Path) -> list[pysam.AlignedSegment]:
    """Every record of a uBAM. `check_sq=False` because an unaligned BAM has no `@SQ` lines."""
    with pysam.AlignmentFile(str(bam), "rb", check_sq=False) as fh:
        return list(fh)


def test_the_extraction_geometry_is_derived_from_the_element_model_and_not_written_down() -> None:
    """The 46 bp window is a consequence of the layout, not a number in the source.

    Anchor start <= 24 -- mechanistic, not fitted: no exact hit anywhere in 18,901 reads starts past
    offset 24, that bound being Tn5 mosaic-end read-through -- plus the 22 bp one match consumes.
    Every term comes out of the elements, so a chemistry with a longer tag gets a wider window
    without a line changing.
    """
    geometry = geometry_for_read(_smartseq3_r1())

    assert geometry.anchor == _TAG
    assert (geometry.anchor_start, geometry.umi_offset, geometry.umi_length) == (0, 11, 8)
    assert (geometry.trailing, geometry.trailing_offset, geometry.cdna_offset) == ("GGG", 19, 22)
    assert geometry.span == 22
    assert geometry.window == 46


def test_the_tagged_read_is_the_one_the_layout_says_carries_a_umi() -> None:
    """Which read is tagged is a fact the layout already states, so nobody gets to name it."""
    layout = ReadLayout(modality="rna", reads=[_plain_read("R2"), _smartseq3_r1()])

    read_id, geometry = tagged_read_geometry(layout)

    assert read_id == "R1"
    assert geometry.anchor == _TAG


def test_a_layout_with_no_umi_element_yields_no_extraction_geometry() -> None:
    """A bulk library has nothing to extract, and saying so beats cutting eight bases off R1."""
    layout = ReadLayout(modality="rna", reads=[_plain_read("R1"), _plain_read("R2")])

    with pytest.raises(UmiExtractError, match="0 of this layout's reads"):
        tagged_read_geometry(layout)


@pytest.mark.parametrize("offset", [0, 13, 15, 23, 24])
def test_a_tag_away_from_offset_zero_is_still_found(offset: int) -> None:
    """The search is unanchored, and 4.3% of the reference's exact hits depend on it.

    Of 8,266 exact hits measured against the reference matcher -- a `re.search`, so unanchored --
    354 are not at offset 0, clustering at 13, 15 and 23. Anchoring at the declared offset is the
    obvious reading of "derived from the element model" and silently loses every one of them. 24 is
    here because the bound is inclusive: it is the deepest offset the read-through can produce.
    """
    geometry = geometry_for_read(_smartseq3_r1())

    match = find_tag(_tagged("ACGTACGT", offset=offset), geometry)

    assert match is not None
    assert match.start == offset
    assert match.umi == "ACGTACGT"


def test_a_tag_deeper_than_the_read_through_bound_is_not_a_tag() -> None:
    """Past 24 the window closes, and that costs exact hits nothing.

    Capping the search there dropped 0 exact hits and 113 of 8,976 fuzzy ones (-1.26%), all of which
    are a purity gain: a tolerant anchor matches spurious 11-mers as deep as offset 133, at offsets
    a fixed-offset chemistry cannot produce.
    """
    geometry = geometry_for_read(_smartseq3_r1())

    assert find_tag(_tagged("ACGTACGT", offset=25), geometry) is None
    assert find_tag(_tagged("ACGTACGT", offset=57), geometry) is None


def test_the_trailing_motif_tolerates_one_substitution_and_not_two() -> None:
    """One, because at three the check is vacuous over a 3 bp motif.

    Three tolerated mismatches accept any three bases at all, so the motif stops confirming anything
    and the extractor manufactures a UMI out of every untagged read that happens to carry the tag's
    11-mer. One absorbs ordinary sequencing error and still says something.
    """
    geometry = geometry_for_read(_smartseq3_r1())

    one_off = find_tag(_tagged("ACGTACGT", trailing="GGA"), geometry)
    assert one_off is not None
    assert one_off.umi == "ACGTACGT"

    assert find_tag(_tagged("ACGTACGT", trailing="GAA"), geometry) is None


def test_a_read_that_is_all_prefix_stays_untagged_rather_than_becoming_an_empty_record() -> None:
    """A match with no cDNA left is not a usable read, and an empty BAM record is one an aligner
    refuses rather than skips -- so it falls through to the untagged path with its bases intact."""
    geometry = geometry_for_read(_smartseq3_r1())

    assert find_tag(_tagged("ACGTACGT", cdna=""), geometry) is None
    assert find_tag(_tagged("ACGTACGT", cdna="A"), geometry) is not None


def test_a_tagged_read_loses_exactly_the_structural_prefix_and_its_qualities_move_with_it(
    tmp_path: Path,
) -> None:
    """The trim is measured from where the anchor WAS found, not from the read's start.

    Trimming a constant 22 would leave 13 bases of read-through and mosaic end on the front of every
    offset read's cDNA, which aligns -- badly, and at exit 0.
    """
    geometry = geometry_for_read(_smartseq3_r1())
    seq = _tagged("ACGTACGT", offset=13)
    r1, r2 = tmp_path / "r1.fastq.gz", tmp_path / "r2.fastq.gz"
    _fastq(r1, [("@cell:0", seq, "+", _quals(seq))])
    _fastq(r2, [("@cell:0", _CDNA, "+", _quals(_CDNA))])

    stats = extract_umis(r1, r2, tmp_path / "cell.bam", geometry, sample="cell")
    read1, read2 = _records(tmp_path / "cell.bam")

    assert (stats.pairs, stats.tagged, stats.untagged) == (1, 1, 0)
    assert stats.offsets == {13: 1}
    assert read1.query_sequence == _CDNA
    assert pysam.qualities_to_qualitystring(read1.query_qualities) == _quals(seq)[13 + 22 :]
    # The mate is untouched: only the tagged read carries a structural prefix.
    assert read2.query_sequence == _CDNA
    # An unaligned pair, flagged the way `samtools import` flags one.
    assert (read1.flag, read2.flag) == (77, 141)
    assert read1.get_tag("UB") == read2.get_tag("UB") == "ACGTACGT"


def test_an_untagged_read_keeps_every_base_and_carries_no_umi(tmp_path: Path) -> None:
    """Internal reads are a third to two thirds of a real library and are counted, not dropped."""
    geometry = geometry_for_read(_smartseq3_r1())
    r1, r2 = _write_pair(tmp_path, [(_CDNA, _CDNA)])

    stats = extract_umis(r1, r2, tmp_path / "cell.bam", geometry, sample="cell")
    read1, read2 = _records(tmp_path / "cell.bam")

    assert (stats.tagged, stats.untagged) == (0, 1)
    assert read1.query_sequence == _CDNA
    assert not read1.has_tag("UB")
    assert not read2.has_tag("UB")


def test_a_pair_whose_names_disagree_still_extracts(tmp_path: Path) -> None:
    """R1 and R2 are paired by POSITION, so the input contract stops depending on who wrote it.

    This dissolves a hazard rather than guarding it: the reference took everything after the last
    underscore of the read name as the UMI with no format check, so a cell named `cell_42` yielded
    the UMI `42` -- silently, at exit 0. Nothing here reads a name for anything but a QNAME, and the
    mate's name is discarded with its FASTQ.
    """
    geometry = geometry_for_read(_smartseq3_r1())
    seq = _tagged("TTTTGGCC")
    r1, r2 = tmp_path / "r1.fastq.gz", tmp_path / "r2.fastq.gz"
    _fastq(r1, [("@cell_42/1", seq, "+", _quals(seq))])
    _fastq(r2, [("@SRR19884922.7 7 length=60", _CDNA, "+", _quals(_CDNA))])

    stats = extract_umis(r1, r2, tmp_path / "cell.bam", geometry, sample="cell_42")
    read1, read2 = _records(tmp_path / "cell.bam")

    assert stats.tagged == 1
    # The UMI comes out of the BASES. `42` is what everything-after-the-last-underscore gave.
    assert read1.get_tag("UB") == "TTTTGGCC"
    # One QNAME for both mates, taken from the tagged read, with the mate suffix left to the flag.
    assert read1.query_name == read2.query_name == "cell_42"


def test_a_plus_line_that_disagrees_with_its_header_is_refused(tmp_path: Path) -> None:
    """The gate is on the RECORD, because these packages repeat the whole ID on the `+` line.

    Rewriting only the `@` line trades one refusal for another -- a wrong first attempt already made
    once, while capturing the reference fixture -- and a reader that compares the two lines is what
    caught it. A half-renamed file is one somebody normalised wrong, not one to extract from.
    """
    geometry = geometry_for_read(_smartseq3_r1())
    seq = _tagged("ACGTACGT")
    r1, r2 = tmp_path / "r1.fastq.gz", tmp_path / "r2.fastq.gz"
    _fastq(r1, [("@cell:0", seq, "+cell:0/1", _quals(seq))])
    _fastq(r2, [("@cell:0", _CDNA, "+", _quals(_CDNA))])

    with pytest.raises(UmiExtractError, match="half-renamed"):
        extract_umis(r1, r2, tmp_path / "cell.bam", geometry, sample="cell")


def test_a_plus_line_repeating_the_whole_id_is_the_normal_case_and_is_accepted(
    tmp_path: Path,
) -> None:
    """The gate refuses DISAGREEMENT, not repetition -- a repeated ID is what these files look like."""
    geometry = geometry_for_read(_smartseq3_r1())
    seq = _tagged("ACGTACGT")
    r1, r2 = tmp_path / "r1.fastq.gz", tmp_path / "r2.fastq.gz"
    _fastq(r1, [("@cell:0", seq, "+cell:0", _quals(seq))])
    _fastq(r2, [("@cell:0", _CDNA, "+cell:0", _quals(_CDNA))])

    assert extract_umis(r1, r2, tmp_path / "cell.bam", geometry, sample="cell").tagged == 1


def test_two_fastqs_of_different_lengths_are_refused_rather_than_zipped_to_the_shorter(
    tmp_path: Path,
) -> None:
    """Positional pairing makes a length disagreement unpairable, and a cell quietly missing its
    tail counts low while saying nothing."""
    geometry = geometry_for_read(_smartseq3_r1())
    seq = _tagged("ACGTACGT")
    r1, r2 = tmp_path / "r1.fastq.gz", tmp_path / "r2.fastq.gz"
    _fastq(r1, [(f"@cell:{i}", seq, "+", _quals(seq)) for i in range(2)])
    _fastq(r2, [("@cell:0", _CDNA, "+", _quals(_CDNA))])

    with pytest.raises(UmiExtractError, match="paired by position"):
        extract_umis(r1, r2, tmp_path / "cell.bam", geometry, sample="cell")


def test_the_same_pair_extracts_to_a_byte_identical_bam(tmp_path: Path) -> None:
    """Deterministic throughout: no unseeded choice anywhere, and no clock in the header.

    The leftmost match wins rather than the best-scoring one, which is both what the reference does
    and the reason no tie-break has to be written down.
    """
    geometry = geometry_for_read(_smartseq3_r1())
    reads = [
        (_tagged("ACGTACGT", offset=0), _CDNA),
        (_tagged("TTTTGGCC", offset=15), _CDNA),
        (_CDNA, _CDNA),
    ]
    r1, r2 = _write_pair(tmp_path, reads)

    first = extract_umis(r1, r2, tmp_path / "one.bam", geometry, sample="cell")
    second = extract_umis(r1, r2, tmp_path / "two.bam", geometry, sample="cell")

    assert (tmp_path / "one.bam").read_bytes() == (tmp_path / "two.bam").read_bytes()
    assert first.to_dict() == second.to_dict()
    assert first.to_dict()["offsets"] == {"0": 1, "15": 1}


def test_a_truncated_input_is_refused_rather_than_extracted_up_to_the_cut(tmp_path: Path) -> None:
    """The bounded reader's own verdict, taken as an input-gate refusal instead of re-decided.

    This step reads every record -- it must, since its output is one BAM record per input record --
    so what the read-budget rule still binds here is its real target: do not write a second loop
    over a FASTQ. Truncation detection comes free with not writing one.
    """
    geometry = geometry_for_read(_smartseq3_r1())
    r1, r2 = _write_pair(tmp_path, [(_tagged("ACGTACGT"), _CDNA)])
    whole = r1.read_bytes()
    r1.write_bytes(whole[: len(whole) - 12])

    with pytest.raises(UmiExtractError, match="ends mid-record|not readable gzip"):
        extract_umis(r1, r2, tmp_path / "cell.bam", geometry, sample="cell")


@pytest.mark.xdist_group("src-trees")
def test_the_extractor_shells_out_to_nothing_so_its_rule_needs_no_image(
    src_trees: SrcTrees,
) -> None:
    """No container: the counter is not an aligner, and this half does not even call a binary.

    The CRAM converter and the fragments finalizer both shell out to a runtime image's htslib, so
    "a workflow module needs a container" is a live default here rather than an absent thought. This
    one writes a BAM through a library, which is the h5ad packager's line exactly. The contrast is
    asserted alongside so the check cannot rot into one that passes because it looks at nothing.
    """
    import ast

    def imported_by(relative: str) -> set[str]:
        tree = src_trees[_src_root() / relative]
        top: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                top |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                top.add(node.module.split(".")[0])
        return top

    assert "subprocess" not in imported_by("workflows/umite/extract.py")
    assert "subprocess" in imported_by("workflows/cram.py")


def _revcomp(seq: str) -> str:
    """The reverse complement, so a synthetic mate maps in the orientation a pair maps in."""
    return seq.translate(str.maketrans("ACGT", "TGCA"))[::-1]


@pytest.mark.external
def test_the_aligner_carries_the_umi_tag_through_to_its_own_output(tmp_path: Path) -> None:
    """The uBAM route, run end to end: `UB:Z:` goes in as input and comes out on the alignment.

    This is the claim the output format rests on, and the reason it is a BAM rather than a FASTQ.
    STAR **refuses** `UB` in `--outSAMattributes` outside its single-cell mode (`FATAL INPUT ERROR:
    ... not allowed for --soloType None`), so the tag cannot be asked for on the way out; it has to
    arrive on the way in and survive. Measured on real data while the reference package was still
    installed (2026-08-04): 452 of 716 aligned records came out carrying their input `UB` — and
    their `RG` — with `UB` named nowhere in the output attribute list.

    Re-run here on this synthetic pair under STAR 2.7.11b (the pinned `align-rna` image, 2026-08-04):
    46 of 46 records aligned, the 40 from tagged pairs all carried `UB:Z:` and all 46 carried
    `RG:Z:`. `--outSAMattributes ... UB` on the same input dies with the FATAL INPUT ERROR above, so
    the input route is not merely the better one, it is the only one.

    `--readFilesSAMattrKeep All` is **STAR's own default**, measured: dropping it from this command
    changed nothing (40 of 46 again). It is passed anyway, and that is not cargo — it pins a default
    the whole output format depends on, in the one place a reader can see the dependency.

    Needs STAR and samtools, which seqforge does not own and this suite never installs, so it skips
    everywhere but a machine that has them.
    """
    import random

    star, samtools = shutil.which("STAR"), shutil.which("samtools")
    if star is None or samtools is None:
        pytest.skip("needs STAR and samtools on PATH")

    rng = random.Random(0)  # noqa: S311 — a synthetic contig, not a security decision
    contig = "".join(rng.choice("ACGT") for _ in range(20_000))
    (tmp_path / "genome.fa").write_text(f">chr1\n{contig}\n")
    index = tmp_path / "index"
    subprocess.run(
        [star, "--runMode", "genomeGenerate", "--genomeDir", str(index),
         "--genomeFastaFiles", str(tmp_path / "genome.fa"), "--genomeSAindexNbases", "6",
         "--outFileNamePrefix", str(tmp_path / "gg_")],
        check=True, capture_output=True,
    )  # fmt: skip

    geometry = geometry_for_read(_smartseq3_r1())
    cdna, mate = contig[1000:1060], _revcomp(contig[1200:1260])
    r1, r2 = _write_pair(tmp_path, [(_tagged("ACGTACGT", offset=13, cdna=cdna), mate)])
    ubam = tmp_path / "cell.bam"
    assert extract_umis(r1, r2, ubam, geometry, sample="cell").tagged == 1

    subprocess.run(
        [star, "--genomeDir", str(index), "--readFilesIn", str(ubam),
         "--readFilesType", "SAM", "PE", "--readFilesCommand", samtools, "view",
         "--readFilesSAMattrKeep", "All", "--outSAMtype", "BAM", "Unsorted",
         "--outFileNamePrefix", str(tmp_path / "star_")],
        check=True, capture_output=True,
    )  # fmt: skip

    aligned = _records(tmp_path / "star_Aligned.out.bam")
    assert aligned, "STAR aligned nothing, so the tag question was never asked"
    assert all(record.get_tag("UB") == "ACGTACGT" for record in aligned)
