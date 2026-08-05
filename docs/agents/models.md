# The model hierarchy: the decisions the field lists cannot show

Read this when you touch `src/seqforge/models/`. It carries **decisions only** — no field lists. The
schemas themselves are exported, not written down: `seqforge schema list` and `seqforge schema export`
are the single source of truth, and a field list copied into prose is a second schema that drifts.
Terms (`Evidenced`, `Observation`, `Assertion`, `Conflict`, `Blocker`, `Manifest`, `Recipe`) are
defined once in [`CONTEXT.md`](../../CONTEXT.md).

Ground rules: py3.12+, `pydantic>=2`, `mypy --strict` on `models/`. Concrete `Evidenced[…]`
subclasses precede any model that references them — a parametrized generic subclass is a class
statement, not a deferred annotation — so the package compiles top-to-bottom.

## Scalars and the controlled vocabulary

[`models/base.py`](../../src/seqforge/models/base.py). Two are load-bearing:

- **`Uri`** runs an `AfterValidator` that rejects any absolute or local filesystem path (`/…`, `~`,
  `file:///`, `C:\…`, UNC `\\host\share`). A manifest URI is a relative path, a non-file scheme
  (`s3://`, `gs://`, `https://`, `sra:`), or a bare accession — never a path to one machine (R7).
- **`Basis`** is a closed set of four, and **`Rung`** is the escalation step `0..7`.

Policy defaults are stamped `basis="inferred"` with an `evidence` ref naming the policy rule that
fired. A distinct `policy_default` basis is unnecessary because the recipe already varies its basis by
*who decided* ([ADR-0004](../adr/0004-two-artifacts-not-one.md)). **Still open, for the maintainer:**
add one if you want a policy default to be machine-distinguishable from a derivation.

## `Evidenced[T]` — the three-truths carrier

`Evidenced[T]` lives in [`models/base.py`](../../src/seqforge/models/base.py); the concrete
specializations are stable named `$defs` in
[`models/evidenced.py`](../../src/seqforge/models/evidenced.py).

`frozen=True` makes a validated field immutable — nothing edits a value after validation (R2).
Manifests are hashed by canonical serialization, never by `hash()`, so the unhashable `list` field
inside the envelope is a non-issue. Disagreement across bases is a first-class `Conflict`, never a
silent merge. One judgement gets exactly one envelope
([ADR-0006](../adr/0006-one-judgement-one-envelope.md)).

## `Observation` — role-free by construction

[`models/observation.py`](../../src/seqforge/models/observation.py). It reports composition,
segmentation, distinct-value ratios, header grammar and integrity, and assigns **no roles**. The
segment taxonomy is structural (`constant` / `random` / `homopolymer`); mapping `constant→linker/TSO`,
`random→CB|UMI|cDNA`, `homopolymer-T→polyT` is the resolver's job, scored and second-guessable.

Decisions the field list cannot show:

- A **distinct-value window is SUPPORTS-only, never a gate.** It is depth-dependent, so normalize with
  `4^len` and the sampled read count before reading it (see [`resolve.md`](resolve.md)).
- **`ABSTAIN` is first-class** — "the probe cannot see this signal" is not "the signal is absent" —
  and it never gates.
- A fixed-geometry read whose **`mode_share` is a minority** — most of its reads have left the length
  the chemistry declares — means a **pre-trimmed upload**, which is a `PRETRIMMED_VARIABLE_LENGTH`
  Blocker; a truncated gzip member is `TRUNCATED_GZIP`. `n_distinct > 1` is not the test and was:
  one read a base short in a 2 000-read head refused the dataset.
- **Head-derived statistics report the coverage they were measured over, and nothing gates on it.**
  `CycleComposition.n_sampled`, each segment's `coverage`, and `Observation.coverage` exist to be
  collected; a threshold picked before the corpus distribution is visible would be a guess.
- **`estimated_total_reads` is extrapolated from *compressed* bytes-per-read** (or the gzip ISIZE),
  never a full scan: the naive decompressed form undercounts by the ~3–5× compression ratio.
- **`FileIdentity.local_uri` is the one place a local path is allowed.** It records where probe read
  bytes, and it is never copied into a manifest.

## `Assertion` — the LLM-facing draft, split from the stored claim

[`models/assertion.py`](../../src/seqforge/models/assertion.py). `AssertionDraft` is the model's only
structured-output surface: it carries no offsets, no `subject`, and a plain-string value. Code
searches the normalized document for the quote, computes the offsets, composes the stored `Assertion`,
and **owns both flags** (`span_verified`, `entailment_ok`) — both must hold before an Assertion flows
into `manifest fill`. Why the surface carries only fields code can re-check, and why a claim's subject
is the *document* rather than a field the model fills in:
[ADR-0008](../adr/0008-llm-surface-carries-only-checkable-fields.md).

