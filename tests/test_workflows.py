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
import math
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Any, get_args

import anndata as ad
import pysam
import pytest
import yaml
from genome.chimera import derive_separator, split_suffixed, suffixed
from scipy.sparse import csr_matrix

from conftest import (
    NO_STAR_ALIGNMENT_ON_MACOS,
    DryRun,
    SrcTrees,
    _build,
    _processing,
    _rendered_shell,
    _rule_blocks,
    _src_root,
    count_matrix,
    planned_paths,
    planning_route,
    star_modules,
    write_fastq_gz,
)
from seqforge import __version__ as seqforge_version
from seqforge import kb
from seqforge.compose import compose, core
from seqforge.models.dataset import ReadDef, ReadElement, ReadLayout
from seqforge.models.processing import RuntimeEnv, SoloFeature
from seqforge.workflows import (
    CHIMERIC_VARIANTS,
    PLATE_COMPONENT_H5AD,
    PLATE_H5AD,
    WORKFLOW_VERSION,
    argv_keys_read_by,
    get_module,
    keys_read_by,
    list_modules,
    memory,
)
from seqforge.workflows.cram import (
    _RENAME_QNAME,
    _SELECT_CAVEAT,
    CramError,
    RecordSelection,
    bam_to_cram,
)
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
    STAR_FINAL_LOG,
    STAR_JUNCTIONS,
    STAR_LOG_FILES,
    STAR_PROGRESS_LOGS,
    H5adError,
    h5ad_suffixes,
    raw_files,
    solo_filtered_files,
    solo_raw_files,
    solo_stats_files,
    write_h5ad,
)
from seqforge.workflows.memory import (
    BULK_RETRIES,
    STARSOLO_RETRIES,
    bam_sort_ram,
    bulk_mem_mb,
    escalated_mem_mb,
)
from seqforge.workflows.metrics import (
    MAX_KNEE_POINTS,
    SEVERITY_PHRASE,
    Decision,
    DecisionRef,
    Finding,
    Metric,
    SampleStats,
    Severity,
    fmt_count,
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
    build_plate_qc_bundle,
    build_qc_bundle,
    chemistry_rule,
    gene_model_rule,
    read_plate_metrics,
    read_star_log,
    solo_features_rule,
    write_plate_qc_bundle,
    write_qc_bundle,
)
from seqforge.workflows.qc import QC_SUFFIX as QC_BUNDLE_SUFFIX
from seqforge.workflows.qc import metrics as starsolo_metrics
from seqforge.workflows.qc import read_metrics as read_starsolo_metrics
from seqforge.workflows.split import (
    DROP_REASONS,
    SPLIT_SUFFIX,
    SplitError,
    SplitStats,
    split_chimera,
)
from seqforge.workflows.stats import (
    MODULES_WITHOUT_CROSS_CHECKS,
    MODULES_WITHOUT_STATS,
    PER_COMPONENT_CAVEAT,
    modules_with_cross_checks,
    modules_with_stats,
    read_pipeline_stats,
)
from seqforge.workflows.umite.count import (
    FATES,
    GENES_DETECTED,
    LAYERS,
    MULTIMAPPING_CAVEAT,
    MULTIMAPPING_HITS,
    MULTIMAPPING_LAYER,
    N_FRAGMENTS,
    N_UMIS,
    PRIMARY_MATRIX,
    SATURATION,
    UmiCountError,
    _step_index,
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
    EXTRACT_SUFFIX,
    TagGeometry,
    UmiExtractError,
    extract_metrics,
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

    WHICH of the four is which is asserted separately from the set, because the set cannot say: the
    junction table and the progress logs have opposite fates — bulk keeps the first as an output and
    sweeps the second two — and a constant holding the other one's filename would leave the union
    below byte-identical while a finished bulk run kept the wrong file.
    """
    assert set(STAR_LOG_FILES) == {"Log.final.out", "Log.out", "Log.progress.out", "SJ.out.tab"}
    assert STAR_JUNCTIONS == "SJ.out.tab"
    assert set(STAR_PROGRESS_LOGS) == {"Log.out", "Log.progress.out"}
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


# ---- the plate's own bundle: one artifact per CELL --------------------------------------------
#
# The second shape behind the same suffix and the same verb. A droplet sample and a plate cell leave
# genuinely different files behind, so the key spaces are two builders rather than one with optional
# keys — and what they share (the aligner's run files) is one private helper. The gates are the same
# two the droplet bundle's are: the key space is what the rule's inputs reduce to, and the reader
# beside the writer resolves every one of them.


#: One cell's split summary, in the shape the splitter writes it through — :class:`SplitStats` owns
#: the payload, so a renamed field there reddens whatever consumes this instead of passing.
_CELL_SPLIT: dict[str, object] = SplitStats(
    separator="__",
    records_in=33,
    kept={"tinyCe": 9, "tinyEc": 9},
    read1={"tinyCe": 5, "tinyEc": 5},
    read2={"tinyCe": 4, "tinyEc": 4},
    multiplaced={"tinyCe": 2, "tinyEc": 1},
    singletons={"tinyCe": 1, "tinyEc": 1},
    mate_pointed={"tinyCe": 0, "tinyEc": 2},
    excess_pointers=0,
    unanswered_survivors=0,
    multiplaced_singletons=0,
    dropped={"unmapped": 7, "secondary": 4, "supplementary": 4},
).to_dict()


def _finished_cell(
    run_dir: Path, *, sample: str = "cell_a", split: Mapping[str, object] | None = None
) -> tuple[Path, Path, Path | None]:
    """One plate cell's directory as its own pipeline leaves it, and the paths the bundle rule names.

    Returns ``(run_dir, extract summary, split summary or None)``. Every file in it is one a twin's
    rule declares — the extraction summary, the aligner's four run files, and on the chimeric arm the
    split's account — so the bundle built from this is the artifact those rules produce rather than a
    shape invented here.
    """
    extract = run_dir / f"{sample}{EXTRACT_SUFFIX}"
    _write(
        extract,
        json.dumps(
            {
                "sample": sample,
                "geometry": _PLATE_GEOMETRY,
                "fragments": 100,
                "tagged": 27,
                "untagged": 73,
                "offsets": {"0": 26, "13": 1},
            }
        ),
    )
    _write(run_dir / STAR_FINAL_LOG, "".join(f"  {k} |\t{v}\n" for k, v in _HEALTHY_LOG.items()))
    for name in STAR_PROGRESS_LOGS:
        _write(run_dir / name, f"{name}: STAR version 2.7.11b\n")
    # Three junctions: one annotated and canonical, one novel and non-canonical, one annotated on
    # the other canonical motif — enough for every counter in the summary to differ from the row
    # count, which is what makes the numbers below a measurement rather than three ways to say 3.
    _write(
        run_dir / STAR_JUNCTIONS,
        "chrI\t100\t200\t1\t1\t1\t10\t2\t30\n"
        "chrI\t500\t600\t2\t0\t0\t3\t0\t18\n"
        "chrII\t900\t950\t1\t2\t1\t7\t1\t22\n",
    )
    summary = None
    if split is not None:
        summary = run_dir / f"{sample}{SPLIT_SUFFIX}"
        _write(summary, json.dumps(split))
    return run_dir, extract, summary


def _bundled_cell(
    results: Path, staging: Path, sample: str, *, split: Mapping[str, object] | None = None
) -> Path:
    """Land one cell's QC bundle under ``<results>/<sample>/``, built by the REAL writer.

    The originals it absorbs are staged somewhere ELSE on purpose: a finished run has swept every one
    of them, so a results directory still holding them is a state the pipeline cannot leave behind —
    and a reader tested against it could be finding the originals rather than the bundle.
    """
    run_dir, extract, split_summary = _finished_cell(staging / sample, sample=sample, split=split)
    return write_plate_qc_bundle(
        run_dir,
        results / sample / f"{sample}{QC_BUNDLE_SUFFIX}",
        sample=sample,
        assembly="sacCer3",
        extract=extract,
        split=split_summary,
    )


@pytest.mark.parametrize("chimeric", [False, True], ids=["plain", "chimeric"])
def test_the_plate_bundle_absorbs_the_originals_and_summarizes_the_junctions(
    tmp_path: Path, chimeric: bool
) -> None:
    """One artifact per cell, and the key space is the claim about what is in it.

    A plate cell used to leave four files and a chimeric one five, beside a junction table nobody can
    analyze at one cell's depth. This is what absorbs them, and every original becomes reclaimable
    only because this carries it — so what a key is called is the artifact's format, and a key that
    stops being written costs the page a column with nothing raising.

    **The junctions arrive as a SUMMARY**, which is the one deliberate asymmetry between the two
    shapes behind this suffix: a parsed table per sample is small change for ten droplet samples and
    roughly a gigabyte across a 784-cell plate, for a file nothing downstream reads. The counts still
    answer what the table was there for — how much splicing the cell showed, and how much of it the
    annotation already knew.

    **`split` is ABSENT on a plain plate rather than empty.** There was no split; an absent key and a
    zero are different claims, and only one of them is a measurement.
    """
    run_dir, extract, split = _finished_cell(
        tmp_path / "results" / "cell_a", split=_CELL_SPLIT if chimeric else None
    )

    bundle = build_plate_qc_bundle(
        run_dir, sample="cell_a", assembly="ce11_ecHT115", extract=extract, split=split
    )

    assert set(bundle) == {
        "sample",
        "assembly",
        "extract",
        "log_final",
        "log_out",
        "log_progress",
        "splice_junction_summary",
    } | ({"split"} if chimeric else set())
    assert (bundle["sample"], bundle["assembly"]) == ("cell_a", "ce11_ecHT115")
    # Each absorbed summary is folded in VERBATIM, so the code that wrote it stays the only code
    # that knows what its keys mean — one owner per artifact, at one remove.
    assert bundle["extract"] == json.loads(extract.read_text())
    assert split is None or bundle["split"] == json.loads(split.read_text())
    # The aligner's end-of-run log, parsed the one way both bundles carry it (the shared helper).
    log_final = bundle["log_final"]
    assert isinstance(log_final, dict) and log_final["Uniquely mapped reads %"] == "88.42%"
    assert "STAR version" in str(bundle["log_out"])
    # ...and the junctions as counts, never as rows.
    assert bundle["splice_junction_summary"] == {
        "junctions": 3,
        "annotated": 2,
        "canonical": 2,
        "unique_reads": 20,
        "multi_reads": 3,
    }

    # A file the pipeline was supposed to write and did not is a refusal here, exactly as it is for
    # the droplet bundle: once the originals are reclaimed this is the only surviving record, so a
    # bundle silently missing a chapter is worse than a job that failed.
    (run_dir / STAR_JUNCTIONS).unlink()
    with pytest.raises(QcError, match=re.escape(STAR_JUNCTIONS)):
        build_plate_qc_bundle(
            run_dir, sample="cell_a", assembly="ce11_ecHT115", extract=extract, split=split
        )


def test_the_plate_bundle_the_writer_produces_is_the_one_its_reader_looks_up(
    tmp_path: Path,
) -> None:
    """`build_plate_qc_bundle` decides the keys and `plate_metrics` looks them up — through the writer.

    The droplet pair's contract, one artifact shape over, and it holds for the same reason: writer and
    reader are in one file precisely so they cannot drift, and a reader driven from a hand-written
    dict would keep resolving against the test's own dict while the page silently lost a column.

    The chimeric arm, because it is the wider key space and contains the plain one. Every column a
    cell's row can carry is pinned here — what the extraction saw, what the aligner did, and what left
    at the split — since those are exactly the lookups that stop resolving on a rename.
    """
    run_dir, extract, split = _finished_cell(tmp_path / "results" / "cell_a", split=_CELL_SPLIT)

    out = write_plate_qc_bundle(
        run_dir,
        tmp_path / f"cell_a{QC_BUNDLE_SUFFIX}",
        sample="cell_a",
        assembly="ce11_ecHT115",
        extract=extract,
        split=split,
    )
    sample = read_plate_metrics(out, "cell_a")
    got = _by_key(sample)

    assert set(got) == (
        {"extract_fragments", "umi_tagged", "umi_anchor_drift"}
        | {
            "input_reads",
            "input_read_length",
            "uniquely_mapped",
            "multi_loci",
            "too_many_loci",
            "unmapped_too_short",
        }
        | {
            f"split_{account}_{component}"
            for account in ("kept", "share", "multiplaced", "singletons", "mate_pointed")
            for component in ("tinyCe", "tinyEc")
        }
        # Cell-wide and not per Component, because the bound they measure the slack in has no
        # Component either — and they are two signs of one difference, so a cell shows a number in
        # at most one of them.
        | {"split_excess_pointers", "split_unanswered_survivors"}
        | {f"split_dropped_{reason}" for reason in DROP_REASONS}
    )
    assert got["extract_fragments"].value == 100
    assert got["umi_tagged"].value == pytest.approx(0.27)
    assert got["uniquely_mapped"].value == pytest.approx(0.8842)  # "88.42%" from the text log
    assert got["split_kept_tinyEc"].value == 9
    assert got["split_dropped_unmapped"].value == 7
    # Columns read in pipeline order, which is the order a reader walks a cell in: what the FASTQs
    # held, what the aligner then did with it, and what left at the split that follows.
    keys = [m.key for m in sample.metrics]
    assert (
        keys.index("umi_tagged")
        < keys.index("uniquely_mapped")
        < keys.index("split_dropped_unmapped")
    )


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
    # Primary alignments only, header kept (the encoder needs the @SQ lines), multi-threaded. The
    # default selection is the one the shipped rules invoke, so its argv is what "nothing on disk
    # changes" means: today's flag, and no tag expression beside it.
    primary = next(c for c in rec.calls if "-F" in c)
    assert primary[:2] == ["samtools", "view"] and "-h" in primary
    assert primary[primary.index("-F") + 1] == "0x100"
    assert "-e" not in primary
    assert primary[primary.index("--threads") + 1] == "8"
    # The read names are rewritten in the stream: `awk`, tab-delimited in AND out, headers passed
    # through untouched, and the new QNAME is a counter.
    rename = next(c for c in rec.calls if c[0] == "awk")
    assert r'FS=OFS="\t"' in rename[1]
    assert "/^@/" in rename[1] and '$1="r"' in rename[1]
    # The CRAM is indexed.
    assert any(c[:3] == ["samtools", "index", "-@"] for c in rec.calls)


@pytest.mark.external
@pytest.mark.skipif(shutil.which("awk") is None, reason="awk not on PATH")
def test_the_read_name_rewrite_carries_every_header_line_into_the_retained_cram(
    tmp_path: Path,
) -> None:
    """The converter inherits the read group rather than learning what one is — RUN, not read.

    The plate route declares `@RG` at the aligner, and the CRAM is the RETAINED artifact, so the
    header line has to survive the one stage between them that rewrites bytes: the QNAME rename. That
    it does is a property of the rename program, and the test beside this one can only say the
    program CONTAINS a header branch — which is a claim about source text, and would pass just as
    happily if the branch printed the line into the wrong stream or dropped the tab layout.

    So the program is executed over a SAM holding the two lines that matter, and the claim is what
    comes out: `@RG` verbatim, tabs and all, and the alignment beneath it renamed. That is also why
    the converter needs no edit for the read group and must not get one — a stage that special-cased
    `@RG` would be a second owner of a fact the aligner already states.

    The same stage is where a selection's caveat becomes a `@CO` line, so the second run is the
    other half of the same claim: a caveat lands at the END of the header — after every line the
    aligner wrote, so `@HD` stays first and no `@SQ` moves, and before the first alignment, so it is
    header and not a malformed record — while no caveat leaves the stream exactly as it was, which
    is what keeps the default archive's bytes the bytes it always had.
    """
    sam = tmp_path / "one.sam"
    sam.write_text(
        "@HD\tVN:1.6\tSO:coordinate\n"
        "@SQ\tSN:chrI\tLN:1000\n"
        "@RG\tID:cell_a\tSM:cell_a\n"
        "A00234:HHV:1:1101:1:1\t0\tchrI\t1\t255\t4M\t*\t0\t0\tACGT\tIIII\tRG:Z:cell_a\n"
    )
    out = subprocess.run(
        ["awk", _RENAME_QNAME, str(sam)], capture_output=True, text=True, check=True
    ).stdout.splitlines()

    assert "@RG\tID:cell_a\tSM:cell_a" in out, (
        "the retained CRAM would carry records naming a read group its header never introduced"
    )
    assert out[:2] == ["@HD\tVN:1.6\tSO:coordinate", "@SQ\tSN:chrI\tLN:1000"]
    # ...and the rename it exists for still happens, on the alignment and on nothing above it.
    assert out[3].split("\t")[0] == "r1" and out[3].endswith("RG:Z:cell_a")
    assert not any(line.startswith("@CO") for line in out), out

    stamped = subprocess.run(
        ["awk", "-v", "caveat=one of many loci", _RENAME_QNAME, str(sam)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert stamped[:3] == out[:3], stamped  # nothing the aligner wrote moves or is displaced
    assert stamped[3] == "@CO\tone of many loci", stamped
    assert stamped[4].split("\t")[0] == "r1", stamped


@pytest.mark.external
@pytest.mark.parametrize(
    ("selection", "kept"),
    [
        (RecordSelection.primary, 3),
        (RecordSelection.mapped, 2),
        (RecordSelection.unique, 1),
        (RecordSelection.multi, 1),
    ],
    ids=["primary", "mapped", "unique", "multi"],
)
def test_each_record_selection_keeps_exactly_the_records_it_names(
    selection: RecordSelection, kept: int, tmp_path: Path
) -> None:
    """Four records — one of each kind the aligner writes — cut four ways by a REAL samtools.

    A selection is a flag filter and a tag expression together, and this is the fixture that shows
    why neither half can be dropped: the record that never aligned carries `NH:i:1` exactly like the
    uniquely placed one, so `[NH]==1` alone keeps it and only the flag half can say a record aligned
    at all. Asserting the argv instead would pass just as happily on a table that said `-F 0x4` or
    `[NH]<2`, so the claim here is the count that comes back OUT of the archive, read with the
    binary the rules actually run.

    The counts are the partition claim at the scale it can be checked: `unique` plus `multi` is
    exactly `mapped`, with nothing in both — so the two archives the plate modules write together
    hold what one mixed archive holds and duplicate no bytes. And `primary`, the default, still
    keeps the record that never aligned, which is the behaviour nothing on disk may lose here.

    The caveat is checked on the same artifact, because that is the point of it: a record in the
    multiply-placed archive means one of the fragment's possible loci is here and never that the
    fragment belongs here, and a reader who copied the file somewhere else has only the file. So it
    is read back OUT of the CRAM header rather than off the argv, and every other selection's
    archive has to be free of it — a caveat on the uniquely-placed half would be saying something
    untrue about it.
    """
    samtools = shutil.which("samtools")
    if samtools is None or shutil.which("awk") is None:
        pytest.skip("needs samtools and awk on PATH")

    fasta = tmp_path / "ref.fa"
    fasta.write_text(">chrI\n" + "ACGT" * 50 + "\n")
    subprocess.run([samtools, "faidx", str(fasta)], check=True, capture_output=True)
    sam = tmp_path / "four.sam"
    sam.write_text(
        "@HD\tVN:1.6\tSO:coordinate\n"
        "@SQ\tSN:chrI\tLN:200\n"
        # Uniquely placed, multiply placed, that one's secondary, never aligned — all carrying NH.
        "u1\t0\tchrI\t1\t255\t4M\t*\t0\t0\tACGT\tIIII\tNH:i:1\n"
        "m1\t0\tchrI\t11\t3\t4M\t*\t0\t0\tACGT\tIIII\tNH:i:2\n"
        "m1\t256\tchrI\t21\t3\t4M\t*\t0\t0\tACGT\tIIII\tNH:i:2\n"
        "n1\t4\t*\t0\t0\t*\t*\t0\t0\tACGT\tIIII\tNH:i:1\n"
    )
    bam = tmp_path / STAR_BAM
    subprocess.run(
        [samtools, "view", "-b", "-o", str(bam), str(sam)], check=True, capture_output=True
    )

    out = tmp_path / "S1" / "S1.cram"
    bam_to_cram(bam, fasta, out, selection=selection)

    counted = subprocess.run(
        [samtools, "view", "-c", "-T", str(fasta), str(out)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert int(counted) == kept

    header = subprocess.run(
        [samtools, "view", "-H", "-T", str(fasta), str(out)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    caveat = _SELECT_CAVEAT.get(selection)
    if caveat is None:
        assert "@CO" not in header, header
    else:
        assert f"@CO\t{caveat}" in header, header


@pytest.mark.parametrize(
    ("fails", "named", "selection"),
    [
        ("-F", "primary records", RecordSelection.primary),
        ("awk", "read-name rewrite", RecordSelection.primary),
        ("-C", "CRAM encode", RecordSelection.primary),
        ("[NH]>1", "multi records", RecordSelection.multi),
    ],
    ids=["primary-filter", "read-name-rewrite", "cram-encoder", "tag-expression"],
)
def test_a_failure_in_any_stage_of_the_pipe_is_a_cram_error_that_names_it(
    fails: str,
    named: str,
    selection: RecordSelection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipe reports the exit status of its LAST stage, so two of these three would pass silently.

    `samtools view -C` happily encodes a truncated stream and exits 0, which is how a filter or a
    rewrite that died mid-file becomes a CRAM missing most of its reads — the silent-plausible-wrong
    class again, and expensive here because the BAM it came from is a `temp()` output that snakemake
    deletes the moment this rule succeeds. So each stage is waited on and named.

    The last row is the selecting stage again with a selection that carries a tag expression, and it
    is here because a malformed expression is the one new way that stage can die: samtools exits
    non-zero on one rather than matching nothing, so the archive a wrong expression produces is a
    named refusal and never a short file. The token it fails on is the expression itself, which no
    other selection's argv contains — so the row also proves the expression reached samtools at all,
    and that the error says WHICH selection was being cut.
    """
    _stub_samtools(monkeypatch, fails=fails)
    bam = tmp_path / STAR_BAM
    bam.write_bytes(b"BAM\0")
    fasta = tmp_path / "ref.fa"
    fasta.write_text(">chr\nACGT\n")
    (tmp_path / "ref.fa.fai").write_text("")

    out = tmp_path / "S1" / "S1.cram"
    with pytest.raises(CramError, match=named):
        bam_to_cram(bam, fasta, out, threads=2, selection=selection)


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

    assert set(list_modules()) == {
        "map/starsolo",
        "map/star",
        "map/chromap",
        "map/star-umi",
        "map/star-umi-chimera",
    }
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

    # The fan-in declaration, by NAME in both directions. Two shipped modules produce a deliverable
    # the sample axis does not reach — the plate pipeline and its chimeric twin, which is the same
    # pipeline one arity out — and the other three are untouched by either's arrival, which is the
    # whole of why the field defaults to absent. A set comparison rather than a count: "some module
    # aggregates" was never the claim.
    assert {n for n in list_modules() if get_module(n).fan_in_artifact is not None} == {
        "map/star-umi",
        "map/star-umi-chimera",
    }
    assert get_module("map/star-umi").fan_in_artifact == PLATE_H5AD
    # The twin's carries a `{component}` and the base's does not, which is the arity difference
    # itself: one object for the deposit against one per Component of the chimera it mapped to.
    assert get_module("map/star-umi-chimera").fan_in_artifact == PLATE_COMPONENT_H5AD
    assert "{component}" in PLATE_COMPONENT_H5AD and "{component}" not in PLATE_H5AD

    # The twin is reachable ONLY through its base's declaration, and the guard set that keeps it that
    # way is DERIVED from the registry rather than typed beside it — a second list is one a new twin
    # would be missing from, and the KB refusal would then pass while guarding nothing.
    # By NAME, and only by name. Restating the comprehension that defines the set would assert the
    # source line back at itself: it cannot go red for anything the code can do, and it would pass
    # unchanged on the day a twin went missing from the registry.
    assert CHIMERIC_VARIANTS == {"map/star-umi-chimera"}
    assert get_module("map/star-umi-chimera").chimeric_variant is None, (
        "a twin of a twin has no meaning and nothing would ever select it"
    )

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

    **A chimera-aware twin is reached by no spec and never will be**, which is a different fact from
    "not yet": no KB backend may name a twin, and one that tries is refused at load. So it is planned
    the way compose will really reach it — its BASE's chemistry under a two-part assembly name
    (:func:`~conftest.planning_route`) — rather than being written into the skip set, where it would
    have lost the ``snakemake -n`` coverage every other module has, permanently and quietly. The
    route costs nothing on disk: chimera detection is syntactic and the genome resolves inside a
    ``run:`` block, so this plans a whole chimeric plate with no built reference anywhere.

    It also owns "the gate leaves no zero-byte FASTQ behind", which used to be a second 1.5s spawn of
    its own on `map/starsolo`. The gate stands in zero-byte FASTQs; they were touched straight into
    the run directory (`pipeline_dir / row["path"]`) and never removed, which was invisible only
    because the gate never ran — `snakemake` was undeclared. The moment it ran, the run directory
    would hold zero-byte files named exactly like the FASTQs, STAR would read them, and the pipeline
    would emit an empty matrix and **exit 0**: silent, plausible, wrong, and introduced by the very
    commit that made the gate work. Asserted here it holds for all three modules, not for starsolo
    alone.
    """
    if module in MODULES_NO_SPEC_REACHES_YET:
        assert not [
            t for t in kb.runnable_spec_ids() if kb.load_spec(t).require_backend().module == module
        ], (
            f"{module} is named as reached by no spec, but a spec reaches it — drop it from "
            f"MODULES_NO_SPEC_REACHES_YET so the composed-pipeline gate covers it"
        )
        pytest.skip(
            f"{module} has no chemistry yet; its .smk is planned from a hand-written config"
        )
    tech, assembly = planning_route(module)
    manifest, reg = _build(tmp_path, tech)
    result = compose(
        manifest, _processing(manifest, assembly=assembly), registry=reg, workspace=tmp_path
    )
    # ...and it composed to the module this case is about. For a twin that is the whole claim of the
    # route above: the chemistry names the base, and the assembly is what swapped it.
    assert result.modules[0].name == module
    # PASS, not skip. This used to read `in {"pass", "skip"}` and so forbade only the one value that
    # could not occur: `snakemake` was in no dependency table, `have("snakemake")` was False, and the
    # gate returned "skip" every time. A skip is green, so the gate was decorative for the life of the
    # repo. If it ever goes missing again, that is a broken environment and this says so.
    assert result.gate["wiring"].status == "pass", result.gate["wiring"].reason
    run_dir = (tmp_path / result.snakefile_path).parent
    strays = [p for p in run_dir.rglob("*") if p.suffix == ".gz" and p.stat().st_size == 0]
    assert not strays, f"the gate left zero-byte stand-ins in the run dir: {strays}"


@pytest.mark.parametrize("module", star_modules())
def test_every_star_workflow_shares_one_genome_copy_and_declares_the_read_group_it_stamps(
    module: str, tmp_path: Path, dry_run: DryRun
) -> None:
    """Three invariants every STAR workflow owes, read off ONE rendered plan.

    STAR's index is per-process and resident for the life of the job, so N mapping jobs running at
    once on one machine cost N copies of it. A composed pipeline runs on ONE machine (ADR-0051),
    which is what makes a single shared segment reachable by every job — and is also what makes the
    multiplication real, since the jobs of a run are concurrent by construction. `map/star-umi` has
    shared a copy since 2026.8.6; the other two loaded a private copy per job until #379.

    The read group is the second invariant and it rides here rather than in a neighbour of its own
    because the plan is the expensive part: this test already composes and dry-runs every STAR
    workflow there is, and a second sweep would spawn `snakemake` four more times to read the same
    text. Both claims are about what a mapping job's command line SAYS, and neither can be made
    anywhere else — a `shell:` literal is source, and a source claim about a command is not a claim
    about a command.

    The scratch is the third, and it rides here for the same reason: what the load rule hands STAR
    as an output prefix is a fact about a command, and the run-files STAR leaves under that prefix
    are the difference between a finished pipeline directory a reader can sort into output and
    scratch and one where they cannot.

    Seven claims, and a dry run is the only thing that can make any of them:

    1. **The load is a job.** A rule unreachable from `rule all` plans nothing, and this one is
       reachable only through the mapping rule's inputs — there is no target naming it.
    2. **Mapping WAITS on it**, which is a dependency edge and appears in no rendered command. Read
       off the load rule's own planned output rather than a path rebuilt here, so a module that moved
       the flag cannot leave this test asserting against a filename nothing produces.
    3. **The mapping invocation ATTACHES** (`--genomeLoad LoadAndKeep`). This is a source claim
       everywhere else — a literal in a shell block for two workflows, a constant in
       `workflows/starsolo_args.py` for the third — and a source claim about a command is not a claim
       about a command.
    4. **The sort cap is stated explicitly.** Under a shared copy STAR REFUSES its own default of
       `0` ("reuse the genome allocation"), and the refusal fires before the genome directory is
       read: every sample on the first attempt, not a degradation. `\\d+` rather than a byte count —
       the exact figures are pinned per workflow by the compose tests that know each one's `mem_mb`;
       what this owns is that some number is there for every STAR workflow, including the next one.
    5. **The flag is not `temp()`.** Snakemake announces every temporary output it would delete, so
       the plan is where "this file survives the run" is legible. Delete it and a rerun is told the
       load never happened, and reloads a segment that is already resident.
    6. **A uBAM-fed alignment declares the read group it stamps** (#416), with the id and the sample
       name being the job's own wildcard. `--outSAMattrRGline` is STAR's ONLY input to an `@RG`
       header line and setting it is also what puts `RG` on a record, so one flag carries both halves
       of the SAM rule that a record's `RG` name a group its header introduced.

       **Which modules owe it is DERIVED from the rendered command, not from a list here**: a route
       that renders `--readFilesSAMattrKeep` is one handing STAR an alignment file whose records
       already carry tags, and that is exactly the route whose records arrive carrying `RG:Z:` from
       the extractor. A FASTQ-fed route hands STAR no input tags, so its records carry no `RG` to
       dangle and its files are not malformed — giving those a read group is a usability improvement
       and not this defect, so it is deliberately absent and this test says nothing about them.

       Asserted per JOB rather than per module, because the value is the whole claim: a flag
       rendering one cell's id onto every cell's records would satisfy any test that only asked
       whether the flag was there.

    7. **Both load invocations write their run-files outside the deliverable**, under a directory the
       block creates and destroys. STAR writes a log, a progress log and a `_STARtmp/` under every
       prefix it is given and cleans up none of it, so the prefix these two used to carry left nine
       undeclared entries beside the index and the flag. The prefix is an argument on a rendered
       command line, which is the only place this is checkable at all, and reading it here is what
       makes the claim cover a fourth workflow the day it ships.

    Parametrized over :func:`~conftest.star_modules`, DERIVED from the registry: the lifecycle is
    copied into each workflow file rather than factored out, so a fourth STAR workflow must be
    covered the day it ships rather than the day someone remembers to add it. `map/chromap` is absent
    because it invokes `chromap`, not because anything here exempts it.

    A **chimera-aware twin** is picked up by that selector and then cannot be planned the way the
    others are, because no KB spec may name one — so it is planned through
    :func:`~conftest.planning_route`, which is its base's chemistry under a two-part assembly name.
    That is not an exemption: the twin carries a fourth copy of this lifecycle and owes every claim
    below, and the route is what lets it pay them with no fixture and nothing on disk.
    """
    tech, assembly = planning_route(module)
    manifest, reg = _build(tmp_path, tech)
    processing = _processing(manifest, assembly=assembly)
    result = compose(manifest, processing, registry=reg, workspace=tmp_path)
    assert result.modules[0].name == module
    pipeline_dir = (tmp_path / result.snakefile_path).parent

    plan_text = dry_run(pipeline_dir, core.plan(manifest, processing, registry=reg))
    rendered = _rendered_shell(plan_text)

    assert list(rendered.get("load_genome", {})) == [""], (
        f"{module} plans no single `load_genome` job, so every mapping job loads its own index:\n"
        f"{sorted(rendered)}"
    )
    load = rendered["load_genome"][""]
    assert load.index("--genomeLoad Remove") < load.index("--genomeLoad LoadAndExit"), load
    assert "|| true" in load, "removing a segment that is not there is a STAR error and a no-op"

    # ...and NEITHER invocation writes its run-files into the pipeline directory. STAR drops a log, a
    # progress log and a `_STARtmp/` under every prefix it is handed and removes none of them, so a
    # prefix under `results/` left nine undeclared entries beside this rule's two real outputs. Both
    # prefixes must therefore name a directory the block itself creates and destroys: a prefix
    # pointed somewhere safer, or a glob sweep afterwards, is a mechanism that has to stay configured
    # correctly, and this one cannot leak whatever a future STAR decides to write.
    scratch = re.search(r"(\w+)=\$\(mktemp -d\)", load)
    assert scratch, f"{module}'s genome load writes under a prefix it did not create:\n{load}"
    made = f'"${scratch.group(1)}"'
    prefixes = re.findall(r"--outFileNamePrefix (\S+)", load)
    assert len(prefixes) == 2 and all(p.startswith(made) for p in prefixes), (
        f"{module}'s genome load leaves STAR run-files inside the deliverable: {prefixes}"
    )
    assert f"rm -rf {made}" in load, (
        f"{module}'s genome load makes an aligner scratch directory that outlives the rule:\n{load}"
    )

    # WHICH rule maps is read off the rendered commands too, so this never has to name a rule per
    # module: the mapping rule is the one whose command runs STAR's aligner.
    mapping = {
        rule: jobs
        for rule, jobs in rendered.items()
        if any("--runMode alignReads" in cmd for cmd in jobs.values())
    }
    assert len(mapping) == 1, f"expected one aligning rule in {module}, got {sorted(mapping)}"
    jobs = next(iter(mapping.values()))
    assert all("--genomeLoad LoadAndKeep" in cmd for cmd in jobs.values()), jobs
    assert all(re.search(r"--limitBAMsortRAM \d+", cmd) for cmd in jobs.values()), jobs

    # The read group, per job, against the job's OWN wildcard -- which is the cell on the routes that
    # owe it, and is the value the retained CRAM will name. A module that rendered the flag from a
    # constant, or from the first cell of the plate, is what this catches and a presence check would
    # not. Owed by the uBAM-fed routes and read off the command rather than off a list: keeping input
    # tags at all IS the property that makes a record arrive carrying `RG:Z:`.
    for wildcard, cmd in jobs.items():
        if "--readFilesSAMattrKeep" not in cmd:
            continue
        assert f"--outSAMattrRGline ID:{wildcard} SM:{wildcard}" in cmd, (
            f"{module} maps {wildcard} from a uBAM whose records carry the extractor's `RG:Z:` and "
            f"declares no read group, so the retained alignment names a group its header never "
            f"introduced:\n{cmd}"
        )
    # ...and the input tags such a route keeps may not include `RG`, because STAR appends the kept
    # input tags after writing its own and de-duplicates against nothing: `All` beside the flag above
    # puts `RG` on a record twice, which is worse than the dangling tag it replaces. A FASTQ-fed
    # module renders no keep list at all, and this says nothing about one.
    for cmd in jobs.values():
        kept = re.search(r"--readFilesSAMattrKeep ((?:\w+ )*\w+)", cmd)
        assert kept is None or "RG" not in kept.group(1).split(), (
            f"{module} keeps the input `RG` while declaring its own, so every record carries the "
            f"tag twice:\n{cmd}"
        )

    flag = planned_paths(plan_text, "output")["load_genome"]
    assert flag <= planned_paths(plan_text, "input")[next(iter(mapping))], (
        f"{module}'s mapping jobs do not depend on {flag}, so they race the load rather than "
        f"attaching to a copy that exists"
    )
    # ...and the flag SURVIVES the run. `temp()` here would tell a rerun that the load never
    # happened, and it would reload a segment that is already resident.
    assert not [path for path in flag if f"Would remove temporary output {path}" in plan_text], (
        f"{module} declares its load flag `temp()`:\n"
        f"{[ln for ln in plan_text.splitlines() if ln.startswith('Would remove')]}"
    )


#: One plate's worth of hand-written config: the three cells, the geometry compose would derive, and
#: the two roles it would place by ROLE. Enough to plan the module and nothing more.
_PLATE_GEOMETRY = "R1:ATTGCGCAATG@0:umi@11+8:GGG@19:cdna@22"


def _plate_run_dir(
    directory: Path,
    samples: Sequence[str],
    *,
    mate: bool | None = True,
    read_through: str | None = None,
    components: Sequence[str] | None = None,
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

    ``components`` writes the directory for the CHIMERIC TWIN instead — a chimeric assembly name, the
    one extra config key compose emits for such a run, and the twin's own ``.smk``. One flag rather
    than a second copy of this function, for the reason ``mate`` is one: the two directories differ
    in exactly the fact the two modules differ in, and two copies would be free to drift on the eight
    keys that are not the subject. The Components are the caller's, not derived from the name: what
    compose reads off a name is compose's claim and is asserted where compose is.
    """
    module = get_module("map/star-umi" if components is None else "map/star-umi-chimera")
    config: dict[str, object] = {
        "container": "docker://example/align-rna",
        "genome": {"assembly": "sacCer3", "annotation": "ensembl_R64-1-1"}
        if components is None
        else {
            "assembly": "_".join(components),
            "annotation": "merged_R64-1-1",
            "components": list(components),
        },
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

    for rule in (
        "load_genome",
        "umi_extract",
        "star_umi_map",
        "unique_to_cram",
        "multiplaced_to_cram",
        "qc_bundle",
        "umi_count",
    ):
        assert rule in plan, f"the plan never reaches `{rule}`:\n{plan}"
    # The fan-in is ONE job over three cells, while the per-cell chain is one job each. That ratio is
    # the module's whole shape, and a per-cell counter followed by a merge would read as three here.
    assert re.search(r"^umi_extract\s+3\s*$", plan, re.M), plan
    assert re.search(r"^star_umi_map\s+3\s*$", plan, re.M), plan
    assert re.search(r"^qc_bundle\s+3\s*$", plan, re.M), plan  # ONE QC artifact per CELL
    assert re.search(r"^umi_count\s+1\s*$", plan, re.M), plan
    assert re.search(r"^load_genome\s+1\s*$", plan, re.M), plan
    # The deliverables `rule all` demands, by name: one object for the plate, BOTH halves of every
    # cell's archive, and every cell's QC bundle. None of them is anyone's input, so one demanded by
    # nothing would simply stop being produced — and one mixed archive per cell is what the two
    # halves replace, which is why the name this asserted before must no longer be planned at all.
    assert f"results/{PLATE_H5AD}" in plan
    cells = ("cell_a", "cell_b", "cell_c")
    assert all(f"results/{s}/{s}.unique.cram" in plan for s in cells), plan
    assert all(f"results/{s}/{s}.multiplaced.cram" in plan for s in cells), plan
    assert all(f"results/{s}/{s}{QC_BUNDLE_SUFFIX}" in plan for s in cells), plan
    assert not re.search(r"results/cell_a/cell_a\.cram\b", plan), plan
    # The shared-memory contract, rendered rather than merely written: the load rule marks any stale
    # segment for destruction before loading, and every mapping job attaches instead of loading.
    assert "--genomeLoad Remove" in plan and "--genomeLoad LoadAndExit" in plan
    assert "--genomeLoad LoadAndKeep" in plan
    # The fragments that never aligned are IN the aligner's output. Without this the counter's first
    # fate is not a small number, it is an unreachable branch: the counter measures what the BAM
    # holds, so every plate object carried an unmapped column that was structurally zero. `Within`
    # and never `Within KeepPairs` — the second token only orders an unmapped record beside its mate
    # in UNSORTED output, and this module writes sorted output, so it would claim an intent the
    # module does not have.
    assert "--outSAMunmapped Within" in plan
    assert "KeepPairs" not in plan, plan
    # ...and the geometry the extractor is handed is the ONE derived value, not six numbers.
    assert f"--geometry {_PLATE_GEOMETRY}" in plan
    # The paired half of what this layout decides, and its mirror is the test below. The extraction
    # renders NO file at all now (ADR-0036) — the table and the cell, one argument each — so the
    # half that still varies here is the aligner's, and the pair that says "paired" here has to be
    # the pair that says "single" there.
    assert re.search(r"--units \S*units\.tsv --sample \S+", plan), plan
    assert not re.search(r"--r1\b|--r2\b", plan), plan
    assert "--readFilesType SAM PE" in plan
    # The fan-in SPENDS the threads it asks the scheduler for. It requested them and handed the verb
    # none of them, so the one job that runs after every cell has finished counted a whole plate on
    # one core of an allocation it was holding whole. Read off the rendered command, because a rule
    # whose `threads:` and whose command line disagree is exactly what that looked like.
    rendered = _rendered_shell(plan)
    assert "--threads 4" in rendered["umi_count"][""]
    # ...and the archive is PARTITIONED BY MAPPABILITY: two files per cell, each cut from the one BAM
    # STAR wrote by a NAMED selection rather than a filter respelled here, so a misspelling is refused
    # at the verb's gate. Neither selection keeps a record that never aligned, so the pair does not
    # grow to carry what the flag above added, and together they are every primary mapped record —
    # nothing lost against the one mixed archive they replace, and no record in both.
    unique, multi = rendered["unique_to_cram"]["cell_a"], rendered["multiplaced_to_cram"]["cell_a"]
    assert "--selection unique" in unique and f"--bam results/cell_a/{STAR_BAM}" in unique, unique
    assert "--selection multi" in multi and f"--bam results/cell_a/{STAR_BAM}" in multi, multi
    assert "--out results/cell_a/cell_a.unique.cram" in unique, unique
    assert "--out results/cell_a/cell_a.multiplaced.cram" in multi, multi
    # ...and ONE QC artifact per cell, built by the verb the droplet module already calls — the same
    # suffix and the same command surface, with the plate shape selected by the absence of the
    # droplet arguments rather than by a second verb. `--split` is a chimeric cell's and is absent
    # here, which is what makes "their absence means a plate bundle" a claim about THIS plan.
    bundle = rendered["qc_bundle"]["cell_a"]
    assert "--run-dir results/cell_a --sample cell_a" in bundle, bundle
    assert f"--extract results/cell_a/cell_a{EXTRACT_SUFFIX}" in bundle, bundle
    assert f"--out results/cell_a/cell_a{QC_BUNDLE_SUFFIX}" in bundle, bundle
    assert "--solo-dir" not in bundle and "--features" not in bundle, bundle
    assert "--split" not in bundle, bundle
    # EVERY ORIGINAL IT ABSORBS IS RECLAIMED, which is what having a consumer makes legal: the
    # extraction summary and all four of the aligner's run files are `temp()` and gone by the end,
    # so a finished cell's directory holds the archives and one QC artifact rather than five more
    # files with nothing saying which of them a reader was meant to keep. Read off the plan, because
    # `temp()` around a name is an expression whose EFFECT depends on what still needs the file.
    removed = {
        line.split()[-1]
        for line in plan.splitlines()
        if line.startswith("Would remove temporary output")
    }
    absorbed = {
        f"results/{s}/{f}" for s in cells for f in (f"{s}{EXTRACT_SUFFIX}", *STAR_LOG_FILES)
    }
    assert absorbed <= removed, sorted(absorbed - removed)
    assert not any(QC_BUNDLE_SUFFIX in path for path in removed), sorted(removed)


def test_the_chimeric_twin_partitions_its_archives_beside_the_split_and_counts_per_component(
    tmp_path: Path, dry_run: DryRun
) -> None:
    """The twin's rule graph, off a rendered plan — the one thing no other test here can say.

    The wiring gate proves the twin PLANS; it renders every `shell:` and runs none, so a counting
    command naming the wrong flag, an archive made from the wrong BAM or against the wrong reference,
    and a matrix demanded as a folder all pass it. Those are the decisions this module exists to
    carry, so they are read off the plan itself, from a hand-written config — which keeps the claim
    about the MODULE rather than about the composer agreeing with itself, exactly as the base
    module's plan test argues.

    1. **The archive is partitioned by mappability, and the two halves are not the same shape.** The
       uniquely-placed half is per Component, cut from that Component's split BAM and encoded against
       that COMPONENT's reference, which is the only way the file speaks one assembly's chromosome
       names. The multiply-placed half is one file per cell in Chimera coordinates, cut from the
       PRE-split BAM: a multiply-placed fragment has no Component, so filing it under one would state
       an assignment the data cannot support. Together they are exactly every primary mapped record,
       which is why the whole-Chimera archive they replace is planned nowhere.
    2. **The split still sits BESIDE the multiply-placed archive**, both reading the BAM STAR wrote,
       so neither inherits the other's filter and the chimeric BAM is freed once both are done.
    3. **`rule all` demands each Component's matrix and each archive BY NAME.** A rule whose output is
       a folder is satisfied by a folder, which is how a counting job that wrote two Components of
       three exits 0 with an organism silently missing; the archives are nobody's input, so a half
       demanded by nothing would simply stop being produced.
    4. **The counting verb is handed a Component and the CHIMERA.** Exactly one of `--component` and
       `--annotation` is legal, so rendering both is exit 2 rather than a precedence rule anyone has
       to remember — and the record saying what each Component contributed lives on the Chimera, so
       passing the Component as the assembly would resolve the wrong reference.
    5. **N-agnostic**: one job per cell for the split, one per cell PER COMPONENT for the unique
       archive, one per Component for the count, over one config list. The counts below are the whole
       shape, and a Component loop written into the module would read as the same number here only by
       coincidence.

    Plus the reclaim rule, which now reaches everything: the per-Component BAMs are `temp()` over two
    readers, which keeps the split's spelling without keeping the split's bytes, and the split summary
    is `temp()` too — what it MEASURED still outlives the records it measured, inside the cell's one
    QC artifact rather than beside it. So a finished cell leaves its archives and that artifact, and
    nothing else a rule wrote on the way.
    """
    components = ("tinyCe", "tinyEc")
    cells = ("cell_a", "cell_b", "cell_c")
    module = get_module("map/star-umi-chimera")
    config = _plate_run_dir(tmp_path, cells, components=components)

    dotted = {
        f"{key}.{sub}" if isinstance(value, dict) else key
        for key, value in config.items()
        for sub in (value if isinstance(value, dict) else [None])
    }
    # The same identity the base's plan test makes, and here it is what proves the ONE new key is
    # read and that nothing else came with it: `read_files_in.cdna` is the module's optional `.get`.
    assert set(module.required_config) == dotted - {"read_files_in.cdna"}

    plan = dry_run(tmp_path)
    rendered = _rendered_shell(plan)

    assert re.search(r"^split_chimera\s+3\s*$", plan, re.M), plan
    # One multiply-placed archive per cell, not one per Component; the unique half is per both.
    assert re.search(r"^multiplaced_to_cram\s+3\s*$", plan, re.M), plan
    assert re.search(r"^unique_to_cram\s+6\s*$", plan, re.M), plan  # three cells x two Components
    assert re.search(r"^qc_bundle\s+3\s*$", plan, re.M), (
        plan
    )  # ONE QC artifact per CELL, not per pair
    assert re.search(r"^umi_count\s+2\s*$", plan, re.M), plan  # one per Component, not one per cell
    # Each Component's object, each archive and each cell's QC bundle, by name — and the one mixed
    # archive these replace is planned nowhere, because the two halves together already hold every
    # record it held.
    for component in components:
        assert f"results/combined.{component}.h5ad" in plan
        assert all(f"results/{s}/{s}.{component}.unique.cram" in plan for s in cells), plan
    assert all(f"results/{s}/{s}.multiplaced.cram" in plan for s in cells), plan
    assert all(f"results/{s}/{s}{QC_BUNDLE_SUFFIX}" in plan for s in cells), plan
    assert not re.search(r"results/cell_a/cell_a\.cram\b", plan), plan

    # WHICH BAM each half is cut from, read off the dependency edges rather than off a command: the
    # multiply-placed archive from the file STAR wrote, so it and the split see the same records; the
    # uniquely-placed ones from the per-Component BAMs, which is the only file that carries a single
    # assembly's names. This is the ordering decision, and a command string cannot state it.
    inputs = planned_paths(plan, "input")
    assert inputs["multiplaced_to_cram"] == {f"results/{s}/{STAR_BAM}" for s in cells}, inputs
    assert inputs["unique_to_cram"] == {
        f"results/{s}/{s}.{c}.bam" for s in cells for c in components
    }, inputs

    # The multiply-placed half's command is the base module's, verbatim, because a multiply-placed
    # fragment is the same thing on both arms.
    cram = rendered["multiplaced_to_cram"]["cell_a"]
    assert f"--bam results/cell_a/{STAR_BAM}" in cram, cram
    assert f"--assembly {'_'.join(components)}" in cram, cram
    assert "--selection multi" in cram, cram
    # The twin asks for the unmapped records too, and drops them at the archive exactly as the base
    # does — the same pair of flags, because the two modules' commands are the half that may not
    # diverge. `Within` and never `Within KeepPairs`: mate adjacency is an UNSORTED-output ordering
    # and this module writes sorted output.
    assert "--outSAMunmapped Within" in plan
    assert "KeepPairs" not in plan, plan

    # The uniquely-placed half is per Component all the way down: this Component's BAM, this
    # Component's assembly — never the Chimera's, which is the whole reason the file is readable
    # against a bare Component — and the selection that leaves the ambiguous records to the file
    # naming them. Read off the plan text rather than through the rendered-command index, because a
    # job carrying two wildcards is keyed by neither.
    for component in components:
        assert f"--bam results/cell_a/cell_a.{component}.bam --assembly {component}" in plan, plan
        assert (
            f"--out results/cell_a/cell_a.{component}.unique.cram --threads 4 --selection unique"
            in plan
        ), plan

    # The split takes the same BAM, one `<component>=<path>` per Component, and the CHIMERA.
    split = rendered["split_chimera"]["cell_a"]
    assert f"--bam results/cell_a/{STAR_BAM}" in split, split
    for component in components:
        assert f"{component}=results/cell_a/cell_a.{component}.bam" in split, split
    assert f"--assembly {'_'.join(components)}" in split, split
    assert f"--summary results/cell_a/cell_a{SPLIT_SUFFIX}" in split, split
    # The threads the rule RESERVED reach the verb. Asking a scheduler for cores and then handing
    # the command none of them is this module's own recorded defect one rule over, where a whole
    # plate was counted on one core inside an allocation sized for the rest — and it is invisible
    # except here, because a declared `threads:` looks identical either way from the rule source.
    assert "--threads 4" in split, split

    # The count is handed a Component and never an annotation — both would be a refusal, and the
    # assembly stays the Chimera because that is where the merge's record lives.
    counting = rendered["umi_count"]
    assert set(counting) == set(components)
    for component in components:
        command = counting[component]
        assert f"--component {component}" in command, command
        assert "--annotation" not in command, command
        assert f"--assembly {'_'.join(components)}" in command, command
        assert f"--out results/combined.{component}.h5ad" in command, command
        # Every cell of the plate, for this Component and no other.
        assert all(f"{s}=results/{s}/{s}.{component}.bam" in command for s in cells), command

    # ONE QC artifact per cell here too, and its command is the base module's plus the one argument
    # this arm has: what left for which Component. Same verb, same suffix, same file to open
    # whichever arm a cell was processed on.
    bundle = rendered["qc_bundle"]["cell_a"]
    assert "--run-dir results/cell_a --sample cell_a" in bundle, bundle
    assert f"--extract results/cell_a/cell_a{EXTRACT_SUFFIX}" in bundle, bundle
    assert f"--split results/cell_a/cell_a{SPLIT_SUFFIX}" in bundle, bundle
    assert f"--out results/cell_a/cell_a{QC_BUNDLE_SUFFIX}" in bundle, bundle
    assert f"--assembly {'_'.join(components)}" in bundle, bundle

    removed = {
        line.split()[-1]
        for line in plan.splitlines()
        if line.startswith("Would remove temporary output")
    }
    assert any(f"cell_a.{components[0]}.bam" in path for path in removed), sorted(removed)
    # EVERY ORIGINAL THE BUNDLE ABSORBS IS RECLAIMED, the split summary included — it used to be
    # kept, because what it measured has to outlive the records it measured, and it now does that
    # inside the bundle instead of beside it. So a finished cell's directory holds its archives and
    # one QC artifact rather than five more files nothing points at.
    absorbed = {
        f"results/{s}/{f}"
        for s in cells
        for f in (f"{s}{EXTRACT_SUFFIX}", f"{s}{SPLIT_SUFFIX}", *STAR_LOG_FILES)
    }
    assert absorbed <= removed, sorted(absorbed - removed)
    assert not any(QC_BUNDLE_SUFFIX in path for path in removed), sorted(removed)


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

    for rule in (
        "load_genome",
        "umi_extract",
        "star_umi_map",
        "unique_to_cram",
        "multiplaced_to_cram",
        "umi_count",
    ):
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
    ), "a tagged read beside a mate from the other run"
    # ...and the rule's OTHER output landed beside it, from the same rendered command. The uBAM
    # above is `temp()` and this is not, which is the only reason a finished plate can still say how
    # much of each cell carried a tag. Asserted where the command is really run, because whether an
    # option reaches the verb is precisely the fact a formatted `shell:` block cannot show.
    written = json.loads((tmp_path / f"results/cell_a/cell_a{EXTRACT_SUFFIX}").read_text())
    assert written["fragments"] == sum(counts.values())
    assert written["tagged"] == sum(counts.values())  # every synthetic read here carries the tag


def _bulk_run_dir(directory: Path, samples: Sequence[str]) -> None:
    """Write a runnable BULK pipeline directory by hand — a paired-end library, one run per sample.

    The bulk twin of :func:`_plate_run_dir`, and hand-written for the same reason: a ``.smk`` is
    configuration in, rules out, so a config nobody composed is what makes the plan below a proof
    about the MODULE rather than about the composer agreeing with itself. Completeness needs no
    assertion of its own — a key the module reads and this does not carry is a `KeyError` while
    snakemake reads the module, and :func:`~conftest.snakemake_dry_run` refuses a non-zero plan.

    Paired-end and nothing else, because what a `star_count` job declares does not branch on the
    mate count: the layout's two shapes are the subject of the compose tests that emit them.
    """
    module = get_module("map/star")
    config: dict[str, object] = {
        "bulk": {"quantMode": "GeneCounts"},
        "container": "docker://example/align-rna",
        "genome": {"assembly": "sacCer3", "annotation": "ensembl_R64-1-1"},
        "mem_mb": 8 * 1024,
        "outdir": "results",
        "read_files_in": {"mate1": "R1", "mate2": "R2"},
        "threads": 4,
        "units_tsv": "units.tsv",
    }
    rows = ["\t".join(("sample_id", "run", "lane", "read_id", "path"))]
    for sample in samples:
        for read_id in ("R1", "R2"):
            path = f"fastq/{sample}_{read_id}.fastq.gz"
            (directory / "fastq").mkdir(parents=True, exist_ok=True)
            (directory / path).write_bytes(b"")
            rows.append("\t".join((sample, sample, "", read_id, path)))
    (directory / "units.tsv").write_text("\n".join(rows) + "\n")
    (directory / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=True))
    shutil.copy2(module.snakefile, directory / module.snakefile.name)
    (directory / "Snakefile").write_text(core.render_wrapper(module.name, module.snakefile.name))


def test_the_bulk_module_keeps_the_junctions_it_can_use_and_sweeps_the_logs_nothing_reads(
    tmp_path: Path, dry_run: DryRun
) -> None:
    """What a finished bulk run leaves in a sample's directory, off the plan that would leave it.

    `star_count` declared the counts alone, so everything else STAR wrote sat there undeclared and a
    reader had nothing telling them which files were the point. Two decisions close that, and both
    are legible only in a plan: a DECLARED output appears under the job's `output:`, and `temp()` is
    announced as a removal snakemake would perform. Source says neither — `temp()` around a name is
    an expression, and what it *does* depends on whether anything downstream still needs the file.

    **The junction table is kept and the two progress logs are not**, which is one judgement about
    depth rather than a preference: a splice junction called from a bulk library's coverage is
    analyzable, and the same file for one plate cell at ~1M reads is noise (which is why the twins
    summarize it instead). Nothing reads `Log.out` or `Log.progress.out` after a run at any depth.

    The removals are asserted as a SET, so the claim is two-sided in one assertion: a module that
    swept the junction table, or the final log the report reads off disk with no rule in between,
    goes red here just as loudly as one that stopped sweeping a progress log.
    """
    samples = ["S1", "S2"]
    _bulk_run_dir(tmp_path, samples)

    plan = dry_run(tmp_path)

    declared = planned_paths(plan, "output")["star_count"]
    assert {f"results/{s}/ReadsPerGene.out.tab" for s in samples} <= declared, declared
    assert {f"results/{s}/{STAR_JUNCTIONS}" for s in samples} <= declared, (
        f"the bulk module declares no junction table, so a file a user can analyze at this depth "
        f"leaves the run undeclared and unnamed:\n{sorted(declared)}"
    )
    removed = {
        line.split()[-1]
        for line in plan.splitlines()
        if line.startswith("Would remove temporary output")
    }
    assert removed == {f"results/{s}/{f}" for s in samples for f in STAR_PROGRESS_LOGS}, (
        f"a finished bulk run sweeps exactly the two logs nothing reads; it swept {sorted(removed)}"
    )


@pytest.mark.parametrize("module_name", star_modules())
def test_a_star_module_marks_a_stale_segment_before_it_loads_and_frees_it_in_one_place(
    module_name: str,
) -> None:
    """The two halves a rendered plan cannot see, for every workflow that loads a STAR index.

    That both handlers call `release_genome_segment()` and that the helper carries
    `--genomeLoad Remove ... || true` is read off the RENDERED command and the EMITTED module by
    `test_every_star_workflow_shares_one_genome_copy_and_declares_the_read_group_it_stamps`. What no
    rendering shows is the ORDER — marking a stale segment after the load is a load that inherits it
    — nor that the command lives in the helper alone, where a second copy is a second chance to fix
    one.

    The release's own scratch is the third such half. A handler fires on no plan, so where the
    release writes STAR's run-files is unreadable anywhere but here, and it is the invocation that
    runs when the run is OVER: a prefix under the results tree drops a log and a `_STARtmp/` into a
    directory whose owner has already started reading it. Asserted as "the prefix names a directory
    this command makes and removes", which is what makes it a mechanism rather than a setting.

    The lifecycle is COPIED into each workflow file rather than factored out, because composition
    copies exactly one `.smk` into a run directory and an included fragment would be neither copied
    nor eligible as the default target. Three copies that must stay in step is the real cost of that,
    and this is what keeps them honest — so it runs over :func:`~conftest.star_modules`, derived from
    the registry, and a fourth STAR workflow is covered the day it ships.
    """
    source = get_module(module_name).snakefile.read_text()
    load = _rule_blocks(get_module(module_name).snakefile)["load_genome"]

    assert load.index("--genomeLoad Remove") < load.index("--genomeLoad LoadAndExit"), (
        "the stale segment must be marked for destruction BEFORE the load, or the load inherits it"
    )
    after = source[source.index("\nonsuccess:") :]
    assert "--genomeLoad Remove" not in after, "the command belongs to the helper, not to a handler"

    # The helper hands its command to `shell()` as a Python literal, so the source spells the shell's
    # own double quotes escaped. Unescaped once here, because what is being read is the command.
    helper = source[source.index("def release_genome_segment(") : source.index("\nonsuccess:")]
    helper = helper.replace('\\"', '"')
    scratch = re.search(r"(\w+)=\$\(mktemp -d\)", helper)
    assert scratch, f"the release writes under a prefix it did not create:\n{helper}"
    made = f'"${scratch.group(1)}"'
    prefixes = re.findall(r"--outFileNamePrefix (\S+)", helper)
    assert prefixes and all(p.startswith(made) for p in prefixes), (
        f"{module_name}'s release leaves STAR run-files inside the deliverable: {prefixes}"
    )
    assert f"rm -rf {made}" in helper, (
        f"{module_name}'s release makes an aligner scratch directory that outlives it:\n{helper}"
    )


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


@pytest.mark.parametrize(
    ("escalate", "retries"),
    [
        pytest.param(escalated_mem_mb, STARSOLO_RETRIES, id="droplet"),
        pytest.param(bulk_mem_mb, BULK_RETRIES, id="bulk"),
    ],
)
def test_the_sort_budget_follows_the_escalated_memory_request(
    escalate: Callable[[int, int], int], retries: int
) -> None:
    """The composed value `bam_sort_ram(escalate(base, attempt))` — the pair, not each half.

    This is the arithmetic behind the defect #205 removed, and the defect is a *product* of the two
    functions rather than a fault in either: a rule that escalates its memory request while handing
    STAR a cap computed from the static `config["mem_mb"]` looks like it retried, spends the queue
    time of a retry, and then refuses in exactly the place attempt 1 refused, because the sort was
    never allowed to grow with the job around it. So what is asserted is the composition, evaluated
    across the attempts the rule can actually reach, and it must be STRICTLY increasing — a claim
    neither function makes on its own and neither can be inspected for.

    **One row per workflow that escalates, because each carries its OWN escalator and its own retry
    count** — the counts are separate so that raising one workflow's headroom cannot silently buy
    another workflow queue time, and separate constants are exactly the thing a test written against
    one of them stops covering. Bulk's escalation is not droplet's for the same reason either:
    `map/star` counts genes and demultiplexes nothing, so it holds none of the unbounded per-read
    array droplet escalates against, and what a retry buys it is a deeper sample's coordinate sort.
    Different reason, identical arithmetic — so it is a row here and not a neighbour.

    **Attempt 1 returning the request unchanged is an acceptance criterion, not an implementation
    detail.** Nearly every sample in a ~10^4-dataset corpus fits in the default request; making all of
    them more expensive to schedule in order to rescue the handful that do not is the outcome #205
    rejected, and `escalate(m, 1) == m` is the whole of what "a first attempt sized as today still
    succeeds on a normal sample" means in code.

    **The numbers are absolute because a test comparing these outputs only to each other would not
    catch the unit bug.** Monotonicity survives deleting the `* 1024 * 1024`; so does "attempt 2 is
    twice attempt 1"; so does any relation between two returns of the same function. STAR takes
    **bytes** and `mem_mb` is MiB, so a cap handed over in MiB is ~10^6 times too small, and STAR
    then FATALs on *every* sample instead of on a large one — the same flag, a different bug, and a
    green suite. Only an absolute value crosses that boundary, so absolute values are what is written.

    The retry count is pinned rather than parameterised away, and that is the point of taking it as a
    parameter beside the escalator: a workflow's count and its linear multiplier are ONE fact
    (`workflows/memory.py` says so), so the worst case anybody reasoning about the queue actually
    needs is their product. Raising a count without restating what the last attempt is now given
    should go red here.
    """
    # Attempt 1 is today's request, byte for byte, at the shipped default and at any other size.
    assert escalate(_DEFAULT_MEM_MB, 1) == _DEFAULT_MEM_MB
    assert escalate(4096, 1) == 4096

    # snakemake's `attempt` is 1-based, so N retries means N+1 attempts.
    budgets = [
        bam_sort_ram(escalate(_DEFAULT_MEM_MB, attempt)) for attempt in range(1, retries + 2)
    ]
    assert all(later > earlier for earlier, later in pairwise(budgets)), (
        f"the sort cap does not rise with the attempt, so a retry buys scheduler memory STAR is "
        f"still forbidden to sort in: {budgets}"
    )

    # Two retries, so three attempts, and 3/4 of 48 / 96 / 144 GiB IN BYTES. Read as GiB: 36, 72,
    # 108. Had the MiB->byte conversion been dropped, the first of these would read 36864.
    assert retries == 2, "the shipped retry count moved; restate the last attempt's budget"
    assert budgets == [38_654_705_664, 77_309_411_328, 115_964_116_992]

    # A recipe whose whole request is under the 1024 MiB sort floor gets the WHOLE REQUEST as the
    # cap, not the floor. The floor may not exceed the request it is a floor under: authorising STAR
    # to sort in more memory than the job was granted trades STAR's legible refusal ("this is how
    # many bytes I needed") for the scheduler's OOM kill, which is the one failure mode #205 exists
    # to remove. Read through the escalator rather than off `bam_sort_ram` alone, because the guard
    # has to survive the composition: an escalator that grew its own floor — the shape
    # `fan_in_mem_mb` has and these two do not — would hand a tiny recipe a cap above its request
    # while `bam_sort_ram` on its own stayed correct.
    assert bam_sort_ram(escalate(512, 1)) == 536_870_912  # 512 MiB, the whole request, not 1024
    # ...and just above the floor the floor still binds: 3/4 of 1200 MiB is 900, under it.
    assert bam_sort_ram(escalate(1200, 1)) == 1_073_741_824  # 1024 MiB, the floor


@pytest.mark.parametrize(
    ("module_name", "rule_name", "retries_name"),
    [
        pytest.param("map/starsolo", "starsolo_count", "STARSOLO_RETRIES", id="droplet"),
        pytest.param("map/star", "star_count", "BULK_RETRIES", id="bulk"),
    ],
)
def test_the_star_rule_escalates_its_memory_on_retry(
    module_name: str, rule_name: str, retries_name: str
) -> None:
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

    **The expected constant is named PER ROW, and that is where "its own count" is actually paid.**
    Bulk and droplet hold the same number today and answer different failure modes — droplet's count
    was chosen against an allocation that grows with every input read, bulk's against a coordinate
    sort that grows with a sample's depth — so a bulk rule that reached for `STARSOLO_RETRIES`
    because it was already imported would run, plan, and tie one workflow's headroom to a number
    nobody chose for it. The row says which constant, so that substitution goes red.
    """
    body = _rule_blocks(get_module(module_name).snakefile)[rule_name]

    retries = re.search(r"^\s+retries:\s*(\S+)\s*$", body, re.M)
    assert retries, f"`{rule_name}` declares no `retries:`, so a killed job is never re-run at all"
    assert retries.group(1) == retries_name, (
        f"`{rule_name}`'s retry count is not `workflows/memory.{retries_name}`. A literal in the "
        f"Snakefile can disagree with the escalation rule it is half of, and another workflow's "
        f"constant makes one workflow's escalation a function of the other's: {retries.group(1)}"
    )
    assert getattr(memory, retries_name) >= 1, "a retry count of 0 makes the escalation unreachable"

    request = re.search(r"^\s+mem_mb=(.*)$", body, re.M)
    assert request, f"`{rule_name}` requests no `mem_mb`, so the scheduler gates nothing"
    assert request.group(1).startswith("lambda") and "attempt" in request.group(1), (
        f"`mem_mb` is not a function of `attempt`, so every retry re-submits the request that was "
        f"already killed: {request.group(1)}"
    )

    # The cap STAR is handed, and the `resources:` block it must be declared in. Both halves matter:
    # a `params:` entry of the identical text would satisfy the `attempt` check and still freeze.
    cap = re.search(r"^\s+bam_sort_ram_bytes=(.*?)^\s{4}\w+:", body, re.M | re.S)
    assert cap, f"`{rule_name}` computes no sort budget; STAR's default 0 reuses the genome's"
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

#: `Average input read length` is the one row in either log below that was RECONSTRUCTED rather than
#: transcribed — both runs were archived before anything read it — so each carries the length its own
#: chemistry put in front of STAR: a 10x cDNA read here, and in the broken run the 28-base barcode
#: read it handed the aligner by mistake — the same swap `_CATCHES_A_BROKEN_RUN` catches by its
#: consequences, sitting here as the cause. Nothing grades this metric, so no assertion rests on
#: either magnitude; what rests on them is that STAR's own label resolves, since a misspelling costs
#: the row silently rather than raising.
_HEALTHY_LOG: dict[str, object] = {
    "Number of input reads": 412331205,
    "Average input read length": 91,
    "Uniquely mapped reads %": "88.42%",
    "% of reads mapped to multiple loci": "6.31%",
    "% of reads mapped to too many loci": "0.42%",
    "% of reads unmapped: too short": "3.90%",
}

#: A real STARsolo run in which the cDNA read was handed to STAR as the barcode read. Every value but
#: the read length noted above is verbatim from its `Summary.csv` / `Log.final.out`; the four that
#: must go red are the ones a human used to catch by eye, and the reason this layer exists.
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
    "Average input read length": 28,
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
#: own "reads" already reports it, and two columns of one number read as two facts. `Summary.csv` has
#: no twin for the read length, though, so `input_read_length` must SURVIVE that same prune and appear
#: on the droplet row as well as the bulk one.
_FULL_SOLO_METRICS = {
    "reads",
    "input_read_length",
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
    tmp_path: Path,
    *,
    summary: dict[str, object],
    log_final: dict[str, object],
    feature: SoloFeature = "Gene",
) -> tuple[Path, Path]:
    """A STAR output tree carrying STAR's REAL `Summary.csv` / `Log.final.out` labels.

    `_fake_run` writes a two-row summary and a made-up log key, which is enough for the bundle-shape
    tests above and useless here: the reader looks rows up by STAR's exact label, so a fixture that
    invents labels can only ever prove the reader finds nothing.

    `feature` is which `soloFeatures` entry STAR counted over. Filing the SAME rows under a different
    feature name is how a caller gets two runs that agree on every number and disagree only on what
    the number is a count of — which is the one thing a fixture with a hard-wired `Gene` cannot say.
    """
    solo, run_dir = _fake_run(tmp_path, [feature])
    _write(solo / feature / "Summary.csv", "".join(f"{k},{v}\n" for k, v in summary.items()))
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
        "map/star-umi-chimera",
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
    assert MODULES_WITHOUT_CROSS_CHECKS == {
        "map/chromap",
        "map/star",
        "map/star-umi",
        # The chimeric twin inherits the plate's whole argument and adds one: nobody has measured
        # what share of a worm plate SHOULD be E. coli, so a bar on a Component's share would be a
        # figure invented at review — which is the thing this set exists to refuse.
        "map/star-umi-chimera",
    }


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
    kw.setdefault("artifacts", (reg.SampleArtifact("x", _stub_reader),))
    mp.setattr(reg, "_SPECS", {**reg._SPECS, "map/star": reg.StatsSpec(**kw)}, raising=True)


def _rules_forgotten(reg: Any, mp: pytest.MonkeyPatch) -> None:
    _spec(reg, mp)
    mp.setattr(reg, "MODULES_WITHOUT_CROSS_CHECKS", MODULES_WITHOUT_CROSS_CHECKS - {"map/star"})


def _rules_and_silence_both(reg: Any, mp: pytest.MonkeyPatch) -> None:
    _spec(reg, mp, checks=(chemistry_rule,))


def _fan_in_reader_pointed_nowhere(reg: Any, mp: pytest.MonkeyPatch) -> None:
    _spec(reg, mp, read_fan_in=_plural_reader)


def _spec_naming_no_artifact(reg: Any, mp: pytest.MonkeyPatch) -> None:
    _spec(reg, mp, artifacts=())


@pytest.mark.parametrize(
    ("drift", "named"),
    [
        (_no_reader, "map/fourth"),
        (_reader_for_no_module, "unknown module"),
        (_rules_forgotten, r"map/star.*declare no cross-checks"),
        (_rules_and_silence_both, "both declare cross-checks"),
        (_fan_in_reader_pointed_nowhere, "no fan_in_artifact"),
        (_spec_naming_no_artifact, "naming no per-sample artifact"),
    ],
    ids=[
        "no-reader",
        "reader-for-no-module",
        "rules-forgotten",
        "rules-and-silence",
        "fan-in",
        "no-artifact",
    ],
)
def test_the_registry_guard_catches_every_way_a_module_and_its_reader_can_drift(
    drift: Callable[[Any, pytest.MonkeyPatch], None],
    named: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guard nobody has seen fail is a guard that may not be looking, and it has six ways to look.

    One row per way a maintainer gets this wrong. The two quietest are the last: a fan-in reader for
    a module declaring no such artifact reads nothing forever, because `read_pipeline_stats` has no
    path to hand it one — and a spec naming no per-sample artifact at all reads nothing forever
    while still looking, from the registry, exactly like a module that reports. Every registry is
    rebound rather than mutated.
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
    # The gene count says it again in its own label, because the note is a caption for the whole
    # table and this is the number a reader quotes on its own. `Gene` counts exons.
    assert got["genes_detected"].label == "Genes (exon)"

    # The SAME summary rows, filed under the feature that counts gene bodies: one number, two words
    # for it. That is what makes the label a function of the region rather than a constant nobody
    # would notice was wrong — an exonic count can never surface under a word that says introns were
    # included, whichever feature the run happened to count over.
    body_solo, body_run = _finished_star_run(
        tmp_path / "body",
        summary=_HEALTHY_SUMMARY,
        log_final=_HEALTHY_LOG,
        feature="GeneFull",
    )
    body = _by_key(
        read_starsolo_metrics(
            write_qc_bundle(
                body_solo, body_run, ["GeneFull"], tmp_path / "body.qc.json.gz", sample="S1"
            ),
            "S1",
        )
    )
    assert body["genes_detected"].label == "Genes (combined)"
    assert body["genes_detected"].value == got["genes_detected"].value == 21044


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


def test_a_metric_is_shown_by_the_formatter_its_own_number_survives() -> None:
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

    `input_read_length` is the same decision one door along: it is a length in bases, read against a
    length the human remembers sequencing, and `count`'s abbreviating default renders a merged
    long-read fragment as `1.2K` — a number nobody can subtract 150 from. So it passes `exact`, and
    the counterfactual below is why.
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

    long_read = _by_key(starsolo_metrics({"log_final": {"Average input read length": 1203}}, "S1"))[
        "input_read_length"
    ]

    assert long_read.display == "1,203" and fmt_count(long_read.value) == "1.2K"
    assert long_read.level == "none"  # no bar to defend, so no colour claiming there is one


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

    landed = stats_registry._SPECS["map/starsolo"].artifacts[0]
    monkeypatch.setattr(
        stats_registry,
        "_SPECS",
        {
            "map/starsolo": stats_registry.StatsSpec(
                artifacts=(stats_registry.SampleArtifact(landed.filename, buggy),)
            )
        },
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
        "input_read_length",
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


#: SAM's own words for the two placements that re-state a read the file already carries. Spelled as
#: the flag bits because that is what a fragment declaring one is asking for, and because a boolean
#: per bit would multiply every time SAM grew another.
_SECONDARY = 0x100
_SUPPLEMENTARY = 0x800


@dataclass(frozen=True)
class _Fragment:
    """One fragment to synthesise: where it lands, what it carries, and how many loci it claims."""

    name: str
    contig: str
    start: int
    end: int
    umi: str = ""
    hits: int = 1
    #: Only one mate of this fragment aligned. Two records: the survivor, flagged mate-unmapped, and
    #: the dead mate written AT the survivor's coordinates with no placement of its own — the shape
    #: an aligner asked to emit what it could not place writes, and the one that makes a dead mate
    #: attributable to a Component at all.
    mate_unmapped: bool = False
    #: Which mate of a half-mapped pair is the one that aligned. Meaningless without
    #: `mate_unmapped`, and it exists because a plate whose survivors are all FIRST mates cannot
    #: tell a check that subtracts each side's own singletons from one that subtracts one side's
    #: from both.
    survivor: int = 1
    #: The dead mate of a half-mapped fragment is NOT in the file: ONE record, the survivor, flagged
    #: mate-unmapped with nothing anywhere to answer it. Meaningless without `mate_unmapped`, and
    #: only a MULTIPLY-placed fragment can take this shape, so the rows that use it carry `hits`
    #: above one. The aligner emits one representative of a locus set and writes an unmapped mate
    #: only for the member it emitted, so a survivor from any other member is the whole of what its
    #: fragment leaves behind. Read off three cells of a 784-cell plate that refused on exactly this.
    dead_mate_absent: bool = False
    #: Neither mate aligned anywhere. One record, no reference, `uT` saying why — the shape STAR
    #: writes for a pair it could not place, and the one a chimera split has to DROP rather than
    #: rewrite, because its RNEXT still names a suffixed chromosome the output header will not have.
    unmapped: bool = False
    #: Bits OR-ed onto both mates' flags: `_SECONDARY` or `_SUPPLEMENTARY`. A fragment carrying one
    #: is a placement some consumer is expected to discard, and which discard rule saw it first is
    #: exactly what a per-reason drop count has to keep apart.
    extra_flags: int = 0
    #: Where this fragment's MATE sits, when that is somewhere other than `contig`. Empty — the
    #: default — puts the mate on this fragment's own contig, which is what an aligner writes and
    #: what every other row here wants. Set to another Component's contig it builds the one template
    #: the chimera splitter's design says cannot exist: both mates carry one `NH` and one chromosome,
    #: so a template spanning two Components is the assumption failing rather than a case to handle.
    mate_contig: str = ""
    #: Which chromosome the DEAD mate's pointer names, when that is not the survivor's own. A field
    #: of its own and not a second use of `mate_contig` above, because the two say opposite things:
    #: `mate_contig` moves the LIVE record's pointer, which is the template the splitter refuses by
    #: name, while this moves only the placeless record's — the survivor stays whole and the split
    #: runs to the end. Meaningless without `mate_unmapped`. Set to another Component's contig it
    #: builds the fragment a real chimera produced and this fixture could not: a survivor on one
    #: organism whose dead mate names the other. Only a MULTIPLY-placed fragment can take that
    #: shape, since the aligner emits one representative of the locus set and the pointer may name a
    #: different member of it, so the row that uses this carries `hits` above one too.
    dead_mate_contig: str = ""
    #: A placeless record for one of this fragment's mates, IN ADDITION to the fully mapped pair,
    #: pointing at this contig. Three records, not two, and no survivor among them carries the
    #: mate-unmapped flag — which is the whole shape: the fragment aligned, so it owes no dead mate,
    #: and the dead mate is in the file anyway. Only a MULTIPLY-placed fragment can take it, since
    #: the leftover belongs to another locus of the same set, so the row using it carries `hits`
    #: above one. Meaningless together with `mate_unmapped`, whose fragment has no mapped pair.
    stray_pointer_contig: str = ""


def _segments(header: Any, frag: _Fragment) -> list[Any]:
    """One `_Fragment` -> its BAM records: two mates, or one when neither of them aligned."""
    import pysam

    span = frag.end - frag.start
    mate_start = frag.end - _READ_LEN

    def build(start: int, flag: int, mate: int, tlen: int) -> Any:
        rec = pysam.AlignedSegment(header)
        rec.query_name = frag.name
        rec.query_sequence = "A" * _READ_LEN
        rec.query_qualities = pysam.qualitystring_to_array("I" * _READ_LEN)
        rec.flag = flag
        if not rec.is_unmapped:
            tid = header.get_tid(frag.contig)
            rec.reference_id = tid
            rec.reference_start = start
            rec.mapping_quality = 255
            rec.cigarstring = f"{_READ_LEN}M"
            rec.next_reference_id = header.get_tid(frag.mate_contig) if frag.mate_contig else tid
            rec.next_reference_start = mate
            rec.template_length = tlen
        tags: list[tuple[str, object, str]] = [("NH", frag.hits, "i")]
        if frag.umi:
            tags.append(("UB", frag.umi, "Z"))
        if frag.unmapped:
            tags.append(("uT", "4", "A"))
        rec.set_tags(tags)
        return rec

    def stranded(flag: int, pointer: str) -> Any:
        """A placeless record: NO placement of its own, a chromosome in its MATE POINTER.

        What an aligner asked to emit what it could not place writes, and the split of the two
        fields is the whole point: `RNAME` is `*`, so nothing may read a placement off this record,
        while `RNEXT` names a chromosome, so something can read an ORGANISM off it. `NH` is zero
        because nothing was placed — which is also why the pointer has to be stated on the row
        rather than inferred here: this record cannot say how many loci its fragment claimed, nor
        whether that fragment aligned at all, and on a real chimera the pointer of a multiply-placed
        one names a member of the locus set the emitted alignment did not take.

        **Read off a real chimeric BAM, not guessed.** This built the record with `RNAME` set as
        well, which is the shape the SAM spec permits and STAR does not write; the check that reads
        it therefore passed here and counted zero on a real cell, refusing 5440 healthy half-mapped
        fragments. A fixture that flatters the code it feeds is worth nothing, so this one now
        writes what the aligner was observed to write.
        """
        rec = pysam.AlignedSegment(header)
        rec.query_name = frag.name
        rec.query_sequence = "A" * _READ_LEN
        rec.query_qualities = pysam.qualitystring_to_array("I" * _READ_LEN)
        rec.flag = flag
        rec.next_reference_id = header.get_tid(pointer)
        rec.next_reference_start = frag.start
        rec.set_tags([("NH", 0, "i"), ("uT", "4", "A")])
        return rec

    if frag.unmapped:
        # PAIRED | UNMAPPED | MATE_UNMAPPED | READ1, and no coordinates at all.
        return [build(frag.start, 1 | 4 | 8 | 64, frag.start, 0)]
    if frag.mate_unmapped:
        # PAIRED | MATE_UNMAPPED for the mate that landed, PAIRED | UNMAPPED for the one that did
        # not, and the READ1/READ2 bits go whichever way round this fragment says.
        live, dead = (64, 128) if frag.survivor == 1 else (128, 64)
        survivor = build(frag.start, 1 | 8 | live, frag.start, 0)
        if frag.dead_mate_absent:
            return [survivor]
        return [survivor, stranded(1 | 4 | dead, frag.dead_mate_contig or frag.contig)]
    pair = [
        build(frag.start, 1 | 2 | 32 | 64 | frag.extra_flags, mate_start, span),
        build(mate_start, 1 | 2 | 16 | 128 | frag.extra_flags, frag.start, -span),
    ]
    if frag.stray_pointer_contig:
        # A THIRD record on a fragment that aligned whole: the second mate again, placeless, its
        # pointer somewhere neither alignment above went. Neither of the two carries the
        # mate-unmapped flag, so this fragment contributes no survivor and one pointer.
        pair.append(stranded(1 | 4 | 32 | 128, frag.stray_pointer_contig))
    return pair


def _synthetic_bam(
    path: Path, fragments: Sequence[_Fragment], header: dict[str, Any] | None = None
) -> Path:
    """`fragments` -> a COORDINATE-sorted BAM, which is the input contract this counter has.

    Sorting by position is what scatters each fragment's two mates apart, so a port that quietly
    depended on name adjacency goes red here rather than in production. A record with no coordinates
    sorts to the END, where a coordinate-sorted BAM puts it, rather than ahead of chromosome zero.

    `header` defaults to the counter's own two-contig header. The chimera rows hand over their own,
    because a chimeric BAM's whole subject is what its @SQ block says and which program wrote it.
    """
    import pysam

    template = pysam.AlignmentHeader.from_dict(
        header if header is not None else {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": _CONTIGS}
    )
    records = [rec for frag in fragments for rec in _segments(template, frag)]
    records.sort(
        key=lambda r: (r.reference_id if r.reference_id >= 0 else 1 << 30, r.reference_start)
    )
    with pysam.AlignmentFile(str(path), "wb", header=template) as out:
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
    # It reaches no counted matrix, and it IS placed: GENE_A's body is one of the loci it could have
    # come from, so the placement layer credits it there.
    _Fragment("multimapper", "chr1", 120, 180, umi="TTTTTTTT", hits=2),
    # The same UMI at three loci, this time BETWEEN GENE_A's exons — which is why the placement
    # layer is over gene bodies: an intronic multimapper is still in the gene. One molecule with the
    # one above, so the layer credits GENE_A once and not twice.
    _Fragment("multimapper_intron", "chr1", 300, 360, umi="TTTTTTTT", hits=3),
    # Four loci, and its representative span covers the bodies of GENE_C and GENE_D at once: the
    # ambiguity rule the counted matrices already use, so it is placed in neither.
    _Fragment("multimapper_two_genes", "chr1", 4520, 4560, umi="CCCCCCCC", hits=4),
    # Multiply placed and untagged, so it is in the fate and in the locus distribution and in no
    # matrix at all — the placement layer is deduplicated molecules, which an untagged read has none
    # of, and mixing raw reads into it would make the ratio against `umi_combined` meaningless.
    _Fragment("multimapper_read", "chr1", 2120, 2180, hits=2),
    # Aligned to a scaffold no GTF line mentions, and to a gap between genes: both `_no_feature`.
    _Fragment("scaffold", "chrUn_synthetic", 50, 110),
    _Fragment("intergenic", "chr1", 8000, 8060),
    # Exons of GENE_C and GENE_D at once, then bodies of both with no exon: ambiguous twice over.
    _Fragment("ambiguous_exon", "chr1", 4660, 4690),
    _Fragment("ambiguous_intron", "chr1", 4520, 4560),
    _Fragment("mate_never_aligned", "chr1", 120, 180, umi="AAAAAAAA", mate_unmapped=True),
    # ...and neither mate anywhere: one record, no coordinates. This is the OTHER half of the
    # unmapped test — the half nothing could reach until the aligner was asked to emit the records
    # it could not place within its output, which is why that fate was a structural zero on every
    # plate object written before then rather than a small number.
    _Fragment("never_aligned", "", 0, 0, unmapped=True),
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


def _matrix_bytes(adata: ad.AnnData, layer: str | None = None) -> tuple[bytes, bytes, bytes]:
    """One count matrix as the three buffers a CSR *is*: values, columns, and row starts.

    Comparing these rather than two whole `.h5ad` files is what lets "this matrix did not move" be
    asserted between two objects that differ elsewhere on purpose — the fates and the new layer.
    All three buffers, because equal values in different places is exactly the failure a values-only
    comparison would call identical.
    """
    matrix = _counts(adata, layer)
    return matrix.data.tobytes(), matrix.indices.tobytes(), matrix.indptr.tobytes()


def _frame(table: object) -> Any:
    """`adata.obs`/`adata.var` are declared as a union with a lazy on-disk table.

    On an object this file just built or read back it is a pandas frame, narrowed once here rather
    than with a cast at every call site — the same move `_counts` makes for a matrix.
    """
    return table


def _hits(adata: ad.AnnData) -> Any:
    """The per-cell locus-count array off `obsm`, narrowed the way `_frame` narrows a table."""
    obsm: Any = adata.obsm
    return obsm[MULTIMAPPING_HITS]


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


def test_the_overlap_index_answers_every_span_the_intervals_do_and_lends_out_its_interned_set() -> (
    None
):
    """The step index against a brute-force sweep of the intervals it was built from.

    `genes` is the busiest path in the counter — once per fragment of every cell — so it is the one
    place where a faster search would be worth a wrong answer, and the two things it has to get
    right are both invisible in a whole-plate fate assertion. Several features opening or closing at
    one base collapse into a single segment, which is where an off-by-one in either bound hides; and
    a span touching exactly one segment is handed the interned set itself rather than a copy of it,
    which is only sound because that set is immutable. The oracle below is the intervals rather than
    a second reading of the index, so a wrong bound fails it instead of agreeing with it.
    """
    intervals = [
        (10, 40, 0),  # three features opening at the same base, two of them closing at the same one
        (10, 40, 1),
        (10, 25, 2),
        (25, 60, 3),  # opens exactly where gene 2 closes: adjacent, never overlapping
        (40, 40, 4),  # zero length, so it covers nothing and must open no segment at all
        (70, 90, 5),
    ]
    index = _step_index(intervals)

    def covering(start: int, end: int) -> frozenset[int]:
        span = set(range(start, end))
        return frozenset(gene for s, e, gene in intervals if span & set(range(s, e)))

    for start in range(0, 100):
        for end in range(start, 101):  # end == start is a span of no bases and covers nothing
            assert index.genes(start, end) == covering(start, end), (start, end)

    # The single-segment answer is the index's own object, not a rebuild of it: `frozenset(found)`
    # would return something equal and distinct, so identity is what says the copy is gone.
    inside = index.genes(11, 20)
    assert inside == frozenset({0, 1, 2})
    assert any(inside is interned for interned in index.sets)

    # A contig whose features were all zero-length is a contig with none, and answers empty for
    # every span rather than raising on an index with nothing in it.
    empty = _step_index([(5, 5, 0)])
    assert empty.genes(0, 1000) == frozenset()
    assert _step_index([]).genes(0, 1000) == frozenset()


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

    Seventeen fragments, five counted matrices and four fates; every number below is read off the
    fixture's own comments rather than recomputed here.

    Both ways a fragment can fail to align are in the plate, and the second of them is why the count
    is two: a record whose own placement is missing, and one standing for a pair whose mate's is.
    The first cannot occur in an aligner's output unless the aligner was asked for it, so the branch
    that reads it sat unreachable, and a plate object's unmapped column was a zero that meant
    nothing. Counting them apart is not the point — a fragment either aligned or it did not — so
    they share the fate rather than splitting it.
    """
    db, cells = _plate(tmp_path)
    adata = count_plate(cells, read_annotation(db))
    # The fates and the fragment total are read off the object, which is the only place they live:
    # the counter hands back no second copy of a cell to check them against.
    row = _frame(adata.obs).loc["cell_a"]

    assert int(row[N_FRAGMENTS]) == len(_PLATE)
    assert {fate: int(row[fate]) for fate in FATES} == {
        "unmapped": 2,  # mate_never_aligned, and never_aligned
        "multimapping": 4,  # every fragment whose NH is above one, placed or not
        "no_feature": 2,  # the scaffold, and the intergenic fragment
        "ambiguous": 2,  # two exonic genes, then two gene bodies and no exon
    }

    # UMIs: GENE_A carries "AAAAAAAA" (twice, deduplicated) and "GGGGGGGG" (the spanning fragment).
    assert _row(adata, "cell_a", "GENE_A") == 2
    assert _row(adata, "cell_a", "GENE_B") == 1
    # ...and an untagged fragment never reaches a UMI matrix, nor a tagged one a read matrix.
    assert _row(adata, "cell_a", "GENE_A", "read_exon") == 1
    assert _row(adata, "cell_a", "GENE_A", "read_intron") == 1
    # Both ambiguous genes stay at zero in every matrix — all six of them, counted off `LAYERS`
    # rather than written out, so a seventh matrix is not silently unasserted. That covers the
    # multiply-placed fragment over both their bodies too: the placement layer reuses the counted
    # matrices' ambiguity rule rather than inventing a looser one, so it credits neither.
    for gene in ("GENE_C", "GENE_D"):
        assert [_row(adata, "cell_a", gene, layer) for layer in (None, *LAYERS)] == [0] * (
            1 + len(LAYERS)
        )

    # Where the ambiguity went. GENE_A's body is one of the loci two multiply-placed fragments could
    # have come from — one over an exon and one between them — and they carry one UMI between them,
    # so the layer credits it ONE molecule, deduplicated exactly as every other UMI matrix is.
    assert _row(adata, "cell_a", "GENE_A", MULTIMAPPING_LAYER) == 1
    # ...and the untagged one is in no matrix at all, GENE_B's included.
    assert _row(adata, "cell_a", "GENE_B", MULTIMAPPING_LAYER) == 0
    # How many loci each of them had, as the per-cell array whose column IS the locus count: two
    # fragments at two loci, one at three, one at four. It sums back to the fate that counted them.
    assert list(_hits(adata)[0]) == [0, 0, 2, 1, 1]
    assert int(_hits(adata)[0].sum()) == int(row["multimapping"])

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
    adata = count_plate(cells, read_annotation(db))

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
    adata = count_plate([("one_cell", tmp_path / "split.bam")], annotation)
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


def test_umi_correction_by_neighbour_index_answers_what_the_full_scan_answers() -> None:
    """The correction reads its neighbours out of an index; this is the scan it has to agree with.

    `scan` below is the reference's algorithm as the counter used to hold it: walk the survivors
    from least abundant upward and STOP at the first one too abundant for the seed to explain. The
    index cannot walk, so it applies that stop as arithmetic on each neighbour it looks up, and the
    two are only the same function because the survivors are in count order and the seed's count
    never rises as it absorbs. Nothing enforces that pair of properties, so this asserts it: the
    moment the stop and the filter disagree on any bucket, one of the two is wrong and this goes
    red. It compares key order as well as totals, so a lost tie-break lands here too — that is the
    same guarantee the byte-identical plate rests on, priced at a millisecond instead of an h5ad.

    Buckets come from a fixed seed rather than from `hypothesis`, which this project does not depend
    on and would not be worth depending on for one property. Three letters, so Hamming-1 neighbours
    are common at these lengths; several lengths including buckets that mix them, because refusing
    to merge a ragged pair is a property of the index's key rather than a rule anybody wrote down,
    and a key is exactly the kind of thing that stops carrying a length by accident. The two counters
    at the end are what keep the generator honest: a widened alphabet or a flattened count
    distribution would leave the buckets with nothing to merge and quietly make all of this vacuous.
    """
    import random

    from seqforge.workflows.umite.count import COUNT_RATIO_THRESHOLD

    # One substitution, spelled here rather than imported: the source realises the distance as the
    # shape of a masked key, so an oracle that read the same number back from it could not notice a
    # key that stopped meaning one substitution.
    def hamming_within(a: str, b: str, threshold: int = 1) -> bool:
        if len(a) != len(b):
            return False
        seen = 0
        for x, y in zip(a, b, strict=True):
            if x != y:
                seen += 1
                if seen > threshold:
                    return False
        return True

    def scan(observations: dict[str, int]) -> dict[str, int]:
        remaining = sorted(observations.items(), key=lambda item: (-item[1], item[0]))
        corrected: dict[str, int] = {}
        while remaining:
            seed, seed_count = remaining.pop(0)
            corrected[seed] = seed_count
            i = len(remaining) - 1
            while i >= 0:
                candidate, candidate_count = remaining[i]
                if (COUNT_RATIO_THRESHOLD * candidate_count) - 1 > seed_count:
                    break
                if hamming_within(seed, candidate):
                    corrected[seed] += candidate_count
                    remaining.pop(i)
                i -= 1
        return corrected

    rng = random.Random(20260811)
    with_a_merge = 0
    ragged = 0
    for _ in range(400):
        lengths = rng.choice(([6], [8], [10], [6, 8], [6, 8, 10]))
        bucket: dict[str, int] = {}
        for _ in range(rng.randint(1, 40)):
            umi = "".join(rng.choice("ACG") for _ in range(rng.choice(lengths)))
            bucket[umi] = rng.randint(1, 12)
        corrected = correct_umis(bucket)
        assert corrected == scan(bucket), bucket
        assert list(corrected) == list(scan(bucket)), bucket
        with_a_merge += len(corrected) < len(bucket)
        ragged += len({len(umi) for umi in bucket}) > 1

    assert with_a_merge > 100, "the generator stopped producing neighbours to merge"
    assert ragged > 50, "the generator stopped producing UMIs of unequal length"


def test_the_object_is_x_plus_five_layers_indexed_on_sample_id_with_the_fates_as_obs_columns(
    tmp_path: Path,
) -> None:
    """The deliverable's shape, which is the half of this ticket no wrong number would show.

    Rows are sample ids, which is what makes an h5ad row and a per-cell CRAM filename join. The
    fates are per-cell scalars and live on `obs`; the reference carries them as extra *gene*
    columns in a matrix whose other 55 335 columns really are genes, which is what forced a
    correction in its output shape.

    Beside them, what the cell yielded: its molecules and how many genes any of them reached. Every
    other `obs` column is an account of how a fragment FAILED to reach a gene, so an object carrying
    only those can say what went wrong and never what came out — and the exhaustive column-set
    assertion below is what makes adding one impossible to do silently.

    Five matrices of expression and not six: that grid is (UMI | read) x (exon | intron | combined),
    and its sixth cell is deliberately absent. An untagged read has nothing to deduplicate by and the
    reference never tries, so a combined READ matrix is `read_exon + read_intron` exactly — a layer
    that earns nothing, kept out on the same rule that lets the combined UMI matrix in.

    The sixth layer is off that grid entirely: it holds where the fragments no matrix may credit were
    PLACED, and its caveat is on the object rather than only in the module that wrote it, because a
    reader who opens this file years from now has the object and not the source. Adding it to
    expression is the one mistake it exists to make hard, so the sentence that forbids that has to
    arrive with the bytes.
    """
    db, cells = _plate(tmp_path)
    out = write_umi_counts(cells, db, tmp_path / "plate" / "counts.h5ad")
    adata = ad.read_h5ad(out)

    assert list(adata.obs_names) == ["cell_a", "cell_b"]  # the order the cells were handed over
    assert _layer_names(adata) == set(LAYERS)
    assert set(LAYERS) == {
        "umi_intron",
        "umi_combined",
        "read_exon",
        "read_intron",
        MULTIMAPPING_LAYER,
    }
    # The derivable cell of the grid, asserted as absent BY NAME: a reader adding two read columns
    # gets the right answer, and a sixth matrix would only be a second place for it to be wrong.
    assert "read_combined" not in _layer_names(adata)
    assert (
        _row(adata, "cell_a", "GENE_A", "read_exon")
        + _row(adata, "cell_a", "GENE_A", "read_intron")
        == 2
    )
    assert adata.uns["primary_matrix"] == PRIMARY_MATRIX
    # The caveat is discoverable from the object ALONE, and it names the layer it is about — so a
    # reader who found the layer first can find the sentence, and neither can be renamed alone.
    assert adata.uns["multimapping_caveat"] == MULTIMAPPING_CAVEAT
    assert MULTIMAPPING_LAYER in MULTIMAPPING_CAVEAT
    assert set(adata.var_names) == {"GENE_A", "GENE_B", "GENE_C", "GENE_D"}
    assert set(adata.obs.columns) == {
        *FATES,
        N_FRAGMENTS,
        SATURATION,
        N_UMIS,
        GENES_DETECTED,
    }
    # What a cell YIELDED, beside the four ways its fragments failed to. Read off `_PLATE` by hand:
    # `cell_a`'s combined UMI matrix holds GENE_A twice — "AAAAAAAA", seen exonically twice and
    # intronically once, and "GGGGGGGG" off the spanning fragment — and GENE_B once, so three
    # molecules over two genes. Both are counted over `umi_combined` and never over `X`, which is
    # what makes this total and the saturation beside it two readings of one number.
    obs = _frame(adata.obs)
    assert (int(obs.loc["cell_a", N_UMIS]), int(obs.loc["cell_a", GENES_DETECTED])) == (3, 2)
    assert (int(obs.loc["cell_b", N_UMIS]), int(obs.loc["cell_b", GENES_DETECTED])) == (1, 1)
    # ...and the one per-cell figure that is a vector rather than a scalar, which is why it is the
    # only thing here on `obsm`: one column per locus count, so `obs` could not have held it.
    assert _hits(adata).shape == (2, 5)
    assert _frame(adata.var).loc["GENE_B", "gene_name"] == "beta"
    _counts(adata)  # sparse in the object and not only on disk: a plate is almost entirely zeros


def test_placing_the_multimappers_leaves_every_counted_matrix_byte_identical(
    tmp_path: Path,
) -> None:
    """The claim that makes attributing ambiguity safe, proved rather than promised.

    Excluding multiply-placed fragments from expression is worth +10.2% of a real cell's primary UMI
    matrix, which is more than the entire improvement the reference tool is published for — so the
    layer that says which genes they fell in may not put a single count back. It is written from the
    branch that had already returned, and the two objects below are the same plate with and without
    that population: every matrix that is expression comes out byte-for-byte the same, and only the
    fate, the locus distribution and the placement layer differ.

    Bytes and not values, for the reason the recount test below compares files rather than numbers: a
    CSR that agrees on every value while disagreeing on where they sit is a matrix that moved.
    """
    db, cells = _plate(tmp_path)
    annotation = read_annotation(db)
    before = _synthetic_bam(
        tmp_path / "before.bam", tuple(frag for frag in _PLATE if frag.hits == 1)
    )

    placed = count_plate([("cell_a", cells[0][1])], annotation)
    unplaced = count_plate([("cell_a", before)], annotation)

    for layer in (None, *(name for name in LAYERS if name != MULTIMAPPING_LAYER)):
        assert _matrix_bytes(placed, layer) == _matrix_bytes(unplaced, layer), layer
    # ...and this really is the same plate MINUS that population rather than two plates that never
    # had one: a fixture whose multiply-placed rows had gone would satisfy every comparison above
    # vacuously, which is the way a claim about them stops being a claim without going red.
    assert int(_frame(placed.obs).loc["cell_a", "multimapping"]) == 4
    assert int(_frame(unplaced.obs).loc["cell_a", "multimapping"]) == 0
    assert _matrix_bytes(placed, MULTIMAPPING_LAYER) != _matrix_bytes(unplaced, MULTIMAPPING_LAYER)


def test_saturation_is_the_molecules_over_the_gene_assigned_fragments_that_carried_a_umi(
    tmp_path: Path,
) -> None:
    """One number under one definition, computed where the deduplication that decides it happens.

    The droplet page reads this ratio out of STARsolo's summary and this one computes it, so the two
    have to be the same arithmetic over the same population or a reader comparing a plate against a
    droplet sample is comparing two things sharing a word. It is deduplicated molecules over the
    fragments that reached a gene carrying a UMI — the positional definition, distinct start
    coordinates over reads, is a different number and would need the chimera split to hold a set per
    cell, which is the streaming property that keeps a plate's memory flat.

    Every figure below is read off the fixture. `umi_combined` is the molecule count because it is
    the only matrix that counts a UMI seen both exonically and intronically ONCE; the deep cell is
    where that matters, since adding its exon and intron matrices reports three molecules where one
    was sequenced, and 0.7 saturation for a library that reached 0.9.
    """
    db, cells = _plate(tmp_path)
    annotation = read_annotation(db)
    adata = count_plate(cells, annotation)
    obs = _frame(adata.obs)

    # cell_a: five fragments reached a gene carrying a UMI — "AAAAAAAA" three times on GENE_A,
    # "GGGGGGGG" once there, "CCCCCCCC" once on GENE_B — and they are three molecules.
    assert float(obs.loc["cell_a", SATURATION]) == pytest.approx(1 - 3 / 5)
    # cell_b is one fragment and one molecule: nothing was sequenced twice, and that is a real zero
    # rather than a missing measurement.
    assert float(obs.loc["cell_b", SATURATION]) == 0.0

    # ...and it rises with duplication: ten tagged fragments on one gene that correct to a single
    # molecule is a cell where nine reads in ten found nothing new.
    deep = count_plate(
        [("deep", _synthetic_bam(tmp_path / "deep.bam", _SPLIT_NEIGHBOURS))], annotation
    )
    assert float(_frame(deep.obs).loc["deep", SATURATION]) == pytest.approx(1 - 1 / 10)

    # A cell with no tagged fragment on any gene has no ratio at all rather than a zero — the
    # denominator is the arithmetic with no answer, and the page then omits the column for that cell
    # instead of reporting a saturation nobody measured.
    untagged = _synthetic_bam(
        tmp_path / "untagged.bam", (_Fragment("read_only", "chr1", 120, 180),)
    )
    empty = count_plate([("untagged", untagged)], annotation)
    assert math.isnan(float(_frame(empty.obs).loc["untagged", SATURATION]))


def test_counting_the_same_plate_twice_gives_a_byte_identical_h5ad(tmp_path: Path) -> None:
    """Determinism, asserted on the artifact rather than on the absence of `random` in the source.

    The reference picks an alignment with an unseeded `random.choice` when a read has several
    primary alignments. There is nothing to choose here — every tie-break is written down — and
    this is what proves it, including the iteration orders that are only accidentally stable.

    **And it holds across the fan-out, which is where determinism is easiest to lose.** Counting a
    plate on N cores makes the order cells FINISH in a property of their depth and of the machine,
    so the two pooled files below are the ones that would differ if any row, any matrix or any
    correction inherited that order instead of the caller's. All four are the same bytes: the width
    is how many cells are counted at once and nothing else.
    """
    db, cells = _plate(tmp_path)

    first = write_umi_counts(cells, db, tmp_path / "first.h5ad")
    second = write_umi_counts(cells, db, tmp_path / "second.h5ad")
    pooled = write_umi_counts(cells, db, tmp_path / "pooled.h5ad", workers=4)
    again = write_umi_counts(cells, db, tmp_path / "pooled-again.h5ad", workers=4)

    assert first.read_bytes() == second.read_bytes()
    assert pooled.read_bytes() == again.read_bytes()
    assert pooled.read_bytes() == first.read_bytes()


def test_a_plate_refuses_rather_than_writing_a_row_that_names_two_cells(tmp_path: Path) -> None:
    """A sample id is an h5ad row, so two cells sharing one refuses instead of overwriting.

    The last case is the one the fan-out newly has to keep: a cell that cannot be counted is a
    refusal in a worker, and a worker's traceback says nothing about which of hundreds of cells it
    belonged to. It carries the sample id here whichever way the plate ran, which is why the same
    refusal is asserted at both widths.
    """
    db, cells = _plate(tmp_path)
    annotation = read_annotation(db)

    with pytest.raises(UmiCountError, match="repeat"):
        count_plate([("same", cells[0][1]), ("same", cells[1][1])], annotation)
    with pytest.raises(UmiCountError, match="no cells"):
        count_plate([], annotation)
    for workers in (1, 4):
        with pytest.raises(UmiCountError, match="'gone'.*missing"):
            count_plate(
                [("counted", cells[0][1]), ("gone", tmp_path / "never-aligned.bam")],
                annotation,
                workers=workers,
            )


def test_the_plate_is_counted_on_every_core_it_was_given_and_on_one_where_fork_is_not_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fan-out's two arms: what the width actually buys, and what happens where it cannot.

    **The width is asserted with a barrier, because nothing weaker can tell a pool from a loop.**
    Every cell waits for every other before it returns, so a plate handed four workers finishes only
    if four cells really were being counted at once — a counter that quietly ran them one after
    another blocks and the barrier breaks. It also makes the completion order genuinely
    indeterminate, which is what puts the row-order claim under load: all four cells finish at the
    same instant, so a plate that collected results as they arrived rather than by index would
    scramble exactly here. The fragment counts are read back per row, because obs_names in the right
    order over rows in the wrong one is the failure that looks correct.

    **The other arm is `spawn`, which this module refuses.** Told that fork is not on offer, the
    counter must count the plate itself rather than pickle the annotation into a worker per cell —
    proved by every cell's pid being this process's, which a forked plate could not report — and it
    must produce the same object it produces any other way.

    `_count_cell` is where both arms hook in because it is the one thing a worker does; patching it
    is what lets the barrier and the pid land inside the counting rather than beside it. A fork
    inherits the patch along with everything else, which is the same property the annotation reaches
    a worker by.
    """
    import multiprocessing
    import os

    from seqforge.workflows.umite import count as counter

    db, two = _plate(tmp_path)
    # Four cells of two shapes, alternating: the deep cell is 13 fragments and the shallow one is 1,
    # so a plate collected by completion rather than by index reads back as [1, 13, 1, 13].
    plate = [(f"cell_{i}", bam) for i, (_id, bam) in enumerate(two * 2)]
    depths = [len(_PLATE), 1, len(_PLATE), 1]
    annotation = read_annotation(db)
    serial = count_plate(plate, annotation)
    serial.write_h5ad(tmp_path / "serial.h5ad")
    real = counter._count_cell

    if "fork" in multiprocessing.get_all_start_methods():
        barrier = multiprocessing.get_context("fork").Barrier(len(plate))

        def in_lockstep(bam: Path, annotation: Any) -> Any:
            counted = real(bam, annotation)
            barrier.wait(timeout=60)
            return counted

        monkeypatch.setattr(counter, "_count_cell", in_lockstep)
        pooled = count_plate(plate, annotation, workers=len(plate))
        monkeypatch.undo()

        assert list(pooled.obs_names) == [sample for sample, _ in plate]
        assert [int(n) for n in _frame(pooled.obs)[N_FRAGMENTS]] == depths
        pooled.write_h5ad(tmp_path / "pooled.h5ad")
        assert (tmp_path / "pooled.h5ad").read_bytes() == (tmp_path / "serial.h5ad").read_bytes()

    monkeypatch.setattr(multiprocessing, "get_all_start_methods", lambda: ["spawn", "forkserver"])
    counted_in: list[int] = []

    def note_the_process(bam: Path, annotation: Any) -> Any:
        counted_in.append(os.getpid())
        return real(bam, annotation)

    monkeypatch.setattr(counter, "_count_cell", note_the_process)
    unforked = count_plate(plate, annotation, workers=len(plate))

    assert counted_in == [os.getpid()] * len(plate), (
        "the counter forked where the platform offers no fork, so it would have spawned — which "
        "pickles the annotation into a worker for every cell of the plate"
    )
    unforked.write_h5ad(tmp_path / "unforked.h5ad")
    assert (tmp_path / "unforked.h5ad").read_bytes() == (tmp_path / "serial.h5ad").read_bytes()


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


# ---- one chimeric BAM back into the single-assembly BAMs it would have been ----------------------
#
# Both ends of the round-trip are hand-written from ONE declared contig table, spelled two ways: the
# chimeric BAM the splitter reads, and the single-assembly header its output is compared against. No
# aligner, no built Chimera, no genome store — which is why this is a row on the plate above rather
# than an `external` test nobody runs, and it is also the limit of what it proves. That STAR produces
# the BAM shape assumed here is not under test; the aligner is not the thing being exercised.
#
# **The set of assertions is measured rather than argued.** A prototype wrote a splitter to this
# contract, broke it eighteen ways on purpose, and kept only the assertions that were the ONLY thing
# catching a defect. Four candidates are deliberately absent. Routing — "every uniquely-placed read
# lands in the Component it came from", the headline bar — catches nothing once the name split
# is liulab-genome's: a record's Component is then a pure function of its RNAME, and every
# constructible misrouting refuses or crashes before any assertion runs. Reads-in-equals-kept-plus-
# dropped is strictly subsumed by the per-reason drop counts. Mate-in-same-Component is the
# splitter's own runtime refusal, which fires before an output exists to assert over. And nothing
# asserts an ambiguous route, because assigning a spanning template to its best-scoring Component
# was rejected outright — there is no such route to have an opinion about.
#
# What the eighteen could not reach is a check that has stopped firing, since a splitter that never
# refuses passes every assertion over its outputs. So the two end-of-run checks are each shown going
# red as well, against a BAM doctored to lose a population no aligner would lose.


@dataclass(frozen=True)
class _Component:
    """One assembly a Chimera is built from, and the chromosomes it declares IN ITS OWN ORDER."""

    name: str
    chromosomes: tuple[tuple[str, int], ...]  # (name, length)


@dataclass(frozen=True)
class _Chimera:
    """Several `_Component`s as one reference: the contig table this section spells two ways."""

    components: tuple[_Component, ...]

    @property
    def separator(self) -> str:
        """The underscore run these names force — DERIVED by liulab-genome, never typed here.

        Typing it would make the `dub` shape below prove nothing: its whole point is that a
        Component whose own chromosome names carry `__` forces a longer run, and a fixture that
        asserts the separator it also hardcoded cannot notice that.
        """
        return derive_separator({c.name: [n for n, _ in c.chromosomes] for c in self.components})

    @property
    def sq(self) -> list[dict[str, Any]]:
        """The chimeric @SQ: Components in sorted order, each block in its own declared order."""
        return [
            {"SN": suffixed(chrom, c.name, self.separator), "LN": length}
            for c in sorted(self.components, key=lambda c: c.name)
            for chrom, length in c.chromosomes
        ]

    def single_assembly_sq(self, component: str) -> list[dict[str, Any]]:
        """What a run against the bare Component alone would have written — the comparison artifact."""
        one = next(c for c in self.components if c.name == component)
        return [{"SN": chrom, "LN": length} for chrom, length in one.chromosomes]


# `chrX` before `chrM` is real ce11 order and is NOT alphabetical, deliberately: an @SQ block that
# happened to be sorted would make the ORDER half of the header assertion decorative, since a
# splitter that sorted its output would agree with it anyway.
_TINY_CE = _Component("tinyCe", (("chrI", 4000), ("chrII", 3000), ("chrX", 2500), ("chrM", 900)))
_TINY_EC = _Component("tinyEc", (("ctg1", 1200), ("ctg2", 800)))
#: The same bacterium with chromosome names that ALREADY carry `__`, which forces the Chimera's
#: separator to `___`. Load-bearing for a narrower reason than it looks: a splitter that hardcodes
#: `__` on such a name does not raise — it still recovers the right Component and corrupts only the
#: bare name, by one trailing underscore — so it is sprung by the HEADER assertion and by nothing
#: else. The plain shape springs the opposite bug: a splitter hardcoding `___`, written by whoever
#: only ever tested on this one, refuses on ordinary names, which is why `plain` only has to be run.
_TINY_EC_DUB = _Component("tinyEcDub", (("ctg__1", 1200), ("ctg__2", 800)))

_CHIMERAS = {
    "plain": _Chimera((_TINY_CE, _TINY_EC)),
    "dub": _Chimera((_TINY_CE, _TINY_EC_DUB)),
}


def _chimeric_plate(chimera: _Chimera) -> tuple[_Fragment, ...]:
    """One fragment of every kind in every Component, plus one template that never aligned at all.

    Nothing here is computed from the splitter's own arithmetic: each row states its kind, and the
    counts the assertions below carry are read off this list by hand — two records per fragment, one
    for the pair that never aligned and three for the one that left a spare dead mate behind, so a
    two-Component shape is 21 kept and 17 dropped out of 38 records in.

    **Three half-mapped fragments per Component, and the 2:1 split between the mate sides is the
    load-bearing part.** They make each Component's first-mate and second-mate counts differ — five
    against four — which is what a real plate does and what used to make this whole step refuse. Two
    on one side and one on the other so that a check subtracting one side's singletons from both, or
    from only its own side, is red rather than accidentally right.

    **And one more, on the first Component only, whose dead mate points at the second.** A pilot
    refused a healthy cell on a per-Component comparison of the two half-mapped derivations, because
    90 of its 5440 such fragments had their survivor on one organism and their mate pointer on the
    other — every one of them multiply placed, none of them unique. Exactly one row here, and not one
    per Component, because a symmetric pair would cancel: what has to be visible is the two
    attributions differing per Component.

    **And one that is not half mapped at all, yet leaves a placeless record behind.** The same pilot
    then refused on the TOTAL, 33026 survivors against 33027 pointers, and the single extra was a
    fully mapped three-locus pair with a third record: its second mate again, unmapped, pointing at a
    contig neither alignment touched. It is another locus of the same multi-mapping set, so it
    belongs to no half-mapped fragment and answers no survivor. That is why the two counts are not
    one population and why what is asserted is a BOUND — and the row is here so that the excess is
    one rather than zero, which is the difference between a check that tolerates and one that counts.
    """
    separator = chimera.separator
    fragments: list[_Fragment] = []
    for component in chimera.components:
        first = suffixed(component.chromosomes[0][0], component.name, separator)
        last = suffixed(component.chromosomes[-1][0], component.name, separator)
        fragments += [
            # Kept: mapped, uniquely placed, primary. The second one is on the LAST contig its
            # Component declares, so a tid remap that only ever gets index zero right goes red.
            _Fragment(f"{component.name}_unique_first", first, 100, 160),
            _Fragment(f"{component.name}_unique_last", last, 200, 260),
            # Also kept, and MARKED rather than dropped: the hit-count tag it already carries is
            # what lets the counter separate it later, so nothing here needs a second file.
            _Fragment(f"{component.name}_multi", first, 300, 360, hits=2),
            # Dropped, one category each, and neither can occur under the flags the aligner runs
            # with today — counted apart so that a flag moving says so instead of moving reads.
            _Fragment(f"{component.name}_secondary", first, 400, 460, extra_flags=_SECONDARY),
            _Fragment(f"{component.name}_supp", first, 500, 560, extra_flags=_SUPPLEMENTARY),
            # Half aligned: the survivor is kept and counted as a singleton, its dead mate is
            # dropped as unmapped and counted again as this Component's second derivation of the
            # same number.
            _Fragment(f"{component.name}_half_first_a", first, 600, 660, mate_unmapped=True),
            _Fragment(f"{component.name}_half_first_b", last, 700, 760, mate_unmapped=True),
            _Fragment(
                f"{component.name}_half_second", first, 800, 860, mate_unmapped=True, survivor=2
            ),
        ]
    here, there = chimera.components[0], chimera.components[1]
    fragments.append(
        # Half aligned AND multiply placed, so its two ends may name different organisms: the
        # survivor is `here`'s singleton and the dead mate is counted under `there`. Neither
        # attribution is wrong and the totals still close, which is the whole amendment.
        _Fragment(
            f"{here.name}_half_multi_across",
            suffixed(here.chromosomes[0][0], here.name, separator),
            900,
            960,
            hits=2,
            mate_unmapped=True,
            dead_mate_contig=suffixed(there.chromosomes[0][0], there.name, separator),
        )
    )
    fragments.append(
        # Fully mapped, multiply placed, and carrying a spare dead mate from another locus of its
        # set — pointing at a SECOND contig of its own Component, which is where the pilot's was.
        # Both mates are kept and neither is a singleton; the third record is dropped as unmapped
        # and counted as a pointer nothing is owed, which is the excess.
        _Fragment(
            f"{here.name}_multi_stray_pointer",
            suffixed(here.chromosomes[0][0], here.name, separator),
            1000,
            1060,
            hits=3,
            stray_pointer_contig=suffixed(here.chromosomes[1][0], here.name, separator),
        )
    )
    fragments.append(_Fragment("never_aligned", "", 0, 0, unmapped=True))
    return tuple(fragments)


def _chimeric_bam(path: Path, chimera: _Chimera, fragments: Sequence[_Fragment]) -> Path:
    """The aligner's output: a coordinate-sorted chimeric BAM whose @PG/@CO name the Chimera."""
    name = "_".join(sorted(c.name for c in chimera.components))
    return _synthetic_bam(
        path,
        fragments,
        header={
            "HD": {"VN": "1.6", "SO": "coordinate"},
            "SQ": chimera.sq,
            "PG": [{"ID": "STAR", "PN": "STAR", "VN": "2.7.11b", "CL": f"STAR --genomeDir {name}"}],
            "CO": [f"user command line: STAR --genomeDir {name}"],
        },
    )


@dataclass(frozen=True)
class _Round:
    """One round-trip: what went in, where it came out, and what the splitter said it did."""

    chimera: _Chimera
    source: Path
    fragments: tuple[_Fragment, ...]
    outputs: dict[str, Path]
    stats: SplitStats


def _split(tmp_path: Path, label: str) -> _Round:
    """Build one Chimera's BAM, split it into every Component, and hand back what to read."""
    chimera = _CHIMERAS[label]
    fragments = _chimeric_plate(chimera)
    bam = _chimeric_bam(tmp_path / f"{label}.bam", chimera, fragments)
    outputs = {c.name: tmp_path / f"{label}.{c.name}.bam" for c in chimera.components}
    stats = split_chimera(bam, outputs, chimera.separator)
    return _Round(chimera, bam, fragments, outputs, stats)


def _doctored(source: Path, target: Path, gone: Callable[[Any], bool]) -> Path:
    """A copy of a chimeric BAM with every record `gone` is true of simply absent from it.

    Both callers build a file no aligner writes, and that is the point: the two end-of-run checks
    stand against a file that has quietly lost a whole population, with the header and every
    surviving record still perfectly well formed and nothing on the page saying anything left.
    """
    with pysam.AlignmentFile(str(source), "rb") as inp:
        with pysam.AlignmentFile(str(target), "wb", header=inp.header) as out:
            for record in inp.fetch(until_eof=True):
                if not gone(record):
                    out.write(record)
    return target


def _kept(fragments: Sequence[_Fragment]) -> list[_Fragment]:
    """The plate's mapped, primary fragments — the keep rule, read off the rows.

    `hits` is deliberately not consulted: a fragment placed at more than one locus is routed by its
    representative record's Component and marked, not dropped, so leaving that clause in would let
    this helper agree with a splitter that had never widened.
    """
    return [f for f in fragments if not f.unmapped and not f.extra_flags]


@pytest.mark.parametrize("label", sorted(_CHIMERAS))
def test_each_component_comes_back_with_the_header_a_single_assembly_run_would_have_written(
    tmp_path: Path, label: str
) -> None:
    """The bar: @SQ names, lengths and ORDER against a hand-written single-assembly header, and @HD.

    Names because a suffix left on makes the output unusable by everything the user owns, which is
    the entire reason this verb exists. Lengths because the split takes them off the BAM's own @SQ
    rather than a `chrom.sizes` that could have drifted underneath it. Order because a Component's
    contigs must arrive as its own assembly declares them, and a splitter that sorted them would
    look right on a table that happened to be alphabetical — this one is not.

    @HD travels untouched, and dropping it is what stops the BAM declaring itself coordinate-sorted:
    an unsorted-looking BAM is not an error anybody sees, it is an index build that fails later.
    """
    round_trip = _split(tmp_path, label)
    with pysam.AlignmentFile(str(round_trip.source), "rb") as chimeric:
        chimeric_hd = chimeric.header.to_dict()["HD"]

    for component, path in round_trip.outputs.items():
        alone = _synthetic_bam(
            tmp_path / f"{label}.{component}.single.bam",
            (),
            header={
                "HD": {"VN": "1.6", "SO": "coordinate"},
                "SQ": round_trip.chimera.single_assembly_sq(component),
            },
        )
        with (
            pysam.AlignmentFile(str(path), "rb") as split,
            pysam.AlignmentFile(str(alone), "rb") as single,
        ):
            assert split.header.to_dict()["SQ"] == single.header.to_dict()["SQ"], (
                f"{component}'s @SQ is not what a run against the bare assembly would have written"
            )
            assert split.header.to_dict()["HD"] == chimeric_hd


@pytest.mark.parametrize("label", sorted(_CHIMERAS))
def test_every_kept_record_resolves_to_the_chromosome_it_actually_sits_on(
    tmp_path: Path, label: str
) -> None:
    """The binary reference dictionary is rewritten, not just the text header.

    A record names its reference by INDEX into that dictionary, so the one corruption nothing else
    here can see is a remap that is off by one and still in range: the header reads perfectly, every
    record resolves, and every read is on the wrong chromosome. Resolving each record's name through
    the output's own header is what catches it — and it only catches it on the SECOND Component,
    whose indexes are the ones that had to move at all.

    It is also where a multiply-placed fragment's two mates are shown ARRIVING, both of them, in the
    output for the Component its records name: keeping them is what lets the counter separate that
    population from the hit-count tag rather than from a file this step would otherwise write.
    """
    round_trip = _split(tmp_path, label)
    separator = round_trip.chimera.separator

    for component, path in round_trip.outputs.items():
        expected = sorted(
            placed
            for frag in _kept(round_trip.fragments)
            if split_suffixed(frag.contig, separator)[1] == component
            # Both mates, and they are on the same chromosome: the keep rule is per record, so an
            # output missing one of a pair is a different failure than an output missing a name.
            # One mate only where one mate is all the aligner placed.
            for placed in [(frag.name, split_suffixed(frag.contig, separator)[0])]
            * (1 if frag.mate_unmapped else 2)
        )
        with pysam.AlignmentFile(str(path), "rb") as split:
            assert sorted((r.query_name, r.reference_name) for r in split) == expected


@pytest.mark.parametrize("label", sorted(_CHIMERAS))
def test_a_singleton_is_subtracted_from_its_own_side_before_the_mates_are_compared(
    tmp_path: Path, label: str
) -> None:
    """A healthy plate whose first and second mates DIFFER is split rather than refused.

    This asserted mate counts were equal and it was wrong on real data: where only one mate aligned
    the survivor is kept — correctly, it is a mapped primary alignment — and no partner is in the
    file to balance it. The arithmetic closed exactly, with no residual, and a pilot lost sixteen of
    sixteen cells to it. What is compared now is the PAIRED REMAINDER, each side less its own
    singletons, which is still a per-record flag test and two more counters: nothing is held.

    Five first mates and four second per Component, read off the plate — seven and five on the
    first, which also holds the half-mapped fragment whose dead mate points at the other organism
    and the whole pair that left a spare dead mate behind — and the split returning at all is half
    the claim.
    """
    round_trip = _split(tmp_path, label)
    stats = round_trip.stats
    here, there = (c.name for c in round_trip.chimera.components)
    assert stats.read1 == {here: 7, there: 5}
    assert stats.read2 == {here: 5, there: 4}
    # Three of those records have no partner, two on the first side and one on the second, so what
    # is left on either side is three whole pairs. The cross-pointing fragment is a fourth on the
    # first Component, and it is a first mate, so that side's remainder is unmoved; the spare-pointer
    # fragment is a whole pair and adds one to each side, so it is unmoved too.
    assert stats.singletons == {here: 4, there: 3}


@pytest.mark.parametrize("label", sorted(_CHIMERAS))
def test_the_summary_accounts_for_every_record_the_split_was_handed(
    tmp_path: Path, label: str
) -> None:
    """The whole payload at once: what was kept, what that was made of, and why the rest went.

    Three discard categories under one keep rule, each counted apart so the rule degrades legibly if
    a flag moves — a category that starts firing says so here rather than reads quietly going
    missing. Multiply-placed is NOT one of them any more: those records are in an output, marked by
    the hit-count tag they carry, and the count of them is an account of what a Component KEPT
    rather than of what it lost.

    Kept plus dropped is exactly the records that came in; `multiplaced`, `singletons` and
    `mate_pointed` are subsets of it — the first two of `kept`, the third of the unmapped drops —
    and deliberately do not enter that sum, nor do `excess_pointers` and `unanswered_survivors`,
    which are the two signs of one difference between two of them, nor
    `multiplaced_singletons`, which is the intersection of two of them and the bound the second sign
    is allowed to reach. Every number is read off the plate's own rows by hand.

    **`singletons` and `mate_pointed` are NOT one population, at any granularity**, which is what
    this plate's last two rows are for. Per Component the cross-pointing row separates them: four
    survivors on the first against four dead mates pointing at it, three against four on the second.
    In TOTAL the spare-pointer row separates them: seven survivors against eight pointers, because a
    fully mapped multi-locus fragment left a dead half in the file that no survivor is owed. A real
    cell refused on each of those shapes in turn, so the split returning at all is as load-bearing
    as any number below it, and `excess_pointers` is that difference reported rather than tolerated.
    """
    round_trip = _split(tmp_path, label)
    here, there = (c.name for c in round_trip.chimera.components)
    assert round_trip.stats.to_dict() == {
        "seqforge": seqforge_version,
        "separator": round_trip.chimera.separator,
        "records_in": 38,
        "kept": {here: 12, there: 9},
        "read1": {here: 7, there: 5},
        "read2": {here: 5, there: 4},
        "multiplaced": {here: 5, there: 2},
        "singletons": {here: 4, there: 3},
        "mate_pointed": {here: 4, there: 4},
        "excess_pointers": 1,
        # Nothing is short on this plate, and exactly one of its survivors was placed at more than
        # one locus — the row whose dead mate points at the other organism. That one is the whole of
        # what a shortfall here would be allowed to be.
        "unanswered_survivors": 0,
        "multiplaced_singletons": 1,
        # Three unmapped records per Component are the dead mates of its half-mapped pairs, one more
        # is the cross-pointing fragment's, one is the spare the whole multi-locus pair left behind,
        # and the odd one is the template neither mate of which aligned anywhere.
        "dropped": {"unmapped": 9, "secondary": 4, "supplementary": 4},
    }


def test_the_paired_remainder_still_refuses_an_output_that_was_genuinely_halved(
    tmp_path: Path,
) -> None:
    """The replacement check has teeth: a check that cannot be shown going red is not a check.

    Subtracting singletons is what stops a healthy plate refusing, and the risk of the change is
    that it stops refusing anything at all. So the second mates are removed from the file with
    nothing anywhere saying they left — not a mate that did not align, which announces itself on the
    survivor's flag, but records missing from a file that still claims they are there. That is the
    halving the check was written for, and it still fires.
    """
    chimera = _CHIMERAS["plain"]
    bam = _chimeric_bam(tmp_path / "plain.bam", chimera, _chimeric_plate(chimera))
    halved = _doctored(
        bam, tmp_path / "halved.bam", lambda r: bool(r.is_read2 and not r.is_unmapped)
    )
    outputs = {c.name: tmp_path / f"{c.name}.bam" for c in chimera.components}
    with pytest.raises(SplitError, match="first and second mates"):
        split_chimera(halved, outputs, chimera.separator)


@pytest.mark.parametrize(
    ("case", "gone"),
    [
        # What an aligner never asked to emit unmapped records leaves behind: zero against seven,
        # where one survivor was placed at more than one locus and six were not. This is the failure
        # the check exists for, and it shipped broken once.
        ("every", lambda r: bool(r.is_unmapped and not r.mate_is_unmapped)),
        # And four of them only, so that the bound is shown to be a bound rather than a test for
        # zero: four against seven refuses on a file that still has half the population in it,
        # because the three that are short are three more than multi-locus emission can account for.
        (
            "some",
            lambda r: bool(
                r.is_unmapped
                and not r.mate_is_unmapped
                and r.query_name.endswith(("_half_first_a", "_half_first_b"))
            ),
        ),
    ],
)
def test_the_placeless_records_are_required_to_cover_the_survivors_rather_than_assumed(
    tmp_path: Path, case: str, gone: Callable[[Any], bool]
) -> None:
    """A uniquely placed survivor is owed a placeless record, and a SHORTFALL past the multiply
    placed ones is a refusal.

    A survivor carries the mate-unmapped flag; the mate it names has no placement of its own and
    reaches a Component only through its mate pointer. The plate above proves what the check may no
    longer claim — the two counts are not one population at any granularity, since a multiply-placed
    fragment's two ends can name different organisms, a fragment that aligned whole can still leave
    a dead half behind, and a multiply-placed survivor can be the only record of its fragment in the
    file — so what needs its own case is what is still asserted, and it is the placeless records
    going missing beyond that: an aligner that was never asked to emit them writes a file exactly
    like the first one here. Every survivor removed from here is uniquely placed, which is what makes
    the shortfall inexplicable and not merely large; the plate holds one multiply-placed survivor, so
    a file one short of its survivors is accepted and these are three and seven short. That is also
    what makes the two counts INDEPENDENT rather than one number read twice. The mate counts still
    balance in both, because nothing a kept record carries has changed; only the placeless side
    falls, and the refusal is the difference between this check and comparing raw mate counts.
    """
    chimera = _CHIMERAS["plain"]
    bam = _chimeric_bam(tmp_path / "plain.bam", chimera, _chimeric_plate(chimera))
    silent = _doctored(bam, tmp_path / f"silent_{case}.bam", gone)
    outputs = {c.name: tmp_path / f"{c.name}.bam" for c in chimera.components}
    with pytest.raises(SplitError, match="counted two ways"):
        split_chimera(silent, outputs, chimera.separator)


def test_a_multiply_placed_survivor_whose_dead_mate_was_never_written_is_counted_not_refused(
    tmp_path: Path,
) -> None:
    """The other sign of the same emission artifact: a SHORTFALL within the multiply-placed
    survivors is split and measured.

    Three cells of a 784-cell plate refused with exactly one survivor unanswered, and in each the
    survivor was mapped, primary, flagged mate-unmapped and placed at more than one locus, with no
    record of its dead mate anywhere in the file — against tens of thousands of placeless records
    the aligner had plainly written. That is the mirror of the row the plate above already carries:
    one representative of a locus set is emitted, so a locus that WAS emitted can leave a dead half
    behind that no survivor is owed, and a locus that was NOT emitted takes its dead half with it and
    leaves a survivor nothing answers. Both are one number with two signs, which is why this file
    needs TWO such fragments to show a shortfall at all — the first is cancelled by the spare dead
    mate the plate's fully mapped multi-locus pair leaves behind, and only the second runs the total
    short. Built as the aligner writes it rather than by deleting records from a healthy file: what
    is claimed here is that a real BAM is accepted, and a doctored one could not make that claim.
    """
    chimera = _CHIMERAS["plain"]
    here = chimera.components[0]
    contig = suffixed(here.chromosomes[0][0], here.name, chimera.separator)
    orphaned = tuple(
        _Fragment(
            f"{here.name}_half_multi_orphan_{n}",
            contig,
            1100 + 100 * n,
            1160 + 100 * n,
            hits=2,
            mate_unmapped=True,
            dead_mate_absent=True,
        )
        for n in range(2)
    )
    bam = _chimeric_bam(tmp_path / "orphaned.bam", chimera, (*_chimeric_plate(chimera), *orphaned))
    outputs = {c.name: tmp_path / f"{c.name}.bam" for c in chimera.components}

    stats = split_chimera(bam, outputs, chimera.separator)

    # Nine survivors against eight placeless records, and the shortfall is reported on its own line
    # rather than tolerated inside the check — which is the same treatment the excess gets, and the
    # reason the excess reads zero here instead of the one the plate alone would have shown.
    assert stats.singletons == {here.name: 6, chimera.components[1].name: 3}
    assert (stats.unanswered_survivors, stats.excess_pointers) == (1, 0)
    # ...against a bound of three, which is what makes one short explicable rather than merely
    # small: the two rows above plus the plate's own multiply-placed survivor.
    assert stats.multiplaced_singletons == 3
    # And the survivors are IN the output — a fragment whose dead mate was never written is still a
    # mapped primary alignment, and the rarer organism is where dropping it would cost most.
    with pysam.AlignmentFile(str(outputs[here.name]), "rb") as split:
        assert {f.name for f in orphaned} <= {record.query_name for record in split}


def test_a_component_the_caller_named_no_output_for_is_refused_rather_than_dropped(
    tmp_path: Path,
) -> None:
    """A partial request is a refusal, because served it looks exactly like a request that was met.

    The reads on the un-named Component go nowhere and nothing says so: the run exits 0, writes the
    file it was asked for, and reads-in-equals-reads-out stops closing with no record of when.
    """
    chimera = _CHIMERAS["plain"]
    bam = _chimeric_bam(tmp_path / "plain.bam", chimera, _chimeric_plate(chimera))
    with pytest.raises(SplitError, match="tinyEc"):
        split_chimera(bam, {"tinyCe": tmp_path / "ce.bam"}, chimera.separator)


def test_an_sq_name_that_will_not_split_at_the_recorded_separator_is_refused(
    tmp_path: Path,
) -> None:
    """A chromosome the reference cannot explain is a refusal, and it is made from the header alone.

    Skipping it instead would put reads in whichever output happened to be open, or in none, on a
    BAM that was mapped to something other than the Chimera named. The check is up front because the
    whole @SQ block is readable before the first record, so nothing has been written when it fires.
    """
    chimera = _CHIMERAS["plain"]
    header = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        # A contig nobody suffixed, which is what a reference the named Chimera did not build looks
        # like from in here — the same shape a chimeric BAM would take on if a contig were appended.
        "SQ": [*chimera.sq, {"SN": "chrUn_unsuffixed", "LN": 500}],
    }
    bam = _synthetic_bam(tmp_path / "stray.bam", (), header=header)
    outputs = {c.name: tmp_path / f"{c.name}.bam" for c in chimera.components}
    with pytest.raises(SplitError, match="chrUn_unsuffixed"):
        split_chimera(bam, outputs, chimera.separator)


