"""`seqforge run` (alias `compile`) -- chain the whole diagram in one headless pass.

FASTQ + metadata in, manifest.yaml + Snakefile out; stops at the first refusal and folds every
stage into one JSON summary. Adds no authority -- it calls the same deterministic stage bodies the
individual verbs run, per assay.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import typer
import yaml
from pydantic import ValidationError

from .. import __version__
from ..compose import ComposeError, compose
from ..io import default_registry
from ..kb import load_spec
from ..manifest import (
    PolicyError,
    ProcessingInputs,
    exit_code_for_report,
    fill_processing,
    validate_processing,
)
from ..workspace import readable, state_dir
from ._common import _auto_cpus, _load_manifest
from .harvest import _harvest_extract_pipeline
from .manifest import _fill_manifest_pipeline
from .processing import _instructions_from
from .root import app

if TYPE_CHECKING:
    from ..models.records import ArchiveRecordSet


def _run_records_stage(
    accession: list[str], records_path: Path | None, *, workspace: Path, offline: bool
) -> tuple[ArchiveRecordSet | None, Path | None]:
    """Fetch + cache the archive records for `run`, returning (the set, a file harvest can render from).

    Where `manifest fill` fetches into memory, `run` writes each record set under `seqforge/records/`
    — the same place `io records` caches — because `run` is the convenience path and every
    stage leaves a resumable artifact. Harvest renders record documents from a *file*, so `run` hands
    the same file to both harvest and fill. `--offline` with an accession refuses, for the reason fill
    does: you asked for those facts, and the manifest is content-addressed and permanent.
    """
    import hashlib

    from ..io.archive import fetch_records
    from ..io.remote import RemoteError
    from ..models.records import ArchiveRecordSet

    if records_path is not None:
        return ArchiveRecordSet.model_validate_json(records_path.read_text()), records_path
    if not accession:
        return None, None
    if offline:
        raise RemoteError(
            f"--accession {', '.join(accession)} needs the archive, and --offline forbids it. "
            f"Fetch once with `seqforge io records {accession[0]}` and pass --records, or drop "
            f"--accession to compile with no sample facts."
        )
    outdir = state_dir(workspace, "records")
    outdir.mkdir(parents=True, exist_ok=True)
    merged: list[Any] = []
    per_accession: list[Path] = []
    for acc in accession:
        record_set = fetch_records(acc)
        target = outdir / f"{acc}.json"
        target.write_text(json.dumps(record_set.model_dump(mode="json"), indent=2))
        per_accession.append(target)
        merged.extend(record_set.records)
    if len(accession) == 1:
        return ArchiveRecordSet(source="ncbi-sra+biosample", query=accession[0], records=merged), (
            per_accession[0]
        )
    # Two accessions render one dataset: harvest needs them in a single document set, so write a
    # combined file keyed by the accession list (the per-accession caches stay, for `io records`).
    combined = ArchiveRecordSet(
        source="ncbi-sra+biosample", query=", ".join(accession), records=merged
    )
    tag = hashlib.sha256(", ".join(sorted(accession)).encode()).hexdigest()
    combined_path = outdir / (readable("combined", tag) + ".json")
    combined_path.write_text(json.dumps(combined.model_dump(mode="json"), indent=2))
    return combined, combined_path


def _run_finish(stages: dict[str, object], code: int) -> None:
    """Emit the single `run` summary and exit with the pipeline's code. Always raises."""
    summary: dict[str, object] = {"ok": code == 0, "exit_code": code, "stages": stages}
    harvest = stages.get("harvest")
    if isinstance(harvest, dict) and isinstance(harvest.get("usage"), dict):
        # The token cost of understanding the prose, surfaced at the top: the full per-document ledger
        # is on disk (seqforge/usage.json) and in the harvest stage; this is the total a reader wants.
        summary["llm_usage"] = harvest["usage"]
    if code == 0:
        assays = stages.get("assays")
        if isinstance(assays, list):  # multi-assay: one manifest + Snakefile per assay
            summary["assays"] = [
                {
                    "chemistry": a.get("chemistry"),
                    "manifest": a.get("manifest"),
                    "snakefile": cast(dict, a.get("compose", {})).get("snakefile_path"),
                }
                for a in assays
            ]
        else:
            summary["manifest"] = cast(dict, stages.get("manifest", {})).get("manifest")
            summary["processing"] = cast(dict, stages.get("processing", {})).get("processing")
            summary["snakefile"] = cast(dict, stages.get("compose", {})).get("snakefile_path")
    typer.echo(json.dumps(summary, indent=2))
    raise typer.Exit(code)


