"""The bounding job: a pass of ``study-bounds`` over a store, then ``seqforge study write-bounds``.

Jev is played by liulab-llms's fake gateway, and liulab-data's store by a few links held in memory,
read from either end as the real store reads them. No test reads a real store or calls a model.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import threading
import types
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from labdata.store.model import RELATIONS, Link, Record
from labllm.testing import FakeGateway, Price
from typer.testing import CliRunner

from seqforge.cli import app
from seqforge.passes.kinds import KINDS
from seqforge.study import bounding
from seqforge.study.bounds import check_bounds

runner = CliRunner()

#: Every link of the store, as it is stored: from, to, relation, and who stated it.
LINKS = [
    # GSE10's group: one link of each kind away, and records two links away or tied by no kind.
    ("GSE19", "GSE10", "SuperSeries of", "SOFT"),
    ("GSE19", "GSE11", "SuperSeries of", "SOFT"),
    ("GSE10", "PRJNA10", "BioProject", "SOFT"),
    ("GSE12", "PRJNA10", "BioProject", "SOFT"),
    ("PRJNA90", "PRJNA10", "umbrella of", "BioProject XML"),
    ("PRJNA90", "PRJNA13", "umbrella of", "BioProject XML"),
    ("GSE13", "PRJNA13", "BioProject", "SOFT"),
    ("GSE10", "PMID:100", "PubMed", "SOFT"),
    ("GSE14", "PMID:100", "PubMed", "SOFT"),
    ("GSE14", "PMID:200", "PubMed", "SOFT"),
    ("GSE15", "PMID:200", "PubMed", "SOFT"),  # two links away, through GSE14's own paper
    ("GSE10", "GSE16", "Reanalyzed by", "SOFT"),
    ("PRJNA10", "PMID:300", "PubMed", "BioProject XML"),  # a paper only a BioProject links
    ("GSE17", "PMID:300", "PubMed", "SOFT"),
    ("GSE10", "PMID:5", "PubMed", "BioProject XML"),  # a placeholder id
    ("GSE18", "PMID:5", "PubMed", "SOFT"),
    # GSE20: one Series and two papers.
    ("GSE20", "PMID:400", "PubMed", "SOFT"),
    ("GSE20", "PMID:401", "PubMed", "SOFT"),
    # GSE30: a paper that 51 other Series link too.
    *((f"GSE{n}", "PMID:500", "PubMed", "SOFT") for n in range(30, 82)),
]

#: Jev's p(yes) for each record, to the question as written and to it reversed.
JEV = {
    "GSE11": (0.9, 0.1),
    "GSE12": (0.8, 0.2),
    "GSE13": (0.2, 0.9),
    "GSE14": (0.3, 0.7),
    "pmid:100": (0.95, 0.05),
    "pmid:200": (0.1, 0.8),
    "pmid:400": (0.9, 0.1),
    "pmid:401": (0.7, 0.6),  # yes as written, and yes reversed: the two disagree
}


class Store:
    """liulab-data's record store, as the job reads it: links from either end, and records."""

    def links(self, accessions: str | Iterable[str], relation: str | None = None) -> Any:
        wanted = [accessions] if isinstance(accessions, str) else list(accessions)
        found: dict[str, list[Link]] = {a: [] for a in wanted}
        for start, end, forward, source in LINKS:
            if start in found:
                found[start].append(Link(forward, end, source, True))
            if end in found:
                found[end].append(Link(RELATIONS[forward], start, source, False))
        return {a: [k for k in ks if relation in (None, k.relation)] for a, ks in found.items()}

    def open_many(self, accessions: Iterable[str]) -> dict[str, Record]:
        day = dt.date(2026, 1, 1)
        return {
            a: Record(a, "GEO Series", None, day, day, day, (), "", day, tuple(links))
            for a, links in self.links(accessions).items()
        }


class Papers:
    """liulab-data's paper reader: every paper is held, with an abstract."""

    def papers(self, pmids: Iterable[str]) -> dict[str, Any]:
        info = {"journal": "J", "published": None, "publication_type": (), "abstract": "Text."}
        return {
            p: types.SimpleNamespace(info=types.SimpleNamespace(title=f"paper {p}", **info))
            for p in pmids
        }


def _jev() -> FakeGateway:
    """Plays Jev. The job asks a study's questions as written first, then reversed."""
    asked: set[str] = set()
    lock = threading.Lock()

    def answer(form: dict[str, Any]) -> dict[str, Any]:
        with lock:
            reverse = form["description"] in asked
            asked.add(form["description"])
        out = {}
        for name in form["questions"]:
            kind, _, number = name.partition("_")
            record = number if kind == "s" else f"pmid:{number}"
            out[name] = {"noul": JEV[record][reverse]}
        return out

    return FakeGateway(answer, prices={bounding.JEV.name: Price(0.0, 0.0)})


