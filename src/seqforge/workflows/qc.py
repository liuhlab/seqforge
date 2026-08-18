"""Bundle one sample's scattered stats + run logs into one gzipped JSON — a droplet one, or a cell's.

This is a finalize step of ``map/starsolo`` and of the two plate twins: once the counts are captured,
the aligner's small stat files, its knee-plot vectors, its run logs and its junctions are all that is
worth keeping — and they are worth keeping, for a future experiment-level QC pass. The aligner
scatters them across ``Solo.out/<Feature>/`` and the sample directory as a dozen little text files;
this collapses them into **one** self-describing ``<sample>.qc.json.gz`` and lets the rule that calls
it ``temp()``-delete the originals.

**Two builders, one suffix, one verb.** A droplet sample and a plate cell leave genuinely different
files behind — one has a barcode whitelist, a cell filter and a knee vector, the other an extraction
summary and, on a Chimera, a split summary — so the two key spaces are two functions rather than one
with optional keys, which would push a "was this a plate?" branch into every reader path. What they
share is the aligner's own run files, and that is a private helper both call rather than two copies.

**The plate bundle summarizes the junctions; the droplet one stores the table.** The same artifact
kind at eighty times the arity is not the same trade-off: a table per sample is small change for ten
droplet samples and roughly a gigabyte for a 784-cell plate, on a file nothing downstream reads. So
the twins keep the counts a junction table can be reduced to and the droplet bundle is unchanged.

JSON (gzipped), not pickle, on purpose: a QC corpus that outlives any one code version must not be
readable only by the exact class that wrote it. Text is portable, diffable, and language-agnostic;
gzip absorbs the one bulky field (``UMIperCellSorted``, one integer per barcode).

Like ``h5ad.py`` this shells out through a ``seqforge io`` verb rather than a Snakemake ``run:`` block
so ``snakemake -n -p`` (compose's wiring gate) can see the command.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, get_args

from ..models.processing import SoloFeature
from .h5ad import (
    STAR_FINAL_LOG,
    STAR_JUNCTIONS,
    STAR_PROGRESS_LOGS,
    _gene_axis,
    _stackable,
)
from .metrics import (
    Finding,
    Metric,
    SampleStats,
    count,
    fmt_pct,
    fraction,
    knee_points,
    sequencing_saturation,
)
from .umite.extract import extract_metrics

#: What every ``rule qc_bundle`` names its output: ``<sample>.qc.json.gz``, per droplet sample and per
#: plate cell. **One suffix for one artifact kind**, which is the same call the single verb behind
#: them is: a reader meeting this name knows it holds one sample's QC and finds out which shape by
#: reading it, rather than by a second suffix somebody has to keep in step with the first. Here rather
#: than in ``starsolo.smk`` so the rule and the post-run reader both consume the name instead of
#: restating it — the same discipline ``h5ad_suffixes`` keeps for the deliverables, and for the same
#: reason: a suffix spelled in the rule and again in the reader is two owners, and the reader's copy
#: fails silently (a report that finds nothing looks exactly like a pipeline that has not run). The
#: shipped module imports it as of ``WORKFLOW_VERSION`` 2026.8.3, which is why this is public and why
#: it is worth keeping public: adopting it there cost a version bump and an invalidated ``run_id``
#: for a change that altered no behaviour, and a repo-wide check now refuses the literal's return.
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


def _payload(path: Path) -> dict[str, object]:
    """One of the plate chain's small JSON summaries, as the mapping a bundle folds in VERBATIM.

    Verbatim, and that is the whole design of the plate bundle: the extraction summary and the split
    summary each have a pure ``payload -> metrics`` reader beside their own writer, so folding the
    payload in unchanged keeps one owner of what each key means and leaves this file owning exactly
    one new key per absorbed artifact. Re-deriving the numbers here would be a second reader of
    somebody else's format, which is the drift this module is arranged to prevent.
    """
    payload = json.loads(_read(path))
    if not isinstance(payload, dict):
        raise QcError(f"{path} is not a JSON object; the step that wrote it did not finish")
    return payload


def _star_run_files(run_dir: Path) -> dict[str, object]:
    """The aligner's end-of-run summary and its two progress logs — the half both bundles share.

    A droplet sample and a plate cell hold genuinely different things, but every ``alignReads`` run
    writes these three the same way, so they are parsed once here rather than in two builders that
    could come to disagree about what a run log is. The filenames belong to the aligner-log constants
    and are imported rather than spelled; the KEYS are this file's, because renaming one of those is
    a change to the artifact's format.
    """
    parameter_dump, speed_table = STAR_PROGRESS_LOGS
    return {
        "log_final": _parse_log_final(_read(run_dir / STAR_FINAL_LOG)),
        "log_out": _read(run_dir / parameter_dump),
        "log_progress": _read(run_dir / speed_table),
    }


#: The ``SJ.out.tab`` columns the plate's junction summary reads, 0-based: the intron motif (``0`` is
#: non-canonical), whether the junction is in the index's annotation, and the two read counts crossing
#: it. Named because a bare ``fields[5]`` is unreadable and because a row shorter than the last of
#: them is a row this cannot summarize.
_SJ_MOTIF, _SJ_ANNOTATED, _SJ_UNIQUE, _SJ_MULTI = 4, 5, 6, 7


def _summarise_sj(text: str) -> dict[str, int]:
    """``SJ.out.tab`` reduced to the five counts a plate cell's junctions are worth keeping as.

    **A summary and not the table**, which is the one deliberate asymmetry between the two bundles.
    A cell at ~1M reads calls junctions nobody can analyze one cell at a time, and 784 of those
    tables is roughly a gigabyte of a plate's deliverable that no reader opens; these counts are what
    survives that, and they still answer the question a per-cell junction table was there for — how
    much splicing this cell showed, and how much of it the annotation already knew.

    A row too short to carry those columns, or carrying a value that is not an integer, is skipped
    rather than raised on: this runs after the aligner exited 0, so a malformed row is a file some
    other process touched, and losing one row's counts beats losing the bundle.
    """
    junctions = annotated = canonical = unique_reads = multi_reads = 0
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) <= _SJ_MULTI:
            continue
        try:
            motif, known, unique, multi = (
                int(fields[i]) for i in (_SJ_MOTIF, _SJ_ANNOTATED, _SJ_UNIQUE, _SJ_MULTI)
            )
        except ValueError:
            continue
        junctions += 1
        annotated += known != 0
        canonical += motif != 0
        unique_reads += unique
        multi_reads += multi
    return {
        "junctions": junctions,
        "annotated": annotated,
        "canonical": canonical,
        "unique_reads": unique_reads,
        "multi_reads": multi_reads,
    }


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
        **_star_run_files(run_dir),
        # The TABLE, and only here. A handful of droplet samples can afford one each, and the plate
        # bundle beside it keeps the counts instead — the arity is the whole difference.
        "splice_junctions": _parse_sj(_read(run_dir / STAR_JUNCTIONS)),
    }
    return bundle


def build_plate_qc_bundle(
    run_dir: Path,
    *,
    sample: str,
    assembly: str | None,
    extract: Path,
    split: Path | None = None,
) -> dict[str, object]:
    """Every artifact one plate CELL left behind, as one JSON-serialisable dict.

    ``run_dir`` is the cell's own directory — a cell IS a sample on the twins — holding the aligner's
    run files. ``extract`` is the summary the extraction wrote a rule earlier, and ``split`` is the
    chimeric twin's account of what left for which Component; both are folded in verbatim under one
    key each, so their own readers stay the only code that knows what their keys mean.

    ``split`` is ABSENT for a plain plate rather than empty, because there was no split: an absent
    key and an empty one are different claims and only one of them is a measurement.

    ``assembly`` is recorded for CRAM-reference provenance, as in the droplet bundle — a cell's two
    archives pin the exact reference bytes by MD5 and this names which assembly that is. On a
    chimeric run it is the Chimera, which is what the aligner was pointed at.
    """
    bundle: dict[str, object] = {
        "sample": sample,
        "assembly": assembly,
        "extract": _payload(extract),
        **_star_run_files(run_dir),
        "splice_junction_summary": _summarise_sj(_read(run_dir / STAR_JUNCTIONS)),
    }
    if split is not None:
        bundle["split"] = _payload(split)
    return bundle


def _dump(bundle: Mapping[str, object], out: Path) -> Path:
    """Write one bundle as gzipped JSON to ``out``. Returns ``out``."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(bundle, fh)
    return out


