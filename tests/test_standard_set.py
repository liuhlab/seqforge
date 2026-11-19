"""Drawing a study of the standard set: the drafting pass, the draft file, and the judged study file.

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
        "split_samples": [],
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
    """One round: the judge's version wins, an unsure study stays, flagged, and no file of the
    study is overwritten -- not the draft now, and not the verdict by a second call."""
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
    assert (account["bounds_from"], account["status"]) == ("judge", "unsure")
    assert study["bounds"]["left_out"] == COMPLETE
    assert (study["bounds_from"], study["status"]) == ("judge", "unsure")
    assert study["read_to_decide"] == ["records", "full_paper"]
    assert study["records"]["series"] == ["GSE1", "GSE2"]
    assert (saved["model"], saved["agrees"]) == (judge.JUDGE_MODEL, False)
    assert (drafted / "S2" / "draft.yaml").read_text() == before
    with pytest.raises(judge.JudgeStopped, match="already judged"):
        _run_judge(drafted, tmp_path)


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
