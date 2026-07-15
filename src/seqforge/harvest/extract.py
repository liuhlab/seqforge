"""``harvest extract`` — **the one LLM touchpoint in the whole compiler** (R1/R2).

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

The wire schema is derived from ``AssertionDraft`` (design §1.8) — never hand-maintained — so the
contract cannot drift from ``models/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from ..kb.schema import Spec
from ..models.assertion import AssertionDraft, ExtractorProvenance, SourceSpan
from .fields import fields_for_role
from .normalize import NormalizedDoc
from .providers import LLMProvider, ProviderUnavailable, resolve_provider, schema_prompt

#: Bump on ANY prompt change — it is folded into ExtractorProvenance so a harvest is reproducible and
#: blamable, and evals treat a prompt edit as a code change (brief §9).
#: 2026.7.1 — gave `experiment.samples.{tissue,condition}` and `accessions` operational definitions.
#: `eval run --llm` caught DeepSeek V4-Pro filing standard worm husbandry ("maintained on NGM plates
#: seeded with E. coli OP50 at 20 C") as an experimental *condition*: a real quote, correctly copied,
#: pinned to a field it does not belong in. The old prompt said only "everything else: the document's
#: own wording", which invites exactly that. See `verify.entails` for why R5 cannot catch this class.
#: 2026.7.2 — `processing.*` becomes askable, of --instruction documents ONLY (R13/R14). Note the
#: hazard this sits on: 2026.7.1's regression WAS field misassignment, and this adds fields whose
#: misassignment reaches the aligner. Three things contain it, none of them the prompt — the field
#: allowlist (`harvest.fields`), the doc-role gate (`verify_drafts`), and R15's all-five default, which
#: means a hallucinated instruction can only mislabel the primary matrix, never destroy signal.
EXTRACT_PROMPT_VERSION = "2026.7.2"

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
- `experiment.organism`: the scientific name as written (e.g. "Caenorhabditis elegans").
- `experiment.accessions`: only an explicit database accession (GEO/SRA/ENA/BioProject, e.g.
  "GSE110823", "PRJNA1027859"). A reference genome or assembly name is NOT an accession.
- `experiment.samples.tissue`: the tissue, cell type, or body part the profiled cells came from, in
  the document's wording. Whole organisms at a life stage are not a tissue — omit the field.
- `experiment.samples.condition`: ONLY the experimental perturbation or treatment group that
  distinguishes one sample from another (e.g. "heat shock", "auxin-treated", "control"). Routine
  culture or husbandry shared by every sample — growth medium, temperature, food source, plate type
  — is NOT a condition. If the document describes no perturbation, omit the field: an unperturbed
  baseline experiment has no condition, and copying husbandry into this field is a wrong answer even
  though the words appear in the document.
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
    """What extract returns: the drafts, who made them, and what the call cost."""

    drafts: list[AssertionDraft]
    extractor: ExtractorProvenance
    provider: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def cache_hit(self) -> bool:
        """True iff the stable prefix was served from cache (0 across repeats => an invalidator)."""
        return self.usage.get("cache_read_tokens", 0) > 0


def build_kb_context(specs: dict[str, Spec]) -> str:
    """The stable prefix: what each KB technology is called in the wild.

    Deterministic and frozen — sorted, no timestamps, no per-request ids — because prefix caching (
    explicit on Anthropic, automatic on DeepSeek) is a byte-prefix match and any change invalidates
    it. This is the alias knowledge that lets the model map a paper's "Chromium Single Cell 3' v3"
    onto the id `10x-3p-gex-v3`; `verify` then checks the same aliases from the same KB, so
    extraction and verification cannot disagree about vocabulary.
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


def _user_content(doc: NormalizedDoc, fields: tuple[str, ...]) -> str:
    return (
        f"Document sha256: {doc.doc_sha256}\n"
        f"Echo that exact string as span.doc_sha256 on every assertion.\n\n"
        f"Fields to look for (omit any the document does not state):\n"
        + "\n".join(f"- {f}" for f in fields)
        + "\n\n<document>\n"
        + doc.text
        + "\n</document>"
    )


def llm_schema() -> dict[str, Any]:
    """The wire schema, derived from the canonical model (R1 / design §1.8)."""
    return ExtractionResult.model_json_schema()


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

    ``fields`` defaults to the set appropriate to the document's ROLE: a reference document is never
    asked about ``processing.*``. Asking and enforcing are separate jobs, though — ``verify_drafts``
    refuses an off-role field regardless of what was asked, because a prompt is not a boundary.
    """
    asked = fields if fields is not None else fields_for_role(doc.role)
    try:
        llm = provider if provider is not None else resolve_provider()
    except ProviderUnavailable as exc:
        raise ExtractUnavailable(str(exc)) from exc

    chosen = model or llm.default_model()
    schema = llm_schema()
    try:
        response = llm.complete_json(
            system=build_system_prompt(specs, schema),
            user=_user_content(doc, asked),
            schema=schema,
            model=chosen,
            max_tokens=max_tokens,
        )
    except ProviderUnavailable as exc:
        raise ExtractUnavailable(str(exc)) from exc

    # THE gate. json-object providers do not enforce shape, so this is where a malformed batch dies —
    # loudly and wholesale, rather than as a half-parsed assertion leaking into the manifest (R2).
    try:
        parsed = ExtractionResult.model_validate_json(response.text)
    except ValidationError as exc:
        raise ExtractUnavailable(
            f"{llm.name} returned output that does not match the AssertionDraft schema: {exc}"
        ) from exc

    extractor = ExtractorProvenance(
        # provenance records the provider too: the same prompt on a different model is a different
        # extractor, and evals must be able to tell those runs apart.
        model_id=f"{llm.name}/{chosen}",
        prompt_version=EXTRACT_PROMPT_VERSION,
    )
    return ExtractionOutcome(
        drafts=[_anchor(d, doc) for d in parsed.drafts],
        extractor=extractor,
        provider=llm.name,
        usage=response.usage,
    )


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
