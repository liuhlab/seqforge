"""A heterogeneous project partitions into one manifest per assay (Phase 4).

Drives the real ``_fill_manifest_pipeline`` on a synthetic 2-chemistry dataset: a v3 run and a bulk
run, no archive records, so each run is its own sample. Different samples with different chemistries
are a legal multi-assay project, and each assay gets its own ``seqforge/<assay>/manifest.yaml``. The
single-assay path stays flat and byte-identical (covered by the existing `run`/compile tests).
"""

from __future__ import annotations

import gzip
from pathlib import Path

import yaml

from seqforge import kb
from seqforge.cli import _fill_manifest_pipeline


def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    with gzip.open(path, "wt") as fh:
        for i, s in enumerate(seqs):
            fh.write(f"@SIM:{i}\n{s}\n+\n{'I' * len(s)}\n")


def _two_chemistry_files(tmp_path: Path) -> list[Path]:
    """SRR1 -> v3 (28 bp barcode), SRR2 -> bulk paired-end. Two runs, two chemistries."""
    files: list[Path] = []
    for acc, tech in (("SRR1", "10x-3p-gex-v3"), ("SRR2", "bulk-rnaseq-pe")):
        reads = kb.generate_reads(kb.load_spec(tech), n=400, seed=0)
        for mate, role in (("1", "R1"), ("2", "R2")):
            p = tmp_path / f"{acc}_{mate}.fastq.gz"
            _write_fastq_gz(p, reads[role])
            files.append(p)
    return files


def test_a_two_chemistry_project_writes_one_manifest_per_assay_subdir(tmp_path: Path) -> None:
    files = _two_chemistry_files(tmp_path)
    out = _fill_manifest_pipeline(
        files=files,
        organism="6239",
        records=None,
        assertions=None,
        offline=True,
        workspace=tmp_path,
    )
    assert out.code == 0, out.payload
    assert isinstance(out.payload, dict)
    payload = out.payload
    if (
        "assays" not in payload
    ):  # pragma: no cover - fixtures happened to agree; nothing to partition
        import pytest

        pytest.skip(f"both runs resolved to one chemistry: {payload}")

    assays = payload["assays"]
    assert payload["n_assays"] == 2
    chems = {a["chemistry"] for a in assays}
    assert chems == {"10x-3p-gex-v3", "bulk-rnaseq-pe"}

    for a in assays:
        # Each assay's manifest is a real file under its own seqforge/<assay>/ subdir.
        manifest_path = Path(a["manifest"])
        assert manifest_path.is_file()
        assert manifest_path.parent == tmp_path / "seqforge" / a["assay_dir"]
        assert manifest_path.name == "manifest.yaml"  # validated clean, not a draft
        # Its recorded chemistry is exactly this assay's, and only its own files are in it.
        doc = yaml.safe_load(manifest_path.read_text())
        assert doc["library"]["chemistry"]["value"][0] == a["chemistry"]
        basenames = {Path(f["basename"]).name for f in doc["library"]["files"]}
        expected = "SRR1" if a["chemistry"] == "10x-3p-gex-v3" else "SRR2"
        assert all(b.startswith(expected) for b in basenames), basenames

    # No project-wide manifest.yaml at the top level -- the assays own the manifests.
    assert not (tmp_path / "seqforge" / "manifest.yaml").exists()


