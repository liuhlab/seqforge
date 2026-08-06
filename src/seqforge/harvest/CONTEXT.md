# Harvest

Prose — the files you handed us and the archive records rendered as text — and the claims read out
of it. This context covers `harvest/`: the model proposes here and nowhere else, code checks every
claim against the bytes of the document it came from, and what a run spent at the model seam is
itself part of the vocabulary.

Words every context shares — **Evidenced**, **Basis**, **Asserted**, **Declared** and the rest — are
defined once in the repo-root `CONTEXT-MAP.md`.

## Language

### Prose and claims

**Document**:
A unit of canonical normalized text a quote can grep into: one file you handed us, or one archive
record rendered on its own. Identified by `doc_sha256` over its source bytes, in a span space pinned
by `normalizer_version`.
_Avoid_: source, corpus, paper, input text

**Instruction**:
A **Document** whose role is `instruction` — the only role permitted to touch `processing.*` fields.
It is read, never obeyed: a claim enters as an **Assertion** and code applies precedence.
_Avoid_: prompt, command, directive, config; a `reference` document is the other role and can never
reach the recipe

**Span**:
Where in a **Document** a claim came from — `{doc_sha256, quote, char_start, char_end}`. The model
supplies the **Quote** only and code computes the offsets, so a fabricated span fails closed rather
than false-rejecting.
_Avoid_: window (a **Window** is a base range inside a read), citation, location, offset

**Variant span**:
A range of an *exemplar*'s text where the near-identical records it stands for *disagree*, marked in
place and never spliced out. Neither a **Span** — that is where a claim came *from* — nor the
**Declared** mark beside it: a variant span decides whether a verified claim may *fan*.
_Avoid_: diff, mask, hole, gap; and never for the reduced text of a **Collapse**, which is a
document of its own and carries no marks at all

**Quote**:
The verbatim substring a claim rests on. It must grep back into the canonical text *and* entail the
value, or the claim is rejected.
_Avoid_: excerpt, snippet, passage; and never `evidence`, which on an envelope is a list of record
ids

**Entailment**:
The check that a **Quote** *supports* the value pinned to it, not merely that the quote exists. It
catches what span verification provably cannot: a real quote attached to a wrong value.
_Avoid_: relevance, similarity, semantic match

**Assertion**:
A claim from prose that survived verification — field, value, **Span**, and the two code-owned flags
`span_verified` and `entailment_ok`. It proposes; it is never an authority.
_Avoid_: fact, extraction, annotation, LLM output

**AssertionDraft**:
The model's only structured-output surface: `{field, value, span:{doc_sha256, quote, context?},
llm_confidence}`. It carries no offsets and no `subject` by design — both would be authority with
nothing to check.
_Avoid_: proposal, raw assertion, candidate (a candidate is a scored technology)

### The model seam

**Exchange**:
One request and the response it got at the model seam, kept whole: system prompt, user text,
returned text, usage, mode. The unit a **Transcript** is a list of, and the unit a call is counted
in: a retry is its own exchange, because tokens were spent on it.
_Avoid_: call, turn, message, completion, round-trip; a **Document** is what an exchange is *about*,
not the exchange

**Ceiling**:
The most tokens one run may spend at the model seam, counted raw and approximate. It bounds what may
be *spent* rather than what may be started, and it refuses rather than warns.
_Avoid_: limit, cap, quota, token budget, max tokens (that is one response's output bound); and
never a **Budget**, which bounds one head in bytes and reads

**Plan**:
Which **Document**s one extraction will send, what each will be asked, and the input tokens that
costs — computed before a token is spent. Exact rather than projected: rendering a document is free,
so a plan holds the same send list the paid run uses.
_Avoid_: estimate, preview, dry run (that is the flag that prints one), schedule; and never for
`compose`'s output, which is a Snakemake plan

**Collapse**:
Reading several archive records through one **Document**: the runs of one sample become one
document, and near-identical records at one level fold onto one *exemplar* whose non-shared spans
are marked. What a collapse never does is drop a byte.
_Avoid_: dedup (that is byte-equality, and it misses records differing only in an accession), merge,
cluster, summarize; and never for `reduce_dataset`'s partition into assays

**Transcript**:
Every **Exchange** of one run, assembled: one system prompt plus N (document, response) pairs, since
the prompt is byte-identical across a run. It is a file, never a field on the result object.
_Avoid_: log, history, conversation, trace; `logs/usage.json` is the **Ledger** — this is what the
spend was spent on

**Ledger**:
What one run spent at the model seam — per-document token and mode costs against the **Ceiling** —
written to `logs/usage.json`. The numbers, where a **Transcript** is what they were spent on.
_Avoid_: usage log, receipt, bill; the *meter* is what writes one, never the file
