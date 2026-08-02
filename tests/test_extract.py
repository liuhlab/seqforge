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
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from seqforge import kb
from seqforge.harvest import (
    ANTHROPIC_DEFAULT_MODEL,
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_FLASH_MODEL,
    DEEPSEEK_MODELS,
    DEEPSEEK_PRO_MODEL,
    EXTRACT_PROMPT_VERSION,
    AnthropicProvider,
    ExtractUnavailable,
    LLMResponse,
    NormalizedDoc,
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
from seqforge.harvest.providers import classify_api_error
from seqforge.io.remote import _MAX_RETRIES
from seqforge.models.records import ArchiveRecord, ArchiveRecordSet, FreeText

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


def _doc(tmp_path: Path, text: str = _TEXT) -> NormalizedDoc:
    p = tmp_path / "methods.txt"
    p.write_text(text)
    return normalize_document(p)


def _sub(tmp_path: Path) -> Path:
    """A second directory, so a second document can keep the same basename as the first."""
    other = tmp_path / "other"
    other.mkdir(exist_ok=True)
    return other


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


# ---------- the wire schema ----------
def test_llm_schema_is_derived_from_the_canonical_model() -> None:
    schema = llm_schema()
    assert "AssertionDraft" in schema["$defs"]
    assert "quote" in schema["$defs"]["SourceSpan"]["properties"]


def test_anthropic_strict_transform_drops_unsupported_constraints() -> None:
    """Constraints live in the canonical schema (Pydantic enforces them at ingest) and
    are stripped from the LLM-facing one. The SDK performs that transform, so there is no second
    hand-maintained schema to drift — this is the CI guard on that.

    Cost is `import anthropic` (the transform itself is free); it is the only seam that proves our wire
    schema survives strict-mode transformation, so we pay it knowingly. It reaches a third-party PRIVATE
    module, so it will break on an SDK upgrade rather than on a seqforge defect."""
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


def test_the_request_says_which_document_it_is_about_and_says_it_readably(tmp_path: Path) -> None:
    """An Exchange keeps the request verbatim and nothing else about it, so the prompt's own first
    line is the only thing that ties a stored exchange back to a document. Written and read in this
    module on purpose: a reader that re-derived the format elsewhere would drift in silence, and
    what it would take with it is the eval report's ability to say which record an exchange was
    about."""
    from seqforge.harvest import document_sha256_in

    doc = _doc(tmp_path)
    provider = _FakeProvider(json.dumps({"drafts": []}))
    extract_drafts(doc, kb.load_all_specs(), provider=provider)

    assert document_sha256_in(provider.captured["user"]) == doc.doc_sha256
    assert document_sha256_in("no document line here") is None
    assert document_sha256_in(provider.captured["system"]) is None, "the prefix names no document"


# ---------- code owns the gate, provenance, offsets ----------
def test_extract_drops_one_malformed_draft_and_keeps_the_rest(tmp_path: Path) -> None:
    """A single malformed draft must NOT sink the batch (#5). Pydantic is still the gate, but per
    draft: the bad one is dropped into `rejected`, the good ones survive.

    This is the same discipline `verify` already applies — a claim whose quote will not grep back is
    dropped, not fatal — extended to the model returning `value: null` for one draft. Before the fix a
    single such draft failed validation of the whole `ExtractionResult` and blocked the dataset from
    compiling.
    """
    good = {
        "field": "library.chemistry",
        "value": "10x-3p-gex-v3",
        "span": {"doc_sha256": "0" * 64, "quote": _QUOTE, "context": None},
        "llm_confidence": 0.9,
    }
    bad = {**good, "value": None}  # the flaky token: a null value where a string is required
    missing = {"field": "library.chemistry"}  # no span at all — malformed the same way, dropped too
    provider = _FakeProvider(json.dumps({"drafts": [good, bad, missing]}))
    outcome = extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=provider)

    assert [d.value for d in outcome.drafts] == ["10x-3p-gex-v3"]  # only the good one survives
    # both malformed shapes — a null value, and a missing required span — are dropped per-draft, not
    # fatal, and land in `rejected` with the same reason (folded from the missing-field sibling test)
    assert len(outcome.rejected) == 2
    assert {r["reason"] for r in outcome.rejected} == {"malformed_draft"}
    assert all(r["field"] == "library.chemistry" for r in outcome.rejected)


def test_extract_rejects_a_broken_top_level_shape_wholesale(tmp_path: Path) -> None:
    """The salvage stops at the batch boundary: a response with no `drafts` array at all has nothing
    to keep, so it dies wholesale rather than pretending it extracted an empty batch — and the error
    names what is actually wrong with `drafts` (missing, or present-but-not-a-list)."""
    with pytest.raises(ExtractUnavailable, match="`drafts` key is missing"):
        extract_drafts(
            _doc(tmp_path),
            kb.load_all_specs(),
            provider=_FakeProvider(json.dumps({"not_drafts": 1})),
        )
    # `{"drafts": null}` must blame `drafts`, not report the useless top-level `got dict`.
    with pytest.raises(ExtractUnavailable, match="`drafts` key is a NoneType"):
        extract_drafts(
            _doc(tmp_path),
            kb.load_all_specs(),
            provider=_FakeProvider(json.dumps({"drafts": None})),
        )


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


class _SequencedProvider:
    """A provider that plays a SEQUENCE of outcomes, one per call — an exception is raised, a string
    is returned as the body. Lets a test watch the retry climb a ladder of failures."""

    name = "fake"

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.n_calls = 0

    def default_model(self) -> str:
        return "fake-model-1"

    def complete_json(self, **kwargs: Any) -> LLMResponse:
        outcome = self._outcomes[min(self.n_calls, len(self._outcomes) - 1)]
        self.n_calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return LLMResponse(text=str(outcome), usage={"input_tokens": 10})


