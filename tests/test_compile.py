"""The compile half: manifest fill/validate/hash and compose (config + units + gates).

The params gate is the semantic check a dry run cannot make, so it gets adversarial coverage: a KB
whose declared offsets contradict the observed layout, and a config that drops or mangles a
chemistry-defining knob, must both FAIL — silently emitting them is how a corpus gets poisoned.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path
from typing import get_args

import pytest
import yaml

from seqforge import __version__, kb
from seqforge.compose import ComposeError, compose, core, params_gate, plan
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
from seqforge.models.evidenced import EvidencedTaxid
from seqforge.models.processing import ProcessingManifest
from seqforge.models.resolve import ResolveResult
from seqforge.probe import probe_file
from seqforge.resolve import resolve_dataset
from seqforge.resolve.confuse import canonical_backend
from seqforge.workflows import WORKFLOW_VERSION, get_module, keys_read_by, list_modules


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
            organism=_taxid(559292),
            accessions=["PRJNA1027859"],
            samples=[SampleGroup(sample_id="s1", file_uris=[p.name for p in paths])],
        ),
        seqforge_version=__version__,
    )
    return manifest, reg


def _taxid(value: int) -> EvidencedTaxid:
    """An organism as the manifest holds it: a value that knows how we know it.

    `ExperimentInputs` takes an `EvidencedTaxid` rather than a bare int because the manifest field is
    evidenced and something has to supply the basis. It used to take the int and stamp
    `basis="asserted"` on it unconditionally -- including for a taxid a human typed on the command
    line, which is `user_confirmed` and not the same claim at all.
    """
    return EvidencedTaxid(value=value, basis="user_confirmed", rung=0)


def _manifest_from(paths: list[Path], tech: str, reg: OnlistRegistry) -> DatasetManifest:
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    return fill_manifest(
        result=out.result,
        spec=kb.load_spec(tech),
        observations=[probe_file(p) for p in paths],
        registry=reg,
        experiment=ExperimentInputs(organism=_taxid(6239), accessions=["PRJNA1027859"]),
        seqforge_version=__version__,
    )


def test_a_manifest_uri_keeps_the_path_relative_to_the_dataset_root(tmp_path: Path) -> None:
    """A URI is RELATIVE, not FLAT — the manifest forbids an absolute path, not structure.

    Found by running the pilot dataset, which `fasterq-dump` had written one directory per accession
    (`SRX24283130/SRR28716558_1.fastq.gz`). Bare basenames made `compose --fastq-dir <root>` resolve
    to `<root>/SRR28716558_1.fastq.gz` — a path that does not exist, inside a units.tsv that looks
    entirely reasonable. No test saw it because every fixture until now put its FASTQs in one flat
    directory.

    Two runs in sibling directories, which is the shape that has structure to lose. A dataset whose
    files all sit in ONE directory has that directory as its root, so its URIs are basenames and
    always were — that is the same rule, not an exception to it, and it is why every existing fixture
    stayed green.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reg = _registry_for(spec)
    paths = []
    for run, seed in (("SRX999", 0), ("SRX998", 1)):
        reads = kb.generate_reads(spec, n=600, seed=seed)
        for k in ("R1", "R2"):
            p = tmp_path / run / f"{run}_{k}.fastq.gz"
            p.parent.mkdir(parents=True, exist_ok=True)
            _write_fastq_gz(p, reads[k])
            paths.append(p)

    manifest = _manifest_from(paths, "10x-3p-gex-v3", reg)
    uris = sorted(f.uri for f in manifest.library.files)
    assert uris == [
        "SRX998/SRX998_R1.fastq.gz",
        "SRX998/SRX998_R2.fastq.gz",
        "SRX999/SRX999_R1.fastq.gz",
        "SRX999/SRX999_R2.fastq.gz",
    ], f"the subdirectory was dropped: {uris}"
    # ...and the whole point: joined to the root, each URI is the file that was actually probed.
    for f in manifest.library.files:
        assert (tmp_path / f.uri).is_file()


def test_a_flat_dataset_still_gets_bare_basenames(tmp_path: Path) -> None:
    """One directory IS the root, so its URIs are basenames -- the same rule, not an exception."""
    spec = kb.load_spec("10x-3p-gex-v3")
    reg = _registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    paths = []
    for k in ("R1", "R2"):
        p = tmp_path / f"s_{k}.fastq.gz"
        _write_fastq_gz(p, reads[k])
        paths.append(p)
    manifest = _manifest_from(paths, "10x-3p-gex-v3", reg)
    assert sorted(f.uri for f in manifest.library.files) == ["s_R1.fastq.gz", "s_R2.fastq.gz"]


def test_two_runs_with_the_same_basename_do_not_collapse_to_one_uri(tmp_path: Path) -> None:
    """The silent half of the same bug, and the reason this is a correctness fix and not ergonomics.

    A basename is not unique across a dataset. Two runs each carrying `reads_1.fastq.gz` in their own
    directory produce the same URI — and `compose._units` looks files up BY URI, so one run's reads
    quietly become the other's. The matrices come out plausible and wrong, which is the failure class
    this project exists to prevent. Nothing anywhere would have said so.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reg = _registry_for(spec)
    paths = []
    for run, seed in (("runA", 0), ("runB", 1)):
        reads = kb.generate_reads(spec, n=600, seed=seed)
        for k in ("R1", "R2"):
            p = tmp_path / run / f"reads_{k}.fastq.gz"  # IDENTICAL basenames across the two runs
            p.parent.mkdir(parents=True, exist_ok=True)
            _write_fastq_gz(p, reads[k])
            paths.append(p)

    manifest = _manifest_from(paths, "10x-3p-gex-v3", reg)
    uris = [f.uri for f in manifest.library.files]
    assert len(set(uris)) == len(paths) == 4, f"URIs collided across runs: {uris}"
    assert len({f.sha256 for f in manifest.library.files}) == 4


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
    # One label per member of the class, and the twin keeps its OWN curie. `assay` used to be a
    # single EvidencedAssay, so v3.1's EFO:0022980 was silently dropped and the manifest read as if
    # `assay` and `chemistry` disagreed.
    assert [a.chemistry for a in manifest.library.assay] == ["10x-3p-gex-v3", "10x-3p-gex-v3.1"]
    assert [a.curie for a in manifest.library.assay] == ["EFO:0009922", "EFO:0022980"]
    # ...and the name is a human's answer to "what IS EFO:0009922", straight from EFO.
    assert [a.name for a in manifest.library.assay] == ["10x 3' v3", "10x 3' v3.1"]
    roles = {f.basename: (f.read_id if f.read_id else None) for f in manifest.library.files}
    assert roles == {"s_R1.fastq.gz": "R1", "s_R2.fastq.gz": "R2"}
    # the manifest carries a relative uri, never the probe's absolute local path
    assert all(not f.uri.startswith("/") for f in manifest.library.files)


def test_the_dataset_manifest_carries_no_intent(tmp_path: Path) -> None:
    """A dataset does not know how it will be processed, because it will be processed many ways."""
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
    reads = {r.read_id: r for r in manifest.library.read_layout.reads}
    assert (reads["R1"].min_len, reads["R1"].max_len) == (28, 28)  # fixed barcode read
    assert reads["R2"].min_len < reads["R2"].max_len  # open-ended cDNA is variable
    cb = next(e for e in reads["R1"].elements if e.role == "CB")
    assert (cb.start, cb.length) == (0, 16)


def test_manifest_hash_is_stable_and_matches_provenance(tmp_path: Path) -> None:
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    assert dataset_content_hash(manifest) == manifest.provenance.dataset_hash
    assert manifest.provenance.kb_version == kb.KB_VERSION


def test_manifest_file_order_is_deterministic_regardless_of_probe_order(tmp_path: Path) -> None:
    """`library.files` — and the immutable dataset content hash over it — must not depend on the order
    probe returned observations. A forked probe pool assembles them in completion order, not submission
    order, so `_build_files` sorts by content hash. GSE208154 hashed differently at --cpus 1 vs 4
    before this; the fix is what makes the manifest genuinely content-addressed.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reg = _registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    paths = []
    for k in ("R1", "R2"):
        p = tmp_path / f"s_{k}.fastq.gz"
        _write_fastq_gz(p, reads[k])
        paths.append(p)
    forward = _manifest_from(paths, "10x-3p-gex-v3", reg)
    reverse = _manifest_from(list(reversed(paths)), "10x-3p-gex-v3", reg)
    assert [f.sha256 for f in forward.library.files] == [f.sha256 for f in reverse.library.files]
    assert dataset_content_hash(forward) == dataset_content_hash(reverse)


