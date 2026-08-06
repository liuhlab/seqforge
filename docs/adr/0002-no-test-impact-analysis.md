# 2. No test-impact analysis, and no `loadfile` — selection is a rule and grouping is opt-in

A coverage-graph selector like `pytest-testmon` is blind to this suite's data: one line changed in a
`kb/specs/*/spec.yaml` reddens fourteen test files while executing no new Python line, so the
selector reports green, and the instrumentation costs 25–70% to buy a discount measured at 1.14×.
Coverage as a *measurement* is welcome off the critical path; only coverage as a *selector* is
refused. `--dist=loadfile` loses the same way — it groups every file whether the grouping pays,
making the longest file the floor on the suite wall — so the tasks run `--dist=loadgroup`, the same
mechanism made opt-in, and a module earns an `xdist_group` mark only once its fixture is measured
against the tests that read it.

**Status.** Absorbs ADR-0038 — the xdist distribution mode, and the per-module grouping test.
