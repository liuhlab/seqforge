# Changelog

Versioning is **CalVer `YYYY.M.PATCH`** — year, month without zero-padding, then a patch counter that
increments per release within the month and resets when the month changes. The version tracks
`[project].version` in `pyproject.toml`.

## 2026.7.0 — 2026-07-14

- **Milestone 0 scaffolding.** Package layout (`src/seqforge`), pixi environments + tasks
  (`test`/`typecheck`/`lint`/`fmt`/`check`/`docs-build`), ruff + mypy-strict config, packaging via
  hatchling.
- **`models/`** — the Pydantic v2 single source of truth: `Evidenced[T]`, `Observation`, `Assertion`
  (+ `AssertionDraft`), `Conflict`, `Blocker`, the three-section `Manifest`, and the score/compile
  output models; plus `schema export` machinery.
- **`probe/`** — bounded (R3) Tier-A FASTQ fingerprinting into a role-free `Observation`; added
  `probe_sample` (Observation + the bounded sample) so `resolve` gets a `WindowProbe`.
- **`io/`** — the onlist registry (`resolve`'s Tier B): width-generic 2-bit packing (`uint32` <=16 bp,
  `uint64` <=32 bp — not a hardcoded 16), a hit-rate test over forward + reverse-complement + a small
  offset scan, `np.intersect1d` set-intersection for confusability, pooch + sha256 fetch, and an
  in-memory synthetic-onlist path for the pilot fixtures. `io peek` / `io resolve` are declared stubs.
- **`resolve/`** — the deterministic scoring engine (`RESOLVE_VERSION` = CalVer): signature-test
  evaluators (`ABSTAIN` first-class, `distinct_ratio` supports-only), a JSON-safe evidence matrix
  `M[role][file]` (no `±inf` on the wire), cardinality-normalized joint role-assignment (brute force
  for `|F|<=8`, Hungarian fallback + no-forbidden-edge post-check), and escalation to
  `Decision | Conflict | Question | Blocker` with rung provenance (onlist-verified rung 3 dominates a
  rung-2 look-alike; §12 benign twins recorded together, 0 questions). Content-addressed `.seqforge/`
  artifacts (R7). `mypy --strict` scope extended to `resolve/`.
- **KB** — added `10x-3p-gex-v2` (26 bp length gate vs v3), `bulk-rnaseq-pe` (the no-barcode PE
  branch), and `splitseq` (original SPLiT-seq, Rosenberg et al. Science 2018 — combinatorial 8 bp
  round barcodes + two fixed 30 bp linkers, read structure + linker sequences pinned from
  scg_lib_structs; Parse Evercode deliberately deferred to its own future entry). All pass
  `kb roundtrip`.
- **CLI** — `io onlist list|show`, `io peek`, `io resolve`, and `resolve score --json` (`--explain`
  emits the evidence matrices; exit 3 on a Blocker, 4 on an open Conflict/question).
- **Day-one negatives** — truncated gzip → `TRUNCATED_GZIP`; an ONT run absent from the KB →
  `UNSUPPORTED_TECHNOLOGY` (refused, not guessed); metadata v2 vs 28 bp reads → a surfaced `Conflict`.
- Design (`docs/design.md`), rules (`CLAUDE.md`), and rationale (`PROJECT_BRIEF.md`) in place.
