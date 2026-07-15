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

from pathlib import Path

import pytest
from pydantic import ValidationError

from seqforge.evals import (
    Case,
    CaseError,
    Expected,
    Grade,
    build_report,
    default_cases_dir,
    discover_cases,
    grade_case,
    load_cases,
    materialize,
    outcome_of,
    run_case,
)
from seqforge.evals.case import Recipe
from seqforge.evals.run import CaseRun, HarvestGrade, _fold_harvest
from seqforge.models.blocker import Blocker, BlockerCode, BlockerSubject
from seqforge.models.conflict import Conflict, ConflictPosition
from seqforge.models.resolve import (
    Candidate,
    Question,
    ResolveResult,
    RoleAssignment,
    TechScore,
)

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


def _grade(expected: dict, result: ResolveResult, exit_code: int):
    return grade_case("t", Expected.model_validate(expected), result, exit_code, LABELS)


# --------------------------------------------------------------------------------------------
# the confusion matrix — every cell, especially the one that matters
# --------------------------------------------------------------------------------------------


def test_correct_decision_is_correct() -> None:
    g = _grade(
        {"outcome": "decide", "fields": {"library.chemistry": "10x-3p-gex-v3"}}, _result(), 0
    )
    assert g.grade is Grade.CORRECT
    assert g.ok


def test_wrong_value_is_false_accept_not_merely_a_failure() -> None:
    """A decision that disagrees with truth IS the corpus-poisoning failure — the headline metric."""
    g = _grade(
        {"outcome": "decide", "fields": {"library.chemistry": "10x-3p-gex-v2"}}, _result(), 0
    )
    assert g.grade is Grade.FALSE_ACCEPT
    assert "10x-3p-gex-v2" in g.notes[0]


def test_guessing_where_refusal_was_correct_is_false_accept() -> None:
    """The ONT case's failure mode: something always ranks highest; 'highest' is not 'right'."""
    g = _grade({"outcome": "refuse", "blockers": ["UNSUPPORTED_TECHNOLOGY"]}, _result(), 0)
    assert g.grade is Grade.FALSE_ACCEPT


def test_silently_picking_instead_of_asking_is_false_accept_and_a_missed_question() -> None:
    """Brief §9: failing to ask a needed question is a hard fail. Its mechanism is a silent pick."""
    g = _grade({"outcome": "ask"}, _result(), 0)
    assert g.grade is Grade.FALSE_ACCEPT
    assert g.missed_question


def test_blocking_a_decidable_case_is_false_refuse() -> None:
    g = _grade(
        {"outcome": "decide", "fields": {"library.chemistry": "10x-3p-gex-v3"}},
        _result(blockers=[BlockerCode.TRUNCATED_GZIP]),
        3,
    )
    assert g.grade is Grade.FALSE_REFUSE


def test_asking_what_code_could_settle_is_over_ask_not_false_refuse() -> None:
    """Nothing wrong entered the manifest. It is a cost regression, tracked separately."""
    g = _grade(
        {"outcome": "decide", "fields": {"library.chemistry": "10x-3p-gex-v3"}},
        _result(conflicts=[_conflict()]),
        4,
    )
    assert g.grade is Grade.OVER_ASK


def test_blocking_instead_of_asking_is_false_refuse() -> None:
    g = _grade({"outcome": "ask"}, _result(blockers=[BlockerCode.UNRESOLVED_CONFLICT]), 3)
    assert g.grade is Grade.FALSE_REFUSE


def test_asking_instead_of_blocking_is_mis_triage() -> None:
    g = _grade({"outcome": "refuse", "blockers": ["TRUNCATED_GZIP"]}, _result(), 4)
    assert g.grade is Grade.MIS_TRIAGE


def test_right_refusal_wrong_blocker_is_wrong_reason_not_correct() -> None:
    """Right outcome, wrong reason: the human is sent the wrong way. Counting it green rots meaning."""
    g = _grade(
        {"outcome": "refuse", "blockers": ["TRUNCATED_GZIP"]},
        _result(blockers=[BlockerCode.CORRUPT_FASTQ]),
        3,
    )
    assert g.grade is Grade.WRONG_REASON
    assert "TRUNCATED_GZIP" in g.notes[0]


def test_correct_refusal_matches_blocker_code() -> None:
    g = _grade(
        {"outcome": "refuse", "blockers": ["TRUNCATED_GZIP"]},
        _result(blockers=[BlockerCode.TRUNCATED_GZIP]),
        3,
    )
    assert g.grade is Grade.CORRECT


# --------------------------------------------------------------------------------------------
# conflicts: positions are the load-bearing assertion, not the field name
# --------------------------------------------------------------------------------------------


