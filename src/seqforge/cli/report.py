"""``seqforge report`` — render a workspace's compile as one self-contained HTML page.

A deterministic reader: it opens the ``seqforge/`` artifacts already on disk (manifest, samples,
evidence matrix, composed pipeline) and writes a single offline HTML file that makes the whole decision
legible at a glance. It touches no FASTQ, no network, and no LLM, and it decides nothing — the verdict
it shows was decided upstream by ``resolve``/``validate``/``compose``.

Exit is ``0`` on a successful render regardless of what the dataset's verdict was: the reader's job is
to render, and the dataset's state (compiled / blocked / needs-a-human) is carried *in* the page and in
the stdout summary, not smuggled into this verb's exit code. Only a usage or I/O failure exits nonzero.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import typer

from ..report import collect_report, render_html
from ..workspace import report_html_path
from .root import app


@app.command("report")
def report_cmd(
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root holding the seqforge/ state to report on."
    ),
    output: Path | None = typer.Option(
        None,
        "-o",
        "--output",
        help="Where to write the HTML (default: seqforge/report.html under the workspace).",
    ),
    timestamp: bool = typer.Option(
        True,
        "--timestamp/--no-timestamp",
        help="Stamp the render time into the footer (omit for byte-reproducible output).",
    ),
) -> None:
    """Write one self-contained HTML report of the workspace's compile; print a JSON summary.

    The page inlines every asset (styles, script, the Mermaid flow-chart engine) so it opens on a
    double-click with no network. Missing artifacts degrade gracefully — the chemistry decision lives
    in the manifest, so the page always renders — and every richer panel appears iff its artifact is
    found.
    """
    out = output if output is not None else report_html_path(workspace)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M") if timestamp else None
    try:
        report = collect_report(workspace, generated_at=generated_at)
    except FileNotFoundError as exc:
        typer.echo(
            json.dumps({"error": "nothing_to_report", "detail": str(exc)}, indent=2), err=True
        )
        raise typer.Exit(1) from exc

    html = render_html(report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    payload = {
        "report": str(out),
        "bytes": len(html.encode("utf-8")),
        "assays": len(report.assays),
        "conclusion": [
            {"assay": a.label, "kind": a.conclusion.kind, "exit": a.conclusion.exit_code}
            for a in report.assays
        ],
    }
    typer.echo(json.dumps(payload, indent=2))
