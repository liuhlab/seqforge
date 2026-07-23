"""The ``seqforge`` Typer application, assembled from one module per command group.

The CLI is the API: every skill action maps to a deterministic ``seqforge <verb>`` (JSON on stdout by
default) that runs with no LLM in the loop -- only ``harvest extract`` and the opt-in
``resolve adjudicate`` touch an LLM. Exit codes are uniform: ``0`` OK, ``1`` ERROR, ``2`` USAGE,
``3`` BLOCKED (a Blocker), ``4`` NEEDS_HUMAN (an open Conflict / question).

Importing this package builds ``app``: :mod:`.root` defines the shared Typer instances, and importing
each command module registers its verbs onto them. A handful of internals are re-exported because the
test suite and ``seqforge run`` reach for them by name.
"""

from __future__ import annotations

# Importing each command module runs its @command decorators, registering the verbs onto `app`.
# The imports look unused; the registration is the side effect that assembles the CLI.
from . import (  # noqa: F401
    compose,
    eval,
    harvest,
    hook,
    io,
    kb,
    manifest,
    preflight,
    probe,
    processing,
    project,
    report,
    resolve,
    run,
    schema,
)
from ._common import _emit, _StageOut  # noqa: F401
from .harvest import _harvest_extract_pipeline  # noqa: F401
from .manifest import _fill_manifest_pipeline  # noqa: F401
from .root import app
from .run import _harvest_halts_run  # noqa: F401

__all__ = ["app"]
