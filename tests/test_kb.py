"""Tests for the KB: schema validation, the DSL guards, and the round-trip self-test."""

from __future__ import annotations

import gzip
import random
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from conftest import KbProbes, registry_for, write_fastq_gz
from seqforge import kb
from seqforge.io import OnlistRegistry, revcomp
from seqforge.kb.schema import Identity, MotifPresent, Read, Spec
from seqforge.models.observation import ConstantSegment
from seqforge.probe import probe_file
from seqforge.resolve.evaluators import Evaluation, Outcome, evaluate
from seqforge.resolve.window import WindowProbe


def test_10x_spec_loads_and_validates() -> None:
    spec = kb.load_spec("10x-3p-gex-v3")
    assert spec.identity.id == "10x-3p-gex-v3"
    assert {r.id for r in spec.reads} == {"R1", "R2"}
    assert spec.require_backend().params["soloCBlen"] == 16
    assert spec.decidable_by  # non-empty: it has processing-divergent confusables


# `test_all_shipped_specs_validate` was deleted (#110): `test_every_kb_spec_roundtrips[<spec>]` collects
# from the same `kb.list_spec_ids()` and calls `run_roundtrip`, which loads (and therefore validates)
# each spec and reddens on an empty-`reads` spec via `assert result["checks"]`. That `10x-3p-gex-v3`
# loads and validates is pinned directly by `test_10x_spec_loads_and_validates` above.


def test_backend_rejects_illegal_template_token() -> None:
    data = kb.load_spec("10x-3p-gex-v3").model_dump()
    data["backend"]["params"]["soloCBwhitelist"] = "{secret:leak}"  # not an {onlist:...} token
    with pytest.raises(ValidationError):
        Spec.model_validate(data)


def test_divergent_confusable_cannot_be_none() -> None:
    data = kb.load_spec("10x-3p-gex-v3").model_dump()
    data["confusable_with"][1]["distinguishable_by"] = ["none"]  # index 1 is the divergent Multiome
    with pytest.raises(ValidationError):
        Spec.model_validate(data)


def test_linker_element_requires_a_sequence() -> None:
    data = kb.load_spec("10x-3p-gex-v3").model_dump()
    data["reads"][0]["elements"].append(
        {"type": "linker", "name": "bad", "start": 28, "end": 30, "seqspec_region_type": "linker"}
    )
    with pytest.raises(ValidationError):
        Spec.model_validate(data)


@pytest.mark.parametrize("delta", [1, -1], ids=["window_wider", "window_narrower"])
def test_an_element_whose_sequence_contradicts_its_own_window_fails_at_load(delta: int) -> None:
    """A declared width that lies shifts every element after it, and surfaces far from its cause.

    The round-trip's C1 constant-element check is circular with respect to *substitution* — the
    generator writes out the same string C1 reads back, so a wrongly published base is green there and
    only ever catchable on real reads (#285). What C1 genuinely stands for is the other derivation,
    **where** the element goes: the generator concatenates elements in order, C1 reads the window back
    off `[start, end)`. So a `sequence` one base wider or narrower than its own window re-frames
    everything downstream and reddens C1 on some *later* element — and only on an entry that has a
    fixture to run the round-trip against at all. The precondition belongs where the element is first
    addressable, three derivations earlier.

    Both directions, because they are not the same edit: a wide literal and a wide window are the two
    ways one clause can be written to catch only half of what it names.
    """
    data = kb.load_spec("splitseq").model_dump()
    elements = next(r for r in data["reads"] if r["id"] == "bc")["elements"]
    linker = next(e for e in elements if e["name"] == "linker1")
    assert len(linker["sequence"]) == linker["end"] - linker["start"], "the shipped entry agrees"

    linker["end"] += delta
    with pytest.raises(ValidationError, match="linker1") as exc:
        Spec.model_validate(data)
    message = str(exc.value)
    assert "30" in message and str(30 + delta) in message, "both widths, so the fix is one edit"


def test_the_width_clause_is_not_decoration_the_shipped_kb_exercises_it() -> None:
    """The positive control: shipped elements carry BOTH a literal and a fixed window, and agree.

    A guard nothing reaches is indistinguishable from a green suite, and this one is a *pure* guard —
    it was measured to land on a clean KB, not to force a migration. The other direction matters as
    much: BD Rhapsody Enhanced's four linkers carry a literal and NO window (they float behind a
    variable-length diversity insert), so the clause must stay conditioned on all three being present
    or every anchored element in the KB would be refused for declaring a width it never claimed.

    `>=`, not `==`. Seven was the count the clause shipped against — evidence that it lands green, not
    an invariant — and an entry that adds an eighth would redden an equality for GROWING the KB rather
    than for lying about a width. What has to hold as the KB grows is the per-element agreement in the
    loop, and that a literal reaches the clause at all.
    """
    both, anchored = 0, 0
    for tech in kb.list_spec_ids():
        for read in kb.load_spec(tech).reads:
            for el in read.elements:
                if el.sequence is None:
                    continue
                if el.start is not None and el.end is not None:
                    both += 1
                    assert len(el.sequence) == el.end - el.start, f"{tech}/{read.id}/{el.name}"
                else:
                    anchored += 1
    assert both >= 7, "7 shipped elements reached this clause when it landed; a KB only grows"
    assert anchored, "and at least one literal with no window, which the clause must not touch"


# ---------- read sets: a subset of declared ids, checked where every other DSL typo is ----------
def test_bulk_declares_a_single_end_read_set() -> None:
    """The chemistry the read-set feature exists for: one entry, two sequencing configurations.

    A single-end bulk RNA-seq FASTQ used to be `Blocker(UNSUPPORTED_TECHNOLOGY)` at exit 3, because
    the entry declared two reads and the role assignment is injective AND total. The set is a subset
    of ids, never a re-declaration, so R1's element coordinates exist exactly once and the two
    configurations cannot drift apart.
    """
    spec = kb.load_spec("bulk-rnaseq")
    assert spec.read_sets == {"se": ["R1"]}
    assert spec.read_set_names() == ["full", "se"], "maximal first, then the declared subsets"
    assert [r.id for r in spec.reads_in("full")] == ["R1", "R2"]
    assert [r.id for r in spec.reads_in("se")] == ["R1"]


def test_a_misspelled_read_set_name_fails_at_load() -> None:
    """The keys are a CLOSED vocabulary, so a typo dies where every other DSL typo dies.

    `single_end:` instead of `se:` would otherwise be a read set nothing ever selects — a spec that
    silently declares one configuration while reading as though it declared two. The vocabulary is
    extended deliberately, exactly as an `ElementType` is.
    """
    data = kb.load_spec("bulk-rnaseq").model_dump()
    data["read_sets"] = {"single_end": ["R1"]}
    with pytest.raises(ValidationError):
        Spec.model_validate(data)


def test_the_maximal_read_sets_name_is_reserved_and_cannot_be_declared() -> None:
    """`full` names `reads` itself, so declaring it would be a second declaration of the same set.

    It is reserved by being absent from the vocabulary: the same mechanism that rejects a misspelling
    rejects the one name a spec may never bind.
    """
    data = kb.load_spec("bulk-rnaseq").model_dump()
    data["read_sets"] = {"full": ["R1", "R2"]}
    with pytest.raises(ValidationError):
        Spec.model_validate(data)


def test_a_read_set_naming_a_read_the_spec_does_not_declare_fails_at_load() -> None:
    """A read set is a SUBSET of declared ids. A dangling id is a dangling name like any other.

    `Spec._cross_refs` already refuses a signature test naming an unknown read; a read set naming one
    would otherwise reach the scorer as a role with no `Read` behind it — a KeyError at scoring time
    instead of a load-time refusal.
    """
    data = kb.load_spec("bulk-rnaseq").model_dump()
    data["read_sets"] = {"se": ["R3"]}
    with pytest.raises(ValidationError, match="R3"):
        Spec.model_validate(data)


def test_a_read_set_may_not_be_empty() -> None:
    """A set with no reads has no roles, so nothing could be assigned to it and it can never win."""
    data = kb.load_spec("bulk-rnaseq").model_dump()
    data["read_sets"] = {"se": []}
    with pytest.raises(ValidationError):
        Spec.model_validate(data)


def test_a_requires_test_a_read_set_cannot_reach_fails_at_load_and_points_at_supports() -> None:
    """The universality rule, proved by construction — **no shipped spec violates it**.

    A `requires` test is a hard AND-gate; a test whose read is absent from the active set is
    *inapplicable* and enters neither the numerator nor its normalizer. So a gate addressed to a read
    only SOME sets carry is a gate that silently stops gating for the sets that lack it — the author
    almost certainly meant either a set-specific `supports`, or a read every set has.

    Two entries declare a read set and neither can fire the refusal: after `read_count` left the
    vocabulary `bulk-rnaseq`'s `requires` is empty, and `smartseq3` — the rule's first shipped instance
    — SATISFIES it, its one anchored motif gate addressing R1, the read both of its sets carry. So
    without this negative case the rule would still ship as decoration. The positive control is in the
    same test on purpose: the identical gate addressed to R1 — a read *every* set carries — must load,
    or the rule would be rejecting universal gates too and nothing would notice.
    """
    data = kb.load_spec("bulk-rnaseq").model_dump()
    assert data["read_sets"] == {"se": ["R1"]}, "the fixture is the shipped single-end set"

    data["signature"]["requires"] = [{"test": "segment_length", "read": "R2", "length": 40}]
    with pytest.raises(ValidationError, match="supports") as exc:
        Spec.model_validate(data)
    assert "'se'" in str(exc.value), "the message must name the set that cannot reach the read"

    data["signature"]["requires"] = [{"test": "segment_length", "read": "R1", "length": 40}]
    Spec.model_validate(data)  # R1 is in EVERY set — universal, so legal


@pytest.mark.parametrize("tech", kb.list_spec_ids())
def test_every_kb_spec_roundtrips(tech: str) -> None:
    """*Every* KB entry is executable and self-testing — so collect from the KB, not a list.

    This was three hardcoded ids plus a separate v3-only test, and the KB has five. The uncovered
    one was `10x-3p-gex-v3.1`, whose own spec comment says it exists because "a predicate cannot be
    computed about a spec that does not exist" — and it was the one spec this predicate was not
    computed over. The claim that "adding a technology automatically adds its own test" was false for
    exactly as long as this list was written by hand.

    Parametrizing over `list_spec_ids()` (the idiom already used twice below) is what makes the
    claim true going forward: the next spec added to the KB is round-tripped because it exists, not
    because someone remembered.
    """
    result = kb.run_roundtrip(tech, seed=0)
    assert result["passed"] is True, result
    assert result["checks"]  # non-vacuous (bulk exercises the open-ended cDNA-variable check)


def test_splitseq_recovers_fixed_linker_structure() -> None:
    # the combinatorial barcode read has TWO fixed internal linkers -> two constant segments recovered
    spec = kb.load_spec("splitseq")
    reads = kb.generate_reads(spec, n=1500, seed=0)
    import gzip as _gz
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "bc.fastq.gz"
        with _gz.open(path, "wt") as fh:
            for i, s in enumerate(reads["bc"]):
                fh.write(f"@SIM:{i}\n{s}\n+\n{'I' * len(s)}\n")
        obs = probe_file(path)
    constant_spans = [(s.start, s.end) for s in obs.segments if isinstance(s, ConstantSegment)]
    # the two 30 bp placeholder linkers at [18,48) and [56,86) come back as constant segments
    assert (18, 48) in constant_spans
    assert (56, 86) in constant_spans


# ---------- the constant sequences themselves, and the floor a motif gate declares ----------
def _constant_elements(spec: Spec) -> set[tuple[str, str]]:
    """``(read id, element name)`` for every `linker`/`fixed` element that declares a sequence.

    Derived from the elements block rather than listed, because the claim underneath is "every one of
    these is checked" — and a hand-written list is that claim with exactly the interesting half (the
    one nobody remembered) left out.
    """
    return {
        (read.id, el.name)
        for read in spec.reads
        for el in read.elements
        if el.type in ("linker", "fixed") and el.sequence is not None
    }


def _constant_checks(checks: list[dict[str, object]]) -> set[tuple[str, str]]:
    """The ``(read, element)`` pairs a round-trip result recorded a constant-sequence check for."""
    return {
        (str(c["read"]), str(c["check"]).split(":", 1)[1])
        for c in checks
        if str(c["check"]).startswith("constant_sequence:")
    }


#: The entries that declare a constant sequence at all. Parametrizing over the whole KB would add ten
#: items asserting that a spec with no linker has no linker checked — a case that cannot fail and
#: reads as coverage. That the KB declares any at all is
#: `test_a_linker_shifted_off_its_own_coordinates_reddens_the_round_trip`'s assertion, which
#: needs one to exist before it can break it.
_SPECS_DECLARING_A_CONSTANT = [
    tech for tech in kb.list_spec_ids() if _constant_elements(kb.load_spec(tech))
]


@pytest.mark.parametrize("tech", _SPECS_DECLARING_A_CONSTANT)
def test_the_round_trip_checks_every_constant_sequence_a_spec_declares(tech: str) -> None:
    """A declared linker is a claim about bytes, so the self-test has to read those bytes back (#285).

    The round-trip computed a statistic for every element and then recorded a check for barcodes with
    an onlist and for UMIs — so a `linker`/`fixed` element fell through with nothing said about it, on
    every entry in the KB. Measured before this: six checks ran for `splitseq` and **none** of them
    touched its two 30 bp linkers, the sequences its own guide holds up as that entry's whole
    discipline, and where three published sources turned out to disagree with the instrument at base 8.

    That the checks now PASS is `test_every_kb_spec_roundtrips`'s assertion, at the shipped read
    count. This one is about which checks EXIST, which does not depend on how many reads were drawn —
    so it draws the smallest sample that still writes a file. An element the round-trip cannot address
    (no fixed coordinates and no anchor of its own) reddens here rather than being skipped in silence:
    that silence is the whole defect.
    """
    spec = kb.load_spec(tech)
    declared = _constant_elements(spec)
    checked = _constant_checks(kb.roundtrip_checks(spec, n=300))
    assert checked == declared, (
        f"{tech}: the round-trip checks {sorted(checked)} but the spec declares {sorted(declared)}. "
        "A constant sequence the round-trip does not address is a sequence nothing in CI reads back."
    )


def test_a_linker_shifted_off_its_own_coordinates_reddens_the_round_trip() -> None:
    """The negative direction: the constant check must be able to fail, and this is what makes it.

    The generator writes `el.sequence` and the check reads it back, so a *substituted* base cannot
    redden anything — both halves would move together. What the check really compares is the two
    derivations of WHERE the sequence goes: the generator concatenates elements in order, and the
    check cuts the declared `[start, end)`. Sliding a window one base off its literal separates them
    while every declared width stays honest, so the schema has nothing to refuse and the failure
    reaches the round-trip, which is the whole claim C1 carries.

    This used to drop a base from the literal instead, and that route is now **closed at load**:
    `Element._addressable` refuses `len(sequence) != end - start` outright (#332), because a width that
    lies shifts every element after it and surfaces here as a mystery on some *later* element — see
    `test_an_element_whose_sequence_contradicts_its_own_window_fails_at_load`. Two derivations of one
    width are a precondition; two derivations of one position are what the round-trip is for.

    Derived from whatever the KB declares first rather than aimed at one entry: the guard is generic,
    and the demonstration should not quietly become a test of one spec. It picks a FIXED-coordinate
    element deliberately — on the anchored path the frame is found BY matching the linker, so the same
    mutation moves the window with it and the check is weaker there by construction.
    """
    fixed_first = [
        (tech, read.id, el.name)
        for tech in kb.list_spec_ids()
        for read in kb.load_spec(tech).reads
        for el in read.elements
        if el.type in ("linker", "fixed")
        and el.sequence is not None
        and el.start is not None
        and el.end is not None
    ]
    assert fixed_first, "the KB declares no fixed-coordinate constant element to demonstrate on"
    tech, read_id, el_name = fixed_first[0]

    data = kb.load_spec(tech).model_dump()
    read = next(r for r in data["reads"] if r["id"] == read_id)
    element = next(el for el in read["elements"] if el["name"] == el_name)
    # Slide the window, carrying both ends so the width never lies. Leftwards where there is room —
    # `start > 0` means a base exists before it. Otherwise rightwards, which needs a base AFTER it: a
    # later element is that base, so the window stays inside the read the generator writes. Checked
    # rather than asserted in prose, because the entry this lands on is whatever the KB declares first.
    shift = -1 if element["start"] > 0 else 1
    if shift == 1:
        assert element is not read["elements"][-1], (
            f"{tech}/{read_id}: {el_name!r} starts at 0 and ends the read — nowhere to slide it to"
        )
    element["start"] += shift
    element["end"] += shift
    broken = Spec.model_validate(data)  # the schema is happy: one position moved, no width did

    checks = kb.roundtrip_checks(broken, n=300)
    constant = [c for c in checks if str(c["check"]).startswith("constant_sequence:")]
    assert constant, f"{tech}: no constant check to fail"
    assert not all(c["ok"] for c in constant), (
        f"{tech}: sliding {el_name!r} off its coordinates left every constant check green — the check "
        "is comparing the declared sequence against itself rather than against the reads."
    )


