"""The STAR stats bundle — one gzipped JSON per sample, built from a dozen scattered text files.

The gate that matters: the bundle is built from exactly the files the ``qc_bundle`` rule hands it
(the ``temp()`` outputs), every value round-trips through gzipped JSON, and a missing file is a loud
refusal rather than a silent gap — because once the raw matrices are gone this bundle is the only
surviving record of the run's QC.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from seqforge.workflows.qc import QcError, build_qc_bundle, write_qc_bundle


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _fake_run(tmp_path: Path, features: list[str]) -> tuple[Path, Path]:
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
    features = ["Gene", "GeneFull", "Velocyto"]
    solo, run_dir = _fake_run(tmp_path, features)
    bundle = build_qc_bundle(
        solo,
        run_dir,
        features,  # type: ignore[arg-type]
        sample="S1",
        assembly="ce11",
    )

    assert bundle["sample"] == "S1"
    assert bundle["assembly"] == "ce11"
    assert bundle["soloFeatures"] == features
    # Summary.csv coerced to typed values, per feature.
    assert bundle["summary"]["Gene"]["Number of Reads"] == 1000  # type: ignore[index]
    assert bundle["summary"]["Gene"]["Sequencing Saturation"] == 0.42  # type: ignore[index]
    # Whitespace .stats files.
    assert bundle["barcodes_stats"]["nMatch"] == 900  # type: ignore[index]
    assert bundle["features_stats"]["Velocyto"]["yesWLmatch"] == 900  # type: ignore[index]
    # UMIperCellSorted only for the stackable features.
    assert bundle["umi_per_cell"]["Gene"] == [50, 30, 10]  # type: ignore[index]
    assert "Velocyto" not in bundle["umi_per_cell"]  # type: ignore[operator]
    # filtered/barcodes.tsv kept as provenance of STAR's default cell call, for every gene-axis feat.
    assert bundle["default_filtered_barcodes"]["Velocyto"] == ["AAAA", "CCCC"]  # type: ignore[index]
    # Log.final.out parsed on `|`; free-text logs kept whole; SJ rows split on tab.
    assert bundle["log_final"]["Number of input reads"] == 1000  # type: ignore[index]
    assert bundle["log_final"]["Uniquely mapped %"] == "95.00%"  # type: ignore[index]
    assert "STAR version" in bundle["log_out"]  # type: ignore[operator]
    assert bundle["splice_junctions"][0] == ["chrI", "100", "200", "1", "1", "1", "10", "0", "30"]  # type: ignore[index]


def test_write_qc_bundle_round_trips_through_gzipped_json(tmp_path: Path) -> None:
    features = ["Gene", "Velocyto"]
    solo, run_dir = _fake_run(tmp_path, features)
    out = tmp_path / "S1.qc.json.gz"
    written = write_qc_bundle(solo, run_dir, features, out, sample="S1", assembly="ce11")  # type: ignore[arg-type]

    assert written == out and out.exists()
    with gzip.open(out, "rt", encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded["sample"] == "S1"
    assert loaded["summary"]["Gene"]["Number of Reads"] == 1000
    assert loaded["umi_per_cell"]["Gene"] == [50, 30, 10]


def test_a_missing_star_file_is_a_refusal_not_a_silent_gap(tmp_path: Path) -> None:
    features = ["Gene"]
    solo, run_dir = _fake_run(tmp_path, features)
    (solo / "Gene" / "Summary.csv").unlink()
    with pytest.raises(QcError, match="Summary.csv"):
        build_qc_bundle(solo, run_dir, features, sample="S1", assembly="ce11")  # type: ignore[arg-type]
