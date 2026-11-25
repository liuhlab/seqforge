"""The open lists a writing model drafts for a study reading: what it is told, and what it answers.

The writer reads a study's records and papers and lists, for every label, the distinct values the
sequenced samples take, each with a quote, and the labels the study varied on purpose, each with
its levels. It never says which sample has which value: Jev, a model that answers closed questions
with probabilities, picks that from these lists. :data:`INSTRUCTIONS` is what the writer is told,
plus the text of each assay guide that applies; :func:`parse_lists` reads its answer, or says why
the answer cannot be used.

An **assay guide** is a short text file in ``guides/`` beside this module, added for a study whose
records name one of its library strategies: :func:`guides_for` picks them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: The labels the writer lists values for. Code writes the other three: biosample, donor, species.
LABELS = (
    "assay",
    "target",
    "control_type",
    "sex",
    "age",
    "development_stage",
    "tissue",
    "cell_type",
    "cell_line",
    "disease",
    "genotype",
    "strain",
    "treatment",
    "dose",
    "duration",
)
#: The labels code writes on its own, which are never a factor.
CODE_LABELS = ("biosample", "donor", "species")
#: The labels whose value may carry a number and a unit.
NUMERIC = ("age", "dose", "duration")
#: What each label holds, as the writer and Jev are told.
MEANING = {
    "assay": "the kind of sequencing experiment, such as RNA-Seq, ChIP-Seq or ATAC-seq",
    "target": "what a pull-down enriches: a histone mark or a protein; na for an assay with no "
    "pull-down and for a control library",
    "control_type": "the kind of background-control library, such as input or IgG; na for every "
    "other library",
    "sex": "the sex: female, male, hermaphrodite or mixed",
    "age": "the age, with a number and a unit",
    "development_stage": "the stage of life, such as embryo, larval stage or adult",
    "tissue": "the tissue or organ",
    "cell_type": "the cell type",
    "cell_line": "the cell line",
    "disease": "the disease or disease model, or healthy",
    "genotype": "the genotype",
    "strain": "the strain",
    "treatment": "what was applied to the sample: a drug, a diet, a vector, an infection or a "
    "procedure",
    "dose": "how much of the treatment",
    "duration": "how long the treatment itself was applied",
}
#: The guides by file, each with the library strategies that pick it, written as
#: :func:`_strategy` folds them.
GUIDES = {"pull-down.txt": ("chipseq", "chipexo", "cutrun", "cuttag")}

_QUOTE = """Q = {"source": "a GSE or GSM id, or each_sample, or pmid:<number>",
     "field": "for a record: the field name exactly as written before the colon, such as
               Sample_characteristics_ch1; for a paper: title, abstract or the section heading
               shown in square brackets",
     "text": "words copied exactly from that one field, one unbroken passage of at most 40 words"}"""

INSTRUCTIONS = (
    """You read the GEO records of one study, and its paper, and write the open lists that a later
step uses to describe every sequenced sample. You do not say which sample has which value: a later
step decides that from your lists. Write only what the text supports.

Every sequenced sample gets these labels:
"""
    + "\n".join(f"- {label}: {meaning}" for label, meaning in MEANING.items())
    + """

Points of meaning:
- control_type is only for background-control libraries, such as input or IgG. An untreated or
  wild-type group that the study compares the others with is a baseline, not a control.
- duration is the length of the treatment itself. Any other time, such as from the end of a
  treatment to the harvest, is a label of the study's own: put it under extra_labels.
- In a study that treats some samples, an untreated or vehicle sample's treatment is its text,
  such as "untreated" or "vehicle (PBS)". The gap na is for a study with no treatment.
- dose and duration give a value and a unit when one number applies. When none does, such as
  "overnight" or "per side", give the text alone.
- disease is "healthy" for a sample with no disease and no disease model.

Answer with ONE JSON object:
{
 "factors": [  // the labels the study varied ON PURPOSE to compare, such as treatment or age
   {"label": "one of the labels above, or a short snake_case name of the study's own",
    "levels": [{"name": "a short unique name", "text": "the source's own words for this level",
                "value": number or [low, high] or null, "unit": "one word or null",
                "search": "a short name to look the text up in an ontology, or null",
                "quote": Q or null}],
    "baseline": "the level name the study compares the others with, or null",
    "ordered": true or false, "quote": Q or null, "against": [Q], "how": "at most 15 words"}],
 "labels": {   // EVERY label above, as a key
   "<label>": {"values": [{"text": "the source's own words", "value": number or [low, high] or null,
                           "unit": "one word or null", "search": "a short name, or null",
                           "quote": Q or null, "against": [Q], "how": "at most 15 words"}],
               "gap": null or {"kind": "na" or "unknown", "why": "at most 15 words"}}},
 "extra_labels": { "<snake_case name>": same shape as a label },  // labels of the study's own
 "ties": [{"control": "GSM...", "pulldowns": ["GSM..."], "quote": Q, "how": "at most 15 words"}]
}
"""
    + _QUOTE
    + """

Rules:
- A label the study varied is a factor. For that label leave "values" empty: the levels say it.
- List every distinct value a label takes among the sequenced samples. A label with one value for
  every sample has one entry. A label with no value has empty values and a gap: "na" when it does
  not apply, such as the cell line of tissue taken from an animal, "unknown" when it applies but
  nothing read says it.
