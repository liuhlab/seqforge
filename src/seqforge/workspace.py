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

import re
from pathlib import Path

#: The directory seqforge writes under a workspace. Visible on purpose; see the module docstring.
STATE_DIRNAME = "seqforge"

#: How much of a content hash to keep in a name a human reads. Twelve hex characters is 48 bits; at
#: the scale these address — the documents of one dataset, the recipes for one dataset — a collision
#: is not a thing that happens, and the other 52 characters are what made the directories unreadable.
SHORT_HASH = 12

#: The dot-prefixed name this replaced. Kept so :func:`legacy_state_dir` can find an old workspace
#: and say so, rather than silently starting a second one beside it.
LEGACY_STATE_DIRNAME = ".seqforge"


def state_dir(workspace: str | Path = ".", *parts: str) -> Path:
    """``<workspace>/seqforge/<parts...>``. Does not create anything — callers that write, mkdir."""
    return Path(workspace).joinpath(STATE_DIRNAME, *parts)


def readable(name: str, digest: str) -> str:
    """``("default", "a3f8...")`` -> ``default-a3f8c19d2b04``. A name a human can find, plus identity.

    Both halves earn their place. The hash is the identity — two recipes over one dataset are two
    runs, and a name alone cannot keep them apart. But a directory of bare 64-hex names is a
    directory you cannot navigate, and that is what `pipeline/` and `normalized/` were: the pilot's
    workspace had six documents and one pipeline in it, and nothing on disk said which was which.

    No model is involved and none is needed. The recipe already has a name and the document already
    has a filename; we simply stopped throwing them away.
    """
    kept = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in name).strip("-.")
    stem = re.sub(r"-{2,}", "-", kept)[:60] or "run"
    return f"{stem}-{digest[:SHORT_HASH]}"


def legacy_state_dir(workspace: str | Path = ".") -> Path | None:
    """An old ``.seqforge/`` in this workspace, if one is there.

    Returned rather than migrated. Moving a user's artifacts without being asked is not this program's
    business, and a rename that half-succeeds on a killed run would be worse than the two directories.
    The CLI mentions it once and gets on with its life.
    """
    old = Path(workspace) / LEGACY_STATE_DIRNAME
    return old if old.is_dir() else None


__all__ = [
    "STATE_DIRNAME",
    "LEGACY_STATE_DIRNAME",
    "SHORT_HASH",
    "state_dir",
    "readable",
    "legacy_state_dir",
]
