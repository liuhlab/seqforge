# Eval-report assets

These files are inlined into every `seqforge eval report` page so it is fully self-contained and opens
offline. They ship inside the package tree, so `packages = ["src/seqforge"]` carries them into the
wheel automatically — no `force-include` needed (`scripts/check_wheel_contents.py` asserts they made
it).

| file | what it is | shipped? |
|---|---|---|
| `eval-report.html` | the page **template** — doctype, head, header chrome, section order, `{{SLOT}}` markers | yes, filled at render |
| `eval-report.css` | **built artifact** — Tailwind CSS v4.3.3, purged and minified, plus this repo's token/component layer | yes, inlined at render |
| `eval-report.src.css` | the **input** to that build: `@import "tailwindcss"` + the first-party layers | yes (source of record) |
| `eval-report.js` | first-party, hand-written | yes, inlined at render |

The template is filled by `_fill` in `../report.py` — one regex pass over `{{SLOT}}` markers, no
templating engine and no dependency. It deliberately cannot express a condition or a loop: every
decision about what to show belongs to the fragment functions in `report.py`, so the layout stays
editable by someone who does not want to read Python, and the template stays something you can read
top to bottom and know what the page contains.

## Tailwind IS vendored, as a built file

Third-party: **Tailwind CSS v4.3.3** (MIT). It is not linked, not fetched, and not reimplemented —
`eval-report.css` is the real compiler's output for this page's class set, and it carries Tailwind's
MIT banner comment at the top.

A Tailwind **Play CDN** `<script src="https://cdn.tailwindcss.com">` was never an option and must
never be added. These pages are downloaded as CI artifacts and opened from `file://`, where an
external stylesheet or script silently renders *nothing* — and
`test_the_eval_report_makes_no_external_network_reference` fails the build if any `http(s)://`
`src`/`href` reaches a page. Vendoring a built, purged stylesheet is the only admissible form.

### Rebuilding it

From **this directory**, with node ≥ 20:

```bash
npm install --no-save --no-audit --no-fund tailwindcss@4.3.3 @tailwindcss/cli@4.3.3
npx @tailwindcss/cli -i eval-report.src.css -o eval-report.css --minify
rm -rf node_modules package-lock.json     # nothing from npm belongs in this repo
```

`--no-save` and the `rm` are the point: node is a *build-time* tool for one generated file, not a
dependency of `seqforge`. There is no `package.json`, no lockfile and no node in `pyproject.toml`,
because a Python package that needs npm to install would be a much worse trade than a checked-in 21 KB
stylesheet.

Pinned to `4.3.3` and not `latest` on purpose: the version is the only thing that makes the artifact
reproducible, and "whatever npm served that day" is not a provenance.

Two things about the build worth knowing before editing it:

- `@import "tailwindcss" source(none);` plus explicit `@source` lines for `../report.py` **and**
  `./eval-report.html` disables automatic source detection, so the purge depends on those two files
  and **not** on the directory the build ran from. Without it the output changes with your shell's
  `cwd`. Both files are listed because both carry class names — the shell's in the template, every
  fragment's in the Python.
- The purge only sees **literal** class strings. `f"lv-{level}"` in Python is invisible to it — which
  is why every computed class (`lv-ok`, `lv-poison`, `row-bad`, …) is a first-party rule written in
  `eval-report.src.css` rather than a Tailwind utility.

### What stops it rotting

Editing `report.py` or `eval-report.src.css` and forgetting to rebuild is the obvious failure, and it
is silent — the page just loses a style. Two tests close it from both ends, so this is a mechanism and
not a rule someone has to remember:

- `test_every_class_the_page_uses_has_a_rule_in_the_stylesheet` renders a report exercising every
  branch and fails if any class on the page has no rule in the built CSS (catches a **new utility in
  `report.py`** that was never built).
- `test_the_built_stylesheet_carries_every_component_its_source_declares` fails if a selector in the
  `@layer components` block of the source is absent from the built file (catches a **new component in
  `.src.css`** that was never built).

## Division of labour, and why the theming is not Tailwind's

Tailwind does layout, spacing and type. **Theming does not go through `dark:` utilities**: the page has
to honour both `prefers-color-scheme` *and* a `data-theme` attribute that a host's theme toggle stamps
on `<html>`, with the attribute winning in **both** directions (`data-theme="light"` on a dark OS must
actually go light — a bare media query cannot do that). That is one token block per theme in
`@layer base`, and every component then reads `var(--sf-*)`. Tailwind's stock `dark:` variant keys off
the media query alone and would quietly ignore the toggle, so `eval-report.src.css` redefines it via
`@custom-variant` — a maintainer who reaches for `dark:` later gets the right behaviour instead of a
bug that only shows up on someone else's laptop.

Semantic colour is deliberately a different hue family from the accent: the accent (indigo) is chrome
and means nothing, `ok`/`warn`/`critical` are the verdict. And `false_accept` gets a *filled* pill
where every other grade gets an outline, because it is not a worse shade of "bad" — it is the one
failure with no tolerable rate, and severity has to be legible as form and not only as hue.

## Relationship to `report/assets/`

`seqforge report` (the workspace reader) has **no** third-party runtime at all — see
`../../report/assets/VENDOR.md`. The two modules share a convention (assets as real files, inlined via
`importlib.resources`, no templating engine, `_script_guard` around embedded JS) but not a stylesheet:
one is a lab-notebook view of a dataset, the other a CI grading report, and merging their CSS would
couple two pages that change for unrelated reasons.
