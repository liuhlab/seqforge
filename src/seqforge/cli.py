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
    PolicyError,
    ProcessingInputs,
    dataset_content_hash,
    exit_code_for_report,
    fill_manifest,
    fill_processing,
    instructions_from_assertions,
    processing_content_hash,
    validate_manifest,
    validate_processing,
)
from .models import SCHEMA_MODELS, export_all, export_schema
from .models.dataset import DatasetManifest, SampleGroup
from .models.processing import ProcessingManifest
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

manifest_app = typer.Typer(
    help="The DATASET manifest: what the data IS. Immutable, one per dataset (R13)."
)
app.add_typer(manifest_app, name="manifest")
processing_app = typer.Typer(
    help="The PROCESSING manifest: what to DO with a dataset. Many per dataset (R13)."
)
app.add_typer(processing_app, name="processing")

harvest_app = typer.Typer(
    help="Prose/metadata -> span-verified Assertions (the one LLM touchpoint)."
)
app.add_typer(harvest_app, name="harvest")

eval_app = typer.Typer(help="The evals harness: measure what unit tests cannot (brief §9).")
app.add_typer(eval_app, name="eval")

hook_app = typer.Typer(help="Agent hooks: the rules as mechanism, not aspiration (design §4.2).")
app.add_typer(hook_app, name="hook")


@app.command()
def version() -> None:
    """Print the seqforge version."""
    typer.echo(__version__)


@app.command("probe")
def probe_cmd(
    files: list[Path] = typer.Argument(..., help="FASTQ .gz files to fingerprint."),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for .seqforge/ state."
    ),
    max_reads: int = typer.Option(200_000, help="Bounded read budget (R3)."),
    max_bytes: int = typer.Option(256 * 1024 * 1024, help="Bounded decompressed-byte cap (R3)."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Do not write .seqforge/ artifacts."),
) -> None:
    """Fingerprint FASTQ bytes into role-free Observations. No LLM, no network, bounded (R3).

    The budget is the point: a 40 GB file costs the same as a 40 MB one, because probe stops at
    --max-reads AND --max-bytes, whichever comes first. Never returns 3/4 — it only observes; refusal
    happens downstream when a validator reads the observation.
    """
    from .probe import probe_file
    from .resolve import Cache

    cache = Cache(workspace) if not no_cache else None
    observations = []
    for path in files:
        try:
            obs = probe_file(path, max_reads=max_reads, max_bytes=max_bytes)
        except (OSError, ValueError) as exc:
            typer.echo(json.dumps({"error": f"{path}: {exc}"}, indent=2), err=True)
            raise typer.Exit(1) from exc
        if cache is not None:
            cache.write_observation(obs)
        observations.append(obs.model_dump(mode="json"))
    typer.echo(json.dumps(observations if len(observations) > 1 else observations[0], indent=2))


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


