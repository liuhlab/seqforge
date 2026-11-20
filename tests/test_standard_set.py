"""Drawing a study of the standard set: the drafting pass, the draft file, and the judged study's files.

Both models are faked. The drafting model is liulab-llms's fake gateway behind ``seqforge pass``;
the judging model is a function in place of the headless command. No test reads a store: the linked
group is built by hand, in the shape ``read_group`` gives.
"""

from __future__ import annotations

import datetime as dt
import json
import types
from pathlib import Path
from typing import Any

import pytest
import yaml
from labllm import Model
from labllm.testing import FakeGateway, Price
from typer.testing import CliRunner

from seqforge.cli import app
from seqforge.passes.kinds import KINDS, Kind
from seqforge.standard_set import draft, judge
from seqforge.study.bounds import check_bounds

runner = CliRunner()
WHEN = dt.datetime(2026, 11, 19, 12, 0, tzinfo=dt.UTC)
DRAFTER = Model("test", "small", max_output_tokens=500)


def _record(accession: str) -> Any:
    return types.SimpleNamespace(accession=accession, type="GEO record", fields=(("Title", "t"),))


def _group(key: str) -> dict[str, Any]:
    """An older Series with its paper, and a newer Series, with its own paper, that re-uses it."""

    def series(gse: str, pmids: list[str], samples: list[str]) -> dict[str, Any]:
        return {
            "id": gse,
            "held": True,
            "record": _record(gse),
            "fields": {"Title": [f"title of {gse}"]},
            "superseries_of": [],
            "bioprojects": [f"PRJNA{gse[3:]}"],
            "pmids": pmids,
            "samples": [
                {
                    "id": s,
                    "record": _record(s),
                    "title": s,
                    "source": "",
                    "characteristics": [],
                    "library": "RNA-Seq / transcriptomic",
                    "series": [gse],
                }
                for s in samples
            ],
        }

    def paper(pmid: str, linked: list[str]) -> dict[str, Any]:
        return {
            "id": f"pmid:{pmid}",
            "pmid": pmid,
            "paper": None,
            "title": f"paper {pmid}",
            "journal": "J",
            "published": "2020-01-01",
            "types": [],
            "abstract": "An abstract.",
            "series": linked,
        }

    return {
        "id": key,
        "series": [
            series("GSE1", ["11"], ["GSM1", "GSM2"]),
            series("GSE2", ["11", "22"], ["GSM3"]),
        ],
        "papers": [paper("11", ["GSE1", "GSE2"]), paper("22", ["GSE2"])],
        "superseries": None,
    }


def _bounds(left_out: dict[str, str]) -> dict[str, Any]:
    return {
        "series": ["GSE1"],
        "papers": ["pmid:11"],
        "reason": "The older Series and its own paper.",
        "left_out": [{"id": k, "reason": v} for k, v in left_out.items()],
    }


COMPLETE = {"GSE2": "newer study that re-uses it", "pmid:22": "the newer study's paper"}


def _small_model(form: dict[str, Any]) -> str:
    """Plays the drafter: S1's draft places every record; S2's forgets the newer Series' paper."""
    text = form["messages"][-1]["content"]
    left_out = COMPLETE if "PICKED BY: S1" in text else {"GSE2": COMPLETE["GSE2"]}
    return json.dumps({"summary": "One older dataset and its paper", "bounds": _bounds(left_out)})


