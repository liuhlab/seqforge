"""Tests for ``harvest extract`` — the one LLM touchpoint.

The model call is faked: liulab-llms's fake gateway keeps the gateway's rules, and a function in each
test plays the provider. That is the point: everything *around* the model is deterministic and must be
provable without spending a token — the schema, the stability of the cached prefix, and above all that
code (not the model) owns provenance, offsets, and the shape gate. Extraction *quality* is an evals
concern (evals), not a unit-test one.

Sending, retrying, caching and the dollar Ceiling are the gateway's, and liulab-llms tests them. What
is tested here is harvest's side of the seam: what it asks, what it keeps of a reply, and that it never
trusts a reply's shape. DeepSeek thinks, so it is sent no schema and nothing holds its reply to a
shape: the gate must hold whichever provider answered.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from labllm import ProviderExhausted
from labllm.testing import FakeGateway

import harvest_kit
from seqforge import kb
from seqforge.harvest import (
    DEEPSEEK_DEFAULT_MODEL,
    EXTRACT_PROMPT_VERSION,
    ExtractUnavailable,
    ModelSeam,
    NormalizedDoc,
    build_kb_context,
    build_system_prompt,
    extract_drafts,
    harvest_model,
    llm_schema,
    normalize_document,
    verify_drafts,
)
from seqforge.models.assertion import ExtractorProvenance, SourceSpan
from seqforge.models.records import ArchiveRecord, ArchiveRecordSet, FreeText, RecordLevel

_QUOTE = "Chromium Single Cell 3' v3"
_TEXT = "Libraries were prepared with the Chromium Single Cell 3' v3 kit."


def _over(provider: Callable[[dict[str, Any]], Any], **seam: Any) -> tuple[FakeGateway, ModelSeam]:
    """A fake gateway with ``provider`` playing the provider, and a seam over a new meter on it.

    The ceiling is ten dollars unless a test says otherwise, which no test here reaches by accident:
    the ceiling is the gateway's to enforce, and a test of it would be a test of liulab-llms.
    """
    fake = harvest_kit.fake(provider)
    return fake, harvest_kit.seam_over(fake, **{"ceiling_usd": 10.0, **seam})


def _answering(text: str, **seam: Any) -> tuple[FakeGateway, ModelSeam]:
    """A seam whose provider answers every request with ``text``.

    A fake gateway caches by request, so two calls that must get two different replies to the same
    request take two seams.
    """
    return _over(harvest_kit.answering(text), **seam)


def _sent(fake: FakeGateway) -> list[dict[str, Any]]:
    """The form of every request the provider was sent, in order. A cache hit sends none."""
    return [e.form for e in fake.record if e.form is not None]


def _user(form: dict[str, Any]) -> str:
    """The volatile half of a request: the documents, and what is asked of them."""
    return str(form["messages"][-1]["content"])


def _system(form: dict[str, Any]) -> str:
    """The stable half of a request: the system prompt."""
    return str(form["messages"][0]["content"])


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


# ---------- the prompt / stable prefix ----------
def test_kb_context_is_deterministic_and_carries_aliases() -> None:
    once = build_kb_context(kb.load_all_specs())
    assert "Chromium 3' v3" in once  # the alias that bridges paper prose -> the KB id
    assert once.index("10x-3p-gex-v2") < once.index("10x-3p-gex-v3") < once.index("splitseq")


def test_system_prompt_carries_the_reply_contract() -> None:
    """DeepSeek thinks and is sent no schema, so the prompt alone asks for JSON: it says 'json',
    carries the schema, and shows a worked example."""
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


@pytest.mark.parametrize(
    ("provider", "name", "sends_schema"),
    [
        pytest.param(None, None, False, id="deepseek-thinks-and-is-sent-none"),
        pytest.param(
            "anthropic", "claude-test-1", True, id="a-model-that-does-not-think-is-sent-it"
        ),
    ],
)
def test_extract_keeps_the_document_out_of_the_cached_prefix(
    provider: str | None, name: str | None, sends_schema: bool, tmp_path: Path
) -> None:
    """The document rides in the volatile half, and the request travels under the wire schema derived
    from ``AssertionDraft`` rather than a second, hand-maintained copy that could drift from it.

    Except from a model that thinks: the gateway sends a DeepSeek schema as a strict function call,
    which DeepSeek takes only with thinking off, so the default model is sent the schema in its
    prompt alone. A model that does not think keeps its provider's strict output."""
    fake, seam = _answering(harvest_kit.NOTHING, model=harvest_model(provider, name))
    extract_drafts(_doc(tmp_path), kb.load_all_specs(), seam=seam)
    (sent,) = _sent(fake)
    assert _TEXT not in _system(sent)  # volatile content must not poison the prefix
    assert _TEXT in _user(sent)
    assert sent.get("schema") == (llm_schema() if sends_schema else None)


def test_the_request_says_which_document_it_is_about_and_says_it_readably(tmp_path: Path) -> None:
    """An Exchange keeps the request verbatim and nothing else about it, so the prompt's own first
    line is the only thing that ties a stored exchange back to a document. Written and read in this
    module on purpose: a reader that re-derived the format elsewhere would drift in silence, and
    what it would take with it is the eval report's ability to say which record an exchange was
    about."""
    from seqforge.harvest import document_sha256_in

    doc = _doc(tmp_path)
    fake, seam = _answering(harvest_kit.NOTHING)
    extract_drafts(doc, kb.load_all_specs(), seam=seam)
    (sent,) = _sent(fake)

    assert document_sha256_in(_user(sent)) == doc.doc_sha256
    assert document_sha256_in("no document line here") is None
    assert document_sha256_in(_system(sent)) is None, "the prefix names no document"


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
    _, seam = _answering(json.dumps({"drafts": [_GOOD, _BAD, missing]}))
    outcome = extract_drafts(_doc(tmp_path), kb.load_all_specs(), seam=seam)

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
            seam=_answering(json.dumps({"not_drafts": 1}))[1],
        )
    # `{"drafts": null}` must blame `drafts`, not report the useless top-level `got dict`.
    with pytest.raises(ExtractUnavailable, match="`drafts` key is a NoneType"):
        extract_drafts(
            _doc(tmp_path),
            kb.load_all_specs(),
            seam=_answering(json.dumps({"drafts": None}))[1],
        )


