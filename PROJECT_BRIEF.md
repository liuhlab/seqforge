# PROJECT_BRIEF.md

> Drop this in the repo root, then run `/init` in Claude Code and give it the
> bootstrap prompt at the bottom of this file.
> `seqforge` below is a placeholder name — replace it.

---

## 1. What we are building

`seqforge` turns **(an arbitrary set of FASTQ files) + (unstructured human/database
metadata)** into **(a validated, machine-readable sequencing library manifest)** and then
into **(a runnable Snakemake pipeline configuration)**.

The end goal is large-scale, uniform reprocessing of public genomic data for downstream
genomic-AI training. That means the system runs headless across thousands of datasets, not
interactively across ten. Design for that.

## 2. The governing metaphor: this is a compiler, not a chatbot

| Stage | Executor | Output |
|---|---|---|
| **probe** | deterministic code, no LLM | `Observation` — facts derived from bytes |
| **harvest** | LLM | `Assertion` — claims from prose, each with a verifiable source span |
| **resolve** | code scores; LLM adjudicates only what code flags ambiguous | ranked candidates, `Conflict`s, `Question`s |
| **compile** | deterministic code | pipeline config + workflow module selection |

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
    and whether you are on a login node. Time is an emergent consequence. CI asserts that
    probing a 50 GB file reads under N bytes. Probes are embarrassingly parallel across files;
    ~10 s/file is an acceptable envelope. If a code path *can* touch a whole multi-GB FASTQ,
    that is a bug.
11. **The manifest is machine-independent. No absolute filesystem paths, ever.**
    Genome -> a `liulab-genome` assembly identifier + annotation version.
    Software -> a `liulab-runtime` environment name.
    Data -> a URI.
    Everything resolves at run time, on whatever cluster. This is what lets us replay a manifest
    on a different machine in three years — which matters, because we will rebuild this corpus.

## 4. Repository layout

Single repo, single `pyproject.toml`, clear internal module boundaries. Do not split into
separate distributions yet.

```
seqforge/
  models/          pydantic v2 schemas; JSON Schema export is the single source of truth
                   (used for validation, LLM structured output, and docs)
  probe/           deterministic FASTQ fingerprinting  (no LLM, no network)
  kb/              knowledge base: one directory per technology (see §6)
  resolve/         candidate scoring, role assignment, confusability, escalation
  compose/         manifest -> snakemake config + module selection
  io/              remote peeking, ENA/SRA/GEO/SDL resolution, pooch-cached onlists
  workflows/       hand-written, versioned, tested Snakemake modules  (NOT generated)
  cli/             typer app; every command supports --json
skills/            SKILL.md agent skills (open Agent Skills standard) + installer
evals/             ground-truth corpus + harness (see §9)
tests/
```

## 5. The core algorithm

```
probe(files)                     -> Observation
harvest(prose, metadata)         -> Assertions          # LLM, span-verified
score(Observation, KB)           -> candidates x role_assignment
resolve(candidates, Assertions)  -> Decision | Conflict | Question
compile(Decision)                -> config + module selection
```

### `probe` — computed from a bounded head-limited stream, no LLM, no network

**Tier A — free structural signals (no KB, no whitelist, computed while streaming anyway).**
These do most of the work, and they alone are enough to solve role assignment.

- **Per-cycle base composition.** This recovers the read layout with zero external input. Any
  cycle where one base exceeds ~90% is *constant sequence* — that is a linker / TSO / adapter,
  located and read off directly. Uniform-ACGT cycles are random regions. A run of T-dominant
  cycles is polyT. "16 random + 12 random + polyT" falls straight out of the profile.
- **Distinct-value ratio** over a candidate window (200k reads): cell barcodes recur because
  cells are resampled, so distinct/total lands around 0.05–0.3. UMIs and cDNA sit near 1.0.
  This separates CB from UMI **with no whitelist at all.**
- **Read-name grammar.** Parse Illumina headers into instrument / flowcell / lane / tile, and
  pull the index sequence out of the comment field (giving dual-vs-single index and index length
  for free). If SRA has normalized the header away, *detect and record that* — the absence of a
  real header is itself a signal, and it changes which evidence we are allowed to trust.
