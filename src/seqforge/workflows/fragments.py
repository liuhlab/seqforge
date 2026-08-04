"""Package chromap's scATAC output as a tabix-indexed ``fragments.tsv.gz`` — ``map/chromap``'s deliverable.

The sibling of :mod:`~seqforge.workflows.h5ad` for the ATAC pipeline. Where STARsolo's deliverable is a
count matrix, chromap's is a **fragments file**: one BED-like line per Tn5 insertion pair (``chrom
start end barcode count``), the standard input to every downstream scATAC tool (ArchR, SnapATAC2,
Signac). A count matrix is the wrong shape for ATAC — there are no genes to count — which is the whole
reason ``map/chromap`` needs a deliverable contract of its own rather than reusing ``h5ad``.

Its input contract **is** chromap's own output layout, exactly as ``h5ad``'s is STARsolo's
``Solo.out/`` — a module packages what its aligner writes. chromap emits an unsorted, uncompressed
fragments file; the finalize step sorts it by coordinate, ``bgzip``s it, and builds the ``.tbi`` tabix
index that random-access readers require.

**Why a CLI verb, not a Snakemake ``run:`` block** (same reason as ``h5ad``): ``snakemake -n -p``
renders every ``shell:`` while planning and cannot see inside a ``run:``, so shelling to ``seqforge io
fragments`` keeps the finalize step visible to compose's wiring gate.

``bgzip``/``tabix`` are htslib binaries, so unlike the h5ad step this one runs inside the pinned
``align-dna`` container — the same rule that has chromap. The QC summary, by contrast, is pure Python
over the fragments text, so it (like ``qc_bundle``) needs no container.

This file also **reads** that summary back, for ``seqforge report``: :func:`metrics` and
:func:`read_metrics` at the bottom are ``map/chromap``'s entry in the pipeline-stats registry. They
live here and not in ``stats.py`` because :meth:`FragmentsQC.to_dict` above decides the keys, and a
lookup a package away from the thing that names them drifts in the one direction nothing catches —
the writer renames a key, the reader keeps asking for the old one, and the page silently loses a
column. Here, one file changes or one file breaks. The metrics it produces speak about **fragments**:
there is no count matrix in an ATAC library and the page must not imply one.
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

# Aliased: `build_fragments_qc` below binds a local `count` (a fragments column), and a helper that
# silently means something else inside one function is how a wrong number gets written.
from .metrics import Metric, SampleStats, ratio
from .metrics import count as count_metric


class FragmentsError(RuntimeError):
    """chromap's fragments output cannot be finalized as written (missing file, no fragments)."""


#: What chromap writes for one sample, before finalize — an unsorted, uncompressed BED-like fragments
#: file. A declared output of the align rule (like STARsolo's named matrices), so chromap exiting 0
#: without writing it is a rule failure rather than a silently empty deliverable.
RAW_FRAGMENTS = "fragments.raw.tsv"

#: The retained deliverable suffixes for one sample, in dependency order: the bgzipped fragments file,
#: its tabix index, and the QC summary. Declared by the rule and produced by :func:`write_fragments` /
#: :func:`write_fragments_qc`, so the two cannot drift — one function, two callers, the same discipline
#: ``h5ad_suffixes`` keeps for STARsolo.
_FRAGMENTS_SUFFIX = ".fragments.tsv.gz"
_TABIX_SUFFIX = ".fragments.tsv.gz.tbi"

#: Public, unlike its two siblings above, because it has a reader as well as a writer: the
#: pipeline-stats registry finds one sample's summary by this name. A private constant would have
#: meant the registry spelling the suffix again, and that copy is the one that fails *silently* — a
#: report that finds nothing looks exactly like a pipeline that never ran, so nothing raises and
#: nobody is told. ``rule fragments_qc`` in ``chromap.smk`` imports it as of ``WORKFLOW_VERSION``
#: 2026.8.3, so the rule that declares the file, the function that writes it and the reader that
#: finds it are one owner. That adoption cost a version bump and an invalidated ``run_id`` for a
#: change that altered no behaviour, and a repo-wide check now refuses the literal's return.
QC_SUFFIX = ".fragments.qc.json.gz"


def fragments_suffixes() -> list[str]:
    """The deliverable filename suffixes a ``map/chromap`` run yields per sample, in build order.

    Called from ``chromap.smk`` at parse time to declare the finalize rules' outputs and mirrored by
    :func:`write_fragments` (which produces the first two) — the STARsolo ``h5ad_suffixes`` contract,
    for fragments.
    """
    return [_FRAGMENTS_SUFFIX, _TABIX_SUFFIX, QC_SUFFIX]