def _transient(msg: str = "429 rate limited", **kw: Any) -> ProviderUnavailable:
    return ProviderUnavailable(msg, transient=True, **kw)


_OK = json.dumps({"drafts": []})

#: The retry policy as a TABLE — transient vs terminal, recover-on-retry vs exhaust-the-budget. Before
#: this, one 429 mid-harvest exited the whole headless run and discarded every document already
#: extracted (#138). `expected` is None when the call must succeed, else a regex the raised
#: `ExtractUnavailable` must match. `n_calls` is what the provider actually saw.
_PROVIDER_RETRY = [
    pytest.param(
        [_transient(), _OK], None, 2,
        id="a-429-backs-off-then-succeeds",
    ),
    pytest.param(
        [_transient("503 bad gateway"), _transient("503 bad gateway"), _OK], None, 3,
        id="a-run-of-5xx-keeps-retrying-inside-the-budget",
    ),
    # The trap this design exists to avoid: `ProviderUnavailable` also carries terminal conditions, so
    # a loop that retried every one of them would back off four times over a missing credential.
    pytest.param(
        [ProviderUnavailable("no API key for deepseek")], "no API key", 1,
        id="a-missing-credential-is-terminal-and-is-not-retried",
    ),
    pytest.param(
        [ProviderUnavailable("the `openai` SDK is not installed")], "not installed", 1,
        id="a-missing-sdk-is-terminal-and-is-not-retried",
    ),
    pytest.param(
        [_transient("503 bad gateway")], "503", _MAX_RETRIES + 1,
        id="a-persistent-5xx-gives-up-once-the-budget-is-spent",
    ),
]  # fmt: skip


@pytest.mark.parametrize("outcomes, expected, n_calls", _PROVIDER_RETRY)
def test_extract_retries_what_is_transient_and_gives_up_on_what_is_not(
    outcomes: list[object],
    expected: str | None,
    n_calls: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # no real wait in the test
    provider = _SequencedProvider(outcomes)

    if expected is None:
        extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=provider)
    else:
        with pytest.raises(ExtractUnavailable, match=expected):
            extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=provider)
    assert provider.n_calls == n_calls


def test_a_failed_attempt_still_counts_against_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused call burned tokens. The ledger must say what the calls cost, not what the last did."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    provider = _SequencedProvider([_transient(usage={"input_tokens": 700}), _OK])

    outcome = extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=provider)
    assert outcome.usage["input_tokens"] == 710, "the 429's 700 plus the successful call's 10"


#: Which SDK exceptions are worth another attempt. Duck-typed on `status_code` / the `Retry-After`
#: header, because both SDKs are OPTIONAL imports — naming their exception classes here would make
#: classification depend on the SDK being installed, which is the terminal case we must tell apart.
_CLASSIFY = [
    pytest.param(429, "3", (True, "3"), id="429-is-transient-and-honours-retry-after"),
    pytest.param(503, None, (True, None), id="5xx-is-transient-and-backs-off"),
    pytest.param(400, None, (False, None), id="400-is-a-verdict-not-a-blip"),
    pytest.param(401, None, (False, None), id="401-is-a-verdict-not-a-blip"),
    pytest.param(404, None, (False, None), id="404-is-a-verdict-not-a-blip"),
]


@pytest.mark.parametrize("status, retry_after, expected", _CLASSIFY)
def test_classify_reads_the_status_the_network_surface_already_decided_on(
    status: int, retry_after: str | None, expected: tuple[bool, str | None]
) -> None:
    class _Resp:
        headers = {"retry-after": retry_after} if retry_after else {}

    exc = type("APIStatusError", (Exception,), {"status_code": status, "response": _Resp()})()
    assert classify_api_error(exc) == expected


@pytest.mark.parametrize(
    "name, transient",
    [("APITimeoutError", True), ("APIConnectionError", True), ("BadRequestError", False)],
)
def test_classify_treats_a_transport_failure_as_transient(name: str, transient: bool) -> None:
    """A call that never reached a verdict is the transport twin of a 5xx — the same as `_get`."""
    assert classify_api_error(type(name, (Exception,), {})()) == (transient, None)


# ---------- provider selection ----------
#: The provider-precedence table: (explicit --provider arg, environment) -> which provider, or a
#: refusal. Explicit beats implicit; a lone credential auto-detects; no credential (and an unknown
#: name) refuses rather than guessing — a silent wrong model is a provenance bug. `expected` is a
#: provider name to select, or (exc, match) for a refusal.
_PROVIDER_SELECTION = [
    ("anthropic", {"ANTHROPIC_API_KEY": "x", "DEEPSEEK_API_KEY": "y"}, "anthropic"),
    ("deepseek", {"ANTHROPIC_API_KEY": "x", "DEEPSEEK_API_KEY": "y"}, "deepseek"),
    (None, {"DEEPSEEK_API_KEY": "y"}, "deepseek"),  # auto-detect from the one credential present
    (
        None,
        {},
        (ProviderUnavailable, "no LLM credential"),
    ),  # neither present -> refuse, don't guess
    ("gpt-9", {}, (ProviderUnavailable, "unknown provider")),  # a name we do not know -> refuse
]


