"""Eval cases — a declarative, *byte-free* description of a dataset and its ground truth (brief §9).

Layout, per the brief::

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

The recipe deliberately reuses ``kb.generate`` — the same R10 round-trip generator the KB self-tests
run on. Evals therefore measure the compiler, not a second, drifting notion of what a FASTQ looks like.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .. import kb
from ..io import OnlistRegistry
from ..kb.generate import write_fastq_gz
from ..models.records import ArchiveRecordSet

CASES_DIRNAME = "cases"


class Truncate(BaseModel):
    """Cut a gzip member mid-stream: valid records, then an abrupt end (the TRUNCATED_GZIP negative)."""

    model_config = ConfigDict(extra="forbid")

    file: str
    fraction: float = Field(default=0.6, gt=0.0, lt=1.0)


class SpecRecipe(BaseModel):
    """Synthesize inputs from a KB spec via the R10 round-trip generator."""

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


class Recipe(BaseModel):
    """``inputs/recipe.yaml``."""

    model_config = ConfigDict(extra="forbid")

    generate: SpecRecipe | RandomRecipe | LocalRecipe = Field(discriminator="kind")
    #: A metadata claim entering resolve as a hypothesis WITHOUT an LLM, so conflict/steering cases
    #: are testable in a no-API-key CI. When a case has prose and `--llm` is on, harvest overrides it.
    hypothesis: str | None = None


class ExpectedConflict(BaseModel):
    """The conflict a case must surface.

    ``positions`` is the load-bearing assertion, not ``field``: design §3.5 specifies the conflict by
    the values that disagree (26 bp asserted vs 28 bp observed), because *that* is the decidable pair
    a human is being shown. Asserting only the field name would let both positions collapse to the
    same value and still pass.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = "observed_vs_asserted"
    field: str | None = None
    #: Expected ``basis -> value`` for each position, e.g. ``{asserted: "26", observed: "28"}``.
    positions: dict[str, str] = Field(default_factory=dict)


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
    #: must be reproducible and must not need the network. It is public metadata (no lab path, R8's
    #: `test_skill_never_leaks_a_lab_path` still applies), it is an INPUT rather than an expectation,
    #: and it is byte-identical to what `io records` returns today.
    records: ArchiveRecordSet | None = None

    @property
    def has_prose(self) -> bool:
        return bool(self.metadata_docs) or bool(self.records)


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


class CaseError(RuntimeError):
    """A case is malformed. Distinct from a case *failing* — this is a bug in the case itself."""


class CaseSkipped(RuntimeError):
    """A case cannot run here (local root unset, LLM needed but disabled). Never a pass or fail."""


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
    """Every case directory, sorted by id."""
    base = Path(cases_dir) if cases_dir is not None else default_cases_dir()
    if not base.is_dir():
        return []
    return [load_case(d) for d in sorted(base.iterdir()) if d.is_dir()]


def materialize(case: Case, dest: Path) -> Materialized:
    """Build the case's FASTQ inputs under ``dest``. Deterministic in the recipe."""
    gen = case.recipe.generate
    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(gen, LocalRecipe):
        built = _materialize_local(gen)
    elif isinstance(gen, RandomRecipe):
        built = _materialize_random(gen, dest)
    else:
        built = _materialize_spec(gen, dest)
    return replace(built, records=case.records)


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


def _materialize_spec(gen: SpecRecipe, dest: Path) -> Materialized:
    try:
        spec = kb.load_spec(gen.spec)
    except Exception as exc:
        raise CaseError(f"unknown KB spec {gen.spec!r}: {exc}") from exc

    pools = kb.build_pools(spec, seed=gen.seed, pool_size=gen.pool_size)
    reads = kb.generate_reads(spec, n=gen.n, seed=gen.seed, pool_size=gen.pool_size, pools=pools)

    paths: list[Path] = []
    labels: dict[str, str] = {}
    for read_id, seqs in reads.items():
        path = dest / f"{read_id}.fastq.gz"
        _write_fastq_gz(path, seqs)
        paths.append(path)
        labels[path.name] = read_id

    if gen.truncate is not None:
        target = dest / f"{gen.truncate.file}.fastq.gz"
        if not target.is_file():
            raise CaseError(
                f"truncate.file={gen.truncate.file!r} is not a read of spec {gen.spec!r} "
                f"(have: {sorted(reads)})"
            )
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
