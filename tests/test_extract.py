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
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from seqforge import kb
from seqforge.harvest import (
    ANTHROPIC_DEFAULT_MODEL,
    DEEPSEEK_DEFAULT_MODEL,
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
from seqforge.models.assertion import ExtractorProvenance, SourceSpan
from seqforge.models.records import ArchiveRecord, ArchiveRecordSet, FreeText, RecordLevel

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


_GOOD: dict[str, Any] = json.loads(_batch())["drafts"][0]
_BAD = {**_GOOD, "value": None}  # the flaky token: a null value where a string is required


# ---------- the wire schema ----------
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
    once = build_kb_context(kb.load_all_specs())
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
    missing = {"field": "library.chemistry"}  # no span at all — malformed the same way, dropped too
    provider = _FakeProvider(json.dumps({"drafts": [_GOOD, _BAD, missing]}))
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


@pytest.mark.parametrize(
    "drafts, values, n_rejected",
    [
        pytest.param([_GOOD], ["10x-3p-gex-v3"], 0, id="a-batch"),
        pytest.param([_GOOD, _BAD, _GOOD], ["10x-3p-gex-v3"] * 2, 1, id="a-malformed-member"),
        pytest.param([], [], 0, id="a-document-that-supports-nothing"),
    ],
)
def test_a_bare_top_level_array_is_the_same_batch_under_a_different_envelope(
    drafts: list[dict[str, Any]], values: list[str], n_rejected: int, tmp_path: Path
) -> None:
    """A model that returns the drafts array without wrapping it has still returned the batch.

    The elements are byte-identical either way and each one still faces
    `AssertionDraft.model_validate` alone, so refusing the document over the absent key would
    discard a whole document's worth of valid extraction for one missing token — the same defect
    `test_extract_drops_one_malformed_draft_and_keeps_the_rest` closed one layer down, and measurably
    live on the weaker json-object models (#190). Unwrapping changes the envelope and nothing else:
    the per-draft gate is still the gate, and `[]` is still the common, correct answer that an
    envelope may not turn into a lost document.
    """
    doc = _doc(tmp_path)
    wrapped = extract_drafts(
        doc, kb.load_all_specs(), provider=_FakeProvider(json.dumps({"drafts": drafts}))
    )
    bare = extract_drafts(doc, kb.load_all_specs(), provider=_FakeProvider(json.dumps(drafts)))

    assert [d.value for d in bare.drafts] == [d.value for d in wrapped.drafts] == values
    assert len(bare.rejected) == len(wrapped.rejected) == n_rejected
    assert bare.drafts == wrapped.drafts and bare.rejected == wrapped.rejected
    assert bare.answered and wrapped.answered and bare.failure is None


@pytest.mark.parametrize(
    "payload, named", [('"drafts"', "str"), ("7", "int"), ("null", "NoneType")]
)
def test_every_other_top_level_shape_still_dies_wholesale(
    tmp_path: Path, payload: str, named: str
) -> None:
    """The array is the ONE extra envelope. A scalar carries no batch to keep, so it stays fatal —
    and the refusal names what came back plus both shapes that would have been read, which the old
    wording ("not a JSON object") can no longer say now that an object is not the only one."""
    with pytest.raises(ExtractUnavailable) as caught:
        extract_drafts(_doc(tmp_path), kb.load_all_specs(), provider=_FakeProvider(payload))

    message = str(caught.value)
    assert f"top-level {named}" in message
    assert "`drafts`" in message and "array of drafts" in message


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
    assert all(m.startswith("deepseek-v4") for m in DEEPSEEK_MODELS)
    # Pro is the default. Flash was, on the argument that it is the cheap end of V4 — and the same
    # benchmark that argument was made for falsified it (#188): pro is faster AND spends fewer output
    # tokens. Cost is still the only axis a model can move (R2 re-greps every quote whichever one
    # proposed it); flash just loses that axis too. The run is written up in `evals/README.md`.
    assert DEEPSEEK_DEFAULT_MODEL == DEEPSEEK_PRO_MODEL == "deepseek-v4-pro"


def test_deepseek_model_catalogue_is_not_an_allowlist() -> None:
    """A name we do not list still reaches the endpoint — DeepSeek may ship one before we do."""
    client = _FakeOpenAIClient('{"drafts": []}')
    provider = deepseek_provider(api_key="k", client=client)
    provider.complete_json(
        system="s", user="u", schema={}, model="deepseek-v9-unreleased", max_tokens=64
    )
    assert client.captured["model"] == "deepseek-v9-unreleased"


def test_openai_compatible_needs_a_key() -> None:
    """DeepSeek is a preset, not a special case — any OpenAI-shaped endpoint reaches the same gate."""
    with pytest.raises(ProviderUnavailable, match="no API key"):
        OpenAICompatibleProvider(base_url="http://localhost:8000/v1", api_key=None).complete_json(
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
#: A prompt whose ESTIMATE is exactly 1000 tokens. The meter deducts a request's estimated cost from
#: the budget before it issues it, and that estimate is the prompt's characters over
#: `CHARS_PER_TOKEN` — so fixing the character count fixes the reservation, and the ceilings below
#: read as a whole number of requests.
_SYSTEM_1K = "s" * 3600
_USER_1K = "u" * 400


class _CostingProvider:
    """Always answers, always costs the same. Enough to reach a ceiling, from any number of threads."""

    name = "costing"

    def __init__(self, per_call: int = 1000) -> None:
        self.per_call = per_call
        self.n_calls = 0
        self._lock = threading.Lock()

    def default_model(self) -> str:
        return "costing-1"

    def complete_json(self, **kwargs: Any) -> LLMResponse:
        with self._lock:
            self.n_calls += 1
        return LLMResponse(text=_OK, usage={"input_tokens": self.per_call})


def _ask(meter: Any, system: str = _SYSTEM_1K, user: str = _USER_1K) -> None:
    meter.complete_json(system=system, user=user, schema={}, model="m", max_tokens=8)


def test_the_ceiling_refuses_the_request_it_cannot_afford_before_issuing_it() -> None:
    """A Ceiling bounds what a run may SPEND, so the refused request is the one the budget could not
    cover — not the one after it. What it will cost is not knowable until it returns, so an estimate
    is deducted at admission and reconciled against the real usage when the response arrives."""
    from seqforge.harvest import CeilingExceeded, TokenMeter

    inner = _CostingProvider(per_call=1000)
    meter = TokenMeter(inner, ceiling=2500)

    _ask(meter)
    _ask(meter)  # 2000 banked, and a third request is estimated at 1000 more than the budget has

    with pytest.raises(CeilingExceeded) as caught:
        _ask(meter)

    assert inner.n_calls == 2, "the request the budget could not cover never reached the provider"
    assert meter.n_exchanges == 2 and meter.tokens == 2000
    assert caught.value.spent == 2000 and caught.value.ceiling == 2500


def test_the_ceiling_refuses_a_wave_admitted_before_anything_has_banked() -> None:
    """The concurrent case, pinned — a whole document set admitted in one wave of the pool.

    A check on the tokens ALREADY BANKED asks a question whose answer is not yet knowable: six
    workers pass it before any of them has banked a thing, so a run that fits inside one wave could
    never refuse at all, and the tightness of a ceiling would depend on the core count of the
    machine that ran it. Nothing here banks until every worker has settled — issued or refused — so
    a meter that admitted on banked totals alone issues all six and this goes red.
    """
    from concurrent.futures import ThreadPoolExecutor

    from seqforge.harvest import CeilingExceeded, TokenMeter

    settled = threading.Semaphore(0)  # one release per worker that has been issued OR refused
    resume = threading.Event()

    class _HeldOpen(_CostingProvider):
        """Holds every issued request open until the whole wave has settled."""

        def complete_json(self, **kwargs: Any) -> LLMResponse:
            settled.release()
            assert resume.wait(timeout=10), "the wave never settled"
            return super().complete_json(**kwargs)

    inner = _HeldOpen(per_call=1000)
    meter = TokenMeter(inner, ceiling=3000)  # three requests, at an estimated 1000 each

    def one(_i: int) -> bool:
        try:
            _ask(meter)
        except CeilingExceeded:
            settled.release()
            return False
        return True

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(one, i) for i in range(6)]
        for _ in range(6):
            assert settled.acquire(timeout=10), "a worker neither reached the provider nor refused"
        resume.set()
        issued = [f.result() for f in futures]

    assert inner.n_calls == 3, "what the budget covered, however many the pool offered at once"
    assert sum(issued) == 3 and issued.count(False) == 3
    assert meter.tokens == 3000, "the ceiling was spent, not overshot by five more requests"


def test_a_request_the_whole_ceiling_cannot_cover_refuses_at_the_gate() -> None:
    """A ceiling smaller than one request stops the run before anything is issued. Admitting it
    "because nothing has been spent yet" would be a ceiling that always allows one arbitrarily
    large call, which is the reading a run of a single enormous document would get."""
    from seqforge.harvest import CeilingExceeded, TokenMeter

    inner = _CostingProvider(per_call=1000)
    meter = TokenMeter(inner, ceiling=500)  # one request is estimated at 1000

    with pytest.raises(CeilingExceeded) as caught:
        _ask(meter)

    assert inner.n_calls == 0 and meter.n_exchanges == 0 and meter.tokens == 0
    assert caught.value.spent == 0 and caught.value.estimate == 1000
    # "0 tokens spent, ceiling 500" reads as a bug rather than a refusal, so the number that
    # actually decided it is in the message a caller sees.
    assert "estimated at 1,000 tokens" in str(caught.value)


def test_a_failed_request_gives_its_reservation_back() -> None:
    """Reserving before the request means reconciling after it, on the failure path too. A leaked
    reservation would starve a run of budget it never spent, and the leak compounds: three flaky
    documents early on would refuse everything after them for the rest of the run."""
    from seqforge.harvest import ProviderUnavailable, TokenMeter

    inner = _SequencedProvider([_transient(), _transient(), _transient(), _OK])
    meter = TokenMeter(inner, ceiling=1000)  # room for exactly one request's estimate at a time

    for _ in range(3):
        with pytest.raises(ProviderUnavailable):
            _ask(meter)

    _ask(meter)  # admitted: three failed requests hold nothing against the budget
    assert meter.n_exchanges == 4 and meter.tokens == 10


def test_the_reservation_learns_what_a_request_really_costs() -> None:
    """The character estimate is a rule of thumb that knows nothing about the OUTPUT half, so on its
    own it under-reserves — and a systematic under-reservation restores exactly the pool-width
    dependence that reserving exists to remove. Once a run has banked an exchange it stops guessing:
    a reservation is at least what an exchange has really cost so far.
    """
    from seqforge.harvest import CeilingExceeded, TokenMeter

    inner = _CostingProvider(per_call=100)
    meter = TokenMeter(inner, ceiling=350)

    for _ in range(3):
        _ask(meter, system="", user="")  # nothing to estimate FROM: zero characters

    with pytest.raises(CeilingExceeded):
        _ask(meter, system="", user="")
    assert meter.tokens == 300, "three at 100, and a fourth the remaining 50 could not cover"


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
    # Reserving is what makes this exact rather than pool-shaped: every request costs what the run
    # has learned a request costs, so the budget is spent to the last token and none of it twice.
    assert meter.tokens == 200


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
    """A record set shaped like a real one: project -> sample -> experiment -> runs.

    **Every level's prose carries a per-sample token**, and that is a property of real deposits rather
    than a convenience here: GSE207085's 1440 sample records each carry ``sample_title:
    nasal_prox1_<N>``, its experiments repeat that title, and its run aliases are ``GSM<N>_r1``. A
    fixture whose two samples differ *only* in their accessions would be the degenerate case
    `plan_extraction`'s near-identical collapse folds into one document — which is a real behaviour
    with its own tests below (:func:`_twins`), and would silently gut every batching test here of the
    second document it needs to batch anything.
    """
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
                free_text=[
                    FreeText(label="sample_alias", text=f"{sample} whole worm plate{sample[-1]}")
                ],
            ),
            ArchiveRecord(
                level="experiment",
                accession=experiment,
                parent=sample,
                free_text=[
                    FreeText(label="design", text=f"Chromium Single Cell 3' v3, plate{sample[-1]}.")
                ],
            ),
        ]
        records += [
            ArchiveRecord(
                level="run",
                accession=f"SRR{sample[-1]}{i}",
                parent=experiment,
                free_text=[FreeText(label="run_alias", text=f"{alias}_plate{sample[-1]}_r{i}")],
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
    # Two samples with the same run count render to the same shape, so the near-identical collapse
    # reduces the second — and the subject claim has to survive BOTH outcomes, which is the point of
    # reading it off the set rather than off one document's name.
    assert {d.source_basename for d in collapsed} == {"runs-SAMN1.txt", "run-SAMN2-variant.txt"}


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


def test_a_structure_only_record_set_plans_nothing_and_asks_nobody(tmp_path: Path) -> None:
    """The plan a hand-written record set produces — an empty one — and the fan-out over it.

    A `source: user` set declares which files compile together and never a fact about what a sample
    was, so it carries no prose at all: every record fails `_worth_asking` and the send list comes
    back empty. That is the intended shape of the case the file exists for — an in-house dataset with
    no accession and no paper — and not a degraded one, so the whole pipeline below it has to treat
    an empty plan as an answer rather than as an input it never expected.

    **Asks nobody, and that is the half that was missing.** With no batches there is no request, so a
    provider that raises when it is touched still yields a clean empty list of outcomes. Zero
    outcomes is precisely what `harvest extract` used to be unable to survive — the loop that builds
    the extractor never ran and the verify step then asserted on the `None` it was left holding.

    Read through the real loader, because the container model does not enforce the hand-written
    dialect: a set built directly could carry exactly the free text this test is about the absence of.
    """
    import yaml

    from seqforge.harvest import extract_planned, plan_extraction
    from seqforge.recordset import load_record_set

    path = tmp_path / "records.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "source": "user",
                "query": "plateA",
                "records": [
                    {"level": "sample", "id": "lib01"},
                    *(
                        {
                            "level": "run",
                            "id": f"plateA_S{n}",
                            "parent": "lib01",
                            "filenames": [f"plateA_S{n}_L001_R1_001.fastq.gz"],
                        }
                        for n in (1, 3)
                    ),
                ],
            }
        )
    )
    plan = plan_extraction(records=load_record_set(path), system_prompt_chars=9_000)

    assert plan.n_documents == 0 and plan.n_records_read == 0
    assert plan.n_requests == 0, "and therefore no request to price"
    assert plan.estimated_input_tokens == 0, "not even the stable prefix, which is paid per request"
    assert plan.report().documents == [], "a dry run says the same thing, in the same shape"

    provider = _FakeProvider(AssertionError("an empty plan must not reach a provider"))
    assert extract_planned(plan, kb.load_all_specs(), provider=provider) == []
    assert provider.captured == {}, "no request was built, let alone sent"


