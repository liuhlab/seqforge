"""`seqforge harvest` -- prose/metadata -> span-verified Assertions (the one LLM touchpoint).

`_harvest_extract_pipeline` is the stage body, returned as a value so `seqforge run` can chain it.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import typer
from pydantic import ValidationError

from ..manifest import instructions_from_assertions
from ..workspace import documents_dir, logs_dir, readable
from ._common import _emit, _StageOut
from .root import harvest_app

if TYPE_CHECKING:
    from ..harvest.normalize import PdfBackend


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

    backend = cast("PdfBackend", pdf_backend.value)
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


def _roled(docs: list[Path] | None, instruction: list[Path] | None) -> list[tuple[Path, str]]:
    """Pair each document with the ROLE its flag assigned. Code owns role; a filename never does."""
    pairs: list[tuple[Path, str]] = [(d, "reference") for d in (docs or [])]
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
        None, "--model", help="Override the model (default: the provider's own default)."
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
    ANTHROPIC_API_KEY. Exit 1 if the LLM surface is unavailable, 4 if any claim fails verification.

    **`--records` is how a claim names a sample.** Each archive record is rendered as its own
    document and asked only what a record at that level can answer: a BioSample's document is asked
    for sample attributes and never for a chemistry; an experiment's protocol paragraph is asked for
    the chemistry and nothing else. Since a sample's document contains one sample's prose, "which
    sample" is answered by which file we handed the model — the model never names one, and cannot.
    """
    _emit(
        _harvest_extract_pipeline(
            docs=docs,
            instruction=instruction,
            records_path=records_path,
            provider=provider,
            model=model,
            verify=verify,
            workspace=workspace,
            pdf_backend=cast("PdfBackend", pdf_backend.value),
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
) -> _StageOut:
    """The body of ``harvest extract``, returned as a value so ``seqforge run`` can chain it.

    The one LLM stage, and the one place ``run`` cannot be fully deterministic — hence ``--no-llm``,
    which is the caller choosing not to enter here at all. Every exit is a ``_StageOut``: exit 1 if no
    provider or the endpoint fails, exit 4 if a claim fails the span tripwire (a rejected claim needs
    a human, not a silent drop). On success it still writes ``assertions.json`` and the rendered
    documents to disk, because a span citation is only checkable while the exact text survives.
    """
    from ..harvest import (
        ExtractionOutcome,
        ExtractUnavailable,
        NormalizedDoc,
        ProviderUnavailable,
        UnreadableDocument,
        extract_drafts,
        has_prose,
        normalize_document,
        normalize_record,
        resolve_provider,
        verify_drafts,
    )
    from ..kb import load_all_specs
    from ..models.records import ArchiveRecordSet

    specs = load_all_specs()
    logs = logs_dir(workspace)
    logs.mkdir(parents=True, exist_ok=True)
    try:
        llm = resolve_provider(provider)
    except ProviderUnavailable as exc:
        return _StageOut({"error": "no_provider", "detail": str(exc)}, 1, err=True)
    chosen = model or llm.default_model()

    all_drafts = []
    normalized = []
    usage_total: dict[str, int] = {}
    extractor = None
    sources: list[tuple[object, str]] = [(d, r) for d, r in _roled(docs, instruction)]
    for doc, role in sources:
        try:
            nd = normalize_document(doc, role=role, pdf_backend=pdf_backend)  # type: ignore[arg-type]
        except UnreadableDocument as exc:
            # A document that yields no quotable text is a refusal, not a silent empty extraction:
            # surface it with a nonzero exit exactly like a missing provider, so `run` halts here
            # rather than emitting a manifest that is silent about a paper it could not read.
            return _StageOut(
                {"error": "unreadable_document", "detail": str(exc), "document": str(doc)},
                1,
                err=True,
            )
        normalized.append(nd)

    if records_path is not None:
        records = ArchiveRecordSet.model_validate_json(records_path.read_text())
        # Only records that HAVE prose, and only levels we ask anything of. A record with an empty
        # ask costs an API call to be told nothing; `fields_for` already knows which those are.
        from ..harvest.fields import fields_for

        for record in records.records:
            if has_prose(record) and fields_for(record.level, "reference"):
                normalized.append(normalize_record(record))

    # The one place `run` cannot be deterministic, and now the one it need not be slow. Each document
    # is an independent, network-bound LLM call, so they go out concurrently on a THREAD pool (I/O, so
    # threads release the GIL — processes would only add IPC). Results are reassembled in `normalized`
    # order below, so assertions.json is byte-identical no matter which call returned first.
    def _extract(nd: NormalizedDoc) -> ExtractionOutcome:
        return extract_drafts(nd, specs, provider=llm, model=chosen)

    outcomes: dict[str, ExtractionOutcome] = {}
    try:
        if len(normalized) > 1:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=min(8, len(normalized))) as pool:
                futures = {pool.submit(_extract, nd): nd for nd in normalized}
                for fut in futures:
                    outcomes[futures[fut].doc_sha256] = fut.result()
        else:
            for nd in normalized:
                outcomes[nd.doc_sha256] = _extract(nd)
    except ExtractUnavailable as exc:
        return _StageOut({"error": "llm_unavailable", "detail": str(exc)}, 1, err=True)

    usage_records: list[dict[str, object]] = []
    for nd in normalized:
        outcome = outcomes[nd.doc_sha256]
        all_drafts.extend(outcome.drafts)
        extractor = outcome.extractor
        for k, v in outcome.usage.items():
            usage_total[k] = usage_total.get(k, 0) + v
        usage_records.append(
            {
                "document": {"scope": nd.scope, "subject": nd.subject, "doc_sha256": nd.doc_sha256},
                "provider": outcome.provider,
                "model": outcome.model,
                "mode": outcome.mode,
                "usage": outcome.usage,
            }
        )

    # The cost ledger (disk is state). Written whether or not we go on to verify, because the call
    # happened and cost tokens regardless. `n_calls` is per-document; `cache_read_tokens > 0` means the
    # stable KB prefix was served from cache, so a second run over the same documents is much cheaper.
    (logs / "usage.json").write_text(
        json.dumps(
            {
                "provider": llm.name,
                "model": chosen,
                "prompt_version": extractor.prompt_version if extractor else None,
                "totals": {**usage_total, "n_calls": len(normalized)},
                "calls": usage_records,
            },
            indent=2,
        )
    )

    payload: dict[str, object] = {
        "provider": llm.name,
        "model": chosen,
        "n_drafts": len(all_drafts),
        "usage": {**usage_total, "n_calls": len(normalized)},
        "usage_by_document": usage_records,
        "usage_path": str(logs / "usage.json"),
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

    normalized = [
        normalize_document(d, pdf_backend=cast("PdfBackend", pdf_backend.value)) for d in docs
    ]
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