def _motif_floor_gates(spec: Spec) -> list[MotifPresent]:
    """The `motif_present` gates this spec claims about its OWN reads, in signature order.

    `requires` and `supports` only. An `excludes` motif is an anti-gate — a claim about somebody
    else's reads — so the population that would calibrate it is not one this spec generates, and
    adding more of its own reads moves that rate in neither direction. The KB declares none today;
    one arriving needs a carrier population of its own, not this collector widened to swallow it.
    """
    return [
        t
        for t in (*spec.signature.requires, *(s.when for s in spec.signature.supports))
        if isinstance(t, MotifPresent)
    ]


#: Every motif gate in the KB, as ``(spec id, its ordinal within that spec)`` — collected, never
#: listed, so a gate added to any entry is calibrated because it exists.
_MOTIF_GATES = [
    (tech, i)
    for tech in kb.list_spec_ids()
    for i, _ in enumerate(_motif_floor_gates(kb.load_spec(tech)))
]

#: Reads per mixed population. Large enough that the binomial noise on a rate near 0.5 is a couple of
#: points — an order of magnitude under the margin below — and small enough to probe in milliseconds.
_FLOOR_N = 400

#: How far each population is built from the floor it tests, as a share of the room on that side: the
#: passing one sits halfway between `min_rate` and every read, the failing one halfway between
#: `min_rate` and none. At the shipped 0.5 floor that is 75% / 25% tagged, measuring back 0.76 / 0.27
#: (2026-08-04) — a quarter clear on each side, which is what "comfortably" has to mean for the
#: assertion to be about calibration rather than about noise.
_FLOOR_MARGIN = 0.5


def _untagged_diluent() -> Spec:
    """The KB entry whose every element is plain cDNA — the honest thing to dilute a layout with.

    Derived, not named, and derived in ONE place: an eval recipe now describes a library where only
    a minority of reads carry the layout, which is the same construction, so the rule lives beside
    the generator that draws both populations rather than once here and once there.
    """
    return kb.all_cdna_spec()


def _evaluate_at(
    fraction: float,
    *,
    gate: MotifPresent,
    spec: Spec,
    read: Read,
    tagged: list[str],
    plain: list[str],
    tmp_path: Path,
) -> Evaluation:
    """Evaluate ``gate`` over a population that is ``fraction`` tagged reads and the rest plain cDNA."""
    from seqforge.io import DEFAULT_REGISTRY

    n = len(tagged)
    k = round(fraction * n)
    mixed = tagged[:k] + plain[: n - k]
    # Interleaved, because a real part-tagged library is: a file holding the tagged reads first would
    # be a population any bounded head could read as fully tagged.
    random.Random(0).shuffle(mixed)
    path = tmp_path / f"mixed-{fraction:.2f}.fastq.gz"
    write_fastq_gz(path, mixed)
    wp = WindowProbe(observation=probe_file(path), seqs=mixed)
    return evaluate(gate, read, wp, spec, DEFAULT_REGISTRY)


@pytest.mark.parametrize(("tech", "ordinal"), _MOTIF_GATES)
def test_a_motif_gate_passes_above_the_floor_it_declares_and_fails_below_it(
    tech: str, ordinal: int, tmp_path: Path
) -> None:
    """`min_rate` is a frequency, and until now no fixture ever put a spec near one (#285).

    The generator writes every element on every read, so each entry's structure is in 100% of its own
    reads and every declared floor was tested at 1.0 — infinitely far above itself. This builds the
    population the entry actually claims by mixing its own reads with the all-cDNA entry's: the honest
    diluent, since the same generator draws both and the untagged half therefore carries no signal the
    tagged half does not.

    Both sides are asserted because only the pair is a calibration. PASS alone is what a 100%-tagged
    fixture already gave; FAIL alone would pass for a gate that can never fire. And the FAIL side is
    what catches a diluent that does not dilute: a read the search window does not fit leaves the
    denominator rather than lowering the rate, so reads too short to be asked show up here as the
    dilute population PASSING, not as a silent green.

    **The limit, written down rather than discovered later.** The synthetic diluent is cleaner than
    reality. Real untagged reads of the chemistry that motivated this carry the tag off-offset at
    ~6% — and *structured*, at offsets 13/15/23 — against 0.25% in real bulk, while uniform-random
    cDNA gives an unstructured ~3% here. This proves the gate is calibrated to a FREQUENCY. It does
    **not** prove robustness against that structured background, which stays measured on real reads
    and must not be claimed from a green here.
    """
    spec = kb.load_spec(tech)
    gate = _motif_floor_gates(spec)[ordinal]
    read = next(r for r in spec.reads if r.id == gate.read)
    assert 0.0 < gate.min_rate <= 1.0, (
        f"{tech}: min_rate={gate.min_rate} is not a floor — a gate every population clears (or none "
        "can) says nothing about the frequency the entry claims."
    )

    tagged = kb.generate_reads(spec, n=_FLOOR_N, seed=0)[read.id]
    diluent = _untagged_diluent()
    # Either mate of a two-cDNA entry is the same construction, so the first one is as arbitrary as it
    # is deterministic. A seed of its own keeps the two populations independent draws.
    plain = kb.generate_reads(diluent, n=_FLOOR_N, seed=1)[diluent.reads[0].id]

    def measure(fraction: float) -> Evaluation:
        return _evaluate_at(
            fraction, gate=gate, spec=spec, read=read, tagged=tagged, plain=plain, tmp_path=tmp_path
        )

    above = gate.min_rate + (1.0 - gate.min_rate) * _FLOOR_MARGIN
    below = gate.min_rate * (1.0 - _FLOOR_MARGIN)
    passing = measure(above)
    failing = measure(below)

    assert passing.outcome is Outcome.PASS, (
        f"{tech}: {above:.0%} of reads carrying the motif did not clear min_rate={gate.min_rate} "
        f"({passing.detail})"
    )
    assert failing.outcome is Outcome.FAIL, (
        f"{tech}: {below:.0%} of reads carrying the motif still cleared min_rate={gate.min_rate} "
        f"({failing.detail}) — the gate is not measuring the frequency it declares."
    )


# ---------- the benign-twin rule, as a computed biconditional ----------
def test_the_benign_twin_biconditional_holds_over_every_loaded_spec_pair() -> None:
    """``backend_identical(A, B) <=> declared processing_equivalent`` — the rule the resolver is built on.

    `confuse.py`'s docstring asserted CI computed this. Nothing did:
    `backend_identical` had zero callers, and the one pair it existed for (v3 <-> v3.1) named a spec
    that was never written, so the flagship example of the rule was the one pair no one could check.

    The two directions fail differently, which is why both halves matter:
    - identical but NOT declared -> we would interrogate a user about a distinction that cannot change
      a single byte of output. The benign-twin rule exists to forbid exactly that.
    - declared but NOT identical -> a FALSE BENIGN: two chemistries that really do compile differently
      get recorded together and one config is emitted for both. That is a silent wrong answer, and it
      is the failure this test is really here for.
    """
    from itertools import combinations

    from seqforge.resolve.confuse import backend_identical, declared_equivalents

    specs = kb.load_all_specs()
    for a, b in combinations(sorted(specs), 2):
        identical = backend_identical(specs[a], specs[b])
        # union of both directions, mirroring what escalate() actually consults at runtime
        declared = b in declared_equivalents(specs[a]) or a in declared_equivalents(specs[b])
        assert identical == declared, (
            f"benign-twin biconditional broken for {a} vs {b}: "
            f"backend_identical={identical} but declared processing_equivalent={declared}"
        )


def test_the_biconditional_is_non_vacuous() -> None:
    """A biconditional that never sees a True on either side proves nothing.

    Pins the flagship pair: v3 and v3.1 exist, are byte-identical, and say so.
    """
    from seqforge.resolve.confuse import backend_identical, declared_equivalents

    specs = kb.load_all_specs()
    assert {"10x-3p-gex-v3", "10x-3p-gex-v3.1"} <= set(specs)
    assert backend_identical(specs["10x-3p-gex-v3"], specs["10x-3p-gex-v3.1"])
    assert "10x-3p-gex-v3.1" in declared_equivalents(specs["10x-3p-gex-v3"])
    # ...and declared on BOTH sides, so the file reads as symmetric to a human
    assert "10x-3p-gex-v3" in declared_equivalents(specs["10x-3p-gex-v3.1"])


def test_a_divergent_pair_is_not_backend_identical() -> None:
    """The other side of the biconditional, on real specs: v2 vs v3 differ (10 vs 12 bp UMI)."""
    from seqforge.resolve.confuse import backend_identical

    specs = kb.load_all_specs()
    assert not backend_identical(specs["10x-3p-gex-v2"], specs["10x-3p-gex-v3"])
    assert not backend_identical(specs["bulk-rnaseq"], specs["splitseq"])


def test_a_declared_twin_that_diverges_would_be_caught() -> None:
    """Prove the guard fires: perturb one param and the biconditional must go red.

    A gate that has never rejected anything is a gate nobody has tested.
    """
    from seqforge.resolve.confuse import backend_identical, declared_equivalents

    specs = kb.load_all_specs()
    v3, v31 = specs["10x-3p-gex-v3"], specs["10x-3p-gex-v3.1"]
    v31_backend = v31.require_backend()
    diverged = v31.model_copy(
        update={
            "backend": v31_backend.model_copy(
                update={"params": {**v31_backend.params, "soloStrand": "Reverse"}}
            )
        }
    )
    assert not backend_identical(v3, diverged)  # no longer identical...
    assert "10x-3p-gex-v3.1" in declared_equivalents(v3)  # ...but still declared benign
    # => identical(False) != declared(True) => the biconditional above would fail. A strand
    #    inversion recorded as a benign twin is precisely the silent corpus killer.


# ---------- The parse/count line, as a property of the DSL ----------
@pytest.mark.parametrize("tech", kb.runnable_spec_ids())
def test_kb_specs_declare_only_parse_keys(tech: str) -> None:
    """The four-line test that would have caught the original misfiling on day one.

    soloFeatures sat in backend.params because that is where the aligner's flags live — and it cost a
    measured 40.7% of a nuclear library, because 10x 3' v3.1 chemistry is byte-identical for cells and
    nuclei. Counting was never a chemistry property.
    """
    from seqforge.compose import RECIPE_PARAM_KEYS
    from seqforge.workflows import parse_keys_for

    backend = kb.load_spec(tech).require_backend()
    params = backend.params
    # The parse namespace is per pipeline now: a spec's params must be a subset of the namespace of
    # the pipeline it targets, not of a single global set every pipeline shared.
    assert set(params) <= parse_keys_for(backend.module), f"{tech}: non-parse key in backend.params"
    assert not set(params) & RECIPE_PARAM_KEYS, f"{tech}: a count key is misfiled as chemistry"


def test_every_starsolo_spec_declares_a_cb_match_type_its_solotype_accepts() -> None:
    """``soloCBmatchWLtype`` is KB-owned now, and both ways of getting it wrong die on a compute node.

    Declaring NOTHING is the dangerous one, because it looks like restraint. The module emits the key
    unconditionally, and STAR's own global default — ``1MM_multi`` — is illegal for
    ``CB_UMI_Complex``, so a new combinatorial chemistry that simply said nothing would FATAL after
    the genome had loaded. Declaring a value from the wrong half of the menu fails identically: the
    ``1MM_multi*`` family is Simple-only and ``EditDist_2`` is Complex-only, so the legal set is a
    function of the spec's own ``soloType`` and cannot be checked one string at a time.

    Collected from the loader, not from a roster of the eleven specs that carry the key today, so the
    twelfth is covered *because it exists*. That is the point of the move: the value used to be a
    branch on ``soloType`` inside ``starsolo.smk``, which can hold two answers, and a planned Parse
    Evercode entry is a third — ``CB_UMI_Complex`` like BD Rhapsody and SPLiT-seq, and ``EditDist_2``
    rather than their ``1MM``. It needs no module change; it needs a line in its own spec.

    The legality matrix is IMPORTED from the composer rather than restated here. It was measured
    against the STAR binary, it is what the compose gate refuses on, and a second copy is a second
    thing to drift — the KB would then be free to declare a pair compose rejects.
    """
    from seqforge.compose.params import CB_MATCH_WL_TYPES

    checked: set[str] = set()
    seen_types: set[str] = set()
    for tech in kb.runnable_spec_ids():
        backend = kb.load_spec(tech).require_backend()
        if backend.module != "map/starsolo":
            continue
        checked.add(tech)
        seen_types.add(str(backend.params.get("soloType")))
        declared = backend.params.get("soloCBmatchWLtype")
        assert declared is not None, (
            f"{tech}: declares no soloCBmatchWLtype. Saying nothing is not the safe answer — STAR's "
            f"global default 1MM_multi is itself illegal for CB_UMI_Complex"
        )
        solo_type = backend.params["soloType"]
        # A param value is typed as the DSL's whole union (scalars and the positional whitelist list);
        # both of these keys are strings in every spec that has them, so narrow rather than cast.
        assert isinstance(solo_type, str) and isinstance(declared, str), (
            f"{tech}: soloType/soloCBmatchWLtype must be strings, got {solo_type!r} / {declared!r}"
        )
        legal = CB_MATCH_WL_TYPES.get(solo_type)
        assert legal is not None, f"{tech}: soloType {solo_type!r} has no measured legality set"
        assert declared in legal, (
            f"{tech}: soloCBmatchWLtype {declared!r} is not legal for {solo_type} — STAR accepts "
            f"{sorted(legal)} there, and refuses anything else with a hard PARAMETERS error"
        )

    # A sweep that selected nothing passes, which is the failure this loop is least able to notice:
    # rename the module and every assertion above stops running with nothing red.
    assert len(checked) > 1, f"expected the starsolo specs, selected {sorted(checked)}"
    # And a sweep that selected only Simple chemistries is the SAME failure, one layer in — it is the
    # shape this whole key was moved to fix, and the shape the issue warns leaves a 10x-only suite
    # green. Both halves of the measured legality matrix must actually be exercised by a real spec.
    assert seen_types == set(CB_MATCH_WL_TYPES), (
        f"the sweep covered soloTypes {sorted(seen_types)}, not {sorted(CB_MATCH_WL_TYPES)}; every "
        f"wrong answer about soloCBmatchWLtype breaks the Complex specs and leaves the 10x ones green"
    )