- **Read-length summary**, stored as `{mode, n_distinct}` and expanded to percentiles only when
  `n_distinct > 1`. Its value is **not** technology detection (short-read FASTQs are usually
  fixed-length). Its value is *data integrity*: variable length in a fixed-cycle Illumina run
  means someone already ran cutadapt/trimmomatic before uploading. That is common in GEO and it
  is a `Blocker` — if barcode offsets have shifted, we will silently produce garbage.
- N-rate, quality encoding, estimated total reads from file size / bytes-per-read.

**Tier B — targeted onlist verification (the hypothesis test).**

The onlist test is a *verification* of a stated hypothesis, not an open-ended search. Metadata
proposes "10x 3' v3"; we load **one** list and check it. Full-panel search is the fallback, run
only when metadata is absent, verification fails, or a conflict surfaces.

It is cheap, and the numbers matter because the instinct is that it is not:
a 16bp barcode is exactly 32 bits, so it is a `uint32`; the 6.8M-entry v3 whitelist is a 27 MB
sorted `uint32` array; `np.searchsorted` over 200k reads is milliseconds. **The whole test is
~100 ms including an mmap'd load.** Testing against the *entire* panel is still under a second,
because the other lists are far smaller (737K lists are 3 MB; SPLiT-seq's combinatorial lists are
96 entries). Precompile to `.npy` and cache. Exact matching suffices — at Q30 over 90% of real
barcodes match exactly, against a random-hit floor of 6.8M/4^16 ~= 0.16%. That is roughly 500:1
signal-to-noise. **Always test the reverse complement** — reverse-complemented ATAC barcodes are
a perennial trap.

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
2  Tier A structural probe                 free       layout, roles, integrity — no KB needed
3  targeted onlist check (ONE list)        ~100 ms    verifies the hypothesis
--- everything below runs only if 0 is absent, 3 disagrees, or a Conflict surfaces ---
4  full-panel onlist + motif search        ~1 s/file  open-ended detection
5  k-mer sketch vs organism panel          ~seconds
6  mini-alignment to tiny reference        ~1 CPU-min (strand, 3'/5' bias)
7  ask the human                           expensive — and that is the point
```

Rungs 0-3 are the default path and cost well under a second. Rungs 4+ are the fallback, not the
norm. Record which rung resolved each field: that record is both provenance and our primary eval
signal.

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

Adding a technology therefore automatically adds its own test, and requires no real data. Also
generate adversarial variants from the same spec and assert the system emits the *correct
Blocker or Conflict* rather than a wrong answer: SRA-mangled headers, dropped technical read,
reverse-complemented barcodes, truncated gzip.

**Confusability matrix, computed in CI.** For every pair of KB entries, determine whether the
cheap probe (rungs 0–2) actually distinguishes them. If it does not, the entry must declare
`decidable_by: [metadata | alignment | user]`. This makes "ask the human" a computed property
rather than a prompt hope, and it blocks any new technology that would silently collide with an
existing one.

Some distinctions are provably undecidable from reads alone, and the system must *know* this
rather than guess: 10x 3' and 5' have identical CB/UMI geometry; inDrop v2 and v3 share oligos
and differ only in sequencing configuration. Encode this honestly.

### Sources to ingest (with attribution)

- **scg_lib_structs** (Teichlab) — CC-BY-4.0, so we may legally derive from it with attribution.
  Ingest `docs/source/` markdown, which is more tractable than the HTML pages.
- **seqspec** (pachterlab) — adopt its Assay/Region/Read decomposition as our *interchange*
  format, not our internal model. We emit valid seqspec as an export target, which gives us
  `seqspec index` (tool strings for STARsolo / kb-python / simpleaf) for free. Provenance,
  confidence, and processing intent live in our richer layer above it.

## 7. Manifest schema

One YAML file, three top-level sections, three Pydantic models, three distinct authorities:

- `library` — physical truth about molecules and sequencer output: assay, chemistry, read layout,
  onlists, file inventory with checksums. Authority: **evidence**.
- `experiment` — biological/metadata truth: organism, tissue, condition, sample grouping,
  accessions, sample-to-file mapping. Authority: **metadata and humans**.
- `processing` — intent: reference build and annotation version, aligner, quantification mode
  (coverage-based vs ratio-based), whether variant calling is required, resource hints.
  Authority: **derived from the first two, plus policy defaults**.

`compose` must be a pure function of these three sections. Purity is what makes pipeline
generation reproducible and diffable. Hash the manifest and embed the hash, the KB version, and
the workflow version in every run's provenance record.

Use controlled vocabularies from day one (EFO/OBI assay terms, NCBI taxids, GENCODE/RefSeq
accessions). The end product is a training corpus; lineage and stable IDs are what make it
filterable and trustworthy later.

## 8. Pipeline composition

- `workflows/` contains **hand-written, versioned, CI-tested** Snakemake modules. Compose them
  with Snakemake's `module` / `use rule ... from ...` mechanism.
- The composer emits `config.yaml` + `units.tsv` + a module selection. It never emits rule source.
- `compose` is a **pure function of the manifest**. It requires no data on disk, local or remote.

### Validating the composer without any data

`snakemake -n` raises `MissingInputException` on absent inputs, so the composer creates a scratch
directory, `touch`es zero-byte files at every path in the manifest's file inventory, and dry-runs
there. This validates config, wildcard resolution, rule wiring, and every generated parameter
string — with no FASTQ present anywhere. Run `snakemake --lint` alongside.
**The composer is not done until both pass.** Wire it into the composer's unit tests.

### Fetch and map are separate modules — decouple, do not omit

Two workflow modules sharing one interface (the manifest's file inventory):

- `fetch`: manifest -> local FASTQ tree
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

### One real end-to-end run in CI, with ground-truth counts

A dry run cannot catch a misspelled `--soloCBwhitelist`, an inverted `--soloStrand`, or a module
whose output the next module cannot parse — and those are exactly the bugs a config compiler
produces. So CI runs the real toolchain once, on data small enough to be free:

- Reference: `Genome("sacCer3")` — 12 Mb, already handled by our own package, STAR index builds
  in about a minute.
- Reads: **simulated from sacCer3 transcripts with barcodes and UMIs we injected**, by the same
  synthetic generator as §6.
- Assertion: not "it ran" but **"the count matrix equals the ground truth we injected."**
  This is the only thing that catches a strand inversion.

Caveat: yeast is nearly intron-free, so intron-aware counting (`GeneFull`) needs a separate
fixture later — either a small intron-rich region via liulab-genome, or a synthetic genome with
designed introns.

## 9. Evals — build these alongside the first feature, not after

```
evals/cases/<case_id>/
  inputs/                # FASTQ (real or synthetic), possibly truncated/corrupt
  metadata/              # GEO text, README, manuscript excerpt, or nothing
  expected.yaml          # ground-truth manifest, OR expected_outcome: refuse | ask
