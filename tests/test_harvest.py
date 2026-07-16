"""Tests for ``harvest``: the canonical span space and the hallucination tripwire.

The adversarial cases ARE the feature. A tripwire that only passes honest input proves nothing — so
these assert that fabricated provenance and a real-quote-wrong-value both get rejected, and equally
that a truthful quote mangled by PDF wrapping is NOT rejected (a tripwire with false positives is one
we would soon learn to ignore).
"""

from __future__ import annotations

from pathlib import Path

from seqforge.harvest import (
    entails,
    find_span,
    normalize_document,
    normalize_text,
    verify_drafts,
)
from seqforge.models.assertion import AssertionDraft, ExtractorProvenance, SourceSpan

EXTRACTOR = ExtractorProvenance(model_id="test-model", prompt_version="v1")


# ---------- normalize: the span space ----------
def test_normalize_dehyphenates_across_a_line_wrap() -> None:
    # a PDF splits "chemistry" across lines; a truthful quote must still be findable
    assert "chemistry" in normalize_text("we used the chemi-\nstry described")


def test_normalize_unwraps_lines_but_keeps_paragraphs() -> None:
    out = normalize_text("first line\nsecond line\n\nnew paragraph")
    assert "first line second line" in out  # a lone newline was a wrap artifact
    assert "\n\n" in out  # a blank line was a real boundary


def test_normalize_expands_ligatures_and_smart_punctuation() -> None:
    out = normalize_text("the ﬁrst 3′ kit — “quoted” ‘v3’")
    assert "first" in out  # ﬁ ligature
    assert "3'" in out  # prime -> apostrophe
    assert '"quoted"' in out and "'v3'" in out  # smart quotes flattened
    assert "-" in out  # em dash -> hyphen


def test_normalize_strips_soft_hyphen_and_nbsp() -> None:
    out = normalize_text("single­cell RNA-seq")
    assert "singlecell RNA-seq" in out


def test_normalize_document_records_both_identities(tmp_path: Path) -> None:
    doc = tmp_path / "methods.txt"
    # deliberately un-canonical (ligature + wrap), so normalization actually does something
    doc.write_text("Libraries used the ﬁrst Chromium Single\nCell 3' v3 kit.")
    nd = normalize_document(doc)
    assert nd.source_basename == "methods.txt"
    assert len(nd.doc_sha256) == 64 and len(nd.normalized_sha256) == 64
    # source identity != span-space identity once normalization has changed anything
    assert nd.doc_sha256 != nd.normalized_sha256
    assert nd.n_chars == len(nd.text)
    assert "first Chromium Single Cell 3' v3" in nd.text


# ---------- find_span ----------
def test_find_span_tolerates_whitespace_differences() -> None:
    text = "Libraries were prepared with the Chromium Single Cell 3' v3 kit."
    assert find_span(text, "Chromium Single Cell 3' v3") is not None
    # the same quote as copied across a line wrap (extra/newline whitespace) still resolves
    span = find_span(text, "Chromium  Single\nCell 3' v3")
    assert span is not None and text[span[0] : span[1]].startswith("Chromium")
    assert find_span(text, "Chromium Single Cell 5' v2") is None


# ---------- entailment ----------
def test_entailment_accepts_a_kb_alias_carried_by_the_quote() -> None:
    # the paper never writes "10x-3p-gex-v3"; it writes the vendor's prose. KB aliases bridge that.
    assert entails("Chromium Single Cell 3' v3 kit", "library.chemistry", "10x-3p-gex-v3")


def test_entailment_rejects_a_real_quote_pinned_to_a_wrong_value() -> None:
    """The design's named failure: a verbatim span attached to a conclusion it does not support."""
    assert not entails("We performed single-cell RNA-seq.", "library.chemistry", "10x-3p-gex-v3")


def test_entailment_rejects_a_different_version() -> None:
    assert not entails("Chromium Single Cell 3' v2 kit", "library.chemistry", "10x-3p-gex-v3")


def test_entailment_plain_value_substring() -> None:
    assert entails(
        "The organism was Caenorhabditis elegans.", "experiment.organism", "Caenorhabditis elegans"
    )


