"""The report's display model — a frozen projection of on-disk artifacts, not the artifacts.

``collect.py`` reads a workspace and builds a :class:`ProjectReport`; ``render.py`` turns it into one
HTML page. Keeping a typed tree between them means the renderer never reaches into a raw manifest or a
loose dict, the projection is validated once, and the same object serialises to the stdout summary.

Everything here is a *view*: it flattens the manifest's ``Evidenced`` envelopes to
``(value, basis, confidence, rung, evidence)`` and resolves each evidence token to something a human
can read (a quote, an accession, a policy rule). It is deliberately modality-general — read roles,
quantification, and the composed config are carried as generic shapes, never STARsolo-typed fields —
so a future non-STAR pipeline renders through the same tree unchanged.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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


class AttributeView(_View):
    """One sample attribute cell: the value, how we know it, and where it came from.

    ``withheld`` marks the null-over-wrong case — an attribute a joined record or assertion mentions
    that the resolver deliberately left out of the manifest because equal authorities disagreed. It is
    a real answer (a value would be a lie), so it is shown as a distinct row, not an empty one.
    """

    key: str
    value: str
    basis: str
    confidence: float | None = None
    rung: int
    evidence: list[EvidenceRef] = Field(default_factory=list)
    withheld: bool = False


class SampleView(_View):
    """One biological sample and everything declared about it."""

    sample_id: str
    accession: str | None = None
    n_files: int = 0
    file_names: list[str] = Field(default_factory=list)
    attributes: list[AttributeView] = Field(default_factory=list)


class AssayLabelView(_View):
    """A chemistry spelled three ways — our key, EFO's CURIE, EFO's words."""

    chemistry: str
    curie: str
    name: str


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
    The ``*_rel`` paths are workspace-relative links to the deliverables, or ``None`` when this assay
    reached the IR but was never composed.
    """

    fields: list[DecisionField] = Field(default_factory=list)
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


class StudyView(_View):
    """The study as the archive declared it, plus its abstract when a record carried one."""

    accession: str | None = None
    title: str | None = None
    center: str | None = None
    data_type: str | None = None
    released: str | None = None
    abstract: str | None = None


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
    study: StudyView | None = None
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
    provenance: list[tuple[str, str]] = Field(default_factory=list)
    #: True iff archive records were joined (sample facts have a declared source).
    has_records: bool = False
    #: True iff a paper/instruction document was read and quoted (harvest ran with real prose).
    has_prose: bool = False

    @property
    def n_samples(self) -> int:
        return len(self.samples)

    @property
    def n_files(self) -> int:
        return len(self.files)

    @property
    def label(self) -> str:
        """A human title for this assay's section — its subdir, or its winning chemistry."""
        return self.subdir or (self.chemistry.value[0] if self.chemistry.value else "assay")

    @property
    def attribute_columns(self) -> list[str]:
        """The union of sample-attribute keys, common ones first — the columns of the sample table."""
        preferred = (
            "strain", "genotype", "age", "dev_stage", "sex", "tissue", "cell_type",
            "cell_line", "treatment", "source_name", "disease",
        )  # fmt: skip
        seen: set[str] = set()
        for s in self.samples:
            seen.update(a.key for a in s.attributes)
        ordered = [k for k in preferred if k in seen]
        ordered += sorted(seen - set(preferred))
        return ordered


class ProjectReport(_View):
    """Everything the report renders: one or more assays under a single workspace.

    ``generated_at`` is injectable (default ``None``) so a test can pin it and assert two renders are
    byte-identical; when ``None`` the page simply omits a timestamp.
    """

    workspace_name: str
    report_version: str
    generated_at: str | None = None
    assays: list[AssayReport] = Field(default_factory=list)

    @property
    def is_multi_assay(self) -> bool:
        return len(self.assays) > 1

    @property
    def worst_exit_code(self) -> int:
        """The project verdict: the most severe assay outcome (blocked/question beats clean)."""
        return max((a.conclusion.exit_code for a in self.assays), default=0)


__all__ = [
    "EvidenceRef",
    "AttributeView",
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
    "StudyView",
    "ConclusionView",
    "AssayReport",
    "ProjectReport",
]
