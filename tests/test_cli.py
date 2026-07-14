"""Smoke tests for the ``seqforge`` CLI (schema export is the first live verb)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from seqforge import __version__
from seqforge.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_schema_export_manifest_is_valid_json() -> None:
    result = runner.invoke(app, ["schema", "export", "Manifest"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["title"] == "Manifest"
    assert "$defs" in doc


def test_schema_export_unknown_model_exits_2() -> None:
    result = runner.invoke(app, ["schema", "export", "NopeModel"])
    assert result.exit_code == 2


def test_schema_export_all_covers_every_model() -> None:
    result = runner.invoke(app, ["schema", "export", "--all"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert "Manifest" in doc and "Observation" in doc


def test_schema_list_lists_manifest() -> None:
    result = runner.invoke(app, ["schema", "list"])
    assert result.exit_code == 0
    assert "Manifest" in result.stdout


def test_kb_list_shows_10x() -> None:
    result = runner.invoke(app, ["kb", "list"])
    assert result.exit_code == 0
    assert "10x-3p-gex-v3" in result.stdout


def test_kb_show_unknown_exits_2() -> None:
    result = runner.invoke(app, ["kb", "show", "nope-tech"])
    assert result.exit_code == 2


def test_kb_lint_is_clean() -> None:
    result = runner.invoke(app, ["kb", "lint"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["ok"] is True


def test_kb_roundtrip_passes() -> None:
    result = runner.invoke(app, ["kb", "roundtrip", "10x-3p-gex-v3"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["passed"] is True
