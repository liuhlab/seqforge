"""``report`` — a deterministic reader that renders a workspace, a study folder or both as one HTML page.

``seqforge report`` answers "what did the compiler decide, and how?" at a glance: the dataset, the
chemistry call and the bytes behind it, the per-sample metadata with its provenance, and the composed
pipeline — one page, every asset inlined, opens offline. It is a *view* over artifacts already on disk
(``collect.py`` -> :class:`~seqforge.report.model.ProjectReport` -> ``report.json`` -> ``render.py``
-> HTML); it decides nothing and writes only its two files, the page and the data it was built from. Missing pieces degrade rather than fail — the chemistry decision
lives in the manifest, so the page always renders, and every richer panel appears iff its artifact is
found.

The renderer is deterministic today, but nothing here forbids a later LLM-written summary: it would
slot into ``panels.py`` as one more fragment without changing the shell.
"""

from __future__ import annotations

#: CalVer YYYY.M.PATCH; bump when the report's layout or projection changes (stamped in the
#: footer, folded into no cache key). History: `docs/research/report-version-history-2026-11-25.md`.
REPORT_VERSION = "2026.11.5"

from .collect import collect_report  # noqa: E402
from .render import ReportFiles, render_html, write_report  # noqa: E402

__all__ = [
    "REPORT_VERSION",
    "ReportFiles",
    "collect_report",
    "render_html",
    "write_report",
]
