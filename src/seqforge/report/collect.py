"""Read a ``seqforge/`` workspace and project it into a :class:`ProjectReport`.

This is where all the graceful degradation lives. The manifest is the one required artifact (the
chemistry decision it carries is what makes the page always render); everything else — the harvested
assertions behind a sample quote, the archive records behind a study abstract, the persisted evidence
matrix, the composed pipeline, and the per-sample QC artifacts a *finished* pipeline left behind — is
joined in if present and simply omitted if not. Nothing here decides anything: it reads what the
deterministic verbs already wrote (and, for the results, what the user's own snakemake wrote) and
flattens it for a human.

The one non-obvious join is the evidence matrix. It is persisted per **run** under a cache key that
folds in tool versions the manifest never stores, so the report never recomputes that key — it scans
``cache/candidates`` for a run whose winning chemistry matches the manifest and whose assigned files
are a subset of the manifest's, then reads the sibling ``cache/matrices`` sidecar. Version-drift-proof
and correct across a multi-run dataset.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from ..models.dataset import DatasetManifest, LibrarySection
from ..models.processing import ProcessingManifest
from ..pipeline import CONFIG_NAME, SNAKEFILE_NAME, UNITS_TSV_NAME, CompiledPipeline
from ..project import discover_assays
from ..workflows.metrics import Alert, Decision, DecisionRef, PipelineStats, gather_alerts
from ..workspace import cache_dir, documents_dir, logs_dir, records_dir, state_dir
from .model import (
    ArtifactEmbed,
    AssayLabelView,
    AssayReport,
    AttributeView,
    ChemistryDecision,
    ConclusionView,
    DecisionField,
    ElementView,
    EvidenceRef,
    FileView,
    MatrixCellView,
    MatrixRoleRow,
    MatrixView,
    OnlistView,
    PipelineStage,
    PlanView,
    ProjectReport,
    ReadView,
    RuledOut,
    SampleView,
    StudyView,
)

#: A handful of common lab organisms, so the Overview can say "C. elegans" not just "taxid 6239".
#: Deliberately tiny and unauthoritative — an unknown taxid degrades to "taxid N", never a wrong name.
_ORGANISM_NAMES: dict[int, str] = {
    6239: "C. elegans",
    7227: "D. melanogaster",
    7955: "D. rerio (zebrafish)",
    9606: "H. sapiens (human)",
    10090: "M. musculus (mouse)",
    10116: "R. norvegicus (rat)",
    3702: "A. thaliana",
    559292: "S. cerevisiae",
    4932: "S. cerevisiae",
    284812: "S. pombe",
    83333: "E. coli K-12",
}

_HEX12 = re.compile(r"-([0-9a-f]{12})$")
_ACCESSION = re.compile(r"^([SED]R[RXPS]\d+|GS[EM]\d+|PRJ[A-Z]{2}\d+|SAM[NED][A-Z]?\d+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: A larger workspace can score dozens of feasible technologies; the Evidence tab shows the winner and
#: a few real contenders, not the whole KB. The full detail lives in the manifest and the sidecar.
_MAX_MATRIX_TECHS = 6


def collect_report(
    workspace: str | Path,
    *,
    generated_at: str | None = None,
    results_dir: Path | None = None,
) -> ProjectReport:
    """Project a workspace into a :class:`ProjectReport` (one :class:`AssayReport` per assay).

    ``generated_at`` is threaded through verbatim (a caller may pin it for byte-deterministic output).
    ``results_dir`` says where a finished pipeline's per-sample outputs are; ``None`` derives it from
    the composed config's ``outdir``, which is what a pipeline started in its own directory used. Both
    are caller-supplied *machine* facts, and neither can change what the page says the compiler
    decided.
    Raises :class:`FileNotFoundError` only when there is genuinely nothing to report — no manifest and
    no draft anywhere under ``seqforge/``.
    """
    from . import REPORT_VERSION

    ws = Path(workspace)
    assays_on_disk = discover_assays(ws)
    if not assays_on_disk:
        assay = _collect_draft(ws)
        assays = [assay] if assay is not None else []
        if not assays:
            raise FileNotFoundError(
                f"no manifest (or draft) under {state_dir(ws)} — nothing to report yet. Run "
                f"`seqforge run` (or at least `manifest fill`) first."
            )
    else:
        assays = [
            _collect_assay(ws, subdir, mpath, results_dir) for subdir, mpath in assays_on_disk
        ]

    return ProjectReport(
        workspace_name=_workspace_name(ws),
        report_version=REPORT_VERSION,
        generated_at=generated_at,
        assays=assays,
    )


def _workspace_name(ws: Path) -> str:
    resolved = ws.resolve()
    # The dataset dir is usually the parent of `seqforge/`; when the workspace IS `seqforge/`, use it.
    name = resolved.name
    return name or "workspace"


# ---- one assay ----------------------------------------------------------------------------------


def _collect_assay(
    ws: Path, subdir: str | None, manifest_path: Path, results_dir: Path | None = None
) -> AssayReport:
    manifest = DatasetManifest.model_validate(yaml.safe_load(manifest_path.read_text()))
    base = manifest_path.parent

    assertions = _load_assertions(ws)
    doc_index = _index_documents(ws)
    records = _load_records(ws, manifest.experiment.study)

    proc_path = base / "processing.yaml"
    proc = (
        ProcessingManifest.model_validate(yaml.safe_load(proc_path.read_text()))
        if proc_path.is_file()
        else None
    )
    pipeline = CompiledPipeline.discover(ws, subdir=subdir)
    plan = _plan(ws, proc, pipeline, assertions, doc_index) if proc is not None else None
    conclusion = _conclusion(has_manifest=True, snakefile=pipeline is not None)

    samples = _samples(manifest, assertions, doc_index)
    stats = _pipeline_stats(pipeline, manifest, results_dir)
    matrices, ruled_out = _matrices(ws, manifest)
    has_prose = any(
        ref.kind == "assertion" and ref.quote
        for s in samples
        for a in s.attributes
        for ref in a.evidence
    )

    taxid = int(manifest.experiment.organism.value)
    return AssayReport(
        subdir=subdir,
        accessions=[str(a) for a in manifest.experiment.accessions.value],
        organism_taxid=taxid,
        organism_name=_ORGANISM_NAMES.get(taxid),
        organism_basis=manifest.experiment.organism.basis,
        study=_study(manifest, records),
        chemistry=_chemistry(manifest.library),
        reads=_reads(manifest.library),
        onlists=_onlists(manifest.library),
        files=_files(manifest.library),
        samples=samples,
        plan=plan,
        matrices=matrices,
        ruled_out=ruled_out,
        artifacts=_artifacts(base, pipeline),
        pipeline_stages=_pipeline_stages(plan),
        conclusion=conclusion,
        pipeline_stats=stats,
        alerts=_alerts(stats, manifest, proc, plan),
        provenance=[
            ("dataset_hash", manifest.provenance.dataset_hash),
            ("kb_version", manifest.provenance.kb_version),
            ("seqforge_version", manifest.provenance.seqforge_version),
        ],
        has_records=bool(records.get("records")) if isinstance(records, dict) else False,
        has_prose=has_prose,
    )


def _chemistry(library: LibrarySection) -> ChemistryDecision:
    ev = library.chemistry
    return ChemistryDecision(
        value=list(ev.value),
        assay_labels=[
            AssayLabelView(chemistry=a.chemistry, curie=a.curie, name=a.name) for a in library.assay
        ],
        basis=ev.basis,
        confidence=ev.confidence,
        rung=ev.rung,
        modality=library.read_layout.modality,
        n_files=len(library.files),
        evidence_shas=list(ev.evidence),
    )


def _reads(library: LibrarySection) -> list[ReadView]:
    out: list[ReadView] = []
    for read in library.read_layout.reads:
        out.append(
            ReadView(
                read_id=read.read_id,
                strand=read.strand,
                min_len=read.min_len,
                max_len=read.max_len,
                elements=[
                    ElementView(
                        role=el.role,
                        region_type=el.region_type,
                        start=el.start,
                        length=el.length,
                        onlist_ref=el.onlist_ref,
                        anchored=el.anchor is not None,
                    )
                    for el in read.elements
                ],
            )
        )
    return out


def _onlists(library: LibrarySection) -> list[OnlistView]:
    return [
        OnlistView(name=o.name, length=o.length, n_entries=o.n_entries) for o in library.onlists
    ]


def _files(library: LibrarySection) -> list[FileView]:
    return [
        FileView(
            basename=f.basename,
            read_id=f.read_id,
            sha256=f.sha256,
            size_bytes=f.size_bytes,
            uri=f.uri,
        )
        for f in sorted(library.files, key=lambda f: f.basename)
    ]


def _samples(
    manifest: DatasetManifest,
    assertions: dict[str, dict[str, Any]],
    doc_index: dict[str, str],
) -> list[SampleView]:
    out: list[SampleView] = []
    for sample in manifest.experiment.samples:
        attrs: list[AttributeView] = []
        for key in sorted(sample.attributes):
            ev = sample.attributes[key]
            attrs.append(
                AttributeView(
                    key=key,
                    value=str(ev.value),
                    basis=ev.basis,
                    confidence=ev.confidence,
                    rung=ev.rung,
                    evidence=[_resolve_evidence(t, assertions, doc_index) for t in ev.evidence],
                )
            )
        out.append(
            SampleView(
                sample_id=sample.sample_id,
                accession=sample.accession,
                n_files=len(sample.file_uris),
                file_names=sorted(Path(u).name for u in sample.file_uris),
                attributes=attrs,
            )
        )
    return out


# ---- evidence join ------------------------------------------------------------------------------


def _resolve_evidence(
    token: str, assertions: dict[str, dict[str, Any]], doc_index: dict[str, str]
) -> EvidenceRef:
    """Dispatch one evidence token on its shape to something a human can follow.

    ``assert-…`` -> its harvested quote/page/document; a bare accession -> a record link; ``policy:`` /
    ``cli:`` -> who decided; a 64-hex sha -> bytes. An unrecognised token degrades to itself.
    """
    if token.startswith("assert-"):
        a = assertions.get(token)
        if a is not None:
            span = a.get("span", {}) if isinstance(a.get("span"), dict) else {}
            doc_sha = str(span.get("doc_sha256", ""))
            document = doc_index.get(doc_sha[:12]) if doc_sha else None
            page = span.get("page")
            return EvidenceRef(
                raw=token,
                kind="assertion",
                quote=span.get("quote"),
                page=int(page) if isinstance(page, int) else None,
                document=document,
            )
        return EvidenceRef(raw=token, kind="assertion")
    if token.startswith("policy:"):
        return EvidenceRef(raw=token, kind="policy")
    if token.startswith("cli:"):
        return EvidenceRef(raw=token, kind="cli")
    if _ACCESSION.match(token):
        return EvidenceRef(raw=token, kind="accession", accession=token)
    if _SHA256.match(token):
        return EvidenceRef(raw=token, kind="file_sha")
    return EvidenceRef(raw=token, kind="other")


def _load_assertions(ws: Path) -> dict[str, dict[str, Any]]:
    """``{assert-id -> assertion dict}`` from ``logs/assertions.json`` (top-level name as fallback)."""
    for path in (logs_dir(ws) / "assertions.json", state_dir(ws) / "assertions.json"):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        items = data.get("assertions") if isinstance(data, dict) else data
        if not isinstance(items, list):
            continue
        return {a["id"]: a for a in items if isinstance(a, dict) and "id" in a}
    return {}


def _index_documents(ws: Path) -> dict[str, str]:
    """``{doc_sha256[:12] -> readable stem}`` over the rendered documents (both layouts)."""
    index: dict[str, str] = {}
    for d in (documents_dir(ws), state_dir(ws) / "documents"):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.txt")):  # sorted: a stable label under any filesystem order
            m = _HEX12.search(f.stem)
            if m:
                # Strip the trailing -<hash12> for a readable label ("experiment-SRX24283130").
                index.setdefault(m.group(1), f.stem[: m.start()])
    return index


def _load_records(ws: Path, study: Any) -> dict[str, Any]:
    """The archive record set for the study, if one was fetched (for the study abstract + samples).

    The file is preferentially the one named for the manifest's study accession, but that accession may
    be a different *form* of the same study than the file is keyed by — the manifest often resolves a
    GEO id (``GSE234962``) to its BioProject (``PRJNA983807``) while the record set was saved under the
    id the user passed. So when the exact name misses, fall back to any ``records/*.json`` that carries
    a record set; there is one per dataset, so this is unambiguous for the common single-dataset case.
    """
    rdir = records_dir(ws)
    if not rdir.is_dir():
        return {}
    accession = getattr(study, "accession", None) if study is not None else None
    candidates: list[Path] = []
    if accession:
        preferred = rdir / f"{accession}.json"
        if preferred.is_file():
            candidates.append(preferred)
    candidates += [p for p in sorted(rdir.glob("*.json")) if p not in candidates]
    for path in candidates:
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if isinstance(data, dict) and data.get("records"):
            return data
    return {}


def _study(manifest: DatasetManifest, records: dict[str, Any]) -> StudyView | None:
    study = manifest.experiment.study
    if study is None:
        return None
    return StudyView(
        accession=study.accession,
        title=study.title,
        center=study.center,
        data_type=study.data_type,
        released=study.released,
        abstract=_abstract(records),
    )


def _abstract(records: dict[str, Any]) -> str | None:
    for rec in records.get("records", []) if isinstance(records, dict) else []:
        if not isinstance(rec, dict) or rec.get("level") != "project":
            continue
        for ft in rec.get("free_text", []):
            if isinstance(ft, dict) and ft.get("label") == "study_abstract" and ft.get("text"):
                return str(ft["text"])
    return None


# ---- plan / pipeline ----------------------------------------------------------------------------


#: Don't inline an artifact bigger than this — a runaway units.tsv shouldn't bloat the page. The
#: composed text artifacts are all a few KB; anything past this is summarized, not embedded.
_MAX_EMBED_BYTES = 256 * 1024


def _artifacts(base: Path, pipeline: CompiledPipeline | None) -> list[ArtifactEmbed]:
    """The workspace's text artifacts, carried *into* the page so relative links can't break.

    Read verbatim and embedded (the panel offers a ``data:`` URI download + an inline view). Skips a
    file over :data:`_MAX_EMBED_BYTES` so the page stays small. Each compiled artifact is *labelled*
    with the same name it is *read* under, so the page can never offer a download named for a file
    the composer stopped writing.
    """
    specs: list[tuple[str, Path, str]] = [
        ("manifest.yaml", base / "manifest.yaml", "text/yaml"),
        ("processing.yaml", base / "processing.yaml", "text/yaml"),
    ]
    if pipeline is not None:
        specs += [
            (SNAKEFILE_NAME, pipeline.snakefile, "text/plain"),
            (CONFIG_NAME, pipeline.config_path, "text/yaml"),
            (UNITS_TSV_NAME, pipeline.units_path, "text/tab-separated-values"),
        ]
    out: list[ArtifactEmbed] = []
    for name, path, mime in specs:
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        size = len(text.encode())
        if size > _MAX_EMBED_BYTES:
            continue
        out.append(ArtifactEmbed(name=name, mime=mime, text=text, size_bytes=size))
    return out


def _pipeline_stages(plan: PlanView | None) -> list[PipelineStage]:
    """A small, human-readable "what will run, in order" — derived from the recipe, not the Snakefile.

    Branches on the recipe's **typed** counting family (``solo`` / ``atac`` / bulk) so it stays
    modality-general, and reads as plain English for a biologist rather than a rule graph.

    It used to branch on ``quantification``'s rendered caption — ``value.startswith("solo")`` over a
    string :func:`_plan` had produced twenty lines above — which made a display decision load-bearing
    for a correctness one. Rewording that caption would have reverted an ATAC dataset to "align with
    STAR, count reads per gene" with nothing failing. :func:`_plan` already branches on ``quant.kind``
    correctly, so this reads the same axis, carried on the view rather than re-derived, which keeps
    the signature a plan view in and stages out.
    """
    if plan is None:
        return []
    if plan.quantification_kind == "solo":
        return [
            PipelineStage(
                key="onlist",
                title="Prepare the barcode whitelist",
                detail="lay out the list of valid cell barcodes this chemistry uses",
            ),
            PipelineStage(
                key="align",
                title="Align & count per cell (STARsolo)",
                detail="map each read to the genome and tally counts per cell and per gene",
            ),
            PipelineStage(
                key="package",
                title="Package the count matrices",
                detail="write the results as an .h5ad file, ready to open in Scanpy/Seurat",
            ),
        ]
    if plan.quantification_kind == "atac":
        # scATAC via chromap: the deliverable is a fragments file, not a count matrix, so an ATAC
        # workspace must not render as "STAR / count reads per gene".
        return [
            PipelineStage(
                key="onlist",
                title="Prepare the barcode whitelist",
                detail="lay out the list of valid cell barcodes this chemistry uses",
            ),
            PipelineStage(
                key="align",
                title="Align & call fragments (chromap)",
                detail="map paired genomic reads and record one fragment per Tn5 insertion, per cell",
            ),
            PipelineStage(
                key="package",
                title="Index the fragments file",
                detail="sort, bgzip and tabix-index fragments.tsv.gz for ArchR / SnapATAC2 / Signac",
            ),
        ]
    return [
        PipelineStage(
            key="align",
            title="Align to the genome (STAR)",
            detail="map every read to its place in the reference genome",
        ),
        PipelineStage(
            key="count",
            title="Count reads per gene",
            detail="tally how many reads land in each gene (all strand orientations)",
        ),
    ]


def _plan(
    ws: Path,
    proc: ProcessingManifest,
    pipeline: CompiledPipeline | None,
    assertions: dict[str, dict[str, Any]],
    doc_index: dict[str, str],
) -> PlanView:
    p = proc.processing
    genome = p.genome.value
    genome_str = f"{genome.assembly} / {genome.annotation_name}"
    if genome.ncbi_taxid:
        genome_str += f" (taxid {genome.ncbi_taxid})"
    quant = p.quantification.value
    if quant.kind == "solo":
        quant_str = "solo: " + ", ".join(quant.features)
    elif quant.kind == "atac":
        # ATAC has no count matrix — the deliverable is a fragments file, so there is no `mode` or
        # feature list to render. Without this branch `quant.mode` AttributeErrors and the whole report
        # crashes on a chromap workspace. The full fragments-aware rendering is PR-E; this is the
        # defensive stub that keeps `seqforge report` from dying on an ATAC manifest.
        quant_str = "atac: fragments (fragments.tsv.gz)"
    else:
        quant_str = f"bulk: {quant.mode}"

    def field(label: str, ev: Any, value: str) -> DecisionField:
        return DecisionField(
            label=label,
            value=value,
            basis=ev.basis,
            confidence=ev.confidence,
            rung=ev.rung,
            evidence=[_resolve_evidence(t, assertions, doc_index) for t in ev.evidence],
        )

    fields = [
        field("genome", p.genome, genome_str),
        field("aligner", p.aligner, str(p.aligner.value)),
        field("quantification", p.quantification, quant_str),
        field("environment", p.environment, str(p.environment.value)),
        field("variant calling", p.variant_calling, "yes" if p.variant_calling.value else "no"),
    ]
    resources = [
        ("threads", str(p.resources.threads)),
        ("mem_gb", str(p.resources.mem_gb)),
        ("gpus", str(p.resources.gpus)),
        ("disk_gb", "auto" if p.resources.disk_gb is None else str(p.resources.disk_gb)),
    ]

    config_kv: list[tuple[str, str]] = []
    primary_feature: str | None = None
    snakefile_rel = config_rel = units_rel = None
    pipeline_name: str | None = None
    if pipeline is not None:
        pipeline_name = pipeline.directory.name
        # An absent or unreadable config reads as an empty one, so the "never composed" and "composed
        # but the config will not parse" degradations are one branch here and are decided once, by
        # the module that owns the file, rather than by a guard per reader.
        config = pipeline.config
        primary_feature = _as_str_or_none(config.get("primary_feature"))
        config_kv = _flatten(config)
        snakefile_rel = _rel(ws, pipeline.snakefile)
        config_rel = _rel(ws, pipeline.config_path)
        units_rel = _rel(ws, pipeline.units_path)

    return PlanView(
        fields=fields,
        quantification_kind=quant.kind,
        resources=resources,
        primary_feature=primary_feature,
        config=config_kv,
        pipeline_name=pipeline_name,
        snakefile_rel=snakefile_rel,
        config_rel=config_rel,
        units_rel=units_rel,
    )


def _as_str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _rel(ws: Path, path: Path) -> str | None:
    """``path`` relative to ``seqforge/`` (where report.html lives), or ``None`` if it is not there."""
    if not path.exists():
        return None
    try:
        return str(path.resolve().relative_to(state_dir(ws).resolve()))
    except ValueError:
        return None


def _flatten(obj: Any, prefix: str = "") -> list[tuple[str, str]]:
    """A nested config dict -> a flat, sorted ``[(dotted.key, value)]`` for an opaque k/v table.

    Modality-general on purpose: the report types none of STARsolo's fields, it shows whatever the
    composer emitted. Scalars render as strings; a list of scalars joins with commas; anything deeper
    falls back to its JSON so nothing is silently dropped.
    """
    rows: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key in sorted(obj, key=str):
            rows.extend(_flatten(obj[key], f"{prefix}{key}."))
    elif isinstance(obj, list) and all(not isinstance(x, (dict, list)) for x in obj):
        rows.append((prefix.rstrip("."), ", ".join(str(x) for x in obj)))
    elif isinstance(obj, (dict, list)):
        rows.append((prefix.rstrip("."), json.dumps(obj)))
    else:
        rows.append((prefix.rstrip("."), str(obj)))
    return rows


# ---- the finished pipeline ----------------------------------------------------------------------


def _pipeline_stats(
    pipeline: CompiledPipeline | None,
    manifest: DatasetManifest,
    results_dir: Path | None,
) -> PipelineStats | None:
    """The finished pipeline's per-sample metrics, or ``None`` when there is nothing on disk to read.

    Three facts, and all three are :class:`~seqforge.pipeline.CompiledPipeline`'s to answer rather
    than this module's to re-derive: WHICH module ran, WHERE its outputs went, and WHICH samples it
    was contracted to produce. That last one comes from the config the pipeline itself consumed and
    never from a listing of the results tree, which is what makes a *partial* pipeline legible — a
    listing can only say what finished, never what is missing.

    ``results_dir`` overrides the second: a pipeline run with ``snakemake --directory`` put its
    outputs somewhere this workspace cannot know, and that is a machine fact, so it arrives as a flag
    rather than as a search. It is joined onto the pipeline directory exactly as ``outdir`` is, which
    leaves an absolute override untouched.

    The manifest's sample ids are the fallback for a config written before ``samples`` existed. They
    agree by construction (the composer derives one from the other), so this is a compatibility path
    and not a second opinion — and it lives here rather than on the owner because the owner
    deliberately does not know what a manifest is.

    Every step degrades to ``None``: an uncomposed workspace, a module with no adapter, a pipeline
    that has not started. All of them are the same fact for a reader — there is no results section —
    and none is a reason to fail a report of what the compiler decided.
    """
    if pipeline is None:
        return None
    module = pipeline.module
    if module is None:
        return None
    samples = pipeline.samples or [s.sample_id for s in manifest.experiment.samples]
    where = pipeline.directory / results_dir if results_dir is not None else pipeline.results_dir
    # Imported here and not at module scope: `stats` reaches its adapter through `qc` -> `h5ad`,
    # which imports scipy — ~0.3 s measured — and `cli/__init__` imports this module to register the
    # verb, so at module scope every `seqforge` invocation would pay it to register `report`.
    from ..workflows.stats import read_pipeline_stats

    try:
        return read_pipeline_stats(module, where, samples)
    except OSError:
        return None


# ---- attribution: which decision does a bad number implicate? ------------------------------------
#
# The rules are pure over one sample's metrics, so a `Finding` can name a decision and can never say
# what that decision is currently SET to. This is where the other half is in hand — the manifest, the
# recipe and the composed config are all already open here — so this is where the join happens, and
# it is the only place in the system that knows both what a `Decision` means and what a manifest is.
#
# Nothing here writes. Every function below reads an already-validated artifact and returns a display
# string; there is no path, no `open(..., "w")` and no exit code in this section, which is what makes
# "an alert never rewrites the manifest or the recipe" a property of the code rather than a promise.


class _DecisionContext(NamedTuple):
    """Everything a resolver may read: the dataset, the recipe, and the composed view of it.

    One argument rather than three positional ones so that adding a resolver — the whole shape of the
    next two tickets — is a function and a dict entry, never a signature change rippling through
    every sibling. ``proc`` and ``plan`` are optional because an assay can reach the IR and never be
    composed, and a resolver that cannot answer returns ``None`` rather than inventing a value.
    """

    manifest: DatasetManifest
    proc: ProcessingManifest | None
    plan: PlanView | None


#: How many file→role pairs a ``read_roles`` value spells out before it summarises. A dataset of two
#: FASTQs wants the mapping written out — it is the answer — and one of two hundred wants a count: a
#: 200-item string in a one-line field is not a value a reader reads, it is a value they scroll past.
_MAX_ROLE_FILES = 4


def _resolve_chemistry(ctx: _DecisionContext) -> DecisionRef | None:
    """The chemistry call, as the Overview badge already shows it — the winner plus its equivalents.

    Named in the manifest's own vocabulary (``library.chemistry``) because the next thing a reader
    does with an alert is open that file and look for that field, and a label that paraphrased it
    would leave them searching.
    """
    values = list(ctx.manifest.library.chemistry.value)
    if not values:
        return None
    equivalents = f" (+{len(values) - 1} equivalent)" if len(values) > 1 else ""
    return DecisionRef(
        decision="chemistry",
        label="chemistry (manifest `library.chemistry`)",
        value=f"{values[0]}{equivalents}",
    )


def _resolve_read_roles(ctx: _DecisionContext) -> DecisionRef | None:
    """Which file was handed over as which read — the other half of the same joint optimization.

    ``read_id`` and ``chemistry`` come out of one decision (``Candidate`` is
    ``(technology, score, role_assignment)`` and the score scores the pair), which is exactly why an
    alert about a whitelist that matches nothing implicates both: the metric cannot tell a wrong list
    from the right list read against the wrong file.

    Sorted by basename, like the files table, so two renders of one workspace agree; a file with no
    role is left out, because "unassigned" is a state ``validate`` already surfaces and repeating it
    inside an alert about barcodes would point at the wrong artifact.
    """
    assigned = [
        (f.basename, f.read_id)
        for f in sorted(ctx.manifest.library.files, key=lambda f: f.basename)
    ]
    pairs = [(name, role) for name, role in assigned if role]
    if not pairs:
        return None
    shown = ", ".join(f"{role} = {name}" for name, role in pairs[:_MAX_ROLE_FILES])
    if len(pairs) > _MAX_ROLE_FILES:
        shown += f", and {len(pairs) - _MAX_ROLE_FILES} more"
    return DecisionRef(
        decision="read_roles",
        label="read roles (manifest `library.files[].read_id`)",
        value=shown,
    )


def _resolve_solo_features(ctx: _DecisionContext) -> DecisionRef | None:
    """Which features the recipe counts, in the order that decides which matrix is THE matrix.

    Order is the whole content of this decision. ``SoloQuant`` is an ordered list with no aligner-side
    referent — STARsolo writes one ``Solo.out/<Feature>/`` per entry whatever the order — so what the
    list buys is a deterministic answer to "which matrix does everything downstream read", and
    ``compose`` projects it out to the config's ``primary_feature``. An alert saying "you are counting
    the wrong feature" is therefore only actionable once the reader can see which one is first, so the
    value spells the list out and names element 0 rather than summarising it.

    Read from the recipe and not from the composed config, because the recipe is the artifact the
    reader edits; ``primary_feature`` is what that edit produces. A recipe that counts with anything
    other than STARsolo has no such list, and resolves to ``None`` rather than to a paraphrase of a
    different field — a decision the workspace cannot answer for is dropped, never rendered empty.

    No ``change_to``: the alternative is a reorder, and where several ``GeneFull*`` variants were
    counted, which one to promote is a choice between them rather than the single swap that field is
    for. The remedy sentence carries it, and a wrong concrete suggestion is worse than none.
    """
    if ctx.proc is None:
        return None
    quant = ctx.proc.processing.quantification.value
    if quant.kind != "solo" or not quant.features:
        return None
    return DecisionRef(
        decision="solo_features",
        label="counted features (recipe `processing.quantification.features`)",
        value=f"{', '.join(quant.features)} — {quant.features[0]} is the primary matrix",
    )


def _resolve_annotation(ctx: _DecisionContext) -> DecisionRef | None:
    """The registered gene model reads were counted against — a **recipe** field, and named as one.

    Rendered ``assembly / annotation_name``, the way ``_plan`` already builds ``genome_str``, so the
    string in the alert is the string the Pipeline tab shows and a reader is not left matching two
    spellings of one decision.

    ``annotation_name`` is ``None`` for a pipeline whose index carries no gene model, and that
    resolves to ``None`` rather than to ``sacCer3 / None``: there is no annotation to point at, and a
    decision the workspace cannot answer for is dropped. The rule that names this decision cannot
    fire on such a pipeline anyway — with no GTF there are no gene rows and no ``reads_in_genes`` —
    so this is the same fact arriving from the other side, not a second guard.
    """
    if ctx.proc is None:
        return None
    genome = ctx.proc.processing.genome.value
    if not genome.annotation_name:
        return None
    return DecisionRef(
        decision="annotation",
        label="annotation (recipe `processing.genome`)",
        value=f"{genome.assembly} / {genome.annotation_name}",
    )


#: The composed config's strand key, matched on its LAST dotted segment. Which block carries the KB's
#: backend params is the **module's** answer (``compose.params.param_block_key`` reads it off
#: :attr:`WorkflowModule.param_block`), so hard-coding ``solo.soloStrand`` here would make this file a
#: second owner of that decision and would go quietly mute the day a module declared another block.
_STRAND_PARAM = "soloStrand"

#: The two ``--soloStrand`` values that are each other's ONLY alternative, so ``change_to`` can be
#: filled. STARsolo also takes ``Unstranded``, and from there "the alternative" is two values rather
#: than one — that case keeps its value and offers no flip, because a wrong concrete suggestion is
#: worse than none (the rule for ``change_to`` that :class:`DecisionRef` already states).
_STRAND_FLIP: dict[str, str] = {"Forward": "Reverse", "Reverse": "Forward"}


def _resolve_strand(ctx: _DecisionContext) -> DecisionRef | None:
    """Which strand the counter was told the cDNA read sits on — read out of the **composed config**.

    The one decision here that is in neither manifest. ``soloStrand`` is a KB **backend param**: the
    parse half, byte-decided, owned by the chemistry spec and never instructable — ADR 0011 is the
    record — and ``compose`` emits it into ``config.yaml``. So it is read from
    :attr:`PlanView.config`, which is that config verbatim, and the label says so: a reader told to
    edit their recipe's ``soloStrand`` would open ``processing.yaml`` and find nothing, and an alert
    that sends its reader to the wrong file is worse than one that names no file at all.

    A workspace that was never composed, or one whose module has no strand param at all (bulk), has
    nothing to answer with and resolves to ``None``; ``gather_alerts`` then drops the row rather than
    drawing a field name with no value beside it.
    """
    if ctx.plan is None:
        return None
    value = next(
        (v for k, v in ctx.plan.config if k.rsplit(".", 1)[-1] == _STRAND_PARAM and v),
        None,
    )
    if value is None:
        return None
    return DecisionRef(
        decision="strand",
        label="strand (composed config `soloStrand`; a KB backend param, not a recipe field)",
        value=value,
        change_to=_STRAND_FLIP.get(value),
    )


#: Every :data:`~seqforge.workflows.metrics.Decision` a rule can name, and how to read what the
#: workspace currently says it is. Total over the literal, and an exhaustiveness test derived from
#: ``get_args`` holds it that way: a member added without teaching this table to read its value would
#: render as a field name with nothing beside it, which reads as a value of nothing.
#:
#: A dict and not a chain of ``if``s for the reason the stats registry is one: the next two rules add
#: an entry here and a function above, and neither touches the grouping, the ordering or the page.
_DECISION_RESOLVERS: dict[Decision, Callable[[_DecisionContext], DecisionRef | None]] = {
    "chemistry": _resolve_chemistry,
    "read_roles": _resolve_read_roles,
    "annotation": _resolve_annotation,
    "strand": _resolve_strand,
    "solo_features": _resolve_solo_features,
}


def _alerts(
    stats: PipelineStats | None,
    manifest: DatasetManifest,
    proc: ProcessingManifest | None,
    plan: PlanView | None,
) -> list[Alert]:
    """The module's findings, grouped and attributed. Empty is the healthy answer and the common one.

    ``n_found`` and never ``n_expected``: a rule that fired on both of the two samples that finished
    has fired on every sample there is to fire on, and a partial run must produce alerts rather than
    wait for a full plate.

    A resolver that raises would take down a page whose whole contract is to degrade, so an
    unresolvable decision is dropped by :func:`~seqforge.workflows.metrics.gather_alerts` and a
    missing one is dropped here. Neither is a silent failure the reader can act on wrongly: what is
    dropped is a row that would have carried a field name and no value.
    """
    if stats is None or not stats.findings:
        return []
    ctx = _DecisionContext(manifest=manifest, proc=proc, plan=plan)

    def resolve(decision: Decision) -> DecisionRef | None:
        resolver = _DECISION_RESOLVERS.get(decision)
        return resolver(ctx) if resolver is not None else None

    return gather_alerts(stats.findings, n_samples=stats.n_found, resolve=resolve)


# ---- evidence matrix ----------------------------------------------------------------------------


#: Pretty family labels for the "also considered, ruled out" summary. Anything not here falls back to
#: the family id with dashes turned to spaces.
_FAMILY_PRETTY: dict[str, str] = {
    "10x-3p-gex": "10x 3′ gene expression",
    "10x-5p-gex": "10x 5′ gene expression",
    "bd-rhapsody-wta": "BD Rhapsody WTA",
    "bd-rhapsody-wta-enhanced": "BD Rhapsody WTA (Enhanced)",
    "splitseq": "SPLiT-seq",
    "bulk-rnaseq-pe": "bulk RNA-seq",
}


def _family_map() -> dict[str, str]:
    """``tech id -> family id`` (the KB parent, or the id itself at a root).

    Graceful: if the KB will not load (a stripped install), returns ``{}`` and callers fall back to a
    version-suffix heuristic. Loading the specs is a few-ms YAML read the report can afford.
    """
    try:
        from ..kb import load_all_specs

        return {i: (s.parent or i) for i, s in load_all_specs().items()}
    except Exception:
        return {}


def _family_of(tech: str, fmap: dict[str, str]) -> str:
    if tech in fmap:
        return fmap[tech]
    return re.sub(r"-v[\d.]+$", "", tech) or tech  # heuristic: strip a trailing -v<version>


def _pretty_family(fam: str) -> str:
    return _FAMILY_PRETTY.get(fam, fam.replace("-", " "))


def _humanize_reason(reason: str) -> str:
    """Turn a scorer's forbidden reason into one plain clause a biologist can read.

    Scorer reasons come in two shapes: named gates ("requires FAIL: onlist …") and bare metric
    diagnostics ("mean_maxfrac=0.27", "motif_rate=0.03"). Both are meaningless to a wet-lab reader, so
    each maps to a plain clause; anything unrecognised and code-shaped degrades to a clean generic
    phrase rather than leaking the raw metric onto the page.
    """
    r = re.sub(r"^(requires FAIL:|excludes matched:)\s*", "", reason).strip()
    low = r.lower()
    if "onlist" in low or "whitelist" in low or "barcode" in low:
        return "its barcodes don't match this kit's whitelist"
    if "read-length" in low or "read length" in low or ("mode" in low and "vs" in low):
        return "the read lengths don't fit this kit's layout"
    if "no valid" in low or "unfillable" in low or "no role" in low:
        return "the reads can't be assigned to this kit's roles"
    if (
        "maxfrac" in low
        or "motif" in low
        or "rate" in low
        or re.fullmatch(r"[a-z0-9_]+=[\d.]+", low)
    ):
        return "the reads don't show this kit's barcode pattern"
    return r[:120] if (r and " " in r) else "ruled out by the read patterns"


def _matrices(ws: Path, manifest: DatasetManifest) -> tuple[list[MatrixView], list[RuledOut]]:
    """Locate the persisted evidence matrix for a representative run and split it by chemistry family.

    Returns ``(winner-family grids, ruled-out families)``. ``([], [])`` when no sidecar is found — an
    old cache or a resumed run — and the Evidence tab degrades to the chemistry decision alone.
    """
    cdir = cache_dir(ws)
    candidates_dir, matrices_dir = cdir / "candidates", cdir / "matrices"
    if not (candidates_dir.is_dir() and matrices_dir.is_dir()):
        return [], []
    manifest_shas = {f.sha256 for f in manifest.library.files}
    chem_values = set(manifest.library.chemistry.value)
    winner = manifest.library.chemistry.value[0] if manifest.library.chemistry.value else None
    sha_to_name = {f.sha256: f.basename for f in manifest.library.files}

    for cand_file in sorted(candidates_dir.glob("*.json")):
        try:
            result = json.loads(cand_file.read_text())
        except (ValueError, OSError):
            continue
        candidates = result.get("candidates") if isinstance(result, dict) else None
        if not isinstance(candidates, list) or not candidates:
            continue
        top = candidates[0]
        assignment = top.get("role_assignment", {}).get("assignment", {})
        if top.get("technology") not in chem_values:
            continue
        if not set(assignment.values()) <= manifest_shas:
            continue
        mfile = matrices_dir / f"{cand_file.stem}.json"
        if not mfile.is_file():
            continue
        try:
            matrices = json.loads(mfile.read_text())
        except (ValueError, OSError):
            continue
        if not isinstance(matrices, dict):
            continue
        return _project_matrices(matrices, candidates, winner, chem_values, sha_to_name)
    return [], []


def _project_matrices(
    matrices: dict[str, Any],
    candidates: list[dict[str, Any]],
    winner: str | None,
    chem_values: set[str],
    sha_to_name: dict[str, str],
) -> tuple[list[MatrixView], list[RuledOut]]:
    fmap = _family_map()
    winner_fam = _family_of(winner, fmap) if winner else None
    score_of: dict[str, float | None] = {}
    for c in candidates:
        tech = c.get("technology")
        if isinstance(tech, str):
            sc = c.get("score", {})
            v = sc.get("value") if isinstance(sc, dict) else None
            score_of[tech] = float(v) if isinstance(v, (int, float)) else None

    # Column order: this run's files, sorted by basename, taken from the winner's (or first) tech row.
    ref = winner if winner in matrices else (next(iter(matrices), None))
    shas = _matrix_shas(matrices, ref)
    columns = sorted(shas, key=lambda s: sha_to_name.get(s, s))
    labels = [sha_to_name.get(s, s[:8]) for s in columns]

    # The winner's family stays a full grid (this is where the v2-vs-v3 discrimination lives).
    family_techs = [t for t in matrices if _family_of(t, fmap) == winner_fam]
    family_techs.sort(key=lambda t: (t not in chem_values, -(score_of.get(t) or -1e9), t))
    views = [
        MatrixView(
            tech=tech,
            is_winner=tech in chem_values,
            score=score_of.get(tech),
            file_labels=labels,
            roles=_matrix_rows(matrices.get(tech, {}), columns),
        )
        for tech in family_techs[:_MAX_MATRIX_TECHS]
    ]

    # Every other family collapses to one "considered, ruled out" line.
    by_fam: dict[str, list[str]] = {}
    for tech in matrices:
        fam = _family_of(tech, fmap)
        if fam != winner_fam:
            by_fam.setdefault(fam, []).append(tech)
    ruled = [
        RuledOut(
            tech=_pretty_family(fam),
            family=fam,
            reason=_representative_reason(matrices, by_fam[fam]),
        )
        for fam in sorted(by_fam)
    ]
    return views, ruled


def _matrix_rows(roles_obj: Any, columns: list[str]) -> list[MatrixRoleRow]:
    rows: list[MatrixRoleRow] = []
    if isinstance(roles_obj, dict):
        for role in roles_obj:
            cells_obj = roles_obj[role]
            cells = [
                _cell(cells_obj.get(s) if isinstance(cells_obj, dict) else None) for s in columns
            ]
            rows.append(MatrixRoleRow(role=role, cells=cells))
    return rows


def _representative_reason(matrices: dict[str, Any], techs: list[str]) -> str:
    """The most common forbidden reason across a family's techs, humanized."""
    from collections import Counter

    reasons: list[str] = []
    for tech in techs:
        roles_obj = matrices.get(tech, {})
        if not isinstance(roles_obj, dict):
            continue
        for cells in roles_obj.values():
            if not isinstance(cells, dict):
                continue
            for cell in cells.values():
                if isinstance(cell, dict) and cell.get("status") == "forbidden":
                    r = str(cell.get("reason", "")).strip()
                    if r:
                        reasons.append(r)
    if not reasons:
        return "ruled out by read geometry"
    return _humanize_reason(Counter(reasons).most_common(1)[0][0])


def _matrix_shas(matrices: dict[str, Any], tech: str | None) -> set[str]:
    shas: set[str] = set()
    roles_obj = matrices.get(tech, {}) if tech else {}
    if isinstance(roles_obj, dict):
        for role in roles_obj:
            if isinstance(roles_obj[role], dict):
                shas.update(roles_obj[role])
    return shas


def _cell(raw: Any) -> MatrixCellView:
    if not isinstance(raw, dict):
        return MatrixCellView(status="forbidden", reason="n/a")
    if raw.get("status") == "scored":
        v = raw.get("value")
        return MatrixCellView(
            status="scored", value=float(v) if isinstance(v, (int, float)) else None
        )
    return MatrixCellView(status="forbidden", reason=str(raw.get("reason", "")))


# ---- conclusion / draft -------------------------------------------------------------------------


def _conclusion(*, has_manifest: bool, snakefile: bool) -> ConclusionView:
    if has_manifest and snakefile:
        return ConclusionView(
            kind="compiled",
            exit_code=0,
            headline="Compiled",
            detail="A manifest was validated and a runnable Snakefile was composed from it.",
        )
    return ConclusionView(
        kind="ir_ready",
        exit_code=0,
        headline="Manifest ready",
        detail="The dataset resolved to a validated manifest; no pipeline has been composed yet.",
    )


def _collect_draft(ws: Path) -> AssayReport | None:
    """A best-effort report for a workspace that refused before writing a manifest.

    Reads any persisted :class:`ResolveResult` and, if it carries a blocker or an open question,
    renders a minimal assay that says so — honestly, without inventing a chemistry. Returns ``None``
    when there is nothing decided to show.
    """
    candidates_dir = cache_dir(ws) / "candidates"
    if not candidates_dir.is_dir():
        return None
    blockers: list[str] = []
    questions: list[str] = []
    for f in sorted(candidates_dir.glob("*.json")):
        try:
            result = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        if not isinstance(result, dict):
            continue
        for b in result.get("blockers", []):
            if isinstance(b, dict) and b.get("message"):
                blockers.append(str(b["message"]))
        for q in result.get("questions", []):
            if isinstance(q, dict) and q.get("prompt"):
                questions.append(str(q["prompt"]))
    if not blockers and not questions:
        return None
    if blockers:
        conclusion = ConclusionView(
            kind="blocker",
            exit_code=3,
            headline="Blocked",
            detail="The dataset did not resolve to a manifest; the compiler refused and is waiting.",
            blockers=sorted(set(blockers)),
        )
    else:
        conclusion = ConclusionView(
            kind="question",
            exit_code=4,
            headline="Needs a human",
            detail="The dataset resolved to a question only a human can settle.",
            questions=sorted(set(questions)),
        )
    return AssayReport(
        chemistry=ChemistryDecision(value=[], basis="observed", rung=0, modality="rna", n_files=0),
        conclusion=conclusion,
    )


__all__ = ["collect_report"]
