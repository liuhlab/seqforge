# 29. A spec declares read sets, not a fixed read list

Date: 2026-08-04

## Status

Accepted.

## Context

`Spec.reads` is a fixed list, and role assignment is **injective and total** (`resolve/assign.py`):
`n_files < n_roles` is invalid, and a role forbidden on every file is unfillable. So a spec that
declares two reads cannot be satisfied by one FASTQ. There is no way to say *"this chemistry has a
paired-end and a single-end configuration"* in one entry.

On the shipped KB that costs a common case: a single-end bulk RNA-seq FASTQ gives
`Blocker(UNSUPPORTED_TECHNOLOGY)`, exit 3, because the generic bulk chemistry declares R1 and R2.
Single-end bulk RNA-seq is not exotic.

**The obvious reading is that the gate is `read_count`, and it is not.** `evaluate()` returns
`abstain / 0.0 / "not a per-cell test"` on a `ReadCount` test (`resolve/evaluators.py`) and nothing
else reads it — all 16 specs declare one and none is gated by it. Relaxing an inert test relaxes
nothing. The two-file demand comes entirely from *declaring two reads*, and this record exists so the
next reader does not spend the measurement again ([#234](https://github.com/liuhlab/seqforge/issues/234)
already spent it once).

**It surfaced from a paper, not from a hypothetical.** SMART-seq3's peer-reviewed Methods publish
three sequencing configurations verbatim — *"75-bp single end, 50-bp single end or 150-bp paired
end"* — so an entry written faithfully from the protocol refuses two of the three configurations that
protocol publishes ([#257](https://github.com/liuhlab/seqforge/issues/257)). The fix is argued here
against **bulk** rather than against a plate assay, because bulk has the same debt and the commoner
case, and because the generic paired-end fallback is where the blast radius actually lands.

## Decision

**A spec declares a maximal read set and may name subsets of it. Every read set is complete, so role
assignment stays injective and total.**

| | rule |
| --- | --- |
| **`reads`** | unchanged — the **maximal read set**, implicitly named `full` |
| **`read_sets`** | optional; each value is a **subset of declared read ids**, never a re-declaration |
| **its keys** | a closed vocabulary, extended deliberately — the same act as adding an `ElementType` |
| **the signature** | one per spec. A test whose `read` is absent from the active set is *inapplicable*; a `requires` test may address only reads present in **every** set |
| **the candidate** | one per spec — the best-scoring read set wins inside `build_tech_evaluation` and is recorded there |
| **the manifest** | records nothing new; its read layout already lists exactly the winning set's reads |
| **a tie** | prefers the **larger** set: it explains more of the data, and the order must be deterministic |

**A read set is a subset of ids, not a second declaration.** That is the whole of why this shape is
cheap: no read is written twice, so two configurations of one chemistry cannot drift in their element
coordinates. Drift is structurally impossible rather than merely discouraged.

**`length_feasible` becomes feasible-iff-*any* read set is.** It is not a nicety. `resolve/geometry.py`
claims a *proven* necessary condition — "a spec it rejects is one `build_tech_evaluation` would also
reject… narrowing the candidate set can never drop a spec that full scoring would have made a winner"
— and it computes `n_roles = len(spec.reads)`. Read sets falsify that claim, and `engine.py`'s
`pool = [...] or runnable` fallback hides the falsification by handing back the full pool whenever the
narrowed one comes up empty. A latent break behind a fallback is worse than a loud one; making the
predicate read-set-aware is what preserves the proof the rest of descent scoring stands on.

## Why not an optional read

`Read.optional: bool` reads like the smaller change and is the larger one. It makes assignment
**partial**: `valid(A)` must accept an unfilled role, `best_assignment` must compare maps of
different sizes, and `unfillable_roles` must stop meaning `MISSING_TECHNICAL_READ` for some roles and
not others. That is a rewrite of the core optimization to express what a complete-subset already
expresses with no change to it at all.

It also buys nothing in breadth. Bulk with an optional R2 accepts exactly the data bulk with an `se`
read set accepts — one cDNA-length FASTQ. The acceptance surface is identical; only the notation and
the blast radius differ.

## Why not a second leaf

A `bulk-rnaseq-se` entry is two entries for one published protocol, and it hits a vocabulary wall.
The two leaves accept each other's reads at rungs 0-2, so each must declare the other; role placement
`{R1}` != `{R1, R2}` makes their canonical backends non-identical, forcing `processing_divergent`,
which forbids `distinguishable_by: [none]`.

Be precise about where the wall actually is, because the first derivation of this argument put it in
the wrong place. Sibling leaves under a shared parent need **no** `confusable_with` edge —
`is_tree_kin` treats the tree as the declaration and `sibling_decided_by` reads the parent's
`children_decided_by`. So the pair would be declarable as siblings. The wall is one step further in:
`children_decided_by` draws from `Mechanism = [none|onlist|metadata|alignment|user]`, and **nothing in
it means "how many files there are"**. `Decidable` does carry a `reads` member, and `escalate` raises
Questions with it — but `Mechanism` cannot supply it, so the pair stays unlabellable either way.

Choosing one entry with two read sets dissolves the question rather than answering it: the choice is
*inside* one chemistry, so there is no pair to label and no mechanism to name.

## Why `read_count` is deleted rather than given meaning

A test in a closed signature vocabulary that abstains on every input is a knob that cannot fail.
Read sets make it doubly dead — a set's cardinality *is* its length, so even a working version would
restate the declaration it sits next to.

Deleting it also retires a collision: `read_count` means *role count* in the KB and *spot count* in
ENA (`io/remote.py`), in one codebase. The deletion is required to be a **provable no-op** — the whole
suite green with no expectation edits — which is the only honest way to cash the inertness claim, and
which would catch the one thing reading cannot settle: whether an abstaining `requires` test
contributes to score normalization.

## Why the guard now asks whether one spec could outrank another

The rung-0-2 under-declaration guard asks whether `a` produces a **valid** assignment on `b`'s data.
Validity was a sound proxy for danger only while every spec consumed every file. A read set is
precisely a spec that consumes fewer, so the proxy stops tracking: bulk's `se` set seats one role on
any 40+ bp cDNA read and orphans the rest, which is *valid* against nearly every leaf in the KB and
dangerous against none of them — it scores `0.9 - 0.25` against their `~0.9+`, because the leftover
penalty (`_LAMBDA / len(roles)` per orphaned file) is harsher the fewer roles a set has.

The guard's own docstring names the danger as *"the resolver would pick one and never ask"*, which is
an **ordering** claim. So the predicate becomes one. Keeping `accepts` instead would have forced a
`confusable_with` edge from bulk to almost every leaf — honest boilerplate that leaves the guard
unable to discriminate, which is a guard decaying into a formality.

The cost is real and is stated rather than buried: the predicate now depends on synthetic score
margins rather than on feasibility alone. **Re-deriving all five of bulk's existing edges under it is
a gate on the change**, not a footnote — a stronger guard that silently drops a true edge has traded
noise for blindness.

## So in code

**A chemistry with more than one sequencing configuration gets a read set, never a second spec and
never an optional read.** A read set names ids that `reads` already declares; if you find yourself
writing a read's coordinates twice, you have picked the wrong shape and the schema will not stop you
from being wrong slowly. A `requires` test may address only reads common to every set — put a
set-specific gate in `supports` or accept that the smaller set inherits it. And when you add a
predicate over `spec.reads`, decide explicitly whether it means *the maximal set* (canonicalization,
the benign-twin comparison) or *any set* (feasibility, recognition); the two are no longer the same
question, and nothing in the type system asks it for you.

**Enforced by.** The subset shape and the closed key vocabulary fail at load, where every other DSL
typo fails — pydantic `extra="forbid"` plus a `Literal`, executed by `load_spec` and `kb lint`. The
confusability half is held by `tests/test_kb.py`:
`test_the_benign_twin_biconditional_holds_over_every_loaded_spec_pair` (with
`test_the_biconditional_is_non_vacuous` and `test_a_divergent_pair_is_not_backend_identical` pinning
both directions, and `test_a_declared_twin_that_diverges_would_be_caught` proving the gate fires) and
`test_no_spec_pair_is_confusable_without_declaring_it`, which is the guard whose predicate this record
changes. `test_a_family_node_recognizes_its_children_and_no_one_else` holds recognition for the tree.

**Three gates do not exist yet, and are conditions of the change rather than follow-ups:** a
recognition case per read set (given only the `se` files, the spec is recognized *and* the `se` set
wins), a negative test for the `requires`-universality lint, and the re-derivation of bulk's five
edges under the new predicate. Until the first two land, R8's "executable and self-testing" holds for
the maximal set and not for the feature this record adds. The lint had **no instance in the shipped
KB** when this was written — after `read_count` went, bulk's `requires` was empty and no other spec
declared a read set — so it shipped as decoration until its negative test was written, which is the
standard `test_a_declared_twin_that_diverges_would_be_caught` already sets in this tree.

**Both of those have since changed, and this paragraph is left standing rather than deleted because
the argument is still the one that holds.** The negative test exists
(`test_a_requires_test_a_read_set_cannot_reach_fails_at_load_and_points_at_supports`), and
[0035](0035-the-mate-is-an-addition-to-umi-extraction.md) gave the rule its first shipped instance —
`smartseq3`'s `se` set, which *satisfies* it, since that entry's only `requires` test addresses R1 and
R1 is in every set. The recognition case per read set exists too, in both directions
(`test_the_plates_maximal_read_set_outranks_its_single_end_subset_on_a_paired_deposit` and
`test_the_generic_single_end_set_does_not_outrank_the_plates_on_a_single_end_plate_deposit`).

## Consequences

- **The KB version bumps, so every dataset gets a new `run_id`.** That is the standing cost of a
  spec-semantics change (`kb/__init__.py`), not a new one, and it is already paid by the
  `read_count` deletion alone.
- **The generic bulk chemistry is renamed to `bulk-rnaseq`** (version `illumina`): an id asserting `-pe` on an
  entry with a single-end read set is the class of claim this tree keeps deleting. This one costs more
  than the version bump — `dataset_hash` carries the chemistry, so stored bulk manifests are
  regenerated rather than recompiled. It is done now because the population of them is smaller today
  than it will ever be again, and because exactly one live line reads the id (`report/collect.py`, a
  display map that already renders it as "bulk RNA-seq" — the `-pe` was never user-facing).
- **`read_layout_kind: paired` becomes `mates`** — 1..2 biological mates chosen by ORDER, against
  `barcoded`/`atac_barcoded` which choose by ROLE. The kind is a property of the *module*, so
  `map/star` cannot be `paired` for one dataset and `single` for another; widening the one kind is the
  only shape that avoids a second hand-written module. Keeping the name `paired` for a kind that is
  single-ended half the time would reintroduce exactly the defect `read_layout_kind` was created to
  remove — a dispatch key that lies about what it selects.
- **`star.smk` changes, and it is hand-written.** Its `mate2` input becomes empty when absent and
  `readfilesin` renders only the mates present. R1 forbids *generating* rule source; editing a
  versioned module by hand is the supported path, and `compose`'s `_read_files_in` is what stops
  emitting a key the layout does not have.
- **The corpus bar is three gates, not one.** #231's baseline discharges #225's constraint 3 by digest
  equality, and `evals/benchmark/GSE283483-bulk` grades `library.chemistry`. So the rename **must**
  move the digest (by exactly one string, re-derived and shown) while the `read_count` deletion and
  the read-set work must **not** — which is why they are three tickets with three gates rather than
  one change with a written excuse.
- **A REFUSAL rested on the two-read declaration, and it was not the guard's** (#309, found on the
  networked benchmark after this landed). A deposit of one cDNA file with a single-cell chemistry
  asserted used to refuse `MISSING_TECHNICAL_READ` because no spec could seat two roles on one file;
  `bulk-rnaseq`'s `se` set seats one, so the deposit became explainable and the refusal became a
  question offering bulk. The orphan predicate above provably cannot catch it — with one file the
  fallback orphans nothing — so `escalate` grew a second, distinct condition on the same proper-subset
  scoping, and descent now keeps the ASSERTED chemistry in the scored pool: its evaluation is what
  answers *why* it could not be seated, and it used to be scored only by the accident of
  `pool = [...] or runnable` firing on an empty pool. **The generalisable lesson is the one this record
  already states about `spec.reads` predicates, one layer out**: a claim proved about the WINNER
  ("narrowing can never drop a spec full scoring would have made a winner") is not a claim about the
  REFUSAL, and read sets are what pull the two apart.
- **A known gap: `geometry_fingerprint` keys on `len(spec.reads)`** and stays maximal-only. It is
  documented as diagnostics-only and is not a correctness predicate, so an SE-capable spec grouping
  with a PE-only one is cosmetic — recorded here so the next reader does not mistake the asymmetry
  with `length_feasible` for an oversight.
- **What is deliberately *not* recorded.** The winning read set lands in the resolve artifacts, where
  "how this was decided" lives, and **not** on the manifest. Compose reads the reads and never the
  name, so a manifest field would be a claim that causes no behaviour and sits inside `dataset_hash`
  — the shape `Spec.decidable_by`, `RegistryEntry.fetchable` and `required_config` were each removed
  for.

The terms this record turns on are defined once in [`CONTEXT.md`](../../CONTEXT.md): a **Read set**,
the **Role assignment** that fills one, and the **Candidate** that carries which one won.
