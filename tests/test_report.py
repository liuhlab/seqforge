"""Tests for ``seqforge report`` — the self-contained HTML decision report.

Everything runs offline against a workspace built by the real ``run`` verb on KB-generated bulk reads
(no network, no provider, no onlist). The load-bearing properties: the page is genuinely
self-contained (no external reference can regress in), it stays small, it is byte-deterministic, and
the collector degrades honestly when a piece is missing rather than crashing or inventing a verdict.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
from html import escape, unescape
from pathlib import Path
from typing import TYPE_CHECKING, get_args

import pytest
import yaml
from typer.testing import CliRunner

from conftest import write_fastq_gz
from seqforge import kb
from seqforge.cli import app
from seqforge.report import collect_report, render_html
from seqforge.report.flow import flow_steps
from seqforge.report.model import AssayReport
from seqforge.workflows.metrics import Alert, DecisionRef, PipelineStats

if TYPE_CHECKING:  # the resolver seam is private, and only its type is wanted at module scope
    from seqforge.models.dataset import DatasetManifest
    from seqforge.report.collect import _DecisionContext

runner = CliRunner()

#: Most tests here read one `seqforge run`. xdist's default `load` scheduler spreads them across
#: workers and each worker rebuilds `_bulk_workspace`, so the build is paid once per worker for
#: identical proof — measured at eight workers it cost roughly five times the CPU of one.
#: `xdist_group` pins the module to a single worker under `--dist=loadgroup`, so it happens once.
#: Correct here because the run dominates: nothing below is slow enough to want its own core.
#:
#: The ratio is quoted and the seconds are not, per the method stated in
#: `docs/research/test-suite-cost-shape.md` — this module has since
#: grown past three times the size that measurement was taken at, and a stale absolute is worse than
#: no number because it is still trusted. Re-measure before changing the grouping, not before
#: reading this.
pytestmark = pytest.mark.xdist_group("report-workspace")


def _build_bulk_workspace(tmp_path: Path) -> Path:
    """A fully compiled workspace via the real ``run`` verb: manifest + processing + Snakefile + caches.

    Bulk needs no onlist (the default registry ships none), so this is the branch CI can run headless.
    The self-consistent run means the manifest shas and the persisted candidate/matrix shas agree, so
    the report's scan-join finds the evidence matrix.
    """
    spec = kb.load_spec("bulk-rnaseq")
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
    assert 'class="mx-cell' in render_html(collect_report(workspace))


def test_samples_render_as_a_metadata_table(workspace: Path) -> None:
    """The per-sample card list is gone: samples are one scrolling table with an expandable drawer."""
    html = render_html(collect_report(workspace))
    assert 'class="sf-scroll-x"' in html  # the region that scrolls, so the page never does
    # The pinned identifier and the drawer's control are one cell. Matched as two classes on one
    # element rather than as a whole `class="…"` string: a third utility beside them is the pane's
    # business, and a test that pins the attribute verbatim fails on a spacing change.
    row_head = re.search(r'<th scope="row" class="([^"]*)"', html)
    assert row_head is not None
    assert {"sf-col-sticky", "smp-toggle"} <= set(row_head.group(1).split())
    assert 'class="smp-caret"' in html  # the expand caret
    assert '<tr id="detail-0-0" hidden>' in html  # the files drawer, closed


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
    html = render_html(report)
    for r in assay.ruled_out:
        assert "=" not in r.reason, f"raw scorer diagnostic leaked onto the page: {r.reason!r}"
        # The reason itself, on the page — not a class name that a rewrite could keep while the
        # sentence it was supposed to carry quietly stopped rendering.
        assert escape(r.reason, quote=True) in html
        assert escape(r.tech, quote=True) in html


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


# -- the page shell ---------------------------------------------------------------------------------
#
# The frame every tab composes into: the header, the tab bar, `<main>`, the footer and `_panel`.
# Asserted at the render seam and nowhere else — a panel function's return value is not the page, and
# PR 1's cautionary tale is a test named for a defect that passed while encoding it.


def _body_classes(html: str) -> set[str]:
    """Every class token the rendered BODY carries — the stylesheets are cut off first.

    Both quote styles, because a fragment nested inside a double-quoted Python string writes
    ``class='x'`` and a check that only saw ``class="x"`` would quietly stop checking those.
    """
    body = html.split("</style>")[-1]
    return {
        token
        for group in re.findall(r"""class\s*=\s*["']([^"'\n]*)["']""", body)
        for token in group.split()
    }


def _pane(html: str, tab: str) -> str:
    """One tab's rendered markup, cut out of the page — the seam every visual assertion belongs at.

    Sliced on the `data-tab` hook rather than on a class the redesign is free to change, and bounded
    by the next pane's opening tag so a claim about one tab cannot be satisfied by another's markup.
    """
    body = html.split("</style>")[-1]
    start = body.index(f'data-tab="{tab}">', body.index("<main"))
    rest = body[start:]
    # Bounded by whichever comes first: the next pane, or the footer for the last one. Without the
    # footer bound, a claim about the last tab could be satisfied by the page's own version string.
    ends = [i for i in (rest.find('<div class="pane" data-tab='), rest.find("<footer")) if i >= 0]
    return rest[: min(ends)] if ends else rest


def test_the_page_frame_is_one_reading_column_under_one_sticky_band(workspace: Path) -> None:
    """Four elements share the reading column, and the two sticky ones became one.

    The width was restated by ``header.top``, ``.tabs-row``, ``main`` and ``footer.foot``, each from
    ``--measure`` — one measurement written four times, which is three chances to change it in three
    places. And the tab bar stuck at a hardcoded ``top: 56px``, a number that had to equal the
    header's *rendered* height: nothing could check it and any change to what the header holds
    falsified it. Wrapping both in one sticky element deletes the number rather than re-tuning it.
    """
    page = render_html(collect_report(workspace)).split("</style>")[-1]

    # the header row, the tab row, <main> and the footer — and nothing else
    assert page.count('class="sf-page') == 4

    band = '<div class="sticky top-0'
    assert page.count(band) == 1, "one sticky element, not a header and a tab bar racing each other"
    inside = page[page.index(band) : page.index("<main")]
    assert "<header" in inside and "<nav>" in inside
    assert "sticky" not in inside.replace(band, "", 1), "only the band sticks; neither child does"


def test_the_three_roles_every_pane_needed_are_drawn_by_one_component_each(
    workspace: Path,
) -> None:
    """A section label, "there is nothing here", and "this section is absent, and why".

    Every pane needs all three and none of them owns any of them, so each was invented once per pane:
    the label as two Python constants — the second silently shadowing the first, since both were named
    ``_SUB_H`` at module scope — plus one spelled-out ``<h4>``; the notice as ``.notice`` plus two
    hand-typed boxes; the gap as ``.empty`` plus one. Three roles, eight spellings, and the only thing
    keeping them looking alike was that three agents happened to pick similar utilities.

    So the assertion is *one class per role, on every pane that plays it*, made against a page
    rendered in the states that produce them — an assay with no samples, no scored comparison, no
    artifacts and no pipeline results, which is the branch a clean fixture never reaches. The
    negative half is what makes it a guard rather than a description: none of the old spellings may
    come back, and no element may hand-spell the label's letter-spacing again.
    """
    report = collect_report(workspace)
    assay = report.assays[0]
    bare = assay.model_copy(
        update={
            "samples": [],
            "matrices": [],
            "ruled_out": [],
            "artifacts": [],
            "pipeline_stats": None,
        }
    )
    page = render_html(report.model_copy(update={"assays": [bare]}))

    for tab in ("samples", "pipeline"):
        assert "sf-empty" in _body_classes(_pane(page, tab)), (
            f"{tab} says nothing is here its own way"
        )
    for tab in ("evidence", "results"):
        assert "sf-notice" in _body_classes(_pane(page, tab)), (
            f"{tab} explains an absence its own way"
        )
    assert "sf-sub-h" in _body_classes(_pane(page, "overview"))

    worn = _body_classes(page)
    assert not (worn & {"notice", "empty"}), "an old spelling of a role that now has a component"
    assert "tracking-[0.07em]" not in worn, (
        "the section label is being hand-spelled again — it is `sf-sub-h`, and the point of a "
        "component is that the eighth site cannot drift from the first seven"
    )


def test_a_refused_compile_paints_the_two_places_a_reader_looks_and_nothing_else(
    workspace: Path,
) -> None:
    """The exception path, rendered — which the headless fixture, being a clean compile, never is.

    Everything the Flow tab's redesign decided is on this branch: `guess`, `measured` and `done` are
    the norm and carry no tint, and only the card that is asking for a human is painted. A page that
    is never rendered in that state proves the untinted half and nothing else, so the conclusion is
    swapped on a real collected report and the whole page re-rendered — still `render_html` in, HTML
    out, and every value below still comes from production rather than from a hand-built view.
    """
    report = collect_report(workspace)
    assay = report.assays[0]
    refused = assay.model_copy(
        update={
            "conclusion": assay.conclusion.model_copy(
                update={
                    "kind": "blocker",
                    "exit_code": 2,
                    "headline": "Blocked",
                    "detail": "a persisted refusal: the read layout matched no known kit",
                }
            )
        }
    )
    page = render_html(report.model_copy(update={"assays": [refused]}))

    # the verdict is one badge in two places, and on this branch it is the tinted member
    assert (
        re.findall(r'class="(sf-verdict [^"]*)"', page.split("</style>")[-1])
        == ["sf-verdict sf-v-blocker"] * 2
    )

    kinds = re.findall(r'<li class="flow-([a-z]+)"', _pane(page, "flow"))
    assert kinds == [s.kind for s in flow_steps(refused)]
    assert kinds[-1] == "blocked", "a refusal must end the narrative on the card that says so"
    assert set(kinds[:-1]) <= {"guess", "measured"}, kinds
    assert kinds.count("blocked") == 1, "exactly one card is painted, and it is the last one"


def test_the_compile_verdict_and_the_pipeline_run_state_are_two_different_badges(
    own_workspace: Path,
) -> None:
    """One badge says the compiler produced a Snakefile; the other says that Snakefile finished.

    They are shown on the same page and they disagree exactly when it matters — here, a workspace
    that compiled cleanly and whose pipeline then wrote nothing readable. So the page is rendered in
    that state and both are read off it: the verdict is the header pill's own component, restated on
    the Overview so the shape a reader learned up top means the same thing twice; the run state is a
    different component entirely. Asserted as *disjoint class sets* rather than as two names, because
    the failure this guards is one of them drifting into the other's clothes.
    """
    _finish_a_starsolo_pipeline(own_workspace)
    from seqforge.pipeline import CompiledPipeline

    pipeline = CompiledPipeline.discover(own_workspace)
    assert pipeline is not None
    for sample in pipeline.samples:
        (pipeline.results_dir / sample / f"{sample}.qc.json.gz").write_bytes(b"not gzip at all")

    page = render_html(collect_report(own_workspace)).split("</style>")[-1]

    verdicts = re.findall(r'class="(sf-verdict [^"]*)"', page)
    assert len(verdicts) == 2, "the compile verdict is the header pill and its Overview restatement"
    assert set(verdicts) == {"sf-verdict sf-v-compiled"}, verdicts

    # The run state is found by the component that draws it, not by a tint: `lvl-bad` is also on
    # every failing metric cell, so keying off the tint would find eighteen elements and prove
    # nothing about the badge.
    states = re.findall(r'class="([^"]*\blvl-state\b[^"]*)"', page)
    assert len(states) == 1, "one run-state badge on the page, for the one pipeline that ran"
    assert "lvl-bad" in states[0].split(), (
        f"a pipeline that wrote nothing readable is the third state, tinted bad: {states[0]}"
    )
    assert not (set(states[0].split()) & set(verdicts[0].split())), (
        "the two badges share a class, so a reader cannot tell which question is being answered"
    )


def test_the_shell_still_carries_every_hook_the_script_selects_on(workspace: Path) -> None:
    """The class names ``report.js`` selects on are a contract, and renaming one fails silently.

    Nothing about the page's *appearance* changes when ``.tab`` becomes ``.sf-tab`` — the tabs simply
    stop switching, and every visual assertion in this file keeps passing. So both ends are asserted
    here: the selector is still in the script, and the page still renders something it matches.
    (``#assay-select`` renders only for a multi-assay workspace; it is held by
    ``test_report_handles_a_multi_assay_layout``.)
    """
    script = (_ASSETS / "report.js").read_text()
    page = render_html(collect_report(workspace)).split("</style>")[-1]

    for selector, in_the_page in (
        ('"section.assay"', '<section class="assay" data-assay="0">'),
        ('".pane"', '<div class="pane active" data-tab="overview">'),
        ('".tab"', '<button class="tab active" data-tab="overview">'),
        ('"theme-toggle"', 'id="theme-toggle"'),
    ):
        assert selector in script, f"report.js no longer selects on {selector}"
        assert in_the_page in page, f"nothing in the page answers report.js's {selector}"


def test_collect_raises_only_when_there_is_nothing_to_report(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        collect_report(tmp_path)


def test_flow_steps_carry_the_real_decision(workspace: Path) -> None:
    """The Flow narrative is a list of typed steps carrying this dataset's real values, ending on a
    ``done`` step for a clean compile — and the winning chemistry id survives into the rendered page.

    The card classes are checked against what ``flow_steps`` actually emitted rather than against a
    restated list: the kind is what decides whether a card is tinted, so a test that named the kinds
    itself would keep passing while the page painted the wrong ones.
    """
    assay = collect_report(workspace).assays[0]
    steps = flow_steps(assay)
    assert (
        steps and steps[-1].kind == "done"
    )  # compiled -> the deliverable, not blocked/needs-a-human
    blob = " ".join(s.title + " " + " ".join(s.desc) + " " + s.note for s in steps)
    assert assay.chemistry.value[0] in blob  # the real chemistry id, not a placeholder

    pane = _pane(render_html(collect_report(workspace)), "flow")
    assert assay.chemistry.value[0] in pane
    assert re.findall(r'<li class="flow-([a-z]+)"', pane) == [s.kind for s in steps]


def test_flow_renders_as_reflowing_html_cards_not_a_scaled_diagram(workspace: Path) -> None:
    """The property that made Mermaid leave, asserted as a property and not as a class name.

    A scaled SVG cannot reflow — its text shrank to nothing on a wide dataset, and dropping it took a
    page from ~2.6 MB to tens of KB. So the flow must be a container whose *column count follows the
    viewport*, holding one element per narrative step, with no drawing surface and no fixed width
    anywhere in the pane. Each of those is checked; asserting a class name would only have proved the
    markup was renamed.
    """
    from importlib.resources import files

    html = render_html(collect_report(workspace))
    pane = _pane(html, "flow")

    grid = re.search(r'<ol class="([^"]*)"', pane)
    assert grid is not None, "the steps are a list"
    classes = grid.group(1).split()
    assert "grid" in classes
    assert {c for c in classes if re.fullmatch(r"(sm|md|lg|xl):grid-cols-\d+", c)}, (
        f"the flow does not reflow — its column count is fixed at every width: {classes}"
    )
    assert len(re.findall(r"<li class=", pane)) == len(
        flow_steps(collect_report(workspace).assays[0])
    )

    assert "<svg" not in pane and "viewBox" not in pane  # nothing is drawn, so nothing can scale
    assert not re.search(r"(?:width|min-width|max-width)\s*:\s*\d", pane)
    assert "text/x-mermaid" not in html and "globalThis.mermaid" not in html

    asset_names = {p.name for p in (files("seqforge.report") / "assets").iterdir()}
    assert "mermaid.min.js" not in asset_names
    assert {"report.tw.css", "report.js"} <= asset_names


def test_every_flow_step_kind_the_narrative_can_emit_has_a_declared_card() -> None:
    """``StepKind`` is closed, ``flow-{kind}`` is computed, and the purge sees neither.

    So a sixth kind would be added to ``flow.py``, rendered by ``panels.py`` and styled by nothing —
    a card with no border on the tab whose whole job is to be read at a glance. The members are read
    out of the ``Literal`` for the same reason ``Basis`` and ``Level`` are: a restated list is a test
    that agrees with itself. The colour split is asserted too, because it is the decision this ticket
    made: only the two kinds that ask for a human are painted.
    """
    from seqforge.report.flow import StepKind

    src = (_ASSETS / "report.src.css").read_text()
    declared = _declared_components(src)
    members = set(get_args(StepKind))
    assert len(members) > 1, "get_args should yield the Literal's members, not an empty tuple"

    assert {f"flow-{k}" for k in members} <= declared, "a step kind renders as an unstyled card"
    assert not _classes_with_no_rule(
        {f"flow-{k}" for k in members}, [(_ASSETS / "report.tw.css").read_text()]
    )

    body = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    painted = {
        k for k in members if re.search(rf"\.flow-{k}\s*\{{[^}}]*--flow-bg:\s*var\(--sf-\w", body)
    }
    assert painted == {"blocked", "ask"}, (
        f"colour marks exceptions: the norm must carry no tint, but {sorted(painted)} do"
    )


def test_the_report_verbs_help_describes_the_page_that_actually_ships() -> None:
    """The verb's ``--help`` still promised the diagram engine the page no longer carries.

    Dropping Mermaid cut a rendered page from ~2.6 MB to tens of KB — the single largest thing ever
    true about this page — and the prose went on describing the bundle as inlined. Prose that names a
    dependency the wheel does not carry is worse than none: it is what a reader checks a size budget
    against, and it would have sent the next person looking for an asset that is not there.

    The renderer's own docstring was asserted here too, and is not any more: the text of a docstring
    is not something the code can do, so a rename broke that half falsely and an indirection would
    have passed it falsely. What the prose stood for is asserted below against the package instead.
    """
    from importlib.resources import files

    from seqforge.report import render

    result = runner.invoke(app, ["report", "--help"])

    assert result.exit_code == 0, result.stdout
    # `--help` is a promise about the page a user is about to get, not a history of it.
    assert "mermaid" not in result.stdout.lower()

    # The word `vendored` was banned here as a proxy for "does not claim a bundle it lacks". The
    # page now really does vendor one thing -- a built Tailwind stylesheet -- so assert the rule the
    # proxy stood for instead: every sheet the renderer inlines is a file the package ships. (The
    # docstring may still NAME mermaid; it names it as the bundle that was removed, which is the
    # history a reader needs. `--help`, above, is where the ban belongs -- that one is a promise.)
    assets = files("seqforge.report") / "assets"
    missing = [name for name in render._STYLESHEETS if not (assets / name).is_file()]
    assert not missing, f"the renderer inlines assets the wheel does not carry: {missing}"


# -- the display helpers ---------------------------------------------------------------------------


def test_the_escaper_renders_a_missing_value_as_empty_not_the_word_none() -> None:
    """An absent optional field must reach the page as nothing, never as the literal word ``None``.

    The display model is full of optionals (a study centre, a read's ``onlist_ref``, a metric's
    display), so ``esc`` is called on ``None`` routinely — and ``str(None)`` renders a five-letter
    English word a reader cannot tell apart from a value that was actually recorded. The eval
    report's sibling escaper has always mapped ``None`` to the empty string; this one did not, and
    two escapers in one repo disagreeing about the same case is the drift worth closing.
    """
    from seqforge.evals.report import esc as eval_esc
    from seqforge.report.panels import esc

    assert esc(None) == ""
    assert esc(None) == eval_esc(None)
    # …and it is still an escaper: quoting stays on, so a value can never close an attribute.
    assert esc('<b>&"') == "&lt;b&gt;&amp;&quot;"
    assert esc(0) == "0" and esc(False) == "False"  # only None is blank, not every falsey value


def test_the_who_decided_column_answers_for_every_basis_in_the_who_voice() -> None:
    """``user_confirmed`` is the basis a **Recipe** almost always carries, and it was the one missing.

    On a dataset field ``basis`` answers *how we know*; on a recipe it answers *who decided*, which is
    why there are two phrasings and why merging them would be wrong. The ``who`` fallback held three
    entries and none of them was the user, so a flag-set or instruction-set recipe field rendered the
    raw token ``user confirmed`` in a column whose other answers are "our default" and "you specified".
    """
    from seqforge.report.model import DecisionField
    from seqforge.report.panels import _who

    chosen = DecisionField(label="genome", value="ce11 / WS298", basis="user_confirmed", rung=0)

    assert _who(chosen) == "you specified"
    assert "_" not in _who(chosen)  # never a raw underscore-stripped token


def test_both_basis_phrasings_cover_the_closed_basis_set() -> None:
    """Two maps, and they are correct to be two — but each must be total over ``Basis``.

    The member set is read out of the ``Literal`` itself rather than restated here: a fifth basis
    then breaks this test on the day it is added, which is the only way either map gets completed
    before a page renders a raw token. Restating the four would make this a test that agrees with
    itself and notices nothing.
    """
    from seqforge.models.base import Basis
    from seqforge.report.panels import _BASIS_LEGEND, _BASIS_PHRASE, _WHO_PHRASE

    members = set(get_args(Basis))
    assert len(members) > 1, "get_args should yield the Literal's members, not an empty tuple"

    assert set(_BASIS_PHRASE) == members, "the 'how we know' map lost a basis"
    assert set(_WHO_PHRASE) == members, "the 'who decided' map lost a basis"
    # Three now: the Samples legend says the same four bases in three or four words under a mark,
    # and a basis missing from it ships a mark whose key does not name it.
    assert set(_BASIS_LEGEND) == members, "the Samples legend lost a basis"
    assert all(v for v in _BASIS_PHRASE.values()) and all(v for v in _WHO_PHRASE.values())
    assert all(v for v in _BASIS_LEGEND.values())
    # Kept separate on purpose: one answers how we know, the other who decided.
    assert _BASIS_PHRASE != _WHO_PHRASE


def test_the_level_phrasing_covers_the_closed_verdict_set() -> None:
    """``Level`` is the second closed set this page renders, and it had the defect ``Basis`` just lost.

    ``_level_mark`` indexes ``_LEVEL_PHRASE`` directly, so a fifth verdict is a ``KeyError`` raised
    mid-render rather than a missing phrase — and the legend hand-listed its four keys, so a verdict
    could be added, graded by an adapter, tinted by the stylesheet and still never appear in the key
    that says what the tint means. Read out of the ``Literal`` for the same reason as ``Basis``: a
    restatement here would be a test that agrees with itself.
    """
    from seqforge.report.panels import _LEVEL_FLAG, _LEVEL_PHRASE
    from seqforge.workflows.metrics import Level

    members = set(get_args(Level))
    assert len(members) > 1, "get_args should yield the Literal's members, not an empty tuple"

    assert set(_LEVEL_PHRASE) == members, "a verdict with no phrase renders as a KeyError"
    assert all(v for v in _LEVEL_PHRASE.values())
    # The flags are deliberately partial -- marking the majority of cells is the same as marking
    # none of them -- but every flag must still name a real verdict.
    assert set(_LEVEL_FLAG) <= members


def test_the_level_legend_names_every_verdict_the_page_can_render(own_workspace: Path) -> None:
    """The legend is what makes a tint and a mark mean anything, so a verdict missing from it is mute.

    Asserted against the **rendered page** and not against ``_LEVEL_LEGEND``, which is where this
    used to reach. A module-level string is not the page: it can be built correctly at import and
    never reach a reader — dropped from the table's caller, rendered on a tab the page decided not to
    offer, or emitted into a branch this fixture does not take — and none of that would have failed
    here. Every visual claim belongs at the render seam, which is the criterion this PR added and the
    defect PR 1 shipped under.
    """
    from seqforge.workflows.metrics import Level

    _finish_a_starsolo_pipeline(own_workspace)
    page = render_html(collect_report(own_workspace)).split("</style>")[-1]

    # Each legend entry, read back off the page: its verdict class, its non-colour mark, its words.
    entries = re.findall(
        r'<span class="lvl-(\w+)[^"]*"><span class="lvl-chip">([^<]*)</span>([^<]*)</span>', page
    )
    named = {level: (mark, words) for level, mark, words in entries}

    assert set(named) == set(get_args(Level)), "a verdict the page can render is missing its key"
    # Spelled out, not `_LEVEL_PHRASE[level].split(" — ")[0]`. That is the legend's OWN formula, and
    # a test that recomputes the expected value the way production computes it agrees with any
    # rewording, including a wrong one — it would pass on a legend that read "far outside the
    # expected range" for `ok`. These are the four sentences a reader is owed.
    assert {level: words for level, (_mark, words) in named.items()} == {
        "ok": "within the expected range",
        "warn": "outside the expected range",
        "bad": "far outside the expected range",
        "none": "no defensible threshold exists for this number",
    }
    for level, (mark, _words) in named.items():
        # The mark is what survives colour-blindness and a greyscale printout, so the key has to
        # show it — and has to show its ABSENCE for the two verdicts that deliberately carry none.
        assert mark == {"warn": "!", "bad": "!!"}.get(level, "")


def test_the_report_package_ships_no_private_helper_nothing_calls() -> None:
    """A helper whose only occurrence in the tree is its own ``def`` is dead weight that reads as live.

    Checked by mechanism rather than by eye, because the eye is what missed it: an AST walk over the
    package for every module-level ``def _name``, against every name loaded anywhere in the package
    **or in this test file** — a helper only a test drives is a seam under test, not dead code.
    """
    import seqforge.report

    package = Path(seqforge.report.__file__).parent
    sources = sorted(package.glob("*.py")) + [Path(__file__)]

    defined: set[str] = set()
    referenced: set[str] = set()
    for path in sources:
        tree = ast.parse(path.read_text())
        if path.parent == package:
            defined |= {
                node.name
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name.startswith("_")
            }
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, ast.alias):
                referenced.add(node.name.rsplit(".", 1)[-1])

    assert defined, "the walk should have found the package's helpers at all"
    assert not (defined - referenced), (
        f"dead private helper(s) in seqforge/report: {sorted(defined - referenced)}"
    )


# -- the two dense grids: the sample-metadata table and the evidence matrix ------------------------
#
# The headless bulk fixture resolves one sample with no attributes and a matrix with no forbidden
# gate, so it cannot exercise a provenance mark, a withheld value or a ruled-out cell — the branches
# these two views exist for. So this section builds a display model that has all of them and renders
# the WHOLE PAGE from it. `render_html(report)` in, HTML out: a panel function's return value is not
# the page, and the one test here that used to reach below that seam is the reason to say so.


def _rich_assay() -> AssayReport:
    """One assay whose Samples and Evidence tabs are actually full.

    Deliberately a hand-built display model and not a workspace: every branch below is reachable only
    from data no headless run produces (an assertion with a quote, a resolver that withheld a value, a
    scored candidate whose gate forbade a read). It is still the production renderer that turns it
    into a page — this fixture replaces the *collector*, never the seam under test.
    """
    from seqforge.models.base import Basis
    from seqforge.report.model import (
        AssayLabelView,
        AttributeView,
        ChemistryDecision,
        ConclusionView,
        ElementView,
        EvidenceRef,
        FileView,
        MatrixCellView,
        MatrixRoleRow,
        MatrixView,
        ReadView,
        RuledOut,
        SampleView,
    )

    quote = "daf-2(e1370) animals were grown at 20 °C and harvested at the L4 stage."
    # One attribute per basis, read out of the Literal so a fifth one lands in this fixture — and
    # therefore on the rendered page — the day it is added, rather than being quietly untested.
    attributes = [
        AttributeView(
            key=f"trait_{basis}",
            value=f"value carried by {basis}",
            basis=basis,
            rung=2,
            evidence=[
                EvidenceRef(
                    raw="assert-1", kind="assertion", quote=quote, document="lee-et-al-2023", page=3
                )
            ],
        )
        for basis in get_args(Basis)
    ]
    attributes.append(
        AttributeView(key="treatment", value="", basis="asserted", rung=1, withheld=True)
    )
    attributes.append(
        AttributeView(
            key="source_name",
            value="whole animal, mixed-stage population harvested 48 h after the L1 "
            "synchronisation step described in the supplementary methods, washed in M9",
            basis="asserted",
            rung=2,
            evidence=[EvidenceRef(raw="SAMN12345678", kind="accession", accession="SAMN12345678")],
        )
    )

    def cells(
        a: float, b: float, *, forbidden: bool = False, absent: bool = False
    ) -> list[MatrixCellView]:
        second = MatrixCellView(status="scored", value=b)
        if forbidden:
            second = MatrixCellView(status="forbidden", reason=_FORBIDDEN_REASON)
        elif absent:
            second = MatrixCellView(status="scored", value=None)
        return [MatrixCellView(status="scored", value=a), second]

    def matrix(tech: str, score: float, **kw: bool) -> MatrixView:
        return MatrixView(
            tech=tech,
            is_winner=bool(kw.pop("winner", False)),
            score=score,
            file_labels=["SRR0001_R1.fastq.gz", "SRR0001_R2.fastq.gz"],
            roles=[
                MatrixRoleRow(role=role, cells=cells(hi, lo, **kw))
                for role, hi, lo in (("barcode", 0.96, 0.02), ("cdna", 0.03, 0.91))
            ],
        )

    return AssayReport(
        organism_taxid=6239,
        organism_name="Caenorhabditis elegans",
        chemistry=ChemistryDecision(
            value=["10x-3p-gex-v3"],
            assay_labels=[
                AssayLabelView(
                    chemistry="10x-3p-gex-v3", curie="EFO:0009922", name="Chromium 3' v3"
                )
            ],
            basis="observed",
            confidence=0.94,
            rung=3,
            modality="rna",
            n_files=2,
        ),
        reads=[
            ReadView(
                read_id="R1",
                strand="+",
                min_len=28,
                max_len=28,
                elements=[ElementView(role="cb", region_type="barcode", start=0, length=16)],
            )
        ],
        files=[
            FileView(
                basename="SRR0001_R1.fastq.gz",
                read_id="R1",
                sha256="0" * 64,
                size_bytes=1,
                uri="file://SRR0001_R1.fastq.gz",
            )
        ],
        samples=[
            SampleView(
                sample_id="SRS0001",
                accession="SAMN00001",
                n_files=1,
                file_names=["SRR0001_R1.fastq.gz"],
                attributes=attributes,
            ),
            SampleView(sample_id="SRS0002", n_files=0),  # every attribute absent: the `—` cells
        ],
        matrices=[
            matrix("10x-3p-gex-v3", 0.94, winner=True, absent=True),
            matrix("10x-3p-gex-v2", 0.61, forbidden=True),
        ],
        ruled_out=[
            RuledOut(
                tech="drop-seq", family="drop-seq", reason="read 1 is 28 bp; this kit needs 20"
            )
        ],
        conclusion=ConclusionView(
            kind="compiled", exit_code=0, headline="Compiled", detail="a Snakefile is ready"
        ),
    )


#: Quoted verbatim by the forbidden-cell test, so "the reason reached the page" is checked against the
#: sentence and not against the fact that *something* rendered.
_FORBIDDEN_REASON = "read 2 is 91 bp and this kit's barcode block needs a fixed 28 bp read"


def _rich_page() -> str:
    from seqforge.report.model import ProjectReport

    return render_html(
        ProjectReport(
            workspace_name="PRJNA1027859",
            report_version="0.0.0",
            assays=[_rich_assay()],
        )
    )


def test_every_basis_the_manifest_can_carry_reaches_the_page_as_its_own_mark() -> None:
    """Provenance is one centrally declared layer, and the key names every member of it.

    Read out of ``Basis`` rather than restated: a fifth basis must break here — on the legend that
    says what a mark means — instead of shipping an unexplained mark onto a page. And each member has
    to appear TWICE, once in the legend and once on a cell, because a legend entry with no mark
    beside any value is a key to a language the page does not speak.
    """
    from seqforge.models.base import Basis

    page = _rich_page().split("</style>")[-1]

    for basis in get_args(Basis):
        assert page.count(f"basis-mark basis-{basis}") >= 2, (
            f"{basis} is missing from the legend or from the grid"
        )
    # …and the legend's own words, so a mark is never offered without a phrase for it.
    from seqforge.report.panels import _BASIS_LEGEND

    for label in _BASIS_LEGEND.values():
        assert f">{label}</span>" in page


def test_a_withheld_attribute_renders_as_withheld_and_an_unmentioned_one_renders_as_absent() -> (
    None
):
    """Two different silences, and the page must not spell them the same way.

    ``withheld`` is a real answer — two equally trusted sources disagreed, so the resolver recorded
    nothing rather than guess — and it is shown as a value. An attribute nobody ever mentioned is a
    gap. Collapsing the first into the second would turn a decision the compiler made on purpose into
    an absence the reader would read as missing data.
    """
    page = _rich_page().split("</style>")[-1]

    assert '<span class="basis-v basis-withheld">— withheld</span>' in page
    assert 'data-value="withheld"' in page  # …and it says so in the popover too
    assert '<td class="basis-cell text-faint">—</td>' in page  # the gap: a dash, no provenance
    # The gap is the one cell with nothing to say, and the script decides that from the DATA rather
    # than from a styling class -- so it must be the branch that carries no `data-key`.
    assert '<td class="basis-cell text-faint">—</td>'.replace("—", "—") in page
    for cell in re.findall(r'<td class="basis-cell[^"]*"[^>]*>', page):
        assert ("data-key=" in cell) != ("text-faint" in cell)


def test_sample_provenance_is_a_pinnable_popover_not_a_transient_tooltip() -> None:
    """A metadata cell carries its provenance as ``data-*`` on a keyboard-reachable button, so the
    script can pin a selectable, copyable popover — never a native ``title=`` a reader can neither
    select nor copy, and which never appears at all on touch.

    Asserted at the render seam and against the script together: the markup alone cannot show that
    anything reads those attributes, and the script alone cannot show that anything emits them.
    """
    page = _rich_page().split("</style>")[-1]
    script = (_ASSETS / "report.js").read_text()

    cell = re.search(r'<td class="basis-cell[^"]*"[^>]*data-key="trait_asserted"[^>]*>', page)
    assert cell, "the asserted attribute did not render as a provenance cell"
    assert 'role="button"' in cell.group(0) and 'tabindex="0"' in cell.group(0)
    assert "data-basis=" in cell.group(0) and "data-quote=" in cell.group(0)
    assert "data-source=" in cell.group(0)
    assert "title=" not in cell.group(0)  # no transient native tooltip on the value cell

    # The script's end of the same contract: it selects these cells, it PINS (a click handler, not a
    # mouseover), it offers Copy, and Escape closes it.
    assert ".basis-cell" in script
    assert 'copyBtn.textContent = "Copy"' in script
    assert 'e.key === "Escape"' in script
    assert "mouseover" not in script and "mouseenter" not in script


def test_both_grids_scroll_in_their_own_region_with_the_row_identifier_pinned() -> None:
    """A wide dataset must scroll inside the table, never sideways in the page body.

    Both grids use the shell's own primitives rather than a second mechanism each, and only ONE column
    is sticky — the row identifier. A frozen block of columns is the whole screen at a narrow viewport,
    which is the width every one of these views has to stay readable at.
    """
    page = _rich_page()

    samples = _pane(page, "samples")
    assert samples.count('class="sf-scroll-x"') == 1
    # one sticky column: the header cell plus one per sample row, and nothing else
    assert samples.count("sf-col-sticky") == 3
    # …and that column is also the drawer's control, matched as two classes on one element rather
    # than as a whole `class="…"` string a third utility would falsify.
    row_head = re.search(r'<th scope="row" class="([^"]*)"', samples)
    assert row_head is not None
    assert {"sf-col-sticky", "smp-toggle"} <= set(row_head.group(1).split())

    evidence = _pane(page, "evidence")
    assert evidence.count("sf-scroll-x") == 2  # the winner's grid and its one sibling
    # …and the matrix's region drops the border and the radius rather than drawing a second box
    # inside the card that already has one.
    assert 'class="sf-scroll-x rounded-none border-0"' in evidence


def test_every_matrix_cell_renders_the_status_it_was_tagged_with() -> None:
    """Scored, forbidden, absent — three states, three renderings, and none of them is blank.

    A forbidden cell used to be an empty ``<td>`` whose ✕ was drawn by the stylesheet and whose reason
    was a ``title=``; a cell that was scored but carried no number fell down the same branch and was
    labelled forbidden, which is a different claim. So: the glyph is in the markup, the reason is real
    text on the same pinnable popover the Samples grid uses, and "nobody scored this" says so.
    """
    page = _rich_page().split("</style>")[-1]

    assert re.search(r'<td class="mx-cell mx-scored" style="background:[^"]+">0\.96</td>', page)
    forbidden = re.search(r'<td class="mx-cell mx-forbidden"[^>]*>(.*?)</td>', page)
    assert forbidden and forbidden.group(1) == "✕", "the ✕ must be markup, not a ::after"
    assert escape(_FORBIDDEN_REASON, quote=True) in page
    absent = re.search(r'<td class="mx-cell mx-absent"[^>]*>(.*?)</td>', page)
    assert absent and absent.group(1) == "not scored"

    # No cell of any status is empty, and none of them shows a sentinel instead of a word.
    assert not re.findall(r'<td class="mx-cell[^"]*"[^>]*>\s*</td>', page)
    for status in ("mx-forbidden", "mx-absent"):
        assert page.count(f'class="mx-cell {status}"') >= 1


def test_the_losing_kits_stay_collapsed_and_keep_a_reason_a_human_can_read() -> None:
    """One grid for the winner; every sibling is a bar, a score and a sentence behind a disclosure.

    Past two grids the reader is comparing two near-identical tables cell by cell, which is the thing
    the family focus exists to prevent. The reason has to survive the collapse — a row that says only
    "0.61" has told the reader nothing about why 0.61 lost.
    """
    evidence = _pane(_rich_page(), "evidence")

    assert evidence.count('<figure class="sf-card') == 1, "exactly one kit gets the full grid"
    assert evidence.count('<details class="mx-sib">') == 1
    assert "<summary" in evidence and "some reads don&#x27;t fit this variant" in evidence
    assert 'style="--mx-w:61%"' in evidence  # the score, as a length as well as a number
    assert "read 1 is 28 bp; this kit needs 20" in evidence  # the ruled-out family's own words


def test_the_evidence_body_is_as_wide_as_its_panel_and_the_measure_stays_on_the_prose() -> None:
    """A reading measure belongs on prose, stated in ``ch`` — never as a pixel cap on a column of grids.

    This body sat in a 768px column inside a 1112px panel, so ~340px of it was gutter on a page whose
    every other panel runs the full width: a difference that reads as a rendering bug rather than as
    anyone's decision. It was also not doing a measure's job. The only paragraph on the tab is the
    panel's lead sentence, which ``.sf-panel-sub`` already holds to 82ch — narrower than the cap was,
    and *outside* it; what the cap narrowed was a bordered verdict strip, the role x file grid that
    starts scrolling the moment it is denied width, and the score bars a reader compares by length.

    Both halves are asserted and neither is enough alone. A ``max-w-*`` back on this body is the
    defect returning, and a stylesheet that had simply lost its prose measure would let the first
    half pass while the page had stopped capping a line of text anywhere.
    """
    evidence = _pane(_rich_page(), "evidence")

    capped = sorted(c for c in _body_classes(evidence) if c.startswith("max-w-"))
    assert not capped, f"the evidence body is capped inside its own panel again: {capped}"
    assert 'class="sf-panel-sub"' in evidence, (
        "the pane's one paragraph — which is what a measure is for"
    )

    built = (_ASSETS / "report.tw.css").read_text()
    assert re.search(r"\.sf-panel-sub\{[^}]*max-width:\s*\d+ch", built), (
        "the prose measure is gone, so 'the cap moved to where the prose is' describes no page"
    )


def test_the_two_grids_keep_the_pages_standing_guarantees() -> None:
    """Self-contained, no external reference, inside the budget, byte-deterministic — with both grids
    full, which is the shape neither the bulk fixture nor any other test in this file renders."""
    html = _rich_page()

    assert not re.findall(r'(?:src|href)\s*=\s*"(?:https?:)?//[^"]+"', html)
    assert len(html.encode()) < 500_000
    assert html == _rich_page()


# -- modality-aware rendering (ATAC / fragments) --------------------------------------------------


def _retarget_the_recipe(ws: Path, quantification: dict[str, object]) -> None:
    """Rewrite the composed recipe's quantification *value* in place, leaving everything else alone.

    The shared fixture is bulk, because bulk is the branch CI can run headless; standing up a real
    chromap compile would need a second KB spec and an onlist registry for the sake of one branch.
    What the stage diagram reads is the recipe on disk, so swapping that one ``Evidenced`` value is
    the entire ATAC input — and it then arrives at the diagram through ``collect_report``, i.e.
    through the production ``_plan``, which is the whole point of doing it here rather than by
    hand-building a ``PlanView``.
    """
    path = ws / "seqforge" / "processing.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["processing"]["quantification"]["value"] = quantification
    path.write_text(yaml.safe_dump(doc, sort_keys=True))


def _rendered_stage_diagram(ws: Path) -> tuple[AssayReport, str]:
    """The assay, and its stage diagram as the *page* shows it — lowercased, tags stripped.

    Read off `render_html` and not off `assay.pipeline_stages`, because "the diagram renders the RNA
    wording on an ATAC dataset" is a claim about the page. The expectation still comes from
    production (the `AssayReport` returned alongside), which is the half of PR 1's lesson that is
    easy to lose while fixing the other half.
    """
    assay = collect_report(ws).assays[0]
    pane = _pane(render_html(collect_report(ws)), "pipeline")
    diagram = pane[pane.index("What the pipeline will run") : pane.index("Processing choices")]
    # Unescaped, because the page escapes what production wrote: `Align & call fragments` reaches
    # the DOM as `&amp;`, and a comparison against production's own string must read what a browser
    # reads, not what the serialiser emitted.
    return assay, unescape(re.sub(r"<[^>]+>", " ", diagram)).lower()


def test_the_stage_diagram_branches_on_the_typed_quantification_family(own_workspace: Path) -> None:
    """An ATAC recipe renders chromap + fragments, and the axis is the recipe's typed kind.

    This was checked against a hand-built ``PlanView`` whose ``value=`` was the production display
    string copied into the fixture, which is a test that can only ever agree with itself: reword the
    caption in ``_plan`` and the diagram silently reverts to the RNA branch with nothing failing —
    on an ATAC dataset, where "count reads per gene" is not a cosmetic error. So the plan view
    arrives from ``collect_report`` here, the wording is read back off the rendered page, and the
    caption is then reworded on the way into the diagram to prove it is not what the branch reads.
    """
    from seqforge.report.collect import _pipeline_stages

    _retarget_the_recipe(own_workspace, {"kind": "atac"})
    assay, blob = _rendered_stage_diagram(own_workspace)
    plan = assay.plan
    assert plan is not None
    assert plan.quantification_kind == "atac"  # the typed family, carried; not the caption

    assert "fragment" in blob
    assert "chromap" in blob
    assert "count genes" not in blob  # the RNA phrasing must not leak into an ATAC run
    # Every stage production built reached the page, in order — the diagram is not a subset of it.
    for stage in assay.pipeline_stages:
        assert stage.title.lower() in blob and stage.detail.lower() in blob

    reworded = plan.model_copy(
        update={
            "fields": [
                f.model_copy(update={"value": "open-chromatin fragments"})
                if f.label == "quantification"
                else f
                for f in plan.fields
            ]
        }
    )
    assert _pipeline_stages(reworded) == assay.pipeline_stages

    # the RNA branch is unchanged: a solo recipe still renders the STARsolo count-matrix stages
    _retarget_the_recipe(own_workspace, {"kind": "solo", "features": ["Gene", "GeneFull"]})
    solo, solo_blob = _rendered_stage_diagram(own_workspace)
    assert solo.plan is not None and solo.plan.quantification_kind == "solo"
    assert "count" in solo_blob and "starsolo" in solo_blob


def test_the_pipeline_tab_embeds_its_artifacts_and_scrolls_them_in_their_own_region(
    workspace: Path,
) -> None:
    """The two properties this tab must not lose at any viewport width.

    A compiled artifact rides in the page as ``data:`` bytes — a relative link breaks the moment the
    HTML moves off the workspace — and every wide thing on the tab (the recipe table, an artifact's
    inline text) scrolls inside its own box, so the page body never scrolls sideways and the tab is
    readable on a phone. Both are read off the rendered pane.
    """
    pane = _pane(render_html(collect_report(workspace)), "pipeline")

    assert 'download="Snakefile"' in pane and "data:text/plain;base64," in pane
    assert not re.search(r'href="(?!data:)[^"]*(?:Snakefile|\.yaml|\.tsv)"', pane), (
        "an artifact is linked out of the page instead of embedded in it"
    )

    tables = re.findall(r"<table[^>]*>", pane)
    assert tables and len(tables) == pane.count('<div class="sf-scroll-x"><table'), (
        "the recipe table can widen the page instead of scrolling inside its own region"
    )
    pres = re.findall(r"<pre[^>]*>", pane)
    assert pres and all("overflow-auto" in tag for tag in pres), (
        f"an artifact's inline text is not its own scroll region: {pres}"
    )


def test_every_tab_the_page_offers_stays_readable_at_a_narrow_viewport(workspace: Path) -> None:
    """A phone is a real reader, and this is what the page can promise one without a browser.

    Two mechanical properties, over the rendered panes: nothing sets a width in pixels (a fixed width
    is the one thing a narrow viewport cannot recover from), and every multi-column container starts
    at one or two columns and *adds* columns as the viewport grows, rather than starting wide and
    hoping. The wide things that genuinely cannot reflow — the recipe table, an artifact's text —
    are held to their own scroll region by the test above, so the page body never scrolls sideways.
    """
    page = render_html(collect_report(workspace))
    grids = 0

    # Every tab the page actually offers, read off its own tab bar rather than hand-listed. Three
    # tabs were named here while three others were migrated by other tickets, so half the page was
    # exempt from the promise; deriving the list means a seventh tab is in scope the day it renders,
    # and a tab the page declines to show (Results, with no pipeline behind it) is not demanded.
    tabs = re.findall(r'<button class="tab[^"]*" data-tab="(\w+)"', page)
    assert set(tabs) >= {"overview", "flow", "samples", "evidence", "pipeline"}, tabs

    for tab in tabs:
        pane = _pane(page, tab)
        # An absolute width, in a style or as an arbitrary utility. `max-width` is exempt: it only
        # ever narrows, which is the opposite failure, and the abstract's 70ch cap is deliberate.
        assert not re.search(r"(?<!max-)(?:min-)?width\s*:\s*\d+\s*(?:px|pt|in|cm|mm)", pane), tab
        assert not re.search(r"(?<!max-)\b(?:min-)?w-\[\d", pane), (
            f"{tab} pins a width as a utility"
        )

        for classes in re.findall(r'class="([^"]*)"', pane):
            tokens = classes.split()
            if "grid" not in tokens:
                continue
            grids += 1
            base = [t for t in tokens if re.fullmatch(r"grid-cols-\d+", t)]
            assert not base or base == ["grid-cols-2"], (
                f"{tab} starts at {base} columns before any breakpoint: {classes}"
            )
            assert [t for t in tokens if re.fullmatch(r"(sm|md|lg|xl):grid-cols-\d+", t)], (
                f"{tab} has a grid whose column count never changes with the viewport: {classes}"
            )
    assert grids >= 2, "no multi-column container was found at all, so nothing above was checked"


# -- the finished pipeline (the Results tab) -------------------------------------------------------
#
# The page's fourth join, and the only one whose artifact seqforge did not write: once the user
# submits the composed Snakefile, its per-sample QC bundles land beside it and the report reads them.
# The shared fixture composes `map/star`, so these tests swap the copied `.smk` for the single-cell
# one to exercise the richer adapter — `CompiledPipeline.module` reads the module off the file that is
# *present*, which is exactly the seam being exercised. (Both modules report; every registered module
# does. The swap buys the barcode and knee metrics bulk has no vector for, not reporting at all.)

#: One STARsolo `Summary.csv` as `qc_bundle` folds it into the artifact — **every** key `qc.metrics`
#: can read, plus the STAR log the same bundle carries.
#:
#: It used to hold four keys, on the argument that what was under test was the JOIN and the metric
#: table is held to real STARsolo values in `tests/test_workflows.py`. That stopped being true when
#: the Results tab's density lever became the *column count*: a four-key summary renders four
#: columns, the fold never engages, three of the six metric groups never appear, and every assertion
#: below would be about a table `map/starsolo` does not produce. A fixture has to hand the renderer
#: the module's real shape or it is testing a different module.
#:
#: The values are a run with two genuine problems in it — 44.3% of reads in cells and a median 311
#: UMI — so a rendered page carries an `ok` cell, a `none` cell, and one of each tinted verdict.
_QC_SUMMARY: dict[str, object] = {
    "Number of Reads": 412331205,
    "Reads With Valid Barcodes": 0.972113,
    "Estimated Number of Cells": 8842,
    "Reads Mapped to Gene: Unique Gene": 0.641902,
    "Reads Mapped to Genome: Unique": 0.812,
    "Fraction of Unique Reads in Cells": 0.443,  # -> bad
    "Median UMI per Cell": 311,  # -> warn
    "Median Gene per Cell": 902,
    "Total Gene Detected": 18422,
    "Sequencing Saturation": 0.612,
    "Q30 Bases in CB+UMI": 0.941,
    "Q30 Bases in RNA read": 0.912,
}

#: One `Log.final.out`, as STAR itself writes it — the artifact `map/star` reports from, and the
#: block a STARsolo bundle folds in verbatim. Bulk needs no renamed module and no QC bundle, which is
#: what makes it the cheap way to hand the chained `run` verb a *finished* pipeline. The numbers are
#: a healthy run; what is under test is the flag, not the grading, which `tests/test_workflows.py`
#: holds to real STAR values.
_STAR_FINAL_LOG: dict[str, object] = {
    "Number of input reads": 1_000_000,
    "Uniquely mapped reads %": "91.20%",
    "% of reads mapped to multiple loci": "4.10%",
    "% of reads mapped to too many loci": "0.30%",
    "% of reads unmapped: too short": "3.90%",
}


def _finish_a_starsolo_pipeline(
    ws: Path, *, outdir: str = "results", summary: dict[str, object] | None = None
) -> list[str]:
    """Make the workspace's compiled pipeline look like a finished single-cell one. Returns its samples.

    Two edits, both to what is *on disk* rather than to any seqforge decision: the copied module
    becomes `starsolo.smk` (so the owner reports `map/starsolo`), and one QC bundle lands per sample
    the composed config contracted for.

    `summary` swaps the `Summary.csv` block those bundles carry, which is how a caller lands a run
    with a real problem in it — the cross-check tests need a rate that fires a rule, and it has to
    reach them through the production reader rather than through a hand-built `PipelineStats`.
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
                    "summary": {"Gene": _QC_SUMMARY if summary is None else summary},
                    # Folded in verbatim by `qc_bundle`, and the only source of the alignment metrics
                    # — without it a starsolo page has no Alignment band at all.
                    "log_final": _STAR_FINAL_LOG,
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
    assert '<div class="sf-scroll-x"><table' in html  # one row per sample, one column per metric
    assert "<svg" in html and "<polyline" in html  # hand-built inline SVG, no library, no network


