# PROJECT_BRIEF.md

> The design rationale for `seqforge` — **why** the system is shaped this way. The rules you are
> actually checked against live in [`CLAUDE.md`](CLAUDE.md); the schemas, scoring function and CLI
> surface live in [`docs/design.md`](docs/design.md).
>
> This brief is a **living document, not a historical record**: where the built system diverges from
> what is written here, the text is wrong and gets corrected. §14 is the running tally of what is
> designed here but not yet built. Read it before trusting any present-tense claim below.

---

## 1. What we are building

`seqforge` turns **(an arbitrary set of FASTQ files) + (unstructured human/database
metadata)** into **(a validated, machine-readable sequencing library manifest)** and then
into **(a runnable Snakemake pipeline configuration)**.

The end goal is large-scale, uniform reprocessing of public genomic data for downstream
genomic-AI training. That means the system runs headless across thousands of datasets, not
interactively across ten. Design for that.

### Why this is one package and not two

Two ArcInstitute tools bracket this problem. **SRAgent** — "agentic workflows for obtaining data
from the Sequence Read Archive" — is an LLM agent that turns an accession plus its prose into
structured metadata: which organism, which tissue, is it single-cell, which 10x version, and
(exactly) *"single nucleus or single cell RNA sequencing?"*. **scRecounter** — "a Nextflow pipeline
to re-process single-cell RNA-seq data from the Sequence Read Archive" — does the other half: fetch,
STARsolo, count matrix. Each is good at its own job.

Nothing joins them, and the consequence is concrete and expensive. scRecounter does not trust SRA
metadata at all, so it *searches* for STAR parameters — barcode whitelist version, CB length, UMI
length, strand, reference index — by "mapping the reads using various parameter combinations" and
picking the winner by fraction of valid barcodes. It re-derives by brute force, on real alignments,
facts that were sitting in prose that SRAgent already read. And the nuclei-vs-cells fact SRAgent
extracts by name is the one that should set `soloFeatures` — it has nowhere to go. The gap between
the two tools is not a missing feature. It is a missing **interface**.

The manifest is that interface, and once it exists the two tools are one compiler. `harvest` is
SRAgent's job, demoted to a frontend that *proposes* rather than concludes. `compile` is
scRecounter's job, driven by a decided manifest instead of a search. Beyond the glue, the interface
buys three things neither tool has: **span verification**, so extracted metadata carries a tripwire
rather than a confidence score (§3.4); **cheap eager verification** — one targeted onlist check,
~100 ms, where scRecounter spends a subsample alignment (§5); and **refusal**, so a dataset that
cannot be decided yields a `Blocker` instead of the best-scoring guess. And because processing is a
separate artifact (§7), scRecounter's uniform reprocessing becomes *one recipe among many* rather
than the only thing the pipeline can do — which is what lets us rebuild the corpus under a different
aligner without re-deriving what the data is.

State the claim honestly: it is architectural, not a track record. SRAgent has working SRA search and
discovery; our `io peek` / `io resolve` are thin. scRecounter has run at scale on real public data;
seqforge has run end-to-end on simulated yeast and worm reads and has not yet processed a single real
dataset. What we have is the interface and the verifiers. Whether that beats two mature tools is
exactly what §12 is for.

## 2. The governing metaphor: this is a compiler, not a chatbot

| Stage | Executor | Output |
|---|---|---|
| **probe** | deterministic code, no LLM | `Observation` — facts derived from bytes |
| **harvest** | LLM | `Assertion` — claims from prose, each with a verifiable source span |
| **resolve** | code scores; LLM adjudicates only what code flags ambiguous *(adjudication unbuilt — §14)* | ranked candidates, `Conflict`s, `Question`s |
| **compose** | deterministic code | pipeline config + workflow module selection |

The LLM has exactly two jobs: (a) parse unstructured prose into structured assertions,
(b) arbitrate ambiguity that the deterministic layer has *already identified as ambiguous*.
Everything else is a verifier. Do not let this line blur.

## 3. Non-negotiable design principles

1. **Agents propose; code decides.** No field enters the manifest without passing a validator.
2. **Emit data, never code.** LLM output must always be something a JSON Schema can validate.
   We generate pipeline *configs* for hand-written, versioned, CI-tested Snakemake modules.
   We never generate Snakefile source text with a model.
3. **Three truths, never merged silently.** Every manifest field carries
   `{value, basis: observed|asserted|inferred|user_confirmed, evidence: [ids], confidence}`.
   `observed` comes from bytes. `asserted` comes from humans/databases. When they disagree,
   that is a first-class `Conflict` object, surfaced — never auto-resolved.
4. **Span-verified extraction.** Every `Assertion` the LLM produces must carry the exact source
   span it derived from. The pipeline greps that span back into the source document. If it
   isn't there, the extraction is rejected. This is our hallucination tripwire.
5. **Refusal is an exit code, not a vibe.** `seqforge manifest validate` returns structured
   `Blocker` objects. Code decides whether we may compile. The LLM only decides what question
   to ask the human.
6. **Every KB entry is executable and self-testing.** See §6.
7. **Cheap first, expensive only on ambiguity.** See the escalation ladder in §5.
8. **Disk is the state; context is a cache.** Every step writes an artifact under `.seqforge/`.
   Any run must be resumable after a kill. The agent never holds state only in context.
9. **The CLI is the API; the skill is a thin client.** Every skill action must correspond to a
   deterministic `seqforge <verb> --json` command that works with no LLM in the loop.
10. **Bounded work, not bounded time.** The probe's contract is a *read budget* (`--max-reads`,
    default 200k) and a byte cap — never wall-clock, which varies with filesystem, gzip level,
    and whether you are on a login node. Time is an emergent consequence. A unit test asserts the
    budget stops the stream early (`tests/test_probe.py`, on a 5 000-read fixture); the large-file
    assertion this principle wants is **not yet written**. Probes are embarrassingly parallel across files;
    ~10 s/file is an acceptable envelope. If a code path *can* touch a whole multi-GB FASTQ,
    that is a bug.
11. **The manifest is machine-independent. No absolute filesystem paths, ever.**
    Genome -> a `liulab-genome` assembly identifier + annotation version.
    Software -> a `liulab-runtime` environment name.
    Data -> a URI.
    Everything resolves at run time, on whatever cluster. This is what lets us replay a manifest
    on a different machine in three years — which matters, because we will rebuild this corpus.
12. **The dataset is immutable; the recipe is not. Two artifacts, never one.**
    A finished assay is a fact — those molecules, that flowcell, those bytes — and facts do not get
    a v2. What to *do* with the assay is a choice, and there are several defensible ones. So the
    manifest splits: `manifest.yaml` (library + experiment) is what the data **is**,
    content-addressed and immutable; `processing.yaml` is a **recipe** — what to do with it — and a
    dataset carries as many as we care to run. `compile(dataset, recipe)` is a pure function of
    both. Aligning one dataset three ways is three recipes against one unchanged manifest, never
    three forks of the truth. If re-running a dataset with a different aligner edits
    `manifest.yaml`, that is a bug.
