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

## Consequences

Two disciplines survive the retirement, and both are enforced:

- **Real data, and its path, stay out of git.** A `kind: local` eval case names an environment
  variable, guarded by `test_skill_never_leaks_a_lab_path`. A lab path is not a project fact.
- **Pre-register `expected.yaml` before a run**, with claims that are *checkable*:
  `experiment.samples.*.<attr>` for all samples and `experiment.samples.<accession>.<attr>` for one.
  You want both — `*` alone passes on a shuffled join.

The benchmark corpus (`evals/benchmark`, seven real *C. elegans* datasets behind byte-light
fingerprint packages) is the **validation** set we develop against: its expectations were *seeded
from a run* and are marked pending maintainer review, which makes them a regression baseline and
explicitly **not** a pre-registration. A true held-out **test** set is a later milestone, and it
will have to be a dataset nobody has compiled yet.
