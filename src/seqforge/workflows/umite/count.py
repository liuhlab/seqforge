"""Count UMIs and reads per gene across every cell of a plate, into ONE ``.h5ad``.

This is the fan-in: one counting job over all N per-cell BAMs, not N counting jobs and a merge. It
re-implements what ``umite``'s ``umicount`` decided, and deliberately not how it decided it — the
mechanism is where the reference costs the most and is wrong in the most places.

**It writes the ``.h5ad`` directly, with no TSV in between.** A plate is thousands of cells against
tens of thousands of genes in every matrix below, almost all of it zero — hundreds of megabytes of
dense text to produce a sparse object several times smaller. Writing a sample column to join back on
would also rebuild, for ourselves, the exact trap the reference had to warn about — its rows are
labelled with the BAM's *basename*, suffix and all. This counter is handed each cell's sample id
along with its BAM, so there is nothing to join and nothing to strip.

The object is one row per cell and the matrices this table lists — which is the **only** place that
says how many there are. A count spelled again in prose is the copy that goes stale, and one did:
three sentences claimed four matrices for a release that shipped five, in a file a user reads. Add a
row here and the number follows; there is nothing else to remember.

| in the object | is | the reference's column |
| --- | --- | --- |
| ``X`` | UMIs, exonic | ``UE`` |
| ``layers["umi_intron"]`` | UMIs, intronic | ``UI`` |
| ``layers["umi_combined"]`` | UMIs, exon and intron deduplicated together | ``U`` under ``--combine_unspliced`` |
| ``layers["read_exon"]`` | untagged reads, exonic | ``RE`` |
| ``layers["read_intron"]`` | untagged reads, intronic | ``RI`` |
| ``layers["umi_multimapping_placement"]`` | UMIs of multiply-placed fragments, over gene bodies | — |

``obs`` is indexed by **sample id**, which is what makes the h5ad row and the per-cell CRAM filename
join, and the four read fates are ``obs`` **columns**. The reference carries them as four extra
*gene* columns (``_unmapped``, ``_multimapping``, ``_no_feature``, ``_ambiguous``) in a table whose
other 55 335 columns are genes — a per-cell scalar dressed as a feature, which is what forced a
correction in its output shape. As columns they need no leading underscore either: the underscore
was there to keep them out of the gene id namespace, and they are not in it any more. Sequencing
saturation is a column beside them, what the cell YIELDED — its molecules and how many genes hold
one — is two more, and how many loci each multiply-placed fragment had is an ``obsm`` array: a
per-cell vector rather than a scalar, so it is the one figure ``obs`` cannot hold.

**A matrix is materialised when it cannot be derived from the others, and only then.** That is why
the combined UMI matrix is here and a combined *read* matrix is not: reads carry no UMI and are
never deduplicated (``umicount.py:407``), so ``read_exon + read_intron`` is exact arithmetic anybody
can do on the object, while the combined UMI figure is a *third* deduplication that neither of the
other two contains — see :func:`deduplicate`. The reference's remaining table, ``D`` (per-gene PCR
duplicates), is dropped rather than derived — the rule above does not reach it: the object carries
deduplicated counts and never the per-gene raw UMI observation totals ``D`` is the remainder of, and
this verb writes nothing else, so no arithmetic on the object recovers it. ``n_fragments`` on
``obs`` is what makes the four fates readable as rates.

**The annotation comes from the database ``liulab-genome`` already built** — no HTSeq, no GTF parse,
no per-worker copy. The reference parses the GTF into two HTSeq ``GenomicArrayOfSets`` and pickles
them; that pickle is 47.5 MB and is serialised into every worker, which at 1440 cells is ~76 GB
through pipes for an object that never changes, on top of a 50 s parse. Reading gffutils' SQLite
once, into the step index below, deletes both — and it is the largest single win in this port,
obtained by declining to reproduce the architecture rather than by optimising it.

**The plate is counted on every core the rule asked for, and there is still no per-worker copy.**
The cells are independent, so :func:`count_plate` fans out over them; the workers are forked, and a
forked worker INHERITS the annotation instead of being sent one. That is the opposite of the
architecture refused above rather than a return to it — nothing is serialised, and what a worker
adds is only the pages it dirties by touching the interned sets, measured as a ceiling of ~75 MB
that a cell reaches within its first twenty thousand fragments and never exceeds. Where fork is not
on offer the plate is counted serially rather than under ``spawn``, which would re-import this
module and pickle the annotation into every worker on every cell.

Note that the reader is **gffutils, not pysam**: pysam reads the alignments, and the built database
is a gffutils SQLite file, which is gffutils' format to read. This module takes the resolved
database path and never an assembly id, exactly as ``cram.py`` takes a resolved FASTA — the
``liulab-genome`` import lives in the (untyped) CLI verb, which keeps this module strictly typed and
unit-testable against a synthetic annotation built in a temp directory.

**Multimappers come from the ``NH`` tag.** The reference classifies a read pair as multimapping
purely from bundle length, and never reads ``NH`` at all. Under ``--outSAMmultNmax 1`` — a settled
module literal — STAR emits exactly one record per multimapper, so every bundle is length 1 and
every multimapper is counted into a gene: measured on the frozen fixture, ``_multimapping`` is
**0 for all ten cells** while 1 640 of 12 977 aligned read names (12.6%) carry ``NH > 1``, and the
same cell realigned with the flag as the only difference gains **+10.2%** on its primary UMI matrix
— more than the entire fuzzy-matching improvement the tool is published for. Reading ``NH``
reproduces the reference's *intent* rather than its mechanism, is immune to the aligner flag, and
lets the flag stay.

**And they are attributed as well as excluded.** They are 6.1%–40.5% of a plate's fragments across
libraries of one strain, one protocol and one sequencing run, and nothing said which genes were
absorbing them. So the branch that excludes them also does the gene-body lookup and accumulates into
a layer of its own — over BODIES rather than exons, because the question is which gene these are in
and an intronic one should still be attributed, and because the gene-body figure is already a matrix
beside it (``umi_combined``) so the ratio between them is one division. What that layer counts is a
**placement**, which is the caveat its name and :data:`MULTIMAPPING_CAVEAT` both carry: a count in a
gene says one of the loci this fragment could have come from is that gene, never that the fragment is
that gene's. Nothing about the other matrices moves — the layer is written from the branch that
already returned, so a fragment excluded from expression is still excluded from it.

**The input is coordinate-sorted, so there is no name adjacency to pair on.** There is exactly one
sort in this pipeline and adding a name sort would cost a full extra pass and double the peak disk,
because every per-cell BAM has to survive until the fan-in finishes. So nothing here reconstructs a
pair. Each fragment is represented by exactly one record — its first mate — and the *mate
coordinates that record already carries* stand in for the second: see :func:`_fragment_span`. One
record per fragment is what keeps both mates from being counted twice in the read matrices, and it
is also what keeps the reference's per-fragment UMI observation counts intact, which the correction
step's count ratio is sensitive to.

Unstranded, one rule for tagged and internal reads alike — which is what the reference does
(``GenomicArrayOfSets("auto", False)``, and no strand branch anywhere in it), whatever the published
analysis did. UMI correction is always on. Everything is deterministic: no unseeded choice, and
every iteration order is sorted rather than inherited from a dict.
"""

from __future__ import annotations

import array
import multiprocessing
import sqlite3
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from math import isnan
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix

# Aliased for the reason `fragments.py` aliases it: `count_bam`/`count_plate` already use the word
# for what this module does, and a helper that silently means something else inside one function is
# how a wrong number gets written.
from ..metrics import (
    Metric,
    MetricGroup,
    SampleStats,
    fraction,
    genes_detected,
    sequencing_saturation,
)
from ..metrics import count as count_metric

if TYPE_CHECKING:  # pragma: no cover — both are runtime deps; keeps import cost off compose
    import anndata
    from pysam import AlignedSegment


class UmiCountError(RuntimeError):
    """The plate cannot be counted as asked (missing BAM, unreadable annotation, no genes)."""


