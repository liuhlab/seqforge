"""Tests for ``project.py``'s pure functions — the project views' *shape*, not their plumbing.

``sample_metadata_table``, ``format_tsv`` and ``project_index`` are pure: manifests (or plain dicts)
in, columns/rows/text out. They had no direct test. What covered them was
``tests/test_partition.py``'s two integration tests, which drive the real ``project metadata`` verb
over a two-chemistry workspace — and whose synthetic samples carry **no BioSample attributes at all**.
So the union-of-what-resolved column logic, the empty-cell rule, and the injection squashing were
each entirely unexercised: the table those tests render has a fixed 6-column shape and can never show
the interesting case.

The integration pair stays where it is. It needs the expensive ``two_chemistry_project`` build, which
is module-scoped and pinned with ``xdist_group`` precisely so it happens once; moving it here would
build it twice. This file takes the session ``synth_10x_v3`` manifest and derives copies, so every
test in it is sub-millisecond.
"""

from __future__ import annotations

from conftest import SynthDataset
from seqforge.models.dataset import DatasetManifest, SampleGroup
from seqforge.models.evidenced import EvidencedStr
from seqforge.project import format_tsv, project_index, sample_metadata_table


def _assay(
    base: DatasetManifest, chemistry: str, sample_id: str, attrs: dict[str, str], n_files: int
) -> DatasetManifest:
    """One single-sample manifest for `chemistry`, carrying exactly `attrs`.

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
            attributes={
                key: EvidencedStr(value=value, basis="asserted", rung=0)
                for key, value in attrs.items()
            },
        )
    ]
    return manifest


def test_the_attribute_columns_are_the_union_of_what_resolved_not_a_fixed_schema(
    synth_10x_v3: SynthDataset,
) -> None:
    """The column set is discovered from the data, then ordered: common attributes in a readable
    order, everything else sorted after them.

    A hand-fixed schema is the shape this repo keeps getting bitten by — it silently drops an
    attribute nobody thought to list. `isolate` is here to prove a name outside `_PREFERRED_ATTRS`
    still gets a column.
    """
    bulk = _assay(
        synth_10x_v3.manifest, "bulk-rnaseq-pe", "s_bulk", {"strain": "CQ757", "isolate": "iso1"}, 1
    )
    tenx = _assay(
        synth_10x_v3.manifest,
        "10x-3p-gex-v3",
        "s_10x",
        {"tissue": "Neurons", "dev_stage": "Adult"},
        2,
    )

    columns, rows = sample_metadata_table([bulk, tenx])

    # strain/dev_stage/tissue are _PREFERRED_ATTRS, in THAT order (not the order they were seen);
    # `isolate` is not, so it follows them, sorted.
    assert columns == [
        "sample_id",
        "accession",
        "assay",
        "organism",
        "strain",
        "dev_stage",
        "tissue",
        "isolate",
        "n_files",
        "files",
    ]
    # ...and the rows are ordered by assay, not by the order the manifests were handed over.
    assert [r["sample_id"] for r in rows] == ["s_10x", "s_bulk"]
    # `files` carries basenames only: a manifest is machine-independent (R7), and so is a view of one.
    assert rows[0]["files"] == "s_10x_R1.fastq.gz;s_10x_R2.fastq.gz"
    assert rows[0]["n_files"] == "2"


def test_an_attribute_that_did_not_resolve_is_an_empty_cell_not_a_guess(
    synth_10x_v3: SynthDataset,
) -> None:
    """Absence, honestly. The 10x sample has no `strain`, so its `strain` cell is empty — not "NA",
    not "None", and not the other sample's value carried across."""
    bulk = _assay(synth_10x_v3.manifest, "bulk-rnaseq-pe", "s_bulk", {"strain": "CQ757"}, 1)
    tenx = _assay(synth_10x_v3.manifest, "10x-3p-gex-v3", "s_10x", {"tissue": "Neurons"}, 1)

    columns, rows = sample_metadata_table([bulk, tenx])
    tenx_row = next(r for r in rows if r["sample_id"] == "s_10x")
    assert "strain" not in tenx_row  # the row does not invent the key...

    body = format_tsv(columns, rows).splitlines()[1:]
    rendered = dict(zip(columns, body[0].split("\t"), strict=True))
    assert rendered["sample_id"] == "s_10x"
    assert rendered["strain"] == ""  # ...and rendering it produces an empty cell, not a placeholder
    assert rendered["tissue"] == "Neurons"


def test_a_tab_or_newline_in_a_value_cannot_break_the_tables_shape() -> None:
    """A TSV whose cells may contain tabs is not a TSV. The attribute values come from prose an LLM
    read out of an archive record, so a tab or a newline in one is an input, not a hypothetical —
    and it must cost a mangled *value*, never a shifted column or an extra row.
    """
    columns = ["sample_id", "treatment", "n_files"]
    rows = [
        {"sample_id": "s1", "treatment": "20\tmM\tparaquat", "n_files": "2"},
        {"sample_id": "s2", "treatment": "fed\nad libitum", "n_files": "1"},
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
            {"chemistry": "bulk-rnaseq-pe", "subdir": "bulk-rnaseq-pe", "n_samples": 1},
        ]
    )

    assert [a["chemistry"] for a in index["assays"]] == [
        "10x-3p-gex-v3",
        "bulk-rnaseq-pe",
        "splitseq-v2",
    ]
    assert index["n_assays"] == 3
    assert index["n_samples"] == 6