# ---------- the dataset is immutable; the processing manifest is plural ----------
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
    assert all(blk.remedy for blk in report.blockers)  # every refusal is actionable


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
    assert all(b.remedy for b in report.blockers)  # every refusal is actionable


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
            experiment=ExperimentInputs(organism=_taxid(559292)),
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
    # The wiring gate must PASS, not skip. This assertion used to read `in {"pass", "skip"}` and so
    # forbade only "fail" -- the one value that could not occur, because `snakemake` was in no
    # dependency table, `have("snakemake")` was False, and the gate returned "skip" every time. A
    # skip is green, so the gate was decorative for the life of the repo. `snakemake-minimal` is now
    # declared in every environment that runs this suite; if it ever goes missing, that is a broken
    # environment and this test says so instead of quietly covering nothing.
    assert result.gate["wiring"] == "pass"
    # e2e stays skip: it is the real count-matrix run and belongs to `seqforge kb e2e`, never to
    # compose. Its toolchain (STAR, liulab-genome, a cluster) is genuinely absent here.
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
    # The whitelist token resolved to a PATH, and compose did not write the file. `rule onlist`
    # builds it and `temp()` deletes it: 10x's real v3 list is 111 MB of text, and writing it into
    # every run directory at compile time cost a third of a gigabyte for one dataset compiled three
    # ways -- for a file STAR opens once. Compose still VERIFIES the registry can produce it, which
    # is the compile-time refusal that matters.
    assert config["solo"]["soloCBwhitelist"] == "onlists/3M-february-2018.txt"
    assert not (pipeline_dir / config["solo"]["soloCBwhitelist"]).exists()

    units = (tmp_path / result.units_path).read_text().splitlines()
    assert units[0].split("\t") == ["sample_id", "run", "read_id", "path"]
    assert len(units) == 3  # header + 2 reads


def test_compose_bd_enhanced_derives_the_adapter_anchored_starsolo_recipe(tmp_path: Path) -> None:
    """BD Rhapsody Enhanced compiles to the adapter-anchored STARsolo recipe endorsed on STAR #1607.

    The diversity insert floats every offset, so the geometry cannot be a read-start quadruple: compose
    DERIVES `soloAdapterSequence` from the linker elements and anchors the CB/UMI positions to that
    adapter (anchor 2 = its start, anchor 3 = its end). The exact strings are the maintainer-endorsed
    ones — an independent cross-check on the element geometry — and the params gate must still PASS with
    `soloAdapterSequence` now an owned (derived) key.
    """
    manifest, reg = _build(tmp_path, "bd-rhapsody-wta-enhanced-v1")
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    assert result.modules[0].name == "map/starsolo"
    assert result.gate["params"] == "pass"
    assert result.gate["wiring"] == "pass"

    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    solo = config["solo"]
    assert solo["soloType"] == "CB_UMI_Complex"
    assert solo["soloAdapterSequence"] == "NNNNNNNNNGTGANNNNNNNNNGACA"
    assert solo["soloCBposition"] == "2_0_2_8 2_13_2_21 3_1_3_9"
    assert solo["soloUMIposition"] == "3_10_3_17"
    assert solo["soloStrand"] == "Forward"
    # the diversity insert is absorbed by the adapter, so no read-start start/length is emitted
    assert "soloCBstart" not in solo and "soloUMIstart" not in solo
    # the module actually passes the adapter to STAR (a hand-written .smk, so grep the shipped source)
    smk = (tmp_path / result.config_path).parent / "starsolo.smk"
    assert "--soloAdapterSequence" in smk.read_text()


def test_the_composer_records_the_run_each_unit_came_from(tmp_path: Path) -> None:
    """units.tsv carries a ``run`` column, from the same `run_key` that grouped the dataset.

    Recording the run is what lets the mapping module pair a pooled sample's mates without re-parsing
    filenames. The value must be `resolve.group.run_key`, not a second notion of "run" -- one function
    owns it.
    """
    from seqforge.compose.core import _units
    from seqforge.resolve.group import run_key

    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    rows = _units(manifest)
    assert rows and all(set(r) >= {"sample_id", "run", "read_id", "path"} for r in rows)
    for r in rows:
        assert r["run"] == run_key(r["path"])


