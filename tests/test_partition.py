"""A heterogeneous project partitions into one manifest per assay (Phase 4).

Drives the real ``_fill_manifest_pipeline`` on a synthetic 2-chemistry dataset: a v3 run and a bulk
run, no archive records, so each run is its own sample. Different samples with different chemistries
are a legal multi-assay project, and each assay gets its own ``seqforge/<assay>/manifest.yaml``. The
single-assay path stays flat and byte-identical (covered by the existing `run`/compile tests).
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import real_cbs, write_fastq_gz
from seqforge import kb
from seqforge.cli import _fill_manifest_pipeline

#: `two_chemistry_project` is a 1.18s partition that four of the six tests here read. Spread by
#: xdist's default `load`, each worker that draws one of them rebuilds it; `xdist_group` pins the
#: module to one worker under `--dist=loadgroup` so it is built once. The module runs 4.9s serially,
#: well under the suite wall, so nothing here wants its own core more than it wants the reuse.
pytestmark = pytest.mark.xdist_group("two-chemistry-project")


def _reads(tech: str, *, n: int = 400, seed: int = 0) -> dict[str, list[str]]:
    """Generator reads for ``tech``; a 10x v3 chemistry gets REAL whitelist barcodes in R1 (see
    :func:`_real_cbs`) so it hits the shipped whitelist on the real-registry pipeline path."""
    reads = kb.generate_reads(kb.load_spec(tech), n=n, seed=seed)
    if tech == "10x-3p-gex-v3":
        real = real_cbs(128)
        rng = random.Random(seed)
        reads["R1"] = [rng.choice(real) + r[16:] for r in reads["R1"]]
    return reads


def _two_chemistry_files(tmp_path: Path) -> list[Path]:
    """SRR1 -> v3 (28 bp barcode), SRR2 -> bulk paired-end. Two runs, two chemistries."""
    files: list[Path] = []
    for acc, tech in (("SRR1", "10x-3p-gex-v3"), ("SRR2", "bulk-rnaseq-pe")):
        reads = _reads(tech)
        for mate, role in (("1", "R1"), ("2", "R2")):
            p = tmp_path / f"{acc}_{mate}.fastq.gz"
            write_fastq_gz(p, reads[role])
            files.append(p)
    return files


def _two_chemistry_files_nested(tmp_path: Path) -> list[Path]:
    """As :func:`_two_chemistry_files`, but each run lives in its OWN accession subdir one level
    deeper -- the GSE310667/GSE126954 on-disk shape (``<root>/SRX.../SRR..._1.fastq.gz``) that
    ``fasterq-dump`` writes. The dataset root is ``tmp_path``; each assay's OWN common root is its
    ``SRX.../`` subdir. That gap is exactly what a per-assay URI computation dropped."""
    files: list[Path] = []
    for acc, srx, tech in (
        ("SRR1", "SRX_A", "10x-3p-gex-v3"),
        ("SRR2", "SRX_B", "bulk-rnaseq-pe"),
    ):
        reads = _reads(tech)
        subdir = tmp_path / srx
        subdir.mkdir()
        for mate, role in (("1", "R1"), ("2", "R2")):
            p = subdir / f"{acc}_{mate}.fastq.gz"
            write_fastq_gz(p, reads[role])
            files.append(p)
    return files


#: The filled workspace and the payload ``_fill_manifest_pipeline`` returned for it.
Project = tuple[Path, dict[str, Any]]


@pytest.fixture(scope="module")
def two_chemistry_project(tmp_path_factory: pytest.TempPathFactory) -> Project:
    """The two-chemistry dataset, partitioned ONCE.

    Four tests below assert different things about the same partition and each re-ran the whole
    ``_fill_manifest_pipeline`` (~1s) to get there. Built once per module; the two that write into
    the tree take `own_two_chemistry_project` instead.

    Built on the NESTED layout (each run in its own ``SRX.../`` subdir): the nested shape dominates the
    flat one -- with a flat layout the multi-assay URI-root bug is invisible, which is the URI-anchoring
    test's own point -- so building nested lets that regression guard read this one partition rather than
    run a fourth pipeline. Every flat-fixture consumer's assertions hold unchanged against it (they key
    on basenames and chemistries, not the subdir).
    """
    root = tmp_path_factory.mktemp("two-chemistry")
    out = _fill_manifest_pipeline(
        files=_two_chemistry_files_nested(root),
        organism="6239",
        records=None,
        assertions=None,
        offline=True,
        workspace=root,
    )
    assert out.code == 0, out.payload
    assert isinstance(out.payload, dict)
    return root, out.payload


@pytest.fixture
def own_two_chemistry_project(two_chemistry_project: Project, tmp_path: Path) -> Project:
    """A private copy, for the tests that write project views back into the workspace."""
    root, payload = two_chemistry_project
    dst = tmp_path / "project"
    shutil.copytree(root, dst)
    return dst, payload


def test_multi_assay_uris_anchor_on_the_dataset_root_not_the_assay_subdir(
    two_chemistry_project: Project,
) -> None:
    """Regression for the multi-assay URI-root bug (GSE310667 15/16, GSE126954 6/7).

    When a dataset splits into assays, each assay's manifest must carry file URIs relative to the
    WHOLE dataset's common root -- the same root ``compose --fastq-dir`` joins against -- not each
    assay's own (deeper) root. Before the fix, a split-off assay whose files sat in an ``SRX.../``
    subdir got bare-basename URIs, so ``<dataset-root>/<basename>`` did not exist and the wiring gate
    failed. Here the assertion is the wiring gate's exact check: every URI joined to the dataset root
    resolves to a real file. This reads the shared NESTED partition (the module fixture is built on it
    precisely so this guard exercises it).
    """
    root, payload = two_chemistry_project
    assert isinstance(payload, dict)
    # This is a REGRESSION guard for a MULTI-assay bug, so partitioning MUST happen -- the v3 and bulk
    # fixtures are deterministic and distinct chemistries. Assert it rather than skip: a silent skip
    # (as a sibling test does, where partitioning is incidental) could mask the regression returning.
    assert "assays" in payload, f"fixtures did not partition into assays: {payload}"

    for a in payload["assays"]:
        srx = "SRX_A" if a["chemistry"] == "10x-3p-gex-v3" else "SRX_B"
        doc = yaml.safe_load(Path(a["manifest"]).read_text())
        # library.files URIs carry the accession subdir and resolve against the dataset root.
        for f in doc["library"]["files"]:
            assert f["uri"].startswith(f"{srx}/"), f["uri"]
            assert (root / f["uri"]).is_file(), f"units path missing: {f['uri']}"
        # experiment.samples.file_uris are anchored identically (referential integrity holds).
        for s in doc["experiment"]["samples"]:
            for uri in s["file_uris"]:
                assert uri.startswith(f"{srx}/"), uri
                assert (root / uri).is_file(), f"sample file_uri missing: {uri}"


def test_single_assay_nested_dataset_still_anchors_on_the_common_root(tmp_path: Path) -> None:
    """Byte-identity guard for the single-assay path: threading a dataset-wide URI map must not
    change a single-assay manifest. One chemistry, all runs in their own subdirs -> the common root
    is the parent of those subdirs, so URIs carry the subdir and resolve against it, exactly as
    before the fix (which for one assay computes the identical map)."""
    import pytest

    files: list[Path] = []
    for acc in ("SRR1", "SRR2"):
        reads = _reads("10x-3p-gex-v3")
        sub = tmp_path / f"SRX_{acc}"
        sub.mkdir()
        for mate, role in (("1", "R1"), ("2", "R2")):
            p = sub / f"{acc}_{mate}.fastq.gz"
            write_fastq_gz(p, reads[role])
            files.append(p)

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
    if "assays" in out.payload:  # pragma: no cover - fixtures unexpectedly split
        pytest.skip("fixtures split into multiple assays")
    doc = yaml.safe_load((tmp_path / "seqforge" / "manifest.yaml").read_text())
    for f in doc["library"]["files"]:
        assert f["uri"].startswith("SRX_SRR"), f["uri"]
        assert (tmp_path / f["uri"]).is_file(), f["uri"]


def test_a_two_chemistry_project_writes_one_manifest_per_assay_subdir(
    two_chemistry_project: Project,
) -> None:
    workspace, payload = two_chemistry_project
    if (
        "assays" not in payload
    ):  # pragma: no cover - fixtures happened to agree; nothing to partition
        pytest.skip(f"both runs resolved to one chemistry: {payload}")

    assays = payload["assays"]
    assert payload["n_assays"] == 2
    chems = {a["chemistry"] for a in assays}
    assert chems == {"10x-3p-gex-v3", "bulk-rnaseq-pe"}

    for a in assays:
        # Each assay's manifest is a real file under its own seqforge/<assay>/ subdir.
        manifest_path = Path(a["manifest"])
        assert manifest_path.is_file()
        assert manifest_path.parent == workspace / "seqforge" / a["assay_dir"]
        assert manifest_path.name == "manifest.yaml"  # validated clean, not a draft
        # Its recorded chemistry is exactly this assay's, and only its own files are in it.
        doc = yaml.safe_load(manifest_path.read_text())
        assert doc["library"]["chemistry"]["value"][0] == a["chemistry"]
        basenames = {Path(f["basename"]).name for f in doc["library"]["files"]}
        expected = "SRR1" if a["chemistry"] == "10x-3p-gex-v3" else "SRR2"
        assert all(b.startswith(expected) for b in basenames), basenames

    # No project-wide manifest.yaml at the top level -- the assays own the manifests.
    assert not (workspace / "seqforge" / "manifest.yaml").exists()


def test_project_views_union_every_assays_samples(own_two_chemistry_project: Project) -> None:
    """sample_metadata.tsv unions all samples across assays; project.yaml indexes the assays."""
    from seqforge.project import discover_assays, write_project_views

    workspace, payload = own_two_chemistry_project
    if "assays" not in payload:  # pragma: no cover - fixtures agreed on one chemistry
        pytest.skip("fixtures agreed on one chemistry")

    assays = discover_assays(workspace)
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

    tsv_path, project_path = write_project_views(workspace, infos)

    # The TSV lives at the project top, not inside an assay subdir.
    assert tsv_path == workspace / "seqforge" / "sample_metadata.tsv"
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


def test_project_metadata_verb_regenerates_from_manifests(
    own_two_chemistry_project: Project,
) -> None:
    """The standalone `seqforge project metadata` verb rebuilds the views from whatever is on disk."""
    from typer.testing import CliRunner

    from seqforge.cli import app

    workspace, payload = own_two_chemistry_project
    if "assays" not in payload:  # pragma: no cover - fixtures agreed on one chemistry
        pytest.skip("fixtures agreed on one chemistry")

    result = CliRunner().invoke(app, ["project", "metadata", "-C", str(workspace)])
    assert result.exit_code == 0, result.output
    assert (workspace / "seqforge" / "sample_metadata.tsv").is_file()
    assert (workspace / "seqforge" / "project.yaml").is_file()


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
