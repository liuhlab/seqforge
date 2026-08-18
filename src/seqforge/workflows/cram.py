"""Convert STAR's coordinate-sorted BAM to CRAM — the finalize step that shrinks the retained
alignment.

CRAM stores each read as its *difference* from the reference, so it is markedly smaller than BAM. The
reference is not embedded (``embed_ref`` is deliberately off): ``samtools view -C -T <ref>`` records
each sequence's MD5 in the header's ``@SQ … M5:`` tags, and seqforge's reference is a UCSC assembly id
that ``liulab-genome`` resolves deterministically forever — so the checksum plus the assembly id
recorded in the QC bundle are enough to recover the exact reference. Not embedding is the smaller,
standard choice, and the user's call.

**Nothing here sorts, and the deletion of that stage is the point.** STAR is now run with
``--outSAMtype BAM SortedByCoordinate`` — it must be, because STAR refuses to write the ``CB``/``UB``
barcode tags into anything but the sorted BAM, and a CRAM with no barcode cannot be recounted, which
is the only reason we keep one. So the BAM arrives sorted and the ``samtools sort`` that used to
stand here is gone. It was built with no ``-T``, so its spill files landed in the process's CWD (the
pipeline dir) as ``samtools.<pid>.<tid>.tmp.NNNN.bam``: undeclared to Snakemake, therefore
un-cleanable by it, and a killed or preempted job simply left them — 41.4 GiB had accumulated across
five pipeline dirs before anyone looked. Giving the sort a ``-T`` under a Snakemake-owned directory
would have fixed that leak; **removing the sort makes it impossible**, and a mechanism that cannot
leak beats one that has to be configured correctly. It also deletes a full re-sort pass over every
BAM. Do not reintroduce a sort here on the assumption that the input might be unsorted: if it ever
is, the module that ran STAR is what changed, and that is where it has to be fixed.

**This takes a resolved FASTA path, never an assembly id.** Resolving ``assembly -> fasta_path`` needs
``liulab-genome``, which ships no type stubs; keeping that import in the (untyped) CLI verb lets this
module stay under ``mypy --strict`` and stay unit-testable with a throwaway FASTA. Same split as
``h5ad``: the strict workflow module does the work, the thin verb wires the environment.

samtools is **not** a dependency of this package. It is a runtime binary the ``align-rna`` image
carries (in its base layer), and the ``solo_to_cram`` rule names that image with ``container:`` —
exactly as ``starsolo_count`` does for STAR. So this module shells out to the samtools the pinned
image provides, never one seqforge installed; that is the same "consume the runtime, don't redefine
it" line that keeps STAR out of every dependency table a RULE resolves against. The samtools a test
execs comes from a test-only pixi environment, which ships nowhere and reaches no rule.
"""

from __future__ import annotations

import subprocess
import tempfile
from enum import StrEnum
from pathlib import Path


class CramError(RuntimeError):
    """The BAM could not be converted (missing input, samtools failure, unreadable reference)."""


class RecordSelection(StrEnum):
    """Which of the aligner's records reach the archive, named once so no caller composes one.

    A selection is a **flag filter and a tag expression together**, and the two halves are not
    interchangeable. The expression can say how many loci a fragment was placed at; it cannot say
    whether this record is a primary alignment, or whether the fragment aligned at all — an unmapped
    record carries ``NH:i:1`` too, so ``[NH]==1`` on its own keeps one. Measured against the
    ``align-rna`` image's samtools on a four-record SAM (one uniquely placed, one multiply placed,
    that one's secondary, one unmapped, every record carrying ``NH``), which is also the fixture the
    test of this table rebuilds.

    ``primary`` is the default and is exactly what this converter has always done. ``mapped`` also
    drops the records that never aligned. ``unique`` and ``multi`` partition ``mapped``: every
    primary mapped record is in one of them and no record is in both, so the two archives together
    hold what one mixed archive holds and duplicate no bytes.

    A selection that needs a caveat carries it in :data:`_SELECT_CAVEAT`, so the file states its own
    terms rather than relying on a reader having found this docstring.
    """

    primary = "primary"
    mapped = "mapped"
    unique = "unique"
    multi = "multi"


#: The ``samtools view`` arguments each selection *is*, spliced into the pipe's first stage. ``0x100``
#: is secondary; ``0x104`` is secondary or unmapped. A malformed expression exits non-zero rather than
#: matching nothing, so a wrong edit here surfaces through the per-stage status check in
#: :func:`_encode` as a named error rather than as a silently short archive.
_SELECT_ARGS: dict[RecordSelection, list[str]] = {
    RecordSelection.primary: ["-F", "0x100"],
    RecordSelection.mapped: ["-F", "0x104"],
    RecordSelection.unique: ["-F", "0x104", "-e", "[NH]==1"],
    RecordSelection.multi: ["-F", "0x104", "-e", "[NH]>1"],
}

