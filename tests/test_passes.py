"""`seqforge pass`, driven in-process through the CLI, with liulab-llms's fake gateway behind it.

A pass of a seqforge kind is the run engine's (liulab-llms's) work; what seqforge owns is the
command around it: which kind, which models, which keys, which task, and what a caller gets back --
one JSON object on stdout and an exit code it can act on. So every test here asserts only on that
JSON, the exit code and the fake gateway's record, which is part of liulab-llms's contract. None
reads the task's state file.
"""

from __future__ import annotations

import json
import os
import re
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from labllm import Reviews, Task
from labllm.testing import FakeGateway
from typer.testing import CliRunner

import passes_kit
from passes_kit import (
    ECHO_PRICES,
    ECHO_STRONG,
    ECHO_TEXTS,
    ECHO_WEAK,
    echo_unit_kind,
    echo_writer,
)
from seqforge.cli import app
from seqforge.passes import gateway
from seqforge.passes.kinds import KIND_MODULES

runner = CliRunner()

#: The echo kind's task folder, where a command run in the test's working directory makes it.
TASK = Path("seqforge", "passes", "echo")
#: The folder liulab-llms reads a task's reviews from. No seqforge command makes it.
REVIEWS = TASK / "reviews"
#: A plain pass of the echo kind, with a ceiling it never reaches.
PASS_START = ("pass", "start", "echo", "--ceiling-usd", "1")


def seqforge(*argv: str) -> tuple[int, dict[str, Any]]:
    """Run ``seqforge <argv>`` and return its exit code and its JSON."""
    result = runner.invoke(app, list(argv))
    assert result.stdout, result.stderr
    return result.exit_code, json.loads(result.stdout)


@pytest.fixture
def echo_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """The echo kind, listed in the kind table under ``echo`` for this test only."""
    monkeypatch.setitem(KIND_MODULES, "echo", passes_kit.__name__)


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


@pytest.fixture(autouse=True)
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty working directory, which a pass's task defaults under."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_pass_that_reaches_the_end_of_its_keys_exits_0_and_counts_every_ending(
    echo_kind: Any, echo_gateway: FakeGateway
) -> None:
    code, out = seqforge(*PASS_START)

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
    echo_kind: Any, echo_gateway: FakeGateway
) -> None:
    # Each call holds back five cents, so six cents lets the first unit through and stops the next.
    code, out = seqforge("pass", "start", "echo", "--ceiling-usd", "0.06", "--units-at-once", "1")

    assert code == 3
    assert out["stopped"] == "ceiling_reached"
    # The gateway's own words for the stop, not only the reason already in `stopped`.
    assert "over its ceiling of 0.06" in out["message"]
    assert out["counts"] == {"settled": 1, "unsettled": 0, "refused": 0, "unfinished": 3, "held": 0}

    sent = len(echo_gateway.record)
    code, out = seqforge("pass", "start", "echo", "--ceiling-usd", "1", "--units-at-once", "1")

    assert code == 0, out
    assert (out["stopped"], out["message"]) == (None, None)
    assert out["counts"] == {"settled": 1, "unsettled": 2, "refused": 1, "unfinished": 0, "held": 0}
    rerun = echo_gateway.record[sent:]
    assert {x.tag for x in rerun if x.cached} == {"u1"}
    assert all(x.cost_usd == 0 for x in rerun if x.cached)
    assert out["cache_hits"] == 1


