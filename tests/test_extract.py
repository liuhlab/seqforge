"""Tests for ``harvest extract`` — the one LLM touchpoint, across providers.

The model call is faked. That is the point: everything *around* the model is deterministic and must be
provable without spending a token — the schema, the stability of the cached prefix, and above all that
code (not the model) owns provenance, offsets, and the shape gate. Extraction *quality* is an evals
concern (evals), not a unit-test one.

Both provider shapes are covered, because they differ in a way that matters: a strict-schema provider
guarantees the shape, a json-object provider (DeepSeek) does not. The gate must hold either way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from seqforge import kb
from seqforge.harvest import (
    ANTHROPIC_DEFAULT_MODEL,
    DEEPSEEK_DEFAULT_MODEL,
    EXTRACT_PROMPT_VERSION,
    AnthropicProvider,
    ExtractUnavailable,
    LLMResponse,
    OpenAICompatibleProvider,
    ProviderUnavailable,
    build_kb_context,
    build_system_prompt,
    deepseek_provider,
    extract_drafts,
    llm_schema,
    normalize_document,
    resolve_provider,
    verify_drafts,
)

_QUOTE = "Chromium Single Cell 3' v3"
_TEXT = "Libraries were prepared with the Chromium Single Cell 3' v3 kit."


class _FakeProvider:
    """A provider that returns whatever JSON text we hand it, and records the request."""

    name = "fake"

    def __init__(self, payload: str | Exception, model: str = "fake-model-1") -> None:
        self._payload = payload
        self._model = model
        self.captured: dict[str, Any] = {}

    def default_model(self) -> str:
        return self._model

    def complete_json(self, **kwargs: Any) -> LLMResponse:
        self.captured = kwargs
        if isinstance(self._payload, Exception):
            raise self._payload
        return LLMResponse(text=self._payload, usage={"input_tokens": 10, "cache_read_tokens": 800})


def _doc(tmp_path: Path, text: str = _TEXT):
    p = tmp_path / "methods.txt"
    p.write_text(text)
    return normalize_document(p)


def _batch(
    quote: str = _QUOTE, value: str = "10x-3p-gex-v3", sha: str = "0" * 64, **extra: Any
) -> str:
    span = {"doc_sha256": sha, "quote": quote, "context": None, **extra}
    return json.dumps(
        {
            "drafts": [
                {"field": "library.chemistry", "value": value, "span": span, "llm_confidence": 0.9}
            ]
        }
    )


# ---------- the wire schema (design §1.8) ----------
def test_llm_schema_is_derived_from_the_canonical_model() -> None:
    schema = llm_schema()
    assert "AssertionDraft" in schema["$defs"]
    assert "quote" in schema["$defs"]["SourceSpan"]["properties"]


def test_anthropic_strict_transform_drops_unsupported_constraints() -> None:
    """Design §1.8: constraints live in the canonical schema (Pydantic enforces them at ingest) and
    are stripped from the LLM-facing one. The SDK performs that transform, so there is no second
    hand-maintained schema to drift — this is the CI guard on that."""
    from anthropic.lib._parse._transform import transform_schema

    strict = transform_schema(llm_schema())

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
    assert strict["$defs"]["AssertionDraft"]["additionalProperties"] is False


# ---------- the prompt / stable prefix ----------
def test_kb_context_is_deterministic_and_carries_aliases() -> None:
    specs = kb.load_all_specs()
    once, twice = build_kb_context(specs), build_kb_context(specs)
    assert once == twice  # prefix caching is a byte match; an unstable context never caches
    assert "Chromium 3' v3" in once  # the alias that bridges paper prose -> the KB id
    assert once.index("10x-3p-gex-v2") < once.index("10x-3p-gex-v3") < once.index("splitseq")


def test_system_prompt_satisfies_the_json_mode_contract() -> None:
    """DeepSeek's json_object mode REQUIRES the word 'json' plus a format example in the prompt."""
    prompt = build_system_prompt(kb.load_all_specs(), llm_schema())
    assert "json" in prompt.lower()
    assert "AssertionDraft" in prompt  # the schema travels in-prompt for non-strict providers
    assert '"drafts"' in prompt  # the worked example
    for volatile in ("2026-07-1", "T00:", "uuid4"):
        assert volatile not in prompt  # nothing per-request may enter the cached prefix


