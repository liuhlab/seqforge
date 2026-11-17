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
- Tests now have time limits: 2 seconds per test and 45 seconds per test run, set once in
  `pyproject.toml`. A test marked `slow` is exempt and runs only through `pixi run test-slow`.
- CI is one job that runs on pull requests only and skips drafts, instead of seven jobs that also
  ran on every push to `main`.
- `pixi run check` runs the three test lanes one after another, as CI does, so each lane is timed
  with the machine to itself.
- liulab-genome is now pinned at `2026.11.1.dev15+gb88e2c40e`, up from
  `2026.8.1.dev2+g30e302516`. seqforge now imports from `genome.assembly.metadata` and
  `genome.assembly.chimera`, so an installed liulab-genome older than this pin fails with an
  `ImportError`.

### Fixed

- Scoring reads against a shipped barcode onlist no longer converts the whole onlist to a wider
  integer type on every lookup. Each lookup against the 6.8 million barcodes of `3M-february-2018`
  took about 9 ms and now takes about 0.04 ms; the result is the same.
