# seqforge — Design Document

**Status: implemented and green** (`pixi run check` passes; the pilot dataset PRJNA1027859 compiles
end to end to a manifest + Snakefile). This is the authoritative design for seqforge's schemas and
algorithms **and the rationale behind them** — it absorbed the old `PROJECT_BRIEF.md`, which is gone.
[`../CLAUDE.md`](../CLAUDE.md) is the enforced rule set (R1–R11), where each rule names a file you can
open and run.

This document is deliberately kept out of the published docs site: it is the **agent-facing** source
of truth, carries a pushback appendix (§5) and genomics values still marked *unverified* (§7 FLAGS),
and must not read as settled guidance under a docs URL. Values that could not be verified from first
principles are collected under **§7 FLAGS**; do not ship them without checking. **§9 is the honest
scope delta** — what is designed here but not yet built.

**Demo dataset.** `PRJNA1027859` is the pilot's worked example (see §8). It was designated a held-out
acceptance case; that was retired on 2026-07-15.

---

## 0. Governing metaphor and the pipeline

`seqforge` is **a compiler, not a chatbot**. Deterministic code owns every decision; the LLM has
exactly two jobs — (a) parse prose into span-verified `Assertion`s, (b) arbitrate ambiguity code has
*already flagged* (job (b) is modelled but its verb is unbuilt — §9). The compiler runs over **two
artifacts** — a `manifest.yaml` (what the data IS: immutable, content-hashed) and a `processing.yaml`
(the recipe: what to DO with it, plural) — and `resolve` has **two** resolvers, siblings that both
emit evidenced values and both can refuse:

```
probe(files)                    -> Observation      deterministic, no LLM, no network, bytes only
records(accession?)             -> ArchiveRecord    project/sample/experiment/run levels; OPTIONAL
harvest(documents)              -> Assertion        LLM extract, then deterministic span-verify
score(Observation, KB, hypo?)   -> Candidates x RoleAssignment, Conflicts, Questions   "what IS it?" from BYTES
resolve_metadata(FileIdentity,  -> samples x attributes, Conflicts, Warnings   "which sample, and what was it?"
                 records?, assertions?)                                        from RECORDS + PROSE
  ══> manifest.yaml   THE IR. One per dataset, hashed, write-once.
plan(Assertions, flags, policy) -> ProcessingSection   precedence: flag > instruction > policy
  ══> processing.yaml   THE RECIPE. Many per dataset. Sparse; empty is legal.
compose(manifest, processing)   -> Snakefile + config.yaml + units.tsv   deterministic — THE product
```

`run` (alias `compile`) chains all of this in one headless pass, stopping at the first refusal. It is
an **orchestrator** over the deterministic verbs, not a monolithic `compile(Decision)`: there is no
`Decision` compile-input, and `run` decides nothing the individual verbs would not. Stage → rule map
(CLAUDE.md R1–R11): probe ⇒ R3/R8/R9; harvest ⇒ R1/R2; score/resolve ⇒ R2/R4/R9; compose ⇒ R1/R7.
All stages ⇒ R5 (disk is state), R6 (CLI is the API).

### 0.1 Why this is one package, not two

Two ArcInstitute tools bracket this problem and nothing joins them. **SRAgent** turns an accession +
its prose into structured metadata (organism, tissue, single-cell, 10x version). **scRecounter**
fetches, runs STARsolo, and emits a count matrix — but it does **not trust SRA metadata at all**, so
it *grid-searches* STAR parameters (whitelist version, CB/UMI length, strand, reference) by aligning
reads under many combinations and picking the winner by fraction of valid barcodes. It re-derives, by
brute force on real alignments, facts that were sitting in prose SRAgent already read. The gap between
the two is not a missing feature; it is a missing **interface**.

The manifest is that interface, and once it exists the two tools are one compiler: `harvest` is
SRAgent's job demoted to *proposing*, `compose` is scRecounter's job driven by a *decided* manifest
instead of a search. The interface buys three things neither has: **span verification** (extracted
metadata carries a tripwire, not a confidence score — R2); **cheap eager verification** (one ~100 ms
onlist check where scRecounter spends a subsample alignment — §3); and **refusal** (an undecidable
dataset yields a `Blocker`, not a best-scoring guess — R2). And because processing is a separate
artifact, uniform reprocessing becomes *one recipe among many*. The claim is architectural, not a
track record: seqforge has compiled the worm pilot end to end, but has not yet executed a pipeline on
real reads at scale.

---

## 1. Pydantic v2 model hierarchy

Target py3.12+, `pydantic>=2`, `mypy --strict` on `models/`. Concrete `Evidenced[...]` subclasses
precede any model that references them (a parametrized generic subclass is a class statement, not a
deferred annotation), so the module concatenates and compiles top-to-bottom.

### 1.0 Scalars & controlled vocabulary

Scalars and validators live in [`models/base.py`](../src/seqforge/models/base.py). The
load-bearing ones: `Uri` runs an `AfterValidator` that rejects any absolute or local filesystem path
(`/…`, `~`, `file:///`, `C:\…`, UNC `\\host\share`) — a manifest URI must be a relative path, a
non-file scheme (`s3://`, `gs://`, `https://`, `sra:`), or a bare accession, never a path to one
machine. `Basis` is a closed set of **four** — `observed` (from bytes), `asserted` (from humans/DBs),
`inferred` (derived), `user_confirmed` — and `Rung` is the escalation-ladder step `0..7`. Policy
defaults are stamped `basis="inferred"` with an `evidence` ref naming the policy rule; whether to add a
distinct `policy_default` basis is left open (§5), and §1.6b's varying basis makes it unnecessary in
practice.

### 1.1 `Evidenced[T]` — the three-truths carrier (R4)

[`models/base.py`](../src/seqforge/models/base.py) (`Evidenced[T]`; the concrete
`Evidenced[…]` specializations are stable named `$defs` in
[`models/evidenced.py`](../src/seqforge/models/evidenced.py)). It wraps **every** interpretive manifest
field so a value never travels without its provenance: `basis` (how we know it), `evidence` (ids of the
Observation / Assertion / onlist-check records that justify it), `confidence`, and `rung` (the cheapest
ladder step that settled it, a provenance + eval signal). Disagreement across bases is a first-class
`Conflict`, never a silent merge.

`frozen=True` makes a validated field immutable (R2: nothing edits a value post-validation).
Manifests are hashed by canonical serialization, never `hash()`, so the unhashable `list` field is a
non-issue.

### 1.2 `Observation` — probe output, per file, cached by sha (R3/R5); **role-free**

`Observation` is the *observed* leg of the three truths. It reports composition, segmentation,
distinct-value ratios, header grammar and integrity — and assigns **no roles**. The segment taxonomy
is structural (`constant` / `random` / `homopolymer`); mapping `constant→linker/TSO`,
`random→CB|UMI|cDNA`, `homopolymer-T→polyT` is the resolver's job, scored and second-guessable.

