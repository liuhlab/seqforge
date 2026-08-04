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
from html import escape
from pathlib import Path
from typing import get_args

import pytest
import yaml
from typer.testing import CliRunner

from conftest import write_fastq_gz
from seqforge import kb
from seqforge.cli import app
from seqforge.report import collect_report, render_html
from seqforge.report.flow import flow_steps
from seqforge.report.model import AssayReport

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
    assert 'class="mx-cell' in render_html(collect_report(workspace))


def test_samples_render_as_a_metadata_table(workspace: Path) -> None:
    """The per-sample card list is gone: samples are one scrolling table with an expandable drawer."""
    html = render_html(collect_report(workspace))
    assert 'class="sf-scroll-x"' in html  # the region that scrolls, so the page never does
    assert 'class="sf-col-sticky basis-toggle"' in html  # the pinned identifier, and the control
    assert 'class="basis-caret"' in html  # the expand caret
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


def test_the_migrated_shell_wears_no_class_the_old_stylesheet_would_still_win_with(
    workspace: Path,
) -> None:
    """The shell's elements left the hand-written sheet, and left it *entirely*.

    ``report.css`` is inlined and unlayered, so it outranks every Tailwind layer whatever the source
    order: an element that keeps its old class keeps its old styling and the utilities beside it do
    nothing. Leaving one on "as a fallback" is therefore not a fallback, it is an override that
    silently wins — which is why this asserts absence from the markup rather than presence of the
    new classes, and why each name below is checked to still HAVE a rule in that sheet. A dead class
    would make this test pass for the wrong reason on the day #220 deletes the file.
    """
    page = render_html(collect_report(workspace))
    worn = _body_classes(page)
    hand_written = (_ASSETS / "report.css").read_text()

    migrated = [
        "top", "top-row", "brand", "spark", "title-dim", "top-spacer",  # the header
        "verdict", "dot", "icon-btn", "assay-switch",                   # its controls
        "tabs", "tabs-row",                                             # the tab bar
        "panel", "foot",                                                # the panel and the footer
    ]  # fmt: skip
    assert not _classes_with_no_rule(set(migrated), [hand_written]), (
        "every name here must still be a live rule in report.css, or this test proves nothing"
    )
    assert not (worn & set(migrated)), (
        f"the shell still wears the old sheet's classes: {sorted(worn & set(migrated))} — "
        "unlayered CSS beats every utility, so those elements are not migrated at all"
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


def test_the_report_verbs_help_describes_the_page_that_actually_ships() -> None:
    """The verb's ``--help`` and the renderer's docstring still promised the removed diagram engine.

    Dropping Mermaid cut a rendered page from ~2.6 MB to tens of KB — the single largest thing ever
    true about this page — and both prose sites went on describing the bundle as inlined. Prose that
    names a dependency the wheel does not carry is worse than none: it is what a reader checks a size
    budget against, and it would have sent the next person looking for an asset that is not there.
    """
    from importlib.resources import files

    from seqforge.report import render

    result = runner.invoke(app, ["report", "--help"])

    assert result.exit_code == 0, result.stdout
    # `--help` is a promise about the page a user is about to get, not a history of it.
    assert "mermaid" not in result.stdout.lower()

    doc = (render.__doc__ or "").lower()
    assert "no third-party runtime" in doc  # the renderer says what it executes
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


def test_the_level_legend_names_every_verdict_the_page_can_render() -> None:
    """The legend is what makes a tint and a mark mean anything, so a verdict missing from it is mute.

    Rendered rather than introspected: the legend is a module-level string built once at import, and
    the failure this guards is a key present in the map but absent from the built HTML.
    """
    from seqforge.report.panels import _LEVEL_LEGEND, _LEVEL_PHRASE
    from seqforge.workflows.metrics import Level

    for level in get_args(Level):
        assert f'class="lvl-{level}"' in _LEVEL_LEGEND, f"{level} is graded but never explained"
        assert _LEVEL_PHRASE[level].split(" — ")[0] in _LEVEL_LEGEND


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


def _pane(page: str, name: str) -> str:
    """One tab's rendered markup. The tab BUTTON carries ``data-tab`` too, so the pane is found by
    its own opening tag — slicing on the attribute alone lands in the tab bar."""
    body = page.split("</style>")[-1]
    start = body.index(f'<div class="pane" data-tab="{name}">')
    return body[start : body.index('<div class="pane"', start + 1)]


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
    assert 'class="sf-col-sticky basis-toggle"' in samples

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


def test_the_two_grids_wear_no_class_the_old_stylesheet_would_still_win_with() -> None:
    """Samples and Evidence left the hand-written sheet, and left it *entirely*.

    ``report.css`` is inlined and unlayered, so it outranks every Tailwind layer whatever the source
    order: an element that keeps its old class keeps its old styling and every utility beside it does
    nothing. Leaving one on "as a fallback" is an override that silently wins. Asserted as absence
    from the markup, with each name checked to still HAVE a rule in that sheet — otherwise this would
    pass for the wrong reason on the day #220 deletes the file.
    """
    worn = _body_classes(_rich_page())
    hand_written = (_ASSETS / "report.css").read_text()

    migrated = [
        "legend-basis", "basis-dot", "samples", "col-sample", "attr-cell",  # the samples grid
        "withheld", "sample-toggle", "row-toggle", "detail-row", "detail-body",
        "file-list", "rstruct", "acc", "tbl-wrap", "tbl-sticky", "num",
        "evidence", "verdict-strip", "win-chip", "vs-note", "family-focus",  # the evidence view
        "fam-note", "matrix-card", "is-winner", "matrix", "mrole", "cell",
        "forbidden", "sibling", "mini-bar", "why", "ruled-out",
        "ruled-list", "ruled-foot", "reason",
    ]  # fmt: skip
    assert not _classes_with_no_rule(set(migrated), [hand_written]), (
        "every name here must still be a live rule in report.css, or this test proves nothing"
    )
    assert not (worn & set(migrated)), (
        f"the grids still wear the old sheet's classes: {sorted(worn & set(migrated))} — "
        "unlayered CSS beats every utility, so those elements are not migrated at all"
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


def _stage_blob(assay: AssayReport) -> str:
    """The stage diagram's whole rendered wording, lowercased — what a biologist would read."""
    return " ".join(f"{s.title} {s.detail}" for s in assay.pipeline_stages).lower()


def test_the_stage_diagram_branches_on_the_typed_quantification_family(own_workspace: Path) -> None:
    """An ATAC recipe renders chromap + fragments, and the axis is the recipe's typed kind.

    This was checked against a hand-built ``PlanView`` whose ``value=`` was the production display
    string copied into the fixture, which is a test that can only ever agree with itself: reword the
    caption in ``_plan`` and the diagram silently reverts to the RNA branch with nothing failing —
    on an ATAC dataset, where "count reads per gene" is not a cosmetic error. So the plan view
    arrives from ``collect_report`` here, and the caption is then reworded on the way into the
    diagram to prove it is not what the branch reads.
    """
    from seqforge.report.collect import _pipeline_stages

    _retarget_the_recipe(own_workspace, {"kind": "atac"})
    assay = collect_report(own_workspace).assays[0]
    plan = assay.plan
    assert plan is not None
    assert plan.quantification_kind == "atac"  # the typed family, carried; not the caption

    blob = _stage_blob(assay)
    assert "fragment" in blob
    assert "chromap" in blob
    assert "count genes" not in blob  # the RNA phrasing must not leak into an ATAC run

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
    solo = collect_report(own_workspace).assays[0]
    assert solo.plan is not None and solo.plan.quantification_kind == "solo"
    solo_blob = _stage_blob(solo)
    assert "count" in solo_blob and "starsolo" in solo_blob


# -- the finished pipeline (the Results tab) -------------------------------------------------------
#
# The page's fourth join, and the only one whose artifact seqforge did not write: once the user
# submits the composed Snakefile, its per-sample QC bundles land beside it and the report reads them.
# The shared fixture composes `map/star`, so these tests swap the copied `.smk` for the single-cell
# one to exercise the richer adapter — `CompiledPipeline.module` reads the module off the file that is
# *present*, which is exactly the seam being exercised. (Both modules report; every registered module
# does. The swap buys the barcode and knee metrics bulk has no vector for, not reporting at all.)

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
    # No disclosure widget promising a table that has no columns behind it.
    assert 'class="stats-details"' not in html


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


#: One `Log.final.out`, as STAR itself writes it — the artifact `map/star` reports from. Bulk needs no
#: renamed module and no QC bundle, which is what makes it the cheap way to hand the chained `run`
#: verb a *finished* pipeline. The numbers are a healthy run; what is under test is the flag, not the
#: grading, which `tests/test_workflows.py` holds to real STAR values.
_STAR_FINAL_LOG: dict[str, object] = {
    "Number of input reads": 1_000_000,
    "Uniquely mapped reads %": "91.20%",
    "% of reads mapped to multiple loci": "4.10%",
    "% of reads mapped to too many loci": "0.30%",
    "% of reads unmapped: too short": "3.90%",
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
    "genstats-conf": "the confidence <div> inside .genstats, styled by `.genstats > div`",
    "kgrid": "<g class='kgrid'> in a knee plot, whose `<line class='kg'>` children are styled",
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
    """Guard one: nothing the page can wear is unstyled in BOTH sheets.

    Adding ``class="mt-8"`` to a fragment and not rebuilding is silent — the page renders and one
    rule is absent. This collects every literal class the page can carry (the rendered page with the
    heaviest tab on, plus the `@source` modules, so a branch this fixture never reaches is still
    checked) and fails if any of them has a rule in neither stylesheet.

    Why "either sheet" is not a way of passing for free. During the expand step the hand-written
    `report.css` supplies nearly every rule, so this guard's Tailwind half would be satisfied by an
    EMPTY build — three things keep it honest anyway. `assert used` refuses an empty page. Guard two
    asserts the built artifact really does carry what its source declares, so the build cannot be
    empty. And `test_the_two_drift_guards_fire_on_a_drifted_input_and_stay_silent_on_a_clean_one`
    exercises this exact matcher against a class present in neither sheet and requires it to fail.
    As #217–#219 move rules out of `report.css` and into utilities, the union narrows to the built
    file on its own — the guard does not need editing for that to happen.
    """
    _finish_a_starsolo_pipeline(own_workspace)
    page = render_html(collect_report(own_workspace))

    used = _literal_classes(
        page.split("</style>")[-1], *(p.read_text() for p in _CLASS_BEARING_SOURCES)
    )
    assert len(used) > 50, "the page and its fragments really do carry classes"

    sheets = [(_ASSETS / name).read_text() for name in ("report.tw.css", "report.css")]
    missing = set(_classes_with_no_rule(used, sheets))

    unexpected = sorted(missing - set(_UNSTYLED_HOOKS))
    assert not unexpected, (
        f"classes with no rule in either stylesheet: {unexpected} — rebuild report.tw.css "
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

    `report.css` is the reset today: it sets `box-sizing` globally and zeroes the margins it cares
    about, and it is unlayered, so it outranks every Tailwind layer. Preflight is a second reset,
    and it reaches bare element selectors the hand-written sheet never mentions — where the layer
    argument does not protect anything. Measured on this page, importing it while `report.css` is
    still inlined would unbold every heading (`h1..h4 { font-weight: inherit }`), take the bullets
    off `ul.pipeline-notes`, drop the UA paragraph margins `p.empty`/`p.notice`/`.organism` rely on,
    and reface `#theme-toggle` with the body font.

    So it is sequenced, not dropped — and sequenced by a mechanism rather than by a note someone has
    to remember. This test goes red in exactly one commit: the one that stops inlining `report.css`.
    Its instruction to that commit is in the failure message.
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
