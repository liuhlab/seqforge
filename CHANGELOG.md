# Changelog

Versioning is **CalVer `YYYY.M.PATCH`** — year, month without zero-padding, then a patch counter that
increments per release within the month and resets when the month changes. The version tracks
`[project].version` in `pyproject.toml`.

## Unreleased

**The manifest is now two artifacts: a dataset (the IR) and a processing manifest (the flags).**
A finished assay is immutable; what you do with it is a choice, and there are several defensible
ones. Same IR + different flags = different binaries. This closes the 2026.7.0 open design question
below — see that entry for what was proposed and what superseded it.

- **R13 — the dataset is immutable; the recipe is plural.** `manifest.yaml` (library + experiment) is
  content-addressed and write-once; `processing.yaml` is what to *do* with it, many per dataset.
  `run_id = H(dataset ⊕ processing ⊕ kb ⊕ workflow)` and `.seqforge/pipeline/<run_id>/`. The old
  `provenance_id(manifest_hash, kb, wf)` folded intent into the manifest hash, so two recipes over one
  dataset **collided on a single id** and compose's fixed output path silently overwrote the first with
  the second — the collision case was exactly the use case the split exists for. Hash churn cost:
  zero, verified (no pinned hash literals anywhere). The module graph enforces the split:
  `models/dataset.py` and `models/processing.py` never import each other, and a test parses the ASTs
  to say so.
- **R14 — the instructable surface is closed; the line is parse vs count.** `KB_PARSE_KEYS` + a
  `Backend` validator: the KB can no longer *express* a count key. `soloFeatures` left three specs;
  `quantMode` and `outSAMtype` left `bulk-rnaseq-pe`, whose `backend.params` is now `{}` — meaningful,
  not degenerate. `params_gate` goes from two checks to four (disjointness, coverage, per-owner
  faithfulness, cross-derivation), because disjointness alone is the decorative bug in reverse: it
  proves the owners cannot disagree, not that either key arrives.
- **R15 — produce every answer rather than ask.** `soloFeatures` defaults to scRecounter's five
  (`Gene GeneFull GeneFull_ExonOverIntron GeneFull_Ex50pAS Velocyto`, no SJ). One alignment, five
  counting rules, one pass. **The `e2e.py` override is deleted**: `kb e2e-introns` runs on the
  compiler's own params, so the fixture that *priced* the 40.7 % defect is now the gate that
  *prevents* it, and `gene_signal_lost` measures a counterfactual instead of our own bug.
- **User processing instructions**, via the sole LLM touchpoint. Precedence: CLI flag >
  `--instruction` document > policy default, with `user_confirmed` finally written. Document **role**
  decides the basis — the flag it arrived under, never its filename, and never the model's reading of
  imperative-vs-declarative mood (that classification has no quote to check it against). Only an
  `--instruction` doc may set `processing.*`; a downloaded methods PDF can never steer the pipeline.
  Prose **promotes, never narrows**: an instructed feature is unioned with the default and promoted to
  primary, so a hallucinated instruction can only mislabel the primary matrix, never destroy signal.
- **`10x-3p-gex-v3.1` exists.** v3 declared it a `processing_equivalent` twin from the start and the
  spec was never written, so the flagship example of the §12 rule was the one pair CI could not check.
  `EFO:0022980`, verified against live EBI OLS (FLAG-1). The benign branch now fires on real data for
  the first time: v3 and v3.1 tie exactly, both recorded, zero questions.
- **Fixed: a §12 false-benign this repo shipped.** `canonical_backend` sorted list-valued params,
  justified solely by `soloFeatures=[Gene,GeneFull] == [GeneFull,Gene]`. After R14 the only list left
  is splitseq's **positional** `soloCBwhitelist`, so a spec and the same spec with its rounds permuted
  canonicalized byte-equal: two chemistries that parse reads *differently*, declared benign twins. It
  never fired only by the accident that `round1 < round2 < round3` alphabetically.
- **Fixed: `AssertionDraft.field` had no allowlist.** `DEFAULT_FIELDS` was only interpolated into the
  prompt; `verify` never compared a returned draft against it, so
  `field: "processing.params.outFilterMismatchNmax", value: "10"` passed both R5 checks on a real
  quote. Nothing exploited it only because no Assertion→`fill` path existed — and this release builds
  that path, so the allowlist landed first.
- **Fixed: a latent nondeterminism in `escalate`.** `max(tie, key=(rung, value))` broke exact ties by
  KB dict iteration order. Benign twins tie exactly by construction, so `candidates[0].technology`
  could flip between runs of an unchanged input. Which twin represents the class is arbitrary; it must
  still be arbitrary the *same way* every run (R7).
