"""`seqforge eval` -- the evals harness: measure what unit tests cannot (design/brief S9)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .root import eval_app


@eval_app.command("list")
def eval_list(
    cases_dir: Path | None = typer.Option(
        None, "--cases", help="Case root (default: evals/cases)."
    ),
) -> None:
    """List the eval corpus: id, expected outcome, and whether the case needs an LLM."""
    from ..evals import CaseError, load_cases

    try:
        cases = load_cases(cases_dir)
    except CaseError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    payload = [
        {
            "id": c.id,
            "outcome": c.expected.outcome,
            "needs_llm": c.needs_llm,
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
    from ..evals import CaseError, Grade, load_cases, run_cases
    from ..harvest import ProviderUnavailable, resolve_provider

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
