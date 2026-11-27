"""The format checks of a study's files and the relations of a reading, through ``seqforge study``.

Every file here is written by hand in the test. The repo's own 50 bounds files are checked where the
standard set is listed, in ``test_study_folder.py``.
"""

from __future__ import annotations

import copy
import datetime
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from seqforge.cli import app

runner = CliRunner()

#: One claim of each backing: a quoted Series, an inferred Series, and a left-out paper marked unknown.
VALID: dict[str, Any] = {
    "format": 1,
    "study": "GSE1",
    "models": {"bounds": "test/large", "probabilities": "test/small"},
    "series": [
        {"id": "GSE1", "quoted": [1, 2], "p": 0.9},
        {"id": "GSE2", "inferred": "Same samples, later run.", "see": [2], "p": 1},
    ],
    "papers": ["pmid:11"],
    "left_out": [{"id": "pmid:22", "unknown": "Nothing read says who made it", "p": None}],
    "reason": "One paper and the two Series it deposited.",
    "passages": {
        1: {"record": "GSE1", "field": "Series_summary", "quote": "a quote"},
        2: {"paper": "pmid:11", "section": "Methods", "quote": "another"},
    },
}


#: A rejection whose reason is the type checker's own wording is held by its field alone.
ITS_TYPE = ""


def _check(
    tmp_path: Path, content: dict[str, Any] | str, verb: str = "check-bounds"
) -> tuple[int, dict[str, Any]]:
    """What ``verb`` says of one file, whose content is given, or its text as it is written."""
    path = tmp_path / "study-file.yaml"
    text = content if isinstance(content, str) else yaml.safe_dump(content, sort_keys=False)
    path.write_text(text)
    result = runner.invoke(app, ["study", verb, str(path)])
    return result.exit_code, json.loads(result.stdout)


def _a_key_twice(content: dict[str, Any]) -> str:
    """The file's text with its ``study`` line written a second time, which a dict cannot hold."""
    return yaml.safe_dump(content, sort_keys=False) + "study: GSE1\n"


def _without(*path: str | int) -> Any:
    def change(bounds: dict[str, Any]) -> None:
        *way, last = path
        at: Any = bounds
        for step in way:
            at = at[step]
        del at[last]

    return change


def _with(value: Any, *path: str | int) -> Any:
    def change(bounds: dict[str, Any]) -> None:
        *way, last = path
        at: Any = bounds
        for step in way:
            at = at[step]
        at[last] = value

    return change


def test_a_bounds_file_in_the_current_format_is_accepted(tmp_path: Path) -> None:
    """A claim of each backing, a passage from a record and one from a paper, a null probability."""
    code, said = _check(tmp_path, VALID)

    assert (code, said["valid"], said["problems"]) == (0, True, [])