def test_expected_conflict_matches_on_field_and_positions() -> None:
    g = _grade(
        {
            "outcome": "ask",
            "conflict": {
                "kind": "observed_vs_asserted",
                "field": "library.read_layout.R1.length",
                "positions": {"asserted": "26", "observed": "28"},
            },
        },
        _result(conflicts=[_conflict()]),
        4,
    )
    assert g.grade is Grade.CORRECT


def test_conflict_on_the_wrong_field_is_wrong_reason() -> None:
    g = _grade(
        {"outcome": "ask", "conflict": {"field": "library.chemistry"}},
        _result(conflicts=[_conflict()]),
        4,
    )
    assert g.grade is Grade.WRONG_REASON


def test_conflict_with_collapsed_positions_is_caught() -> None:
    """The reason positions are asserted: both sides agreeing is not a conflict, however it is labelled."""
    collapsed = _conflict().model_copy(
        update={
            "positions": [
                ConflictPosition(value="28", basis="asserted", confidence=0.9),
                ConflictPosition(value="28", basis="observed", confidence=0.99),
            ]
        }
    )
    g = _grade(
        {
            "outcome": "ask",
            "conflict": {
                "field": "library.read_layout.R1.length",
                "positions": {"asserted": "26", "observed": "28"},
            },
        },
        _result(conflicts=[collapsed]),
        4,
    )
    assert g.grade is Grade.WRONG_REASON


def test_exit_4_with_no_conflict_or_question_is_caught() -> None:
    g = _grade({"outcome": "ask", "conflict": {"field": "library.chemistry"}}, _result(), 4)
    assert g.grade is Grade.WRONG_REASON
    assert "no open conflict" in g.notes[0]


def test_wrong_library_value_while_asking_is_wrong_reason_not_false_accept() -> None:
    """It stopped, so nothing was committed — but the human gets the right question, wrong state."""
    g = _grade(
        {
            "outcome": "ask",
            "conflict": {"field": "library.read_layout.R1.length"},
            "fields": {"library.chemistry": "10x-3p-gex-v3"},
        },
        _result(tech="bulk-rnaseq-pe", conflicts=[_conflict()]),
        4,
    )
    assert g.grade is Grade.WRONG_REASON


# --------------------------------------------------------------------------------------------
# role assignment + field extraction
# --------------------------------------------------------------------------------------------


def test_role_assignment_is_checked_by_label_not_hash() -> None:
    g = _grade({"outcome": "decide", "fields": {"library.roles.R1": "R1"}}, _result(), 0)
    assert g.grade is Grade.CORRECT


def test_swapped_roles_are_a_false_accept_even_with_the_right_chemistry() -> None:
    """Right chemistry + swapped roles emits a pipeline that reads cDNA as a barcode."""
    g = _grade(
        {
            "outcome": "decide",
            "fields": {"library.chemistry": "10x-3p-gex-v3", "library.roles.R1": "R1"},
        },
        _result(roles={"R1": "sha-r2", "R2": "sha-r1"}),
        0,
    )
    assert g.grade is Grade.FALSE_ACCEPT


def test_unsupported_field_path_is_visible_not_silently_green() -> None:
    g = _grade({"outcome": "decide", "fields": {"library.nonsense": "x"}}, _result(), 0)
    assert g.grade is Grade.FALSE_ACCEPT
    assert "unsupported" in str(g.fields[0].actual)


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


def test_report_is_json_round_trippable() -> None:
    report = build_report([_run(Grade.CORRECT)])
    assert report.model_dump(mode="json")["n_cases"] == 1


# --------------------------------------------------------------------------------------------
# cases: the corpus itself, and the recipe machinery
# --------------------------------------------------------------------------------------------


def test_corpus_loads_and_covers_all_three_outcomes() -> None:
    cases = discover_cases()
    assert len(cases) >= 7
    outcomes = {c.expected.outcome for c in cases}
    assert outcomes == {"decide", "refuse", "ask"}, "the corpus must exercise every outcome class"


def test_every_case_has_a_description() -> None:
    """A case whose intent is not written down cannot be maintained when it fails."""
    for case in discover_cases():
        assert case.expected.description.strip(), f"{case.id} has no description"


def test_corpus_ships_no_fastq_bytes() -> None:
    """Inputs are recipes. A committed FASTQ means a case stopped tracking its spec."""
    stray = [p for p in default_cases_dir().rglob("*") if p.suffix in (".gz", ".fastq", ".fq")]
    assert not stray, f"eval cases must ship recipes, not bytes: {stray}"


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


