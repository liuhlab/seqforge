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
REPORT_VERSION = "2026.7.1"

from .collect import collect_report  # noqa: E402
from .render import render_html  # noqa: E402

__all__ = ["REPORT_VERSION", "collect_report", "render_html"]