@pytest.mark.parametrize(
    ("change", "field", "reason"),
    [
        pytest.param(_with(2, "format"), "format", "expected 1", id="another format number"),
        pytest.param(_without("format"), "format", "expected 1", id="no format number"),
        pytest.param(
            _with({}, "split_samples"), "split_samples", "not a field", id="a sample split"
        ),
        pytest.param(
            _with("no label applies", "left_out", 0, "na"),
            "left_out[0].na",
            "not a field",
            id="a mark the bounds do not have",
        ),
        pytest.param(
            _without("series", 0, "quoted"), "series[0]", "this one has none", id="no backing mark"
        ),
        pytest.param(
            _with("Worked out.", "series", 0, "inferred"),
            "series[0]",
            "this one has quoted and inferred",
            id="two backing marks",
        ),
        pytest.param(
            _with([1, 7], "series", 0, "quoted"),
            "series[0].quoted",
            "passage 7 does not exist",
            id="a quoted claim points to a passage that is not there",
        ),
        pytest.param(
            _with([], "series", 0, "quoted"),
            "series[0].quoted",
            "at least one passage",
            id="a quoted claim points to no passage",
        ),
        pytest.param(
            _with([7], "series", 1, "see"),
            "series[1].see",
            "passage 7 does not exist",
            id="an inferred claim rests on a passage that is not there",
        ),
        pytest.param(
            _with(" ", "series", 1, "inferred"),
            "series[1].inferred",
            "says how",
            id="inferred without saying how",
        ),
        pytest.param(
            _with(1.5, "left_out", 0, "p"),
            "left_out[0].p",
            ITS_TYPE,
            id="a probability above one",
        ),
        pytest.param(
            _with("0.5", "series", 0, "p"), "series[0].p", ITS_TYPE, id="a probability in quotes"
        ),
        pytest.param(
            _with(True, "series", 0, "p"), "series[0].p", ITS_TYPE, id="a probability of true"
        ),
        pytest.param(
            _with(["1"], "series", 0, "quoted"),
            "series[0].quoted[0]",
            ITS_TYPE,
            id="a passage number in quotes",
        ),
        pytest.param(
            _with("GSM3618670", "series", 0, "id"),
            "series[0].id",
            "GSE<number>",
            id="a sample kept as a Series",
        ),
        pytest.param(
            _with(["GSE5"], "papers"), "papers[0]", "pmid:<number>", id="a Series among the papers"
        ),
        pytest.param(_with(" ", "reason"), "reason", "blank", id="a blank reason"),
        pytest.param(
            _with(" ", "left_out", 0, "id"), "left_out[0].id", "blank", id="a left-out blank id"
        ),
        pytest.param(
            _with(" ", "left_out", 0, "unknown"),
            "left_out[0].unknown",
            "blank",
            id="a left-out record with a blank note",
        ),
        pytest.param(_a_key_twice, "study", "written twice", id="a key written twice"),
        pytest.param(
            _with(None, "models", "probabilities"),
            "series[0].p",
            "models.probabilities",
            id="a probability no model is named for",
        ),
        pytest.param(
            _with("GSE2", "left_out", 0, "id"),
            "left_out[0].id",
            "already placed at series[1].id",
            id="a record both kept and left out",
        ),
        pytest.param(
            _with({"GSE9": "A Series"}, "titles"),
            "titles.GSE9",
            "not a record these bounds place",
            id="a title for a record the bounds do not place",
        ),
        pytest.param(
            _without("passages", 1, "field"),
            "passages[1]",
            "a record and its field",
            id="a passage that does not say where it was read",
        ),
    ],
)
def test_a_broken_bounds_file_is_rejected_with_the_field_and_the_reason(
    tmp_path: Path, change: Any, field: str, reason: str
) -> None:
    """One format at a time, no sample split, and no claim without its backing: a file that breaks
    the written format exits as a refusal, and each problem names where it is and why."""
    bounds = copy.deepcopy(VALID)

    code, said = _check(tmp_path, change(bounds) or bounds)

    assert (code, said["valid"]) == (3, False)
    assert any(p["field"] == field and reason in p["reason"] for p in said["problems"]), said


def _said(text: str, **more: Any) -> dict[str, Any]:
    return {"text": text, **more, "quoted": [1], "p": 0.9}


def _gap(mark: str, **more: Any) -> dict[str, Any]:
    return {mark: "A note on the gap.", **more, "p": 0.8}


def _material(biosample: str, **own: Any) -> dict[str, Any]:
    return {"biosample": {"id": biosample, "inferred": "One prep per library.", "p": 0.7}, **own}


