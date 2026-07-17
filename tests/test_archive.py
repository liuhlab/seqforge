"""The archive transcriber's network seam: the ``labdata`` accession hop, then ``efetch`` + parse.

``_experiments_for`` no longer routes through ENA/GEO-SOFT — it delegates the accession -> SRA
experiments hop to :func:`labdata.experiments_for`, whose Entrez ``elink`` route reaches a GEO
SuperSeries our own SOFT recursion could not. These tests mock that hop (seqforge never hits the
network in a test) and the ``efetch`` calls, then drive the *real* parse/merge path on the committed
pilot XML — the same fixtures :mod:`test_records` uses — so the composition is exercised end to end
without a byte of network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from seqforge.io import archive
from seqforge.io.remote import RemoteError

FIXTURES = Path(__file__).parent / "fixtures" / "archive"


class _FakeExperiment:
    """The shape ``_experiments_for`` reads off a ``labdata`` Experiment: just ``.accession``."""

    def __init__(self, accession: str) -> None:
        self.accession = accession


def _patch_labdata(monkeypatch: pytest.MonkeyPatch, resolver) -> None:
    """Install ``resolver`` as ``labdata.experiments_for`` (absent in the pinned build, so no raise)."""
    import labdata

    monkeypatch.setattr(labdata, "experiments_for", resolver, raising=False)


# ------------------------------------------------------------ the labdata accession hop


def test_experiments_for_returns_labdatas_experiment_accessions_sorted_and_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_labdata(
        monkeypatch,
        lambda acc: [_FakeExperiment(a) for a in ("SRX2", "SRX1", "SRX2")],
    )
    assert archive._experiments_for("GSE229022") == ["SRX1", "SRX2"]


def test_experiments_for_passes_the_accession_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def resolver(accession: str) -> list[_FakeExperiment]:
        seen.append(accession)
        return [_FakeExperiment("SRX1")]

    _patch_labdata(monkeypatch, resolver)
    archive._experiments_for("GSE229022")
    assert seen == ["GSE229022"]


def test_experiments_for_translates_a_labdata_error_into_a_remote_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from labdata.exceptions import AccessionError

    def resolver(accession: str) -> list[_FakeExperiment]:
        raise AccessionError("not a resolvable accession")

    _patch_labdata(monkeypatch, resolver)
    # A malformed accession must surface as the archive layer's own error type, which the CLI catches
    # for a clean exit — not as a raw labdata exception that escapes to a traceback.
    with pytest.raises(RemoteError, match="could not resolve experiments"):
        archive._experiments_for("banana")


def test_experiments_for_refuses_loudly_when_the_accession_resolves_to_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_labdata(monkeypatch, lambda acc: [])
    # An accession that was GIVEN and yields no experiments is a refusal, not a silent omission from
    # a permanent, content-addressed manifest.
    with pytest.raises(RemoteError, match="no experiments found"):
        archive._experiments_for("GSE229022")


# ------------------------------------------------------------ the whole fetch, composed


def test_fetch_records_composes_labdatas_hop_with_the_efetch_parse_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """labdata resolves the experiments; the committed pilot XML drives the rest, unchanged."""
    _patch_labdata(monkeypatch, lambda acc: [_FakeExperiment("SRX24283130")])

    fixtures = {
        "sra": (FIXTURES / "PRJNA1027859.sra.xml").read_text(),
        "biosample": (FIXTURES / "PRJNA1027859.biosample.xml").read_text(),
        "bioproject": (FIXTURES / "PRJNA1027859.bioproject.xml").read_text(),
    }

    def fake_efetch(db: str, ids: list[str], **params: str) -> str:
        return fixtures[db]

    monkeypatch.setattr(archive, "_efetch", fake_efetch)

    record_set = archive.fetch_records("PRJNA1027859")

    assert record_set.query == "PRJNA1027859"
    # All four levels come through the same parse the pilot exercised.
    assert record_set.at("project")
    assert record_set.at("experiment")
    runs = record_set.at("run")
    assert runs
    # And the BioSample harmonization still lands: the pilot's two strains reach the records under
    # NCBI's own harmonized `strain` key (this is exactly what the merge step exists to do).
    samples = record_set.at("sample")
    assert samples
    strains = {
        attr.value for sample in samples for attr in sample.attributes if attr.name == "strain"
    }
    assert {"CQ757", "CQ758"} <= strains
