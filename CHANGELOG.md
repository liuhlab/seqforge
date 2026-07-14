# Changelog

Versioning is **CalVer `YYYY.M.PATCH`** — year, month without zero-padding, then a patch counter that
increments per release within the month and resets when the month changes. The version tracks
`[project].version` in `pyproject.toml`.

## 2026.7.0 — 2026-07-14

- **Milestone 0 scaffolding.** Package layout (`src/seqforge`), pixi environments + tasks
  (`test`/`typecheck`/`lint`/`fmt`/`check`/`docs-build`), ruff + mypy-strict (scoped to `models/` and
  `probe/`) config, packaging via hatchling.
- **`models/`** — the Pydantic v2 single source of truth: `Evidenced[T]`, `Observation`, `Assertion`
  (+ `AssertionDraft`), `Conflict`, `Blocker`, the three-section `Manifest`, and the score/compile
  output models; plus `schema export` machinery.
- Design (`docs/design.md`), rules (`CLAUDE.md`), and rationale (`PROJECT_BRIEF.md`) in place.
