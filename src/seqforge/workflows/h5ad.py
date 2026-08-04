"""Package STARsolo's count matrices as ``.h5ad`` — the pilot's deliverable format.

This is the last step of ``map/starsolo``, and it lives here rather than in a module of its own on
purpose. Its input contract **is** STARsolo's ``Solo.out/<Feature>/raw/`` directory layout: a
separate "h5ad module" would have a config contract of *"wherever starsolo happened to put things"*,
which is coupling with no interface — the same generalisation-from-a-sample-size-of-one this repo
declined for the aligner plugin API. A second aligner packages its own output; that is what a module
is for.

Writing an ``.h5ad`` is not an aligner, so the aligner-environment rule has no opinion on it and it needs no liulab-runtime
environment: ``anndata`` is a plain dependency, like ``pypdf``. Only the STAR step needs a container.

**Why the rule shells out to a CLI verb instead of using a Snakemake ``run:`` block.** ``snakemake -n
-p`` renders every ``shell:`` block while planning and *cannot see inside* a ``run:`` block — so a
``shell:`` is visible to compose's wiring gate and a ``run:`` is not. Since the gate exists precisely
to catch a param that does not survive compose, the packaging step has to be reachable by it.

Two files come out of a default run, and the split is the user's call (2026-07-15):

- ``<sample>.h5ad`` — ``X`` is the primary feature, one ``layer`` per other gene-axis feature. Four
  counts of the same genes in the same cells belong in one object; they differ only in the counting
  rule, which is exactly what a layer is.
- ``<sample>.velocyto.h5ad`` — spliced / unspliced / ambiguous. Separate because Velocyto is not a
  fourth way to count the same thing: it is three matrices that only mean anything together, and
  scVelo wants its own object.

Everything is read from ``raw/``, never ``filtered/``. Cell calling is a downstream decision made
once, on evidence we do not have at compile time, and ``raw`` is the only form that keeps it
available. (``filtered/`` does exist for every feature including Velocyto — checked on real output,
because I had assumed otherwise.)

**But ``raw/`` is the whole whitelist, and most of it is empty.** ``raw/barcodes.tsv`` on 10x v3 is
all 6,794,880 barcodes the chemistry can express; on the sample this was measured against, 845,694
of them (12.4%) carried a count in any feature. Writing the other 87.6% cost 633.8 MB per
deliverable, against 95.5 MB once they are dropped — and dropping them is lossless in the strict
sense, because a barcode with no count in any feature carries no information about the experiment.
This is emphatically **not** cell calling: a barcode with a single UMI survives, and which barcodes
are cells stays the downstream decision it was.

The mask is the **union** over every matrix in the object, never the primary alone. ``X`` is
``Gene`` — exonic reads — while ``GeneFull`` counts introns as well, so a barcode can be zero in one
and carry real counts in the other; 22,319 barcodes on that sample were exactly that, and
``X.sum() > 0`` would have deleted every one of them. See :func:`_barcode_mask`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix

from ..models.processing import SoloFeature

if TYPE_CHECKING:  # pragma: no cover — anndata is a runtime dep; this keeps import cost off compose
    import anndata

#: What the rows of a feature's ``features.tsv`` are. Two features with the same axis are stackable
#: (same ``var``); two with different axes are not, at any price.
SoloAxis = Literal["gene", "junction"]


class H5adError(RuntimeError):
    """The Solo.out on disk cannot be packaged as written (mismatched axes, missing matrix)."""


@dataclass(frozen=True)
class SoloFeatureOutput:
    """What STAR writes under ``Solo.out/<Feature>/raw/`` for one ``--soloFeatures`` value."""

    axis: SoloAxis
    #: The Matrix Market file(s). One for a plain count; three for Velocyto, which has no
    #: ``matrix.mtx`` at all (verified on real output — do not add one back).
    matrices: tuple[str, ...]


#: STARsolo's output contract, per feature.
#:
#: **This is a hand-written table, and it is the one shape this repo distrusts** — so read how it is
#: kept honest before adding a row. It is not derivable: it states what somebody else's binary
#: writes, and we have no source for that but STAR itself. Two mechanisms stand behind it:
#:
#: 1. ``test_every_solo_feature_is_classified`` collects from ``SoloFeature`` itself, so a new
#:    feature is covered *because it exists*, not because someone remembered. Silently packaging a
#:    new feature into nothing is the failure this prevents.
#: 2. The axis claim is checked **from bytes at run time**: :func:`_stack` compares the actual
#:    ``features.tsv``/``barcodes.tsv`` of every feature it is about to stack and refuses on
#:    disagreement. So a wrong ``axis`` here fails loudly instead of mislabelling a matrix — which is
#:    the failure class (silent, plausible, wrong) that everything else in this repo is built against.
#:
#: The ``matrices`` claim is checked by Snakemake: they are declared outputs of ``starsolo_count``,
#: so a missing one is a hard failure of the rule that was supposed to write it. That matters more
#: than it sounds — the rule used to declare ``directory(Solo.out)``, under which STAR writing three
#: of five features and exiting 0 was indistinguishable from success.
SOLO_FEATURE_OUTPUT: dict[SoloFeature, SoloFeatureOutput] = {
    "Gene": SoloFeatureOutput(axis="gene", matrices=("matrix.mtx",)),
    "GeneFull": SoloFeatureOutput(axis="gene", matrices=("matrix.mtx",)),
    "GeneFull_ExonOverIntron": SoloFeatureOutput(axis="gene", matrices=("matrix.mtx",)),
    "GeneFull_Ex50pAS": SoloFeatureOutput(axis="gene", matrices=("matrix.mtx",)),
    "Velocyto": SoloFeatureOutput(
        axis="gene", matrices=("spliced.mtx", "unspliced.mtx", "ambiguous.mtx")
    ),
    "SJ": SoloFeatureOutput(axis="junction", matrices=("matrix.mtx",)),
}

#: The feature whose matrices are three-in-one rather than one. Named structurally (by what STAR
#: writes) rather than by string compare wherever possible; this constant is the one place the name
#: appears, because the *file naming* of the second h5ad has to come from somewhere.
_VELOCYTO: SoloFeature = "Velocyto"

#: Names for ``features.tsv``'s columns after the first. STARsolo writes CellRanger's three-column
#: form (id, name, type) for every gene-axis feature. Anything else falls through to ``col<N>``,
#: which is honest rather than a guess dressed as a schema.
_VAR_COLUMNS: dict[int, str] = {1: "gene_name", 2: "feature_type"}

_FEATURES_TSV = "features.tsv"
_BARCODES_TSV = "barcodes.tsv"

_MAIN_SUFFIX = ".h5ad"
_VELOCYTO_SUFFIX = ".velocyto.h5ad"


def raw_files(feature: SoloFeature) -> tuple[str, ...]:
    """Every file ``Solo.out/<feature>/raw/`` must contain, relative to that directory."""
    return (*SOLO_FEATURE_OUTPUT[feature].matrices, _FEATURES_TSV, _BARCODES_TSV)


def solo_raw_files(features: Sequence[SoloFeature]) -> list[str]:
    """Every raw file a run of ``--soloFeatures <features>`` must produce, relative to ``Solo.out``.

    These become explicit outputs of ``starsolo_count``, which is what makes "STAR exited 0 having
    written only some of them" a rule failure rather than a thinner matrix nobody notices.
    """
    return [f"{feat}/raw/{name}" for feat in features for name in raw_files(feat)]


def _stackable(features: Sequence[SoloFeature]) -> list[SoloFeature]:
    """The gene-axis, one-matrix features — the ones that go in ``<sample>.h5ad`` together."""
    return [
        f
        for f in features
        if SOLO_FEATURE_OUTPUT[f].axis == "gene" and len(SOLO_FEATURE_OUTPUT[f].matrices) == 1
    ]


def _gene_axis(features: Sequence[SoloFeature]) -> list[SoloFeature]:
    """The gene-axis features — every one that gets a ``filtered/`` cell-called copy on disk.

    That is ``Gene``/``GeneFull*`` **and** ``Velocyto`` (all ``axis == "gene"``); the junction-axis
    ``SJ`` is excluded because we have not confirmed its ``filtered/`` layout on real output, and a
    declared output that STAR did not write is a hard rule failure. Under-declaring only leaves a
    file uncleaned; over-declaring breaks the run — so this stays to what the tree in hand shows.
    """
    return [f for f in features if SOLO_FEATURE_OUTPUT[f].axis == "gene"]


#: STAR's end-of-run summary — mapping rates, splice counts, the unmapped breakdown. Its own constant
#: because **no rule of ours declares it and three readers name it**: STAR emits it unasked on every
#: ``alignReads`` run, and it is then reached for by :data:`STAR_LOG_FILES` below (which declares the
#: stats bundle's inputs), by ``qc.build_qc_bundle`` (which folds it in under ``log_final``), and by
#: the pipeline-stats registry — for which it is the WHOLE artifact ``map/star`` reports from, since
#: bulk has no QC bundle rule and nothing in ``star.smk`` declares or deletes this file. Three
#: spellings of one filename is the drift this repo keeps finding: one owner, three readers, and a
#: rename that reaches every one of them or fails at import instead of silently at runtime.
STAR_FINAL_LOG = "Log.final.out"

#: STAR's per-run log/table files, written beside ``Solo.out`` at ``{OUTDIR}/{sample}/`` (not per
#: feature). The alignment (:data:`STAR_BAM`) is deliberately **not** here: it is the CRAM rule's
#: input, declared on its own, while these four are the stats bundle's. All are written by every
#: ``alignReads`` run.
STAR_LOG_FILES: tuple[str, ...] = (STAR_FINAL_LOG, "Log.out", "Log.progress.out", "SJ.out.tab")

#: The coordinate-sorted alignment STAR writes under ``{OUTDIR}/{sample}/``. Its own constant because
#: exactly one rule (``solo_to_cram``) consumes it, and the file naming has to come from somewhere.
#:
#: **Sorted, and the name says so.** STAR names its output after ``--outSAMtype``, and the module has
#: no choice but ``BAM SortedByCoordinate``: STAR rejects ``CB``/``UB`` in ``--outSAMattributes``
#: outright for an unsorted BAM, and a retained CRAM with no barcode cannot be recounted under
#: another GTF — which is the only reason to retain one. A stale literal here is not a subtle bug:
#: ``starsolo_count`` declares this path as an output, so the rule fails *after* the whole alignment
#: has been paid for.
STAR_BAM = "Aligned.sortedByCoord.out.bam"


def solo_stats_files(features: Sequence[SoloFeature]) -> list[str]:
    """Every small STAR stats file a ``--soloFeatures`` run writes, relative to ``Solo.out``.

    Declared as ``temp()`` outputs of ``starsolo_count`` so the ``qc_bundle`` rule consumes them into
    ``<sample>.qc.json.gz`` and Snakemake then deletes them — no manual ``rm``. Three shapes:

    - ``Barcodes.stats`` — once, at the top level (barcode-demux QC, not per feature).
    - ``Summary.csv`` / ``Features.stats`` — one per feature, for every feature.
    - ``UMIperCellSorted.txt`` — the knee-plot vector, written **only** for the cell-filtered gene
      features (the ``_stackable`` set). STAR writes none for ``Velocyto`` or ``SJ``, so declaring it
      there would fail the rule. This is the one per-feature distinction, confirmed against real
      output (Gene/GeneFull* have it; Velocyto does not).
    """
    out = ["Barcodes.stats"]
    for feat in features:
        out += [f"{feat}/Summary.csv", f"{feat}/Features.stats"]
    out += [f"{feat}/UMIperCellSorted.txt" for feat in _stackable(features)]
    return out


def solo_filtered_files(features: Sequence[SoloFeature]) -> list[str]:
    """Every ``filtered/`` file a ``--soloFeatures`` run writes, relative to ``Solo.out``.

    The cell filter (``--soloCellFilter EmptyDrops_CR``, hardcoded in ``starsolo.smk``; STAR's own
    default is ``CellRanger2.2 3000 0.99 10``) writes a ``filtered/`` copy of each gene-axis feature —
    same matrices + axis files as ``raw/``. Which caller runs does not change this list, and that was
    checked on a real ``EmptyDrops_CR`` run rather than assumed: the tree it wrote is exactly what
    this function declares, no more and no less. Nothing downstream reads it (the h5ad is built from
    ``raw/``), so it is declared ``temp()`` and the ``qc_bundle`` rule lists it as input purely to (a)
    record ``filtered/barcodes.tsv`` — what STAR *called* — as provenance and (b) trigger its deletion.
    """
    return [f"{feat}/filtered/{name}" for feat in _gene_axis(features) for name in raw_files(feat)]


def h5ad_suffixes(features: Sequence[SoloFeature]) -> list[str]:
    """The ``.h5ad`` files a run of ``--soloFeatures <features>`` yields, as filename suffixes.

    Called from ``starsolo.smk`` at parse time to declare the rule's outputs **and** by
    :func:`write_h5ad` to decide what to write, so the two cannot disagree. One function, two callers
    — the alternative (compose emits the list, the verb writes what it likes) is two sources of truth
    for one fact, which is the bug this repo keeps finding.

    A junction-axis feature (``SJ``) yields nothing here, and that is deliberate rather than
    forgotten: its matrix has a different ``var`` axis, nothing downstream consumes it, and STAR has
    already written it to ``Solo.out/SJ/raw/`` for anyone who wants it. It is out of the default
    feature set for the same reason. Give it its own object when something needs one.
    """
    out: list[str] = []
    if _stackable(features):
        out.append(_MAIN_SUFFIX)
    if _VELOCYTO in features:
        out.append(_VELOCYTO_SUFFIX)
    return out


def _read_features(path: Path) -> tuple[list[str], dict[str, list[str]]]:
    rows = [ln.split("\t") for ln in path.read_text().splitlines() if ln]
    if not rows:
        raise H5adError(f"{path} is empty; STARsolo wrote no features")
    names = [r[0] for r in rows]
    width = max(len(r) for r in rows)
    extra = {
        _VAR_COLUMNS.get(i, f"col{i}"): [r[i] if i < len(r) else "" for r in rows]
        for i in range(1, width)
    }
    return names, extra


def _axis_key(raw: Path) -> bytes:
    """The identity of a feature's axes, as bytes. Compared, never interpreted."""
    return (raw / _FEATURES_TSV).read_bytes() + b"\0" + (raw / _BARCODES_TSV).read_bytes()


