"""Content-addressed, resumable ``seqforge/`` artifacts: disk is state, context is cache.

Per-file :class:`Observation` keyed by its content-address (a bounded local key or a provider md5 —
never a whole-file scan; see :func:`~seqforge.probe.core._content_key`) **under a ``PROBE_VERSION``
namespace**: a provider-md5 address is deliberately N-invariant, so without that namespace a probe
change (e.g. a smaller default read budget) would silently re-serve stale observation *values* for a
hosted file whose address did not move; the namespace makes "recompute once on a probe bump" hold for
every address type. The dataset :class:`ResolveResult` is keyed by
``dataset_id = sha256(sorted(file_shas) ⊕ kb_version)`` with ``probe_version`` / ``resolve_version``
folded in — so a probe or scorer change invalidates the cache without a manual bump. :func:`resume_key` adds a stat-only pointer (``realpath+size+mtime`` →
``dataset_id``) so an unchanged re-run rebuilds the answer without reading a FASTQ byte. Every write is
atomic-ish (write-then-rename); a corrupt/absent artifact reads back as ``None`` (recompute), never a
crash.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from pathlib import Path

from ..models.observation import Observation
from ..models.resolve import ResolveResult
from ..probe import PROBE_VERSION
from ..workspace import cache_dir


def dataset_id(
    file_shas: list[str], kb_version: str, probe_version: str, resolve_version: str
) -> str:
    """The content-addressed dataset id — stable under file order, sensitive to tool versions."""
    key = "\n".join(sorted(file_shas))
    key += f"|kb={kb_version}|probe={probe_version}|resolve={resolve_version}"
    return hashlib.sha256(key.encode()).hexdigest()


def resume_key(
    paths: Iterable[str | Path], kb_version: str, probe_version: str, resolve_version: str
) -> str | None:
    """A stat-only key over the input files: same (realpath, size, mtime) set -> same key.

    This is the LOCAL "did anything change since the last run?" check that lets a re-run skip probing
    and read ZERO FASTQ bytes. It is deliberately NOT the content-addressed :func:`dataset_id` (which
    needs the bytes): mtime is the standard cheap heuristic, and ``--no-cache`` is the escape hatch.
    Returns ``None`` if any file is missing — nothing to resume against, so probe afresh.
    """
    parts: list[str] = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            return None
        parts.append(f"{os.path.realpath(p)}|{st.st_size}|{st.st_mtime_ns}")
    key = "\n".join(sorted(parts))
    key += f"|kb={kb_version}|probe={probe_version}|resolve={resolve_version}"
    return hashlib.sha256(key.encode()).hexdigest()


class Cache:
    """Reader/writer for the ``seqforge/cache/`` artifact tree rooted at a workspace.

    These are content-addressed, resumable, and safe to delete — so they live under ``cache/``, not
    beside the manifest a human reads.
    """

    def __init__(self, workspace: str | Path) -> None:
        self.root = cache_dir(workspace)

    def _obs_path(self, sha: str) -> Path:
        # Namespaced by PROBE_VERSION: a probe-semantics bump (e.g. the N=2000 default) must recompute
        # observations once even for md5-addressed files, whose content-address does not move with N.
        return self.root / "observations" / PROBE_VERSION / f"{sha}.json"

    def _resolve_path(self, ds_id: str) -> Path:
        return self.root / "candidates" / f"{ds_id}.json"

    def read_observation(self, sha: str) -> Observation | None:
        path = self._obs_path(sha)
        if not path.is_file():
            return None
        try:
            return Observation.model_validate_json(path.read_text())
        except (ValueError, OSError):
            return None

    def write_observation(self, obs: Observation) -> None:
        self._write(self._obs_path(obs.file.sha256), obs.model_dump_json(indent=2))

    def read_resolve(self, ds_id: str) -> ResolveResult | None:
        path = self._resolve_path(ds_id)
        if not path.is_file():
            return None
        try:
            return ResolveResult.model_validate_json(path.read_text())
        except (ValueError, OSError):
            return None

    def write_resolve(self, ds_id: str, result: ResolveResult) -> None:
        self._write(self._resolve_path(ds_id), result.model_dump_json(indent=2))

    def _resume_path(self, key: str) -> Path:
        return self.root / "resume" / f"{key}.json"

    def read_resume(self, key: str) -> dict[str, object] | None:
        """Read a stat-keyed resume pointer (see :func:`resume_key`); ``None`` if absent/corrupt."""
        path = self._resume_path(key)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def write_resume(self, key: str, payload: dict[str, object]) -> None:
        self._write(self._resume_path(key), json.dumps(payload, indent=2))

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text)
        tmp.replace(path)
