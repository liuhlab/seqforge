# 24. The compiled pipeline directory has one owner, and it sits above the composer

Date: 2026-08-03

## Status

Accepted.

## Context

`compose` writes `seqforge/pipeline/<recipe>-<run_id[:12]>/` and fills it with the wrapper a user
submits, the config that wrapper reads, the units table it iterates, and a copy of the hand-written
**Workflow module** carrying the rules ([0005](0005-run-id-is-the-pairing.md) decides the key; this
record decides who may spell the layout).

Five modules spelled that layout by hand: the composer, the report collector, the project index, the
compose gates and the ground-truth harness. Three of the names were the composer's *private*
constants, which is the arrangement that guarantees the other four re-type them — a name only its
writer owns is a name every reader re-invents.

Two of the five had gone further and grown **independently written implementations of the same
three-step derivation**: which **Workflow module** ran (invert the `.smk` the composer copied in),
which samples the run was contracted to produce (the config's own list, never a listing of the
results tree), and where its outputs went (the config's `outdir`, joined onto the run directory).
Two implementations of one derivation do not disagree until they do, and this repo has already paid
for that shape twice — a STAR command line rendered by hand in a module and in a test that could not
see each other, and two renderings of *"how do I get an index"*. Both times the copy nobody executed
was the broken one.

Meanwhile `workspace.py`, whose entire job is spelling state-directory names once, named
`records/`, `logs/`, `cache/`, `eval/`, `fingerprint/` and `report.html` — and referred to
`pipeline/` six times as a peer it did not name.

**The obvious reading is that this is a naming problem**, and a reader arriving from the six comments
will reach it: put the directory name in `workspace.py` beside the others and be done. That fixes one
string and leaves the derivation duplicated, which is the half that can be silently wrong.

## Decision

**One owner for the layout, split across two homes by what the two jobs are.**

| module | owns | does I/O |
| --- | --- | --- |
| `workspace.py` | `PIPELINE_DIRNAME`, `pipeline_dir(workspace, *parts, subdir=…)` — the **directory** | no |
| `pipeline.py` | the three filenames, and `CompiledPipeline` — what is **inside** one | yes |

`CompiledPipeline` answers five questions about a composed directory, and nothing else answers any of
them: `.module` (inverted out of the workflow registry, never a name-to-module table), `.config`,
`.samples`, `.results_dir`, `.sample_dir(sample)`. The composer **writes** through it; the report
collector, the compose gates and the ground-truth harness **read** through it.

`pipeline.py` is **top-level**, a peer of `workspace.py` and `project.py`.

## Why not names-only in the state-directory module

It is the smaller change and it is what the six comments ask for, so it is the one a reader reaches
first. It removes one hand-spelled string and leaves the two copies of the derivation exactly where
they were — and the derivation is the part that can be *wrong* rather than merely stale, because its
three steps encode judgements: that the module is read off the `.smk` and not off a name, and that
"which samples" comes from the artifact the run consumed and not from a listing of what finished. A
listing can say what landed; it can never say what is missing, so a partial run read that way is
indistinguishable from a complete one. Two copies of that reasoning is two chances to lose it, and
`workspace.py` cannot hold either copy without doing I/O — which would cost it the one property that
makes it safe to import from anywhere.

## Why not the reader inside the composer

It is where the names already lived, and the writer is the natural author of the format. But the
composer *writes* a pipeline directory and everyone else *reads* one, so a reader placed there points
the dependency arrow backwards: the report would import the compiler to learn where a file is, and
would pay the composer's import surface — the KB, the onlist registry, the workflow registry — for a
path join. The same argument the other way is what puts the reader *above* `compose/` rather than
beside it: `compose` may import a module that owns the layout it writes, and does; nothing about that
obliges a reader to import `compose`.

## So in code

**Never join `pipeline`, `Snakefile`, `config.yaml` or `units.tsv` into a path outside the two owners
— ask `CompiledPipeline` for a file inside a run directory and `workspace.pipeline_dir` for the
subtree.** That includes a private constant of your own: `_SNAKEFILE_NAME = "Snakefile"` is the exact
shape this record removed from the composer, and re-introducing it is how the fifth consumer became
five. Which **Workflow module** ran is inverted out of the registry, so a fourth module is answered
for on the day it is registered; a name-to-module table would simply be missing it, would answer
nothing, and nothing would fail to say so.

**Enforced by.** `test_only_the_compiled_pipeline_owner_spells_its_layout`
(`tests/test_repo_invariants.py`) walks the src tree and reads the guarded names off the owners
themselves, so a rename moves the guard with it;
`test_the_owner_answers_every_question_a_composed_directory_can_be_asked` and
`test_which_module_ran_is_inverted_out_of_the_registry_rather_than_matched_by_name`
(`tests/test_pipeline.py`) hold the reader against a directory the real composer wrote.

**The guard is narrowed to path positions, and knows it.** A literal counts when it is joined with
`/`, handed to a call, or assigned whole — not when it is merely equal to one of the names. `pipeline`
is also an ordinary word, and it is legitimately a tab id on the report page and a key in
`project.yaml`; neither is a path, neither would be improved by importing a directory name, and a
guard that demanded they change would be crying wolf on its second day. The known gap is therefore a
consumer that reaches the directory some other way — an f-string interpolating both halves, say.

## Consequences

- The composer's three private filename constants are gone, and its `configfile:` line is rendered
  from the same constant it writes the config under, so the wrapper cannot point at a filename
  nothing produces.
- The ground-truth harness lost its copy of the derivation rather than keeping it beside the new one.
  `kb e2e` now asks the owner which sample it was contracted to produce and where that sample's
  outputs are — the gate that proves the compiler's output runs must not be asking a *second*
  implementation where that output is.
- `CompiledPipeline` caches nothing; every property reads from disk at the call. The harness patches
  the config it has just read and then reads it back, and a value cached at open would hand it the
  file it had already replaced.
- Every reading property degrades rather than raises: an absent, unreadable or non-mapping config
  reads as an empty one and an unrecognised directory names no module. A half-composed workspace is
  a page a reader must still be able to open.
- **`project.yaml`'s `pipeline` key is untouched**, and is the one consumer this record does not
  move. It is an entry key in a published index, not a path; routing it through the directory name
  would mean a rename of the subtree silently renamed a key other tools read. The guard cannot see it
  either way, which is the honest position: it is not a spelling of the layout.
- The recurring suggestion — "put the reader beside the writer, it is the composer's format" — has a
  written answer now, which is the point of the file.
