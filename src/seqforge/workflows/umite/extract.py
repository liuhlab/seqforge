"""Lift the tagged-molecule UMI out of R1 and write it as an unaligned BAM carrying ``UB:Z:``.

Per cell: read the tagged FASTQ, find the anchor in it, cut the UMI out, trim the structural prefix,
and emit one uBAM the aligner reads back. Everything below was *run* on ten published GSE207085
cells against the reference package while it was still installed (2026-08-04); none of it is
inferred from documentation.

**The mate is an addition, not half of the extraction, so it is optional.** The tag operation is
entirely within one read — find the anchor, cut the UMI, trim ``span``, keep the rest — and the
second FASTQ contributes nothing to it: it only *inherits* the resulting ``UB`` onto a record
emitted alongside. Take it away and the operation is unchanged; only the record count out the other
end moves, from two interleaved records per fragment to one. So the single-end form is the base case
and the pairing is the addition, sharing the geometry, the anchor search, the bounded reader and
both truncation verdicts — the only branch is at the write, and the flags say which shape came out
so the uBAM stays self-describing (a ``SAM PE`` invocation over unpaired records is a crash).

**The output is a uBAM, not FASTQ, and that was measured rather than chosen.** STAR refuses ``UB``
in ``--outSAMattributes`` outside its single-cell mode (``FATAL INPUT ERROR: ... not allowed for
--soloType None``), so a UMI cannot be asked for on the way out. The route that works is to put it
in on the way *in*: an unaligned BAM carrying ``UB:Z:``, read with ``--readFilesType SAM PE
--readFilesCommand samtools view --readFilesSAMattrKeep All``. 452 of 716 aligned records came out
carrying their input ``UB`` — and their ``RG`` — without ``UB`` ever being named in the output
attribute list, and this module's own output was put through the same route again (STAR 2.7.11b,
2026-08-04: 40 of 40 tagged records carried it, 46 of 46 carried ``RG``). Carrying the UMI in a tag
rather than in the read name is also what makes the CRAM converter reusable unchanged: it rewrites
every QNAME to ``r<N>`` and would destroy a name-carried UMI, along with the -16.2% that rewrite
buys. ``--readFilesSAMattrKeep All`` is STAR's *default* rather than an opt-in — dropping it changed
nothing on that re-run — so it is passed to pin a default this whole format rests on, not to enable
anything.

**The anchor search is unanchored, and that is the whole point of the search.** The obvious reading
of "the geometry is derived from the element model" is that the tag sits at the declared offset, so
slice it there. Measured against the reference matcher, which is a ``re.search``: of 8,266 exact
hits, **354 (4.3%) are not at offset 0**, clustering at 13, 15 and 23. A port that slices at the
declared offset silently loses every one of them. So the declared offset is the *lower bound* of a
search, never the match position.

**The matcher is an exact anchor, found anywhere from its declared start to 24 bases past it, closed
by a trailing motif tolerant of one substitution.** There is no fuzzy path: the reference's
mismatch- and indel-tolerant fallback was priced against this fixture and refused on purity — the
step to it fabricates a tag in 26% of the reads it adds — so this port is stricter than the
reference on the anchor and looser only on the motif, deliberately (issue #352, Out of scope).

**That 24 is mechanistic rather than fitted.** No exact hit anywhere in 18,901 reads starts past
offset 24 — the bound being Tn5 mosaic-end read-through, which is what puts anything in front of the
tag at all. Capping there costs 0 exact hits, and the 113 of 8,976 hits a tolerant matcher would
find past it (-1.26%) are ones not to have: it matches spurious 11-mers as deep as offset 133, at
offsets a fixed-offset chemistry cannot produce.

**R1 and R2 are paired positionally**, as every other tool does, so the input contract stops
depending on who produced the FASTQ. That dissolves a whole hazard class rather than guarding it:
the reference took everything after the *last underscore* of the read name as the UMI, with no
format check, so a cell named ``cell_42`` yielded the UMI ``42`` — silently, at exit 0. Nothing
here parses a read name for anything but a QNAME.

**The input gate checks both lines of a FASTQ record.** These packages repeat the full ID on the
``+`` line, and a reader that compares the two refuses a record where they disagree. Rewriting only
the ``@`` line therefore trades one refusal for another — a wrong first attempt already made once,
while capturing the reference fixture — so the gate is on the *record*, not on the header.

**This reads every record of both FASTQs, and the read budget does not bind it.** The rule that
bounds a FASTQ read is about *identification*: a probe joins a bounded head to a whole file, its
default is justified by the chemistry call being invariant in N well past it, and every test that
enforces it is a probe or fingerprint test. This step is a transformation whose output is one BAM
record per input record — bounding it would not make it cheaper, it would silently emit a partial
deliverable, which is the failure class this compiler exists to prevent. What the rule *does* bind
here is what it is really guarding: never write a second budget loop. So this iterates
:class:`~seqforge.probe.streaming.BoundedReader` — the one FASTQ loop in the project — under an
explicitly unbounded :class:`~seqforge.probe.streaming.Budget`, and takes its gzip truncation and
corruption verdicts as input-gate refusals instead of re-deciding what a broken FASTQ is.

**What the extraction MEASURED outlives the uBAM it measured, because the uBAM is ``temp()``.** The
counts below are the per-cell readout of whether the chemistry behaved — the tagged fraction is a
tunable protocol parameter, published across 6.9–70.5% over five libraries, so a cell at 2% and a
cell at 28% are a bench problem and a normal run and nothing downstream can tell them apart once the
records are gone. Printing them to stdout left the only surviving copy in whatever captured it,
which on a cluster is a scheduler log somebody rotates. So this also writes one small JSON per cell
beside the cell's other outputs, and the report reads it back — the same shape STAR sets by dropping
``Log.final.out`` into a sample's directory unasked. **Stdout and the file are one payload**: every
key stdout carried it still carries, and both gained the geometry and the version together, so the
printed account and the durable one cannot come to say different things.

``pysam`` is a plain dependency, not a runtime one, and this module needs **no container**: it
shells out to nothing at all. The h5ad packager draws the same line for the same reason — writing a
file is not aligning reads.
"""