#: The BAM tag the UMI rides in. Never the read name: the reference took everything after the last
#: underscore, which for an untagged read is whatever the sequencer put there — and untagged reads
#: are 32-68% of every SMART-seq3 library. A tag is absent or present; a name suffix always parses.
UMI_TAG = "UB"

#: The tag STAR writes with the number of loci a read aligned to. Absent means one.
HITS_TAG = "NH"

#: UMI correction's count guard, a module literal rather than a flag. Correction is always on, and
#: this is the reference's guard on it — a low-count neighbour is only absorbed into a seed at least
#: roughly twice as abundant, so two genuinely distinct UMIs at similar depth are never merged. The
#: *distance* has no constant beside this one: see :func:`correct_umis`, where it is the shape of a
#: key rather than a number anything compares against.
COUNT_RATIO_THRESHOLD = 2

#: What ``X`` is, and what the other matrices are called. The reference's names for these are
#: ``UE``/``UI``/``RE``/``RI``; these say the same thing to somebody who has never read it.
#:
#: ``umi_combined`` follows that same rule and is this repo's own word rather than either tool's:
#: the practice survey in ``docs/research/`` heads the very grid these matrices come from *exonic |
#: intronic | combined*, so the word is already the one this project reasons in. zUMIs calls it
#: ``inex`` and the reference calls it ``U`` (under ``--combine_unspliced``); both are names you have
#: to have read the tool to expand, which is exactly what the sentence above declines to ship.
#:
#: ``umi_multimapping_placement`` is the odd one out and says so in its own name. Every other matrix
#: here is expression; that one is where a fragment the counter refused to credit to any gene was
#: PLACED, which is a different kind of number under the same axes. ``placement`` is the word that
#: keeps the two apart at the point a reader picks a layer — a name spelling only the population
#: (``umi_multimapping``) invites exactly the addition :data:`MULTIMAPPING_CAVEAT` forbids.
PRIMARY_MATRIX = "umi_exon"
MULTIMAPPING_LAYER = "umi_multimapping_placement"
LAYERS: tuple[str, ...] = (
    "umi_intron",
    "umi_combined",
    "read_exon",
    "read_intron",
    MULTIMAPPING_LAYER,
)

#: The sentence that has to travel WITH that layer. The caveat is on the object — ``uns`` — and not
#: only in this file, because the object outlives the session that wrote it and reaches readers who
#: will never open this module. It names the layer by substitution rather than by repeating the
#: string, so the two cannot drift apart.
MULTIMAPPING_CAVEAT = (
    f"{MULTIMAPPING_LAYER} counts a deduplicated molecule in a gene when ONE of the loci its "
    f"fragment could have come from is that gene's body. It is where ambiguity was placed, not "
    f"expression: never add it to {PRIMARY_MATRIX} or to any other layer, and read it as a share "
    f"of umi_combined on the same gene."
)

#: How many loci each multiply-placed fragment had, per cell: an ``obsm`` array whose column ``n`` is
#: how many of that cell's fragments carried ``NH == n``. The locus count IS the column index, so
#: nothing has to ship a second array of labels for a reader to know what a column means — and
#: columns 0 and 1 are structurally empty, which is the price of that and is a few bytes a cell.
MULTIMAPPING_HITS = "multimapping_hits"

#: The four ways a fragment fails to reach a gene, in the order they are decided. Per-cell scalars,
#: so they are ``obs`` columns; :data:`N_FRAGMENTS` is here so they can be read as rates.
FATES: tuple[str, ...] = ("unmapped", "multimapping", "no_feature", "ambiguous")
N_FRAGMENTS = "n_fragments"

#: Sequencing saturation, per cell: one minus this cell's deduplicated molecules over the fragments
#: that reached a gene carrying a UMI. An ``obs`` column rather than a key on the per-cell QC bundle,
#: because the bundle is written a rule EARLIER than the counter and physically cannot carry a number
#: only deduplication produces. A cell with no such fragment has no saturation rather than a zero.
SATURATION = "saturation"

#: What a cell YIELDED: its molecules, and how many genes any of them reached. Every column above is
#: an account of how a fragment FAILED to reach a gene, so an object carrying only those says what
#: went wrong and never what came out — and "did this well work" is the first question anybody asks
#: of a plate.
#:
#: **Both are counted over ``umi_combined`` and never over ``X``.** That layer is the only one
#: counting a UMI seen both exonically and intronically on a gene ONCE, which is the same argument
#: :func:`_saturation` rests on — so the molecule total and the saturation beside it are two
#: readings of one number rather than two derivations nothing forces to agree. The gene total is
#: that layer's length, which is "genes with at least one molecule" exactly: :func:`_combined_umis`
#: files a gene only when its merged bucket is non-empty, and a non-empty bucket corrects to at
#: least one UMI.
#:
#: :data:`GENES_DETECTED` is spelled as the shared metric's own key because a column and the metric
#: read off it are one string everywhere on this path; a second word for it would be a rename
#: nothing catches.
N_UMIS = "n_umis"
GENES_DETECTED = "genes_detected"


# --------------------------------------------------------------------------------------------
# The annotation, read once
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _StepIndex:
    """One contig's intervals as a step function: segment starts, and the gene set on each segment.

    This is HTSeq's ``GenomicArrayOfSets`` in two flat buffers and a tuple. ``starts`` is ascending
    and begins at 0, so a search always lands inside it; ``set_ids[i]`` names the genes covering
    ``[starts[i], starts[i + 1])``.

    **The set ids are the whole reason this fits in memory.** A gencode-scale annotation has ~841 000
    exons, so a step vector over them has ~1.7M segments — and one Python ``frozenset`` per segment
    would cost hundreds of MB for an object where almost every segment names the same one gene.
    Interning collapses that to ~60 000 distinct sets behind an int32 array, which is a few MB. The
    alternative — an interval tree per contig — is a comparable amount of code and answers a
    question we do not ask (which interval), rather than the one we do (which gene set).

    ``starts`` is an ``array.array`` of int64 rather than a numpy array because :meth:`genes` is the
    busiest path in the counter — once per fragment of every cell — and ``bisect`` over a plain
    buffer beats ``np.searchsorted`` at byte-for-byte the same resident size. A ``list[int]`` is
    faster still and is refused: it pays a boxed integer and a pointer per element where a buffer
    pays eight bytes, so it costs five times the memory for the same numbers — twice over, since
    every contig carries a body index and an exon index — to buy a fraction of a microsecond.
    Holding a numpy array and taking ``starts.searchsorted``, the bound method, is the same two
    lines here and the same one line below, which is what keeps that a decision the cluster can
    still make; the measurement behind the choice is in ``docs/research/``.
    """

    starts: array.array[int]
    set_ids: np.ndarray
    sets: tuple[frozenset[int], ...]

    def genes(self, start: int, end: int) -> frozenset[int]:
        """Every gene covering any part of ``[start, end)``. Empty is a legal answer.

        A span that touches exactly one segment — most of them, since a fragment is far shorter than
        the stretch between two annotation boundaries — is answered with the interned set itself
        rather than with a copy of it. That is safe because the interned set is a ``frozenset``: the
        caller holds the index's own object and has no way to change it.
        """
        if end <= start:
            return frozenset()
        first = bisect_right(self.starts, start) - 1
        last = bisect_left(self.starts, end)
        if first < 0:
            first = 0
        if last - first == 1:
            return self.sets[int(self.set_ids[first])]
        found: set[int] = set()
        for i in range(first, last):
            found |= self.sets[int(self.set_ids[i])]
        return frozenset(found)


