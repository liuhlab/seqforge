"""``harvest extract`` — **the one LLM touchpoint in the whole compiler**.

Everything else in seqforge is a verifier. This module's entire job is to turn prose into
``AssertionDraft{field, value, quote}`` candidates. It decides nothing:

- **No offsets.** The model cannot count characters, so it never emits them — ``verify`` greps the
  quote and computes them (a model-supplied offset would reject truthful claims).
- **No provenance identity.** ``span.doc_sha256`` is **overwritten by code** after parsing: we know
  which document we sent, so a fabricated or mistyped sha is not a failure mode we need to have.
- **No verdicts.** The model never asserts that its own quote is real or supportive; ``verify`` owns
  both flags and fails closed.
- **No trusted shape.** Whatever the provider returns is validated against the canonical Pydantic
  model here. That is what makes the provider swappable (see :mod:`seqforge.harvest.providers`):
  strict-schema providers and json-object providers differ in how *likely* a malformed batch is,
  never in whether one could reach the manifest.

The wire schema is derived from ``AssertionDraft`` — never hand-maintained — so the
contract cannot drift from ``models/``.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel, ValidationError

from ..io.remote import _MAX_RETRIES, retry_delay
from ..kb.schema import Spec
from ..models.assertion import AssertionDraft, ExtractorProvenance, SourceSpan
from .fields import describe_asked, fields_for
from .normalize import NormalizedDoc
from .providers import (
    LLMProvider,
    LLMResponse,
    ProviderUnavailable,
    resolve_provider,
    schema_prompt,
)

#: Bump on ANY prompt change — it is folded into ExtractorProvenance so a harvest is reproducible and
#: blamable, and evals treat a prompt edit as a code change.
#: 2026.7.1 — gave `experiment.samples.{tissue,condition}` and `accessions` operational definitions.
#: `eval run --llm` caught DeepSeek V4-Pro filing standard worm husbandry ("maintained on NGM plates
#: seeded with E. coli OP50 at 20 C") as an experimental *condition*: a real quote, correctly copied,
#: pinned to a field it does not belong in. The old prompt said only "everything else: the document's
#: own wording", which invites exactly that. See `verify.entails` for why span verification cannot catch this class.
#: 2026.7.2 — `processing.*` becomes askable, of --instruction documents ONLY. Note the
#: hazard this sits on: 2026.7.1's regression WAS field misassignment, and this adds fields whose
#: misassignment reaches the aligner. Three things contain it, none of them the prompt — the field
#: allowlist (`harvest.fields`), the doc-role gate (`verify_drafts`), and the all-five default, which
#: means a hallucinated instruction can only mislabel the primary matrix, never destroy signal.
#: 2026.7.3 — dropped the hand-written `experiment.samples.condition` definition. `condition` was
#: removed from the asked vocabulary (no archive defines it; NCBI's `treatment`/`genotype`/
#: `disease` replaced it), so the prompt was teaching a field `verify` is guaranteed to reject as
#: `field_not_permitted`: wasted extraction, and a standing invitation to re-file husbandry the way
#: 2026.7.1 did. Also trimmed the `tissue` gloss — it duplicated the NCBI definition `describe_asked`
#: now supplies per attribute and conflated tissue with `cell_type` (its own attribute since 7.2).
#: `test_prompt_names_only_permitted_fields` derives the ⊆ PERMITTED_FIELDS invariant so the prompt
#: cannot drift from `fields.py` again — the hand-maintained-mirror rot this whole module warns about.
#: 2026.7.4 — added `library.prep_type` (single-cell vs single-nucleus). It is the biology twin of the
#: `processing.quantification` caution above: the model reports the INPUT MATERIAL in the document's
#: own words and still names no feature — "single nuclei" is a prep, not GeneFull. Code owns the
#: nuclei->GeneFull-primary mapping (`manifest.policy`), so span verification stays a check on the
#: quote, never a licence to infer a processing decision from biology.
#: 2026.7.5 — documents that receive the SAME ask may travel in ONE request (#190). The system half is
#: byte-untouched, so the cached prefix — and every eval number that turns on it — is unaffected; what
#: moved is the user half of a MULTI-document request, which states the shared ask once and then names
#: each document's sha256 above its own text. A ONE-document request is byte-identical to 2026.7.4's,
#: so a plan whose documents all differ in what is asked of them sends exactly what it sent before —
#: and the version moves anyway, because one code state is one extractor, and a `prompt_version` that
#: depended on how a particular plan happened to group would be unusable as provenance.
#: 2026.8.1 — `bulk-rnaseq`'s four format-describing aliases moved to `descriptive_aliases` (#266),
#: so the KB block no longer offers the model "paired-end RNA-seq" as a spelling of that id. The
#: prompt only ever listed `identity.aliases`, so this is a byte change to the cached prefix and a
#: narrowing of what the model is taught to name; `verify` still ACCEPTS those forms (they stay in
#: `curated_forms`), so nothing the model can now draft is rejected for vocabulary it was never shown.
#: The model is not stranded on a descriptively-worded bulk record either: the id and the entry's
#: name ("Bulk Illumina paired-end RNA-seq (no cell barcode)") are both still in the block.
#: 2026.8.2 — the KB block's bulk entry dropped its `-pe`: the two lines the loop writes for it are now
#: `id: bulk-rnaseq` and `name: Bulk Illumina RNA-seq (no cell barcode)` (#273). The block is derived
#: from the KB, so a chemistry rename lands directly in the CACHED PREFIX, and the version moves for
#: that alone — one code state is one extractor, and a `prompt_version` stamped on two byte-different
#: prompts cannot answer the only question it exists to answer, which is what produced this Assertion.
#: Nothing the model can now draft is newly rejected, and nothing it could draft before is lost. The
#: id it is taught here is the id `verify` checks — both read one KB, so the two cannot disagree about
#: the spelling — and `identity.aliases` is byte-untouched, so every value that named this entry still
#: names it. What moved is the STRING the model is told to return for a chemistry it was already being
#: told about; the entry's own display name moved with it because the block prints that too.
EXTRACT_PROMPT_VERSION = "2026.8.2"

_INSTRUCTIONS = """\
You extract factual claims from a scientific methods document into structured assertions, returned as
json.