def _harvest_halts_run(payload: dict[str, object] | str, code: int) -> bool:
    """Does a harvest result stop the one-pass, or is it surfaced and stepped past?

    A **conflict** (two instructions disagreeing on a `processing.*` field) or an unavailable provider
    halts `run` — the first decides a value nothing else can, the second means the LLM stage could not
    run at all. A **rejected reference claim** does not: it never entered `assertions.json`, so the
    manifest is built from the accepted claims and the bytes, and chemistry comes from bytes anyway. It
    is reported in the summary (`needs_review` + the `rejected` list), which is what we ask for — "not a
    silent drop" — while letting a paper whose prose the span-checker cannot formally tie to a KB id
    still compile. Standalone `harvest extract` keeps exiting 4 on a rejection; only `run` steps past.
    """
    if code == 0:
        return False
    if code == 4 and isinstance(payload, dict) and not (payload.get("conflicts") or []):
        return False
    return True


def _process_and_compose(
    *,
    manifest: Any,
    state: Path,
    subdir: str | None,
    workspace: Path,
    assembly: str | None,
    annotation: str | None,
    assertions_path: Path | None,
    processing_id: str,
    offline: bool,
    onlist_dir: Path | None,
    outdir: str,
    fastq_dir: Path | None,
    sif_dir: Path | None,
) -> tuple[dict[str, object], int]:
    """Stages 4-5 for ONE assay: the flags (``processing.yaml``) + the deliverable (the Snakefile).

    Writes ``processing.yaml`` under ``state`` and the pipeline under ``seqforge/<subdir>/pipeline/``
    (the flat ``seqforge/pipeline/`` when ``subdir`` is None). Returns ``(summary, exit_code)``; the
    caller folds it into the run summary. Same code the single-assay path always ran, per assay.
    """
    summary: dict[str, object] = {}
    try:
        instructions = _instructions_from(assertions_path)
    except (OSError, ValueError, ValidationError) as exc:
        return {"processing": {"error": str(exc)}}, 2
    try:
        processing, warnings = fill_processing(
            spec=load_spec(manifest.library.chemistry.value[0]),
            dataset=manifest,
            processing=ProcessingInputs(assembly=assembly, annotation_name=annotation),
            instructions=instructions,
            processing_id=processing_id,
            pin=True,
            seqforge_version=__version__,
        )
    except (PolicyError, ValidationError) as exc:
        # The one real decision with no safe default; fill_processing's message already names the
        # organism and how to supply a genome, so pass it through.
        return {"processing": {"error": str(exc)}}, 2
    p_report = validate_processing(processing, dataset=manifest)
    proc_path = state / "processing.yaml"
    proc_path.write_text(yaml.safe_dump(processing.model_dump(mode="json"), sort_keys=True))
    summary["processing"] = {
        "processing": str(proc_path),
        "report": p_report.model_dump(mode="json"),
        "warnings": [w.model_dump(mode="json") for w in warnings],
    }
    if not p_report.ok:
        return summary, exit_code_for_report(p_report)

    try:
        result = compose(
            manifest,
            processing,
            registry=default_registry(offline=offline, local_dir=onlist_dir),
            workspace=workspace,
            outdir=outdir,
            fastq_dir=fastq_dir,
            sif_dir=sif_dir,
            subdir=subdir,
        )
    except ComposeError as exc:
        summary["compose"] = {"error": str(exc)}
        return summary, 3
    summary["compose"] = result.model_dump(mode="json")
    return summary, (3 if any(v == "fail" for v in result.gate.values()) else 0)


