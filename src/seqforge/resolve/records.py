"""The metadata resolver: archive records + prose -> per-sample facts. A sibling of ``score``.

``score`` answers "what is this library?" from bytes. This answers "which sample is each file, and
what is that sample?" from records and prose. They are siblings rather than one stage because they
have the same ways of being wrong and therefore need the same discipline: both emit evidenced values,
and both can refuse outright. They differ in one place — the byte resolver surfaces an observed-vs-
asserted disagreement it will not arbitrate, while the metadata resolver decides a sample-attribute
disagreement (by precedence, or null) and only *notes* it — for the reason in "Where basis comes
from", below. What they must never do is talk to each other — see "the line", below.

**The join is code's, at every level.** run -> experiment -> sample -> project comes out of the
record, by accession; record-run -> file-on-disk comes out of the run accession in the filename or
the original filenames the record declares. A language model is never asked which sample a file is,
and never could be: it is not shown the files. The archive also publishes a *size* beside each of
those names, and it checks a join without ever making one (ADR-0033): a name is a fact the submitter
typed and a size is two numbers agreeing, so joining on the second would lay a guess over the first.
Where the name made the join, though, the file on disk is claiming to BE that submitted file, and a
size that disagrees is worth saying out loud — as a warning, because this stage decides.

**The subject is the document.** A claim cannot name a sample — ``AssertionDraft`` has ``field``,
``value``, ``span``, and nothing else, and it stays that way. Instead each record level is rendered
as *its own document*, so the sample-level document contains that sample's fields and nothing else,
and "which sample" is answered by which file we handed the model. This is the trick ``instruct.py``
already ships for document role: code knows it because code chose it. The alternative — a ``subject``
field on the draft — would hand the model a new authority, and the two-jobs sentence would need
rewriting.

**Where basis comes from, and why it is not a vote.**

===========================================  ===============  ================================
source                                       basis            because
===========================================  ===============  ================================
a record's structured field (strain=CQ758)   ``asserted``     the submitter declared it, of
                                                              this sample, in a typed slot
a model reading THIS sample's own prose      ``asserted``     the document is about this
                                                              sample and nothing else
a model reading a DATASET-level document     ``inferred``     the paper says it of the study;
(a paper, a README)                                           that it holds of *this* sample
                                                              is our inference, not its claim
===========================================  ===============  ================================

A disagreement across bases keeps the stronger basis's value (``asserted`` over ``inferred``); a
disagreement that survives every level of precedence stores **no value at all**, because two equal
authorities contradicting each other is not something code may break. Null is not the *safe* answer
there — ``experiment`` is inside ``dataset_hash`` and the manifest is never rewritten, so a null is
exactly as permanent as a wrong value, and the harness grades it ``false_accept``. It is the honest
one, and only where nothing outranks anything. Either way the resolver has **decided** — so the
disagreement is a non-blocking ``warning``, not a refusal, and a single sample annotation is no reason
to stop a whole dataset compiling. Only the byte resolver's ``observed`` vs ``asserted`` conflict
blocks: that one decides what the data *is*, and code may not auto-pick it.

**Two equal authorities, though, must actually be two sources.** One archive deposit read at two of
its levels is one: the sample's typed slot and the experiment title were written by the same hand, in
the same submission, so a model's reading of that submission's prose is not a second authority
contradicting the first — it is the same submitter, read back. So inside one source the typed slot
wins and the reading is noted, whether it paraphrases the declaration or files another attribute's
value under it (:func:`_outranking`; ADR-0021).

That asymmetry still catches the error span verification provably cannot. "We dissected neurons and body wall muscle"
entails ``tissue=neurons`` *and* ``tissue=muscle`` — both quotes are real, both pass span verification
and entailment. What separates them is that the record says ``Neurons``: it is a declaration about this
sample (``asserted``) and the paper's reading is our inference (``inferred``), so the record's value
stands and the paper's is surfaced as a warning a reader can see — never baked in as a fact a corpus
inherits, and never a refusal that stops the compile.

**What the record said that has no key is noted, not swallowed.** The key space is NCBI's 960
harmonized names and it stays closed — a key we coined would accept whatever an extraction wanted to
put in it. But a submitter may type a load-bearing fact into a structured characteristic under a name
nobody curates (``bd rhapsody_capture_bead_version: enhanced beads``), and the same sentence in a
free-text protocol field would have reached the prose path. Silently skipping the structured one made
it *less* legible than the paragraph, so each such attribute leaves a non-blocking note naming the
tag, the value and the sample. Nothing new reaches the manifest; the difference is that the gap is
visible.

**No archive is the normal case, not the degraded one.** Most sequencing data has never had an
accession and never will: a freshly sequenced plate on a lab filesystem has no BioProject, no
BioSample, and no submitter alias. With no record, sample identity falls back to the run grouping
(filenames group; they always did) and sample facts come from whatever prose there is, or from
nothing. Nothing here refuses for lack of a record. The refusal is narrower and it is real: a record
that *exists* and does not account for the files on disk is a broken join, and half-joining would
leave some files with no sample while the manifest still read as though it described them all.

**And a record is not always an archive's.** ``source`` says who declared the set, and a ``user`` one
is a human describing their own pre-deposit data: structure only — levels, ids, parents, filenames —
and never an attribute, because ``asserted`` above means "a submitter typed this into a slot for this
sample" and a line of hand-written YAML has no document to grep back into (`docs/adr/0034`). None of
the precedence machinery below moves; two messages do. Declaring one sample over runs the filenames
would have kept apart is the *reason* such a set gets written, so it is a note rather than a refusal —
and a note only here, since firing it on a deposit would report every ordinary run-to-BioSample
fusion. A set that leaves a file unplaced still refuses, with a remedy naming the file the human
wrote rather than an archive they were never near.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..io.attributes import is_attribute
from ..models.assertion import Assertion
from ..models.base import Basis
from ..models.blocker import Blocker, BlockerCode, BlockerSubject, ValidationWarning
from ..models.evidenced import EvidencedStr, EvidencedTaxid
from ..models.observation import FileIdentity
from ..models.records import ArchiveRecord, ArchiveRecordSet, RecordAttribute, SubmittedFile
from ..models.resolve import MetadataResolution, ProjectFacts, ResolvedSample
from .group import run_key

#: Which authority wins when two sources disagree about one sample attribute. Never a vote and never
#: a confidence comparison: an LLM's self-reported confidence is advisory and would happily
#: outrank a database.
_BASIS_RANK: dict[Basis, int] = {
    "observed": 3,
    "user_confirmed": 2,
    "asserted": 1,
    "inferred": 0,
}

#: A record attribute that is real, useful, and NOT one of NCBI's 960 sample attributes. These are
#: facts about the record rather than the biology — the submitting centre, the BioSample package, the
#: taxid that becomes ``experiment.organism`` — so they are read by name here and never offered as
#: sample fields. Naming them is also what keeps :func:`_unharmonized_note` worth reading: every
#: archive stamps these on every sample, so noting them would be several lines per sample on every
#: dataset that has a record at all, which is how a note stops being read.
_RECORD_META = frozenset(
    {"center_name", "biosample_package", "data_type", "submission_date", "taxonomy_id"}
)

#: The prefix an assertion uses to name a sample attribute: ``experiment.samples.tissue``.
SAMPLE_FIELD_PREFIX = "experiment.samples."

#: One archive deposit, as a SOURCE. Every level of it — the sample's typed slots, the experiment
#: title, the run alias — is one submitter filling one submission, so two answers drawn from it are
#: two readings of one thing rather than two opinions. A document a human handed us is a different
#: author entirely and is identified by its own ``doc_sha256``, which can never equal this (a sha256
#: is 64 hex characters).
_ARCHIVE_SOURCE = "archive"

#: What a document is ABOUT — set by code from which record produced it, never by the model and never
#: from the filename. ``dataset`` is a document handed to us for the whole pile of files (a paper, a
#: README); the others name one record.
DocScope = str


@dataclass(frozen=True)
class DocumentSubject:
    """Which record a document was rendered from. Code's answer to "which sample is this about?".

    Mirrors ``instruct.py``'s ``instruction_docs``: a set of ``doc_sha256`` that code assembled
    because code chose the documents. Nothing here is derivable from the document's contents, and
    that is the point — a spoofable subject would be worse than no subject.
    """

    doc_sha256: str
    scope: DocScope
    #: The record's accession, when the scope names one. ``None`` for a dataset-level document.
    subject: str | None = None


@dataclass(frozen=True)
class _Position:
    """One source's answer for one (sample, attribute), before anything is decided."""

    value: str
    basis: Basis
    evidence: list[str]
    confidence: float | None
    rung: int
    #: WHICH source produced this answer. Basis says how strongly a claim is held and cannot say
    #: whether two claims came from the same place, so a record's own field and a model's reading of
    #: that record's prose used to be indistinguishable from a BioSample contradicting a paper.
    source: str
    #: True when the source TYPED this value into a slot for this attribute, rather than a model
    #: having read it out of that source's prose. It is not a fifth ``Basis`` — it separates two
    #: positions that share one — and inside one source it decides between them, because only the
    #: typed one is a string the submitter actually wrote for this attribute (:func:`_outranking`).
    declared: bool


