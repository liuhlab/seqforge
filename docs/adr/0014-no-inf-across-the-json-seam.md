# 14. No ±inf crosses the JSON seam — a forbidden cell serializes as a status

Date: 2026-07-31

## Status

Accepted.

## Context

In the evidence matrix, a cell is FORBIDDEN when any `requires` gate FAILs or any `excludes` gate
PASSes, and the argmax over injective role assignments needs it to lose against every finite score.
`float("-inf")` is the natural sentinel for that, and it is the right one *in memory*.

But every scored object is also a stdout payload ([ADR 0013](0013-cli-is-a-machine-interface.md)),
and JSON cannot represent infinity.

## Decision

`FORBIDDEN` is `float("-inf")` **for computation only**. Serialized, a cell is a tagged status:

```json
{"status": "forbidden"}
{"status": "scored", "value": 0.82}
```

**No ±inf ever crosses the JSON boundary.**

## Why not serialize the sentinel

JSON cannot represent it and Pydantic's inf handling is lossy — the wire is exactly the place where
a `-inf` quietly becomes something else, and the something else compares fine.

## Why a status rather than `null`

`null` would conflate "forbidden by a gate" with "not computed", and `ABSTAIN` already makes that
distinction load-bearing: a probe that *cannot see* a signal must not read as a signal that is
*absent*, and neither may read as a cell nobody evaluated. A tagged status keeps three states
distinguishable where a nullable float keeps two.

## So in code

**No ±inf crosses the JSON seam.** Keep `float("-inf")` for computation and serialize a cell as a
tagged status — `{"status": "forbidden"}` or `{"status": "scored", "value": …}` — never as `null`,
which would conflate "forbidden by a gate" with "not computed" and with `ABSTAIN`. Inside the
optimizer, encode a forbidden cell as a `+BIG` edge and then post-check that no *selected* edge is
one; a solver will happily return a solution made entirely of them. An unfillable role is
`score(t) = −∞`, never a padded assignment.

**Gate.** `test_resolve_matrix_is_json_safe` (`tests/test_resolve.py`) for the wire, and
`test_assignment_forbidden_diagonal_forces_swap` (same file) for the post-check;
`test_schema_export_is_valid_json_per_model_and_over_all` (`tests/test_cli.py`) keeps the exported
schema describing what actually crosses.

## Consequences

- `TechScore` is JSON-safe by construction, so `schema export` describes what actually goes over the
  wire rather than an in-memory idealization of it.
- The same discipline holds inside the optimizer, where the sentinel is real and the risk is the
  same: a solver handed `+BIG` edges will happily return a "solution" made of them, so the
  post-check above is what makes the encoding safe rather than merely convenient.
