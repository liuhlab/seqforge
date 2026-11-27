"""The study test kit: valid study files written by hand, and the compiles a reading joins.

Imported by the tests of the study files, of the study folder, of the join and of the report's
Study tab, which all build their folders from these. It lives here and not in a test module so that
no test module imports another.
"""

from __future__ import annotations

import copy
import datetime
from pathlib import Path
from typing import Any

import yaml

from seqforge.models.dataset import DatasetManifest, SampleGroup

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


def quoted(text: str, **more: Any) -> dict[str, Any]:
    """A claim of ``text``, quoted from the first passage."""
    return {"text": text, **more, "quoted": [1], "p": 0.9}


def gap(mark: str, **more: Any) -> dict[str, Any]:
    """A gap: a claim marked ``unknown`` or ``na``, with its note."""
    return {mark: "A note on the gap.", **more, "p": 0.8}


def material(biosample: str, **own: Any) -> dict[str, Any]:
    """A reading row of the material ``biosample``, and the claims of its own."""
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
        "donor": gap("na"),
        "assay": quoted("ChIP-Seq"),
        "target": quoted("H3K4me3"),
        "control_type": gap("na", see=[1]),
        "species": {"text": "Mus musculus", "term": "NCBITaxon:10090", "quoted": [2], "p": 0.99},
        "sex": {"unknown": "No record gives it.", "see": [1], "p": None},
        "age": quoted("2 months", value=2, unit="month", level="young"),
        "development_stage": gap("unknown"),
        "tissue": quoted("liver", term="UBERON:0002107"),
        "cell_type": gap("na"),
        "cell_line": gap("na"),
        "disease": {
            "text": "healthy",
            "term": "PATO:0000461",
            "inferred": "No disease is named.",
            "p": 0.8,
        },
        "genotype": quoted("wild type", level="wt"),
        "strain": gap("unknown"),
        "treatment": quoted("bleomycin and DMSO", term=["CHEBI:22907", "CHEBI:28262"]),
        "dose": quoted("per side"),
        "duration": quoted("overnight"),
        "diet": {"text": "standard chow", "value": 5, "unit": "% fat", "quoted": [4], "p": 0.9},
    },
    "samples": {
        "GSM1": material("B1"),
        "GSM2": material("B2"),
        "GSM3": material("B3", age=quoted("12 months", value=12, unit="month", level="middle")),
        "GSM4": material(
            "B4", age=quoted("20 to 24 months", value=[20, 24], level="old", against=[4])
        ),
        "GSM5": material(
            "B1", target=gap("na"), control_type={"text": "input", "quoted": [3], "p": 0.9}
        ),
        "GSM6": material("B5", genotype=quoted("knockout", level="ko")),
        "GSM7": material("B1", control_type=gap("unknown")),
        "GSM8": material(
            "B3",
            target=gap("na"),
            control_type=quoted("input"),
            age=quoted("12 months", value=12, unit="month", level="middle"),
        ),
        "GSM9": material(
            "B4",
            target=gap("na"),
            control_type=quoted("IgG"),
            age=quoted("20 to 24 months", value=[20, 24], level="old"),
        ),
    },
}


#: The four GEO Samples of the joined study, each with the BioSample accession its record links: a
#: young, a middle and an old wild-type library, and a young knockout.
JOINED = {"GSM1": "SAMN1", "GSM3": "SAMN3", "GSM4": "SAMN4", "GSM6": "SAMN6"}


def joined_study(folder: Path, change: Any = None) -> Path:
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


def compiled(workspace: Path, base: DatasetManifest, accessions: list[str]) -> None:
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
