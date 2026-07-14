"""The KB ``spec.yaml`` schema — machine-checkable, closed-vocabulary, self-validating (R1/R10).

One directory per technology: ``kb/specs/<tech>/{spec.yaml, README.md}``. ``spec.yaml`` declares the
read layout (element coordinates), onlist references, a detection ``signature`` (requires / supports /
excludes), a ``backend`` param template, and a ``confusable_with`` list. Every model forbids extra
keys, so a typo fails validation exactly where the DSL is executed. The signature test vocabulary is
*exactly* the scorer's evaluator set.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ElementType = Literal[
    "barcode", "umi", "cdna", "gdna", "linker", "poly_a", "poly_t", "fixed", "index"
]
Mechanism = Literal["none", "onlist", "metadata", "alignment", "user"]
Decidable = Literal["reads", "onlist", "metadata", "alignment", "user"]
Orientation = Literal["forward", "revcomp", "either"]
SeqspecRegion = Literal[
    "barcode",
    "umi",
    "cdna",
    "gdna",
    "index5",
    "index7",
    "linker",
    "poly_A",
    "poly_t",
    "custom_primer",
]

_ONLIST_TOKEN = re.compile(r"^\{onlist:([A-Za-z0-9._-]+)\}$")
_ANY_BRACE = re.compile(r"\{[^}]*\}")


class _Forbid(BaseModel):
    """Base that forbids unknown keys, so the closed vocabulary is enforced where it is executed."""

    model_config = ConfigDict(extra="forbid")


class Anchor(_Forbid):
    """Locate a variable-length / floating element (e.g. inDrop's post-W1 barcode)."""

    relative_to: Literal["read_start", "read_end", "element"] = "read_start"
    ref_element: str | None = None
    ref_side: Literal["start", "end"] = "end"
    offset: int = 0
    motif: str | None = None
    max_mismatch: int = 0


class Element(_Forbid):
    """One element of a read. 0-based half-open ``[start, end)``; ``end=None`` => open-ended (cDNA)."""

    type: ElementType
    name: str
    start: int | None = None
    end: int | None = None
    min_len: int | None = None
    max_len: int | None = None
    anchor: Anchor | None = None
    sequence: str | None = None
    onlist: str | None = None
    seqspec_region_type: SeqspecRegion

    @model_validator(mode="after")
    def _addressable(self) -> Element:
        fixed = self.start is not None and self.end is not None
        opened = self.start is not None and self.end is None
        varlen = self.min_len is not None or self.max_len is not None
        anchored = self.anchor is not None
        if self.type in ("linker", "fixed") and self.sequence is None:
            raise ValueError(f"element {self.name!r}: linker/fixed needs a literal `sequence`")
        if self.type in ("cdna", "gdna"):
            return self  # open-ended is legal
        if not (fixed or opened or anchored or varlen):
            raise ValueError(f"element {self.name!r}: give [start,end), an anchor, or min/max_len")
        return self


class Read(_Forbid):
    """A read (== one FASTQ). ``id`` is a ROLE label (R1/R2/bc/cdna), never a filename claim."""

    id: str
    seqspec_read_id: str
    file_hint: str | None = None
    strand: Literal["pos", "neg"] = "pos"
    min_len: int | None = None
    max_len: int | None = None
    elements: list[Element]


class OnlistRef(_Forbid):
    """Alias -> pooch-registry name. URL/sha256/length/orientation live in the registry, never here."""

    registry: str
    role: Literal["cell_barcode", "sample_index", "feature", "atac_barcode"]
    expected_orientation: Orientation = "forward"


# ---- signature tests: a CLOSED set == the scorer's evaluators ----
class _Seg(_Forbid):
    """A test addressed to a segment by element name XOR (start, end)."""

    read: str
    element: str | None = None
    start: int | None = None
    end: int | None = None

    @model_validator(mode="after")
    def _one_address(self) -> _Seg:
        by_name = self.element is not None
        by_coord = self.start is not None and self.end is not None
        if by_name == by_coord:
            raise ValueError("address a segment by element name XOR (start, end)")
        return self


class ReadCount(_Forbid):
    test: Literal["read_count"]
    roles: int  # biological + barcode ROLE count, never raw file count


class SegmentLength(_Forbid):
    test: Literal["segment_length"]
    read: str
    length: int
    tolerance: int = 0


class HasSegment(_Seg):
    test: Literal["has_segment"]
    kind: Literal["constant", "random", "polyT", "polyA"]


class DistinctRatio(_Seg):
    test: Literal["distinct_ratio"]
    expect: Literal["low", "high"]  # SUPPORTS-only; depth-dependent, never a gate


class OnlistHitRate(_Seg):
    test: Literal["onlist_hit_rate"]
    onlist: str
    orientation: Orientation = "either"
    min: float


class MotifPresent(_Forbid):
    test: Literal["motif_present"]
    read: str
    motif: str
    where: Literal["read_start", "read_end", "anywhere", "window"] = "anywhere"
    search_start: int | None = None
    search_end: int | None = None
    max_mismatch: int = 1
    min_rate: float = 0.5


class BaseComposition(_Seg):
    test: Literal["base_composition"]
    base: Literal["A", "C", "G", "T", "N"]
    min_fraction: float


class HeaderIndex(_Forbid):
    test: Literal["header_index"]
    present: bool


Test = Annotated[
    ReadCount
    | SegmentLength
    | HasSegment
    | DistinctRatio
    | OnlistHitRate
    | MotifPresent
    | BaseComposition
    | HeaderIndex,
    Field(discriminator="test"),
]


class Support(_Forbid):
    when: Test
    weight: float = 1.0


class Signature(_Forbid):
    requires: list[Test]  # hard AND-gates (no distinct_ratio here — it's depth-dependent)
    supports: list[Support]  # additive evidence (onlist + distinct_ratio live here)
    excludes: list[Test]  # anti-gates: any pass => disqualify


class Backend(_Forbid):
    """A data template mapping to a workflow module. Only ``{onlist:<alias>}`` interpolation is legal."""

    module: str
    params: dict[str, str | int | float | list[str]]

    def check_tokens(self, onlist_aliases: set[str]) -> None:
        """Reject any interpolation token that is not a declared ``{onlist:<alias>}``."""
        for value in self._strings():
            for match in _ANY_BRACE.finditer(value):
                token = _ONLIST_TOKEN.match(match.group(0))
                if token is None:
                    raise ValueError(
                        f"illegal template expression {match.group(0)!r} "
                        "(only {onlist:<alias>} is allowed)"
                    )
                if token.group(1) not in onlist_aliases:
                    raise ValueError(f"unknown onlist alias {token.group(1)!r}")

    def _strings(self) -> list[str]:
        out: list[str] = []
        for value in self.params.values():
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, list):
                out.extend(v for v in value if isinstance(v, str))
        return out


