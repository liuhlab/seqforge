"""Count UMIs and reads per gene across every cell of a plate, into ONE ``.h5ad``.

This is the fan-in: one counting job over all N per-cell BAMs, not N counting jobs and a merge. It
re-implements what ``umite``'s ``umicount`` decided, and deliberately not how it decided it — the
mechanism is where the reference costs the most and is wrong in the most places.

**It writes the ``.h5ad`` directly, with no TSV in between.** 1440 cells x ~55 000 genes x 4
matrices is ~79M values each, almost all zero: roughly 630 MB of dense text to produce a sparse
object several times smaller. Writing a sample column to join back on would also rebuild, for
ourselves, the exact trap the reference had to warn about — its rows are labelled with the BAM's
*basename*, suffix and all. This counter is handed each cell's sample id along with its BAM, so
there is nothing to join and nothing to strip.

The object is four matrices and one row per cell:

| in the object | is | the reference's column |
| --- | --- | --- |
| ``X`` | UMIs, exonic | ``UE`` |
| ``layers["umi_intron"]`` | UMIs, intronic | ``UI`` |
| ``layers["read_exon"]`` | untagged reads, exonic | ``RE`` |
| ``layers["read_intron"]`` | untagged reads, intronic | ``RI`` |

``obs`` is indexed by **sample id**, which is what makes the h5ad row and the per-cell CRAM filename
join, and the four read fates are ``obs`` **columns**. The reference carries them as four extra
*gene* columns (``_unmapped``, ``_multimapping``, ``_no_feature``, ``_ambiguous``) in a table whose
other 55 335 columns are genes — a per-cell scalar dressed as a feature, which is what forced a
correction in its output shape. As columns they need no leading underscore either: the underscore
was there to keep them out of the gene id namespace, and they are not in it any more.

The reference's fifth table, ``D`` (per-gene PCR duplicates), is deliberately not a fifth matrix:
the object is four by decision, and ``n_fragments`` on ``obs`` is what makes the four fates readable
as rates.

**The annotation comes from the database ``liulab-genome`` already built** — no HTSeq, no GTF parse,
no per-worker copy. The reference parses the GTF into two HTSeq ``GenomicArrayOfSets`` and pickles
them; that pickle is 47.5 MB and is serialised into every worker, which at 1440 cells is ~76 GB
through pipes for an object that never changes, on top of a 50 s parse. Reading gffutils' SQLite
once, into the step index below, deletes both — and it is the largest single win in this port,
obtained by declining to reproduce the architecture rather than by optimising it.

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

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix

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

#: UMI correction, as module literals rather than flags. Always on, and Hamming-1: at 3 the trailing
#: check is vacuous and the merge manufactures UMIs that were never sequenced. The count ratio is
#: the reference's guard — a low-count neighbour is only absorbed into a seed that is at least
#: roughly twice as abundant, so two genuinely distinct UMIs at similar depth are never merged.
HAMMING_THRESHOLD = 1
COUNT_RATIO_THRESHOLD = 2

#: What ``X`` is, and what the other three matrices are called. The reference's names for these are
#: ``UE``/``UI``/``RE``/``RI``; these say the same thing to somebody who has never read it.
PRIMARY_MATRIX = "umi_exon"
LAYERS: tuple[str, ...] = ("umi_intron", "read_exon", "read_intron")

#: The four ways a fragment fails to reach a gene, in the order they are decided. Per-cell scalars,
#: so they are ``obs`` columns; :data:`N_FRAGMENTS` is here so they can be read as rates.
FATES: tuple[str, ...] = ("unmapped", "multimapping", "no_feature", "ambiguous")
N_FRAGMENTS = "n_fragments"


# --------------------------------------------------------------------------------------------
# The annotation, read once
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _StepIndex:
    """One contig's intervals as a step function: segment starts, and the gene set on each segment.

    This is HTSeq's ``GenomicArrayOfSets`` in two numpy arrays and a tuple. ``starts`` is ascending
    and begins at 0, so ``searchsorted`` always lands inside it; ``set_ids[i]`` names the genes
    covering ``[starts[i], starts[i + 1])``.

    **The set ids are the whole reason this fits in memory.** A gencode-scale annotation has ~841 000
    exons, so a step vector over them has ~1.7M segments — and one Python ``frozenset`` per segment
    would cost hundreds of MB for an object where almost every segment names the same one gene.
    Interning collapses that to ~60 000 distinct sets behind an int32 array, which is a few MB. The
    alternative — an interval tree per contig — is a comparable amount of code and answers a
    question we do not ask (which interval), rather than the one we do (which gene set).
    """

    starts: np.ndarray
    set_ids: np.ndarray
    sets: tuple[frozenset[int], ...]

    def genes(self, start: int, end: int) -> frozenset[int]:
        """Every gene covering any part of ``[start, end)``. Empty is a legal answer."""
        if end <= start:
            return frozenset()
        first = int(np.searchsorted(self.starts, start, side="right")) - 1
        last = int(np.searchsorted(self.starts, end, side="left"))
        found: set[int] = set()
        for i in range(max(first, 0), last):
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
        starts=np.array(starts, dtype=np.int64),
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
    """

    umi_exon: dict[int, dict[str, int]]
    umi_intron: dict[int, dict[str, int]]
    read_exon: dict[int, int]
    read_intron: dict[int, int]
    fates: dict[str, int]
    n_fragments: int


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


