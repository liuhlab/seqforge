"""The **Units table**: which file belongs to which sample, and in what order it arrives.

Every aligner seqforge composes reads its mates **in lockstep** — STAR's `--readFilesIn`, chromap's
`-1/-2/-b` — so each mate of one sample must arrive in the SAME file order. Getting that wrong does
not crash: the mates hold equal read counts either way, so the run completes and writes an artifact
pairing one lane's barcodes with another lane's cDNA. Exit 0, plausible size, wrong matrix.

This lives here rather than in each `.smk` for the reason `starsolo.smk` already imports `memory` and
`QC_SUFFIX`: a Snakefile is not importable, so a closure written inside one can never be unit-tested,
only run — and three copies of an ordering rule are three chances for one of them to drift while the
suite stays green. `units.tsv` is written by `compose`, and the columns this reads are the ones it
writes.

**Its readers are modules AND the verbs those modules call** (`docs/adr/0036`). A verb that needs a
sample's file list is handed this table and the sample id rather than the paths, so the placement is
read from the columns that state it instead of being rebuilt from a rendered command line — where
arity, quoting and order are unguarded by construction, because `snakemake -n -p` *formats* a
`shell:` block and never runs one. That is why the loader is here too: one reader of the file, used
from both sides, rather than a module-local `csv.DictReader` and a second one inside a verb.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from pathlib import Path

#: The units.tsv columns that decide order, most significant first. `run` groups a pooled sample's
#: files; `lane` orders within one run, and is not the tie-break it looks like — a run SPANS its lanes
#: (`docs/adr/0027`), so a four-lane library ties on `run` for every one of its files and `path` alone
#: would hand the pairing to lexical filename order. That happens to hold for bcl2fastq names, by the
#: coincidence that `_L001_` precedes `_R1_`; it is not a fact anything enforces. `path` remains last
#: so the order is total even when two files share a run and a lane.
_ORDER = ("run", "lane", "path")

#: Where a file was sequenced, which is what pairs one mate with another rather than a list index.
#: A run SPANS its lanes (`docs/adr/0027`), so it takes both columns to name a place.
_PLACE = ("run", "lane")


class UnitsError(ValueError):
    """The table cannot answer what was asked of it, and guessing would produce a plausible artifact.

    A refusal and not a `KeyError`: every raise below names a sample, a place and a file, because the
    thing that went wrong is a row that is missing or a row that is surplus, and the reader has to be
    able to go and look at it.
    """


def load_units(path: str | Path) -> list[dict[str, str]]:
    """Read a units.tsv into rows. The one reader, used by a module and by the verbs it calls."""
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def _ordered_rows(
    units: Iterable[Mapping[str, str]], sample: str, role: str
) -> list[Mapping[str, str]]:
    """One sample's `role` rows, in the order `_ORDER` decides. THE order, and the only one."""
    rows = [u for u in units if u["sample_id"] == sample and u["read_id"] == role]
    return sorted(rows, key=lambda u: tuple(u[c] for c in _ORDER))


def ordered_fastqs(units: Iterable[Mapping[str, str]], sample: str, role: str) -> list[str]:
    """The paths of one sample's `role` mate, in the one order every other mate is also put into.

    `role` is a units.tsv `read_id` — the composer's name for which mate a file is, never a guess from
    the filename. No filename is parsed here or in any module that calls this: the columns carry
    seqforge's own grouping, derived once in `resolve.group`.
    """
    return [u["path"] for u in _ordered_rows(units, sample, role)]


def mate_role(units: Iterable[Mapping[str, str]], sample: str, tagged_role: str) -> str | None:
    """The one role this sample carries besides `tagged_role`, or `None` where it carries none.

    **Absence is the statement** (`docs/adr/0035`): no row carrying a second role means there is no
    mate, and nothing declares that separately — a `paired:` column beside these would be the same
    fact said twice, and owed by every sample that has no use for it.

    Refuses on two or more rather than picking, and the refusal is the point. Which file is the mate
    is a claim, and a caller that took "the first other role" would take a barcode read as a cDNA
    mate on a layout nobody has composed yet — silently, and with the record count still plausible.
    """
    others = sorted(
        {u["read_id"] for u in units if u["sample_id"] == sample and u["read_id"] != tagged_role}
    )
    if not others:
        return None
    if len(others) > 1:
        raise UnitsError(
            f"sample {sample!r} carries {others} besides its {tagged_role} read; a tagged-molecule "
            f"extraction takes at most one mate, and choosing between these would pair the tagged "
            f"read with a file nothing says is its mate"
        )
    return others[0]


def paired_fastqs(
    units: Iterable[Mapping[str, str]], sample: str, tagged_role: str
) -> tuple[list[str], list[str] | None]:
    """One sample's tagged files and, beside each, the mate sequenced in the SAME place.

    Returns two lists the caller may zip, or `(tagged, None)` where the sample has no mate at all.
    The parallelism is not a coincidence of two sorts agreeing: index *i* of each list is the file
    that `run` and `lane` put in one place (`docs/adr/0027`), and a place holding a tagged file and
    no mate is refused **by name** rather than shifted onto the next place's mate.

    That refusal is the whole reason this is not `ordered_fastqs` called twice. Two independently
    sorted lists are parallel in every deposit anyone has seen and nothing *makes* them parallel, so
    pairing by index is an assumption about two sorts — and the naive repair, checking the two are
    the same length, passes exactly the case that matters: a cell whose two runs hold 100/50 tagged
    reads and 50/100 mate reads totals 150 either way, so nothing fires and every fragment past the
    fiftieth pairs one run's cDNA against another run's. Placing them first turns that into an
    unequal pair, which the extractor already refuses.

    Rows in one place, if a place ever holds several of one role, keep `_ORDER`'s `path` tie-break —
    so the guarantee degrades to "the same order for both mates", which is what it was before.
    """
    tagged = _ordered_rows(units, sample, tagged_role)
    if not tagged:
        raise UnitsError(
            f"the units table names no {tagged_role!r} file for sample {sample!r}; there is nothing "
            f"to extract, and an empty run would write an empty artifact at exit 0"
        )
    role = mate_role(units, sample, tagged_role)
    if role is None:
        return [u["path"] for u in tagged], None

    unclaimed: dict[tuple[str, ...], list[str]] = {}
    for u in _ordered_rows(units, sample, role):
        unclaimed.setdefault(tuple(u[c] for c in _PLACE), []).append(u["path"])
    mates: list[str] = []
    for u in tagged:
        place = tuple(u[c] for c in _PLACE)
        if not unclaimed.get(place):
            raise UnitsError(
                f"sample {sample!r} carries {u['path']} as its {tagged_role} read for run "
                f"{u['run']!r} lane {u['lane']!r}, and no {role} file was sequenced there. Its "
                f"mate is missing rather than late: pairing it with another run's would tag one "
                f"run's cDNA with another run's molecules and still exit 0"
            )
        mates.append(unclaimed[place].pop(0))
    surplus = sorted(path for queue in unclaimed.values() for path in queue)
    if surplus:
        raise UnitsError(
            f"sample {sample!r} carries {surplus} as {role} files sequenced where it has no "
            f"{tagged_role} read; a mate with nothing to inherit its UMI from is a row that does "
            f"not belong to this sample, not a read to drop"
        )
    return [u["path"] for u in tagged], mates


__all__ = ["UnitsError", "load_units", "mate_role", "ordered_fastqs", "paired_fastqs"]
