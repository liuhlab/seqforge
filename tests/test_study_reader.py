"""The reading job: a pass of ``study-reading`` over a store, then ``seqforge study write-reading``.

The writer and Jev are played by liulab-llms's fake gateway, liulab-data's stores by a few records
held in memory, and its ontology store by a tiny one built with liulab-data's own writer. No test
reads a real store, calls a model or searches a real ontology.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
import re
import time
import types
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from labdata.ontology import SnapshotData, SourceTerm, build_tiny_store
from labdata.store.model import Link, Record
from labllm import Task
from labllm.testing import FakeGateway, Price
from typer.testing import CliRunner

import gateway_kit
from seqforge.cli import app
from seqforge.passes.status import read_only_kind
from seqforge.study import bounding, closed, reader
from seqforge.study.lists import guide_text
from seqforge.study.reading import check_reading, read_reading
from seqforge.study.relations import relations

runner = CliRunner()
#: The module shares one pass, so its tests run on one worker.
pytestmark = pytest.mark.xdist_group("study-reader")

_MOUSE = {"Sample_organism_ch1": "Mus musculus", "Sample_taxid_ch1": "10090", "Sample_type": "SRA"}


def _sample(title: str, source: str, strategy: str, *chars: str) -> dict[str, Any]:
    return {
        **_MOUSE,
        "Sample_title": title,
        "Sample_source_name_ch1": source,
        "Sample_library_strategy": strategy,
        "Sample_characteristics_ch1": list(chars),
    }


#: Every Series and sample record of the store, by accession, each field with its values.
SERIES = {
    "GSE10": {
        "Series_title": "Liver of young and old mice",
        "Series_overall_design": "RNA-seq of young and old mouse livers",
    },
    "GSE20": {
        "Series_title": "H3K4me3 in liver",
        "Series_overall_design": "ChIP-seq of H3K4me3 and input",
    },
    "GSE30": {"Series_title": "A study the writer cannot list"},
    "GSE40": {"Series_title": "Brain of vehicle and drug mice"},
    "GSE50": {
        "Series_title": "Brain of vehicle and drug mice",
        "Series_overall_design": "Drug or vehicle",
    },
    "GSE60": {"Series_title": "Liver of 26 mouse strains"},
    "GSE70": {"Series_title": "Intestine of a mouse and a worm"},
    "GSE80": {"Series_title": "Liver of wild type and knockout mice"},
    "GSE90": {"Series_title": "Liver of fed and fasted mice"},
}
SAMPLES = {
    "GSE10": {
        "GSM11": _sample("liver young 1", "liver", "RNA-Seq", "age: 2 months"),
        "GSM12": _sample("liver young 2", "liver", "RNA-Seq", "age: 2 months"),
        "GSM13": _sample("liver old 1", "liver", "RNA-Seq", "age: 24 months"),
        "GSM14": {**_sample("liver array", "liver", "", "age: 2 months"), "Sample_type": "RNA"},
    },
    "GSE20": {
        "GSM21": _sample("input liver rep1", "liver", "ChIP-Seq", "antibody: none (input)"),
        "GSM22": _sample("H3K4me3 liver rep1", "liver", "ChIP-Seq", "antibody: H3K4me3"),
        "GSM23": _sample("input liver rep2", "liver", "ChIP-Seq", "antibody: none (input)"),
        "GSM24": _sample("H3K4me3 liver rep2", "liver", "ChIP-Seq", "antibody: H3K4me3"),
    },
    "GSE30": {"GSM31": _sample("brain 1", "brain", "RNA-Seq")},
    "GSE40": {
        "GSM41": _sample("brain 1", "brain", "RNA-Seq"),
        "GSM42": _sample("brain 2", "brain", "RNA-Seq"),
    },
    "GSE50": {
        "GSM51": _sample("brain vehicle", "brain", "RNA-Seq", "treatment: vehicle"),
        "GSM52": _sample("brain drug", "brain", "RNA-Seq", "treatment: drug"),
    },
    "GSE60": {"GSM61": _sample("liver", "liver", "RNA-Seq")},
    "GSE70": {
        "GSM71": _sample("mouse gut", "intestine", "RNA-Seq"),
        "GSM72": {
            **_sample("worm gut", "intestine", "RNA-Seq"),
            # No organism written: the species is named from the taxonomy id alone.
            "Sample_organism_ch1": [],
            "Sample_taxid_ch1": "6239",
        },
    },
    "GSE80": {
        "GSM81": _sample("liver wild type", "liver", "RNA-Seq", "genotype: wild type"),
        "GSM82": _sample("liver knockout", "liver", "RNA-Seq", "genotype: knockout"),
    },
    "GSE90": {
        "GSM91": _sample("liver fed", "liver", "RNA-Seq", "diet: fed"),
        "GSM92": _sample("liver fasted 1", "liver", "RNA-Seq", "diet: fasted"),
        "GSM93": _sample("liver fasted 2", "liver", "RNA-Seq", "diet: fasted"),
    },
}
#: The one paper: an abstract, and a full text whose white space differs from the writer's quote.
ARTICLE = (
    b"<article><body><sec><title>Methods</title><p>We used male\n   C57BL/6J mice.</p></sec>"
    b"</body></article>"
)
ABSTRACT = "Livers of young and old mice. The old mice were 22 months of age."
#: The papers each Series links: GSE10 links the one paper.
PAPER_OF = {"GSE10": ["PMID:100"]}


class Store:
    """liulab-data's record store, as the reader reads it: Series, their sample links, samples."""

    settings: ClassVar[dict[str, str]] = {}

    def links(self, accessions: Iterable[str], relation: str | None = None) -> dict[str, Any]:
        """Each record's links, as the bounding job follows them from either end."""
        return {
            a: [k for k in self._links(a) if relation in (None, k.relation)] for a in accessions
        }

    def _links(self, accession: str) -> list[Link]:
        if accession in SERIES:
            paper = [Link("PubMed", p, "SOFT", True) for p in PAPER_OF.get(accession, [])]
            return [*(Link("sample", s, "SOFT", True) for s in SAMPLES[accession]), *paper]
        return [
            Link("PubMed of", s, "SOFT", False) for s, ps in PAPER_OF.items() if accession in ps
        ]

    def open_many(self, accessions: Iterable[str]) -> dict[str, Record]:
        day = dt.date(2026, 1, 1)
        out = {}
        for a in accessions:
            fields = SERIES[a] if a in SERIES else next(s[a] for s in SAMPLES.values() if a in s)
            pairs = tuple(
                (name, v)
                for name, vs in fields.items()
                for v in ([vs] if isinstance(vs, str) else vs)
            )
            out[a] = Record(a, "GEO", None, day, day, day, pairs, "", day, tuple(self._links(a)))
        return out


