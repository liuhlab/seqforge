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
fired; why there is no fifth `policy_default` basis is
[ADR-0004](../adr/0004-two-artifacts-not-one.md)'s Consequences. **Open, noted 2026-08-05:** add one
if you want a policy default machine-distinguishable from a derivation.

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
*request*, which is what batching same-ask documents buys back over a fan-out of archive records;
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

The remedy is meant to be *operable*: one that does not name a command is not finished, and where a
verb we ship already prints the answer, naming a third-party API instead is a lead rather than an
instruction ([ADR-0033](../adr/0033-a-submitted-file-is-a-transcript-entry-not-a-checksum.md)). The
exemplar is `io.remote.technical_read_remedy` — read it there rather than from a copy.

## `DatasetManifest` — two truths, one lifetime

[`models/dataset.py`](../../src/seqforge/models/dataset.py). The manifest is a **finished assay** —
what the bench did — with two truths and one lifetime, immutable. `compose()` is a pure function of
`(DatasetManifest, ProcessingManifest)`, and `validate()` also enforces referential integrity (every
experiment `file_uri` is in the library inventory). The "three truths, three sections" pun and the
damage it did are [ADR-0004](../adr/0004-two-artifacts-not-one.md).

- **One decision, one envelope:** `chemistry` is `LibrarySection`'s *only* `Evidenced` field, and
  `assay`, `read_layout` and `files[].read_id` all follow from it
  ([ADR-0006](../adr/0006-one-judgement-one-envelope.md)); `SampleGroup.attributes` is an open dict
  over a controlled vocabulary, never typed fields
  ([ADR-0007](../adr/0007-sample-attributes-are-ncbi-keys.md)).
- **Read a provenance read count through `reads_in_run`**, never by indexing the dict: it owns
  "minimum within a run" and answers `None` — never `0` — for a manifest that measured nothing
  ([ADR-0030](../adr/0030-a-measurement-lives-in-provenance.md)).

**The measurement is in `models/` and the bar it is compared against is not, deliberately.**
`Spec.min_input_reads` is a **KB** schema field ([`kb.md`](kb.md)) because a `spec.yaml` is
hand-authored and CI-validated, so it is out of `SCHEMA_MODELS` and `schema export` never carried it.
The manifest records what was measured, the KB records the bar, and `compose` is the only place they
meet ([ADR-0030](../adr/0030-a-measurement-lives-in-provenance.md),
[ADR-0032](../adr/0032-a-spec-declares-the-shape-of-a-deposit.md)).

## `ProcessingManifest` — intent, plural

[`models/processing.py`](../../src/seqforge/models/processing.py). The flags to the manifest's IR:
many per dataset, and that plurality *is* the design. `user_confirmed` — a basis written nowhere else
in seqforge — is what this artifact exists to carry.

- **`quantification` is a discriminated union** (`SoloQuant | BulkQuant | AtacQuant | UmiQuant`), so a
  recipe stays well-typed against the module that runs it: `params_gate` fails if the emitted config
  disagrees, and without a member per counting family a plate's config block inherits `quantMode`, an
  instruction its counter never heard of. `SoloQuant.features` is **ordered** (index 0 is primary) and
  **required** — **policy** defaults it to five of the six solo features (`SJ` excluded, a different
  feature axis) at [`manifest/policy.py`](../../src/seqforge/manifest/policy.py). What each other
  member does and does not carry is `schema export ProcessingManifest`.
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
  It is `None` for every chemistry declaring no `min_input_reads`, which is every shipped entry but
  the plate one. It lives on the compose *result* and in the pipeline directory, never in the manifest —
  the measurement is the dataset's and the verdict is the run's
  ([ADR-0032](../adr/0032-a-spec-declares-the-shape-of-a-deposit.md)).

## JSON Schema export is the single source of truth

`model_json_schema()` (2020-12) feeds both validation and docs. The **only** LLM-facing schemas are
`AssertionDraft` and the two arbitration types; that set is pinned as `LLM_FACING` in
[`models/__init__.py`](../../src/seqforge/models/__init__.py) and tested. Which providers ship and
what each guarantees about output shape: [ADR-0009](../adr/0009-llm-provider-is-pluggable.md).

The LLM-facing variant is not built here — the Anthropic SDK's strict-mode transform derives it from
the canonical export, and `test_anthropic_strict_transform_drops_unsupported_constraints`
(`tests/test_extract.py`) is the CI guard that our wire schema survives it. Numeric and `pattern`
constraints therefore stay in the **canonical** schema only: Pydantic enforces them at ingest, which
is the real guardrail.

Three invariants on our side keep that transform total: generics are materialized through the named `Evidenced[…]`
subclasses, so every `$def` is stable; there is **no `value: Any`** anywhere (both `Assertion.value`
and `ConflictPosition.value` are `str`); and discriminated unions live only inside `Observation`,
which is code-emitted and never LLM-produced.
