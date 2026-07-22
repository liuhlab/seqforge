"""Unit tests for the ``seqforge.models`` single source of truth."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from seqforge import models as m

HEX64 = "a" * 64


def _valid_manifest() -> m.DatasetManifest:
    """Build a minimal, fully valid DATASET manifest (a §12-shaped 10x 3' v3 worm dataset)."""
    read_layout = m.ReadLayout(
        modality="rna",
        reads=[
            m.ReadDef(
                read_id="R1",
                strand="pos",
                min_len=28,
                max_len=28,
                elements=[
                    m.ReadElement(
                        role="CB",
                        region_type="barcode",
                        start=0,
                        length=16,
                        onlist_ref="3M-february-2018",
                    ),
                    m.ReadElement(role="UMI", region_type="umi", start=16, length=12),
                ],
            ),
            m.ReadDef(
                read_id="R2",
                strand="pos",
                min_len=25,
                max_len=91,
                elements=[m.ReadElement(role="cDNA", region_type="cdna", start=0)],
            ),
        ],
    )
    library = m.LibrarySection(
        # ONE decision, so ONE envelope. Everything else in `library` follows from the chemistry:
        # the assay labels, the layout, the per-file roles. They used to each carry their own copy of
        # this same confidence, which is how the pilot's manifest printed 0.750672 four times.
        chemistry=m.EvidencedChemistrySet(
            # equivalence class: benign twins recorded together, not silently picked
            value=["10x-3p-gex-v3", "10x-3p-gex-v3.1"],
            basis="observed",
            confidence=0.98,
            rung=3,
        ),
        assay=[
            m.AssayLabel(chemistry="10x-3p-gex-v3", curie="EFO:0009922", name="10x 3' v3"),
            m.AssayLabel(chemistry="10x-3p-gex-v3.1", curie="EFO:0022980", name="10x 3' v3.1"),
        ],
        read_layout=read_layout,
        onlists=[
            m.Onlist(
                name="3M-february-2018",
                uri="onlists/3M-february-2018.txt",
                sha256=HEX64,
                length=16,
                orientation_hint="forward",
                n_entries=6_794_880,
            ),
        ],
        files=[
            m.FileInventoryItem(
                uri="reads/SRR000_1.fastq.gz",
                basename="SRR000_1.fastq.gz",
                sha256=HEX64,
                size_bytes=123,
                read_id="R1",
            ),
            m.FileInventoryItem(
                uri="reads/SRR000_2.fastq.gz",
                basename="SRR000_2.fastq.gz",
                sha256=HEX64,
                size_bytes=456,
                read_id="R2",
            ),
        ],
    )
    experiment = m.ExperimentSection(
        organism=m.EvidencedTaxid(value=6239, basis="asserted", confidence=0.9, rung=0),
        accessions=m.EvidencedAccessionList(
            value=["PRJNA1027859"], basis="asserted", confidence=1.0, rung=0
        ),
        samples=[
            m.SampleGroup(
                sample_id="s1", file_uris=["reads/SRR000_1.fastq.gz", "reads/SRR000_2.fastq.gz"]
            )
        ],
    )
    return m.DatasetManifest(
        library=library,
        experiment=experiment,
        provenance=m.DatasetProvenance(
            dataset_hash=HEX64,
            kb_version="0.1",
            seqforge_version="2026.7.0",
        ),
    )


def _valid_processing() -> m.ProcessingManifest:
    """Build a minimal, fully valid PROCESSING manifest for the dataset above."""
    section = m.ProcessingSection(
        genome=m.EvidencedGenome(
            value=m.GenomeRef(assembly="ce11", annotation_name="WS298", ncbi_taxid=6239),
            basis="inferred",
            confidence=0.9,
            rung=0,
        ),
        aligner=m.EvidencedStr(value="starsolo", basis="inferred", confidence=1.0, rung=0),
        quantification=m.EvidencedQuantification(
            value=m.SoloQuant(features=["Gene", "GeneFull"]),
            basis="user_confirmed",
            evidence=["cli:--quantify"],
            confidence=1.0,
            rung=0,
        ),
        variant_calling=m.EvidencedBool(value=False, basis="inferred", confidence=1.0, rung=0),
        environment=m.EvidencedRuntimeEnv(
            value="align-rna", basis="inferred", confidence=1.0, rung=0
        ),
    )
    return m.ProcessingManifest(
        processing_id="default",
        dataset=m.DatasetPin(dataset_hash=HEX64, accessions=["PRJNA1027859"]),
        processing=section,
        provenance=m.ProcessingProvenance(
            processing_hash=HEX64, workflow_version="0.1", seqforge_version="2026.7.0"
        ),
    )


def test_manifest_round_trips_through_json() -> None:
    man = _valid_manifest()
    again = m.DatasetManifest.model_validate_json(man.model_dump_json())
    assert again == man


def test_chemistry_is_an_equivalence_class() -> None:
    man = _valid_manifest()
    assert man.library.chemistry.value == ["10x-3p-gex-v3", "10x-3p-gex-v3.1"]


def test_the_assay_cannot_disagree_with_the_chemistry_it_names() -> None:
    """One label per chemistry, so the §12 twin keeps its own CURIE instead of being dropped.

    The pilot's manifest printed `assay: EFO:0009922` beside `chemistry: [v3, v3.1]` and the user
    reasonably asked what the difference was. There is none -- they are one answer in two
    vocabularies -- but the assay field held one CURIE where the chemistry held two ids, so v3.1's
    own term (EFO:0022980) silently vanished and the two fields read as if they disagreed.
    """
    man = _valid_manifest()
    assert [a.chemistry for a in man.library.assay] == man.library.chemistry.value
    assert [a.curie for a in man.library.assay] == ["EFO:0009922", "EFO:0022980"]
    assert [a.name for a in man.library.assay] == ["10x 3' v3", "10x 3' v3.1"]


def test_one_decision_carries_exactly_one_confidence() -> None:
    """`confidence: 0.750672` appeared on four fields of the pilot's manifest. It was one number.

    Four envelopes filled from one variable cannot disagree, so they were never four truths. We ask
    that a value not travel without its provenance -- which one honest envelope does. A field
    repeated is decoration that looks like provenance, which is worse than none.
    """
    man = _valid_manifest()
    library = json.dumps(man.model_dump(mode="json")["library"])
    assert library.count('"confidence"') == 1, "library holds one decision, so one confidence"
    assert man.library.chemistry.confidence == 0.98
    assert not hasattr(man.library.read_layout, "confidence")
    assert all(isinstance(f.read_id, str | type(None)) for f in man.library.files)
    assert all(not hasattr(a, "confidence") for a in man.library.assay)


def test_a_sample_attribute_key_must_be_one_ncbi_defines() -> None:
    """No field enters the manifest without passing a validator, and the key space is NCBI's.

    `condition` was ours. No archive uses it, and a field named "condition" accepts anything you can
    call a condition -- which is how the pilot's extraction filed routine worm husbandry into it.
    """
    ok = m.SampleGroup(
        sample_id="s1",
        attributes={"strain": m.EvidencedStr(value="CQ758", basis="asserted", rung=0)},
    )
    assert ok.attributes["strain"].value == "CQ758"
    with pytest.raises(ValidationError, match="NCBI harmonized"):
        m.SampleGroup(
            sample_id="s1",
            attributes={"condition": m.EvidencedStr(value="daf-2", basis="asserted", rung=0)},
        )


@pytest.mark.parametrize(
    "bad_uri",
    [
        "/data/x.fastq.gz",
        "~/data/x.fastq.gz",
        "C:\\reads\\x.fastq.gz",
        "\\\\host\\share\\x",
        "file:///abs/x.fastq.gz",
    ],
)
def test_uri_rejects_absolute_and_local_paths(bad_uri: str) -> None:
    with pytest.raises(ValidationError):
        m.FileInventoryItem(uri=bad_uri, basename="x", sha256=HEX64, size_bytes=1)


@pytest.mark.parametrize(
    "ok_uri", ["reads/x.fastq.gz", "https://ftp.ena/x.fastq.gz", "s3://bucket/x", "SRR12345"]
)
def test_uri_accepts_relative_and_scheme_uris(ok_uri: str) -> None:
    item = m.FileInventoryItem(uri=ok_uri, basename="x", sha256=HEX64, size_bytes=1)
    assert item.uri == ok_uri


def test_segment_discriminated_union_dispatches_on_kind() -> None:
    obs_segments = [
        {"kind": "constant", "start": 22, "end": 44, "consensus": "GAGT", "purity": 0.98},
        {"kind": "random", "start": 0, "end": 16, "mean_entropy_bits": 1.99},
        {"kind": "homopolymer", "base": "T", "start": 44, "end": 60, "mean_run": 15.0},
    ]
    parsed = [
        m.ConstantSegment.model_validate(obs_segments[0]),
        m.RandomSegment.model_validate(obs_segments[1]),
        m.HomopolymerSegment.model_validate(obs_segments[2]),
    ]
    assert [s.kind for s in parsed] == ["constant", "random", "homopolymer"]


def test_evidenced_is_frozen() -> None:
    ev = m.EvidencedStr(value="starsolo", basis="inferred", confidence=1.0, rung=0)
    with pytest.raises(ValidationError):
        ev.value = "bwa"  # type: ignore[misc]


def test_accession_pattern_accepts_ena_and_ddbj() -> None:
    for acc in ["SRR123", "ERR456", "DRR789", "GSE1", "PRJNA1027859", "SAMEA123", "SAMD456"]:
        m.ExperimentSection(
            organism=m.EvidencedTaxid(value=6239, basis="asserted", confidence=1.0, rung=0),
            accessions=m.EvidencedAccessionList(
                value=[acc], basis="asserted", confidence=1.0, rung=0
            ),
            samples=[],
        )


def test_schema_export_covers_every_registered_model() -> None:
    for name in m.SCHEMA_MODELS:
        schema = m.export_schema(name)
        assert schema["title"] == name
    assert m.LLM_FACING.issubset(
        set(m.SCHEMA_MODELS) | {"ArbitrationRequest", "ArbitrationResponse"}
    )


def test_export_all_includes_both_manifests_and_defs() -> None:
    allschemas = m.export_all()
    # TWO artifacts, two schemas. A split that exported only one would silently lose coverage.
    for name in ("DatasetManifest", "ProcessingManifest"):
        assert name in allschemas
        assert "$defs" in allschemas[name]


def test_the_processing_manifest_is_not_llm_facing() -> None:
    """The LLM emits AssertionDraft; CODE composes the processing manifest. That is the emit-data-never-code boundary.

    If ProcessingManifest ever became a structured-output surface, a model would be authoring pipeline
    parameters directly instead of proposing claims that code adjudicates.
    """
    assert "ProcessingManifest" not in m.LLM_FACING
    assert m.LLM_FACING == {"AssertionDraft", "ArbitrationRequest", "ArbitrationResponse"}


def test_the_processing_manifest_refuses_an_unknown_key() -> None:
    """At the model: the instructable surface is ENUMERATED, so an unknown key is an error.

    It was a silent drop until 2026-07-15. `ProcessingSection(soloStrand="Reverse")` constructed
    happily and discarded the field, because the model set only `frozen=True` and inherited
    pydantic's `extra="ignore"`. The design claims "the recipe model forbids extras: an unknown key
    is a validation error, never a passthrough to a command line" — the second half was true (the
    params gate closes that path) and the first half was not.

    `soloStrand` is the case that matters: it is a PARSE decision, byte-decided and never
    instructable, and a wrong strand leaves STARsolo exiting 0 on a matrix that merely looks thin.
    A user has no vocabulary to say it — and now no way to say it quietly either.
    """
    valid = _valid_processing()
    section = valid.processing.model_dump()

    with pytest.raises(ValidationError) as exc:
        m.ProcessingSection(**section, soloStrand="Reverse")
    assert "soloStrand" in str(exc.value)

    with pytest.raises(ValidationError):
        m.ProcessingManifest.model_validate(valid.model_dump() | {"aligner": "star"})


def test_processing_manifest_round_trips_and_discriminates_quantification() -> None:
    p = _valid_processing()
    again = m.ProcessingManifest.model_validate_json(p.model_dump_json())
    assert again == p
    assert isinstance(again.processing.quantification.value, m.SoloQuant)
    assert again.processing.quantification.value.features == ["Gene", "GeneFull"]


def test_solo_quant_rejects_velocyto_without_gene() -> None:
    """STARsolo: "Velocyto quantification requires Gene features" — a real aligner constraint.

    No enum can express "this member requires that one", which is the clearest proof that a closed
    vocabulary is not by itself armor. STAR would error out anyway — but only AFTER the download and
    the alignment we were amortizing, so we refuse first.
    """
    with pytest.raises(ValidationError, match="Velocyto"):
        m.SoloQuant(features=["GeneFull", "Velocyto"])
    m.SoloQuant(features=["Gene", "Velocyto"])  # legal


def test_solo_quant_rejects_duplicates_and_emptiness() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        m.SoloQuant(features=["Gene", "Gene"])
    with pytest.raises(ValidationError):
        m.SoloQuant(features=[])


def test_solo_quant_rejects_a_feature_starsolo_does_not_have() -> None:
    """The closure is what makes span-verification non-vacuous for this field — see verify.entails."""
    with pytest.raises(ValidationError):
        m.SoloQuant(features=["GeneFullish"])


def test_export_schema_unknown_model_raises() -> None:
    with pytest.raises(KeyError):
        m.export_schema("NotAModel")


def test_the_module_graph_enforces_the_split() -> None:
    """The two-artifact split as an import graph, not as a comment.

    `dataset` and `processing` must never import each other. A dataset cannot know how it will be
    processed, because it will be processed many ways; and intent has no business reaching back into
    what the data is. Layering is base -> evidenced -> {dataset, processing}, and the two leaves never
    meet. If someone later needs a type from the other side, that is the split leaking and it should
    hurt here first — a docstring saying "must not" is not a constraint.
    """
    import ast
    from pathlib import Path

    root = Path(m.__file__).parent
    for module, forbidden in (("dataset", "processing"), ("processing", "dataset")):
        tree = ast.parse((root / f"{module}.py").read_text())
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert forbidden not in imported, (
            f"models/{module}.py imports {forbidden} — the split has leaked"
        )


def test_read_anchor_mirrors_the_kb_anchor() -> None:
    """The IR's ``ReadAnchor`` is a deliberate duplicate of the KB DSL's ``Anchor``; keep them equal.

    ``ReadAnchor`` is copied rather than imported because ``models`` is the schema-export source of
    truth and must not pull the ``kb`` -> ``probe`` -> ``models`` import chain back into itself. That
    duplication is exactly the shape this repo distrusts (a hand-kept mirror that drifts), so derive
    the check instead of trusting the copy: field names, annotations and defaults must match, so a
    field added to ``Anchor`` cannot silently stop being carried into the manifest.
    """
    from seqforge.kb.schema import Anchor
    from seqforge.models.dataset import ReadAnchor

    dsl = {k: (f.annotation, f.default) for k, f in Anchor.model_fields.items()}
    ir = {k: (f.annotation, f.default) for k, f in ReadAnchor.model_fields.items()}
    assert ir == dsl, (
        "ReadAnchor has drifted from kb.schema.Anchor — a floating element's addressing would be "
        f"dropped or misrecorded in the manifest.\n  DSL: {dsl}\n  IR:  {ir}"
    )
