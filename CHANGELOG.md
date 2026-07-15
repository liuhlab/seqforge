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
- **`manifest/`** — `fill` assembles the three-section manifest from a resolve Decision (library =
  observed bytes incl. the §12 equivalence class and byte-derived roles; experiment = asserted;
  processing = inferred policy), `validate` is the R4 refusal contract (referential integrity,
  no-absolute-path sweep, controlled vocab, role/layout coherence -> `Blocker`s + exit 3/4), and
  `hash` binds a config to its inputs (content hash + `provenance_id`).
- **`workflows/`** — hand-written, versioned `map/starsolo` + `map/star` Snakemake modules (never
  generated) + `WORKFLOW_VERSION` (CalVer) + a module registry with a config contract. The genome
  index resolves at run time via liulab-genome; no path is ever baked into a manifest.
- **`compose/`** — pure fn manifest -> `config.yaml` + `units.tsv` + module selection, resolving
  `{onlist:alias}` to a materialized whitelist. Three-part gate: the deterministic **params** gate
  always runs (KB-faithfulness + KB-vs-observed-layout cross-derivation + `--readFilesIn` order);
  **wiring** (`snakemake -n`/`--lint`) and **e2e** (count matrix) report `skip` when their toolchain
  is absent — never a silent `pass`. `ComposeResult.gate` gained `skip` for exactly this reason.
- **`kb e2e` — the real count-matrix run, implemented and passing.** Simulates reads from sacCer3
  transcripts with injected barcodes/UMIs and drives the whole compiler (probe → resolve → fill →
  validate → compose → STARsolo **with the composed params**), asserting against the injected truth:
  0 spurious pairs, 0 inflated counts, ≤2% loss unexplained by STAR's own multimapper rate, and a
  proven strand-inversion collapse. First green run on the lab GPU cluster (STAR 2.7.11b via liulab-runtime's
  `align-rna`; sacCer3 + index via liulab-genome): resolve decided `10x-3p-gex-v3` from bytes alone;
  1909/2000 recovered, 0 spurious, 0.7% unexplained; inverted strand collapsed to 49/2000.
  Skip-gated (exit 1 + a reason) wherever STAR/liulab-genome are absent.
- **`harvest/`** — the fourth stage. `normalize` builds the canonical span space (two-rule hyphen
  handling: a wrap hyphen inside a word closes up, a digit-adjacent semantic hyphen survives;
  ligatures, line-unwrapping, NFKC). `verify` is the R5 tripwire — both flags code-owned and
  fail-closed: `span_verified` (the quote really occurs) catches fabricated provenance;
  `entailment_ok` (the quote actually supports the value, checked against the KB's own aliases)
  catches the more dangerous failure — a real quote pinned to a wrong conclusion.
- **`harvest extract` — the ONE LLM touchpoint, and vendor-neutral.** The model only proposes
  `{field, value, quote}`: code overwrites `span.doc_sha256` (we know which doc we sent), discards
  model-supplied offsets (models can't count characters), and validates every batch against the
  canonical `AssertionDraft`. Providers: **anthropic** (strict `json_schema`, `claude-opus-4-8`),
  **deepseek** (`json_object` mode, default **`deepseek-v4-pro`**; `deepseek-chat`/`-reasoner` are
  deprecated 2026-07-24 so a V4 model is named explicitly), and **openai-compatible** (any
  `base_url` — vLLM, Ollama, Together). Auto-detects `DEEPSEEK_API_KEY`/`ANTHROPIC_API_KEY`;
  refuses rather than guessing when neither is set. The wire schema is derived from the canonical
  model, never hand-maintained, with a CI guard that the strict transform drops every constraint the
  strict subset rejects.
- **CLI** — `io onlist list|show`, `io peek`, `io resolve`, `resolve score --json` (`--explain`
  emits the evidence matrices), `manifest fill|validate|hash`, `compose`, `kb e2e`, and
  `harvest normalize|extract|verify` (exit 3 on a Blocker or failed gate, 4 on an open
  Conflict/question or a claim that fails the tripwire).
- **mypy --strict** scope extended again to `manifest/`, `compose/`, and `workflows/` — a wrong type
  there poisons every emitted pipeline parameter.
- **Day-one negatives** — truncated gzip → `TRUNCATED_GZIP`; an ONT run absent from the KB →
  `UNSUPPORTED_TECHNOLOGY` (refused, not guessed); metadata v2 vs 28 bp reads → a surfaced `Conflict`.
- Design (`docs/design.md`), rules (`CLAUDE.md`), and rationale (`PROJECT_BRIEF.md`) in place.
