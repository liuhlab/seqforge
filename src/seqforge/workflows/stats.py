"""Read a finished pipeline's per-sample QC artifacts — one interface, one adapter per module.

``seqforge report`` is a reader: it renders what is on disk and decides nothing. Once the composed
Snakefile has run, what is on disk gains a per-sample QC artifact, and this module is how the report
gets at it — for **any** **Workflow module**, without the report ever learning what STARsolo is.

The seam is :class:`StatsSpec`: where one sample's artifact lives, and how to turn it into the shared
:class:`~seqforge.workflows.metrics.SampleStats`. Every shipped module is wired::

    map/starsolo   <sample>.qc.json.gz             gzipped JSON, written by `rule qc_bundle`
    map/chromap    <sample>.fragments.qc.json.gz   gzipped JSON, written by `rule fragments_qc`
    map/star       Log.final.out                   plain text, written by STAR itself
    map/star-umi   Log.final.out                   the same, one per cell
                   + the fan-in artifact           one h5ad over the plate, one `obs` row per cell

Four artifacts, four vocabularies, and no shared column set — the ATAC summary has no
whitelist-match rate and no per-barcode vector, so an scATAC page speaks about fragments and never
about cells, a bulk page speaks about mapping and never about barcodes, and a plate's counting object
speaks about fragments that reached no gene, which none of the other three measured. That divergence
is the seam earning its keep: it is expressed as four adapters rather than as a widening union of
optional fields on one.

**The spec carries a filename, not a suffix**, and the third row above is what that bought. A
``{sample}.<suffix>`` convention can only express artifacts a seqforge rule names, and ``map/star``
has no QC bundle rule at all: STAR writes ``Log.final.out`` into the sample directory unasked, it
carries no sample name, and nothing in ``star.smk`` declares or deletes it. So bulk reports with no
new rule, no ``WORKFLOW_VERSION`` bump, and therefore no ``run_id`` invalidation and no reprocessing
of anything already compiled. Under a suffix convention that artifact would have been inexpressible
and re-derived by a rule instead, at the cost of recompiling every dataset.

A fourth aligner adds one dict entry and one ``(Path, str) -> SampleStats`` function. It does not
touch ``report/``, and it cannot be forgotten: :data:`MODULES_WITHOUT_STATS` is an explicit list, and
a test fails if a registered module appears in neither it nor :data:`_SPECS`. The alternative — a
``module == "map/starsolo"`` branch in the collector — is the same silent fall-through that
``read_layout_kind`` and ``param_block`` already exist to prevent.

**One module reads a second artifact, and what is new about it is its ARITY, not its name.**
``map/star-umi`` counts its whole plate in one job and writes one ``.h5ad`` whose ``obs`` carries
every cell's read fates — a **fan-in artifact**: dataset-scoped as a file, sample-scoped as data. A
per-sample reader cannot express it, since there is no sample in its path; so the spec's second
reader is plural (:attr:`StatsSpec.read_fan_in`), handed the file and the sample list once and
returning one :class:`SampleStats` per row, which :func:`read_pipeline_stats` merges into what the
per-sample artifact said. Its filename is deliberately **not** a second field on the spec:
``Workflow.fan_in_artifact`` already declares it and the rule that produces it reads that same
constant, so spelling it here would be a third owner of one name — the exact drift the imports below
exist to prevent.

The spec carries a second thing the same way: the module's **cross-checks**, the rules that read a
metric back and say which *decision* looks wrong. Same registry, same guard, one level in — a module
declares rules or is named in :data:`MODULES_WITHOUT_CROSS_CHECKS`, never neither and never both.
Beside the reader because a rule about ``valid_barcodes`` is a fact about whoever wrote
``valid_barcodes``, and a renderer that knew what one was would be that ``module ==`` branch again.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import MODULES, get_module
from .fragments import QC_SUFFIX as _FRAGMENTS_QC_SUFFIX
from .fragments import read_metrics as _read_fragments
from .h5ad import STAR_FINAL_LOG
from .metrics import Finding, PipelineStats, SampleStats
from .qc import QC_SUFFIX as _STARSOLO_QC_SUFFIX
from .qc import chemistry_rule as _starsolo_chemistry_rule
from .qc import gene_model_rule as _starsolo_gene_model_rule
from .qc import read_metrics as _read_starsolo
from .qc import read_star_log as _read_star_log
from .qc import solo_features_rule as _starsolo_solo_features_rule
from .umite.count import read_plate_stats as _read_plate_stats

#: One cross-check rule: one sample's metrics in, zero or more :class:`Finding` out. Pure by
#: signature — there is no path, no manifest and no writer in it — which is what makes a threshold
#: testable against literal values, and what makes "advisory" a property of the type rather than a
#: promise in a docstring.
CrossCheck = Callable[[SampleStats], list[Finding]]


@dataclass(frozen=True)
class StatsSpec:
    """How one **Workflow module**'s finished artifact is found, read, and cross-checked.

    ``artifact`` is a filename under ``<results>/<sample>/``; ``{sample}`` is substituted. ``read``
    owns loading as well as parsing, so the loop below hands over a path and gets metrics back and
    never has to know whether the bytes were gzipped JSON or text. Each adapter keeps a **pure**
    ``Mapping -> SampleStats`` function underneath (``qc.metrics``), which is the internal seam its
    tests drive — no filesystem needed to check a threshold.

    ``checks`` is the second half and it rides on the same spec deliberately: a rule that reads
    ``valid_barcodes`` back is a fact about the module that WROTE ``valid_barcodes``, so the metric
    key and the rule reading it change in one file or fail in one file. Defaulted to empty so the
    field can be added without touching every spec — and :data:`MODULES_WITHOUT_CROSS_CHECKS` is what
    stops that default from being a silent one.

    ``read_fan_in`` is the plural reader, for the one module whose pipeline also produces a
    **fan-in artifact**: ``(the artifact, the sample list) -> one SampleStats per row``, opened once
    and merged into the rows above. It carries **no filename of its own** — the module's
    ``fan_in_artifact`` is the single owner of that, and :func:`read_pipeline_stats` asks the
    registry for it — so this field says only *how* to read the thing, never *where* it is.
    ``None`` for the three per-sample-end-to-end modules, which is the default and the common case.
    """

    artifact: str
    read: Callable[[Path, str], SampleStats]
    checks: tuple[CrossCheck, ...] = ()
    read_fan_in: Callable[[Path, Sequence[str]], dict[str, SampleStats]] | None = None


#: Every artifact name here is **imported, never spelled**. A suffix written in the rule that produces
#: it and again in the reader that finds it is two owners of one fact, and the reader's copy is the one
#: that fails silently: a report that finds nothing looks exactly like a pipeline that never ran, so
#: nothing raises and nobody is told. The shipped ``.smk`` files import them too, as of
#: ``WORKFLOW_VERSION`` 2026.8.3 — ``rule qc_bundle`` in ``starsolo.smk`` and ``rule fragments_qc`` in
#: ``chromap.smk`` each read the same constant this file does, so the rule, the writer and the reader
#: are one owner rather than three. A repo-wide check keeps it that way
#: (``test_no_shipped_snakemake_module_restates_a_suffix_its_writer_owns``), which matters because the
#: cost of closing it was real: editing a shipped module invalidates every ``run_id``, so it is not a
#: rename anyone would repeat casually. ``map/star`` has no such second owner and never will:
#: no rule names :data:`~seqforge.workflows.h5ad.STAR_FINAL_LOG`, because STAR writes it whether or not
#: anyone asked — which is exactly why the entry is a bare filename with no ``{sample}`` in it.
_SPECS: dict[str, StatsSpec] = {
    "map/starsolo": StatsSpec(
        artifact=f"{{sample}}{_STARSOLO_QC_SUFFIX}",
        read=_read_starsolo,
        checks=(
            _starsolo_chemistry_rule,
            _starsolo_gene_model_rule,
            _starsolo_solo_features_rule,
        ),
    ),
    "map/chromap": StatsSpec(artifact=f"{{sample}}{_FRAGMENTS_QC_SUFFIX}", read=_read_fragments),
    "map/star": StatsSpec(artifact=STAR_FINAL_LOG, read=_read_star_log),
    # The plate module reports from the same file `map/star` does, and for the same reason: it runs
    # one STAR job per cell, and STAR writes `Log.final.out` into that cell's directory unasked. A
    # cell IS a sample here, so `<results>/<sample>/Log.final.out` is already this reader's shape
    # with no new rule, no second artifact and no per-cell QC bundle to invent.
    #
    # It is also the ONLY module with a second half, and that half is where its counting decisions
    # are: the fan-in writes every cell's read fates into the combined object's `obs`, and those say
    # what the alignment log cannot — how many fragments reached no gene, and why. They arrive
    # through `read_fan_in` rather than through a second `artifact` entry because that artifact has
    # no sample in its path at all: it is one file for the deposit, holding one row per cell.
    #
    # The filename is STILL not spelled here, and that is the same discipline as the four above one
    # arity out: `map/star-umi` DECLARES its deliverable as `fan_in_artifact`, `star-umi.smk` reads
    # that constant to name its output, and `read_pipeline_stats` asks the registry rather than
    # restating it. Three readers, one owner — a rename reaches every one of them or fails at import.
    "map/star-umi": StatsSpec(
        artifact=STAR_FINAL_LOG, read=_read_star_log, read_fan_in=_read_plate_stats
    ),
}

#: Registered modules that deliberately report nothing **yet** — the half of the drift guard that lets
#: a module say "not yet" out loud instead of being silently absent from :data:`_SPECS` and silently
#: missing from every report.
#:
#: **It is empty, and that is the shipped state rather than a stub.** Every registered module reports;
#: the list did its job while the rollout was partial (``map/chromap`` and then ``map/star`` each sat
#: here naming the ticket that landed its adapter) and emptying is what success looks like. Deleting it
#: now would delete the mechanism along with its backlog: an empty frozenset is precisely what
#: :func:`_check_registry` compares a newly registered module against, so a fourth aligner that reports
#: nothing goes red on the day it is registered instead of shipping a page that is silently blank. The
#: standing cost of keeping it is one set difference.
MODULES_WITHOUT_STATS: frozenset[str] = frozenset()

#: Modules that report metrics and deliberately cross-check **nothing** — the other half of the
#: cross-check guard, in exactly the shape :data:`MODULES_WITHOUT_STATS` established. A module is
#: silent only by saying so; being absent from both halves is a build-time defect.
#:
#: Both entries are arguments, not backlog. ``map/chromap``'s fragments summary carries no
#: whitelist-match rate and no gene assignment at all, so single-cell RNA reasoning applied to it
#: would be reasoning about numbers that are not there; ``map/star`` is bulk — no barcode, no cell,
#: and its two graded metrics vary with genome quality and rRNA content far more than with anything
#: seqforge decided, which is the same reason their own thresholds are loose. A rule with no
#: defensible threshold does not ship, and declaring that out loud is a supported answer rather than
#: a gap. Either name leaves this set the day a rule for it can be argued.
#: ``map/star-umi`` joins them on the same argument read off the artifacts rather than off the assay.
#: Its per-cell half is STAR's own alignment log, which carries no barcode-match rate at all, so
#: every barcode rule the droplet module cross-checks with is a number that is not there. Its
#: fan-in half DOES carry a gene-assignment number — the share of fragments landing on no feature —
#: and it still ships no rule, because a rule is a THRESHOLD and nobody has measured one: what share
#: is wrong varies with the annotation's completeness and with how much of a plate library is
#: intronic, and the droplet bar was set on droplet libraries counted a different way. Reporting the
#: number and declining to grade it is the honest state; it leaves this set the day a bar can be
#: argued from a measurement.
MODULES_WITHOUT_CROSS_CHECKS: frozenset[str] = frozenset(
    {"map/chromap", "map/star", "map/star-umi"}
)

#: What the reader will survive from one sample's artifact: bad **bytes**. Caught per sample, so one
#: corrupt file costs its own row and not the whole pipeline.
#:
#: Three tuple members, one per corruption that actually happens. A file that is not gzip at all
#: raises ``BadGzipFile`` (an ``OSError``); truncated JSON inside a valid stream raises
#: ``JSONDecodeError`` (a ``ValueError``); and a gzip stream that simply STOPS — a preempted job, a
#: full disk — raises ``EOFError``, which is neither. That last one is both the easiest to miss and
#: the likeliest on a cluster, and leaving it out meant one killed sample took down the whole report.
#:
#: ``KeyError``/``TypeError`` are deliberately **absent**. They are what a bug in a metric table
#: raises, and catching them would turn a logic error into a per-sample note that reads like bad
#: input — one `except` doing two jobs, tolerating bad bytes (right) and tolerating bad code (wrong).
_UNREADABLE = (OSError, EOFError, ValueError)


def _read_fan_in(
    module: str,
    spec: StatsSpec,
    results_dir: Path,
    samples: Sequence[str],
    notes: list[str],
) -> dict[str, SampleStats]:
    """One module's **fan-in artifact**, opened ONCE — or ``{}`` when it has none, or none landed.

    **The filename comes from the module, never from the spec.** ``Workflow.fan_in_artifact`` is what
    DECLARES the pipeline's dataset-scoped deliverable and what the rule producing it reads; asking
    the registry here makes this the second reader of one constant rather than the second speller of
    one name — the rule the artifact table above states, applied to the one artifact with no
    ``{sample}`` in it.

    Once, and not once per sample, because the artifact is one object over the whole plate: 1440
    cells means 1440 rows in one file, and re-opening it per row would turn a single read into a
    quadratic one for a page showing five columns.

    A module declaring a reader and no artifact is refused by :func:`_check_registry` at build time,
    which is what the ``artifact is None`` arm here is: the narrowing that fact implies, not a
    silent skip of a job somebody asked for.

    An artifact that is there and unreadable costs a note and nothing else, exactly as a per-sample
    one does — every cell keeps the half of its row the alignment log gave it.
    """
    artifact = get_module(module).fan_in_artifact
    if spec.read_fan_in is None or artifact is None:
        return {}
    path = results_dir / artifact
    if not path.is_file():
        return {}
    try:
        return spec.read_fan_in(path, samples)
    except _UNREADABLE as exc:
        notes.append(
            f"{artifact}: the pipeline's dataset-wide artifact could not be read "
            f"({type(exc).__name__}), so no sample below carries what it measured"
        )
        return {}


def read_pipeline_stats(
    module: str, results_dir: Path, samples: Sequence[str]
) -> PipelineStats | None:
    """Every finished sample of one compiled pipeline, or ``None`` when there is nothing to show.

    ``samples`` is the composed ``config.yaml``'s own ``samples`` list — the same artifact the
    pipeline consumed — so "did it finish" is answered by the files it was contracted to produce
    rather than by parsing a snakemake log. That also makes a **partial** pipeline a first-class
    answer: the samples that landed are reported, and ``n_found``/``n_expected`` says how much did.

    ``None`` means "render no results section": a module this build has no adapter for, a pipeline
    that has not started, or a results directory that is not there. The distinction does not reach the
    page, because for a reader all three are the same fact — there is nothing on disk to read yet.

    A pipeline whose artifacts all landed **unreadable** is emphatically not that fact, and returns
    stats carrying no samples and every failure named. It ran; what it wrote cannot be parsed, which
    is the one thing a reader most needs told and the exact opposite of "not run yet".

    The module's cross-checks run here, over the samples that were **read** — so a corrupt artifact
    costs its own row and its own findings and nothing else, and a partial pipeline is cross-checked
    on what landed rather than made to wait for a full plate.

    A module with a **fan-in artifact** is read from both, and the join is a **union**: a sample is
    reported if EITHER source has it. A cell whose ``Log.final.out`` is missing but whose row is in
    the plate object was counted — it has fates, a fragment count and a matrix column — and dropping
    it would report a plate as thinner than the object on disk says it is. ``n_found`` therefore
    counts the samples one source or the other answered for, which is what "how much landed" means
    once landing can happen twice.
    """
    spec = _SPECS.get(module)
    if spec is None or not results_dir.is_dir():
        return None

    per_sample: dict[str, SampleStats] = {}
    notes: list[str] = []
    for sample in samples:
        path = results_dir / sample / spec.artifact.format(sample=sample)
        if not path.is_file():
            continue
        try:
            per_sample[sample] = spec.read(path, sample)
        except _UNREADABLE as exc:
            notes.append(f"{sample}: its QC artifact could not be read ({type(exc).__name__})")

    # Read once, whatever the plate's size, and merged per sample below. A sample's two sources are
    # two halves of ONE row and not two rows: the alignment log says what STAR did with this cell's
    # reads, the plate object says what the counter then did with its fragments, and a page carrying
    # them as separate rows would be a page where every cell appears twice.
    fan_in = _read_fan_in(module, spec, results_dir, samples, notes)
    found: list[SampleStats] = []
    for sample in samples:
        landed, counted = per_sample.get(sample), fan_in.get(sample)
        if landed is not None and counted is not None:
            landed = landed.model_copy(update={"metrics": [*landed.metrics, *counted.metrics]})
        # Either source alone is a row. Contracted order is kept by walking `samples` rather than by
        # appending as each source answers, so a cell the fan-in alone knows about sits where it
        # belongs on the page instead of after every cell that also had a log of its own.
        row = landed if landed is not None else counted
        if row is not None:
            found.append(row)

    # Nothing found AND nothing unreadable is the only "there is nothing on disk" case. Nothing found
    # WITH notes is a pipeline that ran and wrote bytes nobody can parse — and returning `None` for it
    # put the page's "has not been run yet" sentence over a run that had, dropping the per-sample
    # failures on the floor at exactly the moment they were the whole story. A corrupt artifact is
    # meant to cost its own row; when every artifact is corrupt it must not cost the section.
    if not found and not notes:
        return None

    # First-seen order across samples, so the column set is the adapter's declared order and a sample
    # missing one metric leaves a gap in that column instead of dropping it for everyone.
    columns: list[tuple[str, str]] = []
    seen: set[str] = set()
    for stats in found:
        for metric in stats.metrics:
            if metric.key not in seen:
                seen.add(metric.key)
                columns.append((metric.key, metric.label))

    # No "N of M finished" note here, though the counts below carry the fact: `n_found`/`n_expected`
    # are on the object and the reader renders them, so a note saying it again in words is one fact
    # twice — a heading and its own small print. Counts are the data; the sentence is the view's.
    #
    # And no sample's own caption either. It used to be folded in here as well, which made `notes` two
    # kinds of thing in one `list[str]`: an artifact nobody could parse, and the caption a sample that
    # WAS parsed carries. The reader then had to tell them apart again — by matching note strings back
    # against `SampleStats.note` — so rewording a caption in an adapter would silently stop the match,
    # and the caption would reappear as a read failure. A distinction this function knows is not one a
    # consumer should have to reconstruct across a package seam. It stays on the sample that holds it.

    return PipelineStats(
        module=module,
        n_expected=len(samples),
        n_found=len(found),
        samples=found,
        columns=columns,
        notes=notes,
        findings=[f for stats in found for check in spec.checks for f in check(stats)],
    )


def modules_with_stats() -> list[str]:
    """Registered modules this reader can report on — the drift guard's other half."""
    return sorted(_SPECS)


