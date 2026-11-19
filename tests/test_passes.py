"""`seqforge pass` and `seqforge review`, driven in-process through the CLI, with liulab-llms's fake
gateway and an in-memory review repo behind them.

A pass of a seqforge kind is the run engine's (liulab-llms's) work; what seqforge owns is the
command around it: which kind, which models, which keys, which task, and what a caller gets back --
one JSON object on stdout and an exit code it can act on. So every test here asserts only on that
JSON, the exit code, the fake gateway's record, which is part of liulab-llms's contract, and the
issues and labels in the review repo stand-in. None reads the task's state file.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from labllm import Reviews, Task
from labllm.testing import FakeGateway
from typer.testing import CliRunner

from conftest import StubStore
from passes_kit import (
    ECHO_FORM,
    ECHO_PRICES,
    ECHO_STRONG,
    ECHO_TEXTS,
    ECHO_WEAK,
    FakeGitHub,
    echo_priority,
    echo_unit_kind,
    echo_writer,
)
from seqforge.cli import app
from seqforge.passes import gateway
from seqforge.passes.kinds import KINDS, Kind
from seqforge.passes.review import Marker, Start, read_answer, read_marker

runner = CliRunner()

#: The echo kind's task folder, where a command run in the test's working directory makes it.
TASK = Path("seqforge", "passes", "echo")
#: A plain pass of the echo kind, with a ceiling it never reaches.
PASS_START = ("pass", "start", "echo", "--ceiling-usd", "1")


def seqforge(clone: Path, *argv: str) -> tuple[int, dict[str, Any]]:
    """Run ``seqforge <argv> --reviews <clone>`` and return its exit code and its JSON."""
    result = runner.invoke(app, [*argv, "--reviews", str(clone)])
    assert result.stdout, result.stderr
    return result.exit_code, json.loads(result.stdout)


@pytest.fixture
def echo_kind(monkeypatch: pytest.MonkeyPatch) -> Kind:
    """The echo kind, registered under ``echo`` for this test only."""
    kind = Kind(
        name="echo",
        build=lambda store: echo_unit_kind(ECHO_TEXTS),
        default_models={"writer": ECHO_WEAK},
        default_keys=lambda store: list(ECHO_TEXTS),
        review_form=ECHO_FORM,
        review_priority=echo_priority,
    )
    monkeypatch.setitem(KINDS, kind.name, kind)
    return kind


def _serve(
    monkeypatch: pytest.MonkeyPatch, provider: Callable[[dict[str, Any]], str]
) -> FakeGateway:
    """liulab-llms's fake gateway, with ``provider`` playing the provider, behind the commands."""
    fake = FakeGateway(provider, prices=ECHO_PRICES)
    monkeypatch.setattr(
        "seqforge.passes.gateway.make_gateway", lambda: fake.client(repo="seqforge")
    )
    return fake


