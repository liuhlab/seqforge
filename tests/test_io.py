"""Tests for ``io``: width-generic packing, the onlist hit-rate scan, and the registry."""

from __future__ import annotations

import random
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

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
from seqforge.io.onlist import HitResult, Orientation, Strand, _dtype_for_width


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
    present, absent = pack_barcode("AAAAAAAA"), pack_barcode("GGGGGGGG")
    assert present is not None and absent is not None, "an ACGT-only barcode always packs"
    assert codes.contains(present)
    assert not codes.contains(absent)
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


def test_a_dark_cycle_costs_coverage_not_hit_rate() -> None:
    """A read the sequencer never called leaves the DENOMINATOR (#177).

    The shape is `evals/benchmark/GSE305031`: one dark cycle inside the 16 bp barcode window, in most
    of the reads at the head of the run. Barcodes are matched exactly, so every affected read is
    unmatchable — and under the old denominator that dragged the rate down in proportion to the dark
    fraction, turning a 91% library into a 9% one and refusing it as barcode-absent.

    Both halves are asserted, because only the pair pins the policy: the rate must be the rate among
    reads that COULD hit, and the loss must still be visible as `n_tested`. A change that silently
    dropped the coverage report would pass on the rate alone.
    """
    rng = random.Random(17)
    pool = _pool(rng, 64, 16)
    onlist = PackedOnlist.from_barcodes(pool)
    dark = 900  # of 1000 reads: N at 0-indexed position 1, exactly as the real run has
    reads = []
    for i in range(1000):
        cb = rng.choice(pool)
        if i < dark:
            cb = cb[0] + "N" + cb[2:]
        reads.append(cb + "ACGT" * 5)

    hit = onlist_hit_rate(reads, 0, onlist, orientation="forward")

    assert hit.hit_rate > 0.95, (
        "the reads whose cycle 2 fired are whitelist barcodes; the rate is theirs"
    )
    assert hit.n_tested == 1000 - dark, (
        "the dark reads are the lost coverage, and are reported as such"
    )
    # The old denominator's answer, spelled out so the regression is unmistakable: 100/1000.
    assert hit.hit_rate > 0.5 > (1000 - dark) / 1000


def test_onlist_hit_rate_random_reads_near_floor() -> None:
    rng = random.Random(3)
    onlist = PackedOnlist.from_barcodes(_pool(rng, 64, 16))
    random_reads = _pool(rng, 500, 20)
    hit = onlist_hit_rate(random_reads, 0, onlist, orientation="forward")
    assert hit.hit_rate < 0.05  # ~ floor: random barcodes essentially never hit


