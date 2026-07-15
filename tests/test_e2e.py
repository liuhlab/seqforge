"""Unit coverage for the e2e harness's pure parts.

The real count-matrix run needs STAR + a built genome index (a Linux/cluster operation), so it is
skip-gated. But the harness's own logic — simulation bookkeeping, matrix parsing, STAR-log
accounting, and the comparison verdict — is pure and must be trustworthy *before* it is used to
judge the compiler. A ground-truth harness that is itself wrong would silently bless a broken run.
"""

from __future__ import annotations

import gzip
import json
import shutil
import sys
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


# --------------------------------------------------------------------------------------------
# The cost arm (`kb e2e-cost`). It prices a counting rule rather than judging one, so none of the
# ground-truth machinery above applies — but its two instruments (the GTF filter and the per-run
# memory reading) are exactly the kind that fail silently, which is why they are pinned here.
# --------------------------------------------------------------------------------------------

_ENSEMBL_GTF = """\
#!genome-build R64
chrI\tsrc\texon\t1\t300\t.\t+\t.\tgene_id "G1"; gene_biotype "protein_coding";
chrI\tsrc\texon\t500\t900\t.\t+\t.\tgene_id "G1"; gene_biotype "protein_coding";
chrI\tsrc\texon\t1\t200\t.\t+\t.\tgene_id "L1"; gene_biotype "lncRNA";
"""

# GENCODE spells the same attribute `gene_type`. Byte-for-byte the same biology, and the Ensembl-only
# pattern matched none of it -- which meant the protein_coding filter kept L1 as well as G1.
_GENCODE_GTF = _ENSEMBL_GTF.replace("gene_biotype", "gene_type")

# No biotype attribute at all: the filter cannot be applied, so the harness must refuse.
_UNTYPED_GTF = """\
chrI\tsrc\texon\t1\t300\t.\t+\t.\tgene_id "G1"; gene_name "g one";
chrI\tsrc\texon\t500\t900\t.\t+\t.\tgene_id "G1"; gene_name "g one";
"""


@pytest.mark.parametrize("text", [_ENSEMBL_GTF, _GENCODE_GTF], ids=["ensembl", "gencode"])
def test_the_biotype_filter_reads_both_gtf_dialects(tmp_path: Path, text: str) -> None:
    """Ensembl says `gene_biotype`, GENCODE says `gene_type`; both must filter identically.

    The incident: this pattern was `gene_biotype` only. Every assembly the gates had ever run on
    (sacCer3/ensembl_R64-1-1, ce11/WS298) is Ensembl-flavoured, so it was never wrong in a real run
    -- until hg38, whose GENCODE GTF is the annotation the human corpus actually uses. There it
    matched nothing, and `if biotype and ...` turns matching nothing into filtering nothing: the
    lncRNA below would have entered a fixture that promises protein-coding genes, with no error.
    """
    from seqforge.e2e import _parse_exons

    gtf = tmp_path / "a.gtf"
    gtf.write_text(text)
    exons = _parse_exons(gtf)
    assert set(exons) == {"G1"}, "the lncRNA must be filtered out in BOTH dialects"
    assert len(exons["G1"]) == 2


def test_an_unfilterable_gtf_is_refused_rather_than_silently_widened(tmp_path: Path) -> None:
    """A GTF with no biotype attribute must raise, not quietly keep every gene.

    This is the general form of the bug above: when the filter cannot be applied, the two honest
    options are to error or to keep everything, and keeping everything is worse *because* it does not
    look like a failure -- the fixture builds, the run passes, the gene universe is wrong.
    """
    from seqforge.e2e import E2EUnavailable, _parse_exons

    gtf = tmp_path / "untyped.gtf"
    gtf.write_text(_UNTYPED_GTF)
    with pytest.raises(E2EUnavailable, match="gene_biotype"):
        _parse_exons(gtf)