@pytest.mark.parametrize(
    "explicit, env, expected",
    _PROVIDER_SELECTION,
    ids=[
        "explicit-anthropic",
        "explicit-deepseek",
        "auto-deepseek",
        "refuse-none",
        "refuse-unknown",
    ],
)
def test_resolve_provider_walks_the_precedence_table(
    explicit: str | None,
    env: dict[str, str],
    expected: str | tuple[type[Exception], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("SEQFORGE_LLM_PROVIDER", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    if isinstance(expected, str):
        assert resolve_provider(explicit).name == expected
    else:
        exc, match = expected
        with pytest.raises(exc, match=match):
            resolve_provider(explicit)


def test_provider_defaults() -> None:
    assert AnthropicProvider().default_model() == ANTHROPIC_DEFAULT_MODEL == "claude-opus-4-8"
    # V4 explicitly: deepseek-chat / -reasoner are deprecated aliases (2026-07-24) onto V4-Flash
    assert deepseek_provider(api_key="k").default_model() == DEEPSEEK_DEFAULT_MODEL
    assert DEEPSEEK_MODELS == (DEEPSEEK_FLASH_MODEL, DEEPSEEK_PRO_MODEL)
    assert all(m.startswith("deepseek-v4") for m in DEEPSEEK_MODELS)
    # Flash is the default — ~3x cheaper at the same V4 bar, and cost is the only axis the choice
    # can move: R2 re-verifies every quote whichever model proposed it.
    assert DEEPSEEK_DEFAULT_MODEL == DEEPSEEK_FLASH_MODEL == "deepseek-v4-flash"


def test_deepseek_model_catalogue_is_not_an_allowlist() -> None:
    """A name we do not list still reaches the endpoint — DeepSeek may ship one before we do."""
    client = _FakeOpenAIClient('{"drafts": []}')
    provider = deepseek_provider(api_key="k", client=client)
    provider.complete_json(
        system="s", user="u", schema={}, model="deepseek-v9-unreleased", max_tokens=64
    )
    assert client.captured["model"] == "deepseek-v9-unreleased"


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


class _SequencedOpenAIClient:
    """An OpenAI-shaped client that returns a SEQUENCE of contents, one per create() call — so a test
    can present empty bodies followed by a good one and watch the provider retry (#4)."""

    def __init__(self, contents: list[str]) -> None:
        self._contents = list(contents)
        self.n_calls = 0
        outer = self

        class _Message:
            def __init__(self, content: str) -> None:
                self.content = content

        class _Choice:
            def __init__(self, content: str) -> None:
                self.message = _Message(content)

        class _Usage:
            prompt_tokens = 1500
            completion_tokens = 60
            prompt_cache_hit_tokens = 1024

        class _Response:
            def __init__(self, content: str) -> None:
                self.choices = [_Choice(content)]
                self.usage = _Usage()

        class _Completions:
            def create(self, **kwargs: Any) -> _Response:
                i = outer.n_calls
                outer.n_calls += 1
                content = outer._contents[i] if i < len(outer._contents) else ""
                return _Response(content)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def test_deepseek_retries_past_empty_content_then_succeeds(tmp_path: Path) -> None:
    """#4: json_object mode intermittently returns an empty body. That is a provider hiccup, not the
    document saying nothing, so the provider re-issues the request rather than aborting the harvest."""
    nd = _doc(tmp_path)
    client = _SequencedOpenAIClient(
        ["", "   ", json.dumps({"drafts": []})]
    )  # two empties, then good
    provider = deepseek_provider(api_key="k", client=client)

    outcome = extract_drafts(nd, kb.load_all_specs(), provider=provider)
    assert client.n_calls == 3, (
        "it retried through the two empty bodies and stopped at the good one"
    )
    assert outcome.drafts == []
    # usage is summed over ALL three attempts — the two empty ones still cost tokens (1024 cache-read
    # tokens each in the fake), so the ledger must not undercount them
    assert outcome.usage["cache_read_tokens"] == 3 * 1024


def test_deepseek_gives_up_after_the_retry_budget_of_empty_bodies(tmp_path: Path) -> None:
    """Bounded: a model that ALWAYS returns empty content still fails loudly, it does not spin."""
    nd = _doc(tmp_path)
    client = _SequencedOpenAIClient([""] * 12)  # never a body
    provider = deepseek_provider(api_key="k", client=client)

    with pytest.raises(ExtractUnavailable, match="empty content"):
        extract_drafts(nd, kb.load_all_specs(), provider=provider)
    # One budget now, shared with the transient-API case, instead of a second one nested in the
    # provider. An empty body carries `retry_after="0"`, so these re-issue at once — the backoff is
    # for an endpoint asking for room, and an empty body asks for nothing.
    assert client.n_calls == _MAX_RETRIES + 1


class _SequencedAnthropicClient:
    """An Anthropic-shaped client playing a sequence of turns. A turn is the text block it returns;
    `None` is a thinking-only turn, which carries no text block at all."""

    def __init__(self, turns: list[str | None]) -> None:
        self._turns = list(turns)
        self.n_calls = 0
        outer = self

        class _Block:
            def __init__(self, text: str) -> None:
                self.type, self.text = "text", text

        class _Usage:
            input_tokens, output_tokens = 1500, 60
            cache_read_input_tokens, cache_creation_input_tokens = 1024, 0

        class _Response:
            def __init__(self, text: str | None) -> None:
                # a thinking-only turn has a thinking block and NO text block
                self.content = [_Block(text)] if text is not None else []
                self.usage = _Usage()

        class _Messages:
            def create(self, **kwargs: Any) -> _Response:
                i = outer.n_calls
                outer.n_calls += 1
                return _Response(outer._turns[i] if i < len(outer._turns) else None)

        self.messages = _Messages()


def test_anthropic_retries_a_thinking_only_turn_rather_than_calling_it_bad_json(
    tmp_path: Path,
) -> None:
    """The empty-body recovery was written per-provider, so only the json-object path had it (#138).

    A thinking-only turn leaves no text block. Left as `text=""` it surfaced one step later as
    "returned output that is not valid JSON" — a shape complaint about a response that had no shape.
    """
    client = _SequencedAnthropicClient([None, "", json.dumps({"drafts": []})])
    provider = AnthropicProvider(client=client)

    outcome = extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=provider)
    assert client.n_calls == 3, "it retried through the empty turns and stopped at the good one"
    assert outcome.drafts == []
    assert outcome.usage["cache_read_tokens"] == 3 * 1024, "every attempt cost tokens"


def test_anthropic_gives_up_on_empty_turns_once_the_budget_is_spent(tmp_path: Path) -> None:
    """Bounded on this provider too: never a spin, always a loud `llm_unavailable`."""
    client = _SequencedAnthropicClient([None] * 12)
    provider = AnthropicProvider(client=client)

    with pytest.raises(ExtractUnavailable, match="no text block"):
        extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=provider)
    assert client.n_calls == _MAX_RETRIES + 1


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

    # the tripwire does not care which model produced the drafts: the good draft is accepted, the
    # fabricated quote is rejected. (The offset re-check that a quote greps back is owned by
    # test_harvest.py, not re-proved here.)
    report = verify_drafts(outcome.drafts, [nd], extractor=outcome.extractor)
    assert report.n_accepted == 1
    assert report.rejected[0]["reason"] == "span_not_found"


# ---------- the meter at the provider seam ----------
# One module counts, refuses and records; the three adapters stay untouched. These prove the three
# jobs separately, and prove that doing them does not change what the caller gets back.


def _meter(outcomes: list[object], **kwargs: Any) -> Any:
    """A `TokenMeter` over a `_SequencedProvider`, so a test can watch retries reach the meter."""
    from seqforge.harvest import TokenMeter

    return TokenMeter(_SequencedProvider(outcomes), **kwargs)


def test_the_meter_proxies_the_wrapped_providers_identity(tmp_path: Path) -> None:
    """`ExtractorProvenance.model_id` is `provider/model`, so a meter that named itself would
    restamp every assertion in a corpus with a provider that does not exist."""
    from seqforge.harvest import TokenMeter

    inner = deepseek_provider(api_key="k", client=_FakeOpenAIClient(_batch()))
    meter = TokenMeter(inner)

    assert meter.name == "deepseek"
    assert meter.default_model() == DEEPSEEK_DEFAULT_MODEL
    assert meter.base_url == inner.base_url, "anything else falls through to the provider"

    outcome = extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=meter)
    assert outcome.extractor.model_id == f"deepseek/{DEEPSEEK_DEFAULT_MODEL}"
    assert outcome.provider == "deepseek"


