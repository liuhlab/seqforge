"""Tests for the bounded Tier A probe on synthetic gzipped FASTQ fixtures."""

from __future__ import annotations

import ast
import builtins
import gc
import gzip
import hashlib
import json
import random
import re
import sys
from collections.abc import Callable
from io import BufferedReader, BytesIO
from pathlib import Path
from types import TracebackType

import pytest

from conftest import SrcTrees, write_fastq_gz
from seqforge.models.observation import ConstantSegment, HomopolymerSegment, RandomSegment
from seqforge.probe import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_READS,
    PROBE_VERSION,
    WholeFile,
    build_observation,
    local_whole_file,
    probe_file,
)
from seqforge.probe.core import _params_hash, gzip_isize
from seqforge.probe.streaming import BoundedReader, Budget, FastqHead

BASES = "ACGT"
W1_LINKER = (
    "GAGTGATTGCTTGTGACGCCTT"  # a fixed 22 bp adapter (inDrop's W1), used to test constant detection
)

#: sha256 over the canonically-serialized signal fields of ``_value_stable_fixture``'s observation.
#: Changing this literal is never a fix — it means a probe change moved an observed value, which
#: re-hashes every pinned manifest. Re-pin only alongside a deliberate PROBE_VERSION bump — last
#: moved by 2026.8.1, which added `read_length.mode_share` (this fixture is fixed-length, so it reads
#: 1.0 and no pre-existing value moved with it).
VALUE_STABLE_DIGEST = "93d3304f1cf317ecb1ffc2fc0c5133c3e054e43e08919ffece35f5651ad11d28"
#: 16 bp CB | 22 bp W1 linker | 8 bp UMI | 10 bp polyT, recovered structurally.
VALUE_STABLE_SEGMENTS = ["RandomSegment", "ConstantSegment", "RandomSegment", "HomopolymerSegment"]


def _rand_seq(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(BASES) for _ in range(n))


def _recs(seqs: list[str], name: str = "SIM") -> list[tuple[str, str, str]]:
    return [(f"{name}:{i}", s, "I" * len(s)) for i, s in enumerate(seqs)]


def test_10x_r1_geometry(tmp_path: Path) -> None:
    rng = random.Random(0)
    pool = [_rand_seq(rng, 16) for _ in range(50)]  # 50 recurring cell barcodes
    seqs = [rng.choice(pool) + _rand_seq(rng, 12) for _ in range(2000)]  # 16 CB + 12 UMI = 28 bp
    path = tmp_path / "r1.fastq.gz"
    write_fastq_gz(path, _recs(seqs))

    obs = probe_file(path)
    assert obs.read_length.mode == 28
    assert obs.read_length.n_distinct == 1
    assert obs.read_length.mode_share == 1.0  # every read is at the mode, and the profile says so
    assert obs.probe.n_reads_sampled == 2000
    assert obs.gzip.ok and not obs.gzip.truncated
    assert any(isinstance(s, RandomSegment) for s in obs.segments)
    assert re.fullmatch(r"[0-9a-f]{64}", obs.file.sha256)  # a well-formed content-address


def test_per_cycle_composition_matches_the_reference_loop_byte_for_byte() -> None:
    """The vectorized per-cycle composition (issue #66) must equal the plain per-base loop EXACTLY.

    It feeds the observation hash, so a one-ULP drift would silently re-address the corpus. Both compute
    integer counts and the same Python ``int / int`` fractions, so equality is exact (``==`` on floats,
    not ``approx``). Checked over a spread of shapes and every character class: N, lowercase, punctuation,
    ragged lengths, empty rows.
    """
    from seqforge.probe.signals import per_cycle_composition

    base_idx = {"A": 0, "C": 1, "G": 2, "T": 3}

    def reference(seqs: list[str]) -> list[tuple[int, float, float, float, float, float]]:
        if not seqs:
            return []
        max_len = max(len(s) for s in seqs)
        counts = [[0, 0, 0, 0, 0] for _ in range(max_len)]
        denom = [0] * max_len
        for s in seqs:
            for i, ch in enumerate(s):
                counts[i][base_idx.get(ch, 4)] += 1
                denom[i] += 1
        return [
            (i, c[0] / d, c[1] / d, c[2] / d, c[3] / d, c[4] / d)
            for i, (c, d) in enumerate((counts[i], denom[i] or 1) for i in range(max_len))
        ]

    def assert_identical(seqs: list[str]) -> None:
        got = per_cycle_composition(seqs)
        ref = reference(seqs)
        assert len(got) == len(ref)
        for cc, (cycle, a, c, g, t, n) in zip(got, ref, strict=True):
            assert (cc.cycle, cc.a, cc.c, cc.g, cc.t, cc.n) == (cycle, a, c, g, t, n)

    assert_identical([])  # empty input
    assert_identical([""])  # a single empty read -> no cycles
    assert_identical(["ACGT"])  # one read
    assert_identical(["AAAA", "AAAA"])  # homopolymer
    assert_identical(["ACGTN", "NNNNN"])  # N bases
    assert_identical(["acgtn", "ACGTN"])  # lowercase / non-ACGT -> N bucket
    assert_identical(["ACGT", "AC", "A", ""])  # ragged, including an empty row
    assert_identical(["A.C-G", "N?xY"])  # punctuation / IUPAC codes -> N bucket

    rng = random.Random(0)
    alphabets = ["ACGT", "ACGTN", "ACGTNacgtn.-"]
    for _ in range(200):
        alpha = rng.choice(alphabets)
        seqs = [
            "".join(rng.choice(alpha) for _ in range(rng.randint(0, 30)))
            for _ in range(rng.randint(1, 40))
        ]
        assert_identical(seqs)


