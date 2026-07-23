"""``preflight`` — build a portable fingerprint package from a dataset's FASTQs (+ optional prose).

One full-file bounded probe per FASTQ captures the *identity* the slice cannot recompute (content
address, compressed size, ISIZE); the slicer captures the first N *records* — enough chemistry
evidence to reproduce the resolve verdict when N ≥ the probe budget. The two are written into a staged
tree that mirrors the originals' directory structure (so the manifest's relative URIs reproduce), a
pin (``fingerprint.json``) ties them together, and the tree is packed into a deterministic
``.fingerprint.tar.gz``.

Nothing here reads a whole FASTQ: both the probe and the slicer stop at the ``(reads, max_bytes)``
budget. The package is the point — from it the whole pipeline reproduces the same manifest, hash and
all, with the original bytes gone.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..models.fingerprint import FINGERPRINT_VERSION, FilePin, FingerprintManifest
from ..probe import DEFAULT_MAX_BYTES, DEFAULT_MAX_READS, PROBE_VERSION, gzip_isize, probe_file
from ..workspace import fingerprint_dir, readable
from .pack import extract_info, write_tar_gz
from .subsample import Record, read_records, write_records_gz


@dataclass(frozen=True)
class FingerprintResult:
    """What ``build_fingerprint`` produced: the portable archive, the staged tree, and the pin."""

    package: Path
    staging: Path
    manifest: FingerprintManifest

    @property
    def total_reads_written(self) -> int:
        return sum(f.reads_written for f in self.manifest.files)

    @property
    def package_bytes(self) -> int:
        return self.package.stat().st_size if self.package.exists() else 0


def _common_root(files: list[Path]) -> Path | None:
    """The directory the FASTQs' relative paths are anchored to — ``os.path.commonpath`` of their
    resolved parents, mirroring :func:`seqforge.manifest.fill.dataset_uris` so the package's tree
    reproduces the manifest's relative URIs. ``None`` when they span filesystems (basename fallback)."""
    try:
        return Path(os.path.commonpath([str(f.parent.resolve()) for f in files]))
    except ValueError:
        return None


def _rel_paths(files: list[Path]) -> dict[str, str]:
    """Map each file (by resolved str) to its path relative to the dataset root — the tree to preserve.

    Structure-preserving when the files share a root (``SRX123/reads_1.fastq.gz`` stays nested); a flat
    basename when they do not, exactly as ``dataset_uris`` degrades. Preserving the tree is what makes
    the manifest's relative URI — and therefore the dataset hash — reproduce from the slice.
    """
    root = _common_root(files)
    if root is None:
        return {str(f.resolve()): f.name for f in files}
    return {str(f.resolve()): str(f.resolve().relative_to(root)) for f in files}


def _package_digest(pins: list[FilePin], reads: int) -> str:
    """A content-address for the package: sorted file identities + the read budget + the format version.

    Two ``preflight`` runs over the same dataset at the same N name the same package, so the deliverable
    is idempotent. The reads budget is folded in because a 10k-read and a 200k-read fingerprint of one
    dataset are different artifacts (different size, possibly different reproducibility guarantee).
    """
    h = hashlib.sha256()
    h.update(f"seqforge-fingerprint\x00{FINGERPRINT_VERSION}\x00{reads}\n".encode())
    for pin in sorted(pins, key=lambda p: p.sha256):
        h.update(f"{pin.sha256}\x00{pin.size_bytes}\x00{pin.rel_path}\n".encode())
    return h.hexdigest()


def build_fingerprint(
    files: list[str | Path],
    *,
    workspace: str | Path = ".",
    reads: int = DEFAULT_MAX_READS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    info_docs: list[str | Path] | None = None,
    name: str | None = None,
    include_raw: bool = True,
) -> FingerprintResult:
    """Build a fingerprint package under ``seqforge/fingerprint/`` and return where it landed.

    Parameters
    ----------
    files
        The dataset's gzipped FASTQ files.
    reads
        N — the head-slice budget. Each file keeps its first ``min(N, file reads)`` records. For a
        package that reproduces the full-file manifest (hash included), keep N ≥ the probe's read
        budget; a smaller N is a deliberately lighter fingerprint for the size/accuracy study.
    max_bytes
        Decompressed-byte safety cap the slicer honours alongside ``reads`` (the bounded-read rule).
    info_docs
        Optional paper/spreadsheet documents to carry (original + extracted text/images).
    name
        A human slug for the package; defaults to the dataset root's directory name.
    include_raw
        ``True`` (default) builds a **local** package — the original documents and any extracted PDF
        images travel alongside the text. ``False`` builds a **redistributable** package that carries
        only the extracted text under ``info/text/``: the raw paper is a copyright liability we do not
        redistribute, and figures are dropped until the figure-extraction pipeline improves. A run
        falls back to the text (see :meth:`LoadedFingerprint.info_paths`), so it stays usable.
    """
    paths = [Path(f) for f in files]
    rels = _rel_paths(paths)
    root = _common_root(paths)
    slug = name or (root.name if root is not None else "dataset") or "dataset"

    pins: list[FilePin] = []
    # (dest rel_path, records) — staged in memory, written once the content-addressed name is known.
    staged_fastq: list[tuple[str, list[Record]]] = []
    for path in paths:
        # Full-file bounded probe: the identity a real run would assign (independent of N).
        obs = probe_file(path, max_reads=DEFAULT_MAX_READS, max_bytes=DEFAULT_MAX_BYTES)
        isize = gzip_isize(path)
        # The slice: the first N complete records (bounded; never a whole-file read).
        sl = read_records(path, max_reads=reads, max_bytes=max_bytes)
        rel = rels[str(path.resolve())]
        pkg_rel = str(Path("fastq") / rel)
        pins.append(
            FilePin(
                rel_path=pkg_rel,
                basename=path.name,
                sha256=obs.file.sha256,
                size_bytes=obs.file.size_bytes,
                isize=isize,
                reads_written=sl.n_reads,
                estimated_total_reads=obs.estimated_total_reads,
            )
        )
        staged_fastq.append((pkg_rel, sl.records))

    return assemble_package(
        slug,
        pins,
        staged_fastq,
        workspace=workspace,
        reads=reads,
        max_bytes=max_bytes,
        info_docs=info_docs,
        include_raw=include_raw,
    )


