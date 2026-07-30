"""Do the four ways of building an Observation agree on the same records?

`build_observation` is reached from four places, and each supplies the file's identity differently:

| caller | head from | identity from |
|---|---|---|
| `probe.probe_sample` | a local file | a bounded key over basename + size + ISIZE + head |
| `io.remote.probe_remote` | an HTTP range read | the provider md5, else a bounded key with no ISIZE |
| `io.sra.probe_sra` | a re-serialized spot stream | the ENA md5, else a synthetic whole-run address |
| `fingerprint.probed_from_fingerprint` | a head-slice | a **pin** — the identity of a file it is not reading |

Nothing asserted that they agree. `test_probe.py` covers the local path, `test_remote.py` and
`test_sra.py` each cover their own, and `test_fingerprint.py` compares two paths *against each other*
-- so all four could drift together, or one could drift alone, without a red test. "A URL resolves to
a library exactly as a local file does" is the claim `probe_remote`'s docstring makes and this is the
only place it is checked.

**The equivalence is narrower than "the observations match", and the point of this module is to say
exactly how narrow.** Three sets partition `Observation`, and the coverage test below fails if a new
field joins none of them -- so adding one forces a deliberate answer to "does this agree across
sources?" rather than letting it default to unexamined.

1. `FROM_THE_HEAD` -- computed from the records alone. Same records in, same value out, always.
2. `FROM_THE_WHOLE_FILE` -- the read-count extrapolation. It agrees *here* only because the fixture is
   read to EOF, which makes the count exact and the file's size irrelevant; under a budget that
   actually binds these two diverge by design, because a remote head has no gzip ISIZE to
   extrapolate from and an SRA stream's `size_bytes` is a spot-count proxy. The precondition is
   asserted, not assumed.
3. `FROM_THE_SOURCE` -- identity and read accounting. These *must* differ; that is what having four
   callers is for.
"""

from __future__ import annotations

import gzip
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from seqforge.cli import app
from seqforge.fingerprint.build import build_fingerprint
from seqforge.fingerprint.load import load_fingerprint, probed_from_fingerprint
from seqforge.io import sra
from seqforge.io.remote import probe_remote
from seqforge.models.observation import Observation
from seqforge.probe import content_key_from_md5, content_key_from_sra, probe_sample
from test_remote import _range_server  # tests/ is not a package; pytest puts it on sys.path

#: Computed from the records alone -- every source must agree, unconditionally.
FROM_THE_HEAD = {
    "per_cycle_composition",
    "segments",
    "read_length",
    "distinct_value_windows",
    "read_name",
    "quality_encoding",
    "n_rate",
    "gzip",
}
#: Agree only because this fixture is read to EOF; see the module docstring.
FROM_THE_WHOLE_FILE = {"estimated_total_reads", "est_method"}
#: Differ by design: `file` is the whole point of having four callers, and `probe` counts compressed
#: bytes, which depend on who compressed them (a local upload, a re-serialized stream, a slice).
FROM_THE_SOURCE = {"file", "probe"}

RUN = "SRR9999999"
URL = "https://ftp.x/vol1/SRR9999999_1.fastq.gz"
N_READS = 300


def _records() -> list[tuple[str, str, str]]:
    """One canonical record list: `(header, seq, qual)`, shared verbatim by all four sources.

    Headers are Illumina-shaped so `read_name` actually parses (a grammar that fails to parse is the
    same value everywhere and would prove nothing), and qualities span a range of ordinals so
    `quality_encoding` resolves to `phred33` rather than `unknown`. Sequences are a recurring
    16 bp barcode pool plus a 12 bp random tail, which gives `segments` two regions to find and
    `distinct_value_windows` a low ratio to distinguish from a high one.
    """
    rng = random.Random(20260730)
    pool = [
        "".join(rng.choice("ACGT") for _ in range(16)) for _ in range(40)
    ]  # recurring -> low ratio
    out: list[tuple[str, str, str]] = []
    for i in range(N_READS):
        seq = rng.choice(pool) + "".join(rng.choice("ACGT") for _ in range(12))
        qual = "".join(chr(35 + (j * 7 + i) % 39) for j in range(len(seq)))  # ords 35..73
        out.append((f"SIM:1:FC1:1:1101:{1000 + i}:{2000 + i} 1:N:0:ACGTACGT", seq, qual))
    return out


def _write_local(path: Path, records: list[tuple[str, str, str]]) -> None:
    with gzip.open(path, "wt") as fh:
        for name, seq, qual in records:
            fh.write(f"@{name}\n{seq}\n+\n{qual}\n")


