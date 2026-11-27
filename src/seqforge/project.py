"""Project-level views over a multi-assay compile: the flat ``sample_metadata.tsv`` and ``project.yaml``.

A large project splits into **assays**, one :class:`~seqforge.models.dataset.DatasetManifest` each.
Those manifests are the source of truth; this module unions them back into the "one study" views a
human reads — a flat table with one row per sample across every assay, and a small index mapping each
assay to its subdir / chemistry / sample count. Both are **derived** from the manifests (so they cannot
drift from them) and **deterministic** (sorted rows and columns), so regenerating is a no-op.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models.blocker import BlockerSubject, ValidationWarning
from .models.dataset import DatasetManifest
from .study.folder import StudyFolder
from .study.join import READING_HASH, CompiledSample, join_reading
from .workspace import SAMPLE_METADATA_TSV, sample_metadata_path, state_dir

#: The table's columns, in order. What a sample WAS (its tissue, strain or treatment) is not among
#: them: a study reading holds it, and a compile that names its study joins those labels in after.
_COLUMNS: tuple[str, ...] = ("sample_id", "accession", "assay", "organism", "n_files", "files")

#: The project-level file names, written at the top of ``seqforge/`` (never inside an assay subdir).
#: The table's own name is the workspace layout's, because a compiled pipeline reads it back.
PROJECT_YAML = "project.yaml"


def sample_metadata_table(
    manifests: list[DatasetManifest],
) -> tuple[list[str], list[dict[str, str]]]:
    """One row per sample across every manifest. Returns ``(columns, rows)``, both deterministic.

    Columns: ``sample_id, accession, assay, organism, n_files, files``. An empty ``accession`` means
    the sample has no archive id — absence, honestly, rather than a guessed value.
    """
    rows: list[dict[str, str]] = []
    for manifest in manifests:
        chemistry = manifest.library.chemistry.value[0]
        organism = str(manifest.experiment.organism.value)
        for sample in manifest.experiment.samples:
            rows.append(
                {
                    "sample_id": sample.sample_id,
                    "accession": sample.accession or "",
                    "assay": chemistry,
                    "organism": organism,
                    "n_files": str(len(sample.file_uris)),
                    "files": ";".join(sorted(Path(uri).name for uri in sample.file_uris)),
                }
            )
    rows.sort(key=lambda r: (r["assay"], r["sample_id"]))
    return list(_COLUMNS), rows


def format_tsv(columns: list[str], rows: list[dict[str, str]]) -> str:
    """Render ``(columns, rows)`` as a TSV string (trailing newline).

    Tabs/newlines in a value are squashed to spaces so a stray value can never break the table's
    shape.
    """

    def cell(value: str) -> str:
        return " ".join(str(value).split())

    lines = ["\t".join(columns)]
    lines += ["\t".join(cell(r.get(c, "")) for c in columns) for r in rows]
    return "\n".join(lines) + "\n"


def project_index(assays: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``project.yaml`` payload: the assays (sorted by chemistry) plus project-wide counts.

    Each assay entry names its chemistry, its subdir (``None`` for a single-assay flat project), its
    sample count, and its manifest / pipeline paths — enough to discover every per-assay output.
    """
    entries = sorted(assays, key=lambda a: str(a.get("chemistry", "")))
    return {
        "n_assays": len(entries),
        "n_samples": sum(int(a.get("n_samples", 0)) for a in entries),
        "assays": entries,
    }


def discover_assays(workspace: str | Path) -> list[tuple[str | None, Path]]:
    """``(subdir, manifest_path)`` for each assay under ``seqforge/``.

    A top-level ``manifest.yaml`` is a single, flat assay (subdir ``None``); otherwise every
    ``seqforge/<assay>/manifest.yaml`` is one assay. Sorted, so the result is deterministic.
    """
    root = state_dir(workspace)
    top = root / "manifest.yaml"
    if top.is_file():
        return [(None, top)]
    if not root.is_dir():
        return []
    found = [
        (child.name, child / "manifest.yaml")
        for child in root.iterdir()
        if child.is_dir() and (child / "manifest.yaml").is_file()
    ]
    return sorted(found, key=lambda pair: pair[0] or "")


def _relative_to(path: Any, workspace: str | Path) -> Any:
    """A path made relative to ``workspace`` when it is under it, else left untouched (None passes).

    Keeps ``project.yaml`` machine-independent: a workspace's absolute location must not leak into an
    index that is content-addressed reproducible and re-generatable anywhere the outputs are copied.
    """
    if not path:
        return path
    try:
        return str(Path(path).resolve().relative_to(Path(workspace).resolve()))
    except ValueError:
        return path


