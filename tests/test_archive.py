"""The archive transcriber's network seam: the ``labdata`` accession hop, then ``efetch`` + parse.

``_experiments_for`` no longer routes through ENA/GEO-SOFT — it delegates the accession -> SRA
experiments hop to :func:`labdata.experiments_for`, whose Entrez ``elink`` route reaches a GEO
SuperSeries our own SOFT recursion could not. These tests mock that hop (seqforge never hits the
network in a test) and the ``efetch`` calls, then drive the *real* parse/merge path on the committed
pilot XML — the same fixtures :mod:`test_records` uses — so the composition is exercised end to end
without a byte of network.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from seqforge.io import archive
from seqforge.io.remote import RemoteError

FIXTURES = Path(__file__).parent / "fixtures" / "archive"


class _FakeExperiment:
    """The shape ``_experiments_for`` reads off a ``labdata`` Experiment: just ``.accession``."""

    def __init__(self, accession: str) -> None:
        self.accession = accession


def _patch_labdata(
    monkeypatch: pytest.MonkeyPatch, resolver: Callable[[str], list[_FakeExperiment]]
) -> None:
    """Install ``resolver`` as ``labdata.experiments_for`` (absent in the pinned build, so no raise)."""
    import labdata

    monkeypatch.setattr(labdata, "experiments_for", resolver, raising=False)


# ------------------------------------------------------------ the labdata accession hop


def test_experiments_for_returns_labdatas_experiment_accessions_sorted_and_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def resolver(accession: str) -> list[_FakeExperiment]:
        seen.append(accession)  # record it to prove the accession passes through unchanged
        return [_FakeExperiment(a) for a in ("SRX2", "SRX1", "SRX2")]

    _patch_labdata(monkeypatch, resolver)
    assert archive._experiments_for("GSE229022") == ["SRX1", "SRX2"]
    assert seen == [
        "GSE229022"
    ]  # reaches labdata untransformed (folded from the passthrough sibling)


# `test_experiments_for_translates_a_labdata_error_into_a_remote_error` was deleted (#110): it raised
# `AccessionError`, a subclass of `LabdataError`, which `_experiments_for` catches on one `except
# LabdataError`. `test_experiments_for_does_not_retry_a_terminal_labdata_error` below raises the base
# class onto that same except -- same RemoteError message -- and additionally pins `calls["n"] == 1`,
# so it reddens wherever this one would, and for the narrower catch too (base escapes, subclass would not).


def test_experiments_for_refuses_loudly_when_the_accession_resolves_to_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_labdata(monkeypatch, lambda acc: [])
    # An accession that was GIVEN and yields no experiments is a refusal, not a silent omission from
    # a permanent, content-addressed manifest.
    with pytest.raises(RemoteError, match="no experiments found"):
        archive._experiments_for("GSE229022")


def test_experiments_for_retries_a_transient_labdata_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A momentary NCBI 5xx at the accession hop must not abort `records` (#9): GSE274290 hit a bare
    HTTP 500 live. A transient labdata error backs off and retries, exactly as `_get` does."""
    from labdata.exceptions import LabdataError

    calls = {"n": 0}

    def resolver(accession: str) -> list[_FakeExperiment]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise LabdataError(
                "NCBI E-utilities request failed: HTTP Error 500: Internal Server Error"
            )
        return [_FakeExperiment("SRX1")]

    _patch_labdata(monkeypatch, resolver)
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # `archive` calls `time.sleep` directly
    assert archive._experiments_for("GSE274290") == ["SRX1"]
    assert calls["n"] == 2  # retried through the 500


