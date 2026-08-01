# 18. A benchmark case a real dataset makes red is published anyway

Date: 2026-08-01

## Status

Accepted. Reverses the decision to withhold `GSE110823`'s fingerprint package (issue #156 §5).

## Context

`evals/benchmark/GSE110823` is Rosenberg et al. 2018 — the dataset SPLiT-seq was published on, and
the only real SPLiT-seq reads anywhere in this corpus. Every other chemistry is proven on real
barcodes by its own case; `splitseq` was proven only on reads seqforge generated from its own spec,
which is the circularity that hid a real defect in that entry for its whole life.

When the case was committed on 2026-07-23 it was **red, and known to be red**, for a reason
understood down to the number: the constant-segment purity gate demanded a per-cycle
`mean_maxfrac >= 0.9` and real reads give linker2 **0.827** (0.99+ over the ~61 % of reads that are
genuinely SPLiT-seq; the rest is head junk). So `resolve score` returned `bulk-rnaseq-pe` at 0.98
instead of `splitseq`. `expected.yaml` said `splitseq` anyway, deliberately: it is a **pre-registered
prediction of a fix**, and grading it against what the code did would have enshrined the defect as
the specification.

The package was therefore built and **not uploaded**. The stated reason (#156 §5) was that
"uploading it now would make the benchmark permanently red for a reason already understood."

**The prediction has since come true, and that is the sharpest argument in this record.** A constant
segment is now scored as a share of reads rather than a per-cycle purity, so the off-structure head
junk no longer forbids the `bc` role; on the package published here the case grades `correct`. The
withholding therefore hid two things rather than one — not only the defect, but the *fix landing*.
For a release the corpus could not report the gap, and then could not report that the gap had closed.
A rule that hides a red also hides the green that follows it.

That reasoning is locally sound and globally wrong, and a future reader will reach it again from the
same evidence, because the alternative *looks* like discipline: a corpus you keep green is a corpus
you can trust at a glance. This record exists so that reading does not have to be re-derived and
re-rejected. Two facts make it wrong here. **Eval is not pytest.** The suite asserts what the code
must do and a red suite is a broken build; the eval corpus *measures what the compiler currently
does*, and a red case is a dated finding — the benchmark tier is opt-in, runs on a release or a
manual dispatch, and gates no merge at all. **And withholding hid the wrong thing.** A withheld
package is indistinguishable, at the fetch seam, from a network failure: both were one
`BenchmarkPackageUnavailable` and both rendered as one grey "skipped" card. So the corpus lost its
only SPLiT-seq measurement *and* lost the ability to say it had lost it.

## Decision

**A benchmark case that a real dataset makes red is published anyway. A corpus that holds only the
cases it passes measures nothing.**

Three obligations follow, and they are the price of the decision rather than decoration:

| | |
| --- | --- |
| **The case header says which colour it is, and why** | a red nobody predicted is a defect report; a red the file predicted is an instrument. `GSE110823`'s header carried its failing number before the run, and now records that the prediction was met |
| **The corpus never withholds to stay green** | the only reasons to keep a package off the public repo are the ones that are not about the score: copyright (`--redistributable`), and a dataset reserved for a test set that has not been graded |
| **Absent is not a skip** | a package the corpus does not hold is reported under its own name, so "we have no measurement here" cannot read as "the network had a bad day" |

## Why not withhold until the gate is fixed

Because the gate is what the case is *for*, and withholding inverts the instrument.

1. **The red was the measurement.** The purity gate's 0.827 was not a bug the corpus tripped over —
   it was the finding the corpus exists to produce, and the only evidence that the SPLiT-seq entry
   was wrong about real reads rather than about its own generator. Withholding it deleted the
   finding and kept the defect; publishing it is what lets the corpus now show the finding closed.
2. **It makes green mean less, not more.** A rate computed over cases selected for passing is not a
   rate. `13 packages for 14 cases` with `field_accuracy 1.0` reads as a stronger claim than
   `14 of 14 with one red`, and is a weaker one.
3. **The condition never arrives on its own.** "Upload it when the gate is fixed" makes the upload
   depend on the fix and the fix depend on nobody's attention, since the case is invisible until
   then. The package sat built and unpublished for a release.
4. **It is the discipline this project already rejected once.** [ADR-0016](0016-no-held-out-dataset.md)
   retired the held-out designation and closes: *"When a case goes red we fix the compiler and grade
   it again, which is precisely what a held-out set forbids."* That sentence is the whole of this
   decision, applied one level out. `evals/benchmark` is the **validation** set we develop against;
   a validation case that is hidden while it is red is not a validation case, and the honest name for
   a corpus curated on its own score is a demo.

## Why not a third grade, or an `xfail`

A "known red" marker would be the same withholding written in the report instead of in the upload,
and it decays the same way: the marker outlives the reason, and nothing goes red when the case
starts passing for the wrong reason. The grade vocabulary already carries this — the case grades
`false_accept` or whatever it earns, and `expected.yaml`'s pre-registration is what makes the grade
mean *"the compiler is not there yet"* rather than *"the expectation is wrong"*. A prediction with a
date is a stronger instrument than a mark that suppresses one.

## So in code

**Publish a benchmark package without consulting the colour of the case it feeds, and say in the case
header which colour it is and on what number.** Never withhold a package to keep a rate green — the
only admissible reasons to keep one off the public repo are copyright (build it with
`preflight --redistributable`) and a dataset reserved for a test set that has not been graded yet.
Put it in the corpus with `seqforge io publish-package` rather than by hand, and check `--dry-run`'s
URL against the recipe's `hf:` key before committing, because the build's content-addressed filename
is not the corpus key. And when a package is missing, report **absent** rather than a bare skip: at
the fetch seam a 404 raises `BenchmarkPackageAbsent`, which the harness carries to `skip_kind` and
the page renders under its own label.

**Enforced by.** `test_a_404_is_absent_and_a_5xx_is_merely_unreachable` and
`test_the_404_is_found_even_when_pooch_wraps_it` (`tests/test_io.py`);
`test_the_harness_carries_the_absent_state_from_the_fetch_seam_to_the_json` and
`test_a_package_the_corpus_never_held_reads_as_a_gap_and_not_as_a_blip` (`tests/test_evals.py`);
`test_the_benchmark_dataset_table_covers_every_case_and_agrees_with_it` (same file), which is what
fails if a case is added or removed without its row. **Nothing can check that a package was withheld
to keep a rate green** — a package nobody uploads leaves no diff at all. What makes it *visible* is
the absent state: the corpus now says out loud which cases it holds no bytes for, which is the
closest a mechanism gets to the obligation, and the rest is a review obligation.

## Consequences

- **The benchmark tier is expected to carry reds, and its numbers must always be read with `n`.** A
  rate over a corpus that includes its failures is the only rate worth quoting; `field_accuracy` is
  reported beside the case list for exactly that reason.
- `GSE110823` grades `correct` on the package published here: the constant-segment gate it predicted
  had already been fixed, so the corpus gained a closed finding rather than an open one. The decision
  is not weakened by that — it is why the outcome is visible at all, and the next case published
  under this rule may well stay red for a release.
- The fetch seam gained a second exception type and the harness a `skip_kind`. That is the whole
  mechanical cost, and it is what stops this decision from silently reverting: withholding a package
  now shows up on the page as `absent` rather than as a skip nobody reads.
- **Publishing became a verb.** `seqforge io publish-package` is the producer half of the fingerprint
  contract `preflight` already owned the builder half of. It is in the `io` group because it is a
  network operation and nothing else, and `huggingface_hub` is a producer-side dependency only — the
  consumer still pulls anonymously through pooch with no token
  ([`docs/agents/eval-corpus.md`](../agents/eval-corpus.md)).
- This does **not** license a red in the ci-benchmark (`evals/cases`), which runs hermetically on
  every commit and *is* a gate. The two tiers are disjoint on purpose, and this record is about the
  measured one.
