"""Scratch 2."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from conftest import write_fastq_gz
from seqforge import kb
from seqforge.cli import app

runner = CliRunner()


def _bulk(d: Path, n: int = 600) -> tuple[Path, Path]:
    spec = kb.load_spec("bulk-rnaseq-pe")
    reads = kb.generate_reads(spec, n=n, seed=0)
    f1, f2 = d / "s_R1.fastq.gz", d / "s_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])
    return f1, f2


def test_1_run_twice_original(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    f1, f2 = _bulk(data)

    def hash_with(cpus: int, ws: Path) -> str:
        ws.mkdir()
        r = runner.invoke(
            app,
            ["run", str(f1), str(f2), "--organism", "559292", "--assembly", "sacCer3",
             "--annotation", "ensembl", "--no-llm", "--fastq-dir", str(data),
             "--cpus", str(cpus), "-C", str(ws)],
        )  # fmt: skip
        assert r.exit_code == 0, r.stdout
        return yaml.safe_load((ws / "seqforge" / "manifest.yaml").read_text())["provenance"][
            "dataset_hash"
        ]

    assert hash_with(1, tmp_path / "a") == hash_with(4, tmp_path / "b")


def test_2_fill_twice(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    f1, f2 = _bulk(data)

    def hash_with(cpus: int, ws: Path) -> str:
        ws.mkdir()
        r = runner.invoke(
            app,
            ["manifest", "fill", str(f1), str(f2), "--organism", "559292",
             "--cpus", str(cpus), "-C", str(ws)],
        )  # fmt: skip
        assert r.exit_code == 0, r.stdout
        return yaml.safe_load((ws / "seqforge" / "manifest.yaml").read_text())["provenance"][
            "dataset_hash"
        ]

    assert hash_with(1, tmp_path / "a") == hash_with(4, tmp_path / "b")


def test_3_six_run_cpus1(tmp_path: Path) -> None:
    spec = kb.load_spec("bulk-rnaseq-pe")
    paths: list[str] = []
    for i in range(6):
        reads = kb.generate_reads(spec, n=400, seed=i)
        d = tmp_path / "data" / f"SRX2428313{i}"
        d.mkdir(parents=True)
        for mate, role in (("1", "R1"), ("2", "R2")):
            p = d / f"SRR2871655{i + 3}_{mate}.fastq.gz"
            write_fastq_gz(p, reads[role])
            paths.append(str(p))
    r = runner.invoke(
        app, ["manifest", "fill", *paths, "--organism", "6239", "--cpus", "1", "-C", str(tmp_path)]
    )
    assert r.exit_code == 0, r.stdout


def test_4_six_run_default_cpus(tmp_path: Path) -> None:
    spec = kb.load_spec("bulk-rnaseq-pe")
    paths: list[str] = []
    for i in range(6):
        reads = kb.generate_reads(spec, n=400, seed=i)
        d = tmp_path / "data" / f"SRX2428313{i}"
        d.mkdir(parents=True)
        for mate, role in (("1", "R1"), ("2", "R2")):
            p = d / f"SRR2871655{i + 3}_{mate}.fastq.gz"
            write_fastq_gz(p, reads[role])
            paths.append(str(p))
    r = runner.invoke(app, ["manifest", "fill", *paths, "--organism", "6239", "-C", str(tmp_path)])
    assert r.exit_code == 0, r.stdout
