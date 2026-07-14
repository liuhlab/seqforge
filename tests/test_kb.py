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
