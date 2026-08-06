# 2. Selection is a rule that lives in the suite, and grouping is opt-in

**Selection may not be a function of a fact living outside the suite.** A coverage-graph selector like
`pytest-testmon` is blind to this suite's data: one line changed in a `kb/specs/*/spec.yaml` reddens
fourteen test files while executing no new Python line, so the selector reports green, and the
instrumentation costs 25–70% to buy a discount measured at 1.14×. Coverage as a *measurement* is
welcome off the critical path; only coverage as a *selector* is refused. `--dist=loadfile` loses the
same way — it groups every file whether the grouping pays, making the longest file the floor on the
suite wall — so the tasks run `--dist=loadgroup`, the same mechanism made opt-in, and a module earns
an `xdist_group` mark only once its fixture is measured against the tests that read it. A **marker
partition** passes the rule: `-m external` against its negation is total by boolean negation, static,
and written where the tests are, so CI's two test jobs split the suite rather than duplicate part of
it. A **path-filtered CI lane** fails the rule — it selects on the diff, by a rule nobody re-derives
when a test's inputs change, and `tests/test_skills.py` already proves the gap is not hypothetical:
67 tests of which 19 carry `repo`, so a docs lane would skip 48 that read the prose that changed. A
genuine *reads-prose* axis carrying its own completeness guard would reopen that question; a widened
`repo` never can, because `repo` partitions by what a test is *about* and such a lane needs a
partition by what a test *reads*.

**Status.** Absorbs ADR-0038 — the xdist distribution mode, and the per-module grouping test. Amended
— the principle is stated generally, and the marker partition and the path-filtered lane are ruled on.