def test_prompt_names_only_permitted_fields() -> None:
    """Every manifest path the prompt names must be one code will actually accept.

    `experiment.samples.condition` sat in the prompt for a version after it was cut from the
    asked vocabulary, so the model was being taught to produce a draft `verify` is guaranteed to
    reject as `field_not_permitted`: wasted extraction, and a standing re-invitation to the misfiling
    that removing the field closed. Derive the invariant from the prompt text and PERMITTED_FIELDS
    instead of trusting a human to keep the two in step — the hand-maintained-mirror rot `fields.py`
    is entirely about.
    """
    import re

    from seqforge.harvest.extract import _INSTRUCTIONS
    from seqforge.harvest.fields import PERMITTED_FIELDS

    named = {
        tok
        for tok in re.findall(r"`([a-z_]+(?:\.[a-z_]+)+)`", _INSTRUCTIONS)
        if tok.split(".", 1)[0] in {"library", "experiment", "processing"}
    }
    assert named, "sanity: the prompt should name some fully-qualified fields"
    assert named <= set(PERMITTED_FIELDS), (
        f"prompt names fields code will reject: {sorted(named - set(PERMITTED_FIELDS))}"
    )


def test_extract_keeps_the_document_out_of_the_cached_prefix(tmp_path: Path) -> None:
    provider = _FakeProvider(json.dumps({"drafts": []}))
    extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=provider)
    assert _TEXT not in provider.captured["system"]  # volatile content must not poison the prefix
    assert _TEXT in provider.captured["user"]


# ---------- code owns the gate, provenance, offsets ----------
def test_extract_rejects_a_wrong_shape_wholesale(tmp_path: Path) -> None:
    """json-object providers do not enforce shape. Pydantic is the gate — and it fails CLOSED.

    A half-parsed batch must never yield a partially-valid assertion; the whole batch is refused.
    """
    provider = _FakeProvider(
        json.dumps({"drafts": [{"field": "library.chemistry"}]})
    )  # missing span
    with pytest.raises(ExtractUnavailable, match="does not match"):
        extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=provider)


def test_extract_rejects_non_json(tmp_path: Path) -> None:
    with pytest.raises(ExtractUnavailable):
        extract_drafts(
            _doc(tmp_path), kb.load_all_specs(), provider=_FakeProvider("I cannot help.")
        )


def test_extract_overwrites_the_models_doc_sha(tmp_path: Path) -> None:
    """We know which document we sent; the model's echo is worthless as evidence. Code wins."""
    nd = _doc(tmp_path)
    outcome = extract_drafts(
        nd, kb.load_all_specs(), provider=_FakeProvider(_batch(sha="dead" * 16))
    )
    assert outcome.drafts[0].span.doc_sha256 == nd.doc_sha256


def test_extract_discards_model_supplied_offsets(tmp_path: Path) -> None:
    nd = _doc(tmp_path)
    payload = _batch(char_start=999, char_end=1234)  # a model cannot count characters
    outcome = extract_drafts(nd, kb.load_all_specs(), provider=_FakeProvider(payload))
    assert outcome.drafts[0].span.char_start is None
    assert outcome.drafts[0].span.char_end is None


def test_extract_records_provider_in_provenance(tmp_path: Path) -> None:
    """The same prompt on a different provider is a different extractor; evals must tell them apart."""
    provider = _FakeProvider(json.dumps({"drafts": []}), model="v4-test")
    outcome = extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=provider)
    assert outcome.extractor.model_id == "fake/v4-test"
    assert outcome.extractor.prompt_version == EXTRACT_PROMPT_VERSION
    assert outcome.provider == "fake"
    assert outcome.cache_hit is True


def test_extract_carries_call_mode_and_model_for_the_cost_ledger(tmp_path: Path) -> None:
    """The outcome records HOW the call was made (thinking/effort, max_tokens, response_format) and

    which model, plus the token usage — the raw material the harvest stage writes to seqforge/usage.json
    so a reader can see what understanding the prose cost and at what effort.
    """

    class _ModeProvider(_FakeProvider):
        def complete_json(self, **kwargs: Any) -> LLMResponse:
            self.captured = kwargs
            return LLMResponse(
                text=str(self._payload),
                usage={"input_tokens": 5, "output_tokens": 7},
                mode={
                    "thinking": "adaptive",
                    "max_tokens": kwargs["max_tokens"],
                    "response_format": "json_schema",
                },
            )

    outcome = extract_drafts(
        _doc(tmp_path),
        kb.load_all_specs(),
        provider=_ModeProvider(json.dumps({"drafts": []}), model="v4-test"),
    )
    assert outcome.model == "v4-test"
    assert (
        outcome.mode["thinking"] == "adaptive" and outcome.mode["response_format"] == "json_schema"
    )
    assert outcome.usage["input_tokens"] == 5 and outcome.usage["output_tokens"] == 7


def test_extract_empty_is_a_valid_answer(tmp_path: Path) -> None:
    outcome = extract_drafts(
        _doc(tmp_path, "We sequenced some things."),
        kb.load_all_specs(),
        provider=_FakeProvider(json.dumps({"drafts": []})),
    )
    assert outcome.drafts == []


