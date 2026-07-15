"""Tests for ``io``: width-generic packing, the onlist hit-rate scan, and the registry."""

from __future__ import annotations

import random

import numpy as np
import pytest

from seqforge.io import (
    OnlistNotAvailable,
    OnlistRegistry,
    PackedOnlist,
    intersect_fraction,
    onlist_hit_rate,
    pack_barcode,
    revcomp,
)
from seqforge.io.onlist import _dtype_for_width


def _pool(rng: random.Random, n: int, width: int) -> list[str]:
    return ["".join(rng.choice("ACGT") for _ in range(width)) for _ in range(n)]


def test_revcomp_and_pack_roundtrip() -> None:
    assert revcomp("AACCGGTT") == "AACCGGTT"  # its own revcomp
    assert revcomp("ATGC") == "GCAT"
    assert pack_barcode("AAAA") == 0
    assert pack_barcode("AAAC") == 1  # C == bits 01 in the low position
    assert pack_barcode("ACGT") == 0b00_01_10_11
    assert pack_barcode("ACGN") is None  # N is unpackable -> never a hit


def test_dtype_is_width_generic_not_hardcoded_16() -> None:
    assert _dtype_for_width(8) is np.uint32
    assert _dtype_for_width(16) is np.uint32
    assert _dtype_for_width(17) is np.uint64  # SPLiT-seq-ish widths still pack (not capped at 16)
    assert _dtype_for_width(32) is np.uint64
    with pytest.raises(ValueError):
        _dtype_for_width(33)


def test_packed_onlist_membership_and_floor() -> None:
    codes = PackedOnlist.from_barcodes(["AAAAAAAA", "CCCCCCCC", "AAAAAAAA"])  # dup collapses
    assert codes.n_entries == 2
    assert codes.width == 8
    assert codes.contains(pack_barcode("AAAAAAAA"))  # type: ignore[arg-type]
    assert not codes.contains(pack_barcode("GGGGGGGG"))  # type: ignore[arg-type]
    assert codes.floor == pytest.approx(2 / 4**8)


def test_onlist_hit_rate_forward_and_revcomp() -> None:
    rng = random.Random(1)
    pool = _pool(rng, 64, 16)
    onlist = PackedOnlist.from_barcodes(pool)
    # reads whose [0,16) window is drawn from the pool -> high forward hit-rate
    fwd_reads = [rng.choice(pool) + "ACGT" * 5 for _ in range(500)]
    fwd = onlist_hit_rate(fwd_reads, 0, onlist, orientation="either")
    assert fwd.orientation == "forward" and fwd.offset == 0
    assert fwd.hit_rate > 0.95

    # the same reads reverse-complemented -> the revcomp branch recovers the hit
    rc_reads = [revcomp(r) for r in fwd_reads]  # barcode now at the tail; anchor there
    rc = onlist_hit_rate(rc_reads, len(rc_reads[0]) - 16, onlist, orientation="either")
    assert rc.orientation == "revcomp"
    assert rc.hit_rate > 0.95


def test_onlist_hit_rate_offset_scan_recovers_shift() -> None:
    rng = random.Random(2)
    pool = _pool(rng, 64, 12)
    onlist = PackedOnlist.from_barcodes(pool)
    # barcode is shifted right by 2 bp (a leading 2 bp artifact); anchor at 0, scan finds delta=+2
    reads = ["GG" + rng.choice(pool) + "T" * 10 for _ in range(400)]
    hit = onlist_hit_rate(reads, 0, onlist, orientation="forward", offset_scan=3)
    assert hit.offset == 2
    assert hit.hit_rate > 0.95


def test_onlist_hit_rate_random_reads_near_floor() -> None:
    rng = random.Random(3)
    onlist = PackedOnlist.from_barcodes(_pool(rng, 64, 16))
    random_reads = _pool(rng, 500, 20)
    hit = onlist_hit_rate(random_reads, 0, onlist, orientation="forward")
    assert hit.hit_rate < 0.05  # ~ floor: random barcodes essentially never hit


def test_intersect_fraction() -> None:
    a = PackedOnlist.from_barcodes(["AAAAAAAA", "CCCCCCCC", "GGGGGGGG"])
    b = PackedOnlist.from_barcodes(["CCCCCCCC", "GGGGGGGG", "TTTTTTTT"])
    assert intersect_fraction(a, b) == pytest.approx(2 / 3)
    # different widths cannot collide
    c = PackedOnlist.from_barcodes(["AAAA"])
    assert intersect_fraction(a, c) == 0.0


def test_registry_synthetic_and_offline_real() -> None:
    reg = OnlistRegistry(offline=True)
    reg.register_synthetic("mini", ["AAAAAAAA", "CCCCCCCC"])
    assert reg.has("mini")
    packed = reg.packed("mini")
    assert packed.n_entries == 2
    assert reg.get("mini").sha256  # a content hash was recorded
    # an unknown onlist raises, not returns empty
    with pytest.raises(OnlistNotAvailable):
        reg.packed("does-not-exist")