You are one stage of a deterministic compiler. Downstream code independently re-greps every quote you
produce and checks that the quote supports the value. Claims that fail either check are DISCARDED, so
inventing or stretching a claim gains nothing — it only wastes the extraction.

Rules:
1. Extract ONLY what the document explicitly states. Never use background knowledge, never infer, and
   never complete a pattern. If the document does not state a field, omit it.
2. `quote` must be a VERBATIM, contiguous substring copied from the document text, exactly as it
   appears. Do not paraphrase, normalize, join across a gap, or fix typos.
3. The quote must, ON ITS OWN, support the value. A reader seeing only that quote must be able to
   conclude the value from it. A quote that merely sits near the fact is not enough: for example,
   "we performed single-cell RNA-seq" does NOT support a specific chemistry version.
4. Keep the quote tight — the shortest span that still supports the value.
5. Return an empty `drafts` list if the document supports nothing. That is a CORRECT and common answer.
6. Never emit character offsets. Code computes them.
7. `llm_confidence` (0.0-1.0) is how sure you are that the document states the claim — not how
   plausible the claim is in general.

Values:
- `library.chemistry`: use the knowledge-base `id` when the document names that technology by any of
  its aliases. If the document names a technology not in the knowledge base, use the document's own
  wording.
- `library.prep_type`: whether the sequenced input was whole CELLS or isolated NUCLEI, copied in the
  document's own words ("single-nucleus", "single nuclei", "snRNA-seq", "isolated nuclei", or
  "single-cell", "scRNA-seq", "whole cells"). Report only the PREP the document states; do NOT name a
  STARsolo feature or an analysis mode — "single nuclei" is an input material, not `GeneFull`. Omit if
  the document does not say.
