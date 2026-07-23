"""Tests for ``io``: width-generic packing, the onlist hit-rate scan, and the registry."""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

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


def _naive_hit_rate(seqs, start, onlist, orientation, offset_scan=2):
    """The pre-vectorization loop, kept as an executable oracle for the numpy rewrite."""
    from seqforge.io.onlist import HitResult

    width = onlist.width
    strands = (
        ["forward"]
        if orientation == "forward"
        else ["revcomp"]
        if orientation == "revcomp"
        else ["forward", "revcomp"]
    )
    best = HitResult(hit_rate=0.0, orientation="forward", offset=0, n_tested=0, floor=onlist.floor)
    for strand in strands:
        for delta in range(-offset_scan, offset_scan + 1):
            s = start + delta
            if s < 0:
                continue
            e = s + width
            hits = tested = 0
            for seq in seqs:
                if len(seq) < e:
                    continue
                window = revcomp(seq[s:e]) if strand == "revcomp" else seq[s:e]
                tested += 1
                code = pack_barcode(window)
                if code is not None and onlist.contains(code):
                    hits += 1
            if tested and hits / tested > best.hit_rate:
                best = HitResult(
                    hit_rate=hits / tested,
                    orientation=strand,
                    offset=delta,
                    n_tested=tested,
                    floor=onlist.floor,
                )
    return best


def test_vectorized_hit_rate_matches_the_naive_loop_including_edges() -> None:
    """The numpy rewrite must agree with the read-by-read loop it replaced, byte for byte.

    Covers the cases that make packing subtle: N bases (unpackable, counted in `tested` but never a
    hit), reads shorter than the window, non-zero anchors + offsets, revcomp, and an empty sample.
    """
    rng = random.Random(11)
    pool = _pool(rng, 300, 16)
    onlist = PackedOnlist.from_barcodes(pool)

    def rand_read() -> str:
        prefix = "".join(rng.choice("ACGT") for _ in range(rng.choice([0, 1, 2])))
        core = (
            rng.choice(pool)
            if rng.random() < 0.5
            else "".join(rng.choice("ACGTN") for _ in range(16))
        )
        return prefix + core + "".join(rng.choice("ACGT") for _ in range(rng.choice([0, 3, 20])))

    for _ in range(30):
        seqs = [rand_read() for _ in range(rng.choice([0, 1, 40, 300]))]
        for orientation in ("forward", "revcomp", "either"):
            for start in (0, 1, 2):
                got = onlist_hit_rate(seqs, start, onlist, orientation=orientation)
                want = _naive_hit_rate(seqs, start, onlist, orientation)
                assert got.hit_rate == pytest.approx(want.hit_rate)
                assert (got.n_tested, got.orientation, got.offset) == (
                    want.n_tested,
                    want.orientation,
                    want.offset,
                )


def test_packed_onlist_keeps_no_python_set() -> None:
    """Regression: membership is `searchsorted` on the sorted array, not a 6.8M-entry `frozenset`.

    That set was ~700 MB — the resolver's whole memory ceiling — and it duplicated information the
    sorted `codes` array already holds. If someone reintroduces it, this fails.
    """
    onlist = PackedOnlist.from_barcodes(_pool(random.Random(5), 128, 16))
    assert not hasattr(onlist, "_members")
    assert onlist.codes.tolist() == sorted(onlist.codes.tolist())  # sorted -> searchsorted is valid


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


# ---------- the registry authenticates BARCODES, not packaging ----------
def _gz(path: Path, text: str, *, mtime: int) -> None:
    """Write `text` gzipped with a chosen mtime, so two files differ in header but not content."""
    import gzip

    with gzip.GzipFile(filename=str(path), mode="wb", mtime=mtime) as fh:
        fh.write(text.encode())