- **Fixed: dead contracts.** `required_config` said "checked in CI" and was checked nowhere; the §12
  biconditional was asserted by two docstrings and computed by nobody (`backend_identical` had zero
  callers); `params_gate` picked its param block as "whichever of solo/bulk is a dict", so a bulk
  config with a stray solo block reported "config drops KB param 'quantMode'" — a real failure pinned
  on the wrong cause.
- **New:** `seqforge processing new|validate|hash`; `compose --processing/--assembly/--annotation`;
  `harvest {normalize,extract} --instruction`; `processing new --quantify`. `manifest fill` **drops**
  `--assembly/--annotation` — choosing a reference is not something you learn by probing bytes.
- **New:** `Blocker(GENOME_ORGANISM_MISMATCH)`. A user may instruct `hg38` on a worm dataset: that
  contradicts no byte (probe cannot see organism), it contradicts `experiment.organism`. A
  wrong-but-*valid* assembly is the worst failure this system can produce — it aligns, exits 0, and
  emits a plausible matrix in the wrong coordinate space. `fill` set `ncbi_taxid` from the organism
  while `assembly` came from a flag, and nothing ever compared them.
- `KB_VERSION` → 2026.7.1, `WORKFLOW_VERSION` → 2026.7.1, `EXTRACT_PROMPT_VERSION` → 2026.7.2.

**Verified on arc (job 2680654, ce11 + WS298, commit ac887c7).** The gate ran on the compiler's own
params with the override deleted and reproduced the pre-registered number exactly:
`gene_signal_lost = 0.407`, `composed_soloFeatures = [Gene, GeneFull, GeneFull_ExonOverIntron,
GeneFull_Ex50pAS, Velocyto]`, `primary_feature = Gene`. Gene counted **none** of the 788 injected
intronic reads (1186 vs 1212 exonic); GeneFull recovered 1940 of 2000. The 40.7 % is now a
*counterfactual* measured on a run that did not throw the signal away.

**A second defect the same run surfaced, at scale.** At 10⁶ reads, Gene-only produced **207 spurious
(cell, gene) pairs where GeneFull produced 0** — intronic reads landing in an *overlapping* gene's
exon. So exon-only counting on a nuclear library does not merely lose 40.7 % of it, it also
misassigns. Invisible at the gate's 2 000-read scale, which is where its `n_spurious_pairs == 0`
assertion is calibrated; the assertion was left alone rather than loosened to fit a benchmark.

