"""The compile half: manifest fill/validate/hash and compose (config + units + gates).

The params gate is the semantic check a dry run cannot make, so it gets adversarial coverage: a KB
whose declared offsets contradict the observed layout, and a config that drops or mangles a
chemistry-defining knob, must both FAIL — silently emitting them is how a corpus gets poisoned.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path

import pytest
import yaml

from seqforge import __version__, kb
from seqforge.compose import ComposeError, compose, gates, params_gate, plan
from seqforge.compose.params import param_block_key, param_owners
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
from seqforge.resolve.confuse import canonical_backend
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
    tmp_path: Path, tech: str, keys: tuple[str, ...] | None = None
) -> tuple[DatasetManifest, OnlistRegistry]:
    """Build a manifest from synthetic reads. ``keys`` defaults to the spec's own read ids.

    Callers used to pass ``("R1", "R2")`` unconditionally, which silently pinned this helper to 10x
    and bulk naming and made splitseq (whose reads are ``cdna``/``bc``) raise ``KeyError: 'R1'``
    rather than compose. Deriving the default from the spec is what lets a test iterate the KB.
    """
    spec = kb.load_spec(tech)
    reg = _registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    paths = []
    for k in keys or tuple(r.id for r in spec.reads):
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
    p, _ = fill_processing(
        spec=kb.load_spec(manifest.library.chemistry.value[0]),
        dataset=manifest,
        processing=ProcessingInputs(assembly=assembly, annotation_name=annotation),
        processing_id=processing_id,
        pin=pin,
        seqforge_version=__version__,
    )
    return p


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
    assert p.processing.quantification.evidence == ["policy:default-solo-features"]
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
    status, problems = params_gate(manifest, _processing(manifest), lying, p.config)
    assert status == "fail"
    assert any("soloUMIlen" in problem for problem in problems)


def test_params_gate_fails_when_config_drops_a_chemistry_knob(tmp_path: Path) -> None:
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    spec = kb.load_spec("10x-3p-gex-v3")
    p = plan(manifest, _processing(manifest), registry=reg)
    mangled = dict(p.config)
    mangled["solo"] = {k: v for k, v in p.config["solo"].items() if k != "soloStrand"}  # type: ignore[union-attr]
    status, problems = params_gate(manifest, _processing(manifest), spec, mangled)
    assert status == "fail"
    assert any("soloStrand" in problem for problem in problems)


def test_params_gate_fails_when_read_files_in_swaps_cdna_and_barcode(tmp_path: Path) -> None:
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    spec = kb.load_spec("10x-3p-gex-v3")
    p = plan(manifest, _processing(manifest), registry=reg)
    swapped = dict(p.config)
    swapped["read_files_in"] = {"cdna": "R1", "barcode": "R2"}  # barcode read fed as the cDNA read
    status, problems = params_gate(manifest, _processing(manifest), spec, swapped)
    assert status == "fail"
    assert any("cdna" in problem for problem in problems)


def test_compose_refuses_when_the_whitelist_cannot_be_materialized(tmp_path: Path) -> None:
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    empty = OnlistRegistry(
        offline=True
    )  # no onlist registered -> no --soloCBwhitelist is emittable
    with pytest.raises(ComposeError):
        compose(manifest, _processing(manifest), registry=empty, workspace=tmp_path)


def _one_spec_per_distinct_backend() -> list[str]:
    """One representative per processing-equivalence class — the §12 biconditional, used as leverage.

    Composing "10x-3p-gex-v3.1 specifically" is not a thing the system can do: it is byte-identical
    to v3, so the resolver picks v3 and `fill` refuses the mismatch. That is §12 working, not a bug.
    And it costs no coverage: backend-identical specs render an identical config **by definition**,
    which is exactly what `processing_equivalent` asserts. So collapse the class and test one.

    Derived from `canonical_backend`, never hardcoded — if a new spec is genuinely divergent it gets
    its own case automatically, which is the property the old `{module: tech}` dict destroyed.
    """
    seen: dict[str, str] = {}
    for tech in kb.list_spec_ids():
        seen.setdefault(canonical_backend(kb.load_spec(tech)), tech)
    return sorted(seen.values())


@pytest.mark.parametrize("tech", _one_spec_per_distinct_backend())
def test_every_module_required_config_key_is_actually_emitted(tech: str, tmp_path: Path) -> None:
    """``WorkflowModule.required_config`` says "checked in CI". Until now, nothing checked it.

    The field's own comment reads "the composer must emit every one (checked in CI)" — a contract
    with no enforcement. It matters most exactly when a key MOVES between owners: whichever side
    forgets it, the module still declares it, snakemake resolves `config[...]` at rule-expansion time,
    and the failure surfaces as a KeyError on a compute node long after compose exited 0.

    That prediction came true, and this test was blind to it twice over. It checked ONE hardcoded
    chemistry per module — `{"map/starsolo": "10x-3p-gex-v3"}` — which made a second starsolo
    chemistry structurally unrepresentable, so SPLiT-seq was never composed here at all. And it
    validated against a `required_config` that was itself missing the four keys `starsolo.smk`
    dereferences. Both halves of the guard were wrong in the same direction.

    Now every KB spec is composed against whichever module its backend selects, so a chemistry
    cannot hide behind a dict key.
    """
    work = tmp_path / tech.replace(".", "_")
    work.mkdir(parents=True)
    manifest, reg = _build(work, tech)  # read ids come from the spec, not from 10x's naming
    spec = kb.load_spec(tech)
    module_name = spec.backend.module
    processing = _processing(manifest)
    config = plan(manifest, processing, registry=reg).config
    block = param_block_key(spec)

    # A param key applies to THIS chemistry only if some owner declares it (KB, derived, or the
    # processing manifest). STARsolo's CB geometry is spelled start/len for a simple chemistry and
    # as a quadruple for a combinatorial one, so `required_config` is the union of what the module
    # may read and this is where the branch is resolved. Non-param keys are unconditional.
    # `params_gate` separately proves the block is EXACTLY its owners' union, so nothing gets to be
    # quietly absent by being quietly unowned.
    owned = set(param_owners(spec, processing))
    for dotted in get_module(module_name).required_config:
        if dotted.startswith(f"{block}.") and dotted.split(".", 1)[1] not in owned:
            continue
        assert _has_dotted(config, dotted), (
            f"{tech} -> {module_name}: config is missing required key {dotted!r}. "
            f"The module dereferences it; compose did not emit it. This is a KeyError on a "
            f"compute node, long after compose exited 0."
        )


@pytest.mark.parametrize("tech", _one_spec_per_distinct_backend())
def test_the_params_gate_passes_for_every_chemistry(tech: str, tmp_path: Path) -> None:
    """The gate ran on 10x and bulk and had never once seen a combinatorial chemistry.

    `splitseq` appeared in no compose test at all, so the three-owner coverage check — every emitted
    param attributable to exactly one of KB / derived / processing — was only ever exercised against
    two owners. This is where the third one earns its keep.
    """
    manifest, reg = _build(tmp_path, tech)
    processing = _processing(manifest)
    config = plan(manifest, processing, registry=reg).config
    status, problems = params_gate(manifest, processing, kb.load_spec(tech), config)
    assert status == "pass", problems


def test_a_complex_chemistry_locates_its_barcodes_by_quadruple(tmp_path: Path) -> None:
    """SPLiT-seq's barcodes are derived from the element model, in whitelist order.

    `starsolo.smk` dereferenced `--soloCBstart` unconditionally, which `CB_UMI_Complex` does not
    have and cannot supply: a KeyError on a compute node, long after compose exited 0. Nothing
    caught it because no test composed splitseq.

    The quadruples are COMPUTED from the element coordinates, never transcribed: a published
    SPLiT-seq quadruple is chemistry-specific (v1 puts Round1 at 86-93, Parse/v2 at 78-85), so a
    remembered one is a coin flip between two real chemistries. Order is load-bearing — STARsolo
    pairs the Nth whitelist with the Nth position.
    """
    manifest, reg = _build(tmp_path, "splitseq")
    solo = plan(manifest, _processing(manifest), registry=reg).config["solo"]
    assert isinstance(solo, dict)

    assert solo["soloType"] == "CB_UMI_Complex"
    # round1, round2, round3 -> bc1 @ [86,94), bc2 @ [48,56), bc3 @ [10,18); ends are inclusive
    assert solo["soloCBposition"] == "0_86_0_93 0_48_0_55 0_10_0_17"
    assert solo["soloUMIposition"] == "0_0_0_9"
    assert "soloCBstart" not in solo  # the simple-chemistry spelling must not appear
    assert len(str(solo["soloCBwhitelist"]).split()) == 3  # one whitelist per split-pool round


# ---------- R12: consumer, not parallel universe ----------
#: Names owned upstream by `liulab-genome`. seqforge may CALL them; defining one here means we have
#: started reimplementing the package whose whole job this is.
_UPSTREAM_GENOME_NAMES = frozenset({"Genome", "build_star_index", "register_gtf"})

#: Filenames that would mean seqforge had begun defining aligner environments — `liulab-runtime`'s
#: job. seqforge names an env (`align-rna`); it never says what is inside one.
_ENV_DEFINITION_FILES = ("environment.yml", "environment.yaml", "conda.yml", "Dockerfile")


def _src_root() -> Path:
    import seqforge

    return Path(seqforge.__file__).parent


def test_seqforge_defines_no_genome_machinery(tmp_path: Path) -> None:
    """R12, as an AST check rather than a code-review habit.

    `Genome(assembly).build_star_index(gtf=name)` is a *consumer call* and is exactly right. A
    `def build_star_index` or `class Genome` in this tree is the opposite: it means the resolution
    of assemblies, annotations and indexes — liulab-genome's entire remit — is being duplicated
    here, where it will drift and where R9's "no absolute path in a manifest" stops being anybody's
    invariant.
    """
    import ast

    offenders: list[str] = []
    for py in sorted(_src_root().rglob("*.py")):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name in _UPSTREAM_GENOME_NAMES
            ):
                offenders.append(f"{py.name}:{node.lineno} defines {node.name!r}")
    assert not offenders, "R12: seqforge is redefining liulab-genome's job:\n" + "\n".join(
        offenders
    )


def test_the_genome_machinery_check_can_actually_catch_a_reimplementation(tmp_path: Path) -> None:
    """Prove the guard fires — and that it tolerates the consumer call it must allow."""
    import ast

    def defines_upstream(source: str) -> bool:
        return any(
            isinstance(n, ast.ClassDef | ast.FunctionDef) and n.name in _UPSTREAM_GENOME_NAMES
            for n in ast.walk(ast.parse(source))
        )

    assert defines_upstream("class Genome:\n    pass\n")
    assert defines_upstream("def build_star_index(gtf):\n    return 1\n")
    # the real, correct usage must NOT trip it
    assert not defines_upstream(
        "from genome import Genome\nindex = Genome(assembly).build_star_index(gtf=annotation)\n"
    )


def test_seqforge_defines_no_aligner_environments() -> None:
    """R12's other half: an env is NAMED here and DEFINED in liulab-runtime.

    `RuntimeEnv` is a closed literal of liulab-runtime env names — there is deliberately no profile
    indirection, the name *is* the identifier. A conda YAML or Dockerfile appearing in this tree
    would mean we had started duplicating liulab-runtime, scattering env definitions across two
    repos that then disagree about which STAR ran.
    """
    from typing import get_args

    from seqforge.models.processing import RuntimeEnv

    assert set(get_args(RuntimeEnv)) == {"align-rna", "align-dna", "ml", "ml-gpu"}
    found = [str(p) for name in _ENV_DEFINITION_FILES for p in _src_root().rglob(name)]
    assert not found, f"R12: seqforge is defining an aligner environment: {found}"


# ---------- R1: emit data, never code ----------
#: A Snakemake rule definition. `rule x:` / `checkpoint x:` are the only ways to introduce rule
#: source, so this is the whole vocabulary R1 forbids the composer from emitting.
_RULE_DEF = re.compile(r"^\s*(rule|checkpoint)\s+\w+\s*:", re.M)


def test_the_generated_wrapper_contains_no_rule_source() -> None:
    """R1, at the ONE place seqforge writes Snakemake syntax at all.

    `gates.py` generates a Snakefile so the dry run has an entry point, and its own first line says
    "rule source is never generated" — a claim defended by a comment. Everything the pipeline
    actually executes must come from the hand-written `.smk` modules via `include:`; the moment the
    composer emits a `rule`, R1 is gone and nobody finds out from a comment.

    Asserted against the template rather than a rendered instance because the template is the thing
    a future edit would change.
    """
    wrapper = gates._WRAPPER
    assert not _RULE_DEF.search(wrapper), f"the composer emits rule source (R1):\n{wrapper}"
    assert "include:" in wrapper  # it composes by inclusion...
    assert "configfile:" in wrapper  # ...and parameterises by data


def test_the_rule_source_check_can_actually_catch_generated_rules() -> None:
    """Prove the guard fires, and that it does not cry wolf on the words it must tolerate."""
    assert _RULE_DEF.search("rule starsolo_count:\n    shell: 'STAR'\n")
    assert _RULE_DEF.search("checkpoint split:\n")
    assert _RULE_DEF.search('include: "x.smk"\nrule all:\n    input: []\n')
    # ...but the prose and directives a legitimate wrapper contains are not rule source
    assert not _RULE_DEF.search("# includes the module whose rules we never generate\n")
    assert not _RULE_DEF.search('include: "map/starsolo.smk"\nconfigfile: "config.yaml"\n')
    assert not _RULE_DEF.search('config["ruleset"] = "x"\n')


@pytest.mark.parametrize("module_name", list_modules())
def test_shipped_modules_are_hand_written_not_generated(module_name: str) -> None:
    """The other half of R1: the rules that DO exist are checked-in source, not build artifacts.

    A module whose rules were generated would defeat the wrapper check by moving the generation one
    step earlier, so the modules must be real files under version control, carrying the header that
    says what they are.
    """
    snakefile = get_module(module_name).snakefile
    assert snakefile.is_file(), f"{module_name}: {snakefile} is not on disk"
    text = snakefile.read_text()
    assert _RULE_DEF.search(text), f"{module_name} defines no rules — is it really a module?"
    assert "HAND-WRITTEN" in text and "NEVER machine-generated" in text


#: ``units_tsv`` is injected by the generated run wrapper (``compose/gates.py``), never by the
#: composer's config. Named here so that a module reading some *other* undeclared key stays a
#: failure rather than being waved through by a broad exception.
_WRAPPER_SUPPLIED_KEYS = frozenset({"units_tsv"})


def _keys_read_by(snakefile: Path) -> set[str]:
    """Derive the dotted config keys a module actually reads, from its source.

    Two forms, because the module uses both: `config["a"]["b"]` directly, and the indirection
    `params: solo=config["solo"]` followed by `{params.solo[soloCBlen]}` in the shell block.

    Comments are stripped first, and that is not fussiness — starsolo.smk's own header prose says
    "every chemistry-defining knob arrives via `config["solo"]`", which a naive scan reads as a bare
    read of the whole block and reports as undeclared. Same lesson as `test_skills.py`'s
    `_code_spans`: narrow the haystack, because a check that cries wolf gets deleted.
    """
    code = "\n".join(line.split("#")[0] for line in snakefile.read_text().splitlines())
    keys: set[str] = set()

    # A bare `<name> = config["<section>"]` binds the whole block to a name. Track those, including
    # one rebinding hop (`SOLO = config["solo"]` at module level, then `solo=SOLO` in a params
    # block), because that chain is exactly how the shell reaches `{params.solo[soloType]}`.
    # The lookahead matters: `ASSEMBLY = config["genome"]["assembly"]` is a nested read, not a
    # binding, and must fall through to the direct scan below.
    bound = dict(re.findall(r'(\w+)\s*=\s*config\["(\w+)"\](?!\[)', code))
    for name, src in re.findall(r"^\s*(\w+)\s*=\s*(\w+)\s*,?\s*$", code, re.M):
        if src in bound:
            bound.setdefault(name, bound[src])

    for name, section in bound.items():
        # `{params.<name>[<key>]}` in a shell block, or `<NAME>["<key>"]` in Python.
        subscripts = set(re.findall(rf"\{{params\.{name}\[(\w+)\]\}}", code)) | set(
            re.findall(rf"""\b{name}\[["'](\w+)["']\]""", code)
        )
        # Subscripted -> it is a block alias and each subscript is the real read. Never subscripted
        # -> it was a scalar read all along (`OUTDIR = config["outdir"]`), so the section IS the key.
        keys |= {f"{section}.{k}" for k in subscripts} or {section}

    # Direct reads: config["a"]["b"] -> a.b | config["a"] -> a. Binding sites are already accounted
    # for above, so drop them here rather than double-count the block as a bare key.
    direct = re.sub(r'\w+\s*=\s*config\["\w+"\](?!\[)', "", code)
    for section, sub in re.findall(r'config\["(\w+)"\](?:\["(\w+)"\])?', direct):
        keys.add(f"{section}.{sub}" if sub else section)

    return keys - _WRAPPER_SUPPLIED_KEYS


@pytest.mark.parametrize("module_name", list_modules())
def test_required_config_covers_every_key_the_module_reads(module_name: str) -> None:
    """The contract is DERIVED from the module source, not hand-maintained beside it.

    `starsolo.smk` has always dereferenced `{params.solo[soloCBstart]}`, `[soloCBlen]`,
    `[soloUMIstart]` and `[soloUMIlen]`; `required_config` declared none of the four. Nothing
    noticed, because the only thing checking the contract compared the config against that same
    wrong list. A hand-maintained list of what the code does is a comment with a tuple's syntax.

    Under-declaration is the dangerous direction and the only one asserted here: the module reads a
    key the composer was never told to emit. Over-declaration (a key declared but unread) is merely
    untidy — compose emits it and nothing breaks.
    """
    module = get_module(module_name)
    undeclared = _keys_read_by(module.snakefile) - set(module.required_config)
    assert not undeclared, (
        f"{module_name}: reads {sorted(undeclared)} but does not declare them in required_config. "
        f"Nothing will emit them, and STAR dies at rule-expansion time on a compute node."
    )


def test_the_required_config_scanner_can_actually_catch_an_undeclared_key(tmp_path: Path) -> None:
    """Prove the scanner fires — a derived check that has never failed proves nothing.

    Both forms must be caught: the direct `config[...]` read and the `params` alias indirection
    that hid the real bug for as long as it existed. And prose in a comment must NOT be caught —
    the first draft of this scanner reported starsolo's own header as two undeclared keys.
    """
    smk = tmp_path / "fake.smk"
    smk.write_text(
        '# knobs arrive via `config["solo"]` and `config["read_files_in"]`  <- prose, not a read\n'
        'UNITS = _load_units(config["units_tsv"])\n'
        'OUT = config["outdir"]\n'
        'ASSEMBLY = config["genome"]["assembly"]\n'
        "rule r:\n"
        '    params:\n        solo=config["solo"],\n'
        '    shell:\n        r"STAR --soloCBlen {params.solo[soloCBlen]} --x {params.solo[oops]}"\n'
    )
    found = _keys_read_by(smk)
    assert "solo.soloCBlen" in found  # the alias indirection resolves
    assert "solo.oops" in found  # ... and an undeclared one is visible
    assert "outdir" in found  # the direct form resolves
    assert "genome.assembly" in found  # ... including the nested form
    assert "units_tsv" not in found  # the wrapper supplies it; not the composer's job
    assert "solo" not in found  # the alias BINDING is not a read of the whole block
    assert "read_files_in" not in found  # and neither is a mention in a comment


def test_the_required_config_check_can_catch_a_missing_key(tmp_path: Path) -> None:
    """Prove the guard fires — a contract test that has never failed proves nothing."""
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    config = plan(manifest, _processing(manifest), registry=reg).config
    assert "soloFeatures" in config["solo"]  # type: ignore[operator,index]
    del config["solo"]["soloFeatures"]  # type: ignore[index]
    missing = [d for d in get_module("map/starsolo").required_config if not _has_dotted(config, d)]
    assert "solo.soloFeatures" in missing
    # The position quadruples are also absent, and legitimately so: this is a CB_UMI_Simple
    # chemistry, which locates its barcode by start/length and has no quadruple to give. That is
    # why the real check intersects with `param_owners` rather than demanding the whole union.
    assert set(missing) == {"solo.soloFeatures", "solo.soloCBposition", "solo.soloUMIposition"}


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
    assert params_gate(manifest, _processing(manifest), spec, p.config) == ("pass", [])

    corrupted = {**p.config, "solo": {"soloType": "CB_UMI_Simple"}}
    del corrupted["bulk"]
    status, problems = params_gate(manifest, _processing(manifest), spec, corrupted)
    assert status == "fail"
    assert any("no 'bulk' param block" in p for p in problems), problems
    assert not any("quantMode" in p and "drops" in p for p in problems), problems


# ---------- R14: the gate is where the parse/count line stops being a convention ----------
def test_param_owners_computes_the_line(tmp_path: Path) -> None:
    """The parse/count line as a COMPUTED FACT, directly testable, not a comment nobody re-reads."""
    from seqforge.compose import param_owners

    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    owners = param_owners(kb.load_spec("10x-3p-gex-v3"), _processing(manifest))
    assert owners["soloType"] == "kb"
    assert owners["soloCBwhitelist"] == "kb"
    assert owners["soloStrand"] == "kb"
    assert owners["soloFeatures"] == "processing"  # the whole point of the move


def test_quantification_is_no_longer_decorative(tmp_path: Path) -> None:
    """It used to be written to the manifest and then IGNORED by compose, which read the KB instead.

    Two sources of truth for one decision, unable to disagree only because one was never consulted.
    Change the manifest's intent, and the emitted config must follow it.
    """
    from seqforge.models.processing import SoloQuant

    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    p = _processing(manifest)
    default = plan(manifest, p, registry=reg).config
    assert (
        default["solo"]["soloFeatures"]
        == "Gene GeneFull GeneFull_ExonOverIntron GeneFull_Ex50pAS Velocyto"
    )
    assert default["primary_feature"] == "Gene"

    q = p.processing.quantification
    genefull = p.model_copy(
        update={
            "processing": p.processing.model_copy(
                update={
                    "quantification": q.model_copy(
                        update={"value": SoloQuant(features=["GeneFull", "Gene"])}
                    )
                }
            )
        }
    )
    config = plan(manifest, genefull, registry=reg).config
    assert config["solo"]["soloFeatures"] == "GeneFull Gene"
    # ...and "which matrix is THE matrix" is emitted as a VALUE, not left as a positional convention:
    # STARsolo does not care about order, so the list order has no aligner-side referent.
    assert config["primary_feature"] == "GeneFull"


def test_params_gate_fails_when_the_config_disagrees_with_the_manifest(tmp_path: Path) -> None:
    """The check that makes `quantification` load-bearing: a decorative field cannot be caught."""
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    p = _processing(manifest)
    config = plan(manifest, p, registry=reg).config
    corrupted = {**config, "solo": {**config["solo"], "soloFeatures": "GeneFull"}}  # type: ignore[dict-item]
    status, problems = params_gate(manifest, p, kb.load_spec("10x-3p-gex-v3"), corrupted)
    assert status == "fail"
    assert any("does not match the processing manifest" in problem for problem in problems)


def test_params_gate_fails_when_the_kb_declares_a_count_key(tmp_path: Path) -> None:
    """Belt to the schema validator's braces — it catches the model_copy'd specs tests build."""
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    p = _processing(manifest)
    spec = kb.load_spec("10x-3p-gex-v3")
    misowned = spec.model_copy(
        update={
            "backend": spec.backend.model_copy(
                update={"params": {**spec.backend.params, "soloFeatures": ["Gene"]}}
            )
        }
    )
    status, problems = params_gate(manifest, p, misowned, plan(manifest, p, registry=reg).config)
    assert status == "fail"
    assert any("count key" in problem and "R14" in problem for problem in problems)


def test_params_gate_fails_on_an_emitted_key_with_no_owner(tmp_path: Path) -> None:
    """Coverage: the emitted key set must be EXACTLY the union of the two owners.

    Disjointness alone is the decorative bug in reverse — it proves the two sources cannot disagree,
    not that either key arrives. Before this, the gate iterated the KB alone, so a key moved out of
    the KB silently stopped being gated at all, and an orphan was invisible.
    """
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    p = _processing(manifest)
    config = plan(manifest, p, registry=reg).config
    orphaned = {**config, "solo": {**config["solo"], "outFilterMismatchNmax": "10"}}  # type: ignore[dict-item]
    status, problems = params_gate(manifest, p, kb.load_spec("10x-3p-gex-v3"), orphaned)
    assert status == "fail"
    assert any("no owner declares" in problem for problem in problems)


# ---------- R15: produce every answer rather than ask ----------
def test_the_default_is_screcounters_five_in_screcounters_order() -> None:
    """Exactly scRecounter's five, that order, and deliberately no SJ.

    Their five is a PRECEDENT, not a derivation — adopting it wholesale without pinning it here would
    import someone else's unstated scope decision silently. (Source: ArcInstitute/scRecounter,
    workflows/star_full.nf: `--soloFeatures Gene GeneFull GeneFull_ExonOverIntron GeneFull_Ex50pAS
    Velocyto`.)
    """
    from seqforge.manifest.policy import DEFAULT_SOLO_FEATURES

    assert DEFAULT_SOLO_FEATURES == (
        "Gene",
        "GeneFull",
        "GeneFull_ExonOverIntron",
        "GeneFull_Ex50pAS",
        "Velocyto",
    )
    assert "SJ" not in DEFAULT_SOLO_FEATURES, (
        "a splice-junction matrix has a different feature axis"
    )
    # Gene first: the primary matrix matches the common whole-cell expectation, and Velocyto's
    # "requires Gene" constraint is satisfied by construction rather than by luck.
    assert DEFAULT_SOLO_FEATURES[0] == "Gene"


def test_the_default_counts_the_nuclear_features_without_being_asked(tmp_path: Path) -> None:
    """The 40.7% defect, dissolved rather than answered.

    The KB used to bake soloFeatures:[Gene] into chemistry, so a single-NUCLEUS dataset compiled to
    Gene-only and silently dropped 40.7% of its signal — STARsolo exits 0 and the matrix merely looks
    thin. No nuclei/cells fact is asserted anywhere in this test, and none is needed: GeneFull is
    computed regardless. That is the whole point — we do not ask a question whose every answer we can
    afford to emit.
    """
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    features = plan(manifest, _processing(manifest), registry=reg).config["solo"]["soloFeatures"]
    assert {"Gene", "GeneFull"} <= set(str(features).split())


def test_bulk_never_gets_solo_features(tmp_path: Path) -> None:
    """Counting is MODULE-scoped: soloFeatures is meaningless to plain STAR.

    A processing manifest that carried one shape unconditionally would be a type error the moment it
    met the other module — which is why Quantification is a discriminated union rather than a list.
    """
    manifest, reg = _build(tmp_path, "bulk-rnaseq-pe", ("R1", "R2"))
    config = plan(manifest, _processing(manifest), registry=reg).config
    assert config["bulk"] == {"quantMode": "GeneCounts"}
    assert "solo" not in config
    assert "primary_feature" not in config  # bulk has no Solo.out/<Feature>/ split


# ---------- precedence: policy -> instruction -> flag, in ONE pure function ----------
def _ins(field: str, value: str):
    from seqforge.manifest import Instruction

    return Instruction(field=field, value=value, basis="user_confirmed", evidence=["assert-x-0"])


def test_policy_default_is_inferred_and_names_its_rule() -> None:
    from seqforge.manifest import resolve_features

    features, basis, evidence, warnings = resolve_features()
    assert basis == "inferred"
    assert evidence == [
        "policy:default-solo-features"
    ]  # the rule, by name — that is why no new basis
    assert not warnings
    assert features[0] == "Gene"


def test_prose_promotes_it_never_narrows() -> None:
    """ "...should be aligned in GeneFull mode" — instead of Gene, or make sure GeneFull is computed?

    We take the second: charitable, cheap, and consistent with R15. The instructed feature is UNIONed
    with the default and promoted to primary. Nothing is dropped — which is also the safety argument
    for letting a model source this at all: a hallucinated instruction can only mislabel the primary,
    never destroy signal.
    """
    from seqforge.manifest import DEFAULT_SOLO_FEATURES, resolve_features

    features, basis, evidence, warnings = resolve_features(
        instructions=[_ins("processing.quantification", "GeneFull")]
    )
    assert features[0] == "GeneFull", "the instructed feature becomes primary"
    assert set(features) == set(DEFAULT_SOLO_FEATURES), "and NOTHING is dropped"
    assert basis == "user_confirmed"
    assert evidence == ["assert-x-0"]
    assert not warnings


def test_a_flag_replaces_exactly_and_warns_when_it_narrows() -> None:
    """The user typed the whole list; they mean it. But narrowing is the only irreversible act here."""
    from seqforge.manifest import resolve_features

    features, basis, evidence, warnings = resolve_features(override=("Gene", "GeneFull"))
    assert features == ["Gene", "GeneFull"]
    assert basis == "user_confirmed" and evidence == ["cli:--quantify"]
    assert [w.code for w in warnings] == ["FEATURES_NARROWED"]
    assert "40.7%" in warnings[0].message  # the refusal cites the number that justifies the default


def test_a_flag_beats_an_instruction_silently() -> None:
    """Precedence is not an ambiguity. A flag overriding a file is a normal, intentional act."""
    from seqforge.manifest import resolve_features

    features, basis, evidence, _ = resolve_features(
        instructions=[_ins("processing.quantification", "Velocyto")],
        override=("Gene", "GeneFull"),
    )
    assert features == ["Gene", "GeneFull"], "the flag wins outright"
    assert evidence == ["cli:--quantify"]
    assert basis == "user_confirmed"


def test_two_instructions_disagreeing_is_a_conflict() -> None:
    """Same precedence, no tiebreak: R6 applies to intent exactly as it applies to truth."""
    from seqforge.manifest import instructions_from_assertions
    from seqforge.models.assertion import Assertion, ExtractorProvenance, SourceSpan

    def _a(i: int, value: str) -> Assertion:
        return Assertion(
            id=f"assert-aa-{i}",
            field="processing.genome.assembly",
            value=value,
            span=SourceSpan(doc_sha256="d" * 64, quote=f"align to {value}"),
            span_verified=True,
            entailment_ok=True,
            llm_confidence=0.9,
            extractor=ExtractorProvenance(model_id="m", prompt_version="p"),
        )

    _, conflicts = instructions_from_assertions(
        [_a(0, "ce11"), _a(1, "hg38")], instruction_docs=frozenset({"d" * 64})
    )
    assert len(conflicts) == 1
    assert conflicts[0].field == "processing.genome.assembly"
    assert conflicts[0].kind == "asserted_vs_asserted"
    assert conflicts[0].decidable_by == ["user"]  # the first real use of that vocabulary member
    assert {p.value for p in conflicts[0].positions} == {"ce11", "hg38"}


def test_an_instruction_from_a_reference_doc_never_becomes_an_instruction() -> None:
    from seqforge.manifest import instructions_from_assertions
    from seqforge.models.assertion import Assertion, ExtractorProvenance, SourceSpan

    a = Assertion(
        id="assert-bb-0",
        field="processing.quantification",
        value="GeneFull",
        span=SourceSpan(doc_sha256="e" * 64, quote="in GeneFull mode"),
        span_verified=True,
        entailment_ok=True,
        llm_confidence=0.9,
        extractor=ExtractorProvenance(model_id="m", prompt_version="p"),
    )
    # not among the --instruction docs => dropped, not downgraded
    assert instructions_from_assertions([a], instruction_docs=frozenset()) == ([], [])
    ins, _ = instructions_from_assertions([a], instruction_docs=frozenset({"e" * 64}))
    assert [i.field for i in ins] == ["processing.quantification"]
