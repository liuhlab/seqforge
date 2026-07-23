"""Read a ``seqforge/`` workspace and project it into a :class:`ProjectReport`.

This is where all the graceful degradation lives. The manifest is the one required artifact (the
chemistry decision it carries is what makes the page always render); everything else — the harvested
assertions behind a sample quote, the archive records behind a study abstract, the persisted evidence
matrix, the composed pipeline — is joined in if present and simply omitted if not. Nothing here
decides anything: it reads what the deterministic verbs already wrote and flattens it for a human.

The one non-obvious join is the evidence matrix. It is persisted per **run** under a cache key that
folds in tool versions the manifest never stores, so the report never recomputes that key — it scans
``cache/candidates`` for a run whose winning chemistry matches the manifest and whose assigned files
are a subset of the manifest's, then reads the sibling ``cache/matrices`` sidecar. Version-drift-proof
and correct across a multi-run dataset.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from ..models.dataset import DatasetManifest, LibrarySection
from ..models.processing import ProcessingManifest
from ..project import discover_assays
from ..workspace import cache_dir, documents_dir, logs_dir, records_dir, state_dir
from .model import (
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
    PlanView,
    ProjectReport,
    ReadView,
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


def collect_report(workspace: str | Path, *, generated_at: str | None = None) -> ProjectReport:
    """Project a workspace into a :class:`ProjectReport` (one :class:`AssayReport` per assay).

    ``generated_at`` is threaded through verbatim (a caller may pin it for byte-deterministic output).
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
        assays = [_collect_assay(ws, subdir, mpath) for subdir, mpath in assays_on_disk]

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


def _collect_assay(ws: Path, subdir: str | None, manifest_path: Path) -> AssayReport:
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
    pipeline_dir = _find_pipeline(base)
    plan = _plan(ws, proc, pipeline_dir, assertions, doc_index) if proc is not None else None
    conclusion = _conclusion(has_manifest=True, snakefile=pipeline_dir is not None)

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
        samples=_samples(manifest, assertions, doc_index),
        plan=plan,
        matrices=_matrices(ws, manifest),
        conclusion=conclusion,
        provenance=[
            ("dataset_hash", manifest.provenance.dataset_hash),
            ("kb_version", manifest.provenance.kb_version),
            ("seqforge_version", manifest.provenance.seqforge_version),
        ],
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
    """The archive record set for the study, if one was fetched (for the study abstract)."""
    accession = getattr(study, "accession", None) if study is not None else None
    if not accession:
        return {}
    path = records_dir(ws) / f"{accession}.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


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


def _find_pipeline(base: Path) -> Path | None:
    """The composed pipeline dir for this assay (the one holding a ``Snakefile``), or ``None``."""
    snakefiles = sorted((base / "pipeline").glob("*/Snakefile"))
    return snakefiles[0].parent if snakefiles else None


def _plan(
    ws: Path,
    proc: ProcessingManifest,
    pipeline_dir: Path | None,
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
    if pipeline_dir is not None:
        pipeline_name = pipeline_dir.name
        cfg_path = pipeline_dir / "config.yaml"
        if cfg_path.is_file():
            cfg = yaml.safe_load(cfg_path.read_text())
            if isinstance(cfg, dict):
                primary_feature = _as_str_or_none(cfg.get("primary_feature"))
                config_kv = _flatten(cfg)
        snakefile_rel = _rel(ws, pipeline_dir / "Snakefile")
        config_rel = _rel(ws, cfg_path)
        units_rel = _rel(ws, pipeline_dir / "units.tsv")

    return PlanView(
        fields=fields,
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


# ---- evidence matrix ----------------------------------------------------------------------------


def _matrices(ws: Path, manifest: DatasetManifest) -> list[MatrixView]:
    """Locate and project the persisted evidence matrix for a representative run (see module docstring).

    Returns ``[]`` when no sidecar is found — an old cache, or a resumed run that never rebuilt it.
    The Evidence tab degrades to the chemistry decision, which always lives in the manifest.
    """
    cdir = cache_dir(ws)
    candidates_dir, matrices_dir = cdir / "candidates", cdir / "matrices"
    if not (candidates_dir.is_dir() and matrices_dir.is_dir()):
        return []
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
    return []


def _project_matrices(
    matrices: dict[str, Any],
    candidates: list[dict[str, Any]],
    winner: str | None,
    chem_values: set[str],
    sha_to_name: dict[str, str],
) -> list[MatrixView]:
    score_of: dict[str, float | None] = {}
    ranked: list[str] = []
    for c in candidates:
        tech = c.get("technology")
        if isinstance(tech, str) and tech in matrices:
            ranked.append(tech)
            sc = c.get("score", {})
            v = sc.get("value") if isinstance(sc, dict) else None
            score_of[tech] = float(v) if isinstance(v, (int, float)) else None

    order: list[str] = []
    if winner in matrices:
        order.append(winner)
    for tech in ranked:
        if tech not in order:
            order.append(tech)
    for tech in matrices:  # forbidden-only techs, so a runner-up's red cells still show
        if tech not in order:
            order.append(tech)
    order = order[:_MAX_MATRIX_TECHS]

    # Column order: this run's files, sorted by basename, taken from the winner's (or first) tech row.
    ref_tech = order[0] if order else None
    shas = _matrix_shas(matrices, ref_tech)
    columns = sorted(shas, key=lambda s: sha_to_name.get(s, s))
    labels = [sha_to_name.get(s, s[:8]) for s in columns]

    views: list[MatrixView] = []
    for tech in order:
        roles_obj = matrices.get(tech, {})
        rows: list[MatrixRoleRow] = []
        if isinstance(roles_obj, dict):
            for role in roles_obj:
                cells_obj = roles_obj[role]
                cells = [
                    _cell(cells_obj.get(s) if isinstance(cells_obj, dict) else None)
                    for s in columns
                ]
                rows.append(MatrixRoleRow(role=role, cells=cells))
        views.append(
            MatrixView(
                tech=tech,
                is_winner=tech in chem_values,
                score=score_of.get(tech),
                file_labels=labels,
                roles=rows,
            )
        )
    return views


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