def test_entailment_is_vacuous_when_the_value_is_copied_out_of_the_quote() -> None:
    """Pin the LIMIT of span verification, so nobody mistakes it for a guarantee it does not offer.

    The check's power comes from `value` being a controlled-vocabulary term whose surface forms must
    appear in the quote. For a free-text field the model copies the value out of the quote, so the
    substring test is trivially satisfied and this returns True for ANY field label.

    Hence: entailment catches fabricated and mis-attributed claims, never field-assignment errors. A
    real quote filed under the wrong field passes here by construction — `eval run --llm` caught
    exactly that (standard worm husbandry filed as an experimental `condition`). The defense is the
    prompt's field definition plus the evals corpus, not this function.
    """
    husbandry = "maintained on NGM plates seeded with E. coli OP50 at 20 C"
    quote = f"Caenorhabditis elegans were {husbandry}."
    # A true statement, correctly quoted, filed under a field it does not belong in — and entails()
    # cannot tell. It would say yes to any field name at all:
    assert entails(quote, "experiment.samples.condition", husbandry)
    assert entails(quote, "experiment.samples.tissue", husbandry)
    assert entails(quote, "totally.made.up.field", husbandry)
    assert not entails(
        "The organism was Homo sapiens.", "experiment.organism", "Caenorhabditis elegans"
    )


# ---------- verify: the tripwire end to end ----------
def _doc(tmp_path: Path, text: str) -> object:
    p = tmp_path / "methods.txt"
    p.write_text(text)
    return normalize_document(p)


def test_verify_accepts_a_truthful_span_and_computes_offsets(tmp_path: Path) -> None:
    nd = _doc(tmp_path, "Libraries were prepared with the Chromium Single Cell 3' v3 kit.")
    draft = AssertionDraft(
        field="library.chemistry",
        value="10x-3p-gex-v3",
        span=SourceSpan(doc_sha256=nd.doc_sha256, quote="Chromium Single Cell 3' v3"),
        llm_confidence=0.9,
    )
    report = verify_drafts([draft], [nd], extractor=EXTRACTOR)
    assert report.n_accepted == 1 and not report.rejected
    a = report.assertions[0]
    assert a.span_verified and a.entailment_ok
    # offsets are code-computed, and they really point at the quote
    assert nd.text[a.span.char_start : a.span.char_end] == "Chromium Single Cell 3' v3"


def test_verify_rejects_fabricated_provenance(tmp_path: Path) -> None:
    nd = _doc(tmp_path, "Libraries were prepared with the Chromium Single Cell 3' v3 kit.")
    draft = AssertionDraft(
        field="library.chemistry",
        value="10x-3p-gex-v3",
        span=SourceSpan(doc_sha256=nd.doc_sha256, quote="the 10x Chromium v3 protocol (Figure 2)"),
        llm_confidence=0.99,  # high confidence must not rescue an invented quote
    )
    report = verify_drafts([draft], [nd], extractor=EXTRACTOR)
    assert report.n_accepted == 0
    assert report.rejected[0]["reason"] == "span_not_found"


def test_verify_rejects_real_quote_wrong_value(tmp_path: Path) -> None:
    nd = _doc(tmp_path, "We performed single-cell RNA-seq on dissociated tissue.")
    draft = AssertionDraft(
        field="library.chemistry",
        value="10x-3p-gex-v3.1",
        span=SourceSpan(doc_sha256=nd.doc_sha256, quote="single-cell RNA-seq"),
        llm_confidence=0.95,
    )
    report = verify_drafts([draft], [nd], extractor=EXTRACTOR)
    assert report.n_accepted == 0
    assert report.rejected[0]["reason"] == "not_entailed"


def test_verify_rejects_a_quote_citing_an_unknown_document(tmp_path: Path) -> None:
    nd = _doc(tmp_path, "Chromium Single Cell 3' v3 kit.")
    draft = AssertionDraft(
        field="library.chemistry",
        value="10x-3p-gex-v3",
        span=SourceSpan(doc_sha256="0" * 64, quote="Chromium Single Cell 3' v3"),
        llm_confidence=0.9,
    )
    report = verify_drafts([draft], [nd], extractor=EXTRACTOR)
    assert report.n_accepted == 0 and report.rejected[0]["reason"] == "unknown_doc"


