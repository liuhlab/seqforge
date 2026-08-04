"""The SRA-streaming fingerprint path: ``probe_sra`` + ``build_fingerprint_sra`` + the CLI verbs.

The ``labdata`` stream seam is faked so the whole path runs with no sra-tools and no network: a fake
``labdata.stream_run_reads`` returns a canned :class:`RunReadPreview`-shaped object (reads bucketed by
within-spot index), and the content-address precedence, the fingerprint package, and the ``io
probe-sra`` / ``preflight --accession`` verbs are all exercised against it. The one real dependency is
that ``labdata`` imports (it ships with the lab stack); the shipped build may predate
``stream_run_reads``, so it is patched with ``raising=False``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from seqforge.cli import app
from seqforge.fingerprint.load import load_fingerprint, probed_from_fingerprint
from seqforge.io import remote, sra
from seqforge.io.remote import RemoteError, fastq_targets_meta
from seqforge.probe import content_key_from_md5, content_key_from_sra

runner = CliRunner()

SRR = "SRR31555583"
SRX = "SRX26999999"
MD5_1 = "a" * 32
MD5_2 = "b" * 32


# --------------------------------------------------------------------------- #
# a canned RunReadPreview (the shape labdata.stream_run_reads returns)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Rec:
    header: bytes
    seq: bytes
    plus: bytes
    qual: bytes


@dataclass
class _Preview:
    reads: dict[int, list[_Rec]]
    read_lengths: dict[int, int]
    n_spots_returned: int

    def read_indexes(self) -> list[int]:
        return sorted(self.reads)


def _mate(acc: str, index: int, length: int, n: int, base: bytes = b"A") -> list[_Rec]:
    return [
        _Rec(
            header=f"@{acc}.{i}.{index} {i} length={length}".encode(),
            seq=base * length,
            plus=b"+",
            qual=b"I" * length,
        )
        for i in range(1, n + 1)
    ]


def _preview(acc: str, geometry: dict[int, int], *, n: int = 50) -> _Preview:
    """A preview with ``n`` spots, each mate ``index`` at length ``geometry[index]``."""
    reads = {index: _mate(acc, index, length, n) for index, length in geometry.items()}
    return _Preview(reads=reads, read_lengths=dict(geometry), n_spots_returned=n)


#: How much shorter a trimmed record comes back than its mate's untrimmed peak.
_TRIM = 20


def _trimmed_preview(acc: str, geometry: dict[int, int], *, short: int, n: int = 50) -> _Preview:
    """A variable-length preview: ``short`` of the ``n`` spots came back trimmed at every mate.

    The trimmed records stay a minority, so ``read_lengths`` is still ``geometry[index]`` — a mode
    sitting at the untrimmed peak while the real average sits below it. That gap is the shape of any
    run quality- or adapter-trimmed before submission.
    """
    reads = {
        index: _mate(acc, index, length, n - short) + _mate(acc, index, length - _TRIM, short)
        for index, length in geometry.items()
    }
    return _Preview(reads=reads, read_lengths=dict(geometry), n_spots_returned=n)


@dataclass
class _FakeStream:
    """Stand-in for ``labdata.stream_run_reads``; returns a canned preview and records call args."""

    preview: _Preview | None = None
    exc: Exception | None = None
    calls: list[tuple[str, int, bool]] = field(default_factory=list)

    def __call__(self, run_accession: str, *, n_spots: int, include_technical: bool) -> _Preview:
        self.calls.append((run_accession, n_spots, include_technical))
        if self.exc is not None:
            raise self.exc
        assert self.preview is not None
        return self.preview


def _patch_stream(monkeypatch: pytest.MonkeyPatch, fake: _FakeStream) -> None:
    import labdata

    monkeypatch.setattr(labdata, "stream_run_reads", fake, raising=False)


def _ena_run(**overrides: object) -> dict[str, object]:
    """An ENA filereport row that mirrors ``SRR`` faithfully (two paired FASTQ, aligned md5/bytes)."""
    base: dict[str, object] = {
        "run_accession": SRR,
        "experiment_accession": SRX,
        "fastq_ftp": (
            f"ftp.sra.ebi.ac.uk/vol1/fastq/{SRR}/{SRR}_1.fastq.gz;"
            f"ftp.sra.ebi.ac.uk/vol1/fastq/{SRR}/{SRR}_2.fastq.gz"
        ),
        "fastq_md5": f"{MD5_1};{MD5_2}",
        "fastq_bytes": "111;222",
        "read_count": "1000",
    }
    base.update(overrides)
    return base


#: PRJNA853582 (GSE207085), the plate deposit that motivated the multi-experiment package: 1440 cells,
#: every one of them its own SRX under one study.
PLATE_STUDY = "PRJNA853582"
PLATE_EXPERIMENTS = 1440
PLATE_SRP = "SRP853582"


def _plate_run(i: int) -> dict[str, str]:
    """One cell of a plate deposit: its own SRX, no ENA mirror, sharing the study with every other."""
    return {
        "run_accession": f"SRR{i:07d}",
        "experiment_accession": f"SRX{i:07d}",
        "study_accession": PLATE_SRP,
        "read_count": "1000",
    }


# --------------------------------------------------------------------------- #
# fastq_targets_meta — url/md5/size join
# --------------------------------------------------------------------------- #


#: ``(run, expected)`` for ``fastq_targets_meta`` — the positional url/md5/size join. ``fastq_bytes``
#: is aligned to the UNSORTED ``fastq_ftp``, so the join must re-associate by url after sorting; a
#: url/md5 length mismatch yields NO pairs (never a silent mis-alignment); a missing size defaults to
#: 0 rather than dropping the row.
FASTQ_TARGETS_META = [
    pytest.param(
        {
            "fastq_ftp": f"host/{SRR}_2.fastq.gz;host/{SRR}_1.fastq.gz",
            "fastq_md5": f"{MD5_2};{MD5_1}",
            "fastq_bytes": "222;111",
        },
        [
            (f"https://host/{SRR}_1.fastq.gz", MD5_1, 111),
            (f"https://host/{SRR}_2.fastq.gz", MD5_2, 222),
        ],
        id="joins-and-re-associates-by-url-after-sort",
    ),
    pytest.param(
        {"fastq_ftp": "host/a;host/b", "fastq_md5": MD5_1}, [], id="url-md5-mismatch-is-empty"
    ),
    pytest.param(
        {"fastq_ftp": f"host/{SRR}_1.fastq.gz", "fastq_md5": MD5_1},
        [(f"https://host/{SRR}_1.fastq.gz", MD5_1, 0)],
        id="missing-bytes-defaults-size-to-zero",
    ),
]


@pytest.mark.parametrize("run, expected", FASTQ_TARGETS_META)
def test_fastq_targets_meta_joins_url_md5_and_size(
    run: dict[str, str], expected: list[tuple[str, str, int]]
) -> None:
    assert fastq_targets_meta(run) == expected


# --------------------------------------------------------------------------- #
# probe_sra — content-address precedence
# --------------------------------------------------------------------------- #


def test_probe_sra_adopts_the_ena_identity_and_builds_real_chemistry_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One faithful-mirror stream, observed whole: the ENA md5 identity AND the chemistry signals.

    When the ENA mirror is faithful (two paired files, aligned md5/bytes), each mate adopts its file's
    provider md5 as the content-address (``ena_verified``), keeps its size and basename, and the stream
    is asked for ``n_reads`` spots WITH technical reads. The same observation also carries the real
    chemistry the resolver reads — read-length mode, the sampled count, the sequences — with no local
    path, because a stream has none.
    """
    fake = _FakeStream(_preview(SRR, {1: 28, 2: 94}))
    _patch_stream(monkeypatch, fake)

    mates = sra.probe_sra(_ena_run(), n_reads=50)

    # Identity: read index 1 -> the _1 file (both sort ascending); its md5 IS the content-address.
    assert [m.read_index for m in mates] == [1, 2]
    assert all(m.ena_verified for m in mates)
    assert mates[0].observation.file.sha256 == content_key_from_md5(MD5_1)
    assert mates[0].observation.file.size_bytes == 111
    assert mates[0].basename == f"{SRR}_1.fastq.gz"
    assert mates[1].observation.file.sha256 == content_key_from_md5(MD5_2)
    assert mates[1].observation.file.size_bytes == 222
    # technical reads are kept, and the stream is asked for n_reads spots.
    assert fake.calls == [(SRR, 50, True)]

    # Chemistry: the same mate carries the signals resolve needs, from a stream with no local path.
    obs = mates[0].observation
    assert obs.read_length.mode == 28
    assert obs.probe.n_reads_sampled == 50
    assert mates[0].seqs  # the sampled sequences resolve needs
    assert obs.file.local_uri is None  # a stream has no local path


