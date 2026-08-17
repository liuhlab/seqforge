"""PROTOTYPE — throwaway. A toy `io split-chimera` that can be broken on purpose.

**The question this answers** (ticket #414 on map #406): *what is the cheapest test that would
actually catch a broken split?* Not "what does a correct splitter do" — that is settled in the split
contract (#409). This module exists so that a deliberately-defective splitter can be run against a
candidate assertion set, and the assertions that never go red can be deleted from the real test.

So the code below is the contract plus a `Breakage` switchboard: one flag per plausible way a real
implementation goes wrong. Correct behaviour is `Breakage()` with every flag false.

Not production code, and not shaped like it: no CLI, no refusal exit codes, no `liulab-genome`
resolution. The one thing it does share with the real verb is the seam the contract names — the work
is a pure function over `(bam, components, separator)`, so no built chimera has to exist on disk.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from genome.chimera import split_suffixed

SPLITTER_VERSION = "2026.8.0"


class SplitRefusal(RuntimeError):
    """A chromosome the reference cannot explain — the contract's one refusal."""


@dataclass(frozen=True)
class Breakage:
    """Deliberate defects. Each flag is one plausible way a real splitter goes wrong."""

    hardcoded_separator: bool = False  # splits at "__" whatever the chimera recorded
    hardcoded_separator_long: bool = False  # the same bug written by someone who tested on tinyEcDub
    skip_unsplittable: bool = False  # a name that will not split is skipped, not refused
    keep_suffix: bool = False  # @SQ names left suffixed
    no_refdict_remap: bool = False  # text header rewritten, records keep their old tids
    tid_off_by_one: bool = False  # the remap is off by one but STAYS IN RANGE, so nothing can crash
    mate_tid_not_remapped: bool = False  # RNAME remapped, RNEXT left on the old tid
    naive_component: bool = False  # the name contract re-implemented locally as `RNAME.split("__")[-1]`
    no_mate_check: bool = False  # the free per-record mate check is not made
    drop_read2: bool = False  # the keep rule tests `is_read1` and loses every second mate
    uncounted_skip: bool = False  # a dropped record is dropped without being counted
    keep_multimappers: bool = False  # NH > 1 routed by suffix instead of dropped
    keep_unmapped: bool = False  # unmapped passed through instead of dropped
    sorted_sq: bool = False  # @SQ sorted by name instead of order-preserved
    drop_hd: bool = False  # @HD lost (so SO:coordinate goes with it)
    no_appended_pg: bool = False  # the splitter never records itself
    rewrite_star_pg: bool = False  # STAR's @PG/@CO rewritten to read as a single-assembly run
    allow_partial: bool = False  # a Component the caller did not name is dropped in silence


@dataclass
class Summary:
    """The `<cell>.split.json` payload, at prototype resolution."""

    records_in: int = 0
    kept: Counter[str] = field(default_factory=Counter)
    dropped: Counter[str] = field(default_factory=Counter)
    read1: Counter[str] = field(default_factory=Counter)
    read2: Counter[str] = field(default_factory=Counter)

    def closes(self) -> bool:
        return self.records_in == sum(self.kept.values()) + sum(self.dropped.values())


SKIPPED = ("", "\0skipped")


def _component_of(name: str, separator: str, breakage: Breakage) -> tuple[str, str]:
    sep = separator
    if breakage.hardcoded_separator:
        sep = "__"
    elif breakage.hardcoded_separator_long:
        sep = "___"
    if breakage.naive_component:
        # The one-liner someone writes instead of importing `liulab-genome`'s helper. It is right on
        # every name a `__` chimera spells, which is what makes it survive review.
        parts = name.split("__")
        return "__".join(parts[:-1]), parts[-1]
    try:
        bare, component = split_suffixed(name, sep)
    except Exception as exc:  # noqa: BLE001 — prototype: any failure to split is the refusal
        if breakage.skip_unsplittable:
            return SKIPPED
        raise SplitRefusal(f"{name!r} does not split at {sep!r}") from exc
    return bare, component


def _header_for(
    src: dict[str, Any], component: str, separator: str, breakage: Breakage
) -> tuple[Any, dict[int, int]]:
    """The Component's header, plus old-tid -> new-tid for the binary reference dictionary."""
    import pysam

    sq: list[dict[str, Any]] = []
    tid_map: dict[int, int] = {}
    for old_tid, entry in enumerate(src.get("SQ", [])):
        bare, owner = _component_of(entry["SN"], separator, breakage)
        if owner != component:
            continue
        tid_map[old_tid] = len(sq)
        sq.append({**entry, "SN": entry["SN"] if breakage.keep_suffix else bare})

    if breakage.sorted_sq:
        order = sorted(range(len(sq)), key=lambda i: sq[i]["SN"])
        sq = [sq[i] for i in order]
        rank = {old: order.index(new) for old, new in tid_map.items()}
        tid_map = rank

    out: dict[str, Any] = {"SQ": sq}
    if not breakage.drop_hd:
        out["HD"] = src.get("HD", {})

    pg = list(src.get("PG", []))
    co = list(src.get("CO", []))
    if breakage.rewrite_star_pg:
        # Reads as a single-assembly run: a command line nobody ran, which is why #409 kept STAR's
        # lines verbatim and scoped the identity bar to @SQ + @HD.
        pg = [{**p, "CL": f"STAR --genomeDir {component}"} for p in pg]
        co = [f"user command line: STAR --genomeDir {component}" for _ in co]
    if not breakage.no_appended_pg:
        pg.append(
            {
                "ID": "seqforge-split-chimera",
                "PN": "seqforge",
                "VN": SPLITTER_VERSION,
                "CL": f"seqforge io split-chimera --component {component}",
                **({"PP": pg[-1]["ID"]} if pg else {}),
            }
        )
    if pg:
        out["PG"] = pg
    if co:
        out["CO"] = co
    return pysam.AlignmentHeader.from_dict(out), tid_map


