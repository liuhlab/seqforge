"""The report's display model — a frozen projection of on-disk artifacts, not the artifacts.

``collect.py`` reads a workspace and builds a :class:`ProjectReport`; ``render.py`` turns it into one
HTML page. Keeping a typed tree between them means the renderer never reaches into a raw manifest or a
loose dict, the projection is validated once, and the same object serialises to the stdout summary.

Everything here is a *view*: it flattens the manifest's ``Evidenced`` envelopes to
``(value, basis, confidence, rung, evidence)`` and resolves each evidence token to something a human
can read (a quote, an accession, a policy rule). It is deliberately modality-general — read roles,
quantification, and the composed config are carried as generic shapes, never STARsolo-typed fields —
so a future non-STAR pipeline renders through the same tree unchanged. The one shape defined
elsewhere, :class:`~seqforge.workflows.metrics.PipelineStats`, keeps that property: it carries its own
column set, so a chromap fragments summary and a STARsolo bundle both arrive as one list of graded
metrics and neither tool's vocabulary is typed here either.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models.blocker import ValidationWarning
from ..workflows.metrics import Alert, PipelineStats


class _View(BaseModel):
    """Frozen base for every projection — a report is a snapshot, never mutated after collection."""

    model_config = ConfigDict(frozen=True)


class EvidenceRef(_View):
    """One evidence token, resolved to whatever a reader can actually follow.

    An ``evidence`` list on a manifest field holds bare strings whose *shape* says what they are: an
    ``assert-…`` id (a harvested claim, joinable to its quote), a bare accession (a record declared
    it), a ``policy:<rule>`` / ``cli:<flag>`` token (who decided a processing field), or a file sha
    (bytes). The collector dispatches on shape and fills only the fields that apply; the renderer shows
    ``raw`` when nothing else resolved, so an unrecognised token degrades to itself rather than crashing.
    """

    raw: str
    kind: Literal["assertion", "accession", "policy", "cli", "file_sha", "other"]
    quote: str | None = None
    page: int | None = None
    document: str | None = None
    accession: str | None = None


class SampleView(_View):
    """One biological sample and the files that carry it."""

    sample_id: str
    accession: str | None = None
    n_files: int = 0
    file_names: list[str] = Field(default_factory=list)


class AssayLabelView(_View):
    """A chemistry spelled three ways — our key, its assay's ontology id, and the ontology's words.

    ``name`` is ``None`` when the ontology store was not there to ask.
    """

    chemistry: str
    curie: str
    name: str | None = None


class ElementView(_View):
    """One sub-region of a read (a barcode block, a UMI, the cDNA), as the layout declares it."""

    role: str
    region_type: str
    start: int | None = None
    length: int | None = None
    onlist_ref: str | None = None
    anchored: bool = False


class ReadView(_View):
    """One sequencing read (== one FASTQ role) and the elements packed into it."""

    read_id: str
    strand: str
    min_len: int
    max_len: int
    elements: list[ElementView] = Field(default_factory=list)


class OnlistView(_View):
    """A barcode whitelist the winning chemistry uses."""

    name: str
    length: int | None = None
    n_entries: int | None = None


class FileView(_View):
    """One physical FASTQ, its assigned read role, and its size."""

    basename: str
    read_id: str | None = None
    sha256: str
    size_bytes: int
    uri: str


class ChemistryDecision(_View):
    """The one evidenced decision the whole library follows from: what chemistry this is.

    ``value`` is the equivalence class (``[10x-3p-gex-v3, 10x-3p-gex-v3.1]``) — one member or several,
    processing-equivalent. This is what the Overview badge, the Flow node, and the Evidence tab all key
    off, and it is always present because it lives in the manifest.
    """

    value: list[str]
    assay_labels: list[AssayLabelView] = Field(default_factory=list)
    basis: str
    confidence: float | None = None
    rung: int
    modality: str
    n_files: int
    evidence_shas: list[str] = Field(default_factory=list)


class MatrixCellView(_View):
    """One evidence-matrix cell: a scored support in ``[0,1]`` or a forbidden gate with its reason."""

    status: Literal["scored", "forbidden"]
    value: float | None = None
    reason: str | None = None


class MatrixRoleRow(_View):
    """One role's row across the dataset's files (cells aligned to the column order)."""

    role: str
    cells: list[MatrixCellView] = Field(default_factory=list)


class MatrixView(_View):
    """One technology's evidence matrix — why it won or why a gate forbade it.

    ``file_labels`` are the column headers (file basenames) aligned to every row's ``cells``. Present
    only when the persisted sidecar was found; the collector degrades to per-candidate scores otherwise.
    """

    tech: str
    is_winner: bool = False
    score: float | None = None
    file_labels: list[str] = Field(default_factory=list)
    roles: list[MatrixRoleRow] = Field(default_factory=list)


class RuledOut(_View):
    """A chemistry the probe scored and rejected — a one-line summary, not a full grid.

    The probe scores every geometrically-plausible chemistry and rules the wrong ones out by evidence;
    showing all of them as grids is noise. The winner's own family stays a grid (that is where the real
    v2-vs-v3 discrimination lives); every other family collapses to one of these rows so the reader sees
    *that* it was considered and *why* it lost, without the wall of cells.
    """

    tech: str
    family: str
    reason: str


class DecisionField(_View):
    """A processing field rendered for a human: its value, who decided, and on what evidence.

    Reused for every recipe field (genome, aligner, quantification, …) so the Pipeline tab renders a
    uniform row per decision with a basis badge, without a bespoke shape per field.
    """

    label: str
    value: str
    basis: str
    confidence: float | None = None
    rung: int
    evidence: list[EvidenceRef] = Field(default_factory=list)


class PlanView(_View):
    """The recipe — what to DO with this assay — plus the composed config as opaque key/value.

    ``config`` is a flat list of ``(key, value)`` strings taken verbatim from the composed
    ``config.yaml``: the report never types STARsolo's fields, it shows whatever the composer emitted.
    The ``*_rel`` paths point at the deliverables from the report's own directory, or are ``None``
    when this assay reached the IR but was never composed.

    ``quantification_kind`` is the one exception to "everything here is a display string", and it
    exists because the stage diagram has to *branch*. It used to branch on the caption in ``fields``
    — a string the collector had rendered a few lines earlier — so rewording "atac: fragments…" would
    have silently reverted an ATAC dataset's diagram to "align with STAR, count reads per gene", with
    no test failing and nothing on the page admitting it. Carrying the recipe's own discriminator
    makes the caption cosmetic again.

    Carried as ``str`` rather than as the ``Literal`` the recipe declares, deliberately: this is a
    *view*, built from an already-validated manifest, and a fifth quantification family must render
    (through the fallback branch) rather than raise a validation error inside a page whose whole
    contract is to degrade instead of failing. The closed set is enforced where it is decided, in
    ``models/processing.py``.
    """

    fields: list[DecisionField] = Field(default_factory=list)
    quantification_kind: str | None = None
    resources: list[tuple[str, str]] = Field(default_factory=list)
    primary_feature: str | None = None
    config: list[tuple[str, str]] = Field(default_factory=list)
    pipeline_name: str | None = None
    snakefile_rel: str | None = None
    config_rel: str | None = None
    units_rel: str | None = None


class ArtifactEmbed(_View):
    """A workspace artifact carried *into* the page so it stays self-contained.

    A relative link to ``pipeline/.../Snakefile`` breaks the moment the HTML is moved off the workspace
    — which is the whole point of a one-file report. So the text is embedded: the panel shows it inline
    and offers a ``data:`` URI download, and nothing points at a sibling file that may not be there.
    """

    name: str
    mime: str
    text: str
    size_bytes: int


class PipelineStage(_View):
    """One fixed stage of the composed pipeline, for a small human-readable stage diagram.

    Derived from the recipe (the quantification family), not from parsing the Snakefile: a compact
    "what will run, in order" that a biologist can follow, without rendering a giant per-sample DAG.
    """

    key: str
    title: str
    detail: str


class ArchiveProjectView(_View):
    """The archive's project as it declared it, plus its abstract when a record carried one."""

    accession: str | None = None
    title: str | None = None
    center: str | None = None
    data_type: str | None = None
    released: str | None = None
    abstract: str | None = None


