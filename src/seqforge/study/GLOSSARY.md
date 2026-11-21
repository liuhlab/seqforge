# Study

What the study chain reads: the research behind a set of archive records, its bounds, and the claims
a model writes down about it. This context names what a study file holds and the fixed set of
studies the work is built on.

Words every context shares — **Evidenced**, **Basis**, **Conflict** and the rest — are defined once
in the repo-root `GLOSSARY-MAP.md`.

## Language

**Study**:
One piece of research: the experiments one paper reports, or one project that has no paper yet. It is
whole GEO Series and is never split by sample.
_Avoid_: dataset, project, Series, deposit

**Bounds**:
What belongs to one study: its GEO Series and its papers.
A linked record that does not belong is left out with a reason.
_Avoid_: scope, extent, membership, boundary

**Standard set**:
The fixed list of 50 small GEO studies that work on reading studies is built and checked on. One
list, shared by every later stage.
_Avoid_: benchmark, test set, gold set, corpus

**Claim**:
One statement in a study file, with how it is backed (quoted, inferred or unknown) and its
probability.
_Avoid_: Assertion (harvest's span-verified claim read out of prose), fact

**Passage**:
One quote in a study file with where it was read, numbered once per file.
_Avoid_: Quote (harvest's), span, excerpt
