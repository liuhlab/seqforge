"""Tests for the evals harness — chiefly: **does it fail when it should?**

A harness is a measuring instrument, and an instrument that has only ever read "green" is
indistinguishable from a broken one. The corpus passing (``test_corpus_is_green``) proves almost
nothing on its own; the tests that matter here mutate a known-good case until it is wrong and assert
the harness NOTICES, with the right grade. If these ever pass vacuously, every other eval number in
this repo becomes decoration.

The grading tables are the thing under test, so they are exercised directly against synthetic
``ResolveResult``s rather than through a full pipeline run — a grading bug must not be able to hide
behind a resolver that happens to be right.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from seqforge.cli import app
from seqforge.evals import (
    Case,
    CaseError,
    CaseGrade,
    Expected,
    Grade,
    build_report,
    default_cases_dir,
    discover_cases,
    grade_case,
    load_cases,
    materialize,
    outcome_of,
    render_html,
    run_case,
)
from seqforge.evals.case import SIM_RUN, Recipe
from seqforge.evals.run import CaseRun, HarvestGrade, _fold_harvest, _merge_harvest
from seqforge.harvest import EXTRACT_PROMPT_VERSION
from seqforge.models.assertion import Assertion, ExtractorProvenance, SourceSpan
from seqforge.models.blocker import Blocker, BlockerCode, BlockerSubject
from seqforge.models.conflict import Conflict, ConflictPosition
from seqforge.models.dataset import DatasetManifest
from seqforge.models.resolve import (
    Candidate,
    MetadataResolution,
    Question,
    ResolveResult,
    RoleAssignment,
    TechScore,
)
from seqforge.resolve import DatasetResolution, Hypothesis

if TYPE_CHECKING:  # the stub providers below import it where they build one, as the real code does
    from seqforge.harvest import LLMResponse

# --------------------------------------------------------------------------------------------
# synthetic resolve results — so grading is tested independently of the resolver being correct
# --------------------------------------------------------------------------------------------


def _result(
    tech: str = "10x-3p-gex-v3",
    *,
    blockers: list[BlockerCode] | None = None,
    conflicts: list[Conflict] | None = None,
    questions: list[Question] | None = None,
    roles: dict[str, str] | None = None,
) -> ResolveResult:
    candidates = []
    if not blockers:
        candidates = [
            Candidate(
                technology=tech,
                score=TechScore(technology=tech, status="scored", value=1.0),
                role_assignment=RoleAssignment(
                    assignment=roles or {"R1": "sha-r1", "R2": "sha-r2"}
                ),
                rung_resolved={"chemistry": 3},
            )
        ]
    return ResolveResult(
        dataset_id="ds-test",
        kb_version="2026.7.0",
        rung_reached=3,
        candidates=candidates,
        conflicts=conflicts or [],
        questions=questions or [],
        blockers=[
            Blocker(
                id=f"blk-{i}",
                code=c,
                message=c.value,
                remedy="do the thing",
                subject=BlockerSubject(kind="dataset", ref="ds-test"),
            )
            for i, c in enumerate(blockers or [])
        ],
    )


def _conflict(field: str = "library.read_layout.R1.length") -> Conflict:
    return Conflict(
        id="c1",
        field=field,
        kind="observed_vs_asserted",
        positions=[
            ConflictPosition(value="26", basis="asserted", confidence=0.9),
            ConflictPosition(value="28", basis="observed", confidence=0.99),
        ],
        decidable_by=["reads"],
        status="open",
    )


LABELS = {"sha-r1": "R1", "sha-r2": "R2"}


def _grade(expected: dict[str, object], result: ResolveResult, exit_code: int) -> CaseGrade:
    return grade_case("t", Expected.model_validate(expected), result, exit_code, LABELS)


# --------------------------------------------------------------------------------------------
# the confusion matrix — every cell, especially the one that matters
# --------------------------------------------------------------------------------------------


def _question(
    field: str = "library.chemistry",
    options: list[str] | None = None,
) -> Question:
    """An exit-4 the resolver raised as a QUESTION rather than a conflict: no positions, an option set."""
    return Question(
        id="q-chemistry",
        field=field,
        prompt="Which chemistry applies?",
        options=options or ["10x-3p-gex-v2", "10x-5p-gex-v2"],
        decidable_by=["alignment", "metadata"],
        rung=7,
    )


def _collapsed_conflict() -> Conflict:
    """A "conflict" whose two positions carry the SAME value — i.e. not a conflict at all."""
    return _conflict().model_copy(
        update={
            "positions": [
                ConflictPosition(value="28", basis="asserted", confidence=0.9),
                ConflictPosition(value="28", basis="observed", confidence=0.99),
            ]
        }
    )


#: One row per cell of the confusion matrix: what the case *expected*, what the resolver actually
#: produced, the exit code it produced it with, and the grade the harness must return.
#:
#: These were 18 near-identical functions. As a table the matrix is legible as a matrix — a missing
#: cell is a missing row rather than an absent function nobody notices — and the reason each cell
#: grades the way it does rides beside it as a comment instead of a docstring.
#:
#: ``note`` is a substring that must reach the human: it is matched against everything the grade
#: renders (its notes AND its field checks), because "the reason is visible" is the claim, not which
#: attribute carries it.
CONFUSION_MATRIX = [
    pytest.param(
        {"outcome": "decide", "fields": {"library.chemistry": "10x-3p-gex-v3"}},
        _result(), 0, Grade.CORRECT, None, False,
        id="a-correct-decision-is-correct",
    ),
    # A decision that disagrees with truth IS the corpus-poisoning failure — the headline metric.
    pytest.param(
        {"outcome": "decide", "fields": {"library.chemistry": "10x-3p-gex-v2"}},
        _result(), 0, Grade.FALSE_ACCEPT, "10x-3p-gex-v2", False,
        id="a-wrong-value-is-false-accept-not-merely-a-failure",
    ),
    # The ONT case's failure mode: something always ranks highest; "highest" is not "right".
    pytest.param(
        {"outcome": "refuse", "blockers": ["UNSUPPORTED_TECHNOLOGY"]},
        _result(), 0, Grade.FALSE_ACCEPT, None, False,
        id="guessing-where-refusal-was-correct-is-false-accept",
    ),
    # Failing to ask a needed question is a hard fail. Its mechanism is a silent pick.
    pytest.param(
        {"outcome": "ask"},
        _result(), 0, Grade.FALSE_ACCEPT, None, True,
        id="silently-picking-instead-of-asking-is-false-accept-and-a-missed-question",
    ),
    pytest.param(
        {"outcome": "decide", "fields": {"library.chemistry": "10x-3p-gex-v3"}},
        _result(blockers=[BlockerCode.TRUNCATED_GZIP]), 3, Grade.FALSE_REFUSE, None, False,
        id="blocking-a-decidable-case-is-false-refuse",
    ),
    # Nothing wrong entered the manifest. It is a cost regression, tracked separately.
    pytest.param(
        {"outcome": "decide", "fields": {"library.chemistry": "10x-3p-gex-v3"}},
        _result(conflicts=[_conflict()]), 4, Grade.OVER_ASK, None, False,
        id="asking-what-code-could-settle-is-over-ask-not-false-refuse",
    ),
    pytest.param(
        {"outcome": "ask"},
        _result(blockers=[BlockerCode.UNRESOLVED_CONFLICT]), 3, Grade.FALSE_REFUSE, None, False,
        id="blocking-instead-of-asking-is-false-refuse",
    ),
    pytest.param(
        {"outcome": "refuse", "blockers": ["TRUNCATED_GZIP"]},
        _result(), 4, Grade.MIS_TRIAGE, None, False,
        id="asking-instead-of-blocking-is-mis-triage",
    ),
    # Right outcome, wrong reason: the human is sent the wrong way. Counting it green rots meaning.
    pytest.param(
        {"outcome": "refuse", "blockers": ["TRUNCATED_GZIP"]},
        _result(blockers=[BlockerCode.CORRUPT_FASTQ]), 3, Grade.WRONG_REASON, "TRUNCATED_GZIP", False,
        id="a-right-refusal-with-the-wrong-blocker-is-wrong-reason-not-correct",
    ),
    pytest.param(
        {"outcome": "refuse", "blockers": ["TRUNCATED_GZIP"]},
        _result(blockers=[BlockerCode.TRUNCATED_GZIP]), 3, Grade.CORRECT, None, False,
        id="a-correct-refusal-matches-the-blocker-code",
    ),
    # -- conflicts: the POSITIONS are the load-bearing assertion, not the field name --
    pytest.param(
        {
            "outcome": "ask",
            "conflict": {
                "kind": "observed_vs_asserted",
                "field": "library.read_layout.R1.length",
                "positions": {"asserted": "26", "observed": "28"},
            },
        },
        _result(conflicts=[_conflict()]), 4, Grade.CORRECT, None, False,
        id="an-expected-conflict-matches-on-field-and-positions",
    ),
    pytest.param(
        {"outcome": "ask", "conflict": {"field": "library.chemistry"}},
        _result(conflicts=[_conflict()]), 4, Grade.WRONG_REASON, None, False,
        id="a-conflict-on-the-wrong-field-is-wrong-reason",
    ),
    # Why positions are asserted: both sides agreeing is not a conflict, however it is labelled.
    pytest.param(
        {
            "outcome": "ask",
            "conflict": {
                "field": "library.read_layout.R1.length",
                "positions": {"asserted": "26", "observed": "28"},
            },
        },
        _result(conflicts=[_collapsed_conflict()]), 4, Grade.WRONG_REASON, None, False,
        id="a-conflict-with-collapsed-positions-is-caught",
    ),
    pytest.param(
        {"outcome": "ask", "conflict": {"field": "library.chemistry"}},
        _result(), 4, Grade.WRONG_REASON, "no open conflict", False,
        id="exit-4-with-no-conflict-or-question-is-caught",
    ),
    # -- questions: the OPTION SET is what `positions` is for a conflict --
    # A question has no positions to disagree — it has the set of answers a human is being offered.
    # Naming the field alone would pass on any chemistry question at all, which is exactly the pin a
    # real case needs: "it asks, and it asks between THESE two".
    pytest.param(
        {
            "outcome": "ask",
            "conflict": {
                "field": "library.chemistry",
                "options": ["10x-3p-gex-v2", "10x-5p-gex-v2"],
            },
        },
        _result(questions=[_question()]), 4, Grade.CORRECT, None, False,
        id="an-expected-question-matches-on-field-and-option-set",
    ),
    pytest.param(
        {
            "outcome": "ask",
            "conflict": {
                "field": "library.chemistry",
                "options": ["10x-3p-gex-v2", "10x-5p-gex-v2"],
            },
        },
        _result(questions=[_question(options=["10x-3p-gex-v3", "10x-gemx-3p-v4"])]),
        4, Grade.WRONG_REASON, "10x-5p-gex-v2", False,
        id="the-right-field-with-the-wrong-pair-is-wrong-reason",
    ),
    # An option set asserted where the exit-4 was a Conflict has nothing to match, and saying so is
    # the point: the two shapes are not interchangeable.
    pytest.param(
        {
            "outcome": "ask",
            "conflict": {
                "field": "library.read_layout.R1.length",
                "options": ["26", "28"],
            },
        },
        _result(conflicts=[_conflict()]), 4, Grade.WRONG_REASON, "no question", False,
        id="an-option-set-against-a-conflict-is-caught-rather-than-ignored",
    ),
    # It stopped, so nothing was committed — but the human gets the right question, wrong state.
    pytest.param(
        {
            "outcome": "ask",
            "conflict": {"field": "library.read_layout.R1.length"},
            "fields": {"library.chemistry": "10x-3p-gex-v3"},
        },
        _result(tech="bulk-rnaseq-pe", conflicts=[_conflict()]), 4, Grade.WRONG_REASON, None, False,
        id="a-wrong-library-value-while-asking-is-wrong-reason-not-false-accept",
    ),
    # -- role assignment + field extraction --
    pytest.param(
        {"outcome": "decide", "fields": {"library.roles.R1": "R1"}},
        _result(), 0, Grade.CORRECT, None, False,
        id="role-assignment-is-checked-by-label-not-hash",
    ),
    # Right chemistry + swapped roles emits a pipeline that reads cDNA as a barcode.
    pytest.param(
        {
            "outcome": "decide",
            "fields": {"library.chemistry": "10x-3p-gex-v3", "library.roles.R1": "R1"},
        },
        _result(roles={"R1": "sha-r2", "R2": "sha-r1"}), 0, Grade.FALSE_ACCEPT, None, False,
        id="swapped-roles-are-a-false-accept-even-with-the-right-chemistry",
    ),
    pytest.param(
        {"outcome": "decide", "fields": {"library.nonsense": "x"}},
        _result(), 0, Grade.FALSE_ACCEPT, "unsupported", False,
        id="an-unsupported-field-path-is-visible-not-silently-green",
    ),
]  # fmt: skip


@pytest.mark.parametrize(
    "expected, result, exit_code, grade, note, missed_question", CONFUSION_MATRIX
)
def test_the_confusion_matrix_grades_every_cell(
    expected: dict[str, object],
    result: ResolveResult,
    exit_code: int,
    grade: Grade,
    note: str | None,
    missed_question: bool,
) -> None:
    """Every cell of the grading matrix, especially the ones that matter.

    A harness that has only ever read "green" is indistinguishable from a broken one, so each row
    mutates a known-good case until it is wrong and pins the grade the harness must return.
    """
    g = _grade(expected, result, exit_code)

    assert g.grade is grade
    assert g.ok is (grade is Grade.CORRECT)  # `ok` is the grade, never a second opinion
    assert g.missed_question is missed_question
    if note is not None:
        shown = " ".join(g.notes) + " " + " ".join(str(f.actual) for f in g.fields)
        assert note in shown, f"the reason never reached the human: {shown!r}"


def test_outcome_of_maps_the_uniform_exit_contract() -> None:
    assert outcome_of(0) == "decide"
    assert outcome_of(3) == "refuse"
    assert outcome_of(4) == "ask"
    assert outcome_of(1) == "error"


def test_error_exit_is_not_silently_correct() -> None:
    g = _grade({"outcome": "decide"}, _result(), 1)
    assert g.grade is Grade.FALSE_REFUSE


def test_a_dataset_that_resolved_into_two_assays_reports_both_and_fails() -> None:
    """A project holding two assays has no ONE `library.chemistry`, and grading has to say so.

    `reduce_dataset` partitions the runs by chemistry and more than one group is a legal verdict,
    not an error — but a pre-registration naming a single chemistry did not predict this dataset. If
    the harness graded a representative assay instead, the case would PASS whenever the expectation
    happened to name that one, having compiled a dataset nobody described.
    """
    both = ["10x-3p-gex-v3", "bulk-rnaseq-pe"]
    g = grade_case(
        "t",
        Expected.model_validate({"outcome": "decide", "fields": {"library.chemistry": both[0]}}),
        _result(),
        0,
        LABELS,
        chemistries=both,
    )
    assert g.grade is Grade.FALSE_ACCEPT
    assert [c.actual for c in g.fields if c.path == "library.chemistry"] == [both]


def test_one_assay_grades_exactly_as_the_winning_candidate_does() -> None:
    """The ordinary case: one assay reads off the representative result, unchanged by the partition."""
    expected = {"outcome": "decide", "fields": {"library.chemistry": "10x-3p-gex-v3"}}
    args = ("t", Expected.model_validate(expected), _result(), 0, LABELS, None)
    assert grade_case(*args).grade is Grade.CORRECT
    assert grade_case(*args, chemistries=["10x-3p-gex-v3"]).grade is Grade.CORRECT


# --------------------------------------------------------------------------------------------
# harvest grading: a verified-but-wrong assertion is a false accept
# --------------------------------------------------------------------------------------------


def test_hallucinated_assertion_rolls_up_to_false_accept() -> None:
    """bytes can never contradict experiment.* — a wrong assertion there reaches the manifest unchecked."""
    g = _grade({"outcome": "decide"}, _result(), 0)
    assert g.grade is Grade.CORRECT
    folded = _fold_harvest(g, HarvestGrade(hallucinated=["library.chemistry"]))
    assert folded.grade is Grade.FALSE_ACCEPT
    assert "does not make" in folded.notes[-1]


def test_fold_harvest_does_not_mutate_the_grade_it_is_given() -> None:
    """Regression: _worst() returns a REFERENCE into the trials list, not a copy.

    Folding used to mutate in place, so folding the worst grade rewrote a list element that
    `stability` was then counted over — reporting 0.667 for three identical, perfectly stable trials.
    A metric that is quietly wrong is worse than no metric.
    """
    g = _grade({"outcome": "decide"}, _result(), 0)
    folded = _fold_harvest(g, HarvestGrade(hallucinated=["library.chemistry"]))
    assert folded is not g
    assert g.grade is Grade.CORRECT, "the original grade must be untouched"
    assert folded.grade is Grade.FALSE_ACCEPT
    assert g.notes == [], "notes must not leak into the caller's grade either"


def test_missing_stated_field_is_wrong_reason_not_false_accept() -> None:
    """Under-extraction is a recall failure, not corpus poison. Grading both alike would hide one."""
    g = _grade({"outcome": "decide"}, _result(), 0)
    folded = _fold_harvest(g, HarvestGrade(missing=["experiment.organism"]))
    assert folded.grade is Grade.WRONG_REASON


def test_hallucination_outranks_a_missing_field() -> None:
    g = _grade({"outcome": "decide"}, _result(), 0)
    folded = _fold_harvest(
        g, HarvestGrade(missing=["experiment.organism"], hallucinated=["library.chemistry"])
    )
    assert folded.grade is Grade.FALSE_ACCEPT


# --------------------------------------------------------------------------------------------
# report aggregation
# --------------------------------------------------------------------------------------------


def _run(grade: Grade, *, skipped: str | None = None, actual: str = "decide") -> CaseRun:
    g = grade_case("c", Expected(outcome="decide"), _result(), 0, LABELS)
    g.grade = grade
    g.actual_outcome = actual
    return CaseRun("c", g, skipped=skipped)


def test_skipped_cases_are_excluded_from_every_rate() -> None:
    """A skip is not a pass. Counting it as one would let a missing API key look like success."""
    report = build_report([_run(Grade.CORRECT), _run(Grade.CORRECT, skipped="no key")])
    assert report.n_cases == 1


def test_false_accept_rate_counts_only_false_accepts() -> None:
    report = build_report(
        [
            _run(Grade.FALSE_ACCEPT),
            _run(Grade.CORRECT),
            _run(Grade.FALSE_REFUSE),
            _run(Grade.OVER_ASK),
        ]
    )
    assert report.n_cases == 4
    assert report.false_accept_rate == 0.25
    assert report.false_refuse_rate == 0.25


def test_questions_asked_counts_the_ask_outcome() -> None:
    report = build_report([_run(Grade.CORRECT, actual="ask"), _run(Grade.CORRECT)])
    assert report.questions_asked["total"] == 1.0
    assert report.questions_asked["per_case"] == 0.5


# --------------------------------------------------------------------------------------------
# cases: the corpus itself, and the recipe machinery
# --------------------------------------------------------------------------------------------


def test_the_corpus_is_well_formed() -> None:
    """Four one-line layout properties of the corpus, off ONE `discover_cases()` walk.

    They were four separate tests, each walking the corpus to ask one question of it. Nothing is
    weakened by asking all four in one pass — a failure still names which property broke and which
    case broke it.

    1. It covers every outcome class. A corpus that only ever expects `decide` cannot catch a
       harness that has forgotten how to refuse.
    2. Every case sits under one named purpose group — `spec` (one per KB leaf), `prose` (needs
       harvest), `steering` (a hypothesis meets the bytes), `refusal` (must block), `real` (real
       local data), `grouping` (which sample each file is, from its NAME — the deposit shape #263
       compiled wrong). Grouping is a filing decision, but pinning it means a stray case dropped at
       the top level, or a seventh ad-hoc group, turns red instead of quietly re-messing the
       directory.
    3. Every case says what it is for. A case whose intent is not written down cannot be maintained
       when it fails.
    4. No case ships FASTQ bytes. Inputs are recipes; a committed FASTQ means a case stopped
       tracking its spec.
    """
    base = default_cases_dir()
    cases = discover_cases()
    groups = {"spec", "prose", "steering", "refusal", "real", "grouping"}

    assert len(cases) >= 7
    assert {c.expected.outcome for c in cases} == {"decide", "refuse", "ask"}, (
        "the corpus must exercise every outcome class"
    )
    assert HERMETIC_CASES, "every case needs the LLM — `test_corpus_is_green` would run nothing"

    for case in cases:
        group = case.root.resolve().parent
        assert group.parent == base.resolve() and group.name in groups, (
            f"{case.id} is at {case.root.relative_to(base)}, not under one of {sorted(groups)}"
        )
        assert case.expected.description.strip(), f"{case.id} has no description"

    stray = [p for p in base.rglob("*") if p.suffix in (".gz", ".fastq", ".fq")]
    assert not stray, f"eval cases must ship recipes, not bytes: {stray}"


def test_a_case_that_expects_a_question_pins_which_question() -> None:
    """`outcome: ask` obliges a `conflict:` block that says WHAT is being asked, in both tiers.

    Without it the expectation is only "it stopped", which the exit code already said. A case could
    then keep passing while the resolver stopped for an entirely unrelated reason — the failure mode
    the blocker-code check has always forbidden on the refusal side, and the same argument applies
    here.

    `field` alone is not enough either, and the two shapes of exit 4 pin their reason differently. A
    Conflict is two positions that disagree, so `positions` is the assertion. A Question is a tie the
    bytes cannot break and has no positions at all, so its option set is. Requiring one or the other
    is what stops a chemistry question about an unrelated pair from satisfying a case.
    """
    roots = [default_cases_dir(), default_cases_dir().parent / "benchmark"]
    checked = 0
    for root in roots:
        if not root.is_dir():
            continue
        for case in discover_cases(root):
            if case.expected.outcome != "ask":
                continue
            checked += 1
            want = case.expected.conflict
            assert want is not None, f"{case.id}: expects a question but names none"
            assert want.field, f"{case.id}: names no field for the question it expects"
            assert want.positions or want.options, (
                f"{case.id}: pins only the field — a Conflict owes `positions`, a Question owes "
                f"`options`, or the expectation asserts nothing beyond 'it stopped'"
            )
    assert checked, "no case expects a question — this test is asserting nothing"


def test_ci_benchmark_covers_every_leaf_kb_spec() -> None:
    """One dataset per KB spec: every runnable leaf resolves in a hermetic (no-LLM, no-network) case.

    This is the ci-benchmark's contract — a code path that no case exercises can rot green. Only
    hermetic ``kind: spec`` cases count (a local/fingerprint case backed by out-of-git data skips in
    CI, so crediting it would let coverage lapse silently). Processing-equivalent twins (v3 <-> v3.1:
    identical ``backend.params``, so a real dataset only ever lands the pair) are credited to whichever
    twin has a case — exercising both would re-test the KB's benign-twin biconditional, not the
    resolver.
    """
    from seqforge import kb
    from seqforge.evals.case import SpecRecipe
    from seqforge.resolve.confuse import declared_equivalents

    leaves = {sid for sid in kb.list_spec_ids() if kb.load_spec(sid).backend is not None}
    covered: set[str] = set()
    for c in discover_cases():
        if not isinstance(c.recipe.generate, SpecRecipe) or c.needs_llm:
            continue
        chem = c.expected.fields.get("library.chemistry")
        if not isinstance(chem, str):
            continue
        covered.add(chem)
        covered |= declared_equivalents(kb.load_spec(chem))
    assert leaves <= covered, f"leaf spec(s) with no hermetic ci case: {sorted(leaves - covered)}"


def test_recipe_regenerates_identical_bytes(tmp_path: Path) -> None:
    """Determinism in (spec, seed) is what makes a recipe a legitimate substitute for the bytes.

    Byte-identity, not just record-identity: `.seqforge/` is content-addressed by file sha256, so a
    gzip header that varies with wall-clock would change the dataset id on every regeneration and
    silently defeat the cache. This caught exactly that — `gzip.open` stamps the current mtime, so the
    test failed only when two writes straddled a second boundary (~1 run in 3).
    """
    import hashlib

    case = next(c for c in discover_cases() if c.id == "10x-v3-bytes-only")
    a = materialize(case, tmp_path / "a")
    b = materialize(case, tmp_path / "b")
    assert [p.name for p in a.paths] == [p.name for p in b.paths]
    for pa, pb in zip(a.paths, b.paths, strict=True):
        assert pa.read_bytes() == pb.read_bytes(), f"{pa.name} is not reproducible"
        assert (
            hashlib.sha256(pa.read_bytes()).hexdigest()
            == hashlib.sha256(pb.read_bytes()).hexdigest()
        )


def test_generated_gz_pins_mtime_so_the_header_is_content_only(tmp_path: Path) -> None:
    """Pin the mechanism, not just the symptom: the sha must not move when the clock does.

    The byte-identity test above only catches a wall-clock-dependent header when two writes happen to
    land in different seconds. This asserts the header field itself, so the guarantee cannot regress
    back into a 1-in-3 flake.
    """
    import hashlib
    import struct

    case = next(c for c in discover_cases() if c.id == "10x-v3-bytes-only")
    built = materialize(case, tmp_path / "a")
    raw = built.paths[0].read_bytes()
    # gzip header: magic(2) CM(1) FLG(1) MTIME(4, little-endian) ...
    assert raw[:2] == b"\x1f\x8b"
    assert struct.unpack("<I", raw[4:8])[0] == 0, "gzip header carries a wall-clock mtime"

    later = materialize(case, tmp_path / "b")
    assert (
        hashlib.sha256(raw).hexdigest() == hashlib.sha256(later.paths[0].read_bytes()).hexdigest()
    )


def test_truncate_recipe_actually_truncates(tmp_path: Path) -> None:
    case = next(c for c in discover_cases() if c.id == "truncated-gzip")
    built = materialize(case, tmp_path / "t")
    # By LABEL, not by filename: a generated file is named for the run it belongs to and the mate
    # slot it fills, and the read it carries is what `labels` records.
    by_read = {built.labels[p.name]: p for p in built.paths}
    assert by_read["R1"].stat().st_size < by_read["R2"].stat().st_size


def test_truncate_naming_a_nonexistent_read_is_a_case_error(tmp_path: Path) -> None:
    """A typo'd read id must be a loud case error, never a silently un-truncated (passing) case."""
    recipe = Recipe.model_validate(
        {
            "generate": {
                "kind": "spec",
                "spec": "10x-3p-gex-v3",
                "n": 50,
                "truncate": {"file": "R9", "fraction": 0.5},
            }
        }
    )
    case = Case("bad", tmp_path, recipe, Expected(outcome="refuse"), [])
    with pytest.raises(CaseError, match="R9"):
        materialize(case, tmp_path / "x")


