"""Barcode-whitelist (onlist) registry, width-generic packing, and the hit-rate test.

The registry maps a *name* (``3M-february-2018``, ``737K-august-2016``, …) to a
:class:`RegistryEntry` (``uri``, ``sha256``, barcode ``width``, ``orientation``). A real list is
fetched via ``pooch`` and sha256-verified — **never vendored** (they are large and, for 10x,
license-restricted). For the pilot a *synthetic* list is registered in-memory from the generator's
barcode pool, so the resolver's rung-3 onlist evidence is exercised without touching the real files.

Barcodes are 2-bit packed into a width-generic integer array (``uint32`` for <=16 bp, ``uint64`` for
<=32 bp — never a hardcoded 16 bp), which gives O(1) membership for :func:`onlist_hit_rate` and
``np.intersect1d`` set-intersection for the confusability check (§2.4). ``onlist_hit_rate`` tests the
window **forward and reverse-complement** across a **small positional-offset scan** and records the
winning ``(orientation, offset)`` — a revcomp hit means the barcode read is on the other strand.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

Orientation = Literal["forward", "revcomp", "either"]
Strand = Literal["forward", "revcomp"]

_BASE_TO_BITS = {"A": 0, "C": 1, "G": 2, "T": 3}
_COMPLEMENT = str.maketrans("ACGT", "TGCA")


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
        #: sorted, unique packed codes (dtype uint32 for <=16 bp, uint64 for <=32 bp)
        self.codes = codes
        self._members: frozenset[int] = frozenset(int(c) for c in codes.tolist())

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
        """Membership test for an already-packed barcode code."""
        return code in self._members


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
    for strand in strands:
        for delta in range(-offset_scan, offset_scan + 1):
            s = start + delta
            if s < 0:
                continue
            e = s + width
            hits = 0
            tested = 0
            for seq in sample:
                if len(seq) < e:
                    continue
                window = seq[s:e]
                if strand == "revcomp":
                    window = revcomp(window)
                tested += 1
                code = pack_barcode(window)
                if code is not None and onlist.contains(code):
                    hits += 1
            if tested == 0:
                continue
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


@dataclass(frozen=True)
class RegistryEntry:
    """A registry record for one onlist. ``uri`` is a URL / relative path / ``synthetic:<name>``."""

    name: str
    uri: str
    sha256: str
    width: int
    orientation: Orientation = "forward"
    n_entries: int | None = None
    fetchable: bool = True


class OnlistNotAvailable(RuntimeError):
    """Raised when an onlist cannot be materialized (unknown, or offline + not cached)."""


class OnlistRegistry:
    """Named onlists -> packed whitelists, via pooch (real) or in-memory (synthetic)."""

    def __init__(self, *, cache_dir: str | Path | None = None, offline: bool = False) -> None:
        self.offline = offline
        self.cache_dir = str(cache_dir) if cache_dir is not None else None
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
            fetchable=False,
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
        """Return the packed whitelist for ``name``, fetching + verifying a real list if needed."""
        if name in self._packed:
            return self._packed[name]
        entry = self.get(name)
        if name in self._synthetic:
            packed = PackedOnlist.from_barcodes(self._synthetic[name])
        else:
            packed = PackedOnlist.from_barcodes(self._load_barcodes(entry))
        self._packed[name] = packed
        return packed

    def _load_barcodes(self, entry: RegistryEntry) -> list[str]:
        if self.offline:
            raise OnlistNotAvailable(f"onlist {entry.name!r} is not cached and --offline is set")
        path = self._fetch(entry)
        return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]

    def _fetch(self, entry: RegistryEntry) -> str:
        """Fetch + sha256-verify a real onlist via pooch (the only network touch here)."""
        import pooch  # local import: keep the module importable offline / without pooch resolved

        return str(
            pooch.retrieve(
                url=entry.uri,
                known_hash=f"sha256:{entry.sha256}",
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


#: Real onlists we *declare* (name/width/orientation) but do not vendor. The pilot never fetches
#: these (license-restricted, multi-GB); the resolver ABSTAINs on any onlist not materialized.
_KNOWN: list[RegistryEntry] = [
    RegistryEntry(
        name="3M-february-2018", uri="", sha256="", width=16, n_entries=6_794_880, fetchable=False
    ),
    RegistryEntry(
        name="737K-august-2016", uri="", sha256="", width=16, n_entries=737_280, fetchable=False
    ),
    RegistryEntry(
        name="737K-arc-v1", uri="", sha256="", width=16, n_entries=736_320, fetchable=False
    ),
]


def default_registry(*, offline: bool = True) -> OnlistRegistry:
    """A registry pre-declaring the known real onlists (not materialized in the pilot)."""
    reg = OnlistRegistry(offline=offline)
    for entry in _KNOWN:
        reg.register(entry)
    return reg


#: A shared default registry (real onlists declared, none materialized) for the CLI's offline path.
DEFAULT_REGISTRY: OnlistRegistry = default_registry()
