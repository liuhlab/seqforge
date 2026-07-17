"""`seqforge processing` -- the PROCESSING manifest: what to DO with a dataset. Many per dataset."""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml
from pydantic import ValidationError

from .. import __version__
from ..kb import load_spec
from ..manifest import (
    Instruction,
    PolicyError,
    ProcessingInputs,
    exit_code_for_report,
    fill_processing,
    instructions_from_assertions,
    processing_content_hash,
    validate_processing,
)
from ..models.assertion import Assertion
from ._common import _load_manifest, _load_processing, _parse_quantify
from .root import processing_app


def _instructions_from(path: Path | None) -> list[Instruction]:
    """Rebuild the instructable surface from `harvest extract`'s artifact.

    The precedence ladder (§7) is flag > instruction > policy, and `resolve_processing` has always
    implemented it — its `PolicyError` even tells you to "name an assembly in an --instruction
    document". That branch was unreachable: `--assembly` was a REQUIRED option, and nothing passed
    `instructions=` from any production caller. This is the last mile of a join that already existed.

    Note what is NOT happening: the model does not decide anything here. It found a claim in prose and
    code verified the quote greps back and entails the value; this reads that record and applies
    precedence. "We can accept instructions because we never trust the model to act on them, only to
    find them."
    """
    if path is None:
        return []
    payload = json.loads(path.read_text())
    if isinstance(payload, list):
        raise ValueError(
            "this looks like a pre-2026.7 assertions.json (a bare list). It cannot say which "
            "documents were --instruction, and only those may set processing.*. Re-run "
            "`seqforge harvest extract`."
        )
    docs = frozenset(payload.get("instruction_docs", ()))
    parsed = [Assertion.model_validate(a) for a in payload.get("assertions", ())]
    instructions, conflicts = instructions_from_assertions(parsed, instruction_docs=docs)
    if conflicts:
        raise ValueError(
            f"{len(conflicts)} instruction(s) disagree with each other; only their author can "
            f"settle that: " + "; ".join(c.field for c in conflicts)
        )
    return instructions


@processing_app.command("new")
def processing_new(
    dataset_path: Path = typer.Argument(..., help="Path to the dataset manifest.yaml."),
    assembly: str | None = typer.Option(
        None, "--assembly", help="liulab-genome UCSC assembly id (e.g. ce11)."
    ),
    annotation: str | None = typer.Option(
        None, "--annotation", help="Registered GTF name (e.g. WS298)."
    ),
    assertions: Path | None = typer.Option(
        None,
        "--assertions",
        help="Span-verified assertions from `harvest extract` (seqforge/assertions.json). "
        "Instructions in them fill what no flag supplied.",
    ),
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
    """Author a PROCESSING manifest: what to DO with a dataset. Many per dataset.

    With no flags you get the policy default, which counts every soloFeature — so the common
    case needs no decision from you. --quantify replaces that list exactly; narrowing it warns,
    because dropping a feature is the only irreversible act here.
    """
    dataset = _load_manifest(dataset_path)
    spec = load_spec(dataset.library.chemistry.value[0])
    try:
        instructions = _instructions_from(assertions)
    except (OSError, ValueError, ValidationError) as exc:
        typer.echo(f"{assertions}: {exc}", err=True)
        raise typer.Exit(2) from exc
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
            instructions=instructions,
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


@processing_app.command("validate")
def processing_validate(
    processing_path: Path = typer.Argument(..., help="Path to a processing.yaml."),
    dataset_path: Path | None = typer.Option(
        None, "--dataset", help="Cross-check against this dataset manifest (pin + organism)."
    ),
) -> None:
    """Validate a processing manifest. Exit 3 on a Blocker."""
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