13. **The instructable surface is closed, and the line is parse vs. count.**
    `backend.params` says how to **parse** reads — soloType, CB/UMI offsets, whitelist, strand.
    Those are decided by bytes and are never instructable. The recipe says what to **count**, and
    against which genome, with which aligner, in which environment, at what resources. A user may
    tell us to count introns. A user may not tell us the UMI is 10 bp — the reads already answered
    that, and a human who disagrees is a `Conflict`, not an instruction. The instructable keys are
    enumerated and the recipe model forbids extras: an open instruction surface is a prompt-injection
    path from a GEO description into `--soloStrand`, and a wrong strand exits 0. Both halves are
    enforced — `params_gate` proves no unknown key reaches a command line, and `extra="forbid"` on
    the processing models makes an unknown key a validation error rather than a silent drop (it was
    a silent drop until 2026-07-15).
14. **Do not ask a question whose answer you can afford to produce twice.**
    §12 gives the benign rule: never escalate an ambiguity that cannot change the output. This is
    its sibling — never escalate one whose every answer you can afford to emit. `soloFeatures`
    therefore defaults to all five (`Gene GeneFull GeneFull_ExonOverIntron GeneFull_Ex50pAS
    Velocyto`): one alignment, five counting rules, one pass, with download and alignment dominating
    the cost by orders of magnitude. That dissolves the cells-vs-nuclei question rather than
    answering it. We measured the alternative: `--soloFeatures Gene` silently discards **40.7 %** of
    a nuclear library. Reserve escalation for choices that are genuinely exclusive — a genome, an
    aligner — and let the recipe be where a human answers those once, in writing.

## 4. Repository layout

Single repo, single `pyproject.toml`, clear internal module boundaries. Do not split into
separate distributions yet.

```
src/seqforge/
  models/          pydantic v2 schemas; `schema export` is the single source of truth
                   (used for validation and LLM structured output; docs reuse is unbuilt)
  probe/           deterministic FASTQ fingerprinting  (no LLM, no network)
  kb/              knowledge base: one directory per technology under kb/specs/ (see §6)
  resolve/         candidate scoring, role assignment, confusability, escalation
  manifest/        fill/validate/hash both artifacts; policy.py owns the precedence ladder (§7)
  compose/         (dataset, processing) -> snakemake config + module selection
  io/              remote peeking, ENA/SRA/GEO/SDL resolution, pooch-cached onlists
  workflows/       hand-written, versioned Snakemake modules  (NOT generated). map/ only
  hooks/           PreToolUse/PostToolUse/Stop guards behind `seqforge hook …` (see §10)
  cli.py           a single typer module, not a package: root app + 9 sub-typers. JSON by default
  e2e.py           ground-truth end-to-end runs behind `kb e2e` / `kb e2e-introns` (see §8)
  evals/           the harness (see §9)
skills/            SKILL.md agent skills (open Agent Skills standard). Installer unbuilt
evals/cases/       ground-truth corpus (see §9)
tests/
```

## 5. The core algorithm

```
probe(files)                          -> Observation
harvest(prose, instructions)          -> Assertions       # LLM, span-verified
resolve(Observations, KB, hypothesis?) -> ResolveResult{candidates, Conflicts, Questions, Blockers}
plan(Assertions, flags, policy)       -> ProcessingSection
compose(manifest, processing)         -> config + units.tsv + module selection
```

Note there is no `compile` verb and no `Decision` object: scoring and role assignment happen inside
`resolve`, and composition takes **two artifacts** (§7), not one decision.

### `probe` — computed from a bounded head-limited stream, no LLM, no network

**Tier A — free structural signals (no whitelist, no network, computed while streaming anyway).**
These do most of the work, and they alone are enough to solve role assignment.

- **Per-cycle base composition.** This recovers the read's *segmentation* with zero external input.
  Any cycle where one base exceeds ~90% is *constant sequence* — a linker / TSO / adapter, located
  and read off directly. Uniform-ACGT cycles are random regions. A run of T-dominant cycles is
  polyT. **Adjacent random spans do not separate themselves**: a 10x v3 R1 is 16 bp of barcode
  followed by 12 bp of UMI, both uniform-ACGT, so the profile yields one 28 bp random span, not
  "16 + 12". The boundary inside it comes from the KB's declared geometry, which the profile then
  *corroborates* — segmentation is KB-free, but the 16/12 split is not.
- **Distinct-value ratio** over a candidate window (200k reads): cell barcodes recur because
  cells are resampled, so distinct/total lands around 0.05–0.3. UMIs and cDNA sit near 1.0.
  This separates CB from UMI **with no whitelist at all** — but not with no KB: the ratio is
  computed over the KB-declared element offsets, since (per the previous bullet) the bytes alone
  do not know where the barcode stops.
- **Read-name grammar.** Parse Illumina headers into instrument / flowcell / lane / tile, and
  pull the index sequence out of the comment field (giving dual-vs-single index and index length
  for free). If SRA has normalized the header away, *detect and record that* — the absence of a
  real header is itself a signal, and it changes which evidence we are allowed to trust.
- **Read-length summary**, stored as `{mode, n_distinct}` and expanded to percentiles only when
  `n_distinct > 1`. Its value is **not** technology detection (short-read FASTQs are usually
  fixed-length). Its value is *data integrity*: variable length in a fixed-cycle Illumina run
  means someone already ran cutadapt/trimmomatic before uploading. That is common in GEO and it
  **must be** a `Blocker` — if barcode offsets have shifted, we will silently produce garbage.
  `BlockerCode.PRETRIMMED_VARIABLE_LENGTH` is declared for exactly this and **nothing emits it**
  (§14): the integrity blockers computed today are truncated and corrupt gzip only.
- N-rate, quality encoding, estimated total reads from file size / bytes-per-read.

**Tier B — targeted onlist verification (the hypothesis test).**

The onlist test is a *verification* of a stated hypothesis, not an open-ended search. Metadata
proposes "10x 3' v3"; we load **one** list and check it. Full-panel search is the fallback, run
only when metadata is absent, verification fails, or a conflict surfaces.

**This is the design, and the code currently inverts it.** `resolve_dataset` builds an evaluation for
*every* spec in the KB unconditionally, then lets the hypothesis act as a scoring prior rather than a
gate. At five specs the distinction is invisible — the whole panel is under a second, which is why
this has cost nothing so far. It stops being invisible as the KB grows, and the fix is a gate, not a
rewrite (§14).

It is cheap, and the numbers matter because the instinct is that it is not:
a 16bp barcode is exactly 32 bits, so it is a `uint32`; the 6.8M-entry v3 whitelist is a 27 MB
sorted `uint32` array; `np.searchsorted` over 200k reads is milliseconds. **The whole test is
~100 ms including an mmap'd load.** Testing against the *entire* panel is still under a second,
because the other lists are far smaller (737K lists are 3 MB; SPLiT-seq's combinatorial lists are
96 entries). Exact matching suffices — at Q30 over 90% of real barcodes match exactly, against a
random-hit floor of 6.8M/4^16 ~= 0.16%. That is roughly 500:1 signal-to-noise. **Always test the
reverse complement** — reverse-complemented ATAC barcodes are a perennial trap.

Two gaps here (§14). Precompiling to `.npy` was specified and never built: packing is recomputed per
process and cached in memory only, so every fresh CLI invocation re-packs. And "always" is not
enforced — revcomp is tested only when a spec's onlist declares `orientation: either|revcomp`, and a
spec that pins `forward` silently opts out of the trap this sentence exists to catch.

