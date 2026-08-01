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
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

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
from seqforge.evals.case import Recipe
from seqforge.evals.run import CaseRun, HarvestGrade, _fold_harvest, _merge_harvest
from seqforge.harvest import EXTRACT_PROMPT_VERSION
from seqforge.models.blocker import Blocker, BlockerCode, BlockerSubject
from seqforge.models.conflict import Conflict, ConflictPosition
from seqforge.models.resolve import (
    Candidate,
    Question,
    ResolveResult,
    RoleAssignment,
    TechScore,
)

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
       local data). Grouping is a filing decision, but pinning it means a stray case dropped at the
       top level, or a sixth ad-hoc group, turns red instead of quietly re-messing the directory.
    3. Every case says what it is for. A case whose intent is not written down cannot be maintained
       when it fails.
    4. No case ships FASTQ bytes. Inputs are recipes; a committed FASTQ means a case stopped
       tracking its spec.
    """
    base = default_cases_dir()
    cases = discover_cases()
    groups = {"spec", "prose", "steering", "refusal", "real"}

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
    r1 = next(p for p in built.paths if p.name.startswith("R1"))
    r2 = next(p for p in built.paths if p.name.startswith("R2"))
    assert r1.stat().st_size < r2.stat().st_size


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
            text=_json.dumps(self._payload), usage={"input_tokens": 100, "output_tokens": 20}
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
    """Defence in depth: a quote that is not in the document dies at verify, before grading."""
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
    assert "library.chemistry" not in run.harvest.extracted


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
        p = src / f"{rid}.fastq.gz"
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
# `seqforge eval report` — the HTML renderer
#
# The renderer exists because the benchmark job produced a number and threw the detail away: a green
# exit code told you nothing about WHICH case carried the risk. So these tests are less about markup
# than about the claims the page is allowed to make. Breaking one of them means the report has started
# lying in the specific way a grading dashboard usually lies.
# --------------------------------------------------------------------------------------------------


#: Every branch the renderer has, in one report: a false accept with a wrong scalar AND a wrong
#: multiset, each of the other four failure grades, a correct case, a skip, harvest (including a
#: hallucination), multiple trials, and both clocks. Built fresh per call because tests mutate it.
def _render_fixture() -> dict[str, Any]:
    return {
        "n_cases": 6,
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
                    "extracted": {"experiment.organism": "Caenorhabditis elegans"},
                },
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
                "case": "absent-one",
                "seconds": 0.19,
                "llm_calls": 0,
                "skipped": "package unreachable",
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
    assert "absent-one" in page, "a skip is not a silent omission"
    assert "package unreachable" in page, "and it carries WHY"
    assert "excluded from every rate" in page, "a skip is never a pass"
    assert "1 skipped" in page, "and the count is on the summary tile"

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
            "absent-one",  # skipped — a state, not a grade, so it sorts last
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
    failure. Tokens and LLM calls are what a `--llm` pass costs.
    """
    page = render_html(_render_fixture(), title="T", source=None)
    assert "1/3</b> trials correct" in page, "stability is reported as a fraction of trials"
    assert "hallucinated" in page and "experiment.samples.tissue" in page
    assert "nothing downstream would catch these" in page, "a hallucination is named as poison"
    assert "unstable" in page and "experiment.samples.dev_stage" in page
    assert "the safety net working, not a failure" in page, "n_rejected is not a failure count"
    assert "188,423 tokens" in page, "the token bill"
    assert "missed a question it had to ask" in page, "questions_asked.missed reaches the case"


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
    assert payload["skipped"] == 1
    assert out.exists() and payload["bytes"] == len(out.read_text().encode())
    assert out.read_text().startswith("<!doctype html>")

    # Not JSON, and not an eval report: both are the caller's error, and neither writes a page.
    (tmp_path / "junk.json").write_text("not json at all")
    bad = CliRunner().invoke(app, ["eval", "report", str(tmp_path / "junk.json")])
    assert bad.exit_code == 1
    assert not (tmp_path / "junk.html").exists()


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