@pytest.mark.parametrize("outcomes, expected, n_calls", _PROVIDER_RETRY)
def test_the_meter_counts_real_requests_not_documents(
    outcomes: list[object],
    expected: str | None,
    n_calls: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`n_calls` was `len(documents)`, so up to three retries per document cost tokens nothing
    counted. Metered, it is the retry table's own count — one exchange per request, failures
    included, over ONE document in every row."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    meter = _meter(outcomes)

    if expected is None:
        extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=meter)
    else:
        with pytest.raises(ExtractUnavailable, match=expected):
            extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=meter)
    assert meter.n_exchanges == n_calls


def test_the_meter_banks_what_a_refused_attempt_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    meter = _meter([_transient(usage={"input_tokens": 700}), _OK])

    extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=meter)
    assert meter.usage()["input_tokens"] == 710, "the 429's 700 plus the successful call's 10"
    assert meter.n_exchanges == 2


def test_the_meter_hands_the_response_back_untouched(tmp_path: Path) -> None:
    """It measures; it never reads the text for meaning. Repairing or partly accepting a batch stays
    the forbidden act, and a meter that rewrote a response would be doing it."""
    from seqforge.harvest import TokenMeter

    inner = _FakeProvider(_batch())
    unmetered = inner.complete_json(system="s", user="u", schema={}, model="m", max_tokens=8)
    metered = TokenMeter(_FakeProvider(_batch())).complete_json(
        system="s", user="u", schema={}, model="m", max_tokens=8
    )
    assert metered == unmetered


# ---------- the Ceiling ----------
def test_the_ceiling_refuses_the_request_after_the_one_that_crossed_it(tmp_path: Path) -> None:
    """The crossing request is ISSUED — its cost is not knowable until it returns — and everything
    admitted after it is refused, un-issued. That is what makes a breach reproducible."""
    from seqforge.harvest import CeilingExceeded, TokenMeter

    inner = _SequencedProvider([_OK])  # 10 input tokens per call
    meter = TokenMeter(inner, ceiling=25)

    def one() -> None:
        meter.complete_json(system="s", user="u", schema={}, model="m", max_tokens=8)

    for _ in range(3):  # 10, 20, 30 -> the third is the one that crosses
        one()
    with pytest.raises(CeilingExceeded) as caught:
        one()

    assert inner.n_calls == 3, "the refused request never reached the provider"
    assert meter.n_exchanges == 3 and meter.tokens == 30
    assert caught.value.spent == 30 and caught.value.ceiling == 25


def test_the_ceiling_counts_raw_so_cached_input_is_not_free() -> None:
    """Decided and written down: fresh input, cached input, cache writes and output ALL count. A
    ceiling is a backstop, not a price — and `input_tokens` is normalized to be the whole input on
    both providers, so the reading does not depend on who answered."""
    from seqforge.harvest import raw_tokens

    deepseek = {"input_tokens": 3000, "output_tokens": 300, "cache_read_tokens": 2800}
    anthropic = {
        "input_tokens": 3000,
        "output_tokens": 300,
        "cache_read_tokens": 2500,
        "cache_write_tokens": 300,
    }
    assert raw_tokens(deepseek) == raw_tokens(anthropic) == 3300