@pytest.mark.parametrize("at_mode, n", [(2000, 2000), (1999, 2000), (1200, 2000), (1001, 2000)])
def test_the_read_length_profile_reports_the_share_of_reads_at_the_mode(
    at_mode: int, n: int
) -> None:
    """`n_distinct` counts which lengths are present and never how the reads divide among them.

    One read of 2 000 a base short and a file where two reads in five were trimmed both report
    `n_distinct == 2`, so a gate reading only that number cannot tell a ragged tail from a library
    that moved — which is exactly what `resolve`'s pre-trimming refusal could not do until #190.
    `mode_share` is the quantity it needed. Its denominator is every sampled read, because a read
    cannot fail to reach its own length: unlike a window statistic, nothing drops out of it, which is
    why the profile carries the share and not a denominator of its own.
    """
    from seqforge.probe.signals import read_length_profile

    profile = read_length_profile(["A" * 28 if i < at_mode else "A" * 27 for i in range(n)])

    assert profile.mode == 28
    assert profile.n_distinct == (1 if at_mode == n else 2)
    assert profile.mode_share == pytest.approx(at_mode / n)


def test_a_head_with_no_reads_has_no_share_at_its_mode() -> None:
    """0.0, not a vacuous 1.0 — the answer `HeadCoverage` already gives for an empty head.

    Nothing was observed, so nothing sits at the mode. It is unreachable from the gate that reads it
    (a file with no reads has mode 0, which fills no role), and it stays honest rather than
    convenient in case that ever stops being true.
    """
    from seqforge.probe.signals import read_length_profile

    profile = read_length_profile([])

    assert profile.mode == 0
    assert profile.mode_share == 0.0


def test_linker_and_polyt_segmentation(tmp_path: Path) -> None:
    rng = random.Random(1)
    seqs = [_rand_seq(rng, 8) + W1_LINKER + "T" * 10 for _ in range(500)]  # 8 random + W1 + polyT
    path = tmp_path / "indrop.fastq.gz"
    write_fastq_gz(path, _recs(seqs))

    segs = probe_file(path).segments
    randoms = [s for s in segs if isinstance(s, RandomSegment)]
    constants = [s for s in segs if isinstance(s, ConstantSegment)]
    homos = [s for s in segs if isinstance(s, HomopolymerSegment)]

    assert randoms and randoms[0].start == 0  # variable barcode is a random span at the read start
    assert any(s.consensus.startswith("GAGTGATT") for s in constants)  # the W1 linker
    assert any(s.base == "T" and s.end == 40 for s in homos)  # the polyT tail runs to the read end


def test_distinct_ratio_low_for_recurring_barcode(tmp_path: Path) -> None:
    rng = random.Random(2)
    pool = [_rand_seq(rng, 16) for _ in range(40)]
    seqs = [rng.choice(pool) for _ in range(2000)]  # 16 bp, no UMI: barcodes recur heavily
    path = tmp_path / "cb.fastq.gz"
    write_fastq_gz(path, _recs(seqs))

    windows = probe_file(path).distinct_value_windows
    assert windows, "a random 16 bp segment should yield a distinct-ratio window"
    assert min(w.distinct_ratio for w in windows) < 0.1  # cell-barcode recurrence, not UMI


def test_sra_normalized_header_detected(tmp_path: Path) -> None:
    rng = random.Random(4)
    recs = [(f"SRR9999999.{i} {i} length=28", _rand_seq(rng, 28), "I" * 28) for i in range(1, 51)]
    path = tmp_path / "sra.fastq.gz"
    write_fastq_gz(path, recs)

    obs = probe_file(path)
    assert obs.read_name.sra_normalized is True
    assert obs.read_name.parsed is False


def _write_enormous_fastq_gz(path: Path, *, chunk_mb: int = 1, n_chunks: int = 128) -> int:
    """A FASTQ whose DECOMPRESSED stream dwarfs any budget, written in a fraction of a second.

    Highly repetitive reads compress ~300:1, so ~130 MB of decompressed FASTQ costs ~450 KB on disk
    and a quarter-second to build. That is the trick that makes the bounded-read claim testable at all: the rule
    is about a 50 GB file, and the thing under test is *bytes_read*, which must not care how big the
    file is. Returns the decompressed size in bytes.
    """
    rec = b"@SIM:1\n" + b"ACGT" * 7 + b"\n+\n" + b"I" * 28 + b"\n"
    per_chunk = (chunk_mb * 1_000_000) // len(rec)
    chunk = rec * per_chunk
    with gzip.open(path, "wb", compresslevel=6) as fh:
        for _ in range(n_chunks):
            fh.write(chunk)
    return len(chunk) * n_chunks


#: ``(path, decompressed_bytes)`` — the enormous fixture and how big it really is.
Enormous = tuple[Path, int]


@pytest.fixture(scope="module")
def enormous_fastq(tmp_path_factory: pytest.TempPathFactory) -> Enormous:
    """The 128 MB-decompressed fixture, written ONCE. Read-only: no test may write to this path.

    Three tests rebuilt an identical file. Writing it costs 0.448s; the probe work it enables costs
    0.011s / 0.072s / 0.011s — so almost all of it was the same 0.45s paid three times.

    **It must be requested as a parameter, never called from a test body**, and that is not style.
    `test_the_content_address_never_scans_the_whole_file` monkeypatches `builtins.open` and counts
    every byte read from this exact path; building the file while the counting `open` is installed
    would score 450 KB of *writing* as *reading* and fail the budget assertion. A fixture is built
    during setup, before the test body installs anything.

    Module scope, not a cached function: a shape that skipped a rebuild inconsistently would leave
    `assert on_disk < 2_000_000` and `assert on_disk > compressed_bytes_read * 3` measuring a file
    nobody wrote this run — the read budget's strongest scale test, passing against a stale artifact.
    """
    path = tmp_path_factory.mktemp("enormous") / "enormous.fastq.gz"
    return path, _write_enormous_fastq_gz(path)