def test_every_starsolo_spec_declares_the_read_preprocessing_its_own_protocol_runs() -> None:
    """``clipAdapterType`` is REQUIRED of all of them, and silence is what that forbids.

    The module dereferences the key with a subscript, so a spec that omits it is a ``KeyError`` on a
    compute node. Optional-with-a-default was rejected for a second reason this test is the guard
    for: whichever group stayed silent would be *defined* by silence, and a new entry would join it
    by accident — which is exactly how four chemistries came to be handed a three-prime 10x TSO.

    The sweep is collected from the loader rather than from a roster of the eleven, so the twelfth is
    covered because it exists. Both values must be exercised by a real spec: the whole point of the
    move is that the right answer differs between chemistries, and a sweep in which every entry says
    ``CellRanger4`` is indistinguishable from the module literal this replaced.

    The five 10x names below are typed out on purpose, which is not the same thing as mirroring the
    data: they pin a decision rather than restate the specs, so the assertion is *meant* to go red
    when one of them changes. Derived from the specs it checks, it would pass whatever they said.

    Which clip each trimmer will TAKE is not asserted here, and was: it is a property of the DSL now
    (``Spec._clip_end_matches_the_trimmer``), so a shipped entry that got it wrong could not reach
    the assertion — ``load_spec`` on the line above refuses it first, and would refuse it for every
    other test in this file too.
    """
    seen: dict[str, set[str]] = {}
    for tech in kb.runnable_spec_ids():
        backend = kb.load_spec(tech).require_backend()
        if backend.module != "map/starsolo":
            continue
        declared = backend.params.get("clipAdapterType")
        assert isinstance(declared, str), (
            f"{tech}: declares no clipAdapterType. The module subscripts the key, so saying nothing "
            f"is a KeyError after the queue wait — and a default would file this entry by silence"
        )
        seen.setdefault(declared, set()).add(tech)

    assert set(seen) == {"CellRanger4", "Hamming"}, (
        f"the sweep found {sorted(seen)}; a knowledge base in which every chemistry names the same "
        f"trimmer is the module literal this key replaced, wearing a spec's clothes"
    )
    # The five three-prime 10x entries, by name and as a set, because their command line is the one
    # thing this change was not allowed to move: they are what a published CellRanger matrix is
    # comparable to, and a corpus of counts already exists under exactly this value.
    assert seen["CellRanger4"] >= {
        "10x-3p-gex-v2",
        "10x-3p-gex-v3",
        "10x-3p-gex-v3.1",
        "10x-gemx-3p-v4",
        "10x-multiome-gex",
    }


def test_the_kb_cannot_even_express_a_count_key() -> None:
    """Not a convention — a validator. It fires in load_spec, kb lint, and every test that loads."""
    backend = kb.load_spec("10x-3p-gex-v3").require_backend()
    payload = backend.model_dump()
    payload["params"] = {**payload["params"], "soloFeatures": ["Gene"]}
    with pytest.raises(ValidationError, match="PARSE"):
        type(backend).model_validate(payload)


def test_kb_parse_keys_and_recipe_param_keys_are_disjoint() -> None:
    """The proof that "a user instruction contradicts the observed bytes" is INEXPRESSIBLE.

    Not deprioritized by a runtime comparison — the user has no vocabulary in which to say it. That is
    the strongest form of that guarantee available, and it holds only while each pipeline's parse
    namespace stays disjoint from the count surface. If someone later moves soloStrand into the
    instructable surface, this goes red, because at that point the contradiction becomes sayable.

    Per pipeline, because the parse namespace is per pipeline now: every registered pipeline's
    ``parse_keys`` must be disjoint from the count keys, so no aligner can smuggle a count key into a
    place a user cannot reach.
    """
    from seqforge.compose import RECIPE_PARAM_KEYS
    from seqforge.workflows import list_modules, parse_keys_for

    for module in list_modules():
        assert not (parse_keys_for(module) & RECIPE_PARAM_KEYS), (
            f"{module}: a parse key is also a count key — the contradiction would be sayable"
        )


def test_bulk_declares_no_parse_keys_and_that_is_meaningful() -> None:
    """Empty, not degenerate: bulk PE has no barcode, no UMI, no whitelist, no offsets to declare."""
    assert kb.load_spec("bulk-rnaseq").require_backend().params == {}


def test_backend_identical_is_order_sensitive_for_a_positional_whitelist() -> None:
    """A FALSE BENIGN this repo shipped: canonical_backend used to SORT list-valued params.

    Its only justification was `soloFeatures=[Gene,GeneFull] == [GeneFull,Gene]` — and soloFeatures has
    since left backend.params. What remained under the sort was splitseq's `soloCBwhitelist`,
    which is POSITIONAL: the rounds map to CB positions in order. So a spec and the same spec with its
    rounds permuted — two chemistries that parse reads DIFFERENTLY — canonicalized byte-equal, i.e.
    processing_equivalent, i.e. benign: record both, ask zero questions, emit ONE config for both.

    It never fired only by the alphabetical accident that round1 < round2 < round3. Rename the
    registry entries bc3/bc2/bc1 and it does.
    """
    from seqforge.resolve.confuse import backend_identical

    spec = kb.load_spec("splitseq")
    backend = spec.require_backend()
    wl = backend.params["soloCBwhitelist"]
    assert isinstance(wl, list) and len(wl) == 3
    permuted = spec.model_copy(
        update={
            "backend": backend.model_copy(
                update={"params": {**backend.params, "soloCBwhitelist": list(reversed(wl))}}
            )
        }
    )
    assert not backend_identical(spec, permuted), "permuted rounds are a DIFFERENT chemistry"


def test_the_only_list_valued_parse_param_left_is_positional() -> None:
    """Pins the reasoning above: if a non-positional list param ever returns, revisit _resolve_value.

    Every list-valued parse param is a `soloCBwhitelist` — the ORDERED whitelist list of a
    CB_UMI_Complex chemistry (SPLiT-seq's three rounds, BD Rhapsody's three CLS blocks), whose order is
    positional (i-th whitelist <-> i-th CB segment). A list param that is NOT a whitelist would be a
    new kind of thing and should force a look at how `_resolve_value` flattens it.
    """
    list_params = {
        (tech, key)
        for tech in kb.runnable_spec_ids()
        for key, value in kb.load_spec(tech).require_backend().params.items()
        if isinstance(value, list)
    }
    # Derived, not enumerated: the invariant is "every list-valued parse param is a positional
    # whitelist", so assert THAT rather than a hand-kept roster of which specs have one (which rots the
    # moment a BD/split-pool-shaped chemistry is added — as the Enhanced leaves just did). A non-
    # whitelist list param is the thing that must force a look at `_resolve_value`.
    assert list_params, "expected at least one list-valued whitelist param (splitseq / BD Rhapsody)"
    assert all(key == "soloCBwhitelist" for _, key in list_params), (
        f"a non-whitelist list-valued parse param appeared: "
        f"{sorted(list_params)} — revisit _resolve_value, which flattens list params assuming "
        f"positional soloCBwhitelist semantics"
    )


def test_bd_rhapsody_wins_over_bulk_on_real_shipped_barcodes(tmp_path: Path) -> None:
    """The whole point of shipping the CLS whitelists (#11): a BD Rhapsody library whose reads carry
    REAL cell-label barcodes must WIN over the generic bulk fallback at rung 3 — not tie into a
    question, and not silently collapse to a bulk matrix.

    Synthetic random barcodes miss the whitelist (that is true of every spec's roundtrip, which is why
    `resolve score` decides 10x on geometry there), so this builds reads from the ACTUAL shipped CLS
    lists — exactly what a real GSE274290 run carries. If this ever regresses to `bulk-rnaseq`, the
    onlist is not reaching the scorer and BD Rhapsody datasets would compile as bulk.
    """
    import random

    from seqforge.io import DEFAULT_REGISTRY
    from seqforge.io.onlist import unpack_barcodes
    from seqforge.resolve import resolve_dataset

    cls = [unpack_barcodes(DEFAULT_REGISTRY.packed(f"bd-rhapsody-cls{i}")) for i in (1, 2, 3)]
    assert all(len(c) == 97 for c in cls)  # the shipped lists really are 97 x 9 bp
    link1, link2 = "ACTGGCCTGCGA", "GGTAGCGGTGACA"
    rng = random.Random(0)

    def rand(k: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(k))

    r1 = [  # CLS1 + linker1 + CLS2 + linker2 + CLS3 + UMI(8) + poly-T tail (over-sequenced R1)
        rng.choice(cls[0])
        + link1
        + rng.choice(cls[1])
        + link2
        + rng.choice(cls[2])
        + rand(8)
        + "T" * 15
        for _ in range(800)
    ]
    r2 = [rand(90) for _ in range(800)]
    f1, f2 = tmp_path / "bd_R1.fastq.gz", tmp_path / "bd_R2.fastq.gz"

    def _write(path: Path, seqs: list[str]) -> None:
        with gzip.open(path, "wt") as fh:
            for i, s in enumerate(seqs):
                fh.write(f"@r{i}\n{s}\n+\n{'I' * len(s)}\n")

    _write(f1, r1)
    _write(f2, r2)

    out = resolve_dataset([f1, f2], registry=DEFAULT_REGISTRY, use_cache=False)
    assert out.result.candidates, "BD reads must resolve to a candidate"
    assert out.result.candidates[0].technology == "bd-rhapsody-wta", [
        c.technology for c in out.result.candidates[:3]
    ]
    assert out.result.candidates[0].rung_resolved == {"chemistry": 3}  # decided by the onlist
    assert out.exit_code() == 0  # a clean win — not a divergent-tie question, not a collapse
    assert not out.result.questions


def test_splitseq_wins_over_bulk_on_real_shipped_barcodes(tmp_path: Path) -> None:
    """The point of shipping the round whitelists (#127): the mechanism the spec calls decisive fires.

    `splitseq` says of the one technology it is confusable with: "rung 3 decides it — the round1/2/3
    whitelists hit, and bulk has no whitelist to hit." While we shipped no round lists that sentence
    described nothing: the three weight-3.0 onlist tests abstained, and a real SPLiT-seq dataset
    resolved by asking a human. It failed safely, so nothing was red.

    Built from the ACTUAL shipped lists, exactly as the BD Rhapsody case is, because synthetic random
    barcodes miss every whitelist — which is why `kb roundtrip` passing was never evidence that this
    works. Read 2 is the real geometry: UMI(10) + bc3 + linker1 + bc2 + linker2 + bc1 = 94 cycles.
    """
    import random

    from seqforge.io import DEFAULT_REGISTRY
    from seqforge.io.onlist import unpack_barcodes
    from seqforge.resolve import resolve_dataset

    rounds = [unpack_barcodes(DEFAULT_REGISTRY.packed(f"splitseq-round{i}")) for i in (1, 2, 3)]
    assert all(len(r) == 96 for r in rounds)  # the shipped lists really are 96 x 8 bp
    spec = kb.load_spec("splitseq")
    els = {e.name: e for e in spec.reads[1].elements}
    link1, link2 = els["linker1"].sequence, els["linker2"].sequence
    # A linker element MAY declare no sequence -- `ReadElement.sequence` is optional, because a
    # linker is sometimes only a length. SPLiT-seq's two are reconstructed from the paper's oligo
    # table and are the thing this test builds reads out of, so their absence is not a length
    # mismatch to discover 94 characters later; it is this spec having stopped saying what it says.
    assert link1 is not None and link2 is not None, "SPLiT-seq's linkers must declare a sequence"
    rng = random.Random(0)

    def rand(k: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(k))

    # UMI(10) + bc3 + linker1 + bc2 + linker2 + bc1 -> 10+8+30+8+30+8 = 94
    r2 = [
        rand(10)
        + rng.choice(rounds[2])
        + link1
        + rng.choice(rounds[1])
        + link2
        + rng.choice(rounds[0])
        for _ in range(800)
    ]
    assert all(len(s) == 94 for s in r2), "read 2 must be the declared 94 cycles"
    r1 = [rand(66) for _ in range(800)]  # cDNA
    f1, f2 = tmp_path / "ss_R1.fastq.gz", tmp_path / "ss_R2.fastq.gz"

    def _write(path: Path, seqs: list[str]) -> None:
        with gzip.open(path, "wt") as fh:
            for i, s in enumerate(seqs):
                fh.write(f"@r{i}\n{s}\n+\n{'I' * len(s)}\n")

    _write(f1, r1)
    _write(f2, r2)

    out = resolve_dataset([f1, f2], registry=DEFAULT_REGISTRY, use_cache=False)
    assert out.result.candidates, "SPLiT-seq reads must resolve to a candidate"
    assert out.result.candidates[0].technology == "splitseq", [
        c.technology for c in out.result.candidates[:3]
    ]
    assert out.result.candidates[0].rung_resolved == {"chemistry": 3}  # decided by the onlist
    assert out.exit_code() == 0  # a clean win over the bulk fallback, not a question
    assert not out.result.questions


# ---------- BD Rhapsody Enhanced bead: the anchored/variable-position chemistry (#43) ----------
_VB = ("", "A", "GT", "TCA")  # the 0-3 bp diversity insert -> a per-read stagger


def _enhanced_r1(pools: list[list[str]], n: int, rng: object) -> list[str]:
    """Synthetic Enhanced Read 1: [VB][CLS1]GTGA[CLS2]GACA[CLS3][UMI(8)] + over-sequenced poly-T tail.

    The leading VB length cycles 0..3 so every read is staggered differently — exactly what the fixed-
    offset model cannot express and the anchored resolver must recover. CLS blocks are drawn from
    ``pools`` (the real shipped whitelists), so a resolve run hits them at rung 3.
    """
    import random

    assert isinstance(rng, random.Random)

    def rand(k: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(k))

    out = []
    for i in range(n):
        out.append(
            _VB[i % 4]
            + rng.choice(pools[0])
            + "GTGA"
            + rng.choice(pools[1])
            + "GACA"
            + rng.choice(pools[2])
            + rand(8)
            + "T" * 15
        )
    return out


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [("", "bd-rhapsody-wta-enhanced-v1"), ("-384", "bd-rhapsody-wta-enhanced-v2")],
)
def test_bd_enhanced_resolves_to_the_right_leaf_from_bytes(
    suffix: str, expected: str, tmp_path: Path
) -> None:
    """The headline acceptance (#43): an Enhanced-bead library resolves to the correct leaf FROM BYTES.

    The two Enhanced sub-versions differ ONLY in whitelist (97 vs 384 sequences per CLS block, disjoint
    pools), so telling ``-96`` from ``-v2`` is onlist-decided at rung 3 — exactly the 10x v2/v3 split.
    Reads are built from the REAL shipped CLS lists and staggered by the 0-3 bp diversity insert; a
    clean win here proves the family recognised the GTGA...GACA frame, descended, and the anchored
    onlist hit resolved the per-read barcode windows the stagger created.

    Enhanced vs the ORIGINAL v1 bead is byte-decided too, and this test is the Enhanced side of that
    pair (#110, absorbing `test_bd_v1_and_enhanced_are_told_apart_from_the_bytes`). Both draw CLS blocks
    from the same `bd-rhapsody-cls*` pools, so the onlist cannot separate them — only the linker
    STRUCTURE can: v1 has the fixed 12/13 bp `ACTGGCCTGCGA`/`GGTAGCGGTGACA` linkers, Enhanced the
    staggered 4 bp `GTGA`/`GACA`. Enhanced reads mis-resolving to the original bead reddens the exact-leaf
    assertion here; the reciprocal (v1 reads mis-resolving to Enhanced) reddens
    `test_bd_rhapsody_wins_over_bulk_on_real_shipped_barcodes`, which pins v1 -> `bd-rhapsody-wta`.
    """
    import random

    from seqforge.io import DEFAULT_REGISTRY
    from seqforge.io.onlist import unpack_barcodes
    from seqforge.resolve import resolve_dataset

    pools = [
        unpack_barcodes(DEFAULT_REGISTRY.packed(f"bd-rhapsody-cls{i}{suffix}")) for i in (1, 2, 3)
    ]
    rng = random.Random(0)
    r1 = _enhanced_r1(pools, 800, rng)
    r2 = ["".join(rng.choice("ACGT") for _ in range(90)) for _ in range(800)]
    f1, f2 = tmp_path / "enh_R1.fastq.gz", tmp_path / "enh_R2.fastq.gz"
    write_fastq_gz(f1, r1)
    write_fastq_gz(f2, r2)

    out = resolve_dataset([f1, f2], registry=DEFAULT_REGISTRY, use_cache=False)
    assert out.result.candidates, "Enhanced reads must resolve to a candidate"
    assert out.result.candidates[0].technology == expected, [
        c.technology for c in out.result.candidates[:3]
    ]
    assert out.result.candidates[0].rung_resolved == {"chemistry": 3}  # onlist-decided leaf
    assert (
        out.exit_code() == 0
    )  # a clean win over bulk and the sibling — not a divergent-tie question
    assert not out.result.questions


