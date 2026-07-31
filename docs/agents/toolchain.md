# Toolchain: pixi, lint, typing, versioning, docs

Everything runs through **pixi** (not `pip` / `conda` / `venv`).

```bash
pixi install                 # build environments
pixi run -e test pytest tests/test_probe.py -k budget   # rung 1: the red->green loop, ~2s
pixi run check               # rung 2: lint + fmt-check + typecheck + test, all four in PARALLEL (~17s)
pixi run test                # the whole suite on its own (~10s; xdist, 12 workers max)
pixi run test-failed         # --lf --new-first -x: re-run what broke, worst first
pixi run -e docs docs-build  # mkdocs build --strict
```

**`pixi run check` is the mechanism** — most rules are enforced by tests, so a green suite *is* the
guarantee ([`rules.md`](rules.md) maps rule to test). It is a **pre-PR gate, run once**, not a
per-edit one: in the loop run the one file that tests the module you edited (test files mirror
packages, so that question has an answer), and once the PR is open read CI rather than re-running it
locally. The three rungs, the two markers (`external`, `repo`) and the module→file table are
[`testing.md`](testing.md).

## Lint and format

ruff, with `line-length=100`, `target-version=py312`, `select=[E,W,F,I,UP,B]` and
`ignore=[E501, UP046, UP047]`. PEP-695 generics are off deliberately: classic `Generic[T]` / `TypeVar`
has better pydantic-v2 and mypy support.

## Typing

`mypy --strict` over the whole repository: `src/`, `tests/`, `scripts/`, `skills/`. There is no tier
system and nothing is exempt — commit a new top-level package and the suite goes red until you add
the tree to `files`. You still type the line; you cannot forget it.

**The scope lives in `[tool.mypy] files`, not in the `typecheck` task.** The task string is reachable
only by things that *run* the task (`scripts/check.sh`, the pre-commit hook, CI); an editor extension
reads the config. Putting the scope where all four look is what makes the editor's errors CI's
errors. `test_nothing_tracked_escapes_the_type_checker` (`tests/test_repo_invariants.py`) holds it
open from both sides: every git-tracked `.py` file must fall inside the declared scope, and every
path `exclude` hides must already be gitignored — so nobody turns the gate green by hiding code from
it.

**No tiers.** The only `[[tool.mypy.overrides]]` blocks left declare that four third-party packages
ship no type information (scipy, the two PDF engines, pooch); nothing in this repo is checked at a
lower standard than anything else.

A test that patches a module's clock or HTTP handle imports that module by its own name
(`import requests; monkeypatch.setattr(requests, "get", ...)`), never through the module under test
(`remote.requests`). Both are the same object, so the patch is identical — but the second form needs
`implicit_reexport`, and that flag is keyed on the module being imported **FROM**, not the importing
one, so it cannot be granted to `tests/` only. Measured: an override on the test module clears 0 of
the 9 findings in `test_remote.py`; one on the source module clears 9. Reaching the module by its
own name is what lets the guard stay on everywhere.

**Pylance does not type-check.** `[tool.pyright] typeCheckingMode = "off"` — in the project file
rather than `.vscode/`, so any pyright-based language server honours it. Pylance keeps hover,
completion, go-to-definition, symbol search and unresolved-import reporting; none of those duplicated
mypy. The live squiggles come from the **mypy language server** extension instead, pointed by
`.vscode/settings.json` at the pixi environment's own binary and recommended to a fresh clone by
`.vscode/extensions.json`. Decline the extension and you lose live feedback and fall back to the
task; nothing breaks.

Why one checker and not two, and why the old strict/non-strict split was dissolved:
[`docs/adr/0017-one-type-checker-and-the-editor-runs-it.md`](../adr/0017-one-type-checker-and-the-editor-runs-it.md).

## Versioning: CalVer, never SemVer

`YYYY.M.PATCH`, including the component stamps (`PROBE_VERSION`, `kb_version`, `resolve_version`,
`workflow_version`). They fold into content-addressed cache keys, so a version is a date-stamped
identity rather than a compatibility promise.

## Docs

mkdocs-material → gh-pages, published from `main` by `.github/workflows/docs.yml`. The site is the
**human** layer. Two trees are agent-facing and therefore excluded from it (`exclude_docs` in
`mkdocs.yml`): `agents/` and `adr/` — they must not read as settled guidance under a docs URL.
Anything added to `exclude_docs` must also be added to `ignores` in `.markdownlint-cli2.yaml`;
`tests/test_docs.py` fails if the two lists drift, because they are the same list.

## Discussions (GitHub)

The lab notebook and dev forum: miscellaneous **developmental/technical** notes worth remembering —
benchmark results, profiling records, gotchas, lessons, design debates. This is **not** `docs/`. The
site is the carefully designed end-user layer; Discussions is informal, developer-facing, threaded,
and never needs to read as polished. Post results in *Show and tell*, proposals in *Ideas*, questions
in *Q&A*, driven from the CLI via `gh discussion` (or `gh api graphql`). Record durable findings here
rather than lose them. (The `.wiki.git` is unused — Discussions was chosen over the wiki because it
does notes **and** conversation; a wiki is a document store only.)