def _rewritten(rec: Any, header: Any, tid_map: dict[int, int], breakage: Breakage) -> Any:
    import pysam

    new = pysam.AlignedSegment(header)
    new.query_name = rec.query_name
    new.flag = rec.flag
    n_refs = header.nreferences
    if breakage.no_refdict_remap:
        new.reference_id = rec.reference_id
    elif breakage.tid_off_by_one:
        new.reference_id = (tid_map[rec.reference_id] + 1) % n_refs
    else:
        new.reference_id = tid_map[rec.reference_id]
    new.reference_start = rec.reference_start
    new.mapping_quality = rec.mapping_quality
    new.cigar = rec.cigar
    if rec.next_reference_id >= 0:
        if breakage.no_refdict_remap or breakage.mate_tid_not_remapped:
            new.next_reference_id = rec.next_reference_id
        elif breakage.tid_off_by_one:
            new.next_reference_id = (tid_map[rec.next_reference_id] + 1) % n_refs
        else:
            new.next_reference_id = tid_map[rec.next_reference_id]
        new.next_reference_start = rec.next_reference_start
    new.template_length = rec.template_length
    new.query_sequence = rec.query_sequence  # before qualities: pysam clears them on assignment
    new.query_qualities = rec.query_qualities
    new.set_tags(rec.get_tags(with_value_type=True))
    return new


def split(
    bam: Path,
    outputs: dict[str, Path],
    separator: str,
    breakage: Breakage = Breakage(),
) -> Summary:
    """One chimeric BAM -> one BAM per requested Component, plus the summary.

    Stateless and per-record: the contract's keep rule is *mapped, uniquely-placed, primary*, and
    everything else is dropped and counted by reason.
    """
    import pysam

    summary = Summary()
    with pysam.AlignmentFile(str(bam), "rb") as src:
        header = src.header.to_dict()

        present = {
            _component_of(entry["SN"], separator, breakage)[1] for entry in header.get("SQ", [])
        } - {SKIPPED[1]}
        unasked = present - set(outputs)
        if unasked and not breakage.allow_partial:
            raise SplitRefusal(f"BAM carries Components nobody asked for: {sorted(unasked)}")

        built = {c: _header_for(header, c, separator, breakage) for c in outputs}
        writers = {
            c: pysam.AlignmentFile(str(outputs[c]), "wb", header=built[c][0]) for c in outputs
        }
        try:
            for rec in src.fetch(until_eof=True):
                summary.records_in += 1
                if rec.is_unmapped and not breakage.keep_unmapped:
                    summary.dropped["unmapped"] += 1
                    continue
                if rec.is_secondary:
                    summary.dropped["secondary"] += 1
                    continue
                if rec.is_supplementary:
                    summary.dropped["supplementary"] += 1
                    continue
                hits = rec.get_tag("NH") if rec.has_tag("NH") else 1
                if hits > 1 and not breakage.keep_multimappers:
                    if not breakage.uncounted_skip:
                        summary.dropped["multimapping"] += 1
                    continue

                if breakage.drop_read2 and rec.is_read2:
                    summary.dropped["read2"] += 1
                    continue

                _, component = _component_of(rec.reference_name, separator, breakage)
                if component not in writers:
                    summary.dropped["unrequested"] += 1
                    continue

                # #409's free per-record check: the mate is on this record's own Component. Without
                # it the tid lookup below simply raises, naming neither the read nor the Components.
                if not breakage.no_mate_check and rec.next_reference_id >= 0:
                    _, mate_component = _component_of(
                        src.get_reference_name(rec.next_reference_id), separator, breakage
                    )
                    if mate_component != component:
                        raise SplitRefusal(
                            f"{rec.query_name}: record in {component}, mate in {mate_component}"
                        )

                writers[component].write(_rewritten(rec, *built[component], breakage))
                summary.kept[component] += 1
                if rec.is_read1:
                    summary.read1[component] += 1
                if rec.is_read2:
                    summary.read2[component] += 1
        finally:
            for writer in writers.values():
                writer.close()
    return summary