from __future__ import annotations

import json
import re
from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack, closing
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

import pysam

from ... import __version__
from ...models.dataset import ReadDef, ReadElement, ReadLayout
from ...probe.streaming import BoundedReader, Budget, Record
from ..metrics import Metric, SampleStats, fraction
from ..metrics import count as count_metric
from ..units import load_units, paired_fastqs

#: How far past its declared start the anchor may be found. Mechanistic: no exact hit anywhere in
#: 18,901 reads starts past offset 24, that bound being Tn5 mosaic-end read-through. It is a
#: property of the chemistry rather than of one dataset, which is why it is a module literal and not
#: a knob — a tunable window is a window somebody sets wrong.
MAX_ANCHOR_DRIFT = 24

#: Substitutions tolerated in the motif that closes the tag (the ``GGG`` a template switch leaves).
#: **One, and the value is load-bearing.** At three the check is vacuous over a 3 bp motif — every
#: position may differ, so it accepts any three bases and manufactures a UMI out of an untagged
#: read. One absorbs ordinary sequencing error and still says something.
TRAILING_MAX_MISMATCH = 1

#: An unaligned pair, both mates unmapped: ``PAIRED | UNMAPPED | MATE_UNMAPPED`` plus READ1/READ2.
#: The same two values ``samtools import`` writes, because a uBAM's flags are not ours to invent.
_FLAG_READ1 = 0x1 | 0x4 | 0x8 | 0x40
_FLAG_READ2 = 0x1 | 0x4 | 0x8 | 0x80

#: One unaligned read that is not half of anything: ``UNMAPPED`` and nothing else. Deliberately not
#: ``_FLAG_READ1`` with the mate bits cleared — a record that keeps PAIRED with no mate beside it
#: reads as a truncated pair to everything downstream, and the aligner invocation is derived from
#: these flags rather than told, so they are the whole statement of which shape was written.
_FLAG_UNPAIRED = 0x4

#: The budget this module does not have. Spelled as a value rather than left implicit so that a
#: reader who came here from the bounded-read rule sees the opt-out and its reason (the header
#: docstring) rather than a missing argument.
_UNBOUNDED = Budget(max_reads=2**62, max_bytes=2**62)

#: What one cell's extraction summary is called, under that cell's own directory. **Public because it
#: has a reader as well as a writer**, which is the line ``fragments.QC_SUFFIX`` already draws: the
#: rule declares the file by importing this, and the pipeline-stats registry finds it by importing
#: the same name. A second spelling anywhere is the one that fails in silence — a report that finds
#: nothing looks exactly like a pipeline that never ran, so nothing raises and nobody is told.
#:
#: Plain JSON rather than gzipped, unlike the two QC bundles: this is a handful of counts and a small
#: histogram, so compressing it would cost a reader ``zcat`` and buy a few hundred bytes. It is also
#: the artifact somebody opens by hand at 2am when a plate looks wrong.
EXTRACT_SUFFIX = ".umi-extract.json"


class UmiExtractError(RuntimeError):
    """The pair cannot be extracted as handed over: a bad record, a bad pairing, a bad layout."""


# ---- geometry, derived from the element model ----------------------------------------------------
#
# Nothing below is a declared parse key. The chemistry's `parse_keys` stay EMPTY: every number the
# extractor needs is already in the read layout the manifest carries, and deriving it is what keeps
# "what the data is" and "how to read it" from being two facts that can disagree.


#: One rendered geometry, and the only shape the CLI accepts —
#: ``R1:ATTGCGCAATG@0:umi@11+8:GGG@19:cdna@22``. Every offset in it is ABSOLUTE, in the element
#: model's own 0-based half-open coordinates, so the string reads as the layout it came from rather
#: than as an anchor-relative arithmetic nobody can check by eye.
_RENDERED = re.compile(
    r"^(?P<read>[A-Za-z0-9_.+-]+)"
    r":(?P<anchor>[ACGTN]+)@(?P<anchor_start>\d+)"
    r":umi@(?P<umi_start>\d+)\+(?P<umi_length>\d+)"
    r":(?P<trailing>[ACGTN]+)@(?P<trailing_start>\d+)"
    r":cdna@(?P<cdna_start>\d+)$"
)