def test_the_registry_hashes_content_so_recompression_does_not_break_it(tmp_path: Path) -> None:
    """A `.gz` hash pins PACKAGING. The barcodes are what we mean, so the barcodes are what we hash.

    Measured on the real lists (2026-07-15): `3M-february-2018` is a 12 211 647-byte `.gz` from 10x's
    own Cell Ranger 7.2.0 and an 18 350 152-byte `.gz` from the scg_lib_structs mirror. Same 6 794 880
    barcodes; the two download hashes agree on nothing. A registry pinning the download would reject
    a mirror serving perfect data, while proving nothing about the barcodes either way.

    This reproduces that in miniature: same content, two gzip headers, two file hashes, one accepted
    onlist.
    """
    from seqforge.io.onlist import OnlistRegistry, PackedOnlist, RegistryEntry, codes_sha256

    barcodes = ["ACGTACGTACGTACGT", "TTTTAAAACCCCGGGG"]
    content = "\n".join(barcodes) + "\n"
    set_sha = codes_sha256(PackedOnlist.from_barcodes(barcodes).codes)

    a, b = tmp_path / "a" / "L.txt.gz", tmp_path / "b" / "L.txt.gz"
    for p, mtime in ((a, 1), (b, 999_999)):
        p.parent.mkdir()
        _gz(p, content, mtime=mtime)
    assert a.read_bytes() != b.read_bytes(), "the fixture must differ as FILES or it proves nothing"

    entry = RegistryEntry(name="L", uri="", sha256=set_sha, width=16, n_entries=2)
    for d in (a.parent, b.parent):
        reg = OnlistRegistry(offline=True, local_dir=d)
        reg.register(entry)
        assert reg.packed("L").n_entries == 2, f"{d} was rejected over its gzip header"


def test_the_code_set_hash_ignores_order_and_duplicates_but_not_membership() -> None:
    """A whitelist is a SET. Hashing the set is what makes every source comparable.

    A file hash pins packaging (10x's .gz and the mirror's .gz share no bytes and the same barcodes);
    a text hash pins byte order and line endings (`737K-arc-v1` really has no trailing newline). This
    pins the barcodes and nothing else -- so it answers the only question we ask of a whitelist.
    """
    from seqforge.io.onlist import PackedOnlist, codes_sha256

    a = ["ACGTACGTACGTACGT", "TTTTAAAACCCCGGGG"]
    shuffled = list(reversed(a))
    duped = a + [a[0]]
    different = ["ACGTACGTACGTACGT", "TTTTAAAACCCCGGGC"]

    h = lambda bcs: codes_sha256(PackedOnlist.from_barcodes(bcs).codes)  # noqa: E731
    assert h(a) == h(shuffled) == h(duped), "order and duplicates are not part of the SET"
    assert h(a) != h(different), "...but one changed barcode is"


def test_the_registry_refuses_a_whitelist_that_is_not_the_declared_one(tmp_path: Path) -> None:
    """A wrong whitelist does not error downstream. It silently produces a thin matrix.

    That is the same failure shape as an inverted strand (§5), so the check must be here, at the
    point where bytes become a whitelist, and it must refuse rather than warn.
    """
    from seqforge.io.onlist import OnlistNotAvailable, OnlistRegistry, RegistryEntry

    _gz(tmp_path / "L.txt.gz", "ACGTACGTACGTACGT\nGGGGCCCCAAAATTTT\n", mtime=1)
    reg = OnlistRegistry(offline=True, local_dir=tmp_path)
    reg.register(RegistryEntry(name="L", uri="", sha256="0" * 64, width=16, n_entries=2))
    with pytest.raises(OnlistNotAvailable, match="not the same barcodes"):
        reg.packed("L")


def test_a_local_dir_makes_offline_irrelevant_rather_than_fatal(tmp_path: Path) -> None:
    """Most compute nodes have no internet; a registry that can only fetch cannot work on one."""
    from seqforge.io.onlist import (
        OnlistNotAvailable,
        OnlistRegistry,
        PackedOnlist,
        RegistryEntry,
        codes_sha256,
    )

    content = "ACGTACGTACGTACGT\n"
    _gz(tmp_path / "L.txt.gz", content, mtime=1)
    entry = RegistryEntry(
        name="L",
        uri="https://example.invalid/L.txt.gz",
        sha256=codes_sha256(PackedOnlist.from_barcodes([content.strip()]).codes),
        width=16,
        n_entries=1,
    )

    reg = OnlistRegistry(offline=True, local_dir=tmp_path)
    reg.register(entry)
    assert reg.packed("L").n_entries == 1, "a present local copy must beat `offline`"

    bare = OnlistRegistry(offline=True)
    bare.register(entry)
    with pytest.raises(OnlistNotAvailable, match="onlist-dir"):
        bare.packed("L")  # ...and without one, the refusal names the way forward