class H5adPointer(_View):
    """One ``.h5ad`` a finished pipeline left on disk — the matrices stay there, and this points.

    ``path`` is relative to the directory the report was written into, so it holds wherever ``-o``
    put the report. ``sample`` is the sample the file belongs to, or ``None`` for a **Fan-in
    artifact**, which has no sample in its path because it holds every one.
    """

    path: str
    sample: str | None = None


class ConclusionView(_View):
    """How this assay's compile ended — derived honestly from which artifacts are on disk.

    ``compiled`` (manifest + a composed Snakefile), ``ir_ready`` (manifest, not yet composed),
    ``blocker`` (a persisted refusal), ``question`` (an open conflict/question). Never manufactured
    from a merely-incomplete workspace: the default is a clean exit 0.
    """

    kind: Literal["compiled", "ir_ready", "blocker", "question"]
    exit_code: int
    headline: str
    detail: str
    blockers: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)


class AssayReport(_View):
    """One assay: its identity, its samples, its evidence, its recipe, and how it ended.

    A single flat dataset is one of these with ``subdir=None``; a heterogeneous project is several.
    """

    subdir: str | None = None
    accessions: list[str] = Field(default_factory=list)
    organism_taxid: int | None = None
    organism_name: str | None = None
    organism_basis: str | None = None
    project: ArchiveProjectView | None = None
    chemistry: ChemistryDecision
    reads: list[ReadView] = Field(default_factory=list)
    onlists: list[OnlistView] = Field(default_factory=list)
    files: list[FileView] = Field(default_factory=list)
    samples: list[SampleView] = Field(default_factory=list)
    plan: PlanView | None = None
    matrices: list[MatrixView] = Field(default_factory=list)
    ruled_out: list[RuledOut] = Field(default_factory=list)
    artifacts: list[ArtifactEmbed] = Field(default_factory=list)
    pipeline_stages: list[PipelineStage] = Field(default_factory=list)
    conclusion: ConclusionView
    #: The finished pipeline's per-sample metrics, or None when it has not run (or not finished a
    #: single sample). Deliberately NOT folded into `conclusion`: that carries how the COMPILE ended,
    #: and "the compiler succeeded" and "the pipeline succeeded" are two judgements, which means two
    #: envelopes. Merged, a page would say "Compiled" over a pipeline that mapped nothing, and a
    #: reader would have no way left to ask which of the two it was being told about.
    pipeline_stats: PipelineStats | None = None
    #: Which decision the finished pipeline's numbers make look wrong — attributed, so each one
    #: carries the value the workspace currently has. **Advisory and empty on a healthy run**: seeing
    #: one has to mean something, so there is no "no alerts" state to render. Beside `pipeline_stats`
    #: and not inside it for the reason that one is beside `conclusion`: the stats are what the
    #: pipeline reported, an alert is a judgement about a decision, and one judgement is one envelope.
    alerts: list[Alert] = Field(default_factory=list)
    #: Every ``.h5ad`` the pipeline wrote, found on disk rather than predicted: a pointer to a file
    #: that is not there would send a program to nothing. Empty before the pipeline runs, and for
    #: the bulk and ATAC pipelines, which write no ``.h5ad``.
    h5ad: list[H5adPointer] = Field(default_factory=list)
    provenance: list[tuple[str, str]] = Field(default_factory=list)
    #: True iff archive records were joined (the samples have a declared source).
    has_records: bool = False
    #: True iff a paper/instruction document was read and quoted (harvest ran with real prose).
    has_prose: bool = False

    @property
    def n_samples(self) -> int:
        """How many samples the assay's manifest declares, whether or not any attribute was found.

        Counted from ``samples``, one per manifest sample, and not from the pipeline's metrics.
        """
        return len(self.samples)

    @property
    def n_files(self) -> int:
        """How many FASTQ files the assay's manifest lists, every role counted.

        Index reads and files no role claims count too: ``files`` is the library's whole file list.
        """
        return len(self.files)

    @property
    def label(self) -> str:
        """A human title for this assay's section — its subdir, or its winning chemistry."""
        return self.subdir or (self.chemistry.value[0] if self.chemistry.value else "assay")