def _step_index(intervals: Sequence[tuple[int, int, int]]) -> _StepIndex:
    """``(start, end, gene)`` triples -> a step index over one contig. Half-open, 0-based."""
    events: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for start, end, gene in intervals:
        if end <= start:  # a zero-length feature covers nothing and would open a segment it closes
            continue
        events[start].append((1, gene))
        events[end].append((-1, gene))

    starts: list[int] = [0]
    set_ids: list[int] = [0]
    interned: dict[frozenset[int], int] = {frozenset(): 0}
    sets: list[frozenset[int]] = [frozenset()]

    depth: dict[int, int] = defaultdict(int)
    for coord in sorted(events):
        for delta, gene in events[coord]:
            depth[gene] += delta
            if depth[gene] == 0:
                del depth[gene]
        active = frozenset(depth)
        set_id = interned.get(active)
        if set_id is None:
            set_id = len(sets)
            interned[active] = set_id
            sets.append(active)
        if coord == starts[-1]:  # several features begin or end at the same base
            set_ids[-1] = set_id
        else:
            starts.append(coord)
            set_ids.append(set_id)

    return _StepIndex(
        starts=array.array("q", starts),
        set_ids=np.array(set_ids, dtype=np.int32),
        sets=tuple(sets),
    )


@dataclass(frozen=True)
class Annotation:
    """Gene bodies and exons, indexed for overlap, plus the gene axis the matrices are written on.

    A contig that carries no feature at all is simply **absent** from both dicts and answers with
    the empty set. That is the first of the two traps this port had to reproduce rather than
    re-derive: the reference looks the contig up in an HTSeq array, catches the ``KeyError``, and
    reclassifies the read **``_unmapped``** with a warning — but a read that aligned is not
    unmapped, it is on a scaffold nothing annotates, which is ``_no_feature``. It is a handful of
    reads per cell and invisible in a summary, which is exactly why it is worth getting right.
    """

    gene_ids: tuple[str, ...]
    gene_names: tuple[str, ...]
    bodies: Mapping[str, _StepIndex]
    exons: Mapping[str, _StepIndex]

    def gene_bodies(self, contig: str, start: int, end: int) -> frozenset[int]:
        index = self.bodies.get(contig)
        return frozenset() if index is None else index.genes(start, end)

    def exonic(self, contig: str, start: int, end: int) -> frozenset[int]:
        index = self.exons.get(contig)
        return frozenset() if index is None else index.genes(start, end)


def read_annotation(db: Path) -> Annotation:
    """The ``<name>.db`` ``liulab-genome`` built for a registered GTF -> an overlap index.

    Paid **once per plate**, not once per cell, which is the point: the reference pays a 50 s parse
    and then serialises a 47.5 MB pickle into every one of its workers.

    The rows are pulled through ``features_of_type``, gffutils' own documented reader, rather than
    with a hand-written ``SELECT`` over its ``features`` table. The SELECT is faster — it skips
    building a ``Feature`` per row — but the attribute column is gffutils' own encoding, so reading
    it directly would mean this module owning a schema somebody else writes and can change.

    GTF is 1-based inclusive and every coordinate below this line is 0-based half-open, matching
    what pysam reports for an alignment. The conversion happens here, once, on the way in.
    """
    import gffutils

    if not db.exists():
        raise UmiCountError(
            f"annotation database {db} does not exist; liulab-genome builds it when the GTF is "
            f"registered, so a missing one means the annotation was never registered for this "
            f"assembly"
        )
    # Whether this is a SQLite file at all is asked here, of sqlite3, and not left to gffutils --
    # which opens a connection in its constructor and does not close it when the open fails, so a
    # file that is not a database costs a leaked handle and an error message about a format nobody
    # named. `PRAGMA schema_version` forces the read without naming a table, so nothing here knows
    # gffutils' schema.
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as probe:
        try:
            probe.execute("PRAGMA schema_version")
        except sqlite3.DatabaseError as exc:
            raise UmiCountError(
                f"{db} is not a readable annotation database ({exc}); liulab-genome writes one "
                f"with gffutils when a GTF is registered"
            ) from exc
    database = None
    try:
        database = gffutils.FeatureDB(str(db))
        gene_ids: list[str] = []
        gene_names: list[str] = []
        index: dict[str, int] = {}
        bodies: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
        for feature in database.features_of_type("gene"):
            gene_id = _attribute(feature, "gene_id")
            if gene_id is None or gene_id in index:
                continue
            index[gene_id] = len(gene_ids)
            gene_ids.append(gene_id)
            gene_names.append(_attribute(feature, "gene_name") or "")
            bodies[feature.seqid].append((feature.start - 1, feature.end, index[gene_id]))

        exons: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
        for feature in database.features_of_type("exon"):
            gene_id = _attribute(feature, "gene_id")
            # An exon whose gene has no `gene` line of its own has no column to be counted into.
            # Skipping it keeps the two indexes over the same gene axis; counting it would need a
            # gene body this annotation never declared.
            if gene_id is None or gene_id not in index:
                continue
            exons[feature.seqid].append((feature.start - 1, feature.end, index[gene_id]))
    except UmiCountError:
        raise
    except Exception as exc:  # gffutils raises its own errors, and sqlite3 raises through it
        raise UmiCountError(f"{db} is not a readable annotation database: {exc}") from exc
    finally:
        # gffutils never closes the connection it opens, and this process outlives the read by the
        # whole fan-in — 1440 cells' worth of counting with a SQLite handle held open on an
        # annotation nothing will ask again. `register_gtf` closes its own for the same reason.
        if database is not None:
            database.conn.close()

    if not gene_ids:
        raise UmiCountError(
            f"{db} declares no `gene` features, so there is no gene axis to count on"
        )

    return Annotation(
        gene_ids=tuple(gene_ids),
        gene_names=tuple(gene_names),
        bodies={contig: _step_index(rows) for contig, rows in sorted(bodies.items())},
        exons={contig: _step_index(rows) for contig, rows in sorted(exons.items())},
    )


def _attribute(feature: object, key: str) -> str | None:
    """One GTF attribute off a gffutils feature. gffutils stores every value as a list."""
    values = getattr(feature, "attributes", {}).get(key, [])
    return str(values[0]) if values else None


# --------------------------------------------------------------------------------------------
# One cell
# --------------------------------------------------------------------------------------------


@dataclass
class CellCounts:
    """What one BAM came to. UMI buckets hold observations per UMI; dedup happens at the end.

    ``umi_exon``/``umi_intron`` are kept as ``gene -> umi -> observations`` rather than as sets
    because the correction step needs the counts: a neighbour is absorbed into a seed only when the
    seed is the more abundant of the two, and a set has thrown that away.

    ``umi_multimapping`` is the same shape for the fragments none of the three above may hold, and
    there is one bucket rather than an exonic and an intronic one because its assignment is the gene
    BODY: a multiply-placed fragment inside an intron is in the gene, and splitting it by exon would
    be asking a question about a locus nobody claims the fragment came from.

    ``umi_fragments`` is the saturation denominator and is counted where the assignment is made
    rather than derived afterwards — it is the fragments that reached a gene carrying a UMI, which no
    matrix recovers once each bucket has been deduplicated.
    """

    umi_exon: dict[int, dict[str, int]]
    umi_intron: dict[int, dict[str, int]]
    read_exon: dict[int, int]
    read_intron: dict[int, int]
    umi_multimapping: dict[int, dict[str, int]]
    fates: dict[str, int]
    n_fragments: int
    umi_fragments: int
    #: How many loci each multiply-placed fragment claimed: ``NH`` -> how many fragments carried it.
    hits: dict[int, int]


def _fragment_span(record: AlignedSegment) -> tuple[int, int]:
    """The reference interval this fragment covers, from ONE of its two records.

    The reference resolves a pair by looking up each mate's own interval and preferring a gene both
    mates hit exonically. It can, because it reads a name-sorted BAM and gets the pair for free.
    This reads a coordinate-sorted one, where the mate is thousands of records away, so the second
    mate's *footprint* is recovered from ``TLEN`` instead: for a proper pair the template length is
    the whole fragment, signed by which mate is leftmost, so either record alone yields the same
    span. That covers the mate; what it additionally covers is the inner gap between the two, which
    the reference's union of the two mate intervals does not — and neither excludes the introns a
    spliced mate skips, because the reference's per-mate interval spans its own ``N`` gaps too.

    Anything that is not a proper same-contig pair — a mate on another chromosome, a zero ``TLEN``,
    an unpaired record — falls back to the record's own footprint. So does a ``TLEN`` that
    disagrees with it: the span is unioned with the record's own alignment, so a malformed template
    length can only ever widen the window, never move it off the read.
    """
    start = int(record.reference_start)
    end = int(record.reference_end if record.reference_end is not None else start + 1)
    tlen = int(record.template_length or 0)
    if not (record.is_paired and record.next_reference_id == record.reference_id and tlen):
        return start, end
    if tlen > 0:
        return start, max(end, start + tlen)
    return min(start, end + tlen), end


