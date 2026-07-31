"""Tests for organism name -> NCBI taxid.

The interesting assertions are the refusals. A lookup that returns the *wrong* taxid is the failure
this module exists to prevent, and it is invisible downstream: a worm dataset aligned to a different
worm maps at a rate that merely looks mediocre. So every test here is about what it declines to do.
"""

from __future__ import annotations

import io
import urllib.error

import pytest

from seqforge.io import taxonomy
from seqforge.io.taxonomy import Taxon, TaxonomyUnavailable, resolve, seed_names


def _net(fn, *a, **k):
    """Run a test that needs NCBI, or skip. A skip is green, so nothing here may be ONLY networked."""
    try:
        return fn(*a, **k)
    except TaxonomyUnavailable as exc:  # pragma: no cover - host dependent
        if "failed" in str(exc):
            pytest.skip(f"NCBI unreachable: {exc}")
        raise


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
            raise urllib.error.HTTPError(url, 429, "rate limited", {}, None)  # type: ignore[arg-type]
        return io.BytesIO(b"OK")  # BytesIO is its own context manager

    monkeypatch.setattr(taxonomy.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(taxonomy.time, "sleep", lambda _s: None)
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
        raise urllib.error.HTTPError(url, 400, "bad request", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(taxonomy.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(taxonomy.time, "sleep", lambda _s: None)
    with pytest.raises(urllib.error.HTTPError):
        taxonomy._get(f"{taxonomy._EUTILS}/efetch.fcgi?db=taxonomy&id=6239", timeout=1.0)
    assert calls["n"] == 1


def test_fetch_taxon_wraps_a_terminal_http_error_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient NCBI efetch failure is unreachability, not a false verdict.

    The round-trip verify calls `fetch_taxon`; a terminal HTTPError there (NCBI answering 400/5xx on
    an otherwise valid efetch) must surface as `TaxonomyUnavailable("... failed ...")` so `_net`
    treats it as a skip, not a raw urllib error that reddens CI. Regression guard for CI #148.
    """

    def fake_urlopen(url: str, timeout: object = None) -> object:
        raise urllib.error.HTTPError(url, 400, "bad request", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(taxonomy.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(taxonomy.time, "sleep", lambda _s: None)
    with pytest.raises(TaxonomyUnavailable, match="failed"):
        taxonomy.fetch_taxon(6239, timeout=1.0)


def test_an_unseeded_name_refuses_offline_rather_than_guessing() -> None:
    with pytest.raises(TaxonomyUnavailable, match="--organism <taxid>"):
        resolve("Nematostella vectensis", offline=True)


def test_a_name_ncbi_does_not_know_is_a_refusal_not_a_default() -> None:
    """Everyone's default is human. On a worm dataset a silent default maps at near-zero (§12)."""
    with pytest.raises(TaxonomyUnavailable):
        _net(resolve, "Homo sapiense flurbus")


def test_the_round_trip_accepts_a_synonym() -> None:
    """`answers_to` compares against NCBI's synonyms, not just the scientific name.

    A naive equality check would reject `Rhabditis elegans` -- a real historical name for C. elegans
    that a paper may well use -- and that false refusal is how a verifier gets switched off.
    """
    assert _net(resolve, "Rhabditis elegans") == 6239


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
