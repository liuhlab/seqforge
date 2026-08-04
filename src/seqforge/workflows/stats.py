"""Read a finished pipeline's per-sample QC artifacts — one interface, one adapter per module.

``seqforge report`` is a reader: it renders what is on disk and decides nothing. Once the composed
Snakefile has run, what is on disk gains a per-sample QC artifact, and this module is how the report
gets at it — for **any** **Workflow module**, without the report ever learning what STARsolo is.

The seam is :class:`StatsSpec`: where one sample's artifact lives, and how to turn it into the shared
:class:`~seqforge.workflows.metrics.SampleStats`. Every shipped module is wired::

    map/starsolo   <sample>.qc.json.gz             gzipped JSON, written by `rule qc_bundle`
    map/chromap    <sample>.fragments.qc.json.gz   gzipped JSON, written by `rule fragments_qc`
    map/star       Log.final.out                   plain text, written by STAR itself

Three artifacts, three vocabularies, and no shared column set — the ATAC summary has no
whitelist-match rate and no per-barcode vector, so an scATAC page speaks about fragments and never
about cells, and a bulk page speaks about mapping and never about barcodes. That divergence is the
seam earning its keep: it is expressed as three adapters rather than as a widening union of optional
fields on one.

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

from . import MODULES
from .fragments import QC_SUFFIX as _FRAGMENTS_QC_SUFFIX
from .fragments import read_metrics as _read_fragments
from .h5ad import STAR_FINAL_LOG
from .metrics import Finding, PipelineStats, SampleStats
from .qc import QC_SUFFIX as _STARSOLO_QC_SUFFIX
from .qc import chemistry_rule as _starsolo_chemistry_rule
from .qc import read_metrics as _read_starsolo
from .qc import read_star_log as _read_star_log
from .qc import solo_features_rule as _starsolo_solo_features_rule

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
    """

    artifact: str
    read: Callable[[Path, str], SampleStats]
    checks: tuple[CrossCheck, ...] = ()


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
        checks=(_starsolo_chemistry_rule, _starsolo_solo_features_rule),
    ),
    "map/chromap": StatsSpec(artifact=f"{{sample}}{_FRAGMENTS_QC_SUFFIX}", read=_read_fragments),
    "map/star": StatsSpec(artifact=STAR_FINAL_LOG, read=_read_star_log),
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
MODULES_WITHOUT_CROSS_CHECKS: frozenset[str] = frozenset({"map/chromap", "map/star"})

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
    """
    spec = _SPECS.get(module)
    if spec is None or not results_dir.is_dir():
        return None

    found: list[SampleStats] = []
    notes: list[str] = []
    for sample in samples:
        path = results_dir / sample / spec.artifact.format(sample=sample)
        if not path.is_file():
            continue
        try:
            found.append(spec.read(path, sample))
        except _UNREADABLE as exc:
            notes.append(f"{sample}: its QC artifact could not be read ({type(exc).__name__})")

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
    for note in sorted({s.note for s in found if s.note}):
        notes.append(note)

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


__all__ = [
    "MODULES_WITHOUT_CROSS_CHECKS",
    "MODULES_WITHOUT_STATS",
    "CrossCheck",
    "StatsSpec",
    "modules_with_cross_checks",
    "modules_with_stats",
    "read_pipeline_stats",
]
