# CLI conventions: the contract every verb keeps

**Covers.** `src/seqforge/cli/`.

Read this when you add or change a `seqforge` verb. R6 in detail — the CLI *is* the API, and a skill
is a thin client over it.

**This page enumerates no verbs, on purpose.** The live Typer app is the only list, and
`test_skill_documents_only_real_cli_verbs` (`tests/test_skills.py`) introspects it to check every
markdown surface in the repo — skills, docs and the README — for a verb or a long flag that does not
exist. A hand-written verb table would rot in the one direction that matters, toward *permitting*
fiction, and it has: `seqforge probe` was documented everywhere before it was registered, and an io
skill documented two onlist subcommands the app never had. Run `--help`, or read
[`cli/`](../../src/seqforge/cli/). What follows is only what a test cannot derive.

## The stream split

**Machine JSON goes to stdout; human logs go to stderr.** Every verb emits JSON on stdout **by
default** — there is no `--json` flag to remember, because a flag that must be passed to get the
contract is a contract with an off switch. The plain-text verbs are `kb list`, `schema list` and
`version`; all three print a menu or an identifier for a human.

The practical consequence for a caller: `seqforge <verb> … > out.json` is always valid, and progress
never contaminates the parse. The practical consequence for an implementer: nothing goes to stdout
that is not the result object.

**A verb that produces a human artifact writes a file and still answers on stdout.** `report` and
`eval report` render one self-contained HTML page each — every asset inlined, no network, opens on a
double-click — and print a JSON summary naming what they wrote. That is the shape to copy, and it is
*not* an exception to the split: the HTML is a side effect at a path the caller chose, stdout stays
the result object, and no verb ever gains a `--format html` that would make stdout change shape.

Full rationale, including why this makes the CLI drivable by a headless agent turn:
[ADR-0013](../adr/0013-cli-is-a-machine-interface.md).

## Exit codes are the refusal channel

Uniform across every verb, and the reason refusal never has to be parsed out of prose:

| code | name | meaning |
|---|---|---|
| 0 | OK | including a run that emitted non-blocking `Warning`s |
| 1 | ERROR | a bug or an IO failure — **not** a domain refusal |
| 2 | USAGE | bad invocation |
| 3 | BLOCKED | a refusal no human answer clears: at least one `Blocker`, or a gate a verb owns outright (`eval run`'s accuracy floor) |
| 4 | NEEDS_HUMAN | an open `Conflict`, or a non-empty `questions.md`. A human answer *can* clear it |

The 3-versus-4 split is the whole design: 3 says *this cannot be compiled*, 4 says *this is waiting on
you*. Deriving both from one report is `exit_code_for_report`
([`manifest/validate.py`](../../src/seqforge/manifest/validate.py)) — a new verb that can refuse should
call it rather than re-deciding.

**`probe` and `io peek` never return 3 or 4.** They only observe, and an observation cannot refuse.

Exit 4 and the `Stop` hook are the only two ways an ambiguity clears; see [`state.md`](state.md).

## Flags

There are no truly universal flags, and claiming otherwise is how a docs page starts lying. What
recurs, and what it must mean when it does:

- **`-C` / `--workspace`** — the root holding `seqforge/`. On every verb that reads or writes state.
- **`--no-cache`** — do not read or write the content-addressed artifacts, on `probe` and
  `resolve score`, the two verbs that own a cache. A chained `run` has no opt-out and re-resolves
  only when a version bump moves the key. Resume is *implicit* (R5); **there is no `--resume`
  flag**, and adding one would mean the cache was not trusted.
- **`--offline`** — never reach the network. It must **refuse** rather than quietly degrade: a verb
  that silently skips a lookup under `--offline` produces a manifest whose gaps are invisible.
- **`--max-reads` / `--max-bytes`** — the read budget, on every verb that **heads** a FASTQ; always
  as a pair, though two older verbs spell the read half `--n-reads` / `--reads`. A verb that
  consumes a whole file for the pipeline (`io umi-extract`) has no budget and is still bounded by
  R3's shared reader.
- **`--provider` / `--model`** — on the verbs that reach a model. Selection is
  explicit-beats-implicit, and refuses rather than guessing when no credential is present.
- **`--ceiling`** — the token **Ceiling** (`CONTEXT.md`), on the same verbs as `--provider` /
  `--model`. It refuses at exit 3 rather than warning, and `0` removes it.

A new flag on an existing verb is cheap; a new flag that duplicates one of these under another name is
not. And know what the introspecting test does and does not cover: a flag written into a full
`seqforge <verb> --flag` invocation is checked against the app; a flag named in prose — every bullet
above — is not, so those are a review obligation rather than a guarded claim.

## Which verbs are expensive, and in which currency

Three costs, and each verb should be obviously in or out of each:

- **Network** — the `io` group is the *only* network surface. Everything it fetches is checksum-verified
  and cached; whitelists go through pooch. If a verb outside `io` needs the network, it is calling `io`.
  One verb there **writes** rather than reads — `io publish-package` commits a fingerprint package to
  the public benchmark corpus — and it is here for the same reason the readers are: a verb whose
  whole content is a remote call belongs where a reader expects remote calls to be. It validates the
  package before it sends a byte, refuses rather than guessing when no credential is present, and has
  a `--dry-run` that resolves the destination while touching neither. `io` is also where the composed
  pipeline's own verbs live — `umi-extract`, `umi-count`, `h5ad`, `cram`, `qc-bundle`, `fragments` —
  which reach no network and are here because a `shell:` block must call something a dry run can see
  ([ADR-0036](../adr/0036-a-verb-that-needs-a-file-list-is-handed-the-table.md)).
- **An LLM** — `harvest extract` is the **one** LLM touchpoint in a headless run, and verification runs
  inside it rather than after it. Its inputs are documents **or** `--records`, and either alone is a
  legal invocation: eleven of the eighteen benchmark packages carry no prose at all and their whole
  bill is records, so `harvest extract --records dump.json --dry-run` must plan rather than refuse. It
  exits 2 only when there is nothing at all to read. What that plan prints is already the collapsed
  send list — near-identical records fold onto one exemplar at PLAN time, never at send time, or the
  dry run would quote a bill nobody pays
  ([ADR-0031](../adr/0031-a-collapsed-citation-is-regenerable-only-from-the-record-set.md)). `run` (alias `compile`) reaches a model only by way of that stage, so
  `run --no-llm` is a fully deterministic pipeline. `eval run` reaches a model only for prose cases.
  Everything else — probing, scoring, filling, validating, composing, every `kb` self-test — is
  deterministic and shell-scriptable.
- **A toolchain we do not own** — `kb e2e`, `kb e2e-introns` and `kb e2e-cost` need STAR, a genome and a
  Linux compute node. They are the only verbs that may skip because a **toolchain we do not own** is
  absent — `run` and `eval run` both report skipped work of their own; see
  [`eval-corpus.md`](eval-corpus.md).

`run` adds no authority of its own. It chains the deterministic verbs in one headless pass and stops at
the first refusal, and it decides nothing an individual verb would not — there is no monolithic
compile-input object, and introducing one would move decisions out of the verbs that are tested.

## Adding a verb

Three things, in order. **Emit a first-class Pydantic result type**, so `schema export` references only
types that exist and the stdout object round-trips through JSON Schema. **Return an exit code from the
table above**, via `exit_code_for_report` where the verb can refuse. And if the verb is planned but not
yet landed, add it to the `_PLANNED` set in `tests/test_skills.py` **deliberately** — that set is the
declared exemption for documenting something unbuilt, it is currently empty, and a verb that ships must
leave it or the guard rubber-stamps the fiction it exists to catch.