class WorkspaceReport(_View):
    """One compile workspace the report read: where it is, and its assays.

    ``path`` is the workspace root **relative to the report's own directory**, as every path in
    ``report.json`` is, so a program holding the JSON finds the manifest and the pipeline without
    knowing where the report was written or how a workspace is laid out.
    """

    name: str
    path: str
    assays: list[AssayReport] = Field(default_factory=list)
    #: The hash of the reading this workspace's sample table joined, ``None`` when it joined none.
    reading_hash: str | None = None


#: The caps of the Study tab. One rule for every section: past its cap, a section shows its first
#: part, says how much it hides, and keeps the rest one click away, and each count it hides is a
#: field of ``report.json``. The bounds graph draws this many kept records on each side, and as many
#: left out, and lists the rest of the side under one "+k more" node.
GRAPH_KEPT_CAP = 8
GRAPH_LEFT_OUT_CAP = 8
#: A two-factor cross-table with more columns than this falls back to the grouped list.
CROSS_COLUMNS_CAP = 12
#: The grouped list shows this many rows, then "show all".
GROUPED_ROWS_CAP = 20
#: A sample table past this many samples first folds the rows the same on every one of its labels
#: into one row, then pages the rows by this many.
SAMPLE_ROWS_CAP = 50
#: A value or a quote longer than this is cut, with the full text on click.
TEXT_CAP = 80