def resolve_metadata(
    *,
    files: Sequence[FileIdentity],
    records: ArchiveRecordSet | None = None,
    assertions: Sequence[Assertion] = (),
    subjects: Sequence[DocumentSubject] = (),
) -> MetadataResolution:
    """Resolve the files into samples, and the samples into facts.

    Takes ``FileIdentity`` rather than ``Observation`` on purpose. This stage needs a basename and a
    sha256 and nothing else, and being handed the probe's output would mean *promising* not to read
    the signals in it. A signature that cannot see them keeps the promise structurally — see
    :func:`_the_line` for why it is worth keeping.
    """
    by_doc = {d.doc_sha256: d for d in subjects}
    samples, join_notes, blockers = _join(files, records)
    subject_to_sample = _subject_to_sample(records)

    verified = [a for a in assertions if a.span_verified and a.entailment_ok]
    per_sample: dict[str, dict[str, list[_Position]]] = {}
    unkeyed: dict[str, list[ValidationWarning]] = {}
    for s in samples:
        per_sample[s.sample_id], unkeyed[s.sample_id] = _positions_for(
            s, verified, by_doc, subject_to_sample
        )
    # Which attributes the archive/prose declares PER SAMPLE for anyone — proof the attribute varies by
    # sample. A dataset-level (paper) claim may only fill an attribute nobody declares per-sample; the
    # moment one sample owns a value for it, a blanket study-wide value is an unsafe guess for the
    # samples left blank (#10). "sample-scoped" == any basis stronger than dataset-level `inferred`.
    sample_scoped_attrs = frozenset(
        name
        for positions in per_sample.values()
        for name, found in positions.items()
        if any(p.basis != "inferred" for p in found)
    )
    resolved: list[ResolvedSample] = []
    # The join's own notes lead, because they are about which file is which and everything below is
    # about what a sample was — a reader who sees "this file does not weigh what the record says"
    # wants it before the attribute it eventually flowed into, not after.
    warnings: list[ValidationWarning] = list(join_notes)
    for sample in samples:
        positions = per_sample[sample.sample_id]
        attrs, sample_warnings = _decide(sample.sample_id, positions, sample_scoped_attrs)
        resolved.append(
            ResolvedSample(
                sample_id=sample.sample_id,
                accession=sample.accession,
                attributes=attrs,
                file_shas=sample.file_shas,
            )
        )
        # what the record said and this stage could not key, then what it decided under disagreement
        warnings.extend(unkeyed[sample.sample_id])
        warnings.extend(sample_warnings)

    return MetadataResolution(
        samples=resolved,
        project=_project_facts(records),
        organism=_organism(records),
        warnings=warnings,
        blockers=blockers,
    )