#: What a selection has to say about itself ON the file, as a SAM ``@CO`` header line. Only ``multi``
#: has anything: a multiply-placed fragment was emitted at ONE of the loci it fitted, chosen by the
#: aligner among equals, so a record in that archive is a placement and not an assignment. Reading it
#: as an assignment is the mistake the whole partition exists to make hard, and a docstring cannot
#: travel with a CRAM a user copied to a laptop — the name says which population it is and this says
#: what a row in it means. ASCII and one line, because a ``@CO`` value is one line of text and this
#: crosses awk, a locale and a CRAM header on its way in.
_SELECT_CAVEAT: dict[RecordSelection, str] = {
    RecordSelection.multi: (
        "seqforge: multiply-placed records. A record here means one of this fragment's possible "
        "loci is this one; it never means the fragment belongs here."
    ),
}


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

    ``workdir`` is a **per-call temporary directory**, not the rule's output directory, and that is
    the last undeclared write this module had. The mirror used to land beside the CRAM: two files
    Snakemake never declared and therefore could never clean, in the very tree whose undeclared temp
    files this release set out to make impossible. Bounded and idempotent is better than the sort's
    unbounded spill, but it is not the same claim as *nothing*. Now the pair lives for the length of
    one conversion and goes with it, and the only files left under `results/` are the rule's declared
    outputs. (In practice the branch is not taken at all — ``liulab-genome`` ships the ``.fai``
    beside the FASTA — which is exactly why it was worth closing rather than watching.)
    """
    if fasta.with_name(fasta.name + ".fai").exists():
        return fasta
    workdir.mkdir(parents=True, exist_ok=True)
    local = workdir / fasta.name
    if not local.exists():
        local.symlink_to(fasta)
    _run(["samtools", "faidx", str(local)])
    return local


#: Stage 2 of the pipe: every QNAME becomes ``r<N>``, counting the alignment lines as they go past.
#:
#: ``FS`` and ``OFS`` are both pinned to tab and both are load-bearing. awk rebuilds the whole record
#: the moment a field is assigned, joining it with ``OFS`` — whose default is a **space**. Leave it
#: unpinned and this hands the encoder a file whose fields are space-separated, which is not SAM at
#: all; the failure is total rather than subtle, but it is also not obvious from reading the program.
#:
#: ``/^@/`` is a sound header test rather than a convenient guess: the SAM specification restricts a
#: QNAME's first character to ``[!-?A-~]``, which excludes ``@`` (0x40) by construction. So no
#: alignment line can begin with one, and no header line can be missed.
#:
#: It also STAMPS the selection's caveat, because this is the one stage that already reads the header
#: and writes it back: a ``@CO`` line goes in where the header ends, which is the first alignment
#: line, or the end of a stream that has none. ``caveat`` arrives through ``-v`` and is EMPTY unless
#: the selection has something to say, so the default archive's program and argv are what they were.
_RENAME_QNAME = (
    r'BEGIN{FS=OFS="\t"} /^@/{print; next} '
    r'!stamped{if(caveat!="") print "@CO", caveat; stamped=1} '
    r'{n++; $1="r" n; print} '
    r'END{if(!stamped && caveat!="") print "@CO", caveat}'
)


def bam_to_cram(
    bam: Path,
    fasta: Path,
    out: Path,
    threads: int = 1,
    selection: RecordSelection = RecordSelection.primary,
) -> Path:
    """STAR's coordinate-sorted BAM -> ``out`` (CRAM) + ``out.crai``. Returns ``out``.

    Three stages in one pipe, so nothing intermediate is ever written to disk. Each was measured on
    one real lane (GSE208154 / SAMN29720279), and together they land 12% **below** the CRAM this
    shipped before it carried a barcode at all:

    1. ``samtools view -h`` carrying the ``selection``'s flags and expression — the records this
       archive is *of*. The default, ``primary``, is ``-F 0x100`` and is byte-for-byte what this
       function has always done: primary alignments only. A secondary record re-states a read we
       already have, at a locus we did not choose to believe. It measured −17.8% when it was the
       thing removing them; **since #205 it removes nothing**, because ``starsolo.smk`` passes
       ``--outSAMmultNmax 1`` and STAR no longer writes a secondary record for the sort to carry —
       the saving is real and is now taken one stage earlier, in the aligner, where it also buys back
       the sort budget and the wall-clock. The flag stays anyway, and deliberately: it is a cheap
       invariant rather than a load-bearing filter, and it is what makes this function's output
       independent of how STAR happened to be invoked. Do not delete it for having stopped firing
       (ADR-0023). A caller wanting a narrower archive — the mapped records, or one side of the
       mappability partition — names a :class:`RecordSelection` instead of growing a pipe of its own,
       which is the whole reason the argument exists.
    2. ``awk`` — the read-name rewrite, −16.2%. Illumina names are 38 characters
       (``K00125:217:HCL2YBBXY:8:2111:24637:43374``) and mean nothing once the barcode is a ``CB`` /
       ``UB`` tag: the name was only ever the join key back to R1, and R1 is now in the record.
       samtools has no flag for this — ``--output-fmt-option lossy_names=1`` was measured and saves
       exactly zero bytes — so the rewrite happens in the stream. ``awk`` is in the pinned
       ``align-rna`` image (it is what the measurement itself used), so this adds no dependency.
       It is also where the selection's caveat becomes a ``@CO`` header line, since this stage is
       already reading the header and writing it back: no reheader pass, no second file.
    3. ``samtools view -C -T`` — the CRAM encoder, unchanged, still not embedding the reference.

    Every stage is multi-threaded (``--threads``) so a fat node is actually used, and **every stage's
    exit status is checked**. A pipe reports only its last stage, and ``samtools view -C`` will
    encode a truncated stream and exit 0 — so a filter or a rewrite that died mid-file would become a
    CRAM quietly missing most of its reads. That is expensive here in particular: the BAM is a
    Snakemake ``temp()`` output, deleted the moment this rule reports success. The BAM is left in
    place; the caller owns its deletion.
    """
    if not bam.exists():
        raise CramError(f"{bam} is missing; the STAR run that should have written it did not")
    if not fasta.exists():
        raise CramError(f"reference FASTA {fasta} does not exist")
    out.parent.mkdir(parents=True, exist_ok=True)
    # The `.fai` fallback gets a directory that outlives nothing: it must survive the encode, which is
    # why the whole conversion happens inside the `with`, and it must survive nothing else, which is
    # why there is a `with` at all. See `_ensure_fai`.
    with tempfile.TemporaryDirectory(prefix="seqforge-cram-") as tmp:
        ref = _ensure_fai(fasta, Path(tmp))
        _encode(bam, ref, out, threads, selection)
    _run(["samtools", "index", "-@", str(threads), str(out)])
    return out


def _encode(bam: Path, ref: Path, out: Path, threads: int, selection: RecordSelection) -> None:
    """The three-stage pipe of :func:`bam_to_cram`, waited on and checked stage by stage."""
    # `-h` keeps the header: the encoder needs the @SQ lines to match sequences to the reference.
    # `-T` names that reference; no embed_ref -> smallest CRAM.
    nthreads = str(threads)
    select = ["samtools", "view", "-h", *_SELECT_ARGS[selection], "--threads", nthreads, str(bam)]
    caveat = _SELECT_CAVEAT.get(selection)
    rename = ["awk", *(["-v", f"caveat={caveat}"] if caveat else []), _RENAME_QNAME]
    encode = ["samtools", "view", "-C", "-T", str(ref), "--threads", nthreads, "-o", str(out), "-"]
    try:
        # Each stage hands its read end to the next and then drops this process's copy. Holding one
        # open would keep the upstream stage blocked on a full pipe forever if its consumer died,
        # instead of letting it see EPIPE and exit — and a hang is the one failure a checked exit
        # status cannot report. Both handoffs need it, not just the first: an encoder that dies on a
        # full disk mid-file would otherwise leave awk writing into a pipe nobody drains, and the
        # `with` block waiting on awk.
        with subprocess.Popen(select, stdout=subprocess.PIPE) as filt:
            assert filt.stdout is not None
            with subprocess.Popen(rename, stdin=filt.stdout, stdout=subprocess.PIPE) as renamer:
                filt.stdout.close()
                assert renamer.stdout is not None
                with subprocess.Popen(encode, stdin=renamer.stdout) as encoder:
                    renamer.stdout.close()
        # Reported together, in pipeline order. A stage that fails kills the ones upstream of it with
        # SIGPIPE (they show as -13), so naming only one of them would have to guess which nonzero
        # was the cause; listing them all says what happened without pretending to know.
        failures = [
            f"{name} exited {code}"
            for name, code in (
                (f"samtools view ({selection} records)", filt.returncode),
                ("awk (read-name rewrite)", renamer.returncode),
                ("samtools view (CRAM encode)", encoder.returncode),
            )
            if code
        ]
        if failures:
            raise CramError(f"{bam} -> CRAM failed: " + "; ".join(failures))
    except FileNotFoundError as exc:
        raise CramError(
            f"{exc.filename} is not installed; CRAM conversion needs samtools and awk"
        ) from exc


__all__ = ["CramError", "RecordSelection", "bam_to_cram"]