def modules_with_cross_checks() -> list[str]:
    """Registered modules that cross-check what they read — the second guard's other half."""
    return sorted(module for module, spec in _SPECS.items() if spec.checks)


def _check_registry() -> None:
    """Every registered **Workflow module** either reports or is named as not reporting — and the
    same, once more, for whether it cross-checks what it reports.

    Called by the test suite, not at import: a registry mismatch is a build-time defect, and raising
    here would take down ``seqforge report`` for a dataset that has nothing to do with the new module.

    The cross-check half is the stats half again, one level in, and for the same failure: a module
    that neither declares rules nor declares it has none is silently absent from every diagnosis, and
    a page that names no decision looks exactly like a page whose run was fine. Declaring **both** is
    refused as well — a module listed as having no rules while shipping one is two answers to one
    question, and the list is the one a reader would trust.
    """
    unaccounted = sorted(set(MODULES) - set(_SPECS) - MODULES_WITHOUT_STATS)
    if unaccounted:
        raise AssertionError(
            f"workflow module(s) {unaccounted} have no StatsSpec and are not in "
            f"MODULES_WITHOUT_STATS — add a reader, or say out loud that they report nothing"
        )
    unknown = sorted(
        (set(_SPECS) | MODULES_WITHOUT_STATS | MODULES_WITHOUT_CROSS_CHECKS) - set(MODULES)
    )
    if unknown:
        raise AssertionError(f"stats registered for unknown module(s) {unknown}")

    silent = set(MODULES_WITHOUT_CROSS_CHECKS)
    unchecked = sorted(m for m in _SPECS if not _SPECS[m].checks and m not in silent)
    if unchecked:
        raise AssertionError(
            f"workflow module(s) {unchecked} declare no cross-checks and are not in "
            f"MODULES_WITHOUT_CROSS_CHECKS — add a rule, or say out loud that they have none"
        )
    both = sorted(m for m in _SPECS if _SPECS[m].checks and m in silent)
    if both:
        raise AssertionError(
            f"workflow module(s) {both} both declare cross-checks and are named as having none"
        )

    # A plural reader with no artifact to point it at reads nothing, forever and silently: the
    # filename's owner is the module, so a spec declaring `read_fan_in` for a module that declares no
    # `fan_in_artifact` is a reader that can never be handed a path. Caught here rather than at
    # report time for the reason this whole function is: it is a build-time defect, and `_read_fan_in`
    # treats the same combination as a no-op so a report for an unrelated dataset does not fail.
    unpointed = sorted(m for m, s in _SPECS.items() if s.read_fan_in and not _fan_in_artifact(m))
    if unpointed:
        raise AssertionError(
            f"workflow module(s) {unpointed} register a fan-in reader while declaring no "
            f"fan_in_artifact — the module owns that filename, so there is nothing to read"
        )


def _fan_in_artifact(module: str) -> str | None:
    """The registered module's declared fan-in artifact, or ``None`` for one that has none.

    Through :data:`MODULES` rather than :func:`get_module` so the guard above reads whatever the
    registry has been rebound to in a test, which is how the drift tests exercise it without
    touching the shipped one.
    """
    known = MODULES.get(module)
    return None if known is None else known.fan_in_artifact


__all__ = [
    "MODULES_WITHOUT_CROSS_CHECKS",
    "MODULES_WITHOUT_STATS",
    "CrossCheck",
    "StatsSpec",
    "modules_with_cross_checks",
    "modules_with_stats",
    "read_pipeline_stats",
]