@dataclass(frozen=True)
class TagGeometry:
    """Where the tag, the UMI and the cDNA sit, relative to **the anchor's own start**.

    Relative rather than absolute because the anchor floats: it is declared at ``anchor_start`` and
    found anywhere from there to ``anchor_start + MAX_ANCHOR_DRIFT``, so every offset that follows
    it has to travel with it. ``anchor_start`` is the search's lower bound and never a slice index.

    It carries the ``read_id`` it was derived from, which is what lets a rule wired to hand over the
    plain mate be refused: the geometry states which read is tagged, so nothing has to be told twice.
    """

    #: Which layout read carries the tag. Part of the geometry rather than a second argument beside
    #: it: "where the UMI is" and "which read it is on" are one fact read off one element list, and
    #: splitting them is how a caller comes to hold a geometry for a read it is not extracting.
    read_id: str
    #: The literal tag the element model declares, e.g. an 11 bp piece of the template-switch oligo.
    anchor: str
    #: Where the layout says the anchor begins. The bottom of the search window, not the match.
    anchor_start: int
    #: The UMI's start, measured from the anchor's start.
    umi_offset: int
    umi_length: int
    #: The motif that closes the tag, and its start measured from the anchor's start.
    trailing: str
    trailing_offset: int
    #: Where cDNA begins, measured from the anchor's start — so it is also how much a matched read
    #: loses to trimming.
    cdna_offset: int

    @property
    def span(self) -> int:
        """How many bases one match consumes: tag + UMI + trailing motif."""
        return self.cdna_offset

    def render(self) -> str:
        """The whole geometry as ONE string — what the composer emits and the rule hands over.

        One value, not six, and the same move chromap's ``--read-format`` makes: a geometry split
        across six config keys is six chances for five of them to travel and one to be dropped, and
        the drop is silent because five correct numbers still cut *a* span out of *a* read. It is
        computed by the composer from the element coordinates and never declared by anyone — the KB
        is refused if it tries — so what a rule passes here is a derivation, not a knob.
        """
        return (
            f"{self.read_id}:{self.anchor}@{self.anchor_start}"
            f":umi@{self.anchor_start + self.umi_offset}+{self.umi_length}"
            f":{self.trailing}@{self.anchor_start + self.trailing_offset}"
            f":cdna@{self.anchor_start + self.cdna_offset}"
        )

    @classmethod
    def parse(cls, text: str) -> TagGeometry:
        """Read back what :meth:`render` wrote, or refuse.

        Refuses rather than tolerating a near-miss: this value arrives from a composed config, so a
        string that does not round-trip means the composer and the extractor disagree about what a
        geometry is, and extracting under a half-understood one would cut a UMI out of the wrong
        bases at exit 0.
        """
        match = _RENDERED.match(text.strip())
        if match is None:
            raise UmiExtractError(
                f"{text!r} is not a rendered read structure "
                f"(expected e.g. `R1:ATTGCGCAATG@0:umi@11+8:GGG@19:cdna@22`); it is emitted by "
                f"compose from the element coordinates and is not a value to type by hand"
            )
        anchor_start = int(match["anchor_start"])
        return cls(
            read_id=match["read"],
            anchor=match["anchor"],
            anchor_start=anchor_start,
            umi_offset=int(match["umi_start"]) - anchor_start,
            umi_length=int(match["umi_length"]),
            trailing=match["trailing"],
            trailing_offset=int(match["trailing_start"]) - anchor_start,
            cdna_offset=int(match["cdna_start"]) - anchor_start,
        )


def _placed(elements: Sequence[ReadElement]) -> list[tuple[ReadElement, int, int | None]]:
    """``(element, start, end)`` in declaration order; ``end`` is ``None`` for an open tail.

    A layout may pin every element with a ``start`` or leave them to follow one another; this walks
    the chain so the derivation works either way and never has to ask which style a spec was written
    in. An element of unknown width (the cDNA tail) ends the arithmetic rather than guessing a value.
    """
    placed: list[tuple[ReadElement, int, int | None]] = []
    pos = 0
    for el in elements:
        start = el.start if el.start is not None else pos
        width = el.length
        if width is None and el.sequence is not None:
            width = len(el.sequence)
        if width is None and el.min_len is not None and el.min_len == el.max_len:
            width = el.min_len
        end = start + width if width is not None else None
        placed.append((el, start, end))
        pos = end if end is not None else start
    return placed


def geometry_for_elements(read_id: str, elements: Sequence[ReadElement]) -> TagGeometry:
    """Derive the extraction geometry from one read's elements, or refuse.

    The shape being read out is `` <fixed tag> <UMI> <fixed motif> ... <cDNA> `` with the first three
    contiguous, which is what makes "the match consumes ``span`` bases" true. Adjacency is checked
    rather than assumed: a gap between them means the layout is not this shape, and matching a span
    that straddles a gap would cut the UMI out of the wrong bases while still exiting 0.

    Elements rather than a whole read, because there are two element models that state this one fact
    — the KB's, which the composer derives the emitted geometry from, and the manifest's, which the
    gate re-derives it from — and a single walker over the IR elements is what keeps those two from
    being two answers. :func:`geometry_for_read` is the manifest-side caller.
    """
    placed = _placed(elements)
    umi_at = [i for i, (el, _, _) in enumerate(placed) if el.role == "UMI"]
    if len(umi_at) != 1:
        raise UmiExtractError(
            f"read {read_id} declares {len(umi_at)} UMI elements; the tagged-molecule "
            f"extractor needs exactly one"
        )
    i = umi_at[0]
    umi_el, umi_start, umi_end = placed[i]
    if umi_el.length is None or umi_end is None:
        raise UmiExtractError(f"read {read_id}'s UMI element declares no length")
    if i == 0:
        raise UmiExtractError(
            f"read {read_id} opens with its UMI, so there is no anchor to find it by"
        )
    anchor_el, anchor_start, anchor_end = placed[i - 1]
    if not anchor_el.sequence:
        raise UmiExtractError(
            f"the element before read {read_id}'s UMI declares no literal sequence, so there "
            f"is nothing to search for"
        )
    if anchor_end != umi_start:
        raise UmiExtractError(
            f"read {read_id}'s anchor ends at {anchor_end} and its UMI starts at {umi_start}; "
            f"the extractor cuts one contiguous span and cannot straddle a gap"
        )
    if i + 1 >= len(placed):
        raise UmiExtractError(
            f"read {read_id}'s UMI closes the read; there is no trailing motif to confirm a "
            f"match against, and an anchor alone would tag untagged reads"
        )
    trail_el, trail_start, trail_end = placed[i + 1]
    if not trail_el.sequence or trail_end is None:
        raise UmiExtractError(
            f"the element after read {read_id}'s UMI declares no literal sequence"
        )
    if trail_start != umi_end:
        raise UmiExtractError(
            f"read {read_id}'s UMI ends at {umi_end} and its trailing motif starts at "
            f"{trail_start}; the extractor cuts one contiguous span and cannot straddle a gap"
        )
    cdna_start = next((s for el, s, _ in placed[i + 2 :] if el.role in ("cDNA", "gDNA")), None)
    if cdna_start is None:
        raise UmiExtractError(f"read {read_id} carries a UMI but no cDNA to trim down to")
    return TagGeometry(
        read_id=read_id,
        anchor=anchor_el.sequence,
        anchor_start=anchor_start,
        umi_offset=umi_start - anchor_start,
        umi_length=umi_el.length,
        trailing=trail_el.sequence,
        trailing_offset=trail_start - anchor_start,
        cdna_offset=cdna_start - anchor_start,
    )


