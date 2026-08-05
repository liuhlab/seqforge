"""Tests for the KB: schema validation, the DSL guards, and the round-trip self-test."""

from __future__ import annotations

import gzip
import random
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import KbProbes, write_fastq_gz
from seqforge import kb
from seqforge.kb.schema import MotifPresent, Read, Spec
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
#: `test_a_linker_one_base_short_of_its_own_coordinates_reddens_the_round_trip`'s assertion, which
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


def test_a_linker_one_base_short_of_its_own_coordinates_reddens_the_round_trip() -> None:
    """The negative direction: the constant check must be able to fail, and this is what makes it.

    The generator writes `el.sequence` and the check reads it back, so a *substituted* base cannot
    redden anything — both halves would move together. What the check really compares is the two
    derivations of WHERE the sequence goes: the generator concatenates elements in order, and the
    check cuts the declared `[start, end)`. Nothing validates that `len(sequence)` agrees with its own
    coordinates, so a typo'd linker shifts every element after it — and that is the failure this
    reproduces, by dropping one base from a shipped entry's linker and watching the schema accept it.

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
    element = next(
        el
        for read in data["reads"]
        if read["id"] == read_id
        for el in read["elements"]
        if el["name"] == el_name
    )
    element["sequence"] = element["sequence"][:-1]  # one base short of the window it declares
    broken = Spec.model_validate(data)  # ...and the schema is happy, which is why this test exists

    checks = kb.roundtrip_checks(broken, n=300)
    constant = [c for c in checks if str(c["check"]).startswith("constant_sequence:")]
    assert constant, f"{tech}: no constant check to fail"
    assert not all(c["ok"] for c in constant), (
        f"{tech}: dropping a base from {el_name!r} left every constant check green — the check is "
        "comparing the declared sequence against itself rather than against the reads."
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

    Derived, not named. What the diluent has to BE is "reads carrying no structure at all, drawn by
    the same generator as the tagged ones", which is a property of a spec and checkable right here;
    an id written into a test is a name that outlives the entry it points at, and the way that fails
    is a floor test quietly diluting with the wrong reads.
    """
    plain = [
        tech
        for tech in kb.list_spec_ids()
        if all(el.type == "cdna" for read in kb.load_spec(tech).reads for el in read.elements)
    ]
    assert len(plain) == 1, f"expected exactly one all-cDNA entry to dilute with, found {plain}"
    return kb.load_spec(plain[0])


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

    This is the same defect `UNSHIPPED_ONLIST_DEBT` was built for, one level up, and it hid in the one
    place that register does not look: that guard reads the onlists a spec's own ELEMENTS reference, so
    it never sees a `distinguishable_by: [onlist]` claim about a *pair*. Four edges pointed at ids with
    no spec directory — `10x-gemx-3p-v4` and `10x-5p-gex`, from both v3 and v3.1.

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

    **The gate on that change: bulk's five declared edges re-derived under it**, since a stronger
    guard that silently drops a true edge has traded noise for blindness. Every one survives, and
    with room to spare — `bulk` on the other spec's own reads, both scored with the onlist withheld:

    | edge | bulk | incumbent | margin |
    |---|---|---|---|
    | `splitseq`                    | 1.0000 | 0.5500 | +0.4500 |
    | `bd-rhapsody-wta`             | 0.9875 | 0.5500 | +0.4375 |
    | `bd-rhapsody-wta-enhanced-v1` | 0.9975 | 0.5500 | +0.4475 |
    | `bd-rhapsody-wta-enhanced-v2` | 0.9975 | 0.5500 | +0.4475 |
    | `10x-multiome-atac`           | 0.8800 | 0.7424 | +0.1376 |

    Re-derive with `resolve.confuse.rung02_margin(specs['bulk-rnaseq'], specs[b], probes[b])`.
    Across the whole shipped KB the two predicates in fact flag the identical pair set: every pair
    that accepts at rungs 0-2 also outranks, because the rest are exact ties (margin 0.0000, the 10x
    28 bp cohort and the two Enhanced beads) and a tie is not a separation. So no edge is gained or
    lost here — the change is about the KB the next entry will make, not the one in the tree.

    **`geometry_could_accept` stays a sound skip**, unchanged, and the argument is a containment one
    rather than a new measurement: outranking REQUIRES a valid assignment, so the new predicate
    implies the old one, and every necessary condition of the old is a necessary condition of the
    new. `length_feasible` is proven necessary for validity (geometry.py), hence necessary here; a
    geometry-NO pair cannot outrank. `test_geometry_could_accept_is_necessary_for_rung02_acceptance`
    holds the premise over every shipped pair.
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
            if not geometry_could_accept(specs[a], kb_probes[b]):
                continue  # proven necessary condition — a length-infeasible pair cannot be confusable
            if could_outrank_at_rungs_0_2(specs[a], specs[b], kb_probes[b]):
                margin = rung02_margin(specs[a], specs[b], kb_probes[b])
                undeclared.append(
                    f"{a!r} could outrank {b!r} on {b!r}'s own reads at rungs 0-2 "
                    f"(margin {margin:+.4f}) but does not list it in confusable_with (nor share a "
                    f"parent) — the resolver would pick one and never ask"
                )
    assert not undeclared, "under-declaration:\n" + "\n".join(undeclared)


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
    own = kb_probes["10x-3p-gex-v3"]
    assert accepts_at_rungs_0_2(spec, own)
    assert not rung02_separable(spec, own, spec, own)  # nothing is separable from itself
    assert could_outrank_at_rungs_0_2(spec, spec, own)
    assert rung02_margin(spec, spec, own) == 0.0

    # ...and it discriminates: splitseq's 94 bp barcode read is not 10x's 28 bp geometry.
    assert not accepts_at_rungs_0_2(spec, kb_probes["splitseq"])
    # A spec that cannot score the data at all ranks NOWHERE on it — `None`, not a negative margin.
    # That is the conjunct which keeps `geometry_could_accept` a sound skip for the new predicate.
    assert rung02_margin(spec, splitseq, kb_probes["splitseq"]) is None
    assert not could_outrank_at_rungs_0_2(spec, splitseq, kb_probes["splitseq"])