def _representative(record: AlignedSegment) -> bool:
    """Whether this record is the one that stands for its whole fragment.

    Exactly one record per fragment reaches the matrices, and this is the choice of which. First
    mate, because the flag is on every record and needs nothing else to be read; an unpaired record
    is its own fragment. Secondary and supplementary alignments are dropped before this — a
    secondary record re-states a read at a locus we did not choose to believe, and its primary is
    already carrying the ``NH`` that says so.
    """
    return (not record.is_paired) or bool(record.is_read1)


def correct_umis(observations: Mapping[str, int]) -> dict[str, int]:
    """Merge each UMI into the more abundant near-neighbour that can explain it as a PCR error.

    The reference's rule, kept exactly: take the most abundant UMI as a seed and absorb every UMI
    still standing that is one substitution away *and* rare enough for it to explain
    (``COUNT_RATIO_THRESHOLD * candidate - 1 <= seed``). Repeat with what is left.

    **One substitution, and there is no threshold constant to raise.** The distance is realised by
    blanking exactly one position, so it is the shape of the key rather than a number compared
    against — which is where it belongs, because it was never a knob: at 3 the trailing check is
    vacuous and the merge manufactures UMIs that were never sequenced. Widening it means indexing a
    different key, not editing a literal.

    **The neighbours are looked up, not searched for**, and that is the only difference from the
    reference — which compares the seed against every surviving UMI in the bucket, so a deep gene of
    a deep cell costs seconds. Blanking one position of a UMI leaves a key that its one-substitution
    neighbours share and nothing else does, so a seed reads its neighbours out of a dict. Two
    properties make that the *same* function rather than an approximation of it. The reference stops
    its walk at the first candidate too abundant to absorb, and since it walks in count order
    everything past that point fails the same arithmetic — so the stop is the count test, spelled as
    control flow. And the seed's own count is never raised as it absorbs, so which neighbour it
    takes first cannot change what it ends up holding.

    A key is a position and the two fragments that survive blanking it, so it carries the UMI's
    length and two UMIs of different lengths can never share one. That is this port's refusal to
    merge a ragged tag — the reference asks rapidfuzz for a distance with padding off, which raises
    on a length mismatch — now carried by the index's shape instead of by a rule saying so.

    Each UMI's keys are held rather than rebuilt when it comes up as a seed. That is what puts the
    index ahead of the scan from eleven UMIs upward, which is small enough that the buckets below it
    are not worth a size threshold and a second path through this function: measured over one cell's
    worth of genes they are most of them by count and a rounding error of its correction time.

    **Ties are broken on the UMI itself, not on the order the BAM happened to hand them over.** The
    reference sorts by count alone and lets Python's stable sort fall back to insertion order, which
    is read order — and read order here is coordinate order rather than the name order it was
    written against, so inheriting it would be inheriting somebody else's accident. Sorting equal
    counts by sequence makes the result a function of the data alone, which is what lets the same
    plate counted twice come out byte-identical.
    """
    order = sorted(observations.items(), key=lambda item: (-item[1], item[0]))
    neighbours: dict[tuple[int, str, str], list[str]] = {}
    blanked: dict[str, list[tuple[int, str, str]]] = {}
    for umi, _ in order:
        keys = [(i, umi[:i], umi[i + 1 :]) for i in range(len(umi))]
        for key in keys:
            neighbours.setdefault(key, []).append(umi)
        blanked[umi] = keys

    standing = set(observations)
    corrected: dict[str, int] = {}
    for seed, seed_count in order:
        if seed not in standing:
            continue  # already absorbed by an abundant enough seed
        standing.discard(seed)
        total = seed_count
        for key in blanked[seed]:
            for candidate in neighbours[key]:
                if candidate not in standing:
                    continue
                candidate_count = observations[candidate]
                if (COUNT_RATIO_THRESHOLD * candidate_count) - 1 <= seed_count:
                    total += candidate_count
                    standing.discard(candidate)
        corrected[seed] = total
    return corrected


def count_bam(bam: Path, annotation: Annotation) -> CellCounts:
    """One cell's coordinate-sorted BAM -> its counts, streamed.

    The assignment rule is the reference's ``evaluate_overlap`` specialised to one interval per
    fragment, and the specialisation is exact rather than approximate. With two mate intervals that
    function prefers the genes both mates hit exonically, falls back to the union of the genes
    either mate hits, and calls the fragment ambiguous unless exactly one gene owns the exons. Give
    it a single interval and every one of those branches collapses to:

        exonic genes?  exactly one -> count it exonically; more than one -> ambiguous
        else genes?    exactly one -> count it intronically; more than one -> ambiguous
        else                       -> no feature

    An exon lies inside its own gene body, so an overlapping exon always implies an overlapping
    gene, which is why the exonic set can be consulted first and the intronic case is only ever
    reached when nothing exonic overlapped at all.
    """
    import pysam

    counts = CellCounts(
        umi_exon=defaultdict(dict),
        umi_intron=defaultdict(dict),
        read_exon=defaultdict(int),
        read_intron=defaultdict(int),
        umi_multimapping=defaultdict(dict),
        fates=dict.fromkeys(FATES, 0),
        n_fragments=0,
        umi_fragments=0,
        hits=defaultdict(int),
    )
    if not bam.exists():
        raise UmiCountError(
            f"{bam} is missing; the alignment step that should have written it did not"
        )

    try:
        with pysam.AlignmentFile(str(bam), "rb", check_sq=False) as alignments:
            for record in alignments.fetch(until_eof=True):
                if record.is_secondary or record.is_supplementary:
                    continue
                # A paired record that says it is neither mate cannot be assigned to a fragment,
                # and the failure would be invisible: every count in the cell would simply be
                # missing, at a plausible-looking magnitude, with nothing raised.
                if record.is_paired and not (record.is_read1 or record.is_read2):
                    raise UmiCountError(
                        f"{bam} has a paired record ({record.query_name}) flagged as neither first "
                        f"nor second mate, so nothing can say which of the two it is"
                    )
                if not _representative(record):
                    continue
                counts.n_fragments += 1
                _count_fragment(record, annotation, counts)
    except UmiCountError:
        raise
    except OSError as exc:
        raise UmiCountError(f"{bam} could not be read: {exc}") from exc
    return counts


def _count_fragment(record: AlignedSegment, annotation: Annotation, counts: CellCounts) -> None:
    """Decide one fragment's fate and, if it has one, its gene and its bucket."""
    # Both mates aligned or the fragment is unmapped -- the reference's rule, read off the flags
    # this record already carries rather than off the pair it no longer has. A fragment whose two
    # records the aligner omitted entirely is invisible here, and that is a property of the BAM:
    # the modules that feed this counter ask STAR to write what it could not place WITHIN its
    # output, which is what makes this branch a measurement instead of one that never fires.
    if record.is_unmapped or (record.is_paired and record.mate_is_unmapped):
        counts.fates["unmapped"] += 1
        return
    # Absent means one locus. The tag, not the bundle length: with one record emitted per
    # multimapper the bundle is length 1 and the reference calls it unique.
    if record.has_tag(HITS_TAG) and (hits := int(record.get_tag(HITS_TAG))) > 1:
        counts.fates["multimapping"] += 1
        counts.hits[hits] += 1
        _place_multimapping(record, annotation, counts)
        return

    contig = str(record.reference_name)
    start, end = _fragment_span(record)
    exonic = annotation.exonic(contig, start, end)
    if exonic:
        if len(exonic) > 1:
            counts.fates["ambiguous"] += 1
            return
        gene, spliced = next(iter(exonic)), True
    else:
        bodies = annotation.gene_bodies(contig, start, end)
        if not bodies:
            counts.fates["no_feature"] += 1
            return
        if len(bodies) > 1:
            counts.fates["ambiguous"] += 1
            return
        gene, spliced = next(iter(bodies)), False

    umi = str(record.get_tag(UMI_TAG)) if record.has_tag(UMI_TAG) else ""
    if umi:
        counts.umi_fragments += 1
        bucket = counts.umi_exon if spliced else counts.umi_intron
        bucket[gene][umi] = bucket[gene].get(umi, 0) + 1
    elif spliced:
        counts.read_exon[gene] += 1
    else:
        counts.read_intron[gene] += 1


