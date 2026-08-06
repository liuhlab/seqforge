# The one LLM seam: what harvest sends, what it keeps, and what code re-checks

Read this when you touch `src/seqforge/harvest/` — nine modules, three verbs (`harvest normalize`,
`harvest extract`, `harvest verify`), and the compiler's only wire call to a model. The rule it lives
under is [R2](rules.md): agents propose, code decides. Terms (`Document`, `Plan`, `Assertion`,
`Collapse`, `Variant span`) are in [`CONTEXT.md`](../../CONTEXT.md), and the decisions are the seven
records at the bottom — glossed here, argued there.

## The flow: `normalize → plan → extract → verify`

| module | what it owns |
|---|---|
| `normalize.py` | the **canonical text** a span is computed against — PDF/XLSX extraction, unicode folding, page ranges — plus both identities (`doc_sha256` for the source bytes, `normalized_sha256` for the span space) and both kinds of mark. `render_record` turns one archive record into one document, reproducibly and forever. |
| `plan.py` | records → documents, **before a token is spent**: which records have prose *and* an ask, the two collapses, the batching, and the fan-out that materializes a collapsed claim per member. A plan is exact rather than projected, because rendering is free — which is what makes `--dry-run` the price of the paid run. |
| `extract.py` | the one LLM touchpoint. Batches documents into requests, validates whatever returns against `ExtractionResult`, overwrites `span.doc_sha256` with the document code actually sent, and discards any offset the model volunteered. |
| `verify.py` | the tripwire, failing closed. Three deterministic checks: the `field` is in the allowlist, the quote greps back (`span_verified`), and the quote entails the value (`entailment_ok`). |
| `providers.py` | the wire. Two adapter classes — `AnthropicProvider`, `OpenAICompatibleProvider` — under three selectable names; `deepseek` is a preset factory over the second, not a third adapter. |
| `meter.py` | at the seam: the only thing that counts an `Exchange`, the only thing that refuses (a token **Ceiling**), the only thing that holds the transcript. It satisfies `LLMProvider` and wraps one, and it deliberately writes no file. |
| `transcript.py` | the meter's other half — the one reader and one writer of the `.jsonl` form: a header line, then one line per exchange, prompts by sha. |
| `fields.py` | the closed vocabulary a draft's `field` may name. **Asking and enforcing are different jobs** — the prompt asks, this refuses everything else, and the prompt is the one component here nothing can make deterministic. |
| `prep.py` | not a stage. The cells-vs-nuclei word list, read by `verify` (does this quote entail a *nucleus* value?) and by `manifest/policy.py` (which matrix is primary). It sits here because harvest is upstream of manifest. |

## Two kinds of span mark, and they may never share a type

`NormalizedDoc` carries two tuples of marks over the same canonical text. They answer different
questions about the same range.

| mark | the question | computed from | read by |
|---|---|---|---|
| `DeclaredSpan` | *is this range prose at all?* — the record re-rendering one of its own typed columns, carried with the attribute and value as proof | the record alone | `verify`: a draft whose quote lies **wholly inside** one is refused `quote_is_a_typed_column` |
| `VariantSpan` | *does this claim speak for the whole group?* — where the near-identical members this document stands for disagree | the whole group, so it is undecidable from one record | `plan.fan_claims`: a claim touching a variant is not fanned |

Conflate them and a fanned claim is silently rejected in one direction, or a quote of a typed column
is fanned into 1439 records that never carried it in the other.

## The plan holds three lists, and only one of them costs anything

- **`plan.documents` — the send list.** Exemplars (marked), plus every *reduced* member's distinctive
  bytes as a document of its own. What a model sees, what `batch_documents` groups into requests, and
  what the meter charges.
- **`plan.collapsed` — exemplar `doc_sha256` → a `CollapsedGroup`.** Both halves hold **full**
  renderings, never the reduced text, because the full rendering is what a fanned assertion cites. A
  `reduced` member had its difference sent; a `withheld` member had nothing sent at all.
- **`plan.all_documents` — both, in plan order.** **What reaches disk** (`seqforge/records/documents/`)
  and what `document_subjects` must cover. Write the send list where this belongs and `resolve` drops
  every fanned claim, silently, for having no subject.

The counts split the same way and for the same reason: `stands_for(doc)` names the records this
document is the *only* reading of, `reduced_members(doc)` the ones sent their own difference. Every
record a plan reads appears in exactly one document's `stands_for` — the arithmetic that makes "no
record went unread" checkable rather than asserted.

## The seven records, in reading order

Dated order is not reading order, and two of these amend something.

1. [ADR-0008](../adr/0008-llm-surface-carries-only-checkable-fields.md) — **start here.** What the
   model may emit at all: `AssertionDraft`, no offsets, no subject. The subject is the document.
2. [ADR-0009](../adr/0009-llm-provider-is-pluggable.md) — why the provider is swappable, and why the
   shared gate rather than any adapter is the only place a shape is judged.
3. [ADR-0007](../adr/0007-sample-attributes-are-ncbi-keys.md) — the key space a sample claim may land
   on. Amended 2026-08-01: an unharmonized attribute is now surfaced rather than skipped in silence.
4. [ADR-0021](../adr/0021-one-deposit-is-one-source-at-every-layer.md) — declared beats read, and
   `DeclaredSpan`. **Read it knowing it is one field short**: the marks it describes are not all the
   marks `NormalizedDoc` carries today.
5. [ADR-0031](../adr/0031-a-collapsed-citation-is-regenerable-only-from-the-record-set.md) —
   **narrows ADR-0021**, which is the reason `VariantSpan` sits beside `DeclaredSpan`; also the
   near-identical collapse, the fan-out and `all_documents`. Nothing outside its Status line says it
   amends 0021, which is why the chain is written down here.
6. [ADR-0034](../adr/0034-a-user-record-set-declares-structure-never-a-fact.md) — a hand-written
   record set declares structure and never a fact, so a structure-only input renders no document,
   plans nothing and exits 0. It makes
   [ADR-0010](../adr/0010-two-resolvers-one-blocks-one-warns.md)'s `asserted` precedence conditional
   on that exclusion.
7. [ADR-0033](../adr/0033-a-submitted-file-is-a-transcript-entry-not-a-checksum.md) — last, and about
   what a record *carries* rather than what harvest does with it. Read it if you touch
   `ArchiveRecord`.

Tests are `tests/test_harvest.py` and `tests/test_extract.py`, split by module in
[`testing.md`](testing.md); where a harvested claim LANDS is `tests/test_records.py`. The schemas are
in [`models.md`](models.md), and the verbs' stream split and exit codes in [`cli.md`](cli.md).
