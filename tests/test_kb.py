"""Tests for the KB: schema validation, the DSL guards, and the R10 round-trip self-test."""

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


def test_kb_roundtrip_self_test_passes() -> None:
    result = kb.run_roundtrip("10x-3p-gex-v3", seed=0)
    assert result["passed"] is True
    assert result["checks"]  # length + barcode-recurrence + umi-uniqueness checks all ran


@pytest.mark.parametrize("tech", ["10x-3p-gex-v2", "bulk-rnaseq-pe", "splitseq"])
def test_all_pilot_techs_roundtrip(tech: str) -> None:
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