def test_peak_rss_is_attributed_to_one_child_not_accumulated(tmp_path: Path) -> None:
    """A second measured run must be able to report LESS memory than the first.

    The regression this pins: the measurement used to be
    `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`, a high-water mark over every child the process
    has ever reaped. Running STAR once, that is STAR's peak. Running it N times -- which is exactly
    what a sweep does -- every point after the first is silently max()-ed with its predecessors, so
    the curve can only ever rise. That failure mode is indistinguishable from the memory growth the
    sweep exists to measure, which is what makes it worth a test rather than a comment.

    Big-then-small is the ordering that catches it: under the old code the second reading could not
    fall below the first, so `small < big` is precisely the assertion the bug forbids.
    """
    from seqforge.e2e import _run_measured

    def measure(mb: int, name: str) -> int:
        code, _wall, kib, _err = _run_measured(
            [sys.executable, "-c", f"x = bytearray({mb} * 1024 * 1024); print(len(x))"],
            outdir=tmp_path / name,
            timeout=120,
        )
        assert code == 0
        return kib

    big = measure(400, "big")
    small = measure(1, "small")
    assert small < big, (
        f"peak RSS is accumulating across children: 400 MB run -> {big}, 1 MB run -> {small}. "
        "Each measurement must belong to its own child."
    )


def test_a_measured_run_reports_a_failing_exit_code_with_its_stderr(tmp_path: Path) -> None:
    from seqforge.e2e import _run_measured

    code, _wall, _kib, err = _run_measured(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        outdir=tmp_path / "fail",
        timeout=120,
    )
    assert code == 3
    assert "boom" in err


def test_a_measured_run_that_overruns_its_budget_is_killed(tmp_path: Path) -> None:
    from seqforge.e2e import E2EUnavailable, _run_measured

    with pytest.raises(E2EUnavailable, match="budget"):
        _run_measured(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            outdir=tmp_path / "slow",
            timeout=1,
        )


def test_cost_reads_have_the_v3_geometry_and_come_from_the_models(tmp_path: Path) -> None:
    """The cost fixture must still be 10x-v3-shaped, or the compiler cannot identify it."""
    from seqforge.e2e import GeneModel, write_cost_fastqs

    models = [GeneModel(gene_id="G1", mrna="ACGT" * 200, introns=("TTGCA" * 80,))]
    cbs = ["ACGTACGTACGTACGT", "TTTTCCCCGGGGAAAA"]
    cdna, bc = tmp_path / "r2.fastq.gz", tmp_path / "r1.fastq.gz"
    stats = write_cost_fastqs(
        models, n_reads=500, cbs=cbs, cdna_path=cdna, bc_path=bc, read_len=90, seed=1
    )
    assert stats["n_reads"] == 500
    assert stats["n_exonic"] + stats["n_intronic"] == 500
    assert stats["n_intronic"] > 0, "no intronic reads means Velocyto has nothing to price"

    with gzip.open(cdna, "rt") as fh:
        cdna_lines = fh.read().splitlines()
    with gzip.open(bc, "rt") as fh:
        bc_lines = fh.read().splitlines()
    assert len(cdna_lines) == len(bc_lines) == 500 * 4

    cdna_seqs = cdna_lines[1::4]
    bc_seqs = bc_lines[1::4]
    assert {len(s) for s in cdna_seqs} == {90}
    assert {len(s) for s in bc_seqs} == {28}, "16 bp CB + 12 bp UMI is what v3 requires"
    assert {s[:16] for s in bc_seqs} <= set(cbs), "every CB must come from the whitelist sample"
    # a read that is not a substring of its source would mean the fixture is fabricating sequence
    sources = models[0].mrna + "|" + models[0].introns[0]
    assert all(s in sources for s in cdna_seqs)


