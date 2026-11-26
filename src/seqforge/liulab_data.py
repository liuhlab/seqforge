"""liulab-data, the lab's data package, as every seqforge command that needs it imports it.

liulab-data is a pixi dependency and not a wheel one, so an installed copy of seqforge can lack it.
:func:`needed` turns that into one :class:`LiulabDataMissing` with one remedy. What a command does
with it is its own: ``io`` and ``study check-reading`` refuse, and the report shows ids alone. The
module belongs to none of them, so each reaches the same sentence.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

#: What to do when liulab-data is not importable.
REMEDY = (
    "Run seqforge from liulab-runtime, which ships it, or install it: "
    "pip install git+https://github.com/liuhlab/liulab-data.git"
)


class LiulabDataMissing(Exception):
    """liulab-data is not importable. Its message names the import that failed and the remedy."""


@contextmanager
def needed() -> Iterator[None]:
    """Raise :class:`LiulabDataMissing` for an import inside the block that fails."""
    try:
        yield
    except ImportError as exc:
        raise LiulabDataMissing(f"liulab-data is not importable ({exc}). {REMEDY}") from exc
