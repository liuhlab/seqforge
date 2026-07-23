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
from seqforge.io import sra
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


# --------------------------------------------------------------------------- #
# fastq_targets_meta — url/md5/size join
# --------------------------------------------------------------------------- #


def test_fastq_targets_meta_joins_url_md5_and_size_sorted_by_url() -> None:
    # fastq_bytes is aligned to the UNSORTED fastq_ftp; the join must re-associate by url after sort.
    meta = fastq_targets_meta(
        {
            "fastq_ftp": f"host/{SRR}_2.fastq.gz;host/{SRR}_1.fastq.gz",
            "fastq_md5": f"{MD5_2};{MD5_1}",
            "fastq_bytes": "222;111",
        }
    )
    assert meta == [
        (f"https://host/{SRR}_1.fastq.gz", MD5_1, 111),
        (f"https://host/{SRR}_2.fastq.gz", MD5_2, 222),
    ]


def test_fastq_targets_meta_is_empty_on_a_url_md5_mismatch() -> None:
    assert fastq_targets_meta({"fastq_ftp": "host/a;host/b", "fastq_md5": MD5_1}) == []


def test_fastq_targets_meta_defaults_size_to_zero_when_bytes_missing() -> None:
    meta = fastq_targets_meta({"fastq_ftp": f"host/{SRR}_1.fastq.gz", "fastq_md5": MD5_1})
    assert meta == [(f"https://host/{SRR}_1.fastq.gz", MD5_1, 0)]


# --------------------------------------------------------------------------- #
# probe_sra — content-address precedence
# --------------------------------------------------------------------------- #


def test_probe_sra_adopts_the_ena_md5_identity_when_the_mirror_is_faithful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeStream(_preview(SRR, {1: 28, 2: 94}))
    _patch_stream(monkeypatch, fake)

    mates = sra.probe_sra(_ena_run(), n_reads=50)

    assert [m.read_index for m in mates] == [1, 2]
    assert all(m.ena_verified for m in mates)
    # read index 1 -> the _1 file (both sort ascending); its md5 IS the content-address.
    assert mates[0].observation.file.sha256 == content_key_from_md5(MD5_1)
    assert mates[0].observation.file.size_bytes == 111
    assert mates[0].basename == f"{SRR}_1.fastq.gz"
    assert mates[1].observation.file.sha256 == content_key_from_md5(MD5_2)
    assert mates[1].observation.file.size_bytes == 222
    # technical reads are kept, and the stream is asked for n_reads spots.
    assert fake.calls == [(SRR, 50, True)]


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


def test_probe_sra_builds_real_observations_with_chemistry_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stream(monkeypatch, _FakeStream(_preview(SRR, {1: 28, 2: 94})))

    mates = sra.probe_sra(_ena_run(), n_reads=50)

    obs = mates[0].observation
    assert obs.read_length.mode == 28
    assert obs.probe.n_reads_sampled == 50
    assert mates[0].seqs  # the sampled sequences resolve needs
    assert obs.file.local_uri is None  # a stream has no local path


def test_probe_sra_raises_on_an_empty_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stream(monkeypatch, _FakeStream(_Preview(reads={}, read_lengths={}, n_spots_returned=0)))
    with pytest.raises(RemoteError, match="streamed no reads"):
        sra.probe_sra(_ena_run())


def test_probe_sra_translates_a_labdata_error_into_a_remote_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from labdata.exceptions import DownloadError

    _patch_stream(monkeypatch, _FakeStream(exc=DownloadError("fastq-dump not found")))
    with pytest.raises(RemoteError, match="could not stream reads"):
        sra.probe_sra(_ena_run())


def test_probe_sra_requires_a_run_accession() -> None:
    with pytest.raises(RemoteError, match="no 'run_accession'"):
        sra.probe_sra({"read_count": "1000"})


# --------------------------------------------------------------------------- #
# resolve_single_experiment_runs — the one-library guard
# --------------------------------------------------------------------------- #


def test_resolve_single_experiment_returns_the_runs_of_one_experiment(
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
    srx, runs = sra.resolve_single_experiment_runs(SRX)
    assert srx == SRX
    assert len(runs) == 2


def test_resolve_single_experiment_refuses_a_multi_experiment_accession(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    with pytest.raises(RemoteError, match="spans 3 experiments"):
        sra.resolve_single_experiment_runs("GSE283483")


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


def test_preflight_refuses_both_files_and_accession(tmp_path: Path) -> None:
    fastq = tmp_path / "reads_1.fastq.gz"
    fastq.write_bytes(b"")
    result = runner.invoke(app, ["preflight", str(fastq), "--accession", SRR])
    assert result.exit_code == 2
    assert "not both and not neither" in result.output


def test_preflight_refuses_neither_files_nor_accession() -> None:
    result = runner.invoke(app, ["preflight"])
    assert result.exit_code == 2


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