def _read_matrix(path: Path, shape: tuple[int, int]) -> csr_matrix:
    """One Matrix Market file -> a **cells x genes** sparse matrix.

    **Transposed on the way in, by construction.** STARsolo writes genes x barcodes; AnnData is
    observations x variables, and the observations are cells. Getting this backwards yields an object
    that loads cleanly, plots cleanly, and is wrong in the one way nobody checks — and on a square
    matrix it would not even be a shape error. So the swap happens where you can see it: STAR's
    column index becomes the row, its row index becomes the column, and ``shape`` is asserted against
    the axes we actually read rather than taken from the file's own header.

    Parsed here rather than via ``scipy.io.mmread`` because that function's return type is mid-change
    (sparse matrix -> sparse array) and it warns about it; pinning our matrix orientation to somebody
    else's deprecation cycle is a poor trade for twelve lines. ``int32`` because these are UMI counts
    per gene per cell.
    """
    if not path.exists():
        raise H5adError(f"{path} is missing; the STAR run that should have written it did not")
    rows: list[int] = []
    cols: list[int] = []
    data: list[int] = []
    header_seen = False
    with path.open() as fh:
        for line in fh:
            if line.startswith("%") or not line.strip():
                continue
            if not header_seen:  # the dims line; we trust the axis files, not this
                header_seen = True
                continue
            gene, barcode, value = line.split()
            rows.append(int(barcode) - 1)  # 1-based -> 0-based, and STAR's COLUMN is our ROW
            cols.append(int(gene) - 1)
            data.append(int(value))
    mat = coo_matrix(
        (
            np.array(data, dtype=np.int32),
            (np.array(rows, dtype=np.int64), np.array(cols, dtype=np.int64)),
        ),
        shape=shape,
    )
    return mat.tocsr()


