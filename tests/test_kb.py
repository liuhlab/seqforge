"""Tests for the KB: schema validation, the DSL guards, and the round-trip self-test."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest
from pydantic import ValidationError

from seqforge import kb
from seqforge.kb.schema import Spec
from seqforge.models.observation import ConstantSegment
from seqforge.probe import probe_file
from seqforge.probe.signals import window_distinct_ratio


def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    with gzip.open(path, "wt") as fh:
        for i, s in enumerate(seqs):
            fh.write(f"@SIM:{i}\n{s}\n+\n{'I' * len(s)}\n")


def test_10x_spec_loads_and_validates() -> None:
    spec = kb.load_spec("10x-3p-gex-v3")
    assert spec.identity.id == "10x-3p-gex-v3"
    assert {r.id for r in spec.reads} == {"R1", "R2"}
    assert spec.backend.params["soloCBlen"] == 16
    assert spec.decidable_by  # non-empty: it has processing-divergent confusables


def test_all_shipped_specs_validate() -> None:
    specs = kb.load_all_specs()
    assert "10x-3p-gex-v3" in specs
    for spec in specs.values():
        assert spec.reads


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


def test_roundtrip_10x_geometry(tmp_path: Path) -> None:
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=2000, seed=0, pool_size=64)
    assert set(reads) == {"R1", "R2"}

    r1 = reads["R1"]
    assert all(len(s) == 28 for s in r1)  # declared 16 CB + 12 UMI

    obs_path = tmp_path / "R1.fastq.gz"
    _write_fastq_gz(obs_path, r1)
    obs = probe_file(obs_path)

    # probe recovers the declared 28 bp geometry; R1 has no internal linker (all-random)
    assert obs.read_length.mode == 28
    assert not any(isinstance(s, ConstantSegment) for s in obs.segments)

    # role-conditioned distinct-ratio recovers CB recurrence vs UMI uniqueness (the declared layout)
    cb_ratio = window_distinct_ratio(r1, 0, 16)
    umi_ratio = window_distinct_ratio(r1, 16, 28)
    assert cb_ratio is not None and cb_ratio < 0.2  # 64 barcodes over 2000 reads
    assert umi_ratio is not None and umi_ratio > 0.8

    # R2 cDNA is open-ended -> variable length
    assert len({len(s) for s in reads["R2"]}) > 1


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


# ---------- §12: the benign rule, as a computed biconditional (design §2.4) ----------
def test_section_12_biconditional_holds_over_every_loaded_spec_pair() -> None:
    """``backend_identical(A, B) <=> declared processing_equivalent`` — the rule the resolver is built on.

    Both `confuse.py`'s docstring and design §2.4 asserted CI computed this. Nothing did:
    `backend_identical` had zero callers, and the one pair it existed for (v3 <-> v3.1) named a spec
    that was never written, so the flagship example of the rule was the one pair no one could check.

    The two directions fail differently, which is why both halves matter:
    - identical but NOT declared -> we would interrogate a user about a distinction that cannot change
      a single byte of output. §12 exists to forbid exactly that.
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
            f"§12 biconditional broken for {a} vs {b}: "
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
    assert not backend_identical(specs["bulk-rnaseq-pe"], specs["splitseq"])


def test_a_declared_twin_that_diverges_would_be_caught() -> None:
    """Prove the guard fires: perturb one param and the biconditional must go red.

    A gate that has never rejected anything is a gate nobody has tested.
    """
    from seqforge.resolve.confuse import backend_identical, declared_equivalents

    specs = kb.load_all_specs()
    v3, v31 = specs["10x-3p-gex-v3"], specs["10x-3p-gex-v3.1"]
    diverged = v31.model_copy(
        update={
            "backend": v31.backend.model_copy(
                update={"params": {**v31.backend.params, "soloStrand": "Reverse"}}
            )
        }
    )
    assert not backend_identical(v3, diverged)  # no longer identical...
    assert "10x-3p-gex-v3.1" in declared_equivalents(v3)  # ...but still declared benign
    # => identical(False) != declared(True) => the biconditional above would fail. A strand
    #    inversion recorded as a benign twin is precisely the silent corpus killer.


# ---------- The parse/count line, as a property of the DSL ----------
@pytest.mark.parametrize("tech", kb.list_spec_ids())
def test_kb_specs_declare_only_parse_keys(tech: str) -> None:
    """The four-line test that would have caught the original misfiling on day one.

    soloFeatures sat in backend.params because that is where the aligner's flags live — and it cost a
    measured 40.7% of a nuclear library, because 10x 3' v3.1 chemistry is byte-identical for cells and
    nuclei. Counting was never a chemistry property.
    """
    from seqforge.compose import RECIPE_PARAM_KEYS
    from seqforge.kb.schema import KB_PARSE_KEYS

    params = kb.load_spec(tech).backend.params
    assert set(params) <= KB_PARSE_KEYS, f"{tech}: non-parse key in backend.params"
    assert not set(params) & RECIPE_PARAM_KEYS, f"{tech}: a count key is misfiled as chemistry"