# `test_bd_v1_and_enhanced_are_told_apart_from_the_bytes` was deleted (#110): both halves were
# re-assertions. The v1 -> `bd-rhapsody-wta` half is the same construction as
# `test_bd_rhapsody_wins_over_bulk_on_real_shipped_barcodes`, which additionally pins
# `rung_resolved == {chemistry: 3}`, `exit_code() == 0` and no questions; the Enhanced ->
# `{enhanced-v1, enhanced-v2}` half is strictly weaker than `test_bd_enhanced_resolves_to_the_right_leaf_from_bytes`,
# which pins the EXACT leaf from the identical build. Its linker-structure reasoning moved onto that
# test's docstring.


def test_the_anchored_resolver_recovers_the_staggered_frame() -> None:
    """`kb.anchor.resolve_windows` recovers each CLS/UMI window across the 0-3 bp insert, and only then.

    The unit-level guarantee under the resolution above: given the declared Enhanced layout, every
    staggered read's barcode windows are recovered exactly, and a read WITHOUT the GTGA...GACA frame
    (a cDNA read) yields no frame at all rather than a wrong slice.
    """
    import random

    from seqforge.kb.anchor import has_anchored_elements, resolve_windows

    spec = kb.load_spec("bd-rhapsody-wta-enhanced-v1")
    bc = next(r for r in spec.reads if r.id == "bc")
    assert has_anchored_elements(bc)
    rng = random.Random(0)

    def rand(k: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(k))

    recovered = 0
    for i in range(400):
        c1, c2, c3, umi = rand(9), rand(9), rand(9), rand(8)
        seq = _VB[i % 4] + c1 + "GTGA" + c2 + "GACA" + c3 + umi + "T" * 20
        w = resolve_windows(seq, bc)
        assert w is not None
        if (
            seq[slice(*w["cls1"])] == c1
            and seq[slice(*w["cls2"])] == c2
            and seq[slice(*w["cls3"])] == c3
            and seq[slice(*w["UMI"])] == umi
        ):
            recovered += 1
    assert recovered == 400  # every staggered read, exactly
    # a plain cDNA read has no GTGA...GACA frame -> unresolved, never mis-sliced
    misfires = sum(1 for _ in range(400) if resolve_windows(rand(90), bc) is not None)
    assert misfires <= 8  # chance frame matches are rare and would fail the onlist anyway


def test_every_confusable_target_is_a_technology_we_support() -> None:
    """A `confusable_with` edge must point at a spec that exists, not at one we mean to write.

    This is the same defect `test_a_spec_that_calls_onlists_decisive_can_actually_reach_one` was built
    for, one level up, and it hid in the one place that guard does not look: it reads the onlists a
    spec's own ELEMENTS reference, so it never sees a `distinguishable_by: [onlist]` claim about a
    *pair*. Four edges pointed at ids with no spec directory — `10x-gemx-3p-v4` and `10x-5p-gex`, from
    both v3 and v3.1.

    A dangling edge fails the way this repo's worst failures fail: quietly and safely. The resolver
    cannot score a spec that does not exist, so the divergent tie the edge describes never happens; a
    real GEM-X library instead misses the v3 whitelist, matches no positive target, and abstains or
    mis-resolves. Nothing is red, and the note in the spec reads as though the case were handled.

    Declaring confusability with a chemistry we cannot resolve to is therefore not a forward
    declaration, it is a promise with no way to come due. Either write the entry or drop the edge.
    """
    ids = set(kb.list_spec_ids())
    dangling = {
        spec_id: sorted(c.id for c in kb.load_spec(spec_id).confusable_with if c.id not in ids)
        for spec_id in sorted(ids)
    }
    dangling = {k: v for k, v in dangling.items() if v}

    assert not dangling, (
        "confusable_with points at technologies that have no spec:\n"
        + "\n".join(f"  {k} -> {', '.join(v)}" for k, v in dangling.items())
        + "\nWrite the entry (a spec directory plus a hermetic ci eval case) or delete the edge. An "
        "edge to a chemistry the resolver cannot score describes a tie that can never be taken, so it "
        "reads as handled while a real library of that chemistry abstains or mis-resolves."
    )


# ---------- The rung-0-2 separability guard ----------
@pytest.mark.xdist_group("kb-probes")
def test_no_spec_pair_is_confusable_without_declaring_it(kb_probes: KbProbes) -> None:
    """The under-declaration guard the confusability contract called for and nobody built.

    `decidable_by` and `confusable_with` were hand-maintained claims: nothing computed whether the
    cheap probes ACTUALLY separate two entries, so a new technology that silently collided with an
    existing one passed lint, round-trip and the whole suite. The self-test promised such a merge would be
    blocked. It would not have been.

    Computed, not asserted-to: generate each spec's own synthetic reads, then ask every OTHER spec
    whether it could **outrank** their owner on them using rungs 0-2 alone (the onlist is withheld
    from BOTH sides via an empty registry, so rung-3 evidence cannot rescue either answer). If A
    could come out on top of B on B's own data, A must say so.

    It found one on its first run. `bulk-rnaseq` — the generic paired-end fallback — takes
    SPLiT-seq's cdna+bc pair on geometry alone, and declared nothing. The system already knew: a test
    comment called bulk "the generic bulk fallback that merely fails to be forbidden (rung 2)". The
    KB is where that has to be written down, because the KB is what the resolver reads.

    **The predicate is an ORDERING one, and used to be a validity one** (#275, ADR-0029). Validity
    tracked danger only while every spec consumed every file; a spec that consumes fewer is valid
    against nearly every leaf while scoring far below all of them, because the leftover-file penalty
    is `λ/|R|` per orphan and so bites harder the fewer roles a set has. Under validity such a spec
    would have to declare an edge to almost the whole KB — boilerplate that leaves this guard unable
    to discriminate. The message this guard has always failed with names the danger as "the resolver
    would pick one and never ask", which is a claim about ORDER; the question it asks is now that one.

    **The gate on that change: bulk's declared edges re-derived under it**, since a stronger guard
    that silently drops a true edge has traded noise for blindness.
    `test_bulks_declared_edges_still_derive_under_the_ordering_predicate` is where that happens and
    where the measured margins are tabulated; they moved under #307, which took the withheld onlist
    out of the rung-0-2 NORMALIZER as well as its numerator, and the barcoded incumbents recovered
    the handicap that had been putting the fallback above them.

    Across the whole shipped KB the two predicates flag ALMOST the identical pair set: every pair that
    accepts at rungs 0-2 also outranks, except `bulk-rnaseq` -> `10x-multiome-atac`, where bulk
    accepts the data but now ranks decisively below the chemistry on it. The rest are exact ties
    (margin 0.0000, the 10x 28 bp cohort and the two Enhanced beads) and a tie is not a separation.
    So no edge is gained here and the one that is no longer demanded is kept with its argument
    recorded — the change is about the KB the next entry will make, not the one in the tree.

    **`geometry_could_accept` stays a sound skip**, unchanged, and the argument is a containment one
    rather than a new measurement: outranking REQUIRES a valid assignment, so the new predicate
    implies the old one, and every necessary condition of the old is a necessary condition of the
    new. `length_feasible` is proven necessary for validity (geometry.py), hence necessary here; a
    geometry-NO pair cannot outrank. `test_geometry_could_accept_is_necessary_for_rung02_acceptance`
    holds the premise over every shipped pair.

    **And a higher score is not the whole of "would pick one and never ask"** (ADR-0029). A read set
    that ORPHANS the file the incumbent seats as its barcode read does not get to anchor the tie band,
    so the resolver raises a divergent-tie question on those pairs rather than deciding — measured, at
    rungs 0-2, over the eight pairs where it fires. `could_outrank_at_rungs_0_2` therefore reads
    `seats_a_file_the_fallback_dropped`, the SAME predicate `escalate` acts on, and demanding an edge
    for a danger the resolver already averts would be this guard becoming the formality it exists to
    prevent. `test_the_orphan_exemption_is_not_a_blanket_one` holds it open at both ends.
    """
    from seqforge.resolve.confuse import could_outrank_at_rungs_0_2, is_tree_kin, rung02_margin
    from seqforge.resolve.geometry import geometry_could_accept

    specs = kb.load_all_specs()
    tree = kb.build_tree(specs)
    # Only LEAF chemistries are scored at runtime, so only they can be confused at runtime. A family
    # node is validated by the recognition self-test, not here.
    leaves = tree.leaves()

    undeclared: list[str] = []
    for a in leaves:
        declared = {c.id for c in specs[a].confusable_with}
        for b in leaves:
            if a == b or b in declared:
                continue
            if is_tree_kin(specs, a, b):
                continue  # siblings / parent-child: the tree DECLARES this confusability
            if not geometry_could_accept(specs[a], kb_probes[b, "full"]):
                continue  # proven necessary condition — a length-infeasible pair cannot be confusable
            if could_outrank_at_rungs_0_2(specs[a], specs[b], kb_probes[b, "full"]):
                margin = rung02_margin(specs[a], specs[b], kb_probes[b, "full"])
                undeclared.append(
                    f"{a!r} could outrank {b!r} on {b!r}'s own reads at rungs 0-2 "
                    f"(margin {margin:+.4f}) but does not list it in confusable_with (nor share a "
                    f"parent) — the resolver would pick one and never ask"
                )
    assert not undeclared, "under-declaration:\n" + "\n".join(undeclared)


@pytest.mark.xdist_group("kb-probes")
def test_the_orphan_exemption_is_not_a_blanket_one(kb_probes: KbProbes) -> None:
    """Prove the guard still FIRES with the exemption in place, and fires on exactly the right pairs.

    An exemption nobody has watched fail is an exemption that may be swallowing everything, and this
    one sits inside the only CI error the confusability contract has. So the perturbation: **strip
    `bulk-rnaseq`'s declared edges** in memory and re-ask the guard's question. Five of its pairs must
    come back flagged — the ones where the fallback explains every file and the resolver really would
    pick it and never ask — and the 10x cohort must not, because there the fallback orphans the
    barcode read and the resolver asks.

    That split is the whole claim, and it is what makes the exemption legible as targeted rather than
    total: it turns on whether the file was EXPLAINED, not on who scored higher. `splitseq` and the
    three BD beads put their barcode read at 60-94 bp, which bulk's 40 bp floor admits, so bulk's
    maximal set seats both files and orphans nothing. `smartseq3` orphans nothing either and is the
    starkest instance of it — both of its files are long cDNA reads, one merely carrying a 22 bp
    structural prefix, so the fallback explains the pair completely and ties dead level.

    **`10x-multiome-atac` was the sixth and has left the set, and NOT via the exemption** (#307). It
    still orphans its 24 bp barcode read from bulk's MAXIMAL set, so the exemption — scoped to a
    proper-subset read set, so that a rule introduced by read sets cannot retire an edge predating
    them — still does not fire on it, and that is asserted below rather than inferred. What changed is
    the ordering: with the onlist out of the rung-0-2 signature the incumbent recovers to 0.9067
    against bulk's 0.8800, so bulk is decisively BELOW and could not outrank it. A pair leaving this
    set because the true chemistry now wins is the guard working, not the guard blinded — but the two
    causes are indistinguishable from the set alone, which is why the exemption is pinned separately.

    Deleting a spec's edges is exactly how `test_a_declared_twin_that_diverges_would_be_caught` proves
    the benign-twin gate fires, and it is the same reason here: a guard that has never been seen to go
    red is a guard nobody knows is connected.
    """
    from seqforge.resolve.confuse import (
        could_outrank_at_rungs_0_2,
        rung02_margin,
        seats_a_file_the_fallback_dropped,
        without_rung3_evidence,
    )
    from seqforge.resolve.geometry import geometry_could_accept
    from seqforge.resolve.scoring import build_tech_evaluation

    specs = kb.load_all_specs()
    undeclared_bulk = specs["bulk-rnaseq"].model_copy(update={"confusable_with": []})
    flagged = {
        b
        for b in kb.build_tree(specs).leaves()
        if b != "bulk-rnaseq"
        and geometry_could_accept(undeclared_bulk, kb_probes[b, "full"])
        and could_outrank_at_rungs_0_2(undeclared_bulk, specs[b], kb_probes[b, "full"])
    }

    assert flagged == {
        "splitseq",
        "bd-rhapsody-wta",
        "bd-rhapsody-wta-enhanced-v1",
        "bd-rhapsody-wta-enhanced-v2",
        "smartseq3",
    }, (
        f"an undeclared bulk must still be caught against the five leaves whose data it fully "
        f"explains; got {sorted(flagged)}. Too few means the exemption is swallowing real danger, "
        f"too many means it stopped applying where the resolver genuinely asks."
    )

    # Multiome ATAC left on the ORDERING, not on the exemption — the two are indistinguishable from
    # the set above, and only one of them would mean this guard had been blinded.
    atac_probes = kb_probes["10x-multiome-atac", "full"]
    registry = OnlistRegistry(offline=True)
    fallback = build_tech_evaluation(without_rung3_evidence(undeclared_bulk), atac_probes, registry)
    incumbent = build_tech_evaluation(
        without_rung3_evidence(specs["10x-multiome-atac"]), atac_probes, registry
    )
    assert not seats_a_file_the_fallback_dropped(incumbent, fallback, specs["10x-multiome-atac"]), (
        "the exemption must NOT be what excuses this pair — bulk orphans it from its maximal set"
    )
    margin = rung02_margin(undeclared_bulk, specs["10x-multiome-atac"], atac_probes)
    assert margin is not None and margin < 0, (
        f"...so the only honest reason it is unflagged is that bulk ranks below the chemistry on the "
        f"chemistry's own reads; margin {margin}"
    )


def test_a_confusable_pair_declares_how_it_is_decided(tmp_path: Path) -> None:
    """ "Ask the human" must be a COMPUTED property, not a prompt hope.

    A pair that the cheap probes cannot separate has to name the mechanism that can — onlist,
    metadata, alignment or a user — because that name is what the escalation ladder branches on. A
    `distinguishable_by: [none]` on a *divergent* pair would be a dead end the resolver cannot act
    on, which the schema already refuses; this asserts the rest of the KB actually says something.
    """
    for tech in kb.list_spec_ids():
        spec = kb.load_spec(tech)
        for c in spec.confusable_with:
            assert c.distinguishable_by, f"{tech} -> {c.id}: confusable but no mechanism named"
            if c.relationship == "processing_divergent":
                assert c.distinguishable_by != ["none"], (
                    f"{tech} -> {c.id}: divergent AND undecidable is a dead end, not a declaration"
                )


