"""Tests for organism name -> NCBI taxid.

The interesting assertions are the refusals. A lookup that returns the *wrong* taxid is the failure
this module exists to prevent, and it is invisible downstream: a worm dataset aligned to a different
worm maps at a rate that merely looks mediocre. So every test here is about what it declines to do.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from email.message import Message

import pytest

from seqforge.io import taxonomy
from seqforge.io.taxonomy import Taxon, TaxonomyUnavailable, resolve, seed_names

#: A canned NCBI efetch (taxonomy) payload for taxid 6239, in the shape `fetch_taxon` parses: the
#: scientific name and rank, plus the <Synonym>/<GenbankCommonName>/<CommonName> names the round trip's
#: `answers_to` relies on. Committed so these facts are checked offline and deterministically — the two
#: tests below used to make the partition's only two LIVE NCBI calls, and because they were unmarked and
#: `_net` turned an unreachable NCBI into a green skip, CI never actually guaranteed them (#111).
_EFETCH_6239_XML = (
    "<TaxaSet><Taxon>"
    "<TaxId>6239</TaxId>"
    "<ScientificName>Caenorhabditis elegans</ScientificName>"
    "<Rank>species</Rank>"
    "<OtherNames>"
    "<GenbankCommonName>roundworm</GenbankCommonName>"
    "<Synonym>Rhabditis elegans</Synonym>"
    "<CommonName>nematode</CommonName>"
    "</OtherNames>"
    "</Taxon></TaxaSet>"
)


def test_the_seed_resolves_offline() -> None:
    """The common path costs no network: the pilot's organism is in the table."""
    assert resolve("Caenorhabditis elegans", offline=True) == 6239
    assert resolve("  caenorhabditis   ELEGANS ", offline=True) == 6239, "case/space are key noise"


def test_api_key_is_appended_only_for_eutils_and_only_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eutils_url = f"{taxonomy._EUTILS}/efetch.fcgi?db=taxonomy&id=6239"
    monkeypatch.setenv("NCBI_API_KEY", "K")
    assert "api_key=K" in taxonomy._with_api_key(eutils_url)
    assert taxonomy._with_api_key("https://example.com/x") == "https://example.com/x"  # not eutils
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    assert "api_key" not in taxonomy._with_api_key(eutils_url)


def test_taxonomy_get_retries_a_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same #9 backoff as `remote._get`, but over urllib (a non-2xx arrives as an HTTPError)."""
    calls = {"n": 0}

    def fake_urlopen(url: str, timeout: object = None) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(url, 429, "rate limited", Message(), None)
        return io.BytesIO(b"OK")  # BytesIO is its own context manager

    # `taxonomy` resolves `urllib.request.urlopen` and `time.sleep` through the modules themselves,
    # so patching them here is patching exactly what it calls.
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    assert taxonomy._get(f"{taxonomy._EUTILS}/efetch.fcgi?db=taxonomy&id=6239", timeout=1.0) == "OK"
    assert calls["n"] == 2