def test_a_pipeline_that_ran_and_wrote_garbage_never_renders_as_not_run(
    own_workspace: Path,
) -> None:
    """The page's one job is saying what happened, and this is the case it used to get exactly wrong.

    Every artifact landing corrupt collapsed to `None` in the reader, and `None` is the renderer's
    "has not been run yet" sentence — so a pipeline that ran, burned a night of cluster time and wrote
    unparseable bytes was reported as a pipeline that never started, with the per-sample failures the
    reader had already named thrown away. That is the same false-claim shape the relocation flag
    exists to prevent, on the same page, and nothing was failing.
    """
    from seqforge.pipeline import CompiledPipeline

    samples = _finish_a_starsolo_pipeline(own_workspace)
    pipeline = CompiledPipeline.discover(own_workspace)
    assert pipeline is not None
    for sample in samples:
        (pipeline.results_dir / sample / f"{sample}.qc.json.gz").write_bytes(b"not gzip at all")

    assay = collect_report(own_workspace).assays[0]

    assert assay.pipeline_stats is not None, "a run that wrote garbage is not a run that never ran"
    assert assay.pipeline_stats.n_found == 0
    assert assay.pipeline_stats.n_expected == len(samples)
    assert len(assay.pipeline_stats.notes) == len(samples)

    html = render_html(collect_report(own_workspace))
    assert ">Results</button>" in html
    assert "not been run yet" not in html
    assert "No readable result" in html
    for sample in samples:  # which sample, and what kind of failure -- not just a count
        assert f"{sample}: its QC artifact could not be read (BadGzipFile)" in html
    # No table, and no control offering to unfold one: a disclosure promising columns that do not
    # exist reads as a rendering bug rather than as the honest account this section is. Asserted on
    # the fold control rather than on the removed `<details>`, because the removed one can never come
    # back and a guard that can only pass is the rule it replaced -- `grp-fold` DOES render on the
    # healthy page two tests up, so its absence here is a fact about this branch.
    pane = _pane(html, "results")
    assert "grp-fold" not in pane
    assert "<table" not in pane


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