@kb_app.command("e2e-introns")
def kb_e2e_introns(
    workdir: Path = typer.Option(..., "--workdir", help="Scratch dir for reads + STAR output."),
    assembly: str = typer.Option("ce11", help="Must be intron-rich; sacCer3 cannot test this."),
    annotation: str = typer.Option("WS298", help="Registered GTF name."),
    fasta: Path | None = typer.Option(None, help="Override: genome FASTA."),
    gtf: Path | None = typer.Option(None, help="Override: GTF."),
    star_index: Path | None = typer.Option(
        None, "--star-index", help="Override: prebuilt STAR index."
    ),
    star: str | None = typer.Option(None, "--star", help="STAR binary (liulab-runtime align-rna)."),
    n_cells: int = typer.Option(8, help="Simulated cells."),
    reads_per_cell: int = typer.Option(250, help="Simulated reads per cell."),
    intron_frac: float = typer.Option(0.4, help="Fraction of reads drawn from introns (pre-mRNA)."),
    threads: int = typer.Option(8, help="STAR threads."),
    seed: int = typer.Option(0, help="Simulation seed."),
) -> None:
    """The intron-rich / GeneFull gate: inject intronic reads, assert Gene and GeneFull disagree right.

    Yeast is nearly intron-free, so the sacCer3 e2e certifies neither counting rule. This injects a
    known number of intronic reads (what a single-NUCLEUS library actually contains) and asserts Gene
    recovers only the exonic truth while GeneFull recovers exon+intron — both from ONE STARsolo run,
    so the alignment is identical and only the counting rule differs. Reports `gene_signal_lost`: what
    `--soloFeatures Gene` silently discards from a nuclear library. Exit 3 on failure, 1 if the
    toolchain is unavailable.
    """
    from .e2e import E2EUnavailable, discover_assets, run_intron_e2e

    try:
        assets = discover_assets(
            assembly=assembly,
            annotation=annotation,
            fasta=fasta,
            gtf=gtf,
            star_index=star_index,
            star_bin=star,
        )
        result = run_intron_e2e(
            assets,
            workdir=workdir,
            n_cells=n_cells,
            reads_per_cell=reads_per_cell,
            intron_frac=intron_frac,
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
def io_peek(
    uri: str = typer.Argument(..., help="Remote FASTQ URI to range-read."),
    max_reads: int = typer.Option(4, help="Records to report from the fetched prefix."),
    max_bytes: int = typer.Option(1 << 16, help="Compressed bytes to range-read (R3 budget)."),
) -> None:
    """Range-read the head of a remote gzipped FASTQ. Never downloads the file (R3).

    64 KB is ~0.013% of a 517 MB run. Exit 1 if the host ignores Range and answers 200 with the whole
    file — bounded means bounded by the server, not by our intentions.
    """
    from .io.remote import RemoteError

    try:
        typer.echo(json.dumps(peek(uri, max_reads=max_reads, max_bytes=max_bytes), indent=2))
    except (NotYetImplemented, RemoteError) as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc


@io_app.command("resolve")
def io_resolve(
    accession: str = typer.Argument(..., help="GSE/GSM, PRJNA/PRJEB, SRP/SRX/SRR, SAMN..."),
    check_reads: bool = typer.Option(
        True,
        "--check-reads/--no-check-reads",
        help="Compare SRA's per-read table to what ENA published (detects a dropped technical read).",
    ),
) -> None:
    """Expand an accession into runs + declared metadata, and flag a dropped technical read.

    The important part is the flag, not the inventory: fasterq-dump skips technical reads BY DEFAULT,
    so a 10x barcode read routinely vanishes from the published FASTQ while staying inside the .sra —
    leaving a dataset that looks like plain single-end RNA-seq. Two metadata calls catch it before a
    byte is downloaded (R11 rung 0). Exit 4 if any run is missing one: a human must re-fetch it.
    """
    from .io.remote import RemoteError

    try:
        result = resolve_accession(accession, check_reads=check_reads)
    except (NotYetImplemented, RemoteError) as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2))
    if result.get("n_runs_missing_technical_read"):
        raise typer.Exit(4)


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
    docs: list[Path] = typer.Argument(None, help="Reference documents to cite (.txt/.md/.pdf)."),
    instruction: list[Path] = typer.Option(
        [],
        "--instruction",
        help="Document(s) authored FOR seqforge (e.g. alignment_instruction.md).",
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for .seqforge/ state."
    ),
) -> None:
    """Extract each document ONCE into the canonical text that spans are computed against (R5).

    A document's ROLE is the flag it arrived under, never its filename: only an --instruction document
    may set processing.* (R13). `alignment_instruction.md` is a convention you pass here, load-bearing
    nowhere — a filename trigger would be spoofable by renaming a downloaded PDF.
    """
    from .harvest import normalize_document

    outdir = Path(workspace) / ".seqforge" / "normalized"
    outdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for doc, role in _roled(docs, instruction):
        try:
            nd = normalize_document(doc, role=role)
        except (OSError, RuntimeError) as exc:
            typer.echo(f"{doc}: {exc}", err=True)
            raise typer.Exit(1) from exc
        target = outdir / f"{nd.doc_sha256}.txt"
        target.write_text(nd.text)
        rows.append(
            {
                "source": nd.source_basename,
                "role": nd.role,
                "doc_sha256": nd.doc_sha256,
                "normalized_sha256": nd.normalized_sha256,
                "normalizer_version": nd.normalizer_version,
                "n_chars": nd.n_chars,
                "path": str(target.relative_to(Path(workspace))),
            }
        )
    typer.echo(json.dumps({"normalized": rows}, indent=2))


