"""Bundle STARsolo's scattered stats + run logs into one gzipped JSON per sample.

This is a finalize step of ``map/starsolo``: once ``<sample>.h5ad`` captures the counts, STAR's small
per-feature stat files, its knee-plot vectors, its run logs, and its splice-junction table are all
that is worth keeping — and they are worth keeping, for a future experiment-level QC pass. STAR
scatters them across ``Solo.out/<Feature>/`` and the sample directory as a dozen little text files;
this collapses them into **one** self-describing ``<sample>.qc.json.gz`` and lets the rule that calls
it ``temp()``-delete the originals.

JSON (gzipped), not pickle, on purpose: a QC corpus that outlives any one code version must not be
readable only by the exact class that wrote it. Text is portable, diffable, and language-agnostic;
gzip absorbs the one bulky field (``UMIperCellSorted``, one integer per barcode).

Like ``h5ad.py`` this shells out through a ``seqforge io`` verb rather than a Snakemake ``run:`` block
so ``snakemake -n -p`` (compose's wiring gate) can see the command.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..models.processing import SoloFeature
from .h5ad import STAR_FINAL_LOG, _gene_axis, _stackable
from .metrics import Metric, SampleStats, count, fraction, knee_points

#: What ``rule qc_bundle`` names its output, per sample: ``<sample>.qc.json.gz``. Here rather than in
#: ``starsolo.smk`` so the rule and the post-run reader both consume the name instead of restating it
#: — the same discipline ``h5ad_suffixes`` keeps for the deliverables, and for the same reason: a
#: suffix spelled in the rule and again in the reader is two owners, and the reader's copy fails
#: silently (a report that finds nothing looks exactly like a pipeline that has not run). The shipped
#: module imports it as of ``WORKFLOW_VERSION`` 2026.8.3, which is why this is public and why it is
#: worth keeping public: adopting it there cost a version bump and an invalidated ``run_id`` for a
#: change that altered no behaviour, and a repo-wide check now refuses the literal's return.
QC_SUFFIX = ".qc.json.gz"


class QcError(RuntimeError):
    """A file STAR was supposed to write is missing or unreadable, so the bundle cannot be built."""


def _read(path: Path) -> str:
    if not path.exists():
        raise QcError(f"{path} is missing; the STAR run that should have written it did not")
    return path.read_text()


def _coerce(value: str) -> object:
    """A stat value as the narrowest type it cleanly is: ``int``, then ``float``, else the raw string.

    STAR mixes integers (read counts), floats (rates), and strings (``95.5%``, timestamps) freely in
    these files. Coercing the clean cases keeps the JSON queryable; leaving the rest as strings keeps
    it lossless — no value is reshaped into a number it is not.
    """
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _parse_kv(text: str, sep: str) -> dict[str, object]:
    """``key<sep>value`` lines -> dict. Blank lines skipped; a line without ``sep`` is dropped."""
    out: dict[str, object] = {}
    for line in text.splitlines():
        if not line.strip() or sep not in line:
            continue
        key, value = line.split(sep, 1)
        out[key.strip()] = _coerce(value.strip())
    return out


def _parse_whitespace_kv(text: str) -> dict[str, object]:
    """``name   value`` (STAR's ``.stats`` files) -> dict, first token key, remainder value."""
    out: dict[str, object] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        out[parts[0]] = _coerce(parts[1]) if len(parts) == 2 else " ".join(parts[1:])
    return out


def _parse_log_final(text: str) -> dict[str, object]:
    """``Log.final.out``: ``   Label |\\tvalue`` lines, with section headers (no ``|``) skipped."""
    return _parse_kv(text, "|")


def _parse_umi_per_cell(text: str) -> list[int]:
    """``UMIperCellSorted.txt``: one integer per line, already descending. The knee-plot data."""
    return [int(line) for line in text.splitlines() if line.strip()]


def _parse_sj(text: str) -> list[list[str]]:
    """``SJ.out.tab``: tab-separated collapsed splice junctions, one list of columns per row."""
    return [line.split("\t") for line in text.splitlines() if line.strip()]


def _read_lines(path: Path) -> list[str]:
    return [line for line in _read(path).splitlines() if line.strip()]


def build_qc_bundle(
    solo_dir: Path,
    run_dir: Path,
    features: Sequence[SoloFeature],
    *,
    sample: str,
    assembly: str | None,
) -> dict[str, object]:
    """Every STAR stat/log for one sample, as one JSON-serialisable dict.

    ``solo_dir`` is the sample's ``Solo.out``; ``run_dir`` is the sample directory holding the
    top-level logs. ``assembly`` is recorded for CRAM-reference provenance (the ``<sample>.cram`` in
    the same directory pins the exact reference bytes by MD5, and this names which assembly that is).
    """
    bundle: dict[str, object] = {
        "sample": sample,
        "assembly": assembly,
        "soloFeatures": list(features),
        "barcodes_stats": _parse_whitespace_kv(_read(solo_dir / "Barcodes.stats")),
        "summary": {
            feat: _parse_kv(_read(solo_dir / feat / "Summary.csv"), ",") for feat in features
        },
        "features_stats": {
            feat: _parse_whitespace_kv(_read(solo_dir / feat / "Features.stats"))
            for feat in features
        },
        "umi_per_cell": {
            feat: _parse_umi_per_cell(_read(solo_dir / feat / "UMIperCellSorted.txt"))
            for feat in _stackable(features)
        },
        # What STAR's default cell filter called -- kept because we drop the filtered matrix (the
        # h5ad is built from raw/), and this tiny list is the only surviving record of that call.
        "default_filtered_barcodes": {
            feat: _read_lines(solo_dir / feat / "filtered" / "barcodes.tsv")
            for feat in _gene_axis(features)
        },
        "log_final": _parse_log_final(_read(run_dir / STAR_FINAL_LOG)),
        "log_out": _read(run_dir / "Log.out"),
        "log_progress": _read(run_dir / "Log.progress.out"),
        "splice_junctions": _parse_sj(_read(run_dir / "SJ.out.tab")),
    }
    return bundle


def write_qc_bundle(
    solo_dir: Path,
    run_dir: Path,
    features: Sequence[SoloFeature],
    out: Path,
    *,
    sample: str,
    assembly: str | None = None,
) -> Path:
    """Build the bundle and write it as gzipped JSON to ``out``. Returns ``out``."""
    bundle = build_qc_bundle(solo_dir, run_dir, features, sample=sample, assembly=assembly)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(bundle, fh)
    return out


# ---- reading the bundle back --------------------------------------------------------------------
#
# The other half of the format contract, deliberately in this file. `build_qc_bundle` above decides
# which STAR file lands under which key; everything below looks those keys up. Split across two
# modules they would drift silently — a renamed key would keep writing and quietly stop reading, and
# the page would lose a metric with nothing failing. Here, one file changes or one file breaks.


def _as_number(value: object) -> float | None:
    """A bundle value as a number, tolerating STAR's percent strings. Anything else -> ``None``.

    ``_coerce`` above keeps ``"95.50%"`` a string because it is not cleanly a float, which is the
    right call for a lossless archive and the wrong shape for a metric. Percent strings are STAR's
    ``Log.final.out`` convention (``Summary.csv`` uses bare 0–1 fractions), so this is where the two
    conventions meet — and both come out as a fraction, so a threshold means one thing everywhere.
    """
    if isinstance(value, bool):  # bool is an int; a flag is not a measurement
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("%"):
            try:
                return float(text[:-1]) / 100.0
            except ValueError:
                return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


#: Which ``--soloFeatures`` feature the headline metrics are read from, most-preferred first. STAR
#: writes one ``Summary.csv`` per feature and they disagree by design (``Gene`` is exonic,
#: ``GeneFull*`` counts introns too), so the page has to name which one it is showing rather than
#: silently pick whichever the dict yielded. ``SJ`` and ``Velocyto`` are absent: neither has the
#: cell-level summary these metrics come from.
_FEATURE_PREFERENCE: tuple[str, ...] = (
    "Gene",
    "GeneFull_Ex50pAS",
    "GeneFull_ExonOverIntron",
    "GeneFull",
)


#: Features that have a ``Summary.csv`` but no cell-level rows in it, so none of the metrics below
#: resolve against them. Excluded from the fallback as well as the preference: picking ``SJ`` because
#: it sorted first yields zero metrics *and* a note claiming they were "counted from the SJ feature"
#: — a caption for a table that is not there, which is worse than the empty table alone.
_NO_CELL_SUMMARY: frozenset[str] = frozenset({"SJ", "Velocyto"})


def _pick_feature(summary: Mapping[str, Any]) -> str | None:
    """The feature whose ``Summary.csv`` the headline metrics come from, or ``None`` if there is none."""
    for feature in _FEATURE_PREFERENCE:
        if feature in summary:
            return feature
    # A feature we have no preference for is still better than no metrics — but only one that could
    # carry them. `sorted`, so an unfamiliar feature set resolves deterministically rather than by
    # dict order; two renders of the same bundle must produce the same page.
    return next(iter(sorted(set(summary) - _NO_CELL_SUMMARY)), None)


def _summary_get(summary: Mapping[str, Any], feature: str, template: str) -> float | None:
    """One ``Summary.csv`` row, by a template whose ``{f}`` is the feature word.

    STAR substitutes the feature name into those labels (``Reads Mapped to GeneFull: Unique
    GeneFull``), so the template is resolved against the feature first and against the literal
    ``Gene`` second — a fallback rather than a guess, because an unresolved key yields ``None`` and
    the metric is then simply absent from the page.
    """
    rows = summary.get(feature)
    if not isinstance(rows, Mapping):
        return None
    for word in (feature, "Gene"):
        value = rows.get(template.format(f=word))
        if value is not None:
            return _as_number(value)
    return None


def alignment_metrics(log_final: Mapping[str, Any]) -> list[Metric]:
    """STAR's ``Log.final.out`` as graded metrics — the half that is **not** single-cell.

    Split out of :func:`metrics` rather than inlined into it because "what STAR's alignment log says"
    is not a STARsolo fact: the bundle folds that log in verbatim, and the bulk module writes the very
    same file with no bundle around it. It has two callers for exactly that reason — :func:`metrics`,
    which reads it out of the bundle, and :func:`read_star_log`, which reads it straight off disk — so
    a threshold cannot be tightened for one pipeline and left behind in the other.

    The thresholds are deliberately loose. Unique-mapping rate varies with genome quality, read
    length and rRNA content far more than with anything seqforge decided, so these are set to catch
    the *decision* failures (wrong assembly, wrong species, unclipped adapter) and not to grade a
    library — a bar tight enough to flag ordinary biology is a bar that gets ignored.
    """
    unique = _as_number(log_final.get("Uniquely mapped reads %"))
    multi = _as_number(log_final.get("% of reads mapped to multiple loci"))
    too_many = _as_number(log_final.get("% of reads mapped to too many loci"))
    too_short = _as_number(log_final.get("% of reads unmapped: too short"))
    built = [
        count(
            "input_reads",
            "Input reads",
            _as_number(log_final.get("Number of input reads")),
            group="input",
            hint="How many read pairs STAR was handed. Compare it with what you expected to sequence.",
        ),
        fraction(
            "uniquely_mapped",
            "Uniquely mapped",
            unique,
            group="alignment",
            ok=0.60,
            warn=0.35,
            hint="Share of reads placed at exactly one locus. A low value usually means the wrong "
            "genome assembly or species, not a bad library.",
            headline=True,
        ),
        fraction(
            "multi_loci",
            "Multi-mapped",
            multi,
            group="alignment",
            hint="Reads placed at several loci — repeats and gene families. Informational.",
        ),
        fraction(
            "too_many_loci",
            "Mapped to too many loci",
            too_many,
            group="alignment",
            ok=0.10,
            warn=0.30,
            higher_is_better=False,
            hint="Reads matching more places than STAR will report. A high value points at rRNA or "
            "another repetitive contaminant.",
        ),
        fraction(
            "unmapped_too_short",
            "Unmapped: too short",
            too_short,
            group="alignment",
            ok=0.20,
            warn=0.45,
            higher_is_better=False,
            hint="STAR's catch-all for reads it could not extend into an alignment — adapter "
            "read-through, or reads from a genome this is not.",
            headline=True,
        ),
    ]
    return [m for m in built if m is not None]


def metrics(bundle: Mapping[str, Any], sample: str) -> SampleStats:
    """A ``<sample>.qc.json.gz`` bundle -> the normalised metrics for one finished STARsolo sample.

    **Pure**: a ``Mapping`` in, a :class:`~seqforge.workflows.metrics.SampleStats` out, no filesystem.
    That is the internal seam this module's tests drive — the metric table and its thresholds are
    checked against literal dicts, and only :func:`read_metrics` needs a file on disk.

    Every lookup degrades to an absent metric rather than a zero. A bundle from an older
    ``WORKFLOW_VERSION``, or one STAR wrote with a feature set we do not prefer, renders fewer rows;
    it never renders a wrong one.
    """
    summary = bundle.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    feature = _pick_feature(summary)

    built: list[Metric | None] = []
    if feature is not None:
        built += [
            count(
                "reads",
                "Reads",
                _summary_get(summary, feature, "Number of Reads"),
                group="input",
                hint="Reads STARsolo processed for this sample.",
                headline=True,
            ),
            fraction(
                "valid_barcodes",
                "Valid barcodes",
                _summary_get(summary, feature, "Reads With Valid Barcodes"),
                group="barcode",
                ok=0.75,
                warn=0.50,
                hint="Share of reads whose cell barcode matched the kit's whitelist. THE check on "
                "the chemistry call: a near-zero value means the wrong kit was identified, or the "
                "barcode and cDNA reads are the wrong way round.",
                headline=True,
            ),
            fraction(
                "reads_in_genes",
                "Reads in genes",
                _summary_get(summary, feature, "Reads Mapped to {f}: Unique {f}"),
                group="counts",
                ok=0.30,
                warn=0.15,
                hint="Share of reads assigned to an annotated gene. High genome mapping with low "
                "gene mapping points at the wrong annotation (GTF) or the wrong strand setting.",
                headline=True,
            ),
            fraction(
                "reads_in_genome",
                "Reads in genome",
                _summary_get(summary, feature, "Reads Mapped to Genome: Unique"),
                group="alignment",
                hint="Uniquely mapped share as STARsolo counts it. Read it against 'Reads in genes' "
                "— a large gap is an annotation or strand problem, not a mapping one.",
            ),
            count(
                "cells",
                "Estimated cells",
                _summary_get(summary, feature, "Estimated Number of Cells"),
                group="cells",
                exact=True,
                hint="Barcodes STAR's cell filter called real. No threshold is possible here — only "
                "you know how many cells were loaded.",
                headline=True,
            ),
            fraction(
                "reads_in_cells",
                "Reads in cells",
                _summary_get(summary, feature, "Fraction of Unique Reads in Cells"),
                group="cells",
                ok=0.70,
                warn=0.50,
                hint="Share of gene-assigned reads that landed in called cells. A low value means "
                "ambient RNA or dying cells — a lot of the library is background.",
                headline=True,
            ),
            count(
                "median_umi",
                "Median UMI / cell",
                _summary_get(summary, feature, "Median UMI per Cell"),
                group="cells",
                exact=True,
                ok=500,
                warn=200,
                hint="Depth per cell. Low values mean an under-sequenced or low-input library.",
            ),
            count(
                "median_genes",
                "Median genes / cell",
                _summary_get(summary, feature, "Median {f} per Cell"),
                group="cells",
                exact=True,
                ok=500,
                warn=200,
                hint="Genes detected in the median cell.",
            ),
            count(
                "genes_detected",
                "Genes detected",
                _summary_get(summary, feature, "Total {f} Detected"),
                group="counts",
                exact=True,
                hint="Distinct genes with at least one count across all cells.",
            ),
            fraction(
                "saturation",
                "Sequencing saturation",
                _summary_get(summary, feature, "Sequencing Saturation"),
                group="duplication",
                hint="Share of reads that were a repeat of a molecule already seen. Not a pass/fail "
                "— it says whether sequencing deeper would find anything new.",
            ),
            fraction(
                "q30_cb_umi",
                "Q30 in CB+UMI",
                _summary_get(summary, feature, "Q30 Bases in CB+UMI"),
                group="barcode",
                ok=0.85,
                warn=0.70,
                hint="Base quality in the barcode read. Poor quality here costs barcode matches.",
            ),
            fraction(
                "q30_rna",
                "Q30 in cDNA",
                _summary_get(summary, feature, "Q30 Bases in RNA read"),
                group="alignment",
                ok=0.80,
                warn=0.65,
                hint="Base quality in the cDNA read.",
            ),
        ]

    log_final = bundle.get("log_final")
    solo = [m for m in built if m is not None]
    alignment = alignment_metrics(log_final) if isinstance(log_final, Mapping) else []
    # STARsolo's own "Reads" already reports the input count, so STAR's duplicate is dropped rather
    # than shown twice under two names — two columns of one number reads as two facts.
    alignment = [m for m in alignment if m.key != "input_reads"] if solo else alignment

    umi = bundle.get("umi_per_cell")
    vector: list[int] = []
    if isinstance(umi, Mapping) and feature is not None:
        raw = umi.get(feature)
        if isinstance(raw, list):
            vector = [int(v) for v in raw if isinstance(v, int | float)]

    note = f"counted from the {feature} feature" if feature else ""
    return SampleStats(
        sample_id=sample,
        metrics=solo + alignment,
        knee=knee_points(vector),
        note=note,
    )


def read_metrics(path: Path, sample: str) -> SampleStats:
    """Load one ``<sample>.qc.json.gz`` and normalise it. Raises ``OSError``/``ValueError`` if unusable.

    The thin half of the adapter: the loading lives here so the registry hands over a path and gets
    metrics back, and the judgement lives in :func:`metrics`, which needs no file to test.
    """
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        bundle = json.load(fh)
    if not isinstance(bundle, Mapping):
        raise ValueError(f"{path} is not a QC bundle object")
    return metrics(bundle, sample)


def read_star_log(path: Path, sample: str) -> SampleStats:
    """``map/star``'s adapter: STAR's own ``Log.final.out``, with no bundle in between.

    Here rather than in ``stats.py`` because this file already owns both halves of "what STAR's
    alignment log says" — :func:`_parse_log_final` puts that log into the bundle and
    :func:`alignment_metrics` reads it back out — and the bulk pipeline wants exactly those two
    composed. Bulk has no barcodes, no cells and no knee vector, so there is nothing else to add: the
    adapter is one line, and that is the point rather than an omission.

    The alternative was a ``qc_bundle``-shaped rule for ``map/star``, which would have given both
    pipelines one artifact shape to read. It was rejected because STAR writes this file unasked and
    nothing in ``star.smk`` declares or deletes it: reading it as it lies means bulk reports with no
    rule change, hence no ``WORKFLOW_VERSION`` bump, hence no ``run_id`` invalidation and no
    reprocessing of anything already compiled. That is what a
    :class:`~seqforge.workflows.stats.StatsSpec` carrying a *filename* rather than a suffix buys, and
    this is the artifact it was shaped for.

    Raises ``OSError``/``ValueError`` if the bytes are unusable, like both siblings above, so one bad
    file costs its own row and not the whole pipeline. Far less can go wrong here than with a gzipped
    JSON, and deliberately so: a text log a killed job truncated mid-write still parses, and the
    metrics its missing lines would have carried are simply absent rather than a row of zeros.
    """
    return SampleStats(
        sample_id=sample, metrics=alignment_metrics(_parse_log_final(path.read_text()))
    )


__all__ = [
    "QC_SUFFIX",
    "QcError",
    "alignment_metrics",
    "build_qc_bundle",
    "metrics",
    "read_metrics",
    "read_star_log",
    "write_qc_bundle",
]
