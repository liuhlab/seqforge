"""Tests for the bounded Tier A probe on synthetic gzipped FASTQ fixtures."""

from __future__ import annotations

import gzip
import random
from pathlib import Path

from seqforge.models.observation import ConstantSegment, HomopolymerSegment, RandomSegment
from seqforge.probe import hash_file, probe_file

BASES = "ACGT"
W1_LINKER = (
    "GAGTGATTGCTTGTGACGCCTT"  # a fixed 22 bp adapter (inDrop's W1), used to test constant detection
)


def _rand_seq(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(BASES) for _ in range(n))


def _write_fastq_gz(path: Path, records: list[tuple[str, str, str]]) -> None:
    with gzip.open(path, "wt") as fh:
        for name, seq, qual in records:
            fh.write(f"@{name}\n{seq}\n+\n{qual}\n")


def _recs(seqs: list[str], name: str = "SIM") -> list[tuple[str, str, str]]:
    return [(f"{name}:{i}", s, "I" * len(s)) for i, s in enumerate(seqs)]


def test_10x_r1_geometry(tmp_path: Path) -> None:
    rng = random.Random(0)
    pool = [_rand_seq(rng, 16) for _ in range(50)]  # 50 recurring cell barcodes
    seqs = [rng.choice(pool) + _rand_seq(rng, 12) for _ in range(2000)]  # 16 CB + 12 UMI = 28 bp
    path = tmp_path / "r1.fastq.gz"
    _write_fastq_gz(path, _recs(seqs))

    obs = probe_file(path)
    assert obs.read_length.mode == 28
    assert obs.read_length.n_distinct == 1
    assert obs.probe.n_reads_sampled == 2000
    assert obs.gzip.ok and not obs.gzip.truncated
    assert any(isinstance(s, RandomSegment) for s in obs.segments)
    assert obs.file.sha256 == hash_file(path)


def test_linker_and_polyt_segmentation(tmp_path: Path) -> None:
    rng = random.Random(1)
    seqs = [_rand_seq(rng, 8) + W1_LINKER + "T" * 10 for _ in range(500)]  # 8 random + W1 + polyT
    path = tmp_path / "indrop.fastq.gz"
    _write_fastq_gz(path, _recs(seqs))

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
    _write_fastq_gz(path, _recs(seqs))

    windows = probe_file(path).distinct_value_windows
    assert windows, "a random 16 bp segment should yield a distinct-ratio window"
    assert min(w.distinct_ratio for w in windows) < 0.1  # cell-barcode recurrence, not UMI


def test_truncated_gzip_is_flagged(tmp_path: Path) -> None:
    rng = random.Random(3)
    path = tmp_path / "trunc.fastq.gz"
    _write_fastq_gz(path, _recs([_rand_seq(rng, 28) for _ in range(300)]))
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) - 20])  # cut the gzip stream mid-member

    obs = probe_file(path)
    assert obs.gzip.truncated or not obs.gzip.ok


def test_sra_normalized_header_detected(tmp_path: Path) -> None:
    rng = random.Random(4)
    recs = [(f"SRR9999999.{i} {i} length=28", _rand_seq(rng, 28), "I" * 28) for i in range(1, 51)]
    path = tmp_path / "sra.fastq.gz"
    _write_fastq_gz(path, recs)

    obs = probe_file(path)
    assert obs.read_name.sra_normalized is True
    assert obs.read_name.parsed is False


def test_bounded_budget_and_read_estimate(tmp_path: Path) -> None:
    rng = random.Random(5)
    path = tmp_path / "big.fastq.gz"
    _write_fastq_gz(path, _recs([_rand_seq(rng, 28) for _ in range(5000)]))

    obs = probe_file(path, max_reads=100)
    assert obs.probe.n_reads_sampled == 100  # stopped at the budget, did NOT read all 5000
    assert obs.probe.bytes_read < 20_000  # only a bounded decompressed prefix was touched (R3)
    assert obs.estimated_total_reads > 1000  # extrapolated from compressed bytes-per-read


def _write_enormous_fastq_gz(path: Path, *, chunk_mb: int = 1, n_chunks: int = 128) -> int:
    """A FASTQ whose DECOMPRESSED stream dwarfs any budget, written in a fraction of a second.

    Highly repetitive reads compress ~300:1, so ~130 MB of decompressed FASTQ costs ~450 KB on disk
    and a quarter-second to build. That is the trick that makes R3's claim testable at all: the rule
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


def test_the_read_budget_bounds_bytes_read_however_large_the_file(tmp_path: Path) -> None:
    """R3: "a code path that CAN stream a whole multi-GB FASTQ is a bug" — asserted, not asserted-to.

    R3 cited a "50 GB reads < N bytes" check that was never written; what existed proved the budget
    bit on a 5 000-read fixture, which is a scale at which nothing could go wrong. This is the
    property that actually matters: `bytes_read` is a function of the BUDGET, not of the file. A
    regression that streamed to EOF would pass every small-fixture test in this file and fail here.
    """
    path = tmp_path / "enormous.fastq.gz"
    decompressed = _write_enormous_fastq_gz(path)
    on_disk = path.stat().st_size
    assert decompressed > 100_000_000  # the fixture really is enormous once decompressed...
    assert on_disk < 2_000_000  # ...while costing the test suite ~450 KB and ~0.2 s

    obs = probe_file(path)  # DEFAULT budgets: 200k reads / 256 MB

    assert obs.probe.n_reads_sampled == 200_000  # stopped at the budget, not at EOF
    assert obs.probe.bytes_read < decompressed / 5  # touched a small prefix, not the file
    # The read budget binds first here (200k x ~40 B is well under the 256 MB byte cap), so this is
    # the number to pin: a whole-file stream would be ~134 MB, two orders of magnitude larger.
    assert obs.probe.bytes_read < 20_000_000
    assert obs.estimated_total_reads > 1_000_000  # and it still knows the file is huge


def test_the_byte_budget_binds_when_the_reads_are_long(tmp_path: Path) -> None:
    """The other half of R3's contract: `--max-reads` AND `--max-bytes`, not either alone.

    A read budget alone is not a byte budget — 200 000 long reads is unbounded work. The byte cap is
    what makes the guarantee hold for a chemistry we have not met yet.
    """
    path = tmp_path / "enormous.fastq.gz"
    _write_enormous_fastq_gz(path)

    obs = probe_file(path, max_reads=10_000_000, max_bytes=1_000_000)

    assert obs.probe.bytes_read <= 1_100_000  # the byte cap bound it, with a decoder-block margin
    assert obs.probe.n_reads_sampled < 10_000_000  # ...and stopped it well short of the read budget


def test_hash_file_is_content_stable(tmp_path: Path) -> None:
    rng = random.Random(6)
    a = tmp_path / "a.fastq.gz"
    b = tmp_path / "b.fastq.gz"
    seqs = [_rand_seq(rng, 28) for _ in range(50)]
    _write_fastq_gz(a, _recs(seqs))
    _write_fastq_gz(b, _recs(seqs[:-1]))
    assert hash_file(a) == hash_file(a)
    assert hash_file(a) != hash_file(b)
