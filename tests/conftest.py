"""Shared test support — the seam ``tests/`` never had.

Without a conftest every file re-implemented its own setup, so the same fact was re-proved dozens of
times and nothing could be tuned in one place. What lives here:

* :func:`_no_wiring_gate` — the autouse stub that stops ``compose`` spawning ``snakemake -n -p``
  in every test that happens to compose. One test opts back in per workflow module.
* :func:`dry_run` — the same subprocess, returning the PLAN TEXT rather than a four-character
  verdict. It lives here because the ``external`` marker is derived from fixture names, so a
  module-local spawner is a spawn the marker cannot see.
* :func:`write_fastq_gz` — the one synthetic-FASTQ writer (it was copied verbatim into 13 files).
  Its ``compresslevel`` default is **1**, not zlib's 9: the suite writes hundreds of throwaway
  fixtures whose compressed *size* nothing reads. A fixture that pins compressed bytes must pass its
  own level explicitly — see ``test_probe.py``'s ``_value_stable_fixture``, which owns its
  compressor because it owns a literal ``size_bytes``.
* :func:`registry_for` — a synthetic :class:`OnlistRegistry` backed by the generator's own pools.
* :data:`synth_10x_v3`, :data:`synth_bulk_pe`, :data:`synth_splitseq` — **session-scoped** read-only
  FASTQ directories and the ``(manifest, registry)`` built from each, for the three shapes the suite
  keeps rebuilding: barcoded, no-barcode, and complex-geometry.
* :data:`kb_probes` — every KB spec's own reads, probed once
  (``(spec id, read set) -> [WindowProbe]``).
* :data:`src_trees` — every ``.py`` under ``src/seqforge``, parsed once (``path -> ast.Module``).
* :func:`gate_that_must_not_run` — un-stub the gate for the one test that proves it is NOT reached.
  Not ``external``: it spawns nothing, and a counter makes that a mechanism rather than a promise.
* :func:`pytest_cmdline_main` — a bare ``pytest`` runs the whole suite ACROSS CORES, because
  nobody should have to remember a flag to avoid a minute of waiting. A run that names a path, a
  keyword or a marker is left exactly as it was typed.

**What may be shared is immutable products only.** The manifest, the registry and a directory
nothing writes into are safe; a *workspace* never is. ``seqforge/cache/`` makes resume implicit, so
a shared workspace would let a later test collect a cached ``Observation`` and pass for the wrong
reason. Every test still composes into its own ``tmp_path``.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import re
import sys
import types
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest

from seqforge import __version__, kb
from seqforge.compose import core as compose_core
from seqforge.compose.core import ComposePlan
from seqforge.io import OnlistRegistry
from seqforge.manifest import ExperimentInputs, ProcessingInputs, fill_manifest, fill_processing
from seqforge.manifest.hash import dataset_content_hash
from seqforge.models.dataset import DatasetManifest, FileInventoryItem, SampleGroup
from seqforge.models.evidenced import EvidencedTaxid
from seqforge.models.processing import ProcessingManifest
from seqforge.probe import probe_file
from seqforge.resolve import resolve_dataset
from seqforge.resolve.engine import Hypothesis
from seqforge.resolve.window import WindowProbe

#: ``(header, sequence, quality)`` — what a FASTQ record is, once the ``@`` and ``+`` are stripped.
Record = tuple[str, str, str]

#: Cheap by default; see the module docstring. ~5-8s across the suite, and no claim depends on it.
DEFAULT_COMPRESSLEVEL = 1

#: What :data:`kb_probes` hands back: ``(KB spec id, read set)`` -> the probes a scorer sees for that
#: technology in that sequencing configuration. The maximal set is keyed ``full``, so ``probes[id,
#: "full"]`` is what a caller asking "this spec's own reads" wants, and ``probes["bulk-rnaseq", "se"]``
#: is a genuinely single-end deposit of the same chemistry.
KbProbes = dict[tuple[str, str], list[WindowProbe]]

#: What :data:`src_trees` hands back: every ``.py`` under ``src/seqforge`` -> its parsed AST.
SrcTrees = dict[Path, ast.Module]


# --------------------------------------------------------------------------- #
# the whole suite never runs on one core
# --------------------------------------------------------------------------- #

#: The worker cap for a full-suite run, and it is a MEASURED number — see the comment on the ``test``
#: task in ``pyproject.toml``, which carries the sweep. Uncapped ``auto`` is slower on a big box than
#: this, because each worker pays the interpreter import and rebuilds every session-scoped fixture.
FULL_SUITE_WORKERS = 12

#: Distribution mode for an injected run. It behaves like the default for unmarked tests and groups
#: only the tests carrying an ``xdist_group`` mark, so a module with an expensive session fixture can
#: opt into building it once instead of once per worker.
FULL_SUITE_DIST = "loadgroup"

#: Options that mean "this invocation is not the whole suite", by pytest's own ``dest`` names. Any
#: one of them present and the args are left exactly as typed.
_SELECTORS = (
    "keyword",  # -k: a subset by name
    "markexpr",  # -m: a subset by marker
    "lf",  # --last-failed: the handful that just broke
    "failedfirst",  # --failed-first: same handful, reordered
    "maxfail",  # -x: unreliable across workers -- the ones in flight keep going
    "usepdb",  # --pdb: an interactive session is a session of one
)


def _is_the_whole_suite(config: pytest.Config) -> bool:
    """Is this invocation the entire suite — no path argument, or a path that IS the test root?

    ``args_source`` is pytest's own answer to "did a human name a path": it is ``ARGS`` only when one
    was given, and the configured ``testpaths`` otherwise. Spelling ``pytest tests`` by hand is the
    same run as spelling nothing, so that shape counts too; a node id (it carries ``::``) never does.
    """
    if config.args_source is not pytest.Config.ArgsSource.ARGS:
        return True
    roots = {str(p).rstrip("/") for p in config.getini("testpaths")}
    return bool(roots) and {a.rstrip("/") for a in config.args} == roots


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    """Give a full-suite run its workers, so no one has to remember to ask for them.

    The suite takes ~56s on one core and ~13s across twelve, and the whole difference used to hang on
    an agent typing the parallel verb rather than the bare one. A guard that REFUSED the serial run
    would still cost a retry, so this does not refuse anything: it fills in the flags the parallel
    verb passes explicitly, and only when the invocation is the whole suite.

    A targeted run is left alone on purpose — spinning up twelve workers to run three tests costs
    more than it saves, and the selector is what makes a targeted run targeted.

    ``pytest_load_initial_conftests`` is the hook this would ideally use (it can rewrite ``args``
    before anything parses them), but pytest does not call it for a ``conftest.py`` at all — only for
    installed plugins. This one is called, and early enough: xdist turns ``numprocesses`` into
    ``tx`` in its own ``pytest_cmdline_main``, and ``tryfirst`` in a conftest registered after it runs
    first. Returning ``None`` lets the rest of the chain run, which is what actually starts the
    session.

    The worker guard is not optional. An xdist worker re-enters this same hook with ``numprocesses``
    reset to ``None``, so without it every worker would fork twelve more.
    """
    if hasattr(config, "workerinput"):
        return
    if not _is_the_whole_suite(config):
        return
    if any(getattr(config.option, dest, None) for dest in _SELECTORS):
        return
    if config.option.capture == "no":  # -s: output a human is watching, and workers interleave it
        return
    if getattr(config.option, "numprocesses", None) is not None:
        return  # an explicit -n, including -n 0, is a decision -- honour it
    if config.pluginmanager.is_blocked("xdist"):
        return  # -p no:xdist is the same decision, spelled the other way
    if not config.pluginmanager.hasplugin("xdist"):
        print(
            "conftest: pytest-xdist is not installed, so this full-suite run is serial and will "
            "take about a minute instead of about ten seconds. Install it, or name a single test "
            "file to keep the loop short.",
            file=sys.stderr,
        )
        return

    config.option.numprocesses = "auto"
    config.option.maxprocesses = FULL_SUITE_WORKERS
    if config.option.dist == "no":
        config.option.dist = FULL_SUITE_DIST


# --------------------------------------------------------------------------- #
# the wiring gate: paid once per workflow module, not once per compose
# --------------------------------------------------------------------------- #


#: Fixture names that mean "do not stub ``wiring_gate`` for this test".
_UNSTUBS_THE_GATE = frozenset({"real_wiring_gate", "gate_that_must_not_run"})

#: Fixture names that mean "this test spawns ``snakemake``" — which is what ``external`` is ABOUT.
#: The two sets are deliberately different; see :func:`pytest_collection_modifyitems`.
_SPAWNS_SNAKEMAKE = frozenset({"real_wiring_gate", "dry_run"})


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
    if _UNSTUBS_THE_GATE & set(request.fixturenames):
        return
    monkeypatch.setattr("seqforge.compose.gates.wiring_gate", lambda pipeline_dir, plan: "skip")


@pytest.fixture
def real_wiring_gate() -> None:
    """Opt back in. Marks a test as one that pays ~1.5s of ``snakemake -n -p`` deliberately."""
    return None


@pytest.fixture
def gate_that_must_not_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Un-stub ``wiring_gate`` for a test whose whole claim is that the gate never runs.

    Needed because ``real_wiring_gate`` used to mean two things at once — "do not stub the gate" and
    "spawn ``snakemake``" — and ``external`` is only ever about the second. A test that un-stubs the
    gate and then passes ``run_wiring_gate=False`` spawns nothing and has no business being dropped
    from ``test-fast``.

    The "and then never runs it" half is a **mechanism, not a promise**: the real gate is installed
    behind a counter, and reaching it turns the run red at teardown. Without that, this fixture would
    be a second hand-written claim about what a test does — the exact thing deriving the marker from
    fixture names exists to avoid.

    Precisely: pytest renders a teardown failure as an ERROR against the test rather than as a FAIL,
    so the test itself still reads "passed" in the dot line. The run is non-zero either way, which is
    what matters; do not go looking for a red F.
    """
    from seqforge.compose import gates

    real = gates.wiring_gate
    calls: list[Path] = []

    def counted(pipeline_dir: Path, plan: ComposePlan) -> str:
        calls.append(pipeline_dir)
        return real(pipeline_dir, plan)

    monkeypatch.setattr("seqforge.compose.gates.wiring_gate", counted)
    yield
    assert not calls, (
        f"this test takes `gate_that_must_not_run` but reached the real gate ({calls}); it spawns "
        "`snakemake` and must take `real_wiring_gate` instead, so it is marked `external`"
    )