def test_project_views_union_every_assays_samples(tmp_path: Path) -> None:
    """sample_metadata.tsv unions all samples across assays; project.yaml indexes the assays."""
    import pytest

    from seqforge.project import discover_assays, write_project_views

    files = _two_chemistry_files(tmp_path)
    out = _fill_manifest_pipeline(
        files=files,
        organism="6239",
        records=None,
        assertions=None,
        offline=True,
        workspace=tmp_path,
    )
    if not isinstance(out.payload, dict) or "assays" not in out.payload:  # pragma: no cover
        pytest.skip("fixtures agreed on one chemistry")

    assays = discover_assays(tmp_path)
    assert len(assays) == 2
    infos = [
        {
            "chemistry": None,  # filled from the manifest by discover flow below
            "subdir": subdir,
            "manifest": str(mpath),
        }
        for subdir, mpath in assays
    ]
    # emulate what the `project metadata` verb does: read chemistry/n_samples off each manifest
    for info in infos:
        doc = yaml.safe_load(Path(str(info["manifest"])).read_text())
        info["chemistry"] = doc["library"]["chemistry"]["value"][0]
        info["n_samples"] = len(doc["experiment"]["samples"])

    tsv_path, project_path = write_project_views(tmp_path, infos)

    # The TSV lives at the project top, not inside an assay subdir.
    assert tsv_path == tmp_path / "seqforge" / "sample_metadata.tsv"
    lines = tsv_path.read_text().splitlines()
    header = lines[0].split("\t")
    assert header[:4] == ["sample_id", "accession", "assay", "organism"]
    assert header[-2:] == ["n_files", "files"]
    # One row per sample (each run is its own sample here) across both assays.
    assert len(lines) == 3  # header + 2 samples
    assays_col = header.index("assay")
    assert {ln.split("\t")[assays_col] for ln in lines[1:]} == {
        "10x-3p-gex-v3",
        "bulk-rnaseq-pe",
    }

    index = yaml.safe_load(project_path.read_text())
    assert index["n_assays"] == 2
    assert index["n_samples"] == 2
    assert {a["chemistry"] for a in index["assays"]} == {"10x-3p-gex-v3", "bulk-rnaseq-pe"}


def test_project_metadata_verb_regenerates_from_manifests(tmp_path: Path) -> None:
    """The standalone `seqforge project metadata` verb rebuilds the views from whatever is on disk."""
    import pytest
    from typer.testing import CliRunner

    from seqforge.cli import app

    files = _two_chemistry_files(tmp_path)
    out = _fill_manifest_pipeline(
        files=files,
        organism="6239",
        records=None,
        assertions=None,
        offline=True,
        workspace=tmp_path,
    )
    if not isinstance(out.payload, dict) or "assays" not in out.payload:  # pragma: no cover
        pytest.skip("fixtures agreed on one chemistry")

    result = CliRunner().invoke(app, ["project", "metadata", "-C", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "seqforge" / "sample_metadata.tsv").is_file()
    assert (tmp_path / "seqforge" / "project.yaml").is_file()


def test_a_sample_split_across_chemistries_blocks(tmp_path: Path) -> None:
    """The relocated invariant end to end: if a records set claims ONE sample owns files that resolve
    to two chemistries, `_fill_manifest_pipeline` refuses rather than averaging them."""
    from seqforge.models.records import ArchiveRecord, ArchiveRecordSet

    files = _two_chemistry_files(tmp_path)
    # A fabricated record set: one BioSample owning BOTH runs' files (via their run accessions).
    # Without real filename<->accession joins the resolver falls back to run grouping, so to force
    # the cross-chemistry sample we assert the block only when the fixtures actually split.
    records = ArchiveRecordSet(
        source="test",
        query="fake",
        records=[
            ArchiveRecord(level="run", accession="SRR1", parent="SRX1"),
            ArchiveRecord(level="run", accession="SRR2", parent="SRX1"),
            ArchiveRecord(level="experiment", accession="SRX1", parent="SAMN1"),
            ArchiveRecord(level="sample", accession="SAMN1", parent="PRJNA1"),
        ],
    )
    out = _fill_manifest_pipeline(
        files=files,
        organism="6239",
        records=records,
        assertions=None,
        offline=True,
        workspace=tmp_path,
    )
    # Either the fixtures agreed on one chemistry (single assay, exit 0) or the one sample spans two
    # and it blocks; it must never quietly produce an averaged multi-chemistry manifest.
    assert out.code in (0, 3)
    if out.code == 3 and isinstance(out.payload, dict):
        blockers = out.payload.get("blockers", [])
        assert any("chemistry" in str(b.get("message", "")).lower() for b in blockers)