@pytest.mark.xdist_group("enormous-fastq")
def test_the_read_budget_bounds_bytes_read_however_large_the_file(enormous_fastq: Enormous) -> None:
    """A code path that CAN stream a whole multi-GB FASTQ is a bug — asserted, not asserted-to.

    The bounded-read rule cited a "50 GB reads < N bytes" check that was never written; what existed proved the budget
    bit on a 5 000-read fixture, which is a scale at which nothing could go wrong. This is the
    property that actually matters: `bytes_read` is a function of the BUDGET, not of the file. A
    regression that streamed to EOF would pass every small-fixture test in this file and fail here.
    """
    path, decompressed = enormous_fastq
    on_disk = path.stat().st_size
    assert decompressed > 100_000_000  # the fixture really is enormous once decompressed...
    assert on_disk < 2_000_000  # ...while costing the test suite ~450 KB and ~0.2 s

    obs = probe_file(path)  # DEFAULT budgets: DEFAULT_MAX_READS reads / 256 MB

    assert obs.probe.n_reads_sampled == DEFAULT_MAX_READS  # stopped at the budget, not at EOF
    assert obs.probe.bytes_read < decompressed / 5  # touched a small prefix, not the file
    # The read budget binds first here (N x ~40 B is far under the 256 MB byte cap), so this is the
    # number to pin: a whole-file stream would be ~134 MB, orders of magnitude larger.
    assert obs.probe.bytes_read < 20_000_000
    assert obs.estimated_total_reads > 1_000_000  # and it still knows the file is huge


@pytest.mark.xdist_group("enormous-fastq")
def test_the_byte_budget_binds_when_the_reads_are_long(enormous_fastq: Enormous) -> None:
    """The other half of the bounded-read contract: `--max-reads` AND `--max-bytes`, not either alone.

    A read budget alone is not a byte budget — 200 000 long reads is unbounded work. The byte cap is
    what makes the guarantee hold for a chemistry we have not met yet.
    """
    path, _ = enormous_fastq

    obs = probe_file(path, max_reads=10_000_000, max_bytes=1_000_000)

    assert obs.probe.bytes_read <= 1_100_000  # the byte cap bound it, with a decoder-block margin
    assert obs.probe.n_reads_sampled < 10_000_000  # ...and stopped it well short of the read budget


def test_the_content_address_is_stable_and_distinguishes_content(tmp_path: Path) -> None:
    """The content key is a NAME: same bytes -> same key, different content -> different key.

    Replaces the old whole-file-sha test. The key is now derived from the bounded head + size + gzip
    ISIZE (issue #37), never a whole-file read.
    """
    rng = random.Random(6)
    a = tmp_path / "a.fastq.gz"
    b = tmp_path / "b.fastq.gz"
    seqs = [_rand_seq(rng, 28) for _ in range(50)]
    write_fastq_gz(a, _recs(seqs))
    write_fastq_gz(b, _recs(seqs[:-1]))  # one fewer read => different content
    key_a = probe_file(a).file.sha256
    assert key_a == probe_file(a).file.sha256  # stable across probes of the same file
    assert key_a != probe_file(b).file.sha256  # distinct content => distinct key


def _reader_fixture(tmp_path: Path, n: int = 500, read_len: int = 40) -> bytes:
    """A gzipped FASTQ as raw bytes, for feeding BoundedReader a stream directly."""
    rng = random.Random(11)
    path = tmp_path / "reader.fastq.gz"
    write_fastq_gz(path, _recs([_rand_seq(rng, read_len) for _ in range(n)]))
    return path.read_bytes()


def test_the_reader_stops_at_the_read_budget(tmp_path: Path) -> None:
    """The read budget's first bound, tested where it is enforced rather than through a probe."""
    reader = BoundedReader(BytesIO(_reader_fixture(tmp_path)), Budget(10, 1 << 30))
    records = list(reader)

    assert len(records) == 10 and reader.n_reads == 10
    assert reader.budget_exhausted  # a budget stopped it, not EOF
    assert reader.ok and not reader.truncated
    header, seq, plus, qual = records[0]
    assert header == b"@SIM:0" and plus == b"+"  # newlines stripped, bytes preserved
    assert len(seq) == 40 and len(qual) == 40


def test_the_reader_stops_at_the_byte_budget(tmp_path: Path) -> None:
    """R3's second budget: whichever half trips first stops the read, so a huge `max_reads` alone does not unbound it."""
    reader = BoundedReader(BytesIO(_reader_fixture(tmp_path)), Budget(1_000_000, 2_000))
    records = list(reader)

    assert 0 < len(records) < 500  # stopped well short of the file
    assert reader.budget_exhausted
    # It stops on the first record that crosses the budget, so it overshoots by at most one record.
    assert 2_000 <= reader.decompressed_bytes < 2_000 + 200


def test_an_abandoned_read_says_so_instead_of_reaching_into_a_closed_handle(
    tmp_path: Path,
) -> None:
    """A generator finalised AFTER its caller closed the handle: legible, never an unraisable crash.

    R3's one FASTQ loop ends by taking its final position, and a caller that stops mid-stream —
    which is what every mid-loop refusal does — may well have closed the handle before the generator
    is collected. Reaching into it there raises out of `GeneratorExit`, where nothing can catch it:
    it prints at some later, unrelated moment and is ignored, which is the worst shape a failure in
    the shared reader can have.

    `sys.unraisablehook` rather than the suite's `filterwarnings = error`, because collection time
    is not ours to schedule: the warning pytest raises for one arrives during whichever test happens
    to be running when the collector gets to it. Capturing the hook makes the assertion about THIS
    generator, deterministically.
    """
    path = tmp_path / "abandoned.fastq.gz"
    path.write_bytes(_reader_fixture(tmp_path))
    handle = open(path, "rb")  # noqa: SIM115 - closed by hand, which is the case under test
    reader = BoundedReader(handle, Budget(1_000_000, 1 << 30))
    stream = iter(reader)
    next(stream)  # begin, then walk away from it the way a refusal does
    handle.close()

    unraisable: list[object] = []
    previous = sys.unraisablehook
    sys.unraisablehook = unraisable.append
    try:
        stream.close()
        del stream
        gc.collect()
    finally:
        sys.unraisablehook = previous

    assert unraisable == [], f"finalising the reader reached into a closed handle: {unraisable}"
    assert reader.abandoned
    # Absent, never zero: the count was never taken, and nothing may read this as a measurement.
    assert reader.compressed_bytes == 0


