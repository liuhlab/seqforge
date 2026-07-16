"""Grading — turn (expected, actual) into the metrics brief §9 actually asks for.

The whole harness reduces to one asymmetry: **not all failures cost the same.** A refusal is cheap —
a human looks at it. A confidently wrong manifest is expensive — it silently poisons a training
corpus and nothing downstream ever asks again. So the outcome classes are not a pass/fail bit but a
3x3 confusion, and one cell is singled out.

Actual outcome comes from the uniform exit contract, never re-derived: ``0`` decide, ``3`` refuse,
``4`` ask.

======================  ==============  ==============  ==============
expected \\ actual       decide          refuse          ask
======================  ==============  ==============  ==============
decide (values match)   correct         false_refuse    over_ask
decide (values differ)  FALSE_ACCEPT    false_refuse    over_ask
refuse                  FALSE_ACCEPT    correct*        mis_triage
ask                     FALSE_ACCEPT    false_refuse    correct*
======================  ==============  ==============  ==============

``*`` right outcome, wrong reason (wrong BlockerCode / wrong conflict) grades ``wrong_reason``: it is
not a false-accept — the pipeline still stopped — but it is not correct either, because the human
gets sent the wrong way. Counting it as a pass would let a blocker's *meaning* rot untested.

Three cells deserve their reasoning stated, because each is a judgement call:

- **expected=ask, actual=decide is a false_accept, not a separate "missed question".** The brief calls
  failing to ask a needed question a hard fail. Its *mechanism* is a silent pick, which is exactly
  what false-accept measures. It is additionally flagged ``missed_question`` for the report.
- **expected=decide, actual=ask is over_ask, not a false-refuse.** Nothing wrong entered the manifest;
  a human was asked a question code could have settled. That is a cost regression, tracked under
  "questions asked (fewer is better)" — not a correctness failure.
- **expected=ask, actual=refuse is false_refuse.** It stopped without giving the human a decidable
  question. The effect on throughput is a block, so it is counted as one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..models.resolve import MetadataResolution, ResolveResult


class Grade(StrEnum):
    """How a case came out. ``FALSE_ACCEPT`` is the metric that matters (brief §9)."""

    CORRECT = "correct"
    #: Produced a decision that is wrong, or produced one at all when it should have stopped.
    FALSE_ACCEPT = "false_accept"
    #: Blocked on something it should have decided or asked about.
    FALSE_REFUSE = "false_refuse"
    #: Asked a human what code could have settled. A cost regression, not a correctness one.
    OVER_ASK = "over_ask"
    #: Right outcome class, wrong reason: wrong BlockerCode or wrong conflict.
    WRONG_REASON = "wrong_reason"
    #: Refused when it should have asked, or vice versa — both stop, but send the human elsewhere.
    MIS_TRIAGE = "mis_triage"


def outcome_of(exit_code: int) -> str:
    """Map the uniform exit contract onto an outcome class. Never re-derived from the result body."""
    return {0: "decide", 3: "refuse", 4: "ask"}.get(exit_code, "error")


@dataclass
class FieldCheck:
    path: str
    expected: Any
    actual: Any
    ok: bool


@dataclass
class CaseGrade:
    """One case's verdict: the grade, why, and the field-level detail behind it."""

    case_id: str
    grade: Grade
    expected_outcome: str
    actual_outcome: str
    fields: list[FieldCheck] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    missed_question: bool = False

    @property
    def ok(self) -> bool:
        return self.grade is Grade.CORRECT

    def to_json(self) -> dict[str, Any]:
        return {
            "case": self.case_id,
            "grade": self.grade.value,
            "expected": self.expected_outcome,
            "actual": self.actual_outcome,
            "fields": [
                {"path": f.path, "expected": f.expected, "actual": f.actual, "ok": f.ok}
                for f in self.fields
            ],
            "notes": self.notes,
            "missed_question": self.missed_question,
        }