def write_qc_bundle(
    solo_dir: Path,
    run_dir: Path,
    features: Sequence[SoloFeature],
    out: Path,
    *,
    sample: str,
    assembly: str | None = None,
) -> Path:
    """Build the droplet bundle and write it as gzipped JSON to ``out``. Returns ``out``."""
    return _dump(
        build_qc_bundle(solo_dir, run_dir, features, sample=sample, assembly=assembly), out
    )


def write_plate_qc_bundle(
    run_dir: Path,
    out: Path,
    *,
    sample: str,
    assembly: str | None = None,
    extract: Path,
    split: Path | None = None,
) -> Path:
    """Build one plate cell's bundle and write it as gzipped JSON to ``out``. Returns ``out``."""
    return _dump(
        build_plate_qc_bundle(
            run_dir, sample=sample, assembly=assembly, extract=extract, split=split
        ),
        out,
    )


# ---- reading the bundle back --------------------------------------------------------------------
#
# The other half of the format contract, deliberately in this file. The two builders above decide
# which file lands under which key; everything below looks those keys up. Split across two modules
# they would drift silently — a renamed key would keep writing and quietly stop reading, and the page
# would lose a metric with nothing failing. Here, one file changes or one file breaks. That is also
# why the plate reader lives beside the plate builder rather than beside the fan-in it reports with:
# the pairing is per ARTIFACT, not per pipeline.


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


