"""The ``seqforge`` Typer application, assembled from one module per command group.

The CLI is the API: every skill action maps to a deterministic ``seqforge <verb>`` (JSON on stdout by
default) that runs with no LLM in the loop -- only ``harvest extract`` and the opt-in
``resolve adjudicate`` touch an LLM. Exit codes are uniform: ``0`` OK, ``1`` ERROR, ``2`` USAGE,
``3`` BLOCKED (a Blocker), ``4`` NEEDS_HUMAN (an open Conflict / question).

Importing this package builds ``app``: :mod:`.root` defines the shared Typer instances, and importing
each command module registers its verbs onto them. ``app`` is the only name this package exports --
``seqforge run`` and the test suite both reach a stage body through the module that defines it, so
there is nothing else to re-export.
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
from .root import app

__all__ = ["app"]
