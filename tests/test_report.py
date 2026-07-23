"""Tests for ``seqforge report`` — the self-contained HTML decision report.

Everything runs offline against a workspace built by the real ``run`` verb on KB-generated bulk reads
(no network, no provider, no onlist). The load-bearing properties: the page is genuinely
self-contained (no external reference can regress in), it stays small, it is byte-deterministic, and
the collector degrades honestly when a piece is missing rather than crashing or inventing a verdict.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from seqforge import kb
from seqforge.cli import app
from seqforge.report import collect_report, render_html
from seqforge.report.flow import _san, flow_mermaid

runner = CliRunner()


def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    with gzip.open(path, "wt") as fh:
        for i, s in enumerate(seqs):
            fh.write(f"@SIM:{i}\n{s}\n+\n{'I' * len(s)}\n")


def _build_bulk_workspace(tmp_path: Path) -> Path:
    """A fully compiled workspace via the real ``run`` verb: manifest + processing + Snakefile + caches.

    Bulk needs no onlist (the default registry ships none), so this is the branch CI can run headless.
    The self-consistent run means the manifest shas and the persisted candidate/matrix shas agree, so
    the report's scan-join finds the evidence matrix.
    """
    spec = kb.load_spec("bulk-rnaseq-pe")
    reads = kb.generate_reads(spec, n=600, seed=0)
    f1, f2 = tmp_path / "s_R1.fastq.gz", tmp_path / "s_R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])
    result = runner.invoke(
        app,
        [
            "run", str(f1), str(f2),
            "--organism", "559292",
            "--assembly", "sacCer3",
            "--annotation", "ensembl",
            "--no-llm",
            "--fastq-dir", str(tmp_path),
            "-C", str(tmp_path),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.stdout
    return tmp_path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return _build_bulk_workspace(tmp_path)


def test_report_verb_writes_a_self_contained_html_page(workspace: Path) -> None:
    result = runner.invoke(app, ["report", "-C", str(workspace), "--no-timestamp"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    out = Path(payload["report"])
    assert out == workspace / "seqforge" / "report.html"
    assert out.is_file()
    assert payload["assays"] == 1
    assert payload["conclusion"][0]["kind"] == "compiled"

    html = out.read_text()
    assert html.startswith("<!doctype html>")
    assert "</html>" in html
    for tab in ("Overview", "Flow", "Samples", "Evidence", "Pipeline"):
        assert f">{tab}</button>" in html


def test_run_emits_the_report_as_a_best_effort_stage(workspace: Path) -> None:
    """``run`` drops the report on its own; it is a stage, and a compiled dataset says so on the page."""
    assert (workspace / "seqforge" / "report.html").is_file()


def test_report_makes_no_external_network_reference(workspace: Path) -> None:
    """The whole point is that it opens offline. No src/href/@import may point off-host; an SVG
    ``xmlns`` is a namespace, not a fetch, so it is explicitly allowed."""
    html = render_html(collect_report(workspace))
    # The load-bearing check: nothing FETCHABLE points off-host. A URL sitting inside an inlined
    # <script> as a string literal (mermaid bundles marked, whose banner names its homepage) is data,
    # not a request — so we constrain src/href/@import specifically, not every http(s) substring.
    offsite = re.findall(r'(?:src|href)\s*=\s*"(?:https?:)?//[^"]+"', html)
    assert not offsite, f"external references leaked in: {offsite[:3]}"
    assert "@importurl(http" not in html.replace(" ", "").replace("'", '"').lower()
    assert "cdn.jsdelivr" not in html and "unpkg" not in html, "a CDN link regressed in"
    # and the mermaid engine is genuinely inlined, not linked
    assert "globalThis.mermaid" in html


def test_report_stays_under_the_size_budget(workspace: Path) -> None:
    html = render_html(collect_report(workspace))
    assert len(html.encode()) < 6_000_000, "report bloated past 6 MB (mermaid embedded twice?)"


def test_report_render_is_byte_deterministic(workspace: Path) -> None:
    a = render_html(collect_report(workspace, generated_at=None))
    b = render_html(collect_report(workspace, generated_at=None))
    assert a == b


def test_report_locates_the_persisted_evidence_matrix(workspace: Path) -> None:
    """A self-consistent run wrote candidates + matrices; the scan-join must find them and mark a
    winner, and the rendered matrix must reach the page."""
    assay = collect_report(workspace).assays[0]
    assert assay.matrices, "the persisted matrix should have been located by the scan-join"
    assert any(m.is_winner for m in assay.matrices)
    assert 'class="matrix"' in render_html(collect_report(workspace))


def test_report_degrades_when_the_matrix_cache_is_absent(workspace: Path) -> None:
    """Delete the sidecar: no crash, no invented matrix — the chemistry decision (in the manifest)
    still renders, and the page says the matrix was not persisted."""
    shutil.rmtree(workspace / "seqforge" / "cache" / "matrices")
    report = collect_report(workspace)
    assert report.assays[0].matrices == []
    html = render_html(report)
    assert "Chemistry decision" in html
    assert "not persisted" in html


def test_report_is_ir_ready_without_a_composed_pipeline(workspace: Path) -> None:
    """Remove the composed pipeline: the verdict falls back to ir-ready, never a manufactured refusal."""
    shutil.rmtree(workspace / "seqforge" / "pipeline")
    assay = collect_report(workspace).assays[0]
    assert assay.conclusion.kind == "ir_ready"
    assert assay.conclusion.exit_code == 0
    assert assay.plan is not None and assay.plan.snakefile_rel is None


def test_report_handles_a_multi_assay_layout(workspace: Path) -> None:
    """Two ``<assay>/manifest.yaml`` render as two assays with a switcher in the shell."""
    sf = workspace / "seqforge"
    manifest = (sf / "manifest.yaml").read_text()
    for name in ("assay-a", "assay-b"):
        (sf / name).mkdir()
        (sf / name / "manifest.yaml").write_text(manifest)
    (sf / "manifest.yaml").unlink()

    report = collect_report(workspace)
    assert report.is_multi_assay and len(report.assays) == 2
    assert 'id="assay-select"' in render_html(report)


def test_collect_raises_only_when_there_is_nothing_to_report(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        collect_report(tmp_path)


def test_flow_mermaid_carries_the_real_decision(workspace: Path) -> None:
    assay = collect_report(workspace).assays[0]
    src = flow_mermaid(assay)
    assert src.startswith("flowchart TD")
    assert "classDef artifact" in src
    assert assay.chemistry.value[0] in src  # the real chemistry id, not a placeholder
    assert "Compiled" in src  # compiled -> the deliverable terminal, not blocked/needs-a-human


def test_mermaid_label_sanitizer_strips_the_characters_that_break_a_label() -> None:
    assert _san('a"b#c\nd') == "abc d"
