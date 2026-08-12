"""Tests for ``seqforge.manifest`` — filling, validating and hashing the two artifacts.

The immutable dataset (relative URIs, byte-derived roles, a content hash that must not move across a
processing sweep) and the plural recipe (the precedence ladder policy -> instruction -> flag). Shared
build helpers (``built_v3``, ``_build``, ``_manifest_from``, ``_processing`` …) live in
``tests/conftest.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    Built,
    SynthDataset,
    _build,
    _manifest_from,
    _processing,
    _taxid,
    registry_for,
    solo_block,
    write_fastq_gz,
)
from seqforge import __version__, kb
from seqforge.compose import ComposeError, plan
from seqforge.io import OnlistRegistry
from seqforge.manifest import (
    ExperimentInputs,
    FillError,
    Instruction,
    dataset_content_hash,
    exit_code_for_report,
    fill_manifest,
    processing_content_hash,
    run_id,
    validate_manifest,
    validate_processing,
)
from seqforge.manifest.hash import spec_content_hash
from seqforge.models.assertion import Assertion, ExtractorProvenance, SourceSpan
from seqforge.models.blocker import Blocker, BlockerCode, BlockerSubject
from seqforge.models.dataset import INDEX_ROLE, DatasetManifest, SampleGroup
from seqforge.models.resolve import ResolveResult
from seqforge.workflows import WORKFLOW_VERSION


def test_a_manifest_uri_keeps_the_path_relative_to_the_dataset_root(tmp_path: Path) -> None:
    """A URI is RELATIVE, not FLAT — the manifest forbids an absolute path, not structure.

    Found by running the pilot dataset, which `fasterq-dump` had written one directory per accession.
    Bare basenames made `compose --fastq-dir <root>` resolve to a path that does not exist, inside a
    units.tsv that looks entirely reasonable. Two runs in sibling directories, which is the shape
    that has structure to lose; a dataset whose files all sit in ONE directory has that directory as
    its root, so its URIs are basenames and always were — the same rule, not an exception.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reg = registry_for(spec)
    paths = []
    for run, seed in (("SRX999", 0), ("SRX998", 1)):
        reads = kb.generate_reads(spec, n=600, seed=seed)
        for k in ("R1", "R2"):
            p = tmp_path / run / f"{run}_{k}.fastq.gz"
            p.parent.mkdir(parents=True, exist_ok=True)
            write_fastq_gz(p, reads[k])
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


