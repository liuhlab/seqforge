"""Tests for ``project.py``'s pure functions — the project views' *shape*, not their plumbing.

``sample_metadata_table``, ``format_tsv`` and ``project_index`` are pure: manifests (or plain dicts)
in, columns/rows/text out. ``tests/test_partition.py``'s two integration tests drive the real
``project metadata`` verb over a two-chemistry workspace; this file covers what those cannot see
cheaply — row order across assays and the injection squashing.

The integration pair stays where it is. It needs the expensive ``two_chemistry_project`` build, which
is module-scoped and pinned with ``xdist_group`` precisely so it happens once; moving it here would
build it twice. This file takes the session ``synth_10x_v3`` manifest and derives copies, so every
test in it is sub-millisecond.
"""

from __future__ import annotations

from conftest import SynthDataset
from seqforge.models.dataset import DatasetManifest, SampleGroup
from seqforge.project import format_tsv, project_index, sample_metadata_table


def _assay(base: DatasetManifest, chemistry: str, sample_id: str, n_files: int) -> DatasetManifest:
    """One single-sample manifest for `chemistry`.

    Derived from the session manifest rather than built by hand, so it stays a manifest the
    validators actually accept — a hand-rolled stand-in would drift from the real model silently.
    """
    manifest = base.model_copy(deep=True)
    manifest.library.chemistry = manifest.library.chemistry.model_copy(
        update={"value": [chemistry]}
    )
    manifest.experiment.samples = [
        SampleGroup(
            sample_id=sample_id,
            file_uris=[f"reads/{sample_id}_R{i + 1}.fastq.gz" for i in range(n_files)],
        )
    ]
    return manifest


def test_the_rows_are_ordered_by_assay_and_name_files_by_basename(
    synth_10x_v3: SynthDataset,
) -> None:
    """The table is regenerated on every run, so its row order must come from the content and not
    from the order the manifests were handed over."""
    bulk = _assay(synth_10x_v3.manifest, "bulk-rnaseq", "s_bulk", 1)
    tenx = _assay(synth_10x_v3.manifest, "10x-3p-gex-v3", "s_10x", 2)

    columns, rows = sample_metadata_table([bulk, tenx])

    assert columns == ["sample_id", "accession", "assay", "organism", "n_files", "files"]
    assert [r["sample_id"] for r in rows] == ["s_10x", "s_bulk"]
    # `files` carries basenames only: a manifest is machine-independent, and so is a view of one.
    assert rows[0]["files"] == "s_10x_R1.fastq.gz;s_10x_R2.fastq.gz"
    assert rows[0]["n_files"] == "2"


def test_a_tab_or_newline_in_a_value_cannot_break_the_tables_shape() -> None:
    """A TSV whose cells may contain tabs is not a TSV. Sample ids and file names come from archive
    records and from whatever a submitter named their files, so a tab or a newline in one is an
    input, not a hypothetical — and it must cost a mangled *value*, never a shifted column or an
    extra row.
    """
    columns = ["sample_id", "files", "n_files"]
    rows = [
        {"sample_id": "s1", "files": "20\tmM\tparaquat", "n_files": "2"},
        {"sample_id": "s2", "files": "fed\nad libitum", "n_files": "1"},
    ]

    lines = format_tsv(columns, rows).splitlines()

    assert len(lines) == 3, "a newline in a value must not open a new row"
    assert all(line.count("\t") == len(columns) - 1 for line in lines), (
        "a tab in a value must not open a new column"
    )
    # The value survives, whitespace-squashed rather than dropped.
    assert lines[1].split("\t")[1] == "20 mM paraquat"
    assert lines[2].split("\t")[1] == "fed ad libitum"


def test_the_index_sorts_assays_by_chemistry_and_totals_their_samples() -> None:
    """`project.yaml` is regenerated on every run, so its ordering must come from the content and not
    from whichever order `discover_assays` happened to walk the directory in."""
    index = project_index(
        [
            {"chemistry": "splitseq-v2", "subdir": "splitseq-v2", "n_samples": 3},
            {"chemistry": "10x-3p-gex-v3", "subdir": "10x-3p-gex-v3", "n_samples": 2},
            {"chemistry": "bulk-rnaseq", "subdir": "bulk-rnaseq", "n_samples": 1},
        ]
    )

    assert [a["chemistry"] for a in index["assays"]] == [
        "10x-3p-gex-v3",
        "bulk-rnaseq",
        "splitseq-v2",
    ]
    assert index["n_assays"] == 3
    assert index["n_samples"] == 6
