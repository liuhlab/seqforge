"""One chimera-mapped BAM into one BAM per **Component**, each spelled the way a single-assembly run
would have spelled it.

A **Chimera** is one reference built from several component assemblies, whose chromosome names carry
a ``<separator><component>`` suffix so that every read declares which organism it landed on. That
suffix is what makes the alignment interpretable and what makes every artifact downstream of it
unusable: ``chrI__ce11`` is not ``chrI``, so a browser, a counter and whatever script the user
already owns all refuse it, and a retained CRAM inherits the misspelling permanently. This module is
the undo — one pass over the chimeric BAM, one output per component, each carrying the chromosome
names, the ``@SQ`` order and the lengths its own assembly declares.

**The keep rule is one sentence: a mapped, uniquely-placed, primary alignment.** Four discard
categories fall under it — ``NH > 1``, unmapped, secondary, supplementary — and each is counted
separately even though only the first can occur under the flags the aligner runs with today.
Counting them apart is what lets the rule degrade legibly when a flag changes: a category that
starts firing says so in the summary, instead of quietly moving reads. Unmapped in particular is
DROPPED rather than passed through — such a record's ``RNEXT`` still names its mate's *suffixed*
chromosome, so rewriting ``RNAME`` alone would leave the pointer aimed at a sequence this output's
header does not contain.

**The unit is the record, and nothing is held.** Both mates of a template carry the same ``NH`` (it
counts placements of the template, not of one mate) and sit on the same chromosome, so a stateless
per-record filter keeps a template together by construction. No name sort, no buffer, and a
ten-million-record BAM streams.

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

**Two runtime checks, because the two facts underneath this design were read off the aligner's
source and nobody has yet watched them hold on a real chimera.** The mate sits on this record's own
component, checked per record; and each output kept as many first mates as second, checked once at
the end. Neither is here to catch a bug in this module — the first turns an opaque dictionary lookup
failure into a refusal that names the read and both components, and the second turns a silently
halved output into one that says so.
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
DROP_REASONS = ("unmapped", "secondary", "supplementary", "multimapping")

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
    #: First and second mates per output, kept apart rather than summed. Their equality is the check
    #: that a keep rule testing one mate flag cannot pass, and it is unreadable once added together.
    read1: dict[str, int]
    read2: dict[str, int]
    dropped: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        """The summary payload — what the verb prints AND what lands on disk, one shape.

        Every component that was asked for appears in ``kept``/``read1``/``read2`` even at zero, and
        every reason appears in ``dropped`` even at zero: an absent key and a zero are different
        claims, and only one of them is a measurement. The seqforge version is stamped here rather
        than passed in, because it is provenance about the code that produced the numbers and is not
        something a caller can hold a stale copy of.
        """
        return {
            "seqforge": __version__,
            "separator": self.separator,
            "records_in": self.records_in,
            "kept": dict(sorted(self.kept.items())),
            "read1": dict(sorted(self.read1.items())),
            "read2": dict(sorted(self.read2.items())),
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
) -> SplitStats:
    """One chimeric BAM -> one unindexed, coordinate-sorted BAM per requested Component.

    The outputs stay unindexed because neither consumer needs an index, and stay coordinate-sorted
    because the input is: records are written in the order they were read, and dropping records
    cannot disturb an order.

    ``summary`` is where the counts land, and handing over ``None`` skips the file — the split still
    happened and the caller still gets the numbers back, they simply do not outlive the process.
    """
    import pysam

    if not bam.exists():
        raise SplitError(
            f"{bam} is missing; the alignment step that should have written it did not"
        )

    kept: Counter[str] = Counter()
    read1: Counter[str] = Counter()
    read2: Counter[str] = Counter()
    dropped: Counter[str] = Counter({reason: 0 for reason in DROP_REASONS})
    records_in = 0

    with pysam.AlignmentFile(str(bam), "rb") as source:
        header = source.header.to_dict()
        _checked_request(header, outputs, separator)
        # Which Component each reference index belongs to, resolved ONCE off the header. A record's
        # own Component is then a lookup rather than a string operation per record, and — since the
        # request has already been checked against this same list — every lookup below is total.
        owner = [_split_name(str(entry["SN"]), separator)[1] for entry in header.get("SQ", [])]
        plan = {c: _restored(header, c, path, separator) for c, path in outputs.items()}
        for path in outputs.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        writers = {
            component: pysam.AlignmentFile(str(outputs[component]), "wb", header=plan[component][0])
            for component in outputs
        }
        try:
            for record in source.fetch(until_eof=True):
                records_in += 1
                if record.is_unmapped:
                    dropped["unmapped"] += 1
                    continue
                if record.is_secondary:
                    dropped["secondary"] += 1
                    continue
                if record.is_supplementary:
                    dropped["supplementary"] += 1
                    continue
                # No NH is one placement: the tag is the aligner's statement that a read went
                # somewhere else too, and its absence is not a missing measurement.
                hits = int(record.get_tag("NH")) if record.has_tag("NH") else 1
                if hits > 1:
                    dropped["multimapping"] += 1
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
                restored, tids = plan[component]
                writers[component].write(_rewritten(record, restored, tids))
                kept[component] += 1
                if record.is_read1:
                    read1[component] += 1
                if record.is_read2:
                    read2[component] += 1
        finally:
            for writer in writers.values():
                writer.close()

    torn = {c: (read1[c], read2[c]) for c in sorted(outputs) if read1[c] != read2[c]}
    if torn:
        raise SplitError(
            f"these outputs kept a different number of first and second mates, as "
            f"component: (read1, read2) — {torn}. Both mates of a template carry one NH and sit on "
            f"one chromosome, so a stateless filter keeps them together; a difference means that "
            f"assumption is false for this aligner, and the split is not the thing to trust"
        )

    stats = SplitStats(
        separator=separator,
        records_in=records_in,
        kept={component: kept[component] for component in outputs},
        read1={component: read1[component] for component in outputs},
        read2={component: read2[component] for component in outputs},
        dropped=dict(dropped),
    )
    if summary is not None:
        _write_summary(stats, summary)
    return stats


__all__ = [
    "DROP_REASONS",
    "SPLIT_SUFFIX",
    "SplitError",
    "SplitStats",
    "parse_outputs",
    "split_chimera",
]