def test_taxonomy_get_does_not_retry_a_terminal_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 400 is terminal: it surfaces immediately, and -- unlike a 429 -- is NOT retried.

    The call-count assertion is what makes this test earn its keep next to the 429 sibling (#110): the
    name promised "does not retry" and nothing checked it, so it passed under retries too. `calls == 1`
    now guards the no-retry-on-terminal behaviour directly; no other test does.
    """
    calls = {"n": 0}

    def fake_urlopen(url: str, timeout: object = None) -> object:
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 400, "bad request", Message(), None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    with pytest.raises(urllib.error.HTTPError):
        taxonomy._get(f"{taxonomy._EUTILS}/efetch.fcgi?db=taxonomy&id=6239", timeout=1.0)
    assert calls["n"] == 1


def test_fetch_taxon_wraps_a_terminal_http_error_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient NCBI efetch failure is unreachability, not a false verdict.

    The round-trip verify calls `fetch_taxon`; a terminal HTTPError there (NCBI answering 400/5xx on
    an otherwise valid efetch) must surface as `TaxonomyUnavailable("... failed ...")` so an
    unreachable NCBI reads as unavailability, not a raw urllib error that reddens CI. Regression guard
    for CI #148.
    """

    def fake_urlopen(url: str, timeout: object = None) -> object:
        raise urllib.error.HTTPError(url, 400, "bad request", Message(), None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    with pytest.raises(TaxonomyUnavailable, match="failed"):
        taxonomy.fetch_taxon(6239, timeout=1.0)


def test_an_unseeded_name_refuses_offline_rather_than_guessing() -> None:
    with pytest.raises(TaxonomyUnavailable, match="--organism <taxid>"):
        resolve("Nematostella vectensis", offline=True)


def test_a_name_ncbi_does_not_know_is_a_refusal_not_a_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Everyone's default is human. On a worm dataset a silent default maps at near-zero, so a
    name NCBI returns no id for is a refusal, never a fallback. Driven offline with a canned empty
    esearch — formerly a live call `_net` would turn into a green skip, so CI never guaranteed it.
    """

    def fake_get(url: str, *, timeout: float) -> str:
        assert "esearch" in url, "no efetch should be issued when esearch returns no id"
        return json.dumps({"esearchresult": {"idlist": []}})

    monkeypatch.setattr(taxonomy, "_get", fake_get)
    with pytest.raises(TaxonomyUnavailable, match="no match"):
        resolve("Homo sapiense flurbus")


def test_the_round_trip_accepts_a_synonym(monkeypatch: pytest.MonkeyPatch) -> None:
    """`answers_to` compares against NCBI's synonyms, not just the scientific name.

    A naive equality check would reject `Rhabditis elegans` -- a real historical name for C. elegans
    that a paper may well use -- and that false refusal is how a verifier gets switched off. Driven
    offline with canned esearch + efetch payloads (formerly a live call `_net` would silently skip).
    """

    def fake_get(url: str, *, timeout: float) -> str:
        if "esearch" in url:
            return json.dumps({"esearchresult": {"idlist": ["6239"]}})
        return _EFETCH_6239_XML  # efetch, for the round-trip verify

    monkeypatch.setattr(taxonomy, "_get", fake_get)
    assert resolve("Rhabditis elegans") == 6239


def test_fetch_taxon_parses_scientific_name_synonyms_and_common_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fetch_taxon` reads the scientific name, its <Synonym>s and <Genbank>/<CommonName>s off the
    efetch XML — the parse `answers_to` depends on, which nothing exercised offline until now (#111)."""
    monkeypatch.setattr(taxonomy, "_get", lambda url, *, timeout: _EFETCH_6239_XML)
    taxon = taxonomy.fetch_taxon(6239, timeout=1.0)
    assert taxon.taxid == 6239
    assert taxon.scientific_name == "Caenorhabditis elegans"
    assert taxon.rank == "species"
    assert set(taxon.names) == {"roundworm", "Rhabditis elegans", "nematode"}
    assert taxon.answers_to("Rhabditis elegans")  # a <Synonym> answers
    assert taxon.answers_to("NEMATODE")  # a <CommonName>, case-insensitively


def test_the_round_trip_rejects_a_taxid_that_does_not_answer_to_the_name() -> None:
    """The verifier's whole job, exercised without the network by lying to it directly."""
    briggsae = Taxon(taxid=6238, scientific_name="Caenorhabditis briggsae", rank="species")
    assert not briggsae.answers_to("Caenorhabditis elegans")
    assert briggsae.answers_to("caenorhabditis  BRIGGSAE")


# `test_the_seed_table_agrees_with_ncbi` was deleted (#110): it claimed "every entry re-resolved live
# and round-trip verified", but `resolve()` returns `_SEED[key]` before the network and before verify,
# so with a raiser swapped in for urlopen it still passed -- it asserted `_SEED[k] == _SEED[k]`, the
# exact self-validating table its own docstring lectured against. The seed values that ARE pinned
# against literals stay pinned: `test_the_seed_resolves_offline` (6239) and the ranks test below
# (4932 / 559292). The archive-fixture note near the top of test_records.py records that neither the
# seed table nor the archive XMLs has a live freshness guard -- a known gap, not an oversight.


def test_seed_ranks_are_deliberate_not_accidental() -> None:
    """`Saccharomyces cerevisiae` is the SPECIES (4932); S288C is the STRAIN (559292).

    Both are correct answers to different questions, and NCBI's search returns the species. The table
    carries both because the sacCer3 fixtures use the strain -- and neither is silently promoted to
    the other, because quietly changing a caller's rank is how a wrong reference reaches a corpus.
    """
    seed = seed_names()
    assert seed["saccharomyces cerevisiae"] == 4932
    assert seed["saccharomyces cerevisiae s288c"] == 559292