```

Metrics tracked on every PR that touches prompts, KB, or resolve logic:

- field-level accuracy against ground truth
- **false-accept rate** (produced a confident wrong manifest) — the metric that matters most
- **false-refuse rate** (blocked on something it should have resolved)
- questions asked (fewer is better; failing to ask a *needed* question is a hard fail)
- tokens and wall-clock per dataset

Treat prompt and KB changes as code changes. Without this harness the system rots invisibly.

## 10. Agent layer

Skills follow the open Agent Skills standard (`SKILL.md` + progressive disclosure), so they port
across Claude Code, Codex CLI, Gemini CLI, etc. Ship an installer that places them in each
product's discovery path (`.claude/skills/`, `.agents/skills/`, ...), since those paths still differ.

Skills — each one a thin wrapper over `seqforge` CLI commands:

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
  candidates.json
  conflicts.json
  questions.md            # open questions for the human
  manifest.draft.yaml
  manifest.yaml           # written only once validate passes
  pipeline/
  journal.jsonl
  LESSONS.md
```

**Hooks turn policy into mechanism** (do not rely on the prompt to enforce invariants):

- `PreToolUse`: block any bash command that streams a FASTQ over ~200 MB without a head/subsample
  flag. This makes the "seconds, not minutes" rule a hard invariant.