@dataclass(frozen=True)
class _Sample:
    """A joined sample: who it is, which files carry it, and the record behind it (if any)."""

    sample_id: str
    accession: str | None
    file_shas: list[str]
    record: ArchiveRecord | None


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
    record_of: dict[str, ArchiveRecord | None] = {}
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
        # A hand-written id is a GROUPING KEY and not a specimen the archive named (`CONTEXT.md`,
        # **Sample**), so it is not an accession and must not be stored as one — `plate7` does not
        # match the accession pattern and reached this stage as an uncaught validation error rather
        # than as anything a caller could act on. Carrying no record with it is the same rule from
        # the other side: a structure-only set has nothing for `_positions_for` to read, and a loader
        # that let an attribute through would otherwise have it graded `asserted` — the standing
        # reserved for a slot a submitter typed (`docs/adr/0034`).
        declarer = None if declared_by_user else sample
        accession_of[sample_id] = declarer.accession if declarer is not None else None
        record_of[sample_id] = declarer
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
                record=record_of[sid],
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
    one that would silently attach the wrong sample's facts. Nothing here can tell them apart, so the
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
        subject=BlockerSubject(kind="dataset", ref=f"{SAMPLE_FIELD_PREFIX}{sample_id}"),
    )


def _join_by_filename(files: Sequence[FileIdentity]) -> list[_Sample]:
    """No record: the run grouping IS the sample identity.

    This is the path for every dataset that never went near an archive, which is most of them. It is
    exactly what the pipeline already did — filenames group, bytes assign — and it produces samples
    with no facts, because there is nothing declaring any.
    """
    grouped: dict[str, list[str]] = {}
    for f in files:
        grouped.setdefault(run_key(f.basename), []).append(f.sha256)
    return [
        _Sample(sample_id=sid, accession=None, file_shas=sorted(grouped[sid]), record=None)
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
        "(`fasterq-dump --split-files` names them <RUN>_1.fastq.gz). To compile with no sample facts "
        "at all, omit the accession — a dataset with no record is not an error."
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
            f"half-join: the files it cannot place would silently get no sample facts, and a manifest "
            f"that is confident about some samples and quiet about others reads as one about all."
        ),
        remedy=remedy,
        subject=BlockerSubject(kind="dataset", ref="experiment.samples"),
        evidence=sorted(unclaimed),
    )