@dataclass(frozen=True)
class FragmentsQC:
    """Summary statistics over one sample's fragments file — the ATAC analog of ``qc_bundle``."""

    sample: str
    assembly: str
    n_fragments: int
    n_barcodes: int
    total_reads: int
    #: fragments in the busiest barcode / fragments in the quietest — a crude complexity spread that
    #: does not need the whole per-barcode vector materialized to be useful in a QC glance.
    max_fragments_per_barcode: int
    min_fragments_per_barcode: int

    def to_dict(self) -> dict[str, object]:
        return {
            "sample": self.sample,
            "assembly": self.assembly,
            "n_fragments": self.n_fragments,
            "n_barcodes": self.n_barcodes,
            "total_reads": self.total_reads,
            "max_fragments_per_barcode": self.max_fragments_per_barcode,
            "min_fragments_per_barcode": self.min_fragments_per_barcode,
        }


def _require(binary: str) -> str:
    """Resolve an htslib binary or raise a FragmentsError naming what is missing.

    ``bgzip``/``tabix`` come from the ``align-dna`` container the finalize rule declares; a bare
    ``FileNotFoundError`` three hours into a run is less useful than saying which tool the image lacks.
    """
    path = shutil.which(binary)
    if path is None:
        raise FragmentsError(
            f"{binary!r} is not on PATH; the fragments finalize step needs htslib "
            f"({binary}), which the align-dna container provides — run with "
            f"--software-deployment-method apptainer, or install htslib."
        )
    return path


def write_fragments(raw: Path, out_gz: Path) -> Path:
    """chromap's raw fragments file -> a coordinate-sorted, bgzipped, tabix-indexed ``fragments.tsv.gz``.

    Sort by ``(chrom, start)`` — tabix requires coordinate order — then ``bgzip`` and index as a BED
    (``tabix -p bed``, whose 0-based [start, end) matches a fragments file). The ``.tbi`` lands beside
    ``out_gz`` where tabix writes it. The raw input is read whole (it is one sample's fragments, not a
    FASTQ), then replaced by the compressed form; nothing here streams a genome.
    """
    if not raw.is_file():
        raise FragmentsError(
            f"{raw} is missing; the chromap run that should have written it did not"
        )
    bgzip = _require("bgzip")
    tabix = _require("tabix")
    out_gz.parent.mkdir(parents=True, exist_ok=True)

    # sort -k1,1 -k2,2n: tabix demands (chrom, start) order. The sort is external so a large fragments
    # file is not held in Python memory; bgzip reads the sorted stream on stdin and writes out_gz.
    with out_gz.open("wb") as fh:
        sort = subprocess.Popen(["sort", "-k1,1", "-k2,2n", str(raw)], stdout=subprocess.PIPE)
        try:
            subprocess.run([bgzip, "-c"], stdin=sort.stdout, stdout=fh, check=True)
        finally:
            if sort.stdout is not None:
                sort.stdout.close()
            sort.wait()
    if sort.returncode:
        raise FragmentsError(f"sorting {raw} failed (exit {sort.returncode})")
    subprocess.run([tabix, "-p", "bed", str(out_gz)], check=True)
    return out_gz


def _open_fragments(path: Path) -> IO[str]:
    """Open a fragments file whether it is plain text or bgzipped (``gzip`` reads a bgzip block fine)."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt")
    return path.open()


def build_fragments_qc(fragments: Path, *, sample: str, assembly: str) -> FragmentsQC:
    """Summarize a fragments file (plain or ``.gz``) in one pass — pure Python, no external tool.

    A fragments line is ``chrom<TAB>start<TAB>end<TAB>barcode<TAB>count``; ``count`` is the number of
    read pairs supporting that fragment. Blank lines and ``#`` comment/header lines are skipped. The
    per-barcode tallies are kept as a running dict rather than the full vector, so a many-cell sample
    does not need every fragment resident to report the spread.
    """
    per_barcode: dict[str, int] = {}
    n_fragments = 0
    total_reads = 0
    with _open_fragments(fragments) as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 4:
                raise FragmentsError(
                    f"{fragments}: malformed fragments line {line!r} (need chrom/start/end/barcode)"
                )
            barcode = cols[3]
            count = int(cols[4]) if len(cols) >= 5 and cols[4].isdigit() else 1
            per_barcode[barcode] = per_barcode.get(barcode, 0) + 1
            n_fragments += 1
            total_reads += count
    counts = list(per_barcode.values())
    return FragmentsQC(
        sample=sample,
        assembly=assembly,
        n_fragments=n_fragments,
        n_barcodes=len(per_barcode),
        total_reads=total_reads,
        max_fragments_per_barcode=max(counts) if counts else 0,
        min_fragments_per_barcode=min(counts) if counts else 0,
    )


def write_fragments_qc(fragments: Path, out: Path, *, sample: str, assembly: str) -> Path:
    """Write :func:`build_fragments_qc` as a gzipped JSON, mirroring ``qc_bundle``'s ``.qc.json.gz``."""
    qc = build_fragments_qc(fragments, sample=sample, assembly=assembly)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt") as fh:
        json.dump(qc.to_dict(), fh, indent=2, sort_keys=True)
    return out


# ---- reading the summary back -------------------------------------------------------------------
#
# The reader sits beside the writer for the same reason it does in `qc.py`: `FragmentsQC.to_dict`
# above decides the keys, and everything below looks them up. One file changes, or one file breaks.