# --------------------------------------------------------------------------- #
# the SRA stream seam, faked exactly as tests/test_sra.py fakes it
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


@dataclass
class _FakeStream:
    preview: _Preview
    calls: list[tuple[str, int, bool]] = field(default_factory=list)

    def __call__(self, run_accession: str, *, n_spots: int, include_technical: bool) -> _Preview:
        self.calls.append((run_accession, n_spots, include_technical))
        return self.preview


def _four_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, tuple[Observation, list[str]]]:
    """The same records, fingerprinted through all four callers of `build_observation`."""
    records = _records()
    local = tmp_path / "reads_1.fastq.gz"
    _write_local(local, records)
    blob = local.read_bytes()

    # 1. local file
    local_obs, local_seqs = probe_sample(local)

    # 2. HTTP range read, serving the very same gzip bytes
    monkeypatch.setattr("seqforge.io.remote.requests.get", _range_server({URL: blob}))
    remote_obs, remote_seqs = probe_remote(URL, md5="a" * 32)

    # 3. an SRA spot stream, re-serialized through `records_to_gz_bytes`
    preview = _Preview(
        reads={
            1: [
                _Rec(f"@{h}".encode(), s.encode(), b"+", q.encode())
                for h, s, q in records  # the identical records, as the stream would yield them
            ]
        },
        read_lengths={1: len(records[0][1])},
        n_spots_returned=len(records),
    )
    import labdata

    monkeypatch.setattr(labdata, "stream_run_reads", _FakeStream(preview), raising=False)
    mates = sra.probe_sra({"run_accession": RUN}, n_reads=N_READS * 2)
    assert len(mates) == 1
    sra_obs, sra_seqs = mates[0].observation, mates[0].seqs

    # 4. a fingerprint slice standing in for the original, identity from the pin
    result = build_fingerprint([local], workspace=tmp_path / "ws", reads=N_READS * 2, name="ds")
    loaded = load_fingerprint(result.staging)
    _paths, probed = probed_from_fingerprint(loaded)
    assert len(probed) == 1
    pin_obs, pin_seqs = next(iter(probed.values()))

    return {
        "local": (local_obs, local_seqs),
        "remote": (remote_obs, remote_seqs),
        "sra": (sra_obs, sra_seqs),
        "fingerprint": (pin_obs, pin_seqs),
    }


def test_the_three_field_sets_cover_every_observation_field() -> None:
    """A new Observation field must join one of the three sets -- silence is not an answer.

    Without this, a field added tomorrow is neither asserted equal nor declared source-dependent: it
    escapes the contract by default, which is exactly how the four callers drifted apart unnoticed in
    the first place.
    """
    declared = FROM_THE_HEAD | FROM_THE_WHOLE_FILE | FROM_THE_SOURCE
    actual = set(Observation.model_fields)

    assert not (actual - declared), (
        f"Observation gained {sorted(actual - declared)} — decide which set it belongs to: "
        "FROM_THE_HEAD (agrees always), FROM_THE_WHOLE_FILE (agrees only when read to EOF), "
        "or FROM_THE_SOURCE (differs by design)."
    )
    assert not (declared - actual), (
        f"declared but gone from Observation: {sorted(declared - actual)}"
    )


def test_every_source_reads_the_same_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The precondition for everything below: four readers, one set of sequences."""
    four = _four_observations(tmp_path, monkeypatch)
    expected = [seq for _h, seq, _q in _records()]

    for name, (_obs, seqs) in four.items():
        assert seqs == expected, f"{name} did not read the fixture's records"


def test_the_head_derived_signals_agree_across_all_four_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same records, same signals — whether they arrived from disk, a URL, a stream, or a slice."""
    four = _four_observations(tmp_path, monkeypatch)
    baseline = four["local"][0]

    for name, (obs, _seqs) in four.items():
        for field_name in sorted(FROM_THE_HEAD):
            assert getattr(obs, field_name) == getattr(baseline, field_name), (
                f"{name} disagrees with the local probe on {field_name!r}, but both read the same "
                "records — a signal must not depend on where its bytes came from."
            )


