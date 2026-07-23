"""The fingerprint package: a head-slice that reproduces the full dataset's identity.

The load-bearing test is :func:`test_a_fingerprint_run_reproduces_the_full_dataset_hash` — build a
package from full FASTQs, throw the originals away, resolve + fill from the slices, and assert the
``dataset_content_hash`` is byte-identical. That is the whole promise ("even the FASTQ is gone"): the
slice carries the chemistry evidence, the pin carries the identity, and together they are a sufficient
standalone input. The rest guard the properties that make it true — reproducible packaging, valid
records, faithful headers/qualities, and a pin that is independent of the slice size.
"""

from __future__ import annotations

import gzip
import json
import random
from pathlib import Path

import pytest
import yaml

from seqforge import __version__, kb
from seqforge.cli import app
from seqforge.fingerprint.build import build_fingerprint
from seqforge.fingerprint.load import load_fingerprint, probed_from_fingerprint
from seqforge.fingerprint.subsample import read_records, write_records_gz
from seqforge.io import OnlistRegistry
from seqforge.manifest import ExperimentInputs, dataset_content_hash, fill_manifest
from seqforge.models.dataset import DatasetManifest
from seqforge.models.evidenced import EvidencedTaxid
from seqforge.probe import probe_file
from seqforge.probe.streaming import sample_fastq_gz
from seqforge.resolve import resolve_dataset

TECH = "10x-3p-gex-v3"


def _write_fastq_gz(path: Path, seqs: list[str], *, prefix: str = "SIM") -> None:
    """A plain (non-reproducible) writer — stands in for a real upload the slicer never controls."""
    import gzip

    with gzip.open(path, "wt") as fh:
        for i, s in enumerate(seqs):
            fh.write(f"@{prefix}:{i} extra/desc\n{s}\n+\n{'I' * len(s)}\n")


def _synth_dataset(tmp_path: Path, *, n: int = 800) -> tuple[list[Path], OnlistRegistry]:
    """A resolvable 10x v3 dataset: reads drawn from the SAME barcode pool the registry is given."""
    spec = kb.load_spec(TECH)
    pools = kb.build_pools(spec, seed=0)
    reg = OnlistRegistry(offline=True)
    for alias, ref in spec.onlists.items():
        if alias in pools:
            reg.register_synthetic(ref.registry, pools[alias])
    reads = kb.generate_reads(spec, n=n, seed=0, pools=pools)
    paths: list[Path] = []
    for k in (r.id for r in spec.reads):
        p = (
            tmp_path / "SRX999" / f"reads_{k}.fastq.gz"
        )  # a nested tree, so we exercise relative URIs
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_fastq_gz(p, reads[k])
        paths.append(p)
    return paths, reg


def _taxid() -> EvidencedTaxid:
    return EvidencedTaxid(value=6239, basis="user_confirmed", rung=0)


def _manifest_hash(paths: list[Path], reg: OnlistRegistry, probed: dict | None = None) -> str:
    """Resolve + fill a manifest from ``paths`` (optionally via a pinned probe map) → its dataset hash."""
    out = resolve_dataset(paths, registry=reg, use_cache=False, _probed=probed)
    observations = (
        [probed[str(p)][0] for p in paths] if probed is not None else [probe_file(p) for p in paths]
    )
    manifest = fill_manifest(
        result=out.result,
        spec=kb.load_spec(TECH),
        observations=observations,
        registry=reg,
        experiment=ExperimentInputs(organism=_taxid(), accessions=["PRJNA1027859"]),
        seqforge_version=__version__,
    )
    return dataset_content_hash(manifest)


def test_a_fingerprint_run_reproduces_the_full_dataset_hash(tmp_path: Path) -> None:
    """The core invariant: a package cut at N ≥ the probe budget reproduces the full-FASTQ hash.

    The slice's own bytes differ from the originals' (different compressed size, different ISIZE, hence
    a different naive content-key), so this can only hold because the pin carries the whole-file
    identity and the loader stamps it back onto the stand-in probe. Fewer reads than the budget would
    change the observation — here N ≥ the file's read count, so the observation is byte-identical.
    """
    paths, reg = _synth_dataset(tmp_path, n=800)
    full_hash = _manifest_hash(paths, reg)

    result = build_fingerprint(paths, workspace=tmp_path, reads=200_000, name="ds")
    # Prove the originals are not consulted: read the package back from its own directory only.
    loaded = load_fingerprint(result.staging)
    slice_paths, probed = probed_from_fingerprint(loaded)
    fp_hash = _manifest_hash(slice_paths, reg, probed=probed)

    assert fp_hash == full_hash