def test_verify_accepts_a_quote_broken_by_pdf_wrapping(tmp_path: Path) -> None:
    """A truthful quote mangled by PDF wrapping must survive — a tripwire with false positives is one
    we would soon learn to ignore. Exercises all three hazards at once: a SEMANTIC hyphen at a wrap
    (3-prime), a wrap hyphen inside a word (chemi-stry), and a plain mid-sentence line break."""
    nd = _doc(
        tmp_path,
        "Libraries used the Chromium Single Cell 3-\nprime v3 chemi-\nstry\nthroughout the study.",
    )
    assert (
        "3-prime v3 chemistry throughout" in nd.text
    )  # meaningful hyphen kept, wrap hyphen closed
    draft = AssertionDraft(
        field="library.chemistry",
        value="10x-3p-gex-v3",
        span=SourceSpan(doc_sha256=nd.doc_sha256, quote="Chromium Single Cell 3-prime v3"),
        llm_confidence=0.8,
    )
    report = verify_drafts([draft], [nd], extractor=EXTRACTOR)
    assert report.n_accepted == 1, report.rejected


# ---------- the field allowlist: the check span verification cannot stand in for ----------
def test_verify_rejects_a_field_nobody_authorized(tmp_path: Path) -> None:
    """A REAL quote, entailing a REAL value, on a field that was never on offer.

    This is not a hypothetical. `AssertionDraft.field` is a plain `str` (it must be, to stay inside
    every provider's strict-schema subset), and `DEFAULT_FIELDS` was only ever interpolated into the
    prompt — `verify` never compared a returned draft against it. Both span-verification checks pass
    here on their own terms: the quote is verbatim and it genuinely supports "10". Span verification
    asks "is this claim in the document?"; the question this draft needs is "may you set this field at
    all?", and only an allowlist can answer it. Without it, prose becomes aligner argv, which is
    precisely what we forbid.
    """
    nd = _doc(tmp_path, "For this dataset, add --outFilterMismatchNmax 10 to the alignment.")
    draft = AssertionDraft(
        field="processing.params.outFilterMismatchNmax",
        value="10",
        span=SourceSpan(doc_sha256=nd.doc_sha256, quote="add --outFilterMismatchNmax 10"),
        llm_confidence=0.95,
    )
    report = verify_drafts([draft], [nd], extractor=EXTRACTOR)
    assert report.n_accepted == 0
    assert report.rejected[0]["reason"] == "field_not_permitted"

    # ...and prove the rejection is the ALLOWLIST talking, not a weak quote: both span-verification checks pass.
    from seqforge.harvest.verify import entails, find_span

    assert find_span(nd.text, draft.span.quote) is not None
    assert entails(draft.span.quote, draft.field, draft.value)


def test_verify_still_accepts_every_permitted_field(tmp_path: Path) -> None:
    """The allowlist must not be so tight it rejects the fields we actually ask for."""
    from seqforge.harvest.fields import DEFAULT_FIELDS, PERMITTED_FIELDS

    assert set(DEFAULT_FIELDS) <= PERMITTED_FIELDS
    nd = _doc(tmp_path, "We profiled Caenorhabditis elegans neurons, deposited as PRJNA1027859.")
    draft = AssertionDraft(
        field="experiment.organism",
        value="Caenorhabditis elegans",
        span=SourceSpan(doc_sha256=nd.doc_sha256, quote="Caenorhabditis elegans"),
        llm_confidence=0.9,
    )
    report = verify_drafts([draft], [nd], extractor=EXTRACTOR)
    assert report.n_accepted == 1, report.rejected


