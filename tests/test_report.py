"""Tests for ``seqforge report`` — the self-contained HTML decision report.

Everything runs offline against a workspace built by the real ``run`` verb on KB-generated bulk reads
(no network, no provider, no onlist). The load-bearing properties: the page is genuinely
self-contained (no external reference can regress in), it stays small, it is byte-deterministic, and
the collector degrades honestly when a piece is missing rather than crashing or inventing a verdict.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import write_fastq_gz
from seqforge import kb
from seqforge.cli import app
from seqforge.report import collect_report, render_html
from seqforge.report.flow import flow_steps

runner = CliRunner()

#: Every test here reads one `seqforge run` (2.2s). xdist's default `load` scheduler spreads them
#: across workers and each worker rebuilds `_bulk_workspace` — 16 tests cost 4.05s of CPU on one
#: worker and 20.60s on eight, for identical proof. `xdist_group` pins the module to a single worker
#: under `--dist=loadgroup`, so the build happens once. Correct here because the run dominates the
#: tests: nothing below is slow enough to want its own core.
pytestmark = pytest.mark.xdist_group("report-workspace")


def _build_bulk_workspace(tmp_path: Path) -> Path:
    """A fully compiled workspace via the real ``run`` verb: manifest + processing + Snakefile + caches.

    Bulk needs no onlist (the default registry ships none), so this is the branch CI can run headless.
    The self-consistent run means the manifest shas and the persisted candidate/matrix shas agree, so
    the report's scan-join finds the evidence matrix.
    """
    spec = kb.load_spec("bulk-rnaseq-pe")
    reads = kb.generate_reads(spec, n=600, seed=0)
    f1, f2 = tmp_path / "s_R1.fastq.gz", tmp_path / "s_R2.fastq.gz"
    write_fastq_gz(f1, reads["R1"])
    write_fastq_gz(f2, reads["R2"])
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


@pytest.fixture(scope="module")
def _bulk_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The compiled workspace, built ONCE for this module.

    `_build_bulk_workspace` runs the whole `seqforge run` verb — 2.2s measured, and it was the top
    13 entries in `--durations` because every test below re-ran it to look at the same page.
    """
    return _build_bulk_workspace(tmp_path_factory.mktemp("report-bulk"))


@pytest.fixture
def workspace(_bulk_workspace: Path) -> Path:
    """The shared build, read-only. A test that changes the tree takes `own_workspace` instead."""
    return _bulk_workspace


@pytest.fixture
def own_workspace(_bulk_workspace: Path, tmp_path: Path) -> Path:
    """A private copy, for the tests that delete from or add to the workspace.

    Mutating the shared one would make this file order-dependent — the exact failure mode a workspace
    with an implicit resume cache turns into a test that passes for the wrong reason.
    """
    dst = tmp_path / "workspace"
    shutil.copytree(_bulk_workspace, dst)
    return dst


def test_report_verb_writes_a_self_contained_html_page(own_workspace: Path) -> None:
    workspace = own_workspace  # the verb WRITES report.html; it does not share a tree
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


def test_report_makes_no_external_network_reference(workspace: Path) -> None:
    """The whole point is that it opens offline. No src/href/@import may point off-host; a data: URI
    (an embedded artifact download) is inline bytes, not a fetch, so it is explicitly allowed."""
    html = render_html(collect_report(workspace))
    # The load-bearing check: nothing FETCHABLE points off-host. We constrain src/href/@import
    # specifically, not every http(s) substring (a URL inside an inlined <script> string is data).
    offsite = re.findall(r'(?:src|href)\s*=\s*"(?:https?:)?//[^"]+"', html)
    assert not offsite, f"external references leaked in: {offsite[:3]}"
    assert "@importurl(http" not in html.replace(" ", "").replace("'", '"').lower()
    assert "cdn.jsdelivr" not in html and "unpkg" not in html, "a CDN link regressed in"


def test_report_stays_under_the_size_budget(workspace: Path) -> None:
    """No third-party engine is inlined any more (the Flow tab is HTML cards), so a page is a few tens
    of KB. The old budget was 6 MB to accommodate Mermaid; a page over 500 KB now means real bloat."""
    html = render_html(collect_report(workspace))
    assert len(html.encode()) < 500_000, "report bloated past 500 KB (a heavy asset regressed in?)"


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


def test_samples_render_as_a_metadata_table(workspace: Path) -> None:
    """The per-sample card list is gone: samples are one table with an expandable detail row."""
    html = render_html(collect_report(workspace))
    assert 'class="samples"' in html  # the metadata table, not a card per sample
    assert "row-toggle" in html  # the expand control
    assert 'class="detail-row"' in html  # the files/quotes drawer