def test_a_sample_pooled_across_runs_pairs_and_comma_joins_readfilesin(tmp_path: Path) -> None:
    """A pooled (multi-run) sample must reach STAR comma-joined per mate AND with mates paired by run.

    Two real bugs hid here, both only on multi-run samples -- single-run fixtures never exercised the
    path:
      1. space-joining a mate's files (``{input.cdna} {input.barcode}`` over a multi-file input) made
         STAR read them as extra MATES and segfault;
      2. mates listed in different run order desync STAR ("quality string length is not equal to
         sequence length") -- it pairs cDNA of run K with barcodes of run J.

    Pairing is driven by the units.tsv ``run`` column (seqforge's own grouping), NOT the filename. To
    prove that, the fixture gives run ``r1`` alphabetically-late filenames and run ``r2`` early ones,
    lists the rows scrambled, and asserts the planned command orders both mates by RUN (r1 then r2) --
    the opposite of what sorting by filename would produce. Generalises the fix; nothing is 2-run
    specific.
    """
    import subprocess

    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    pipeline_dir = (tmp_path / result.snakefile_path).parent

    units_path = pipeline_dir / "units.tsv"
    header = units_path.read_text().splitlines()[0].split("\t")
    assert header == ["sample_id", "run", "read_id", "path"]
    sid = units_path.read_text().splitlines()[1].split("\t")[0]
    # run -> {role -> filename}; filename order is the REVERSE of run order, so a filename sort would
    # mispair. read_files_in is cdna=R2, barcode=R1.
    f = {
        "r1": {"R1": "z_bc.fastq.gz", "R2": "z_cdna.fastq.gz"},
        "r2": {"R1": "a_bc.fastq.gz", "R2": "a_cdna.fastq.gz"},
    }
    # rows deliberately SCRAMBLED across mates
    rows = [
        [sid, "r2", "R2", f["r2"]["R2"]],
        [sid, "r1", "R1", f["r1"]["R1"]],
        [sid, "r2", "R1", f["r2"]["R1"]],
        [sid, "r1", "R2", f["r1"]["R2"]],
    ]
    units_path.write_text("\n".join("\t".join(r) for r in [header, *rows]) + "\n")
    for _sid, _run, _rid, path in rows:  # `snakemake -n` needs its source inputs to exist
        (pipeline_dir / path).touch()

    proc = subprocess.run(
        ["snakemake", "-d", str(pipeline_dir), "-s", str(pipeline_dir / "Snakefile"), "-n", "-p"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout + proc.stderr

    # cdna=R2 first, then barcode=R1; each mate comma-joined AND both ordered by run (r1, then r2).
    expected = f"--readFilesIn {f['r1']['R2']},{f['r2']['R2']} {f['r1']['R1']},{f['r2']['R1']}"
    assert expected in out, (
        f"mates must comma-join and pair by the run column; got: "
        f"{[ln for ln in out.splitlines() if 'readFilesIn' in ln]}"
    )


def test_compose_emits_a_snakefile_even_when_no_gate_runs(tmp_path: Path) -> None:
    """The Snakefile is the DELIVERABLE, so nothing optional may be its reason for existing.

    It used to be written inside `wiring_gate`, after an early `return "skip"` when `snakemake` was
    absent from PATH — and `snakemake` was in no dependency table, so that branch always taken. The
    product of the compiler was a side effect of a validation step that could not fire, and `compose`
    exited 0 having emitted nothing runnable.

    `run_wiring_gate=False` is the sharpest way to state the invariant: no gate ran, and the
    deliverable is still on disk and still complete.
    """
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    result = compose(
        manifest, _processing(manifest), registry=reg, workspace=tmp_path, run_wiring_gate=False
    )
    assert result.gate["wiring"] == "skip"
    snakefile = tmp_path / result.snakefile_path
    assert snakefile.is_file(), "compose ran a gate-free path and emitted no Snakefile"
    assert get_module("map/starsolo").snakefile.name in snakefile.read_text()


def test_the_wiring_gate_leaves_no_zero_byte_fastq_in_the_run_directory(tmp_path: Path) -> None:
    """The gate stands in zero-byte FASTQs; they must never land where the pipeline will read them.

    They were touched straight into the run directory (`pipeline_dir / row["path"]`) and never
    removed. Invisible only because the gate never ran: `snakemake` was undeclared. The moment it
    ran, the run directory would hold zero-byte files named exactly like the FASTQs, STAR would read
    them, and the pipeline would emit an empty matrix and **exit 0**.

    That is the failure this whole project exists to prevent — silent, plausible, wrong — and it
    would have been introduced by the very commit that made the gate work.
    """
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    assert result.gate["wiring"] == "pass", (
        "the gate must have actually run for this to mean anything"
    )
    run_dir = (tmp_path / result.snakefile_path).parent
    strays = [p for p in run_dir.rglob("*") if p.suffix == ".gz" and p.stat().st_size == 0]
    assert not strays, f"the gate left zero-byte stand-ins in the run dir: {strays}"


def test_the_wiring_gate_fails_a_workflow_that_plans_nothing(tmp_path: Path) -> None:
    """A dry run that plans zero jobs exits 0. The gate must not read that as success.

    This is what the wrapper did for the life of the repo: `configfile:` + `include:` parses clean,
    lists every rule, and plans **nothing**, because an `include:`d rule is not a default target.
    Exit code 0. A gate that only checks the return code cannot tell "correct" from "did nothing",
    so it has to look at the output.

    Rather than trust that argument, this builds the broken wrapper on purpose and asserts the gate
    catches it.
    """
    from seqforge.compose.gates import wiring_gate

    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    p = plan(manifest, _processing(manifest), registry=reg)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "config.yaml").write_text(yaml.safe_dump(p.config, sort_keys=True))
    (run_dir / "units.tsv").write_text(core._units_tsv(p.units))
    for rel, lines in p.onlist_files.items():
        t = run_dir / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("\n".join(lines) + "\n")
    module = get_module(p.module.name)
    # the OLD wrapper, verbatim in shape: include: instead of module/use rule
    (run_dir / "Snakefile").write_text(
        f'configfile: "config.yaml"\ninclude: "{module.snakefile.resolve()}"\n'
    )
    assert wiring_gate(run_dir, p) == "fail", (
        "an include:-only wrapper plans zero jobs and exits 0; the gate must not call that a pass"
    )


def _dry_run(pipeline_dir: Path, p: core.ComposePlan) -> str:
    """The wiring gate's dry run, but returning the PLAN instead of a verdict."""
    import shutil
    import subprocess

    from seqforge.compose.gates import _replica

    scratch = _replica(pipeline_dir, p)
    try:
        proc = subprocess.run(
            ["snakemake", "-d", str(scratch), "-s", str(scratch / "Snakefile"), "-n", "-p"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout + proc.stderr
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_the_composed_pipeline_plans_the_h5ad_as_its_deliverable(tmp_path: Path) -> None:
    """The pilot's product is a matrix a human can open, so the default target must BE that file.

    `rule all` used to demand `directory(Solo.out)`, which meant a green pipeline ended in a folder
    of Matrix Market files — and, worse, that STAR writing three of five features and exiting 0 was
    indistinguishable from success, since the directory existed either way.

    This reads the actual plan rather than the exit code, for the reason the gate does: a dry run
    that plans NOTHING also exits 0.
    """
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    pipeline_dir = (tmp_path / result.snakefile_path).parent
    p = plan(manifest, _processing(manifest), registry=reg)

    planned = _dry_run(pipeline_dir, p)
    assert "solo_to_h5ad" in planned, "the packaging step is not reachable from the default target"
    # `-p` renders every shell block while planning, which is the only reason this is visible at all
    # (a `run:` block would be opaque here) — and it is why the packaging step is a `shell:`.
    assert "seqforge io h5ad" in planned
    sample = manifest.experiment.samples[0].sample_id
    assert f"rule all:\n    input: results/{sample}/{sample}.h5ad" in planned, (
        f"the default target is not the deliverable. Planned:\n{planned}"
    )
    assert f"{sample}.velocyto.h5ad" in planned


def test_every_seqforge_verb_a_shipped_module_shells_out_to_exists() -> None:
    """A module's `shell:` naming a verb we renamed fails hours into a run, on a compute node.

    Derived from the live Typer app on one side and the module source on the other, so neither can be
    kept true by hand. This is `test_skill_documents_only_real_cli_verbs` pointed at the other place
    that hardcodes our own CLI — and the shipped modules are the more expensive place to be wrong.
    """
    import typer

    from seqforge.cli import app

    def paths(a: typer.Typer, prefix: tuple[str, ...] = ()) -> set[str]:
        out = {
            " ".join((*prefix, c.name or (c.callback.__name__ if c.callback else "")))
            for c in a.registered_commands
        }
        for g in a.registered_groups:
            assert g.typer_instance is not None and g.name is not None
            out |= paths(g.typer_instance, (*prefix, g.name))
        return out

    known = paths(app)
    for name in list_modules():
        # `shell:` blocks only. Scanning the whole file reads the rule's own docstring — which says
        # "a `shell:` calling a seqforge verb" — and reports `seqforge verb` as missing. Same lesson
        # `keys_read_by` learned: a scanner pointed at prose cries wolf, and then gets deleted.
        for block in re.findall(
            r"shell:\s*\n\s*r?\"\"\"(.*?)\"\"\"", get_module(name).snakefile.read_text(), re.DOTALL
        ):
            # `[a-z0-9-]`, not `[a-z-]`: the first verb this test ever met was `h5ad`, and a
            # name-shaped regex that stops at a digit matches `io h` and reports *that* as missing.
            for verb in re.findall(r"\bseqforge ((?:[a-z][a-z0-9-]* ){0,2}[a-z][a-z0-9-]*)", block):
                # longest match first: `io h5ad` is a command; `io` alone is only its group
                words = verb.split()
                assert any(" ".join(words[:n]) in known for n in range(len(words), 0, -1)), (
                    f"{name} shells out to `seqforge {verb}`, which is not a registered verb"
                )


def _rule_blocks(snakefile: Path) -> dict[str, str]:
    """`rule <name>:` -> its body text. Snakemake rules are top-level and flat, so a split suffices."""
    text = snakefile.read_text()
    parts = re.split(r"^rule (\w+):$", text, flags=re.M)[1:]
    return dict(zip(parts[0::2], parts[1::2], strict=True))


def test_no_run_directive_rule_declares_a_container() -> None:
    """A `container:` on a `run:` rule is ACCEPTED AND SILENTLY IGNORED — so declaring one is a lie.

    Measured against snakemake's own source on 2026-07-15, not recalled from the docs: the container
    wrap lives in `snakemake/shell.py` and therefore only ever wraps a `shell:` command. A `run:`
    block executes Python in the snakemake process and never passes through it. Snakemake's own
    linter agrees — it excludes `is_run` rules from its "missing software definition" check.

    That makes this exactly the failure class this repo is built against: the directive parses, the
    dry run is clean, the pipeline exits 0, and the software was never pinned. Nothing else in the
    stack would say so, so this test does. `genome_index` is the rule that would tempt someone.
    """
    for name in list_modules():
        for rule, body in _rule_blocks(get_module(name).snakefile).items():
            if re.search(r"^\s{4}run:$", body, re.M):
                assert not re.search(r"^\s{4}container:", body, re.M), (
                    f"{name}:{rule} declares a container on a `run:` rule, where snakemake ignores "
                    f"it. Make it a `shell:` (see `solo_to_h5ad`) or drop the directive."
                )


def test_the_aligner_rule_runs_in_a_pinned_container() -> None:
    """The env name was recorded and read by nothing, so every run used whatever STAR was on PATH."""
    for name in list_modules():
        blocks = _rule_blocks(get_module(name).snakefile)
        aligner = [r for r, b in blocks.items() if "STAR --runMode alignReads" in b]
        assert aligner, (
            f"{name} has no rule that invokes STAR; this test is looking at the wrong one"
        )
        for rule in aligner:
            assert 'container: config["container"]' in blocks[rule], (
                f"{name}:{rule} invokes STAR with no container, so the aligner is whatever the "
                f"submitting shell happened to have"
            )
    # ...and the composer must actually emit the key those rules read.
    assert "container" in get_module("map/starsolo").required_config


def test_starsolo_count_clears_startmp_before_running_so_reruns_are_preemption_safe() -> None:
    """A preempted STAR leaves `_STARtmp` behind and ABORTS a rerun if it already exists.

    On a preemptible partition every requeued STARsolo alignment failed: STAR refuses to reuse
    `_STARtmp`, and snakemake cannot clean it because it is an undeclared output. So `starsolo_count`
    removes its own `_STARtmp` before invoking STAR, and it must do so *before* the STAR command or
    the abort still fires. `{params.prefix}` is `results/<sample>/`, so this clears
    `results/<sample>/_STARtmp`.
    """
    body = _rule_blocks(get_module("map/starsolo").snakefile)["starsolo_count"]
    star = body.find("STAR --runMode alignReads")
    assert star != -1, (
        "starsolo_count no longer invokes STAR; this test is looking at the wrong rule"
    )
    cleanup = body.find("rm -rf {params.prefix}_STARtmp")
    assert cleanup != -1, (
        "starsolo_count invokes STAR but never clears `_STARtmp`, so a preempted rerun aborts"
    )
    assert cleanup < star, (
        "starsolo_count clears `_STARtmp` AFTER invoking STAR, which is too late — STAR aborts on "
        "the stale dir before the cleanup runs"
    )


def test_the_cram_rule_runs_in_a_pinned_container() -> None:
    """`seqforge io cram` shells out to samtools — a runtime tool, exactly like STAR — so its rule
    must name the image, not run against whatever samtools the submitting shell happened to have.

    The samtools call is behind the verb, so the detectable signal at the module level is the verb
    invocation itself; the container is what pins the tool the verb reaches. This is the same guard as
    `test_the_aligner_rule_runs_in_a_pinned_container`, generalised: the line for a `container:` is
    "the rule ends up invoking an external runtime binary", not "is the aligner". The pure-seqforge
    steps (h5ad, onlist, the qc bundle) reach no such binary and correctly carry no container.
    """
    seen = False
    for name in list_modules():
        for rule, body in _rule_blocks(get_module(name).snakefile).items():
            if "seqforge io cram" in body:
                seen = True
                assert 'container: config["container"]' in body, (
                    f"{name}:{rule} runs `seqforge io cram` (which shells out to samtools) with no "
                    f"container, so the tool is whatever the submitting shell happened to have"
                )
    assert seen, "no rule runs `seqforge io cram`; this test is looking at the wrong place"


def test_the_container_is_a_liulab_runtime_env_and_nothing_is_defined_here() -> None:
    """We NAME liulab-runtime's artifact. Naming is the opposite of defining."""
    from seqforge.models.processing import RuntimeEnv
    from seqforge.workflows import RUNTIME_IMAGE, container_uri

    for env in get_args(RuntimeEnv):
        assert container_uri(env) == f"docker://{RUNTIME_IMAGE}:{env}"


def test_a_prebuilt_sif_beats_the_ghcr_tag_but_only_if_it_is_really_there(tmp_path: Path) -> None:
    """A compute node that cannot reach ghcr.io cannot pull; the lab prebuilds these images.

    The naming (`liulab-runtime_<env>.sif`) is read off liulab-runtime's own `build-sifs.sh`, whose
    header says apptainer is not even installed on the arc login node. A *missing* file falls back to
    the tag rather than emitting a path to nothing: a config naming an absent SIF fails on a node.
    """
    from seqforge.workflows import container_uri

    assert container_uri("align-rna", tmp_path).startswith("docker://")  # empty dir -> tag
    sif = tmp_path / "liulab-runtime_align-rna.sif"
    sif.touch()
    assert container_uri("align-rna", tmp_path) == str(sif.resolve())


def test_compose_refuses_a_recipe_whose_env_cannot_supply_the_aligner(tmp_path: Path) -> None:
    """`map/starsolo` in the `ml` env is a container with no STAR in it — refuse, never correct."""
    manifest, reg = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    processing = _processing(manifest)
    section = processing.processing.model_copy(
        update={"environment": processing.processing.environment.model_copy(update={"value": "ml"})}
    )
    broken = processing.model_copy(update={"processing": section})

    with pytest.raises(ComposeError, match="align-rna"):
        compose(manifest, broken, registry=reg, workspace=tmp_path)


def test_policy_takes_the_runtime_env_from_the_module_that_needs_it() -> None:
    """One owner. It was hardcoded `"align-rna"` beside a module that also declared `align-rna`."""
    for tech in kb.runnable_spec_ids():
        spec = kb.load_spec(tech)
        from seqforge.manifest.policy import processing_defaults

        assert (
            processing_defaults(spec).environment == get_module(spec.require_backend().module).env
        )


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
    """The headline use case, and the disk-state bug that would have broken it.

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
    """Disk is STATE, not INPUT.

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
    for tech in kb.runnable_spec_ids():  # abstract family nodes have no backend to compose
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


def test_soloBarcodeReadLength_stays_optional_and_is_passed_only_when_declared() -> None:
    """The "make it actually run" flag: 10x sets it, SPLiT-seq must not be forced to.

    STARsolo FATALs by default unless the barcode read is exactly CB+UMI long, so 10x v2/v3/v3.1 --
    whose R1 is routinely sequenced to 150 nt -- declare `soloBarcodeReadLength: 0` to disable the
    check, and `starsolo.smk` now passes it through. But the module reads it with `SOLO.get(...)`, not
    a subscript, on purpose: a subscript would make `keys_read_by` mark `solo.soloBarcodeReadLength` a
    REQUIRED config key, and the composer would then owe it for EVERY starsolo chemistry -- including
    SPLiT-seq, which does not declare it and whose params gate forbids emitting a key it does not own.
    This test pins that optionality, so a refactor to `SOLO["soloBarcodeReadLength"]` goes red here
    rather than on a compute node running SPLiT-seq.
    """
    assert "solo.soloBarcodeReadLength" not in get_module("map/starsolo").required_config


def test_soloBarcodeReadLength_reaches_STAR_for_10x_and_not_for_splitseq(tmp_path: Path) -> None:
    """Config-level for both chemistries, then the rendered STAR command when snakemake is present."""
    (tmp_path / "v3").mkdir()
    (tmp_path / "ss").mkdir()
    v3, v3_proc, v3_reg = _pair(tmp_path / "v3", "10x-3p-gex-v3", ("R1", "R2"))
    v3_config = plan(v3, v3_proc, registry=v3_reg).config["solo"]
    assert isinstance(v3_config, dict)
    assert v3_config["soloBarcodeReadLength"] == "0"  # 10x declares it, so compose emits it

    ss, ss_reg = _build(tmp_path / "ss", "splitseq")
    ss_config = plan(ss, _processing(ss), registry=ss_reg).config["solo"]
    assert isinstance(ss_config, dict)
    assert "soloBarcodeReadLength" not in ss_config  # SPLiT-seq does not, so it is not emitted

    import shutil as _shutil

    if not _shutil.which("snakemake"):
        pytest.skip("snakemake not installed")
    # ...and it reaches the actual STAR argv for 10x, and is absent (not a KeyError) for SPLiT-seq.
    v3_result = compose(v3, v3_proc, registry=v3_reg, workspace=tmp_path / "v3")
    v3_plan = _dry_run(
        (tmp_path / "v3" / v3_result.config_path).parent, core.plan(v3, v3_proc, registry=v3_reg)
    )
    assert "--soloBarcodeReadLength 0" in v3_plan

    ss_proc = _processing(ss)
    ss_result = compose(ss, ss_proc, registry=ss_reg, workspace=tmp_path / "ss")
    ss_plan = _dry_run(
        (tmp_path / "ss" / ss_result.config_path).parent, core.plan(ss, ss_proc, registry=ss_reg)
    )
    assert "--soloBarcodeReadLength" not in ss_plan


# ---------- consumer, not parallel universe ----------
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
    """Consumer, not parallel universe, as an AST check rather than a code-review habit.

    `Genome(assembly).build_star_index(gtf=name)` is a *consumer call* and is exactly right. A
    `def build_star_index` or `class Genome` in this tree is the opposite: it means the resolution
    of assemblies, annotations and indexes — liulab-genome's entire remit — is being duplicated
    here, where it will drift and where the "no absolute path in a manifest" rule stops being anybody's
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
    assert not offenders, "seqforge is redefining liulab-genome's job:\n" + "\n".join(offenders)


#: Every liulab-genome attribute seqforge calls. We are a consumer, and a consumer has an
#: import surface — this is it.
#:
#: **This is a hand-written list, and that is fine here, because it is checked against the REAL
#: package rather than against itself.** That distinction is the whole lesson of this repo: a list
#: mirroring code and validated by a test that reads the same list proves nothing (`required_config`);
#: a list asserted against the actual object is a contract test, and it goes red the moment upstream
#: moves. Nothing here can drift silently.
_GENOME_API = {
    "get_star_index",  # starsolo.smk / star.smk rule genome_index + e2e: resolve the prebuilt index
    "register_gtf",  # staging an annotation (see the consumer note in CLAUDE.md)
    "fasta_path",  # e2e: simulate reads from real sequence
    "default_gtf_path",  # e2e: build gene models
    "annotations",  # e2e/docs: which GTF names are registered
}


def test_seqforge_only_calls_liulab_genome_methods_that_exist() -> None:
    """The consumer surface is real, checked at test time, in every environment.

    `discover_assets` called `Genome.get_star_index(...)` — a method liulab-genome **has never had**.
    It was a lazy import, inside an arm that only runs on a cluster, against a dependency that was not
    declared, so nothing could have noticed: the `AttributeError` simply waited for whoever ran it. It
    waited until 2026-07-15.

    Same shape as the STAR-argv bug one commit earlier: two renderings of "how do I get an index",
    by hand, in two places that could not see each other — `starsolo.smk` said `build_star_index` and
    was right, `e2e.py` said `get_star_index` and was wrong, and the one nobody executed was the
    broken one.

    liulab-genome is a declared dependency now, so this check runs everywhere rather than on a cluster
    nobody visits. That is the fix; renaming the method was just the symptom.
    """
    from genome import Genome

    missing = sorted(name for name in _GENOME_API if not hasattr(Genome, name))
    assert not missing, (
        f"seqforge calls liulab-genome attributes that do not exist: {missing}. "
        f"Either upstream moved and our calls need updating, or this list has grown a name nobody "
        f"calls. Both are real; neither is silent any more."
    )


def test_the_genome_api_check_would_catch_a_method_that_does_not_exist() -> None:
    """Prove the guard discriminates — the names we call resolve, an invented one does not.

    This once asserted `not hasattr(Genome, "get_star_index")`, pinning the exact bug that broke on
    2026-07-15: e2e.py called `get_star_index` for the life of the repo and it never existed. On
    2026-07-17 liulab-genome added it as a resolve-only lookup, and seqforge switched every index
    reference to it (both the `genome_index` rule and e2e), dropping `build_star_index` entirely —
    seqforge never builds an index, it only resolves the prebuilt one. So `get_star_index` now
    resolves. The lesson the guard protects is unchanged — a name our code calls must exist on the
    real object — so this checks that the name we use resolves, and that an invented one still would
    not.
    """
    from genome import Genome

    assert hasattr(Genome, "get_star_index"), "seqforge resolves the prebuilt index through this"
    assert not hasattr(Genome, "resolve_star_index_please"), (
        "a name liulab-genome does not define must not resolve — else the guard proves nothing"
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
    """The other half of consumer-not-parallel-universe: an env is NAMED here and DEFINED in liulab-runtime.

    `RuntimeEnv` is a closed literal of liulab-runtime env names — there is deliberately no profile
    indirection, the name *is* the identifier. A conda YAML or Dockerfile appearing in this tree
    would mean we had started duplicating liulab-runtime, scattering env definitions across two
    repos that then disagree about which STAR ran.
    """
    from typing import get_args

    from seqforge.models.processing import RuntimeEnv

    assert set(get_args(RuntimeEnv)) == {"align-rna", "align-dna", "ml", "ml-gpu"}
    found = [str(p) for name in _ENV_DEFINITION_FILES for p in _src_root().rglob(name)]
    assert not found, f"seqforge is defining an aligner environment: {found}"


# ---------- emit data, never code ----------
#: A Snakemake rule definition. `rule x:` / `checkpoint x:` are the only ways to introduce rule
#: source, so this is the whole vocabulary the composer is forbidden from emitting.
_RULE_DEF = re.compile(r"^\s*(rule|checkpoint)\s+\w+\s*:", re.M)


def test_the_generated_wrapper_contains_no_rule_source() -> None:
    """Emit data, never code, at the ONE place seqforge writes Snakemake syntax at all.

    `compose` generates a Snakefile — the deliverable — and its own header says "rule source is never
    generated". Everything the pipeline actually executes must come from the hand-written `.smk`
    modules; the moment the composer emits a `rule`, that guarantee is gone and nobody finds out from a comment.

    Asserted against the template rather than a rendered instance because the template is the thing
    a future edit would change.
    """
    wrapper = core._WRAPPER
    assert not _RULE_DEF.search(wrapper), f"the composer emits rule source:\n{wrapper}"
    assert "configfile:" in wrapper  # it parameterises by data...
    assert "module " in wrapper and "use rule * from" in wrapper  # ...and composes by reference


def test_the_wrapper_makes_the_modules_rules_reachable_as_default_targets() -> None:
    """The deliverable must DO something when a user runs bare `snakemake`. It did not.

    Snakemake's default target is the first rule defined in the *main* Snakefile, and an `include:`d
    rule is not one. The wrapper was `configfile:` + `include:`, which parses clean, lists all three
    rules, and then plans **zero jobs**: "Nothing to be done", exit 0. Measured 2026-07-15 — the same
    module content inlined builds 3 jobs, via `include:` builds 0.

    Nothing caught it because the only thing that would run the wrapper was a gate that could not run
    (`snakemake` was in no dependency table), and the gate would not have caught it either: it
    checked the exit code, and planning nothing exits 0.

    `use rule * from m as *` re-declares the rules in this workflow, so `all` becomes a real default
    target. This test pins the property, not the spelling: a future wrapper may compose however it
    likes as long as bare `snakemake` still reaches the rules.
    """
    wrapper = core._WRAPPER
    assert "include:" not in wrapper, (
        "an `include:`d rule is not a default target -- the wrapper would plan zero jobs and exit 0"
    )
    assert "use rule * from" in wrapper


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
    """The other half of emit-data-never-code: the rules that DO exist are checked-in source, not build artifacts.

    A module whose rules were generated would defeat the wrapper check by moving the generation one
    step earlier, so the modules must be real files under version control, carrying the header that
    says what they are.
    """
    snakefile = get_module(module_name).snakefile
    assert snakefile.is_file(), f"{module_name}: {snakefile} is not on disk"
    text = snakefile.read_text()
    assert _RULE_DEF.search(text), f"{module_name} defines no rules — is it really a module?"
    assert "HAND-WRITTEN" in text and "NEVER machine-generated" in text


@pytest.mark.parametrize("module_name", list_modules())
def test_required_config_is_exactly_what_the_module_reads(module_name: str) -> None:
    """The contract is COMPUTED from the module source, so neither direction can drift.

    It used to be a hand-written tuple checked one way against a scanner that lived here in the test
    file. Both halves were wrong at once: it *under*-declared the four soloCB/UMI keys `starsolo.smk`
    has always dereferenced (a `KeyError` on a compute node, long after compose exited 0), and it
    *over*-declared `primary_feature` and `env`, which no rule reads and nothing checked.

    There is now one list and the module source is it, so this test asserts an identity rather than
    an inclusion. That reads as tautological and is not: it pins that `required_config` never goes
    back to being typed by hand, and `test_the_required_config_scanner_can_catch_an_undeclared_key`
    is what proves the derivation itself is not vacuous.
    """
    module = get_module(module_name)
    assert set(module.required_config) == set(keys_read_by(module.snakefile))
    assert module.required_config == tuple(sorted(module.required_config))


def test_the_required_config_scanner_can_catch_an_undeclared_key(tmp_path: Path) -> None:
    """Prove the scanner fires — a derived check that has never failed proves nothing.

    This is the load-bearing test of the pair: `required_config` is now *defined* as this scanner's
    output, so if the scanner silently missed a key, the identity test above would still pass while
    the composer dropped it. Both forms must be caught: the direct `config[...]` read and the
    `params` alias indirection that hid the real bug for as long as it existed. And prose in a
    comment must NOT be caught — the first draft of this scanner reported starsolo's own header as
    two undeclared keys.
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
    found = keys_read_by(smk)
    assert "solo.soloCBlen" in found  # the alias indirection resolves
    assert "solo.oops" in found  # ... and an undeclared one is visible
    assert "outdir" in found  # the direct form resolves
    assert "genome.assembly" in found  # ... including the nested form
    assert "units_tsv" in found  # the COMPOSER emits it now; no wrapper injects it
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


# ---------- the gate is where the parse/count line stops being a convention ----------
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
    assert any("count key" in problem for problem in problems)


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


# ---------- produce every answer rather than ask ----------
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

    We take the second: charitable, cheap, and consistent with counting everything by default. The instructed feature is UNIONed
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


def test_a_single_nucleus_prep_promotes_genefull_to_primary() -> None:
    """#12: a verified nuclei prep makes GeneFull the PRIMARY matrix — a nuclear library is ~1/3
    intronic and a Gene-first primary silently under-counts it. All five features stay; only the order
    (which becomes adata.X) changes, and Gene still follows so Velocyto's requirement holds."""
    from seqforge.manifest import DEFAULT_SOLO_FEATURES, resolve_features

    features, basis, evidence, warnings = resolve_features(prep_type="single-nucleus")
    assert features[0] == "GeneFull", "nuclei -> GeneFull primary"
    assert features[1] == "Gene", "Gene still present (Velocyto requires it) and right behind"
    assert set(features) == set(DEFAULT_SOLO_FEATURES), (
        "nothing dropped — one alignment, five counts"
    )
    assert basis == "inferred"  # code inferred the ordering from biology; no one asserted the list
    assert evidence == ["policy:genefull-primary-for-single-nucleus"]
    assert not warnings


def test_a_single_cell_prep_stays_gene_primary() -> None:
    from seqforge.manifest import resolve_features

    features, _, evidence, _ = resolve_features(prep_type="single-cell")
    assert features[0] == "Gene"
    assert evidence == ["policy:default-solo-features"]


def test_a_flag_or_instruction_beats_a_nuclei_prep() -> None:
    """The prep reorder is only the DEFAULT path: an explicit --quantify or a processing instruction
    is the user talking, and outranks a biology inference."""
    from seqforge.manifest import resolve_features

    flagged, _, _, _ = resolve_features(override=("Gene",), prep_type="single-nucleus")
    assert flagged == ["Gene"], "the flag wins outright, nuclei prep does not resurrect GeneFull"
    instructed, _, _, _ = resolve_features(
        instructions=[_ins("processing.quantification", "Velocyto")], prep_type="single-nucleus"
    )
    assert instructed[0] == "Velocyto", "an explicit instruction still sets the primary"


def _prep_assertion(value: str, *, verified: bool = True) -> object:
    from seqforge.models.assertion import Assertion, ExtractorProvenance, SourceSpan

    return Assertion(
        id="assert-prep-0",
        field="library.prep_type",
        value=value,
        span=SourceSpan(doc_sha256="0" * 64, quote=value),
        span_verified=verified,
        entailment_ok=verified,
        llm_confidence=0.9,
        extractor=ExtractorProvenance(model_id="test", prompt_version="test"),
    )


def test_prep_type_from_assertions_normalizes_the_biology_words() -> None:
    from seqforge.manifest import prep_type_from_assertions

    for phrase in ("single nuclei", "single-nucleus RNA-seq", "isolated nuclei", "snRNA-seq"):
        assert prep_type_from_assertions([_prep_assertion(phrase)]) == "single-nucleus", phrase
    for phrase in ("single-cell", "scRNA-seq", "whole cells"):
        assert prep_type_from_assertions([_prep_assertion(phrase)]) == "single-cell", phrase


def test_prep_type_matches_whole_words_not_bare_substrings() -> None:
    """The value steers which matrix is primary, so a bare "nucle"/"cell" substring must not classify:
    "nucleic acid" is not a nuclei prep and "Cell Ranger" is not single-cell. A phrase naming BOTH, or
    neither, resolves to None rather than a guess."""
    from seqforge.manifest.policy import _normalize_prep_type

    assert _normalize_prep_type("total nucleic acid extraction") is None  # not "nuclei"
    assert _normalize_prep_type("aligned with Cell Ranger") is None  # not "single-cell"
    assert _normalize_prep_type("nucleotide") is None
    assert (
        _normalize_prep_type("single-nucleus and single-cell were compared") is None
    )  # both -> None
    assert _normalize_prep_type("nuclei were isolated") == "single-nucleus"
    assert _normalize_prep_type("single cell suspension") == "single-cell"


def test_prep_type_from_assertions_ignores_unverified_and_refuses_a_disagreement() -> None:
    from seqforge.manifest import prep_type_from_assertions

    # an unverified claim never counts
    assert prep_type_from_assertions([_prep_assertion("single nuclei", verified=False)]) is None
    # two verified claims that disagree -> None, never a guess between them
    disagree = [_prep_assertion("single nuclei"), _prep_assertion("single-cell")]
    assert prep_type_from_assertions(disagree) is None
    # nothing to say
    assert prep_type_from_assertions([]) is None


def test_the_processing_cli_reads_prep_type_from_the_assertions_file(tmp_path: Path) -> None:
    """The CLI seam: `processing new` / `run` read the same assertions.json harvest wrote and pull the
    prep fact from it, so a single-nucleus paper reaches `resolve_features` without a new flag."""
    import json as _json

    from seqforge.cli.processing import _prep_type_from

    p = tmp_path / "assertions.json"
    p.write_text(
        _json.dumps(
            {
                "assertions": [
                    {
                        "id": "assert-prep-0",
                        "field": "library.prep_type",
                        "value": "single nuclei",
                        "span": {"doc_sha256": "0" * 64, "quote": "single nuclei"},
                        "span_verified": True,
                        "entailment_ok": True,
                        "llm_confidence": 0.9,
                        "extractor": {"model_id": "t", "prompt_version": "t"},
                    }
                ]
            }
        )
    )
    assert _prep_type_from(p) == "single-nucleus"
    assert _prep_type_from(None) is None


def test_two_instructions_disagreeing_is_a_conflict() -> None:
    """Same precedence, no tiebreak: a disagreement is surfaced for intent exactly as for truth."""
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


def test_validate_refuses_a_manifest_with_a_file_nobody_will_read(tmp_path: Path) -> None:
    """A file with no role is a file the pipeline drops in silence. That must be a Blocker.

    This is the check whose absence let a 6-run dataset validate clean while 5/6 of it evaporated:
    `resolve` did ONE global assignment across all 12 files, ten came back with `read_id=None`,
    `compose._units` skipped them without a word, and the manifest was content-addressed and blessed.
    Exit 0, wrong answer, no symptom.

    The inverse check ("is every declared role filled?") existed the whole time and passed, because it
    only ever needed ONE file per role. Both directions are needed; only one was there.
    """
    manifest, _ = _build(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    assert validate_manifest(manifest).ok, "the fixture must start clean or this proves nothing"

    files = list(manifest.library.files)
    files.append(
        files[0].model_copy(
            update={
                "read_id": None,
                "basename": "orphan.fastq.gz",
                "uri": "orphan.fastq.gz",
                "sha256": "f" * 64,
            }
        )
    )
    orphaned = manifest.model_copy(
        update={"library": manifest.library.model_copy(update={"files": files})}
    )
    report = validate_manifest(orphaned)
    assert not report.ok
    blocker = next(b for b in report.blockers if b.id.startswith("blk-unassigned-"))
    assert "orphan.fastq.gz" in blocker.message
    assert blocker.remedy, "a Blocker with no way forward is a wall"
    assert exit_code_for_report(report) == 3


# ---------- the whitelist is built by a rule, used, and deleted (point 9) ----------


def test_the_whitelist_is_a_rule_output_not_a_compile_time_write(tmp_path: Path) -> None:
    """111 MB of barcodes is a build artifact, and compose used to write it into every run dir.

    10x's v3 whitelist is 6 794 880 barcodes. It ships packed (522 kB of deltas) and expands to
    111 MB of text that STAR opens exactly once. Compose wrote that expansion into the run directory
    at compile time, permanently, per recipe -- so one dataset compiled three ways cost a third of a
    gigabyte of identical bytes nothing ever cleaned up.

    `temp()` was also meaningless before this rule existed: the whitelist was bound to
    `starsolo_count.input` with no producing rule, and snakemake cannot delete a file it did not make.
    """
    manifest, processing, reg = _pair(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    result = compose(manifest, processing, registry=reg, workspace=tmp_path)
    pipeline_dir = (tmp_path / result.config_path).parent
    assert not (pipeline_dir / "onlists").exists(), "compose wrote the whitelist"

    module = (_src_root() / "workflows" / "map" / "starsolo.smk").read_text()
    assert 'temp("onlists/{name}.txt")' in module
    assert "seqforge io onlist write" in module


def test_the_dry_run_plans_the_whitelist_and_marks_it_temporary(tmp_path: Path) -> None:
    """The gate that catches the mistake this change nearly made.

    A rule declared above `rule all` becomes the workflow's default target, and a default target with
    a wildcard is a hard snakemake error -- which is exactly what happened on the first attempt here.
    A dry run is the only thing that knows.
    """
    import shutil as _shutil

    if not _shutil.which("snakemake"):
        pytest.skip("snakemake not installed")
    manifest, processing, reg = _pair(tmp_path, "10x-3p-gex-v3", ("R1", "R2"))
    result = compose(manifest, processing, registry=reg, workspace=tmp_path)
    assert result.gate["wiring"] == "pass"
    p = core.plan(manifest, processing, registry=reg)
    plan_text = _dry_run((tmp_path / result.config_path).parent, p)
    assert "rule onlist" in plan_text, "the whitelist has no producing job in the plan"
    assert "3M-february-2018" in plan_text


def test_the_config_block_is_read_off_the_module_not_matched_on_its_name() -> None:
    """The last `== "map/starsolo"` in the tree, and the last silent "everything else is bulk".

    `param_block_key` was `"solo" if spec.backend.module == "map/starsolo" else "bulk"`, which is the
    same bug `read_layout_kind` was created to kill one function earlier: a third module gets its
    params written into a `bulk:` block it never reads, and the params gate agrees with the composer
    because the gate calls this same function. Two things wrong identically look, from inside a test,
    exactly like two things right.

    Now it is read off what the module source actually dereferences.
    """
    import ast

    from seqforge.workflows import MODULES, list_modules

    assert MODULES["map/starsolo"].param_block == "solo"
    assert MODULES["map/star"].param_block == "bulk"

    # A COMPARISON against a module name, not a mention of one: the docstrings deliberately keep the
    # old line so the bug it names stays findable. Grepping the text would forbid the record of the
    # fix along with the fix, which is how a guard teaches people to delete their own history.
    names = set(list_modules())
    offenders: list[str] = []
    for py in sorted(_src_root().rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.Compare) and any(
                isinstance(c, ast.Constant) and c.value in names for c in node.comparators
            ):
                offenders.append(f"{py.relative_to(_src_root())}:{node.lineno}")
    assert not offenders, (
        "a workflow module is being dispatched on by NAME. Every module that is not the one named "
        "silently takes the other branch, and nothing goes red — that is how `_read_files_in` "
        "emitted mate1/mate2 for a barcoded chemistry. Declare the fact on the module:\n"
        + "\n".join(offenders)
    )


def test_a_module_whose_config_contract_is_unreadable_refuses() -> None:
    """Guessing which block a module reads is how the wrong params reach an aligner."""
    from seqforge.workflows import WorkflowModule

    ghost = WorkflowModule(
        name="map/ghost",
        version="0.0.0",
        env="align-rna",
        snakefile=tmp_snakefile(),
        read_layout_kind="paired",
    )
    with pytest.raises(ValueError, match="exactly one of solo/bulk"):
        _ = ghost.param_block


def tmp_snakefile() -> Path:
    """A module that reads neither aligner-param block. Written to a real path: `required_config`
    scans the source, which is the whole point of it being derived."""
    import tempfile

    p = Path(tempfile.mkdtemp()) / "ghost.smk"
    p.write_text('rule x:\n    output: "y"\n    shell: "echo {config[outdir]}"\n')
    return p
