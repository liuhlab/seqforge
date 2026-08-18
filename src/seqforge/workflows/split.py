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
singletons — checked once at the end. And the singleton count is derived a SECOND, independent way
and the two are compared: asked to emit what it could not place, the aligner writes a dead mate as a
placeless record at its live mate's coordinates, so that record names a Component too, and there
must be exactly as many of them per Component as there are singletons. None of the three is here to
catch a bug in this module — the first turns an opaque dictionary lookup failure into a refusal that
names the read and both components, the second turns a silently halved output into one that says so,
and the third costs one more counter, no buffer, and is strictly stronger than comparing raw mate
counts was, since a whole population disappearing from the file would leave those counts balanced.
"""

from __future__ import annotations

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

#: What one split's summary is called, under the cell's own directory. **Public because it has a
#: reader as well as a writer**, which is the line ``fragments.QC_SUFFIX`` already draws: the rule
#: declares the file by importing this, and whatever reads the counts back finds them by importing
#: the same name. A second spelling anywhere is the one that fails in silence — a reader that finds
#: nothing looks exactly like a split that never ran, so nothing raises and nobody is told.
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
        that are in the outputs rather than a fate that took records out of them.
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


def _rewritten(record: AlignedSegment, header: AlignmentHeader, tids: Mapping[int, int]) -> Any:
    """One kept record, rebuilt against the restored header so its references resolve there.

    A record is copied field by field rather than handed to the writer as it stands, because what
    changes is not a field but the dictionary the record's reference INDEXES into: the same tid means
    a different chromosome under the new header, and both mates' indexes have to move together.
    """
    import pysam

    out = pysam.AlignedSegment(header)
    out.query_name = record.query_name
    out.flag = record.flag
    out.reference_id = tids[record.reference_id]
    out.reference_start = record.reference_start
    out.mapping_quality = record.mapping_quality
    out.cigartuples = record.cigartuples
    if record.next_reference_id >= 0:
        out.next_reference_id = tids[record.next_reference_id]
        out.next_reference_start = record.next_reference_start
    out.template_length = record.template_length
    # Sequence before qualities, and not the other way round: pysam clears the qualities whenever a
    # sequence is assigned, so the obvious order silently writes a record with no base qualities.
    out.query_sequence = record.query_sequence
    out.query_qualities = record.query_qualities
    out.set_tags(record.get_tags(with_value_type=True))
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
    The record loop is one stateless pass and stays on one core whatever this says; what scales is
    the block compression underneath it, which is where the wall-clock of writing several BAMs
    actually goes. So the figure is DIVIDED across the outputs rather than handed to each of them:
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
    # side's own; their sum per Component is what the summary carries and what the second
    # derivation below has to reproduce.
    singleton1: Counter[str] = Counter()
    singleton2: Counter[str] = Counter()
    # The second derivation: placeless records whose mate DID align, per Component. Every counter
    # here is one integer per output, so nothing grows with the input.
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
                    # ...and, on its way out, the second derivation of the singleton count. Asked to
                    # emit what it could not place, the aligner writes a dead mate at its LIVE
                    # mate's coordinates, so this record names the Component its partner landed on
                    # even though nothing placed it. A record whose mate is unmapped too names no
                    # Component and is no fragment's survivor, so it is not one of these.
                    if not record.mate_is_unmapped and record.reference_id >= 0:
                        dead_mates[owner[record.reference_id]] += 1
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
                header_out, tids = restored[component]
                writers[component].write(_rewritten(record, header_out, tids))
                kept[component] += 1
                # No NH is one placement: the tag is the aligner's statement that a read went
                # somewhere else too, and its absence is not a missing measurement. It marks the
                # record rather than removing it, so this is a count of what is IN the output.
                if record.has_tag("NH") and int(record.get_tag("NH")) > 1:
                    multiplaced[component] += 1
                if record.is_read1:
                    read1[component] += 1
                    if record.mate_is_unmapped:
                        singleton1[component] += 1
                if record.is_read2:
                    read2[component] += 1
                    if record.mate_is_unmapped:
                        singleton2[component] += 1
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

    disagreed = {
        c: (singletons[c], dead_mates[c]) for c in sorted(outputs) if singletons[c] != dead_mates[c]
    }
    if disagreed:
        raise SplitError(
            f"the fragments that half aligned were counted two ways and the two disagree, as "
            f"component: (survivors carrying the mate-unmapped flag, placeless records whose mate "
            f"did align) — {disagreed}. They are one population seen from either end, so the "
            f"likeliest cause is an aligner that was never asked to write out what it could not "
            f"place: with those records absent the second count is zero and nothing else here "
            f"would say so"
        )

    stats = SplitStats(
        separator=separator,
        records_in=records_in,
        kept={component: kept[component] for component in outputs},
        read1={component: read1[component] for component in outputs},
        read2={component: read2[component] for component in outputs},
        multiplaced={component: multiplaced[component] for component in outputs},
        singletons=singletons,
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
    "this is the only place a chimeric run reports them, since they never reach a matrix. The half "
    "of them whose MATE did align is counted a second time beside each Component, as its "
    "singletons.",
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

    **Four metrics per Component, and each is a different question.** What it KEPT, and that as a
    SHARE of the records that came in: the share is the number the whole exercise exists to produce
    — the bacterial fraction of a well, readable without opening an ``.h5ad`` — and it is a share
    because only a share compares between two cells of different depths, while the count is what
    makes the page CLOSE, since every kept count plus every drop count is exactly the records that
    came in. Then how much of that was MULTIPLY PLACED, which is how much of the Component's kept
    signal a reader may not treat as one locus; and how many SINGLETONS it holds, records whose mate
    did not align, which is the population that used to make this whole step refuse.

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
        ]
    built += [
        count_metric(
            f"split_dropped_{reason}",
            f"Dropped: {reason}",
            _counted(dropped, reason),
            group="alignment",
            exact=True,
            hint=_DROP_HINTS[reason],
        )
        for reason in DROP_REASONS
    ]
    return SampleStats(sample_id=sample, metrics=[m for m in built if m is not None])


def read_split_summary(path: Path, sample: str) -> SampleStats:
    """Load one ``<sample>.split.json`` and normalise it.

    The thin half of the adapter, in the shape ``extract.read_extract_summary`` established: loading
    lives beside the code that WRITES the file so the registry hands over a path and gets metrics
    back, and the judgement lives in :func:`split_metrics`, which needs no file to test. Raises
    ``OSError``/``ValueError`` if the bytes are unusable, so one bad summary costs its own columns
    and not the whole page.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} is not a chimera split summary")
    return split_metrics(payload, sample)


__all__ = [
    "DROP_REASONS",
    "SPLIT_SUFFIX",
    "SplitError",
    "SplitStats",
    "parse_outputs",
    "read_split_summary",
    "split_chimera",
    "split_metrics",
]
