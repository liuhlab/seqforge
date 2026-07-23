"""Assemble one :class:`ProjectReport` into a single self-contained HTML string.

The shell: a sticky header with the verdict, the tab bar, one assay section per assay, a footer, and —
inlined at the end — the vendored Mermaid bundle plus the report's own CSS/JS. Every asset is read from
the package via ``importlib.resources`` and embedded, so the output makes zero network requests and
opens on a double-click. No templating engine: the fragments come from ``panels.py`` and are
concatenated here.
"""

from __future__ import annotations

from importlib.resources import files

from .model import ProjectReport
from .panels import assay_section, assay_switcher, esc, tab_bar

_VERDICT_LABEL = {
    "compiled": "Compiled",
    "ir_ready": "Manifest ready",
    "blocker": "Blocked",
    "question": "Needs a human",
}


def _asset(name: str) -> str:
    """Read a packaged asset (``report/assets/<name>``) as text."""
    return (files(__package__) / "assets" / name).read_text(encoding="utf-8")


def _script_guard(text: str) -> str:
    """Neutralise any ``</script`` in embedded JS so it can't close the inlining ``<script>`` early.

    ``<\\/script`` is byte-equivalent to ``</script`` in every JS string/regex context, so this never
    changes behaviour; it only guarantees the browser's tokenizer keeps reading.
    """
    return text.replace("</script", "<\\/script")


def _project_verdict(report: ProjectReport) -> tuple[str, str]:
    """The header pill for the whole project: the most severe assay outcome, and its label."""
    kinds = {a.conclusion.kind for a in report.assays}
    for kind in ("blocker", "question", "ir_ready", "compiled"):
        if kind in kinds:
            return kind, _VERDICT_LABEL[kind]
    return "ir_ready", _VERDICT_LABEL["ir_ready"]


def render_html(report: ProjectReport) -> str:
    """Render ``report`` to one complete, self-contained HTML document."""
    css = _asset("report.css")
    mermaid_js = _script_guard(_asset("mermaid.min.js"))
    report_js = _script_guard(_asset("report.js"))

    verdict_kind, verdict_label = _project_verdict(report)
    sections = "".join(assay_section(a, i) for i, a in enumerate(report.assays))

    ts = f" · {esc(report.generated_at)}" if report.generated_at else ""
    footer = (
        '<footer class="foot">'
        f"seqforge report v{esc(report.report_version)}{ts} · "
        f'a deterministic view of <span class="mono">{esc(report.workspace_name)}/seqforge/</span>. '
        "The manifest and YAML hold the exhaustive detail; this page is the glance layer."
        "</footer>"
    )

    header = (
        '<header class="top"><div class="top-row">'
        '<span class="brand">seqforge<span class="spark"> ⚡ </span>report</span>'
        f'<span class="title-dim mono">{esc(report.workspace_name)}</span>'
        '<span class="top-spacer"></span>'
        f"{assay_switcher(report)}"
        f'<span class="verdict {esc(verdict_kind)}"><span class="dot"></span>{esc(verdict_label)}</span>'
        '<button id="theme-toggle" class="icon-btn" title="Toggle light / dark" aria-label="Toggle theme">☽</button>'
        "</div></header>"
    )

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>seqforge report — {esc(report.workspace_name)}</title>\n"
        f"<style>{css}</style>\n"
        "</head>\n<body>\n"
        f"{header}\n{tab_bar()}\n"
        f"<main>{sections}</main>\n"
        f"{footer}\n"
        f"<script>{mermaid_js}</script>\n"
        f"<script>{report_js}</script>\n"
        "</body>\n</html>\n"
    )


__all__ = ["render_html"]
