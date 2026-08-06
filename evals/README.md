# The eval corpus

```bash
seqforge eval list                       # what is in the corpus
seqforge eval plan --cases evals/benchmark   # what an --llm pass would cost; spends nothing
seqforge eval run                        # deterministic cases only — no API key, no network
seqforge eval run --llm --trials 3       # include the prose cases (costs tokens)
seqforge eval run --llm --model deepseek-v4-flash  # the other V4; the default is -v4-pro
seqforge eval run --llm --provider anthropic --model claude-opus-4-8
seqforge eval run --case chemistry-unstated-trap --llm
seqforge eval run --llm --ceiling 2000000  # raise the per-case token ceiling; 0 removes it
seqforge eval run --no-llm -C out && seqforge eval report out/seqforge/eval
```

**`eval plan` is the dry run for a whole tier.** `harvest extract --dry-run` prices one dataset; the
decision it informs — *is this `--llm` pass worth its money* — is taken over a corpus, and used to be
answerable only by making one. It reaches no model and needs no credential, though it does
materialize each case, because a fingerprint package carries its prose inside itself and those
characters cannot be counted without unpacking it. Every token it reports is an **input** token:
output is not estimable, since the model decides how many claims a document supports, and that half
is what `--ceiling` bounds.

**`--ceiling` ships on, at 500,000 raw tokens per case**, and it refuses rather than warns: a case
that reaches it is reported with a `TOKEN_CEILING_EXCEEDED` Blocker instead of a grade, and the run
exits 3. Raw means everything counts — fresh input, cached input, cache writes and output — because
a ceiling is a backstop and not a price. Measured 2026-07-31, the largest benchmark case other than
GSE126954 spent 122 K, so the default clears the corpus by 4x and is a number nothing normally
touches. It costs nothing under `--no-llm`, which spends no tokens at all.

**`-C` gives the run a directory**, under `seqforge/eval/`: `report.json` — byte-identical to what the
command printed — and `transcripts/<case>.jsonl` for every case that reached a model. That is where a
transcript lives, because stdout *is* the result object and a thousand-exchange transcript cannot ride on
it; the report gains the paths, never the contents. `seqforge eval report` is handed the directory.

`eval run` emits machine JSON on stdout and nothing else (ADR-0013), so the human-readable page is a
*consumer* of that stream rather than a second output mode: `seqforge eval report` writes one
self-contained HTML file (every asset inlined, no network) that names the false accepts instead of
averaging them into a rate. `benchmark.yml` uploads it as the job's artifact.

**The page shows the claims, the refusals, and a sample of the transcript.** Each graded assertion
is rendered with the quote it rests on and the span code computed for it — `library.chemistry =
"RNA-Seq"` is not a finding, *from this quote, in this document, at these offsets* is — and the drafts
the tripwire threw out are a readable list rather than an integer.

```bash
seqforge eval report out/seqforge/eval                        # a representative sample (default)
seqforge eval report out/seqforge/eval --transcript all       # every exchange; a large page
seqforge eval report out/seqforge/eval --transcript none      # grades only
```

The system prompt is rendered **once** per report: it is byte-identical across every request in a run,
which is what makes prefix caching work, so a transcript is one prompt plus N (document, response)
pairs. The default `sample` keeps one exchange per document scope plus every exchange that produced a
refused draft or a graded claim, and the page says how many it left out — a silently truncated
transcript reads as a complete one.

## Why this exists

Every other stage of the compiler can be pinned by a unit test: same bytes in, same artifact out. Two
things here cannot, and both matter.

1. **The LLM stage is nondeterministic.** The same document has produced different quotes across runs
   — both correct, both span-verified. There is no output to snapshot, only a rate to measure.
2. **Prompt and KB edits are silent.** Add a KB alias, reword an instruction, and extraction behavior
   changes without a single test going red. The brief is explicit: *treat prompt and KB changes as
   code changes.*