- "search" is a short name of the value, for an ontology search only: "hippocampus" for "Rat
  hippocampus", "Alzheimer's disease" for "Alzheimer's disease (5XFAD)". Give it for assay,
  tissue, cell_type, cell_line, disease, treatment and development_stage. For a treatment of
  several compounds, give a list of names, one per compound.
- Copy every quote exactly: never join pieces, never fix spelling, never add words. Prefer the
  words of a GEO record over the paper when both say it. each_sample means every sample record
  holds the words in that field.
- "against" lists the passages that say otherwise, when the sources disagree, such as an age the
  paper gives differently from the records. A passage that is merely not used is not against.
- "ties" stays empty unless the notes for an assay below ask for it.
- Only sequenced samples count. GEO supplementary files are not available: a value found only
  there is unknown.
"""
)

#: The note sent with the one retry, after an answer that could not be used.
RETRY = (
    "\n\nYour previous answer could not be used: {reason}. Answer again: one JSON object in "
    "the shape the instructions give."
)


def _strategy(name: str) -> str:
    """A library strategy folded for matching: ``ChIP-Seq`` and ``CUT&Tag`` as ``chipseq``, ``cuttag``."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def guides_for(strategies: Iterable[str]) -> list[str]:
    """The guides a study's library strategies pick, by file name."""
    folded = {_strategy(s) for s in strategies}
    return [name for name, picked_by in GUIDES.items() if folded & set(picked_by)]


def guide_text(name: str) -> str:
    """One guide's text."""
    return files("seqforge.study").joinpath("guides", name).read_text(encoding="utf-8")


def instructions(guides: Iterable[str]) -> str:
    """What the writer is told: the general instructions, then each guide's text."""
    notes = [guide_text(name) for name in guides]
    return INSTRUCTIONS + "".join(f"\nNotes for this study's assay:\n{n}" for n in notes)


class _Loose(BaseModel):
    """A part of the writer's answer: unknown keys are dropped, and plain types coerced.

    A level named ``3`` is the text ``"3"``: a name or a text written as a number is not broken.
    """

    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)


class Quote(_Loose):
    """A quote as the writer cites it: where, and the words."""

    source: str
    field: str = ""
    text: str


class Value(_Loose):
    """One value a label takes, or one level of a factor."""

    name: str | None = None
    text: str = ""
    value: float | list[float] | None = None
    unit: str | None = None
    search: str | list[str] | None = None
    quote: Quote | None = None
    against: list[Quote] = Field(default_factory=list)
    how: str = ""

    @field_validator("value")
    @classmethod
    def _range(cls, value: float | list[float] | None) -> float | list[float] | None:
        if isinstance(value, list) and len(value) != 2:
            raise ValueError("a range is [low, high]")
        return value


class Gap(_Loose):
    """Why a label has no value."""

    kind: str
    why: str = ""


class Label(_Loose):
    """The values one label takes, or why it has none."""

    values: list[Value] = Field(default_factory=list)
    gap: Gap | None = None


class FactorList(_Loose):
    """A label the writer says the study varied on purpose, with its levels."""

    label: str
    levels: list[Value] = Field(min_length=1)
    baseline: str | None = None
    ordered: bool | None = None
    quote: Quote | None = None
    against: list[Quote] = Field(default_factory=list)
    how: str = ""


class Tie(_Loose):
    """A control library and the pull-downs made from its material, with the words that say so."""

    control: str
    pulldowns: list[str] = Field(min_length=1)
    quote: Quote | None = None
    how: str = ""


class Lists(_Loose):
    """The writer's whole answer."""

    factors: list[FactorList] = Field(default_factory=list)
    labels: dict[str, Label]
    extra_labels: dict[str, Label] = Field(default_factory=dict)
    ties: list[Tie] = Field(default_factory=list)


def snake(name: str) -> str:
    """A label name as the reading writes one: lower case, words joined by ``_``."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _json_object(text: str) -> object:
    """The JSON the text is, or else the first JSON object in it, read from its opening brace.

    Raises the error of reading the whole text when no brace opens an object.
    """
    try:
        return json.loads(text)
    except ValueError as whole:
        decoder = json.JSONDecoder()
        for at in (m.start() for m in re.finditer(r"\{", text)):
            try:
                data, _ = decoder.raw_decode(text, at)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
        raise whole


def parse_lists(text: str) -> tuple[Lists | None, str | None]:
    """The writer's answer as lists, or ``None`` and why it cannot be used.

    The writer answers in text, with no schema, so the object is taken out of it: a code fence or
    prose around it is no fault. An answer is broken when it holds no JSON object, does not have
    the shape the instructions give, or names one level of a factor twice.
    """
    try:
        data = _json_object(text)
    except (TypeError, ValueError) as exc:
        return None, f"not JSON: {exc}"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    try:
        lists = Lists.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        return None, f"not in the shape asked for, at {where}: {first['msg']}"
    for factor in lists.factors:
        names = [str(level.name or level.text) for level in factor.levels]
        if len(set(names)) != len(names):
            return None, f"factor {factor.label} names a level twice"
    return lists, None
