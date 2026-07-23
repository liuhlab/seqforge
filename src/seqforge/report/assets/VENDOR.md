# Vendored assets

These files are inlined into every `seqforge report` HTML so the page is fully self-contained and
opens offline (no CDN, no network). They ship inside the package tree, so `packages=["src/seqforge"]`
carries them into the wheel automatically — no `force-include` needed.

## `mermaid.min.js`

- **Project:** [Mermaid](https://mermaid.js.org/) — diagramming from text.
- **Version:** 11.4.1
- **License:** MIT © 2014–present Knut Sveidqvist
- **Source:** `https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js`
- **Build:** the minified UMD bundle; its last line assigns `globalThis.mermaid`, which is what the
  report's inlined script drives (`mermaid.render(...)`).
- **Why vendored, not fetched:** the report must render on a double-click with no network. A strict
  "no external network references" test (`tests/test_report.py`) fails the build if any `http(s)://`
  `src`/`href` sneaks into a rendered page, so this file cannot regress into a CDN link.

To update: re-download the same `dist/mermaid.min.js` for the new version, confirm the tail still
assigns `globalThis.mermaid`, bump the version above, and re-run `pixi run check` (the size guard and
the offline test must stay green).

## `report.css`, `report.js`

First-party (authored in this repo), not third-party. Kept here as files rather than Python string
literals so they get real syntax highlighting and linting, and are inlined at render via
`importlib.resources`.
