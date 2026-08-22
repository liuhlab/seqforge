"""One chimera-mapped BAM into one BAM per **Component**, each spelled the way a single-assembly run
would have spelled it.

A **Chimera** is one reference built from several component assemblies, whose chromosome names carry
a ``<separator><component>`` suffix so that every read declares which organism it landed on. That
suffix is what makes the alignment interpretable and what makes every artifact downstream of it
unusable: ``chrI__ce11`` is not ``chrI``, so a browser, a counter and whatever script the user
already owns all refuse it, and a retained CRAM inherits the misspelling permanently. This module is
the undo — one pass over the chimeric BAM, one output per component, each carrying the chromosome
names, the ``@SQ`` order and the lengths its own assembly declares.

**The keep rule is one sentence: a mapped, primary alignment.** Three discard categories fall under
it — unmapped, secondary, supplementary — and each is counted separately even though only the first
can occur under the flags the aligner runs with today. Counting them apart is what lets the rule
degrade legibly when a flag changes: a category that starts firing says so in the summary, instead
of quietly moving reads. Unmapped in particular is DROPPED rather than passed through — such a
record's ``RNEXT`` still names its mate's *suffixed* chromosome, so rewriting ``RNAME`` alone would
leave the pointer aimed at a sequence this output's header does not contain — and dropping it is why
a chimeric object's unmapped fate reads structurally zero, with this summary the only place a
chimeric run reports the number at all.

**A fragment placed at more than one locus is NOT a discard category.** It is routed by its
representative record's Component and kept, MARKED rather than dropped: the record already carries
the aligner's hit-count tag, and that tag is what the counter downstream reads. Separating the two
populations therefore costs no intermediate artifact here and puts no Chimera concept there. What is
lost by keeping them is nothing that was ever measured — a read ambiguous across organisms is
indistinguishable from a within-organism repeat on one emitted record, so the Component a
multiply-placed record is filed under is its representative's and says nothing about the rest of the
locus set.

**The unit is the record, and nothing is held.** Both mates of a template carry the same ``NH`` (it
counts placements of the template, not of one mate) and sit on the same chromosome, so a stateless
per-record filter keeps a template together by construction. No name sort, no buffer, and a
ten-million-record BAM streams.

**A pair is not always whole, and the survivor is kept.** Where only one mate aligned, the record
that did carries the mate-unmapped flag and has no partner beside it in the output: a **singleton**,
by construction, since an unmapped mate is not in the file as an alignment. It is real evidence, and
discarding it would cost the rarer organism most — its fragments are shorter and far more
soft-clipped, and its share is the number a chimeric run exists to produce. So a singleton is kept,
counted on the mate side it was kept on, and subtracted from that side before first and second mates
are compared.

**The work is a pure function of ``(bam, outputs, separator)`` and nothing else** — no ``Genome``
call, no reference FASTA, no ``chrom.sizes``. Names, order AND lengths all come off the BAM's own
``@SQ`` block, which is the copy that actually placed these records: a sizes table that drifted
under a retained BAM therefore cannot break a split. It also collapses the refusal for an
unexplainable chromosome into ONE up-front header check rather than a raise from inside the record
loop, because the whole reference is known before the first record is read. Resolving an assembly to
``(components, separator)`` off the chimera's completion record is the CLI verb's job, exactly as
``io cram`` resolves the reference FASTA outside ``workflows/cram.py``; this module therefore stays
strictly typed and unit-testable with no built reference anywhere on disk.

**What each output's header promises, and what it does not.** ``@SQ`` is filtered to this component,
suffix-stripped, in the order the chimeric header had it; ``@HD`` passes through untouched; the
aligner's ``@PG`` and ``@CO`` survive verbatim and this module appends its own ``@PG``. The BINARY
reference dictionary is rewritten as well as the text header — a record names its reference by index
into that dictionary, so a text-only rewrite would leave every record resolving to some other
chromosome, in range and silently wrong. What is deliberately NOT attempted is literal header
identity with a single-assembly run: the aligner's ``@PG …CL:`` and its ``@CO user command line:``
both embed ``--genomeDir``, so identity would mean writing down a command line nobody ran. The bar is
``@SQ`` plus ``@HD``, and the aligner's own lines keep saying truthfully what produced the alignment.

**Three runtime checks, because the facts underneath this design were read off the aligner's source
rather than watched on a real chimera.** The mate sits on this record's own component, checked per
record. Each output's PAIRED REMAINDER balances — first and second mates, each less that side's own
singletons — checked once at the end. And the half-mapped population is BOUNDED from the other end:
asked to emit what it could not place, the aligner writes a dead mate as a placeless record flagged
mate-mapped, so a UNIQUELY placed survivor is answered by exactly one such record and the placeless
count may fall short only by however many survivors were placed at more than one locus. None of the
three is here to catch a bug in this module — the first turns an opaque dictionary lookup failure
into a refusal that names the read and both components, the second turns a silently halved output
into one that says so, and the third costs three more counters, no buffer, and is strictly stronger
than comparing raw mate counts was, since a whole population disappearing from the file would leave
those counts balanced.

**The third check is a two-sided BOUND on totals, and a real chimera moved it three times.** It was
written as an equality per Component, and a pilot cell refused on it while counting 5440 half-mapped
fragments from either end — survivors attributing 4929/511 to the two organisms and mate pointers
4928/512, with all 90 of the fragments whose two ends named different contigs placed at more than
one locus. Moved to the total, it refused again: 33026 survivors against 33027 placeless records,
the one extra belonging to a fully mapped three-locus pair that ALSO left a placeless copy of its
second mate in the file, pointing at a contig neither of its own alignments touched. Left asserting
the other direction, it refused a third time, on the other sign: three cells of a 784-cell plate came
up exactly one placeless record SHORT of their survivors, and each shortfall was a survivor whose own
hit count was above one and whose dead mate is nowhere in the file, against 52366 placeless records
the aligner plainly did write. All three are one fact — **a per-fragment correspondence between the two
ends does not survive multi-locus emission** — and the third is the exact mirror of the second: one
emission policy, an EXCESS where a locus that was emitted left a dead half behind, a DEFICIT where a
locus that was not emitted took its dead half with it. So only a uniquely placed survivor is owed a
placeless record, and what is asserted is that the shortfall may not exceed the survivors carrying a
hit count above one. Filtering the multiply-placed out of the comparison is not available here — the
dead record carries a hit count of zero and cannot say what its fragment's was, and recovering it
means holding templates, which this module may not do — so what is available is that population's
SIZE, and it bounds the shortfall rather than removing it from either count. A fixed tolerance was
the alternative and it is a number nobody can read afterwards: both differences are counted and
reported instead, because they are what a reader subtracts before calling either end a count of
half-mapped fragments, and the per-Component pair is likewise a MEASUREMENT, its gap a lower bound
on the half-mapped fragments whose loci span two organisms. The bound still catches what the check
exists for: an aligner never asked to emit unmapped records counts zero placeless records against a
survivor population that multiply-placed survivors are only ever a fraction of.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from genome.chimera import ChimeraNamingError, split_suffixed

from .. import __version__
from . import WORKFLOW_VERSION
from .metrics import Metric, SampleStats, fraction
from .metrics import count as count_metric

if TYPE_CHECKING:  # pragma: no cover — a runtime dep; keeps the import cost off compose
    from pysam import AlignedSegment, AlignmentHeader

#: What one split's summary is called, under the cell's own directory. **Public because more than one
#: rule names it**, which is the line ``fragments.QC_SUFFIX`` already draws: the rule that writes it
#: and the rule that folds it into that cell's QC bundle both declare the path by importing this. A
#: second spelling anywhere is the one that fails in silence — a consumer that finds nothing looks
#: exactly like a split that never ran, so nothing raises and nobody is told.
#:
#: The file itself is reclaimed once the bundle carries it, so what OUTLIVES a run is the payload
#: :func:`split_metrics` reads out of that bundle rather than this file.
SPLIT_SUFFIX = ".split.json"

#: Why a record was not kept, in the order the keep rule tests for them. Public because it is the
#: summary payload's key space: a reader that meets a reason it does not know is reading a newer
#: split, and one that misses a reason is reading a payload written before that reason existed.
DROP_REASONS = ("unmapped", "secondary", "supplementary")

#: The ``@PG`` record this module appends. One identifier, spelled once, because it is also what a
#: later reader would look the line up by.
_PROGRAM_ID = "seqforge-split-chimera"


class SplitError(RuntimeError):
    """The BAM cannot be split as asked: an unexplainable chromosome, a partial request, a torn pair."""


@dataclass(frozen=True)
class SplitStats:
    """What one split did: what came in, what each Component kept, and why the rest was dropped."""

    #: The separator the names were actually read at. Carried because every count beside it is only
    #: interpretable against it — a component whose own chromosome names hold a doubled underscore
    #: forces a longer run, and the artifact outlives the assembly argument that decided which.
    separator: str
    records_in: int
    kept: dict[str, int]
    #: First and second mates per output, kept apart rather than summed. What is checked is their
    #: PAIRED REMAINDER — each less its own singletons — and neither half of that is readable once
    #: the two are added together.
    read1: dict[str, int]
    read2: dict[str, int]
    #: Kept records the aligner placed at more than one locus, per output. A SHARE of ``kept``
    #: rather than a category beside it: these records are in the BAM, marked by the hit-count tag
    #: they carry, and this is the number that says how much of a Component's kept signal is
    #: ambiguous without anyone re-reading the file to find out.
    multiplaced: dict[str, int]
    #: Kept records whose mate did not align, per output — a singleton has no partner in the file,
    #: which is why first and second mates may legitimately differ. Its own line rather than folded
    #: into a drop category, because nothing was dropped: the survivor is evidence and was kept.
    singletons: dict[str, int]
    #: The same population counted from the other end: placeless records whose mate DID align, filed
    #: under the Component their MATE POINTER names. A MEASUREMENT beside ``singletons``, never a
    #: second spelling of it — neither the per-Component split nor the total is asserted equal to it,
    #: because a fragment placed at more than one locus has no Component, its dead mate may point at
    #: a different member of the locus set than the emitted alignment took, and a member whose
    #: alignment WAS emitted can still leave a dead half behind. The gap between the two
    #: attributions is a lower bound on the half-mapped fragments whose loci span two organisms.
    mate_pointed: dict[str, int]
    #: How far the placeless records run BEYOND the survivors, each uniquely placed one of which is
    #: owed exactly one — dead halves left behind by a locus whose alignment WAS emitted, which is a
    #: multi-locus emission artifact and nothing else. Zero on a library with no multiply-placed
    #: fragment, and it is the number to subtract
    #: before calling either ``singletons`` or ``mate_pointed`` a count of half-mapped fragments.
    #: Its own line rather than a tolerance swallowed inside the check, because a tolerance is a
    #: number nobody can read afterwards. The placeless population it was measured against is
    #: ``singletons`` plus this less :attr:`unanswered_survivors`, and that is deliberately not a key
    #: of its own: a record flagged mate-mapped whose pointer is unset counts toward the bound and
    #: cannot be attributed, so ``mate_pointed`` may sum lower than the population — one subtraction
    #: away, and no BAM an aligner writes has yet made it non-zero.
    excess_pointers: int
    #: The same difference on the other sign: survivors with no placeless record left to answer them.
    #: A count of its own rather than a negative ``excess_pointers``, because at most one of the two
    #: can be non-zero on one BAM and a column reading "excess: -1" is a page arguing with its own
    #: heading — and because every reader of these payloads takes a count as non-negative, including
    #: the summaries already on disk, which were written when this direction was still a refusal.
    #: Non-zero means the aligner emitted a representative alignment for a fragment whose other mate
    #: never aligned and wrote no unmapped record for that mate, which only multi-locus emission
    #: does; above :attr:`multiplaced_singletons` it is not that, and the split refuses.
    unanswered_survivors: int
    #: Survivors carrying a hit count above one — the population that can lose its dead mate, and so
    #: the bound the shortfall above is checked against. Reported because a shortfall is unreadable
    #: without it: it is the difference between a cell that ran one under its slack and one that ran
    #: one under a slack of thousands, and neither the survivors nor ``multiplaced`` says which.
    multiplaced_singletons: int
    dropped: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        """The summary payload — what the verb prints AND what lands on disk, one shape.

        Every component that was asked for appears in every per-output account even at zero, and
        every reason appears in ``dropped`` even at zero: an absent key and a zero are different
        claims, and only one of them is a measurement. The seqforge version is stamped here rather
        than passed in, because it is provenance about the code that produced the numbers and is not
        something a caller can hold a stale copy of.

        Every kept count plus every drop count is exactly ``records_in``. ``multiplaced`` and
        ``singletons`` do NOT enter that sum — they are subsets of ``kept``, describing the records
        that are in the outputs rather than a fate that took records out of them. Nor does
        ``mate_pointed``, which is a share of the unmapped drop count and the one account here filed
        under a Component its records POINT at rather than one they sit on. Nor ``excess_pointers``
        and ``unanswered_survivors``, which are no fate at all but the two signs of one difference:
        add the first to the singletons and subtract the second, and the result is the placeless
        population the bound was actually checked against. At most one of the two is non-zero, and
        ``multiplaced_singletons`` beside them is not a difference but the bound the second is
        allowed to reach.
        """
        return {
            "seqforge": __version__,
            "separator": self.separator,
            "records_in": self.records_in,
            "kept": dict(sorted(self.kept.items())),
            "read1": dict(sorted(self.read1.items())),
            "read2": dict(sorted(self.read2.items())),
            "multiplaced": dict(sorted(self.multiplaced.items())),
            "singletons": dict(sorted(self.singletons.items())),
            "mate_pointed": dict(sorted(self.mate_pointed.items())),
            "excess_pointers": self.excess_pointers,
            "unanswered_survivors": self.unanswered_survivors,
            "multiplaced_singletons": self.multiplaced_singletons,
            "dropped": {reason: self.dropped[reason] for reason in DROP_REASONS},
        }


def parse_outputs(specs: Sequence[str]) -> dict[str, Path]:
    """``component=/path/to.bam`` arguments -> the outputs :func:`split_chimera` takes.

    One spelling of a path and not two: the component travels WITH the file it decides the contents
    of, so there is no second list for a caller to keep in the same order as the first. The same
    shape ``io umi-count`` takes its cells in, for the same reason.
    """
    outputs: dict[str, Path] = {}
    for spec in specs:
        # `marker`, not `separator`: in this module that word already means the underscore run a
        # Chimera's chromosome names carry, and two meanings one screen apart is one too many.
        component, marker, path = spec.partition("=")
        if not marker or not component or not path:
            raise SplitError(
                f"{spec!r} is not a `component=path` pair; each Component's BAM has to arrive with "
                f"the Component name that says what belongs in it"
            )
        if component in outputs:
            # A dict would keep the last silently, so one of the two files named would simply never
            # be written and nothing would say which.
            raise SplitError(
                f"{component} was given two output paths; only one of them can be written"
            )
        outputs[component] = Path(path)
    return outputs


def _split_name(name: str, separator: str) -> tuple[str, str]:
    """One suffixed chromosome name -> ``(chromosome, component)``, or the refusal it earns.

    The split is liulab-genome's and never ours. The suffix spelling is baked into a built reference
    and has exactly one owner, so a second implementation here — an ``rsplit``, a suffix test, a
    default separator — is a copy that can disagree with the thing that wrote the names, on the one
    input where nobody would look for a disagreement.
    """
    try:
        chromosome, component = split_suffixed(name, separator)
    except ChimeraNamingError as exc:
        raise SplitError(
            f"{name!r} does not split at {separator!r}, so nothing can say which Component it "
            f"belongs to. Either this BAM was mapped to something other than the Chimera named, or "
            f"that reference carries a chromosome nobody suffixed"
        ) from exc
    return chromosome, component


def _program(source: Mapping[str, Any], component: str, path: Path) -> dict[str, str]:
    """The ``@PG`` line this module appends: what wrote the file, and which workflow version did.

    ``PP`` chains it onto whatever the aligner left, so the header still reads as one ordered history
    rather than two unrelated claims. ``CL`` is reconstructed from this call's own arguments and is
    deliberately partial — the assembly that resolved the separator never reaches this module, which
    is why the separator itself is recorded in the summary instead of guessed at here.
    """
    previous = list(source.get("PG", []))
    entry = {
        "ID": _PROGRAM_ID,
        "PN": "seqforge",
        "VN": WORKFLOW_VERSION,
        "CL": f"seqforge io split-chimera {component}={path}",
    }
    if previous:
        entry["PP"] = str(previous[-1]["ID"])
    return entry


def _restored(
    source: Mapping[str, Any], component: str, path: Path, separator: str
) -> tuple[AlignmentHeader, dict[int, int]]:
    """This Component's header, plus the old-tid -> new-tid map its records are rewritten through.

    Everything the chimeric header carried survives untouched except ``@SQ`` — ``@HD``, the
    aligner's ``@PG`` and ``@CO``, and anything else a header may hold pass through by construction
    rather than by being enumerated here, so a section this module has never heard of is carried
    rather than dropped.

    The tid map is built in the SAME walk that filters ``@SQ``, which is what makes the two agree:
    a component's chromosomes keep the relative order the chimeric header had them in, and a
    record's new index is its position in that filtered list. Deriving the map a second way — by
    name lookup after the fact, say — is how a remap ends up in range and wrong.
    """
    import pysam

    sq: list[dict[str, Any]] = []
    tids: dict[int, int] = {}
    for old, entry in enumerate(source.get("SQ", [])):
        chromosome, owner = _split_name(str(entry["SN"]), separator)
        if owner != component:
            continue
        tids[old] = len(sq)
        sq.append({**entry, "SN": chromosome})
    header = {
        **source,
        "SQ": sq,
        "PG": [*source.get("PG", []), _program(source, component, path)],
    }
    return pysam.AlignmentHeader.from_dict(header), tids


def _rewritten(record: AlignedSegment, tids: Mapping[int, int]) -> AlignedSegment:
    """One kept record, copied, with its reference indexes moved into the restored dictionary.

    Nothing about the record changes except where its references POINT: the same tid means a
    different chromosome under the restored header, so the record's own index moves and its mate's
    moves with it. Everything else — the flag, the coordinate, the CIGAR, the sequence, the base
    qualities, the template length, and every aux tag at the width it was declared at — survives
    because it is never taken apart. A copy has no field list to fall behind the record's, and no tag
    is re-declared from its value, so nothing here can narrow an integer or widen an array.

    **An unset mate pointer is left exactly as it is.** ``-1`` is not a reference index but a record
    saying its mate went nowhere, and it is no key of the map; a copy arrives already carrying it, so
    the patch has to move a SET pointer and let that one alone — which is the opposite failure from
    the one a field-by-field rebuild had, where an unset pointer stayed unset by never being assigned.

    **What comes back is for a WRITER and for nothing else.** pysam will not let a record's header be
    reassigned, so the copy stays associated with the SOURCE file's header: call ``reference_name`` or
    ``to_string()`` on the returned object and the moved index is resolved against the chimeric
    dictionary, which answers with a suffixed name — and, the index having already moved, not even
    the one the record was read from. What lands on disk is
    right because :meth:`AlignmentFile.write` resolves names against the FILE's header — the record
    contributes the index, the output header contributes the dictionary — and the only thing between
    that and a bug is that the sole caller hands this straight to the writer for the Component whose
    map it was rewritten through. The copy also holds that source header by reference, so it stays
    readable after the source file is closed.

    Copied rather than rebuilt because the rebuild was 81.4% of this rule's wall time, and reading
    every aux tag back as ``(tag, value, type)`` tuples to re-declare it was 54.8% on its own. Method
    and tables: ``docs/research/split-chimera-record-copy-2026-08-21.md``, which cites the cluster
    collection the production figures came off.
    """
    out = copy.copy(record)
    out.reference_id = tids[record.reference_id]
    if record.next_reference_id >= 0:
        out.next_reference_id = tids[record.next_reference_id]
    return out


def _checked_request(
    source: Mapping[str, Any], outputs: Mapping[str, Path], separator: str
) -> None:
    """The one up-front check: every ``@SQ`` name splits, and every Component present was asked for.

    Both refusals are for a chromosome the reference cannot explain, and both are answerable from the
    header alone — which is why they are made once, here, instead of per record. A partial request in
    particular has to be refused rather than served: served, it writes exactly the file it was asked
    for, and reads-in-equals-reads-out stops closing with nothing to say it did.
    """
    present = {_split_name(str(entry["SN"]), separator)[1] for entry in source.get("SQ", [])}
    unnamed = sorted(present - set(outputs))
    if unnamed:
        raise SplitError(
            f"this BAM carries Components nobody named an output for: {unnamed}. A partial request "
            f"is refused rather than served — the reads on those chromosomes would go nowhere, and "
            f"the loss would read as a filter that had been asked for"
        )


def _write_summary(stats: SplitStats, path: Path) -> None:
    """The split's counts, on disk, as the last act of a split that finished.

    Last, and only on success, so the file's existence means the BAMs beside it were written whole. A
    summary left behind by a refusal would state a kept count reached at whatever record the refusal
    landed on — a number indistinguishable from a thin library, which is the failure this artifact
    exists to make visible. Trailing newline because a human opens this one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats.to_dict(), indent=2) + "\n", encoding="utf-8")


