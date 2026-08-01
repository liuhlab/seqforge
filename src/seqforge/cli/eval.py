"""`seqforge eval` -- the evals harness: measure what unit tests cannot (a rate, not a snapshot)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .root import eval_app

#: The corpus-wide backstop, in tokens, per case. Measured 2026-07-31: the largest benchmark case
#: other than GSE126954 spends 122 K (in + out) and this clears it by 4x, while GSE126954's 3.47 M
#: is stopped however it is counted. It ships ON rather than off, because the run this exists to
#: stop is an unattended one — a ceiling nobody passes is a number nobody sets. `--ceiling 0`
#: removes it; a genuinely larger dataset raises it.
DEFAULT_EVAL_CEILING = 500_000

#: The report's name inside the run directory. `eval report <directory>` looks for exactly this, so
#: a caller passes the directory a run wrote rather than a filename it has to remember.


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
        help="Override the provider's default model — on DeepSeek that default is "
        "deepseek-v4-flash; pass deepseek-v4-pro to spend for recall. Whichever ran is recorded "
        "in the report's `extractor`, because a baseline does not transfer across models.",
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
    ceiling: int = typer.Option(
        DEFAULT_EVAL_CEILING,
        "--ceiling",
        min=0,
        help="Refuse past N tokens at the model seam PER CASE (raw: cached input and cache writes "
        "count too). A case that reaches it is blocked, not graded, and the run exits 3. "
        "0 = no ceiling.",
    ),
    fail_under: float = typer.Option(
        1.0, "--fail-under", help="Exit 3 if field accuracy drops below this."
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
) -> None:
    """Run the eval corpus and report its metrics.

    `--no-llm` (the default) restricts to deterministic cases, so this runs in a CI with no API key;
    prose cases skip rather than fail. Exit 3 if any false-accept occurs or accuracy drops below
    `--fail-under` — a false accept is never tolerable at any threshold, so it is not on a slider.

    `--llm` inherits the provider's own default model rather than pinning one here — that is
    `deepseek-v4-flash` on the DeepSeek preset, the cheap end of V4, which is what a corpus-scale
    harness should reach for by default. `--model deepseek-v4-pro` buys recall on the hardest prose.
    The report names whichever ran, since the numbers are a claim about that extractor and not
    about the harness.

    `--ceiling` is the token backstop, per case, and it refuses rather than warns: a case that
    reaches it is reported with a `Blocker` instead of a grade and the run exits 3. It costs nothing
    under `--no-llm`, which spends no tokens at all.

    It writes a run directory under `-C`: `report.json`, and one `transcripts/<case>.jsonl` per case
    that reached a model. **Stdout is unchanged in shape** and gains the paths, not the contents — a
    thousand-exchange transcript cannot ride on a stream that IS the result object. `seqforge eval
    report <that directory>` renders it.
    """
    from ..evals import CaseError, Grade, load_cases, run_cases
    from ..harvest import ProviderUnavailable, resolve_provider
    from ..workspace import EVAL_REPORT_FILENAME, eval_dir

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
        cases,
        llm=llm,
        provider=llm_provider,
        model=model,
        trials=trials,
        jobs=jobs,
        ceiling=ceiling,
        workspace=workspace,
    )
    rendered = json.dumps(report.model_dump(mode="json"), indent=2)
    # The same bytes to both, which is the point of the file: CI used to `tee` stdout into a path it
    # invented, so the artifact existed only as long as the shell line that made it. The verb owns
    # its directory now, and stdout is still exactly the result object.
    run_dir = eval_dir(workspace)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / EVAL_REPORT_FILENAME).write_text(rendered, encoding="utf-8")
    typer.echo(rendered)

    # Before the accuracy gate, because a blocked case was not graded at all: reporting "accuracy
    # 1.000" over the cases that did finish, and only then mentioning the one that was cut off, gets
    # the order of the two facts backwards.
    blocked = [r for r in runs if r.blocker is not None]
    if blocked:
        typer.echo(
            f"TOKEN CEILING reached in {len(blocked)} case(s): {[r.case_id for r in blocked]} — "
            f"each stopped at --ceiling {ceiling:,} raw tokens and was not graded. Raise it, or "
            f"read why the case costs that much before you do",
            err=True,
        )
        raise typer.Exit(3)

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
        ...,
        help="A run directory from `seqforge eval run`, or its report.json directly "
        "(use - to read it from stdin).",
    ),
    output: Path | None = typer.Option(
        None, "-o", "--output", help="HTML file to write (default: the input with a .html suffix)."
    ),
    title: str = typer.Option("seqforge eval report", "--title", help="Heading for the page."),
    source: str | None = typer.Option(
        None, "--source", help="The command that produced the report; rendered as its provenance."
    ),
    transcript: str = typer.Option(
        "sample",
        "--transcript",
        help="How much of the transcript to render: sample (a representative selection per case, "
        "the default), all (every exchange), none. Needs the run DIRECTORY — the exchanges live in "
        "files beside the report, never in it.",
    ),
    timestamp: bool = typer.Option(
        True,
        "--timestamp/--no-timestamp",
        help="Stamp the render time into the footer (omit for byte-reproducible output).",
    ),
) -> None:
    """Render an `eval run` report as one self-contained HTML page; print a JSON summary.

    A *consumer* of `eval run`'s output, never a second output mode for it: ADR-0013 makes machine
    JSON the contract there and forbids a `--json` switch, so the human-readable artifact is a
    separate verb and the stream keeps its shape.

        seqforge eval run --no-llm --cases evals/benchmark -C report
        seqforge eval report report/seqforge/eval -o report/benchmark.html

    The argument is the **directory** `eval run` wrote, which is where the transcripts are too. A
    path to a JSON file still works, and so does `-`: a report that arrived over a pipe is still a
    report, it just has no transcripts beside it.

    `--transcript` says how much of the transcript to render. The default `sample` is a
    representative selection per case — one exchange per document scope, plus every exchange that
    produced a rejected draft or a graded assertion — and the page states how many it left out, since
    a silently truncated transcript reads as a complete one. `all` renders every exchange (a
    corpus-scale run makes a large page); `none` renders none.

    Exit is 0 on a successful render whatever the report said — the same rule `seqforge report`
    follows. The verdict belongs to `eval run`, which already exits 3 on any false accept; smuggling
    it into the renderer too would make a CI step that renders a red report fail for rendering it.
    """
    import sys
    from datetime import datetime

    from ..evals.report import TRANSCRIPT_MODES, attach_transcripts, render_html
    from ..workspace import EVAL_REPORT_FILENAME, eval_dir

    if transcript not in TRANSCRIPT_MODES:
        typer.echo(f"--transcript must be one of {'|'.join(TRANSCRIPT_MODES)}", err=True)
        raise typer.Exit(2)

    source_path = report
    if report.is_dir():
        # A run directory, or the workspace one lives under. Accepting both means a caller never has
        # to spell `seqforge/eval` — which is the whole point of one module owning that name, and the
        # CI workflow was the drift waiting to happen.
        run_dir = report if (report / EVAL_REPORT_FILENAME).is_file() else eval_dir(report)
        source_path = run_dir / EVAL_REPORT_FILENAME
    if str(report) == "-":
        if output is None:
            typer.echo("reading from stdin needs an explicit -o/--output", err=True)
            raise typer.Exit(2)
        raw = sys.stdin.read()
    else:
        try:
            raw = source_path.read_text(encoding="utf-8")
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

    # The exchanges are files beside the report, because stdout is the result object and a thousand
    # of them cannot ride on it. They are folded in HERE, once, so the renderer stays a pure
    # function of one dict. A report read from stdin has no directory beside it and gets none.
    payload = attach_transcripts(
        payload, None if str(report) == "-" else source_path.parent, mode=transcript
    )

    # Beside the report it renders, not `<dir>.html` next to the directory: a run directory is a
    # thing you hand somebody, and the page belongs inside it.
    out = output if output is not None else source_path.with_suffix(".html")
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
                # The subset the corpus itself is missing. Counted apart from the skips because it
                # is the one that is an instruction: publish the package.
                "absent": sum(1 for c in cases if c.get("skip_kind") == "absent"),
                # Named, not counted: the whole point of the page is that a false accept is a
                # verdict with the cases attached, and the summary line says the same thing.
                "false_accepts": false_accepts,
            },
            indent=2,
        )
    )