#: Nine libraries of one pull-down study: two young wild-type replicates, one middle and one old
#: wild-type library, and one young knockout. An input was made from the material of the first
#: replicate and of the middle library, and an IgG library from the old one's. One more pull-down
#: from the first replicate's material has a control type that nothing read gives.
READING: dict[str, Any] = {
    "format": 2,
    "study": "GSE1",
    "models": {"reading": "test/large", "probabilities": "test/small"},
    "read": ["records", "abstract", "full_text"],
    "read_detail": "The Series, its six samples and the one paper.",
    "ontology_snapshot": {"source": "a test snapshot", "date": datetime.date(2026, 11, 21)},
    "passages": {
        1: {"record": "GSE1", "field": "Series_overall_design", "quote": "a quote"},
        2: {"record": "each_sample", "field": "Sample_organism_ch1", "quote": "Mus musculus"},
        3: {"record": ["GSM5"], "field": "Sample_title", "quote": "input"},
        4: {"paper": "pmid:11", "section": "Methods", "quote": "fed standard chow"},
    },
    "factors": {
        "age": {
            "levels": ["young", "middle", "old"],
            "baseline": "young",
            "ordered": True,
            "quoted": [1],
            "p": 0.9,
        },
        "genotype": {
            "levels": ["wt", "ko"],
            "baseline": "wt",
            "ordered": False,
            "inferred": "The titles name two genotypes.",
            "see": [1],
            "p": 0.8,
        },
    },
    "shared": {
        "donor": _gap("na"),
        "assay": _said("ChIP-Seq"),
        "target": _said("H3K4me3"),
        "control_type": _gap("na", see=[1]),
        "species": {"text": "Mus musculus", "term": "NCBITaxon:10090", "quoted": [2], "p": 0.99},
        "sex": {"unknown": "No record gives it.", "see": [1], "p": None},
        "age": _said("2 months", value=2, unit="month", level="young"),
        "development_stage": _gap("unknown"),
        "tissue": _said("liver", term="UBERON:0002107"),
        "cell_type": _gap("na"),
        "cell_line": _gap("na"),
        "disease": {
            "text": "healthy",
            "term": "PATO:0000461",
            "inferred": "No disease is named.",
            "p": 0.8,
        },
        "genotype": _said("wild type", level="wt"),
        "strain": _gap("unknown"),
        "treatment": _said("bleomycin and DMSO", term=["CHEBI:22907", "CHEBI:28262"]),
        "dose": _said("per side"),
        "duration": _said("overnight"),
        "diet": {"text": "standard chow", "value": 5, "unit": "% fat", "quoted": [4], "p": 0.9},
    },
    "samples": {
        "GSM1": _material("B1"),
        "GSM2": _material("B2"),
        "GSM3": _material("B3", age=_said("12 months", value=12, unit="month", level="middle")),
        "GSM4": _material(
            "B4", age=_said("20 to 24 months", value=[20, 24], level="old", against=[4])
        ),
        "GSM5": _material(
            "B1", target=_gap("na"), control_type={"text": "input", "quoted": [3], "p": 0.9}
        ),
        "GSM6": _material("B5", genotype=_said("knockout", level="ko")),
        "GSM7": _material("B1", control_type=_gap("unknown")),
        "GSM8": _material(
            "B3",
            target=_gap("na"),
            control_type=_said("input"),
            age=_said("12 months", value=12, unit="month", level="middle"),
        ),
        "GSM9": _material(
            "B4",
            target=_gap("na"),
            control_type=_said("IgG"),
            age=_said("20 to 24 months", value=[20, 24], level="old"),
        ),
    },
}


def _a_factor_one_row_writes(reading: dict[str, Any]) -> None:
    reading["factors"]["batch"] = {**reading["factors"]["genotype"], "levels": ["a", "b"]}
    reading["factors"]["batch"]["baseline"] = None
    reading["samples"]["GSM1"]["batch"] = _said("first", level="a")


def test_a_reading_in_the_current_format_is_accepted(tmp_path: Path) -> None:
    """Each of the four backings, a passage of each source, a label of the study's own, a range, a
    row that replaces a shared claim, a gap that names the passages it rests on, a claim that names
    a passage against it, a treatment of two compounds, a dose and a duration with no number, a
    number on the study's own label, and the id for healthy as a disease term."""
    code, said = _check(tmp_path, READING, "check-reading")

    assert (code, said["valid"], said["problems"]) == (0, True, [])


