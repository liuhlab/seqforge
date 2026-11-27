"""Assemble one :class:`ProjectReport` into a single self-contained HTML string.

The shell: one sticky band holding the header and the tab bar, one assay section per assay, a footer,
and — inlined at the end — the page's stylesheet and its JS, and nothing else. Both are read from the
package via ``importlib.resources`` and embedded, so the output makes zero network requests and opens
on a double-click. No templating engine: the fragments come from ``panels.py`` and are concatenated
here.

The header and the tab bar stick as **one** element rather than as two siblings, because two sticky
siblings need the second to know the first's rendered height — the stylesheet carried ``top: 56px``,
a number no code could check and every change to the header's contents could falsify. One band has
no such number in it. Four elements and only four carry ``sf-page``, the one reading column: the
header row, the tab row, ``<main>`` and the footer.

**One stylesheet** (``_STYLESHEETS``), and the tuple stays a tuple because an ``@layer`` statement
has to precede the other rules of *its* stylesheet — one ``<style>`` element per sheet keeps that
true however many there are. There were two: a 559-line hand-written sheet was inlined beside the
vendored build while the page moved onto it a tab at a time, and it won every overlap on the
strength of being unlayered. It has no callers left, so it is gone; an expand–contract that never
contracts is two systems.

**No third-party runtime executes in the page**, and the two things that would have needed one are
hand-built instead: the Flow tab is plain HTML cards, and the Results tab's knee plots are inline SVG
(``panels._knee_figure``). The Flow tab did inline a ~2.5 MB Mermaid bundle, and it was dropped
because a scaled SVG cannot reflow — its text shrank to nothing on a wide dataset — which took a
rendered page from ~2.6 MB to a few tens of KB. A charting library would cost the same order again
and arrive, as they do, as a CDN ``<script src>`` that quietly makes an "offline" page need the
network. ``report/assets/VENDOR.md`` is the long form, including what the Tailwind build is and how
to rebuild it.

**One set of data, two files.** :func:`write_report` writes ``report.json`` first, parses the page's
model back out of that text, renders the page from what it parsed, and embeds the same data in the
page as an ``application/json`` block. A person reads the page, a program reads the JSON or the
block, and none of the three can say something the others do not — a field the JSON could not carry
would be missing from the page too, rather than shown to a person and withheld from a program.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import NamedTuple

from .model import ProjectReport
from .panels import assay_section, assay_switcher, esc, tab_bar

_VERDICT_LABEL = {
    "compiled": "Compiled",
    "ir_ready": "Manifest ready",
    "blocker": "Blocked",
    "question": "Needs a human",
}

#: The page's stylesheets. One, now that the hand-written sheet has no callers — but each still
#: becomes its own ``<style>``: an ``@layer`` statement has to precede the other rules of ITS
#: stylesheet, and one element per sheet keeps that true without depending on how they were
#: concatenated.
_STYLESHEETS = ("report.tw.css",)


def _asset(name: str) -> str:
    """Read a packaged asset (``report/assets/<name>``) as text."""
    return (files(__package__) / "assets" / name).read_text(encoding="utf-8")


def _script_guard(text: str) -> str:
    r"""Neutralise any ``</script`` in embedded JS so it can't end the inlining ``<script>`` early.

    ``<\/script`` is byte-equivalent to ``</script`` in every JS string/regex context, so this never
    changes behaviour; it only guarantees the browser's tokenizer keeps reading.
    """
    return text.replace("</script", "<\\/script")


def _data_block(report: ProjectReport) -> str:
    r"""The report's data as the page embeds it: one ``application/json`` script, parseable as-is.

    Every ``<`` becomes ``\u003c``. A ``<`` can only occur inside a JSON string, where the escape
    parses back to the same character, and without one no ``</script>`` in a quote or a file name can
    end the block early.
    """
    data = report.model_dump_json().replace("<", "\\u003c")
    return f'<script type="application/json" id="seqforge-report-data">{data}</script>'


def _workspace_names(report: ProjectReport) -> str:
    """What the page is a report on, for its title and header: each workspace's name."""
    return ", ".join(ws.name for ws in report.workspaces) or "report"


