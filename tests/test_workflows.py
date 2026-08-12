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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Any, get_args

import anndata as ad
import pysam
import pytest
import yaml
from scipy.sparse import csr_matrix

from conftest import (
    NO_STAR_ALIGNMENT_ON_MACOS,
    DryRun,
    SrcTrees,
    _build,
    _processing,
    _rule_blocks,
    _src_root,
    count_matrix,
    write_fastq_gz,
)
from seqforge import kb
from seqforge.compose import compose, core
from seqforge.models.dataset import ReadDef, ReadElement, ReadLayout
from seqforge.models.processing import RuntimeEnv, SoloFeature
from seqforge.workflows import (
    PLATE_H5AD,
    WORKFLOW_VERSION,
    argv_keys_read_by,
    get_module,
    keys_read_by,
    list_modules,
)
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
from seqforge.workflows.umite.count import (
    FATES,
    LAYERS,
    N_FRAGMENTS,
    PRIMARY_MATRIX,
    UmiCountError,
    correct_umis,
    count_bam,
    count_plate,
    deduplicate,
    fate_metrics,
    parse_cells,
    read_annotation,
    write_umi_counts,
)
from seqforge.workflows.umite.extract import (
    TagGeometry,
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

    The narrowing itself moved to ``conftest.count_matrix`` when the plate gate needed the same one;
    this is the name the rest of this file already calls.
    """
    return count_matrix(adata, layer)


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

    assert set(list_modules()) == {"map/starsolo", "map/star", "map/chromap", "map/star-umi"}
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

    # The fan-in declaration, by NAME in both directions. Exactly one shipped module produces a
    # deliverable the sample axis does not reach, and the other three are untouched by its arrival —
    # which is the whole of why the field defaults to absent. A set comparison rather than a count:
    # "some module aggregates" was never the claim.
    assert {n for n in list_modules() if get_module(n).fan_in_artifact is not None} == {
        "map/star-umi"
    }
    assert get_module("map/star-umi").fan_in_artifact == PLATE_H5AD

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


#: Registered modules **no shipped spec names yet**, and why each is here. **Empty, and it emptied
#: the way it was meant to**: `map/star-umi` sat here for exactly as long as its chemistry took to
#: land, because a KB entry cannot precede its module — the confusability biconditional computes
#: `backend_identical` off the resolved `backend.module`, and against a placeholder CI stamps
#: `processing_equivalent`, which is wrong. The module shipped first and `smartseq3` followed, so the
#: composed-pipeline gate below now covers every registered module with a real chemistry.
#:
#: Kept as a named, empty set rather than deleted, in the shape `MODULES_WITHOUT_STATS` established:
#: a module no spec reaches is untested by that gate, and the only safe version of that is one which
#: says which module and until when. The next module to ship ahead of its entry writes its name here
#: and takes `test_the_plate_module_plans_a_whole_run_from_a_hand_written_config`'s route in the
#: meantime — a `.smk` proved to plan from a hand-written config, needing no chemistry at all.
MODULES_NO_SPEC_REACHES_YET: frozenset[str] = frozenset()


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
    a spec targets it, and a module no spec reaches is refused here unless it is NAMED in
    :data:`MODULES_NO_SPEC_REACHES_YET` with the reason, rather than going quietly untested.

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
    if module in MODULES_NO_SPEC_REACHES_YET:
        assert not techs, (
            f"{module} is named as reached by no spec, but {techs} reach it — drop it from "
            f"MODULES_NO_SPEC_REACHES_YET so the composed-pipeline gate covers it"
        )
        pytest.skip(
            f"{module} has no chemistry yet; its .smk is planned from a hand-written config"
        )
    assert techs, f"{module} is registered but no spec reaches it"
    manifest, reg = _build(tmp_path, techs[0])
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    # PASS, not skip. This used to read `in {"pass", "skip"}` and so forbade only the one value that
    # could not occur: `snakemake` was in no dependency table, `have("snakemake")` was False, and the
    # gate returned "skip" every time. A skip is green, so the gate was decorative for the life of the
    # repo. If it ever goes missing again, that is a broken environment and this says so.
    assert result.gate["wiring"].status == "pass", result.gate["wiring"].reason
    run_dir = (tmp_path / result.snakefile_path).parent
    strays = [p for p in run_dir.rglob("*") if p.suffix == ".gz" and p.stat().st_size == 0]
    assert not strays, f"the gate left zero-byte stand-ins in the run dir: {strays}"


#: One plate's worth of hand-written config: the three cells, the geometry compose would derive, and
#: the two roles it would place by ROLE. Enough to plan the module and nothing more.
_PLATE_GEOMETRY = "R1:ATTGCGCAATG@0:umi@11+8:GGG@19:cdna@22"


def _plate_run_dir(
    directory: Path,
    samples: Sequence[str],
    *,
    mate: bool | None = True,
    read_through: str | None = None,
) -> dict[str, object]:
    """Write a runnable plate pipeline directory by hand, and return the config it carries.

    Hand-written rather than composed, and the point is that it does not need a chemistry: a ``.smk``
    is configuration in, rules out, so its whole contract is the key set :func:`keys_read_by` scans
    off it. The caller asserts that this config covers exactly that set, which is what makes the plan
    below a proof about the module rather than about a fixture.

    ``mate=False`` writes the OTHER sequencing configuration Smart-seq3's Methods publish — the
    tagged read and nothing else (ADR-0035). One flag rather than a second copy of this function,
    because the two directories differ in exactly the one fact the module branches on: whether the
    layout places a ``cdna`` role, and therefore whether units.tsv carries an R2 row at all. Two
    copies would be free to drift on the eight keys that are NOT the subject.

    ``mate=None`` writes the INCOHERENT third state, which is neither configuration and is the only
    one that can pull the module's two branches apart: the layout *declares* a ``cdna`` role while
    units.tsv stages no file for it. It exists because the module used to render each branch from a
    different fact, and this directory is what told the two apart.

    ``read_through`` writes the adapter compose derives for a chemistry that declares one. Absent by
    default, because that is the shape every other assertion in this file is about and because an
    absent key is what the module's ``.get`` exists to serve.
    """
    module = get_module("map/star-umi")
    config: dict[str, object] = {
        "container": "docker://example/align-rna",
        "genome": {"assembly": "sacCer3", "annotation": "ensembl_R64-1-1"},
        "mem_mb": 8 * 1024,
        "outdir": "results",
        "read_files_in": {"umi_cdna": "R1", "cdna": "R2"}
        if mate is not False
        else {"umi_cdna": "R1"},
        "threads": 4,
        "umi": {"read_structure": _PLATE_GEOMETRY}
        | ({"read_through": read_through} if read_through else {}),
        "units_tsv": "units.tsv",
    }
    rows = ["\t".join(("sample_id", "run", "lane", "read_id", "path"))]
    for sample in samples:
        for read_id in ("R1", "R2") if mate is True else ("R1",):
            path = f"fastq/{sample}_{read_id}.fastq.gz"
            (directory / "fastq").mkdir(parents=True, exist_ok=True)
            (directory / path).write_bytes(b"")
            rows.append("\t".join((sample, sample, "", read_id, path)))
    (directory / "units.tsv").write_text("\n".join(rows) + "\n")
    (directory / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=True))
    shutil.copy2(module.snakefile, directory / module.snakefile.name)
    (directory / "Snakefile").write_text(core.render_wrapper(module.name, module.snakefile.name))
    return config


def test_the_plate_module_plans_a_whole_run_from_a_hand_written_config(
    tmp_path: Path, dry_run: DryRun
) -> None:
    """The plate `.smk` plans a real DAG on its own — per cell in, one object out.

    Written when no chemistry named this module, and `smartseq3` names it now — so what keeps it is
    no longer "nothing else reaches these rules". It is that **nothing COMPOSED reaches them**: the
    config above is hand-written, so the key set asserted against `required_config` is a claim about
    the MODULE. Compose the same plate and the composer supplies whatever the composer supplies, and
    the assertion turns into the composer agreeing with itself. `composed_plate` covers the other
    half — the shipped entry compiling into this module — and the two are deliberately not merged.

    Two claims, and the second is what makes the first mean something. The plan must reach every
    rule — the shared load, the per-cell chain, and the fan-in — and the hand-written config above
    must be EXACTLY the key set scanned off the module source. Configuration nobody reads is how a
    module comes to depend on a key the composer does not owe it, which surfaces as a `KeyError` on a
    compute node long after compose exited 0.
    """
    module = get_module("map/star-umi")
    config = _plate_run_dir(tmp_path, ["cell_a", "cell_b", "cell_c"])

    dotted = {
        f"{key}.{sub}" if isinstance(value, dict) else key
        for key, value in config.items()
        for sub in (value if isinstance(value, dict) else [None])
    }
    # `read_files_in.cdna` is the one key the config carries and the scan does not: the module reads
    # it with `.get`, so a single-end plate is not obliged to emit a mate it does not have.
    assert set(module.required_config) == dotted - {"read_files_in.cdna"}

    plan = dry_run(tmp_path)

    for rule in ("load_genome", "umi_extract", "star_umi_map", "umi_to_cram", "umi_count"):
        assert rule in plan, f"the plan never reaches `{rule}`:\n{plan}"
    # The fan-in is ONE job over three cells, while the per-cell chain is one job each. That ratio is
    # the module's whole shape, and a per-cell counter followed by a merge would read as three here.
    assert re.search(r"^umi_extract\s+3\s*$", plan, re.M), plan
    assert re.search(r"^star_umi_map\s+3\s*$", plan, re.M), plan
    assert re.search(r"^umi_count\s+1\s*$", plan, re.M), plan
    assert re.search(r"^load_genome\s+1\s*$", plan, re.M), plan
    # The deliverables `rule all` demands, by name: one object for the plate, one CRAM per cell.
    assert f"results/{PLATE_H5AD}" in plan
    assert all(f"results/{s}/{s}.cram" in plan for s in ("cell_a", "cell_b", "cell_c"))
    # The shared-memory contract, rendered rather than merely written: the load rule marks any stale
    # segment for destruction before loading, and every mapping job attaches instead of loading.
    assert "--genomeLoad Remove" in plan and "--genomeLoad LoadAndExit" in plan
    assert "--genomeLoad LoadAndKeep" in plan
    # ...and the geometry the extractor is handed is the ONE derived value, not six numbers.
    assert f"--geometry {_PLATE_GEOMETRY}" in plan
    # The paired half of what this layout decides, and its mirror is the test below. The extraction
    # renders NO file at all now (ADR-0036) — the table and the cell, one argument each — so the
    # half that still varies here is the aligner's, and the pair that says "paired" here has to be
    # the pair that says "single" there.
    assert re.search(r"--units \S*units\.tsv --sample \S+", plan), plan
    assert not re.search(r"--r1\b|--r2\b", plan), plan
    assert "--readFilesType SAM PE" in plan


def test_the_three_prime_clip_takes_its_arity_from_the_same_fact_the_read_type_does(
    tmp_path: Path, dry_run: DryRun
) -> None:
    """Per mate, and STAR counts. A clip rendered for the run rather than for the cell is a FATAL.

    Measured against STAR 2.7.11b, the pinned binary, at parameter init: `--clip3pAdapterSeq` must
    carry one value PER MATE, and `--clip3pAdapterMMp` must match its arity or STAR refuses the run
    outright. This module's mate count is per SAMPLE — one plate legally mixes cells sequenced both
    ways — so two values rendered once for the whole run would FATAL on every single-end cell, and
    one value would FATAL on every paired one. Hence the arity comes off `mate_count`, the same
    single fact `--readFilesType` renders, rather than off a second reading of the layout.

    `--clip3pAdapterMMp 0.1` is STAR's own default restated at the arity the paired form demands. It
    varies with nothing and so is a module literal, not a chemistry's to choose.

    The ordering hazard, asserted rather than assumed: the UMI lives in the first 19 bp of the tagged
    read, so anything clipping that read before extraction destroys it. Clipping inside the ALIGNER
    puts it after extraction by construction — the two are different rules and the uBAM is the edge
    between them — and the one-for-one count below is what says so without reading the source.
    """
    seq = "CTGTCTCTTATACACATCT"
    paired = tmp_path / "paired"
    paired.mkdir()
    _plate_run_dir(paired, ["cell_a", "cell_b"], read_through=seq)
    plan = dry_run(paired)
    assert f"--clip3pAdapterSeq {seq} {seq} --clip3pAdapterMMp 0.1 0.1" in plan, plan
    assert "--readFilesType SAM PE" in plan
    # One clip per aligner job and not one per cell-touching job: the extractor runs first, over the
    # untouched read, and never sees the flag.
    assert plan.count("--clip3pAdapterSeq") == plan.count("--readFilesType") == 2, plan

    single = tmp_path / "single"
    single.mkdir()
    _plate_run_dir(single, ["cell_a"], mate=False, read_through=seq)
    plan_se = dry_run(single)
    assert f"--clip3pAdapterSeq {seq} --clip3pAdapterMMp 0.1" in plan_se, plan_se
    assert "--readFilesType SAM SE" in plan_se
    assert f"{seq} {seq}" not in plan_se, "two values on a single-end cell is STAR exit 101"

    # ...and a chemistry that declares no adapter renders NO flag, rather than an empty one STAR
    # would match against every read. The module reads the key with `.get`, so it is also not a key
    # every plate is obliged to emit — a subscript here would oblige each one to name an adapter.
    bare = tmp_path / "bare"
    bare.mkdir()
    _plate_run_dir(bare, ["cell_a"])
    assert "clip3p" not in dry_run(bare)
    assert "umi.read_through" not in get_module("map/star-umi").required_config


def test_the_extractors_mate_and_the_aligners_read_type_come_from_one_fact(
    tmp_path: Path, dry_run: DryRun
) -> None:
    """A `cdna` role declared but staging nothing extracts unpaired — so it must render `SAM SE` too.

    The one state that can pull this module's two branches apart, and it is here because they were
    briefly rendered from two different facts: `--r2` from the list snakemake staged, and
    `--readFilesType` from whether the layout named a `cdna` role. Those agree on both sequencing
    configurations the protocol publishes and disagree on exactly this one — a role declared for the
    layout with no file behind it for this cell — where the extractor writes an unpaired uBAM while
    the aligner is still told its input is paired.

    Under ADR-0036 the two branches converge harder rather than merely agreeing: the extractor reads
    its mate off units.tsv, and `read_files_type` derives from the same rows through the same
    ordering helper, so a role with no row behind it is single-ended to BOTH readers. The assertion
    stays because "they cannot disagree" is a property to hold, not one to assume — and because the
    reading that matters is still the uBAM's shape against the flag that describes it.

    **What made it worth a test is that it is SILENT.** `snakemake -n` planned that combination at
    returncode 0, so compose's wiring gate passed it and the whole chain reported success; the
    failure arrived later, in the user's hands, as STAR exit 104 — `FATAL ERROR in input BAM file:
    the consecutive lines in paired-end BAM have different read IDs` (measured 2026-08-05 against the
    `align-rna` image). That is precisely the shape ADR-0035 was written to delete, reappearing
    inside the change that deleted it, which is why the guard is a test and not a comment.

    Nothing composes this directory today — compose writes `read_files_in` and units.tsv together, so
    the role and the rows cannot disagree. That is a guard by ABSENCE, and this tree's standing
    judgement on those is that they are worth one test each.
    """
    _plate_run_dir(tmp_path, ["cell_a"], mate=None)
    plan = dry_run(tmp_path)

    assert not re.search(r"--r1\b|--r2\b", plan), (
        f"no file belongs on this command line at all (ADR-0036):\n{plan}"
    )
    # It PLANS, which is the assertion the extractor half turned into: the module stages no mate and
    # units.tsv offers none, so `mate_fastqs`' agreement check passes rather than refusing.
    assert "umi_extract" in plan, plan
    assert "--readFilesType SAM SE" in plan, (
        "the extractor was handed one FASTQ and wrote an unpaired uBAM, so the aligner must be told "
        f"`SAM SE`; `SAM PE` over those records is STAR exit 104, not a wrong number:\n{plan}"
    )
    assert "SAM PE" not in plan, plan


def test_the_plate_module_plans_a_single_end_run_and_hands_the_extractor_no_mate(
    tmp_path: Path, dry_run: DryRun
) -> None:
    """The same module, the same rules, over a layout that places only the tagged read.

    This is the DAG the mate-role helper used to REFUSE to build. It raised rather than rendering
    `--r2` with nothing after it, and a raise inside an input function lands as an
    `InputFunctionException` at DAG construction — which the compose wiring gate turns into exit 3
    with snakemake's reason discarded, and, on a machine with no snakemake at all, into a green
    compose whose Snakefile dies wherever the user submits it. So the assertion has to be a PLAN and
    not a rendered string read off the source: only building the DAG proves the refusal is gone.

    Two readers carry the layout, and both come from the single fact that `read_files_in` places no
    `cdna` role (ADR-0035), so units.tsv carries no row for one. The extractor finds no mate role in
    the table and writes one unpaired record per fragment; the aligner is handed `SAM SE`, because a
    `SAM PE` over a uBAM of unpaired records is a crash — the loud kind, hours in, after the index
    has loaded and the plate has queued.

    No `external` marker by hand: taking `dry_run` IS what marks this test, since the marker is
    derived from the fixtures a test requests rather than written beside it.
    """
    module = get_module("map/star-umi")
    config = _plate_run_dir(tmp_path, ["cell_a", "cell_b"], mate=False)

    dotted = {
        f"{key}.{sub}" if isinstance(value, dict) else key
        for key, value in config.items()
        for sub in (value if isinstance(value, dict) else [None])
    }
    # EXACT, with nothing subtracted, and that is the other half of the paired test's subtraction:
    # this layout emits no `read_files_in.cdna` and the module demands none, so a subscript smuggled
    # back into the mate helper would fail here rather than on a compute node.
    assert set(module.required_config) == dotted

    plan = dry_run(tmp_path)

    for rule in ("load_genome", "umi_extract", "star_umi_map", "umi_to_cram", "umi_count"):
        assert rule in plan, f"the plan never reaches `{rule}`:\n{plan}"
    # The shape does not follow the layout: still one extraction a cell and still ONE fan-in.
    assert re.search(r"^umi_extract\s+2\s*$", plan, re.M), plan
    assert re.search(r"^umi_count\s+1\s*$", plan, re.M), plan
    # No file reaches the command line at all (ADR-0036): the table and the cell do, and the verb
    # resolves the rest. A single-end cell states its shape by having no R2 ROW, which is a fact
    # this plan cannot render wrongly because it renders nothing about it.
    assert re.search(r"--units \S*units\.tsv --sample cell_a\b", plan), plan
    assert not re.search(r"--r1\b|--r2\b", plan), plan
    assert f"--geometry {_PLATE_GEOMETRY}" in plan
    # ...and the aligner reads the uBAM as what the extractor actually wrote.
    assert "--readFilesType SAM SE" in plan
    assert "SAM PE" not in plan, plan


def test_a_mate_the_module_will_not_stage_and_the_table_still_offers_is_refused_at_dag_time(
    tmp_path: Path, dry_run: DryRun
) -> None:
    """The mirror of the test above, and the state ADR-0036 newly makes reachable.

    The extractor stopped being handed its mate and started resolving it from units.tsv, where a
    role is a COLUMN and its elements are not. This module keeps reading `read_files_in["cdna"]`,
    which is compose's role-checked answer — the non-tagged read carrying a cDNA or gDNA element. A
    layout whose second non-index read is neither leaves the two saying different things: nothing is
    staged and the aligner is told `SAM SE`, while the verb finds one non-tagged role and writes an
    interleaved PAIRED uBAM. STAR reads those one record at a time and counts every fragment twice,
    **at exit 0** — a wrong matrix rather than a crash, which is the half nothing downstream notices.

    No shipped chemistry reaches it (`smartseq3` has two reads and the second is plain cDNA), so
    this is guarded by a CHECK rather than by absence — which is the shape #327 was filed about. The
    refusal lands inside an input function, so it is an `InputFunctionException` at DAG construction:
    exactly where compose's wiring gate is looking, and before anything is submitted.
    """
    _plate_run_dir(tmp_path, ["cell_a"], mate=False)  # the layout places NO `cdna` role
    rows = (tmp_path / "units.tsv").read_text().rstrip("\n").splitlines()
    path = "fastq/cell_a_R2.fastq.gz"
    (tmp_path / path).write_bytes(b"")  # ... and a second role the table carries anyway
    (tmp_path / "units.tsv").write_text(
        "\n".join([*rows, "\t".join(("cell_a", "cell_a", "", "R2", path))]) + "\n"
    )

    refusal = dry_run(tmp_path, refused=True)

    assert "InputFunctionException" in refusal, refusal
    assert "must be the same answer" in refusal, refusal


def test_the_plate_modules_own_rendered_extraction_runs_over_a_cell_that_spans_two_runs(
    tmp_path: Path, dry_run: DryRun
) -> None:
    """The module renders it, and it RUNS — the layer that broke, at the layer that broke on it.

    `snakemake -n -p` formats every `shell:` block while planning and never runs one, so a rendered
    command's arity, quoting and ordering are unguarded by construction: `--r1 {input.tagged}` over
    a cell topped up across two runs planned clean for as long as it existed and died at job
    execution, `exit 2, Got unexpected extra argument(s)`, on a compute node past handover
    (ADR-0036). Nothing between the CLI tests and the STAR-gated end-to-end covered it,
    because the composed plate those run over is 1:1 — the one deposit shape that never had the bug.

    So this takes the module's own rendered string and executes it, against a cell whose files come
    from two runs. It needs no aligner: the extractor shells out to nothing at all, which is why the
    gap could be closed cheaply and was worth closing rather than deferring to the external suite.
    The uBAM must then carry every fragment of both files, each tagged read interleaved with the
    mate **from its own run** — which is readable off the bases, since the two runs' cDNA differ.
    """
    import pysam

    tag, marks = "ATTGCGCAATG", {"runa": "AAA", "runb": "CCC"}
    counts = {"runa": 3, "runb": 2}
    _plate_run_dir(tmp_path, ["cell_a"], mate=True)
    rows = ["\t".join(("sample_id", "run", "lane", "read_id", "path"))]
    for run, n in counts.items():
        body = "GATCACAGGTCTATCACCCTATTAACCACTCACGGGAGCTCTCCATGCATT" + marks[run]
        for read_id, seqs in (("R1", [tag + "ACGTACGT" + "GGG" + body] * n), ("R2", [body] * n)):
            path = f"fastq/cell_a_{run}_{read_id}.fastq.gz"
            write_fastq_gz(tmp_path / path, seqs, prefix=f"{run}:cell_a")
            rows.append("\t".join(("cell_a", run, "", read_id, path)))
    (tmp_path / "units.tsv").write_text("\n".join(rows) + "\n")

    plan = dry_run(tmp_path)
    command = re.search(r"^\s+seqforge io umi-extract .*?(?=\n\n)", plan, re.M | re.S)
    assert command, f"the plan renders no extraction:\n{plan}"
    done = subprocess.run(  # noqa: S602 - a `shell:` block is what is under test
        command.group(0), shell=True, cwd=tmp_path, capture_output=True, text=True,
        executable="/bin/bash",
    )  # fmt: skip
    assert done.returncode == 0, f"{command.group(0)}\n{done.stdout}\n{done.stderr}"

    with pysam.AlignmentFile(str(tmp_path / "results/cell_a/cell_a.unaligned.bam"), "rb",
                             check_sq=False) as ubam:  # fmt: skip
        records = list(ubam.fetch(until_eof=True))
    assert len(records) == 2 * sum(counts.values())  # every fragment of BOTH runs, interleaved
    assert [str(r.query_sequence)[-3:] for r in records] == (
        [marks["runa"]] * 2 * counts["runa"] + [marks["runb"]] * 2 * counts["runb"]
    ), "a tagged read reached the uBAM beside a mate from the other run"


def test_the_plate_module_marks_a_stale_segment_before_it_loads_and_frees_it_in_one_place() -> None:
    """The two halves the composed-plate plan cannot see.

    That both handlers call `release_genome_segment()` and that the helper carries
    `--genomeLoad Remove ... || true` is read off the RENDERED command and the EMITTED module by
    `test_a_composed_plate_plans_every_rule_and_resolves_every_cells_wildcard`. What no rendering
    shows is the ORDER — marking a stale segment after the load is a load that inherits it — nor
    that the command lives in the helper alone, where a second copy is a second chance to fix one.
    """
    source = get_module("map/star-umi").snakefile.read_text()
    load = _rule_blocks(get_module("map/star-umi").snakefile)["load_genome"]

    assert load.index("--genomeLoad Remove") < load.index("--genomeLoad LoadAndExit"), (
        "the stale segment must be marked for destruction BEFORE the load, or the load inherits it"
    )
    after = source[source.index("\nonsuccess:") :]
    assert "--genomeLoad Remove" not in after, "the command belongs to the helper, not to a handler"


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

    Everything the pipeline executes must come from the hand-written `.smk` modules; the moment the
    composer emits a `rule`, that guarantee is gone and nobody finds out from a comment. Asserted
    against the template, which is the thing a future edit would change, and only the ABSENCE — that
    the wrapper parameterises by `configfile:` and composes by reference rather than by `include:`
    is proved by the plate module planning a real DAG off this same template.
    """
    wrapper = core._WRAPPER
    assert not _RULE_DEF.search(wrapper), f"the composer emits rule source:\n{wrapper}"


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
def test_a_shipped_module_declares_rules_and_derives_its_own_config_contract(
    module_name: str,
) -> None:
    """The other half of emit-data-never-code: the rules that DO exist are checked-in source.

    A module whose rules were generated would defeat the wrapper check by moving the generation one
    step earlier, so each module has to be a real file under version control that really declares
    rules — read as SHAPE, since a header claiming to be hand-written is one a generator can write.
    """
    module = get_module(module_name)
    text = module.snakefile.read_text()
    assert _RULE_DEF.search(text), f"{module_name} defines no rules — is it really a module?"

    # `required_config` is COMPUTED from the module source, so neither direction can drift (folded
    # from test_required_config_is_exactly_what_the_module_reads). It once under-declared the four
    # soloCB/UMI keys `starsolo.smk` dereferences and over-declared `primary_feature`/`env` that no
    # rule reads. This identity reads as tautological but is not: it pins that `required_config` never
    # goes back to a hand-typed literal; test_the_required_config_scanner_can_catch_an_undeclared_key
    # is what proves the derivation itself is not vacuous.
    #
    # Two sources for a module whose argv renderer moved into Python — the reads moved, the rule that
    # they are DERIVED and never declared did not. Asserting against the snakefile alone is what this
    # line used to say, and it would now pass only by the module keeping its command line in a file
    # that cannot be unit-tested.
    derived = set(keys_read_by(module.snakefile))
    if module.argv_source is not None:
        derived |= argv_keys_read_by(module.argv_source)
    assert set(module.required_config) == derived
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
    # The fourth name. `param_block` intersects against a LITERAL set and raises unless exactly one
    # survives, so a module reading a block that literal does not name does not fall through to bulk
    # — it kills compose outright. That is the design working, and it is why the literal has to gain
    # a name with the module rather than after it.
    assert MODULES["map/star-umi"].param_block == "umi"

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
    # 14 since `clipAdapterType` and its five-prime override followed `soloCBmatchWLtype` out of
    # starsolo.smk and into the KB — the same move for the same reason, one release later.
    assert len(parse_keys_for("map/starsolo")) == 14
    # A bulk pipeline declares no parse params — empty, not degenerate (no barcode/UMI/whitelist).
    assert parse_keys_for("map/star") == frozenset()
    # The plate pipeline's is empty for the opposite reason: it needs six numbers and every one of
    # them is already in the element coordinates, so all six are DERIVED into one key rather than
    # declared. Empty here is what makes "a backend declaring a parse key is refused" true of it.
    assert parse_keys_for("map/star-umi") == frozenset()
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
    # A hyphenated id goes through the same rsplit and nothing else. A lookup for the one module
    # whose tail is not a bare binary name would regress to exactly the mirror that was deleted, so
    # the id has to be a true statement on its own — and it is, because STAR is what aligns here.
    assert MODULES["map/star-umi"].aligner == "star-umi"


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

    Which `resources:` entry carries the cap is the half `test_the_module_never_computes_a_star
    _memory_cap_from_the_config` cannot sweep for: it reads the flag's argument, and a binding of the
    identical text under `params:` would satisfy it and still freeze on attempt 1.

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


def test_the_plate_module_turns_the_recipes_one_figure_into_two_requests() -> None:
    """One recipe number in, a per-cell request and a fan-in request out — and they differ.

    The recipe says exactly one thing because `resources.mem_gb` is INTENT, and per-rule budgets in a
    recipe would make every recipe carry every module's rule names. So the map lives in the module,
    which is the only artifact that knows its own rule graph, and the claim here is that it IS a map:
    two rule classes that scale differently must not come out of it as one number.

    The per-cell request is the whole figure because a mapping job is dominated by the genome index,
    which is per process and independent of read count — 27.7 GB peak against a 25 GB index whether
    the well holds 901 reads or 3.1M. The fan-in loads no index at all, so it takes a share.
    """
    from seqforge.workflows.memory import PLATE_RETRIES, fan_in_mem_mb, per_cell_mem_mb

    assert per_cell_mem_mb(_DEFAULT_MEM_MB, 1) == _DEFAULT_MEM_MB
    assert fan_in_mem_mb(_DEFAULT_MEM_MB, 1) < per_cell_mem_mb(_DEFAULT_MEM_MB, 1)

    # Escalation per rule class, INDEPENDENTLY: each is linear in its own attempt over its own base,
    # so a retried counter never asks for a mapping job's headroom and vice versa. Attempt 1 is the
    # unescalated request for both, which is what keeps the common case unchanged.
    for attempt in range(1, PLATE_RETRIES + 2):
        assert per_cell_mem_mb(_DEFAULT_MEM_MB, attempt) == _DEFAULT_MEM_MB * attempt
        assert (
            fan_in_mem_mb(_DEFAULT_MEM_MB, attempt) == fan_in_mem_mb(_DEFAULT_MEM_MB, 1) * attempt
        )

    # A recipe smaller than the fan-in floor may not be turned into a request BIGGER than the recipe:
    # a job asking the scheduler for more than the pipeline was budgeted is a job that never starts.
    tiny = 2 * 1024
    assert fan_in_mem_mb(tiny, 1) == tiny


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

    Both halves are asserted by NAME rather than by size: "some module is silent" was never the
    claim, and a list that shrinks must shrink because an adapter landed rather than because a name
    was quietly dropped from the guard. Why `MODULES_WITHOUT_STATS` survives while empty is argued
    where it is declared.
    """
    from seqforge.workflows import stats as stats_registry

    stats_registry._check_registry()
    assert set(modules_with_stats()) | MODULES_WITHOUT_STATS == set(list_modules())
    # And `MODULES_WITHOUT_STATS` is the OTHER half: a module may only be silent by saying so.
    assert not (set(modules_with_stats()) & MODULES_WITHOUT_STATS)
    assert set(modules_with_stats()) == {
        "map/starsolo",
        "map/chromap",
        "map/star",
        "map/star-umi",
    }
    assert MODULES_WITHOUT_STATS == frozenset()


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
    assert set(modules_with_cross_checks()) | MODULES_WITHOUT_CROSS_CHECKS == set(
        modules_with_stats()
    )
    assert not (set(modules_with_cross_checks()) & MODULES_WITHOUT_CROSS_CHECKS)
    assert set(modules_with_cross_checks()) == {"map/starsolo"}
    assert MODULES_WITHOUT_CROSS_CHECKS == {"map/chromap", "map/star", "map/star-umi"}


def _stub_reader(path: Path, sample: str) -> SampleStats:
    """A reader the guard rows can hang a synthetic spec off. Never called; only registered."""
    return SampleStats(sample_id=sample)


def _plural_reader(path: Path, samples: Sequence[str]) -> dict[str, SampleStats]:
    """Ditto, one arity out — a fan-in reader the guard refuses before anything could call it."""
    return {}


def _no_reader(reg: Any, mp: pytest.MonkeyPatch) -> None:
    mp.setattr(reg, "MODULES", {**reg.MODULES, "map/fourth": reg.MODULES["map/star"]}, raising=True)


def _reader_for_no_module(reg: Any, mp: pytest.MonkeyPatch) -> None:
    mp.setattr(reg, "_SPECS", {**reg._SPECS, "map/ghost": reg._SPECS["map/starsolo"]}, raising=True)


def _spec(reg: Any, mp: pytest.MonkeyPatch, **kw: object) -> None:
    mp.setattr(
        reg,
        "_SPECS",
        {**reg._SPECS, "map/star": reg.StatsSpec(artifact="x", read=_stub_reader, **kw)},
        raising=True,
    )


def _rules_forgotten(reg: Any, mp: pytest.MonkeyPatch) -> None:
    _spec(reg, mp)
    mp.setattr(reg, "MODULES_WITHOUT_CROSS_CHECKS", MODULES_WITHOUT_CROSS_CHECKS - {"map/star"})


def _rules_and_silence_both(reg: Any, mp: pytest.MonkeyPatch) -> None:
    _spec(reg, mp, checks=(chemistry_rule,))


def _fan_in_reader_pointed_nowhere(reg: Any, mp: pytest.MonkeyPatch) -> None:
    _spec(reg, mp, read_fan_in=_plural_reader)


@pytest.mark.parametrize(
    ("drift", "named"),
    [
        (_no_reader, "map/fourth"),
        (_reader_for_no_module, "unknown module"),
        (_rules_forgotten, r"map/star.*declare no cross-checks"),
        (_rules_and_silence_both, "both declare cross-checks"),
        (_fan_in_reader_pointed_nowhere, "no fan_in_artifact"),
    ],
    ids=["no-reader", "reader-for-no-module", "rules-forgotten", "rules-and-silence", "fan-in"],
)
def test_the_registry_guard_catches_every_way_a_module_and_its_reader_can_drift(
    drift: Callable[[Any, pytest.MonkeyPatch], None],
    named: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guard nobody has seen fail is a guard that may not be looking, and it has five ways to look.

    One row per way a maintainer gets this wrong. The last is the quietest: a fan-in reader for a
    module declaring no such artifact reads nothing forever, because `read_pipeline_stats` has no
    path to hand it one. Every registry is rebound rather than mutated.
    """
    from seqforge.workflows import stats as stats_registry

    drift(stats_registry, monkeypatch)

    with pytest.raises(AssertionError, match=named):
        stats_registry._check_registry()


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
    # A module that measures no barcode at all still reports its metrics, with nothing to say about
    # them -- silence declared, not silence by omission.
    assert stats.findings == []
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


@pytest.mark.parametrize(
    ("summary", "alert_id"),
    [
        (
            {"Gene": {**_HEALTHY_SUMMARY, "Reads With Valid Barcodes": 0.000762759}},
            "starsolo.valid-barcodes-near-zero",
        ),
        (
            {"Gene": _NUCLEAR_GENE, "GeneFull": _NUCLEAR_GENEFULL},
            "starsolo.intronic-reads-uncounted",
        ),
        (
            {"Gene": {**_HEALTHY_SUMMARY, "Reads Mapped to Gene: Unique Gene": 0.021}},
            "starsolo.reads-mapped-but-not-counted",
        ),
    ],
    ids=["chemistry", "solo-features", "gene-model"],
)
def test_every_rule_starsolo_declares_reaches_the_pipeline_through_the_registry(
    summary: dict[str, object], alert_id: str, tmp_path: Path
) -> None:
    """A rule that is written and never registered fires in its own unit test and on nothing else.

    Driven through `read_pipeline_stats` over real bytes rather than by calling the rule, one row per
    rule `map/starsolo` declares, with the healthy second sample as the discriminator: a rule wired
    to fire unconditionally would name it too. And the finding rides on the PIPELINE — `SampleStats`
    is what the artifact said, a judgement about a decision is a second envelope, and that is what
    makes "did this fire on every sample" answerable at all.
    """
    results = tmp_path / "results"
    _landed(results, "s1", {"sample": "s1", "summary": summary})
    _landed(results, "s2", {"sample": "s2", "summary": {"Gene": _HEALTHY_SUMMARY}})

    stats = read_pipeline_stats("map/starsolo", results, ["s1", "s2"])

    assert stats is not None
    assert [(f.sample_id, f.alert_id) for f in stats.findings] == [("s1", alert_id)]
    assert not any(hasattr(s, "findings") for s in stats.samples), (
        "a finding is a judgement, and SampleStats carries what the artifact said"
    )


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
    must agree file-for-file. Since a run went lane-blind (ADR-0027) a multi-lane library is
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


def _two_run_cell() -> list[dict[str, str]]:
    """One plate cell topped up across two runs — the 20-of-190 shape, tagged read and mate."""
    return [
        {"sample_id": "c1", "run": "runB", "lane": "", "read_id": "R2", "path": "B_cdna.fq"},
        {"sample_id": "c1", "run": "runA", "lane": "", "read_id": "R1", "path": "A_tag.fq"},
        {"sample_id": "c1", "run": "runB", "lane": "", "read_id": "R1", "path": "B_tag.fq"},
        {"sample_id": "c1", "run": "runA", "lane": "", "read_id": "R2", "path": "A_cdna.fq"},
    ]


def test_a_cell_across_two_runs_pairs_by_where_each_file_was_sequenced() -> None:
    """`paired_fastqs` puts each tagged file beside the mate from its OWN run, not its own index.

    The rows arrive interleaved and out of order on purpose: what makes the two lists parallel must
    be the `run` and `lane` columns (ADR-0027), never the accident that two independent sorts came
    out the same way. The tagged order is still `ordered_fastqs`' order — one derivation used twice,
    which is the property ADR-0036 turns on.
    """
    from seqforge.workflows.units import ordered_fastqs, paired_fastqs

    units = _two_run_cell()

    tagged, mates = paired_fastqs(units, "c1", "R1")
    assert tagged == ordered_fastqs(units, "c1", "R1") == ["A_tag.fq", "B_tag.fq"]
    assert mates == ["A_cdna.fq", "B_cdna.fq"]


def test_a_lane_reorders_the_pairing_the_way_it_reorders_the_order() -> None:
    """Within one run, `lane` places the pair — and the filenames sort AGAINST it, deliberately.

    A pairing that fell back on the path sort would come out reversed here and would still hold on
    every real bcl2fastq name, which is the coincidence `units.tsv` grew a lane column to stop
    depending on.
    """
    from seqforge.workflows.units import paired_fastqs

    units = [
        {"sample_id": "c1", "run": "r", "lane": "L001", "read_id": "R1", "path": "z_tag.fq"},
        {"sample_id": "c1", "run": "r", "lane": "L002", "read_id": "R1", "path": "a_tag.fq"},
        {"sample_id": "c1", "run": "r", "lane": "L001", "read_id": "R2", "path": "a_cdna.fq"},
        {"sample_id": "c1", "run": "r", "lane": "L002", "read_id": "R2", "path": "z_cdna.fq"},
    ]

    assert paired_fastqs(units, "c1", "R1") == (
        ["z_tag.fq", "a_tag.fq"],  # L001, L002
        ["a_cdna.fq", "z_cdna.fq"],  # ... and each mate is the one from THAT lane
    )


def test_a_run_whose_mate_was_never_deposited_is_refused_by_name() -> None:
    """A place holding a tagged file and no mate: refused, naming the file and the run.

    Not shifted onto the next run's mate, which is what a two-list pairing by index does — and it
    does it silently, because the shift only shows up as a record-count disagreement if the two runs
    happen to hold different numbers of reads.
    """
    from seqforge.workflows.units import UnitsError, paired_fastqs

    units = [row for row in _two_run_cell() if (row["run"], row["read_id"]) != ("runB", "R2")]

    with pytest.raises(UnitsError, match=r"B_tag\.fq.*runB"):
        paired_fastqs(units, "c1", "R1")

    # ... and the mirror: a mate with no tagged read to inherit a UMI from is surplus, not spare.
    orphan = [row for row in _two_run_cell() if (row["run"], row["read_id"]) != ("runB", "R1")]
    with pytest.raises(UnitsError, match=r"B_cdna\.fq"):
        paired_fastqs(orphan, "c1", "R1")


def test_a_sample_with_no_second_role_has_no_mate_and_one_with_two_is_refused() -> None:
    """Absence IS the statement (ADR-0035), and ambiguity is a refusal rather than a first pick."""
    from seqforge.workflows.units import UnitsError, mate_role, paired_fastqs

    single = [row for row in _two_run_cell() if row["read_id"] == "R1"]
    assert mate_role(single, "c1", "R1") is None
    assert paired_fastqs(single, "c1", "R1") == (["A_tag.fq", "B_tag.fq"], None)

    crowded = [*single, {"sample_id": "c1", "run": "runA", "lane": "", "read_id": "R2", "path": "c"},
               {"sample_id": "c1", "run": "runA", "lane": "", "read_id": "R3", "path": "d"}]  # fmt: skip
    with pytest.raises(UnitsError, match="at most one mate"):
        paired_fastqs(crowded, "c1", "R1")

    # A sample the table says nothing about is a refusal too: an empty run writes an empty artifact.
    with pytest.raises(UnitsError, match="names no 'R1' file for sample 'ghost'"):
        paired_fastqs(single, "ghost", "R1")


# ================================================================================================
# umite/count — one counting job over a whole plate, into one h5ad
# ================================================================================================
#
# The centrepiece is a synthetic annotation and BAM where **every fragment's fate is known by
# construction**. That is deliberate rather than convenient: the frozen agreement record captured
# against the real tool proves "agrees with umite on these ten cells" and is structurally blind to
# the one arithmetic this port most easily inverts — its combined UMI figure equals exon + intron
# *exactly* on all ten cells, so a port that deduplicated over the union of the two buckets would
# have passed it. A fixture built by construction proves the counting *rule*, and it carries no
# species: a real-genome slice would bake one assembly and one annotation into this repo.

_GTF = """\
chr1\tsynthetic\tgene\t1\t1000\t.\t+\t.\tgene_id "GENE_A"; gene_name "alpha";
chr1\tsynthetic\texon\t101\t200\t.\t+\t.\tgene_id "GENE_A"; transcript_id "GENE_A.1";
chr1\tsynthetic\texon\t501\t600\t.\t+\t.\tgene_id "GENE_A"; transcript_id "GENE_A.1";
chr1\tsynthetic\tgene\t2001\t3000\t.\t-\t.\tgene_id "GENE_B"; gene_name "beta";
chr1\tsynthetic\texon\t2101\t2200\t.\t-\t.\tgene_id "GENE_B"; transcript_id "GENE_B.1";
chr1\tsynthetic\tgene\t4001\t5000\t.\t+\t.\tgene_id "GENE_C"; gene_name "gamma";
chr1\tsynthetic\texon\t4601\t4700\t.\t+\t.\tgene_id "GENE_C"; transcript_id "GENE_C.1";
chr1\tsynthetic\tgene\t4501\t5500\t.\t+\t.\tgene_id "GENE_D"; gene_name "delta";
chr1\tsynthetic\texon\t4651\t4750\t.\t+\t.\tgene_id "GENE_D"; transcript_id "GENE_D.1";
"""

#: What the GTF above says, 0-based half-open, so the fragments below can be placed against the
#: geometry rather than against a re-derivation of it: GENE_A [0,1000) with exons [100,200) and
#: [500,600); GENE_B [2000,3000) exon [2100,2200); GENE_C [4000,5000) exon [4600,4700); GENE_D
#: [4500,5500) exon [4650,4750). C and D overlap in their bodies from 4500 and in their exons from
#: 4650, which is what makes a fragment there ambiguous in two different ways. `chrUn_synthetic` is
#: in the BAM header and in no GTF line at all.
_CONTIGS = [{"SN": "chr1", "LN": 10000}, {"SN": "chrUn_synthetic", "LN": 1000}]

_READ_LEN = 20


def _annotation_db(tmp_path: Path) -> Path:
    """The synthetic GTF, built into a gffutils database the way liulab-genome builds one.

    The flags mirror `genome.io.gtf.register_gtf` — `keep_order`, `merge_strategy="create_unique"`,
    `sort_attribute_values`, and inference disabled because a real annotation declares its own
    `gene` rows. Building it here rather than reaching for a registered assembly is what keeps this
    species-free and runnable with no genome on the box: what the counter consumes is a database,
    and this is one, written by the same library.
    """
    import gffutils

    gtf = tmp_path / "synthetic.gtf"
    gtf.write_text(_GTF)
    db = tmp_path / "synthetic.db"
    built = gffutils.create_db(
        str(gtf),
        str(db),
        keep_order=True,
        merge_strategy="create_unique",
        sort_attribute_values=True,
        disable_infer_genes=True,
        disable_infer_transcripts=True,
    )
    built.conn.close()  # gffutils leaves it open; `register_gtf` closes its own for the same reason
    return db


@dataclass(frozen=True)
class _Fragment:
    """One fragment to synthesise: where it lands, what it carries, and how many loci it claims."""

    name: str
    contig: str
    start: int
    end: int
    umi: str = ""
    hits: int = 1
    mate_unmapped: bool = False


def _segments(header: Any, frag: _Fragment) -> list[Any]:
    """One `_Fragment` -> its BAM records: two mates, or one when the mate never aligned."""
    import pysam

    tid = header.get_tid(frag.contig)
    span = frag.end - frag.start
    mate_start = frag.end - _READ_LEN

    def build(start: int, flag: int, mate: int, tlen: int) -> Any:
        rec = pysam.AlignedSegment(header)
        rec.query_name = frag.name
        rec.query_sequence = "A" * _READ_LEN
        rec.query_qualities = pysam.qualitystring_to_array("I" * _READ_LEN)
        rec.flag = flag
        rec.reference_id = tid
        rec.reference_start = start
        rec.mapping_quality = 255
        rec.cigarstring = f"{_READ_LEN}M"
        rec.next_reference_id = tid
        rec.next_reference_start = mate
        rec.template_length = tlen
        tags: list[tuple[str, object, str]] = [("NH", frag.hits, "i")]
        if frag.umi:
            tags.append(("UB", frag.umi, "Z"))
        rec.set_tags(tags)
        return rec

    if frag.mate_unmapped:
        # PAIRED | MATE_UNMAPPED | READ1, and no second record: STAR writes none unless asked to,
        # so the flag on this one is the only evidence that the fragment did not align.
        return [build(frag.start, 1 | 8 | 64, frag.start, 0)]
    return [
        build(frag.start, 1 | 2 | 32 | 64, mate_start, span),
        build(mate_start, 1 | 2 | 16 | 128, frag.start, -span),
    ]


def _synthetic_bam(path: Path, fragments: Sequence[_Fragment]) -> Path:
    """`fragments` -> a COORDINATE-sorted BAM, which is the input contract this counter has.

    Sorting by position is what scatters each fragment's two mates apart, so a port that quietly
    depended on name adjacency goes red here rather than in production.
    """
    import pysam

    header = pysam.AlignmentHeader.from_dict(
        {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": _CONTIGS}
    )
    records = [rec for frag in fragments for rec in _segments(header, frag)]
    records.sort(key=lambda r: (r.reference_id, r.reference_start))
    with pysam.AlignmentFile(str(path), "wb", header=header) as out:
        for rec in records:
            out.write(rec)
    return path


#: The plate every fate assertion below reads. One line per fragment, and the comment above each is
#: the whole expected result — nothing here is computed, so no assertion can agree with it by
#: sharing an arithmetic error.
_PLATE: tuple[_Fragment, ...] = (
    # GENE_A, exonic, the same UMI twice: two observations that deduplicate to one count.
    _Fragment("a_exon_1", "chr1", 120, 180, umi="AAAAAAAA"),
    _Fragment("a_exon_2", "chr1", 520, 580, umi="AAAAAAAA"),
    # That SAME UMI again, this time between the two exons. Deduplicated inside its own bucket, so
    # it counts once more in the intron matrix — `inex` is NOT `exon + intron`.
    _Fragment("a_intron", "chr1", 300, 360, umi="AAAAAAAA"),
    # The first mate lands in the intron and its mate reaches the second exon; the span between
    # them is what makes the fragment exonic, recovered from this record's own mate coordinates.
    _Fragment("a_spanning", "chr1", 300, 520, umi="GGGGGGGG"),
    # Untagged: one count each in the read matrices, and one count is what says both mates were not
    # counted twice.
    _Fragment("a_exon_read", "chr1", 120, 180),
    _Fragment("a_intron_read", "chr1", 300, 360),
    _Fragment("b_exon", "chr1", 2120, 2180, umi="CCCCCCCC"),
    # NH says two loci. One record, primary, over an exon — which the reference counts into GENE_A.
    _Fragment("multimapper", "chr1", 120, 180, umi="TTTTTTTT", hits=2),
    # Aligned to a scaffold no GTF line mentions, and to a gap between genes: both `_no_feature`.
    _Fragment("scaffold", "chrUn_synthetic", 50, 110),
    _Fragment("intergenic", "chr1", 8000, 8060),
    # Exons of GENE_C and GENE_D at once, then bodies of both with no exon: ambiguous twice over.
    _Fragment("ambiguous_exon", "chr1", 4660, 4690),
    _Fragment("ambiguous_intron", "chr1", 4520, 4560),
    _Fragment("mate_never_aligned", "chr1", 120, 180, umi="AAAAAAAA", mate_unmapped=True),
)


def _plate(tmp_path: Path) -> tuple[Path, list[tuple[str, Path]]]:
    """The two-cell plate: the annotation database, and the cells in the order they are handed over."""
    db = _annotation_db(tmp_path)
    cell_a = _synthetic_bam(tmp_path / "cell_a.bam", _PLATE)
    cell_b = _synthetic_bam(tmp_path / "cell_b.bam", _PLATE[6:7])  # GENE_B alone
    return db, [("cell_a", cell_a), ("cell_b", cell_b)]


def _row(adata: ad.AnnData, sample: str, gene: str, layer: str | None = None) -> int:
    """One cell's count for one gene, by name — never by an index this file also computed."""
    return int(
        _counts(adata, layer)[adata.obs_names.get_loc(sample), adata.var_names.get_loc(gene)]
    )


def _frame(table: object) -> Any:
    """`adata.obs`/`adata.var` are declared as a union with a lazy on-disk table.

    On an object this file just built or read back it is a pandas frame, narrowed once here rather
    than with a cast at every call site — the same move `_counts` makes for a matrix.
    """
    return table


def test_the_annotation_is_read_from_the_built_database_with_no_gtf_parse(tmp_path: Path) -> None:
    """What the counter consumes is the database liulab-genome built, not the GTF it built it from.

    The reference parses the GTF with HTSeq into two `GenomicArrayOfSets`, pickles them at 47.5 MB
    and serialises that into every worker — ~76 GB through pipes at 1440 cells, on top of a 50 s
    parse. Reading the database instead is what deletes both, so the gene axis arriving from it,
    correct, is the load-bearing claim of the port's largest single win.
    """
    annotation = read_annotation(_annotation_db(tmp_path))

    assert set(annotation.gene_ids) == {"GENE_A", "GENE_B", "GENE_C", "GENE_D"}
    assert annotation.gene_names[annotation.gene_ids.index("GENE_A")] == "alpha"

    # The geometry, asked as the overlap questions the counter asks rather than read back as spans.
    a = annotation.gene_ids.index("GENE_A")
    assert annotation.exonic("chr1", 120, 140) == frozenset({a})
    assert annotation.exonic("chr1", 300, 320) == frozenset()  # between GENE_A's two exons
    assert annotation.gene_bodies("chr1", 300, 320) == frozenset({a})
    # A contig with no annotated feature at all answers empty rather than raising — see the fate
    # test below for why that difference is worth a test of its own.
    assert annotation.gene_bodies("chrUn_synthetic", 50, 70) == frozenset()


def test_a_missing_or_unreadable_annotation_database_is_a_refusal_not_an_empty_gene_axis(
    tmp_path: Path,
) -> None:
    """An annotation that cannot be read must not become a matrix of zeros nobody questions."""
    with pytest.raises(UmiCountError, match="does not exist"):
        read_annotation(tmp_path / "never-registered.db")

    not_a_database = tmp_path / "junk.db"
    not_a_database.write_bytes(b"this is not sqlite")
    with pytest.raises(UmiCountError):
        read_annotation(not_a_database)


def test_every_fragment_of_the_synthetic_plate_lands_where_it_was_built_to_land(
    tmp_path: Path,
) -> None:
    """The whole counting rule at once, against a plate whose every fate is known by construction.

    Thirteen fragments, four counted matrices and four fates; every number below is read off the
    fixture's own comments rather than recomputed here.
    """
    db, cells = _plate(tmp_path)
    adata, per_cell = count_plate(cells, read_annotation(db))
    cell = per_cell[0]

    assert cell.n_fragments == len(_PLATE)
    assert cell.fates == {
        "unmapped": 1,  # mate_never_aligned
        "multimapping": 1,  # NH == 2
        "no_feature": 2,  # the scaffold, and the intergenic fragment
        "ambiguous": 2,  # two exonic genes, then two gene bodies and no exon
    }

    # UMIs: GENE_A carries "AAAAAAAA" (twice, deduplicated) and "GGGGGGGG" (the spanning fragment).
    assert _row(adata, "cell_a", "GENE_A") == 2
    assert _row(adata, "cell_a", "GENE_B") == 1
    # ...and an untagged fragment never reaches a UMI matrix, nor a tagged one a read matrix.
    assert _row(adata, "cell_a", "GENE_A", "read_exon") == 1
    assert _row(adata, "cell_a", "GENE_A", "read_intron") == 1
    # The multimapper's gene, and both ambiguous ones, stay at zero in every matrix — all five of
    # them, counted off `LAYERS` rather than written out, so a sixth matrix is not silently unasserted.
    for gene in ("GENE_C", "GENE_D"):
        assert [_row(adata, "cell_a", gene, layer) for layer in (None, *LAYERS)] == [0] * (
            1 + len(LAYERS)
        )

    # The second cell is a different row, not a copy of the first.
    assert _row(adata, "cell_b", "GENE_B") == 1
    assert _row(adata, "cell_b", "GENE_A") == 0
    assert int(_frame(adata.obs).loc["cell_b", N_FRAGMENTS]) == 1


def test_a_umi_seen_both_exonically_and_intronically_counts_once_in_each_so_inex_is_not_their_sum(
    tmp_path: Path,
) -> None:
    """The arithmetic the frozen agreement fixture is blind to, pinned by construction instead.

    "AAAAAAAA" reaches GENE_A three times: twice over an exon, once between the exons. Each bucket
    is deduplicated on its own, so it counts once in each and the exon and intron matrices sum to
    two — where deduplicating over the union of the buckets would give one. The two differ by
    exactly the UMIs seen both ways on one gene, and on the ten real cells captured against the
    reference that intersection is empty, so this case cannot be measured against real data at that
    depth.
    """
    db, cells = _plate(tmp_path)
    adata, _ = count_plate(cells, read_annotation(db))

    assert _PLATE[0].umi == _PLATE[2].umi  # the same UMI, one exonic fragment and one intronic
    assert _row(adata, "cell_a", "GENE_A", "umi_intron") == 1
    assert _row(adata, "cell_a", "GENE_A") + _row(adata, "cell_a", "GENE_A", "umi_intron") == 3
    # And the combined matrix is the figure that arithmetic is NOT: the union of the two buckets
    # holds "AAAAAAAA" (seen both ways, one molecule) and the spanning fragment's "GGGGGGGG".
    assert _row(adata, "cell_a", "GENE_A", "umi_combined") == 2
    # A gene only ever seen one way has nothing to combine, and the layer still carries its count --
    # this matrix is every UMI on the gene, not the ones that crossed the exon/intron line.
    assert _row(adata, "cell_a", "GENE_B", "umi_combined") == 1


#: The one construction that separates all three candidate definitions of the combined UMI matrix,
#: which is why it is written down rather than picked. Two UMIs one substitution apart on GENE_A of
#: one cell: exonically "AAAAAAAA" x2 and "AAAAAAAT" x2, intronically "AAAAAAAA" x5 and "AAAAAAAT"
#: x1. Correction is Hamming-1 WITH a count-ratio test, so the abundances decide, and here they
#: decide differently in each bucket and differently again in the union -- see the test below for
#: the three numbers that come out. Every other case anybody would reach for first (one UMI seen
#: both ways, two neighbours in one bucket) leaves at least two of the three definitions agreeing.
_SPLIT_NEIGHBOURS: tuple[_Fragment, ...] = (
    # Exonic: two observations of each UMI, over GENE_A's two exons.
    _Fragment("exon_a_1", "chr1", 120, 180, umi="AAAAAAAA"),
    _Fragment("exon_a_2", "chr1", 520, 580, umi="AAAAAAAA"),
    _Fragment("exon_b_1", "chr1", 120, 180, umi="AAAAAAAT"),
    _Fragment("exon_b_2", "chr1", 520, 580, umi="AAAAAAAT"),
    # Intronic: five of the first UMI and one of its neighbour, between the two exons.
    _Fragment("intron_a_1", "chr1", 300, 360, umi="AAAAAAAA"),
    _Fragment("intron_a_2", "chr1", 300, 360, umi="AAAAAAAA"),
    _Fragment("intron_a_3", "chr1", 300, 360, umi="AAAAAAAA"),
    _Fragment("intron_a_4", "chr1", 300, 360, umi="AAAAAAAA"),
    _Fragment("intron_a_5", "chr1", 300, 360, umi="AAAAAAAA"),
    _Fragment("intron_b_1", "chr1", 300, 360, umi="AAAAAAAT"),
)


def test_two_neighbour_umis_split_across_the_buckets_merge_only_if_the_counts_merge_first(
    tmp_path: Path,
) -> None:
    """Why the combined matrix is a MATRIX: no arithmetic over the published two recovers it.

    The reference merges the two populations **before** correcting — under `--combine_unspliced` the
    bucket key stays `'U'` for an intronic assignment (`umicount.py:401`) and `umi_correction` then
    runs once over the union (`umicount.py:437-448`) — and the count-ratio guard is what makes that
    ordering observable. On this cell's GENE_A:

        exon   "AAAAAAAA" x2, "AAAAAAAT" x2  -> 2 UMIs   (2*2-1 > 2: the neighbour survives)
        intron "AAAAAAAA" x5, "AAAAAAAT" x1  -> 1 UMI    (2*1-1 <= 5: the neighbour is absorbed)
        union  "AAAAAAAA" x7, "AAAAAAAT" x3  -> 1 UMI    (2*3-1 <= 7: absorbed again)

    So the three candidate definitions give three different answers here, and the two wrong ones are
    computed below from the same counts rather than described: a port that adds the two matrices
    reports **3**, a port that corrects each bucket and unions the surviving keys reports **2**, and
    only merging the raw observations first reports **1**. That is the whole reason this layer
    exists — both wrong answers are what a reader would derive from an object that omitted it.
    """
    annotation = read_annotation(_annotation_db(tmp_path))
    a = annotation.gene_ids.index("GENE_A")
    counts = count_bam(_synthetic_bam(tmp_path / "split.bam", _SPLIT_NEIGHBOURS), annotation)

    # The fixture as built, read back off the raw buckets: nothing below is deduplicated yet.
    assert counts.umi_exon[a] == {"AAAAAAAA": 2, "AAAAAAAT": 2}
    assert counts.umi_intron[a] == {"AAAAAAAA": 5, "AAAAAAAT": 1}

    entries = deduplicate(counts)
    assert entries["umi_exon"][a] == 2
    assert entries["umi_intron"][a] == 1
    assert entries["umi_combined"][a] == 1

    # The two answers that are NOT this matrix, from the same counts. Sum first...
    assert entries["umi_exon"][a] + entries["umi_intron"][a] == 3
    # ...then the tempting one: correct each bucket, union what survives. It cannot see that the
    # neighbour's seven-observation seed and its own three are one molecule, because by then the
    # counts it would have to compare have already been thrown away.
    corrected_apart = set(correct_umis(counts.umi_exon[a])) | set(
        correct_umis(counts.umi_intron[a])
    )
    assert len(corrected_apart) == 2

    # And it is the object's number, not just `deduplicate`'s: the layer is what a reader opens.
    adata, _ = count_plate([("one_cell", tmp_path / "split.bam")], annotation)
    assert _row(adata, "one_cell", "GENE_A", "umi_combined") == 1


def test_a_multimapper_is_read_off_nh_and_never_reaches_the_gene_it_aligned_to(
    tmp_path: Path,
) -> None:
    """`NH`, not bundle length — which is what makes the aligner's one-record flag survivable.

    The reference infers multimapping from how many alignments a read name has, so with STAR
    emitting exactly one record per multimapper every bundle is length 1 and every multimapper is
    counted into a gene: `_multimapping` measured 0 for all ten fixture cells while 12.6% of aligned
    names carried `NH > 1`, and realigning one cell with the flag as the only difference moved its
    primary UMI matrix by +10.2% — more than the fuzzy-matching gain the tool is published for.
    """
    db = _annotation_db(tmp_path)
    annotation = read_annotation(db)
    exonic = _Fragment("hit", "chr1", 120, 180, umi="AAAAAAAA")

    unique = count_bam(_synthetic_bam(tmp_path / "unique.bam", (exonic,)), annotation)
    multi = count_bam(
        _synthetic_bam(tmp_path / "multi.bam", (replace(exonic, hits=4),)), annotation
    )

    assert unique.fates["multimapping"] == 0
    assert unique.umi_exon[annotation.gene_ids.index("GENE_A")] == {"AAAAAAAA": 1}
    assert multi.fates["multimapping"] == 1
    assert multi.umi_exon == {}  # the same fragment, over the same exon, counted nowhere


def test_a_read_on_a_scaffold_with_no_annotated_feature_is_no_feature_and_not_unmapped(
    tmp_path: Path,
) -> None:
    """The reference catches a lookup error on the contig and calls the read unmapped. It aligned.

    A handful of reads per cell land on scaffolds a GTF never mentions, and calling them unmapped
    charges an annotation gap to the aligner — wrong in a way a summary hides completely.
    """
    annotation = read_annotation(_annotation_db(tmp_path))
    bam = _synthetic_bam(
        tmp_path / "scaffold.bam", (_Fragment("off_annotation", "chrUn_synthetic", 50, 110),)
    )

    counts = count_bam(bam, annotation)

    assert counts.fates["no_feature"] == 1
    assert counts.fates["unmapped"] == 0


def test_both_mates_of_a_fragment_are_counted_once_and_the_mate_coordinates_still_place_it(
    tmp_path: Path,
) -> None:
    """Two records, one count — and the second mate still decides where the fragment goes.

    The input is coordinate-sorted, so the two mates are nowhere near each other and nothing here
    reconstructs the pair. One record stands for the fragment and the mate's footprint arrives
    through the template length that record already carries, which is why `a_spanning` — whose
    first mate is squarely in an intron — is counted as exonic rather than intronic.
    """
    annotation = read_annotation(_annotation_db(tmp_path))
    a = annotation.gene_ids.index("GENE_A")

    both = count_bam(
        _synthetic_bam(tmp_path / "pair.bam", (_Fragment("untagged", "chr1", 120, 180),)),
        annotation,
    )
    assert both.read_exon == {a: 1}  # two records went in
    assert both.n_fragments == 1

    spanning = count_bam(
        _synthetic_bam(
            tmp_path / "span.bam", (_Fragment("spanning", "chr1", 300, 520, umi="GGGGGGGG"),)
        ),
        annotation,
    )
    assert spanning.umi_exon == {a: {"GGGGGGGG": 1}}
    assert spanning.umi_intron == {}


def test_a_paired_record_flagged_as_neither_mate_refuses_instead_of_halving_the_cell(
    tmp_path: Path,
) -> None:
    """One record stands for a fragment, so a record that will not say which mate it is stops the run.

    Every count in the cell would otherwise simply be absent — at a plausible magnitude, with
    nothing raised and nothing to compare against. STAR always sets one of the two flags; a BAM
    where it did not is malformed, and this is what says so out loud.
    """
    import pysam

    annotation = read_annotation(_annotation_db(tmp_path))
    header = pysam.AlignmentHeader.from_dict(
        {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": _CONTIGS}
    )
    record = _segments(header, _Fragment("nameless_mate", "chr1", 120, 180))[0]
    record.flag = 1 | 2  # PAIRED and proper, and neither READ1 nor READ2
    bam = tmp_path / "malformed.bam"
    with pysam.AlignmentFile(str(bam), "wb", header=header) as out:
        out.write(record)

    with pytest.raises(UmiCountError, match="neither first nor second mate"):
        count_bam(bam, annotation)


def test_umi_correction_absorbs_a_neighbour_only_when_the_seed_can_explain_it() -> None:
    """Hamming-1 with the count-ratio guard, as a rule about two numbers rather than about a BAM.

    The guard is what keeps two genuinely distinct UMIs at similar depth apart: a candidate is only
    absorbed into a seed at least roughly twice as abundant. At a Hamming threshold of 3 the
    trailing check is vacuous and the merge starts manufacturing UMIs, which is why 1 is a module
    literal and not a flag.
    """
    # One error away and far rarer -> absorbed, and the seed keeps every observation.
    assert correct_umis({"AAAAAAAA": 9, "AAAAAAAT": 1}) == {"AAAAAAAA": 10}
    # One error away but comparably abundant -> two real UMIs, left alone.
    assert correct_umis({"AAAAAAAA": 2, "AAAAAAAT": 2}) == {"AAAAAAAA": 2, "AAAAAAAT": 2}
    # Two errors away -> beyond the threshold at any ratio.
    assert correct_umis({"AAAAAAAA": 9, "AAAAAATT": 1}) == {"AAAAAAAA": 9, "AAAAAATT": 1}
    # Equal counts break on the sequence, never on the order the BAM happened to hand them over.
    assert list(correct_umis({"TTTTTTTT": 3, "AAAAAAAA": 3})) == ["AAAAAAAA", "TTTTTTTT"]


def test_the_object_is_x_plus_four_layers_indexed_on_sample_id_with_the_fates_as_obs_columns(
    tmp_path: Path,
) -> None:
    """The deliverable's shape, which is the half of this ticket no wrong number would show.

    Rows are sample ids, which is what makes an h5ad row and a per-cell CRAM filename join. The
    fates are per-cell scalars and live on `obs`; the reference carries them as extra *gene*
    columns in a matrix whose other 55 335 columns really are genes, which is what forced a
    correction in its output shape.

    Five matrices and not six: the grid is (UMI | read) x (exon | intron | combined), and the sixth
    cell is deliberately absent. An untagged read has nothing to deduplicate by and the reference
    never tries, so a combined READ matrix is `read_exon + read_intron` exactly — a layer that earns
    nothing, kept out on the same rule that lets the combined UMI matrix in.
    """
    db, cells = _plate(tmp_path)
    out = write_umi_counts(cells, db, tmp_path / "plate" / "counts.h5ad")
    adata = ad.read_h5ad(out)

    assert list(adata.obs_names) == ["cell_a", "cell_b"]  # the order the cells were handed over
    assert _layer_names(adata) == set(LAYERS)
    assert set(LAYERS) == {"umi_intron", "umi_combined", "read_exon", "read_intron"}
    # The derivable cell of the grid, asserted as absent BY NAME: a reader adding two read columns
    # gets the right answer, and a sixth matrix would only be a second place for it to be wrong.
    assert "read_combined" not in _layer_names(adata)
    assert (
        _row(adata, "cell_a", "GENE_A", "read_exon")
        + _row(adata, "cell_a", "GENE_A", "read_intron")
        == 2
    )
    assert adata.uns["primary_matrix"] == PRIMARY_MATRIX
    assert set(adata.var_names) == {"GENE_A", "GENE_B", "GENE_C", "GENE_D"}
    assert set(adata.obs.columns) == {*FATES, N_FRAGMENTS}
    assert _frame(adata.var).loc["GENE_B", "gene_name"] == "beta"
    _counts(adata)  # sparse in the object and not only on disk: a plate is almost entirely zeros


def test_counting_the_same_plate_twice_gives_a_byte_identical_h5ad(tmp_path: Path) -> None:
    """Determinism, asserted on the artifact rather than on the absence of `random` in the source.

    The reference picks an alignment with an unseeded `random.choice` when a read has several
    primary alignments. There is nothing to choose here — every tie-break is written down — and
    this is what proves it, including the iteration orders that are only accidentally stable.
    """
    db, cells = _plate(tmp_path)

    first = write_umi_counts(cells, db, tmp_path / "first.h5ad")
    second = write_umi_counts(cells, db, tmp_path / "second.h5ad")

    assert first.read_bytes() == second.read_bytes()


def test_a_plate_refuses_rather_than_writing_a_row_that_names_two_cells(tmp_path: Path) -> None:
    """A sample id is an h5ad row, so two cells sharing one refuses instead of overwriting."""
    db, cells = _plate(tmp_path)
    annotation = read_annotation(db)

    with pytest.raises(UmiCountError, match="repeat"):
        count_plate([("same", cells[0][1]), ("same", cells[1][1])], annotation)
    with pytest.raises(UmiCountError, match="no cells"):
        count_plate([], annotation)
    with pytest.raises(UmiCountError, match="missing"):
        count_plate([("gone", tmp_path / "never-aligned.bam")], annotation)


def test_each_cells_sample_id_travels_with_its_bam_instead_of_being_read_off_the_filename() -> None:
    """The join the reference had to warn about, removed by never making it.

    Its rows come out labelled `SRR19884922.namesort.bam` — the BAM's basename, suffix and all — so
    every consumer has to strip something it was never told the shape of.
    """
    assert parse_cells(["c1=/x/one.bam", "c2=/y/two.bam"]) == [
        ("c1", Path("/x/one.bam")),
        ("c2", Path("/y/two.bam")),
    ]
    for malformed in ("/x/one.bam", "=/x/one.bam", "c1="):
        with pytest.raises(UmiCountError, match="sample_id=path"):
            parse_cells([malformed])


# ---- the plate object's second reader: what `seqforge report` gets out of it ----------------------
#
# These drive `read_pipeline_stats` and they live HERE, in the counter's section, for the reason
# ADR-0025 gives for the reader itself: what an `obs` column is called is the writer's fact, so the
# claim under test is that the columns `count_plate` writes are the columns the page reads. Every one
# of them therefore goes through the real writer over the synthetic plate above, rather than through
# a hand-built AnnData that could only ever agree with itself.


def _plate_results(tmp_path: Path, *, logged: Sequence[str] = ("cell_a", "cell_b")) -> Path:
    """A finished `map/star-umi` run on disk: the fan-in h5ad, plus a `Log.final.out` per cell.

    `logged` is which cells got as far as writing an alignment log — a preempted plate has cells the
    counter measured and STAR's per-cell log did not survive for, which is exactly the union case.
    """
    db, cells = _plate(tmp_path)
    results = tmp_path / "results"
    write_umi_counts(cells, db, results / PLATE_H5AD)
    for sample in logged:
        _write(
            results / sample / "Log.final.out",
            "".join(f"  {k} |\t{v}\n" for k, v in _HEALTHY_LOG.items()),
        )
    return results


def test_the_plates_read_fates_reach_the_report_beside_the_per_cell_alignment_log(
    tmp_path: Path,
) -> None:
    """The counter's own verdicts are on the page, and they arrive from the artifact that has them.

    A cell's alignment log says what STAR did with its reads and stops there — it cannot say how many
    fragments reached no gene, or were ambiguous, or were dropped as multimappers, because the
    counter had not run when STAR wrote it. Those are in the plate object's `obs`, one row per cell,
    and until this landed they were written and read by nobody.

    Carried as RATES over `n_fragments`, which is what that column is on the object for: cells on one
    plate differ by three orders of magnitude in depth, so a count of ambiguous fragments is not
    comparable across a page and a share is. The counts stay recoverable — `n_fragments` is a column
    of its own — so nothing is lost by dividing.
    """
    results = _plate_results(tmp_path)

    stats = read_pipeline_stats("map/star-umi", results, ["cell_a", "cell_b"])

    assert stats is not None and stats.complete
    cell_a = _by_key(stats.samples[0])
    # STAR's half is still there, untouched: this is one row per cell and not two.
    assert "uniquely_mapped" in cell_a
    # ...and the counter's half, off the synthetic plate whose every fate is known by construction:
    # 13 fragments, of which 1 unmapped, 1 multimapping, 2 no-feature and 2 ambiguous.
    assert cell_a[N_FRAGMENTS].value == len(_PLATE)
    assert cell_a["no_feature"].value == pytest.approx(2 / len(_PLATE))
    assert cell_a["ambiguous"].value == pytest.approx(2 / len(_PLATE))
    assert cell_a["unmapped"].value == pytest.approx(1 / len(_PLATE))
    assert cell_a["multimapping"].value == pytest.approx(1 / len(_PLATE))
    # Every fate the counter records has a column and a label a human can read, checked against
    # `FATES` itself rather than against a list here — a fifth fate must not reach the page unnamed.
    assert set(FATES) <= set(cell_a)
    assert all(label for _, label in stats.columns)
    # And none of them is graded. `map/star-umi` cross-checks nothing on the stated argument that a
    # bar for "too many fragments hit no feature" is a number nobody has measured; an ungraded column
    # says that out loud, where an invented threshold would tint a page nobody could act on.
    assert {_levels(stats.samples[0])[fate] for fate in FATES} == {"none"}
    assert stats.findings == []
    # Ten columns is past the width at which the report folds a table behind a control, so which of
    # these survives the fold is a decision: depth, and the fate that implicates the gene model.
    assert {m.key for m in stats.samples[0].metrics if m.headline} == {
        "uniquely_mapped",
        "unmapped_too_short",
        N_FRAGMENTS,
        "no_feature",
    }


def test_a_cell_the_counter_measured_is_reported_even_with_no_alignment_log_of_its_own(
    tmp_path: Path,
) -> None:
    """The join is a UNION, because a missing per-cell log does not unmake a counted cell.

    An intersection is the tempting shape — merge the fan-in into the rows that landed — and it
    silently shortens the plate: a cell whose `Log.final.out` was lost to a preemption still has a
    row in the object, a fragment count and a column in every matrix, and reporting it as absent
    would say the counter never saw it. `n_found` is how many cells one source or the other answered
    for, which is what "how much landed" means once landing can happen twice.
    """
    results = _plate_results(tmp_path, logged=["cell_a"])

    stats = read_pipeline_stats("map/star-umi", results, ["cell_a", "cell_b"])

    assert stats is not None
    assert [s.sample_id for s in stats.samples] == ["cell_a", "cell_b"]  # contracted order, kept
    assert (stats.n_found, stats.n_expected) == (2, 2)
    logged, counted = (_by_key(s) for s in stats.samples)
    assert "uniquely_mapped" in logged and "uniquely_mapped" not in counted
    # The fan-in-only cell is a real row and not a placeholder: it carries what the counter measured.
    assert counted[N_FRAGMENTS].value == 1  # `cell_b` is the one-fragment cell of the fixture
    assert set(FATES) <= set(counted)
    # A column one source alone produced is still a column for everyone -- the union rule the metric
    # table already keeps, now across two artifacts rather than across two samples.
    assert {key for key, _ in stats.columns} >= {"uniquely_mapped", *FATES, N_FRAGMENTS}


def test_the_plate_object_is_opened_once_however_many_cells_the_page_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One artifact, one read — the property that makes a dataset-scoped reader affordable at all.

    The per-sample half of this registry opens one file per sample because there is one file per
    sample. The fan-in half must not inherit that shape: on the 1440-cell deposit this module was
    built for, a per-sample open would parse one object holding every cell 1440 times to take one row
    out of each. Asserting it on a two-cell plate is enough to tell the two apart — the wrong shape
    counts two opens — and the wrong shape is invisible in every other assertion on this page.
    """
    from seqforge.workflows import stats as stats_registry

    results = _plate_results(tmp_path)
    spec = stats_registry._SPECS["map/star-umi"]
    assert spec.read_fan_in is not None, "this test is vacuous unless the plate declares a reader"
    real = spec.read_fan_in
    opened: list[Path] = []

    def counting(path: Path, samples: Sequence[str]) -> dict[str, SampleStats]:
        opened.append(path)
        return real(path, samples)

    monkeypatch.setattr(
        stats_registry,
        "_SPECS",
        {**stats_registry._SPECS, "map/star-umi": replace(spec, read_fan_in=counting)},
        raising=True,
    )

    stats = read_pipeline_stats("map/star-umi", results, ["cell_a", "cell_b"])

    assert stats is not None and stats.n_found == 2
    assert opened == [results / PLATE_H5AD]


def test_the_registry_reads_the_fan_in_artifact_the_module_declares_and_never_spells_its_name(
    tmp_path: Path,
) -> None:
    """One owner for the filename, and the registry is a READER of it like the rule that writes it.

    `map/star-umi` DECLARES `fan_in_artifact`, `star-umi.smk` names its output from that constant,
    and this reader asks the registry for it — so a rename reaches all three or fails at import.
    Spelled here instead, the reader's copy is the one that fails silently: a page that shows no
    fates looks exactly like a plate that has not been counted yet, so nothing raises and nobody is
    told. That is the same failure the QC-suffix constants above were made one owner to prevent.

    Asserted behaviourally — an object written under any other name is not found — because what a
    reader spells in its own source is not what decides which file it opens.
    """
    db, cells = _plate(tmp_path)
    results = tmp_path / "results"
    write_umi_counts(cells, db, results / "plate-counts.h5ad")  # a plausible name, and not the one
    _write(results / "cell_a" / "Log.final.out", "  Number of input reads |\t10\n")

    stats = read_pipeline_stats("map/star-umi", results, ["cell_a"])

    assert stats is not None
    assert not set(FATES) & set(_by_key(stats.samples[0]))
    assert stats.notes == []  # an artifact that is not there is missing, never a failure to read


def test_a_corrupt_plate_object_costs_a_note_and_never_the_cells_that_did_land(
    tmp_path: Path,
) -> None:
    """The fan-in is one file for the whole deposit, so an unreadable one must not cost the page.

    A per-sample artifact that cannot be parsed costs its own row — the registry has always said so.
    This one is 1440 rows, and the same rule has to hold one arity out: every cell keeps the half of
    its row STAR's log gave it, and the page says what it could not read rather than silently
    dropping five columns.
    """
    results = _plate_results(tmp_path)
    (results / PLATE_H5AD).write_bytes(b"this is not an h5ad")

    stats = read_pipeline_stats("map/star-umi", results, ["cell_a", "cell_b"])

    assert stats is not None
    assert [s.sample_id for s in stats.samples] == ["cell_a", "cell_b"]
    assert "uniquely_mapped" in _by_key(stats.samples[0])
    assert not set(FATES) & set(_by_key(stats.samples[0]))
    assert any(PLATE_H5AD in note for note in stats.notes), stats.notes


def test_a_cell_that_counted_nothing_has_no_rates_rather_than_four_zeroes() -> None:
    """The one division here with no answer, and absence is what it produces.

    Pure over an `obs` row, which is the seam the loader exists to keep testable: a cell whose BAM
    held no fragment at all cannot have a share of them unmapped, and a rendered `0.0%` is a number
    a reader acts on. The fragment count itself is a real zero and stays — it was measured.
    """
    empty = fate_metrics(dict.fromkeys(FATES, 0) | {N_FRAGMENTS: 0}, "cell_x")
    assert {m.key for m in empty.metrics} == {N_FRAGMENTS}
    assert empty.metrics[0].value == 0

    counted = fate_metrics({"unmapped": 1, N_FRAGMENTS: 4}, "cell_y")
    # And a fate the object never carried is absent too, never a zero: an older plate object written
    # before a fate existed did not measure zero of them, it measured nothing.
    assert {m.key: m.value for m in counted.metrics} == {N_FRAGMENTS: 4, "unmapped": 0.25}


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
    """Which read is tagged is a fact the layout already states, so nobody gets to name it.

    The id rides ON the geometry rather than beside it, which is what lets the verb refuse a rule
    wired to hand over the mate without being told separately which read that was.
    """
    layout = ReadLayout(modality="rna", reads=[_plain_read("R2"), _smartseq3_r1()])

    geometry = tagged_read_geometry(layout)

    assert geometry.read_id == "R1"


def test_the_rendered_geometry_round_trips_and_reads_as_the_layout_it_came_from() -> None:
    """One derived value carries all six numbers, and reading it back gives the same six.

    The rendering is what the composer emits into the config and what the rule hands the extractor,
    so a round trip that loses a field is a run that cuts a span out of the wrong bases at exit 0.
    Absolute offsets in the element model's own coordinates, so the string can be checked by eye
    against a spec: tag at 0, UMI at 11 for 8, the motif at 19, cDNA from 22.
    """
    geometry = geometry_for_read(_smartseq3_r1())

    assert geometry.render() == f"R1:{_TAG}@0:umi@11+8:GGG@19:cdna@22"
    assert TagGeometry.parse(geometry.render()) == geometry

    # ...and a string that is not one is refused rather than half-read: this value arrives from a
    # composed config, so a near-miss means the composer and the extractor disagree about what a
    # geometry IS.
    with pytest.raises(UmiExtractError, match="is not a rendered read structure"):
        TagGeometry.parse(f"R1:{_TAG}@0:umi@11:GGG@19:cdna@22")


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

    stats = extract_umis([r1], [r2], tmp_path / "cell.bam", geometry, sample="cell")
    read1, read2 = _records(tmp_path / "cell.bam")

    assert (stats.fragments, stats.tagged, stats.untagged) == (1, 1, 0)
    assert stats.offsets == {13: 1}
    assert read1.query_sequence == _CDNA
    qualities = read1.query_qualities
    assert qualities is not None, "a record whose sequence survived the trim without its qualities"
    assert pysam.qualities_to_qualitystring(qualities) == _quals(seq)[13 + 22 :]
    # The mate is untouched: only the tagged read carries a structural prefix.
    assert read2.query_sequence == _CDNA
    # An unaligned pair, flagged the way `samtools import` flags one.
    assert (read1.flag, read2.flag) == (77, 141)
    assert read1.get_tag("UB") == read2.get_tag("UB") == "ACGTACGT"


def test_an_untagged_read_keeps_every_base_and_carries_no_umi(tmp_path: Path) -> None:
    """Internal reads are a third to two thirds of a real library and are counted, not dropped."""
    geometry = geometry_for_read(_smartseq3_r1())
    r1, r2 = _write_pair(tmp_path, [(_CDNA, _CDNA)])

    stats = extract_umis([r1], [r2], tmp_path / "cell.bam", geometry, sample="cell")
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

    stats = extract_umis([r1], [r2], tmp_path / "cell.bam", geometry, sample="cell_42")
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
        extract_umis([r1], [r2], tmp_path / "cell.bam", geometry, sample="cell")


def test_a_plus_line_repeating_the_whole_id_is_the_normal_case_and_is_accepted(
    tmp_path: Path,
) -> None:
    """The gate refuses DISAGREEMENT, not repetition -- a repeated ID is what these files look like."""
    geometry = geometry_for_read(_smartseq3_r1())
    seq = _tagged("ACGTACGT")
    r1, r2 = tmp_path / "r1.fastq.gz", tmp_path / "r2.fastq.gz"
    _fastq(r1, [("@cell:0", seq, "+cell:0", _quals(seq))])
    _fastq(r2, [("@cell:0", _CDNA, "+cell:0", _quals(_CDNA))])

    assert extract_umis([r1], [r2], tmp_path / "cell.bam", geometry, sample="cell").tagged == 1


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
        extract_umis([r1], [r2], tmp_path / "cell.bam", geometry, sample="cell")


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

    first = extract_umis([r1], [r2], tmp_path / "one.bam", geometry, sample="cell")
    second = extract_umis([r1], [r2], tmp_path / "two.bam", geometry, sample="cell")

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
        extract_umis([r1], [r2], tmp_path / "cell.bam", geometry, sample="cell")


# ---- the same extraction with no mate beside it --------------------------------------------------
#
# The plate protocol publishes single-end sequencing configurations alongside the paired one, and the
# tag operation is entirely WITHIN the tagged read: find the anchor, cut the UMI out, trim the span,
# keep the rest. The mate contributes nothing to it and only inherits the resulting `UB` onto a
# record emitted alongside. So these cases are not a reduced extractor -- they are the same one, with
# the geometry parse, the anchor search, the bounded reader and both truncation verdicts shared
# unchanged, branching only at the write.


def test_a_single_end_plate_writes_one_unpaired_record_per_read(tmp_path: Path) -> None:
    """One record per fragment, and not one of them flagged as half of a pair.

    The flags are what make the uBAM self-describing, and the aligner reads them rather than being
    told: a `SAM PE` invocation over records the writer marked PAIRED with no mate beside them is a
    crash. So "unpaired" here has to be the ABSENCE of the paired bit -- flag `0x4` alone -- and not
    a paired record whose partner was dropped, which is what a reduced pairing would have produced.
    """
    geometry = geometry_for_read(_smartseq3_r1())
    reads = [(_tagged("ACGTACGT"), _CDNA), (_tagged("TTTTGGCC", offset=13), _CDNA), (_CDNA, _CDNA)]
    r1, _ = _write_pair(tmp_path, reads)

    stats = extract_umis([r1], None, tmp_path / "cell.bam", geometry, sample="cell")
    records = _records(tmp_path / "cell.bam")

    assert len(records) == len(reads)
    assert not any(record.is_paired for record in records)
    assert {record.flag for record in records} == {4}
    # A fragment is one record here and two on a paired plate, which is why the count is not `pairs`.
    assert (stats.fragments, stats.tagged, stats.untagged) == (3, 2, 1)
    assert stats.offsets == {0: 1, 13: 1}
    # The verb prints this object, so the rename is the only thing that may move in its key set.
    assert set(stats.to_dict()) == {"sample", "fragments", "tagged", "untagged", "offsets"}


def test_a_tagged_single_end_read_is_trimmed_from_its_anchor_and_carries_its_umi(
    tmp_path: Path,
) -> None:
    """The trim is measured from where the anchor WAS found, mate or no mate.

    The same claim the paired case makes, asserted again on the lone read because it is the whole
    operation here: trimming a constant 22 would leave 13 bases of read-through and mosaic end on
    the front of this read's cDNA, which aligns -- badly, and at exit 0. `UB` rides on the one
    record, since there is no second record for it to be inherited by.
    """
    geometry = geometry_for_read(_smartseq3_r1())
    seq = _tagged("ACGTACGT", offset=13)
    r1 = tmp_path / "r1.fastq.gz"
    _fastq(r1, [("@cell:0", seq, "+", _quals(seq))])

    stats = extract_umis([r1], None, tmp_path / "cell.bam", geometry, sample="cell")
    (record,) = _records(tmp_path / "cell.bam")

    assert (stats.fragments, stats.tagged, stats.untagged) == (1, 1, 0)
    assert stats.offsets == {13: 1}
    assert record.query_sequence == _CDNA
    qualities = record.query_qualities
    assert qualities is not None, "a record whose sequence survived the trim without its qualities"
    assert pysam.qualities_to_qualitystring(qualities) == _quals(seq)[13 + 22 :]
    assert record.get_tag("UB") == "ACGTACGT"
    assert record.get_tag("RG") == "cell"


def test_an_untagged_single_end_read_keeps_every_base_and_still_names_its_cell(
    tmp_path: Path,
) -> None:
    """Internal reads are a third to two thirds of a real library and are counted, not dropped.

    `RG` is the half that must survive the mate's absence: on a plate the cell IS the file, so the
    read group is the only thing on an untagged record that says which library it came from. A
    record that lost it would be counted into the plate under no cell at all.
    """
    geometry = geometry_for_read(_smartseq3_r1())
    r1, _ = _write_pair(tmp_path, [(_CDNA, _CDNA)])

    stats = extract_umis([r1], None, tmp_path / "cell.bam", geometry, sample="cell")
    (record,) = _records(tmp_path / "cell.bam")

    assert (stats.fragments, stats.tagged, stats.untagged) == (1, 0, 1)
    assert record.query_sequence == _CDNA
    assert not record.has_tag("UB")
    assert record.get_tag("RG") == "cell"


def test_the_mate_changes_nothing_about_what_is_extracted_from_the_tagged_read(
    tmp_path: Path,
) -> None:
    """The same file, extracted with and without its mate, yields the same UMIs and the same bases.

    This is the whole claim: the mate is an addition, not half of the operation, so taking it away
    may move the record count out the other end and nothing else. The obvious reading -- that
    single-end is a degenerate pairing -- wants a second code path that does less, and a second path
    is exactly what would let these two answers drift apart while both still exit 0.
    """
    geometry = geometry_for_read(_smartseq3_r1())
    reads = [(_tagged("ACGTACGT"), _CDNA), (_tagged("TTTTGGCC", offset=15), _CDNA), (_CDNA, _CDNA)]
    r1, r2 = _write_pair(tmp_path, reads)

    paired = extract_umis([r1], [r2], tmp_path / "paired.bam", geometry, sample="cell")
    alone = extract_umis([r1], None, tmp_path / "alone.bam", geometry, sample="cell")

    def carried(records: list[pysam.AlignedSegment]) -> list[tuple[str | None, str | None]]:
        return [
            (record.query_sequence, str(record.get_tag("UB")) if record.has_tag("UB") else None)
            for record in records
        ]

    tagged_half = [record for record in _records(tmp_path / "paired.bam") if record.is_read1]
    assert carried(_records(tmp_path / "alone.bam")) == carried(tagged_half)
    assert (alone.fragments, alone.tagged, alone.offsets) == (
        paired.fragments,
        paired.tagged,
        paired.offsets,
    )


def test_a_single_end_input_is_gated_by_the_same_truncation_and_gzip_verdicts(
    tmp_path: Path,
) -> None:
    """One bounded reader, one pair of verdicts, whether or not a mate is beside it.

    A second code path for the lone read is a second place for the input gate to be forgotten, and
    the forgetting is silent: a truncated upload extracts a cell that is quietly missing its tail,
    and a file that is not gzip at all yields an empty BAM at exit 0. Both are asserted here so the
    shared reader stays the one that decides.
    """
    geometry = geometry_for_read(_smartseq3_r1())
    r1, _ = _write_pair(tmp_path, [(_tagged("ACGTACGT"), _CDNA)])
    whole = r1.read_bytes()

    r1.write_bytes(whole[: len(whole) - 12])
    with pytest.raises(UmiExtractError, match="ends mid-record|not readable gzip"):
        extract_umis([r1], None, tmp_path / "cut.bam", geometry, sample="cell")

    r1.write_bytes(b"@cell:0\n" + _CDNA.encode() + b"\n+\n" + _quals(_CDNA).encode() + b"\n")
    with pytest.raises(UmiExtractError, match="not readable gzip"):
        extract_umis([r1], None, tmp_path / "plain.bam", geometry, sample="cell")


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


@NO_STAR_ALIGNMENT_ON_MACOS
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

    Needs STAR and samtools, which seqforge does not own. What runs it is the `test-external`
    environment, which carries both from bioconda for this purpose alone, and CI's `test (external
    binaries)` job, which is `pytest -m external` in that environment on every pull request. Before
    that environment existed this ran on no host this project's CI could reach — which is to say
    nowhere, for the life of the repo, silently, because a skip is green (#333).
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
    assert extract_umis([r1], [r2], ubam, geometry, sample="cell").tagged == 1

    subprocess.run(
        [star, "--genomeDir", str(index), "--readFilesIn", str(ubam),
         "--readFilesType", "SAM", "PE", "--readFilesCommand", samtools, "view",
         "--sysShell", "/bin/bash",
         "--readFilesSAMattrKeep", "All", "--outSAMtype", "BAM", "Unsorted",
         "--outFileNamePrefix", str(tmp_path / "star_")],
        check=True, capture_output=True,
    )  # fmt: skip

    aligned = _records(tmp_path / "star_Aligned.out.bam")
    assert aligned, "STAR aligned nothing, so the tag question was never asked"
    assert all(record.get_tag("UB") == "ACGTACGT" for record in aligned)


def test_the_shared_index_load_asks_the_scheduler_for_the_residency_it_holds() -> None:
    """`load_genome` is the job that materializes the genome segment, and it declared no memory.

    Every other rule in `map/star-umi` derives a request carefully; the one holding the largest
    single allocation on the node asked for zero, so a scheduler would pack jobs beside it knowing
    nothing about it. This goes red if `index_mem_mb` stops covering a per-cell request -- which is
    the bound that makes it safe to ask for once per node rather than per cell.

    It does NOT assert the rule wiring: a `.smk` is not importable, and compose's wiring gate already
    evaluates every `resources:` callable when it runs `snakemake -n`, so a broken expression there
    fails the gate rather than this test.
    """
    from seqforge.workflows.memory import PLATE_RETRIES, index_mem_mb, per_cell_mem_mb

    # An upper bound on what a mapping job needs, because a mapping job is this residency plus a
    # sort buffer. Asking for less on the rule that runs once per node is what costs the node.
    for attempt in range(1, PLATE_RETRIES + 2):
        assert index_mem_mb(_DEFAULT_MEM_MB, attempt) >= per_cell_mem_mb(_DEFAULT_MEM_MB, attempt)
    assert index_mem_mb(_DEFAULT_MEM_MB, 1) == _DEFAULT_MEM_MB


def test_the_recipe_figure_buys_a_sort_and_the_ratio_is_what_a_small_genome_must_clear() -> None:
    """One figure covers index residency AND a sort, and only the sort half constrains the recipe.

    Which term dominates a mapping job's peak is a property of the SAMPLE: a plate cell of a few
    thousand reads is index-dominated (27.7 GB against a 25 GB index), a 215M-read droplet sample is
    sort-dominated (~160 B/record, ~32 GB). The residency half is not this suite's to check -- it is a
    measurement against a real index -- but the sort half is arithmetic the shipped code does, so it
    is pinned here.

    Two claims, and the second is the one a small genome runs into. First, the default figure covers
    the sample the default was moved for, and shrinking it stops covering that sample -- so a future
    "ce11's index is 1.3 GB, drop the default" goes red here instead of in a 20-hour run. Second, the
    same three quarters read backwards is the floor a recipe sizing DOWN has to clear: the request
    must be at least four thirds of the sort expected. That inequality binds on a small genome, where
    nothing about residency argues for the default, and never on a human one.
    """
    from seqforge.workflows.memory import bam_sort_ram, per_cell_mem_mb

    mib = 1024 * 1024
    # The 215M-read sample the default was moved for: ~160 B/record ~= 32 GB of sort.
    needed_bytes = 32 * 1024 * mib
    assert bam_sort_ram(per_cell_mem_mb(_DEFAULT_MEM_MB, 1)) >= needed_bytes

    # Halving the recipe figure -- the tempting "small genome" edit -- stops covering it.
    assert bam_sort_ram(per_cell_mem_mb(_DEFAULT_MEM_MB // 2, 1)) < needed_bytes

    # The four thirds, at the boundary in both directions rather than as a comfortable inequality:
    # one MiB less than the ratio demands is one MiB of sort the sample does not get. Ceiling
    # division, because the share floors and the recipe states whole gigabytes anyway.
    wanted_mb = 8 * 1024
    four_thirds_mb = -(-4 * wanted_mb // 3)
    assert bam_sort_ram(per_cell_mem_mb(four_thirds_mb, 1)) >= wanted_mb * mib
    assert bam_sort_ram(per_cell_mem_mb(four_thirds_mb - 1, 1)) < wanted_mb * mib


# ================================================================================================
# the clip flags, judged by the binary that has to accept them rather than by the binary's own help
# ================================================================================================
#
# STAR's shipped help is stale relative to STAR's shipped code, in BOTH directions, and this project
# relied on both halves. `parametersDefault` still advertises a `clipAdapterType None` mode the code
# rejects outright, and it still carries an "under development, do not use" banner over
# `clip5pAdapterSeq` that the CellRanger4 carve-out made wrong years ago. Reading the manual would
# have produced a wrong knowledge base twice, so every value in these entries was settled by running
# the binary instead -- and that measurement then lived only in a dated research file. Nobody re-runs
# a document. These two tests are the same measurement, re-taken by CI's external lane on every pull
# request, so a STAR that changes what it accepts goes red here rather than on a compute node.
#
# They cost nothing. `--genomeDir` names a path that does not exist, and STAR validates its whole
# parameter set BEFORE it opens a genome, so a combination it accepts dies on the missing
# `genomeParameters.txt` and one it refuses dies on a parameter error instead. No index, no
# alignment, ~8 ms an invocation.


#: The verdict line a run reaches when every parameter was legal: it got as far as the genome that is
#: not there. Classified on that line ALONE and never on the whole output -- STAR echoes every
#: parameter it was handed back into stdout, so a naive match over the output finds the echo of the
#: flag rather than the judgement on it.
_STAR_REACHED_THE_GENOME = "could not open genome file"

#: ...and how STAR names the other outcome, on the same line.
_STAR_REFUSED_THE_PARAMETERS = "fatal PARAMETER error"

#: An emitted config key -> the STAR flag `map/starsolo` renders it as. Two of the three are the same
#: word, because a starsolo parse key IS its flag; `read_through` is the one deliberate rename, and it
#: is what the split between the entry and the module looks like: the chemistry states the sequence
#: once as a fact about the molecule, and each pipeline works out its own flag from it -- which is why
#: the plate module spells the same value at its cell's mate count and this one at a fixed 1.
_CLIP_FLAG_OF_KEY: dict[str, str] = {
    "clipAdapterType": "--clipAdapterType",
    "clip5pAdapterSeq": "--clip5pAdapterSeq",
    "read_through": "--clip3pAdapterSeq",
}


def _rendered_clip_flags(spec: kb.Spec) -> list[str]:
    """The clip argv one chemistry reaches STAR with, built from the sources the composer builds it from.

    Never a roster of chemistries and never a hand-typed flag string. The KB's own backend params and
    `derived_params` are the two halves `compose` merges into the block `rule starsolo_count`
    subscripts, so what goes in front of the binary below is what a run would go in front of it with,
    and a twelfth entry is covered because it exists rather than because someone remembered it here.
    """
    from seqforge.compose.params import derived_params

    params: dict[str, object] = {**spec.require_backend().params, **derived_params(spec)}
    flags: list[str] = []
    for key, flag in _CLIP_FLAG_OF_KEY.items():
        value = params.get(key)
        if value is not None:
            flags += [flag, str(value)]

    unclaimed = {k for k in params if "clip" in k.lower() or k == "read_through"} - set(
        _CLIP_FLAG_OF_KEY
    )
    assert not unclaimed, (
        f"{spec.identity.id} emits {sorted(unclaimed)}, which this sweep knows no STAR flag for, so "
        f"the binary is never asked about it and the entry rests on the help text again. A new clip "
        f"key belongs here with the flag it renders as"
    )
    return flags


def _star_parameter_verdict(star: str, workdir: Path, flags: Sequence[str]) -> str:
    """STAR's own one-line verdict on `flags`, taken at parameter initialization and no further.

    The two read files are what make the mate count real rather than assumed: STARsolo peels the
    barcode read off, so a two-file `--readFilesIn` is ONE mate and `--clip3pAdapterSeq` takes one
    value -- the arity `map/starsolo` renders at, and the reason a second value is a hard refusal even
    when it is `-`, STAR's own per-mate no-clip sentinel. `--soloType CB_UMI_Simple` with
    `--soloCBwhitelist None` is the cheapest way to reach that peel and is scaffolding: the clip
    validation consults the trimmer and the mate count and nothing else, and every clip flag under
    test comes from the caller.
    """
    cdna, barcode = workdir / "cdna.fastq", workdir / "barcode.fastq"
    for path in (cdna, barcode):
        path.write_text("@read\n" + "ACGT" * 10 + "\n+\n" + "I" * 40 + "\n")
    proc = subprocess.run(
        [star, "--genomeDir", str(workdir / "no-such-genome"),
         "--outFileNamePrefix", f"{workdir}/star_",
         "--readFilesIn", str(cdna), str(barcode),
         "--soloType", "CB_UMI_Simple", "--soloCBwhitelist", "None", *flags],
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    verdicts = [
        line for line in (proc.stdout + proc.stderr).splitlines() if "EXITING because of" in line
    ]
    assert verdicts, (
        f"STAR neither reached the genome nor refused the parameters, so nothing was measured:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    return verdicts[0]


@pytest.mark.external
def test_every_clip_flag_a_starsolo_chemistry_renders_is_one_the_pinned_star_accepts(
    tmp_path: Path,
) -> None:
    """The entries say which trimmer runs; only the binary can say the combination is legal.

    STAR's shipped help is wrong about this in both directions -- it advertises a `clipAdapterType`
    mode its code rejects, and it warns off a five-prime override its code supports beside
    `CellRanger4` -- so reading the manual would have produced a wrong knowledge base twice. What
    settled every value here was running the binary, and the record of that was a dated research
    file: true when written, re-run by nobody. This is that measurement as something CI takes again
    on every pull request.

    Derived, never enumerated. The chemistries come from the loader and the flags from the two
    sources the composer merges, so this covers the twelfth entry the day it lands. What it goes red
    for is a real thing the code can do: an entry pairing a trimmer with a clip at an end that trimmer
    will not take is refused at parameter initialization, BEFORE the genome loads, which is every
    sample of a deposit dying after its queue wait over a flag nobody typed.
    """
    star = shutil.which("STAR")
    if star is None:
        pytest.skip("needs STAR on PATH; the `test-star` environment carries the pinned build")

    ends_exercised: set[str] = set()
    for tech in kb.runnable_spec_ids():
        spec = kb.load_spec(tech)
        if spec.require_backend().module != "map/starsolo":
            continue
        flags = _rendered_clip_flags(spec)
        assert "--clipAdapterType" in flags, (
            f"{tech}: renders no trimmer at all, so the rule's subscript is a KeyError on a compute "
            f"node and this invocation would prove nothing about the chemistry"
        )
        ends_exercised |= {f for f in flags if f.startswith("--clip") and f != "--clipAdapterType"}

        verdict = _star_parameter_verdict(star, tmp_path, flags)
        assert _STAR_REACHED_THE_GENOME in verdict, (
            f"{tech}: STAR refuses the clip flags this chemistry renders -- {' '.join(flags)} -- so "
            f"every sample of a deposit on it would fail at parameter initialization, before a "
            f"genome is even opened. STAR said: {verdict}"
        )

    assert ends_exercised == {"--clip5pAdapterSeq", "--clip3pAdapterSeq"}, (
        f"the sweep put {sorted(ends_exercised)} in front of the binary; a knowledge base in which "
        f"no chemistry declares a clip asks STAR the one question it already answers by default, and "
        f"this test would then pass while measuring nothing"
    )


@pytest.mark.external
@pytest.mark.parametrize(
    ("flags", "refusal"),
    [
        pytest.param(
            ["--clipAdapterType", "CellRanger4", "--clip3pAdapterSeq", "CTGTCTCTTATACACATCT"],
            "uses fixed sequences",
            id="three-prime-clip-beside-the-trimmer-that-builds-its-own",
        ),
        pytest.param(
            [
                "--clipAdapterType",
                "Hamming",
                "--clip5pAdapterSeq",
                "AAGCAGTGGTATCAACGCAGAGTGAATGGG",
            ],
            "not supported yet",
            id="five-prime-override-beside-the-trimmer-that-takes-none",
        ),
        pytest.param(
            ["--clipAdapterType", "None"],
            "not a valid option",
            id="the-mode-the-shipped-help-still-advertises",
        ),
        pytest.param(
            ["--clipAdapterType", "Hamming", "--clip3pAdapterSeq", "AAAAAAAA", "-"],
            "match the number of mates",
            id="a-second-clip-value-for-a-mate-solo-already-peeled-off",
        ),
    ],
)
def test_the_clip_pairings_no_shipped_spec_can_produce_are_fatal_in_the_binary(
    tmp_path: Path, flags: list[str], refusal: str
) -> None:
    """What the load-time rule and the module's fixed arity are worth, priced by the aligner.

    An explicit table is the honest shape here and the derived sweep above is not, for one reason:
    no shipped entry can produce any of these. The schema refuses the first two at spec load -- a
    declared clip must sit at an end its declared trimmer takes -- and refuses the third as a trimmer
    it knows no end for, while the module renders the fourth structurally impossible by emitting one
    clip value and never a per-mate list. So the only way to ask STAR whether those rules are earning
    their place is to hand it the combinations by hand.

    Each row is a FATAL at parameter initialization, which is what makes the rules worth having
    rather than folklore: the run dies before the genome is opened, so a deposit produces no output
    at all and the failure names a flag nobody typed. The third row is the one the manual gets wrong
    -- `clipAdapterType None` is documented and does not exist -- and is why a value this schema does
    not recognise is refused outright rather than assumed harmless.

    The fourth is the arity claim the sweep above depends on. `--readFilesIn` hands the rule two
    files, solo peels the barcode read off, and one mate takes exactly one clip value: a second one
    is fatal even when it is `-`, STAR's own no-clip sentinel. `map/star-umi` renders the same flag at
    its cell's mate count, so the two modules reach opposite answers from one rule and the wrong one
    is a FATAL rather than a wrong number.
    """
    star = shutil.which("STAR")
    if star is None:
        pytest.skip("needs STAR on PATH; the `test-star` environment carries the pinned build")

    verdict = _star_parameter_verdict(star, tmp_path, flags)
    assert _STAR_REFUSED_THE_PARAMETERS in verdict and refusal in verdict, (
        f"STAR accepted {' '.join(flags)}, which nothing may ship and the knowledge base refuses at "
        f"load. Either the binary's rules moved -- in which case the schema's rule is now the wrong "
        f"shape -- or this row stopped asking the question. STAR said: {verdict}"
    )
