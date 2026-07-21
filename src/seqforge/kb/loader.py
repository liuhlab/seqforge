"""Discover and load KB ``spec.yaml`` files, validating each against the :class:`Spec` schema."""

from __future__ import annotations

from dataclasses import dataclass
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


def runnable_spec_ids() -> list[str]:
    """Ids of specs that compile to a recipe — leaves and runnable families, not abstract nodes.

    An abstract family node declares no ``backend``: it classifies during descent but is never scored,
    composed, or params-gated. Tests and tools over "every chemistry" collect from here (derived from
    the KB, not hand-maintained) so a family node never masquerades as a runnable one.
    """
    return [tech_id for tech_id in list_spec_ids() if load_spec(tech_id).backend is not None]


@dataclass(frozen=True)
class KbTree:
    """The KB as a parent/child forest.

    A **family** node narrows to its children; a **leaf** is a concrete, runnable chemistry. Siblings
    (same parent) are confusable-by-construction, decided by the parent's ``children_decided_by`` — so
    the resolver reads sibling relationships off the tree instead of a hand-declared ``confusable_with``
    clique. ``children`` is ``node id -> sorted child ids``.
    """

    specs: dict[str, Spec]
    children: dict[str, list[str]]

    def children_of(self, tech_id: str) -> list[str]:
        return list(self.children.get(tech_id, []))

    def is_family(self, tech_id: str) -> bool:
        return bool(self.children.get(tech_id))

    def parent_of(self, tech_id: str) -> str | None:
        return self.specs[tech_id].parent

    def siblings_of(self, tech_id: str) -> list[str]:
        """Other children of this node's parent (empty for a root)."""
        parent = self.specs[tech_id].parent
        if parent is None:
            return []
        return [c for c in self.children.get(parent, []) if c != tech_id]

    def ancestors_of(self, tech_id: str) -> list[str]:
        """The parent chain from this node up to (and including) its root, nearest first."""
        out: list[str] = []
        seen: set[str] = set()
        cur = self.specs[tech_id].parent
        while cur is not None and cur not in seen:
            out.append(cur)
            seen.add(cur)
            cur = self.specs[cur].parent if cur in self.specs else None
        return out

    def runnable_descendants_of(self, tech_id: str) -> list[str]:
        """Every leaf / runnable family at or below this node (what descent may terminate on)."""
        out: list[str] = []
        stack = [tech_id]
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            if self.specs[node].backend is not None:
                out.append(node)
            stack.extend(self.children.get(node, []))
        return sorted(out)

    def leaves(self) -> list[str]:
        """Ids with no children — the concrete, compilable chemistries."""
        return sorted(i for i in self.specs if not self.children.get(i))


def build_tree(specs: dict[str, Spec]) -> KbTree:
    """Build and VALIDATE the KB forest; raise ``ValueError`` on a malformed tree.

    Checks: every ``parent`` resolves; no cycles; ``node_kind`` agrees with having children; every
    abstract family (a family with no backend) has at least one runnable descendant, so descent from it
    can always terminate at something that compiles.
    """
    children: dict[str, list[str]] = {}
    for tech_id, spec in specs.items():
        parent = spec.parent
        if parent is None:
            continue
        if parent == tech_id:
            raise ValueError(f"{tech_id!r}: a node cannot be its own parent")
        if parent not in specs:
            raise ValueError(f"{tech_id!r}: parent {parent!r} is not a known spec")
        children.setdefault(parent, []).append(tech_id)
    for parent in children:
        children[parent] = sorted(children[parent])

    for tech_id in specs:  # no parent cycles: every chain must terminate at a root
        seen: set[str] = set()
        cur: str | None = tech_id
        while cur is not None:
            if cur in seen:
                raise ValueError(f"parent cycle through {tech_id!r}")
            seen.add(cur)
            cur = specs[cur].parent

    tree = KbTree(specs=specs, children=children)
    for tech_id, spec in specs.items():
        has_children = bool(children.get(tech_id))
        if has_children and spec.node_kind != "family":
            raise ValueError(f"{tech_id!r} has children but is not declared node_kind: family")
        if spec.node_kind == "family" and not has_children:
            raise ValueError(f"{tech_id!r} is declared a family but has no children")
        if spec.node_kind == "family" and spec.backend is None:
            if not tree.runnable_descendants_of(tech_id):
                raise ValueError(
                    f"abstract family {tech_id!r} has no runnable descendant to compile"
                )
    return tree


def load_tree() -> KbTree:
    """Load every spec and build the validated KB tree."""
    return build_tree(load_all_specs())