def test_the_pin_carries_the_whole_file_identity_not_the_slice(tmp_path: Path) -> None:
    """Each pin's sha256/size_bytes equal a full-file probe's — never the (smaller) slice's own."""
    paths, _ = _synth_dataset(tmp_path, n=500)
    result = build_fingerprint(paths, workspace=tmp_path, reads=100, name="ds")  # slice << file
    pins = {p.basename: p for p in result.manifest.files}
    for path in paths:
        obs = probe_file(path)
        pin = pins[path.name]
        assert pin.sha256 == obs.file.sha256
        assert pin.size_bytes == obs.file.size_bytes == path.stat().st_size
        # The slice on disk is strictly smaller than the file it was cut from.
        assert (result.staging / pin.rel_path).stat().st_size < path.stat().st_size


def test_preflight_is_byte_reproducible(tmp_path: Path) -> None:
    """Same inputs at the same N → a byte-identical tar.gz (sorted, mtime=0, fixed ownership)."""
    paths, _ = _synth_dataset(tmp_path, n=400)
    a = build_fingerprint(paths, workspace=tmp_path / "a", reads=200, name="ds")
    b = build_fingerprint(paths, workspace=tmp_path / "b", reads=200, name="ds")
    assert a.package.read_bytes() == b.package.read_bytes()


def test_sliced_records_parse_back_through_the_streamer(tmp_path: Path) -> None:
    """The slice is a valid FASTQ: the probe's own streamer reads exactly the first N records back."""
    paths, _ = _synth_dataset(tmp_path, n=600)
    result = build_fingerprint(paths, workspace=tmp_path, reads=150, name="ds")
    for pin in result.manifest.files:
        assert pin.reads_written == 150
        sample = sample_fastq_gz(result.staging / pin.rel_path, max_reads=10_000, max_bytes=1 << 30)
        assert sample.ok and not sample.truncated
        assert sample.n_reads == 150


def test_the_slice_preserves_original_headers_and_qualities(tmp_path: Path) -> None:
    """A slice re-emits the ORIGINAL four lines — headers and qualities intact, never fabricated."""
    src = tmp_path / "one.fastq.gz"
    _write_fastq_gz(src, ["ACGTACGT", "TTTTGGGG", "CCCCAAAA"], prefix="READ")
    original = read_records(src, max_reads=10, max_bytes=1 << 30)
    assert original.n_reads == 3
    assert original.records[0][0] == b"@READ:0 extra/desc"  # header, verbatim
    assert original.records[0][1] == b"ACGTACGT"  # sequence
    assert original.records[0][3] == b"IIIIIIII"  # quality

    out = tmp_path / "slice.fastq.gz"
    write_records_gz(out, original.records[:2])
    reround = read_records(out, max_reads=10, max_bytes=1 << 30)
    assert reround.records == original.records[:2]


def test_the_slicer_stops_at_the_read_budget(tmp_path: Path) -> None:
    """N bounds the slice: a file with more reads than N yields exactly N records, never the whole file."""
    src = tmp_path / "many.fastq.gz"
    _write_fastq_gz(src, [f"ACGT{'A' * (i % 5)}" for i in range(5000)])
    sl = read_records(src, max_reads=1000, max_bytes=1 << 30)
    assert sl.n_reads == 1000


@pytest.mark.parametrize("reads", [50, 200, 200_000])
def test_the_package_round_trips_from_the_tarball(tmp_path: Path, reads: int) -> None:
    """Loading from the .tar.gz (not the staging dir) yields the same pins and readable slices."""
    paths, _ = _synth_dataset(tmp_path, n=300)
    result = build_fingerprint(paths, workspace=tmp_path, reads=reads, name="ds")
    loaded = load_fingerprint(result.package, unpack_to=tmp_path / "unpacked")
    assert [p.sha256 for p in loaded.manifest.files] == [p.sha256 for p in result.manifest.files]
    slice_paths, probed = probed_from_fingerprint(loaded)
    assert len(probed) == len(paths)
    for sp in slice_paths:
        assert sp.exists()