def _project_verdict(report: ProjectReport) -> tuple[str, str]:
    """The header pill for the whole project: the most severe assay outcome, and its label."""
    kinds = {a.conclusion.kind for a in report.assays}
    for kind in ("blocker", "question", "ir_ready", "compiled"):
        if kind in kinds:
            return kind, _VERDICT_LABEL[kind]
    return "ir_ready", _VERDICT_LABEL["ir_ready"]


def render_html(report: ProjectReport) -> str:
    """Render ``report`` to one complete, self-contained HTML document."""
    styles = "\n".join(f"<style>{_asset(name)}</style>" for name in _STYLESHEETS)
    report_js = _script_guard(_asset("report.js"))

    verdict_kind, verdict_label = _project_verdict(report)
    sections = "".join(assay_section(a, i) for i, a in enumerate(report.assays))

    ts = f" · {esc(report.generated_at)}" if report.generated_at else ""
    names = _workspace_names(report)
    viewed = ", ".join(
        f'<span class="font-mono">{esc(ws.name)}/seqforge/</span>' for ws in report.workspaces
    )
    footer = (
        '<footer class="sf-page mt-6 border-t border-line py-6 text-xs text-dim">'
        f"seqforge report v{esc(report.report_version)}{ts} · "
        f"a deterministic view of {viewed}. "
        "The manifest and YAML hold the exhaustive detail; this page is the glance layer."
        "</footer>"
    )

    header = (
        '<header class="border-b border-line">'
        '<div class="sf-page flex flex-wrap items-center gap-3 py-3">'
        '<span class="text-base font-bold tracking-tight">seqforge'
        '<span class="text-accent"> ⚡ </span>report</span>'
        f'<span class="min-w-0 truncate font-mono text-sm text-dim">{esc(names)}</span>'
        '<span class="flex-1"></span>'
        f"{assay_switcher(report)}"
        f'<span class="sf-verdict sf-v-{esc(verdict_kind)}">'
        f'<span class="size-2 rounded-full bg-current"></span>{esc(verdict_label)}</span>'
        '<button id="theme-toggle" class="sf-icon-btn" title="Toggle light / dark" aria-label="Toggle theme">☽</button>'
        "</div></header>"
    )

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>seqforge report — {esc(names)}</title>\n"
        f"{styles}\n"
        "</head>\n<body>\n"
        # `data-sticky-band` is the only way anything can ask how tall this is. The band exists as ONE
        # element precisely so no number for its height is ever written down; a script that scrolls
        # something to just below it therefore has to measure, and this is what it measures.
        '<div data-sticky-band class="sticky top-0 z-30 border-b border-line bg-surface/90 '
        f'backdrop-blur-sm backdrop-saturate-150">{header}\n{tab_bar(report)}</div>\n'
        f'<main class="sf-page pt-6 pb-10">{sections}</main>\n'
        f"{footer}\n"
        f"{_data_block(report)}\n"
        f"<script>{report_js}</script>\n"
        "</body>\n</html>\n"
    )


class ReportFiles(NamedTuple):
    """The two files one report is: the page, the data beside it, and the page's size in bytes."""

    html: Path
    json: Path
    html_bytes: int


def report_json_path(html_path: Path) -> Path:
    """Where the data file of the page at ``html_path`` goes: beside it, under the same stem.

    ``seqforge/report.html`` gets ``seqforge/report.json``. A page that ``-o`` moved takes its data
    with it, and two pages written into one directory under two names do not share one data file.
    """
    return html_path.with_suffix(".json")


def write_report(report: ProjectReport, html_path: Path) -> ReportFiles:
    """Write ``report.json``, then the page built from it, beside each other.

    The page is rendered from the model parsed back out of the JSON text, not from ``report``, so
    the page can only show what the file carries.
    """
    json_path = report_json_path(html_path)
    text = report.model_dump_json(indent=2) + "\n"
    html = render_html(ProjectReport.model_validate_json(text))
    html_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(text, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    return ReportFiles(html=html_path, json=json_path, html_bytes=len(html.encode("utf-8")))


__all__ = ["ReportFiles", "render_html", "report_json_path", "write_report"]