def _roled(docs: list[Path] | None, instruction: list[Path] | None) -> list[tuple[Path, str]]:
    """Pair each document with the ROLE its flag assigned. Code owns role; a filename never does."""
    pairs: list[tuple[Path, str]] = [(d, "reference") for d in (docs or [])]
    pairs += [(d, "instruction") for d in (instruction or [])]
    if not pairs:
        typer.echo("give at least one document, or --instruction FILE", err=True)
        raise typer.Exit(2)
    return pairs


@harvest_app.command("extract")
def harvest_extract(
    docs: list[Path] = typer.Argument(None, help="Reference documents to cite (.txt/.md/.pdf)."),
    instruction: list[Path] = typer.Option(
        [],
        "--instruction",
        help="Document(s) authored FOR seqforge; only these may set processing.*.",
    ),
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
    for doc, role in _roled(docs, instruction):
        nd = normalize_document(doc, role=role)
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
    # what the user may act on: verified directives, projected onto the instructable surface
    instructions, conflicts = instructions_from_assertions(
        report.assertions,
        instruction_docs=frozenset(d.doc_sha256 for d in normalized if d.role == "instruction"),
    )
    payload["instructions"] = [
        {"field": i.field, "value": i.value, "basis": i.basis, "evidence": i.evidence}
        for i in instructions
    ]
    payload["conflicts"] = [c.model_dump(mode="json") for c in conflicts]
    payload["rejected"] = report.rejected
    payload["assertions"] = [a.model_dump(mode="json") for a in report.assertions]
    typer.echo(json.dumps(payload, indent=2))
    if conflicts:
        raise typer.Exit(4)  # two instructions disagreeing has no tiebreak — only the author knows
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


def _load_manifest(path: Path) -> DatasetManifest:
    try:
        return DatasetManifest.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, ValidationError, ValueError) as exc:
        typer.echo(f"cannot read manifest {path}: {exc}", err=True)
        raise typer.Exit(2) from exc


def _load_processing(path: Path) -> ProcessingManifest:
    try:
        return ProcessingManifest.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, ValidationError, ValueError) as exc:
        typer.echo(f"cannot read processing manifest {path}: {exc}", err=True)
        raise typer.Exit(2) from exc


@manifest_app.command("fill")
def manifest_fill(
    files: list[Path] = typer.Argument(..., help="The dataset's FASTQ .gz files."),
    organism: int = typer.Option(..., "--organism", help="NCBI taxid (metadata truth, e.g. 6239)."),
    accession: list[str] = typer.Option([], "--accession", help="Accession(s) for this dataset."),
    sample_id: str = typer.Option("sample1", "--sample-id", help="Sample id for the file group."),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for .seqforge/ state."
    ),
) -> None:
    """Probe -> resolve -> assemble the DATASET manifest: what the data IS (R13).

    Takes no genome. Choosing a reference is intent, not something you learn by probing bytes, so it
    lives in `seqforge processing new`. Writes manifest.yaml ONLY after a clean validate (R7).
    """
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
    """Print the dataset manifest's content hash and whether it matches the recorded one."""
    manifest = _load_manifest(manifest_path)
    content = dataset_content_hash(manifest)
    typer.echo(
        json.dumps(
            {
                "dataset_hash": content,
                "recorded_hash": manifest.provenance.dataset_hash,
                "matches": content == manifest.provenance.dataset_hash,
            },
            indent=2,
        )
    )


