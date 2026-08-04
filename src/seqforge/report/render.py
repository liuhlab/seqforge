"""Assemble one :class:`ProjectReport` into a single self-contained HTML string.

The shell: a sticky header with the verdict, the tab bar, one assay section per assay, a footer, and —
inlined at the end — the page's stylesheets and its JS, and nothing else. All are read from the
package via ``importlib.resources`` and embedded, so the output makes zero network requests and opens
on a double-click. No templating engine: the fragments come from ``panels.py`` and are concatenated
here.

Two stylesheets go in, in a deliberate order (see ``_STYLESHEETS``): the vendored Tailwind build
first, the hand-written sheet second. Tailwind emits everything inside real cascade layers and
unlayered CSS outranks every layer whatever the source order, so the hand-written sheet already wins
every overlap on the strength of the cascade alone; putting it second means it also wins on source
order, which is what decides the small unlayered remainder Tailwind emits (``@property``
registrations today, keyframes tomorrow). Two arguments agreeing beats one of them silently
mattering.

**No third-party runtime executes in the page**, and the two things that would have needed one are
hand-built instead: the Flow tab is plain HTML cards, and the Results tab's knee plots are inline SVG
(``panels._knee_figure``). The Flow tab did inline a ~2.5 MB Mermaid bundle, and it was dropped
because a scaled SVG cannot reflow — its text shrank to nothing on a wide dataset — which took a
rendered page from ~2.6 MB to a few tens of KB. A charting library would cost the same order again
and arrive, as they do, as a CDN ``<script src>`` that quietly makes an "offline" page need the
network. ``report/assets/VENDOR.md`` is the long form, including what the Tailwind build is and how
to rebuild it.
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

#: The page's stylesheets, in cascade order — vendored build first, hand-written sheet last, so the
#: unlayered sheet wins on source order as well as on layer rank. Each becomes its own ``<style>``:
#: an ``@layer`` statement has to precede the other rules of ITS stylesheet, and one element per
#: sheet keeps that true without depending on how they were concatenated.
_STYLESHEETS = ("report.tw.css", "report.css")


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
    styles = "\n".join(f"<style>{_asset(name)}</style>" for name in _STYLESHEETS)
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
        f"{styles}\n"
        "</head>\n<body>\n"
        f"{header}\n{tab_bar(report)}\n"
        f"<main>{sections}</main>\n"
        f"{footer}\n"
        f"<script>{report_js}</script>\n"
        "</body>\n</html>\n"
    )


__all__ = ["render_html"]
