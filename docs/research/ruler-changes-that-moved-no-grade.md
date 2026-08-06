# Two changes to the ruler, and what measuring them showed

Measured **2026-08-02** for [#184](https://github.com/liuhlab/seqforge/issues/184) /
[#188](https://github.com/liuhlab/seqforge/issues/188) and
[#196](https://github.com/liuhlab/seqforge/issues/196), against `main` and against `ac11b44`. Moved
out of `docs/agents/eval-corpus.md` on **2026-08-05**: the conclusions live there, the method here.

Both changes are to the **harness**, not to the compiler — cases where the eval was scoring a code
path the product had abandoned. Each landed on its own, before any compiler fix, so the next tier
pass measures one changed thing. Neither moved a grade for the reason it was made, and writing down
*why the movement that did happen is not attributable* is the whole point of the note.

**Method.** `seqforge eval run` over `evals/benchmark`, single trial, both flags where stated;
`--llm` runs on `deepseek-v4-pro` at the default fan-out. The attribution argument re-computes both
reductions offline over each run's **own** accepted assertions, so the comparison is not between two
draws.

**What it could not establish.** Nothing here is a measurement of the extractor: every `--llm` number
is one trial, and the same tier was independently shown to flip between `over_ask` and `correct`
across repeats of a single case. Read a per-case `--llm` grade below as a draw, never as a property.
Neither run reaches a corpus that is *heterogeneous* in geometry, which is precisely the population
the `#196` divergence would show up on — so "no case moved" is a fact about this corpus.

## One reduction for the hypothesis (#184/#188)

**The harness was not reducing prose the way the compiler does.** `manifest fill` takes a dataset's
chemistry claims agreement-or-nothing; the harness took the last document to claim one, off a
`by_field` dict. Two callers, one stage, different answers — so a dataset naming two chemistries
steered the harness's scorer and the compiler's not at all, and the grade was partly a claim about
the harness. There is one `resolve.chemistry_hypothesis` now and both call it.

**Measured after it, `deepseek-v4-pro`, `trials=1`, whole benchmark tier: 15/18, exit 3.** Against
the same command at `ac11b44` (also 15/18, exit 3):

| | `ac11b44` | after the ruler fix |
|---|---|---|
| `GSE234962` | `over_ask` | **`correct`** |
| `GSE229022` | `correct` | `over_ask` |
| `GSE317744` | `false_accept` (attribute drop) | `over_ask`, and chemistry **wrong** (`10x-3p-gex-v2`) |
| `GSE310378` | `false_accept` | `false_accept` |

**None of those moves is attributable to the change.** Recomputing both reductions over each run's own
accepted assertions: on the post-fix draw the old and the new return the **same hypothesis on all 18
cases** (no case produced two *distinct* chemistry values), so the fix was a measured no-op there. What
moved is which case `library.chemistry = "RNA-Seq"` landed on — one accepted claim on `GSE234962`
before, one each on `GSE229022` and `GSE317744` after. Mechanism 1 is real and its case list is a
per-draw lottery; **scope to the mechanism, never to a case list**, and do not read a single-trial
per-case grade as a property of the extractor.

`GSE317744` is the sharpest instance: byte-identical to `10x-3p-gex-v2` at every rung, so its recipe's
`10x 5'` is the only thing that can decide it. A single `"RNA-Seq"` claim displaced that hypothesis
and the run resolved the wrong chemistry — the metadata-decided channel losing to a `library_strategy`
suffix, live, on pro. That is #189's premise reproducing, not this change's doing.

The no-LLM tier is unmoved: 18/18, exit 0, 92 s.

## One resolver for the dataset, and the corpus's filenames had to become real (#196)

**The same shape one layer down, and larger.** `manifest fill` calls `resolve_runs` — group the files
into runs by name, resolve each run on its own bytes. The harness called `resolve_dataset`, which
answers "what is this ONE library?" and does one global role assignment: handed `PRJNA1027859`'s 18
files it seats one (R1, R2) pair and leaves sixteen with no role at all. **10 of the 18 frozen cases
are multi-run** — `GSE126954` alone is 56 runs — so on ten cases the benchmark graded a code path the
product had abandoned. It passed because those corpora are *homogeneous*: every run shares one
geometry, so one global assignment lands on a correct chemistry. That is a property of the corpus,
not of the code.

The fix promotes the reduction rather than teaching the harness to imitate it: `reduce_dataset` (see
[`resolve.md`](../agents/resolve.md)) is one function with two callers, exactly as
`chemistry_hypothesis` is.

**The corollary, and it is not cosmetic.** `materialize` named each generated file after the read it
carries — `R1.fastq.gz`, `cdna.fastq.gz` — which is a shape no deposit has and, worse, one that
groups into no run: two names sharing no stem are two single-file *runs*, and a barcode read with no
cDNA mate resolves to nothing. That was invisible while the harness scored a whole file list as one
library; the moment it resolves runs, **every generated case refuses `UNSUPPORTED_TECHNOLOGY`**. A
case built from one KB spec is one library, so its files are now deposited under one run,
`SIM_<mate>.fastq.gz`, where the mate token is the spec's own `file_hint` — the same token
`filename_prior` reads off a real submitter's file, so the sub-threshold nudge is unchanged and the
symmetric-role bulk case still seats R1 and R2 the way it always did. The label a case's
`expected.yaml` writes role assertions against (`R1`, `cdna`) is unchanged; only the filename moved.

**Measured, no-LLM tier, before and after: 18/18 both times, and *zero* cases moved** — every field's
graded `actual` byte-identical, `field_accuracy 1.0`, `false_accept_rate 0.0`, one question asked
(`GSE126954`). Wall clock 89.9 s → 92.6 s: resolving 56 runs against the KB is more total work than
resolving one 175-file pool once. A no-op on this corpus was the prediction — homogeneity is exactly
why the divergence was invisible — and the fix is justified by the divergence being real, not by the
number moving.
