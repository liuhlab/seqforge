"""Which samples the **live** knowledge base admits into a pipeline, and what it keeps out.

A chemistry may declare a read floor — ``Spec.min_input_reads`` — and a sample below it is not
analysed in any form. That is a plate's normal state rather than an exception: failed and empty wells
are designed into the format, so refusing on one is refusing on every real plate, and the answer is to
drop the well and say so.

**The split this module exists to hold.** The manifest keeps every sample. It is what the data IS, and
you were handed them all; dropping there would make the dataset's identity a function of a knowledge-
base number, so raising the floor would give the same bytes a different name. So the measurement lives
in the manifest (per file, in ``provenance``) and the *verdict* lives here, recomputed at every
compile against whatever knowledge base is loaded. Freeze the verdict into a write-once artifact and
the next compile silently re-reads the first knowledge base's opinion of a threshold that has since
moved.

Nothing here writes, refuses, or renders a path. :func:`admit` is a pure split of the manifest's
samples into three buckets and :func:`render_record` turns one into prose; the composer owns the
refusals, because a compile with nothing left to produce is a fact about the compile.

**Inert by default.** ``min_input_reads`` is ``None`` on all sixteen shipped specs, so :func:`admit`
answers ``None`` and the composer takes the path it took before this module existed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..kb.schema import Spec
from ..models.dataset import DatasetManifest, SampleGroup
from ..resolve.group import run_key


@dataclass(frozen=True)
class Admission:
    """One dataset's samples, split by the read floor the loaded spec declares.

    The three buckets are disjoint and together are every sample the manifest carries, which is what
    lets the totals line be arithmetic rather than a second count.
    """

    #: The floor, in reads, as the spec loaded at compile time declares it.
    threshold: int
    #: Whether that spec says one ``Sample`` of it IS one cell. It decides one word — the noun the
    #: record and the CLI line use — and nothing else. A floor is a general admission threshold any
    #: chemistry may declare; only the plate chemistries make the thing being dropped a *cell*.
    sample_is_cell: bool
    #: The samples that cleared the floor, in manifest order. This becomes what the pipeline is
    #: contracted to produce — a dropped sample was never contracted, so it is not "missing" from it.
    admitted: tuple[SampleGroup, ...]
    #: sample id -> its **exact** read count, for every sample below the floor. Exact rather than
    #: extrapolated because any file shallow enough to fail a floor of this size was read to EOF
    #: inside the probe's budget, at no extra bytes.
    excluded: dict[str, int]
    #: Sample ids whose depth the manifest never measured — not the same claim as zero reads, and
    #: the composer refuses rather than reading one as the other.
    unmeasured: tuple[str, ...]

    @property
    def declared(self) -> int:
        """Every sample the manifest carries: the denominator of the totals line."""
        return len(self.admitted) + len(self.excluded) + len(self.unmeasured)

    @property
    def noun(self) -> str:
        """``cells`` or ``samples`` — what a reader of this deposit calls the thing that was dropped."""
        return "cells" if self.sample_is_cell else "samples"

    @property
    def summary(self) -> str:
        """The totals line, and the line that actually does the work.

        Nobody spots a split cell by reading 240 rows; everybody spots 768 samples on a 384-well
        plate. It is rendered once and read twice — the record's headline and the CLI's human stream —
        so the two can never come to disagree about how much was lost.
        """
        return f"{len(self.excluded)} of {self.declared} {self.noun} dropped"


def sample_reads(manifest: DatasetManifest, sample: SampleGroup) -> int | None:
    """``sample``'s depth: the MINIMUM within each of its runs, summed ACROSS them. ``None`` = unmeasured.

    The join is free here and is deliberately not frozen upstream: ``file_uris`` gives the sample's
    files, the inventory gives each one's checksum, and the filename gives the run they were
    sequenced in — the same ``run_key`` that grouped the dataset during resolution, so the two can
    never disagree about what a run is.

    **Minimum within, sum across**, and both halves matter. A run's mates are two views of one set of
    fragments, so summing them reports a 700-read library at 1400 and clears a floor it does not
    reach; healthy mates are equal by construction, which makes the minimum free rather than
    pessimistic. Two *runs*, on the other hand, are two genuine passes over the library, so a cell
    topped up in a second run must not be gated twice at half its depth.

    ``None`` propagates from :meth:`~seqforge.models.dataset.DatasetProvenance.reads_in_run`: a
    manifest written before per-file counts existed measured nothing, and a gate reading that as zero
    would drop every sample in it.
    """
    by_uri = {f.uri: f for f in manifest.library.files}
    runs: dict[str, list[str]] = {}
    for uri in sample.file_uris:
        item = by_uri.get(uri)
        if item is not None:
            runs.setdefault(run_key(uri), []).append(item.sha256)
    total = 0
    for shas in runs.values():
        depth = manifest.provenance.reads_in_run(shas)
        if depth is None:
            return None
        total += depth
    return total


def admit(manifest: DatasetManifest, spec: Spec) -> Admission | None:
    """Split ``manifest``'s samples on the read floor ``spec`` declares, or ``None`` when it declares none.

    ``None`` is the answer for every dataset the sixteen shipped entries describe, and it is what makes
    the whole path inert rather than merely cheap. A manifest with no samples at all also answers
    ``None``: the composer's implicit single-sample fallback has no id to exclude and no depth to
    attribute, so there is nothing here a floor could be about.
    """
    floor = spec.min_input_reads
    if floor is None or not manifest.experiment.samples:
        return None
    admitted: list[SampleGroup] = []
    excluded: dict[str, int] = {}
    unmeasured: list[str] = []
    for sample in manifest.experiment.samples:
        depth = sample_reads(manifest, sample)
        if depth is None:
            unmeasured.append(sample.sample_id)
        elif depth < floor:
            excluded[sample.sample_id] = depth
        else:
            admitted.append(sample)
    return Admission(
        threshold=floor,
        sample_is_cell=spec.identity.sample_is_cell,
        admitted=tuple(admitted),
        excluded=excluded,
        unmeasured=tuple(unmeasured),
    )


def render_record(
    admission: Admission, *, chemistry: str, kb_version: str, filename_derived: bool
) -> str:
    """The exclusion record: what was dropped, why, how much of the plate it was, and what it cost.

    Written into the pipeline directory because that is the deliverable a human opens, and it is the
    only place the question *"where did those cells go?"* is asked. It is prose plus a table rather
    than a machine format on purpose — the reader is a person, and the machine already has the same
    facts on ``ComposeResult``.

    ``filename_derived`` carries the one disclosure this design owes and cannot repair: with no
    accession anywhere in the deposit, the sample axis came from filenames, so a cell sequenced across
    two runs arrived as two half-depth samples and was gated as two half-cells. Nothing in the bytes
    or the names says two runs are one cell, so it is disclosed at the point of loss rather than
    hidden — and only there, because a compile that dropped nothing lost nothing to disclose.
    """
    noun = admission.noun
    unit = noun[:-1]
    lines = [
        "# Excluded before this pipeline was contracted",
        "",
        f"**{admission.summary}.**",
        "",
        f"Each {unit} below carries fewer reads than the {admission.threshold}-read admission floor "
        f"`{chemistry}` declares (`min_input_reads`), read from the knowledge base loaded at compile "
        f"time ({kb_version}). A {unit} below that floor is not analysed in any form and does not "
        f"block the others.",
        "",
        f"The dataset manifest still carries every one of these {noun} — it is what the data IS, and "
        f"the verdict below is not part of that. Move the floor and the same bytes keep the same "
        f"identity; only this pipeline changes.",
        "",
        "They were never *contracted*, either: `config.yaml`'s `samples` and `units.tsv` are the "
        "post-drop list, so nothing downstream reports these ids as results that failed to arrive.",
    ]
    if filename_derived:
        lines += [
            "",
            f"**No sample in this dataset carries an archive accession, so the {unit} axis came from "
            f"filenames.** A {unit} sequenced across two runs therefore arrived as two half-depth "
            f"samples and was gated as two half-{noun}. Nothing in the bytes or the names says two "
            f"runs are one {unit}, so this is disclosed rather than repaired: check the depths of the "
            f"{noun} that survived, which are in the manifest's provenance, before reading the count "
            f"above as the number of {noun} that failed at the bench.",
        ]
    lines += ["", "| sample_id | reads | threshold |", "| --- | --- | --- |"]
    lines += [
        f"| {sample_id} | {reads} | {admission.threshold} |"
        for sample_id, reads in admission.excluded.items()
    ]
    return "\n".join(lines) + "\n"


__all__ = ["Admission", "admit", "render_record", "sample_reads"]