def _naive_hit_rate(
    seqs: list[str],
    start: int,
    onlist: PackedOnlist,
    orientation: Orientation,
    offset_scan: int = 2,
) -> HitResult:
    """The pre-vectorization loop, kept as an executable oracle for the numpy rewrite."""
    width = onlist.width
    strands: list[Strand] = (
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
                code = pack_barcode(window)
                if code is None:
                    continue  # a non-ACGT window cannot hit, so it is not tested
                tested += 1
                if onlist.contains(code):
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

    Covers the cases that make packing subtle: N bases (unpackable, so neither a hit nor a test),
    reads shorter than the window, non-zero anchors + offsets, revcomp, and an empty sample.
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

    for _ in range(
        12
    ):  # every listed edge (N bases, short reads, empty sample, revcomp) is hit early
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

    def h(bcs: list[str]) -> str:
        return codes_sha256(PackedOnlist.from_barcodes(bcs).codes)

    assert h(a) == h(shuffled) == h(duped), "order and duplicates are not part of the SET"
    assert h(a) != h(different), "...but one changed barcode is"


def test_the_registry_refuses_a_whitelist_that_is_not_the_declared_one(tmp_path: Path) -> None:
    """A wrong whitelist does not error downstream. It silently produces a thin matrix.

    That is the same failure shape as an inverted strand, so the check must be here, at the
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
        ORIENTATIONS,
        codes_sha256,
        decode_codes,
        shipped_entries,
    )

    index = json.loads(_INDEX.read_text())
    blobs = {p.name[: -len(_PACKED_SUFFIX)] for p in _ONLIST_DATA.glob(f"*{_PACKED_SUFFIX}")}
    assert set(index) == blobs, "index.json and the shipped blobs disagree about what exists"

    # Orientation is the one field the blob cannot settle -- packed codes carry no strand -- so it is
    # checked against the vocabulary instead, and off the raw file rather than off `shipped_entries`,
    # which now narrows it on the way through and would confirm itself.
    for name, meta in sorted(index.items()):
        assert meta.get("orientation", "forward") in ORIENTATIONS, (
            f"{name}: index.json claims an orientation nothing scans"
        )

    for entry in shipped_entries():
        assert entry.packed_path is not None, f"{entry.name}: a shipped entry with no blob path"
        codes = decode_codes(entry.packed_path.read_bytes(), entry.width)
        assert codes.size == entry.n_entries, f"{entry.name}: index count is not the data's count"
        assert codes_sha256(codes) == entry.sha256, f"{entry.name}: index hash is not the data's"
        assert (codes[:-1] < codes[1:]).all(), f"{entry.name}: codes are not sorted and unique"
        assert len(entry.source_sha256) == 64, (
            f"{entry.name}: no provenance for what it was packed from"
        )


def test_every_orientation_has_a_scan_plan() -> None:
    """A new orientation must say which strands it scans, or it `KeyError`s at the scan.

    Collected from `Orientation` itself, so a new member is covered *because it exists* rather than
    because someone remembered -- the same discipline `test_every_solo_feature_is_classified` uses
    for the other `Literal`-keyed map in this repo. mypy does not check a dict literal against its
    key type, so nothing else would say a value had been added to one and not the other.
    """
    from typing import get_args

    from seqforge.io.onlist import _STRANDS_SCANNED, Orientation

    assert set(_STRANDS_SCANNED) == set(get_args(Orientation))


def test_an_unknown_orientation_is_refused_before_it_can_reach_the_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`pack` is the only writer of `index.json`, so a value outside the vocabulary dies here.

    Exit 2 rather than 3: a value outside a closed vocabulary is a malformed invocation, not a
    Blocker. The package data is redirected at `tmp_path` so that "nothing was written" is something
    this test can actually assert, rather than infer from the exit code.
    """
    from typer.testing import CliRunner

    from seqforge.cli import app
    from seqforge.io import onlist as onlist_mod

    data = tmp_path / "onlists"
    monkeypatch.setattr(onlist_mod, "_ONLIST_DATA", data)
    monkeypatch.setattr(onlist_mod, "_INDEX", data / "index.json")
    text = tmp_path / "bc.txt"
    text.write_text("ACGTACGTACGTACGT\n")

    result = CliRunner().invoke(
        app,
        ["io", "onlist", "pack", str(text), "--name", "typo-list", "--orientation", "reverse"],
    )
    assert result.exit_code == 2, result.output
    message = " ".join(result.output.split())
    assert "'reverse'" in message, "the refusal must name the value it rejected"
    for legal in ("forward", "revcomp", "either"):
        assert legal in message, f"the refusal must name {legal} as a way forward"
    assert not data.exists(), "a refused orientation must reach neither a blob nor the index"


def test_an_orientation_outside_the_vocabulary_is_refused_when_the_index_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The verb is not the only way in -- `shipped_entries` believes whatever `index.json` says.

    `ValueError`, not `OnlistNotAvailable`: that one names actions a caller can take (fetch the list,
    point `--onlist-dir` at it) and none of them fix a corrupt vocabulary.
    """
    import json

    from seqforge.io import onlist as onlist_mod

    index = tmp_path / "index.json"
    index.write_text(
        json.dumps({"L": {"width": 16, "n_entries": 1, "sha256": "ab", "orientation": "reverse"}})
    )
    monkeypatch.setattr(onlist_mod, "_INDEX", index)
    with pytest.raises(ValueError, match="reverse"):
        onlist_mod.shipped_entries()


def test_the_shipped_10x_whitelists_are_the_real_ones() -> None:
    """Pinned to numbers verified against 10x's OWN CellRanger 7.2.0 on 2026-07-15, not remembered.

    v3 is separable from 10x Multiome and GEM-X by whitelist ALONE -- all three share the 28 bp /
    16+12 geometry -- so if these barcodes are wrong, the resolver confidently decides the
    wrong chemistry and nothing downstream disagrees.

    The code-set hashes below were derived by packing three independent copies of each list (the
    scg_lib_structs mirror, the lab's copy, and CellRanger 7.2.0's own) and confirming all three
    produce the same set. The offline-default and floor checks are folded in from the vendoring test,
    so the 6.8M list is decoded once here rather than twice across two registry instances.
    """
    from seqforge.io import DEFAULT_REGISTRY
    from seqforge.io.onlist import codes_sha256, default_registry

    # The point of vendoring: a 10x dataset composes out of the box, offline. Every entry once carried
    # `uri=""`/`sha256=""`, so `compose` exited 3 for ANY real 10x dataset -- the pilot could not
    # resolve at all. `.offline` is a bare attribute read, so this decodes nothing.
    assert DEFAULT_REGISTRY.offline, "the default must not reach the network by surprise"

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
    packed_by_name = {}
    for name, (n, sha) in expected.items():
        packed = reg.packed(name)  # no network, no --onlist-dir: it ships
        assert packed.n_entries == n, f"{name}: wrong barcode count"
        assert packed.width == 16
        assert codes_sha256(packed.codes) == sha, f"{name}: these are not the declared barcodes"
        packed_by_name[name] = packed

    # ~0.16% chance hit rate for a random 16-mer: the 500:1 signal-to-noise relies on it. Read off
    # the 3M list already decoded above rather than decoding the 6.8M barcodes a second time.
    assert 0.001 < packed_by_name["3M-february-2018"].floor < 0.002


def test_a_packed_onlist_round_trips_through_the_shipped_encoding() -> None:
    """Delta-then-gzip must be exactly lossless: it IS the whitelist now, not a cache of one."""
    import numpy as np

    from seqforge.io.onlist import decode_codes, encode_codes

    codes = np.array(sorted({7, 11, 4_000_000_000, 2**32 - 1, 0}), dtype="<u4")
    assert (decode_codes(encode_codes(codes), 16) == codes).all()


# The wheel-contents guarantee -- that the built wheel actually ships the onlists, the `.smk`
# modules, the KB specs and the report assets -- moved to the CI `build` job, where the wheel already
# exists (scripts/check_wheel_contents.py, `pixi run check-wheel`, #108). Building a second wheel
# under pytest cost ~1.9s in `default` and skipped entirely in the `test` env CI runs, so it billed
# the developer and protected nothing on the machine that mattered.


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


def test_a_fetch_failure_is_a_typed_unavailable_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A network failure (offline, DNS, a 5xx) surfaces as BenchmarkPackageUnavailable — a skip.

    Note what this test no longer claims. It used to say "offline, 404, DNS" in one breath, and that
    single bucket is exactly the defect below: a package nobody ever published is a *gap in the
    corpus*, not a network blip, and folding the two made a missing dataset look like bad weather.
    """
    import pooch

    from seqforge.io import (
        BenchmarkPackageAbsent,
        BenchmarkPackageUnavailable,
        fetch_benchmark_package,
    )

    def _boom(**kwargs: object) -> NoReturn:
        raise OSError("no network in CI")

    monkeypatch.setattr(pooch, "retrieve", _boom)
    with pytest.raises(BenchmarkPackageUnavailable, match="GSE274290") as caught:
        fetch_benchmark_package("packages/GSE274290.fingerprint.tar.gz", cache_dir=tmp_path)
    assert not isinstance(caught.value, BenchmarkPackageAbsent), (
        "no HTTP response came back at all, so nothing was learned about whether the package exists"
    )


def test_a_404_is_absent_and_a_5xx_is_merely_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The archive answering "no such file" is a different fact from the archive not answering.

    A 404 means the repo was reached and does not hold this package — it was never published, which
    is a gap in the corpus that the eval report has to say out loud. Everything else the wire can do
    (offline, DNS, a rate limit, a 503) says nothing about whether the package exists, so it stays
    the old skip. `BenchmarkPackageAbsent` SUBCLASSES `BenchmarkPackageUnavailable` on purpose: every
    existing `except` keeps catching both, and only a caller that wants the distinction pays for it.
    """
    import pooch
    import requests

    from seqforge.io import (
        BenchmarkPackageAbsent,
        BenchmarkPackageUnavailable,
        fetch_benchmark_package,
    )

    def _status(code: int) -> Callable[..., NoReturn]:
        def _raise(**kwargs: object) -> NoReturn:
            response = requests.Response()
            response.status_code = code
            response.url = "https://huggingface.co/x"
            raise requests.exceptions.HTTPError(f"{code} for url", response=response)

        return _raise

    monkeypatch.setattr(pooch, "retrieve", _status(404))
    with pytest.raises(BenchmarkPackageAbsent, match="GSE110823") as absent:
        fetch_benchmark_package("packages/GSE110823.fingerprint.tar.gz", cache_dir=tmp_path)
    assert isinstance(absent.value, BenchmarkPackageUnavailable), "still catchable as the old type"
    assert "never published" in str(absent.value), "the reason is on the exception, not inferred"

    for code in (429, 500, 503):
        monkeypatch.setattr(pooch, "retrieve", _status(code))
        with pytest.raises(BenchmarkPackageUnavailable) as caught:
            fetch_benchmark_package("packages/GSE110823.fingerprint.tar.gz", cache_dir=tmp_path)
        assert not isinstance(caught.value, BenchmarkPackageAbsent), (
            f"HTTP {code} is the archive being unwell, not the package being missing"
        )


def test_the_404_is_found_even_when_pooch_wraps_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The status is read off the exception CHAIN, not off the top-level exception only.

    pooch hands the requests error straight through today, but it downloads through a pluggable
    downloader and a future one may re-raise its own type `from` the original. Walking the chain
    costs nothing and means a wrapped 404 does not silently demote to "offline".
    """
    import pooch
    import requests

    from seqforge.io import BenchmarkPackageAbsent, fetch_benchmark_package

    def _wrapped(**kwargs: object) -> NoReturn:
        response = requests.Response()
        response.status_code = 404
        try:
            raise requests.exceptions.HTTPError("404 for url", response=response)
        except requests.exceptions.HTTPError as inner:
            raise RuntimeError("download failed") from inner

    monkeypatch.setattr(pooch, "retrieve", _wrapped)
    with pytest.raises(BenchmarkPackageAbsent):
        fetch_benchmark_package("packages/GSE110823.fingerprint.tar.gz", cache_dir=tmp_path)


# --------------------------------------------------------------------------------------------
# publishing a fingerprint package: the producer half of the same contract
#
# `preflight` builds a package and `fetch_benchmark_package` reads one; between them there used to
# be nothing but a maintainer's memory. These tests never upload — the real call is behind an
# injected API object, which is also what keeps the suite hermetic.
# --------------------------------------------------------------------------------------------


def _tiny_package(tmp_path: Path, *, name: str = "GSE110823") -> Path:
    """A real (tiny) fingerprint package on disk — built by the builder, not hand-assembled."""
    from conftest import write_fastq_gz
    from seqforge.fingerprint.build import build_fingerprint

    src = tmp_path / "src"
    src.mkdir()
    rng = random.Random(7)
    for read in ("R1", "R2"):
        path = src / f"sample_{read}.fastq.gz"
        write_fastq_gz(path, ["".join(rng.choice("ACGT") for _ in range(50)) for _ in range(40)])
    return build_fingerprint(
        sorted(src.glob("*.fastq.gz")), workspace=tmp_path / "ws", reads=20, name=name
    ).package


class _FakeCommit:
    commit_url = "https://huggingface.co/datasets/liuhlab/seqforge-benchmark/commit/deadbeef"


class _FakeApi:
    """Stands in for `HfApi`. Records the one call it is allowed to receive."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def upload_file(self, **kwargs: object) -> _FakeCommit:
        self.calls.append(kwargs)
        return _FakeCommit()


def test_a_dry_run_publish_resolves_the_destination_and_uploads_nothing(tmp_path: Path) -> None:
    """ "Where does this go, and under what name" must be answerable without spending a commit.

    The destination is DERIVED from the filename rather than typed, because the consuming side
    derives it too: an eval recipe's `hf:` key is written by hand, and a package uploaded under a
    name no recipe points at is a 404 that nobody discovers until the benchmark next runs. So the
    dry run reports the public URL the fetch will GET, which is the string those two must agree on.
    """
    from seqforge.io import hf_package_url, publish_benchmark_package

    package = _tiny_package(tmp_path)
    result = publish_benchmark_package(package, dry_run=True, api=_FakeApi())

    assert result.dry_run is True
    assert result.commit_url is None, "a dry run committed nothing, and says so"
    # The BUILD names its output `GSE110823-<digest12>.fingerprint.tar.gz`; the corpus is keyed by
    # dataset. A recipe pinned to the digest would 404 the moment the package was rebuilt.
    assert "-" not in Path(result.rel_path).name.removesuffix(".fingerprint.tar.gz")
    assert result.rel_path == "packages/GSE110823.fingerprint.tar.gz"
    assert result.url == hf_package_url(result.rel_path)
    # It read the pin, so the summary states what would enter the corpus rather than what was named.
    assert result.n_files == 2 and result.reads == 20
    assert result.size_bytes == package.stat().st_size


def test_publishing_uploads_exactly_the_path_the_fetch_side_reads(tmp_path: Path) -> None:
    """The upload goes through `HfApi().upload_file` — never the `hf` command line, which hangs.

    Pinned here because the failure mode of getting it wrong is not an error: shelling out to `hf
    upload` blocks forever with no output, which reads as a slow network rather than as a bug.

    The path is asserted against `hf_package_url`'s own output rather than a literal, so the two
    halves of the contract cannot drift apart while both tests stay green.
    """
    from seqforge.io import hf_package_url, publish_benchmark_package

    package = _tiny_package(tmp_path)
    api = _FakeApi()
    result = publish_benchmark_package(package, api=api, message="Publish it")

    assert len(api.calls) == 1
    call = api.calls[0]
    assert call["path_in_repo"] == "packages/GSE110823.fingerprint.tar.gz"
    assert call["repo_type"] == "dataset", "a dataset repo, not a model repo — the URL differs"
    assert call["repo_id"] == "liuhlab/seqforge-benchmark"
    assert call["revision"] == "main"
    assert call["commit_message"] == "Publish it"
    assert result.dry_run is False
    assert result.commit_url == _FakeCommit.commit_url
    assert result.url == hf_package_url(str(call["path_in_repo"]))


def test_publishing_refuses_anything_that_is_not_a_fingerprint_package(tmp_path: Path) -> None:
    """The pin is validated BEFORE a byte is sent, and a refusal uploads nothing.

    A corrupt package on the public repo is worse than a missing one: a missing one skips with a
    reason, and a corrupt one turns every future run of that case into a diagnosis. The check is
    cheap — the pin is read straight out of the tarball, without unpacking it anywhere.
    """
    from seqforge.io import publish_benchmark_package

    api = _FakeApi()
    junk = tmp_path / "notes.tar.gz"
    import tarfile

    (tmp_path / "notes.txt").write_text("not a package")
    with tarfile.open(junk, "w:gz") as tar:
        tar.add(tmp_path / "notes.txt", arcname="notes.txt")

    with pytest.raises(ValueError, match="not a fingerprint package"):
        publish_benchmark_package(junk, api=api)
    with pytest.raises(FileNotFoundError):
        publish_benchmark_package(tmp_path / "nope.tar.gz", api=api)
    assert api.calls == [], "a refusal must not have uploaded anything first"
