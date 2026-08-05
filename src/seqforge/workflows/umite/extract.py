"""Lift the tagged-molecule UMI out of R1 and write the pair as an unaligned BAM carrying ``UB:Z:``.

Per cell: read the two FASTQs, find the anchor in R1, cut the UMI out, trim the structural prefix,
and emit one uBAM the aligner reads back. Everything below was *run* on ten published GSE207085
cells against the reference package while it was still installed (2026-08-04); none of it is
inferred from documentation.

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

**The search stops at 24, and that bound is mechanistic rather than fitted.** No exact hit anywhere
in 18,901 reads starts past offset 24 — the bound being Tn5 mosaic-end read-through, which is what
puts anything in front of the tag at all. Capping there costs 0 exact hits, and the 113 of 8,976
fuzzy hits it drops (-1.26%) are a purity gain: a tolerant anchor matches spurious 11-mers as deep
as offset 133, at offsets a fixed-offset chemistry cannot produce.

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

``pysam`` is a plain dependency, not a runtime one, and this module needs **no container**: it
shells out to nothing at all. The h5ad packager draws the same line for the same reason — writing a
file is not aligning reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

import pysam

from ...models.dataset import ReadDef, ReadElement, ReadLayout
from ...probe.streaming import BoundedReader, Budget, Record

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

#: The budget this module does not have. Spelled as a value rather than left implicit so that a
#: reader who came here from the bounded-read rule sees the opt-out and its reason (the header
#: docstring) rather than a missing argument.
_UNBOUNDED = Budget(max_reads=2**62, max_bytes=2**62)


class UmiExtractError(RuntimeError):
    """The pair cannot be extracted as handed over: a bad record, a bad pairing, a bad layout."""


# ---- geometry, derived from the element model ----------------------------------------------------
#
# Nothing below is a declared parse key. The chemistry's `parse_keys` stay EMPTY: every number the
# extractor needs is already in the read layout the manifest carries, and deriving it is what keeps
# "what the data is" and "how to read it" from being two facts that can disagree.


@dataclass(frozen=True)
class TagGeometry:
    """Where the tag, the UMI and the cDNA sit, relative to **the anchor's own start**.

    Relative rather than absolute because the anchor floats: it is declared at ``anchor_start`` and
    found anywhere from there to ``anchor_start + MAX_ANCHOR_DRIFT``, so every offset that follows
    it has to travel with it. ``anchor_start`` is the search's lower bound and never a slice index.
    """

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

    @property
    def window(self) -> int:
        """How much of R1 the search may look at — the extraction window.

        Derived, never a literal: the deepest legal match starts at ``anchor_start + drift`` and
        runs for ``span``. For the shipped plate chemistry that is 0 + 24 + 22 = the first 46 bp.
        """
        return self.anchor_start + MAX_ANCHOR_DRIFT + self.span


def _placed(read: ReadDef) -> list[tuple[ReadElement, int, int | None]]:
    """``(element, start, end)`` in declaration order; ``end`` is ``None`` for an open tail.

    A layout may pin every element with a ``start`` or leave them to follow one another; this walks
    the chain so the derivation works either way and never has to ask which style a spec was written
    in. An element of unknown width (the cDNA tail) ends the arithmetic rather than guessing a value.
    """
    placed: list[tuple[ReadElement, int, int | None]] = []
    pos = 0
    for el in read.elements:
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


def geometry_for_read(read: ReadDef) -> TagGeometry:
    """Derive the extraction geometry from one read's elements, or refuse.

    The shape being read out is `` <fixed tag> <UMI> <fixed motif> ... <cDNA> `` with the first three
    contiguous, which is what makes "the match consumes ``span`` bases" true. Adjacency is checked
    rather than assumed: a gap between them means the layout is not this shape, and matching a span
    that straddles a gap would cut the UMI out of the wrong bases while still exiting 0.
    """
    placed = _placed(read)
    umi_at = [i for i, (el, _, _) in enumerate(placed) if el.role == "UMI"]
    if len(umi_at) != 1:
        raise UmiExtractError(
            f"read {read.read_id} declares {len(umi_at)} UMI elements; the tagged-molecule "
            f"extractor needs exactly one"
        )
    i = umi_at[0]
    umi_el, umi_start, umi_end = placed[i]
    if umi_el.length is None or umi_end is None:
        raise UmiExtractError(f"read {read.read_id}'s UMI element declares no length")
    if i == 0:
        raise UmiExtractError(
            f"read {read.read_id} opens with its UMI, so there is no anchor to find it by"
        )
    anchor_el, anchor_start, anchor_end = placed[i - 1]
    if not anchor_el.sequence:
        raise UmiExtractError(
            f"the element before read {read.read_id}'s UMI declares no literal sequence, so there "
            f"is nothing to search for"
        )
    if anchor_end != umi_start:
        raise UmiExtractError(
            f"read {read.read_id}'s anchor ends at {anchor_end} and its UMI starts at {umi_start}; "
            f"the extractor cuts one contiguous span and cannot straddle a gap"
        )
    if i + 1 >= len(placed):
        raise UmiExtractError(
            f"read {read.read_id}'s UMI closes the read; there is no trailing motif to confirm a "
            f"match against, and an anchor alone would tag untagged reads"
        )
    trail_el, trail_start, trail_end = placed[i + 1]
    if not trail_el.sequence or trail_end is None:
        raise UmiExtractError(
            f"the element after read {read.read_id}'s UMI declares no literal sequence"
        )
    if trail_start != umi_end:
        raise UmiExtractError(
            f"read {read.read_id}'s UMI ends at {umi_end} and its trailing motif starts at "
            f"{trail_start}; the extractor cuts one contiguous span and cannot straddle a gap"
        )
    cdna_start = next((s for el, s, _ in placed[i + 2 :] if el.role in ("cDNA", "gDNA")), None)
    if cdna_start is None:
        raise UmiExtractError(f"read {read.read_id} carries a UMI but no cDNA to trim down to")
    return TagGeometry(
        anchor=anchor_el.sequence,
        anchor_start=anchor_start,
        umi_offset=umi_start - anchor_start,
        umi_length=umi_el.length,
        trailing=trail_el.sequence,
        trailing_offset=trail_start - anchor_start,
        cdna_offset=cdna_start - anchor_start,
    )


def tagged_read_geometry(layout: ReadLayout) -> tuple[str, TagGeometry]:
    """``(read_id, geometry)`` for the one read in a layout that carries a tagged-molecule UMI.

    Refuses on none and on more than one, rather than picking. Which read is tagged is a fact about
    the chemistry that the layout already states; a caller that had to name it could name the wrong
    one, and the extractor would then cut a UMI out of a cDNA read.
    """
    tagged = [r for r in layout.reads if any(el.role == "UMI" for el in r.elements)]
    if len(tagged) != 1:
        raise UmiExtractError(
            f"{len(tagged)} of this layout's reads carry a UMI element "
            f"({[r.read_id for r in layout.reads]}); the extractor needs exactly one"
        )
    return tagged[0].read_id, geometry_for_read(tagged[0])


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
    """
    lowest, highest = geometry.anchor_start, geometry.anchor_start + MAX_ANCHOR_DRIFT
    window = seq[: geometry.window]
    stop = highest + len(geometry.anchor)  # `find`'s end is exclusive over the SLICE, not the start
    at = window.find(geometry.anchor, lowest, stop)
    while at != -1:
        umi_from = at + geometry.umi_offset
        trail_from = at + geometry.trailing_offset
        if at + geometry.span < len(seq) and _mismatches_within(
            seq[trail_from : trail_from + len(geometry.trailing)],
            geometry.trailing,
            TRAILING_MAX_MISMATCH,
        ):
            return TagMatch(at, seq[umi_from : umi_from + geometry.umi_length])
        at = window.find(geometry.anchor, at + 1, stop)
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
    pairs: int
    tagged: int
    #: How many tagged reads had their anchor at each offset. The reason the search is unanchored is
    #: a measured offset distribution, so a run that reports its own is a run that can be checked
    #: against it instead of trusted — the 4.3%-not-at-zero claim is observable per cell, forever.
    offsets: dict[int, int]

    @property
    def untagged(self) -> int:
        """Internal reads: no tag, no UMI, nothing trimmed. A third to two thirds of a real library."""
        return self.pairs - self.tagged

    def to_dict(self) -> dict[str, object]:
        """The JSON the verb prints. Offsets are string-keyed because JSON object keys are strings."""
        return {
            "sample": self.sample,
            "pairs": self.pairs,
            "tagged": self.tagged,
            "untagged": self.untagged,
            "offsets": {str(k): self.offsets[k] for k in sorted(self.offsets)},
        }


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
    """One unaligned BAM record. The UMI rides in ``UB``, and nothing rides in the name."""
    segment = pysam.AlignedSegment(header)
    segment.query_name = name
    segment.flag = flag
    # Sequence before qualities, and not the other way round: assigning `query_sequence` discards
    # whatever qualities the record held, so the reverse order writes a record with none.
    segment.query_sequence = seq
    segment.query_qualities = pysam.qualitystring_to_array(qual)
    segment.set_tag("RG", sample, value_type="Z")
    if umi is not None:
        segment.set_tag("UB", umi, value_type="Z")
    return segment


