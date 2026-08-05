"""`seqforge records` -- the record set: which files compile together, and nothing else.

**Top-level, and deliberately not under `io`.** `io` is this compiler's only network surface, and a
reader who sees a verb there is entitled to assume it reaches out. Both verbs here read a local
directory and a local file; `new` writes one. Filing them under `io` would cost that group the one
property that makes it worth having as a group, in exchange for the accident that the noun `records`
already appears there. `seqforge io records <accession>` stays exactly where it is: it *fetches* a
transcript from an archive, and this group is about the file a human types when there is no archive
to fetch from.

**What the two verbs are for.** A filename cannot say that two runs are one library — `_S1` and `_S3`
on one flowcell are two libraries or one library resequenced for depth, and nothing in the name tells
those apart, so both compile as two samples at partial depth and exit 0. `records new` drafts the file
that can say it, and drafts it as a no-op so writing it into somebody's dataset directory changes no
answer. `records validate` exists so that a refusal on a hand-written file is legible: a mistyped
record set is user input, and user input that cannot be used is exactly what this compiler's exit
codes are for.

**Why validate answers with more than `ok`.** A record set is the one input here nobody can check by
re-reading — it declares a grouping, and a grouping that parses is not the same as a grouping that
says what its author meant. So the result carries what actually loaded: the source, the record count
at each level, the files claimed, and, for a hand-written set, every sample that more than one run
points at. That last one is the whole reason the file exists, and it is the line an author most needs
to see printed back at them (`docs/adr/0034`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import typer

from ..manifest import exit_code_for_report
from ..models.records import USER_SOURCE, ArchiveRecordSet, RecordLevel
from ..models.resolve import RecordSetResult, RecordSetSummary, ValidationReport
from ..recordset import RecordSetError, draft_record_set, load_record_set
from .root import records_app

#: The archive's four levels, annotated so the summary counts them all rather than only the two a
#: hand-written set may declare. A set that reports `experiment: 0` and one that never had the level
#: at all look identical to a caller unless every level is always reported.
_LEVELS: tuple[RecordLevel, ...] = ("project", "sample", "experiment", "run")


def _summarize(records: ArchiveRecordSet) -> RecordSetSummary:
    """Describe a loaded set in the terms its author wrote it in."""
    fused: dict[str, list[str]] = {}
    if records.source == USER_SOURCE:
        claimed: dict[str, list[str]] = {}
        for run in records.at("run"):
            if run.parent is not None:
                claimed.setdefault(run.parent, []).append(run.accession)
        fused = {parent: runs for parent, runs in claimed.items() if len(runs) > 1}
    return RecordSetSummary(
        source=records.source,
        query=records.query,
        n={level: len(records.at(level)) for level in _LEVELS},
        n_filenames=sum(len(record.filenames) for record in records.records),
        fused=fused,
    )


def _clean() -> ValidationReport:
    """The report for a set that loaded. Built rather than implied, so the exit code has a source."""
    return ValidationReport(ok=True, blockers=[], conflicts=[])


def _refuse(exc: RecordSetError) -> NoReturn:
    """Print a refusal on the human stream and exit on it.

    Prose and not JSON, because this is the branch with no result object: `records new` answers on
    stdout with the drafted YAML or with an envelope naming what it wrote, and a refusal is neither.
    Every blocker is printed with its remedy, since the remedy is the half that says what to type --
    a message alone leaves a caller knowing they were refused and not what to do about it.
    """
    for blocker in exc.blockers:
        typer.echo(f"{blocker.id}: {blocker.message}", err=True)
        typer.echo(f"  remedy: {blocker.remedy}", err=True)
    raise typer.Exit(exit_code_for_report(exc.report))


@records_app.command("new")
def records_new(
    fastq_dir: Path = typer.Argument(..., help="Directory holding this dataset's FASTQ files."),
    out: Path | None = typer.Option(None, "-o", "--out", help="Write here (default: stdout)."),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing --out file (refused by default)."
    ),
) -> None:
    """Draft a record set over a directory of FASTQ. Applying it unedited changes nothing.

    It declares one sample per run -- the grouping seqforge already derives from these filenames -- so
    the file it writes cannot move an answer, only record one. What it adds is comments: the run pairs
    differing only in a sample-sheet `_S<n>` token, or only in a flowcell id, are the two shapes a
    filename provably cannot resolve, and each is named beside the record whose `parent` would settle
    it. It makes no guess; it points at the decision, which is yours.

    With no -o the YAML goes to stdout, so it can be piped or read before it is kept. With -o it is
    written there and stdout carries a JSON envelope naming the file and what it says.

    It declares STRUCTURE ONLY -- no strain, no tissue, no genotype. A fact typed into this file has
    no document to grep and no span to verify, and it would outrank a claim that has both. Write those
    in a README and run `seqforge harvest` over it.
    """
    # Checked before anything is read, because it is a fact about the invocation and not about the
    # data: a caller who mistyped -o should be told so whether or not the directory they named holds
    # FASTQ. A record set is EDITED -- the whole point of the file is the grouping a human decided in
    # it -- so silently replacing one with a fresh draft would discard exactly the work this verb
    # exists to make possible, and it would do it on a re-run of the same command that created it.
    if out is not None and out.exists() and not force:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "reason": (
                        f"{out} already exists and a draft would replace it; re-run with --force to "
                        f"do that. A record set is edited by hand and a draft is one sample per run, "
                        f"so overwriting one discards whatever grouping was decided in it."
                    ),
                },
                indent=2,
            ),
            err=True,
        )
        raise typer.Exit(2)

    try:
        drafted = draft_record_set(fastq_dir)
    except RecordSetError as exc:
        _refuse(exc)

    if out is None:
        typer.echo(drafted)
        raise typer.Exit(0)

    out.write_text(drafted)
    try:
        loaded = load_record_set(out)
    except RecordSetError as exc:
        # Read back rather than described from the text we just formatted: the envelope's counts are
        # then a statement about the bytes on disk, and `records new -o x` followed by
        # `records validate x` cannot disagree. A draft that does not load is OUR defect and not a
        # domain refusal, which is why this exits 1 and says so rather than exiting 3 as though the
        # caller had typed something wrong.
        typer.echo(
            f"the draft written to {out} does not load, which is a bug in seqforge:", err=True
        )
        for blocker in exc.blockers:
            typer.echo(f"  {blocker.id}: {blocker.message}", err=True)
        raise typer.Exit(1) from exc

    result = RecordSetResult(records=str(out), report=_clean(), summary=_summarize(loaded))
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    raise typer.Exit(exit_code_for_report(result.report))


@records_app.command("validate")
def records_validate(
    records_path: Path = typer.Argument(..., help="Path to a record set (.yaml or .json)."),
) -> None:
    """Validate a record set and say what it declares. Exit 3 on a Blocker.

    Both dialects go through one loader, so this reads a cache `seqforge io records` wrote and a file
    a human typed with no flag to pick between them -- `source` decides, never the extension.

    On success the answer is not merely `ok`. A record set that parses is not yet a record set that
    says what its author meant, so the summary prints back the counts, the files claimed, and every
    sample that more than one run was declared to belong to.
    """
    try:
        loaded = load_record_set(records_path)
    except RecordSetError as exc:
        # On stdout, unlike every other refusal in this module: here the report IS the result object.
        # `manifest fill --records` puts the same blockers on stderr because its result is a manifest
        # and a refusal is not one; this verb was asked for the verdict and nothing else.
        refused = RecordSetResult(records=str(records_path), report=exc.report)
        typer.echo(json.dumps(refused.model_dump(mode="json"), indent=2))
        raise typer.Exit(exit_code_for_report(refused.report)) from exc

    result = RecordSetResult(records=str(records_path), report=_clean(), summary=_summarize(loaded))
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    raise typer.Exit(exit_code_for_report(result.report))