class Cut(_View):
    """How much of one capped section shows: ``shown`` items first, and ``hidden`` behind a click."""

    shown: int
    hidden: int = 0


class PassageView(_View):
    """One quote of a study file under its number, and where it was read.

    ``source`` is the record and its field, or the paper and its section, joined for a reader.
    ``hidden_chars`` is how much of the quote is past the cap, cut on the page until a click.
    """

    number: int
    source: str
    quote: str
    hidden_chars: int = 0


class BoundsRecord(_View):
    """One record the bounds placed: a GEO Series or a paper, kept or left out, and why.

    ``backing`` is the claim's backing mark. It is ``None`` only for a kept paper, which a bounds
    file lists with no claim of its own. ``reason`` is what an inferred claim says or an unknown
    claim's note, and ``reason_passages`` are the bounds file's passages the claim is quoted from or
    rests on. ``superseries`` is the SuperSeries a sub-Series belongs to. Only the standard set's
    study file says which Series that is, so it is ``None`` for any other folder. ``passages`` are
    the reading's passages quoted from this record, empty when the study has no reading. ``title``
    is the record's title as the bounds file copied it, ``None`` for a file drawn before titles were
    copied, whose records go by their ids alone. ``drawn`` is false for a record past its side's
    cap, which the graph lists under the side's "+k more" node instead of drawing.
    """

    id: str
    kind: Literal["series", "paper"]
    kept: bool
    drawn: bool = True
    title: str | None = None
    backing: Literal["quoted", "inferred", "unknown"] | None = None
    reason: str | None = None
    reason_passages: list[PassageView] = Field(default_factory=list)
    p: float | None = None
    superseries: str | None = None
    passages: list[PassageView] = Field(default_factory=list)


class StudyCell(_View):
    """One label of one sample, as the reading says it and as a sample table shows it.

    ``value`` is what the claim says, without its level: its made-up id, its text, its number or
    its term. It is ``None`` for a gap, which ``backing`` names as unknown or na. ``level`` is the
    factor level the claim names, ``None`` for a claim outside its factor's levels and for every
    label that is no factor. ``note`` is how an inferred claim was worked out, or the gap's note.
    ``passages`` are the reading's passages the claim is quoted from, or rests on when it is not
    quoted, and ``against`` those that speak against it, each by its number in
    :attr:`StudyReport.passages`. ``inherited`` is true for a claim the sample takes from
    ``shared``. ``hidden_chars`` is how much of the value is past the cap, cut on the page.
    """

    value: str | None = None
    hidden_chars: int = 0
    term: list[str] = Field(default_factory=list)
    level: str | None = None
    backing: Literal["quoted", "inferred", "unknown", "na"]
    note: str | None = None
    p: float | None = None
    inherited: bool = False
    passages: list[int] = Field(default_factory=list)
    against: list[int] = Field(default_factory=list)


