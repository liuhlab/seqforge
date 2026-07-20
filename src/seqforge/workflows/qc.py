"""Bundle STARsolo's scattered stats + run logs into one gzipped JSON per sample.

This is a finalize step of ``map/starsolo``: once ``<sample>.h5ad`` captures the counts, STAR's small
per-feature stat files, its knee-plot vectors, its run logs, and its splice-junction table are all
that is worth keeping — and they are worth keeping, for a future experiment-level QC pass. STAR
scatters them across ``Solo.out/<Feature>/`` and the sample directory as a dozen little text files;
this collapses them into **one** self-describing ``<sample>.qc.json.gz`` and lets the rule that calls
it ``temp()``-delete the originals.

JSON (gzipped), not pickle, on purpose: a QC corpus that outlives any one code version must not be
readable only by the exact class that wrote it. Text is portable, diffable, and language-agnostic;
gzip absorbs the one bulky field (``UMIperCellSorted``, one integer per barcode).

Like ``h5ad.py`` this shells out through a ``seqforge io`` verb rather than a Snakemake ``run:`` block
so ``snakemake -n -p`` (compose's wiring gate) can see the command.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from ..models.processing import SoloFeature
from .h5ad import _gene_axis, _stackable


class QcError(RuntimeError):
    """A file STAR was supposed to write is missing or unreadable, so the bundle cannot be built."""


def _read(path: Path) -> str:
    if not path.exists():
        raise QcError(f"{path} is missing; the STAR run that should have written it did not")
    return path.read_text()


def _coerce(value: str) -> object:
    """A stat value as the narrowest type it cleanly is: ``int``, then ``float``, else the raw string.

    STAR mixes integers (read counts), floats (rates), and strings (``95.5%``, timestamps) freely in
    these files. Coercing the clean cases keeps the JSON queryable; leaving the rest as strings keeps
    it lossless — no value is reshaped into a number it is not.
    """
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _parse_kv(text: str, sep: str) -> dict[str, object]:
    """``key<sep>value`` lines -> dict. Blank lines skipped; a line without ``sep`` is dropped."""
    out: dict[str, object] = {}
    for line in text.splitlines():
        if not line.strip() or sep not in line:
            continue
        key, value = line.split(sep, 1)
        out[key.strip()] = _coerce(value.strip())
    return out


def _parse_whitespace_kv(text: str) -> dict[str, object]:
    """``name   value`` (STAR's ``.stats`` files) -> dict, first token key, remainder value."""
    out: dict[str, object] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        out[parts[0]] = _coerce(parts[1]) if len(parts) == 2 else " ".join(parts[1:])
    return out


def _parse_log_final(text: str) -> dict[str, object]:
    """``Log.final.out``: ``   Label |\\tvalue`` lines, with section headers (no ``|``) skipped."""
    return _parse_kv(text, "|")


def _parse_umi_per_cell(text: str) -> list[int]:
    """``UMIperCellSorted.txt``: one integer per line, already descending. The knee-plot data."""
    return [int(line) for line in text.splitlines() if line.strip()]


def _parse_sj(text: str) -> list[list[str]]:
    """``SJ.out.tab``: tab-separated collapsed splice junctions, one list of columns per row."""
    return [line.split("\t") for line in text.splitlines() if line.strip()]


def _read_lines(path: Path) -> list[str]:
    return [line for line in _read(path).splitlines() if line.strip()]


def build_qc_bundle(
    solo_dir: Path,
    run_dir: Path,
    features: list[SoloFeature],
    *,
    sample: str,
    assembly: str | None,
) -> dict[str, object]:
    """Every STAR stat/log for one sample, as one JSON-serialisable dict.

    ``solo_dir`` is the sample's ``Solo.out``; ``run_dir`` is the sample directory holding the
    top-level logs. ``assembly`` is recorded for CRAM-reference provenance (the ``<sample>.cram`` in
    the same directory pins the exact reference bytes by MD5, and this names which assembly that is).
    """
    bundle: dict[str, object] = {
        "sample": sample,
        "assembly": assembly,
        "soloFeatures": list(features),
        "barcodes_stats": _parse_whitespace_kv(_read(solo_dir / "Barcodes.stats")),
        "summary": {
            feat: _parse_kv(_read(solo_dir / feat / "Summary.csv"), ",") for feat in features
        },
        "features_stats": {
            feat: _parse_whitespace_kv(_read(solo_dir / feat / "Features.stats"))
            for feat in features
        },
        "umi_per_cell": {
            feat: _parse_umi_per_cell(_read(solo_dir / feat / "UMIperCellSorted.txt"))
            for feat in _stackable(features)
        },
        # What STAR's default cell filter called -- kept because we drop the filtered matrix (the
        # h5ad is built from raw/), and this tiny list is the only surviving record of that call.
        "default_filtered_barcodes": {
            feat: _read_lines(solo_dir / feat / "filtered" / "barcodes.tsv")
            for feat in _gene_axis(features)
        },
        "log_final": _parse_log_final(_read(run_dir / "Log.final.out")),
        "log_out": _read(run_dir / "Log.out"),
        "log_progress": _read(run_dir / "Log.progress.out"),
        "splice_junctions": _parse_sj(_read(run_dir / "SJ.out.tab")),
    }
    return bundle


def write_qc_bundle(
    solo_dir: Path,
    run_dir: Path,
    features: list[SoloFeature],
    out: Path,
    *,
    sample: str,
    assembly: str | None = None,
) -> Path:
    """Build the bundle and write it as gzipped JSON to ``out``. Returns ``out``."""
    bundle = build_qc_bundle(solo_dir, run_dir, features, sample=sample, assembly=assembly)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(bundle, fh)
    return out


__all__ = ["QcError", "build_qc_bundle", "write_qc_bundle"]
