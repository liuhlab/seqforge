# Changelog

Versioning is **CalVer `YYYY.M.PATCH`** — year, month without zero-padding, then a patch counter that
increments per release within the month and resets when the month changes. The version is the
release's git tag (`v2026.7.1` is version `2026.7.1`), which hatch-vcs reads at build time;
`pyproject.toml` carries no version.

## Unreleased

### Changed

- Python 3.13 is now the minimum supported version (`requires-python = ">=3.13"`). Every pixi
  environment already ran 3.13, so the old 3.12 floor had no test behind it.
- The package version now comes from the git tag, through hatch-vcs. The release wheel is built
  by `pixi run build`, the same task every pull request's CI runs, and is checked by
  `pixi run check-dist`.