@dataclasses.dataclass(frozen=True)
class _File:
    kind: str
    path: Path | None

    def open(self) -> io.BytesIO:
        return io.BytesIO(ARTICLE)


class Papers:
    """liulab-data's paper reader: one paper, with its abstract and its article XML."""

    def papers(self, pmids: Iterable[str]) -> dict[str, Any]:
        made = dataclasses.make_dataclass("Info", ["title", "abstract", "published"])
        info = made("Aging liver", ABSTRACT, None)
        paper = dataclasses.make_dataclass("Paper", ["info", "files"])
        return {p: paper(info, (_File("xml", Path("article.xml")),)) for p in pmids}


#: The UTC day the module's pass started, the day its ontology search pins the store's snapshots.
PINNED_ON: list[dt.date] = []


def _stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """liulab-data's record store and paper store, as the kind opens them, played by the two above."""
    import labdata
    import labdata.papers

    monkeypatch.setattr(labdata, "open_store", lambda root=None: Store())
    monkeypatch.setattr(
        labdata.papers, "open_paper_store", lambda root=None: types.SimpleNamespace(reader=Papers())
    )


#: The ontology store: a few terms of each vocabulary the studies need. ChEBI is not held.
VOCABULARIES = [
    SnapshotData(
        "uberon",
        "2026-11-05",
        [
            SourceTerm("UBERON:0002107", "liver"),
            SourceTerm("UBERON:0000160", "intestine"),
        ],
    ),
    # One text, two species: a mouse's intestine is UBERON's, a worm's is WBbt's.
    SnapshotData("wbbt", "2025-08-19", [SourceTerm("WBbt:0005772", "intestine")]),
    # The mouse's adult stage is named neither "adult" nor "adult stage".
    SnapshotData("mmusdv", "2025-01-23", [SourceTerm("MmusDv:0000110", "mature stage")]),
    SnapshotData("efo", "3.96.0", [SourceTerm("EFO:0008896", "RNA-seq")]),
    SnapshotData("pato", "2026-10-01", [SourceTerm("PATO:0000461", "normal")]),
    SnapshotData(
        "ncbitaxon",
        "2026-11-22",
        [
            SourceTerm("NCBITaxon:10090", "Mus musculus"),
            SourceTerm("NCBITaxon:6239", "Caenorhabditis elegans"),
        ],
        searchable=False,
    ),
]


def _q(source: str, field: str, text: str) -> dict[str, str]:
    return {"source": source, "field": field, "text": text}


def _values(*values: dict[str, Any]) -> dict[str, Any]:
    return {"values": list(values), "gap": None}


