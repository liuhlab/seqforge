"""Cells-vs-nuclei prep recognition — the single home for the vocabulary.

Two callers read the SAME words, so the words live once, here:

- :mod:`seqforge.harvest.verify` checks that a ``library.prep_type`` quote *entails* its value — a
  terse "snRNA-seq" span must support a verbose "single-nucleus RNA sequencing" value, and a cell
  quote must never support a nucleus value.
- :mod:`seqforge.manifest.policy` normalizes the span-verified value into the prep that steers which
  matrix is primary (``GeneFull`` for nuclei).

``harvest`` is upstream of ``manifest`` (it produces the Assertions ``manifest`` consumes), so
``manifest`` imports this and not the other way round. The module depends on nothing but ``re``, so the
edge stays light and acyclic — importing it drags no LLM/PDF machinery into the compile path.
"""

from __future__ import annotations

import re

#: Word-boundary patterns for the two preps. Anchored on WHOLE words (not a bare "nucle"/"cell"
#: substring, which would misread "nucleic acid" as nuclei or "Cell Ranger" as single-cell), and kept
#: to the specific terms a methods section actually uses for the input material.
_NUCLEUS_RE = re.compile(
    r"\b(?:single[-\s]?nucle(?:us|i)|nuclei|nuclear|nucleus|sn-?rna|sn-?seq)\b", re.I
)
_CELL_RE = re.compile(r"\b(?:single[-\s]?cells?|sc-?rna|sc-?seq|whole[-\s]?cells?)\b", re.I)


def normalize_prep_type(raw: str) -> str | None:
    """A free-text prep phrase -> ``single-cell`` | ``single-nucleus`` | ``None``.

    Code's job, not the model's: the model reports the biology in the paper's words, this maps those
    words to one of two values. ``None`` when the phrase names neither clearly, OR names BOTH (so
    nothing is guessed) — the value steers which matrix is primary, so an ambiguous phrase must not
    silently pick one.
    """
    nucleus = bool(_NUCLEUS_RE.search(raw))
    cell = bool(_CELL_RE.search(raw))
    if nucleus == cell:  # neither term, or both -> refuse to guess
        return None
    return "single-nucleus" if nucleus else "single-cell"