def grade_case(
    case_id: str,
    expected: Any,
    result: ResolveResult,
    exit_code: int,
    labels: dict[str, str],
    metadata: MetadataResolution | None = None,
) -> CaseGrade:
    """Grade one resolve outcome against a case's ``expected.yaml``.

    ``labels`` maps file sha256 -> a stable label (``R1``/``R2``) so role assertions can be written
    against the recipe's read ids rather than machine-dependent hashes.

    ``metadata`` is the second resolver's answer, and until it existed the harness could not see a
    sample at all: it graded a ``ResolveResult``, which has candidates and conflicts and no samples,
    so every sample-level claim in a pre-registration was un-checkable prose. design.md §9 named
    that gap ("the grader cannot express … SRX→sample mapping") and it stayed named for as long as
    nothing produced samples to grade.
    """
    actual = outcome_of(exit_code)
    exp = expected.outcome
    notes: list[str] = []

    if actual == "error":
        return CaseGrade(case_id, Grade.FALSE_REFUSE, exp, actual, notes=[f"exit {exit_code}"])

    # Field checks run on `ask` too, not just `decide`: design §3.5 says the library section takes the
    # observed value while the conflict stays attached. So "what did the library land on" is a real
    # assertion even when the case correctly stops to ask, and skipping it would leave the value
    # untested on exactly the path where metadata is known to be lying.
    checks = (
        _check_fields(expected.fields, result, labels, metadata)
        if actual in ("decide", "ask")
        else []
    )

    if exp == "decide":
        if actual == "decide":
            bad = [c for c in checks if not c.ok]
            if bad:
                notes += [f"{c.path}: expected {c.expected!r}, got {c.actual!r}" for c in bad]
                # A decision that disagrees with ground truth IS the corpus-poisoning failure.
                return CaseGrade(case_id, Grade.FALSE_ACCEPT, exp, actual, checks, notes)
            return CaseGrade(case_id, Grade.CORRECT, exp, actual, checks, notes)
        if actual == "refuse":
            notes.append(f"blocked: {_codes(result)}")
            return CaseGrade(case_id, Grade.FALSE_REFUSE, exp, actual, checks, notes)
        notes.append("asked a question code should have settled")
        return CaseGrade(case_id, Grade.OVER_ASK, exp, actual, checks, notes)

    if exp == "refuse":
        if actual == "decide":
            notes.append(f"guessed {_tech(result)!r} where refusal was correct")
            return CaseGrade(case_id, Grade.FALSE_ACCEPT, exp, actual, checks, notes)
        if actual == "ask":
            notes.append("asked instead of blocking")
            return CaseGrade(case_id, Grade.MIS_TRIAGE, exp, actual, checks, notes)
        got = _codes(result)
        missing = [c for c in expected.blockers if c not in got]
        if missing:
            notes.append(f"expected blocker(s) {missing}, got {sorted(got)}")
            return CaseGrade(case_id, Grade.WRONG_REASON, exp, actual, checks, notes)
        return CaseGrade(case_id, Grade.CORRECT, exp, actual, checks, notes)

    # exp == "ask"
    if actual == "decide":
        notes.append(f"silently picked {_tech(result)!r} instead of surfacing a question")
        return CaseGrade(
            case_id, Grade.FALSE_ACCEPT, exp, actual, checks, notes, missed_question=True
        )
    if actual == "refuse":
        notes.append(f"blocked ({_codes(result)}) instead of asking an answerable question")
        return CaseGrade(case_id, Grade.FALSE_REFUSE, exp, actual, checks, notes)
    if expected.conflict is not None:
        ok, why = _check_conflict(expected.conflict, result)
        if not ok:
            notes.append(why)
            return CaseGrade(case_id, Grade.WRONG_REASON, exp, actual, checks, notes)
    bad = [c for c in checks if not c.ok]
    if bad:
        # It stopped to ask (so: not a false accept — nothing was silently committed), but the value
        # the library landed on is wrong. The human gets the right question about the wrong state.
        notes += [f"{c.path}: expected {c.expected!r}, got {c.actual!r}" for c in bad]
        return CaseGrade(case_id, Grade.WRONG_REASON, exp, actual, checks, notes)
    return CaseGrade(case_id, Grade.CORRECT, exp, actual, checks, notes)