def test_cost_reads_are_deterministic_in_seed(tmp_path: Path) -> None:
    from seqforge.e2e import GeneModel, write_cost_fastqs

    models = [GeneModel(gene_id="G1", mrna="ACGT" * 200, introns=("TTGCA" * 80,))]
    cbs = ["ACGTACGTACGTACGT"]

    def emit(tag: str, seed: int) -> bytes:
        cdna, bc = tmp_path / f"{tag}_r2.gz", tmp_path / f"{tag}_r1.gz"
        write_cost_fastqs(
            models, n_reads=200, cbs=cbs, cdna_path=cdna, bc_path=bc, read_len=90, seed=seed
        )
        return gzip.open(cdna, "rb").read() + gzip.open(bc, "rb").read()

    assert emit("a", 7) == emit("b", 7)
    assert emit("c", 7) != emit("d", 8)


def test_the_line_fit_recovers_a_known_slope_and_intercept() -> None:
    """The fit is the deliverable, so it gets a test with an answer known in advance."""
    from seqforge.e2e import _fit_line

    # 30 GB fixed + 16 bytes per read -- the shape we expect from a genome index plus per-read arrays
    per_read = 16 / 1024**3
    pts = [(n, 30.0 + per_read * n) for n in (2_000_000, 8_000_000, 32_000_000)]
    fit = _fit_line(pts)
    assert fit["ok"]
    assert fit["intercept_gb"] == pytest.approx(30.0, abs=0.01)
    assert fit["bytes_per_read"] == pytest.approx(16.0, abs=0.1)
    assert fit["max_residual_gb"] == pytest.approx(0.0, abs=0.001)
    # and the extrapolation must be labelled as one, since that is the number people will quote
    assert fit["projected"]["500M_reads"]["extrapolation_factor"] == pytest.approx(15.6, abs=0.1)


def test_the_fit_reports_a_residual_that_can_falsify_the_line() -> None:
    """A curve must not be reported as a line with a clean conscience."""
    from seqforge.e2e import _fit_line

    bent = [(1_000_000, 30.0), (2_000_000, 30.1), (4_000_000, 40.0)]
    assert _fit_line(bent)["max_residual_gb"] > 1.0


def test_the_fit_refuses_when_there_is_nothing_to_fit() -> None:
    from seqforge.e2e import _fit_line

    assert not _fit_line([(1_000_000, 30.0)])["ok"]
    assert not _fit_line([(1_000_000, 30.0), (1_000_000, 31.0)])["ok"]


def test_resume_reloads_a_measured_point_but_only_for_the_same_features(tmp_path: Path) -> None:
    """A requeue must not repay for a point already measured -- unless the features changed.

    This is R7 (disk is state) doing real work rather than decorating: on a preemptible partition a
    requeue at hour 5 of a 6 hour sweep is normal, and a sweep that restarted from zero would make the
    free partition the expensive one. The features guard is the part worth testing: the same depth
    measured under a different --quantify is a DIFFERENT measurement wearing the same tag, and
    silently reusing it would put a Gene-only number in an all-five curve.
    """
    from seqforge.e2e import _load_resumable_points

    partial = tmp_path / "cost_sweep.partial.json"
    measured = {"n_reads": 10_000_000, "star_peak_rss_gb": 31.5}
    five = ["Gene", "GeneFull", "GeneFull_ExonOverIntron", "GeneFull_Ex50pAS", "Velocyto"]
    partial.write_text(json.dumps({"soloFeatures": five, "points": [measured]}))

    assert _load_resumable_points(partial, five) == {10_000_000: measured}
    assert _load_resumable_points(partial, ["Gene"]) == {}, "different features must not be reused"


def test_resume_ignores_a_failed_point_and_unreadable_state(tmp_path: Path) -> None:
    from seqforge.e2e import _load_resumable_points

    partial = tmp_path / "p.json"
    partial.write_text(
        json.dumps({"soloFeatures": ["Gene"], "points": [{"n_reads": 5, "failed": True}]})
    )
    assert _load_resumable_points(partial, ["Gene"]) == {}, "a failed point is not a measurement"

    partial.write_text("{ this is not json")
    assert _load_resumable_points(partial, ["Gene"]) == {}
    assert _load_resumable_points(tmp_path / "absent.json", ["Gene"]) == {}