**So every number here is scoped to an extractor**, and the report says which one:
`extractor: {provider, model, prompt_version}`. `--llm` takes the provider's own default —
`deepseek-v4-pro`, and this corpus is what settled that (see the bottom of this file) — and
`--model deepseek-v4-flash` is the other V4 DeepSeek serves. The same prompt on a different model is
a **different extractor** (ADR-0009), so a run's numbers may only be compared against a baseline that
names the same one.

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

## Two tiers

The corpus lives in two directories with different jobs. Both run the same real compiler; they differ
in what they hold fixed.

| | `cases/` | `benchmark/` |
|---|---|---|
| **pins** | the *machinery* — resolution, the confusion matrix, conflicts, harvest traps | the compiler on *real* datasets |
| **inputs** | recipes (bytes generated on the fly) — synthetic + adversarial | fingerprint packages pulled from HF |
| **runs** | every commit, hermetic (`test_corpus_is_green`) — no network, no key | on release / manual (`benchmark.yml`), networked |

Keeping them disjoint is load-bearing: `discover_cases()` over `cases/` never reaches `benchmark/`, so
a package pull can never sneak into per-commit CI. The `benchmark/` tier is documented at the bottom.

### How `cases/` is organized

A case is a directory holding an `expected.yaml`; the directory above it is a **purpose group**
(organisation only — a case's id is still its own leaf-directory name, and discovery finds a case by
its `expected.yaml` at any depth, so groups never change a case's identity):

| group | what it pins |
|---|---|
| `spec/` | one dataset per KB **leaf chemistry**; bytes alone must decide (the coverage tier) |
| `prose/` | the **harvest / LLM** path — extract a stated fact, stay silent on an unstated one |
| `steering/` | a metadata **hypothesis meets the bytes** — overridden (→ decide) or surfaced as a conflict (→ ask) |
| `refusal/` | negatives that must **block**, with the right blocker code |
| `grouping/` | which files are **one run**, and so one sample, from names alone — the record-less deposit shapes (ADR-0027) |
| `real/` | a **real local dataset**, resolved from an env var (data out of git) — the pilot's pre-registration |

`test_the_corpus_is_well_formed` enforces this, so a stray top-level case or an ad-hoc seventh group
turns red rather than quietly re-messing the directory. The same test asks the corpus's three other
layout questions off the same walk: every outcome class is covered, every case has a description, and
no case ships FASTQ bytes.

## Adding a case

```
evals/cases/<group>/<case_id>/       # <group> = spec | prose | steering | refusal | real
  inputs/recipe.yaml   # HOW to build the FASTQ — never the FASTQ itself
  metadata/*.txt       # prose for the LLM stage (optional)
  records.json         # archive transcript (optional; real cases — sample facts come from here)
  expected.yaml        # ground truth, or the expected refusal/question
```

**Inputs are recipes, not bytes.** A recipe is a few hundred bytes, deterministic in `(spec, seed)`,
and regenerates byte-identically on any machine using the same generator the KB round-trip uses. So a
case is diffable, a KB spec edit *moves its inputs with it*, and no FASTQ ever enters git history.

```yaml
# inputs/recipe.yaml
generate:
  kind: spec              # spec | random | local | fingerprint
  spec: 10x-3p-gex-v3
  n: 3000
  seed: 0
  onlists: synthetic      # synthetic (rung 3 reachable) | none (structure only, rung <=2)
  reads: [R2]             # optional: which reads were DEPOSITED; default (empty) is all of them
  truncate: {file: R1, fraction: 0.6}   # optional: the TRUNCATED_GZIP negative
  over_length: {read: R1, extra: 2}     # optional: sequence a read past its declared cycles
  deposit: {libraries: 2, lanes: 2}     # optional: N libraries x M lanes; default 1x1 is byte-identical
hypothesis: 10x-3p-gex-v2 # optional: a metadata claim WITHOUT an LLM, so conflict cases run in CI
```

`reads:` says a submitted read set is not always the chemistry's read set — SRA drops the technical
read unless a dump asks for it, and a run submitted as a Cell Ranger BAM never had one in the
archive's read space at all. It withholds a **file**, never a molecule: the reads it keeps are
byte-identical to the ones a full deposit writes, because the generator draws them all from one seeded
stream and the filter runs after it. Pair it with `hypothesis:` and the deposit is a declared
single-cell library with no barcode read — `refusal/barcode-read-never-deposited`.

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
  options: [10x-3p-gex-v2, 10x-5p-gex-v2]       # what `positions` is when the exit 4 is a QUESTION
assertions:                             # harvest ground truth (only checked under --llm)
  - {field: experiment.organism, value: Caenorhabditis elegans}
forbidden_fields:                       # fields the prose does NOT state — silence is correct
  - experiment.samples.tissue
```

`forbidden_fields` is not an afterthought. Rewarding recall alone trains the prompt to guess; these
are the cases where the right answer is to say nothing.

**An `outcome: ask` arrives in two shapes, and the `conflict` block pins whichever one it is.** A
Conflict is two positions that disagree, so `positions` is the assertion. A Question is a tie the
bytes cannot break, so it has no positions at all — what it has is the answer set a human is being
offered, and `options` is the assertion. Give one or the other; naming only `field` would pass on any
question about that field, which leaves the "and why" unasserted. Asserting `options` where the
resolver raised a *conflict* fails rather than passing on the shared field name.

## Cases backed by real data

A case over real data uses `kind: local` with `root_env`, so its ground truth is committed while its
bytes stay at a path this repo does not contain — real FASTQs are far too large for git, and their
location is a lab fact rather than a project fact. If the root is unset or absent the case **skips**:
never a pass, never a fail.

Pre-register `expected.yaml` from declared metadata only, **before** any run. That discipline is
independent of whether a case is reserved: it is what separates "we predicted this" from "we wrote
down what happened", and only the first can be wrong.

## Conventions

- **A case that cannot run must skip, never pass.** Skipped cases are excluded from every rate.
- **Write down why.** `description` is required and should say what breaking the case would mean.
- **Prefer the case that hurts.** `chemistry-unstated-trap` exists because the bytes really are v3 and
  the prose really does describe that experiment without naming it — so a model answering "v3" is
  correct about the world and wrong at its job. Cases that can only pass are decoration.

## What the corpus has caught

Each of these is a dated finding whose detail lives in the case's own row and `expected.yaml` header.
What generalizes past the one case is here, because that is the part the next case can be written from.

- **A head slice is not a random sample — it is the flow cell's first tiles, which is exactly where a
  dark cycle lives** (2026-08-01, `GSE305031`). The fix (#177) was to the onlist hit rate's
  *denominator*, not to the read budget: an unpackable window was measuring how many cycles the
  sequencer called rather than which whitelist the library came from. A dark cycle now costs
  **coverage** and leaves the **rate** alone. `RESOLVE_VERSION` bumped and `PROBE_VERSION` did not,
  because what was defective is a cached *refusal* rather than the bytes behind it.
- **An absence is only tested by a case that reaches the leaf without it**, which is why one
  chemistry needed two cases where another needed one. A separation you can *measure* is honest
  coverage from a single real case — BD Rhapsody Enhanced's three cell-label pools hit 0.930 / 0.974 /
  0.961 against 0.001 for the disjoint 97×3 (2026-07-31). `10x-5p-gex-v2`'s separation from
  `10x-3p-gex-v2` is a documented *absence*: they share a whitelist, so one case would either carry a
  hypothesis and never test the whitelist, or omit the hypothesis and never reach v2 at all.
- **An honest question settles at `ask`; a manufactured one stays at `decide`.** `GSE126954` stops on
  the knowledge base's one declared read-undecidable pair — the entry says so, and neither declared
  mechanism is reachable — so `ask` is what it is owed, and its seven field claims still grade.
  `GSE234962` stops on a `library_strategy` string re-read out of the record that typed it; moving
  *that* one to `ask` would enshrine a defect as the specification (#184).
- **One archive deposit read at two of its levels is one source** (#182). `GSE282765-colon-crod-wta`
  graded `false_accept` under `--llm` and `correct` without it, for a *resolver* reason rather than a
  hallucination: a quote of an experiment title re-derived `experiment.samples.treatment` from the
  same submission that had typed it into a BioSample slot, both positions arrived as `asserted`, and
  equal authorities that disagree leave the attribute **null**. A prose reading wholly inside the typed
  value is now absorbed rather than tied against. Containment holds in one direction and over whole
  words, so a reading that *extends* the typed value (`control` read as `control RNAi`) is still a
  disagreement and still leaves null.

## The `benchmark/` tier (real data on HF)

Real datasets (mostly *C. elegans*), run from their byte-light **fingerprint packages** on the HF repo
[`liuhlab/seqforge-benchmark`](https://huggingface.co/datasets/liuhlab/seqforge-benchmark). Each
`benchmark/<accession>/` case is a `kind: fingerprint` recipe (`hf: packages/<accession>.fingerprint.tar.gz`)
plus a committed `records.json`, so chemistry grades from the pinned bytes and sample facts from the
archive transcript — anonymous read, no token, no NCBI key. A package that is unreachable **skips**; a
package the repo does not hold (a 404 — it was never published) is reported as **absent**, a gap in
the corpus rather than a network blip. Both are excluded from every rate. Publish one with
`seqforge io publish-package <package>` (`--dry-run` first: it prints the URL the `hf:` key below must
match). A case that a real dataset makes red is published anyway — ADR-0016.

```yaml
# benchmark/<accession>/inputs/recipe.yaml
generate:
  kind: fingerprint
  hf: packages/GSE274290.fingerprint.tar.gz   # or `path:` (committed) / `root_env:` (staged out of git)
```

A benchmark case may also declare a `hypothesis:` beside `generate:`, exactly as a `steering/` case
does. It stands in for a chemistry claim the archive states in prose, so the case exercises the
metadata-conditioned branches without an API key — `GSE208154` needs one to reach
`MISSING_TECHNICAL_READ` at all: its lone 91 bp cDNA read is honestly bulk without it, so
`bulk-rnaseq`'s single-end read set explains the deposit and the run *decides* at exit 0 rather than
refusing (before read sets it degraded to the generic `UNSUPPORTED_TECHNOLOGY` instead — either way,
the assertion is what names the actual defect). Use it only where the record states the chemistry
verbatim, and quote that sentence in the recipe comment.

**Provenance is per case, and the file's own header says which.** The first tranche was *seeded from a
run* and was **reviewed against the publications** on 2026-07-31 (`# REVIEWED <date>`, issue #81): its
`experiment.*` values were confirmed field by field against the paper and the fragile ones pruned,
while `library.chemistry` stays a byte-resolved regression baseline. Later cases were **pre-registered
before their run**. A file carrying `AUTO-SEEDED … PENDING MAINTAINER REVIEW` has had neither — none
does today, and the header is the authority if one appears again.

**What "fragile" means, since the transcript is committed.** A pruned value was never a grading risk:
`records.json` is frozen beside the case, so no re-submission can move it. It was pruned because it
asserts nothing *about the experiment* — a verbatim duplicate of another field, a `none` / `n/a` form
filler, a sentence typed into a `tissue` slot, or a submitting-account label. The test is whether the
science would have to change for the value to change; if not, the claim is decoration, and a corpus
of decoration is what makes a green benchmark meaningless.

What each dataset is, and the one thing it covers that nothing else does, is a row in
[`benchmark-datasets.tsv`](benchmark-datasets.tsv) — read it before adding a dataset, because the
question is not "is this a nice dataset" but "what does the corpus not yet cover".

### The `--llm` pass on this tier grades harvest too, and that is a decision (2026-08-01)

**It was not always. Until this date the tier's `--llm` pass ran the extraction stage on all eighteen
cases and measured only whether it crashed** — no case declared `assertions:`, so nothing the model
found was graded, and nothing it invented could fail. The alternative on the table was to keep it that
way and call it a smoke test, leaving `cases/prose` the only place harvest is measured. Two
measurements decided it the other way, and both are worth keeping because either one moving should
reopen the question.

**The cost that argued for a smoke test is gone.** `seqforge eval plan --cases evals/benchmark`, run
on 2026-08-01: **141 documents — so 141 requests before retries — and ~517 K estimated input tokens**
across all eighteen cases, the largest single case being `PRJNA1195922` at 25 documents / ~83 K. The
same pass used to be **1,100 calls and 3.70 M input tokens** over thirteen graded cases, most of it one
dataset asking the nine-attribute sample vocabulary once per *run*. Collapsing a sample's runs into one
document and narrowing the committed transcripts removed it. A tier pass is now roughly a tenth of what
it was, so "too expensive to be worth grading" is no longer a true sentence about it.

**The prose is real, but it is not everywhere.** Only **7 of the 18** packages carry an `info/text`
document at all — `GSE126954`, `GSE234962`, `GSE256266`, `GSE274290`, `PRJNA1027859`, `PRJNA1195922`,
`PRJNA658829`. The other eleven have nothing but their archive records, and `GSE282765-colon-crod-wta`
was built with no `--doc` at all because its series has no linked publication. So this tier can never
be *the* harvest corpus; it can hold real, checkable claims where a real document makes them, and
`cases/prose` keeps owning the adversarial ones a synthetic document can be built to contain.

Two cases carry that ground truth today, and the pattern to copy is in their files:

| case | assertion | forbidden, and why silence is right |
|---|---|---|
| `GSE274290` | `experiment.organism` | the paper describes RNAi-treated animals — for its *western blots*, not for the library that was sequenced |
| `PRJNA658829` | `experiment.organism` | every experiment record is 2 KB of routine husbandry, and `treatment` is asked of it |

Both also forbid `experiment.samples.disease`, which each document baits and neither states about a
worm. **`forbidden_fields` is the half that earns this.** A graded assertion only rewards recall, and
rewarding recall alone trains the prompt to guess; the claim these cases exist to catch is a *real
quote attached to the wrong sample*, which span verification passes by construction and no byte can
ever contradict.

Three rules for adding more, learned from writing these two:

- **Check the assertion against the package, not the paper you read online.** `info/text` is
  extracted text, sometimes lightly mangled, and it is the only thing a run can see. Unpack the
  package and grep.
- **Prefer a field only one document can answer.** `experiment.organism` is asked of a
  *dataset*-scoped document and of nothing else, so the carried paper is its sole possible source. A
  sample attribute can be claimed by several documents at once, and the grade keeps one of them.
- **A forbidden field must be absent from every document**, the archive records included — not just
  from the paper. The grade looks at the whole accepted set.

None of this changes what `--no-llm` does: chemistry still comes from the pinned bytes and sample
facts from `records.json`, with no key and nothing graded about harvest.

**Read this before you spend, because the plan is not the bill on the cheap model.** The first graded
tier pass (2026-08-01, `deepseek-v4-flash`, default fan-out) issued **68** of the planned 141 requests
— 257,592 input, 203,079 output, 161,920 cache-read tokens, 326 s wall — because five of the seven
prose-carrying cases aborted on DeepSeek's known empty-`json_object` failure and **skipped**. A skip
is excluded from every rate, so no number is poisoned, but the harvest half was only sometimes
measured and nothing said which times. That pass is why `deepseek-v4-pro` is now the default (below);
what the report buys is knowing you needed it.

### A stage that did not run is not a stage that found nothing (2026-08-01, #182)

That pass reported a clean `harvest.matched` for the two cases that survived and **said nothing at
all** about the five that did not. It is the same "could not check" versus "checked and found
nothing" split this file already draws between an `unavailable` package and an `absent` one, one
level in — and it was decided the same way. Three things changed, and no exit code did.

**A per-document abort no longer takes the case down with it.** One document raising through the
whole case is what made the tier's harvest half all-or-nothing, and it is why `--trials N` is the
*wrong* instrument here: all N trials skip together and measure nothing, so three single-trial runs
are strictly better. Under `--llm` the documents that answered are graded and the ones that did not
are named, with the provider's own message. (`harvest extract` is unchanged and still fails closed:
a manifest silently short a fact cannot be told from a complete one.)