def test_sample_provenance_is_a_pinnable_popover_not_a_transient_tooltip() -> None:
    """A metadata cell carries its provenance as ``data-*`` on a keyboard-reachable button, so the
    script can pin a selectable, copyable popover. It must NOT fall back to a native ``title=`` a
    reader can neither select nor copy. Tested on the helper directly (the headless bulk fixture has
    no sample attributes, so it renders no such cells)."""
    from seqforge.report.model import AttributeView, EvidenceRef
    from seqforge.report.panels import _attr_cell

    attr = AttributeView(
        key="tissue",
        value="Motor neurons",
        basis="asserted",
        rung=2,
        evidence=[
            EvidenceRef(
                raw="assert-1", kind="assertion", quote="motor neurons", document="paper", page=3
            )
        ],
    )
    html = _attr_cell(attr)
    assert 'role="button"' in html and 'tabindex="0"' in html
    assert 'data-basis="' in html and 'data-quote="motor neurons"' in html
    assert 'data-source="paper p.3"' in html
    assert "title=" not in html  # no transient native tooltip on the value cell


def test_pipeline_artifacts_are_embedded_not_linked(workspace: Path) -> None:
    """Self-containment: the composed artifacts ride in the page (inline view + a ``data:`` download),
    and no panel points at a sibling file that breaks the moment the HTML is moved."""
    report = collect_report(workspace)
    assert report.assays[0].artifacts, "the composed workspace should carry embedded artifacts"
    html = render_html(report)
    assert 'download="Snakefile"' in html
    assert "data:text/plain;base64," in html  # the Snakefile, embedded as bytes
    assert 'href="pipeline/' not in html  # the old broken relative links are gone
    assert 'href="manifest.yaml"' not in html


def test_evidence_collapses_ruled_out_families_with_human_reasons(workspace: Path) -> None:
    """Bulk PE reads score against 10x/BD too; those families collapse to one ruled-out line each
    with a plain-language reason — never a raw scorer diagnostic like ``motif_rate=0.03``."""
    report = collect_report(workspace)
    assay = report.assays[0]
    assert assay.ruled_out, "other families should be scored and ruled out"
    for r in assay.ruled_out:
        assert "=" not in r.reason, f"raw scorer diagnostic leaked onto the page: {r.reason!r}"
    assert 'class="ruled-list"' in render_html(report)


def test_report_degrades_when_the_matrix_cache_is_absent(own_workspace: Path) -> None:
    """Delete the sidecar: no crash, no invented matrix — the chemistry decision (in the manifest)
    still renders, and the page says the matrix was not persisted."""
    shutil.rmtree(own_workspace / "seqforge" / "cache" / "matrices")
    report = collect_report(own_workspace)
    assert report.assays[0].matrices == []
    html = render_html(report)
    assert "How the chemistry was decided" in html  # the panel still renders
    assert "not persisted" in html


def test_report_is_ir_ready_without_a_composed_pipeline(own_workspace: Path) -> None:
    """Remove the composed pipeline: the verdict falls back to ir-ready, never a manufactured refusal."""
    shutil.rmtree(own_workspace / "seqforge" / "pipeline")
    assay = collect_report(own_workspace).assays[0]
    assert assay.conclusion.kind == "ir_ready"
    assert assay.conclusion.exit_code == 0
    assert assay.plan is not None and assay.plan.snakefile_rel is None


def test_report_handles_a_multi_assay_layout(own_workspace: Path) -> None:
    """Two ``<assay>/manifest.yaml`` render as two assays with a switcher in the shell."""
    sf = own_workspace / "seqforge"
    manifest = (sf / "manifest.yaml").read_text()
    for name in ("assay-a", "assay-b"):
        (sf / name).mkdir()
        (sf / name / "manifest.yaml").write_text(manifest)
    (sf / "manifest.yaml").unlink()

    report = collect_report(own_workspace)
    assert report.is_multi_assay and len(report.assays) == 2
    assert 'id="assay-select"' in render_html(report)