def _place_multimapping(record: AlignedSegment, annotation: Annotation, counts: CellCounts) -> None:
    """Where a multiply-placed fragment's REPRESENTATIVE locus fell, if exactly one gene owns it.

    Called from the branch that has already decided this fragment's fate, and it decides nothing:
    the fate stays ``multimapping``, no other bucket is touched, and everything the primary matrices
    hold is what they held before this function existed. That is the property that makes attributing
    ambiguity safe at all — the measured inflation excluding these fragments prevents (+10.2% on one
    real cell's primary UMI matrix) is untouched, because exclusion is still what happens to them.

    **The ambiguity rule is the one the matrices already use**, not a second one: a span over more
    than one gene body belongs to none of them, so it is dropped here exactly as it would be dropped
    from expression. There is no exonic branch, because the body is the question — and no read
    matrix, because a fragment with nothing to deduplicate by cannot be compared against the
    deduplicated matrix this layer exists to be divided by.
    """
    umi = str(record.get_tag(UMI_TAG)) if record.has_tag(UMI_TAG) else ""
    if not umi:
        return
    start, end = _fragment_span(record)
    bodies = annotation.gene_bodies(str(record.reference_name), start, end)
    if len(bodies) != 1:
        return
    bucket = counts.umi_multimapping[next(iter(bodies))]
    bucket[umi] = bucket.get(umi, 0) + 1


def _combined_umis(counts: CellCounts) -> dict[int, int]:
    """The combined UMI matrix: one deduplication over the union of the two buckets, per gene.

    **The raw observation counts are merged BEFORE correction, and that ordering is the whole
    definition.** It is what the reference does — under ``--combine_unspliced`` the bucket key stays
    ``'U'`` for an intronic assignment too (``umicount.py:401``), so both populations land in one
    bucket and ``umi_correction`` then runs once over it (``umicount.py:437-448``) — and it is not a
    detail one can shuffle: correction is Hamming-1 *with a count-ratio test*, so a UMI's abundance
    decides which neighbours it can absorb, and in the union its abundance is the sum of the two.

    Which makes this matrix genuinely non-derivable, in two independent ways, both of which a port
    can get wrong while looking right. On one gene of one cell with UMIs ``A``/``B`` one substitution
    apart, ``A``x2 ``B``x2 exonically and ``A``x5 ``B``x1 intronically: the exon bucket corrects to
    two UMIs (2*2-1 > 2, so ``B`` survives), the intron bucket to one (2*1-1 <= 5, so ``B`` is
    absorbed), and the union — ``A``x7 ``B``x3 — to **one**. A port that adds the two matrices
    reports 3; a port that corrects each bucket and unions the surviving *keys* reports 2; only
    merging the counts first reports 1. Neither wrong answer is reachable from the object, which is
    why this is a fifth matrix rather than a note telling the reader to add two columns.

    ``.get`` and not ``[]``: both buckets are ``defaultdict``s, and subscripting one for a gene that
    is only in the other would insert an empty dict into the very mapping :func:`deduplicate` reads
    beside this. Genes are walked in sorted order for the reason everything here is — the same plate
    counted twice must come out byte-identical, and a dict's order is the BAM's.
    """
    combined: dict[int, int] = {}
    for gene in sorted(set(counts.umi_exon) | set(counts.umi_intron)):
        merged = dict(counts.umi_exon.get(gene, {}))
        for umi, observations in counts.umi_intron.get(gene, {}).items():
            merged[umi] = merged.get(umi, 0) + observations
        if merged:
            combined[gene] = len(correct_umis(merged))
    return combined


def deduplicate(counts: CellCounts) -> dict[str, dict[int, int]]:
    """Every matrix's non-zero entries for one cell, as ``matrix -> gene -> value``.

    **Each UMI bucket is deduplicated on its own**, which is the second trap this port reproduces
    rather than re-derives: one UMI seen both exonically and intronically on the same gene counts
    once in each, so the combined figure is **not** ``exon + intron``. The frozen agreement fixture
    cannot tell the two apart — at 2 000 reads a cell the combined figure equalled the sum exactly
    on all ten cells — so a port that deduplicated over the union would have passed it while
    quietly losing a count per gene wherever the depth was real.

    The combined figure is therefore carried as its own matrix rather than left to be added up by
    whoever opens the object: :func:`_combined_umis` is a third deduplication, over the union of the
    two buckets' RAW counts, and no arithmetic over the two published ones recovers it.

    The read matrices have no such third form and deliberately get no layer. An untagged read
    carries nothing to deduplicate by and the reference never tries (``umicount.py:407``), so
    ``read_exon + read_intron`` is exact — a derivable layer would be one more matrix to write, keep
    consistent and explain, in exchange for an addition.

    The multiply-placed fragments are deduplicated the same way and by the same function, which is
    what makes their layer divisible by ``umi_combined``: two matrices of molecules over one gene
    axis, differing only in whether the aligner could say where the molecule came from.
    """
    return {
        PRIMARY_MATRIX: {g: len(correct_umis(u)) for g, u in counts.umi_exon.items() if u},
        "umi_intron": {g: len(correct_umis(u)) for g, u in counts.umi_intron.items() if u},
        "umi_combined": _combined_umis(counts),
        "read_exon": {g: n for g, n in counts.read_exon.items() if n},
        "read_intron": {g: n for g, n in counts.read_intron.items() if n},
        MULTIMAPPING_LAYER: {
            g: len(correct_umis(u)) for g, u in counts.umi_multimapping.items() if u
        },
    }


def _saturation(molecules: int, umi_fragments: int) -> float | None:
    """One minus this cell's molecules over the gene-assigned fragments that carried a UMI.

    **The droplet pipeline's definition, not a second one under its name.** STARsolo reports the
    same ratio over the same population — reads with a usable tag that reached a gene, against the
    molecules they deduplicated to — so the two numbers sit in one table and mean one thing. The
    positional definition (distinct start coordinates over reads) was rejected: it is a different
    number under the same word, and producing it would make the chimera split hold a set of distinct
    keys for a whole cell, which is the streaming property that keeps a plate's memory flat.

    ``umi_combined`` is the molecule count because it is the only one that counts a UMI seen both
    exonically and intronically on a gene ONCE; summing the exon and intron matrices would report
    more molecules than were sequenced and understate saturation. The multiply-placed fragments are
    in neither term — they never reached a gene, so they are not in the denominator, and the layer
    that places them is not expression, so it is not in the numerator.

    **It is HANDED that total rather than summing the layer itself**, which is the whole reason this
    takes an int. The same figure is a column of its own on the object now, and a function that
    re-derived it here would be a second account of how many molecules a cell has, agreeing with the
    first only by coincidence — a page reporting a molecule count that its neighbouring ratio
    contradicts is the failure this port was built to make unreachable. One derivation lives in
    :func:`_count_cell`, and both readings come off it.

    A cell whose fragments all went untagged or ungenned has no ratio rather than a zero: the
    denominator is the one that has no answer, and a rendered ``0%`` is a number a reader acts on.
    """
    if not umi_fragments:
        return None
    return 1.0 - (molecules / umi_fragments)


# --------------------------------------------------------------------------------------------
# The plate
# --------------------------------------------------------------------------------------------

#: The start method a plate is counted under, and the only one this module will use. A forked child
#: inherits the annotation, which is why the fan-out costs no serialisation at all; ``spawn``
#: re-imports this module and pickles the annotation into every worker on every cell, which is the
#: architecture the header declines. Where the platform does not offer fork the plate is counted on
#: one core instead — slower is a cost, and a 47.5 MB pickle per cell is a design.
_FORK = "fork"

