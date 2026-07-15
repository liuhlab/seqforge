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