- `experiment.organism`: the scientific name as written (e.g. "Caenorhabditis elegans").
- `experiment.accessions`: only an explicit database accession (GEO/SRA/ENA/BioProject, e.g.
  "GSE110823", "PRJNA1027859"). A reference genome or assembly name is NOT an accession.
- `experiment.samples.tissue`: a whole organism at a life stage ("adult worm", "L4 larva") is NOT a
  tissue — omit it rather than filing the life stage here (that is `dev_stage`). Each asked sample
  attribute arrives with its own NCBI definition; keep each value in its own field — a cell type is
  `cell_type`, a perturbation is `treatment`, a mutation is `genotype`, none of them `tissue`.
- `processing.quantification`: the STARsolo feature the document NAMES, exactly, as one of: Gene, SJ,
  GeneFull, GeneFull_ExonOverIntron, GeneFull_Ex50pAS, Velocyto. Emit one assertion per feature named.
  Only extract this when the document names the feature; a document describing the BIOLOGY ("single
  nuclei", "pre-mRNA", "include introns") does NOT name a feature, and inferring one from biology is
  not your job — omit the field. Asking for GeneFull adds it; it never removes anything else.
- `processing.genome.assembly`: the UCSC assembly id the document NAMES (e.g. "ce11", "hg38",
  "mm39"). An organism name is not an assembly — omit the field rather than translating one.
- everything else: the document's own wording.
"""


class ExtractUnavailable(RuntimeError):
    """The LLM surface could not produce a usable batch (no provider, API error, or bad shape)."""


class ExtractionResult(BaseModel):
    """The model's structured-output surface: a batch of drafts and nothing else.

    A thin container over the canonical :class:`AssertionDraft` (structured outputs need an object at
    the top level) — deliberately NOT a second hand-maintained schema.
    """

    drafts: list[AssertionDraft]


@dataclass(frozen=True)
class ExtractionOutcome:
    """What extract returns: the drafts, who made them, the call MODE, and what it cost."""

    drafts: list[AssertionDraft]
    extractor: ExtractorProvenance
    provider: str = ""
    model: str = ""
    #: How the call was made — thinking/effort, max_tokens, response_format (see ``LLMResponse.mode``).
    mode: dict[str, object] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    #: Individual drafts the model returned malformed (e.g. ``value: null``) — dropped, not fatal. Same
    #: shape as ``VerifyReport.rejected`` so a run can report both surfaces the same way.
    rejected: list[dict[str, object]] = field(default_factory=list)
    #: Why this document produced nothing, when the reason is that it was never answered — the
    #: provider's own message. ``None`` on every outcome a model really returned, including one that
    #: honestly returned no drafts. **The two are not the same fact**, and an outcome that could not
    #: tell them apart is what let a whole dataset's extraction read as a document supporting nothing.
    failure: str | None = None

    @property
    def answered(self) -> bool:
        """Did the model answer this document at all? An empty batch is an answer; a failure is not."""
        return self.failure is None

    @property
    def cache_hit(self) -> bool:
        """True iff the stable prefix was served from cache (0 across repeats => an invalidator)."""
        return self.usage.get("cache_read_tokens", 0) > 0

    @classmethod
    def unanswered(
        cls, *, provider: str, model: str, detail: str, usage: dict[str, int] | None = None
    ) -> ExtractionOutcome:
        """One document the provider never answered, as a result rather than as an exception.

        It still names who was asked, because "nothing came back" is a fact about an extractor and a
        report that could not say which one would be unreadable a week later.
        """
        return cls(
            drafts=[],
            extractor=ExtractorProvenance(
                model_id=f"{provider}/{model}", prompt_version=EXTRACT_PROMPT_VERSION
            ),
            provider=provider,
            model=model,
            usage=dict(usage or {}),
            failure=detail,
        )


def build_kb_context(specs: dict[str, Spec]) -> str:
    """The stable prefix: what each KB technology is called in the wild.

    Deterministic and frozen — sorted, no timestamps, no per-request ids — because prefix caching (
    explicit on Anthropic, automatic on DeepSeek) is a byte-prefix match and any change invalidates
    it. This is the alias knowledge that lets the model map a paper's "Chromium Single Cell 3' v3"
    onto the id `10x-3p-gex-v3`; `verify` then checks the same KB, so extraction and verification
    cannot disagree about vocabulary. Only the NAMING aliases are listed — a `descriptive_alias`
    ("paired-end RNA-seq") is a phrase any chemistry's record carries truthfully, and offering it here
    as a spelling of `bulk-rnaseq` would invite exactly the draft #266 stopped code from believing.
    `verify` accepts them anyway, so the asymmetry can only ever accept a draft, never reject one.
    """
    lines = ["Knowledge-base technologies (use these ids for library.chemistry):", ""]
    for tech_id in sorted(specs):
        spec = specs[tech_id]
        aliases = ", ".join(spec.identity.aliases) if spec.identity.aliases else "(none)"
        lines += [
            f"id: {spec.identity.id}",
            f"  name: {spec.identity.name}",
            f"  aliases: {aliases}",
        ]
    return "\n".join(lines)


def build_system_prompt(specs: dict[str, Spec], schema: dict[str, Any]) -> str:
    """Instructions + json contract + KB aliases — one prompt, every provider, one prompt_version."""
    return "\n\n".join([_INSTRUCTIONS, schema_prompt(schema), build_kb_context(specs)])


#: The first line :func:`_user_content` writes, read back. An Exchange keeps the user half verbatim
#: and nothing else about the document — it is a record of a request, not of a plan — so this line is
#: how a stored exchange is joined back to the document it was about. Written and read in one module
#: on purpose: a reader that re-derived the format elsewhere would drift from the writer in silence.
_DOC_LINE = re.compile(r"^Document sha256: ([0-9a-f]{64})\s*$", re.MULTILINE)


def document_sha256_in(user: str) -> str | None:
    """Which document an exchange's user half was about, or ``None`` if it does not say.

    **The FIRST one it names.** A request carrying several documents that share an ask names each of
    them, and this reports the one the request opens with — which is the same document the request's
    cost is booked against in :func:`extract_batch`. So a batched request has one document it is
    "about" for ledger and transcript purposes, and the two surfaces agree on which; what neither can
    say is that the other members were in the same request.
    """
    match = _DOC_LINE.search(user)
    return match.group(1) if match is not None else None


def _user_content(doc: NormalizedDoc, fields: tuple[str, ...]) -> str:
    """The per-document half of the prompt: which fields, and the document.

    The ask is scoped, so a sample record's document is never even asked for a chemistry, and the
    sample-attribute definitions come from NCBI's own list rather than from a paraphrase here — see
    `fields.describe_asked`.

    Note what this does NOT say: which sample the document is about. It does not need to. The document
    holds one record's prose and nothing else, so "which sample" is answered by which file we handed
    the model, and code already knows the answer because code chose the file.
    """
    return (
        f"Document sha256: {doc.doc_sha256}\n"
        f"Echo that exact string as span.doc_sha256 on every assertion.\n\n"
        f"Fields to look for (omit any the document does not state):\n"
        + describe_asked(fields)
        + "\n\n<document>\n"
        + doc.text
        + "\n</document>"
    )


def _batch_user_content(docs: Sequence[NormalizedDoc], fields: tuple[str, ...]) -> str:
    """Several documents that receive the SAME ask, as the volatile half of ONE request.

    The ask is written **once** and the documents follow it, which is the second half of what
    batching saves: the nine sample attributes with their NCBI definitions are ~1.3 KB, paid per
    request now rather than per document, on top of the ~9 KB system prefix.

    All of it lives in the USER half, and none of it in the system prompt. That placement is
    load-bearing rather than tidy: the system half is byte-identical across a run, which is the only
    reason prefix caching works at all, and a preamble that said "3 documents follow" would make the
    cached prefix a function of how a plan happened to group — invalidating the cache on every batch
    of a different width.

    One document renders **byte-identically to** :func:`_user_content`, deliberately: a plan whose
    documents all differ in what is asked of them then sends exactly what it sent before batching
    existed, and — the part that matters — the per-document fallback in
    :func:`~seqforge.harvest.plan.extract_planned` re-asks through a shape that has never been
    batched. A fallback that inherited the batch envelope could fail for the same reason the batch
    did.
    """
    if len(docs) == 1:
        return _user_content(docs[0], fields)
    return (
        f"{len(docs)} documents follow, each preceded by its own sha256.\n"
        f"Answer all of them in ONE `drafts` list. On every assertion, copy the sha256 printed "
        f"above the document the quote came from into span.doc_sha256, exactly as written. A "
        f"document that supports nothing simply contributes no assertion, which is normal.\n\n"
        f"Fields to look for (omit any the document does not state):\n"
        + describe_asked(fields)
        + "\n\n"
        + "\n\n".join(
            f"Document sha256: {d.doc_sha256}\n<document>\n{d.text}\n</document>" for d in docs
        )
    )


def llm_schema() -> dict[str, Any]:
    """The wire schema, derived from the canonical model."""
    return ExtractionResult.model_json_schema()


def _complete_with_retry(llm: LLMProvider, **call: Any) -> LLMResponse:
    """One provider call, retried while the provider says the failure was transient.

    `run` stops at the first refusal, so before this a single 429 mid-harvest exited the whole
    headless run — and discarded every document already extracted, which had been paid for in tokens.
    That is the same defect a 429 caused in the `records` stage, so this borrows that fix's policy
    (`retry_delay`, `_MAX_RETRIES`) rather than inventing a second one.

    **The provider classifies; this only obeys.** By the time a `ProviderUnavailable` arrives here the
    SDK exception is gone and "no credential" is indistinguishable from "rate limited" — so a loop
    that guessed would back off four times over a missing API key. It retries only what was *marked*
    transient, and `retry_after` carries the pace: `"0"` for an empty body, which asks for nothing,
    and the server's own `Retry-After` for a rate limit.

    Usage accrues across attempts. A refused call still burned tokens, and the ledger is meant to say
    what the calls cost rather than what the last one did.
    """
    spent: dict[str, int] = {}

    def _bank(usage: dict[str, int]) -> None:
        for key, val in usage.items():
            spent[key] = spent.get(key, 0) + val

    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = llm.complete_json(**call)
        except ProviderUnavailable as exc:
            _bank(exc.usage)
            if not exc.transient or attempt == _MAX_RETRIES:
                # Terminal, or the budget is spent. Either way it fails loudly as `llm_unavailable`
                # at exit 1 — bounded, never a spin.
                raise ExtractUnavailable(str(exc)) from exc
            time.sleep(retry_delay(exc.retry_after, attempt))
            continue
        _bank(response.usage)
        return replace(response, usage=spent)
    raise AssertionError("unreachable: the loop either returns or raises")  # pragma: no cover


def extract_drafts(
    doc: NormalizedDoc,
    specs: dict[str, Spec],
    *,
    provider: LLMProvider | None = None,
    model: str | None = None,
    fields: tuple[str, ...] | None = None,
    max_tokens: int = 8000,
) -> ExtractionOutcome:
    """Ask a model for span-carrying claims about ``doc``. Proposes only — ``verify`` decides.

    ``fields`` defaults to the set appropriate to the document's SCOPE and ROLE: a reference document
    is never asked about ``processing.*``, and a sample record's document is never asked about the
    chemistry. Asking and enforcing are separate jobs, though — ``verify_drafts`` refuses an
    off-scope field regardless of what was asked, because a prompt is not a boundary.

    One document, one request: this is :func:`extract_batch` at width one, and that width is the one
    where the model's echoed ``doc_sha256`` is worthless as evidence and code overwrites it outright.
    """
    return extract_batch(
        [doc],
        specs,
        provider=provider,
        model=model,
        fields=fields,
        max_tokens=max_tokens,
    )[0]


def _parsed_drafts(text: str, provider_name: str) -> list[Any]:
    """THE gate: the response's top-level shape, or a wholesale refusal. Returns the raw draft list.

    json-object providers do not enforce shape, so this is where a malformed batch is caught. The
    split is deliberate: a broken TOP-LEVEL shape (no JSON at all, or no `drafts` array under either
    accepted envelope) is a provider failure with nothing to salvage and dies wholesale (that is #4's
    empty-content case). But a single malformed DRAFT — a `value: null`, a missing span — is just one
    bad proposal from a proposer we already distrust: the caller drops it into `rejected` and keeps
    the rest, exactly as `verify` drops a claim whose quote will not grep back. One flaky token from
    the model must not sink a whole document's worth of valid extraction (#5).

    **This is the only notion of "the response was unusable" there is**, and a batched request adds
    no second one: a batch that fails here fails exactly as a single document does, and what its
    caller then does about it — re-ask each document alone — is a recovery policy, not another
    verdict about the response.
    """
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExtractUnavailable(
            f"{provider_name} returned output that is not valid JSON: {exc}"
        ) from exc
    if isinstance(raw, list):
        # A bare top-level array is that same batch under a different envelope, so unwrap it and let
        # the per-draft gate below do exactly what it does for a wrapped one. Every element still
        # faces `AssertionDraft.model_validate` alone and a malformed one still lands in `rejected` —
        # nothing here is more trusted than before, only differently packaged. Losing the document
        # instead costs a whole document's worth of valid extraction for one token the model failed
        # to write, and it is measurably live: on the benchmark corpus the weaker json-object model
        # lost 6 of 141 documents this way, the stronger one 0-1 (#190).
        #
        # This is NOT the silent half-parse a malformed batch must die of. That one is hunting for a
        # drafts-shaped array *inside* a response whose shape we did not understand, and promoting a
        # fragment of it — half a batch, which is the only outcome worse than none. Here the whole
        # response IS the array: nothing is left over, nothing is searched for, and nothing is
        # repaired. Every other top-level shape (a string, a number, `null`, an object with no usable
        # `drafts` key) still dies wholesale below, because none of them contains a batch to keep.
        #
        # Deliberately not recorded as an envelope quirk anywhere. `Exchange.text` already holds the
        # response verbatim and every run writes its transcript to an address, so how often a model
        # does this is a grep away at full fidelity; a flag beside it would be a lossy second copy of
        # a fact we keep, computed on every outcome for no reader — the shape of dead field this repo
        # has just finished deleting. And an envelope we have never seen still fails loudly, so a
        # provider whose behaviour really changes announces itself rather than being normalised away.
        raw = {"drafts": raw}
    if not isinstance(raw, dict):
        raise ExtractUnavailable(
            f"{provider_name} returned a top-level {type(raw).__name__}, not a JSON object with a "
            f"`drafts` array (nor a bare array of drafts)"
        )
    if not isinstance(raw.get("drafts"), list):
        # Name what is actually wrong with `drafts` — missing, or the wrong type ({'drafts': null}
        # reports "null", not the useless "got dict" of the top-level object.
        detail = "missing" if "drafts" not in raw else f"a {type(raw['drafts']).__name__}"
        raise ExtractUnavailable(
            f"{provider_name} returned no `drafts` array: the `drafts` key is {detail}, not a list"
        )
    return list(raw["drafts"])


def extract_batch(
    docs: Sequence[NormalizedDoc],
    specs: dict[str, Spec],
    *,
    provider: LLMProvider | None = None,
    model: str | None = None,
    fields: tuple[str, ...] | None = None,
    max_tokens: int = 8000,
) -> list[ExtractionOutcome]:
    """One request for several documents that receive the same ask. One outcome per document, in order.

    The overhead this removes is the whole reason it exists: the system prefix is ~9 KB and the
    sample-attribute ask ~1.3 KB, both byte-identical per request, so three archive records of 45,
    209 and 213 characters cost ~9.4 K input tokens as three requests and are >95 % prompt (#190).

    **Every document in ``docs`` must receive the same ask**, because a prompt is the only place the
    ask exists: batching two different asks would silently ask each document the union, wasting the
    ask on documents it does not fit. :func:`~seqforge.harvest.plan.batch_documents` is what
    guarantees it; ``fields`` here defaults from the first document, and enforcement is
    ``verify_drafts``'s regardless — it refuses an off-scope field whatever was asked, so grouping is
    about not *wasting* the ask, never about safety.

    **Which document a draft belongs to is the one thing a batch makes the model responsible for.**
    At width one code overwrites the echoed ``span.doc_sha256`` outright, because we know what we
    sent; at width N the echo is how a draft is routed, and a draft naming a sha that is in no
    document of this batch means the model lost track of the batch. That is a batch-level failure and
    the whole batch is refused, rather than the draft being dropped: dropping it would lose a claim
    that an unbatched run would have kept, which is precisely what batching may not do, while
    refusing costs one extra round trip through a request shape that has no shas to lose track of.

    Routing by the echo is safe for the same reason the batch is: the claim still has to grep back
    into the document it names. A draft mis-routed *between two members* is not caught here on
    purpose — `verify.find_span` is the authority on whether a document carries a quote, and a second,
    weaker "is this substring in that text" living here would disagree with it, re-issuing batches
    over claims that verify fine. Such a draft fails span verification loudly instead.

    A response that answers only SOME of the batch is **not** a failure. A document supporting nothing
    is a correct and common answer that returns no drafts at all, so partial coverage is
    indistinguishable from it — and treating it as a failure would re-ask a batch of eight sample
    records every time six of them had nothing to say, which is strictly more requests than not
    batching at all.
    """
    if not docs:
        return []
    asked = fields if fields is not None else fields_for(docs[0].scope, docs[0].role)
    try:
        llm = provider if provider is not None else resolve_provider()
    except ProviderUnavailable as exc:
        raise ExtractUnavailable(str(exc)) from exc

    chosen = model or llm.default_model()
    schema = llm_schema()
    response = _complete_with_retry(
        llm,
        system=build_system_prompt(specs, schema),
        user=_batch_user_content(docs, asked),
        schema=schema,
        model=chosen,
        max_tokens=max_tokens,
    )

    # First index rather than the document itself: two members could in principle render identically,
    # and a sha-keyed map would then quietly drop one of them. `batch_documents` keeps a batch's shas
    # distinct so this is not reachable from the planner, and a caller that ignores that gets the
    # first — which is what `verify_drafts` would do with the same pair anyway.
    home_of: dict[str, int] = {}
    for i, doc in enumerate(docs):
        home_of.setdefault(doc.doc_sha256, i)

    drafts: list[list[AssertionDraft]] = [[] for _ in docs]
    rejected: list[list[dict[str, object]]] = [[] for _ in docs]
    for item in _parsed_drafts(response.text, llm.name):
        try:
            draft = AssertionDraft.model_validate(item)
        except ValidationError as exc:
            # A malformed draft is dropped whether or not this was a batch, so an unroutable one
            # costs nothing to file against the request's own first document — unlike a VALID draft,
            # which is a claim we would otherwise keep and so is worth a round trip to place.
            claimed = _claimed_index(item, home_of)
            where = 0 if claimed is None else claimed
            rejected[where].append(_malformed_draft(item, docs[where], exc))
            continue
        index = 0 if len(docs) == 1 else home_of.get(draft.span.doc_sha256, -1)
        if index < 0:
            raise ExtractUnavailable(
                f"{llm.name} returned a draft for document {draft.span.doc_sha256!r}, which is "
                f"none of the {len(docs)} documents this request carried"
            )
        drafts[index].append(_anchor(draft, docs[index]))

    extractor = ExtractorProvenance(
        # provenance records the provider too: the same prompt on a different model is a different
        # extractor, and evals must be able to tell those runs apart.
        model_id=f"{llm.name}/{chosen}",
        prompt_version=EXTRACT_PROMPT_VERSION,
    )
    return [
        ExtractionOutcome(
            drafts=drafts[i],
            extractor=extractor,
            provider=llm.name,
            model=chosen,
            mode=response.mode,
            # Cost is a property of the REQUEST, and this batch was one. It is booked whole against
            # the document the request opens with — the one `document_sha256_in` reports for the
            # stored exchange, so the ledger and the transcript name the same document — and the
            # others carry nothing. Dividing it per document would print a number nobody was billed;
            # repeating it per document would make every consumer that sums the outcomes (the eval
            # harness does) report N times the real spend.
            usage=response.usage if i == 0 else {},
            rejected=rejected[i],
        )
        for i in range(len(docs))
    ]


def _claimed_index(item: object, home_of: dict[str, int]) -> int | None:
    """Which document a MALFORMED draft claims to be about, if it named one of this request's."""
    span = item.get("span") if isinstance(item, dict) else None
    sha = span.get("doc_sha256") if isinstance(span, dict) else None
    return home_of.get(sha) if isinstance(sha, str) else None


def _malformed_draft(item: object, doc: NormalizedDoc, exc: ValidationError) -> dict[str, object]:
    """One draft the model returned malformed. Recorded in the ``rejected`` channel and dropped — a
    non-fatal echo of ``verify._reject``, so both surfaces read the same way. Kept defensive because
    ``item`` failed validation: any field may be missing or the wrong type.

    ``doc`` is one this request really carried, chosen by the caller, and never whatever the draft
    claims: a draft that failed validation has no trustworthy span, and code owns provenance identity
    here for the same reason :func:`_anchor` overwrites it on a draft that passed. (A batched request
    carries several, and the caller reads the claimed sha only to pick between documents it did send
    — never to accept one it did not.) Recording it is what makes a rejection readable as *this
    claim, from this record* rather than as an anonymous line in a tally."""
    span = item.get("span") if isinstance(item, dict) else None
    quote = span.get("quote") if isinstance(span, dict) else None
    errors = exc.errors()
    detail = errors[0]["msg"] if errors else str(exc)
    return {
        "doc_sha256": doc.doc_sha256,
        "field": item.get("field") if isinstance(item, dict) else None,
        "value": item.get("value") if isinstance(item, dict) else None,
        "quote": quote[:120] if isinstance(quote, str) else None,
        "reason": "malformed_draft",
        "detail": f"draft failed AssertionDraft validation: {detail}",
    }


def _anchor(draft: AssertionDraft, doc: NormalizedDoc) -> AssertionDraft:
    """Force every draft onto the document we actually sent.

    We know which document this was; the model's echo of the sha is therefore worthless as evidence
    and dangerous as a failure mode (a mistyped sha would be rejected downstream as `unknown_doc`,
    which looks like a hallucination but is just a typo). Code owns provenance identity — the same
    reason code owns the offsets.
    """
    return draft.model_copy(
        update={
            "span": SourceSpan(
                doc_sha256=doc.doc_sha256,
                quote=draft.span.quote,
                context=draft.span.context,
                # offsets stay None here on purpose: `verify` computes them from the real text
            )
        }
    )