#: The annotation a forked worker counts against. Set on the parent immediately before the pool is
#: built and cleared the moment it closes, so the children have it from the fork itself.
#:
#: A module global precisely because that is what fork copies. Handing it over as a task argument,
#: or through the pool's ``initializer``, would pickle it — one copy per cell in the first case and
#: one per worker in the second, either of which reintroduces the cost this arrangement exists to
#: avoid. It is read-only for the life of the pool: nothing in a worker mutates it, and the parent
#: has no worker running when it writes here.
_INHERITED_ANNOTATION: Annotation | None = None


@dataclass(frozen=True)
class _Counted:
    """One cell's answer. Everything the plate object takes from a cell, and deliberately nothing
    else — see :func:`_count_cell`, which is where what is NOT here dies.

    A record rather than a tuple because it outgrew one: an anonymous tuple unpacked in four places
    is where a plate silently gets one cell's saturation against another's fates. The count of
    fields is deliberately not stated here — it has drifted once already, and a field added is
    exactly the moment nobody rereads the sentence above it.
    """

    matrices: dict[str, dict[int, int]]
    fates: dict[str, int]
    n_fragments: int
    #: ``NH`` -> how many of this cell's fragments carried it. Kilobytes: a cell has as many entries
    #: as the aligner's multimapping limit allows, whatever its depth.
    hits: dict[int, int]
    #: This cell's deduplicated molecules over ``umi_combined``, and how many genes hold one. Both
    #: carried rather than recovered from :attr:`matrices` by whoever wants them, because
    #: :attr:`saturation` is already one minus a share of the first: two callers summing the same
    #: layer is two derivations of one number, and the object would carry no evidence of which one
    #: a column came from.
    #:
    #: The gene field is spelled for the ``obs`` column it feeds and never ``n_genes``, because this
    #: module already uses that word for the gene-axis WIDTH of every matrix — every gene in the
    #: annotation, the same number for every cell on the plate. A per-cell count and a plate-wide
    #: constant under one word is a reader deciding from context which of the two a line means.
    n_umis: int
    genes_detected: int
    #: ``None`` where the cell had no gene-assigned fragment carrying a UMI to divide by.
    saturation: float | None


def _count_cell(bam: Path, annotation: Annotation) -> _Counted:
    """One cell, all the way to its matrices. What a worker does, and what one core does.

    Both arms of :func:`_count_cells` call exactly this, so a pooled plate and a serial plate do the
    same work in the same order and there is no second account of what counting a cell means.

    **The deduplication happens here and not back in the parent**, which is what the fan-out buys
    beyond :func:`count_bam` itself: correcting one real cell's UMIs is measured at 0.9 s over
    200 000 fragments and 4.6 s over a million, so a parent that deduplicated the plate itself would
    hold a serial tail the size of the whole correction pass while every worker idled.

    **And the raw :class:`CellCounts` dies here, in the worker that built it.** It is the largest
    object in this module by a wide margin — a cell's every UMI observation, ~2.5 MB pickled at
    200 000 fragments against 34 kB for the matrices deduplicated out of it, and real cells run
    deeper than that. A plate that carried them all back would hold every cell's at once for the
    life of the run: on the deposit this counter sizes for, gigabytes, and the term that would bind
    the fan-in's memory request first. What survives the worker is what the object is written from
    and nothing more — the matrices, the four fates, the fragment total, the hit-count distribution,
    what the cell yielded and one float.

    **Saturation is computed here for the same reason**: it is one minus a deduplicated figure over
    a raw one, and the deduplicated figure exists only between :func:`deduplicate` returning and this
    frame ending. A parent that wanted it would have to be handed the per-gene molecule totals of
    every cell, which is the matrix it is already being handed, or the raw observations, which are
    the object this function exists to let die.

    **And the molecule total is summed once, here, rather than by each of its two readers.** It is
    a column on the object and it is the numerator of that ratio, and nothing about a page would
    reveal a disagreement between the two: a reader sees a molecule count beside a saturation and
    has no way to check that one was derived from the other. So the sum happens where the ratio is
    built, and the ratio is handed the answer.
    """
    counts = count_bam(bam, annotation)
    matrices = deduplicate(counts)
    # The one place this cell's molecules are counted. Everything that reports them -- the column,
    # and the ratio the column's neighbour is one minus -- reads this, so the two cannot disagree.
    molecules = matrices["umi_combined"]
    n_umis = sum(molecules.values())
    return _Counted(
        matrices=matrices,
        fates=counts.fates,
        n_fragments=counts.n_fragments,
        hits=dict(counts.hits),
        n_umis=n_umis,
        genes_detected=len(molecules),
        saturation=_saturation(n_umis, counts.umi_fragments),
    )


def _count_inherited(bam: Path) -> _Counted:
    """A worker's whole job: count one cell against the annotation it was forked with."""
    annotation = _INHERITED_ANNOTATION
    if annotation is None:  # pragma: no cover — fork copies the global, so this cannot be reached
        raise UmiCountError(
            f"a counting worker was started without an annotation, so {bam} cannot be counted; "
            f"only a fork inherits one, and this worker did not"
        )
    return _count_cell(bam, annotation)


def _refusal(sample: str, exc: BaseException) -> UmiCountError:
    """A failure in one cell, named for the cell rather than for the file or the worker.

    A plate is one job over hundreds of BAMs, so "which cell" is the first thing anybody asks and
    the one thing a pooled traceback does not say: the exception is raised in a child and re-raised
    in the parent with nothing about which task it belonged to. Naming it here means the pooled and
    the serial paths refuse in the same words.
    """
    return UmiCountError(f"cell {sample!r} could not be counted: {exc}")


def _count_cells(
    cells: Sequence[tuple[str, Path]], annotation: Annotation, workers: int
) -> list[_Counted]:
    """Every cell counted, on up to ``workers`` cores, in the order the cells were given.

    **The results are collected by index and never by completion.** Cells differ in depth by three
    orders of magnitude, so they finish in an order that is a property of the data; the h5ad's rows
    are the caller's order, and a plate whose rows arrived as they finished would be labelled
    correctly and hold the wrong counts.

    One task per cell rather than a contiguous slice each, for the same reason: a chunk of a
    sorted-looking cell list is a chunk of similar depth, and one worker then holds the tail of the
    plate while the rest are idle.

    A cell that cannot be counted stops the plate, and what is not yet running is cancelled rather
    than waited for — the refusal has already been decided, and a plate that reported it only after
    counting its remaining thousand cells would be a wrong kind of patient.
    """
    global _INHERITED_ANNOTATION
    width = max(1, min(workers, len(cells)))
    counted: list[_Counted] = []
    if width == 1 or _FORK not in multiprocessing.get_all_start_methods():
        for sample, bam in cells:
            try:
                counted.append(_count_cell(bam, annotation))
            except Exception as exc:
                raise _refusal(sample, exc) from exc
        return counted

    _INHERITED_ANNOTATION = annotation
    try:
        with ProcessPoolExecutor(
            max_workers=width, mp_context=multiprocessing.get_context(_FORK)
        ) as pool:
            futures = [pool.submit(_count_inherited, bam) for _, bam in cells]
            for (sample, _bam), future in zip(cells, futures, strict=True):
                try:
                    counted.append(future.result())
                except Exception as exc:
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise _refusal(sample, exc) from exc
    finally:
        _INHERITED_ANNOTATION = None
    return counted


def _matrix(rows: Sequence[Mapping[int, int]], n_genes: int) -> csr_matrix:
    """``cell -> gene -> value`` for every cell -> one cells x genes sparse matrix.

    Cells x genes, because AnnData is observations x variables and the observations are the cells.
    ``int32``: these are UMI and read counts per gene per cell.
    """
    cell_ix: list[int] = []
    gene_ix: list[int] = []
    values: list[int] = []
    for cell, row in enumerate(rows):
        for gene in sorted(row):
            cell_ix.append(cell)
            gene_ix.append(gene)
            values.append(row[gene])
    matrix = coo_matrix(
        (
            np.array(values, dtype=np.int32),
            (np.array(cell_ix, dtype=np.int64), np.array(gene_ix, dtype=np.int64)),
        ),
        shape=(len(rows), n_genes),
    )
    return matrix.tocsr()


