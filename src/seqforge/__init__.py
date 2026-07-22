"""seqforge — compile FASTQ + metadata into a validated library manifest and a Snakemake config.

A compiler, not a chatbot: deterministic code owns every decision; the LLM only parses prose into
span-verified assertions and arbitrates already-flagged ambiguity. See ``docs/design.md``.
"""

from __future__ import annotations

try:  # pragma: no cover - version is provided by the installed package metadata
    from importlib.metadata import version

    __version__ = version("seqforge")
except Exception:  # pragma: no cover - not installed; mirror the static CalVer in pyproject.toml
    __version__ = "2026.7.1"

__all__ = ["__version__"]