#: What ``uns`` records about the trim. Both go on every object this module writes: ``raw/`` is a
#: Snakemake ``temp()`` output, so once the run finishes these two numbers are the only surviving
#: statement that the obs axis was cut down from a whitelist, and from what size.
_UNS_WHITELIST = "n_barcodes_whitelist"
_UNS_RETAINED = "n_barcodes_retained"


@dataclass(frozen=True)
class _BarcodeMask:
    """One decision about which barcodes are worth writing, and the two things that must agree on it.

    ``barcodes`` is already subset and :meth:`apply` is the only way a matrix gets subset, so the obs
    axis and every matrix are cut by the same mask, computed once — they cannot drift apart into an
    object whose row labels name different cells than its counts do. A matrix that somehow escaped
    ``apply`` is not a silent misalignment either: AnnData refuses a layer whose shape disagrees with
    ``X``, so the mistake is a hard error at construction rather than counts on the wrong cells.
    """

    keep: np.ndarray
    barcodes: list[str]
    n_whitelist: int

    def apply(self, matrix: csr_matrix) -> csr_matrix:
        """``matrix``, keeping only the rows this mask kept. Row order is preserved."""
        return matrix[self.keep]

    def record(self, adata: anndata.AnnData) -> None:
        """State in ``uns`` what the object was cut down from, and to what."""
        adata.uns[_UNS_WHITELIST] = self.n_whitelist
        adata.uns[_UNS_RETAINED] = len(self.barcodes)