class StudySample(_View):
    """One sample of the reading: its GEO Sample, the BioSample its record links, its claims."""

    sample: str
    biosample_accession: str | None = None
    #: The path of each workspace whose manifest holds this sample's BioSample accession, as
    #: :attr:`WorkspaceReport.path` gives it; empty when no workspace the report read compiled it.
    compiled_in: list[str] = Field(default_factory=list)
    #: Every label the sample has once the shared claims are applied, by label.
    cells: dict[str, StudyCell] = Field(default_factory=dict)


class StudyFactor(_View):
    """A label the study varied on purpose: its levels in order, its baseline, and its claim's p."""

    label: str
    levels: list[str]
    baseline: str | None = None
    ordered: bool = False
    p: float | None = None


class DesignCount(_View):
    """The samples of one design group under one assay, or under every assay when ``assay`` is None."""

    assay: str | None = None
    samples: list[str] = Field(default_factory=list)


class DesignGroup(_View):
    """One combination of factor levels and its samples.

    ``levels`` is aligned to :attr:`StudyReport.factors`, ``None`` where the samples are at no level
    of that factor. ``counts`` holds one entry per assay that has a sample here, and is empty for a
    cross-table cell no sample is in.
    """

    levels: list[str | None] = Field(default_factory=list)
    counts: list[DesignCount] = Field(default_factory=list)


class DesignTable(_View):
    """The minimum design table: how many samples are at each combination of factor levels.

    ``cross`` for two factors: ``rows`` are the first factor's levels and ``columns`` the second's,
    each with ``None`` after them when a sample is at no level, and ``groups`` holds every pair,
    row by row. ``grouped`` otherwise: ``rows`` and ``columns`` are empty, and ``groups`` holds one
    entry per combination the samples take, in the factors' level order. ``assays`` are the assays
    the counts are split by, empty when the study has one.

    Two factors whose cross-table would be wider than its cap fall back to the grouped list, and
    ``cross_columns`` says how many columns it would have had; it is ``None`` otherwise. ``cut`` is
    how many of a grouped list's rows show before "show all", ``None`` for a cross-table.
    """

    kind: Literal["cross", "grouped"]
    rows: list[str | None] = Field(default_factory=list)
    columns: list[str | None] = Field(default_factory=list)
    groups: list[DesignGroup] = Field(default_factory=list)
    assays: list[str] = Field(default_factory=list)
    cross_columns: int | None = None
    cut: Cut | None = None


class SameForAll(_View):
    """A label whose claim is the same for every sample, folded into one line: the label and it."""

    label: str
    cell: StudyCell


class TopicTable(_View):
    """One sample table: the labels of one topic, those that vary as columns, the rest folded.

    ``labels`` is every label of the topic the reading has, factors aside: a factor is a column of
    every topic table, at the left. ``varying`` are the labels whose claims differ between samples,
    the table's columns in order, and ``same`` the labels folded into one "same for all" line.

    ``rows`` are the table's rows, in the order of :attr:`StudyReport.samples`, and empty when no
    label varies. Each row is one sample, until the study has more samples than the cap: then the
    samples whose row is the same on every factor and every varying label share one row, shown as
    its first sample "× n", and ``folded`` counts the samples a row holds beside its first. ``cut``
    is how many rows the first page shows, and how many are on the pages after it.
    """

    topic: str
    labels: list[str] = Field(default_factory=list)
    varying: list[str] = Field(default_factory=list)
    same: list[SameForAll] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    folded: int = 0
    cut: Cut = Field(default_factory=lambda: Cut(shown=0))


