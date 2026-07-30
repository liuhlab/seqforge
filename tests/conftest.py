"""Shared test support — the seam ``tests/`` never had.

Without a conftest every file re-implemented its own setup, so the same fact was re-proved dozens of
times and nothing could be tuned in one place. What lives here:

* :func:`_no_wiring_gate` — the autouse stub that stops ``compose`` spawning ``snakemake -n -p``
  in every test that happens to compose. One test opts back in per workflow module.
* :func:`write_fastq_gz` — the one synthetic-FASTQ writer (it was copied verbatim into 13 files).
  Its ``compresslevel`` default is **1**, not zlib's 9: the suite writes hundreds of throwaway
  fixtures whose compressed *size* nothing reads. A fixture that pins compressed bytes must pass its
  own level explicitly — see ``test_probe.py``'s ``_value_stable_fixture``, which owns its
  compressor because it owns a literal ``size_bytes``.
* :func:`registry_for` — a synthetic :class:`OnlistRegistry` backed by the generator's own pools.
* :data:`synth_10x_v3` — a **session-scoped** read-only FASTQ directory and the ``(manifest,
  registry)`` built from it, for the one shape most of the suite wants.

**What may be shared is immutable products only.** The manifest, the registry and a directory
nothing writes into are safe; a *workspace* never is. ``seqforge/cache/`` makes resume implicit
(rule R5), so a shared workspace would let a later test collect a cached ``Observation`` and pass
for the wrong reason. Every test still composes into its own ``tmp_path``.
"""

from __future__ import annotations

import gzip
import re
import types
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from seqforge import __version__, kb
from seqforge.io import OnlistRegistry
from seqforge.manifest import ExperimentInputs, fill_manifest
from seqforge.models.dataset import DatasetManifest, SampleGroup
from seqforge.models.evidenced import EvidencedTaxid
from seqforge.probe import probe_file
from seqforge.resolve import resolve_dataset

#: ``(header, sequence, quality)`` — what a FASTQ record is, once the ``@`` and ``+`` are stripped.
Record = tuple[str, str, str]

#: Cheap by default; see the module docstring. ~5-8s across the suite, and no claim depends on it.
DEFAULT_COMPRESSLEVEL = 1


# --------------------------------------------------------------------------- #
# the wiring gate: paid once per workflow module, not once per compose
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_wiring_gate(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``wiring_gate`` unless a test asks for the real one.

    ``run_wiring_gate: bool = True`` makes a 1.49s ``snakemake -n -p`` subprocess the default, so
    every test that composes anything re-proves a fact about a hand-written ``.smk`` module rather
    than about its own inputs. It was spawned ~41 times and only four of those spawns had a consumer
    — all four on ``map/starsolo``, which left ``map/star`` and ``map/chromap`` spawned dozens of
    times and asserted on never.

    Patching the module ATTRIBUTE rather than flipping the default is what reaches the CLI-driven
    callers: ``compose.core`` imports ``wiring_gate`` inside the function body, so every path — the
    ``run`` verb, the report fixture, the fingerprint spine — resolves it here at call time.
    """
    if "real_wiring_gate" in request.fixturenames:
        return
    monkeypatch.setattr("seqforge.compose.gates.wiring_gate", lambda pipeline_dir, plan: "skip")


@pytest.fixture
def real_wiring_gate() -> None:
    """Opt back in. Marks a test as one that pays ~1.5s of ``snakemake -n -p`` deliberately."""
    return None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Anything that asks for the real wiring gate spawns ``snakemake``, so it IS ``external``.

    Derived from the fixture rather than written down, for the same reason the KB collects its own
    test ids: a hand-maintained list of external tests is a list that goes stale silently, and the
    staleness shows up as ``test-fast`` quietly spawning a subprocess nobody meant it to.
    """
    for item in items:
        if "real_wiring_gate" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.external)


def write_fastq_gz(
    path: Path,
    records: Sequence[str] | Sequence[Record],
    *,
    prefix: str = "SIM",
    desc: str = "",
    compresslevel: int = DEFAULT_COMPRESSLEVEL,
) -> None:
    """Write ``records`` as gzipped FASTQ.

    ``records`` is either bare sequences — headered ``@<prefix>:<i>`` with an all-``I`` quality
    string, the shape almost every caller wants — or full ``(header, seq, qual)`` triples for the
    fixtures that care about read names or quality encodings. ``desc`` appends a space-separated
    description to a generated header (a real Illumina header has one; a slicer must preserve it).

    This is ``gzip.open`` semantics deliberately, not the reproducible writer in
    ``kb.generate``: the byte-stable one omits the filename from the gzip header, which moves the
    compressed size, and ``test_probe.py`` pins that size against a literal.
    """
    with gzip.open(path, "wt", compresslevel=compresslevel) as fh:
        for i, record in enumerate(records):
            if isinstance(record, str):
                name = f"{prefix}:{i} {desc}" if desc else f"{prefix}:{i}"
                header, seq, qual = name, record, "I" * len(record)
            else:
                header, seq, qual = record
            fh.write(f"@{header}\n{seq}\n+\n{qual}\n")