def test_the_kb_cannot_even_express_a_count_key() -> None:
    """Not a convention — a validator. It fires in load_spec, kb lint, and every test that loads."""
    spec = kb.load_spec("10x-3p-gex-v3")
    payload = spec.backend.model_dump()
    payload["params"] = {**payload["params"], "soloFeatures": ["Gene"]}
    with pytest.raises(ValidationError, match="PARSE"):
        type(spec.backend).model_validate(payload)


def test_kb_parse_keys_and_recipe_param_keys_are_disjoint() -> None:
    """The proof that "a user instruction contradicts the observed bytes" is INEXPRESSIBLE.

    Not deprioritized by a runtime comparison — the user has no vocabulary in which to say it. That is
    the strongest form of that guarantee available, and it holds only while these two sets stay disjoint. If
    someone later moves soloStrand into the instructable surface, this goes red, because at that point
    the contradiction becomes sayable.
    """
    from seqforge.compose import RECIPE_PARAM_KEYS
    from seqforge.kb.schema import KB_PARSE_KEYS

    assert not (KB_PARSE_KEYS & RECIPE_PARAM_KEYS)


def test_bulk_declares_no_parse_keys_and_that_is_meaningful() -> None:
    """Empty, not degenerate: bulk PE has no barcode, no UMI, no whitelist, no offsets to declare."""
    assert kb.load_spec("bulk-rnaseq-pe").backend.params == {}


def test_backend_identical_is_order_sensitive_for_a_positional_whitelist() -> None:
    """A §12 FALSE BENIGN this repo shipped: canonical_backend used to SORT list-valued params.

    Its only justification was `soloFeatures=[Gene,GeneFull] == [GeneFull,Gene]` — and soloFeatures has
    since left backend.params. What remained under the sort was splitseq's `soloCBwhitelist`,
    which is POSITIONAL: the rounds map to CB positions in order. So a spec and the same spec with its
    rounds permuted — two chemistries that parse reads DIFFERENTLY — canonicalized byte-equal, i.e.
    processing_equivalent, i.e. §12-benign: record both, ask zero questions, emit ONE config for both.

    It never fired only by the alphabetical accident that round1 < round2 < round3. Rename the
    registry entries bc3/bc2/bc1 and it does.
    """
    from seqforge.resolve.confuse import backend_identical

    spec = kb.load_spec("splitseq")
    wl = spec.backend.params["soloCBwhitelist"]
    assert isinstance(wl, list) and len(wl) == 3
    permuted = spec.model_copy(
        update={
            "backend": spec.backend.model_copy(
                update={"params": {**spec.backend.params, "soloCBwhitelist": list(reversed(wl))}}
            )
        }
    )
    assert not backend_identical(spec, permuted), "permuted rounds are a DIFFERENT chemistry"


def test_the_only_list_valued_parse_param_left_is_positional() -> None:
    """Pins the reasoning above: if a non-positional list param ever returns, revisit _resolve_value."""
    list_params = {
        (tech, key)
        for tech in kb.list_spec_ids()
        for key, value in kb.load_spec(tech).backend.params.items()
        if isinstance(value, list)
    }
    assert list_params == {("splitseq", "soloCBwhitelist")}


# ---------- The rung-0-2 separability guard (design §2.4, fact 1) ----------
def _probes_for(spec: Spec, workdir: Path) -> list[object]:
    """Synthetic reads for one spec, probed — the input a scorer sees for a dataset of this tech."""
    from seqforge.resolve.window import WindowProbe

    reads = kb.generate_reads(spec, n=400, seed=0)
    out: list[object] = []
    for read_id, seqs in reads.items():
        path = workdir / f"{spec.identity.id.replace('/', '_')}_{read_id}.fastq.gz"
        _write_fastq_gz(path, seqs)
        out.append(WindowProbe(observation=probe_file(path), seqs=seqs[:200]))
    return out