def _hit_counts(counted: Sequence[_Counted]) -> np.ndarray:
    """Every cell's hit-count distribution as one cells x (loci + 1) array, column ``n`` = ``NH n``.

    Dense on purpose. The width is the deepest ``NH`` anywhere on the plate — bounded by the
    aligner's own multimapping limit and single digits in practice — so the whole array is a few
    kilobytes for a plate of thousands of cells, and a sparse encoding of that costs more to explain
    than to store. Two columns minimum, so a plate with nothing multiply placed still has a column
    for the case, and a reader indexing ``[:, 2]`` gets zeros rather than an ``IndexError``.
    """
    width = max(2, max((max(cell.hits, default=0) for cell in counted), default=0) + 1)
    return np.array(
        [[cell.hits.get(n, 0) for n in range(width)] for cell in counted], dtype=np.int32
    )


def count_plate(
    cells: Sequence[tuple[str, Path]], annotation: Annotation, workers: int = 1
) -> anndata.AnnData:
    """Every cell of a plate -> one AnnData, rows in the order the cells were given.

    **The object is the whole answer, and there is no second one beside it.** This used to hand back
    every cell's raw :class:`CellCounts` as well, which meant the plate held all of them at once for
    no reader that wanted them: what anything actually took off that list was the four fates and the
    fragment total, and both are ``obs`` columns here. A figure spelled twice is the copy that goes
    stale, and this one was also the largest object in the module — see :func:`_count_cell`, which
    is now where a cell's observations are last alive.

    **On a chimeric run this is called once per Component**, over that Component's per-cell BAMs, so
    every per-cell figure below — the fates, the yield, the saturation, the hit-count distribution
    and the placement layer — comes out per Component without this function knowing the word. That is
    the shape the ambiguity question wants: two organisms' libraries saturate at different rates and
    detect different numbers of genes, and one object holding both could only report their average.

    Row order is the caller's, never a sort and never the order the filesystem answered in: the
    composer hands the cells over in its own sample order, and the h5ad row is the sample id. That
    holds whatever ``workers`` is — see :func:`_count_cells`, which collects by index.

    ``workers`` is the width of the fan-out over cells, and one is serial. The rule that runs this
    passes the thread count it asked the scheduler for, uncapped: a worker's resident growth is a
    CEILING rather than a rate — ~75 MB of copy-on-write on the interned gene sets, reached inside
    the first twenty thousand fragments of the first cell and flat from there — so the width the
    memory arithmetic can afford is far wider than any node's core count. Asking for more workers
    than there are cells simply gets one per cell.
    """
    import anndata as ad

    if not cells:
        raise UmiCountError("no cells were given to count; a plate with no cells has no matrix")
    duplicates = sorted(s for s, n in Counter(s for s, _ in cells).items() if n > 1)
    if duplicates:
        raise UmiCountError(
            f"sample ids must be unique — each names one h5ad row — but {duplicates} repeat"
        )

    counted = _count_cells(cells, annotation, workers)
    entries = [cell.matrices for cell in counted]
    n_genes = len(annotation.gene_ids)

    adata = ad.AnnData(X=_matrix([e[PRIMARY_MATRIX] for e in entries], n_genes))
    adata.obs_names = [sample for sample, _ in cells]
    adata.var_names = list(annotation.gene_ids)
    adata.var["gene_name"] = list(annotation.gene_names)
    for layer in LAYERS:
        adata.layers[layer] = _matrix([e[layer] for e in entries], n_genes)
    for fate in FATES:
        adata.obs[fate] = np.array([cell.fates[fate] for cell in counted], dtype=np.int32)
    adata.obs[N_FRAGMENTS] = np.array([cell.n_fragments for cell in counted], dtype=np.int32)
    adata.obs[N_UMIS] = np.array([cell.n_umis for cell in counted], dtype=np.int32)
    adata.obs[GENES_DETECTED] = np.array([cell.genes_detected for cell in counted], dtype=np.int32)
    # A cell with no ratio is `nan` and not a zero, which pandas carries and `fate_metrics` reads
    # back as an absent metric rather than as a rendered `0.0%` somebody would act on.
    adata.obs[SATURATION] = np.array(
        [np.nan if cell.saturation is None else cell.saturation for cell in counted],
        dtype=np.float64,
    )
    adata.obsm[MULTIMAPPING_HITS] = _hit_counts(counted)
    adata.uns["primary_matrix"] = PRIMARY_MATRIX
    adata.uns["multimapping_caveat"] = MULTIMAPPING_CAVEAT
    return adata


def write_umi_counts(
    cells: Sequence[tuple[str, Path]], annotation_db: Path, out: Path, workers: int = 1
) -> Path:
    """The one entry point: N per-cell BAMs + the built annotation -> one ``.h5ad``. Returns ``out``.

    The verb behind this is a thin marshalling wrapper, and this is where the work is, so the whole
    fan-in is unit-testable against a synthetic annotation and a BAM whose every read has a fate
    known by construction.

    **The annotation is read BEFORE the fan-out and never inside it**, which is what makes the width
    free: one gffutils read for the plate, and every worker forked from the process that already
    holds the result.
    """
    annotation = read_annotation(annotation_db)
    adata = count_plate(cells, annotation, workers)
    out.parent.mkdir(parents=True, exist_ok=True)
    adata.write_h5ad(out)
    return out


def parse_cells(pairs: Sequence[str]) -> list[tuple[str, Path]]:
    """``sample_id=/path/to.bam`` arguments -> the cells :func:`write_umi_counts` takes.

    The sample id travels **with** its BAM rather than being recovered from the filename, which is
    the join the reference had to warn about: its rows come out labelled ``SRR19884922.namesort.bam``
    and every consumer has to strip a suffix it was never told about.

    One pair per cell on the command line, which at 1440 cells is ~130 kB of argv against a Linux
    limit of ~2 MB. The reference's own ceiling was measured at ~8 600 cells, so the deposit this
    was built for is nowhere near it; a plate that ever is wants a list file, not a longer line.
    """
    cells: list[tuple[str, Path]] = []
    for pair in pairs:
        sample, sep, path = pair.partition("=")
        if not sep or not sample or not path:
            raise UmiCountError(
                f"{pair!r} is not a `sample_id=path` pair; each cell's BAM has to arrive with the "
                f"sample id that names its h5ad row"
            )
        cells.append((sample, Path(path)))
    return cells


# --------------------------------------------------------------------------------------------
# Reading the plate object back, for `seqforge report`
# --------------------------------------------------------------------------------------------
#
# ADR-0025: the module that WRITES an artifact owns reading it. `count_plate` above decides that the
# fates are `obs` columns and what each is called, so the lookup that reads them back belongs beside
# it — and `stats.py` stays a registry that dispatches on a module rather than a file that knows
# what an h5ad is. The alternative drifts in the one direction nothing catches: rename a column here,
# and a reader a package away keeps asking for the old one while the page silently loses a column.
#
# The shape is the two halves every other adapter keeps (`qc.py`, `fragments.py`): a PURE function
# from what the artifact said to a `SampleStats`, and a thin loader around it. What is different is
# the arity — this artifact holds every cell, so the loader is plural and opens it ONCE.


