"""Named ``Evidenced[T]`` specializations over the base scalars — stable ``$defs`` for schema export.

Pydantic generics are structural: ``Evidenced[str]`` has no stable class name, so exporting it inlines
an anonymous schema and the ``$defs`` churn every time an unrelated field moves. Naming each one keeps
the exported JSON Schema (design §1.8, the single source of truth) diffable.

These are the wrappers whose payload is a **base scalar**, so they can be shared by both halves of the
manifest without either half importing the other. A wrapper over a *domain* type lives next to that
type — :class:`~seqforge.models.dataset.EvidencedReadLayout` in ``dataset``,
:class:`~seqforge.models.processing.EvidencedGenome` in ``processing``. That is not a stylistic
preference: it is what keeps ``dataset`` and ``processing`` from importing each other, and that
independence is the two-artifact split expressed as an import graph rather than as a comment.

    base  ->  evidenced  ->  dataset
                         ->  processing        (dataset and processing never meet)
"""

from __future__ import annotations

from .base import Accession, AssayTerm, ChemistryId, Evidenced, NcbiTaxid


class EvidencedStr(Evidenced[str]):
    """An ``Evidenced`` string field."""


class EvidencedBool(Evidenced[bool]):
    """An ``Evidenced`` boolean field."""


class EvidencedTaxid(Evidenced[NcbiTaxid]):
    """An ``Evidenced`` NCBI taxid."""


class EvidencedAssay(Evidenced[AssayTerm]):
    """An ``Evidenced`` EFO/OBI assay CURIE."""


class EvidencedChemistrySet(Evidenced[list[ChemistryId]]):
    """An ``Evidenced`` chemistry equivalence class (benign twins recorded together, §12)."""


class EvidencedAccessionList(Evidenced[list[Accession]]):
    """An ``Evidenced`` list of accessions."""


__all__ = [
    "EvidencedStr",
    "EvidencedBool",
    "EvidencedTaxid",
    "EvidencedAssay",
    "EvidencedChemistrySet",
    "EvidencedAccessionList",
]