def test_a_read_that_finished_is_not_abandoned_however_it_ended(tmp_path: Path) -> None:
    """`abandoned` is a verdict about the READ, and both endings that are not one clear it.

    A clean EOF and a **Budget** trip are the two ways a read finishes, and neither is abandonment —
    which is exactly why the flag is not called `exhausted`: `budget_exhausted` already means the
    second of them, and means the opposite thing.
    """
    whole = BoundedReader(BytesIO(_reader_fixture(tmp_path)), Budget(1_000_000, 1 << 30))
    list(whole)
    assert not whole.abandoned and not whole.budget_exhausted and whole.compressed_bytes > 0

    bounded = BoundedReader(BytesIO(_reader_fixture(tmp_path)), Budget(10, 1 << 30))
    list(bounded)
    assert not bounded.abandoned and bounded.budget_exhausted and bounded.compressed_bytes > 0

    # And a caller that stops mid-stream with its handle still OPEN measured the count it stopped
    # at — which is what the plate extractor's exit-stack ordering buys, and why it is kept.
    stopped = BoundedReader(BytesIO(_reader_fixture(tmp_path)), Budget(1_000_000, 1 << 30))
    stream = iter(stopped)
    next(stream)
    stream.close()
    assert not stopped.abandoned and stopped.compressed_bytes > 0


def _cut_tail(data: bytes) -> bytes:
    return data[:-20]  # a member cut mid-stream (a truncated upload / a bounded range-read head)


def _not_gzip(_data: bytes) -> bytes:
    return b"this is not a gzip stream" * 100  # never a gzip at all