#: The one ``Summary.csv`` row a feature comparison is made on: the share of the library that feature
#: assigned to a gene. The same row the headline ``reads_in_genes`` metric reports for the ONE feature
#: :func:`_pick_feature` selected, so the number compared across features and the number shown for one
#: of them are the same measurement rather than two that could drift into meaning different things.
#: ``test_carrying_per_feature_counts_moves_no_value_the_metrics_table_already_showed`` asserts the
#: two agree on the headline feature, which is what holds that shut.
_READS_IN_GENES_ROW = "Reads Mapped to {f}: Unique {f}"

#: Below this share of reads assigned to a gene, gene assignment is poor. Declared **here**, above the
#: metric table, because it has two readers and they must not drift: it is ``reads_in_genes``' own
#: ``warn`` floor — the boundary below which the page tints that cell red — and it is the bar
#: :func:`gene_model_rule` fires under, so the rule speaks exactly where the tint already is and adds
#: the diagnosis a tint cannot carry. One name, passed to :func:`fraction` and read by the rule, so
#: tightening the metric moves the rule **by construction** rather than by anyone remembering to.
#:
#: The silence above it is the design, as with the barcode bar. Between this and the metric's 30%
#: ``ok`` bar a low gene rate has ordinary explanations — an intron-rich prep, a sparse annotation, a
#: degraded library — and none of them is a decision this compiler made.
POOR_GENE_ASSIGNMENT = 0.15