def test_fetchable_is_derived_from_the_uri_and_cannot_disagree_with_it() -> None:
    """It was a hand-set field that no code branched on -- read only for display, and wrong.

    Every real entry declared `fetchable=False` while its true problem was an empty `uri`. A flag
    that describes behaviour without causing it is a comment with a bool's syntax.
    """
    from seqforge.io.onlist import RegistryEntry

    assert RegistryEntry(name="x", uri="https://h/x.gz", sha256="", width=16).fetchable
    assert not RegistryEntry(name="x", uri="", sha256="", width=16).fetchable
    assert not RegistryEntry(name="x", uri="synthetic:x", sha256="", width=16).fetchable


def test_the_shipped_onlist_index_matches_the_shipped_data() -> None:
    """`index.json` sits beside the blobs it describes -- so it is checked against THEM, not itself.

    This is the shape the repo keeps getting burned by (`required_config`, `decidable_by`): a table
    of facts about some data, maintained by hand, validated by a test that reads the same table. Here
    the index is generated by `io onlist pack` and this test DECODES every blob and compares. The
    index cannot claim a width, a count or a hash that the data disagrees with, and a blob with no
    entry -- or an entry with no blob -- is an error rather than a thing nobody notices.
    """
    import json

    from seqforge.io.onlist import (
        _INDEX,
        _ONLIST_DATA,
        _PACKED_SUFFIX,
        codes_sha256,
        decode_codes,
        shipped_entries,
    )

    index = json.loads(_INDEX.read_text())
    blobs = {p.name[: -len(_PACKED_SUFFIX)] for p in _ONLIST_DATA.glob(f"*{_PACKED_SUFFIX}")}
    assert set(index) == blobs, "index.json and the shipped blobs disagree about what exists"

    for entry in shipped_entries():
        codes = decode_codes(entry.packed_path.read_bytes(), entry.width)
        assert codes.size == entry.n_entries, f"{entry.name}: index count is not the data's count"
        assert codes_sha256(codes) == entry.sha256, f"{entry.name}: index hash is not the data's"
        assert (codes[:-1] < codes[1:]).all(), f"{entry.name}: codes are not sorted and unique"
        assert len(entry.source_sha256) == 64, (
            f"{entry.name}: no provenance for what it was packed from"
        )


def test_the_shipped_10x_whitelists_are_the_real_ones() -> None:
    """Pinned to numbers verified against 10x's OWN CellRanger 7.2.0 on 2026-07-15, not remembered.

    v3 is separable from 10x Multiome and GEM-X by whitelist ALONE -- all three share the 28 bp /
    16+12 geometry (§12) -- so if these barcodes are wrong, the resolver confidently decides the
    wrong chemistry and nothing downstream disagrees.

    The code-set hashes below were derived by packing three independent copies of each list (the
    scg_lib_structs mirror, the lab's copy, and CellRanger 7.2.0's own) and confirming all three
    produce the same set.
    """
    from seqforge.io.onlist import default_registry

    expected = {
        "3M-february-2018": (
            6_794_880,
            "53d8182fd8d4c705ff99b6a583c640e28f58b847ac5bc7b20bb8f4f11ebe50ee",
        ),
        "737K-august-2016": (
            737_280,
            "199f7ae76cc1341d54c8024fd9a11a256145f64c999d5ce04f144a9acd8e8b5c",
        ),
        "737K-arc-v1": (
            736_320,
            "e267adf2a1605adcd40fbb67800d0d19ab8c35170ace26fe1f0d3523766d2234",
        ),
    }
    reg = default_registry()
    for name, (n, sha) in expected.items():
        packed = reg.packed(name)  # no network, no --onlist-dir: it ships
        assert packed.n_entries == n, f"{name}: wrong barcode count"
        assert packed.width == 16
        from seqforge.io.onlist import codes_sha256

        assert codes_sha256(packed.codes) == sha, f"{name}: these are not the declared barcodes"


def test_the_shipped_whitelists_resolve_with_no_network_and_no_setup() -> None:
    """The point of vendoring: a 10x dataset composes out of the box.

    Every entry used to carry `uri=""`/`sha256=""`, so `compose` exited 3 for ANY real 10x dataset --
    the pilot could not resolve at all.
    """
    from seqforge.io import DEFAULT_REGISTRY

    assert DEFAULT_REGISTRY.offline, "the default must not reach the network by surprise"
    packed = DEFAULT_REGISTRY.packed("3M-february-2018")
    assert packed.n_entries == 6_794_880
    # ~0.16% chance hit rate for a random 16-mer: the 500:1 signal-to-noise §5 relies on
    assert 0.001 < packed.floor < 0.002


