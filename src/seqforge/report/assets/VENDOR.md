# Report assets

These files are inlined into every `seqforge report` HTML so the page is fully self-contained and
opens offline (no CDN, no network). They ship inside the package tree, so `packages=["src/seqforge"]`
carries them into the wheel automatically — no `force-include` needed
(`scripts/check_wheel_contents.py` asserts they made it).

| file | what it is | shipped? |
|---|---|---|
| `report.tw.css` | **built artifact** — Tailwind CSS v4.3.3, purged and minified, plus this repo's token/component layer | yes, inlined at render |
| `report.src.css` | the **input** to that build: Tailwind's parts + the first-party layers | yes (source of record) |
| `report.js` | first-party, hand-written | yes, inlined at render |

`report.js` is kept as a real file rather than a Python string literal so it gets syntax highlighting
and linting, and is inlined at render via `importlib.resources`. So is `report.src.css`, which no
Python reads: a wheel that carried a built stylesheet without the file it was compiled from could not
be rebuilt.

There used to be a second sheet. `report.css` was 559 hand-written lines and it was the page's whole
styling and its reset; it was inlined *beside* the build for the length of the redesign, and it won
every overlap on one mechanical fact — Tailwind emits everything inside real CSS cascade layers, and
unlayered CSS outranks every layer whatever the source order. That is what let the design system
arrive without moving a pixel and then take the page over one element at a time: a migrated element
stopped wearing its old class, the old rule stopped matching, and the utilities beside it took
effect immediately. It has no callers left and is deleted. An expand–contract that never contracts is
two systems.

## Tailwind IS vendored, as a built file

**The page ships no third-party _runtime_** — no charting library, no diagram engine, no script
fetched when it opens — and `test_report_makes_no_external_network_reference` is what keeps that
true. It does ship third-party **CSS**, and that is the claim this file exists to make precisely.

Vendored: **Tailwind CSS v4.3.3**, MIT-licensed (© Tailwind Labs, Inc.; the licence text travels with
the npm package, and the built file carries Tailwind's MIT banner comment at the top). It is not
linked, not fetched and not reimplemented — `report.tw.css` is the real compiler's output for this
page's class set, pinned to that exact version and checked in. Nothing about it executes: it is a
stylesheet.

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

Verified from a clean checkout of this repo: `npm install` writes `node_modules/` here, the CLI
reads `report.src.css` and writes `report.tw.css`, and the `rm` leaves the tree exactly as `git
status` found it — the only changed file is the built stylesheet, and it is byte-identical to the
one checked in when the source has not moved.

`--no-save` and the `rm` are the point: node is a *build-time* tool for one generated file, not a
dependency of `seqforge`. There is no `package.json`, no lockfile and no node in `pyproject.toml`,
because a Python package that needs npm to install would be a much worse trade than a checked-in
25 KB stylesheet. Nothing in CI runs this, which is why the two drift guards below exist.

Pinned to `4.3.3` and not `latest` on purpose, and to the same version as `evals/assets/`: the
version is the only thing that makes the artifact reproducible, "whatever npm served that day" is not
a provenance, and two builds on two versions is not one system.

Three things about the build are worth knowing before editing it:

- Tailwind's parts are imported **individually** (`tailwindcss/theme.css`,
  `tailwindcss/preflight.css`, `tailwindcss/utilities.css`) rather than as `@import "tailwindcss"`,
  because that is the documented way to leave one of them out — and one of them *was* left out for
  the length of the redesign. See "Preflight was sequenced" below.
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

### Preflight was sequenced, not dropped

`tailwindcss/preflight.css` is imported, and it is the page's only reset. It was held back for the
length of the redesign, deliberately. Tailwind's cascade-layer argument — unlayered beats layered, so
the hand-written sheet won — held for every property that sheet *set*; Preflight is the part where it
did not, because a reset reaches bare element selectors `report.css` never mentioned, and there the
layered rule beats only the UA stylesheet and wins. Measured against the page as it then was,
importing it alongside would have:

| Preflight rule | what moved |
|---|---|
| `h1..h6 { font-weight: inherit }` | every heading unbolds — each set a size and inherited its weight from the UA |
| `ol,ul,menu { list-style: none }` | the pipeline notes lose their bullets |
| `*,::before,::after { margin: 0; padding: 0 }` | every fallback paragraph loses the UA margins it relied on |
| `button { font: inherit }` | `#theme-toggle` swaps the UA control font for the body font |
| `b,strong { font-weight: bolder }` | a `<b>` inside a 600-weight parent goes to 900 rather than 700 |

Two resets on one page is one too many, so it arrived in the commit that deleted the other one —
which is also the only commit that was allowed to change the page. Each of the five was then a
decision to make rather than a thing to survive, and each is made explicitly: a heading states its
own size and weight, a list states its own markers, and `report.src.css`'s `@layer base` states what
a reset does not decide (the page's colour, its typeface, and that a horizontal overflow belongs to a
`.sf-scroll-x` and never to the body).

`test_preflight_arrives_exactly_when_the_hand_written_sheet_leaves` is the mechanism rather than this
paragraph. It is a biconditional and it still fires in both directions: drop the import on a rebuild
and it goes red with the line to add, re-inline a second reset and it goes red for the other reason.

### What stops it rotting

Editing `panels.py` or `report.src.css` and forgetting to rebuild is the obvious failure, and it is
silent — the page just loses a style. Two tests close it from both ends, so this is a mechanism and
not a rule someone has to remember:

- `test_every_class_the_page_uses_has_a_rule_in_a_stylesheet` collects every literal class the page
  can carry — the rendered page plus the `@source` modules, so a branch the fixture never reaches is
  checked too — and fails if any of them has no rule in the built stylesheet (catches a **new utility
  in `panels.py`** that was never built). It looked in a *union* of two sheets while the hand-written
  one was still inlined, which is a weaker claim; with one sheet left it is as strong as it reads,
  and it is also what now catches a dead class — a name with a rule nowhere.
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