@pytest.mark.parametrize(
    ("change", "field", "reason"),
    [
        pytest.param(_with(1, "format"), "format", "expected 2", id="an older format number"),
        pytest.param(
            _with(["records", "full text"], "read"),
            "read[1]",
            ITS_TYPE,
            id="something read that the format does not name",
        ),
        pytest.param(_with([], "factors"), "factors", "not a mapping", id="factors as a list"),
        pytest.param(
            _with("No pull-down.", "shared", "target", "na"),
            "shared.target",
            "this one has quoted and na",
            id="a claim both quoted and marked as not applying",
        ),
        pytest.param(
            _with([1], "shared", "assay", "see"),
            "shared.assay.see",
            "no see beside it",
            id="a quoted claim that also rests on passages",
        ),
        pytest.param(
            _with([9], "factors", "age", "quoted"),
            "factors.age.quoted",
            "passage 9 does not exist",
            id="a factor backed by a passage that is not there",
        ),
        pytest.param(
            _with([9], "shared", "assay", "against"),
            "shared.assay.against",
            "passage 9 does not exist",
            id="a claim that names a passage against it that is not there",
        ),
        pytest.param(
            _with(["UBERON:0002107", "UBERON:0001114"], "shared", "tissue", "term"),
            "shared.tissue.term",
            "only for a treatment",
            id="a list of terms on a label that is not the treatment",
        ),
        pytest.param(
            _without("ontology_snapshot"),
            "ontology_snapshot",
            "missing",
            id="ontology ids with no snapshot named",
        ),
        pytest.param(
            _with(
                {"levels": ["H3K4me3"], "baseline": None, "ordered": False, "quoted": [1], "p": 1},
                "factors",
                "target",
            ),
            "factors.target",
            "never a factor",
            id="the target of a pull-down made a factor",
        ),
        pytest.param(
            _without("shared", "strain"),
            "samples.GSM1.strain",
            "missing",
            id="a label no row and no shared claim writes",
        ),
        pytest.param(
            _with(_gap("unknown"), "shared", "species"),
            "shared.species",
            "never unknown or na",
            id="a species that is not known",
        ),
        pytest.param(
            _without("samples", "GSM1", "biosample", "id"),
            "samples.GSM1.biosample.id",
            "missing",
            id="a biosample with no id",
        ),
        pytest.param(
            _with("Perhaps an inbred line.", "shared", "strain", "text"),
            "shared.strain.text",
            "gives no value",
            id="a gap that still says something",
        ),
        pytest.param(
            _with({"na": "Not aged.", "level": "old", "p": 0.5}, "samples", "GSM4", "age"),
            "samples.GSM4.age.level",
            "gives no value",
            id="a gap that still names a level",
        ),
        pytest.param(
            _with(" ", "shared", "donor", "na"), "shared.donor.na", "blank", id="a blank reason"
        ),
        pytest.param(
            _with({"quoted": [1], "p": 0.9}, "shared", "assay"),
            "shared.assay",
            "says something",
            id="a quoted claim that says nothing",
        ),
        pytest.param(
            _with("2", "shared", "age", "value"),
            "shared.age.value",
            "a number, or a range",
            id="a value in quotes",
        ),
        pytest.param(
            _with(["young", "middle", "old", "ancient"], "factors", "age", "levels"),
            "factors.age.levels",
            "no sample is at the level ancient",
            id="a level no sample is at",
        ),
        pytest.param(
            _without("samples", "GSM3", "age", "level"),
            "samples.GSM3.age.level",
            "the shared claim this one replaces names a level",
            id="a row that replaces a shared level and names none",
        ),
        pytest.param(_a_key_twice, "study", "written twice", id="a key written twice"),
        pytest.param(
            _with("ancient", "samples", "GSM4", "age", "level"),
            "samples.GSM4.age.level",
            "ancient is not a level of the factor age",
            id="a level the factor does not have",
        ),
        pytest.param(
            _with("liver", "shared", "tissue", "level"),
            "shared.tissue.level",
            "tissue is not a factor",
            id="a level on a label the study did not vary",
        ),
        pytest.param(
            _with("newborn", "factors", "age", "baseline"),
            "factors.age.baseline",
            "newborn is not one of the factor's levels",
            id="a baseline that is not a level",
        ),
        pytest.param(
            _with(["young", "middle", "old", "old"], "factors", "age", "levels"),
            "factors.age.levels",
            "named twice",
            id="a level named twice",
        ),
        pytest.param(
            _with([2, 12, 22], "factors", "age", "levels"),
            "factors.age.levels[0]",
            ITS_TYPE,
            id="a level name that is a number",
        ),
        pytest.param(
            _with(
                {"levels": ["a"], "baseline": "a", "ordered": False, "quoted": [1], "p": 1},
                "factors",
                "batch",
            ),
            "factors.batch",
            "no sample has this label",
            id="a factor on a label no sample has",
        ),
        pytest.param(
            _a_factor_one_row_writes,
            "samples.GSM2.batch",
            "missing",
            id="a factor on a label some samples lack",
        ),
        pytest.param(
            _with({"text": "F", "term": "F", "quoted": [1], "p": 0.9}, "shared", "sex"),
            "shared.sex.term",
            "one of female, male, hermaphrodite or mixed",
            id="a sex term that is not one of the four words",
        ),
        pytest.param(
            _with("WBbt:0005772", "shared", "tissue", "term"),
            "shared.tissue.term",
            "comes from uberon",
            id="a term from another species' vocabulary",
        ),
        pytest.param(
            _with(3, "shared", "tissue", "value"),
            "shared.tissue.value",
            "tissue takes no number",
            id="a number on a label that takes none",
        ),
    ],
)
def test_a_broken_reading_is_rejected_with_the_field_and_the_reason(
    tmp_path: Path, change: Any, field: str, reason: str
) -> None:
    """A reading that breaks the written format exits as a refusal, and each problem names where it
    is and why. The relations are refused for the same problems, and none is printed."""
    reading = copy.deepcopy(READING)
    broken = change(reading) or reading

    code, said = _check(tmp_path, broken, "check-reading")

    assert (code, said["valid"]) == (3, False)
    assert any(p["field"] == field and reason in p["reason"] for p in said["problems"]), said
    assert _check(tmp_path, broken, "relations") == (3, said)


