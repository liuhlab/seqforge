"""Barcode-whitelist (onlist) registry, width-generic packing, and the hit-rate test.

The registry maps a *name* (``3M-february-2018``, ``737K-august-2016``, …) to a
:class:`RegistryEntry` (``uri``, ``sha256``, barcode ``width``, ``orientation``). **The real lists
ship with the package, pre-packed**, so a 10x dataset resolves with no network and no setup; a list we
do not ship falls back to ``--onlist-dir`` and then to ``pooch``. A *synthetic* list can be registered
in-memory from the generator's barcode pool, so rung-3 evidence is exercised in tests without the real
files.

Shipping them is affordable because of what a barcode is: 2 bits per base, so 10x's 6 794 880-entry v3
list is a sorted ``uint32`` array — and *sorted* is what pays, since 6.8 M draws from 4^16 leave ~630
gaps, the deltas need ~10 bits, and gzip does the rest. **522 kB**, against 12 MB for 10x's own
``.txt.gz``. See :func:`encode_codes`.

Identity is the barcode **set**, never the file: :func:`codes_sha256`. See it for why the two obvious
alternatives are both wrong.

Barcodes are 2-bit packed into a width-generic integer array (``uint32`` for <=16 bp, ``uint64`` for
<=32 bp — never a hardcoded 16 bp), which gives O(1) membership for :func:`onlist_hit_rate` and
``np.intersect1d`` set-intersection for the confusability check (§2.4). ``onlist_hit_rate`` tests the
window **forward and reverse-complement** across a **small positional-offset scan** and records the
winning ``(orientation, offset)`` — a revcomp hit means the barcode read is on the other strand.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

Orientation = Literal["forward", "revcomp", "either"]
Strand = Literal["forward", "revcomp"]

_BASE_TO_BITS = {"A": 0, "C": 1, "G": 2, "T": 3}
_COMPLEMENT = str.maketrans("ACGT", "TGCA")

#: ``ord(base) -> 2-bit code`` for uppercase ACGT (matching ``_BASE_TO_BITS``); everything else — N,
#: lowercase, any non-ACGT, and pad — maps to 255, the "unpackable" sentinel. It is ``>= 4``, so any
#: window touching one drops out of the packable set exactly as ``pack_barcode`` returns ``None``.
_ORD_TO_BITS = np.full(256, 255, dtype=np.uint8)
for _base, _bits in _BASE_TO_BITS.items():
    _ORD_TO_BITS[ord(_base)] = _bits


def revcomp(seq: str) -> str:
    """Reverse-complement an ACGT string (non-ACGT chars pass through unchanged)."""
    return seq.translate(_COMPLEMENT)[::-1]


def pack_barcode(seq: str) -> int | None:
    """2-bit pack an ACGT barcode into an integer, or ``None`` if it contains a non-ACGT base."""
    code = 0
    for ch in seq:
        bits = _BASE_TO_BITS.get(ch)
        if bits is None:  # N or any non-ACGT base -> unpackable, never a whitelist hit
            return None
        code = (code << 2) | bits
    return code


def _dtype_for_width(width: int) -> type[np.unsignedinteger]:
    """Pick the narrowest unsigned integer that holds a ``2*width``-bit packed barcode."""
    if width <= 0:
        raise ValueError(f"barcode width must be positive, got {width}")
    if width <= 16:
        return np.uint32
    if width <= 32:
        return np.uint64
    raise ValueError(f"barcode width {width} > 32 bp exceeds the uint64 pack budget")


class PackedOnlist:
    """A width-generic, 2-bit-packed barcode whitelist: O(1) membership + set intersection."""

    def __init__(self, width: int, codes: np.ndarray) -> None:
        self.width = width
        #: sorted, unique packed codes (dtype uint32 for <=16 bp, uint64 for <=32 bp). This IS the
        #: membership index: it is sorted, so `np.searchsorted` answers containment in O(log n) with
        #: ~27 MB, and there is no reason to also materialize a 6.8M-entry Python `frozenset` (which
        #: cost ~700 MB and was the resolver's whole memory ceiling). Vectorized membership over a
        #: read sample is one `searchsorted` call — see `onlist_hit_rate`.
        self.codes = codes

    @classmethod
    def from_barcodes(cls, barcodes: list[str]) -> PackedOnlist:
        """Pack a list of equal-width ACGT barcodes into a sorted, de-duplicated array."""
        if not barcodes:
            raise ValueError("cannot build a PackedOnlist from an empty barcode list")
        widths = {len(b) for b in barcodes}
        if len(widths) != 1:
            raise ValueError(f"onlist barcodes are not uniform width: {sorted(widths)}")
        width = widths.pop()
        dtype = _dtype_for_width(width)
        packed: list[int] = []
        for b in barcodes:
            code = pack_barcode(b.upper())
            if code is None:
                raise ValueError(f"onlist barcode {b!r} contains a non-ACGT base")
            packed.append(code)
        arr = np.array(sorted(set(packed)), dtype=dtype)
        return cls(width, arr)

    @property
    def n_entries(self) -> int:
        """Number of distinct barcodes in the whitelist."""
        return int(self.codes.size)

    @property
    def floor(self) -> float:
        """Chance hit-rate: probability a uniform-random barcode lands in the whitelist."""
        return self.n_entries / (4.0**self.width)

    def contains(self, code: int) -> bool:
        """Membership test for an already-packed barcode code, by binary search on the sorted codes."""
        i = int(np.searchsorted(self.codes, code))
        return i < self.codes.size and int(self.codes[i]) == int(code)


def unpack_barcodes(packed: PackedOnlist) -> list[str]:
    """A packed whitelist -> the barcode text ``--soloCBwhitelist`` expects. Inverse of packing.

    Lives here, beside :func:`pack_barcode`, because it is the same fact read backwards. It used to
    live in ``compose``, which is how ``compose`` came to write a 111 MB file: the code that could
    produce the text was in the module that decided when to.
    """
    bases = "ACGT"
    width = int(packed.width)
    out: list[str] = []
    for code in packed.codes.tolist():
        chars = []
        c = int(code)
        for _ in range(width):
            chars.append(bases[c & 0b11])
            c >>= 2
        out.append("".join(reversed(chars)))
    return sorted(out)


def write_onlist_text(registry: OnlistRegistry, name: str, path: str | Path) -> int:
    """Materialize a whitelist as text at ``path``. Returns the number of barcodes written.

    **This is a build step, not a compile step**, and that distinction is the whole point of it
    existing. 10x's v3 whitelist is 6 794 880 barcodes = 111 MB of text, and ``compose`` used to
    write it into every run directory: one dataset compiled three ways cost a third of a gigabyte of
    identical bytes, sitting there permanently, for a file STAR reads once. Now a Snakemake rule
    builds it, STAR reads it, and ``temp()`` deletes it — which is exactly what ``temp()`` is for and
    what the shipped ``.smk`` now declares.

    Writes to a sibling temp file and renames, so a killed run leaves no half-written whitelist for
    the next one to read. A truncated whitelist does not fail loudly: STARsolo exits 0 and emits a
    matrix that merely looks like a thin dataset — the same failure shape as an inverted strand.
    """
    packed = registry.packed(name)
    barcodes = unpack_barcodes(packed)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".partial")
    tmp.write_text("\n".join(barcodes) + "\n")
    tmp.replace(target)
    return len(barcodes)


def intersect_fraction(a: PackedOnlist, b: PackedOnlist) -> float:
    """Fraction of the smaller whitelist that also appears in the larger (onlist_separable, §2.4).

    Computed by actual set-intersection over the packed arrays (not sha256 inequality — different
    file hashes prove the files differ, not that the barcode *sets* differ). Different-width lists
    cannot collide, so the intersection is 0 by construction.
    """
    if a.width != b.width:
        return 0.0
    inter = np.intersect1d(a.codes, b.codes, assume_unique=True)
    denom = min(a.n_entries, b.n_entries)
    return (int(inter.size) / denom) if denom else 0.0


@dataclass(frozen=True)
class HitResult:
    """The best onlist hit found over the orientation x offset scan (§3.1)."""

    hit_rate: float
    orientation: Strand
    offset: int
    n_tested: int
    floor: float

    def score(self, min_rate: float) -> float:
        """Normalized support score in ``[0, 1]``: ``clip((best - floor)/(min - floor), 0, 1)``."""
        span = min_rate - self.floor
        if span <= 0:
            return 1.0 if self.hit_rate >= min_rate else 0.0
        return max(0.0, min(1.0, (self.hit_rate - self.floor) / span))


def _encode_sample(sample: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """Sample reads -> a padded ``(n, maxlen)`` uint8 matrix of 2-bit base codes + a lengths vector.

    Built ONCE and reused across every ``(strand, offset)`` window — that reuse is the speedup. A
    window scan then becomes a column slice, a vectorized pack, and one ``searchsorted``, in place of
    ``strands * offsets * n`` Python ``pack_barcode`` calls. Non-ACGT bases and pad positions are 255,
    which is ``>= 4``, so any window touching one is unpackable exactly as ``pack_barcode`` is ``None``.
    """
    n = len(sample)
    lengths = np.fromiter((len(s) for s in sample), dtype=np.int64, count=n)
    maxlen = int(lengths.max()) if n else 0
    mat = np.full((n, maxlen), 255, dtype=np.uint8)
    for i, seq in enumerate(sample):
        if seq:
            raw = np.frombuffer(seq.encode("ascii", "replace"), dtype=np.uint8)
            mat[i, : raw.size] = _ORD_TO_BITS[raw]
    return mat, lengths


def _pack_window(mat: np.ndarray, s: int, width: int, *, rc: bool) -> tuple[np.ndarray, np.ndarray]:
    """Pack column window ``[s, s+width)`` into codes + an all-ACGT validity mask, both vectorized.

    Forward packing matches ``pack_barcode``: the first base is most significant (big-endian base-4).
    Reverse-complement is ``3 - base`` (A<->T, C<->G) on the reversed window — reverse-then-complement
    equals complement-then-reverse since the complement is per-base, and it is computed on the encoded
    matrix, never by re-reading strings. ``valid`` is taken from the forward window (reversal does not
    change which reads are all-ACGT), so an invalid row's garbage code is dropped by the caller.
    """
    win = mat[:, s : s + width].astype(np.uint64)
    valid = (win < 4).all(axis=1)
    if rc:
        win = np.uint64(3) - win[:, ::-1]
    weights = np.uint64(4) ** np.arange(width - 1, -1, -1, dtype=np.uint64)
    packed = (win * weights).sum(axis=1, dtype=np.uint64)
    return packed, valid


def onlist_hit_rate(
    seqs: list[str],
    start: int,
    onlist: PackedOnlist,
    *,
    orientation: Orientation = "either",
    offset_scan: int = 2,
    max_reads: int = 50_000,
) -> HitResult:
    """Best whitelist hit-rate for a barcode anchored at ``start`` (width taken from ``onlist``).

    Tests forward and/or reverse-complement (per ``orientation``) across offsets
    ``[-offset_scan, +offset_scan]`` and returns the winning ``(orientation, offset, hit_rate)``.
    Bounded by ``max_reads`` (R3): the sample is already head-limited, and this caps the work again.

    Vectorized: the sample is encoded once into a base-code matrix, and each window is a slice + a
    ``searchsorted`` against the onlist's sorted codes. ``n_tested`` counts reads long enough to hold
    the window (as before, including non-ACGT reads that cannot hit); ``hit_rate = hits / n_tested``.
    """
    width = onlist.width
    strands: list[Strand]
    if orientation == "forward":
        strands = ["forward"]
    elif orientation == "revcomp":
        strands = ["revcomp"]
    else:
        strands = ["forward", "revcomp"]

    sample = seqs[:max_reads]
    best = HitResult(hit_rate=0.0, orientation="forward", offset=0, n_tested=0, floor=onlist.floor)
    if not sample:
        return best

    mat, lengths = _encode_sample(sample)
    maxcol = mat.shape[1]
    wl = onlist.codes  # sorted, unique
    for strand in strands:
        for delta in range(-offset_scan, offset_scan + 1):
            s = start + delta
            e = s + width
            # Guard the column range: e > maxcol means no read is long enough (inrange would be empty
            # anyway), but slicing past the matrix would silently return a NARROWER window and pack the
            # wrong width. s < 0 is off the read's 5' end.
            if s < 0 or e > maxcol:
                continue
            inrange = lengths >= e
            tested = int(inrange.sum())
            if tested == 0:
                continue
            packed, valid = _pack_window(mat[inrange], s, width, rc=strand == "revcomp")
            packable = packed[valid]
            if packable.size:
                idx = np.clip(np.searchsorted(wl, packable), 0, wl.size - 1)
                hits = int((wl[idx] == packable).sum())
            else:
                hits = 0
            rate = hits / tested
            if rate > best.hit_rate:
                best = HitResult(
                    hit_rate=rate,
                    orientation=strand,
                    offset=delta,
                    n_tested=tested,
                    floor=onlist.floor,
                )
    return best


#: Where the shipped, pre-packed whitelists live (package data).
_ONLIST_DATA = Path(__file__).parent / "onlists"

#: Suffix of the shipped form: gzipped little-endian uint32 **deltas** of the sorted code array.
_PACKED_SUFFIX = ".codes.gz"


def encode_codes(codes: np.ndarray) -> bytes:
    """Serialize a sorted, unique packed-code array to the shipped form.

    **Delta-then-gzip, and the numbers are why.** The v3 whitelist is 6 794 880 sorted draws from
    4^16, so consecutive codes differ by ~630 on average: the deltas need ~10 bits where the values
    need 32, and gzip finishes the job. Measured 2026-07-15:

        raw uint32   27.2 MB          .npy      27.2 MB
        .npz          9.7 MB          10x's .txt.gz  12.2 MB
        delta+gzip    0.5 MB   <- this

    45x smaller than the text it came from, so all three lists ship in well under a megabyte. A `.npy`
    would have been *bigger* than the file we were trying to avoid vendoring.
    """
    deltas = np.diff(codes, prepend=codes.dtype.type(0)).astype(codes.dtype.newbyteorder("<"))
    return gzip.compress(deltas.tobytes(), 6)


def decode_codes(blob: bytes, width: int) -> np.ndarray:
    """Inverse of :func:`encode_codes`: gunzip, then prefix-sum the deltas back to codes."""
    dtype = np.dtype(_dtype_for_width(width)).newbyteorder("<")
    deltas = np.frombuffer(gzip.decompress(blob), dtype=dtype)
    # dtype= pins the accumulator: numpy's default would widen to int64 and hand back a different
    # array than the one that was encoded.
    return np.cumsum(deltas, dtype=dtype)


def codes_sha256(codes: np.ndarray) -> str:
    """The canonical identity of a barcode SET: sha256 over sorted, unique, little-endian codes.

    This is a better identity than hashing the file, and the difference is not academic:

    - A `.gz` hash pins the **packaging**. The same 6 794 880 barcodes arrive as a 12 MB `.gz` from
      10x's Cell Ranger and an 18 MB `.gz` from the scg_lib_structs mirror, agreeing on no bytes.
    - A hash of the decompressed **text** pins the byte order and line endings. Real: `737K-arc-v1`
      has no trailing newline, so a well-meaning re-write that adds one changes the hash while
      changing no barcode.
    - This pins the **barcodes**, and nothing else. Order-independent, duplicate-independent,
      compression-independent, newline-independent. It answers exactly the question we ask of a
      whitelist: *is this the same set?*
    """
    return hashlib.sha256(
        np.ascontiguousarray(codes, dtype=codes.dtype.newbyteorder("<")).tobytes()
    ).hexdigest()


@dataclass(frozen=True)
class RegistryEntry:
    """A registry record for one onlist.

    ``sha256`` is the **code-set hash** (:func:`codes_sha256`), not a file hash — see that function
    for why. ``uri`` records where the list came from and is a fallback source; a shipped list needs
    no network at all.
    """

    name: str
    uri: str
    #: :func:`codes_sha256` of the barcode set. Empty = unverifiable (declared, not pinned).
    sha256: str
    width: int
    orientation: Orientation = "forward"
    n_entries: int | None = None
    #: Where the packed data lives, if it ships with the package. Set by :func:`shipped_entries`.
    packed_path: Path | None = None
    #: Provenance of the text this was packed from — recorded, never checked at run time (the packed
    #: form is sorted and de-duplicated, so the original byte order is gone and this hash cannot be
    #: recomputed from it). It is here so a human can re-derive the data and prove where it came from.
    source_sha256: str = ""

    @property
    def shipped(self) -> bool:
        return self.packed_path is not None and self.packed_path.is_file()

    @property
    def fetchable(self) -> bool:
        """Can this be materialized without a local copy? **Derived, never declared.**

        It was a hand-set field that no code branched on — written in three places, read only by
        `io onlist list` for display, and it said `fetchable=False` on entries whose real problem was
        an empty `uri`. A flag that describes behaviour without causing it is a comment with a bool's
        syntax. Now it cannot disagree with the registry.
        """
        return self.shipped or (bool(self.uri) and not self.uri.startswith("synthetic:"))


class OnlistNotAvailable(RuntimeError):
    """Raised when an onlist cannot be materialized (unknown, or offline + not cached)."""


class OnlistRegistry:
    """Named onlists -> packed whitelists, via pooch (real) or in-memory (synthetic)."""

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        offline: bool = False,
        local_dir: str | Path | None = None,
    ) -> None:
        self.offline = offline
        self.cache_dir = str(cache_dir) if cache_dir is not None else None
        #: A directory of already-downloaded lists, looked up as `<local_dir>/<name>.txt[.gz]`.
        #: The escape hatch for a compute node with no internet — which is most of them, and where a
        #: registry that can only fetch is a registry that cannot work. Checked BEFORE the network,
        #: so a present local copy makes `offline` irrelevant rather than fatal. It is still verified
        #: against the content hash, so "local" buys convenience, never trust.
        self.local_dir = str(local_dir) if local_dir is not None else None
        self._entries: dict[str, RegistryEntry] = {}
        self._synthetic: dict[str, list[str]] = {}
        self._packed: dict[str, PackedOnlist] = {}

    def register(self, entry: RegistryEntry) -> None:
        """Declare a (typically real, pooch-fetchable) onlist without materializing it."""
        self._entries[entry.name] = entry

    def register_synthetic(
        self, name: str, barcodes: list[str], *, orientation: Orientation = "forward"
    ) -> RegistryEntry:
        """Register an in-memory synthetic onlist from a barcode pool (the pilot fixture)."""
        if not barcodes:
            raise ValueError(f"synthetic onlist {name!r} needs at least one barcode")
        width = len(barcodes[0])
        text = "\n".join(barcodes) + "\n"
        sha = hashlib.sha256(text.encode()).hexdigest()
        entry = RegistryEntry(
            name=name,
            uri=f"synthetic:{name}",
            sha256=sha,
            width=width,
            orientation=orientation,
            n_entries=len(barcodes),
        )
        self._entries[name] = entry
        self._synthetic[name] = list(barcodes)
        self._packed.pop(name, None)
        return entry

    def has(self, name: str) -> bool:
        return name in self._entries

    def names(self) -> list[str]:
        return sorted(self._entries)

    def get(self, name: str) -> RegistryEntry:
        try:
            return self._entries[name]
        except KeyError as exc:
            raise OnlistNotAvailable(f"unknown onlist {name!r}") from exc

    def packed(self, name: str) -> PackedOnlist:
        """Return the packed whitelist for ``name``, materializing + verifying it if needed."""
        if name in self._packed:
            return self._packed[name]
        entry = self.get(name)
        if name in self._synthetic:
            packed = PackedOnlist.from_barcodes(self._synthetic[name])
        else:
            packed = PackedOnlist(entry.width, self._load_codes(entry))
        self._packed[name] = packed
        return packed

    def _local_path(self, entry: RegistryEntry) -> Path | None:
        """An already-downloaded copy under ``local_dir``, if there is one."""
        if not self.local_dir:
            return None
        for suffix in (".txt.gz", ".txt", ".gz", ""):
            candidate = Path(self.local_dir) / f"{entry.name}{suffix}"
            if candidate.is_file():
                return candidate
        return None

    def _load_codes(self, entry: RegistryEntry) -> np.ndarray:
        """Materialize an onlist's packed codes: shipped data, then a local copy, then the network.

        Shipped first because it is the common case and costs nothing — no network, no unpacking of a
        27 MB text file, no `--onlist-dir` for the user to know about.
        """
        if entry.shipped:
            assert entry.packed_path is not None
            codes = decode_codes(entry.packed_path.read_bytes(), entry.width)
            return self._verify(entry, codes, entry.packed_path)

        path = self._local_path(entry)
        if path is None:
            if self.offline:
                raise OnlistNotAvailable(
                    f"onlist {entry.name!r} is not shipped or cached, and this registry is offline. "
                    f"Point --onlist-dir at a directory containing {entry.name}.txt.gz, or use a "
                    f"registry that may fetch."
                )
            if not entry.uri:
                raise OnlistNotAvailable(
                    f"onlist {entry.name!r} is not shipped and has no source URL, so it cannot be "
                    f"fetched. Download it and point --onlist-dir at the directory holding "
                    f"{entry.name}.txt.gz."
                )
            path = Path(self._fetch(entry))
        return self._verify(entry, self._codes_from_text(path), path)

    def _verify(self, entry: RegistryEntry, codes: np.ndarray, source: Path) -> np.ndarray:
        """Refuse a whitelist that is not the declared barcode set.

        Refusing rather than warning, because a wrong whitelist does not error downstream: STARsolo
        exits 0 and emits a matrix that merely looks like a thin dataset — the same failure shape as
        an inverted strand (§5), and the reason eager verification exists at all.
        """
        digest = codes_sha256(codes)
        if entry.sha256 and digest != entry.sha256:
            raise OnlistNotAvailable(
                f"onlist {entry.name!r} from {source} is not the same barcodes the registry "
                f"declares: got code-set sha256 {digest} ({codes.size} barcodes), expected "
                f"{entry.sha256}" + (f" ({entry.n_entries} barcodes)" if entry.n_entries else "")
            )
        return codes

    def _codes_from_text(self, path: Path) -> np.ndarray:
        """Read a whitelist's text (gzipped or not) and pack it.

        The gzip magic decides, not the file extension: a mirror serving `.txt` that is actually
        gzipped (or the reverse) is a packaging detail, and guessing from the name is how you get a
        whitelist of one line that starts with `\\x1f\\x8b`.
        """
        raw = path.read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        barcodes = [line.strip() for line in raw.decode().splitlines() if line.strip()]
        if not barcodes:
            raise OnlistNotAvailable(f"{path} contains no barcodes")
        return PackedOnlist.from_barcodes(barcodes).codes

    def _fetch(self, entry: RegistryEntry) -> str:
        """Fetch a real onlist via pooch (the only network touch here).

        ``known_hash=None`` on purpose, and it is not a hole: `_barcodes_from` verifies the
        **decompressed** bytes against `entry.sha256` immediately after. Pooch can only hash what it
        downloaded, and a download hash pins the *packaging* — the same 6 794 880 barcodes arrive as a
        12 MB `.gz` from 10x and an 18 MB `.gz` from the scg_lib_structs mirror, agreeing on no bytes
        at all. Pinning that would break on a recompression while proving nothing about the barcodes.
        """
        import pooch  # local import: keep the module importable offline / without pooch resolved

        return str(
            pooch.retrieve(
                url=entry.uri,
                known_hash=None,
                path=self.cache_dir,
                progressbar=False,
            )
        )


def synthetic_registry(pools: dict[str, list[str]]) -> OnlistRegistry:
    """Build a registry whose registry-names are backed by the generator's synthetic pools.

    ``pools`` maps a *registry name* (e.g. ``3M-february-2018``) to its barcode list. Used by the
    resolve tests so rung-3 onlist evidence fires against the same barcodes the reads were drawn from.
    """
    reg = OnlistRegistry(offline=True)
    for name, barcodes in pools.items():
        reg.register_synthetic(name, barcodes)
    return reg


#: The shipped whitelists' index: `onlists/index.json`, **generated** by `seqforge io onlist pack`
#: alongside the `.codes.gz` blobs it describes.
#:
#: It is a file beside the data it describes, which is the shape this repo distrusts — so it is never
#: hand-edited, and `test_the_shipped_onlist_index_matches_the_shipped_data` decodes every blob and
#: checks the index against **what is actually in it**. The index cannot claim a width, a count or a
#: hash the data disagrees with, and a blob with no index entry (or an entry with no blob) is an error.
#: Adding a whitelist is: run the verb, commit both files. Nothing to remember.
_INDEX = _ONLIST_DATA / "index.json"


def shipped_entries() -> list[RegistryEntry]:
    """Every whitelist that ships with the package, discovered from `onlists/index.json`."""
    if not _INDEX.is_file():
        return []
    index = json.loads(_INDEX.read_text())
    out: list[RegistryEntry] = []
    for name, meta in sorted(index.items()):
        out.append(
            RegistryEntry(
                name=name,
                uri=str(meta.get("uri", "")),
                sha256=str(meta["sha256"]),
                width=int(meta["width"]),
                orientation=meta.get("orientation", "forward"),
                n_entries=int(meta["n_entries"]),
                packed_path=_ONLIST_DATA / f"{name}{_PACKED_SUFFIX}",
                source_sha256=str(meta.get("source_sha256", "")),
            )
        )
    return out


def write_shipped(
    name: str,
    codes: np.ndarray,
    *,
    width: int,
    uri: str = "",
    orientation: Orientation = "forward",
    source_sha256: str = "",
) -> Path:
    """Pack `codes` into the shipped form and record it in the index. Used by `io onlist pack`.

    The maintenance half of the "adding a whitelist is dropping a file in" promise: this is the only
    writer of `index.json`, so the index cannot drift from the blobs by hand.
    """
    _ONLIST_DATA.mkdir(parents=True, exist_ok=True)
    blob_path = _ONLIST_DATA / f"{name}{_PACKED_SUFFIX}"
    blob_path.write_bytes(encode_codes(codes))
    index = json.loads(_INDEX.read_text()) if _INDEX.is_file() else {}
    index[name] = {
        "width": int(width),
        "n_entries": int(codes.size),
        "sha256": codes_sha256(codes),
        "orientation": orientation,
        "uri": uri,
        "source_sha256": source_sha256,
    }
    _INDEX.write_text(json.dumps(dict(sorted(index.items())), indent=2, sort_keys=True) + "\n")
    return blob_path


def default_registry(
    *, offline: bool = True, local_dir: str | Path | None = None
) -> OnlistRegistry:
    """A registry carrying every shipped whitelist, ready to use with no network and no setup.

    ``offline`` still defaults True, and now costs almost nothing: the shipped lists need no network,
    so ``offline`` only governs the fallback for a list we do not ship. ``local_dir`` (or
    ``$SEQFORGE_ONLIST_DIR``) points at already-downloaded text copies and is checked before the
    network — the escape hatch for a whitelist that ships with neither us nor a public URL.
    """
    if local_dir is None:
        local_dir = os.environ.get("SEQFORGE_ONLIST_DIR") or None
    reg = OnlistRegistry(offline=offline, local_dir=local_dir)
    for entry in shipped_entries():
        reg.register(entry)
    return reg


#: A shared default registry for the CLI. The shipped whitelists resolve with no network and no
#: setup; anything else falls back to `--onlist-dir` / `$SEQFORGE_ONLIST_DIR`, and failing that the
#: resolver ABSTAINs rather than guessing and `compose` exits 3 rather than emitting a
#: `--soloCBwhitelist` that points at nothing.
DEFAULT_REGISTRY: OnlistRegistry = default_registry()
