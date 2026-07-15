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
- **Prompt `2026.7.1` — `experiment.samples.{tissue,condition}` and `accessions` got operational
  definitions, because `eval run --llm` proved they needed them.** DeepSeek V4-Pro filed standard worm
  husbandry ("maintained on NGM plates seeded with E. coli OP50 at 20 C") as an experimental
  *condition*: a real quote, correctly copied, pinned to a field it does not belong in — every sample
  shares that husbandry, and an unperturbed baseline experiment has no condition at all. The prompt
  had defined chemistry and organism, then said "everything else: the document's own wording", which
  invites exactly this. The fix says what each field IS and when to **omit** it; the corpus then
  measured the fix — false-accept **0.111 → 0.0**, field accuracy **0.933 → 1.0**, exit 3 → 0.
  **The finding underneath is architectural, and is now recorded in `verify.entails()` and pinned by a
  test: entailment is VACUOUS when the value is copied out of the quote.** R5's power comes entirely
  from `value` being a controlled-vocabulary term whose surface forms must appear in the quote —
  `library.chemistry` is protected (a "droplet-based single-cell" quote cannot smuggle in a v3
  chemistry; verified 3/3 live against a document that describes a v3 experiment in every way except
  naming it), free text is not, because `form in quote` is trivially true. So **R5 is a tripwire for
  fabricated and mis-attributed claims, never for field-assignment errors** — tightening the matcher
  would not help, as there is nothing left there to check. The defense for free-text fields is the
  prompt's field definition plus the corpus that measures it.
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
- **`evals/` — the harness that measures what unit tests cannot (brief §9).** Every other stage can be
  pinned by "same bytes in, same artifact out"; two things here cannot, and both are load-bearing: the
  LLM stage is **nondeterministic** (the same document has yielded different, both-correct,
  both-span-verified quotes across runs — there is no output to snapshot, only a rate to measure), and
  **prompt/KB edits are silent** (adding a KB alias changes extraction behavior without changing a
  single test). Structure:
  - **Cases are recipes, never bytes** (`evals/cases/<id>/inputs/recipe.yaml`): a few hundred bytes,
    deterministic in `(spec, seed)`, regenerating byte-identically anywhere via the same `kb.generate`
    the R10 round-trip uses — so a KB spec edit *moves the inputs with it*, no FASTQ enters git, and a
    held-out case (`kind: local`, root from out-of-git env) uses the same format: ground truth committed,
    bytes wherever the maintainer keeps them. A missing root **skips** — never a pass, never a fail.
  - **Grading encodes the asymmetry that a refusal is cheap and a wrong manifest is not.** A 3x3
    confusion, not a pass/fail bit: `false_accept` (decided wrong, or decided at all when it should
    have stopped) is the headline; `false_refuse`, `over_ask` (a cost regression, not a correctness
    one), `mis_triage`, and `wrong_reason` (right outcome, wrong BlockerCode/conflict — counting it
    green would let a blocker's *meaning* rot untested) are tracked apart because they cost differently.
    A harvest hallucination **rolls up to `false_accept`**: bytes can never contradict `experiment.*`,
    so a verified-but-wrong assertion reaches the manifest unchallenged.
  - **Trials are first-class.** `--trials N` re-runs each prose case; a case is correct only if *every*
    trial is. A stage right 4 times in 5 is not right, and averaging that away is how a harness lies.
  - **9 seed cases** covering all three outcome classes: 4 byte-only positives (10x v2/v3 — the pair
    that catches a length gate loosening; bulk PE — the case guarding *single-cell scoring well on bulk*;
    splitseq — role inversion), the 3 day-one negatives, and 2 prose cases. The sharpest is
    `chemistry-unstated-trap`: the bytes really *are* v3 and the prose really does describe that
    experiment without naming it, so a model answering "v3" is **correct about the world and wrong at
    its job** — the one case where being right is a failure. Verified: the R5 tripwire rejects it
    unaided (no span entails the claim), before grading is even reached.
  - `eval run` defaults to `--no-llm` (runs in a keyless CI; prose cases skip). Exit 3 on **any**
    false-accept — not on a slider, because no threshold makes one tolerable — or below `--fail-under`.
  - **Live-verified on the lab cluster** (DeepSeek V4-Pro, 3 trials/prose case): 9 cases,
    field_accuracy **1.0**, false_accept **0.0**, false_refuse **0.0**, every case stable across all
    trials, 6 LLM calls in ~90–130 s (wall-clock is API latency, so it moves run to run). `cost` reports
    non-zero `cache_read_tokens` every run — the stable KB prefix genuinely caches, measured not assumed.
  - **It found four real defects, three of them in itself** — which is the point: a harness nobody has
    seen fail is indistinguishable from a broken one.
    1. *A case was wrong, not the design.* It asserted the metadata-vs-reads conflict on
       `library.chemistry`; design §958 specifies it on **geometry** (`read_layout.R1.length`, 26
       asserted vs 28 observed). `positions` is now the load-bearing assertion — a field-name check
       alone passes even if both sides collapse to one value.
    2. *A case was wrong, not the model.* `chemistry-unstated-trap` forbade `experiment.samples.tissue`
       and the live run graded a false-accept for "neurons" — but the prose says "C. elegans neurons"
       and "Neuronal nuclei were isolated". Removed from `forbidden_fields` rather than promoted to a
       required assertion: later runs returned "neuronal nuclei" and "neurons" for the same fact
       across runs, so pinning an exact string would measure string matching, not extraction. A
       harness that cries wolf gets ignored exactly when it is right.
    3. *Trials kept only the LAST harvest*, so a model that hallucinated on trial 1 and behaved on
       trial 3 graded clean — the exact illusion trials exist to dispel. Now merged worst-wins:
       hallucinated/missing union, matched intersects, and come-and-go fields surface as `unstable`.
    4. *`stability` was quietly lying.* `_worst` returns a **reference** into the trials list (`min`
       does not copy) and `_fold_harvest` mutated it in place, so folding rewrote an element that
       `stability` was then counted over — reporting **0.667 for three identical, perfectly stable
       trials**. It looked like real nondeterminism and was pure aliasing. Folding is now per-trial and
       non-mutating. A metric that is quietly wrong is worse than no metric.
- **CLI** — `io onlist list|show`, `io peek`, `io resolve`, `resolve score --json` (`--explain`
  emits the evidence matrices), `manifest fill|validate|hash`, `compose`, `kb e2e`,
  `harvest normalize|extract|verify` (exit 3 on a Blocker or failed gate, 4 on an open
  Conflict/question or a claim that fails the tripwire), and `eval list|run`.
- **Reproducible `.fastq.gz` — one writer, three callers.** The evals determinism test failed ~1 run in
  3 and the cause was real, not a test artifact: `gzip.open(path, "wt")` stamps the **current mtime**
  into the gzip header, so identical reads written a second apart produce different bytes. Everything
  downstream is content-addressed by file sha256 (R7), so a wall-clock-dependent header silently
  changes the dataset id — two runs over the same synthetic input could never share a cache entry, and
  "deterministic in (spec, seed)" was quietly false at the byte level where it is claimed. The writer
  was duplicated at three production sites (`kb/roundtrip`, `e2e`, `evals/case`), each carrying the
  same latent bug; there is now one `kb.generate.write_fastq_gz` (`mtime=0`, `filename=""`) and the
  others delegate. Guarded by a test that asserts the header's mtime field directly, so the property
  cannot regress back into an intermittent flake.
- `resolve_dataset` takes `Sequence[str | Path]`, not `list` — mypy caught that an invariant `list`
  forced callers holding a `list[Path]` to copy it. An API defect, not a caller bug.
- **mypy --strict** scope extended again to `manifest/`, `compose/`, `workflows/`, `harvest/`, and
  `evals/` — a wrong type poisons every emitted pipeline parameter, and in `evals/` it silently
  corrupts a metric (a broken measuring instrument is worse than none: it still reads green).
- **Day-one negatives** — truncated gzip → `TRUNCATED_GZIP`; an ONT run absent from the KB →
  `UNSUPPORTED_TECHNOLOGY` (refused, not guessed); metadata v2 vs 28 bp reads → a surfaced `Conflict`.
- Design (`docs/design.md`), rules (`CLAUDE.md`), and rationale (`PROJECT_BRIEF.md`) in place.