def test_a_template_whose_mate_sits_on_another_component_is_refused_by_name(tmp_path: Path) -> None:
    """The splitter's own runtime check, and the reason it exists is the message rather than the stop.

    The design rests on a fact read off the aligner's source that nobody has yet watched hold on a
    real chimera: both mates of a template carry one `NH` and sit on one chromosome, which is what
    lets the filter be stateless — no name sort, no buffer. If that is ever false, the record's mate
    points into a reference dictionary this output does not have, and the split would die on a lookup
    naming neither the read nor the organisms involved.

    So it is checked per record and refused by name. This is deliberately NOT the output assertion
    the map originally wanted — asserting mates land together proves nothing, because the refusal
    fires before any output exists to inspect. What is worth a case is that the refusal FIRES and
    says what it saw. It cannot prove the aligner never writes such a template; nothing cheap can.
    """
    chimera = _CHIMERAS["plain"]
    ce, ec = chimera.components
    spanning = _Fragment(
        "worm_body_bacterial_mate",
        suffixed(ce.chromosomes[0][0], ce.name, chimera.separator),
        100,
        160,
        mate_contig=suffixed(ec.chromosomes[0][0], ec.name, chimera.separator),
    )
    bam = _chimeric_bam(tmp_path / "spanning.bam", chimera, (spanning,))
    outputs = {c.name: tmp_path / f"{c.name}.bam" for c in chimera.components}
    with pytest.raises(SplitError) as refusal:
        split_chimera(bam, outputs, chimera.separator)
    # The read AND both Components: a diagnosis, not a stop. Named because the whole value of the
    # check is turning an opaque failure into a sentence someone can act on.
    assert {spanning.name, ce.name, ec.name} <= set(re.findall(r"[\w.]+", str(refusal.value)))


