"""Tests for ``project.py``'s pure functions — the project views' *shape*, not their plumbing.

``sample_metadata_table``, ``format_tsv`` and ``project_index`` are pure: manifests (or plain dicts)
in, columns/rows/text out. ``tests/test_partition.py``'s two integration tests drive the real
``project metadata`` verb over a two-chemistry workspace; this file covers what those cannot see
cheaply — row order across assays and the injection squashing.

The integration pair stays where it is. It needs the expensive ``two_chemistry_project`` build, which
is module-scoped and pinned with ``xdist_group`` precisely so it happens once; moving it here would
build it twice. This file takes the session ``synth_10x_v3`` manifest and derives copies, so every
test in it is sub-millisecond.

A study named with ``--study`` is joined into the table through the compile verbs themselves:
``project metadata`` over a manifest written by hand, and ``run`` once, for the manifest's hash.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import anndata as ad
import numpy as np
import pytest
import yaml
from scipy.sparse import csr_matrix
from typer.testing import CliRunner

from conftest import SynthDataset, write_fastq_gz
from seqforge import kb
from seqforge.cli import app
from seqforge.models.dataset import DatasetManifest, SampleGroup
from seqforge.project import format_tsv, project_index, sample_metadata_table
from test_study import READING, VALID, _said

runner = CliRunner()


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


#: The four GEO Samples of the joined study, each with the BioSample accession its record links: a
#: young, a middle and an old wild-type library, and a young knockout.
JOINED = {"GSM1": "SAMN1", "GSM3": "SAMN3", "GSM4": "SAMN4", "GSM6": "SAMN6"}


def _study(folder: Path, change: Any = None) -> Path:
    """A study folder holding bounds and a reading of :data:`JOINED`: yeast, by 10x 3' v3."""
    reading = copy.deepcopy(READING)
    reading["samples"] = {
        gsm: {"biosample_accession": samn, **reading["samples"][gsm]}
        for gsm, samn in JOINED.items()
    }
    reading["shared"]["species"] = {
        "text": "Saccharomyces cerevisiae S288C",
        "term": "NCBITaxon:559292",
        "quoted": [2],
        "p": 0.99,
    }
    reading["shared"]["assay"] = {
        "text": "10x 3' v3",
        "term": "EFO:0009922",
        "quoted": [1],
        "p": 0.9,
    }
    if change is not None:
        change(reading)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "bounds.yaml").write_text(yaml.safe_dump(VALID, sort_keys=False))
    (folder / "reading.yaml").write_text(yaml.safe_dump(reading, sort_keys=False))
    return folder


def _compiled(workspace: Path, base: DatasetManifest, accessions: list[str]) -> None:
    """A compiled workspace whose one manifest holds one sample per BioSample accession."""
    manifest = base.model_copy(deep=True)
    manifest.experiment.samples = [
        SampleGroup(sample_id=f"s_{a}", accession=a, file_uris=[f"reads/{a}_R1.fastq.gz"])
        for a in accessions
    ]
    (workspace / "seqforge").mkdir(parents=True)
    (workspace / "seqforge" / "manifest.yaml").write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=True)
    )