def _subject_to_sample(records: ArchiveRecordSet | None) -> dict[str, str]:
    """Map any record accession (run, experiment, or sample) to its sample's accession.

    A run or an experiment belongs to exactly one sample, so a claim from *its* document is a
    declaration about *that* sample — the same standing a sample's own document has. This is the join
    that lets ``_basis_for`` treat a run alias ("N2_wild_type", "daf-2 R3") as ``asserted`` of its
    sample: the run names the sample by belonging to it, and code did the join, so no model was asked
    "which sample". Without it a run document's claim maps to no sample and is silently discarded —
    which is why the pilot's clearest genotype signal never reached the manifest.
    """
    if records is None:
        return {}
    out: dict[str, str] = {}
    for level in ("sample", "experiment", "run"):
        for rec in records.at(level):
            sample = rec if level == "sample" else records.ancestor(rec, "sample")
            if sample is not None:
                out[rec.accession] = sample.accession
    return out


def _positions_for(
    sample: _Sample,
    assertions: Sequence[Assertion],
    by_doc: dict[str, DocumentSubject],
    subject_to_sample: dict[str, str],
) -> tuple[dict[str, list[_Position]], list[ValidationWarning]]:
    """Every source's answer for every attribute of one sample, plus what the record said that has no
    key to say it under. Decides nothing: the notes report an exclusion the key space already made.
    """
    out: dict[str, list[_Position]] = {}
    notes: list[ValidationWarning] = []

    if sample.record is not None:
        for attr in sample.record.attributes:
            if attr.name in _RECORD_META:
                continue
            if not attr.harmonized or not is_attribute(attr.name):
                notes.append(_unharmonized_note(sample.sample_id, attr))
                continue
            out.setdefault(attr.name, []).append(
                _Position(
                    value=attr.value,
                    basis="asserted",
                    evidence=[sample.record.accession],
                    # A copy is not a judgement: no confidence, because none was formed. See
                    # `Evidenced.confidence`.
                    confidence=None,
                    rung=0,
                    source=_ARCHIVE_SOURCE,
                    declared=True,
                )
            )

    for a in assertions:
        if not a.field.startswith(SAMPLE_FIELD_PREFIX):
            continue
        name = a.field[len(SAMPLE_FIELD_PREFIX) :]
        if not is_attribute(name):
            continue  # `fields.py` already refused it; belt and braces
        doc = by_doc.get(a.span.doc_sha256)
        if doc is None:
            continue  # a document code did not place has no subject, so it may not name one
        basis = _basis_for(doc, sample, subject_to_sample)
        if basis is None:
            continue  # this document is about a different sample
        out.setdefault(name, []).append(
            _Position(
                value=a.value,
                basis=basis,
                evidence=[a.id],
                confidence=a.llm_confidence,
                rung=0,
                source=_source_of(doc),
                # A model read this out of prose; nobody typed it into a slot for this attribute.
                declared=False,
            )
        )
    return out, notes


