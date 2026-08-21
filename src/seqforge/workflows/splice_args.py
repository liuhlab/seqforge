"""STAR's junction decision — **one spelling, and every STAR module reads it from here**.

Until this file, every STAR **Workflow module** passed genome, threads, input, output, sort and read
group and *nothing about splicing*, so every dataset inherited STAR's junction defaults. Those
defaults are mammal-calibrated and the lab works in compact genomes, and at `--alignIntronMax 0` —
STAR's default — the gap check is dead code: the one comparison against it in the tree is guarded on
the value being non-zero, so what bounds a novel intron is emergent from window merging, and merging
is transitive up to the contig. The worm plate carries `N` gaps to 1,049,334 bp on an organism whose
longest annotated intron is 100,912 bp, held up by 9-13 bp **anchors** with 66-122 bp of adapter
soft-clip, and no uniqueness filter removes them — which is why they sit in files named
`.unique.cram`. The census and the STAR-source derivations behind every sentence above are #459's.

**One function, not a fourth `shell:` literal.** Three of the four modules spell their aligner argv
inline and `map/starsolo` already has an argv owner (`workflows/starsolo_args.py`); a fourth and
fifth hand-written copy of one flag list is exactly the drift that owner's record was written about,
where a list spelled twice drifted twice and nine flags went missing without anyone deciding. So the
three inline modules interpolate :func:`splice_shell_args` as ONE `params:` slot and
:func:`~seqforge.workflows.starsolo_args.starsolo_argv` calls :func:`splice_argv`. There is one place
left to drop a flag from and it is this file.

**Three of these vary with NOTHING, which is what makes them module literals.** The rule is the one
the plate module's read-through renderer already states: a value that differs between two chemistries
belongs to the chemistry's entry in the knowledge base, and a value that differs between none is the
module's. The intron cap is the fourth and it is neither — it varies with the ASSEMBLY — so it
arrives as an ARGUMENT to the function below rather than as a fifth spelling of the same list, from
the one place an assembly's facts are recorded: the recipe's genome reference, which the processing
policy filled from liulab-genome's shipped table (`manifest.policy.intron_length_cap`).

**The argument is required and its value may be `None`**, which is the difference between a module
that has no cap and a module that forgot to ask for one. A default would make the second silent, and
silence is precisely the failure this file exists to prevent: a slot the module has to place is a
slot the module can misplace, and four modules each have to place this one. Stated required, a module
that drops it dies where every plan in the suite renders, at parse time, rather than aligning a worm
against the contig.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from typing import Any

#: STAR's own ``Standard`` attribute set, restated because naming ANY attribute REPLACES the default
#: rather than adding to it — a module that asked for the junction pair alone would silently stop
#: writing `NH`, and our counter reads `NH` directly to tell a unique placement from a multi one.
#:
#: The three modules that spell no attribute list of their own get exactly this plus
#: :data:`JUNCTION_ATTRIBUTES`, which is STAR's default output with two tags added and nothing taken
#: away. `map/starsolo` spells its own list (it also carries `CB`/`UB`, which its extractor depends
#: on) and passes ``sam_attributes=None`` so this one does not compete with it.
STANDARD_SAM_ATTRIBUTES: tuple[str, ...] = ("NH", "HI", "AS", "nM")

#: What a spliced record has to carry for its junction to be checkable against the annotation AFTER
#: the run, and the cheapest item in this file: it is the only one that costs no alignments.
#:
#: ``jM`` is the intron motif and adds 20 to the value where the junction is ANNOTATED, which is the
#: whole point — the artifact population can otherwise only be BRACKETED (a floor from gaps longer
#: than any annotated intron, a ceiling from every short-anchor junction, and the honest answer
#: somewhere between them) because a junction cannot be told from a real one after the fact. ``jI``
#: carries the intron's 1-based coordinates, so the check needs no realignment and no index.
#:
#: Spelled ONCE, here, and read by `starsolo_args`'s own attribute list rather than restated in it:
#: the pair is this decision's, whichever route a module takes to STAR's command line.
JUNCTION_ATTRIBUTES: tuple[str, ...] = ("jM", "jI")


def splice_argv(
    *,
    intron_max: int | None,
    sam_attributes: Sequence[str] | None = STANDARD_SAM_ATTRIBUTES,
) -> tuple[str, ...]:
    """The junction flags every STAR module ships, in order, **excluding the binary and everything else**.

    Three filters, a length bound and an attribute list, and the ordering below is the ordering of how
    much discrimination each buys — which is not the order intuition suggests.

    ``--outFilterType BySJout`` does the most work and is organism-independent. Under STAR's default
    ``Normal`` the junction filters (`outSJfilterOverhangMin`, 12 bp for a canonical GT/AG motif, and
    `outSJfilterIntronMaxVsReadN`) govern the junction SIDE FILE alone; under ``BySJout`` an alignment
    carrying a junction that failed them is rejected in the second stitching pass and STAR falls back
    to its next-best placement. Every junction in the observed population fails both. It also makes
    the side file and the retained archive agree about which junctions exist, which is the blind spot
    the plate's junction QC has today — it summarises the side file, and a junction on an 8 bp
    overhang never reaches it while the alignment sits in the BAM.

    ``--alignSJoverhangMin 8`` with ``--alignSJDBoverhangMin 1`` is ENCODE's pair, taken because it is
    published and widely run rather than because either number was measured here. Raising the anchor
    a NOVEL junction needs while lowering what an ANNOTATED one needs moves real splicing onto the
    exempt path, which is what makes the pair protective rather than merely stricter, and with a GTF
    in the index nearly every real junction is annotated. **Recorded honestly: 8 does not by itself
    reach the observed anchors.** STAR's stitching test is ``< min + shift``, so at a repeat shift of
    0 an exactly-8 bp anchor still passes, and the census found anchors from 8 to 21 bp. ``BySJout``
    is what closes that gap, since the junction filter it enforces uses the 12 bp threshold above.

    ``intron_max`` is the assembly's, and it is the BACKSTOP rather than the fix: it excludes the
    structurally impossible, while the anchor filters above do the discriminating. It is deliberately
    a loose round number with a recorded rationale and never derived from an annotation — a catalogue
    of transcripts someone observed is a FLOOR on what biology does, so reading a ceiling off its
    longest entry is category-incorrect however carefully computed, and it fails silently in the
    tight direction when it is computed wrong. On an intron-free component it cannot bind at all,
    which is the clearest demonstration available that it is not the fix.

    **Both flags or neither, and never one.** ``--alignIntronMax`` alone leaves the mate gap
    uncapped while still redefining STAR's window binning as a side effect — STAR's own source
    carries an `ISSUE - to be fixed in STAR3` comment on exactly that — so a mate pair could still
    span what a single read may not. That side effect is the second-order benefit and worth the
    record: the cap re-derives `winBinNbits` (50,000 -> 14), which drops the per-step window reach
    from ~589,824 bp to ~147,456 bp, so a tight cap SUPPRESSES the transitive window merging that
    fabricates the gap rather than only bounding the gap it fabricated.

    ``None`` renders neither flag, which is an assembly the lab has not characterised: an unfilled
    row must change nothing rather than impose a number nobody chose.

    ``sam_attributes`` is what this module writes BESIDE :data:`JUNCTION_ATTRIBUTES`, defaulting to
    STAR's own four because naming an attribute replaces the default set rather than extending it.
    ``None`` renders no attribute list at all, for the one module that states its own — it appends
    the junction pair to that list instead, so the pair is spelled once here either way.

    Six ENCODE flags that travel with the two overhang minimums are deliberately NOT adopted:
    `outFilterMultimapNmax` would redefine the unique/multi split the two retained archives are a
    partition of, the mismatch pair interacts with what the Tn5 read-through clip does to the
    read-length denominator and needs its own measurement, `alignIntronMin` is a 1 bp move and inert
    here, and `sjdbScore` points the wrong way — it is the bonus for crossing an ANNOTATED junction,
    so lowering it makes annotated junctions less competitive against phantom novel ones.
    """
    return (
        "--outFilterType",
        "BySJout",
        "--alignSJoverhangMin",
        "8",
        "--alignSJDBoverhangMin",
        "1",
        *(
            ()
            if intron_max is None
            else (
                "--alignIntronMax",
                str(intron_max),
                "--alignMatesGapMax",
                str(intron_max),
            )
        ),
        *(
            ()
            if sam_attributes is None
            else ("--outSAMattributes", *sam_attributes, *JUNCTION_ATTRIBUTES)
        ),
    )


def splice_shell_args(**kwargs: Any) -> str:
    """:func:`splice_argv`, quoted for a ``shell:`` block — the ONE `params:` slot an inline module gets.

    `snakemake -n -p` *formats* a shell block and never runs one, so arity and quoting in a rendered
    command line are unguarded by construction. Joining here rather than in three Snakefiles is what
    collapses that exposure to a single tested call, and it is why the three inline modules take one
    slot rather than one per flag: a slot the module has to place is a slot the module can misplace.

    Everything is forwarded, so ``intron_max`` stays required through here too: a module calling this
    with no arguments raises where its `params:` block is evaluated, which is when the Snakefile is
    parsed and therefore in every plan the suite renders.
    """
    return shlex.join(splice_argv(**kwargs))


__all__ = [
    "JUNCTION_ATTRIBUTES",
    "STANDARD_SAM_ATTRIBUTES",
    "splice_argv",
    "splice_shell_args",
]