**The Velocyto decision rule is retired — by the maintainer, not by a measurement (2026-07-15).**
Velocyto is unconditional. Saying this plainly because the rule was pre-registered (">2× wall-clock
or over the `mem_gb` hint ⇒ drop to four") and a retired rule must not be mistaken later for a rule
that was tested and passed. It was not tested. `--quantify` still narrows for anyone who wants to.

**Peak RSS at 10⁴ × hg38 is UNMEASURED and deferred to real human data.** The ce11 fixture cannot
answer it, and a green ce11 number would be actively misleading: peak RSS was 2.804 GB at 2 000 reads
and 2.809 GB at 10⁶ reads — a 500× read increase moved it 5 MB, because that 2.8 GB is the *ce11
index* sitting in RAM and the counting is a rounding error on top of it. "All-5 fits in 32 GB on
ce11" is a fact about ce11's index size, not about Velocyto. On hg38 the index alone approaches the
32 GB hint before a single read is counted, which is the only configuration where the rule could ever
have bitten. The instrument is built and waiting (`kb e2e-introns --quantify` + the `cost` block
reporting `star_wall_s` / `star_peak_rss_gb`); what generalizes off ce11 is the *slope* — bytes per
read per feature-set, additive with a genome-sized constant — not the absolute number.

One correction to the rule's own wording: `mem_gb` is not a workflow-module property. It is
`ResourceHints.mem_gb` on the **processing manifest** (default 32), i.e. it lives in the recipe —
which is where a resource request belongs under R13.

## 2026.7.0 — 2026-07-14

> **~~OPEN DESIGN QUESTION (needs the maintainer)~~ — RESOLVED in Unreleased (R13/R14/R15).**
> The diagnosis below stands and is kept verbatim as the record of finding the defect *before* the
> acceptance run. Its **proposed remedy was withdrawn, not implemented**, and the reason is worth
> keeping: it would have made an unknown prep a `Question` (exit 4) — trading a silent wrong answer
> for a question. R15 buys back both by counting every feature, so the question never needs asking.
> Shipping an exit-4 that never needed to fire trains people to route around exit codes; the path was
> deleted before it was written. The `nuclei | cells` fact is likewise not needed: it would only
> reorder which matrix is primary, never decide what gets computed.
>
> **`soloFeatures` is misfiled as chemistry.**
> Surfaced by pre-registering PRJNA1027859 from declared metadata — before the run, without touching
> the data — and then **priced at 40.7 % silent signal loss** by `kb e2e-introns`.
>
> The acceptance case is single-**nucleus** RNA-seq. Nuclei are full of unspliced pre-mRNA, so most
> reads are intronic and STARsolo must count them with `--soloFeatures GeneFull`. The KB bakes
> `soloFeatures: [Gene]` into `10x-3p-gex-v3.backend.params` — but 3′ v3.1 chemistry is
> **byte-identical for cells and nuclei**: what differs is the RNA population, a property of *sample
> prep*, not chemistry. Compiling the case today emits `Gene` and silently undercounts; STARsolo exits
> 0 and the matrix merely looks thin — the same failure shape as a strand inversion.
> Compounding it, `processing.quantification` is **decorative**: policy sets it to `"gene"`, writes it
> to the manifest, and compose then ignores it and reads the KB instead. Two sources of truth for one
> decision, unable to disagree only because one is never consulted.
>
> Proposed (not applied — it changes the KB/manifest contract): `backend.params` says how to **parse**
> reads (soloType/CB/UMI/whitelist/strand); `processing` says what to **count** (soloFeatures), driven
> by an asserted `nuclei | cells` sample-prep fact — with an **unknown prep on a single-cell chemistry
> becoming a Question (exit 4), never a silent `Gene` default.**

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
  round barcodes + two fixed 30 bp linkers; Parse Evercode deliberately deferred to its own future
  entry). All pass `kb roundtrip`.
  **`splitseq`'s `soloStrand` FLAG is now CLOSED — `Forward`, derived, never recalled.** The RT primer
  (`/5Phos/AGGCCAGAGCATTCG` + bc1 + dT(15)VN) anneals to mRNA and RT extends its 3′ end, so
  first-strand cDNA is antisense with bc1 at its 5′ end; rounds 2/3 ligate onto that same 5′ end.
  Assembling the paper's own Table S12 oligos 5′→3′ **reconstructs both linkers base-for-base and
  lands Read 2 on exactly 10+8+30+8+30+8 = 94 cycles** — so the spec is reproduced from primary
  oligos, not copied from a diagram. Read 2 therefore reads the antisense first strand; Read 1 is its
  mate = sense = `Forward`. Both primer types share that 5′ architecture and differ only in the
  priming tail, which is why random-hexamer priming does **not** destroy strandedness here (the trap:
  random-primed *bulk* RNA-seq genuinely is unstranded; this is not that). Corroborated independently
  by the authors' own pipeline (read strand vs GTF strand, no flip == `featureCounts -s 1`; TSO found
  at the **start** of read1 with no revcomp) and by scg_lib_structs running `Forward` on SRR6750042 —
  GSE110823 itself. An adversarial search found **no** source claiming Reverse. Honest caveat kept:
  most pipelines never *chose* — they inherited STARsolo's Forward default, and silence means Forward
  by accident, not by verification; rung 6 (GSE110823 both ways, bounded) remains decisive.
  Also captured: a **v1/v2 discriminator** from Read 2 alone (v1 = 30 bp linker2, Round1 at 86–93;
  Parse/v2 = 22 bp linker2, Round1 at 78–85 — which is why published `soloCBposition` quadruples
  disagree in the wild: different chemistries, not typos), and that there are **96 Round1 barcodes but
  only 48 RT wells** (dT and hexamer in one well carry different barcodes, paired *i* ↔ *i+48*) — a
  demultiplexer treating all 96 as distinct doubles the apparent cell count at half the depth.
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
- **`kb e2e-introns` — the GeneFull gate (closes the design's intron-rich caveat).** Yeast is nearly
  intron-free, so the sacCer3 e2e certifies neither counting rule. This injects known **intronic**
  reads — what a single-nucleus library actually contains — and asserts the rules disagree exactly as
  they must, from **one** STARsolo run (`--soloFeatures Gene GeneFull`, so the alignment is identical
  and only the counting rule differs). Measured on arc (ce11 + WS298): 1 212 exonic + 788 intronic
  injected; `Gene` = **1 186** (the exonic truth, counting no intronic reads); `GeneFull` = **1 940**;
  0 spurious, 0 inflated; resolve decided `10x-3p-gex-v3` from ce11 bytes unaided.
  **It also priced a real defect: `gene_signal_lost = 0.407`** — `--soloFeatures Gene` silently
  discards 40.7 % of a nuclear library, and `composed_soloFeatures` was `[Gene]`, i.e. the compiler
  would emit exactly that. Honest about scope: the gate **overrides** that one param (the KB declares
  `[Gene]`), so it proves the GeneFull path works and quantifies the cost — it does **not** prove the
  compiler would choose GeneFull, because today it cannot. See the open design question above.
- **CLI** — `probe` (bounded; never returns 3/4 — it only observes), `io onlist list|show`,
  `io peek`, `io resolve --check-reads`, `resolve score --json` (`--explain` emits the evidence
  matrices), `manifest fill|validate|hash`, `compose`, `kb e2e`, `kb e2e-introns`,
  `harvest normalize|extract|verify` (exit 3 on a Blocker or failed gate, 4 on an open
  Conflict/question or a claim that fails the tripwire), `eval list|run`, and
  `hook install|check|pre-tool-use|post-tool-use|stop`.
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
- **`io/` — the network surface, implemented and live-verified.** `io peek` range-reads the head of a
  remote gzipped FASTQ: **65 536 bytes from a 517 597 461-byte file = 0.0127 %**, yielding 4 real
  records with true lengths. It asserts **HTTP 206**, never `Accept-Ranges` — a server may advertise
  ranges, ignore the header and answer 200 with the whole file, and trusting the advertisement is
  exactly how a "bounded" read becomes a 40 GB download; a 200 is therefore a refusal. The budget caps
  **decompressed** bytes (`decompress(blob, max_length)`), so it is a real R3 bound rather than a
  compressed-byte proxy — and a zip bomb is a non-event.
  **The valuable half is not fetching — it is detecting what the archive threw away.** `fasterq-dump`
  skips technical reads **by default**, so a 10x barcode read routinely vanishes from the published
  FASTQ while remaining inside the `.sra`, leaving something that looks like ordinary single-end
  RNA-seq and is silently unprocessable as single-cell. `io resolve --check-reads` catches it from two
  metadata calls, before a byte is downloaded (R11 rung 0) → exit 4. Verified live on SRR9170959: ENA
  publishes 50.0 bases/spot in **one** file while declaring `library_layout=PAIRED`; SRA reports
  `nreads=3`, `[50, 50, 10]`, `readTypes=TBT` → 60 bases/spot discarded. That NCBI and ENA disagree on
  `base_count` for one run is not a bug to reconcile: they are two truths about what the file holds,
  and **the disagreement IS the signal** (R6). Also verified live: the SuperSeries trap — GSE140511
  declares no SRP, so eutils returns zero and a non-recursing resolver loses the dataset *while
  reporting success*; recursion finds 2 sub-series → 2 studies.
  Research corrected three assumptions before they shipped: SDL is form-encoded not JSON and has no
  `filetype=src`; `sra-pub-src-1/-2` are public, not requester-pays; and SDL is usually the **wrong**
  remedy (originals exist for select studies only), so the remedy now names
  `fasterq-dump --include-technical` first. Two endpoints we depend on are undocumented, so both are
  pinned behind parsers with offline tests over real captured payloads.
- **`hooks/` — the rules as mechanism (design §4.2).** `CLAUDE.md` can *say* "never read a whole
  FASTQ"; only a hook can stop one. `PreToolUse` denies an unbounded stream (R3), an absolute path
  into a manifest (R9), and ad-hoc access to a held-out root (§8) — while allowing the sanctioned
  `seqforge` verbs, which are bounded by construction. `PostToolUse` re-runs `manifest validate` after
  any manifest edit (R2: the model does not grade its own work). `Stop` refuses to end a turn while
  `questions.md` is non-empty, yielding on `stop_hook_active` so a guard can never hang the agent.
  The logic is typed and tested rather than living in a shell script, because **a guard that silently
  never fires is indistinguishable from one that always allows**. Held-out roots come from out-of-git
  config + `SEQFORGE_CASE_*` env vars, so declaring an eval case automatically protects its data —
  one source of truth, and the public repo still carries the rule and never a path (asserted by test).
  Every guard is tested from both sides, which caught a real over-block: `s3://bucket/x.fastq.gz`
  contains `/bucket/x.fastq.gz`, so the R9 scan rejected the very URIs R9 *wants*.
- **`skills/` — brief §10's nine thin clients + a cross-product installer.** Each wraps deterministic
  verbs and carries the judgement, not just the syntax: what a stage must **not** do is the part worth
  writing down (`exam` must not volunteer a chemistry — it would usually be right, which is what makes
  it dangerous; `compose` must never report a skipped gate as passing; `resolve` acts only on exit 4).
  `test_skills.py` pins them against the real Typer app — a stale skill is a confident instruction to
  run a verb that does not exist — and it earned itself immediately by catching that **`seqforge probe`
  was in the design's CLI surface and named by four skills, but never registered as a command.** Now
  implemented and verified.
- Design (`docs/design.md`), rules (`CLAUDE.md`), and rationale (`PROJECT_BRIEF.md`) in place.