def _number(payload: Mapping[str, Any], key: str) -> float | None:
    """One summary value as a number, or ``None`` so the metric is absent rather than a zero.

    Narrower than `qc._as_number`, and deliberately: this artifact is written by `to_dict` above from
    typed fields, so there are no percent strings to cross and nothing to coerce. A string here means
    the payload is not one of ours, which is a reason to drop the metric, not to parse harder.
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def metrics(payload: Mapping[str, Any], sample: str) -> SampleStats:
    """A ``<sample>.fragments.qc.json.gz`` payload -> normalised metrics for one finished sample.

    **Pure**, like ``qc.metrics`` — a dict in, a :class:`~seqforge.workflows.metrics.SampleStats` out,
    no filesystem — so the thresholds below are testable against a literal dict.

    The ATAC column set is genuinely smaller than STARsolo's, and that is a property of the artifact
    rather than an omission here: chromap's summary carries no whitelist-match rate, so there is no
    ATAC equivalent of "valid barcodes" — the metric that catches a wrong chemistry call on the RNA
    side. Nothing below invents one. A column set that varies by module is exactly what
    :class:`~seqforge.workflows.metrics.PipelineStats` was shaped to carry.
    """
    n_fragments = _number(payload, "n_fragments")
    n_barcodes = _number(payload, "n_barcodes")
    total_reads = _number(payload, "total_reads")

    # Read pairs per retained fragment — a PCR-duplication proxy, the one derived number the summary
    # supports. Guarded rather than assumed: a run that produced no fragments would divide by zero.
    per_fragment = (
        total_reads / n_fragments
        if total_reads is not None and n_fragments is not None and n_fragments > 0
        else None
    )
    mean_per_barcode = (
        n_fragments / n_barcodes
        if n_fragments is not None and n_barcodes is not None and n_barcodes > 0
        else None
    )

    built: list[Metric | None] = [
        count_metric(
            "reads",
            "Read pairs",
            total_reads,
            group="input",
            hint="Read pairs supporting the fragments chromap kept.",
            headline=True,
        ),
        count_metric(
            "fragments",
            "Fragments",
            n_fragments,
            group="counts",
            ok=1e6,
            warn=1e5,
            hint="Tn5 insertion pairs in the final fragments file — the ATAC deliverable's size.",
            headline=True,
        ),
        count_metric(
            "barcodes",
            "Barcodes seen",
            n_barcodes,
            group="barcode",
            exact=True,
            hint="Distinct barcodes with at least one fragment. This is NOT a cell count — no cell "
            "calling has happened yet, so background barcodes are included.",
            headline=True,
        ),
        # `ratio` and not `count`, and that is the whole reason `ratio` exists: this is the one
        # derived number here carrying a bar, its bars are 2.0 and 4.0, and an integer display would
        # show 1.9 (ok) and 2.1 (warn) both as "2" and 3.9 (warn) and 4.1 (bad) both as "4" — one
        # string, two colours, which reads as a rendering bug rather than as a threshold.
        ratio(
            "reads_per_fragment",
            "Reads / fragment",
            per_fragment,
            group="duplication",
            ok=2.0,
            warn=4.0,
            higher_is_better=False,
            hint="Duplication proxy: how many read pairs collapsed into each retained fragment. A "
            "high value means the library was sequenced past its complexity.",
            headline=True,
        ),
        ratio(
            "mean_fragments_per_barcode",
            "Mean fragments / barcode",
            mean_per_barcode,
            group="barcode",
            hint="Averaged over every barcode seen, including background — read it as a spread "
            "indicator, not as per-cell depth.",
        ),
        count_metric(
            "max_fragments_per_barcode",
            "Busiest barcode",
            _number(payload, "max_fragments_per_barcode"),
            group="barcode",
            exact=True,
            hint="Fragments in the single busiest barcode.",
        ),
    ]
    # No knee: chromap's summary keeps the per-barcode spread as two numbers rather than the whole
    # vector, so there is no curve to draw and an empty list says so rather than a flat line at zero.
    return SampleStats(sample_id=sample, metrics=[m for m in built if m is not None])


def read_metrics(path: Path, sample: str) -> SampleStats:
    """Load one ``<sample>.fragments.qc.json.gz`` and normalise it.

    The thin half of the adapter, mirroring ``qc.read_metrics``: the loading lives here so the
    registry hands over a path and gets metrics back, and the judgement lives in :func:`metrics`,
    which needs no file to test. Raises ``OSError``/``ValueError`` if the bytes are unusable.
    """
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} is not a fragments QC object")
    return metrics(payload, sample)


__all__ = [
    "QC_SUFFIX",
    "FragmentsError",
    "FragmentsQC",
    "RAW_FRAGMENTS",
    "build_fragments_qc",
    "fragments_suffixes",
    "metrics",
    "read_metrics",
    "write_fragments",
    "write_fragments_qc",
]
