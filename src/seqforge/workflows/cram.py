"""Convert STAR's ``Aligned.out.bam`` to a coordinate-sorted CRAM — the finalize step that shrinks
the retained alignment.

CRAM stores each read as its *difference* from the reference, so it is markedly smaller than BAM. The
reference is not embedded (``embed_ref`` is deliberately off): ``samtools view -C -T <ref>`` records
each sequence's MD5 in the header's ``@SQ … M5:`` tags, and seqforge's reference is a UCSC assembly id
that ``liulab-genome`` resolves deterministically forever — so the checksum plus the assembly id
recorded in the QC bundle are enough to recover the exact reference. Not embedding is the smaller,
standard choice, and the user's call.

**This takes a resolved FASTA path, never an assembly id.** Resolving ``assembly -> fasta_path`` needs
``liulab-genome``, which ships no type stubs; keeping that import in the (untyped) CLI verb lets this
module stay under ``mypy --strict`` and stay unit-testable with a throwaway FASTA. Same split as
``h5ad``: the strict workflow module does the work, the thin verb wires the environment.

samtools is **not** a dependency of this package. It is a runtime binary the ``align-rna`` image
carries (in its base layer), and the ``solo_to_cram`` rule names that image with ``container:`` —
exactly as ``starsolo_count`` does for STAR. So this module shells out to the samtools the pinned
image provides, never one seqforge installed; that is the same "consume the runtime, don't redefine
it" line that keeps STAR out of every dependency table here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class CramError(RuntimeError):
    """The BAM could not be converted (missing input, samtools failure, unreadable reference)."""


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:  # samtools not on PATH
        raise CramError(f"{cmd[0]} is not installed; CRAM conversion needs samtools") from exc
    except subprocess.CalledProcessError as exc:
        raise CramError(f"{' '.join(cmd)} exited {exc.returncode}") from exc


def _ensure_fai(fasta: Path, workdir: Path) -> Path:
    """A FASTA with a readable ``.fai`` beside it, creating one in ``workdir`` if the store is read-only.

    ``samtools view -C -T`` needs ``<ref>.fai``. ``liulab-genome``'s reference store is frequently
    read-only, so writing the index next to the FASTA fails. If an index already exists we use the
    FASTA in place; otherwise we mirror the FASTA into ``workdir`` (a symlink — no bytes copied) and
    index *that*, so the ``.fai`` lands somewhere writable.
    """
    if fasta.with_name(fasta.name + ".fai").exists():
        return fasta
    workdir.mkdir(parents=True, exist_ok=True)
    local = workdir / fasta.name
    if not local.exists():
        local.symlink_to(fasta)
    _run(["samtools", "faidx", str(local)])
    return local


#: Floor for samtools sort's per-thread memory (``-m``). Below this, sort spills to many tiny temp
#: files and thrashes; this keeps a sane minimum even when the budget divided by threads is small.
_MIN_SORT_MEM_MB = 256


def _sort_mem_per_thread_mb(sort_mem_mb: int | None, threads: int) -> int | None:
    """Per-thread ``-m`` for ``samtools sort`` from a TOTAL budget, or ``None`` to use its default.

    samtools sort holds ``-m`` bytes **per thread** before spilling, so the total is ``threads * m``.
    We spend ~3/4 of the budget on the sort (leaving headroom for the CRAM encoder running in the same
    pipe, plus the OS) and split that across threads — so more cores *and* more memory both make it
    finish faster, which is the whole point of setting it rather than single-threading the default.
    """
    if sort_mem_mb is None:
        return None
    return max(_MIN_SORT_MEM_MB, (sort_mem_mb * 3 // 4) // max(1, threads))


def bam_to_cram(
    bam: Path, fasta: Path, out: Path, threads: int = 1, sort_mem_mb: int | None = None
) -> Path:
    """``Aligned.out.bam`` -> coordinate-sorted ``out`` (CRAM) + ``out.crai``. Returns ``out``.

    Sorted so the CRAM is indexable (random access by region) and so like reads compress together.
    Every samtools stage runs multi-threaded (``-@ threads``) and the sort is given a real memory
    budget (``-m`` per thread, derived from ``sort_mem_mb``) so a fat node is actually used — never a
    single-threaded default. The BAM is left in place; the caller (a Snakemake ``temp()`` output) owns
    its deletion.
    """
    if not bam.exists():
        raise CramError(f"{bam} is missing; the STAR run that should have written it did not")
    if not fasta.exists():
        raise CramError(f"reference FASTA {fasta} does not exist")
    ref = _ensure_fai(fasta, out.parent)
    out.parent.mkdir(parents=True, exist_ok=True)
    # sort (BAM is written --outSAMtype BAM Unsorted) piped straight into the CRAM encoder, so no
    # intermediate sorted BAM lands on disk. `-T` names the reference; no embed_ref -> smallest CRAM.
    sort = ["samtools", "sort", "-@", str(threads), "-O", "bam"]
    per_thread = _sort_mem_per_thread_mb(sort_mem_mb, threads)
    if per_thread is not None:
        sort += ["-m", f"{per_thread}M"]
    sort.append(str(bam))
    view = ["samtools", "view", "-C", "-T", str(ref), "-@", str(threads), "-o", str(out), "-"]
    try:
        with subprocess.Popen(sort, stdout=subprocess.PIPE) as sorter:
            assert sorter.stdout is not None
            view_proc = subprocess.run(view, stdin=sorter.stdout, check=False)
        if sorter.returncode:
            raise CramError(f"samtools sort exited {sorter.returncode}")
        if view_proc.returncode:
            raise CramError(f"samtools view (CRAM) exited {view_proc.returncode}")
    except FileNotFoundError as exc:
        raise CramError("samtools is not installed; CRAM conversion needs samtools") from exc
    _run(["samtools", "index", "-@", str(threads), str(out)])
    return out


__all__ = ["CramError", "bam_to_cram"]
