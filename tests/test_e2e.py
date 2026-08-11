"""Unit coverage for the e2e harness's pure parts.

The real count-matrix run needs STAR + a built genome index (a Linux/cluster operation), so it is
skip-gated. But the harness's own logic — simulation bookkeeping, matrix parsing, STAR-log
accounting, and the comparison verdict — is pure and must be trustworthy *before* it is used to
judge the compiler. A ground-truth harness that is itself wrong would silently bless a broken run.
"""

from __future__ import annotations

import gzip
import json
import random
import resource
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from seqforge.e2e import (
    SHIPPED_CELL_FILTER,
    _compare,
    simulate,
    star_stats,
)
from seqforge.workflows.starsolo_args import (
    CELLRANGER_PARITY,
    SAM_WRITE_PATH,
    SHIPPED_OUT_SAM_TYPE,
    SORT_CAP_SHELL,
    starsolo_argv,
    starsolo_shell_args,
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


def test_parse_h5ad_reads_x_and_layers(tmp_path: Path) -> None:
    """The ground-truth reader opens the DELIVERABLE, so the h5ad is what these gates assert on.

    It replaced `parse_solo_matrix`, which read STAR's Matrix Market files: everything downstream of
    those (the transpose to cells x genes, which feature became X, which layer got which name) sat
    outside the only test in this repo that checks a count against ground truth.
    """
    import anndata as ad
    from scipy.sparse import csr_matrix

    from seqforge.e2e import parse_h5ad

    adata = ad.AnnData(X=csr_matrix(np.array([[5, 0], [0, 7]], dtype=np.int32)))
    adata.obs_names = ["CELL1", "CELL2"]
    adata.var_names = ["GENE_A", "GENE_B"]
    adata.layers["GeneFull"] = csr_matrix(np.array([[6, 0], [0, 9]], dtype=np.int32))
    adata.write_h5ad(tmp_path / "s1.h5ad")

    assert parse_h5ad(tmp_path / "s1.h5ad") == {("CELL1", "GENE_A"): 5, ("CELL2", "GENE_B"): 7}
    assert parse_h5ad(tmp_path / "s1.h5ad", layer="GeneFull") == {
        ("CELL1", "GENE_A"): 6,
        ("CELL2", "GENE_B"): 9,
    }


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


def test_compare_counts_loss_but_no_fabrication() -> None:
    """STAR dropping an ambiguous read is a LOSS, not a fabrication — the verdict must distinguish."""
    truth = {("C1", "G1"): 5, ("C1", "G2"): 3}
    v = _compare(truth, {("C1", "G1"): 4})
    assert v["exact"] is False
    assert v["n_spurious_pairs"] == 0 and v["n_inflated_pairs"] == 0
    assert v["recovered_total"] == 4
    assert v["recovery_rate"] == 0.5

    exact = _compare(truth, dict(truth))
    assert exact["exact"] is True and exact["recovery_rate"] == 1.0
    assert exact["n_spurious_pairs"] == 0 and exact["n_inflated_pairs"] == 0


# --------------------------------------------------------------------------------------------
# the gates must run the SHIPPED module, not a private copy of its command line
# --------------------------------------------------------------------------------------------


def test_the_correctness_arms_run_the_composed_snakefile() -> None:
    """The ground-truth gates must drive `starsolo.smk`, never a second hand-written STAR argv.

    `kb e2e` used to build the composed config and then assemble its **own** STAR command line, so
    STARsolo's argv existed twice by hand and had already drifted -- the copy here could not run a
    `CB_UMI_Complex` chemistry at all, and it omitted nine of the module's flags. That second
    rendering is gone (#348): `workflows/starsolo_args.py` renders both, so `run_starsolo` runs the
    shipped command line rather than a lookalike.

    **This guard is not redundant now, and the distinction is the reason it survives.** One renderer
    settles what the arms RUN; it says nothing about what they DRIVE. A gate must exercise the emitted
    Snakefile -- the rules, the wiring, the outputs snakemake declares -- and reaping STAR directly
    skips all of it however correct the argv is. So the correctness arms must reach `run_composed`,
    and only the cost sweep may reach `run_starsolo`, because it is the memory instrument: under
    snakemake, `ru_maxrss` folds in descendants and turns an exact reading into an approximate one.

    The arms need a cluster, so nothing here executes them -- which is why the call graph is read
    statically.
    """
    import ast
    import inspect

    from seqforge import e2e

    called = {
        fn.name: {
            c.func.id
            for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        }
        for fn in ast.walk(ast.parse(inspect.getsource(e2e)))
        if isinstance(fn, ast.FunctionDef)
    }
    for arm in ("run_e2e", "run_intron_e2e"):
        assert "run_composed" in called[arm], f"{arm} does not run the composed Snakefile"
        assert "run_starsolo" not in called[arm], (
            f"{arm} calls run_starsolo -- that renders a SECOND STAR command line by hand and "
            f"leaves the shipped module unexecuted, which is the bug this test exists for"
        )
    assert "run_starsolo" in called["run_cost_sweep"], "the memory instrument lost its measured run"


def test_e2e_constructs_experiment_inputs_with_real_fields() -> None:
    """Every `ExperimentInputs(...)` in e2e.py must name fields the dataclass actually has.

    These constructions sit behind a STAR-availability gate, so they execute only on a cluster and
    every local `pixi run check` skips them. That is exactly how `organism_taxid=` — a parameter of
    the `experiment_from_metadata` factory, never a field of `ExperimentInputs` (the field is
    `organism: EvidencedTaxid`) — sat in three call sites and crashed all three `kb e2e*` arms while
    the suite stayed green. Check the call statically (the same shape as the AST guards over the
    liulab-genome consumer surface), so a field rename cannot hide behind the skip again.
    """
    import ast
    import dataclasses
    import inspect

    from seqforge import e2e
    from seqforge.manifest.fill import ExperimentInputs

    fields = {f.name for f in dataclasses.fields(ExperimentInputs)}
    calls = [
        node
        for node in ast.walk(ast.parse(inspect.getsource(e2e)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ExperimentInputs"
    ]
    assert calls, "sanity: e2e.py should construct ExperimentInputs"
    for call in calls:
        used = {kw.arg for kw in call.keywords if kw.arg is not None}
        assert used <= fields, (
            f"e2e.py builds ExperimentInputs with non-field kwarg(s) {sorted(used - fields)}; "
            f"real fields are {sorted(fields)} — this crashes `kb e2e` where the suite cannot see it"
        )


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


#: The measured child must outlive several of the parent's VmHWM poll intervals (`_POLL_S = 0.05s`
#: in e2e.py), or the peak is never sampled. A child that allocates and exits inside one interval is
#: caught at an arbitrary pre-peak instant instead — the origin of the `400 MB run -> 12 KiB` flake:
#: both a `bytearray` alloc and its page-touch finish in milliseconds, faster than a single poll. Ten
#: intervals of headroom keeps the peak resident long enough that sampling it is a certainty, not a
#: race, on the loadiest CI runner.
_HOLD_PEAK_RESIDENT_S = 0.5


def _measure_mb(tmp_path: Path, mb: int, name: str) -> int:
    """Spawn a child that makes ``mb`` MB genuinely resident, and return its measured peak RSS (KiB).

    The child touches every page (an untouched ``bytearray`` is not necessarily resident, and this is
    about RESIDENT memory) and then holds that peak for :data:`_HOLD_PEAK_RESIDENT_S` so the parent's
    ``VmHWM`` poller is guaranteed to sample it — see the note there.
    """
    from seqforge.e2e import _run_measured

    code, _wall, kib, _err = _run_measured(
        [
            sys.executable,
            "-c",
            f"x = bytearray({mb} * 1024 * 1024)\n"
            f"for i in range(0, len(x), 4096): x[i] = 1\n"
            f"import time; time.sleep({_HOLD_PEAK_RESIDENT_S})\n"
            f"print(len(x))",
        ],
        outdir=tmp_path / name,
        timeout=120,
    )
    assert code == 0
    return kib


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
    big = _measure_mb(tmp_path, 400, "big")
    small = _measure_mb(tmp_path, 1, "small")
    assert small < big, (
        f"peak RSS is accumulating across children: 400 MB run -> {big}, 1 MB run -> {small}. "
        "Each measurement must belong to its own child."
    )


def test_peak_rss_does_not_inherit_the_measuring_process_own_memory(tmp_path: Path) -> None:
    """A fat parent must not raise the floor under a thin child.

    **The test above passed on macOS and was red on arc, and the code was wrong both times.** On
    Linux a spawned child's address space starts as a copy of its parent's, `ru_maxrss` is a
    high-water mark, and `exec` never lowers it -- so `wait4` reported `max(parent_rss, child_peak)`.
    Measured: with an 879 MB parent, a 1 MB child reported `879260 KiB`, the parent's RSS to the
    byte, and so did a 400 MB child. macOS spawns via `posix_spawn` and does not do this, which is
    the only reason the sibling test above ever passed locally -- it was green for a reason unrelated
    to the code being right, and went red only under a suite fat enough to cross the floor.

    So this test makes the parent fat ON PURPOSE, which is the condition the bug needs. It fails on
    the old `wait4` code on Linux and passes on macOS, and that asymmetry is the point: a measuring
    instrument may not silently report the weight of the person holding it.
    """
    ballast = bytearray(700 * 1024 * 1024)
    for i in range(0, len(ballast), 4096):
        ballast[i] = 1  # make it genuinely resident, not just mapped
    parent = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KiB on Linux, bytes on macOS
    try:
        small = _measure_mb(tmp_path, 1, "small")
        assert small < parent / 2, (
            f"a 1 MB child reported {small} while this process holds {parent} -- the measurement is "
            f"reporting the PARENT's memory, so every number below the parent's RSS is a floor, not "
            f"a reading"
        )
    finally:
        del ballast


def test_a_measured_run_surfaces_a_failure_and_kills_an_overrun(tmp_path: Path) -> None:
    from seqforge.e2e import E2EUnavailable, _run_measured

    code, _wall, _kib, err = _run_measured(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        outdir=tmp_path / "fail",
        timeout=120,
    )
    assert code == 3
    assert "boom" in err

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
    projected = fit["projected"]
    assert isinstance(projected, dict), "a fit that reports `ok` must carry its projections"
    assert projected["500M_reads"]["extrapolation_factor"] == pytest.approx(15.6, abs=0.1)


def test_resume_reloads_a_measured_point_but_only_under_an_identical_fingerprint(
    tmp_path: Path,
) -> None:
    """A requeue must not repay for a point it has -- unless ANY input to the number changed.

    Disk is state, doing real work: on a preemptible partition a requeue at hour 5 of a 6 hour
    sweep is normal, and restarting from zero would make the free partition the expensive one.

    The guard originally compared soloFeatures alone, which an audit caught, and the sting was in the
    part not considered: the partial file is never deleted, so a COMPLETED sweep leaves the resume
    armed. It needs no preemption -- a second run in the same workdir at --threads 48 would silently
    reuse the 16-thread points, and per-thread buffers are IN the peak. The guard was narrower than
    the thing it guarded, which is how it reintroduced the failure it existed to prevent.
    """
    from seqforge.e2e import _load_resumable_points

    partial = tmp_path / "cost_sweep.partial.json"
    measured = {"n_reads": 10_000_000, "star_peak_rss_gb": 34.57}
    partial.write_text(json.dumps({"fingerprint": "abc123", "points": [measured]}))

    assert _load_resumable_points(partial, "abc123") == {10_000_000: measured}
    assert _load_resumable_points(partial, "different") == {}, "a changed input must not be reused"

    partial.write_text(
        json.dumps({"fingerprint": "abc123", "points": [{"n_reads": 5, "failed": True}]})
    )
    assert _load_resumable_points(partial, "abc123") == {}, "a failed point is not a measurement"
    partial.write_text("{ this is not json")
    assert _load_resumable_points(partial, "abc123") == {}
    assert _load_resumable_points(tmp_path / "absent.json", "abc123") == {}


_SIMPLE_SOLO = {
    "soloType": "CB_UMI_Simple",
    "soloCBstart": 1,
    "soloCBlen": 16,
    "soloUMIstart": 17,
    "soloUMIlen": 12,
    "soloCBmatchWLtype": "1MM_multi_Nbase_pseudocounts",
    "soloStrand": "Forward",
    "soloFeatures": "Gene GeneFull",
    "clipAdapterType": "CellRanger4",
}
_COMPLEX_SOLO = {
    **_SIMPLE_SOLO,
    "soloType": "CB_UMI_Complex",
    "soloCBposition": "0_0_2_-1 0_21_2_-1 0_43_2_-1",
    "soloUMIposition": "3_10_3_21",
    "soloCBmatchWLtype": "1MM",
}
_RENDER: dict[str, Any] = {
    "genome_dir": "idx",
    "cdna": "a.fq.gz",
    "barcode": "b.fq.gz",
    "whitelist": "wl.txt",
    "out_prefix": "out/",
    "threads": 4,
}


def test_the_shipped_flag_sets_reach_star_as_data_not_as_a_string_match() -> None:
    """The parity set and the SAM write path are rendered whole, in order, into the real argv.

    This replaces a whitespace-normalised substring search over `starsolo.smk`'s text. That search was
    the only expression available while STARsolo's command line was rendered twice — once here by
    hand, once in a `shell:` block nothing could import — and it could only ever check that the two
    copies still agreed. There is one copy now, so the claim worth making is the direct one: what the
    renderer emits IS the set, contiguously, rather than a substring of some prose that mentions it.

    The failure this exists for is not hypothetical in either direction. `BAM Unsorted` survived in
    four places after the module moved to a coordinate-sorted BAM to get `CB`/`UB` into the CRAM; and
    a determinism guard that quietly fell back to `CellRanger2.2` — a closed-form knee that could not
    be nondeterministic if it tried — would have gone green forever while the Monte-Carlo caller we
    ship went unchecked.
    """
    argv = starsolo_argv(_SIMPLE_SOLO, bam_sort_ram_bytes=None, **_RENDER)

    for shipped in (CELLRANGER_PARITY, SAM_WRITE_PATH):
        start = argv.index(shipped[0])
        assert argv[start : start + len(shipped)] == shipped, (
            f"{shipped[0]} is rendered, but not with the rest of its set beside it"
        )

    # The constant the determinism instrument guards is READ from the set, so it cannot name a caller
    # the pipeline does not run -- there is no second literal left to disagree with.
    assert SHIPPED_CELL_FILTER in CELLRANGER_PARITY

    # The SAM type is the module's by default and the sweep's only by asking, which is the whole of
    # `kb e2e-cost --out-sam-type`: an arm that only ever priced `None` priced a command nobody runs.
    sam_type = argv.index("--outSAMtype") + 1
    assert argv[sam_type : sam_type + len(SHIPPED_OUT_SAM_TYPE)] == SHIPPED_OUT_SAM_TYPE
    counted = starsolo_argv(
        _SIMPLE_SOLO, bam_sort_ram_bytes=None, out_sam_type=("None",), **_RENDER
    )
    assert counted[counted.index("--outSAMtype") + 1] == "None"


def test_a_complex_chemistry_renders_positions_and_the_instrument_can_therefore_run_one() -> None:
    """The divergence that made `run_starsolo` unable to run half the KB (#348).

    STARsolo spells barcode geometry two ways, and passing `--soloCBstart` to a `CB_UMI_Complex`
    chemistry is an error rather than a preference. The module branched on `soloType` and the
    instrument named the four simple-geometry flags directly, so a Complex chemistry was not merely
    unmeasured by the cost sweep — it was unrunnable by it. Both call this now.

    `soloCBposition` is asserted to reach STAR as N SEPARATE tokens, which is the half a substring
    match cannot see: compose emits the quadruples space-joined, and handed over whole they would be
    one quoted argument naming a barcode geometry STAR cannot parse.
    """
    argv = starsolo_argv(_COMPLEX_SOLO, bam_sort_ram_bytes=None, **_RENDER)

    assert "--soloCBstart" not in argv, "a Complex chemistry has no start/length to give"
    positions = argv.index("--soloCBposition")
    assert argv[positions + 1 : positions + 4] == ("0_0_2_-1", "0_21_2_-1", "0_43_2_-1")
    assert argv[argv.index("--soloUMIposition") + 1] == "3_10_3_21"

    simple = starsolo_argv(_SIMPLE_SOLO, bam_sort_ram_bytes=None, **_RENDER)
    assert "--soloCBposition" not in simple and "--soloCBstart" in simple


def test_only_the_instrument_carries_the_sort_cap_in_its_argv() -> None:
    """The one flag the Snakefile may not receive through the renderer, and why it is not an omission.

    `--limitBAMsortRAM` must escalate with the retry attempt, and snakemake re-expands `resources:`
    per attempt but NOT `params:`. So the module takes it as `SORT_CAP_SHELL`, concatenated into its
    shell template where snakemake formats it, and the renderer emits nothing. An instrument passes
    the number — and omitting it is precisely what left the memory instrument running without the flag
    that bounds STAR's memory.
    """
    assert "--limitBAMsortRAM" not in starsolo_argv(
        _SIMPLE_SOLO, bam_sort_ram_bytes=None, **_RENDER
    )
    assert "--limitBAMsortRAM" not in starsolo_shell_args(_SIMPLE_SOLO, **_RENDER)
    assert "{resources.bam_sort_ram_bytes}" in SORT_CAP_SHELL

    metered = starsolo_argv(_SIMPLE_SOLO, bam_sort_ram_bytes=1234, **_RENDER)
    assert metered[metered.index("--limitBAMsortRAM") + 1] == "1234"


def test_the_cell_filter_fixture_plants_cells_inside_an_ambient_background(tmp_path: Path) -> None:
    """EmptyDrops needs empty droplets: a matrix of nothing but cells would decide nothing.

    The guard's whole cost-effectiveness rests on synthesising its own raw matrix rather than
    borrowing one from a pipeline run (everything under `Solo.out` is `temp()` and gone by then). That
    is only sound if what it synthesises is something a cell caller can have an opinion about — a
    dense minority against a sparse majority — so the fixture's shape is asserted here, where no STAR
    is needed, rather than inferred from the guard going green on a machine that has one.
    """
    from seqforge.e2e import _write_raw_matrix

    raw = tmp_path / "raw"
    planted = _write_raw_matrix(raw, seed=0)
    barcodes = (raw / "barcodes.tsv").read_text().split()
    features = (raw / "features.tsv").read_text().splitlines()
    lines = [ln for ln in (raw / "matrix.mtx").read_text().splitlines() if not ln.startswith("%")]
    header, body = lines[0], lines[1:]
    n_genes, n_bc, nnz = (int(v) for v in header.split())

    # STARsolo writes genes x barcodes, and a transposed fixture would be a silently valid matrix
    # about which the caller would answer a different question.
    assert (n_genes, n_bc) == (len(features), len(barcodes))
    assert len(body) == nnz, "the Matrix Market header must count the entries that follow it"
    assert 0 < planted < n_bc / 10, "the planted cells must be a small minority of the barcodes"

    # per-barcode totals: the planted cells sit an order of magnitude above the ambient droplets,
    # which is the separation the caller is being asked to find.
    totals = [0] * (n_bc + 1)
    for line in body:
        _gene, bc, value = line.split()
        totals[int(bc)] += int(value)
    cells = totals[1 : planted + 1]
    ambient = totals[planted + 1 :]
    assert min(cells) > 10 * (sum(ambient) / len(ambient)), (
        "the planted cells are not distinguishable from the ambient background, so a cell caller "
        "given this matrix is not being asked a question"
    )


def test_the_resume_fingerprint_covers_every_input_that_moves_the_number(tmp_path: Path) -> None:
    """Change any one of them and the fingerprint must change; change none and it must not.

    Pinned per-field rather than as one blob, because the failure mode is a knob QUIETLY missing from
    the key -- and a test that only checks "same args => same hash" would pass with every field
    dropped. Each case below is a measurement that would be silently reused as another's.
    """
    from seqforge.e2e import _cost_fingerprint

    base = dict(
        feature_list=["Gene", "Velocyto"],
        assembly="hg38",
        annotation="gencode_v50",
        n_cells=5000,
        intron_frac=0.4,
        read_len=90,
        max_genes=2000,
        threads=16,
        seed=0,
        star_version="2.7.11b",
        whitelist_entries=6_794_880,
        out_sam_type=("None",),
    )
    # `base` is one heterogeneous dict unpacked into a typed signature, and the sweep below rebuilds
    # it with a DYNAMIC key -- which is the test. A TypedDict types the unpack but cannot express a
    # key computed at runtime, so the suppressions stay rather than the per-field sweep being given
    # up for a single blob comparison the docstring already rejects.
    ref = _cost_fingerprint(**base)  # type: ignore[arg-type]
    assert _cost_fingerprint(**base) == ref, "the same inputs must give the same key"  # type: ignore[arg-type]

    for field, changed in [
        ("feature_list", ["Gene"]),  # a Gene-only number in an all-five curve
        ("assembly", "mm39"),  # a different index is a different intercept
        ("annotation", "gencode_v49"),  # a different feature axis
        ("n_cells", 20_000),  # matrix occupancy
        ("intron_frac", 0.1),  # what Velocyto actually has to do
        ("read_len", 150),
        ("max_genes", 20_000),
        ("threads", 48),  # per-thread buffers are IN the peak -- the audit's case
        ("seed", 1),
        ("star_version", "2.7.10a"),  # a different aligner is a different allocator
        ("whitelist_entries", 737_280),  # a different chemistry's onlist
        # The shipped setting costs buffers -- named, not retyped, so this case keeps varying the
        # value the cost arm exists to price rather than one the module has moved off.
        ("out_sam_type", SHIPPED_OUT_SAM_TYPE),
    ]:
        assert _cost_fingerprint(**{**base, field: changed}) != ref, (  # type: ignore[arg-type]
            f"{field} changes the measured peak but not the resume key -- a run that varied it "
            f"would silently reuse points measured under the old value"
        )


def test_partial_state_is_written_atomically(tmp_path: Path) -> None:
    """A preemption mid-write must not destroy the state that exists to survive preemption."""
    from seqforge.e2e import _atomic_write_json, _load_resumable_points

    p = tmp_path / "s.json"
    _atomic_write_json(p, {"fingerprint": "f", "points": [{"n_reads": 1, "star_peak_rss_gb": 3.0}]})
    assert _load_resumable_points(p, "f") == {1: {"n_reads": 1, "star_peak_rss_gb": 3.0}}
    assert not list(tmp_path.glob("*.tmp")), "the temp file must not survive the rename"


def test_sharded_generation_emits_distinct_reads_not_n_copies_of_one_stream(tmp_path: Path) -> None:
    """N workers must produce N DIFFERENT shards, and the whole must equal the requested depth.

    The failure this exists for is silent by construction: if every shard drew the same seed, the run
    would still emit exactly n_reads records, they would still be valid FASTQ, STAR would still align
    them, and the sweep would still report a peak -- of a library with 1/N the diversity. Nothing
    downstream would notice, which is precisely why the per-shard seed derivation gets an assertion
    rather than a comment.
    """
    from seqforge.e2e import GeneModel, write_cost_fastqs_sharded

    rng = random.Random(3)
    models = [
        GeneModel(
            gene_id=f"G{i}",
            mrna="".join(rng.choice("ACGT") for _ in range(2000)),
            introns=("".join(rng.choice("ACGT") for _ in range(1000)),),
        )
        for i in range(12)
    ]
    cbs = ["".join(rng.choice("ACGT") for _ in range(16)) for _ in range(64)]

    cdna, bc, stats = write_cost_fastqs_sharded(
        models, n_reads=4000, cbs=cbs, out_dir=tmp_path, tag="t", n_workers=4, seed=5
    )
    assert len(cdna) == len(bc) == 4 == stats["n_shards"]
    assert stats["n_reads"] == 4000, "the shard split must not lose or invent reads"
    assert stats["n_exonic"] + stats["n_intronic"] == 4000

    def barcodes(p: Path) -> list[str]:
        with gzip.open(p, "rt") as fh:
            return fh.read().splitlines()[1::4]

    shards = [barcodes(p) for p in bc]
    assert sum(len(s) for s in shards) == 4000
    # the real assertion: no two shards may be the same stream
    for i in range(len(shards)):
        for j in range(i + 1, len(shards)):
            assert shards[i] != shards[j], f"shard {i} and {j} are identical -- same RNG stream"


@pytest.mark.parametrize(
    ("n_reads", "n_workers"), [(101, 1), (7, 4)], ids=["one-worker", "uneven-split"]
)
def test_a_shard_split_still_totals_the_requested_depth(
    tmp_path: Path, n_reads: int, n_workers: int
) -> None:
    """One worker takes the inline path and 7 across 4 is 2/2/2/1: neither may drop the remainder."""
    from seqforge.e2e import GeneModel, write_cost_fastqs_sharded

    models = [GeneModel(gene_id="G0", mrna="ACGT" * 500, introns=("TTGCA" * 200,))]
    _c, _b, stats = write_cost_fastqs_sharded(
        models,
        n_reads=n_reads,
        cbs=["ACGTACGTACGTACGT"],
        out_dir=tmp_path,
        tag="s",
        n_workers=n_workers,
    )
    assert stats["n_reads"] == n_reads and stats["n_shards"] == n_workers


def test_star_reads_sharded_mates_as_comma_separated_lists() -> None:
    """STAR's --readFilesIn takes a list per mate, which is what lets sharding skip a merge."""
    from seqforge.e2e import E2EUnavailable, _fq_arg

    assert _fq_arg(Path("/a/one.fastq.gz")) == "/a/one.fastq.gz"
    assert _fq_arg([Path("/a/s0.gz"), Path("/a/s1.gz")]) == "/a/s0.gz,/a/s1.gz"
    with pytest.raises(E2EUnavailable, match="shards"):
        _fq_arg([])  # no shards would hand STAR an empty --readFilesIn


def test_two_points_are_refused_because_their_residual_cannot_falsify_anything() -> None:
    """A line through 2 points fits them exactly, so max_residual_gb would be 0.0 whatever the truth.

    Found by an adversarial audit of this very file, and it is the sharpest version of the failure
    this repo keeps hitting: `_fit_line`'s docstring PROMISED the residual made linearity "falsifiable
    by its own output", and at n=2 that promise silently inverted -- the run with the LEAST evidence
    advertised the STRONGEST possible evidence of linearity. It is not hypothetical: the sweep is
    deliberately resilient to a point exhausting the cgroup, and the default sweep is three points, so
    one lost point lands on n=2. If the MIDDLE point is lost, every other field is identical to a
    healthy run -- same max_measured_reads, same extrapolation_factor -- and only the slope moves,
    absorbing the noise that should have shown up as residual.
    """
    from seqforge.e2e import _fit_line

    # a wildly nonlinear truth and a perfect line are indistinguishable at n=2
    verdict = _fit_line([(2_000_000, 30.0), (8_000_000, 45.0)])
    assert not verdict["ok"], "2 points must not report a fit"
    assert verdict["n_points"] == 2
    assert verdict["degrees_of_freedom"] == 0
    assert "0.0 by construction" in str(verdict["reason"])
    assert "max_residual_gb" not in verdict, "a refused fit must not report a residual at all"

    # and a physically impossible one (memory FALLING with reads) must not sail through either
    assert not _fit_line([(2_000_000, 30.0), (8_000_000, 12.0)])["ok"]
    # nor one point, nor two readings taken at the same depth
    assert not _fit_line([(1_000_000, 30.0)])["ok"]
    assert not _fit_line([(1_000_000, 30.0), (1_000_000, 31.0)])["ok"]


def test_three_points_is_the_smallest_fit_that_can_be_wrong() -> None:
    """3 points leave 1 degree of freedom, which is the least that makes the residual mean something."""
    from seqforge.e2e import _fit_line

    ok = _fit_line([(1_000_000, 30.0), (2_000_000, 30.1), (4_000_000, 40.0)])
    assert ok["ok"] and ok["n_points"] == 3
    # a bent curve must surface as a residual, not be reported as a line with a clean conscience
    residual = ok["max_residual_gb"]
    assert isinstance(residual, float), "an accepted fit must report the residual that falsifies it"
    assert residual > 1.0


def test_revcomp_is_applied_to_uppercase_so_a_soft_masked_base_cannot_be_laundered(
    tmp_path: Path,
) -> None:
    """A lowercase base must be complemented, not merely reversed and then upper-cased.

    `_COMPLEMENT` maps ACGTN only, so `translate` passes lowercase through untouched. The mRNA path
    used to revcomp first and `.upper()` after, which turned a soft-masked base into a REVERSED BUT
    UNCOMPLEMENTED one wearing plausible uppercase. Every assembly this lab currently ships is
    unmasked (hg38: 1 lowercase base in 119,396,956), so it was dormant -- which is the reason to make
    it structural rather than leave it depending on a property of the FASTA nobody checks. The intron
    path in the same loop was always correct, by the accident of upper-casing first.
    """
    from seqforge.e2e import _revcomp, load_cost_models

    # decisive at the primitive level
    assert _revcomp("acgt".upper()) == "ACGT"
    assert _revcomp("acgt").upper() != _revcomp("acgt".upper()), "the bug's shape, pinned"

    # and through the real loader, on a minus-strand gene whose exons are soft-masked
    body = "acgtacgtac" * 120  # 1200 bp, entirely lowercase
    (tmp_path / "m.fa").write_text(
        ">chr1\n" + "\n".join(body[i : i + 60] for i in range(0, 1200, 60)) + "\n"
    )
    (tmp_path / "m.gtf").write_text(
        'chr1\ts\texon\t1\t500\t.\t-\t.\tgene_id "G1"; gene_biotype "protein_coding";\n'
        'chr1\ts\texon\t900\t1200\t.\t-\t.\tgene_id "G1"; gene_biotype "protein_coding";\n'
    )
    models = load_cost_models(tmp_path / "m.fa", tmp_path / "m.gtf", min_len=100, min_intron=100)
    assert models, "fixture must build a model"
    mrna = models[0].mrna
    assert set(mrna) <= set("ACGTN"), "sequence must be uppercase"
    # the true answer: uppercase the genomic exons, splice, THEN revcomp
    spliced = (body[0:500] + body[899:1200]).upper()
    assert mrna == _revcomp(spliced)