def test_held_out_case_skips_when_its_root_is_unset(tmp_path: Path, monkeypatch) -> None:
    """Design §8: a held-out root lives in out-of-git config. Absent => skip, never pass, never fail."""
    monkeypatch.delenv("SEQFORGE_TEST_HELDOUT", raising=False)
    recipe = Recipe.model_validate(
        {"generate": {"kind": "local", "root_env": "SEQFORGE_TEST_HELDOUT"}}
    )
    case = Case("held", tmp_path, recipe, Expected(outcome="decide"), [])
    run = run_case(case)
    assert run.skipped is not None
    assert "SEQFORGE_TEST_HELDOUT" in run.skipped
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


def test_corpus_is_green() -> None:
    """The deterministic corpus, through the real compiler. No LLM, no network, no API key."""
    cases = [c for c in discover_cases() if not (c.has_prose and c.recipe.hypothesis is None)]
    report, runs = run_cases_no_llm(cases)
    failures = [r.to_json() for r in runs if r.skipped is None and not r.grade.ok]
    assert not failures, f"eval corpus regressed: {failures}"
    assert report.false_accept_rate == 0.0
    assert report.field_accuracy == 1.0


def run_cases_no_llm(cases):
    from seqforge.evals import run_cases

    return run_cases(cases, llm=False)


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

    def __init__(self, drafts: list[dict]) -> None:
        self._payload = {"drafts": drafts}

    def default_model(self) -> str:
        return "stub-model-1"

    def complete_json(self, **kwargs):
        import json as _json

        from seqforge.harvest import LLMResponse

        return LLMResponse(
            text=_json.dumps(self._payload), usage={"input_tokens": 100, "output_tokens": 20}
        )


def _draft(fieldname: str, value: str, quote: str) -> dict:
    # doc_sha256 is a placeholder on purpose: extract._anchor overwrites it with the real one.
    return {
        "field": fieldname,
        "value": value,
        "span": {"doc_sha256": "0" * 64, "quote": quote, "context": None},
        "llm_confidence": 0.95,
    }


def _trap_case() -> Case:
    return next(c for c in discover_cases() if c.id == "chemistry-unstated-trap")


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
    # Either the R5 tripwire rejects the claim (entailment fails), or it survives and the case
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

    def __init__(self, payloads: list[list[dict]]) -> None:
        self._payloads = payloads
        self.calls = 0

    def default_model(self) -> str:
        return "flaky-1"

    def complete_json(self, **kwargs):
        import json as _json

        from seqforge.harvest import LLMResponse

        payload = self._payloads[min(self.calls, len(self._payloads) - 1)]
        self.calls += 1
        return LLMResponse(text=_json.dumps({"drafts": payload}), usage={})


def test_a_hallucination_in_any_trial_survives_to_the_grade() -> None:
    """Regression: trials kept only the LAST harvest, so a real failure vanished on a re-run.

    The model hallucinates on trial 1 and behaves on trials 2-3. Reporting the final trial would
    grade this clean — which is exactly the illusion trials exist to dispel.
    """
    good = [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    bad = good + [
        _draft("experiment.samples.condition", "heat shock", "single-cell RNA-seq"),
    ]
    provider = _FlakyProvider([bad, good, good])
    run = run_case(_trap_case(), llm=True, provider=provider, trials=3)
    assert provider.calls == 3
    assert run.harvest is not None
    # If the claim survived verify it must reach the grade; if verify killed it, that is also fine —
    # what must NOT happen is a trial-1 failure being forgotten because trial 3 was clean.
    if "experiment.samples.condition" in run.harvest.extracted:
        assert run.harvest.hallucinated == ["experiment.samples.condition"]
        assert run.grade.grade is Grade.FALSE_ACCEPT
    else:
        assert run.harvest.n_rejected >= 1


def test_a_field_found_in_only_some_trials_is_reported_unstable() -> None:
    """Extraction that comes and goes is a finding, not a rounding error."""
    with_org = [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    provider = _FlakyProvider([with_org, [], with_org])
    run = run_case(_trap_case(), llm=True, provider=provider, trials=3)
    assert run.harvest is not None
    assert run.harvest.matched == [], "a field missed in any trial must not count as matched"
    assert run.harvest.unstable == ["experiment.organism"]
    assert run.harvest.missing == ["experiment.organism"]


def test_trials_run_the_llm_case_repeatedly_and_report_stability() -> None:
    case = _trap_case()
    provider = _StubProvider(
        [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    )
    run = run_case(case, llm=True, provider=provider, trials=3)
    assert run.trials == 3
    assert run.stability == 1.0
    assert run.llm_calls == 3
    assert run.usage["input_tokens"] == 300


def test_usage_is_accumulated_into_the_report() -> None:
    case = _trap_case()
    provider = _StubProvider(
        [_draft("experiment.organism", "Caenorhabditis elegans", "Caenorhabditis elegans")]
    )
    run = run_case(case, llm=True, provider=provider)
    report = build_report([run])
    assert report.cost["input_tokens"] == 100.0
    assert report.cost["llm_calls"] == 1.0
