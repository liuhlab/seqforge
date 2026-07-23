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
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO


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
_QC_SUFFIX = ".fragments.qc.json.gz"


def fragments_suffixes() -> list[str]:
    """The deliverable filename suffixes a ``map/chromap`` run yields per sample, in build order.

    Called from ``chromap.smk`` at parse time to declare the finalize rules' outputs and mirrored by
    :func:`write_fragments` (which produces the first two) — the STARsolo ``h5ad_suffixes`` contract,
    for fragments.
    """
    return [_FRAGMENTS_SUFFIX, _TABIX_SUFFIX, _QC_SUFFIX]


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


__all__ = [
    "FragmentsError",
    "FragmentsQC",
    "RAW_FRAGMENTS",
    "build_fragments_qc",
    "fragments_suffixes",
    "write_fragments",
    "write_fragments_qc",
]
