"""The metadata resolver: archive records -> which sample each file is. A sibling of ``score``.

``score`` answers "what is this library?" from bytes. This answers "which sample is each file?" from
records. They are siblings rather than one stage because they have the same ways of being wrong and
therefore need the same discipline: both can refuse outright. What a sample WAS (its tissue, strain
or treatment) is not this stage's question: a study reading holds those.

**This resolver is never shown the probe**, and the two must never talk to each other. The tempting
version reads the probe's output too ("the reads are 28+94, so the protocol paragraph saying 28+94 is
corroborated"), and it is wrong twice. A probe-sighted reader would settle ties the probe itself
created and log the wrong reason: nothing records corroboration, so the manifest would say "asserted"
for a fact a byte decided, and the rung provenance would be a lie. And read lengths say nothing about
which sample a file is: the join is made by names the record declares, and the probe has nothing to
add to it. The chemistry hypothesis is the legitimate half of the same idea and goes the other way:
prose steers which whitelist ``score`` checks first, and never enters the evidence matrix.

**The join is code's, at every level.** run -> experiment -> sample -> project comes out of the
record, by accession; record-run -> file-on-disk comes out of the run accession in the filename or
the original filenames the record declares. A language model is never asked which sample a file is,
and never could be: it is not shown the files. The archive also publishes a *size* beside each of
those names, and it checks a join without ever making one (ADR-0033): a name is a fact the submitter
typed and a size is two numbers agreeing, so joining on the second would lay a guess over the first.
Where the name made the join, though, the file on disk is claiming to BE that submitted file, and a
size that disagrees is worth saying out loud — as a warning, because this stage decides.

**No archive is the normal case, not the degraded one.** Most sequencing data has never had an
accession and never will: a freshly sequenced plate on a lab filesystem has no BioProject, no
BioSample, and no submitter alias. With no record, sample identity falls back to the run grouping
(filenames group; they always did). Nothing here refuses for lack of a record. The refusal is
narrower and it is real: a record that *exists* and does not account for the files on disk is a
broken join, and half-joining would leave some files with no sample while the manifest still read as
though it described them all.

**And a record is not always an archive's.** ``source`` says who declared the set, and a ``user`` one
is a human describing their own pre-deposit data: structure only — levels, ids, parents, filenames
(ADR-0034). Two messages change for it. Declaring one sample over runs the filenames would have kept
apart is the *reason* such a set gets written, so it is a note rather than a refusal — and a note
only here, since firing it on a deposit would report every ordinary run-to-BioSample fusion. A set
that leaves a file unplaced still refuses, with a remedy naming the file the human wrote rather than
an archive they were never near.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..models.blocker import Blocker, BlockerCode, BlockerSubject, ValidationWarning
from ..models.dataset import ArchiveProject
from ..models.evidenced import EvidencedTaxid
from ..models.observation import FileIdentity
from ..models.records import ArchiveRecord, ArchiveRecordSet, SubmittedFile
from ..models.resolve import MetadataResolution, ResolvedSample
from .group import run_key


@dataclass(frozen=True)
class DocumentSubject:
    """Which record a document was rendered from, or ``dataset`` for one a human staged.

    Mirrors ``instruct.py``'s ``instruction_docs``: a set of ``doc_sha256`` that code assembled
    because code chose the documents. Nothing here is derivable from the document's contents, and
    that is the point — a spoofable subject would be worse than no subject. The wrong-paper guard
    (:mod:`seqforge.resolve.provenance`) reads it to find the documents a human staged.
    """

    doc_sha256: str
    #: What the document is ABOUT, as :data:`seqforge.harvest.fields.DocScope` names it. ``dataset``
    #: is one a human handed us; the guard asks nothing else of it.
    scope: str


def resolve_metadata(
    *,
    files: Sequence[FileIdentity],
    records: ArchiveRecordSet | None = None,
) -> MetadataResolution:
    """Resolve the files into samples, and read the project and organism the records declare.

    Takes ``FileIdentity`` rather than ``Observation`` on purpose. This stage needs a basename and a
    sha256 and nothing else, and being handed the probe's output would mean *promising* not to read
    the signals in it. A signature that cannot see them keeps the promise structurally — the module
    docstring says why it is worth keeping.
    """
    samples, join_notes, blockers = _join(files, records)
    return MetadataResolution(
        samples=[
            ResolvedSample(
                sample_id=sample.sample_id,
                accession=sample.accession,
                file_shas=sample.file_shas,
            )
            for sample in samples
        ],
        project=_archive_project(records),
        organism=_organism(records),
        warnings=join_notes,
        blockers=blockers,
    )


@dataclass(frozen=True)
class _Sample:
    """A joined sample: who it is, and which files carry it."""

    sample_id: str
    accession: str | None
    file_shas: list[str]


def _join(
    files: Sequence[FileIdentity], records: ArchiveRecordSet | None
) -> tuple[list[_Sample], list[ValidationWarning], list[Blocker]]:
    """Files -> samples. The record when there is one, the filenames when there is not.

    Two ways in, and they are not interchangeable — which one matched is what decides whether the
    archive's declared size has anything to say about the file (:func:`_size_disagreement`). Accession
    first, because it is the archive's own identifier and a submitter's filename is only ever the
    fallback for a file that no longer carries one.
    """
    if records is None or not records.at("run"):
        return _join_by_filename(files), [], []

    runs = records.at("run")
    declared_by_user = records.declared_by_hand
    by_accession = {r.accession: r for r in runs}
    # The submitted file itself and not just its name: the size on it is read below, and looking it
    # up a second time from the run would mean re-deciding which of that run's entries matched.
    by_submitted_name: dict[str, tuple[ArchiveRecord, SubmittedFile]] = {}
    for declared in runs:
        for submitted in declared.submitted_files:
            by_submitted_name[submitted.filename] = (declared, submitted)

    grouped: dict[str, list[str]] = {}
    accession_of: dict[str, str | None] = {}
    # What the FILENAMES would have said, kept beside what the records decided. Only ever read for a
    # hand-written set, and only to say where the two disagree (:func:`_fused_runs_note`).
    run_keys_of: dict[str, set[str]] = {}
    unclaimed: list[str] = []
    notes: list[ValidationWarning] = []

    for f in files:
        basename = f.basename
        run = by_accession.get(run_key(basename))
        if run is None:
            claimed = by_submitted_name.get(basename)
            if claimed is None:
                unclaimed.append(basename)
                continue
            run, submitted = claimed
            # ONLY here. A file wearing the name the submitter uploaded under is asserting it is that
            # upload, so the archive's number about that upload is about this file too.
            note = _size_disagreement(f, run, submitted)
            if note is not None:
                notes.append(note)
        sample = records.ancestor(run, "sample")
        # A run whose sample record is missing still has an identity — its own accession. Degraded,
        # and honest about it: the files are grouped correctly, we just cannot say what they are.
        sample_id = sample.accession if sample is not None else run.accession
        grouped.setdefault(sample_id, []).append(f.sha256)
        # A hand-written id is a GROUPING KEY and not a specimen the archive named (this package's
        # `GLOSSARY.md`, **Sample**), so it is not an accession and must not be stored as one —
        # `plate7` does not match the accession pattern and reached this stage as an uncaught
        # validation error rather than as anything a caller could act on. So `accession` stays empty
        # for a set somebody wrote, and for a run with no sample record above it to take one from.
        accession_of[sample_id] = None if declared_by_user or sample is None else sample.accession
        run_keys_of.setdefault(sample_id, set()).add(run_key(basename))

    if unclaimed:
        return [], notes, [_join_blocker(unclaimed, records)]

    if declared_by_user:
        notes.extend(
            _fused_runs_note(sid, run_keys_of[sid])
            for sid in sorted(grouped)
            if len(run_keys_of[sid]) > 1
        )

    return (
        [
            _Sample(
                sample_id=sid,
                accession=accession_of[sid],
                file_shas=sorted(grouped[sid]),
            )
            for sid in sorted(grouped)
        ],
        notes,
        [],
    )


def _size_disagreement(
    file: FileIdentity, run: ArchiveRecord, submitted: SubmittedFile
) -> ValidationWarning | None:
    """The archive says that upload weighed X; this file weighs Y. A note, never anything else.

    A `Warning` is the whole of what this may be, and both halves of that are decisions. It is not a
    `Blocker` because ADR-0010 gives this resolver no refusal that is not a broken join — a compile
    stopped over a byte count would be stopped over a fact no rule downstream reads. And it does not
    touch the join, because the join was made by a name the submitter typed: withdrawing it on a size
    would strand the file with no sample, which is the half-join :func:`_join_blocker` exists to
    prevent, arrived at from the other side.

    It comes from ``stat()`` and reads no FASTQ, which is why it is here at all: the md5 on the same
    record element would answer the same question far better and costs every byte of the file to
    check, so the read budget rules it out and the size catches most of what it would have caught for
    nothing.

    A recompression is the likeliest cause and is harmless; a truncated download is the one worth
    catching; a different file that happens to share a common name (``sample1_R1.fastq.gz``) is the
    one that would silently join the wrong sample. Nothing here can tell them apart, so the
    message reports the two numbers and names all three rather than picking one.
    """
    if submitted.size_bytes is None or submitted.size_bytes == file.size_bytes:
        return None
    return ValidationWarning(
        code="submitted_file_size_mismatch",
        message=(
            f"{file.basename}: joined to {run.accession} by the filename the record declares, but the "
            f"archive says that submitted file is {submitted.size_bytes} bytes and this one is "
            f"{file.size_bytes}. The join stands — the name is the submitter's own and a size does not "
            f"get to unmake it — so this only says the copy on disk may be a recompression, a "
            f"truncated download, or a different file that happens to share the name."
        ),
        subject=BlockerSubject(kind="file", ref=file.basename),
    )


def _fused_runs_note(sample_id: str, keys: set[str]) -> ValidationWarning:
    """A hand-written record set put several filename-runs into one sample. Say so; never refuse.

    Two libraries of the same chemistry declared as one sample is the one shape nothing else catches.
    The gate that refuses a sample spanning two chemistries does not see it — the chemistries agree —
    and :func:`_join_blocker` does not either, because every file was placed. It is also the expensive
    direction of being wrong: a split library gives quarter-depth matrices somebody notices, and a
    fused one gives a single plausible matrix nobody does.

    **It is still a note.** Fusing runs the filenames separate is the *whole point* of writing a
    record set by hand — a library resequenced for saturation is ``_S3`` where batch one was ``_S1``,
    and a library split across two flowcells carries a different flowcell id in every name, so both
    compile as two samples at partial depth until a human says otherwise. Refusing here would refuse
    the feature working, and an exit code that fires when it need not teaches callers to route around
    exit codes. Silence is the other failure — a mistyped ``parent`` would be permanent and invisible
    — and a warning naming what disagreed and how it was settled is this resolver's own instrument
    for a thing it decided rather than deferred.

    **Only for a set a human wrote**, which is what makes ``source`` semantic rather than decorative.
    Every ordinary deposit joins several runs under one BioSample — that is what ``ancestor(run,
    "sample")`` is *for* — so fired on an archive set this would put a line on every dataset that has
    a record at all, which is how a note stops being read.
    """
    ordered = sorted(keys)
    return ValidationWarning(
        code="declared_sample_fuses_runs",
        message=(
            f"{sample_id}: the record set fuses {len(ordered)} runs the filenames would have kept "
            f"apart ({', '.join(ordered[:6])}{', ...' if len(ordered) > 6 else ''}). Nothing in those "
            f"names says they are one library, so this grouping is declared rather than observed — "
            f"they compile into one {sample_id} matrix because the record set says so, where the same "
            f"files with no record set would compile into {len(ordered)}. If that is not what was "
            f"meant, the line to fix is the `parent` on one of those runs."
        ),
        subject=BlockerSubject(kind="dataset", ref=f"experiment.samples.{sample_id}"),
    )


def _join_by_filename(files: Sequence[FileIdentity]) -> list[_Sample]:
    """No record: the run grouping IS the sample identity.

    This is the path for every dataset that never went near an archive, which is most of them. It is
    exactly what the pipeline already did — filenames group, bytes assign.
    """
    grouped: dict[str, list[str]] = {}
    for f in files:
        grouped.setdefault(run_key(f.basename), []).append(f.sha256)
    return [
        _Sample(sample_id=sid, accession=None, file_shas=sorted(grouped[sid]))
        for sid in sorted(grouped)
    ]


def _join_blocker(unclaimed: list[str], records: ArchiveRecordSet) -> Blocker:
    """Refuse the half-join, and say which of the two halves is actually missing.

    A set with no ``io_version`` was transcribed before seqforge read submitted files at all, so its
    second half is a question we did not ask rather than an answer the archive gave. Told the old way
    — "they match none of the original filenames the record declares" — that reads as the archive's
    fault and sends a reader to inspect a download that is very likely fine, when one command against
    the cache is the fix. The distinction is the whole reason the stamp is on the set (ADR-0033):
    *most* deposits publish no originals, so an empty list can never be the signal by itself.

    A set a **human wrote** takes neither of those branches, and that is not a nicety. It carries no
    writer stamp — nothing stamped it — and no accession, so the stale-cache branch would fire on
    every one of them and send its reader to re-fetch from an archive that was never involved. Same
    refusal, same code: what changes is that the gap is in a file they can open, and the remedy names
    the two ways to close it.
    """
    declared = sorted({r.accession for r in records.at("run")})
    user_written = records.declared_by_hand
    # A hand-written set's `query` is not an accession — it defaults to the file's own stem — so it
    # is introduced as the name of a file rather than dropped where a reader expects one to be typed.
    named = f"the record set {records.query}" if user_written else records.query
    # The check-the-files remedy, which is the tail of both archive branches: even a re-fetch can only
    # get you back to this question, so it is stated once rather than diverging between the two.
    on_disk = (
        "the files are not from this accession, or they were renamed after download: check the "
        "accession, or re-fetch them with a tool that keeps the run accession in the filename "
        "(`fasterq-dump --split-files` names them <RUN>_1.fastq.gz). To compile with no record at "
        "all, omit the accession — the filenames then group the samples, and that is not an error."
    )
    if user_written:
        why = "none of them by any run id or filename it lists: "
        remedy = (
            "This record set was written by hand, so what does not account for these files is the "
            "file you wrote, not an archive: add each one to the `filenames` of the run it belongs "
            "to, or re-draft with `seqforge records new <dir>` — which lists every file in the "
            "directory, one sample per run — and edit that. Dropping the record set entirely is also "
            "legal: the filenames group on their own, and a dataset with no record set is not an "
            "error."
        )
    elif records.io_version is None:
        why = (
            "none of them by run accession, and this record set carries no writer stamp — it was "
            "cached before seqforge transcribed the submitter's own filenames, so the other half of "
            "the join is missing from the cache rather than from the archive: "
        )
        remedy = (
            f"Re-fetch the records first — `seqforge io records {records.query}` rewrites the set "
            f"with the names the submitter uploaded under, which is what places a file whose "
            f"accession was renamed away. If it still refuses on the fresh set, then either "
            f"{on_disk}"
        )
    else:
        why = "none of them by run accession or by the original filenames the record declares: "
        remedy = f"Either {on_disk}"
    return Blocker(
        id="blk-record-join-incomplete",
        code=BlockerCode.RECORD_JOIN_INCOMPLETE,
        message=(
            f"{named} declares {len(declared)} run(s) ({', '.join(declared[:6])}"
            f"{', ...' if len(declared) > 6 else ''}), and {len(unclaimed)} file(s) on disk match "
            f"{why}"
            f"{', '.join(sorted(unclaimed)[:6])}{', ...' if len(unclaimed) > 6 else ''}. Refusing to "
            f"half-join: the files it cannot place would silently get no sample, and a manifest that "
            f"places some files and is quiet about others reads as one about all."
        ),
        remedy=remedy,
        subject=BlockerSubject(kind="dataset", ref="experiment.samples"),
        evidence=sorted(unclaimed),
    )


def _archive_project(records: ArchiveRecordSet | None) -> ArchiveProject | None:
    """The archive project's declared, structured facts. The abstract is deliberately not among them."""
    if records is None:
        return None
    projects = records.at("project")
    if not projects:
        return None
    p = projects[0]
    accession = p.accession if _looks_like_accession(p.accession) else None
    return ArchiveProject(
        accession=accession,
        title=p.text("study_title"),
        center=_meta(p, "center_name"),
        data_type=_meta(p, "data_type"),
        released=_meta(p, "submission_date"),
    )