@pytest.mark.parametrize(
    ("drafts", "values", "n_rejected"),
    [
        pytest.param([_GOOD], ["10x-3p-gex-v3"], 0, id="a-batch"),
        pytest.param([_GOOD, _BAD, _GOOD], ["10x-3p-gex-v3"] * 2, 1, id="a-malformed-member"),
        pytest.param([], [], 0, id="a-document-that-supports-nothing"),
    ],
)
def test_a_bare_array_or_a_fenced_object_is_the_same_batch_under_a_different_envelope(
    drafts: list[dict[str, Any]], values: list[str], n_rejected: int, tmp_path: Path
) -> None:
    """A model that returns the drafts array without wrapping it has still returned the batch, and so
    has one that wraps the object in a code fence among prose.

    The elements are byte-identical either way and each one still faces
    `AssertionDraft.model_validate` alone, so refusing the document over the absent key would
    discard a whole document's worth of valid extraction for one missing token — the same defect
    `test_extract_drops_one_malformed_draft_and_keeps_the_rest` closed one layer down, and measurably
    live on the weaker json-object models (#190). The fence is the default model's to add: it thinks,
    so it is sent no schema, and nothing but the prompt holds its reply to bare JSON. Unwrapping
    changes the envelope and nothing else: the per-draft gate is still the gate, and `[]` is still
    the common, correct answer that an envelope may not turn into a lost document.
    """
    doc = _doc(tmp_path)
    wrapped = extract_drafts(
        doc, kb.load_all_specs(), seam=_answering(json.dumps({"drafts": drafts}))[1]
    )
    bare = extract_drafts(doc, kb.load_all_specs(), seam=_answering(json.dumps(drafts))[1])
    fence = f"Here are the drafts:\n```json\n{json.dumps({'drafts': drafts})}\n```\nDone."
    fenced = extract_drafts(doc, kb.load_all_specs(), seam=_answering(fence)[1])

    for other in (bare, fenced):
        assert [d.value for d in other.drafts] == [d.value for d in wrapped.drafts] == values
        assert len(other.rejected) == len(wrapped.rejected) == n_rejected
        assert other.drafts == wrapped.drafts
        assert other.rejected == wrapped.rejected
        assert other.answered
        assert other.failure is None
    assert wrapped.answered


@pytest.mark.parametrize(
    ("payload", "named"), [('"drafts"', "str"), ("7", "int"), ("null", "NoneType")]
)
def test_every_other_top_level_shape_still_dies_wholesale(
    tmp_path: Path, payload: str, named: str
) -> None:
    """The array is the ONE extra envelope. A scalar carries no batch to keep, so it stays fatal —
    and the refusal names what came back plus both shapes that would have been read, which the old
    wording ("not a JSON object") can no longer say now that an object is not the only one."""
    with pytest.raises(ExtractUnavailable) as caught:
        extract_drafts(_doc(tmp_path), kb.load_all_specs(), seam=_answering(payload)[1])

    message = str(caught.value)
    assert f"top-level {named}" in message
    assert "`drafts`" in message
    assert "array of drafts" in message


def test_extract_overwrites_the_models_doc_sha(tmp_path: Path) -> None:
    """We know which document we sent; the model's echo is worthless as evidence. Code wins."""
    nd = _doc(tmp_path)
    outcome = extract_drafts(nd, kb.load_all_specs(), seam=_answering(_batch(sha="dead" * 16))[1])
    assert outcome.drafts[0].span.doc_sha256 == nd.doc_sha256


def test_extract_discards_model_supplied_offsets(tmp_path: Path) -> None:
    nd = _doc(tmp_path)
    payload = _batch(char_start=999, char_end=1234)  # a model cannot count characters
    outcome = extract_drafts(nd, kb.load_all_specs(), seam=_answering(payload)[1])
    assert outcome.drafts[0].span.char_start is None
    assert outcome.drafts[0].span.char_end is None


#: `--provider` and `--model` -> who the extractor was and what every request is sent with, or a
#: refusal. DeepSeek is the default, called with `deepseek-v4-pro` (it won the benchmark on every
#: axis, cost included) and with thinking on at high effort, its documented default, sent explicitly
#: so the setting is in the cache key. A DeepSeek name harvest does not default to still reaches the
#: gateway. Any other provider takes its own defaults and must be named a model, since harvest knows
#: no other provider's names: a guessed model is a provenance bug. `mode` is None for a refusal.
_MODELS = [
    pytest.param(
        None, None, "deepseek/deepseek-v4-pro", {"thinking": True, "effort": "high"},
        id="deepseek-pro-thinking-high-by-default",
    ),
    pytest.param(
        "deepseek", "deepseek-flash", "deepseek/deepseek-flash", {"thinking": True, "effort": "high"},
        id="a-deepseek-name-is-passed-through",
    ),
    pytest.param(
        "anthropic", "claude-test-1", "anthropic/claude-test-1", {},
        id="another-provider-is-sent-no-settings",
    ),
    pytest.param("anthropic", None, None, None, id="another-provider-needs-a-model"),
]  # fmt: skip