def test_unknown_spec_is_a_case_error(tmp_path: Path) -> None:
    recipe = Recipe.model_validate({"generate": {"kind": "spec", "spec": "not-a-tech"}})
    case = Case("bad", tmp_path, recipe, Expected(outcome="decide"), [])
    with pytest.raises(CaseError, match="not-a-tech"):
        materialize(case, tmp_path / "x")


def test_a_local_case_skips_when_its_root_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local case's data lives outside the repo. Absent => skip, never pass, never fail."""
    monkeypatch.delenv("SEQFORGE_TEST_LOCAL", raising=False)
    recipe = Recipe.model_validate(
        {"generate": {"kind": "local", "root_env": "SEQFORGE_TEST_LOCAL"}}
    )
    case = Case("local", tmp_path, recipe, Expected(outcome="decide"), [])
    run = run_case(case)
    assert run.skipped is not None
    assert "SEQFORGE_TEST_LOCAL" in run.skipped
    assert build_report([run]).n_cases == 0


def test_prose_case_skips_without_llm_rather_than_failing() -> None:
    """Its expectation depends on a claim only the LLM supplies; byte-only would grade the wrong thing."""
    case = next(c for c in discover_cases() if c.id == "chemistry-unstated-trap")
    run = run_case(case, llm=False)
    assert run.skipped is not None
    assert "--llm" in run.skipped


def test_load_cases_rejects_an_unknown_id() -> None:
    with pytest.raises(CaseError, match="nope"):
        load_cases(only=["nope"])


def test_extra_keys_in_expected_are_rejected() -> None:
    """extra=forbid: a typo'd key must not silently assert nothing."""
    with pytest.raises(ValidationError, match="feilds"):
        Expected.model_validate({"outcome": "decide", "feilds": {"library.chemistry": "x"}})


# --------------------------------------------------------------------------------------------
# the end-to-end gate
# --------------------------------------------------------------------------------------------


#: The hermetic corpus, one item per case. Collected at import, which is what lets xdist spread it.
HERMETIC_CASES = [c for c in discover_cases() if not c.needs_llm]


@pytest.mark.parametrize("case", HERMETIC_CASES, ids=lambda c: c.id)
def test_corpus_is_green(case: Case) -> None:
    """The deterministic corpus, through the real compiler. No LLM, no network, no API key.

    **This stays in CI.** It is not `pixi run eval` leaking in: `.github/workflows/benchmark.yml`
    names this test as the per-commit tier and runs the networked HF tier separately, and
    `test_the_hf_benchmark_tier_is_well_formed_and_separate_from_the_hermetic_corpus` proves the two
    directories do not overlap. It is the only test that runs the compiler over the corpus.

    **Parametrized, one item per case**, because as a single unit it was the suite's most expensive
    test (6.89s, 7% of all measured time) and — worse — an INDIVISIBLE one, so xdist could not spread
    it and it sat on the critical path alone. Per case it is ~1.15s at worst, and the critical path
    drops to that. Two bonuses: a red run now names the case instead of printing a whole-corpus JSON
    blob, and `-k splitseq` becomes a rung-1 command.

    The aggregate assertions it used to carry (`false_accept_rate == 0.0`, `field_accuracy == 1.0`)
    are ENTAILED, not dropped: `grade_case` returns CORRECT only when no field check failed, so
    all-CORRECT gives both by construction. `build_report`'s rate arithmetic — including the
    `questions_asked` metric — is separately unit-tested above at <5ms, against
    synthetic runs rather than a 7-second corpus pass.

    The item count rises by 14 here, and that is the intended kind of growth: it tracks
    `discover_cases()`, so a new case gets a node the moment it exists.
    """
    run = run_case(case, llm=False)
    if run.skipped is not None:
        pytest.skip(run.skipped)
    assert run.grade.ok, run.to_json()


def test_run_cases_fans_out_and_aggregates() -> None:
    """`run_cases` is what `seqforge eval run` calls; `test_corpus_is_green` no longer does.

    Parametrizing the corpus moved that test onto `run_case` (singular), which left the plural — the
    one the CLI actually invokes — with no caller in the suite at all. It is two lines, and two lines
    that nothing tests are two lines that can break silently in the verb a release is graded by.

    One case, because what is under test is the fan-out and the hand-off to `build_report`, not the
    corpus. The report is round-tripped through JSON here too: that claim used to have a test of its
    own over a synthetic run, and this is a better place for it — the report came from a real one.
    """
    from seqforge.evals import run_cases

    case = next(c for c in HERMETIC_CASES if c.id == "10x-v3-bytes-only")
    report, runs = run_cases([case], llm=False)

    assert [r.case_id for r in runs] == [case.id]
    assert report.n_cases == 1
    assert report.false_accept_rate == 0.0
    assert report.model_dump(mode="json")["n_cases"] == 1


# --------------------------------------------------------------------------------------------
# the --llm path, driven offline by a stub provider
#
# These pin the *grading* of a model's behaviour without paying a provider or depending on one
# being reachable. A real model is asked to do this for real by `eval run --llm`; here we hand the
# harness the exact outputs a good and a bad model would produce and assert it tells them apart.
# --------------------------------------------------------------------------------------------


class _StubProvider:
    """Returns a canned drafts payload. Mirrors tests/test_extract.py's fake."""

    name = "stub"

    def __init__(self, drafts: list[dict[str, object]]) -> None:
        self._payload = {"drafts": drafts}

    def default_model(self) -> str:
        return "stub-model-1"

    def complete_json(self, **kwargs: object) -> LLMResponse:
        import json as _json

        from seqforge.harvest import LLMResponse

        return LLMResponse(
            text=_json.dumps(self._payload),
            usage={"input_tokens": 100, "output_tokens": 20},
            # A real adapter records HOW the call was made, and the eval path used to drop it while
            # the CLI path kept it. A stub that returned no mode could not tell those two apart.
            mode={"max_tokens": 8000, "response_format": "json_object"},
        )


def _draft(fieldname: str, value: str, quote: str) -> dict[str, object]:
    # doc_sha256 is a placeholder on purpose: extract._anchor overwrites it with the real one.
    return {
        "field": fieldname,
        "value": value,
        "span": {"doc_sha256": "0" * 64, "quote": quote, "context": None},
        "llm_confidence": 0.95,
    }


def _trap_case() -> Case:
    return next(c for c in discover_cases() if c.id == "chemistry-unstated-trap")