#: What the writer answers for each study.
LISTS: dict[str, dict[str, Any]] = {
    "GSE10": {
        "factors": [
            {
                "label": "age",
                "levels": [
                    {
                        "name": "young",
                        "text": "2 months",
                        "value": 2,
                        "unit": "month",
                        "quote": _q("GSM11", "Sample_characteristics_ch1", "age: 2 months"),
                    },
                    # No sample is at this level, so code drops it.
                    {"name": "middle", "text": "12 months", "value": 12, "unit": "month"},
                    {
                        "name": "old",
                        "text": "24 months",
                        "value": 24,
                        "unit": "month",
                        "quote": _q("GSM13", "Sample_characteristics_ch1", "age: 24 months"),
                        "against": [
                            _q("pmid:100", "abstract", "The old mice were 22 months of age.")
                        ],
                    },
                ],
                "baseline": "young",
                "ordered": True,
                "quote": _q(
                    "GSE10", "Series_overall_design", "RNA-seq of young and old mouse livers"
                ),
            }
        ],
        "labels": {
            "assay": _values(
                {
                    "text": "RNA-Seq",
                    "search": "RNA-seq",
                    "quote": _q("each_sample", "Sample_library_strategy", "RNA-Seq"),
                }
            ),
            "tissue": _values(
                {
                    "text": "liver",
                    "search": "liver",
                    "quote": _q("GSM11", "Sample_source_name_ch1", "liver"),
                }
            ),
            "disease": _values({"text": "healthy", "how": "no disease is named"}),
            "sex": _values(
                {"text": "male", "quote": _q("pmid:100", "Methods", "male C57BL/6J mice")}
            ),
            # A quote the record does not hold: not found, so the claim is inferred.
            "strain": _values(
                {
                    "text": "C57BL/6J",
                    "how": "the paper's mice",
                    "quote": _q("GSE10", "Series_overall_design", "C57BL/6J mice were used"),
                }
            ),
        },
    },
    "GSE20": {
        # The writer makes the target a factor; it is a label, never a factor.
        "factors": [
            {
                "label": "target",
                "levels": [
                    {
                        "name": "H3K4me3",
                        "text": "H3K4me3",
                        "quote": _q("GSM22", "Sample_characteristics_ch1", "antibody: H3K4me3"),
                    }
                ],
            }
        ],
        "labels": {
            "assay": _values(
                {
                    "text": "ChIP-Seq",
                    "quote": _q("each_sample", "Sample_library_strategy", "ChIP-Seq"),
                }
            ),
            "control_type": _values(
                {
                    "text": "input",
                    "quote": _q("GSM21", "Sample_characteristics_ch1", "antibody: none (input)"),
                }
            ),
            "tissue": _values(
                {
                    "text": "liver",
                    "search": "liver",
                    "quote": _q("GSM21", "Sample_source_name_ch1", "liver"),
                }
            ),
        },
        "ties": [
            {
                "control": "GSM21",
                "pulldowns": ["GSM22"],
                "quote": _q("GSM21", "Sample_title", "liver rep1"),
            },
            {
                "control": "GSM23",
                "pulldowns": ["GSM24"],
                "quote": _q("GSM23", "Sample_title", "liver rep2"),
            },
        ],
    },
    "GSE40": {"factors": [], "labels": {"tissue": _values({"text": "brain"})}},
    "GSE50": {
        "factors": [
            {
                "label": "treatment",
                "baseline": "vehicle",
                "levels": [
                    {
                        "name": "vehicle",
                        "text": "vehicle",
                        "quote": _q("GSM51", "Sample_characteristics_ch1", "treatment: vehicle"),
                    },
                    {
                        "name": "drug",
                        "text": "drug",
                        "quote": _q("GSM52", "Sample_characteristics_ch1", "treatment: drug"),
                    },
                ],
            }
        ],
        "labels": {"tissue": _values({"text": "brain"})},
    },
    # One more strain than Jev chooses among.
    "GSE60": {
        "factors": [],
        "labels": {"strain": _values(*({"text": f"strain {n}"} for n in range(26)))},
    },
    "GSE70": {
        "factors": [],
        "labels": {
            "tissue": _values({"text": "intestine"}),
            "development_stage": _values({"text": "adult"}),
        },
    },
    "GSE80": {
        "factors": [
            {
                "label": "genotype",
                "baseline": "wild type",
                "levels": [
                    {"name": "wild type", "text": "wild type"},
                    {"name": "knockout", "text": "knockout"},
                ],
            }
        ],
        "labels": {"tissue": _values({"text": "liver"})},
    },
    "GSE90": {
        "factors": [
            {
                "label": "diet",
                "baseline": "fed",
                "levels": [{"name": "fed", "text": "fed"}, {"name": "fasted", "text": "fasted"}],
            }
        ],
        "labels": {"tissue": _values({"text": "liver"})},
    },
}