def _unharmonized_note(sample_id: str, attr: RecordAttribute) -> ValidationWarning:
    """The submitter typed a fact into a structured slot under a name nobody controls.

    Keeping it out of ``experiment.samples`` is the decision, and it stands: a key we coined would
    accept whatever an extraction wanted to put in it, which is exactly how a field called
    "condition" swallowed routine worm husbandry. *Silence* was never part of that decision, though,
    and it produced an asymmetry worth naming — the same sentence in a free-text protocol field
    reaches the prose path and can become a span-verified claim, so a submitter who used the
    structured slot was LESS legible to the compiler than one who buried it in a paragraph. A GEO
    record declaring ``bd rhapsody_capture_bead_version: enhanced beads`` — the single fact that
    separates one BD Rhapsody bead from the other — vanished here without a word.

    So the drop is noted rather than closed: no key is invented, no refusal is weakened, nothing
    reaches the manifest that could not before, and the fact stops being invisible (#165). This
    stage decides and never blocks, so a note is the whole of what it may emit.
    """
    tag = attr.raw_name or attr.name
    return ValidationWarning(
        code="sample_attribute_unharmonized",
        message=(
            f"{sample_id}: the record declares {tag!r} = {attr.value!r}, which is not one of NCBI's "
            f"harmonized attribute names, so it stays on the record and becomes no sample fact — a "
            f"key nobody curates would accept whatever was put in it. The same words in a free-text "
            f"field would have reached the prose path instead."
        ),
        subject=BlockerSubject(kind="field", ref=f"{SAMPLE_FIELD_PREFIX}{attr.name}"),
    )


def _basis_for(
    doc: DocumentSubject, sample: _Sample, subject_to_sample: dict[str, str]
) -> Basis | None:
    """What a claim from this document is, *about this sample*. ``None`` = it is not about it at all.

    A document that names a level *belonging to* this sample — the sample itself, or one of its
    experiments or runs — is a declaration about that sample (``asserted``). ``subject_to_sample``
    holds that join, computed by code from the record hierarchy, so a run alias is asserted of its
    sample exactly as the sample's own alias is. A dataset-scoped document (a paper) makes a claim
    about the study; that it holds of any one of six samples is **our** inference (``inferred``). That
    distinction is what makes the precedence in :func:`_decide` principled rather than a tiebreak we
    invented.
    """
    if doc.scope == "dataset":
        return "inferred"
    if doc.subject is not None and subject_to_sample.get(doc.subject) == sample.accession:
        return "asserted"
    return None


def _source_of(doc: DocumentSubject) -> str:
    """WHICH source a document's claims come from — a different question from how strong they are.

    It branches where :func:`_basis_for` branches, and on the same fact, because it is the same
    distinction asked for a different purpose: a document code rendered from a record is the archive
    speaking (every level of one deposit is one submitter), and a document a human handed us is a
    separate author who happens to describe the same study. Basis alone cannot answer this — a record
    field and a model's reading of that record's own prose are both ``asserted``, which is exactly how
    one source arguing with itself became indistinguishable from a BioSample contradicting a paper.
    """
    return doc.doc_sha256 if doc.scope == "dataset" else _ARCHIVE_SOURCE