def test_the_plan_charges_the_stable_prefix_once_per_request(tmp_path: Path) -> None:
    """The prefix is ~9 KB and byte-identical on every request, so N requests pay it N times. That is
    the arithmetic that makes a fan-out over one-line aliases expensive, and a plan that only counted
    the documents' own text would hide it.

    Per REQUEST, not per document: the sample record and the collapsed run document are asked the
    same nine attributes and travel together (#190), so four documents are three requests — and a
    dry run still charging four would overstate the run it is a dry run of by a whole prefix.
    """
    from seqforge.harvest import plan_extraction

    prefix = len(build_system_prompt(kb.load_all_specs(), llm_schema()))
    plan = plan_extraction(
        documents=[_doc(tmp_path)], records=_records({"SAMN1": 4}), system_prompt_chars=prefix
    )

    assert plan.n_documents == 4  # the paper, a sample, an experiment, one collapsed run document
    assert plan.n_requests == 3, "the sample and the run document are one question, asked once"
    assert plan.n_chars == sum(len(d.text) for d in plan.documents)
    assert plan.estimated_input_tokens == (3 * prefix + plan.n_chars) // 4
    assert plan.estimated_input_tokens > 3 * prefix // 4


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


# ---------- one request for the documents that get the same question ----------
# A document is not a request. Three archive records of 45, 209 and 213 characters cost 9,382 input
# tokens as three requests — >95% prompt, paid three times over three round trips — and the whole
# point of grouping them is that the count drops. What makes it safe is the tripwire that was already
# there: every draft carries a `doc_sha256` and the quote must grep back into THAT document, so
# cross-document contamination fails span verification by construction. What makes it never worse is
# the fallback: a batch that fails is immediately re-asked one document at a time.