Model: [`models/observation.py`](../src/seqforge/models/observation.py). The decisions the field
list can't show: a distinct-value window is **SUPPORTS-only, never a gate** — it is depth-dependent, so
normalize with `4^len` and the sampled-N before reading it (§4.1); `ABSTAIN` ("the probe cannot see
this signal") is first-class and distinct from "the signal is absent", and it never gates; `n_distinct
> 1` on a fixed-geometry read means a pre-trimmed upload → `PRETRIMMED_VARIABLE_LENGTH` Blocker, and a
truncated member → `TRUNCATED_GZIP`; `estimated_total_reads` is extrapolated from **compressed**
bytes-per-read (or the gzip ISIZE), never a full scan, because the naive decompressed form undercounts
by the ~3–5× compression ratio; and `FileIdentity.local_uri` is the one place a local path is allowed
— it records where probe read bytes and is NEVER copied into a manifest.

### 1.3 `Assertion` — LLM-facing draft split from stored (R1/R2)

The LLM cannot count character offsets, so it emits only `{doc_sha256, quote, context?}`;
deterministic code searches the normalized document for the quote, computes offsets, and sets the
two verification flags. This makes the P4 tripwire *fail-closed* instead of *false-rejecting*.

Model: [`models/assertion.py`](../src/seqforge/models/assertion.py). `AssertionDraft` —
`{field, value, span:{doc_sha256, quote, context?}, llm_confidence}`, no offsets, value a plain string
— is the LLM's only structured-output surface; code searches the normalized doc for the quote, computes
the offsets, composes the stored `Assertion`, and **owns** both flags (`span_verified`,
`entailment_ok`).

`span_verified` catches *fabricated provenance*; `entailment_ok` catches a *real quote mis-attached
to a wrong value* (the more common LLM failure — a verbatim "single-cell RNA-seq" span pinned to
"10x 3′ v3.1"). Both must hold before an Assertion flows into `manifest fill`.

### 1.4 `Conflict` — first-class, surfaced (R4)

Model: [`models/conflict.py`](../src/seqforge/models/conflict.py). A surfaced disagreement
between truths, never auto-picked: `positions[]` (min 2) generalizes the common observed/asserted
pair, `kind` is derivable from the position bases, and `resolution` records who decided (`code` /
`user` / `benign_equivalence`).

`status="benign"` is the §2.4 escape hatch: two confusable KB entries that emit *identical*
`backend.params` (v3 vs v3.1) are recorded together with zero questions. `decidable_by` includes
`onlist` — the mechanism §2.4 actually uses to split Multiome/GEM-X from v3.

### 1.5 `Blocker` / `Warning` — refusal as an exit code (R2)

Model: [`models/blocker.py`](../src/seqforge/models/blocker.py). A `Blocker` is a structured
refusal emitted alongside a nonzero exit, and is **always fatal** — advisory diagnostics are a separate
`Warning` type (non-blocking, exits 0), so branching code never inspects a severity field to learn
whether it blocks. Every Blocker carries an actionable `remedy` and a `subject` that is a basename /
dotted path / dataset id, never an absolute path. The closed `BlockerCode` set is
`MISSING_TECHNICAL_READ`, `TRUNCATED_GZIP`, `CORRUPT_FASTQ`, `UNSUPPORTED_TECHNOLOGY`,
`PRETRIMMED_VARIABLE_LENGTH`, `NO_VALID_ROLE_ASSIGNMENT`, `ONLIST_VERIFICATION_FAILED`,
`UNRESOLVED_CONFLICT`, `MISSING_CONTROLLED_VOCAB`, `ABSOLUTE_PATH`.

`MISSING_TECHNICAL_READ.remedy` is operable: *"re-fetch with `fasterq-dump --include-technical`, or
pull the original submitted files `sra-pub-src-*` via the SRA Data Locator / SDL API."*

### 1.6 `DatasetManifest` — two truths, two authorities (R7/R11)

> **The "three truths / three sections" pun did the damage, and it is gone.** R4's three
> truths are the three *bases*; nothing in R4 ever depended on there being three *sections*.
> But both were three, so `processing` inherited the grammar of a truth — `Evidenced` fields,
> an "authority", a uniform `basis="inferred"` stamped on by construction — and then compose
> read almost none of them (4 of its 6 fields had no reader at all). A field that is never read
> cannot produce the `Conflict` R4 promises. §1.0 listing **four** bases against three sections
> was the tell. Intent now lives in §1.6b, in its own artifact.

Model: [`models/dataset.py`](../src/seqforge/models/dataset.py). The manifest is a finished
assay — what the bench did — with TWO truths and ONE lifetime, **immutable**. `compose()` is a pure
function of `(DatasetManifest, ProcessingManifest)`, and `validate()` also enforces referential
integrity (every experiment `file_uri` ∈ the library inventory). The decisions the field list can't
show:

- **`LibrarySection` — physical truth, authority = evidence, one decision → one envelope.** `chemistry`
  is the *only* `Evidenced` field: it is the joint optimization over (which technology, which file is
  which read), carried as an equivalence class (`EvidencedChemistrySet`) because benign twins (v3 +
  v3.1) are recorded together. Everything else *follows* from it — `assay` is the same answer in EFO's
  vocabulary, `read_layout` is the KB's structure filled with measured lengths, `files[].read_id` is
  the assignment half of the same optimization (so it is **not** `Evidenced`; the score rides on
  `chemistry`). They each used to carry their own envelope, and the pilot's manifest showed what that
  bought: `confidence: 0.750672` printed four times, identical, because it was always one number about
  one decision. Four envelopes filled from one variable cannot disagree — they were never four truths,
  and R4 asks only that a value not travel without its provenance, which one honest envelope does.
- **`SampleGroup.attributes` is keyed by NCBI's 960 harmonized BioSample names**, with NCBI's own
  definitions ([`io/attributes.py`](../src/seqforge/io/attributes.py)), and the validator refuses
  anything else. Two typed fields (`tissue`, `condition`) used to sit here and both were wrong:
  `condition` was *ours* — no archive defines it, and a field named "condition" accepts anything you can
  call a condition, so a model duly filed worm husbandry into it — and two typed fields cannot hold
  `strain`, the only structured field separating the pilot's wild-type samples from its daf-2 mutants.
  An open dict over a controlled vocabulary rather than 960 pydantic fields, because a typed list
  mirroring somebody else's vocabulary rots the moment they add to it.
- **`Study` is NOT `Evidenced`** — none of it is an interpretation; the record says the title is X and
  we copy X exactly as we copy a sha256. The abstract is deliberately absent: it is prose, it belongs
  in a document a quote can grep into, and pasting a paragraph of English into a content-addressed
  manifest would make the dataset's identity depend on it.
- **`DatasetProvenance` omits `workflow_version`** on purpose — the assay happened before we had an
  opinion about which rules would run over it; that belongs to the processing manifest.

### 1.6b `ProcessingManifest` — intent, plural (R11)

The flags to §1.6's IR. Many per dataset; that plurality IS the design. `basis` here records **who
decided**, not how we know — which is why `user_confirmed`, unwritten anywhere else in seqforge since
the beginning, is the basis this section exists to carry.

Model: [`models/processing.py`](../src/seqforge/models/processing.py). The decisions:

- **`quantification` is a discriminated union** (`SoloQuant | BulkQuant`) and is no longer decorative —
  `params_gate` fails if the emitted config disagrees with it. `SoloQuant.features` is **ordered**
  (`[0]` primary) and **defaults to all five** solo features; a validator enforces "no duplicates" and
  "Velocyto requires Gene" (a real STAR constraint no enum can express). `BulkQuant` needs no
  strandedness knob — `--quantMode GeneCounts` already emits all three strand columns, so there was
  never a decision to make there.
- **`basis` records *who decided*:** a CLI flag or an `--instruction` doc → `user_confirmed`, policy →
  `inferred`. The two `user_confirmed` tiers differ only in *precedence*; the channel lives in
  `evidence`. That is why §1.0 needs no `policy_default` basis — once a section carries a *varying*
  basis, `inferred` + a ref naming the rule is distinguishable by inspection.
- **`dataset is None` ⇒ a template**, portable across 10⁴ datasets (a mandatory pin would destroy
  uniform reprocessing); set ⇒ **bound**, and `compose` refuses a mismatch with a Blocker and never
  auto-repins. `compose` ALWAYS writes the bound form it used to `processing.lock.yaml` — disk is
  state, not input.

`run_id = H(dataset_hash ⊕ processing_hash ⊕ kb_version ⊕ workflow_version)` — the pairing is
recorded here, at compile time, never inside either input. The old `provenance_id(manifest_hash, kb,
wf)` could not express it: with intent folded into the manifest hash, two recipes over one dataset
**collided on a single id**, and compose's fixed output path meant the second silently overwrote the
first. The collision case was exactly the use case the split exists for.

### 1.7 Score / compile output models (were missing — Blocker A)

The four-stage contract emits ranked candidates, decisions, questions, and compiled configs; each is
a first-class Pydantic type so `schema export` references only types that exist, and every stdout
object round-trips through JSON Schema.

Models: [`models/`](../src/seqforge/models/) (the score / resolve / compose / run / eval result
types). Two decisions worth stating: `TechScore` is
**JSON-safe** — no ±inf ever appears in serialized output, `status="forbidden"` means a
requires/excludes gate failed and `status="scored"` carries the finite normalized value; and
`ArbitrationRequest` / `ArbitrationResponse` are the opt-in, still-unbuilt LLM job-(b) schemas, where
the response references a position by *index* and re-derives no values.

### 1.7b The LLM provider is pluggable (implemented)

`harvest extract` is the only LLM touchpoint, and nothing downstream trusts it: code re-greps every
quote, checks entailment, and validates the batch against `AssertionDraft` before anything reaches a
manifest. **That is precisely what makes the vendor swappable** — the provider choice is about cost
and extraction quality, never about correctness guarantees. seqforge is therefore not locked to any
vendor:

Three providers ship: `anthropic` (strict `json_schema`, shape **guaranteed**; explicit
`cache_control`; default `claude-opus-4-8`), `deepseek` (`json_object` only, shape **not** enforced;
automatic prefix caching; `deepseek-v4-pro`, V4-Flash ≈3× cheaper), and `openai-compatible` (any
`base_url`, caller-supplied model). **The capability gap is contained, not papered over:** for
json-object providers the schema and a worked example travel in the prompt (DeepSeek *requires* the
word "json" plus an example), and `ExtractionResult.model_validate_json` is the gate — a wrong shape
fails the **whole batch** loudly rather than leaking a half-parsed assertion. One prompt serves every
provider, so `prompt_version` stays comparable; `ExtractorProvenance.model_id` records `provider/model`,
because the same prompt on a different model is a different extractor and evals must tell those runs
apart.

Selection is explicit-beats-implicit (`--provider` / `SEQFORGE_LLM_PROVIDER`, else auto-detect from
`DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY`), and **refuses rather than guessing** when no credential is
present — silently extracting with a different model than intended is a provenance bug.

### 1.8 JSON Schema export — the single source of truth

`Manifest.model_json_schema()` (2020-12) feeds validation (Pydantic itself) and docs. The **only**
LLM-facing schemas are `AssertionDraft` and `ArbitrationRequest`/`ArbitrationResponse`. Derive the
LLM-facing variant from the canonical one with a deterministic, CI-tested transform — never a
hand-maintained second schema:

emit with `ref_template="#/$defs/{model}"`, then for the provider "strict" subset rewrite `oneOf →
anyOf`, drop the `discriminator` keyword (keep the literal tag field), inline single-member `allOf`,
hoist `$ref`-sibling descriptions onto the referenced `$def`, strip `default`, set
`additionalProperties: false`, and put every property in `required` (nullability via the null branch);
numeric/`pattern` constraints stay in the canonical schema only (Pydantic enforces them at ingest, the
real guardrail), stripped from the LLM schema.

Generics are materialized via the named `Evidenced[...]` subclasses (stable `$defs`); no `value: Any`
anywhere (`Assertion.value` and `ConflictPosition.value` are `str`); discriminated unions live only
inside `Observation` (code-emitted, never LLM-produced).

---

## 2. KB `spec.yaml` schema

Layout: `kb/<tech>/{spec.yaml, README.md}` — one directory per technology. `README.md` is prose for
the LLM (how the assay works, aliases, gotchas, SRA failure modes); `spec.yaml` is machine-checkable.
The schema is a Pydantic v2 model (`extra="forbid"` on **every** model, including each test leaf), so
a typo'd key fails validation exactly where the DSL is executed. Rationale for Pydantic here is R1/R8
(single executable validator + self-test), **not** R1's LLM-output clause — `spec.yaml` is
human-authored and CI-validated, not LLM output.

### 2.1 The schema (abridged; full closed vocabularies)

Schema model: [`kb/schema.py`](../src/seqforge/kb/schema.py). The decisions the field list can't show:

- An `Element` must have **exactly one coherent addressing mode** — a fixed `[start,end)` XOR an
  `anchor` (a floating element, inDrop-class) XOR `min_len/max_len` — enforced by a model validator;
  `linker`/`fixed` require a `sequence`, and `end=null` (open) is allowed only for `cdna`/`gdna`.
- The `signature` tests are a **closed set, identical to the scorer's evaluators** (§3): `requires` are
  hard AND-gates (no `distinct_ratio`, which is depth-dependent), `supports` are additive positive
  evidence (onlist + distinct_ratio live here), `excludes` are anti-gates (any pass disqualifies).
  `read_count` counts biological + barcode ROLES, not raw files.
- `Backend.params` is the **chemistry-defining minimum only** (CellRanger-parity knobs are processing
  policy, below); the one interpolation token allowed anywhere in it is `{onlist:<alias>}`, validated —
  any other `{…}` fails.
- **`decidable_by` is a derived `Spec` property**, not a stored field: the union of `distinguishable_by`
  over the processing-divergent confusables. It used to be hand-typed on every spec, read by nothing,
  under a comment claiming a "CI-computed union" that no CI computed — so it drifted freely with nothing
  to notice (the same failure as `RegistryEntry.fetchable`, and `required_config` before it). The
  derivation reproduces all five hand-typed values exactly, which is how you know it was only ever a
  comment. `Spec._cross_refs` resolves every test `read`/`element`, every `anchor.ref_element`, and
  every onlist alias against the reads/elements block.

**What moved out of the KB backend:** CellRanger-parity knobs (`soloUMIdedup 1MM_CR`,
`soloUMIfiltering MultiGeneUMI_CR`, `clipAdapterType CellRanger4`, `outFilterScoreMin 30`) are
processing **policy**, not chemistry — they are applied at `compose` time from `processing`, so
`backend_identical` (below) stays sensitive to chemistry, not policy.

### 2.2 Worked spec — `10x-3p-gex-v3` (fixed offsets)

Worked spec: [`kb/specs/10x-3p-gex-v3/spec.yaml`](../src/seqforge/kb/specs/10x-3p-gex-v3/spec.yaml).
Fixed offsets — R1 is 28 bp = 16 bp CB + 12 bp UMI (`soloType CB_UMI_Simple`), R2 is open-ended cDNA.
Its `signature` shows the rung structure: `requires` are structural gates friendly to rungs 0–2 (read
count, 28 bp segment length, two random segments; **no** onlist, **no** distinct_ratio), `supports` add
the rung-3 `onlist_hit_rate` (weight 5) plus depth-dependent distinct-ratio priors, and `excludes`
anti-gate the Multiome `737K-arc-v1` list. The load-bearing part is `confusable_with`: v3.1 is
`processing_equivalent` / `[none]` (identical geometry, whitelist, and params → §2.4 benign, 0
questions); Multiome and GEM-X v4 share the 28 bp / 16+12 geometry and are `processing_divergent`,
separated only by onlist at rung 3 — **`10x-gemx-3p-v4` is a *required* entry** (without it the
flagship fails its own rung-0–2 under-declaration CI, §2.4); 10x 5′ is metadata/alignment-decidable
(antisense cDNA → `soloStrand Reverse`; read-undecidable when geometry and whitelist coincide, FLAG-7).

### 2.3 Worked spec — `splitseq` (combinatorial; pilot #3)

SPLiT-seq stresses **combinatorial multi-block indexing**: the cell barcode is the concatenation of
three round-specific 8 bp barcodes drawn from small (~96-entry) whitelists, separated by **fixed**
linkers. Unlike inDrop the positions are fixed, so no `anchor` is needed — the `anchor` path stays in
the schema for a future inDrop entry (coverage caveat, §6).

**Scope decision (implemented):** this entry models the **original published SPLiT-seq only**
(Rosenberg et al., *Science* 2018, doi:10.1126/science.aam8999). Parse Biosciences **Evercode** is a
separate, actively-versioned commercial descendant with different linkers/whitelists — it is
**deferred to its own future KB entry** and never conflated with `splitseq`. The read structure and
the two 30 bp linker sequences below are **pinned verbatim from scg_lib_structs** (CC-BY): the
[methods page](https://teichlab.github.io/scg_lib_structs/methods_html/SPLiT-seq.html) and the
["Published Manuscript" read-2 variant in issue #13](https://github.com/Teichlab/scg_lib_structs/issues/13).
Read 2 (94 cycles) is `[10 UMI][8 bc3][30 linker1][8 bc2][30 linker2][8 bc1]`; Read 1 (66 cycles) is
the cDNA. Still-open FLAGs: the round whitelists (register the real barcode files from scg_lib_structs
with URL + sha256), `soloStrand`, and the EFO CURIE. Per FLAG-3, `soloCBposition`/`soloUMIposition`
are **omitted from the KB and derived from the element coordinates at compose time**, never hand-typed.

Worked spec: [`kb/specs/splitseq/spec.yaml`](../src/seqforge/kb/specs/splitseq/spec.yaml) — `soloType
CB_UMI_Complex`, the three round whitelists concatenated in **positional** order (rounds map to CB
positions in order, so §2.4 must **not** sort this list). Its `signature` gates the two fixed 30 bp
linkers as `requires` `has_segment … kind: constant`, scores the three round barcodes as weight-3
onlist `supports`, and `excludes` a 16 bp 10x CB hit.

The `onlist_hit_rate` evaluator is **width-generic**: it reads the barcode length from the registry
entry (8 bp here → still a `uint32` pack, small sorted arrays), not a hardcoded 16 bp window (R4/§4.1).

### 2.4 The CI confusability matrix (computed purely from `reads` + `signature` + `backend`)

For every ordered pair `(A, B)`, CI derives three facts and validates the declared labels — there is
no hand-maintained truth table:

1. **`rung02_separable(A,B)`** — do the cheap structural probes (rungs 0–2, no onlist) separate them?
   Generate synthetic reads from `A.reads`, run the non-onlist subset of `A.signature` against
   `synth_B` and symmetrically. **Under-declaration guard:** `not separable ∧ B ∉ A.confusable_with`
   → **CI ERROR** (this is why `10x-gemx-3p-v4` is a *required* entry in the flagship's
   `confusable_with` — the 28 bp / 16+12 geometry is not rung-0-2-separable). **Over-declaration:**
   `separable ∧ B ∈ A.confusable_with` → CI WARNING.

2. **`onlist_separable(A,B)`** — for still-confusable pairs, do their `onlist_hit_rate` tests
   distinguish (rung 3)? True iff their whitelists have **low cross-hit** — computed by actual
   set-intersection (`np.intersect1d` over the two sorted packed arrays), **not** sha256 inequality
   (different hashes prove the files differ, not that the barcode sets differ).

3. **`backend_identical(A,B)`** — resolve each `backend.params`, expand `{onlist:alias}` to the
   registry **sha256**, canonicalize (sort keys — **never list order**), **and include the read→role
   placement** (`readFilesIn` order derived from `reads`). Two are identical iff those canonical
   forms are byte-equal. *(Including role placement matters: two techs differing only in which read
   is biological would otherwise be falsely labeled benign.)*

   **List order is significant and must not be normalized.** This rule used to say "normalize
   `soloFeatures` order", which was its only justification for sorting — and soloFeatures has since
   left `backend.params` (R11: it says what to *count*). What the sort would normalize now is the
   only list-valued parse param left, splitseq's `soloCBwhitelist: [round1, round2, round3]`, which
   is **positional**: rounds map to CB positions in order. Verified before deletion —
   `backend_identical(splitseq, splitseq-with-rounds-reversed)` returned **True**: two chemistries
   that parse reads differently, declared benign twins, one config emitted for both. It never fired
   only by the alphabetical accident that `round1 < round2 < round3`.

   Since R11, this predicate means exactly *"these two chemistries parse reads identically"* — which
   is what `processing_equivalent` should always have meant, and makes the rule **stronger**: two
   specs differing only in what they count are no longer distinguishable here, because that is not a
   chemistry fact at all. (This also resolves an inconsistency: §2.1 named soloFeatures in the
   canonicalization while the CellRanger-parity note argued `backend_identical` must stay insensitive
   to policy. The second was right.)

**§2.4 benign rule (the biconditional CI asserts):** `backend_identical(A,B) ⟺ relationship ==
processing_equivalent`. v3 vs v3.1 → identical module + `soloCB*/UMI*` + whitelist sha + strand +
role placement → benign → `distinguishable_by:[none]`. At runtime, a score tie between two candidates
with a CI-proven `processing_equivalent` edge **must not** escalate: record both ids into
`library.chemistry` (equivalence class) and ask **0** questions. `backend_identical == False` ⟹
`processing_divergent`, `distinguishable_by` non-empty ≠ `[none]`; listing `onlist` requires
`onlist_separable == True`. **`decidable_by` is derived** (a `Spec` property: the union over divergent
confusables of the minimal sufficient mechanism) and asserted equal to the declared list.

### 2.5 Synthetic generation (round-trip, R8) and adversarial fixtures

The generator is a pure function of `reads[].elements[]` only (never `signature`/`backend`, so the
round-trip is a real test, not a tautology): walk elements in order, drawing `barcode`+onlist from a
fixed synthetic cell pool of K barcodes reused across reads (so the recurrence signal is realistic —
**reconcile K with the probe window** so the distinct-ratio lands in-band, e.g. K≈2–5k over a 200k
window, not K=100), `umi` fresh-random, `linker`/`fixed` literal, `cdna` from a tiny bundled
reference, homopolymers as runs. Variable/anchored layout falls out of concatenation.

**Round-trip assertion:** `spec → synth FASTQ → seqforge probe → recovered layout`; `assert recovered
== declared`. **Adversarial variants, generated from the same block, assert the correct
Blocker/Conflict** (not a wrong answer): reverse-complement the read (probe recovers via the revcomp
onlist path + flags orientation); linker with 1–2 mismatches; drop the barcode read entirely
(missing-technical-read → `Blocker(MISSING_TECHNICAL_READ)` + remedy); a 26 bp R1 (must miss v3's
`segment_length:28` gate — proving 28 bp alone doesn't pick a chemistry); a fixed-cycle read with
`n_distinct>1` (pre-trimmed → `Blocker(PRETRIMMED_VARIABLE_LENGTH)`); SRA-normalized header
(`header_index` ABSTAINs, does not gate).

### 2.6 seqspec export/check

`kb seqspec-export` maps each `Spec` onto seqspec's `Assay/Read/Region` decomposition (using the
explicit `seqspec_region_type`), and `kb seqspec-check` runs `seqspec check` + diffs `seqspec index -t
starsolo` against `backend.params` — two independent derivations of the geometry must agree.
**Caveat (FLAG-6):** seqspec's starsolo emitter targets fixed `CB_UMI_Simple` geometry; whether it
emits `CB_UMI_Complex` position strings for combinatorial/anchored barcodes is unverified, so the
dual-derivation check is currently scoped to fixed-offset chemistries — confirm before relying on it
for `splitseq`.

---

## 3. Scoring, joint role-assignment, and escalation

### 3.1 Signature-test evaluators

`local_score(test, role, obs_f | tech) → [0,1]` (for `supports`) or `{PASS, FAIL, ABSTAIN}` (for
`requires`/`excludes`). **`ABSTAIN` is first-class** — "the probe cannot see this signal" ≠ "the
signal is absent" — and it **never gates** (a `header_index` ABSTAIN on an SRA-normalized corpus must
not reject every SRA dataset). Gate semantics: `requires` FAIL → cell forbidden; `excludes` PASS →
cell forbidden; `supports` → `Σ weight · score` with `Σ weight = 1` so a finite cell ∈ [0,1].

- **`segment_length`** — triangular around L for scoring; `PASS iff |mode − L| ≤ tol_gate` (the gate
  that separates 3′v3=28 from 3′v2=26). Open-ended cDNA uses a `min` variant.
- **`has_segment{constant|random|polyT|polyA}`** — mean per-cycle evidence over the window
  (constant: max-base fraction ≥ .9; random: near-uniform; polyT/polyA: base fraction ≥ min).
- **`distinct_ratio`** — `distinct/total` over the window. **SUPPORTS-ONLY, never a gate.**
  Depth-dependent (§5 pushback #2): only meaningful when normalized. `expect="high"` (UMI/cDNA)
  requires `4^len ≫ n_sampled`, else the ratio saturates below the band; `expect="low"` (CB) is
  conditioned on estimated reads-per-cell. It *proposes* CB-vs-UMI; the onlist confirms.
- **`onlist_hit_rate`** (rung 3, the hypothesis test) — width-generic (barcode length from the
  registry, not hardcoded 16), tests forward **and** reverse-complement **and** a small positional
  offset scan; records the winning `(orientation, δ)`. `floor = onlist.length / 4^len`; `score =
  clip((best − floor)/(min − floor), 0, 1)`; `PASS iff best ≥ min` (≈0.6). At Q30 discriminative
  power ≈ 0.9/floor ≈ 500:1. A revcomp hit means the barcode read is on the other strand → supply the
  revcomp whitelist file; it does **not** flip `--soloStrand` (which is the KB's 3′/5′ property).
- **`motif_present`** — fraction of reads matching an IUPAC motif (≤ max_mismatch) in an **inclusive**
  window; used for fixed linkers (SPLiT-seq) and, as an `excludes` test, to reject 10x when an
  internal linker appears in a barcode read.
- **`base_composition`** — element-addressable (via `_Seg`), so it can target a floating region, not
  just cycles 0–3.
- **`header_index`** — `ABSTAIN` if the header is SRA-normalized (and this is how probe *detects* the
  normalization); otherwise checks a per-file ~constant 8–10 bp index.

### 3.2 The evidence matrix (JSON-safe)

For technology `t` with roles `R_t` and files `F`:

```
M_t[r][f] = FORBIDDEN                          if any requires(r) gate FAILs, or any excludes(r) gate PASSes
          = Σ_{s ∈ supports(r)} w_s · local_score(s,r,obs_f)     otherwise   ∈ [0,1]
```

`FORBIDDEN` is the internal sentinel `float("-inf")` **for computation only**. Serialized (`--json`)
it is `{"status":"forbidden"}`; a scored cell is `{"status":"scored","value":x}` — **no `±inf` ever
crosses the JSON boundary** (JSON can't represent it and Pydantic's inf handling is lossy).

### 3.3 The joint optimization (cardinality-normalized)

An assignment `A: R_t ↪ F` is **injective** (each role a distinct file); leftover files are
unassigned at penalty. `valid(A)` = no forbidden selected cell ∧ dataset-level `requires` (mostly
*decomposed* into per-cell gates + injectivity — "exactly one 28 bp read AND one cDNA read" falls out
of the per-row gates plus injectivity; only genuinely non-decomposable globals need an explicit
post-check).

```
raw(t) = max_{A valid} [ Σ_{r∈R_t} ( M_t[r][A(r)] + β · prior(r, A(r)) ) ]
score(t) = raw(t) / |R_t|  −  (λ / |R_t|) · |F \ A*(R_t)|          # cardinality-NORMALIZED (pushback #3)
score(t) = −∞ (forbidden) if no valid A exists
best_tech = argmax_t score(t)
```

Normalizing by `|R_t|` makes a 2-role 10x and a 6-role SPLiT-seq comparable (an unnormalized sum
would bias `argmax` toward high-role-count techs). **The filename prior** `β · prior(r,f)` (1 if the
file's `_1/_2` token matches the role's conventional slot, else 0) has `β ≪ min(w)`, so it can only
break an **exact byte-tie** — never override bytes or flip validity. `fasterq-dump` `_1/_2/_3` say
nothing about R1/R2/I1, so filename is a weak prior, never a gate.

**Algorithm.** `|F| ≤ 4` (the common single-cell case): brute-force all ≤ `4! = 24` injective maps,
filter by `valid` (which natively enforces non-decomposable globals). Large `|F|`: Hungarian on
`−(M + β·prior)` with forbidden cells as `+BIG` edges, **then post-check that no selected edge is a
BIG/forbidden edge** (an all-forbidden row → the role is unfillable → `score(t) = −∞`, *not* a padded
assignment); re-check globals, escalating to Murty k-best if a non-decomposable global fails. Building
`M` over all techs is dominated by `onlist_hit_rate` (~100 ms) — which is exactly why the hypothesis
orders and prunes which onlists are computed.

### 3.4 How the asserted hypothesis enters (pushback #1)

The original `score(Observation, KB)` cannot implement rung 3 ("test ONE list") because the list to
load is a *metadata* fact, not recoverable from the bytes being identified. So:

```
score(Observation, KB, hypothesis: Assertion | None) -> ResolveResult
```

The span-verified `hypothesis` has exactly two **control-flow** effects and zero evidential ones: (1)
**selector** — picks the one rung-3 onlist/signature to evaluate first, enabling early-stop at ~100
ms; (2) **tie-break prior** — a sub-threshold nudge on evaluation order. It **never** enters `M`,
un-gates a forbidden cell, or wins a Conflict.

**Determinism.** For fixed `Observation`, the validity and finite score of any candidate that gets
computed is a pure function of the bytes; `hypothesis` only changes *which* candidates are computed,
at *what cost*, to *which rung*. So: same `Obs`, same `h` → identical output; same `Obs`, different
`h` → **identical winner whenever the bytes are decisive** (a wrong `h` just fails its `requires`
gate, blocks early-stop, forces the ladder down). Only where bytes are genuinely non-decisive (a
processing-divergent pair) may `h` break the tie — and then only via the escalation function at rung
0, recorded `basis: asserted` and **surfaced**, never merged into `observed`.

### 3.5 Escalation → `{Decision | Conflict | Question | Blocker}`

Inputs: ranked `[(t, score(t), A_t, rung_reached_t)]`, `Observation`, verified `Assertion`s, and the
CI confusability metadata. `margin = score(top) − score(second)`; `θ` a small tie threshold.

```
if no candidate passes requires:
    if a required PHYSICAL read is unfillable by any file      -> Blocker(MISSING_TECHNICAL_READ, remedy)   # rung 2
    elif gzip/integrity failed                                 -> Blocker(TRUNCATED_GZIP | CORRUPT_FASTQ)   # rung 2
    else                                                       -> Blocker(UNSUPPORTED_TECHNOLOGY)           # "unsupported", not a guess
divergent_ties = { t in confusable_ties(top) : not processing_equivalent(top, t) }
if margin > θ and divergent_ties == {}:                        -> Decision(top)                             # rung 2 or 3
elif divergent_ties == {} and the tie set is a DECLARED processing_equivalent group:
                                                               -> Decision(record ALL); Questions=0; Conflicts=0   # §2.4 benign, rung 3
else:  # processing_DIVERGENT tie
    for mode in top.decidable_by (ladder order):
        onlist(3)   : already tried in scoring
        metadata(0) : if a span-verified Assertion disambiguates & is byte-consistent -> Decision(asserted), SURFACED
        alignment(6): mini-align to a tiny reference (strand / 3'-vs-5') -> Decision if it resolves
        user(7)     : -> Question  => the batch DEFERS to a human via exit 4   (locked decision)
```

An **undeclared** sub-θ tie (a hole in the confusability matrix) is **never** record-both — it
escalates to a Question, so record-both fires *only* for a CI-declared processing-equivalent group.
**Conflict detection runs unconditionally in parallel:** if an observed value contradicts an asserted
one (e.g. asserted 26 bp vs observed 28 bp), emit a surfaced `Conflict`; the library section (authority
= evidence) takes the observed value, the Conflict stays attached, and `compile` refuses until
`user_confirmed`. **Exit-code contract:** an open `Conflict` or non-empty `questions.md` → exit **4**
everywhere (including `manifest validate`); exit **3** is reserved for hard `Blocker`s that no human
answer can clear.

### 3.6 Worked example — the benign v3/v3.1 case (synthetic ce11-shaped fixture; real dataset untouched)

Two files/sample, one 28 bp read + one ~90 bp read; `_1/_2` carry no role info; `header_index`
ABSTAINs on both.

| | f1 (_1) | f2 (_2) |
|---|---|---|
| len (mode, n_distinct) | 28, 1 | 90, many |
| distinct_ratio [0,16) | 0.08 (→ CB) | ~1.0 |
| distinct_ratio [16,28) | 0.98 (→ UMI) | — |
| composition | uniform ACGT, no linker | gene-biased |

Byte-derived roles: f1 → CB16+UMI12, f2 → cDNA. Candidate family (28 bp, 16+12) = {v3, v3.1, GEM-X
v4, Multiome}. Hypothesis "10x 3′ v3" selects `3M-february-2018`: forward hit 0.82 (>0.6), RC ≈ floor.
The evidence matrix leaves one valid injective assignment — CB→f1 (28 PASS, 3M .82 PASS, cell 1.00),
cDNA→f2 (90 PASS, 1.00); the cross-assignments f1-as-cDNA and f2-as-CB are both FORBIDDEN on
`segment_length` — so `raw = 2.00`, `|R_t|=2` → `score(3′v3) = 1.00`. **3′v3.1**
identical matrix → 1.00. **GEM-X v4** and **Multiome** require their own onlists which hit ≈ floor on
these reads → FORBIDDEN → excluded *(FLAG-8: certified by CI cross-hit, not memory; if a whitelist
were a superset the pair would become a divergent tie routed to `decidable_by`, not silently gated)*.
Tie {v3, v3.1} is CI-proven `processing_equivalent` → **Decision: record both**, `library.chemistry =
["10x-3p-gex-v3","10x-3p-gex-v3.1"]`, `basis: observed`, **Questions = 0, Conflicts = 0**, rung 3. The
adversarial variants off this same fixture — no technical read → `Blocker(MISSING_TECHNICAL_READ)`,
swapped `_1/_2` → identical manifest (byte-driven; β sub-threshold), lying "asserted v2" metadata → a
surfaced `Conflict` (library keeps observed v3/v3.1) — each land the outcome §2.5 and §3.5 predict.

---

## 4. CLI verb surface (Typer; JSON on stdout by default)

**Conventions.** Machine JSON to **stdout**, human logs to **stderr**, so stdout is a clean pipe.
There is **no `--json` flag** — JSON is the default and only machine format (`kb list` is the one
plain-text verb). Universal flags: `-C/--workspace`, `--kb`/`--kb-version`, `--no-cache`, `--offline`.
**Exit codes (uniform):** `0` OK · `1` ERROR (bug/IO, not a domain refusal) · `2` USAGE ·
`3` BLOCKED (≥1 `Blocker`) · `4` NEEDS_HUMAN (open `Conflict` / non-empty `questions.md`).
`probe`/`io peek` never return 3/4 — they only observe; refusal happens downstream when a validator
reads the observation.

```
seqforge
  probe FILES…                 det   bytes→Observation (--max-reads 200k, --max-bytes 256MB; never whole FASTQ)
  io peek URI | probe-remote URI | resolve ACC | records ACC | attributes | efo CURIE | h5ad SOLO_DIR
                               det†  the network + packaging surface; pooch + sha256-verified.
                                     probe-remote range-reads a bounded head → Observation, so a library is
                                     fingerprinted from a URL with no local file; provider md5 = content-address (#39)
  io onlist {list|show|write|pack}   det   the shipped barcode whitelists; `pack` adds a new one
  harvest normalize DOC…       det   PDF/text → seqforge/records/documents/*.txt canonical span space
  harvest extract              LLM   normalized text (+KB aliases) → AssertionDraft[] → verified Assertion[]
                                     ← the ONE batch LLM touchpoint; verify runs INSIDE it
  harvest verify DRAFTS --doc  det   re-check quotes back into canonical text + value-entailment (standalone)
  resolve score FILES          det   Obs+KB[+hypothesis:Assertion] → ResolveResult; NO LLM
  manifest fill | validate | hash    det   fill runs BOTH resolvers; validate → Blocker[] + exit; no-abs-path
  processing new | validate | hash   det   the recipe artifact (R11); unknown keys forbidden (R11)
  compose                      det   (manifest, processing) → Snakefile + config.yaml + units.tsv; gate §4.1
  run (alias compile)          LLM★  headless records→harvest→fill→processing→compose; --no-llm, --cpus, --assembly
  kb list|show|lint|roundtrip|e2e|e2e-introns|e2e-cost|e2e-fit   det  self-tests + ground-truth runs
  schema list | export [MODEL|--all]   det   dump JSON Schema (the single source of truth)
  eval list | run              det‡  evals harness: field-acc, false-accept, false-refuse, questions, tokens
  hook {pre-tool-use|post-tool-use|stop|install|check}   det   policy-as-mechanism (§4.2)
```

`†` io is the only network surface. `‡` eval invokes `harvest extract` only for prose cases; `--no-llm`
restricts to deterministic cases. `★` `run`'s only LLM touchpoint is `harvest extract`; `run --no-llm`
is a pure deterministic pipeline, and there is **no `--resume` flag** — re-running resumes through each
stage's content-addressed cache (R5). **Planned, not built (§9):** `resolve adjudicate` (LLM job (b),
modelled but no verb) and `resolve apply`; `kb confusability` / `seqspec-export` (the pairwise checks
run in the test suite; the seqspec *emitter* is unbuilt); the `journal` / `status` verbs.

Key behaviors: `probe` decompresses incrementally and **stops** at the budget (no whole-file seek).
`harvest verify` is a substring search for `quote` (LLM offsets are a hint) plus the value-entailment
gate. `io resolve` handles ENA/SRA/GEO and **SDL `sra-pub-src-*`** (the remedy path for the
dropped-technical-read Blocker); ENA-declared library fields are carried `basis: asserted`, never
observed. `estimated_total_reads` divides **compressed** size by compressed-bytes-per-read (or reads
the gzip ISIZE) — the naive decompressed form undercounts by the ~3–5× ratio.

### 4.1 The split compose gate + `kb e2e` (pushback #8)

`compose` is a pure function of the (dataset, processing) pair (no data on disk). Its gate has **three** parts, because
a dry-run cannot catch a strand inversion.

**`skip` is first-class (implemented).** Parts 1 and 3 depend on a toolchain seqforge does not own
(`snakemake`; STAR + liulab-genome + network), and the count-matrix run is a Linux/cluster operation.
A gate reporting `pass` because it never ran would let green CI be mistaken for coverage, so each gate
reports `pass` / `fail` / **`skip`**, and `params` — which needs no toolchain — always runs.

1. **Wiring** — touch zero-byte files at every path in the file inventory **∪ resolved-onlist cache
   paths** (so a whitelist declared as a Snakemake `input:` doesn't raise a spurious
   `MissingInputException`), then `snakemake -n` + `snakemake --lint`. Catches config, wildcard
   resolution, rule wiring.
2. **Params** — deterministic assertions that the emitted `--soloStrand` / `--soloUMIlen` / `--soloCBlen`
   and the `--readFilesIn` order (cDNA read before barcode read, derived from the role assignment)
   match the KB `backend.params`. These are the semantic bugs a linter *cannot* see and must **not**
   be attributed to `snakemake -n`.
3. **`kb e2e`** — the one real end-to-end run (**IMPLEMENTED and passing**): reads simulated
   from sacCer3 transcripts with injected barcodes/UMIs, driven through the *whole* compiler —
   probe → resolve (which must decide the chemistry from the bytes alone, no metadata hint) → fill →
   validate → compose → **STARsolo run with the composed params** → assert the matrix against the
   injected truth. Runs on a Linux compute node (STAR + liulab-genome); `skip` elsewhere.

   **The assertion is "accounted", not naively exact.** Real transcripts multimap — paralog/
   subtelomeric families (Y′/`YRF1`) that STARsolo legitimately drops — so `observed == injected` is
   unachievable, and demanding it would only teach us to weaken the gate. Instead the gate asserts what
   indicts *us*:
   - **0 spurious pairs** — never count a read for a gene it did not come from;
   - **0 inflated counts** — never invent a UMI (a dedup/geometry bug looks exactly like this);
   - **`unexplained_loss <= 2%`** — subtract STAR's own multimapper loss (read from `Log.final.out`);
     what remains is the compiler's error, and it must be ~0;
   - **strand sensitivity** — the same reads re-run under an inverted `--soloStrand` must collapse,
     or the gate could not have caught an inversion in the first place.

   Measured on arc (2 000 reads, 120 genes, 8 cells): resolve decided `10x-3p-gex-v3` unaided,
   recovered with **0 spurious / 0 inflated** and **0.7 % unexplained** (the rest is STAR's own
   multimapper loss), and the inverted strand **collapsed** — the proof the gate can catch an inversion.
   **Intron-rich fixture — CLOSED** (`kb e2e-introns`, ce11 + WS298): one STARsolo run with
   `--soloFeatures Gene GeneFull` (identical alignment, only the counting rule differs) counted `Gene` =
   the exonic truth alone (recovery 0.979) and `GeneFull` = exon + intron (0.97), again 0 spurious /
   0 inflated, resolve deciding the chemistry from ce11 bytes unaided.

   **That run measured a real defect, and the defect is now FIXED: `gene_signal_lost = 0.407`.**
   `--soloFeatures Gene` silently discards **40.7 %** of a nuclear library, and `composed_soloFeatures`
   was `[Gene]` — i.e. the compiler *would* have emitted it. The KB filed `soloFeatures` under
   `backend.params`, but 10x 3′ v3.1 chemistry is byte-identical for cells and nuclei: what differs is
   the RNA population, a property of **sample prep**, not chemistry. Surfaced by pre-registering
   PRJNA1027859 (single-**nucleus** RNA-seq) from declared metadata — before the run, without touching
   the data.

   **Resolved (R11).** `backend.params` says how to **parse** reads (soloType/CB/UMI/whitelist/strand);
   the **processing manifest** says what to **count** — and `params_gate` now fails if the emitted
   config disagrees with it (policy used to hardcode `"gene"` into the manifest and let compose ignore
   it for the KB: two sources of truth that couldn't disagree only because one was never read). The
   counting question is then *dissolved, not answered*: `soloFeatures` defaults to all five, so GeneFull
   is computed whether or not anyone says the prep is nuclear. The CHANGELOG's earlier remedy — an
   unknown prep raising a `Question` (exit 4) — was **withdrawn, not implemented**: it traded a silent
   wrong answer for a question, and the all-five default buys back both; an exit-4 that never needed to
   fire only trains people to route around exit codes. The fixture proves it landed — with its
   `[Gene, GeneFull]` override deleted it runs on the compiler's own params and asserts
   `composed_soloFeatures ⊇ {Gene, GeneFull}`, so the fixture that *priced* the defect is now the gate
   that *prevents* it.

   **Velocyto is unconditional — a maintainer decision (2026-07-15), not a measurement.** The
   pre-registered rule (">2× wall-clock or over the `mem_gb` hint ⇒ drop to four") is **retired**, and
   retired rather than tested-and-passed — the two leave the same trace unless someone records which
   happened, and this one was never tested. `--quantify` still narrows.

   **Peak RSS at 10⁴ × hg38 is measured** (`kb e2e-cost` on hg38, 2026-07-15): **34.7 GB at 100 M
   reads, 44.1 GB at 250 M**, so the flat regime ends between, and `peak_rss ≈ genome-sized intercept +
   slope × reads`. The ce11 fixture cannot answer this — peak RSS moved only 2.804 → 2.809 GB across a
   500× read increase, because 2.8 GB *is* the ce11 index and the counting is a rounding error on it, so
   a green ce11 number would be worse than none. Only the **slope** generalizes off ce11; the absolute
   figure needed the real hg38 index. The instrument is `kb e2e-cost` / `kb e2e-introns --quantify` with
   a `cost` block reporting `star_wall_s` / `star_peak_rss_gb`. **Still open (§9):** above 250 M reads is
   extrapolation from one post-knee point, so a deep human library is provisioned 128 GB until the sweep
   extends — an expensive default is not a trap, because the processing manifest can override it.

   (`mem_gb` is `ResourceHints.mem_gb` on the **processing manifest**, default 32 — not a workflow
   module property. A resource request is intent, so R11 puts it in the recipe.)

   Still open: a **SPLiT-seq** e2e — this run certifies 10x 3′ v3's `soloStrand Forward` only. Note a
   simulation cannot settle `splitseq`'s strand FLAG on its own: simulating requires assuming the
   strand, which is circular. That FLAG needs the Rosenberg-2018 oligo derivation or real GSE110823
   data; a simulation can then only prove compose stays faithful to whatever the KB declares.

### 4.2 Skill → verb map, hooks, state

All eight skills are thin clients: `orchestrate`→`run`/`compile`; `exam`→`probe` (+`io peek`);
`harvest`→`normalize`(det)+`extract`(LLM)+`verify`(det); `resolve`→`score`(det); `manifest`→`fill`/
`validate`/`hash`; `compose`→`compose`; `io`→`io *`; `kb-author`→`kb *` (the skill's LLM drafts
README/spec prose; the verbs that write/verify are deterministic). **Every verb is shell-scriptable
with no LLM except `harvest extract`** — so `run --no-llm` drives the whole compiler minus extraction.
(`resolve adjudicate`, the opt-in second LLM job, is modelled but unbuilt — §9.)

**Hooks (policy → mechanism):** `PreToolUse` blocks a Bash call that would stream a FASTQ with no
head/byte bound — **size-blind by design** (it never stats the file: a path that *can* stream a
multi-GB FASTQ is the bug, R3) — **and** any write of an absolute path / `/scratch/**` into a manifest
or config; `PostToolUse` auto-runs `manifest validate` after any manifest edit; `Stop` refuses to end
a turn while `resolve/*/questions.md` is non-empty. Because `run` leaves adjudication off, the Stop
hook and exit 4 are the only ways ambiguity clears — both route to a human — which keeps the batch to
one LLM touchpoint.

**`seqforge/` (no leading dot; resumable, content-addressed):** the top level holds only what a human
reaches for; state sorts into `cache/`, `records/`, and `logs/`. Under `cache/`: per-file
`Observation` by its **content-address** — derived from the provider md5 when known (issue #39; a
remote probe fingerprints straight from a URL), else a bounded local key (basename + head sample +
size + gzip ISIZE), never a whole-file scan (`cache/observations/`); dataset
`candidates`/`conflicts`/`questions` by `dataset_id = sha256(sorted(file_shas) ⊕ kb_version)` with
`probe_version`/`resolve_version` folded in (`cache/candidates/`); a stat-keyed resume pointer
(`realpath+size+mtime` → `dataset_id`) that lets an unchanged re-run rebuild the answer reading
**zero** FASTQ bytes (`cache/resume/`); `cache/taxonomy.json`. Under `records/`: fetched `records/<accession>.json`, and
the canonical documents by `doc_sha256` (+ `normalizer_version`) in `records/documents/`. Under
`logs/`: the harvest cost ledger `logs/usage.json` and `logs/assertions.json`. `manifest.yaml` written
**only** after a clean `validate` (and exactly one of `manifest.yaml`/`manifest.draft.yaml` ever
exists). Compiled output lives under `pipeline/<recipe>-<run_id[:12]>/` — `run_id = H(dataset ⊕
processing ⊕ kb ⊕ workflow)`, keyed by the **run** so one dataset compiled two ways does not overwrite
itself — each carrying `config.yaml`, `units.tsv`, the `Snakefile`, a **copy of the hand-written
module** it imports locally, and `processing.lock.yaml` (the dataset-bound recipe that produced it).

---

## 5. Pushback appendix (the arguments to settle)

Ranked; ✔ = folded into this design, ✱ = open for the maintainer. Full detail in the approved plan.

1. ✔ **`score()` needs the hypothesis** — a byte-blind signature can't do "cheap first"; it
   enters as a selector/prior, never evidence (§3.4).
2. ✔ **Distinct-ratio is depth-dependent** — supports-only, normalized; onlist confirms CB/UMI (§3.1).
3. ✔ **Score must be role-cardinality-normalized** — else `argmax` favors high-role-count techs (§3.3).
4. ✔ **`decidable_by` must include `onlist`** — the mechanism §2.4 actually uses (§1.4, §2).
5. ✔ **Onlist orientation is per (chemistry, read), not per list** — registry value is a hint only (§1.6).
6. ✔ **Onlist index must be width-generic** — SPLiT-seq's 8 bp blocks, not a hardcoded 16 (§3.1).
7. ✔ **The exact-span contract is infeasible as an LLM instruction** — LLM emits `quote`, code
   computes offsets; verify also checks entailment (§1.3).
8. ✔ **The dry-run gate can't catch a strand inversion** — split gate + `kb e2e` count-matrix run (§4.1).
9. ✱ **Pre-registering PRJNA1027859's organism vs "don't tune against it"** — safe reading:
   `expected.yaml` uses GEO-declared metadata + provider-independent prior only, committed before any
   run; never a value read from the data. Authored later, on the maintainer's go (§8).
- ✱ **Basis for policy defaults** — kept to the four bases (`inferred` + an evidence ref naming the
  policy rule); add a `policy_default` basis if you want it machine-distinguishable (§1.0).

---

## 6. Milestone-0 scope + coverage caveats

Vertical slice, all four stages at reduced coverage (breadth before depth), for three technologies
chosen for architectural coverage: **(1) bulk Illumina RNA-seq PE** (no-barcode branch, header
parsing, run/lane grouping); **(2) 10x 3′ GEX v3** (onlist matching, technical-read identification,
SRA-mangling); **(3) SPLiT-seq** (original Rosenberg-2018 combinatorial multi-block indexing — the
pilot-#3 generalization test; Parse Evercode deferred to its own future entry). Plus the three
day-one negatives: truncated gzip → `Blocker`; a KB-absent
technology (ONT) → `UNSUPPORTED_TECHNOLOGY`, not a guess; a contradiction (metadata v2, reads v3) →
surfaced `Conflict`.

**Coverage caveat (recorded so green CI isn't mistaken for full coverage):** SPLiT-seq exercises
combinatorial barcodes + fixed linkers + small onlists, but **not** variable-length/anchored elements.
The `anchor`/`motif` element path is in the schema and the scorer, but its dedicated test fixture (an
inDrop-class chemistry with a floating W1 linker) is **deferred** — add an inDrop entry to exercise it
before claiming the element model fully generalizes.

---

## 7. Genomics-correctness FLAGS (unverified — do not ship without checking)

1. **EFO/OBI CURIEs — all four pilot techs verified against the EBI OLS** (not memory): `EFO:0009922`
   (10x 3′ v3), `EFO:0009899` (10x 3′ v2), `EFO:0009919` (SPLiT-seq), `EFO:0008896` ("RNA-Seq", bulk).
   Any *future* tech must be looked up against live EFO/OBI the same way before use. (Noted for later:
   v3.1 has its own term `EFO:0022980`, and Parse Evercode's are `EFO:0022600/1/2` — distinct assays.)
2. **GEM-X 3′ v4 whitelist filename** — referenced conceptually; register it by URL+sha256, do not
   invent a filename.
3. **SPLiT-seq — DONE (read structure + linkers) / still-open (whitelists, strand).** The Read-2
   layout and both 30 bp linker sequences are now pinned verbatim from scg_lib_structs (Science-2018
   variant; see §2.3). Still to pin: the three round whitelists (register the real barcode files by
   URL+sha256), `soloStrand`, and the EFO CURIE. `soloCBposition`/`soloUMIposition` are generated from
   the element model at compose, never hand-entered (FLAG-3). Parse Evercode is a separate future entry.
4. **10x 5′ `soloStrand`** — 5′ vs 3′ have identical CB/UMI geometry (read-undecidable); 5′ is a known
   confusion source. KB carries `soloStrand` per chemistry; CI round-trip confirms it. Do not assert
   from memory.
5. **seqspec `CB_UMI_Complex` support** — the dual-derivation check (`seqspec index` vs `backend.params`)
   may not cover adapter-anchored/combinatorial barcodes; scope it to fixed-offset chemistries until
   confirmed.

**Confident (encode directly, verified by `kb roundtrip`):** 10x 3′ v2 = 16+10 = 26 bp,
`737K-august-2016`; 3′ v3/v3.1 = 16+12 = 28 bp, `3M-february-2018` (~6.79M), identical STARsolo params
(§2.4 benign); Multiome GEX `737K-arc-v1`; 28 bp does not identify chemistry (v3/v3.1/GEM-X v4/Multiome
all 28 bp, separated only by onlist); STARsolo `CB_UMI_Simple` param names; `--readFilesIn` is cDNA
read first, then barcode read.

---

## 8. Demo dataset — PRJNA1027859

The pilot's worked example: the dataset the tutorial is written from, and what "it works end to end"
means. Its on-disk root lives in local, out-of-git config, never in this repo — real FASTQs are far
too large for git, and a lab path is not a project fact. `ce11` (C. elegans, taxid 6239, WBcel235) is
confirmed available in liulab-genome.

**It was a held-out acceptance case until 2026-07-15**, when the maintainer retired the designation:
reserving it was a misunderstanding of what it is for. The `PreToolUse` guard and the
`SEQFORGE_CASE_*` root registry that enforced it are deleted, not suspended. The project has no
held-out case.

Its pre-registration (`evals/cases/PRJNA1027859/expected.yaml`) stands and stays honoured — written
from declared metadata and provider-independent prior knowledge only, committed before any run, never
from a value read out of the data. That discipline never depended on the data being reserved: it is
what makes the file a prediction rather than a transcript, and only a prediction can be wrong.

---

## 9. What is designed here but not yet built

The honest scope delta. **When you build one, delete its line here and fix the tense above** — a stale
§9 is worse than none. What closed and when lives in `CHANGELOG.md`.

**Not built:**

- **Journal flywheel, entirely** — no `journal.jsonl` writer, no `distill`, no `LESSONS.md`;
  `questions.md` is read by the `Stop` hook and written by nothing. The fictional `seqforge-journal`
  skill was removed (a skill is a client of verbs that exist).
- **SPLiT-seq whitelists** — the spec names `splitseq-round1/2/3` and we ship none, so its three
  weight-3 onlist tests ABSTAIN and rung 3 can never decide; pinned `UNSHIPPED_ONLIST_DEBT`. **Do not
  guess barcodes** — a wrong whitelist emits a thin-looking matrix instead of failing.
- **`fetch` workflow module** (+ the Snakemake-8 storage-plugin evaluation it needs first).
- **Escalation rungs 5–6** (k-mer, mini-alignment); a tie past rung 3 asks a human. Rung 4 is likely
  redundant with rungs 2–3.
- **`resolve adjudicate`** (LLM job (b) — modelled, no verb) and **`resolve apply`**.
- **seqspec export** + **scg_lib_structs ingestion** (the decomposition is adopted; the emitter isn't),
  **GENCODE/RefSeq accessions**, **`syrupy`/`hypothesis` tests**, **dual-index parsing**, and
  **inDrop's W1 linker** (the variable-length/anchored-motif case SPLiT-seq doesn't cover — §6).

**Known correctness gaps:**

- **Onlist revcomp is tested only when a spec declares it** — a `forward`-pinned spec opts out of the
  ATAC trap silently (§3).
- **The hypothesis is a scoring prior, not a gate** — every spec is evaluated unconditionally;
  invisible at 5 specs, not at 500.
- **Harvest does not retry a transient provider error** — DeepSeek's empty-content-in-JSON return
  correctly refuses the batch, but `run` then exits 1; a bounded retry in `extract` is the fix.
- **`resources` carries no `Evidenced` basis** — deliberate (a hint, not a decision).

**Open measurement:** peak RSS above 250 M reads × hg38 is unmeasured (100 M → 34.7 GB, 250 M → 44.1
GB, curve bends between); `kb e2e-cost --sweep` settles it, meanwhile deep libraries get 128 GB.

**Maintenance debt** (each is its own focused pass — do not fold into a feature branch):

- **Consolidate the rule set and stop citing R-numbers in code.** Good code is self-explanatory; an
  R-number is a mutable label, not a shackle, so rules may be renumbered as long as every citation
  moves with them in one commit. Do not add new `(RN)` citations to code meanwhile.
- **De-duplicate this doc against the code.** §1/§2 embed the Pydantic models and KB specs that already
  live in `src/models/` and `kb/specs/` (`schema export` is the source of truth) — ~65 % of this file
  is schema listings. Replace them with pointers + the design *decisions*, lifting the inline rationale
  into prose first so nothing is lost. That, not prose-trimming, is what actually shortens this doc.

---

## 10. Next stage: stress tests, then new assays

Everything shipped is single-cell 3′ 10x — one corner of the space. Next-stage work, ordered so each
step stresses an abstraction before the next leans on it.

**Stress first (cases, no new code):** one project / several assays (a divergent run must `Blocker`,
never average); one assay / several layouts — `_1/_2`, `_I1`, lane-split all → the *same* manifest,
technical-read-dropped → `Blocker(MISSING_TECHNICAL_READ)` (the `-swapped`/`-no-technical` fixtures §9
owes); metadata disagreeing with the bytes (a `Conflict`) vs with itself across runs (a `Warning`).

**New assays, in dependency order** (each a `spec.yaml` + round-trip fixture, plus the work it forces):

1. **Bulk RNA-seq PE** (`bulk-rnaseq-pe` spec exists, path doesn't) — the no-barcode branch and the
   first non-STARsolo **map module**; proves `param_block_key` selects by chemistry, not "not-starsolo".
2. **10x Multiome (GEX+ATAC)** — two libraries, one sample (the `records.py` join generalizes); the
   canonical revcomp-onlist case, making §9's revcomp-gate gap load-bearing.
3. **ATAC (chromap)** — a new aligner env (consumed, not defined — R10) and a non-matrix deliverable
   (fragments): the first test that the aligner field is open, not STAR-shaped.
4. **ChIP-seq** — coverage + peaks; decide from the spec whether it belongs here or is a sibling.
5. **inDrop v3** — the variable-length barcode + anchored W1 linker SPLiT-seq skips; needs the
   `splitseq-round*` debt (§9) paid first.

The pressure lands on a second/third `workflows/map/` module, a deliverable past `.h5ad`, and the
`fetch` module — but **no new LLM job**: still bytes → `score`, prose → `Assertion`, code decides. An
assay that seems to need the model to *act* is the signal to re-read §0, not to relax the two-job line.