@pytest.fixture
def drafted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A set folder holding the drafts of S1 and S2, written from one faked drafting pass."""
    kind = Kind(
        name=draft.KIND_NAME,
        build=lambda store: draft.bounds_kind(_group),
        default_models={"drafter": DRAFTER},
        default_keys=lambda store: ["S1", "S2"],
        review_form=draft.KIND.review_form,
    )
    monkeypatch.setitem(KINDS, kind.name, kind)
    fake = FakeGateway(_small_model, prices={DRAFTER.name: Price(0.0, 0.00001)})
    monkeypatch.setattr(
        "seqforge.passes.gateway.make_gateway", lambda: fake.client(repo="seqforge")
    )
    monkeypatch.delenv("SEQFORGE_REVIEWS", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "clone" / "reviews").mkdir(parents=True)
    clone = ["--reviews", str(tmp_path / "clone")]

    started = runner.invoke(app, ["pass", "start", draft.KIND_NAME, "--ceiling-usd", "1", *clone])
    assert started.exit_code == 0, started.output
    counts = json.loads(started.stdout)["counts"]
    assert (counts["settled"], counts["unsettled"]) == (1, 1)

    set_dir = tmp_path / "set"
    written = runner.invoke(app, ["standard-set", "draft", "--dir", str(set_dir), *clone])
    assert written.exit_code == 0, written.output
    return set_dir


def test_a_draft_that_leaves_a_linked_record_unplaced_is_kept_and_says_so(drafted: Path) -> None:
    """Every record of the linked group is in the study or left out with a reason. A draft that
    breaks that is still a draft: it is written, with what the check found, for the judge to fix."""
    whole = yaml.safe_load((drafted / "S1" / "draft.yaml").read_text())
    short = yaml.safe_load((drafted / "S2" / "draft.yaml").read_text())

    assert whole["checks_failed"] == []
    assert whole["bounds"]["left_out"] == COMPLETE
    assert whole["model"] == "test/small"
    assert short["checks_failed"] == ["well_formed: neither in the study nor left out: pmid:22"]


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        pytest.param(
            {"series": ["pmid:11"], "papers": ["GSE1"]}, "wrong list", id="a paper as a Series"
        ),
        pytest.param({"series": ["GSE1", "GSE1"]}, "more than once in series", id="a Series twice"),
    ],
)
def test_bounds_name_each_record_once_and_in_the_list_of_its_own_kind(
    change: dict[str, Any], problem: str
) -> None:
    """The draft and the verdict key their lists by identifier, so a repeat or a paper among the
    Series would be lost or misread there: it is refused while it is still what the model wrote."""
    assert draft.bounds_problems(_bounds(COMPLETE), _group("S1")) == []

    problems = draft.bounds_problems({**_bounds(COMPLETE), **change}, _group("S1"))

    assert any(problem in found for found in problems), problems


def test_judging_a_study_whose_records_link_a_paper_needs_the_paper_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The judge is owed every paper of the linked group. The pick may list none of them, since
    a record can gain a paper after picking, so the group as read decides, not the pick."""
    (tmp_path / "pick.yaml").write_text(
        yaml.safe_dump({"studies": [{"id": "S1", "linked_series": ["GSE1"], "pmids": []}]})
    )
    store = types.SimpleNamespace(settings={})
    monkeypatch.setattr("seqforge.standard_set.material.open_sources", lambda: (store, None))
    monkeypatch.setattr(
        "seqforge.standard_set.material.read_group", lambda store, papers, entry: _group("S1")
    )
    monkeypatch.setattr(judge, "ask_claude", lambda prompt, folder: pytest.fail("judge was asked"))

    result = runner.invoke(app, ["standard-set", "judge", "S1", "--dir", str(tmp_path)])

    assert result.exit_code == 2
    assert "no paper store" in result.output


ENTRY = {
    "id": "S2",
    "direction": "aging",
    "hard_shape": None,
    "reason_picked": "A re-used dataset.",
}