def test_two_runs_with_the_same_basename_do_not_collapse_to_one_uri(tmp_path: Path) -> None:
    """The silent half of the same bug, and why this is a correctness fix and not ergonomics.

    A basename is not unique across a dataset. Two runs each carrying `reads_1.fastq.gz` in their own
    directory produce the same URI — and `compose._units` looks files up BY URI, so one run's reads
    quietly become the other's: matrices that are plausible and wrong, with no symptom.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reg = registry_for(spec)
    paths = []
    for run, seed in (("runA", 0), ("runB", 1)):
        reads = kb.generate_reads(spec, n=600, seed=seed)
        for k in ("R1", "R2"):
            p = tmp_path / run / f"reads_{k}.fastq.gz"  # IDENTICAL basenames across the two runs
            p.parent.mkdir(parents=True, exist_ok=True)
            write_fastq_gz(p, reads[k])
            paths.append(p)

    manifest = _manifest_from(paths, "10x-3p-gex-v3", reg)
    uris = [f.uri for f in manifest.library.files]
    assert len(set(uris)) == len(paths) == 4, f"URIs collided across runs: {uris}"
    assert len({f.sha256 for f in manifest.library.files}) == 4


def test_fill_records_the_equivalence_class_and_byte_derived_roles(built_v3: Built) -> None:
    manifest, _ = built_v3
    # benign twins recorded together, basis observed
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
    # Never the probe's absolute local path. One directory IS the root, so a flat dataset's URIs are
    # bare basenames — the same relative-URI rule the nested fixture above proves, not an exception.
    assert sorted(f.uri for f in manifest.library.files) == ["s_R1.fastq.gz", "s_R2.fastq.gz"]


def test_the_dataset_manifest_carries_no_intent(built_v3: Built) -> None:
    """A dataset does not know how it will be processed, because it will be processed many ways."""
    manifest, _ = built_v3
    assert set(DatasetManifest.model_fields) == {"library", "experiment", "provenance"}
    # ...and its provenance carries no workflow_version: the assay happened before we had an opinion
    # about which rules would one day run over it.
    assert "workflow_version" not in type(manifest.provenance).model_fields


def test_processing_carries_the_derived_intent(built_v3: Built) -> None:
    manifest, _ = built_v3
    p = _processing(manifest)
    assert p.processing.aligner.value == "starsolo"
    assert p.processing.environment.value == "align-rna"
    assert p.processing.genome.value.assembly == "sacCer3"
    # basis records WHO DECIDED; policy defaults are `inferred` + an evidence ref naming the rule,
    # which is why no `policy_default` basis is needed.
    assert p.processing.quantification.basis == "inferred"
    assert p.processing.quantification.evidence == ["policy:default-solo-features"]
    assert p.provenance.workflow_version == WORKFLOW_VERSION


def test_fill_uses_observed_geometry_not_just_declared(built_v3: Built) -> None:
    manifest, _ = built_v3
    reads = {r.read_id: r for r in manifest.library.read_layout.reads}
    assert (reads["R1"].min_len, reads["R1"].max_len) == (28, 28)  # fixed barcode read
    assert reads["R2"].min_len < reads["R2"].max_len  # open-ended cDNA is variable
    cb = next(e for e in reads["R1"].elements if e.role == "CB")
    assert (cb.start, cb.length) == (0, 16)


def test_manifest_file_order_is_deterministic_regardless_of_probe_order(tmp_path: Path) -> None:
    """`library.files` — and the dataset content hash over it — must not depend on the order probe
    returned observations. A forked pool assembles them in completion order, so `_build_files` sorts
    by content hash. GSE208154 hashed differently at --cpus 1 vs 4 before that.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reg = registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    paths = []
    for k in ("R1", "R2"):
        p = tmp_path / f"s_{k}.fastq.gz"
        write_fastq_gz(p, reads[k])
        paths.append(p)
    forward = _manifest_from(paths, "10x-3p-gex-v3", reg)
    reverse = _manifest_from(list(reversed(paths)), "10x-3p-gex-v3", reg)
    assert [f.sha256 for f in forward.library.files] == [f.sha256 for f in reverse.library.files]
    assert dataset_content_hash(forward) == dataset_content_hash(reverse)


def test_dataset_hash_is_invariant_across_a_processing_sweep(built_v3: Built) -> None:
    """THE test for the whole split: change the intent, and what the data IS must not move.

    Aligning one dataset three ways is three processing manifests against one unchanged dataset hash,
    never three forks of the truth. If this ever goes red, the split has leaked.
    """
    manifest, _ = built_v3
    before = manifest.provenance.dataset_hash
    sweep = [
        _processing(manifest, processing_id="default"),
        _processing(manifest, assembly="ce11", annotation="WS298", processing_id="worm"),
        _processing(manifest, processing_id="template", pin=False),
    ]
    assert len({p.provenance.processing_hash for p in sweep}) == 3, "three recipes, three hashes"
    assert manifest.provenance.dataset_hash == before == dataset_content_hash(manifest)
    assert manifest.provenance.kb_version == kb.KB_VERSION

    # The processing hash matches its provenance AND ignores it: mutating ONLY the provenance section
    # must not move the content hash, because provenance STORES the hash and is never hashed.
    p = sweep[0]
    assert processing_content_hash(p) == p.provenance.processing_hash
    mutated = p.model_copy(
        update={"provenance": p.provenance.model_copy(update={"processing_hash": "0" * 64})}
    )
    assert processing_content_hash(mutated) == processing_content_hash(p), (
        "the content hash must exclude provenance -- it is what provenance carries"
    )


def _spec_hash(manifest: DatasetManifest) -> str:
    """The knowledge-base component of a run id, computed the way compose computes it (#361).

    Held CONSTANT across both sweeps below, because a second recipe and a stripped read count are
    exactly the things that do not touch the chemistry. It used to be spelled
    `manifest.provenance.kb_version`, which worked only as an opaque distinct string: the parameter is
    a content hash of one spec now, and a CalVer stamp read back out of provenance would misdescribe
    it at the call site.
    """
    return spec_content_hash(kb.load_spec(manifest.library.chemistry.value[0]))


