"""STAR's command line for ``map/starsolo`` — **one owner, two consumers**.

`starsolo.smk` renders this into its `shell:` block and `e2e.run_starsolo` hands it to `subprocess`.
Before this module they were two hand-written argv that could not see each other, and they had
already drifted: the instrument named the four `soloCB`/`soloUMI` start/len flags directly, so it
could not run a `CB_UMI_Complex` chemistry at all, and it omitted **nine** flags the module ships —
including `--limitBAMsortRAM`, which is to say the memory instrument left out the flag that caps
STAR's memory.

The fix is not a guard that detects the drift. It is that there is no longer a second place to drift
*from*: the Snakefile's shell block is `STAR {params.argv}`, so a flag cannot be dropped there
because there is nowhere left to drop one from, and the instrument may differ only where a
measurement physically forces it (see :func:`starsolo_argv`'s ``sys_shell`` and ``out_sam_type``).

**This is the same move `starsolo.smk` already made for `h5ad`, `memory`, `qc` and `units`, and for
the same reason its header gives**: a Snakefile is not importable, so a closure written inside one
can never be unit-tested, only run. Every function below used to live in that file, reading a
module-level `SOLO` global; each now takes the block as an argument and has a test.

**The config keys are still derived, never declared.** `starsolo.smk` used to be the only place a
`solo` key was read, so scanning its source for `SOLO["..."]` recovered the composer's obligations
(`WorkflowModule.required_config`). Those reads live here now, so
:func:`~seqforge.workflows.argv_keys_read_by` walks this module's AST for them instead — which sees
every branch rather than the one a scan happens to take, and tells a subscript (the composer owes
this key) from a ``.get`` (the chemistry that has it emits it) exactly rather than by pattern.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

#: STAR's ``--outSAMtype`` as `starsolo.smk` ships it.
#:
#: **Coordinate-sorted rather than unsorted, and that is not tidiness.** STARsolo writes only the
#: cDNA mate into the BAM and the barcode lives solely in the other mate, so with no ``CB``/``UB``
#: tag the barcode is IRRECOVERABLY absent from the retained alignment — which is what made 920 GiB
#: of shipped CRAM unable to do any of the three things a retained alignment is for. STAR emits those
#: tags in the sorted BAM and nowhere else, so the sort is the price of the barcode. It is also a
#: refund: the finalize rule no longer re-sorts, and the resulting CRAM measured 12% SMALLER than the
#: barcode-less one it replaces (#198).
#:
#: The cost instrument may override it — that is the one axis `kb e2e-cost --out-sam-type` sweeps,
#: and it exists to price the gap between writing a BAM and not writing one. Nothing else may.
SHIPPED_OUT_SAM_TYPE: tuple[str, ...] = ("BAM", "SortedByCoordinate")

#: STAR's ``--genomeLoad`` as `starsolo.smk` ships it: ATTACH to the copy `rule load_genome` already
#: put in shared memory, and leave it there for the next mapping job (#379).
#:
#: A CONSTANT and not a per-attempt value, which is what separates it from :data:`SORT_CAP_SHELL`
#: below: the flag says which memory the index lives in and never how much of anything a job gets, so
#: nothing about a retry moves it and it rides the argv like every other module literal. Without a
#: shared copy each concurrent job loads its own — six droplet samples against a ~31 GB human index
#: is ~186 GB of index where ~31 GB would do — and a composed pipeline runs on ONE machine (ADR-0051),
#: which is both what makes the sharing possible and what makes the multiplication real.
#:
#: It is also why `--limitBAMsortRAM` stopped being optional: STAR's default of ``0`` means "reuse the
#: genome allocation", and under this mode there is no such allocation to reuse, so the run is refused
#: before the genome directory is read. The two flags are one decision.
#:
#: An INSTRUMENT overrides it, and that is the fourth axis :func:`starsolo_argv` permits — see
#: `e2e.run_starsolo`, which reaps STAR directly with no load rule to create the segment and no
#: handler to release one.
SHIPPED_GENOME_LOAD = "LoadAndKeep"

#: The CellRanger >=4 equivalence set, and **no others** (#198).
#:
#: The documented set (Kaminow, Yunusov & Dobin 2021). Without them we emit STARsolo-DEFAULT counts,
#: which are not comparable to published CellRanger matrices — a real problem for a corpus whose
#: point is comparability.
#:
#: ``--soloCellFilter EmptyDrops_CR`` is the only member whose correctness argument had a hole in it:
#: it is **Monte-Carlo** (10 000 ambient simulations by default), and a nondeterministic QC bundle
#: would be a genuine problem for a content-addressed compiler. It was measured before it was
#: adopted — bit-identical across repeats, thread counts and RNG seeds on a real 6.79M-barcode
#: matrix — and `e2e.run_cell_filter_determinism` keeps that true on every gate run rather than
#: believing it, which also covers the case nobody would otherwise notice: a future `align-rna`
#: bumping STAR under it.
#:
#: **Why these are literals and not KB keys** — the argument is ADR-0011's, and it is about what a
#: value VARIES WITH rather than what it is for: none of these differs between two chemistries, so
#: none belongs to the KB. The params gate requires the emitted key set to be EXACTLY
#: ``union(KB keys, processing keys)`` and `required_config` is COMPUTED from the source, so making one
#: a ``solo[...]`` subscript would OBLIGE all eleven starsolo specs to declare it.
#: ``--clipAdapterType`` was in this set until #355 and is exactly why the test has to be "varies with
#: what" and not "chosen for what": it was picked for parity like the four here, and its right value
#: still moves from one chemistry to the next, so it is a subscript now and the specs carry it.
#: Verified against the STAR 2.7.11b binary that every literal here is accepted for ``CB_UMI_Simple``
#: AND ``CB_UMI_Complex`` — this is the class of change that passes a 10x-only suite and breaks the
#: four Complex specs.
CELLRANGER_PARITY: tuple[str, ...] = (
    "--outFilterScoreMin",
    "30",
    "--soloUMIfiltering",
    "MultiGeneUMI_CR",
    "--soloUMIdedup",
    "1MM_CR",
    "--soloCellFilter",
    "EmptyDrops_CR",
)

#: What STAR writes into the retained alignment. **Not** part of the CellRanger-parity set above, and
#: the separation is deliberate: these shape the alignment we keep, not the counts, so calling them
#: CellRanger parity would be a claim nobody measured. They are hardcoded for the same OWNERSHIP
#: reason — none differs between two chemistries, so none belongs to the KB (ADR-0011).
#:
#: ``--limitBAMsortRAM`` is absent here because it is the one value that is *not* a constant: it is a
#: function of the memory the attempt was actually given, so :func:`starsolo_argv` takes it as an
#: argument and :data:`SORT_CAP_SHELL` carries it for the Snakefile. See `workflows.memory.bam_sort_ram`
#: for why passing it at all is not optional.
#:
#: ``--outSAMmultNmax 1`` earns its own paragraph, because it is the only member that changes WHICH
#: RECORDS come out rather than what each record carries or where it goes (#205). STAR emits every
#: alignment of a multi-mapping read and coordinate-sorts them all; `seqforge io cram` then discards
#: the secondaries with ``-F 0x100``. On the measured sample that was 198.8M records sorted against
#: 162.9M retained — ~18% of the sort spent producing bytes the very next rule deletes, paid in both
#: the sort budget and in wall-clock. ``nTrOutWrite = min(P.outSAMmultNmax, nTrOutSAM)`` writes only a
#: top-scoring alignment, which is the record the CRAM filter keeps.
#:
#: **The counts are untouched and the CRAM is not byte-identical**, which is the opposite of what #205
#: claimed and was checked against the STAR 2.7.11b source rather than its manual. Counts: the flag
#: appears ONLY in the SAM/BAM write path and the alignment-ordering code, in NO Solo counting file;
#: ``SoloFeature_addBAMtags`` keys CB/UB on the read index alone and the gene assignment is an
#: order-independent set union. The CRAM: for a read with ``NH > 1``, ``outSAMmultNmax != -1`` is
#: itself the trigger in ``ReadAlign_multMapSelect.cpp`` for partitioning ``trMult`` so max-score
#: alignments come first AND for marking ``trMult[0]`` primary instead of ``trBest`` — and ``HI`` is an
#: OUTPUT-ORDER index, so a multimapper's retained record now always carries ``HI:i:1``. Where several
#: loci tie on score it can also be a DIFFERENT one of them: ``trBest`` breaks the tie on the shorter
#: genomic span, the partition takes the first in window order. Both are top-scoring, so this changes
#: the tie-break and not the quality; ``NH`` still counts every locus and a uniquely mapping read is
#: bit-for-bit untouched. ``-F 0x100`` STAYS in `cram.py`, and do not "clean it up": it is now a cheap
#: invariant rather than a load-bearing filter, and an invariant is not deleted for the crime of
#: having stopped firing.
#:
#: ``--soloMultiMappers`` is deliberately ABSENT (it stays ``Unique``): 87% of the multi-gene signal on
#: the measured library was the tandem rDNA array, EM splits identical copies evenly and emits a large
#: arbitrary number that reads as data, and all four multimapper matrices are FRACTIONAL, which breaks
#: pseudobulk. The diagnostic that would justify revisiting it (``Features.stats`` MultiFeature)
#: already ships in every QC bundle.
SAM_WRITE_PATH: tuple[str, ...] = (
    "--outSAMmultNmax",
    "1",
    "--outSAMattributes",
    "NH",
    "HI",
    "AS",
    "nM",
    "CB",
    "UB",
)


#: The sort cap, as a snakemake **shell-template fragment** rather than an argv token.
#:
#: The one flag the Snakefile cannot receive through :func:`starsolo_shell_args`, and the reason is
#: measured rather than stylistic. `--limitBAMsortRAM` must escalate with the retry attempt, and
#: snakemake re-expands `resources:` per attempt but **not** `params:` — a `params:` callable is
#: expanded once, on attempt 1, and every retry reuses that value verbatim (traced against the pinned
#: 9.23.1; the argument is in `starsolo.smk`'s resources block). Nor can the placeholder be smuggled
#: through a params value: snakemake formats the shell template in a single pass, so braces arriving
#: *inside* a substituted value are never expanded at all — they would reach STAR as literal text.
#:
#: So it is concatenated into the shell template, where snakemake formats it. The flag name and the
#: resource name are spelled HERE, once; `starsolo.smk` references this constant and spells neither.
SORT_CAP_SHELL = "--limitBAMsortRAM {resources.bam_sort_ram_bytes}"


def cb_umi_geometry(solo: Mapping[str, Any]) -> tuple[str, ...]:
    """Where the CB and UMI live — and STARsolo spells this two different ways.

    A simple chemistry (10x) has one contiguous barcode, so a start/length pair locates it. A
    combinatorial one (SPLiT-seq, BD Rhapsody) has barcodes scattered between linkers, so each needs
    a position quadruple and no start/length exists to give. This is not a preference: passing
    ``--soloCBstart`` to ``CB_UMI_Complex`` is an error, and the keys are absent from the config
    precisely because the chemistry has no such value. Compose emits whichever set the ``soloType``
    implies (the params gate proves the block is exactly what its owners declared), so the branch
    here reads what is there.

    GEOMETRY ONLY. This branch used to also pin ``--soloCBmatchWLtype 1MM`` on the Complex side; that
    key is now the KB's (#198) and is emitted once, for BOTH branches, by :func:`starsolo_argv`. The
    pin was load-bearing and its reason survives in the specs that inherited it — STAR REJECTS its
    own global default ``1MM_multi`` for ``CB_UMI_Complex``, so a Complex chemistry naming no match
    type FATALs on the default alone — but it could only ever state one value per ``soloType``, and
    Parse Evercode (``EditDist_2``) and BD Rhapsody (``1MM``) are both Complex and disagree. A branch
    that yields two answers cannot serve three chemistries; a per-chemistry file can.

    **The instrument could not take this branch until it lived here.** `e2e.run_starsolo` named the
    four simple-geometry flags directly, so a Complex chemistry was not merely unmeasured but
    unrunnable by it.
    """
    if solo["soloType"] == "CB_UMI_Complex":
        # `soloCBposition` is N space-separated quadruples, one per barcode element — compose emits it
        # with a `" ".join` in the whitelist's declared order, so it splits into N argv tokens exactly
        # as `--soloFeatures` does. Passed whole it would be ONE quoted argument, and STAR would read a
        # three-barcode chemistry as having a single, unparseable position. `soloUMIposition` is one
        # quadruple and splits to one token.
        return (
            "--soloCBposition",
            *str(solo["soloCBposition"]).split(),
            "--soloUMIposition",
            *str(solo["soloUMIposition"]).split(),
        )
    return (
        "--soloCBstart",
        str(solo["soloCBstart"]),
        "--soloCBlen",
        str(solo["soloCBlen"]),
        "--soloUMIstart",
        str(solo["soloUMIstart"]),
        "--soloUMIlen",
        str(solo["soloUMIlen"]),
    )


def barcode_read_length(solo: Mapping[str, Any]) -> tuple[str, ...]:
    """``--soloBarcodeReadLength``, and ONLY when the chemistry declares it.

    STARsolo's default (1) FATALs unless the barcode read is exactly CB+UMI long. 10x v2/v3/v3.1 R1
    is routinely sequenced longer than the 26/28 nt the barcode occupies (a 150 nt R1 is common), so
    their specs set ``soloBarcodeReadLength: 0`` to disable that check and read CB/UMI from the fixed
    offsets. A chemistry that does not set the key (SPLiT-seq, ...) keeps STAR's default, so the flag
    is emitted iff it is present.

    ``.get``, deliberately NOT a subscript: a subscript would make
    :func:`~seqforge.workflows.argv_keys_read_by` mark ``solo.soloBarcodeReadLength`` a REQUIRED
    config key, and the composer would then be obliged to emit it for every starsolo chemistry —
    including SPLiT-seq, whose params gate forbids emitting a key it does not own.
    """
    value = solo.get("soloBarcodeReadLength")
    return () if value is None else ("--soloBarcodeReadLength", str(value))


def read_through_clip(solo: Mapping[str, Any]) -> tuple[str, ...]:
    """The chemistry's read-through, as the flag STAR takes — or NOTHING where it declares none.

    **ONE value, because STARsolo aligns ONE mate.** ``--readFilesIn`` hands this module two files,
    cDNA first and then the barcode read, but the barcode read is not a mate: solo peels it off and
    only the cDNA read reaches the aligner. Measured against the pinned 2.7.11b at parameter init,
    under BOTH soloTypes, a second value — even ``-``, STAR's per-mate no-clip sentinel — is the hard
    ``--clip3pAdapterSeq has to contain 1 values to match the number of mates``. So this module's
    arity is a fixed 1 where `map/star-umi`'s is its cell's mate count, and the two land on opposite
    answers from the same rule. It is also what puts the clip out of the barcode read's reach
    STRUCTURALLY: there is no mate to aim at it, hence no sentinel to get wrong and no way to trim a
    CB or a UMI.

    ``--clip3pAdapterMMp`` is deliberately NOT restated, and that is the same measurement: STAR's own
    default is a single 0.1, which already matches this arity, so naming it would be a flag that
    reads as a decision and is the default. `map/star-umi` restates it because its paired form makes
    the default's arity WRONG, never because the number was worth saying.

    Read with ``.get``, so absence renders as ABSENCE — an empty flag is one STAR takes and matches
    against every read — and so the key stays one only the chemistry that has it emits.

    Every base it reaches is cDNA by compose's doing rather than this rule's: ``--readFilesIn`` is
    ordered by ROLE and the params gate re-checks that placement.
    """
    sequence = solo.get("read_through")
    return ("--clip3pAdapterSeq", str(sequence)) if sequence else ()


def clip_adapter(solo: Mapping[str, Any]) -> tuple[str, ...]:
    """``--clipAdapterType``, plus whichever clip the chemistry declares at the end that trimmer takes.

    One fragment for up to three flags, because STAR makes them one decision. ``CellRanger4`` builds
    two fixed adapters — the 10x three-prime TSO off the cDNA read's 5' end and poly-A off its 3' —
    and a supplied ``--clip5pAdapterSeq`` REPLACES the first rather than adding to it, read off
    STAR's own source in `docs/research/starsolo-read-preprocessing-per-family.md`. So a chemistry
    whose protocol clips a DIFFERENT TSO says so once and gets its own sequence clipped, under the
    only mode where the override is legal at all. The other mode is the mirror: ``Hamming`` takes no
    five-prime sequence and is the only one that accepts a three-prime one, which is why a
    ``read_through`` and an override are never both here — the schema refuses the pairing at spec
    load, one rule for both directions.

    The trimmer is the KB's and not this module's (#355), because its correct value MOVES from one
    chemistry to the next — the ownership argument is on `Backend` in `kb/schema.py`, the per-vendor
    evidence in the research file above. ``Hamming`` with nothing declared is STAR's default and a
    no-op, which is exactly what the 5' and BD vendors do to a cDNA read; this module used to hand
    all eleven chemistries ``CellRanger4`` and clip a 10x TSO off four reads that never carried one.

    ``clipAdapterType`` is a SUBSCRIPT and both clips are ``.get``, and that difference is the whole
    mechanism: the subscript makes the key REQUIRED, so all eleven specs owe a value and none is
    defined by silence, while a clip stays a key only the chemistry that has one emits and the params
    gate polices.
    """
    flags: list[str] = ["--clipAdapterType", str(solo["clipAdapterType"])]
    override = solo.get("clip5pAdapterSeq")
    if override:
        flags += ["--clip5pAdapterSeq", str(override)]
    return (*flags, *read_through_clip(solo))


def adapter_sequence(solo: Mapping[str, Any]) -> tuple[str, ...]:
    """``--soloAdapterSequence``, and ONLY when the chemistry declares it (an ANCHORED bead).

    BD Rhapsody Enhanced prepends a variable 0-3 bp diversity insert to the barcode read, so the
    CB/UMI offsets float. STARsolo absorbs the stagger by anchoring to this adapter
    (``NNN...GTGANNN...GACA``): it finds the adapter in each read and reads the barcodes at the
    anchor-2/anchor-3 positions :func:`cb_umi_geometry` emits. Derived from the linker elements at
    compose time (`compose/params.py`) and present in ``config["solo"]`` only for such a chemistry —
    ``.get``, so a fixed-offset chemistry (10x, the original BD bead) neither declares it nor has the
    scanner mark it a required key.
    """
    value = solo.get("soloAdapterSequence")
    return () if value is None else ("--soloAdapterSequence", str(value))


def starsolo_argv(
    solo: Mapping[str, Any],
    *,
    genome_dir: str | Path,
    cdna: str,
    barcode: str,
    whitelist: str | Path | Sequence[str | Path],
    out_prefix: str,
    sample: str,
    threads: int,
    bam_sort_ram_bytes: int | None,
    out_sam_type: Sequence[str] = SHIPPED_OUT_SAM_TYPE,
    genome_load: str = SHIPPED_GENOME_LOAD,
    sys_shell: str | None = None,
    extra: Sequence[str] = (),
) -> tuple[str, ...]:
    """Every argument STAR is given, in order, **excluding the binary itself**.

    The whole command line for this module lives here. `starsolo.smk` joins the result into one
    shell token and `e2e.run_starsolo` passes it to `subprocess` unjoined, so the module and the
    instrument cannot render different commands — there is only one rendering.

    ``cdna`` and ``barcode`` are pre-joined mate strings, not path lists: a pooled sample's runs and
    lanes are comma-joined per mate so it maps in one STAR pass, and getting that order wrong does
    not crash (see `workflows.units`, which owns the ordering and is the only thing allowed to
    decide it). cDNA FIRST, then the barcode read — asserted by compose's params gate.

    **The four parameters an instrument may differ on, and nothing else.**

    - ``out_sam_type`` — the axis `kb e2e-cost --out-sam-type` sweeps, to price the gap between
      writing an alignment and not writing one.
    - ``genome_load`` — the module ATTACHES to the copy `rule load_genome` put in shared memory
      (:data:`SHIPPED_GENOME_LOAD`), and an instrument reaping STAR directly has neither that rule
      nor the handler that releases the segment. Left shared, the first arm of a sweep would pay the
      load and every later arm would attach to it, which is a different measurement under the same
      name, and the segment would outlive the run.
    - ``sys_shell`` — STAR runs its reader from a shebang-less script it writes itself, which execs
      only where libc retries through `/bin/sh`; glibc does, macOS does not. The instrument names a
      shell so the script gets a ``#!``; under snakemake the job already has one.
    - ``extra`` — trailing arguments a measurement adds to itself, never a claim about what ships.

    Everything else is shared by construction. That is the fix for #348: the instrument used to omit
    nine flags, and the omission was nobody's decision — the argv was written once and the module
    grew past it.

    ``whitelist`` takes one path or several: 10x names one, a split-pool chemistry names three, and
    STAR reads them as N values of a single flag.

    ``sample`` is the library the alignment came from, and it is the only value on this command line
    that names the DATA rather than the recipe — hence a parameter, where every other decision here
    is a module literal. It becomes ``--outSAMattrRGline ID:<sample> SM:<sample>``, and an instrument
    owes one too: a read group is not a flag a measurement may drop, because dropping it is exactly
    the drift ADR-0049 closed. ONE line whatever the sample's file count, because STAR replicates a
    single ``RG`` entry across every comma-joined input file and refuses any other count than 1 or N
    (`Parameters_readFilesInit.cpp`); a pooled sample is one library either way.

    ``bam_sort_ram_bytes=None`` omits the sort cap, and only the Snakefile path may pass it: that one
    flag has to escalate per retry attempt, so it reaches the shell as :data:`SORT_CAP_SHELL` instead.
    An instrument passes the number — omitting it is what left the memory instrument running without
    the flag that bounds STAR's memory.
    """
    onlists = (whitelist,) if isinstance(whitelist, (str, Path)) else tuple(whitelist)
    return (
        "--runMode",
        "alignReads",
        "--genomeDir",
        str(genome_dir),
        "--runThreadN",
        str(threads),
        "--genomeLoad",
        genome_load,
        "--readFilesIn",
        cdna,
        barcode,
        "--readFilesCommand",
        "zcat",
        *(() if sys_shell is None else ("--sysShell", sys_shell)),
        "--soloType",
        str(solo["soloType"]),
        *cb_umi_geometry(solo),
        *adapter_sequence(solo),
        *barcode_read_length(solo),
        "--soloCBwhitelist",
        *(str(w) for w in onlists),
        "--soloCBmatchWLtype",
        str(solo["soloCBmatchWLtype"]),
        "--soloStrand",
        str(solo["soloStrand"]),
        # --soloFeatures takes N space-separated values; STAR writes one Solo.out/<feature>/ per value.
        "--soloFeatures",
        *str(solo["soloFeatures"]).split(),
        *clip_adapter(solo),
        *CELLRANGER_PARITY,
        "--outFileNamePrefix",
        out_prefix,
        "--outSAMtype",
        *out_sam_type,
        *(() if bam_sort_ram_bytes is None else ("--limitBAMsortRAM", str(bam_sort_ram_bytes))),
        *SAM_WRITE_PATH,
        # The read group. NOT a member of :data:`SAM_WRITE_PATH` even though it belongs to the same
        # write path, for the one reason that keeps that tuple a tuple: its value moves from one
        # sample to the next, so it cannot be a constant.
        #
        # `--outSAMattrRGline` is STAR's ONLY input to an `@RG` header line, and setting it also
        # appends `RG` to the output attribute order on STAR's own initiative — `RG` is not a word
        # `--outSAMattributes` accepts, so there is no second way to ask for the tag and no way to
        # get the line without it. Header and tag therefore arrive together or not at all, which is
        # the SAM rule this pays: a record's `RG` must name a group the header introduced. Until now
        # this route's records named none at all — valid, and refused by the GATK family for having
        # no library provenance, which is a retained CRAM nobody downstream can re-call from.
        "--outSAMattrRGline",
        f"ID:{sample}",
        f"SM:{sample}",
        *extra,
    )


def starsolo_shell_args(solo: Mapping[str, Any], **kwargs: Any) -> str:
    """:func:`starsolo_argv`, quoted for a ``shell:`` block — **without** the sort cap.

    `snakemake -n -p` *formats* a shell block and never runs one, so arity and quoting in a rendered
    command line are unguarded by construction (the argument is `workflows.units`'). Joining here
    rather than in the Snakefile is what collapses that exposure to a single tested call.

    The cap is excluded because it cannot survive a params value; the Snakefile appends
    :data:`SORT_CAP_SHELL` to its shell template instead. Taking no ``bam_sort_ram_bytes`` argument at
    all is deliberate — a caller cannot pass one here and quietly get a cap that never escalates.
    """
    return shlex.join(starsolo_argv(solo, bam_sort_ram_bytes=None, **kwargs))


__all__ = [
    "CELLRANGER_PARITY",
    "SAM_WRITE_PATH",
    "SHIPPED_GENOME_LOAD",
    "SHIPPED_OUT_SAM_TYPE",
    "SORT_CAP_SHELL",
    "adapter_sequence",
    "barcode_read_length",
    "cb_umi_geometry",
    "clip_adapter",
    "read_through_clip",
    "starsolo_argv",
    "starsolo_shell_args",
]
