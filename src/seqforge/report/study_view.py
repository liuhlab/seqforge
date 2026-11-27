"""The Study tab's data: a study folder, and the workspaces that compiled it, as a :class:`StudyReport`.

Read from a folder the study package has already opened and checked
(:func:`~seqforge.study.folder.open_study_folder`), so no file name of a study folder is spelled
here: the bounds graph's records, the design table, the sample tables by topic and each sample's
coverage by the workspaces the report read. Each section past its cap counts what it hides.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .model import (
    CROSS_COLUMNS_CAP,
    GRAPH_KEPT_CAP,
    GRAPH_LEFT_OUT_CAP,
    GROUPED_ROWS_CAP,
    SAMPLE_ROWS_CAP,
    TEXT_CAP,
    BoundsRecord,
    Cut,
    DesignCount,
    DesignGroup,
    DesignTable,
    PassageView,
    SameForAll,
    StudyCell,
    StudyFactor,
    StudyReport,
    StudySample,
    TopicTable,
    WorkspaceReport,
    relative_path,
)

if TYPE_CHECKING:
    from ..study.claims import Claim, Passage
    from ..study.folder import StudyFolder
    from ..study.reading import Reading, SampleClaim


def collect_study(
    study: StudyFolder, report_dir: Path, workspaces: Sequence[WorkspaceReport] = ()
) -> StudyReport:
    """The Study tab's data: every record the bounds placed, kept and left out, and why.

    Read from a folder the study package has already opened and checked, so no file name of a study
    folder is spelled here. Each record carries the reading's passages quoted from it: a passage
    read from that record, or from that paper. Each sample names the ``workspaces`` that compiled
    it, before the sample tables fold their rows.
    """
    bounds = study.bounds
    quotes = study.reading.passages if study.reading is not None else {}

    def record(id_: str, claim: Claim | None, *, kept: bool) -> BoundsRecord:
        backing = None if claim is None else _backing(claim)
        cites = [] if claim is None else (claim.quoted or claim.see or [])
        return BoundsRecord(
            id=id_,
            kind="paper" if id_.startswith("pmid:") else "series",
            kept=kept,
            title=bounds.titles.get(id_),
            backing=backing,
            reason=None if claim is None else (claim.inferred or claim.unknown),
            reason_passages=[_passage_view(n, bounds.passages[n]) for n in cites],
            p=None if claim is None else claim.p,
            superseries=study.superseries if id_ in study.subseries else None,
            passages=[
                _passage_view(n, q) for n, q in sorted(quotes.items()) if _quotes_from(q, id_)
            ],
        )

    records = [record(c.id, c, kept=True) for c in bounds.series]
    records += [record(paper, None, kept=True) for paper in bounds.papers]
    records += [record(c.id, c, kept=False) for c in bounds.left_out]
    records, graph = _drawn(records)
    view = StudyReport(
        study=bounds.study,
        path=relative_path(study.path, report_dir),
        reason=bounds.reason,
        records=records,
        graph=graph,
        has_reading=study.reading is not None,
        reading_hash=None if study.reading is None else study.reading.content_hash(),
    )
    if study.reading is None:
        return view
    return view.model_copy(update=_reading_view(study.reading, workspaces))


def _drawn(
    records: list[BoundsRecord],
) -> tuple[list[BoundsRecord], dict[Literal["series", "paper"], Cut]]:
    """Which records the graph draws, and how many of each side's it draws and lists instead.

    Each side draws its first kept records and its first left-out ones, each up to its cap; the
    rest of the side are listed under one "+k more" node.
    """
    seen: dict[tuple[str, bool], int] = {}
    drawn = []
    for r in records:
        at = seen[r.kind, r.kept] = seen.get((r.kind, r.kept), 0) + 1
        cap = GRAPH_KEPT_CAP if r.kept else GRAPH_LEFT_OUT_CAP
        drawn.append(r if at <= cap else r.model_copy(update={"drawn": False}))
    kinds: tuple[Literal["series", "paper"], ...] = ("series", "paper")
    graph = {
        kind: Cut(
            shown=sum(r.drawn for r in drawn if r.kind == kind),
            hidden=sum(not r.drawn for r in drawn if r.kind == kind),
        )
        for kind in kinds
    }
    return drawn, graph


def _reading_view(reading: Reading, workspaces: Sequence[WorkspaceReport]) -> dict[str, Any]:
    """The reading's part of the Study tab: its samples, factors, design table and topic tables."""
    factors = [
        StudyFactor(label=label, levels=f.levels, baseline=f.baseline, ordered=f.ordered, p=f.p)
        for label, f in reading.factors.items()
    ]
    samples = [
        StudySample(
            sample=sample,
            biosample_accession=reading.samples[sample].biosample_accession,
            cells={
                label: _study_cell(
                    label, claim, inherited=label not in reading.samples[sample].claims
                )
                for label, claim in row.items()
            },
        )
        for sample, row in reading.rows().items()
    ]
    samples = [_coverage(sample, workspaces) for sample in samples]

    def rank(sample: StudySample) -> list[int]:
        # A sample at no level of a factor comes after every sample at one.
        return [
            f.levels.index(level)
            if (level := _level(sample, f.label)) is not None
            else len(f.levels)
            for f in factors
        ]

    samples.sort(key=rank)  # stable: the reading's own order within a group
    return {
        "passages": [_passage_view(n, q) for n, q in sorted(reading.passages.items())],
        "factors": factors,
        "samples": samples,
        "design": _design_table(factors, samples),
        "topics": _topic_tables(reading, samples),
    }


