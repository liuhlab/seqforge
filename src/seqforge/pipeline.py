"""One compiled pipeline directory, and the one module that knows what is inside it.

``compose`` writes ``seqforge/pipeline/<recipe>-<run_id[:12]>/`` and fills it with four things: the
wrapper a user submits, the config that wrapper reads, the units table it iterates, and a copy of the
hand-written **Workflow module** carrying the rules. Everything downstream of that has to ask the
same small questions of the directory — where is the Snakefile, what did the composer decide, which
**Workflow module** is going to run, which samples the pipeline was contracted to produce, and where
do its outputs land.

Five modules used to answer them by hand: the composer that writes the directory, the report
collector, the project index, the compose gates and the ground-truth harness. Two of those had grown
*independently written* implementations of the same three-step derivation — which module, which
samples, which output directory. Two implementations of one derivation do not disagree until they do,
and this repo has already paid for that shape twice: two renderings of a STAR command line in files
that could not see each other, and two renderings of "how do I get an index", where in both cases the
copy nobody executed was the broken one.

So the layout has one owner, deliberately split across two homes:

- ``workspace.py`` names the **directory**, beside every other subtree it names, and does no I/O.
- this module names what is **inside** one, and opens it.

**Top-level rather than inside ``compose/``.** The composer *writes* a pipeline directory; everyone
else *reads* one. A reader that lived beside the writer would mean the report imports the compiler to
learn where a file is — the dependency arrow backwards — and would charge every reader the composer's
import surface (the KB, the onlist registry, the workflow registry) for a path join.

**Nothing here is cached.** Every property reads from disk at the call. A pipeline directory is state,
not a value: the ground-truth harness patches the config it has just read and then reads it again, and
a value cached at open would hand it back the file it had already replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .workflows import MODULES
from .workspace import pipeline_dir

#: The three artifacts every compiled pipeline directory carries, spelled once. They were private
#: constants on the composer, which made every *reader* of a composed directory spell them again —
#: the report collector, the compose gates and the ground-truth harness each carried their own copy
#: of at least one. A name that only its writer owns is a name every reader re-invents.
SNAKEFILE_NAME = "Snakefile"
CONFIG_NAME = "config.yaml"
UNITS_TSV_NAME = "units.tsv"

#: The exclusion record — present only when the composer's admission floor kept a sample out, which is
#: the only state in which there is anything to say. Named here beside the three because a run
#: directory's contents have one owner, and a reader looking for "where did those cells go?" must not
#: have to know which module happened to write the answer.
EXCLUSIONS_NAME = "excluded.md"

#: Where a pipeline puts its per-sample outputs when nobody says otherwise: what ``compose`` writes
#: into ``config["outdir"]`` absent a flag, and what a reader falls back to for a config written
#: before that key existed. Both ends of one name, so the fallback cannot drift from the default it
#: standing in for — and a wrong guess here would cost an unfound results directory, never a wrong
#: one, because the samples are then simply not there.
DEFAULT_OUTDIR = "results"


@dataclass(frozen=True)
class CompiledPipeline:
    """A compiled pipeline directory: the three paths ``compose`` writes, and what a reader may ask.

    Construct it around a directory that already exists (the composer, having just made one; a
    harness, having just composed) or find one with :meth:`discover`. The path properties are joins
    and touch nothing, which is what lets the composer *write* through the same object every reader
    reads through — one owner for the layout rather than a writer's private constants and four
    re-spellings of them.

    Every reading property degrades rather than raises: an absent, unreadable or non-mapping config
    reads as an empty one, an unknown module reads as ``None``. A half-composed workspace and a
    workspace composed by a newer seqforge are the same fact to a caller — there is nothing here to
    report — and neither is a reason to fail a report of what the compiler decided.
    """

    directory: Path

    @classmethod
    def discover(
        cls, workspace: str | Path, *, subdir: str | None = None
    ) -> CompiledPipeline | None:
        """The compiled pipeline of one assay, or ``None`` when the assay was never composed.

        A pipeline directory is the one holding a wrapper, so that is what is looked for; the
        recipe-plus-hash name above it is deliberately not parsed, because the name is for a human
        and the identity is the hash inside it. Sorted, and the first is taken: a workspace compiled
        twice holds two directories, and picking by filesystem order would make a report's contents
        depend on the order a directory happened to be walked in.
        """
        found = sorted(pipeline_dir(workspace, subdir=subdir).glob(f"*/{SNAKEFILE_NAME}"))
        return cls(found[0].parent) if found else None

    @property
    def snakefile(self) -> Path:
        """The pipeline wrapper — **the deliverable**, the file a user submits."""
        return self.directory / SNAKEFILE_NAME

    @property
    def config_path(self) -> Path:
        """The composed config: the machine-specific instantiation of a machine-independent manifest."""
        return self.directory / CONFIG_NAME

    @property
    def units_path(self) -> Path:
        """The units table — one row per (sample, **Run**, **Lane**, read role, file)."""
        return self.directory / UNITS_TSV_NAME

    @property
    def exclusions_path(self) -> Path:
        """The exclusion record — why this run's sample list is shorter than the manifest's.

        A join like the three above, so it names the file whether or not one is there: absent is the
        normal state, and it is the honest reading of "nothing was excluded". A caller that wants to
        know asks the filesystem, exactly as it would for a results directory.
        """
        return self.directory / EXCLUSIONS_NAME

    @property
    def module(self) -> str | None:
        """Which **Workflow module** ran, read off the ``.smk`` the composer copied in.

        ``compose`` copies the module's hand-written Snakefile in beside the wrapper verbatim, so the
        file that is *present* names the module that will run — and the mapping is **inverted out of
        the registry** rather than written down here. A hardcoded ``{"starsolo.smk": "map/starsolo"}``
        is exactly the drift ``read_layout_kind`` and ``param_block`` were built to kill: a fourth
        module would simply be missing from it, would answer nothing, and nothing would fail to say
        so.

        ``None`` when no ``.smk`` here is one this build knows — a directory composed by a newer
        seqforge, or one that is not a pipeline at all.
        """
        by_snakefile = {m.snakefile.name: m.name for m in MODULES.values()}
        # Sorted, so the answer never inherits whatever order the filesystem walked the directory in.
        for smk in sorted(self.directory.glob("*.smk")):
            module = by_snakefile.get(smk.name)
            if module is not None:
                return module
        return None

    @property
    def config(self) -> dict[str, Any]:
        """What the composer decided, as a plain dict — ``{}`` if absent, unreadable or not a mapping.

        Untyped on purpose. Its keys are the aligner's, and a reader that typed them would have to be
        edited every time a **Workflow module** declared a new one; the report already renders it as
        an opaque key/value table for that reason.
        """
        try:
            loaded = yaml.safe_load(self.config_path.read_text())
        except (OSError, ValueError, yaml.YAMLError):  # missing, non-UTF-8, or not YAML
            return {}
        return loaded if isinstance(loaded, dict) else {}

    @property
    def samples(self) -> list[str]:
        """The sample ids this pipeline was **contracted** to produce, in the order the config carries.

        From the config — the artifact the pipeline itself was handed — and never from a listing of the
        results tree. That is the whole point: a listing can say what finished and can never say what
        is missing, so a partial pipeline read that way is indistinguishable from a complete one. The
        manifest is not consulted here either; a caller who wants the manifest's sample ids as a
        fallback for a config predating the key has the manifest and this does not.
        """
        declared = self.config.get("samples")
        return [str(s) for s in declared] if isinstance(declared, list) else []

    @property
    def results_dir(self) -> Path:
        """Where the pipeline wrote its per-sample outputs: the config's ``outdir``, joined onto here.

        Relative in the config because the wrapper's own instructions have the user run from inside
        this directory; joining an absolute value leaves it untouched, which is what a relocated pipeline
        needs.
        """
        return self.directory / str(self.config.get("outdir") or DEFAULT_OUTDIR)

    def sample_dir(self, sample: str) -> Path:
        """``<results>/<sample>`` — where one sample's artifacts land, whatever module wrote them.

        The join is here rather than at each caller because it is the shape every shipped module
        agrees on, and a caller that spells it is a caller that can spell it differently.
        """
        return self.results_dir / sample


__all__ = [
    "SNAKEFILE_NAME",
    "CONFIG_NAME",
    "UNITS_TSV_NAME",
    "EXCLUSIONS_NAME",
    "DEFAULT_OUTDIR",
    "CompiledPipeline",
]