# ---------------------------------------------------------------- processing (the flags)
@processing_app.command("new")
def processing_new(
    dataset_path: Path = typer.Argument(..., help="Path to the dataset manifest.yaml."),
    assembly: str = typer.Option(
        ..., "--assembly", help="liulab-genome UCSC assembly id (e.g. ce11)."
    ),
    annotation: str = typer.Option(..., "--annotation", help="Registered GTF name (e.g. WS298)."),
    quantify: str | None = typer.Option(
        None,
        "--quantify",
        help="Comma-separated soloFeatures. EXACT replacement of the default (which counts all five).",
    ),
    threads: int | None = typer.Option(None, "--threads", help="Threads per mapping job."),
    processing_id: str = typer.Option("default", "--id", help="Human slug for this recipe."),
    pin: bool = typer.Option(
        True,
        "--pin/--template",
        help="Bind to this dataset's hash, or leave it portable across datasets.",
    ),
    out: Path | None = typer.Option(None, "-o", "--out", help="Write here (default: stdout)."),
) -> None:
    """Author a PROCESSING manifest: what to DO with a dataset (R13). Many per dataset.

    With no flags you get the policy default, which counts every soloFeature (R15) — so the common
    case needs no decision from you. --quantify replaces that list exactly; narrowing it warns,
    because dropping a feature is the only irreversible act here.
    """
    dataset = _load_manifest(dataset_path)
    spec = load_spec(dataset.library.chemistry.value[0])
    try:
        processing, warnings = fill_processing(
            spec=spec,
            dataset=dataset,
            processing=ProcessingInputs(
                assembly=assembly,
                annotation_name=annotation,
                features=_parse_quantify(quantify),
                threads=threads,
            ),
            processing_id=processing_id,
            pin=pin,
            seqforge_version=__version__,
        )
    except (PolicyError, ValidationError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    report = validate_processing(processing, dataset=dataset)
    payload = yaml.safe_dump(processing.model_dump(mode="json"), sort_keys=True)
    if out is not None:
        out.write_text(payload)
        typer.echo(
            json.dumps(
                {
                    "processing": str(out),
                    "report": report.model_dump(mode="json"),
                    "warnings": [w.model_dump(mode="json") for w in warnings],
                },
                indent=2,
            )
        )
    else:
        typer.echo(payload)
    raise typer.Exit(exit_code_for_report(report))


def _parse_quantify(value: str | None) -> tuple[str, ...] | None:
    """`--quantify Gene,GeneFull` -> the tuple. The MODEL validates membership, not this parser."""
    if value is None:
        return None
    return tuple(v.strip() for v in value.split(",") if v.strip())


@processing_app.command("validate")
def processing_validate(
    processing_path: Path = typer.Argument(..., help="Path to a processing.yaml."),
    dataset_path: Path | None = typer.Option(
        None, "--dataset", help="Cross-check against this dataset manifest (pin + organism)."
    ),
) -> None:
    """Validate a processing manifest. Exit 3 on a Blocker (R4)."""
    processing = _load_processing(processing_path)
    dataset = _load_manifest(dataset_path) if dataset_path is not None else None
    report = validate_processing(processing, dataset=dataset)
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
    raise typer.Exit(exit_code_for_report(report))


@processing_app.command("hash")
def processing_hash_cmd(
    processing_path: Path = typer.Argument(..., help="Path to a processing.yaml."),
) -> None:
    """Print the processing manifest's content hash and whether it matches the recorded one."""
    processing = _load_processing(processing_path)
    content = processing_content_hash(processing)
    typer.echo(
        json.dumps(
            {
                "processing_hash": content,
                "recorded_hash": processing.provenance.processing_hash,
                "matches": content == processing.provenance.processing_hash,
                "pinned_to": processing.dataset.dataset_hash if processing.dataset else None,
            },
            indent=2,
        )
    )


@app.command("compose")
def compose_cmd(
    manifest_path: Path = typer.Argument(..., help="Path to a validated manifest.yaml."),
    processing_path: Path | None = typer.Option(
        None, "--processing", help="A processing manifest. Omit to use policy defaults."
    ),
    assembly: str | None = typer.Option(
        None, "--assembly", help="Genome, when composing without --processing."
    ),
    annotation: str | None = typer.Option(
        None, "--annotation", help="Registered GTF name, when composing without --processing."
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for .seqforge/ state."
    ),
    outdir: str = typer.Option(
        "results", help="Pipeline output directory (written into the config)."
    ),
) -> None:
    """Compile (dataset, processing) -> config.yaml + units.tsv + a module selection.

    ``--processing`` is optional: a processing manifest exists because someone wanted something
    non-default, and requiring one per dataset would mean 10^4 boilerplate files nobody reads. Either
    way compose writes the fully-resolved, dataset-bound manifest it used to processing.lock.yaml, so
    the run's state is on disk regardless (R7). Exit 3 if a gate fails.
    """
    manifest = _load_manifest(manifest_path)
    report = validate_manifest(manifest)
    if not report.ok:
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2), err=True)
        typer.echo("refusing to compose an invalid manifest", err=True)
        raise typer.Exit(exit_code_for_report(report))

    if processing_path is not None:
        processing = _load_processing(processing_path)
    else:
        if assembly is None or annotation is None:
            # The one thing with no safe default. Deriving an assembly from experiment.organism would
            # mean choosing hg38 vs hg19 vs T2T on the user's behalf — a policy call, and that map is
            # liulab-genome's job (R12). Refuse, but make the refusal actionable (R4).
            typer.echo(
                f"compose needs a genome: this dataset's organism is taxid "
                f"{manifest.experiment.organism.value}. Pass --assembly/--annotation, or author one "
                f"with `seqforge processing new`.",
                err=True,
            )
            raise typer.Exit(2)
        processing, _ = fill_processing(
            spec=load_spec(manifest.library.chemistry.value[0]),
            dataset=manifest,
            processing=ProcessingInputs(assembly=assembly, annotation_name=annotation),
            seqforge_version=__version__,
        )

    p_report = validate_processing(processing, dataset=manifest)
    if not p_report.ok:
        typer.echo(json.dumps(p_report.model_dump(mode="json"), indent=2), err=True)
        typer.echo("refusing to compose with an invalid processing manifest", err=True)
        raise typer.Exit(exit_code_for_report(p_report))

    try:
        result = compose(manifest, processing, workspace=workspace, outdir=outdir)
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