def _barcode_mask(barcodes: list[str], matrices: Iterable[csr_matrix]) -> _BarcodeMask:
    """The barcodes carrying a count in ANY of ``matrices``, as one mask over ``barcodes``.

    The union is the whole subtlety — the module docstring carries the number it is worth — so hand
    this every matrix that will end up in the object, ``X`` and each layer alike, and hand them over
    *before* any of them is subset. A caller that passes only the primary gets an object missing the
    cells whose counts happen to be intronic, and nothing anywhere says so.
    """
    keep = np.zeros(len(barcodes), dtype=bool)
    for matrix in matrices:
        # `getnnz` counts STORED entries rather than nonzero values, and in general those differ --
        # but not on this path. Every matrix here was parsed from a Matrix Market file, whose
        # coordinate form lists only the entries that exist, and STAR writes no explicit zero. So
        # this is an honest nonzero test, and it walks the stored indices instead of summing a
        # 6.8-million-row matrix per feature.
        keep |= matrix.getnnz(axis=1) > 0
    return _BarcodeMask(
        keep=keep,
        barcodes=[b for b, k in zip(barcodes, keep, strict=True) if k],
        n_whitelist=len(barcodes),
    )


def _stack(
    solo_dir: Path, features: Sequence[SoloFeature], primary: SoloFeature
) -> anndata.AnnData:
    """The gene-axis features as one object: ``X`` = primary, one layer per other feature.

    The obs axis is ``raw/barcodes.tsv`` minus the barcodes no feature counted — see
    :func:`_barcode_mask`, and note that the *union* there is what makes this correct.
    """
    import anndata as ad

    raws = {f: solo_dir / f / "raw" for f in features}
    # THE assertion. Four features are stacked into one `var`/`obs`, so if their axes differ, every
    # layer but the first is silently misaligned -- each count landing on the wrong gene, in an
    # object that opens fine and plots fine. Bytes decide; nothing here interprets a gene id.
    keys = {f: _axis_key(raw) for f, raw in raws.items()}
    odd = sorted(f for f, k in keys.items() if k != keys[primary])
    if odd:
        raise H5adError(
            f"features {odd} do not share {primary}'s barcodes.tsv/features.tsv, so their counts "
            f"cannot be layers of one object. This should be impossible for a single STAR run — "
            f"check that Solo.out was not assembled from more than one."
        )

    names, extra = _read_features(raws[primary] / _FEATURES_TSV)
    barcodes = (raws[primary] / _BARCODES_TSV).read_text().split()
    shape = (len(barcodes), len(names))
    # Every matrix is read before the object is built, so a missing one raises BEFORE anything is
    # written: a refusal must not leave a half-written deliverable on disk for someone to find.
    mats = {f: _read_matrix(raws[f] / "matrix.mtx", shape) for f in features}
    # The whitelist's empty rows go, on the union across every feature -- which is why the mask is
    # built from all of `mats` and not from `mats[primary]`. The axis assertion above has already
    # run: it compares the files as STAR wrote them, and must not be handed a trimmed axis.
    mask = _barcode_mask(barcodes, mats.values())
    mats = {f: mask.apply(m) for f, m in mats.items()}

    adata = ad.AnnData(X=mats[primary])
    adata.obs_names = mask.barcodes
    adata.var_names = names
    for col, values in extra.items():
        adata.var[col] = values
    for feat, mat in mats.items():
        if feat != primary:
            adata.layers[feat] = mat
    adata.uns["primary_feature"] = primary
    adata.uns["soloFeatures"] = list(features)
    mask.record(adata)
    return adata