@pytest.mark.xdist_group("kb-probes")
def test_bulks_five_declared_edges_still_derive_under_the_ordering_predicate(
    kb_probes: KbProbes,
) -> None:
    """The gate on #275: a stronger guard that silently drops a TRUE edge has traded noise for
    blindness, and that failure is invisible unless it is checked for directly.

    The under-declaration sweep cannot check it — it `continue`s on a declared pair, so every edge in
    the KB is a claim the sweep takes on trust. `bulk-rnaseq` is where the trust is expensive: it
    is the generic paired-end fallback, its five edges are the only thing standing between a
    single-cell library and a bulk gene-count matrix at exit 0, and each was derived under the OLD
    (validity) predicate. So they are re-derived here, under the new one, as an executable gate
    rather than a sentence in a pull request.

    Each edge is required to clear the resolver's tie band, not merely to reach it: the note on every
    one of them says the ONLIST decides (rung 3), which is a claim that at rungs 0-2 bulk is not
    merely level with the incumbent but ahead of it. The measured margins run +0.1376 (Multiome ATAC,
    the closest — two genomic mates ARE a bulk cDNA pair to this fallback) to +0.4500 (SPLiT-seq).

    An edge that stops deriving is a discussion, not a deletion: the honest repairs are to tighten
    the fallback's gates or to write down why the pair separates now, and both are changes somebody
    has to argue for. Deleting the edge to make this green removes the only declaration that stops a
    silent bulk answer for that chemistry.
    """
    from seqforge.resolve.confuse import could_outrank_at_rungs_0_2, rung02_margin
    from seqforge.resolve.escalate import _THETA

    specs = kb.load_all_specs()
    bulk = specs["bulk-rnaseq"]
    edges = sorted(c.id for c in bulk.confusable_with)
    assert len(edges) == 5, (
        f"bulk-rnaseq declares {len(edges)} confusable edges, not the five this gate re-derives: "
        f"{edges}. A new one needs a line here; a missing one needs an argument, not a diff."
    )

    for other in edges:
        margin = rung02_margin(bulk, specs[other], kb_probes[other])
        assert margin is not None and margin > _THETA, (
            f"bulk-rnaseq -> {other!r} no longer derives: margin {margin} on {other!r}'s own "
            f"reads at rungs 0-2. The edge says the ONLIST decides this pair, which presumes the "
            f"cheap rungs do not. Do not delete the edge to make this pass."
        )
        assert could_outrank_at_rungs_0_2(bulk, specs[other], kb_probes[other])


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
            assert accepts_at_rungs_0_2(fam_spec, kb_probes[child]), (
                f"family {fam!r} must recognize its child {child!r} at rungs 0-2"
            )
        descendants = set(tree.runnable_descendants_of(fam))
        declared = {c.id for c in fam_spec.confusable_with}
        for other in tree.leaves():
            if other in descendants:
                continue
            if not accepts_at_rungs_0_2(fam_spec, kb_probes[other]):
                continue
            assert declared & {other, *tree.ancestors_of(other)}, (
                f"family {fam!r} recognizes non-descendant {other!r} at rungs 0-2 and declares "
                f"nothing about it — it would claim foreign data silently. Either tighten the "
                f"family's gates or declare a confusable edge to {other!r} (or to its family) "
                f"naming the mechanism that decides between them."
            )