`ExtractionPlanReport` shares this file because it is about the same two things one step earlier:
which documents exist, and what each will be asked — including the ones a **collapse** folded away.
That is two facts, so `PlannedDocument` keeps them apart: `members` is every record the document is
the ONLY reading of, `reduced_members` every record that shares its prose and was sent its own
difference as a document of its own. "One document, 1440 members" therefore means 1439 records nobody
need read again — never 1439 records nobody read, which is what one merged list says
([ADR-0031](../adr/0031-a-collapsed-citation-is-regenerable-only-from-the-record-set.md)).
It is a **result type, not an LLM-facing one** —
`harvest extract --dry-run` prints it and no model ever sees it — so it is in `SCHEMA_MODELS` and
deliberately not in `LLM_FACING`. `estimated_input_tokens` charges the stable system prefix once per
*document* rather than once per run, which is the arithmetic a fan-out over archive records hides;
output tokens are absent because the model decides how many claims a document supports, and the
**Ceiling** is what bounds that half.

`EvalPlanReport` / `CasePlanRow` ([`models/resolve.py`](../../src/seqforge/models/resolve.py)) are the
same object one level up — what `seqforge eval plan` answers for a whole tier — and they live beside
`EvalReport` rather than beside `ExtractionPlanReport` because they are shaped by the harness's
grain: rows in case order, and a **skip that is named rather than costed at zero**. A skipped case is
a case whose price is unknown, not a free one, so it is excluded from the totals and carries its
`unavailable`/`absent` kind, exactly as it does in `EvalReport.per_case`.

## `Conflict` — surfaced, never auto-picked

[`models/conflict.py`](../../src/seqforge/models/conflict.py). `positions[]` (minimum 2) generalizes
the common observed/asserted pair; `kind` is derivable from the position bases; `resolution` records
who decided (`code` / `user` / `benign_equivalence`).

`status="benign"` is the escape hatch for CI-proven twins: two confusable KB entries that emit
*identical* `backend.params` are recorded together and ask zero questions
([`kb.md`](kb.md)). `decidable_by` includes `onlist` — the mechanism that actually splits Multiome and
GEM-X from v3.

## `Blocker` / `Warning` — refusal as an exit code

[`models/blocker.py`](../../src/seqforge/models/blocker.py). A `Blocker` is a structured refusal
emitted alongside a nonzero exit and is **always fatal**; advisory diagnostics are a separate,
non-blocking `Warning` ([ADR-0013](../adr/0013-cli-is-a-machine-interface.md)). `BlockerCode` is a
closed set — read it from the enum, not from prose. Every Blocker carries an actionable `remedy` and a
`subject` that is a basename, a dotted path or a dataset id, never an absolute path.

The remedy is meant to be *operable*, not decorative. The one to imitate is
`MISSING_TECHNICAL_READ`'s: *"re-fetch with `fasterq-dump --include-technical`, or pull the original
submitted files `sra-pub-src-*` via the SRA Data Locator."* A remedy that does not name a command is
not finished.

## `DatasetManifest` — two truths, one lifetime

[`models/dataset.py`](../../src/seqforge/models/dataset.py). The manifest is a **finished assay** —
what the bench did — with two truths and one lifetime, immutable. `compose()` is a pure function of
`(DatasetManifest, ProcessingManifest)`, and `validate()` also enforces referential integrity (every
experiment `file_uri` is in the library inventory). The "three truths, three sections" pun and the
damage it did are [ADR-0004](../adr/0004-two-artifacts-not-one.md).

- **`LibrarySection` — physical truth, authority is evidence.** `chemistry` is the *only* `Evidenced`
  field, carried as an equivalence class because benign twins are recorded together rather than chosen
  between. `assay`, `read_layout` and `files[].read_id` all *follow* from it and carry no envelope of
  their own ([ADR-0006](../adr/0006-one-judgement-one-envelope.md)).
- **`SampleGroup.attributes` is keyed by NCBI's 960 harmonized BioSample names**, with NCBI's own
  definitions ([`io/attributes.py`](../../src/seqforge/io/attributes.py)); the validator refuses
  anything else. An open dict over a controlled vocabulary, never typed fields:
  [ADR-0007](../adr/0007-sample-attributes-are-ncbi-keys.md).
- **`Study` is not `Evidenced`**, and its abstract is deliberately absent — none of it is an
  interpretation, and prose belongs in a document a quote can grep into.
- **`DatasetProvenance` omits `workflow_version`** on purpose: the assay happened before we had an
  opinion about which rules would run over it, and that opinion belongs to the recipe. It **carries
  the per-file read counts** for the mirror-image reason: a measurement a later stage will threshold
  is a function of the probe's budget, so it may not sit inside the two sections the content hash
  covers ([ADR-0030](../adr/0030-a-measurement-lives-in-provenance.md)). Read one through
  `reads_in_run`, which owns "minimum within a run" and answers `None` — never `0` — for a manifest
  that measured nothing.

## `ProcessingManifest` — intent, plural

[`models/processing.py`](../../src/seqforge/models/processing.py). The flags to the manifest's IR:
many per dataset, and that plurality *is* the design. `user_confirmed` — a basis written nowhere else
in seqforge — is what this artifact exists to carry.