def test_run_id_differs_per_processing_manifest(built_v3: Built) -> None:
    """One dataset x N processing manifests = N runs.

    `provenance_id(manifest_hash, kb, workflow)` could not express this: with intent folded into the
    manifest hash, two recipes over one dataset produced an IDENTICAL id, and the composer's fixed
    output path meant the second silently overwrote the first.
    """
    manifest, _ = built_v3
    a = _processing(manifest, processing_id="gene")
    b = _processing(manifest, assembly="ce11", annotation="WS298", processing_id="worm")
    ids = [
        run_id(
            dataset_hash=manifest.provenance.dataset_hash,
            processing_hash=p.provenance.processing_hash,
            spec_hash=_spec_hash(manifest),
            workflow_version=p.provenance.workflow_version,
        )
        for p in (a, b)
    ]
    assert ids[0] != ids[1]


def test_provenance_counts_the_reads_of_every_file_in_the_inventory(built_v3: Built) -> None:
    """Every file, every manifest — the counts compose cannot otherwise have.

    No read count exists anywhere else a composer can reach, and the field is populated
    unconditionally: gating it on the loaded KB declaring a threshold would make two manifests of the
    same bytes differ by the date they were written. 600 exactly is a statement about the FIXTURE —
    at n=600 the probe head reaches EOF, so the estimate is an exact count rather than the
    bytes-per-read extrapolation a budget-exhausting file gets.
    """
    manifest, _ = built_v3
    counts = manifest.provenance.estimated_reads
    assert set(counts) == {f.sha256 for f in manifest.library.files}
    assert set(counts.values()) == {600}


def test_the_read_counts_move_neither_the_dataset_hash_nor_the_run_id(built_v3: Built) -> None:
    """The counts are budget-dependent, so this is what keeps `--max-reads` out of the identity.

    Two mutations, because they fail differently. STRIPPED proves the field is not folded in at all;
    DOUBLED proves it is the *values* that are excluded and not merely an empty dict serializing
    away. Asserted against the fixture's own recorded hash, not only across the pair, so every
    manifest that existed before the field did still hashes to what it hashed to.
    """
    manifest, _ = built_v3
    prov = manifest.provenance
    assert prov.estimated_reads, "the fixture carries no counts, so this proves nothing"

    # Stripped by dropping the KEY, not by blanking the value: a manifest written before this field
    # existed is immutable and there is nothing to rewrite it from, so it has to load as-is.
    stripped = DatasetManifest.model_validate(
        {
            **manifest.model_dump(mode="json"),
            "provenance": {
                k: v for k, v in prov.model_dump(mode="json").items() if k != "estimated_reads"
            },
        }
    )
    assert stripped.provenance.estimated_reads == {}
    doubled = manifest.model_copy(
        update={
            "provenance": prov.model_copy(
                update={"estimated_reads": {k: v * 2 for k, v in prov.estimated_reads.items()}}
            )
        }
    )
    assert (
        dataset_content_hash(manifest)
        == dataset_content_hash(stripped)
        == dataset_content_hash(doubled)
        == prov.dataset_hash
    )

    p = _processing(manifest)
    ids = {
        run_id(
            dataset_hash=m.provenance.dataset_hash,
            processing_hash=p.provenance.processing_hash,
            spec_hash=_spec_hash(m),
            workflow_version=p.provenance.workflow_version,
        )
        for m in (manifest, stripped, doubled)
    }
    assert len(ids) == 1, "the read counts reached run_id"


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        pytest.param({0: 600, 1: 600}, 600, id="healthy-mates-agree"),
        pytest.param({0: 599, 1: 600}, 599, id="the-minimum-never-the-sum"),
        pytest.param({}, None, id="measured-nothing-is-not-zero"),
        pytest.param({0: 600}, None, id="one-of-two-is-not-a-measured-run"),
    ],
)
def test_a_runs_read_count_is_the_minimum_and_silence_is_not_zero(
    built_v3: Built, counts: dict[int, int], expected: int | None
) -> None:
    """Not the sum: R1 and R2 are two views of the same fragments, so adding them doubles the depth.

    Healthy mates are equal by construction and an unequal pair was refused upstream as truncated, so
    the minimum is free rather than pessimistic. An unmeasured file gates as `None`, never as `0`: a
    gate reading silence as zero would drop every sample in a pre-field manifest at exit 0.
    """
    manifest, _ = built_v3
    shas = [f.sha256 for f in manifest.library.files]
    assert len(shas) == 2, "the fixture must be a pair or these rows mean nothing"
    prov = manifest.provenance.model_copy(
        update={"estimated_reads": {shas[i]: n for i, n in counts.items()}}
    )
    assert prov.reads_in_run(shas) == expected


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