def _decide(
    sample_id: str,
    positions: dict[str, list[_Position]],
    sample_scoped_attrs: frozenset[str] = frozenset(),
) -> tuple[dict[str, EvidencedStr], list[ValidationWarning]]:
    """Turn each attribute's positions into at most one value, plus non-blocking notes. Never a vote.

    The resolver DECIDES here rather than defer, and either way it is resolved — so a disagreement is a
    ``warning``, not a blocking conflict:

    - a stronger authority wins (:func:`_outranking`): keep its value, note the source that disagreed.
      Two levels — ``asserted`` over ``inferred``, and then, inside ONE source, the slot the submitter
      typed over a model's reading of that source's prose;
    - authorities that still tie leave the attribute **null**, because code does not get to break a
      tie between equals. Null is a value here, not a question for a human;
    - a sample covered ONLY by a dataset-level ``inferred`` claim, for an attribute some *other* sample
      owns per-sample (``sample_scoped_attrs``), is also left **null**: the paper's blanket value
      varies by sample and is an unsafe guess for a sample the archive left blank (#10).

    A null-or-precedence sample attribute must not stop a dataset compiling: the strain already tells
    the pilot's two conditions apart, and most datasets have no such prose at all. Only the byte
    resolver's ``observed`` vs ``asserted`` disagreement blocks — that one decides what the data *is*.
    """
    attrs: dict[str, EvidencedStr] = {}
    warnings: list[ValidationWarning] = []

    for name, found in sorted(positions.items()):
        if name in sample_scoped_attrs and all(p.basis == "inferred" for p in found):
            # This sample has only a dataset-level (paper) claim for an attribute the archive declares
            # per-sample elsewhere — so the attribute is sample-specific and the study-wide value is a
            # guess here. Null beats a wrong value that a permanent, content-addressed manifest bakes
            # in. PRJNA1027859: the paper's blanket `daf-2` must not stamp the wild-type samples.
            seen = ", ".join(sorted(f"{p.value!r} ({p.basis})" for p in found))
            warnings.append(
                ValidationWarning(
                    code="sample_attribute_inferred_only",
                    message=(
                        f"{sample_id} {SAMPLE_FIELD_PREFIX}{name}: only a dataset-level inferred claim "
                        f"({seen}) covers this sample, but the attribute is declared per-sample "
                        f"elsewhere in the dataset; left null rather than stamp a study-wide value on "
                        f"one sample"
                    ),
                    subject=BlockerSubject(kind="field", ref=f"{SAMPLE_FIELD_PREFIX}{name}"),
                )
            )
            continue
        ranked = _outranking(found)
        distinct = {_norm_value(p.value) for p in found}
        if len(distinct) == 1:
            # Everyone agrees, so nothing is decided and nothing is noted — but the SPELLING still
            # has to be chosen, and it is chosen by the same precedence rather than by which position
            # happened to be built first. `Colon`/`colon` fold together and so do `wild-type`/`wild
            # type`, so what the submitter typed is what a write-once manifest carries.
            attrs[name] = _evidenced(ranked[0])
            continue

        winners = {_norm_value(p.value) for p in ranked}
        seen = ", ".join(sorted(f"{p.value!r} ({p.basis})" for p in found))
        if len(winners) == 1:
            # a stronger authority exists: keep its value; what it outranked is only a note
            attrs[name] = _evidenced(ranked[0])
            held = (
                "the value the record declares"
                if ranked[0].declared
                else f"the {ranked[0].basis} value"
            )
            resolution = f"kept {held} {ranked[0].value!r}"
        else:
            # Equal authorities disagree and code does not get to break a tie between equals. Null is
            # not the safe answer — the manifest is write-once and content-hashed (ADR-0004), so a
            # null is exactly as permanent as a wrong value, and the harness grades it `false_accept`.
            # It is the honest one: with nothing typed behind either reading there is no third fact
            # to prefer, which is what rungs 4-6 exist for.
            resolution = (
                "left null — equal-authority sources disagree, and code may not break the tie"
            )
        warnings.append(
            ValidationWarning(
                code="sample_attribute_ambiguous",
                message=f"{sample_id} {SAMPLE_FIELD_PREFIX}{name}: sources disagree ({seen}); {resolution}",
                subject=BlockerSubject(kind="field", ref=f"{SAMPLE_FIELD_PREFIX}{name}"),
            )
        )

    return attrs, warnings


def _outranking(found: list[_Position]) -> list[_Position]:
    """The positions nothing here outranks — one of them wins, and a tie between them is null.

    Two levels, applied in order (`docs/adr/0021-one-deposit-is-one-source-at-every-layer.md`):

    1. **Basis.** A declaration about this sample beats our inference from a paper.
    2. **Declared, within ONE source.** A value the submitter TYPED into a slot for this attribute
       beats a model's reading of that same source's prose — whatever the reading says.

    Level 2 is what makes level 1 safe to state at all. GSE282765's BioSample types
    ``treatment = "Citrobacter rodentium infection"`` and a model reading that same submission's
    experiment title asserts ``treatment = "Citrobacter rodentium"`` — correct, span-verified,
    entailed, and one word short. Both arrive ``asserted``, they compare unequal, and the
    equal-authority rule left the attribute null: **adding a true statement destroyed a fact the
    archive had already supplied** (#182). That rule was written for a BioSample disagreeing with a
    paper, and nothing could tell that apart from a record disagreeing with a reading of itself.

    **It is not a comparison of the two strings, and that is the whole of it.** The earlier repair
    absorbed a reading wholly *inside* the typed value; it closed the shape above and no other.
    GSE317744 types ``treatment = "MC38_3 weeks"`` and the model asserted ``treatment = "CCR9 KO"`` —
    the sample's genotype, filed under the wrong key (#189). The two strings share nothing, so
    containment, overlap, synonyms and punctuation folding all miss it, and
    :func:`~seqforge.harvest.verify.entails` concedes it cannot see a field-assignment error at all.
    Preferring the typed slot covers both, because it never asks what the reading says.

    **Safety is that nothing new reaches the manifest.** What gets stored is the submitter's own
    string, byte for byte — exactly what a run with no prose at all would store. The cost is the
    mirror image: a submitter who typed a placeholder now beats real prose, which is why the losing
    reading is always named in a ``sample_attribute_ambiguous`` warning rather than dropped silently.

    **Only within one source, and only against something actually typed.** ``declared`` is set only
    where a record carried the value in a slot of its own, so the level-2 filter is scoped by
    ``source``: a paper is a second author and still loses on basis, with the note that says so. Where
    nothing was typed at all — two prose readings, no submitter's string behind either — nothing is
    filtered and the tie stands, which is the arbitration verb's job and not code's.
    """
    rank = max(_BASIS_RANK[p.basis] for p in found)
    top = [p for p in found if _BASIS_RANK[p.basis] == rank]
    typed = {p.source for p in top if p.declared}
    kept = [p for p in top if p.declared or p.source not in typed]
    # Declared first, so the stored value is the submitter's own string rather than whichever
    # position happened to be built first.
    return sorted(kept, key=lambda p: not p.declared)