@pytest.fixture(scope="module")
def drawn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, dict[str, Any], Any]]:
    """One pass over GSE10, GSE20 and GSE30, written twice beside the standard set's own files."""
    kind = dataclasses.replace(
        bounding.KIND,
        build=lambda store: bounding.bounds_kind(Store(), Papers()),
        reads_store=False,
    )
    fake, here = _jev(), tmp_path_factory.mktemp("bounding")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setitem(KINDS, kind.name, kind)
        monkeypatch.setattr(
            "seqforge.passes.gateway.make_gateway", lambda: fake.client(repo="seqforge")
        )
        monkeypatch.delenv("SEQFORGE_REVIEWS", raising=False)
        monkeypatch.chdir(here)
        clone = ["--reviews", str(here / "clone")]
        (here / "clone" / "reviews").mkdir(parents=True)
        (here / "keys.txt").write_text("GSE10\nGSE20\nGSE30\n")
        started = runner.invoke(
            app, ["pass", "start", kind.name, "--ceiling-usd", "1", "--keys", "keys.txt", *clone]
        )
        assert started.exit_code == 0, started.output
        counts = json.loads(started.stdout)["counts"]
        assert (counts["settled"], counts["unsettled"], counts["refused"]) == (1, 1, 1)

        set_dir = here / "set"
        (set_dir / "GSE10").mkdir(parents=True)
        (set_dir / "GSE10" / "bounds.yaml").write_text("the standard set's own\n")
        write = ["study", "write-bounds", "--out", str(set_dir), "--name", "job-bounds.yaml"]
        written, again = runner.invoke(app, [*write, *clone]), runner.invoke(app, [*write, *clone])
        assert (written.exit_code, again.exit_code) == (0, 0), written.output
        yield set_dir, json.loads(written.stdout), json.loads(again.stdout)


def _placed(bounds: dict[str, Any]) -> dict[str, Any]:
    """Each record the bounds place: in or out, and its probability (a kept paper has none)."""
    out: dict[str, Any] = {claim["id"]: ("in", claim["p"]) for claim in bounds["series"]}
    out |= dict.fromkeys(bounds["papers"], ("in", None))
    return out | {claim["id"]: ("out", claim["p"]) for claim in bounds["left_out"]}


def test_a_study_is_its_series_and_every_record_one_link_away_each_placed_by_jev(
    drawn: tuple[Path, dict[str, Any], Any],
) -> None:
    """One hop over the four kinds of link, and nothing that a reanalysis, a BioProject's own paper,
    a placeholder paper id or a second hop links. Code keeps the starting Series and leaves the
    SuperSeries out; Jev's answer as written places every other record, and gives its probability.
    The job's file goes beside the standard set's own, which it never overwrites."""
    set_dir, said, again = drawn
    bounds = yaml.safe_load((set_dir / "GSE10" / "job-bounds.yaml").read_text())

    assert check_bounds(bounds) == []
    assert _placed(bounds) == {
        "GSE10": ("in", None),
        "GSE11": ("in", 0.9),
        "GSE12": ("in", 0.8),
        "pmid:100": ("in", None),
        "GSE19": ("out", None),
        "GSE13": ("out", 0.8),
        "GSE14": ("out", 0.7),
        "pmid:200": ("out", 0.9),
    }
    assert bounds["models"] == {
        "bounds": "typesafe/jev-1.14.0",
        "probabilities": "typesafe/jev-1.14.0",
    }
    assert all("inferred" in claim for claim in bounds["series"] + bounds["left_out"])
    assert said["studies"]["GSE10"] == {"ending": "settled", "file": "written"}
    assert again["studies"]["GSE10"]["file"] == "kept the file on disk"
    assert (set_dir / "GSE10" / "bounds.yaml").read_text() == "the standard set's own\n"


def test_a_study_whose_two_answers_disagree_on_a_record_is_unsettled_and_still_written(
    drawn: tuple[Path, dict[str, Any], Any],
) -> None:
    """Jev says yes to pmid:401 as asked and yes reversed. The study is unsettled, and its file
    places the paper by the answer as written."""
    set_dir, said, _ = drawn
    bounds = yaml.safe_load((set_dir / "GSE20" / "job-bounds.yaml").read_text())

    assert said["studies"]["GSE20"]["ending"] == "unsettled"
    assert "pmid:401" in said["studies"]["GSE20"]["checks_failed"][0]
    assert check_bounds(bounds) == []
    assert bounds["papers"] == ["pmid:400", "pmid:401"]


def test_a_group_of_more_than_fifty_series_is_refused_and_written_nowhere(
    drawn: tuple[Path, dict[str, Any], Any],
) -> None:
    set_dir, said, _ = drawn

    assert said["studies"]["GSE30"] == {"ending": "refused", "reason": "group too large: 52 Series"}
    assert not (set_dir / "GSE30").exists()