Onlists are a **registry, not vendored data**: `{name -> URL, sha256, barcode length, orientation}`,
fetched with pooch and hash-verified. 10x whitelists ship under Cell Ranger's license, so we do not
redistribute them. Non-10x is easier than expected — scg_lib_structs publishes barcode CSVs under
CC-BY, and seqspec's published assay specs carry `onlist` entries with URLs and checksums, so most
of the registry can be *harvested from seqspec* rather than hand-curated.

**Why we verify eagerly rather than only on downstream error.** Because the dangerous failures do
not error:

- **wrong strand** -> roughly half the reads go unassigned. STARsolo exits 0 and emits a matrix
  that merely looks like a thin dataset. Strand is exactly what GEO metadata never states.
- **wrong UMI length** -> dedup over- or under-collapses; counts are systematically biased; the
  matrix looks fine.
- **10x GEX v3 vs Multiome GEX** -> both are 16bp CB + 12bp UMI, both R1 = 28bp.
  *Geometry cannot separate them.* Only the onlist can.

The loud failures we would catch anyway. The quiet ones poison a training corpus, and those are
precisely what a 100 ms check catches. Note that scRecounter does not trust SRA metadata *at all* —
it grid-searches STAR parameters (whitelist version, CB length, UMI length, strand, reference) and
picks by fraction of valid barcodes. That is eager verification by the most expensive method
available. We do the same verification ~1000x cheaper. We do not skip it.

### Role assignment is a joint optimization, not a filename lookup

Filenames lie. `fasterq-dump` `_1/_2/_3` says nothing about which read is R1 / R2 / I1. Given a
candidate technology's expected read layout, brute-force the bipartite matching of files to roles
that maximizes evidence. The technology's score *is* the max over role assignments. Filenames are
a weak prior only, never decisive.

### Escalation ladder — metadata proposes, bytes verify

The user's claim is the *hypothesis*, never the *conclusion*. Trust it enough to skip the search;
never enough to skip the check.

```
0  metadata / prose (LLM, span-verified)   proposes the hypothesis
1  filename / directory structure          free       weak prior, never decisive
2  Tier A structural probe + KB geometry   free       layout, roles, integrity — no onlist needed
3  targeted onlist check                   ~100 ms    verifies the hypothesis
--- below runs only when a processing-DIVERGENT tie survives 3 and metadata cannot settle it ---
4  full-panel onlist + motif search        ~1 s/file  open-ended detection        [NOT BUILT]
5  k-mer sketch vs organism panel          ~seconds                               [NOT BUILT]
6  mini-alignment to tiny reference        ~1 CPU-min (strand, 3'/5' bias)        [NOT BUILT]
7  ask the human                           expensive — and that is the point
```

Rungs 0-3 are the default path and cost well under a second. Record which rung resolved each field:
that record is both provenance and our primary eval signal.

Two corrections to the ladder as originally drawn. **The trigger is narrower than "0 is absent or 3
disagrees"**: the only thing that escalates is a processing-divergent tie — two or more candidates
within θ of the top that would compile to *different* params. A `Conflict` does **not** escalate; it
is detected in parallel and surfaced (§3.3). An ambiguity that cannot change the output is never
escalated (§12), and neither is one whose every answer we can afford to emit (§3.14). **And rungs 4-6
do not exist**: a tie surviving rung 3 goes straight to rung 7. Rung 4 in particular may never be
needed — both of its halves already run unconditionally at rungs 2-3, so it is not a fallback so much
as a description of what the default path does. Rungs 5 and 6 are real, unbuilt fallbacks (§14).

## 6. The knowledge base: executable and self-testing

Each technology is one directory containing:

- `README.md` — prose for the LLM: how the assay works, aliases, history, gotchas, how to tell it
  apart from its siblings, common SRA failure modes.
- `spec.yaml` — machine-checkable: read layout, element coordinates, onlist references, a
  detection `signature` (requires / supports / excludes tests), a `backend` block mapping to a
  workflow module and its parameters, and a `confusable_with` list.
- Synthetic-data generation is **derived from `spec.yaml`** — not written by hand.

**The round-trip test is mandatory and is what makes the KB trustworthy:**

```
spec.yaml --generate--> synthetic FASTQ set --probe--> recovered spec
assert recovered == declared
```

Adding a technology therefore automatically adds its own test, and requires no real data. True as of
2026-07-15, and it was not before: the round-trip was parametrized over a *hardcoded* list of spec
ids, so `10x-3p-gex-v3.1` shipped with no round-trip at all. It collects from `kb.list_spec_ids()`
now, which is what makes the sentence a mechanism rather than an aspiration.

Also generate adversarial variants from the same spec and assert the system emits the *correct
Blocker or Conflict* rather than a wrong answer: SRA-mangled headers, dropped technical read,
reverse-complemented barcodes, truncated gzip. **One of the four is built** — truncated gzip, as a
mutation on the eval recipe with a negative test asserting the `Blocker`. The other three are §14.

**Confusability, checked pairwise in the test suite.** For every pair of KB entries, determine
whether the cheap probe (rungs 0–2) actually distinguishes them. If it does not, the entry must
declare `decidable_by: [reads | onlist | metadata | alignment | user]`. This makes "ask the human" a
computed property rather than a prompt hope, and it blocks any new technology that would silently
collide with an existing one.

Both halves are real as of 2026-07-15, and both are computed over every spec pair in
`tests/test_kb.py`, collected from the KB rather than listed by hand:

- the §12 biconditional — `backend_identical(A,B) ⟺ declared processing_equivalent`;
- **rung-0–2 separability** — generate each spec's own synthetic reads, then ask every other spec
  whether it would claim them using the cheap probes alone. The onlist is withheld by handing the
  evaluator an empty registry, so rung-3 evidence cannot rescue the answer. `A accepts B ∧ B ∉
  A.confusable_with` is an error.

Until then `decidable_by` was hand-maintained: a claim, not a computed property. The guard found a
real one on its first run — `bulk-rnaseq-pe`, the generic paired-end fallback, requires little and
forbids less, and accepts SPLiT-seq's `cdna`+`bc` pair on geometry alone while declaring nothing.
The system already *knew*, in the sense that a test comment called bulk "the generic bulk fallback
that merely fails to be forbidden (rung 2)". A comment is not something the resolver can read.

Some distinctions are provably undecidable from reads alone, and the system must *know* this
rather than guess: 10x 3' and 5' have identical CB/UMI geometry; inDrop v2 and v3 share oligos
and differ only in sequencing configuration. Encode this honestly.

### Sources to ingest (with attribution)

Both of these are **plans, not code** — no ingestion or export exists today (§14). Every mention of
either in the repo is a hand-authored citation in a comment or README, which is attribution for
knowledge a human transcribed, not a provenance trail from an automated harvest. Stated plainly
because "sources to ingest" reads like a description of a pipeline that runs.

- **scg_lib_structs** (Teichlab) — CC-BY-4.0, so we may legally derive from it with attribution.
  Ingest `docs/source/` markdown, which is more tractable than the HTML pages.
- **seqspec** (pachterlab) — adopt its Assay/Region/Read decomposition as our *interchange*
  format, not our internal model. We emit valid seqspec as an export target, which gives us
  `seqspec index` (tool strings for STARsolo / kb-python / simpleaf) for free. Provenance,
  confidence, and processing intent live in our richer layer above it.
  **The load-bearing half of this is done**: the decomposition is adopted, and every element carries
  a required `seqspec_region_type` from a closed seqspec vocabulary (every read a `seqspec_read_id`),
  so our specs are already shaped to map onto seqspec. What is missing is only the emitter.