def geometry_for_read(read: ReadDef) -> TagGeometry:
    """The extraction geometry of one manifest read — :func:`geometry_for_elements` on its own id."""
    return geometry_for_elements(read.read_id, read.elements)


def tagged_geometry(reads: Sequence[tuple[str, Sequence[ReadElement]]]) -> TagGeometry:
    """The geometry of the one read among ``reads`` that carries a tagged-molecule UMI.

    Refuses on none and on more than one, rather than picking. Which read is tagged is a fact about
    the chemistry that the element model already states; a caller that had to name it could name the
    wrong one, and the extractor would then cut a UMI out of a cDNA read.

    ``(read_id, elements)`` pairs rather than a ``ReadLayout``, so the composer can hand over a KB
    spec's reads translated into IR elements and get the SAME answer the manifest gives — one
    selection rule and one derivation, consulted from both sides.
    """
    tagged = [(read_id, els) for read_id, els in reads if any(el.role == "UMI" for el in els)]
    if len(tagged) != 1:
        raise UmiExtractError(
            f"{len(tagged)} of this layout's reads carry a UMI element "
            f"({[read_id for read_id, _ in reads]}); the extractor needs exactly one"
        )
    return geometry_for_elements(*tagged[0])


def tagged_read_geometry(layout: ReadLayout) -> TagGeometry:
    """:func:`tagged_geometry` over a manifest's read layout — what the BYTES were decided to be."""
    return tagged_geometry([(read.read_id, read.elements) for read in layout.reads])


# ---- finding one tag ----------------------------------------------------------------------------


@dataclass(frozen=True)
class TagMatch:
    """A tag found in one read: where the anchor started, and the UMI it carried."""

    start: int
    umi: str


def _mismatches_within(observed: str, expected: str, limit: int) -> bool:
    """Hamming distance from ``observed`` to ``expected``, thresholded. Substitutions only.

    A length disagreement is a non-match rather than a distance: the motif is a fixed-width piece of
    a fixed-offset chemistry, and a read too short to hold it holds no evidence either way.
    """
    if len(observed) != len(expected):
        return False
    seen = 0
    for a, b in zip(observed, expected, strict=True):
        if a != b:
            seen += 1
            if seen > limit:
                return False
    return True


def find_tag(seq: str, geometry: TagGeometry) -> TagMatch | None:
    """The leftmost tag in ``seq``'s extraction window, or ``None`` for an untagged (internal) read.

    Leftmost because the reference matcher is a ``re.search`` and takes the first hit; a port that
    took the best-scoring hit would disagree with it on the reads where it matters and would need a
    tie-break nobody has written down. Deterministic: no candidate is chosen at random, and the
    scan order is a plain left-to-right walk.

    A hit whose span runs off the end of the read is **not** a hit. The tagged read has to keep at
    least one base of cDNA to be worth aligning, and a zero-length record is one an aligner refuses
    rather than skips — so a read that is all prefix falls through to the untagged path, keeping its
    bases, instead of becoming an empty record with a UMI on it.

    **The read is searched where it lies**, with no prefix cut from it first. What bounds the search
    is the last offset the anchor may START at, and a copy of the extraction window on top of that
    bounds nothing further: the anchor is never longer than the span a match consumes, so a hit
    inside the search bound is inside the window already. Slicing one anyway cost 0.05 µs a read to
    say nothing.
    """
    lowest, highest = geometry.anchor_start, geometry.anchor_start + MAX_ANCHOR_DRIFT
    stop = highest + len(geometry.anchor)  # `find`'s end is exclusive, so the last legal start + it
    at = seq.find(geometry.anchor, lowest, stop)
    while at != -1:
        umi_from = at + geometry.umi_offset
        trail_from = at + geometry.trailing_offset
        if at + geometry.span < len(seq) and _mismatches_within(
            seq[trail_from : trail_from + len(geometry.trailing)],
            geometry.trailing,
            TRAILING_MAX_MISMATCH,
        ):
            return TagMatch(at, seq[umi_from : umi_from + geometry.umi_length])
        at = seq.find(geometry.anchor, at + 1, stop)
    return None


# ---- the input gate -----------------------------------------------------------------------------