def test_a_packed_onlist_round_trips_through_the_shipped_encoding() -> None:
    """Delta-then-gzip must be exactly lossless: it IS the whitelist now, not a cache of one."""
    import numpy as np

    from seqforge.io.onlist import decode_codes, encode_codes

    codes = np.array(sorted({7, 11, 4_000_000_000, 2**32 - 1, 0}), dtype="<u4")
    assert (decode_codes(encode_codes(codes), 16) == codes).all()


def test_the_wheel_ships_the_data_the_package_cannot_work_without() -> None:
    """Build a wheel and look inside. Package data is not Python, so nobody notices it going missing.

    Each of these absent is a specific, silent-ish failure a unit test would never see, because the
    source tree always has them:

      - `io/onlists/*.codes.gz`  -> compose exits 3 on every real 10x dataset
      - `workflows/map/*.smk`    -> the emitted Snakefile includes a module that is not there
      - `kb/specs/*/spec.yaml`   -> the KB is empty and nothing resolves

    It also pins the packaging arrangement: `packages = ["src/seqforge"]` already carries them, and a
    `force-include` on top is a hard build error rather than a duplicate. Both directions are covered
    here -- the wheel builds, AND it has the files.
    """
    import subprocess
    import sys
    import zipfile

    root = Path(__file__).resolve().parents[1]
    if not (root / "pyproject.toml").is_file():  # pragma: no cover - installed, not a checkout
        pytest.skip("not running from a source checkout")
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", tmp],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:  # pragma: no cover - depends on the host toolchain
            if "No module named build" in proc.stderr:
                pytest.skip("python-build is not installed in this environment")
            pytest.fail(f"the wheel does not build:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
        wheels = list(Path(tmp).glob("*.whl"))
        assert len(wheels) == 1, f"expected one wheel, got {wheels}"
        names = zipfile.ZipFile(wheels[0]).namelist()

    assert any(n.endswith("io/onlists/index.json") for n in names)
    assert sum(n.endswith(".codes.gz") for n in names) >= 3, "the packed whitelists are missing"
    assert sum(n.endswith(".smk") for n in names) >= 2, "the workflow modules are missing"
    assert sum(n.endswith("spec.yaml") for n in names) >= 5, "the KB specs are missing"


# --------------------------------------------------------------------------------------------
# the HF benchmark fetch — URL construction offline, and failure -> a typed skip signal
#
# The actual pull is a networked-job concern (a public HF dataset, anonymous read, pooch-cached).
# What must hold with no network is that the URL we build is the public `resolve` endpoint and that
# a fetch failure is the typed exception the eval harness turns into a skip, never a raw crash.
# --------------------------------------------------------------------------------------------


def test_hf_package_url_is_the_public_resolve_endpoint() -> None:
    from seqforge.io import HF_BENCHMARK_REPO, hf_package_url

    url = hf_package_url("packages/GSE274290.fingerprint.tar.gz")
    assert url == (
        f"https://huggingface.co/datasets/{HF_BENCHMARK_REPO}/resolve/main/"
        "packages/GSE274290.fingerprint.tar.gz"
    )
    # A revision pins reproducibility; a leading slash on the path must not double up.
    assert hf_package_url("/p.tar.gz", revision="v1").endswith("/resolve/v1/p.tar.gz")


def test_a_fetch_failure_is_a_typed_unavailable_not_a_crash(monkeypatch, tmp_path: Path) -> None:
    """pooch raising (offline, 404, DNS) must surface as BenchmarkPackageUnavailable — i.e. a skip."""
    import pooch

    from seqforge.io import BenchmarkPackageUnavailable, fetch_benchmark_package

    def _boom(**kwargs):
        raise OSError("no network in CI")

    monkeypatch.setattr(pooch, "retrieve", _boom)
    with pytest.raises(BenchmarkPackageUnavailable, match="GSE274290"):
        fetch_benchmark_package("packages/GSE274290.fingerprint.tar.gz", cache_dir=tmp_path)
