"""Unit tests for the ``seqforge.models`` single source of truth."""

from __future__ import annotations

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
        assay=m.EvidencedAssay(value="EFO:0009922", basis="observed", confidence=0.9, rung=3),
        # equivalence class: benign twins recorded together, not silently picked
        chemistry=m.EvidencedChemistrySet(
            value=["10x-3p-gex-v3", "10x-3p-gex-v3.1"], basis="observed", confidence=0.98, rung=3
        ),
        read_layout=m.EvidencedReadLayout(
            value=read_layout, basis="observed", confidence=0.95, rung=2
        ),
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
                read_id=m.EvidencedStr(value="R1", basis="observed", confidence=0.99, rung=2),
            ),
            m.FileInventoryItem(
                uri="reads/SRR000_2.fastq.gz",
                basename="SRR000_2.fastq.gz",
                sha256=HEX64,
                size_bytes=456,
                read_id=m.EvidencedStr(value="R2", basis="observed", confidence=0.99, rung=2),
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
    # TWO artifacts, two schemas (R13). A split that exported only one would silently lose coverage.
    for name in ("DatasetManifest", "ProcessingManifest"):
        assert name in allschemas
        assert "$defs" in allschemas[name]


def test_the_processing_manifest_is_not_llm_facing() -> None:
    """The LLM emits AssertionDraft; CODE composes the processing manifest. That boundary is R1.

    If ProcessingManifest ever became a structured-output surface, a model would be authoring pipeline
    parameters directly instead of proposing claims that code adjudicates.
    """
    assert "ProcessingManifest" not in m.LLM_FACING
    assert m.LLM_FACING == {"AssertionDraft", "ArbitrationRequest", "ArbitrationResponse"}


def test_the_processing_manifest_refuses_an_unknown_key() -> None:
    """R14 at the model: the instructable surface is ENUMERATED, so an unknown key is an error.

    It was a silent drop until 2026-07-15. `ProcessingSection(soloStrand="Reverse")` constructed
    happily and discarded the field, because the model set only `frozen=True` and inherited
    pydantic's `extra="ignore"`. The brief claimed "the recipe model forbids extras: an unknown key
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
    the alignment we were amortizing, so we refuse first (R4).
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
    """The closure is what makes R5 non-vacuous for this field — see verify.entails."""
    with pytest.raises(ValidationError):
        m.SoloQuant(features=["GeneFullish"])


def test_export_schema_unknown_model_raises() -> None:
    with pytest.raises(KeyError):
        m.export_schema("NotAModel")


def test_the_module_graph_enforces_the_split() -> None:
    """R13 as an import graph, not as a comment.

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
        assert forbidden not in imported, f"models/{module}.py imports {forbidden} — R13 has leaked"