**A case whose model failed still grades its byte half.** It used to skip entirely, so a `--llm`
pass graded thirteen cases where `--no-llm` graded eighteen — two runs that could not be diffed at
all. Nothing about the byte half needs a model.

**Every harvest grade carries a `status`**, and the report a tier-wide `harvest` block:

```jsonc
"harvest": {
  "cases_complete": 0, "cases_partial": 1, "cases_unmeasured": 0,
  "documents_planned": 14, "documents_extracted": 12, "documents_failed": 2,
  "assertions_unchecked": 1          // excluded from field_accuracy, which is why it is reported
}
```

`documents_planned` is this run's own plan, so the plan-versus-issued gap needs no second command;
compare it against `eval plan`'s `n_documents` to also catch the cases that never planned at all
(an unreachable package). `documents_extracted` against `cost.llm_calls` is what retries cost.

A graded assertion a failed document would have been asked is reported **`unchecked`**, never
`missing`. The rule is asymmetric on purpose: `missing` claims the model read everything and did not
say it, so one unread document unsettles it, while `matched` needs a single document and nothing
unsettles it. Measured on `GSE234962`, whose paper aborts while its supplementary table answers —
both dataset-scoped — the symmetric rule reported the binomial that paper writes fifteen times as a
claim the model had failed to make.