def _hamming_within(a: str, b: str, threshold: int) -> bool:
    """Whether ``a`` and ``b`` differ in at most ``threshold`` positions.

    Unequal lengths are **not** within any threshold. The reference asks rapidfuzz for the distance
    with padding off, which raises on a length mismatch; refusing to merge says the same thing
    without making a ragged tag an exception, and without a dependency for eight characters.
    """
    if len(a) != len(b):
        return False
    seen = 0
    for x, y in zip(a, b, strict=True):
        if x != y:
            seen += 1
            if seen > threshold:
                return False
    return True


def correct_umis(observations: Mapping[str, int]) -> dict[str, int]:
    """Merge each UMI into the more abundant near-neighbour that can explain it as a PCR error.

    The reference's algorithm, kept: take the most abundant UMI as a seed, then walk the remaining
    UMIs from the least abundant upward, absorbing any within :data:`HAMMING_THRESHOLD` of the seed
    and stopping as soon as a candidate is too abundant for the seed to explain
    (``COUNT_RATIO_THRESHOLD * candidate - 1 > seed``). Repeat with what is left.

    **Ties are broken on the UMI itself, not on the order the BAM happened to hand them over.** The
    reference sorts by count alone and lets Python's stable sort fall back to insertion order, which
    is read order — and read order here is coordinate order rather than the name order it was
    written against, so inheriting it would be inheriting somebody else's accident. Sorting equal
    counts by sequence makes the result a function of the data alone, which is what lets the same
    plate counted twice come out byte-identical.
    """
    remaining = sorted(observations.items(), key=lambda item: (-item[1], item[0]))
    corrected: dict[str, int] = {}
    while remaining:
        seed, seed_count = remaining.pop(0)
        corrected[seed] = seed_count
        i = len(remaining) - 1
        while i >= 0:
            candidate, candidate_count = remaining[i]
            if (COUNT_RATIO_THRESHOLD * candidate_count) - 1 > seed_count:
                break  # everything left of here is at least this abundant
            if _hamming_within(seed, candidate, HAMMING_THRESHOLD):
                corrected[seed] += candidate_count
                remaining.pop(i)
            i -= 1
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
        fates=dict.fromkeys(FATES, 0),
        n_fragments=0,
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
                if not _representative(record):
                    continue
                counts.n_fragments += 1
                _count_fragment(record, annotation, counts)
    except OSError as exc:
        raise UmiCountError(f"{bam} could not be read: {exc}") from exc
    return counts


def _count_fragment(record: AlignedSegment, annotation: Annotation, counts: CellCounts) -> None:
    """Decide one fragment's fate and, if it has one, its gene and its bucket."""
    # Both mates aligned or the fragment is unmapped -- the reference's rule, read off the flags
    # this record already carries rather than off the pair it no longer has. A fragment whose two
    # records the aligner omitted entirely is invisible here, and that is a property of the BAM.
    if record.is_unmapped or (record.is_paired and record.mate_is_unmapped):
        counts.fates["unmapped"] += 1
        return
    # Absent means one locus. The tag, not the bundle length: with one record emitted per
    # multimapper the bundle is length 1 and the reference calls it unique.
    if record.has_tag(HITS_TAG) and int(record.get_tag(HITS_TAG)) > 1:
        counts.fates["multimapping"] += 1
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
        bucket = counts.umi_exon if spliced else counts.umi_intron
        bucket[gene][umi] = bucket[gene].get(umi, 0) + 1
    elif spliced:
        counts.read_exon[gene] += 1
    else:
        counts.read_intron[gene] += 1