class DryRun(Protocol):
    """What :func:`dry_run` hands back. A Protocol, because the ``plan`` argument is OPTIONAL and
    ``Callable[...]`` cannot say so — spelling it ``Callable[..., str]`` erases both parameters and
    leaves the three call sites with no signature to check at all."""

    def __call__(self, directory: Path, plan: ComposePlan | None = None) -> str: ...


@pytest.fixture
def dry_run() -> DryRun:
    """``snakemake -n -p`` over a composed run directory, returning the PLAN TEXT.

    A *fixture*, not a module-local helper, and that is the whole point. ``wiring_gate`` returns a
    four-character verdict while holding the plan text, so every test that wanted the plan re-spawned
    through a private ``_dry_run`` in the compose tests — invisible to
    :func:`pytest_collection_modifyitems`, which is how two tests that shell out to ``snakemake`` came
    to be selected by ``test-fast``. Requesting this fixture IS the spawn, so the marker follows.

    Pass ``plan`` to run against a throwaway ``_replica`` (source inputs stood in, tree removed
    afterwards) — the gate's own arrangement. Omit it to run against ``directory`` exactly as the test
    left it, for the tests that mutate ``units.tsv`` and stage their own inputs.
    """
    import shutil
    import subprocess

    from seqforge.compose.gates import _replica

    def run(directory: Path, plan: ComposePlan | None = None) -> str:
        target = _replica(directory, plan) if plan is not None else directory
        try:
            proc = subprocess.run(
                ["snakemake", "-d", str(target), "-s", str(target / "Snakefile"), "-n", "-p"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            assert proc.returncode == 0, proc.stderr
            return proc.stdout + proc.stderr
        finally:
            if plan is not None:
                shutil.rmtree(target, ignore_errors=True)

    return run


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """A test is ``external`` iff it SPAWNS a binary — which is a fixture it took, not a claim.

    Derived from fixture names rather than written down, for the same reason the KB collects its own
    test ids: a hand-maintained list of external tests goes stale silently, and the staleness shows up
    as ``test-fast`` quietly spawning a subprocess nobody meant it to.

    ``_SPAWNS_SNAKEMAKE`` is deliberately not ``_UNSTUBS_THE_GATE``. Keying on "asked for the real
    gate" was close enough to be wrong in both directions: ``_dry_run`` spawned ``snakemake`` with no
    fixture at all (so ``test-fast`` hard-failed on a machine without it, which is the exact thing
    ``test-fast`` exists to avoid), while a test that un-stubs the gate only to prove it is never
    called was dropped from ``test-fast`` for a subprocess it does not run.
    """
    for item in items:
        if _SPAWNS_SNAKEMAKE & set(getattr(item, "fixturenames", ())):
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


def real_cbs(n: int, onlist: str = "3M-february-2018") -> list[str]:
    """``n`` real barcodes from a SHIPPED whitelist (default ``3M-february-2018``, the 3' v3 list),
    spread across the sorted list so early bases stay diverse.

    For the paths that drive the REAL registry rather than a synthetic one: random CBs would miss the
    shipped whitelist, and F1b would then refuse the v3 run as barcode-absent. Real CBs make it hit,
    as real data does. Naming the list is what lets a test put a library on the *right* whitelist and
    then check that every other shipped list — not just the ones a synthetic registry bothered to
    register — declines it.
    """
    from seqforge.io import DEFAULT_REGISTRY
    from seqforge.io.onlist import PackedOnlist, unpack_barcodes

    packed = DEFAULT_REGISTRY.packed(onlist)
    step = max(1, packed.codes.shape[0] // n)
    return unpack_barcodes(PackedOnlist(packed.width, packed.codes[::step][:n]))


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

    A chemistry the KB says the BYTES cannot settle gets the mechanism the KB names for it. 10x 3' v2
    and 10x 5' v1/v2 share the 26 bp geometry AND the ``737K-august-2016`` whitelist, so synthetic
    reads for either tie and ``resolve`` correctly asks — there is no seed at which bytes alone land on
    one of them. Supplying the rung-0 claim EXERCISES ``distinguishable_by: [metadata, alignment]``
    rather than working around it, and it is derived from ``spec.decidable_by``, never a hand list, so
    a future undecidable-from-bytes entry is covered because it says it is.
    """
    spec = kb.load_spec(tech)
    reg = registry_for(spec)
    reads = kb.generate_reads(spec, n=n, seed=seed)
    paths = []
    for k in keys or tuple(r.id for r in spec.reads):
        p = directory / f"s_{k}.fastq.gz"
        write_fastq_gz(p, reads[k])
        paths.append(p)
    hypothesis = (
        Hypothesis(value=tech, id="synth-fixture", confidence=0.99)
        if "metadata" in spec.decidable_by
        else None
    )
    out = resolve_dataset(paths, registry=reg, use_cache=False, hypothesis=hypothesis)
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


@pytest.fixture(scope="session")
def synth_bulk_pe(tmp_path_factory: pytest.TempPathFactory) -> SynthDataset:
    """The no-barcode shape. Companion to :data:`synth_10x_v3`; built 3x before it existed."""
    return build_synth_dataset(tmp_path_factory.mktemp("synth-bulk-pe"), "bulk-rnaseq")


@pytest.fixture(scope="session")
def synth_bulk_se(tmp_path_factory: pytest.TempPathFactory) -> SynthDataset:
    """The same chemistry, sequenced SINGLE-END — one FASTQ, resolved and filled.

    Not a trimmed copy of :data:`synth_bulk_pe`'s manifest: it is one file handed to the resolver, so
    every claim read off it (the winning read set, the one-read layout, the file inventory) is one the
    byte resolver actually made. That is the difference between testing the composer's tolerance of a
    one-read layout and testing that a single-end deposit compiles at all.
    """
    return build_synth_dataset(
        tmp_path_factory.mktemp("synth-bulk-se"), "bulk-rnaseq", keys=("R1",)
    )


@pytest.fixture(scope="session")
def synth_splitseq(tmp_path_factory: pytest.TempPathFactory) -> SynthDataset:
    """The complex-geometry shape (``cdna``/``bc``, three whitelists). Companion to the two above."""
    return build_synth_dataset(tmp_path_factory.mktemp("synth-splitseq"), "splitseq")


# --------------------------------------------------------------------------- #
# the compile half: the shared build helpers ``test_manifest.py`` and ``test_compose.py`` both read
# --------------------------------------------------------------------------- #

#: What every build here produces: the dataset manifest and the registry its onlists came from.
Built = tuple[DatasetManifest, OnlistRegistry]


@pytest.fixture
def built_v3(synth_10x_v3: SynthDataset) -> Built:
    """The suite's default shape, built ONCE per session — see ``tests/conftest.py``.

    33 tests in this file each re-derived it (a resolve + two probes, 0.238s apiece) to get a value
    that is the same every time. It is an immutable product: a test that varies it takes a
    ``model_copy``, and every test still composes into its own ``tmp_path``.
    """
    return synth_10x_v3.manifest, synth_10x_v3.registry


def _build(tmp_path: Path, tech: str, keys: tuple[str, ...] | None = None) -> Built:
    """Build a manifest from synthetic reads under ``tmp_path``, for a tech ``built_v3`` is not.

    The body moved to ``conftest.build_synth_dataset`` when the session fixture needed it; this stays
    as the name the rest of the file already calls.
    """
    dataset = build_synth_dataset(tmp_path, tech, keys=keys)
    return dataset.manifest, dataset.registry


def _taxid(value: int) -> EvidencedTaxid:
    """An organism as the manifest holds it: a value that knows how we know it.

    `ExperimentInputs` takes an `EvidencedTaxid` rather than a bare int because the manifest field is
    evidenced and something has to supply the basis. It used to take the int and stamp
    `basis="asserted"` on it unconditionally -- including for a taxid a human typed on the command
    line, which is `user_confirmed` and not the same claim at all.
    """
    return EvidencedTaxid(value=value, basis="user_confirmed", rung=0)


def _manifest_from(paths: list[Path], tech: str, reg: OnlistRegistry) -> DatasetManifest:
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    return fill_manifest(
        result=out.result,
        spec=kb.load_spec(tech),
        observations=[probe_file(p) for p in paths],
        registry=reg,
        experiment=ExperimentInputs(organism=_taxid(6239), accessions=["PRJNA1027859"]),
        seqforge_version=__version__,
    )


def _processing(
    manifest: DatasetManifest,
    *,
    assembly: str = "sacCer3",
    annotation: str = "ensembl",
    processing_id: str = "default",
    pin: bool = True,
) -> ProcessingManifest:
    p, _ = fill_processing(
        spec=kb.load_spec(manifest.library.chemistry.value[0]),
        dataset=manifest,
        processing=ProcessingInputs(assembly=assembly, annotation_name=annotation),
        processing_id=processing_id,
        pin=pin,
        seqforge_version=__version__,
    )
    return p


# --------------------------------------------------------------------------- #
# a PLATE: one Sample per cell, at declared read depths, for the admission floor
# --------------------------------------------------------------------------- #

#: cell id -> run key -> the read count of each of that run's files, in read-layout order.
Plate = dict[str, dict[str, tuple[int, ...]]]


def one_run_each(depths: dict[str, int]) -> Plate:
    """The strictly 1:1 plate — one cell, one run, every file at the same depth. 170 of 190 real ones."""
    return {cell: {"r": (n, n)} for cell, n in depths.items()}


def plate_of(
    base: DatasetManifest, cells: Plate, *, accession: str | None = None
) -> DatasetManifest:
    """``base`` with its one sample replaced by ``cells``, and a provenance count for every new file.

    The files are named, never written: ``compose.plan`` joins a path and reads no byte, so a plate of
    synthetic cells costs nothing on disk. Each file is a copy of one the real resolver assigned, so
    the read layout, the chemistry and every emitted param stay exactly what it decided; only the uri,
    the checksum and the sample claiming it are new. ``dataset_hash`` is recomputed at the end, because
    a manifest whose recorded identity disagrees with its own content is not one anything downstream
    may be asked a question about.

    Here rather than beside its first caller because both the composer's tests and the CLI's read it,
    and two builders of one fixture shape do not disagree until they do.
    """
    template = list(base.library.files)
    files: list[FileInventoryItem] = []
    samples: list[SampleGroup] = []
    reads: dict[str, int] = {}
    for cell, runs in cells.items():
        uris: list[str] = []
        for run, depths in runs.items():
            for item, depth in zip(template, depths, strict=True):
                uri = f"{cell}_{run}_{item.read_id}.fastq.gz"
                sha = hashlib.sha256(uri.encode()).hexdigest()
                files.append(item.model_copy(update={"uri": uri, "basename": uri, "sha256": sha}))
                reads[sha] = depth
                uris.append(uri)
        samples.append(SampleGroup(sample_id=cell, accession=accession, file_uris=uris))
    plate = base.model_copy(
        update={
            "library": base.library.model_copy(update={"files": files}),
            "experiment": base.experiment.model_copy(update={"samples": samples}),
            "provenance": base.provenance.model_copy(update={"estimated_reads": reads}),
        }
    )
    return plate.model_copy(
        update={
            "provenance": plate.provenance.model_copy(
                update={"dataset_hash": dataset_content_hash(plate)}
            )
        }
    )


def declare_read_floor(monkeypatch: pytest.MonkeyPatch, tech: str, floor: int | None) -> None:
    """Hand the COMPOSER ``tech``'s spec with a read floor, and one ``Sample`` declared to be one cell.

    A ``model_copy`` and never a mutation: ``load_spec`` is cached and hands back a SHARED ``Spec``, so
    setting the field in place would leak a floor into every other test in the session. Patched at the
    composer's own name because that is the only reader this fixture is about — resolve's half of the
    same declaration is exercised in ``tests/test_resolve.py`` against its own fixture.
    """
    spec = kb.load_spec(tech)
    declared = spec.model_copy(
        update={
            "identity": spec.identity.model_copy(update={"sample_is_cell": True}),
            "min_input_reads": floor,
        }
    )
    monkeypatch.setattr(compose_core, "load_spec", lambda name: declared if name == tech else spec)


def solo_block(config: dict[str, object]) -> dict[str, object]:
    """The emitted ``solo:`` block, narrowed once.

    ``plan(...).config`` is a ``dict[str, object]`` because it serializes to YAML, so every reader
    would otherwise narrow the same value at each use. Returns the config's OWN dict, not a copy: a
    caller that mutates it corrupts the config, which is exactly what the compose corruption cases
    rely on.
    """
    solo = config["solo"]
    assert isinstance(solo, dict), "a starsolo config must carry a solo block"
    return solo


def _src_root() -> Path:
    import seqforge

    return Path(seqforge.__file__).parent


def _rule_blocks(snakefile: Path) -> dict[str, str]:
    """`rule <name>:` -> its body text. Snakemake rules are top-level and flat, so a split suffices.

    Here rather than beside its first caller because it now has two of them — `test_compose.py` reads
    rule bodies for the container/`run:` invariants, `test_workflows.py` for the memory-escalation
    shape — and this file is where every other shared test helper lives. One splitter, so the two
    files cannot come to disagree about what a rule body is.
    """
    parts = re.split(r"^rule (\w+):$", snakefile.read_text(), flags=re.M)[1:]
    return dict(zip(parts[0::2], parts[1::2], strict=True))


@pytest.fixture(scope="session")
def kb_probes(tmp_path_factory: pytest.TempPathFactory) -> KbProbes:
    """Every KB spec's own synthetic reads, probed — ``(spec id, read set) -> the probes a scorer sees``.

    Six tests across ``test_kb.py`` and ``test_resolve.py`` rebuilt this sweep from two copies of the
    same private helper, at ~19.4 ms/spec. The worst rebuilt it per ``(family, leaf)`` PAIR — 20
    rebuilds writing the same filenames over and over.

    Safe to share because it is an **immutable product**, in the sense ``tests/conftest.py`` means it:
    ``WindowProbe`` is a frozen dataclass, ``kb.load_spec`` is already cached (so the ``Read``
    identities its ``_frame_cache`` memoizes on are session-stable either way), and that cache is a
    pure memo of a deterministic function — a probe answers the same question whoever asked first.
    No ``seqforge/`` workspace is involved, and nothing writes into the directory once it is built.

    Keyed over ``kb.list_spec_ids()``, which is the same ids as ``load_all_specs()`` and
    ``load_tree().specs``, so one sweep serves callers that iterate any of the three — and now by
    ``(id, read set)``, because a spec that publishes more than one sequencing configuration has more
    than one "its own reads". The maximal set is ``full``; a declared subset's entry is the SAME probes
    narrowed to that set's reads, so ``("bulk-rnaseq", "se")`` is a single-end bulk deposit written
    from the same seed as the paired-end one. No extra FASTQ is written for a subset: sharing the file
    is what keeps the two configurations byte-comparable rather than merely similar.
    """
    workdir = tmp_path_factory.mktemp("kb-probes")
    out: KbProbes = {}
    for tech_id in kb.list_spec_ids():
        spec = kb.load_spec(tech_id)
        reads = kb.generate_reads(spec, n=400, seed=0)
        by_read: dict[str, WindowProbe] = {}
        for read_id, seqs in reads.items():
            path = workdir / f"{tech_id.replace('/', '_')}_{read_id}.fastq.gz"
            write_fastq_gz(path, seqs)
            by_read[read_id] = WindowProbe(observation=probe_file(path), seqs=seqs[:200])
        for set_name in spec.read_set_names():
            out[tech_id, set_name] = [by_read[r.id] for r in spec.reads_in(set_name)]
    return out


@pytest.fixture(scope="session")
def src_trees() -> SrcTrees:
    """Every ``.py`` under ``src/seqforge``, parsed once — ``path -> its AST``.

    Three tests each walked `rglob("*.py")` + `ast.parse` over the whole tree to ask a one-line
    question of it. The trees are read-only here (every consumer only ``ast.walk``s them), which is
    what makes one parse serve all three.
    """
    import seqforge

    root = Path(seqforge.__file__).parent
    return {py: ast.parse(py.read_text()) for py in sorted(root.rglob("*.py"))}
