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

from .models.dataset import DatasetManifest
from .workspace import state_dir

#: Common BioSample attributes, ordered for readability. Any *other* resolved attribute follows,
#: sorted — the column set is the union of what actually resolved, never a hand-fixed schema.
_PREFERRED_ATTRS: tuple[str, ...] = (
    "strain",
    "genotype",
    "age",
    "dev_stage",
    "sex",
    "tissue",
    "cell_type",
    "cell_line",
    "treatment",
    "source_name",
    "disease",
)

_LEADING: tuple[str, ...] = ("sample_id", "accession", "assay", "organism")
_TRAILING: tuple[str, ...] = ("n_files", "files")

#: The project-level file names, written at the top of ``seqforge/`` (never inside an assay subdir).
SAMPLE_METADATA_TSV = "sample_metadata.tsv"
PROJECT_YAML = "project.yaml"


def sample_metadata_table(
    manifests: list[DatasetManifest],
) -> tuple[list[str], list[dict[str, str]]]:
    """One row per sample across every manifest. Returns ``(columns, rows)``, both deterministic.

    Columns: ``sample_id, accession, assay, organism``, then every resolved BioSample attribute
    (common ones first, the rest sorted), then ``n_files, files``. An empty cell means the attribute
    did not resolve for that sample — absence, honestly, rather than a guessed value.
    """
    rows: list[dict[str, str]] = []
    seen_attrs: set[str] = set()
    for manifest in manifests:
        chemistry = manifest.library.chemistry.value[0]
        organism = str(manifest.experiment.organism.value)
        for sample in manifest.experiment.samples:
            attrs = {key: ev.value for key, ev in sample.attributes.items()}
            seen_attrs.update(attrs)
            rows.append(
                {
                    "sample_id": sample.sample_id,
                    "accession": sample.accession or "",
                    "assay": chemistry,
                    "organism": organism,
                    "n_files": str(len(sample.file_uris)),
                    "files": ";".join(sorted(Path(uri).name for uri in sample.file_uris)),
                    **attrs,
                }
            )
    attr_cols = [a for a in _PREFERRED_ATTRS if a in seen_attrs]
    attr_cols += sorted(seen_attrs - set(_PREFERRED_ATTRS))
    columns = [*_LEADING, *attr_cols, *_TRAILING]
    rows.sort(key=lambda r: (r["assay"], r["sample_id"]))
    return columns, rows


def format_tsv(columns: list[str], rows: list[dict[str, str]]) -> str:
    """Render ``(columns, rows)`` as a TSV string (trailing newline). Tabs/newlines in a value are
    squashed to spaces so a stray attribute value can never break the table's shape."""

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


def write_project_views(workspace: str | Path, assays: list[dict[str, Any]]) -> tuple[Path, Path]:
    """Write ``sample_metadata.tsv`` + ``project.yaml`` at the project top and return their paths.

    ``assays`` is one dict per assay carrying at least ``chemistry``, ``subdir``, ``n_samples`` and a
    ``manifest`` path (optionally ``pipeline``/``snakefile``); the manifests are loaded to build the
    flat table. Written even for a single-assay project — the "one study" view is always useful. Paths
    in ``project.yaml`` are stored relative to ``workspace`` so the index is portable.
    """
    root = state_dir(workspace)
    root.mkdir(parents=True, exist_ok=True)
    manifests = [
        DatasetManifest.model_validate(yaml.safe_load(Path(a["manifest"]).read_text()))
        for a in assays
    ]
    columns, rows = sample_metadata_table(manifests)
    tsv_path = root / SAMPLE_METADATA_TSV
    tsv_path.write_text(format_tsv(columns, rows))

    portable = [
        {
            k: (_relative_to(v, workspace) if k in ("manifest", "snakefile", "pipeline") else v)
            for k, v in a.items()
        }
        for a in assays
    ]
    yaml_path = root / PROJECT_YAML
    yaml_path.write_text(yaml.safe_dump(project_index(portable), sort_keys=True))
    return tsv_path, yaml_path


__all__ = [
    "SAMPLE_METADATA_TSV",
    "PROJECT_YAML",
    "sample_metadata_table",
    "format_tsv",
    "project_index",
    "discover_assays",
    "write_project_views",
]