## 7. Manifest schema — two artifacts, because a dataset and a recipe have different lifetimes

**`manifest.yaml` — the dataset. Two top-level sections, two distinct authorities. Immutable.**

- `library` — physical truth about molecules and sequencer output: assay, chemistry, read layout,
  onlists, file inventory with checksums. Authority: **evidence**.
- `experiment` — biological/metadata truth: organism, tissue, condition, sample grouping,
  accessions, sample-to-file mapping. Authority: **metadata and humans**.

Both are claims about *what the data is*. A finished assay does not change, so neither does this
file: it is content-addressed, and nothing downstream may write to it. When a new fact arrives, the
manifest is rebuilt from evidence and gets a new hash. It is never patched.

**`processing.yaml` — the recipe. What to do with the dataset. Authority: the user, then policy.**

Reference build + annotation version, aligner, counting features, whether variant calling is
required, runtime environment, resource hints.

Sparseness lives on the **input** side, and that is the design, not laxity. What a user hands us is a
sparse override set — `gcc` with no flags still compiles — so the empty override set is legal and
means "all policy defaults". What `plan` *produces* is the opposite: a fully-resolved intent in which
every decision field carries a value and an `Evidenced` basis naming who decided it. Do not conflate
the two; `processing.yaml` is the resolved artifact, so its fields are populated, not optional.

One recipe can drive 10⁴ datasets, which is what uniform reprocessing *is*. So a recipe **need not**
name its dataset, and that optionality is the whole design: **unpinned it is a template**, portable
across the corpus; **pinned** to a `dataset_hash` it is bound, and `compose` refuses a mismatched pin
with a `Blocker` rather than silently re-pinning. Compose always writes the bound form it actually
used to `processing.lock.yaml`, so the pairing is recorded on disk whether or not it was recorded in
the input.

`plan(manifest, recipe) -> ProcessingSection` folds recipe, harvested instructions, and policy into a
fully-resolved intent in which every *decision* field is `Evidenced` and its `basis` records which
rung of the precedence ladder supplied it. (`resources` — threads, memory, disk, GPUs — is the
deliberate exception: an advisory scheduler hint, not a decision about the data, so it carries no
basis.)

| precedence | source | basis |
|---|---|---|
| 1 | an explicit CLI flag | `user_confirmed` |
| 2 | `alignment_instruction.md` — a document authored **for** seqforge | `user_confirmed` |
| 3 | a policy default seqforge chose | `inferred` |

1 and 2 share a basis and differ only in precedence: both are the user talking to seqforge, one just
talks later. The *channel* lives in `evidence`. An instruction is not a new kind of input — it is
prose about `processing.*` instead of `experiment.*`, span-verified like any other `Assertion`, which
is why it needs no new LLM job (§2) and inherits the hallucination tripwire for free. This is the
first real use of `user_confirmed`, which had sat in the `Basis` literal, unwritten, since the
beginning.

Note the ladder has no tier for a *downloaded* paper: only a document you hand us under
`--instruction` may set `processing.*`. A GEO description is an untrusted input, and prose reaching
`--soloStrand` would be prompt injection from a database field into an aligner. Under §3.14 that
costs nothing — a paper saying "we used GeneFull" describes a subset of what we already compute.

`compile` must be a pure function of `(manifest, ProcessingSection)` and nothing else. Purity across
*both* inputs is what makes pipeline generation reproducible and diffable, and what makes "same
dataset + different recipe = different pipeline, same manifest hash" a fact rather than a hope. Hash
both, and embed both hashes — plus the KB version and the workflow version — in every run's
provenance record: `run_id = H(manifest_hash, recipe_hash, kb_version, workflow_version)`.

Use controlled vocabularies from day one. Two of the three are in place: **EFO/OBI** assay CURIEs are
pattern-enforced on `library.assay` (a KB spec without one is refused at fill), and **NCBI taxids**
are required on `experiment.organism`. The third — **GENCODE/RefSeq accessions** — does not exist
anywhere: annotation is a registered `liulab-genome` GTF *name* (`WS298`), which is a local registry
key, not a stable public accession. That is a real gap for a corpus meant to stay filterable in three
years, and it is §14, not a decision to drop the vocabulary.

The end product is a training corpus; lineage and stable IDs are what make it filterable and
trustworthy later.

## 8. Pipeline composition

- `workflows/` contains **hand-written and versioned** Snakemake modules (`WORKFLOW_VERSION`, CalVer,
  folded into provenance). Compose them with Snakemake's `module` / `use rule ... from ...`
  mechanism. They are not yet *tested* in any meaningful sense — see the dry-run note below.
- The composer emits `config.yaml` + `units.tsv` + a module selection. It never emits rule source.
- `compose` is a **pure function of the (dataset manifest, resolved recipe) pair**. It requires no
  data on disk, local or remote. Two inputs, still no I/O: the recipe is data, not a side channel.

### The instructable surface is closed — parse is not negotiable, count is

`backend.params` says how to **parse** reads: `soloType`, CB/UMI offsets and lengths, whitelist,
strand. Every one is decided by bytes, and the recipe may not touch any of them. The recipe says what
to **count** (`soloFeatures`), and against which genome, with which aligner, in which environment, at
what resources. The keys are enumerated and the KB model forbids extras; an unknown key is a
validation error, never a passthrough to a command line.

The two key sets being **disjoint** is what makes "a user instruction contradicts the observed bytes"
*inexpressible* rather than merely deprioritized — the user has no vocabulary in which to say it.
That is the strongest form of §3.1 available, and it is why moving a key across this line has to be
an explicit, gated act rather than an edit.

This resolves the `soloFeatures` misfiling that `kb e2e-introns` priced at **40.7 % silent signal
loss**. It sat in `backend.params` because that is where the aligner's flags live — but 10x 3′ v3.1
chemistry is byte-identical for cells and nuclei. What differs is the RNA population, a property of
*sample prep*, not of chemistry. It was never a parse decision, and it moves to the recipe.

Note the consequence for §6's confusability matrix: with `soloFeatures` gone, the CI comparison runs
over a strictly smaller set of params, so two entries differing only in what they count become
processing-equivalent. That is correct — they now genuinely are, and the recipe decides the rest.

### Count everything; do not ask which

`soloFeatures` defaults to all five — `Gene GeneFull GeneFull_ExonOverIntron GeneFull_Ex50pAS
Velocyto`. One alignment, five counting rules, one pass. So we do not ask whether the library is
cells or nuclei: we emit every answer and let the consumer choose. Not free — `Velocyto` in
particular costs memory — but small against a download and an alignment we are paying for regardless.

**The instrument is `kb e2e-cost`** (not `kb e2e`, which measures nothing, and no longer
`kb e2e-introns --quantify`, which is a correctness gate that happens to time itself and is capped at
~16.8 M reads by its own unique-UMI trick). It sweeps read depth on hg38 and fits
`peak_rss = intercept + slope × reads`, because a single number would be almost entirely index.

