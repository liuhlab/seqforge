"""Content-addressed, resumable ``.seqforge/`` artifacts: disk is state, context is cache.

Per-file :class:`Observation` keyed by file sha256; the dataset :class:`ResolveResult` keyed by
``dataset_id = sha256(sorted(file_shas) ⊕ kb_version)`` with ``probe_version`` / ``resolve_version``
folded in — so a probe or scorer change invalidates the cache without a manual bump. Every write is
atomic-ish (write-then-rename); a corrupt/absent artifact reads back as ``None`` (recompute), never a
crash.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..models.observation import Observation
from ..models.resolve import ResolveResult
from ..workspace import state_dir


def dataset_id(
    file_shas: list[str], kb_version: str, probe_version: str, resolve_version: str
) -> str:
    """The content-addressed dataset id — stable under file order, sensitive to tool versions."""
    key = "\n".join(sorted(file_shas))
    key += f"|kb={kb_version}|probe={probe_version}|resolve={resolve_version}"
    return hashlib.sha256(key.encode()).hexdigest()


class Cache:
    """Reader/writer for the ``.seqforge/`` artifact tree rooted at a workspace."""

    def __init__(self, workspace: str | Path) -> None:
        self.root = state_dir(workspace)

    def _obs_path(self, sha: str) -> Path:
        return self.root / "observations" / f"{sha}.json"

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

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text)
        tmp.replace(path)