**A skip still poisons no rate, and this is still not a failure.** Exit 3 says the compiler produced
a wrong answer and exit 4 says a human is owed one; a stage the provider did not answer is neither,
and a red tier on DeepSeek's uptime would be a worse instrument than a green one. What it gets is a
number, a stderr line, and a tile on the page.

### The default model, and the run that decided it (2026-08-02, #188)

**This section is the measurement's home.** Everywhere else that says pro is the default — ADR-0009,
`harvest/providers.py`, `cli/eval.py`, the harvest skill — carries the claim and points here, so there
is one place to re-date when the next re-baseline lands.

`deepseek-v4-flash` was the default (#167) on a *cost* argument: ≈3× cheaper per token across 10⁴
datasets, and safe to pull because it cannot move correctness (span verification re-greps every quote
whichever model proposed it, so a weak model costs coverage and never correctness). It lost in its own
currency. Three runs of this corpus at `ac11b44`, same prompt `2026.7.4`, `trials=1`, differing only
in extractor — the table is #188's:

| | no-LLM | `deepseek-v4-flash` | `deepseek-v4-pro` |
|---|---:|---:|---:|
| cases correct | **18 / 18** | 12 / 18 | 15 / 18 |
| field accuracy | 1.000 | 0.955 | **0.982** |
| wall clock | 89.8 s | 369.3 s | **140.5 s** |
| output tokens | — | 328,857 | **93,716** |
| documents failed | — | 6 / 141 | **1 / 141** |

Pro is ~2.6× faster on ~3.5× the output-token efficiency, on the same input. Flash's extra output is
largely `field_not_permitted_for_doc` rejections — claims the prompt never asked of that document,
which `verify_drafts` discards. **The default is `deepseek-v4-pro`.**

**Read the pro column as one draw, not as the extractor's baseline.** A second pro run of the same
18 cases at the same commit graded the same 15/18 but reported **0/141 documents failed** and 100,328
output tokens. Single-trial LLM numbers are claims about one sample (`stability` is unmeasured at
`trials=1`); what survives across both draws is the *shape* — pro faster, pro cheaper in output, pro
failing at most one document where flash failed six. The comparison is directional and it is enough
to settle a default; it is not a baseline any later run may be diffed against.

A silent fallback to another model after N failures is still rejected: the same prompt on a different
model is a different extractor (ADR-0009), so a run that switched mid-pass could not name its own, and
`extractor` is the field that makes a baseline comparable at all. `--model` stays a decision a reader
takes *from the report*, which is also why the coverage line names the model that ran and prescribes
none.

One finding from that pass was not about harvest at all — a partial quote of a record's own attribute
colliding with the attribute itself, which is the metadata resolver's business. It is the last entry
under *What the corpus has caught* above.

### The regression protocol, and the run it prescribes (2026-08-04, #296)

Written here rather than in the pull request that ran it, because it is a procedure the next person
changing the shipped path has to repeat.

**Who runs what.** The hermetic tier is CI, on every commit. **The networked tier is
maintainer-launched, single-trial, with `--llm` excluded** — six single-trial `--llm` runs of one case
graded two `correct`, two `over_ask` and two aborted, so a single-trial harvest grade is a coin flip
and a digest taken over one would be an instrument nobody trusted twice. `--no-llm` reaches no model,
needs no credential, and grades chemistry from pinned bytes and sample facts from committed records.

**It is two commands, not one**: a run over exactly the frozen eighteen, which the grade digest is
taken from, and a run over the whole tier, which is what a newly-added case turns green. Land each
change separately with its own before/after digest pair. **Quote a hex with the tree it was taken on**,
re-take it on the tree you are about to change, and diff it against that same tree — a hex carried over
from a neighbouring tree has been misattributed once already. `src/seqforge/evals/digest.py` owns the
baselines and what moved them, and it *refuses* rather than filters: the recipe hashes the whole
per-case list as well as the count, so equal-digest and add-a-case are incompatible instruments.

**Pre-declare the moves, and assert the strings before the run rather than after.** #296's shape is
the one to copy: two lines running the chemistry matcher over the dataset's own declared protocol
string, passing **first**, so a red afterwards is about the work and nothing else. A pre-declaration
that turns out unnecessary is the instrument working; one invented afterwards is not. Every `run_id`
moves whenever the KB or workflow version bumps, so such a run is **cold** and no cache carries an
answer into it.

**Budget the clock.** The eighteen finish in about 91 s. A plate case roughly doubles the tier's wall
clock on its own, because 96 cells are 96 resolves against the whole knowledge base — the tier's most
expensive case by an order of magnitude, and the only place the sample explosion is measured on real
bytes.

**Change the ruler on its own, before any compiler fix.** Two such changes (#184/#188, #196) moved no
grade for the reason they were made, and #307's support-normalizer change was *predicted* to move the
digest and did not. The runs, and the argument for why the movement that did happen is not attributable
to them, are in [`ruler-changes-that-moved-no-grade.md`](../docs/research/ruler-changes-that-moved-no-grade.md)
and [`support-normalizer-asymmetry.md`](../docs/research/support-normalizer-asymmetry.md). An unchanged
digest across a deliberate semantic change is informative only because both ends were taken on one tree.

**The `--llm` blind spot is structural and this protocol does not close it.** No routine gate observes
the harvest path: the hermetic tier excludes every case that harvests, and a `--no-llm` digest never
calls harvest. Closing it in general is separate work.

None of those provenances makes this a *test* set: when a case goes red we fix the compiler and grade
it again, which is exactly what a held-out set forbids. Run it with `seqforge eval run --no-llm --cases
evals/benchmark`; it fires in CI only on a published release or manual dispatch
(`.github/workflows/benchmark.yml`), never per-commit. A true held-out **test** set is a later
milestone, scoped — and not decided — in
[`held-out-test-set-scope.md`](../docs/research/held-out-test-set-scope.md).
