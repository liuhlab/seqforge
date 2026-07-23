"""Consume a fingerprint package: rebuild the probe map so a run resolves as the full FASTQs would.

The mirror of :mod:`.build`. Each sliced FASTQ is probed exactly as a normal local file is — same
bounded sampler, same Tier-A pipeline — and then the *identity* the slice cannot carry (content
address, compressed size, ISIZE) is stamped back on from the pin. The result is a ``_probed`` map
keyed by slice path that drops straight into ``resolve.resolve_runs`` / ``resolve_dataset``: from the
slices the pipeline produces the same observations, the same verdict, and the same ``dataset_hash`` the
originals would, with no original byte present.

This is the same seam ``io.remote.probe_remote`` uses to fingerprint a URL — identity from the pin,
signals from a bounded read — pointed at a local slice instead of an HTTP range.
"""

from __future__ import annotations

import tarfile
from dataclasses import dataclass
from pathlib import Path

from ..models.fingerprint import FingerprintManifest
from ..models.observation import Observation
from ..probe import DEFAULT_MAX_BYTES, DEFAULT_MAX_READS, build_observation
from ..probe.streaming import sample_fastq_gz


@dataclass(frozen=True)
class LoadedFingerprint:
    """An unpacked (or in-place) fingerprint: where its files live and what the pin declares."""

    root: Path  # the directory holding fingerprint.json + fastq/ (+ info/)
    manifest: FingerprintManifest

    def fastq_paths(self) -> list[Path]:
        """The sliced FASTQ paths, in pin order — what a run passes to ``resolve``."""
        return [self.root / pin.rel_path for pin in self.manifest.files]

    def info_paths(self) -> list[Path]:
        """The documents ``harvest`` should read: the raw originals if present, else the extracted text.

        A **local** package carries the originals under ``info/docs/`` and a run reads those verbatim.
        A **redistributable** package (built with ``include_raw=False``) drops the raw paper for
        copyright and carries only ``info/text/*.txt``; here the run falls back to that text. ``harvest``
        handles a ``.txt`` through its plain-text branch and span-verifies against whatever it is
        handed, so the package stays usable either way — the text-fed assertions are *equivalent* to a
        raw-PDF run's (a different ``doc_sha256``, no PDF page offsets), not byte-identical.
        """
        docs = [self.root / rel for rel in self.manifest.info if rel.startswith("info/docs/")]
        if docs:
            return docs
        return [self.root / rel for rel in self.manifest.info if rel.startswith("info/text/")]


def load_fingerprint(
    source: str | Path, *, unpack_to: str | Path | None = None
) -> LoadedFingerprint:
    """Open a fingerprint package — a ``.tar.gz`` (unpacked) or an already-unpacked directory.

    A directory is read in place (no copy); a tarball is extracted under ``unpack_to`` (default: a
    sibling directory named after the archive, minus ``.fingerprint.tar.gz``). Either way the return
    points at the directory that holds ``fingerprint.json``.
    """
    src = Path(source)
    if src.is_dir():
        root = src
    else:
        base = src.name
        for suffix in (".fingerprint.tar.gz", ".tar.gz", ".tgz"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        dest = Path(unpack_to) if unpack_to is not None else src.parent / base
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(src, mode="r:*") as tar:
            _safe_extract(tar, dest)
        root = dest
    manifest = FingerprintManifest.model_validate_json((root / "fingerprint.json").read_text())
    return LoadedFingerprint(root=root, manifest=manifest)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract every member under ``dest``, refusing any path that escapes it (path-traversal guard).

    ``filter="data"`` is the modern tarfile safety filter (regular files/dirs only, no traversal, no
    device/link surprises) — a fingerprint holds nothing else — and it also silences the 3.14
    extract-without-filter deprecation. The explicit ``commonpath`` check stays as belt-and-suspenders.
    """
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not (target == dest or dest in target.parents):
            raise ValueError(f"fingerprint archive member escapes destination: {member.name!r}")
    tar.extractall(dest, filter="data")


def probed_from_fingerprint(
    loaded: LoadedFingerprint,
    *,
    max_reads: int = DEFAULT_MAX_READS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[list[Path], dict[str, tuple[Observation, list[str]]]]:
    """Probe every slice and stamp its pinned identity: ``(slice paths, _probed map)`` for ``resolve``.

    The map is keyed by ``str(slice path)``, exactly what ``resolve_dataset`` looks up. For a package
    cut at N ≥ the probe budget, each rebuilt observation is byte-identical to a full-file probe's
    (bar the local path), so the downstream manifest and hash reproduce; a lighter package resolves on
    fewer reads by design (the size/accuracy study).
    """
    pins = {pin.rel_path: pin for pin in loaded.manifest.files}
    paths: list[Path] = []
    probed: dict[str, tuple[Observation, list[str]]] = {}
    for rel, pin in pins.items():
        slice_path = loaded.root / rel
        sample = sample_fastq_gz(slice_path, max_reads=max_reads, max_bytes=max_bytes)
        obs, seqs = build_observation(
            sample,
            size_bytes=pin.size_bytes,  # pinned: the whole-file compressed size
            sha256=pin.sha256,  # pinned: the whole-file content-address
            basename=pin.basename,
            local_uri=str(slice_path),
            isize=pin.isize,  # pinned: the whole-file ISIZE trailer
            max_reads=max_reads,
            max_bytes=max_bytes,
        )
        paths.append(slice_path)
        probed[str(slice_path)] = (obs, seqs)
    return paths, probed


__all__ = ["LoadedFingerprint", "load_fingerprint", "probed_from_fingerprint"]
