"""The ``seqforge`` Typer application — the CLI is the API (R8).

Every skill action maps to a deterministic ``seqforge <verb> --json`` that runs with no LLM in the
loop (only ``harvest extract`` and the opt-in ``resolve adjudicate`` touch an LLM). Exit codes are
uniform: ``0`` OK, ``1`` ERROR, ``2`` USAGE, ``3`` BLOCKED (a Blocker), ``4`` NEEDS_HUMAN (an open
Conflict / question).

Milestone 0 wires the deterministic spine incrementally; ``schema export`` is live, the remaining
verbs are declared and raise a clear "not yet implemented" until their stage lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml
from pydantic import ValidationError

from . import __version__
from .compose import ComposeError, compose
from .io import DEFAULT_REGISTRY
from .io.remote import NotYetImplemented, peek, resolve_accession
from .kb import list_spec_ids, load_spec, run_roundtrip
from .manifest import (
    ExperimentInputs,
    FillError,
    ProcessingInputs,
    exit_code_for_report,
    fill_manifest,
    manifest_content_hash,
    provenance_id,
    validate_manifest,
)
from .models import SCHEMA_MODELS, export_all, export_schema
from .models.manifest import Manifest, SampleGroup
from .resolve import Hypothesis, resolve_dataset

app = typer.Typer(
    name="seqforge",
    help="Compile FASTQ + metadata into a validated library manifest and a Snakemake config.",
    no_args_is_help=True,
    add_completion=False,
)

schema_app = typer.Typer(help="Export JSON Schema from the Pydantic models (the source of truth).")
app.add_typer(schema_app, name="schema")

kb_app = typer.Typer(help="The executable, self-testing knowledge base (R10).")
app.add_typer(kb_app, name="kb")

io_app = typer.Typer(help="The network + onlist surface (pooch-cached, sha256-verified).")
app.add_typer(io_app, name="io")

onlist_app = typer.Typer(help="Barcode-whitelist (onlist) registry.")
io_app.add_typer(onlist_app, name="onlist")

resolve_app = typer.Typer(help="Score bytes + KB into a ranked, escalated chemistry decision.")
app.add_typer(resolve_app, name="resolve")

manifest_app = typer.Typer(help="Assemble, validate, and hash the machine-independent manifest.")
app.add_typer(manifest_app, name="manifest")

harvest_app = typer.Typer(
    help="Prose/metadata -> span-verified Assertions (the one LLM touchpoint)."
)
app.add_typer(harvest_app, name="harvest")

eval_app = typer.Typer(help="The evals harness: measure what unit tests cannot (brief §9).")
app.add_typer(eval_app, name="eval")


@app.command()
def version() -> None:
    """Print the seqforge version."""
    typer.echo(__version__)


@schema_app.command("list")
def schema_list() -> None:
    """List every model whose JSON Schema can be exported."""
    for name in sorted(SCHEMA_MODELS):
        typer.echo(name)


@schema_app.command("export")
def schema_export(
    model: str | None = typer.Argument(
        None, help="Model class name to export (e.g. Manifest). Omit with --all for every model."
    ),
    export_all_models: bool = typer.Option(
        False, "--all", help="Export every model's schema as one JSON object."
    ),
) -> None:
    """Dump one model's (or every model's) JSON Schema to stdout."""
    if export_all_models:
        typer.echo(json.dumps(export_all(), indent=2, sort_keys=True))
        return
    if model is None:
        typer.echo("give a MODEL name or --all; see `seqforge schema list`", err=True)
        raise typer.Exit(2)
    try:
        schema = export_schema(model)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(schema, indent=2, sort_keys=True))


@kb_app.command("list")
def kb_list() -> None:
    """List every technology in the knowledge base."""
    for tech_id in list_spec_ids():
        typer.echo(tech_id)


@kb_app.command("show")
def kb_show(tech: str = typer.Argument(..., help="Technology id, e.g. 10x-3p-gex-v3.")) -> None:
    """Dump one technology's validated spec as JSON."""
    try:
        spec = load_spec(tech)
    except FileNotFoundError as exc:
        typer.echo(f"unknown technology {tech!r}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(spec.model_dump(mode="json"), indent=2))


@kb_app.command("lint")
def kb_lint() -> None:
    """Validate every shipped spec.yaml against the schema. Exit 3 if any is invalid."""
    results = []
    ok = True
    for tech_id in list_spec_ids():
        try:
            load_spec(tech_id)
            results.append({"tech": tech_id, "ok": True})
        except (ValidationError, ValueError) as exc:
            ok = False
            results.append({"tech": tech_id, "ok": False, "error": str(exc)})
    typer.echo(json.dumps({"ok": ok, "specs": results}, indent=2))
    if not ok:
        raise typer.Exit(3)


@kb_app.command("roundtrip")
def kb_roundtrip(
    tech: str = typer.Argument(..., help="Technology id to round-trip."),
    seed: int = typer.Option(0, help="RNG seed for the synthetic generator."),
) -> None:
    """Self-test: spec -> synth FASTQ -> probe -> recover; assert recovered == declared. Exit 3 on fail."""
    try:
        result = run_roundtrip(tech, seed=seed)
    except FileNotFoundError as exc:
        typer.echo(f"unknown technology {tech!r}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(result, indent=2))
    if not result["passed"]:
        raise typer.Exit(3)


@kb_app.command("e2e")
def kb_e2e(
    workdir: Path = typer.Option(..., "--workdir", help="Scratch dir for reads + STAR output."),
    assembly: str = typer.Option("sacCer3", help="liulab-genome assembly id."),
    annotation: str = typer.Option("ensembl_R64-1-1", help="Registered GTF name."),
    fasta: Path | None = typer.Option(
        None, help="Override: genome FASTA (else via liulab-genome)."
    ),
    gtf: Path | None = typer.Option(None, help="Override: GTF (else via liulab-genome)."),
    star_index: Path | None = typer.Option(
        None, "--star-index", help="Override: prebuilt STAR index."
    ),
    star: str | None = typer.Option(
        None, "--star", help="STAR binary (e.g. liulab-runtime align-rna)."
    ),
    n_cells: int = typer.Option(8, help="Simulated cells."),
    reads_per_cell: int = typer.Option(250, help="Simulated reads per cell."),
    threads: int = typer.Option(8, help="STAR threads."),
    seed: int = typer.Option(0, help="Simulation seed."),
) -> None:
    """The real count-matrix run: simulate -> resolve -> compose -> STARsolo -> assert vs ground truth.

    Exit 3 if the recovered matrix does not equal what was injected (or if a strand inversion would
    go undetected); exit 1 if the toolchain (STAR / a genome index) is unavailable.
    """
    from .e2e import E2EUnavailable, discover_assets, run_e2e

    try:
        assets = discover_assets(
            assembly=assembly,
            annotation=annotation,
            fasta=fasta,
            gtf=gtf,
            star_index=star_index,
            star_bin=star,
        )
        result = run_e2e(
            assets,
            workdir=workdir,
            n_cells=n_cells,
            reads_per_cell=reads_per_cell,
            threads=threads,
            seed=seed,
        )
    except E2EUnavailable as exc:
        typer.echo(json.dumps({"skipped": True, "reason": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result.get("passed"):
        raise typer.Exit(3)


@onlist_app.command("list")
def io_onlist_list() -> None:
    """List the onlists declared in the default registry (none are materialized in the pilot)."""
    rows = []
    for name in DEFAULT_REGISTRY.names():
        entry = DEFAULT_REGISTRY.get(name)
        rows.append(
            {
                "name": entry.name,
                "width": entry.width,
                "orientation": entry.orientation,
                "n_entries": entry.n_entries,
                "fetchable": entry.fetchable,
            }
        )
    typer.echo(json.dumps({"onlists": rows}, indent=2))


@onlist_app.command("show")
def io_onlist_show(
    name: str = typer.Argument(..., help="Registry name, e.g. 3M-february-2018."),
) -> None:
    """Show one onlist registry entry as JSON."""
    if not DEFAULT_REGISTRY.has(name):
        typer.echo(f"unknown onlist {name!r}", err=True)
        raise typer.Exit(2)
    entry = DEFAULT_REGISTRY.get(name)
    typer.echo(
        json.dumps(
            {
                "name": entry.name,
                "uri": entry.uri,
                "sha256": entry.sha256,
                "width": entry.width,
                "orientation": entry.orientation,
                "n_entries": entry.n_entries,
                "fetchable": entry.fetchable,
            },
            indent=2,
        )
    )


@io_app.command("peek")
def io_peek(uri: str = typer.Argument(..., help="Remote FASTQ URI to range-read.")) -> None:
    """Range-read a remote FASTQ header into a partial Observation (not yet implemented)."""
    try:
        typer.echo(json.dumps(peek(uri)))
    except NotYetImplemented as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@io_app.command("resolve")
def io_resolve(
    accession: str = typer.Argument(..., help="ENA/SRA/GEO/BioProject accession."),
) -> None:
    """Expand an accession into a file inventory (not yet implemented)."""
    try:
        typer.echo(json.dumps(resolve_accession(accession)))
    except NotYetImplemented as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@resolve_app.command("score")
def resolve_score(
    files: list[Path] = typer.Argument(..., help="The dataset's FASTQ .gz files."),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for .seqforge/ state."
    ),
    assert_chemistry: str | None = typer.Option(
        None,
        "--assert-chemistry",
        help="A metadata-asserted chemistry (the span-verified hypothesis).",
    ),
    explain: bool = typer.Option(
        False, "--explain", help="Also emit the JSON-safe evidence matrices."
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Do not read/write .seqforge/ artifacts."
    ),
    max_reads: int = typer.Option(200_000, help="Bounded read budget (R3)."),
    max_bytes: int = typer.Option(256 * 1024 * 1024, help="Bounded decompressed-byte cap (R3)."),
) -> None:
    """Score FASTQ bytes + KB into a ResolveResult. Exit 3 on a Blocker, 4 on an open Conflict/question."""
    hypothesis = Hypothesis(value=assert_chemistry) if assert_chemistry else None
    output = resolve_dataset(
        [str(f) for f in files],
        hypothesis=hypothesis,
        workspace=workspace,
        max_reads=max_reads,
        max_bytes=max_bytes,
        use_cache=not no_cache,
    )
    payload: dict[str, object] = output.result.model_dump(mode="json")
    if explain:
        payload = {"result": payload, "matrices": output.matrices}
    typer.echo(json.dumps(payload, indent=2))
    code = output.exit_code()
    if code != 0:
        raise typer.Exit(code)


@harvest_app.command("normalize")
def harvest_normalize(
    docs: list[Path] = typer.Argument(..., help="Source documents (.txt/.md/.pdf)."),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for .seqforge/ state."
    ),
) -> None:
    """Extract each document ONCE into the canonical text that spans are computed against (R5)."""
    from .harvest import normalize_document

    outdir = Path(workspace) / ".seqforge" / "normalized"
    outdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for doc in docs:
        try:
            nd = normalize_document(doc)
        except (OSError, RuntimeError) as exc:
            typer.echo(f"{doc}: {exc}", err=True)
            raise typer.Exit(1) from exc
        target = outdir / f"{nd.doc_sha256}.txt"
        target.write_text(nd.text)
        rows.append(
            {
                "source": nd.source_basename,
                "doc_sha256": nd.doc_sha256,
                "normalized_sha256": nd.normalized_sha256,
                "normalizer_version": nd.normalizer_version,
                "n_chars": nd.n_chars,
                "path": str(target.relative_to(Path(workspace))),
            }
        )
    typer.echo(json.dumps({"normalized": rows}, indent=2))


@harvest_app.command("extract")
def harvest_extract(
    docs: list[Path] = typer.Argument(..., help="Source documents (.txt/.md/.pdf)."),
    provider: str | None = typer.Option(
        None, "--provider", help="anthropic | deepseek | openai-compatible (default: auto-detect)."
    ),
    model: str | None = typer.Option(
        None, "--model", help="Override the model (default: the provider's own default)."
    ),
    verify: bool = typer.Option(
        True, "--verify/--no-verify", help="Span-verify the drafts immediately (R5)."
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for .seqforge/ state."
    ),
) -> None:
    """The ONE LLM touchpoint: prose -> AssertionDraft[] -> (verified) Assertion[].

    The model only proposes `{field, value, quote}`; code computes the offsets and decides what
    survives — which is what makes the provider swappable. Auto-detects DEEPSEEK_API_KEY /
    ANTHROPIC_API_KEY. Exit 1 if the LLM surface is unavailable, 4 if any claim fails verification.
    """
    from .harvest import (
        ExtractUnavailable,
        ProviderUnavailable,
        extract_drafts,
        normalize_document,
        resolve_provider,
        verify_drafts,
    )
    from .kb import load_all_specs

    specs = load_all_specs()
    state = Path(workspace) / ".seqforge"
    state.mkdir(parents=True, exist_ok=True)
    try:
        llm = resolve_provider(provider)
    except ProviderUnavailable as exc:
        typer.echo(json.dumps({"error": "no_provider", "detail": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc
    chosen = model or llm.default_model()

    all_drafts = []
    normalized = []
    usage_total: dict[str, int] = {}
    extractor = None
    for doc in docs:
        nd = normalize_document(doc)
        normalized.append(nd)
        try:
            outcome = extract_drafts(nd, specs, provider=llm, model=chosen)
        except ExtractUnavailable as exc:
            typer.echo(
                json.dumps({"error": "llm_unavailable", "detail": str(exc)}, indent=2), err=True
            )
            raise typer.Exit(1) from exc
        all_drafts.extend(outcome.drafts)
        extractor = outcome.extractor
        for k, v in outcome.usage.items():
            usage_total[k] = usage_total.get(k, 0) + v

    payload: dict[str, object] = {
        "provider": llm.name,
        "model": chosen,
        "n_drafts": len(all_drafts),
        "usage": usage_total,
        "drafts": [d.model_dump(mode="json") for d in all_drafts],
    }
    if not verify:
        typer.echo(json.dumps(payload, indent=2))
        return

    assert extractor is not None
    report = verify_drafts(all_drafts, normalized, extractor=extractor)
    (state / "assertions.json").write_text(
        json.dumps([a.model_dump(mode="json") for a in report.assertions], indent=2)
    )
    payload["n_accepted"] = report.n_accepted
    payload["n_rejected"] = len(report.rejected)
    payload["rejected"] = report.rejected
    payload["assertions"] = [a.model_dump(mode="json") for a in report.assertions]
    typer.echo(json.dumps(payload, indent=2))
    if report.rejected:
        raise typer.Exit(4)  # a claim that failed the tripwire needs a human, not a silent drop


@harvest_app.command("verify")
def harvest_verify(
    drafts_json: Path = typer.Argument(..., help="AssertionDraft[] JSON (from `harvest extract`)."),
    docs: list[Path] = typer.Option(..., "--doc", help="Source document(s) the drafts cite."),
    model_id: str = typer.Option("unknown", help="Model that produced the drafts (provenance)."),
    prompt_version: str = typer.Option("unknown", help="Prompt version (provenance)."),
) -> None:
    """Grep each quote back into the canonical text + check it entails the value. Exit 4 if any fail.

    Both flags are code-owned, so a hallucinated or mis-attributed claim fails closed (R5).
    """
    from .harvest import normalize_document, verify_drafts
    from .models.assertion import AssertionDraft, ExtractorProvenance

    try:
        raw = json.loads(drafts_json.read_text())
        drafts = [AssertionDraft.model_validate(d) for d in raw]
    except (OSError, ValidationError, ValueError) as exc:
        typer.echo(f"cannot read drafts {drafts_json}: {exc}", err=True)
        raise typer.Exit(2) from exc

    normalized = [normalize_document(d) for d in docs]
    report = verify_drafts(
        drafts,
        normalized,
        extractor=ExtractorProvenance(model_id=model_id, prompt_version=prompt_version),
    )
    typer.echo(
        json.dumps(
            {
                "n_drafts": len(drafts),
                "n_accepted": report.n_accepted,
                "n_rejected": len(report.rejected),
                "assertions": [a.model_dump(mode="json") for a in report.assertions],
                "rejected": report.rejected,
            },
            indent=2,
        )
    )
    if report.rejected:
        raise typer.Exit(4)  # a rejected claim needs a human, not a silent drop


def _load_manifest(path: Path) -> Manifest:
    try:
        return Manifest.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, ValidationError, ValueError) as exc:
        typer.echo(f"cannot read manifest {path}: {exc}", err=True)
        raise typer.Exit(2) from exc


@manifest_app.command("fill")
def manifest_fill(
    files: list[Path] = typer.Argument(..., help="The dataset's FASTQ .gz files."),
    organism: int = typer.Option(..., "--organism", help="NCBI taxid (metadata truth, e.g. 6239)."),
    assembly: str = typer.Option(
        ..., "--assembly", help="liulab-genome UCSC assembly id (e.g. ce11)."
    ),
    annotation: str = typer.Option(..., "--annotation", help="Registered GTF name (e.g. WS298)."),
    accession: list[str] = typer.Option([], "--accession", help="Accession(s) for this dataset."),
    sample_id: str = typer.Option("sample1", "--sample-id", help="Sample id for the file group."),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for .seqforge/ state."
    ),
) -> None:
    """Probe -> resolve -> assemble a manifest. Writes manifest.yaml ONLY after a clean validate (R7)."""
    out = resolve_dataset([str(f) for f in files], workspace=workspace, use_cache=False)
    if out.exit_code() != 0:
        typer.echo(json.dumps(out.result.model_dump(mode="json"), indent=2))
        raise typer.Exit(out.exit_code())
    winner = out.result.candidates[0]
    spec = load_spec(winner.technology)
    samples = [
        SampleGroup(sample_id=sample_id, file_uris=[o.file.basename for o in out.observations])
    ]
    try:
        manifest = fill_manifest(
            result=out.result,
            spec=spec,
            observations=out.observations,
            registry=DEFAULT_REGISTRY,
            experiment=ExperimentInputs(
                organism_taxid=organism, accessions=list(accession), samples=samples
            ),
            processing=ProcessingInputs(assembly=assembly, annotation_name=annotation),
            seqforge_version=__version__,
        )
    except FillError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(3) from exc

    report = validate_manifest(manifest, conflicts=out.result.conflicts)
    state = Path(workspace) / ".seqforge"
    state.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=True)
    # R7: manifest.yaml exists only if it validated clean; otherwise it stays a draft.
    target = state / ("manifest.yaml" if report.ok else "manifest.draft.yaml")
    target.write_text(payload)
    typer.echo(
        json.dumps({"manifest": str(target), "report": report.model_dump(mode="json")}, indent=2)
    )
    raise typer.Exit(exit_code_for_report(report))


@manifest_app.command("validate")
def manifest_validate(
    manifest_path: Path = typer.Argument(..., help="Path to a manifest.yaml."),
) -> None:
    """Validate a manifest. Exit 3 on a Blocker, 4 on an open Conflict (R4)."""
    report = validate_manifest(_load_manifest(manifest_path))
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
    raise typer.Exit(exit_code_for_report(report))


@manifest_app.command("hash")
def manifest_hash_cmd(
    manifest_path: Path = typer.Argument(..., help="Path to a manifest.yaml."),
) -> None:
    """Print the manifest's content hash and its provenance id."""
    manifest = _load_manifest(manifest_path)
    content = manifest_content_hash(manifest)
    typer.echo(
        json.dumps(
            {
                "manifest_hash": content,
                "recorded_hash": manifest.provenance.manifest_hash,
                "matches": content == manifest.provenance.manifest_hash,
                "provenance_id": provenance_id(
                    content, manifest.provenance.kb_version, manifest.provenance.workflow_version
                ),
            },
            indent=2,
        )
    )


@app.command("compose")
def compose_cmd(
    manifest_path: Path = typer.Argument(..., help="Path to a validated manifest.yaml."),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for .seqforge/ state."
    ),
    outdir: str = typer.Option(
        "results", help="Pipeline output directory (written into the config)."
    ),
    threads: int = typer.Option(8, help="Threads to request per mapping job."),
) -> None:
    """Compile a manifest into config.yaml + units.tsv + a module selection. Exit 3 if a gate fails."""
    manifest = _load_manifest(manifest_path)
    report = validate_manifest(manifest)
    if not report.ok:
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2), err=True)
        typer.echo("refusing to compose an invalid manifest", err=True)
        raise typer.Exit(exit_code_for_report(report))
    try:
        result = compose(manifest, workspace=workspace, outdir=outdir, threads=threads)
    except ComposeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    if any(v == "fail" for v in result.gate.values()):
        raise typer.Exit(3)


