"""``report`` — a deterministic reader that renders one workspace as one self-contained HTML page.

``seqforge report`` answers "what did the compiler decide, and how?" at a glance: the dataset, the
chemistry call and the bytes behind it, the per-sample metadata with its provenance, and the composed
pipeline — one page, every asset inlined, opens offline. It is a *view* over artifacts already on disk
(``collect.py`` -> :class:`~seqforge.report.model.ProjectReport` -> ``render.py`` -> HTML); it decides
nothing and writes only the report. Missing pieces degrade rather than fail — the chemistry decision
lives in the manifest, so the page always renders, and every richer panel appears iff its artifact is
found.

The renderer is deterministic today, but nothing here forbids a later LLM-written summary: it would
slot into ``panels.py`` as one more fragment without changing the shell.
"""

from __future__ import annotations

#: CalVer YYYY.M.PATCH; bumped when the report's layout or projection changes. Not folded into any
#: content-addressed cache key — the report is a rebuildable view, not an input to anything.
#: 2026.8.0 — a **Results** tab: a completeness strip for the pipeline itself (distinct from the
#: header's compile verdict — "we produced a Snakefile" and "that Snakefile finished" are two facts),
#: a General-Statistics table tinted by each metric's own verdict, a per-sample headline strip, and
#: hand-built inline-SVG knee plots. The tab is omitted from the strip entirely when no assay has
#: results, so a compiled-but-not-yet-run workspace renders exactly the page it rendered before.
REPORT_VERSION = "2026.8.0"

from .collect import collect_report  # noqa: E402
from .render import render_html  # noqa: E402

__all__ = ["REPORT_VERSION", "collect_report", "render_html"]
