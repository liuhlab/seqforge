"""Unit coverage for the e2e harness's pure parts.

The real count-matrix run needs STAR + a built genome index (a Linux/cluster operation), so it is
skip-gated. But the harness's own logic — simulation bookkeeping, matrix parsing, STAR-log
accounting, and the comparison verdict — is pure and must be trustworthy *before* it is used to
judge the compiler. A ground-truth harness that is itself wrong would silently bless a broken run.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from seqforge.e2e import (
    _compare,
    parse_solo_matrix,
    simulate,
    star_stats,
)

_GENES = [("GENE_A", "ACGT" * 200), ("GENE_B", "TTGCA" * 160)]


def test_simulate_bookkeeping_is_self_consistent() -> None:
    sim = simulate(_GENES, n_cells=3, reads_per_cell=20, read_len=50, seed=1)
    assert len(sim.cdna) == len(sim.barcode) == 60
    # ground truth totals must equal the reads emitted, or the assertion it backs is meaningless
    assert sum(sim.truth.values()) == 60
    # R1 = 16 bp CB + 12 bp UMI = 28 bp (the v3 geometry the resolver must recognize)
    assert all(len(b) == 28 for b in sim.barcode)
    assert all(len(c) == 50 for c in sim.cdna)
    # every cell barcode is drawn from the whitelist, and UMIs are unique (so count == read count)
    cells = {b[:16] for b in sim.barcode}
    assert cells <= set(sim.whitelist) and len(cells) == 3
    umis = [b[16:] for b in sim.barcode]
    assert len(set(umis)) == len(umis)
    # every emitted cDNA fragment really is a substring of the gene it was attributed to
    by_id = dict(_GENES)
    for (_cell, gene), _n in sim.truth.items():
        assert gene in by_id


def test_parse_solo_matrix(tmp_path: Path) -> None:
    (tmp_path / "barcodes.tsv").write_text("CELL1\nCELL2\n")
    (tmp_path / "features.tsv").write_text("GENE_A\tA\tGene\nGENE_B\tB\tGene\n")
    # Matrix Market: gene(row) barcode(col) value; a 0 entry must not become a phantom count
    (tmp_path / "matrix.mtx").write_text(
        "%%MatrixMarket matrix coordinate integer general\n%\n2 2 3\n1 1 5\n2 2 7\n1 2 0\n"
    )
    counts = parse_solo_matrix(tmp_path)
    assert counts == {("CELL1", "GENE_A"): 5, ("CELL2", "GENE_B"): 7}


def test_star_stats_parses_log(tmp_path: Path) -> None:
    (tmp_path / "Log.final.out").write_text(
        "                          Number of input reads |\t2000\n"
        "                        Uniquely mapped reads number |\t1923\n"
        "        Number of reads mapped to multiple loci |\t53\n"
        "        Number of reads mapped to too many loci |\t24\n"
    )
    stats = star_stats(tmp_path)
    assert stats["input_reads"] == 2000
    assert stats["uniquely_mapped"] == 1923
    assert stats["multi_loci"] == 53
    assert star_stats(tmp_path / "nope") == {}  # absent log -> no stats, not a crash


def test_compare_flags_spurious_and_inflated() -> None:
    truth = {("C1", "G1"): 5, ("C1", "G2"): 3}
    # G3 was never injected (fabricated), and G1 reports MORE than injected (inflated)
    observed = {("C1", "G1"): 7, ("C1", "G2"): 3, ("C1", "G3"): 2}
    v = _compare(truth, observed)
    assert v["exact"] is False
    assert v["n_spurious_pairs"] == 1
    assert v["n_inflated_pairs"] == 1
    assert v["example_spurious"] == [{"cell": "C1", "gene": "G3", "observed": 2}]


def test_compare_exact_when_matrix_matches() -> None:
    truth = {("C1", "G1"): 5, ("C1", "G2"): 3}
    v = _compare(truth, dict(truth))
    assert v["exact"] is True
    assert v["recovery_rate"] == 1.0
    assert v["n_spurious_pairs"] == 0 and v["n_inflated_pairs"] == 0


def test_compare_counts_loss_but_no_fabrication() -> None:
    """STAR dropping an ambiguous read is a LOSS, not a fabrication — the verdict must distinguish."""
    truth = {("C1", "G1"): 5, ("C1", "G2"): 3}
    v = _compare(truth, {("C1", "G1"): 4})
    assert v["n_spurious_pairs"] == 0 and v["n_inflated_pairs"] == 0
    assert v["recovered_total"] == 4
    assert v["recovery_rate"] == 0.5


@pytest.mark.skipif(shutil.which("STAR") is None, reason="STAR not installed (Linux/cluster only)")
def test_star_is_available_when_claimed() -> None:  # pragma: no cover - host dependent
    assert shutil.which("STAR")


# --------------------------------------------------------------------------------------------
# the intron-rich / GeneFull fixture
#
# The STARsolo run itself needs a cluster (skip-gated above), but the parts that decide whether the
# ground truth is even MEANINGFUL are pure and testable here: intron cleanliness and the exon/intron
# truth split. A fixture whose "intronic" reads secretly overlap an exon would pass while proving
# nothing, so those are the assertions worth having.
# --------------------------------------------------------------------------------------------


def test_overlaps_detects_touching_intervals() -> None:
    from seqforge.e2e import _overlaps

    spans = [(10, 20), (40, 50), (100, 200)]
    assert _overlaps(spans, 15, 16)  # inside
    assert _overlaps(spans, 5, 10)  # touches the left edge
    assert _overlaps(spans, 20, 25)  # touches the right edge
    assert _overlaps(spans, 5, 300)  # engulfs
    assert not _overlaps(spans, 21, 39)  # the gap between
    assert not _overlaps(spans, 201, 500)  # past the end
    assert not _overlaps(spans, 1, 9)  # before the start


def test_merge_coalesces_adjacent_and_overlapping_spans() -> None:
    from seqforge.e2e import _merge

    assert _merge([(1, 5), (6, 9)]) == [(1, 9)]  # adjacent -> merged
    assert _merge([(1, 5), (3, 9)]) == [(1, 9)]  # overlapping -> merged
    assert _merge([(1, 5), (8, 9)]) == [(1, 5), (8, 9)]  # a real gap survives
    assert _merge([(8, 9), (1, 5)]) == [(1, 5), (8, 9)]  # input order must not matter


def test_feature_list_accepts_both_kb_shapes() -> None:
    """KB params carry soloFeatures as a list or a string; STAR's CLI wants separate argv items."""
    from seqforge.e2e import _feature_list

    assert _feature_list(["Gene", "GeneFull"]) == ["Gene", "GeneFull"]
    assert _feature_list("Gene GeneFull") == ["Gene", "GeneFull"]
    assert _feature_list("Gene") == ["Gene"]
    assert _feature_list(("Gene",)) == ["Gene"]


