"""``validate_manifest`` — the checks Pydantic cannot do locally, and the advisory notes.

Focus here: the low-confidence chemistry warning (#55). A winning chemistry composes identically
whether its score is 0.95 or 0.44; nothing upstream gates on an *absolute* floor. The warning is
non-blocking (exit 0) and rung-aware — an onlist-backed winner (rung 3) is trusted at a lower score
than a geometry-only one (rung 2).
"""

from __future__ import annotations

import pytest

from seqforge import models as m
from seqforge.manifest.validate import (
    _CHEM_CONF_FLOOR_GEOMETRY,
    _CHEM_CONF_FLOOR_ONLIST,
    exit_code_for_report,
    validate_manifest,
)

HEX64 = "a" * 64


def _manifest(*, confidence: float | None, rung: int) -> m.DatasetManifest:
    """A minimal, structurally-valid single-cell manifest with a chosen chemistry confidence/rung.

    Everything but ``confidence``/``rung`` is fixed and clean, so the ONLY thing a validate can find
    is (or is not) the low-confidence note.
    """
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
        chemistry=m.EvidencedChemistrySet(
            value=["10x-3p-gex-v3"],
            basis="observed",
            confidence=confidence,
            rung=rung,
        ),
        assay=[m.AssayLabel(chemistry="10x-3p-gex-v3", curie="EFO:0009922", name="10x 3' v3")],
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
                sha256="b" * 64,
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
                sample_id="s1",
                file_uris=["reads/SRR000_1.fastq.gz", "reads/SRR000_2.fastq.gz"],
            )
        ],
    )
    return m.DatasetManifest(
        library=library,
        experiment=experiment,
        provenance=m.DatasetProvenance(
            dataset_hash=HEX64, kb_version="0.1", seqforge_version="2026.7.0"
        ),
    )


def _low_conf_warnings(report: m.ValidationReport) -> list[m.ValidationWarning]:
    return [w for w in report.warnings if w.code == "LOW_CONFIDENCE_CHEMISTRY"]


def test_a_high_confidence_winner_raises_no_low_confidence_note() -> None:
    report = validate_manifest(_manifest(confidence=0.98, rung=3))
    assert _low_conf_warnings(report) == []
    assert report.ok is True
    assert exit_code_for_report(report) == 0


def test_a_geometry_only_lonely_low_winner_is_flagged_but_still_compiles() -> None:
    # 0.44 is the compile audit's PRJNA658829 parental-sample class: geometry-only (rung 2), no onlist
    # hit. It must warn — and, crucially, must NOT block: the note rides along at exit 0.
    report = validate_manifest(_manifest(confidence=0.44, rung=2))
    notes = _low_conf_warnings(report)
    assert len(notes) == 1
    assert "0.44" in notes[0].message
    assert notes[0].subject.ref == "library.chemistry"
    assert report.ok is True  # a warning never makes a manifest non-compilable
    assert exit_code_for_report(report) == 0


def test_the_floor_is_rung_aware_an_onlist_winner_is_trusted_lower() -> None:
    # A score that trips the geometry (rung 2) floor but clears the onlist (rung 3) floor: the same
    # 0.60 warns without an onlist and does not warn with one, because an onlist positively
    # participating is stronger evidence than bare geometry at the same number.
    assert _CHEM_CONF_FLOOR_ONLIST < 0.60 < _CHEM_CONF_FLOOR_GEOMETRY
    assert _low_conf_warnings(validate_manifest(_manifest(confidence=0.60, rung=2)))
    assert not _low_conf_warnings(validate_manifest(_manifest(confidence=0.60, rung=3)))


def test_a_null_confidence_never_warns() -> None:
    # `confidence=None` is a legal "no judgement was weighed" value; there is nothing to floor.
    report = validate_manifest(_manifest(confidence=None, rung=2))
    assert _low_conf_warnings(report) == []
    assert report.ok is True


@pytest.mark.parametrize("rung", [2, 3])
def test_a_clean_certain_winner_never_warns_at_either_rung(rung: int) -> None:
    assert not _low_conf_warnings(validate_manifest(_manifest(confidence=1.0, rung=rung)))
