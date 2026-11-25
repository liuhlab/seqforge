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
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from labdata.ontology import SnapshotData, SourceTerm, build_tiny_store
from labdata.store.model import Link, Record
from labllm import Rejected
from labllm.testing import FakeGateway, Price
from typer.testing import CliRunner

from seqforge.cli import app
from seqforge.passes.kinds import KINDS
from seqforge.study import reader
from seqforge.study.lists import guide_text
from seqforge.study.ontology_search import StoreSearch
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
}
#: The one paper: an abstract, and a full text whose white space differs from the writer's quote.
ARTICLE = (
    b"<article><body><sec><title>Methods</title><p>We used male\n   C57BL/6J mice.</p></sec>"
    b"</body></article>"
)
ABSTRACT = "Livers of young and old mice. The old mice were 22 months of age."


class Store:
    """liulab-data's record store, as the reader reads it: Series, their sample links, samples."""

    settings: ClassVar[dict[str, str]] = {}

    def open_many(self, accessions: Iterable[str]) -> dict[str, Record]:
        day = dt.date(2026, 1, 1)
        out = {}
        for a in accessions:
            if a in SERIES:
                fields, links = (
                    SERIES[a],
                    tuple(Link("sample", s, "SOFT", True) for s in SAMPLES[a]),
                )
            else:
                fields = next(s[a] for s in SAMPLES.values() if a in s)
                links = ()
            pairs = tuple(
                (name, v)
                for name, vs in fields.items()
                for v in ([vs] if isinstance(vs, str) else vs)
            )
            out[a] = Record(a, "GEO", None, day, day, day, pairs, "", day, links)
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
        info = dataclasses.make_dataclass("Info", ["title", "abstract"])("Aging liver", ABSTRACT)
        paper = dataclasses.make_dataclass("Paper", ["info", "files"])
        return {p: paper(info, (_File("xml", Path("article.xml")),)) for p in pmids}


#: The day the pass opens the ontology store, which pins its snapshots for the whole pass.
OPENED = dt.date(2026, 11, 21)
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
}


def _writer(form: dict[str, Any]) -> str:
    # The gateway's rule, which its fake does not keep: a DeepSeek schema is a strict function
    # call, and a strict function call with thinking on is refused before any paid call.
    if form["provider"] == "deepseek" and form["settings"]["thinking"] and form.get("schema"):
        raise Rejected("DeepSeek strict function calls need thinking off")
    user = form["messages"][-1]["content"]
    study = re.search(r"=== GEO Series (GSE\d+) ===", user)
    assert study is not None
    key = study.group(1)
    if key == "GSE30" or (key == "GSE40" and "could not be used" not in user):
        return "{the writer's answer, cut off"
    if key == "GSE10":
        return f"Here are the lists {{as asked}}:\n```json\n{json.dumps(LISTS[key])}\n```\nDone."
    return json.dumps(LISTS[key])


def _noul(name: str, question: dict[str, Any]) -> float:
    if name.endswith("_varied_negated"):
        # Jev says yes to both askings about the GSE50 treatment: the two disagree.
        return 0.8 if "treatment" in question["instructions"] else 0.1
    if name.endswith("_ordered"):
        return 0.9 if " age " in question["instructions"] else 0.1
    return 0.9


def _choose(name: str, question: dict[str, Any], sample: str | None) -> tuple[str, float]:
    options: dict[str, str] = question["criteria"]
    if name.endswith(("_baseline", "_baseline_reversed")):
        return next(k for k, v in options.items() if v in ("young", "vehicle")), 0.9
    if name.startswith("gap_"):
        return "na", 0.9
    if name == "donor":
        return "one_each", 0.8
    if name.startswith("o"):
        return "c0", 0.95 if options["c0"].startswith(("UBERON", "WBbt")) else 0.6
    assert sample is not None
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
            out[name] = {"noul": _noul(name, question)}
        else:
            choice, p = _choose(name, question, sample)
            out[name] = {"choice": choice, "probabilities": {choice: p}}
    return out