def _decoded(record: Record, *, path: Path, index: int) -> tuple[str, str, str]:
    """``(query_name, sequence, quality)`` for one gated FASTQ record.

    Three refusals live here, and each of them is a file that would otherwise produce a plausible
    BAM. The ``+`` line is checked against the ``@`` line because these packages repeat the whole ID
    there and a half-rewritten name is the shape a normalisation step leaves behind. The quality
    length is checked against the sequence length because a BAM record pairs them by position and
    the writer would raise somewhere less legible. And the bytes are decoded strictly, because the
    lenient decode a probe uses — which replaces what it cannot read — is right for a signal and
    wrong for a payload we are about to write back out.
    """
    header, seq, plus, qual = record
    repeated = plus[1:]
    if repeated and repeated != header[1:]:
        raise UmiExtractError(
            f"{path} record {index}: the '+' line names "
            f"{repeated.decode('ascii', 'replace')!r} but the '@' line names "
            f"{header[1:].decode('ascii', 'replace')!r}. These differ, so this file has been "
            f"half-renamed: rewrite BOTH lines of a record, or neither"
        )
    if len(seq) != len(qual):
        raise UmiExtractError(
            f"{path} record {index}: {len(seq)} bases against {len(qual)} quality characters"
        )
    try:
        name = header[1:].split(maxsplit=1)[0].decode("ascii")
        return name, seq.decode("ascii"), qual.decode("ascii")
    except (IndexError, UnicodeDecodeError) as exc:
        raise UmiExtractError(
            f"{path} record {index} is not a readable FASTQ record: {exc}"
        ) from exc


def _query_name(name: str) -> str:
    """The QNAME a uBAM record takes: the tagged read's name, minus any mate suffix.

    Which mate a record is lives in its flag once it is in a BAM, so a ``/1`` riding along in the
    name is a second, weaker copy of that fact — and one that some readers pair on. Only the tagged
    read's name is ever consulted: the mate's is discarded with its FASTQ, which is what makes
    positional pairing hold for a pair whose names disagree.
    """
    return name[:-2] if name.endswith(("/1", "/2")) else name


def _checked(reader: BoundedReader, path: Path) -> None:
    """Raise if the stream that just finished was not a whole, readable gzip FASTQ.

    Both verdicts come from the one bounded reader rather than being re-decided here, and both
    matter for different reasons: a truncated upload silently costs reads off the end of a cell,
    and a corrupt member is not a FASTQ at all. Valid only once the iteration is exhausted, which is
    why this is called after the loop and not inside it.
    """
    if not reader.ok:
        raise UmiExtractError(f"{path} is not readable gzip: re-fetch it before extracting")
    if reader.truncated:
        raise UmiExtractError(
            f"{path} ends mid-record: a truncated FASTQ would extract a cell that is quietly "
            f"missing its tail"
        )


# ---- the extraction -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractStats:
    """What one cell's extraction did — the numbers a rule log and a QC page want."""

    sample: str
    #: Input molecules, which is one record out on a single-end plate and two on a paired one. Not
    #: ``pairs``: that name states the paired shape as though it were the only one, and the count it
    #: names is a count of what came IN, which the mate's presence does not change.
    fragments: int
    tagged: int
    #: How many tagged reads had their anchor at each offset. The reason the search is unanchored is
    #: a measured offset distribution, so a run that reports its own is a run that can be checked
    #: against it instead of trusted — the 4.3%-not-at-zero claim is observable per cell, forever.
    offsets: dict[int, int]
    #: The geometry this cell was extracted under, held as the value and rendered on the way out.
    #: Carried because every number beside it is only interpretable against it: a tagged fraction of
    #: 0 means a dead library under one geometry and the wrong read handed over under another, and
    #: the artifact outlives the config that said which. It is also what makes the offsets readable —
    #: the declared anchor start is in it, so "where the tag actually began" can be compared with
    #: where the layout said it would.
    geometry: TagGeometry

    @property
    def untagged(self) -> int:
        """Internal reads: no tag, no UMI, nothing trimmed. A third to two thirds of a real library."""
        return self.fragments - self.tagged

    def to_dict(self) -> dict[str, object]:
        """The summary payload — what the verb prints AND what lands on disk, one shape.

        Offsets are string-keyed because JSON object keys are strings. The seqforge version is
        stamped here rather than passed in: it is provenance about the code that produced the
        numbers, so it is not something a caller can hold a stale copy of.
        """
        return {
            "sample": self.sample,
            "seqforge": __version__,
            "geometry": self.geometry.render(),
            "fragments": self.fragments,
            "tagged": self.tagged,
            "untagged": self.untagged,
            "offsets": {str(k): self.offsets[k] for k in sorted(self.offsets)},
        }


