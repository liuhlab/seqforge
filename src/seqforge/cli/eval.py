"""`seqforge eval` -- the evals harness: measure what unit tests cannot (a rate, not a snapshot)."""

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
        None,
        "--model",
        help="Override the provider's default model. A baseline is model-scoped: the recorded "
        "numbers were measured on deepseek-v4-pro, not on the deepseek-v4-flash default.",
    ),
    trials: int = typer.Option(
        1, "--trials", min=1, help="Re-run each prose case N times; extraction is nondeterministic."
    ),
    jobs: int | None = typer.Option(
        None,
        "--jobs",
        "-j",
        min=1,
        help="Cases to run at once. Default: usable cores (CPU affinity aware), capped at 24. "
        "1 forces sequential.",
    ),
    fail_under: float = typer.Option(
        1.0, "--fail-under", help="Exit 3 if field accuracy drops below this."
    ),
) -> None:
    """Run the eval corpus and report its metrics.

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

    report, runs = run_cases(
        cases, llm=llm, provider=llm_provider, model=model, trials=trials, jobs=jobs
    )
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


@eval_app.command("report")
def eval_report(
    report: Path = typer.Argument(
        ..., help="JSON from `seqforge eval run` (use - to read it from stdin)."
    ),
    output: Path | None = typer.Option(
        None, "-o", "--output", help="HTML file to write (default: the input with a .html suffix)."
    ),
    title: str = typer.Option("seqforge eval report", "--title", help="Heading for the page."),
    source: str | None = typer.Option(
        None, "--source", help="The command that produced the report; rendered as its provenance."
    ),
    timestamp: bool = typer.Option(
        True,
        "--timestamp/--no-timestamp",
        help="Stamp the render time into the footer (omit for byte-reproducible output).",
    ),
) -> None:
    """Render an `eval run` report as one self-contained HTML page; print a JSON summary.

    A *consumer* of `eval run`'s stdout, never a second output mode for it: ADR-0013 makes machine
    JSON the contract there and forbids a `--json` switch, so the human-readable artifact is one pipe
    away and the stream keeps its shape.

        seqforge eval run --no-llm --cases evals/benchmark > report.json
        seqforge eval report report.json -o report.html

    Exit is 0 on a successful render whatever the report said — the same rule `seqforge report`
    follows. The verdict belongs to `eval run`, which already exits 3 on any false accept; smuggling
    it into the renderer too would make a CI step that renders a red report fail for rendering it.
    """
    import sys
    from datetime import datetime

    from ..evals.report import render_html

    if str(report) == "-":
        if output is None:
            typer.echo("reading from stdin needs an explicit -o/--output", err=True)
            raise typer.Exit(2)
        raw = sys.stdin.read()
    else:
        try:
            raw = report.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(json.dumps({"error": "unreadable", "detail": str(exc)}, indent=2), err=True)
            raise typer.Exit(1) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(json.dumps({"error": "not_json", "detail": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc
    if not isinstance(payload, dict):
        typer.echo(json.dumps({"error": "not_an_eval_report"}, indent=2), err=True)
        raise typer.Exit(1)

    out = output if output is not None else report.with_suffix(".html")
    html = render_html(
        payload,
        title=title,
        source=source,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M") if timestamp else None,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    cases = payload.get("per_case", [])
    false_accepts = [c["case"] for c in cases if c.get("grade") == "false_accept"]
    typer.echo(
        json.dumps(
            {
                "report": str(out),
                "bytes": len(html.encode("utf-8")),
                "cases": len(cases),
                "skipped": sum(1 for c in cases if c.get("skipped")),
                # Named, not counted: the whole point of the page is that a false accept is a
                # verdict with the cases attached, and the summary line says the same thing.
                "false_accepts": false_accepts,
            },
            indent=2,
        )
    )