def test_the_relations_of_a_reading_are_worked_out_from_its_claims(tmp_path: Path) -> None:
    """All five kinds from one reading, in the file's order: the replicate pair, each contrast
    against a baseline with the other factor held, the runs along the ordered factor, the
    libraries that share material, and each pull-down's control.

    A control library is one whose control type is said. So the pull-down whose control type is
    unknown is a signal and no control, and it is no replicate of the two whose control type is
    na. The input run and the IgG library never share a series.
    """
    code, said = _check(tmp_path, READING, "relations")

    chip = {"assay": "ChIP-Seq", "target": "H3K4me3"}
    assert (code, said["valid"]) == (0, True)
    assert said["relations"] == {
        "replicates": [
            {
                **chip,
                "control_type": None,
                "levels": {"age": "young", "genotype": "wt"},
                "samples": ["GSM1", "GSM2"],
            }
        ],
        "contrasts": [
            {
                "factor": "age",
                "baseline": "young",
                "levels": ["young", "middle"],
                "samples": {"young": ["GSM1", "GSM2"], "middle": ["GSM3"]},
                **chip,
                "fixed": {"genotype": "wt"},
            },
            {
                "factor": "age",
                "baseline": "young",
                "levels": ["young", "old"],
                "samples": {"young": ["GSM1", "GSM2"], "old": ["GSM4"]},
                **chip,
                "fixed": {"genotype": "wt"},
            },
            {
                "factor": "genotype",
                "baseline": "wt",
                "levels": ["wt", "ko"],
                "samples": {"wt": ["GSM1", "GSM2"], "ko": ["GSM6"]},
                **chip,
                "fixed": {"age": "young"},
            },
        ],
        "series": [
            {
                "factor": "age",
                **chip,
                "control_type": None,
                "fixed": {"genotype": "wt"},
                "steps": [
                    {"level": "young", "samples": ["GSM1", "GSM2"]},
                    {"level": "middle", "samples": ["GSM3"]},
                    {"level": "old", "samples": ["GSM4"]},
                ],
            },
            {
                "factor": "age",
                "assay": "ChIP-Seq",
                "target": None,
                "control_type": "input",
                "fixed": {"genotype": "wt"},
                "steps": [
                    {"level": "young", "samples": ["GSM5"]},
                    {"level": "middle", "samples": ["GSM8"]},
                ],
            },
        ],
        "shared_biosample": [
            {"biosample": "B1", "samples": ["GSM1", "GSM5", "GSM7"]},
            {"biosample": "B3", "samples": ["GSM3", "GSM8"]},
            {"biosample": "B4", "samples": ["GSM4", "GSM9"]},
        ],
        "signal_to_control": [
            {"biosample": "B1", "signal": "GSM1", "control": "GSM5"},
            {"biosample": "B1", "signal": "GSM7", "control": "GSM5"},
            {"biosample": "B3", "signal": "GSM3", "control": "GSM8"},
            {"biosample": "B4", "signal": "GSM4", "control": "GSM9"},
        ],
    }