_SPECIES = ("Caenorhabditis elegans", "Drosophila melanogaster", "Mus musculus")


def _many(tmp_path: Path, texts: list[str]) -> list[NormalizedDoc]:
    """One document per text, each in its own directory so the basenames may collide."""
    docs = []
    for i, text in enumerate(texts):
        where = tmp_path / f"doc-{i}"
        where.mkdir()
        docs.append(_doc(where, text))
    return docs


def _species_docs(tmp_path: Path) -> tuple[list[NormalizedDoc], dict[str, str]]:
    """Three same-ask documents, each naming an organism the other two never mention.

    Distinct claims are what make routing checkable at all: with three identical documents, a draft
    filed against the wrong one is indistinguishable from a draft filed against the right one.
    """
    docs = _many(tmp_path, [f"Samples were {s}, reared at 20 C." for s in _SPECIES])
    return docs, {d.doc_sha256: s for d, s in zip(docs, _SPECIES, strict=True)}


def _shas_in(user: str) -> list[str]:
    """Every document the request names, in the order it names them."""
    prefix = "Document sha256: "
    return [line[len(prefix) :] for line in user.splitlines() if line.startswith(prefix)]


def _organism(value: str, sha: str) -> dict[str, Any]:
    """One draft the model could honestly return: the species, quoted from the document naming it."""
    return {
        "field": "experiment.organism",
        "value": value,
        "span": {"doc_sha256": sha, "quote": value, "context": None},
        "llm_confidence": 0.9,
    }


class _AnswersEveryDocument:
    """A model that answers every document a request carries, and says which claim came from which.

    Batching hands the model one job a single-document request never gives it: naming the document a
    claim came from. So this reads the shas the request printed, answers each with the claim only
    that document supports, and records the WIDTH of every request — which is how a test observes
    that the request count dropped rather than inferring it.

    The knobs are the three ways a batch can go wrong, one per test: refusing a multi-document
    request outright, answering only some of it, and losing track of which document is which.
    """

    name = "batching"

    def __init__(
        self,
        answers: dict[str, str],
        *,
        batch_failure: Exception | str | None = None,
        answer_only: int | None = None,
        reroute: dict[str, str] | None = None,
    ) -> None:
        self._answers = answers
        self._batch_failure = batch_failure
        self._answer_only = answer_only
        self._reroute = reroute or {}
        self.n_calls = 0
        #: How many documents each request carried, in call order.
        self.widths: list[int] = []
        self.asked: list[dict[str, Any]] = []

    def default_model(self) -> str:
        return "batching-1"

    def complete_json(self, **kwargs: Any) -> LLMResponse:
        self.n_calls += 1
        self.asked.append(kwargs)
        shas = _shas_in(str(kwargs["user"]))
        self.widths.append(len(shas))
        if len(shas) > 1 and self._batch_failure is not None:
            if isinstance(self._batch_failure, Exception):
                raise self._batch_failure
            return LLMResponse(text=self._batch_failure, usage={"input_tokens": 30})
        drafts = [
            _organism(self._answers[sha], self._reroute.get(sha, sha))
            for sha in shas[: self._answer_only]
        ]
        return LLMResponse(text=json.dumps({"drafts": drafts}), usage={"input_tokens": 10})


def test_documents_that_receive_the_same_ask_travel_in_one_request(tmp_path: Path) -> None:
    """The item itself (#190): the request count drops, and the prompt overhead drops with it.

    Three documents, one request. The stable system prefix is unchanged — batching lives entirely in
    the volatile half, because a preamble in the cached half would make the prefix a function of how
    a plan happened to group and invalidate the cache on every batch of a different width — and the
    ask, ~1.3 KB of NCBI definitions, is now written once per REQUEST rather than once per document.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    docs, answers = _species_docs(tmp_path)
    plan = plan_extraction(documents=docs)
    provider = _AnswersEveryDocument(answers)

    outcomes = extract_planned(plan, kb.load_all_specs(), provider=provider)

    assert plan.n_documents == 3, "still three documents..."
    assert plan.n_requests == provider.n_calls == 1, "...and one question, asked once"
    assert provider.widths == [3]
    assert [len(o.drafts) for o in outcomes] == [1, 1, 1], "every document still got its own answer"

    (sent,) = provider.asked
    assert sent["system"] == build_system_prompt(kb.load_all_specs(), llm_schema())
    assert sent["user"].count("Fields to look for") == 1, "the ask is paid once, not once per doc"
    assert _shas_in(sent["user"]) == [d.doc_sha256 for d in docs]
    assert all(d.text in sent["user"] for d in docs)


def test_two_different_asks_never_share_a_request() -> None:
    """The group key is the ASK, not the (scope, role) pair it comes from.

    A prompt is the only place the ask exists, so documents may share a request exactly when the
    request would put the same question to both. A `sample` record and a collapsed `run` document are
    asked the same nine attributes and are one question; an `experiment` document is asked two fields
    and is another. Keying on scope would send two requests to ask one question.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    plan = plan_extraction(records=_records({"SAMN1": 2, "SAMN2": 2}))
    assert plan.n_documents == 6

    by_index = {i: plan.asked(plan.documents[i]) for i in range(6)}
    for batch in plan.batches:
        assert len({by_index[i] for i in batch}) == 1, "one request puts one question"

    scopes = {tuple(sorted(plan.documents[i].scope for i in batch)) for batch in plan.batches}
    assert scopes == {("run", "run", "sample", "sample"), ("experiment", "experiment")}

    provider = _AnswersEveryDocument({d.doc_sha256: "Mus musculus" for d in plan.documents})
    extract_planned(plan, kb.load_all_specs(), provider=provider, partial=True)
    assert provider.n_calls == 2 < plan.n_documents
    assert sorted(provider.widths) == [2, 4]