@hook_app.command("pre-tool-use")
def hook_pre_tool_use() -> None:
    """Deny an unbounded FASTQ stream (R3), an absolute path in a manifest (R9), or held-out access.

    Reads the hook payload on stdin, emits a permissionDecision on stdout. Exit 0 always: the decision
    travels in the JSON, and a crashing guard must never wedge the agent.
    """
    from .hooks import pre_tool_use

    payload = _hook_payload()
    denial = pre_tool_use(payload)
    if denial is None:
        raise typer.Exit(0)
    typer.echo(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": denial.message(),
                }
            }
        )
    )


@hook_app.command("post-tool-use")
def hook_post_tool_use() -> None:
    """After any manifest edit, re-run `manifest validate`. The model does not grade its own work (R2)."""
    from .hooks import post_tool_use_targets

    payload = _hook_payload()
    target = post_tool_use_targets(payload)
    if target is None or not Path(target).is_file():
        raise typer.Exit(0)
    try:
        manifest = _load_manifest(Path(target))
        report = validate_manifest(manifest)
    except (FillError, ValidationError, ValueError, OSError) as exc:
        typer.echo(
            json.dumps(
                {
                    "decision": "block",
                    "reason": f"{target} did not parse as a Manifest: {exc}",
                }
            )
        )
        raise typer.Exit(0) from None
    if report.ok:
        typer.echo(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": f"manifest validate: OK ({Path(target).name}).",
                    }
                }
            )
        )
        raise typer.Exit(0)
    codes = [str(getattr(b.code, "value", b.code)) for b in report.blockers]
    typer.echo(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"manifest validate FAILED on {Path(target).name} (exit "
                    f"{exit_code_for_report(report)}): {codes}. Refusal is the contract (R4) — fix "
                    "the manifest; do not proceed as though it validated."
                ),
            }
        )
    )


@hook_app.command("stop")
def hook_stop(
    workspace: Path = typer.Option(Path("."), "-C", "--workspace", help="Root holding .seqforge/."),
) -> None:
    """Refuse to end the turn while questions.md is non-empty — ambiguity routes to a human."""
    from .hooks import stop_decision

    payload = _hook_payload()
    reason = stop_decision(payload, workspace=workspace)
    if reason is None:
        raise typer.Exit(0)
    typer.echo(json.dumps({"decision": "block", "reason": reason}))


