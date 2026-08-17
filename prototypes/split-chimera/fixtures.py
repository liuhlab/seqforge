"""PROTOTYPE — throwaway. The synthetic chimera, and the single-assembly BAM to compare it against.

Deliberately mirrors `tests/test_workflows.py`'s `_Fragment` / `_synthetic_bam` — the counter's own
plate machinery — because the map's standing preference is that a test is a row on an existing case
before it is ever a new one. If this shape works, the real test is that helper plus suffixed contig
names, not a new fixture module.

**No aligner and no built chimera.** The chimeric BAM is hand-written, and so is the single-assembly
BAM the header is compared against: both come from the same declared contig table, spelled two ways.
That is the whole cheapness claim, and it is what this prototype is testing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from genome.chimera import derive_separator, suffixed

READ_LEN = 20


@dataclass(frozen=True)
class Component:
    """One half of a chimera: a name and the chromosomes it declares, in ITS OWN order."""

    name: str
    chromosomes: tuple[tuple[str, int], ...]  # (name, length), in declared order


@dataclass(frozen=True)
class Shape:
    """A chimera to build a fixture from, and the separator its names force."""

    label: str
    components: tuple[Component, ...]

    @property
    def separator(self) -> str:
        return derive_separator({c.name: [n for n, _ in c.chromosomes] for c in self.components})

    @property
    def sq(self) -> list[dict[str, Any]]:
        """The chimeric @SQ: components in sorted order, each block in its own declared order."""
        sep = self.separator
        return [
            {"SN": suffixed(chrom, c.name, sep), "LN": length}
            for c in sorted(self.components, key=lambda c: c.name)
            for chrom, length in c.chromosomes
        ]

    def single_assembly_sq(self, component: str) -> list[dict[str, Any]]:
        """What a run against the bare component alone would have written."""
        one = next(c for c in self.components if c.name == component)
        return [{"SN": chrom, "LN": length} for chrom, length in one.chromosomes]


# `chrX` before `chrM` is real ce11 order and is NOT alphabetical — deliberate, because an @SQ block
# that happens to be sorted makes the order assertion decorative.
_CE = Component("tinyCe", (("chrI", 4000), ("chrII", 3000), ("chrX", 2500), ("chrM", 900)))
_EC = Component("tinyEc", (("ctg1", 1200), ("ctg2", 800)))
_SC = Component("tinySc", (("chrI", 700),))  # same chromosome NAME as tinyCe, on purpose
_DUB = Component("tinyEcDub", (("ctg__1", 1200), ("ctg__2", 800)))

SHAPES = {
    "plain": Shape("plain", (_CE, _EC)),
    "dub": Shape("dub", (_CE, _DUB)),  # names already carry `__`, so the separator becomes `___`
    "triple": Shape("triple", (_CE, _EC, _SC)),
}


@dataclass(frozen=True)
class Fragment:
    """One template to synthesise, and what the splitter is expected to do with it."""

    name: str
    contig: str  # the SUFFIXED name, i.e. the chimeric spelling
    start: int
    kind: str = "unique"  # unique | multimapper | unmapped | secondary | supplementary
    component: str = ""  # where a kept fragment belongs; "" when it is expected to be dropped
    mate_contig: str = ""  # only for the cross-Component mate, which STAR is said never to write


def _records(header: Any, frag: Fragment) -> list[Any]:
    import pysam

    def build(
        start: int, flag: int, mate: int, tlen: int, hits: int, on: str = "", to: str = ""
    ) -> Any:
        rec = pysam.AlignedSegment(header)
        rec.query_name = frag.name
        rec.flag = flag
        if not rec.is_unmapped:
            rec.reference_id = header.get_tid(on or frag.contig)
            rec.reference_start = start
            rec.mapping_quality = 255 if hits == 1 else 3
            rec.cigarstring = f"{READ_LEN}M"
            rec.next_reference_id = header.get_tid(to or on or frag.contig)
            rec.next_reference_start = mate
            rec.template_length = tlen
        rec.query_sequence = "A" * READ_LEN
        rec.query_qualities = pysam.qualitystring_to_array("I" * READ_LEN)
        rec.set_tags([("NH", hits, "i")])
        return rec

    span = READ_LEN * 3
    mate_start = frag.start + span - READ_LEN
    if frag.kind == "unmapped":
        rec = pysam.AlignedSegment(header)
        rec.query_name = frag.name
        rec.flag = 1 | 4 | 8 | 64  # PAIRED | UNMAPPED | MATE_UNMAPPED | READ1
        rec.query_sequence = "A" * READ_LEN
        rec.query_qualities = pysam.qualitystring_to_array("I" * READ_LEN)
        rec.set_tags([("uT", 4, "i")])
        return [rec]
    if frag.kind == "secondary":
        return [build(frag.start, 1 | 2 | 64 | 256, mate_start, span, 2)]
    if frag.kind == "supplementary":
        return [build(frag.start, 1 | 2 | 64 | 2048, mate_start, span, 1)]
    if frag.kind == "cross_mate":
        # STAR is said never to write this (one Transcript has one Chr), and nobody has watched that
        # hold. The pair is spelled here so the splitter's free check has something to fire on.
        return [
            build(frag.start, 1 | 2 | 32 | 64, mate_start, span, 1, frag.contig, frag.mate_contig),
            build(mate_start, 1 | 2 | 16 | 128, frag.start, -span, 1, frag.mate_contig, frag.contig),
        ]
    hits = 2 if frag.kind == "multimapper" else 1
    return [
        build(frag.start, 1 | 2 | 32 | 64, mate_start, span, hits),
        build(mate_start, 1 | 2 | 16 | 128, frag.start, -span, hits),
    ]


def plate(shape: Shape, cross_mate: bool = False) -> tuple[Fragment, ...]:
    """One fragment of every kind, in every Component — nothing here is computed."""
    sep = shape.separator
    frags: list[Fragment] = []
    for comp in shape.components:
        first = suffixed(comp.chromosomes[0][0], comp.name, sep)
        last = suffixed(comp.chromosomes[-1][0], comp.name, sep)
        frags += [
            Fragment(f"{comp.name}_unique_1", first, 100, "unique", comp.name),
            Fragment(f"{comp.name}_unique_2", last, 200, "unique", comp.name),
            Fragment(f"{comp.name}_multi", first, 300, "multimapper"),
            Fragment(f"{comp.name}_secondary", first, 400, "secondary"),
            Fragment(f"{comp.name}_supp", first, 500, "supplementary"),
        ]
    frags.append(Fragment("nowhere", "", 0, "unmapped"))
    if cross_mate:
        first, second = sorted(shape.components, key=lambda c: c.name)[:2]
        frags.append(
            Fragment(
                "spanning",
                suffixed(first.chromosomes[0][0], first.name, sep),
                600,
                "cross_mate",
                mate_contig=suffixed(second.chromosomes[0][0], second.name, sep),
            )
        )
    return tuple(frags)


def chimeric_bam(path: Path, shape: Shape, fragments: tuple[Fragment, ...]) -> Path:
    """A COORDINATE-sorted chimeric BAM, with a STAR-shaped @PG/@CO naming the chimera."""
    import pysam

    name = "_".join(sorted(c.name for c in shape.components))
    header = pysam.AlignmentHeader.from_dict(
        {
            "HD": {"VN": "1.6", "SO": "coordinate"},
            "SQ": shape.sq,
            "PG": [{"ID": "STAR", "PN": "STAR", "VN": "2.7.11b", "CL": f"STAR --genomeDir {name}"}],
            "CO": [f"user command line: STAR --genomeDir {name}"],
        }
    )
    records = [rec for frag in fragments for rec in _records(header, frag)]
    records.sort(key=lambda r: (r.reference_id if r.reference_id >= 0 else 1 << 30, r.reference_start))
    with pysam.AlignmentFile(str(path), "wb", header=header) as out:
        for rec in records:
            out.write(rec)
    return path


def single_assembly_bam(path: Path, shape: Shape, component: str) -> Path:
    """The comparison artifact: what the same reads against the BARE component would have written.

    Hand-written from the same declared table, so producing it costs no aligner and no reference.
    """
    import pysam

    header = pysam.AlignmentHeader.from_dict(
        {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": shape.single_assembly_sq(component)}
    )
    with pysam.AlignmentFile(str(path), "wb", header=header):
        pass
    return path