def test_a_ceiling_breach_is_a_blocker_with_an_operable_remedy() -> None:
    """Exit 3 with a refusal a caller can act on — never a warning, and never `llm_unavailable`."""
    from seqforge.harvest import CeilingExceeded
    from seqforge.models.blocker import BlockerCode

    blocker = CeilingExceeded(
        ceiling=500_000, spent=512_000, n_exchanges=180, subject="GSE126954"
    ).blocker()

    assert blocker.code is BlockerCode.TOKEN_CEILING_EXCEEDED
    assert blocker.subject.kind == "dataset" and blocker.subject.ref == "GSE126954"
    assert "--ceiling" in blocker.remedy, "a remedy that names no command is not finished"
    assert "500,000" in blocker.message and "180" in blocker.message


def test_no_ceiling_never_refuses() -> None:
    """`None` and `0` both mean no ceiling, so a CLI default of 0 needs no special case."""
    from seqforge.harvest import TokenMeter

    for ceiling in (None, 0):
        meter = TokenMeter(_SequencedProvider([_OK]), ceiling=ceiling)
        for _ in range(5):
            meter.complete_json(system="s", user="u", schema={}, model="m", max_tokens=8)
        assert meter.n_exchanges == 5 and meter.ceiling is None


def test_the_meter_holds_under_the_concurrent_fan_out() -> None:
    """Extraction fans out over threads — a pool per document under a pool per case — so the running
    total is shared mutable state. Unlocked, `+=` on it loses increments, and the ceiling it guards
    reads low exactly when the run is widest."""
    from concurrent.futures import ThreadPoolExecutor

    from seqforge.harvest import CeilingExceeded, TokenMeter

    meter = TokenMeter(_SequencedProvider([_OK]), ceiling=200)  # 10 input tokens per call

    def one(_i: int) -> bool:
        try:
            meter.complete_json(system="s", user="u", schema={}, model="m", max_tokens=8)
        except CeilingExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        issued = sum(pool.map(one, range(200)))

    assert meter.n_exchanges == issued, "every issued request was banked exactly once"
    assert meter.tokens == 10 * issued
    # The in-flight ones finish and are banked, so the total may overshoot by at most the width of
    # the pool; nothing may be admitted once the ceiling is reached.
    assert 200 <= meter.tokens <= 200 + 10 * 8


# ---------- the transcript ----------
def test_the_transcript_is_one_prompt_plus_a_pair_per_exchange(tmp_path: Path) -> None:
    """983 exchanges share one byte-identical system prompt (that is why prefix caching works), so
    a transcript stores it ONCE and every exchange points at it."""
    from seqforge.harvest import TokenMeter

    meter = TokenMeter(_SequencedProvider([_OK]))
    docs = []
    for i in range(4):
        (tmp_path / f"d{i}").mkdir()
        docs.append(_doc(tmp_path / f"d{i}", f"We used protocol number {i}."))
    for doc in docs:
        extract_drafts(doc, kb.load_all_specs(), provider=meter)

    transcript = meter.transcript()
    assert transcript.n_exchanges == 4
    assert len(transcript.prompts) == 1, "the stable prefix is stored once, not four times"
    system = transcript.prompt_for(transcript.exchanges[0])
    assert system == build_system_prompt(kb.load_all_specs(), llm_schema())
    # the volatile half is per document, and it is the document that varies
    assert [d.text in e.user for d, e in zip(docs, transcript.exchanges, strict=True)] == [True] * 4
    assert all(e.ok and e.text == _OK for e in transcript.exchanges)


def test_a_refused_exchange_is_still_in_the_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry is its own exchange, because tokens were spent on it. A transcript that dropped the
    failures would disagree with the count beside it."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    meter = _meter([_transient("429 rate limited", usage={"input_tokens": 700}), _OK])

    extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=meter)
    transcript = meter.transcript()

    assert transcript.n_exchanges == 2
    failed, succeeded = transcript.exchanges
    assert not failed.ok and "429" in (failed.error or "") and failed.text == ""
    assert failed.tokens == 700 and succeeded.tokens == 10
    assert succeeded.ok
    # json-safe, and the failure says so rather than reading as an empty response
    assert "error" in failed.to_json() and "error" not in succeeded.to_json()
    assert transcript.to_json()["prompts"] == transcript.prompts


# ---------- the plan: records -> documents, before a token is spent ----------
# One module owns the fan-out both `harvest extract` and the eval harness used to spell for
# themselves. What it must get right is the count (a call per run was 92% of a corpus's bill), the
# reassembly (a dict keyed by document identity loses one of two identical documents), and where a
# collapsed document's claims land (a subject that maps to no sample is silently discarded).


def _records(runs_per_sample: dict[str, int], *, alias: str = "N2_wild_type") -> ArchiveRecordSet:
    """A record set shaped like a real one: project -> sample -> experiment -> runs."""
    records = [
        ArchiveRecord(
            level="project",
            accession="PRJNA1",
            free_text=[FreeText(label="study_abstract", text="Wild-type and daf-2 mutants.")],
        )
    ]
    for sample, n_runs in runs_per_sample.items():
        experiment = f"SRX{sample[-1]}"
        records += [
            ArchiveRecord(
                level="sample",
                accession=sample,
                parent="PRJNA1",
                free_text=[FreeText(label="sample_alias", text=f"{sample} whole worm")],
            ),
            ArchiveRecord(
                level="experiment",
                accession=experiment,
                parent=sample,
                free_text=[FreeText(label="design", text="Chromium Single Cell 3' v3.")],
            ),
        ]
        records += [
            ArchiveRecord(
                level="run",
                accession=f"SRR{sample[-1]}{i}",
                parent=experiment,
                free_text=[FreeText(label="run_alias", text=f"{alias}_r{i}")],
            )
            for i in range(n_runs)
        ]
    return ArchiveRecordSet(source="test", query="PRJNA1", records=records)