def _write_summary(stats: ExtractStats, path: Path) -> None:
    """One cell's counts, on disk, as the last act of an extraction that finished.

    Last, and only on success, so the file's existence means the uBAM beside it was written whole. A
    summary left behind by a refusal would say a cell was extracted at whatever depth the reader had
    reached — a number indistinguishable from a shallow library, which is the failure this artifact
    exists to make visible.

    Written straight to ``path`` with no temp-and-rename, for the reason the uBAM is: it is a
    declared rule output, snakemake deletes the outputs of a failed job, and the mechanism that owns
    the file owns cleaning it up. Trailing newline because a human opens this one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats.to_dict(), indent=2) + "\n", encoding="utf-8")


def _counted(payload: Mapping[str, object], key: str) -> int | None:
    """One non-negative integer out of a summary payload, or ``None`` for anything else.

    Absent is absent, and so is a value of the wrong shape: a metric the artifact does not carry must
    be missing from the page rather than rendered as a zero somebody acts on. That covers an older
    summary written before a key existed and a hand-edited one, with the same answer for both.
    """
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _anchor_drift(payload: Mapping[str, object], tagged: int | None) -> float | None:
    """The share of tagged reads whose tag did NOT start where the layout declares it.

    The offsets histogram compressed to the one number it is read for. The search is unanchored
    because a measured 4.3% of exact hits are not at the declared start (clustering at 13, 15 and
    23), so this cell's own figure is what makes that measurement checkable rather than trusted, and
    a distribution that has shifted is a primer or trimming problem no count matrix would explain.

    The declared start comes from the geometry the payload carries, parsed by the same code that
    rendered it. A payload with no readable geometry yields ``None`` rather than a share measured
    against a guessed origin — the histogram is still on disk, and a wrong denominator is worse than
    a missing column. So does a histogram holding more matches than the file says were tagged: the
    two numbers are written together and disagree only in a file somebody edited, and a share above
    100% reads as a rendering bug rather than as the inconsistency it is.
    """
    offsets, geometry = payload.get("offsets"), payload.get("geometry")
    if not tagged or not isinstance(offsets, Mapping) or not isinstance(geometry, str):
        return None
    try:
        declared = TagGeometry.parse(geometry).anchor_start
    except UmiExtractError:
        return None
    drifted = at_declared = 0
    for at, seen in offsets.items():
        if not isinstance(seen, int) or isinstance(seen, bool) or seen < 0:
            return None
        if str(at) == str(declared):
            at_declared += seen
        else:
            drifted += seen
    return drifted / tagged if drifted + at_declared <= tagged else None


def extract_metrics(payload: Mapping[str, object], sample: str) -> SampleStats:
    """One cell's summary payload -> the columns its report row shows. Pure — no file, no pysam.

    **The tagged fraction is the point of this adapter** (the module docstring argues why), and it is
    deliberately **ungraded**: a library tuned low is a choice somebody made at the bench and not a
    fault, so inventing a bar would tint a page over a decision. The number is the contribution; a
    threshold is a measurement nobody has made.

    Fragments come along as its own denominator, and they are the extractor's count of what it READ
    rather than the counter's of what reached the matrix. Two numbers that should agree, from two
    artifacts, one of which exists long before the other: a plate still extracting has this column
    and no other, and a plate that finished has both and can be checked.
    """
    fragments, tagged = _counted(payload, "fragments"), _counted(payload, "tagged")
    built: list[Metric | None] = [
        count_metric(
            "extract_fragments",
            "Fragments read",
            fragments,
            group="input",
            exact=True,
            hint="Fragments this cell's FASTQs held, counted by the extractor — one per read pair "
            "on a paired plate, one per read on a single-ended one, whatever its fate.",
        ),
        fraction(
            "umi_tagged",
            "UMI-tagged",
            tagged / fragments if tagged is not None and fragments else None,
            group="input",
            hint="Share of fragments carrying a template-switch tag, so the ones a UMI can "
            "deduplicate. It is a tunable property of the tagmentation, published from 6.9% to "
            "70.5%, and ungraded here for that reason — but a cell far below its plate's own "
            "spread is a bench problem, and one at zero means the wrong read was extracted.",
            headline=True,
        ),
        fraction(
            "umi_anchor_drift",
            "Anchor drift",
            _anchor_drift(payload, tagged),
            group="input",
            hint="Share of tagged reads whose tag began somewhere other than where the layout "
            "declares. A few percent is normal (4.3% measured, from mosaic-end read-through); a "
            "shifted distribution is a primer or trimming problem, not a counting one.",
        ),
    ]
    return SampleStats(sample_id=sample, metrics=[m for m in built if m is not None])


def read_extract_summary(path: Path, sample: str) -> SampleStats:
    """Load one ``<sample>.umi-extract.json`` and normalise it.

    The thin half of the adapter, in the shape ``fragments.read_metrics`` established: loading lives
    here so the registry hands over a path and gets metrics back, and the judgement lives in
    :func:`extract_metrics`, which needs no file to test. Raises ``OSError``/``ValueError`` if the
    bytes are unusable, so one bad summary costs its own columns and not the whole page.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} is not a UMI extraction summary")
    return extract_metrics(payload, sample)


def _header(sample: str) -> dict[str, object]:
    """The uBAM header: unsorted, no reference sequences, one read group naming the cell.

    A read group and not a cell barcode tag, because on a plate assay the cell *is* the file — the
    demultiplexing happened at the bench — and a read group is what a BAM already has for "which
    library this record came from". It survives the aligner: the same run that carried ``UB``
    through carried ``RG`` with it.
    """
    return {
        "HD": {"VN": "1.6", "SO": "unsorted"},
        "RG": [{"ID": sample, "SM": sample}],
    }


#: The seven SAM fields between the flag and the sequence, and an unaligned record has none of them:
#: no reference, no position, no mapping quality, no CIGAR, no mate reference and no template
#: length. Their placeholders are the same for every record this module writes, so they are spelled
#: once here rather than rebuilt per record.
_UNALIGNED_FIELDS = "\t*\t0\t0\t*\t*\t0\t0\t"