- **`quantification` is a discriminated union** (`SoloQuant | BulkQuant | AtacQuant | UmiQuant`) and
  is not decorative: `params_gate` fails if the emitted config disagrees with it. `SoloQuant.features`
  is **ordered** (index 0 is primary) and **defaults to all five** solo features; validators enforce
  "no duplicates" and "Velocyto requires Gene", a real STAR constraint no enum can express.
  `BulkQuant` needs no strandedness knob — `--quantMode GeneCounts` already emits all three strand
  columns, so there was never a decision to make there. `AtacQuant` and `UmiQuant` carry **no knob at
  all**, for two different reasons that land in the same place: ATAC's deliverable is a fragments
  file, so nothing is counted, and the plate counter writes all four matrices in one pass, so nothing
  is chosen. A member per counting family is what keeps a recipe well-typed against the module that
  runs it — without one, a plate's config block inherits `quantMode`, an instruction its counter has
  never heard of.
- **`basis` records *who decided*:** a CLI flag or an `--instruction` document is `user_confirmed`,
  policy is `inferred`.
- **`dataset is None` means a template; set means bound.** `compose` refuses a mismatch with a
  Blocker, never auto-repins, and always writes the bound form it used to `processing.lock.yaml`.
  Both decisions: [ADR-0004](../adr/0004-two-artifacts-not-one.md).

A compiled run is identified by the *pairing*, not by the manifest alone: recorded at compile time and
stored inside neither input, after a single provenance id collided on two recipes over one dataset.
The formula is in [ADR-0005](../adr/0005-run-id-is-the-pairing.md), and only there.

Why the recipe's key set is *closed*, and disjoint from `backend.params`, so that "a user instruction
contradicts the bytes" is inexpressible: [ADR-0011](../adr/0011-closed-instructable-surface.md). Why
an ambiguity whose every answer we can afford to emit is dissolved rather than asked:
[ADR-0012](../adr/0012-produce-every-answer-rather-than-ask.md).

## Score, resolve and compose result types

The stage contract emits ranked candidates, decisions, questions and compiled configs, and each is a
first-class Pydantic type — so `schema export` references only types that exist and every stdout
object round-trips through JSON Schema. Two decisions worth stating:

- **`TechScore` is JSON-safe**: the forbidden sentinel never crosses the wire as `±inf`
  ([ADR-0014](../adr/0014-no-inf-across-the-json-seam.md)).
- **`ArbitrationRequest` / `ArbitrationResponse`** are the opt-in schemas for the LLM's second job
  (arbitrating an ambiguity code already flagged). They are **modelled and unbuilt** — there is no
  verb. The response references a position by *index* and re-derives no values, which is the whole
  point: an arbiter that could restate a value would be an authority.
- **`ComposeResult.admission`** is the read floor the live KB applied and the samples it kept out.
  It is `None` for every chemistry declaring no `min_input_reads`, which is all sixteen shipped
  entries. It lives on the compose *result* and in the pipeline directory, never in the manifest —
  the measurement is the dataset's and the verdict is the run's
  ([ADR-0032](../adr/0032-a-spec-declares-the-shape-of-a-deposit.md)).

## The LLM provider is pluggable

Three providers ship. `anthropic` uses strict `json_schema`, so the output shape is **guaranteed**,
with explicit `cache_control`. `deepseek` offers `json_object` only, so the shape is **not** enforced
by the provider, and caching is automatic prefix caching. `openai-compatible` takes any `base_url` and
a caller-supplied model.

Selection is explicit-beats-implicit (`--provider`, then `SEQFORGE_LLM_PROVIDER`, then auto-detection
from the credentials present) and **refuses rather than guessing** when no credential is available.
Why span verification is what makes the vendor swappable at all, and how the json-object capability
gap is contained rather than papered over:
[ADR-0009](../adr/0009-llm-provider-is-pluggable.md).

## JSON Schema export is the single source of truth

`model_json_schema()` (2020-12) feeds both validation and docs. The **only** LLM-facing schemas are
`AssertionDraft` and the two arbitration types; that set is pinned as `LLM_FACING` in
[`models/__init__.py`](../../src/seqforge/models/__init__.py) and tested.

The LLM-facing variant is **derived from the canonical one by a deterministic, CI-tested transform** —
never a hand-maintained second schema. Emit with `ref_template="#/$defs/{model}"`, then for a
provider's "strict" subset: rewrite `oneOf` to `anyOf`, drop the `discriminator` keyword (keep the
literal tag field), inline single-member `allOf`, hoist `$ref`-sibling descriptions onto the
referenced `$def`, strip `default`, set `additionalProperties: false`, and put every property in
`required` (nullability travels via the null branch). Numeric and `pattern` constraints stay in the
**canonical** schema only: Pydantic enforces them at ingest, which is the real guardrail, so carrying
them into the LLM schema buys nothing and costs a divergence.

Three invariants keep the transform total: generics are materialized through the named `Evidenced[…]`
subclasses, so every `$def` is stable; there is **no `value: Any`** anywhere (both `Assertion.value`
and `ConflictPosition.value` are `str`); and discriminated unions live only inside `Observation`,
which is code-emitted and never LLM-produced.