def _check_conflict(want: Any, result: ResolveResult) -> tuple[bool, str]:
    open_conflicts = [c for c in result.conflicts if c.status == "open"]
    if not open_conflicts and not result.questions:
        return False, "exit 4 but no open conflict or question"
    kinds = {c.kind for c in open_conflicts}
    if want.kind and open_conflicts and want.kind not in kinds:
        return False, f"expected conflict kind {want.kind!r}, got {sorted(kinds)}"
    if want.field:
        seen = {c.field for c in open_conflicts} | {q.field for q in result.questions}
        if want.field not in seen:
            return False, f"expected conflict on {want.field!r}, got {sorted(x for x in seen if x)}"
    if want.positions:
        matching = [c for c in open_conflicts if not want.field or c.field == want.field]
        got = {p.basis: p.value for c in matching for p in c.positions}
        if got != want.positions:
            return False, f"expected positions {want.positions}, got {got}"
    return True, ""


def _check_fields(
    want: dict[str, Any],
    result: ResolveResult,
    labels: dict[str, str],
    metadata: MetadataResolution | None = None,
) -> list[FieldCheck]:
    checks: list[FieldCheck] = []
    top = result.candidates[0] if result.candidates else None
    for path, expected in sorted(want.items()):
        actual = _extract_field(path, result, top, labels, metadata)
        checks.append(FieldCheck(path, expected, actual, _equal(expected, actual)))
    return checks


def _extract_field(
    path: str,
    result: ResolveResult,
    top: Any,
    labels: dict[str, str],
    metadata: MetadataResolution | None = None,
) -> Any:
    if path.startswith("experiment."):
        return _extract_experiment_field(path, metadata)
    if top is None:
        return None
    if path == "library.chemistry":
        return top.technology
    if path == "library.equivalence_members":
        return sorted(top.equivalence_members)
    if path == "rung":
        return result.rung_reached
    if path.startswith("library.roles."):
        role = path.split(".", 2)[2]
        sha = top.role_assignment.assignment.get(role)
        return labels.get(sha or "", sha)
    return f"<unsupported field {path}>"


def _extract_experiment_field(path: str, metadata: MetadataResolution | None) -> Any:
    """The sample-level half of the grading surface. ``None`` when the resolver said nothing.

    ``None`` rather than an ``<unsupported field>`` sentinel, and the difference is the whole reason
    this exists: a pre-registration saying `tissue: Neurons` must FAIL against a manifest that says
    null. A sentinel string would also fail, but it would fail for the wrong reason and would keep
    failing after the bug was fixed.

    Two shapes, both dotted:

    - ``experiment.samples.<sample_id>.<attr>`` — one sample's attribute. The sample id is the
      archive's accession when a record was joined, so this is a claim about a specific BioSample.
    - ``experiment.samples.*.<attr>`` — the attribute across EVERY sample, sorted. This is what a
      pre-registration usually wants: "tissue=Neurons" was never a claim about one of the six.
    - ``experiment.organism`` / ``experiment.study.<field>``.
    """
    if metadata is None:
        return None
    if path == "experiment.organism":
        return metadata.organism.value if metadata.organism is not None else None
    if path.startswith("experiment.study."):
        field = path.split(".", 2)[2]
        return getattr(metadata.project, field, None) if metadata.project else None
    if path.startswith("experiment.samples."):
        rest = path[len("experiment.samples.") :]
        sample_id, _, attr = rest.rpartition(".")
        if not sample_id:  # `experiment.samples.tissue` with no subject names no sample
            return f"<unsupported field {path}: name a sample id, or '*' for all of them>"
        if sample_id == "*":
            return sorted(
                s.attributes[attr].value for s in metadata.samples if attr in s.attributes
            )
        for sample in metadata.samples:
            if sample.sample_id == sample_id:
                found = sample.attributes.get(attr)
                return found.value if found is not None else None
        return None
    return f"<unsupported field {path}>"


def _equal(expected: Any, actual: Any) -> bool:
    if isinstance(expected, list) and isinstance(actual, list):
        return sorted(map(str, expected)) == sorted(map(str, actual))
    return bool(expected == actual)


def _codes(result: ResolveResult) -> list[str]:
    return sorted(str(getattr(b.code, "value", b.code)) for b in result.blockers)


def _tech(result: ResolveResult) -> str | None:
    return result.candidates[0].technology if result.candidates else None
