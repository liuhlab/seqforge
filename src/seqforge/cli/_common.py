"""Shared CLI helpers used across command groups: stage results, safe loaders, small parsers.

None of these touch a Typer app -- they are the plumbing every command group reuses. `_StageOut`
decouples *what a stage says and whether it refused* (the exit code) from *where that output goes*,
which is what lets one stage body serve both a standalone verb and the one-pass `run`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import typer
import yaml
from pydantic import ValidationError

from ..io.taxonomy import resolve as resolve_organism
from ..models.dataset import DatasetManifest
from ..models.processing import ProcessingManifest


def _today() -> str:
    """Today, for the ``fetched`` stamp on a generated vocabulary file.

    Local import and a function rather than a module constant: a constant would be evaluated at import
    time, and every artifact seqforge writes is content-addressed — a clock reachable from module
    scope is a clock that eventually ends up inside a hash.
    """
    import datetime

    return datetime.date.today().isoformat()


@dataclass(frozen=True)
class _StageOut:
    """One stage's result, decoupled from how it is printed.

    A stage decides *what* to say and *whether it is a refusal* (the exit code); the command wrapper
    decides *where* it goes. That split is what lets a single stage body serve both a standalone verb
    (which echoes it and exits) and ``seqforge run`` (which folds it into one summary). ``payload`` is
    a dict rendered as JSON, or a bare string echoed as-is — ``FillError`` prints a plain sentence,
    ``records_unavailable`` prints JSON, and both must keep doing exactly that.
    """

    payload: dict[str, object] | str
    code: int
    err: bool = False


def _emit(out: _StageOut) -> None:
    """Print a stage result the way a standalone verb does, then exit with its code."""
    body = out.payload if isinstance(out.payload, str) else json.dumps(out.payload, indent=2)
    typer.echo(body, err=out.err)
    raise typer.Exit(out.code)


def _auto_cpus(cpus: int) -> int:
    """Resolve ``--cpus``: a positive value is taken as-is; ``0`` means auto = ``min(8, detected)``.

    Files probe in parallel across processes, and cores are not a budget — this only decides how
    fast, never what. ``0`` is the default so the common multicore case is fast without a flag, while a
    shared login node can be pinned with ``--cpus 1``. The cap at 8 keeps a 96-core node from
    fork-bombing itself on a 12-file dataset where the win is already gone by 8.
    """
    if cpus > 0:
        return cpus
    import os

    return max(1, min(8, os.cpu_count() or 1))


def _load_manifest(path: Path) -> DatasetManifest:
    try:
        return DatasetManifest.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, ValidationError, ValueError) as exc:
        typer.echo(f"cannot read manifest {path}: {exc}", err=True)
        raise typer.Exit(2) from exc


def _load_processing(path: Path) -> ProcessingManifest:
    try:
        return ProcessingManifest.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, ValidationError, ValueError) as exc:
        typer.echo(f"cannot read processing manifest {path}: {exc}", err=True)
        raise typer.Exit(2) from exc


def _resolve_organism(value: str, *, offline: bool = False) -> int:
    """`--organism` takes a taxid or a name. A bare integer is taken at face value.

    Not "is it all digits, else look it up" with a fallback -- a name that happens to be numeric is
    not a thing, and a taxid that fails to parse should say so rather than be searched for on NCBI.
    """
    text = value.strip()
    if text.isdigit():
        return int(text)
    return resolve_organism(text, offline=offline)


def _parse_quantify(value: str | None) -> tuple[str, ...] | None:
    """`--quantify Gene,GeneFull` -> the tuple. The MODEL validates membership, not this parser."""
    if value is None:
        return None
    return tuple(v.strip() for v in value.split(",") if v.strip())
