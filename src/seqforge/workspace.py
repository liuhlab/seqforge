"""Where seqforge keeps its state, and the one place that name is spelled.

``seqforge/``, not ``.seqforge/``. The leading dot said "this is plumbing, look away", and that was
exactly backwards: this directory holds the manifest, the resolve verdicts, the rendered documents a
citation greps into, and the compiled Snakefile the user submits. It is not cache — it is the
**output**, and R7's "disk is state, context is cache" is a statement about which of the two matters.
A user who does not know it exists cannot read their own manifest, and hiding the artifacts of a
compiler whose whole product is artifacts is a strange thing to have done.

One constant, because the alternative is what this repo keeps finding: the literal was written out in
five modules, and five copies of a string is five chances for one of them to be stale.

.. warning::

   A ``.gitignore`` entry for this must be **anchored** (``/seqforge/``). An unanchored ``seqforge/``
   matches any directory of that name at any depth, which in this repo means ``src/seqforge/`` — git
   would ignore our own source tree, and it would do it silently.
"""

from __future__ import annotations

from pathlib import Path

#: The directory seqforge writes under a workspace. Visible on purpose; see the module docstring.
STATE_DIRNAME = "seqforge"

#: The dot-prefixed name this replaced. Kept so :func:`legacy_state_dir` can find an old workspace
#: and say so, rather than silently starting a second one beside it.
LEGACY_STATE_DIRNAME = ".seqforge"


def state_dir(workspace: str | Path = ".", *parts: str) -> Path:
    """``<workspace>/seqforge/<parts...>``. Does not create anything — callers that write, mkdir."""
    return Path(workspace).joinpath(STATE_DIRNAME, *parts)


def legacy_state_dir(workspace: str | Path = ".") -> Path | None:
    """An old ``.seqforge/`` in this workspace, if one is there.

    Returned rather than migrated. Moving a user's artifacts without being asked is not this program's
    business, and a rename that half-succeeds on a killed run would be worse than the two directories.
    The CLI mentions it once and gets on with its life.
    """
    old = Path(workspace) / LEGACY_STATE_DIRNAME
    return old if old.is_dir() else None


__all__ = ["STATE_DIRNAME", "LEGACY_STATE_DIRNAME", "state_dir", "legacy_state_dir"]
