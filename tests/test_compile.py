"""The compile half: manifest fill/validate/hash and compose (config + units + gates).

The params gate is the semantic check a dry run cannot make, so it gets adversarial coverage: a KB
whose declared offsets contradict the observed layout, and a config that drops or mangles a
chemistry-defining knob, must both FAIL — silently emitting them is how a corpus gets poisoned.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest
import yaml

from seqforge import __version__, kb
from seqforge.compose import ComposeError, compose, params_gate, plan
from seqforge.io import OnlistRegistry
from seqforge.manifest import (
    ExperimentInputs,
    FillError,
    ProcessingInputs,
    exit_code_for_report,
    fill_manifest,
    manifest_content_hash,
    validate_manifest,
)
from seqforge.models.manifest import Manifest, SampleGroup
from seqforge.models.resolve import ResolveResult
from seqforge.probe import probe_file
from seqforge.resolve import resolve_dataset
from seqforge.workflows import WORKFLOW_VERSION, get_module, list_modules


def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    with gzip.open(path, "wt") as fh:
        for i, s in enumerate(seqs):
            fh.write(f"@SIM:{i}\n{s}\n+\n{'I' * len(s)}\n")


def _registry_for(spec: kb.Spec) -> OnlistRegistry:
    pools = kb.build_pools(spec, seed=0)
    reg = OnlistRegistry(offline=True)
    for alias, ref in spec.onlists.items():
        if alias in pools:
            reg.register_synthetic(ref.registry, pools[alias])
    return reg


def _build(tmp_path: Path, tech: str, keys: tuple[str, str]) -> tuple[Manifest, OnlistRegistry]:
    spec = kb.load_spec(tech)
    reg = _registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    paths = []
    for k in keys:
        p = tmp_path / f"s_{k}.fastq.gz"
        _write_fastq_gz(p, reads[k])
        paths.append(p)
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    obs = [probe_file(p) for p in paths]
    manifest = fill_manifest(
        result=out.result,
        spec=spec,
        observations=obs,
        registry=reg,
        experiment=ExperimentInputs(
            organism_taxid=559292,
            accessions=["PRJNA1027859"],
            samples=[SampleGroup(sample_id="s1", file_uris=[p.name for p in paths])],
        ),
        processing=ProcessingInputs(assembly="sacCer3", annotation_name="ensembl"),
        seqforge_version=__version__,
    )
    return manifest, reg


# ---------- manifest ----------
def test_fill_records_the_equivalence_class_and_byte_derived_roles(tmp_path: Path) -> None:
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    # §12 benign twins recorded together, basis observed
    assert manifest.library.chemistry.value == ["10x-3p-gex-v3", "10x-3p-gex-v3.1"]
    assert manifest.library.chemistry.basis == "observed"
    assert manifest.library.assay.value == "EFO:0009922"
    roles = {f.basename: (f.read_id.value if f.read_id else None) for f in manifest.library.files}
    assert roles == {"s_R1.fastq.gz": "R1", "s_R2.fastq.gz": "R2"}
    # R9: the manifest carries a relative uri, never the probe's absolute local path
    assert all(not f.uri.startswith("/") for f in manifest.library.files)
    assert manifest.processing.aligner.value == "starsolo"
    assert manifest.processing.environment.value == "align-rna"


def test_fill_uses_observed_geometry_not_just_declared(tmp_path: Path) -> None:
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    reads = {r.read_id: r for r in manifest.library.read_layout.value.reads}
    assert (reads["R1"].min_len, reads["R1"].max_len) == (28, 28)  # fixed barcode read
    assert reads["R2"].min_len < reads["R2"].max_len  # open-ended cDNA is variable
    cb = next(e for e in reads["R1"].elements if e.role == "CB")
    assert (cb.start, cb.length) == (0, 16)


def test_manifest_hash_is_stable_and_matches_provenance(tmp_path: Path) -> None:
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    assert manifest_content_hash(manifest) == manifest.provenance.manifest_hash
    assert manifest.provenance.kb_version == kb.KB_VERSION
    assert manifest.provenance.workflow_version == WORKFLOW_VERSION


def test_validate_clean_manifest(tmp_path: Path) -> None:
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    report = validate_manifest(manifest)
    assert report.ok and not report.blockers
    assert exit_code_for_report(report) == 0


def test_validate_catches_referential_integrity_break(tmp_path: Path) -> None:
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    broken = manifest.model_copy(
        update={
            "experiment": manifest.experiment.model_copy(
                update={"samples": [SampleGroup(sample_id="s1", file_uris=["ghost.fastq.gz"])]}
            )
        }
    )
    report = validate_manifest(broken)
    assert not report.ok
    assert exit_code_for_report(report) == 3
    assert any("ghost.fastq.gz" in b.message for b in report.blockers)
    assert all(b.remedy for b in report.blockers)  # R4: every refusal is actionable


def test_fill_refuses_over_a_blocker(tmp_path: Path) -> None:
    spec = kb.load_spec("10x-3p-gex-v3")
    blocked = ResolveResult(
        dataset_id="x",
        kb_version=kb.KB_VERSION,
        rung_reached=2,
        candidates=[],
        conflicts=[],
        questions=[],
        blockers=[
            __import__("seqforge.models.blocker", fromlist=["Blocker"]).Blocker(
                id="b",
                code="TRUNCATED_GZIP",
                message="m",
                remedy="r",
                subject={"kind": "file", "ref": "f"},
            )
        ],
    )
    with pytest.raises(FillError):
        fill_manifest(
            result=blocked,
            spec=spec,
            observations=[],
            registry=OnlistRegistry(offline=True),
            experiment=ExperimentInputs(organism_taxid=559292),
            processing=ProcessingInputs(assembly="sacCer3", annotation_name="ensembl"),
            seqforge_version=__version__,
        )


# ---------- workflows ----------
def test_workflow_modules_are_registered_and_present_on_disk() -> None:
    assert set(list_modules()) == {"map/starsolo", "map/star"}
    for name in list_modules():
        module = get_module(name)
        assert module.snakefile.is_file(), f"{name} snakefile missing"
        assert module.version == WORKFLOW_VERSION
        assert module.env == "align-rna"


# ---------- compose ----------
def test_compose_10x_emits_kb_params_and_passes_the_params_gate(tmp_path: Path) -> None:
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    result = compose(manifest, registry=reg, workspace=tmp_path)
    assert result.modules[0].name == "map/starsolo"
    assert result.gate["params"] == "pass"
    # the infra-dependent gates must report skip, never a silent pass
    assert result.gate["wiring"] in {"pass", "skip"}
    assert result.gate["e2e"] == "skip"

    config = yaml.safe_load((tmp_path / ".seqforge" / "pipeline" / "config.yaml").read_text())
    assert config["solo"]["soloCBlen"] == "16"
    assert config["solo"]["soloUMIlen"] == "12"
    assert config["solo"]["soloStrand"] == "Forward"
    # --readFilesIn order: the cDNA read precedes the barcode read
    assert config["read_files_in"] == {"cdna": "R2", "barcode": "R1"}
    # the whitelist token resolved to a materialized file
    wl = tmp_path / ".seqforge" / "pipeline" / config["solo"]["soloCBwhitelist"]
    assert wl.is_file() and len(wl.read_text().split()) == 64

    units = (tmp_path / ".seqforge" / "pipeline" / "units.tsv").read_text().splitlines()
    assert units[0].split("\t") == ["sample_id", "read_id", "path"]
    assert len(units) == 3  # header + 2 reads


def test_compose_bulk_selects_plain_star(tmp_path: Path) -> None:
    manifest, reg = _build(tmp_path, "bulk-rnaseq-pe", ("R1", "R2"))
    result = compose(manifest, registry=reg, workspace=tmp_path)
    assert result.modules[0].name == "map/star"
    assert result.gate["params"] == "pass"
    config = yaml.safe_load((tmp_path / ".seqforge" / "pipeline" / "config.yaml").read_text())
    assert config["bulk"]["quantMode"] == "GeneCounts"
    assert config["read_files_in"] == {"mate1": "R1", "mate2": "R2"}
    assert "solo" not in config


def test_params_gate_fails_when_kb_offsets_contradict_the_observed_layout(tmp_path: Path) -> None:
    """A KB claiming a 10 bp UMI over a 12 bp UMI read must FAIL — this is the quiet corpus killer."""
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    spec = kb.load_spec("10x-3p-gex-v3")
    lying = spec.model_copy(
        update={
            "backend": spec.backend.model_copy(
                update={"params": {**spec.backend.params, "soloUMIlen": 10}}
            )
        }
    )
    p = plan(manifest, registry=reg)
    status, problems = params_gate(manifest, lying, p.config)
    assert status == "fail"
    assert any("soloUMIlen" in problem for problem in problems)


def test_params_gate_fails_when_config_drops_a_chemistry_knob(tmp_path: Path) -> None:
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    spec = kb.load_spec("10x-3p-gex-v3")
    p = plan(manifest, registry=reg)
    mangled = dict(p.config)
    mangled["solo"] = {k: v for k, v in p.config["solo"].items() if k != "soloStrand"}  # type: ignore[union-attr]
    status, problems = params_gate(manifest, spec, mangled)
    assert status == "fail"
    assert any("soloStrand" in problem for problem in problems)


def test_params_gate_fails_when_read_files_in_swaps_cdna_and_barcode(tmp_path: Path) -> None:
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    spec = kb.load_spec("10x-3p-gex-v3")
    p = plan(manifest, registry=reg)
    swapped = dict(p.config)
    swapped["read_files_in"] = {"cdna": "R1", "barcode": "R2"}  # barcode read fed as the cDNA read
    status, problems = params_gate(manifest, spec, swapped)
    assert status == "fail"
    assert any("cdna" in problem for problem in problems)


def test_compose_refuses_when_the_whitelist_cannot_be_materialized(tmp_path: Path) -> None:
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    empty = OnlistRegistry(
        offline=True
    )  # no onlist registered -> no --soloCBwhitelist is emittable
    with pytest.raises(ComposeError):
        compose(manifest, registry=empty, workspace=tmp_path)
