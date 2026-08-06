# The eval corpus and the ground-truth runs

Read this when you add an eval case, extend the benchmark, or change what the compose gate asserts.
One lifetime: **how we prove the compiler works, and what the proofs actually measured.** Numbers here
are dated, because a measurement without a date is a claim — and a measurement with a method is a
[`docs/research/`](../research/) note that this page carries the conclusion of.

## The demo dataset

**`PRJNA1027859`** is the pilot's worked example — the dataset the tutorial is written from, and what
"it works end to end" means concretely. `ce11` (*C. elegans*, taxid 6239, WBcel235) is confirmed
available in `liulab-genome`.

Two disciplines, and only two, survive from its history:

- **Real data, and its path, stay out of git.** The on-disk root lives in local, out-of-git config; a
  local eval case names an environment variable instead, and `test_skill_never_leaks_a_lab_path`
  enforces it. Real FASTQs are far too large for git, and a lab path is not a project fact.
- **Pre-register `expected.yaml` before a run.** Only a prediction can be wrong. Its claims must be
  *checkable* against the manifest — a per-sample attribute for one accession, or the same attribute
  across all of them.

**There is no held-out dataset.** The designation was retired on 2026-07-15 and its guard and registry
were **deleted, not suspended**; the pre-registration stands in their place
([ADR-0016](../adr/0016-no-held-out-dataset.md)). A true held-out *test* set is a later milestone,
scoped as scope and not as a decision in
[`docs/research/held-out-test-set-scope.md`](../research/held-out-test-set-scope.md).

**Still open, for the maintainer:** whether pre-registering the pilot's organism sits comfortably with
"don't tune against it". The safe reading, and the one in force: `expected.yaml` uses archive-declared
metadata and a provider-independent prior only, committed before any run, and never a value read out
of the data.

## The benchmark, in two tiers

A dataset enters the corpus through a byte-light **fingerprint package** rather than its FASTQs. The
`fingerprint` recipe kind ([`evals/case.py`](../../src/seqforge/evals/case.py)) unpacks a package,
stamps each slice's pinned whole-file identity back on — so resolve reaches the same verdict, and the
same hash, that the originals would — then grades chemistry from the pinned bytes and sample
attributes from a committed `records.json`. No full FASTQ, and no API key.

A package comes from one of three sources: a path committed in the case directory, a path in the
public Hugging Face benchmark repo (`liuhlab/seqforge-benchmark`), or a root staged out of git behind
an environment variable.

**Reading a package needs no token and no SDK; publishing one needs both, and that asymmetry is the
design.** A public HF dataset serves every file at a stable URL to an anonymous GET, so the consumer
side is the same pooch call the onlist registry makes. Writing is an authenticated commit, so
`seqforge io publish-package` uses `huggingface_hub`'s `HfApi().upload_file` and the maintainer's
write token — never the `hf` command-line client, which hangs. The dependency is a hard one, so it
installs everywhere, but the import is local to the publishing path: nothing on the reading side
executes it, the networked eval job never reaches it, and the CI job carries no secret. It sits
beside `anthropic` and `openai`, which are declared the same way for the same reason — a verb
most installs never call still has to work without an extras incantation.

**An unreachable package skips; a package the corpus does not hold is reported `absent`.** Both are
excluded from every rate and only one is an instruction, because a 404 means the archive answered and
the fix is to publish rather than to retry — the argument, and what folding the two together cost, is
[ADR-0018](../adr/0018-a-red-benchmark-case-is-published-anyway.md).

Two tiers ride that mechanism, in two directories, and they are disjoint on purpose:

| tier | directory | when it runs |
|---|---|---|
| **ci-benchmark** | `evals/cases` — synthetic per-spec recipes covering every leaf spec, plus any committed tiny fingerprint | hermetically, in `test_corpus_is_green`, on every commit |
| **benchmark** | `evals/benchmark` — real datasets whose text-only fingerprint packages live on Hugging Face | only in `benchmark.yml`, on a published release or manual dispatch |

Disjointness is the point: a package pull can never sneak into hermetic CI, so a free Hugging Face
account's rate limit can never gate a PR.