def _meta(record: ArchiveRecord, name: str) -> str | None:
    for attr in record.attributes:
        if attr.name == name:
            return attr.value
    return None


def _organism(records: ArchiveRecordSet | None) -> EvidencedTaxid | None:
    """The taxid every sample record agrees on. Disagreement yields ``None`` rather than a majority.

    A dataset whose samples are two organisms is a real thing (a xenograft, a co-culture), and it is
    not something this function may flatten. ``None`` sends the caller to ask.
    """
    if records is None:
        return None
    seen: dict[str, list[str]] = {}
    for sample in records.at("sample"):
        taxid = _meta(sample, "taxonomy_id")
        if taxid:
            seen.setdefault(taxid, []).append(sample.accession)
    if len(seen) != 1:
        return None
    taxid, evidence = next(iter(seen.items()))
    if not taxid.isdigit() or int(taxid) <= 0:
        return None
    return EvidencedTaxid(
        value=int(taxid), basis="asserted", evidence=sorted(evidence), confidence=None, rung=0
    )


def _looks_like_accession(value: str) -> bool:
    import re

    return bool(re.match(r"^([SED]R[RXPS]\d+|GS[EM]\d+|PRJ[A-Z]{2}\d+|SAM[NED][A-Z]?\d+)$", value))


__all__ = [
    "DocumentSubject",
    "resolve_metadata",
]
