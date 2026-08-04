# Report assets

These files are inlined into every `seqforge report` HTML so the page is fully self-contained and
opens offline (no CDN, no network). They ship inside the package tree, so `packages=["src/seqforge"]`
carries them into the wheel automatically — no `force-include` needed
(`scripts/check_wheel_contents.py` asserts they made it).

| file | what it is | shipped? |
|---|---|---|
| `report.css` | first-party, hand-written — the page's styling and its reset | yes, inlined at render |
| `report.tw.css` | **built artifact** — Tailwind CSS v4.3.3, purged and minified, plus this repo's token/component layer | yes, inlined at render |
| `report.src.css` | the **input** to that build: Tailwind's parts + the first-party layers | yes (source of record) |
| `report.js` | first-party, hand-written | yes, inlined at render |

`report.css` and `report.js` are kept as real files rather than Python string literals so they get
syntax highlighting and linting, and are inlined at render via `importlib.resources`.

Both stylesheets go into the page, in the order `render._STYLESHEETS` names: the vendored build
first, the hand-written sheet last. Tailwind emits everything inside real CSS cascade layers, and
unlayered CSS outranks every layer whatever the source order — so `report.css`, which is entirely
unlayered, wins every overlap on the cascade alone. Putting it second means it also wins on source
order, which is what decides the small *unlayered* remainder Tailwind emits (`@property`
registrations today). Two arguments agreeing beats one of them silently mattering.

## Tailwind IS vendored, as a built file

Third-party: **Tailwind CSS v4.3.3** (MIT). It is not linked, not fetched and not reimplemented —
`report.tw.css` is the real compiler's output for this page's class set, and it carries Tailwind's
MIT banner comment at the top. Nothing third-party *executes*: it is a stylesheet, and the page still
ships no diagram engine and no charting library (see below).

A Tailwind **Play CDN** `<script src="https://cdn.tailwindcss.com">` was never an option and must
never be added. These pages are opened from `file://`, where an external stylesheet or script
silently renders *nothing* — and `test_report_makes_no_external_network_reference` fails the build if
any `http(s)://` `src`/`href` reaches a page. Vendoring a built, purged stylesheet is the only
admissible form.

### Rebuilding it

From **this directory**, with node ≥ 20:

```bash
npm install --no-save --no-audit --no-fund tailwindcss@4.3.3 @tailwindcss/cli@4.3.3
npx @tailwindcss/cli -i report.src.css -o report.tw.css --minify
rm -rf node_modules package-lock.json     # nothing from npm belongs in this repo
```

`--no-save` and the `rm` are the point: node is a *build-time* tool for one generated file, not a
dependency of `seqforge`. There is no `package.json`, no lockfile and no node in `pyproject.toml`,
because a Python package that needs npm to install would be a much worse trade than a checked-in 3 KB
stylesheet.

Pinned to `4.3.3` and not `latest` on purpose, and to the same version as `evals/assets/`: the
version is the only thing that makes the artifact reproducible, "whatever npm served that day" is not
a provenance, and two builds on two versions is not one system.

Three things about the build are worth knowing before editing it:

- Tailwind's parts are imported **individually** (`tailwindcss/theme.css`, `tailwindcss/utilities.css`)
  rather than as `@import "tailwindcss"`, because that is the documented way to leave one of them out
  — and one of them *is* left out. See "Preflight is sequenced" below.
- `source(none)` on the utilities import plus explicit `@source` lines for `../panels.py`,
  `../render.py` and `./report.js` disables automatic source detection, so the purge depends on those
  three files and **not** on the directory the build ran from. Without it the output changes with
  your shell's `cwd`. All three carry class names: every fragment's in `panels.py`, the page shell's
  in `render.py`, and the provenance popover's in the JS that builds it at runtime.
- The purge only sees **literal** class strings. `f"lvl-{level}"` in Python is invisible to it —
  which is why every computed class is a first-party rule rather than a Tailwind utility.

### The tokens are shared, the components are not

`report.src.css` imports **`../../assets/sf-tokens.css`**, the one token layer, which
`evals/assets/eval-report.src.css` imports too. That file carries the `--sf-*` scales, both theme
blocks and the `dark` variant; Tailwind bundles the import at build time, so each page still ships as
one self-contained stylesheet. A token fix — a contrast pair that fails for a deuteranope, a surface
too bright at night — lands once instead of twice and slightly differently.