- `PostToolUse`: auto-run `seqforge manifest validate` after any manifest edit.
- `Stop`: refuse to end the turn while `questions.md` is non-empty.

**The journal is a flywheel, not a landfill.** `journal.jsonl` is append-only. Distillation into
`LESSONS.md` is an explicit, human-approved step, and recurring lessons get *promoted into the KB
via PR*. Make that promotion path low-friction: project journal -> distilled lesson -> KB entry ->
CI test. That loop is how the package gets better with use instead of accumulating cruft.

## 11. Milestone 0 (the only thing to build first)

Vertical slice, end to end, for exactly three technologies chosen for **architectural coverage,
not popularity**:

1. **Bulk Illumina RNA-seq, paired-end** — the no-barcode branch, header parsing, run/lane grouping.
2. **10x 3' GEX v3** — onlist matching, technical-read identification, the SRA-mangling gotcha.
3. **inDrop v3 (or SPLiT-seq/Parse)** — anchored linker motif, variable-length barcode,
   combinatorial indexing. This is the one that proves the element model generalizes beyond 10x.
   If the abstractions survive inDrop's W1 linker, they will survive most things.

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
matter most. Manufacture these from the same files and put them in `evals/cases/` alongside it:

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

> **Compute in CI whether two confusable KB entries produce identical `backend.params`. If they do,
> the ambiguity is benign and the resolver must not escalate it.**

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
mid-sentence line breaks. Extract once into a normalized canonical text (`info/normalized/*.txt`),
store offsets into **that**, and verify against **that**.

## 13. Engineering conventions

- Python 3.12+, pixi, `src` layout, CalVer, ruff + mypy strict on `models/` and `probe/`.
- Pydantic v2 models are the single source of truth; export JSON Schema and reuse it for
  validation, LLM structured output, and docs.
- Typer CLI, `--json` on every command. Anything a skill can do, a shell script can do.
- pytest; `syrupy`/inline snapshots for golden manifests; hypothesis for the synthetic generator.
- Content-address every artifact by (input hash + tool version + params); cache observations by
  file checksum so re-runs are instant.
- Follow the existing liulab package conventions for CI/CD, lint, and release
  (see `liulab-compute-skills` and `liulab-runtime`).

### We are a consumer of the existing liulab stack, not a parallel universe of it

- **`liulab-genome`** (https://liuhlab.github.io/liulab-genome/) owns everything about reference
  assemblies, annotations, and aligner indexes. We reference assemblies by identifier and let it
  resolve. We do not build our own genome-file machinery, and we do not put paths in manifests.
- **`liulab-runtime`** (https://liuhlab.github.io/liulab-runtime/) owns aligner environments and
  containers (`align-rna`, `align-dna`, ...). Workflow rules name an environment; the profile
  resolves it. We do not define our own aligner environments.

If a feature we want belongs in one of those two packages, it goes there, not here.

---

## Bootstrap prompt for Claude Code

Paste this after dropping `PROJECT_BRIEF.md` in an empty repo:

> Read `PROJECT_BRIEF.md` in full, plus `../liulab-compute-skills` and `../liulab-runtime` for our
> existing CI/CD, lint, and release conventions.
>
> Then run `/init` and write a `CLAUDE.md` that encodes the brief's ten non-negotiable design
> principles as rules you will actually be checked against — especially: emit data never code;
> agents propose and code decides; refusal is an exit code; never read a whole FASTQ.
>
> Then, before writing any implementation code, produce a design document at `docs/design.md`
> covering: (1) the Pydantic model hierarchy for Observation, Assertion, Conflict, Blocker, and
> the three-section Manifest; (2) the KB `spec.yaml` schema including the `signature` and
> `confusable_with` blocks; (3) the scoring function and the joint role-assignment optimization;
> (4) the CLI verb surface. Show me the schemas and the scoring function and stop there. Do not
> scaffold the package until I have reviewed the design.
>
> Push back on anything in the brief you think is wrong. I would rather argue now than refactor
> later.
>
> One hard rule up front: §12 describes a real GEO dataset at
> `PRJNA1027859`. It is our **held-out acceptance case**. Do
> not read it, sample it, or tune anything against it during pilot development — build against
> synthetic fixtures only. I will tell you when to run it.