def extract_umis(
    tagged_fastq: Path,
    mate_fastq: Path,
    out: Path,
    geometry: TagGeometry,
    *,
    sample: str,
) -> ExtractStats:
    """One cell's two FASTQs -> one uBAM carrying ``UB:Z:``. Returns what it did.

    ``tagged_fastq`` is the read the layout says carries the tag; ``mate_fastq`` is its partner, and
    the two are paired **by position** — record *n* with record *n*, names never consulted. Files of
    unequal length are refused rather than zipped to the shorter one, because a pair silently
    dropped off the end is a cell that counts low and says nothing.

    A tagged read loses exactly ``geometry.span`` bases from where its anchor was found; its mate is
    untouched, and an untagged read keeps every base it arrived with. Deterministic end to end:
    given the same two files this writes the same bytes.

    **It writes straight to ``out``, with no temp-and-rename.** A truncated input is only knowable
    once the read has finished, so a refusal can leave a partial BAM behind — and the tempting fix
    is to build it under a sibling name and move it into place. That sibling would be a file the
    rule never declared, in the tree whose undeclared temp files this project has already paid 41
    GiB to be rid of. ``out`` is a declared rule output, and Snakemake deletes the outputs of a
    failed job; the mechanism that owns the file owns cleaning it up.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    offsets: dict[int, int] = {}
    pairs = tagged = 0
    with open(tagged_fastq, "rb") as raw1, open(mate_fastq, "rb") as raw2:
        one, two = BoundedReader(raw1, _UNBOUNDED), BoundedReader(raw2, _UNBOUNDED)
        header = pysam.AlignmentHeader.from_dict(_header(sample))
        with pysam.AlignmentFile(str(out), "wb", header=header) as bam:
            for index, (rec1, rec2) in enumerate(zip_longest(one, two)):
                if rec1 is None or rec2 is None:
                    short = mate_fastq if rec2 is None else tagged_fastq
                    raise UmiExtractError(
                        f"{short} runs out at record {index} while its mate does not; the two are "
                        f"paired by position, so a length disagreement is unpairable rather than "
                        f"truncatable"
                    )
                name, seq, qual = _decoded(rec1, path=tagged_fastq, index=index)
                _, mate_seq, mate_qual = _decoded(rec2, path=mate_fastq, index=index)
                qname = _query_name(name)
                match = find_tag(seq, geometry)
                if match is None:
                    kept_seq, kept_qual, umi = seq, qual, None
                else:
                    cut = match.start + geometry.span
                    kept_seq, kept_qual, umi = seq[cut:], qual[cut:], match.umi
                    offsets[match.start] = offsets.get(match.start, 0) + 1
                    tagged += 1
                # Interleaved, tagged read first: the aligner reads this back as a paired stream and
                # pairs adjacent records, so the two mates of a fragment must not be separated.
                for flag, base, qc in (
                    (_FLAG_READ1, kept_seq, kept_qual),
                    (_FLAG_READ2, mate_seq, mate_qual),
                ):
                    seg = _segment(
                        header, name=qname, seq=base, qual=qc, flag=flag, sample=sample, umi=umi
                    )
                    bam.write(seg)
                pairs += 1
        _checked(one, tagged_fastq)
        _checked(two, mate_fastq)
    return ExtractStats(sample=sample, pairs=pairs, tagged=tagged, offsets=offsets)


__all__ = [
    "MAX_ANCHOR_DRIFT",
    "TRAILING_MAX_MISMATCH",
    "ExtractStats",
    "TagGeometry",
    "TagMatch",
    "UmiExtractError",
    "extract_umis",
    "find_tag",
    "geometry_for_read",
    "tagged_read_geometry",
]