def _velocyto(solo_dir: Path) -> anndata.AnnData:
    """Velocyto's three matrices as one object.

    ``X`` duplicates ``layers["spliced"]`` on purpose: scVelo reads ``layers["spliced"]`` and
    ``layers["unspliced"]``, while most everything else reads ``X``. The cost is one sparse matrix.
    """
    import anndata as ad

    raw = solo_dir / _VELOCYTO / "raw"
    names, extra = _read_features(raw / _FEATURES_TSV)
    barcodes = (raw / _BARCODES_TSV).read_text().split()
    shape = (len(barcodes), len(names))
    layers = {
        m.removesuffix(".mtx"): _read_matrix(raw / m, shape)
        for m in SOLO_FEATURE_OUTPUT[_VELOCYTO].matrices
    }
    # Union across all three, and `X` duplicating `spliced` is exactly what makes the narrower mask
    # tempting here: a barcode whose counts are all unspliced is a cell in the middle of
    # transcription, and it is zero in `X`.
    mask = _barcode_mask(barcodes, layers.values())
    layers = {name: mask.apply(mat) for name, mat in layers.items()}

    adata = ad.AnnData(X=layers["spliced"])
    adata.obs_names = mask.barcodes
    adata.var_names = names
    for col, values in extra.items():
        adata.var[col] = values
    for name, mat in layers.items():
        adata.layers[name] = mat
    adata.uns["primary_feature"] = "spliced"
    mask.record(adata)
    return adata


