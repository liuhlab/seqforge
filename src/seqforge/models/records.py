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
    #: Filenames the archive says this record's data was submitted as. The only thing that can join a
    #: record to a file whose name no longer contains the accession.
    filenames: list[str] = Field(default_factory=list)

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

    Content-addressed and cached under the workspace (R7): a record is a fact about the archive at a
    moment, so re-fetching it should be a choice rather than a side effect of re-running.
    """

    model_config = ConfigDict(frozen=True)

    #: Which archive, and how. e.g. ``ncbi-sra+biosample``.
    source: str
    #: What was asked for. The accession a human typed.
    query: str
    records: list[ArchiveRecord] = Field(default_factory=list)

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
    "FreeText",
    "RecordAttribute",
    "ArchiveRecord",
    "ArchiveRecordSet",
]