@app.command("run")
def run_cmd(
    files: list[Path] = typer.Argument(..., help="The dataset's FASTQ .gz files."),
    accession: list[str] = typer.Option(
        [], "--accession", help="Accession(s): the archive's per-sample records. Optional."
    ),
    records_path: Path | None = typer.Option(
        None, "--records", help="An already-fetched record set, instead of fetching now."
    ),
    doc: list[Path] = typer.Option(
        [], "--doc", help="Reference document(s) — a paper .pdf/.txt/.md — to read for claims."
    ),
    instruction: list[Path] = typer.Option(
        [],
        "--instruction",
        help="Document(s) authored FOR seqforge; only these may set processing.*.",
    ),
    organism: str | None = typer.Option(
        None, "--organism", help="NCBI taxid or name. Optional when --accession declares it."
    ),
    assembly: str | None = typer.Option(
        None,
        "--assembly",
        help="Genome: liulab-genome UCSC assembly id (e.g. ce11). The one decision.",
    ),
    annotation: str | None = typer.Option(
        None, "--annotation", help="Registered GTF name (e.g. WS298)."
    ),
    no_llm: bool = typer.Option(
        False, "--no-llm", help="Skip the one LLM stage; fully deterministic. Ignores --doc."
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="anthropic | deepseek | openai-compatible (default: auto-detect)."
    ),
    model: str | None = typer.Option(None, "--model", help="Override the extraction model."),
    processing_id: str = typer.Option("default", "--id", help="Human slug for the recipe."),
    fastq_dir: Path | None = typer.Option(
        None, "--fastq-dir", help="Where this machine keeps the FASTQs (for units.tsv)."
    ),
    onlist_dir: Path | None = typer.Option(
        None,
        "--onlist-dir",
        envvar="SEQFORGE_ONLIST_DIR",
        help="Directory of downloaded barcode whitelists (<name>.txt.gz).",
    ),
    sif_dir: Path | None = typer.Option(
        None,
        "--sif-dir",
        envvar="LIU_LAB_PACKAGES",
        help="Directory of prebuilt liulab-runtime images (liulab-runtime_<env>.sif).",
    ),
    outdir: str = typer.Option("results", help="Pipeline output directory (written into config)."),
    offline: bool = typer.Option(False, "--offline", help="Never reach the network."),
    cpus: int = typer.Option(
        0, "--cpus", help="Parallel probe workers. 0 = auto (min(8, CPUs)); 1 = sequential."
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
) -> None:
    """One pass: FASTQ + metadata -> manifest.yaml AND a runnable Snakefile.

    Chains the deterministic verbs — records, harvest, manifest fill, processing new, compose — in
    order, stops at the first refusal, and emits ONE JSON summary keyed by stage. It decides nothing
    itself: chemistry, read roles and organism come from the same code the individual verbs run, and
    the exit-code contract is preserved (3 BLOCKED, 4 NEEDS_HUMAN). Re-running is resumable through
    each stage's own content-addressed cache; there is no --resume flag.

    The genome is the one real decision and has no safe default: pass --assembly/--annotation, or state
    it in an --instruction document. Everything else is optional — no accession, no paper, and
    --no-llm each give a quieter, still-true manifest. `harvest extract` is the sole LLM touchpoint and
    calls its own provider (DEEPSEEK_API_KEY / ANTHROPIC_API_KEY), which is why --no-llm exists.
    """
    from ..io.remote import RemoteError

    stages: dict[str, object] = {}

    # 1) Archive records (optional): fetch + cache, or refuse offline.
    records: ArchiveRecordSet | None = None
    records_file: Path | None = None
    try:
        records, records_file = _run_records_stage(
            accession, records_path, workspace=workspace, offline=offline
        )
    except RemoteError as exc:
        stages["records"] = {"error": "records_unavailable", "detail": str(exc)}
        _run_finish(stages, 3)
    if records is not None:
        stages["records"] = {
            "source": records.source,
            "n": {
                level: len(records.at(level))  # type: ignore[arg-type]
                for level in ("project", "sample", "experiment", "run")
            },
        }

    # 2) Harvest — the one LLM stage. Skipped by --no-llm or when there is no prose to read.
    assertions_path: Path | None = None
    if no_llm and (doc or instruction):
        stages["harvest"] = {"skipped": "--no-llm: documents were not read"}
    elif not no_llm and (doc or instruction):
        harvested = _harvest_extract_pipeline(
            docs=doc,
            instruction=instruction,
            records_path=records_file,
            provider=provider,
            model=model,
            verify=True,
            workspace=workspace,
        )
        stages["harvest"] = (
            harvested.payload
            if isinstance(harvested.payload, dict)
            else {"error": harvested.payload}
        )
        if _harvest_halts_run(harvested.payload, harvested.code):
            _run_finish(stages, harvested.code)
        if harvested.code == 4:
            # rejected reference claims survived the halt check: surface them, do not stop (see
            # `_harvest_halts_run`). They were dropped from assertions.json already; this is the "not
            # a silent drop" we ask for, in a field a headless caller still sees.
            cast(dict, stages["harvest"])["needs_review"] = (
                "prose claims failed span-verification and were dropped (see 'rejected'); the manifest "
                "was built from the accepted claims and the bytes"
            )
        assertions_path = state_dir(workspace) / "assertions.json"

    # 3) The IR: what the data IS. Probe + resolve + metadata, both resolvers, both able to refuse.
    fill = _fill_manifest_pipeline(
        files=files,
        organism=organism,
        records=records,
        assertions=assertions_path,
        offline=offline,
        workspace=workspace,
        cpus=_auto_cpus(cpus),
    )
    stages["manifest"] = fill.payload if isinstance(fill.payload, dict) else {"error": fill.payload}
    if fill.code != 0:
        _run_finish(stages, fill.code)

    # A project is one assay (the flat, byte-identical layout) or several (one seqforge/<assay>/ each).
    manifest_payload = cast(dict, stages["manifest"])
    if "assays" in manifest_payload:
        targets = [
            (cast(str, a["chemistry"]), cast(str, a["assay_dir"]), Path(cast(str, a["manifest"])))
            for a in cast(list, manifest_payload["assays"])
        ]
    else:
        targets = [(None, None, Path(cast(str, manifest_payload["manifest"])))]

    # 4-5) The flags + the deliverable, per assay. Each is a normal single-chemistry compile.
    compiled: list[tuple[str | None, str, dict[str, object], int]] = []
    assay_infos: list[dict[str, object]] = []
    worst = 0
    for chemistry, subdir, manifest_path in targets:
        manifest = _load_manifest(manifest_path)
        state = state_dir(workspace, subdir) if subdir else state_dir(workspace)
        summary, code = _process_and_compose(
            manifest=manifest,
            state=state,
            subdir=subdir,
            workspace=workspace,
            assembly=assembly,
            annotation=annotation,
            assertions_path=assertions_path,
            processing_id=processing_id,
            offline=offline,
            onlist_dir=onlist_dir,
            outdir=outdir,
            fastq_dir=fastq_dir,
            sif_dir=sif_dir,
        )
        worst = max(worst, code)
        compiled.append((chemistry, str(manifest_path), summary, code))
        assay_infos.append(
            {
                "chemistry": manifest.library.chemistry.value[0],
                "subdir": subdir,
                "n_samples": len(manifest.experiment.samples),
                "manifest": str(manifest_path),
                "snakefile": cast(dict, summary.get("compose", {})).get("snakefile_path"),
            }
        )

    # The "one study" view over every assay: a flat sample table + an assay index, at the project top.
    # Derived from the manifests (which all exist -- fill succeeded above), so it is written even if a
    # downstream compose failed. See seqforge/project.py.
    from ..project import write_project_views

    tsv_path, project_path = write_project_views(workspace, assay_infos)
    stages["project"] = {
        "sample_metadata": str(tsv_path),
        "project": str(project_path),
        "n_assays": len(assay_infos),
    }

    if targets[0][0] is None:  # single assay: flat stages, byte-identical to before
        _, _, summary, code = compiled[0]
        if "processing" in summary:
            stages["processing"] = summary["processing"]
        if "compose" in summary:
            stages["compose"] = summary["compose"]
        _run_finish(stages, code)
    else:  # multi-assay: one complete record per assay
        stages["assays"] = [
            {"chemistry": chem, "manifest": mpath, **summary}
            for chem, mpath, summary, _ in compiled
        ]
        _run_finish(stages, worst)


app.command(
    "compile", help="Alias for `run`: FASTQ + metadata -> manifest + Snakefile in one pass."
)(run_cmd)