@pytest.mark.parametrize(("provider", "name", "model_id", "mode"), _MODELS)
def test_extract_records_provider_in_provenance(
    provider: str | None,
    name: str | None,
    model_id: str | None,
    mode: dict[str, Any] | None,
    tmp_path: Path,
) -> None:
    """The same prompt on a different provider is a different extractor; evals must tell them apart.

    And the settings the request was sent with are the ones the outcome records, so the Ledger says
    at what effort the prose was read rather than what someone meant to send.
    """
    if model_id is None or mode is None:
        with pytest.raises(ValueError, match="needs --model"):
            harvest_model(provider, name)
        return

    fake, seam = _answering(harvest_kit.NOTHING, model=harvest_model(provider, name))
    outcome = extract_drafts(_doc(tmp_path), kb.load_all_specs(), seam=seam)

    assert outcome.extractor.model_id == model_id
    assert outcome.extractor.prompt_version == EXTRACT_PROMPT_VERSION
    assert f"{outcome.provider}/{outcome.model}" == model_id
    (sent,) = _sent(fake)
    assert f"{sent['provider']}/{sent['model']}" == model_id
    allowance = sent["settings"]["max_output_tokens"]
    assert {k: v for k, v in sent["settings"].items() if v is not None} == {
        **mode,
        "max_output_tokens": allowance,
    }
    assert outcome.mode == {**mode, "max_output_tokens": allowance}


def test_extract_carries_what_its_request_cost_for_the_ledger(tmp_path: Path) -> None:
    """The outcome records what its request cost: the provider's own usage record, the dollars, and
    whether the gateway's cache answered. That is the raw material the harvest stage writes to
    `seqforge/logs/usage.json`, so a reader can see what understanding the prose cost — and the same
    request again is answered from the cache, for nothing.
    """
    fake, seam = _answering(harvest_kit.NOTHING)
    doc = _doc(tmp_path)

    paid = extract_drafts(doc, kb.load_all_specs(), seam=seam)
    replayed = extract_drafts(doc, kb.load_all_specs(), seam=seam)

    first, again = fake.record
    assert not first.cached
    assert again.cached
    assert paid.cost_usd == pytest.approx(first.cost_usd)
    assert paid.cost_usd > 0
    assert not paid.cached
    assert paid.usage, "the provider's own usage record rides along"
    assert replayed.cost_usd == 0.0
    assert replayed.cached


# ---------- the transcript ----------
def test_the_transcript_is_one_prompt_plus_a_pair_per_exchange(tmp_path: Path) -> None:
    """983 exchanges share one byte-identical system prompt (that is why prefix caching works), so
    a transcript stores it ONCE and every exchange points at it."""
    _, seam = _answering(harvest_kit.NOTHING)
    docs = []
    for i in range(4):
        (tmp_path / f"d{i}").mkdir()
        docs.append(_doc(tmp_path / f"d{i}", f"We used protocol number {i}."))
    for doc in docs:
        extract_drafts(doc, kb.load_all_specs(), seam=seam)

    transcript = seam.transcript()
    assert transcript.n_exchanges == 4
    assert len(transcript.prompts) == 1, "the stable prefix is stored once, not four times"
    system = transcript.prompt_for(transcript.exchanges[0])
    assert system == build_system_prompt(kb.load_all_specs(), llm_schema())
    # the volatile half is per document, and it is the document that varies
    assert [d.text in e.user for d, e in zip(docs, transcript.exchanges, strict=True)] == [True] * 4
    assert all(e.ok and e.text == harvest_kit.NOTHING for e in transcript.exchanges)


# ---------- the plan: records -> documents, before a token is spent ----------
# One module owns the fan-out both `harvest extract` and the eval harness used to spell for
# themselves. What it must get right is the count (a call per run was 92% of a corpus's bill), and
# the reassembly (a dict keyed by document identity loses one of two identical documents).


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


def test_only_the_experiment_records_are_sent() -> None:
    """A record document is asked only what its level can answer, and only an experiment can answer
    anything: its protocol paragraph names the chemistry. Project, sample and run records carry prose
    here, and none of it costs a call."""
    from seqforge.harvest import plan_extraction

    plan = plan_extraction(records=_records({"SAMN1": 12, "SAMN2": 4}))

    assert Counter(d.scope for d in plan.documents) == {"experiment": 2}
    assert plan.n_records_read == 2
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

    assert plan.n_documents == 0
    assert plan.n_records_read == 0
    assert plan.estimated_input_tokens == 0, "no call, no stable prefix to pay for"