def test_validate_processing_blocks_a_genome_organism_mismatch(built_v3: Built) -> None:
    """A wrong-but-VALID assembly is the worst failure this system can produce: STAR aligns, exits 0,
    and emits a plausible matrix in the wrong coordinate space. Every other check catches something
    that would look broken; this one catches something that looks fine."""
    manifest, _ = built_v3  # organism = 559292 (yeast)
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


def test_validate_catches_referential_integrity_break(built_v3: Built) -> None:
    manifest, _ = built_v3
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
            Blocker(
                id="b",
                code=BlockerCode.TRUNCATED_GZIP,
                message="m",
                remedy="r",
                subject=BlockerSubject(kind="file", ref="f"),
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


def test_quantification_is_no_longer_decorative(built_v3: Built) -> None:
    """It used to be written to the manifest and then IGNORED by compose, which read the KB instead.

    Two sources of truth for one decision, unable to disagree only because one was never consulted.
    Change the manifest's intent, and the emitted config must follow it.
    """
    from seqforge.models.processing import SoloQuant

    manifest, reg = built_v3
    p = _processing(manifest)
    default = plan(manifest, p, registry=reg).config
    assert (
        solo_block(default)["soloFeatures"]
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
    assert solo_block(config)["soloFeatures"] == "GeneFull Gene"
    # ...and "which matrix is THE matrix" is emitted as a VALUE, not left as a positional convention:
    # STARsolo does not care about order, so the list order has no aligner-side referent.
    assert config["primary_feature"] == "GeneFull"


def test_bulk_never_gets_solo_features(synth_bulk_pe: SynthDataset) -> None:
    """Counting is MODULE-scoped: soloFeatures is meaningless to plain STAR, which is why
    Quantification is a discriminated union rather than a list."""
    manifest, reg = synth_bulk_pe.manifest, synth_bulk_pe.registry
    config = plan(manifest, _processing(manifest), registry=reg).config
    assert config["bulk"] == {"quantMode": "GeneCounts"}
    assert "solo" not in config
    assert "primary_feature" not in config  # bulk has no Solo.out/<Feature>/ split


def _ins(field: str, value: str) -> Instruction:
    return Instruction(field=field, value=value, basis="user_confirmed", evidence=["assert-x-0"])


# Spelled out rather than read off DEFAULT_SOLO_FEATURES: these rows are what pins the shipped five
# and their order, so deriving them from the constant would make every row pass by construction.
_FIVE = ["Gene", "GeneFull", "GeneFull_ExonOverIntron", "GeneFull_Ex50pAS", "Velocyto"]
_GENEFULL_FIRST = ["GeneFull", "Gene", "GeneFull_ExonOverIntron", "GeneFull_Ex50pAS", "Velocyto"]
_VELOCYTO_FIRST = ["Velocyto", "Gene", "GeneFull", "GeneFull_ExonOverIntron", "GeneFull_Ex50pAS"]
_QUANT = "processing.quantification"


@pytest.mark.parametrize(
    ("call", "features", "basis", "evidence", "warned"),
    [
        pytest.param({}, _FIVE, "inferred", ["policy:default-solo-features"], [], id="policy"),
        pytest.param(
            {"instructions": [_ins(_QUANT, "GeneFull")]},
            _GENEFULL_FIRST,
            "user_confirmed",
            ["assert-x-0"],
            [],
            id="prose-promotes-never-narrows",
        ),
        pytest.param(
            {"override": ("Gene", "GeneFull")},
            ["Gene", "GeneFull"],
            "user_confirmed",
            ["cli:--quantify"],
            ["FEATURES_NARROWED"],
            id="a-flag-replaces-exactly",
        ),
        pytest.param(
            {"instructions": [_ins(_QUANT, "Velocyto")], "override": ("Gene", "GeneFull")},
            ["Gene", "GeneFull"],
            "user_confirmed",
            ["cli:--quantify"],
            ["FEATURES_NARROWED"],
            id="a-flag-beats-an-instruction",
        ),
        pytest.param(
            {"prep_type": "single-nucleus"},
            _GENEFULL_FIRST,
            "inferred",
            ["policy:genefull-primary-for-single-nucleus"],
            [],
            id="nuclei-reorder-only",
        ),
        pytest.param(
            {"prep_type": "single-cell"},
            _FIVE,
            "inferred",
            ["policy:default-solo-features"],
            [],
            id="cells-take-no-reorder",
        ),
        pytest.param(
            {"override": ("Gene",), "prep_type": "single-nucleus"},
            ["Gene"],
            "user_confirmed",
            ["cli:--quantify"],
            ["FEATURES_NARROWED"],
            id="a-flag-beats-a-nuclei-prep",
        ),
        pytest.param(
            {"instructions": [_ins(_QUANT, "Velocyto")], "prep_type": "single-nucleus"},
            _VELOCYTO_FIRST,
            "user_confirmed",
            ["assert-x-0"],
            [],
            id="an-instruction-beats-a-nuclei-prep",
        ),
    ],
)
def test_the_counting_ladder_is_policy_then_prep_then_instruction_then_flag(
    call: dict[str, Any],
    features: list[str],
    basis: str,
    evidence: list[str],
    warned: list[str],
) -> None:
    """Only a flag narrows. Prose ("...aligned in GeneFull mode") and a verified nuclei prep PROMOTE
    to primary and drop nothing, which is why a model may source this at all: a hallucination can
    mislabel which matrix is `adata.X`, never destroy signal. Gene stays behind GeneFull so
    Velocyto's "requires Gene" holds by construction. Basis records who decided, and a policy default
    is `inferred` naming its rule — which is why no `policy_default` basis was ever needed.
    """
    from seqforge.manifest import resolve_features

    got, got_basis, got_evidence, warnings = resolve_features(**call)
    assert got == features
    assert got_basis == basis
    assert got_evidence == evidence
    assert [w.code for w in warnings] == warned
    # every narrowing cites the measured loss that justifies counting everything by default
    assert all("40.7%" in w.message for w in warnings)


_PREP = "library.prep_type"


def _assertion(
    field: str, value: str, *, doc: str = "0", aid: str = "assert-0", ok: bool = True
) -> Assertion:
    return Assertion(
        id=aid,
        field=field,
        value=value,
        span=SourceSpan(doc_sha256=doc * 64, quote=value),
        span_verified=ok,
        entailment_ok=ok,
        llm_confidence=0.9,
        extractor=ExtractorProvenance(model_id="m", prompt_version="p"),
    )


def test_prep_type_from_assertions_normalizes_the_biology_words() -> None:
    """The value steers which matrix is primary, so it matches WHOLE WORDS: a bare "nucle"/"cell"
    substring must not classify, and a phrase naming both or neither is `None` rather than a guess."""
    from seqforge.manifest import prep_type_from_assertions

    for phrase in ("single nuclei", "single-nucleus RNA-seq", "isolated nuclei", "snRNA-seq"):
        assert prep_type_from_assertions([_assertion(_PREP, phrase)]) == "single-nucleus", phrase
    for phrase in ("single-cell", "scRNA-seq", "whole cells", "single cell suspension"):
        assert prep_type_from_assertions([_assertion(_PREP, phrase)]) == "single-cell", phrase
    for none_phrase in (
        "total nucleic acid extraction",  # not "nuclei"
        "aligned with Cell Ranger",  # not "single-cell"
        "nucleotide",
        "single-nucleus and single-cell were compared",  # both -> None, never a guess
    ):
        assert prep_type_from_assertions([_assertion(_PREP, none_phrase)]) is None, none_phrase


def test_prep_type_from_assertions_ignores_unverified_and_refuses_a_disagreement() -> None:
    from seqforge.manifest import prep_type_from_assertions

    # an unverified claim never counts
    assert prep_type_from_assertions([_assertion(_PREP, "single nuclei", ok=False)]) is None
    # two verified claims that disagree -> None, never a guess between them
    disagree = [_assertion(_PREP, "single nuclei"), _assertion(_PREP, "single-cell")]
    assert prep_type_from_assertions(disagree) is None
    assert prep_type_from_assertions([]) is None  # nothing to say


def test_prep_type_read_off_harvests_artifact(tmp_path: Path) -> None:
    """The only path by which a paper's biology reaches `resolve_features`: both `processing new` and
    `run` read the prep out of `assertions.json` through here, so the reader is the seam, not the
    normalizer above. A pre-2026.7 bare list stays silent — the instruction read refuses that file by
    name, and one refusal is what the user should see.
    """
    from seqforge.manifest import prep_type_from_assertions_file

    path = tmp_path / "assertions.json"
    path.write_text(
        json.dumps(
            {
                "instruction_docs": [],
                "assertions": [_assertion(_PREP, "single nuclei").model_dump(mode="json")],
            }
        )
    )
    assert prep_type_from_assertions_file(path) == "single-nucleus"
    assert prep_type_from_assertions_file(None) is None, "no --assertions is not an error"

    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps([_assertion(_PREP, "single nuclei").model_dump(mode="json")]))
    assert prep_type_from_assertions_file(legacy) is None


def test_two_instructions_disagreeing_is_a_conflict() -> None:
    """Same precedence, no tiebreak: a disagreement is surfaced for intent exactly as for truth."""
    from seqforge.manifest import instructions_from_assertions

    field = "processing.genome.assembly"
    _, conflicts = instructions_from_assertions(
        [
            _assertion(field, v, doc="d", aid=f"assert-aa-{i}")
            for i, v in enumerate(("ce11", "hg38"))
        ],
        instruction_docs=frozenset({"d" * 64}),
    )
    assert len(conflicts) == 1
    assert conflicts[0].field == field
    assert conflicts[0].kind == "asserted_vs_asserted"
    assert conflicts[0].decidable_by == ["user"]  # the first real use of that vocabulary member
    assert {p.value for p in conflicts[0].positions} == {"ce11", "hg38"}


def test_an_instruction_from_a_reference_doc_never_becomes_an_instruction() -> None:
    from seqforge.manifest import instructions_from_assertions

    a = _assertion(_QUANT, "GeneFull", doc="e", aid="assert-bb-0")
    # not among the --instruction docs => dropped, not downgraded
    assert instructions_from_assertions([a], instruction_docs=frozenset()) == ([], [])
    ins, _ = instructions_from_assertions([a], instruction_docs=frozenset({"e" * 64}))
    assert [i.field for i in ins] == [_QUANT]


def _with_file(
    manifest: DatasetManifest, basename: str, *, read_id: str | None = None, sha: str = "f"
) -> DatasetManifest:
    """``manifest`` plus one more file, named ``basename`` and carrying ``read_id`` (default: none)."""
    files = list(manifest.library.files)
    files.append(
        files[0].model_copy(
            update={
                "read_id": read_id,
                "basename": basename,
                "uri": basename,
                "sha256": sha * 64,
            }
        )
    )
    return manifest.model_copy(
        update={"library": manifest.library.model_copy(update={"files": files})}
    )


def _unassigned_blocker(manifest: DatasetManifest) -> Blocker:
    report = validate_manifest(manifest)
    assert not report.ok
    return next(b for b in report.blockers if b.id.startswith("blk-unassigned-"))


def test_validate_refuses_a_manifest_with_a_file_nobody_will_read(built_v3: Built) -> None:
    """A file with no role is a file the pipeline drops in silence. That must be a Blocker.

    The check whose absence let a 6-run dataset validate clean while 5/6 of it evaporated: ten of
    twelve files came back with `read_id=None`, `compose._units` skipped them without a word, and the
    manifest was content-addressed and blessed. The inverse check ("is every declared role filled?")
    passed throughout, because it only ever needed ONE file per role. Both directions are needed.
    """
    manifest, _ = built_v3
    clean = validate_manifest(manifest)
    assert clean.ok, "the fixture must start clean or this proves nothing"
    assert exit_code_for_report(clean) == 0

    orphaned = _with_file(manifest, "orphan.fastq.gz")
    blocker = _unassigned_blocker(orphaned)
    assert "orphan.fastq.gz" in blocker.message
    assert blocker.remedy, "a Blocker with no way forward is a wall"
    assert exit_code_for_report(validate_manifest(orphaned)) == 3


def test_the_unassigned_remedy_is_unchanged_for_a_file_that_is_nobody_s_lane(
    built_v3: Built,
) -> None:
    """No layout role shares its designation => the lane-sibling diagnosis must NOT fire.

    Three ways to be nobody's lane: no designation at all; a designation no seated read shares; and —
    the one that decides where the INDEX_ROLE files go — a designation shared only with an
    index-tagged read. An index file is not a representative a lane was ever compared against, and a
    roleless file designated `I1` is by construction longer than the gate that would have tagged it.
    """
    manifest, _ = built_v3
    seated = _with_file(manifest, "s_L001_I1_001.fastq.gz", read_id=INDEX_ROLE, sha="e")

    for basename in ("sample_barcodes.fastq.gz", "s_L002_I2_001.fastq.gz"):
        remedy = _unassigned_blocker(_with_file(manifest, basename)).remedy
        assert "several runs" in remedy, basename
        assert "seqforge manifest fill" in remedy, basename

    stray = _unassigned_blocker(_with_file(seated, "s_L002_I1_001.fastq.gz"))
    assert "several runs" in stray.remedy, "an index read is nobody's representative"
    assert stray.subject.ref == "s_L002_I1_001.fastq.gz"
