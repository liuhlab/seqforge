"""Tests for ``harvest extract`` — the one LLM touchpoint.

The model call is mocked. That is the point: everything *around* the model is deterministic and must
be provable without spending a token — the schema handed to it, the stability of the cached prefix,
and above all that code (not the model) owns provenance and offsets. The model's actual extraction
quality is an evals concern (brief §9), not a unit-test one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from seqforge import kb
from seqforge.harvest import (
    DEFAULT_MODEL,
    EXTRACT_PROMPT_VERSION,
    ExtractionResult,
    ExtractUnavailable,
    build_kb_context,
    extract_drafts,
    normalize_document,
    verify_drafts,
)
from seqforge.models.assertion import AssertionDraft, SourceSpan


class _FakeUsage:
    input_tokens = 1200
    output_tokens = 90
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 800


class _FakeResponse:
    def __init__(self, parsed: ExtractionResult) -> None:
        self.parsed_output = parsed
        self.usage = _FakeUsage()


class _FakeMessages:
    """Captures the request so the tests can assert on what we actually send the model."""

    def __init__(self, parsed: ExtractionResult | Exception) -> None:
        self._parsed = parsed
        self.captured: dict[str, Any] = {}

    def parse(self, **kwargs: Any) -> _FakeResponse:
        self.captured = kwargs
        if isinstance(self._parsed, Exception):
            raise self._parsed
        return _FakeResponse(self._parsed)


class _FakeClient:
    def __init__(self, parsed: ExtractionResult | Exception) -> None:
        self.messages = _FakeMessages(parsed)


def _doc(
    tmp_path: Path, text: str = "Libraries were prepared with the Chromium Single Cell 3' v3 kit."
):
    p = tmp_path / "methods.txt"
    p.write_text(text)
    return normalize_document(p)


def _draft(quote: str, value: str = "10x-3p-gex-v3", sha: str = "0" * 64) -> AssertionDraft:
    return AssertionDraft(
        field="library.chemistry",
        value=value,
        span=SourceSpan(doc_sha256=sha, quote=quote),
        llm_confidence=0.9,
    )


# ---------- the wire schema (R1 / design §1.8) ----------
def test_llm_schema_is_derived_and_strict_subset_clean() -> None:
    """The schema the model sees is DERIVED from the canonical model and drops what strict rejects.

    Design §1.8: numeric/pattern constraints live in the canonical schema only — Pydantic enforces
    them at ingest (the real guardrail, R2) — and are stripped from the LLM-facing schema. The SDK
    performs that transform, so there is no hand-maintained second schema to drift. This test is the
    CI guard: if someone adds a `pattern`/`minLength` to an LLM-facing model, or the transform stops
    stripping, we find out here rather than via a 400 on a live extraction.
    """
    from anthropic.lib._parse._transform import transform_schema

    strict = transform_schema(ExtractionResult)

    def all_keys(node: Any, acc: set[str] | None = None) -> set[str]:
        acc = acc if acc is not None else set()
        if isinstance(node, dict):
            for k, v in node.items():
                acc.add(k)
                all_keys(v, acc)
        elif isinstance(node, list):
            for v in node:
                all_keys(v, acc)
        return acc

    banned = {
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "multipleOf", "minLength", "maxLength", "pattern", "default",
    }  # fmt: skip
    assert not (all_keys(strict) & banned)

    draft = strict["$defs"]["AssertionDraft"]
    assert draft["additionalProperties"] is False
    # the canonical bounds survive only as a hint the model *might* follow; Pydantic re-checks them
    assert "1.0" in draft["properties"]["llm_confidence"]["description"]
    # the fields the model must supply are exactly the claim + its span
    assert set(draft["required"]) >= {"field", "value", "span", "llm_confidence"}


# ---------- the cached prefix ----------
def test_kb_context_is_deterministic_and_carries_aliases() -> None:
    specs = kb.load_all_specs()
    once, twice = build_kb_context(specs), build_kb_context(specs)
    # prompt caching is a byte-prefix match — an unstable context would silently never cache
    assert once == twice
    assert "10x-3p-gex-v3" in once
    assert "Chromium 3' v3" in once  # the alias that lets the model map paper prose -> the id
    # ids appear in sorted order, so a KB addition cannot reshuffle the prefix
    assert once.index("10x-3p-gex-v2") < once.index("10x-3p-gex-v3") < once.index("splitseq")


def test_kb_context_has_no_volatile_content() -> None:
    ctx = build_kb_context(kb.load_all_specs())
    for volatile in ("2026-", "T00:", "uuid", "session"):
        assert volatile not in ctx


# ---------- what we send ----------
def test_extract_sends_cache_breakpoint_on_the_last_system_block(tmp_path: Path) -> None:
    client = _FakeClient(ExtractionResult(drafts=[]))
    extract_drafts(_doc(tmp_path), kb.load_all_specs(), client=client)
    sent = client.messages.captured

    assert sent["model"] == DEFAULT_MODEL == "claude-opus-4-8"
    assert sent["output_format"] is ExtractionResult  # schema derived from the canonical model
    assert sent["thinking"] == {"type": "adaptive"}

    system = sent["system"]
    # the breakpoint must sit on the LAST system block (render order: tools -> system -> messages),
    # so instructions + KB context cache together and the volatile document stays outside
    assert "cache_control" not in system[0]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    # the document is volatile and must NOT be in the cached prefix
    assert "Chromium Single Cell 3' v3 kit" not in "".join(b["text"] for b in system)
    assert "Chromium Single Cell 3' v3 kit" in sent["messages"][0]["content"]


def test_extract_reports_cache_usage(tmp_path: Path) -> None:
    client = _FakeClient(ExtractionResult(drafts=[]))
    outcome = extract_drafts(_doc(tmp_path), kb.load_all_specs(), client=client)
    assert outcome.cache_hit is True
    assert outcome.usage["cache_read_input_tokens"] == 800


# ---------- code owns provenance ----------
def test_extract_overwrites_the_models_doc_sha(tmp_path: Path) -> None:
    """We know which document we sent — the model's echo is worthless as evidence, so code wins.

    A mistyped sha would otherwise be rejected downstream as `unknown_doc`, which looks like a
    hallucination but is just a typo. Removing the field from the model's control removes the class.
    """
    nd = _doc(tmp_path)
    client = _FakeClient(
        ExtractionResult(drafts=[_draft("Chromium Single Cell 3' v3", sha="dead" * 16)])
    )
    outcome = extract_drafts(nd, kb.load_all_specs(), client=client)
    assert outcome.drafts[0].span.doc_sha256 == nd.doc_sha256  # not the model's "dead..." value


def test_extract_never_accepts_model_supplied_offsets(tmp_path: Path) -> None:
    nd = _doc(tmp_path)
    lying = AssertionDraft(
        field="library.chemistry",
        value="10x-3p-gex-v3",
        span=SourceSpan(
            doc_sha256=nd.doc_sha256,
            quote="Chromium Single Cell 3' v3",
            char_start=999,  # a model cannot count characters; this must never survive
            char_end=1234,
        ),
        llm_confidence=0.9,
    )
    outcome = extract_drafts(
        nd, kb.load_all_specs(), client=_FakeClient(ExtractionResult(drafts=[lying]))
    )
    assert outcome.drafts[0].span.char_start is None
    assert outcome.drafts[0].span.char_end is None


def test_extract_records_blamable_provenance(tmp_path: Path) -> None:
    outcome = extract_drafts(
        _doc(tmp_path), kb.load_all_specs(), client=_FakeClient(ExtractionResult(drafts=[]))
    )
    assert outcome.extractor.model_id == DEFAULT_MODEL
    assert outcome.extractor.prompt_version == EXTRACT_PROMPT_VERSION


def test_extract_empty_is_a_valid_answer(tmp_path: Path) -> None:
    outcome = extract_drafts(
        _doc(tmp_path, "We sequenced some things."),
        kb.load_all_specs(),
        client=_FakeClient(ExtractionResult(drafts=[])),
    )
    assert outcome.drafts == []


def test_extract_surfaces_api_failure_as_unavailable(tmp_path: Path) -> None:
    client = _FakeClient(RuntimeError("429 rate limited"))
    with pytest.raises(ExtractUnavailable):
        extract_drafts(_doc(tmp_path), kb.load_all_specs(), client=client)


# ---------- extract -> verify: the loop that actually protects the corpus ----------
def test_extracted_drafts_flow_into_the_tripwire(tmp_path: Path) -> None:
    """A truthful draft survives; a fabricated one from the same batch is rejected."""
    nd = _doc(tmp_path)
    client = _FakeClient(
        ExtractionResult(
            drafts=[
                _draft("Chromium Single Cell 3' v3"),  # real quote, supports the value
                _draft("using the 10x v3 protocol (Fig. 2)"),  # never appears in the document
            ]
        )
    )
    outcome = extract_drafts(nd, kb.load_all_specs(), client=client)
    report = verify_drafts(outcome.drafts, [nd], extractor=outcome.extractor)
    assert report.n_accepted == 1
    assert len(report.rejected) == 1 and report.rejected[0]["reason"] == "span_not_found"
    # the survivor carries code-computed offsets that really point at the quote
    a = report.assertions[0]
    assert nd.text[a.span.char_start : a.span.char_end] == "Chromium Single Cell 3' v3"