def _segment(
    header: pysam.AlignmentHeader,
    *,
    name: str,
    seq: str,
    qual: str,
    flag: int,
    sample: str,
    umi: str | None,
) -> pysam.AlignedSegment:
    """One unaligned BAM record. The UMI rides in ``UB``, and nothing rides in the name.

    Parsed from one SAM line rather than assembled attribute by attribute: 2.64 µs a record became
    0.65, most of that the per-base quality array the string form never has to build, since a SAM
    line carries the quality string the FASTQ already handed over. **What is written is unchanged**
    — this is how a record is built, not what it holds, and the byte-identity test is what says so.

    Two bytes need saying, because both are places where the two constructions do NOT agree by
    default. The record's **bin** — the index field of a coordinate-sorted BAM, meaningless in an
    unsorted uBAM nothing indexes — is put back to the zero an unset record has always carried,
    because parsing fills in the bin htslib computes for an unmapped read and this file's bytes are
    its identity. And a **quality string of exactly** ``*`` is SAM's word for *no qualities at all*,
    so the one-base read whose Phred happens to be 9 has its quality restored by hand rather than
    silently written as a record that never had one.

    The input gate covers the rest: it takes the QNAME as one whitespace-free token and refuses a
    record whose quality is not as long as its sequence. What it does not check is a tab inside
    either — a FASTQ that is not one — and parsing stops on it where assembling encoded it as a base.
    """
    tags = f"\tRG:Z:{sample}" if umi is None else f"\tRG:Z:{sample}\tUB:Z:{umi}"
    segment = pysam.AlignedSegment.fromstring(
        f"{name}\t{flag}{_UNALIGNED_FIELDS}{seq}\t{qual}{tags}", header
    )
    segment.bin = 0
    if qual == "*":
        segment.query_qualities = pysam.qualitystring_to_array(qual)
    return segment


def _positionally_paired(
    one: BoundedReader, two: BoundedReader, *, tagged_fastq: Path, mate_fastq: Path
) -> Generator[tuple[Record, Record | None], None, None]:
    """Record *n* of the tagged FASTQ with record *n* of its mate, or refuse.

    Positional, as every other tool does, so the input contract stops depending on who produced the
    FASTQ; files of unequal length are refused rather than zipped to the shorter one, because a pair
    silently dropped off the end is a cell that counts low and says nothing.

    A generator rather than a branch inside the loop, so the refusal cannot fire — or be forgotten —
    on a run that has no mate to be unequal with. What it yields is the shape the single-end case
    yields too, with ``None`` where the mate would be, which is what keeps one loop below.
    """
    for index, (rec1, rec2) in enumerate(zip_longest(one, two)):
        if rec1 is None or rec2 is None:
            short = mate_fastq if rec2 is None else tagged_fastq
            raise UmiExtractError(
                f"{short} runs out at record {index} while its mate does not; the two are "
                f"paired by position, so a length disagreement is unpairable rather than "
                f"truncatable"
            )
        yield rec1, rec2


def extraction_inputs(
    *,
    units: Path | None,
    sample: str,
    r1: Sequence[Path],
    r2: Sequence[Path],
    tagged_role: str,
) -> tuple[list[Path], list[Path] | None]:
    """The two forms of "which files is this cell", resolved into ONE answer (ADR-0036).

    The **Units table** form — ``units`` plus ``sample`` — is what a module renders, and it is the
    form that has no file list on the command line at all: the verb asks
    :func:`~seqforge.workflows.units.paired_fastqs` for the same sample's files the rule declared as
    its inputs, off the same table, so there is no arity, quoting or ordering fact in the rendered
    command for `snakemake -n -p` to plan clean and then die on.

    The direct form — repeated ``--r1``/``--r2`` — survives for a hand invocation and for a test that
    wants two paths and no table. There the caller is *asserting* the pairing rather than reading it,
    so the two lists are taken in the order given and refused when they are not the same length.

    Exactly one form, and the refusals are the mutual exclusivity: both together is a caller who
    believes two different things about which files these are, and neither is a caller who has said
    nothing. They converge here, and everything downstream sees one pair of lists.
    """
    if units is not None and (r1 or r2):
        raise UmiExtractError(
            "--units and --r1/--r2 both name this cell's files. The table states which file was "
            "sequenced where and the paths state only an order, so they cannot be reconciled: pass "
            "one of them"
        )
    if units is not None:
        tagged, mates = paired_fastqs(load_units(units), sample, tagged_role)
        return [Path(p) for p in tagged], None if mates is None else [Path(p) for p in mates]
    if not r1:
        raise UmiExtractError(
            "no input files: pass --units <units.tsv> to read this cell's files off the table that "
            "states them, or --r1 (repeated) to name them directly"
        )
    if r2 and len(r2) != len(r1):
        raise UmiExtractError(
            f"{len(r1)} --r1 files against {len(r2)} --r2 files. Passed directly they pair in the "
            f"order given, so a length disagreement leaves a tagged read with no mate — and every "
            f"pair after the gap joined to the wrong file"
        )
    return list(r1), list(r2) or None


