"""``ArchiveRecord`` — what a public archive *declares* about a dataset, before anyone interprets it.

A record is not a truth and not a manifest. It is a transcript: this is what the submitter typed and
the archive stored, split into the two halves that need different machinery.

- ``attributes`` is the **structured** half (``strain = CQ758``). Code parses it. No model is
  involved, and none is needed: it is already a key and a value.
- ``free_text`` is the **prose** half (``"Rep3 daf2 reads"``, a study abstract, a protocol
  paragraph). Code cannot parse it; that is job (a), and it is what harvest is for.

**Every record is optional, and that is a requirement rather than an accident.** seqforge compiles
FASTQ that arrives with an accession, FASTQ that arrives with a README, and FASTQ that arrives with
nothing. There is no archive for a freshly-sequenced plate on a lab filesystem, so no code path may
assume one exists, and "no record" must produce a quieter manifest rather than a refusal. What a
record adds when it *is* there is per-sample subject identity — the thing a dataset-level document
can never supply.

**The hierarchy is the archive's, and the join is ours.** ``parent`` points one level up
(run -> experiment -> sample -> project) and is copied out of the record, never inferred: the archive
already knows which run came from which sample, and re-deriving that from filenames would be a guess
where a fact was available. What code still has to decide is the last hop — record run to *file on
disk* — because the archive does not know what you downloaded or what you named it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The four levels of the archive's own hierarchy. ``project`` is the study; ``sample`` is the
#: biological material; ``experiment`` is the library prep; ``run`` is one sequencing run == the
#: files. Other archives use other words for the same four things.
RecordLevel = Literal["project", "sample", "experiment", "run"]

#: The ``source`` of a set a **human wrote** about their own pre-deposit data, rather than one
#: transcribed from an archive. It lives beside the field it is a value of, because a string literal
#: repeated across modules is a chance to typo a decision — and this one decides which dialect the
#: loader enforces, whether fusing runs is remarkable, and where a refusal sends its reader
#: (ADR-0034).
#:
#: **Read it through** :attr:`ArchiveRecordSet.declared_by_hand`, which is the question those
#: consumers are actually asking. What is left for the constant is the loader: it dispatches on the
#: raw ``source`` of a mapping that is not yet a model, and it writes the value back out.
USER_SOURCE = "user"


class FreeText(BaseModel):
    """One piece of prose from a record, and what the archive called it.

    ``label`` is the archive's own field name (``sample_alias``, ``design_description``), kept so a
    quote can be traced to the field it came out of rather than to an anonymous blob of text.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    text: str