def test_the_runs_of_one_sample_are_one_document_not_one_each() -> None:
    """The actual fix, and the ceiling is only a backstop.

    A run alias is often the only place a WT-vs-mutant contrast is written, so the runs are still
    READ — the defect was one call per run, not reading runs. Twelve one-line aliases belonging to
    one sample are one document; two samples are two.
    """
    from seqforge.harvest import plan_extraction

    plan = plan_extraction(records=_records({"SAMN1": 12, "SAMN2": 4}))

    by_scope = Counter(d.scope for d in plan.documents)
    assert by_scope == {"sample": 2, "experiment": 2, "run": 2}, "one run document per SAMPLE"
    assert "project" not in by_scope, "a project record is asked nothing, so it costs no call"
    assert plan.n_records_read == 2 + 2 + 16
    assert plan.n_records_collapsed == 14, "16 runs, 2 documents"

    collapsed = next(d for d in plan.documents if d.scope == "run")
    assert collapsed.text.count("run_alias") == 12, "every alias is still in the document"
    assert plan.members[collapsed.doc_sha256] == tuple(f"SRR1{i}" for i in range(12))


def test_a_collapsed_run_document_speaks_for_its_sample_not_for_one_run() -> None:
    """Where the collapse is placed is the whole risk, and it fails silently when it is wrong.

    `resolve/records.py` keeps a claim only when the document's subject maps to the sample being
    resolved, and DROPS it otherwise with nothing said. `_subject_to_sample` maps a sample accession
    to itself, so a document carrying the sample's accession resolves as `asserted` — while pointing
    it at one of its twelve runs would attribute the other eleven aliases to a run they did not come
    from. That the claims survive the join is proved end to end in `tests/test_records.py`.
    """
    from seqforge.harvest import plan_extraction

    plan = plan_extraction(records=_records({"SAMN1": 3, "SAMN2": 3}))
    collapsed = [d for d in plan.documents if d.scope == "run"]

    assert {d.subject for d in collapsed} == {"SAMN1", "SAMN2"}
    assert {d.source_basename for d in collapsed} == {"runs-SAMN1.txt", "runs-SAMN2.txt"}


def test_a_run_document_is_asked_what_an_alias_can_answer() -> None:
    """Scope stays `run` because that is what the prose IS, and the ask follows the scope."""
    from seqforge.harvest import plan_extraction
    from seqforge.harvest.fields import ASKED_SAMPLE_ATTRIBUTES

    plan = plan_extraction(records=_records({"SAMN1": 2}))
    collapsed = next(d for d in plan.documents if d.scope == "run")

    assert plan.asked(collapsed) == tuple(
        f"experiment.samples.{a}" for a in ASKED_SAMPLE_ATTRIBUTES
    )
    assert "library.chemistry" not in plan.asked(collapsed)


def test_a_lone_run_is_rendered_exactly_as_its_own_record() -> None:
    """Collapsing one run must not invent a second rendering of it: a quote is only checkable while
    the exact bytes it was greppedded against can be regenerated from the record."""
    from seqforge.harvest import normalize_record, plan_extraction

    records = _records({"SAMN1": 1})
    plan = plan_extraction(records=records)
    run = next(r for r in records.records if r.level == "run")

    doc = next(d for d in plan.documents if d.scope == "run")
    assert doc.doc_sha256 == normalize_record(run).doc_sha256
    assert doc.subject == run.accession, "no sample to speak for; it speaks for itself"


def test_a_run_whose_sample_is_missing_keeps_its_own_identity() -> None:
    """Folding an orphan run in with unrelated runs would be inventing a join the archive did not
    declare — and `_basis_for` would then place its claims on somebody else's sample."""
    from seqforge.harvest import plan_extraction

    records = ArchiveRecordSet(
        source="test",
        query="PRJNA1",
        records=[
            ArchiveRecord(
                level="run",
                accession=f"SRR{i}",
                free_text=[FreeText(label="run_alias", text=f"orphan_{i}")],
            )
            for i in range(3)
        ],
    )
    plan = plan_extraction(records=records)

    assert {d.subject for d in plan.documents} == {"SRR0", "SRR1", "SRR2"}
    assert plan.n_records_collapsed == 0


def test_a_record_with_nothing_to_read_or_nothing_to_ask_costs_no_call() -> None:
    from seqforge.harvest import plan_extraction

    records = ArchiveRecordSet(
        source="test",
        query="PRJNA1",
        records=[
            ArchiveRecord(level="project", accession="PRJNA1"),
            ArchiveRecord(level="sample", accession="SAMN1", parent="PRJNA1"),  # no prose
            ArchiveRecord(
                level="run",
                accession="SRR1",
                parent="SAMN1",
                free_text=[FreeText(label="run_alias", text="   ")],  # whitespace is not prose
            ),
        ],
    )
    plan = plan_extraction(records=records)

    assert plan.n_documents == 0 and plan.n_records_read == 0
    assert plan.estimated_input_tokens == 0, "no call, no stable prefix to pay for"