def _topic_tables(reading: Reading, samples: list[StudySample]) -> list[TopicTable]:
    """One sample table per topic that has a label, factors aside, in the label table's order.

    A label's topic is the one its row in the label table declares; a label of the study's own is
    in Other. A label whose claim is the same for every sample folds into the table's "same for
    all" line, whether each sample takes the claim from ``shared`` or writes it again.
    """
    from ..study.labels import LABELS, TOPICS, label_row

    written = [label for s in samples for label in s.cells]
    labels = [
        label
        for label in dict.fromkeys([*(row.name for row in LABELS), *written])
        if label in written and label not in reading.factors
    ]
    tables = []
    for topic in TOPICS:
        mine = [label for label in labels if label_row(label).topic == topic]
        if not mine:
            continue
        varying, same = [], []
        for label in mine:
            if (folded := _same_for_all(label, samples)) is None:
                varying.append(label)
            else:
                same.append(SameForAll(label=label, cell=folded))
        rows = _rows([*reading.factors, *varying], samples) if varying else []
        tables.append(
            TopicTable(
                topic=topic,
                labels=mine,
                varying=varying,
                same=same,
                rows=rows,
                folded=sum(len(row) - 1 for row in rows),
                cut=Cut.capped(len(rows), SAMPLE_ROWS_CAP),
            )
        )
    return tables


def _rows(labels: list[str], samples: list[StudySample]) -> list[list[str]]:
    """A topic table's rows, each the samples it shows: one each, up to the cap.

    Past the cap, the samples whose row shows the same on every one of ``labels`` share one row,
    placed where its first sample is. A row shows a cell's value, level, backing, p and whether it
    is inherited, and the workspaces that compiled its samples, so those are what must be the same;
    the passages behind a cell are not shown in the row, and the first sample's open on a click.
    """
    if len(samples) <= SAMPLE_ROWS_CAP:
        return [[s.sample] for s in samples]
    rows: dict[tuple[object, ...], list[str]] = {}
    for s in samples:
        shown = tuple(
            None
            if (c := s.cells.get(label)) is None
            else (c.value, c.level, c.backing, c.p, c.inherited)
            for label in labels
        )
        rows.setdefault((tuple(s.compiled_in), *shown), []).append(s.sample)
    return list(rows.values())


def _same_for_all(label: str, samples: list[StudySample]) -> StudyCell | None:
    """The one claim every sample has on ``label``, or ``None`` when the label varies.

    Where a sample took the claim from makes no difference to what it says, so the folded claim
    is inherited only when every sample's is.
    """
    from ..study.labels import varies

    cells = [s.cells.get(label) for s in samples]
    said = [None if c is None else c.model_copy(update={"inherited": False}) for c in cells]
    if not said or said[0] is None or varies(said):
        return None
    return said[0].model_copy(update={"inherited": all(c.inherited for c in cells if c)})


def _study_cell(label: str, claim: SampleClaim, *, inherited: bool) -> StudyCell:
    """One claim as a sample table's cell: what it says, its level, and how it is backed."""
    from ..study.join import cell

    term = claim.term
    # The level is a field of its own, so the value is what the claim says beside it.
    value = cell(label, claim.model_copy(update={"level": None})) if claim.says else None
    return StudyCell(
        value=value,
        hidden_chars=_hidden_chars(value),
        term=[term] if isinstance(term, str) else list(term or []),
        level=claim.level,
        backing="na" if claim.na is not None else _backing(claim),
        note=claim.inferred or claim.unknown or claim.na,
        p=claim.p,
        inherited=inherited,
        passages=claim.quoted or claim.see or [],
        against=claim.against or [],
    )