# ---- the plate object's second reader: what `seqforge report` gets out of it ----------------------
#
# These drive `read_pipeline_stats` and they live HERE, in the counter's section, for the reason
# ADR-0025 gives for the reader itself: what an `obs` column is called is the writer's fact, so the
# claim under test is that the columns `count_plate` writes are the columns the page reads. Every one
# of them therefore goes through the real writer over the synthetic plate above, rather than through
# a hand-built AnnData that could only ever agree with itself.


def _plate_results(tmp_path: Path, *, bundled: Sequence[str] = ("cell_a", "cell_b")) -> Path:
    """A finished `map/star-umi` run on disk: the fan-in h5ad, plus one QC bundle per cell.

    `bundled` is which cells got as far as writing that bundle — a preempted plate has cells the
    counter measured and whose own artifact did not survive, which is exactly the union case. It is
    ONE list because a finished cell leaves ONE file: the extraction summary and the alignment log
    used to be two more artifacts here, and they are now two keys inside this one.
    """
    db, cells = _plate(tmp_path)
    results = tmp_path / "results"
    write_umi_counts(cells, db, results / PLATE_H5AD)
    for sample in bundled:
        _bundled_cell(results, tmp_path / "staging", sample)
    return results


def test_the_plates_read_fates_reach_the_report_beside_the_per_cell_qc_bundle(
    tmp_path: Path,
) -> None:
    """The counter's own verdicts are on the page, and they arrive from the artifact that has them.

    A cell's QC bundle says what its FASTQs held and what the aligner did with them, and stops there
    — it cannot say how many fragments reached no gene, or were ambiguous, or were dropped as
    multimappers, because the counter had not run when the cell's chain finished. Those are in the
    plate object's `obs`, one row per cell, and until this landed they were written and read by
    nobody.

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
    # 17 fragments, of which 2 unmapped, 4 multimapping, 2 no-feature and 2 ambiguous.
    assert cell_a[N_FRAGMENTS].value == len(_PLATE)
    assert cell_a["no_feature"].value == pytest.approx(2 / len(_PLATE))
    assert cell_a["ambiguous"].value == pytest.approx(2 / len(_PLATE))
    assert cell_a["unmapped"].value == pytest.approx(2 / len(_PLATE))
    assert cell_a["multimapping"].value == pytest.approx(4 / len(_PLATE))
    # Saturation arrives from the same object under the key the droplet page uses, which is the whole
    # point of computing it here rather than inventing a second word for it.
    assert cell_a[SATURATION].value == pytest.approx(1 - 3 / 5)
    # ...and what the cell yielded, which nothing above reports: three molecules over two genes,
    # both counted over `umi_combined`.
    assert (cell_a[N_UMIS].value, cell_a[GENES_DETECTED].value) == (3, 2)
    # ONE DERIVATION, not two that happen to sit near each other. Saturation is one minus these
    # molecules over the five fragments that reached a gene carrying a UMI, so the two columns of
    # this row have to close on each other — a second `sum` over the matrices would let them drift
    # and nothing on the page would say so.
    assert cell_a[SATURATION].value == pytest.approx(1 - cell_a[N_UMIS].value / 5)
    # Both name the region they were counted over. An exonic total and one that includes introns
    # are two measurements, and the word in the label is the only thing keeping them apart.
    assert cell_a[GENES_DETECTED].label == "Genes (combined)"
    assert "combined" in cell_a[N_UMIS].label
    # Neither is graded, for the reason nothing else on this page is: nobody has measured how many
    # molecules or genes a cell SHOULD yield, and the chimeric twin renders both once per Component,
    # where one bar would grade a bacterium against a worm's expectations on one row.
    assert {_levels(stats.samples[0])[key] for key in (N_UMIS, GENES_DETECTED)} == {"none"}
    # Every fate the counter records has a column and a label a human can read, checked against
    # `FATES` itself rather than against a list here — a fifth fate must not reach the page unnamed.
    assert set(FATES) <= set(cell_a)
    assert all(label for _, label in stats.columns)
    # And none of them is graded. `map/star-umi` cross-checks nothing on the stated argument that a
    # bar for "too many fragments hit no feature" is a number nobody has measured; an ungraded column
    # says that out loud, where an invented threshold would tint a page nobody could act on.
    assert {_levels(stats.samples[0])[fate] for fate in FATES} == {"none"}
    assert stats.findings == []
    # Well past the width at which the report folds a table behind a control, so which of these
    # survives the fold is a decision: the chemistry readout, mapping, depth, the fate that
    # implicates the gene model, and what the cell yielded — the two numbers a reader quotes when
    # asked whether a well worked at all, which no share of a failure can answer.
    assert {m.key for m in stats.samples[0].metrics if m.headline} == {
        "umi_tagged",
        "uniquely_mapped",
        "unmapped_too_short",
        N_FRAGMENTS,
        "no_feature",
        N_UMIS,
        GENES_DETECTED,
    }


def test_a_cell_the_counter_measured_is_reported_even_with_no_alignment_log_of_its_own(
    tmp_path: Path,
) -> None:
    """The join is a UNION, because a missing per-cell artifact does not unmake a counted cell.

    An intersection is the tempting shape — merge the fan-in into the rows that landed — and it
    silently shortens the plate: a cell whose QC bundle was lost to a preemption still has a row in
    the object, a fragment count and a column in every matrix, and reporting it as absent would say
    the counter never saw it. `n_found` is how many cells one source or the other answered for,
    which is what "how much landed" means once landing can happen twice.
    """
    results = _plate_results(tmp_path, bundled=["cell_a"])

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
    _bundled_cell(results, tmp_path / "staging", "cell_a")

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


def test_the_absorbed_summaries_still_reach_the_page_as_chapters_of_one_row(
    tmp_path: Path,
) -> None:
    """The extraction's numbers are inside the cell's bundle now, and a cell is still ONE row.

    Each chapter speaks about a different step and none is a version of another: the extraction says
    what the FASTQs held and how much of it carried a tag, the aligner's log says what it did with
    the reads it was then handed, and the plate object says what the counter did with the fragments.
    A page that carried them separately would show every cell two or three times.

    The tagged fraction is what makes this worth asserting after the collapse. It is the per-cell
    readout of whether the chemistry behaved and no other artifact on a finished plate carries it —
    the aligner never saw an untagged read as anything but a read — and its own file is now
    reclaimed, so if the bundle did not carry it up it would reach the page from nowhere at all.
    Columns read in pipeline order: the order the bundle's reader assembles them in, then the fan-in.
    """
    results = _plate_results(tmp_path)

    stats = read_pipeline_stats("map/star-umi", results, ["cell_a", "cell_b"])

    assert stats is not None and stats.complete
    assert [s.sample_id for s in stats.samples] == [
        "cell_a",
        "cell_b",
    ]  # two cells, not four or six
    cell_a = _by_key(stats.samples[0])
    assert cell_a["umi_tagged"].value == pytest.approx(0.27)
    assert cell_a["extract_fragments"].value == 100
    assert cell_a["umi_anchor_drift"].value == pytest.approx(1 / 27)
    # ...beside both other halves, on the same row.
    assert "uniquely_mapped" in cell_a and set(FATES) <= set(cell_a)
    keys = [key for key, _ in stats.columns]
    assert keys.index("umi_tagged") < keys.index("uniquely_mapped") < keys.index("no_feature")


def test_a_cell_whose_downstream_steps_never_ran_is_not_counted_as_finished(
    tmp_path: Path,
) -> None:
    """ "N of M finished" counts the artifact written LAST, and the failed arm is why it has to.

    That count used to be read off the aligner's own log, which STAR writes the moment it stops
    aligning — so a chimeric run whose split then refused for every cell rendered "16 of 16 cells
    finished" over a results directory holding no matrix at all. The bundle is written downstream of
    the whole per-cell chain, so a cell that mapped and got no further is honestly not finished.

    The absorbed originals are left on disk for `cell_b` deliberately: what is asserted is that the
    reader does not fall back to them. Emptying that directory instead would pass while the registry
    still counted a log the aligner wrote before any of the work that matters.
    """
    results = tmp_path / "results"
    _bundled_cell(results, tmp_path / "staging", "cell_a", split=_CELL_SPLIT)
    # `cell_b` mapped and then its split refused, so its bundle rule never ran.
    _finished_cell(results / "cell_b", sample="cell_b")

    stats = read_pipeline_stats("map/star-umi-chimera", results, ["cell_a", "cell_b"])

    assert stats is not None and not stats.complete
    assert (stats.n_found, stats.n_expected) == (1, 2)
    assert [s.sample_id for s in stats.samples] == ["cell_a"]
    assert (results / "cell_b" / STAR_FINAL_LOG).is_file(), (
        "the fallback this test refuses has to be on disk, or it proves nothing"
    )


def test_an_unreadable_bundle_costs_its_own_columns_and_names_the_file_it_could_not_read(
    tmp_path: Path,
) -> None:
    """Bad bytes cost their own columns, and the note has to say which file to go and look at.

    A plate is 1440 cells, so "its QC artifact could not be read" is not a place to start looking —
    and the cell's own artifact is now the only place the extraction and the alignment survive, so
    losing it loses every column but the counter's. That half stays: the fan-in gave this cell its
    fates and its fragment count, exactly as a corrupt fan-in leaves every cell what its own bundle
    gave it.
    """
    results = _plate_results(tmp_path)
    (results / "cell_a" / f"cell_a{QC_BUNDLE_SUFFIX}").write_bytes(b"not a gzip stream at all")

    stats = read_pipeline_stats("map/star-umi", results, ["cell_a", "cell_b"])

    assert stats is not None
    cell_a, cell_b = (_by_key(s) for s in stats.samples)
    assert "umi_tagged" not in cell_a and "umi_tagged" in cell_b
    assert "uniquely_mapped" not in cell_a and "uniquely_mapped" in cell_b
    assert set(FATES) <= set(cell_a), "the fan-in half of this cell's row must survive"
    assert stats.notes == [f"cell_a: cell_a{QC_BUNDLE_SUFFIX} could not be read (BadGzipFile)"]


def _chimeric_plate_results(
    tmp_path: Path, *, wrote: Sequence[str] | None = None
) -> tuple[Path, _Round, dict[str, int]]:
    """A finished chimeric plate on disk: one QC bundle per cell, one counting object per Component.

    Returns the results directory, the split round-trip whose summary each bundle absorbed, and how
    many fragments each Component's object holds for `cell_a`. The two Components are counted over
    DIFFERENT fragment sets deliberately — the whole plate against one gene's worth — so a reader
    that let one Component's row overwrite the other's is red on a number rather than on a key set.

    `wrote` is which Components got an object at all; a run that counted one organism of two is the
    per-Component half of "a missing artifact costs its own columns".
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    round_trip = _split(tmp_path, "plain")
    first, second = (c.name for c in round_trip.chimera.components)
    db, whole = _plate(tmp_path)
    thin = [
        (sample, _synthetic_bam(tmp_path / f"{second}_{sample}.bam", _PLATE[6:7]))
        for sample, _ in whole
    ]
    results = tmp_path / "results"
    for component, cells in ((first, whole), (second, thin)):
        if wrote is None or component in wrote:
            write_umi_counts(cells, db, results / PLATE_COMPONENT_H5AD.format(component=component))
    for sample in ("cell_a", "cell_b"):
        _bundled_cell(results, tmp_path / "staging", sample, split=round_trip.stats.to_dict())
    return results, round_trip, {first: len(_PLATE), second: 1}