def join_study(
    columns: list[str],
    rows: list[dict[str, str]],
    manifests: list[DatasetManifest],
    study: StudyFolder,
) -> tuple[list[str], list[dict[str, str]], dict[str, Any]]:
    """The table with the study's reading joined in by BioSample accession, and what the join said.

    The manifests are untouched: the labels are a view of the reading and land in the table alone.
    A folder with no reading yet joins nothing, and says so.
    """
    said: dict[str, Any] = {"folder": str(study.path)}
    if study.reading is None:
        warning = ValidationWarning(
            code="STUDY_HAS_NO_READING",
            message=f"{study.path} holds no reading yet, so no labels were joined",
            subject=BlockerSubject(kind="file", ref=study.path.name),
        )
        return columns, rows, {**said, "reading_hash": None, "warnings": [warning.model_dump()]}
    samples = [
        CompiledSample(
            sample_id=sample.sample_id,
            accession=sample.accession,
            taxid=str(manifest.experiment.organism.value),
            assay_terms=tuple(label.curie for label in manifest.library.assay),
        )
        for manifest in manifests
        for sample in manifest.experiment.samples
    ]
    joined = join_reading(study.reading, samples, taken=columns)
    rows = [{**row, **joined.cells.get(row["sample_id"], {})} for row in rows]
    said |= {
        "reading_hash": joined.reading_hash,
        "columns": joined.columns,
        "warnings": [w.model_dump() for w in joined.warnings],
        "unchecked": joined.unchecked,
    }
    return [*columns, *joined.columns], rows, said


def write_project_views(
    workspace: str | Path, assays: list[dict[str, Any]], study: StudyFolder | None = None
) -> tuple[Path, Path, dict[str, Any] | None]:
    """Write ``sample_metadata.tsv`` + ``project.yaml`` at the project top and return their paths.

    ``assays`` is one dict per assay carrying at least ``chemistry``, ``subdir``, ``n_samples`` and a
    ``manifest`` path (optionally ``pipeline``/``snakefile``); the manifests are loaded to build the
    flat table. Written even for a single-assay project — the "one study" view is always useful. Paths
    in ``project.yaml`` are stored relative to ``workspace`` so the index is portable.

    With a ``study``, its reading is joined into the table (:func:`join_study`), and what the join
    said is returned third; without one, the third is ``None`` and the table is the manifests' alone.
    """
    root = state_dir(workspace)
    root.mkdir(parents=True, exist_ok=True)
    manifests = [
        DatasetManifest.model_validate(yaml.safe_load(Path(a["manifest"]).read_text()))
        for a in assays
    ]
    columns, rows = sample_metadata_table(manifests)
    joined = None
    if study is not None:
        columns, rows, joined = join_study(columns, rows, manifests, study)
    tsv_path = root / SAMPLE_METADATA_TSV
    _write_if_changed(tsv_path, format_tsv(columns, rows))

    portable = [
        {
            k: (_relative_to(v, workspace) if k in ("manifest", "snakefile", "pipeline") else v)
            for k, v in a.items()
        }
        for a in assays
    ]
    yaml_path = root / PROJECT_YAML
    _write_if_changed(yaml_path, yaml.safe_dump(project_index(portable), sort_keys=True))
    return tsv_path, yaml_path, joined


def _write_if_changed(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` unless the file already holds it.

    A compiled pipeline's labelling rule takes the sample table as an input, and snakemake re-runs a
    rule whose input is newer than its output: a table rewritten with the same text would relabel
    every sample on every run.
    """
    if path.is_file() and path.read_text() == text:
        return
    path.write_text(text)


def _read_table(table: Path) -> tuple[list[str], list[dict[str, str]]]:
    """The header and the rows of a table :func:`write_project_views` wrote; empty when absent."""
    if not table.is_file():
        return [], []
    lines = table.read_text().splitlines()
    header = lines[0].split("\t") if lines else []
    return header, [dict(zip(header, line.split("\t"), strict=True)) for line in lines[1:]]


def sample_labels(table: Path, sample_id: str) -> tuple[dict[str, str], str | None]:
    """One sample's labels in a table :func:`write_project_views` wrote, and the reading's hash.

    The labels are the columns a joined study added: every column past the compile's own but the
    hash. A sample with no reading hash carries no labels, which is what an absent table, a table
    written with no study, a sample the join did not reach and a sample not in the table all mean;
    each reads as ``({}, None)``.
    """
    header, rows = _read_table(table)
    row = next((r for r in rows if r.get("sample_id") == sample_id), {})
    digest = row.get(READING_HASH) or None
    if digest is None:
        return {}, None
    return {c: row[c] for c in header if c not in _COLUMNS and c != READING_HASH}, digest


def joined_reading_hash(workspace: str | Path) -> str | None:
    """The hash of the reading a workspace's sample table joined, or ``None`` when it joined none.

    :func:`write_project_views` writes the table in one go from one reading, so every row it
    labelled carries the same hash; a workspace with no table, or one written with no study, has
    none to give.
    """
    _, rows = _read_table(sample_metadata_path(workspace))
    return next((digest for r in rows if (digest := r.get(READING_HASH))), None)


__all__ = [
    "PROJECT_YAML",
    "SAMPLE_METADATA_TSV",
    "discover_assays",
    "format_tsv",
    "join_study",
    "joined_reading_hash",
    "project_index",
    "sample_labels",
    "sample_metadata_table",
    "write_project_views",
]