@pytest.mark.xdist_group("kb-probes")
def test_the_separability_guard_can_actually_catch_a_collision(kb_probes: KbProbes) -> None:
    """Prove the guard fires: a spec IS confusable with itself, by construction.

    A tautology, and that is the point — if the predicate cannot recognise a spec's own synthetic
    reads, it recognises nothing and every "declared OK" above is vacuous.

    Both predicates are pinned here, because the guard's question moved from one to the other (#275)
    and `rung02_separable` still asks the older one. A spec ties ITSELF exactly — margin 0.0000 — so
    it could outrank itself, and that is the sign convention as much as the tautology: a tie is not a
    separation. A predicate reading `>` rather than `>=` would exempt the exact tie, which is the one
    shape where nothing in the bytes orders the pair at all and `escalate` falls through to its
    alphabetical determinism tiebreak to decide which of the two leads the candidate list.
    """
    from seqforge.resolve.confuse import (
        accepts_at_rungs_0_2,
        could_outrank_at_rungs_0_2,
        rung02_margin,
        rung02_separable,
    )

    spec = kb.load_spec("10x-3p-gex-v3")
    splitseq = kb.load_spec("splitseq")
    own = kb_probes["10x-3p-gex-v3", "full"]
    assert accepts_at_rungs_0_2(spec, own)
    assert not rung02_separable(spec, own, spec, own)  # nothing is separable from itself
    assert could_outrank_at_rungs_0_2(spec, spec, own)
    assert rung02_margin(spec, spec, own) == 0.0

    # ...and it discriminates: splitseq's 94 bp barcode read is not 10x's 28 bp geometry.
    assert not accepts_at_rungs_0_2(spec, kb_probes["splitseq", "full"])
    # A spec that cannot score the data at all ranks NOWHERE on it — `None`, not a negative margin.
    # That is the conjunct which keeps `geometry_could_accept` a sound skip for the new predicate.
    assert rung02_margin(spec, splitseq, kb_probes["splitseq", "full"]) is None
    assert not could_outrank_at_rungs_0_2(spec, splitseq, kb_probes["splitseq", "full"])


@pytest.mark.xdist_group("kb-probes")
def test_bulks_declared_edges_still_derive_under_the_ordering_predicate(
    kb_probes: KbProbes,
) -> None:
    """The gate on #275: a stronger guard that silently drops a TRUE edge has traded noise for
    blindness, and that failure is invisible unless it is checked for directly.

    The under-declaration sweep cannot check it — it `continue`s on a declared pair, so every edge in
    the KB is a claim the sweep takes on trust. `bulk-rnaseq` is where the trust is expensive: it
    is the generic paired-end fallback, its edges are the only thing standing between a single-cell
    library and a bulk gene-count matrix at exit 0, and the first five were derived under the OLD
    (validity) predicate. So they are re-derived here, under the new one, as an executable gate
    rather than a sentence in a pull request.

    **What the gate asserts is the DANGER DIRECTION, and #307 is why it is no longer two shapes.**
    The edge exists to stop one thing: the resolver handing back a bulk gene-count matrix for a
    single-cell library without asking. That happens when — and only when — bulk sits DECISIVELY
    ABOVE the incumbent on the incumbent's own reads. So every edge, whatever mechanism it names, is
    re-derived against `margin <= θ`, and a `[metadata]` edge additionally against `margin >= -θ`,
    because that edge promises a mechanism only a tie ever reaches: outside the band in either
    direction the resolver decides and never asks a human.

    **The five `[onlist]` edges used to be re-derived against the OPPOSITE arithmetic — `margin > θ`,
    bulk ahead — and that reading was an artifact of the defect #307 fixed.** Withholding the onlist
    at rungs 0-2 emptied the numerator of every `onlist_hit_rate` support but left its weight in the
    normalizer, so each barcoded incumbent was marked down by however much whitelist evidence it had
    the honesty to declare — 5 of 8 support weight for the 10x cohort, 9 of 10 for SPLiT-seq and the
    BD beads — while bulk, which declares none, lost nothing. Bulk was not ahead of these chemistries
    on the cheap probes; it was ahead of a handicap. With the onlist out of the signature at rungs 0-2
    (`confuse.without_rung3_evidence`) the incumbents recover and the pairs stand level or the
    incumbent leads, which is the honest relationship and still leaves the onlist as what SEPARATES
    them — the claim the edge makes.

    | edge | mechanism | bulk | incumbent | margin | was |
    |---|---|---|---|---|---|
    | `splitseq`                    | `[onlist]`   | 1.0000 | 1.0000 | +0.0000 | +0.4500 |
    | `bd-rhapsody-wta`             | `[onlist]`   | 0.9875 | 1.0000 | -0.0125 | +0.4375 |
    | `bd-rhapsody-wta-enhanced-v1` | `[onlist]`   | 0.9975 | 1.0000 | -0.0025 | +0.4475 |
    | `bd-rhapsody-wta-enhanced-v2` | `[onlist]`   | 0.9975 | 1.0000 | -0.0025 | +0.4475 |
    | `10x-multiome-atac`           | `[onlist]`   | 0.8800 | 0.9067 | -0.0267 | +0.1376 |
    | `smartseq3`                   | `[metadata]` | 1.0100 | 1.0100 | +0.0000 | +0.0000 |

    Re-derive with `resolve.confuse.rung02_margin(specs['bulk-rnaseq'], specs[b], probes[b])`.

    **`10x-multiome-atac` is the one edge the under-declaration sweep would no longer DEMAND**, and it
    is kept anyway. At -0.0267 bulk is decisively below, so `could_outrank_at_rungs_0_2` is now False
    and an undeclared version of this pair would not be flagged. That is not the same as the edge
    being false: the sweep measures one direction on synthetic reads with every whitelist withheld,
    and the edge's actual claim — that a Multiome ATAC deposit's two genomic mates read as a bulk cDNA
    pair, and the whitelist is what tells them apart — is about real data. Deleting it would trade a
    declared danger for an undeclared one on the strength of a synthetic margin moving by 0.16.

    An edge that stops deriving is a discussion, not a deletion: the honest repairs are to tighten
    the fallback's gates or to write down why the pair separates now, and both are changes somebody
    has to argue for. Deleting the edge to make this green removes the only declaration that stops a
    silent bulk answer for that chemistry.
    """
    from seqforge.resolve.confuse import could_outrank_at_rungs_0_2, rung02_margin
    from seqforge.resolve.escalate import _THETA

    specs = kb.load_all_specs()
    bulk = specs["bulk-rnaseq"]
    edges = {c.id: c.distinguishable_by for c in bulk.confusable_with}
    assert len(edges) == 6, (
        f"bulk-rnaseq declares {len(edges)} confusable edges, not the six this gate re-derives: "
        f"{sorted(edges)}. A new one needs a line here; a missing one needs an argument, not a diff."
    )

    for other, mechanisms in sorted(edges.items()):
        margin = rung02_margin(bulk, specs[other], kb_probes[other, "full"])
        assert margin is not None, (
            f"bulk-rnaseq scores nothing on {other!r}'s own reads, so it cannot be confusable with "
            f"it at all. Do not delete the edge to make this pass."
        )
        assert margin <= _THETA, (
            f"bulk-rnaseq -> {other!r} no longer derives: margin {margin:+.4f} on {other!r}'s own "
            f"reads at rungs 0-2 puts the generic fallback DECISIVELY ABOVE the chemistry. There the "
            f"resolver returns bulk and never reaches for {mechanisms}, which is the silent bulk "
            f"answer this edge exists to stop. Do not delete the edge to make this pass."
        )
        if mechanisms != ["onlist"]:
            assert margin >= -_THETA, (
                f"bulk-rnaseq -> {other!r} declares {mechanisms}, i.e. that the cheap rungs cannot "
                f"order this pair — but the margin on {other!r}'s own reads is {margin:+.4f}, below "
                f"the tie band. Outside the band the resolver decides and never reaches for the "
                f"mechanism this edge promises, so the declaration is the thing that is wrong."
            )
    # The sweep's own predicate, per edge: inside the band it still holds, and the one edge that has
    # left it is named rather than quietly dropped, so a THIRD one leaving goes red here.
    outranked = {
        other
        for other in edges
        if could_outrank_at_rungs_0_2(bulk, specs[other], kb_probes[other, "full"])
    }
    assert outranked == set(edges) - {"10x-multiome-atac"}, (
        f"the under-declaration sweep demands every bulk edge but `10x-multiome-atac`, whose margin "
        f"of -0.0267 puts bulk decisively below it; got {sorted(outranked)}. An edge leaving this set "
        f"is the sweep ceasing to require it, which needs the argument in this docstring extended."
    )


@pytest.mark.xdist_group("kb-probes")
def test_the_single_end_set_does_not_outrank_a_single_cell_chemistry_on_its_own_data(
    kb_probes: KbProbes,
) -> None:
    """The plausible-matrix failure a single-end fallback could introduce, asserted rather than argued.

    A one-role read set seats itself on any 40+ bp cDNA read, and every single-cell chemistry in the KB
    HAS a cDNA read. So the question the read-set feature has to answer directly is whether that set can
    take a real single-cell library's own data — because the answer being "no" is not a refusal, it is a
    bulk gene-count matrix at exit 0, and a plausible matrix is the one failure here that nobody
    downstream notices.

    Nothing had to be invented for it to lose. The score is normalized by role count and charges
    ``λ/|R|`` per orphaned file, so a one-role set on a two-file deposit pays 0.25 for the mate it
    declined to explain, while the barcoded incumbent's whitelist HITS and takes it to ~0.97. Measured
    over every barcoded leaf, on that leaf's own synthetic reads, with each leaf's own whitelist
    registered — i.e. the comparison the resolver actually makes at runtime:

    | leaf | incumbent | bulk `se` | margin |
    |---|---|---|---|
    | `10x-3p-gex-v2`               | 0.9700 | 0.7500 | -0.2200 |
    | `10x-3p-gex-v3` / `-v3.1`     | 0.9719 | 0.7500 | -0.2219 |
    | `10x-5p-gex-v2`               | 0.9700 | 0.7500 | -0.2200 |
    | `10x-5p-gex-v3`               | 0.9719 | 0.7500 | -0.2219 |
    | `10x-gemx-3p-v4`              | 0.9719 | 0.7500 | -0.2219 |
    | `10x-multiome-gex`            | 0.9719 | 0.7500 | -0.2219 |
    | `10x-multiome-atac`           | 0.9805 | 0.5100 | -0.4705 |
    | `bd-rhapsody-wta` (+ both Enhanced) | 1.0000 | 0.7500 | -0.2500 |
    | `splitseq`                    | 1.0000 | 0.7500 | -0.2500 |

    The set is measured DIRECTLY, not through ``build_tech_evaluation``'s maximum. On most of these the
    maximal set is length-forbidden (a 28 bp barcode read is under bulk's 40 bp floor), so the maximum
    IS the single-end set and a test reading only the winner could not say which set it had measured;
    on the five where the maximal set is feasible it is the incumbent, and those five are the declared
    `confusable_with` edges whose behaviour this change leaves exactly as it was.
    """
    from seqforge.resolve.escalate import _THETA, _barcode_read_id
    from seqforge.resolve.scoring import build_tech_evaluation, read_set_evaluations

    specs = kb.load_all_specs()
    bulk = specs["bulk-rnaseq"]
    barcoded = [i for i in kb.build_tree(specs).leaves() if _barcode_read_id(specs[i]) is not None]
    assert len(barcoded) >= 10, f"expected the KB's single-cell leaves, got {barcoded}"

    for leaf in barcoded:
        wps = kb_probes[leaf, "full"]
        registry = registry_for(specs[leaf])  # the leaf's OWN whitelist, so rung 3 can fire
        incumbent = build_tech_evaluation(specs[leaf], wps, registry)
        se = next(e for e in read_set_evaluations(bulk, wps, registry) if e.read_set == "se")
        assert incumbent.valid, f"{leaf} must score its own reads"
        assert se.value < incumbent.value - _THETA, (
            f"bulk's single-end read set scores {se.value:.4f} against {leaf}'s {incumbent.value:.4f} "
            f"on {leaf}'s OWN reads (margin {se.value - incumbent.value:+.4f}) — a barcodeless "
            f"fallback level with a real single-cell chemistry on its own data emits a bulk gene-count "
            f"matrix for a single-cell library at exit 0. Do not widen the tie band to fix this."
        )


@pytest.mark.xdist_group("kb-probes")
def test_the_plates_maximal_read_set_outranks_its_single_end_subset_on_a_paired_deposit(
    kb_probes: KbProbes,
) -> None:
    """A paired-end plate deposit must not silently lose R2 to the subset that declines to explain it.

    `smartseq3` publishes two configurations and declares `se: [R1]` for the second, so from this KB
    version on EVERY plate deposit has two candidate read sets rather than one. The direction nobody
    asked for is the subset winning on paired data: that is a whole mate dropped from the alignment at
    exit 0, and — because `dataset_hash` is taken over the layout the winning set produces — it would
    also REGENERATE every stored plate manifest rather than recompile it.

    Measured on the entry's own synthetic pair, through `read_set_evaluations` and not through
    `build_tech_evaluation`'s maximum, for the reason
    `test_the_single_end_set_does_not_outrank_a_single_cell_chemistry_on_its_own_data` gives: a test
    that reads only the winner cannot say WHICH set it measured.

    **The maximal set wins by exactly `λ/|R|`** — the orphan penalty a one-role set pays for the one
    file it left unassigned — so nothing had to be invented for it to win, and the margin does not
    move with read depth, because it is not evidence. The winning value is also the score this entry
    had BEFORE it declared a read set, which is what backs the version log's claim that this bump
    re-keys `run_id` alone. Both figures, the three depths they were taken at, and the method:
    `docs/research/smartseq3-single-end-configuration.md` (2026-08-05).
    """
    from seqforge.resolve.scoring import read_set_evaluations

    specs = kb.load_all_specs()
    plate = specs["smartseq3"]
    assert plate.read_set_names() == ["full", "se"], (
        "the plate declares exactly the two published sets"
    )

    wps = kb_probes["smartseq3", "full"]
    assert len(wps) == 2, "the paired configuration is the two-file deposit"
    evs = {e.read_set: e for e in read_set_evaluations(plate, wps, registry_for(plate))}
    full, se = evs["full"], evs["se"]
    assert full.valid and se.valid, "both sets must score a paired plate deposit"
    assert full.value > se.value, (
        f"smartseq3's single-end subset scores {se.value:.4f} against the maximal set's "
        f"{full.value:.4f} on the entry's OWN PAIRED reads (margin {se.value - full.value:+.4f}). The "
        f"subset winning a two-file deposit drops R2 from the alignment at exit 0 and moves the read "
        f"layout, so every stored plate manifest is regenerated rather than recompiled. Fix the "
        f"scoring, not this expectation."
    )