@pytest.fixture(scope="module")
def read(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Path, dict[str, Any], FakeGateway]]:
    """One pass over five studies, written beside the set's own files."""
    here = tmp_path_factory.mktemp("reader")
    set_dir = here / "set"
    for key in SERIES:
        (set_dir / key).mkdir(parents=True)
        papers = ["pmid:100"] if key == "GSE10" else []
        bounds = {"study": key, "series": [{"id": key}], "papers": papers}
        (set_dir / key / "bounds.yaml").write_text(yaml.safe_dump(bounds))
    (set_dir / "GSE10" / "reading.yaml").write_text("the standard set's own\n")
    ontologies = build_tiny_store(here / "ontologies", VOCABULARIES)
    opened = []

    def build(store: Any) -> Any:
        opened.append(StoreSearch(OPENED))
        return reader.reading_kind(Store(), Papers(), set_dir=set_dir, search=opened[-1])

    kind = dataclasses.replace(reader.KIND, build=build, reads_store=False)
    fake = FakeGateway(
        lambda form: _writer(form) if form["route"] == "chat" else _jev(form),
        prices={reader.JEV.name: Price(0.0, 0.0), reader.WRITER.name: Price(0.0, 0.0)},
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setitem(KINDS, kind.name, kind)
        monkeypatch.setattr(
            "seqforge.passes.gateway.make_gateway", lambda: fake.client(repo="seqforge")
        )
        monkeypatch.delenv("SEQFORGE_REVIEWS", raising=False)
        monkeypatch.setenv("LABDATA_ONTOLOGIES", str(ontologies))
        monkeypatch.chdir(here)
        clone = ["--reviews", str(here / "clone")]
        (here / "clone" / "reviews").mkdir(parents=True)
        (here / "keys.txt").write_text("\n".join(SERIES) + "\n")
        started = runner.invoke(
            app, ["pass", "start", kind.name, "--ceiling-usd", "1", "--keys", "keys.txt", *clone]
        )
        assert started.exit_code == 0, started.output
        write = [
            "study",
            "write-reading",
            "--out",
            str(set_dir),
            "--name",
            "job-reading.yaml",
            *clone,
        ]
        written = runner.invoke(app, write)
        assert written.exit_code == 0, written.output
        yield set_dir, json.loads(written.stdout)["studies"], fake
    for search in opened:
        search.close()


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
        "date": OPENED,
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
        "date": OPENED,
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


def test_the_reading_pass_refuses_to_start_without_the_ontology_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ontology store's variable names a folder that is no store: the pass exits 2 naming the
    variable, rather than read with no search."""
    import labdata

    fake = FakeGateway(lambda form: "{}", prices={})
    # The module's pass holds its own build of the kind until the module ends: run the real one.
    monkeypatch.setitem(KINDS, reader.KIND_NAME, reader.KIND)
    monkeypatch.setattr(labdata, "open_store", lambda root=None: Store())
    monkeypatch.setattr("seqforge.passes.gateway.make_gateway", lambda: fake.client(repo="s"))
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("LABDATA_ONTOLOGIES", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "clone" / "reviews").mkdir(parents=True)
    (tmp_path / "keys.txt").write_text("GSE10\n")

    started = runner.invoke(
        app,
        [
            *("pass", "start", reader.KIND_NAME, "--ceiling-usd", "1", "--keys", "keys.txt"),
            *("--reviews", str(tmp_path / "clone")),
        ],
    )

    assert started.exit_code == 2, started.output
    assert "LABDATA_ONTOLOGIES" in started.stderr
    assert fake.record == []


def test_the_pass_pins_its_snapshots_on_the_utc_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store's snapshots are named by UTC day, so the day a reading says they were pinned is
    the UTC day too, wherever the pass runs: here in a zone whose own date is another day."""
    utc = dt.datetime.now(dt.UTC)
    # Twelve hours behind UTC is yesterday before noon UTC; fourteen ahead is tomorrow after it.
    zone = "Etc/GMT+12" if utc.hour < 12 else "Etc/GMT-14"
    built: list[Any] = []
    monkeypatch.setattr(reader, "paper_reader", lambda: None)
    monkeypatch.setattr(reader, "reading_kind", lambda *a, search, **k: built.append(search))
    monkeypatch.setenv("LABDATA_ONTOLOGIES", str(build_tiny_store(tmp_path, VOCABULARIES)))
    try:
        with pytest.MonkeyPatch.context() as clock:
            clock.setenv("TZ", zone)
            time.tzset()
            assert dt.date.today() != utc.date()
            reader.KIND.build(None)
    finally:
        time.tzset()

    built[0].close()
    assert built[0].date == utc.date()