def test_the_plan_charges_the_stable_prefix_once_per_document(tmp_path: Path) -> None:
    """The prefix is ~3 KB and byte-identical on every request, so N documents pay it N times. That
    is the arithmetic that makes a fan-out over one-line aliases expensive, and a plan that only
    counted the documents' own text would hide it."""
    from seqforge.harvest import plan_extraction

    prefix = len(build_system_prompt(kb.load_all_specs(), llm_schema()))
    plan = plan_extraction(
        documents=[_doc(tmp_path)], records=_records({"SAMN1": 4}), system_prompt_chars=prefix
    )

    assert plan.n_documents == 4  # the paper, a sample, an experiment, one collapsed run document
    assert plan.n_chars == sum(len(d.text) for d in plan.documents)
    assert plan.estimated_input_tokens == (4 * prefix + plan.n_chars) // 4
    assert plan.estimated_input_tokens > 4 * prefix // 4


def test_the_plan_is_free_and_the_dry_run_is_the_list_the_paid_run_sends(tmp_path: Path) -> None:
    """A dry run is exact rather than a projection: it renders the same documents, and rendering
    costs no token and no network."""
    from seqforge.harvest import plan_extraction

    plan = plan_extraction(documents=[_doc(tmp_path)], records=_records({"SAMN1": 2}))
    report = plan.report()

    assert report.n_documents == plan.n_documents == len(report.documents)
    assert [d.doc_sha256 for d in report.documents] == [d.doc_sha256 for d in plan.documents]
    collapsed = next(d for d in report.documents if d.scope == "run")
    assert collapsed.members == ["SRR10", "SRR11"], "the collapse is visible, not implicit"
    assert report.model_dump(mode="json")["n_records_collapsed"] == 1


def test_two_identical_asks_are_one_document_and_one_result(tmp_path: Path) -> None:
    """The reassembly bug, pinned.

    `cli/harvest.py` keyed its outcomes by `doc_sha256`: two documents that render identically each
    cost a call, one result survived, and the loop then read that one outcome once per colliding
    document — duplicating its drafts, its rejected list and its usage. So the plan folds an
    identical ask into one document, and the fan-out returns a LIST in plan order with no key to
    collide on.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    doc = _doc(tmp_path)
    plan = plan_extraction(documents=[doc, doc, doc])
    assert plan.n_documents == 1

    provider = _FakeProvider(_batch())
    outcomes = extract_planned(plan, kb.load_all_specs(), provider=provider)
    assert len(outcomes) == 1
    assert sum(len(o.drafts) for o in outcomes) == 1, "one document, one document's worth of drafts"


def test_the_same_document_under_two_roles_is_two_asks(tmp_path: Path) -> None:
    """Identity is the whole question the call would put, not just the bytes: a reference document is
    never asked about `processing.*`, so the same file offered under both flags is two calls."""
    from dataclasses import replace as _replace

    from seqforge.harvest import plan_extraction

    reference = _doc(tmp_path)
    instruction = _replace(reference, role="instruction")
    plan = plan_extraction(documents=[reference, instruction])

    assert plan.n_documents == 2
    assert "processing.quantification" in plan.asked(instruction)
    assert "processing.quantification" not in plan.asked(reference)


def test_the_fan_out_returns_outcomes_in_plan_order(tmp_path: Path) -> None:
    """Positional, and required rather than tidy: `verify_drafts` and the last-wins field map both
    read the drafts list positionally, so a completion-ordered result would make a run's graded
    assertions depend on which socket returned first."""
    from seqforge.harvest import extract_planned, plan_extraction

    plan = plan_extraction(records=_records({"SAMN1": 2, "SAMN2": 2}))
    assert plan.n_documents == 6

    class _FinishesOutOfOrder:
        """The first document submitted is the last to answer."""

        name = "echo"

        def default_model(self) -> str:
            return "echo-1"

        def complete_json(self, **kwargs: Any) -> LLMResponse:
            time.sleep(0.05 if plan.documents[0].text in str(kwargs["user"]) else 0.0)
            return LLMResponse(text=_batch(quote=_QUOTE), usage={"input_tokens": 1})

    outcomes = extract_planned(plan, kb.load_all_specs(), provider=_FinishesOutOfOrder())

    # every draft is anchored by code onto the document that was actually sent, so the shas ARE the
    # order the calls were made in
    assert [o.drafts[0].span.doc_sha256 for o in outcomes] == [d.doc_sha256 for d in plan.documents]


class _OneDocumentFails:
    """Answers every document except the one whose text holds ``poison``, which comes back unusable.

    Modelled on the failure that produced this seam: DeepSeek's empty/invalid ``json_object`` (#4),
    which lands on whichever document provokes a long response rather than on the longest document.
    """

    name = "flaky"

    def __init__(self, poison: str) -> None:
        self._poison = poison
        self.n_calls = 0

    def default_model(self) -> str:
        return "flaky-1"

    def complete_json(self, **kwargs: Any) -> LLMResponse:
        self.n_calls += 1
        if self._poison in str(kwargs["user"]):
            return LLMResponse(text="not json at all", usage={"input_tokens": 3})
        return LLMResponse(text=_batch(quote=_QUOTE), usage={"input_tokens": 1})


def test_one_documents_abort_takes_down_the_whole_plan_by_default(tmp_path: Path) -> None:
    """The compiler fails closed, and that stays true.

    An extraction missing a document produces a manifest silently short a fact, and nothing
    downstream can tell it from a complete one — so the default is still to raise.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    plan = plan_extraction(documents=[_doc(tmp_path), _doc(_sub(tmp_path), "A poison table.")])
    assert plan.n_documents == 2

    with pytest.raises(ExtractUnavailable):
        extract_planned(plan, kb.load_all_specs(), provider=_OneDocumentFails("poison"))