def assemble_package(
    slug: str,
    pins: list[FilePin],
    staged_fastq: list[tuple[str, list[Record]]],
    *,
    workspace: str | Path = ".",
    reads: int = DEFAULT_MAX_READS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    info_docs: list[str | Path] | None = None,
    include_raw: bool = True,
) -> FingerprintResult:
    """Stage pinned slices + carried prose into a content-addressed ``.fingerprint.tar.gz``.

    The source-agnostic tail of a fingerprint build: given the per-file pins (identity) and their
    staged records (the slices), it names the package by :func:`_package_digest`, writes each slice with
    the reproducible gzip writer, carries the info docs, writes ``fingerprint.json``, and packs a
    deterministic tar. Both :func:`build_fingerprint` (local files) and ``io.sra.build_fingerprint_sra``
    (an SRA stream) build pins + staged records their own way and hand them here, so the two producers
    emit byte-identical packages for identical content — a fingerprint from an accession loads and
    reproduces exactly as one from local FASTQs.
    """
    digest = _package_digest(pins, reads)
    stem = readable(slug, digest)
    staging = fingerprint_dir(workspace) / stem
    if staging.exists():
        import shutil

        shutil.rmtree(staging)  # idempotent rebuild: same inputs -> same stem, replace cleanly
    staging.mkdir(parents=True, exist_ok=True)

    for pkg_rel, records in staged_fastq:
        dest = staging / pkg_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_records_gz(dest, records)

    info_rels = extract_info([Path(d) for d in (info_docs or [])], staging, include_raw=include_raw)

    manifest = FingerprintManifest(
        fingerprint_version=FINGERPRINT_VERSION,
        probe_version=PROBE_VERSION,
        reads=reads,
        max_bytes=max_bytes,
        files=pins,
        info=info_rels,
    )
    (staging / "fingerprint.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )

    package = fingerprint_dir(workspace) / f"{stem}.fingerprint.tar.gz"
    write_tar_gz(staging, package)
    return FingerprintResult(package=package, staging=staging, manifest=manifest)


#: The info subtrees a redistributable package may not carry: the raw paper (copyright) and its
#: extracted figures (the figure pipeline is not good enough yet). ``info/text/`` is what harvest needs.
_NON_REDISTRIBUTABLE = ("info/docs/", "info/images/")


def strip_to_redistributable(package: str | Path, dest: str | Path) -> FingerprintResult:
    """Repack an existing fingerprint package as a **redistributable** (text-only) copy at ``dest``.

    The retroactive twin of ``build_fingerprint(..., include_raw=False)``: for packages built before
    the flag existed (or when the original FASTQs are long gone), this reads only the byte-light package
    and drops ``info/docs/`` (the raw paper) and ``info/images/`` (its figures), keeping ``info/text/``,
    the FASTQ slices, and the pin. The reads and pins are **untouched**, so the dataset hash reproduces
    byte-for-byte — only the info manifest and tree shrink. The package's content-address (its stem) is
    computed from pins + read budget and never included the docs, so the redistributable copy keeps the
    same identity; a run falls back to ``info/text/`` (:meth:`LoadedFingerprint.info_paths`).
    """
    import shutil
    import tempfile

    from .load import load_fingerprint

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="seqforge-strip-") as tmp:
        loaded = load_fingerprint(package, unpack_to=Path(tmp) / "unpacked")
        root = loaded.root
        kept = [rel for rel in loaded.manifest.info if not rel.startswith(_NON_REDISTRIBUTABLE)]
        for sub in ("docs", "images"):
            shutil.rmtree(root / "info" / sub, ignore_errors=True)
        manifest = loaded.manifest.model_copy(update={"info": kept})
        (root / "fingerprint.json").write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
        write_tar_gz(root, dest)
    return FingerprintResult(package=dest, staging=Path(dest), manifest=manifest)


__all__ = [
    "FingerprintResult",
    "assemble_package",
    "build_fingerprint",
    "strip_to_redistributable",
]