# ---------------------------------------------------------------- the mechanism must be able to fire


#: KB entries whose declared onlists we do not ship, and which therefore CANNOT be resolved the way
#: their own spec says they are. An exact pin, not a filter: this is a debt, and a debt you can forget
#: is a debt you keep.
#:
#: **Empty, and it took shipping a whitelist to empty it.** `splitseq` sat here: it declared three
#: barcode whitelists and said of the one technology it is confusable with "rung 3 decides it: the
#: round1/2/3 whitelists hit", while the three we shipped were all 10x's. The three weight-3.0 onlist
#: tests abstained and the mechanism the spec called decisive could never fire. That failure was safe
#: — it over-asks, it does not answer wrongly — which is exactly why it survived unnoticed: nothing
#: was red, and every test that appeared to prove SPLiT-seq works built a synthetic registry from the
#: spec's own aliases, proving the spec agrees with itself.
#:
#: The barcodes now ship, derived from the paper's own Supplementary Table S12 rather than guessed;
#: `test_the_splitseq_rounds_are_one_barcode_set` pins what that derivation found.
#:
#: Adding an entry here is legitimate; leaving one unrecorded is not. Do NOT close a future entry by
#: guessing barcodes — a wrong whitelist does not fail loudly. STARsolo exits 0 and emits a matrix
#: that merely looks like a thin dataset, and a plausible matrix is unrecoverable in a way a refusal
#: never is.
UNSHIPPED_ONLIST_DEBT: dict[str, list[str]] = {}


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
    *declare* a mechanism that does not exist, and that fails SILENTLY: the tests abstain, resolve
    over-asks, and nothing is red. This is the check that makes the declaration cost something.
    """
    from seqforge.io import DEFAULT_REGISTRY

    gaps: dict[str, list[str]] = {}
    for spec_id in kb.list_spec_ids():
        spec = kb.load_spec(spec_id)
        if "onlist" not in spec.decidable_by:
            continue
        missing = [n for n in _onlists_that_would_decide(spec) if not DEFAULT_REGISTRY.has(n)]
        if missing:
            gaps[spec_id] = missing

    assert gaps == UNSHIPPED_ONLIST_DEBT, (
        "the KB's rung-3 claims no longer match what ships.\n"
        f"  found:    {gaps}\n"
        f"  recorded: {UNSHIPPED_ONLIST_DEBT}\n"
        "If you shipped a whitelist, delete its entry from UNSHIPPED_ONLIST_DEBT. If you added a "
        "spec that declares onlists we do not have, either ship them or record the debt here — but "
        "do not leave it unrecorded: a spec whose decisive mechanism cannot fire looks exactly like "
        "one that works, right up until a real dataset arrives."
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
    import yaml

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