def _level(sample: StudySample, factor: str) -> str | None:
    """The level of ``factor`` the sample is at, or ``None`` when its claim names none."""
    claim = sample.cells.get(factor)
    return None if claim is None else claim.level


def _assay(sample: StudySample) -> str:
    """The sample's assay as the design table splits by it: what its claim says, or its gap."""
    claim = sample.cells.get("assay")
    if claim is None:
        return "no claim"
    return claim.value if claim.value is not None else claim.backing


def _design_table(factors: list[StudyFactor], samples: list[StudySample]) -> DesignTable:
    """The samples counted by combination of factor levels: a cross-table for two factors.

    ``samples`` are in the factors' level order, so a grouped list's groups are too.
    """
    assays = list(dict.fromkeys(_assay(s) for s in samples))
    split = len(assays) > 1

    def counts(members: list[StudySample]) -> list[DesignCount]:
        if not split:
            return [DesignCount(samples=[s.sample for s in members])] if members else []
        return [
            DesignCount(assay=a, samples=[s.sample for s in members if _assay(s) == a])
            for a in assays
            if any(_assay(s) == a for s in members)
        ]

    def axis(f: StudyFactor) -> list[str | None]:
        outside = any(_level(s, f.label) is None for s in samples)
        return [*f.levels, *([None] if outside else [])]

    # Two factors whose cross-table is too wide for the page fall back to the grouped list.
    wide = len(factors) == 2 and len(axis(factors[1])) > CROSS_COLUMNS_CAP
    if len(factors) == 2 and not wide:
        first, second = factors
        rows, columns = axis(first), axis(second)
        groups = [
            DesignGroup(
                levels=[r, c],
                counts=counts(
                    [
                        s
                        for s in samples
                        if _level(s, first.label) == r and _level(s, second.label) == c
                    ]
                ),
            )
            for r in rows
            for c in columns
        ]
        return DesignTable(
            kind="cross",
            rows=rows,
            columns=columns,
            groups=groups,
            assays=assays if split else [],
        )
    by_levels: dict[tuple[str | None, ...], list[StudySample]] = {}
    for s in samples:
        by_levels.setdefault(tuple(_level(s, f.label) for f in factors), []).append(s)
    return DesignTable(
        kind="grouped",
        groups=[DesignGroup(levels=list(k), counts=counts(v)) for k, v in by_levels.items()],
        assays=assays if split else [],
        cross_columns=len(axis(factors[1])) if wide else None,
        cut=Cut.capped(len(by_levels), GROUPED_ROWS_CAP),
    )


def _backing(claim: Claim) -> Literal["quoted", "inferred", "unknown"]:
    """The one backing mark a checked claim holds."""
    if claim.quoted is not None:
        return "quoted"
    return "inferred" if claim.inferred is not None else "unknown"


def _quotes_from(passage: Passage, record: str) -> bool:
    """Whether ``passage`` was read from ``record``: that paper, or that record or one of a list."""
    if passage.paper is not None:
        return passage.paper == record
    return record == passage.record or (
        isinstance(passage.record, list) and record in passage.record
    )


def _passage_view(number: int, passage: Passage) -> PassageView:
    """One passage under its number, with where it was read joined for a reader."""
    if passage.paper is not None:
        source = f"{passage.paper} · {passage.section}"
    else:
        record = passage.record
        named = ", ".join(record) if isinstance(record, list) else record
        source = f"{named} · {passage.field}"
    return PassageView(
        number=number,
        source=source,
        quote=passage.quote,
        hidden_chars=_hidden_chars(passage.quote),
    )


def _hidden_chars(text: str | None) -> int:
    """How many characters of ``text`` are past the cap, and so cut on the page until a click."""
    return max(len(text or "") - TEXT_CAP, 0)


def _coverage(sample: StudySample, workspaces: Sequence[WorkspaceReport]) -> StudySample:
    """``sample`` marked with each workspace whose manifest holds its BioSample accession.

    The compile's join meets the two sides on the same id, so a sample is compiled where the join
    would label it.
    """
    accession = sample.biosample_accession
    if accession is None:
        return sample
    return sample.model_copy(
        update={
            "compiled_in": [
                ws.path
                for ws in workspaces
                if any(s.accession == accession for a in ws.assays for s in a.samples)
            ]
        }
    )