def test_nuclei_simulation_splits_exonic_and_intronic_truth() -> None:
    """The two truths must stay apart, and their sum must be every read — no read counted twice."""
    from seqforge.e2e import GeneModel, simulate_nuclei

    models = [
        GeneModel(gene_id="G1", mrna="ACGT" * 200, introns=("TTTT" * 100,)),
        GeneModel(gene_id="G2", mrna="TGCA" * 200, introns=("GGGG" * 100,)),
    ]
    sim = simulate_nuclei(models, n_cells=3, reads_per_cell=50, intron_frac=0.4, seed=0)

    n_exonic = sum(sim.truth_exonic.values())
    n_intronic = sum(sim.truth_intronic.values())
    assert n_exonic + n_intronic == 150 == len(sim.cdna) == len(sim.barcode)
    assert n_intronic > 0, "intron_frac=0.4 must actually produce intronic reads"
    assert n_exonic > 0
    # truth_full is the sum, per (cell, gene) — GeneFull's target
    assert sum(sim.truth_full.values()) == 150
    # unique UMIs => injected count == read count, which is what makes the assertion exact
    umis = [b[16:] for b in sim.barcode]
    assert len(set(umis)) == len(umis)


def test_nuclei_simulation_is_deterministic_in_seed() -> None:
    from seqforge.e2e import GeneModel, simulate_nuclei

    models = [GeneModel(gene_id="G1", mrna="ACGT" * 200, introns=("TTTT" * 100,))]
    a = simulate_nuclei(models, n_cells=2, reads_per_cell=20, seed=7)
    b = simulate_nuclei(models, n_cells=2, reads_per_cell=20, seed=7)
    assert a.cdna == b.cdna and a.barcode == b.barcode
    assert a.truth_exonic == b.truth_exonic and a.truth_intronic == b.truth_intronic


def test_nuclei_simulation_refuses_genes_with_no_usable_intron() -> None:
    """Refuse loudly rather than emit a fixture that silently tests nothing."""
    from seqforge.e2e import E2EUnavailable, GeneModel, simulate_nuclei

    models = [GeneModel(gene_id="G1", mrna="ACGT" * 200, introns=("TTT",))]  # intron < read_len
    with pytest.raises(E2EUnavailable, match="intron"):
        simulate_nuclei(models, n_cells=1, reads_per_cell=5, seed=0)


def test_intron_reads_come_only_from_introns() -> None:
    """The fixture's core claim: an 'intronic' read must not be findable in the mRNA.

    If intronic reads overlapped exons, `Gene` would legitimately count them, the Gene-vs-GeneFull
    assertion would collapse, and the fixture would pass while proving nothing.
    """
    from seqforge.e2e import GeneModel, simulate_nuclei

    mrna = "ACGT" * 250
    intron = "TTTTGGGG" * 60  # shares no 90-mer with mrna
    models = [GeneModel(gene_id="G1", mrna=mrna, introns=(intron,))]
    sim = simulate_nuclei(models, n_cells=2, reads_per_cell=100, intron_frac=1.0, seed=1)
    assert sum(sim.truth_exonic.values()) == 0
    assert sum(sim.truth_intronic.values()) == 200
    for read in sim.cdna:
        assert read in intron
        assert read not in mrna