class StudyReport(_View):
    """The study folder the report read: its bounds, its design and what each sample was.

    ``path`` is the folder relative to the report's own directory, as every path here is: ``.``
    when the report was written into the study folder. ``reason`` is the bounds file's one line on
    why these records are one study. ``records`` holds every record the bounds placed, kept and
    left out, in the order the bounds file writes them, and ``graph`` how many of each side's the
    graph draws and how many it lists under the side's "+k more" node, by the records' kind. The
    rest is the reading's, and empty, with ``design`` ``None``, when the study has none: ``samples``
    in the design's order, ``factors`` in the reading's, ``topics`` in the label table's, and
    ``passages`` the reading's, which a cell names by number.
    """

    study: str
    path: str
    reason: str
    records: list[BoundsRecord] = Field(default_factory=list)
    graph: dict[Literal["series", "paper"], Cut] = Field(default_factory=dict)
    has_reading: bool = False
    #: The hash of the folder's reading as it is now, ``None`` when the study has no reading.
    reading_hash: str | None = None
    passages: list[PassageView] = Field(default_factory=list)
    factors: list[StudyFactor] = Field(default_factory=list)
    samples: list[StudySample] = Field(default_factory=list)
    design: DesignTable | None = None
    topics: list[TopicTable] = Field(default_factory=list)


class ProjectReport(_View):
    """Everything the report renders, and exactly what ``report.json`` holds.

    The page is built from this tree *after* it has been through JSON, and it embeds the same JSON,
    so the file a program reads and the page a person reads carry one set of data. The top level is
    a list of workspaces rather than one workspace's fields because a report is not bound to one
    compile: a study read before anything was compiled, and one compiled across several workspaces,
    are the same document with zero or several entries here and ``study`` beside them.

    ``generated_at`` is injectable (default ``None``) so a test can pin it and assert two renders are
    byte-identical; when ``None`` the page simply omits a timestamp.
    """

    report_version: str
    generated_at: str | None = None
    workspaces: list[WorkspaceReport] = Field(default_factory=list)
    #: The study folder ``--study`` named, or ``None`` when the report read none.
    study: StudyReport | None = None
    #: What the report found out of step between the parts it read. ``READING_STALE``: a
    #: workspace joined a reading other than the study folder's current one, so the labels in its
    #: sample table and ``.h5ad`` files are older than the Study tab. Advisory, as the join's own
    #: warnings are: the report renders either way.
    warnings: list[ValidationWarning] = Field(default_factory=list)

    @property
    def assays(self) -> list[AssayReport]:
        """Every assay in the report, workspace by workspace, in the order they were read."""
        return [a for ws in self.workspaces for a in ws.assays]

    @property
    def is_multi_assay(self) -> bool:
        """Whether the report holds more than one assay, and so needs an assay switcher."""
        return len(self.assays) > 1

    @property
    def worst_exit_code(self) -> int:
        """The project verdict: the most severe assay outcome (blocked/question beats clean)."""
        return max((a.conclusion.exit_code for a in self.assays), default=0)


__all__ = [  # noqa: RUF022 - grouped by concept under comments, not alphabetical
    # Re-exported, not defined here. Both are the type of a public field on `AssayReport`, so they
    # are part of this tree's surface whoever defines them — and they are defined beside the code
    # that WRITES the artifacts they describe, because who writes a format owns how to read it. For
    # `Alert` that argument is one step longer and lands in the same place: a rule that produces one
    # is a fact about an aligner, so the shape belongs where the rules are.
    "Alert",
    "PipelineStats",
    "EvidenceRef",
    "SampleView",
    "AssayLabelView",
    "ElementView",
    "ReadView",
    "OnlistView",
    "FileView",
    "ChemistryDecision",
    "MatrixCellView",
    "MatrixRoleRow",
    "MatrixView",
    "RuledOut",
    "DecisionField",
    "PlanView",
    "ArtifactEmbed",
    "PipelineStage",
    "ArchiveProjectView",
    "H5adPointer",
    "ConclusionView",
    "AssayReport",
    "WorkspaceReport",
    "GRAPH_KEPT_CAP",
    "GRAPH_LEFT_OUT_CAP",
    "CROSS_COLUMNS_CAP",
    "GROUPED_ROWS_CAP",
    "SAMPLE_ROWS_CAP",
    "TEXT_CAP",
    "Cut",
    "PassageView",
    "BoundsRecord",
    "StudyCell",
    "StudySample",
    "StudyFactor",
    "DesignCount",
    "DesignGroup",
    "DesignTable",
    "SameForAll",
    "TopicTable",
    "StudyReport",
    "ProjectReport",
]
