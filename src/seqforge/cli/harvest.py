"""`seqforge harvest` -- prose/metadata -> span-verified Assertions (the one LLM touchpoint).

`_harvest_extract_pipeline` is the stage body, returned as a value so `seqforge run` can chain it.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from pydantic import ValidationError

from ..manifest import instructions_from_assertions
from ..workspace import documents_dir, logs_dir, readable
from ._common import _emit, _StageOut
from .root import harvest_app

if TYPE_CHECKING:
    from ..harvest.fields import DocRole
    from ..harvest.meter import TokenMeter
    from ..harvest.normalize import PdfBackend
    from ..models.assertion import ExtractorProvenance


class PdfBackendChoice(StrEnum):
    """Which engine opens a PDF, exposed as ``--pdf-backend``. ``pymupdf`` (AGPL-3.0) is the default
    because it read every real manuscript in the eval, including ones ``pypdf`` (BSD) cannot parse;
    ``pypdf`` stays as the permissive fallback. Neither reorders geometrically — tables come from
    pdfplumber either way, so the choice is really which reader survives more files."""

    pypdf = "pypdf"
    pymupdf = "pymupdf"


@harvest_app.command("normalize")
def harvest_normalize(
    docs: list[Path] = typer.Argument(
        None, help="Reference documents to cite (.txt/.md/.pdf/.xlsx)."
    ),
    instruction: list[Path] = typer.Option(
        [],
        "--instruction",
        help="Document(s) authored FOR seqforge (e.g. alignment_instruction.md).",
    ),
    pdf_backend: PdfBackendChoice = typer.Option(
        PdfBackendChoice.pymupdf,
        "--pdf-backend",
        help="PDF text extractor: pymupdf (default, AGPL, reads more PDFs) | pypdf (BSD fallback).",
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
) -> None:
    """Extract each document ONCE into the canonical text that spans are computed against.

    A document's ROLE is the flag it arrived under, never its filename: only an --instruction document
    may set processing.*. `alignment_instruction.md` is a convention you pass here, load-bearing
    nowhere — a filename trigger would be spoofable by renaming a downloaded PDF.
    """
    from ..harvest import normalize_document

    backend: PdfBackend = pdf_backend.value
    outdir = documents_dir(workspace)
    outdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for doc, role in _roled(docs, instruction):
        try:
            nd = normalize_document(doc, role=role, pdf_backend=backend)
        except (OSError, RuntimeError) as exc:
            typer.echo(f"{doc}: {exc}", err=True)
            raise typer.Exit(1) from exc
        target = outdir / _document_filename(nd)
        target.write_text(nd.text)
        rows.append(
            {
                "source": nd.source_basename,
                "role": nd.role,
                "scope": nd.scope,
                "subject": nd.subject,
                "doc_sha256": nd.doc_sha256,
                "normalized_sha256": nd.normalized_sha256,
                "normalizer_version": nd.normalizer_version,
                "n_chars": nd.n_chars,
                "path": str(target.relative_to(Path(workspace))),
            }
        )
    typer.echo(json.dumps({"normalized": rows}, indent=2))


def _document_filename(doc: Any) -> str:
    """``paper.pdf`` -> ``paper-3f8a1c2d9b04.txt``; a record -> ``sample-SAMN40935621-....txt``.

    The hash stays, because two documents can share a name and the identity is the hash. But a
    directory of bare 64-hex filenames is a directory you cannot read, and an early build's document
    cache was exactly that: nothing in it said which file was the paper. The source
    name is already known -- we opened the file -- so printing it costs nothing and no model is
    involved in producing it.
    """
    return readable(Path(doc.source_basename).stem, doc.doc_sha256) + ".txt"


def _roled(docs: list[Path] | None, instruction: list[Path] | None) -> list[tuple[Path, DocRole]]:
    """Pair each document with the ROLE its flag assigned. Code owns role; a filename never does."""
    pairs: list[tuple[Path, DocRole]] = [(d, "reference") for d in (docs or [])]
    pairs += [(d, "instruction") for d in (instruction or [])]
    if not pairs:
        typer.echo("give at least one document, or --instruction FILE", err=True)
        raise typer.Exit(2)
    return pairs


@harvest_app.command("extract")
def harvest_extract(
    docs: list[Path] = typer.Argument(
        None, help="Reference documents to cite (.txt/.md/.pdf/.xlsx)."
    ),
    instruction: list[Path] = typer.Option(
        [],
        "--instruction",
        help="Document(s) authored FOR seqforge; only these may set processing.*.",
    ),
    records_path: Path | None = typer.Option(
        None,
        "--records",
        help="A record set from `seqforge io records`. Each record's free text becomes its OWN "
        "document, which is how a claim gets to name a sample.",
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="anthropic | deepseek | openai-compatible (default: auto-detect)."
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Override the model (default: the provider's own). DeepSeek serves "
        "deepseek-v4-pro (the default) and deepseek-v4-flash.",
    ),
    ceiling: int = typer.Option(
        0,
        "--ceiling",
        min=0,
        help="Refuse past N tokens spent at the model seam in this run (raw: cached input and "
        "cache writes count too). Blocks with exit 3 rather than warning. 0 = no ceiling.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the extraction PLAN and stop: what will be asked, of what, and an estimated "
        "input-token cost. Reaches no model and needs no credential.",
    ),
    verify: bool = typer.Option(
        True, "--verify/--no-verify", help="Span-verify the drafts immediately."
    ),
    pdf_backend: PdfBackendChoice = typer.Option(
        PdfBackendChoice.pymupdf,
        "--pdf-backend",
        help="PDF text extractor: pymupdf (default, AGPL, reads more PDFs) | pypdf (BSD fallback).",
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
) -> None:
    """The ONE LLM touchpoint: prose -> AssertionDraft[] -> (verified) Assertion[].

    The model only proposes `{field, value, quote}`; code computes the offsets and decides what
    survives — which is what makes the provider swappable. Auto-detects DEEPSEEK_API_KEY /
    ANTHROPIC_API_KEY. Exit 1 if the LLM surface is unavailable, 3 if the run reaches its
    `--ceiling`, 4 if any claim fails verification.

    **`--records` is how a claim names a sample.** Each archive record is rendered as its own
    document and asked only what a record at that level can answer: a BioSample's document is asked
    for sample attributes and never for a chemistry; an experiment's protocol paragraph is asked for
    the chemistry and nothing else. Since a sample's document contains one sample's prose, "which
    sample" is answered by which file we handed the model — the model never names one, and cannot. A
    sample's RUNS are one document between them rather than one each: a run belongs to exactly one
    sample, so the claims still name that sample and the exchange count stops scaling with the run count.

    **`--dry-run` answers "what will this cost" without asking anybody.** It renders every document —
    which costs no token and no network — and prints the plan, so the send list you inspected is the
    one a real run would use.
    """
    _emit(
        _harvest_extract_pipeline(
            docs=docs,
            instruction=instruction,
            records_path=records_path,
            provider=provider,
            model=model,
            ceiling=ceiling,
            verify=verify,
            dry_run=dry_run,
            workspace=workspace,
            pdf_backend=pdf_backend.value,
        )
    )


def _harvest_extract_pipeline(
    *,
    docs: list[Path] | None,
    instruction: list[Path] | None,
    records_path: Path | None,
    provider: str | None,
    model: str | None,
    verify: bool,
    workspace: Path,
    pdf_backend: PdfBackend = "pymupdf",
    ceiling: int = 0,
    dry_run: bool = False,
) -> _StageOut:
    """The body of ``harvest extract``, returned as a value so ``seqforge run`` can chain it.

    The one LLM stage, and the one place ``run`` cannot be fully deterministic — hence ``--no-llm``,
    which is the caller choosing not to enter here at all. Every exit is a ``_StageOut``: exit 1 if no
    provider or the endpoint fails, exit 3 with a ``Blocker`` if the run reaches its token
    ``ceiling``, exit 4 if a claim fails the span tripwire (a rejected claim needs a human, not a
    silent drop). On success it still writes ``assertions.json``, the transcript and the rendered
    documents to disk, because a span citation is only checkable while the exact text survives.

    ``dry_run`` returns the plan and stops **before** a provider is even resolved: "what will this
    dataset cost" must be answerable on a machine with no credential at all.
    """
    from ..harvest import (
        CeilingExceeded,
        ExtractUnavailable,
        ProviderUnavailable,
        TokenMeter,
        UnreadableDocument,
        build_system_prompt,
        extract_planned,
        llm_schema,
        normalize_document,
        plan_extraction,
        resolve_provider,
        verify_drafts,
    )
    from ..kb import load_all_specs
    from ..models.records import ArchiveRecordSet

    specs = load_all_specs()
    handed = []
    for doc, role in _roled(docs, instruction):
        try:
            handed.append(normalize_document(doc, role=role, pdf_backend=pdf_backend))
        except UnreadableDocument as exc:
            # A document that yields no quotable text is a refusal, not a silent empty extraction:
            # surface it with a nonzero exit exactly like a missing provider, so `run` halts here
            # rather than emitting a manifest that is silent about a paper it could not read.
            return _StageOut(
                {"error": "unreadable_document", "detail": str(exc), "document": str(doc)},
                1,
                err=True,
            )

    dataset_ref = "dataset"
    records = None
    if records_path is not None:
        records = ArchiveRecordSet.model_validate_json(records_path.read_text())
        dataset_ref = records.query or dataset_ref

    # Which documents exist, and what each will be asked, is one module's decision — the eval harness
    # makes the same call. Rendering costs nothing, so the plan is the send list rather than a
    # projection of one, and `--dry-run` hands it back before a provider is resolved.
    plan = plan_extraction(
        documents=handed,
        records=records,
        system_prompt_chars=len(build_system_prompt(specs, llm_schema())),
    )
    if dry_run:
        return _StageOut(plan.report().model_dump(mode="json"), 0)

    logs = logs_dir(workspace)
    logs.mkdir(parents=True, exist_ok=True)
    try:
        resolved = resolve_provider(provider)
    except ProviderUnavailable as exc:
        return _StageOut({"error": "no_provider", "detail": str(exc)}, 1, err=True)
    chosen = model or resolved.default_model()

    all_drafts = []
    normalized = list(plan.documents)
    extractor = None

    # Every request goes through the meter, and only the meter counts. It proxies the provider's
    # identity (`name`, `default_model`), so provenance still records who actually answered.
    llm = TokenMeter(resolved, ceiling=ceiling, subject=dataset_ref)

    usage_records: list[dict[str, object]] = []
    try:
        outcomes = extract_planned(plan, specs, provider=llm, model=chosen)
    except CeilingExceeded as exc:
        # A refusal, not a failure: the provider was fine and another attempt would only spend more.
        # The ledger and the transcript are written first, because the tokens up to the ceiling were
        # really spent and a reader asking "on what?" is exactly the reader a breach produces.
        _write_usage(logs, llm, chosen, None, usage_records, len(normalized))
        return _StageOut(
            {
                "error": "token_ceiling_exceeded",
                "detail": str(exc),
                "blockers": [exc.blocker().model_dump(mode="json")],
                "usage": {**llm.usage(), "n_calls": llm.n_exchanges},
                "usage_path": str(logs / "usage.json"),
                "transcript_path": str(_write_transcript(logs, llm)),
            },
            # stdout, like every other refusal: a `blockers` list IS the result object, and a caller
            # that has to parse stderr to learn why a run stopped has no contract at all.
            3,
        )
    except ExtractUnavailable as exc:
        return _StageOut({"error": "llm_unavailable", "detail": str(exc)}, 1, err=True)

    extract_rejected: list[dict[str, object]] = []
    # Positional, never keyed by document identity: two documents that render identically are one
    # document to the plan, and a dict keyed by `doc_sha256` used to pay for both, keep one result
    # and then read it once per collider — duplicating its drafts, its rejections and its usage.
    for nd, outcome in zip(normalized, outcomes, strict=True):
        all_drafts.extend(outcome.drafts)
        extract_rejected.extend(outcome.rejected)
        extractor = outcome.extractor
        usage_records.append(
            {
                "document": {"scope": nd.scope, "subject": nd.subject, "doc_sha256": nd.doc_sha256},
                "provider": outcome.provider,
                "model": outcome.model,
                "mode": outcome.mode,
                "usage": outcome.usage,
            }
        )

    _write_usage(logs, llm, chosen, extractor, usage_records, len(normalized))
    payload: dict[str, object] = {
        "provider": llm.name,
        "model": chosen,
        "n_drafts": len(all_drafts),
        # Drafts the model returned malformed (e.g. `value: null`) — dropped, not fatal (#5). Surfaced
        # so a run is not silent about a batch that was partly lost, but never an exit code: a flaky
        # token is a provider hiccup we tolerate, not a claim a human must weigh in on.
        "n_extract_rejected": len(extract_rejected),
        "extract_rejected": extract_rejected,
        "usage": {**llm.usage(), "n_calls": llm.n_exchanges, "n_documents": len(normalized)},
        "usage_by_document": usage_records,
        "usage_path": str(logs / "usage.json"),
        # The transcript, at an address. `usage.json` says what the run spent; this says what it
        # spent it on, and it is a PATH on stdout rather than the text: stdout is the result object,
        # and a thousand exchanges cannot ride on it.
        "transcript_path": str(_write_transcript(logs, llm)),
        "drafts": [d.model_dump(mode="json") for d in all_drafts],
    }
    if not verify:
        return _StageOut(payload, 0)

    assert extractor is not None
    report = verify_drafts(all_drafts, normalized, extractor=extractor)
    instruction_docs = frozenset(d.doc_sha256 for d in normalized if d.role == "instruction")
    # An OBJECT, not a bare list, and the `instruction_docs` key is the reason. Which documents were
    # authored FOR seqforge is what decides whether an assertion may touch `processing.*` --
    # and it lived only in this process's memory, so the artifact could not reconstruct the
    # instructable surface and `processing new` had no way to consume it. The join existed in
    # `fill_processing` the whole time and nothing could reach it.
    # `document_subjects` is the same idea one level up: which RECORD each document was rendered
    # from. It is what lets `manifest fill` tell a sample's own alias (a declaration about that
    # sample) from a paper about six samples (an inference about each), and it too lived only in this
    # process's memory. Code owns both mappings because code chose both documents.
    (logs / "assertions.json").write_text(
        json.dumps(
            {
                "instruction_docs": sorted(instruction_docs),
                "document_subjects": [
                    {"doc_sha256": d.doc_sha256, "scope": d.scope, "subject": d.subject}
                    for d in sorted(normalized, key=lambda d: d.doc_sha256)
                ],
                "assertions": [a.model_dump(mode="json") for a in report.assertions],
            },
            indent=2,
        )
    )
    # The rendered documents, on disk, under readable names. A span citation is only checkable if the
    # exact text it was greppedded against still exists -- and for a record-derived document these
    # bytes exist nowhere else, because we made them.
    docdir = documents_dir(workspace)
    docdir.mkdir(parents=True, exist_ok=True)
    for nd in normalized:
        (docdir / _document_filename(nd)).write_text(nd.text)
    payload["n_accepted"] = report.n_accepted
    payload["n_rejected"] = len(report.rejected)
    # what the user may act on: verified directives, projected onto the instructable surface
    instructions, conflicts = instructions_from_assertions(
        report.assertions, instruction_docs=instruction_docs
    )
    payload["instructions"] = [
        {"field": i.field, "value": i.value, "basis": i.basis, "evidence": i.evidence}
        for i in instructions
    ]
    payload["conflicts"] = [c.model_dump(mode="json") for c in conflicts]
    payload["rejected"] = report.rejected
    payload["assertions"] = [a.model_dump(mode="json") for a in report.assertions]
    # Exit 4 when the author must weigh in: two instructions disagreeing has no tiebreak, and a claim
    # that failed the span tripwire needs a human rather than a silent drop.
    code = 4 if (conflicts or report.rejected) else 0
    return _StageOut(payload, code)


def _write_transcript(logs: Path, meter: TokenMeter) -> Path:
    """The run's exchanges, on disk beside the ledger that says what they cost.

    Nothing anywhere used to persist a prompt or a response: ``usage.json`` records the shape of every
    call and no text, and ``records/documents/`` holds the document but never the message. So "why did
    the model say that" was unanswerable the moment the process exited, which is exactly when it is
    asked. Written whether or not verification follows, and written on a ceiling breach too — a run
    that stopped mid-way is the one whose transcript is most worth reading.
    """
    from ..harvest import TRANSCRIPT_FILENAME, write_transcript

    return write_transcript(logs / TRANSCRIPT_FILENAME, meter.transcript())


def _write_usage(
    logs: Path,
    meter: TokenMeter,
    model: str,
    extractor: ExtractorProvenance | None,
    rows: list[dict[str, object]],
    n_documents: int,
) -> None:
    """The cost ledger (disk is state), written whether or not we go on to verify — the calls
    happened and cost tokens regardless, and a run refused at its ceiling is the one that most needs
    to say what it spent.

    ``n_calls`` is the meter's count of real REQUESTS, retries included. It used to be the document
    count, which made every retry free in the only place a reader would look. ``n_documents`` keeps
    the old number beside it, because "983 documents, 1002 requests" is two facts and neither
    substitutes for the other. ``cache_read_tokens > 0`` means the stable KB prefix was served from
    cache, so a second run over the same documents is much cheaper.
    """
    (logs / "usage.json").write_text(
        json.dumps(
            {
                "provider": meter.name,
                "model": model,
                "prompt_version": extractor.prompt_version if extractor else None,
                "totals": {
                    **meter.usage(),
                    "n_calls": meter.n_exchanges,
                    "n_documents": n_documents,
                },
                "calls": rows,
            },
            indent=2,
        )
    )


@harvest_app.command("verify")
def harvest_verify(
    drafts_json: Path = typer.Argument(..., help="AssertionDraft[] JSON (from `harvest extract`)."),
    docs: list[Path] = typer.Option(..., "--doc", help="Source document(s) the drafts cite."),
    model_id: str = typer.Option("unknown", help="Model that produced the drafts (provenance)."),
    prompt_version: str = typer.Option("unknown", help="Prompt version (provenance)."),
    pdf_backend: PdfBackendChoice = typer.Option(
        PdfBackendChoice.pymupdf,
        "--pdf-backend",
        help="PDF extractor — must match the one `extract` used, or the canonical text differs.",
    ),
) -> None:
    """Grep each quote back into the canonical text + check it entails the value. Exit 4 if any fail.

    Both flags are code-owned, so a hallucinated or mis-attributed claim fails closed.
    """
    from ..harvest import normalize_document, verify_drafts
    from ..models.assertion import AssertionDraft, ExtractorProvenance

    try:
        raw = json.loads(drafts_json.read_text())
        drafts = [AssertionDraft.model_validate(d) for d in raw]
    except (OSError, ValidationError, ValueError) as exc:
        typer.echo(f"cannot read drafts {drafts_json}: {exc}", err=True)
        raise typer.Exit(2) from exc

    normalized = [normalize_document(d, pdf_backend=pdf_backend.value) for d in docs]
    report = verify_drafts(
        drafts,
        normalized,
        extractor=ExtractorProvenance(model_id=model_id, prompt_version=prompt_version),
    )
    typer.echo(
        json.dumps(
            {
                "n_drafts": len(drafts),
                "n_accepted": report.n_accepted,
                "n_rejected": len(report.rejected),
                "assertions": [a.model_dump(mode="json") for a in report.assertions],
                "rejected": report.rejected,
            },
            indent=2,
        )
    )
    if report.rejected:
        raise typer.Exit(4)  # a rejected claim needs a human, not a silent drop
