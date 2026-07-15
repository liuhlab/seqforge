"""Smoke tests for the ``seqforge`` CLI (schema export is the first live verb)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from typer.testing import CliRunner

from seqforge import __version__, kb
from seqforge.cli import app

runner = CliRunner()


def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    with gzip.open(path, "wt") as fh:
        for i, s in enumerate(seqs):
            fh.write(f"@SIM:{i}\n{s}\n+\n{'I' * len(s)}\n")


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


def test_io_onlist_list_shows_known_lists() -> None:
    result = runner.invoke(app, ["io", "onlist", "list"])
    assert result.exit_code == 0
    names = {o["name"] for o in json.loads(result.stdout)["onlists"]}
    assert "3M-february-2018" in names


def test_io_peek_not_implemented_exits_1() -> None:
    result = runner.invoke(app, ["io", "peek", "s3://bucket/reads.fastq.gz"])
    assert result.exit_code == 1


def test_resolve_score_cli_decides_v3(tmp_path: Path) -> None:
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=800, seed=0)
    f1 = tmp_path / "R1.fastq.gz"
    f2 = tmp_path / "R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])
    result = runner.invoke(
        app, ["resolve", "score", str(f1), str(f2), "-C", str(tmp_path), "--no-cache"]
    )
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    # geometry-only path (default registry materializes no onlist) still decides v3 at rung 2
    assert doc["candidates"][0]["technology"] == "10x-3p-gex-v3"
    assert doc["rung_reached"] == 2