class RecordAttribute(BaseModel):
    """One structured key/value a record declares.

    ``harmonized`` records whether ``name`` is one of NCBI's 960 curated attribute names or the
    submitter's own invention. Both are kept: an unharmonized attribute is a real thing the submitter
    said, and dropping it would lose information, while promoting it into the controlled key space
    would be a guess. Only a harmonized attribute may become a manifest sample fact.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    value: str
    harmonized: bool = False
    #: The submitter's raw tag, when it differs from ``name``. Provenance for the harmonization.
    raw_name: str | None = None


class SubmittedFile(BaseModel):
    """One file the submitter uploaded, as the archive declares it (ADR-0033).

    The unit is the file, not the name: an archive states the name, a hash, a size and a location on
    one element, and they only mean anything together — a name with no hash and no location is what
    we used to keep, and a hash with no location names bytes nobody can reach.

    Archive-neutral on purpose. SRA spells this ``<SRAFile supertype="Original">`` with a
    ``semantic_name``, a ``cluster`` and an ``<Alternatives access_type=...>`` child; ENA spells the
    same four facts ``submitted_ftp``/``submitted_md5``/``submitted_bytes``; an in-house deposit has
    no ``supertype`` at all. Modelling one archive's element would be that XML wearing a model's
    clothes, and it could not accept the other two.
    """

    model_config = ConfigDict(frozen=True)

    filename: str
    #: The **provider's** md5 over the bytes at ``uri`` — an address, adopted via
    #: ``content_key_from_md5`` if those bytes are ever fetched. It is never computed here and never
    #: compared against a file on disk: doing that means reading a FASTQ end to end, which the read
    #: budget forbids — and #37 already removed one whole-file hash for that reason.
    md5: str | None = None
    #: What the archive says the upload weighed. It *checks* a join the filename already made and
    #: never makes one, because matching on a size is a coincidence over a fact the archive supplied.
    size_bytes: int | None = None
    #: Where the submitter's own copy — never normalized, never regenerated — can be fetched. Carried
    #: and printed; fetching it needs credentials and an egress budget, which is its own decision.
    uri: str | None = None


class ArchiveRecord(BaseModel):
    """One level of one archive record, as fetched. A transcript, not an interpretation."""

    model_config = ConfigDict(frozen=True)

    level: RecordLevel
    #: The record's id in whatever namespace produced it. Not typed as ``Accession``: this is the
    #: archive's word for the record, and seqforge is not only ever handed NCBI accessions.
    accession: str
    #: The record one level up, by its own id. Copied from the record; never inferred.
    parent: str | None = None
    attributes: list[RecordAttribute] = Field(default_factory=list)
    free_text: list[FreeText] = Field(default_factory=list)
    #: The files the archive says this record's data was submitted as. Their names are the only thing
    #: that can join a record to a file whose name no longer contains the accession.
    submitted_files: list[SubmittedFile] = Field(default_factory=list)

    @property
    def filenames(self) -> list[str]:
        """The submitted names, derived. Stored nowhere, so it cannot disagree with the files."""
        return [f.filename for f in self.submitted_files]

    def attribute(self, name: str) -> str | None:
        """The value of a harmonized attribute, or ``None``. Never raises — absence is normal."""
        for attr in self.attributes:
            if attr.name == name and attr.harmonized:
                return attr.value
        return None

    def text(self, label: str) -> str | None:
        for ft in self.free_text:
            if ft.label == label:
                return ft.text
        return None


class ArchiveRecordSet(BaseModel):
    """Every record fetched for one query, across all four levels.

    Content-addressed and cached under the workspace: a record is a fact about the archive at a
    moment, so re-fetching it should be a choice rather than a side effect of re-running.
    """

    model_config = ConfigDict(frozen=True)

    #: Which archive, and how. e.g. ``ncbi-sra+biosample``.
    source: str
    #: What was asked for. The accession a human typed.
    query: str
    records: list[ArchiveRecord] = Field(default_factory=list)
    #: The ``IO_VERSION`` of the transcriber that wrote this set, stamped by
    #: :func:`~seqforge.io.archive.fetch_records`. **Absent means the set predates submitted files**,
    #: which a reader must not confuse with a deposit that publishes none — most publish none, so
    #: empty is the normal case and could never be the signal by itself. Optional rather than
    #: defaulted here because ``models`` may not import ``io``: the writer stamps it, and a set that
    #: came off disk keeps whatever stamp it was written with.
    io_version: str | None = None

    @property
    def declared_by_hand(self) -> bool:
        """Did a human write this set about their own data, rather than a transcriber fetch it?

        The question three consumers actually ask, named once here rather than re-derived from
        ``source`` at each of them. Every one of the three is a *decision* — which dialect the loader
        enforces, whether fusing runs is worth a note or is the archive's ordinary shape, and whether
        a refusal's remedy should send its reader to an archive — so a string comparison spelled out
        three times is three places for one decision to drift, and nothing would go red when it did.

        It reads ``source`` and does not replace it: ``source`` records *which* archive a set came
        from, of which "a human" is one value, and the loader still has to write that value.
        """
        return self.source == USER_SOURCE

    def at(self, level: RecordLevel) -> list[ArchiveRecord]:
        return [r for r in self.records if r.level == level]

    def by_accession(self, accession: str) -> ArchiveRecord | None:
        for r in self.records:
            if r.accession == accession:
                return r
        return None

    def ancestor(self, record: ArchiveRecord, level: RecordLevel) -> ArchiveRecord | None:
        """Walk ``parent`` up to ``level``. The join, and it is pure record-following.

        Bounded by the number of records so a record set with a parent cycle (an archive bug, or a
        hand-written one) terminates rather than hanging.
        """
        seen: set[str] = set()
        current: ArchiveRecord | None = record
        while current is not None and current.accession not in seen:
            if current.level == level:
                return current
            seen.add(current.accession)
            current = self.by_accession(current.parent) if current.parent else None
        return None


__all__ = [
    "RecordLevel",
    "USER_SOURCE",
    "FreeText",
    "RecordAttribute",
    "SubmittedFile",
    "ArchiveRecord",
    "ArchiveRecordSet",
]
