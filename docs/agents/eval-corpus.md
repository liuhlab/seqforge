# The eval corpus and the ground-truth runs

Read this when you add an eval case, extend the benchmark, or change what the compose gate asserts.
One lifetime: **how we prove the compiler works, and what the proofs actually measured.** Numbers here
are dated, because a measurement without a date is a claim.

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
([ADR-0016](../adr/0016-no-held-out-dataset.md)).

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

**An unreachable package skips; it never fails. A package the corpus does not hold is reported as
`absent`.** These are two states, not one: a 404 means the archive answered and has no such package —
it was never published — so the case cannot run anywhere, for anyone, and the fix is to publish it
rather than to try again later. Both are excluded from every rate, and only one is an instruction.
Folding them together is how `GSE110823` sat out of the corpus for a release without anyone tripping
over it, behind a word that reads as transient.

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
red we fix the compiler and grade it again. A true held-out *test* set is a later milestone — scoped
below, and not decided.

One thing a fingerprint package **cannot** reproduce: a *published* FASTQ set missing a technical
read. `preflight --accession` streams the `.sra` with `--include-technical`, so a 10x barcode read
that `fasterq-dump` skipped on the way to ENA is **recovered** in the package — the tool that builds
the fixture repairs the very defect the fixture was meant to pin. Barcode-absence is therefore only
pinnable where the read is absent from the archive's read space itself: a Cell Ranger BAM submission,
where CB and UMI are tags rather than reads (`GSE208154`, the benchmark tier's only refusal — the
synthetic ones live in `evals/cases/refusal/`). Where it is merely ENA-dropped (`GSE229022`), what the
package pins is the index-read layout instead.

**BD Rhapsody Enhanced got real reads on 2026-07-31, and the gap it closed is worth naming exactly.**
Until `GSE282765-colon-crod-wta` landed (BD Rhapsody WTA on Enhanced beads, mouse colon), the Enhanced
0–3 bp diversity insert, the `GTGA`/`GACA` linkers and the anchored per-read frame recovery had been
exercised only on reads seqforge generated from its own spec — the same circularity that hid a real
defect in `splitseq` for the life of that entry. On the real slice the frame phase-locks in **92.6 %**
of R1 across the stagger, and the 384×3 Enhanced pools hit **0.930 / 0.974 / 0.961** against **0.001**
for the disjoint 97×3 pools: a ~1000× separation, so the onlist that tells the two Enhanced leaves
apart is now measured rather than assumed. The two BD cases are also a deliberate pair — GSE274290 and
this one name the *same* instrument string ("BD Rhapsody Express Single-Cell Analysis System") in their
extract protocols and differ only in the declared bead, so anything deciding BD chemistry from the
instrument rather than the R1 bytes gets exactly one of them wrong.

