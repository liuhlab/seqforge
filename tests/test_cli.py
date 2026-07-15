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


def test_manifest_fill_validate_hash_compose_spine(tmp_path: Path) -> None:
    """The whole deterministic spine, driven through the real CLI: probe->resolve->manifest->compose.

    Uses the no-barcode bulk branch so it needs no onlist: the default registry deliberately
    materializes no real whitelist (they are license-restricted), which is exactly why the 10x path
    refuses to compose until one is registered.
    """
    spec = kb.load_spec("bulk-rnaseq-pe")
    reads = kb.generate_reads(spec, n=600, seed=0)
    f1 = tmp_path / "s_R1.fastq.gz"
    f2 = tmp_path / "s_R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])

    filled = runner.invoke(
        app,
        [
            "manifest",
            "fill",
            str(f1),
            str(f2),
            "--organism",
            "559292",
            "--assembly",
            "sacCer3",
            "--annotation",
            "ensembl",
            "-C",
            str(tmp_path),
        ],
    )
    assert filled.exit_code == 0, filled.stdout
    assert json.loads(filled.stdout)["report"]["ok"] is True
    # R7: manifest.yaml exists only because validate came back clean
    manifest_path = tmp_path / ".seqforge" / "manifest.yaml"
    assert manifest_path.is_file()
    assert not (tmp_path / ".seqforge" / "manifest.draft.yaml").exists()

    validated = runner.invoke(app, ["manifest", "validate", str(manifest_path)])
    assert validated.exit_code == 0
    assert json.loads(validated.stdout)["ok"] is True

    hashed = runner.invoke(app, ["manifest", "hash", str(manifest_path)])
    assert hashed.exit_code == 0
    assert json.loads(hashed.stdout)["matches"] is True

    composed = runner.invoke(app, ["compose", str(manifest_path), "-C", str(tmp_path)])
    assert composed.exit_code == 0, composed.stdout
    doc = json.loads(composed.stdout)
    assert doc["modules"][0]["name"] == "map/star"
    assert doc["gate"]["params"] == "pass"
    assert doc["gate"]["e2e"] == "skip"  # honest: the count-matrix run needs STAR + liulab-genome
    assert (tmp_path / ".seqforge" / "pipeline" / "config.yaml").is_file()
    assert (tmp_path / ".seqforge" / "pipeline" / "units.tsv").is_file()


def test_compose_refuses_invalid_manifest(tmp_path: Path) -> None:
    bad = tmp_path / "nope.yaml"
    bad.write_text("library: {}\n")
    result = runner.invoke(app, ["compose", str(bad), "-C", str(tmp_path)])
    assert result.exit_code == 2  # unreadable/invalid manifest is a usage error, not a silent pass


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
