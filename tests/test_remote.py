"""Tests for the network surface — the PARSERS, offline.

Two of these endpoints (`run_new`, GEO SOFT) are **undocumented**, so their shape can change without
notice. The fixtures are trimmed from genuine responses (SRR9170959 is the real
dropped-technical-read case: SRA says 3 reads / 110 bases per spot, ENA published 50), and pinning
them means a change surfaces as a red test rather than as a silently empty result.
"""

from __future__ import annotations

import gzip
import re
import time
import types
from collections.abc import Callable

import pytest
import requests

from conftest import range_server
from seqforge.io import remote
from seqforge.io.remote import (
    RemoteError,
    RunStatistics,
    classify_accession,
    decompress_prefix,
    dropped_reads,
    fastq_targets,
    fastq_urls,
    parse_fastq_prefix,
    parse_filereport,
    parse_run_new,
    parse_soft_bioproject,
    parse_soft_srp,
    parse_soft_superseries,
    peek,
    probe_remote,
    retry_delay,
    technical_read_remedy,
)
from seqforge.probe import content_key_from_md5


def _resp(status: int, text: str = "", retry_after: str | None = None) -> types.SimpleNamespace:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return types.SimpleNamespace(status_code=status, text=text, headers=headers)


#: ``(outcomes, expected, n_calls)`` for ``remote._get``. Each outcome is a response to return or an
#: exception to raise; the last one repeats, so "always 503" is one row rather than a loop.
#:
#: These were five functions differing only in the sequence they fed the fake. The retry policy is a
#: TABLE — transient vs terminal, HTTP vs transport, succeed-on-retry vs exhaust-the-budget — and a
#: cell nobody wrote is a policy nobody decided. `expected` is the returned body, or a regex the
#: raised `RemoteError` must match.
RETRY_POLICY = [
    # A single 429 used to abort the whole `records` stage (#9). It now backs off and retries.
    pytest.param(
        [_resp(429, "rate limited", retry_after="0"), _resp(200, "OK")], "OK", 2,
        id="a-429-backs-off-then-succeeds",
    ),
    pytest.param(
        [_resp(503, "service unavailable")], re.compile("HTTP 503"), remote._MAX_RETRIES + 1,
        id="a-persistent-5xx-gives-up-after-the-retry-budget",
    ),
    pytest.param(
        [_resp(404, "not found")], re.compile("HTTP 404"), 1,
        id="a-404-is-terminal-and-is-not-retried",
    ),
    # A reset connection is the transport-level twin of a 5xx — NCBI resets under load (it aborted
    # GSE310667's records fetch live). It backs off and retries rather than aborting the stage.
    pytest.param(
        [
            requests.ConnectionError("('Connection aborted.', ConnectionResetError(104))"),
            _resp(200, "OK"),
        ],
        "OK", 2,
        id="a-dropped-connection-retries-then-succeeds",
    ),
    pytest.param(
        [requests.Timeout("read timed out")], re.compile("failed"), remote._MAX_RETRIES + 1,
        id="a-persistent-connection-error-gives-up",
    ),
]  # fmt: skip


