"""``seqforge report`` — render a compile, a study or both as one HTML page and its data.

A deterministic reader: it opens the ``seqforge/`` artifacts already on disk (manifest, samples,
evidence matrix, composed pipeline) and writes a single offline HTML file that makes the whole decision
legible at a glance, with ``report.json`` beside it holding the same data for a program. It touches no FASTQ, no network, and no LLM, and it decides nothing — the verdict
it shows was decided upstream by ``resolve``/``validate``/``compose``.

Exit is ``0`` on a successful render regardless of what the dataset's verdict was: the reader's job is
to render, and the dataset's state (compiled / blocked / needs-a-human) is carried *in* the page and in
the stdout summary, not smuggled into this verb's exit code. Only a usage or I/O failure exits nonzero.
That holds for the **run** state too — a run that failed is rendered and reported, and rendering it
succeeded — which is what keeps "I could not render" distinguishable from "the run I rendered did not
finish". A caller that wants to gate on the run reads the field.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import typer

from ..report import collect_report, write_report
from ..workspace import REPORT_HTML, report_html_path
from ._common import _open_study
from .root import app


@app.command("report")
def report_cmd(
    workspace: Path | None = typer.Option(
        None,
        "-C",
        "--workspace",
        help="Root holding the seqforge/ state to report on. Default: the current directory, "
        "unless --study is given alone.",
    ),
    study: Path | None = typer.Option(
        None,
        "--study",
        help="A study folder to show on the Study tab: its bounds as a graph of the records kept "
        "and left out. Given alone, the report needs no compile and is written into the folder.",
    ),
    output: Path | None = typer.Option(
        None,
        "-o",
        "--output",
        help="Where to write the HTML (default: seqforge/report.html under the workspace, or "
        "report.html in the study folder when no workspace is given). The data file goes beside "
        "it, under the same name with .json.",
    ),
    timestamp: bool = typer.Option(
        True,
        "--timestamp/--no-timestamp",
        help="Stamp the render time into the footer (omit for byte-reproducible output).",
    ),
    results: Path | None = typer.Option(
        None,
        "--results",
        help="Where the finished pipeline's per-sample outputs are. Default: the composed config's "
        "`outdir`, under the pipeline dir — which is where a pipeline started there put them. This "
        "is the escape hatch for a pipeline run with `snakemake --directory`; a relative value is "
        "joined onto the pipeline dir, so pass an absolute path if it lives elsewhere.",
    ),
) -> None:
    """Write one self-contained HTML report of a compile, a study or both, and its data; print a summary.

    ``--study`` adds the Study tab, read from the study folder through its format checks: a folder
    the checks refuse exits 2 before anything is written. Given without ``-C``, the report is the
    study's alone, needs no compile, and is written into the study folder.

    ``report.json`` is written beside the page and is what the page is built from: the same data, for
    a program, with each path in it relative to where the report was written — each ``.h5ad`` the
    pipeline left included, so a caller opens the matrices without knowing the workspace's layout.

    The page inlines every asset it has — one stylesheet and one script — so it opens on a
    double-click with no network. There is no diagram or charting engine to inline: the flow is plain
    HTML cards and the knee plots are hand-built inline SVG, which is what keeps a rendered page in
    the tens of KB. Missing artifacts degrade gracefully — the chemistry decision lives in the
    manifest, so the page always renders — and every richer panel appears iff its artifact is found.
    That includes the pipeline's own results: once the composed Snakefile has run, its per-sample QC
    artifacts are joined in as well, and a partial pipeline reports what landed.

    ``--results`` is a machine fact, like ``--fastq-dir`` and ``--sif-dir``: it says where this
    machine put the outputs, and it cannot change anything the page says the compiler decided.
    """
    opened = _open_study(study)
    if workspace is None and opened is None:
        workspace = Path()
    # The folder the report starts from is where it writes: the workspace's seqforge/, or the study
    # folder when it is read alone. A study read beside a workspace is read where it is.
    if output is not None:
        out = output
    elif workspace is not None:
        out = report_html_path(workspace)
    else:
        assert opened is not None
        out = opened.path / REPORT_HTML
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M") if timestamp else None
    try:
        report = collect_report(
            workspace,
            study=opened,
            generated_at=generated_at,
            results_dir=results,
            report_dir=out.parent,
        )
    except FileNotFoundError as exc:
        typer.echo(
            json.dumps({"error": "nothing_to_report", "detail": str(exc)}, indent=2), err=True
        )
        raise typer.Exit(1) from exc

    written = write_report(report, out)

    payload = {
        "report": str(written.html),
        "json": str(written.json),
        "bytes": written.html_bytes,
        "assays": len(report.assays),
        "study": None if report.study is None else report.study.study,
        "conclusion": [
            {
                "assay": a.label,
                "kind": a.conclusion.kind,
                "exit": a.conclusion.exit_code,
                # How the PIPELINE went, beside how the COMPILE went and never merged into it — two
                # judgements, two envelopes. `null` is "no pipeline output was found", which for a
                # workspace that was only ever compiled is the correct answer and not a failure.
                "pipeline_stats": None
                if a.pipeline_stats is None
                else {
                    "module": a.pipeline_stats.module,
                    # Two values and no third, so a caller gates on a string compare rather than on
                    # a count it has to interpret. It is NOT this verb's exit code and never will
                    # be: rendering succeeded, which is what an exit code from a reader reports.
                    "run_state": a.pipeline_stats.state,
                    "samples_finished": a.pipeline_stats.n_found,
                    "samples_expected": a.pipeline_stats.n_expected,
                    # What was demanded and not produced, so the caller that gated on the state above
                    # knows what to re-run without opening the page.
                    "missing_deliverables": a.pipeline_stats.missing_deliverables,
                },
                # Which decision the pipeline's own numbers make look wrong, beside the numbers and
                # never folded into the exit code above: an alert is advisory, and a finding that
                # exists only inside an HTML document is not machine-accessible. Dumped whole rather
                # than summarised — the stable `id` is what a consumer suppresses or tracks a
                # recurring alert by, and the rest is what it would otherwise have to parse out of
                # the page. An empty list is the healthy answer, and the common one.
                "alerts": [alert.model_dump(mode="json") for alert in a.alerts],
            }
            for a in report.assays
        ],
    }
    typer.echo(json.dumps(payload, indent=2))