Each benchmark case commits its `records.json` — the archive's own BioSample and SRA transcript — so
sample facts grade deterministically with no NCBI key, while `library.chemistry` grades from the
pinned bytes. **Provenance is per case, and the file's own header says which.** The first tranche was
*seeded from a run* on 2026-07-23 and was **reviewed against the publications on 2026-07-31**, case by
case (issue #81) — a file carrying `AUTO-SEEDED … PENDING MAINTAINER REVIEW` has not been reviewed,
and none does today; later cases were **pre-registered before their run**. Seeded, reviewed or
pre-registered, every one of them is still the **validation** set we develop against: when a case goes
red we fix the compiler and grade it again.

**What each case uniquely covers is one row of `evals/benchmark-datasets.tsv`, and why it grades as
it does is its own `expected.yaml` header.** Both are checked against the case files themselves by
`test_the_benchmark_dataset_table_covers_every_case_and_agrees_with_it`, so neither can rot silently;
a third copy in prose here would be the one that does. Read those before adding a case, and write the
new case's row and header rather than a paragraph on this page. Recipe knobs that exist only because
some deposit shape was otherwise inexpressible — `over_length`, `deposit` (N libraries × M lanes),
`dilute`, `shallow` — are documented in the recipe schema beside the case that needed them.

One thing a fingerprint package **cannot** reproduce: a *published* FASTQ set missing a technical
read. `preflight --accession` streams the `.sra` with `--include-technical`, so a 10x barcode read
that `fasterq-dump` skipped on the way to ENA is **recovered** in the package — the tool that builds
the fixture repairs the very defect the fixture was meant to pin. Barcode-absence is therefore only
pinnable where the read is absent from the archive's read space itself: a Cell Ranger BAM submission,
where CB and UMI are tags rather than reads (`GSE208154`, the benchmark tier's only refusal — the
synthetic ones live in `evals/cases/refusal/`). Where it is merely ENA-dropped (`GSE229022`), what the
package pins is the index-read layout instead.

**One package is one library, and `--multi-experiment` is the caller saying two experiments are one.**
The default refusal keeps a series mixing modalities — GSE283483's bulk RNA + Multiome GEX + Multiome
ATAC — out of a single package, and it stays. The flag is an assertion by the caller and never an
inference from the data (#242). Chemistries differing across the spanned experiments are **not** a
problem downstream: `resolve_runs` resolves each run on its own bytes and `by_chemistry` partitions
them into assays. What blocks is a *sample* whose files span chemistries, and that check is unchanged.

Redistributable packages carry **extracted text only**. `preflight --redistributable` builds one from
FASTQs and `seqforge strip-fingerprint` repacks an existing package, dropping the raw paper for
copyright and its figures until the figure pipeline improves — the reads and pins are untouched, so
the dataset hash is preserved. A run falls back to the extracted text, so nothing we may not
redistribute reaches Hugging Face while the harvest input survives.

### What the corpus has caught, and the lessons worth not rediscovering

Each is a dated finding whose detail is in the case's own row and header; what generalizes past one
case is here.

- **A head slice is not a random sample, it is the flow cell's first tiles, which is precisely where a
  dark cycle lives** (2026-08-01, `GSE305031`). The fix (#177) was to the hit rate's **denominator**,
  not to the read budget: an unpackable window measured how many cycles the sequencer called rather
  than which whitelist the library came from, so a dark cycle now costs **coverage** and leaves the
  **rate** alone. `RESOLVE_VERSION` bumped and `PROBE_VERSION` did not, because the defect fixed is a
  cached *refusal*.
- **An absence is only tested by a case that reaches the leaf without it** — why the 5′ family needed
  two cases where BD Enhanced needed one. A separation you can measure (the Enhanced pools at
  0.930/0.974/0.961 against 0.001 for the disjoint 97×3, 2026-07-31) is honest coverage from one real
  case; `10x-5p-gex-v2`'s separation from `10x-3p-gex-v2` is a documented *absence*, and one case
  would either carry a hypothesis and never test the whitelist or omit one and never reach v2.
- **An honest question settles at `ask`; a manufactured one stays at `decide`.** `GSE126954` stops on
  the KB's one declared read-undecidable pair, whose entry says so and neither of whose declared
  mechanisms is reachable, so its expectation is `ask` and its seven field claims still grade.
  `GSE234962` stops on a `library_strategy` string re-read out of the record that typed it — moving
  that one to `ask` would enshrine the defect as the specification (#184).

### The `--llm` pass grades harvest, and a stage that did not run says so

Two decisions, both 2026-08-01. **The tier grades harvest** (#164) rather than smoke-testing it: seven
of the frozen eighteen packages carry an `info/text` document at all, so this tier is where a claim
meets prose somebody else wrote, while the adversarial cases stay synthetic in `evals/cases/prose`
because a trap has to be constructed. And **a harvest stage that never ran is not one that found
nothing** (#182): the abort's blast radius is one document rather than one dataset, every harvest
grade carries a `status` of `complete`/`partial`/`unmeasured`, a skip enters no rate, and a claim a
failed document would have been asked is `unchecked` — never `missing`, which asserts the model read
everything and did not say it. Consequently `--trials N` is the wrong instrument on this tier and
three single-trial runs are the right one: before #182 all N trials skipped together, so the flag a
maintainer reaches for to measure stability was the one that hid it.

Both decisions are argued at length, with their costs, coverage and token counts, in
[`evals/README.md`](../../evals/README.md) — see *"The `--llm` pass on this tier grades harvest too"*,
*"A stage that did not run is not a stage that found nothing"*, and *"The default model, and the run
that decided it"* (#188, which reversed the default to `deepseek-v4-pro`). That file sits beside
`expected.yaml` and is the measurement's home; this page carries the decision and points there.

One consequence is **not** about harvest and belongs here: `GSE282765-colon-crod-wta` graded
`false_accept` under `--llm` and `correct` without it for a *resolver* reason, not a hallucination. A
quote of an experiment title re-derived `experiment.samples.treatment` from the same submission that
had typed it into a BioSample slot, both positions arrived at `resolve.records._decide` as `asserted`,
and equal authorities that disagree leave the attribute **null**. **Fixed (#182): one archive deposit
read at two of its levels is one source**, so a prose reading wholly inside the typed value is
absorbed rather than tied against. Containment holds in one direction and over whole words, so a
reading that *extends* the typed value (`control` read as `control RNAi`) is still a disagreement and
still leaves null.

### The nineteenth case is a plate, and it is published red on purpose

`GSE207085-nasal-prox1-96cells` is the first case in either tier where **the cell barcode is the
file**: 96 FACS-sorted murine nasal Prox1+ cells, one SMART-seq3 library each, demultiplexed at the
bench. PRJNA853582 is strictly 1:1:1:1 — 1440 runs, 1440 `SRX`, 1440 BioSamples, one distinct
BioSample attribute block across the whole population, measured in
[`docs/research/gse207085-archive-shape.md`](../research/gse207085-archive-shape.md) — so it enters
under one `--multi-experiment` assertion and is the corpus's only many-experiment package. That is
what it is for: 96 samples in one manifest exhibit the sample explosion, the plate-splitting hazard
and the dud-well hazard that ten one-cell fixtures exercise not at all.

**It was published red and the red was the measurement**
([ADR-0018](../adr/0018-a-red-benchmark-case-is-published-anyway.md)). Measured on ten cells of this
same deposit (#230), seqforge decided the generic bulk chemistry at exit 0 and `compose` selected
`map/star` — a bulk gene-count matrix for a single-cell experiment, most confident on the cleanest
cells. `expected.yaml` said `smartseq3` anyway, committed before any run and before the entry it names
existed. It went green on 2026-08-04 (#296) with that file never edited.

**Selection stays unselected, and that is the decision** (#258). The rule is *sort the 1440 runs by
`run_accession` ascending, take the first 96*, and it ships as `build-package.sh` in the case
directory rather than as a sentence, because the verb takes one accession and packages every run under
it — "96 of 1440" cannot be said on the command line. The sort is load-bearing (ENA's filereport does
not return these rows in accession order), and the block it yields runs against the submitter's own
cell numbering, which is evidence the rule tracks nothing they arranged. A plate picked to contain a
starved cell would be a designed test wearing a real dataset's clothes; no cell in this draw is below
`min_input_reads`, so the drop path belongs to the hermetic tier below.

**`-n 2000`, against the tier's usual 20 000.** 2 000 is `DEFAULT_MAX_READS`, so the package holds
exactly what resolve reads and still reproduces the manifest hash — **and cannot serve a probe budget
above 2 000 without being rebuilt.** The other build parameters, and what the build costs, are
comments in the case's own `inputs/recipe.yaml`.

**The committed transcript is 289 records, and the projection that said 60 KB counted cells.** A plate
deposit gives every cell its own BioSample *and* its own experiment *and* its own run, so 96 cells are
`1 + 96 + 96 + 96` records. Nothing is pruned, and the repetition is the point: 96 experiment records
carrying the *same* 245-character `library_construction_protocol` are the only real instance in either
tier of the near-identical-record shape harvest's collapse exists for.

**The frozen-18 grade digest is [`evals/digest.py`](../../src/seqforge/evals/digest.py), and it
refuses rather than filters.** The recipe hashes `n_cases` *and* the whole per-case list, so
equal-digest and add-a-case are incompatible instruments and `grade_digest` raises on a report holding
anything but `FROZEN_18` rather than silently dropping rows. The module owns the baselines, what moved
them and what did not, because prose in a doc cannot be run any more than prose in an issue can.
**Quote a hex with the tree it was taken on**, re-take it on the tree you are about to change, and
diff it against that same tree: a hex carried over from a neighbouring tree has been misattributed
once already.

### The plate's designed behaviours are hermetic, and one of them could not be built as written

The real plate above is deliberately un-tilted, which is exactly why it covers none of the plate's
*rules*: no cell in its accession-ordered draw is under the read floor, and choosing one that was
would measure our selection. Four hermetic cases carry the rules instead (2026-08-04, #294), built by
construction at no data cost, and all four graded `correct` on their first run against
pre-registrations committed before the inputs existed.

| case | outcome | what only this case pins |
|---|---|---|
| `grouping/plate-with-a-starved-cell` | decide | three cells, one at 400 reads against a floor of 1000: the manifest keeps all three, the pipeline is contracted for two |
| `steering/tag-at-the-floor-undeclared` | ask | a thin library with no metadata — the tie stands, and the fallback is what the run provisionally lands on |
| `steering/tag-at-the-floor-declared` | decide | byte-identical inputs plus the archive's own sentence — the tie collapses to `smartseq3` |
| `refusal/plate-cell-below-the-tag-floor` | refuse | one cell *under* the gate dissents outright, and the deposit refuses rather than partitioning into two assays at exit 0 |

**The refusal case's ticket said "below the tag floor … it ties … and it refuses", and the live code
does not do that.** Below the declared 2 % floor `smartseq3` fails its own admission gate, so the
generic fallback is the only candidate and **decides at exit 0** — and no declared string rescues it,
because a claim cannot put back a chemistry the bytes excluded. **There is no exit-3 path out of a tie
at all**: a processing-divergent tie always becomes a Question at exit 4, and the exit-3 refusal the
corpus was owed is a *different* mechanism — a cell whose bytes exclude the plate's chemistry
outright. Both are shipped rather than one relabelled as the other.

### The regression protocol, and the run it prescribes

Written down here rather than in the pull request that ran it (2026-08-04, #296), because it is a
procedure the next person changing the shipped path has to repeat.

**Who runs what.** The hermetic tier is CI on every commit. **The networked tier is
maintainer-launched, single-trial, with `--llm` excluded** — six single-trial `--llm` runs of one case
graded two `correct`, two `over_ask` and two aborted, so a single-trial harvest grade is a coin flip
and a digest over one would be an instrument nobody trusted twice. `--no-llm` reaches no model, needs
no credential, and grades chemistry from pinned bytes and sample facts from committed records.

**The protocol is two commands, not one**: a run over exactly the frozen eighteen, which the digest is
taken from, and a run over the whole tier, which is what turns the plate case green. Land each change
separately with its own before/after digest pair, both re-taken on the same tree.

**Pre-declare the moves, and assert the strings before the run rather than after.** #296's example is
the shape to copy: two lines running `kb.match.resolve_chemistry` over `GSE207085`'s own declared
protocol string, passing **first**, so a red afterwards is about the work and nothing else. A
pre-declaration that turns out unnecessary is the instrument working; one invented afterwards is not.
Note that every `run_id` moves whenever `KB_VERSION` or `WORKFLOW_VERSION` bumps, so such a run is
**cold** and no cache carries an answer into it.

**Budget the clock.** The eighteen finish in about 91 s; the plate case roughly doubles the tier's
wall clock on its own, because 96 cells are 96 resolves against the whole KB. It is the tier's most
expensive case by an order of magnitude and it is worth it — it is the only place the sample explosion
is measured on real bytes.

Two ruler changes (#184/#188, #196) were landed on their own before any compiler fix and moved no
grade for the reason they were made; the runs, and why the movement that did happen is not
attributable to them, are in
[`docs/research/ruler-changes-that-moved-no-grade.md`](../research/ruler-changes-that-moved-no-grade.md).
So was #307's support-normalizer change, which was *predicted* to move the digest and did not
([`docs/research/support-normalizer-asymmetry.md`](../research/support-normalizer-asymmetry.md)) — an
unchanged digest across a deliberate semantic change is informative only because both ends were taken
on the same tree.

**The `--llm` blind spot is structural and this protocol does not close it.** No routine gate observes
the harvest path: the hermetic tier excludes every case that harvests, and a `--no-llm` digest never
calls harvest. Closing it in general is separate work.

## The compose gate has three parts, because a dry run cannot catch a strand inversion

`compose` is a pure function of the (manifest, recipe) pair and touches no data on disk. Its gate is
therefore split:

1. **Wiring** — touch zero-byte files at every path in the file inventory **and** at every resolved
   onlist cache path, so a whitelist declared as a Snakemake input does not raise a spurious missing-
   input error; then dry-run and lint. This catches config, wildcard resolution and rule wiring.
2. **Params** — deterministic assertions that the emitted strand, UMI length, CB length and
   `--readFilesIn` **order** (cDNA read before barcode read, derived from the role assignment) match the
   KB's backend params. These are the semantic bugs a linter cannot see, and they must **not** be
   attributed to the dry run.
3. **A real end-to-end run** — reads simulated from a real transcriptome with injected barcodes and
   UMIs, driven through the *whole* compiler and then through STARsolo with the composed params, with
   the resulting matrix asserted against the injected truth.

**`skip` is first-class.** Parts 1 and 3 depend on a toolchain seqforge does not own — Snakemake for
one, STAR plus `liulab-genome` plus network for the other — and the count-matrix run is a Linux and
cluster operation. So each part reports pass, fail or **skip**, and part 2, which needs no toolchain,
always runs. A gate reporting `pass` because it never ran would let a green CI be mistaken for
coverage.

### The assertion is "accounted", not naively exact

Real transcripts multimap. Paralog and subtelomeric families that STARsolo legitimately drops make
`observed == injected` unachievable, and demanding it would only teach us to weaken the gate. So the
gate asserts the four things that indict **us**:

- **0 spurious pairs** — never count a read for a gene it did not come from;
- **0 inflated counts** — never invent a UMI, which is exactly what a dedup or geometry bug looks like;
- **unexplained loss at or under 2 %** — subtract STAR's own multimapper loss, read from its log; what
  remains is the compiler's error, and it must be near zero;
- **strand sensitivity** — the same reads re-run under an inverted strand must *collapse*, or the gate
  could not have caught an inversion in the first place.

A fifth clause rides along and is about the aligner rather than about us: **cell-filter determinism**.
`--soloCellFilter EmptyDrops_CR`, adopted in #198, is Monte-Carlo — 10 000 ambient simulations — and
the barcodes it calls are archived in every `<sample>.qc.json.gz`. A content-addressed artifact that
changes when nothing changed has a hash that means nothing, so the gate filters one synthetic raw
matrix twice and demands byte-identical `barcodes.tsv`. It loads no genome and reads no FASTQ, so it
costs about a second; it asserts nothing about *which* barcodes are right, only that asking twice
gives one answer. That was measured once, on one real sample, before the caller was adopted — this is
what keeps it true across a future `align-rna` that bumps STAR underneath us.

## What the runs measured

Three runs on 2026-07-15, one conclusion each; the numbers, the boxes and the commands are in
[`docs/research/e2e-gate-runs.md`](../research/e2e-gate-runs.md).

- **`kb e2e`** (sacCer3) — resolve decided the chemistry from the bytes **unaided**, the matrix
  recovered with 0 spurious and 0 inflated, and the inverted strand collapsed 2 000 counts to 49. The
  gate can catch an inversion rather than merely claiming to.
- **`kb e2e-introns`** (ce11 + WS298) — closed the intron-rich fixture, and priced a real defect:
  gene-only counting silently discarded **40.7 %** of a nuclear library, which the all-five feature
  default now prevents ([ADR-0012](../adr/0012-produce-every-answer-rather-than-ask.md)).
- **`kb e2e-cost`** (hg38) — peak memory at corpus scale is a genome-sized intercept plus a slope in
  reads, and **only the slope generalizes off ce11**. A resource request is *intent*, so the memory
  hint lives on the **recipe**, not on a workflow module. Above 250 M reads is extrapolation from a
  single post-knee point, so a deep human library is provisioned 128 GB until the sweep extends.

## What the runs do not yet cover

A **SPLiT-seq** end-to-end run. The existing runs certify one chemistry's strand and nothing else.

And a simulation cannot settle SPLiT-seq's open strand question on its own, which is worth
understanding before anyone tries: simulating the reads requires assuming the strand, which is
circular. That question needs the original oligo derivation or real published data. Once it is
settled, a simulation can prove only that `compose` stays faithful to whatever the KB declares — which
is a real thing to prove, but a different one.
