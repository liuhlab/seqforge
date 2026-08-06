# A held-out TEST set: what it would measure that pre-registration structurally cannot

Scope written **2026-08-01** for [#81](https://github.com/liuhlab/seqforge/issues/81), moved out of
the eval-corpus reference page on **2026-08-05** because that page described what exists and this
describes what does not.

**Nothing here is decided, and there is no third tier.**
ADR-0016 is in force: this project reserves no dataset, and no
directory named below exists in the tree. This is written so a later milestone can be executed
without re-deriving it — and cancelled on evidence rather than drift, for which see the last section.

**Method.** No measurement. This is a design scope argued from ADR-0016's record, from the two tiers
as they stood on 2026-08-01, and from the contamination surfaces `preflight` and `io publish-package`
actually expose. The one number in it — the 95 % upper bound at n ≈ 10 — is a Rule-of-Three
calculation, not an observation.

**What it could not establish.** Whether there is any gradient to overfit along at all: that needs
the ledger the first cancellation criterion below asks for, classifying every benchmark red so far by
how it was fixed, and nobody has built it. Nor the recurring data bill, which is unpriced. Both are
prerequisites to the milestone, not products of this note.

## ADR-0016 retired one worked example; a test set is a different object

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

## "Nobody has compiled it" is a claim about the bytes, and the line is already sharp

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

## Mechanize the artifact; write down the conduct

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

## Graded once per milestone, because the grading spends it

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
issue, never a merge gate — `eval run`'s exit 3 on a false accept stays a report, as it already
is in `benchmark.yml`.

## Rough size, composition, and where it would sit beside the two tiers

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

## Candidates looked at and not added (2026-08-01)

Recorded because the reasons are the expensive part, not the outcome. These are candidates, not
reservations — a row here reserves nothing, and what each covers is stated from the covering side in
`evals/benchmark-datasets.tsv`'s `uniqueness` column, where it stays true as cases change.

| dataset | why not (yet) |
|---|---|
| `GSE208229` | the single-index variant of the layout `GSE229022` covers in its harder dual-index form; its only unique asset is a non-null `readTypes` string, an `io resolve` metadata property a fingerprint cannot carry |
| `GSE136049` | 10x v3 at 2×150 — the over-length-R1 case `resolve/` already names `GSE126954`'s `SRX5411291` as the exemplar of. 395 M reads, ~29 GB/file, no new coverage |
| `GSE310667` | same over-length-R1 coverage. Released Nov 2025 and never compiled here, so it is also the strongest candidate on this list should a held-out test set ever be built — which is a reason to spend it carefully, not a reservation |
| `GSE316206` | mouse GEM-X 3′ v4, protocol says "Chromium GEM-X Single Cell 3' Reagent Kits v4" verbatim and the sample axis is richer than `GSE305031`'s (genotype/sex/age/treatment). **The run is not loaded**: `spots=0`, no `<Statistics>`, ENA `read_count=0`. Nothing for `preflight` to stream and no declared read lengths to predict against. Worth revisiting as a mouse companion once it loads |
| `GSE308872` | says "GEM-X" but is the **wrong** GEM-X: "GEM-X OCM 3' Chip Kit v4 4-plex" — on-chip multiplexing, a barcode layout the KB has no entry for |
| `GSE337641` | wrong GEM-X again: "GEM-X Flex Gene Expression" — probe-based fixed-RNA profiling, not 3′ at all |
| `GSE325467` | a genuinely good GEM-X 5′ v3 alternative (`SRR37705344`, 28/90). Passed over for `GSE310378`, which names the exact 10x document the spec cites (CG000733 revA) and declares its read configuration in words |
| `GSE319238` | one series carrying 5′ v2 microglia *and* 3′ v3.1 whole brain — attractive, but the protocol sentence is a **conditional naming both kits** for every sample, so a per-sample chemistry claim would require resolving the condition. Weaker pre-registration than `GSE317744`'s one-kit-per-sample statement; keep as a future `steering/` candidate |

## What would make this not worth doing

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