def test_a_chimeric_plate_reports_what_left_at_the_split_and_each_components_fates(
    tmp_path: Path,
) -> None:
    """The twin's page: per-Component accounts, the drop counts, and every Component's fan-in.

    Three decisions meet here and none of them is legible from any other test.

    **The split summary is load-bearing rather than additional.** `unmapped` is dropped one rule
    before the counter, so it reads structurally zero in every per-Component matrix — a page
    rendering only that number would state a falsehood about the data, since those reads existed and
    left earlier. It lives in this artifact, and the per-Component share beside it is the number the
    whole chimera exercise exists to produce: the bacterial fraction of a well, readable without
    opening an `.h5ad`.

    **The fan-in is read once per Component, by the reader the plain twin already has.** The twin's
    fan-in artifact carries a `{component}`, so the Component loop lives in the registry and each
    object goes through the plate reader unchanged — the `obs` columns are the same columns, written
    by the same counter. What the loop adds is the key: two organisms' fates, fragment counts and
    saturation sit side by side on one cell's row, which is exactly what a shared key space could not
    do. The two objects here hold different fragment sets so that a collision is a wrong NUMBER and
    not merely a missing column.

    **The caveat travels with every one of those columns**, because a rate on one Component's object
    rides a denominator the split already took the unplaced records out of, and rendered beside a
    single-assembly page's column of the same name it is not the same measurement. Nothing on this
    half is headline either: the Component axis is N-wide, so the at-a-glance strip stays the cell's
    own.

    **All of it arrives through the cell's ONE artifact plus those objects**, which is what the twin's
    bundle is one key wider than the base's for: the split summary is reclaimed once that bundle
    carries it, so the numbers below reach the page only if the absorption kept them intact.

    Every payload is written by the REAL splitter and the REAL counter over the synthetic plates
    above, not by hand: what a summary key or an `obs` column is called is the writer's fact, so a
    reader driven from a hand-built dict could only ever agree with itself.
    """
    results, round_trip, fragments = _chimeric_plate_results(tmp_path)
    components = list(fragments)

    stats = read_pipeline_stats("map/star-umi-chimera", results, ["cell_a", "cell_b"], components)

    assert stats is not None and stats.complete
    cell_a = _by_key(stats.samples[0])
    assert "uniquely_mapped" in cell_a  # STAR's half, on the same row and not a row of its own
    # Every Component of the Chimera gets five columns, each a different question: what it kept,
    # that as a share of the records that came in, how much of it was multiply placed, how many of
    # its records have no mate in the file, and how many dead mates pointed at it. Read off
    # `_chimeric_plate`'s rows by hand — 35 records in, 10 and 9 kept. The kept count is what lets
    # the page close (every kept plus every drop is the 35) and the share is what compares across
    # cells of different depth; the rest are accounts of what is INSIDE those and add nothing to the
    # sum. The last two are a pair: apart they are one population counted twice, and their gap is
    # the only report anything makes of half-mapped fragments whose loci span two organisms.
    stats_out = round_trip.stats
    assert set(stats_out.kept) == {c.name for c in round_trip.chimera.components}
    for component, n in stats_out.kept.items():
        assert cell_a[f"split_kept_{component}"].value == n
        assert cell_a[f"split_share_{component}"].value == pytest.approx(n / stats_out.records_in)
        assert cell_a[f"split_multiplaced_{component}"].value == stats_out.multiplaced[component]
        assert cell_a[f"split_singletons_{component}"].value == stats_out.singletons[component]
        assert cell_a[f"split_mate_pointed_{component}"].value == stats_out.mate_pointed[component]
    assert sum(stats_out.kept.values()) + sum(stats_out.dropped.values()) == (
        stats_out.records_in
    ), "the page's own columns have to add back up to the records the BAM held"
    # ...plus two columns that are the cell's and not a Component's, the two signs of how far the
    # placeless records ran past the survivors each uniquely placed one is owed. One over on this
    # plate, from the fully mapped multi-locus pair that left a spare dead mate behind, and none
    # short; both reach the page as numbers rather than being absorbed as slack inside the check
    # that lets them through, and a cell can only ever show one of them.
    assert cell_a["split_excess_pointers"].value == 1
    assert cell_a["split_unanswered_survivors"].value == 0
    # ...and every reason, always present, because an absent key and a zero are different claims and
    # only one of them is a measurement.
    assert {r: cell_a[f"split_dropped_{r}"].value for r in DROP_REASONS} == {
        "unmapped": 9,
        "secondary": 4,
        "supplementary": 4,
    }
    # Nothing the split says is graded: nobody has measured what share of a worm plate SHOULD be
    # E. coli, so a bar here would be a figure invented at review.
    assert {_levels(stats.samples[0])[m] for m in cell_a if m.startswith("split_")} == {"none"}
    assert stats.findings == []
    # The counter's half, once per Component and under nobody else's key: a bare fate key would be
    # whichever object happened to be read last, which is the collision this whole shape avoids.
    assert not set(FATES) & set(cell_a)
    # What the plain twin's reader says about the same row, to compare the hints against: the caveat
    # is APPENDED to what the counter wrote and never replaces it, so what the number measures stays
    # the counter's sentence and what a per-Component reading of it is NOT is the registry's.
    plain = _by_key(
        fate_metrics(
            dict.fromkeys(FATES, 1) | {N_FRAGMENTS: 4, N_UMIS: 2, GENES_DETECTED: 2}, "cell_x"
        )
    )
    for component, total in fragments.items():
        assert {f"{fate}_{component}" for fate in FATES} <= set(cell_a)
        # Two Components, two different fragment sets, both intact on one row.
        assert cell_a[f"{N_FRAGMENTS}_{component}"].value == total
        assert f"{SATURATION}_{component}" in cell_a
        assert component in cell_a[f"{N_FRAGMENTS}_{component}"].label
        # And the caveat on every one of them — `unmapped` most of all, since that is the fate the
        # split takes out one rule early and the caveat is what says where those records went. What
        # the Component yielded rides the same rule: a molecule total off one Component's object is
        # a total over the records that reached that Component and not over the cell's.
        for key in (*FATES, N_UMIS, GENES_DETECTED):
            assert cell_a[f"{key}_{component}"].hint == f"{plain[key].hint} {PER_COMPONENT_CAVEAT}"
    # The whole Component's numbers, off the synthetic plate whose every fate is known by
    # construction: 17 fragments, 4 of them multiply placed. That fate is the one the argument for
    # shipping no reader at all rested on half of — it read a structural zero until the split began
    # keeping multiply-placed records, and it is a real measurement now.
    first = components[0]
    assert cell_a[f"multimapping_{first}"].value == pytest.approx(4 / len(_PLATE))
    assert cell_a[f"no_feature_{first}"].value == pytest.approx(2 / len(_PLATE))
    assert cell_a[f"{SATURATION}_{first}"].value == pytest.approx(1 - 3 / 5)
    # ...and what that Component yielded, from the same object and the same derivation: three
    # molecules over two genes, against the thin Component's one and one. Two organisms' yields side
    # by side on one row is the number the per-Component key exists to keep apart.
    second = components[1]
    assert (cell_a[f"{N_UMIS}_{first}"].value, cell_a[f"{GENES_DETECTED}_{first}"].value) == (3, 2)
    assert (cell_a[f"{N_UMIS}_{second}"].value, cell_a[f"{GENES_DETECTED}_{second}"].value) == (
        1,
        1,
    )
    # The at-a-glance strip stays the cell's own: the Component axis is N-wide, so promoting these
    # would put an unbounded number of columns in a strip whose whole job is being small — which is
    # what the counter marking its yield headline on the plain page makes a live question here.
    assert {m.key for m in stats.samples[0].metrics if m.headline} == {
        "umi_tagged",
        "uniquely_mapped",
        "unmapped_too_short",
    }
    # Columns read in pipeline order: what STAR did, then the split, then what each Component's
    # counter made of what the split handed it.
    keys = [key for key, _ in stats.columns]
    assert keys.index("uniquely_mapped") < keys.index("split_dropped_unmapped")
    assert keys.index("split_dropped_unmapped") < keys.index(f"no_feature_{components[0]}")
    assert keys.index(f"no_feature_{components[0]}") < keys.index(f"no_feature_{components[1]}")