def extract_umis(
    tagged_fastqs: Sequence[Path],
    mate_fastqs: Sequence[Path] | None,
    out: Path,
    geometry: TagGeometry,
    *,
    sample: str,
    summary: Path | None = None,
) -> ExtractStats:
    """One cell's FASTQs, and optionally its mates' -> one uBAM carrying ``UB:Z:``. Returns what it did.

    ``tagged_fastqs`` are the files of the read the layout says carries the tag — usually one, and
    more than one for a cell topped up across two runs, which is the ordinary form of the 20-of-190
    plate deposits that are not strictly 1:1. They are read **in sequence**, in the order handed
    over, and that order is the **Units table**'s (ADR-0036): every fragment of every file
    reaches the uBAM, and nothing is concatenated on disk first.

    ``mate_fastqs`` are their partners **when there are any**: hand over ``None`` for a single-end
    plate and each fragment becomes one unpaired record instead of two interleaved ones. Nothing
    else moves — the mate contributes nothing to the extraction and only inherits the ``UB`` the
    tagged read produced, so the anchor search, the trim and the UMI are the same values either way.
    Given both, ``mate_fastqs[i]`` is the mate of ``tagged_fastqs[i]``: they are paired FILE by file
    and then RECORD by record within each pair, so a run whose read counts disagree with its own
    mate's is refused where it happens rather than absorbed by the next run's surplus.

    A tagged read loses exactly ``geometry.span`` bases from where its anchor was found; its mate is
    untouched, and an untagged read keeps every base it arrived with. Deterministic end to end:
    given the same input this writes the same bytes.

    ``summary`` is where this cell's counts land, and handing over ``None`` skips it — the extraction
    is unchanged either way, and a hand invocation still gets its numbers on stdout. It is a path and
    not a flag because the rule DECLARES it as an output and passes what it declared: a path derived
    here from ``out`` would be a second owner of the filename, and the owner that drifts silently is
    always the one nothing reads back.

    **It writes straight to ``out``, with no temp-and-rename.** A truncated input is only knowable
    once the read has finished, so a refusal can leave a partial BAM behind — and the tempting fix
    is to build it under a sibling name and move it into place. That sibling would be a file the
    rule never declared, in the tree whose undeclared temp files this project has already paid 41
    GiB to be rid of. ``out`` is a declared rule output, and Snakemake deletes the outputs of a
    failed job; the mechanism that owns the file owns cleaning it up.
    """
    if not tagged_fastqs:
        raise UmiExtractError(f"cell {sample!r} was handed no tagged FASTQ to extract from")
    if mate_fastqs is not None and len(mate_fastqs) != len(tagged_fastqs):
        raise UmiExtractError(
            f"cell {sample!r} was handed {len(tagged_fastqs)} tagged files and "
            f"{len(mate_fastqs)} mates; each tagged file is extracted beside its OWN mate, so "
            f"these cannot be zipped"
        )
    mates: Sequence[Path | None] = (
        mate_fastqs if mate_fastqs is not None else [None] * len(tagged_fastqs)
    )
    pairs = list(zip(tagged_fastqs, mates, strict=True))
    out.parent.mkdir(parents=True, exist_ok=True)
    offsets: dict[int, int] = {}
    fragments = tagged = 0
    header = pysam.AlignmentHeader.from_dict(_header(sample))
    with pysam.AlignmentFile(str(out), "wb", header=header) as bam:
        # One BAM around every pair, and the pairs in order: a cell's second run continues the same
        # object rather than producing a second one to merge. The record index below restarts per
        # file, because it is what a refusal names — "record 4100 of a cell" would send a reader
        # looking through the wrong file for it.
        for tagged_fastq, mate_fastq in pairs:
            with ExitStack() as open_files:
                raw1 = open_files.enter_context(open(tagged_fastq, "rb"))
                one = BoundedReader(raw1, _UNBOUNDED)
                two: BoundedReader | None = None
                # The base case: one read, one record, nothing beside it. The mate below replaces
                # this stream rather than the loop that consumes it.
                source: Generator[tuple[Record, Record | None], None, None] = (
                    (rec, None) for rec in one
                )
                if mate_fastq is not None:
                    raw2 = open_files.enter_context(open(mate_fastq, "rb"))
                    two = BoundedReader(raw2, _UNBOUNDED)
                    source = _positionally_paired(
                        one, two, tagged_fastq=tagged_fastq, mate_fastq=mate_fastq
                    )
                # Closed BEFORE the files it reads through, which is what entering it last buys. A
                # refusal leaves this loop mid-stream, and the reader takes its final byte position
                # in its own cleanup: finalised while the handles are still open it MEASURES that
                # position, and finalised after they are gone it records an **Abandoned read**
                # instead (`probe.streaming`). Either way it no longer raises where nothing can
                # catch it — the ordering now buys the accounting rather than the crash.
                stream = open_files.enter_context(closing(source))
                for index, (rec1, rec2) in enumerate(stream):
                    name, seq, qual = _decoded(rec1, path=tagged_fastq, index=index)
                    qname = _query_name(name)
                    match = find_tag(seq, geometry)
                    if match is None:
                        kept_seq, kept_qual, umi = seq, qual, None
                    else:
                        cut = match.start + geometry.span
                        kept_seq, kept_qual, umi = seq[cut:], qual[cut:], match.umi
                        offsets[match.start] = offsets.get(match.start, 0) + 1
                        tagged += 1
                    # The only branch the mate makes, and it is at the write: one unpaired record,
                    # or two interleaved with the tagged read first — the aligner reads a paired
                    # stream back by pairing adjacent records, so the two mates of a fragment must
                    # not be separated. Everything above this line ran on the tagged read alone.
                    written = [(_FLAG_UNPAIRED, kept_seq, kept_qual)]
                    if mate_fastq is not None and rec2 is not None:
                        _, mate_seq, mate_qual = _decoded(rec2, path=mate_fastq, index=index)
                        written = [
                            (_FLAG_READ1, kept_seq, kept_qual),
                            (_FLAG_READ2, mate_seq, mate_qual),
                        ]
                    for flag, base, qc in written:
                        seg = _segment(
                            header, name=qname, seq=base, qual=qc, flag=flag, sample=sample, umi=umi
                        )
                        bam.write(seg)
                    fragments += 1
                _checked(one, tagged_fastq)
                if two is not None and mate_fastq is not None:
                    _checked(two, mate_fastq)
    stats = ExtractStats(
        sample=sample, fragments=fragments, tagged=tagged, offsets=offsets, geometry=geometry
    )
    if summary is not None:
        _write_summary(stats, summary)
    return stats


__all__ = [
    "EXTRACT_SUFFIX",
    "MAX_ANCHOR_DRIFT",
    "TRAILING_MAX_MISMATCH",
    "ExtractStats",
    "TagGeometry",
    "TagMatch",
    "UmiExtractError",
    "extract_metrics",
    "extract_umis",
    "extraction_inputs",
    "find_tag",
    "geometry_for_elements",
    "geometry_for_read",
    "read_extract_summary",
    "tagged_geometry",
    "tagged_read_geometry",
]
