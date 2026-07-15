# The eval corpus

```bash
seqforge eval list                       # what is in the corpus
seqforge eval run                        # deterministic cases only — no API key, no network
seqforge eval run --llm --trials 3       # include the prose cases (costs tokens)
seqforge eval run --llm --provider anthropic --model claude-opus-4-8
seqforge eval run --case chemistry-unstated-trap --llm
```

## Why this exists

Every other stage of the compiler can be pinned by a unit test: same bytes in, same artifact out. Two
things here cannot, and both matter.

1. **The LLM stage is nondeterministic.** The same document has produced different quotes across runs
   — both correct, both span-verified. There is no output to snapshot, only a rate to measure.
2. **Prompt and KB edits are silent.** Add a KB alias, reword an instruction, and extraction behavior
   changes without a single test going red. The brief is explicit: *treat prompt and KB changes as
   code changes.*

## The metric that matters

Not all failures cost the same, so grading is a 3x3 confusion rather than a pass/fail bit:

| grade | meaning | cost |
|---|---|---|
| `false_accept` | **decided wrong, or decided at all when it should have stopped** | **a human never looks again; the corpus is silently poisoned** |
| `false_refuse` | blocked on something it should have decided or asked | throughput — a human looks and unblocks it |
| `over_ask` | asked what code could settle | a question that did not need asking |
| `mis_triage` | refused when it should have asked, or vice versa | stopped, but sends the human the wrong way |
| `wrong_reason` | right outcome, wrong BlockerCode / conflict | the refusal's *meaning* has rotted |

A refusal costs attention. A false accept costs the corpus. `eval run` exits 3 on **any** false
accept — it is not on a `--fail-under` slider, because no threshold makes one tolerable.

## Adding a case

```
evals/cases/<case_id>/
  inputs/recipe.yaml   # HOW to build the FASTQ — never the FASTQ itself
  metadata/*.txt       # prose for the LLM stage (optional)
  expected.yaml        # ground truth, or the expected refusal/question
```

**Inputs are recipes, not bytes.** A recipe is a few hundred bytes, deterministic in `(spec, seed)`,
and regenerates byte-identically on any machine using the same generator the KB round-trip uses. So a
case is diffable, a KB spec edit *moves its inputs with it*, and no FASTQ ever enters git history.

```yaml
# inputs/recipe.yaml
generate:
  kind: spec              # spec | random | local
  spec: 10x-3p-gex-v3
  n: 3000
  seed: 0
  onlists: synthetic      # synthetic (rung 3 reachable) | none (structure only, rung <=2)
  truncate: {file: R1, fraction: 0.6}   # optional: the TRUNCATED_GZIP negative
hypothesis: 10x-3p-gex-v2 # optional: a metadata claim WITHOUT an LLM, so conflict cases run in CI
```

```yaml
# expected.yaml
outcome: decide           # decide | refuse | ask
description: >-           # required: a case whose intent is unwritten cannot be maintained
  Why this case exists and what breaking it would mean.
fields:
  library.chemistry: 10x-3p-gex-v3
  library.roles.R1: R1    # role assignment, by recipe read id — the resolver never sees filenames
blockers: [TRUNCATED_GZIP]              # outcome: refuse
conflict:                               # outcome: ask
  field: library.read_layout.R1.length
  positions: {asserted: "26", observed: "28"}   # the load-bearing part, not the field name
assertions:                             # harvest ground truth (only checked under --llm)
  - {field: experiment.organism, value: Caenorhabditis elegans}
forbidden_fields:                       # fields the prose does NOT state — silence is correct
  - experiment.samples.tissue
```

`forbidden_fields` is not an afterthought. Rewarding recall alone trains the prompt to guess; these
are the cases where the right answer is to say nothing.

## Held-out cases

A held-out case uses `kind: local` with `root_env`, so its ground truth is committed while its bytes
stay at a path this repo does not contain. If the root is unset or absent the case **skips** — never a
pass, never a fail. Pre-register `expected.yaml` from declared metadata only, **before** any run.

## Conventions

- **A case that cannot run must skip, never pass.** Skipped cases are excluded from every rate.
- **Write down why.** `description` is required and should say what breaking the case would mean.
- **Prefer the case that hurts.** `chemistry-unstated-trap` exists because the bytes really are v3 and
  the prose really does describe that experiment without naming it — so a model answering "v3" is
  correct about the world and wrong at its job. Cases that can only pass are decoration.