@pytest.mark.xdist_group("kb-probes")
def test_the_generic_single_end_set_does_not_outrank_the_plates_on_a_single_end_plate_deposit(
    kb_probes: KbProbes,
) -> None:
    """The plate's half of the read-set contest — and it is NOT symmetric with bulk's.

    `test_the_single_end_set_does_not_outrank_a_single_cell_chemistry_on_its_own_data` measures the
    generic entry losing to every barcoded leaf by ~0.22-0.25, and both halves of that margin are
    unavailable here: a plate has no onlist to hit (its cell barcode is the FILE), and on a ONE-file
    deposit neither set orphans anything, so nobody pays `λ/|R|`. ADR-0035 records that a near-tie is
    therefore the expected result rather than a defect.

    Scored on `smartseq3`'s own single-end deposit — the same reads as the paired one, narrowed to R1
    by the fixture — through `read_set_evaluations` directly, for the reason the paired case gives.

    **The near-tie is real and its exact margin is NOT — it moves with read depth, and this test
    scores the depth where it is smallest.** `kb_probes` hands a scorer `seqs[:200]`, at which the two
    entries tie EXACTLY; on every read, which is what the resolver scores a real deposit on, the plate
    leads by +0.000999. Both readings are inside `_THETA`, which is the only claim that survives both.
    The figures, the three depths, and why one support saturates while the other does not:
    `docs/research/smartseq3-single-end-configuration.md` (2026-08-05).

    **So the assertion is deliberately one-sided: bulk must not WIN, not that the plate must.** That
    is what makes it depth-proof — it holds at both depths, where an assertion demanding an outright
    plate win would pass on a deposit and fail on the very fixture that scores it here. Landing inside
    `_THETA` routes to the `smartseq3` <-> `bulk-rnaseq` edge both entries declare
    `processing_divergent` / `distinguishable_by: [metadata]` — a Question at exit 4, which is
    recoverable and is the designed outcome. Asserting an outright plate win would pin this fixture's
    read depth into a claim about the chemistry, and it would invite the one repair ADR-0035 forbids:
    #257 measured every additional R1 support on this entry as a strict liability, so tuning the
    signature to open a margin here trades real per-cell margins for a synthetic contest one.
    """
    from seqforge.resolve.escalate import _THETA
    from seqforge.resolve.scoring import read_set_evaluations

    specs = kb.load_all_specs()
    plate, bulk = specs["smartseq3"], specs["bulk-rnaseq"]

    wps = kb_probes["smartseq3", "se"]
    assert len(wps) == 1, "the single-end configuration is the one-file deposit"
    mine = next(
        e for e in read_set_evaluations(plate, wps, registry_for(plate)) if e.read_set == "se"
    )
    theirs = next(
        e for e in read_set_evaluations(bulk, wps, registry_for(bulk)) if e.read_set == "se"
    )
    assert mine.valid, "smartseq3 must score its own single-end reads, or the set is unreachable"
    assert theirs.valid, (
        "the generic fallback does reach this deposit — that is what makes it a contest"
    )
    assert theirs.value <= mine.value + _THETA, (
        f"bulk's single-end read set scores {theirs.value:.4f} against smartseq3's {mine.value:.4f} on "
        f"a SINGLE-END PLATE deposit (margin {theirs.value - mine.value:+.4f}), decisively above the "
        f"tie band. There the resolver returns bulk and never reaches the [metadata] edge, so a plate "
        f"library gets a bulk gene-count matrix at exit 0 — the plausible matrix, strictly worse than "
        f"the refusal this read set was added to remove. Do not widen the tie band, and do not add an "
        f"R1 support to open a margin (#257: every one of them is a strict liability)."
    )


@pytest.mark.xdist_group("kb-probes")
def test_a_family_node_recognizes_its_children_and_no_one_else(kb_probes: KbProbes) -> None:
    """Self-testing, for an ABSTRACT family: it proves itself by RECOGNITION, not by round-trip.

    A family node has no runnable backend, so `spec -> synth -> probe -> recover params` is meaningless.
    Its contract instead: (1) accept EVERY child's reads at rungs 0-2, so a prior that names the family
    can descend into it and reach whichever leaf the bytes pick; (2) claim no foreign data — never
    recognise a leaf outside itself unless it SAYS it does, so the loose classifier never silently takes
    another assay's reads (the `bulk`-accepts-everything trap, at the family level).

    Half (2) used to be the flat "reject every non-descendant leaf", and this test's own comment recorded
    why it held: "purely by the 26-28 bp R1 length gate — no cross-family edge needed". `10x-5p-gex` is
    the case that needs one. 5' reads ARE 3' reads to every cheap probe — same 16 bp CB, same 26/28 bp
    barcode read, same open-ended cDNA mate — so no length gate can separate the two families, and one
    tightened enough to try would start rejecting the family's own children.

    Recognition is therefore not the thing to forbid; UNDECLARED recognition is. A family that reaches
    into another family's data must carry a `confusable_with` edge naming that leaf or one of its
    ancestors — which is the same rule the leaf-level under-declaration sweep applies one level down,
    and it keeps the original trap intact: `bulk-rnaseq` is declared by nobody, so a family that
    started accepting bulk reads still turns this red.
    """
    from seqforge.resolve.confuse import accepts_at_rungs_0_2

    tree = kb.load_tree()
    families = [i for i in tree.specs if tree.is_family(i)]
    assert families, "expected at least one family node (10x-3p-gex)"
    for fam in families:
        fam_spec = tree.specs[fam]
        for child in tree.children_of(fam):
            assert accepts_at_rungs_0_2(fam_spec, kb_probes[child, "full"]), (
                f"family {fam!r} must recognize its child {child!r} at rungs 0-2"
            )
        descendants = set(tree.runnable_descendants_of(fam))
        declared = {c.id for c in fam_spec.confusable_with}
        for other in tree.leaves():
            if other in descendants:
                continue
            if not accepts_at_rungs_0_2(fam_spec, kb_probes[other, "full"]):
                continue
            assert declared & {other, *tree.ancestors_of(other)}, (
                f"family {fam!r} recognizes non-descendant {other!r} at rungs 0-2 and declares "
                f"nothing about it — it would claim foreign data silently. Either tighten the "
                f"family's gates or declare a confusable edge to {other!r} (or to its family) "
                f"naming the mechanism that decides between them."
            )


# ---------------------------------------------------------------- the mechanism must be able to fire


def test_the_splitseq_rounds_are_one_barcode_set() -> None:
    """SPLiT-seq reuses ONE 96 x 8 bp set across all three rounds — a KB fact, not a packing accident.

    It falls out of the derivation: the round-1 RT, round-2 and round-3 ligation oligos in the paper's
    Supplementary Table S12 carry the same 96 barcodes in the same well order, and only their flanking
    sequences differ. Pinned because it is load-bearing in both directions — a future edit that packs
    three *different* lists has either found a source we did not, or corrupted one of them, and either
    way this should stop it and be argued rather than absorbed.

    The three registry names are kept distinct even so: the spec declares three, `CB_UMI_Complex`
    takes three whitelist paths, and a chemistry that later diverges per round needs somewhere to say
    so.
    """
    from seqforge.io import DEFAULT_REGISTRY

    rounds = [DEFAULT_REGISTRY.get(f"splitseq-round{i}") for i in (1, 2, 3)]
    for i, onlist in enumerate(rounds, 1):
        assert onlist.n_entries == 96, f"round{i}: SPLiT-seq is 96 barcodes per round"
        assert onlist.width == 8, f"round{i}: 8 bp per round barcode"
        assert onlist.orientation == "forward", (
            f"round{i}: read 2 reads the oligo's own orientation — the round-3 oligo's read-2 primer "
            "is followed directly by UMI then barcode, so no revcomp is involved"
        )
    assert rounds[0].sha256 == rounds[1].sha256 == rounds[2].sha256, (
        "the three rounds draw on one barcode set; three different lists means the source changed"
    )
    assert all(r.uri.endswith("aam8999_tables12.xlsx") for r in rounds), (
        "each round is pinned to the paper's own Supplementary Table S12, not to a secondary copy"
    )


def _onlists_that_would_decide(spec: Spec) -> list[str]:
    """The registry names a spec's own rung-3 claim depends on.

    An onlist referenced only by an `excludes` anti-gate is a detection probe, not a decider, so it
    is not counted — the same distinction `_build_onlists` already draws in `fill`.
    """
    used = {el.onlist for read in spec.reads for el in read.elements if el.onlist}
    return sorted({spec.onlists[alias].registry for alias in used})


def test_a_spec_that_calls_onlists_decisive_can_actually_reach_one() -> None:
    """The gap this repo could not see: a KB entry declaring what the code cannot execute.

    Adding a technology really is one YAML file and zero Python — SPLiT-seq proves it. But a spec can
    *declare* a mechanism that does not exist, and that fails SILENTLY: the tests go unconfirmed,
    nothing is red, and the chemistry loses. This is the check that makes the declaration cost
    something.

    **There is no debt list any more, and deleting it is the point** (#321). This assertion used to
    compare against a recorded `UNSHIPPED_ONLIST_DEBT`, so a spec could land without its whitelist as
    long as somebody wrote the gap down. The comment beside that pin told the next author the failure
    was tolerable — "it over-asks, it does not answer wrongly" — and that was false. Measured on reads
    built from the barcodes we now ship, `splitseq` with its three lists withheld scores **0.3300**
    against `bulk-rnaseq`'s **0.7800** on its own data: its onlist supports carry 9 of its 10
    barcode-role weight and keep that weight when unconfirmed, deliberately (#307), so at +0.45 the
    chemistry sits far outside θ, never joins the tie set its own declared `confusable_with` edge
    would be consulted for, and the deposit compiles to a bulk gene-count matrix at exit 0. With all
    three shipped the same reads tie 0.7800/0.7800 and the edge routes the decision to the onlist,
    which hits.

    So the debt was never a deferral, it was a silently wrong answer with a note attached — and a note
    is a rule somebody has to remember. Removing the escape hatch makes the right thing happen by
    default: ship the whitelist, or do not ship the spec. `splitseq` is the precedent for paying that
    cost rather than deferring it — its three lists were derived from the paper's own Supplementary
    Table S12, and `test_the_splitseq_rounds_are_one_barcode_set` pins what the derivation found.

    Do NOT close a future gap by guessing barcodes. A wrong whitelist does not fail loudly: STARsolo
    exits 0 and emits a matrix that merely looks like a thin dataset, and a plausible matrix is
    unrecoverable in a way a refusal never is.
    """
    from seqforge.io import DEFAULT_REGISTRY

    gaps = _onlist_gaps(DEFAULT_REGISTRY)
    assert gaps == {}, (
        f"these specs call the onlist decisive and cannot reach one: {gaps}\n"
        "A spec whose decisive whitelist does not ship LOSES SILENTLY to `bulk-rnaseq` — measured, "
        "`splitseq` without its three lists scores 0.3300 against bulk's 0.7800 on its own reads, "
        "far outside the tie band, so nothing asks and the deposit compiles as bulk at exit 0. It "
        "does not over-ask; it answers wrongly.\n"
        "Ship the whitelist (`seqforge io onlist pack`) or do not ship the spec. There is no safe "
        "third option, and recording the gap instead of closing it was the one this test used to "
        "allow (#321)."
    )


def _onlist_gaps(registry: OnlistRegistry) -> dict[str, list[str]]:
    """spec id -> the decisive onlists ``registry`` cannot reach. Empty is the only legal answer."""
    gaps: dict[str, list[str]] = {}
    for spec_id in kb.list_spec_ids():
        spec = kb.load_spec(spec_id)
        if "onlist" not in spec.decidable_by:
            continue
        missing = [n for n in _onlists_that_would_decide(spec) if not registry.has(n)]
        if missing:
            gaps[spec_id] = missing
    return gaps


def test_the_unshipped_onlist_guard_can_actually_go_red() -> None:
    """Prove the guard above fires, now that there is no way to record your way past it.

    It has only ever been seen green, and a guard nobody has watched fail is a guard nobody knows is
    connected — the same reason `test_the_orphan_exemption_is_not_a_blanket_one` and
    `test_a_declared_twin_that_diverges_would_be_caught` perturb their subjects in memory. It matters
    more here than it did before #321: the assertion used to compare against a recorded pin, so an
    author who tripped it had a green-making edit available and would have found out the guard worked.
    Now the only way past is to ship the list, and nobody will discover the wrong `decidable_by` by
    accident.

    Withhold SPLiT-seq's three rounds from an otherwise-complete registry — exactly the state the KB
    was in before those lists were derived — and the guard must name that spec and those three lists,
    and nothing else.
    """
    from seqforge.io import DEFAULT_REGISTRY
    from seqforge.io.onlist import shipped_entries

    withheld = OnlistRegistry(offline=True)
    for entry in shipped_entries():
        if not entry.name.startswith("splitseq-"):
            withheld.register(entry)
    assert len(withheld.names()) == len(DEFAULT_REGISTRY.names()) - 3, (
        "the perturbation must remove exactly SPLiT-seq's three rounds"
    )

    assert _onlist_gaps(withheld) == {
        "splitseq": ["splitseq-round1", "splitseq-round2", "splitseq-round3"]
    }, (
        "the guard must name the spec AND the lists it cannot reach, or its message cannot be acted on"
    )


def test_only_the_plate_entry_says_a_sample_is_a_cell_and_it_is_the_one_that_sets_a_read_floor() -> (
    None
):
    """`sample_is_cell: False` / `min_input_reads: None` are the defaults, so ONE spec.yaml declares.

    That is the regression bar for the whole plate mechanism, and it stayed cheap by construction
    rather than by measurement: sixteen of the seventeen shipped entries keep the file they had, and
    the reduction's cell gate is provably inert on every dataset none of them describes
    (`reduce_dataset`'s companion test asserts the other half — that inert means the byte-for-byte
    old path). `smartseq3` is the entry the mechanism was built for, so it is the one exception and
    it is named here rather than counted: a second id appearing in this set is a second plate
    chemistry, which is a thing to argue for, not a diff.

    **Both fields are declared on that entry, and only ONE direction of that is a rule.** They answer
    different questions — one names what the technology IS, the other is an admission threshold that
    names no technology. A chemistry whose `Sample` IS a cell must also say how thin a cell may be,
    or its starved wells dissent instead of abstaining, so that half is asserted. The converse is
    **not**, and this test used to demand it: `compose.admission` says plainly that a chemistry *may*
    declare a read floor, and one declared beside an ordinary chemistry drops *samples* rather than
    *cells* — a supported case `test_a_floor_on_a_chemistry_whose_sample_is_not_a_cell_drops_samples_and_says_so`
    exercises. It has no shipped instance today, which is exactly how a test comes to forbid a
    permitted shape without anyone noticing.

    The `declaring` bar below is not that rule wearing a disguise. It says what the KB SHIPS — one
    entry departs from both defaults — and a floor-only entry appearing in it is a change to argue
    for on its own terms, not a shape the schema refuses. The assertion dropped here was the other
    thing: a claim about what a spec is ALLOWED to say.

    Read off the FILES and not off the loaded model, because a default is exactly what a model read
    cannot distinguish from a declaration.

    `Spec` is deliberately absent from `SCHEMA_MODELS`, so neither field moves `schema export`. The
    KB schema is Pydantic so that one executable validator also self-tests every entry — not because
    anything on the wire or at the model seam consumes it; a spec.yaml is human-authored and
    CI-validated, and no model writes one. What holds "the schema export is the schema" for the KB is
    `kb lint` and the round-trip, and this assertion sits here so a reader looking for the golden
    that did not move finds the reason rather than the absence.
    """
    from seqforge.kb.loader import SPECS_DIR
    from seqforge.models import SCHEMA_MODELS

    assert "Spec" not in SCHEMA_MODELS
    assert Spec.model_fields["min_input_reads"].default is None
    assert Identity.model_fields["sample_is_cell"].default is False

    declaring: set[str] = set()
    for spec_id in kb.list_spec_ids():
        raw = yaml.safe_load((SPECS_DIR / spec_id / "spec.yaml").read_text())
        declares = "min_input_reads" in raw or "sample_is_cell" in raw["identity"]
        spec = kb.load_spec(spec_id)
        if not declares:
            assert spec.min_input_reads is None and not spec.identity.sample_is_cell, spec_id
            continue
        declaring.add(spec_id)
        # Each field straight off the file it was written in: what the loaded model carries, it
        # carries because the entry said so and not because the schema defaulted it.
        assert raw.get("min_input_reads") == spec.min_input_reads, spec_id
        assert raw["identity"].get("sample_is_cell", False) == spec.identity.sample_is_cell, spec_id
        if spec.identity.sample_is_cell:
            assert spec.min_input_reads is not None, (
                f"{spec_id} says one Sample of it IS a cell but declares no read floor. A plate "
                f"whose starved wells have no threshold to fall under dissents instead of "
                f"abstaining, and every one of them compiles."
            )

    assert declaring == {"smartseq3"}, (
        f"the plate fields are declared by {sorted(declaring)}, not by the one plate entry. Every "
        f"other shipped chemistry demultiplexes in the read, so a sample of it is a library and not "
        f"a cell — and a floor beside one of those would drop libraries nobody asked to drop."
    )