def _real_v3_dataset(tmp_path: Path, *, n: int = 1500) -> list[Path]:
    """A 10x v3 dataset drawn from the SHIPPED whitelist, so the real registry (what the CLI drives)
    resolves it — synthetic random barcodes would miss ``3M-february-2018`` and be refused absent."""
    from seqforge.io import DEFAULT_REGISTRY
    from seqforge.io.onlist import PackedOnlist, unpack_barcodes

    packed = DEFAULT_REGISTRY.packed("3M-february-2018")
    step = max(1, packed.codes.shape[0] // n)
    cbs = unpack_barcodes(PackedOnlist(packed.width, packed.codes[::step][:n]))
    rng = random.Random(0)
    d = tmp_path / "SRX_demo"
    d.mkdir(parents=True)
    r1, r2 = d / "reads_R1.fastq.gz", d / "reads_R2.fastq.gz"
    with gzip.open(r1, "wt") as f1, gzip.open(r2, "wt") as f2:
        for i in range(n):
            s1 = cbs[i % len(cbs)] + "".join(rng.choice("ACGT") for _ in range(12))
            s2 = "".join(rng.choice("ACGT") for _ in range(90))
            f1.write(f"@R:{i} 1\n{s1}\n+\n{'I' * len(s1)}\n")
            f2.write(f"@R:{i} 2\n{s2}\n+\n{'I' * len(s2)}\n")
    return [r1, r2]


def test_redistributable_package_carries_text_but_no_raw_doc_or_images(tmp_path: Path) -> None:
    """The copyright fix: ``include_raw=False`` drops ``info/docs/`` AND ``info/images/``.

    A public package must not redistribute the raw paper (copyright) nor its extracted figures (the
    figure pipeline is not good enough yet). Only the extracted text survives — which is what harvest
    needs — so the package stays usable while carrying nothing we may not redistribute.
    """
    paths, _ = _synth_dataset(tmp_path, n=200)
    doc = tmp_path / "paper.md"
    doc.write_text("# Methods\n\nWe used the Chromium Single Cell 3' v3 Reagent Kit.\n")

    result = build_fingerprint(
        paths, workspace=tmp_path, reads=100, name="ds", info_docs=[doc], include_raw=False
    )
    info = result.manifest.info
    assert any(rel.startswith("info/text/") for rel in info), "the extracted text must survive"
    assert not any(rel.startswith("info/docs/") for rel in info), "no raw doc may be redistributed"
    assert not any(rel.startswith("info/images/") for rel in info), (
        "no figures may be redistributed"
    )
    # And nothing raw is on disk either — not merely absent from the manifest.
    assert not (result.staging / "info" / "docs").exists()
    assert not (result.staging / "info" / "images").exists()


def test_local_package_still_carries_the_raw_doc_by_default(tmp_path: Path) -> None:
    """The default is a LOCAL package: the original travels verbatim under ``info/docs/``."""
    paths, _ = _synth_dataset(tmp_path, n=200)
    doc = tmp_path / "paper.md"
    doc.write_text("# Methods\n\nChromium Single Cell 3' v3.\n")
    result = build_fingerprint(paths, workspace=tmp_path, reads=100, name="ds", info_docs=[doc])
    assert "info/docs/paper.md" in result.manifest.info
    assert (result.staging / "info" / "docs" / "paper.md").read_text() == doc.read_text()


def test_info_paths_prefers_the_raw_doc_and_falls_back_to_text(tmp_path: Path) -> None:
    """A run reads ``info/docs/`` when present, else ``info/text/`` — so a redistributable run works."""
    paths, _ = _synth_dataset(tmp_path, n=200)
    doc = tmp_path / "paper.md"
    doc.write_text("# Methods\n\nSingle-nucleus RNA-seq of C. elegans neurons.\n")

    local = build_fingerprint(
        paths, workspace=tmp_path / "local", reads=100, name="ds", info_docs=[doc]
    )
    got = load_fingerprint(local.staging).info_paths()
    assert [p.name for p in got] == ["paper.md"] and all("info/docs/" in str(p) for p in got)

    redist = build_fingerprint(
        paths, workspace=tmp_path / "r", reads=100, name="ds", info_docs=[doc], include_raw=False
    )
    got = load_fingerprint(redist.staging).info_paths()
    assert [p.name for p in got] == ["paper.txt"] and all("info/text/" in str(p) for p in got)
    # The fallback document is a real, readable file a run's harvest can normalize.
    assert got[0].read_text().strip()


def test_preflight_redistributable_flag_omits_raw_docs(tmp_path: Path) -> None:
    """End to end through the CLI: ``preflight --redistributable`` writes no ``info/docs/``."""
    from typer.testing import CliRunner

    runner = CliRunner()
    paths = _real_v3_dataset(tmp_path, n=200)
    doc = tmp_path / "paper.md"
    doc.write_text("# Methods\n\nChromium Single Cell 3' v3.\n")
    res = runner.invoke(
        app,
        [
            "preflight",
            *map(str, paths),
            "--name",
            "demo",
            "--redistributable",
            "--doc",
            str(doc),
            "-C",
            str(tmp_path / "ws"),
        ],
    )
    assert res.exit_code == 0, res.output
    info = json.loads(res.stdout)["info"]
    assert any(r.startswith("info/text/") for r in info)
    assert not any(r.startswith("info/docs/") for r in info)


def test_strip_to_redistributable_drops_raw_docs_and_preserves_the_hash(tmp_path: Path) -> None:
    """Retroactive copyright fix: strip a full package to text-only WITHOUT changing the dataset hash.

    A package built with the raw paper (the pre-flag default, and what the worm deliverables shipped)
    must be repackable into a redistributable copy — no info/docs/, no info/images/, only info/text/ —
    and because the reads and pins are untouched, the manifest hash a run reproduces must be identical.
    """
    from seqforge.fingerprint.build import strip_to_redistributable

    paths, reg = _synth_dataset(tmp_path, n=800)
    doc = tmp_path / "paper.md"
    doc.write_text("# Methods\n\nChromium Single Cell 3' v3 of C. elegans.\n")
    full = build_fingerprint(paths, workspace=tmp_path, reads=200_000, name="ds", info_docs=[doc])
    assert any(r.startswith("info/docs/") for r in full.manifest.info)

    stripped = strip_to_redistributable(full.package, tmp_path / "redist.fingerprint.tar.gz")
    assert not any(r.startswith("info/docs/") for r in stripped.manifest.info)
    assert not any(r.startswith("info/images/") for r in stripped.manifest.info)
    assert any(r.startswith("info/text/") for r in stripped.manifest.info), "text must survive"

    # The pins are byte-identical, so the manifest hash reproduces from either package.
    full_loaded = load_fingerprint(full.package, unpack_to=tmp_path / "u-full")
    redist_loaded = load_fingerprint(stripped.package, unpack_to=tmp_path / "u-redist")
    fp_full, probed_full = probed_from_fingerprint(full_loaded)
    fp_redist, probed_redist = probed_from_fingerprint(redist_loaded)
    assert _manifest_hash(fp_full, reg, probed=probed_full) == _manifest_hash(
        fp_redist, reg, probed=probed_redist
    )
    # And the raw doc is truly gone from the redistributable tree.
    assert redist_loaded.info_paths() and all(
        "info/text/" in str(p) for p in redist_loaded.info_paths()
    )


def test_run_from_fingerprint_cli_reproduces_the_hash(tmp_path: Path) -> None:
    """End to end through the CLI: ``preflight`` then ``run --fingerprint`` yields the same manifest.

    Exercises the argument plumbing the API tests skip — the optional ``files`` argument, ``--fingerprint``
    unpacking, and the pinned probe map threaded into ``run``. Both invocations stop at the genome-less
    processing stage (exit 2); the manifest they each write is what must match.
    """
    from typer.testing import CliRunner

    runner = CliRunner()
    paths = _real_v3_dataset(tmp_path)
    ws_full, ws_fp = tmp_path / "full", tmp_path / "fp"

    pf = runner.invoke(app, ["preflight", *map(str, paths), "--name", "demo", "-C", str(ws_fp)])
    assert pf.exit_code == 0, pf.output
    package = json.loads(pf.stdout)["package"]

    base = ["--no-llm", "--organism", "6239", "--offline"]
    runner.invoke(app, ["run", *map(str, paths), *base, "-C", str(ws_full)])
    runner.invoke(app, ["run", "--fingerprint", package, *base, "-C", str(ws_fp)])

    def _hash(ws: Path) -> str:
        manifest = DatasetManifest.model_validate(
            yaml.safe_load((ws / "seqforge" / "manifest.yaml").read_text())
        )
        return dataset_content_hash(manifest)

    assert _hash(ws_full) == _hash(ws_fp)