def _judge(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> None:
    usage = {judge.JUDGE_MODEL: {"outputTokens": 10}}
    monkeypatch.setattr(
        judge,
        "ask_claude",
        lambda prompt, folder: {"modelUsage": usage, "is_error": False, **result},
    )


def _run_judge(set_dir: Path, tmp_path: Path) -> dict[str, Any]:
    return judge.judge_study(
        ENTRY, _group("S2"), set_dir=set_dir, work=tmp_path / "work", store_build="b1", when=WHEN
    )


def test_where_the_judge_disagrees_its_bounds_become_the_study_and_the_draft_stays(
    drafted: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One round: the judge's version wins and is written as the study's bounds file, an unsure
    study stays, flagged, and no file of the study is overwritten -- not the draft now, and not the
    verdict by a second call."""
    before = (drafted / "S2" / "draft.yaml").read_text()
    verdict = {
        "agrees": False,
        "bounds": _bounds(COMPLETE),
        "reason": "The draft dropped a paper.",
        "unsure": True,
        "read": ["full_paper", "records"],
    }
    _judge(monkeypatch, {"structured_output": verdict})

    account = _run_judge(drafted, tmp_path)

    study = yaml.safe_load((drafted / "S2" / "study.yaml").read_text())
    saved = yaml.safe_load((drafted / "S2" / "verdict.yaml").read_text())
    bounds = yaml.safe_load((drafted / "S2" / "bounds.yaml").read_text())
    assert (account["bounds_from"], account["status"]) == ("judge", "unsure")
    assert "bounds" not in study
    assert check_bounds(bounds) == []
    assert (bounds["study"], [claim["id"] for claim in bounds["series"]]) == ("S2", ["GSE1"])
    assert {claim["id"]: claim["inferred"] for claim in bounds["left_out"]} == COMPLETE
    assert bounds["models"] == {"bounds": judge.JUDGE_MODEL, "probabilities": None}
    assert (study["bounds_from"], study["status"]) == ("judge", "unsure")
    assert study["read_to_decide"] == ["records", "full_paper"]
    assert study["records"]["series"] == ["GSE1", "GSE2"]
    assert (saved["model"], saved["agrees"]) == (judge.JUDGE_MODEL, False)
    assert (drafted / "S2" / "draft.yaml").read_text() == before
    with pytest.raises(judge.JudgeStopped, match="already judged"):
        _run_judge(drafted, tmp_path)

    # A call cut off after the verdict left no bounds file and no study file. The next call
    # finishes both from the draft and the verdict, and must not ask the judge again.
    whole = {name: (drafted / "S2" / name).read_text() for name in ("bounds.yaml", "study.yaml")}
    for name in whole:
        (drafted / "S2" / name).unlink()
    monkeypatch.setattr(judge, "ask_claude", lambda prompt, folder: pytest.fail("asked again"))
    assert _run_judge(drafted, tmp_path)["finished_from_verdict"] is True
    assert {name: (drafted / "S2" / name).read_text() for name in whole} == whole


@pytest.mark.parametrize(
    "result",
    [
        pytest.param({"stop_reason": "refusal", "structured_output": None}, id="the judge refuses"),
        pytest.param(
            {
                "structured_output": {
                    "agrees": True,
                    "bounds": None,
                    "reason": "",
                    "unsure": False,
                    "read": [],
                }
            },
            id="the judge agrees with a malformed draft",
        ),
    ],
)
def test_a_judge_that_leaves_no_usable_bounds_stops_the_study_and_writes_nothing(
    drafted: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]
) -> None:
    """A refusal is reported and never worked around, and malformed bounds never become a study."""
    _judge(monkeypatch, result)

    with pytest.raises(judge.JudgeStopped):
        _run_judge(drafted, tmp_path)

    assert sorted(p.name for p in (drafted / "S2").iterdir()) == ["draft.yaml"]


@pytest.mark.repo
def test_the_standard_set_lists_fifty_studies_each_with_identifiers_reason_and_bounds() -> None:
    """The repo's own set, through the listing command: every study still in the pick is drawn, and
    nothing else is. A study added or dropped without the pick, a direction split that drifted, a
    study file missing what later stages read, or a bounds file that is not in the current format
    goes red here."""
    from seqforge.standard_set import default_set_dir
    from seqforge.standard_set.material import pick_entries

    listed = {s["id"]: s for s in json.loads(runner.invoke(app, ["standard-set", "list"]).stdout)}
    picked = pick_entries(default_set_dir())

    assert sorted(listed) == sorted(picked)
    by_direction = [s["direction"] for s in listed.values()]
    assert {d: by_direction.count(d) for d in set(by_direction)} == {
        "aging": 17,
        "neurodegeneration": 17,
        "c_elegans": 16,
    }
    for key, study in listed.items():
        assert study["reason_picked"], key
        assert study["records"]["series"], key
        bounds = study["bounds"]
        assert check_bounds(bounds) == [], key
        assert bounds["study"] == key
        assert {claim["id"] for claim in bounds["series"]} <= set(study["records"]["series"]), key