def test_a_factor_with_no_baseline_contrasts_each_pair_of_its_levels_once(tmp_path: Path) -> None:
    """A study that compares its levels with each other names no reference level, so no side of a
    contrast is the baseline and every pair is one contrast, in the order the levels are written."""
    reading = copy.deepcopy(READING)
    reading["factors"]["age"]["baseline"] = None

    _, said = _check(tmp_path, reading, "relations")

    by_age = [c for c in said["relations"]["contrasts"] if c["factor"] == "age"]
    assert [(c["baseline"], c["levels"]) for c in by_age] == [
        (None, ["young", "middle"]),
        (None, ["young", "old"]),
        (None, ["middle", "old"]),
    ]


@pytest.mark.parametrize(
    "verb",
    [
        pytest.param(["study", "check-reading", "GSE1/reading.yaml"], id="study check-reading"),
        pytest.param(["standard-set", "list", "--dir", "."], id="standard-set list"),
    ],
)
def test_a_reading_check_without_liulab_data_refuses_to_run_and_names_the_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: list[str]
) -> None:
    """liulab-data holds each vocabulary's id prefixes, and an installed copy of seqforge can lack
    it. The check then cannot judge an id: it refuses the run as a setup it cannot use, and names
    the remedy, rather than reject a file that is valid. Listing a set whose study holds a reading
    runs the same check, and refuses the same way."""
    monkeypatch.setitem(sys.modules, "labdata.ontology.vocabularies", None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "GSE1").mkdir()
    (tmp_path / "pick.yaml").write_text(yaml.safe_dump({"studies": [{"id": "GSE1"}]}))
    for name, content in (("bounds.yaml", VALID), ("reading.yaml", READING)):
        (tmp_path / "GSE1" / name).write_text(yaml.safe_dump(content, sort_keys=False))

    result = runner.invoke(app, verb)

    assert result.exit_code == 2
    assert "liulab-runtime" in result.output


def test_a_file_that_is_not_text_cannot_be_read(tmp_path: Path) -> None:
    """Bytes that are not UTF-8 are no study file in any format: the command says it cannot read
    the file and exits as it does for a missing one, where a rejection would name a field."""
    path = tmp_path / "reading.yaml"
    path.write_bytes(b"format: 1\nstudy: \xff\xfe\n")

    result = runner.invoke(app, ["study", "check-reading", str(path)])

    assert result.exit_code == 2
    assert "cannot read" in result.output
