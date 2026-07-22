"""Tests for the network surface — the PARSERS, offline.

The HTTP calls need the network and are skip-gated; the parsers do not, and the parsers are where
bugs actually live. Two of these endpoints (`run_new`, GEO SOFT) are **undocumented**, so their
shape can change without notice — pinning real captured payloads here means a change surfaces as a
red test rather than as a silently empty result.

The fixtures below are trimmed from genuine responses (SRR9170959 is the real dropped-technical-read
case: SRA says 3 reads / 110 bases per spot, ENA published 50).
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import types
import warnings
import zlib

import pytest

from seqforge.io import remote
from seqforge.io.remote import (
    RemoteError,
    RunStatistics,
    _content_range_total,
    _uri_basename,
    classify_accession,
    decompress_prefix,
    dropped_reads,
    fastq_targets,
    fastq_urls,
    parse_fastq_prefix,
    parse_filereport,
    parse_run_new,
    parse_soft_srp,
    parse_soft_superseries,
    probe_remote,
    retry_delay,
    technical_read_remedy,
)
from seqforge.probe import content_key_from_md5


def _resp(status: int, text: str = "", retry_after: str | None = None) -> types.SimpleNamespace:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return types.SimpleNamespace(status_code=status, text=text, headers=headers)


def test_get_retries_a_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single 429 used to abort the whole `records` stage (#9). It now backs off and retries."""
    seq = [_resp(429, "rate limited", retry_after="0"), _resp(200, "OK")]
    calls = {"n": 0}

    def fake_get(url: str, params: object = None, timeout: object = None) -> object:
        i = calls["n"]
        calls["n"] += 1
        return seq[i]

    monkeypatch.setattr(remote.requests, "get", fake_get)
    monkeypatch.setattr(remote.time, "sleep", lambda _s: None)  # no real wait in the test
    assert remote._get("https://eutils.example/efetch") == "OK"
    assert calls["n"] == 2  # first 429, then the 200


def test_get_gives_up_after_the_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_get(url: str, params: object = None, timeout: object = None) -> object:
        calls["n"] += 1
        return _resp(503, "service unavailable")

    monkeypatch.setattr(remote.requests, "get", fake_get)
    monkeypatch.setattr(remote.time, "sleep", lambda _s: None)
    with pytest.raises(RemoteError, match="HTTP 503"):
        remote._get("https://eutils.example/efetch")
    assert calls["n"] == remote._MAX_RETRIES + 1  # tried, then exhausted