def test_under_partial_one_documents_abort_costs_only_that_document(tmp_path: Path) -> None:
    """The harness's opposite need, and the sharpest half of #182.

    Five of seven prose-carrying benchmark cases measured **nothing** because one document each
    raised through the whole case — and the aborts landed on a 1 KB supplementary table, not on the
    whole paper. A report saying "the other documents extracted these claims, this one never
    answered" is strictly more informative than one saying nothing.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    good, bad = _doc(tmp_path), _doc(_sub(tmp_path), "A poison table.")
    plan = plan_extraction(documents=[good, bad])

    outcomes = extract_planned(
        plan, kb.load_all_specs(), provider=_OneDocumentFails("poison"), partial=True
    )

    assert len(outcomes) == 2, "still positionally aligned with the plan"
    by_doc = dict(zip(plan.documents, outcomes, strict=True))
    assert by_doc[good].answered and by_doc[good].drafts, "the good document still extracted"
    survivor = by_doc[bad]
    assert not survivor.answered
    assert survivor.drafts == []
    assert survivor.failure is not None and "not valid JSON" in survivor.failure
    # ...and it still names who did not answer, because "nothing came back" is a fact about an
    # extractor and a report that cannot say which one is unreadable a week later.
    assert survivor.extractor.model_id == "flaky/flaky-1"


def test_an_empty_batch_is_an_answer_and_a_failure_is_not(tmp_path: Path) -> None:
    """The distinction the whole ticket rests on: checked and found nothing, versus could not check.

    Both produce zero drafts. Folding them together is what let a stage that never ran report the
    same way as one that read the document honestly and had nothing to say about it.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    plan = plan_extraction(documents=[_doc(tmp_path, "We sequenced some things.")])
    (silent,) = extract_planned(
        plan, kb.load_all_specs(), provider=_FakeProvider(json.dumps({"drafts": []})), partial=True
    )
    assert silent.answered and silent.drafts == [] and silent.failure is None


def test_partial_does_not_swallow_a_ceiling(tmp_path: Path) -> None:
    """A Ceiling is a refusal, not an unavailability — the provider answered everything it was given.

    It owes the caller a ``Blocker`` and exit 3, so it must reach one however the fan-out was asked
    to treat a failed document.
    """
    from seqforge.harvest import CeilingExceeded, TokenMeter, extract_planned, plan_extraction

    plan = plan_extraction(documents=[_doc(tmp_path)])
    meter = TokenMeter(_FakeProvider(_batch()), ceiling=1)
    # Bank a request's worth first, so the ceiling is already reached when the fan-out asks to be
    # admitted — the check is on the total banked so far, not on a guess about the next request.
    meter.complete_json(system="s", user="u", schema={}, model="m", max_tokens=8)

    with pytest.raises(CeilingExceeded):
        extract_planned(plan, kb.load_all_specs(), provider=meter, partial=True)


def test_the_fan_out_bounds_what_the_provider_sees_however_the_pools_nest() -> None:
    """Case-level and document-level concurrency MULTIPLY, and the provider sees the product. The
    semaphore, not the pool size, is what bounds it — a pool per document under a pool per case is
    14 x 24 requests in flight from one key, which measures a rate limiter and not the compiler."""
    from seqforge.evals.run import MAX_DEFAULT_JOBS
    from seqforge.harvest import MAX_IN_FLIGHT, plan

    assert MAX_IN_FLIGHT <= MAX_DEFAULT_JOBS
    assert plan._SLOTS._value == MAX_IN_FLIGHT, "sized once at import, shared by every pool"


# ---------- the transcript's address on disk ----------
def test_a_transcript_round_trips_through_its_file(tmp_path: Path) -> None:
    """One writer, one reader, one format. A writer with no reader is a shape every consumer has to
    re-derive from the code that produced it — and the report renderer is the consumer."""
    from seqforge.harvest import TokenMeter, read_transcript, write_transcript

    monkey = _SequencedProvider([_transient("429 rate limited", usage={"input_tokens": 700}), _OK])
    meter = TokenMeter(monkey)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(time, "sleep", lambda _s: None)
        extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=meter)

    path = write_transcript(tmp_path / "transcript.jsonl", meter.transcript())
    back = read_transcript(path)

    assert back.to_json() == meter.transcript().to_json()
    assert back.n_exchanges == 2
    assert not back.exchanges[0].ok and "429" in (back.exchanges[0].error or "")
    assert back.prompt_for(back.exchanges[1]) == build_system_prompt(
        kb.load_all_specs(), llm_schema()
    )


def test_the_transcript_file_holds_one_prompt_and_a_line_per_exchange(tmp_path: Path) -> None:
    """The header carries the prompts; every line after it is one exchange pointing at its sha. At
    983 exchanges the alternative is three megabytes of one repeated string, and a file nobody
    opens — and it is what makes the file streamable rather than a tree to parse whole."""
    from seqforge.harvest import TokenMeter, write_transcript

    meter = TokenMeter(_SequencedProvider([_OK, _OK, _OK]))
    for i in range(3):
        (tmp_path / f"d{i}").mkdir()
        extract_drafts(
            _doc(tmp_path / f"d{i}", f"We used protocol {i}."),
            kb.load_all_specs(),
            provider=meter,
        )

    lines = write_transcript(tmp_path / "t.jsonl", meter.transcript()).read_text().splitlines()

    assert len(lines) == 4, "one header plus one line per exchange"
    header = json.loads(lines[0])
    assert list(header["prompts"]) == [json.loads(lines[1])["prompt_sha256"]]
    assert header["n_exchanges"] == 3
    assert all("prompts" not in json.loads(line) for line in lines[1:])