def deduplicate(counts: CellCounts) -> dict[str, dict[int, int]]:
    """The four matrices' non-zero entries for one cell, as ``matrix -> gene -> value``.

    **Each UMI bucket is deduplicated on its own**, which is the second trap this port reproduces
    rather than re-derives: one UMI seen both exonically and intronically on the same gene counts
    once in each, so the combined figure is **not** ``exon + intron``. The frozen agreement fixture
    cannot tell the two apart — at 2 000 reads a cell the combined figure equalled the sum exactly
    on all ten cells — so a port that deduplicated over the union would have passed it while
    quietly losing a count per gene wherever the depth was real.
    """
    return {
        PRIMARY_MATRIX: {g: len(correct_umis(u)) for g, u in counts.umi_exon.items() if u},
        "umi_intron": {g: len(correct_umis(u)) for g, u in counts.umi_intron.items() if u},
        "read_exon": {g: n for g, n in counts.read_exon.items() if n},
        "read_intron": {g: n for g, n in counts.read_intron.items() if n},
    }


# --------------------------------------------------------------------------------------------
# The plate
# --------------------------------------------------------------------------------------------


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


def count_plate(
    cells: Sequence[tuple[str, Path]], annotation: Annotation
) -> tuple[anndata.AnnData, list[CellCounts]]:
    """Every cell of a plate -> one AnnData, rows in the order the cells were given.

    Row order is the caller's, never a sort and never the order the filesystem answered in: the
    composer hands the cells over in its own sample order, and the h5ad row is the sample id.
    """
    import anndata as ad

    if not cells:
        raise UmiCountError("no cells were given to count; a plate with no cells has no matrix")
    duplicates = sorted(s for s, n in Counter(s for s, _ in cells).items() if n > 1)
    if duplicates:
        raise UmiCountError(
            f"sample ids must be unique — each names one h5ad row — but {duplicates} repeat"
        )

    per_cell = [count_bam(bam, annotation) for _, bam in cells]
    entries = [deduplicate(counts) for counts in per_cell]
    n_genes = len(annotation.gene_ids)

    adata = ad.AnnData(X=_matrix([e[PRIMARY_MATRIX] for e in entries], n_genes))
    adata.obs_names = [sample for sample, _ in cells]
    adata.var_names = list(annotation.gene_ids)
    adata.var["gene_name"] = list(annotation.gene_names)
    for layer in LAYERS:
        adata.layers[layer] = _matrix([e[layer] for e in entries], n_genes)
    for fate in FATES:
        adata.obs[fate] = np.array([c.fates[fate] for c in per_cell], dtype=np.int32)
    adata.obs[N_FRAGMENTS] = np.array([c.n_fragments for c in per_cell], dtype=np.int32)
    adata.uns["primary_matrix"] = PRIMARY_MATRIX
    return adata, per_cell


def write_umi_counts(cells: Sequence[tuple[str, Path]], annotation_db: Path, out: Path) -> Path:
    """The one entry point: N per-cell BAMs + the built annotation -> one ``.h5ad``. Returns ``out``.

    The verb behind this is a thin marshalling wrapper, and this is where the work is, so the whole
    fan-in is unit-testable against a synthetic annotation and a BAM whose every read has a fate
    known by construction.
    """
    annotation = read_annotation(annotation_db)
    adata, _ = count_plate(cells, annotation)
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


__all__ = [
    "COUNT_RATIO_THRESHOLD",
    "FATES",
    "HAMMING_THRESHOLD",
    "HITS_TAG",
    "LAYERS",
    "N_FRAGMENTS",
    "PRIMARY_MATRIX",
    "UMI_TAG",
    "Annotation",
    "CellCounts",
    "UmiCountError",
    "correct_umis",
    "count_bam",
    "count_plate",
    "deduplicate",
    "parse_cells",
    "read_annotation",
    "write_umi_counts",
]