@eval_app.command("list")
def eval_list(
    cases_dir: Path | None = typer.Option(
        None, "--cases", help="Case root (default: evals/cases)."
    ),
) -> None:
    """List the eval corpus: id, expected outcome, and whether the case needs an LLM."""
    from .evals import CaseError, load_cases

    try:
        cases = load_cases(cases_dir)
    except CaseError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    payload = [
        {
            "id": c.id,
            "outcome": c.expected.outcome,
            "needs_llm": c.has_prose and c.recipe.hypothesis is None,
            "description": " ".join(c.expected.description.split())[:100],
        }
        for c in cases
    ]
    typer.echo(json.dumps(payload, indent=2))


@eval_app.command("run")
def eval_run(
    case: list[str] = typer.Option(None, "--case", help="Run only these case ids (repeatable)."),
    cases_dir: Path | None = typer.Option(
        None, "--cases", help="Case root (default: evals/cases)."
    ),
    llm: bool = typer.Option(
        False, "--llm/--no-llm", help="Run prose cases through harvest extract (costs tokens)."
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="anthropic | deepseek | openai-compatible (default: auto-detect)."
    ),
    model: str | None = typer.Option(
        None, "--model", help="Override the provider's default model."
    ),
    trials: int = typer.Option(
        1, "--trials", min=1, help="Re-run each prose case N times; extraction is nondeterministic."
    ),
    fail_under: float = typer.Option(
        1.0, "--fail-under", help="Exit 3 if field accuracy drops below this."
    ),
) -> None:
    """Run the eval corpus and report brief §9's metrics.

    `--no-llm` (the default) restricts to deterministic cases, so this runs in a CI with no API key;
    prose cases skip rather than fail. Exit 3 if any false-accept occurs or accuracy drops below
    `--fail-under` — a false accept is never tolerable at any threshold, so it is not on a slider.
    """
    from .evals import CaseError, Grade, load_cases, run_cases
    from .harvest import ProviderUnavailable, resolve_provider

    try:
        cases = load_cases(cases_dir, only=list(case) if case else None)
    except CaseError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    if not cases:
        typer.echo("no cases found", err=True)
        raise typer.Exit(2)

    llm_provider = None
    if llm:
        try:
            llm_provider = resolve_provider(provider)
        except ProviderUnavailable as exc:
            typer.echo(json.dumps({"error": "no_provider", "detail": str(exc)}, indent=2), err=True)
            raise typer.Exit(1) from exc

    report, runs = run_cases(cases, llm=llm, provider=llm_provider, model=model, trials=trials)
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))

    false_accepts = [r for r in runs if r.skipped is None and r.grade.grade is Grade.FALSE_ACCEPT]
    if false_accepts:
        typer.echo(
            f"FALSE ACCEPT in {len(false_accepts)} case(s): "
            f"{[r.case_id for r in false_accepts]} — a confident wrong manifest is the one "
            f"failure the corpus never recovers from",
            err=True,
        )
        raise typer.Exit(3)
    if report.field_accuracy < fail_under:
        typer.echo(
            f"field accuracy {report.field_accuracy:.3f} < --fail-under {fail_under}", err=True
        )
        raise typer.Exit(3)


if __name__ == "__main__":  # pragma: no cover
    app()
