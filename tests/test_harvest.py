"""Tests for ``harvest``: the canonical span space and the R5 hallucination tripwire.

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
