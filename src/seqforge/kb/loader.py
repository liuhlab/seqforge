"""Discover and load KB ``spec.yaml`` files, validating each against the :class:`Spec` schema."""

from __future__ import annotations

from pathlib import Path

import yaml

from .schema import Spec

SPECS_DIR = Path(__file__).parent / "specs"


def list_spec_ids() -> list[str]:
    """Return the ids of every technology directory that ships a ``spec.yaml``."""
    if not SPECS_DIR.is_dir():
        return []
    return sorted(p.name for p in SPECS_DIR.iterdir() if (p / "spec.yaml").is_file())


def load_spec(tech_id: str) -> Spec:
    """Load and validate one technology's ``spec.yaml``."""
    path = SPECS_DIR / tech_id / "spec.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"no KB spec for {tech_id!r} at {path}")
    data = yaml.safe_load(path.read_text())
    return Spec.model_validate(data)


def load_all_specs() -> dict[str, Spec]:
    """Load and validate every KB spec (the deterministic core of ``kb lint``)."""
    return {tech_id: load_spec(tech_id) for tech_id in list_spec_ids()}