def test_get_does_not_retry_a_terminal_status(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_get(url: str, params: object = None, timeout: object = None) -> object:
        calls["n"] += 1
        return _resp(404, "not found")

    monkeypatch.setattr(remote.requests, "get", fake_get)
    monkeypatch.setattr(remote.time, "sleep", lambda _s: None)
    with pytest.raises(RemoteError, match="HTTP 404"):
        remote._get("https://eutils.example/efetch")
    assert calls["n"] == 1  # a 404 is terminal, not retried


def test_get_retries_a_dropped_connection_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reset connection is the transport-level twin of a 5xx — NCBI resets under load (aborted
    GSE310667's records fetch live). It backs off and retries rather than aborting the stage."""
    calls = {"n": 0}

    def fake_get(url: str, params: object = None, timeout: object = None) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise remote.requests.ConnectionError(
                "('Connection aborted.', ConnectionResetError(104))"
            )
        return _resp(200, "OK")

    monkeypatch.setattr(remote.requests, "get", fake_get)
    monkeypatch.setattr(remote.time, "sleep", lambda _s: None)
    assert remote._get("https://eutils.example/efetch") == "OK"
    assert calls["n"] == 2


def test_get_gives_up_on_a_persistent_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_get(url: str, params: object = None, timeout: object = None) -> object:
        calls["n"] += 1
        raise remote.requests.Timeout("read timed out")

    monkeypatch.setattr(remote.requests, "get", fake_get)
    monkeypatch.setattr(remote.time, "sleep", lambda _s: None)
    with pytest.raises(RemoteError, match="failed"):
        remote._get("https://eutils.example/efetch")
    assert calls["n"] == remote._MAX_RETRIES + 1  # retried to the budget, then raised


def test_retry_delay_honors_an_integer_retry_after_else_backs_off() -> None:
    assert retry_delay("2", 0) == 2.0  # server-specified wait wins
    assert retry_delay(None, 0) == 1.0  # base
    assert retry_delay(None, 2) == 4.0  # exponential: 1 * 2**2
    assert retry_delay(None, 99) == 16.0  # capped
    assert retry_delay("not-a-number", 0) == 1.0  # a date-form Retry-After falls back to backoff


# ---------------------------------------------------------------------------------------------
# accession classification
# ---------------------------------------------------------------------------------------------


def test_classify_every_namespace() -> None:
    cases = {
        "GSE110823": "geo_series",
        "GSM3017260": "geo_sample",
        "PRJNA1027859": "bioproject",
        "PRJEB12345": "bioproject",
        "SRP502277": "study",
        "ERP123456": "study",
        "SRX24283133": "experiment",
        "SRR28716553": "run",
        "ERR1234567": "run",
        "SAMN40935616": "biosample",
        "SAMEA1234567": "biosample",
    }
    for acc, kind in cases.items():
        assert classify_accession(acc) == kind, acc


def test_classify_refuses_to_guess() -> None:
    """`unknown` is a first-class answer. Guessing a namespace would send the query somewhere wrong."""
    for acc in ("", "hello", "GSE", "SRR", "NM_001301717", "ENSG00000141510"):
        assert classify_accession(acc) == "unknown", acc


# ---------------------------------------------------------------------------------------------
# GEO SOFT -> SRP, including the SuperSeries trap
# ---------------------------------------------------------------------------------------------

_SOFT_WITH_SRP = """\
^SERIES = GSE164073
!Series_title = Some study
!Series_relation = BioProject: https://www.ncbi.nlm.nih.gov/bioproject/PRJNA692883
!Series_relation = SRA: https://www.ncbi.nlm.nih.gov/sra?term=SRP299835
"""

_SOFT_SUPERSERIES = """\
^SERIES = GSE140511
!Series_title = A SuperSeries
!Series_relation = SuperSeries of: GSE140399
!Series_relation = SuperSeries of: GSE140510
"""


def test_parse_soft_finds_the_srp() -> None:
    """Exact match, which also proves the BioProject is not read as the SRA study: both arrive as
    `!Series_relation` and only the SRA one carries `term=SRP...`, so `== ["SRP299835"]` (no stray
    PRJNA692883) subsumes a separate not-confused check."""
    assert parse_soft_srp(_SOFT_WITH_SRP) == ["SRP299835"]


def test_parse_soft_superseries_is_detected() -> None:
    """A SuperSeries owns no runs: eutils and runinfo both return ZERO, silently.

    Without this, a resolver reports success and loses the entire dataset — the worst kind of wrong.
    """
    assert parse_soft_superseries(_SOFT_SUPERSERIES) == ["GSE140399", "GSE140510"]
    assert parse_soft_srp(_SOFT_SUPERSERIES) == [], "a SuperSeries declares no SRP of its own"


# ---------------------------------------------------------------------------------------------
# ENA filereport
# ---------------------------------------------------------------------------------------------

_TSV = (
    "run_accession\tread_count\tbase_count\tfastq_ftp\tlibrary_layout\n"
    "SRR9170959\t79615125\t3980756250\tftp.sra.ebi.ac.uk/vol1/fastq/SRR917/009/SRR9170959/SRR9170959.fastq.gz\tPAIRED\n"
)


def test_parse_filereport_reads_the_tsv() -> None:
    rows = parse_filereport(_TSV)
    assert len(rows) == 1
    assert rows[0]["run_accession"] == "SRR9170959"
    assert rows[0]["read_count"] == "79615125"


def test_parse_filereport_treats_header_only_as_empty_not_an_error() -> None:
    assert parse_filereport("run_accession\tread_count\n") == []
    assert parse_filereport("") == []


def test_fastq_urls_splits_and_adds_the_scheme() -> None:
    run = {"fastq_ftp": "ftp.x/a_1.fastq.gz;ftp.x/a_2.fastq.gz"}
    assert fastq_urls(run) == ["https://ftp.x/a_1.fastq.gz", "https://ftp.x/a_2.fastq.gz"]


def test_fastq_urls_are_sorted_because_ena_does_not_guarantee_order() -> None:
    run = {"fastq_ftp": "ftp.x/a_2.fastq.gz;ftp.x/a_1.fastq.gz"}
    assert fastq_urls(run) == ["https://ftp.x/a_1.fastq.gz", "https://ftp.x/a_2.fastq.gz"]


def test_fastq_urls_empty_is_meaningful_not_a_crash() -> None:
    """ENA generates NO fastq for cellranger BAMs / BAMs with CB tags — i.e. exactly the 10x case."""
    assert fastq_urls({"fastq_ftp": ""}) == []
    assert fastq_urls({}) == []


# ---------------------------------------------------------------------------------------------
# run_new — the only place reads-per-spot is exposed
# ---------------------------------------------------------------------------------------------

_RUN_NEW_DROPPED = """<?xml version="1.0"?>
<RUN_LIST><RUN accession="SRR9170959">
  <Statistics nreads="3" nspots="79615125">
    <Read index="0" count="79615125" average="50" stdev="0"/>
    <Read index="1" count="79615125" average="50" stdev="0"/>
    <Read index="2" count="79615125" average="10" stdev="0"/>
  </Statistics>
  <RUN_ATTRIBUTES><RUN_ATTRIBUTE>
    <TAG>options</TAG>
    <VALUE>--readTypes=TBT --read1PairFiles=x.1.fastq.gz</VALUE>
  </RUN_ATTRIBUTE></RUN_ATTRIBUTES>
</RUN></RUN_LIST>
"""

_RUN_NEW_CLEAN = """<?xml version="1.0"?>
<RUN_LIST><RUN accession="SRR8526547">
  <Statistics nreads="2" nspots="100">
    <Read index="0" count="100" average="26" stdev="0"/>
    <Read index="1" count="100" average="98" stdev="0"/>
  </Statistics>
</RUN></RUN_LIST>
"""


def test_parse_run_new_reads_the_per_read_table() -> None:
    stats = parse_run_new(_RUN_NEW_DROPPED, "SRR9170959")
    assert stats.n_reads == 3
    assert [r.average_length for r in stats.reads] == [50, 50, 10]
    assert stats.spot_length == 110
    assert stats.read_types == "TBT"  # Technical / Biological / Technical


def test_parse_run_new_tolerates_a_missing_readtypes() -> None:
    """`readTypes` only appears for fastq-load.py submissions; absent is NORMAL, not an error."""
    stats = parse_run_new(_RUN_NEW_CLEAN, "SRR8526547")
    assert stats.n_reads == 2
    assert stats.spot_length == 124
    assert stats.read_types is None


def test_parse_run_new_rejects_garbage_loudly() -> None:
    with pytest.raises(RemoteError, match="unparsable"):
        parse_run_new("<not xml", "SRR1")


def test_parse_run_new_tolerates_missing_statistics() -> None:
    stats = parse_run_new("<RUN_LIST><RUN/></RUN_LIST>", "SRR1")
    assert stats.n_reads == 0 and stats.spot_length == 0


# ---------------------------------------------------------------------------------------------
# the dropped-technical-read detector (rung 0 — two metadata calls, zero bytes)
# ---------------------------------------------------------------------------------------------


def test_detects_a_dropped_technical_read() -> None:
    """The real SRR9170959 case: SRA says 110 bases/spot across 3 reads; ENA published 50.

    A dropped 10x barcode read leaves a dataset that looks like plain single-end RNA-seq and is
    silently unprocessable as single-cell. This costs two metadata calls and no bytes.
    """
    run = {"read_count": "79615125", "base_count": "3980756250", "fastq_ftp": "ftp.x/a.fastq.gz"}
    stats = parse_run_new(_RUN_NEW_DROPPED, "SRR9170959")
    d = dropped_reads(run, stats)
    assert d is not None
    assert d.sra_spot_length == 110
    assert d.ena_spot_length == 50.0
    assert d.missing_bases == 60.0
    assert d.n_reads_sra == 3
    assert d.n_files_ena == 1
    assert d.read_types == "TBT"


def test_no_false_accusation_when_the_archives_agree() -> None:
    """SRR8526547: 26+98=124 declared, 124 published. Nothing dropped — must NOT flag."""
    run = {
        "read_count": "100",
        "base_count": "12400",
        "fastq_ftp": "ftp.x/a_1.fq.gz;ftp.x/a_2.fq.gz",
    }
    stats = parse_run_new(_RUN_NEW_CLEAN, "SRR8526547")
    assert dropped_reads(run, stats) is None


def test_detector_abstains_rather_than_guessing() -> None:
    """Missing inputs => ABSTAIN. A detector that accuses on absent evidence gets switched off."""
    stats = parse_run_new(_RUN_NEW_CLEAN, "SRR1")
    assert dropped_reads({}, stats) is None
    assert dropped_reads({"read_count": "0", "base_count": "0"}, stats) is None
    assert (
        dropped_reads({"read_count": "100", "base_count": "12400"}, RunStatistics("SRR1")) is None
    )


def test_detector_absorbs_rounding_in_enas_averages() -> None:
    """ENA reports mean bases/spot; a sub-1-base gap is arithmetic, not a dropped read."""
    run = {"read_count": "100", "base_count": "12350"}  # 123.5 vs SRA's 124
    stats = parse_run_new(_RUN_NEW_CLEAN, "SRR8526547")
    assert dropped_reads(run, stats) is None


def test_remedy_names_fasterq_dump_first_not_sdl() -> None:
    """SDL is a fallback: originals exist for select studies only, so most runs dead-end there.

    The remedy must be operable (design §1.5) — naming the usually-empty path first is not.
    """
    remedy = technical_read_remedy("SRR9170959")
    assert "--include-technical" in remedy
    assert remedy.index("fasterq-dump") < remedy.index("Data Locator")
    assert "SRR9170959" in remedy


# ---------------------------------------------------------------------------------------------
# io peek — bounded gzip prefix decoding
# ---------------------------------------------------------------------------------------------


def _fastq_gz(n: int = 50, read_len: int = 90) -> bytes:
    body = "".join(f"@READ:{i}\n{'A' * read_len}\n+\n{'I' * read_len}\n" for i in range(n))
    return gzip.compress(body.encode())


def test_decompress_prefix_reads_a_whole_small_member() -> None:
    out = decompress_prefix(_fastq_gz(3), max_bytes=1 << 20)
    assert out.decode().count("@READ:") == 3


def test_decompress_prefix_tolerates_a_truncated_tail() -> None:
    """The core claim of `io peek`: a byte-range prefix inflates without raising.

    zlib simply returns fewer bytes and leaves eof False — so "handling truncation" is just stopping.
    """
    blob = _fastq_gz(500)
    out = decompress_prefix(blob[: len(blob) // 2], max_bytes=1 << 20)
    assert len(out) > 0
    assert b"@READ:0" in out


def test_decompress_prefix_enforces_a_decompressed_byte_budget() -> None:
    """The budget is on DECOMPRESSED bytes, not a compressed-byte proxy — also a zip-bomb guard."""
    out = decompress_prefix(_fastq_gz(5000), max_bytes=1000)
    assert len(out) <= 1000


def test_decompress_prefix_rejects_a_corrupt_member() -> None:
    with pytest.raises(RemoteError, match="not readable"):
        decompress_prefix(b"this is not gzip at all", max_bytes=1000)


def test_parse_fastq_prefix_drops_the_partial_trailing_record() -> None:
    """The range boundary cuts mid-record; a half-read must never be reported as a read length."""
    text = "@a\nACGT\n+\nIIII\n@b\nACG"  # 'b' is incomplete
    headers, lengths = parse_fastq_prefix(text, max_reads=10)
    assert headers == ["@a"]
    assert lengths == [4]


def test_parse_fastq_prefix_respects_max_reads() -> None:
    text = "".join(f"@r{i}\nACGT\n+\nIIII\n" for i in range(20))
    headers, _ = parse_fastq_prefix(text, max_reads=3)
    assert len(headers) == 3


def test_parse_fastq_prefix_on_empty_input() -> None:
    assert parse_fastq_prefix("", max_reads=4) == ([], [])


def test_peek_round_trips_a_real_gzip_prefix() -> None:
    """End-to-end over the pure path: bytes -> inflate -> records, exactly as `io peek` does."""
    blob = _fastq_gz(200, read_len=90)
    text = decompress_prefix(blob[:2048], max_bytes=1 << 20).decode("utf-8", errors="replace")
    headers, lengths = parse_fastq_prefix(text, max_reads=4)
    assert headers[0] == "@READ:0"
    assert set(lengths) == {90}


def test_zlib_wbits_31_is_the_gzip_incantation() -> None:
    """Pin the magic number: 31 = 16 (gzip wrapper) + 15 (window). 15 alone would fail on gzip."""
    blob = _fastq_gz(2)
    assert zlib.decompressobj(31).decompress(blob).startswith(b"@READ:0")
    with pytest.raises(zlib.error):
        zlib.decompressobj(15).decompress(blob)


# ---------------------------------------------------------------------------------------------
# #39 — provider-md5 content key + fingerprint a library from a URL (probe_remote)
# ---------------------------------------------------------------------------------------------


def _range_server(blobs: dict[str, bytes], *, status: int = 206) -> object:
    """A fake ``requests.get`` that serves a 206 Range slice of ``blobs[url]`` with a Content-Range.

    Honors ``Range: bytes=0-N`` exactly as ENA does, so a bounded read returns a bounded prefix and the
    206's ``Content-Range: .../TOTAL`` carries the true file size. ``status=200`` simulates a host that
    ignores Range and hands back the whole file — the case ``_range_get`` must refuse.
    """

    def fake_get(
        url: str,
        headers: dict[str, str] | None = None,
        timeout: object = None,
        stream: object = None,
    ) -> object:
        data = blobs[url]
        match = re.search(r"bytes=0-(\d+)", (headers or {}).get("Range", ""))
        chunk = data[: int(match.group(1)) + 1] if match else data
        return types.SimpleNamespace(
            status_code=status,
            content=chunk,
            headers={"Content-Range": f"bytes 0-{max(0, len(chunk) - 1)}/{len(data)}"},
            close=lambda: None,
        )

    return fake_get


def test_fastq_targets_pairs_each_url_with_its_md5() -> None:
    """ENA's fastq_ftp and fastq_md5 are index-aligned; the join is positional. This is the one place a
    URL and its content hash arrive together, which is what lets the remote probe key on the md5."""
    run = {
        "fastq_ftp": "ftp.x/a_1.fastq.gz;ftp.x/a_2.fastq.gz",
        "fastq_md5": "a" * 32 + ";" + "b" * 32,
    }
    assert fastq_targets(run) == [
        ("https://ftp.x/a_1.fastq.gz", "a" * 32),
        ("https://ftp.x/a_2.fastq.gz", "b" * 32),
    ]


def test_fastq_targets_pairs_before_sorting_so_url_and_md5_stay_aligned() -> None:
    """Sorting is by URL, but the pairing happens first — a reversed ftp order keeps each md5 with its
    own URL rather than re-aligning to the sorted position."""
    run = {
        "fastq_ftp": "ftp.x/a_2.fastq.gz;ftp.x/a_1.fastq.gz",
        "fastq_md5": "2" * 32 + ";" + "1" * 32,
    }
    assert fastq_targets(run) == [
        ("https://ftp.x/a_1.fastq.gz", "1" * 32),
        ("https://ftp.x/a_2.fastq.gz", "2" * 32),
    ]


def test_fastq_targets_refuses_to_mispair_on_a_length_mismatch() -> None:
    """A missing or short md5 list yields NO pairs rather than a silent mis-alignment: guessing which
    md5 goes with which URL would poison the content-address."""
    assert (
        fastq_targets({"fastq_ftp": "ftp.x/a_1.fastq.gz;ftp.x/a_2.fastq.gz", "fastq_md5": "a" * 32})
        == []
    )
    assert fastq_targets({"fastq_ftp": "ftp.x/a.fastq.gz", "fastq_md5": ""}) == []
    assert fastq_targets({}) == []


def test_content_key_from_md5_is_64_hex_and_injective() -> None:
    """The 32-hex provider md5 maps into the 64-hex content-address space injectively: identical md5 ->
    identical address (dedup is correct), distinct md5 -> distinct address, and it is case/space-stable."""
    a = content_key_from_md5("d41d8cd98f00b204e9800998ecf8427e")
    assert re.fullmatch(r"[0-9a-f]{64}", a)
    assert content_key_from_md5("  D41D8CD98F00B204E9800998ECF8427E ") == a  # normalized
    assert content_key_from_md5("0" * 32) != a  # a different md5 is a different address


def test_content_key_from_md5_rejects_a_non_md5() -> None:
    for bad in ("", "abc", "z" * 32, "a" * 31, "a" * 64):
        with pytest.raises(ValueError, match="md5"):
            content_key_from_md5(bad)


def test_content_range_total_parses_the_size_and_abstains_when_unknown() -> None:
    assert _content_range_total({"Content-Range": "bytes 0-65535/517000000"}) == 517000000
    assert _content_range_total({"Content-Range": "bytes 0-100/*"}) is None  # server doesn't know
    assert _content_range_total({}) is None


def test_uri_basename_strips_query_fragment_and_slash() -> None:
    assert _uri_basename("https://ftp.x/vol1/SRR1_1.fastq.gz?foo=1#bar") == "SRR1_1.fastq.gz"
    assert _uri_basename("https://ftp.x/SRR1_2.fastq.gz/") == "SRR1_2.fastq.gz"


def test_probe_remote_fingerprints_from_a_url_using_the_provider_md5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heart of #39: a bounded Range read becomes an Observation with NO local file. The provider
    md5 is the content-address (matching the hosted bytes), local_uri is None, size_bytes is the total
    the 206 declared, and the read geometry survives the round-trip through inflate + the signal pipeline."""
    data = _fastq_gz(400, read_len=90)
    md5 = hashlib.md5(data).hexdigest()
    url = "https://ftp.x/vol1/SRR1_2.fastq.gz"
    monkeypatch.setattr(remote.requests, "get", _range_server({url: data}))

    obs, seqs = probe_remote(url, md5=md5)

    assert obs.file.sha256 == content_key_from_md5(md5)  # the provider md5 IS the address
    assert obs.file.local_uri is None  # nothing was staged
    assert obs.file.basename == "SRR1_2.fastq.gz"
    assert obs.file.size_bytes == len(data)  # from Content-Range, not a local stat
    assert obs.read_length.mode == 90
    assert len(seqs) == 400  # the whole small member fit in the default range


def test_probe_remote_reads_a_bounded_prefix_never_the_whole_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`probe_remote` must never read a whole FASTQ. With a compressed budget smaller than the file it
    reads a strict prefix, drops the trailing partial record, and still yields a valid Observation whose
    size_bytes is the true total (from Content-Range) rather than the bytes read."""
    data = _fastq_gz(5000, read_len=90)
    url = "https://ftp.x/big.fastq.gz"
    monkeypatch.setattr(remote.requests, "get", _range_server({url: data}))

    obs, seqs = probe_remote(url, md5="a" * 32, max_compressed_bytes=512)

    assert obs.probe.compressed_bytes_read <= 512  # bounded by the range, not the file
    assert obs.probe.compressed_bytes_read < len(data)  # a strict prefix
    assert obs.gzip.truncated  # the tail past the range boundary was dropped
    assert len(seqs) > 0 and obs.read_length.mode == 90  # still a usable fingerprint
    assert obs.file.size_bytes == len(data)  # the whole-file size, from Content-Range


def test_probe_remote_without_md5_derives_a_bounded_remote_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provider md5 (a submitted BAM, or a bare URL) -> a bounded remote content key over
    basename + size + head, a valid 64-hex address that reads no whole file."""
    data = _fastq_gz(100, read_len=50)
    url = "https://ftp.x/nomd5.fastq.gz"
    monkeypatch.setattr(remote.requests, "get", _range_server({url: data}))

    obs, _seqs = probe_remote(url)

    assert re.fullmatch(r"[0-9a-f]{64}", obs.file.sha256)
    assert obs.file.sha256 != content_key_from_md5("a" * 32)  # not an md5 address
    assert obs.file.local_uri is None


def test_probe_remote_refuses_a_host_that_ignores_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 means the server ignored Range and is handing us the whole file — refuse, exactly as peek
    does. 'Bounded' means bounded by the server, not by our intentions."""
    data = _fastq_gz(10)
    url = "https://ftp.x/whole.fastq.gz"
    monkeypatch.setattr(remote.requests, "get", _range_server({url: data}, status=200))

    with pytest.raises(RemoteError, match="answered 200"):
        probe_remote(url, md5="a" * 32)


@pytest.mark.skipif(
    not os.environ.get("SEQFORGE_LIVE_NET"),
    reason="live-network smoke; set SEQFORGE_LIVE_NET=1 to fingerprint a real ENA URL",
)
def test_probe_remote_live_fingerprints_a_real_ena_url() -> None:
    """The genuine remote-peek E2E, opt-in (``SEQFORGE_LIVE_NET=1``): resolve a real run, range-read a
    bounded head of its hosted FASTQ, and confirm the provider md5 is the content-address that matches
    the hosted bytes — all without staging the file. Off by default so CI and a normal ``pixi run
    check`` stay fully offline; the offline tests above already exercise our whole code path."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)  # a live socket must never fail the suite
        try:
            runs = remote.ena_filereport("SRR9170959")  # a real, long-published run
            targets = [t for run in runs for t in fastq_targets(run)]
            if not targets:
                pytest.skip("ENA returned no fastq_ftp/fastq_md5 for the probe run")
            url, md5 = targets[0]
            obs, _seqs = probe_remote(url, md5=md5, max_compressed_bytes=1 << 18)  # 256 KB prefix
        except RemoteError as exc:  # pragma: no cover - host dependent
            pytest.skip(f"ENA unreachable: {exc}")

    assert obs.file.sha256 == content_key_from_md5(md5)  # the hosted md5 IS the address
    assert obs.file.local_uri is None  # nothing staged
    assert obs.probe.compressed_bytes_read <= (1 << 18)  # a bounded prefix, not the whole file
    assert obs.probe.n_reads_sampled > 0 and obs.read_length.mode > 0  # a usable fingerprint