def test_experiments_for_does_not_retry_a_terminal_labdata_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed accession is terminal — no backoff, one attempt, a loud RemoteError."""
    from labdata.exceptions import LabdataError

    calls = {"n": 0}

    def resolver(accession: str) -> list[_FakeExperiment]:
        calls["n"] += 1
        raise LabdataError("banana is not a resolvable accession")

    _patch_labdata(monkeypatch, resolver)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    with pytest.raises(RemoteError, match="could not resolve experiments"):
        archive._experiments_for("banana")
    assert calls["n"] == 1


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


# ------------------------------------------------------------ a study bigger than one efetch batch

_STUDY = (
    '<STUDY center_name="BioProject" alias="PRJNA9999999" accession="SRP999999">'
    "<IDENTIFIERS><PRIMARY_ID>SRP999999</PRIMARY_ID>"
    '<EXTERNAL_ID namespace="BioProject">PRJNA9999999</EXTERNAL_ID></IDENTIFIERS>'
    "<DESCRIPTOR><STUDY_TITLE>A study of more runs than fit in one request</STUDY_TITLE>"
    "</DESCRIPTOR></STUDY>"
)


def _package_set(pairs: Sequence[tuple[str, str]]) -> str:
    """``efetch db=sra`` XML for (experiment, biosample) pairs, every package restating the STUDY.

    Restating it is the archive's own shape — a package is the whole chain above one experiment — and
    it is why a fetch that pages sees the same study once per page.
    """
    packages = "".join(
        f'<EXPERIMENT_PACKAGE><EXPERIMENT accession="{exp}"><TITLE>{exp}</TITLE></EXPERIMENT>'
        f"{_STUDY}"
        f'<SAMPLE accession="SRS{exp[3:]}" alias="{sample} nuclei"><IDENTIFIERS>'
        f'<EXTERNAL_ID namespace="BioSample">{sample}</EXTERNAL_ID></IDENTIFIERS>'
        "<SAMPLE_NAME><TAXON_ID>6239</TAXON_ID></SAMPLE_NAME></SAMPLE>"
        f'<RUN_SET><RUN accession="SRR{exp[3:]}"/></RUN_SET></EXPERIMENT_PACKAGE>'
        for exp, sample in pairs
    )
    return f"<EXPERIMENT_PACKAGE_SET>{packages}</EXPERIMENT_PACKAGE_SET>"


def _paged_archive(
    monkeypatch: pytest.MonkeyPatch, pairs: Sequence[tuple[str, str]]
) -> list[tuple[str, list[str]]]:
    """Serve ``pairs`` as an archive that answers exactly the ids each request asks for.

    Returns the call log, so a test can see how many pages the fetch actually took. The biosample and
    bioproject replies are empty on purpose: they only ever *add* attributes, and this is about how
    many records come back.
    """
    _patch_labdata(monkeypatch, lambda acc: [_FakeExperiment(exp) for exp, _ in pairs])
    calls: list[tuple[str, list[str]]] = []

    def fake_efetch(db: str, ids: list[str], **params: str) -> str:
        calls.append((db, list(ids)))
        if db != "sra":
            return "<RecordSet/>"
        asked = set(ids)
        return _package_set([p for p in pairs if p[0] in asked])

    monkeypatch.setattr(archive, "_efetch", fake_efetch)
    return calls


def test_a_study_too_large_for_one_efetch_holds_exactly_one_project_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every page restates the study, so deduplicating within a page leaves one project record per
    page (#239). SRP383998's 1440 runs came back with the project 15 times, which is exactly its page
    count — a defect no study small enough for a single request can show.
    """
    pairs = [(f"SRX{i:07d}", f"SAMN{i:07d}") for i in range(2 * archive._BATCH + 50)]
    calls = _paged_archive(monkeypatch, pairs)

    record_set = archive.fetch_records("PRJNA9999999")

    assert [db for db, _ in calls].count("sra") == 3, "the fixture must span more than one page"
    assert [r.accession for r in record_set.at("project")] == ["PRJNA9999999"]
    seen = {(r.level, r.accession) for r in record_set.records}
    assert len(seen) == len(record_set.records)  # and no level duplicates across pages either
    assert len(record_set.at("experiment")) == len(record_set.at("run")) == len(pairs)


def test_a_sample_whose_experiments_straddle_a_page_boundary_is_one_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sample repeats in every page holding one of its experiments, and pages split on experiment
    count, so a sample with several experiments lands in two of them. The duplicate reaches the
    biosample request too, where it spends an id slot on an attribute set already asked for.
    """
    per_sample = 60  # so SAMN0000001's experiments (60-119) fall either side of the 100 boundary
    pairs = [(f"SRX{i:07d}", f"SAMN{i // per_sample:07d}") for i in range(2 * archive._BATCH + 50)]
    calls = _paged_archive(monkeypatch, pairs)

    record_set = archive.fetch_records("PRJNA9999999")

    assert [r.accession for r in record_set.at("sample")] == [f"SAMN{i:07d}" for i in range(5)]
    asked = [ids for db, ids in calls if db == "biosample"]
    assert asked and all(len(ids) == len(set(ids)) for ids in asked)


def test_efetch_adds_the_ncbi_api_key_only_when_the_environment_sets_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """eutils raises its 3->10 req/sec cap for a keyed caller (#9). The key is read from the
    environment and added to the request params, and it never appears when unset."""
    captured: dict[str, dict[str, str] | None] = {}

    def fake_get(url: str, params: dict[str, str] | None = None, timeout: int = 30) -> str:
        captured["params"] = params
        return "<EXPERIMENT_PACKAGE_SET/>"

    monkeypatch.setattr(archive, "_get", fake_get)

    monkeypatch.setenv("NCBI_API_KEY", "SECRET-KEY-123")
    archive._efetch("sra", ["SRX1"])
    keyed = captured["params"]
    assert keyed is not None, "_efetch reached _get with no params at all"
    assert keyed["api_key"] == "SECRET-KEY-123"

    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    archive._efetch("sra", ["SRX1"])
    unkeyed = captured["params"]
    assert unkeyed is not None, "_efetch reached _get with no params at all"
    assert "api_key" not in unkeyed