class Confusable(_Forbid):
    id: str
    relationship: Literal["processing_equivalent", "processing_divergent"]
    distinguishable_by: list[Mechanism]
    note: str = ""

    @model_validator(mode="after")
    def _shape(self) -> Confusable:
        if self.relationship == "processing_divergent" and self.distinguishable_by == ["none"]:
            raise ValueError("a processing_divergent pair cannot be distinguishable_by [none]")
        return self


class Identity(_Forbid):
    id: str
    version: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    assay_ontology: list[str] = Field(default_factory=list)
    modality: Literal["rna", "atac", "multi"] = "rna"


class Spec(_Forbid):
    """A complete, self-validating technology specification."""

    schema_version: int
    identity: Identity
    reads: list[Read]
    onlists: dict[str, OnlistRef]
    signature: Signature
    backend: Backend
    confusable_with: list[Confusable] = Field(default_factory=list)
    decidable_by: list[Decidable] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cross_refs(self) -> Spec:
        aliases = set(self.onlists)
        read_ids = {r.id for r in self.reads}
        elements_by_read = {r.id: {e.name for e in r.elements} for r in self.reads}

        # every onlist alias referenced by an element must be declared
        for read in self.reads:
            for el in read.elements:
                if el.onlist and el.onlist not in aliases:
                    raise ValueError(f"element {el.name!r}: unknown onlist {el.onlist!r}")
                if el.anchor and el.anchor.ref_element:
                    if el.anchor.ref_element not in elements_by_read[read.id]:
                        raise ValueError(
                            f"element {el.name!r}: anchor ref_element "
                            f"{el.anchor.ref_element!r} not in read {read.id!r}"
                        )

        # every signature test must reference a declared read (and element/onlist)
        tests: list[Test] = [
            *self.signature.requires,
            *self.signature.excludes,
            *(s.when for s in self.signature.supports),
        ]
        for t in tests:
            read = getattr(t, "read", None)
            if read is not None and read not in read_ids:
                raise ValueError(f"signature test references unknown read {read!r}")
            element = getattr(t, "element", None)
            if element is not None and read is not None:
                if element not in elements_by_read.get(read, set()):
                    raise ValueError(
                        f"signature test references unknown element {element!r} in read {read!r}"
                    )
            onlist = getattr(t, "onlist", None)
            if onlist is not None and onlist not in aliases:
                raise ValueError(f"signature test references unknown onlist {onlist!r}")

        self.backend.check_tokens(aliases)
        return self
