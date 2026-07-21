"""Split a pile of FASTQ files into the **runs** they came from, by filename.

**Filenames lie about roles. They do not lie about identity — and the difference is the whole design
of this module.**

`fasterq-dump`'s `_1` / `_2` suffixes say nothing about which read is the barcode and which is the
cDNA: they are an artifact of dump order, and inferring roles from them is exactly the guess this
project exists to refuse. Roles are decided by bytes, in `resolve`, and nothing here touches them.

But `SRR28716558` is not an interpretation. It is an accession the archive assigned, printed on the
file by the tool that wrote it. Grouping by it is a rung-1 signal used for the one thing rung 1 is
allowed to do: a weak, checkable prior about which files belong together. So filenames *group*; bytes
*assign*. If the grouping is wrong the chemistry check downstream disagrees loudly, because two runs
of the same library resolve to the same chemistry and a mis-grouped pair does not.

**Why this exists.** `resolve_dataset` scores one set of files as ONE library, which is correct and
always was. The bug was that nobody split first: hand it a 6-run dataset's 12 files and it does a
single global role assignment, picks the best (R1, R2) pair out of all 12, and leaves the other **ten**
with no role at all. `_units` then skips them, `validate` passes clean, and you get a content-addressed
manifest that has quietly dropped 5/6 of the data. Exit 0. That is the failure class §5 of the brief
exists to prevent, and the pilot dataset is exactly 6 runs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

#: A mate/read token at the END of a stem: `_1`, `_2`, `_R1`, `.R2`, and Illumina's `_R1_001`.
#:
#: `[1-4]` rather than `[12]` on purpose: `fasterq-dump --include-technical` emits `_1.._4` for a
#: 10x run (I1/I2/R1/R2), and a `_3` that failed to match here would become its own bogus "run"
#: instead of joining its siblings. Extra files inside a group are fine — a leftover is what
#: `resolve`'s assignment penalty is for; a leftover in a group of its own is not.
_MATE = re.compile(r"^(?P<stem>.+?)[._](?:R|read[-_]?)?(?P<mate>[1-4])(?:[._]\d{3})?$", re.I)

#: Extensions to strip before looking for a mate token. Longest first — `.fastq.gz` before `.gz`.
_EXTS = (".fastq.gz", ".fq.gz", ".fastq.bz2", ".fastq.xz", ".fastq", ".fq", ".gz")

#: A LEADING SRA/ENA/DDBJ run accession the dump tool printed on the file. Unlike the mate token, an
#: accession is a real identity the archive assigned (see the module docstring), so it OUTRANKS the
#: mate heuristic below. It has to: an original-format download can carry the submitter's own lane
#: naming *after* the accession — `SRR36109512_11314-RM-1_S1_L005_R1_001` — so the two mate files
#: differ only in `_R1_`/`_R2_` buried mid-name, where the end-anchored mate strip cannot see it. The
#: strip then keys each file to the whole `..._S1_L005` stem, every file becomes its own singleton
#: "run", and the record join (`records.py`, `by_accession.get(run_key(...))`) misses every file
#: (#6, GSE310667). Keying on the accession rejoins the mates and lands the join.
_SRA_RUN = re.compile(r"^([SED]RR\d+)(?=[._])")


def _strip_ext(name: str) -> str:
    lowered = name.lower()
    for ext in _EXTS:
        if lowered.endswith(ext):
            return name[: -len(ext)]
    return name


def run_key(path: str | Path) -> str:
    """The run a file belongs to, derived from its name. Never a claim about the file's ROLE.

    `SRR28716558_1.fastq.gz` -> `SRR28716558`; `SRR36109512_11314-RM-1_S1_L005_R1_001.fastq.gz` ->
    `SRR36109512` (a leading accession wins over any submitter suffix); `x_S1_L001_R1_001.fastq.gz` ->
    `x_S1_L001`; a name with no accession and no mate token is its own run, which is the right answer
    for a single-end library.
    """
    stem = _strip_ext(Path(path).name)
    sra = _SRA_RUN.match(stem)
    if sra is not None:
        return sra.group(1)
    match = _MATE.match(stem)
    return match.group("stem") if match else stem


def group_runs(paths: Sequence[str | Path]) -> dict[str, list[Path]]:
    """Group `paths` into runs, preserving input order within each run and sorting the runs by key.

    Every input path lands in exactly one group; nothing is dropped, deduplicated, or reordered across
    groups. That is worth stating because the bug this module fixes was files silently disappearing.
    """
    groups: dict[str, list[Path]] = {}
    for path in paths:
        groups.setdefault(run_key(path), []).append(Path(path))
    return {key: groups[key] for key in sorted(groups)}