def test_a_cell_is_a_sample_only_beside_a_module_that_counts_them_together() -> None:
    """The biconditional, fired in both directions on a spec built to violate it.

    `identity.sample_is_cell` is true **iff** the spec's module declares a dataset-scoped fan-in
    artifact, and both halves are live wrong answers rather than tidiness. A cell-is-a-sample
    chemistry beside a per-sample module compiles a 1440-well plate to 1440 separate objects at exit
    0. A fan-in module beside a chemistry that stays silent leaves the dataset reduction with nothing
    to tell "a plate whose cells must not be split apart" from "a project that mixes two assays", so
    one dissenting well partitions the deposit into two manifests — again at exit 0.

    It fires where every other DSL mistake in this file dies: at LOAD, which is `load_spec`, `kb
    lint`, and every test that touches a spec. Driven through `Spec.model_validate` over a real
    shipped entry with one field changed, so what is under test is the rule and not a fixture.
    """
    from seqforge.kb.loader import SPECS_DIR

    raw = yaml.safe_load((SPECS_DIR / "10x-3p-gex-v3" / "spec.yaml").read_text())
    assert raw["backend"]["module"] == "map/starsolo"  # per-sample end to end

    with pytest.raises(ValidationError, match="is per-sample end to end"):
        Spec.model_validate({**raw, "identity": {**raw["identity"], "sample_is_cell": True}})

    # ...and the other way round: the aggregating module, with the chemistry saying nothing. The
    # backend's params have to go with it — this module parses nothing, because its whole geometry is
    # derived — which is the second half of "parse keys are empty" arriving as a load-time refusal.
    plate = {**raw, "backend": {"module": "map/star-umi", "params": {}}}
    with pytest.raises(ValidationError, match="does not declare identity.sample_is_cell"):
        Spec.model_validate(plate)

    # Both together is the legal pairing, and asserting it is what stops the rule from being one that
    # merely refuses everything.
    ok = Spec.model_validate({**plate, "identity": {**raw["identity"], "sample_is_cell": True}})
    assert ok.identity.sample_is_cell and ok.require_backend().module == "map/star-umi"


def test_no_spec_may_name_a_chimeric_twin_and_the_guard_set_is_derived_from_the_registry() -> None:
    """A twin is reachable by being SWAPPED IN, and a spec naming one takes a route that never asks.

    Compose selects a chimera-aware twin when the recipe's assembly is spelled like a **chimera**'s,
    by swapping it in for the pipeline the chemistry already binds to. A spec naming the twin
    directly reaches the same module without that question ever being asked — so an ordinary,
    single-assembly recipe would compile to a pipeline that splits a BAM by a suffix nothing put
    there, and the run would die deep in a fan-in rather than at compose. One dispatch rule, and this
    is what keeps it the only one.

    The set is DERIVED from the module registry, so the day a second base declares a twin the refusal
    covers it without anyone writing its name down: a hand-kept list is one a new twin is missing
    from, and the guard would pass while guarding nothing. That derivation is the second assertion
    below — every id the set holds is refused, so the loop covers a second twin the day one arrives
    and a refusal wired to one hardcoded id would go red here instead of passing quietly. What is NOT
    asserted is the comprehension that builds the set: restating it would check the source line
    against itself, and the registry test next door already pins the membership by name.

    It fires at LOAD, where every other DSL mistake in this file dies, and on the BACKEND — before
    the cell-axis biconditional a few validators along gets to speak, so a spec naming the plate twin
    is told the real reason rather than being asked for a flag it may not be entitled to anyway.
    """
    from seqforge.kb.loader import SPECS_DIR
    from seqforge.workflows import CHIMERIC_VARIANTS

    assert CHIMERIC_VARIANTS, "no module declares a twin, so this rule refuses nothing"

    raw = yaml.safe_load((SPECS_DIR / "10x-3p-gex-v3" / "spec.yaml").read_text())
    for twin in sorted(CHIMERIC_VARIANTS):
        with pytest.raises(ValidationError, match="chimera-aware twin"):
            Spec.model_validate(
                {
                    # The cell axis is declared too, so what refuses the spec is this rule and not
                    # the biconditional that would also have something to say about a fan-in module.
                    **raw,
                    "identity": {**raw["identity"], "sample_is_cell": True},
                    "backend": {"module": twin, "params": {}},
                }
            )


def test_a_backend_on_the_plate_module_may_declare_no_parse_key_at_all() -> None:
    """Its parse namespace is EMPTY, so any declared key is refused at load — including a real one.

    Every number the extractor needs is already in the element coordinates, so the whole geometry is
    DERIVED into one config key rather than declared. That makes "a user instruction contradicts the
    observed bytes" inexpressible for this pipeline by construction: there is nothing to write.

    The refused keys below are *valid* keys of another pipeline, which is the case that matters — the
    namespace is per pipeline, so a plausible-looking knob copied from a neighbouring spec must not
    quietly become this backend's. `clip5pAdapterSeq` is the one worth naming beside a `solo*` offset:
    it states a fact about the MOLECULE rather than about STARsolo's geometry, so it is the key a
    plate entry would most reasonably reach for, and it stays starsolo-only until a second module can
    honour a five-prime override. The shape that DID earn a reader in two modules, `read_through`, is
    a top-level field rather than a param for exactly that reason.
    """
    from seqforge.kb.loader import SPECS_DIR

    raw = yaml.safe_load((SPECS_DIR / "10x-3p-gex-v3" / "spec.yaml").read_text())
    plate = {
        **raw,
        "identity": {**raw["identity"], "sample_is_cell": True},
        "backend": {"module": "map/star-umi", "params": {"soloUMIlen": 8}},
    }
    for params in ({"soloUMIlen": 8}, {"clip5pAdapterSeq": "AAGCAGTGGTATCAACGCAGAGTGAATGGG"}):
        with pytest.raises(ValidationError, match=r"does not parse"):
            Spec.model_validate({**plate, "backend": {"module": "map/star-umi", "params": params}})

    # Nor may it declare the derived key itself, which is the same refusal reached from the other
    # side: `read_structure` is not in this pipeline's parse namespace either, because it is not
    # declarable anywhere at all.
    with pytest.raises(ValidationError, match=r"does not parse"):
        Spec.model_validate(
            {**plate, "backend": {"module": "map/star-umi", "params": {"read_structure": "x"}}}
        )


def test_a_read_floor_of_zero_is_a_gate_that_cannot_fire() -> None:
    """`min_input_reads: 0` admits everything while reading as a threshold somebody chose.

    `None` already says "admit everything" and says it once; a zero would be a second spelling of it
    that looks like a decision, which is the shape a reviewer cannot check. So the schema refuses it
    where the DSL is executed, exactly as it refuses a typo'd key.
    """
    from seqforge.kb.loader import SPECS_DIR

    raw = yaml.safe_load((SPECS_DIR / "10x-3p-gex-v3" / "spec.yaml").read_text())
    assert Spec.model_validate({**raw, "min_input_reads": 1000}).min_input_reads == 1000
    with pytest.raises(ValidationError, match="min_input_reads"):
        Spec.model_validate({**raw, "min_input_reads": 0})


def test_a_read_through_needs_a_sequence_and_a_read_that_could_reach_it() -> None:
    """The mosaic end is declared ONCE per chemistry, and only where a read can reach it.

    Two refusals at LOAD, because each describes a block that could not mean anything. A value
    outside `ACGT` is not a molecule an aligner could match, so it would clip nothing while reading
    as a decision somebody made; and a chemistry whose reads carry no genomic span has nothing to
    read THROUGH, so the block would state a fact with no consumer — the shape `decidable_by` was
    deleted for.

    The positive case is the entry that motivated it. Tagmentation cuts at random, so a fragment
    shorter than the read runs off the end of its own cDNA and into the mosaic end; everything
    behind that match is non-genomic too, which is why the value is terminal rather than a span.
    """
    from seqforge.kb.loader import SPECS_DIR

    raw = yaml.safe_load((SPECS_DIR / "smartseq3" / "spec.yaml").read_text())
    assert Spec.model_validate(raw).read_through == "CTGTCTCTTATACACATCT"

    for bad in ["ctgtctcttatacacatct", "CTGTCTCTTATACACAT?T", ""]:
        with pytest.raises(ValidationError, match="read_through"):
            Spec.model_validate({**raw, "read_through": bad})

    # The same entry with every genomic span taken out of its reads: tag, UMI and the motif that
    # closes the tag are all the layout has left, and none of them is a span a fragment can run past.
    no_cdna = {
        **raw,
        "reads": [
            {**r, "elements": [e for e in r["elements"] if e["type"] not in ("cdna", "gdna")]}
            for r in raw["reads"]
        ],
    }
    with pytest.raises(ValidationError, match="no read carries cdna or gdna"):
        Spec.model_validate(no_cdna)


@pytest.mark.parametrize(
    ("trimmer", "read_through", "five_prime_override", "refused"),
    [
        ("CellRanger4", "CTGTCTCTTATACACATCT", None, "three-prime clip"),
        ("Hamming", "CTGTCTCTTATACACATCT", None, None),
        ("Hamming", None, "AAGCAGTGGTATCAACGCAGAGTGAATGGG", "five-prime clip"),
        ("CellRanger4", None, "AAGCAGTGGTATCAACGCAGAGTGAATGGG", None),
        ("None", None, None, "knows no end for"),
    ],
)
def test_a_declared_clip_must_sit_at_an_end_its_declared_trimmer_takes(
    trimmer: str,
    read_through: str | None,
    five_prime_override: str | None,
    refused: str | None,
) -> None:
    """The trimmer a chemistry names decides which END of a read a clip may be declared at.

    Not a preference and not a wasted flag: the trimmer that will not take an adapter at that end
    refuses the whole run at parameter initialization, before the genome is loaded, so a spec pairing
    them wrong kills every sample of the deposit after its queue wait, over a flag nobody typed. That
    is why the pairing is refused at LOAD — and why it is refused *here*, since one half of it is a
    backend param and the other is top level, and the spec is the only thing that sees both.

    Both refusals are exercised, and so are both legal pairings, because the two modes are exactly
    complementary: a rule that refused every clip would pass the refusal rows on its own and be
    indistinguishable from this one. The last row is the trimmer nobody can check — STAR's shipped
    help still advertises an option its code rejects — and it is refused rather than skipped, since
    skipping would turn the rule off for exactly the entry that got the trimmer wrong.
    """
    from seqforge.kb.loader import SPECS_DIR

    raw = yaml.safe_load((SPECS_DIR / "10x-3p-gex-v3" / "spec.yaml").read_text())
    params = {**raw["backend"]["params"], "clipAdapterType": trimmer}
    if five_prime_override is not None:
        params["clip5pAdapterSeq"] = five_prime_override
    candidate = {
        **raw,
        "backend": {**raw["backend"], "params": params},
        "read_through": read_through,
    }
    if refused is None:
        legal = Spec.model_validate(candidate)
        assert legal.read_through == read_through
        assert legal.require_backend().params.get("clip5pAdapterSeq") == five_prime_override
        return
    with pytest.raises(ValidationError, match=refused):
        Spec.model_validate(candidate)


@pytest.mark.parametrize(
    ("family", "expected"),
    [
        ("bd-rhapsody", "A" * 38),
        ("10x-5p-gex", revcomp("TTTCTTATATGGG")),
    ],
)
def test_leaves_that_share_a_cdna_read_share_one_derivable_read_through(
    family: str, expected: str
) -> None:
    """One sequence per family, and in neither case can a reviewer check the literal by eye.

    A family whose leaves are identical on the cDNA read gets one answer for all of them, so any
    drift between siblings is this sweep's to catch. What makes it worth its lines is that the
    expected value is DERIVED here rather than copied off the entry — the same reason neither value
    was hand-typed into the files:

    * BD hands STAR a fixed run of A's, byte-identical across its pipeline 2.2.1 / 2.3 / 2.4b / 3.0.
      A run one base short reads identically and is wrong at exit 0: STAR clips a shorter tail than
      the molecule carries, and every cell keeps paying the remainder inside the length-relative
      filter. STAR's ``polyA`` *keyword* is a different and more aggressive clip — a run as long as
      the read — and is explicitly not what BD passes, which is why this is a sequence and not a mode.
    * The 10x 5' anchor is the REVERSE COMPLEMENT of the template-switch tail the gel-bead primer
      ends in, and that relationship is the claim. The entry derives the primer in full for its
      strand call and deliberately does not restate the anchor beside it, so the two are written
      down once each and joined here; transcribing a complement by eye is exactly the error this
      catches. Both 5' leaves declare it because prevalence is a property of a LIBRARY rather than of
      a kit — measured from 0.094% to 10.41% across five 5' libraries against 0.0000% on 3' — so
      declaring it only where it happened to be common would file a library property as a chemistry
      one.

    Collected from the loader rather than from a roster, so a new leaf of either family is covered
    because it exists; each row asserts it matched something, because a renamed id would otherwise
    empty the sweep and pass.
    """
    found = [t for t in kb.runnable_spec_ids() if t.startswith(family)]
    assert found, f"the {family} sweep matched no entry, so it proves nothing about the value below"
    for tech in found:
        declared = kb.load_spec(tech).read_through
        assert declared == expected, (
            f"{tech}: read_through is {declared!r}, not the {expected!r} this family's cDNA read "
            f"runs into. The expected value is derived here and never copied from the entry, so a "
            f"hand-edited literal that reads plausibly is what goes red"
        )


def test_decidable_by_is_derived_from_the_confusables_not_typed_beside_them() -> None:
    """It was a hand-typed field on every spec, read by nothing, with a comment claiming CI computed it.

    `escalate` builds a Question's decidable_by from `confusable_with[].distinguishable_by` inline —
    the very union the comment described — so the field caused no behaviour and was free to drift.
    That is `RegistryEntry.fetchable` again, and `required_config` before it.
    """
    assert "decidable_by" not in Spec.model_fields
    for spec_id in kb.list_spec_ids():
        spec = kb.load_spec(spec_id)
        expected = sorted(
            {
                m
                for c in spec.confusable_with
                if c.relationship == "processing_divergent"
                for m in c.distinguishable_by
                if m != "none"
            }
        )
        assert spec.decidable_by == expected


def test_writing_a_decidable_by_into_a_spec_is_now_an_error() -> None:
    """Deriving it is only half the fix. The other half is that you cannot re-declare it.

    `Spec` forbids extra keys, so a spec.yaml carrying `decidable_by:` fails to load rather than
    being silently ignored beside the property that replaced it — which is exactly how a
    hand-maintained contract comes back.
    """
    from seqforge.kb.loader import SPECS_DIR

    raw = yaml.safe_load((SPECS_DIR / "10x-3p-gex-v3" / "spec.yaml").read_text())
    Spec.model_validate(raw)  # the real spec loads
    with pytest.raises(ValidationError, match="decidable_by"):
        Spec.model_validate({**raw, "decidable_by": ["onlist"]})


def test_a_spec_with_no_divergent_confusable_is_decidable_by_nothing() -> None:
    """Not a bug: nothing to decide. Equivalent twins are recorded together, never chosen between.

    Every shipped spec now carries at least one *divergent* confusable (v2 gained its over-length
    v2<->v3 edge, and the rest always had one), so the property is tested on a spec stripped to its
    equivalent-only confusables — which is exactly the shape it is asserting about, derived not typed.
    """
    v31 = kb.load_spec("10x-3p-gex-v3.1")
    equiv_only = v31.model_copy(
        update={
            "confusable_with": [
                c for c in v31.confusable_with if c.relationship == "processing_equivalent"
            ]
        }
    )
    assert equiv_only.confusable_with  # non-vacuous: it keeps the v3 twin
    assert all(c.relationship == "processing_equivalent" for c in equiv_only.confusable_with)
    assert equiv_only.decidable_by == []


