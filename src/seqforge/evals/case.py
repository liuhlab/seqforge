"""Eval cases — a declarative, *byte-free* description of a dataset and its ground truth.

Layout::

    evals/cases/<case_id>/
      inputs/recipe.yaml   # HOW to build the FASTQ, not the FASTQ itself
      metadata/*.txt       # prose the LLM stage reads (optional)
      expected.yaml        # ground truth, or the expected refusal/question

**Inputs are a recipe, never committed bytes.** A recipe is a few hundred bytes, is deterministic in
``(spec, seed)``, and regenerates byte-identically on any machine — so a case is diffable, a KB spec
change is *visible* in the inputs it produces, and no FASTQ ever enters git history. It also lets a
case backed by **real** data (which is far too large for git, and whose path is a lab fact this public
repo must not carry) use the same format via ``kind: local``: the ground truth is committed, the bytes
stay wherever the maintainer keeps them.

The recipe deliberately reuses ``kb.generate`` — the same round-trip generator the KB self-tests
run on. Evals therefore measure the compiler, not a second, drifting notion of what a FASTQ looks like.
"""

from __future__ import annotations

import os
import random
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import kb
from ..io import OnlistRegistry
from ..kb.generate import write_fastq_gz
from ..kb.schema import Spec
from ..models.observation import Observation
from ..models.records import ArchiveRecord, ArchiveRecordSet
from ..resolve.group import run_key

CASES_DIRNAME = "cases"


class Truncate(BaseModel):
    """Cut a gzip member mid-stream: valid records, then an abrupt end (the TRUNCATED_GZIP negative)."""

    model_config = ConfigDict(extra="forbid")

    file: str
    fraction: float = Field(default=0.6, gt=0.0, lt=1.0)


class OverLength(BaseModel):
    """Sequence one read past the cycles its chemistry declares — the archive's commonest deviation.

    A kit's declared read length is a recommendation, and submitters routinely exceed it: a 26 bp 10x
    barcode read arrives at 28, or a 28 bp one at 150. The CB/UMI still sit at the declared offsets and
    the trailing bases are junk the aligner ignores, so the library is unchanged — but the exact
    ``segment_length`` gate that separates two chemistries by two cycles cannot know that, and the
    whitelist has to say so instead. Without this knob that shape is inexpressible in a recipe, so the
    only cases covering it are real datasets in the networked tier.

    ``extra`` random ACGT bases are appended to every read of ``read``, deterministically in the
    recipe's own ``seed`` — a recipe stays reproducible or it is not a stand-in for the bytes.
    """

    model_config = ConfigDict(extra="forbid")

    #: A ``Read.id`` of the spec being generated (``R1``, ``bc``, …).
    read: str
    extra: int = Field(gt=0)


class SpecRecipe(BaseModel):
    """Synthesize inputs from a KB spec via the round-trip generator."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["spec"] = "spec"
    spec: str
    n: int = Field(default=3000, gt=0)
    seed: int = 0
    pool_size: int = Field(default=64, gt=0)
    #: ``synthetic`` registers the exact pools the reads were drawn from (rung 3 reachable);
    #: ``none`` withholds the whitelist, so the case can only be settled by structure (rung <=2).
    onlists: Literal["synthetic", "none"] = "synthetic"
    truncate: Truncate | None = None
    over_length: OverLength | None = None


class RandomRecipe(BaseModel):
    """Bytes that match no KB technology — the ONT / UNSUPPORTED_TECHNOLOGY negative."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["random"] = "random"
    name: str = "reads"
    n: int = Field(default=200, gt=0)
    min_len: int = Field(default=500, gt=0)
    max_len: int = Field(default=3000, gt=0)
    seed: int = 0