#: Each sample's value texts, as Jev picks them; a label it has none of is na.
_YOUNG = ("2 months", "RNA-Seq", "liver", "healthy", "male", "C57BL/6J")
TRUTH = {
    "GSM11": frozenset(_YOUNG),
    "GSM12": frozenset(_YOUNG),
    "GSM13": frozenset({"24 months", *_YOUNG[1:]}),
    "GSM21": frozenset({"ChIP-Seq", "input", "liver"}),
    "GSM23": frozenset({"ChIP-Seq", "input", "liver"}),
    "GSM22": frozenset({"ChIP-Seq", "H3K4me3", "liver"}),
    "GSM24": frozenset({"ChIP-Seq", "H3K4me3", "liver"}),
    "GSM41": frozenset({"brain"}),
    "GSM42": frozenset({"brain"}),
    "GSM51": frozenset({"vehicle", "brain"}),
    "GSM52": frozenset({"drug", "brain"}),
    "GSM61": frozenset({"strain 0"}),
    "GSM71": frozenset({"intestine", "adult"}),
    "GSM72": frozenset({"intestine"}),
    "GSM81": frozenset({"wild type", "liver"}),
    "GSM82": frozenset({"knockout", "liver"}),
    "GSM91": frozenset({"fed", "liver"}),
    "GSM92": frozenset({"fasted", "liver"}),
    "GSM93": frozenset({"fasted", "liver"}),
}
#: Jev's p(yes) that the study varied a factor on purpose, asked plain and then negated, by the
#: factor's label; any other factor gets 0.9 and 0.1. Jev says yes to both askings about the GSE50
#: treatment, and exactly 0.5 to the plain asking about the GSE80 genotype.
VARIED = {"treatment": (0.9, 0.8), "genotype": (0.5, 0.1)}
#: The level of each factor Jev calls a study's reference.
BASELINES = ("young", "vehicle", "wild type", "fed")
#: The samples whose level Jev leaves unanswered, in both askings, and whose tissue.
NO_LEVEL, NO_TISSUE = ("GSM93",), ("GSM92",)


def _writer(form: dict[str, Any]) -> str:
    user = form["messages"][-1]["content"]
    study = re.search(r"=== GEO Series (GSE\d+) ===", user)
    assert study is not None
    key = study.group(1)
    if key == "GSE30" or (key == "GSE40" and "could not be used" not in user):
        return "{the writer's answer, cut off"
    if key == "GSE10":
        return f"Here are the lists {{as asked}}:\n```json\n{json.dumps(LISTS[key])}\n```\nDone."
    return json.dumps(LISTS[key])


def _p_yes(asked: str) -> float | None:
    """Jev's p(yes), by what the question is about; ``None`` leaves it out."""
    if varied := re.match(r"Did the study vary (\w+) on purpose", asked):
        return VARIED.get(varied.group(1), (0.9, 0.1))[0]
    if negated := re.match(r"Is (\w+) \(values: .* only a description", asked, re.DOTALL):
        return VARIED.get(negated.group(1), (0.9, 0.1))[1]
    if ordered := re.match(r"Do the levels of (\w+) ", asked):
        return {"age": 0.9, "diet": None}.get(ordered.group(1), 0.1)
    return 0.9


def _choose(asked: str, options: dict[str, str], sample: str | None) -> tuple[str, float] | None:
    """Jev's pick and its probability, by what the question is about; ``None`` leaves it out."""
    if "is the reference" in asked:
        return next(k for k, v in options.items() if v in BASELINES), 0.9
    if " what about their " in asked:
        return "na", 0.9
    if asked.startswith("Where did each"):
        return "one_each", 0.8
    if asked.startswith("Which ontology term"):
        first = next(iter(options))
        return first, 0.95 if options[first].startswith(("UBERON", "WBbt")) else 0.6
    assert sample is not None
    if sample in NO_LEVEL and re.match(r"Which \w+ does `sample` have\?", asked):
        return None
    if sample in NO_TISSUE and asked.startswith("What is the tissue of `sample`"):
        return None
    for k, text in options.items():
        if any(text.endswith((f"= {v}", f" is {v}")) for v in TRUTH[sample]):
            return k, 0.9
    return "na", 0.85


def _jev(form: dict[str, Any]) -> dict[str, Any]:
    named = re.match(r"`sample` is (GSM\d+)\.", form["description"])
    sample = named.group(1) if named else None
    out: dict[str, Any] = {}
    for name, question in form["questions"].items():
        if question["type"] == "noul":
            if (p := _p_yes(question["instructions"])) is not None:
                out[name] = {"noul": p}
        elif picked := _choose(question["instructions"], question["criteria"], sample):
            out[name] = {"choice": picked[0], "probabilities": {picked[0]: picked[1]}}
    return out


def _serve(monkeypatch: pytest.MonkeyPatch, here: Path) -> FakeGateway:
    """The stores, the ontology store and the fake gateway behind the commands, run from ``here``."""
    _stores(monkeypatch)
    fake = gateway_kit.install(
        monkeypatch,
        lambda form: _writer(form) if form["route"] == "chat" else _jev(form),
        prices={closed.JEV.name: Price(0.0, 0.0), reader.WRITER.name: Price(0.0, 0.0)},
    )
    ontologies = build_tiny_store(here / "ontologies", VOCABULARIES)
    monkeypatch.setenv("LABDATA_ONTOLOGIES", str(ontologies))
    monkeypatch.chdir(here)
    return fake


