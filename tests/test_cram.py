"""BAM -> CRAM finalize. The gates: the reference is passed (never embedded), the sort is
multi-threaded with a real memory budget (never samtools' single-thread default), and the reference
FASTA is resolved from the assembly id rather than baked as a path.

samtools is stubbed so the test needs no binary and no genome: what is asserted is the *argv* we hand
it, which is where the correctness lives (a stray ``embed_ref``, a missing ``-T``, a single thread).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from seqforge.workflows.cram import CramError, _sort_mem_per_thread_mb, bam_to_cram


def test_sort_memory_is_split_across_threads_with_headroom() -> None:
    """More cores and more memory both shrink per-thread ``-m`` sensibly; a floor stops thrashing."""
    # 32 GB budget, 8 threads -> ~3/4 of 32768 / 8 = 3072 MB/thread.
    assert _sort_mem_per_thread_mb(32768, 8) == (32768 * 3 // 4) // 8
    # A tiny budget never drops below the floor.
    assert _sort_mem_per_thread_mb(100, 8) == 256
    # No budget -> None, so samtools keeps its own default rather than us guessing.
    assert _sort_mem_per_thread_mb(None, 8) is None


class _Recorder:
    """Captures every samtools argv and makes the pipeline 'succeed' by touching the output file."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(cmd)
        # `samtools view -o <out>` and `samtools faidx <local>` must leave their file behind.
        if "view" in cmd and "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"CRAM\0")
        if cmd[:2] == ["samtools", "faidx"]:
            Path(cmd[-1] + ".fai").write_text("")
        return subprocess.CompletedProcess(cmd, 0)

    def popen(self, cmd: list[str], **kwargs: object) -> _FakePopen:
        self.calls.append(cmd)
        return _FakePopen(cmd)


class _FakePopen:
    def __init__(self, cmd: list[str]) -> None:
        self.stdout = subprocess.DEVNULL
        self.returncode = 0

    def __enter__(self) -> _FakePopen:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _stub_samtools(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec.run)
    monkeypatch.setattr(subprocess, "Popen", rec.popen)
    return rec


def test_cram_passes_the_reference_and_never_embeds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _stub_samtools(monkeypatch)
    bam = tmp_path / "Aligned.out.bam"
    bam.write_bytes(b"BAM\0")
    fasta = tmp_path / "ref.fa"
    fasta.write_text(">chr\nACGT\n")
    (tmp_path / "ref.fa.fai").write_text("")  # index already beside the fasta

    out = tmp_path / "S1" / "S1.cram"
    bam_to_cram(bam, fasta, out, threads=8, sort_mem_mb=32768)

    flat = " ".join(" ".join(c) for c in rec.calls)
    # CRAM against the reference, reference NOT embedded.
    assert "-C -T" in flat
    assert str(fasta) in flat
    assert "embed_ref" not in flat
    # Multi-threaded sort with a real per-thread budget, not a single-thread default.
    sort = next(c for c in rec.calls if c[:2] == ["samtools", "sort"])
    assert "-@" in sort and sort[sort.index("-@") + 1] == "8"
    assert "-m" in sort
    # The CRAM is indexed.
    assert any(c[:3] == ["samtools", "index", "-@"] for c in rec.calls)


def test_a_missing_bam_refuses_before_touching_samtools(tmp_path: Path) -> None:
    with pytest.raises(CramError, match="missing"):
        bam_to_cram(tmp_path / "nope.bam", tmp_path / "ref.fa", tmp_path / "o.cram")


def test_a_read_only_reference_store_gets_its_fai_written_somewhere_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No .fai beside the FASTA -> we mirror it into the output dir and index there, not in the store."""
    rec = _stub_samtools(monkeypatch)
    bam = tmp_path / "Aligned.out.bam"
    bam.write_bytes(b"BAM\0")
    fasta = tmp_path / "store" / "ref.fa"
    fasta.parent.mkdir()
    fasta.write_text(">chr\nACGT\n")  # deliberately no ref.fa.fai beside it

    out = tmp_path / "S1" / "S1.cram"
    bam_to_cram(bam, fasta, out, threads=2)

    faidx = next(c for c in rec.calls if c[:2] == ["samtools", "faidx"])
    indexed = Path(faidx[-1])
    assert indexed.parent == out.parent  # written into the writable run dir, not the store
    assert indexed.is_symlink()
