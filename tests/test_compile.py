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
    dataset_content_hash,
    exit_code_for_report,
    fill_manifest,
    fill_processing,
    processing_content_hash,
    run_id,
    validate_manifest,
    validate_processing,
)
from seqforge.models.dataset import DatasetManifest, SampleGroup
from seqforge.models.processing import ProcessingManifest
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


def _build(
    tmp_path: Path, tech: str, keys: tuple[str, str]
) -> tuple[DatasetManifest, OnlistRegistry]:
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
        seqforge_version=__version__,
    )
    return manifest, reg


def _processing(
    manifest: DatasetManifest,
    *,
    assembly: str = "sacCer3",
    annotation: str = "ensembl",
    processing_id: str = "default",
    pin: bool = True,
) -> ProcessingManifest:
    return fill_processing(
        spec=kb.load_spec(manifest.library.chemistry.value[0]),
        dataset=manifest,
        processing=ProcessingInputs(assembly=assembly, annotation_name=annotation),
        processing_id=processing_id,
        pin=pin,
        seqforge_version=__version__,
    )


def _pair(
    tmp_path: Path, tech: str, keys: tuple[str, str]
) -> tuple[DatasetManifest, ProcessingManifest, OnlistRegistry]:
    manifest, reg = _build(tmp_path, tech, keys)
    return manifest, _processing(manifest), reg


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


def test_the_dataset_manifest_carries_no_intent(tmp_path: Path) -> None:
    """R13: a dataset does not know how it will be processed, because it will be processed many ways."""
    assert "processing" not in DatasetManifest.model_fields
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    assert set(DatasetManifest.model_fields) == {"library", "experiment", "provenance"}
    # ...and its provenance carries no workflow_version: the assay happened before we had an opinion
    # about which rules would one day run over it.
    assert "workflow_version" not in type(manifest.provenance).model_fields


def test_processing_carries_the_derived_intent(tmp_path: Path) -> None:
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    p = _processing(manifest)
    assert p.processing.aligner.value == "starsolo"
    assert p.processing.environment.value == "align-rna"
    assert p.processing.genome.value.assembly == "sacCer3"
    # basis records WHO DECIDED; policy defaults are `inferred` + an evidence ref naming the rule,
    # which is why no `policy_default` basis is needed (design §1.0's open note).
    assert p.processing.quantification.basis == "inferred"
    assert p.processing.quantification.evidence == ["policy:default-quantification"]
    assert p.provenance.workflow_version == WORKFLOW_VERSION


def test_fill_uses_observed_geometry_not_just_declared(tmp_path: Path) -> None:
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    reads = {r.read_id: r for r in manifest.library.read_layout.value.reads}
    assert (reads["R1"].min_len, reads["R1"].max_len) == (28, 28)  # fixed barcode read
    assert reads["R2"].min_len < reads["R2"].max_len  # open-ended cDNA is variable
    cb = next(e for e in reads["R1"].elements if e.role == "CB")
    assert (cb.start, cb.length) == (0, 16)


def test_manifest_hash_is_stable_and_matches_provenance(tmp_path: Path) -> None:
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    assert dataset_content_hash(manifest) == manifest.provenance.dataset_hash
    assert manifest.provenance.kb_version == kb.KB_VERSION


# ---------- R13: the dataset is immutable; the processing manifest is plural ----------
def test_dataset_hash_is_invariant_across_a_processing_sweep(tmp_path: Path) -> None:
    """THE test for the whole split: change the intent, and what the data IS must not move.

    Aligning one dataset three ways is three processing manifests against one unchanged dataset hash,
    never three forks of the truth. If this ever goes red, the split has leaked.
    """
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    before = manifest.provenance.dataset_hash
    sweep = [
        _processing(manifest, processing_id="default"),
        _processing(manifest, assembly="ce11", annotation="WS298", processing_id="worm"),
        _processing(manifest, processing_id="template", pin=False),
    ]
    assert len({p.provenance.processing_hash for p in sweep}) == 3, "three recipes, three hashes"
    assert manifest.provenance.dataset_hash == before == dataset_content_hash(manifest)


def test_run_id_differs_per_processing_manifest(tmp_path: Path) -> None:
    """One dataset x N processing manifests = N runs.

    `provenance_id(manifest_hash, kb, workflow)` could not express this: with intent folded into the
    manifest hash, two recipes over one dataset produced an IDENTICAL id — and the composer's fixed
    output path meant the second silently overwrote the first. The collision case was exactly the use
    case the split exists for.
    """
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    a = _processing(manifest, processing_id="gene")
    b = _processing(manifest, assembly="ce11", annotation="WS298", processing_id="worm")
    ids = [
        run_id(
            dataset_hash=manifest.provenance.dataset_hash,
            processing_hash=p.provenance.processing_hash,
            kb_version=manifest.provenance.kb_version,
            workflow_version=p.provenance.workflow_version,
        )
        for p in (a, b)
    ]
    assert ids[0] != ids[1]


def test_processing_hash_matches_provenance_and_ignores_it(tmp_path: Path) -> None:
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    p = _processing(manifest)
    assert processing_content_hash(p) == p.provenance.processing_hash


