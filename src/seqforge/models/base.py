"""Shared scalars, controlled vocabularies, and the ``Evidenced[T]`` three-truths carrier.

These are the atoms every other model is built from. ``Evidenced[T]`` wraps every interpretive
manifest field so a value never travels without its provenance.
"""

from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
)


def _reject_absolute_or_local(value: str) -> str:
    """Reject absolute or local filesystem paths in a manifest URI.

    A manifest URI is a relative path, a non-file scheme (``s3://``, ``gs://``, ``https://``,
    ``sra:``), or a bare accession — never ``/abs``, ``~/x``, ``C:\\...``, a UNC ``\\\\host``, or a
    ``file://`` absolute path.
    """
    bad = (
        value.startswith(("/", "~"))
        or value.startswith("file:///")
        or (len(value) > 1 and value[1] == ":")  # Windows drive, e.g. C:\
        or value.startswith("\\\\")  # UNC \\host\share
    )
    if bad:
        raise ValueError(f"absolute/local path forbidden in a manifest URI: {value!r}")
    return value


# ---- scalars & controlled vocabulary ----
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""Lowercase hex sha256 of a file's bytes."""

Uri = Annotated[str, StringConstraints(min_length=1), AfterValidator(_reject_absolute_or_local)]
"""A manifest URI: relative path / non-file scheme / bare accession. Never an absolute local path."""

LocalPath = Annotated[str, StringConstraints(min_length=1)]
"""An internal-only local filesystem path (e.g. where probe read bytes). Never enters a Manifest."""

AssayTerm = Annotated[str, StringConstraints(pattern=r"^(EFO|OBI):\d{4,}$")]
"""EFO/OBI assay CURIE, e.g. ``EFO:0009922``."""

NcbiTaxid = PositiveInt
"""NCBI taxonomy id, e.g. 9606, 10090, 559292, 6239."""

Accession = Annotated[
    str,
    StringConstraints(pattern=r"^([SED]R[RXPS]\d+|GS[EM]\d+|PRJ[A-Z]{2}\d+|SAM[NED][A-Z]?\d+)$"),
]
"""NCBI/ENA/DDBJ run/experiment/study/sample, GEO, BioProject, or BioSample accession."""

ChemistryId = str
"""KB primary key, e.g. ``10x-3p-gex-v3``. Open vocabulary, validated against the KB in code."""

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
Rung = Annotated[int, Field(ge=0, le=7)]
Basis = Literal["observed", "asserted", "inferred", "user_confirmed"]


T = TypeVar("T")


class Evidenced(BaseModel, Generic[T]):
    """A single manifest field value tagged with its epistemic provenance.

    Wraps every interpretive manifest field. ``basis`` records how we know it
    (``observed`` from bytes, ``asserted`` from humans/DBs, ``inferred`` derived,
    ``user_confirmed``); disagreement across bases becomes a first-class ``Conflict``, never a
    silent merge. ``rung`` is the cheapest escalation-ladder step that settled the field.

    ``confidence`` is **optional, and ``None`` is the informative value**: it means no judgement was
    made, so there is no confidence to report. Copying a ``strain = CQ758`` out of a BioSample record
    involves no interpretation — the record says it, we transcribed it, and ``basis="asserted"`` plus
    the record accession in ``evidence`` already says everything true about how we know it. Writing
    ``1.0`` there would invite exactly the question it cannot answer ("you are certain the strain is
    CQ758?"); we are certain the *record declares* it, which is a different claim and the one we make.

    So: a number here means somebody or something weighed evidence and could have been wrong. That is
    the winning candidate's score, or a language model's advisory self-report on reading prose. It is
    never a decoration, and it is never copied from a neighbouring field — a value repeated across
    four fields is one judgement wearing four hats, which is the ``processing``-masquerading-as-a-truth
    mistake in miniature.
    """

    model_config = ConfigDict(frozen=True)

    value: T
    basis: Basis
    evidence: list[str] = Field(default_factory=list)
    confidence: Confidence | None = None
    rung: Rung