def test_only_unsettled_and_refused_runs_those_units_on_a_stronger_model_and_skips_reviewed_ones(
    echo_kind: Any, echo_gateway: FakeGateway
) -> None:
    code, _ = seqforge(*PASS_START)
    assert code == 0
    # A person settles delta, one of the weak model's two wrong answers, with liulab-llms's own
    # save. The pass made no reviews folder: the save does.
    assert not REVIEWS.exists()
    Reviews(REVIEWS).save(
        echo_unit_kind(ECHO_TEXTS),
        "u4",
        who="a reviewer",
        inputs_tag="text=delta",
        answer={"word": "DELTA"},
    )

    strong = f"writer={ECHO_STRONG.provider}/{ECHO_STRONG.name},max-output-tokens=50"
    sent = len(echo_gateway.record)
    code, out = seqforge(
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
    echo_kind: Any, echo_gateway: FakeGateway, tmp_path: Path
) -> None:
    keys = tmp_path / "keys.txt"
    keys.write_text("u3\nu1\n")

    code, out = seqforge(
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
_SET_UP = ["echo", "--ceiling-usd", "1"]


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
        pytest.param(_SET_UP, _no_gateway_address, "LABLLM_GATEWAY", id="no-gateway"),
        pytest.param(
            _SET_UP, _state_file_of_another_layout, "not a task's state file", id="layout"
        ),
        pytest.param(["echo"], None, None, id="no-ceiling"),
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if arrange is not None:
        arrange(monkeypatch)

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
    echo_kind: Any, timed_gateway: FakeGateway, tmp_path: Path
) -> None:
    # Output costs a tenth of a cent a character of the reply, so each reply's price is its length:
    # {"word": "ALPHA"} is 17 characters, $0.017. One unit at a time, so each unit's seconds are its
    # own call's.
    one = ("--units-at-once", "1")
    assert seqforge("pass", "start", "echo", "--ceiling-usd", "1", *one)[0] == 0
    # A person drops delta, one of the weak model's two wrong answers.
    Reviews(REVIEWS).save(
        echo_unit_kind(ECHO_TEXTS),
        "u4",
        who="a reviewer",
        inputs_tag="text=delta",
        drop=True,
        note="not a word",
    )
    strong = f"writer={ECHO_STRONG.provider}/{ECHO_STRONG.name},max-output-tokens=50"
    step_up = ("--only", "unsettled,refused", "--model", strong, "--label", "step up")
    assert seqforge("pass", "start", "echo", "--ceiling-usd", "1", *step_up, *one)[0] == 0
    # u9 has no text, so it is held; u2 replays from the cache at no cost; u1 pays; u4 meets the
    # ceiling, since each call holds back five cents against it.
    keys = tmp_path / "keys.txt"
    keys.write_text("u9\nu2\nu1\nu4\n")
    code, _ = seqforge(
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

    code, out = seqforge("pass", "status", "echo")

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, ran = seqforge(*PASS_START)
    assert code == 0
    gateway_down(monkeypatch, echo_gateway)
    sent = len(echo_gateway.record)

    code, out = seqforge("pass", "status", "echo")

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
]


@pytest.mark.parametrize("argv", [pytest.param(list(PASS_START), id="pass-start"), *_READS_TASK])
def test_a_task_another_process_holds_exits_3_naming_the_holder(
    argv: list[str],
    echo_kind: Any,
    echo_gateway: FakeGateway,
) -> None:
    assert seqforge(*PASS_START)[0] == 0
    sent = len(echo_gateway.record)
    with Task(TASK, gateway=echo_gateway.client(), reviews=REVIEWS):
        code, out = seqforge(*argv)

    assert code == 3
    assert out["stopped"] == "task_in_use"
    assert f"process {os.getpid()} on {socket.gethostname()}" in out["message"]
    assert len(echo_gateway.record) == sent


@pytest.mark.parametrize("name", sorted(KIND_MODULES))
def test_every_kind_in_the_table_loads_under_its_listed_name(name: str) -> None:
    """The kind table is the one place a kind's name is written, and every command finds its kind
    there. A module moved or renamed without its row, or a kind that calls itself by another name,
    fails here and not when a pass starts: the command loads the kind and looks for its task under
    the kind's own name."""
    result = runner.invoke(app, ["pass", "status", name])

    assert result.exit_code == 2, result.stdout + result.stderr
    assert f"no task in {Path('seqforge', 'passes', name)}:" in result.stderr


@pytest.mark.parametrize("argv", _READS_TASK)
def test_a_command_that_reads_a_task_refuses_a_folder_with_none_and_makes_none(
    argv: list[str], echo_kind: Any
) -> None:
    result = runner.invoke(app, argv)

    assert result.exit_code == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert f"no task in {TASK}" in result.stderr
    assert not TASK.exists()
