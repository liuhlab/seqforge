# Report assets

These files are inlined into every `seqforge report` HTML so the page is fully self-contained and
opens offline (no CDN, no network). They ship inside the package tree, so `packages=["src/seqforge"]`
carries them into the wheel automatically — no `force-include` needed.

## `report.css`, `report.js`

First-party (authored in this repo), not third-party. Kept here as files rather than Python string
literals so they get real syntax highlighting and linting, and are inlined at render via
`importlib.resources`.

## No third-party runtime

The report has **no vendored third-party engine**, and no charting library either. The Results tab's
knee plots are hand-built inline SVG (`panels._knee_figure`): a `<polyline>` over log-log axes drawn
from at most 200 points per sample, styled by the classes in `report.css` so they follow the theme
toggle like everything else. That is a few KB per sample against the ~1 MB a plotting bundle would
cost, and it keeps the promise below intact — a chart engine is exactly the kind of asset that
arrives as a CDN `<script src>` and quietly makes the page need the network.

The Flow tab used to render a Mermaid diagram
(a ~2.5 MB inlined bundle), but a scaled SVG cannot reflow — its text shrank to nothing on wide
datasets — so the flow is now plain HTML cards that wrap responsively via CSS. Dropping Mermaid cut a
rendered page from ~2.6 MB to a few tens of KB. A strict "no external network references" test
(`tests/test_report.py`) still fails the build if any `http(s)://` `src`/`href` sneaks into a page, so
the report can never regress into a CDN link.