def test_collect_raises_only_when_there_is_nothing_to_report(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        collect_report(tmp_path)


def test_flow_steps_carry_the_real_decision(workspace: Path) -> None:
    """The Flow narrative is a list of typed steps carrying this dataset's real values, ending on a
    ``done`` step for a clean compile — and the winning chemistry id survives into the rendered page."""
    assay = collect_report(workspace).assays[0]
    steps = flow_steps(assay)
    assert (
        steps and steps[-1].kind == "done"
    )  # compiled -> the deliverable, not blocked/needs-a-human
    blob = " ".join(s.title + " " + " ".join(s.desc) + " " + s.note for s in steps)
    assert assay.chemistry.value[0] in blob  # the real chemistry id, not a placeholder
    html = render_html(collect_report(workspace))
    assert 'class="flow-strip"' in html and assay.chemistry.value[0] in html


def test_flow_renders_as_html_cards_not_a_scaled_diagram(workspace: Path) -> None:
    """No mermaid: the flow is plain HTML cards (readable at any width), so the page ships no diagram
    engine and no ``text/x-mermaid`` block, and the packaged assets no longer include the bundle."""
    from importlib.resources import files

    html = render_html(collect_report(workspace))
    assert 'class="flow-strip"' in html and 'class="flow-step' in html
    assert "text/x-mermaid" not in html and "globalThis.mermaid" not in html

    asset_names = {p.name for p in (files("seqforge.report") / "assets").iterdir()}
    assert "mermaid.min.js" not in asset_names
    assert {"report.css", "report.js"} <= asset_names


# -- modality-aware rendering (ATAC / fragments) --------------------------------------------------


def test_pipeline_stages_render_fragments_not_gene_counts_for_atac() -> None:
    """An ATAC recipe's stage diagram must show chromap + fragments, never the RNA "count genes"
    language — the report reads the quantification family off the recipe, so an `atac:` quant flips it."""
    from seqforge.report.collect import _pipeline_stages
    from seqforge.report.model import DecisionField, PlanView

    atac_plan = PlanView(
        fields=[
            DecisionField(
                label="quantification",
                value="atac: fragments (fragments.tsv.gz)",
                basis="inferred",
                rung=3,
            )
        ]
    )
    stages = _pipeline_stages(atac_plan)
    blob = " ".join(f"{s.title} {s.detail}" for s in stages).lower()
    assert "fragment" in blob
    assert "chromap" in blob
    assert "count genes" not in blob  # the RNA phrasing must not leak into an ATAC run

    # the RNA branch is unchanged: a solo recipe still renders the STARsolo count-matrix stages
    solo_plan = PlanView(
        fields=[
            DecisionField(
                label="quantification", value="solo: Gene, GeneFull", basis="inferred", rung=3
            )
        ]
    )
    solo_blob = " ".join(f"{s.title} {s.detail}" for s in _pipeline_stages(solo_plan)).lower()
    assert "count" in solo_blob and "starsolo" in solo_blob


# -- the finished pipeline (the Results tab) -------------------------------------------------------
#
# The page's fourth join, and the only one whose artifact seqforge did not write: once the user
# submits the composed Snakefile, its per-sample QC bundles land beside it and the report reads them.
# The bulk fixture composes `map/star`, which is registered as not-yet-reporting, so these tests swap
# the copied `.smk` for the single-cell one — `CompiledPipeline.module` reads the module off the file
# that is *present*, which is exactly the seam being exercised.

#: One STARsolo `Summary.csv`, as `qc_bundle` folds it into the artifact. Small on purpose: what is
#: under test here is the JOIN — does the collector find the file, name the module and count the
#: samples — and the metric table itself is held to real STARsolo values in `tests/test_workflows.py`.
_QC_SUMMARY: dict[str, object] = {
    "Number of Reads": 412331205,
    "Reads With Valid Barcodes": 0.972113,
    "Estimated Number of Cells": 8842,
    "Reads Mapped to Gene: Unique Gene": 0.641902,
}


def _finish_a_starsolo_pipeline(ws: Path, *, outdir: str = "results") -> list[str]:
    """Make the workspace's compiled pipeline look like a finished single-cell one. Returns its samples.

    Two edits, both to what is *on disk* rather than to any seqforge decision: the copied module
    becomes `starsolo.smk` (so the owner reports `map/starsolo`), and one QC bundle lands per sample
    the composed config contracted for.
    """
    import gzip

    from seqforge.pipeline import CompiledPipeline

    pipeline = CompiledPipeline.discover(ws)
    assert pipeline is not None, "the fixture workspace should already be composed"
    for smk in pipeline.directory.glob("*.smk"):
        smk.rename(smk.with_name("starsolo.smk"))

    samples = pipeline.samples
    assert samples, "the composed config should carry its own sample list"
    for sample in samples:
        out = pipeline.directory / outdir / sample / f"{sample}.qc.json.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(out, "wt", encoding="utf-8") as fh:
            json.dump(
                {
                    "sample": sample,
                    "summary": {"Gene": _QC_SUMMARY},
                    # A per-barcode vector, so the knee figures are drawn rather than skipped: they
                    # are the only code on this page that emits SVG, and the size and byte-identity
                    # guarantees below are worth nothing against a page that never drew one.
                    "umi_per_cell": {"Gene": [4213, 980, 310, 44, 9, 1]},
                },
                fh,
            )
    return samples


def test_a_finished_pipelines_metrics_are_joined_in_and_the_results_tab_appears(
    own_workspace: Path,
) -> None:
    """The join, end to end: an artifact on disk becomes a graded row on the page.

    Every fact comes from the compiled pipeline's own owner — which module ran, where the outputs are,
    which samples were contracted — so this also holds that the collector asks rather than re-derives.
    """
    samples = _finish_a_starsolo_pipeline(own_workspace)

    assay = collect_report(own_workspace).assays[0]

    assert assay.pipeline_stats is not None
    assert assay.pipeline_stats.module == "map/starsolo"
    assert assay.pipeline_stats.n_found == len(samples) == assay.pipeline_stats.n_expected
    assert assay.pipeline_stats.complete
    assert "valid_barcodes" in {k for k, _ in assay.pipeline_stats.columns}

    html = render_html(collect_report(own_workspace))
    assert ">Results</button>" in html  # the tab is offered, because there is something behind it
    assert "97.2%" in html  # the graded valid-barcode rate, formatted by the code that owns it
    assert 'class="genstats-table"' in html
    assert 'class="knee-svg"' in html  # hand-built inline SVG, no plotting library and no network


def test_a_workspace_that_was_only_compiled_renders_the_page_it_always_did(workspace: Path) -> None:
    """No pipeline output, no Results tab — never a tab leading to "not run yet".

    The reader is not offered a door into an empty room, and the tab's *presence* becomes the signal
    that something here has results. A compiled-but-not-run workspace must render exactly the page it
    rendered before this section existed.

    This fixture is bulk, and `map/star` **does** report — so the `None` below is the absence of a
    results directory and not the absence of an adapter. That distinction is the whole reason to say
    it here: while the rollout was partial this test could have passed on an unregistered module and
    proved nothing about an empty room.
    """
    assay = collect_report(workspace).assays[0]

    assert assay.pipeline_stats is None
    assert ">Results</button>" not in render_html(collect_report(workspace))


def test_a_relocated_pipeline_is_found_only_through_the_results_flag(own_workspace: Path) -> None:
    """`snakemake --directory` puts the outputs somewhere the workspace cannot know.

    That is a machine fact, so it arrives as a flag rather than as a search: without it the collector
    looks where the composed config said and correctly finds nothing. The value is joined onto the
    pipeline directory exactly as `outdir` is, so an absolute path is left untouched.
    """
    from seqforge.pipeline import CompiledPipeline

    _finish_a_starsolo_pipeline(own_workspace, outdir="elsewhere")
    pipeline = CompiledPipeline.discover(own_workspace)
    assert pipeline is not None

    assert collect_report(own_workspace).assays[0].pipeline_stats is None
    relative = collect_report(own_workspace, results_dir=Path("elsewhere")).assays[0]
    absolute = collect_report(
        own_workspace, results_dir=(pipeline.directory / "elsewhere").resolve()
    ).assays[0]

    assert relative.pipeline_stats is not None and relative.pipeline_stats.complete
    assert absolute.pipeline_stats is not None
    assert absolute.pipeline_stats == relative.pipeline_stats


def test_the_report_verb_passes_the_results_flag_through_and_summarises_the_pipeline(
    own_workspace: Path,
) -> None:
    """The CLI's own seam: `--results` reaches the collector, and stdout carries how the run went.

    `pipeline_stats` sits BESIDE `kind`/`exit` in the JSON summary and is never folded into them —
    "the compiler succeeded" and "the pipeline succeeded" are two judgements, and a machine consumer
    that could only read one of them would have no way left to ask which it was being told.
    """
    samples = _finish_a_starsolo_pipeline(own_workspace, outdir="elsewhere")

    result = runner.invoke(
        app,
        ["report", "-C", str(own_workspace), "--no-timestamp", "--results", "elsewhere"],
    )

    assert result.exit_code == 0, result.stdout
    conclusion = json.loads(result.stdout)["conclusion"][0]
    assert conclusion["kind"] == "compiled"  # the COMPILE verdict, untouched
    assert conclusion["pipeline_stats"] == {
        "module": "map/starsolo",
        "samples_finished": len(samples),
        "samples_expected": len(samples),
    }


def test_the_page_keeps_its_standing_guarantees_with_results_rendered(own_workspace: Path) -> None:
    """Self-contained, inside the size budget, byte-deterministic — re-proved with the heaviest tab on.

    Results is the one section that draws: hand-built inline SVG per sample, plus a metrics table.
    The three properties above are asserted elsewhere against a page that has none of that, which
    would leave exactly the new drawing code unchecked for the failures they exist to catch.
    """
    _finish_a_starsolo_pipeline(own_workspace)

    html = render_html(collect_report(own_workspace))

    assert not re.findall(r'(?:src|href)\s*=\s*"(?:https?:)?//[^"]+"', html)
    assert len(html.encode()) < 500_000
    assert html == render_html(collect_report(own_workspace))
