"""seqforge — compile FASTQ + metadata into a validated library manifest and a Snakemake config.

A compiler, not a chatbot: deterministic code owns every decision; the LLM only parses prose into
span-verified assertions and arbitrates already-flagged ambiguity. ``CONTEXT-MAP.md`` and the
per-context ``CONTEXT.md`` files are the glossary; the decisions behind it are recorded in
``docs/adr/`` and beside the code each governs.
"""

from __future__ import annotations

from importlib.metadata import version

#: The installed distribution's version, which hatch-vcs took from the git tag. The package version
#: is written by hand nowhere in this repo.
__version__ = version("seqforge")

__all__ = ["__version__"]