def _finish_a_bulk_pipeline(ws: Path, *, outdir: str) -> list[str]:
    """Land STAR's own final log per contracted sample under ``<pipeline>/<outdir>/``."""
    from seqforge.pipeline import CompiledPipeline

    pipeline = CompiledPipeline.discover(ws)
    assert pipeline is not None, "the fixture workspace should already be composed"
    samples = pipeline.samples
    assert samples, "the composed config should carry its own sample list"
    for sample in samples:
        out = pipeline.directory / outdir / sample / "Log.final.out"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(f"  {k} |\t{v}\n" for k, v in _STAR_FINAL_LOG.items()))
    return samples


def test_the_run_verb_carries_the_results_flag_so_a_relocated_pipeline_reads_as_run(
    own_workspace: Path,
) -> None:
    """`run` renders the page at the end of a compile, and it must be able to say the pipeline ran.

    It already carries the rest of the machine-fact family — `--fastq-dir`, `--sif-dir` — and this is
    the same kind of fact: where *this machine* put the outputs. Without it a re-run after a pipeline
    relocated by `snakemake --directory` re-rendered the page with the claim that the pipeline has
    not been run, which is a false statement on the one section whose whole job is saying what
    happened.
    """
    samples = _finish_a_bulk_pipeline(own_workspace, outdir="elsewhere")

    result = runner.invoke(
        app,
        [
            "run", str(own_workspace / "s_R1.fastq.gz"), str(own_workspace / "s_R2.fastq.gz"),
            "--organism", "559292",
            "--assembly", "sacCer3",
            "--annotation", "ensembl",
            "--no-llm",
            "--fastq-dir", str(own_workspace),
            "-C", str(own_workspace),
            "--results", "elsewhere",
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.stdout
    html = (own_workspace / "seqforge" / "report.html").read_text()
    assert "not been run yet" not in html  # the false claim this flag exists to stop
    assert ">Results</button>" in html  # the tab is offered, because the pipeline did finish

    stats = collect_report(own_workspace, results_dir=Path("elsewhere")).assays[0].pipeline_stats
    assert stats is not None and stats.module == "map/star"
    assert stats.n_found == len(samples) == stats.n_expected


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


# -- the Results tab as a table ---------------------------------------------------------------------
#
# Sample-by-metric was always a grid, and it used to sit one disclosure below a wall of per-sample
# tiles. Everything below is asserted against `render_html(...)` output and never against a panel
# function: a fragment's return value is not the page, and a test that reaches under the seam can
# pass while encoding the very defect it is named for. That happened here once already (PR 1's stage
# diagram) and it is why this section exists in this shape.


def _render_with_stats(workspace: Path, stats: PipelineStats) -> str:
    """The real page, with the first assay's pipeline stats swapped for `stats`.

    The shared fixture compiles exactly ONE sample, and two of the properties below need more than
    one: a column can only have a gap in it if some other sample filled it, and a sticky row header
    that is the only row proves nothing about scrolling. The stats still come from the production
    reader over real artifacts on disk — only which assay they hang off is arranged here — and the
    assertion is still made against `render_html`, which is the seam that matters.
    """
    report = collect_report(workspace)
    assay = report.assays[0].model_copy(update={"pipeline_stats": stats})
    return render_html(report.model_copy(update={"assays": [assay]}))


def _land_bundles(results: Path, bundles: dict[str, dict[str, object]]) -> PipelineStats:
    """Write one QC bundle per name and read them back through the production adapter."""
    import gzip

    from seqforge.workflows.stats import read_pipeline_stats

    for sample, summary in bundles.items():
        out = results / sample / f"{sample}.qc.json.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(out, "wt", encoding="utf-8") as fh:
            json.dump({"sample": sample, "summary": {"Gene": summary}}, fh)
    stats = read_pipeline_stats("map/starsolo", results, sorted(bundles))
    assert stats is not None
    return stats


def test_the_table_leads_and_sits_behind_no_disclosure(own_workspace: Path) -> None:
    """The view that answers the reader's first question is the view they see first.

    One sample is the case that used to render a strip of tiles and fold the table away, because the
    threshold that chose between them counted the samples that were *found* — so the same dataset
    laid itself out differently depending on how much of its pipeline had finished, and a run with
    six samples showed a wall of boxes where a scannable grid belonged. Both the strips and that
    threshold are gone; what is asserted here is the consequence a reader can see.
    """
    _finish_a_starsolo_pipeline(own_workspace)
    pane = _pane(render_html(collect_report(own_workspace)), "results")

    assert "<table" in pane, "the metrics table is the section"
    assert "<details" not in pane, "nothing on this tab is behind a disclosure widget"
    # The table comes before the one control that folds part of it away, and there is exactly one
    # such control -- not one per group.
    assert pane.index("<table") < pane.index("grp-fold")
    assert pane.count('type="checkbox"') == 1


def test_only_the_two_exceptional_verdicts_are_tinted(own_workspace: Path) -> None:
    """Colour marks exceptions. A cell that is fine, and one nobody could grade, look the same.

    Both halves are needed and neither is enough alone: the page has to put a verdict class on every
    graded cell (asserted from the rendered HTML), and the stylesheet has to give two of the four no
    tint at all (asserted from the built artifact, which is the only place that fact lives). A test
    that read one and not the other would pass on a page whose `ok` cells were pale green.
    """
    _finish_a_starsolo_pipeline(own_workspace)
    pane = _pane(render_html(collect_report(own_workspace)), "results")

    graded = re.findall(r'<td class="lvl-(\w+) lvl-cell', pane)
    assert set(graded) == {"ok", "warn", "bad", "none"}, (
        "the fixture must put all four verdicts on the page, or this asserts about three of them"
    )

    built = (_ASSETS / "report.tw.css").read_text()

    def tint(level: str) -> str:
        """What `.lvl-cell` would paint a cell of this verdict with — grouped selectors and all."""
        found = re.search(rf"\.lvl-{level}(?![\w-])[^{{}}]*\{{[^}}]*--lv-bg:\s*([^;}}]+)", built)
        assert found, f"lvl-{level} declares no --lv-bg, so a cell wearing it paints nothing"
        return found.group(1).strip()

    assert tint("ok") == tint("none") == "transparent", (
        "ok and ungraded carry no tint at all, and are identical — a metric that is fine must look "
        "no different from one nobody could set a defensible bar for"
    )
    assert "transparent" not in {tint("warn"), tint("bad")}
    assert tint("warn") != tint("bad"), "two tinted hues, and they are two"
    # And the tint is the only thing painting a cell: nothing else in the page's own component block
    # gives a metric cell a background, or "no tint" would be one rule away from untrue.
    assert re.search(r"\.lvl-cell\{background:var\(--lv-bg\)\}", built)


def test_a_verdict_is_legible_with_the_colour_taken_away(own_workspace: Path) -> None:
    """Colour-blindness, a greyscale printout, a bad projector: the mark carries the verdict alone.

    The two tinted hues were 0.3 ΔE apart under a deuteranope simulation before this PR re-stepped
    the amber, which is to say they were the same colour — so the mark was never garnish, it was the
    only thing working. Marked cells are also deliberately the minority: marking most of them is the
    same as marking none.
    """
    _finish_a_starsolo_pipeline(own_workspace)
    pane = _pane(render_html(collect_report(own_workspace)), "results")

    cells = re.findall(r'<td class="lvl-(\w+) lvl-cell[^>]*>(.*?)</td>', pane)
    assert cells
    for level, body in cells:
        marked = 'role="img"' in body
        assert marked == (level in {"warn", "bad"}), f"{level} carries the wrong mark: {body!r}"
        if marked:
            assert "aria-label=" in body  # the mark says what it means to a screen reader too


def test_every_metric_group_reaches_the_page_as_a_labelled_band(own_workspace: Path) -> None:
    """The closed set is read out of the ``Literal``, so a seventh member fails here on the day it lands.

    Hand-listing the six would make this a test that agrees with itself and notices nothing — the
    same argument ``test_both_basis_phrasings_cover_the_closed_basis_set`` makes for ``Basis``. What
    it protects against is a group that is graded by an adapter and reaches the page as a span of
    columns under no heading at all, which is worse than no grouping.

    Asserted on the rendered page, so it also holds that the label is *drawn* and not merely mapped.
    """
    from seqforge.report.panels import _GROUP_LABEL
    from seqforge.workflows.metrics import MetricGroup

    members = set(get_args(MetricGroup))
    assert len(members) > 1, "get_args should yield the Literal's members, not an empty tuple"
    assert set(_GROUP_LABEL) == members, "a group with no band label renders as a KeyError"

    _finish_a_starsolo_pipeline(own_workspace)
    pane = _pane(render_html(collect_report(own_workspace)), "results")

    # `map/starsolo` is the module that emits every group, which is what makes it the one to assert
    # exhaustiveness against; a bulk or ATAC page correctly renders fewer bands.
    bands = dict(re.findall(r'<th scope="colgroup"[^>]*data-group="(\w+)"[^>]*>([^<]+)</th>', pane))
    assert bands == {group: _GROUP_LABEL[group] for group in members}
    # Rule and label, never a second hue: a coloured cell must only ever mean "this number is wrong".
    assert not re.findall(r'<th scope="colgroup"[^>]*class="[^"]*(?:bg-|lvl-)', pane)


def test_the_fold_engages_on_the_module_that_needs_it_and_on_no_other(own_workspace: Path) -> None:
    """The density lever is the module's column count, and that is what makes it safe.

    `map/starsolo` declares sixteen columns, which is a spreadsheet, so it folds to its seven
    headline ones behind one control. `map/star` declares five — and only two of them headline, so
    folding would have hidden a bulk run's own read count behind a click for no gain. The threshold
    is over a property of *which module ran*, identical on the first sample and the ninety-sixth;
    the threshold this replaces was over how many samples had finished, which is why the same
    dataset used to change shape while its pipeline was still running.
    """
    _finish_a_starsolo_pipeline(own_workspace)
    solo = _pane(render_html(collect_report(own_workspace)), "results")

    assert "grp-fold" in solo
    headline = re.findall(r'<td class="lvl-\w+ lvl-cell text-right whitespace-nowrap">', solo)
    folded = re.findall(r'<td class="lvl-\w+ lvl-cell[^"]*grp-extra">', solo)
    assert len(headline) == 7 and len(folded) == 9, (len(headline), len(folded))
    assert f"Show all {len(headline) + len(folded)} metrics" in solo
    assert f"Show the {len(headline)} headline metrics" in solo


def test_a_bulk_module_shows_every_column_and_offers_no_disclosure(own_workspace: Path) -> None:
    """Five columns is not a spreadsheet, so there is nothing to fold and no control to say so."""
    _finish_a_bulk_pipeline(own_workspace, outdir="results")
    pane = _pane(render_html(collect_report(own_workspace)), "results")

    assert "<table" in pane
    assert "grp-fold" not in pane, "a disclosure over five columns is a click for nothing"
    assert "grp-extra" not in pane, "nothing is hidden, so nothing wears the fold class"
    assert pane.count('<th scope="col"') == 1 + 5  # the sample id, then every metric STAR wrote


def test_the_sample_column_is_the_only_one_that_stays_put(own_workspace: Path) -> None:
    """One sticky column, and it is the row's identifier — never a frozen block of headline columns.

    Eight pinned columns is the whole width of a laptop screen, which collides head-on with the page
    staying readable at a narrow viewport. So the wide table scrolls inside its own region and the
    identifier rides along; the page body never scrolls sideways.
    """
    results = own_workspace / "seqforge" / "pipeline-elsewhere"
    stats = _land_bundles(results, {"S1": _QC_SUMMARY, "S2": _QC_SUMMARY, "S3": _QC_SUMMARY})
    pane = _pane(_render_with_stats(own_workspace, stats), "results")

    assert 'class="sf-scroll-x"' in pane  # the wide table has its own scroll region
    # One header cell plus one row header per sample, and not one metric cell among them.
    assert pane.count("sf-col-sticky") == 1 + 3
    assert not re.findall(r"<td[^>]*sf-col-sticky", pane)


def _land_bundles_counted_off(results: Path, features: dict[str, str]) -> PipelineStats:
    """One bundle per sample, each counted off the feature named for it, read back by the adapter.

    `_land_bundles` writes every sample off `Gene`, which is one note for the whole run — the case a
    single shared footnote would happen to get right. The case the page has to survive is the other
    one: samples counted off DIFFERENT features, where one line under the table would silently claim
    all of them were counted the same way.
    """
    import gzip

    from seqforge.workflows.stats import read_pipeline_stats

    for sample, feature in features.items():
        out = results / sample / f"{sample}.qc.json.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(out, "wt", encoding="utf-8") as fh:
            json.dump({"sample": sample, "summary": {feature: _QC_SUMMARY}}, fh)
    stats = read_pipeline_stats("map/starsolo", results, sorted(features))
    assert stats is not None
    return stats


def _counting_block(pane: str) -> str:
    """The block under the metrics table that says what its numbers were read off, or `""`."""
    marker = "Where these numbers came from"
    return pane[pane.index(marker) :].split("</div>")[0] if marker in pane else ""


def test_the_pinned_identifier_column_carries_the_identifier_and_nothing_else(
    own_workspace: Path,
) -> None:
    """The sticky column is one identifier wide because one identifier is all it holds.

    A per-sample note used to render inside that `<th>`, and a sentence in a column sized to a sample
    id wrapped to three lines: the column collapsed to about 115px and every row it touched grew,
    tinted verdict cell included. A `min-width` floor was tried and reverted on measurement — past
    7rem the table is forced to scroll sideways and the verdict cell is the first thing cut, which
    trades a cosmetic problem for a functional one. So the note left the column instead.
    """
    results = own_workspace / "seqforge" / "pipeline-elsewhere"
    stats = _land_bundles_counted_off(results, {"S1": "Gene", "S2": "GeneFull", "S3": "Gene"})
    pane = _pane(_render_with_stats(own_workspace, stats), "results")

    ids = re.findall(r'<th scope="row" class="sf-col-sticky">(.*?)</th>', pane)
    assert ids == [sample.sample_id for sample in stats.samples], (
        "the row header holds the identifier and nothing else — no nested markup, no sentence"
    )
    # And the note was not simply deleted, which is how this could pass while being wrong.
    assert "counted from the Gene feature" in pane
    assert "counted from the GeneFull feature" in pane


def test_two_ways_of_counting_stay_two_claims_naming_the_samples_that_carry_each(
    own_workspace: Path,
) -> None:
    """Attribution survives the move: one line per distinct note, and each names who it holds for.

    A single footnote for the table is the obvious way to get the sentence out of the sticky column,
    and it is the one thing that may not happen — two samples can be counted off different features,
    and one line would claim all of them were counted the same way. Grouping says exactly as much as
    the data supports and no more.
    """
    results = own_workspace / "seqforge" / "pipeline-elsewhere"
    stats = _land_bundles_counted_off(results, {"S1": "Gene", "S2": "GeneFull", "S3": "Gene"})
    pane = _pane(_render_with_stats(own_workspace, stats), "results")
    lines = re.findall(r"<li[^>]*>(.*?)</li>", _counting_block(pane))

    assert len(lines) == 2, lines
    (gene,) = [line for line in lines if "counted from the Gene feature" in line]
    (full,) = [line for line in lines if "counted from the GeneFull feature" in line]
    assert "S1, S3" in gene and "S2" not in gene
    assert "S2" in full and "S1" not in full and "S3" not in full
    # Said ONCE on the tab, in the one place that can say who it holds for: the run-state block above
    # the table used to print the same sentences with no samples attached to them.
    assert pane.count("counted from the GeneFull feature") == 1
    # A footnote, not a caption: the legend is read before the numbers because it says how a tint
    # grades, and this is read after one of them surprises you.
    assert pane.index("<table") < pane.index("Where these numbers came from")


def test_a_note_a_whole_plate_shares_is_one_line_and_a_lone_sample_is_still_named(
    own_workspace: Path,
) -> None:
    """Where the data does support "counted the same way", the page says so in words.

    Ninety-six ids under a table that already lists ninety-six ids is the wall this block exists to
    avoid, and printing them would make it grow with the plate instead of with the number of ways the
    run was counted — every id appears at most once across the whole block, which is what keeps it off
    the page's size budget. The other direction is asserted on the same rule: a run with one sample
    names it, because "all 1 samples" is a sentence about a plate written for a single well.
    """
    results = own_workspace / "seqforge" / "pipeline-elsewhere"
    plate = {f"{row}{col:02d}": "Gene" for row in "ABCDEFGH" for col in range(1, 13)}
    stats = _land_bundles_counted_off(results, plate)
    pane = _pane(_render_with_stats(own_workspace, stats), "results")
    block = _counting_block(pane)

    assert len(plate) == 96
    (line,) = re.findall(r"<li[^>]*>(.*?)</li>", block)
    assert "counted from the Gene feature" in line and f"all {len(plate)} samples" in line
    assert not [well for well in plate if well in block], "the block reads the plate back at itself"
    assert '<th scope="row" class="sf-col-sticky">A01</th>' in pane, "the ids are in the table"

    _finish_a_starsolo_pipeline(own_workspace)
    lone = _counting_block(_pane(render_html(collect_report(own_workspace)), "results"))
    assert "counted from the Gene feature" in lone and "all 1 samples" not in lone


def test_on_a_partial_run_all_ranges_over_the_rows_shown_and_says_so(
    own_workspace: Path,
) -> None:
    """ "all N samples" counts the rows under it, not the plate the config contracted.

    A caption can only speak for an artifact somebody read, so N is the parsed samples — which on a
    partial run is narrower than the banner directly above ("2 of 5 samples finished"). Two numbers a
    reader has to reconcile is one too many, so the word *shown* does the reconciling. The complete
    case must NOT carry it: there is nothing to disambiguate when every contracted sample landed, and
    a qualifier that never comes off is noise.
    """
    from seqforge.workflows.stats import read_pipeline_stats

    results = own_workspace / "seqforge" / "pipeline-elsewhere"
    _land_bundles_counted_off(results, {"S1": "Gene", "S2": "Gene"})
    partial = read_pipeline_stats("map/starsolo", results, ["S1", "S2", "S3", "S4", "S5"])
    assert partial is not None and (partial.n_found, partial.n_expected) == (2, 5)

    block = _counting_block(_pane(_render_with_stats(own_workspace, partial), "results"))
    assert "all 2 samples shown" in block, block
    assert "all 5 samples" not in block, "N is the rows read, never the plate contracted"

    whole = read_pipeline_stats("map/starsolo", results, ["S1", "S2"])
    assert whole is not None and whole.complete
    done = _counting_block(_pane(_render_with_stats(own_workspace, whole), "results"))
    assert "all 2 samples" in done and "shown" not in done


def test_a_sample_missing_a_metric_leaves_a_gap_in_that_column(own_workspace: Path) -> None:
    """A gap, never a zero — and never a column dropped for everyone because one sample was thin.

    The column set is a union across samples, so a sample whose STARsolo run wrote fewer rows keeps
    its blanks and every other sample keeps its numbers. A zero here would be a number a reader would
    act on, and the tool did not write it.
    """
    results = own_workspace / "seqforge" / "pipeline-elsewhere"
    thin: dict[str, object] = {"Number of Reads": 10, "Sequencing Saturation": 0.5}
    stats = _land_bundles(results, {"S1": thin, "S2": _QC_SUMMARY})
    pane = _pane(_render_with_stats(own_workspace, stats), "results")

    rows = re.findall(r"<tr><th scope=\"row\"[^>]*>(.*?)</tr>", pane)
    assert len(rows) == 2
    thin_row, full_row = rows
    assert "S1" in thin_row and "S2" in full_row
    # Same number of cells in both rows -- the column survives -- and the thin one is mostly gaps.
    assert thin_row.count("<td") == full_row.count("<td") == len(stats.columns)
    assert thin_row.count(">—</td>") == len(stats.columns) - 2
    assert ">0</td>" not in thin_row and ">0.0%</td>" not in thin_row  # never a manufactured zero


def test_a_partial_run_still_renders_what_landed_and_says_how_much_did(own_workspace: Path) -> None:
    """Unchanged by the redesign, and re-asserted because a table is a new place to lose it.

    A listing can say what landed and can never say what is missing, so the count comes from the
    composed config's own sample list. Two of three finished: both rows render, and the page says so
    rather than looking like a complete run with a short table.
    """
    results = own_workspace / "seqforge" / "pipeline-elsewhere"
    stats = _land_bundles(results, {"S1": _QC_SUMMARY, "S2": _QC_SUMMARY})
    partial = stats.model_copy(update={"n_expected": 3})
    pane = _pane(_render_with_stats(own_workspace, partial), "results")

    assert "2 of 3 samples finished" in pane
    assert 'class="lvl-warn lvl-state' in pane  # a partial run is one of the two tinted states
    assert pane.count('<th scope="row"') == 2  # what landed is there, in full


# -- the cross-check: which decision does a bad number implicate? -----------------------------------
#
# The rules themselves are pure and are held to literal values in `tests/test_workflows.py`. What is
# under test here is the other three seams: the collector attributing a finding to the value the
# workspace currently carries, the page drawing it beside the numbers that produced it, and the verb
# handing it to a machine. Everything visual is asserted against `render_html`.

#: The same finished run with #215's real barcode rate in it — 0.076% valid barcodes, which is not a
#: bad library but a whitelist that does not belong to these reads.
_BROKEN_BARCODES: dict[str, object] = {**_QC_SUMMARY, "Reads With Valid Barcodes": 0.000762759}


def _finish_a_starsolo_pipeline_over(ws: Path, summaries: dict[str, dict[str, object]]) -> None:
    """Make the composed pipeline look like a finished MULTI-sample starsolo run.

    The shared fixture compiles exactly one sample, and scope is a claim about how many samples fired
    out of how many landed — a distinction one sample cannot draw. So the composed config's own
    `samples` list is rewritten and one bundle lands per name. Both edits are to what is *on disk*,
    the counts still come from the artifact the pipeline was handed rather than from a listing, and
    nothing the compiler decided is touched.
    """
    import gzip

    from seqforge.pipeline import CompiledPipeline

    pipeline = CompiledPipeline.discover(ws)
    assert pipeline is not None, "the fixture workspace should already be composed"
    for smk in pipeline.directory.glob("*.smk"):
        smk.rename(smk.with_name("starsolo.smk"))
    config = pipeline.config
    config["samples"] = sorted(summaries)
    pipeline.config_path.write_text(yaml.safe_dump(config, sort_keys=True))

    for sample, summary in summaries.items():
        out = pipeline.directory / str(config.get("outdir") or "results") / sample
        out.mkdir(parents=True, exist_ok=True)
        with gzip.open(out / f"{sample}.qc.json.gz", "wt", encoding="utf-8") as fh:
            json.dump({"sample": sample, "summary": {"Gene": summary}}, fh)


def _alert_block(html: str) -> str:
    """The Results tab's alert block as the page renders it, or `""` when there is none."""
    pane = _pane(html, "results")
    marker = "What looks wrong"
    if marker not in pane:
        return ""
    return pane[pane.index(marker) : pane.index("How each number reads")]


def test_a_healthy_run_produces_no_alerts_and_renders_no_alert_block_at_all(
    own_workspace: Path,
) -> None:
    """Seeing an alert has to mean something, so a good run renders nothing — not an empty section.

    "No alerts" printed on every page is a heading a reader learns to skip, and the day one appears
    it appears in a place their eye has already been trained to pass over. The absence is the signal.
    """
    _finish_a_starsolo_pipeline(own_workspace)

    assay = collect_report(own_workspace).assays[0]
    page = render_html(collect_report(own_workspace))

    assert assay.pipeline_stats is not None and assay.pipeline_stats.complete
    assert assay.pipeline_stats.findings == [] and assay.alerts == []
    assert _alert_block(page) == ""
    assert "lvl-state" in _pane(page, "results"), (
        "the run-state block still renders, or this asserts nothing about the alert block"
    )


def test_an_alert_names_the_decision_and_the_value_the_workspace_currently_carries(
    own_workspace: Path,
) -> None:
    """Attribution, at the seam that owns it: the collector already holds the manifest and the recipe.

    "Your chemistry call looks wrong" is only actionable once it says what the call currently IS, and
    a rule cannot know that — it is pure over metrics. So the expected values are read out of the
    manifest on disk rather than restated here: a test that spelled `bulk-rnaseq` would keep
    passing against a collector that had stopped reading the manifest at all.
    """
    _finish_a_starsolo_pipeline(own_workspace, summary=_BROKEN_BARCODES)
    manifest = yaml.safe_load((own_workspace / "seqforge" / "manifest.yaml").read_text())
    chemistry = manifest["library"]["chemistry"]["value"][0]
    roles = {f["basename"]: f["read_id"] for f in manifest["library"]["files"]}

    (alert,) = collect_report(own_workspace).assays[0].alerts

    assert alert.id == "starsolo.valid-barcodes-near-zero"
    assert alert.severity == "likely" and alert.scope == "systematic"
    named = {ref.decision: ref for ref in alert.implicates}
    assert set(named) == {"chemistry", "read_roles"}
    assert "library.chemistry" in named["chemistry"].label
    assert named["chemistry"].value.startswith(chemistry)
    assert "library.files" in named["read_roles"].label
    for basename, read_id in roles.items():
        assert f"{read_id} = {basename}" in named["read_roles"].value


def test_an_alert_renders_beside_the_numbers_that_produced_it_and_nowhere_else(
    own_workspace: Path,
) -> None:
    """The claim and its evidence are one scroll apart, and the reader can check one against the other.

    Between the pipeline's own state and the legend that says how the numbers below grade: an alert is
    a claim ABOUT that table, so it leads it. Everything asserted here is read off the rendered page —
    including that the alert reaches no other tab, since a decision-shaped claim would look at home on
    Pipeline and would then be a second place the same fact lives.
    """
    _finish_a_starsolo_pipeline(own_workspace, summary=_BROKEN_BARCODES)
    page = render_html(collect_report(own_workspace))
    pane = _pane(page, "results")
    block = _alert_block(page)

    assert block, "the broken fixture must raise one, or this test asserts about an absent section"
    assert pane.index("lvl-state") < pane.index("What looks wrong") < pane.index("<table")
    assert pane.index("What looks wrong") < pane.index("How each number reads")
    # The claim, the mark that survives a greyscale printout, the values, the remedy, and the id a
    # machine consumer tracks it by — all of it on the page, none of it colour-only.
    assert "Almost no read carries a barcode" in block
    assert '<span class="lvl-flag" role="img" aria-label="this run is probably wrong">!!' in block
    assert "0.1%" in block and "matched the whitelist" in block
    assert "Check which kit this library really is" in block
    assert "starsolo.valid-barcodes-near-zero" in block
    assert "What looks wrong" not in _pane(page, "pipeline") + _pane(page, "overview")


def test_a_systematic_alert_and_an_isolated_one_are_two_different_claims_on_the_page(
    own_workspace: Path,
) -> None:
    """Systematic points at a decision; isolated points at a well, and the page must say which.

    A reader cannot draw that from a list of sample ids — on a plate, 96 ids and 94 ids look the same
    — so the count of how many fired out of how many LANDED is written in words. Driven twice over
    one workspace so the two renders differ only in which bundles are broken.
    """
    _finish_a_starsolo_pipeline_over(
        own_workspace, {"S1": _BROKEN_BARCODES, "S2": _BROKEN_BARCODES, "S3": _BROKEN_BARCODES}
    )
    every = _alert_block(render_html(collect_report(own_workspace)))

    _finish_a_starsolo_pipeline_over(
        own_workspace, {"S1": _BROKEN_BARCODES, "S2": _QC_SUMMARY, "S3": _QC_SUMMARY}
    )
    one = _alert_block(render_html(collect_report(own_workspace)))

    assert "every sample that finished (3 of 3)" in every
    assert "1 of 3 samples that finished" in one
    # And the per-sample evidence survives the grouping: an alert that collapsed three samples to one
    # number would have thrown away the evidence for its own claim.
    assert every.count("<li>") > one.count("<li>")
    for sample in ("S1", "S2", "S3"):
        assert sample in every
    assert "S2" not in one and "S3" not in one


def test_a_partial_run_still_raises_the_alert_it_would_have_raised_complete(
    own_workspace: Path,
) -> None:
    """Nobody should wait for a full plate to learn the chemistry call was wrong.

    Scope is computed against what LANDED and never against what was contracted, so two of three
    samples that both fired is *systematic* — every sample there was to fire on did. The contracted
    count is still on the page, one block up, where it says how much of the run finished.
    """
    _finish_a_starsolo_pipeline_over(
        own_workspace, {"S1": _BROKEN_BARCODES, "S2": _BROKEN_BARCODES}
    )
    pipeline_dir = next((own_workspace / "seqforge" / "pipeline").iterdir())
    config = yaml.safe_load((pipeline_dir / "config.yaml").read_text())
    config["samples"] = ["S1", "S2", "S3"]
    (pipeline_dir / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=True))

    page = render_html(collect_report(own_workspace))
    pane = _pane(page, "results")

    assert "2 of 3 samples finished" in pane  # the run state, unchanged
    assert "every sample that finished (2 of 2)" in _alert_block(page)


def test_alerts_reach_the_report_verbs_machine_output_with_their_stable_ids(
    own_workspace: Path,
) -> None:
    """A finding that exists only inside an HTML document is not machine-accessible, and the CLI is
    this project's interface.

    Beside `pipeline_stats` and never folded into `exit`: an alert is advisory, so a consumer reads
    "the compile succeeded" and "these decisions look wrong" as two answers. The stable id is what
    lets one be suppressed or tracked across runs, so it is asserted by name.
    """
    _finish_a_starsolo_pipeline(own_workspace, summary=_BROKEN_BARCODES)

    result = runner.invoke(app, ["report", "-C", str(own_workspace), "--no-timestamp"])

    assert result.exit_code == 0, result.stdout
    conclusion = json.loads(result.stdout)["conclusion"][0]
    assert conclusion["exit"] == 0 and conclusion["kind"] == "compiled"
    (alert,) = conclusion["alerts"]
    assert alert["id"] == "starsolo.valid-barcodes-near-zero"
    assert alert["severity"] == "likely" and alert["scope"] == "systematic"
    assert alert["samples"] and len(alert["measured"]) == len(alert["samples"])
    assert {d["decision"] for d in alert["implicates"]} == {"chemistry", "read_roles"}
    assert all(d["value"] for d in alert["implicates"])


def test_an_alert_never_rewrites_the_manifest_or_the_recipe(own_workspace: Path) -> None:
    """The whole path over one workspace, twice — and the two artifacts come back byte-identical.

    This is the compiler's first backward edge, and the constraint that makes it safe is that it is
    advisory: the dataset manifest is immutable and content-addressed, and a pairing's identity is
    hashed at compile time, so evidence arriving after that may inform the user and may not silently
    move either artifact. That must be a test rather than a promise.

    The discriminator is the second half: the same run, the same verb, and the only difference is a
    barcode rate that raises an alert. Bytes identical, exit code identical, conclusion identical —
    with an alert on the page that was not there before. Without that half this would pass on a build
    where the cross-check never ran at all.
    """
    seqforge_dir = own_workspace / "seqforge"
    before = {p: p.read_bytes() for p in (seqforge_dir / "manifest.yaml", seqforge_dir / "processing.yaml")}  # fmt: skip

    _finish_a_starsolo_pipeline(own_workspace)
    healthy = runner.invoke(app, ["report", "-C", str(own_workspace), "--no-timestamp"])
    healthy_conclusion = json.loads(healthy.stdout)["conclusion"][0]

    _finish_a_starsolo_pipeline(own_workspace, summary=_BROKEN_BARCODES)
    alerted = runner.invoke(app, ["report", "-C", str(own_workspace), "--no-timestamp"])
    alerted_conclusion = json.loads(alerted.stdout)["conclusion"][0]

    assert healthy_conclusion["alerts"] == [] and alerted_conclusion["alerts"], (
        "the two runs must differ in exactly the thing under test, or nothing here discriminates"
    )
    assert alerted.exit_code == healthy.exit_code == 0
    assert alerted_conclusion["exit"] == healthy_conclusion["exit"]
    assert alerted_conclusion["kind"] == healthy_conclusion["kind"]
    assert {p: p.read_bytes() for p in before} == before


def test_every_decision_an_alert_can_name_is_resolvable_to_a_value(own_workspace: Path) -> None:
    """`Decision` is a closed set, guarded from its own `Literal` — a member with no resolver is mute.

    Two halves, and neither is enough. The table must be total over the literal (so #222's `strand`
    cannot ship as a field name with nothing beside it), and the resolvers must actually answer
    against a real workspace — a table full of functions that all return `None` would satisfy the
    first half and render nothing.

    The context is built to be ANSWERABLE, which is a third claim and it is deliberate: a resolver
    that returned a value for a workspace that cannot hold one would be inventing it, so the fixture
    is made to hold one rather than the resolver made to guess. The shared workspace compiles a bulk
    recipe and `solo_features` is a STARsolo-only decision, so the recipe's counting decision is
    swapped for the one a STARsolo pipeline carries — the same class of edit
    `_finish_a_starsolo_pipeline` already makes to the module on disk, one artifact up.
    """
    from seqforge.report.collect import _DECISION_RESOLVERS
    from seqforge.workflows.metrics import Decision

    members = set(get_args(Decision))
    assert len(members) > 1, "get_args should yield the Literal's members, not an empty tuple"
    assert set(_DECISION_RESOLVERS) == members, "a decision with no resolver renders as a bare name"

    from seqforge.models.dataset import DatasetManifest

    manifest = DatasetManifest.model_validate(
        yaml.safe_load((own_workspace / "seqforge" / "manifest.yaml").read_text())
    )
    ctx = _decision_context(own_workspace, manifest)
    for decision, resolve in _DECISION_RESOLVERS.items():
        ref = resolve(ctx)
        assert ref is not None and ref.decision == decision
        assert ref.label and ref.value, f"{decision} resolved to a label with no value beside it"


def test_every_severity_the_page_can_draw_wears_a_verdict_the_palette_already_had() -> None:
    """The third closed set this page renders, and PR 3's colour budget is zero.

    An alert is an exception by construction — it exists only because something looks wrong — so it
    wears the verdict pair rather than a third hue, and the mark that survives colour-blindness comes
    from the same table the metric cells use. Derived from the `Literal` for the reason `Basis` and
    `Level` are: a restatement here would be a test that agrees with itself.
    """
    from seqforge.report.panels import _LEVEL_FLAG, _LEVEL_PHRASE, _SEVERITY_LEVEL
    from seqforge.workflows.metrics import SEVERITY_PHRASE, Severity

    members = set(get_args(Severity))
    assert len(members) > 1, "get_args should yield the Literal's members, not an empty tuple"

    assert set(_SEVERITY_LEVEL) == members, "a severity with no verdict class renders untinted"
    assert set(SEVERITY_PHRASE) == members
    # No new hue and no new mark: every verdict an alert wears is one the metrics table already
    # wears, and every one of them carries a non-colour flag.
    assert set(_SEVERITY_LEVEL.values()) <= set(_LEVEL_PHRASE)
    assert set(_SEVERITY_LEVEL.values()) <= set(_LEVEL_FLAG)
    assert _SEVERITY_LEVEL["likely"] == "bad" and _SEVERITY_LEVEL["possible"] == "warn"


def test_an_alerts_alternative_reaches_the_page_and_a_nameless_decision_draws_no_heading(
    own_workspace: Path,
) -> None:
    """Two card branches proven before a rule reached them — `change_to` now has one, a heading none.

    `change_to` is filled only where the alternative is genuinely enumerable — a role assignment has
    one swap, a chemistry call has a KB's worth — so the chemistry rule leaves it empty and the
    markup that renders it would otherwise ship unexercised. And an alert whose decisions all failed
    to resolve must draw no "Points at" heading at all: a heading over an empty list is the same lie
    as a field name with no value beside it.

    Built as models and rendered through `render_html`, the way the Samples and Evidence tabs are
    driven: the assertion is still made against the page, and the input is a shape a rule is entitled
    to produce rather than one this fixture can be made to produce.
    """
    _finish_a_starsolo_pipeline(own_workspace)
    report = collect_report(own_workspace)
    swapped = Alert(
        id="demo.enumerable",
        title="The two reads look the wrong way round",
        severity="possible",
        scope="isolated",
        samples=["S1"],
        n_samples=2,
        measured=["S1 measured something"],
        implicates=[
            DecisionRef(
                decision="read_roles",
                label="read roles (manifest `library.files[].read_id`)",
                value="R1 = a.fastq.gz, R2 = b.fastq.gz",
                change_to="R1 = b.fastq.gz, R2 = a.fastq.gz",
            )
        ],
        remedy="swap them and compose again",
    )
    nameless = swapped.model_copy(update={"id": "demo.nameless", "implicates": []})
    assay = report.assays[0].model_copy(update={"alerts": [swapped, nameless]})

    block = _alert_block(render_html(report.model_copy(update={"assays": [assay]})))

    assert block.count("demo.") == 2, "both cards render, or one branch is asserted twice"
    assert "the alternative is <b>R1 = b.fastq.gz, R2 = a.fastq.gz</b>" in block
    assert block.count("Points at") == 1, "the alert that resolved nothing draws no heading"


def test_the_page_keeps_its_standing_guarantees_with_an_alert_rendered(own_workspace: Path) -> None:
    """Self-contained, no external reference, inside the budget, byte-deterministic — with alerts on.

    The four are asserted elsewhere against a page that has no alert block, which would leave exactly
    the new markup unchecked for the failures they exist to catch. Determinism is the one that bites
    here: alerts are grouped out of a dict, and a grouping that leaked iteration order would render a
    different page on a second read of the same workspace.
    """
    _finish_a_starsolo_pipeline_over(
        own_workspace, {"S1": _BROKEN_BARCODES, "S2": _QC_SUMMARY, "S3": _BROKEN_BARCODES}
    )

    html = render_html(collect_report(own_workspace))

    assert _alert_block(html), (
        "the fixture must raise one, or the guarantees are re-proved unchanged"
    )
    assert not re.findall(r'(?:src|href)\s*=\s*"(?:https?:)?//[^"]+"', html)
    assert len(html.encode()) < 500_000
    assert html == render_html(collect_report(own_workspace))


# -- the vendored stylesheet's drift guards ---------------------------------------------------------
#
# `report.tw.css` is Tailwind's output, and Tailwind purges: it contains only what was literally
# present in the sources it was pointed at when the build ran. Editing `panels.py` or
# `report.src.css` and forgetting to rebuild is therefore a SILENT failure — the page keeps
# rendering and one rule is quietly absent — and the build needs npm, so CI cannot just re-run it.
# Two guards close that from both ends, and `test_the_two_drift_guards_fire_...` proves each one's
# matcher actually rejects a drifted input rather than only accepting today's.

_ASSETS = Path(__file__).resolve().parents[1] / "src/seqforge/report/assets"

#: The modules the purge is pointed at (`@source` in `report.src.css`), which is also where the
#: guard below must look: a class is invisible to Tailwind and to us for exactly the same reasons.
_CLASS_BEARING_SOURCES = (
    Path(__file__).resolve().parents[1] / "src/seqforge/report/panels.py",
    Path(__file__).resolve().parents[1] / "src/seqforge/report/render.py",
    _ASSETS / "report.js",
)

#: Classes the page carries that deliberately have NO rule in either stylesheet: structural hooks a
#: script or a reader selects on, and one SVG group whose children are what gets styled. They are
#: named because the guard cannot tell a hook from drift by looking, and an undeclared exception is
#: a hole. The guard asserts this set in both directions — give one of them a rule and it says so,
#: so the list cannot quietly become the place unstyled classes go to hide.
_UNSTYLED_HOOKS = {
    "assay": "<section class='assay' data-assay=N> — the pane the assay switcher shows and hides",
}


def _literal_classes(*texts: str) -> set[str]:
    """Every LITERAL class name in `texts` — a rendered page, or a module the purge reads.

    A token carrying an f-string hole (``lvl-{esc(m.level)}``, ``tab{" active" if …}``) is dropped,
    because it is not a literal — which is the whole reason a computed class has to be a declared
    component instead of a utility. Both quote styles are read: a fragment nested inside a
    double-quoted Python string writes ``class='x'``, and a guard that only saw ``class="x"`` would
    quietly stop checking exactly those. ``className =`` is read too, for the popover `report.js`
    builds at runtime.
    """
    found: set[str] = set()
    for text in texts:
        for group in re.findall(r"""class(?:Name)?\s*=\s*["']([^"'\n]*)["']""", text):
            found |= {c for c in group.split() if "{" not in c and "}" not in c}
    return found


def _declared_components(src_css: str) -> set[str]:
    """The class selectors the ``@layer components`` block of a build input declares.

    Comments are stripped first, so a prefix named in prose (``lvl-*`` is #217's) is not mistaken
    for a component this file declares.
    """
    source = re.sub(r"/\*.*?\*/", "", src_css, flags=re.S)
    return set(re.findall(r"\.([a-zA-Z][\w-]*)", source[source.index("@layer components") :]))


def _classes_with_no_rule(classes: set[str], sheets: list[str]) -> list[str]:
    """Those of `classes` that NONE of `sheets` writes a rule for.

    Tailwind escapes `:` `/` `.` and friends in its selectors, so `md:grid-cols-2` is emitted as
    `.md\\:grid-cols-2`; the substitution below reproduces that before matching. The trailing
    `(?![\\w-])` is what stops `.sf-card` from satisfying `sf-card-2`.
    """
    return sorted(
        c
        for c in classes
        if not any(
            re.search(r"\." + re.escape(re.sub(r"([:/.\[\]()%,])", r"\\\1", c)) + r"(?![\w-])", s)
            for s in sheets
        )
    )


def test_every_class_the_page_uses_has_a_rule_in_a_stylesheet(own_workspace: Path) -> None:
    """Guard one: nothing the page can wear is unstyled.

    Adding ``class="mt-8"`` to a fragment and not rebuilding is silent — the page renders and one
    rule is absent. This collects every literal class the page can carry (the rendered page with the
    heaviest tab on, plus the `@source` modules, so a branch this fixture never reaches is still
    checked) and fails if any of them has no rule in the built stylesheet.

    It used to take a *union* of two sheets, because a hand-written one was inlined beside the build
    while the page moved onto it — and a union is a weak guard: an empty build would have satisfied
    it. That sheet is gone, so the union is one file and the guard is now exactly as strong as it
    reads. It also subsumes the three per-pane guards that were deleted with it: a class left on an
    element because "the old sheet still styles it" now has a rule nowhere, and lands here by name.
    """
    _finish_a_starsolo_pipeline(own_workspace)
    page = render_html(collect_report(own_workspace))

    used = _literal_classes(
        page.split("</style>")[-1], *(p.read_text() for p in _CLASS_BEARING_SOURCES)
    )
    assert len(used) > 50, "the page and its fragments really do carry classes"

    sheet = (_ASSETS / "report.tw.css").read_text()
    missing = set(_classes_with_no_rule(used, [sheet]))

    unexpected = sorted(missing - set(_UNSTYLED_HOOKS))
    assert not unexpected, (
        f"classes with no rule in the stylesheet: {unexpected} — rebuild report.tw.css "
        "(see assets/VENDOR.md), or declare the class in _UNSTYLED_HOOKS if it is a bare hook"
    )
    # The other direction, so the exception list cannot rot into a hiding place: a hook that has
    # since acquired a rule is no longer an exception and must leave the list.
    styled = sorted(set(_UNSTYLED_HOOKS) - missing)
    assert not styled, f"these now have a rule and are not hooks any more: {styled}"


def test_the_built_stylesheet_carries_every_component_its_source_declares() -> None:
    """Guard two, from the CSS side: the built artifact is not behind its own input.

    The guard above catches a new class in `panels.py`; this one catches a new component in
    `report.src.css` that was edited and never compiled. Together they mean the built file cannot
    silently fall behind either of its inputs — which matters because the build needs npm and so
    cannot run in CI.
    """
    declared = _declared_components((_ASSETS / "report.src.css").read_text())
    assert {"sf-page", "sf-card", "sf-scroll-x"} <= declared, (
        "the source really does declare the shell components this asserts about"
    )

    built = (_ASSETS / "report.tw.css").read_text()
    missing = _classes_with_no_rule(declared, [built])
    assert not missing, (
        f"declared in report.src.css but absent from report.tw.css: {missing} — "
        "rebuild it (see assets/VENDOR.md)"
    )


def test_the_two_drift_guards_fire_on_a_drifted_input_and_stay_silent_on_a_clean_one() -> None:
    """The discriminator. A guard that only ever passes is the rule it was supposed to replace.

    Both guards above are one matcher over two inputs, so this drives that matcher directly: a
    synthetic clean input must come back empty, and a synthetic drifted one must come back naming
    exactly what drifted. Nothing here reads a real asset, so it keeps discriminating whatever the
    checked-in stylesheets happen to contain.
    """
    clean = ".sf-card{border:1px solid}.tab{color:red}"

    # Guard one's matcher: silent on a class that has a rule, loud on one that does not.
    assert _classes_with_no_rule({"sf-card", "tab"}, [clean]) == []
    assert _classes_with_no_rule({"sf-card", "mt-8"}, [clean]) == ["mt-8"]
    # The union is a real union — a rule in EITHER sheet satisfies it, which is the whole reason
    # the report's guard differs from the eval report's.
    assert _classes_with_no_rule({"mt-8"}, [clean, ".mt-8{margin-top:2rem}"]) == []
    # ...and a prefix is not a match, or every `sf-*` class would pass on the strength of `.sf-card`.
    assert _classes_with_no_rule({"sf-card-2"}, [clean]) == ["sf-card-2"]
    # Tailwind's escaped selectors are matched as Tailwind writes them.
    assert _classes_with_no_rule({"md:flex"}, [r".md\:flex{display:flex}"]) == []

    # Guard two's matcher: a component the source declares and the build does not carry.
    src = "@layer components {\n  .sf-page { color: red }\n  .sf-new { color: blue }\n}"
    assert _declared_components(src) == {"sf-page", "sf-new"}
    assert _classes_with_no_rule(_declared_components(src), [".sf-page{color:red}"]) == ["sf-new"]
    assert _classes_with_no_rule(_declared_components(src), [".sf-page{}.sf-new{}"]) == []
    # A selector written in prose is not a declaration — otherwise the prefix table in the source's
    # header comment would enrol #217's components into this file's.
    assert _declared_components("/* .lvl-ok is #217's */\n@layer components{.sf-page{}}") == {
        "sf-page"
    }

    # And the extractor the first guard feeds on: literals in, f-string holes out.
    assert _literal_classes('<td class="metric-cell lvl-{esc(m.level)}">') == {"metric-cell"}
    assert _literal_classes("<span class='basis-dot'>", 'x.className = "pp-copy"') == {
        "basis-dot",
        "pp-copy",
    }


def test_preflight_arrives_exactly_when_the_hand_written_sheet_leaves() -> None:
    """The page carries ONE reset, and which one is a function of which sheets are inlined.

    `report.css` was the reset: it set `box-sizing` globally and zeroed the margins it cared about,
    and it was unlayered, so it outranked every Tailwind layer. Preflight is a second reset, and it
    reaches bare element selectors that sheet never mentioned — the one part of the build the layer
    argument would not have protected. Importing it alongside would have unbolded every heading,
    taken the bullets off the pipeline notes, dropped the UA paragraph margins the notices relied on,
    and refaced `#theme-toggle` with the body font.

    So it was sequenced rather than dropped, by a mechanism rather than by a note someone had to
    remember: neither state is one this repo can be left in, and the biconditional is what says so
    in both directions. The pair has now swapped — no hand-written sheet, Preflight on — and the
    live half is the one that fires on a rebuild that drops the import, with the line to add in its
    message. The other half stays because re-inlining a second reset is exactly what a well-meaning
    revert would do.
    """
    from seqforge.report import render

    hand_written_is_inlined = "report.css" in render._STYLESHEETS
    built = (_ASSETS / "report.tw.css").read_text()
    # `-webkit-text-size-adjust` is emitted by Preflight and by nothing else Tailwind ships.
    preflight_is_built = "text-size-adjust" in built

    assert preflight_is_built != hand_written_is_inlined, (
        'add `@import "tailwindcss/preflight.css" layer(base);` to report.src.css and rebuild'
        if hand_written_is_inlined is False
        else "report.src.css must not import tailwindcss/preflight.css while report.css is inlined "
        "— two resets on one page, and the second one moves headings, lists and paragraphs"
    )


def test_a_metrics_meaning_is_reachable_from_its_column_header_and_stored_once(
    own_workspace: Path,
) -> None:
    """ "A metric's meaning stays reachable from its column header rather than repeated in every cell."

    The criterion has two halves and only the second is about the page looking right. **Reachable**
    is the accessibility half: every hint the adapter wrote reaches a reader, through the same pinned
    popover the samples grid uses, off a control that is keyboard-reachable. **Once** is the budget
    half: the hint is byte-identical down its whole column, so a 96-sample table would pay for it
    ninety-six times — ~300 KB of the page's 500 KB, and the one thing on this tab that could break
    the budget on its own.

    Both are read off the rendered page, and both are counted rather than spot-checked: this
    criterion was implemented and shipped with no test at all, so `_metric_head` could have dropped
    its `hint` argument entirely and the suite would have stayed green.
    """
    _finish_a_starsolo_pipeline(own_workspace)
    page = render_html(collect_report(own_workspace))
    pane = _pane(page, "results")

    heads = re.findall(r'<span class="metric-head"([^>]*)>', pane)
    assert len(heads) >= 5, f"the metrics table's headers carry no hint at all: {len(heads)}"

    for attrs in heads:
        assert 'role="button"' in attrs and 'tabindex="0"' in attrs, (
            f"a hint that only a mouse can open is not reachable: {attrs}"
        )
        basis = re.search(r'data-basis="([^"]*)"', attrs)
        assert basis and len(basis.group(1)) > 20, f"the header carries no sentence: {attrs}"

    # ...and stored ONCE. Every hint appears exactly as many times as there are column headers
    # carrying it — never once per cell. Asserted over the real sentences the adapter wrote, so a
    # renderer that started repeating them per row fails here rather than in a size budget later.
    for attrs in heads:
        sentence = re.search(r'data-basis="([^"]*)"', attrs).group(1)  # type: ignore[union-attr]
        assert pane.count(sentence) == 1, (
            f"a hint is repeated {pane.count(sentence)} times; it belongs to the column, not the cell"
        )

    # The `role="button"` sits on a span INSIDE the `<th>`, never on the `<th>`: a column header that
    # announces itself as a button has stopped being a column header, and screen-reader table
    # navigation is what a wide metrics table needs most.
    assert not re.search(r'<th[^>]*role="button"', pane), (
        "a column header announcing itself as a button is no longer a column header"
    )


# -- the nuclear-library rule: counting one library two ways -----------------------------------------
#
# The rule itself is pure and is held to literal values in `tests/test_workflows.py`. What is under
# test here is the join: a recipe that counts more than one way, an artifact that carries both counts
# (it always did), and a page that names which feature is currently primary. Everything visual is
# asserted against `render_html`.

#: The library from #215, as STAR counted it twice: 30.1% of reads land in an exon, 70.8% land
#: anywhere in the gene body. Two `Summary.csv` blocks in one bundle, which is what the writer has
#: always produced for a recipe with two features in it.
_EXONIC_SUMMARY: dict[str, object] = {**_QC_SUMMARY, "Reads Mapped to Gene: Unique Gene": 0.301}
_FULL_LENGTH_SUMMARY: dict[str, object] = {
    **_QC_SUMMARY,
    "Reads Mapped to GeneFull: Unique GeneFull": 0.708,
}


def _count_with(ws: Path, features: list[str]) -> None:
    """Rewrite the workspace's recipe so it counts with STARsolo over `features`, in that order.

    Through the model rather than by poking yaml keys, so the shape on disk is whatever
    `ProcessingManifest` says it is and this helper cannot encode a layout that has moved. The shared
    fixture compiles a BULK recipe and `solo_features` is a STARsolo-only decision — the same class of
    on-disk edit `_finish_a_starsolo_pipeline` makes to the module, one artifact up.
    """
    from seqforge.models.processing import ProcessingManifest, SoloQuant

    path = ws / "seqforge" / "processing.yaml"
    proc = ProcessingManifest.model_validate(yaml.safe_load(path.read_text()))
    counting = proc.processing.quantification.model_copy(
        update={"value": SoloQuant(features=features)}  # type: ignore[arg-type]
    )
    proc = proc.model_copy(
        update={"processing": proc.processing.model_copy(update={"quantification": counting})}
    )
    path.write_text(yaml.safe_dump(proc.model_dump(mode="json"), sort_keys=True))


def _land_multi_feature_bundles(
    ws: Path, samples: list[str], summary: dict[str, dict[str, object]]
) -> None:
    """One QC bundle per sample carrying `summary` verbatim as its per-feature `Summary.csv` block.

    `_finish_a_starsolo_pipeline` writes exactly one feature, which is enough for every test above and
    useless here: the whole signal is the DISAGREEMENT between two of them.
    """
    import gzip

    from seqforge.pipeline import CompiledPipeline

    pipeline = CompiledPipeline.discover(ws)
    assert pipeline is not None, "the fixture workspace should already be composed"
    for smk in pipeline.directory.glob("*.smk"):
        smk.rename(smk.with_name("starsolo.smk"))
    config = pipeline.config
    config["samples"] = sorted(samples)
    pipeline.config_path.write_text(yaml.safe_dump(config, sort_keys=True))

    for sample in samples:
        out = pipeline.directory / str(config.get("outdir") or "results") / sample
        out.mkdir(parents=True, exist_ok=True)
        with gzip.open(out / f"{sample}.qc.json.gz", "wt", encoding="utf-8") as fh:
            json.dump(
                {
                    "sample": sample,
                    "summary": summary,
                    "log_final": _STAR_FINAL_LOG,
                    "umi_per_cell": {"Gene": [4213, 980, 310, 44, 9, 1]},
                },
                fh,
            )


def test_a_nuclear_library_counted_exonically_becomes_a_claim_about_the_recipe(
    own_workspace: Path,
) -> None:
    """The gap the artifact always carried, joined to the ordered list that decides which matrix wins.

    The recipe's features are read back off disk rather than restated here: a test that spelled
    `Gene, GeneFull` would keep passing against a collector that had stopped reading the recipe at
    all. What the page must carry is the whole chain — the claim, the non-colour mark, the two numbers
    it fired on, which feature is currently primary, the reorder remedy, and the stable id.
    """
    _count_with(own_workspace, ["Gene", "GeneFull"])
    samples = _finish_a_starsolo_pipeline(own_workspace)
    _land_multi_feature_bundles(
        own_workspace, samples, {"Gene": _EXONIC_SUMMARY, "GeneFull": _FULL_LENGTH_SUMMARY}
    )
    recipe = yaml.safe_load((own_workspace / "seqforge" / "processing.yaml").read_text())
    features = recipe["processing"]["quantification"]["value"]["features"]

    (alert,) = collect_report(own_workspace).assays[0].alerts
    block = _alert_block(render_html(collect_report(own_workspace)))

    assert alert.id == "starsolo.intronic-reads-uncounted"
    assert alert.severity == "possible"
    (ref,) = alert.implicates
    assert ref.decision == "solo_features"
    assert "processing.quantification.features" in ref.label
    for feature in features:
        assert feature in ref.value
    assert f"{features[0]} is the primary matrix" in ref.value
    assert ref.change_to is None, "a reorder among several full-length features is not one swap"

    assert "40.7%" in block and "70.8%" in block and "30.1%" in block
    assert '<span class="lvl-flag" role="img" aria-label="worth a look">!' in block
    assert "put GeneFull first" in block
    assert "starsolo.intronic-reads-uncounted" in block


def test_a_pipeline_that_counted_one_feature_reports_normally_and_raises_no_alert(
    own_workspace: Path,
) -> None:
    """The common case — `soloFeatures` is frequently just `Gene` — and it must cost nothing.

    A recipe that counts one way has no second column to disagree with, so there is no gap to measure
    and the page renders as it always did: no alert block at all, the metrics table intact, and the
    feature the headline numbers came from still named on the row it belongs to. That last one is its
    own acceptance criterion, and it is the thing a reader needs in order to judge the numbers.
    """
    _count_with(own_workspace, ["Gene"])
    samples = _finish_a_starsolo_pipeline(own_workspace)
    _land_multi_feature_bundles(own_workspace, samples, {"Gene": _EXONIC_SUMMARY})

    assay = collect_report(own_workspace).assays[0]
    page = render_html(collect_report(own_workspace))
    pane = _pane(page, "results")

    assert assay.pipeline_stats is not None and assay.pipeline_stats.complete
    assert assay.pipeline_stats.findings == [] and assay.alerts == []
    assert _alert_block(page) == ""
    assert "counted from the Gene feature" in pane
    assert "Reads in genes" in pane, "the table must still render, or this asserts nothing"


def test_the_page_keeps_its_guarantees_with_the_feature_alert_on_a_plate_sized_run(
    own_workspace: Path,
) -> None:
    """The size criterion, measured on a plate rather than argued — and there is nothing to bound.

    The per-sample artifact does not grow AT ALL: `build_qc_bundle` has written every feature's
    `Summary.csv` since the first bundle ever produced, so this ticket changed no writer, no `.smk`
    and no `WORKFLOW_VERSION`. What grows is one narrow mapping in memory. So the honest form of "any
    growth is bounded and does not push the page past its budget" is to render 96 wells with the rule
    firing on every one of them and measure, with the other three standing guarantees re-proved on the
    same page: an alert whose sample list is 96 ids long is exactly where a page bloats.
    """
    _count_with(own_workspace, ["Gene", "GeneFull"])
    plate = [f"{row}{col:02d}" for row in "ABCDEFGH" for col in range(1, 13)]
    _land_multi_feature_bundles(
        own_workspace, plate, {"Gene": _EXONIC_SUMMARY, "GeneFull": _FULL_LENGTH_SUMMARY}
    )

    html = render_html(collect_report(own_workspace))
    block = _alert_block(html)

    assert len(plate) == 96
    assert "every sample that finished (96 of 96)" in block
    assert len(html.encode()) < 500_000
    assert not re.findall(r'(?:src|href)\s*=\s*"(?:https?:)?//[^"]+"', html)
    assert html == render_html(collect_report(own_workspace))


def test_a_plate_sized_alert_stays_inside_the_budget_with_its_sample_cap_lifted(
    own_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What holds the test above inside 500 KB is `_ALERT_MAX_SAMPLES`, not the budget it asserts.

    The alert list truncates well short of a plate, so the block above weighs the same at 96 firing
    samples as at a handful, and that budget stops being sensitive to what one alert row costs: a
    mutation fattening each *rendered* row by ~6 KB — 96 of which would be half a megabyte of list —
    went green through it, because only a handful of rows were ever rendered. The cap is right and
    stays. What is wrong is that the page's headroom then rests on a constant nobody re-measures, and
    raising it is a one-line change whose suite is green either way.

    So the number asserted here is the **untruncated** one: what the page weighs with every sample
    that fired spelled out, which is exactly what someone raising the cap is implicitly claiming is
    affordable. Measured when this landed (2026-08-04) a 96-well page is ~269 KB truncated and
    ~283 KB untruncated, against the same 500 KB budget — so this fires once an alert row costs about
    2.4 KB, against the ~160 bytes one costs today. The ~6 KB row that escaped is comfortably red.

    **The cap is lifted, never asserted.** A test spelling its value would restate the constant and
    catch nothing; the property is a relationship between a row's cost and the page's budget, and it
    is held from both ends — the shipped page must genuinely truncate here (or the two renders are
    the same page and this measures nothing), and the lifted one must still fit.
    """
    from seqforge.report import panels

    _count_with(own_workspace, ["Gene", "GeneFull"])
    plate = [f"{row}{col:02d}" for row in "ABCDEFGH" for col in range(1, 13)]
    _land_multi_feature_bundles(
        own_workspace, plate, {"Gene": _EXONIC_SUMMARY, "GeneFull": _FULL_LENGTH_SUMMARY}
    )

    # One collection, two renders: the cap is a rendering decision, so the alert this measures is
    # byte-identical on both sides and the only thing that moves is how much of it reaches the page.
    report = collect_report(own_workspace)
    truncated = _alert_block(render_html(report))
    assert truncated, "the plate must raise an alert, or there is no list here to truncate"
    assert sum(well in truncated for well in plate) < len(plate), (
        "the cap must bind on a plate, or the render below is the same page measured twice"
    )

    monkeypatch.setattr(panels, "_ALERT_MAX_SAMPLES", len(plate))
    html = render_html(report)

    assert all(well in _alert_block(html) for well in plate), (
        "every firing sample must reach the block once the cap is lifted, or something else is "
        "bounding this list and the size below is not the untruncated one"
    )
    assert len(html.encode()) < 500_000, (
        "a plate with every firing sample spelled out breaks the 500 KB budget — an alert row has "
        "grown to where _ALERT_MAX_SAMPLES, not the budget, is what keeps the page inside it"
    )


# -- the gene model and the strand: attribution across two artifacts ---------------------------------
#
# The second rule's own seam. The threshold work is held to literal values in `tests/test_workflows.py`;
# what is under test here is that its two decisions resolve to what the workspace currently carries —
# one out of the recipe, one out of the COMPOSED CONFIG, which is a place no other decision reads from
# — and that all of it reaches the rendered page.

#: The same finished run with its gene model missed: mapping is healthy (81.2%) and almost nothing was
#: assigned to a gene. Not a bad library — a GTF or a strand that does not belong to these reads.
_UNCOUNTED_GENES: dict[str, object] = {**_QC_SUMMARY, "Reads Mapped to Gene: Unique Gene": 0.021}


def _write_a_strand_into_the_composed_config(ws: Path, strand: str) -> None:
    """Put a `soloStrand` where `compose` puts one, so a decision-under-test has a value to read.

    The shared fixture compiles a BULK pipeline — the branch CI can run headless — so its config
    carries a `bulk` param block and no strand at all. `compose` writes the KB's backend params under
    the block key the module itself declares (`param_block_key`), which for `map/starsolo` is `solo`,
    so this is that block written on disk. Like every other edit these fixtures make it touches only
    what is on disk; nothing the compiler decided moves.
    """
    from seqforge.pipeline import CompiledPipeline

    pipeline = CompiledPipeline.discover(ws)
    assert pipeline is not None, "the fixture workspace should already be composed"
    config = pipeline.config
    solo = config.get("solo")
    config["solo"] = {**(solo if isinstance(solo, dict) else {}), "soloStrand": strand}
    pipeline.config_path.write_text(yaml.safe_dump(config, sort_keys=True))


def _decision_context(ws: Path, manifest: DatasetManifest) -> _DecisionContext:
    """Everything a resolver may read, out of a real workspace rather than out of three `None`s.

    Three of the five decisions live outside the dataset manifest — two in the recipe, one in the
    composed config — so a context of nulls would let their resolvers return `None` and still satisfy
    an exhaustiveness check over the `Literal`, which is the half that test says is not enough.

    Two things are written in, because the shared fixture compiles a **bulk** pipeline and carries
    neither: a strand, and a counting section with features in it. Both are the same class of on-disk
    edit `_finish_a_starsolo_pipeline` already makes to the module one artifact up — they make the
    workspace *answerable*, not the resolver answer. That a workspace which never composed one
    **drops** the ref rather than drawing an empty row is the neighbouring tests' claim, not this
    one's.
    """
    from seqforge.models.processing import ProcessingManifest, SoloQuant
    from seqforge.report.collect import _DecisionContext

    _write_a_strand_into_the_composed_config(ws, "Forward")
    plan = collect_report(ws).assays[0].plan
    assert plan is not None, "the fixture workspace should already carry a recipe"
    proc = ProcessingManifest.model_validate(
        yaml.safe_load((ws / "seqforge" / "processing.yaml").read_text())
    )
    counting = proc.processing.quantification.model_copy(
        update={"value": SoloQuant(features=["Gene", "GeneFull"])}
    )
    proc = proc.model_copy(
        update={"processing": proc.processing.model_copy(update={"quantification": counting})}
    )
    return _DecisionContext(manifest=manifest, proc=proc, plan=plan)


def test_the_gene_model_alert_names_the_annotation_and_the_strand_it_could_be_flipped_to(
    own_workspace: Path,
) -> None:
    """The two decisions this rule points at, resolved out of the two artifacts that hold them.

    The annotation is a recipe field and the strand is not — it is a KB backend param the composer
    emitted — so they are read from different places and the labels have to say which, or a reader
    goes looking for a `soloStrand` in a recipe that has never had one. Both expected values are read
    back off disk rather than restated here: a test that spelled `sacCer3` would keep passing against
    a collector that had stopped reading the recipe at all.
    """
    _finish_a_starsolo_pipeline(own_workspace, summary=_UNCOUNTED_GENES)
    _write_a_strand_into_the_composed_config(own_workspace, "Forward")
    proc = yaml.safe_load((own_workspace / "seqforge" / "processing.yaml").read_text())
    genome = proc["processing"]["genome"]["value"]
    expected = f"{genome['assembly']} / {genome['annotation_name']}"

    (alert,) = collect_report(own_workspace).assays[0].alerts
    block = _alert_block(render_html(collect_report(own_workspace)))

    assert alert.id == "starsolo.reads-mapped-but-not-counted"
    refs = {d.decision: d for d in alert.implicates}
    assert set(refs) == {"annotation", "strand"}
    assert refs["annotation"].value == expected and "processing.genome" in refs["annotation"].label
    assert refs["strand"].value == "Forward" and refs["strand"].change_to == "Reverse"
    # ...and the label says where it lives, because it does not live in the recipe.
    assert "soloStrand" in refs["strand"].label and "not a recipe field" in refs["strand"].label

    assert f"currently <b>{expected}</b>" in block
    assert "currently <b>Forward</b>; the alternative is <b>Reverse</b>" in block


def test_a_strand_this_workspace_never_composed_is_dropped_rather_than_drawn_empty(
    own_workspace: Path,
) -> None:
    """A decision the workspace cannot answer for is dropped, never rendered as a field with no value.

    The fixture's config is a bulk one and carries no `soloStrand`, which is the honest version of
    that case rather than a mocked absence. The annotation still resolves, so what is asserted is one
    ref dropping out of a card that otherwise attributes — not a card that failed to attribute at all.
    """
    _finish_a_starsolo_pipeline(own_workspace, summary=_UNCOUNTED_GENES)

    (alert,) = collect_report(own_workspace).assays[0].alerts
    block = _alert_block(render_html(collect_report(own_workspace)))

    assert [d.decision for d in alert.implicates] == ["annotation"]
    assert "soloStrand" not in block
    assert "Points at" in block, "the heading survives, or the discriminator is the whole card"


def test_the_strand_offers_an_alternative_only_where_the_alternative_is_one_value(
    own_workspace: Path,
) -> None:
    """`--soloStrand` also takes `Unstranded`, and from there "the alternative" is two values, not one.

    `change_to` is filled only where the alternative is genuinely enumerable, so an unstranded config
    still says what the strand IS and offers no flip: a wrong concrete suggestion is worse than none,
    and this is the branch where a blind `Forward`/`Reverse` flip would invent one.
    """
    _finish_a_starsolo_pipeline(own_workspace, summary=_UNCOUNTED_GENES)
    _write_a_strand_into_the_composed_config(own_workspace, "Unstranded")

    (alert,) = collect_report(own_workspace).assays[0].alerts
    (strand,) = [d for d in alert.implicates if d.decision == "strand"]
    block = _alert_block(render_html(collect_report(own_workspace)))

    assert strand.value == "Unstranded" and strand.change_to is None
    assert "currently <b>Unstranded</b>" in block
    assert "the alternative is" not in block


def test_a_workspace_that_was_never_composed_answers_for_neither_new_decision(
    own_workspace: Path,
) -> None:
    """An assay can reach the IR and never be composed, and then neither decision has a value to read.

    The recipe is not there, and neither is the config it would have produced. Both resolve to `None`
    rather than to a plausible-looking default, and `gather_alerts` drops the row — a field name with
    no value beside it reads as a value of nothing.

    This is the case the exhaustiveness test above cannot cover, because that one needs a context
    that answers. Asserted here from the other side, with both directions in one test so a resolver
    that had stopped reading its artifact entirely would fail the second half.
    """
    from seqforge.models.dataset import DatasetManifest
    from seqforge.report.collect import _DecisionContext, _resolve_annotation, _resolve_strand

    manifest = DatasetManifest.model_validate(
        yaml.safe_load((own_workspace / "seqforge" / "manifest.yaml").read_text())
    )
    bare = _DecisionContext(manifest=manifest, proc=None, plan=None)

    assert _resolve_annotation(bare) is None and _resolve_strand(bare) is None

    full = _decision_context(own_workspace, manifest)
    assert _resolve_annotation(full) is not None and _resolve_strand(full) is not None


def test_a_genome_with_no_registered_gene_model_names_no_annotation(own_workspace: Path) -> None:
    """`annotation_name` is `None` for an index built from the FASTA alone, and "sacCer3 / None" is
    a value a reader would try to act on.

    So the ref is dropped rather than rendered. The rule that names this decision cannot fire on such
    a pipeline anyway — no GTF, no `Summary.csv` gene rows, no `reads_in_genes` — which makes this the
    same fact arriving from the other side rather than a second guard, and it is asserted here
    because nothing else in this file composes a chromap recipe.
    """
    from seqforge.models.dataset import DatasetManifest
    from seqforge.report.collect import _resolve_annotation

    manifest = DatasetManifest.model_validate(
        yaml.safe_load((own_workspace / "seqforge" / "manifest.yaml").read_text())
    )
    ctx = _decision_context(own_workspace, manifest)
    assert ctx.proc is not None and _resolve_annotation(ctx) is not None

    evidenced = ctx.proc.processing.genome
    no_gtf = evidenced.model_copy(
        update={"value": evidenced.value.model_copy(update={"annotation_name": None})}
    )
    stripped = ctx.proc.model_copy(
        update={"processing": ctx.proc.processing.model_copy(update={"genome": no_gtf})}
    )

    assert _resolve_annotation(ctx._replace(proc=stripped)) is None


def test_the_page_keeps_its_standing_guarantees_with_the_gene_model_alert_rendered(
    own_workspace: Path,
) -> None:
    """Self-contained, no external reference, inside the budget, byte-deterministic — with this alert.

    Asserted again for this rule rather than assumed off the others: this is the card that carries a
    `change_to` line, which no other shipped rule fills.
    """
    _finish_a_starsolo_pipeline_over(
        own_workspace, {"S1": _UNCOUNTED_GENES, "S2": _QC_SUMMARY, "S3": _UNCOUNTED_GENES}
    )
    _write_a_strand_into_the_composed_config(own_workspace, "Forward")

    html = render_html(collect_report(own_workspace))
    block = _alert_block(html)

    assert "starsolo.reads-mapped-but-not-counted" in block, (
        "the fixture must raise this rule, or the guarantees are re-proved unchanged"
    )
    assert "the alternative is <b>Reverse</b>" in block
    assert not re.findall(r'(?:src|href)\s*=\s*"(?:https?:)?//[^"]+"', html)
    assert len(html.encode()) < 500_000
    assert html == render_html(collect_report(own_workspace))