**Measured 2026-07-15 and the answer is that depth does not matter.** 10 M → 34.570 GB, 40 M →
34.600 GB, 100 M → 34.659 GB: a **10× increase in depth costs 89 MB** (~0.95 bytes/read). The ce11
result generalized exactly as predicted — that 2.8 GB was the index, and on hg38 the ~30 GB index is
likewise the whole bill. All five do **not** breach a 32 GB hint by any read-dependent term; they sit
just above it because the *index* does, at any depth, with one counting rule or five.

What sizes the run is fixed by the chemistry and the annotation, not the library: the 6 794 880-entry
whitelist, the 78 733-gene feature axis (from the **index**, so it is unmoved by which genes the reads
came from), and the index itself. The count matrices — the thing one might expect to grow — are ~100 MB
and grow *sub*-linearly, 4× the reads giving 2× the non-zeros.

So `--soloFeatures` is the rare knob that is free in memory and merely expensive in disk. Velocyto is
unconditional **because it was measured**, not by decision. Provision ~48-64 GB.

Two things were measured rather than reasoned about, both because reasoning about them is exactly how
this document used to get things wrong:

- The sweep ran `--outSAMtype None`; the shipped module runs `BAM Unsorted`. That gap is **+745 MB**
  and +19% wall-clock (34.600 → 35.345 GB at 40 M, one variable changed). A production run is
  ~35.3 GB. Measured at one depth only — constant is the *expectation* (the BAM is streamed; buffers
  are per-thread, not per-read), and expectation is not measurement.
- **Reproducibility**: the 40 M point re-measured on a different node, through a different code path
  (32 sharded FASTQs vs one), on *different reads* → **34.600 GB**, identical to three decimals. Which
  also retires an objection raised in this very session: that node variance might swamp the signal and
  therefore the sweep had to stay on one node. Node variance is under 1 MB. The job array is fine.

### Validating the composer without any data

`snakemake -n` raises `MissingInputException` on absent inputs, so the composer creates a scratch
directory, `touch`es zero-byte files at every path in the manifest's file inventory, and dry-runs
there. This validates config, wildcard resolution, rule wiring, and every generated parameter
string — with no FASTQ present anywhere. Run `snakemake --lint` alongside.
**The composer is not done until both pass.** Wire it into the composer's unit tests.

**This is the most dangerous gap in the repo, because it looks closed and is not.** The gate is
written and wired into the unit tests — and it has never executed. `snakemake` is not a declared
dependency in any environment, so the test skips, a skip is green, and the assertion
(`gate["wiring"] in {"pass","skip"}`) forbids only the value that cannot occur. Everything this
paragraph exists to catch — a misspelled flag, a broken wildcard, a rule wiring mistake — is
therefore uncaught.

**But do not over-credit the gate: `snakemake -n` would not catch our worst known wiring bug.**
`starsolo.smk` hardcodes `--soloCBstart/CBlen/UMIstart/UMIlen`, which `CB_UMI_Complex` chemistries
(SPLiT-seq) do not have. A dry run never formats the `shell:` block, so the failing lookup is never
evaluated and the gate reports **pass** on a module guaranteed to `KeyError` on a compute node. The
gate needs `-p` to force formatting, and `--lint` must come out of it entirely — lint fires on every
rule here for a missing `log:`/`conda:` directive (see the environment note above), which would make
the gate a constant red that misdiagnoses a correct config.

**The instrument that did catch it cost nothing** (2026-07-15). `WorkflowModule.required_config`
declares the config keys a module reads, and it omitted those four — while the test enforcing it
checked against that same wrong list, over *one hardcoded chemistry per module*, which made SPLiT-seq
structurally unrepresentable. Both halves of the guard were wrong in the same direction. The
requirement is now derived from the module source by scanning it, and the coverage test iterates the
KB, so the bug became a 0 ms failure naming the exact missing key. A dry-run gate is still worth
having for what it genuinely covers — wildcards, DAG wiring, config resolution across chemistries
nobody imagined — but it was never this bug's instrument (§14).

The fix underneath: `starsolo.smk` now branches on `soloType`, because STARsolo spells barcode
location two ways and a combinatorial chemistry has no start/length to give. The position quadruples
are **computed from the element coordinates** (`derived_params`), never transcribed — a published
SPLiT-seq quadruple is chemistry-specific (v1 puts Round1 at 86-93, Parse/v2 at 78-85), so a
remembered one is a coin flip between two real chemistries. That makes the element model the single
source for where a barcode is, and adds a third param owner beside the KB and the processing
manifest: **derived**. One fact, one owner.

### Fetch and map are separate modules — decouple, do not omit