Theming does **not** go through `dark:` utilities. The page must honour both `prefers-color-scheme`
*and* the `data-theme` attribute `report.js` stamps on `<html>`, with the attribute winning in **both**
directions (`data-theme="light"` on a dark OS must actually go light — a bare media query cannot do
that). That is one token block per theme, and every component then reads `var(--sf-*)`. Tailwind's
stock `dark:` variant keys off the media query alone and would quietly ignore the toggle, so
`sf-tokens.css` redefines it via `@custom-variant`.

What is deliberately **not** shared is either page's `@layer components`: a lab-notebook view of a
dataset and a CI grading report change for unrelated reasons, and coupling their component
vocabularies would make every redesign of one a review of the other.

### Preflight is sequenced, not dropped

`tailwindcss/preflight.css` is **not** imported yet. Tailwind's cascade-layer argument — unlayered
beats layered, so the hand-written sheet wins — holds for every property that sheet *sets*. Preflight
is the part where it does not: a reset reaches bare element selectors `report.css` never mentions,
and there the layered rule beats only the UA stylesheet and wins. Measured against today's page,
importing it while `report.css` is still inlined would:

| Preflight rule | what moves |
|---|---|
| `h1,h2,h3,h4 { font-weight: inherit }` | every heading unbolds — `.hero h1`, `.panel > h2`, `.family-focus h3`, `.detail-body h4` all set a size and inherit their weight from the UA |
| `ol,ul,menu { list-style: none }` | `ul.pipeline-notes` loses its bullets |
| `* { margin: 0 }` | `p.empty`, `p.notice` and `.hero p.organism` lose the UA paragraph margins they rely on |
| `button { font: inherit }` | `#theme-toggle.icon-btn` swaps the UA control font for the body font |
| `b,strong { font-weight: bolder }` | a `<b>` inside a 600-weight parent goes to 900 rather than 700 |

The page already has a reset — `report.css` sets `* { box-sizing: border-box }` and zeroes the
margins it cares about — and two resets on one page is one too many. So Preflight arrives with the
commit that deletes the hand-written sheet, which is the commit that is allowed to change the page.
`test_preflight_arrives_exactly_when_the_hand_written_sheet_leaves` is the mechanism rather than this
paragraph: it goes red the moment `report.css` stops being inlined, and its failure message is the
one line to add.

### What stops it rotting

Editing `panels.py` or `report.src.css` and forgetting to rebuild is the obvious failure, and it is
silent — the page just loses a style. Two tests close it from both ends, so this is a mechanism and
not a rule someone has to remember:

- `test_every_class_the_page_uses_has_a_rule_in_a_stylesheet` collects every literal class the page
  can carry — the rendered page plus the `@source` modules, so a branch the fixture never reaches is
  checked too — and fails if any of them has a rule in *neither* stylesheet (catches a **new utility
  in `panels.py`** that was never built).
- `test_the_built_stylesheet_carries_every_component_its_source_declares` fails if a selector in the
  `@layer components` block of the source is absent from the built file (catches a **new component in
  `report.src.css`** that was never built).
- `test_the_two_drift_guards_fire_on_a_drifted_input_and_stay_silent_on_a_clean_one` drives both
  matchers against synthetic inputs, because a guard that only passes today is the rule it replaced.

## No charting or diagram engine

The Results tab's knee plots are hand-built inline SVG (`panels._knee_figure`): a `<polyline>` over
log-log axes drawn from at most 200 points per sample, styled by classes so they follow the theme
toggle like everything else. That is a few KB per sample against the ~1 MB a plotting bundle would
cost, and a chart engine is exactly the kind of asset that arrives as a CDN `<script src>` and quietly
makes the page need the network.

The Flow tab used to render a Mermaid diagram (a ~2.5 MB inlined bundle), but a scaled SVG cannot
reflow — its text shrank to nothing on wide datasets — so the flow is now plain HTML cards that wrap
responsively via CSS. Dropping Mermaid cut a rendered page from ~2.6 MB to a few tens of KB.