def test_a_template_is_portable_but_a_bound_one_refuses_a_foreign_dataset(tmp_path: Path) -> None:
    """Both forms are legitimate and they are for different jobs.

    A template is how you drive a corpus — a mandatory pin would mean 10^4 near-identical files that
    nobody reads. A bound manifest is how you publish a run, and it must never auto-repin.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a, reg = _build(tmp_path / "a", "10x-3p-gex-v3", ("R1", "R2"))
    b, _ = _build(tmp_path / "b", "10x-3p-gex-v2", ("R1", "R2"))
    assert a.provenance.dataset_hash != b.provenance.dataset_hash

    template = _processing(a, pin=False)
    assert template.dataset is None
    plan(a, template, registry=reg)  # portable: composes against a dataset it was not built for

    bound = _processing(b)  # pinned to b...
    with pytest.raises(ComposeError, match="pinned to dataset"):
        plan(a, bound, registry=reg)  # ...so composing it against a is a refusal, not a repin

    report = validate_processing(bound, dataset=a)
    assert not report.ok
    assert [blk.code for blk in report.blockers] == ["DATASET_PIN_MISMATCH"]
    assert exit_code_for_report(report) == 3


def test_validate_processing_blocks_a_genome_organism_mismatch(tmp_path: Path) -> None:
    """A wrong-but-VALID assembly is the worst failure this system can produce.

    It is not a crash and it does not look empty: STAR aligns, exits 0, and emits a plausible matrix
    in the wrong coordinate space. Every other check catches something that would look broken; this
    one catches something that looks fine.
    """
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))  # organism = 559292 (yeast)
    p = _processing(manifest)
    assert validate_processing(p, dataset=manifest).ok

    worm = p.processing.genome.value.model_copy(update={"assembly": "ce11", "ncbi_taxid": 6239})
    lying = p.model_copy(
        update={
            "processing": p.processing.model_copy(
                update={"genome": p.processing.genome.model_copy(update={"value": worm})}
            )
        }
    )
    report = validate_processing(lying, dataset=manifest)
    assert not report.ok
    assert [blk.code for blk in report.blockers] == ["GENOME_ORGANISM_MISMATCH"]
    assert all(blk.remedy for blk in report.blockers)  # R4: every refusal is actionable


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
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    assert result.modules[0].name == "map/starsolo"
    assert result.gate["params"] == "pass"
    # the infra-dependent gates must report skip, never a silent pass
    assert result.gate["wiring"] in {"pass", "skip"}
    assert result.gate["e2e"] == "skip"

    # read the path compose REPORTS, not one reconstructed here: the layout is keyed by run_id and a
    # test that hardcodes it is testing its own arithmetic
    pipeline_dir = (tmp_path / result.config_path).parent
    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    assert config["solo"]["soloCBlen"] == "16"
    assert config["solo"]["soloUMIlen"] == "12"
    assert config["solo"]["soloStrand"] == "Forward"
    # --readFilesIn order: the cDNA read precedes the barcode read
    assert config["read_files_in"] == {"cdna": "R2", "barcode": "R1"}
    # the whitelist token resolved to a materialized file
    wl = pipeline_dir / config["solo"]["soloCBwhitelist"]
    assert wl.is_file() and len(wl.read_text().split()) == 64

    units = (tmp_path / result.units_path).read_text().splitlines()
    assert units[0].split("\t") == ["sample_id", "read_id", "path"]
    assert len(units) == 3  # header + 2 reads


def test_compose_bulk_selects_plain_star(tmp_path: Path) -> None:
    manifest, reg = _build(tmp_path, "bulk-rnaseq-pe", ("R1", "R2"))
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    assert result.modules[0].name == "map/star"
    assert result.gate["params"] == "pass"
    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    assert config["bulk"]["quantMode"] == "GeneCounts"
    assert config["read_files_in"] == {"mate1": "R1", "mate2": "R2"}
    assert "solo" not in config


def test_two_processing_manifests_do_not_overwrite_each_other(tmp_path: Path) -> None:
    """The headline use case, and the R7 bug that would have broken it.

    compose wrote to a FIXED `.seqforge/pipeline/config.yaml`, so composing one dataset two ways left
    one config on disk — the second silently clobbering the first, along with its units.tsv and its
    materialized onlists. Keying by run_id is what makes "the same dataset paired with multiple
    processing manifests" mean anything.
    """
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    a = compose(
        manifest, _processing(manifest, processing_id="yeast"), registry=reg, workspace=tmp_path
    )
    b = compose(
        manifest,
        _processing(manifest, assembly="ce11", annotation="WS298", processing_id="worm"),
        registry=reg,
        workspace=tmp_path,
    )
    assert a.config_path != b.config_path, "two runs must not share one config path"
    assert (tmp_path / a.config_path).is_file() and (tmp_path / b.config_path).is_file()
    assert yaml.safe_load((tmp_path / a.config_path).read_text())["genome"]["assembly"] == "sacCer3"
    assert yaml.safe_load((tmp_path / b.config_path).read_text())["genome"]["assembly"] == "ce11"


def test_compose_writes_the_bound_processing_lock(tmp_path: Path) -> None:
    """R7 says disk is STATE, not that disk is INPUT.

    compose takes no --processing on the default path — 10^4 boilerplate files nobody reads is not a
    design — but whatever decided the run must still be recoverable from disk afterwards. So the
    fully-resolved, dataset-BOUND manifest lands beside the config it produced, even when the input
    was a template with no pin.
    """
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    template = _processing(manifest, pin=False)
    assert template.dataset is None
    result = compose(manifest, template, registry=reg, workspace=tmp_path)

    lock = (tmp_path / result.config_path).parent / "processing.lock.yaml"
    assert lock.is_file()
    written = ProcessingManifest.model_validate(yaml.safe_load(lock.read_text()))
    assert written.dataset is not None, "the lock must be BOUND even when the input was not"
    assert written.dataset.dataset_hash == manifest.provenance.dataset_hash


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
    p = plan(manifest, _processing(manifest), registry=reg)
    status, problems = params_gate(manifest, lying, p.config)
    assert status == "fail"
    assert any("soloUMIlen" in problem for problem in problems)


def test_params_gate_fails_when_config_drops_a_chemistry_knob(tmp_path: Path) -> None:
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    spec = kb.load_spec("10x-3p-gex-v3")
    p = plan(manifest, _processing(manifest), registry=reg)
    mangled = dict(p.config)
    mangled["solo"] = {k: v for k, v in p.config["solo"].items() if k != "soloStrand"}  # type: ignore[union-attr]
    status, problems = params_gate(manifest, spec, mangled)
    assert status == "fail"
    assert any("soloStrand" in problem for problem in problems)


def test_params_gate_fails_when_read_files_in_swaps_cdna_and_barcode(tmp_path: Path) -> None:
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    spec = kb.load_spec("10x-3p-gex-v3")
    p = plan(manifest, _processing(manifest), registry=reg)
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
        compose(manifest, _processing(manifest), registry=empty, workspace=tmp_path)


def test_every_module_required_config_key_is_actually_emitted(tmp_path: Path) -> None:
    """``WorkflowModule.required_config`` says "checked in CI". Until now, nothing checked it.

    The field's own comment reads "the composer must emit every one (checked in CI)" — a contract
    with no enforcement. It matters most exactly when a key MOVES between owners: whichever side
    forgets it, the module still declares it, snakemake resolves `config[...]` at rule-expansion time,
    and the failure surfaces as a KeyError on a compute node long after compose exited 0.
    """
    cases = {"map/starsolo": "10x-3p-gex-v3", "map/star": "bulk-rnaseq-pe"}
    assert set(cases) == set(list_modules()), "a module was added without a required_config case"

    for module_name, tech in cases.items():
        work = tmp_path / module_name.replace("/", "_")
        work.mkdir(parents=True)
        manifest, reg = _build(work, tech, ("R1", "R2"))
        config = plan(manifest, _processing(manifest), registry=reg).config
        for dotted in get_module(module_name).required_config:
            node: object = config
            for part in dotted.split("."):
                assert isinstance(node, dict) and part in node, (
                    f"{module_name}: config is missing required key {dotted!r} "
                    f"(stopped at {part!r})"
                )
                node = node[part]


def test_the_required_config_check_can_catch_a_missing_key(tmp_path: Path) -> None:
    """Prove the guard fires — a contract test that has never failed proves nothing."""
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    config = plan(manifest, _processing(manifest), registry=reg).config
    assert "soloFeatures" in config["solo"]  # type: ignore[operator,index]
    del config["solo"]["soloFeatures"]  # type: ignore[index]
    missing = [d for d in get_module("map/starsolo").required_config if not _has_dotted(config, d)]
    assert missing == ["solo.soloFeatures"]


def _has_dotted(config: object, dotted: str) -> bool:
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def test_params_gate_names_the_right_block_for_a_bulk_manifest(tmp_path: Path) -> None:
    """A stray ``solo`` block on a bulk config must not be misdiagnosed as "KB param dropped".

    The gate used to take "whichever of solo/bulk is a dict", so this config reported
    ``config drops KB param 'quantMode'`` — a true failure pinned on the wrong cause, which sends the
    reader to the KB when the bug is in the composer. The block is a function of the module; it is
    now read from the one definition the composer also writes through.
    """
    manifest, reg = _build(tmp_path, "bulk-rnaseq-pe", ("R1", "R2"))
    spec = kb.load_spec("bulk-rnaseq-pe")
    p = plan(manifest, _processing(manifest), registry=reg)
    assert params_gate(manifest, spec, p.config) == ("pass", [])

    corrupted = {**p.config, "solo": {"soloType": "CB_UMI_Simple"}}
    del corrupted["bulk"]
    status, problems = params_gate(manifest, spec, corrupted)
    assert status == "fail"
    assert any("no 'bulk' param block" in p for p in problems), problems
    assert not any("quantMode" in p and "drops" in p for p in problems), problems
