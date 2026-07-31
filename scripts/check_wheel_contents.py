#!/usr/bin/env python
"""Look inside the built wheel and fail if the package data it cannot work without is missing.

Package data is not Python, so nobody notices it going missing: the source tree always has it, so
every unit test stays green while the *shipped* artifact is broken. Each file checked here, absent,
is a specific and silent-ish failure:

  - `io/onlists/*.codes.gz`  -> compose exits 3 on every real 10x dataset
  - `workflows/map/*.smk`    -> the emitted Snakefile includes a module that is not there
  - `kb/specs/*/spec.yaml`   -> the KB is empty and nothing resolves
  - `report/assets/*`        -> `seqforge report` renders an unstyled page

It also pins the packaging arrangement: `packages = ["src/seqforge"]` already carries them, and a
`force-include` on top would be a hard build error rather than a duplicate. Both directions are
covered here -- the wheel BUILT (the `build` job's own step already proved that), AND it has the
files.

This lives in the CI `build` job rather than in pytest deliberately (issue #108). The wheel already
exists there; the equivalent test built a *second* wheel via subprocess, cost ~1.9s in the `default`
env that `pixi run check` uses, and -- worst -- skipped in the `test` env CI actually runs, because
`python-build` is a `dev`-feature dependency. The assertion now runs exactly where the artifact it
inspects already sits. Run it locally the same way CI does: `pixi run build && pixi run check-wheel`.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def _check(names: list[str]) -> list[str]:
    """Return the list of failures (empty == the wheel ships everything it must)."""
    # The counts are conservative LOWER bounds, not exact totals — the wheel ships more of each
    # (5 onlists, 3 map modules, 10+ specs). They exist to catch "the whole category fell out of the
    # packaging" (a broken `packages`/`force-include`), which is the silent failure mode, not the loss
    # of one file among many. Bump a bound only if a category's floor genuinely rises.
    failures: list[str] = []
    if not any(n.endswith("io/onlists/index.json") for n in names):
        failures.append("io/onlists/index.json is missing")
    if sum(n.endswith(".codes.gz") for n in names) < 3:
        failures.append("the packed whitelists (io/onlists/*.codes.gz) are missing")
    if sum(n.endswith(".smk") for n in names) < 2:
        failures.append("the workflow modules (workflows/map/*.smk) are missing")
    if sum(n.endswith("spec.yaml") for n in names) < 5:
        failures.append("the KB specs (kb/specs/*/spec.yaml) are missing")
    if not (
        any(n.endswith("report/assets/report.css") for n in names)
        and any(n.endswith("report/assets/report.js") for n in names)
    ):
        failures.append("the report's inlined CSS/JS assets are missing -> report renders unstyled")
    return failures


def main() -> int:
    wheels = sorted(DIST.glob("*.whl"))
    if not wheels:
        print(f"no wheel found in {DIST} -- run `pixi run build` first", file=sys.stderr)
        return 2
    if len(wheels) > 1:
        # `python -m build` writes one wheel per version; more than one means stale artifacts.
        print(f"expected one wheel in {DIST}, found {len(wheels)}: {wheels}", file=sys.stderr)
        return 2
    wheel = wheels[0]
    names = zipfile.ZipFile(wheel).namelist()
    failures = _check(names)
    if failures:
        print(f"{wheel.name} is missing package data it cannot work without:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"{wheel.name}: ships onlists, .smk modules, KB specs and report assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