@hook_app.command("install")
def hook_install(
    workspace: Path = typer.Option(Path("."), "-C", "--workspace", help="Project root."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing hooks block."),
) -> None:
    """Write the three hooks into .claude/settings.json, merging with whatever is already there."""
    from .hooks import HOOKS_VERSION

    settings_path = Path(workspace) / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings: dict[str, object] = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text() or "{}")
        except json.JSONDecodeError as exc:
            typer.echo(f"{settings_path} is not valid JSON: {exc}", err=True)
            raise typer.Exit(1) from exc
    existing = settings.get("hooks")
    if existing and not force:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "reason": f"{settings_path} already defines hooks; re-run with --force to replace",
                },
                indent=2,
            ),
            err=True,
        )
        raise typer.Exit(2)

    run = "${CLAUDE_PROJECT_DIR}/.claude/hooks/seqforge-hook.sh"
    settings["hooks"] = {
        "PreToolUse": [
            {
                "matcher": "Bash|Write|Edit|NotebookEdit|Read|Grep|Glob",
                "hooks": [{"type": "command", "command": f"{run} pre-tool-use", "args": []}],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Write|Edit|NotebookEdit",
                "hooks": [{"type": "command", "command": f"{run} post-tool-use", "args": []}],
            }
        ],
        "Stop": [{"hooks": [{"type": "command", "command": f"{run} stop", "args": []}]}],
    }
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    shim = Path(workspace) / ".claude" / "hooks" / "seqforge-hook.sh"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "# Generated by `seqforge hook install`. The guard logic lives in seqforge.hooks (typed +\n"
        "# tested); this only routes the event. Fails OPEN by design: a broken hook must not wedge\n"
        "# the agent, and a guard that can hang is worse than the risk it manages.\n"
        "set -uo pipefail\n"
        'cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0\n'
        'exec pixi run -q -- python -m seqforge.cli hook "$@" 2>/dev/null || exit 0\n'
    )
    shim.chmod(0o755)
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "hooks_version": HOOKS_VERSION,
                "settings": str(settings_path),
                "shim": str(shim),
                "events": ["PreToolUse", "PostToolUse", "Stop"],
            },
            indent=2,
        )
    )


@hook_app.command("check")
def hook_check(
    workspace: Path = typer.Option(Path("."), "-C", "--workspace", help="Root holding .seqforge/."),
) -> None:
    """Self-test: prove each guard fires. A guard nobody has watched deny is not a guard.

    A hook that silently never fires is indistinguishable from one that always allows — so this
    exercises every rule against a known-bad payload and reports what it caught.
    """
    from .hooks import HOOKS_VERSION, heldout_roots, pre_tool_use, questions_outstanding

    roots = heldout_roots()
    probe = "/tmp/__seqforge_probe_root__"
    cases = [
        (
            "R3 unbounded FASTQ",
            {"tool_name": "Bash", "tool_input": {"command": "zcat big.fastq.gz | wc -l"}},
            [],
        ),
        (
            "R3 allows a bounded stream",
            {"tool_name": "Bash", "tool_input": {"command": "zcat big.fastq.gz | head -n 400"}},
            [],
        ),
        (
            "R3 allows the seqforge verb",
            {"tool_name": "Bash", "tool_input": {"command": "seqforge probe big.fastq.gz --json"}},
            [],
        ),
        (
            "R9 absolute path in manifest",
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "manifest.yaml",
                    "file_text": "genome: /scratch/ref/hg38.fa\n",
                },
            },
            [],
        ),
        (
            "held-out ad-hoc access",
            {"tool_name": "Bash", "tool_input": {"command": f"ls {probe}/reads"}},
            [probe],
        ),
        (
            "held-out via the sanctioned verb",
            {"tool_name": "Bash", "tool_input": {"command": f"seqforge probe {probe}/a.fastq.gz"}},
            [probe],
        ),
    ]
    results = []
    for name, payload, extra in cases:
        denial = pre_tool_use(payload, roots=extra or roots)
        results.append(
            {"case": name, "denied": denial is not None, "rule": denial.rule if denial else None}
        )
    typer.echo(
        json.dumps(
            {
                "hooks_version": HOOKS_VERSION,
                "heldout_roots_configured": len(roots),
                "open_questions": [str(p) for p in questions_outstanding(workspace)],
                "checks": results,
            },
            indent=2,
        )
    )


def _hook_payload() -> dict[str, object]:
    """Read the hook event from stdin. A malformed payload means NO OPINION, never a crash."""
    import sys

    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


if __name__ == "__main__":  # pragma: no cover
    app()