Two workflow modules sharing one interface (the manifest's file inventory):

- `fetch`: manifest -> local FASTQ tree   **[NOT BUILT — the registry holds only `map/star` and
  `map/starsolo`. Everything below is the argument for building it, not a description of it.]**
- `map`:   local FASTQ tree -> counts

Users with local data never invoke `fetch`. But at 10^4 datasets, download is both the dominant
cost *and* the dominant failure mode, and we want Snakemake's retry / resource / cluster machinery
managing it rather than a bash loop. On Slurm this is close to mandatory anyway: fetch belongs on
an I/O queue, mapping on compute. Evaluate Snakemake 8+ **storage plugins** first — remote URIs
declared directly as rule inputs, with retrieval and caching handled by Snakemake — which may make
a bespoke `fetch` module unnecessary.

### References and environments resolve at run time, never at compose time

- **Genome**: the manifest carries a `liulab-genome` assembly identifier + annotation version.
  `Genome(...)` resolves it — and its aligner index — on whatever machine actually runs.
  No genome path is ever written into a config or a workflow.
- **Software**: each rule declares an *abstract environment name* (e.g. `align-rna`); the
  execution profile maps that to a `pixi run -e` prefix or a `liulab-runtime` container.
  Environment definitions stay in liulab-runtime, in one place. We do not scatter conda YAMLs
  through the workflow, and we do not duplicate liulab-runtime's job.
  **Half built:** the composer emits the env name into the config, but no rule declares or reads it —
  neither `.smk` file carries a `conda:` or `container:` directive, so today the runner's ambient
  environment decides which STAR runs. The name is recorded, not honoured (§14).

### One real end-to-end run with ground-truth counts (manual; CI pending)

A dry run cannot catch a misspelled `--soloCBwhitelist`, an inverted `--soloStrand`, or a module
whose output the next module cannot parse — and those are exactly the bugs a config compiler
produces. So we run the real toolchain once, on data small enough to be free. **This is built and
green, and it is run by hand on a cluster** (`seqforge kb e2e`) — STAR is not in any environment
here, so the test skips locally and CI has no aligner to run it either — this one really does need a
cluster (§14):

- Reference: `Genome("sacCer3")` — 12 Mb, already handled by our own package, STAR index builds
  in about a minute.
- Reads: **simulated from sacCer3 transcripts with barcodes and UMIs we injected**, by the same
  synthetic generator as §6.
- Assertion: not "it ran" but **"the count matrix equals the ground truth we injected."**
  This is the only thing that catches a strand inversion.

Yeast is nearly intron-free, so intron-aware counting (`GeneFull`) needs its own fixture. **That
fixture exists**: `seqforge kb e2e-introns` takes the first of the two routes — a real intron-rich
genome (ce11) via liulab-genome, with reads injected into clean intronic space. It is what priced the
`soloFeatures` defect at 40.7 % (§3.14), and it carries the cost arm (`--quantify`) that prices the
all-five default.

## 9. Evals — build these alongside the first feature, not after

```
evals/cases/<case_id>/
  inputs/recipe.yaml     # HOW to build the FASTQ (kind: spec | random | local) — never the bytes
  metadata/              # GEO text, README, manuscript excerpt, or nothing
  expected.yaml          # outcome: decide | refuse | ask, plus the ground truth refining it
```

Inputs are a **generator recipe, not committed FASTQ**: the bytes are materialized to a tempdir at
run time from the same synthetic generator as §6, so the corpus stays diffable and weighs nothing.
`outcome` is mandatory and primary; the ground-truth fields refine it. (The key is `outcome`, not
`expected_outcome`, and the model forbids extras — a case file written the old way fails to load.)

Metrics, tracked whenever prompts, KB, or resolve logic change:

- field-level accuracy against ground truth
- **false-accept rate** (produced a confident wrong manifest) — the metric that matters most
- **false-refuse rate** (blocked on something it should have resolved)
- questions asked (fewer is better; failing to ask a *needed* question is a hard fail)
- tokens and wall-clock per dataset

Treat prompt and KB changes as code changes. Without this harness the system rots invisibly.

**Half true.** CI runs the unit suite on every PR — but not the evals, which need a real model and a
key. So the metrics above are computed and real, and re-baselining after a prompt or KB change is
still a manual step somebody has to remember. That is exactly the failure mode this section was
written to prevent, and it is the strongest remaining argument for wiring `eval run --llm` into a
scheduled job rather than a PR gate.

## 10. Agent layer

Skills follow the open Agent Skills standard (`SKILL.md` + progressive disclosure), so they port
across Claude Code, Codex CLI, Gemini CLI, etc. Ship an installer that places them in each
product's discovery path (`.claude/skills/`, `.agents/skills/`, ...), since those paths still differ.
**The installer is unbuilt (§14)** — the nine `SKILL.md` files exist and are copied by hand.

Skills — each one a thin wrapper over `seqforge` CLI commands. A test asserts every verb a skill
names actually exists, which is what keeps "thin client" honest; `journal` is the one skill that
fails that standard today, wrapping four commands that do not exist:

| skill | responsibility |
|---|---|
| `orchestrate` | owns the state machine; never touches files directly |
| `exam` | runs `seqforge probe`; returns a compact Observation, never raw FASTQ lines |
| `harvest` | prose/metadata to span-verified Assertions (the LLM-heavy one) |
| `resolve` | adjudicates only the conflicts and ambiguities that code flagged |
| `manifest` | fills and validates the manifest; loops until validate passes clean |
| `compose` | emits pipeline config; gates on `snakemake --dry-run` |
| `io` | remote peeking (HTTP range GET on gzip prefixes), ENA/SRA/SDL resolution |
| `kb-author` | interviews the user, writes a new KB entry + spec + fixtures, opens a PR |
| `journal` | appends decisions; distills recurring lessons |

**Subagents are for context hygiene.** `exam` and `harvest` burn tokens on bulky tool output and
long documents; they must return only a compact structured object to the orchestrator. The
orchestrator should never see a raw FASTQ line except as a short quoted example.

**On-disk state** (resumable, inspectable, diffable, greppable):

```
.seqforge/
  observations/<file_sha>.json
  assertions.json
  candidates/<dataset_id>.json   # dataset_id = sha256(sorted(file_shas) ⊕ kb_version), probe/
                                 # resolve versions folded in. Conflicts live INSIDE this artifact,
                                 # not in a separate file — one artifact, so they cannot drift apart
  questions.md            # open questions for the human   [only ever READ — nothing writes it]
  manifest.draft.yaml
  manifest.yaml           # written only once validate passes
  pipeline/<run_id>/      # config.yaml, units.tsv, onlists, processing.lock.yaml — keyed by RUN,
                          # because one dataset compiled two ways is two runs (§3.12)
  journal.jsonl           [NOT BUILT]
  LESSONS.md              [NOT BUILT]
```

**Hooks turn policy into mechanism** (do not rely on the prompt to enforce invariants):

- `PreToolUse`: block any bash command that streams a FASTQ with no read/byte bound — **regardless of
  the file's size**. The guard never stats the file, deliberately: a path that *can* stream a
  multi-GB FASTQ is a bug even when today's file happens to be small (§3.10). This makes the read/byte
  budget (200k reads, 256 MB decompressed) a hard invariant. Wall-clock is never a budget.
- `PostToolUse`: auto-run `seqforge manifest validate` after any manifest edit.
- `Stop`: refuse to end the turn while `questions.md` is non-empty.

**The journal is a flywheel, not a landfill.** `journal.jsonl` is append-only. Distillation into
`LESSONS.md` is an explicit, human-approved step, and recurring lessons get *promoted into the KB
via PR*. Make that promotion path low-friction: project journal -> distilled lesson -> KB entry ->
CI test. That loop is how the package gets better with use instead of accumulating cruft.

**None of this exists (§14).** No journal writer, no `distill` verb, no `LESSONS.md`; `grep -r journal
src/` is empty, and the `journal` skill in the table above wraps four commands that were never built.
The *back* of the path is real and now genuinely low-friction — a lesson can become a spec, and the
round-trip and pairwise-confusability checks pick it up automatically because it exists — so what is
missing is the front of the flywheel, not the whole thing.

## 11. Milestone 0 (the only thing to build first)

Vertical slice, end to end, for exactly three technologies chosen for **architectural coverage,
not popularity**:

1. **Bulk Illumina RNA-seq, paired-end** — the no-barcode branch, header parsing, run/lane grouping.
2. **10x 3' GEX v3** — onlist matching, technical-read identification, the SRA-mangling gotcha.
3. **inDrop v3 (or SPLiT-seq/Parse)** — anchored linker motif, variable-length barcode,
   combinatorial indexing. This is the one that proves the element model generalizes beyond 10x.
   If the abstractions survive inDrop's W1 linker, they will survive most things.
   **SPLiT-seq was the branch taken, and it exercises two of the three properties**: combinatorial
   indexing (three barcode rounds) and fixed linkers. It does *not* exercise a variable-length
   barcode or an anchored (search-for-me) motif — its barcodes are fixed 8 bp at fixed offsets. The
   generalization claim is therefore *partly* tested: inDrop's W1 remains the real proof (§14).

Plus three negative fixtures that must pass from day one:

- truncated / corrupt gzip -> `Blocker`
- a technology deliberately absent from the KB (e.g. an ONT run) -> "unsupported", not a guess
- a contradiction (metadata says v2, reads say v3) -> surfaced `Conflict`, not a silent pick

Ship all four stages (probe / harvest / resolve / compile) at reduced coverage rather than one
stage at full coverage. Breadth first, then depth — the abstractions are what we are testing.

## 12. Held-out acceptance case: PRJNA1027859

```
<held-out-case>/        # FASTQ, _1/_2 per SRX, 6 SRX
<held-out-case>/info/   # the paper PDF for this dataset
```

> The concrete on-disk root is intentionally **not** recorded in this repo (it is a lab path); it
> lives in local, out-of-git config. This is the first of several held-out cases.

Declared technology (from GEO): 10x Chromium, Single Cell 3' v3.1 Reagent Kit. Organism: a worm.
The FASTQs came from `fasterq-dump`, so the `_1` / `_2` suffixes carry **no reliable role
information** — they are an artifact of the dump order, not a statement about R1 and R2.

### This is a held-out acceptance test, not a development fixture

Do not read it, sample it, profile it, or tune against it during pilot development. Build against
the synthetic KB round-trips (§6). Run this dataset **once**, when the pilot is otherwise complete.
If we iterate against it, we convert our only real acceptance case into a training set and it stops
telling us anything.

### Pre-register the expected outcome, in writing, before running it

This is what makes it a test rather than a demo. Commit the expectations to
`evals/cases/PRJNA1027859/expected.yaml` *first*:

- **role assignment**: exactly one 28 bp read (16 CB + 12 UMI) and one cDNA read. The `_1` / `_2`
  ordering must be **derived from the bytes, never assumed from the filename.**
- **technology**: the 10x 3' v3 / v3.1 processing-equivalence class
- **onlist**: `3M-february-2018`, forward orientation, hit rate > 0.6
- **organism + assembly**: fill this in from what you already know about the dataset — the *system*
  must recover it from the PDF, never from a default
- **samples**: 6; the SRX -> sample mapping and the biological identity of each sample come from the
  paper, each carrying a verified source span
- **questions asked: 0. Conflicts: 0.**

Then run it and diff against the pre-registration.

### The pre-registration is not yet gradeable — fix this BEFORE the run

Half of what is pre-registered above cannot be checked by the harness as it stands, and one item
cannot even be attempted. Every fix below must land **before** the run: changing the grader afterwards
to accommodate a result is how a held-out case quietly becomes a training set.

- **Harvest cannot run on this case at all.** The harness enables the language model only when a case
  "has prose", and a local-files case has no way to point at a document — so the paper is unreachable
  and the organism can never be recovered from it. That is the *single thing this case exists to
  test* (see "The organism must come from the paper" below). Needs a `docs_glob` on the local recipe.
- **`pypdf` is not a declared dependency.** PDF extraction fails loudly rather than silently, which is
  correct, but it fails. Declare it or the run dies at the first document.
- **Not expressible by the grader:** onlist name / orientation / hit-rate, sample count and the
  SRX→sample mapping, organism, assembly, and every compose-level claim (the grader never runs
  compose). Supported today are chemistry, equivalence members, per-file roles, and rung.

### The three things this case is specifically designed to catch

**1. The missing technical read.** `fasterq-dump` without `--include-technical` silently drops the
10x barcode read. **Confirmed present in this dataset**: all six `*_1.fastq.gz` files carry a 28 bp
read (16 CB + 12 UMI), consistent with the declared v3.1 and ruling out v2 (which would be 26 bp).
The general rule still holds: if neither `_1` nor `_2` is 28 bp, the barcode read is *gone*, and the
only correct behaviour is a `Blocker` with an actionable remedy — re-fetch with `--include-technical`,
or pull the submitter's original files via the SDL API from the `sra-pub-src-*` buckets, which
preserve the original FASTQ/BAM. Emitting a manifest anyway is a failure. So is inferring that `_1`
is the barcode read *because it is named `_1`*.

Note also that 28 bp does **not** identify the chemistry. At least four 10x configurations produce a
28 bp R1 — 3' v3, 3' v3.1, GEM-X 3' v4, and Multiome GEX — and they are separated *only* by onlist
(`3M-february-2018`, a newer GEM-X list, and `737K-arc-v1` respectively). Geometry narrows to a
family; the onlist collapses it to one.

### Derived adversarial fixtures — because the real dataset is too well-behaved

PRJNA1027859 dumped cleanly, which means on its own it does **not** exercise the failure paths that
matter most. Manufacture these from the same files and put them in `evals/cases/` alongside it.
**None of the three exists yet (§14)**; `-swapped` and `-lying-metadata` are buildable today, while
`-no-technical` needs the missing-barcode-read `Blocker` built first:

- `PRJNA1027859-no-technical`: only `*_2.fastq.gz` present. Expected: `Blocker`, naming the missing
  barcode read and the `--include-technical` / SDL remedy. This is the most common GEO 10x trap and
  the clean case does not cover it.
- `PRJNA1027859-swapped`: `_1` and `_2` symlinked to each other's names. Expected: identical manifest
  to the clean case. Proves role assignment is derived from bytes, on real data rather than synthetic.
- `PRJNA1027859-lying-metadata`: the real FASTQs, plus a doctored metadata blob claiming v2.
  Expected: a `Conflict` (26 bp asserted vs 28 bp observed), surfaced — not silently resolved in
  favour of either side.

**2. v3 vs v3.1 must NOT trigger a question.** They share read geometry and the same
`3M-february-2018` whitelist, so the probe cannot separate them — and it does not need to, because
they emit **identical** STARsolo parameters. This generalizes into a schema requirement:

> **Compute whether two confusable KB entries produce identical `backend.params`. If they do,
> the ambiguity is benign and the resolver must not escalate it.**

This one is genuinely built: the biconditional runs over every spec pair in the test suite (§6). Note
it is stronger now than when written — with `soloFeatures` moved out of `backend.params` (§8),
"identical params" means precisely "these two chemistries parse reads identically", which is what
processing-equivalence should have meant all along.

A system that interrogates the user about distinctions that cannot change the output is a system
nobody will use. `confusable_with` therefore needs to distinguish *confusable and
processing-divergent* (resolve it) from *confusable and processing-equivalent* (record both,
proceed).

**3. The organism must come from the paper.** Everyone's default is human. On a worm dataset a
silently-defaulted reference produces near-zero mapping — loud here, but the same class of bug is
silent elsewhere (see the strand argument in §5). This exercises harvest -> Assertion ->
`processing.genome` end to end, on an organism that nobody hardcodes by accident.

### PDF span verification

The anti-hallucination check (§3, principle 4) greps each Assertion's source span back into the
source document. Naive grep against PDF-extracted text will fail on hyphenation, ligatures, and
mid-sentence line breaks. Extract once into a normalized canonical text, store offsets into **that**,
and verify against **that**.

This is built and works as specified; two details differ from the sketch above. The canonical text
lives at `.seqforge/normalized/<doc_sha256>.txt` — content-addressed **under the workspace, never
inside the dataset root**, because writing into a held-out root is itself a violation of the rule
that governs this case. And a `normalized_sha256` is recorded alongside, so that normalization drift
invalidates verification rather than silently passing it.

## 13. Engineering conventions

- Python 3.12+, pixi, `src` layout, CalVer, ruff + mypy strict on `models/`, `probe/`, `resolve/`,
  `manifest/`, `compose/`, `workflows/`, `harvest/`, `evals/` — everything but `cli/`, `io/`, `kb/`,
  `hooks/`.
- Pydantic v2 models are the single source of truth; export JSON Schema and reuse it for
  validation, LLM structured output, and docs. (The docs reuse is unbuilt — there is no docs site.)
- Typer CLI emitting **JSON by default** — there is no `--json` flag, and `kb list` is the one
  plain-text verb. Anything a skill can do, a shell script can do.
- pytest. *Planned:* `syrupy`/inline snapshots for golden manifests; hypothesis for the synthetic
  generator. Both are pinned; neither is imported yet (§14).
- Content-address every artifact by (input hash + tool version + params); cache observations by
  file checksum so re-runs are instant.
- Follow the existing liulab package conventions for CI/CD, lint, and release
  (see `liulab-compute-skills` and `liulab-runtime`). Lint, pre-commit and CI all follow the sibling
  shape. Note CI is the **backstop**, not the mechanism: most rules here are enforced by tests, so
  `pixi run check` before a commit is what actually holds the line.

### We are a consumer of the existing liulab stack, not a parallel universe of it

- **`liulab-genome`** (https://liuhlab.github.io/liulab-genome/) owns everything about reference
  assemblies, annotations, and aligner indexes. We reference assemblies by identifier and let it
  resolve. We do not build our own genome-file machinery, and we do not put paths in manifests.
- **`liulab-runtime`** (https://liuhlab.github.io/liulab-runtime/) owns aligner environments and
  containers (`align-rna`, `align-dna`, ...). Workflow rules name an environment; the profile
  resolves it. We do not define our own aligner environments.

If a feature we want belongs in one of those two packages, it goes there, not here.

---

## 14. What is designed here but not yet built

*Audited claim-by-claim against the code on 2026-07-15; the closable half was closed the same day.
The bootstrap prompt that used to sit here — "drop this in an empty repo and run `/init`" — was
deleted: it described work long since done, and an agent obeying it would have re-scaffolded a built
package.*

Everything in §1–§13 is the design. This section is the honest delta, kept so that a present-tense
sentence up there never has to be read as a promise. **Rule for maintaining it: when you fix
something, delete its line here and fix the tense above.** A stale §14 is worse than no §14.

### What the audit closed (2026-07-15) — kept only as a warning about how these happen

The audit's organising finding was *"there is no CI, and five rules cite it as their enforcement"*.
The fix was not CI. **CI was never the mechanism those rules needed — a test was**; CI only schedules
tests. So the four missing tests were written, and *then* a scheduler was added to run them: CI on
every push and PR. Pre-commit carries the fast hooks only — running the suite on every commit taxed
prose-only commits for no gain, so a red commit can now exist locally and is caught at push.

Every one of these had the same shape, and it is worth naming because it will recur: **a contract
maintained by hand, beside the code it describes, checked against itself.**

- Round-trip coverage was a hardcoded list of 3 while the KB had 5 — so `10x-3p-gex-v3.1` had no
  round-trip test, and "adding a tech adds its own test" was false. Now collects from
  `kb.list_spec_ids()`.
- `WorkflowModule.required_config` omitted four keys `starsolo.smk` dereferences, and the test
  enforcing it validated against that same wrong list over one hardcoded chemistry per module — which
  made SPLiT-seq structurally unrepresentable. The requirement is now **scanned out of the module
  source**; SPLiT-seq composes.
- `decidable_by` / `confusable_with` were hand-maintained claims. Now computed: the guard's first run
  found `bulk-rnaseq-pe` accepting SPLiT-seq's reads at rungs 0–2 while declaring nothing.
- `PRETRIMMED_VARIABLE_LENGTH` was declared and never emitted; R3's "50 GB" assertion was never
  written; R12's import check and R1's rule-source check did not exist. All four now do.

### Correctness gaps (the code disagrees with a stated guarantee)

| gap | where | cost |
|---|---|---|
| The composer's `snakemake -n` / `--lint` gate **has never executed** — `snakemake` is undeclared, so the test skips, and a skip is green | §8 | wildcards, DAG wiring and config resolution are unchecked. It would *not* have caught the SPLiT-seq `KeyError` (dry-run never formats `shell:`), and `--lint` would make it a constant red. Wanted: `-n -p`, `--lint` moved out, `snakemake-minimal` in its own solve-group |
| Onlist revcomp is tested only when a spec declares it; a spec pinning `forward` opts out silently | §5 | the ATAC trap §5 names is not actually guarded |
| The hypothesis is a scoring prior, not a gate — every spec is evaluated unconditionally | §5 | invisible at 5 specs; not at 500 |
| Workflow rules declare no environment (`conda:`/`container:`) — the env name is emitted and ignored | §8 | ambient STAR runs; §3.11's machine-independence is recorded, not honoured |
| `resources` fields carry no `Evidenced` basis | §7 | deliberate (a hint, not a decision) — recorded so it stops reading as an oversight |

### Unbuilt (promised, never started)

- **The journal flywheel, entirely** — no `journal.jsonl` writer, no `distill`, no `LESSONS.md`; the
  `journal` skill wraps four non-existent verbs. `questions.md` is read by the `Stop` hook and
  written by nothing. (§10)
- **`fetch` workflow module** — and the Snakemake-8 storage-plugin evaluation §8 asks for first. (§8)
- **Escalation rungs 5 and 6** — k-mer sketch, mini-alignment. A tie surviving rung 3 asks a human.
  Rung 4 is likely unnecessary: both halves already run at rungs 2–3. (§5)
- **`resolve adjudicate`** — the LLM's second job (§2) has no verb. Only `resolve score` exists.
- **seqspec export** and **scg_lib_structs ingestion** (§6). The seqspec *decomposition* is adopted;
  only the emitter is missing.
- **GENCODE/RefSeq accessions** (§7) — annotation is a local registry name, not a public accession.
- **`.npy` onlist precompilation** (§5) — packing is in-memory per process; every CLI run re-packs.
- **`syrupy` snapshots and `hypothesis` property tests** (§13) — pinned, never imported.
- **Skills installer** (§10) — the nine `SKILL.md` files are copied by hand.
- **Dual-index parsing** (§5) — the regex captures the second index; `parse_read_name` drops it, so
  "dual-vs-single index for free" is half true.
- **inDrop's W1 linker** (§11) — SPLiT-seq covers combinatorial indexing but not a variable-length
  barcode or an anchored motif, so the generalization claim is only partly tested.

### Before the held-out run — blocking

- **Harvest cannot run on the held-out case**: a local-files case has no `docs_glob`, so `has_prose`
  is false, so the language model never runs, so the organism can never come from the paper — the
  one thing §12 designed this case to prove.
- **`pypdf` is undeclared**; PDF extraction fails loudly on day one.
- **The grader cannot express** onlist / orientation / hit-rate, sample count, SRX→sample mapping,
  organism, assembly, or any compose-level claim. It never runs compose.
- **The three adversarial fixtures** (`-no-technical`, `-swapped`, `-lying-metadata`) do not exist.

### Open measurements

- ~~Peak memory for all five counting rules at 10⁴ × hg38 is unmeasured~~ — **measured 2026-07-15**;
  see below. It was filed here as "deferred to real human data", and that was wrong in a way worth
  keeping: it needed the real human **genome**, not real human **reads**. Reads simulated from real
  hg38 sequence exercise the same structures and exercise them *harder* (random UMIs ⇒ near-zero
  duplication ⇒ more distinct UMIs than real data; 91.5 % unique mapping vs a real ~60-90 %, because
  there are no sequencing errors or adapters). Every bias runs toward over-estimating, which is the
  right direction when the output is a `--mem` request. Waiting on real data cost nothing and bought
  nothing.
- ~~Velocyto is unconditional by maintainer decision, not by a measurement~~ — **it is now a
  measurement.** The pre-registered rule (">2× wall-clock or over the `mem_gb` hint ⇒ drop to four")
  was retired before it was tested; it has now been tested and does not fire.
