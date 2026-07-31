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

`mypy --strict` on `models/`, `probe/`, `resolve/`, `manifest/`, `compose/`, `workflows/`, `harvest/`
and `evals/` — everything except `cli/`, `io/`, `kb/` and `hooks/`. The split is not taste: a wrong
type in the strict set poisons the corpus.

## Versioning: CalVer, never SemVer

`YYYY.M.PATCH`, including the component stamps (`PROBE_VERSION`, `kb_version`, `resolve_version`,
`workflow_version`). They fold into content-addressed cache keys, so a version is a date-stamped
identity rather than a compatibility promise.

## Docs

mkdocs-material → gh-pages, published from `main` by `.github/workflows/docs.yml`. The site is the
**human** layer. Three trees are agent-facing and therefore excluded from it (`exclude_docs` in
`mkdocs.yml`): `design.md`, `agents/` and `adr/` — they must not read as settled guidance under a docs
URL. Anything added to `exclude_docs` must also be added to `ignores` in `.markdownlint-cli2.yaml`;
`tests/test_docs.py` fails if the two lists drift, because they are the same list.

## Discussions (GitHub)

The lab notebook and dev forum: miscellaneous **developmental/technical** notes worth remembering —
benchmark results, profiling records, gotchas, lessons, design debates. This is **not** `docs/`. The
site is the carefully designed end-user layer; Discussions is informal, developer-facing, threaded,
and never needs to read as polished. Post results in *Show and tell*, proposals in *Ideas*, questions
in *Q&A*, driven from the CLI via `gh discussion` (or `gh api graphql`). Record durable findings here
rather than lose them. (The `.wiki.git` is unused — Discussions was chosen over the wiki because it
does notes **and** conversation; a wiki is a document store only.)

## Planned, not built

`syrupy` snapshots and `hypothesis` are pinned but not imported. Both register a `pytest11` plugin
that every xdist worker would otherwise import, so `addopts` disables them (along with the transitive
`zarr` and `anyio` plugins). **Adopting either means deleting its `-p no:` line** in `pyproject.toml`;
the failure if you forget is loud, which is why that is a comment and not a guard test.