# ---------- resolve_chemistry: what a prose chemistry string NAMES in the KB ----------
#: Every chemistry string the benchmark corpus is known to produce — a value a model emitted into a
#: `library.chemistry` draft, or a phrase a record carries verbatim — and the node it must name.
#: Measured against the live KB before the matcher was written (#184); pinned here so the measurement
#: is a test rather than a paragraph.
_CORPUS_STRINGS: list[tuple[str, str | None]] = [
    # The bug this matcher exists to close: SRA writes `library_strategy: RNA-Seq` on every
    # transcriptomic run, and the old substring rule read that as the bulk paired-end chemistry.
    ("RNA-Seq", None),
    # A trademark glued to the next word by PDF extraction. `BD Rhapsody` is still carried by it.
    ("BD Rhapsody™Whole Transcriptome Analysis Kit", "bd-rhapsody-wta"),
    # A real kit that is not in the KB at all. Naming no node is the honest answer.
    ("SMARTer Stranded Total RNA-seq Pico Input Mammalian V2 kit (Takara Bio)", None),
    ("10x-3p-gex-v3.1", "10x-3p-gex-v3.1"),
    # The improvement: a whole methods sentence carries the leaf's alias, and now reaches it.
    (
        "10X Genomics Chromium X system using the Single Cell 3' v3.1 Reagent Kits",
        "10x-3p-gex-v3.1",
    ),
    # "Chromium" plus a generic assay word names a vendor, not a chemistry: 3' and 5' are the same
    # sentence and different pipelines.
    ("Chromium single-cell RNA-sequencing", None),
    ("10x 5'", "10x-5p-gex"),
]

#: The four hypothesis strings the eval corpus steers with. They resolve where `_match_tech` put
#: them, which is what protects the deterministic (`--no-llm`) tier from this change.
_CORPUS_HYPOTHESES: list[tuple[str, str]] = [
    ("10x 5'", "10x-5p-gex"),
    ("10x-3p-gex-v2", "10x-3p-gex-v2"),
    ("10x-3p-gex-v3", "10x-3p-gex-v3"),
    ("bulk-rnaseq", "bulk-rnaseq"),
]


def _resolved_id(value: str) -> str | None:
    spec = kb.resolve_chemistry(value)
    return None if spec is None else spec.identity.id


@pytest.mark.parametrize(("value", "expected"), _CORPUS_STRINGS)
def test_every_chemistry_string_the_corpus_produces_names_what_it_was_measured_to_name(
    value: str, expected: str | None
) -> None:
    assert _resolved_id(value) == expected


@pytest.mark.parametrize(("value", "expected"), _CORPUS_HYPOTHESES)
def test_the_corpus_hypotheses_land_where_the_matcher_they_replace_put_them(
    value: str, expected: str
) -> None:
    assert _resolved_id(value) == expected


def test_a_generic_strategy_word_names_no_chemistry() -> None:
    """The whole point: an archive's own strategy/platform vocabulary is not a chemistry claim.

    Each of these resolved to a real KB node under the old two-directional substring rule — `RNA-Seq`
    and `Illumina` to `bulk-rnaseq` (via the alias "Illumina PE RNA-seq"), `transcriptome` and
    `WTA` to `bd-rhapsody-wta` (via "…Whole Transcriptome Analysis") — because the *needle* was
    allowed to sit inside the alias. A term that names a whole field of assays entails no chemistry,
    and no amount of it appearing in a curated alias makes it one.
    """
    for value in (
        "RNA-Seq",
        "Illumina",
        "transcriptome",
        "WTA",
        "single-cell RNA-seq",
        "10X Genomics v2 chemistry",  # names a vendor and a version, but no assay end
        "",
        "   ",
    ):
        assert kb.resolve_chemistry(value) is None, value


def test_chemistry_matching_is_one_directional() -> None:
    """`alias ⊆ needle` narrows; `needle ⊆ alias` is vacuous, and it is the direction that was wrong.

    A value carrying a curated alias says at least what the alias says. A value that is merely a
    FRAGMENT of an alias says less than it — "RNA-seq" is inside "bulk RNA-seq" and inside
    "paired-end RNA-seq", and inside a hundred other kit names nobody has curated. Only the first
    direction is entailment.
    """
    assert _resolved_id("libraries were built with the bulk RNA-seq protocol") == "bulk-rnaseq"
    assert kb.resolve_chemistry("RNA-seq") is None
    assert kb.resolve_chemistry("Rhapsody") is not None  # a whole alias, short but curated
    assert kb.resolve_chemistry("Rhap") is None  # ...and a fragment of one is not


def test_a_leaf_alias_outranks_the_family_alias_it_contains() -> None:
    """Tie-break: most alias tokens matched, which picks the leaf over its own family node.

    Every leaf's spelling contains its family's ("10x 3' v3" carries "10x 3'"), so both match and the
    winner has to be the more specific claim. The reverse — a bare family term — must still stop at
    the family, because that is all the prose said.
    """
    assert _resolved_id("10x 3' v3") == "10x-3p-gex-v3"
    assert _resolved_id("10x 3'") == "10x-3p-gex"
    assert _resolved_id("10x-3p-gex-v3.1") == "10x-3p-gex-v3.1"  # not the v3 prefix inside it


#: Values carrying BOTH a chemistry's own name and a phrase that only describes the sequencing
#: format. Measured against the shipped KB in #266: every one of them resolved to `bulk-rnaseq`.
_NAMED_AND_DESCRIBED: list[tuple[str, str]] = [
    ("SPLiT-seq", "splitseq"),  # the control: the name alone always worked
    ("SPLiT-seq paired-end RNA-seq", "splitseq"),
    ("10x 3' v3 paired-end RNA-seq", "10x-3p-gex-v3"),
    ("BD Rhapsody paired-end RNA-seq", "bd-rhapsody-wta"),
    ("SPLiT-seq, polyA RNA-seq PE", "splitseq"),
    ("single cell RNA-seq (SPLiT-seq), Illumina PE RNA-seq", "splitseq"),
]


@pytest.mark.parametrize(("value", "expected"), _NAMED_AND_DESCRIBED)
def test_a_phrase_that_only_describes_the_format_never_outranks_a_chemistrys_own_name(
    value: str, expected: str
) -> None:
    """A value that both names an assay and describes how it was run names the ASSAY (#266).

    "SPLiT-seq paired-end RNA-seq" says *a SPLiT-seq library, sequenced paired-end*: one clause names
    the chemistry, the other describes the run. Ranking a matched form by its significant-token count
    read that backwards — "paired-end RNA-seq" is four tokens against `SPLiT-seq`'s two, so the
    wordier DESCRIPTION beat the NAME and all five of these landed on `bulk-rnaseq`. Verbosity is
    not specificity, and the tree cannot settle it either: `bulk-rnaseq` and `splitseq` are both
    root leaves, so neither is the other's ancestor. Which forms only describe is a fact about the
    entry, so the entry declares it (`identity.descriptive_aliases`).
    """
    assert _resolved_id(value) == expected


def test_a_descriptive_phrase_still_names_the_bulk_entry_when_nothing_else_is_named() -> None:
    """The other half of #266: a demotion, not a deletion.

    A `descriptive_alias` still reaches its node — it just loses to any form that NAMES one. Dropping
    the four phrases instead would refuse a real bulk record that describes itself the only way an
    archive ever does, which is the over-strictness #184 measured and rejected. `bulk RNA-seq` names
    the chemistry rather than describing a format, so it stays a first-class alias and wins alone.
    """
    assert _resolved_id("Illumina PE RNA-seq") == "bulk-rnaseq"
    assert _resolved_id("polyA RNA-seq PE") == "bulk-rnaseq"
    assert _resolved_id("RNA-seq PE") == "bulk-rnaseq"
    assert _resolved_id("bulk RNA-seq") == "bulk-rnaseq"
    assert (
        kb.resolve_chemistry("RNA-seq") is None
    )  # ...and a bare strategy word still names nothing


def test_a_real_kit_name_resolves_where_a_strict_alias_table_would_refuse() -> None:
    """Why the repair is entailment and not exact-alias matching (measured in #184).

    Requiring an exact KB id/name/alias rejects every realistic prose spelling — a paper writes
    "Chromium Next GEM Single-Cell 5' Reagent Kit v2", never `10x-5p-gex-v2`. That would have closed
    the metadata-hypothesis channel in production while the benchmark stayed green on its recipe
    hypotheses: the harness failing differently from the product, which is the trap this whole line
    of work opens with.
    """
    assert _resolved_id("Chromium Next GEM Single-Cell 5' Reagent Kit v2") == "10x-5p-gex-v2"
    assert _resolved_id("Chromium Single Cell 3' v3") == "10x-3p-gex-v3"
    # the GEM-X trap: "GEM-X" alone is 3' or 5', and the sentence says which
    assert _resolved_id("10x Genomics Chromium GEM-X Single Cell 5' Chip v3") == "10x-5p-gex-v3"


def test_chemistry_matching_does_not_depend_on_the_order_specs_were_loaded_in() -> None:
    """A KB addition must never re-point an unrelated dataset — `run_id` folds the chemistry.

    The rule it replaces returned the FIRST dict-order match, so `WTA` named `bd-rhapsody-wta` only
    because that directory sorts before `bd-rhapsody-wta-enhanced`; loading the KB the other way
    round moved it. Scoring every candidate and breaking the tie on (alias tokens, id) makes the
    answer a property of the strings, and the same either way.
    """
    specs = kb.load_all_specs()
    reversed_specs = dict(reversed(list(specs.items())))
    for value in [*(v for v, _ in _CORPUS_STRINGS), "Rhapsody Enhanced", "WTA", "10x 3' v3"]:
        forward = kb.resolve_chemistry(value, specs)
        backward = kb.resolve_chemistry(value, reversed_specs)
        assert (forward is None) == (backward is None), value
        if forward is not None and backward is not None:
            assert forward.identity.id == backward.identity.id, value
    assert _resolved_id("Rhapsody Enhanced") == "bd-rhapsody-wta-enhanced"


#: Every string in the KB's OWN vocabulary whose resolution #266 moves, and what it moves to.
#: Measured, not guessed: 121 single strings (every curated form of every shipped spec, plus the
#: issue's own values) resolved before and after, and these ten differ. The other 111 are unchanged.
_MOVED_BY_266: list[tuple[str, str, str]] = [
    # 1. the defect itself — a described format stops outranking a named chemistry
    ("SPLiT-seq paired-end RNA-seq", "bulk-rnaseq", "splitseq"),
    ("10x 3' v3 paired-end RNA-seq", "bulk-rnaseq", "10x-3p-gex-v3"),
    ("BD Rhapsody paired-end RNA-seq", "bulk-rnaseq", "bd-rhapsody-wta"),
    # 2. a versioned family alias now reaches the leaf that declares it. `SC3Pv2` carries `SC3P` and
    #    scores one significant token either way, so the lower id took it and the FAMILY won — the
    #    reverse of this module's own stated rule, and unreachable by the alias's own owner.
    ("SC3Pv2", "10x-3p-gex", "10x-3p-gex-v2"),
    ("SC3Pv3", "10x-3p-gex", "10x-3p-gex-v3"),
    ("SC3Pv4", "10x-3p-gex", "10x-gemx-3p-v4"),
    ("SC5Pv1", "10x-5p-gex", "10x-5p-gex-v2"),  # the v2 entry declares both, "also covers v1"
    ("SC5Pv2", "10x-5p-gex", "10x-5p-gex-v2"),
    ("SC5Pv3", "10x-5p-gex", "10x-5p-gex-v3"),
    # 3. the one that was a wrong answer rather than a vague one: the GEX arm's own verbatim name
    #    resolved to the ATAC arm, because the ATAC name's tokens are a subset of the GEX name's and
    #    `10x-multiome-atac` sorts first. ATAC and GEX are different pipelines.
    (
        "10x Chromium Single Cell Multiome ATAC + Gene Expression (GEX arm)",
        "10x-multiome-atac",
        "10x-multiome-gex",
    ),
]


@pytest.mark.parametrize(("value", "was", "now"), _MOVED_BY_266)
def test_the_strings_this_rule_moves_are_pinned_where_it_moved_them(
    value: str, was: str, now: str
) -> None:
    """A resolution that moves silently is the hazard; a resolution that moves PINNED is the fix.

    The chemistry folds into `run_id`, so any change to this ranking re-points datasets — which is
    why the module docstring argues about load order at all, and why "the corpus grades are
    unchanged" is not the whole bar. The benchmark tier's digest is byte-identical across this change
    (#231), and it could not have caught these: no shipped case carries any of these strings. So they
    are enumerated from a measured sweep instead, and every one is a node the value names better.
    """
    assert was != now  # non-vacuous: each row is a real move, not a restatement
    assert _resolved_id(value) == now


def test_a_tie_between_two_names_is_not_broken_by_which_one_is_longer() -> None:
    """The #266 tie-break must not re-open #266's own defect one class up.

    "10x 3' v3, bulk RNA-seq" names two chemistries, so component 1 cannot separate them and both
    matched forms carry three significant tokens. Settling that on the longer string hands it to
    "bulk RNA-seq" (12 characters) over "10x 3' v3" (9) — verbosity beating a name again, which is
    the defect, not the fix. Length is only ever evidence when one form is contained in the other,
    and these two share nothing; a form that says strictly more than another wins, and forms that
    say different things fall through to the id.
    """
    assert _resolved_id("10x 3' v3, bulk RNA-seq") == "10x-3p-gex-v3"
    assert _resolved_id("10x 5' v3, bulk RNA-seq") == "10x-5p-gex-v3"


def _named_pool(*names: tuple[str, str]) -> dict[str, Spec]:
    """A pool of ``(tech_id, name)`` nodes carrying no aliases — only their own spelling matches.

    Built off a shipped spec, so the reads and signature are a real entry's rather than a stub; the
    matcher reads nothing but ``identity`` either way. ``model_copy`` does not re-run validation, so
    this is a shaped object and not a claim that the KB would accept these two as specs.
    """
    base = kb.load_all_specs()["bulk-rnaseq"]
    return {
        tech_id: base.model_copy(
            update={
                "identity": base.identity.model_copy(
                    update={"id": tech_id, "name": name, "aliases": [], "descriptive_aliases": []}
                )
            }
        )
        for tech_id, name in names
    }


def test_the_longer_matched_name_breaks_a_tie_not_the_alphabetically_lower_id() -> None:
    """A node has to be reachable by its own name, whatever it sorts next to (#266).

    `tokens()` reads "Smart-seq3xpress" as `['smart', 'seq3xpress']`, so a `Smart-seq3` node matches
    it as a plain substring and scores the same two significant tokens the xpress node's own name
    does. Breaking that tie on the lower id made the xpress entry unreachable by the only string that
    unambiguously names it — and no alias repairs it, because "Smart-seq3 xpress" is not carried by
    the concatenated spelling at all (`{'smart','seq3','xpress'}` ⊄ `{'smart','seq3xpress'}`).

    The longer matched FORM wins instead: still a property of the two strings, so it cannot move with
    the order the KB loaded in. A synthetic pool, because SMART-seq3 has no shipped entry yet — #257
    scoped it to Hagemann-Jensen 2020 and left xpress a future sibling.
    """
    pool = _named_pool(("smartseq3", "Smart-seq3"), ("smartseq3xpress", "Smart-seq3xpress"))
    assert kb.resolve_chemistry_id("Smart-seq3xpress", pool) == "smartseq3xpress"
    assert (
        kb.resolve_chemistry_id("Smart-seq3", pool) == "smartseq3"
    )  # ...and the shorter still its
    reversed_pool = dict(reversed(list(pool.items())))
    assert kb.resolve_chemistry_id("Smart-seq3xpress", reversed_pool) == "smartseq3xpress"
