"""The order a sample's FASTQs reach an aligner, decided once for every mapping module.

Every aligner seqforge composes reads its mates **in lockstep** — STAR's `--readFilesIn`, chromap's
`-1/-2/-b` — so each mate of one sample must arrive in the SAME file order. Getting that wrong does
not crash: the mates hold equal read counts either way, so the run completes and writes an artifact
pairing one lane's barcodes with another lane's cDNA. Exit 0, plausible size, wrong matrix.

This lives here rather than in each `.smk` for the reason `starsolo.smk` already imports `memory` and
`QC_SUFFIX`: a Snakefile is not importable, so a closure written inside one can never be unit-tested,
only run — and three copies of an ordering rule are three chances for one of them to drift while the
suite stays green. `units.tsv` is written by `compose`, and the columns this reads are the ones it
writes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

#: The units.tsv columns that decide order, most significant first. `run` groups a pooled sample's
#: files; `lane` orders within one run, and is not the tie-break it looks like — a run SPANS its lanes
#: (`docs/adr/0027`), so a four-lane library ties on `run` for every one of its files and `path` alone
#: would hand the pairing to lexical filename order. That happens to hold for bcl2fastq names, by the
#: coincidence that `_L001_` precedes `_R1_`; it is not a fact anything enforces. `path` remains last
#: so the order is total even when two files share a run and a lane.
_ORDER = ("run", "lane", "path")


def ordered_fastqs(units: Iterable[Mapping[str, str]], sample: str, role: str) -> list[str]:
    """The paths of one sample's `role` mate, in the one order every other mate is also put into.

    `role` is a units.tsv `read_id` — the composer's name for which mate a file is, never a guess from
    the filename. No filename is parsed here or in any module that calls this: the columns carry
    seqforge's own grouping, derived once in `resolve.group`.
    """
    rows = [u for u in units if u["sample_id"] == sample and u["read_id"] == role]
    return [u["path"] for u in sorted(rows, key=lambda u: tuple(u[c] for c in _ORDER))]


__all__ = ["ordered_fastqs"]