def _corrupt_deflate(data: bytes) -> bytes:
    b = bytearray(data)
    b[len(b) // 2] ^= 0xFF  # a bit flip inside the deflate payload, past the header -> zlib.error
    return bytes(b)


def _bad_crc(data: bytes) -> bytes:
    b = bytearray(data)
    b[-8] ^= 0xFF  # the CRC32 field of the trailer: the payload decodes but is not what it claims
    return bytes(b)


#: ``(mutate, budget, ok, truncated, has_records)`` — the reader's integrity verdict over every
#: corrupt/edge gzip it must survive. The verdict is the two flags ``(ok, truncated)`` and they must
#: NOT collapse into one: a mid-member cut is ``ok=True, truncated=True`` ("re-download and verify the
#: checksum"), while a non-gzip / corrupt-deflate / bad-CRC stream is ``ok=False, truncated=False``
#: ("this is not gzip FASTQ") — two different remedies, so two different Blockers, so two different
#: flags (#94). ``has_records`` is the other half each case uniquely proves: an intact head still
#: parses under a cut or a trailer error (``True``), a non-gzip yields nothing (``False``, and
#: ``n_reads == 0``), and a mid-payload flip is pinned only to NOT RAISE (its record count is an
#: implementation detail), so it carries ``None``.
GZIP_VERDICT = [
    pytest.param(_cut_tail, Budget(1_000_000, 1 << 30), True, True, True, id="cut-mid-member"),
    pytest.param(_not_gzip, Budget(100, 1 << 30), False, False, False, id="not-gzip"),
    pytest.param(
        _corrupt_deflate, Budget(1_000_000, 1 << 30), False, False, None, id="corrupt-deflate"
    ),
    pytest.param(_bad_crc, Budget(1_000_000, 1 << 30), False, False, True, id="bad-crc"),
]


@pytest.mark.parametrize("mutate, budget, ok, truncated, has_records", GZIP_VERDICT)
def test_the_reader_pins_a_distinct_integrity_verdict_per_corruption(
    tmp_path: Path,
    mutate: Callable[[bytes], bytes],
    budget: Budget,
    ok: bool,
    truncated: bool,
    has_records: bool | None,
) -> None:
    """The ``(ok, truncated)`` truth table over corrupt gzip — and the two flags must never collapse.

    ``gzip.GzipFile`` reads its header lazily, so a format error surfaces on the first *record* read —
    the same call a mid-member cut fails in — and the reader once flagged both as ``truncated``,
    leaving ``ok`` unreachable and a caller unable to tell a partial upload from bytes that were never
    FASTQ. The remedies differ, so the two Blockers differ, so the two flags must (#94). A
    corrupt-deflate member additionally raises ``zlib.error``, which is neither ``OSError`` nor
    ``EOFError`` and so once ESCAPED the iterator and killed the probe — here it must be caught, i.e.
    ``list(reader)`` must not raise, whatever the corruption.
    """
    reader = BoundedReader(BytesIO(mutate(_reader_fixture(tmp_path))), budget)
    records = list(reader)  # must not raise

    assert (reader.ok, reader.truncated) == (ok, truncated)
    if has_records is True:
        assert records  # the intact head still parsed
    elif has_records is False:
        assert records == [] and reader.n_reads == 0  # nothing parsed, and the counter says so


def _gz_member(payload: bytes) -> bytes:
    """One gzip member holding exactly these bytes — a FASTQ laid out by hand, separators and all."""
    buf = BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(payload)
    return buf.getvalue()


#: ``(n_records, read_len, terminator, final_newline)`` — the line shapes a FASTQ arrives in. A long
#: read is longer than one decompressed pull, so a single LINE spans several of them; CRLF and a
#: missing final newline are the two ways a file disagrees with the tidy case about where a line ends.
READER_SHAPES = [
    pytest.param(200, 40, b"\n", True, id="short-reads"),
    pytest.param(3, 30_000, b"\n", True, id="a-line-longer-than-one-pull"),
    pytest.param(200, 40, b"\n", False, id="no-final-newline"),
    pytest.param(200, 40, b"\r\n", True, id="crlf"),
    pytest.param(200, 40, b"\r\n", False, id="crlf-no-final-newline"),
    pytest.param(2, 0, b"\n", True, id="empty-sequence"),
]


@pytest.mark.parametrize("n, read_len, eol, final_newline", READER_SHAPES)
def test_the_reader_hands_back_the_lines_it_was_given_and_counts_every_byte(
    n: int, read_len: int, eol: bytes, final_newline: bool
) -> None:
    """Same bytes in, same records out — and the accounting is the file's own size, not an estimate.

    The counters are outputs of the iteration: read after the loop, they describe it. What they must
    describe is *these* bytes, and a FASTQ is not always the tidy case — a line can be longer than
    one decompressed pull, a file written on Windows ends its lines with a carriage return that
    belongs to the LINE (only the feed separates one from the next), and a file can stop without a
    final newline at all. Each shape is a way the record text and the byte count can quietly
    disagree with the file that was read, and a fingerprint slice writes these records back out — so
    a byte that goes missing here is a byte that goes missing from a package.
    """
    rng = random.Random(11)
    records = [
        (f"@SIM:{i}".encode(), _rand_seq(rng, read_len).encode(), b"+", b"I" * read_len)
        for i in range(n)
    ]
    payload = b"".join(eol.join(record) + eol for record in records)
    carriage = eol[:-1]  # whatever precedes the line feed stays on the line
    expected: list[tuple[bytes, ...]] = [
        tuple(line + carriage for line in record) for record in records
    ]
    if not final_newline:
        payload = payload[: -len(eol)]
        expected[-1] = expected[-1][:3] + (records[-1][3],)
    data = _gz_member(payload)

    reader = BoundedReader(BytesIO(data), Budget(10_000, 1 << 30))

    assert list(reader) == expected
    assert reader.n_reads == n
    assert reader.ok and not reader.truncated
    assert not reader.budget_exhausted  # a clean EOF, not a budget stop
    assert reader.decompressed_bytes == len(payload)  # every line and every separator, exactly
    assert 0 < reader.compressed_bytes <= len(data)


def _value_stable_fixture(path: Path) -> None:
    """A deterministic FASTQ exercising every Tier-A signal, small enough to be read to EOF.

    Read-to-EOF is the point: ``budget_exhausted`` is then False and the read estimate is the exact
    sampled count, so no signal field depends on the compressed size — and ``gzip.open`` writes the
    *filename* into its header, which would otherwise leak a ``tmp_path`` into the digest.

    Qualities span ords 35..73 rather than the suite's usual all-``'I'``: a constant ``'I'`` (73)
    resolves to ``quality_encoding == "unknown"``, so every other fixture here leaves the phred33
    branch unexercised.
    """
    rng = random.Random(1234)
    pool = [_rand_seq(rng, 16) for _ in range(40)]  # recurring barcodes -> a low distinct ratio
    records: list[tuple[str, str, str]] = []
    for i in range(200):
        seq = rng.choice(pool) + W1_LINKER + _rand_seq(rng, 8) + "T" * 10
        if i % 50 == 0:
            seq = seq[:30] + "N" + seq[31:]  # a sprinkle of N so called coverage is below 1.0
        qual = "".join(chr(35 + (j % 39)) for j in range(len(seq)))
        records.append((f"INSTR:1:FLOWCELL:1:1101:{1000 + i}:{2000 + i} 1:N:0:ACGTACGT", seq, qual))
    # The fixture that pins bytes owns its compressor. `size_bytes` and `sha256` below are literals,
    # and the shared writer defaults to level 1 for the fixtures where nothing reads the size.
    write_fastq_gz(path, records, compresslevel=9)


def test_the_signal_fields_are_value_stable(tmp_path: Path) -> None:
    """Pin the observation's signal values against a literal digest.

    A refactor of the probe must not move a single observed value: `Observation` values are what
    `dataset_content_hash` covers, so a shift here re-hashes every pinned manifest. Nothing else in
    the suite pins absolute values — `test_fingerprint` compares two probe paths *against each
    other*, so it stays green if both move together.

    `file` and `probe` are excluded: the first carries a tmp path, the second the version stamp
    (`PROBE_VERSION` is deliberately not part of the manifest hash — `manifest/hash.py` hashes
    values, not tool versions). The named assertions below are not redundant with the digest: when
    the digest breaks they say *which* signal moved.
    """
    path = tmp_path / "stable.fastq.gz"
    _value_stable_fixture(path)
    obs = probe_file(path)

    signals = obs.model_dump(mode="json", exclude={"file", "probe"})
    canonical = json.dumps(signals, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == VALUE_STABLE_DIGEST

    # Legibility on failure: which signal moved?
    assert obs.read_length.mode == 56  # 16 CB + 22 linker + 8 UMI + 10 polyT
    assert obs.read_length.n_distinct == 1
    assert obs.estimated_total_reads == 200  # read to EOF -> exact, never extrapolated
    assert obs.est_method == "isize"
    assert obs.quality_encoding == "phred33"
    assert obs.gzip.ok and not obs.gzip.truncated
    # 200 reads x 56 cycles, four of them holding the one sprinkled N.
    assert obs.coverage.reach_fraction == 1.0
    assert obs.coverage.called_fraction == pytest.approx(1 - 4 / (200 * 56))
    assert obs.read_name.parsed and obs.read_name.lane == 1
    assert [type(s).__name__ for s in obs.segments] == VALUE_STABLE_SEGMENTS


def test_the_identity_and_provenance_fields_are_value_stable(tmp_path: Path) -> None:
    """The other half of the observation — the half the digest above deliberately excludes.

    `test_the_signal_fields_are_value_stable` drops `file` and `probe`, for good reasons (a tmp path,
    a version stamp). The effect is that *nothing* pins how a file is named or what a probe records
    about its own read — so a refactor of the identity path could move every content address in the
    corpus and the suite would stay green. The observation cache is keyed by `file.sha256` and
    `dataset_uris` maps one sha per file, so a shift there is not cosmetic.

    Each literal is paired with the inputs it is computed from, which is the difference between a
    guard and a tripwire. `_content_key` folds in the **compressed** size, so a different zlib could
    move `sha256` through no fault of the probe; when that happens `size_bytes` moves with it and the
    failure reads as "the fixture compressed differently" instead of "identity broke". `isize` is the
    uncompressed size and depends on no compressor at all, so it pins the record text itself.
    """
    path = tmp_path / "stable.fastq.gz"
    _value_stable_fixture(path)
    obs = probe_file(path)

    # The inputs to the address, pinned so a failure above says which one moved.
    assert obs.file.basename == "stable.fastq.gz"  # part of the identity: files differ by name
    assert obs.file.size_bytes == 2850  # compressed; zlib-dependent
    assert gzip_isize(path) == 33200  # uncompressed; depends only on the records
    assert obs.file.sha256 == "4f72e7da08a0e654eb284ba19b49132c8f1f544d8a85548ac7daeeb656be3196"

    # A local probe stages a path; `local_uri` is the one field that cannot be a literal.
    assert obs.file.local_uri == str(path)

    # Provenance. `params_hash` is a pure function of the budget — no fixture, no compressor, no
    # environment — so it pins as a bare literal, and any change to what feeds it goes red here.
    assert obs.probe.params_hash == _params_hash(Budget(DEFAULT_MAX_READS, DEFAULT_MAX_BYTES))
    assert obs.probe.params_hash == "8ffd5fe97ddea836"
    assert obs.probe.n_reads_sampled == 200  # the whole fixture: read to EOF, not budget-stopped
    assert obs.probe.bytes_read == 33200  # decompressed, so it equals the ISIZE above
    assert obs.probe.compressed_bytes_read == 2850  # and this equals the file size
    assert obs.probe.tool_version == PROBE_VERSION  # stamped, though not hashed into a manifest


def test_the_stamped_budget_is_the_one_the_head_was_read_under(tmp_path: Path) -> None:
    """`params_hash` describes the read, and there is no longer a way to make it describe anything else.

    It used to be recomputed inside `build_observation` from `max_reads`/`max_bytes` the caller passed
    a *second* time, alongside a head it merely promised had been read under them. Nothing checked the
    promise and nothing reads `params_hash` downstream, so a caller that passed a different budget
    stamped a hash that lied, silently and forever. The budget now rides on the head, so the two
    cannot disagree — this pins that they don't.
    """
    path = tmp_path / "stable.fastq.gz"
    _value_stable_fixture(path)
    odd = Budget(37, 1 << 20)

    head = FastqHead.from_path(path, odd)
    obs, _seqs = build_observation(head, local_whole_file(path, head.seqs))

    assert head.budget == odd  # the head remembers what bounded it
    assert head.n_reads == 37  # and it really was bounded by it
    assert obs.probe.params_hash == _params_hash(odd)
    assert obs.probe.params_hash != _params_hash(Budget())  # not the default it never used


def test_local_uri_follows_the_head_not_the_file_it_describes(tmp_path: Path) -> None:
    """`local_uri` answers "where were the bytes", which is a fact about the read, not the file.

    The distinction is invisible for a local probe (the two files are the same one) and load-bearing
    for a fingerprint replay, where the head is cut from a slice while the identity describes an
    absent original. Feeding a stream a `WholeFile` that names some other file must therefore leave
    `local_uri` empty rather than inventing a path from the basename.
    """
    path = tmp_path / "stable.fastq.gz"
    _value_stable_fixture(path)
    elsewhere = WholeFile(
        basename="somewhere_else.fastq.gz", sha256="d" * 64, size_bytes=99, isize=1
    )

    from_disk = FastqHead.from_path(path)
    from_stream = FastqHead.read(BytesIO(path.read_bytes()))

    disk_obs, _ = build_observation(from_disk, elsewhere)
    stream_obs, _ = build_observation(from_stream, elsewhere)

    assert disk_obs.file.local_uri == str(path)  # the head knew where it read
    assert stream_obs.file.local_uri is None  # a stream has nowhere to point
    # Both were told the same file, and both say so — identity does not follow the bytes.
    assert disk_obs.file.basename == stream_obs.file.basename == "somewhere_else.fastq.gz"
    assert disk_obs.file.sha256 == stream_obs.file.sha256 == "d" * 64


class _CountingReader:
    """Wrap a binary file object and tally every byte handed out by ``read``/``readinto``."""

    def __init__(self, fh: BufferedReader, counter: list[int]) -> None:
        self._fh = fh
        self._counter = counter

    def read(self, *args: int) -> bytes:
        data = self._fh.read(*args)
        self._counter[0] += len(data)
        return data

    def readinto(self, b: bytearray | memoryview) -> int:
        n = self._fh.readinto(b)
        self._counter[0] += n
        return n

    def __getattr__(self, name: str) -> object:
        return getattr(self._fh, name)

    def __enter__(self) -> _CountingReader:
        self._fh.__enter__()
        return self

    def __exit__(
        self,
        cls: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._fh.__exit__(cls, value, traceback)


@pytest.mark.xdist_group("enormous-fastq")
def test_the_content_address_never_scans_the_whole_file(
    enormous_fastq: Enormous, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#37 tripwire: fingerprinting must never read a whole FASTQ, however large.

    The old content-address hashed the entire *compressed* file — a whole-file read that
    ``obs.probe.bytes_read`` (decompressed sample only) never saw, so no existing test caught it. This
    counts EVERY byte read from the file at the OS boundary and pins it under the bounded head; a
    regression that scans the whole file reads >= its on-disk size and fails both assertions.
    """
    # from the fixture, so the 0.45s of WRITING happens before `counting_open` is installed — counted
    # as reading, it would blow the budget assertion at the bottom of this test
    path, _ = enormous_fastq  # >100 MB decompressed, small on disk
    on_disk = path.stat().st_size

    counter = [0]
    # Every open inside the patched window is `open(<path>, "rb")`, so the passthrough is binary —
    # but it stays varargs, because `open` is patched on `builtins` and must forward whatever it gets.
    real_open: Callable[..., BufferedReader] = builtins.open

    def counting_open(
        file: object, *args: object, **kwargs: object
    ) -> BufferedReader | _CountingReader:
        fh = real_open(file, *args, **kwargs)
        return _CountingReader(fh, counter) if str(file) == str(path) else fh

    monkeypatch.setattr(builtins, "open", counting_open)
    obs = probe_file(path)  # DEFAULT budgets: DEFAULT_MAX_READS reads / 256 MB
    monkeypatch.undo()

    # Precondition: the file really is much larger than the bounded head we sampled.
    assert on_disk > obs.probe.compressed_bytes_read * 3
    assert re.fullmatch(r"[0-9a-f]{64}", obs.file.sha256)  # it still produced a key...
    assert counter[0] < on_disk  # ...without scanning the whole compressed file
    # Tighter: only the bounded head sample (+ the 4-byte ISIZE trailer + decoder read-ahead).
    assert counter[0] <= obs.probe.compressed_bytes_read + 65_536


# ================================================================================================
# head coverage — what each head-derived statistic was actually measured over (#190)
# ================================================================================================
#: A head slice is not a random sample, so composition, segmentation and the windows
#: `consensus_match_rate` is cut from are worth what they were taken over. These pin the figure that
#: says so: honest on a head that covers its material, and loud on the one cycle that does not.
#: They pin REPORTING only — `test_the_coverage_figures_decide_nothing` is the other half.


def test_a_head_that_covers_its_material_says_so(tmp_path: Path) -> None:
    """The undegraded baseline, without which a degraded number means nothing.

    Every read is the same length and every base was called, so nothing went missing by either
    route: each cycle's denominator is the whole sample, and every span was classified over all of
    it. A figure that could not read 1.0 here would be a units bug rather than a measurement.
    """
    rng = random.Random(20)
    seqs = [_rand_seq(rng, 30) for _ in range(400)]
    path = tmp_path / "clean.fastq.gz"
    write_fastq_gz(path, _recs(seqs))

    obs = probe_file(path)

    assert obs.coverage.reach_fraction == 1.0
    assert obs.coverage.called_fraction == 1.0
    assert [c.n_sampled for c in obs.per_cycle_composition] == [400] * 30
    assert obs.segments and all(s.coverage == 1.0 for s in obs.segments)


def test_a_ragged_head_loses_reach_while_every_base_it_has_was_called(tmp_path: Path) -> None:
    """The two loss channels are separable, and a trimmed file must not read as an unread one.

    Half these reads stop at cycle 20, so a statistic over the tail rests on half the sample — the
    denominator `window_bases` silently applies when it drops a read too short to span a column.
    That is a fact about read lengths and nothing is wrong with the run, which is exactly why it is
    reported apart from the base-call channel instead of averaged into one number.
    """
    rng = random.Random(21)
    seqs = [_rand_seq(rng, 50) for _ in range(100)] + [_rand_seq(rng, 20) for _ in range(100)]
    path = tmp_path / "ragged.fastq.gz"
    write_fastq_gz(path, _recs(seqs))

    obs = probe_file(path)
    denoms = [c.n_sampled for c in obs.per_cycle_composition]

    assert denoms[:20] == [200] * 20  # every read reaches the first 20 cycles...
    assert denoms[20:] == [100] * 30  # ...and only the long half reaches the rest
    assert obs.coverage.reach_fraction == pytest.approx((100 * 50 + 100 * 20) / (200 * 50))
    assert obs.coverage.called_fraction == 1.0  # nothing here was uncalled


#: The dark cycle, at GSE305031's proportion: N at one cycle in 91% of the head's reads and nowhere
#: else. `i % 100 >= 9` puts 91 of every 100 reads in the dark.
DARK_CYCLE = 12
DARK_SHARE = 0.91


def _dark_cycle_fixture(path: Path, n: int = 1000) -> None:
    """8 bp barcode | 22 bp W1 linker | 10 bp polyT, with one linker cycle mostly uncalled."""
    rng = random.Random(22)
    seqs = []
    for i in range(n):
        seq = _rand_seq(rng, 8) + W1_LINKER + "T" * 10
        if i % 100 >= 9:
            seq = seq[:DARK_CYCLE] + "N" + seq[DARK_CYCLE + 1 :]
        seqs.append(seq)
    write_fastq_gz(path, _recs(seqs))


def test_a_dark_cycle_shows_up_as_lost_coverage_and_nowhere_else_in_the_segmentation(
    tmp_path: Path,
) -> None:
    """The measured case: a cycle nobody called splits a linker, and only coverage says why.

    91% N at one cycle leaves a dominant base fraction of 0.09, which is under the purity threshold,
    so the cycle is classified `random` — and a `random` span carries nothing that distinguishes
    "these bases vary" from "these bases were never read". `evals/benchmark/GSE305031` is a real
    library shaped exactly like this at R1 cycle 2. The classification is left alone deliberately;
    what changes is that the span now says it was decided over 9% of the sample while its neighbours
    were decided over all of it.
    """
    path = tmp_path / "dark.fastq.gz"
    _dark_cycle_fixture(path)

    obs = probe_file(path)
    spans = [(type(s).__name__, s.start, s.end) for s in obs.segments]

    # The linker is split in two by the one cycle that was not read.
    assert spans == [
        ("RandomSegment", 0, 8),
        ("ConstantSegment", 8, 12),
        ("RandomSegment", 12, 13),
        ("ConstantSegment", 13, 28),
        ("HomopolymerSegment", 28, 40),
    ]
    dark = next(s for s in obs.segments if (s.start, s.end) == (DARK_CYCLE, DARK_CYCLE + 1))
    assert dark.coverage == pytest.approx(1 - DARK_SHARE)
    assert all(s.coverage == 1.0 for s in obs.segments if s is not dark)
    assert obs.per_cycle_composition[DARK_CYCLE].n == pytest.approx(DARK_SHARE)


def test_the_head_wide_figure_barely_moves_for_the_cycle_that_ruined_a_statistic(
    tmp_path: Path,
) -> None:
    """Why the figure is recorded per span and not only per file: one bad cycle in forty is 2%.

    A whole-head average dilutes exactly the artefact it would be consulted about — the head-wide
    called coverage of the fixture above is ~0.977, which is indistinguishable from a clean file,
    while the span that a chemistry decision actually reads sits at 0.09. A single number would have
    been collected for the corpus and shown nothing.
    """
    path = tmp_path / "dark.fastq.gz"
    _dark_cycle_fixture(path)

    obs = probe_file(path)
    read_len = 8 + len(W1_LINKER) + 10

    assert obs.coverage.reach_fraction == 1.0  # every read is full length; only a base is missing
    assert obs.coverage.called_fraction == pytest.approx(1 - DARK_SHARE / read_len)
    assert obs.coverage.called_fraction > 0.97


#: The three field names this issue added that a scorer must never read. `CycleComposition.n_sampled`
#: is deliberately NOT here: `WindowProbe` already exposes an `n_sampled` of its own that scoring may
#: legitimately read, and an attribute guard cannot tell the two apart.
REPORT_ONLY_FIGURES = {"coverage", "reach_fraction", "called_fraction"}


@pytest.mark.xdist_group("src-trees")
def test_the_coverage_figures_decide_nothing(src_trees: SrcTrees) -> None:
    """Report only, as a mechanism rather than as a remembered rule.

    The decision that added these numbers was explicit that a poor one must refuse nothing: making
    coverage gate is a separate call with refusal consequences, and there is no evidence yet about
    where a sensible threshold sits. That intent survives exactly as long as someone remembers it,
    so it is checked instead — no module outside the two that produce and declare the figures may
    even read one. A future change that gates on coverage has to delete this test, which is the
    point: it makes the gate a decision someone takes rather than one that arrives in a diff.
    """
    owners = {"probe", "models"}
    offenders = [
        f"{path.parent.name}/{path.name}:{node.lineno} reads .{node.attr}"
        for path, tree in src_trees.items()
        if path.parent.name not in owners
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in REPORT_ONLY_FIGURES
    ]

    assert not offenders, (
        "a coverage figure is report-only and nothing may consume one:\n  " + "\n  ".join(offenders)
    )


# ================================================================================================
# consensus_match_rate — the per-read statistic `has_segment kind: constant` gates on (#149)
# ================================================================================================
#: These reach the numpy kernel directly, rather than through `WindowProbe`, because the properties
#: that make it safe are properties of the counting: junk stays in the denominator, a pad byte never
#: matches, and a window of pure noise scores ~0. Routed through a probe they would be entangled with
#: window-cutting, and the one that matters most — that the statistic CANNOT be driven to 1.0 by
#: selection — is invisible unless you can hand it a population you chose.


@pytest.mark.parametrize("carriers, n", [(0, 400), (100, 400), (200, 400), (400, 400)])
def test_consensus_match_rate_is_the_share_of_carriers(carriers: int, n: int) -> None:
    """It reports the fraction carrying the consensus — junk COUNTED, never filtered out.

    The whole defect in #149 was a statistic that could not tell a clean population from a
    contaminated one. This is the property that fixes it, so it is pinned by value: hand it a known
    mixture and the answer is that mixture.
    """
    from seqforge.probe.signals import consensus_match_rate

    rng = random.Random(7)
    fixed = "ACGTACGTACGTACGTACGTACGTACGTAC"  # 30 bp, the width of a SPLiT-seq linker
    bases = [fixed] * carriers + [
        "".join(rng.choice("ACGT") for _ in range(len(fixed))) for _ in range(n - carriers)
    ]
    rng.shuffle(bases)
    assert consensus_match_rate(bases, 3) == pytest.approx(carriers / n, abs=0.02)


def test_consensus_match_rate_bottoms_out_on_pure_noise() -> None:
    """A window with no fixed sequence scores ~0 — the falsifiability the mean could not offer.

    Matching 30 columns to within 3 by chance is ~1e-13, so a modal consensus computed over noise is
    an artefact no read actually carries. If this ever returned a high number, every 30 bp window of
    anything would look like a linker and the gate would be decorative.
    """
    from seqforge.probe.signals import consensus_match_rate

    rng = random.Random(11)
    noise = ["".join(rng.choice("ACGT") for _ in range(30)) for _ in range(500)]
    rate = consensus_match_rate(noise, 3)
    assert rate is not None and rate < 0.01


def test_consensus_match_rate_counts_a_short_read_as_a_non_carrier() -> None:
    """A read that falls short of the column is a non-carrier, never a partial match.

    The pad sentinel is 0, which is not a base byte, so it can never equal a consensus base. Were it
    counted as agreement instead, a truncated file would score as though it carried the sequence.
    """
    from seqforge.probe.signals import consensus_match_rate

    fixed = "ACGTACGTACGT"
    assert consensus_match_rate([fixed] * 9 + [fixed[:4]], 0) == pytest.approx(0.9)


def test_consensus_match_rate_has_no_answer_for_nothing() -> None:
    """Empty input is ``None`` — "not measured", which the caller turns into ABSTAIN, not FAIL."""
    from seqforge.probe.signals import consensus_match_rate

    assert consensus_match_rate([], 1) is None
    assert consensus_match_rate(["", ""], 1) is None