def registry_for(spec: kb.Spec, *, seed: int = 0, pool_size: int = 64) -> OnlistRegistry:
    """A synthetic registry whose registry-names are backed by the generator's barcode pools.

    Offline: the reads a test writes are drawn from these pools, so the whitelist signal fires
    without touching the shipped multi-million-barcode onlists.
    """
    pools = kb.build_pools(spec, seed=seed, pool_size=pool_size)
    reg = OnlistRegistry(offline=True)
    for alias, ref in spec.onlists.items():
        if alias in pools:
            reg.register_synthetic(ref.registry, pools[alias])
    return reg


def range_server(blobs: dict[str, bytes], *, status: int = 206) -> Callable[..., object]:
    """A fake ``requests.get`` that serves a 206 Range slice of ``blobs[url]`` with a Content-Range.

    Honors ``Range: bytes=0-N`` exactly as ENA does, so a bounded read returns a bounded prefix and the
    206's ``Content-Range: .../TOTAL`` carries the true file size. ``status=200`` simulates a host that
    ignores Range and hands back the whole file — the case ``_range_get`` must refuse.

    It lives here, not in ``test_remote.py``, because ``test_observation_sources.py`` also needs it
    and a test file importing another test file's private helper is a seam being routed around.
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


@dataclass(frozen=True)
class SynthDataset:
    """A built synthetic dataset: where its reads are, and what resolving them produced.

    Everything on it is read-only. ``dir`` is a session directory no test may write into; the
    manifest and registry are immutable products. A test that needs to vary the manifest takes a
    ``model_copy``, and a test that needs a workspace uses its own ``tmp_path``.
    """

    tech: str
    dir: Path
    paths: tuple[Path, ...]
    manifest: DatasetManifest
    registry: OnlistRegistry


def build_synth_dataset(
    directory: Path,
    tech: str,
    *,
    keys: tuple[str, ...] | None = None,
    n: int = 600,
    seed: int = 0,
    taxid: int = 559292,
) -> SynthDataset:
    """Write synthetic reads for ``tech``, resolve them, and fill a manifest — the whole front half.

    ``keys`` defaults to the spec's own read ids. Passing ``("R1", "R2")`` unconditionally silently
    pins a caller to 10x and bulk naming and makes splitseq (whose reads are ``cdna``/``bc``) raise
    ``KeyError: 'R1'`` rather than compose; deriving the default from the spec is what lets a test
    iterate the KB.
    """
    spec = kb.load_spec(tech)
    reg = registry_for(spec)
    reads = kb.generate_reads(spec, n=n, seed=seed)
    paths = []
    for k in keys or tuple(r.id for r in spec.reads):
        p = directory / f"s_{k}.fastq.gz"
        write_fastq_gz(p, reads[k])
        paths.append(p)
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    manifest = fill_manifest(
        result=out.result,
        spec=spec,
        observations=[probe_file(p) for p in paths],
        registry=reg,
        experiment=ExperimentInputs(
            organism=EvidencedTaxid(value=taxid, basis="user_confirmed", rung=0),
            accessions=["PRJNA1027859"],
            samples=[SampleGroup(sample_id="s1", file_uris=[p.name for p in paths])],
        ),
        seqforge_version=__version__,
    )
    return SynthDataset(
        tech=tech, dir=directory, paths=tuple(paths), manifest=manifest, registry=reg
    )


@pytest.fixture(scope="session")
def synth_10x_v3(tmp_path_factory: pytest.TempPathFactory) -> SynthDataset:
    """The suite's default shape: a resolved, filled ``10x-3p-gex-v3`` pair of reads.

    Built once per session. It replaces ~45 identical rebuilds that each cost a resolve + two probes
    to re-derive the same manifest.
    """
    return build_synth_dataset(tmp_path_factory.mktemp("synth-10x-v3"), "10x-3p-gex-v3")