def test_one_components_missing_object_costs_its_own_columns_and_names_what_is_owed(
    tmp_path: Path,
) -> None:
    """A Chimera has N counting objects, so one of them failing must cost N-1 nothing.

    The per-sample half of this registry has always said a corrupt artifact costs its own row; the
    fan-in half says the same one arity out, and on a Chimera there is a third: a plate's fan-in is
    one file per Component, and the organism that was counted has to keep its numbers when the one
    beside it did not. A reader that opened them as a set would drop both.

    Both ways of losing one are here because they are different answers, and the run state is where
    the difference shows: an object nobody wrote is still OWED and says so through the declaration,
    while an object that is there and will not parse is a read failure and names the file to go and
    look at. Neither costs the page.
    """
    results, round_trip, fragments = _chimeric_plate_results(tmp_path)
    counted, lost = fragments
    (results / PLATE_COMPONENT_H5AD.format(component=lost)).write_bytes(b"not an h5ad at all")

    stats = read_pipeline_stats(
        "map/star-umi-chimera", results, ["cell_a", "cell_b"], [counted, lost]
    )

    assert stats is not None
    cell_a = _by_key(stats.samples[0])
    assert {f"{fate}_{counted}" for fate in FATES} <= set(cell_a)
    assert not {f"{fate}_{lost}" for fate in FATES} & set(cell_a)
    assert "uniquely_mapped" in cell_a  # ...and the cell's own bundle, untouched by either
    assert any(PLATE_COMPONENT_H5AD.format(component=lost) in note for note in stats.notes), (
        stats.notes
    )
    assert stats.missing_deliverables == []  # it is there; it is unreadable, which is not the same

    # The other way: never written at all. Owed on the run state, silent in the notes, and the
    # Component beside it keeps every column.
    only_one, _, _ = _chimeric_plate_results(tmp_path / "partial", wrote=[counted])

    partial = read_pipeline_stats(
        "map/star-umi-chimera", only_one, ["cell_a", "cell_b"], [counted, lost]
    )

    assert partial is not None and partial.state == "failed"
    assert partial.missing_deliverables == [PLATE_COMPONENT_H5AD.format(component=lost)]
    assert partial.notes == []
    assert {f"{fate}_{counted}" for fate in FATES} <= set(_by_key(partial.samples[0]))


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

    # A saturation the counter wrote as `nan` is that same absence arriving as a float rather than as
    # a gap — a cell with nothing tagged to deduplicate has no ratio, and `nan%` on a page is worse
    # than no column. A real zero is a measurement and stays.
    absent = fate_metrics({N_FRAGMENTS: 4, SATURATION: float("nan")}, "cell_z")
    assert {m.key for m in absent.metrics} == {N_FRAGMENTS}
    assert {m.key: m.value for m in fate_metrics({SATURATION: 0.0}, "cell_w").metrics} == {
        SATURATION: 0.0
    }


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
    """Every number the search uses is a consequence of the layout, not a number in the source.

    Anchor start <= 24 -- mechanistic, not fitted: no exact hit anywhere in 18,901 reads starts past
    offset 24, that bound being Tn5 mosaic-end read-through -- plus the 22 bp one match consumes.
    Every term but that bound comes out of the elements, so a chemistry with a longer tag searches
    deeper and trims further without a line changing.
    """
    geometry = geometry_for_read(_smartseq3_r1())

    assert geometry.anchor == _TAG
    assert (geometry.anchor_start, geometry.umi_offset, geometry.umi_length) == (0, 11, 8)
    assert (geometry.trailing, geometry.trailing_offset, geometry.cdna_offset) == ("GGG", 19, 22)
    assert geometry.span == 22


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