def test_probe_sra_falls_back_to_a_synthetic_address_when_a_technical_read_was_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SRA has two reads; ENA published one file AND we flagged the drop — the mirror is unfaithful.
    _patch_stream(monkeypatch, _FakeStream(_preview(SRR, {1: 28, 2: 94})))
    run = _ena_run(
        fastq_ftp=f"host/{SRR}.fastq.gz",
        fastq_md5=MD5_1,
        fastq_bytes="111",
        technical_read_dropped=True,
    )

    mates = sra.probe_sra(run, n_reads=50)

    assert not any(m.ena_verified for m in mates)
    assert mates[0].observation.file.sha256 == content_key_from_sra(
        SRR, 1, spot_count=1000, read_length=28
    )
    assert mates[0].basename == f"{SRR}_1.fastq.gz"


def test_probe_sra_sees_a_lossy_mirror_in_the_stream_it_already_took(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENA lists a file per mate, but published fewer bases per spot than the ``.sra`` holds.

    That verdict used to arrive on the run row from a per-run NCBI call the resolver made for every
    run in the study. The stream is taken with technical reads included, so it already carries the
    per-read table the call would have returned — the address stays honest and costs no request.
    """
    _patch_stream(monkeypatch, _FakeStream(_preview(SRR, {1: 28, 2: 94})))
    calls: list[str] = []
    monkeypatch.setattr(remote, "run_statistics", calls.append)
    # 122 bases per spot streamed, 94 published: the barcode read never reached the mirror.
    run = _ena_run(base_count="94000")

    mates = sra.probe_sra(run, n_reads=50)

    assert not any(m.ena_verified for m in mates)
    assert mates[0].observation.file.sha256 == content_key_from_sra(
        SRR, 1, spot_count=1000, read_length=28
    )
    assert calls == []


def test_the_streamed_read_table_answers_only_where_a_mode_is_an_average() -> None:
    """The table is a per-read average where the stream holds one, and ``None`` where it does not.

    ``preview.read_lengths`` is a MODE. Where every record at an index came back the same length that
    mode is also that index's average and the table can be built from it; where the lengths vary,
    nothing in hand is an average, so there is no table and the caller has to abstain. "Cannot tell"
    and "agrees" are different answers and this is where they part.
    """
    fixed = _preview(SRR, {1: 28, 2: 94})
    table = sra._streamed_read_table(SRR, fixed, fixed.read_indexes(), 1000)
    assert table is not None
    assert [(r.index, r.average_length) for r in table.reads] == [(1, 28), (2, 94)]

    trimmed = _trimmed_preview(SRR, {1: 150, 2: 150}, short=10)
    assert sra._streamed_read_table(SRR, trimmed, trimmed.read_indexes(), 1000) is None


def test_probe_sra_keeps_the_ena_identity_when_the_stream_shows_variable_read_lengths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trimmed run that was mirrored faithfully must not be accused of dropping a read.

    Summing modes against ENA's ``base_count / read_count`` compares a peak to an average: a 2x150
    run trimmed to a mean of 140 per read streams as 150 + 150 = 300 while ENA published 280 bases
    per spot, so a mirror that lost nothing looks lossy. The accusation would be silent — the run
    would just lose its ENA-adopted address for the synthetic one and move the dataset hash — so the
    comparison abstains instead, which is what it promises and costs no request either way.
    """
    _patch_stream(monkeypatch, _FakeStream(_trimmed_preview(SRR, {1: 150, 2: 150}, short=10)))
    calls: list[str] = []
    monkeypatch.setattr(remote, "run_statistics", calls.append)

    mates = sra.probe_sra(_ena_run(base_count="280000"), n_reads=50)

    assert all(m.ena_verified for m in mates)
    assert mates[0].observation.file.sha256 == content_key_from_md5(MD5_1)
    assert calls == []


def test_probe_sra_adopts_the_ena_identity_when_fixed_length_reads_agree_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixed-length reads still reach the comparison, and 28 + 94 against 122 published agrees.

    The abstain above is "cannot tell", not "agrees": where every record at an index came back one
    length, that length is the average, the comparison runs, and this mirror published every base the
    stream holds.
    """
    _patch_stream(monkeypatch, _FakeStream(_preview(SRR, {1: 28, 2: 94})))
    calls: list[str] = []
    monkeypatch.setattr(remote, "run_statistics", calls.append)

    mates = sra.probe_sra(_ena_run(base_count="122000"), n_reads=50)

    assert all(m.ena_verified for m in mates)
    assert mates[0].observation.file.sha256 == content_key_from_md5(MD5_1)
    assert calls == []


def test_probe_sra_falls_back_when_ena_never_mirrored_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stream(monkeypatch, _FakeStream(_preview(SRR, {1: 28, 2: 94})))

    mates = sra.probe_sra({"run_accession": SRR, "read_count": "1000"}, n_reads=50)

    assert not any(m.ena_verified for m in mates)
    assert mates[1].observation.file.sha256 == content_key_from_sra(
        SRR, 2, spot_count=1000, read_length=94
    )


def test_probe_sra_synthetic_address_is_invariant_to_the_spot_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = {"run_accession": SRR, "read_count": "1000"}  # whole-run spot count fixed, not from N

    _patch_stream(monkeypatch, _FakeStream(_preview(SRR, {1: 28}, n=50)))
    sha_small = sra.probe_sra(run, n_reads=50)[0].observation.file.sha256

    _patch_stream(monkeypatch, _FakeStream(_preview(SRR, {1: 28}, n=100)))
    sha_large = sra.probe_sra(run, n_reads=100)[0].observation.file.sha256

    assert sha_small == sha_large  # the address does not depend on how many spots were streamed


def _download_error() -> Exception:
    from labdata.exceptions import DownloadError

    return DownloadError("fastq-dump not found")


#: ``(stream, run, match)`` — the inputs on which ``probe_sra`` refuses with a ``RemoteError``. A
#: preview with no records ("streamed no reads"), a ``labdata`` download failure translated at the seam
#: ("could not stream reads"), and a run row with no ``run_accession`` (rejected before the stream is
#: ever touched, so no fake is installed).
PROBE_SRA_REFUSALS = [
    pytest.param(
        _FakeStream(_Preview(reads={}, read_lengths={}, n_spots_returned=0)),
        _ena_run(),
        "streamed no reads",
        id="empty-stream",
    ),
    pytest.param(
        _FakeStream(exc=_download_error()),
        _ena_run(),
        "could not stream reads",
        id="labdata-error",
    ),
    pytest.param(None, {"read_count": "1000"}, "no 'run_accession'", id="no-run-accession"),
]


@pytest.mark.parametrize("stream, run, match", PROBE_SRA_REFUSALS)
def test_probe_sra_refuses_with_a_remote_error(
    monkeypatch: pytest.MonkeyPatch,
    stream: _FakeStream | None,
    run: dict[str, object],
    match: str,
) -> None:
    if stream is not None:
        _patch_stream(monkeypatch, stream)
    with pytest.raises(RemoteError, match=match):
        sra.probe_sra(run)


# --------------------------------------------------------------------------- #
# resolve_package_runs — the one-library guard and its opt-in
# --------------------------------------------------------------------------- #


def test_resolve_package_runs_returns_the_runs_of_one_experiment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sra,
        "resolve_accession",
        lambda acc, check_reads=True: {
            "runs": [
                {"run_accession": "SRR1", "experiment_accession": SRX},
                {"run_accession": "SRR2", "experiment_accession": SRX},
            ]
        },
    )
    assert len(sra.resolve_package_runs(SRX)) == 2


def test_resolve_package_runs_refuses_a_multi_experiment_accession_and_names_the_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default stays a refusal, and it now tells the caller the opt-in exists.

    A refusal that does not name the flag leaves the caller believing the shape is unbuildable, which
    is how GSE207085 came to be packaged as ten one-cell fixtures.
    """
    monkeypatch.setattr(
        sra,
        "resolve_accession",
        lambda acc, check_reads=True: {
            "runs": [
                {"run_accession": "SRR1", "experiment_accession": "SRX_BULK"},
                {"run_accession": "SRR2", "experiment_accession": "SRX_GEX"},
                {"run_accession": "SRR3", "experiment_accession": "SRX_ATAC"},
            ]
        },
    )
    with pytest.raises(RemoteError, match="spans 3 experiments") as excinfo:
        sra.resolve_package_runs("GSE283483")
    assert "--multi-experiment" in str(excinfo.value)


@pytest.fixture
def stats_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count every per-run NCBI stats request the plate study provokes, ENA's inventory stubbed.

    The endpoint serves ONE run per request — a comma-joined or repeated ``acc`` answers for a single
    accession — so there is no batch to fall back on, and the only thing that keeps a 1440-run study
    affordable is not making the call at all. The count is the property; asserting it is what stops
    the pre-pass creeping back.
    """
    calls: list[str] = []

    def _counted(accession: str) -> remote.RunStatistics:
        calls.append(accession)
        return remote.RunStatistics(accession=accession)

    monkeypatch.setattr(remote, "run_statistics", _counted)
    monkeypatch.setattr(
        remote,
        "ena_filereport",
        lambda acc, **kwargs: [_plate_run(i) for i in range(PLATE_EXPERIMENTS)],
    )
    return calls


def test_resolving_a_plate_deposit_costs_no_per_run_stats_call(stats_calls: list[str]) -> None:
    """Both the refusal and the opt-in resolve the 1440-run study without one per-run round-trip."""
    with pytest.raises(RemoteError, match=f"spans {PLATE_EXPERIMENTS} experiments"):
        sra.resolve_package_runs(PLATE_STUDY)
    assert stats_calls == []

    runs = sra.resolve_package_runs(PLATE_STUDY, multi_experiment=True)
    assert [r["experiment_accession"] for r in runs] == [
        f"SRX{i:07d}" for i in range(PLATE_EXPERIMENTS)
    ]
    assert stats_calls == []


def test_a_plate_deposit_refusal_summarises_its_experiments(stats_calls: list[str]) -> None:
    """1440 SRX in an error string is not an error string anybody reads, so the listing is capped."""
    with pytest.raises(RemoteError) as excinfo:
        sra.resolve_package_runs(PLATE_STUDY)
    message = str(excinfo.value)
    assert "SRX0000000 (1 run)" in message
    assert f"and {PLATE_EXPERIMENTS - 8} more" in message
    assert len(message) < 600


# --------------------------------------------------------------------------- #
# build_fingerprint_sra — a loadable, reproducing package
# --------------------------------------------------------------------------- #


def test_build_fingerprint_sra_produces_a_package_that_reproduces_the_pinned_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_stream(monkeypatch, _FakeStream(_preview(SRR, {1: 28, 2: 94})))

    result = sra.build_fingerprint_sra([_ena_run()], workspace=tmp_path, reads=50)

    assert result.package.exists()
    assert len(result.manifest.files) == 2
    pinned = {p.basename: p.sha256 for p in result.manifest.files}
    assert pinned[f"{SRR}_1.fastq.gz"] == content_key_from_md5(MD5_1)
    assert pinned[f"{SRR}_2.fastq.gz"] == content_key_from_md5(MD5_2)

    # Load it back and re-probe the slices: the reconstructed observations carry the pinned identity,
    # so a fingerprint from an accession reproduces exactly like one from local FASTQs.
    loaded = load_fingerprint(result.package)
    _paths, probed = probed_from_fingerprint(loaded, max_reads=50)
    shas = {obs.file.sha256 for obs, _seqs in probed.values()}
    assert shas == {content_key_from_md5(MD5_1), content_key_from_md5(MD5_2)}


def test_build_fingerprint_sra_is_deterministic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_stream(monkeypatch, _FakeStream(_preview(SRR, {1: 28, 2: 94})))
    first = sra.build_fingerprint_sra([_ena_run()], workspace=tmp_path, reads=50)
    _patch_stream(monkeypatch, _FakeStream(_preview(SRR, {1: 28, 2: 94})))
    second = sra.build_fingerprint_sra([_ena_run()], workspace=tmp_path, reads=50)
    assert first.package.name == second.package.name  # same inputs -> same content-addressed stem


# --------------------------------------------------------------------------- #
# the CLI verbs
# --------------------------------------------------------------------------- #


def test_io_probe_sra_emits_one_observation_per_mate(monkeypatch: pytest.MonkeyPatch) -> None:
    import seqforge.cli.io as cli_io

    monkeypatch.setattr(
        cli_io, "resolve_accession", lambda acc, check_reads=True: {"runs": [_ena_run()]}
    )
    _patch_stream(monkeypatch, _FakeStream(_preview(SRR, {1: 28, 2: 94})))

    result = runner.invoke(app, ["io", "probe-sra", SRR, "--n-reads", "50"])

    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.stdout)
    assert payload["n_mates"] == 2
    assert {m["read_index"] for m in payload["mates"]} == {1, 2}
    assert all(m["ena_verified"] for m in payload["mates"])


def test_preflight_accession_builds_a_streamed_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        sra, "resolve_accession", lambda acc, check_reads=True: {"runs": [_ena_run()]}
    )
    _patch_stream(monkeypatch, _FakeStream(_preview(SRR, {1: 28, 2: 94})))

    result = runner.invoke(
        app, ["preflight", "--accession", SRX, "--reads", "50", "-C", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.stdout)
    assert payload["source"] == "sra-stream"
    assert payload["n_files"] == 2
    assert Path(payload["package"]).exists()


def test_preflight_refuses_both_files_and_accession_and_neither(tmp_path: Path) -> None:
    """``preflight`` takes files XOR an accession: both is exit 2 ("not both and not neither"), and so
    is neither."""
    fastq = tmp_path / "reads_1.fastq.gz"
    fastq.write_bytes(b"")
    both = runner.invoke(app, ["preflight", str(fastq), "--accession", SRR])
    assert both.exit_code == 2
    assert "not both and not neither" in both.output

    neither = runner.invoke(app, ["preflight"])
    assert neither.exit_code == 2


def test_preflight_accession_refuses_a_multi_experiment_series(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        sra,
        "resolve_accession",
        lambda acc, check_reads=True: {
            "runs": [
                {"run_accession": "SRR1", "experiment_accession": "SRX_BULK"},
                {"run_accession": "SRR2", "experiment_accession": "SRX_ATAC"},
            ]
        },
    )
    result = runner.invoke(app, ["preflight", "--accession", "GSE283483", "-C", str(tmp_path)])
    assert result.exit_code == 1
    assert "spans 2 experiments" in result.output
    assert "--multi-experiment" in result.output


def test_preflight_accession_packages_every_experiment_when_the_caller_opts_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A plate deposit is buildable as ONE package: two cells, two SRX, four sliced mates in it.

    The package the guard used to make unbuildable — and the shape a many-cell benchmark case needs,
    since ten one-cell packages prove nothing about the sample explosion.
    """
    monkeypatch.setattr(
        sra,
        "resolve_accession",
        lambda acc, check_reads=True: {"runs": [_plate_run(0), _plate_run(1)]},
    )
    _patch_stream(monkeypatch, _FakeStream(_preview(SRR, {1: 28, 2: 94})))

    result = runner.invoke(
        app,
        [
            "preflight",
            "--accession",
            PLATE_STUDY,
            "--multi-experiment",
            "--reads",
            "50",
            "-C",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.stdout)
    assert payload["n_files"] == 4
    assert {f["basename"] for f in payload["files"]} == {
        f"SRR{i:07d}_{index}.fastq.gz" for i in (0, 1) for index in (1, 2)
    }
    # No one SRX names a package spanning two of them, so the shared study does.
    assert Path(payload["package"]).name.startswith(f"{PLATE_SRP}-")