#: How each fate reads on the page: its label, the band it sits under, and what a reader should make
#: of it. All four are the counter's own verdicts on a fragment it could not count, which is why they
#: band under *Counts* and not under *Alignment* — the alignment band on this module's page is
#: STAR's, and two "multimapping" columns under one heading would be two numbers about two different
#: things sharing a word.
#:
#: No thresholds, on any of them. `map/star-umi` is in `MODULES_WITHOUT_CROSS_CHECKS` on exactly this
#: argument: what share of fragments landing on no feature is *wrong* depends on the annotation, and
#: nobody has measured a bar for it. An ungraded number a reader can compare across cells beats an
#: invented bar they learn to ignore.
_FATE_METRICS: dict[str, tuple[str, MetricGroup, str]] = {
    "unmapped": (
        "Unmapped",
        "counts",
        "Fragments the aligner did not place, or whose mate it did not. Compare it with STAR's own "
        "unmapped share beside it: these count fragments, that counts reads.",
    ),
    "multimapping": (
        "Multimapping",
        "counts",
        "Fragments carrying NH > 1 — placed at several loci, so no gene owns them. Read off the "
        "tag rather than off how many records the aligner chose to emit.",
    ),
    "no_feature": (
        "No feature",
        "counts",
        "Fragments that landed where this annotation declares no gene at all. A high share usually "
        "means the wrong gene model rather than a bad library.",
    ),
    "ambiguous": (
        "Ambiguous",
        "counts",
        "Fragments overlapping more than one gene, which no single column can be credited with.",
    ),
}

#: The one fate that belongs in the at-a-glance strip. This module's page carries its per-cell
#: alignment log AND the fates, the fragment total and saturation, which is past the width at which
#: the report folds a table to its headline set — so what the folded view holds is a decision rather
#: than an accident. Saturation is not in it either: it says whether sequencing deeper would pay,
#: which is a question about the next run rather than about whether this one worked. Depth and
#: STAR's mapping rate say whether the cell sequenced and aligned; this says whether what aligned
#: could be counted at all, which is the one thing neither of the others reports and the one that
#: implicates a decision (the gene model) rather than the library. The other three are one click
#: away, not absent.
_HEADLINE_FATE = "no_feature"


def fate_metrics(row: Mapping[str, float], sample: str) -> SampleStats:
    """One cell's ``obs`` row -> the metrics its page column shows. Pure — no file, no anndata.

    The fates are carried as **rates**, over :data:`N_FRAGMENTS`, which is what that column is on the
    object for: a plate's cells differ by three orders of magnitude in depth (901 to ~3M fragments on
    the deposit this was built for), so a raw count of ambiguous fragments says nothing until it is
    divided, and the divisor is right there. The count itself is not lost — ``n_fragments`` is a
    column of its own, so the page carries the numerator's denominator rather than the numerator.

    A cell whose ``n_fragments`` is zero yields no rates at all rather than four zeros: dividing by it
    is the one arithmetic here that has no answer, and a rendered ``0.0%`` is a number a reader acts
    on. Absent is absent, exactly as it is for a metric an artifact never carried — and a saturation
    the counter wrote as ``nan`` is that same absence, arriving as a float instead of as a gap.

    Saturation is built by the shared constructor rather than restated here, which is what makes the
    droplet page's column and this one the same column: one key, one label, one sentence saying what
    the number means, in the module both import. The gene total is built by the same shared
    constructor and for the same reason, naming the region it was counted over so it can never be
    read against a droplet page's exonic total as though the gap were biology.

    **The molecule total is built HERE and is not a named metric**, which is the rule that module
    states: one producer, so a constructor there would be an indirection with a single call site,
    and the sentence it carries has nowhere to drift to. Both are the yield — what the cell produced
    rather than how its fragments failed to — and both are headline, because "did this well work"
    is what a reader asks first and no share of a failure answers it.

    **Neither is graded**, on the argument the fates are not graded on. Nobody has measured how many
    molecules or how many genes a cell SHOULD yield: depth, chemistry, input and organism each move
    both further than anything seqforge decided, and the chimeric twin renders each column once per
    Component, where a single bar would grade a bacterium against a worm's expectations in adjacent
    columns of one row.
    """
    total = row.get(N_FRAGMENTS)
    built: list[Metric | None] = [
        count_metric(
            N_FRAGMENTS,
            "Fragments",
            total,
            group="input",
            exact=True,
            hint="Fragments the counter read for this cell — one per read pair, whatever its fate.",
            headline=True,
        )
    ]
    for fate in FATES:
        label, group, hint = _FATE_METRICS[fate]
        seen = row.get(fate)
        built.append(
            fraction(
                fate,
                label,
                seen / total if seen is not None and total else None,
                group=group,
                hint=hint,
                headline=fate == _HEADLINE_FATE,
            )
        )
    built.append(
        count_metric(
            N_UMIS,
            "UMI (combined)",
            row.get(N_UMIS),
            group="counts",
            exact=True,
            hint="Deduplicated molecules this cell counted into genes, over exons and introns "
            "together — a UMI seen in both on one gene is one molecule. The saturation beside it "
            "is one minus this number over the fragments that carried a UMI to a gene.",
            headline=True,
        )
    )
    built.append(genes_detected(row.get(GENES_DETECTED), region="combined", headline=True))
    saturated = row.get(SATURATION)
    built.append(
        sequencing_saturation(None if saturated is None or isnan(saturated) else saturated)
    )
    return SampleStats(sample_id=sample, metrics=[m for m in built if m is not None])


def _obs_columns(adata: anndata.AnnData) -> dict[str, list[float]]:
    """The per-cell columns the object actually carries, as plain floats. Absent columns stay absent.

    Its own function because it is where anndata stops: ``adata.obs`` is declared as a union with a
    lazy on-disk table, so the narrowing happens once, at the boundary, and everything past it is
    plain Python the metric table can be tested against with no h5ad in the way. A column an older
    object was written without is simply missing from the result, and :func:`fate_metrics` then omits
    its metric instead of reporting a zero nobody counted.

    Floats and not ints, because saturation is one: narrowing to int here would round every cell's
    ratio to 0 and the page would report a plate that had sequenced nothing twice.
    """
    frame: Any = adata.obs
    return {
        column: [float(value) for value in frame[column]]
        for column in (*FATES, N_FRAGMENTS, SATURATION, N_UMIS, GENES_DETECTED)
        if column in frame.columns
    }


def read_plate_stats(path: Path, samples: Sequence[str]) -> dict[str, SampleStats]:
    """The whole plate's ``obs`` -> one :class:`SampleStats` per cell, keyed by sample id.

    **Plural, because the artifact is.** This is the one file the fan-in writes for the whole
    deposit, so it is opened once and every cell's row comes out of that one read; a per-sample
    reader would open a 1440-row object 1440 times to take one row out of each.

    ``backed="r"``: ``obs`` is what the report wants and the matrices are what the file mostly
    IS, so they stay on disk. Reading the object eagerly would pull ~400M sparse entries into a
    process whose whole job is to render five columns per cell.

    Restricted to ``samples`` — the composed config's own list, the same axis the per-sample half is
    read on. A row naming a cell this pipeline was not contracted to produce belongs to some other
    plate that wrote here, and the report's job is to say how much of THIS pipeline landed.

    Raises ``OSError``/``ValueError`` on bytes it cannot read, like every other adapter, so a
    truncated artifact costs its own note rather than the whole page.
    """
    import anndata as ad

    adata = ad.read_h5ad(path, backed="r")
    try:
        columns = _obs_columns(adata)
        names = [str(name) for name in adata.obs_names]
    finally:
        # Backed mode leaves the HDF5 handle open, and `seqforge report` outlives this read by the
        # whole rendering pass -- the same handle discipline `read_annotation` keeps for gffutils.
        adata.file.close()

    wanted = set(samples)
    return {
        name: fate_metrics({column: values[i] for column, values in columns.items()}, name)
        for i, name in enumerate(names)
        if name in wanted
    }


__all__ = [
    "COUNT_RATIO_THRESHOLD",
    "FATES",
    "GENES_DETECTED",
    "HITS_TAG",
    "LAYERS",
    "MULTIMAPPING_CAVEAT",
    "MULTIMAPPING_HITS",
    "MULTIMAPPING_LAYER",
    "N_FRAGMENTS",
    "N_UMIS",
    "PRIMARY_MATRIX",
    "SATURATION",
    "UMI_TAG",
    "Annotation",
    "CellCounts",
    "UmiCountError",
    "correct_umis",
    "count_bam",
    "count_plate",
    "deduplicate",
    "fate_metrics",
    "parse_cells",
    "read_annotation",
    "read_plate_stats",
    "write_umi_counts",
]