def test_report_names_the_extractor_that_produced_it() -> None:
    """A baseline is model-scoped, so the report has to say which model it is a claim about.

    The DeepSeek preset serves two V4 models and defaults to the cheap one, so the same command over
    the same corpus can produce two different sets of numbers. `--model` is usually unset, which is
    exactly the case that must not report `null`: the effective model is the provider's default.
    """
    from seqforge.evals import run_cases

    case = _trap_case()
    provider = _StubProvider(
        [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    )

    inherited, _ = run_cases([case], llm=True, provider=provider)
    assert inherited.extractor is not None
    assert inherited.extractor["provider"] == "stub"
    assert inherited.extractor["model"] == provider.default_model()  # the DEFAULT, not None
    assert inherited.extractor["prompt_version"] == EXTRACT_PROMPT_VERSION

    asked, _ = run_cases([case], llm=True, provider=provider, model="stub-model-pro")
    assert asked.extractor is not None
    assert asked.extractor["model"] == "stub-model-pro"

    # `--no-llm` has no extractor at all, and says so rather than naming one that never ran.
    byte_only, _ = run_cases(
        [next(c for c in HERMETIC_CASES if c.id == "10x-v3-bytes-only")], llm=False
    )
    assert byte_only.extractor is None
    assert byte_only.model_dump(mode="json")["extractor"] is None


def test_llm_case_passes_when_the_model_behaves() -> None:
    """Extract the stated organism, stay silent on the unstated chemistry."""
    case = _trap_case()
    provider = _StubProvider(
        [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    )
    run = run_case(case, llm=True, provider=provider)
    assert run.skipped is None
    assert run.grade.grade is Grade.CORRECT
    assert run.harvest is not None
    assert run.harvest.matched == ["experiment.organism"]
    assert run.harvest.hallucinated == []
    assert run.llm_calls == 1


def test_the_trap_fires_on_a_guess_that_happens_to_be_right() -> None:
    """The sharpest assertion in the suite.

    The stub claims chemistry v3 — which is what the BYTES independently say, and what resolve
    decides. The value is correct about the world. The document never states it, so the claim is a
    guess, and the harness must grade it false_accept anyway. If this test ever goes green by the
    grade becoming CORRECT, the harness has started rewarding lucky guesses.
    """
    case = _trap_case()
    provider = _StubProvider(
        [
            _draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans"),
            # a REAL span from the document, pinned to a conclusion it does not support
            _draft("library.chemistry", "10x-3p-gex-v3", "droplet-based single-cell"),
        ]
    )
    run = run_case(case, llm=True, provider=provider)
    assert run.harvest is not None
    # Either the span-verification tripwire rejects the claim (entailment fails), or it survives and the case
    # grades false_accept. Both are acceptable; silently passing is not.
    if run.harvest.hallucinated:
        assert run.grade.grade is Grade.FALSE_ACCEPT
    else:
        assert run.harvest.n_rejected >= 1, "an unsupported chemistry claim must not pass verify"
        assert run.grade.grade is Grade.CORRECT


def test_fabricated_quote_is_caught_by_the_tripwire_not_the_grader() -> None:
    """Defence in depth: a quote that is not in the document dies at verify, before grading.

    And it dies **legibly**. The refusal used to survive as one increment of an integer, so a report
    could say four drafts were thrown out and nothing about which claim, from which document, for
    what reason — the count says a net caught something and nothing whatever about what.
    """
    case = _trap_case()
    provider = _StubProvider(
        [
            _draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans"),
            _draft("library.chemistry", "10x-3p-gex-v3", "we used the Chromium Single Cell 3' v3"),
        ]
    )
    run = run_case(case, llm=True, provider=provider)
    assert run.harvest is not None
    assert run.harvest.n_rejected >= 1
    assert "library.chemistry" not in {a.field for a in run.harvest.assertions}

    refused = next(r for r in run.harvest.rejected if r["field"] == "library.chemistry")
    assert refused["reason"] == "span_not_found"
    assert refused["quote"] == "we used the Chromium Single Cell 3' v3", "the quote, not a tally"
    assert refused["value"] == "10x-3p-gex-v3"
    # ...and which document it was refused against, which is what makes it readable at all — and
    # what joins it back to the exchange that produced it.
    assert refused["doc_sha256"] in {d["doc_sha256"] for d in run.harvest.documents}
    assert run.harvest.to_json()["n_rejected"] == len(run.harvest.rejected)


def test_the_harvest_grade_keeps_the_quote_it_graded() -> None:
    """A graded claim travels whole — value, quote, span, document — not as `field -> str(value)`.

    Flattening it was a claim about the model's answer with the evidence for the answer removed: the
    report could say `experiment.organism = "Caenorhabditis elegans"` and never *from this quote, in
    this document, at these offsets*, which is the entire content of the tripwire that let the claim
    through. `matched`/`missing`/`hallucinated` are verdicts about these Assertions; a verdict whose
    evidence was thrown away on the way to the report cannot be checked by the person reading it.
    """
    run = run_case(
        _trap_case(),
        llm=True,
        provider=_StubProvider(
            [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
        ),
    )
    assert run.harvest is not None
    (claim,) = run.harvest.assertions
    assert claim.field == "experiment.organism"
    assert claim.value == "Caenorhabditis elegans"
    assert claim.span.quote == "Caenorhabditis elegans"
    # code-owned, and the reason a quote is checkable rather than decorative
    assert claim.span.char_start is not None and claim.span.char_end is not None
    assert claim.span_verified and claim.entailment_ok

    row = run.harvest.to_json()
    assert row["assertions"][0]["span"]["quote"] == "Caenorhabditis elegans", "and it survives JSON"
    assert "extracted" not in row, "the flattened dict is gone, not carried alongside"
    # the documents that were sent, so a sha256 in a span resolves back to something readable
    sources = {d["doc_sha256"]: d for d in row["documents"]}
    assert claim.span.doc_sha256 in sources
    assert sources[claim.span.doc_sha256]["scope"] in {"dataset", "sample", "experiment", "run"}
    assert row["mode"], "how the call was made was dropped on this path and the CLI path kept it"


def test_a_malformed_draft_reaches_the_grade_instead_of_the_floor() -> None:
    """The extract-time refusal channel, which the eval path read past entirely.

    `ExtractionOutcome` has four halves and the loop read two, so a model returning nothing but
    broken drafts graded identically to one that read the document and honestly found nothing. Both
    producers of a refusal now land in one list, and the reason tells them apart.
    """
    bad: dict[str, object] = {"field": "experiment.organism", "value": None, "span": {"quote": "x"}}
    run = run_case(_trap_case(), llm=True, provider=_StubProvider([bad]))

    assert run.harvest is not None
    (refused,) = run.harvest.rejected
    assert refused["reason"] == "malformed_draft"
    assert refused["doc_sha256"] in {d["doc_sha256"] for d in run.harvest.documents}
    assert run.harvest.assertions == []


def test_under_extraction_is_graded_wrong_reason_not_correct() -> None:
    """A model that returns nothing is not correct just because it hallucinated nothing."""
    case = _trap_case()
    run = run_case(case, llm=True, provider=_StubProvider([]))
    assert run.harvest is not None
    assert run.harvest.missing == ["experiment.organism"]
    assert run.grade.grade is Grade.WRONG_REASON


def test_harvest_hypothesis_steers_resolve() -> None:
    """The full stack: prose -> verified assertion -> hypothesis -> resolve agrees -> decide."""
    case = next(c for c in discover_cases() if c.id == "10x-v3-prose")
    provider = _StubProvider(
        [
            _draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans"),
            _draft("library.chemistry", "10x-3p-gex-v3", "Chromium Single Cell 3' v3 Reagent Kit"),
        ]
    )
    run = run_case(case, llm=True, provider=provider)
    assert run.grade.grade is Grade.CORRECT
    assert run.harvest is not None
    assert sorted(run.harvest.matched) == ["experiment.organism", "library.chemistry"]


def _capture_hypotheses(monkeypatch: pytest.MonkeyPatch) -> list[Hypothesis | None]:
    """Record the hypothesis each `resolve_runs` call is handed, then let the real one run."""
    from seqforge.evals import run as run_module
    from seqforge.resolve import resolve_runs as real  # the same object `run.py` imported

    seen: list[Hypothesis | None] = []

    def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("hypothesis"))
        return real(*args, **kwargs)

    monkeypatch.setattr(run_module, "resolve_runs", _spy)
    return seen


def _two_chemistry_provider() -> _StubProvider:
    """A model that names v3 and v2. Each draft verifies against exactly one of the two documents."""
    return _StubProvider(
        [
            _draft("library.chemistry", "10x-3p-gex-v3", "Chromium Single Cell 3' v3 Reagent Kit"),
            _draft("library.chemistry", "10x-3p-gex-v2", "Chromium Single Cell 3' v2 Reagent Kit"),
        ]
    )


def _prose_case_plus_a_second_chemistry(tmp_path: Path, *, hypothesis: str | None = None) -> Case:
    """`10x-v3-prose`, plus a document naming a DIFFERENT chemistry. Two documents, two answers.

    A second document rather than a second draft on the first: `verify_drafts` anchors every draft to
    the document it was extracted from, so a v2 quote absent from the v3 methods text is rejected
    before it can become an assertion. Two documents is also the real shape — GSE234962's four
    experiment records are what put a second chemistry into that dataset's accepted set.

    ``hypothesis`` declares one in the recipe, as a case that must run without an API key does.
    """
    import dataclasses

    second = tmp_path / "supplementary_methods.txt"
    second.write_text(
        "Supplementary Methods\n\n"
        "The pilot libraries were prepared with the Chromium Single Cell 3' v2 Reagent Kit.\n"
    )
    case = next(c for c in discover_cases() if c.id == "10x-v3-prose")
    return dataclasses.replace(
        case,
        metadata_docs=[*case.metadata_docs, second],
        recipe=case.recipe.model_copy(update={"hypothesis": hypothesis}),
    )


def test_two_documents_naming_two_chemistries_steer_the_harness_with_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness reduces prose to a hypothesis exactly as `manifest fill` does: unanimity or nothing.

    It used to reduce it with a last-wins `by_field` dict, so a dataset whose prose named two
    chemistries handed the scorer whichever document happened to be extracted last, while the
    compiler over the identical prose handed it nothing. That is a harness failing differently from
    the thing it measures (#188), and it makes a benchmark number a claim about the benchmark.

    The harvest GRADE is deliberately not asserted here: "did the model say this at all" is a
    different question from "what did the manifest store", and only the second one is this reduction.
    """
    seen = _capture_hypotheses(monkeypatch)
    run_case(
        _prose_case_plus_a_second_chemistry(tmp_path), llm=True, provider=_two_chemistry_provider()
    )
    assert seen == [None], "two accepted chemistry claims must steer nothing"


def test_a_harvest_that_agrees_on_nothing_leaves_the_recipes_hypothesis_standing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`None` from harvest means "no opinion", never "override the declared one with nothing".

    A case may declare its hypothesis in `inputs/recipe.yaml` so it runs with no API key; the
    benchmark's one metadata-decided case (`GSE317744`) is graded on chemistry through exactly that
    channel. Reducing harvest's two answers to `None` must leave the recipe's claim where it was —
    the harness's `if hyp is not None` guard, asserted rather than assumed.
    """
    case = _prose_case_plus_a_second_chemistry(tmp_path, hypothesis="10x-3p-gex-v3")
    seen = _capture_hypotheses(monkeypatch)
    run_case(case, llm=True, provider=_two_chemistry_provider())
    assert len(seen) == 1 and seen[0] is not None
    assert (seen[0].value, seen[0].id) == ("10x-3p-gex-v3", "recipe")


# --------------------------------------------------------------------------------------------
# the contract: two front doors onto one compiler, and therefore onto one manifest
# --------------------------------------------------------------------------------------------

#: The organism both paths are pinned to. Nothing in this case declares one — it has no archive
#: records — and a manifest refuses to be assembled without an organism, so leaving it unpinned
#: would put a difference into the manifest that says nothing whatever about the contract.
CONTRACT_TAXID = 6239


#: The two run accessions the fixture below deposits its generated pairs under. Two, because ONE run
#: cannot tell the two resolvers apart: `resolve_dataset` scores a whole file list as one library and
#: `resolve_runs` scores each run on its own bytes, and on a single-run dataset those are the same
#: call. The whole divergence this contract now guards (#196) is invisible below two.
CONTRACT_RUNS = ("SRR9000001", "SRR9000002")


def _two_bulk_runs(tmp_path: Path) -> Path:
    """The corpus's own `bulk-pe-bytes-only` bytes, TWICE, in one directory that both paths read.

    **Bulk, because it is the shape that needs no whitelist.** A case generated from a barcoded spec
    hands the harness a registry built from the very pools its reads were drawn from, while
    `manifest fill` takes no registry at all and always resolves against the shipped one — so its
    synthetic barcodes would miss every real whitelist and the two paths could not be compared on
    any manifest. That is a property of the corpus's generator, not of the pipeline, and choosing
    the no-onlist shape removes it rather than papering over it.

    **Two runs, and a different seed for each.** `materialize` deposits one case's files under ONE
    run (`SIM_R1`, `SIM_R2`), because one KB spec is one library — so a multi-run dataset is built by
    generating it twice and re-depositing each pair under its own accession. Only the run stem moves;
    the mate token the generator wrote is left exactly as it is, so neither pair is renamed into a
    shape the resolver reads differently. The seeds differ because the two runs must be different
    bytes: identical reads content-address to identical shas, and a manifest keyed by sha would
    silently hold two files where four were handed to it.
    """
    import dataclasses

    case = next(c for c in discover_cases() if c.id == "bulk-pe-bytes-only")
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    for seed, accession in enumerate(CONTRACT_RUNS):
        seeded = case.recipe.generate.model_copy(update={"seed": seed})
        built = materialize(
            dataclasses.replace(case, recipe=case.recipe.model_copy(update={"generate": seeded})),
            tmp_path / f"gen-{accession}",
        )
        for path in built.paths:
            path.rename(data / path.name.replace(SIM_RUN, accession, 1))
    return data


def _two_chemistry_case_over(data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Case:
    """A case over `data` carrying two documents that name two DIFFERENT chemistries.

    Two claims rather than one is what makes the reduction load-bearing. With one claim the
    last-wins dict the harness used to keep and the compiler's agreement-or-nothing rule return the
    same hypothesis, and no comparison downstream of them can tell the two apart. With two they
    return different answers — and against barcodeless bulk reads a single-cell hypothesis surfaces
    a cross-family conflict, so a harness reducing prose its own way *stops* where the compiler
    decides.

    A `local` recipe rather than a copy: it points the harness at this directory instead of
    generating its own, so both paths probe the same inodes and the file URIs cannot drift.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    first = docs / "methods.txt"
    first.write_text(
        "Methods\n\nLibraries were prepared with the Chromium Single Cell 3' v3 Reagent Kit.\n"
    )
    second = docs / "supplementary_methods.txt"
    second.write_text(
        "Supplementary Methods\n\n"
        "The pilot libraries used the Chromium Single Cell 3' v2 Reagent Kit.\n"
    )
    monkeypatch.setenv("SEQFORGE_CONTRACT_ROOT", str(data))
    recipe = Recipe.model_validate(
        {"generate": {"kind": "local", "root_env": "SEQFORGE_CONTRACT_ROOT"}}
    )
    return Case(
        "harness-vs-front-door",
        tmp_path,
        recipe,
        Expected(outcome="decide", fields={"library.chemistry": "bulk-rnaseq-pe"}),
        [first, second],
    )


def _harness_decisions(
    case: Case, provider: _StubProvider | None, monkeypatch: pytest.MonkeyPatch
) -> tuple[CaseRun, DatasetResolution, MetadataResolution]:
    """`run_case` in full, plus the two resolutions it reached: `(run, dataset resolve, metadata)`.

    Spies rather than a reimplementation — each records what the real function returned and hands it
    straight back — so what is captured is what the harness actually decided, on the path a
    `seqforge eval run` takes. The byte spy sits on `reduce_dataset` rather than on `resolve_runs`,
    because the dataset-level verdict is what the harness grades and what the front door renders;
    the run-by-run resolve underneath it is the same function on both paths and never diverged.

    ``provider=None`` is a `--no-llm` run, for a case whose subject is the bytes and the filenames.
    It costs one resolve rather than two: a caller that wants both the GRADE and the sample -> files
    map has to see the same pass produce them, or it is asserting over a second run of the compiler.
    """
    from seqforge.evals import run as run_module
    from seqforge.resolve import reduce_dataset as real_reduce
    from seqforge.resolve.records import resolve_metadata as real_metadata

    seen: dict[str, Any] = {}

    def _reduce(*args: Any, **kwargs: Any) -> Any:
        seen["resolve"] = real_reduce(*args, **kwargs)
        return seen["resolve"]

    def _metadata(*args: Any, **kwargs: Any) -> Any:
        seen["metadata"] = real_metadata(*args, **kwargs)
        return seen["metadata"]

    monkeypatch.setattr(run_module, "reduce_dataset", _reduce)
    monkeypatch.setattr(run_module, "resolve_metadata", _metadata)
    run = run_case(case, llm=provider is not None, provider=provider)
    return run, seen["resolve"], seen["metadata"]


def _manifest_the_harness_decided(
    out: DatasetResolution, metadata: MetadataResolution
) -> DatasetManifest | None:
    """What the compiler would have written from the harness's own two resolutions — or ``None``.

    The harness stops at a graded `ResolveResult` and builds no manifest, which is exactly why this
    contract has to be stated rather than read off a file. `fill_manifest` is a pure function of
    what the two resolvers decided, so assembling the harness's outputs with the compiler's OWN
    assembler asks "would `manifest fill` have written this?" and asks nothing at all about the
    assembler, which is shared and is not what ever diverged.

    ``role_of_sha`` is passed for the reason it exists: a `RoleAssignment` maps each role to ONE
    file, so a two-run dataset's second pair has no role in it at all. Omitting it here would leave
    those files bare in the harness's manifest and present in the front door's — a difference this
    fixture would report as a divergence when it is only this helper under-calling the assembler.

    ``None`` is the compiler's own gate, not an absence: the fill pipeline returns before it
    assembles anything when the byte resolve does not exit 0, so a refusal here is a comparable
    answer rather than a missing one — and it is the answer a divergent hypothesis produces.
    """
    from seqforge import __version__, kb
    from seqforge.io import DEFAULT_REGISTRY
    from seqforge.manifest import dataset_uris, experiment_from_metadata, fill_manifest

    if out.exit_code != 0:
        return None
    uris = dataset_uris(out.observations)
    return fill_manifest(
        result=out.result,
        spec=kb.load_spec(out.result.candidates[0].technology),
        observations=out.observations,
        registry=DEFAULT_REGISTRY,
        experiment=experiment_from_metadata(
            metadata, out.observations, organism_taxid=CONTRACT_TAXID, uris=uris
        ),
        seqforge_version=__version__,
        role_of_sha=out.role_of_sha(),
        uris=uris,
    )


def _flat(value: Any, prefix: str = "") -> dict[str, Any]:
    """A manifest as ``dotted.path -> scalar``, so a mismatch can name a field."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, sub in value.items():
            out.update(_flat(sub, f"{prefix}.{key}" if prefix else str(key)))
        return out
    if isinstance(value, list):
        listed: dict[str, Any] = {}
        for i, sub in enumerate(value):
            listed.update(_flat(sub, f"{prefix}[{i}]"))
        return listed
    return {prefix: value}


def _field_diff(harness: dict[str, Any], front_door: dict[str, Any]) -> str:
    """Only the fields that differ, one per line. Two 400-line dumps are not a diagnosis."""
    a, b = _flat(harness), _flat(front_door)
    return "\n".join(
        f"  {path}: harness={a.get(path)!r} manifest-fill={b.get(path)!r}"
        for path in sorted(set(a) | set(b))
        if a.get(path) != b.get(path)
    )


def test_the_harness_and_manifest_fill_compile_one_case_into_one_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One case, both front doors, one manifest — the guard the last divergence lived without.

    `evals/run.py` calls the product's own `resolve_runs`, `reduce_dataset`, `resolve_metadata`,
    `extract_planned` and `chemistry_hypothesis`, so there is no second pipeline left to unify.
    Nothing *pinned* that, though, and the divergences there were went unnoticed for years of the
    corpus's life: the harness reducing a dataset's chemistry claims with a last-wins `by_field`
    dict while `manifest fill` took them agreement-or-nothing (#188), and then the harness calling
    `resolve_dataset` on a whole dataset while the front door called `resolve_runs` (#196). So: run
    one case through both doors and assert they land on the same manifest, hash and field by field.

    **The case is MULTI-RUN, and that is what makes the second divergence visible.**
    `resolve_dataset` and `resolve_runs` are the same call on a one-run dataset, so the single-run
    case this test was born with could not tell them apart — and had to rename its files into one
    run to get `manifest fill` to accept them at all. Two runs of bulk reads is the smallest shape
    where the two resolvers answer differently: whole-dataset scoring seats one (R1, R2) pair out of
    the four files and leaves the other two with no role, which lands in the manifest as two bare
    inventory rows and a different `dataset_hash`.

    **The case is also built so the prose reduction decides the answer.** Two documents name two
    different chemistries over barcodeless bulk reads. Agreement-or-nothing yields no hypothesis and
    the bytes decide `bulk-rnaseq-pe` at exit 0; any reduction that picks one of the two instead
    hands a single-cell claim to bulk bytes, which surfaces a cross-family conflict at exit 4 — and
    a pipeline that stops writes no manifest at all. That is why the outcome is compared before the
    content: the divergence changes *whether* there is a manifest, and the two manifests it does
    produce are identical.

    **What is pinned, and why each one is.**

    - *the files* — a `local` recipe aims the harness at the same directory the argv names, so
      neither path generates its own bytes and the URIs cannot drift;
    - *the run count* — asserted below rather than assumed, because a fixture that quietly collapses
      to one run turns this back into a test that cannot see #196;
    - *the organism* — no record declares one and the manifest will not assemble without it;
    - *`--offline`* — the harness reaches no network by construction, so the front door must not
      either, or the comparison would depend on a socket;
    - *the accepted claims* — the front door is handed the harness's own verified assertions, in
      `harvest extract`'s artifact shape. Extraction is nondeterministic and is **not** the contract:
      both paths already call `extract_planned`. The reduction of what it returns is.

    **What this deliberately does NOT compare: the harvest grade.** `matched` / `missing` /
    `hallucinated` answer "did the model say this at all", which is a different question from "what
    did the manifest store" — on purpose, and it is why the harness keeps a per-field view the
    manifest has no room for. Do not "fix" that by asserting it here.

    The stub provider is the same offline one the rest of this file drives the `--llm` path with:
    no key, no socket, and a canned payload standing in for the two documents' claims.
    """
    import yaml

    from seqforge.manifest import dataset_content_hash

    data = _two_bulk_runs(tmp_path)
    case = _two_chemistry_case_over(data, tmp_path, monkeypatch)
    run, out, metadata = _harness_decisions(case, _two_chemistry_provider(), monkeypatch)

    assert run.skipped is None, run.skipped
    assert [r.run_id for r in out.runs.runs] == list(CONTRACT_RUNS), (
        "the harness must resolve this case as the two runs it is; one run cannot distinguish "
        "`resolve_dataset` from `resolve_runs`, which is the whole point of the fixture"
    )
    assert run.harvest is not None
    claimed = {a.value for a in run.harvest.assertions if a.field == "library.chemistry"}
    assert claimed == {"10x-3p-gex-v2", "10x-3p-gex-v3"}, (
        f"both claims must survive verification or the reduction is never exercised: {claimed}"
    )

    # `harvest extract`'s artifact, which is how `manifest fill` is given prose at all. The subjects
    # ride along because an assertion's doc_sha256 is an opaque hash without them.
    artifact = tmp_path / "assertions.json"
    artifact.write_text(
        json.dumps(
            {
                "assertions": [a.model_dump(mode="json") for a in run.harvest.assertions],
                "document_subjects": [
                    {"doc_sha256": d["doc_sha256"], "scope": d["scope"], "subject": d["subject"]}
                    for d in run.harvest.documents
                ],
            }
        )
    )

    workspace = tmp_path / "front-door"
    filled = CliRunner().invoke(
        app,
        [
            "manifest",
            "fill",
            *sorted(str(p) for p in data.glob("*.fastq.gz")),
            "--organism",
            str(CONTRACT_TAXID),
            "--offline",
            "--assertions",
            str(artifact),
            "-C",
            str(workspace),
        ],
    )

    assert filled.exit_code == out.exit_code, (
        f"the two paths disagree on the OUTCOME, before any manifest: harness exit "
        f"{out.exit_code}, `manifest fill` exit {filled.exit_code}\n{filled.stdout}"
    )

    harness = _manifest_the_harness_decided(out, metadata)
    written = workspace / "seqforge" / "manifest.yaml"
    assert written.is_file() is (harness is not None), (
        "one path produced a manifest and the other refused to"
    )
    assert harness is not None, "this case is meant to decide; a refusal grades nothing"

    front_door = DatasetManifest.model_validate(yaml.safe_load(written.read_text()))
    mine, theirs = harness.model_dump(mode="json"), front_door.model_dump(mode="json")
    assert mine == theirs, (
        "the same bytes and the same claims compiled into two different manifests:\n"
        + _field_diff(mine, theirs)
    )
    # Recomputed rather than read off `provenance`: the content address is what a processing
    # manifest pins to, so "the same manifest" has to mean the same identity a later compose
    # resolves against, not merely two files that happen to serialize alike.
    assert dataset_content_hash(harness) == dataset_content_hash(front_door)


# --------------------------------------------------------------------------------------------
# the record-less multi-lane deposit — a run spans its lanes (#263, ADR-0027)
# --------------------------------------------------------------------------------------------

#: The case this section stages, and the variable its `local` recipe reads. The ground truth lives in
#: `evals/cases/grouping/record-less-two-libraries-two-lanes/`; only the LAYOUT lives here, because a
#: `spec` recipe deposits one library under one run by construction and has no knob for a deposit.
LANE_CASE_ID = "record-less-two-libraries-two-lanes"
LANE_ROOT_ENV = "SEQFORGE_CASE_TWO_LIBRARIES_TWO_LANES"

#: `(library, sample-sheet entry, lane) -> reads`, deposited as `<name>_S<n>_L<lane>_<read>_001`.
#: Two libraries on one flowcell, two lanes each — GSE126954's shape
#: (`Murray_b01_S1_L001_R1_001.fastq.gz`, 14 libraries x 4 lanes), which is plain bcl2fastq and the
#: commonest deposit there is. The two libraries carry DIFFERENT `_S<n>`: that token is the
#: sample-sheet entry, it is the one thing separating them, and ADR-0027 never strips it.
#:
#: **The depths differ per lane because two lanes must be different BYTES.** Identical reads
#: content-address to identical shas, and a manifest keyed by sha would hold four files where eight
#: were handed to it — the same trap `_two_bulk_runs` names. Same seed, so both lanes draw from one
#: read stream, as one library sequenced twice does.
#:
#: **They stay inside 400–500 because a real lane carries its sibling's cycle count and a generated
#: one does not.** `kb/generate.py` draws each cDNA read's length uniformly in [60, 91], so a file's
#: MODAL length is a lottery over a wide band, while `index_tagged_roles` re-seats a surplus lane
#: file onto its role only within `_LANE_LEN_TOL` (3 bp) of that role's representative. A depth
#: outside this band leaves one of the eight files with no role at all — silent data loss, and
#: nothing whatever to do with grouping. Measured every 20 reads across 300–1600: 400–500 is a band
#: over which both reads' modes hold still (66 / 65), and 380 and 560 are two of the places they jump.
LANE_DEPTHS = {
    ("SIM_b01", "S1", "L001"): 500,
    ("SIM_b01", "S1", "L002"): 460,
    ("SIM_b02", "S2", "L001"): 440,
    ("SIM_b02", "S2", "L002"): 420,
}


def _two_libraries_two_lanes(tmp_path: Path) -> Path:
    """`bulk-pe-bytes-only`'s own bytes, deposited the way bcl2fastq deposits a two-lane run.

    **Bulk, for the reason `_two_bulk_runs` gives.** A `local` case is handed no registry, so it
    resolves against the SHIPPED whitelists exactly as `manifest fill` does — and a barcoded spec's
    synthetic barcodes, drawn from pools this fixture would have to register itself, miss every real
    one. That would turn a grouping case into a question about its own fixture's onlist. The bug is
    in the filename path and is chemistry-blind, so the chemistry that needs no whitelist is right.

    Only the run stem moves: the mate token the generator wrote (`SIM_R1` -> `_R1_001`) is carried
    through untouched, so `resolve.group_runs` and `resolve.engine._read_designation` read the same
    token off this deposit that they read off a submitter's file.
    """
    import dataclasses

    case = next(c for c in discover_cases() if c.id == "bulk-pe-bytes-only")
    data = tmp_path / "flowcell"
    data.mkdir(parents=True, exist_ok=True)
    for (library, entry, lane), depth in LANE_DEPTHS.items():
        generate = case.recipe.generate.model_copy(update={"n": depth})
        built = materialize(
            dataclasses.replace(case, recipe=case.recipe.model_copy(update={"generate": generate})),
            tmp_path / f"gen-{library}-{lane}",
        )
        for path in built.paths:
            mate = path.name.removeprefix(f"{SIM_RUN}_").split(".", 1)[0]
            path.rename(data / f"{library}_{entry}_{lane}_{mate}_001.fastq.gz")
    return data


def test_a_record_less_multi_lane_deposit_stays_two_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two libraries x two lanes, no record: TWO samples, not four and not one (#263, ADR-0027).

    **The corpus could not see this and that is why the case exists.** The ci tier is record-less
    but every case is one library under one run — `_materialize_spec` enforces it. The benchmark
    tier has the lane tokens and every lane-tokened case ships a `records.json`, so the join lands
    at the SAMPLE level, above the run, and the filename path never runs. #263 lived in the gap:
    `run_key` kept the lane, so a four-lane library became four runs, and with no record the run
    grouping IS the sample identity — four `<sample>.h5ad` at a quarter depth each, every file
    assigned, `validate` clean, exit 0.

    **The count is asserted here rather than in `expected.yaml` because it is not expressible
    there.** `Expected.fields` reaches `library.*`, `rung` and `experiment.*`, and a record-less
    dataset with no prose has no sample ATTRIBUTE — `experiment.samples.*.<attr>` is the empty list
    at two samples and at four alike. So the case's own expectation is graded by `run_case` (the
    chemistry, the exit code, the whole harness path) and the sample count is read off the SAME
    pass's spies. One pass, or this would be asserting over a second run of the compiler.

    **Both failure directions.** Four is the split #263 shipped; one is the merge ADR-0027 forbids,
    where `_S<n>` is stripped and two libraries on one flowcell become one plausible matrix nobody
    notices. A rule may fail toward the first and never toward the second, so both are pinned.
    """
    from seqforge.resolve.group import group_runs, lane_of, run_key

    data = _two_libraries_two_lanes(tmp_path)
    paths = sorted(data.glob("*.fastq.gz"))
    monkeypatch.setenv(LANE_ROOT_ENV, str(data))
    case = next(c for c in discover_cases() if c.id == LANE_CASE_ID)

    # The fixture really is multi-lane, asserted before anything is concluded from it. `(run_key,
    # lane)` is the key #263 grouped by, one token at a time: four of those is what makes "two runs"
    # below a claim rather than a description of a deposit that never had a lane in it.
    assert len(paths) == 8
    assert {lane_of(p) for p in paths} == {"L001", "L002"}
    assert len({(run_key(p), lane_of(p)) for p in paths}) == 4
    assert sorted(group_runs(paths)) == ["SIM_b01_S1", "SIM_b02_S2"]

    run, out, metadata = _harness_decisions(case, None, monkeypatch)
    assert run.skipped is None, run.skipped
    assert run.grade.ok, run.to_json()

    assert [s.sample_id for s in metadata.samples] == ["SIM_b01_S1", "SIM_b02_S2"], (
        "a record-less four-lane deposit is one sample per library; four is #263 and one is the "
        "merge ADR-0027 forbids"
    )
    assert [len(s.file_shas) for s in metadata.samples] == [4, 4], "a sample keeps both its lanes"
    # Every file, once. A sample holding a lane twice, or the eight collapsing to four, would leave
    # the counts above intact and the depth wrong — which is the failure #263 was, expressed in shas.
    assert len({sha for s in metadata.samples for sha in s.file_shas}) == len(paths)
    assert out.exit_code == 0 and sorted(out.assays) == ["bulk-rnaseq-pe"]
    # A fused run assigns roles across its lanes (`index_tagged_roles`), and a file with no role is
    # dropped by `_units` at exit 0 — the same silent-loss class one level down.
    assert len(out.role_of_sha()) == len(paths), (
        "a lane lost its role and would be dropped silently"
    )


# --------------------------------------------------------------------------------------------
# a stage that did not run, told apart from one that ran and found nothing (#182)
# --------------------------------------------------------------------------------------------


class _PoisonedProvider(_StubProvider):
    """Answers every document except the one whose text holds ``poison``.

    The real failure it stands in for is DeepSeek's empty/invalid ``json_object`` (#4), and the
    shape is the part worth copying: on `GSE234962` both aborts landed on `mmc2.txt`, a 1 KB
    supplementary table the model quotes rows of — so the failure keys on the length of the
    RESPONSE a document provokes, not on the length of the document.
    """

    name = "poisoned"

    def __init__(self, drafts: list[dict[str, object]], poison: str) -> None:
        super().__init__(drafts)
        self._poison = poison

    def complete_json(self, **kwargs: object) -> LLMResponse:
        if self._poison in str(kwargs["user"]):
            from seqforge.harvest import LLMResponse

            return LLMResponse(text="", usage={"input_tokens": 40})
        return super().complete_json(**kwargs)


def _two_document_trap(tmp_path: Path) -> Case:
    """The trap case plus a second document that states nothing — so one can fail and one survive."""
    import dataclasses

    second = tmp_path / "supplementary.txt"
    second.write_text("Table S1. A per-sample grid of nothing in particular.\n")
    case = _trap_case()
    return dataclasses.replace(case, metadata_docs=[*case.metadata_docs, second])


def test_a_case_whose_only_document_never_answered_grades_and_says_so() -> None:
    """The heart of #182: a skipped harvest stage must not read as a clean one.

    Five of seven prose-carrying benchmark cases aborted on the first graded tier pass and skipped
    ENTIRELY — so the byte half of each, which needs no model at all, went ungraded too, and the
    summary said nothing about any of them. Now the case grades what it can and the harvest grade
    carries the one word that qualifies the rest of it.
    """
    run = run_case(_trap_case(), llm=True, provider=_PoisonedProvider([], "droplet"))

    assert run.skipped is None, "the deterministic half of the case still runs and still grades"
    assert run.grade.grade is Grade.CORRECT
    assert run.harvest is not None
    assert run.harvest.status == "unmeasured"
    assert run.harvest.n_documents == 1 and run.harvest.n_documents_failed == 1
    # `could not check`, never `checked and found nothing` — the same split the corpus already makes
    # between a package that is `absent` and one that is `unavailable`.
    assert run.harvest.unchecked == ["experiment.organism"]
    # An empty `missing` is what keeps the grade above at `correct`: an unchecked field must not
    # fold in as a failed extraction, which would grade `wrong_reason` for a document nobody read.
    assert run.harvest.missing == []
    # and WHICH document never answered, in the row a reader looks a sha up in
    (doc,) = run.harvest.documents
    assert "failure" in doc and doc["failure"]
    assert run.harvest.to_json()["status"] == "unmeasured"


def test_one_documents_abort_no_longer_takes_the_whole_case_down(tmp_path: Path) -> None:
    """The sharpest edge of the finding, and the highest-value half of the fix.

    `--trials N` made this worse rather than measurable: one document's abort raised through the
    whole case, so all N trials skipped together and measured nothing. Now the documents that
    answered are still graded, and the one that did not is named.
    """
    good = [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    run = run_case(
        _two_document_trap(tmp_path), llm=True, provider=_PoisonedProvider(good, "Table S1")
    )

    assert run.skipped is None
    assert run.harvest is not None
    assert run.harvest.status == "partial"
    assert run.harvest.n_documents == 2 and run.harvest.n_documents_failed == 1
    # The surviving document was asked the same field, so the verdict on it is a REAL one.
    assert run.harvest.matched == ["experiment.organism"]
    assert run.harvest.unchecked == []
    assert run.grade.grade is Grade.CORRECT
    failed = [d for d in run.harvest.documents if "failure" in d]
    assert [d["source"] for d in failed] == ["supplementary.txt"]


def test_a_negative_verdict_needs_every_document_a_positive_one_needs_one(tmp_path: Path) -> None:
    """Measured live on `GSE234962`, and the reason the rule is asymmetric.

    Its paper aborted while its supplementary table answered. Both are dataset-scoped, so both were
    asked `experiment.organism` — and the first cut of this reported the binomial the paper writes
    fifteen times as a claim the model had failed to make. `missing` means the model read everything
    and did not say it, so one unread document unsettles it; `matched` needs a single document, so
    nothing unsettles it.
    """
    silent = run_case(
        _two_document_trap(tmp_path), llm=True, provider=_PoisonedProvider([], "droplet-based")
    )
    assert silent.harvest is not None
    assert silent.harvest.status == "partial", "the second document answered, and honestly said no"
    assert silent.harvest.unchecked == ["experiment.organism"]
    assert silent.harvest.missing == []
    assert silent.grade.grade is Grade.CORRECT, "not `wrong_reason`: nothing was established"


def test_an_unchecked_assertion_poisons_no_rate_and_is_reported_instead() -> None:
    """A skip poisons no rate — which is correct, and is exactly why it was invisible.

    So the constraint holds (nothing unchecked enters `field_accuracy`'s denominator) and the run
    report carries the coverage that says how much of the stage the rate is a claim about.
    """
    from seqforge.evals import run_cases

    report, _ = run_cases([_trap_case()], llm=True, provider=_PoisonedProvider([], "droplet"))

    assert report.field_accuracy == 1.0, "the byte half graded; the unchecked assertion did not"
    assert report.harvest is not None
    assert report.harvest["assertions_unchecked"] == 1.0
    assert report.harvest["cases_unmeasured"] == 1.0
    assert report.harvest["cases_complete"] == 0.0


def test_the_report_carries_the_plan_versus_issued_gap() -> None:
    """`eval plan` prices the tier; the run is where the gap between that and what was issued shows.

    141 planned, 68 issued, and no number anywhere said so. `documents_planned` is this run's own
    plan, so the comparison needs no second command and cannot be read against a different day's.
    """
    from seqforge.evals import run_cases

    good = [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    report, _ = run_cases([_trap_case()], llm=True, provider=_StubProvider(good))

    assert report.harvest is not None
    assert report.harvest["documents_planned"] == 1.0
    assert report.harvest["documents_extracted"] == 1.0
    assert report.harvest["documents_failed"] == 0.0
    assert report.harvest["cases_complete"] == 1.0
    # and it survives the wire, because a number nobody can read out of the JSON is not reported
    assert report.model_dump(mode="json")["harvest"]["documents_planned"] == 1.0


def test_a_no_llm_run_reports_no_harvest_coverage_rather_than_zeros() -> None:
    """Zeros would read as a stage that ran and found nothing. `--no-llm` ran no stage at all."""
    from seqforge.evals import run_cases

    report, _ = run_cases(
        [next(c for c in HERMETIC_CASES if c.id == "10x-v3-bytes-only")], llm=False
    )
    assert report.harvest is None
    assert report.model_dump(mode="json")["harvest"] is None


def test_harvest_status_names_the_three_states_and_nothing_between() -> None:
    """`complete` | `partial` | `unmeasured`, decided from the counts and never set by a caller."""
    assert HarvestGrade(n_documents=3).status == "complete"
    assert HarvestGrade(n_documents=3, n_documents_failed=1).status == "partial"
    assert HarvestGrade(n_documents=3, n_documents_failed=3).status == "unmeasured"
    # A case that sent nothing measured nothing; reporting `complete` would be the whole bug again.
    assert HarvestGrade().status == "unmeasured"


def test_across_trials_one_trial_reaching_the_document_settles_it() -> None:
    """`unchecked` intersects while `missing` unions, and the pair has to stay consistent.

    A field one trial could not reach and another read has a real verdict; a field matched in one
    trial and missed in another is `unstable`, which is a finding rather than a gap.
    """
    reached = HarvestGrade(matched=["experiment.organism"], n_documents=2)
    blind = HarvestGrade(unchecked=["experiment.organism"], n_documents=2, n_documents_failed=2)
    merged = _merge_harvest([blind, reached])

    assert merged.unchecked == [], "one trial read the document, so this is not a gap"
    assert merged.matched == ["experiment.organism"]
    assert merged.n_documents_failed == 2, "each failed attempt was paid for separately"
    assert merged.status == "partial"

    both_blind = _merge_harvest([blind, blind])
    assert both_blind.unchecked == ["experiment.organism"]
    assert both_blind.status == "unmeasured"


def test_a_field_matched_in_one_trial_and_missed_in_another_is_still_unstable() -> None:
    """The pre-existing merge rule, pinned while the rule that computes it is rewritten.

    `matched` used to be a plain intersection. It is now "some trial got it and none missed it",
    which is the same set whenever every trial could check every field — and this is that case.
    """
    got = HarvestGrade(matched=["experiment.organism"], n_documents=1)
    missed = HarvestGrade(missing=["experiment.organism"], n_documents=1)
    merged = _merge_harvest([got, missed])

    assert merged.matched == []
    assert merged.missing == ["experiment.organism"]
    assert merged.unstable == ["experiment.organism"]


class _FlakyProvider:
    """Returns a different payload per call — a stand-in for real extraction nondeterminism."""

    name = "flaky"

    def __init__(self, payloads: list[list[dict[str, object]]]) -> None:
        self._payloads = payloads
        self.calls = 0

    def default_model(self) -> str:
        return "flaky-1"

    def complete_json(self, **kwargs: object) -> LLMResponse:
        import json as _json

        from seqforge.harvest import LLMResponse

        payload = self._payloads[min(self.calls, len(self._payloads) - 1)]
        self.calls += 1
        return LLMResponse(text=_json.dumps({"drafts": payload}), usage={})


def test_stability_is_not_contaminated_by_folding_the_harvest_grade() -> None:
    """The 0.667-from-three-identical-trials bug, end to end.

    Every trial hallucinates, so every trial is a false_accept and stability is honestly 0.0. The bug
    this pins produced a *fractional* stability by mutating one element of the trials list after the
    fact — a number that looked like real nondeterminism and was pure aliasing.
    """
    bad = [
        _draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans"),
        # The real 2026.7.1 regression, in the vocabulary that replaced ours: DeepSeek filed
        # standard worm husbandry as an experimental condition. `condition` was our invention and is
        # gone; NCBI's `treatment` is the field that inherits the trap, and it inherits NCBI's
        # definition with it, which is the point.
        _draft(
            "experiment.samples.treatment", "maintained on NGM plates", "maintained on NGM plates"
        ),
    ]
    run = run_case(_trap_case(), llm=True, provider=_StubProvider(bad), trials=3)
    assert run.harvest is not None
    assert run.harvest.hallucinated == ["experiment.samples.treatment"]
    assert run.grade.grade is Grade.FALSE_ACCEPT
    assert run.stability == 0.0, (
        "all three trials failed identically; stability is 0, not a fraction"
    )


def test_stability_is_one_when_every_trial_is_clean_and_nothing_folds() -> None:
    """Three clean trials: stability 1.0 — and the per-trial wiring that produced it.

    The wiring half was a separate test making the IDENTICAL call (same case, same `_StubProvider`,
    same `trials=3`) to assert a disjoint set of attributes off the same 0.5s run.
    """
    good = [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    run = run_case(_trap_case(), llm=True, provider=_StubProvider(good), trials=3)
    assert run.grade.grade is Grade.CORRECT
    assert run.stability == 1.0
    # every trial really ran, and each one's cost was counted
    assert run.trials == 3
    assert run.llm_calls == 3
    assert run.usage["input_tokens"] == 300


def test_stability_is_fractional_only_when_trials_genuinely_differ() -> None:
    """A real 2-in-3 failure — the case this metric exists to report — and the failure SURVIVING.

    Trials used to keep only the LAST harvest, so a hallucination on trial 1 vanished when trials 2-3
    came back clean: precisely the illusion trials exist to dispel. That regression had its own test
    on the same `_FlakyProvider([bad, good, good])` call, and it could only prove the point
    CONDITIONALLY, because its bad draft ("heat shock", quoted from "single-cell RNA-seq") might die
    at span-verification before ever reaching the fold. This one's bad draft quotes itself, so it
    survives verify deterministically — one call, and the fact is unconditional.
    """
    good = [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    # `condition` is no longer an assertable field, so a draft naming it now dies at the allowlist
    # and never reaches the fold this test is about. The trap moves to NCBI's `treatment`, which is
    # what `condition` was trying to be and is what the case's forbidden_fields now names.
    bad = good + [
        _draft(
            "experiment.samples.treatment", "maintained on NGM plates", "maintained on NGM plates"
        )
    ]
    provider = _FlakyProvider([bad, good, good])
    run = run_case(_trap_case(), llm=True, provider=provider, trials=3)
    assert provider.calls == 3
    assert run.grade.grade is Grade.FALSE_ACCEPT, "one bad trial condemns the case"
    assert run.stability == pytest.approx(2 / 3), "but stability reports it happened 1 time in 3"
    # the trial-1 failure reached the grade rather than being forgotten because trial 3 was clean
    assert run.harvest is not None
    assert run.harvest.hallucinated == ["experiment.samples.treatment"]


def test_a_field_found_in_only_some_trials_is_reported_unstable() -> None:
    """Extraction that comes and goes is a finding, not a rounding error.

    Against `_merge_harvest` directly, because that is the seam that owns the claim: it is a pure
    function over a list of `HarvestGrade`s, and reaching it through a full materialize + three
    harvest/resolve/grade passes cost 0.55s to assert the same thing. This file already uses exactly
    this seam for the sibling `_fold_harvest`. The per-trial WIRING — that three trials run and each
    one's harvest reaches the merge — is proved at both ends by
    `test_stability_is_one_when_every_trial_is_clean_and_nothing_folds` and
    `test_stability_is_fractional_only_when_trials_genuinely_differ`.
    """
    found = HarvestGrade(matched=["experiment.organism"])
    missed = HarvestGrade(missing=["experiment.organism"])
    merged = _merge_harvest([found, missed, found])
    assert merged.matched == [], "a field missed in any trial must not count as matched"
    assert merged.unstable == ["experiment.organism"]
    assert merged.missing == ["experiment.organism"]


def _assertion(field_name: str, value: str, quote: str, sha: str = "d" * 64) -> Assertion:
    return Assertion(
        id=f"assert-{sha[:8]}-0",
        field=field_name,
        value=value,
        span=SourceSpan(doc_sha256=sha, quote=quote, char_start=0, char_end=len(quote)),
        span_verified=True,
        entailment_ok=True,
        llm_confidence=0.9,
        extractor=ExtractorProvenance(model_id="stub/stub-model-1", prompt_version="2026.7.4"),
    )


def test_merging_trials_keeps_every_distinct_claim_and_every_refusal() -> None:
    """The claim merge follows the same rule the field merge does, and the old one did not.

    `extracted.update(...)` let the last trial win, so a claim only one trial made vanished — the
    exact fact `unstable` exists to report, deleted from the evidence that would let a reader see
    it. Distinct claims union; the same claim made twice is one claim, keyed by what the claim IS
    (field, value, quote, document) rather than by an `id` that carries a draft's position in a
    batch. Refusals concatenate instead: a draft refused in every trial cost three refusals, which
    is what the count has always said.
    """
    a = _assertion("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")
    b = _assertion("library.chemistry", "10x-3p-gex-v3", "Chromium Single Cell 3' v3")
    docs = [{"doc_sha256": "d" * 64, "source": "methods.txt", "scope": "dataset", "n_chars": 40}]
    one = HarvestGrade(assertions=[a, b], rejected=[{"reason": "not_entailed"}], documents=docs)
    two = HarvestGrade(assertions=[a], rejected=[{"reason": "not_entailed"}], documents=docs)

    merged = _merge_harvest([one, two, two])
    assert [x.field for x in merged.assertions] == ["experiment.organism", "library.chemistry"]
    assert merged.n_rejected == 3, "a refusal repeated in every trial was paid for three times"
    assert merged.documents == docs, (
        "the plan is a function of the case, so it is one list not three"
    )


def test_usage_is_accumulated_into_the_report() -> None:
    case = _trap_case()
    provider = _StubProvider(
        [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    )
    run = run_case(case, llm=True, provider=provider)
    report = build_report([run])
    assert report.cost["input_tokens"] == 100.0
    assert report.cost["llm_calls"] == 1.0


# --------------------------------------------------------------------------------------------
# the `fingerprint` eval kind: a real dataset run from its byte-light package, hermetically
#
# The benchmark's whole premise. A fingerprint package (head-sliced FASTQs + a pin that carries the
# whole-file identity) resolves the chemistry from the slice, reproduces the full dataset's identity
# from the pin, and grades sample attributes from a committed records.json — no full FASTQ, no
# network, no API key. These pin that the kind materializes, resolves, grades, and skips correctly.
# bulk-rnaseq-pe is used because it is decided by STRUCTURE alone (no onlist lookup), so the fixture
# is hermetic regardless of which whitelists happen to be cached.
# --------------------------------------------------------------------------------------------


def _bulk_fingerprint(tmp_path: Path) -> tuple[Path, str]:
    """A tiny real fingerprint package of synthetic bulk PE reads, plus a matching records.json."""
    import gzip
    import json

    from seqforge import kb
    from seqforge.fingerprint.build import build_fingerprint

    # n=600 with reads=400 below, not n=1500/reads=2000. bulk-rnaseq-pe is decided by STRUCTURE
    # alone and the chemistry call is N-invariant, so the smaller N grades identically. It
    # also makes the docstring true: at n=1500 with reads=2000 the "slice" was the WHOLE file, so
    # "no full FASTQ is present (only the slice)" was not being proved by anything.
    spec = kb.load_spec("bulk-rnaseq-pe")
    reads = kb.generate_reads(spec, n=600, seed=0)
    src = tmp_path / "SRR12345678"
    src.mkdir(parents=True)
    paths = []
    for rid, seqs in reads.items():
        # A real deposit's names, because that is what a real package holds: the run accession the
        # archive assigned plus a mate token, which is how `group_runs` learns these two files are
        # one run and how the record join below reaches them.
        p = src / f"SRR12345678_{rid}.fastq.gz"
        with gzip.open(p, "wt") as fh:
            for i, s in enumerate(seqs):
                fh.write(f"@r{i}\n{s}\n+\n{'I' * len(s)}\n")
        paths.append(p)
    result = build_fingerprint(paths, workspace=tmp_path / "build", reads=400, name="bulkfp")
    records = {
        "source": "test",
        "query": "TEST",
        "records": [
            {
                "level": "run",
                "accession": "SRR12345678",
                "parent": "SRX12345678",
                "filenames": [p.name for p in paths],
            },
            {"level": "experiment", "accession": "SRX12345678", "parent": "SAMN12345678"},
            {
                "level": "sample",
                "accession": "SAMN12345678",
                "attributes": [{"name": "strain", "value": "CB4856", "harmonized": True}],
            },
        ],
    }
    return result.package, json.dumps(records)


def _fingerprint_case_dir(tmp_path: Path, recipe_yaml: str, records_json: str | None) -> Path:
    case_dir = tmp_path / "case"
    (case_dir / "inputs").mkdir(parents=True)
    (case_dir / "inputs" / "recipe.yaml").write_text(recipe_yaml)
    (case_dir / "expected.yaml").write_text(
        "outcome: decide\n"
        "description: a real bulk dataset resolved from its fingerprint package, samples from records\n"
        "fields:\n"
        "  library.chemistry: bulk-rnaseq-pe\n"
        "  experiment.samples.*.strain: [CB4856]\n"
        "  experiment.samples.SAMN12345678.strain: CB4856\n"
    )
    if records_json is not None:
        (case_dir / "records.json").write_text(records_json)
    return case_dir


def test_a_fingerprint_case_resolves_from_the_package_and_grades_samples_from_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kind end to end: pinned bytes decide the chemistry, records decide the sample attributes.

    No full FASTQ is present (only the slice), no onlist is fetched, no LLM runs. That this grades
    CORRECT is the benchmark's core promise — a real dataset checked in CI from a byte-light artifact.
    """
    from seqforge.evals.case import load_case

    package, records_json = _bulk_fingerprint(tmp_path)
    case_dir = _fingerprint_case_dir(
        tmp_path, "generate:\n  kind: fingerprint\n  root_env: SEQFORGE_TEST_FP\n", records_json
    )
    monkeypatch.setenv("SEQFORGE_TEST_FP", str(package))
    run = run_case(load_case(case_dir), llm=False)
    assert run.skipped is None
    assert run.grade.grade is Grade.CORRECT, run.grade.notes
    assert run.llm_calls == 0, "a fingerprint case grades hermetically, with no LLM call"


def test_a_fingerprint_case_skips_when_its_root_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An out-of-git package that is not on this machine skips — never a pass, never a fail."""
    from seqforge.evals.case import load_case

    monkeypatch.delenv("SEQFORGE_TEST_FP", raising=False)
    case_dir = _fingerprint_case_dir(
        tmp_path, "generate:\n  kind: fingerprint\n  root_env: SEQFORGE_TEST_FP\n", None
    )
    run = run_case(load_case(case_dir), llm=False)
    assert run.skipped is not None
    assert "SEQFORGE_TEST_FP" in run.skipped
    assert build_report([run]).n_cases == 0


def _benchmark_tier_or_skip() -> Path:
    """The networked tier's root, or skip. Two tests need it, and a stray copy of the skip is how
    one of them quietly stops covering anything when the directory moves."""
    bench = default_cases_dir().parent / "benchmark"
    if not bench.is_dir():
        pytest.skip("no HF benchmark tier committed")
    return bench


def test_the_hf_benchmark_tier_is_well_formed_and_separate_from_the_hermetic_corpus() -> None:
    """`evals/benchmark` (the networked HF tier) loads offline and never leaks into hermetic CI.

    Loading a case does NOT fetch (materialize does), so this validates the whole tier — every case is
    an `hf:`-sourced fingerprint case with a committed records.json — with no network. And it pins the
    separation: `default_cases_dir()` (what `test_corpus_is_green` runs) must not contain the benchmark
    tier, or a package pull would sneak into per-commit CI.
    """
    from seqforge.evals.case import FingerprintRecipe

    bench = _benchmark_tier_or_skip()
    cases = discover_cases(bench)
    assert cases, "the benchmark tier is present but empty"
    for c in cases:
        gen = c.recipe.generate
        assert isinstance(gen, FingerprintRecipe) and gen.hf, (
            f"{c.id}: benchmark cases pull from hf"
        )
        assert c.records is not None, f"{c.id}: a benchmark case commits its records.json"
        assert c.expected.fields.get("library.chemistry"), f"{c.id}: pins a resolved chemistry"
    # The two tiers are disjoint directories — the hermetic corpus never sees the networked one. The
    # check is by PATH, not id: a dataset may legitimately appear in both tiers (PRJNA1027859 is the
    # pilot's full-pipeline local case AND a fingerprint benchmark case), but no benchmark case
    # directory is ever one `discover_cases()` walks, so none can be pulled in per-commit CI.
    assert bench.resolve() != default_cases_dir().resolve()
    hermetic_roots = {c.root.resolve() for c in discover_cases()}
    assert all(c.root.resolve() not in hermetic_roots for c in cases)


def test_a_benchmark_case_declares_exactly_the_samples_it_grades() -> None:
    """A benchmark case's `records.json` may not carry a sample the case does not grade.

    The transcript is fetched for a whole accession; the package pins a chosen subset of its runs. So
    the two drift, and the drift is expensive rather than wrong: every record with prose is one
    extraction call, and a sample no slice can join to is billed and then discarded unread. GSE126954
    declared the series' eighth sample, whose 910 run records were 92% of that case's LLM spend and
    83% of the whole benchmark's, for claims the resolver never reached.

    This is the offline half of the invariant, and it is a proxy: the expectation's
    `experiment.samples.*` lists are one entry per graded sample, so their length is what the
    transcript's sample count must equal. It needs no package and therefore runs everywhere. The
    networked half below asserts the real property against the bytes.
    """
    bench = _benchmark_tier_or_skip()
    for case in discover_cases(bench):
        assert case.records is not None, f"{case.id}: a benchmark case commits its records.json"
        declared = {r.accession for r in case.records.at("sample")}
        graded = {
            len(v)
            for k, v in case.expected.fields.items()
            if k.startswith("experiment.samples.*") and isinstance(v, list)
        }
        assert len(graded) <= 1, (
            f"{case.id}: the `*` lists disagree on how many samples this case grades: {graded}"
        )
        if graded:
            assert len(declared) == graded.pop(), (
                f"{case.id}: records.json declares {len(declared)} sample(s) but the expectation "
                f"grades a different number — narrow the transcript to what the package pins"
            )
        # A per-accession pin naming a sample the transcript does not carry can never be graded.
        for key in case.expected.fields:
            parts = key.split(".")
            if key.startswith("experiment.samples.") and parts[2] != "*":
                assert parts[2] in declared, f"{case.id}: {key} names an undeclared sample"


@pytest.mark.skipif(
    not os.environ.get("SEQFORGE_LIVE_NET"),
    reason="needs the HF packages; set SEQFORGE_LIVE_NET=1 to check transcripts against real bytes",
)
def test_a_benchmark_transcript_declares_exactly_what_its_package_pins() -> None:
    """The real invariant, against the bytes: every sample the transcript declares is one the package ships.

    `materialize` drops the records no slice can reach, so a fixture that has drifted still runs
    correctly — which is exactly why the drift would otherwise stay invisible. Here the narrowing is
    applied to the committed file and no *sample* may disappear: one that does is a sample the case
    can never grade, and every record under it is an extraction call spent on nothing.

    The grain is the sample, not the record, and that is deliberate. A package legitimately pins a
    subset of a sample's runs — GSE229022 ships one lane of a two-lane sample — so a run record with
    no slice is thrift, not drift, and the narrowing quietly saves its call. A *sample* with no slice
    is the GSE126954 defect: 910 runs, 92% of the case's spend, joined to nothing.

    Opt-in, because it pulls every package from Hugging Face. A package that will not download skips
    that case rather than failing it, the same contract the harness keeps.
    """
    from seqforge.evals.case import CaseSkipped, _records_the_package_reaches, materialize

    bench = _benchmark_tier_or_skip()
    checked = 0
    for case in discover_cases(bench):
        assert case.records is not None
        with tempfile.TemporaryDirectory(prefix="seqforge-invariant-") as tmp:
            try:
                built = materialize(case, Path(tmp) / "inputs")
            except CaseSkipped:
                continue
            narrowed = _records_the_package_reaches(case.records, built.paths)
        dropped = {r.accession for r in case.records.at("sample")} - {
            r.accession for r in narrowed.at("sample")
        }
        assert not dropped, (
            f"{case.id}: records.json declares sample(s) {sorted(dropped)} that no slice in the "
            f"package reaches — every record under them is an extraction call that grades nothing"
        )
        checked += 1
    if not checked:
        pytest.skip("no benchmark package could be fetched")


def test_the_benchmark_dataset_table_covers_every_case_and_agrees_with_it() -> None:
    """`evals/benchmark-datasets.tsv` is one row per benchmark case, and it cannot drift.

    The table exists because a directory of accessions does not tell a reader what the corpus
    *covers* — which is the question you ask before adding a dataset. Its `uniqueness` column is
    prose and must stay hand-written; but a hand-written index rots the moment a case is added and
    nothing notices, so the mechanical columns are checked against the case files themselves and the
    row set is checked for exact correspondence. Add a case without a row (or vice versa) and this
    turns red, which is the only reason the table is worth keeping.
    """
    bench = _benchmark_tier_or_skip()
    table = bench.parent / "benchmark-datasets.tsv"
    assert table.is_file(), f"the benchmark tier is committed but {table.name} is not"

    lines = [ln for ln in table.read_text().splitlines() if ln.strip()]
    header, *body = (ln.split("\t") for ln in lines)
    expected_columns = [
        "case_id",
        "accession",
        "organism",
        "chemistry",
        "outcome",
        "provenance",
        "uniqueness",
    ]
    assert header == expected_columns, f"unexpected columns: {header}"
    rows = {r[0]: dict(zip(header, r, strict=True)) for r in body}
    assert len(rows) == len(body), "a case id appears twice"

    cases = {c.id: c for c in discover_cases(bench)}
    assert rows.keys() == cases.keys(), (
        f"table and tier disagree: only in table {rows.keys() - cases.keys()}, "
        f"only in tier {cases.keys() - rows.keys()}"
    )
    for case_id, case in cases.items():
        row = rows[case_id]
        assert row["chemistry"] == case.expected.fields.get("library.chemistry"), case_id
        assert row["outcome"] == case.expected.outcome, case_id
        organism = case.expected.fields.get("experiment.organism")
        assert row["organism"] == (str(organism) if organism is not None else "-"), case_id
        # The rest is prose — including `accession`, which names the SERIES a reader would search
        # for, while a case may be built from one run of it. Nothing can check prose is *right*, so
        # check it was written: a row whose uniqueness is blank is a dataset nobody could justify.
        for column in ("accession", "provenance", "uniqueness"):
            assert row[column].strip(), f"{case_id}: {column} is empty"


def test_a_fingerprint_recipe_needs_exactly_one_source() -> None:
    """`path` XOR `root_env`: naming both (or neither) is a case error, not a silent default."""
    for gen in ({}, {"path": "p.tar.gz", "root_env": "X"}):
        with pytest.raises(ValidationError, match="exactly one"):
            Recipe.model_validate({"generate": {"kind": "fingerprint", **gen}})


# --------------------------------------------------------------------------------------------------
# `seqforge eval plan` — what an --llm pass over a TIER costs, before any of it is paid
#
# `harvest extract --dry-run` priced one dataset. The decision it informs is taken over a corpus, and
# was answerable only by spending one. These pin the two things that make the number worth trusting:
# it is the send list the paid run really uses, and a case it could not price says so instead of
# reporting zero.
# --------------------------------------------------------------------------------------------------


def _priceable_case(tmp_path: Path, *, n_runs: int = 3) -> Case:
    """A case with prose beside it AND a records transcript — the two halves of a real bill."""
    from seqforge.evals.case import load_case

    case_dir = tmp_path / "priceable"
    (case_dir / "inputs").mkdir(parents=True)
    (case_dir / "inputs" / "recipe.yaml").write_text(
        "generate:\n  kind: random\n  n: 4\n  min_len: 40\n  max_len: 60\n"
    )
    (case_dir / "expected.yaml").write_text(
        "outcome: refuse\ndescription: prose plus a transcript, so a plan has both halves to price\n"
        "blockers: [UNSUPPORTED_TECHNOLOGY]\n"
    )
    (case_dir / "metadata").mkdir()
    (case_dir / "metadata" / "methods.txt").write_text(
        "We profiled Caenorhabditis elegans by droplet-based single-cell RNA sequencing.\n"
    )
    (case_dir / "records.json").write_text(
        json.dumps(
            {
                "source": "test",
                "query": "TEST",
                "records": [
                    {
                        "level": "sample",
                        "accession": "SAMN1",
                        "free_text": [{"label": "title", "text": "adult hermaphrodite neurons"}],
                    },
                    *(
                        {
                            "level": "run",
                            "accession": f"SRR{i}",
                            "parent": "SAMN1",
                            "free_text": [{"label": "alias", "text": f"N2_wild_type_rep{i}"}],
                        }
                        for i in range(n_runs)
                    ),
                ],
            }
        )
    )
    return load_case(case_dir)


def test_the_tier_plan_is_the_send_list_the_paid_run_would_use(tmp_path: Path) -> None:
    """The plan and the run must not be able to drift, so this checks one against the other.

    A cost estimate nobody can join back to the run it predicts is decoration. Both sides are
    computed here — the plan with no provider at all, the run against a stub that answers every
    request — and both counts have to agree.

    The two counts are separate because they stopped being the same number: documents that receive
    the same ask travel in one request, so `n_documents` is how much material is read and
    `n_requests` is how many times a model is reached. It is the second that a run's call count must
    be joined to — the plan is the floor, and a retry is the only thing allowed to push the real run
    above it. Asserting the run against the DOCUMENT count would silently re-pin one-call-per-
    document, which is the thing batching removed.
    """
    from seqforge.evals import plan_case, system_prompt_chars

    case = _priceable_case(tmp_path)
    row = plan_case(case, prompt_chars=system_prompt_chars())

    # One human document + one sample record + the sample's three runs, collapsed into one document.
    assert row.n_documents == 3
    # The sample and run documents share the same ask and travel together; the human document's
    # dataset-scope ask is different, so it cannot join them.
    assert row.n_requests == 2
    assert row.n_records_read == 4
    assert row.n_records_collapsed == 2
    assert row.n_chars > 0
    assert row.estimated_input_tokens > row.n_chars // 4, "the system prefix is charged per request"
    assert row.skipped is None

    run = run_case(case, llm=True, provider=_StubProvider([]))
    assert run.harvest is not None
    assert run.harvest.n_documents == row.n_documents
    assert run.llm_calls == row.n_requests, "the plan is the floor on what the run issues"


def test_a_case_the_plan_cannot_price_is_named_rather_than_costed_at_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skip is a case whose price is unknown, not a case that is free.

    Reporting zero tokens for an unreachable package would understate the tier by exactly the cases a
    maintainer most needs to know about, and it would understate it silently. So the row carries the
    reason and the kind, and the totals exclude it — the same contract `eval run` keeps for a skip.
    """
    from seqforge.evals import plan_cases
    from seqforge.evals.case import load_case

    monkeypatch.delenv("SEQFORGE_TEST_FP", raising=False)
    case_dir = _fingerprint_case_dir(
        tmp_path, "generate:\n  kind: fingerprint\n  root_env: SEQFORGE_TEST_FP\n", None
    )
    report = plan_cases([load_case(case_dir), _priceable_case(tmp_path)], jobs=1)

    assert report.n_cases == 1, (
        "a skipped case is excluded from the totals, as it is from every rate"
    )
    assert report.n_skipped == 1
    assert report.n_reaching_a_model == 1
    skipped = next(r for r in report.per_case if r.skipped is not None)
    assert "SEQFORGE_TEST_FP" in (skipped.skipped or "")
    assert skipped.skip_kind == "unavailable"
    assert skipped.estimated_input_tokens == 0


def test_the_plan_prices_the_trials_it_is_asked_about_and_names_what_will_breach(
    tmp_path: Path,
) -> None:
    """`--trials N` really does send the same list N times, so a plan that ignored it would lie.

    The ceiling check is deliberately one-sided and named as such: it lists the cases whose estimated
    INPUT alone already clears the bar. Output and cache-write tokens count against a Ceiling too and
    neither is knowable before the model answers, so this can only ever be a lower bound.
    """
    from seqforge.evals import plan_cases

    case = _priceable_case(tmp_path)
    once = plan_cases([case], jobs=1)
    thrice = plan_cases([case], trials=3, jobs=1)

    assert thrice.trials == 3
    assert thrice.estimated_input_tokens == once.estimated_input_tokens * 3
    assert thrice.n_documents == once.n_documents * 3
    # Per-case rows stay per trial: a trial is the unit that repeats, so multiplying them too would
    # report the same tokens twice to anyone adding the column up.
    assert thrice.per_case[0].estimated_input_tokens == once.per_case[0].estimated_input_tokens

    assert once.estimated_over_ceiling == []
    tight = plan_cases([case], ceiling=1, jobs=1)
    assert tight.ceiling == 1
    assert tight.estimated_over_ceiling == [case.id]


def test_eval_plan_is_a_verb_that_prices_a_tier_and_needs_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point is that it answers on a machine with no key, so the key is removed here.

    `--dry-run` on `harvest extract` returns before a provider is even resolved; this is the tier-wide
    half of that promise, and a verb that quietly needed a credential would be useless for the one
    decision it exists to inform.
    """
    from typer.testing import CliRunner

    from seqforge.cli import app

    for key in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    _priceable_case(tmp_path)

    result = CliRunner().invoke(app, ["eval", "plan", "--cases", str(tmp_path), "-j", "1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["n_cases"] == 1
    assert payload["per_case"][0]["n_documents"] == 3
    assert payload["estimated_input_tokens"] > 0
    assert payload["system_prompt_chars"] > 0

    empty = CliRunner().invoke(app, ["eval", "plan", "--cases", str(tmp_path / "nothing")])
    assert empty.exit_code == 2


# --------------------------------------------------------------------------------------------------
# `seqforge eval report` — the HTML renderer
#
# The renderer exists because the benchmark job produced a number and threw the detail away: a green
# exit code told you nothing about WHICH case carried the risk. So these tests are less about markup
# than about the claims the page is allowed to make. Breaking one of them means the report has started
# lying in the specific way a grading dashboard usually lies.
# --------------------------------------------------------------------------------------------------


#: The sha256 of the one document the fixture's exchanges are about. A real one is a real sha; what
#: matters here is that the SAME string appears in the document list, in a claim's span, on a
#: refusal, and on the "Document sha256:" line of an exchange — that chain is what the page joins on.
_FIXTURE_DOC = "a1" * 32
_FIXTURE_SAMPLE_DOC = "b2" * 32


def _fixture_exchange(sha: str, why: str, **over: Any) -> dict[str, Any]:
    """One exchange as `attach_transcripts` writes it into a case row."""
    row: dict[str, Any] = {
        "doc_sha256": sha,
        "why": why,
        "prompt_sha256": "c3" * 32,
        "model": "deepseek-v4-flash",
        "user": f"Document sha256: {sha}\nEcho that exact string as span.doc_sha256.\n\n"
        "Fields to look for:\n- experiment.organism\n\n<document>\n"
        # Long enough to trip the clip, and carrying the characters that would end the page early if
        # anything here reached the browser as markup rather than as text.
        + ("Worms were grown at 20 C <b>& harvested</b> </script> alert(1). " * 30)
        + "\n</document>",
        "text": '{"drafts": [{"field": "experiment.organism", "value": "Caenorhabditis elegans"}]}',
        "usage": {"input_tokens": 4102, "output_tokens": 210},
    }
    row.update(over)
    return row


#: Every branch the renderer has, in one report: a false accept with a wrong scalar AND a wrong
#: multiset, each of the other four failure grades, a correct case, a skip, harvest (including a
#: hallucination, the claims it graded, and the drafts it refused), a sampled transcript with the
#: system prompt beside it, multiple trials, and both clocks. Built fresh per call because tests
#: mutate it.
def _render_fixture() -> dict[str, Any]:
    return {
        "n_cases": 6,
        "prompts": [
            {
                "sha256": "c3" * 32,
                "text": "You extract factual claims from a scientific methods document.\n"
                "Rules:\n1. Extract ONLY what the document explicitly states.\n",
                "n_exchanges": 412,
            }
        ],
        "extractor": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "prompt_version": "2026.7.2",
        },
        "field_accuracy": 0.5,
        "false_accept_rate": 1 / 6,
        "false_refuse_rate": 1 / 6,
        "questions_asked": {"total": 2.0, "per_case": 0.33, "missed": 1.0},
        "cost": {
            "seconds": 412.7,
            "wall_seconds": 61.3,
            "llm_calls": 9.0,
            "input_tokens": 184213.0,
            "output_tokens": 4210.0,
        },
        "per_case": [
            {
                "case": "green-one",
                "seconds": 7.2,
                "llm_calls": 0,
                "grade": "correct",
                "expected": "decide",
                "actual": "decide",
                "fields": [
                    {"path": "library.chemistry", "expected": "a", "actual": "a", "ok": True}
                ],
                "notes": [],
                "missed_question": False,
                "trials": 1,
                "stability": 1.0,
            },
            {
                "case": "poisoned-one",
                "seconds": 21.9,
                "llm_calls": 3,
                "grade": "false_accept",
                "expected": "refuse",
                "actual": "decide",
                "fields": [
                    {
                        "path": "library.chemistry",
                        "expected": "10x-3p-gex-v3",
                        "actual": "bulk-rnaseq-pe",
                        "ok": False,
                    },
                    {
                        "path": "experiment.samples.*.strain",
                        "expected": ["N2", "N2", "VC2010, derived from N2"],
                        "actual": ["N2", "N2", "N2"],
                        "ok": False,
                    },
                ],
                "notes": ["decided where it should have stopped"],
                "missed_question": True,
                "trials": 3,
                "stability": 0.333,
                "usage": {"input_tokens": 61000, "output_tokens": 1400},
                "harvest": {
                    "matched": ["experiment.organism"],
                    "missing": ["experiment.study.title"],
                    "hallucinated": ["experiment.samples.tissue"],
                    "unstable": ["experiment.samples.dev_stage"],
                    "n_rejected": 4,
                    "n_calls": 412,
                    "n_documents": 73,
                    "mode": {"max_tokens": 8000, "response_format": "json_object"},
                    "documents": [
                        {
                            "doc_sha256": _FIXTURE_DOC,
                            "source": "methods.pdf",
                            "scope": "dataset",
                            "subject": None,
                            "n_chars": 41233,
                        },
                        {
                            "doc_sha256": _FIXTURE_SAMPLE_DOC,
                            "source": "SAMN14126930.txt",
                            "scope": "sample",
                            "subject": "SAMN14126930",
                            "n_chars": 812,
                        },
                    ],
                    # The claim, WITH the quote it rests on and the span that locates it — the whole
                    # point of the grade carrying Assertions rather than `field -> str(value)`.
                    "assertions": [
                        {
                            "id": "assert-a1a1a1a1-0",
                            "field": "experiment.organism",
                            "value": "Caenorhabditis elegans",
                            "span": {
                                "doc_sha256": _FIXTURE_DOC,
                                "quote": "worms (Caenorhabditis elegans) were grown at 20 C",
                                "context": None,
                                "char_start": 4120,
                                "char_end": 4168,
                                "page": 3,
                            },
                            "span_verified": True,
                            "entailment_ok": True,
                            "llm_confidence": 0.94,
                            "extractor": {
                                "model_id": "deepseek/deepseek-v4-flash",
                                "prompt_version": "2026.7.2",
                            },
                        }
                    ],
                    # Both producers, so the page has to render a malformed draft (no field, no
                    # value, no quote) beside a verify refusal that has all three.
                    "rejected": [
                        {
                            "doc_sha256": _FIXTURE_DOC,
                            "field": "library.chemistry",
                            "value": "10x-3p-gex-v3",
                            "quote": "droplet-based single-cell",
                            "reason": "not_entailed",
                            "detail": "quote does not support value '10x-3p-gex-v3'",
                        },
                        {
                            "doc_sha256": _FIXTURE_DOC,
                            "field": "experiment.samples.tissue",
                            "value": "L4 larva",
                            "quote": "synchronised L4 larvae",
                            "reason": "field_not_permitted_for_doc",
                            "detail": "'experiment.samples.tissue' may not be set by 'methods.pdf'",
                        },
                        {
                            "doc_sha256": _FIXTURE_SAMPLE_DOC,
                            "field": None,
                            "value": None,
                            "quote": None,
                            "reason": "malformed_draft",
                            "detail": "draft failed AssertionDraft validation: Input should be a "
                            "valid string",
                        },
                        {
                            "doc_sha256": _FIXTURE_SAMPLE_DOC,
                            "field": "processing.genome.assembly",
                            "value": "ce11",
                            "quote": "mapped to the worm genome",
                            "reason": "not_entailed",
                            "detail": "quote does not support value 'ce11'",
                        },
                    ],
                },
                # What `attach_transcripts` folds in from `transcripts/<case>.jsonl`: a SAMPLE, and
                # the total it was drawn from, so the page can say what it left out.
                "transcript": "transcripts/poisoned-one.jsonl",
                "n_exchanges": 412,
                "exchanges": [
                    _fixture_exchange(_FIXTURE_DOC, "produced a graded assertion"),
                    _fixture_exchange(
                        _FIXTURE_SAMPLE_DOC,
                        "the request failed",
                        text="",
                        error="deepseek: 429 rate limited",
                        usage={"input_tokens": 3980, "output_tokens": 0},
                    ),
                ],
            },
            {
                "case": "triaged-wrong",
                "seconds": 3.0,
                "llm_calls": 0,
                "grade": "mis_triage",
                "expected": "refuse",
                "actual": "ask",
                "fields": [],
                "notes": ["asked instead of blocking"],
                "missed_question": False,
                "trials": 1,
                "stability": 0.0,
            },
            {
                "case": "blocked-wrongly",
                "seconds": 1.5,
                "llm_calls": 0,
                "grade": "false_refuse",
                "expected": "decide",
                "actual": "refuse",
                "fields": [],
                "notes": ["blocked: ['TRUNCATED_GZIP']"],
                "missed_question": False,
                "trials": 1,
                "stability": 0.0,
            },
            {
                "case": "right-answer-wrong-reason",
                "seconds": 2.5,
                "llm_calls": 0,
                "grade": "wrong_reason",
                "expected": "refuse",
                "actual": "refuse",
                "fields": [],
                "notes": ["expected blocker(s) ['MISSING_TECHNICAL_READ']"],
                "missed_question": False,
                "trials": 1,
                "stability": 0.0,
            },
            {
                "case": "asked-too-much",
                "seconds": 57.3,
                "llm_calls": 0,
                "grade": "over_ask",
                "expected": "decide",
                "actual": "ask",
                "fields": [],
                "notes": ["asked a question code should have settled"],
                "missed_question": False,
                "trials": 1,
                "stability": 0.0,
            },
            {
                "case": "offline-one",
                "seconds": 0.19,
                "llm_calls": 0,
                "skipped": "package unreachable",
            },
            # The other skip, and the reason `skip_kind` exists: the archive answered, and it does
            # not hold this package. Nothing about the wire went wrong; the corpus has a hole.
            {
                "case": "never-published-one",
                "seconds": 0.11,
                "llm_calls": 0,
                "skipped": "benchmark package 'packages/GSE110823.fingerprint.tar.gz' was never "
                "published to liuhlab/seqforge-benchmark",
                "skip_kind": "absent",
            },
        ],
    }


def test_the_html_renderer_shows_a_false_accept_rather_than_averaging_it_away() -> None:
    """The one thing the page must never do is what a dashboard usually does.

    `evals/README.md` is explicit that a false accept has no tolerable rate — `eval run` exits 3 on
    any, deliberately not on a `--fail-under` slider. So the page has to STATE it, in words, and name
    the cases; rounding it into an accuracy figure would make the report agree with the exit code
    while hiding which case carried the risk. This also pins that the failing value is legible beside
    the expected one, because "field accuracy 50%" is not a bug report.

    Breaking this means a green-looking page over a poisoned corpus.
    """
    page = render_html(_render_fixture(), title="T", source="seqforge eval run")

    assert "FALSE ACCEPT" in page, "a false accept is stated outright, not folded into a percentage"
    assert "poisoned-one" in page, "and the case is named"
    assert "bulk-rnaseq-pe" in page and "10x-3p-gex-v3" in page, "the wrong value must be visible"
    assert "library.chemistry" in page, "beside the field path it was wrong about"

    clean = dict(_render_fixture())
    clean["per_case"] = [c for c in clean["per_case"] if c.get("grade") != "false_accept"]
    ok_page = render_html(clean, title="T", source=None)
    assert "No false accepts." in ok_page
    assert "FALSE ACCEPT in" not in ok_page


def test_a_skip_keeps_its_reason_and_never_becomes_a_pass() -> None:
    """An unreachable HF package is a real state of the benchmark tier, not an absence.

    `build_report` excludes skips from every rate; a page that also excluded them from the LIST would
    let a tier silently shrink — thirteen cases quietly becoming eight while every rate stayed 100%.
    So a skip renders with its reason on the face of the card, and the page says out loud that skips
    are outside the rates.
    """
    page = render_html(_render_fixture(), title="T", source=None)
    assert "offline-one" in page, "a skip is not a silent omission"
    assert "package unreachable" in page, "and it carries WHY"
    assert "excluded from every rate" in page, "a skip is never a pass"
    assert "2 skipped" in page, "and the count is on the summary tile"

    # The degenerate benchmark run: HF is down, every package is unreachable, nothing graded. That
    # must read as "nothing was measured", never as a clean sheet.
    all_skipped = {
        "per_case": [{"case": f"c{i}", "skipped": "package unreachable"} for i in range(3)],
        "cost": {},
        "questions_asked": {},
    }
    page = render_html(all_skipped, title="T", source=None)
    assert "Nothing was graded — all 3 cases skipped." in page
    assert "No false accepts." in page, "true, and stated beside the fact that nothing ran"


def test_a_package_the_corpus_never_held_reads_as_a_gap_and_not_as_a_blip() -> None:
    """`skip_kind: absent` is a finding about the CORPUS; a plain skip is an accident of the machine.

    Both stay outside every rate — the benchmark tier is opt-in and gates no merge, so a dataset that
    was never uploaded must not fail a run. What it must not do either is read as bad weather: the
    fix for "offline" is to try again, and the fix for "never published" is to publish the package.
    A page that spelled them the same way let a dataset go quietly missing behind a word that means
    *transient*, which is exactly how GSE110823 sat out of the corpus without anyone tripping over it.

    The distinction is machine-readable first (`skip_kind` in the JSON, and in `eval report`'s stdout
    summary) and visible second, so an agent reading the report never has to grep a sentence for 404.
    """
    page = render_html(_render_fixture(), title="T", source=None)
    assert "never-published-one" in page
    assert ">absent<" in page, "the absent case is labelled as such, not as a generic skip"
    assert "a gap in the corpus" in page, "and the page says what that means"
    assert "1 of them never published" in page, "counted apart from the skips on the summary tile"
    # ...and the plain skip is untouched: still a skip, still excluded, still not called absent.
    offline = page[page.index("offline-one") :]
    assert offline[: offline.index("</div>")].count(">absent<") == 0

    # The degenerate case of the degenerate case: nothing graded, and the reason is the corpus.
    nothing = {
        "per_case": [
            {"case": f"c{i}", "skipped": "never published", "skip_kind": "absent"} for i in range(3)
        ],
        "cost": {},
        "questions_asked": {},
    }
    empty = render_html(nothing, title="T", source=None)
    assert "Nothing was graded — all 3 cases skipped." in empty
    assert "3 of them absent — the corpus does not hold those packages." in empty


def test_the_harness_carries_the_absent_state_from_the_fetch_seam_to_the_json() -> None:
    """One fact, three layers: `io` types it, `case` labels the skip, `run` puts it in the JSON.

    The report can only tell a gap from a blip if the state survives the whole way, and every hop is
    somewhere it could be flattened back into a sentence. This walks the hops rather than the page.
    """
    from seqforge.evals.case import CaseSkipped
    from seqforge.evals.run import CaseRun
    from seqforge.io import BenchmarkPackageAbsent, BenchmarkPackageUnavailable

    assert issubclass(BenchmarkPackageAbsent, BenchmarkPackageUnavailable)
    assert CaseSkipped("x").kind == "unavailable", "the default is the state that says nothing"
    assert CaseSkipped("x", kind="absent").kind == "absent"

    absent = CaseRun("c", _empty_grade_for("c"), skipped="never published", skip_kind="absent")
    assert absent.to_json() == {
        "case": "c",
        "seconds": 0.0,
        "llm_calls": 0,
        "skipped": "never published",
        "skip_kind": "absent",
    }
    plain = CaseRun("c", _empty_grade_for("c"), skipped="offline")
    assert plain.to_json()["skip_kind"] == "unavailable"


def test_the_report_separates_work_done_from_elapsed_time() -> None:
    """`cost.seconds` is the SUM of per-case durations; `cost.wall_seconds` is the clock.

    They were the same number until the runner went parallel, and the renderer this replaced printed
    the sum under the label "wall time" — which now reports a 61-second run as having taken seven
    minutes. Both are worth having and they answer different questions: the sum says the corpus got
    more expensive, the elapsed says the run got faster. The page must therefore show both, labelled,
    and must not call either one the other.
    """
    page = render_html(_render_fixture(), title="T", source=None)
    assert "work done" in page and "sum of the per-case durations" in page
    assert "elapsed" in page and "wall clock" in page
    assert "6.9m" in page, "the summed work, in its own tile"
    assert "61.3s" in page, "and the elapsed time, in another"
    assert "wall time" not in page, "the bug this replaces: the sum labelled as the clock"

    # An older report (the committed 2026-07-31 benchmark) predates `wall_seconds`. Saying so beats
    # inventing it, and beats silently printing the sum in its place.
    older = _render_fixture()
    del older["cost"]["wall_seconds"]
    page = render_html(older, title="T", source=None)
    assert "not recorded by this run" in page


def _partly_measured_fixture() -> dict[str, Any]:
    """The fixture's one harvesting case, with a document the provider never answered."""
    fixture = _render_fixture()
    fixture["harvest"] = {
        "cases": 1.0,
        "cases_complete": 0.0,
        "cases_partial": 1.0,
        "cases_unmeasured": 0.0,
        "documents_planned": 73.0,
        "documents_extracted": 68.0,
        "documents_failed": 5.0,
        "assertions_unchecked": 1.0,
    }
    case = next(c for c in fixture["per_case"] if c["case"] == "poisoned-one")
    case["harvest"]["status"] = "partial"
    case["harvest"]["n_documents_failed"] = 5
    case["harvest"]["unchecked"] = ["experiment.samples.strain"]
    case["harvest"]["documents"][1]["failure"] = (
        "deepseek returned output that is not valid JSON: Expecting value: line 1 column 1"
    )
    return fixture


def test_the_page_says_how_much_of_the_harvest_stage_ran() -> None:
    """The finding, on the page: a green `matched` list says nothing about the documents that never
    answered, and until now nothing on the page said which those were.

    Three places, because they answer three different questions — how much of the tier (the tile),
    how much of this case (the status line on the card), and which document and why (its own row).
    """
    page = render_html(_partly_measured_fixture(), title="T", source=None)

    assert "harvest coverage" in page and "68 of 73" in page
    assert "1 partly" in page and "1 assertion(s) unchecked" in page
    assert "documents answered" in page, "the per-case status line qualifies the chips under it"
    assert "unchecked" in page and "experiment.samples.strain" in page
    assert "could not check, not checked and found nothing" in page
    # ...and WHICH document, by the name a human reads, with the provider's own reason
    assert "SAMN14126930" in page and "not valid JSON" in page


def test_a_fully_measured_run_says_so_rather_than_staying_silent() -> None:
    """The other half: a page that only ever mentions coverage when it is bad teaches a reader to
    read silence as good, which is precisely how the first tier pass went unnoticed."""
    fixture = _partly_measured_fixture()
    fixture["harvest"] = {
        "cases": 1.0,
        "cases_complete": 1.0,
        "cases_partial": 0.0,
        "cases_unmeasured": 0.0,
        "documents_planned": 73.0,
        "documents_extracted": 73.0,
        "documents_failed": 0.0,
        "assertions_unchecked": 0.0,
    }
    page = render_html(fixture, title="T", source=None)
    assert "every planned document answered" in page


def test_a_report_predating_the_coverage_field_still_renders_as_it_meant() -> None:
    """An older report has no `status` and no `harvest` block, and it is read as complete — which is
    what it meant, since a case whose model failed did not appear as a graded case at all then.

    A renderer that could only read the current shape would make the committed benchmark reports
    unreadable on the day the shape changed.
    """
    page = render_html(_render_fixture(), title="T", source=None)
    assert "harvest coverage" not in page, "no coverage was recorded, so none is claimed"
    assert "unmeasured" not in page and "documents answered" not in page
    assert "experiment.organism" in page, "and the harvest block it DID record still renders"


def test_a_no_llm_run_gets_no_coverage_tile_rather_than_a_zeroed_one() -> None:
    """`0 of 0 documents` reads as a coverage failure. `--no-llm` has no stage to have covered."""
    byte_only = _render_fixture()
    byte_only["harvest"] = None
    assert "harvest coverage" not in render_html(byte_only, title="T", source=None)


def test_header_names_the_extractor_beside_the_command() -> None:
    """The command alone under-describes the run now that one preset has two models.

    `seqforge eval run --llm` reads identically whether it spent flash money or pro money, and the
    numbers differ. The header carries both; a report from a `--no-llm` run, or from before the
    field existed, drops the chip rather than guessing a model that never ran.
    """
    page = render_html(_render_fixture(), title="T", source="seqforge eval run --llm")
    assert "deepseek/deepseek-v4-flash" in page, "which model produced these numbers"
    assert "2026.7.2" in page, "and under which prompt — the other half of the extractor"

    byte_only = _render_fixture()
    del byte_only["extractor"]
    page = render_html(byte_only, title="T", source="seqforge eval run")
    assert "extractor" not in page.split("</style>", 1)[1]
    assert "seqforge eval run" in page, "the command line survives on its own"


def test_cases_are_ordered_worst_first_and_severity_is_carried_in_form() -> None:
    """What needs attention has to read at a glance, without parsing any text.

    Two mechanisms, both asserted here. ORDER: cases sort by `GRADE_ORDER`, so a false accept is the
    first thing on the page and a correct case cannot push it below the fold. FORM: the grade is a
    `lv-*` severity class on the card, the stripe and the pill — not only the grade's name — and
    `false_accept` gets its own level rather than sharing "bad" with `mis_triage`, because it is not a
    worse shade of bad, it is the failure with no tolerable rate.
    """
    page = render_html(_render_fixture(), title="T", source=None)
    order = [
        page.index(f'<code class="case-id">{case}</code>')
        for case in (
            "poisoned-one",  # false_accept
            "triaged-wrong",  # mis_triage
            "blocked-wrongly",  # false_refuse
            "right-answer-wrong-reason",  # wrong_reason
            "asked-too-much",  # over_ask
            "green-one",  # correct
            # Skips are a state, not a grade, so they sort last — and among themselves by id.
            "never-published-one",
            "offline-one",
        )
    ]
    assert order == sorted(order), "cases must be laid out worst grade first, then skips"

    assert 'class="pill lv-poison">false_accept<' in page, "the worst grade gets its own level"
    assert 'class="pill lv-bad">mis_triage<' in page
    assert 'class="pill lv-warn">over_ask<' in page
    assert 'class="pill lv-ok">correct<' in page
    # Failures are open on arrival; a correct case collapses. No script has run at this point.
    assert 'data-level="poison" open' in page
    assert 'data-level="ok" open' not in page


def test_a_failed_field_shows_expected_beside_actual_and_marks_what_differs() -> None:
    """A failed `experiment.samples.*` check is a multiset, and the question is WHICH element moved.

    These lists are seven samples with two distinct values between them, so the page collapses
    identical elements onto a multiplicity and marks the chips whose count differs from the other
    side. Printing the raw list would show seven chips of which one differs by position — true, and
    not the question anyone is asking.
    """
    page = render_html(_render_fixture(), title="T", source=None)
    assert "experiment.samples.*.strain" in page
    assert "×2" in page and "×3" in page, "identical elements collapse onto their multiplicity"
    assert 'class="chip chip-diff">N2<span class="mult">×3' in page, (
        "the count that differs is marked"
    )
    assert 'class="chip chip-diff">VC2010, derived from N2' in page, (
        "so is the value only one side has"
    )


def test_the_report_surfaces_trials_harvest_and_the_token_bill() -> None:
    """Everything `eval run` measures beyond the grade, because it measured it for a reason.

    A case correct 2 times in 3 is a finding, not a rounding error (`_merge_harvest` refuses to
    average it away, so the page must not either). A `hallucinated` field is corpus poison and is
    labelled as such; `n_rejected` is the span-verification tripwire WORKING and must not read as a
    failure. Tokens and exchanges are what a `--llm` pass costs.
    """
    page = render_html(_render_fixture(), title="T", source=None)
    assert "1/3</b> trials correct" in page, "stability is reported as a fraction of trials"
    assert "hallucinated" in page and "experiment.samples.tissue" in page
    assert "nothing downstream would catch these" in page, "a hallucination is named as poison"
    assert "unstable" in page and "experiment.samples.dev_stage" in page
    assert "the safety net working, not a failure" in page, "n_rejected is not a failure count"
    assert "max_tokens 8000 · response_format json_object" in page, (
        "how the calls were made, beside what they cost — the eval path used to drop it"
    )
    assert "188,423 tokens" in page, "the token bill"
    assert "missed a question it had to ask" in page, "questions_asked.missed reaches the case"


def test_a_graded_claim_is_shown_with_the_quote_it_rests_on() -> None:
    """`library.chemistry = "RNA-Seq"` is not a finding; *from this quote, at these offsets* is.

    The grade used to flatten each Assertion to `field -> str(value)` on the way to the report, which
    threw away the only part of it a reader can independently check. So the page shows the quote, the
    document it came from, and the span code computed for it.

    What the page deliberately does NOT show is a per-row pair of verification ticks. `verify` only
    builds an Assertion once `span_verified` and `entailment_ok` both hold, so a column of them is a
    column of the constant `true` — evidence-shaped and carrying nothing. The invariant is stated
    once, and it points at the rejected drawer, where those checks are the ones that fired.
    """
    page = render_html(_render_fixture(), title="T", source=None)

    assert "worms (Caenorhabditis elegans) were grown at 20 C" in page, "the quote itself"
    assert "chars 4,120–4,168" in page, "and where it is, which is what makes it checkable"
    assert "p.3" in page, "including the page, when the document has pages"
    assert "methods.pdf · dataset" in page, "and which document, not a bare sha256"
    assert "span-verified <b>and</b> entailment-checked" in page, "the invariant, stated once"
    assert page.count("span-verified") == 1, "...once, not once per row of always-true booleans"


def test_rejected_drafts_are_readable_instead_of_being_a_count() -> None:
    """One benchmark case's 84 refusals survived as the integer 84 — a net caught something, and
    nothing about what.

    Both producers have to read the same way: a draft the model returned malformed (whose field,
    value and quote may all be absent — the row says `none` rather than printing `None`) and a draft
    whose field, document, quote or entailment failed. The reason is what tells them apart, so it
    leads the row.
    """
    page = render_html(_render_fixture(), title="T", source=None)

    assert "the 4 draft(s) the tripwire threw out" in page
    assert "not_entailed" in page and "field_not_permitted_for_doc" in page
    assert "droplet-based single-cell" in page, "the quote that was refused"
    assert "10x-3p-gex-v3" in page, "beside the value it failed to support"
    assert "quote does not support value" in page, "and the detail that says which check fired"
    assert "malformed_draft" in page and ">none<" in page, "a draft with no field says so"
    assert "SAMN14126930 · sample" in page, "every refusal names the document it came from"


def test_a_report_from_before_the_grade_carried_its_claims_still_renders() -> None:
    """The committed benchmark reports predate the shape and must not become unreadable.

    An older run recorded a flat `extracted` dict and an integer `n_rejected`. It projects with the
    values it has and says outright that it has no quotes, rather than dropping the block or
    inventing provenance for a claim whose provenance was never written down.
    """
    older = _render_fixture()
    case = older["per_case"][1]
    case["harvest"] = {
        "matched": ["experiment.organism"],
        "missing": [],
        "hallucinated": [],
        "n_rejected": 84,
        "extracted": {"experiment.organism": "Caenorhabditis elegans"},
    }
    case.pop("exchanges")
    page = render_html(older, title="T", source=None)

    assert "Caenorhabditis elegans" in page, "the value it did record"
    assert "not recorded by this run" in page, "and the absence of a quote, said out loud"
    assert "84" in page, "the old count still reads as the count it was"


# --------------------------------------------------------------------------------------------
# the transcript on the page — a sample, and what it left out
# --------------------------------------------------------------------------------------------
#
# A transcript is one system prompt plus N (document, response) pairs, and N reaches the hundreds on
# a corpus-scale run. The page is ONE inlined file that opens from `file://`, so every exchange is
# not an option — and the first twelve is not a sample, it is whatever the thread pool finished
# first. These pin what is selected, and that the page never shows less than everything in silence.


def _exchange(sha: str, *, text: str = '{"drafts": []}', error: str | None = None) -> Any:
    from seqforge.harvest import Exchange

    return Exchange(
        prompt_sha256="c3" * 32,
        user=f"Document sha256: {sha}\nFields to look for:\n- experiment.organism\n\n<document>x",
        text=text,
        usage={"input_tokens": 100, "output_tokens": 10},
        model="stub-1",
        error=error,
    )


def test_the_exchange_selection_keeps_what_has_signal_and_one_of_every_scope() -> None:
    """The representative rule, against the function that owns it.

    Four things earn an exchange its place: it failed (it produced neither draft nor claim, so no
    other rule would keep it, and "we paid and got nothing" is exactly what a reader is looking
    for), its document produced a refused draft, its document produced a graded claim, or it is the
    first of its scope. The cap bounds the first three; scope coverage is added afterwards and never
    competes with them, because five extra exchanges is what coverage costs.
    """
    from seqforge.evals.report import select_exchanges

    shas = [f"{i:02x}" * 32 for i in range(6)]
    exchanges = [_exchange(s) for s in shas] + [_exchange(shas[4], error="429")]
    scopes = {shas[0]: "dataset", shas[1]: "sample", shas[2]: "sample", shas[3]: "sample"}
    # shas[4] and shas[5] are in no document list at all — an exchange the report cannot place.

    chosen = select_exchanges(
        exchanges,
        scopes=scopes,
        claimed={shas[1]},
        refused={shas[3]},
        limit=None,
    )
    why = {x.user.split()[2]: reason for x, reason in chosen}
    assert why[shas[1]] == "produced a graded assertion"
    assert why[shas[3]] == "produced a rejected draft"
    assert why[shas[4]] == "the request failed", "an exchange no document list can place, kept"
    assert why[shas[0]] == "the first dataset-scoped document"
    assert len(chosen) == 4, "the two with signal, the failed request, and the uncovered scope"
    assert shas[2] not in why, "`sample` was already covered — coverage, not one per document"
    assert [x for x, _ in chosen] == sorted((x for x, _ in chosen), key=exchanges.index), (
        "the sample is a subsequence of the run, never a ranking"
    )

    # The cap bites the signal-selected ones only, and coverage survives it.
    capped = select_exchanges(exchanges, scopes=scopes, claimed=set(shas), refused=set(), limit=1)
    assert len(capped) == 2, "one under the cap, plus the scope nothing else covered"


def test_a_sampled_transcript_says_how_much_it_left_out() -> None:
    """The minimum this page owes a reader. A transcript truncated in silence reads as a complete
    one, which turns "the model was never asked about that" and "we did not show you" into the same
    page."""
    page = render_html(_render_fixture(), title="T", source=None)

    assert "Showing <b>2 of 412 exchanges</b>" in page
    assert "clipped — showing 800 of 2,112 characters" in page, "and the clip inside one, too"
    assert "transcripts/poisoned-one.jsonl" in page, "beside the address of the unclipped run"
    assert "produced a graded assertion" in page, "each exchange says why it was the one shown"
    assert "deepseek: 429 rate limited" in page, "a failed request is spend with nothing to show"


def test_the_system_prompt_is_rendered_once_for_the_whole_report() -> None:
    """One prompt plus N (document, response) pairs — that IS a transcript.

    The system prompt is byte-identical across every request in a run, which is exactly why prefix
    caching works; rendering it per exchange would be the same three kilobytes a few hundred times.
    """
    fixture = _render_fixture()
    page = render_html(fixture, title="T", source=None)
    body = page.split("</style>", 1)[1]

    prompt = fixture["prompts"][0]["text"]
    assert body.count("You extract factual claims") == 1, "once per report, not once per exchange"
    assert prompt.splitlines()[0] in body
    assert "The system prompt <code class='val'>2026.7.2</code>" in body, "with its version"
    assert "412 exchange(s)" in body, "and how many requests it was sent with"

    # A run that somehow issued two prompts says so: the prefix cannot have been cached across them.
    fixture["prompts"].append({"sha256": "d4" * 32, "text": "Other.", "n_exchanges": 3})
    assert "2</b> distinct system prompts" in render_html(fixture, title="T", source=None)

    # ...and a report with no transcript beside it has no panel at all. Absent, not empty.
    del fixture["prompts"]
    assert "The system prompt" not in render_html(fixture, title="T", source=None)


def test_a_recorded_transcript_that_was_not_read_says_so_rather_than_reading_as_none() -> None:
    """`eval report report.json` (or `--transcript none`) renders the grade and no exchanges. That
    is a different sentence from "this case reached no model", and the page has to spell it."""
    fixture = _render_fixture()
    case = fixture["per_case"][1]
    del case["exchanges"]
    del fixture["prompts"]
    page = render_html(fixture, title="T", source=None)
    assert "were recorded in" in page and "transcripts/poisoned-one.jsonl" in page
    assert "and not read" in page


def test_a_case_stopped_at_its_ceiling_still_shows_what_it_spent_the_tokens_on() -> None:
    """A blocked case reports as a skip, and a skip card carries no grade — but the exchanges up to
    the breach were paid for, and "on what?" is the whole question a Ceiling breach produces."""
    fixture = _render_fixture()
    blocked = fixture["per_case"][1]
    blocked["skipped"] = "token ceiling: 500,000 tokens spent over 412 exchange(s)"
    page = render_html(fixture, title="T", source=None)

    assert "token ceiling" in page
    assert "Showing <b>2 of 412 exchanges</b>" in page, (
        "the spend is on the card, not only a number"
    )


def test_a_corpus_scale_transcript_is_bounded_by_the_sample_and_not_by_the_run() -> None:
    """The display discipline, measured. A saturated corpus — fourteen cases, hundreds of exchanges
    each, one of them with 84 refused drafts — must produce a page a browser opens, and the number
    of exchanges the RUN made must not be what decides its size."""
    fixture = _render_fixture()
    template = fixture["per_case"][1]
    saturated = []
    for i in range(14):
        case = json.loads(json.dumps(template))
        case["case"] = f"heavy-{i:02d}"
        case["n_exchanges"] = 983
        case["exchanges"] = [
            _fixture_exchange(_FIXTURE_DOC, "produced a rejected draft") for _ in range(17)
        ]
        case["harvest"]["rejected"] = case["harvest"]["rejected"] * 21  # 84
        saturated.append(case)
    fixture["per_case"] = saturated

    page = render_html(fixture, title="T", source=None)
    assert len(page.encode()) < 1_000_000, "a saturated corpus still opens as one page"

    # And the total is not what sized it: ten times the exchanges, byte-identical page.
    for case in fixture["per_case"]:
        case["n_exchanges"] = 9830
    assert len(render_html(fixture, title="T", source=None).encode()) == len(page.encode()) + 14


def test_the_eval_report_makes_no_external_network_reference() -> None:
    """The whole point is that it opens offline.

    A benchmark page is downloaded as a CI artifact and opened from `file://`, where an external
    stylesheet or script silently renders NOTHING — the failure looks like a styling bug and is
    actually a missing asset. This is also what makes Tailwind admissible here only as a vendored,
    built CSS file: a Play-CDN `<script src=...>` would fail exactly this assertion.

    Note the check is on fetchable references, not on every `http` substring: a skip reason quotes the
    HF URL that 404'd, and the vendored stylesheet carries Tailwind's MIT banner. Neither is a fetch.
    """
    fixture = _render_fixture()
    fixture["per_case"][-1]["skipped"] = (
        "could not fetch 'packages/GSE110823.fingerprint.tar.gz' from "
        "https://huggingface.co/datasets/liuhlab/seqforge-benchmark/resolve/main/x: 404"
    )
    page = render_html(fixture, title="T", source=None)

    offsite = re.findall(r'(?:src|href)\s*=\s*["\']?(?:https?:)?//[^"\'\s>]+', page)
    assert not offsite, f"external references leaked in: {offsite[:3]}"
    assert "@import url(http" not in page.replace(" ", "").lower()
    assert "cdn.tailwindcss.com" not in page, "the Play CDN would render nothing from file://"
    assert "cdn.jsdelivr" not in page and "unpkg" not in page, "a CDN link regressed in"
    # ...and the 404 reason still made it onto the page, URL and all.
    assert "404" in page and "GSE110823" in page
    assert len(page.encode()) < 500_000, "the page bloated (a heavy asset regressed in?)"


def test_both_themes_render_and_an_explicit_theme_beats_the_media_query() -> None:
    """The page is opened from disk AND published to a host that has its own light/dark toggle.

    So it needs both mechanisms, and the explicit one has to win in BOTH directions: a host stamping
    `data-theme="light"` on a dark OS must actually go light, which a bare `prefers-color-scheme`
    block cannot do. Semantic colour is asserted to be a separate token family from the accent — if
    `ok` were the accent hue, a reader could not tell chrome from a verdict.
    """
    page = render_html(_render_fixture(), title="T", source=None)
    # The stylesheet is minified, so attribute-selector quoting is not guaranteed: normalise it away
    # rather than pin the minifier's taste.
    flat = page.replace(": ", ":").replace('"', "").replace("'", "")
    assert "@media (prefers-color-scheme:dark)" in flat
    assert ":root[data-theme=dark]" in flat
    assert ":root:not([data-theme=light])" in flat, (
        "system dark must yield to an explicit light theme, or the toggle only works one way"
    )
    assert "--sf-ok:" in page and "--sf-crit:" in page and "--sf-warn:" in page
    assert "--sf-accent:" in page, "the accent is its own token and never a verdict colour"
    assert "tabular-nums" in page, "digits that line up get lined up"


def test_the_page_inlines_its_script_and_never_fetches_one() -> None:
    """The JS is inlined and guarded, exactly as `seqforge.report` does it.

    `_script_guard` neutralises any `</script` inside the embedded source so a future edit cannot
    close the inlining tag early and dump the rest of the script as page text. The page is fully
    readable with JS disabled — the toggle and the filter are enhancements — which is why the
    open/closed state of every case is an attribute set at render time.
    """
    page = render_html(_render_fixture(), title="T", source=None)
    assert "<script>" in page and "<script " not in page, "inlined, never sourced"
    body = page.split("<script>", 1)[1]
    assert "</script" not in body[: body.rindex("</script>")], (
        "an unguarded </script would truncate"
    )


def test_the_page_template_and_the_renderer_cannot_drift_apart() -> None:
    """The shell lives in `assets/eval-report.html`; the fragments live in Python. Both must agree.

    Splitting them is what makes the layout editable without reading string concatenation, and the
    obvious failure of that split is silent: rename a slot in one file and the page renders with a
    literal `{{TILES}}` where a panel should be, or drops a section entirely. `_fill` refuses in both
    directions instead — a slot nobody fills, and a fill nobody asked for.

    It is also one regex pass, not `str.format` and not repeated `.replace`: the page embeds arbitrary
    archive prose, and a case id that happened to contain `{{CSS}}` must be text, never a second
    substitution.
    """
    from seqforge.evals.report import _fill

    assert _fill("<i>{{A}}</i>", A="x") == "<i>x</i>"
    with pytest.raises(KeyError, match="does not fill"):
        _fill("{{NOPE}}", A="x")
    with pytest.raises(KeyError, match="does not have"):
        _fill("{{A}}", A="x", B="y")
    # A slot's own content is never rescanned.
    assert _fill("{{A}}{{B}}", A="{{B}}", B="!") == "{{B}}!"

    template = (
        Path(__file__).resolve().parents[1] / "src/seqforge/evals/assets/eval-report.html"
    ).read_text()
    page = render_html(_render_fixture(), title="T", source="x")
    assert "{{" not in page, "every slot was filled"
    assert re.findall(r"\{\{([A-Z_]+)\}\}", template), "the template really is slotted"


def test_every_class_the_page_uses_has_a_rule_in_the_stylesheet() -> None:
    """The drift guard for the vendored Tailwind build (see `evals/assets/VENDOR.md`).

    Tailwind purges: `eval-report.css` contains only the utilities that were literally present in
    `report.py` when the build ran. So adding `class="mt-8"` and not rebuilding is a SILENT failure —
    the page keeps rendering and one rule is quietly absent. This renders a report exercising every
    branch and fails if any class on the page has no rule, which is the mechanism that makes "rebuild
    the CSS" something nobody has to remember.
    """
    page = render_html(_render_fixture(), title="T", source="x")
    css = (
        Path(__file__).resolve().parents[1] / "src/seqforge/evals/assets/eval-report.css"
    ).read_text()
    used: set[str] = set()
    # Both quote styles: a fragment nested inside a double-quoted Python string writes `class='x'`,
    # and a guard that only saw `class="x"` would quietly stop checking exactly those.
    for group in re.findall(r"""class=["']([^"']*)["']""", page.split("</style>", 1)[1]):
        used |= set(group.split())
    assert used, "the page has classes"
    # Tailwind escapes `:` `/` `.` etc. in selectors, so `md:grid-cols-2` is `.md\:grid-cols-2`.
    missing = sorted(
        c
        for c in used
        if not re.search(
            r"\." + re.escape(re.sub(r"([:/.\[\]()%,])", r"\\\1", c)) + r"(?![\w-])", css
        )
    )
    assert not missing, (
        f"classes with no rule: {missing} — rebuild eval-report.css (see assets/VENDOR.md)"
    )


def test_the_built_stylesheet_carries_every_component_its_source_declares() -> None:
    """The other half of the drift guard, from the CSS side.

    The test above catches a new class in `report.py`; this one catches a new component in
    `eval-report.src.css` that was edited and never compiled. Together they mean the built file cannot
    silently fall behind either of its two inputs — which matters because the build needs npm and so
    cannot run in CI.
    """
    assets = Path(__file__).resolve().parents[1] / "src/seqforge/evals/assets"
    source = re.sub(r"/\*.*?\*/", "", (assets / "eval-report.src.css").read_text(), flags=re.S)
    built = (assets / "eval-report.css").read_text()

    components = source[source.index("@layer components") :]
    declared = set(re.findall(r"\.([a-zA-Z][\w-]*)", components))
    assert len(declared) > 20, "the source really does declare components"
    missing = sorted(c for c in declared if not re.search(rf"\.{re.escape(c)}(?![\w-])", built))
    assert not missing, (
        f"declared in eval-report.src.css but absent from the built CSS: {missing} — "
        "rebuild it (see assets/VENDOR.md)"
    )


def test_eval_report_is_a_verb_that_writes_a_file_and_answers_on_stdout(tmp_path: Path) -> None:
    """`seqforge eval report` follows `seqforge report`, not a `scripts/` one-off.

    Three things are the contract. It WRITES the HTML and prints machine JSON on stdout, so it is a
    verb an agent can drive rather than a file only a human knows how to produce — the CLI is the API.
    It NAMES the false accepts in that summary rather than counting them, for the same reason the page
    does. And it exits 0 whatever the report said: the verdict belongs to `eval run`, which already
    refuses on a false accept, and a renderer that failed too would make a red benchmark destroy the
    artifact that explains it.
    """
    from typer.testing import CliRunner

    from seqforge.cli import app

    src = tmp_path / "report.json"
    src.write_text(json.dumps(_render_fixture()))
    out = tmp_path / "nested" / "report.html"

    result = CliRunner().invoke(app, ["eval", "report", str(src), "-o", str(out), "--no-timestamp"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["false_accepts"] == ["poisoned-one"], "named on stdout, not just counted"
    assert payload["skipped"] == 2
    assert payload["absent"] == 1, "a package the corpus never held is counted apart from a skip"
    assert out.exists() and payload["bytes"] == len(out.read_text().encode())
    assert out.read_text().startswith("<!doctype html>")

    # Not JSON, and not an eval report: both are the caller's error, and neither writes a page.
    (tmp_path / "junk.json").write_text("not json at all")
    bad = CliRunner().invoke(app, ["eval", "report", str(tmp_path / "junk.json")])
    assert bad.exit_code == 1
    assert not (tmp_path / "junk.html").exists()


def test_the_render_summary_counts_a_case_whose_stage_did_not_finish(tmp_path: Path) -> None:
    """A graded case whose LLM half did not run is invisible in a count of what skipped, because it
    did not skip — the byte half really ran. So the summary counts it on a line of its own."""
    from typer.testing import CliRunner

    from seqforge.cli import app

    src = tmp_path / "report.json"
    src.write_text(json.dumps(_partly_measured_fixture()))
    result = CliRunner().invoke(
        app, ["eval", "report", str(src), "-o", str(tmp_path / "p.html"), "--no-timestamp"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["harvest_not_fully_measured"] == 1
    assert payload["skipped"] == 2, "and it is not double-counted as one of those"


def test_a_partly_measured_llm_run_reports_it_on_stderr_and_still_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The constraint and the fix in one place.

    Making a flaky provider fail the tier would put this command's exit code at the mercy of
    somebody else's uptime — exit 3 means the compiler answered wrong and exit 4 means a human is
    owed an answer, and an unanswered document is neither. So the coverage is *said*, loudly, on the
    stream humans read, and the exit code is unmoved.
    """
    from typer.testing import CliRunner

    import seqforge.harvest as harvest_mod
    from seqforge.cli import app

    monkeypatch.setattr(
        harvest_mod, "resolve_provider", lambda *a, **k: _PoisonedProvider([], "droplet")
    )
    result = CliRunner().invoke(
        app,
        ["eval", "run", "--llm", "--case", "chemistry-unstated-trap", "-C", str(tmp_path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["harvest"]["cases_unmeasured"] == 1.0
    assert payload["field_accuracy"] == 1.0, "the byte half graded; nothing unchecked entered it"
    (row,) = payload["per_case"]
    assert "skipped" not in row, "the case graded; only its LLM half did not"
    assert row["harvest"]["status"] == "unmeasured"
    # ...and it is said where a human looks, on the stream that is not the result object.
    assert "HARVEST PARTLY MEASURED" in result.stderr
    assert "chemistry-unstated-trap" in result.stderr
    # The remedy names the model that ran and prescribes no model at all. It used to advise `--model
    # deepseek-v4-pro` unconditionally — including on a run of pro, where the advice was to change
    # nothing (#188). `stub-model-1` is this provider's default, resolved rather than echoed as null.
    assert "answered by stub-model-1" in result.stderr
    assert "--model" not in result.stderr, "naming the extractor is not prescribing a replacement"


def _stub_case(case_id: str) -> Case:
    """A Case the parallel tests can make by the dozen — `run_case` is patched out, so only `id`
    is ever read, but it is a real Case so the signature under test is the real one."""
    return Case(
        id=case_id,
        root=Path("/nonexistent") / case_id,
        recipe=Recipe.model_validate({"generate": {"kind": "random", "n": 1}}),
        expected=Expected.model_validate(
            {"outcome": "decide", "description": "stub", "fields": {"library.chemistry": "x"}}
        ),
        metadata_docs=[],
    )


def _empty_grade_for(case_id: str) -> CaseGrade:
    return CaseGrade(
        case_id=case_id, grade=Grade.CORRECT, expected_outcome="decide", actual_outcome="decide"
    )


def test_parallel_runs_preserve_order_and_do_not_change_any_result() -> None:
    """`run_cases(jobs=N)` is a speed change and must be nothing else.

    Two claims, both load-bearing rather than tidy. **Order**: `per_case` is committed to reports and
    read in diffs, so rows must follow the case list, not the order threads happened to finish — the
    stub makes the FIRST case the slowest, so an implementation collecting by completion returns them
    backwards and fails here. **Identity**: the same corpus at `jobs=1` and `jobs=8` must produce the
    same report, or the number depends on the machine that ran it.
    """
    from seqforge.evals import run as run_mod

    order = ["slow-first", "middle", "fast-last"]
    delays = {"slow-first": 0.30, "middle": 0.15, "fast-last": 0.0}

    def fake_run_case(case: Any, **kwargs: Any) -> CaseRun:
        time.sleep(delays[case.id])
        return CaseRun(case.id, _empty_grade_for(case.id), seconds=delays[case.id])

    cases = [_stub_case(cid) for cid in order]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(run_mod, "run_case", fake_run_case)
        parallel, par_runs = run_mod.run_cases(cases, jobs=8)
        sequential, seq_runs = run_mod.run_cases(cases, jobs=1)

    assert [r.case_id for r in par_runs] == order, "rows follow the case list, not finish order"
    assert [r.case_id for r in seq_runs] == order

    # They really overlapped: elapsed is about the slowest case, not the sum of all three.
    assert parallel.cost["wall_seconds"] < parallel.cost["seconds"]
    assert sequential.cost["wall_seconds"] >= sequential.cost["seconds"]

    # ...and nothing else moved.
    assert parallel.field_accuracy == sequential.field_accuracy
    assert parallel.false_accept_rate == sequential.false_accept_rate
    assert parallel.n_cases == sequential.n_cases == len(order)
    assert parallel.cost["seconds"] == sequential.cost["seconds"]


def test_the_job_default_reads_usable_cores_and_stays_capped() -> None:
    """The default must read *usable* cores, and must not fan out without a ceiling.

    `os.cpu_count()` reports the machine; `os.process_cpu_count()` reports what this process may
    actually use under CPU affinity or a cgroup. On the shared node this was written on they read 48
    and 192, and spending the machine's count there oversubscribes every other job on the box. The
    cap matters for the opposite reason: a run wide enough to saturate a node measures contention —
    or, under `--llm`, the provider's rate limit — rather than the compiler.
    """
    from seqforge.evals.run import MAX_DEFAULT_JOBS, default_jobs

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("os.process_cpu_count", lambda: 4)
        assert default_jobs() == 4, "below the cap, take the usable cores"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("os.process_cpu_count", lambda: 192)
        assert default_jobs() == MAX_DEFAULT_JOBS == 24, "a big node does not mean a big fan-out"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("os.process_cpu_count", lambda: None)  # unknowable: serial, never 0
        assert default_jobs() == 1


# --------------------------------------------------------------------------------------------
# the meter in the harness: what a case really cost, and the ceiling that stops one that runs away
# --------------------------------------------------------------------------------------------


class _RetryingProvider:
    """Refuses transiently `n_refusals` times, then answers. Every attempt costs tokens."""

    name = "retrying"

    def __init__(self, n_refusals: int) -> None:
        self._left = n_refusals
        self.calls = 0

    def default_model(self) -> str:
        return "retrying-1"

    def complete_json(self, **kwargs: object) -> LLMResponse:
        import json as _json

        from seqforge.harvest import LLMResponse, ProviderUnavailable

        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise ProviderUnavailable(
                "429 rate limited",
                transient=True,
                retry_after="0",
                usage={"input_tokens": 40},
            )
        return LLMResponse(text=_json.dumps({"drafts": []}), usage={"input_tokens": 100})


def test_a_cases_call_count_is_requests_not_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    """One document, two requests: the first was rate-limited and cost tokens. The old count was
    `len(docs)`, so the retry was invisible in the only number a cost review reads."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    provider = _RetryingProvider(n_refusals=1)

    run = run_case(_trap_case(), llm=True, provider=provider)

    assert provider.calls == 2
    assert run.harvest is not None
    assert run.harvest.n_calls == 2, "the refused attempt is its own exchange"
    assert run.harvest.n_documents == 1, "and the document count is kept beside it"
    assert run.llm_calls == 2
    assert run.usage["input_tokens"] == 140, "the 429's 40 plus the answered call's 100"


class _ExpensiveStub(_StubProvider):
    """A stub whose one answered request costs more than the ceiling can carry twice.

    The meter reserves a request's ESTIMATE before issuing it, so a ceiling buys a number of
    requests rather than a number of banked tokens — which means `ceiling=1` no longer produces a
    breach to observe: it cannot cover the first request either, so nothing is issued, nothing is
    banked, and there is no ledger. Making the SPEND large instead of the ceiling tiny keeps these
    two tests about what they were always about — a case refused on what it already paid for.
    """

    def complete_json(self, **kwargs: object) -> LLMResponse:
        from dataclasses import replace

        return replace(super().complete_json(**kwargs), usage={"input_tokens": 100_000})


def test_a_case_that_reaches_its_ceiling_is_blocked_not_graded() -> None:
    """Per case, because a case is a dataset and the ceiling bounds a dataset — and the meter spans
    the case's trials, so the second trial here is refused on what the first spent. It refuses: the
    case carries a `Blocker` and scores nothing, rather than warning into a log nobody reads.

    The request that would cross the ceiling is refused BEFORE it is issued, on its estimate: the
    first trial is admitted and banks more than a second could fit under, so the second never
    leaves. A breach is therefore reproducible rather than a race, and — unlike the accounting this
    replaced — it does not depend on how many requests happened to be in flight.
    """
    provider = _ExpensiveStub(
        [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    )
    run = run_case(_trap_case(), llm=True, provider=provider, ceiling=50_000, trials=2)

    assert run.blocker is not None
    assert run.llm_calls == 1, "the first trial's request was issued; the second never left"
    assert run.blocker.code is BlockerCode.TOKEN_CEILING_EXCEEDED
    assert run.blocker.subject.ref == _trap_case().id, "the refusal names the dataset it is about"
    assert run.skipped is not None and "ceiling" in run.skipped
    assert run.to_json()["blockers"][0]["code"] == "TOKEN_CEILING_EXCEEDED"
    # excluded from every rate: the case did not run, so it is neither a pass nor a failure
    assert build_report([run]).n_cases == 0


def test_a_ceiling_high_enough_never_binds() -> None:
    """A backstop is a number nothing normally touches; the default must not change any grade."""
    provider = _StubProvider(
        [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    )
    run = run_case(_trap_case(), llm=True, provider=provider, ceiling=500_000)
    assert run.blocker is None and run.skipped is None


def test_the_report_carries_cache_write_tokens() -> None:
    """The Anthropic normalizer produces it and every consumer but the on-disk ledger dropped it —
    so a cache-heavy run's cost was reported short in the only place anybody reads it."""

    class _CachingProvider:
        name = "caching"

        def default_model(self) -> str:
            return "caching-1"

        def complete_json(self, **kwargs: object) -> LLMResponse:
            from seqforge.harvest import LLMResponse

            return LLMResponse(
                text=json.dumps({"drafts": []}),
                usage={
                    "input_tokens": 1500,
                    "output_tokens": 60,
                    "cache_read_tokens": 1000,
                    "cache_write_tokens": 200,
                },
            )

    run = run_case(_trap_case(), llm=True, provider=_CachingProvider())
    cost = build_report([run]).cost
    assert cost["cache_write_tokens"] == 200.0
    assert cost["cache_read_tokens"] == 1000.0


def test_the_default_eval_ceiling_clears_every_case_the_benchmark_measured() -> None:
    """500,000 is a measurement, not a round number: the largest benchmark case other than
    GSE126954 spent 122 K raw (2026-07-31), and this clears it by 4x while stopping GSE126954's
    3.47 M however it is counted."""
    from seqforge.cli.eval import DEFAULT_EVAL_CEILING

    assert DEFAULT_EVAL_CEILING == 500_000
    assert DEFAULT_EVAL_CEILING >= 4 * 122_000
    assert DEFAULT_EVAL_CEILING < 3_475_000


# --------------------------------------------------------------------------------------------
# the run directory — evals were the one part of seqforge that wrote nothing
# --------------------------------------------------------------------------------------------
#
# `run_case` did everything in a TemporaryDirectory that is deleted and the whole result rode on
# stdout, so a 983-exchange transcript had no address it could go to. Stdout is the result object,
# which means the transcript has to be a file, which means the verb has to own a directory.


def test_eval_run_writes_a_run_directory_and_stdout_gains_the_paths(tmp_path: Path) -> None:
    """The paths, not the contents. What lands on stdout keeps its shape and names the files; the
    exchanges themselves are on disk, where a thousand of them are readable."""
    from seqforge.evals import run_cases
    from seqforge.harvest import read_transcript
    from seqforge.workspace import eval_dir

    case = _trap_case()
    provider = _StubProvider(
        [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    )
    report, _ = run_cases([case], llm=True, provider=provider, workspace=tmp_path)

    run_dir = eval_dir(tmp_path)
    assert report.run_dir == str(run_dir)
    row = report.model_dump(mode="json")["per_case"][0]
    assert row["transcript"] == f"transcripts/{case.id}.jsonl", "relative: the directory travels"

    transcript = read_transcript(run_dir / row["transcript"])
    assert transcript.n_exchanges == row["llm_calls"] == 1
    assert transcript.exchanges[0].user not in json.dumps(report.model_dump(mode="json"))


def test_a_run_with_nowhere_to_write_holds_no_transcript(tmp_path: Path) -> None:
    """14 cases x N exchanges of held text is memory nothing will read, so the meter only records
    when there is a directory to write it into."""
    from seqforge.evals import run_cases

    provider = _StubProvider(
        [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    )
    report, runs = run_cases([_trap_case()], llm=True, provider=provider)

    assert report.run_dir is None
    assert runs[0].transcript is None
    assert "transcript" not in runs[0].to_json()


def test_a_case_stopped_at_its_ceiling_still_names_its_transcript(tmp_path: Path) -> None:
    """A skip short-circuits `to_json`, and a blocked case is reported as a skip — but the exchanges
    up to the breach were paid for, and "on what?" is exactly the question a breach produces."""
    from seqforge.harvest import read_transcript
    from seqforge.workspace import eval_dir

    provider = _ExpensiveStub(
        [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    )
    run = run_case(
        _trap_case(),
        llm=True,
        provider=provider,
        ceiling=50_000,
        trials=2,
        workspace=eval_dir(tmp_path),
    )

    assert run.blocker is not None and run.skipped is not None
    row = run.to_json()
    assert row["skipped"] and row["transcript"] == f"transcripts/{run.case_id}.jsonl"
    assert read_transcript(eval_dir(tmp_path) / row["transcript"]).n_exchanges == 1


def test_eval_run_owns_its_directory_and_eval_report_reads_it(tmp_path: Path) -> None:
    """CI used to `mkdir` a directory and `tee` stdout into a path the YAML invented, so the layout
    existed only as long as that shell line. The verb writes `report.json` itself — the same bytes
    it prints — and the renderer is handed the directory rather than a filename to remember."""
    from typer.testing import CliRunner

    from seqforge.cli import app
    from seqforge.workspace import eval_dir

    runner = CliRunner()
    ran = runner.invoke(
        app, ["eval", "run", "--no-llm", "--case", "10x-v3-bytes-only", "-C", str(tmp_path)]
    )
    assert ran.exit_code == 0, ran.output

    run_dir = eval_dir(tmp_path)
    written = (run_dir / "report.json").read_text()
    assert json.loads(written) == json.loads(ran.stdout), "the file and the stream are one object"

    rendered = runner.invoke(app, ["eval", "report", str(run_dir), "--no-timestamp"])
    assert rendered.exit_code == 0, rendered.output
    page = Path(json.loads(rendered.stdout)["report"])
    assert page == run_dir / "report.html", "the page belongs inside the directory it renders"
    assert page.read_text().startswith("<!doctype html>")


def test_the_page_joins_a_real_run_to_the_exchanges_it_paid_for(tmp_path: Path) -> None:
    """The join the whole display rests on, end to end and through a real transcript file.

    An `Exchange` keeps the request verbatim and nothing else about it — it is a record of what was
    sent, not of what was planned — so the only thing tying one to a document is the line the prompt
    itself writes: `Document sha256: <sha>`. `extract` writes it and `document_sha256_in` reads it
    back, in one module, and this pins both ends against the same run: break the prompt's first line
    and the page silently stops being able to say which record an exchange was about.
    """
    from seqforge.evals import run_cases
    from seqforge.evals.report import attach_transcripts
    from seqforge.workspace import eval_dir

    provider = _StubProvider(
        [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    )
    report, _ = run_cases([_trap_case()], llm=True, provider=provider, workspace=tmp_path)

    payload = attach_transcripts(report.model_dump(mode="json"), eval_dir(tmp_path))
    row = payload["per_case"][0]
    (exchange,) = row["exchanges"]
    assert exchange["doc_sha256"] == row["harvest"]["assertions"][0]["span"]["doc_sha256"]
    assert exchange["why"] == "produced a graded assertion"
    assert row["n_exchanges"] == row["llm_calls"] == 1
    assert payload["prompts"][0]["text"].startswith("You extract factual claims")
    assert payload["prompts"][0]["n_exchanges"] == 1

    page = render_html(payload, title="T", source=None, generated_at=None)
    assert "Showing <b>every exchange</b>" in page, "one exchange IS the whole run here"
    assert "Caenorhabditis elegans" in page


def test_the_transcript_flag_chooses_how_many_exchanges_the_page_carries(
    tmp_path: Path,
) -> None:
    """`all | sample | none`, and a report with no directory beside it renders either way.

    The default is a sample because a corpus-scale run cannot be shown whole; `all` exists because
    "show me everything" is a real request; `none` exists because a grading page that is only about
    the grades is a legitimate thing to want. What none of them may do is quietly show less.
    """
    from typer.testing import CliRunner

    from seqforge.cli import app
    from seqforge.evals import run_cases
    from seqforge.workspace import eval_dir

    provider = _StubProvider(
        [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    )
    report, _ = run_cases([_trap_case()], llm=True, provider=provider, workspace=tmp_path)
    run_dir = eval_dir(tmp_path)
    # what `eval run` writes beside the transcripts it just produced
    (run_dir / "report.json").write_text(json.dumps(report.model_dump(mode="json")))
    runner = CliRunner()

    def _render(*flags: str) -> str:
        out = tmp_path / f"page-{'-'.join(flags) or 'default'}.html"
        result = runner.invoke(
            app, ["eval", "report", str(run_dir), "-o", str(out), "--no-timestamp", *flags]
        )
        assert result.exit_code == 0, result.output
        return out.read_text()

    assert "The system prompt" in _render(), "sampled by default"
    assert "The system prompt" in _render("--transcript", "all")
    dark = _render("--transcript", "none")
    assert "The system prompt" not in dark, "none means none"
    assert "and not read" in dark, "...and says the exchanges exist and were not read"

    bad = runner.invoke(app, ["eval", "report", str(run_dir), "--transcript", "everything"])
    assert bad.exit_code == 2, "a bad mode is a usage error, not a page with a guess in it"

    # The JSON alone, with no directory of transcripts beside it: still a report, still renders.
    lonely = tmp_path / "alone" / "report.json"
    lonely.parent.mkdir()
    lonely.write_text((run_dir / "report.json").read_text())
    result = runner.invoke(app, ["eval", "report", str(lonely), "--no-timestamp"])
    assert result.exit_code == 0, result.output
    assert "The system prompt" not in lonely.with_suffix(".html").read_text()


def test_eval_report_takes_the_workspace_as_well_as_the_run_directory(tmp_path: Path) -> None:
    """`eval report <workspace>` finds the run beneath it, so no caller spells the state path.

    `eval run -C <ws>` writes under `<ws>/seqforge/eval/`, and only `workspace.py` is allowed to know
    that. A caller that reconstructs the path by hand is a second owner of the name: the CI workflow
    did exactly that, and a rename of the directory would have left it reading a file that no longer
    existed while still exiting 0. Both spellings resolve to the same report.
    """
    from typer.testing import CliRunner

    from seqforge.cli import app
    from seqforge.workspace import eval_dir

    runner = CliRunner()
    ws = tmp_path / "ws"
    written = runner.invoke(
        app, ["eval", "run", "--no-llm", "--cases", str(default_cases_dir()), "-C", str(ws)]
    )
    assert written.exit_code in (0, 3), written.output
    run_dir = eval_dir(ws)
    assert (run_dir / "report.json").is_file(), "the run directory holds the report"

    from_workspace = runner.invoke(
        app, ["eval", "report", str(ws), "-o", str(tmp_path / "a.html"), "--no-timestamp"]
    )
    from_run_dir = runner.invoke(
        app, ["eval", "report", str(run_dir), "-o", str(tmp_path / "b.html"), "--no-timestamp"]
    )
    assert from_workspace.exit_code == 0, from_workspace.output
    assert from_run_dir.exit_code == 0, from_run_dir.output
    assert (tmp_path / "a.html").read_text() == (tmp_path / "b.html").read_text()
