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
manifest that has quietly dropped 5/6 of the data. Exit 0. Silent data loss at exit 0 is the failure
class this compiler exists to prevent, and the pilot dataset is exactly 6 runs.

**And why the key is narrow.** The same failure class has a second shape, one level up: a key that
kept the lane made a four-lane library four runs, and with no archive record a run IS the sample
identity — four `<sample>.h5ad` at a quarter depth each, self-consistent, exit 0 (#263). So a run is
lane-blind (ADR-0027), and every token the key drops names the convention and the corpus files that
prove it. Splitting a library gives quarter-depth matrices a human notices; merging two gives one
plausible matrix nobody notices, so this key may fail toward the first and never toward the second.
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

#: A TRAILING bcl2fastq lane token, matched once the mate token is off. A run spans every lane it was
#: loaded into (ADR-0027), so this comes OFF the key: keeping it made a four-lane library four runs,
#: and with no record a run is the sample identity — four samples at a quarter depth, exit 0 (#263).
#:
#: `\d{3}` and never `\d+`, because `L<n>` is not a lane-only namespace:
#: `XQTL_F4_N2PTM299_L2_1_S2_L004_R1_001.fastq.gz` (15 corpus files) spells the worm's LARVAL STAGE
#: `L2` in the same name it spells a lane `L004`. The digit count is the only thing telling them
#: apart — all 250 real lane tokens in the benchmark tier are three digits, because bcl2fastq pads
#: and a larval stage does not. `_L\d+` bites wherever the mate strip leaves such a token trailing
#: (`worm_L2_R1_001.fastq.gz` -> `worm_L2`), fusing three stages into one run.
#:
#: Case-SENSITIVE, unlike its siblings `_MATE` and `_ILLUMINA_DESIGNATION` (both `re.I`), and that is
#: the asymmetry above applied to a token nobody has observed in lower case: a missed `_l001` splits a
#: library, which is loud and recoverable, while a wrongly-matched one merges two and is neither.
#:
#: `_S<n>` is deliberately NOT stripped: it is the sample-sheet entry, the one token separating two
#: libraries on one flowcell, and a library resequenced as `_S3` is a second run of the same sample
#: that only a record may rejoin. `(?P<stem>.+)` is the floor — a strip that would leave nothing keeps
#: the name, so `L001_R1_001.fastq.gz` stays `L001`.
_LANE = re.compile(r"^(?P<stem>.+)[._](?P<lane>L\d{3})$")


def _strip_ext(name: str) -> str:
    lowered = name.lower()
    for ext in _EXTS:
        if lowered.endswith(ext):
            return name[: -len(ext)]
    return name


def _strip_lane(stem: str) -> tuple[str, str]:
    """`("cell_42_S1_L001")` -> `("cell_42_S1", "L001")`; no lane token -> `(stem, "")`."""
    match = _LANE.match(stem)
    return (match.group("stem"), match.group("lane")) if match else (stem, "")


def run_key(path: str | Path) -> str:
    """The run a file belongs to, derived from its name. Never a claim about the file's ROLE.

    `SRR28716558_1.fastq.gz` -> `SRR28716558`; `SRR36109512_11314-RM-1_S1_L005_R1_001.fastq.gz` ->
    `SRR36109512` (a leading accession wins over any submitter suffix); `x_S1_L001_R1_001.fastq.gz` ->
    `x_S1`, because a run spans its lanes (ADR-0027); a name with no accession, no mate token and no
    lane is its own run, which is the right answer for a single-end library.

    A pure function of ONE basename, never of the directory: the same file resolves to the same run
    whether or not its siblings have arrived, so a neighbour landing cannot move a sample identity —
    and `dataset_hash` with it.
    """
    stem = _strip_ext(Path(path).name)
    sra = _SRA_RUN.match(stem)
    if sra is not None:
        return sra.group(1)
    match = _MATE.match(stem)
    return _strip_lane(match.group("stem") if match else stem)[0]


def lane_of(path: str | Path) -> str:
    """The flowcell lane a file came from — `L001` — or `""` when the name carries none.

    The lane comes OFF the run key (ADR-0027) and survives here as data: `compose` writes it into
    `units.tsv` so a sample's files order identically for every mate. Same `_LANE` as the strip, so
    what a lane IS is decided once and the column can never disagree with the grouping.

    Read on the mate-stripped stem whichever branch `run_key` took, because a lane sits INSIDE a run
    either way — GSE310378 deposits `SRR36109512_11314-RM-1_S1_L005_R1_001.fastq.gz`, keyed on the
    accession and still lane `L005`.
    """
    stem = _strip_ext(Path(path).name)
    match = _MATE.match(stem)
    return _strip_lane(match.group("stem") if match else stem)[1]


def group_runs(paths: Sequence[str | Path]) -> dict[str, list[Path]]:
    """Group `paths` into runs, preserving input order within each run and sorting the runs by key.

    Every input path lands in exactly one group; nothing is dropped, deduplicated, or reordered across
    groups. That is worth stating because the bug this module fixes was files silently disappearing.
    """
    groups: dict[str, list[Path]] = {}
    for path in paths:
        groups.setdefault(run_key(path), []).append(Path(path))
    return {key: groups[key] for key in sorted(groups)}
