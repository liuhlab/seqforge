"""Tests for `workflows.fragments` — the `map/chromap` deliverable contract.

The QC path is pure Python over the fragments text, so it runs anywhere; the finalize path shells to
htslib (`bgzip`/`tabix`) and is skipped when those are absent, exactly as the STAR integration tests
skip without STAR.
"""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest

from seqforge.workflows.fragments import (
    FragmentsError,
    build_fragments_qc,
    fragments_suffixes,
    write_fragments,
    write_fragments_qc,
)

_HTSLIB = shutil.which("bgzip") is not None and shutil.which("tabix") is not None

# chrom  start  end  barcode  count -- three fragments across two cells, deliberately out of coordinate
# order so the finalize sort is exercised.
_RAW = (
    "chr2\t50\t60\tCCC\t3\n"
    "chr1\t300\t400\tAAA\t1\n"
    "chr1\t100\t200\tAAA\t2\n"
)


def test_fragments_suffixes_are_the_three_deliverables_in_build_order() -> None:
    assert fragments_suffixes() == [
        ".fragments.tsv.gz",
        ".fragments.tsv.gz.tbi",
        ".fragments.qc.json.gz",
    ]


# -- QC (pure Python) -------------------------------------------------------


def test_build_fragments_qc_counts_fragments_barcodes_and_reads(tmp_path: Path) -> None:
    raw = tmp_path / "fragments.raw.tsv"
    raw.write_text(_RAW)

    qc = build_fragments_qc(raw, sample="s1", assembly="mm10")

    assert qc.sample == "s1"
    assert qc.assembly == "mm10"
    assert qc.n_fragments == 3
    assert qc.n_barcodes == 2  # AAA, CCC
    assert qc.total_reads == 6  # 3 + 1 + 2
    assert qc.max_fragments_per_barcode == 2  # AAA has two fragments
    assert qc.min_fragments_per_barcode == 1  # CCC has one


def test_build_fragments_qc_reads_a_bgzipped_or_plain_file(tmp_path: Path) -> None:
    # A .gz suffix is opened through gzip; the counts must match the plain-text read.
    gz = tmp_path / "fragments.tsv.gz"
    with gzip.open(gz, "wt") as fh:
        fh.write(_RAW)
    qc = build_fragments_qc(gz, sample="s1", assembly="mm10")
    assert qc.n_fragments == 3
    assert qc.n_barcodes == 2


def test_build_fragments_qc_skips_comment_and_blank_lines(tmp_path: Path) -> None:
    raw = tmp_path / "fragments.raw.tsv"
    raw.write_text("# a header comment\n\n" + _RAW)
    qc = build_fragments_qc(raw, sample="s1", assembly="mm10")
    assert qc.n_fragments == 3  # the comment and blank line are not fragments


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