**Still synthetic-only, as of 2026-07-31:** `bd-rhapsody-wta-enhanced-v1` (the 97×3 sibling — only
`-v2` got real reads), `10x-5p-gex-v2` / `-v3`, and `10x-gemx-3p-v4`. Each is covered in `evals/cases/spec`
by reads seqforge generated from its own spec, which proves the generator and the spec agree with each
other and nothing else. The pre-registerable candidate for `enhanced-v1` is `GSE266161` → `GSM8238055`
→ `SRR28817193`, pinned to the 96×3 pools by a producer-authored script rather than by a measurement
of ours (`rock_roi_paper/06_Sankey_plots/wta_unmod_first_mixing_experiment.sh` matches
`^[ACTG]{0,3}${line}` against `whitelist_96x3/BD_CLS1.txt` on that run's exact filename).

**Considered and not added** — recorded here rather than in a commit message, because the next person
to grow the corpus needs the reasons, not just the outcome. These are candidates, not reservations;
nothing about a held-out set is decided.

| dataset | why not (yet) |
|---|---|
| `GSE208229` | the single-index variant of the layout `GSE229022` covers in its harder dual-index form; its only unique asset is a non-null `readTypes` string, an `io resolve` metadata property a fingerprint cannot carry |
| `GSE136049` | 10x v3 at 2×150 — the over-length-R1 case `resolve/` already names `GSE126954`'s `SRX5411291` as the exemplar of. 395 M reads, ~29 GB/file, no new coverage |
| `GSE310667` | same over-length-R1 coverage. Released Nov 2025 and never compiled here, so it is also the strongest candidate on this list should a held-out test set ever be built — which is a reason to spend it carefully, not a reservation |

Redistributable packages carry **extracted text only**. `preflight --redistributable` builds one from
FASTQs and `seqforge strip-fingerprint` repacks an existing package, dropping the raw paper for
copyright and its figures until the figure pipeline improves — the reads and pins are untouched, so
the dataset hash is preserved. A run falls back to the extracted text, so nothing we may not
redistribute reaches Hugging Face while the harvest input survives.

## Scope only — a held-out TEST set would measure what pre-registration structurally cannot

**Nothing here is decided, and there is no third tier.**
[ADR-0016](../adr/0016-no-held-out-dataset.md) is in force: this project reserves no dataset, and no
directory named below exists in the tree. What follows is the scope issue #81 asks for, written so a
later milestone can be executed without re-deriving it — and cancelled on evidence rather than drift,
for which see the last subsection.

### ADR-0016 retired one worked example; a test set is a different object

ADR-0016's argument is specific to the thing it retired. `PRJNA1027859` is the pilot's worked example,
the tutorial's source and the fixture that priced `gene_signal_lost = 0.407`, and a dataset nobody may
look at can be none of those. Its second argument — the reservation would not have caught that defect,
the pre-registration did — is true, and true for a reason that does not generalize: **that defect was
a label problem.** GEO declared single-nucleus, the expectation said so before the run, and the
compiler disagreed. Reserving the bytes was never going to surface it.

The two disciplines hold different halves of the comparison still:

| | what it fixes | what it catches | what it is blind to |
|---|---|---|---|
| **pre-registration** | the expectation, dated before the run | a transcript wearing a prediction's clothes | the *code* drifting toward the corpus |
| **reservation** | the code, never fitted to these bytes | a green rate that was fitted rather than earned | a wrong expectation — reserving never made one right |

So the crux, and the sentence to carry forward: **pre-registration is a property of the label;
held-out is a property of what happens after the grade.** `evals/benchmark/GSE283483-*` were
pre-registered before their run (2026-07-24) and are validation data all the same, because when one
goes red we will fix the compiler and grade it again. The label was honest and stays honest; the
number the second pass yields is fitted anyway. **A test set is defined by its retirement rule, not by
the honesty of its expectations** — and that is the whole of what it buys on top of pre-registration.

Which also settles the size question. Reserving *one* dataset buys a bit, not a rate, and ADR-0016 was
retiring an n = 1 reservation. Co-adaptation is an aggregate effect — a dozen cases across some number
of releases, every red fixed on its own honest merits — and only an aggregate measurement can see it.

### "Nobody has compiled it" is a claim about the bytes, and the line is already sharp

The benchmark grades two things from two sources: `library.chemistry` from the **pinned bytes**, and
`experiment.*` from a committed `records.json`. Contamination therefore has two channels, and they get
opposite rules.

**The record must be read, and reading it is not compiling.** Nobody picks a dataset without knowing
the organism, the assay, that a paper exists and that runs are public — and the `experiment.*`
expectations are *transcribed* from the record, so reading it is the job rather than a concession.
Every value the archive declares, chemistry included, is fair to write into `expected.yaml`; that is
exactly what the pilot's pre-registration does with "Single Cell 3 v3.1".

**The bytes must not be scored.** The line worth naming is not "do not download the FASTQ" — it is *do
not run the byte resolver, and do not read a probe's output*. That line is already sharp with no new
machinery: `preflight` emits a package path, per-file `sha256`, sizes and read counts, and **nothing
byte-derived** — no read length, no segment, no verdict — while `preflight --accession` streams the
first N spots straight from SRA, so the FASTQs never land. A maintainer can reserve a dataset end to
end and see only what the archive already told them. Two residues, both small, both worth stating: the
summary prints filenames, and an `_I1_` in a basename does leak a layout hint even though the resolver
itself never sees filenames; and a reserved package must stay off the public HF repo until it is
graded, because a public package is one any agent can pull and compile.

**Selection is the leak byte-hygiene cannot close.** A maintainer picking datasets whose records
resemble what `evals/benchmark` already holds builds an easy test set; one picking oddities builds a
hard one, and neither number means what it appears to. The fix is not to look less but to decide less
after looking: **pre-commit the sampling frame** — a stated population and an inclusion rule, e.g.
*every GEO series matching this query, published in this window, with at least one public run and a
paper, taken in accession order until n is reached* — then take what it yields, including the cases we
expect to fail. A frame written down first is a frame that cannot be tilted afterwards.

### Mechanize the artifact; write down the conduct

ADR-0016 deleted a `PreToolUse` guard and a root registry on purpose, and its closing line is
unambiguous: nothing can check that a value was not back-filled from a run. That is not an argument
against mechanism — it is an argument about *what kind*. The guard failed because it mechanized
**conduct**, and a conduct guard is both evadable and in the way of every other job its dataset had.
Properties of a **file** are a different matter, and this repo already mechanizes several.

The honest split, if the milestone is taken:

- **Mechanizable, and none of it a guard.** Keep the test directory disjoint from `discover_cases()`
  exactly as `evals/benchmark` already is, so reaching it means typing its path — that stops an
  automated sweep from grading it by accident, which is the realistic accident, and hides nothing from
  a human. Then check the *shape* of a test case: a `predicts` stamp present, no `AUTO-SEEDED` header,
  and an expectation commit that predates the grading commit. All three are properties of files and of
  `git log`, which is what `predicts` was designed for:
  [`evals/case.py`](../../src/seqforge/evals/case.py) already states the (a) dataset-claim /
  (b) compiler-output split that makes the audit possible.
- **Not mechanizable, and stated rather than assumed.** That nobody scored the bytes before the
  grading. Nothing sees this: it leaves no diff at all, which makes it *weaker* than the back-fill
  obligation ADR-0016 named, since an edited `expected.yaml` at least surfaces in review. The review
  obligation is therefore a staffing one — **whoever reserves a dataset is not whoever debugs against
  it** — and the grading commit records who ran it and when.

That is the resolution of "prefer an automatic mechanism over a remembered rule" against ADR-0016's
warning: mechanize the artifact, write down the conduct, and never build a mechanism whose only effect
is to make an unkept obligation feel kept.

### Graded once per milestone, because the grading spends it

**A test set is a consumable.** Grading it produces failures, the failures produce fixes, and the next
grading measures a compiler fitted to them. Nothing about that second number is dishonest, and nothing
about it is held out either. So the policy that keeps the name true is: **grade once per milestone,
and retire the cases that were graded** — they move into `evals/benchmark/`, where their remaining
value is as regression baselines, and the tier is refilled under a fresh frame.

The cost of that policy is the honest part of it: a test set carries a **recurring data bill**. A
milestone that cannot afford eight to fifteen fresh datasets does not have a test set that milestone —
it has a second validation set, and should say so rather than re-grade the old one.

**What a result would mean.** A green run refutes; it does not certify. At n ≈ 10 with no failures the
95 % upper bound on the failure rate is about 26 % (about 14 % at n = 20) — enough to catch a compiler
that is badly fitted, nowhere near enough to license a claim about 10⁴ datasets, and the number must
never be quoted without its n. The red run is the valuable one: it is the only measurement this
project would have that can tell *the benchmark is green because the compiler works* apart from *the
benchmark is green because we fixed it every time it went red*. A red is a dated **finding** and an
issue, never a merge gate — `eval run`'s exit 3 on a false accept stays a report here, as it already
is in `benchmark.yml`.

### Rough size, composition, and where it would sit beside the two tiers

Same mechanism, a third directory, a different reading discipline — there is nothing new to build.

| tier | directory | when it runs |
|---|---|---|
| **ci-benchmark** | `evals/cases` | every commit, hermetic |
| **benchmark** | `evals/benchmark` | published release or manual dispatch |
| *(scoped, does not exist)* **testset** | *not in the tree* | never automatically — dispatched by a human, once per milestone |

- **Size: 8–15 datasets.** Under about eight the rate has no resolution; over about fifteen the
  reservation costs more diagnostic data than the measurement is worth and the refill stops being
  affordable.
- **Not worm-only.** `evals/benchmark` is *C. elegans*-heavy because the pilot is, and a test set that
  inherits that restriction cannot detect that we have fitted to worm.
- **Include chemistries the KB does not cover.** Covering every leaf spec is the ci-benchmark's job.
  The question a test set exists to answer is what happens on the *unknown* at 10⁴ scale, so a correct
  **refusal** is a pass, and a frame yielding only supported chemistries has measured the resolver
  rather than the compiler.
- **Disjoint from both tiers, by accession.** `PRJNA1027859` is currently a case in `evals/cases/real`
  *and* in `evals/benchmark`; a test case sharing an accession with either tier is not held out.

### What would make this not worth doing

Written first, so the milestone can be cancelled on evidence rather than quietly abandoned.

1. **There may be no gradient to overfit along.** Overfitting needs a fitting step. Before building
   anything, classify every benchmark red so far by how it was fixed: (i) re-derived from an
   independent source — an oligo spec, a paper, the chemistry itself — or (ii) a threshold moved until
   the case passed. The SPLiT-seq purity gate and the all-five feature default were (i). **If the
   ledger is all (i), a held-out set measures nothing new**, and this milestone should be closed.
2. **The reservation costs exactly the diagnostic value the benchmark has actually returned.** Every
   dataset held out is a dataset that cannot price a defect — ADR-0016's argument, and it does not
   weaken because there are ten of them instead of one. This corpus's returns to date are findings,
   not scores.
3. **Production is a better held-out set, and it is free.** A headless run over ~10⁴ public datasets
   generates a continuously refreshed, genuinely unseen sample — refusal rate, conflict rate, blocker
   mix — that no curated set of ten can rival. If the decision this number would inform lands after
   that corpus starts running, a hand-built test set is a worse instrument bought early.
4. **It adds an obligation weaker than the one ADR-0016 already could not enforce.** "Nobody scored
   these bytes" leaves no artifact at all, where a back-filled `expected.yaml` at least leaves a diff.
   An unauditable discipline protecting a number that can only refute is a thin thing to build a tier
   around.

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

## What the runs measured

**`kb e2e`** (sacCer3, 2 000 reads, 120 genes, 8 cells, measured 2026-07-15): resolve decided
`10x-3p-gex-v3` **unaided** — no metadata hint, chemistry from the bytes alone — and the matrix
recovered with **0 spurious, 0 inflated and 0.7 % unexplained**, the remainder being STAR's own
multimapper loss. The inverted strand **collapsed 2 000 counts to 49**, which is the proof that the
gate can catch an inversion rather than merely claiming to.

**`kb e2e-introns`** (ce11 + WS298) closed the intron-rich fixture. One STARsolo run with two counting
features — identical alignment, only the counting rule differing — counted `Gene` as the exonic truth
alone (recovery 0.979) and `GeneFull` as exon plus intron (0.97), again 0 spurious and 0 inflated,
resolve again deciding the chemistry from the bytes unaided.

**That run priced a real defect, and the defect is fixed.** Gene-only counting silently discarded
**40.7 %** of a nuclear library, and the compiler *would* have emitted exactly that. The fix was not
an exit-4 question but the parse-versus-count split plus an all-five feature default — one alignment,
five counting rules, one pass — so the fixture that priced the defect is now the gate that prevents
it: with its override deleted it asserts the composed feature set against the compiler's own params
([ADR-0012](../adr/0012-produce-every-answer-rather-than-ask.md)). Velocyto is unconditional, a
maintainer decision of 2026-07-15 rather than a measurement.

**`kb e2e-cost`** (hg38, 2026-07-15) measured peak memory at corpus scale: **34.7 GB at 100 M reads
and 44.1 GB at 250 M**, so the flat regime ends between them and peak RSS is roughly a genome-sized
intercept plus a slope in reads. The ce11 fixture cannot answer this — peak RSS moved only 2.804 to
2.809 GB across a 500× read increase, because 2.8 GB *is* the ce11 index and the counting is a
rounding error on it, so a green ce11 number would have been worse than none. **Only the slope
generalizes off ce11**; the absolute figure needed the real hg38 index.

The instrument is `kb e2e-cost`, or `kb e2e-introns --quantify`, reporting wall time and peak RSS.
Note that a resource request is *intent*, so the memory hint lives on the **recipe**, not on a
workflow module.

**Still open:** above 250 M reads is extrapolation from a single post-knee point, so a deep human
library is provisioned 128 GB until the sweep extends. An expensive default is not a trap here,
because the recipe can override it.

## What the runs do not yet cover

A **SPLiT-seq** end-to-end run. The existing runs certify one chemistry's strand and nothing else.

And a simulation cannot settle SPLiT-seq's open strand question on its own, which is worth
understanding before anyone tries: simulating the reads requires assuming the strand, which is
circular. That question needs the original oligo derivation or real published data. Once it is
settled, a simulation can prove only that `compose` stays faithful to whatever the KB declares — which
is a real thing to prove, but a different one.
