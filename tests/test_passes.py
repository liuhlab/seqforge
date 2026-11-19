"""`seqforge pass` and `seqforge review`, driven in-process through the CLI, with liulab-llms's fake
gateway and an in-memory review repo behind them.

A pass of a seqforge kind is the run engine's (liulab-llms's) work; what seqforge owns is the
command around it: which kind, which models, which keys, which task, and what a caller gets back --
one JSON object on stdout and an exit code it can act on. So every test here asserts only on that
JSON, the exit code, the fake gateway's record, which is part of liulab-llms's contract, and the
issues and labels in the review repo stand-in. None reads the task's state file.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from labllm import Reviews, Task
from labllm.testing import FakeGateway
from typer.testing import CliRunner

from conftest import (
    ECHO_FORM,
    ECHO_STRONG,
    ECHO_TEXTS,
    ECHO_WEAK,
    FakeGitHub,
    StubStore,
    echo_unit_kind,
)
from seqforge.cli import app
from seqforge.passes import gateway
from seqforge.passes.kinds import KINDS, Kind
from seqforge.passes.review import Marker, Start, read_answer, read_marker

runner = CliRunner()


def start(clone: Path, *argv: str) -> tuple[int, dict[str, Any]]:
    """Run ``seqforge pass start`` and return its exit code and its JSON."""
    result = runner.invoke(app, ["pass", "start", *argv, "--reviews", str(clone)])
    assert result.stdout, result.stderr
    return result.exit_code, json.loads(result.stdout)


def test_a_pass_that_reaches_the_end_of_its_keys_exits_0_and_counts_every_ending(
    echo_kind: Any, echo_gateway: FakeGateway, review_clone: Path
) -> None:
    code, out = start(review_clone, "echo", "--ceiling-usd", "1")

    assert code == 0
    assert out == {
        "kind": "echo",
        "kind_version": "1",
        "task": str(Path("seqforge", "passes", "echo")),
        "pass_id": 1,
        "label": "echo",
        "store_build": None,
        "models": {
            "writer": {
                "provider": ECHO_WEAK.provider,
                "name": ECHO_WEAK.name,
                "thinking": None,
                "effort": None,
                "temperature": None,
                "max_output_tokens": 50,
            }
        },
        "keys": 4,
        "counts": {"settled": 1, "unsettled": 2, "refused": 1, "unfinished": 0, "held": 0},
        "stopped": None,
        "message": None,
        "spent_usd": out["spent_usd"],
        "first_cost_usd": out["first_cost_usd"],
        "requests": 4,
        "cache_hits": 0,
    }
    assert out["spent_usd"] == out["first_cost_usd"] > 0


def test_a_pass_stopped_at_its_ceiling_exits_3_and_its_rerun_replays_answers_at_no_cost(
    echo_kind: Any, echo_gateway: FakeGateway, review_clone: Path
) -> None:
    # Each call holds back five cents, so six cents lets the first unit through and stops the next.
    code, out = start(review_clone, "echo", "--ceiling-usd", "0.06", "--units-at-once", "1")

    assert code == 3
    assert out["stopped"] == "ceiling_reached"
    # The gateway's own words for the stop, not only the reason already in `stopped`.
    assert "over its ceiling of 0.06" in out["message"]
    assert out["counts"] == {"settled": 1, "unsettled": 0, "refused": 0, "unfinished": 3, "held": 0}

    sent = len(echo_gateway.record)
    code, out = start(review_clone, "echo", "--ceiling-usd", "1", "--units-at-once", "1")

    assert code == 0, out
    assert (out["stopped"], out["message"]) == (None, None)
    assert out["counts"] == {"settled": 1, "unsettled": 2, "refused": 1, "unfinished": 0, "held": 0}
    rerun = echo_gateway.record[sent:]
    assert {x.tag for x in rerun if x.cached} == {"u1"}
    assert all(x.cost_usd == 0 for x in rerun if x.cached)
    assert out["cache_hits"] == 1


def test_only_unsettled_and_refused_runs_those_units_on_a_stronger_model_and_skips_reviewed_ones(
    echo_kind: Any, echo_gateway: FakeGateway, review_clone: Path
) -> None:
    code, _ = start(review_clone, "echo", "--ceiling-usd", "1")
    assert code == 0
    # A person settles delta, one of the weak model's two wrong answers, as review read will.
    Reviews(review_clone / "reviews").save(
        echo_unit_kind(ECHO_TEXTS),
        "u4",
        who="a reviewer",
        inputs_tag="text=delta",
        answer={"word": "DELTA"},
    )

    strong = f"writer={ECHO_STRONG.provider}/{ECHO_STRONG.name},max-output-tokens=50"
    sent = len(echo_gateway.record)
    code, out = start(
        review_clone, "echo", "--ceiling-usd", "1", "--only", "unsettled,refused", "--model", strong
    )

    assert code == 0
    assert out["keys"] == 2
    assert out["models"]["writer"]["name"] == ECHO_STRONG.name
    assert out["counts"] == {"settled": 2, "unsettled": 0, "refused": 0, "unfinished": 0, "held": 0}
    assert {x.tag for x in echo_gateway.record[sent:]} == {"u2", "u3"}


def test_keys_file_runs_exactly_its_keys_in_its_order(
    echo_kind: Any, echo_gateway: FakeGateway, review_clone: Path, tmp_path: Path
) -> None:
    keys = tmp_path / "keys.txt"
    keys.write_text("u3\nu1\n")

    code, out = start(
        review_clone, "echo", "--ceiling-usd", "1", "--keys", str(keys), "--units-at-once", "1"
    )

    assert code == 0
    assert out["keys"] == 2
    assert [x.tag for x in echo_gateway.record] == ["u3", "u1"]


def test_a_task_another_process_holds_exits_3_naming_the_holder(
    echo_kind: Any, echo_gateway: FakeGateway, review_clone: Path
) -> None:
    folder = Path("seqforge", "passes", "echo")
    with Task(folder, gateway=echo_gateway.client(), reviews=review_clone / "reviews"):
        code, out = start(review_clone, "echo", "--ceiling-usd", "1")

    assert code == 3
    assert out["stopped"] == "task_in_use"
    assert f"process {os.getpid()} on {socket.gethostname()}" in out["message"]
    assert echo_gateway.record == []


def _no_gateway_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("seqforge.passes.gateway.make_gateway", _REAL_MAKE_GATEWAY)
    monkeypatch.delenv("LABLLM_GATEWAY", raising=False)


def _state_file_of_another_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    folder = Path("seqforge", "passes", "echo")
    folder.mkdir(parents=True)
    (folder / "state.sqlite").write_text("a file liulab-llms did not write")


#: The real gateway seam, before a test's fixture replaces it with the fake's client.
_REAL_MAKE_GATEWAY = gateway.make_gateway
_CLONE = "<the review clone>"
_SET_UP = ["echo", "--ceiling-usd", "1", "--reviews", _CLONE]


@pytest.mark.parametrize(
    ("argv", "arrange", "reason"),
    [
        pytest.param(["nope", *_SET_UP[1:]], None, "no kind named 'nope'", id="unknown-kind"),
        pytest.param(
            [*_SET_UP, "--model", "reader=test/echo-weak"], None, "no role reader", id="roles"
        ),
        pytest.param(
            [*_SET_UP, "--model", "writer=echo-weak"], None, "ROLE=PROVIDER/NAME", id="model"
        ),
        pytest.param(
            [*_SET_UP, "--keys", "absent.txt"], None, "cannot read the keys file", id="keys-file"
        ),
        pytest.param(_SET_UP[:3], None, "no reviews folder", id="no-reviews"),
        pytest.param(_SET_UP, _no_gateway_address, "LABLLM_GATEWAY", id="no-gateway"),
        pytest.param(
            _SET_UP, _state_file_of_another_layout, "not a task's state file", id="layout"
        ),
        pytest.param(["echo", "--reviews", _CLONE], None, None, id="no-ceiling"),
    ],
)
def test_a_setup_the_pass_cannot_use_exits_2_with_a_one_line_reason(
    argv: list[str],
    arrange: Callable[[pytest.MonkeyPatch], None] | None,
    reason: str | None,
    echo_kind: Any,
    echo_gateway: FakeGateway,
    review_clone: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if arrange is not None:
        arrange(monkeypatch)

    argv = [str(review_clone) if a == _CLONE else a for a in argv]
    result = runner.invoke(app, ["pass", "start", *argv])

    assert result.exit_code == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert echo_gateway.record == []
    if reason is None:  # the ceiling has no default: leaving it out is the CLI's usage error
        assert "--ceiling-usd" in result.stderr
    else:
        assert reason in result.stderr
        assert len(result.stderr.strip().splitlines()) == 1, result.stderr


def test_a_kind_that_reads_the_store_reads_one_build_and_names_it(
    echo_gateway: FakeGateway, review_clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import labdata

    # The store is refreshed as soon as the pass has opened it: every later open reads build b2.
    first = StubStore({}, {"u1": "alpha", "u2": "beta"}, [], settings={"build": "b1"})
    refreshed = StubStore({}, {"u1": "omega", "u3": "psi"}, [], settings={"build": "b2"})
    opens = iter([first])
    monkeypatch.setattr(labdata, "open_store", lambda root=None: next(opens, refreshed))

    def texts(store: Any) -> dict[str, str]:
        return dict(store.texts)

    kind = Kind(
        name="stored-echo",
        build=lambda store: echo_unit_kind(texts(store), name="stored-echo"),
        default_models={"writer": ECHO_WEAK},
        default_keys=lambda store: sorted(texts(store)),
        review_form=ECHO_FORM,
        reads_store=True,
    )
    monkeypatch.setitem(KINDS, kind.name, kind)

    code, out = start(review_clone, "stored-echo", "--ceiling-usd", "1")

    assert code == 0
    assert out["store_build"] == "b1"
    asked = sorted(x.form["messages"][-1]["content"] for x in echo_gateway.record if x.form)
    assert asked == ["alpha", "beta"]


def file_reviews(clone: Path, *argv: str) -> tuple[int, dict[str, Any]]:
    """Run ``seqforge review file`` and return its exit code and its JSON."""
    result = runner.invoke(app, ["review", "file", *argv, "--reviews", str(clone)])
    assert result.stdout, result.stderr
    return result.exit_code, json.loads(result.stdout)


def tick(body: str, label: str) -> str:
    """Tick the box a person sees as ``label``, as GitHub does: ``[ ]`` becomes ``[x]`` on its line."""
    lines = body.splitlines()
    (at,) = [n for n, line in enumerate(lines) if line.startswith("- [ ] ") and label in line]
    lines[at] = "- [x] " + lines[at].removeprefix("- [ ] ")
    return "\n".join(lines)


def test_review_file_files_the_queue_in_the_kinds_priority_labelled_and_marked(
    echo_kind: Any, echo_gateway: FakeGateway, review_clone: Path, github: FakeGitHub
) -> None:
    code, _ = start(review_clone, "echo", "--ceiling-usd", "1")
    assert code == 0

    code, out = file_reviews(review_clone, "echo", "--count", "2")

    assert code == 0, out
    # The echo kind reviews refused units first: gamma, then beta. Delta waits for the next filing.
    assert out["filed"] == [
        {"key": key, "url": issue.url}
        for key, issue in zip(("u3", "u2"), github.filed, strict=True)
    ]
    assert out["unfiled"] == 1
    assert [sorted(i.labels) for i in github.filed] == [
        ["check:answered", "review:echo"],
        ["check:in_capitals", "review:echo"],
    ]
    beta = github.filed[1].body
    # What a reviewer reads: the records with links, each pass's answer with its models and stamp,
    # and each failed check with its message.
    for shown in (
        "https://en.wiktionary.org/wiki/beta",
        "The word `ateb`",
        f"writer={ECHO_WEAK.provider}/{ECHO_WEAK.name}",
        "`text=beta`",
        "in_capitals: 'ateb' is not 'BETA'",
    ):
        assert shown in beta, shown
    assert read_marker(beta) == Marker(
        kind="echo",
        key="u2",
        inputs_tag="text=beta",
        starts={"pass-1": Start(1, {"word": "ateb"}), "candidate-1": Start(None, {"word": "BETA"})},
    )
    # A person starts from pass 1's answer and ticks capitals. The ticks read back with no task;
    # storing the answer is what runs the kind's checks on it.
    ticked = tick(tick(beta, "Pass 1's answer"), "Write it in capitals")
    assert read_answer(ECHO_FORM, ticked) == ({"word": "ATEB"}, 1)


def test_review_file_never_files_a_unit_twice_for_the_same_inputs_tag(
    echo_kind: Any,
    echo_gateway: FakeGateway,
    review_clone: Path,
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start(review_clone, "echo", "--ceiling-usd", "1")
    code, out = file_reviews(review_clone, "echo")
    assert code == 0
    assert (len(out["filed"]), out["unfiled"]) == (3, 0)
    github.close(1, "completed")  # a closed issue counts as filed as much as an open one

    code, out = file_reviews(review_clone, "echo")

    assert code == 0
    assert (out["filed"], out["unfiled"]) == ([], 0)
    assert len(github.filed) == 3

    # Beta's text changes, so the next pass answers it for new inputs: it is filed again.
    monkeypatch.setitem(ECHO_TEXTS, "u2", "bravo")
    start(review_clone, "echo", "--ceiling-usd", "1")

    code, out = file_reviews(review_clone, "echo")

    assert code == 0
    assert [f["key"] for f in out["filed"]] == ["u2"]
    marker = read_marker(github.filed[-1].body)
    assert marker is not None
    assert (marker.key, marker.inputs_tag) == ("u2", "text=bravo")


def test_review_file_makes_each_label_the_repo_lacks_and_leaves_the_others_as_they_are(
    echo_kind: Any, echo_gateway: FakeGateway, review_clone: Path, github: FakeGitHub
) -> None:
    # A person made the kind's label by hand before anything was filed.
    github.label_table["review:echo"] = ("000000", "made by hand")
    before = dict(github.label_table)
    start(review_clone, "echo", "--ceiling-usd", "1")

    code, out = file_reviews(review_clone, "echo")

    assert code == 0, out
    made = {name: github.label_table[name] for name in github.label_table.keys() - before.keys()}
    assert sorted(made) == ["check:answered", "check:in_capitals"]
    assert all(description for _, description in made.values())
    assert {name: github.label_table[name] for name in before} == before


def test_review_file_on_a_task_another_process_holds_exits_3_naming_the_holder(
    echo_kind: Any, echo_gateway: FakeGateway, review_clone: Path, github: FakeGitHub
) -> None:
    start(review_clone, "echo", "--ceiling-usd", "1")
    folder = Path("seqforge", "passes", "echo")
    with Task(folder, gateway=echo_gateway.client(), reviews=review_clone / "reviews"):
        code, out = file_reviews(review_clone, "echo")

    assert code == 3
    assert out["stopped"] == "task_in_use"
    assert f"process {os.getpid()} on {socket.gethostname()}" in out["message"]
    assert github.filed == []


def test_review_file_on_a_folder_with_no_task_exits_2_and_makes_none(
    echo_kind: Any, review_clone: Path, github: FakeGitHub
) -> None:
    result = runner.invoke(app, ["review", "file", "echo", "--reviews", str(review_clone)])

    assert result.exit_code == 2, result.stdout + result.stderr
    assert "no task in" in result.stderr
    assert not Path("seqforge", "passes", "echo").exists()