#: At or above this share of reads mapped uniquely to the genome, mapping is **not** the problem —
#: :func:`gene_model_rule`'s precondition rather than a grade of its own, which is why it is not
#: passed to any :func:`fraction` call: ``reads_in_genome`` is deliberately ungraded, a hint with no
#: threshold, because a unique-mapping rate varies with genome quality and rRNA content far more than
#: with anything seqforge decided.
#:
#: Set at ``uniquely_mapped``'s ``ok`` bar and for the same reason — that value is where "the reads
#: found the right genome" starts being true — but it is deliberately **not** wired to it. They are
#: two different measurements: ``uniquely_mapped`` is STAR's own ``Log.final.out`` percentage over
#: input reads, and this is STARsolo's ``Summary.csv`` share, whose denominator is what STARsolo
#: processed. Sharing a number is not sharing a definition, and a constant wired across that gap
#: would silently move this rule the day someone regraded a metric it does not read.
HEALTHY_GENOME_MAPPING = 0.60

#: Which ``soloFeatures`` feature is THE exonic count. STARsolo's own name for it, and the reason the
#: comparison below has a fixed left-hand side: every other cell-level feature in the vocabulary
#: counts something *broader* than exons, so the gap always has one direction.
_EXONIC_FEATURE = "Gene"


