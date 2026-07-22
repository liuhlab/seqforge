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

    info_rels = extract_info([Path(d) for d in (info_docs or [])], staging)

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


__all__ = ["FingerprintResult", "build_fingerprint"]