def write_h5ad(
    solo_dir: Path, features: Sequence[SoloFeature], primary: SoloFeature, out_prefix: Path
) -> list[Path]:
    """``Solo.out`` -> the ``.h5ad`` files :func:`h5ad_suffixes` promised, in that order."""
    stackable = _stackable(features)
    written: list[Path] = []
    for suffix in h5ad_suffixes(features):
        out = Path(f"{out_prefix}{suffix}")
        out.parent.mkdir(parents=True, exist_ok=True)
        if suffix == _VELOCYTO_SUFFIX:
            adata = _velocyto(solo_dir)
        else:
            # `primary` names which matrix is X. It is `soloFeatures[0]` and may be a feature that
            # is not stackable, in which case the first one that is takes its place -- a stated
            # fallback beats an IndexError three hours into a run.
            head = primary if primary in stackable else stackable[0]
            adata = _stack(solo_dir, stackable, head)
        adata.write_h5ad(out)
        written.append(out)
    return written


__all__ = [
    "H5adError",
    "SoloAxis",
    "SoloFeatureOutput",
    "SOLO_FEATURE_OUTPUT",
    "STAR_BAM",
    "STAR_FINAL_LOG",
    "STAR_LOG_FILES",
    "h5ad_suffixes",
    "raw_files",
    "solo_filtered_files",
    "solo_raw_files",
    "solo_stats_files",
    "write_h5ad",
]
