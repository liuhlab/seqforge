# Changelog

Versioning is **CalVer `YYYY.M.PATCH`** — year, month without zero-padding, then a patch counter that
increments per release within the month and resets when the month changes. The version tracks
`[project].version` in `pyproject.toml`.

## Unreleased

### Changed

- Python 3.13 is now the minimum supported version (`requires-python = ">=3.13"`). Every pixi
  environment already ran 3.13, so the old 3.12 floor had no test behind it.