#: Joiners a submitter uses where another types a space. It fixes no failing case on its own —
#: ``MC38_3 weeks`` and ``MC38 tumor_3 weeks`` still differ, and the precedence above is what settles
#: that pair — so what it buys is narrower and worth stating: two positions spelled apart only by one
#: of these stop being a disagreement, which is a warning not raised and, where nothing is declared,
#: a value stored instead of a null.
_VALUE_JOINERS = str.maketrans("-_/", "   ")


def _norm_value(value: str) -> str:
    """Case-, whitespace- and joiner-folded key for testing whether two attribute VALUES agree.
    'Male' and 'male' are the same sex and ``wild-type`` is the same genotype as ``wild type``; a
    permanent, content-addressed manifest must not null or warn about an attribute over a
    capitalization or a hyphen — only a genuine disagreement should. Which SPELLING is then stored is
    precedence's answer, not this function's: it folds for the comparison only, and `_evidenced`
    keeps whichever position :func:`_outranking` ranked first exactly as its source wrote it.
    """
    return " ".join(value.translate(_VALUE_JOINERS).split()).casefold()


def _evidenced(p: _Position) -> EvidencedStr:
    return EvidencedStr(
        value=p.value,
        basis=p.basis,
        evidence=list(p.evidence),
        confidence=p.confidence,
        rung=p.rung,
    )


def _project_facts(records: ArchiveRecordSet | None) -> ProjectFacts | None:
    """The study's declared, structured facts. The abstract is deliberately not among them."""
    if records is None:
        return None
    projects = records.at("project")
    if not projects:
        return None
    p = projects[0]
    accession = p.accession if _looks_like_accession(p.accession) else None
    return ProjectFacts(
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

    from ..models.base import Accession  # noqa: F401  (documents where the pattern is owned)

    return bool(re.match(r"^([SED]R[RXPS]\d+|GS[EM]\d+|PRJ[A-Z]{2}\d+|SAM[NED][A-Z]?\d+)$", value))


def _the_line() -> str:
    """Why this resolver is not shown the probe. Kept as prose because it is a design commitment.

    The tempting version of this module reads the probe's output too — "the reads are 28+94, so the
    protocol paragraph saying 28+94 is corroborated". Two reasons not to, and the second is the cheap
    one:

    1. A probe-sighted reader would settle ties the probe itself created, and log the wrong reason.
       Nothing records corroboration, so the manifest would say "asserted" for a fact that a byte
       actually decided, and the rung provenance would be a lie.
    2. Read lengths say nothing about neurons. There is no byte in a FASTQ that bears on ``tissue``,
       ``strain``, ``sex`` or ``dev_stage``. The probe has zero bits to contribute to every field this
       module resolves, so the whole question is moot for the fields it actually decides.

    The chemistry hypothesis is the *legitimate* half of the same idea, and it goes the other way:
    prose steers which whitelist ``score`` checks first, and never enters the evidence matrix.
    """
    return __doc__ or ""


__all__ = [
    "DocScope",
    "DocumentSubject",
    "SAMPLE_FIELD_PREFIX",
    "resolve_metadata",
]