def test_a_structure_only_record_set_plans_nothing_and_asks_nobody(tmp_path: Path) -> None:
    """The plan a hand-written record set produces — an empty one — and the fan-out over it.

    A `source: user` set declares which files compile together and never a fact about what a sample
    was, so it carries no prose at all: every record fails `_worth_asking` and the send list comes
    back empty. That is the intended shape of the case the file exists for — an in-house dataset with
    no accession and no paper — and not a degraded one, so the whole pipeline below it has to treat
    an empty plan as an answer rather than as an input it never expected.

    **Asks nobody, and that is the half that was missing.** With no batches there is no request, so
    the gateway records nothing and the fan-out yields a clean empty list of outcomes. Zero
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

    assert plan.n_documents == 0
    assert plan.n_records_read == 0
    assert plan.n_requests == 0, "and therefore no request to price"
    assert plan.estimated_input_tokens == 0, "not even the stable prefix, which is paid per request"
    assert plan.report().documents == [], "a dry run says the same thing, in the same shape"

    fake, seam = _answering(harvest_kit.NOTHING)
    assert extract_planned(plan, kb.load_all_specs(), seam=seam) == []
    assert fake.record == [], "no request was built, let alone sent"
    assert seam.n_exchanges == 0


def test_the_plan_charges_the_stable_prefix_once_per_request(tmp_path: Path) -> None:
    """The prefix is ~9 KB and byte-identical on every request, so N requests pay it N times. That is
    the arithmetic that makes a fan-out over one-line aliases expensive, and a plan that only counted
    the documents' own text would hide it.

    Per REQUEST, not per document: two experiment records are asked the same question and travel
    together (#190), so three documents are two requests — and a dry run still charging three would
    overstate the run it is a dry run of by a whole prefix.
    """
    from seqforge.harvest import plan_extraction

    prefix = len(build_system_prompt(kb.load_all_specs(), llm_schema()))
    plan = plan_extraction(
        documents=[_doc(tmp_path)],
        records=_records({"SAMN1": 1, "SAMN2": 1}),
        system_prompt_chars=prefix,
    )

    assert plan.n_documents == 3  # the paper and two experiments
    assert plan.n_requests == 2, "the two experiments are one question, asked once"
    assert plan.n_chars == sum(len(d.text) for d in plan.documents)
    assert plan.estimated_input_tokens == (2 * prefix + plan.n_chars) // 4
    assert plan.estimated_input_tokens > 2 * prefix // 4


def test_the_plan_is_free_and_the_dry_run_is_the_list_the_paid_run_sends(tmp_path: Path) -> None:
    """A dry run is exact rather than a projection: it renders the same documents, and rendering
    costs no token and no network."""
    from seqforge.harvest import plan_extraction

    plan = plan_extraction(documents=[_doc(tmp_path)], records=_records({"SAMN1": 2}))
    report = plan.report()

    assert report.n_documents == plan.n_documents == len(report.documents)
    assert [d.doc_sha256 for d in report.documents] == [d.doc_sha256 for d in plan.documents]
    experiment = next(d for d in report.documents if d.scope == "experiment")
    assert experiment.members == ["SRX1"], "the record each document renders is visible"
    assert experiment.fields == ["library.chemistry"]


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

    _, seam = _answering(_batch())
    outcomes = extract_planned(plan, kb.load_all_specs(), seam=seam)
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

    plan = plan_extraction(documents=[_doc(tmp_path)], records=_records({"SAMN1": 2, "SAMN2": 2}))
    assert plan.n_documents == 3
    assert plan.n_requests == 2

    def finishes_out_of_order(form: dict[str, Any]) -> str:
        """The first document submitted is the last to answer."""
        time.sleep(0.05 if plan.documents[0].text in _user(form) else 0.0)
        return _batch(quote=_QUOTE)

    _, seam = _over(finishes_out_of_order)
    outcomes = extract_planned(plan, kb.load_all_specs(), seam=seam)

    # every draft is anchored by code onto the document that was actually sent, so the shas ARE the
    # order the calls were made in
    assert [o.drafts[0].span.doc_sha256 for o in outcomes] == [d.doc_sha256 for d in plan.documents]


def _one_document_fails(poison: str) -> Callable[[dict[str, Any]], str]:
    """Answers every document except the one whose text holds ``poison``, which comes back unusable.

    Modelled on the failure that produced this seam: DeepSeek's invalid ``json_object`` (#4), which
    lands on whichever document provokes a long response rather than on the longest document.
    """

    def provider(form: dict[str, Any]) -> str:
        return "not json at all" if poison in _user(form) else _batch(quote=_QUOTE)

    return provider


def test_one_documents_abort_takes_down_the_whole_plan_by_default(tmp_path: Path) -> None:
    """The compiler fails closed, and that stays true.

    An extraction missing a document produces a manifest silently short a fact, and nothing
    downstream can tell it from a complete one — so the default is still to raise.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    plan = plan_extraction(documents=[_doc(tmp_path), _doc(_sub(tmp_path), "A poison table.")])
    assert plan.n_documents == 2

    with pytest.raises(ExtractUnavailable):
        extract_planned(plan, kb.load_all_specs(), seam=_over(_one_document_fails("poison"))[1])


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

    _, seam = _over(_one_document_fails("poison"))
    outcomes = extract_planned(plan, kb.load_all_specs(), seam=seam, partial=True)

    assert len(outcomes) == 2, "still positionally aligned with the plan"
    by_doc = dict(zip(plan.documents, outcomes, strict=True))
    assert by_doc[good].answered, "the good document still extracted"
    assert by_doc[good].drafts, "the good document still extracted"
    survivor = by_doc[bad]
    assert not survivor.answered
    assert survivor.drafts == []
    assert survivor.failure is not None
    assert "not valid JSON" in survivor.failure
    # ...and it still names who did not answer, because "nothing came back" is a fact about an
    # extractor and a report that cannot say which one is unreadable a week later.
    assert survivor.extractor.model_id == f"deepseek/{DEEPSEEK_DEFAULT_MODEL}"


def test_an_empty_batch_is_an_answer_and_a_failure_is_not(tmp_path: Path) -> None:
    """The distinction the whole ticket rests on: checked and found nothing, versus could not check.

    Both produce zero drafts. Folding them together is what let a stage that never ran report the
    same way as one that read the document honestly and had nothing to say about it.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    plan = plan_extraction(documents=[_doc(tmp_path, "We sequenced some things.")])
    _, seam = _answering(harvest_kit.NOTHING)
    (silent,) = extract_planned(plan, kb.load_all_specs(), seam=seam, partial=True)
    assert silent.answered
    assert silent.drafts == []
    assert silent.failure is None


def _balance_used_up(form: dict[str, Any]) -> str:
    """A provider whose prepaid balance is gone: every request stops."""
    raise ProviderExhausted("the prepaid balance is used up")


@pytest.mark.parametrize("width", [1, 3], ids=["a-lone-document", "a-batch"])
def test_partial_does_not_swallow_a_stop(width: int, tmp_path: Path) -> None:
    """A stop is not an unanswered document. The ceiling reached, the provider's balance used up, the
    gateway gone: each says no further request will be answered, so it owes the caller a ``Blocker``
    and exit 3, and must reach one however the fan-out was asked to treat a failed document.

    Nor does a batch fall back past one. Re-asking its documents alone would only meet the stop
    again, once per document, so the request that stopped is the only exchange the seam keeps — and
    it keeps it, with the reason, because a run that stopped is the one most worth reading.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    docs, _ = _species_docs(tmp_path)
    plan = plan_extraction(documents=docs[:width])
    assert [len(b) for b in plan.batches] == [width]
    _, seam = _over(_balance_used_up)

    with pytest.raises(ProviderExhausted):
        extract_planned(plan, kb.load_all_specs(), seam=seam, partial=True)

    (stopped,) = seam.transcript().exchanges
    assert not stopped.ok
    assert (stopped.error or "").startswith("provider_exhausted")
    assert stopped.text == ""


# ---------- the transcript's address on disk ----------
def test_a_transcript_round_trips_through_its_file(tmp_path: Path) -> None:
    """One writer, one reader, one format. A writer with no reader is a shape every consumer has to
    re-derive from the code that produced it — and the report renderer is the consumer.

    One exchange of each kind a run keeps — paid for, answered from the gateway's cache, and stopped
    — so what each cost, whether it was cached and why it stopped all survive the file.
    """
    from seqforge.harvest import read_transcript, write_transcript

    answered, unanswered = _doc(tmp_path), _doc(_sub(tmp_path), "A table nobody will read.")

    def provider(form: dict[str, Any]) -> str:
        if unanswered.text in _user(form):
            raise ProviderExhausted("the prepaid balance is used up")
        return harvest_kit.NOTHING

    _, seam = _over(provider)
    for doc in (answered, answered):
        extract_drafts(doc, kb.load_all_specs(), seam=seam)
    with pytest.raises(ProviderExhausted):
        extract_drafts(unanswered, kb.load_all_specs(), seam=seam)

    path = write_transcript(tmp_path / "transcript.jsonl", seam.transcript())
    back = read_transcript(path)

    assert back == seam.transcript()
    paid, replayed, stopped = back.exchanges
    assert (paid.ok, paid.cached, paid.cost_usd > 0) == (True, False, True)
    assert (replayed.ok, replayed.cached, replayed.cost_usd) == (True, True, 0.0)
    assert not stopped.ok
    assert (stopped.error or "").startswith("provider_exhausted")
    assert back.prompt_for(stopped) == build_system_prompt(kb.load_all_specs(), llm_schema())


def test_the_transcript_file_holds_one_prompt_and_a_line_per_exchange(tmp_path: Path) -> None:
    """The header carries the prompts; every line after it is one exchange pointing at its sha. At
    983 exchanges the alternative is three megabytes of one repeated string, and a file nobody
    opens — and it is what makes the file streamable rather than a tree to parse whole."""
    from seqforge.harvest import write_transcript

    _, seam = _answering(harvest_kit.NOTHING)
    for i in range(3):
        (tmp_path / f"d{i}").mkdir()
        extract_drafts(
            _doc(tmp_path / f"d{i}", f"We used protocol {i}."), kb.load_all_specs(), seam=seam
        )

    lines = write_transcript(tmp_path / "t.jsonl", seam.transcript()).read_text().splitlines()

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
    claim came from. So this reads the shas the request printed and answers each with the claim only
    that document supports. The fake gateway records every request, so a test reads the WIDTH of each
    off the record (:func:`_widths`) — which is how it observes that the request count dropped rather
    than inferring it.

    The knobs are the three ways a batch can go wrong, one per test: answering a multi-document
    request with a reply nobody can use, answering only some of it, and losing track of which
    document is which.
    """

    def __init__(
        self,
        answers: dict[str, str],
        *,
        batch_failure: str | None = None,
        answer_only: int | None = None,
        reroute: dict[str, str] | None = None,
    ) -> None:
        self._answers = answers
        self._batch_failure = batch_failure
        self._answer_only = answer_only
        self._reroute = reroute or {}

    def __call__(self, form: dict[str, Any]) -> str:
        shas = _shas_in(_user(form))
        if len(shas) > 1 and self._batch_failure is not None:
            return self._batch_failure
        drafts = [
            _organism(self._answers[sha], self._reroute.get(sha, sha))
            for sha in shas[: self._answer_only]
        ]
        return json.dumps({"drafts": drafts})


def _widths(fake: FakeGateway) -> list[int]:
    """How many documents each request the provider was sent carried, in the order it was sent."""
    return [len(_shas_in(_user(form))) for form in _sent(fake)]


def test_documents_that_receive_the_same_ask_travel_in_one_request(tmp_path: Path) -> None:
    """The item itself (#190): the request count drops, and the prompt overhead drops with it.

    Three documents, one request. The stable system prefix is unchanged — batching lives entirely in
    the volatile half, because a preamble in the cached half would make the prefix a function of how
    a plan happened to group and invalidate the cache on every batch of a different width — and the
    ask, ~1.3 KB of NCBI definitions, is now written once per REQUEST rather than once per document.

    One request is one bill, booked whole against the document it opens with, which is the document
    its tag names to the gateway. Divided per document it would be a number nobody was billed;
    repeated per document, every consumer that sums the outcomes would report three times the spend.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    docs, answers = _species_docs(tmp_path)
    plan = plan_extraction(documents=docs)
    fake, seam = _over(_AnswersEveryDocument(answers))

    outcomes = extract_planned(plan, kb.load_all_specs(), seam=seam)

    assert plan.n_documents == 3, "still three documents..."
    assert plan.n_requests == len(_sent(fake)) == 1, "...and one question, asked once"
    assert _widths(fake) == [3]
    assert [len(o.drafts) for o in outcomes] == [1, 1, 1], "every document still got its own answer"

    (paid,) = fake.record
    assert paid.tag == docs[0].doc_sha256
    assert [o.cost_usd for o in outcomes] == [pytest.approx(paid.cost_usd), 0.0, 0.0]
    assert paid.cost_usd > 0

    (sent,) = _sent(fake)
    assert _system(sent) == build_system_prompt(kb.load_all_specs(), llm_schema())
    assert _user(sent).count("Fields to look for") == 1, "the ask is paid once, not once per doc"
    assert _shas_in(_user(sent)) == [d.doc_sha256 for d in docs]
    assert all(d.text in _user(sent) for d in docs)


def test_two_different_asks_never_share_a_request(tmp_path: Path) -> None:
    """The group key is the ASK, not the (scope, role) pair it comes from.

    A prompt is the only place the ask exists, so documents may share a request exactly when the
    request would put the same question to both. Two papers are asked the dataset's fields and are
    one question; an `experiment` record is asked the chemistry alone and is another.
    """
    from seqforge.harvest import extract_planned, plan_extraction

    papers = _many(tmp_path, ["Mus musculus, the first paper.", "Mus musculus, the second paper."])
    plan = plan_extraction(documents=papers, records=_records({"SAMN1": 2, "SAMN2": 2}))
    assert plan.n_documents == 4

    by_index = {i: plan.asked(plan.documents[i]) for i in range(4)}
    for batch in plan.batches:
        assert len({by_index[i] for i in batch}) == 1, "one request puts one question"

    scopes = {tuple(sorted(plan.documents[i].scope for i in batch)) for batch in plan.batches}
    assert scopes == {("dataset", "dataset"), ("experiment", "experiment")}

    fake, seam = _over(
        _AnswersEveryDocument({d.doc_sha256: "Mus musculus" for d in plan.documents})
    )
    extract_planned(plan, kb.load_all_specs(), seam=seam, partial=True)
    assert len(_sent(fake)) == 2 < plan.n_documents
    assert sorted(_widths(fake)) == [2, 2]


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
    assert 2 * len(docs[0].text) > MAX_BATCH_CHARS
    assert len(docs[2].text) > MAX_BATCH_CHARS

    plan = plan_extraction(documents=docs)
    fake, seam = _over(_AnswersEveryDocument({d.doc_sha256: "Mus musculus" for d in docs}))
    extract_planned(plan, kb.load_all_specs(), seam=seam, partial=True)

    assert plan.n_requests == len(_sent(fake)) == 3
    assert _widths(fake) == [1, 1, 1]
    users = harvest_kit.sent_texts(fake)
    assert all(any(d.text in user for user in users) for d in docs), "whole, in one request each"


def test_the_width_of_a_request_is_the_output_budget_divided_by_its_ask() -> None:
    """The rule itself: how many documents share a request is a function of what they are asked.

    ``(32 000 - 1 000) / (n_asked x 62)`` — an output budget, less a fixed reservation for reasoning
    tokens that bill against that same ceiling, over the measured cost of serialising one draft. So
    an experiment record's one-field ask comes out at 500, a dataset document's four at 125, and an
    instruction document's six at 83.

    Every one of them is above the single count of **8** that used to be applied to all of them
    alike: that number was worst-case-tuned against the widest ask, so the experiment request — the
    one carrying the protocol paragraph — paid the widest ask's price. The width falling as the ask
    widens is the whole shape, and it is what one constant could not express at any value.
    """
    from seqforge.harvest.fields import fields_for
    from seqforge.harvest.plan import batch_width

    assert batch_width(fields_for("experiment", "reference")) == 500
    assert batch_width(fields_for("dataset", "reference")) == 125
    assert batch_width(fields_for("dataset", "instruction")) == 83

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

    A one-line document is 25 characters and can still support every field asked of it, so what
    bounds the RESPONSE is how many drafts the ask permits, never how much text went in. That is why the second
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

    A full-width batch of the widest ask (an instruction document's six fields) is 83 x 6 x 62 =
    30 876 output tokens of drafts, plus the 1 000 reserved for reasoning tokens — which bill against
    this same ceiling — for 31 876, just under the 32 000 budget. Pinned at 8 000, the number this replaced, the same request would run
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

    width = batch_width(fields_for("dataset", "instruction"))
    texts = [f"Aliquot {i} was N2 wild type." for i in range(width)]
    docs = [_replace(d, role="instruction") for d in _many(tmp_path, texts)]
    plan = plan_extraction(documents=docs)
    assert [len(b) for b in plan.batches] == [width], (
        "one full-width request, and this is its price"
    )

    fake, seam = _over(
        _AnswersEveryDocument(
            {d.doc_sha256: "Mus musculus" for d in docs}, batch_failure="not json at all"
        )
    )
    extract_planned(plan, kb.load_all_specs(), seam=seam)

    ceilings = [form["settings"]["max_output_tokens"] for form in _sent(fake)]
    assert _widths(fake) == [width] + [1] * width, "the batch, then one request per document"
    assert ceilings == [31_876] + [8000] * width

    from seqforge.harvest.plan import PER_DRAFT_TOKENS, REASONING_HEADROOM_TOKENS

    worst_case_drafts = width * len(fields_for("dataset", "instruction")) * PER_DRAFT_TOKENS
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
        fake, seam = _over(_AnswersEveryDocument(answers))
        extract_planned(plan, kb.load_all_specs(), seam=seam, partial=True)
        return sorted((_user(f), int(f["settings"]["max_output_tokens"])) for f in _sent(fake))

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
    fake, seam = _over(
        _AnswersEveryDocument({d.doc_sha256: "Mus musculus" for d in plan.documents})
    )

    extract_planned(plan, kb.load_all_specs(), seam=seam, partial=True)

    assert report.n_requests == len(_sent(fake)) < report.n_documents
    assert sorted(_widths(fake)) == sorted(len(b) for b in plan.batches)
    sent = [sha for user in harvest_kit.sent_texts(fake) for sha in _shas_in(user)]
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

    _, seam = _over(_AnswersEveryDocument(answers))
    batched = extract_planned(plan, kb.load_all_specs(), seam=seam)
    alone = [extract_drafts(d, kb.load_all_specs(), seam=seam) for d in docs]

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

    _, seam = _over(_AnswersEveryDocument(answers, reroute=swap))
    (contaminated,) = [
        o for o in extract_planned(plan, kb.load_all_specs(), seam=seam) if len(o.drafts) == 2
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
    fake, seam = _over(_AnswersEveryDocument(answers, answer_only=2))

    outcomes = extract_planned(plan, kb.load_all_specs(), seam=seam)

    assert len(_sent(fake)) == 1, "no fallback: the request was answered"
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
            docs, kb.load_all_specs(), seam=_over(_AnswersEveryDocument(answers, reroute=stray))[1]
        )

    plan = plan_extraction(documents=docs)
    fake, seam = _over(_AnswersEveryDocument(answers, reroute=stray))
    outcomes = extract_planned(plan, kb.load_all_specs(), seam=seam)

    assert _widths(fake) == [3, 1, 1, 1], "the batch, then one request per document"
    assert [len(o.drafts) for o in outcomes] == [1, 1, 1], "and the claim it nearly dropped is back"


_BATCH_FAILURES = [
    pytest.param("not json at all", id="an-unusable-envelope"),
    pytest.param(json.dumps({"notes": []}), id="an-envelope-with-no-drafts"),
]


@pytest.mark.parametrize("failure", _BATCH_FAILURES)
def test_a_batch_failure_falls_back_to_per_document_calls_and_loses_nothing(
    failure: str, tmp_path: Path
) -> None:
    """THE decision (#190): batching may never lose more documents than not batching would have.

    Unbatched, a failed request costs one document; batched it could cost the whole batch, which
    would make "half a batch is worse than none" worse rather than better. So a batch-level failure
    — any reply the one shape gate cannot use — re-asks every member individually, at once. Worst
    case that is one extra round trip; here the model fails on anything wider than one document,
    which is the worst case, and all three documents still answer with the claim each carries.

    A provider's own failure is not a batch failure: the gateway retries it, and what outlasts the
    retries is a stop, which no fallback re-asks (:func:`test_partial_does_not_swallow_a_stop`).
    """
    from seqforge.harvest import extract_planned, plan_extraction

    docs, answers = _species_docs(tmp_path)
    plan = plan_extraction(documents=docs)
    fake, seam = _over(_AnswersEveryDocument(answers, batch_failure=failure))

    outcomes = extract_planned(plan, kb.load_all_specs(), seam=seam)

    assert [o.answered for o in outcomes] == [True] * 3
    assert [d.value for o in outcomes for d in o.drafts] == list(_SPECIES)
    assert _widths(fake) == [3, 1, 1, 1], "one wasted round trip, and exactly one"
    assert len(_sent(fake)) == 1 + plan.n_documents


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

    every_batch_fails = _AnswersEveryDocument(answers, batch_failure="not json at all")

    def also_fails_on_one(form: dict[str, Any]) -> str:
        """Refuses every batch, and also refuses one particular document on its own."""
        user = _user(form)
        if docs[1].text in user and len(_shas_in(user)) == 1:
            return "not json at all"
        return every_batch_fails(form)

    with pytest.raises(ExtractUnavailable):
        extract_planned(plan, kb.load_all_specs(), seam=_over(also_fails_on_one)[1])

    _, seam = _over(also_fails_on_one)
    outcomes = extract_planned(plan, kb.load_all_specs(), seam=seam, partial=True)
    assert [o.answered for o in outcomes] == [True, False, True]
    assert outcomes[1].failure is not None
    assert "not valid JSON" in outcomes[1].failure
    assert [d.value for o in outcomes for d in o.drafts] == [_SPECIES[0], _SPECIES[2]]


# ---------- the near-identical collapse: read once, fan by grep, and price the plate that differs ---
# 1440 sample records that differ only in an accession are semantically identical and LEXICALLY
# distinct, so `_deduplicated` — which keys on the whole text — misses them entirely. What follows
# pins the three rules that replace it: group by token skeleton, withhold only a member whose
# distinctive tokens are its own accession, and fan a claim iff its quote touches no variant.

_FAN_EXTRACTOR = ExtractorProvenance(model_id="test/fan", prompt_version="v1")


def _twins(
    prose: list[str], *, level: RecordLevel = "experiment", label: str = "design"
) -> ArchiveRecordSet:
    """One record per entry of ``prose``, at one level, under a project.

    Experiment records by default, because they are the only records a plan sends.

    **The accessions are deliberately of different lengths** (`SRX1`, `SRX22`, `SRX333`). An
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
    assert plan.n_records_read == 4
    assert plan.n_records_collapsed == 3
    exemplar = plan.documents[0]
    assert plan.stands_for(exemplar) == ("SRX1", "SRX22", "SRX333", "SRX4444")
    assert plan.report().documents[0].members == list(plan.stands_for(exemplar))
    assert plan.report().documents[0].reduced_members == [], "nothing here was sent its difference"
    assert [m.value for m in exemplar.variants] == ["SRX1"], "only the accession varies"


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

    assert same.n_documents == 1
    assert same.n_records_collapsed == 3
    assert same.n_records_reduced == 0, "nothing distinctive to send; the accession is ours"

    assert one_differs.n_documents == 4, (
        "every record is asked — the bill is the information content"
    )
    assert one_differs.n_records_collapsed == 0, "nothing is withheld, because nothing is silent"
    assert one_differs.n_records_reduced == 3
    # ...and what each of them is asked is its own value, which is what makes the ask worth making.
    sent = {d.subject: d.text for d in one_differs.documents}
    assert "day7" in sent["SRX4444"]
    assert "day3" in sent["SRX22"]
    assert one_differs.n_chars < 4 * len(sent["SRX1"]), "and the shared prose is read once"


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

    assert exemplar.members == ["SRX1"], "it is the only reading of its own record and no other"
    assert exemplar.reduced_members == ["SRX22", "SRX333", "SRX4444"]
    # ...and each of those is right here, in the same report, with the bytes it cost.
    assert [d.members for d in reduced] == [["SRX22"], ["SRX333"], ["SRX4444"]]
    assert all(d.n_chars > 0 and d.reduced_members == [] for d in reduced)

    assert sum(len(d.members) for d in report.documents) == report.n_records_read == 4

    # One group holding one of each, which is the case a single list cannot describe at all. SRX22's
    # alias is nothing but the accession we ourselves wrote, so it is withheld — read only through the
    # exemplar. SRX333 says `day7`, so its difference is sent and it is read in a document of its own.
    mixed = plan_extraction(
        records=_twins(["day3", "SRX22", "day7"], label="sample_alias")
    ).report()

    assert mixed.documents[0].members == ["SRX1", "SRX22"], "withheld, so read only here"
    assert mixed.documents[0].reduced_members == ["SRX333"], "sent, so read over there"
    assert sum(len(d.members) for d in mixed.documents) == mixed.n_records_read == 3


def test_a_claim_fans_out_only_where_its_quote_touches_no_variant() -> None:
    """The fan-out predicate, both directions.

    A quote lying entirely inside spans byte-identical across the group greps into every member by
    construction. A quote reaching into a variant speaks for the member we sent and nothing else —
    which is what stops a value fanning onto the record that differed.
    """
    from seqforge.harvest import fan_claims, plan_extraction

    plan = plan_extraction(records=_twins([f"{_QUOTE}, N2 whole worm"] * 3))
    shared = fan_claims(_verified(plan, "library.chemistry", "10x-3p-gex-v3", _QUOTE), plan)
    named = fan_claims(
        _verified(
            plan, "library.chemistry", "10x-3p-gex-v3", f"experiment SRX1\n\ndesign: {_QUOTE}"
        ),
        plan,
    )

    assert [f.n_records for f in shared.fanned] == [3]
    assert named.fanned == [], "the quote carries the accession, which is exactly what varies"
    assert len(named.assertions) == 1


def test_a_dataset_scoped_claim_stays_one_assertion_and_buys_a_proof_of_unanimity() -> None:
    """`library.chemistry` is dataset-scoped, and `chemistry_hypothesis` reduces N identical claims to
    one regardless — so copying it onto 1440 records would buy nothing. What the grep buys here is the
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
    assert [f.n_records for f in fan.fanned] == [4]
    said, claim = fan.assertions[0], fan.fanned[0]
    assert claim.assertion_id == said.id, "the claim's report names the assertion it is"
    assert fan.fanned[0].records == ("SRX1", "SRX22", "SRX333", "SRX4444")


def test_the_age_hazard_never_fans_a_value_into_the_record_that_said_something_else() -> None:
    """The character-granularity bug, at the level a user would meet it.

    Three records say `v3` and one says `v30`. At character granularity `v3` is shared, so the claim
    would fan onto the record that said v30 and the leftover variant would be a single `0` nothing
    would ever send. At token granularity the position varies, every record is asked, and no claim
    fans anywhere.
    """
    from seqforge.harvest import fan_claims, plan_extraction

    plan = plan_extraction(records=_twins([_QUOTE] * 3 + [f"{_QUOTE}0"]))
    fan = fan_claims(_verified(plan, "library.chemistry", "10x-3p-gex-v3", _QUOTE), plan)

    assert plan.n_documents == 4, "the record that differed is asked, not guessed at"
    assert plan.n_records_collapsed == 0, (
        "nothing is withheld: every member's `v3`/`v30` is askable"
    )
    # The group DOES form and the marks ARE computed — which is what makes this the real test. The
    # claim does not fan because its quote reaches into the token that varies, not because no group
    # was found. At character granularity `3` would have been shared and the fan would be silent.
    assert plan.collapsed
    assert plan.documents[0].variants
    assert fan.fanned == []
    assert len(fan.assertions) == 1
    assert "v30" in {d.text.split()[-1] for d in plan.documents}, (
        "the odd record is asked its own v30"
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
    assert plan.collapsed == {}
    assert [d.variants for d in plan.documents] == [(), ()]


def test_the_collapse_is_at_plan_time_so_the_dry_run_is_the_bill_the_paid_run_pays() -> None:
    """Collapse at send time and `harvest extract --dry-run` and `eval plan` both report a bill nobody
    pays — which is this workstream's own test, since the dry run's whole claim is that it is the same
    list the paid run sends rather than a projection of one."""
    from seqforge.harvest import extract_planned, plan_extraction

    plan = plan_extraction(records=_twins(["whole worm, day3"] * 6))
    report = plan.report()

    fake, seam = _over(
        _AnswersEveryDocument({d.doc_sha256: "Mus musculus" for d in plan.documents})
    )
    extract_planned(plan, kb.load_all_specs(), seam=seam, partial=True)

    assert report.n_documents == plan.n_documents == 1
    assert len(_sent(fake)) == report.n_requests == 1, "one document sent, one request made"
    assert report.n_records_collapsed == 5


def test_a_collapsed_member_is_rendered_and_kept_even_though_it_is_never_sent() -> None:
    """`all_documents` is what must reach disk, as against `documents`, which is what is paid for.

    A fanned assertion cites a document nobody sent. Its bytes exist nowhere else — we made them —
    and `resolve` drops a claim whose document has no subject, so both the text and the subject have
    to survive the process (ADR-0031).
    """
    from seqforge.harvest import plan_extraction

    plan = plan_extraction(records=_twins(["whole worm, day3"] * 3))

    assert len(plan.all_documents) == 3
    assert plan.n_documents == 1
    assert plan.all_documents[0] is plan.documents[0]
    assert [d.subject for d in plan.all_documents] == ["SRX1", "SRX22", "SRX333"]
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