def _countable_features() -> frozenset[str]:
    """Every ``SoloFeature`` that carries the cell-level rows these numbers come from.

    Derived from the aligner's own closed vocabulary minus :data:`_NO_CELL_SUMMARY`, rather than from
    a list of names typed out here. A seventh feature is a new STARsolo release, and the failure a
    hand-written list produces is the silent one: it would keep passing while the new feature quietly
    fell out of every comparison. A complement fails the other way — a new feature enters the
    comparison — and of the two, being asked about a feature beats never being told about it.
    """
    return frozenset(get_args(SoloFeature)) - _NO_CELL_SUMMARY


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

    An unclipped adapter is the one of those three a reader can now *see* rather than infer, because
    ``input_read_length`` is the length STAR was left with **after** doing its own clipping. It
    carries no bar, and that is the decision rather than an omission. The shortfall only means
    something against the length that was sequenced, and nothing downstream of ``probe`` holds that
    number — the manifest never carried a read length. Nor can ``unmapped_too_short`` stand in for
    it: correctly clipped Smart-seq3 cells still average 22.75% there, down from 54.43% unclipped
    but above this table's own 0.20 ``ok`` bar, so a cross-check reusing that bar would fire on the
    healthy case, and four cells of one chemistry argue no other. Full tables:
    ``docs/research/smartseq3-tn5-read-through.md``. Publishing the number and declaring no bar is
    the whole of it; the comparison belongs to the reader who knows what they sequenced.
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
        count(
            "input_read_length",
            "Input read length",
            _as_number(log_final.get("Average input read length")),
            group="input",
            exact=True,
            hint="Mean bases that reached the aligner per FRAGMENT — both mates of a pair summed, "
            "so 150 bp paired-end reads arrive here as ~300 rather than 150. STAR counts it AFTER "
            "its own clipping, so the value drops below what was sequenced if and only if a declared "
            "adapter read-through was actually found. Compare the two: that is what tells a library "
            "clipped and still mapping badly from one that was never clipped at all, which look "
            "identical in the unmapped-too-short share. No bar — nothing in this report records the "
            "length that went in, so only you can supply it.",
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
                _summary_get(summary, feature, _READS_IN_GENES_ROW),
                group="counts",
                ok=0.30,
                warn=POOR_GENE_ASSIGNMENT,
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
            sequencing_saturation(_summary_get(summary, feature, "Sequencing Saturation")),
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

    # Every feature the run counted, on the one row a feature comparison is made on. The bundle has
    # carried every feature's `Summary.csv` since the first one ever written — the table above shows
    # ONE of them because `_pick_feature` selects, and this is the rest of what was already on disk,
    # carried up narrow. Nothing in the writer changed to make it available, so no artifact grew and
    # no `run_id` was invalidated; what was missing was a reader that looked at more than one column.
    per_feature = {
        feat: value
        for feat in sorted(set(summary) & _countable_features())
        if (value := _summary_get(summary, feat, _READS_IN_GENES_ROW)) is not None
    }

    # Which feature the RECIPE counts first, straight off `soloFeatures` — the ordered list
    # `build_qc_bundle` has always written. Element 0 is what `compose` projects to the config's
    # `primary_feature`, so this is the recipe's own intent rather than the reader's preference, and
    # it is what lets a rule tell "counting exonically" from "counting exonically BY MISTAKE".
    declared = bundle.get("soloFeatures")
    primary = str(declared[0]) if isinstance(declared, list) and declared else ""

    note = f"counted from the {feature} feature" if feature else ""
    return SampleStats(
        sample_id=sample,
        metrics=solo + alignment,
        knee=knee_points(vector),
        note=note,
        feature_reads_in_genes=per_feature,
        primary_feature=primary,
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


def plate_metrics(bundle: Mapping[str, Any], sample: str) -> SampleStats:
    """One plate cell's bundle -> its report row: the extraction, then the alignment, then the split.

    **Pure**, like :func:`metrics`, and composed rather than written: each absorbed artifact keeps its
    own ``payload -> metrics`` function beside its own writer, and this hands each one the payload the
    builder folded in. So a column means the same thing whether it reached the page through a cell's
    bundle or, as it did before the bundle existed, through the file itself.

    In pipeline order, which is the order a reader wants to walk a cell in — what the FASTQs held and
    how much of it carried a tag, what the aligner then did with those reads, and what left at the
    split. A payload the bundle does not carry costs its columns and nothing else: a plain plate has
    no split, and an older bundle may have no key a newer reader looks for.
    """
    # Imported HERE and not at the top of this file: the splitter reaches for the genome package
    # while it is imported, and that package is not a dependency of the wheel — so spelling it above
    # would make every droplet run's bundle verb, which has nothing to do with a Chimera, need an
    # install it never needed before.
    from .split import split_metrics

    def chapter(key: str, adapter: Callable[[Mapping[str, Any], str], SampleStats]) -> list[Metric]:
        payload = bundle.get(key)
        return adapter(payload, sample).metrics if isinstance(payload, Mapping) else []

    log_final = bundle.get("log_final")
    return SampleStats(
        sample_id=sample,
        metrics=[
            *chapter("extract", extract_metrics),
            *(alignment_metrics(log_final) if isinstance(log_final, Mapping) else []),
            *chapter("split", split_metrics),
        ],
    )


def read_plate_metrics(path: Path, sample: str) -> SampleStats:
    """Load one plate cell's ``<sample>.qc.json.gz`` and normalise it.

    The thin half of the adapter, in the shape :func:`read_metrics` established: loading lives here so
    the registry hands over a path and gets metrics back, and the judgement lives in
    :func:`plate_metrics`, which needs no file to test.
    """
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        bundle = json.load(fh)
    if not isinstance(bundle, Mapping):
        raise ValueError(f"{path} is not a QC bundle object")
    return plate_metrics(bundle, sample)


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


# ---- the cross-check ----------------------------------------------------------------------------
#
# A rule here, and not in the renderer, for the same reason the reader is here: "a valid barcode" is
# a STARsolo fact, and a renderer that knew what one was would be the `module == "map/starsolo"`
# branch the per-module registry exists to prevent. It is registered on this module's `StatsSpec`,
# under the same drift guard, so a fourth aligner declares its own rules or says out loud it has none.
#
# Pure, and over ONE sample's metrics: a threshold is then testable against literal values with no
# filesystem, which is the only way a bar like the one below can be argued rather than asserted.


#: Below this share of whitelist-matching reads, the rule fires. **1%, and the argument is that no
#: real barcoded library lives under it**: even a badly degraded barcode read on the right kit matches
#: percent-scale, so a rate this low is a whitelist that does not belong to these reads at all — which
#: is a decision (which kit, or which file is the barcode read), not a library.
#:
#: At or above it the rule is deliberately silent, and that silence is the design. Between 1% and the
#: metric's own 50% bar there is more than one explanation — a degraded barcode read, a contaminated
#: library, a related-but-wrong kit — so naming one decision would be a guess wearing a diagnosis. The
#: number is still bad there, and the metric's own `bad` tint already says so; what the alert adds is
#: a claim about *cause*, and it is only made where the cause is decided.
NEAR_ZERO_VALID_BARCODES = 0.01


def _metric_value(sample: SampleStats, key: str) -> float | None:
    """One metric's raw number, or ``None`` when the adapter never wrote it.

    Absent is absent and never a zero — the rule the metric table already lives by, and here it is the
    difference between silence and the loudest alert this system has. Bulk STAR measures no barcode at
    all, so reading "no valid-barcode rate" as "a valid-barcode rate of zero" would fire on every bulk
    run ever compiled.
    """
    for metric in sample.metrics:
        if metric.key == key:
            return metric.value
    return None


def chemistry_rule(sample: SampleStats) -> list[Finding]:
    """Near-zero valid barcodes -> the chemistry call, or the barcode read's role, looks wrong.

    What this adds over the metric is not "bad" — the metric already says bad, and a reader who does
    not know STARsolo reads that as a bad library. It names the two **decisions** that produce the
    number: which kit was called, and which FASTQ was handed over as the barcode read. Those are the
    only two things a reader can act on, and the compiler made both.

    Both are implicated rather than one, because the metric cannot separate them: a whitelist that
    matches nothing looks identical whether the wrong list was chosen or the right list was read
    against the cDNA. Picking one would be a guess, and an alert that guesses is an alert that gets
    ignored the first time it is wrong.
    """
    value = _metric_value(sample, "valid_barcodes")
    if value is None or value >= NEAR_ZERO_VALID_BARCODES:
        return []
    return [
        Finding(
            alert_id="starsolo.valid-barcodes-near-zero",
            sample_id=sample.sample_id,
            title="Almost no read carries a barcode this kit's whitelist knows",
            severity="likely",
            measured=(
                f"{fmt_pct(value)} of reads matched the whitelist "
                f"(this rule fires below {fmt_pct(NEAR_ZERO_VALID_BARCODES)}); "
                "a real library of this kit matches the great majority"
            ),
            implicates=["chemistry", "read_roles"],
            remedy=(
                "Check which kit this library really is, and which FASTQ was handed over as the "
                "barcode read. If either is wrong, correct it and compose again — nothing here "
                "changes your manifest."
            ),
        )
    ]


#: How much of the library may be intronic-only before the rule speaks. **30 points, and the argument
#: is that ordinary biology lives under it**: a whole-cell 10x library carries a real intronic
#: fraction, commonly ten to twenty points, so a bar at 0.20 would fire on healthy runs — and a rule
#: that fires on a healthy run is worse than a rule that does not exist. A nuclear prep runs far
#: higher: the failure in #215 measured 0.407, which clears this with margin.
#:
#: Severity is ``possible`` and not ``likely``, and that is the other half of the bar. Counting
#: exonically **can be deliberate** — an exonic count is the right answer for plenty of whole-cell
#: work — so the claim this rule is entitled to make is "you are probably counting the wrong feature",
#: never "this run is wrong". Move the number only on evidence, and put the evidence in the commit.
INTRONIC_ONLY_READ_SHARE = 0.30


def solo_features_rule(sample: SampleStats) -> list[Finding]:
    """A large exonic-versus-full-length gap -> the primary counted feature looks like the wrong one.

    The measurement is a subtraction between two ways STAR counted the same library:
    ``reads_in_genes(GeneFull*) - reads_in_genes(Gene)``. Both numbers are shares of the whole
    library, so their difference is the share of the whole library that is **intronic-only** — reads
    landing inside a gene body and outside every exon, which a nuclear prep produces in bulk and an
    exonic count discards. That is the number #215 quotes, and it was 40.7% of a library.

    Compared against the largest full-length count rather than an average of them, because an average
    reports a number no feature produced. Silent when the run counted one way — there is no gap to
    measure, and that is most runs: ``soloFeatures`` is frequently just ``Gene``. Silent too when the
    full-length count is the *smaller* one, which is not a thing this rule has a story for.

    **Silent when the recipe already counts a full-length feature first.** The gap is a fact about the
    library and it survives the fix — a nuclear prep still has intronic reads once ``GeneFull`` is
    primary — so firing on the measurement alone would raise "you are counting the wrong feature" at a
    reader who is counting the right one, with a remedy telling them to do what they have already
    done. That is the firing-on-a-healthy-run failure that makes a rule worse than no rule, and it is
    why :attr:`~seqforge.workflows.metrics.SampleStats.primary_feature` is carried: the claim is not
    "this library is nuclear", it is "the matrix everything downstream reads is missing most of it".

    The remedy is a REORDER and never a replacement:
    :class:`~seqforge.models.processing.SoloQuant` rules that a prep fact may only reorder the feature
    list, since compute is spent once and dropping a feature is the only irreversible act available.
    A remedy contradicting a validator in this repo is worse than none.
    """
    counted = sample.feature_reads_in_genes
    exonic = counted.get(_EXONIC_FEATURE)
    if sample.primary_feature and sample.primary_feature != _EXONIC_FEATURE:
        return []
    # The vocabulary decides what counts as a full-length feature, here as well as in the reader that
    # filled the mapping: the rule states the comparison, so the rule is where it has to hold. `SJ`
    # and `Velocyto` are out by `_NO_CELL_SUMMARY` — neither has the cell-level rows this subtracts.
    full_length = {
        f: v for f, v in counted.items() if f in _countable_features() - {_EXONIC_FEATURE}
    }
    if exonic is None or not full_length:
        return []
    feature, value = max(full_length.items(), key=lambda item: (item[1], item[0]))
    gap = value - exonic
    if gap < INTRONIC_ONLY_READ_SHARE:
        return []
    return [
        Finding(
            alert_id="starsolo.intronic-reads-uncounted",
            sample_id=sample.sample_id,
            title="Most of what this library gained from introns is not in the counted matrix",
            severity="possible",
            measured=(
                f"{fmt_pct(gap)} of the library is intronic-only "
                f"({feature}: {fmt_pct(value)} of reads in a gene, "
                f"{_EXONIC_FEATURE}: {fmt_pct(exonic)}); "
                f"this rule fires at {fmt_pct(INTRONIC_ONLY_READ_SHARE)}"
            ),
            implicates=["solo_features"],
            remedy=(
                f"If these are nuclei, put {feature} first in the recipe's counting features — "
                f"element 0 is the matrix everything downstream reads. Keep {_EXONIC_FEATURE} in "
                "the list: a prep fact may reorder the features, never shorten them. Nothing here "
                "changes your recipe."
            ),
        )
    ]


def gene_model_rule(sample: SampleStats) -> list[Finding]:
    """Reads landing on the genome and not in genes -> the gene model, or the strand, looks wrong.

    Two numbers the bundle already carries, read *against each other*: ``reads_in_genome`` (STARsolo's
    ``Reads Mapped to Genome: Unique``) and ``reads_in_genes`` (``Reads Mapped to Gene: Unique
    Gene``). Neither alone decides anything — the first is a hint with no threshold at all and the
    second is already graded — and it is the **gap** between them that is diagnostic: reads that found
    their locus and then found no feature there were counted against the wrong gene model, or counted
    on the wrong strand. A reader who has not run STARsolo has no way to see that in two adjacent
    percentages, which is exactly what this layer is for.

    **Silent when both are poor, and that is half the specification.** A run where little maps and
    little counts has a mapping problem — the wrong assembly, the wrong species, the wrong read handed
    over as the barcode — and claiming an annotation failure there would be a second, contradictory
    diagnosis on a page that already carries the right one. A page that fires two contradictory alerts
    at one run is worse than either alone.

    **Silent when either number is absent**, which is what makes "a pipeline whose aligner index
    carries no gene model never triggers it" true by construction rather than by a special case:
    :attr:`~seqforge.models.processing.GenomeRef.annotation_name` is ``None`` exactly when there is no
    GTF, and with no GTF STAR writes no gene rows into ``Summary.csv``, so ``reads_in_genes`` is
    simply not there. The rule is deliberately **not** handed the annotation's name: it is pure over
    one sample's metrics, and a parameter would buy a special case where absence already answers.

    **Silent when another feature counted the same reads fine.** The headline ``reads_in_genes`` is
    read off whichever feature :func:`_pick_feature` selected — ``Gene``, the exonic one, wherever it
    exists — so a nuclear library counted exonically shows healthy mapping beside a poor exonic count
    and looks exactly like a wrong annotation from these two numbers alone. It is not one: a
    ``GeneFull*`` count in the same bundle proves the reads DID land in genes and the gene model is
    fine. Firing here would put a ``likely`` alert naming the annotation and the strand over a run
    whose real problem is which feature is primary — the louder alert pointing at the wrong decision,
    beside the quieter one pointing at the right decision. That is the contradiction the paragraph
    above refuses in the both-poor case, arriving from the other side.

    Both decisions are implicated rather than one, for the reason the chemistry rule implicates two:
    the metric cannot separate them. An inverted ``soloStrand`` and a GTF for the wrong assembly
    produce the same two percentages, so picking one would be a guess wearing a diagnosis.
    """
    genome = _metric_value(sample, "reads_in_genome")
    genes = _metric_value(sample, "reads_in_genes")
    if genome is None or genes is None:
        return []
    if genome < HEALTHY_GENOME_MAPPING or genes >= POOR_GENE_ASSIGNMENT:
        return []
    counted_elsewhere = max(
        (v for f, v in sample.feature_reads_in_genes.items() if f != _EXONIC_FEATURE), default=None
    )
    if counted_elsewhere is not None and counted_elsewhere >= POOR_GENE_ASSIGNMENT:
        return []
    return [
        Finding(
            alert_id="starsolo.reads-mapped-but-not-counted",
            sample_id=sample.sample_id,
            title="Reads land on the genome but hardly any land in a gene",
            severity="likely",
            measured=(
                f"{fmt_pct(genome)} of reads mapped uniquely to the genome, and only "
                f"{fmt_pct(genes)} of them were assigned to a gene (this rule fires at or above "
                f"{fmt_pct(HEALTHY_GENOME_MAPPING)} mapped with under "
                f"{fmt_pct(POOR_GENE_ASSIGNMENT)} counted); the aligner found the genome, so the "
                "gap is in what the reads were counted against"
            ),
            implicates=["annotation", "strand"],
            remedy=(
                "Check that the registered annotation is the gene model for this assembly, and that "
                "the strand matches how this kit's cDNA read is oriented. The annotation is a "
                "recipe field; the strand is a KB backend param, so it belongs to the chemistry "
                "spec and not to your recipe. Correct either and compose again — nothing here "
                "changes your manifest."
            ),
        )
    ]


__all__ = [
    "HEALTHY_GENOME_MAPPING",
    "INTRONIC_ONLY_READ_SHARE",
    "NEAR_ZERO_VALID_BARCODES",
    "POOR_GENE_ASSIGNMENT",
    "QC_SUFFIX",
    "QcError",
    "alignment_metrics",
    "chemistry_rule",
    "build_plate_qc_bundle",
    "build_qc_bundle",
    "gene_model_rule",
    "metrics",
    "plate_metrics",
    "read_metrics",
    "read_plate_metrics",
    "read_star_log",
    "solo_features_rule",
    "write_plate_qc_bundle",
    "write_qc_bundle",
]