def test_no_spec_pair_is_confusable_without_declaring_it(tmp_path: Path) -> None:
    """The under-declaration guard design §2.4 specified and nobody built.

    `decidable_by` and `confusable_with` were hand-maintained claims: nothing computed whether the
    cheap probes ACTUALLY separate two entries, so a new technology that silently collided with an
    existing one passed lint, round-trip and the whole suite. The self-test promised such a merge would be
    blocked. It would not have been.

    Computed, not asserted-to: generate each spec's own synthetic reads, then ask every OTHER spec
    whether it would claim them using rungs 0-2 alone (the onlist is withheld via an empty registry,
    so rung-3 evidence cannot rescue the answer). If A accepts B's data, A must say so.

    It found one on its first run. `bulk-rnaseq-pe` — the generic paired-end fallback — accepts
    SPLiT-seq's cdna+bc pair on geometry alone, and declared nothing. The system already knew: a test
    comment called bulk "the generic bulk fallback that merely fails to be forbidden (rung 2)". The
    KB is where that has to be written down, because the KB is what the resolver reads.
    """
    from seqforge.resolve.confuse import accepts_at_rungs_0_2

    ids = kb.list_spec_ids()
    specs = {i: kb.load_spec(i) for i in ids}
    probes = {i: _probes_for(specs[i], tmp_path) for i in ids}

    undeclared: list[str] = []
    for a in ids:
        declared = {c.id for c in specs[a].confusable_with}
        for b in ids:
            if a == b or b in declared:
                continue
            if accepts_at_rungs_0_2(specs[a], probes[b]):
                undeclared.append(
                    f"{a!r} accepts {b!r}'s reads at rungs 0-2 but does not list it in "
                    f"confusable_with — the resolver would pick one and never ask"
                )
    assert not undeclared, "under-declaration:\n" + "\n".join(undeclared)


def test_a_confusable_pair_declares_how_it_is_decided(tmp_path: Path) -> None:
    """ "Ask the human" must be a COMPUTED property, not a prompt hope (§6).

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


def test_the_separability_guard_can_actually_catch_a_collision(tmp_path: Path) -> None:
    """Prove the guard fires: a spec IS confusable with itself, by construction.

    A tautology, and that is the point — if `accepts_at_rungs_0_2` cannot recognise a spec's own
    synthetic reads, it recognises nothing and every "declared OK" above is vacuous.
    """
    from seqforge.resolve.confuse import accepts_at_rungs_0_2, rung02_separable

    spec = kb.load_spec("10x-3p-gex-v3")
    own = _probes_for(spec, tmp_path)
    assert accepts_at_rungs_0_2(spec, own)
    assert not rung02_separable(spec, own, spec, own)  # nothing is separable from itself

    # ...and it discriminates: splitseq's 94 bp barcode read is not 10x's 28 bp geometry.
    splitseq = kb.load_spec("splitseq")
    assert not accepts_at_rungs_0_2(spec, _probes_for(splitseq, tmp_path))


# ---------------------------------------------------------------- the mechanism must be able to fire


#: KB entries whose declared onlists we do not ship, and which therefore CANNOT be resolved the way
#: their own spec says they are. An exact pin, not a filter: this is a debt, and a debt you can forget
#: is a debt you keep.
#:
#: **What is actually broken.** `splitseq` declares three barcode whitelists (`splitseq-round1/2/3`)
#: and says of the one technology it is confusable with: "Rung 3 decides it: the round1/2/3
#: whitelists hit, and bulk has no whitelist to hit." We ship three whitelists and all three are
#: 10x's. So `DEFAULT_REGISTRY.has("splitseq-round1")` is False, the three weight-3.0 onlist tests
#: ABSTAIN, and the one mechanism the spec calls decisive can never fire. A real SPLiT-seq dataset
#: does not resolve — it asks a human.
#:
#: That failure is safe (it over-asks; it does not answer wrongly), which is exactly why it survived:
#: nothing was red. Every test that appears to prove SPLiT-seq works builds a synthetic registry from
#: the spec's own aliases — proving the spec agrees with itself, which was never in doubt.
#:
#: To close it: obtain the real 96 x 8 bp round1/2/3 barcodes from an authoritative source, verify
#: them against a real SPLiT-seq dataset, `seqforge io onlist pack` them, and delete the entry below.
#: Do NOT close it by guessing barcodes: a wrong whitelist does not fail loudly — STARsolo exits 0 and
#: emits a matrix that merely looks like a thin dataset.
UNSHIPPED_ONLIST_DEBT: dict[str, list[str]] = {
    "splitseq": ["splitseq-round1", "splitseq-round2", "splitseq-round3"],
}


def _onlists_that_would_decide(spec) -> list[str]:
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
    """Not a bug: nothing to decide. §12's equivalent twins are recorded together, never chosen between."""
    v2 = kb.load_spec("10x-3p-gex-v2")
    assert v2.decidable_by == []
    assert all(c.relationship == "processing_equivalent" for c in v2.confusable_with) or not (
        v2.confusable_with
    )