def test_the_character_budget_splits_an_oversized_group_rather_than_sending_one_request(
    tmp_path: Path,
) -> None:
    """The budget bounds one request, and a document is never split to fit it.

    Splitting one would change the text a quote is greppable against, which is the one thing this
    pipeline may not do — so a document larger than the whole budget is simply its own request, and
    arrives whole.
    """
    from seqforge.harvest import extract_planned, plan_extraction
    from seqforge.harvest.plan import MAX_BATCH_CHARS

    sentence = "Cells were fixed. "
    half = sentence * (MAX_BATCH_CHARS // (2 * len(sentence)) + 10)  # two never fit together
    over = sentence * (MAX_BATCH_CHARS // len(sentence) + 10)  # one is a request by itself
    docs = _many(tmp_path, [f"{half} Aliquot 0.", f"{half} Aliquot 1.", f"{over} Aliquot 2."])
    assert 2 * len(docs[0].text) > MAX_BATCH_CHARS and len(docs[2].text) > MAX_BATCH_CHARS

    plan = plan_extraction(documents=docs)
    provider = _AnswersEveryDocument({d.doc_sha256: "Mus musculus" for d in docs})
    extract_planned(plan, kb.load_all_specs(), provider=provider, partial=True)

    assert plan.n_requests == provider.n_calls == 3 and provider.widths == [1, 1, 1]
    assert all(any(d.text in str(sent["user"]) for sent in provider.asked) for d in docs), (
        "whole, in one request each"
    )


def test_the_width_of_a_request_is_the_output_budget_divided_by_its_ask() -> None:
    """The rule itself: how many documents share a request is a function of what they are asked.

    ``(32 000 - 1 000) / (n_asked x 62)`` — an output budget, less a fixed reservation for reasoning
    tokens that bill against that same ceiling, over the measured cost of serialising one draft. So
    the nine sample attributes come out at 55, an experiment record's two-field ask at 250, and a
    dataset document's thirteen at 38.

    **55 and not the 57 #233 decision 1 quotes**, and the two tokens between them are the point.
    Decision 1 computed 57 as ``32 000 / 558`` with no reserve at all, before #282 required one;
    rounding the measured 62 down to 60 reaches 57 again only by handing the reserve straight back,
    at which point a full batch's worst case (``57 x 558 = 31 806``) exceeds the ``31 780`` it would
    ask for. A width that matches a projection the projection's own assumptions no longer support is
    worth less than one that falls out of the measurement.

    Every one of them is above the single count of **8** that used to be applied to all of them
    alike, and the narrow ask by 31x: that number was worst-case-tuned against the widest ask, so the
    experiment request — the one carrying the protocol paragraph — paid the sample vocabulary's
    price. The width falling as the ask widens is the whole shape, and it is what one constant could
    not express at any value.
    """
    from seqforge.harvest.fields import fields_for
    from seqforge.harvest.plan import batch_width

    assert batch_width(fields_for("sample", "reference")) == 55
    assert batch_width(fields_for("run", "reference")) == 55, "nine attributes, the same question"
    assert batch_width(fields_for("experiment", "reference")) == 250
    assert batch_width(fields_for("dataset", "reference")) == 38

    asks = _every_ask()
    assert all(batch_width(a) > 8 for a in asks), "no ask is still bounded by the count it replaced"
    widest = max(asks, key=len)
    assert batch_width(widest) == min(batch_width(a) for a in asks), "widest ask, narrowest request"
    assert batch_width(()) >= 1, (
        "a document asked nothing is still a document, not a divide by zero"
    )


def test_a_request_is_bounded_by_its_ask_too_and_never_asks_about_one_document_twice(
    tmp_path: Path,
) -> None:
    """Characters alone do not bound a batch of one-line records, and two bounds are not one twice.

    A run alias is 20 characters and can still support nine sample attributes, so what bounds the
    RESPONSE is how many drafts the ask permits, never how much text went in. That is why the second
    bound is the ask's own width rather than a count: the character budget here is nowhere near
    binding, and the batch still closes. Separately, two documents whose text is byte-identical are
    indistinguishable to a model routing by sha, so they never share a request — a question with no
    answer must not be asked.
    """
    from dataclasses import replace as _replace

    from seqforge.harvest import plan_extraction
    from seqforge.harvest.fields import fields_for
    from seqforge.harvest.plan import MAX_BATCH_CHARS, batch_documents, batch_width

    width = batch_width(fields_for("dataset", "reference"))
    tiny = _many(tmp_path, [f"Rep {i} was N2 wild type." for i in range(width + 3)])
    plan = plan_extraction(documents=tiny)

    assert plan.n_chars < MAX_BATCH_CHARS, "the input budget is not what closed these batches"
    assert [len(b) for b in plan.batches] == [width, 3]

    # Same bytes, same ask, different subject: one document to the model, two to the plan.
    twin = _replace(tiny[0], subject="SAMN9")
    assert batch_documents([tiny[0], twin]) == ((0,), (1,))


def _every_ask() -> list[tuple[str, ...]]:
    """Every question this compiler can put to a document — every role x every scope.

    Collected from the ``Literal``s themselves rather than retyped here, so a sixth scope arrives in
    these assertions the moment it is declared. A list spelled out by hand stays green over a
    vocabulary it has stopped covering, which is the one way a test like this fails silently.
    """
    from typing import get_args

    from seqforge.harvest.fields import DocRole, DocScope, fields_for

    return [fields_for(scope, role) for scope in get_args(DocScope) for role in get_args(DocRole)]


def test_max_tokens_is_computed_from_the_batch_and_reserves_room_for_reasoning(
    tmp_path: Path,
) -> None:
    """The ceiling a width was divided out of is the ceiling the request actually asks for.

    A full-width nine-attribute batch is 55 x 9 x 62 = 30 690 output tokens of drafts, plus the
    1 000 reserved for reasoning tokens — which bill against this same ceiling — for 31 690, just
    under the 32 000 budget. Pinned at 8 000, the number this replaced, the same request would run
    out of ceiling about a quarter of the way through its own batch, truncate the JSON, and fail the
    shape gate wholesale.

    The reserve has to survive the arithmetic, not just appear in it: the drafts a full batch can
    emit fit **inside** what the request asks for, leaving the reservation actually reserved. That is
    the check a rounded-down per-draft rate would quietly break.

    The fallback is priced too, and priced to what it already was: one document asks 8 000, which is
    what every unbatched request has asked since before batching existed. A fallback that could fail
    by truncation where the batch it is recovering did not would be no fallback at all.
    """
    from dataclasses import replace as _replace

    from seqforge.harvest import extract_planned, plan_extraction
    from seqforge.harvest.fields import fields_for
    from seqforge.harvest.plan import BATCH_OUTPUT_BUDGET, batch_max_tokens, batch_width

    width = batch_width(fields_for("sample", "reference"))
    texts = [f"Aliquot {i} was N2 wild type." for i in range(width)]
    docs = [_replace(d, scope="sample") for d in _many(tmp_path, texts)]
    plan = plan_extraction(documents=docs)
    assert [len(b) for b in plan.batches] == [width], (
        "one full-width request, and this is its price"
    )

    provider = _AnswersEveryDocument(
        {d.doc_sha256: "Mus musculus" for d in docs},
        batch_failure=ProviderUnavailable("this endpoint refused the request"),
    )
    extract_planned(plan, kb.load_all_specs(), provider=provider)

    ceilings = [sent["max_tokens"] for sent in provider.asked]
    assert provider.widths == [width] + [1] * width, "the batch, then one request per document"
    assert ceilings == [31_690] + [8000] * width

    from seqforge.harvest.plan import PER_DRAFT_TOKENS, REASONING_HEADROOM_TOKENS

    worst_case_drafts = width * len(fields_for("sample", "reference")) * PER_DRAFT_TOKENS
    assert worst_case_drafts + REASONING_HEADROOM_TOKENS <= ceilings[0], (
        "the reservation is only a reservation if the drafts fit under the ceiling without it"
    )

    assert all(
        8000 <= batch_max_tokens(ask, n) <= BATCH_OUTPUT_BUDGET
        for ask in _every_ask()
        for n in (1, batch_width(ask))
    ), "never above the budget, and never below what one document already asked for"


def test_the_requests_a_plan_sends_do_not_depend_on_the_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property batching has always claimed, now that a second number could have broken it.

    `MAX_IN_FLIGHT` is derived from the core count, and it sizes the POOL that carries the batches;
    it must never size the batches themselves. Neither may the width rule — it reads the ask, and the
    ask comes from the document. So the same plan run under a pool of one and a pool of forty-eight
    issues the same requests: the same documents grouped the same way, each carrying the same
    `max_tokens`.

    What the patch below actually varies is `max_workers`, and saying so is the honest version:
    `_SLOTS` is sized once at import and rebinding the module attribute does not resize it, so the
    process-wide allowance is the same in both runs while the executor's width is not. That is
    sufficient for what this test is about — the two runs schedule differently, and a plan whose
    grouping or ceilings depended on scheduling would diverge. A test that claimed to vary both would
    be claiming a reach it does not have.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    plan = plan_extraction(documents=[_doc(tmp_path)], records=_records({"SAMN1": 2, "SAMN2": 2}))
    answers = {d.doc_sha256: "Mus musculus" for d in plan.documents}

    def _requests(pool: int) -> list[tuple[str, int]]:
        monkeypatch.setattr("seqforge.harvest.plan.MAX_IN_FLIGHT", pool)
        provider = _AnswersEveryDocument(answers)
        extract_planned(plan, kb.load_all_specs(), provider=provider, partial=True)
        return sorted((str(sent["user"]), int(sent["max_tokens"])) for sent in provider.asked)

    laptop, node = _requests(1), _requests(48)

    assert len(laptop) == plan.n_requests > 1, "more than one request, or this proves nothing"
    assert laptop == node


def test_the_dry_runs_request_list_is_the_one_the_paid_run_issues(tmp_path: Path) -> None:
    """The module's central promise, re-checked against a run now that the widths have moved.

    `--dry-run` reports `n_requests`, which derives from `batches` — the very tuple the fan-out
    iterates — so the count cannot drift from the run by construction. What a test still owes is that
    the *contents* agree: every planned document reaches exactly one request, no request carries a
    document the plan did not list, and the widths the plan predicted are the widths sent.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    plan = plan_extraction(documents=[_doc(tmp_path)], records=_records({"SAMN1": 2, "SAMN2": 2}))
    report = plan.report()
    provider = _AnswersEveryDocument({d.doc_sha256: "Mus musculus" for d in plan.documents})

    extract_planned(plan, kb.load_all_specs(), provider=provider, partial=True)

    assert report.n_requests == provider.n_calls < report.n_documents
    assert sorted(provider.widths) == sorted(len(b) for b in plan.batches)
    sent = [sha for request in provider.asked for sha in _shas_in(str(request["user"]))]
    assert sorted(sent) == sorted(d.doc_sha256 for d in report.documents), (
        "each document once, and nothing the dry run did not list"
    )


def test_a_batched_run_lands_every_claim_on_the_document_that_carried_it(tmp_path: Path) -> None:
    """The correctness the whole item rests on, checked all the way through the tripwire.

    A batched run must produce the same assertions as an unbatched one, against the same documents:
    the sha the model echoes is how a draft is routed, `verify` then re-greps the quote into the
    document it names, and every claim here belongs to exactly one of the three.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    docs, answers = _species_docs(tmp_path)
    plan = plan_extraction(documents=docs)

    batched = extract_planned(plan, kb.load_all_specs(), provider=_AnswersEveryDocument(answers))
    alone = [
        extract_drafts(d, kb.load_all_specs(), provider=_AnswersEveryDocument(answers))
        for d in docs
    ]

    assert [[d.model_dump() for d in o.drafts] for o in batched] == [
        [d.model_dump() for d in o.drafts] for o in alone
    ], "one request or three, the drafts are the same drafts on the same documents"

    report = verify_drafts(
        [d for o in batched for d in o.drafts], docs, extractor=batched[0].extractor
    )
    assert report.rejected == []
    assert [(a.value, a.span.doc_sha256) for a in report.assertions] == [
        (species, doc.doc_sha256) for doc, species in zip(docs, _SPECIES, strict=True)
    ]


def test_a_claim_routed_to_the_wrong_member_fails_the_span_tripwire(tmp_path: Path) -> None:
    """Why batching is safe by construction, rather than by the model being careful.

    A model that files one document's claim under another member's sha has produced a quote that is
    not in the document it cites, and `verify` greps it back into THAT document — so contamination
    between two members of one request fails closed, exactly as a fabricated citation does. Nothing
    in `extract` re-checks it: `find_span` is the authority on whether a document carries a quote,
    and a second, weaker answer here would disagree with it and re-issue batches over claims that
    verify fine.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    docs, answers = _species_docs(tmp_path)
    plan = plan_extraction(documents=docs)
    swap = {docs[0].doc_sha256: docs[1].doc_sha256}

    (contaminated,) = [
        o
        for o in extract_planned(
            plan, kb.load_all_specs(), provider=_AnswersEveryDocument(answers, reroute=swap)
        )
        if len(o.drafts) == 2
    ]
    report = verify_drafts(contaminated.drafts, docs, extractor=contaminated.extractor)

    assert [a.value for a in report.assertions] == [_SPECIES[1]], "its own claim survives"
    assert [r["reason"] for r in report.rejected] == ["span_not_found"]


def test_a_batch_that_answers_only_some_of_its_documents_is_not_a_failure(tmp_path: Path) -> None:
    """Per-document silence is a legitimate answer, and it is why coverage cannot be a failure signal.

    "This document supports nothing" returns no drafts, so a batch answering two of its three
    documents is indistinguishable from one where the third had nothing to say — which is the common
    case, not the exception. Treating it as a batch failure would re-ask fifty-seven sample records
    every time fifty of them were silent, which is strictly more requests than never batching at all.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    docs, answers = _species_docs(tmp_path)
    plan = plan_extraction(documents=docs)
    provider = _AnswersEveryDocument(answers, answer_only=2)

    outcomes = extract_planned(plan, kb.load_all_specs(), provider=provider)

    assert provider.n_calls == 1, "no fallback: the request was answered"
    assert [len(o.drafts) for o in outcomes] == [1, 1, 0]
    assert [o.answered for o in outcomes] == [True, True, True]
    assert outcomes[2].failure is None, "checked and found nothing is not could not check"


def test_a_draft_for_a_document_the_request_never_carried_refuses_the_whole_batch(
    tmp_path: Path,
) -> None:
    """An unroutable draft means the model lost track of the batch, so the batch is re-asked.

    Silently dropping it would lose a claim an unbatched run would have kept — code overwrites the
    echoed sha at width one, so there is nothing there to lose track of — and losing a claim is
    exactly what batching may not do. Re-asking costs one round trip and recovers it, which is the
    trade the fallback already makes.
    """
    from seqforge.harvest import extract_planned, plan_extraction
    from seqforge.harvest.extract import extract_batch

    docs, answers = _species_docs(tmp_path)
    stray = {docs[2].doc_sha256: "f" * 64}

    with pytest.raises(ExtractUnavailable, match="none of the 3 documents"):
        extract_batch(
            docs, kb.load_all_specs(), provider=_AnswersEveryDocument(answers, reroute=stray)
        )

    plan = plan_extraction(documents=docs)
    provider = _AnswersEveryDocument(answers, reroute=stray)
    outcomes = extract_planned(plan, kb.load_all_specs(), provider=provider)

    assert provider.widths == [3, 1, 1, 1], "the batch, then one request per document"
    assert [len(o.drafts) for o in outcomes] == [1, 1, 1], "and the claim it nearly dropped is back"


_BATCH_FAILURES = [
    pytest.param(ProviderUnavailable("this endpoint refused the request"), id="a-provider-error"),
    pytest.param("not json at all", id="an-unusable-envelope"),
    pytest.param(json.dumps({"notes": []}), id="an-envelope-with-no-drafts"),
]


@pytest.mark.parametrize("failure", _BATCH_FAILURES)
def test_a_batch_failure_falls_back_to_per_document_calls_and_loses_nothing(
    failure: Exception | str, tmp_path: Path
) -> None:
    """THE decision (#190): batching may never lose more documents than not batching would have.

    Unbatched, a failed request costs one document; batched it could cost the whole batch, which
    would make "half a batch is worse than none" worse rather than better. So a batch-level failure
    — a provider error, or any response the one shape gate cannot use — re-asks every member
    individually, at once. Worst case that is one extra round trip; here the provider fails on
    anything wider than one document, which is the worst case, and all three documents still answer
    with the claim each carries.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    docs, answers = _species_docs(tmp_path)
    plan = plan_extraction(documents=docs)
    provider = _AnswersEveryDocument(answers, batch_failure=failure)

    outcomes = extract_planned(plan, kb.load_all_specs(), provider=provider)

    assert [o.answered for o in outcomes] == [True] * 3
    assert [d.value for o in outcomes for d in o.drafts] == list(_SPECIES)
    assert provider.widths == [3, 1, 1, 1], "one wasted round trip, and exactly one"
    assert provider.n_calls == 1 + plan.n_documents


def test_a_document_that_fails_alone_costs_what_it_cost_before_batching(tmp_path: Path) -> None:
    """What the fallback does with a document that fails on its own is what an unbatched run does.

    `partial` still decides it, and it still decides it per document: off, the plan raises and the
    compiler fails closed; on, that one document comes back unanswered and the other two keep their
    claims. Neither answer is new — the point is that a batch failure hands the decision back to the
    policy that already existed rather than making one of its own.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    docs, answers = _species_docs(tmp_path)
    plan = plan_extraction(documents=docs)

    class _AlsoFailsOnOne(_AnswersEveryDocument):
        """Refuses every batch, and also refuses one particular document on its own."""

        def complete_json(self, **kwargs: Any) -> LLMResponse:
            if docs[1].text in str(kwargs["user"]) and len(_shas_in(str(kwargs["user"]))) == 1:
                self.n_calls += 1
                self.widths.append(1)
                return LLMResponse(text="not json at all", usage={"input_tokens": 3})
            return super().complete_json(**kwargs)

    with pytest.raises(ExtractUnavailable):
        extract_planned(
            plan,
            kb.load_all_specs(),
            provider=_AlsoFailsOnOne(answers, batch_failure="not json at all"),
        )

    outcomes = extract_planned(
        plan,
        kb.load_all_specs(),
        provider=_AlsoFailsOnOne(answers, batch_failure="not json at all"),
        partial=True,
    )
    assert [o.answered for o in outcomes] == [True, False, True]
    assert outcomes[1].failure is not None and "not valid JSON" in outcomes[1].failure
    assert [d.value for o in outcomes for d in o.drafts] == [_SPECIES[0], _SPECIES[2]]


def test_a_failed_batch_gives_its_reservation_back_before_the_fallback_asks_for_one(
    tmp_path: Path,
) -> None:
    """The meter interaction the fallback must not get wrong: a batch that failed still reserved.

    The meter deducts a request's estimate before issuing it and reconciles on every path out, so by
    the time the retries ask to be admitted the batch's estimate is already back in the budget. Held
    instead, it would starve the very fallback it was meant to protect — the run would refuse at a
    ceiling that comfortably covers one batch plus one document at a time, having answered nothing.
    """
    from seqforge.harvest import CeilingExceeded, TokenMeter, extract_planned, plan_extraction
    from seqforge.harvest.meter import estimated_tokens

    docs, answers = _species_docs(tmp_path)
    plan = plan_extraction(documents=docs)
    refuses = ProviderUnavailable("this endpoint refused the request")

    # The PROMPTS are read off an unceilinged run; what they are estimated at is then computed with
    # the meter's own `estimated_tokens`, deliberately. This is a ceiling expressed as a SCALE, not
    # an expected value: the subject is whether a failed batch's reservation comes back, and that is
    # only observable against a budget tight enough that holding it would starve the retries. Pinning
    # a literal here would pin the estimator instead, and go red on any change to it while proving
    # nothing about release. What gives this test its failure power is the mechanism, not the number
    # — neuter `_release` and it refuses at the third retry.
    probe = TokenMeter(_AnswersEveryDocument(answers, batch_failure=refuses))
    extract_planned(plan, kb.load_all_specs(), provider=probe)
    transcript = probe.transcript()
    costs = [estimated_tokens(transcript.prompt_for(x), x.user) for x in transcript.exchanges[:2]]

    provider = _AnswersEveryDocument(answers, batch_failure=refuses)
    meter = TokenMeter(provider, ceiling=sum(costs) - 1)
    outcomes = extract_planned(plan, kb.load_all_specs(), provider=meter)

    assert [o.answered for o in outcomes] == [True] * 3
    assert provider.widths == [3, 1, 1, 1]
    # ...and the ceiling is genuinely binding: one token less and the batch itself cannot be issued.
    with pytest.raises(CeilingExceeded):
        extract_planned(
            plan,
            kb.load_all_specs(),
            provider=TokenMeter(
                _AnswersEveryDocument(answers, batch_failure=refuses), ceiling=costs[0] - 1
            ),
        )


# ---------- the near-identical collapse: read once, fan by grep, and price the plate that differs ---
# 1440 sample records that differ only in an accession are semantically identical and LEXICALLY
# distinct, so `_deduplicated` — which keys on the whole text — misses them entirely. What follows
# pins the three rules that replace it: group by token skeleton, withhold only a member whose
# distinctive tokens are its own accession, and fan a claim iff its quote touches no variant.

_FAN_EXTRACTOR = ExtractorProvenance(model_id="test/fan", prompt_version="v1")


def _twins(
    prose: list[str], *, level: RecordLevel = "sample", label: str = "sample_title"
) -> ArchiveRecordSet:
    """One record per entry of ``prose``, at one level, under a project.

    **The accessions are deliberately of different lengths** (`SAMN1`, `SAMN22`, `SAMN333`). An
    invariant span therefore sits at a different offset in every member, which is the whole reason
    `fan_claims` recomputes offsets with `find_span` against each member's own text rather than
    copying the exemplar's — a copied offset would point at the wrong characters in a document that
    genuinely carries the quote.
    """
    stem = {"sample": "SAMN", "experiment": "SRX", "run": "SRR"}[level]
    records = [ArchiveRecord(level="project", accession="PRJNA1")]
    for i, text in enumerate(prose, start=1):
        accession = f"{stem}{str(i) * i}"
        parent = "PRJNA1"
        if level != "sample":
            parent = f"SAMN{str(i) * i}"
            records.append(ArchiveRecord(level="sample", accession=parent, parent="PRJNA1"))
        records.append(
            ArchiveRecord(
                level=level,
                accession=accession,
                parent=parent,
                free_text=[FreeText(label=label, text=text)],
            )
        )
    return ArchiveRecordSet(source="test", query="PRJNA1", records=records)


def _verified(plan: Any, field: str, value: str, quote: str, doc: Any = None) -> list[Any]:
    """One draft through the REAL tripwire, so nothing below can fan a claim verify would refuse."""
    from seqforge.models.assertion import AssertionDraft

    doc = doc or plan.documents[0]
    draft = AssertionDraft(
        field=field,
        value=value,
        llm_confidence=0.9,
        span=SourceSpan(doc_sha256=doc.doc_sha256, quote=quote),
    )
    report = verify_drafts([draft], list(plan.documents), extractor=_FAN_EXTRACTOR)
    assert report.n_accepted == 1, report.rejected
    return report.assertions


def test_records_that_differ_only_in_an_accession_are_one_ask(tmp_path: Path) -> None:
    """The ticket's case. `_deduplicated` keys on the whole text and these texts all differ, so the
    shipped dedup misses every one of them; the collapse is what sees that they say the same thing.

    Everything stays visible: the records are still READ (`n_records_read`), the fold is counted
    (`n_records_collapsed`), and the exemplar's `members` names every record it stands for — because
    "one document, 4 members" is a different thing for a human to audit than 4 readings. Here all
    three others are *withheld*, so `members` really is the whole group and `reduced_members` is
    empty; the test below is the case where that stops being true.
    """
    from seqforge.harvest import plan_extraction

    plan = plan_extraction(records=_twins(["whole worm, day3"] * 4))

    assert plan.n_documents == 1, "four records, one ask"
    assert plan.n_records_read == 4 and plan.n_records_collapsed == 3
    exemplar = plan.documents[0]
    assert plan.stands_for(exemplar) == ("SAMN1", "SAMN22", "SAMN333", "SAMN4444")
    assert plan.report().documents[0].members == list(plan.stands_for(exemplar))
    assert plan.report().documents[0].reduced_members == [], "nothing here was sent its difference"
    assert [m.value for m in exemplar.variants] == ["SAMN1"], "only the accession varies"


def test_one_record_that_differs_makes_every_record_asked_with_no_special_case(
    tmp_path: Path,
) -> None:
    """The non-degenerate plate, and the design pricing itself.

    If one record says `day7` where three say `day3`, then `day3` is not shared, so it is not in the
    invariant, so it lands in the variants — and every variant then carries a token that is not that
    record's own accession, so **every record is asked**. Cost tracks information content. The
    degenerate plate is cheap *because* it is degenerate, not because anything assumed it was.
    """
    from seqforge.harvest import plan_extraction

    same = plan_extraction(records=_twins(["whole worm, day3"] * 4))
    one_differs = plan_extraction(records=_twins(["whole worm, day3"] * 3 + ["whole worm, day7"]))

    assert same.n_documents == 1 and same.n_records_collapsed == 3
    assert same.n_records_reduced == 0, "nothing distinctive to send; the accession is ours"

    assert one_differs.n_documents == 4, (
        "every record is asked — the bill is the information content"
    )
    assert one_differs.n_records_collapsed == 0, "nothing is withheld, because nothing is silent"
    assert one_differs.n_records_reduced == 3
    # ...and what each of them is asked is its own value, which is what makes the ask worth making.
    sent = {d.subject: d.text for d in one_differs.documents}
    assert "day7" in sent["SAMN4444"] and "day3" in sent["SAMN22"]
    assert one_differs.n_chars < 4 * len(sent["SAMN1"]), "and the shared prose is read once"


def test_a_reduced_member_is_not_reported_as_a_record_this_document_was_the_only_reading_of() -> (
    None
):
    """The two outcomes are two facts, and one member list said the wrong one about both.

    A reduced member IS sent — its distinctive bytes, as a document of its own — so listing it beside
    the withheld ones under a single `members` says the opposite of what happened. On GSE207085 that
    reads as one exemplar standing for 1440 records while 4317 of the 4320 were in fact asked their
    difference, and a reader concludes 1439 went unread. The guarantee this whole mechanism is named
    for is that none of them did.

    So the arithmetic is the check, not the prose: **every record a plan reads appears in exactly one
    document's `members`**. Summing that column against `n_records_read` is how "no record went
    unread" becomes something a reader verifies rather than something we assert.
    """
    from seqforge.harvest import plan_extraction

    plan = plan_extraction(records=_twins(["whole worm, day3"] * 3 + ["whole worm, day7"]))
    report = plan.report()
    exemplar, *reduced = report.documents

    assert exemplar.members == ["SAMN1"], "it is the only reading of its own record and no other"
    assert exemplar.reduced_members == ["SAMN22", "SAMN333", "SAMN4444"]
    # ...and each of those is right here, in the same report, with the bytes it cost.
    assert [d.members for d in reduced] == [["SAMN22"], ["SAMN333"], ["SAMN4444"]]
    assert all(d.n_chars > 0 and d.reduced_members == [] for d in reduced)

    assert sum(len(d.members) for d in report.documents) == report.n_records_read == 4

    # One group holding one of each, which is the case a single list cannot describe at all. SAMN22's
    # alias is nothing but the accession we ourselves wrote, so it is withheld — read only through the
    # exemplar. SAMN333 says `day7`, so its difference is sent and it is read in a document of its own.
    mixed = plan_extraction(
        records=_twins(["day3", "SAMN22", "day7"], label="sample_alias")
    ).report()

    assert mixed.documents[0].members == ["SAMN1", "SAMN22"], "withheld, so read only here"
    assert mixed.documents[0].reduced_members == ["SAMN333"], "sent, so read over there"
    assert sum(len(d.members) for d in mixed.documents) == mixed.n_records_read == 3


def test_a_claim_fans_out_only_where_its_quote_touches_no_variant() -> None:
    """The fan-out predicate, both directions.

    A quote lying entirely inside spans byte-identical across the group greps into every member by
    construction. A quote reaching into a variant speaks for the member we sent and nothing else —
    which is what stops a value fanning onto the record that differed.
    """
    from seqforge.harvest import fan_claims, plan_extraction

    plan = plan_extraction(records=_twins(["N2 whole worm, day3"] * 3))
    shared = fan_claims(_verified(plan, "experiment.samples.age", "day3", "day3"), plan)
    named = fan_claims(
        _verified(plan, "experiment.samples.strain", "N2", "sample SAMN1\n\nsample_title: N2"), plan
    )

    assert [f.n_records for f in shared.fanned] == [3]
    assert named.fanned == [], "the quote carries the accession, which is exactly what varies"
    assert len(named.assertions) == 1


def test_a_sample_scoped_claim_materializes_one_assertion_per_member() -> None:
    """The fan-out UNIT follows field arity, and the field path already says which.

    The nine `experiment.samples.*` are sample-scoped, so each member gets its own `Assertion` citing
    its OWN document — which is what keeps `resolve.records._basis_for` untouched: the claim maps home
    through `subject_to_sample` exactly as an unfanned one does, and stays `asserted`. Offsets are
    recomputed against each member's text, and the members' accessions are different lengths on
    purpose, so a copied offset would be provably wrong.
    """
    from seqforge.harvest import fan_claims, plan_extraction

    plan = plan_extraction(records=_twins(["whole worm, day3"] * 3))
    fan = fan_claims(_verified(plan, "experiment.samples.age", "day3", "day3"), plan)

    assert len(fan.assertions) == 3, "one per member"
    assert len({a.id for a in fan.assertions}) == 3
    assert len({a.span.doc_sha256 for a in fan.assertions}) == 3, "each cites its own document"
    assert {a.span_verified for a in fan.assertions} == {True}
    assert {a.entailment_ok for a in fan.assertions} == {True}

    withheld = {d.doc_sha256: d for d in plan.collapsed[plan.documents[0].doc_sha256].members}
    for a in fan.assertions[1:]:
        text = withheld[a.span.doc_sha256].text
        assert text[a.span.char_start : a.span.char_end] == "day3"
    assert len({a.span.char_start for a in fan.assertions}) == 3, "the offsets really do differ"
    # ...and the claim's own report names every assertion it produced, first the model's own. A count
    # beside a list nobody can join back to is what `PlannedDocument.members` cannot answer for a
    # claim, and it is the whole reason `FannedClaim` exists (folded from the report sibling test).
    assert [f.materialized for f in fan.fanned] == [True]
    said, claim = fan.assertions[0], fan.fanned[0]
    assert claim.assertion_ids == tuple(a.id for a in fan.assertions)
    assert (claim.field, claim.value, claim.quote) == (said.field, said.value, said.span.quote)


def test_a_dataset_scoped_claim_stays_one_assertion_and_buys_a_proof_of_unanimity() -> None:
    """`library.chemistry` is dataset-scoped, and `chemistry_hypothesis` reduces N identical claims to
    one regardless — so materializing 1440 of them would buy nothing. What the grep buys here is the
    *proof of unanimity*: one assertion, and a count of the records whose own bytes carry the quote.

    This is what keeps that check ("agreement or nothing") from going vacuous under a collapse. A
    record whose paragraph said something else would have a different token, fail the grep, leave the
    group and get its own ask — which is the test above.
    """
    from seqforge.harvest import fan_claims, plan_extraction

    plan = plan_extraction(
        records=_twins(
            ["Libraries were prepared with the Chromium Single Cell 3' v3 kit."] * 4,
            level="experiment",
            label="design",
        )
    )
    fan = fan_claims(_verified(plan, "library.chemistry", "10x-3p-gex-v3", _QUOTE), plan)

    assert len(fan.assertions) == 1, "one judgement, one envelope — not one per record"
    assert [(f.n_records, f.materialized) for f in fan.fanned] == [(4, False)]
    assert fan.fanned[0].records == ("SRX1", "SRX22", "SRX333", "SRX4444")


def test_the_age_hazard_never_fans_a_value_into_the_record_that_said_something_else() -> None:
    """The character-granularity bug, at the level a user would meet it.

    Three records say `age: 3` and one says `age: 30`. At character granularity `3` is shared, so the
    claim would fan onto the record that said 30 and the leftover variant would be a single `0`
    nothing would ever send. At token granularity the position varies, every record is asked, and no
    claim fans anywhere.
    """
    from seqforge.harvest import fan_claims, plan_extraction

    plan = plan_extraction(records=_twins(["age: 3"] * 3 + ["age: 30"], label="sample_alias"))
    fan = fan_claims(_verified(plan, "experiment.samples.age", "3", "age: 3"), plan)

    assert plan.n_documents == 4, "the record that differed is asked, not guessed at"
    assert plan.n_records_collapsed == 0, "nothing is withheld: every member's `3`/`30` is askable"
    # The group DOES form and the marks ARE computed — which is what makes this the real test. The
    # claim does not fan because its quote reaches into the token that varies, not because no group
    # was found. At character granularity `3` would have been shared and the fan would be silent.
    assert plan.collapsed and plan.documents[0].variants
    assert fan.fanned == [] and len(fan.assertions) == 1
    assert "30" in {d.text.split()[-1] for d in plan.documents}, (
        "the odd record is asked its own 30"
    )


def test_a_document_a_human_handed_us_is_never_folded_into_another(tmp_path: Path) -> None:
    """A paper has no record behind it, so there is no accession to recognize as "the record's own
    identity" and no submitter repeating themselves — only two authors who happen to agree. Folding
    them would be inventing a join nobody declared."""
    from seqforge.harvest import plan_extraction

    twins = []
    for name, accession in (("a.md", "SAMN1"), ("b.md", "SAMN22")):
        path = tmp_path / name
        path.write_text(f"Chromium Single Cell 3' v3 libraries, deposited as {accession}.")
        twins.append(normalize_document(path))

    plan = plan_extraction(documents=twins)
    assert plan.n_documents == 2, "two authors who agree, not one submitter repeating themselves"
    assert plan.collapsed == {} and [d.variants for d in plan.documents] == [(), ()]


def test_the_collapse_is_at_plan_time_so_the_dry_run_is_the_bill_the_paid_run_pays() -> None:
    """Collapse at send time and `harvest extract --dry-run` and `eval plan` both report a bill nobody
    pays — which is this workstream's own test, since the dry run's whole claim is that it is the same
    list the paid run sends rather than a projection of one."""
    from seqforge.harvest import extract_planned, plan_extraction

    plan = plan_extraction(records=_twins(["whole worm, day3"] * 6))
    report = plan.report()

    provider = _AnswersEveryDocument({d.doc_sha256: "Mus musculus" for d in plan.documents})
    extract_planned(plan, kb.load_all_specs(), provider=provider, partial=True)

    assert report.n_documents == plan.n_documents == 1
    assert provider.n_calls == report.n_requests == 1, "one document sent, one request made"
    assert report.n_records_collapsed == 5


def test_a_collapsed_member_is_rendered_and_kept_even_though_it_is_never_sent() -> None:
    """`all_documents` is what must reach disk, as against `documents`, which is what is paid for.

    A fanned assertion cites a document nobody sent. Its bytes exist nowhere else — we made them —
    and `resolve` drops a claim whose document has no subject, so both the text and the subject have
    to survive the process (ADR-0031).
    """
    from seqforge.harvest import plan_extraction

    plan = plan_extraction(records=_twins(["whole worm, day3"] * 3))

    assert len(plan.all_documents) == 3 and plan.n_documents == 1
    assert plan.all_documents[0] is plan.documents[0]
    assert [d.subject for d in plan.all_documents] == ["SAMN1", "SAMN22", "SAMN333"]
    assert all(d.text for d in plan.all_documents)


def test_the_residue_is_zero_in_a_request_of_one_and_grows_with_the_batch() -> None:
    """The instrument for the `--llm` recall probe, and it is deterministic — no model, no network.

    A batch puts several documents in one prompt and the model routes each draft by echoing a
    `doc_sha256`, so a quote occurring verbatim in two members of one request verifies either way and
    nothing downstream can tell. Counting is per DOCUMENT, not per request: count each distinct span
    once per request and the totals fall as the batch widens — because there are fewer requests —
    which reads as the hazard shrinking when it is doing the opposite.

    Eight documents, each sharing one phrase with the document four along and with nobody nearer. So
    the residue is genuinely zero at widths 1, 2 and 4 and only appears at 8 — monotone, and not
    monotone by construction. On a real near-identical deposit it instead SATURATES at width 2, since
    two such documents already collide on everything they share; both shapes are the same number.
    """
    from seqforge.harvest import plan_extraction, quote_residue

    phrases = [
        "kept on NGM plates seeded with OP50",
        "grown in liquid culture with added cholesterol",
        "raised at twenty degrees in a shared incubator",
        "harvested by hand under a dissecting microscope",
    ]
    plan = plan_extraction(
        records=_twins([f"cohort{i} cohort{i} cohort{i} {phrases[i % 4]}" for i in range(8)])
    )
    assert plan.n_documents == 8, "nothing folds: this measures batching, not the collapse"

    alone = quote_residue(plan, width=1)
    pairs = quote_residue(plan, width=2)
    quads = quote_residue(plan, width=4)
    wide = quote_residue(plan, width=8)

    assert alone.rate == 0.0, "a request of one has nobody to collide with"
    assert pairs.rate == quads.rate == 0.0, "no neighbour within four shares a phrase"
    assert 0 < wide.rate <= 1.0, (
        "the pair four apart lands in one request, and now it can be misrouted"
    )
    assert alone.n_spans == pairs.n_spans == wide.n_spans, "only the ambiguity moves with width"
    assert [r.n_documents for r in wide.per_request] == [8]