def test_extract_surfaces_provider_failure(tmp_path: Path) -> None:
    provider = _FakeProvider(ProviderUnavailable("429 rate limited"))
    with pytest.raises(ExtractUnavailable, match="429"):
        extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=provider)


# ---------- provider selection ----------
def test_resolve_provider_prefers_explicit_over_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "y")
    assert resolve_provider("anthropic").name == "anthropic"
    assert resolve_provider("deepseek").name == "deepseek"


def test_resolve_provider_auto_detects_from_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEQFORGE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "y")
    assert resolve_provider().name == "deepseek"


def test_resolve_provider_refuses_rather_than_guessing(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SEQFORGE_LLM_PROVIDER", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ProviderUnavailable, match="no LLM credential"):
        resolve_provider()
    with pytest.raises(ProviderUnavailable, match="unknown provider"):
        resolve_provider("gpt-9")


def test_provider_defaults() -> None:
    assert AnthropicProvider().default_model() == ANTHROPIC_DEFAULT_MODEL == "claude-opus-4-8"
    # V4 explicitly: deepseek-chat / -reasoner are deprecated aliases (2026-07-24) onto V4-Flash
    assert deepseek_provider(api_key="k").default_model() == DEEPSEEK_DEFAULT_MODEL
    assert DEEPSEEK_DEFAULT_MODEL.startswith("deepseek-v4")


def test_openai_compatible_provider_is_generic() -> None:
    """DeepSeek is a preset, not a special case — any OpenAI-shaped endpoint works."""
    local = OpenAICompatibleProvider(base_url="http://localhost:8000/v1", default_model="qwen3")
    assert local.default_model() == "qwen3"
    assert local.base_url == "http://localhost:8000/v1"


def test_openai_compatible_needs_a_key() -> None:
    with pytest.raises(ProviderUnavailable, match="no API key"):
        OpenAICompatibleProvider(base_url="https://api.deepseek.com", api_key=None).complete_json(
            system="s", user="u", schema={}, model="m", max_tokens=10
        )


# ---------- a DeepSeek-shaped provider, end to end into the tripwire ----------
class _FakeOpenAIClient:
    """Mimics the OpenAI SDK surface DeepSeek speaks (chat.completions.create)."""

    def __init__(self, content: str) -> None:
        payload = self

        class _Message:
            def __init__(self) -> None:
                self.content = content

        class _Choice:
            def __init__(self) -> None:
                self.message = _Message()

        class _Usage:
            prompt_tokens = 1500
            completion_tokens = 60
            prompt_cache_hit_tokens = 1024

        class _Response:
            def __init__(self) -> None:
                self.choices = [_Choice()]
                self.usage = _Usage()

        class _Completions:
            def create(self, **kwargs: Any) -> _Response:
                payload.captured = kwargs
                return _Response()

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()
        self.captured: dict[str, Any] = {}


def test_deepseek_shaped_provider_requests_json_mode_and_flows_into_verify(tmp_path: Path) -> None:
    nd = _doc(tmp_path)
    good = json.loads(_batch())
    bad = json.loads(_batch(quote="the 10x v3 protocol (Fig. 2)"))  # never appears in the document
    client = _FakeOpenAIClient(json.dumps({"drafts": good["drafts"] + bad["drafts"]}))
    provider = deepseek_provider(api_key="k", client=client)

    outcome = extract_drafts(nd, kb.load_all_specs(), provider=provider)
    sent = client.captured
    assert sent["model"] == DEEPSEEK_DEFAULT_MODEL
    assert sent["response_format"] == {"type": "json_object"}  # DeepSeek's only structured mode
    assert sent["messages"][0]["role"] == "system"
    assert outcome.usage["cache_read_tokens"] == 1024  # DeepSeek's automatic prefix caching
    assert outcome.extractor.model_id == f"deepseek/{DEEPSEEK_DEFAULT_MODEL}"

    # the tripwire does not care which model produced the drafts
    report = verify_drafts(outcome.drafts, [nd], extractor=outcome.extractor)
    assert report.n_accepted == 1
    assert report.rejected[0]["reason"] == "span_not_found"
    a = report.assertions[0]
    assert nd.text[a.span.char_start : a.span.char_end] == _QUOTE


def test_deepseek_empty_content_is_refused_not_read_as_no_findings(tmp_path: Path) -> None:
    """DeepSeek documents that json mode can return empty content. An empty batch must never be
    mistaken for 'the document states nothing' — that would silently drop real metadata."""
    provider = deepseek_provider(api_key="k", client=_FakeOpenAIClient("   "))
    with pytest.raises(ExtractUnavailable, match="empty content"):
        extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=provider)