def _bounds(key: str) -> str:
    """A bounds file the format check accepts: the one Series, and the papers it links."""
    bounds = {
        "format": 1,
        "study": key,
        "models": {"bounds": None, "probabilities": None},
        "series": [{"id": key, "inferred": "the Series the study was picked by", "p": None}],
        "papers": [p.lower() for p in PAPER_OF.get(key, [])],
        "left_out": [],
        "reason": "The study is its one Series.",
        "passages": {},
    }
    return yaml.safe_dump(bounds)


@pytest.fixture(scope="module")
def read(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Path, dict[str, Any], FakeGateway]]:
    """One pass over every study of the store, written beside the folder's own files."""
    here = tmp_path_factory.mktemp("reader")
    set_dir = here / "set"
    for key in SERIES:
        (set_dir / key).mkdir(parents=True)
        (set_dir / key / "bounds.yaml").write_text(_bounds(key))
    (set_dir / "GSE10" / "reading.yaml").write_text("the standard set's own\n")
    with pytest.MonkeyPatch.context() as monkeypatch:
        fake = _serve(monkeypatch, here)
        (here / "keys.txt").write_text("\n".join(SERIES) + "\n")
        PINNED_ON.append(dt.datetime.now(dt.UTC).date())
        started = runner.invoke(
            app,
            [
                *("pass", "start", reader.KIND_NAME, "--ceiling-usd", "1", "--keys", "keys.txt"),
                *("--studies", str(set_dir)),
            ],
        )
        assert started.exit_code == 0, started.output
        write = [
            "study",
            "write-reading",
            "--out",
            str(set_dir),
            "--name",
            "job-reading.yaml",
        ]
        written = runner.invoke(app, write)
        assert written.exit_code == 0, written.output
        yield set_dir, json.loads(written.stdout)["studies"], fake