def test_the_allowlist_is_exact_match_not_a_prefix_rule(tmp_path: Path) -> None:
    """A prefix rule ("anything under experiment.") would re-open the hole it exists to close."""
    from seqforge.harvest.fields import is_permitted

    assert is_permitted("experiment.samples.tissue")
    assert not is_permitted("experiment.samples.tissue.extra")
    # `condition` was OURS, not NCBI's. It is gone -- see io/attributes.py.
    assert not is_permitted("experiment.samples.condition")
    assert not is_permitted("experiment.anything.you.can.name")
    assert not is_permitted("library.chemistry.value")


# ---------- doc role: only a document you hand us may steer the pipeline ----------
def test_a_reference_doc_may_not_set_processing(tmp_path: Path) -> None:
    """A downloaded methods PDF must never reach the aligner.

    The quote is real and it entails GeneFull — both span-verification checks pass. What rejects it is the document's
    ROLE, which code owns because code chose the flag. This is a deliberate narrowing of "instructions
    may live among the unstructured metadata", and it costs nothing: with the all-five default a
    paper saying "we used GeneFull" describes a subset of what we already compute.
    """
    from seqforge.harvest import normalize_document

    doc = tmp_path / "paper.txt"
    doc.write_text("Reads were quantified in GeneFull mode against ce11.")
    nd = normalize_document(doc)  # default role: reference
    assert nd.role == "reference"
    draft = AssertionDraft(
        field="processing.quantification",
        value="GeneFull",
        span=SourceSpan(doc_sha256=nd.doc_sha256, quote="quantified in GeneFull mode"),
        llm_confidence=0.9,
    )
    report = verify_drafts([draft], [nd], extractor=EXTRACTOR)
    assert report.n_accepted == 0
    assert report.rejected[0]["reason"] == "field_not_permitted_for_doc"

    # ...and the SAME bytes, offered as an instruction, are accepted. Role is not a property of the
    # file — it is a property of how it was offered.
    nd_i = normalize_document(doc, role="instruction")
    assert nd_i.doc_sha256 == nd.doc_sha256, "role must not fork a document's identity"
    assert verify_drafts([draft], [nd_i], extractor=EXTRACTOR).n_accepted == 1


def test_r5_is_non_vacuous_for_a_closed_vocabulary_field(tmp_path: Path) -> None:
    """The one field where entailment actually bites — and it must stay that way.

    `entails` is vacuous when value ⊆ quote, so it does real work ONLY for a controlled vocabulary.
    soloFeatures is closed (six STARsolo values), so a quote must NAME the feature. A quote describing
    the biology does not, and rejecting it is the RIGHT answer: inferring "nuclei -> GeneFull" is an
    inference code owns, not the model. If surface_forms ever learned that alias, span verification would be theatre.
    """
    from seqforge.harvest.verify import entails

    assert entails("should be aligned in GeneFull mode", "processing.quantification", "GeneFull")
    assert entails("pass --soloFeatures GeneFull", "processing.quantification", "GeneFull")
    assert entails(
        "quantify with GeneFull_Ex50pAS", "processing.quantification", "GeneFull_Ex50pAS"
    )
    # ...and the line that must NOT be crossed:
    assert not entails("we prepared single nuclei", "processing.quantification", "GeneFull")
    assert not entails("count introns too", "processing.quantification", "GeneFull")
    assert not entails("this is a pre-mRNA rich sample", "processing.quantification", "GeneFull")


def test_surface_forms_dispatch_is_exact_match_not_a_substring_test() -> None:
    """The old test was `if "chemistry" in field or "assay" in field` — it would misfire on any field
    that merely CONTAINS the word, e.g. `processing.assay_override`."""
    from seqforge.harvest.verify import surface_forms

    assert len(surface_forms("library.chemistry", "10x-3p-gex-v3")) > 1  # KB aliases attach
    assert surface_forms("processing.assay_override", "10x-3p-gex-v3") == ["10x-3p-gex-v3"]
    assert surface_forms("experiment.samples.tissue", "neurons") == ["neurons"]
    # quantification has NO alias source, on purpose: STARsolo's own spelling is the only form, so an
    # alias table could only loosen the one check that is not vacuous.
    assert surface_forms("processing.quantification", "GeneFull") == ["GeneFull"]
