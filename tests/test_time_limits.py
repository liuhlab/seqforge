"""The suite budget, the per-test timeout and the slow lane, through pytest itself.

Each test runs pytest over a scratch tree carrying this repo's own `conftest.py` and
`[tool.pytest.ini_options]`, with the two limits shrunk so a short sleep crosses them.
"""

from __future__ import annotations

import json
import shlex
import tomllib
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

REPO = Path(__file__).resolve().parents[1]
MANIFEST = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

SLOW_TEST = """
import time

import pytest


@pytest.mark.slow
def test_takes_its_time():
    time.sleep(0.8)
"""


def scratch(
    pytester: pytest.Pytester,
    tests: str,
    *,
    timeout: float,
    budget: float,
    where: str = "tests/test_scratch.py",
) -> None:
    """A tree with this repo's pytest configuration, these limits, and one test file."""
    ini = dict(MANIFEST["tool"]["pytest"]["ini_options"], timeout=timeout, suite_budget=budget)
    lines = "".join(f"{key} = {json.dumps(value)}\n" for key, value in ini.items())
    pytester.makepyprojecttoml(f"[tool.pytest.ini_options]\n{lines}")
    pytester.makeconftest((REPO / "conftest.py").read_text(encoding="utf-8"))
    path = pytester.path / where
    path.parent.mkdir(parents=True)
    path.write_text(tests, encoding="utf-8")


def slow_lane() -> list[str]:
    """The arguments `pixi run test-slow` hands pytest."""
    command = shlex.split(MANIFEST["tool"]["pixi"]["tasks"]["test-slow"])
    assert command[0] == "pytest"
    return command[1:]


def test_a_suite_inside_its_budget_passes_and_says_so(pytester: pytest.Pytester) -> None:
    scratch(pytester, "def test_quick():\n    pass\n", timeout=1, budget=30)
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(["suite time: * s of its 30 s budget"])


# The second place is a directory named `slow`, which an unmarked test is not exempted by.
@pytest.mark.parametrize("where", ["tests/test_scratch.py", "tests/slow/test_scratch.py"])
def test_a_suite_past_its_budget_fails_the_run(pytester: pytest.Pytester, where: str) -> None:
    tests = "import time\n\n\ndef test_sleeps():\n    time.sleep(0.3)\n"
    scratch(pytester, tests, timeout=1, budget=0.1, where=where)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["suite time: * s, over its 0.1 s budget*"])


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_a_test_past_the_timeout_fails_in_any_phase(pytester: pytest.Pytester, phase: str) -> None:
    tests = f"""
import time

import pytest


@pytest.fixture
def phases():
    if "{phase}" == "setup":
        time.sleep(0.5)
    yield
    if "{phase}" == "teardown":
        time.sleep(0.5)


def test_sleeps(phases):
    if "{phase}" == "call":
        time.sleep(0.5)
"""
    scratch(pytester, tests, timeout=0.1, budget=30)
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*Timeout*"])


def test_a_slow_test_is_deselected_by_default(pytester: pytest.Pytester) -> None:
    scratch(pytester, SLOW_TEST + "\n\ndef test_quick():\n    pass\n", timeout=0.2, budget=0.6)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1, deselected=1)
    assert result.ret == pytest.ExitCode.OK


def test_the_slow_lane_holds_a_slow_test_to_neither_limit(pytester: pytest.Pytester) -> None:
    scratch(pytester, SLOW_TEST, timeout=0.2, budget=0.6)
    result = pytester.runpytest(*slow_lane())
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.OK