def _reading(set_dir: Path, key: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((set_dir / key / "job-reading.yaml").read_text())
    assert check_reading(data) == []
    return data


def _sent(fake: FakeGateway) -> list[dict[str, Any]]:
    """Every request the fake gateway answered, as its form."""
    return [e.form for e in fake.record if e.form is not None]


def _first_id(text: str) -> str:
    found = re.search(r"GS[EM]\d+", text)
    assert found is not None
    return found.group(0)


def _claim(reading: dict[str, Any], sample: str, label: str) -> dict[str, Any]:
    claim: dict[str, Any] = {**reading["shared"], **reading["samples"][sample]}[label]
    return claim


def test_a_study_is_read_into_a_reading_that_passes_the_check_beside_the_sets_own(
    read: tuple[Path, dict[str, Any], FakeGateway],
) -> None:
    """The writer lists, Jev picks, code writes a file the format check accepts, beside the set's
    own reading and never over it. Only sequenced samples get a row; a factor level no sample is at
    is dropped; the models are named as the pass ran them."""
    set_dir, said, _ = read
    reading = _reading(set_dir, "GSE10")

    assert said["GSE10"] == {"ending": "settled", "file": "written"}
    assert (set_dir / "GSE10" / "reading.yaml").read_text() == "the standard set's own\n"
    assert list(reading["samples"]) == ["GSM11", "GSM12", "GSM13"]
    age = reading["factors"]["age"]
    assert (age["levels"], age["baseline"], age["ordered"]) == (["young", "old"], "young", True)
    assert [_claim(reading, g, "age")["level"] for g in ("GSM11", "GSM12", "GSM13")] == [
        "young",
        "young",
        "old",
    ]
    assert reading["models"] == {
        "reading": "deepseek/deepseek-flash",
        "probabilities": "typesafe/jev-1.14.0",
    }
    assert reading["read"] == ["records", "abstract", "full_text"]


def test_a_claim_is_quoted_only_from_a_passage_code_found_and_terms_come_from_a_clear_pick(
    read: tuple[Path, dict[str, Any], FakeGateway],
) -> None:
    """A record quote is found character for character, a paper quote once white space is one
    space; a quote its source does not hold leaves the claim inferred, citing nothing. A passage
    Jev judges to speak against a claim is listed under against. Species is the record's taxonomy
    id, healthy its PATO id, and a searched term is written only for a pick Jev is sure of. The
    file names the snapshot of each vocabulary its ids came from, and the day the store was opened;
    a vocabulary searched for a term not written is not named."""
    set_dir, _, _ = read
    reading = _reading(set_dir, "GSE10")
    passages = reading["passages"]
    cited = {
        label: _claim(reading, "GSM13", label)
        for label in ("age", "sex", "strain", "species", "disease", "tissue", "assay")
    }

    assert passages[cited["age"]["quoted"][0]]["record"] == "GSM13"
    assert passages[cited["age"]["against"][0]] == {
        "paper": "pmid:100",
        "section": "abstract",
        "quote": "The old mice were 22 months of age.",
    }
    assert passages[cited["sex"]["quoted"][0]] == {
        "paper": "pmid:100",
        "section": "Methods",
        "quote": "male C57BL/6J mice",
    }
    assert cited["strain"]["inferred"] == "the paper's mice"
    assert "see" not in cited["strain"]
    assert not any("C57BL/6J mice were used" in p.get("quote", "") for p in passages.values())
    assert cited["species"]["term"] == "NCBITaxon:10090"
    assert passages[cited["species"]["quoted"][0]]["record"] == "each_sample"
    assert cited["disease"]["term"] == "PATO:0000461"
    assert cited["tissue"]["term"] == "UBERON:0002107"
    assert "term" not in cited["assay"]
    assert reading["ontology_snapshot"] == {
        "source": "liulab-data ontology store: ncbitaxon-2026-11-22, pato-2026-10-01, "
        "uberon-2026-11-05",
        "date": PINNED_ON[0],
    }


def test_a_study_whose_two_askings_disagree_is_unsettled_and_still_written(
    read: tuple[Path, dict[str, Any], FakeGateway],
) -> None:
    """Jev says yes both to whether GSE50 varied its treatment on purpose and to the negated
    question. The study is unsettled, and its reading is written. Its treatment's vocabulary,
    ChEBI, is not in the store, so its only ids are the species', and the snapshot names only the
    taxonomy's."""
    set_dir, said, _ = read
    reading = _reading(set_dir, "GSE50")

    assert said["GSE50"]["ending"] == "unsettled"
    assert "factor treatment: varied on purpose" in said["GSE50"]["checks_failed"][0]
    assert reading["factors"]["treatment"]["baseline"] == "vehicle"
    assert reading["ontology_snapshot"] == {
        "source": "liulab-data ontology store: ncbitaxon-2026-11-22",
        "date": PINNED_ON[0],
    }


def test_a_probability_of_exactly_one_half_is_neither_yes_nor_no_and_leaves_the_study_unsettled(
    read: tuple[Path, dict[str, Any], FakeGateway],
) -> None:
    """Jev's p(yes) that GSE80 varied its genotype on purpose is exactly 0.5, and its p(yes) to
    the negated question is 0.1. At 0.5 Jev leans neither way, so the two askings do not agree:
    the study is unsettled. The genotype is not kept as a factor, so it stays a label."""
    set_dir, said, _ = read
    reading = _reading(set_dir, "GSE80")

    assert said["GSE80"]["ending"] == "unsettled"
    assert "factor genotype: varied on purpose" in said["GSE80"]["checks_failed"][0]
    assert reading["factors"] == {}
    assert _claim(reading, "GSM82", "genotype")["text"] == "knockout"


def test_a_question_jev_leaves_unanswered_is_an_unknown_claim_and_leaves_the_study_unsettled(
    read: tuple[Path, dict[str, Any], FakeGateway],
) -> None:
    """Jev leaves GSM93's diet unanswered, in both askings. Its claim is unknown with no
    probability, and two missing answers do not agree: the study is unsettled, naming the sample.
    GSM92's tissue and whether the diet's levels are ordered, each asked once and left unanswered,
    are named too."""
    set_dir, said, _ = read
    reading = _reading(set_dir, "GSE90")

    assert said["GSE90"]["ending"] == "unsettled"
    assert "GSM93: level of diet" in said["GSE90"]["checks_failed"][0]
    left = said["GSE90"]["checks_failed"][1]
    assert left == "all_answered: Jev left unanswered factor diet: ordered; GSM92: value of tissue"
    assert reading["samples"]["GSM93"]["diet"] == {
        "unknown": "what was read does not say its diet",
        "p": None,
    }


def test_a_broken_writer_answer_is_asked_for_once_more_and_a_second_refuses(
    read: tuple[Path, dict[str, Any], FakeGateway],
) -> None:
    """GSE40's first answer is cut off and its second is whole: it is read. GSE30's writer is cut
    off twice: the study is refused with the reason, and no file is written. The writer thinks and
    sends no schema, which the gateway refuses for DeepSeek with thinking on, so its object is
    taken out of the reply's text: GSE10's, in a code fence among prose, is read the first time."""
    set_dir, said, fake = read
    asked = [_first_id(f["messages"][-1]["content"]) for f in _sent(fake) if f["route"] == "chat"]

    assert (asked.count("GSE40"), asked.count("GSE30"), asked.count("GSE10")) == (2, 2, 1)
    assert said["GSE40"] == {"ending": "settled", "file": "written"}
    assert said["GSE30"]["ending"] == "refused"
    assert "could not be used twice" in said["GSE30"]["reason"]
    assert not (set_dir / "GSE30" / "job-reading.yaml").exists()


def test_the_pull_down_guide_goes_only_to_a_pull_down_study_and_jev_checks_each_tie(
    read: tuple[Path, dict[str, Any], FakeGateway],
) -> None:
    """GSE20's records name ChIP-Seq, so the writer and Jev get the pull-down guide; no other study
    does. The target stays a label even when the writer makes it a factor. Each control the writer
    ties to a pull-down, by words the titles share, is tied once Jev agrees: one biosample id, and
    so one signal-to-control pair."""
    set_dir, said, fake = read
    guide = guide_text("pull-down.txt")
    with_guide = {
        _first_id(f["messages"][-1]["content"] if f["route"] == "chat" else f["description"])
        for f in _sent(fake)
        if guide in (f["messages"][0]["content"] if f["route"] == "chat" else f["description"])
    }
    reading = _reading(set_dir, "GSE20")
    valid, problems = read_reading(reading)
    assert valid is not None, problems

    assert said["GSE20"]["ending"] == "settled"
    assert {s[:4] for s in with_guide} == {"GSE2", "GSM2"}
    assert reading["factors"] == {}
    assert _claim(reading, "GSM22", "target")["text"] == "H3K4me3"
    assert relations(valid)["signal_to_control"] == [
        {"biosample": "B1", "signal": "GSM22", "control": "GSM21"},
        {"biosample": "B2", "signal": "GSM24", "control": "GSM23"},
    ]


def test_a_study_listing_more_values_than_jev_chooses_among_is_refused_naming_the_label(
    read: tuple[Path, dict[str, Any], FakeGateway],
) -> None:
    """GSE60's writer lists 26 strains, one more than Jev's options hold. No value is cut from
    the options unseen: the study is refused, and the reason names the label and the count."""
    set_dir, said, _ = read

    assert said["GSE60"]["ending"] == "refused"
    assert "strain (26)" in said["GSE60"]["reason"]
    assert not (set_dir / "GSE60" / "job-reading.yaml").exists()


def test_a_term_is_looked_up_in_the_vocabulary_of_each_samples_species(
    read: tuple[Path, dict[str, Any], FakeGateway],
) -> None:
    """GSE70's mouse and worm both have the tissue "intestine". The mouse's term is UBERON's and
    the worm's is WBbt's: a term found for one species is never written for the other. The worm's
    record names no organism, so its species is named from its taxonomy id. The mouse is adult,
    and its stage is MmusDv's mature stage, the parent of its adult stages."""
    set_dir, _, _ = read
    reading = _reading(set_dir, "GSE70")

    assert _claim(reading, "GSM71", "tissue")["term"] == "UBERON:0000160"
    assert _claim(reading, "GSM72", "tissue")["term"] == "WBbt:0005772"
    assert _claim(reading, "GSM71", "development_stage")["term"] == "MmusDv:0000110"
    assert _claim(reading, "GSM72", "species") == {
        "text": "Caenorhabditis elegans",
        "term": "NCBITaxon:6239",
        "inferred": "the record's taxonomy id, named by NCBI Taxonomy",
        "p": None,
    }


def _start(kind: str, studies: Path, *more: str) -> Any:
    """``seqforge pass start <kind>`` on the folder of study folders ``studies``, with ``more``."""
    argv = ["pass", "start", kind, "--ceiling-usd", "1", "--studies", str(studies), *more]
    return runner.invoke(app, argv)


def test_both_jobs_run_on_a_folder_that_is_not_the_standard_set_and_join_through_its_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh folder holds no pick, so a pass that names no units refuses and says how to name
    them. Then the four commands run in order with the default file names, and the reading job
    reads the bounds the bounding job wrote there: the paper Jev kept, GSE10's, is read in full."""
    studies = tmp_path / "studies"
    studies.mkdir()
    _serve(monkeypatch, tmp_path)
    (tmp_path / "keys.txt").write_text("GSE10\n")

    unnamed = _start(bounding.KIND_NAME, studies)
    assert unnamed.exit_code == 2
    assert "--keys" in unnamed.stderr
    for done in (
        _start(bounding.KIND_NAME, studies, "--keys", "keys.txt"),
        runner.invoke(app, ["study", "write-bounds", "--out", str(studies)]),
        _start(reader.KIND_NAME, studies, "--keys", "keys.txt"),
        runner.invoke(app, ["study", "write-reading", "--out", str(studies)]),
    ):
        assert done.exit_code == 0, done.output
    bounds = yaml.safe_load((studies / "GSE10" / "bounds.yaml").read_text())
    reading = yaml.safe_load((studies / "GSE10" / "reading.yaml").read_text())

    assert bounds["papers"] == ["pmid:100"]
    assert check_reading(reading) == []
    assert reading["read"] == ["records", "abstract", "full_text"]


def test_a_study_whose_bounds_file_is_missing_or_fails_its_check_is_held_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GSE10's bounds file has three fields and no format number, which the format check rejects;
    GSE20 has no bounds file. Each study is held before a model is asked, and its reason names
    the check's problems or the missing file."""
    studies = tmp_path / "studies"
    (studies / "GSE10").mkdir(parents=True)
    three = {"study": "GSE10", "series": [{"id": "GSE10"}], "papers": []}
    (studies / "GSE10" / "bounds.yaml").write_text(yaml.safe_dump(three))
    fake = _serve(monkeypatch, tmp_path)
    (tmp_path / "keys.txt").write_text("GSE10\nGSE20\n")

    started = _start(reader.KIND_NAME, studies, "--keys", "keys.txt")
    assert started.exit_code == 0, started.output
    task = Path(json.loads(started.stdout)["task"])
    with Task(task, gateway=fake.client(repo="seqforge"), reviews=task / "reviews") as opened:
        kind = read_only_kind(reader.KIND_NAME)
        held = {key: opened.outcomes(kind, key)[-1] for key in ("GSE10", "GSE20")}

    assert json.loads(started.stdout)["counts"]["held"] == 2
    assert fake.record == []
    assert {outcome.ending for outcome in held.values()} == {"held"}
    assert "format: expected 1, missing" in str(held["GSE10"].reason)
    assert "no bounds file" in str(held["GSE20"].reason)


@pytest.mark.parametrize(
    ("unopened", "said"),
    [
        ("record", "liulab-data's record store"),
        ("paper", "liulab-data's paper store"),
        ("ontology", "LABDATA_ONTOLOGIES"),
    ],
)
def test_the_reading_pass_refuses_to_start_without_a_store_it_reads(
    unopened: str, said: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store the kind reads cannot be opened: no record store is set up, the paper store set up
    is not a store, or the ontology store's variable names a folder that is no store. The pass
    exits 2 naming that store, rather than read without it."""
    import labdata
    import labdata.papers
    from labdata.exceptions import NotAStoreError

    def no_paper_store(root: Any = None) -> Any:
        raise NotAStoreError(f"{tmp_path / 'empty'} is not a paper store")

    papers = types.SimpleNamespace(reader=Papers())
    fake = gateway_kit.install(monkeypatch, lambda form: "{}", prices={})
    (tmp_path / "empty").mkdir()
    if unopened != "record":  # the suite's own stub leaves no record store set up
        monkeypatch.setattr(labdata, "open_store", lambda root=None: Store())
    monkeypatch.setattr(
        labdata.papers,
        "open_paper_store",
        no_paper_store if unopened == "paper" else lambda root=None: papers,
    )
    monkeypatch.setenv("LABDATA_ONTOLOGIES", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "keys.txt").write_text("GSE10\n")

    started = runner.invoke(
        app,
        [
            *("pass", "start", reader.KIND_NAME, "--ceiling-usd", "1", "--keys", "keys.txt"),
        ],
    )

    assert started.exit_code == 2, started.output
    assert said in started.stderr
    assert fake.record == []


def test_the_ontology_store_pins_its_snapshots_on_the_utc_day_and_a_units_tag_names_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store's snapshots are named by UTC day, so the day a reading says they were pinned is
    the UTC day too, wherever the pass runs: here in a zone whose own date is another day.

    A store update changes no record yet can change a reading's terms, so a unit's inputs tag
    names every snapshot the store pinned: a review of the old reading stops holding."""
    store = build_tiny_store(tmp_path / "store", VOCABULARIES)
    utc = dt.datetime.now(dt.UTC)
    # Twelve hours behind UTC is yesterday before noon UTC; fourteen ahead is tomorrow after it.
    zone = "Etc/GMT+12" if utc.hour < 12 else "Etc/GMT-14"
    try:
        with pytest.MonkeyPatch.context() as clock:
            clock.setenv("TZ", zone)
            clock.setenv("LABDATA_ONTOLOGIES", str(store))
            time.tzset()
            assert dt.date.today() != utc.date()
            ontology = reader.Ontology()
    finally:
        time.tzset()
    ontology.close()

    assert ontology.date == utc.date()

    studies = tmp_path / "studies"
    (studies / "GSE20").mkdir(parents=True)
    (studies / "GSE20" / "bounds.yaml").write_text(_bounds("GSE20"))
    monkeypatch.setenv("LABDATA_ONTOLOGIES", str(store))

    def tag() -> str:
        pinned = reader.Ontology()
        try:
            kind = reader.reading_kind(Store(), None, folder=studies, ontology=pinned)
            return kind.load("GSE20").tag
        finally:
            pinned.close()

    before, again = tag(), tag()
    # One vocabulary gets a new release; every other snapshot, and every record, stays.
    build_tiny_store(store, [SnapshotData("efo", "3.97.0", [SourceTerm("EFO:0008896", "RNA-seq")])])
    assert before == again != tag()
