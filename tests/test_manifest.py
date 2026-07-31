"""Tests for ``seqforge.manifest`` — filling, validating and hashing the two artifacts.

The immutable dataset (what the data IS: relative URIs, byte-derived roles, the content hash that
must not move across a processing sweep) and the plural recipe (what to DO with it: the precedence
ladder policy -> instruction -> flag, and the soloFeatures policy). One file per package, so an agent
editing ``manifest/`` knows which file to run.

The shared build helpers (``built_v3``, ``_build``, ``_manifest_from``, ``_processing`` …) live in
``tests/conftest.py`` — ``test_compose.py`` reads them too.
"""

from __future__ import annotations

from pathlib import Path

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
from seqforge.models.assertion import Assertion, ExtractorProvenance, SourceSpan
from seqforge.models.dataset import DatasetManifest, SampleGroup
from seqforge.models.resolve import ResolveResult
from seqforge.workflows import WORKFLOW_VERSION


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


def test_a_flat_dataset_still_gets_bare_basenames(tmp_path: Path) -> None:
    """One directory IS the root, so its URIs are basenames -- the same rule, not an exception."""
    spec = kb.load_spec("10x-3p-gex-v3")
    reg = registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    paths = []
    for k in ("R1", "R2"):
        p = tmp_path / f"s_{k}.fastq.gz"
        write_fastq_gz(p, reads[k])
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
    # the manifest carries a relative uri, never the probe's absolute local path
    assert all(not f.uri.startswith("/") for f in manifest.library.files)


def test_the_dataset_manifest_carries_no_intent(built_v3: Built) -> None:
    """A dataset does not know how it will be processed, because it will be processed many ways."""
    assert "processing" not in DatasetManifest.model_fields
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


def test_manifest_hash_is_stable_and_matches_provenance(built_v3: Built) -> None:
    manifest, _ = built_v3
    assert dataset_content_hash(manifest) == manifest.provenance.dataset_hash
    assert manifest.provenance.kb_version == kb.KB_VERSION


def test_manifest_file_order_is_deterministic_regardless_of_probe_order(tmp_path: Path) -> None:
    """`library.files` — and the immutable dataset content hash over it — must not depend on the order
    probe returned observations. A forked probe pool assembles them in completion order, not submission
    order, so `_build_files` sorts by content hash. GSE208154 hashed differently at --cpus 1 vs 4
    before this; the fix is what makes the manifest genuinely content-addressed.
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

    # The processing hash matches its provenance AND ignores it (folded from the old
    # test_processing_hash_matches_provenance_and_ignores_it, whose name promised the second half but
    # asserted only the first). Mutating ONLY the provenance section must not move the content hash:
    # provenance STORES the hash, it is never part of what is hashed.
    p = sweep[0]
    assert processing_content_hash(p) == p.provenance.processing_hash
    mutated = p.model_copy(
        update={"provenance": p.provenance.model_copy(update={"processing_hash": "0" * 64})}
    )
    assert processing_content_hash(mutated) == processing_content_hash(p), (
        "the content hash must exclude provenance -- it is what provenance carries"
    )


def test_run_id_differs_per_processing_manifest(built_v3: Built) -> None:
    """One dataset x N processing manifests = N runs.

    `provenance_id(manifest_hash, kb, workflow)` could not express this: with intent folded into the
    manifest hash, two recipes over one dataset produced an IDENTICAL id — and the composer's fixed
    output path meant the second silently overwrote the first. The collision case was exactly the use
    case the split exists for.
    """
    manifest, _ = built_v3
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
    """A wrong-but-VALID assembly is the worst failure this system can produce.

    It is not a crash and it does not look empty: STAR aligns, exits 0, and emits a plausible matrix
    in the wrong coordinate space. Every other check catches something that would look broken; this
    one catches something that looks fine.
    """
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


def test_the_default_counts_the_nuclear_features_without_being_asked(built_v3: Built) -> None:
    """The 40.7% defect, dissolved rather than answered.

    The KB used to bake soloFeatures:[Gene] into chemistry, so a single-NUCLEUS dataset compiled to
    Gene-only and silently dropped 40.7% of its signal — STARsolo exits 0 and the matrix merely looks
    thin. No nuclei/cells fact is asserted anywhere in this test, and none is needed: GeneFull is
    computed regardless. That is the whole point — we do not ask a question whose every answer we can
    afford to emit.
    """
    manifest, reg = built_v3
    features = solo_block(plan(manifest, _processing(manifest), registry=reg).config)[
        "soloFeatures"
    ]
    assert {"Gene", "GeneFull"} <= set(str(features).split())


def test_bulk_never_gets_solo_features(synth_bulk_pe: SynthDataset) -> None:
    """Counting is MODULE-scoped: soloFeatures is meaningless to plain STAR.

    A processing manifest that carried one shape unconditionally would be a type error the moment it
    met the other module — which is why Quantification is a discriminated union rather than a list.
    """
    manifest, reg = synth_bulk_pe.manifest, synth_bulk_pe.registry
    config = plan(manifest, _processing(manifest), registry=reg).config
    assert config["bulk"] == {"quantMode": "GeneCounts"}
    assert "solo" not in config
    assert "primary_feature" not in config  # bulk has no Solo.out/<Feature>/ split


def _ins(field: str, value: str) -> Instruction:
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

    # the complement (folded from test_a_single_cell_prep_stays_gene_primary): a single-CELL prep
    # takes no reorder — it stays Gene-primary on the default policy.
    sc_features, _, sc_evidence, _ = resolve_features(prep_type="single-cell")
    assert sc_features[0] == "Gene"
    assert sc_evidence == ["policy:default-solo-features"]


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


def _prep_assertion(value: str, *, verified: bool = True) -> Assertion:
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

    # Whole words, not bare substrings (folded from test_prep_type_matches_whole_words_not_bare_substr
    # ings, re-expressed through the public `prep_type_from_assertions` rather than the private
    # `_normalize_prep_type` it used to import). The value steers which matrix is primary, so a bare
    # "nucle"/"cell" substring must not classify, and a phrase naming BOTH or neither -> None.
    for none_phrase in (
        "total nucleic acid extraction",  # not "nuclei"
        "aligned with Cell Ranger",  # not "single-cell"
        "nucleotide",
        "single-nucleus and single-cell were compared",  # both -> None, never a guess
    ):
        assert prep_type_from_assertions([_prep_assertion(none_phrase)]) is None, none_phrase
    assert prep_type_from_assertions([_prep_assertion("nuclei were isolated")]) == "single-nucleus"
    assert prep_type_from_assertions([_prep_assertion("single cell suspension")]) == "single-cell"


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


def test_validate_refuses_a_manifest_with_a_file_nobody_will_read(built_v3: Built) -> None:
    """A file with no role is a file the pipeline drops in silence. That must be a Blocker.

    This is the check whose absence let a 6-run dataset validate clean while 5/6 of it evaporated:
    `resolve` did ONE global assignment across all 12 files, ten came back with `read_id=None`,
    `compose._units` skipped them without a word, and the manifest was content-addressed and blessed.
    Exit 0, wrong answer, no symptom.

    The inverse check ("is every declared role filled?") existed the whole time and passed, because it
    only ever needed ONE file per role. Both directions are needed; only one was there.
    """
    manifest, _ = built_v3
    clean = validate_manifest(manifest)
    assert clean.ok, "the fixture must start clean or this proves nothing"
    assert (
        exit_code_for_report(clean) == 0
    )  # clean report -> exit 0 (was test_validate_clean_manifest)

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
