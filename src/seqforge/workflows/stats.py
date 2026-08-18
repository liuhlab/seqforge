"""Read a finished pipeline's per-sample QC artifacts — one interface, one adapter per module.

``seqforge report`` is a reader: it renders what is on disk and decides nothing. Once the composed
Snakefile has run, what is on disk gains a per-sample QC artifact, and this module is how the report
gets at it — for **any** **Workflow module**, without the report ever learning what STARsolo is.

The seam is :class:`StatsSpec`: where one sample's artifact lives, and how to turn it into the shared
:class:`~seqforge.workflows.metrics.SampleStats`. Every shipped module is wired::

    map/starsolo           <sample>.qc.json.gz             gzipped JSON, by `rule qc_bundle`
    map/chromap            <sample>.fragments.qc.json.gz   gzipped JSON, by `rule fragments_qc`
    map/star               Log.final.out                   plain text, written by STAR itself
    map/star-umi           <sample>.qc.json.gz             one per CELL, by that module's own
                                                           `rule qc_bundle`: the extraction summary,
                                                           the alignment log and a junction summary
                           + the fan-in artifact           one h5ad over the plate, one row per cell
    map/star-umi-chimera   <sample>.qc.json.gz             the same, plus the split summary
                           and NO fan-in reader            argued on the entry itself

Five artifacts, five vocabularies, and no shared column set — the ATAC summary has no
whitelist-match rate and no per-barcode vector, so an scATAC page speaks about fragments and never
about cells, a bulk page speaks about mapping and never about barcodes, and a plate's counting object
speaks about fragments that reached no gene, which none of the other three measured. That divergence
is the seam earning its keep: it is expressed as five adapters rather than as a widening union of
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

**A module's per-sample artifacts are PLURAL, and a chain may COLLAPSE them into one** — which is
what the plate twins do, and why the field stays a tuple over a registry where every entry is now a
single name. They used to read two files and three: STAR's own alignment log,
the summary the extraction wrote a step earlier, and on a Chimera the split's account of what left.
All of it is now folded into one bundle per cell by a rule downstream of every step that wrote a
piece of it, so a cell's QC is one file a reader opens and one entry here. :attr:`StatsSpec.artifacts`
stays a tuple: a module whose chain leaves two durable accounts states two filenames rather than
having one adapter open a sibling, and a sample missing one keeps the other.

**One module reads a SECOND artifact, and what is new about that one is its ARITY, not its name.**
``map/star-umi`` counts its whole plate in one job and writes one ``.h5ad`` whose ``obs`` carries
every cell's read fates — a **fan-in artifact**: dataset-scoped as a file, sample-scoped as data. A
per-sample reader cannot express it, since there is no sample in its path; so the spec's last
reader is plural in the other direction (:attr:`StatsSpec.read_fan_in`), handed the file and the
sample list once and returning one :class:`SampleStats` per row, which :func:`read_pipeline_stats`
merges into what the per-sample artifacts said. Its filename is deliberately **not** a field on the
spec: ``Workflow.fan_in_artifact`` already declares it and the rule that produces it reads that same
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
from .qc import QC_SUFFIX as _QC_SUFFIX
from .qc import chemistry_rule as _starsolo_chemistry_rule
from .qc import gene_model_rule as _starsolo_gene_model_rule
from .qc import read_metrics as _read_starsolo
from .qc import read_plate_metrics as _read_plate_cell
from .qc import read_star_log as _read_star_log
from .qc import solo_features_rule as _starsolo_solo_features_rule
from .umite.count import read_plate_stats as _read_plate_stats

#: One cross-check rule: one sample's metrics in, zero or more :class:`Finding` out. Pure by
#: signature — there is no path, no manifest and no writer in it — which is what makes a threshold
#: testable against literal values, and what makes "advisory" a property of the type rather than a
#: promise in a docstring.
CrossCheck = Callable[[SampleStats], list[Finding]]


@dataclass(frozen=True)
class SampleArtifact:
    """One per-sample file a module's pipeline leaves behind, and how to turn it into metrics.

    ``filename`` sits under ``<results>/<sample>/``; ``{sample}`` is substituted. ``read`` owns
    loading as well as parsing, so the loop below hands over a path and gets metrics back and never
    has to know whether the bytes were gzipped JSON, plain JSON or text. Each adapter keeps a
    **pure** ``Mapping -> SampleStats`` function underneath (``qc.metrics``), which is the internal
    seam its tests drive — no filesystem needed to check a threshold.

    A pair rather than two parallel tuples on the spec, because the filename and the code that can
    read those bytes are one fact: split across two lists they can go out of step by one, and the
    failure is a reader handed a file it does not understand.

    ``finishes`` is whether this file LANDING means the pipeline is done with this sample. True for
    a module's terminal artifact — and false for an artifact a mid-pipeline rule writes, which is
    evidence ABOUT a sample and not evidence that the sample is finished. ``n_found`` feeds
    ``PipelineStats.complete``, which the page renders as a green "all N samples finished", so an
    artifact written halfway down a chain counted there tints a plate that has not finished one.
    What is shown is still the union of every artifact that landed; only what counts as FINISHED is
    narrower.

    **Every shipped module names one terminal artifact today, and the field is what makes that a
    claim rather than an assumption.** The plate twins used to report from mid-pipeline files and to
    carry ``finishes`` on the aligner's log — which is how a run whose every downstream step failed
    reported every cell finished. Folding those files into one bundle per cell moved the flag onto
    the artifact that is written last; a module that adds a second, earlier account says so here.
    """

    filename: str
    read: Callable[[Path, str], SampleStats]
    finishes: bool = True


@dataclass(frozen=True)
class StatsSpec:
    """How one **Workflow module**'s finished artifacts are found, read, and cross-checked.

    ``artifacts`` is every per-sample file the pipeline leaves behind, in the order their columns
    should appear. One for every shipped module today, which is a fact about those pipelines rather
    than about this type: a chain whose links each leave a durable account states each of them, and a
    sample's metrics are the concatenation of what each artifact that landed gave it, so a missing
    one costs its columns and never the row.

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

    artifacts: tuple[SampleArtifact, ...]
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
        artifacts=(SampleArtifact(f"{{sample}}{_QC_SUFFIX}", _read_starsolo),),
        checks=(
            _starsolo_chemistry_rule,
            _starsolo_gene_model_rule,
            _starsolo_solo_features_rule,
        ),
    ),
    "map/chromap": StatsSpec(
        artifacts=(SampleArtifact(f"{{sample}}{_FRAGMENTS_QC_SUFFIX}", _read_fragments),)
    ),
    "map/star": StatsSpec(artifacts=(SampleArtifact(STAR_FINAL_LOG, _read_star_log),)),
    # The plate module reports from ONE artifact per cell, the same suffix `map/starsolo` reports
    # from and a different shape inside it: a cell IS a sample here, so `<results>/<sample>/` holds
    # one bundle carrying what the extraction saw, what the aligner then did, and a summary of the
    # junctions it called. It used to read STAR's `Log.final.out` where it lay plus the extraction
    # summary beside it; both are now folded in and reclaimed, so a finished plate leaves one file
    # per cell instead of four and a reader looks in one place.
    #
    # **`finishes` is on the bundle, and that is the point rather than a consequence.** It sat on
    # STAR's log, which STAR writes the moment it finishes aligning — so a cell counted as finished
    # while every step after the aligner was still to come, and a run whose split refused for every
    # cell rendered as "all N cells finished" with nothing downstream on disk. The bundle is written
    # by a rule downstream of every per-cell step, so it cannot make that claim early.
    #
    # It is also the only module with a SECOND half, and that half is where its counting decisions
    # are: the fan-in writes every cell's read fates into the combined object's `obs`, and those say
    # what the per-cell bundle cannot — how many fragments reached no gene, and why. They arrive
    # through `read_fan_in` rather than as another `artifacts` entry because that artifact has no
    # sample in its path at all: it is one file for the deposit, holding one row per cell.
    #
    # The fan-in filename is STILL not spelled here, and that is the same discipline as every entry
    # above one arity out: `map/star-umi` DECLARES its deliverable as `fan_in_artifact`,
    # `star-umi.smk` reads that constant to name its output, and `read_pipeline_stats` asks the
    # registry rather than restating it. Three readers, one owner — a rename reaches every one of
    # them or fails at import.
    "map/star-umi": StatsSpec(
        artifacts=(SampleArtifact(f"{{sample}}{_QC_SUFFIX}", _read_plate_cell),),
        read_fan_in=_read_plate_stats,
    ),
    # The chimeric twin: the plate module's spec, one key wider inside the same artifact, and MINUS
    # its fan-in reader.
    #
    # What its bundle gains is the per-cell split summary, read last so the page reads left to right
    # in pipeline order — extraction, then alignment, then the split that follows it. It is
    # **load-bearing rather than decorative**: `unmapped` reads structurally zero in every
    # per-Component matrix, because those records are dropped at the split one rule before the
    # counter, and that summary is where they now live, beside each Component's count of the records
    # placed at more than one locus, which the split marks and keeps. It also carries the number the
    # whole chimera exercise exists to produce — each Component's share of this cell — so a
    # bacterial fraction is readable without opening an `.h5ad`. It reaches the page as a key of the
    # bundle rather than as a second entry here, which is what makes the twin's bundle downstream of
    # its split: on a chimeric run, "N of M finished" counts cells whose split ran.
    #
    # **`read_fan_in` is absent, and that is argued rather than deferred.** This module's
    # `fan_in_artifact` carries a `{component}`, so reporting the fan-in means looping over
    # Components and giving every cell's row N colliding sets of fate keys under per-Component
    # prefixes — and the numbers bought that way are precisely the ones that do not compare across
    # run types: one of the four fates is structurally zero here and every surviving rate, the
    # headline one included, rides a smaller denominator. Rendered beside a single-assembly run's
    # page they are not the same measurement. The stated cost is that a chimeric run's page shows no
    # gene-assignment fates at all; they are in each object's `obs` for anyone who wants them.
    #
    # Declining the READER does not decline the ARTIFACT. Whether each Component's object was written
    # is a fact this module still answers, through the same `fan_in_artifact` declaration and the
    # Component list the recipe composed against — so a chimeric run that counted two organisms of
    # three reports as failed and names the third, while its page stays free of numbers that would
    # not compare.
    "map/star-umi-chimera": StatsSpec(
        artifacts=(SampleArtifact(f"{{sample}}{_QC_SUFFIX}", _read_plate_cell),),
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
#: Every entry is an argument, not backlog. ``map/chromap``'s fragments summary carries no
#: whitelist-match rate and no gene assignment at all, so single-cell RNA reasoning applied to it
#: would be reasoning about numbers that are not there; ``map/star`` is bulk — no barcode, no cell,
#: and its two graded metrics vary with genome quality and rRNA content far more than with anything
#: seqforge decided, which is the same reason their own thresholds are loose. A rule with no
#: defensible threshold does not ship, and declaring that out loud is a supported answer rather than
#: a gap. Any name here leaves this set the day a rule for it can be argued.
#: ``map/star-umi`` joins them on the same argument read off the artifacts rather than off the assay.
#: Its per-cell half is the aligner's own log, folded into that cell's bundle and carrying no
#: barcode-match rate at all, so
#: every barcode rule the droplet module cross-checks with is a number that is not there. Its
#: fan-in half DOES carry a gene-assignment number — the share of fragments landing on no feature —
#: and it still ships no rule, because a rule is a THRESHOLD and nobody has measured one: what share
#: is wrong varies with the annotation's completeness and with how much of a plate library is
#: intronic, and the droplet bar was set on droplet libraries counted a different way. Reporting the
#: number and declining to grade it is the honest state; it leaves this set the day a bar can be
#: argued from a measurement.
#: ``map/star-umi-chimera`` inherits every word of that and adds one of its own: **nobody has
#: measured what share of a worm plate should be *E. coli***, so a threshold on a Component's share
#: would be a figure invented at review — which is exactly what this set exists to refuse. The number
#: is reported, and not graded.
MODULES_WITHOUT_CROSS_CHECKS: frozenset[str] = frozenset(
    {"map/chromap", "map/star", "map/star-umi", "map/star-umi-chimera"}
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


def _merged(held: SampleStats | None, arriving: SampleStats) -> SampleStats:
    """One reading of a sample folded into whatever was already held for it.

    One function for every join in this file, because they are the same join: a sample's artifacts
    are chapters of one row and not rows of their own, and a page carrying them separately would show
    every cell two or three times. Order is arrival order, which is the registry's declared order,
    so the column set below reads in pipeline order rather than in whatever order the files landed.

    Asymmetric on purpose — ``held`` may be nothing and ``arriving`` may not. "Nothing yet, and then
    a reading" is the ordinary first step of a fold, while "a reading of nothing" is not a thing an
    artifact that landed can produce, so the signature says which absence is expected.

    **Metrics merge and nothing else does.** The other fields of a
    :class:`~seqforge.workflows.metrics.SampleStats` are single-valued judgements — which feature the
    numbers came from, the knee vector, the caption — and no module has two artifacts that both speak
    there: every reader but ``qc.read_metrics`` returns an id and a metric list. Code to arbitrate
    between them would be code no test could turn red, which is the reason it is absent rather than
    written and unexercised. A second speaking artifact is a design question, not a merge rule.
    """
    if held is None:
        return arriving
    return held.model_copy(update={"metrics": [*held.metrics, *arriving.metrics]})


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


def _missing_deliverables(module: str, results_dir: Path, components: Sequence[str]) -> list[str]:
    """Which of the module's declared dataset-scoped deliverables are not on disk.

    **Through the declaration, never through a second list.** ``Workflow.fan_in_artifact`` is what
    the module states it produces for the whole deposit and what its ``rule all`` demands by name;
    asking the registry here makes the run state read the same fact the pipeline was contracted on.
    A hand-kept table of "what a finished run looks like" would be a third owner of that name, and
    the failure of one is silent in the direction that matters: a deliverable nobody listed is a
    deliverable nobody notices missing.

    A name carrying a ``{component}`` is expanded once per **Component**, because that is the arity
    the twin's ``rule all`` demands and a run that counted two organisms of three is a run with an
    organism silently absent. An unexpandable name with no Component to expand it — a config that
    lost the key — is reported as missing under its own pattern rather than as nothing owed at all:
    "we cannot say what was demanded" is not "everything was produced".
    """
    artifact = _fan_in_artifact(module)
    if artifact is None:
        return []
    if "{component}" in artifact:
        demanded = [artifact.format(component=c) for c in components] or [artifact]
    else:
        demanded = [artifact]
    return [name for name in demanded if not (results_dir / name).is_file()]


def read_pipeline_stats(
    module: str,
    results_dir: Path,
    samples: Sequence[str],
    components: Sequence[str] = (),
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
    reported if ANY source has it. A cell whose ``Log.final.out`` is missing but whose row is in
    the plate object was counted — it has fates, a fragment count and a matrix column — and dropping
    it would report a plate as thinner than the object on disk says it is.

    **What is SHOWN and what is FINISHED are two questions**, and they came apart the first time a
    module reported from a mid-pipeline artifact. Every source that landed puts its columns on the
    page; only a source that ``finishes`` counts toward ``n_found``, and ``n_found`` is what
    ``complete`` — the "all N samples finished" half of the state — is read off. A cell whose bundle
    is not there is not a finished cell, however much of its chain ran: the twins' bundle is written
    downstream of every per-cell step, so a plate whose split refused is honestly "0 of 1440".

    **And what is FINISHED is still not whether the RUN finished.** Every contracted cell can land
    its alignment log while the object the whole plate fans in to was never written — the run that
    reported every cell done and no matrix at all. ``components`` is what that check needs and the
    only reason it is a parameter: the twin's deliverable is named once per **Component**, so the
    Chimera the recipe composed against decides how many objects were demanded. Empty for every
    per-sample module and for a plain reference, which is the default and the common case.
    """
    spec = _SPECS.get(module)
    if spec is None or not results_dir.is_dir():
        return None

    per_sample: dict[str, SampleStats] = {}
    notes: list[str] = []
    finished: set[str] = set()
    for sample in samples:
        for artifact in spec.artifacts:
            filename = artifact.filename.format(sample=sample)
            path = results_dir / sample / filename
            if not path.is_file():
                continue
            try:
                read = artifact.read(path, sample)
            except _UNREADABLE as exc:
                # Named, because a module with two artifacts has two ways to be unreadable and "its
                # QC artifact" would leave a reader guessing which file to go and look at.
                notes.append(f"{sample}: {filename} could not be read ({type(exc).__name__})")
                continue
            per_sample[sample] = _merged(per_sample.get(sample), read)
            if artifact.finishes:
                finished.add(sample)

    # Read once, whatever the plate's size, and merged per sample below. A sample's two sources are
    # two halves of ONE row and not two rows: the alignment log says what STAR did with this cell's
    # reads, the plate object says what the counter then did with its fragments, and a page carrying
    # them as separate rows would be a page where every cell appears twice.
    fan_in = _read_fan_in(module, spec, results_dir, samples, notes)
    found: list[SampleStats] = []
    for sample in samples:
        # Either source alone is a row. Contracted order is kept by walking `samples` rather than by
        # appending as each source answers, so a cell the fan-in alone knows about sits where it
        # belongs on the page instead of after every cell that also had a log of its own.
        landed, counted = per_sample.get(sample), fan_in.get(sample)
        row = landed if counted is None else _merged(landed, counted)
        if row is not None:
            found.append(row)
        if counted is not None:
            # The fan-in is the last thing the pipeline writes, so a row in it always means finished.
            finished.add(sample)

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
        # How many samples the pipeline FINISHED, which is not how many rows are below: a sample
        # whose only artifact is a mid-pipeline one has real numbers to show and is not done, and
        # `complete` reads the sample half of the run state off this count.
        n_found=len(finished),
        # The other half, and the one no count of samples can reach: what the module said it would
        # leave for the whole deposit, and whether it is there.
        missing_deliverables=_missing_deliverables(module, results_dir, components),
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
    # A spec that names no per-sample artifact reports nothing while looking like it reports — the
    # same silence `MODULES_WITHOUT_STATS` exists to make somebody say out loud. Cheap to write once
    # the field is a tuple, and a tuple is exactly what makes the empty case expressible at all.
    unsourced = sorted(m for m, s in _SPECS.items() if not s.artifacts)
    if unsourced:
        raise AssertionError(
            f"workflow module(s) {unsourced} register a StatsSpec naming no per-sample artifact; "
            f"there is nothing for the reader to open"
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
    "SampleArtifact",
    "StatsSpec",
    "modules_with_cross_checks",
    "modules_with_stats",
    "read_pipeline_stats",
]
