# 16. No held-out dataset — a pre-registered prediction instead

Date: 2026-07-31

## Status

Accepted (2026-07-15). Supersedes the held-out acceptance-case designation.

## Context

`PRJNA1027859` was designated a **held-out acceptance case**, enforced as mechanism: a `PreToolUse`
guard plus a `SEQFORGE_CASE_*` root registry that refused to let development read it. The intuition
is the ML one — keep a set you have never looked at, so a green result means something.

## Decision

**The designation is retired; the project has no held-out case.** Reserving this dataset was a
misunderstanding of what it is for: it is the *pilot's worked example* — the dataset the tutorial is
written from, and what "it works end to end" means. The guard and the registry are **deleted, not
suspended**.

What replaces it is the discipline that was doing the real work all along:
`evals/cases/real/PRJNA1027859/expected.yaml` is **pre-registered** — written from GEO-declared
metadata and provider-independent prior knowledge only, committed **before any run**, never from a
value read out of the data. That is what makes the file a prediction rather than a transcript, and
**only a prediction can be wrong**. It never depended on the data being reserved.

## Why not keep the reservation

A set you may not look at cannot also be the worked example, the tutorial source, and the fixture
that prices a defect — and it was pricing one. `gene_signal_lost = 0.407`
([ADR 0012](0012-produce-every-answer-rather-than-ask.md)) was surfaced by pre-registering *this*
dataset from its **declared** metadata (single-nucleus RNA-seq), before the run, without touching it.
The reservation would not have caught it; the pre-registration did.

## So in code

**Reserve no dataset; write `expected.yaml` before the run that would confirm it.** Fill it from
declared metadata and provider-independent prior knowledge only — a value read out of a run makes
the file a transcript, and a transcript cannot be wrong. Write claims that are *checkable*, and both
shapes: `experiment.samples.*.<attr>` and `experiment.samples.<accession>.<attr>`, because `*` alone
passes on a shuffled join. Name a lab path through an environment variable; a `kind: local` case
never carries one literally. Do not add a guard that hides a dataset from development, and do not
cite the benchmark corpus as a pre-registration — read the case's own header, because a seeded and
since-reviewed expectation is still a regression baseline, not a prediction made before the run.

**Enforced by.** `test_skill_never_leaks_a_lab_path` (`tests/test_skills.py`);
`test_the_pilots_pre_registered_sample_facts_are_checkable_and_hold` (`tests/test_records.py`);
`test_extra_keys_in_expected_are_rejected` and `test_corpus_is_green` (`tests/test_evals.py`).
**Nothing can check that a value was not back-filled from a run** — that is what committing the file
first buys, and it is a review obligation rather than a mechanism.

## Consequences

Two disciplines survive the retirement — real data and its path stay out of git, and `expected.yaml`
is pre-registered — and both are above, with what enforces them. A lab path is not a project fact.

The benchmark corpus (`evals/benchmark`, real datasets behind byte-light fingerprint packages,
mostly but not only *C. elegans*) is the **validation** set we develop against. Its provenance is
per case, and each file's header says which: the first tranche was *seeded from a run*, and was
**reviewed against the publications on 2026-07-31** (issue #81) — `experiment.*` confirmed field by
field against the paper, the fragile values pruned — while later cases were pre-registered before
their run. **None of that makes it a test set.** When a case goes red we fix the compiler and grade
it again, which is precisely what a held-out set forbids; a reviewed expectation is a sounder
regression baseline, not a held-out measurement. A true held-out **test** set is a later milestone,
it will have to be a dataset nobody has compiled yet, and it is scoped — deliberately as scope, not
as a decision — in
[`docs/research/held-out-test-set-scope.md`](../research/held-out-test-set-scope.md).