@pytest.fixture
def echo_gateway(monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    """The fake gateway, with :func:`echo_writer` as the provider."""
    return _serve(monkeypatch, echo_writer)


@pytest.fixture
def review_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty clone of the review repo, and the working directory a pass's task defaults under.

    ``SEQFORGE_REVIEWS`` is cleared, so a machine that sets it cannot hand a test its real clone.
    """
    monkeypatch.delenv("SEQFORGE_REVIEWS", raising=False)
    monkeypatch.chdir(tmp_path)
    clone = tmp_path / "review-clone"
    (clone / "reviews").mkdir(parents=True)
    return clone


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    """The in-memory review repo, behind the review commands."""
    fake = FakeGitHub()
    monkeypatch.setattr("seqforge.passes.github.make_github", lambda clone: fake)
    return fake


def test_a_pass_that_reaches_the_end_of_its_keys_exits_0_and_counts_every_ending(
    echo_kind: Any, echo_gateway: FakeGateway, review_clone: Path
) -> None:
    code, out = seqforge(review_clone, *PASS_START)

    assert code == 0
    assert out == {
        "kind": "echo",
        "kind_version": "1",
        "task": str(TASK),
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
    code, out = seqforge(
        review_clone, "pass", "start", "echo", "--ceiling-usd", "0.06", "--units-at-once", "1"
    )

    assert code == 3
    assert out["stopped"] == "ceiling_reached"
    # The gateway's own words for the stop, not only the reason already in `stopped`.
    assert "over its ceiling of 0.06" in out["message"]
    assert out["counts"] == {"settled": 1, "unsettled": 0, "refused": 0, "unfinished": 3, "held": 0}

    sent = len(echo_gateway.record)
    code, out = seqforge(
        review_clone, "pass", "start", "echo", "--ceiling-usd", "1", "--units-at-once", "1"
    )

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
    code, _ = seqforge(review_clone, *PASS_START)
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
    code, out = seqforge(
        review_clone,
        "pass",
        "start",
        "echo",
        "--ceiling-usd",
        "1",
        "--only",
        "unsettled,refused",
        "--model",
        strong,
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

    code, out = seqforge(
        review_clone,
        "pass",
        "start",
        "echo",
        "--ceiling-usd",
        "1",
        "--keys",
        str(keys),
        "--units-at-once",
        "1",
    )

    assert code == 0
    assert out["keys"] == 2
    assert [x.tag for x in echo_gateway.record] == ["u3", "u1"]


def _no_gateway_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("seqforge.passes.gateway.make_gateway", _REAL_MAKE_GATEWAY)
    monkeypatch.delenv("LABLLM_GATEWAY", raising=False)


def _state_file_of_another_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    TASK.mkdir(parents=True)
    (TASK / "state.sqlite").write_text("a file liulab-llms did not write")


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
        # The gateway is the only judge of a model name: the pass starts, and its first request stops it.
        pytest.param(
            [*_SET_UP, "--model", "writer=test/echo-typo"], None, "no price", id="rejected-model"
        ),
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
        # Typer colours its usage errors under CI, which splits the option name with styles.
        assert "--ceiling-usd" in re.sub(r"\x1b\[[0-9;]*m", "", result.stderr)
    else:
        # One line says why; a pass that started first writes only its own progress lines before it.
        *progress, last = result.stderr.strip().splitlines()
        assert reason in last
        assert all(line.startswith("echo: ") for line in progress), result.stderr


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

    code, out = seqforge(review_clone, "pass", "start", "stored-echo", "--ceiling-usd", "1")

    assert code == 0
    assert out["store_build"] == "b1"
    asked = sorted(x.form["messages"][-1]["content"] for x in echo_gateway.record if x.form)
    assert asked == ["alpha", "beta"]

    # The build is kept with the pass, and status reads it back with no store to open.
    def no_store(root: Any = None) -> Any:
        raise AssertionError("pass status opened the store")

    monkeypatch.setattr(labdata, "open_store", no_store)
    code, out = seqforge(review_clone, "pass", "status", "stored-echo")
    assert (code, out["passes"][0]["store_build"]) == (0, "b1")


# `seqforge pass status`: what a task holds, read back with no gateway.

#: How long the provider takes to answer each text, in seconds on the fake gateway's clock.
ECHO_SECONDS = {"alpha": 1.0, "beta": 2.0, "gamma": 3.0, "delta": 4.0}


@pytest.fixture
def timed_gateway(monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    """The echo kind's fake gateway, whose provider takes :data:`ECHO_SECONDS` to answer."""

    def writer(form: dict[str, Any]) -> str:
        fake.clock.sleep(ECHO_SECONDS[form["messages"][-1]["content"]])
        return echo_writer(form)

    fake = _serve(monkeypatch, writer)
    return fake


def _spread(total: float, median: float, p90: float) -> dict[str, Any]:
    return {
        "total": pytest.approx(total),
        "median": pytest.approx(median),
        "p90": pytest.approx(p90),
    }


def test_status_counts_each_units_ending_and_each_passs_cost_and_time_per_unit(
    echo_kind: Any, timed_gateway: FakeGateway, review_clone: Path, tmp_path: Path
) -> None:
    # Output costs a tenth of a cent a character of the reply, so each reply's price is its length:
    # {"word": "ALPHA"} is 17 characters, $0.017. One unit at a time, so each unit's seconds are its
    # own call's.
    one = ("--units-at-once", "1")
    assert seqforge(review_clone, "pass", "start", "echo", "--ceiling-usd", "1", *one)[0] == 0
    # A person drops delta, one of the weak model's two wrong answers.
    Reviews(review_clone / "reviews").save(
        echo_unit_kind(ECHO_TEXTS),
        "u4",
        who="a reviewer",
        inputs_tag="text=delta",
        drop=True,
        note="not a word",
    )
    strong = f"writer={ECHO_STRONG.provider}/{ECHO_STRONG.name},max-output-tokens=50"
    step_up = ("--only", "unsettled,refused", "--model", strong, "--label", "step up")
    assert (
        seqforge(review_clone, "pass", "start", "echo", "--ceiling-usd", "1", *step_up, *one)[0]
        == 0
    )
    # u9 has no text, so it is held; u2 replays from the cache at no cost; u1 pays; u4 meets the
    # ceiling, since each call holds back five cents against it.
    keys = tmp_path / "keys.txt"
    keys.write_text("u9\nu2\nu1\nu4\n")
    code, _ = seqforge(
        review_clone,
        "pass",
        "start",
        "echo",
        "--ceiling-usd",
        "0.06",
        "--keys",
        str(keys),
        "--model",
        strong,
        *one,
    )
    assert code == 3

    code, out = seqforge(review_clone, "pass", "status", "echo")

    assert code == 0, out
    assert out["units"] == 5
    # u4 counts twice: its current ending is the person's drop, and its latest outcome is unfinished.
    assert out["counts"] == {
        "settled": 3,
        "unsettled": 0,
        "refused": 0,
        "dropped": 1,
        "unfinished": 1,
        "held": 1,
    }
    first, second, third = out["passes"]
    assert (first["pass_id"], first["label"], first["store_build"]) == (1, "echo", None)
    assert first["models"]["writer"]["name"] == ECHO_WEAK.name
    assert first["ceiling_usd"] == 1
    assert first["counts"] == {
        "settled": 1,
        "unsettled": 2,
        "refused": 1,
        "unfinished": 0,
        "held": 0,
    }
    assert first["spent_usd"] == pytest.approx(0.062)
    # alpha, beta, gamma, delta: 17, 16, 12 and 17 characters, 1 to 4 seconds, one request each.
    assert first["per_unit"] == {
        "units": 4,
        "spent_usd": _spread(0.062, 0.0165, 0.017),
        "first_cost_usd": _spread(0.062, 0.0165, 0.017),
        "requests": {"total": 4, "median": 1, "p90": 1},
        "seconds": _spread(10, 2.5, 4),
    }
    assert (second["label"], second["models"]["writer"]["name"]) == ("step up", ECHO_STRONG.name)
    assert second["per_unit"]["seconds"] == _spread(5, 2.5, 3)
    assert third["stopped"] == "ceiling_reached"
    assert third["counts"] == {
        "settled": 2,
        "unsettled": 0,
        "refused": 0,
        "unfinished": 1,
        "held": 1,
    }
    # The held unit called no model, so it is not among the units the pass ran. u2's answer came
    # from the cache: nothing paid now, $0.016 when first paid.
    assert third["per_unit"]["units"] == 3
    assert third["per_unit"]["spent_usd"] == _spread(0.017, 0, 0.017)
    assert third["per_unit"]["first_cost_usd"] == _spread(0.033, 0.016, 0.017)


def _gateway_refusing_connections(monkeypatch: pytest.MonkeyPatch, fake: FakeGateway) -> None:
    fake.refuse_connections()


def _gateway_address_unset(monkeypatch: pytest.MonkeyPatch, fake: FakeGateway) -> None:
    _no_gateway_address(monkeypatch)


@pytest.mark.parametrize("gateway_down", [_gateway_refusing_connections, _gateway_address_unset])
def test_status_reads_a_task_with_the_gateway_down(
    gateway_down: Callable[[pytest.MonkeyPatch, FakeGateway], None],
    echo_kind: Any,
    echo_gateway: FakeGateway,
    review_clone: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, ran = seqforge(review_clone, *PASS_START)
    assert code == 0
    gateway_down(monkeypatch, echo_gateway)
    sent = len(echo_gateway.record)

    code, out = seqforge(review_clone, "pass", "status", "echo")

    assert code == 0, out
    assert out["counts"] == {
        "settled": 1,
        "unsettled": 2,
        "refused": 1,
        "dropped": 0,
        "unfinished": 0,
        "held": 0,
    }
    assert [p["spent_usd"] for p in out["passes"]] == [ran["spent_usd"]]
    assert len(echo_gateway.record) == sent


#: Each command that reads the echo kind's task.
_READS_TASK = [
    pytest.param(["pass", "status", "echo"], id="pass-status"),
    pytest.param(["review", "file", "echo"], id="review-file"),
]


@pytest.mark.parametrize("argv", [pytest.param(list(PASS_START), id="pass-start"), *_READS_TASK])
def test_a_task_another_process_holds_exits_3_naming_the_holder(
    argv: list[str],
    echo_kind: Any,
    echo_gateway: FakeGateway,
    review_clone: Path,
    github: FakeGitHub,
) -> None:
    assert seqforge(review_clone, *PASS_START)[0] == 0
    sent = len(echo_gateway.record)
    with Task(TASK, gateway=echo_gateway.client(), reviews=review_clone / "reviews"):
        code, out = seqforge(review_clone, *argv)

    assert code == 3
    assert out["stopped"] == "task_in_use"
    assert f"process {os.getpid()} on {socket.gethostname()}" in out["message"]
    assert len(echo_gateway.record) == sent
    assert github.filed == []


@pytest.mark.parametrize("argv", _READS_TASK)
def test_a_command_that_reads_a_task_refuses_a_folder_with_none_and_makes_none(
    argv: list[str], echo_kind: Any, review_clone: Path, github: FakeGitHub
) -> None:
    result = runner.invoke(app, [*argv, "--reviews", str(review_clone)])

    assert result.exit_code == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert f"no task in {TASK}" in result.stderr
    assert not TASK.exists()


def tick(body: str, label: str) -> str:
    """Tick the box a person sees as ``label``, as GitHub does: ``[ ]`` becomes ``[x]`` on its line."""
    lines = body.splitlines()
    (at,) = [n for n, line in enumerate(lines) if line.startswith("- [ ] ") and label in line]
    lines[at] = "- [x] " + lines[at].removeprefix("- [ ] ")
    return "\n".join(lines)


def test_review_file_files_the_queue_in_the_kinds_priority_labelled_and_marked(
    echo_kind: Any, echo_gateway: FakeGateway, review_clone: Path, github: FakeGitHub
) -> None:
    code, _ = seqforge(review_clone, *PASS_START)
    assert code == 0

    code, out = seqforge(review_clone, "review", "file", "echo", "--count", "2")

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
    seqforge(review_clone, *PASS_START)
    code, out = seqforge(review_clone, "review", "file", "echo")
    assert code == 0
    assert (len(out["filed"]), out["unfiled"]) == (3, 0)
    github.close(1, "completed")  # a closed issue counts as filed as much as an open one

    code, out = seqforge(review_clone, "review", "file", "echo")

    assert code == 0
    assert (out["filed"], out["unfiled"]) == ([], 0)
    assert len(github.filed) == 3

    # Beta's text changes, so the next pass answers it for new inputs: it is filed again.
    monkeypatch.setitem(ECHO_TEXTS, "u2", "bravo")
    seqforge(review_clone, *PASS_START)

    code, out = seqforge(review_clone, "review", "file", "echo")

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
    seqforge(review_clone, *PASS_START)

    code, out = seqforge(review_clone, "review", "file", "echo")

    assert code == 0, out
    made = {name: github.label_table[name] for name in github.label_table.keys() - before.keys()}
    assert sorted(made) == ["check:answered", "check:in_capitals"]
    assert all(description for _, description in made.values())
    assert {name: github.label_table[name] for name in before} == before


#: Git as the tests run it: no configuration of the machine's own, and the same person everywhere.
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "a reviewer",
    "GIT_AUTHOR_EMAIL": "reviewer@example.org",
    "GIT_COMMITTER_NAME": "a reviewer",
    "GIT_COMMITTER_EMAIL": "reviewer@example.org",
}
#: The review repo's remote, beside the clone: ``origin`` names it by this relative path, so a copy
#: of the pair works wherever it lands.
_REMOTE = "review-remote.git"


def git(where: Path, *argv: str) -> str:
    """Run git in ``where`` and return its output; a failure fails the test."""
    done = subprocess.run(
        ["git", "-C", str(where), *argv],
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.fixture(scope="session")
def _review_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A folder holding the review repo's remote, a bare git repo, and a clone of it, made once.

    Its ``reviews/`` folder joins JSON Lines files on a merge, as the real repo's does.
    """
    made = tmp_path_factory.mktemp("review-repo")
    git(made, "init", "--quiet", "--bare", "--initial-branch=main", _REMOTE)
    clone = made / "review-clone"
    (clone / "reviews").mkdir(parents=True)
    git(clone, "init", "--quiet", "--initial-branch=main")
    (clone / "reviews" / ".gitattributes").write_text("*.jsonl merge=union\n")
    git(clone, "add", "reviews/.gitattributes")
    git(clone, "commit", "--quiet", "-m", "Set up the review repo")
    git(clone, "remote", "add", "origin", f"../{_REMOTE}")
    git(clone, "push", "--quiet", "--set-upstream", "origin", "main")
    return made


@pytest.fixture
def review_remote(
    _review_repo: Path, review_clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """The review repo's remote, a local bare git repo, with ``review_clone`` a clone of it."""
    for name, value in _GIT_ENV.items():
        monkeypatch.setenv(name, value)
    shutil.rmtree(review_clone)
    shutil.copytree(_review_repo / "review-clone", review_clone)
    return Path(shutil.copytree(_review_repo / _REMOTE, tmp_path / _REMOTE))


def answer(github: FakeGitHub, key: str, *ticks: str, reason: str = "completed") -> int:
    """A person ticks the boxes labelled ``ticks`` on ``key``'s issue and closes it as ``reason``.

    Returns the issue's number. The person is ``octo-reviewer`` on GitHub.
    """
    (number,) = [
        n
        for n, issue in enumerate(github.filed, 1)
        if (m := read_marker(issue.body)) and m.key == key
    ]
    issue = github.filed[number - 1]
    body = issue.body
    for label in ticks:
        body = tick(body, label)
    github.filed[number - 1] = dataclasses.replace(issue, body=body)
    github.close(number, reason, by="octo-reviewer")
    return number


def test_review_read_stores_a_completed_issue_once_while_a_pass_holds_the_task(
    echo_kind: Any,
    echo_gateway: FakeGateway,
    review_remote: Path,
    review_clone: Path,
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Delta's text reads the same backwards, so the weak model's answer is right but for its case.
    monkeypatch.setitem(ECHO_TEXTS, "u4", "level")
    seqforge(review_clone, *PASS_START)
    seqforge(review_clone, "review", "file", "echo")
    number = answer(github, "u4", "Pass 1's answer", "Write it in capitals")
    (review_clone / "notes.txt").write_text("the reviewer's own file, not the command's\n")

    kind = echo_unit_kind(ECHO_TEXTS)
    with Task(TASK, gateway=echo_gateway.client(), reviews=review_clone / "reviews"):
        code, out = seqforge(review_clone, "review", "read", "echo")

    assert code == 0, out
    url = github.filed[number - 1].url
    assert out["answered"] == [
        {"key": "u4", "url": url, "who": "octo-reviewer", "accepted_pass": 1}
    ]
    assert (out["dropped"], out["reopened"], out["inputs_changed"]) == ([], [], [])
    assert out["pushed"] is True
    (review,) = Reviews(review_clone / "reviews").read(kind)
    assert (review.key, review.who, review.accepted_pass) == ("u4", "octo-reviewer", 1)
    assert (review.inputs_tag, review.answer) == ("text=level", {"word": "LEVEL"})
    with Task(TASK, gateway=echo_gateway.client(), reviews=review_clone / "reviews") as task:
        assert task.keys(kind, endings=("settled",)) == ["u1", "u4"]
    # The reviews file alone is committed, and pushed to the review repo.
    pushed = git(review_remote, "show", "main:reviews/echo.jsonl")
    assert pushed == (review_clone / "reviews" / "echo.jsonl").read_text()
    assert git(review_clone, "status", "--porcelain") == "?? notes.txt\n"

    code, out = seqforge(review_clone, "review", "read", "echo")

    assert code == 0, out
    assert (out["answered"], out["pushed"]) == ([], None)
    assert len(Reviews(review_clone / "reviews").read(kind)) == 1


def test_review_read_stores_an_issue_closed_as_not_planned_as_a_drop_with_its_ticked_reasons(
    echo_kind: Any,
    echo_gateway: FakeGateway,
    review_remote: Path,
    review_clone: Path,
    github: FakeGitHub,
) -> None:
    seqforge(review_clone, *PASS_START)
    seqforge(review_clone, "review", "file", "echo")
    number = answer(github, "u3", "not a word", "other", reason="not_planned")

    code, out = seqforge(review_clone, "review", "read", "echo")

    assert code == 0, out
    url = github.filed[number - 1].url
    assert out["dropped"] == [
        {"key": "u3", "url": url, "who": "octo-reviewer", "note": "not a word; other"}
    ]
    assert (out["answered"], out["reopened"], out["pushed"]) == ([], [], True)
    kind = echo_unit_kind(ECHO_TEXTS)
    (review,) = Reviews(review_clone / "reviews").read(kind)
    assert (review.key, review.ending, review.note) == ("u3", "dropped", "not a word; other")
    assert (review.who, review.inputs_tag) == ("octo-reviewer", "text=gamma")
    with Task(TASK, gateway=echo_gateway.client(), reviews=review_clone / "reviews") as task:
        assert task.keys(kind, endings=("dropped",)) == ["u3"]


def test_review_read_stores_nothing_it_cannot_and_reopens_only_what_a_person_can_fix(
    echo_kind: Any,
    echo_gateway: FakeGateway,
    review_remote: Path,
    review_clone: Path,
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seqforge(review_clone, *PASS_START)
    seqforge(review_clone, "review", "file", "echo")
    beta = answer(github, "u2", "Pass 1's answer")  # the weak model's ateb: it fails in_capitals
    gamma = answer(github, "u3")  # closed with nothing ticked
    delta = answer(github, "u4", "The text, capitalised")
    monkeypatch.setitem(ECHO_TEXTS, "u4", "delta, revised")  # its inputs change after filing

    code, out = seqforge(review_clone, "review", "read", "echo")

    assert code == 4, out
    reasons = {entry["key"]: entry["reason"] for entry in out["reopened"]}
    assert reasons == {
        "u2": "The answer fails the kind's checks: in_capitals: 'ateb' is not 'BETA'.",
        "u3": "Tick a starting answer.",
    }
    for number, key in ((beta, "u2"), (gamma, "u3")):
        assert github.filed[number - 1].state == "open"
        (comment,) = github.comments[number]
        assert reasons[key] in comment
    # Delta's answer was for inputs it no longer has. No person can fix that, so its issue stays
    # closed, says why, and is marked so that reading back passes over it from then on.
    assert out["inputs_changed"] == [{"key": "u4", "url": github.filed[delta - 1].url}]
    assert github.filed[delta - 1].state == "closed"
    assert "inputs-changed" in github.filed[delta - 1].labels
    (comment,) = github.comments[delta]
    assert "from `text=delta` to `text=delta, revised`" in comment
    assert (out["answered"], out["dropped"], out["pushed"]) == ([], [], None)
    assert not (review_clone / "reviews" / "echo.jsonl").exists()

    code, out = seqforge(review_clone, "review", "read", "echo")

    assert code == 0, out
    assert (out["reopened"], out["inputs_changed"]) == ([], [])
    assert len(github.comments[delta]) == 1


def pushed_keys(remote: Path) -> list[str]:
    """The keys of the echo kind's reviews the review repo holds, in key order."""
    lines = git(remote, "show", "main:reviews/echo.jsonl").splitlines()
    return sorted(json.loads(line)["key"] for line in lines)


def test_review_read_whose_push_is_behind_pulls_and_keeps_both_clones_reviews(
    echo_kind: Any,
    echo_gateway: FakeGateway,
    review_remote: Path,
    review_clone: Path,
    github: FakeGitHub,
    tmp_path: Path,
) -> None:
    seqforge(review_clone, *PASS_START)
    seqforge(review_clone, "review", "file", "echo")
    # Another person's clone pushes a review of alpha first, so this clone's push is behind.
    other = Path(shutil.copytree(review_clone, tmp_path / "other-clone"))
    Reviews(other / "reviews").save(
        echo_unit_kind(ECHO_TEXTS),
        "u1",
        who="someone else",
        inputs_tag="text=alpha",
        answer={"word": "ALPHA"},
    )
    git(other, "add", "reviews/echo.jsonl")
    git(other, "commit", "--quiet", "-m", "A review from another clone")
    git(other, "push", "--quiet")
    answer(github, "u2", "The text, capitalised")

    code, out = seqforge(review_clone, "review", "read", "echo")

    assert code == 0, out
    assert ([a["key"] for a in out["answered"]], out["pushed"]) == (["u2"], True)
    assert pushed_keys(review_remote) == ["u1", "u2"]


def test_review_read_whose_push_fails_exits_3_with_the_reviews_committed_for_its_rerun(
    echo_kind: Any,
    echo_gateway: FakeGateway,
    review_remote: Path,
    review_clone: Path,
    github: FakeGitHub,
    tmp_path: Path,
) -> None:
    seqforge(review_clone, *PASS_START)
    seqforge(review_clone, "review", "file", "echo")
    answer(github, "u3", "not a word", reason="not_planned")
    review_remote.rename(tmp_path / "unreachable.git")  # the review repo cannot be reached

    code, out = seqforge(review_clone, "review", "read", "echo")

    assert code == 3, out
    assert (out["stopped"], out["pushed"]) == ("git_failed", False)
    assert [d["key"] for d in out["dropped"]] == ["u3"]
    assert git(review_clone, "status", "--porcelain") == ""  # committed, waiting in the clone

    (tmp_path / "unreachable.git").rename(review_remote)
    code, out = seqforge(review_clone, "review", "read", "echo")

    assert code == 0, out
    assert (out["dropped"], out["pushed"]) == ([], True)
    assert pushed_keys(review_remote) == ["u3"]