class LocalRecipe(BaseModel):
    """Real files at a path this repo does not contain.

    ``root`` is resolved from the environment at run time, never committed — the data is too large for
    git and its location is a lab fact, not a project fact. A case whose root is unset or absent
    **skips**: it never fails and never silently passes.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["local"] = "local"
    #: Name of the env var holding the dataset root. The value lives in out-of-git config.
    root_env: str
    glob: str = "*.fastq.gz"
    #: Prose that lives WITH the data rather than in the case directory — a glob under ``root``,
    #: e.g. ``info/*.pdf``.
    #:
    #: Without this a local case could not point at a document at all, so ``has_prose`` was false, so
    #: the language model never ran, so the organism could never come from the paper — **the single
    #: thing PRJNA1027859 exists to test**. A synthetic case keeps its prose in ``metadata/``; a real
    #: one cannot, because the paper is 10 MB and lives beside 220 GB of FASTQ, outside the repo.
    docs_glob: str = ""


class FingerprintRecipe(BaseModel):
    """A committed or out-of-git **fingerprint package** — the byte-light benchmark input.

    A fingerprint (``<dataset>.fingerprint.tar.gz``) is a head-slice of every FASTQ plus a pin that
    carries the whole-file identity, so it reproduces the same resolve verdict — and the same manifest
    hash — with the originals gone. Feeding one through this recipe is how the benchmark runs a *real*
    dataset in CI without shipping (or even reaching) the full FASTQ.

    Three sources, exactly one set (mirroring :class:`LocalRecipe`'s skip-when-unset discipline):

    - ``path`` — a package committed inside the case directory (``package.fingerprint.tar.gz``), for a
      small hermetic ci fixture that runs offline on every commit.
    - ``hf`` — a package path within the public HF benchmark repo, pulled (pooch-cached, anonymous, no
      token) by the opt-in / scheduled networked eval job. Unreachable — offline, or not yet uploaded —
      ⇒ **skip**, so the job stays green before the HF repo is populated and CI never depends on it.
    - ``root_env`` — an env var naming a package path (a ``.tar.gz`` or an unpacked directory), for a
      package staged out of git by the maintainer. Unset/absent ⇒ **skip**, like a missing local root.

    The package carries its own ``info/text/`` prose, surfaced as ``metadata_docs`` so a ``--llm`` run
    harvests it; a hermetic ``--no-llm`` run resolves the chemistry from the pinned bytes and grades
    sample attributes from the committed ``records.json`` — no network, no key.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["fingerprint"] = "fingerprint"
    #: A package committed under the case dir, relative to it (e.g. ``package.fingerprint.tar.gz``).
    path: str = ""
    #: OR a package path within the public HF benchmark repo (``packages/GSE274290.fingerprint.tar.gz``).
    hf: str = ""
    #: OR the name of an env var holding the package path. The value lives in out-of-git config.
    root_env: str = ""

    @model_validator(mode="after")
    def _exactly_one_source(self) -> FingerprintRecipe:
        if sum(bool(s) for s in (self.path, self.hf, self.root_env)) != 1:
            raise ValueError(
                "a fingerprint recipe needs exactly one of `path`, `hf`, or `root_env`"
            )
        return self


class Recipe(BaseModel):
    """``inputs/recipe.yaml``."""

    model_config = ConfigDict(extra="forbid")

    generate: SpecRecipe | RandomRecipe | LocalRecipe | FingerprintRecipe = Field(
        discriminator="kind"
    )
    #: A metadata claim entering resolve as a hypothesis WITHOUT an LLM, so conflict/steering cases
    #: are testable in a no-API-key CI. When a case has prose and `--llm` is on, harvest overrides it
    #: — but only where the prose AGREES with itself: two chemistries in one dataset reduce to no
    #: hypothesis at all (`resolve.chemistry_hypothesis`), and this declared one is what survives.
    hypothesis: str | None = None


class ExpectedConflict(BaseModel):
    """The conflict — or the question — a case must surface. Both are exit 4, and both are pinned here.

    ``positions`` is the load-bearing assertion, not ``field``: a Conflict is specified by
    the values that disagree (26 bp asserted vs 28 bp observed), because *that* is the decidable pair
    a human is being shown. Asserting only the field name would let both positions collapse to the
    same value and still pass.

    ``options`` is what ``positions`` is for the other shape of exit 4. A question has nothing that
    disagrees — it has the answers a human is being offered — so a case that stops because the bytes
    tie between two chemistries can name that pair and nothing else will satisfy it. Naming only the
    field would pass on *any* chemistry question, which leaves the interesting half of "it asks, and
    here is why" unasserted.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = "observed_vs_asserted"
    field: str | None = None
    #: Expected ``basis -> value`` for each position, e.g. ``{asserted: "26", observed: "28"}``.
    positions: dict[str, str] = Field(default_factory=dict)
    #: Expected option set of the Question, e.g. ``["10x-3p-gex-v2", "10x-5p-gex-v2"]``.
    options: list[str] = Field(default_factory=list)


class ExpectedAssertion(BaseModel):
    """A claim the prose really makes, which harvest must extract AND span-verify."""

    model_config = ConfigDict(extra="forbid")

    field: str
    value: str


class Expected(BaseModel):
    """``expected.yaml`` — ground truth, or the expected refusal/question.

    ``outcome`` is the primary contract; everything else refines it. Note ``forbidden_fields``: prose
    traps where the correct extraction is *silence*. Rewarding only recall would train the prompt to
    guess, which is precisely the failure this harness exists to catch.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["decide", "refuse", "ask"]
    description: str = ""
    #: Which code the expectation was written against — required for a case over real data, meaningless
    #: for a synthetic one.
    #:
    #: A pre-registration mixes two kinds of claim and only one is sacred:
    #:
    #: (a) claims about the DATASET — organism, chemistry, what the record declares. From public
    #:     metadata. **Never change these.** Editing one after a run is cheating, full stop.
    #: (b) claims about OUR COMPILER'S OUTPUT on that dataset — a function of code version. Editing
    #:     one after a code change is not tuning against the answer; it is keeping a prediction
    #:     well-typed.
    #:
    #: This stamp is what makes the difference auditable from `git log` alone: was every (a) claim
    #: byte-identical to the pre-run commit, and did every (b) change cite only a code diff? A (b)
    #: edit derived from a **diff** passes. One derived from a **run** does not. Never overwrite a
    #: (b) claim — append, and let the old prediction stand in git as the dated record.
    predicts: dict[str, str] = Field(default_factory=dict)
    #: Dotted manifest paths -> expected value. Supported: ``library.chemistry``,
    #: ``library.equivalence_members``, ``library.roles.<role_id>`` (value = a file label), ``rung``.
    fields: dict[str, Any] = Field(default_factory=dict)
    #: For ``outcome: refuse`` — the BlockerCodes that must be raised.
    blockers: list[str] = Field(default_factory=list)
    #: For ``outcome: ask`` — the conflict that must be surfaced.
    conflict: ExpectedConflict | None = None
    #: Harvest ground truth (checked only when the LLM stage runs).
    assertions: list[ExpectedAssertion] = Field(default_factory=list)
    #: Fields the prose does NOT state: extracting any of them is a hallucination.
    forbidden_fields: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Case:
    id: str
    root: Path
    recipe: Recipe
    expected: Expected
    metadata_docs: list[Path]
    #: `<case>/records.json` — what the archive declares, as `seqforge io records` fetched it.
    #:
    #: Committed rather than fetched at run time, for the same reason the FASTQ is a recipe: a case
    #: must be reproducible and must not need the network. It is public metadata (no lab path,
    #: `test_skill_never_leaks_a_lab_path` still applies), it is an INPUT rather than an expectation,
    #: and it is byte-identical to what `io records` returns today.
    records: ArchiveRecordSet | None = None

    @property
    def has_prose(self) -> bool:
        return bool(self.metadata_docs) or bool(self.records)

    @property
    def needs_llm(self) -> bool:
        """Whether the expectation depends on a claim only *harvest* (the LLM) can supply.

        True iff there is a document to read and no declared hypothesis to stand in for it. Records
        alone do **not** need the LLM: a record's harmonized sample attributes resolve deterministically
        through the metadata resolver, so a records-only case (a hermetic fingerprint case is the
        motivating example) runs with no key. A case whose package carries prose surfaces that prose at
        ``materialize`` time, not here, so this stays load-time cheap and never unpacks a package.
        """
        return bool(self.metadata_docs) and self.recipe.hypothesis is None


@dataclass(frozen=True)
class Materialized:
    """Inputs on disk, plus the onlist registry the resolver may use."""

    paths: list[Path]
    registry: OnlistRegistry | None
    #: Label per file basename, e.g. ``R1.fastq.gz`` -> ``R1``, for role-assignment assertions.
    labels: dict[str, str]
    #: The case's archive records, carried through so the metadata resolver gets the same input the
    #: CLI would give it. ``None`` for a case with no accession, which is most of them.
    records: ArchiveRecordSet | None = None
    #: A pre-built probe map (``str(path) -> (Observation, seqs)``) for a fingerprint case: the sliced
    #: reads probed with the whole-file identity stamped back from the pin, so resolve reproduces the
    #: full dataset's verdict and hash. ``None`` for a case whose bytes are probed live.
    probed: dict[str, tuple[Observation, list[str]]] | None = None
    #: Prose the package carried (``info/text/`` of a fingerprint), fed to harvest under ``--llm``.
    #: A synthetic case keeps its prose in ``metadata/``; a fingerprint case ships it inside the tarball.
    metadata_docs: list[Path] = field(default_factory=list)


class CaseError(RuntimeError):
    """A case is malformed. Distinct from a case *failing* — this is a bug in the case itself."""


#: Why a case did not run. ``unavailable`` is an accident of *this machine* — no local root, no API
#: key, the archive offline — and says nothing about the corpus. ``absent`` is a property of the
#: **corpus itself**: the package was never published, so the case cannot run anywhere, for anyone,
#: until someone uploads it. One is weather and the other is a gap, and a report that spelled them
#: the same way let a dataset go quietly missing behind a word that reads as transient.
SkipKind = Literal["unavailable", "absent"]


class CaseSkipped(RuntimeError):
    """A case cannot run here (local root unset, LLM needed but disabled). Never a pass or fail.

    ``kind`` is the machine-readable half of the reason, carried on the exception rather than
    re-derived from its message: a report that had to grep a sentence for "404" would be a parser
    over prose, which is the thing exit codes exist to avoid.
    """

    def __init__(self, message: str, *, kind: SkipKind = "unavailable") -> None:
        super().__init__(message)
        self.kind: SkipKind = kind


def default_cases_dir() -> Path:
    """``<repo>/evals/cases``. The harness ships with the repo, so this is a relative walk-up."""
    return Path(__file__).resolve().parents[3] / "evals" / CASES_DIRNAME


def load_case(root: Path) -> Case:
    """Load one case directory. Raises :class:`CaseError` if the case itself is malformed."""
    root = Path(root)
    expected_path = root / "expected.yaml"
    recipe_path = root / "inputs" / "recipe.yaml"
    if not expected_path.is_file():
        raise CaseError(f"{root.name}: missing expected.yaml")
    if not recipe_path.is_file():
        raise CaseError(f"{root.name}: missing inputs/recipe.yaml")
    try:
        expected = Expected.model_validate(_read_yaml(expected_path))
        recipe = Recipe.model_validate(_read_yaml(recipe_path))
    except Exception as exc:  # pydantic/yaml -> a case-level error, not a crash
        raise CaseError(f"{root.name}: {exc}") from exc

    meta_dir = root / "metadata"
    docs = sorted(p for p in meta_dir.glob("*") if p.is_file()) if meta_dir.is_dir() else []
    docs += _docs_beside_the_data(recipe)

    records_path = root / "records.json"
    records = (
        ArchiveRecordSet.model_validate_json(records_path.read_text())
        if records_path.is_file()
        else None
    )
    return Case(
        id=root.name,
        root=root,
        recipe=recipe,
        expected=expected,
        metadata_docs=docs,
        records=records,
    )


def _docs_beside_the_data(recipe: Recipe) -> list[Path]:
    """Prose living at a local case's data root (`docs_glob`), if the root is set and present.

    Silent when the root is unset: the case is about to skip for that reason anyway, and raising here
    would turn "this machine does not have the data" into a load error for every OTHER case in the
    corpus, since `discover_cases` loads them all.
    """
    gen = recipe.generate
    if not isinstance(gen, LocalRecipe) or not gen.docs_glob:
        return []
    root = os.environ.get(gen.root_env)
    if not root or not Path(root).is_dir():
        return []
    return sorted(p for p in Path(root).glob(gen.docs_glob) if p.is_file())


def discover_cases(cases_dir: Path | None = None) -> list[Case]:
    """Every case under ``cases_dir``, at any nesting depth, sorted by path.

    A case *is* a directory that holds an ``expected.yaml``; the directories above it are purpose
    groups (``spec/``, ``prose/``, ``steering/``, ``refusal/``, ``real/``) that organise the corpus for
    a reader without changing a case's identity — a case's id stays its own leaf-directory name, so a
    group is a filing decision, not part of the case. Finding cases by their ``expected.yaml`` rather
    than by ``iterdir`` is what lets the layout be grouped or flat (the benchmark tier is flat) and the
    same discovery serve both.
    """
    base = Path(cases_dir) if cases_dir is not None else default_cases_dir()
    if not base.is_dir():
        return []
    roots = sorted({p.parent for p in base.rglob("expected.yaml")})
    return [load_case(d) for d in roots]


def materialize(case: Case, dest: Path) -> Materialized:
    """Build the case's FASTQ inputs under ``dest``. Deterministic in the recipe."""
    gen = case.recipe.generate
    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(gen, LocalRecipe):
        built = _materialize_local(gen)
    elif isinstance(gen, FingerprintRecipe):
        built = _materialize_fingerprint(gen, case.root, dest)
    elif isinstance(gen, RandomRecipe):
        built = _materialize_random(gen, dest)
    else:
        built = _materialize_spec(gen, dest)
    records = case.records
    if records is not None and isinstance(gen, FingerprintRecipe):
        records = _records_the_package_reaches(records, built.paths)
    return replace(built, records=records)


def _records_the_package_reaches(
    records: ArchiveRecordSet, paths: Sequence[Path]
) -> ArchiveRecordSet:
    """Drop the archive records no slice in the package can ever join to.

    A fingerprint package pins a chosen subset of a series' runs, but the committed transcript is
    whatever the archive declared for the whole accession — so the two drift apart silently, and the
    drift is one-sided. A file with no record refuses the join outright; a record with no file is
    never reached, because the join walks the files on disk. It costs one extraction call per record
    and grades nothing: GSE126954 declared a re-analysis sample whose 910 runs were 92% of the case's
    spend and 83% of the whole benchmark's, for claims the resolver then discarded unread.

    Narrowing here rather than in the metadata resolver is deliberate. A real dataset legitimately
    has records for runs you were not handed, and refusing or dropping them there would be a change
    to what seqforge decides. This is a property of the *fixture*: a case grades the bytes it ships,
    so it should ask about the samples it ships and nothing else.

    Run identity is matched the way the join matches it — by run accession, or by an original
    filename the record declares — so a package whose slices keep submitter names narrows the same
    way one carrying SRA names does.
    """
    basenames = {p.name for p in paths}
    reachable = {run_key(name) for name in basenames}
    keep: set[str] = set()
    for run in records.at("run"):
        if run.accession not in reachable and not (set(run.filenames) & basenames):
            continue
        current: ArchiveRecord | None = run
        while current is not None and current.accession not in keep:
            keep.add(current.accession)
            current = records.by_accession(current.parent) if current.parent else None
    return records.model_copy(
        update={"records": [r for r in records.records if r.accession in keep]}
    )


def _materialize_fingerprint(gen: FingerprintRecipe, case_root: Path, dest: Path) -> Materialized:
    """Unpack a fingerprint package and rebuild its pinned probe map — the benchmark's real-data seam.

    The slices are probed exactly as a normal local file is, then the whole-file identity is stamped
    back from the pin, so resolve reaches the same verdict (and the manifest the same hash) the full
    FASTQs would. The package's ``info/text/`` prose rides along as ``metadata_docs``.
    """
    from ..fingerprint.load import load_fingerprint, probed_from_fingerprint

    pkg = _fingerprint_package(gen, case_root)
    loaded = load_fingerprint(pkg, unpack_to=dest / "package")
    paths, probed = probed_from_fingerprint(loaded)
    return Materialized(
        paths=paths,
        registry=None,
        labels={p.name: _label(p.name) for p in paths},
        probed=probed,
        metadata_docs=loaded.info_paths(),
    )


def _fingerprint_package(gen: FingerprintRecipe, case_root: Path) -> Path:
    """Resolve a fingerprint recipe to a package on disk, or :class:`CaseSkipped` if it is not here.

    Three sources, one skip contract, **two kinds of skip**. A ``root_env`` package lives outside the
    repo; unset or missing it **skips**, like a local case. An ``hf`` package is pulled from the public
    HF benchmark (pooch-cached, no token): unreachable — offline, or the archive unwell — it skips as
    ``unavailable``, so the networked job stays green on a bad day; a 404 skips as ``absent``, because
    a package nobody published is a gap in the corpus rather than weather, and the report names it as
    one. A committed ``path`` package is a hermetic fixture and should always be present, so a missing
    one also skips (never a silent pass — the ci fixture's own test fails loudly if it vanishes).

    Both kinds still skip. The benchmark tier is opt-in and gates no merge, so a missing package must
    not fail a run; what it must not do either is look like a network blip nobody has to act on.
    """
    if gen.root_env:
        root = os.environ.get(gen.root_env)
        if not root:
            raise CaseSkipped(
                f"${gen.root_env} is not set (a fingerprint package lives outside the repo)"
            )
        pkg = Path(root)
        if not pkg.exists():
            raise CaseSkipped(f"${gen.root_env}={root} does not exist on this machine")
        return pkg
    if gen.hf:
        from ..io import (
            BenchmarkPackageAbsent,
            BenchmarkPackageUnavailable,
            fetch_benchmark_package,
        )

        try:
            return fetch_benchmark_package(gen.hf)
        except BenchmarkPackageAbsent as exc:
            raise CaseSkipped(str(exc), kind="absent") from exc
        except BenchmarkPackageUnavailable as exc:
            raise CaseSkipped(str(exc)) from exc
    pkg = (case_root / gen.path).resolve()
    if not pkg.exists():
        raise CaseSkipped(f"fingerprint package not found: {gen.path!r} under {case_root}")
    return pkg


def _materialize_local(gen: LocalRecipe) -> Materialized:
    root = os.environ.get(gen.root_env)
    if not root:
        raise CaseSkipped(
            f"${gen.root_env} is not set (a local case's root lives outside the repo)"
        )
    base = Path(root)
    if not base.is_dir():
        raise CaseSkipped(f"${gen.root_env}={root} does not exist on this machine")
    paths = sorted(base.glob(gen.glob))
    if not paths:
        raise CaseSkipped(f"no files matching {gen.glob!r} under ${gen.root_env}")
    return Materialized(paths=paths, registry=None, labels={p.name: _label(p.name) for p in paths})


def _materialize_random(gen: RandomRecipe, dest: Path) -> Materialized:
    rng = random.Random(gen.seed)
    seqs = [
        "".join(rng.choice("ACGT") for _ in range(rng.randint(gen.min_len, gen.max_len)))
        for _ in range(gen.n)
    ]
    path = dest / f"{gen.name}.fastq.gz"
    _write_fastq_gz(path, seqs)
    return Materialized(paths=[path], registry=None, labels={path.name: gen.name})


#: The run accession stand-in every generated case deposits its files under. A case built from one KB
#: spec IS one library, so its files are one run's — and the only thing that says so downstream is the
#: filename, because `resolve.group_runs` groups by name (never by role) and nothing else in the
#: pipeline knows a case exists. Deliberately not an `[SED]RR\d+` shape: these bytes were never in an
#: archive, and a name that reads like an accession in a manifest is a lie a reader cannot check.
SIM_RUN = "SIM"


def _deposited_as(spec: Spec, read_id: str, index: int) -> str:
    """The filename read ``read_id``'s bytes are written under: ``SIM_R1.fastq.gz``.

    **The generator used to name each file after the read it carries** — `R1.fastq.gz`,
    `cdna.fastq.gz` — which is a shape no deposit has and, worse, one that groups into no run: two
    names sharing no stem are two single-file RUNS to `resolve.group_runs`, and a barcode read with
    no cDNA mate resolves to nothing. That was invisible for as long as the eval harness handed a
    case's whole file list to `resolve_dataset` as one library; the moment it resolves runs the way
    `manifest fill` does (#196), every generated case refuses `UNSUPPORTED_TECHNOLOGY`.

    The mate token is the spec's own ``file_hint`` (`_R1_` -> `R1`), so the name carries exactly the
    conventional slot a real submitter's file carries, and `scoring.filename_prior` — a sub-threshold
    nudge that can break an exact byte tie and nothing else — reads the same token off a generated
    case that it reads off a real one. A spec that declares no hint falls back to the read's 1-based
    position, which still groups; what may never happen is a token `group_runs` cannot read as a
    mate, because then the case silently splits into runs again.
    """
    read = next((r for r in spec.reads if r.id == read_id), None)
    hint = (read.file_hint or "").strip("_") if read is not None else ""
    return f"{SIM_RUN}_{hint or index}.fastq.gz"


def _materialize_spec(gen: SpecRecipe, dest: Path) -> Materialized:
    try:
        spec = kb.load_spec(gen.spec)
    except Exception as exc:
        raise CaseError(f"unknown KB spec {gen.spec!r}: {exc}") from exc

    pools = kb.build_pools(spec, seed=gen.seed, pool_size=gen.pool_size)
    reads = kb.generate_reads(spec, n=gen.n, seed=gen.seed, pool_size=gen.pool_size, pools=pools)

    if gen.over_length is not None:
        ol = gen.over_length
        if ol.read not in reads:
            raise CaseError(
                f"over_length.read={ol.read!r} is not a read of spec {gen.spec!r} "
                f"(have: {sorted(reads)})"
            )
        # A stream of its own, offset from the generator's, so appending junk cannot shift the barcodes
        # a case's whole point may rest on.
        rng = random.Random(gen.seed + 977)
        reads[ol.read] = [
            seq + "".join(rng.choice("ACGT") for _ in range(ol.extra)) for seq in reads[ol.read]
        ]

    paths: list[Path] = []
    labels: dict[str, str] = {}
    names = {read_id: _deposited_as(spec, read_id, i) for i, read_id in enumerate(reads, start=1)}
    for read_id, seqs in reads.items():
        path = dest / names[read_id]
        _write_fastq_gz(path, seqs)
        paths.append(path)
        labels[path.name] = read_id

    if gen.truncate is not None:
        # A recipe names the READ, never the file it landed under — that name is this function's
        # business. A typo must stay the loud case error it always was, not a silently un-truncated
        # (and therefore passing) case.
        if gen.truncate.file not in names:
            raise CaseError(
                f"truncate.file={gen.truncate.file!r} is not a read of spec {gen.spec!r} "
                f"(have: {sorted(reads)})"
            )
        target = dest / names[gen.truncate.file]
        data = target.read_bytes()
        target.write_bytes(data[: int(len(data) * gen.truncate.fraction)])

    registry: OnlistRegistry | None = None
    if gen.onlists == "synthetic":
        registry = OnlistRegistry(offline=True)
        for alias, ref in spec.onlists.items():
            if alias in pools:
                registry.register_synthetic(ref.registry, pools[alias])
    return Materialized(paths=paths, registry=registry, labels=labels)


def _label(basename: str) -> str:
    name = basename
    for suffix in (".gz", ".fastq", ".fq"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    """The KB's reproducible writer: identical recipe -> identical bytes -> identical sha256.

    Reproducibility is what makes a recipe a legitimate stand-in for the bytes it replaces, so this
    module must not grow its own writer. See :func:`kb.generate.write_fastq_gz` for why a plain
    ``gzip.open`` is not reproducible.
    """
    write_fastq_gz(path, seqs)


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise CaseError(f"{path.name}: expected a YAML mapping, got {type(data).__name__}")
    return data