def test_a_record_holds_what_it_held_however_it_was_built(tmp_path: Path) -> None:
    """The two fields where a SAM line and a BAM record do not mean the same thing.

    A record is parsed from one SAM line rather than assembled field by field, which is four times
    cheaper and writes the same record -- except here, and neither of these shows up in a
    run-it-twice comparison, because both constructions are deterministic. `*` is SAM's word for
    "this read has no qualities", so a one-base read whose Phred happens to be 9 spells its whole
    quality string that way and would come back carrying none at all. And the index bin -- which an
    unsorted, unindexed uBAM has no use for -- is one the parse computes and the assembly leaves at
    zero, so the file's bytes move if nobody puts it back.
    """
    geometry = geometry_for_read(_smartseq3_r1())
    r1 = tmp_path / "r1.fastq.gz"
    _fastq(r1, [("@cell:0", "A", "+", "*"), ("@cell:1", _CDNA, "+", _quals(_CDNA))])

    extract_umis([r1], None, tmp_path / "cell.bam", geometry, sample="cell")
    one_base, internal = _records(tmp_path / "cell.bam")

    qualities = one_base.query_qualities
    assert qualities is not None, "a `*` quality string is a Phred of 9, not an absent quality"
    assert list(qualities) == [9]
    assert (one_base.bin, internal.bin) == (0, 0)


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
    # The verb prints this object AND it is what lands on disk, so its key set is a published shape:
    # a rename here silently costs a column on every plate's page, and the file is the only surviving
    # account of the extraction once the uBAM is reclaimed.
    assert set(stats.to_dict()) == {
        "sample",
        "seqforge",
        "geometry",
        "fragments",
        "tagged",
        "untagged",
        "offsets",
    }


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