def _metadata(workspace: Path, study: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """What ``project metadata --study`` says, and the sample table it writes, row by row."""
    result = runner.invoke(
        app, ["project", "metadata", "-C", str(workspace), "--study", str(study)]
    )
    assert result.exit_code == 0, result.output
    lines = (workspace / "seqforge" / "sample_metadata.tsv").read_text().splitlines()
    header = lines[0].split("\t")
    rows = [dict(zip(header, line.split("\t"), strict=True)) for line in lines[1:]]
    return json.loads(result.stdout), rows


def _batch_on_two(reading: dict[str, Any]) -> None:
    """A label of the study's own that two samples give alike and the other two do not give."""
    for gsm in ("GSM1", "GSM3"):
        reading["samples"][gsm]["batch"] = _said("first")


@pytest.mark.parametrize(
    ("change", "batch"),
    [
        pytest.param(None, None, id="the reading as written"),
        pytest.param(
            _batch_on_two,
            {"SAMN1": "first", "SAMN3": "first", "SAMN4": "", "SAMN6": ""},
            id="a label some samples lack varies",
        ),
    ],
)
def test_a_named_study_joins_its_factors_and_varying_labels_into_the_sample_table(
    tmp_path: Path, synth_10x_v3: SynthDataset, change: Any, batch: dict[str, str] | None
) -> None:
    """The reading owns what each sample was, and the compile joins it by BioSample accession: one
    column per factor and per label that varies across the compiled samples, and the hash of the
    reading the labels came from. A label the same for every sample tells no sample apart, so it
    gets no column; one that some samples lack does tell them apart, so it gets one. A different
    reading has a different hash, which is how a stale label shows."""
    _compiled(tmp_path, synth_10x_v3.manifest, list(JOINED.values()))
    said, rows = _metadata(tmp_path, _study(tmp_path / "GSE1", change))
    views = [tmp_path / "seqforge" / name for name in ("sample_metadata.tsv", "project.yaml")]
    written = [path.stat().st_mtime_ns for path in views]

    assert list(rows[0]) == [
        *["sample_id", "accession", "assay", "organism", "n_files", "files"],
        *["age", "genotype", "biosample", *(["batch"] if batch else []), "reading_hash"],
    ]
    by_accession = {r["accession"]: r for r in rows}
    assert {a: (r["age"], r["genotype"], r["biosample"]) for a, r in by_accession.items()} == {
        "SAMN1": ("young", "wt", "B1"),
        "SAMN3": ("middle", "wt", "B3"),
        "SAMN4": ("old", "wt", "B4"),
        "SAMN6": ("young", "ko", "B5"),
    }
    if batch:
        assert {a: r["batch"] for a, r in by_accession.items()} == batch
    assert {r["reading_hash"] for r in rows} == {said["study"]["reading_hash"]}
    assert said["study"]["warnings"] == []
    # The same inputs again write nothing: a pipeline's labelling rule reads the table's mtime.
    _metadata(tmp_path, tmp_path / "GSE1")
    assert [path.stat().st_mtime_ns for path in views] == written

    def another_age(reading: dict[str, Any]) -> None:
        reading["samples"]["GSM3"]["age"]["text"] = "13 months"

    again, _ = _metadata(tmp_path, _study(tmp_path / "GSE1-again", another_age))
    assert again["study"]["reading_hash"] != said["study"]["reading_hash"]


def _mouse(reading: dict[str, Any]) -> None:
    reading["shared"]["species"] = {"text": "Mus musculus", "term": "NCBITaxon:10090"}
    reading["shared"]["species"] |= {"quoted": [2], "p": 0.99}


def _general_assay(reading: dict[str, Any]) -> None:
    reading["shared"]["assay"]["term"] = "EFO:0008913"


@pytest.mark.parametrize(
    ("accessions", "change", "under", "code", "subject"),
    [
        pytest.param(
            [*JOINED.values(), "SAMN99"],
            None,
            None,
            "SAMPLE_NOT_IN_READING",
            "experiment.samples.s_SAMN99",
            id="a manifest sample with no reading row",
        ),
        pytest.param(
            ["SAMN1", "SAMN3", "SAMN4"],
            None,
            None,
            "READING_SAMPLE_NOT_COMPILED",
            "reading.samples.GSM6",
            id="a reading row with no compiled sample",
        ),
        pytest.param(
            list(JOINED.values()),
            _mouse,
            None,
            "SPECIES_NOT_ORGANISM",
            "reading.samples.GSM1.species",
            id="species and organism of different taxids",
        ),
        pytest.param(
            list(JOINED.values()),
            _general_assay,
            False,
            "ASSAY_NOT_UNDER_READING",
            "reading.samples.GSM1.assay",
            id="a kit term outside the reading's assay term",
        ),
    ],
)
def test_a_reading_that_disagrees_with_the_compile_warns_and_never_refuses(
    tmp_path: Path,
    synth_10x_v3: SynthDataset,
    monkeypatch: pytest.MonkeyPatch,
    accessions: list[str],
    change: Any,
    under: bool | None,
    code: str,
    subject: str,
) -> None:
    """Neither the manifest nor the reading observes the bytes, so where they disagree the verb
    warns, exits 0 and still writes the table: a reading never blocks a compile. Whether a kit's
    assay term is under the reading's is the ontology's to say, so the test gives that answer."""
    if under is not None:
        monkeypatch.setattr("seqforge.study.join.term_under", lambda term, ancestor: under)
    _compiled(tmp_path, synth_10x_v3.manifest, accessions)
    said, rows = _metadata(tmp_path, _study(tmp_path / "GSE1", change))

    warned = said["study"]["warnings"]
    assert {w["code"] for w in warned} == {code}
    assert subject in {w["subject"]["ref"] for w in warned}
    assert len(rows) == len(accessions)


def test_a_named_study_leaves_the_manifest_and_its_hash_as_they_were(tmp_path: Path) -> None:
    """The dataset is write-once, and the reading's labels are a view joined from the reading: a
    compile run with ``--study`` writes the very manifest the same compile writes without one."""
    reads = kb.generate_reads(kb.load_spec("bulk-rnaseq"), n=600, seed=0)
    for key in ("R1", "R2"):
        write_fastq_gz(tmp_path / f"s_{key}.fastq.gz", reads[key])
    compile_ = [
        "run",
        *(str(tmp_path / f"s_{key}.fastq.gz") for key in ("R1", "R2")),
        *["--organism", "559292", "--assembly", "sacCer3", "--annotation", "ensembl"],
        *["--no-llm", "-C", str(tmp_path)],
    ]

    def manifest_hash() -> str:
        manifest = tmp_path / "seqforge" / "manifest.yaml"
        said = runner.invoke(app, ["manifest", "hash", str(manifest)])
        assert said.exit_code == 0, said.output
        return said.stdout

    without = runner.invoke(app, compile_)
    assert without.exit_code == 0, without.stdout
    before = manifest_hash()
    joined = runner.invoke(app, [*compile_, "--study", str(_study(tmp_path / "GSE1"))])
    assert joined.exit_code == 0, joined.stdout

    assert json.loads(joined.stdout)["stages"]["project"]["study"]["reading_hash"]
    assert manifest_hash() == before


def test_each_h5ad_carries_its_samples_labels_as_categorical_obs_and_the_readings_hash(
    tmp_path: Path, synth_10x_v3: SynthDataset
) -> None:
    """The last rule of a droplet pipeline labels each count object from the sample table, through
    the verb its ``shell:`` calls: one categorical ``obs`` column per label in the table, and the
    hash of the reading they came from in ``uns``. A compile that named no study has no labels to
    write, so its object is the one the packaging step wrote, byte for byte, and a hard link to it,
    so a compile with no study keeps one copy of the matrices on disk as it did before labels."""
    counts = tmp_path / "unlabelled.h5ad"
    ad.AnnData(X=csr_matrix(np.eye(3, dtype=np.int32))).write_h5ad(counts)
    table = tmp_path / "seqforge" / "sample_metadata.tsv"

    def label(sample: str, out: Path) -> Path:
        argv = ["io", "h5ad-labels", str(counts), "--sample", sample, "--table", str(table)]
        result = runner.invoke(app, [*argv, "--out", str(out)])
        assert result.exit_code == 0, result.output
        return out

    _compiled(tmp_path, synth_10x_v3.manifest, list(JOINED.values()))
    plain = runner.invoke(app, ["project", "metadata", "-C", str(tmp_path)])
    assert plain.exit_code == 0, plain.output
    plain_h5ad = label("s_SAMN3", tmp_path / "plain.h5ad")
    assert plain_h5ad.read_bytes() == counts.read_bytes()
    assert plain_h5ad.samefile(counts)

    said, _ = _metadata(tmp_path, _study(tmp_path / "GSE1"))
    labelled = ad.read_h5ad(label("s_SAMN3", tmp_path / "s_SAMN3.h5ad"))

    obs: Any = labelled.obs
    assert list(obs.columns) == ["age", "genotype", "biosample"]
    assert all(str(obs[c].dtype) == "category" for c in obs.columns)
    assert obs.iloc[0].to_dict() == {"age": "middle", "genotype": "wt", "biosample": "B3"}
    assert labelled.uns["reading_hash"] == said["study"]["reading_hash"]
    assert np.array_equal(csr_matrix(labelled.X).toarray(), np.eye(3, dtype=np.int32))