@pytest.mark.parametrize("outcomes, expected, n_calls", RETRY_POLICY)
def test_get_retries_what_is_transient_and_gives_up_on_what_is_not(
    outcomes: list[object],
    expected: object,
    n_calls: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def fake_get(url: str, params: object = None, timeout: object = None) -> object:
        outcome = outcomes[min(calls["n"], len(outcomes) - 1)]
        calls["n"] += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    # Patched on the modules themselves, not through `remote.requests` / `remote.time`: those are
    # the same objects (`remote` did `import requests`, so the attribute IS `sys.modules`' entry), so
    # this is the identical mutation — and reaching them by their own name is what lets the checker
    # keep `no_implicit_reexport` on for every module in the tree.
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # no real wait in the test

    if isinstance(expected, re.Pattern):
        with pytest.raises(RemoteError, match=expected.pattern):
            remote._get("https://eutils.example/efetch")
    else:
        assert remote._get("https://eutils.example/efetch") == expected
    assert calls["n"] == n_calls


def test_retry_delay_honors_an_integer_retry_after_else_backs_off() -> None:
    assert retry_delay("2", 0) == 2.0  # server-specified wait wins
    assert retry_delay(None, 0) == 1.0  # base
    assert retry_delay(None, 2) == 4.0  # exponential: 1 * 2**2
    assert retry_delay(None, 99) == 16.0  # capped
    assert retry_delay("not-a-number", 0) == 1.0  # a date-form Retry-After falls back to backoff


# ---------------------------------------------------------------------------------------------
# accession classification
# ---------------------------------------------------------------------------------------------


#: ``accession -> namespace``, every namespace ENA's own HTTP 400 bodies name plus the shapes that
#: must come back ``unknown``: a bare prefix, a transcript, a gene. `unknown` is a first-class answer,
#: because guessing a namespace would send the query somewhere wrong.
NAMESPACES = {
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
    "SRS4245278": "sample",
    "SRA1234567": "submission",
    "": "unknown",
    "hello": "unknown",
    "GSE": "unknown",
    "SRR": "unknown",
    "NM_001301717": "unknown",
    "ENSG00000141510": "unknown",
}


def test_classify_every_namespace_and_refuse_to_guess_the_rest() -> None:
    for acc, kind in NAMESPACES.items():
        assert classify_accession(acc) == kind, acc


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

#: GSE207085, trimmed from the live brief record. A SubSeries declares no `term=SRP...` and has no
#: sub-series to walk — the BioProject URL is the only route to its runs. The supplementary-file line
#: is kept deliberately: it is the other URL in the record, and it must not be read as a BioProject.
_SOFT_SUBSERIES = """\
^SERIES = GSE207085
!Series_title = Three-dimensional morphologic and molecular atlases of murine nasal vasculatures \
[Mouse_Nasal_SmartSeq]
!Series_supplementary_file = ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE207nnn/GSE207085/suppl/GSE207085_ss3_prox1_ct_normalized_expression_matrix.csv.gz
!Series_relation = SubSeries of: GSE207086
!Series_relation = BioProject: https://www.ncbi.nlm.nih.gov/bioproject/PRJNA853582
"""

#: GSE207086, the SuperSeries above GSE207085 — it declares BOTH its sub-series and an umbrella
#: BioProject, which is what makes the order the two are tried in observable.
_SOFT_SUPERSERIES_WITH_BIOPROJECT = """\
^SERIES = GSE207086
!Series_title = Three-dimensional morphologic and molecular atlases of human/mouse nasal vasculatures
!Series_relation = SuperSeries of: GSE207083
!Series_relation = SuperSeries of: GSE207084
!Series_relation = SuperSeries of: GSE207085
!Series_relation = BioProject: https://www.ncbi.nlm.nih.gov/bioproject/PRJNA853578
"""

_SOFT_WITH_NOTHING = """\
^SERIES = GSE999999
!Series_title = Processed matrices only
!Series_supplementary_file = ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE999nnn/GSE999999/suppl/counts.csv.gz
"""


def _soft_stub(
    records: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> list[str]:  # returns the fetch log
    """Serve GEO SOFT from a dict, and record which accessions were asked for."""
    fetched: list[str] = []

    def fake_soft(accession: str) -> str:
        fetched.append(accession)
        return records[accession]

    monkeypatch.setattr(remote, "geo_soft", fake_soft)
    return fetched


#: ``(parser, soft, expected)``. Each parser reads its own relation line and nothing else: a record
#: carries several URLs, only the SRA one holds `term=SRP...`, and a supplementary FTP path under
#: `GSE207nnn` is not an accession. A SuperSeries declares no SRP of its own — eutils and runinfo both
#: return ZERO for one, silently, so a resolver that misses the sub-series reports success and loses
#: the whole dataset, the worst kind of wrong.
SOFT_PARSERS = [
    pytest.param(parse_soft_srp, _SOFT_WITH_SRP, ["SRP299835"], id="srp-not-bioproject"),
    pytest.param(parse_soft_srp, _SOFT_SUPERSERIES, [], id="a-superseries-declares-no-srp"),
    pytest.param(
        parse_soft_superseries,
        _SOFT_SUPERSERIES,
        ["GSE140399", "GSE140510"],
        id="its-sub-series",
    ),
    pytest.param(parse_soft_bioproject, _SOFT_SUBSERIES, ["PRJNA853582"], id="a-subseries-project"),
    pytest.param(parse_soft_bioproject, _SOFT_WITH_SRP, ["PRJNA692883"], id="beside-an-srp"),
    pytest.param(parse_soft_bioproject, _SOFT_WITH_NOTHING, [], id="no-bioproject-at-all"),
]


@pytest.mark.parametrize("parser, soft, expected", SOFT_PARSERS)
def test_the_soft_parsers_read_their_own_relation_line(
    parser: Callable[[str], list[str]], soft: str, expected: list[str]
) -> None:
    assert parser(soft) == expected


def test_a_subseries_resolves_through_its_own_bioproject(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GEO accession resolves to the runs of THAT accession (#238).

    A SubSeries declares neither an SRA study nor a sub-series, so it had nothing to walk and
    `io resolve GSE207085` exited 1; its own BioProject is the route. The fetch log is half the
    assertion — walking up to GSE207086 and back down returns the siblings' runs too.
    """
    fetched = _soft_stub({"GSE207085": _SOFT_SUBSERIES}, monkeypatch)

    assert remote.geo_to_studies("GSE207085") == ["PRJNA853582"]
    assert fetched == ["GSE207085"], "the SuperSeries and its siblings were never consulted"


def test_a_superseries_still_walks_down_rather_than_taking_its_umbrella_bioproject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The BioProject is the last resort, not the first: a SuperSeries declares one that spans every
    sub-series, and taking it would answer with the union whichever accession was asked for."""
    fetched = _soft_stub(
        {
            "GSE207086": _SOFT_SUPERSERIES_WITH_BIOPROJECT,
            "GSE207083": _SOFT_SUBSERIES.replace("PRJNA853582", "PRJNA853580"),
            "GSE207084": _SOFT_SUBSERIES.replace("PRJNA853582", "PRJNA853581"),
            "GSE207085": _SOFT_SUBSERIES,
        },
        monkeypatch,
    )

    assert remote.geo_to_studies("GSE207086") == ["PRJNA853580", "PRJNA853581", "PRJNA853582"]
    assert "PRJNA853578" not in fetched
    assert sorted(fetched) == ["GSE207083", "GSE207084", "GSE207085", "GSE207086"]


def test_a_geo_record_declaring_neither_refuses_and_names_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A series carrying only processed matrices has no raw data to point at. That is still a
    failure, and the message names the declarations looked for so a reader can check the record."""
    _soft_stub({"GSE999999": _SOFT_WITH_NOTHING}, monkeypatch)

    with pytest.raises(RemoteError, match="no SRA study, sub-series or BioProject"):
        remote.geo_to_studies("GSE999999")


# ---------------------------------------------------------------------------------------------
# ENA filereport
# ---------------------------------------------------------------------------------------------

_TSV = (
    "run_accession\tread_count\tbase_count\tfastq_ftp\tlibrary_layout\n"
    "SRR9170959\t79615125\t3980756250\tftp.sra.ebi.ac.uk/vol1/fastq/SRR917/009/SRR9170959/SRR9170959.fastq.gz\tPAIRED\n"
)


def test_parse_filereport_reads_rows_and_treats_a_header_only_tsv_as_empty() -> None:
    """A data row parses into a dict keyed by column; a header-only or empty TSV is [], not an error."""
    rows = parse_filereport(_TSV)
    assert len(rows) == 1
    assert rows[0]["run_accession"] == "SRR9170959"
    assert rows[0]["read_count"] == "79615125"

    assert parse_filereport("run_accession\tread_count\n") == []
    assert parse_filereport("") == []


#: What the filereport must ASK ENA for, one entry per deposit that is invisible without it.
#: `library_construction_protocol` is ENA's answer to SRA's LIBRARY_CONSTRUCTION_PROTOCOL and the only
#: ENA field carrying a submitter's prose about how the library was built (#237). The four
#: `submitted_*` are ONE fact — ENA's spelling of what SRA publishes on `<SRAFile supertype="Original">`,
#: an address over the submitter's own upload (ADR-0033) — and three quarters of it was requested:
#: name, size and format, never the hash. ENA generates no FASTQ at all for a cellranger BAM, so there
#: the submitted file is the only data and its md5 the only content-address on offer.
ENA_ASKS_FOR = [
    "library_construction_protocol",
    "submitted_ftp",
    "submitted_bytes",
    "submitted_format",
    "submitted_md5",
]


@pytest.mark.parametrize("field", ENA_ASKS_FOR)
def test_the_filereport_asks_ena_for_the_fields_a_deposit_is_invisible_without(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, dict[str, str] | None] = {}

    def fake_get(url: str, params: dict[str, str] | None = None, timeout: int = 30) -> str:
        captured["params"] = params
        return f"{field}\nvalue\n"

    monkeypatch.setattr(remote, "_get", fake_get)

    remote.ena_filereport("SRP383998")
    params = captured["params"]
    assert params is not None
    assert field in params["fields"].split(","), params["fields"]


def test_a_bam_only_run_resolves_to_its_submitted_md5_and_says_why_it_has_no_fastq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking ENA for the hash is half of it — `io resolve` is where a human sees the answer.

    Driven on an ERR run because `run_new` is an NCBI endpoint that serves SRR only, so this reaches
    no network: what is under test is the annotation, not the statistics call it skips.
    """
    run = {
        "run_accession": "ERR4082915",
        "submitted_ftp": "ftp.sra.ebi.ac.uk/vol1/err/ERR408/possorted_genome_bam.bam",
        "submitted_bytes": "28543057",
        "submitted_md5": "993e02dd8079b30a23285828a8ee9982",
    }
    monkeypatch.setattr(remote, "ena_filereport", lambda _acc: [run])

    entry = remote.resolve_accession("ERR4082915")["runs"][0]

    assert entry["submitted_md5"] == run["submitted_md5"]
    assert entry["submitted_bytes"] == "28543057"  # the siblings it must arrive beside
    assert entry["fastq_urls"] == [] and "note" in entry, "the BAM case: submitted is all there is"


#: ``(run, expected)`` for ``fastq_urls``: it splits ``fastq_ftp`` on ``;``, prepends the ``https://``
#: scheme, and sorts (ENA does not guarantee order). An absent or empty field is a meaningful "no
#: fastq" — the 10x case, where ENA generates none for a cellranger BAM / a BAM with CB tags — not a
#: crash.
FASTQ_URLS = [
    pytest.param(
        {"fastq_ftp": "ftp.x/a_1.fastq.gz;ftp.x/a_2.fastq.gz"},
        ["https://ftp.x/a_1.fastq.gz", "https://ftp.x/a_2.fastq.gz"],
        id="splits-and-adds-scheme",
    ),
    pytest.param(
        {"fastq_ftp": "ftp.x/a_2.fastq.gz;ftp.x/a_1.fastq.gz"},
        ["https://ftp.x/a_1.fastq.gz", "https://ftp.x/a_2.fastq.gz"],
        id="sorted-because-ena-order-is-not-guaranteed",
    ),
    pytest.param({"fastq_ftp": ""}, [], id="empty-field-is-no-fastq"),
    pytest.param({}, [], id="missing-field-is-no-fastq"),
]


@pytest.mark.parametrize("run, expected", FASTQ_URLS)
def test_fastq_urls_splits_prefixes_sorts_and_treats_empty_as_no_fastq(
    run: dict[str, str], expected: list[str]
) -> None:
    assert fastq_urls(run) == expected


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


#: ``(xml, n_reads, lengths, spot_length, read_types)``. The endpoint is undocumented, so every field
#: is optional: `readTypes` appears only for fastq-load.py submissions and its absence is NORMAL, and
#: a well-formed document declaring no per-read table is a legitimate empty rather than garbage.
RUN_NEW = [
    pytest.param(_RUN_NEW_DROPPED, 3, [50, 50, 10], 110, "TBT", id="the-per-read-table"),
    pytest.param(_RUN_NEW_CLEAN, 2, [26, 98], 124, None, id="a-missing-readtypes-is-normal"),
    pytest.param("<RUN_LIST><RUN/></RUN_LIST>", 0, [], 0, None, id="no-statistics-is-an-empty"),
]


@pytest.mark.parametrize("xml, n_reads, lengths, spot_length, read_types", RUN_NEW)
def test_parse_run_new_reads_the_per_read_table(
    xml: str, n_reads: int, lengths: list[int], spot_length: int, read_types: str | None
) -> None:
    stats = parse_run_new(xml, "SRR9170959")
    assert stats.n_reads == n_reads
    assert [r.average_length for r in stats.reads] == lengths
    assert stats.spot_length == spot_length
    assert stats.read_types == read_types  # "TBT" = Technical / Biological / Technical


def test_parse_run_new_refuses_xml_it_cannot_parse() -> None:
    """A loud refusal, never a silent zero reads that reads as "this run has no technical read"."""
    with pytest.raises(RemoteError, match="unparsable"):
        parse_run_new("<not xml", "SRR1")


# ---------------------------------------------------------------------------------------------
# the dropped-technical-read detector (rung 0 — two metadata calls, zero bytes)
# ---------------------------------------------------------------------------------------------


def test_detects_a_dropped_technical_read() -> None:
    """The real SRR9170959 case: SRA says 110 bases/spot across 3 reads; ENA published 50. A dropped
    10x barcode read leaves a dataset that looks like plain single-end RNA-seq and is silently
    unprocessable as single-cell — and this costs two metadata calls and no bytes."""
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


_CLEAN_STATS = parse_run_new(_RUN_NEW_CLEAN, "SRR8526547")  # 26+98=124 declared

#: ``(run, stats)`` the dropped-read detector must NOT flag — the verdict is always ``None``. A
#: detector that accuses on absent evidence, or on a sub-1-base gap that is arithmetic rather than a
#: dropped read, gets switched off, so null-over-wrong is pinned across every not-a-drop shape: the
#: archives agree (124 declared, 124 published), no run fields at all, zero counts, empty SRA
#: statistics, and ENA's mean bases/spot rounding a hair under SRA's.
NO_DROP = [
    pytest.param(
        {
            "read_count": "100",
            "base_count": "12400",
            "fastq_ftp": "ftp.x/a_1.fq.gz;ftp.x/a_2.fq.gz",
        },
        _CLEAN_STATS,
        id="archives-agree",
    ),
    pytest.param({}, _CLEAN_STATS, id="no-run-fields"),
    pytest.param({"read_count": "0", "base_count": "0"}, _CLEAN_STATS, id="zero-counts"),
    pytest.param(
        {"read_count": "100", "base_count": "12400"}, RunStatistics("SRR1"), id="empty-stats"
    ),
    pytest.param(
        {"read_count": "100", "base_count": "12350"}, _CLEAN_STATS, id="sub-1-base-rounding"
    ),  # 123.5 vs SRA's 124
]


@pytest.mark.parametrize("run, stats", NO_DROP)
def test_the_detector_abstains_rather_than_falsely_accusing(
    run: dict[str, str], stats: RunStatistics
) -> None:
    """Missing, agreeing, or rounding-level inputs => ABSTAIN, never a false accusation."""
    assert dropped_reads(run, stats) is None


def test_the_remedy_names_the_fix_first_and_a_record_we_hold_before_a_second_api() -> None:
    """A Blocker's remedy must be operable, and the ORDER is the claim.

    `fasterq-dump --include-technical` is the real fix. The fallback used to be "go query SDL and
    hope", for a fact an already fetched, parsed and cached `ArchiveRecordSet` states: it names the
    `sra-pub-src-*` bucket per run, so SDL is one route to those bytes and never the only one
    (ADR-0033) — and originals exist for select studies only, so most runs dead-end there.
    """
    remedy = technical_read_remedy("SRR9170959")

    assert "--include-technical" in remedy
    assert remedy.index("fasterq-dump") < remedy.index("seqforge io records SRR9170959")
    assert remedy.index("io records") < remedy.index("Data Locator"), (
        "the record set we hold is the first fallback; SDL is what a deposit with no originals leaves"
    )


# ---------------------------------------------------------------------------------------------
# io peek — bounded gzip prefix decoding
# ---------------------------------------------------------------------------------------------


def _fastq_gz(n: int = 50, read_len: int = 90) -> bytes:
    body = "".join(f"@READ:{i}\n{'A' * read_len}\n+\n{'I' * read_len}\n" for i in range(n))
    return gzip.compress(body.encode())


def test_decompress_prefix_tolerates_a_truncated_tail() -> None:
    """The core claim of `io peek`: a byte-range prefix inflates without raising. zlib returns fewer
    bytes and leaves eof False — so "handling truncation" is just stopping."""
    blob = _fastq_gz(500)
    out = decompress_prefix(blob[: len(blob) // 2], max_bytes=1 << 20)
    assert len(out) > 0
    assert b"@READ:0" in out


def test_decompress_prefix_inflates_under_a_decompressed_byte_budget() -> None:
    """The budget is on DECOMPRESSED bytes, not a compressed-byte proxy — also a zip-bomb guard."""
    assert decompress_prefix(_fastq_gz(3), max_bytes=1 << 20).decode().count("@READ:") == 3
    assert len(decompress_prefix(_fastq_gz(5000), max_bytes=1000)) <= 1000


def test_decompress_prefix_rejects_a_corrupt_member() -> None:
    with pytest.raises(RemoteError, match="not readable"):
        decompress_prefix(b"this is not gzip at all", max_bytes=1000)


_TWENTY_RECS = "".join(f"@r{i}\nACGT\n+\nIIII\n" for i in range(20))

#: ``(text, max_reads, headers, lengths)`` for ``parse_fastq_prefix``. It reads whole records only — a
#: partial trailing record (the range boundary cut mid-record) is dropped, never reported as a read
#: length — stops at ``max_reads``, and returns two empty lists on empty input.
FASTQ_PREFIX = [
    pytest.param("@a\nACGT\n+\nIIII\n@b\nACG", 10, ["@a"], [4], id="drops-partial-trailing-record"),
    pytest.param(_TWENTY_RECS, 3, [f"@r{i}" for i in range(3)], [4, 4, 4], id="respects-max-reads"),
    pytest.param("", 4, [], [], id="empty-input"),
]


@pytest.mark.parametrize("text, max_reads, headers, lengths", FASTQ_PREFIX)
def test_parse_fastq_prefix_reads_whole_records_up_to_the_budget(
    text: str, max_reads: int, headers: list[str], lengths: list[int]
) -> None:
    assert parse_fastq_prefix(text, max_reads=max_reads) == (headers, lengths)


def test_peek_range_reads_a_url_and_reports_its_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `io peek` verb itself, not a hand-composition of its parts. Its two helpers are covered
    above; what this pins is the FUNCTION -- the `max_bytes` it hands the range read, the `.decode()`,
    and the PeekResult keys `seqforge io peek` prints -- none of which a test that inlines
    `decompress_prefix` + `parse_fastq_prefix` exercises (#110)."""
    data = _fastq_gz(200, read_len=90)
    url = "https://ftp.x/head.fastq.gz"
    monkeypatch.setattr(requests, "get", range_server({url: data}))

    result = peek(url, max_reads=4)

    assert result["uri"] == url
    assert result["example_header"] == "@READ:0"
    assert result["n_records"] == 4  # capped by max_reads, not the 200 in the member
    assert set(result["read_lengths"]) == {90}
    assert (
        0 < result["compressed_bytes_read"] <= (1 << 16)
    )  # the default range bound, never the file


# ---------------------------------------------------------------------------------------------
# #39 — provider-md5 content key + fingerprint a library from a URL (probe_remote)
# ---------------------------------------------------------------------------------------------


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


def test_content_key_from_md5_is_an_injective_64_hex_address_or_a_refusal() -> None:
    """The 32-hex provider md5 maps into the 64-hex content-address space injectively: identical md5 ->
    identical address (dedup is correct), distinct md5 -> distinct address, case/space-stable — and
    anything that is not an md5 is refused rather than folded into a plausible-looking address."""
    a = content_key_from_md5("d41d8cd98f00b204e9800998ecf8427e")
    assert re.fullmatch(r"[0-9a-f]{64}", a)
    assert content_key_from_md5("  D41D8CD98F00B204E9800998ECF8427E ") == a  # normalized
    assert content_key_from_md5("0" * 32) != a  # a different md5 is a different address

    for bad in ("", "abc", "z" * 32, "a" * 31, "a" * 64):
        with pytest.raises(ValueError, match="md5"):
            content_key_from_md5(bad)


def test_probe_remote_reads_a_bounded_prefix_never_the_whole_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`probe_remote` must never read a whole FASTQ. With a compressed budget smaller than the file it
    reads a strict prefix, drops the trailing partial record, and still yields a valid Observation
    addressed by the provider md5, with no local file and the true total the 206 declared."""
    data = _fastq_gz(5000, read_len=90)
    url = "https://ftp.x/big.fastq.gz"
    monkeypatch.setattr(requests, "get", range_server({url: data}))

    obs, seqs = probe_remote(url, md5="a" * 32, max_compressed_bytes=512)

    assert obs.probe.compressed_bytes_read <= 512  # bounded by the range, not the file
    assert obs.probe.compressed_bytes_read < len(data)  # a strict prefix
    assert obs.gzip.truncated  # the tail past the range boundary was dropped
    assert len(seqs) > 0 and obs.read_length.mode == 90  # still a usable fingerprint
    assert obs.file.sha256 == content_key_from_md5("a" * 32)  # the provider md5 IS the address
    assert obs.file.local_uri is None  # nothing was staged
    assert obs.file.size_bytes == len(data)  # the whole-file size, from Content-Range


def test_a_206_that_declares_no_total_sizes_the_file_at_the_bytes_actually_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Content-Range: bytes 0-N/*` — a streaming host that does not know the length. The size falls
    back to what was read, and must never become 0 or the whole file's true size, which nothing here
    was told. This is the only branch where the declared total is genuinely absent, and it is
    unreachable through a server that answers a number."""
    data = _fastq_gz(5000, read_len=90)
    url = "https://ftp.x/streamed.fastq.gz"
    monkeypatch.setattr(requests, "get", range_server({url: data}, known_total=False))

    obs, _seqs = probe_remote(url, md5="a" * 32, max_compressed_bytes=512)

    assert obs.file.size_bytes == obs.probe.compressed_bytes_read  # the fall back, not the total
    assert obs.file.size_bytes < len(data)  # the true size was never declared, so never claimed


def test_probe_remote_without_md5_derives_a_bounded_remote_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provider md5 (a submitted BAM, or a bare URL) -> a bounded remote content key over
    basename + size + head, a valid 64-hex address that reads no whole file."""
    data = _fastq_gz(100, read_len=50)
    url = "https://ftp.x/nomd5.fastq.gz?token=abc#frag"
    monkeypatch.setattr(requests, "get", range_server({url: data}))

    obs, _seqs = probe_remote(url)

    assert obs.file.basename == "nomd5.fastq.gz"  # the key's only name: not the query, not the frag
    assert re.fullmatch(r"[0-9a-f]{64}", obs.file.sha256)
    assert obs.file.sha256 != content_key_from_md5("a" * 32)  # not an md5 address
    assert obs.file.local_uri is None


def test_probe_remote_refuses_a_host_that_ignores_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 means the server ignored Range and is handing us the whole file — refuse, exactly as peek
    does. 'Bounded' means bounded by the server, not by our intentions."""
    data = _fastq_gz(10)
    url = "https://ftp.x/whole.fastq.gz"
    monkeypatch.setattr(requests, "get", range_server({url: data}, status=200))

    with pytest.raises(RemoteError, match="answered 200"):
        probe_remote(url, md5="a" * 32)