def split_chimera(
    bam: Path,
    outputs: Mapping[str, Path],
    separator: str,
    *,
    summary: Path | None = None,
    threads: int = 1,
) -> SplitStats:
    """One chimeric BAM -> one unindexed, coordinate-sorted BAM per requested Component.

    The outputs stay unindexed because neither consumer needs an index, and stay coordinate-sorted
    because the input is: records are written in the order they were read, and dropping records
    cannot disturb an order.

    ``summary`` is where the counts land, and handing over ``None`` skips the file — the split still
    happened and the caller still gets the numbers back, they simply do not outlive the process.

    **``threads`` buys BGZF codec threads and nothing else, because there is nothing else to buy.**
    The record loop is one stateless pass and stays on one core whatever this says — and the loop is
    where the wall-clock went, not the block compression underneath it. Measured on the
    implementation that rebuilt each kept record, the codec was 12.2% of this pass against the loop's
    81.4%, and it was that cheap only because it is threaded and overlaps behind a serial producer:
    take most of the loop away, as copying the record does, and the codec's share of a much smaller
    wall rises without anything having been bought. So the figure is DIVIDED across the outputs
    rather than handed to each of them:
    ``n`` writers each opening a pool of ``n`` is ``n²`` codec threads against an allocation of
    ``n``, and oversubscribing a scheduler that was told a number is worse than ignoring the number
    — which is the other failure available here, and the one this repo has already paid for once,
    on the rule that asked for threads and counted a whole plate on one core.

    The reader keeps the caller's figure whole. Decompressing one file is the pass's floor, it is
    strictly cheaper than the compression it feeds, and it cannot overlap itself.
    """
    import pysam

    if not bam.exists():
        raise SplitError(
            f"{bam} is missing; the alignment step that should have written it did not"
        )

    kept: Counter[str] = Counter()
    read1: Counter[str] = Counter()
    read2: Counter[str] = Counter()
    multiplaced: Counter[str] = Counter()
    # Singletons by the mate side they were kept on, because the remainder check subtracts each
    # side's own; their sum is the TOTAL the placeless population below is bounded against.
    singleton1: Counter[str] = Counter()
    singleton2: Counter[str] = Counter()
    # Of those singletons, the ones the aligner placed at more than one locus. One integer and not a
    # per-Component account, because the bound it feeds is on totals: only one representative of a
    # locus set is emitted, so a survivor with a hit count above one may be the only record of its
    # fragment in the file and its dead mate never written at all.
    multiplaced_singletons = 0
    # Placeless records whose mate DID align, counted twice over because two different questions are
    # being asked of one record. `pointers` is the POPULATION, decided on flags alone so that a
    # record whose mate pointer happens to be unset is still in it — undercounting here would break
    # the bound below and refuse a healthy cell. `dead_mates` is the per-Component ATTRIBUTION,
    # which only a record whose pointer resolves can enter at all. Every counter here is one integer
    # per output, so nothing grows with the input.
    pointers = 0
    dead_mates: Counter[str] = Counter()
    dropped: Counter[str] = Counter({reason: 0 for reason in DROP_REASONS})
    records_in = 0

    # Floored at one: a `--threads 0` from somewhere is a caller saying nothing, not a caller asking
    # for no codec at all, and htslib takes the difference badly.
    codec = max(1, threads)
    per_writer = max(1, codec // len(outputs)) if outputs else 1

    with pysam.AlignmentFile(str(bam), "rb", threads=codec) as source:
        header = source.header.to_dict()
        _checked_request(header, outputs, separator)
        # Which Component each reference index belongs to, resolved ONCE off the header. A record's
        # own Component is then a lookup rather than a string operation per record, and — since the
        # request has already been checked against this same list — every lookup below is total.
        owner = [_split_name(str(entry["SN"]), separator)[1] for entry in header.get("SQ", [])]
        # `restored`, never `plan`: `plan` is compose's compile verb everywhere else in this
        # repo, and one word for two unrelated things is how a reader loses a page.
        restored = {c: _restored(header, c, path, separator) for c, path in outputs.items()}
        for path in outputs.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        writers = {
            component: pysam.AlignmentFile(
                str(outputs[component]), "wb", header=restored[component][0], threads=per_writer
            )
            for component in outputs
        }
        try:
            for record in source.fetch(until_eof=True):
                records_in += 1
                if record.is_unmapped:
                    dropped["unmapped"] += 1
                    # ...and, on its way out, the other end of the half-mapped population. Asked to
                    # emit what it could not place, the aligner writes a dead mate with NO placement
                    # of its own -- `RNAME` is `*` -- and its live mate's chromosome in its MATE
                    # POINTER, which is the only field on it that names a Component. Read off a real
                    # chimeric BAM, not off the aligner's source: the first version of this read
                    # `RNAME`, counted zero against 5440 flagged survivors, and refused a healthy
                    # cell. A record whose mate is unmapped too points nowhere and answers no
                    # survivor, so it is not one of these. Membership is the FLAG test and the
                    # attribution is the nested one: what the bound compares must not turn on
                    # whether a pointer resolves, or a record that resolves nowhere would make a
                    # healthy population look short.
                    if not record.mate_is_unmapped:
                        pointers += 1
                        if record.next_reference_id >= 0:
                            dead_mates[owner[record.next_reference_id]] += 1
                    continue
                if record.is_secondary:
                    dropped["secondary"] += 1
                    continue
                if record.is_supplementary:
                    dropped["supplementary"] += 1
                    continue

                component = owner[record.reference_id]
                mate = record.next_reference_id
                if mate >= 0 and owner[mate] != component:
                    raise SplitError(
                        f"{record.query_name} is placed on {component} and its mate on "
                        f"{owner[mate]}: a template spanning two Components belongs to neither "
                        f"output, and assigning it to one would turn a cross-species ambiguity into "
                        f"a confident read"
                    )
                _, tids = restored[component]
                writers[component].write(_rewritten(record, tids))
                kept[component] += 1
                # No NH is one placement: the tag is the aligner's statement that a read went
                # somewhere else too, and its absence is not a missing measurement. It marks the
                # record rather than removing it, so this is a count of what is IN the output.
                multi = record.has_tag("NH") and int(record.get_tag("NH")) > 1
                if multi:
                    multiplaced[component] += 1
                if record.is_read1:
                    read1[component] += 1
                    if record.mate_is_unmapped:
                        singleton1[component] += 1
                if record.is_read2:
                    read2[component] += 1
                    if record.mate_is_unmapped:
                        singleton2[component] += 1
                # A survivor that may have no dead mate anywhere in the file. The mate-side test is
                # repeated rather than folded into the two branches above so that this population is
                # EXACTLY the one the bound is taken over — a record that is neither first nor second
                # mate is no singleton on either side, and one counted here that was not counted
                # there would let the shortfall be forgiven by a record that never entered it.
                if multi and record.mate_is_unmapped and (record.is_read1 or record.is_read2):
                    multiplaced_singletons += 1
        finally:
            for writer in writers.values():
                writer.close()

    singletons = {component: singleton1[component] + singleton2[component] for component in outputs}

    torn = {
        c: (read1[c] - singleton1[c], read2[c] - singleton2[c])
        for c in sorted(outputs)
        if read1[c] - singleton1[c] != read2[c] - singleton2[c]
    }
    if torn:
        raise SplitError(
            f"these outputs kept a different number of first and second mates once each side's own "
            f"singletons were subtracted, as component: (paired read1, paired read2) — {torn}. A "
            f"record whose mate did not align has no partner in this file and says so on its own "
            f"flag, so it is taken off its own side first; what is left is whole pairs, and both "
            f"mates of one carry a single hit count and sit on one chromosome, so a stateless "
            f"filter keeps them together. A remainder that does not balance means that assumption "
            f"is false for this aligner, and the split is not the thing to trust"
        )

    mate_pointed = {component: dead_mates[component] for component in outputs}
    survivors = sum(singletons.values())
    unanswered = max(0, survivors - pointers)
    if unanswered > multiplaced_singletons:
        raise SplitError(
            f"the fragments that half aligned were counted two ways and the placeless records fall "
            f"further short of the survivors than multi-locus emission can account for: "
            f"{survivors} survivors carrying the mate-unmapped flag against {pointers} placeless "
            f"records whose mate did align, {unanswered} short, where the shortfall may not exceed "
            f"the survivors placed at more than one locus and this run counted "
            f"{multiplaced_singletons} of those. "
            f"A survivor placed at ONE locus says on its own flag that its mate did not align, and "
            f"the aligner was asked to write out what it could not place, so exactly one such "
            f"record answers it; a survivor placed at several is owed nothing, because the "
            f"representative alignment that was emitted can be the only record of its fragment in "
            f"the file. Past that population the records are missing for some other reason — an "
            f"aligner never asked to emit them at all writes none, and this run wrote "
            f"{dropped['unmapped']} placeless records in total. The other direction is not refused "
            f"at all: a fragment placed at more than one locus can leave a placeless record behind "
            f"that no survivor is owed, so an EXCESS is expected on real data and is reported "
            f"rather than asserted away"
        )

    stats = SplitStats(
        separator=separator,
        records_in=records_in,
        kept={component: kept[component] for component in outputs},
        read1={component: read1[component] for component in outputs},
        read2={component: read2[component] for component in outputs},
        multiplaced={component: multiplaced[component] for component in outputs},
        singletons=singletons,
        mate_pointed=mate_pointed,
        excess_pointers=max(0, pointers - survivors),
        unanswered_survivors=unanswered,
        multiplaced_singletons=multiplaced_singletons,
        dropped=dict(dropped),
    )
    if summary is not None:
        _write_summary(stats, summary)
    return stats


def _counted(payload: Mapping[str, Any], key: str) -> int | None:
    """One non-negative integer out of a summary payload, or ``None`` for anything else.

    Absent is absent, and so is a value of the wrong shape: a number the artifact does not carry has
    to be missing from the page rather than rendered as a zero somebody acts on. That covers a
    summary written before a key existed and a hand-edited one, with the same answer for both — and
    it matters more here than it does for the extraction summary, because two of the counts below
    are *expected* to be zero on a healthy run and a fabricated zero would be indistinguishable.
    """
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


#: What each drop reason means for a reader, and why the number is where it is. Beside the reasons
#: rather than inline in the builder below, because the set is :data:`DROP_REASONS` and a hint list
#: that could go out of step with it by one is the drift the shared tuple exists to stop.
_DROP_HINTS: dict[str, str] = {
    "unmapped": "Records the aligner placed nowhere. Dropped rather than passed through — such a "
    "record's mate pointer still names a suffixed chromosome this output no longer declares — and "
    "this is the only place a chimeric run reports them, since they never reach a matrix. Those of "
    "them whose MATE did align answer the singletons kept beside each Component, seen from the "
    "other end and counted again as that Component's mate-pointed column: one for every uniquely "
    "placed singleton, more wherever a multiply-placed fragment left a dead half behind at a locus "
    "it did align at, which is the excess column, and fewer wherever one of those fragments took "
    "its dead half with it to a locus the aligner did not emit, which is the unanswered column.",
    "secondary": "Non-primary alignments of a read placed elsewhere too. Structurally absent under "
    "this pipeline's flags, so a number above zero means a flag moved rather than a library changed.",
    "supplementary": "Chimeric (split-read) alignment segments. Structurally absent under this "
    "pipeline's flags, like the secondaries above.",
}


def split_metrics(payload: Mapping[str, Any], sample: str) -> SampleStats:
    """One cell's split summary -> the columns its report row shows. Pure — no file, no pysam.

    **This is where a chimeric run's ``unmapped`` LIVES**, and that is what makes the adapter
    load-bearing rather than decorative. Those reads are dropped at the split, one rule before the
    counter, so that fate reads structurally zero in every per-Component matrix — a page rendering
    it from the counting object would be stating a falsehood about the data, since the reads existed
    and left earlier. They are counted here, by reason, and reported here.

    **Five metrics per Component, and each is a different question.** What it KEPT, and that as a
    SHARE of the records that came in: the share is the number the whole exercise exists to produce
    — the bacterial fraction of a well, readable without opening an ``.h5ad`` — and it is a share
    because only a share compares between two cells of different depths, while the count is what
    makes the page CLOSE, since every kept count plus every drop count is exactly the records that
    came in. Then how much of that was MULTIPLY PLACED, which is how much of the Component's kept
    signal a reader may not treat as one locus; and how many SINGLETONS it holds, records whose mate
    did not align, which is the population that used to make this whole step refuse.

    The fifth is the singleton count's other end — the MATE-POINTED records, dead mates whose
    pointer named this Component — and it earns a column on an axis this docstring otherwise argues
    for keeping narrow, because it is the only pair of numbers here that a reader must see TOGETHER.
    Apart they are one population counted twice and the second adds nothing; side by side their gap
    is a lower bound on the half-mapped fragments whose loci span two organisms, which no other
    column on this page, and no ``obs`` column in any matrix downstream of it, reports at all.

    **Two columns are the cell's and not a Component's, and they are the two signs of one
    difference.** The two ends are not one population once fragments are placed at more than one
    locus: a locus whose alignment WAS emitted can still leave a dead half in the file answering no
    survivor, which is the EXCESS, and a locus that was NOT emitted takes its dead half with it,
    leaving a survivor nothing answers, which is the UNANSWERED. What the split asserts is a
    two-sided bound and these are how far it ran either way — at most one of them is ever non-zero.
    Neither has a Component because the bound has none, and they are on the page rather than
    swallowed as a tolerance inside the check: a tolerance is a number nobody can read afterwards,
    and these are what a reader has to add to and subtract from the two columns above before calling
    either a count of half-mapped fragments.

    **Ungraded, every one of them.** Nobody has measured what share of a worm plate *should* be *E.
    coli*, so a bar here would be a figure invented at review — which is exactly what the module's
    membership in the cross-check silence set says out loud. And no ``headline``: the Component axis
    is N-wide by construction, so promoting it would put an unbounded number of columns in a strip
    whose whole job is being small.

    Each Component's share is neither a bound nor a clean assignment: multiply-placed records are
    kept and filed under their representative record's Component, so a read ambiguous across
    organisms is in exactly one Component's count with nothing on that record saying which others it
    could have been. The multiply-placed count beside it is how large that population is.
    """

    def account(key: str) -> Mapping[str, Any]:
        """One per-key mapping out of the payload, or an empty one — the same answer a summary
        written before that key existed and a hand-edited one both earn, since neither measured it.
        """
        value = payload.get(key)
        return value if isinstance(value, Mapping) else {}

    kept = account("kept")
    multiplaced = account("multiplaced")
    singletons = account("singletons")
    mate_pointed = account("mate_pointed")
    records_in = _counted(payload, "records_in")
    dropped = account("dropped")

    built: list[Metric | None] = []
    for component in sorted(kept):
        built += [
            count_metric(
                f"split_kept_{component}",
                f"{component} kept",
                _counted(kept, component),
                group="alignment",
                exact=True,
                hint=f"Alignment records that landed on {component} and were kept — mapped, primary, "
                f"however many loci the fragment was placed at. These plus every other Component's, "
                f"plus the drop counts, are exactly the records this cell's chimeric BAM held.",
            ),
            fraction(
                f"split_share_{component}",
                f"{component} share",
                (_counted(kept, component) or 0) / records_in if records_in else None,
                group="alignment",
                hint="The count beside this as a share of the records that came in — the figure that "
                "compares between two cells of different depths. Not a clean assignment: a read "
                "ambiguous across Components is filed under the one its emitted record landed on, "
                "and the multiply-placed count beside this says how many such records there are.",
            ),
            count_metric(
                f"split_multiplaced_{component}",
                f"{component} multiply placed",
                _counted(multiplaced, component),
                group="alignment",
                exact=True,
                hint=f"Of the records kept for {component}, those the aligner placed at more than one "
                f"locus — within this organism or across Components, with one emitted record unable "
                f"to say which. They are IN the output, marked by the hit-count tag they carry, so "
                f"the counter can separate them without a second file.",
            ),
            count_metric(
                f"split_singletons_{component}",
                f"{component} singletons",
                _counted(singletons, component),
                group="alignment",
                exact=True,
                hint=f"Of the records kept for {component}, those whose mate did not align and so "
                f"have no partner in this file. Kept, because they are real evidence and dropping "
                f"them would cost the shorter, more soft-clipped organism most — whose share is the "
                f"number this whole step exists to produce.",
            ),
            count_metric(
                f"split_mate_pointed_{component}",
                f"{component} mate-pointed",
                _counted(mate_pointed, component),
                group="alignment",
                exact=True,
                hint=f"The half-mapped fragments beside this one counted from the other end: "
                f"records the aligner placed nowhere whose own mate DID align, and whose mate "
                f"pointer names {component}. Over all Components this population may not fall short "
                f"of the singletons or the split refuses, and the excess column says how far it "
                f"runs over; per Component the two need not agree at all, because a fragment placed "
                f"at more than one locus has no organism — its dead mate can point at a different "
                f"member of the locus set than the alignment that was emitted. The gap against the "
                f"singleton count beside this is therefore a lower bound on the half-mapped "
                f"fragments whose loci span two organisms.",
            ),
        ]
    built += [
        count_metric(
            "split_excess_pointers",
            "Excess mate pointers",
            _counted(payload, "excess_pointers"),
            group="alignment",
            exact=True,
            hint="Placeless records whose mate DID align, beyond the singletons that are each owed "
            "one — dead halves belonging to fragments that aligned somewhere, left in the file by a "
            "locus of a multi-mapping set whose alignment was emitted. Zero unless the library "
            "holds multiply-placed fragments, and the number to subtract before reading the "
            "singleton or mate-pointed columns as a count of half-mapped fragments.",
        ),
        count_metric(
            "split_unanswered_survivors",
            "Unanswered survivors",
            _counted(payload, "unanswered_survivors"),
            group="alignment",
            exact=True,
            hint="The same difference the other way: records whose mate did not align with no "
            "placeless record left in the file to answer them. Only a survivor placed at more than "
            "one locus can be one — the aligner emits a representative alignment and need write no "
            "unmapped mate for the members it did not emit — so the split refuses when this runs "
            "past the survivors carrying a hit count above one, which is what an aligner never "
            "asked to emit unmapped records gives. At most one of this and the excess beside it is "
            "ever non-zero.",
        ),
        *(
            count_metric(
                f"split_dropped_{reason}",
                f"Dropped: {reason}",
                _counted(dropped, reason),
                group="alignment",
                exact=True,
                hint=_DROP_HINTS[reason],
            )
            for reason in DROP_REASONS
        ),
    ]
    return SampleStats(sample_id=sample, metrics=[m for m in built if m is not None])


__all__ = [
    "DROP_REASONS",
    "SPLIT_SUFFIX",
    "SplitError",
    "SplitStats",
    "parse_outputs",
    "split_chimera",
    "split_metrics",
]
