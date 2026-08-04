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
#: content-addressed cache key — the report is a rebuildable view, not an input to anything. It is
#: stamped in the footer, which is the whole reason it is worth bumping: a page found on a shared
#: drive months later has to be able to say which design drew it.
#:
#: 2026.8.2 — **the cross-check**. Results gains an alert block between the run state and the metrics
#: table: the compiler joins what it decided to what came back and says which DECISION looks wrong,
#: naming the manifest or recipe field and the value it currently carries. Advisory and non-mutating
#: — nothing here writes an artifact or moves an exit code — and absent entirely on a healthy run, so
#: seeing one means something. No new hue: severity wears the verdict pair the page already spends
#: its colour budget on (`likely` -> `bad`, `possible` -> `warn`), with the same `!`/`!!` mark.
#:
#: 2026.8.1 — **the design system**. The 559-line hand-written stylesheet is gone; the page is drawn
#: by a vendored, purged Tailwind build over the token layer it now shares with ``seqforge eval
#: report``, plus one small first-party component layer. Colour marks exceptions only — a metric
#: that is within range, a step that went fine and a number nobody could set a bar for all carry no
#: tint, and the two hues that remain are a measured contrast pair rather than an eyeballed one.
#: Results leads with one sample-by-metric table behind a single fold instead of a wall of per-sample
#: tiles; Samples and Evidence share one grid vocabulary; Flow and Pipeline are hairline cards. Every
#: panel is the same box in the same column, and the page is ~26 % smaller.
#:
#: 2026.8.0 — a **Results** tab: a completeness strip for the pipeline itself (distinct from the
#: header's compile verdict — "we produced a Snakefile" and "that Snakefile finished" are two facts),
#: a General-Statistics table tinted by each metric's own verdict, a per-sample headline strip, and
#: hand-built inline-SVG knee plots. The tab is omitted from the strip entirely when no assay has
#: results, so a compiled-but-not-yet-run workspace renders exactly the page it rendered before.
REPORT_VERSION = "2026.8.2"

from .collect import collect_report  # noqa: E402
from .render import render_html  # noqa: E402

__all__ = ["REPORT_VERSION", "collect_report", "render_html"]