# ---- the summary that outlives the uBAM it measured ----------------------------------------------
#
# The uBAM is `temp()`, so once the aligner and the CRAM converter have consumed it every record is
# gone -- and with them the only evidence of how many fragments carried a tag at all. That share is
# the per-cell readout of whether the chemistry behaved (a tunable tagmentation parameter, published
# from 6.9% to 70.5%), so a cell at 2% and a cell at 28% are a bench problem and a normal run that
# nothing downstream can tell apart. These drive the REAL writer and the REAL reader, because the
# failure being prevented is a rename in either: a report that finds nothing looks exactly like a
# plate that was never extracted, so nothing raises and nobody is told.


def _extracted(tmp_path: Path, reads: list[tuple[str, str]], *, sample: str = "cell") -> Path:
    """Extract one cell into `<results>/<sample>/`, laid out as the rule lays it out, -> the summary."""
    geometry = geometry_for_read(_smartseq3_r1())
    r1, r2 = _write_pair(tmp_path, reads)
    cell = tmp_path / "results" / sample
    summary = cell / f"{sample}{EXTRACT_SUFFIX}"
    extract_umis(
        [r1], [r2], cell / f"{sample}.unaligned.bam", geometry, sample=sample, summary=summary
    )
    return summary


def test_what_the_extraction_measured_is_still_on_disk_once_the_ubam_is_reclaimed(
    tmp_path: Path,
) -> None:
    """The whole point of the artifact: snakemake deletes the uBAM, and the counts stay.

    Asserted by deleting the BAM, which is exactly what `temp()` does the moment the mapping and the
    CRAM jobs have consumed it. Before this landed the numbers went to stdout alone, so the only
    surviving copy was whatever captured the workflow's output -- on a cluster, a scheduler log
    somebody rotates -- and after the BAM was reclaimed nothing on disk could tell a dead library
    from a normal one.

    The payload is checked against what the extraction returned rather than against literals, so a
    writer that serialises the wrong field goes red here instead of shipping a plausible file.
    """
    geometry = geometry_for_read(_smartseq3_r1())
    r1, r2 = _write_pair(tmp_path, [(_tagged("ACGTACGT"), _CDNA), (_CDNA, _CDNA)])
    ubam, summary = tmp_path / "cell.unaligned.bam", tmp_path / f"cell{EXTRACT_SUFFIX}"

    stats = extract_umis([r1], [r2], ubam, geometry, sample="cell", summary=summary)
    ubam.unlink()

    assert summary.is_file()
    assert json.loads(summary.read_text()) == stats.to_dict()
    # And every field the artifact was asked to carry is in it, by name: these are what a reader
    # needs to judge a cell, and the geometry is what makes the rest interpretable at all.
    written = json.loads(summary.read_text())
    assert (written["fragments"], written["tagged"], written["untagged"]) == (2, 1, 1)
    assert written["offsets"] == {"0": 1}
    assert written["geometry"] == geometry.render()
    assert written["seqforge"] == seqforge_version