def test_the_read_estimate_agrees_only_because_nothing_hit_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`estimated_total_reads` agrees here for a stated reason, not as a general property.

    Read to EOF, the sampled count IS the total and no size or ISIZE is consulted. Assert that
    precondition explicitly: if a future fixture grows past the budget this test must fail loudly
    rather than quietly compare two extrapolations that were never meant to match.
    """
    four = _four_observations(tmp_path, monkeypatch)

    for name, (obs, _seqs) in four.items():
        assert obs.probe.n_reads_sampled == N_READS, f"{name} did not read the fixture to EOF"
        assert obs.estimated_total_reads == N_READS, f"{name} extrapolated instead of counting"
        assert obs.est_method == "isize", f"{name} fell back to the compressed ratio"


def test_each_source_names_the_file_its_own_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`file` is what the four callers exist to differ on — pin *how*, so a refactor cannot blur it.

    Four addresses over identical records is not a bug: a content address is a NAME, and these are
    four different naming authorities. The local key folds in the gzip ISIZE the others cannot reach;
    the remote adopts the provider md5 outright; SRA has no hosted-byte identity at all and derives a
    synthetic one; the fingerprint copies a pin describing a file it is not reading.
    """
    four = _four_observations(tmp_path, monkeypatch)
    obs = {name: o for name, (o, _s) in four.items()}

    assert obs["remote"].file.sha256 == content_key_from_md5("a" * 32)
    assert obs["sra"].file.sha256 == content_key_from_sra(
        RUN, 1, spot_count=N_READS, read_length=28
    )
    # The pin describes the ORIGINAL, so it matches the local probe exactly...
    assert obs["fingerprint"].file.sha256 == obs["local"].file.sha256
    assert obs["fingerprint"].file.size_bytes == obs["local"].file.size_bytes
    # ...while local_uri points at the slice actually read, which is a different file on disk.
    assert obs["fingerprint"].file.local_uri != obs["local"].file.local_uri

    # Only a local read can stage a path; the other two never touch the filesystem.
    assert obs["local"].file.local_uri == str(tmp_path / "reads_1.fastq.gz")
    assert obs["remote"].file.local_uri is None
    assert obs["sra"].file.local_uri is None

    # Four distinct addresses, three distinct authorities (the pin re-uses the local one).
    assert len({o.file.sha256 for o in obs.values()}) == 3


def test_the_probe_accounting_reflects_who_compressed_the_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decompressed accounting is a property of the records; compressed accounting is not.

    `bytes_read` counts the record text, so it agrees across sources. `compressed_bytes_read` counts
    what was inflated, and the local upload, the re-serialized SRA stream and the fingerprint slice
    were compressed by different writers — so it legitimately does not.
    """
    four = _four_observations(tmp_path, monkeypatch)
    obs = {name: o for name, (o, _s) in four.items()}
    baseline = obs["local"].probe

    for name, o in obs.items():
        assert o.probe.bytes_read == baseline.bytes_read, f"{name} inflated a different record text"
        assert o.probe.n_reads_sampled == baseline.n_reads_sampled
        assert o.probe.tool_version == baseline.tool_version

    # Same bytes over the wire as on disk: the range read inflates the identical member.
    assert obs["remote"].probe.compressed_bytes_read == baseline.compressed_bytes_read
    # A different writer produced these, so the compressed size is its own.
    assert obs["sra"].probe.compressed_bytes_read != baseline.compressed_bytes_read


def test_the_budget_is_stamped_identically_when_it_is_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`params_hash` records the budget a probe ran under, and must not vary by source.

    It is currently recomputed inside `build_observation` from parameters the caller supplies a second
    time, rather than from the read that actually happened — so a caller that passes a budget
    different from the one it read under stamps a hash that lies. Nothing else in the suite would
    notice: `params_hash` is written in one place and read nowhere.
    """
    four = _four_observations(tmp_path, monkeypatch)
    stamped = {name: o.probe.params_hash for name, (o, _s) in four.items()}

    # local, remote and the fingerprint all ran the default budget.
    assert stamped["local"] == stamped["remote"] == stamped["fingerprint"]
    # SRA was handed an explicit read budget above, and says so rather than claiming the default.
    assert stamped["sra"] != stamped["local"]


def _probe_via_cli(path: Path) -> dict[str, Any]:
    """Drive the `seqforge probe` verb, which no test has ever executed."""
    result = CliRunner().invoke(app, ["probe", str(path)])
    assert result.exit_code == 0, result.output
    return dict(json.loads(result.stdout))


def test_the_probe_cli_verb_emits_the_same_observation_as_the_library(tmp_path: Path) -> None:
    """R6: the CLI is the API. A verb no test runs is a verb that can rot silently.

    `docs/getting-started.md` once told a reader to run `seqforge probe` after it had been removed
    from the skills, which is the failure this closes: the verb is exercised end to end, and its JSON
    is checked to be the Observation rather than merely well-formed.
    """
    path = tmp_path / "reads_1.fastq.gz"
    _write_local(path, _records())

    emitted = _probe_via_cli(path)
    direct, _seqs = probe_sample(path)

    assert emitted == direct.model_dump(mode="json")
