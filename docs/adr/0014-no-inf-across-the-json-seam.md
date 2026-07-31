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

## Consequences

- `TechScore` is JSON-safe by construction, so `schema export` describes what actually goes over the
  wire rather than an in-memory idealization of it.
- The same discipline holds inside the optimizer: an all-forbidden row means the role is unfillable
  → `score(t) = −∞`, **not** a padded assignment. The Hungarian path encodes forbidden cells as
  `+BIG` edges and then **post-checks that no selected edge is a BIG edge**, because a solver will
  happily return a "solution" made of them.