def test_no_summary_is_written_when_none_is_asked_for(tmp_path: Path) -> None:
    """A path and not a convention: nothing is derived from `--out`, so nothing appears beside it.

    The rule DECLARES the summary and passes what it declared, and a path guessed from the BAM's
    would be a second owner of that filename -- the owner that goes stale in silence. A hand
    invocation that asks for no summary must therefore leave the directory as it found it.
    """
    geometry = geometry_for_read(_smartseq3_r1())
    r1, r2 = _write_pair(tmp_path, [(_tagged("ACGTACGT"), _CDNA)])
    out = tmp_path / "bam" / "cell.unaligned.bam"

    extract_umis([r1], [r2], out, geometry, sample="cell")

    assert [p.name for p in out.parent.iterdir()] == ["cell.unaligned.bam"]


def test_the_summary_the_extractor_writes_is_the_one_the_report_reads_back(tmp_path: Path) -> None:
    """The writer decides the payload keys and the reader looks them up -- through the real writer.

    They live in one file precisely so they cannot drift, and a test that handed `extract_metrics` a
    dict of its own could not catch a rename in the writer: the reader would keep resolving against
    the test's dict while the page silently lost the one column this artifact exists for.

    The tagged fraction is that column. It is deliberately UNGRADED -- a library tuned low is a
    choice somebody made at the bench, and inventing a bar would tint a page over a decision rather
    than over a fault -- so what is asserted is the number and the absence of a verdict on it.
    """
    summary = _extracted(
        tmp_path,
        [(_tagged("ACGTACGT"), _CDNA), (_tagged("TTTTGGCC", offset=13), _CDNA), (_CDNA, _CDNA)],
    )

    sample = extract_metrics(json.loads(summary.read_text()), "cell")
    got = {m.key: m for m in sample.metrics}

    assert got["extract_fragments"].value == 3
    assert got["umi_tagged"].value == pytest.approx(2 / 3)
    assert got["umi_anchor_drift"].value == pytest.approx(1 / 2)  # one of the two tags is at 13
    assert {m.level for m in sample.metrics} == {"none"}
    # One headline, and it is the chemistry readout rather than a count: ten columns is past the
    # width at which the report folds the table away, so which survives the fold is a decision.
    assert {m.key for m in sample.metrics if m.headline} == {"umi_tagged"}


def test_a_cell_that_held_no_fragment_reports_no_tagged_share_rather_than_zero() -> None:
    """The one division here with no answer, and absence is what it produces.

    Pure over a payload, which is the seam the loader exists to keep testable. A cell whose FASTQs
    held nothing cannot have a share of them tagged, and a rendered `0.0%` is a number a reader acts
    on -- the same rule `fate_metrics` keeps for a cell the counter measured nothing in. The count
    itself is a real zero and stays: it was measured.
    """
    sample = extract_metrics(
        {"fragments": 0, "tagged": 0, "offsets": {}, "geometry": _PLATE_GEOMETRY}, "cell"
    )

    got = {m.key: m for m in sample.metrics}
    assert got["extract_fragments"].value == 0
    assert "umi_tagged" not in got
    assert "umi_anchor_drift" not in got


def test_the_drift_column_is_measured_against_the_start_the_geometry_declares() -> None:
    """The offsets histogram compressed to the number it is read for, and against the right origin.

    The search is unanchored because a measured 4.3% of exact hits do not start where the layout
    declares -- so a cell's own figure is what makes that measurement checkable rather than trusted,
    and a distribution that has shifted is a primer or trimming problem no count matrix explains.
    Measured against a GUESSED origin the column would be a different number that looks like this
    one, which is why the declared start is parsed out of the geometry the payload carries and a
    payload with no readable geometry drops the column instead of inventing a denominator.
    """
    counted = {"fragments": 10, "tagged": 8, "offsets": {"0": 6, "13": 1, "15": 1}}

    drifted = extract_metrics({**counted, "geometry": _PLATE_GEOMETRY}, "cell")
    assert {m.key: m.value for m in drifted.metrics}["umi_anchor_drift"] == pytest.approx(2 / 8)

    # A geometry that does not round-trip is a summary this reader cannot interpret, not a crash:
    # `TagGeometry.parse` refuses one, and the refusal is not among the exceptions the registry
    # tolerates, so an uncaught one would cost the whole page rather than one column.
    for unreadable in ({}, {"geometry": ""}, {"geometry": "R1:umi@0+8"}):
        thin = extract_metrics({**counted, **unreadable}, "cell")
        keys = {m.key for m in thin.metrics}
        assert "umi_anchor_drift" not in keys
        assert {"extract_fragments", "umi_tagged"} <= keys, (
            "the counts survive an unreadable geometry"
        )


def _revcomp(seq: str) -> str:
    """The reverse complement, so a synthetic mate maps in the orientation a pair maps in."""
    return seq.translate(str.maketrans("ACGT", "TGCA"))[::-1]


@NO_STAR_ALIGNMENT_ON_MACOS
@pytest.mark.external
def test_the_aligner_carries_the_umi_tag_through_and_stamps_exactly_one_read_group(
    tmp_path: Path,
) -> None:
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

    **The read group half is the same run asking a second question** (#416), and it is here rather
    than in a neighbour because it is the SAME alignment: the two tags share one command line, and
    what makes the fix non-obvious is precisely that they interact. The `RG` that used to survive
    named a group the output header never declared — STAR builds that header from the genome and its
    own parameters and inherits nothing from an input BAM — so the module now passes
    `--outSAMattrRGline`, which is the only thing that emits an `@RG` line and which also makes STAR
    stamp its own `RG` (`RG` is not a word `--outSAMattributes` takes). STAR appends the kept input
    tags AFTER its own and de-duplicates against nothing, so the keep list had to stop saying `All`
    and name `UB`; **`RG` is asserted here to appear exactly once**, because a doubled tag is invalid
    SAM and would be a worse artifact than the dangling one. `UB` is asserted on the same records
    because dropping `All` is where the UMI could have been lost.

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
         "--readFilesSAMattrKeep", "UB", "--outSAMtype", "BAM", "Unsorted",
         "--outSAMattrRGline", "ID:cell", "SM:cell",
         "--outFileNamePrefix", str(tmp_path / "star_")],
        check=True, capture_output=True,
    )  # fmt: skip

    out_bam = tmp_path / "star_Aligned.out.bam"
    aligned = _records(out_bam)
    assert aligned, "STAR aligned nothing, so the tag question was never asked"
    assert all(record.get_tag("UB") == "ACGTACGT" for record in aligned)

    with pysam.AlignmentFile(str(out_bam), "rb", check_sq=False) as handle:
        groups = handle.header.to_dict().get("RG", [])
    assert groups == [{"ID": "cell", "SM": "cell"}], (
        f"the header declares no single read group naming the cell: {groups}"
    )
    for record in aligned:
        # Counted off the raw tag list rather than read with `get_tag`, which answers with the first
        # match and would report a doubled tag as a single one -- the exact failure this asks about.
        assert [tag for tag, _ in record.get_tags()].count("RG") == 1, record.to_string()
        assert record.get_tag("RG") == "cell"


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
