"""The fingerprint *pin*: the small side-file that lets a head-slice reproduce the full dataset.

A fingerprint package carries head-sampled FASTQs — real 4-line records, but only the first N of a
file that may have had 10^8. A slice has a *different* compressed size, a different gzip ISIZE, and so
a different bounded content-key than the file it came from: probed naively it would resolve to a
different ``FileIdentity.sha256`` and the manifest's ``dataset_hash`` would not match the full-FASTQ
run's. That defeats the whole point ("even the FASTQ is gone, the manifest still reproduces").

The fix is to separate the two things a probe produces. The *chemistry* evidence — geometry, whitelist
hit-rate, per-cycle composition — lives in the reads, and the slice carries enough of them (N ≥ the
probe budget) to reproduce it exactly. The *identity* — content-address and compressed size — lives in
the whole file, and the slice cannot recompute it, so we **pin** it here and stamp it back onto the
stand-in probe via ``probe.build_observation(sha256=…, size_bytes=…)``. This is exactly how
``io.remote.probe_remote`` stamps a hosted file's identity onto a bounded range-read prefix.

The pin is deliberately not ``Evidenced`` and never enters a manifest: it is provenance *of the
package*, not a claim about the biology. It records what a full-file probe measured, so a fingerprint
run can present the identical observation without the bytes being present.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .base import Sha256

#: CalVer stamp for the fingerprint format. Bumped when the package layout or pin schema changes in a
#: way that a fingerprint run must notice; folded into no manifest hash (the pin *carries* identity, it
#: is not part of it), only into the package's own provenance.
FINGERPRINT_VERSION = "2026.7.1"


class FilePin(BaseModel):
    """The pinned identity of one FASTQ, plus where its slice sits in the package tree.

    ``sha256`` and ``size_bytes`` are the whole-file values a full probe produced — the two fields the
    dataset hash is sensitive to that a slice cannot reproduce on its own. ``isize`` (the gzip ISIZE
    trailer) is pinned too so the reconstructed observation is byte-identical, not merely hash-equal:
    it only feeds the read-count estimate, which is a resources hint and not hashed, but carrying it
    costs nothing and keeps the stand-in probe a faithful copy. ``rel_path`` is the slice's path
    relative to the package root, and preserving the original directory tree is what makes the
    manifest's *relative* URI (``dataset_uris``' ``commonpath``) reproduce for free.
    """

    model_config = ConfigDict(frozen=True)

    #: Path of the slice within the package, e.g. ``fastq/SRX123/reads_1.fastq.gz``. Preserves the
    #: original tree so the manifest's relative URI reproduces.
    rel_path: str
    #: The original filename — part of the local content-key, and the manifest's ``basename``.
    basename: str
    #: The whole-file content-address a full probe assigned (a provider md5 mapping, or a bounded
    #: local key). Stamped onto the stand-in probe so the manifest file inventory matches.
    sha256: Sha256
    #: The original **compressed** file size in bytes. Hashed; the slice's own size differs.
    size_bytes: int = Field(gt=0)
    #: The original gzip ISIZE trailer (uncompressed size mod 2^32), or ``None`` if unreadable.
    isize: int | None = None
    #: How many complete records the slice actually holds. Must be ≥ the probe budget for the
    #: reconstructed observation to match a full-file probe (fewer reads = a different observation).
    reads_written: int = Field(ge=0)
    #: The full file's estimated total reads, as the original probe reported it — carried for the
    #: report/benchmark, not for the hash.
    estimated_total_reads: int | None = None


class FingerprintManifest(BaseModel):
    """``fingerprint.json`` — the pins for every sliced FASTQ plus the package's own provenance.

    This is the sufficient standalone input the package exists to be: given the slices and these pins,
    the pipeline reproduces the same ``manifest.yaml`` (including ``dataset_hash``) the full FASTQs
    would have. It is plumbing, not a manifest — nothing here is ``Evidenced`` and it never enters the
    dataset the hash covers.
    """

    model_config = ConfigDict(frozen=True)

    fingerprint_version: str = FINGERPRINT_VERSION
    #: The probe version whose budget the slices were cut to. A fingerprint run must probe with a
    #: budget ≤ ``reads``; a newer probe that reads *more* than the slice holds would see a truncated
    #: file and could resolve differently, so this records what the slice is sufficient for.
    probe_version: str
    #: The read budget (N) the slices were cut to — the knob. Each slice holds ``min(N, file reads)``.
    reads: int = Field(gt=0)
    #: The decompressed-byte safety cap the slicer honoured (mirrors the probe's ``--max-bytes``).
    max_bytes: int = Field(gt=0)
    #: One pin per FASTQ in the package.
    files: list[FilePin]
    #: Relative paths of the extracted information files (paper text/images, xlsx sheets) carried
    #: alongside the reads, if any. A FASTQ-only fingerprint leaves this empty.
    info: list[str] = Field(default_factory=list)


__all__ = ["FINGERPRINT_VERSION", "FilePin", "FingerprintManifest"]
